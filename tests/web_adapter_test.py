"""Офлайн-тест веб-адаптера: авторизация, REST → ядро, WS-события, файловый jail.

Без Telegram и Claude: ядро — фейк, адаптер поднимается на свободном порту.

Запуск: .venv/bin/python tests/web_adapter_test.py
"""
import asyncio
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp  # noqa: E402

from orchestrator.adapters.web.adapter import WebAdapter  # noqa: E402

calls: list = []


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_env(tmp: Path):
    session = SimpleNamespace(
        name="dev", title="Dev", model=None, linked_path=None,
        running=True, started_at=0.0, session_dir=tmp / "dev",
    )
    (tmp / "dev").mkdir()

    class FakeCore:
        manager = SimpleNamespace(
            get=lambda n: session if n == "dev" else None,
            list_all=lambda: [session],
        )

        def t(self, k, **kw):
            return k

        def features(self):
            # Единый источник правды о доступных фичах живёт в ядре; адаптеры
            # только транслируют его в свой UI.
            return {"wallet": True, "stats": True}

        def session_status(self, s):
            return "waiting"

        def box_mark(self, s):
            # Пометка нестандартной изоляции сессии (`--box`); у фейка её нет.
            return ""

        def profile_mark(self, s):
            # Пометка нестандартной учётки (`--profile`); у фейка её нет.
            return ""

        def box_choices(self):
            return ["bwrap", "off"]

        def fmt_duration(self, sec):
            return "0 мин"

        async def ensure_running(self, s):
            return "running"

        async def user_message(self, s, text, origin):
            calls.append(("user_message", text, origin.adapter))

        async def slash_command(self, s, cmd):
            calls.append(("slash", cmd))

        async def request_report(self, s, origin):
            calls.append(("request_report",))

        async def hard_stop(self, s):
            calls.append(("hard_stop",))

        async def permission_verdict(self, s, rid, behavior, via):
            calls.append(("perm", rid, behavior, via))
            return True

        def history(self, name):
            return [{"ts": 1.0, "kind": "reply", "text": "**жирный** финал"}]

        def path_in_workspace(self, path, s):
            # Как в ядре: только внутри папки сессии.
            try:
                return path.resolve().is_relative_to((tmp / "dev").resolve())
            except (OSError, ValueError):
                return False

    config = SimpleNamespace(
        web_host="127.0.0.1", web_port=free_port(), web_token="test-token",
        incoming_dir="incoming", sessions_dir=tmp,
    )
    return FakeCore(), config, session


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="web_test_"))
    core, config, session = make_env(tmp)
    adapter = WebAdapter(config, core)
    await adapter.start()
    base = f"http://127.0.0.1:{config.web_port}"
    auth = {"Authorization": "Bearer test-token"}
    try:
        async with aiohttp.ClientSession() as http:
            # без токена — 401 на /api, статика открыта
            async with http.get(f"{base}/api/sessions") as r:
                assert r.status == 401
            async with http.get(f"{base}/") as r:
                assert r.status == 200 and "text/html" in r.headers["Content-Type"]
            print("OK auth: /api без токена → 401, статика открыта")

            # / с не-ASCII ?token= не должен падать 500 (compare_digest на байтах)
            async with http.get(f"{base}/?token=пароль") as r:
                assert r.status == 200, r.status
            # / с верным ?token= ставит HttpOnly-cookie
            async with http.get(f"{base}/?token=test-token") as r:
                assert r.status == 200
                assert "orch_web_token=" in r.headers.get("Set-Cookie", "")
            print("OK h_index: не-ASCII token не 500; верный token ставит cookie")

            # ?token= тоже работает (вход по ссылке из лога)
            async with http.get(f"{base}/api/sessions?token=test-token") as r:
                assert r.status == 200
            # список сессий
            async with http.get(f"{base}/api/sessions", headers=auth) as r:
                data = await r.json()
            assert data[0]["name"] == "dev" and data[0]["status"] == "waiting", data
            print("OK GET /api/sessions")

            # WS: hello + событие доставки
            async with http.ws_connect(f"{base}/api/ws", headers=auth) as ws:
                hello = await asyncio.wait_for(ws.receive_json(), 5)
                assert hello["type"] == "hello" and hello["sessions"][0]["name"] == "dev"
                await adapter.deliver_text(session, "**готово**", intermediate=False)
                ev = await asyncio.wait_for(ws.receive_json(), 5)
                assert ev["type"] == "reply" and ev["session"] == "dev"
                assert "<b>готово</b>" in ev["html"], ev
                print("OK WS: hello + reply-событие с html")

                # session_state_changed (переход из ЛЮБОГО источника: TG/idle/
                # смерть) → сигнал обновить список даже без REST от веба.
                await adapter.session_state_changed(session)
                ev = await asyncio.wait_for(ws.receive_json(), 5)
                assert ev["type"] == "sessions_changed", ev
                print("OK WS: session_state_changed → sessions_changed")

                # бабл: post → ref, edit тем же ref
                ref = await adapter.bubble_post(session, "<b>x</b>", stop_button=True)
                ev = await asyncio.wait_for(ws.receive_json(), 5)
                assert ev["type"] == "bubble" and ev["ref"] == ref and ev["stop_button"]
                await adapter.bubble_finish(session, ref, delete=True)
                ev = await asyncio.wait_for(ws.receive_json(), 5)
                assert ev["type"] == "bubble_close" and ev["delete"] is True
                print("OK WS: бабл post/finish")

            # message → ядро; /команда → slash_command
            calls.clear()
            async with http.post(f"{base}/api/sessions/dev/message", headers=auth,
                                 json={"text": "привет"}) as r:
                assert r.status == 200
            async with http.post(f"{base}/api/sessions/dev/message", headers=auth,
                                 json={"text": "/context"}) as r:
                assert (await r.json())["slash"] is True
            assert ("user_message", "привет", "web") in calls and ("slash", "/context") in calls
            print("OK POST message: текст → user_message, /команда → slash")

            # permission → ядро
            async with http.post(f"{base}/api/sessions/dev/permission", headers=auth,
                                 json={"request_id": "r1", "behavior": "allow"}) as r:
                assert (await r.json())["handled"] is True
            assert ("perm", "r1", "allow", "web") in calls
            print("OK POST permission → permission_verdict")

            # stop / interrupt
            async with http.post(f"{base}/api/sessions/dev/stop", headers=auth) as r:
                assert r.status == 200
            async with http.post(f"{base}/api/sessions/dev/interrupt", headers=auth) as r:
                assert r.status == 200
            assert ("request_report",) in calls and ("hard_stop",) in calls
            print("OK stop/interrupt")

            # history: reply получает html
            async with http.get(f"{base}/api/sessions/dev/history", headers=auth) as r:
                items = await r.json()
            assert "<b>жирный</b>" in items[0]["html"], items
            print("OK history c html-рендером")

            # файловый jail: внутри workspace — отдаётся, снаружи — 403
            inside = tmp / "dev" / "out.txt"
            inside.write_text("data")
            async with http.get(f"{base}/api/sessions/dev/file",
                                headers=auth, params={"path": str(inside)}) as r:
                assert r.status == 200 and await r.text() == "data"
            async with http.get(f"{base}/api/sessions/dev/file",
                                headers=auth, params={"path": "/etc/passwd"}) as r:
                assert r.status == 403
            print("OK GET file: workspace отдан, /etc/passwd → 403")

            # неизвестная сессия — 404
            async with http.post(f"{base}/api/sessions/ghost/message", headers=auth,
                                 json={"text": "x"}) as r:
                assert r.status == 404
            print("OK неизвестная сессия → 404")

            # снапшот бабла: null пока нет, объект после bubble_post
            async with http.get(f"{base}/api/sessions/dev/bubble", headers=auth) as r:
                assert await r.json() is None
            ref = await adapter.bubble_post(session, "<b>работаю</b>", stop_button=True)
            async with http.get(f"{base}/api/sessions/dev/bubble", headers=auth) as r:
                snap = await r.json()
            assert snap and snap["ref"] == ref and snap["stop_button"] is True
            await adapter.bubble_finish(session, ref, delete=True)
            async with http.get(f"{base}/api/sessions/dev/bubble", headers=auth) as r:
                assert await r.json() is None
            print("OK /bubble: снапшот появляется/исчезает с баблом")

            # многострочный текст с '/' в первой строке — НЕ слэш-команда
            calls.clear()
            async with http.post(f"{base}/api/sessions/dev/message", headers=auth,
                                 json={"text": "/var/log/x\nвот лог"}) as r:
                assert (await r.json())["slash"] is False
            assert any(c[0] == "user_message" for c in calls)
            print("OK многострочный '/...' → обычное сообщение, не команда")
    finally:
        await adapter.stop()

    print("ALL WEB OK")


async def test_web_adapter():
    await main()

if __name__ == "__main__":
    asyncio.run(main())
