"""Авто-ответчик стартовых диалогов интерактивного claude: экран → клавиши.

Самодостаточный кусок launch-механики (Слой 2): матчит стартовые диалоги
Claude Code в скользящем окне вывода PTY и возвращает клавиши-ответы. Никаких
зависимостей от SessionManager/оркестратора — только snapshot ANSI-стриппера из
box.ansi. Поток-драйвер PTY, который скармливает сюда вывод и пишет клавиши в
stdin, остаётся у launcher'а (переедет следующим срезом).
"""

from __future__ import annotations

import threading
from typing import Callable

from .ansi import strip_ansi


def _trust_keys(screen: str) -> bytes:
    """Клавиши для диалога доверия рабочей папке — по фактическому фокусу.

    Диалог двухпунктовый, и пункт по умолчанию менялся от версии к версии:
    старые версии ставили «Yes, I trust this folder» первым (хватало Enter),
    а v2.1.275 рендерит «❯ No, exit» первым. Голый Enter там выбирал «No,
    exit» → claude сразу выходил, и оркестратор крутил respawn-цикл сессии
    (инцидент 2026-09-18, сессия ad-coder после смены профиля). Поэтому
    смотрим рендер: ❯ вплотную перед «No, exit» — это новый диалог (пункты в
    нём БЕЗ номеров; прочие диалоги рендерят «❯ 1. No, exit» с номером между
    ❯ и текстом, и маркер «❯no,exit» в них не встречается) — спускаемся
    стрелкой на «Yes, I trust this folder» и жмём Enter. Иначе — Enter как
    раньше.
    """
    return b"\x1b[B\r" if "❯no,exit" in screen else b"\r"


# Стартовые диалоги интерактивного claude и клавиши-ответы.
# Маркеры ищутся в тексте экрана без пробелов и в нижном регистре.
# Клавиши — байты ИЛИ функция (screen → bytes): функция нужна диалогам, где
# ответ зависит от рендера (фокус пункта), а не только от факта показа.
_DIALOGS: list[tuple[str, bytes | Callable[[str], bytes]]] = [
    ("trustthisfolder", _trust_keys),
    # ВАЖНО: маркер — по тексту ПУНКТА диалога («Yes, I accept»), НЕ по
    # «bypasspermissions»: последнее ложно совпадает со строкой СТАТУСА
    # «⏵⏵ bypass permissions on» (постоянная UI-плашка, не диалог) → слался «2»
    # как сообщение в чат (замечено под agent-vm).
    ("yes,iaccept", b"2\r"),           # bypass-permissions диалог: «2. Yes, I accept»
    ("localdevelopment", b"\r"),       # dev-channels: «I am using this for local development»
    # agent-vm ставит managed-настройки (CLAUDE_CODE_MAX_RETRIES/RETRY_WATCHDOG) —
    # Claude на старте просит их подтвердить. Доверяем (это настройки самого
    # sandbox-инструмента, benign retry-конфиг): пункт 1 «Yes, I trust» по умолч.
    ("managedsettingsrequireapproval", b"\r"),
]


class _DialogAnswerer:
    """Матчер стартовых диалогов: экран → клавиши-ответы.

    Работает ТОЛЬКО до готовности сессии и выключается досрочно, когда все
    диалоги отвечены. Это принципиально: маркеры ищутся в скользящем окне
    вывода, а вывод после старта — это уже беседа. Матчер «на всю жизнь сессии»
    впечатывал клавиши в stdin Claude, когда текст беседы случайно содержал
    маркер (модель пишет «yes, I accept» → в сессию уходит «2\\r» и вылезает
    спурьёзным сообщением «your message was just 2»).

    Выключение — по событию, а не по таймеру: `stop()` зовётся, когда
    channel-сервер ответил на /ping. Это ТОЧНЫЙ признак, что Claude полностью
    поднялся и стартовые диалоги позади, и он не зависит от того, сколько
    длился старт (под agent-vm первая загрузка OCI-образа занимает минуты —
    любое фиксированное окно там оставило бы диалог без ответа).

    Живёт в потоке _pty_driver; `stop()` зовётся из event loop — поэтому
    флаг проверяется/ставится под локом.
    """

    def __init__(self) -> None:
        self._answered: set[str] = set()
        self._buf = b""
        self._lock = threading.Lock()
        self._active = True

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def stop(self) -> None:
        """Сессия готова — больше не трогаем stdin (весь вывод дальше — беседа)."""
        with self._lock:
            self._active = False
            self._buf = b""

    def feed(self, chunk: bytes) -> list[bytes]:
        """Скормить кусок вывода. Вернуть клавиши, которые надо послать."""
        with self._lock:
            if not self._active:
                return []
            self._buf = (self._buf + chunk)[-16384:]
            screen = strip_ansi(self._buf).replace(b" ", b"")
            screen_text = screen.decode(errors="replace").lower()
            out: list[bytes] = []
            # Один чанк может принести несколько диалогов сразу (под agent-vm
            # вывод идёт через проброшенный PTY и перерисовки склеиваются) —
            # отвечаем на все, а не только на первый.
            for marker, keys in _DIALOGS:
                if marker in screen_text and marker not in self._answered:
                    self._answered.add(marker)
                    out.append(keys(screen_text) if callable(keys) else keys)
            if out:
                self._buf = b""
            if len(self._answered) == len(_DIALOGS):
                self._active = False
            return out
