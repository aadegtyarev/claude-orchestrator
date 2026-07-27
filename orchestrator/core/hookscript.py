"""Хук-диспетчер Claude Code: шаблон скрипта и его рендер.

Диспетчер хуков (Stop + PreToolUse + PostToolUse + SubagentStop) как отдельный python-скрипт (а не curl с
токеном в аргументах). Токен встроен константой в этот 0600-файл — НЕ в
cmdline (иначе виден в /proc/<pid>/cmdline любому локальному пользователю)
и НЕ в settings.local.json (0644). Раньше curl -H 'Authorization: Bearer …'
тёк в оба места (REVIEW S1, найдено адверсариальным ревью).

ВАЖНО про модель угроз: 0600 прячет ORCH_TOKEN от ДРУГИХ unix-пользователей,
но НЕ от самой модели — она бежит под тем же uid и session_dir примонтирован
ей RW, так что `cat hook_dispatch.py` ей доступен. Считаем ORCH_TOKEN
известным модели: под нашей рамкой (страховки от случайных глупостей, не
изоляция от враждебной/заинъекченной модели) это принято — см.
docs/archive/REVIEW-2026-07-19.md §1. С токеном можно POST /permission/<имя>
(само-аппрув), /reply, /event — от НАМЕРЕННОГО злоупотребления это не защита.

Stop-хук — fallback против «потерянного финала» (REVIEW: модель нередко
завершает длинный ход обычным текстом вместо tool-вызова reply_to_user;
канал ретранслирует только явные tool-call'ы, голый текст остаётся в
транскрипте и не долетает до Telegram — 9/9 длинных ходов в живой сессии).
hook_event_name различает событие: PreToolUse → POST /event/<имя> (бабл),
Stop → POST /stop/<имя> с last_assistant_message (боту решать, нужен ли
fallback — см. core/app.py handle_stop_event).

__HOST__/__PORT__/__NAME__/__TOKEN__ подставляются обычным replace (без .format-
скобок, чтобы безопасно для любого значения токена). __HOST__ — адрес
оркестратора с точки зрения СЕССИИ: 127.0.0.1 под bwrap/off (общий loopback),
host-gateway IP под agent-vm (гость VM не видит хостовый loopback).
"""

from __future__ import annotations

HOOK_SCRIPT = '''#!/usr/bin/env python3
"""Хук-диспетчер Claude Code (Stop + Pre/Post-ToolUse + SubagentStop) → POST оркестратору.

Токен встроен константой сюда (файл 0600), НЕ в cmdline/настройки — иначе
ORCH_TOKEN виден ДРУГОМУ локальному процессу через /proc/<pid>/cmdline
(REVIEW.md S1). От самой модели (тот же uid, RW-доступ) не прячет — принято.
Читает событие из stdin, всегда выходит 0 — хук не должен блокировать Claude."""
import json
import sys
import urllib.request

_ORCH = "http://__HOST__:__PORT__"
_NAME = "__NAME__"
_TOKEN = "__TOKEN__"


def main():
    try:
        raw = sys.stdin.read()
        try:
            event = json.loads(raw).get("hook_event_name", "")
        except ValueError:
            event = ""
        path = "/stop/" + _NAME if event == "Stop" else "/event/" + _NAME
        req = urllib.request.Request(
            _ORCH + path,
            data=raw.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + _TOKEN,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


main()
sys.exit(0)
'''


def render(host: str, port: int, name: str, token: str) -> str:
    """Скрипт хука с подставленными адресом оркестратора (host:port), именем
    сессии и токеном. `host` — 127.0.0.1 под bwrap/off, host-gateway под agent-vm."""
    return (
        HOOK_SCRIPT
        .replace("__HOST__", host)
        .replace("__PORT__", str(port))
        .replace("__NAME__", name)
        .replace("__TOKEN__", token)
    )
