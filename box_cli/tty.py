"""Арбитр терминала claude-box: ЕДИНСТВЕННЫЙ владелец stdin + VaultHost поверх него.

Зачем. В claude-box один и тот же fd (stdin терминала) нужен двум потребителям:

  * PTY-relay лончера — гонит нажатия оператора в claude (терминал в raw);
  * кошелёк — спрашивает confirm/ASK перед тем, как пустить команду на хост
    с реальным кредом.

Наивное решение (каждый вешает свой `loop.add_reader`) НЕ работает и ломается
необратимо: в asyncio второй add_reader на тот же fd ЗАМЕНЯЕТ колбэк, а
`remove_reader` в finally снимает читателя ЦЕЛИКОМ. То есть после первого же
confirm (а confirm в policy — штатная настройка, а не экзотика) клавиатура в
сессии умирала бы до конца процесса, ответ «y» печатался бы вслепую (raw = нет
эха), и его байты воровались бы у claude.

Поэтому stdin здесь ровно один хозяин — StdinArbiter:

  * один add_reader на весь запуск;
  * по умолчанию прочитанные байты уходят в PTY-мастер процесса;
  * на время вопроса арбитр ПЕРЕКЛЮЧАЕТ режим: терминал возвращается в
    нормальный (канонический, с эхом — оператор видит, что печатает), вопрос
    печатается в stderr, собранная строка отдаётся ожидающему future, терминал
    возвращается в raw, и байты снова льются в PTY;
  * восстановление режима — в finally, поэтому исключение/Ctrl-C/таймаут не
    оставляют терминал сломанным;
  * молчание оператора = таймаут = пустой ответ = DENY (правило «никогда не
    повисать»: висящий вопрос заморозил бы и сессию).

BoxVaultHost — реализация vault.host.VaultHost поверх арбитра; подставляется в
демон/прокси кошелька ВМЕСТО TtyVaultHost (тот остаётся для `vault serve`, где
stdin ничей и читать его самому можно).

UnattendedVaultHost — тот же контракт для `claude-box -p …`: терминала и
оператора нет, поэтому вопросы не задаются, а сразу отклоняются с записью в лог
(§4.6 «unattended → deny+log»). Общее у обоих хостов (лог наблюдаемости/аудита)
вынесено в _LogVaultHost, чтобы не расходилось.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import sys
import termios
from typing import Callable

from vault.connectors.contract import ScopeGrant
from vault.host import AskResult
from vault.policy import PolicyEditor, PolicyError

logger = logging.getLogger("claude-box.tty")

_TAG = re.compile(r"<[^>]+>")  # снять HTML-разметку из observe-строк для терминала

# Управляющие/непечатаемые байты (C0 без \t, DEL, C1) — вырезаем из ЛЮБОГО
# недоверенного текста перед выводом на реальный tty. Иначе модель, управляя URL
# запроса, протащила бы ESC-последовательности в grant.value (percent-декодируется
# коннектором) и очистила бы/подрисовала бы экран оператора поверх ASK-вопроса —
# спуфинг ровно той прозрачности, ради которой ASK и существует (нашло ревью).
# PolicyEditor такое отклоняет на ЗАПИСИ, но экран подделывается ДО ответа.
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _tty_safe(text: str) -> str:
    """Обезвредить недоверенный текст для вывода в терминал: убрать управляющие
    байты (ESC/CR/переводы каретки/C1). \\t и \\n сохраняем — они безопасны и
    нужны для разметки самого вопроса."""
    return _CTRL.sub("", text)

# Таймаут вопроса. У TtyVaultHost без таймаута живёт только confirm (там вопрос
# держит один лишь `vault serve`), здесь же вопрос держит ВЕСЬ терминал сессии:
# зависший confirm заморозил бы и claude. Поэтому таймаут на обоих — молчание
# оператора трактуется как отказ (безопасная сторона), как и в ASK.
PROMPT_TIMEOUT = 120.0

_YES = ("y", "yes", "д", "да")
# Третий исход ASK (§4.6, «разрешить навсегда»): узкий грант пишется в policy.
# Латинское «a» и кириллическое «а»/«всегда» — оператор в русской раскладке не
# должен угадывать раскладку, чтобы записать грант.
_ALWAYS = ("a", "always", "а", "всегда")


class StdinArbiter:
    """Единственный владелец stdin: relay в PTY + вопросы оператору.

    write_bytes — куда лить байты в обычном режиме (обычно os.write в pty_master
    процесса); возвращает False, если писать больше некуда (EOF/ошибка) — тогда
    арбитр снимает relay, но остаётся живым для вопросов.
    """

    def __init__(
        self,
        stdin_fd: int,
        *,
        write_bytes: Callable[[bytes], bool] | None = None,
        timeout: float = PROMPT_TIMEOUT,
        log: logging.Logger | None = None,
    ) -> None:
        self.fd = stdin_fd
        self._write = write_bytes
        self.timeout = timeout
        self.log = log or logger
        self._reader_on = False
        self._relay_on = True
        self._pending: asyncio.Future[str] | None = None
        self._buf = bytearray()
        # Один вопрос за раз: терминал общий, два параллельных вопроса перемешали
        # бы ввод (тот же урок, что у TtyVaultHost — один лок на confirm и ask).
        self._lock = asyncio.Lock()
        # Настройки терминала ДО raw — их и восстанавливаем на время вопроса
        # (канонический режим + эхо). Снимаем при создании арбитра, то есть до
        # входа в raw_terminal.
        self._cooked: list | None = None
        if os.isatty(self.fd):
            with contextlib.suppress(termios.error, OSError):
                self._cooked = termios.tcgetattr(self.fd)

    # ── владение fd ──────────────────────────────────────────────────────────
    def set_sink(self, write_bytes: Callable[[bytes], bool]) -> None:
        """Назначить приёмник relay (PTY-мастер появляется позже арбитра)."""
        self._write = write_bytes
        self._relay_on = True

    def start(self) -> bool:
        """Повесить ЕДИНСТВЕННЫЙ читатель stdin. False — fd не селектится
        (не tty/закрыт): тогда relay нет, а вопросы честно отвечают DENY."""
        if self._reader_on:
            return True
        try:
            asyncio.get_running_loop().add_reader(self.fd, self._readable)
        except (OSError, ValueError):
            return False
        self._reader_on = True
        return True

    def stop(self) -> None:
        """Снять читатель (выход из запуска). Идемпотентно."""
        if not self._reader_on:
            return
        self._reader_on = False
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(self.fd)

    # ── чтение ───────────────────────────────────────────────────────────────
    def _readable(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        fut = self._pending
        if fut is not None and not fut.done():
            self._collect(data, fut)
            return
        if not data:  # EOF stdin — релеить больше нечего, но вопросы ещё живы
            self._relay_on = False
            self.stop()
            return
        if self._relay_on and self._write is not None and not self._write(data):
            self._relay_on = False

    def _collect(self, data: bytes, fut: asyncio.Future[str]) -> None:
        """Режим вопроса: копим строку до перевода, остаток отдаём relay."""
        if not data:  # EOF посреди вопроса — ответа не будет, значит отказ
            fut.set_result("")
            return
        self._buf += data
        for i, ch in enumerate(self._buf):
            if ch in (0x0A, 0x0D):  # \n или \r
                line = bytes(self._buf[:i]).decode("utf-8", "replace")
                rest = bytes(self._buf[i + 1:])
                self._buf.clear()
                fut.set_result(line)
                # Всё, что оператор успел напечатать после ответа, — это уже ввод
                # сессии: не теряем его, а отдаём в PTY.
                if rest and self._relay_on and self._write is not None:
                    if not self._write(rest):
                        self._relay_on = False
                return

    # ── вопрос оператору ─────────────────────────────────────────────────────
    @contextlib.contextmanager
    def _normal_mode(self):
        """Вернуть терминал в нормальный режим (эхо+строки) на время вопроса и
        восстановить прежний (raw) в finally — при любом исходе."""
        if self._cooked is None or not os.isatty(self.fd):
            yield
            return
        try:
            current = termios.tcgetattr(self.fd)
        except (termios.error, OSError):
            yield
            return
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self._cooked)
            yield
        finally:
            with contextlib.suppress(termios.error, OSError):
                termios.tcsetattr(self.fd, termios.TCSANOW, current)

    async def prompt(self, question: str, preview: str, choices: str = "[y/N] ") -> str:
        """Спросить оператора и вернуть введённую строку («» = нет ответа).

        Читаем ТЕМ ЖЕ единственным читателем (никаких своих add_reader), поэтому
        relay не ломается: после ответа байты снова уходят в PTY. Таймаут — свой,
        по истечении возвращаем «» (вызывающий трактует как DENY).

        `choices` — хвост подсказки ответа. По умолчанию `[y/N] ` (confirm и
        двоичный ASK); ASK с грантом «навсегда» передаёт `[y/N/a] `, добавляя
        третий исход. Транспорт от этого не меняется — меняется лишь печатаемая
        строка и разбор ответа у вызывающего."""
        if not self.start():
            self.log.warning("вопрос без читаемого stdin → отказ: %s", question)
            return ""
        # question/preview содержат недоверенный текст (descr коннектора, URL
        # запроса, значение гранта) — обезвреживаем управляющие байты ПЕРЕД
        # выводом на tty (см. _tty_safe). choices — наш литерал, не трогаем.
        question, preview = _tty_safe(question), _tty_safe(preview)
        async with self._lock:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str] = loop.create_future()
            self._buf.clear()
            self._pending = fut
            try:
                with self._normal_mode():
                    sys.stderr.write(f"\r\n{question}\r\n  {preview}\r\n{choices}")
                    sys.stderr.flush()
                    try:
                        return await asyncio.wait_for(fut, self.timeout)
                    except asyncio.TimeoutError:
                        sys.stderr.write("\r\n(нет ответа — отказ)\r\n")
                        sys.stderr.flush()
                        return ""
            finally:
                self._pending = None
                self._buf.clear()


class _LogVaultHost:
    """Общая часть хостов claude-box: наблюдаемость/аудит/уведомление — в лог.

    Вопросы (confirm/ask) наследники решают по-разному: attended спрашивает
    оператора через арбитра, unattended отказывает сразу. Всё остальное у них
    одинаково, поэтому живёт здесь и не копируется (значения секретов не
    печатаются нигде — в лог идут только имя секрета и команда)."""

    def __init__(self, log: logging.Logger | None = None) -> None:
        self.log = log or logger

    async def observe(self, session_name: str, line_html: str) -> None:
        self.log.info("[%s] %s", session_name, _TAG.sub("", line_html))

    def record(self, session_name: str, *, secret: str, cmd: str, allowed: bool) -> None:
        self.log.info("audit [%s] %s: %s → %s", session_name, secret, cmd,
                      "allowed" if allowed else "denied")

    async def notify_denied(self, session_name: str, cmd_display: str) -> None:
        self.log.warning("[%s] ОТКАЗ: %s", session_name, cmd_display)


class UnattendedVaultHost(_LogVaultHost):
    """VaultHost для unattended-запуска (`claude-box -p …`): deny+log, без спроса.

    §4.6 архитектуры: «standalone: attended (tty) → вопрос в терминал; unattended
    (-p, CI) → deny+log». Здесь оператора за терминалом НЕТ (stdin не наш, эхо
    некуда печатать), поэтому вопрос не задаётся ВООБЩЕ — ни с таймаутом, ни
    вслепую:

      * ждать таймаут бессмысленно и вредно: спрашивать некого, а модель всё это
        время висела бы на своём HTTP/exec-вызове (Р0 «никогда не повисать» —
        отказ обязан прийти сразу);
      * «молча разрешить» тем более нельзя: confirm в policy оператор поставил
        именно затем, чтобы каждый такой вызов проходил через него.

    Отказ прозрачен в обе стороны: оператору — строка в лог (какой секрет, какая
    команда, почему), модели — предписывающий текст deny_remedy (его подставляют
    демон и прокси вместо своих формулировок про «оператор не подтвердил», см.
    vault.host.deny_remedy). Смысл текста: «в unattended не спрашивают; либо
    попроси разрешение заранее, либо запусти без -p»."""

    deny_remedy = (
        "Запрос отклонён: claude-box запущен в unattended-режиме (-p), в нём "
        "подтверждения у оператора не спрашиваются — спросить некого, поэтому "
        "всё, что требует подтверждения, отклоняется сразу (а не ждёт ответа). "
        "Что делать: обойдись тем, что разрешено без подтверждения; либо попроси "
        "оператора заранее разрешить это в policy кошелька (убрать confirm / "
        "расширить скоуп секрета); либо пусть он запустит задачу интерактивно "
        "(claude-box без -p) — тогда вопрос дойдёт до терминала. Повторять тот же "
        "вызов бессмысленно: ответ будет тем же."
    )

    async def _deny(self, what: str, description: str, preview: str) -> bool:
        self.log.warning(
            "unattended (-p): %s отклонён без спроса — «%s»; вызов: %s",
            what, description, preview,
        )
        return False

    async def confirm(self, session_name: str, description: str, preview: str) -> bool:
        return await self._deny("confirm", description, preview)

    async def ask(self, session_name: str, description: str, preview: str) -> bool:
        return await self._deny("ask", description, preview)


class BoxVaultHost(_LogVaultHost):
    """VaultHost для claude-box: confirm/ASK через арбитра терминала.

    Отличия от TtyVaultHost: не трогает stdin сам (спрашивает через арбитра,
    который делит fd с relay) и даёт таймаут ОБОИМ вопросам — вопрос держит
    терминал живой сессии, зависнуть тут нельзя. Наблюдаемость/аудит — в лог, как
    и у TtyVaultHost (значения секретов не печатаются нигде).

    ASK-грант «навсегда» (§4.6, третий исход). У ASK три ответа: `n` (отказ),
    `y` (разрешить РАЗОВО, policy не трогаем), `a` (записать УЗКИЙ грант в policy
    — такой запрос дальше пройдёт без спроса). Третий вариант предлагается ТОЛЬКО
    когда коннектор дал узкий грант И правка policy разрешена; ЧТО именно
    запишется (секрет/ключ/значение) и как отозвать оператор видит в тексте
    вопроса ДО ввода — «навсегда» вслепую недопустимо. Живой прокси claude-box
    подхватывает запись сам (он поднят со store и перечитывает secrets.toml по
    mtime/size, а persisted=True дополнительно синхронно расширяет его снимок
    scope — см. proxy._apply_grant; дублирование он дедупит, двойного применения
    нет).

    `policy`/`allow_policy_edit` нужны ТОЛЬКО для этого третьего исхода. Без них
    (policy=None или allow_policy_edit=False — напр. secrets.toml только на
    чтение) хост работает как раньше: `[y/N]`, лишь разовый грант, и в тексте
    честно сказано, почему «навсегда» недоступно.
    """

    def __init__(
        self, arbiter: StdinArbiter, *, assume_yes: bool = False,
        policy: PolicyEditor | None = None, allow_policy_edit: bool = False,
        log: logging.Logger | None = None,
    ) -> None:
        super().__init__(log)
        self.arbiter = arbiter
        self.assume_yes = assume_yes
        self._policy = policy
        self._allow_policy_edit = allow_policy_edit

    async def _yesno(self, question: str, preview: str, what: str) -> bool:
        if self.assume_yes:
            return True
        if not os.isatty(self.arbiter.fd):
            self.log.warning("%s без tty → отказ: %s", what, preview)
            return False
        ans = await self.arbiter.prompt(question, preview)
        ok = ans.strip().lower() in _YES
        if not ok:
            self.log.info("%s: отказ оператора (%s)", what, preview)
        return ok

    async def confirm(self, session_name: str, description: str, preview: str) -> bool:
        return await self._yesno(
            f"кошелёк: подтвердить «{description}»?", preview, "confirm")

    def _grant_offer(self, grant: ScopeGrant | None) -> tuple[bool, str]:
        """Можно ли предложить «навсегда» → (можно?, довесок к тексту вопроса).

        Когда МОЖНО — довесок показывает точную будущую запись (секрет · ключ ·
        значение), человеческую расшифровку и команду отзыва, ВСЁ это до ввода:
        правило прозрачности — грант «навсегда» оператор одобряет, только увидев,
        что именно ляжет в policy. Когда НЕЛЬЗЯ — коротко ПОЧЕМУ, чтобы отсутствие
        варианта «a» не читалось как поломка (то же, что делает оркестратор).

        Условия «можно» (все сразу):
          * коннектор дал УЗКИЙ грант (grant не None — из запроса к корню сервиса
            узкого префикса не вывести, «разрешить всё» кнопкой не выдаём);
          * грант знает секрет (его штампует прокси — см. proxy._ask_grant);
          * есть редактор policy и правка разрешена (secrets.toml пишется).
        """
        if grant is None or not grant.secret:
            return False, (
                "\r\n  (только разово: узкого гранта из этого запроса не выводится "
                "— «навсегда» недоступно)")
        if self._policy is None or not self._allow_policy_edit:
            return False, (
                "\r\n  (только разово: policy недоступна на запись — «навсегда» "
                "недоступно, правь secrets.toml вручную)")
        revoke = f"vault policy scope {grant.secret} -{grant.value}"
        return True, (
            f"\r\n  [a] «навсегда» ЗАПИШЕТ в policy: "
            f"{grant.secret} · scope.{grant.key} += {grant.value}"
            f"\r\n      ({grant.label})"
            f"\r\n      отозвать: {revoke}")

    async def ask(
        self,
        session_name: str,
        description: str,
        preview: str,
        grant: ScopeGrant | None = None,
    ) -> AskResult:
        """Спрос ГРАНТа доступа ВНЕ scope (§4.6 ASK-flow) с тремя исходами.

        `n`/пусто/таймаут/без tty → AskResult(granted=False); `y` →
        AskResult(granted=True) (разовый грант, policy не тронут); `a` (только
        если предложен) → узкая запись в policy и AskResult(granted=True,
        persisted=True), а при сбое записи — честный AskResult(granted=True,
        persisted=False) с сообщением «в policy не записано» (см. _persist_grant).

        Обратная совместимость: без grant / без policy-edit ведёт себя как прежний
        двоичный ASK (`[y/N]`), результат нормализуется в AskResult. Значение
        секрета сюда не приходит и не печатается — preview это факт запроса."""
        if self.assume_yes:
            # Неинтерактивный «да»: разрешаем разово, но НЕ пишем policy — грант
            # «навсегда» без явного взгляда оператора был бы записью вслепую.
            return AskResult(granted=True)
        if not os.isatty(self.arbiter.fd):
            self.log.warning("ask без tty → отказ: %s", preview)
            return AskResult(granted=False)
        can_always, note = self._grant_offer(grant)
        question = f"кошелёк: РАЗРЕШИТЬ доступ «{description}»?{note}"
        choices = "[y/N/a] " if can_always else "[y/N] "
        ans = (await self.arbiter.prompt(question, preview, choices=choices)).strip().lower()
        if can_always and ans in _ALWAYS:
            return await self._persist_grant(session_name, grant)
        if ans in _YES:
            return AskResult(granted=True)  # разовый грант, policy не тронут
        self.log.info("ask: отказ оператора (%s)", preview)
        return AskResult(granted=False)

    async def _persist_grant(self, session_name: str, grant: ScopeGrant) -> AskResult:
        """Записать узкий грант в policy и ЧЕСТНО отчитаться в tty/лог.

        Сбой записи (нет прав/лок занят/битый TOML) НЕ превращаем в тихое
        «разрешено разово»: доступ на ЭТОТ запрос оператор уже одобрил (отзывать
        назад — сюрприз хуже), но факт «в policy НЕ записано» печатается в
        терминал и уходит в лог, а persisted=False оставляет прокси со старым
        scope — следующий такой запрос честно спросит снова. Молчаливого «оператор
        думает, что записал навсегда, а не записал» не возникает."""
        assert grant is not None  # вариант «a» без гранта не предлагается
        assert self._policy is not None
        try:
            # grant_scope синхронный и ждёт межпроцессный flock (до ~1с при
            # конкурентной правке secrets.toml из бота/CLI). В event-loop это
            # заморозило бы ВСЮ сессию, включая PTY-relay — выносим в executor.
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self._policy.grant_scope(
                    grant.secret, grant.key, grant.value, exist_ok=True))
        except (PolicyError, OSError) as e:
            self.log.error(
                "wallet: постоянный грант НЕ записан (%s.%s += %s): %s",
                grant.secret, grant.key, grant.value, e)
            sys.stderr.write(
                f"\r\n(в policy НЕ записано: {_tty_safe(str(e))}; доступ выдан "
                "только на этот запрос)\r\n")
            sys.stderr.flush()
            return AskResult(granted=True, persisted=False)
        sys.stderr.write(
            f"\r\nзаписано в policy: {grant.secret} · scope.{grant.key} += "
            f"{grant.value}\r\nотозвать: vault policy scope {grant.secret} "
            f"-{grant.value}\r\n")
        sys.stderr.flush()
        self.log.info(
            "wallet: постоянный грант записан: scope.%s += %s (секрет %s)",
            grant.key, grant.value, grant.secret)
        self.record(
            session_name, secret=grant.secret,
            cmd=f"policy scope.{grant.key} += {grant.value}", allowed=True)
        return AskResult(granted=True, persisted=True)


__all__ = ["BoxVaultHost", "UnattendedVaultHost", "StdinArbiter", "PROMPT_TIMEOUT"]
