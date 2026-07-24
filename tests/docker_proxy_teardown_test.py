"""Регрессия: прокси не оставляет висящих задач при обрыве клиента на молчащем
апстриме (это давало шумный GeneratorExit / «Task was destroyed» в логах прода).

Без docker: поднимаем фейковый «молчащий» апстрим (принимает запрос и молчит, как
контейнер sleep без вывода), клиент шлёт запрос и резко закрывается — прокси обязан
прибрать соединение (self._conns пустеет), а не ждать вечно.

Запуск: .venv/bin/python tests/docker_proxy_teardown_test.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.docker.proxy import DockerProxy  # noqa: E402


async def _silent_upstream(sockpath: Path):
    """Апстрим, который читает запрос и МОЛЧИТ (не отвечает) — как контейнер без
    вывода. Держит соединение открытым."""
    async def handle(r, w):
        try:
            await r.readuntil(b"\r\n\r\n")  # проглотить голову
            await r.read()                   # МОЛЧИМ (не отвечаем), но выходим на EOF,
                                             # когда прокси закроет соединение при teardown
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            w.close()
    return await asyncio.start_unix_server(handle, path=str(sockpath))


async def scenario():
    d = Path(tempfile.mkdtemp())
    up_sock = d / "up.sock"
    proxy_sock = d / "proxy.sock"
    upstream = await _silent_upstream(up_sock)
    proxy = DockerProxy(proxy_sock, roots_provider=lambda: [d], real_sock=str(up_sock))
    await proxy.start()
    try:
        # клиент шлёт запрос (не create → пройдёт к апстриму) и резко закрывается
        r, w = await asyncio.open_unix_connection(str(proxy_sock))
        w.write(b"GET /_ping HTTP/1.1\r\nHost: docker\r\n\r\n")
        await w.drain()
        await asyncio.sleep(0.2)             # прокси успел завести соединение
        assert len(proxy._conns) == 1, f"ожидали 1 живое соединение, есть {len(proxy._conns)}"
        w.close()                            # РЕЗКО уходим
        try:
            await w.wait_closed()
        except Exception:  # noqa: BLE001
            pass

        # прокси должен прибрать задачу (не виснуть на молчащем апстриме)
        for _ in range(50):                  # до 5 c
            if not proxy._conns:
                break
            await asyncio.sleep(0.1)
        assert not proxy._conns, f"задача соединения повисла: {proxy._conns}"
        print("OK teardown: обрыв клиента на молчащем апстриме → соединение прибрано")
    finally:
        await proxy.stop()
        upstream.close()
        try:
            await upstream.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    print("ALL DOCKER-PROXY-TEARDOWN OK")


def main():
    asyncio.run(asyncio.wait_for(scenario(), 30))


if __name__ == "__main__":
    main()
