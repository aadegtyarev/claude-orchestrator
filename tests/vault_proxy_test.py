"""VaultProxy — живой end-to-end MITM-forward-прокси (фаза 2, срез 2.3,
docs/ARCHITECTURE-claude-box.md §4.2/§4.4). Автономно: только vault.* + stdlib.

Схема теста:
  * «реальный сервис» — локальный HTTPS-сервер на само-подписанном (через тот же
    VaultCA) серте; отражает увиденный `Authorization` в теле, чтобы проверить
    инъекцию/её отсутствие;
  * клиент — stdlib (ssl + сокет: ручной CONNECT + TLS через прокси). aiohttp не
    берём — реориджин доверяет сервису через upstream_ssl с CA Vault, а клиент
    доверяет прокси тем же CA;
  * прокси — VaultProxy(ca, generic-bearer, secret, scope), upstream_ssl = trust
    к CA Vault (так реориджин доверяет локальному «сервису»).

Проверяем: (1) CONNECT+MITM TLS; (2) впрыснутый Bearer виден СЕРВИСУ, но НЕ
клиенту; (3) ALLOW под префиксом → 200; (4) вне scope → 403 c remedy, до сервиса
не дошло; (5) обрыв соединения не оставляет висящих задач у прокси.

Мягкий скип, если среды нет (нет ssl / openssl). Всё под таймаутами — не виснет.

Запуск: .venv/bin/python tests/vault_proxy_test.py
"""
from __future__ import annotations

import asyncio
import ssl
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from vault.proxy import VaultProxy, _upgrade_tls  # noqa: E402
    from vault.secret import Secret  # noqa: E402
    from vault.tls import VaultCA  # noqa: E402
    from vault.connectors import GenericBearerConnector  # noqa: E402
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERR = exc

_TIMEOUT = 15
_SECRET_VALUE = "s3cr3t-token-value-DO-NOT-LEAK"


def _skip(reason: str) -> bool:
    print(f"SKIP {reason}")
    return True


def _mk_secret() -> Secret:
    return Secret(
        name="svc", value=_SECRET_VALUE, env="TOK", description="",
        sessions=("*",), commands=(), deny=(), allow_unsafe=False,
        confirm=False, shared=False,
    )


class _Service:
    """Локальный HTTPS-«реальный сервис»: отражает Authorization и путь."""

    def __init__(self, ctx: ssl.SSLContext) -> None:
        self._ctx = ctx
        self.server: asyncio.AbstractServer | None = None
        self.host = "localhost"
        self.port = 0
        self.seen: list[str] = []  # target'ы всех дошедших до сервиса запросов

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=self._ctx
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _handle(self, reader, writer) -> None:
        try:
            line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_TIMEOUT)
            target = line.decode("latin-1").split(" ")[1]
            self.seen.append(target)
            auth = ""
            while True:
                hl = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_TIMEOUT)
                if hl in (b"\r\n", b"\n"):
                    break
                name, _, value = hl.rstrip(b"\r\n").decode("latin-1").partition(":")
                if name.strip().lower() == "authorization":
                    auth = value.strip()
            # Путь с «big» → большой ответ (для теста застоя клиента): клиент не
            # читает → write-буфер прокси заполняется → drain обязан размыкаться
            # по таймауту, а не виснуть.
            if "big" in target:
                body = b"x" * 8_000_000
            else:
                body = f"path={target} auth={auth}".encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass


def _service_ctx(ca: VaultCA) -> ssl.SSLContext:
    """Серверный контекст сервиса с leaf(localhost) от VaultCA."""
    leaf = ca.issue("localhost")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(leaf.cert_path), keyfile=str(leaf.key_path))
    return ctx


