"""Хук-диспетчер Claude Code: шаблон скрипта и его рендер.

Диспетчер хуков (Stop + PreToolUse + PostToolUse + SubagentStop + PreCompact)
как отдельный python-скрипт (а не curl с токеном в аргументах). Токен встроен
константой в этот 0600-файл — НЕ в cmdline (иначе виден в /proc/<pid>/cmdline)
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

PreCompact — единственное событие, которое НЕ ходит по сети, а печатает: его
stdout Claude Code дописывает в промпт суммаризатора (см. originprompt.py,
COMPACT_TRUST_INSTRUCTION — зачем это нужно). Текст зашит в скрипт, а не
запрашивается у оркестратора, ровно потому, что остальной диспетчер —
fire-and-forget: недоступный оркестратор там теряет бабл, а здесь потерял бы
саму инструкцию, и сжатие молча снова записало бы отказ как факт. По той же
причине POST на PreCompact не делаем: у события нет tool_name, ядро отрисовало
бы пустой бабл (handle_tool_event роутит всё неизвестное в PreToolUse).

__ORCH__/__NAME__/__TOKEN__/__COMPACT__ подставляются ОДНИМ проходом re.sub и
каждое значение — через json.dumps (готовым python-литералом), а не .format:
так безопасно любое значение токена, имени и текста.

Один проход и экранирование — не украшение, а два разных лекарства, оба
найдены ревью этого среза. Цепочка .replace() шла по УЖЕ СОБРАННОЙ строке, и
подставленное значение снова попадало под следующие замены: имя сессии
доезжает внутрь текста инструкции (compact_trust_instruction), поэтому сессия
с именем `__TOKEN__` печатала бы боевой ORCH_TOKEN в промпт суммаризатора — то
есть в контекст модели и в саммари. json.dumps закрывает вторую дыру: голая
подстановка ломала скрипт синтаксически, стоило токену из .env содержать
кавычку или обратный слэш (у сгенерированного token_urlsafe их нет — молчало).

__ORCH__ — адрес оркестратора с точки зрения СЕССИИ: 127.0.0.1 под bwrap/off
(общий loopback), host-gateway IP под agent-vm (гость VM не видит хостовый
loopback).
"""

from __future__ import annotations

import json
import re

from orchestrator.core.originprompt import compact_trust_instruction

HOOK_SCRIPT = '''#!/usr/bin/env python3
"""Хук-диспетчер Claude Code (Stop + Pre/Post-ToolUse + SubagentStop) → POST
оркестратору; PreCompact → инструкция суммаризатору в stdout.

Токен встроен константой сюда (файл 0600), НЕ в cmdline/настройки — иначе
ORCH_TOKEN виден ДРУГОМУ локальному процессу через /proc/<pid>/cmdline
(REVIEW.md S1). От самой модели (тот же uid, RW-доступ) не прячет — принято.
Читает событие из stdin, всегда выходит 0 — хук не должен блокировать Claude
(на PreCompact ненулевой код ещё и отменил бы сжатие)."""
import json
import sys
import urllib.request

_ORCH = __ORCH__
_NAME = __NAME__
_TOKEN = __TOKEN__
_COMPACT = __COMPACT__


def main():
    try:
        raw = sys.stdin.read()
        try:
            event = json.loads(raw).get("hook_event_name", "")
        except ValueError:
            event = ""
        if event == "PreCompact":
            # stdout уезжает в промпт суммаризатора; сети тут нет намеренно —
            # инструкция не должна зависеть от того, жив ли оркестратор.
            sys.stdout.write(_COMPACT)
            return
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


_PLACEHOLDER_RE = re.compile(r"__(ORCH|NAME|TOKEN|COMPACT)__")


def render(host: str, port: int, name: str, token: str) -> str:
    """Скрипт хука с подставленными адресом оркестратора (host:port), именем
    сессии, токеном и текстом PreCompact-инструкции. `host` — 127.0.0.1 под
    bwrap/off, host-gateway под agent-vm."""
    values = {
        "ORCH": f"http://{host}:{port}",
        "NAME": name,
        "TOKEN": token,
        "COMPACT": compact_trust_instruction(name),
    }
    # Один проход: re.sub НЕ перечитывает подставленное, поэтому значение,
    # похожее на плейсхолдер (сессия по имени `__TOKEN__`), остаётся собой и не
    # вытягивает в текст чужой секрет. json.dumps — чтобы кавычка/слэш/перевод
    # строки в любом из значений остались данными, а не сломали скрипт.
    return _PLACEHOLDER_RE.sub(lambda m: json.dumps(values[m.group(1)]), HOOK_SCRIPT)
