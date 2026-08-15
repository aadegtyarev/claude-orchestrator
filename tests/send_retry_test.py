"""Регресс: send_to_claude ретраит, пока channel-сервер поднимается.

Гонка двух сообщений подряд (media group / быстрый повтор): второе видит
session.running=True сразу после старта процесса claude, но channel-сервер
(MCP-подпроцесс) на порту ещё не слушает → ConnectionRefused. Короткий ретрай
закрывает стартовое окно; дохлая сессия → сразу пробрасываем.

Запуск: .venv/bin/python tests/send_retry_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp  # noqa: E402

from orchestrator.core import sessions  # noqa: E402
from orchestrator.core.sessions import SessionManager  # noqa: E402


def _conn_err() -> aiohttp.ClientConnectorError:
    # Конструктор ClientConnectorError разный по версиям — берём инстанс без
    # __init__: для except-по-типу и isinstance этого достаточно.
    return aiohttp.ClientConnectorError.__new__(aiohttp.ClientConnectorError)


class _Resp:
    def raise_for_status(self):
        return None


class _CM:
    def __init__(self, fail, exc):
        self.fail = fail
        self.exc = exc

    async def __aenter__(self):
        if self.fail:
            raise self.exc()
        return _Resp()

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, fail_times, exc=_conn_err):
        self.fail_times = fail_times
        self.exc = exc
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        return _CM(self.calls <= self.fail_times, self.exc)


def _mgr(http):
    m = SessionManager.__new__(SessionManager)
    m._http = http
    m._http_session = lambda: http
    m._channel_headers = lambda: {}
    return m


async def test_send_retry():
    sessions.SEND_RETRY_TIMEOUT = 5.0  # тест не должен ждать долго

    # 3 отказа подряд, потом успех — ретрай доводит доставку.
    http = _Http(fail_times=3)
    m = _mgr(http)
    sess = SimpleNamespace(name="s", port=12345, last_activity=0.0, running=True,
                           channel_blocked=False)
    await m.send_to_claude(sess, "hi", "ctx")
    assert http.calls == 4, http.calls
    print("OK send_to_claude: ретраит до подъёма channel-сервера")

    # Сессия умерла во время ретрая → сразу пробрасываем, не крутимся.
    http2 = _Http(fail_times=99)
    m2 = _mgr(http2)
    dead = SimpleNamespace(name="s", port=12345, last_activity=0.0, running=False,
                           channel_blocked=False)
    try:
        await m2.send_to_claude(dead, "hi", "ctx")
        assert False, "должно было пробросить"
    except aiohttp.ClientConnectorError:
        pass
    assert http2.calls == 1, http2.calls
    print("OK send_to_claude: дохлая сессия → не ретраим, пробрасываем")

    # Медленный stdio-MCP хендшейк: /ping уже отвечает, но /notify упирается в
    # ClientTimeout → asyncio.TimeoutError. Раньше ретрай ловил только
    # ConnectionRefused и сообщение молча терялось; теперь ретраим и таймаут.
    http3 = _Http(fail_times=2, exc=asyncio.TimeoutError)
    m3 = _mgr(http3)
    sess3 = SimpleNamespace(name="s", port=12345, last_activity=0.0, running=True,
                            channel_blocked=False)
    await m3.send_to_claude(sess3, "hi", "ctx")
    assert http3.calls == 3, http3.calls
    print("OK send_to_claude: ретраит и на таймаут хендшейка (не только refused)")


def main():
    asyncio.run(test_send_retry())
    print("ALL SEND-RETRY OK")


if __name__ == "__main__":
    main()
