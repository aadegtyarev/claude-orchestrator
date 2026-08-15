"""Рендер сообщений: один текст — один проход, и ни одного артефакта на выходе.

Было два перемешанных соглашения. Часть текстов ядра размечена HTML (диалект
Telegram), часть — markdown; часть ответов Telegram отправлял без `parse_mode`,
а веб гнал текст ядра через `md_to_html`, который экранирует чужие теги. Оба
раза это ловилось глазами оператора, а не тестом: сперва `/info` и `/profile`
показали буквальное «<code>», потом массовая правка отняла `parse_mode` у бабла
и permission-запроса — и сырой HTML увидели уже там.

Отсюда устройство, которое здесь и проверяется:
  • разметку выбирает ровно одна функция на адаптер (`TelegramAdapter._render`,
    `WebAdapter._payload`);
  • наружу текст уходит ровно через один выход (`_deliver`/`_send`), поэтому
    «забыть parse_mode» негде;
  • тексты ЯДРА (`texts.py`, `*_text()`, бабл, permission) — HTML, тексты
    МОДЕЛИ — markdown.

Тест устроен так же просто: прогоняем КАЖДЫЙ текст через настоящий путь
отправки и смотрим, нет ли в том, что реально уйдёт, артефактов — экранированных
тегов, двойных сущностей, недорендеренного markdown.

Запуск: .venv/bin/python tests/textrender_test.py
"""

from __future__ import annotations

import ast
import asyncio
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.texts import MESSAGES  # noqa: E402

ADAPTER = Path(__file__).resolve().parent.parent / "orchestrator/adapters/telegram/adapter.py"
# Разметка, которую понимает Telegram (и мы в текстах ядра).
ALLOWED_TAG = re.compile(r'</?(?:b|strong|i|em|s|u|code|pre|blockquote)>|<a href="[^"]*">|</a>')
MARKDOWN = re.compile(r"`[^`]+`|\*\*[^*]+\*\*")
# Артефакт = то, что оператор увидит вместо разметки.
ARTIFACTS = {
    "экранированный тег": re.compile(r"&lt;/?(?:b|i|s|u|code|pre|a)\b"),
    "двойное экранирование": re.compile(r"&amp;(?:lt|gt|amp|quot|#\d+);"),
    "недорендеренный markdown": MARKDOWN,
}


def artifacts(rendered: str) -> list[str]:
    """Что оператор увидит не так. Пусто — рендер чистый."""
    return [name for name, pattern in ARTIFACTS.items() if pattern.search(rendered)]


def core_texts():
    for lang, messages in MESSAGES.items():
        for key, text in messages.items():
            if isinstance(text, str):
                yield lang, key, text


# ── тексты ядра как таковые ──────────────────────────────────────────────────
def test_no_raw_angle_brackets():
    """Сырой «<» в тексте ядра = отвергнутое Telegram сообщение.

    Литеральные скобки пишем сущностями (&lt;имя&gt;) — так текст остаётся
    валидным HTML и читается одинаково в чате и в вебе.
    """
    bad = [(lang, key) for lang, key, text in core_texts()
           if set("<>") & set(ALLOWED_TAG.sub("", text))]
    assert not bad, f"сырые скобки в текстах: {bad}"


def test_languages_have_same_keys():
    ru, en = set(MESSAGES["ru"]), set(MESSAGES["en"])
    assert ru == en, f"только ru: {sorted(ru - en)}; только en: {sorted(en - ru)}"


def test_plain_unwraps_markup_and_entities():
    plain = OrchestratorCore.plain
    assert plain("<b>ikar</b> → <code>~/.claude</code>") == "ikar → ~/.claude"
    assert plain("/profile &lt;имя&gt;") == "/profile <имя>"
    assert plain("<pre>a &amp; b</pre>") == "a & b"


# ── Telegram: прогон каждого текста через настоящий выход ────────────────────
def make_adapter():
    """Адаптер с фейковым Bot API: собираем то, что реально ушло бы в чат."""
    from orchestrator.adapters.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    sent: list[dict] = []

    async def api(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id=7)

    adapter.bot = SimpleNamespace(send_message=api, edit_message_text=api)
    adapter.chat_id = 1
    adapter._thread_of = lambda session: 42
    adapter._stop_markup = lambda thread_id, unblock_active=False: None
    return adapter, sent, api


def test_every_core_text_renders_clean():
    """Каждый текст ядра, пройдя выход наружу, уходит без артефактов."""
    adapter, sent, api = make_adapter()
    for lang, key, text in core_texts():
        sent.clear()
        asyncio.run(adapter._deliver(api, text))
        assert len(sent) == 1, (lang, key, sent)
        assert sent[0]["parse_mode"] == "HTML", (lang, key)
        assert sent[0]["text"] == text, f"{lang}/{key}: текст ядра переписан рендером"
        found = artifacts(sent[0]["text"])
        assert not found, f"{lang}/{key}: {found}"


