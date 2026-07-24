"""TtyVaultHost — реализация VaultHost для ЗАПУСКА БЕЗ ОРКЕСТРАТОРА (standalone
`vault serve`). Никакого Telegram/бабла: подтверждение спрашивается на tty,
наблюдаемость и аудит идут в stderr-лог.

Симметрично OrchestratorVaultHost (кнопки/бабл/notice), но для локального
человека за терминалом. Часть автономного пакета vault/ — оркестратор не нужен.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

logger = logging.getLogger("vault.tty")

_TAG = re.compile(r"<[^>]+>")  # снять HTML-разметку из observe-строк для tty

# Таймаут ASK-спроса на tty (Р0: молчание оператора → безопасный дефолт False).
# confirm остаётся без таймаута (блокирующее подтверждение перед действием), а
# ASK поднимается посреди HTTP-запроса под прокси — виснуть на нём нельзя.
_ASK_TIMEOUT = 120.0

_YES = ("y", "yes", "д", "да")


class TtyVaultHost:
    """Standalone-хост: confirm через tty, observe/record/notify — в stderr-лог.

    assume_yes=True — не спрашивать, всё подтверждать (неинтерактивный режим,
    напр. CI/скрипт). По умолчанию: есть tty → спросить; нет tty → отказать
    (безопасная сторона — не подтверждаем вслепую)."""

    def __init__(
        self,
        *,
        assume_yes: bool = False,
        ask_timeout: float = _ASK_TIMEOUT,
        log: logging.Logger | None = None,
    ) -> None:
        self.assume_yes = assume_yes
        # Таймаут ASK — атрибут (не только модульная константа): тесты подставляют
        # малое значение, чтобы проверить размыкание без 2-мин ожидания.
        self.ask_timeout = ask_timeout
        self.log = log or logger
        # Одна tty на весь демон → confirm/ask сериализуем ОДНИМ локом: без этого
        # конкурентные add_reader на один fd затирали бы колбэк друг друга и более
        # ранний запрос висел бы вечно (нашло ревью 1.5).
        self._confirm_lock = asyncio.Lock()

    async def confirm(self, session_name: str, description: str, preview: str) -> bool:
        if self.assume_yes:
            return True
        if not sys.stdin.isatty():
            self.log.warning(
                "confirm без tty и без --yes → отказ: %s", description)
            return False
        # confirm блокирует до ответа (подтверждение ПЕРЕД действием — без таймаута
        # by design). Внешняя отмена (shutdown) пробрасывается как CancelledError,
        # НЕ маскируем её обычным False.
        ans = await self._prompt_locked(f"vault: подтвердить «{description}»?", preview)
        return ans.strip().lower() in _YES

    async def ask(self, session_name: str, description: str, preview: str) -> bool:
        """ASK-грант на ЭТОТ запрос (§4.6). Спрашиваем на tty ПО ОБРАЗЦУ confirm
        (add_reader+lock, отменяемо), но с САМОДОСТАТОЧНЫМ таймаутом (Р0): ASK
        поднимается посреди запроса под прокси — молчание оператора не должно
        висеть, дефолт False. assume_yes → True; нет tty → False (некому спросить).

        Таймаут ОБЁРНУТ ВОКРУГ ВСЕГО тела (_prompt_locked), включая ЗАХВАТ лока:
        если параллельный confirm (у него таймаута нет by design) держит tty-лок и
        никто не отвечает, ask всё равно разомкнётся по своему таймауту, а не
        застрянет на acquire до начала отсчёта (нашло ревью). asyncio.timeout —
        только 3.11+, поэтому wait_for (есть с 3.4, держим 3.10)."""
        if self.assume_yes:
            return True
        if not sys.stdin.isatty():
            self.log.warning("ask без tty и без --yes → отказ: %s", description)
            return False
        try:
            ans = await asyncio.wait_for(
                self._prompt_locked(f"vault: РАЗРЕШИТЬ доступ «{description}»?", preview),
                self.ask_timeout,
            )
        except asyncio.TimeoutError:
            self.log.warning("ask без ответа (таймаут) → отказ: %s", description)
            return False
        return ans.strip().lower() in _YES

    async def _prompt_locked(self, question: str, preview: str) -> str:
        """Захват tty-лока + промпт + ожидание ответа. БЕЗ своего таймаута: его
        накладывает вызывающий (confirm — блокирующе; ask — обёрнут в wait_for,
        покрывающий И ожидание лока).

        Читаем stdin через add_reader (selectors), а НЕ input() в потоке: поток в
        input() нельзя отменить, и asyncio.run на выходе ждал бы его вечно — SIGINT
        посреди спроса вешал бы `vault serve`. Здесь чтение async и отменяемо:
        CancelledError (от wait_for-таймаута ask или внешней отмены) пробрасывается
        — НЕ маскируем; контекст-менеджер лока и finally снимут reader и лок.

        Один общий лок на confirm И ask: обе делят единственную tty, конкурентные
        add_reader на один fd затирали бы колбэк друг друга и более ранний вопрос
        висел бы вечно (урок ревью 1.5)."""
        async with self._confirm_lock:
            sys.stderr.write(f"{question}\n  {preview}\n[y/N] ")
            sys.stderr.flush()
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[str] = loop.create_future()
            fd = sys.stdin.fileno()

            def _readable() -> None:
                if fut.done():
                    return
                try:
                    data = os.read(fd, 4096)  # tty канонический: строка готова
                except OSError:
                    data = b""
                fut.set_result(data.decode("utf-8", "replace"))

            loop.add_reader(fd, _readable)
            try:
                return await fut
            finally:
                loop.remove_reader(fd)

    async def observe(self, session_name: str, line_html: str) -> None:
        self.log.info("[%s] %s", session_name, _TAG.sub("", line_html))

    def record(self, session_name: str, *, secret: str, cmd: str, allowed: bool) -> None:
        self.log.info("audit [%s] %s: %s → %s", session_name, secret, cmd,
                      "allowed" if allowed else "denied")

    async def notify_denied(self, session_name: str, cmd_display: str) -> None:
        self.log.warning("[%s] ОТКАЗ: %s", session_name, cmd_display)


__all__ = ["TtyVaultHost"]
