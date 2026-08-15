"""Авто-переподъём сессии, когда канал не отвечает при «живом» процессе.

Живой случай: процесс claude жив (session.running=True), а его MCP channel-сервер
мёртв (не поднялся/умер — типично после рестарта оркестратора). Раньше отправка
упиралась в закрытый порт и оператор получал СЫРОЕ «Cannot connect to host
127.0.0.1:PORT», хотя чинится это тривиально — переподнять сессию.

Проверяем контракт:
  • канал не отвечает → ядро САМО зовёт manager.revive() и ПОВТОРЯЕТ отправку;
  • после успешного повтора ошибки оператору нет (сообщение доехало);
  • переподъём ровно ОДИН (не крутим бесконечно), если и повтор упал — честная
    ошибка прежним путём (UserError с текстом forward_fail);
  • не-сетевые сбои отправки переподъём НЕ триггерят (лечим только этот случай).

Запуск: .venv/bin/python tests/session_revive_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

import aiohttp  # noqa: E402

from orchestrator.core.app import OrchestratorCore, UserError  # noqa: E402
from orchestrator.core.sessions import SessionError  # noqa: E402


def _conn_error() -> aiohttp.ClientConnectorError:
    """Настоящий ClientConnectorError — ровно то, что летит от мёртвого порта."""
    key = SimpleNamespace(host="127.0.0.1", port=38703, is_ssl=False, ssl=None)
    return aiohttp.ClientConnectorError(key, OSError(111, "Connect call failed"))


class _Manager:
    """Фейк SessionManager: отправка падает N раз, revive считается."""

    def __init__(self, fail_times: int, exc_factory=_conn_error):
        self.fail_times = fail_times
        self.exc_factory = exc_factory
        self.sent: list[str] = []
        self.revived = 0

    async def send_to_claude(self, session, text, context_id) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc_factory()
        self.sent.append(text)

    async def revive(self, session) -> bool:
        self.revived += 1
        return True


def _core(manager) -> OrchestratorCore:
    """Ядро без адаптеров/IO: подменяем только то, что трогает user_message."""
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.manager = manager
    core.bubbles = SimpleNamespace(
        freeze_and_open=lambda name: asyncio.sleep(0),
        close=lambda name: asyncio.sleep(0),
        append=lambda *a, **k: asyncio.sleep(0),
    )
    core.tools = SimpleNamespace(forget=lambda name: None)
    core.turns = SimpleNamespace(start=lambda name: None, stop=lambda name: None)
    core._record = lambda *a, **k: None
    core.context_id = lambda session, origin: "ctx"
    core.t = lambda key, **kw: f"{key}:{kw.get('error', '')}"
    core._notify_state_changed = lambda session: asyncio.sleep(0)
    core.session_hooks = []
    return core


SESSION = SimpleNamespace(name="ikar", running=True, channel_blocked=False)
ORIGIN = SimpleNamespace(adapter="telegram")


async def test_dead_channel_revives_and_delivers():
    """Канал молчит один раз → ядро переподняло сессию и ДОСТАВИЛО сообщение."""
    mgr = _Manager(fail_times=1)
    core = _core(mgr)
    await core.user_message(SESSION, "привет", ORIGIN)
    assert mgr.revived == 1, f"переподъём не сделан: {mgr.revived}"
    assert mgr.sent == ["привет"], f"сообщение не доехало: {mgr.sent}"
    print("OK мёртвый канал: сессия переподнята, сообщение доставлено без ошибки")


async def test_revive_happens_once_then_honest_error():
    """Если и после переподъёма канал молчит — ровно ОДИН revive и честная
    ошибка оператору (а не бесконечный цикл переподъёмов)."""
    mgr = _Manager(fail_times=99)
    core = _core(mgr)
    try:
        await core.user_message(SESSION, "привет", ORIGIN)
    except UserError as e:
        assert "forward_fail" in str(e), str(e)
    else:
        raise AssertionError("повторный сбой обязан дать UserError")
    assert mgr.revived == 1, f"переподъём должен быть ровно один, было {mgr.revived}"
    assert mgr.sent == [], "сообщение не могло доехать"
    print("OK повторный сбой: ровно один переподъём, затем честная ошибка")


async def test_dead_pty_revives_too():
    """Второй вход в сессию — терминал — лечится тем же переподъёмом.

    При заблокированном канале доставка идёт не по HTTP, а вставкой в PTY, и
    мёртвый терминал приходит как SessionError, а не ClientConnectorError.
    Раньше эта ветка мимо revive отдавала оператору сырую ошибку.
    """
    mgr = _Manager(fail_times=1,
                   exc_factory=lambda: SessionError("Терминал сессии недоступен: EIO"))
    core = _core(mgr)
    await core.user_message(SESSION, "привет", ORIGIN)
    assert mgr.revived == 1, f"переподъём не сделан: {mgr.revived}"
    assert mgr.sent == ["привет"], f"сообщение не доехало: {mgr.sent}"
    print("OK мёртвый терминал: сессия переподнята, сообщение доставлено")


async def test_non_network_failure_does_not_revive():
    """Не-сетевой сбой отправки лечится не переподъёмом — сессию не трогаем."""
    mgr = _Manager(fail_times=1, exc_factory=lambda: RuntimeError("boom"))
    core = _core(mgr)
    try:
        await core.user_message(SESSION, "привет", ORIGIN)
    except UserError:
        pass
    else:
        raise AssertionError("не-сетевой сбой обязан дать UserError")
    assert mgr.revived == 0, "переподъём на не-сетевом сбое — лишний"
    print("OK не-сетевой сбой: переподъёма нет, ошибка как прежде")


def main() -> None:
    asyncio.run(test_dead_channel_revives_and_delivers())
    asyncio.run(test_revive_happens_once_then_honest_error())
    asyncio.run(test_dead_pty_revives_too())
    asyncio.run(test_non_network_failure_does_not_revive())
    print("ALL SESSION-REVIVE OK")


if __name__ == "__main__":
    main()
