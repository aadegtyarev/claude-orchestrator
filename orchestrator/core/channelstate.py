"""Состояние dev-канала сессии по её claude.log: живой | заблокирован | неясно.

Канал (`--dangerously-load-development-channels`) может НЕ подняться, хотя
сессия внешне совершенно жива. Наблюдалось 14-15.08.2026 на сессии ikar после
`/profile work`: учётка профиля принадлежит Team-оргу, где каналы выключены
орг-политикой, и Claude Code вместо канала рисует баннер «Channels are not
enabled for your org · have an administrator set channelsEnabled: true in
managed settings».

Коварство в том, что channel_server при этом стартует как ни в чём не бывало:
он подключается обычным `--mcp-config`, а не гейтом каналов. Значит `/ping`
отвечает 200 (готовность сессии считается достигнутой), MCP-handshake проходит,
`POST /notify` возвращает 200 — а push-уведомление уходит в никуда. Оркестратор
считал сообщение доставленным, оператор видел «сессия работает», и молчание
выглядело как «модель думает». Проверено вручную: notify 200 → дельта
claude.log = 0 байт.

Здесь — чистая функция «что говорит лог». Что с этим делать (заметка оператору +
доставка через PTY) решают SessionManager и ядро.

Матчим по тексту со СНЯТЫМИ пробелами: TUI переносит статус-строку и рвёт её
управляющими последовательностями, поэтому пробелы внутри баннера ненадёжны.
Берём два самых характерных куска каждого баннера и смотрим, чей ПОСЛЕДНИЙ
в просмотренном куске лога — лог накопительный, и в нём могут лежать кадры
предыдущего процесса сессии.
"""

from __future__ import annotations

import re

from .ansi import strip_ansi

OK = "ok"            # канал загружен: баннер «Channels (experimental) …»
BLOCKED = "blocked"  # канал отбит орг-политикой
UNKNOWN = "unknown"  # ни одного баннера не видно — не гадаем

# Сколько хвоста лога смотреть. Статус-строка перерисовывается на каждом кадре
# TUI, так что свежий баннер всегда близко к концу; окно нужно лишь чтобы не
# втягивать десятки МБ.
SCAN_BYTES = 256 * 1024

_WS_RE = re.compile(rb"\s+")
# Куски баннера «Channels are not enabled for your org · have an administrator
# set channelsEnabled: true in managed settings».
_BLOCKED_RE = re.compile(rb"notenabledforyourorg|channelsEnabled:true", re.IGNORECASE)
# Куски баннера «Channels (experimental) messages from server:channel-<имя>
# inject directly in this session · restart without …».
_OK_RE = re.compile(rb"Channels\(experimental\)|injectdirectlyinthissession", re.IGNORECASE)


def _last(pattern: re.Pattern[bytes], text: bytes) -> int:
    """Позиция последнего совпадения или -1."""
    pos = -1
    for m in pattern.finditer(text):
        pos = m.start()
    return pos


def channel_state(raw: bytes) -> str:
    """OK | BLOCKED | UNKNOWN по куску claude.log (сырые байты с ANSI)."""
    text = _WS_RE.sub(b"", strip_ansi(raw))
    blocked = _last(_BLOCKED_RE, text)
    ok = _last(_OK_RE, text)
    if blocked < 0 and ok < 0:
        return UNKNOWN
    return BLOCKED if blocked > ok else OK