def test_model_text_renders_markdown():
    """Ответ модели — markdown: кавычки становятся тегами, «<» экранируется."""
    adapter, sent, api = make_adapter()
    asyncio.run(adapter._deliver(api, "запусти `ruff check .`, если a < b", core=False))
    out = sent[0]["text"]
    assert "<code>ruff check .</code>" in out, out
    assert "&lt;" in out and not artifacts(out), out


def test_markup_failure_falls_back_to_plain():
    """Разметку отвергли — спасаем текст, а не молчим."""
    adapter, _, _ = make_adapter()
    attempts: list[dict] = []

    async def picky(**kwargs):
        attempts.append(kwargs)
        if "parse_mode" in kwargs:
            raise RuntimeError("Bad Request: can't parse entities")
        return SimpleNamespace(message_id=7)

    asyncio.run(adapter._deliver(picky, "битый <тег"))
    assert len(attempts) == 2 and "parse_mode" not in attempts[1]


def test_other_errors_are_not_swallowed():
    """«Сообщение не изменилось» — НЕ повод переслать бабл сырым текстом."""
    adapter, _, _ = make_adapter()

    async def not_modified(**kwargs):
        raise RuntimeError("Bad Request: message is not modified")

    try:
        asyncio.run(adapter._deliver(not_modified, "<b>бабл</b>"))
    except RuntimeError:
        return
    raise AssertionError("ошибка не про разметку должна всплыть наверх")


def test_bubble_goes_out_as_html():
    """Бабл — готовый HTML ядра: уходит с разметкой и без второго рендера."""
    adapter, sent, _ = make_adapter()
    session = SimpleNamespace(name="s")
    body = "⏳ Работаю…\n<code>ruff check .</code>"
    ref = asyncio.run(adapter.bubble_post(session, body, stop_button=True))
    asyncio.run(adapter.bubble_edit(session, ref, body + "!", stop_button=True))
    assert len(sent) == 2, sent
    for kwargs in sent:
        assert kwargs["parse_mode"] == "HTML", kwargs
        assert "<code>" in kwargs["text"] and not artifacts(kwargs["text"])


# ── веб: тот же текст, тот же выбор рендера ──────────────────────────────────
def make_web():
    from orchestrator.adapters.web.adapter import WebAdapter
    web = WebAdapter.__new__(WebAdapter)
    web.core = SimpleNamespace(plain=OrchestratorCore.plain)
    return web


def test_web_renders_every_core_text_clean():
    """В вебе текст ядра идёт разметкой как есть, а text — он же без тегов."""
    web = make_web()
    for lang, key, text in core_texts():
        payload = web._payload(text)
        assert payload["html"] == text, f"{lang}/{key}: текст ядра переписан"
        found = artifacts(payload["html"])
        assert not found, f"{lang}/{key}: {found}"
        # В plain-версии тегов быть не должно, а литеральные скобки — можно:
        # «/profile <имя>» именно так и читается без разметки.
        assert not ALLOWED_TAG.search(payload["text"]), \
            f"{lang}/{key}: тег остался в plain-версии"


def test_web_renders_model_text_as_markdown():
    web = make_web()
    payload = web._payload("запусти `ruff check .`", core=False)
    assert "<code>ruff check .</code>" in payload["html"]
    assert not artifacts(payload["html"])


# ── устройство: другого выхода наружу быть не должно ─────────────────────────
def test_single_way_out():
    """Прямые вызовы Bot API живут только в _send/_deliver.

    Именно этот инвариант ловит регрессию, которая уже случалась: массовая
    правка сняла parse_mode с бабла и permission-запроса, и сырой HTML ушёл в
    чат. Пока весь выход в двух функциях, «забыть разметку» негде.
    """
    tree = ast.parse(ADAPTER.read_text(encoding="utf-8"))
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                owner[id(node)] = fn.name
    api = {"send_message", "edit_message_text", "reply", "answer", "edit_text"}
    outside = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in api:
            continue
        receiver = ast.unparse(node.func.value)
        if receiver.startswith("callback") and node.func.attr == "answer":
            continue  # всплывающий тост, а не сообщение
        if "text" not in {k.arg for k in node.keywords} and not node.args:
            continue
        where = owner.get(id(node), "?")
        if where not in ("_send", "_deliver"):
            outside.append((node.lineno, receiver + "." + node.func.attr, where))
    assert not outside, f"отправка мимо единого выхода: {outside}"


def main() -> None:
    test_no_raw_angle_brackets()
    test_languages_have_same_keys()
    test_plain_unwraps_markup_and_entities()
    test_every_core_text_renders_clean()
    test_model_text_renders_markdown()
    test_markup_failure_falls_back_to_plain()
    test_other_errors_are_not_swallowed()
    test_bubble_goes_out_as_html()
    test_web_renders_every_core_text_clean()
    test_web_renders_model_text_as_markdown()
    test_single_way_out()
    print("ALL TEXT-RENDER OK")


if __name__ == "__main__":
    main()
