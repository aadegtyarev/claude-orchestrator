"""Единый рендер текстов: у ядра один диалект разметки, у каждого адаптера — один проход.

Было два перемешанных соглашения. Часть текстов ядра размечена HTML
(`<code>`, `<b>` — диалект Telegram), часть — markdown (обратные кавычки);
часть ответов Telegram уходила вообще без `parse_mode`, а веб гнал текст ядра
через `md_to_html`, который экранирует чужие теги. Итог был виден глазами:
`/info` и `/profile` показывали оператору буквальное «<code>».

Соглашение теперь одно:
  • тексты ЯДРА (texts.py и *_text()) — HTML телеграм-диалекта;
  • тексты МОДЕЛИ — markdown, их рендерит md_to_html;
  • адаптер выбирает путь явно (`_send(core=True)`, `notify` → html как есть),
    и ни один текст не проходит через рендер дважды.

Что проверяем:
  • ни один текст ядра не содержит сырых «<»/«>» вне разрешённых тегов —
    иначе Telegram отвергнет сообщение и оператор увидит его без разметки;
  • ни один текст ядра не размечен markdown — это соглашение модели, не ядра;
  • наборы ключей ru/en совпадают (иначе на другом языке будет KeyError);
  • plain() снимает разметку и разворачивает сущности;
  • _send(core=True) НЕ гоняет текст через md_to_html, а core=False — гоняет.

Запуск: .venv/bin/python tests/textrender_test.py
"""

from __future__ import annotations

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

# Разметка, которую понимает Telegram (и мы в текстах ядра).
ALLOWED_TAG = re.compile(r'</?(?:b|strong|i|em|s|u|code|pre|blockquote)>|<a href="[^"]*">|</a>')
MARKDOWN = re.compile(r"`[^`]+`|\*\*[^*]+\*\*")


def texts():
    for lang, messages in MESSAGES.items():
        for key, text in messages.items():
            if isinstance(text, str):
                yield lang, key, text


def test_no_raw_angle_brackets():
    """Сырой «<» в тексте ядра = отвергнутое Telegram сообщение.

    Литеральные скобки пишем сущностями (&lt;имя&gt;) — так текст остаётся
    валидным HTML и читается одинаково в чате и в вебе.
    """
    bad = [(lang, key) for lang, key, text in texts()
           if set("<>") & set(ALLOWED_TAG.sub("", text))]
    assert not bad, f"сырые скобки в текстах: {bad}"


def test_no_markdown_in_core_texts():
    """Markdown — диалект МОДЕЛИ. В ядре он не отрендерится и покажется как есть."""
    bad = [(lang, key) for lang, key, text in texts() if MARKDOWN.search(text)]
    assert not bad, f"markdown в текстах ядра: {bad}"


def test_languages_have_same_keys():
    ru, en = set(MESSAGES["ru"]), set(MESSAGES["en"])
    assert ru == en, f"только ru: {sorted(ru - en)}; только en: {sorted(en - ru)}"


def test_plain_unwraps_markup_and_entities():
    plain = OrchestratorCore.plain
    assert plain("<b>ikar</b> → <code>~/.claude</code>") == "ikar → ~/.claude"
    assert plain("/profile &lt;имя&gt;") == "/profile <имя>"
    assert plain("<pre>a &amp; b</pre>") == "a & b"


# ── Telegram: один текст — один рендер ───────────────────────────────────────
def make_adapter():
    from orchestrator.adapters.telegram.adapter import TelegramAdapter
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    sent: list[str] = []

    async def send_message(text: str, **kwargs) -> None:
        sent.append(text)

    adapter.bot = SimpleNamespace(send_message=send_message)
    return adapter, sent


def test_core_text_is_not_rendered_twice():
    """Текст ядра уходит как есть: md_to_html съел бы его собственные теги."""
    adapter, sent = make_adapter()
    text = "Учётка: <code>~/.claude</code>"
    asyncio.run(adapter._send(1, None, text, core=True))
    assert sent == [text], sent


def test_model_text_is_rendered_from_markdown():
    """Ответ модели — markdown: обратные кавычки обязаны стать <code>."""
    adapter, sent = make_adapter()
    asyncio.run(adapter._send(1, None, "запусти `ruff check .`"))
    assert "<code>ruff check .</code>" in sent[0], sent


def test_model_text_is_escaped():
    """И при этом «<» из вывода модели экранируется, а не ломает разметку."""
    adapter, sent = make_adapter()
    asyncio.run(adapter._send(1, None, "если a < b"))
    assert "&lt;" in sent[0], sent


def main() -> None:
    test_no_raw_angle_brackets()
    test_no_markdown_in_core_texts()
    test_languages_have_same_keys()
    test_plain_unwraps_markup_and_entities()
    test_core_text_is_not_rendered_twice()
    test_model_text_is_rendered_from_markdown()
    test_model_text_is_escaped()
    print("ALL TEXT-RENDER OK")


if __name__ == "__main__":
    main()