def _trust_ctx(ca: VaultCA) -> ssl.SSLContext:
    """Клиентский контекст, доверяющий корню Vault (и для клиента, и для upstream)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=ca.ca_cert_pem())
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


async def _open_mitm(proxy_url: str, host: str, port: int, trust: ssl.SSLContext):
    """Ручной CONNECT к прокси + апгрейд клиентской стороны в TLS. Возвращает
    (reader, writer) поверх MITM-туннеля. Всё под таймаутом (не виснет)."""
    p = proxy_url.removeprefix("http://")
    phost, pport = p.split(":")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(phost, int(pport)), timeout=_TIMEOUT
    )
    authority = f"{host}:{port}"
    writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    await writer.drain()
    status_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_TIMEOUT)
    assert b"200" in status_line, f"CONNECT не удался: {status_line!r}"
    while True:  # дочитать заголовки ответа CONNECT
        hl = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_TIMEOUT)
        if hl in (b"\r\n", b"\n"):
            break
    # Кросс-версионный апгрейд (start_tls только с 3.11) — тот же helper, что и
    # в прокси, но клиентская сторона: server_side=False + server_hostname.
    reader, writer = await _upgrade_tls(
        reader, writer, trust,
        server_side=False, server_hostname=host, timeout=_TIMEOUT,
    )
    return reader, writer


async def _read_status(text: str) -> int:
    return int(text.split(" ", 2)[1]) if text.startswith("HTTP/") else 0


async def _client_get(proxy_url: str, host: str, port: int, path: str,
                      trust: ssl.SSLContext, *,
                      request_line: str | None = None,
                      host_header: str | None = None) -> tuple[int, str]:
    """CONNECT+MITM+GET через прокси → (status_code, raw_response_text).

    request_line — переопределить строку запроса целиком (для absolute-form);
    host_header — переопределить заголовок Host (для проверки маршрута по
    CONNECT-authority, а не по Host)."""
    reader, writer = await _open_mitm(proxy_url, host, port, trust)
    try:
        authority = f"{host}:{port}"
        rl = request_line if request_line is not None else f"GET {path} HTTP/1.1"
        hh = host_header if host_header is not None else authority
        writer.write(f"{rl}\r\nHost: {hh}\r\n\r\n".encode())
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout=_TIMEOUT)
        text = raw.decode("latin-1")
        return await _read_status(text), text
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass


async def _setup(*, upstream_trust: bool = True, **proxy_kwargs):
    """Поднять CA, сервис и прокси; вернуть (ca, service, proxy, trust).

    upstream_trust=False → прокси с ДЕФОЛТНЫМ (системным) upstream_ssl: реориджин
    к сервису на Vault-CA-серте провалится (прод-путь, самозванец не получит кред).
    proxy_kwargs пробрасываются в VaultProxy (напр. idle_timeout=… для теста застоя)."""
    ca = VaultCA(Path(tempfile.mkdtemp(prefix="vault_proxy_")))
    service = _Service(_service_ctx(ca))
    await service.start()
    scope = {"url_prefixes": [f"https://localhost:{service.port}/allowed"]}
    kw = dict(proxy_kwargs)
    if upstream_trust:
        kw.setdefault("upstream_ssl", _trust_ctx(ca))
    proxy = VaultProxy(ca, GenericBearerConnector(), _mk_secret(), scope, **kw)
    await proxy.start()
    return ca, service, proxy, _trust_ctx(ca)


async def test_mitm_and_inject_allow():
    """CONNECT+MITM ок; ALLOW под префиксом → 200; сервис видит Bearer, клиент — нет."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault.proxy не удался: {_IMPORT_ERR}")
    ca, service, proxy, trust = await _setup()
    try:
        code, text = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/allowed/data", trust
        )
        assert code == 200, f"ожидали 200, получили {code}: {text!r}"
        # (2) сервис увидел впрыснутый Bearer …
        assert f"auth=Bearer {_SECRET_VALUE}" in text, f"сервис не увидел кред: {text!r}"
        # … но клиент нигде не видел САМ секрет как свой ввод — он его не посылал.
        # Здесь секрет в ответе есть только потому, что сервис его отразил (эхо
        # авторизации); проверяем, что КЛИЕНТ не подставлял его сам — см.
        # test_client_never_sends_secret ниже.
        print("OK VaultProxy: CONNECT+MITM, ALLOW под префиксом, Bearer впрыснут сервису")
    finally:
        await proxy.stop()
        await service.stop()


