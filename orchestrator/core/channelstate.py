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

ГЛАВНАЯ ловушка этого детектора — лог хранит не только рамку TUI, но и ВСЮ
беседу. Стоит модели или оператору упомянуть баннер (а в этом репозитории он
описан в docs/, и я сам вписал его туда при разработке) — цитата в логе
неотличима от настоящего баннера. На живом проде это и случилось: сессия
claude-orchestrator с исправным каналом получила вердикт «заблокирован», потому
что несколькими экранами выше обсуждала текст баннера. Отсюда две защиты:

  • матчим фразу ЦЕЛИКОМ, а не куски: «Channels are not enabled for your org»
    рядом с «channelsEnabled: true in managed settings». Обрывок вроде
    «channelsEnabled: true» встречается в разговоре сплошь и рядом, полная
    формулировка — практически нет;
  • смотрим ОКНО СТАРТА процесса, а не хвост лога. Баннер — стартовый артефакт,
    он печатается сразу за приветственной рамкой; беседа же наматывается
    мегабайтами позже (в проде цитата лежала в 4.5 МБ от баннера).

Матчим по тексту со снятыми пробелами: TUI переносит статус-строку и рвёт её
управляющими последовательностями, так что пробелы внутри баннера ненадёжны.
"""

from __future__ import annotations

import re

from .ansi import strip_ansi

OK = "ok"            # канал загружен: баннер «Channels (experimental) …»
BLOCKED = "blocked"  # канал отбит орг-политикой
UNKNOWN = "unknown"  # ни одного баннера не видно — не гадаем

# Сколько байт от начала вывода процесса считать «окном старта». Баннер идёт
# сразу за приветственной рамкой; запас нужен на то, что рамка целиком состоит
# из управляющих последовательностей и весит куда больше, чем выглядит.
SCAN_BYTES = 256 * 1024

_WS_RE = re.compile(rb"\s+")
# Баннер «Channels are not enabled for your org · have an administrator set
# channelsEnabled: true in managed settings» — обе половины подряд. Между ними
# TUI ставит разделитель (·), иногда с переносом, поэтому щель до 8 символов.
_BLOCKED_RE = re.compile(
    rb"Channelsarenotenabledforyourorg.{0,8}"
    rb"haveanadministratorsetchannelsEnabled:trueinmanagedsettings",
    re.IGNORECASE | re.DOTALL,
)
# Баннер «Channels (experimental) messages from server:channel-<имя> inject
# directly in this session · restart without …».
_OK_RE = re.compile(
    rb"Channels\(experimental\)messagesfromserver:channel-[\w.-]+"
    rb"injectdirectlyinthissession",
    re.IGNORECASE | re.DOTALL,
)


def _last(pattern: re.Pattern[bytes], text: bytes) -> int:
    """Позиция последнего совпадения или -1."""
    pos = -1
    for m in pattern.finditer(text):
        pos = m.start()
    return pos


def channel_state(raw: bytes) -> str:
    """OK | BLOCKED | UNKNOWN по окну старта claude.log (сырые байты с ANSI).

    `raw` — вывод С НАЧАЛА текущего процесса (см. SCAN_BYTES): вызывающий
    обязан отрезать предыдущие запуски, иначе вердикт будет про чужой процесс.
    """
    text = _WS_RE.sub(b"", strip_ansi(raw))
    blocked = _last(_BLOCKED_RE, text)
    ok = _last(_OK_RE, text)
    if blocked < 0 and ok < 0:
        return UNKNOWN
    return BLOCKED if blocked > ok else OK
