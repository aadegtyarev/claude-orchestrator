"""Регресс §2: resume-фолбэк держит порт зарезервированным на чистый рестарт.

Без резерва конкурентный _find_free_port в окне между _stop_process и подъёмом
нового channel-сервера отдал бы порт другой сессии → два сервера на одном порту,
сообщения перепутались бы. Проверяем, что в этом окне порт недоступен.

Запуск: .venv/bin/python tests/resume_port_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import sessions  # noqa: E402
from orchestrator.core.sessions import SessionManager  # noqa: E402


class FakeSession:
    def __init__(self, port):
        self.name = "s"
        self.port = port
        self.claude_session_id = "old"
        self.session_dir = Path("/tmp")
        self.linked_path = None
        self.sandbox = None  # движок изоляции = дефолт .env (флаг --box не задан)
        self.running = False
        self.watcher = None
        self.started_at = 0.0
        self.ops = asyncio.Lock()


def _free_port() -> int:
    # Свободный порт динамически — хардкод падал по TIME_WAIT при полном прогоне
    # (пул из одного порта + bind без SO_REUSEADDR в _find_free_port).
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def run():
    old_grace = sessions.RESUME_GRACE
    sessions.RESUME_GRACE = 0.0
    try:
        port = _free_port()
        m = SessionManager.__new__(SessionManager)
        m.config = SimpleNamespace(channel_port_start=port, channel_port_end=port)
        m._by_name = {}
        m._inflight_ports = set()
        m._lock = asyncio.Lock()

        session = FakeSession(port)
        m._by_name["s"] = session
        reserved_during_wait = {"ok": None}

        m._guard_unique_cwd = lambda s: None
        m._write_mcp_json = lambda s: None
        m._write_claude_settings = lambda s: None
        m._start_watcher = lambda s: m._inflight_ports.discard(s.port)
        m.save_state = lambda: None

        async def fake_start_claude(s, resume=False):
            # resume «стартует», но окажется мёртвым (fallback); clean — живой.
            s.running = not resume
        m._start_claude = fake_start_claude

        async def fake_wait_ready(s):
            return None
        m._wait_ready = fake_wait_ready

        async def fake_stop(s, save=True):
            s.running = False
            m._inflight_ports.discard(s.port)
        m._stop_process = fake_stop

        async def fake_wait_port_free(port, timeout=10.0):
            # После _stop_process фикс должен был заново зарезервировать порт —
            # иначе конкурентный _find_free_port увёл бы его другой сессии.
            reserved_during_wait["ok"] = m._find_free_port() is None
        m._wait_port_free = fake_wait_port_free

        resumed = await m._resume_locked(session)
        assert resumed is False, resumed
        assert reserved_during_wait["ok"] is True, "порт НЕ зарезервирован в окне фолбэка"
        assert session.running is True
        # После успешного рестарта watcher снял именно inflight-резерв (порт
        # теперь держит running-сессия, а не «в полёте»).
        assert port not in m._inflight_ports
        print("OK resume-фолбэк держит порт зарезервированным на чистый рестарт")
    finally:
        sessions.RESUME_GRACE = old_grace


def test_resume_port():
    asyncio.run(run())


if __name__ == "__main__":
    asyncio.run(run())
    print("ALL RESUME-PORT OK")
