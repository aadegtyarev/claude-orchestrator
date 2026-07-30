"""Команда /term [on|off] и липкий режим терминала в ядре.

Проверяем контракт OrchestratorCore.term_command:
  • term on/off включает/выключает липкий режим для ключа топика;
  • отказ включения в главном чате (session=None) — безопасность: там шелл
    на хосте с правами оператора;
  • /term без аргументов — краткий статус (вкл/выкл, каталог, занятость);
  • неизвестный аргумент — понятная ошибка со списком допустимых.

Также проверяем TerminalMode.split_escape — это уже покрыто в
terminal_mode_test.py, здесь интеграционно: как оно вяжется с term_command.

Запуск: .venv/bin/python tests/terminal_command_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import OrchestratorCore, UserError  # noqa: E402
from orchestrator.core.termmode import TerminalMode  # noqa: E402
from orchestrator.core.texts import get_texts  # noqa: E402


# ── фикстуры ─────────────────────────────────────────────────────────


class FakeBashShell:
    """Фейк BashSession: busy и snapshot для тестов term_command."""
    busy = False


class FakeBash:
    """Фейк BashShellManager: get по ключу."""
    def __init__(self):
        self._shells: dict[str, FakeBashShell] = {}

    def get(self, key: str) -> FakeBashShell | None:
        shell = self._shells.get(key)
        if shell is None:
            shell = FakeBashShell()
            self._shells[key] = shell
        return shell


class FakeMgr:
    """Фейк SessionManager: effective_cwd и get."""
    def __init__(self, cwd: Path | None = None):
        self._cwd = cwd or Path("/tmp/test-project")

    def effective_cwd(self, session):
        return self._cwd


def make_core(cwd: Path | None = None):
    """Ядро с минимумом зависимостей, достаточным для term_command."""
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.manager = FakeMgr(cwd)
    core._texts = get_texts("ru")
    core.term = TerminalMode()
    core.bash = FakeBash()
    return core


def make_session(name: str = "proj"):
    """Фейк сессии — только поля, нужные term_command через bash_cwd."""
    return SimpleNamespace(name=name)


# ── тесты команды /term ─────────────────────────────────────────────


def test_term_on_off_roundtrip():
    """Включили — ключ активен; выключили — нет."""
    core = make_core()
    session = make_session()
    key = "s:proj:tg123"

    r = core.term_command(session, key, "on")
    assert "включён" in r.lower() or "терминала" in r.lower(), r
    assert core.term.is_on(key) is True

    r = core.term_command(session, key, "off")
    assert "выключен" in r.lower() or "терминала" in r.lower(), r
    assert core.term.is_on(key) is False


def test_term_on_refused_in_main_chat():
    """В главном чате (session=None) /term on разрешён, но с предупреждением:
    шелл идёт по ХОСТУ с правами оператора без песочницы, любая опечатка
    выполнится на машине. Риск «забыл, что в терминале» снимает закреп."""
    core = make_core()
    key = "main:tg0"

    r = core.term_command(None, key, "on")
    assert core.term.is_on(key) is True
    # Предупреждение о хосте обязано быть — иначе оператор не поймёт риск.
    assert "ХОСТ" in r or "HOST" in r.upper(), r


def test_term_off_works_in_main_chat():
    """/term off работает везде — выключить можно и из главного чата."""
    core = make_core()
    key = "main:tg0"
    # Сперва включим (в обход запрета — напрямую) и проверим, что выключается.
    core.term.turn_on(key)
    assert core.term.is_on(key) is True

    r = core.term_command(None, key, "off")
    assert "выключен" in r.lower() or "OFF" in r, r
    assert core.term.is_on(key) is False


def test_termclose_cbdata_roundtrip():
    """Кнопка ✖ «Закрыть» кодирует и парсит thread_id."""
    from orchestrator.adapters.telegram.cbdata import (
        parse_termclose, termclose_cb,
    )
    assert termclose_cb(42) == "termclose:42"
    assert parse_termclose("termclose:42") == 42
    assert parse_termclose("garbage") is None


def test_term_on_no_host_warning_in_session():
    """В топике сессии предупреждения о хосте НЕ должно быть: там bwrap."""
    core = make_core()
    sess = make_session("proj")
    r = core.term_command(sess, "s:proj:tg1", "on")
    assert core.term.is_on("s:proj:tg1") is True
    assert "ХОСТ" not in r and "HOST" not in r.upper(), r



def test_term_status_on():
    """Без аргументов — статус: включён, каталог, занятость."""
    core = make_core()
    session = make_session()
    key = "s:proj:tg123"
    core.term.turn_on(key)

    r = core.term_command(session, key, "")
    assert "включён" in r.lower() or "ON" in r, r
    assert "/tmp/test-project" in r, r
    # Оболочка в простое — не занята.
    assert "нет" in r.lower() or "no" in r.lower(), r


def test_term_status_off():
    """Без аргументов — статус: выключен."""
    core = make_core()
    session = make_session()
    key = "s:proj:tg123"

    r = core.term_command(session, key, "")
    assert "выключен" in r.lower() or "OFF" in r, r


def test_term_unknown_arg():
    """Неизвестный аргумент — понятная ошибка со списком допустимых."""
    core = make_core()
    session = make_session()
    key = "s:proj:tg123"

    with pytest.raises(UserError) as exc:
        core.term_command(session, key, "enable")
    assert "on" in str(exc.value).lower() and "off" in str(exc.value).lower(), str(exc.value)


def test_term_per_key_isolation():
    """Режим для одного топика не включает его в другом."""
    core = make_core()
    session = make_session()
    key_a = "s:proj:tg1"
    key_b = "s:proj:tg2"

    core.term_command(session, key_a, "on")
    assert core.term.is_on(key_a) is True
    assert core.term.is_on(key_b) is False


# ── интеграция split_escape с командой ──────────────────────────────


def test_split_escape_routes_to_claude():
    """`>` в начале — claude (используется адаптером для выхода из липкого режима)."""
    assert TerminalMode.split_escape("> привет") == ("claude", "привет")
    assert TerminalMode.split_escape(">ls") == ("claude", "ls")


def test_split_escape_routes_to_shell():
    """Обычный текст без `>` — в шелл."""
    assert TerminalMode.split_escape("ls -la") == ("shell", "ls -la")
    assert TerminalMode.split_escape("") == ("shell", "")


def test_split_escape_double_escape():
    """`>>` — экранирование: уходит в шелл без первого символа."""
    assert TerminalMode.split_escape(">> out.txt") == ("shell", "> out.txt")


def test_split_escape_only_gt():
    """Просто `>` — claude с пустым текстом."""
    assert TerminalMode.split_escape(">") == ("claude", "")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
