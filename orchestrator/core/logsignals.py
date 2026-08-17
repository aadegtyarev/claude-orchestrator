"""Разбор «живых сигналов» из claude.log для ретранслятора ошибок API.

Чистые функции над байтами: триггер — только настоящий баннер TUI
«API Error: <код>», классификация (ratelimit/protocol/generic), ретраи
«attempt K/M» и баннеры краш-рестарта. Тестируется без петли и Telegram
(error_relay_test). Вынесено из bot.py (REVIEW.md D1).
"""

from __future__ import annotations

import re

# Триггер — ТОЛЬКО настоящий баннер «API Error: <код> <детали>». Модель охотно
# пишет слова «rate-limit»/«api error» в ответах, и широкий греп ловил прозу как
# ложный алерт. Баннер с кодом модель дословно не цитирует — это надёжный сигнал.
# group(1)=код, group(2)=хвост строки с деталями (для класса).
API_ERR_BANNER_RE = re.compile(rb"API Error:\s*(\d{3})\b([^\n]{0,140})", re.IGNORECASE)
# Лимит-баннер БЕЗ 3-значного HTTP-кода: «API Error: Server is temporarily
# limiting requests … [1310][Weekly/Monthly Limit Exhausted. Your limit will
# reset at <дата>]». HTTP-код тут не 3-значный (1310 в скобках), поэтому
# основной регекс его не ловил — а именно на нём noos часами «молчал».
_QUOTA_BANNER_RE = re.compile(
    rb"API Error:[^\n]*?(?:temporarily limiting requests|Limit Exhausted)"
    rb"[^\n]{0,160}",
    re.IGNORECASE,
)
_QUOTA_RESET_RE = re.compile(rb"reset at\s*([0-9:\- ]{10,25})", re.IGNORECASE)
# Класс ошибки по деталям баннера (код разбираем отдельно).
_RL_DETAIL_RE = re.compile(rb"rate[\s_-]?limit|overloaded|\bcapacity\b", re.IGNORECASE)
_PROTO_DETAIL_RE = re.compile(rb"server_tool_use|tool_result|messages\.\d|thinking", re.IGNORECASE)
# Живые сигналы из claude.log (когда тулов нет, но что-то происходит):
#  • ретрай API-ошибки — «Retrying … attempt K/M»;
#  • краш-рестарт — баннер «Resume this session with» / «Welcome back».
RETRY_RE = re.compile(rb"attempt\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)
RESTART_RE = re.compile(rb"Resume this session with|Welcome back", re.IGNORECASE)
# «Живой пульс» из TUI-спиннера Claude Code: забавные глаголы + таймер, напр.
# «✻ Cogitating…», «✳ Quantumizing…» (сейчас думает) или «✻ Cogitated for 5m 57s»
# (только что думал). Показываем последний в бабле как признак «модель жива»,
# когда tool-событий нет. Эллипсис — юникодный (…, \xe2\x80\xa6) или три точки.
# Спиннер целиком несёт три живых сигнала: глагол, прошедшее время и счётчик
# выходных токенов, напр. «✻ Cogitating… (1m 23s · ↓ 340 tokens)» или
# «✻ Cogitated for 5m 57s». Собираем их в одну строку пульса.
# Активный глагол «…»: за ним в скобках часто время «(12s ·».
_VERB_ACTIVE_RE = re.compile(
    rb"\b([A-Z][a-z]{2,}ing)(?:\xe2\x80\xa6|\.{3})(?:\s*\((\d+[hms][\dhms ]*?)[ )\xc2\xb7])?"
)
# Завершённый «Verb for <time>».
_VERB_DONE_RE = re.compile(rb"\b([A-Z][a-z]{2,}(?:ed|ing))\s+for\s+(\d[\dhms ]*[hms])")
# Свежайший блок статистики спиннера «(<время> · ↓ <токены> tokens)» —
# оба живых числа разом. Токены реально с суффиксом «k/m» («↓2.6k tokens»,
# «↓1.8k tokens»); прежний `\d+ tokens` ломался о «k» и при >1000 токенов
# (почти всегда во время генерации) счётчик пропадал. Держим строкой как есть
# («2.6k»). Тащим отдельным regex, т.к. строка глагола часто перебита анимацией
# спиннера («Deciphering…94✶1305…») и время рядом с ней теряется — а этот блок
# TUI перерисовывает целиком, из него берём тикающее время для «живости».
_STATS_RE = re.compile(
    rb"\((\d+\s*[hms](?:\s*\d+\s*[hms])*)\s*\xc2\xb7"       # (<время>·
    rb"\s*\xe2\x86\x93\s*(\d[\d.]*[kmKM]?)\s*tokens",       #  ↓<токены> tokens
)
# Компакция контекста: Claude Code сжимает историю («Compacting conversation…»
# в TUI), ввод заблокирован и глаголов-спиннера нет — для человека это такой же
# «тупняк», как долгий ход. Показываем явным пульсом, чтобы было видно ЧТО идёт.
_COMPACTING_RE = re.compile(rb"Compacting conversation", re.IGNORECASE)
# Протухшая учётка Claude Code — самый тихий вид поломки: сессия жива, но на
# КАЖДОЕ сообщение отвечает сама за 0 с баннером «Login expired · Please run
# /login» (варианты: «Not logged in · Run /login», «Invalid API key · Please
# run /login»), без тулов и без reply_to_user. Каналу ретранслировать нечего,
# вотчдогу нечего ловить (лог растёт, процесс жив) — в чат не приходит ничего,
# сообщения оператора съедаются молча (живой случай: сессия noos 2026-08-17).
#
# Матчим по «экрану без пробелов» (тот же приём, что в box.dialog): TUI
# позиционирует курсор, и после снятия ANSI пробелы между словами пропадают —
# в логе noos банер выглядит как «●Loginexpired·Pleaserun/login». Требуем
# баннер ЦЕЛИКОМ, вместе с «·» и «/login»: проза модели такого не даёт, а
# окончательный гейт от ложной тревоги — `claude auth status` (см. turn.py).
_LOGIN_BANNER_RE = re.compile(
    rb"(?:loginexpired|notloggedin|invalidapikey)\xc2\xb7(?:please)?run/login",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(rb"\s+")


def classify_api_error(code: bytes, detail: bytes) -> str:
    """Класс ошибки API: ratelimit | protocol | generic.

    ratelimit — 429/529/overloaded: транзитно, помогает смена модели.
    protocol — 400 с кривым server_tool_use/tool_result/thinking: апстрим (z.ai)
              шлёт несогласованный блок, модель тут ни при чём — /clear или
              /close_session.
    generic  — прочее (5xx и т.п.).
    """
    if code in (b"429", b"529") or _RL_DETAIL_RE.search(detail):
        return "ratelimit"
    if code == b"400" and _PROTO_DETAIL_RE.search(detail):
        return "protocol"
    return "generic"


def has_login_banner(chunk: bytes) -> bool:
    """Есть ли в куске баннер протухшей учётки («Login expired · … /login»).

    Пробелы выкидываем перед матчем: TUI рисует строку позиционированием
    курсора, и после strip_ansi слова слипаются непредсказуемо.
    """
    return bool(_LOGIN_BANNER_RE.search(_SPACE_RE.sub(b"", chunk)))


def detect_log_signals(chunk: bytes) -> dict:
    """Разобрать кусок claude.log на классы сигналов.

    Возвращает {api_error, retry, restarts, pulse, quota, tokens, login}:
      • api_error — (code, klass) баннера «API Error: <код>» либо None;
      • retry — (attempt, total) из «attempt K/M», последний в куске, либо None;
      • restarts — сколько баннеров рестарта;
      • login — виден баннер протухшей учётки (подтверждать `auth status`).
    """
    out: dict = {
        "api_error": None, "retry": None, "restarts": 0, "pulse": None, "quota": None,
        "login": has_login_banner(chunk),
    }
    m = API_ERR_BANNER_RE.search(chunk)
    if m:
        out["api_error"] = (m.group(1), classify_api_error(m.group(1), m.group(2)))
    # Лимит-баннер без 3-значного кода (Weekly/Monthly exhausted и т.п.).
    qm = _QUOTA_BANNER_RE.search(chunk)
    if qm:
        reset = _QUOTA_RESET_RE.search(qm.group(0))
        out["quota"] = (
            b" ".join(reset.group(1).split()).decode("ascii", "replace") if reset else ""
        )
    rm = RETRY_RE.search(chunk)
    if rm:
        out["retry"] = (int(rm.group(1)), int(rm.group(2)))
    out["restarts"] = len(RESTART_RE.findall(chunk))
    # Свежайший блок «(<время> · ↓ <токены>)»: тикающее время и счётчик токенов
    # (строкой, с суффиксом «2.6k»). Оба — самый надёжный признак «модель жива».
    stats = _STATS_RE.findall(chunk)
    if stats:
        live_elapsed, tok = stats[-1]
        out["tokens"] = tok.decode("ascii", "replace")
    else:
        live_elapsed = None
        out["tokens"] = None
    out["pulse"] = _extract_pulse(chunk, live_elapsed)
    return out


def _extract_pulse(chunk: bytes, live_elapsed: bytes | None = None) -> str | None:
    """Последний спиннер-глагол Claude Code (+время, если рядом) из куска лога
    → строка для бабла (напр. «Cogitating · 12s» или «Cogitated · 5m 57s»)
    либо None. live_elapsed — свежайшее время из блока статистики (тикает даже
    когда строка глагола перебита анимацией спиннера); приоритетнее inline-времени.

    Всё это — из TUI-вывода в claude.log (в транскрипте спиннера нет). Приоритет
    активному «-ing…» (думает сейчас); иначе последний завершённый «… for <time>».
    Парсинг TUI хрупок: изменится рендер — вернётся None, бабл просто без пульса.
    """
    active = list(_VERB_ACTIVE_RE.finditer(chunk))
    if active:
        verb = active[-1].group(1).decode("ascii", "replace")
        elapsed = live_elapsed or active[-1].group(2)  # свежее время > inline
        if elapsed:
            dur = b" ".join(elapsed.split()).decode("ascii", "replace")
            return f"{verb} · {dur}"
        return verb
    # Компакция — блокирующее состояние без спиннера; выше «завершённого» глагола
    # (после «Cogitated for 3s» модель могла уйти в сжатие — оно и есть текущее).
    if _COMPACTING_RE.search(chunk):
        return "Сжимаю контекст"
    done = list(_VERB_DONE_RE.finditer(chunk))
    if done:
        verb = done[-1].group(1).decode("ascii", "replace")
        dur = b" ".join(done[-1].group(2).split()).decode("ascii", "replace")
        return f"{verb} · {dur}"
    return None
