"""История команд терминала — замена «стрелке вверх», которой в чате нет.

Оператор ошибся в команде и хочет поправить: в настоящем терминале это Up +
правка, в чате — либо правка своего сообщения, либо выбор из списка последних.
Хранение истории на стороне оркестратора и делает второй путь возможным.

Запуск: .venv/bin/python tests/term_history_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.termhistory import TermHistory  # noqa: E402


def test_empty_by_default():
    assert TermHistory().last("s:proj:tg1") == []


def test_keeps_order_newest_first():
    """Свежие сверху: в меню сначала то, что запускал только что."""
    h = TermHistory()
    for cmd in ("ls", "pytest", "git status"):
        h.add("k", cmd)
    assert h.last("k") == ["git status", "pytest", "ls"]


def test_history_is_per_terminal():
    h = TermHistory()
    h.add("s:a:tg1", "ls")
    h.add("s:b:tg1", "pwd")
    assert h.last("s:a:tg1") == ["ls"]
    assert h.last("s:b:tg1") == ["pwd"]


def test_repeat_moves_command_up_without_duplicating():
    """Повтор команды не засоряет список копиями, а поднимает её наверх."""
    h = TermHistory()
    for cmd in ("ls", "pytest", "ls"):
        h.add("k", cmd)
    assert h.last("k") == ["ls", "pytest"]


def test_limit_keeps_only_recent():
    h = TermHistory(limit=3)
    for cmd in ("a", "b", "c", "d"):
        h.add("k", cmd)
    assert h.last("k") == ["d", "c", "b"]


def test_blank_commands_are_ignored():
    """Пустая строка в истории бесполезна и занимает место."""
    h = TermHistory()
    h.add("k", "   ")
    h.add("k", "")
    assert h.last("k") == []


def test_command_is_stored_stripped():
    h = TermHistory()
    h.add("k", "  ls -la  ")
    assert h.last("k") == ["ls -la"]


def test_last_respects_requested_count():
    h = TermHistory()
    for cmd in ("a", "b", "c"):
        h.add("k", cmd)
    assert h.last("k", 2) == ["c", "b"]


def test_forget_session_drops_its_terminals():
    """Сессия удалена — её история уходит вместе с оболочками."""
    h = TermHistory()
    h.add("s:proj:tg1", "ls")
    h.add("s:proj:tg2", "pwd")
    h.add("s:other:tg1", "id")
    h.forget_session("proj")
    assert h.last("s:proj:tg1") == []
    assert h.last("s:proj:tg2") == []
    assert h.last("s:other:tg1") == ["id"]


def test_forget_session_does_not_match_name_prefix():
    """`proj` не должен снести историю сессии `project`."""
    h = TermHistory()
    h.add("s:project:tg1", "ls")
    h.forget_session("proj")
    assert h.last("s:project:tg1") == ["ls"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
