"""TurnSupervisor — единый владелец фоновых задач одного хода Claude.

На время обработки запроса (от пуша в канал до финального ответа) на топик
живут три задачи:
  * typing   — индикатор «печатает…» в Telegram;
  * watchdog — сторож зависаний (лог не растёт + CPU-дерево молчит);
  * error-relay — трансляция ошибок API/ретраев/краш-рестартов из claude.log.

Плюс флаг _last_action_was_reply — гейт Stop-фолбэка («потерянный финал»
хода: модель закончила голым текстом вместо reply_to_user).

Раньше всё это было размазано по TelegramBot четырьмя словарями с ручной
синхронизацией (REVIEW.md D5/D6); теперь жизненный цикл хода в одном месте:
start() открывает ход, stop() закрывает, forget() — при удалении сессии.

Зависимости отданы колбэками, чтобы модуль не знал про транспорт:
  send(session, text)     — служебное сообщение в сессию (ядро шлёт во все
                            адаптеры);
  typing(session) -> bool — показать «печатает…»; False = слать некуда,
                            цикл гаснет.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, TYPE_CHECKING

from .ansi import strip_ansi
from .logsignals import detect_log_signals

if TYPE_CHECKING:
    from .sessions import Session, SessionManager

logger = logging.getLogger(__name__)

# Индикатор «печатает…»: Telegram гасит его через ~5 с, обновляем чаще.
TYPING_INTERVAL = 4.0

# Сторож зависаний: если после отправки Claude молчит и claude.log не растёт
# STALL_CHECKS проверок подряд — предупреждаем в топик (рост лога = живой
# ход/размышление, отсутствие роста = завис).
WATCHDOG_GRACE = 20.0
WATCHDOG_CHECK = 15.0
STALL_CHECKS = 2
# Ретранслятор ошибок API из claude.log: как часто нюхать хвост, как часто
# писать в чат (между любыми двумя алертами), и сколько глушить ОДНУ И ТУ ЖЕ
# ошибку — чтобы 10-минутная петля 400-х не родила 10 одинаковых сообщений.
ERROR_RELAY_INTERVAL = 6.0
ERROR_RELAY_COOLDOWN = 60.0
ERROR_RELAY_REPEAT = 600.0
# Живой сигнал ретраев: пере-файр при росте attempt, но не чаще раза в N сек.
RETRY_SURFACE_INTERVAL = 30.0


async def _false() -> bool:
    """Заглушка проверки учётки для сборок без неё (тесты, урезанные ядра)."""
    return False


def read_log_delta(path: Path, offset: int) -> tuple[bytes, int]:
    """Прочитать приращение лога с offset: (сырой delta, новый offset=size).

    Если файл стал меньше offset (resume/ротация/усечение) — читаем с начала.
    Чистая функция над файлом — тестируется без петли/Telegram (REVIEW.md B5).
    Бросает OSError наружу (вызывающий continue'ит).
    """
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        delta = fh.read(size - offset)
    return delta, size


class TurnSupervisor:
    """Фоновые задачи хода + Stop-гейт, ключ — имя сессии."""

    def __init__(
        self,
        manager: "SessionManager",
        t: Callable[..., str],
        send: Callable[["Session", str], Awaitable[None]],
        typing: Callable[["Session"], Awaitable[bool]],
        status: Callable[..., Awaitable[None]] | None = None,
        tool_inflight: Callable[[str], bool] | None = None,
        auth_expired: Callable[["Session"], Awaitable[bool]] | None = None,
    ):
        self.manager = manager
        self.t = t
        self._send = send
        self._typing_action = typing
        # Подтверждение «учётка действительно протухла» (claude auth status).
        # Баннер из лога — только повод спросить: текст TUI встречается и в
        # прозе модели, а тревога «вы разлогинены» на ровном месте хуже молчания.
        self._auth_expired = auth_expired or (lambda session: _false())
        # Идёт ли СЕЙЧАС foreground-тул (PreToolUse без PostToolUse) — сигнал
        # «жив, даже если CPU дерева на миг замер» (напр. Bash `sleep`/сетевое
        # ожидание). Заменяет has_kids из /proc, который под bwrap структурно
        # всегда True (дочерний bwrap-init) и делал вотчдог вечно-«живым».
        self._tool_inflight = tool_inflight or (lambda name: False)
        # Колбэк живого статуса (спиннер-глагол + реальная модель → бабл).
        # None = не показывать. Сигнатура: (name, pulse=…, model=…).
        self._status = status or (lambda name, **kw: asyncio.sleep(0))
        # session.name -> задача, шлющая «печатает…» пока Claude обрабатывает запрос.
        self._typing: dict[str, asyncio.Task] = {}
        # имя сессии -> сторож зависаний (стартует/гаснет вместе с typing).
        self._watchdogs: dict[str, asyncio.Task] = {}
        # имя сессии -> ретранслятор ошибок API (rate-limit/5xx) из claude.log.
        self._error_relays: dict[str, asyncio.Task] = {}
        # имя сессии -> было ли ПОСЛЕДНЕЕ действие модели (до этого Stop-события)
        # вызовом reply_to_user. Гейт для handle_stop_event: длинный ход
        # часто заканчивается голым текстом вместо tool-вызова — этот текст
        # канал не видит и он не долетает до Telegram (REVIEW.md: 9/9 длинных
        # ходов в живой сессии теряли финал именно так). ВАЖНО: это не «reply
        # был где-то в ходе» — reply мог случиться в середине, а потом ещё
        # шли Bash/Edit и голый текст после них уже потерян. Поэтому флаг
        # ставится True на reply_to_user и явно сбрасывается в False
        # любым ДРУГИМ тул-вызовом — остаётся True только если reply был
        # самым последним, что видел бот перед Stop.
        self._last_action_was_reply: dict[str, bool] = {}

    # ── жизненный цикл хода ─────────────────────────────────────

    def start(self, name: str) -> None:
        """«печатает…» + сторож зависаний + релей ошибок на время запроса.

        Все гаснут финальным ответом (stop() в handle_reply).
        """
        self.stop(name)
        self._typing[name] = asyncio.create_task(self._guarded(self._typing_loop(name), "typing"))
        self._watchdogs[name] = asyncio.create_task(self._guarded(self._watchdog_loop(name), "watchdog"))
        self._error_relays[name] = asyncio.create_task(
            self._guarded(self._error_relay_loop(name), "error_relay")
        )

    @staticmethod
    async def _guarded(coro, label: str) -> None:
        """Обёртка фоновой задачи хода: неожиданное исключение внутри цикла
        иначе умирало бы молча («Task exception was never retrieved») — теперь
        оно логируется, а не теряется. CancelledError пробрасываем (штатный стоп)."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Фоновая задача хода '%s' упала", label)

    def stop(self, name: str) -> None:
        for registry in (self._typing, self._watchdogs, self._error_relays):
            task = registry.pop(name, None)
            if task is not None:
                task.cancel()

    def forget(self, name: str) -> None:
        """Полная зачистка состояния топика (удаление сессии)."""
        self.stop(name)
        self._last_action_was_reply.pop(name, None)

    # ── Stop-гейт «потерянного финала» ──────────────────────────

    def note_tool(self, name: str, is_reply: bool) -> None:
        """Отметить тул-вызов: reply_to_user ставит флаг, любой другой
        (в т.ч. send_file_to_user) — сбрасывает."""
        self._last_action_was_reply[name] = is_reply

    def pop_reply_flag(self, name: str) -> bool:
        """Прочитать и сбросить флаг на Stop-событии (новое окно до следующего
        Stop)."""
        flag = self._last_action_was_reply.get(name, False)
        self._last_action_was_reply[name] = False
        return flag

    # ── фоновые циклы ───────────────────────────────────────────

    async def _typing_loop(self, name: str) -> None:
        while True:
            session = self.manager.get(name)
            if session is None or not await self._typing_action(session):
                return
            await asyncio.sleep(TYPING_INTERVAL)

    async def _watchdog_loop(self, name: str) -> None:
        """Если Claude молчит И claude.log не растёт несколько проверок подряд —
        это зависание (а не долгое размышление): предупреждаем в топик.
        """
        session = self.manager.get(name)
        if session is None:
            return
        log = session.session_dir / "claude.log"
        await asyncio.sleep(WATCHDOG_GRACE)
        try:
            last_size = log.stat().st_size
        except OSError:
            last_size = 0
        stalls = 0
        while True:
            await asyncio.sleep(WATCHDOG_CHECK)
            try:
                size = log.stat().st_size
            except OSError:
                size = last_size
            # Жизнь = лог растёт ИЛИ дерево claude ест CPU ИЛИ идёт foreground-тул.
            # Одних байт мало: спиннер «almost done» может на секунды замолчать
            # в нормальной работе, и это не зависание. is_busy — только CPU-дельта
            # (has_kids выкинут: под bwrap всегда True); _tool_inflight ловит
            # «тул идёт, но CPU замер» (Bash sleep/сетевое ожидание).
            alive = (
                size != last_size
                or self.manager.is_busy(session)
                or self._tool_inflight(name)
            )
            last_size = size
            stalls = 0 if alive else stalls + 1
            if stalls < STALL_CHECKS:
                continue
            # Завис: снимаем «печатает», шлём диагностику один раз.
            t = self._typing.pop(name, None)
            if t is not None:
                t.cancel()
            tail = await asyncio.to_thread(self.manager.tail_log, session, 10)
            msg = self.t("stalled")
            if tail:
                msg += "\n\n" + self.t("session_died_tail", tail=tail[:1200])
            await self._send(session, msg)
            return

    async def _error_relay_loop(self, name: str) -> None:
        """Транслировать в чат, что Claude делает, когда тулов ещё нет.

        Сигналы нюхаем из ПРИРАЩЕНИЯ claude.log (отрисовка TUI) с момента старта
        хода; разбирает их logsignals.detect_log_signals. Три класса:
          1. «API Error: <код>» — настоящая ошибка API. Триггер — именно баннер с
             кодом, не слова «rate-limit» в прозе ответов (раньше ловили ложное).
             Класс задаёт действие: rate-limit→/model, 400-протокол→/clear,
             прочее→«задерживается». Раз в ERROR_RELAY_COOLDOWN, та же — REPEAT.
          2. Ретрай «attempt K/M» — ЖИВОЙ: пере-файр при росте K (видно прогресс
             3/100 → 47/100), throttle RETRY_SURFACE_INTERVAL. Главное против
             «5 минут тишины, непонятно что делает».
          3. Краш-рестарт «Resume this session» mid-хода — Claude упал и поднялся.
             Растёт счётчик → предупреждаем (раз в COOLDOWN), намекаем /close_session.
        """
        session = self.manager.get(name)
        if session is None:
            return
        log = session.session_dir / "claude.log"
        # Точку отсчёта берём СРАЗУ (до паузы), а читаем — после: иначе всё, что
        # TUI напечатал в первые WATCHDOG_GRACE секунд, выпадало из первой
        # дельты. Мимо проскакивал ровно самый быстрый сигнал — баннер
        # протухшей учётки, который появляется мгновенно и ход на этом кончается.
        try:
            offset = log.stat().st_size  # всё, что уже в логе до хода, — не наше
        except OSError:
            return
        await asyncio.sleep(WATCHDOG_GRACE)
        last_surfaced = -ERROR_RELAY_COOLDOWN
        last_sig: str | None = None
        last_sig_at = -ERROR_RELAY_REPEAT
        last_retry_k = 0
        last_retry_at = -RETRY_SURFACE_INTERVAL
        restart_count = 0
        last_restart_at = -ERROR_RELAY_COOLDOWN
        last_quota_at = -ERROR_RELAY_REPEAT
        last_login_at = -ERROR_RELAY_REPEAT
        loop_time = asyncio.get_running_loop().time

        async def _surf(text: str) -> None:
            try:
                await self._send(session, text)
            except Exception as e:
                logger.debug("error_relay: не удалось отправить: %s", e)

        while True:
            await asyncio.sleep(ERROR_RELAY_INTERVAL)
            # Читаем ТОЛЬКО приращение с прошлого тика (seek), а не весь лог
            # целиком каждый тик (REVIEW.md B5). read_log_delta — чистая функция
            # (тестируется отдельно), сама разруливает усечение/ротацию.
            try:
                delta, offset = read_log_delta(log, offset)
            except OSError:
                continue
            chunk = strip_ansi(delta)
            sig = detect_log_signals(chunk)
            now = loop_time()

            # 0. Живой статус: спиннер-глагол + время + токены (из TUI-лога) и
            #    реальная модель (из транскрипта, после подмены прокси). Признак
            #    «модель жива» и ЧЕМ занята, когда tool-событий нет (думает/ждёт).
            if sig["pulse"]:
                try:
                    pulse = sig["pulse"]
                    if sig["tokens"]:
                        pulse += f" · ↓{sig['tokens']} tok"
                    model = await asyncio.to_thread(self.manager.read_last_model, session)
                    await self._status(name, pulse=pulse, model=model or "")
                except Exception as e:
                    logger.debug("status: %s", e)

            # 0б. Лимит-баннер (Weekly/Monthly exhausted, серверный throttle без
            #     3-значного кода) — частая причина «часами молчит и не едет»:
            #     модель ретраит недельный лимит по кругу. Показываем причину и
            #     дату сброса; редко (раз в REPEAT), чтобы не спамить в цикле.
            if sig["quota"] is not None and now - last_quota_at >= ERROR_RELAY_REPEAT:
                last_quota_at = now
                reset = sig["quota"]
                await _surf(
                    self.t("api_error_quota", reset=reset) if reset
                    else self.t("api_error_quota_noreset")
                )

            # 0в. Протухшая учётка. Самый тихий отказ: claude жив и отвечает на
            #     каждое сообщение сам («Login expired · Please run /login») за
            #     0 с — ни тула, ни reply_to_user, в чат не долетает ничего, а
            #     сообщения оператора съедаются. Баннер в логе — только повод
            #     спросить `claude auth status`: его текст встречается и в прозе
            #     модели, и ложная тревога «вы разлогинены» дороже задержки.
            if sig["login"] and now - last_login_at >= ERROR_RELAY_REPEAT:
                try:
                    expired = await self._auth_expired(session)
                except Exception as e:  # релей не должен падать на проверке
                    expired = False
                    logger.debug("error_relay: проверка учётки: %s", e)
                if expired:
                    last_login_at = now
                    await _surf(self.t("login_expired"))

            # 1. Ошибка API (с дедупом: раз в COOLDOWN, та же — раз в REPEAT).
            if sig["api_error"]:
                code, klass = sig["api_error"]
                s = f"{code.decode()}:{klass}"
                cooled = now - last_surfaced >= ERROR_RELAY_COOLDOWN
                fresh = not (s == last_sig and now - last_sig_at < ERROR_RELAY_REPEAT)
                if cooled and fresh:
                    last_surfaced = now
                    last_sig, last_sig_at = s, now
                    msg = self.t(f"api_error_{klass}")
                    if klass == "protocol":
                        # Прочитать загрязнённый контекст и приложить эксцепт —
                        # чтобы было видно, ЧТО именно отравлено (чужой бэкенд).
                        try:
                            excerpt = await asyncio.to_thread(
                                self.manager.read_pollution_excerpt, session
                            )
                        except Exception as e:  # релей не должен падать на чтении
                            excerpt = None
                            logger.debug("error_relay: pollution excerpt: %s", e)
                        if excerpt:
                            msg += "\n\n" + self.t(
                                "api_error_pollution_tail", excerpt=excerpt
                            )
                    await _surf(msg)

            # 2. Живой ретрай: показываем прогресс (3/100 → 47/100) в СТАТУСЕ
            #    бабла (set_status), а не отдельным сообщением — он редактируется
            #    на месте (не насыпать N сообщений), сам резолвится: как модель
            #    оживёт, следующий pulse перезапишет статус; конец хода — бабл
            #    закроется. Throttle — RETRY_SURFACE_INTERVAL (реже правок).
            if sig["retry"]:
                k, total = sig["retry"]
                if k > last_retry_k and now - last_retry_at >= RETRY_SURFACE_INTERVAL:
                    last_retry_k = k
                    last_retry_at = now
                    try:
                        await self._status(
                            name, pulse=self.t("api_retrying", attempt=k, total=total)
                        )
                    except Exception as e:
                        logger.debug("error_relay: retry-статус: %s", e)

            # 3. Краш-рестарт-луп: баннер «Resume this session» mid-хода = Claude
            #    упал и поднялся. Растёт счётчик — предупреждаем (раз в COOLDOWN).
            if sig["restarts"]:
                restart_count += sig["restarts"]
                if now - last_restart_at >= ERROR_RELAY_COOLDOWN:
                    last_restart_at = now
                    await _surf(self.t("session_restart_loop", count=restart_count))
