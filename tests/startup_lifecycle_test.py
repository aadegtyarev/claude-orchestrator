"""Startup cleanup and Telegram network recovery regressions."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

import pytest  # noqa: E402

import orchestrator.__main__ as entrypoint  # noqa: E402
import orchestrator.adapters.telegram.adapter as telegram_module  # noqa: E402
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError  # noqa: E402
from orchestrator.adapters.telegram.adapter import TelegramAdapter  # noqa: E402


class _FakeBot:
    def __init__(self):
        self.default_menu_calls = 0
        self.group_menu_calls = 0
        self.me_calls = 0
        self.closed = False
        self.session = SimpleNamespace(close=self.close)
        self._me_cache = None
        self.menu_errors = []
        self.group_menu_errors = []
        self.me_errors = []

    async def set_my_commands(self, commands, scope=None):
        if scope is None:
            self.default_menu_calls += 1
            errors = self.menu_errors
        else:
            self.group_menu_calls += 1
            errors = self.group_menu_errors
        if errors:
            raise errors.pop(0)

    async def delete_my_commands(self):
        pass

    async def me(self):
        if self._me_cache is None:
            self.me_calls += 1
            if self.me_errors:
                raise self.me_errors.pop(0)
            self._me_cache = object()
        return self._me_cache

    async def close(self):
        self.closed = True


class _FakeDispatcher:
    def __init__(self):
        self.poll_calls = []
        self.poll_event = asyncio.Event()

    async def start_polling(self, bot, **kwargs):
        self.poll_calls.append(kwargs)
        await bot.me()  # aiogram _polling preflight uses cached Bot.me().
        await self.poll_event.wait()

    async def stop_polling(self):
        self.poll_event.set()


def _adapter(bot=None):
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.config = SimpleNamespace(show_command_menu=True)
    adapter.core = SimpleNamespace(command_available=lambda _command: True)
    adapter.t = lambda key: key
    adapter.bot = bot or _FakeBot()
    adapter.dp = _FakeDispatcher()
    adapter._poll_task = None
    return adapter


async def test_telegram_start_retries_only_failed_menu_call_and_warms_bot_me(monkeypatch):
    bot = _FakeBot()
    bot.menu_errors = [TelegramNetworkError(method="setMyCommands", message="offline")]
    bot.group_menu_errors = [TelegramNetworkError(method="setMyCommands", message="offline")]
    bot.me_errors = [TelegramNetworkError(method="getMe", message="offline")]
    adapter = _adapter(bot)
    delays = []
    async def no_delay(seconds):
        delays.append(seconds)

    monkeypatch.setattr(telegram_module, "STARTUP_NETWORK_BUDGET_S", 10)
    monkeypatch.setattr(telegram_module, "_startup_sleep", no_delay)
    await adapter.start()
    await asyncio.sleep(0)
    assert bot.default_menu_calls == 2
    assert bot.group_menu_calls == 2
    assert bot.me_calls == 2
    assert delays == [1.0, 1.0, 1.0]
    assert len(adapter.dp.poll_calls) == 1
    assert adapter.dp.poll_calls[0]["close_bot_session"] is False
    assert adapter._poll_task is not None and not adapter._poll_task.done()
    await adapter.stop()
    assert bot.closed


async def test_telegram_start_does_not_retry_auth_or_start_poller(monkeypatch):
    bot = _FakeBot()
    error = TelegramUnauthorizedError(method="getMe", message="unauthorized")
    bot.me_errors = [error]
    adapter = _adapter(bot)
    monkeypatch.setattr(telegram_module, "STARTUP_NETWORK_BUDGET_S", 10)

    with pytest.raises(TelegramUnauthorizedError) as raised:
        await adapter.start()
    assert raised.value is error
    assert bot.me_calls == 1
    assert adapter.dp.poll_calls == []


async def test_telegram_start_does_not_retry_programming_error(monkeypatch):
    bot = _FakeBot()
    error = ValueError("invalid local configuration")
    bot.me_errors = [error]
    adapter = _adapter(bot)
    monkeypatch.setattr(telegram_module, "STARTUP_NETWORK_BUDGET_S", 10)

    with pytest.raises(ValueError) as raised:
        await adapter.start()
    assert raised.value is error
    assert bot.me_calls == 1
    assert adapter.dp.poll_calls == []


async def test_telegram_start_network_budget_exhaustion_is_visible(monkeypatch):
    bot = _FakeBot()
    bot.me_errors = [TelegramNetworkError(method="getMe", message="offline") for _ in range(100)]
    adapter = _adapter(bot)
    monkeypatch.setattr(telegram_module, "STARTUP_NETWORK_BUDGET_S", 0.01)
    monkeypatch.setattr(telegram_module, "STARTUP_RETRY_DELAYS_S", (0.01,))

    with pytest.raises(RuntimeError, match="Telegram startup timed out during bot identity"):
        await adapter.start()
    assert bot.me_calls >= 1
    assert adapter.dp.poll_calls == []


async def test_telegram_start_cancellation_during_backoff_stops_before_poller(monkeypatch):
    bot = _FakeBot()
    bot.me_errors = [TelegramNetworkError(method="getMe", message="offline")]
    adapter = _adapter(bot)
    entered_sleep = asyncio.Event()

    async def wait_for_cancel(_seconds):
        entered_sleep.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(telegram_module, "_startup_sleep", wait_for_cancel)
    start_task = asyncio.create_task(adapter.start())
    await entered_sleep.wait()
    start_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await start_task
    await adapter.stop()
    assert adapter.dp.poll_calls == []
    assert bot.closed


async def test_main_start_failure_preserves_error_and_runs_all_cleanup(monkeypatch, tmp_path):
    calls = []
    cores = []
    config = SimpleNamespace(
        sessions_dir=tmp_path / "sessions",
        max_instances=1,
        adapters=(),
        sandbox="off",
        sandbox_docker=False,
        allowed_user_ids=(),
        idle_timeout_h=0,
    )

    class Manager:
        def load_state(self):
            pass

        def total(self):
            return 0

        async def shutdown(self):
            calls.append("manager")

    class Core:
        def __init__(self, _config, _manager):
            cores.append(self)
            self.manager = _manager
            self.modules = []
            self.startup_notifications = 0
            self.bubbles = SimpleNamespace(close_all=self._close_bubbles)
            self.handle_reply = self.handle_tool_event = None
            self.handle_permission_request = self.handle_stop_event = None

        async def _close_bubbles(self):
            calls.append("bubbles")
            raise RuntimeError("bubble cleanup sentinel")

        def register_adapter(self, _adapter):
            pass

        async def start(self):
            raise RuntimeError("startup sentinel")

        async def notify_startup(self, _restored):
            self.startup_notifications += 1

        def save_history(self):
            calls.append("history")

        async def close(self):
            calls.append("core")

    class Runner:
        name = "direct"

        def preflight(self):
            return True, ""

    class ReplyRunner:
        async def cleanup(self):
            calls.append("reply")

    monkeypatch.setattr(entrypoint.Config, "from_env", lambda: config)
    monkeypatch.setattr(entrypoint, "SessionManager", lambda _config: Manager())
    monkeypatch.setattr(entrypoint, "OrchestratorCore", Core)
    monkeypatch.setattr(entrypoint, "make_adapters", lambda *_args: [])
    monkeypatch.setattr(entrypoint, "make_modules", lambda *_args: [])
    monkeypatch.setattr(entrypoint, "make_runner", lambda *_args: Runner())
    async def start_server(*_args):
        return ReplyRunner()
    monkeypatch.setattr(entrypoint, "start_reply_server", start_server)

    with pytest.raises(RuntimeError, match="startup sentinel"):
        await entrypoint.main()
    assert calls == ["history", "bubbles", "manager", "reply", "core"]
    assert cores[0].startup_notifications == 0


def main():
    tests = (
        test_telegram_start_retries_only_failed_menu_call_and_warms_bot_me,
        test_telegram_start_does_not_retry_auth_or_start_poller,
        test_telegram_start_does_not_retry_programming_error,
        test_telegram_start_network_budget_exhaustion_is_visible,
        test_telegram_start_cancellation_during_backoff_stops_before_poller,
    )
    for test in tests:
        patcher = pytest.MonkeyPatch()
        try:
            asyncio.run(test(monkeypatch=patcher))
        finally:
            patcher.undo()
    with tempfile.TemporaryDirectory() as temp_dir:
        patcher = pytest.MonkeyPatch()
        try:
            asyncio.run(
                test_main_start_failure_preserves_error_and_runs_all_cleanup(
                    monkeypatch=patcher, tmp_path=Path(temp_dir)
                )
            )
        finally:
            patcher.undo()
    print("ALL STARTUP-LIFECYCLE OK")


if __name__ == "__main__":
    main()