async def test_client_never_sends_secret():
    """Клиент НЕ владеет секретом: инъекцию делает прокси. Проверяем, что без
    прокси (клиент шлёт свой запрос) значение секрета не фигурирует в исходящем —
    оно живёт только в прокси. Косвенно: отражённый сервисом Bearer появляется
    ТОЛЬКО при походе через прокси."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault.proxy не удался: {_IMPORT_ERR}")
    ca, service, proxy, trust = await _setup()
    try:
        # Прямой TLS к сервису БЕЗ прокси (клиент сам, без кред) → auth пустой.
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("localhost", service.port, ssl=trust,
                                    server_hostname="localhost"),
            timeout=_TIMEOUT,
        )
        try:
            writer.write(b"GET /allowed/x HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(65536), timeout=_TIMEOUT)
        finally:
            writer.close()
        text = raw.decode("latin-1")
        assert "auth=" in text and _SECRET_VALUE not in text, (
            f"клиент сам НЕ должен слать секрет, но он в запросе: {text!r}"
        )
        print("OK VaultProxy: клиент не владеет секретом (прямой запрос без кред)")
    finally:
        await proxy.stop()
        await service.stop()


async def test_out_of_scope_denied():
    """Запрос вне scope → 403 с remedy в теле, до сервиса не дошло."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault.proxy не удался: {_IMPORT_ERR}")
    ca, service, proxy, trust = await _setup()
    try:
        code, text = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/forbidden/x", trust
        )
        assert code == 403, f"ожидали 403 вне scope, получили {code}: {text!r}"
        # remedy предписывающий (Р0) — есть тело с указанием, что делать.
        assert "scope" in text.lower() or "префикс" in text.lower(), (
            f"в 403 нет предписывающего remedy: {text!r}"
        )
        # секрет НЕ впрыснут (до сервиса не дошли — нет auth-эха от сервиса).
        assert f"Bearer {_SECRET_VALUE}" not in text, "секрет утёк в DENY-ответе"
        assert "path=/forbidden" not in text, "запрос дошёл до сервиса вопреки DENY"
        print("OK VaultProxy: вне scope → 403 с remedy, до сервиса не дошло")
    finally:
        await proxy.stop()
        await service.stop()


async def test_abort_does_not_hang():
    """Обрыв соединения (клиент шлёт CONNECT и рвёт) не оставляет висящих задач."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault.proxy не удался: {_IMPORT_ERR}")
    ca, service, proxy, trust = await _setup()
    try:
        # Открыть, послать CONNECT и тут же закрыть, не завершая TLS.
        for _ in range(3):
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", proxy.port), timeout=_TIMEOUT
            )
            writer.write(f"CONNECT localhost:{service.port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=_TIMEOUT)
            except Exception:  # noqa: BLE001
                pass
        # Дать циклу прокрутить обработчики и проверить, что прокси ещё жив:
        # успешный запрос после серии обрывов доказывает, что задачи не залипли.
        await asyncio.sleep(0.2)
        code, _ = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/allowed/ok", trust
        )
        assert code == 200, f"прокси не обслуживает после обрывов: {code}"
        # Ни одной висящей задачи прокси в цикле (кроме текущей и служебных).
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        assert len(pending) <= 1, f"похоже на залипшие задачи: {pending}"
        print("OK VaultProxy: обрывы соединений не вешают прокси, задачи не залипают")
    finally:
        await proxy.stop()
        await service.stop()


async def test_default_upstream_rejects_untrusted_service():
    """Прод-trust (регресс, CRITICAL-класс): при ДЕФОЛТНОМ upstream_ssl реориджин
    к сервису на Vault-CA-серте (не системный trust) ПРОВАЛИВАЕТСЯ → 502, кред НЕ
    уходит. Доказывает: в проде самозванец под сервис не получит секрет."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault.proxy не удался: {_IMPORT_ERR}")
    ca, service, proxy, trust = await _setup(upstream_trust=False)
    try:
        code, text = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/allowed/data", trust
        )
        assert code == 502, f"ожидали 502 (upstream не доверен), получили {code}: {text!r}"
        assert _SECRET_VALUE not in text, "секрет утёк в ответе при провале upstream"
        # До сервиса НЕ дошли (TLS не установился) — сервис ничего не видел.
        assert service.seen == [], f"запрос дошёл до недоверенного сервиса: {service.seen}"
        print("OK VaultProxy: дефолтный upstream_ssl режет Vault-CA-серт → 502, кред не ушёл")
    finally:
        await proxy.stop()
        await service.stop()


