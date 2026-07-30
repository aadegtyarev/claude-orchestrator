"""OrchestratorCore — транспорт-независимое ядро оркестратора.

Владеет всем, что не зависит от конкретного мессенджера/интерфейса:
  * командной логикой (создание/остановка/удаление сессий, /stats, /model…);
  * маршрутизацией ответов Claude (handle_reply/tool/stop/permission из
    reply_server) во ВСЕ активные адаптеры;
  * статус-баблом (bubble.py) и фоновыми задачами хода (turn.py);
  * jail'ом на отправку файлов и bash-терминалом;
  * журналом событий сессии (история для веб-интерфейса).

Адаптеры (orchestrator/adapters/*) реализуют Transport (core/transport.py):
принимают команды пользователя и вызывают методы ядра; ядро доставляет
исходящее во все адаптеры разом. Происхождение сообщения (Origin) ездит через
Claude как context_id = "<адаптер>:<имя-сессии>:<токен-адаптера>" — по нему
ядро находит сессию и отдаёт origin-адаптеру возможность ответить цитатой.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from box import profiles

from . import bashshell
from .bashshell import BashShellManager
from .bubble import BubbleManager
from .termhistory import TermHistory
from .termmode import TerminalMode
from .errors import UserError
from .history import HistoryLog
from .permission import PermissionRelay
from .reports import parse_cost
from .sessions import Session, SessionError, SessionManager
from .slug import slugify
from .subagentnaming import SubagentNaming
from .texts import get_texts
from .toolactivity import ToolActivity
from .toolline import AGENT_SPAWN_TOOLS, shorten, tool_line, tool_line_full
from .transcript import read_last_model
from .transport import Origin, Transport
from .turn import TurnSupervisor
from ..config import Config

logger = logging.getLogger(__name__)

# /bash: как часто перечитывать вывод и перерисовывать статус, сколько ждать
# команду и сколько текста показывать.
BASH_POLL_INTERVAL = 1.5
BASH_TIMEOUT = 600.0
BASH_OUTPUT_LIMIT = 3500
# Сколько файлов с полным выводом держим в папке сессии (см. _trim_bash_outputs).
BASH_OUTPUTS_KEEP = 20

# Синонимы моделей для кнопок /model. Маппинг на конкретные версии делает
# сам Claude Code — мы не дублируем его каталог и не отстаём от переименований.
MODEL_ALIASES = ["fable", "opus", "sonnet", "haiku"]

# Команда → фича из OrchestratorCore.features(), без которой она не работает.
# Команды, которых здесь нет, доступны всегда. Адаптеры обязаны спрашивать
# core.command_available() при построении меню/справки — так выключенная фича
# не оставляет следов ни в одном интерфейсе.
#
# `bash` СПЕЦИАЛЬНО не гейтим: в топике сессии под agent-vm он отказывает
# (одна VM на каталог), но в главном чате это операторский терминал на хосте —
# он работает, и прятать его нельзя.
COMMAND_FEATURE = {
    "wallet": "wallet",
    "stats": "stats",
}

# UserError импортируется выше из .errors и реэкспортируется здесь: адаптеры и
# тесты берут его как `from ...core.app import UserError` (обратная совместимость).


class OrchestratorCore:
    def __init__(self, config: Config, manager: SessionManager):
        self.config = config
        self.manager = manager
        self._texts = get_texts(config.bot_lang)
        self.adapters: dict[str, Transport] = {}
        self.modules: list = []  # модули (modules/*) — start/stop вместе с ядром
        # Редакторы исходящего в чат текста (reply/notice/бабл): модули вымарывают
        # свои значения. wallet чистит значения секретов (shared модель видит и
        # может случайно эхнуть — safety-net, чтобы не улетело в Telegram). Пусто
        # без модулей → _scrub бесплатный no-op.
        self.output_redactors: list[Callable[[str], str]] = []
        self.bubbles = BubbleManager(
            self._transports, manager.get, self.t, config.delete_bubble,
            unblock_available=self._unblock_available,
            persist_path=config.sessions_dir / ".live_bubbles.json",
        )
        # Активность инструментов сессии (кнопка ⏭ + сигнал жизни для вотчдога) —
        # см. core/toolactivity.py. Кормится из hook-хендлеров, читается кнопкой/
        # вотчдогом, снимается одним forget(name) на границе хода/teardown.
        self.tools = ToolActivity()
        # Именование завершившихся сабагентов (agent_id → тип) — см.
        # core/subagentnaming.py. Кормится из hook-хендлеров, снимается pop при
        # SubagentStop и forget(name) на границе хода/teardown.
        self.naming = SubagentNaming()
        # Фоновые задачи хода (typing/watchdog/error-relay) и Stop-гейт —
        # единым владельцем (turn.py). Доставка — колбэками в адаптеры.
        self.turns = TurnSupervisor(
            manager, self.t, self.notice, self._typing_any, self.bubbles.set_status,
            tool_inflight=self.tools.inflight,
        )
        # Постоянные bash-терминалы (мимо Claude Code): ключ — см. bash_key.
        self.bash = BashShellManager()
        # Топики, работающие как терминал (/term on): обычные сообщения там
        # уходят в шелл, а не claude. Только в памяти — см. termmode.
        self.term = TerminalMode()
        # Маркер конца текущей команды на терминал — нужен кнопке ⏹, чтобы
        # дописать его руками после Ctrl-C (см. interrupt_bash).
        self._done_markers: dict[str, str] = {}
        # История команд терминала — то, что в настоящем шелле даёт «стрелка вверх».
        # Хранение в памяти на время жизни оболочки — см. termhistory.
        self.termhist = TermHistory()
        # Мелкие фоновые задачи ядра без иного владельца — держим ссылку
        # (asyncio хранит только слабую), иначе GC мог бы собрать на лету.
        self._bg_tasks: set[asyncio.Task] = set()
        # Журнал событий per-сессия: показать историю в веб-интерфейсе после
        # перезагрузки страницы. Персистится через graceful-рестарт (иначе
        # пропадала при каждом рестарте — «история потерялась»); полная — в
        # транскрипте CC.
        self.journal = HistoryLog(config.sessions_dir / ".history.json")
        self.journal.load()
        # Permission relay (кнопки «разрешить?» + сбор первого ответа) — см.
        # core/permission.py. Владеет ожидающими запросами; коллабораторы (бродкаст
        # в адаптеры, журнал) инъектируются.
        self.perms = PermissionRelay(
            manager, self.t, self._each_transport, self._record
        )
        # Хуки «сессия создана» — модули дописывают свою обвязку в папку сессии.
        self.session_hooks: list[Callable[[Session], Awaitable[None]]] = []
        manager.on_dead = self.notify_session_dead
        manager.on_docker_launch = self.notify_docker_launch

    def t(self, key: str, **kwargs) -> str:
        return self._texts[key].format(**kwargs)

    # ── адаптеры и модули ───────────────────────────────────────

    def register_adapter(self, transport: Transport) -> None:
        self.adapters[transport.name] = transport

    def _transports(self) -> list[Transport]:
        return list(self.adapters.values())

    async def _each_transport(self, action, label: str, *, warn: bool = False) -> None:
        """Выполнить `action(tr)` на каждом транспорте, изолируя сбой одного
        адаптера от прочих (per-adapter try/except — единая точка). `warn=True` —
        короткий logger.warning для ОЖИДАЕМОГО сбоя доставки (сеть/адаптер);
        иначе logger.exception со стеком для неожиданного. `_deliver_text`/
        `_send_file`/`_typing_any` НЕ идут через этот хелпер: они фильтруют origin
        и агрегируют возврат."""
        for tr in self._transports():
            try:
                await action(tr)
            except Exception as e:
                if warn:
                    logger.warning("%s через %s: %s", label, tr.name, e)
                else:
                    logger.exception("%s через %s", label, tr.name)

    async def start(self) -> None:
        for tr in self._transports():
            await tr.start()
        for mod in self.modules:
            await mod.start(self)

    async def cleanup_stale_bubbles(self) -> None:
        """Убрать сообщения-баблы, осиротевшие при НЕ-graceful смерти прошлого
        процесса (краш/SIGKILL — close_all не отработал). refs персистятся в
        .live_bubbles.json; читаем, удаляем через адаптеры, чистим файл.
        Вызывать на старте ПОСЛЕ подъёма адаптеров и load_state сессий."""
        path = self.config.sessions_dir / ".live_bubbles.json"
        try:
            entries = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        adapters = {tr.name: tr for tr in self._transports()}
        removed = 0
        for e in entries:
            session = self.manager.get(str(e.get("session", "")))
            tr = adapters.get(str(e.get("adapter", "")))
            ref = e.get("ref")
            if session is None or tr is None or ref is None:
                continue
            try:
                # Уважаем DELETE_BUBBLE: журнальные баблы (delete=False) — снять
                # мёртвые кнопки, но сообщение оставить; иначе — удалить.
                await tr.bubble_finish(session, str(ref), delete=self.config.delete_bubble)
                removed += 1
            except Exception as ex:
                logger.debug("cleanup сироты бабла %s: %s", e.get("session"), ex)
        if removed:
            logger.info("Убрал осиротевших баблов с прошлого запуска: %d", removed)
        try:
            path.unlink()
        except OSError:
            pass

    async def close(self) -> None:
        for mod in self.modules:
            try:
                await mod.stop()
            except Exception:
                logger.exception("Модуль %s: ошибка остановки", getattr(mod, "name", mod))
        # Прибираем постоянные bash-оболочки, иначе они осиротеют и переживут
        # процесс. В потоке — proc.wait(timeout) блокирующий.
        await asyncio.to_thread(self.bash.close_all)
        for tr in self._transports():
            try:
                await tr.stop()
            except Exception:
                logger.exception("Адаптер %s: ошибка остановки", tr.name)

    # ── журнал событий (история веб-интерфейса) — фасад над HistoryLog ──

    def _record(self, session: Session, kind: str, **payload) -> None:
        # Стабильный API для модулей (wallet зовёт core._record) и PermissionRelay.
        self.journal.record(session.name, kind, **payload)

    def history(self, name: str) -> list[dict]:
        return self.journal.events(name)

    def save_history(self) -> None:
        self.journal.save()

    # ── доставка во все адаптеры ────────────────────────────────

    def _scrub(self, text: str) -> str:
        """Прогнать текст через редакторы модулей (вымарать значения секретов).
        No-op, если редакторов нет (кошелёк выключен). getattr — для фикстур,
        строящих core через __new__ без полного __init__."""
        for redactor in getattr(self, "output_redactors", ()):
            try:
                text = redactor(text)
            except Exception:
                logger.exception("output redactor")
        return text

    async def notice(self, session: Session | None, text: str) -> None:
        """Служебное сообщение (сторож, релей ошибок, смерть сессии…)."""
        text = self._scrub(text)
        if session is not None:
            self._record(session, "notice", text=text)
        await self._each_transport(lambda tr: tr.notify(session, text), "notify", warn=True)

    async def _typing_any(self, session: Session) -> bool:
        alive = False
        for tr in self._transports():
            try:
                alive = await tr.typing(session) or alive
            except Exception as e:
                logger.debug("typing через %s: %s", tr.name, e)
        return alive

    async def _deliver_text(
        self, session: Session, text: str, origin: Origin | None, intermediate: bool
    ) -> None:
        text = self._scrub(text)
        self._record(
            session, "intermediate" if intermediate else "reply", text=text
        )
        for tr in self._transports():
            try:
                await tr.deliver_text(
                    session, text,
                    origin=origin if origin and origin.adapter == tr.name else None,
                    intermediate=intermediate,
                )
            except Exception as e:
                logger.warning("Доставка ответа через %s: %s", tr.name, e)

    # ── context_id: происхождение сообщений ─────────────────────

    @staticmethod
    def context_id(session: Session, origin: Origin) -> str:
        return f"{origin.adapter}:{session.name}:{origin.token}"

    def _parse_context(self, context_id: str) -> tuple[Session | None, Origin | None]:
        """context_id = <адаптер>:<имя-сессии>:<токен>. Кривой — drop, а не
        «дефолт куда-нибудь»: иначе баг канала мог бы вбросить ответ не туда."""
        parts = context_id.split(":", 2)
        if len(parts) == 3:
            adapter, sname, token = parts
            session = self.manager.get(sname)
            if session is not None:
                origin = Origin(adapter, token) if adapter in self.adapters else None
                return session, origin
        logger.warning("Некорректный context_id (игнорирую): %r", context_id)
        return None, None

    # ── статусы и справочная информация ─────────────────────────

    def session_status(self, session: Session) -> str:
        """stopped | working | waiting — общий словарь статусов для адаптеров."""
        if not session.running:
            return "stopped"
        if self.bubbles.has(session.name):
            return "working"
        return "waiting"

    # ── команды: жизненный цикл сессий ──────────────────────────

    def resolve_box(self, raw: str | None) -> str | None:
        """Значение флага `--box` → движок изоляции сессии (None = дефолт .env).

        Разбор и проверка доступности здесь, а не в адаптерах: /new есть и в
        Telegram, и в вебе, а сообщение об ошибке должно быть одно и на языке
        бота. Движок, которого эта сборка дать не может, отвергаем С ПРИЧИНОЙ —
        молча свалиться в дефолт нельзя: оператор считал бы, что сессия
        изолирована иначе, чем на самом деле."""
        if raw is None:
            return None  # флага не было
        engine = Config.parse_box(raw)
        choices = ", ".join(
            "vm" if e == "agent-vm" else e for e in self.config.box_choices()
        )
        if engine is None:
            # Пустое значение (`--box` без аргумента, `--box=`) — тоже отказ:
            # флаг набран, но непонятно какой движок; молча взять дефолт значит
            # соврать оператору о том, как изолирована сессия.
            raise UserError(
                self.t("box_unknown", value=raw.strip() or "(пусто)", choices=choices)
            )
        reason = self.config.box_reject_reason(engine)
        if reason is not None:
            raise UserError(self.t("box_unavailable", box=engine, reason=reason))
        return engine

    def resolve_profile(self, raw: str | None, engine: str | None) -> str | None:
        """Значение флага `--profile` → профиль сессии.

        None = флага не было (сессия возьмёт дефолт .env), "" = ЯВНО без
        профиля (общий CLAUDE_CONFIG_DIR, даже если в .env профиль задан).
        Имя валидируем здесь, ДО создания сессии: оно идёт в path-join, и
        правила те же, что у `claude-box` (box.profiles) — профили общие.

        Под agent-vm отказываем честно: гость ИГНОРИРУЕТ хостовый
        CLAUDE_CONFIG_DIR (креды ему сеет сам agent-vm), поэтому профиль там
        не работал бы, а молча его проглотить значит соврать оператору о том,
        под какой учётной записью работает сессия.
        """
        if raw is None:
            return None  # флага не было
        name = raw.strip()
        if not name:
            return ""  # `--profile ""` — осознанный отказ от профиля
        engine = engine or self.config.sandbox
        if engine == "agent-vm":
            raise UserError(self.t("profile_vm"))
        try:
            return profiles.validate_name(name)
        except profiles.ProfileError as e:
            raise UserError(self.t("profile_bad", error=e)) from e

    async def create_session(
        self,
        title: str,
        project_path: str | None = None,
        box: str | None = None,
        profile: str | None = None,
    ) -> Session:
        """Создать сессию и привязать её ко всем адаптерам (топик и т.п.).

        Если адаптер, которому поверхность обязательна (requires_binding —
        Telegram), не смог привязаться, сессия откатывается: «сессия-призрак»
        без интерфейса заняла бы слот/порт и держала бы процесс claude, куда
        никто не может писать. Адаптеры без привязки (веб) на None не влияют.
        """
        # Пре-чек с локализованными сообщениями: manager.create бросает
        # SessionError с русским текстом (гонка-защита под локом), который при
        # BOT_LANG=en дал бы смесь языков. Здесь — на языке бота.
        slug = slugify(title)
        if self.manager.has_name(slug):
            raise UserError(self.t("name_exists", name=slug))
        if self.manager.count() >= self.config.max_instances:
            raise UserError(self.t("limit_reached", limit=self.config.max_instances))
        # Ресурсный пре-чек здесь же, рядом с лимитом: отказ на языке бота и ДО
        # создания папок/портов. manager.create проверит ещё раз (он же точка
        # входа для не-ботовых вызовов) — двойная проверка тут дешёвая.
        sandbox = self.resolve_box(box)
        # Профиль — после движка: под agent-vm он невозможен, и знать движок
        # сессии (её `--box`, а не только дефолт .env) нужно до отказа.
        prof = self.resolve_profile(profile, sandbox)
        try:
            self.manager.check_resources(sandbox)
        except SessionError as e:
            raise UserError(str(e)) from e
        try:
            session = await self.manager.create(title, project_path, sandbox, prof)
        except SessionError as e:
            raise UserError(str(e)) from e
        for tr in self._transports():
            try:
                address = await tr.bind_session(session)
            except Exception as e:
                logger.exception("Адаптер %s: bind_session(%s)", tr.name, session.name)
                if getattr(tr, "requires_binding", False):
                    await self._rollback_session(session)
                    raise UserError(self.t("bind_fail", adapter=tr.name, error=e)) from e
                address = None
            if address is None and getattr(tr, "requires_binding", False):
                await self._rollback_session(session)
                raise UserError(self.t("bind_none", adapter=tr.name))
            if address is not None:
                session.bindings[tr.name] = address
        self.manager.save_state()
        for hook in self.session_hooks:
            try:
                await hook(session)
            except Exception:
                logger.exception("session_hook для %s", session.name)
        await self._notify_state_changed(session)
        return session

    async def _rollback_session(self, session: Session) -> None:
        """Снести только что созданную сессию (привязка не удалась): гасим
        процесс, освобождаем слот/порт, убираем уже сделанные привязки."""
        bindings = dict(session.bindings)
        await self.manager.delete(session)
        for tr in self._transports():
            address = bindings.get(tr.name)
            if address is not None:
                try:
                    await tr.unbind_session(session, address)
                except Exception:
                    logger.exception("Адаптер %s: unbind при откате(%s)", tr.name, session.name)

    async def _teardown_runtime(
        self, session: Session, *, close_bash: bool = True, forget_turn: bool = False
    ) -> None:
        """Единый разбор рантайма сессии перед сменой/остановкой процесса Claude.

        Гасит ровно то, что переживает смерть процесса и ударило бы по
        следующему ходу/сессии: сторожа хода (typing/watchdog/error-relay),
        висящие permission-кнопки во всех адаптерах, bg-состояние кнопки ⏭,
        статус-бабл и (для терминальных остановок) bash-оболочки.

        Единая точка вместо дублирования в 5+ местах (close/delete/clear/
        switch_model/dead/idle), которое раньше уже разъезжалось (часть очисток
        забывалась). Per-session состояние снимается через владельцев: активность
        тула — `tools.forget`, именование сабагентов — `naming.forget`.

        close_bash=False — сессия продолжится (clear/switch_model перезапускают
        Claude в той же папке), терминалы /bash не трогаем. forget_turn=True —
        сессия удаляется совсем (turns.forget вместо stop)."""
        if forget_turn:
            self.turns.forget(session.name)
        else:
            self.turns.stop(session.name)
        await self.perms.forget(session)
        self.tools.forget(session.name)
        self.naming.forget(session.name)
        if close_bash:
            await asyncio.to_thread(self.bash.close_for_session, session.name)
            # Оболочек больше нет — липкий режим её топиков тоже снимаем,
            # иначе сессия-тёзка унаследовала бы чужое состояние.
            self.term.forget_session(session.name)
            # Историю команд — тоже: оболочки закрыты, список без них бесполезен.
            self.termhist.forget_session(session.name)
        await self.bubbles.close(session.name)

    async def _notify_state_changed(self, session: Session | None = None) -> None:
        """Сообщить адаптерам, что состав/статус сессий изменился — те, что
        показывают список (веб), обновятся. Best-effort: сбой одного адаптера
        не ломает операцию. Источник — ядро, поэтому изменение из любого
        адаптера / idle / смерти доходит до всех (раньше веб обновлял список
        только после СВОИХ REST и залипал на чужих переходах)."""
        await self._each_transport(
            lambda tr: tr.session_state_changed(session), "session_state_changed"
        )

    async def close_session(self, session: Session) -> None:
        await self._teardown_runtime(session)
        await self.manager.close(session)
        self._record(session, "status", status="stopped")
        await self._notify_state_changed(session)

    async def delete_session(self, session: Session) -> None:
        await self._teardown_runtime(session, forget_turn=True)
        bindings = dict(session.bindings)
        await self.manager.delete(session)
        self.journal.forget(session.name)
        for tr in self._transports():
            address = bindings.get(tr.name)
            if address is None:
                continue
            try:
                await tr.unbind_session(session, address)
            except Exception:
                logger.exception("Адаптер %s: unbind_session(%s)", tr.name, session.name)
        await self._notify_state_changed(session)

    async def clear_session(self, session: Session) -> None:
        """Чистый контекст: перезапуск Claude с новым UUID, привязки остаются."""
        # Гасим фоновые задачи прошлого хода (typing/watchdog/error-relay) —
        # иначе после /clear «печатает…» крутится вечно на уже пустой сессии,
        # а error-relay тайлит лог нового процесса. Сессия продолжится (тот же
        # cwd, новый UUID) — bash-терминалы не трогаем (close_bash=False).
        await self._teardown_runtime(session, close_bash=False)
        try:
            await self.manager.clear(session)
        except Exception as e:
            logger.exception("Сессия %s: ошибка /clear", session.name)
            await self.manager.close(session)
            await self._notify_state_changed(session)
            raise UserError(self.t("clear_fail", error=e)) from e
        await self._notify_state_changed(session)

    async def switch_model(self, session: Session, model: str) -> bool:
        """Сменить модель; вернуть resumed (контекст продолжен?)."""
        # Сессия продолжится (перезапуск Claude в той же папке) — bash не трогаем.
        await self._teardown_runtime(session, close_bash=False)
        try:
            resumed = await self.manager.set_model(session, model)
        except Exception as e:
            logger.exception("Сессия %s: ошибка смены модели", session.name)
            raise UserError(self.t("model_fail", model=model, error=e)) from e
        await self._notify_state_changed(session)
        return resumed

    def _systemd_unit(self) -> str:
        """Юнит текущего процесса — из cgroup (надёжно для systemd-сервиса);
        override через ORCH_SYSTEMD_UNIT, фолбэк — стандартное имя."""
        if self.config.orch_systemd_unit:
            return self.config.orch_systemd_unit
        try:
            cg = Path("/proc/self/cgroup").read_text()
            m = re.search(r"([\w@.-]+\.service)", cg)
            if m:
                return m.group(1)
        except OSError:
            pass
        return "claude-orchestrator.service"

    async def restart_service(self) -> str:
        """Перезапустить весь оркестратор через systemd (self-restart из бота).
        `--no-block`: systemctl ставит задачу и сразу выходит, чтобы мы успели
        ответить пользователю до того, как systemd остановит процесс. Возвращает
        имя юнита; UserError, если systemctl не сработал (не под systemd?)."""
        unit = self._systemd_unit()
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "restart", "--no-block", unit,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
        except OSError as e:
            raise UserError(self.t("restart_fail", error=str(e))) from e
        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip() or f"systemctl вернул {proc.returncode}"
            raise UserError(self.t("restart_fail", error=msg))
        return unit

    def web_url(self) -> str | None:
        """URL веб-интерфейса с токеном — если веб-адаптер поднят (ADAPTERS
        содержит web). None, если веб не запущен."""
        tr = self.adapters.get("web")
        return tr.public_url() if tr is not None and hasattr(tr, "public_url") else None

    def features(self) -> dict[str, bool]:
        """Что РЕАЛЬНО работает при текущей конфигурации — единый источник
        правды для всех интерфейсов (Telegram, веб, будущие).

        Правило: выключенная или неработающая фича не оставляет артефактов в
        рантайме — ни команды в меню, ни строки в справке, ни кнопки в UI, ни
        упоминания в промте модели. Артефакт = ложное обещание: оператор решит,
        что оно есть, а модель попробует этим воспользоваться и упрётся в отказ.

        Новая фича под условием = одна запись здесь (+ в COMMAND_FEATURE, если
        у неё есть команда), а не правки в каждом адаптере.
        """
        return {
            # Кошелёк подключается только под bwrap (см. Config._wallet_module).
            "wallet": "wallet" in self.config.modules,
            # Транскрипт Claude под agent-vm лежит ВНУТРИ гостя — на хосте его
            # не прочитать, статистики не будет никогда.
            "stats": self.config.sandbox != "agent-vm",
        }

    def command_available(self, command: str) -> bool:
        """Доступна ли команда при этой конфигурации (нет в карте = всегда)."""
        feature = COMMAND_FEATURE.get(command)
        return feature is None or self.features().get(feature, True)

    def help_text(self) -> str:
        """Справка без строк про недоступные команды (см. features)."""
        hidden = [
            f"<code>/{cmd}</code>"
            for cmd in COMMAND_FEATURE
            if not self.command_available(cmd)
        ]
        return "\n".join(
            ln for ln in self.t("help", boxes=self.box_choices_label()).split("\n")
            if not any(marker in ln for marker in hidden)
        )

    # ── изоляция сессии (флаг --box у /new) ─────────────────────

    @staticmethod
    def box_name(engine: str) -> str:
        """Имя движка ДЛЯ ОПЕРАТОРА: agent-vm пишем коротко «vm» — как в флаге."""
        return "vm" if engine == "agent-vm" else engine

    def box_choices(self) -> list[str]:
        """Значения `--box`, которые эта сборка примет (первое — по умолчанию).
        Единый источник для всех адаптеров: предлагать движок, которого нет,
        значит обещать отказ."""
        return [self.box_name(e) for e in self.config.box_choices()]

    def box_choices_label(self) -> str:
        return ", ".join(self.box_choices())

    def box_note(self, session: Session) -> str:
        """Приписка о нестандартной изоляции сессии (пусто, если движок дефолтный).

        Прозрачность: сессия без песочницы работает с правами оператора — это
        должно быть видно и при создании, и в /list, а не только в .sessions.json."""
        engine = self.manager.engine_of(session)
        if engine == self.config.sandbox:
            return ""
        note = self.t(
            "box_created",
            box=self.box_name(engine),
            default=self.box_name(self.config.sandbox),
        )
        # Кошелёк работает только под bwrap. В сессии с другим движком он не
        # просто «выключен»: модель там ходит по хосту с правами оператора и
        # читает и сам файл секретов, и токены соседних сессий. Оператор вправе
        # так решить (для того и флаг), но узнать об этом должен СРАЗУ.
        if "wallet" in self.config.modules and engine != "bwrap":
            note += self.t("box_created_no_wallet")
        return note

    def profile_note(self, session: Session) -> str:
        """Приписка об учётке сессии (пусто, если она как в .env).

        Прозрачность та же, что у box_note: под каким профилем работает сессия
        — видно сразу при создании, а не только в .sessions.json. Явный отказ
        от профиля (`--profile ""`) при заданном CLAUDE_PROFILE тоже называем:
        такая сессия ходит под общей учёткой оркестратора."""
        profile = self.manager.profile_of(session)
        if profile == self.config.claude_profile:
            return ""
        if profile is None:
            return self.t("profile_created_none")
        return self.t("profile_created", profile=profile)

    def profile_mark(self, session: Session) -> str:
        """Короткая пометка профиля для списка сессий (пусто, если дефолтный)."""
        profile = self.manager.profile_of(session)
        if profile == self.config.claude_profile or profile is None:
            return ""
        return self.t("profile_mark", profile=profile)

    def box_mark(self, session: Session) -> str:
        """Короткая пометка движка для списка сессий (пусто, если дефолтный)."""
        engine = self.manager.engine_of(session)
        if engine == self.config.sandbox:
            return ""
        return self.t("box_mark", box=self.box_name(engine))

    def wallet_command(self, args_str: str) -> str:
        """`/wallet …` — просмотр/правка policy кошелька. Ядро находит модуль
        `wallet` по имени (как прочие команды; полный реестр команд модулей —
        отложенный §3-рефактор). UserError, если кошелёк не подключён."""
        mod = next((m for m in self.modules if getattr(m, "name", "") == "wallet"), None)
        if mod is None:
            raise UserError(self.t("wallet_disabled"))
        return mod.handle_command(args_str)

    async def ensure_running(self, session: Session) -> str:
        """Возобновить остановленную сессию. Возвращает running|resumed|fresh."""
        if session.running:
            return "running"
        try:
            resumed = await self.manager.resume(session)
        except SessionError as e:
            raise UserError(self.t("resume_fail", error=e)) from e
        await self._notify_state_changed(session)
        return "resumed" if resumed else "fresh"

    # ── сообщения пользователя ──────────────────────────────────

    async def user_message(self, session: Session, text: str, origin: Origin) -> None:
        """Переслать сообщение пользователя в Claude (сессия уже запущена).

        Если Клод ещё не ответил на предыдущее, а пользователь уже шлёт
        следующее — старый бабл замораживается на месте, новый открывается
        независимо (см. bubble.freeze_and_open).
        """
        await self.bubbles.freeze_and_open(session.name)
        try:
            try:
                await self.manager.send_to_claude(
                    session, text, self.context_id(session, origin)
                )
            except aiohttp.ClientConnectorError:
                # Канал сессии не отвечает, хотя процесс числится живым (типовой
                # случай: claude жив, а его channel-сервер умер/не поднялся —
                # например после рестарта оркестратора). Раньше это всплывало
                # оператору сырым «Cannot connect to host 127.0.0.1:PORT», хотя
                # чинится тривиально — переподнять сессию. Делаем это САМИ и
                # ровно один раз: молча пересобираем сессию и повторяем отправку;
                # не помогло — отдаём честную ошибку прежним путём.
                logger.warning(
                    "Сессия %s: канал не отвечает — переподнимаю и повторяю отправку",
                    session.name)
                await self.manager.revive(session)
                await self._notify_state_changed(session)
                await self.manager.send_to_claude(
                    session, text, self.context_id(session, origin)
                )
        except Exception as e:
            logger.error("Сессия %s: не удалось передать сообщение: %s", session.name, e)
            # Бабл уже открыт (freeze_and_open) — закрываем, иначе сессия
            # навсегда числится «working» (session_status по bubbles.has),
            # а _active копит поздние хук-события в бабл, который никто не
            # закроет.
            await self.bubbles.close(session.name)
            raise UserError(self.t("forward_fail", error=e)) from e
        self._record(session, "user", text=text, via=origin.adapter)
        self.tools.forget(session.name)  # новый ход — сброс bg-состояния
        snippet = html.escape(shorten(text, 28))
        await self.bubbles.append(session.name, f"📨 {snippet}")
        self.turns.start(session.name)

    async def request_report(self, session: Session, origin: Origin) -> None:
        """Запрос статус-отчёта (кнопка 📋): push-сообщение модели «отчитайся и
        продолжай» (не останавливаться). Модель прочитает, когда доберётся.
        Настоящее прерывание хода — hard_stop (Esc в PTY, кнопка ⛔)."""
        await self.manager.send_to_claude(
            session, self.t("stop_message"), self.context_id(session, origin)
        )
        await self.bubbles.append(session.name, self.t("bubble_stop_requested"))

    async def hard_stop(self, session: Session) -> None:
        """Жёсткое прерывание хода: Esc в PTY (см. sessions.interrupt_turn).

        Ход обрывается сразу, финального ответа не будет — гасим сторожей и
        бабл здесь же, иначе индикаторы висели бы до таймаута.
        """
        try:
            self.manager.interrupt_turn(session)
        except SessionError as e:
            raise UserError(str(e)) from e
        self.turns.stop(session.name)
        await self.bubbles.close(session.name)
        self._record(session, "status", status="interrupted")
        await self.notice(session, self.t("esc_done"))

    def unblock_action(self, name: str) -> str | None:
        """Что сделает кнопка ⏭ сейчас: "kick" (Esc — прервать ожидание фона),
        "background" (Ctrl+B — свернуть идущую задачу) или None (нечего). Логика —
        в ToolActivity; здесь только гейт «сессия существует»."""
        if self.manager.get(name) is None:
            return None
        return self.tools.unblock_action(name)

    async def unblock(self, session: Session) -> None:
        """Разблокировать ввод модели, НЕ прерывая ход насмерть (для этого —
        hard_stop). Контекстно (см. unblock_action):
          * модель ждёт фоновую задачу (TaskOutput) → Esc прерывает именно
            ожидание, модель принимает новый ввод;
          * идёт долгая foreground-команда → Ctrl+B сворачивает её в фон.
        Оба случая освобождают ввод. Бабл/сторожей не гасим — ход живой."""
        action = self.unblock_action(session.name)
        try:
            if action == "kick":
                self.manager.interrupt_turn(session)  # Esc — пинок ожидания
                await self.bubbles.append(session.name, self.t("bubble_kicked"))
            else:
                # background или "нечего" — Ctrl+B безвреден как no-op.
                self.manager.background_turn(session)  # Ctrl+B — в фон
                await self.bubbles.append(session.name, self.t("bubble_backgrounded"))
        except SessionError as e:
            raise UserError(str(e)) from e

    async def slash_command(self, session: Session, cmd: str) -> None:
        """Неизвестные /команды — прямо в терминал Claude (команды Claude Code)."""
        try:
            self.manager.type_into_pty(session, cmd)
        except SessionError as e:
            raise UserError(self.t("send_fail", error=e)) from e

    async def compact(self, session: Session) -> None:
        try:
            self.manager.type_into_pty(session, "/compact")
        except SessionError as e:
            raise UserError(self.t("send_fail", error=e)) from e

    # ── ответы Claude (вызывается reply_server'ом) ──────────────

    async def handle_reply(self, data: dict) -> None:
        """Текстовый ответ или файл от Claude (тул reply_to_user)."""
        session, origin = self._parse_context(str(data.get("context_id", "")))
        if session is None:
            return
        self.manager.touch(session)  # ответ/файл = активность (таймер простоя)

        if data.get("file_path"):
            # Файл — ПРОМЕЖУТОЧНОЕ действие (send_file_to_user), ход продолжается
            # (см. handle_tool_event: файл не считается «текстовым» ответом).
            # НЕ глушим сторожей хода (typing/watchdog/error-relay) — иначе модель
            # шлёт файл в середине длинного хода и зависание после этого не
            # заметят. Ход завершат reply(complete=true) или Stop-хук.
            await self._send_file(
                session, str(data["file_path"]), str(data.get("caption", "")), origin
            )
            return

        text = str(data.get("text", ""))
        complete = bool(data.get("complete", False))
        logger.info(
            "reply сессия=%s complete=%s len=%d", session.name, complete, len(text)
        )

        if not complete:
            # Промежуточный ответ Клода — отдельным полным сообщением (💬),
            # а не обрезанной строчкой в бабле: важно видеть, что он пишет,
            # пока работает дальше.
            if text:
                await self._deliver_text(session, text, origin, intermediate=True)
                # Бабл, если он был, «застрял» бы ВЫШЕ этого 💬 (у него свой
                # message_id). Замораживаем его на месте и открываем новый —
                # дальнейшие события хода пойдут ПОД ответом модели, как бабл
                # шёл под сообщением пользователя (freeze_and_open, линейная
                # история без прыжков). Только если бабл активен.
                if self.bubbles.has(session.name):
                    await self.bubbles.freeze_and_open(session.name)
            return

        # Финал (даже с пустым текстом): гасим typing и бабл, чтобы индикатор
        # не крутился вечно. Сначала сообщение, потом чистка бабла — окна
        # «ответа ещё нет» не остаётся.
        self.turns.stop(session.name)
        self.tools.forget(session.name)  # ход завершён — сброс bg-состояния
        if text:
            await self._deliver_text(session, text, origin, intermediate=False)
        await self.bubbles.close(session.name)

    def _sendfile_roots(self, session: Session) -> list[Path]:
        """Рабочие папки, откуда Клоду разрешено отправлять файлы в чат:
        cwd проекта (или папка сессии), сама папка сессии и incoming-каталог.
        """
        return [
            self.manager.effective_cwd(session),
            session.session_dir,
            self.incoming_dir(session),
        ]

    def incoming_dir(self, session: Session) -> Path:
        """Каталог для присланных файлов: INCOMING_DIR относительный — внутри
        папки сессии, абсолютный — общий. Единый источник правды для обоих
        адаптеров и jail'а send_file (иначе куда кладут ≠ что отдают)."""
        inc = Path(self.config.incoming_dir).expanduser()
        if not inc.is_absolute():
            inc = session.session_dir / inc
        return inc

    def path_in_workspace(self, path: Path, session: Session) -> bool:
        """Лежит ли path (после resolve, со симлинками) внутри одной из рабочих
        папок сессии. Ошибки resolve/stat (битая симссылка, нет прав) → False:
        лучше отказать, чем вынести файл за пределами workspace."""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for root in self._sendfile_roots(session):
            try:
                if resolved.is_relative_to(root.resolve()):
                    return True
            except (OSError, ValueError):
                continue
        return False

    async def send_log(self, session: Session, origin: Origin | None = None) -> None:
        """Отправить полный claude.log сессии файлом — для отладки (формат Claude
        Code сменился, парсер молчит и т.п.). Лог в session_dir (внутри jail),
        доставляется документом во все адаптеры; если файла нет — notice."""
        log = session.session_dir / "claude.log"
        await self._send_file(
            session, str(log), self.t("log_caption", name=session.title), origin
        )

    async def _send_file(
        self, session: Session, file_path: str, caption: str, origin: Origin | None
    ) -> None:
        path = Path(file_path).expanduser()
        # Jail: только внутри рабочих папок сессии. Без этого промпт-инъекция
        # из чужого файла/CLAUDE.md могла бы заставить Клода вызвать
        # send_file_to_user на ~/.ssh/id_rsa и выслать секреты в чат.
        # resolve() раскрывает симлинки — escape через ссылку тоже отсекается.
        if not self.path_in_workspace(path, session):
            logger.warning("send_file отклонён вне workspace: %s", path)
            await self.notice(session, self.t("sendfile_denied", path=path))
            return
        if not path.is_file():
            await self.notice(session, self.t("sendfile_not_found", path=path))
            return
        self._record(session, "file", path=str(path), caption=caption)
        for tr in self._transports():
            try:
                await tr.deliver_file(
                    session, path, caption,
                    origin=origin if origin and origin.adapter == tr.name else None,
                )
            except Exception as e:
                logger.warning("Доставка файла через %s: %s", tr.name, e)

    # ── события хуков Claude Code ───────────────────────────────

    async def handle_tool_event(self, session_name: str, payload: dict) -> None:
        """Хуки Claude Code (PreToolUse/PostToolUse/SubagentStop) → статус-бабл.
        Роут по hook_event_name; PreToolUse — вызов инструмента (строка)."""
        session = self.manager.get(session_name)
        if session is None:
            return
        event = str(payload.get("hook_event_name") or "")
        if event == "PostToolUse":
            await self._handle_post_tool(session, payload)
            return
        if event == "SubagentStop":
            await self._handle_subagent_stop(session, payload)
            return
        tool = str(payload.get("tool_name") or "?")
        # Наш канальный тул — mcp__channel-<slug>__reply_to_user; сверяем по
        # ХВОСТУ имени, а не подстрокой: `"reply_to_user" in tool` ложно
        # срабатывало бы на чужом MCP-туле (mcp__notes__draft_reply_to_user),
        # ставя reply-флаг и глуша Stop-фолбэк «потерянного финала».
        is_reply = tool.endswith("__reply_to_user")
        is_file = tool.endswith("__send_file_to_user")
        # reply_to_user — единственное действие, после которого «до Stop больше
        # ничего не потерять» истинно; ЛЮБОЙ другой тул (в т.ч. send_file_to_user)
        # сбрасывает флаг — см. turn.TurnSupervisor.
        self.turns.note_tool(session.name, is_reply)
        if is_reply:
            return  # результат и так придёт сообщением — в бабле это шум
        if is_file:
            return  # тоже придёт сообщением; не считается «текстовым» ответом
        # Запоминаем последний значимый тул: TaskOutput = модель ждёт фоновую
        # задачу (заглушка-ожидание в TUI), в этом состоянии кнопка ⏬ неактивна
        # (сворачивать нечего — уже в фоне), а ⛔ прерывает именно ожидание.
        self.tools.note_tool(session.name, tool)
        tool_input = payload.get("tool_input") or {}
        if tool == "TaskOutput":
            # Явно показываем «ждёт фон» — иначе для пользователя это выглядит
            # как молчание (эксперимент подтвердил: Bash run_in_background →
            # TaskOutput). Строка не схлопывается (tool=None).
            await self.bubbles.append(session.name, self.t("bubble_waiting_bg"))
            return
        # Реальный тул стартовал (не reply/file/TaskOutput) — помечаем как in-flight
        # до его PostToolUse (кнопка ⏭ активна, вотчдог видит сессию живой).
        self.tools.start(session.name, str(payload.get("tool_use_id") or ""))
        # agent_id/agent_type — на каждом тул-вызове ВНУТРИ сабагента.
        agent_id = payload.get("agent_id")
        # Сабагент уже завершился (SubagentStop пришёл), а этот его тул-хук
        # прилетел позже из-за гонки доставки async-хуков — не нестим строку под
        # «завершил» (иначе визуально «сабагент закончил, но работа идёт»).
        # Рендерим верхним уровнем: agent_id → None (и note_child не тронем).
        if agent_id and self.naming.is_closed(session.name, str(agent_id)):
            agent_id = None
        # Запоминаем тип сабагента для будущей строки «✅ … завершил»:
        #  • дочерний тул несёт agent_id + agent_type — самый надёжный источник;
        #  • спавн (Agent/Task) несёт subagent_type, но agent_id ещё нет —
        #    копим по порядку как фолбэк (сматчим при SubagentStop по очереди).
        if agent_id and (atype := payload.get("agent_type")):
            self.naming.note_child(session.name, str(agent_id), str(atype))
        elif tool in AGENT_SPAWN_TOOLS and (st := (tool_input.get("subagent_type"))):
            self.naming.note_spawn(session.name, str(st))
        # Спавн сабагента (описание всегда разное) и TodoWrite (состояние
        # тудушки) не схлопываем; остальные — по (tool, agent_id).
        collapsible = tool not in AGENT_SPAWN_TOOLS and tool != "TodoWrite"
        await self.bubbles.append(
            session.name,
            # Вымарываем значения секретов: команда-инпут (напр. `echo <shared>`
            # или запись ключа в .env) не должна светить значение в Telegram.
            self._scrub(tool_line(tool, tool_input, self.t)),
            agent_id=str(agent_id) if agent_id else None,
            tool=tool if collapsible else None,
            full_html=self._scrub(tool_line_full(tool, tool_input)),
            tool_use_id=str(payload.get("tool_use_id") or ""),  # для PostToolUse
        )

    async def _handle_post_tool(self, session: Session, payload: dict) -> None:
        """PostToolUse: вызов завершился — сворачиваем «текущую» строку и
        приписываем итог. exit_code в payload НЕТ (проверено на 2.1.215), поэтому
        статус по tool_response: ✗ — прервано, ⚠ — есть stderr, иначе ✓; + время."""
        # Тул завершился — снимаем его из in-flight (для ЛЮБОГО тула, не только
        # Bash: иначе кнопка ⏭ осталась бы активной до конца хода).
        self.tools.finish(session.name, str(payload.get("tool_use_id") or ""))
        if str(payload.get("tool_name") or "") != "Bash":
            return  # подробный вид/итог пока только для Bash
        resp = payload.get("tool_response") or {}
        if payload.get("interrupted") or (isinstance(resp, dict) and resp.get("interrupted")):
            mark = "✗"
        elif isinstance(resp, dict) and str(resp.get("stderr") or "").strip():
            mark = "⚠"
        else:
            mark = "✓"
        dur = payload.get("duration_ms")
        status = f"{mark} · {int(dur)}мс" if isinstance(dur, (int, float)) else mark
        await self.bubbles.complete(
            session.name, str(payload.get("tool_use_id") or ""), status
        )

    async def _handle_subagent_stop(self, session: Session, payload: dict) -> None:
        """SubagentStop: сабагент завершился — ИМЕНОВАННОЙ строкой в бабл (с
        отступом под его agent_id).

        Имя (dev-planner/…) снимаем из SubagentNaming (точный матч по agent_id →
        фолбэк по порядку спавнов). Модель в payload НЕТ — читаем из транскрипта
        сабагента (agent_transcript_path, а если его нет/не прочёлся — собираем
        путь `<session-transcript-dir>/<uuid>/subagents/agent-<id>.jsonl` сами)."""
        agent_id = str(payload.get("agent_id") or "")
        agent = self.naming.pop(session.name, agent_id)
        # Сабагент закрыт: его запоздалые тул-хуки (гонка доставки) больше не
        # нестить под ним (см. handle_tool_event / naming.is_closed).
        self.naming.close(session.name, agent_id)
        model = await self._read_subagent_model(session, agent_id, payload)
        if agent and model:
            line = self.t("subagent_done_named", agent=agent, model=model)
        elif agent:
            line = self.t("subagent_done_named_nomodel", agent=agent)
        elif model:
            line = self.t("subagent_done", model=model)
        else:
            line = self.t("subagent_done_nomodel")
        await self.bubbles.append(
            session.name, line, agent_id=agent_id or None,
        )

    async def _read_subagent_model(
        self, session: Session, agent_id: str, payload: dict
    ) -> str:
        """Модель сабагента: сперва по agent_transcript_path из payload, затем
        собранный путь subagents/agent-<id>.jsonl, затем фолбэк — новейший
        agent-*.jsonl в subagents/. '' — не удалось.

        Фолбэк на новейший нужен, когда в payload НЕТ agent_transcript_path И
        agent_id пуст/не совпал (тогда имя резолвится по спавну, а модель
        терялась → строка «завершил» без модели). Сабагент только что завершил
        — его транскрипт дописан последним, поэтому обычно он и есть новейший."""
        candidates: list[Path] = []
        tpath = payload.get("agent_transcript_path")
        if tpath:
            candidates.append(Path(tpath))
        subdir: Path | None = None
        if agent_id:
            sess_tr = self.manager.transcript_path(session)
            # subagents/ лежит в подпапке-uuid сессии рядом с её .jsonl.
            subdir = sess_tr.with_suffix("") / "subagents"
            candidates.append(subdir / f"agent-{agent_id}.jsonl")
        else:
            subdir = self.manager.transcript_path(session).with_suffix("") / "subagents"
        newest = await asyncio.to_thread(self._newest_subagent_transcript, subdir)
        if newest is not None and newest not in candidates:
            candidates.append(newest)
        for path in candidates:
            try:
                model = await asyncio.to_thread(read_last_model, path)
            except Exception:
                model = None
            if model:
                return model
        return ""

    @staticmethod
    def _newest_subagent_transcript(subdir: "Path | None") -> "Path | None":
        """Новейший (по mtime) agent-*.jsonl в subagents/ или None. Блокирующий
        stat — звать через to_thread. Тихо переносит отсутствие каталога."""
        if subdir is None:
            return None
        try:
            files = [p for p in subdir.glob("agent-*.jsonl") if p.is_file()]
        except OSError:
            return None
        if not files:
            return None
        return max(files, key=lambda p: p.stat().st_mtime)

    def _unblock_available(self, name: str) -> bool:
        """Есть ли что разблокировать (для активности кнопки ⏭): ждёт фон
        (Esc) или идёт команда (Ctrl+B). В покое — нечего."""
        return self.unblock_action(name) is not None

    async def handle_stop_event(self, session_name: str, payload: dict) -> None:
        """Stop-хук — конец хода Claude Code.

        Фолбэк против «потерянного финала»: если последним действием модели
        перед этим Stop был НЕ reply_to_user, а last_assistant_message непустой —
        этот текст канал не ретранслировал и он не долетел до пользователя.
        Шлём его сами.

        Не закрывает бабл/typing: Stop не означает «ход окончательно завершён» —
        часто это пауза перед автопродолжением (ждём CI, фоновый шелл).
        """
        session = self.manager.get(session_name)
        if session is None:
            return
        # Прозрачность фоновых процессов: снимок из payload (авторитетно от
        # харнесса) + уведомление о ВНОВЬ появившихся задачах. Делаем ДО фолбэка
        # текста — снимок обновляем на каждом Stop независимо от reply-флага.
        await self._update_background(session, payload)
        if self.turns.pop_reply_flag(session.name):
            return
        text = str(payload.get("last_assistant_message") or "").strip()
        if not text:
            return
        await self._deliver_text(session, text, None, intermediate=False)

    async def _update_background(self, session: Session, payload: dict) -> None:
        """Обновить снимок фоновых задач/кронов сессии из Stop-payload и уведомить
        оператора о НОВЫХ задачах (id, о которых ещё не говорили) — без спама на
        каждый ход. `/bg` показывает полный текущий список по запросу."""
        tasks = payload.get("background_tasks")
        crons = payload.get("session_crons")
        session.background_tasks = tasks if isinstance(tasks, list) else []
        session.session_crons = crons if isinstance(crons, list) else []
        fresh = [
            t for t in session.background_tasks
            if isinstance(t, dict) and t.get("id") is not None
            and str(t["id"]) not in session.bg_seen
        ]
        for t in session.background_tasks:
            if isinstance(t, dict) and t.get("id") is not None:
                session.bg_seen.add(str(t["id"]))
        if fresh:
            items = "; ".join(self._bg_task_brief(t) for t in fresh)
            await self.notice(session, self.t("bg_new", items=items))

    @staticmethod
    def _bg_task_brief(task: dict) -> str:
        """Короткое описание фоновой задачи для уведомления (HTML-контекст notify):
        тип · команда (статус). Команда произвольная — экранируем."""
        kind = html.escape(str(task.get("type") or "task"))
        status = html.escape(str(task.get("status") or ""))
        detail = " ".join(str(task.get("command") or task.get("description") or "").split())
        if len(detail) > 80:
            detail = detail[:80] + "…"
        detail = html.escape(detail)
        head = f"{kind} · {detail}" if detail else kind
        return f"{head} ({status})" if status else head

    def bg_text(self, session: Session) -> str:
        """Текст `/bg`: фоновые задачи и кроны сессии из последнего снимка."""
        tasks = [t for t in session.background_tasks if isinstance(t, dict)]
        crons = [c for c in session.session_crons if isinstance(c, dict)]
        if not tasks and not crons:
            return self.t("bg_empty")
        lines = [self.t("bg_header", title=html.escape(session.title))]
        lines.append(self.t("bg_tasks_n", n=len(tasks)) if tasks else self.t("bg_no_tasks"))
        for task in tasks:
            tid = html.escape(str(task.get("id") or "?"))
            kind = html.escape(str(task.get("type") or "task"))
            status = html.escape(str(task.get("status") or ""))
            desc = html.escape(" ".join(str(task.get("description") or "").split()))
            cmd = html.escape(" ".join(str(task.get("command") or "").split())[:200])
            lines.append(f" • [{tid}] {kind} · {status}")
            if desc:
                lines.append(f"   {desc}")
            if cmd:
                lines.append(f"   <code>{cmd}</code>")
        lines.append(self.t("bg_crons_n", n=len(crons)) if crons else self.t("bg_no_crons"))
        for cron in crons:
            sched = html.escape(str(cron.get("schedule") or cron.get("cron") or ""))
            raw_desc = str(cron.get("description") or cron.get("prompt") or "")
            desc = html.escape(" ".join(raw_desc.split())[:120])
            lines.append(f" • {sched} {desc}".rstrip())
        return "\n".join(lines)

    # ── permission relay ────────────────────────────────────────

    async def handle_permission_request(self, session_name: str, payload: dict) -> None:
        """Запрос разрешения ОТ Claude Code (зовётся reply_server'ом) → PermissionRelay.

        Если у сессии стоит auto_allow (кнопка «✅ Всё» / /perms auto) — одобряем
        сразу, без кнопок в чат: оператор уже сказал «мне пофиг, разрешай всё»
        (живой случай: модель лезет в SSH, и каждый чих спрашивать — мука)."""
        session = self.manager.get(session_name)
        if session is None:
            return
        request_id = str(payload.get("request_id", ""))
        if session.auto_allow:
            try:
                await self.manager.send_permission(session, request_id, "allow")
            except Exception as e:
                logger.error("Сессия %s: авто-одобрение не ушло: %s", session.name, e)
            return
        await self.perms.request_from_claude(
            session, payload, always_label=self.t("perm_allow_all"))

    async def request_confirmation(
        self,
        session: Session,
        tool: str,
        description: str,
        preview: str,
        timeout: float = 300.0,
    ) -> bool:
        """Локальное подтверждение для модулей (wallet) → PermissionRelay."""
        return await self.perms.request_confirmation(
            session, tool, description, preview, timeout
        )

    async def request_choice(
        self,
        session: Session,
        tool: str,
        description: str,
        preview: str,
        timeout: float = 300.0,
        always_label: str | None = None,
    ) -> str:
        """Локальное подтверждение с третьим исходом «разрешить навсегда» (§4.6
        ASK-грант кошелька) → PermissionRelay. Возвращает "deny"|"allow"|
        "allow_always"; без `always_label` третьей кнопки нет вовсе."""
        return await self.perms.request_choice(
            session, tool, description, preview, timeout, always_label
        )

    async def permission_verdict(
        self, session: Session, request_id: str, behavior: str, via: str
    ) -> bool:
        """Вердикт из адаптера → PermissionRelay."""
        return await self.perms.verdict(session, request_id, behavior, via)

    # ── статистика и справки ────────────────────────────────────

    def model_display(self, session: Session, stats: dict | None = None) -> str:
        """Имя модели для показа: реальная из транскрипта → установленная
        (алиас вроде opus) → «по умолчанию Claude Code».

        Без транскрипта (stats=None) читает его — блокирующее чтение, дёргать
        через asyncio.to_thread. Если stats уже есть — лишнего I/O нет.
        """
        model = (stats or self.manager.read_stats(session) or {}).get("model", "")
        return model or session.model or self.t("default_model")

    def stats_text(self, session: Session) -> str:
        """Блокирующее чтение транскрипта — вызывать через asyncio.to_thread."""
        stats = self.manager.read_stats(session)
        uptime = self.fmt_duration(time.time() - session.started_at)
        header = f"📊 {session.title}" + (
            "" if session.running else self.t("stats_stopped_suffix")
        )
        if stats is None:
            # Под agent-vm транскрипт живёт в госте, а мы читаем его на хосте —
            # он не появится НИКОГДА. Не тянем оператора ждать «ещё не создан»,
            # а честно называем ограничение режима.
            key = (
                "stats_no_transcript_vm"
                if self.manager.engine_of(session) == "agent-vm"
                else "stats_no_transcript"
            )
            return self.t(key, header=header, uptime=uptime)
        if stats.get("stale_schema"):
            # Транскрипт есть и валиден, но ни одного ожидаемого поля не
            # извлекли — вероятно, поменялась схема Claude Code. Показываем хвост
            # claude.log и зовём скачать полный лог (/log) для разработчиков.
            tail = self.manager.tail_log(session, 12)
            return self.t(
                "stats_stale_schema", header=header, uptime=uptime,
                tail=tail or self.t("log_empty"),
            )
        ctx = stats["context_tokens"]
        window = self.config.context_window
        engine = self.manager.engine_of(session)
        wallet = "wallet" in self.config.modules
        return self.t(
            "stats_body",
            header=header,
            model=self.model_display(session, stats),
            ctx=self.fmt_num(ctx),
            pct=f"{ctx / window * 100:.0f}",
            window=self.fmt_num(window),
            out=self.fmt_num(stats["output_tokens"]),
            turns=stats["turns"],
            kb=f"{stats['transcript_bytes'] / 1024:.0f}",
            sandbox=self.box_name(engine),
            wallet=self.t("stats_wallet_on" if wallet and engine == "bwrap"
                          else "stats_wallet_off"),
            perms_mode=self.config.permission_mode,
            perms_auto=(" · " + self.t("perms_auto_yes")) if session.auto_allow else "",
            uptime=uptime,
        )

    async def usage_text(self, session: Session) -> str | None:
        """Расходы и лимиты плана: прогнать /cost в терминале Claude и разобрать.
        None — распарсить не удалось. Требует запущенной сессии (иначе
        type_into_pty бросит SessionError → UserError для адаптера)."""
        try:
            delta = await self.manager.run_and_capture(session, "/cost")
        except SessionError as e:
            raise UserError(self.t("send_fail", error=e)) from e
        data = parse_cost(delta)
        if not data:
            return None
        lines = [self.t("usage_title", name=session.title)]
        if "cost" in data:
            lines.append(self.t("usage_cost", cost=data["cost"]))
        if "session_pct" in data:
            reset = data.get("session_reset", "")
            lines.append(self.t("usage_session", pct=data["session_pct"],
                                reset=f" · {reset}" if reset else ""))
        if "week_pct" in data:
            reset = data.get("week_reset", "")
            lines.append(self.t("usage_week", pct=data["week_pct"],
                                reset=f" · {reset}" if reset else ""))
        for name, pct in data.get("models", []):
            lines.append(self.t("usage_model", model=name, pct=pct))
        return "\n".join(lines)

    def collect_skills(self, session: Session | None = None) -> list[tuple[str, str]]:
        """Скиллы профиля Claude Code (глобальные + плагины). Блокирующее I/O.

        session задана — берём скиллы ЕЁ учётки: под своим профилем сессия
        видит другой набор, и показывать ей общий значило бы обещать скиллы,
        которых у неё нет. Без сессии (главный чат) — учётка по умолчанию.
        """
        config_dir = (
            self.manager.config_dir_of(session) or Path.home() / ".claude"
        )
        skill_files: list[Path] = []
        skill_files += sorted((config_dir / "skills").glob("*/SKILL.md"))
        plugins = config_dir / "plugins"
        if plugins.is_dir():
            skill_files += sorted(plugins.glob("**/skills/*/SKILL.md"))
        result, seen = [], set()
        for path in skill_files:
            name, desc = path.parent.name, ""
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            for line in text.splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    desc = " ".join(line.split(":", 1)[1].split())[:120]
            if name not in seen:
                seen.add(name)
                result.append((name, desc))
        return result

    def ls_text(self, arg: str | None, session: "Session | None" = None) -> str:
        """Листинг файлов для /ls. Базовый каталог — то же разделение, что у
        /bash: в сессии папка проекта (effective_cwd), в главном чате — дом
        пользователя на хосте. Относительный аргумент (`./`, `sub/dir`)
        резолвится ОТ этой базы, а не от cwd процесса-оркестратора; абсолютный
        путь и `~` — как есть."""
        base = (
            self.manager.effective_cwd(session)
            if session is not None
            else Path.home()
        )
        if arg and arg.strip():
            p = Path(arg.strip()).expanduser()
            target = (p if p.is_absolute() else base / p).resolve()
        else:
            target = base
        if not target.exists():
            return self.t("ls_not_exists", path=target)
        if not target.is_dir():
            return self.t("ls_file", path=target)
        try:
            entries = sorted(
                target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())
            )
        except OSError as e:
            return self.t("ls_no_access", error=e)
        lines = [f"📁 {target}"]
        if not entries:
            lines.append(self.t("ls_empty"))
        for entry in entries[:30]:
            icon = "📁" if entry.is_dir() else "📄"
            lines.append(f"{icon} {entry.name}{'/' if entry.is_dir() else ''}")
        if len(entries) > 30:
            lines.append(self.t("ls_more", n=len(entries) - 30))
        return "\n".join(lines)

    @staticmethod
    def parse_new_args(raw: str) -> tuple[str, str | None, str | None, str | None]:
        """Аргументы «новой сессии» → (имя, путь, значение --box, значение --profile).

        Поддерживает: имя с пробелами, обрамляющие кавычки, форму `/path`,
        форму `имя /path` (путь = токен, начинающийся с / или ~), и флаги
        `--box off` / `--profile work` (в форме `--флаг=значение` тоже) в ЛЮБОМ
        месте строки — они вырезаются до разбора имени/пути, поэтому имя с
        пробелами не ломается. Значения флагов здесь не проверяются: этим
        занимаются resolve_box/resolve_profile.
        """
        raw = raw.strip()
        quoted = len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]
        # Кавычки вокруг ВСЕЙ строки — просьба взять её как имя буквально:
        # «--box» внутри них принадлежит имени, а не нам.
        box = profile = None
        if not quoted:
            raw, box = OrchestratorCore._cut_flag(raw, "box")
            raw, profile = OrchestratorCore._cut_flag(raw, "profile")
            quoted = len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]
        if quoted:
            raw = raw[1:-1].strip()
        if not raw:
            return "", None, box, profile

        def is_path(tok: str) -> bool:
            return tok.startswith("/") or tok.startswith("~")

        if is_path(raw):
            return Path(raw).name, raw, box, profile
        tokens = raw.split()
        path_idx = next((i for i, tok in enumerate(tokens) if is_path(tok)), None)
        if path_idx is not None:
            project_path = " ".join(tokens[path_idx:])
            title = " ".join(tokens[:path_idx]) or Path(project_path).name
            return title, project_path, box, profile
        return raw, None, box, profile

    @staticmethod
    def _cut_flag(raw: str, flag: str) -> tuple[str, str | None]:
        """Вырезать `--<flag> <значение>` / `--<flag>=<значение>` из аргументов.

        Возвращает (строка без флага, значение-или-None). Значение НЕ проверяем
        (это делают resolve_box/resolve_profile) — здесь только разбор строки.
        Флаг без значения даёт пустую строку: для `--box` это понятная ошибка от
        resolve_box, для `--profile` — осмысленное «явно без профиля».

        Кавычки вокруг значения снимаем: `--profile ""` — единственный способ
        сказать «без профиля», и оператор пишет его именно так.
        """
        prefix = f"--{flag}"
        tokens = raw.split()
        out: list[str] = []
        value: str | None = None
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            low = tok.lower()
            if low == prefix:
                value = tokens[i + 1] if i + 1 < len(tokens) else ""
                i += 2
                continue
            if low.startswith(f"{prefix}="):
                value = tok.split("=", 1)[1]
                i += 1
                continue
            out.append(tok)
            i += 1
        if value is not None and len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        return (" ".join(out) if value is not None else raw), value


    # ── /bash: постоянный терминал мимо Claude ──────────────────

    @staticmethod
    def bash_key(session: Session | None, scope: str) -> str:
        """Ключ bash-оболочки: у сессии — общий across-адаптеры не нужен,
        адаптер даёт свой scope (телеграм — топик, веб — вкладка)."""
        return f"s:{session.name}:{scope}" if session is not None else f"main:{scope}"

    def bash_cwd(self, session: Session | None) -> Path:
        """Стартовый cwd терминала — то же разделение, что у /ls и /new:
          * в сессии → папка проекта (та же файловая песочница, что у claude);
          * в главном чате → дом пользователя на хосте. Main-chat /bash бежит
            ВНЕ песочницы (операторский терминал, полный скоуп — см. run_bash),
            поэтому прежний нейтральный `.bash-main` не нужен: изоляции нет,
            оператор и так с полным доступом к хосту.
        """
        if session is not None:
            return self.manager.effective_cwd(session)
        return Path.home()

    def perms_command(self, session: Session | None, args: str) -> str:
        """/perms [auto|lock]: авто-одобрение запросов разрешений сессии.

        Без аргументов — статус. `auto` — одобрять всё, не спрашивая (тот же
        эффект, что у кнопки «✅ Всё» на запросе). `lock` — снова спрашивать
        по каждому запросу."""
        action = (args or "").strip().lower()
        if action == "auto":
            if session is None:
                raise UserError(self.t("only_topic"))
            session.auto_allow = True
            return self.t("perms_auto_on")
        if action == "lock":
            if session is None:
                raise UserError(self.t("only_topic"))
            session.auto_allow = False
            return self.t("perms_auto_off")
        if action:
            raise UserError(self.t("perms_unknown_arg", arg=action))
        if session is None:
            return self.t("perms_status_main")
        return self.t(
            "perms_status",
            mode=self.config.permission_mode,
            auto=self.t("perms_auto_yes" if session.auto_allow else "perms_auto_no"),
        )

    def term_command(self, session: Session | None, key: str, args: str) -> str:
        """Обработка /term [on|off]: включить/выключить липкий режим терминала.

        Без аргументов — краткий статус топика: включён ли режим, рабочий
        каталог, занята ли оболочка командой.

        Работает и в главном чате (session is None): там шелл идёт по ХОСТУ с
        правами оператора без песочницы, и при включении оператор об этом
        предупреждается. Риск «забыл, что в терминале» снимает закреплённый
        статус (см. адаптер) — он висит, пока режим активен.
        """
        action = (args or "").strip().lower()

        if action == "on":
            self.term.turn_on(key)
            # В главном чате шелл идёт по ХОСТУ с правами оператора без песочницы:
            # предупреждаем, что любая опечатка выполнится на машине. В топике
            # сессии этого риска нет — там bwrap.
            host = session is None
            return self.t("term_on", cwd=str(self.bash_cwd(session))) + (
                self.t("term_on_host_warn") if host else ""
            )

        if action == "off":
            self.term.turn_off(key)
            return self.t("term_off")

        if action == "":
            # Статус: включён/выключен, каталог, занятость оболочки.
            if self.term.is_on(key):
                cwd = str(self.bash_cwd(session)) if session is not None else str(Path.home())
                busy = "да" if self.bash_busy(key) else "нет"
                return self.t("term_status_on", cwd=cwd, busy=busy)
            return self.t("term_status_off")

        raise UserError(self.t("term_unknown_arg", arg=args.strip()))

    async def run_bash(
        self,
        key: str,
        session: Session | None,
        cmd: str,
        # (html, done, путь-к-файлу-с-полным-выводом|None)
        on_update: Callable[[str, bool, str | None], Awaitable[None]],
    ) -> None:
        """Выполнить команду в постоянном bash-терминале. Скоуп зависит от
        контекста: в сессии — та же файловая песочница, что у claude (только
        папка сессии/проекта); в главном чате — на ХОСТЕ без песочницы (полный
        операторский скоуп, cwd = дом). Стримит рендер (HTML) через
        on_update(html, done); в конце — код возврата. Занят — UserError.
        """
        cwd = self.bash_cwd(session)
        if session is not None:
            # Баш В СЕССИИ → скоуп сессии: та же файловая песочница, что и claude
            # (видит только папку сессии/проекта). agent-vm отдельный /bash не
            # изолирует (одна VM на cwd) — не деградируем молча, отказываем.
            if not self.manager.runner_for(session).supports_prefix:
                raise UserError(
                    self.t("bash_no_isolation", sandbox=self.manager.engine_of(session))
                )
            wrapper = self.manager.sandbox_prefix(
                chdir=cwd, extra_rw=[cwd, session.session_dir], session=session
            )
        else:
            # Баш СНАРУЖИ сессии (main-chat) → операторский терминал на ХОСТЕ,
            # полный скоуп, без песочницы (systemctl, управление хостом). Это
            # команда ТОЛЬКО оператора (ALLOWED_USER_IDS), не модели.
            wrapper = None
        shell = self.bash.get_or_create(key, cwd, wrapper)
        if shell.busy:
            raise UserError(self.t("bash_busy"))
        shell.busy = True
        # Запоминаем команду в историю терминала (стрелка вверх для чата):
        # повтор и правка сообщения работают от этой записи.
        self.termhist.add(key, cmd)
        # Маркеры начала И конца: смещение по длине буфера ненадёжно —
        # BashSession тримит буфер до _BUF_CAP, и после переполнения
        # len(snapshot) залипает на пределе, snapshot()[start:] становится
        # пустым, а маркер конца никогда не находится (ложный таймаут на
        # каждой команде). Ищем вывод МЕЖДУ маркерами в полном снепшоте —
        # устойчиво к тримингу, пока сам вывод команды влезает в буфер.
        token = uuid.uuid4().hex
        start_marker = f"__BEG_{token}__"
        done_marker = f"__DONE_{token}__"
        # Кнопке ⏹ нужно знать маркер этой команды: Ctrl-C убивает строку с
        # ним, и без ручного дописывания цикл ниже не увидел бы конца.
        self._done_markers[key] = done_marker
        interrupted = False
        # Перехват вывода ЭТОЙ команды: читающий поток PTY кормит его напрямую
        # (attach_capture), поэтому размер вывода больше не ограничен кольцевым
        # буфером — раньше вывод крупнее _BUF_CAP вытеснял из него метку
        # начала, и команда доезжала до таймаута с пустым результатом.
        #
        # Создаём ДО try: упади это здесь — finally ниже свалился бы на
        # необъявленной переменной, подменив настоящую ошибку и не дойдя до
        # снятия busy. Терминал залип бы навсегда, а в липком режиме следующие
        # сообщения оператора уходили бы в stdin мёртвой команды.
        out_path = self.bash_output_path(cmd, session)
        # Хвост держим ровно по размеру показа: файл заводится, как только
        # вывод в сообщение не влез. Возьми хвост больше — вывод на 20 КБ
        # оператор увидел бы обрезанным и БЕЗ файла, то есть потерял бы то,
        # что до этой ветки доезжало целиком.
        capture = bashshell.OutputCapture(
            out_path, start_marker, done_marker, tail_cap=BASH_OUTPUT_LIMIT)
        try:
            shell.attach_capture(capture)
            # $? сразу за меткой — код возврата именно команды пользователя.
            shell.write(f"echo {start_marker}\n{cmd}\necho {done_marker} $?\n")
            out = b""
            code = None
            deadline = asyncio.get_running_loop().time() + BASH_TIMEOUT
            last_shown = ""
            cwd = self._short_cwd(session)
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(BASH_POLL_INTERVAL)
                out = capture.tail()
                code = capture.code
                shown, _ = self.bash_render(cmd, out, code, cwd=cwd)
                is_terminal = code is not None
                if shown != last_shown or is_terminal:
                    # Файл отдаём только когда вывод в него реально уехал
                    # (не влез в хвост) и команда закончилась.
                    file_path = None
                    if is_terminal:
                        # Закрываем ДО отдачи пути: адаптер сразу отправит файл,
                        # и недописанный буфер уехал бы обрезанным. Повторный
                        # close в finally безвреден (идемпотентен).
                        capture.close()
                        file_path = str(capture.path) if capture.overflowed else None
                    try:
                        await on_update(shown, is_terminal, file_path)
                        last_shown = shown
                    except Exception:
                        pass  # рендер не должен ронять исполнение
                if code is not None:
                    return
            # Таймаут: прерываем убежавшую команду (Ctrl-C), чтобы она не
            # гадила в общий буфер следующему вызову. Оболочка живёт.
            interrupted = True
            shell.interrupt()
            # При таймауте тоже может быть длинный вывод — отдаём файлом.
            capture.close()
            shown, _ = self.bash_render(cmd, capture.tail(), None, timeout=True, cwd=cwd)
            file_path = str(capture.path) if capture.overflowed else None
            await on_update(shown, True, file_path)
        except asyncio.CancelledError:
            # Клиент отвалился (веб: закрыл вкладку) — команда ещё бежит.
            # Прерываем её, иначе её вывод перемешается со следующим /bash,
            # а busy освободится под работающей командой.
            interrupted = True
            shell.interrupt()
            raise
        finally:
            self._done_markers.pop(key, None)
            # Отцепляем перехват в ЛЮБОМ случае: иначе вывод следующей команды
            # продолжал бы литься в файл предыдущей.
            capture.close()
            shell.attach_capture(None)
            # Файлы с выводом копились бы до удаления сессии: сборочный лог на
            # десятки мегабайт, гоняемый по десять раз в день, съедает диск.
            if capture.overflowed:
                self._trim_bash_outputs(out_path.parent)
            if not interrupted:
                shell.busy = False
            else:
                # Дать Ctrl-C дойти и осесть, потом освободить (в фоне, чтобы
                # не держать вызывающего и не падать на отменённом таске).
                async def _release():
                    await asyncio.sleep(0.5)
                    shell.busy = False
                self._track(_release())

    def _track(self, coro) -> asyncio.Task:
        """Запустить фоновую задачу с удержанием ссылки — самоочищается по
        завершении. Для мелких задач ядра без иного владельца (см. _bg_tasks)."""
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    def bash_busy(self, key: str) -> bool:
        """Занят ли терминал (идёт команда) — адаптер спрашивает ДО поста
        статус-сообщения, чтобы не оставить висящий «⏳ Выполняю…»."""
        shell = self.bash.get(key)
        return shell is not None and shell.busy

    def bash_input(self, key: str, text: str) -> bool:
        """Досыл сырого ввода в открытый терминал (ответ на y/n). False — нет
        открытой оболочки."""
        shell = self.bash.get(key)
        if shell is None:
            return False
        shell.write(text + "\n")
        return True

    def bash_input_if_busy(self, key: str, text: str) -> bool:
        """Отдать текст РАБОТАЮЩЕЙ команде на вход. False — команды уже нет.

        Проверка «занят» и запись — одним вызовом, потому что порознь они
        образуют гонку: команда успевает завершиться между ними, и то, что
        оператор писал ей в ответ («y», пароль), достаётся освободившейся
        оболочке уже как КОМАНДА. Событийный цикл у нас один, поэтому
        достаточно не делать await между проверкой и записью.
        """
        shell = self.bash.get(key)
        if shell is None or not shell.busy:
            return False
        shell.write(text + "\n")
        return True

    def term_last_commands(self, key: str, count: int = 5) -> list[str]:
        """Последние команды терминала — для кнопки «История» и меню."""
        return self.termhist.last(key, count)

    def interrupt_bash(self, key: str) -> bool:
        """Прервать бегущую команду терминала (Ctrl-C). False — оболочки нет.

        Ctrl-C стирает недописанную строку bash — а в ней лежит наш маркер
        конца, поэтому он НЕ придёт никогда: цикл в run_bash досидел бы до
        таймаута (10 минут) с застывшим статусом, кнопкой ⏹ и залипшим busy,
        из-за которого следующие сообщения уходили бы в stdin вместо запуска.
        Поэтому маркер дописываем сами: команда убита, но цикл видит конец,
        рисует финальный статус и штатно освобождает терминал.
        """
        shell = self.bash.get(key)
        if shell is None:
            return False
        shell.interrupt()
        marker = self._done_markers.get(key)
        if marker is not None:
            # 130 — код возврата команды, убитой SIGINT (128 + 2), как в шелле.
            shell.write(f"echo {marker} 130\n")
        return True

    def _short_cwd(self, session: Session | None) -> str:
        """Базовый каталог терминала для шапки вывода: ~/... вместо полного."""
        cwd = self.bash_cwd(session)
        home = Path.home()
        try:
            rel = cwd.relative_to(home)
            return f"~/{rel}" if str(rel) != "." else "~"
        except ValueError:
            return str(cwd)

    def bash_render(
        self, cmd: str, out: bytes, code: str | None, timeout: bool = False,
        cwd: str | None = None,
    ) -> tuple[str, bool]:
        """HTML статуса bash: команда + вывод в <pre>, обрезка с хвоста.
        Возвращает (html, was_truncated) — флаг обрезки нужен вызывающему,
        чтобы сохранить полный вывод в файл и отдать адаптеру.

        cwd — рабочий каталог сессии в шапке (чтобы было видно, ГДЕ выполнено).
        Это базовый каталог терминала, а не живой pwd: после `cd subdir` он не
        обновится. Живой pwd потянул бы чтение /proc бwrap'а — дорого и хрупко,
        а «в каком проекте» — отвечает и базовый."""
        text = out.decode(errors="replace")
        truncated = len(text) > BASH_OUTPUT_LIMIT
        if truncated:
            text = "…" + text[-BASH_OUTPUT_LIMIT:]
        body = html.escape(text) if text else self.t("bash_no_output")
        if cwd:
            header = f"⚡ <code>{html.escape(cwd)}</code> ❯ <code>{html.escape(cmd)}</code>"
        else:
            header = f"⚡ <code>{html.escape(cmd)}</code>"
        if timeout:
            footer = self.t("bash_timeout")
        elif code is None:
            footer = self.t("bash_wait")
        else:
            footer = self.t("bash_done", code=code)
        return f"{header}\n<pre>{body}</pre>\n{footer}", truncated

    @staticmethod
    def _trim_bash_outputs(folder: Path, keep: int = BASH_OUTPUTS_KEEP) -> None:
        """Держать в папке только последние `keep` выводов.

        Иначе файлы копятся до удаления сессии: сборочный лог на десятки
        мегабайт, запускаемый по десять раз в день, за неделю съедает диск.
        Старое здесь уже не нужно — за ним всё равно никто не возвращается,
        а свежий вывод лежит рядом со свежим сообщением в чате.
        """
        try:
            files = sorted(
                folder.glob("bash_*.txt"), key=lambda p: p.stat().st_mtime, reverse=True
            )
        except OSError:
            return
        for stale in files[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass  # чужой файл//гонка — чистка не должна ронять команду

    def bash_output_path(self, cmd: str, session: Session | None) -> Path:
        """Куда OutputCapture положит полный вывод команды.

        Файл заводится ЛЕНИВО (только если вывод не влез в хвост), поэтому
        здесь мы лишь считаем путь и ничего не создаём. В сессии — в её папке
        (веб отдаёт оттуда через существующий jail), иначе — во временный
        каталог ОС.
        """
        # Суффикс-токен обязателен: одна и та же команда из телеграма и из веба
        # идёт параллельно (ключи оболочек разные), и без него оба перехвата
        # открыли бы ОДИН файл — один обнулил бы другой, а в итоге оператор
        # получил бы чересполосицу двух логов.
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"bash_{ts}_{slugify(cmd)[:40]}_{uuid.uuid4().hex[:6]}.txt"
        if session is not None:
            return session.session_dir / ".bash_outputs" / fname
        # СВОЯ подпапка, а не корень /tmp: чистка старых выводов удаляет всё,
        # что подходит под bash_*.txt, и в общем /tmp снесла бы чужие файлы —
        # хоть соседнего экземпляра оркестратора, хоть постороннего процесса.
        return Path(tempfile.gettempdir()) / "claude-orchestrator-bash" / fname

    # ── уведомления жизненного цикла ────────────────────────────

    async def notify_session_dead(self, session: Session, code: int | str) -> None:
        """Колбэк SessionManager: Claude умер сам по себе."""
        await self._teardown_runtime(session)
        await self._notify_state_changed(session)
        text = self.t("session_died", name=session.title, code=code)
        tail = await asyncio.to_thread(self.manager.tail_log, session)
        if tail:
            text += "\n\n" + self.t("session_died_tail", tail=tail[:1500])
        await self.notice(session, text)

    async def notify_docker_launch(self, name: str, summary: str) -> None:
        """Колбэк docker-прокси: модель запустила контейнер — подсветить в бабле."""
        await self.bubbles.append_background(
            name, f"🐳 docker <code>{html.escape(summary)}</code>", tool="docker")

    async def notify_idle_closed(self, sessions: list[Session]) -> None:
        """Колбэк sweeper: сессии остановлены по простою."""
        for session in sessions:
            await self._teardown_runtime(session)
            await self._notify_state_changed(session)
            await self.notice(
                session, self.t("idle_closed", hours=f"{self.config.idle_timeout_h:g}")
            )

    async def notify_startup(self, restored: int) -> None:
        """Сообщить во все адаптеры, что оркестратор онлайн."""
        # Учётка ПО УМОЛЧАНИЮ (config_dir_of(None) учитывает CLAUDE_PROFILE):
        # сообщение общее для всех сессий, а своя учётка сессии называется при
        # её создании и помечена в /list.
        config_dir = self.manager.config_dir_of(None) or Path.home() / ".claude"
        base_url = self.config.claude_env.get("ANTHROPIC_BASE_URL") or self.t("url_default")
        await self._each_transport(
            lambda tr: tr.notify(
                None, self.t("startup", n=restored, config=config_dir, url=base_url)
            ),
            "Стартовое уведомление", warn=True,
        )

    # ── форматирование ──────────────────────────────────────────

    @staticmethod
    def fmt_num(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    def fmt_duration(self, seconds: float) -> str:
        m = int(seconds) // 60
        if m < 60:
            return self.t("min", m=m)
        return self.t("hour_min", h=m // 60, m=m % 60)
