"""Липкий режим терминала: обычные сообщения топика уходят в шелл.

Зачем: /bash перед каждой командой — это не работа в терминале, а диктовка
команд по одной. `/term on` делает топик терминалом до `/term off`.

Инварианты, которые тут проверяются:
  • режим включается ТОЛЬКО в топике сессии (в главном чате шелл идёт по хосту
    с правами оператора — случайно отправленное сообщение выполнилось бы там);
  • строка, начинающаяся с `>`, всё равно уходит claude — режим не запирает
    топик;
  • состояние живёт по тому же ключу, что и сама оболочка, и умирает вместе
    с сессией (иначе новая сессия с тем же именем унаследовала бы режим).

Запуск: .venv/bin/python tests/terminal_mode_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.termmode import TerminalMode  # noqa: E402


def test_off_by_default():
    tm = TerminalMode()
    assert not tm.is_on("s:proj:tg1")


def test_on_off_roundtrip():
    tm = TerminalMode()
    tm.turn_on("s:proj:tg1")
    assert tm.is_on("s:proj:tg1")
    tm.turn_off("s:proj:tg1")
    assert not tm.is_on("s:proj:tg1")


def test_modes_are_per_key():
    """Режим в одном топике не включает его в соседнем."""
    tm = TerminalMode()
    tm.turn_on("s:proj:tg1")
    assert not tm.is_on("s:proj:tg2")
    assert not tm.is_on("s:other:tg1")


def test_forget_session_drops_its_modes():
    """Сессия удалена — режим не должен воскреснуть у тёзки.

    Ключи оболочек сессии начинаются с "s:<имя>:", их и снимаем.
    """
    tm = TerminalMode()
    tm.turn_on("s:proj:tg1")
    tm.turn_on("s:proj:tg2")
    tm.turn_on("s:other:tg1")
    tm.forget_session("proj")
    assert not tm.is_on("s:proj:tg1")
    assert not tm.is_on("s:proj:tg2")
    assert tm.is_on("s:other:tg1")  # соседнюю не тронули


def test_forget_session_does_not_match_name_prefix():
    """`proj` не должен снести режимы сессии `project` (префикс — не имя)."""
    tm = TerminalMode()
    tm.turn_on("s:project:tg1")
    tm.forget_session("proj")
    assert tm.is_on("s:project:tg1")


# ── разбор строки в липком режиме ────────────────────────────────────


def test_escape_prefix_routes_to_claude():
    """`>` в начале — сообщение claude, даже когда терминал липкий."""
    assert TerminalMode.split_escape("> почини тест") == ("claude", "почини тест")
    assert TerminalMode.split_escape(">почини") == ("claude", "почини")


def test_plain_text_routes_to_shell():
    assert TerminalMode.split_escape("ls -la") == ("shell", "ls -la")


def test_escape_of_escape_goes_to_shell():
    """`>>` — способ отправить в шелл строку, которая сама начинается с `>`.

    Без этого нельзя было бы написать `> file` (перенаправление вывода).
    """
    assert TerminalMode.split_escape(">> out.txt") == ("shell", "> out.txt")


def test_empty_escape_is_claude_with_empty_text():
    assert TerminalMode.split_escape(">") == ("claude", "")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