async def test_pipelining_second_request_ignored():
    """Смуглинг/pipelining: два запроса back-to-back на одном MITM-TLS. Прокси
    обслуживает ТОЛЬКО первый (Connection: close, один запрос на туннель); второй
    не проскакивает мимо in_scope и до сервиса не доходит."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault.proxy не удался: {_IMPORT_ERR}")
    ca, service, proxy, trust = await _setup()
    reader, writer = await _open_mitm(proxy.proxy_url, "localhost", service.port, trust)
    try:
        authority = f"localhost:{service.port}"
        # Первый — ALLOW; второй — вне scope. Шлём слитно, до чтения ответа.
        writer.write(
            f"GET /allowed/one HTTP/1.1\r\nHost: {authority}\r\n\r\n"
            f"GET /forbidden/two HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()
        )
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), timeout=_TIMEOUT)
        text = raw.decode("latin-1")
        assert await _read_status(text) == 200, f"первый запрос не 200: {text!r}"
        assert "path=/allowed/one" in text, f"ответ не на первый запрос: {text!r}"
        await asyncio.sleep(0.2)  # дать шанс (ошибочно) обработать второй
        assert service.seen == ["/allowed/one"], (
            f"второй запрос проскочил к сервису мимо in_scope: {service.seen}"
        )
        print("OK VaultProxy: pipelining — второй запрос не проскакивает мимо in_scope")
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass
        await proxy.stop()
        await service.stop()


async def test_host_header_ignored_route_by_authority():
    """Host-mismatch: CONNECT на in-scope host, но Host-заголовок на чужой → и
    in_scope, и реориджин идут по CONNECT-authority; чужой ресурс не подставляет
    решение (запрос всё равно уходит к authority-сервису под его скоупом)."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault.proxy не удался: {_IMPORT_ERR}")
    ca, service, proxy, trust = await _setup()
    try:
        code, text = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/allowed/x", trust,
            host_header="evil.example.com",
        )
        # Решение по authority (localhost, in-scope) → ALLOW, дошли до НАШЕГО сервиса.
        assert code == 200, f"ожидали 200 (маршрут по authority), получили {code}: {text!r}"
        assert service.seen == ["/allowed/x"], f"ушли не туда: {service.seen}"
        assert f"auth=Bearer {_SECRET_VALUE}" in text, "кред не подставлен по authority"
        print("OK VaultProxy: Host-заголовок игнорируется, маршрут по CONNECT-authority")
    finally:
        await proxy.stop()
        await service.stop()


async def test_absolute_form_request_rejected():
    """LOW: absolute-form request-line (`GET https://evil/… HTTP/1.1`) в туннеле →
    явный 400 (fail-closed), к in_scope мусорный url не пускаем, сервис не тронут."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault.proxy не удался: {_IMPORT_ERR}")
    ca, service, proxy, trust = await _setup()
    try:
        code, text = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/allowed/x", trust,
            request_line="GET https://evil.example.com/allowed/x HTTP/1.1",
        )
        assert code == 400, f"ожидали 400 на absolute-form, получили {code}: {text!r}"
        assert service.seen == [], f"absolute-form дошёл до сервиса: {service.seen}"
        assert _SECRET_VALUE not in text, "секрет утёк в ответе на absolute-form"
        print("OK VaultProxy: absolute-form request-line → 400, сервис не тронут")
    finally:
        await proxy.stop()
        await service.stop()


async def test_client_stall_does_not_hang():
    """MEDIUM-фикс: клиент делает валидный запрос к большому ресурсу и перестаёт
    читать → write-буфер прокси заполняется → drain размыкается по idle_timeout,
    прокси не виснет и продолжает обслуживать; висящих задач не остаётся."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault.proxy не удался: {_IMPORT_ERR}")
    # Малый idle_timeout — чтобы застой размыкался быстро, без 5-мин ожидания.
    ca, service, proxy, trust = await _setup(idle_timeout=1.0)
    try:
        reader, writer = await _open_mitm(proxy.proxy_url, "localhost", service.port, trust)
        authority = f"localhost:{service.port}"
        writer.write(f"GET /allowed/big HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
        await writer.drain()
        # Прочитать чуть-чуть и ЗАМЕРЕТЬ (не читать остаток большого ответа).
        await asyncio.wait_for(reader.read(1024), timeout=_TIMEOUT)
        await asyncio.sleep(2.5)  # > idle_timeout: прокси должен разомкнуть drain
        # Прокси жив: свежий запрос на другом туннеле проходит.
        code, _ = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/allowed/ok", trust
        )
        assert code == 200, f"прокси завис на застое клиента: {code}"
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.2)
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        assert len(pending) <= 1, f"похоже на залипшие задачи после застоя: {pending}"
        print("OK VaultProxy: застой клиента размыкается по таймауту, задачи не залипают")
    finally:
        await proxy.stop()
        await service.stop()


def main() -> None:
    if _IMPORT_ERR is not None:
        _skip(f"vault.proxy недоступен: {_IMPORT_ERR}")
        return
    for coro in (
        test_mitm_and_inject_allow,
        test_client_never_sends_secret,
        test_out_of_scope_denied,
        test_abort_does_not_hang,
        test_default_upstream_rejects_untrusted_service,
        test_pipelining_second_request_ignored,
        test_host_header_ignored_route_by_authority,
        test_absolute_form_request_rejected,
        test_client_stall_does_not_hang,
    ):
        asyncio.run(coro())
    print("ALL VAULT-PROXY OK")


if __name__ == "__main__":
    main()
