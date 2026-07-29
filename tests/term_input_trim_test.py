"""Две доработки терминала поверх агентских кусков.

1. Автоввод в липком режиме: пока команда крутится и ждёт ответа (y/n, пароль),
   следующее сообщение уходит ЕЙ НА ВХОД, а не получает «терминал занят».
   Ради этого режим и делался — вспоминать про /bashin посреди диалога с
   командой абсурдно.
2. Чистка файлов с полным выводом: без неё они копятся до удаления сессии.

Запуск: .venv/bin/python tests/term_input_trim_test.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import BASH_OUTPUTS_KEEP, OrchestratorCore  # noqa: E402


# ── чистка файлов с выводом ──────────────────────────────────────────


def make_outputs(folder: Path, count: int) -> None:
    """Создать count файлов вывода с РАЗНЫМ временем правки (свежие — позже)."""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        p = folder / f"bash_2026010{i % 9}_00000{i % 9}_cmd{i}.txt"
        p.write_text(f"вывод {i}", encoding="utf-8")
        # mtime задаём явно: тест не должен зависеть от разрешения таймера ФС.
        import os
        os.utime(p, (1000 + i, 1000 + i))


def test_trim_keeps_only_recent(tmp_path: Path):
    make_outputs(tmp_path, BASH_OUTPUTS_KEEP + 5)
    OrchestratorCore._trim_bash_outputs(tmp_path)
    left = list(tmp_path.glob("bash_*.txt"))
    assert len(left) == BASH_OUTPUTS_KEEP


def test_trim_removes_oldest_not_newest(tmp_path: Path):
    """Удаляем старое: за ним не возвращаются, свежее лежит рядом с сообщением."""
    make_outputs(tmp_path, 5)
    OrchestratorCore._trim_bash_outputs(tmp_path, keep=2)
    left = sorted(p.name for p in tmp_path.glob("bash_*.txt"))
    assert left == sorted(["bash_20260103_000003_cmd3.txt",
                           "bash_20260104_000004_cmd4.txt"])


def test_trim_below_limit_keeps_everything(tmp_path: Path):
    make_outputs(tmp_path, 3)
    OrchestratorCore._trim_bash_outputs(tmp_path, keep=10)
    assert len(list(tmp_path.glob("bash_*.txt"))) == 3


def test_trim_ignores_foreign_files(tmp_path: Path):
    """Чужие файлы в папке не наши — не трогаем даже при переполнении."""
    make_outputs(tmp_path, 4)
    keep_me = tmp_path / "заметка.txt"
    keep_me.write_text("не наше", encoding="utf-8")
    OrchestratorCore._trim_bash_outputs(tmp_path, keep=1)
    assert keep_me.exists()


def test_trim_survives_missing_folder(tmp_path: Path):
    """Папки нет — чистка молчит, а не роняет выполнение команды."""
    OrchestratorCore._trim_bash_outputs(tmp_path / "нет-такой")


# ── автоввод в работающую команду ────────────────────────────────────


class FakeCore:
    """Минимальное ядро: только то, что дёргает on_text в липком режиме."""

    def __init__(self, busy: bool, accept_input: bool = True):
        self._busy = busy
        self._accept_input = accept_input
        self.sent_input: list[str] = []
        self.ran: list[str] = []
        from orchestrator.core.termmode import TerminalMode
        self.term = TerminalMode()
        self.term.turn_on("k")

    def bash_key(self, session, scope):
        return "k"

    def bash_busy(self, key):
        return self._busy

    def bash_input(self, key, text):
        if not self._accept_input:
            return False
        self.sent_input.append(text)
        return True


async def route(core: FakeCore, text: str) -> str:
    """Повторяет решение on_text в липком режиме: ввод в команду или запуск.

    Тест проверяет само ПРАВИЛО маршрутизации, а не aiogram: поднимать бота
    ради выбора из двух веток — дороже и хрупче, чем проверить правило.
    """
    key = core.bash_key(None, "tg1")
    where, payload = core.term.split_escape(text)
    if where == "claude":
        return "claude"
    if core.bash_busy(key) and core.bash_input(key, payload):
        return "input"
    core.ran.append(payload)
    return "run"


async def test_message_goes_to_running_command():
    core = FakeCore(busy=True)
    assert await route(core, "y") == "input"
    assert core.sent_input == ["y"]
    assert core.ran == []


async def test_message_runs_when_terminal_is_free():
    core = FakeCore(busy=False)
    assert await route(core, "ls -la") == "run"
    assert core.ran == ["ls -la"]
    assert core.sent_input == []


async def test_escape_still_reaches_claude_while_busy():
    """`>` работает даже во время команды — режим не запирает топик."""
    core = FakeCore(busy=True)
    assert await route(core, "> что там с тестами") == "claude"
    assert core.sent_input == []


async def test_falls_back_to_run_when_shell_gone():
    """Оболочка умерла (bash_input вернул False) — не молчим, а пробуем запуск."""
    core = FakeCore(busy=True, accept_input=False)
    assert await route(core, "y") == "run"


def test_input_if_busy_is_atomic():
    """Проверка «занят» и запись — ОДИН вызов, иначе гонка.

    Порознь они выглядят так: спросили busy → команда успела завершиться →
    записали. Тогда ответ команде («y», пароль) достаётся освободившейся
    оболочке уже КАК КОМАНДА. Здесь проверяем сам контракт: свободной
    оболочке метод ничего не пишет.
    """
    from orchestrator.core.app import OrchestratorCore

    written: list[str] = []
    shell = SimpleNamespace(busy=False, write=written.append)
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.bash = SimpleNamespace(get=lambda key: shell)

    assert core.bash_input_if_busy("k", "y") is False
    assert written == []  # свободной оболочке ввод не уходит

    shell.busy = True
    assert core.bash_input_if_busy("k", "y") is True
    assert written == ["y\n"]


async def test_interrupt_releases_terminal_on_live_shell(tmp_path: Path):
    """После ⏹ терминал обязан освободиться, а run_bash — завершиться.

    Регресс: Ctrl-C стирает недописанную строку bash вместе с маркером конца,
    поэтому цикл run_bash не видел завершения и сидел до таймаута (10 минут).
    Всё это время busy оставался взведённым — и следующие сообщения оператора
    уходили в stdin вместо запуска, молча, под одной лишь реакцией.
    """
    from orchestrator.core.app import OrchestratorCore
    from orchestrator.core.bashshell import BashShellManager
    from orchestrator.core.texts import get_texts

    core = OrchestratorCore.__new__(OrchestratorCore)
    core.bash = BashShellManager()
    core._done_markers = {}
    core._texts = get_texts("ru")
    core.config = SimpleNamespace(sandbox="off", sessions_dir=tmp_path)
    core.manager = SimpleNamespace(
        runner_for=lambda s: SimpleNamespace(supports_prefix=True))
    core.termhist = SimpleNamespace(add=lambda *a: None)
    core._track = lambda coro: asyncio.ensure_future(coro)
    core.bash_cwd = lambda s: tmp_path

    async def on_update(html, done, file_path=None):
        pass

    task = asyncio.ensure_future(core.run_bash("k", None, "sleep 60", on_update))
    await asyncio.sleep(2.0)  # дать команде реально стартовать в PTY
    assert core.interrupt_bash("k") is True
    # Без фикса здесь был бы таймаут: маркер конца после Ctrl-C не приходит.
    await asyncio.wait_for(task, timeout=10)
    await asyncio.sleep(0.8)  # _release снимает busy в фоне
    assert core.bash.get("k").busy is False
    core.bash.close_all()


def test_input_if_busy_without_shell():
    """Оболочки нет вовсе — False, без исключения."""
    from orchestrator.core.app import OrchestratorCore

    core = OrchestratorCore.__new__(OrchestratorCore)
    core.bash = SimpleNamespace(get=lambda key: None)
    assert core.bash_input_if_busy("k", "y") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
