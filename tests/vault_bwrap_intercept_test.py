"""Перехват TLS в bwrap-песочнице end-to-end (фаза 2, срез 2.5,
docs/ARCHITECTURE-claude-box.md §4.2/§4.3).

Два уровня:
  * ЮНИТ (везде): launch-хук кошелька поднимает per-session прокси и готовит
    env-вклад — HTTPS_PROXY + trust-bundle появляются ТОЛЬКО при прокси-секрете
    (сессия без него ничего нового не получает); CA-bundle записан в дом сессии
    0644 и содержит корень Vault; стоп прокси освобождает порт. Плюс регресс
    прод-trust: WalletModule._make_proxy_pool оставляет upstream_ssl ДЕФОЛТНЫМ.
  * ЖИВОЙ (мягкий скип без bwrap/openssl): локальный HTTPS-«сервис», прокси-
    секрет со scope на него, ВНУТРИ bwrap-песочницы (runners/sandbox.build_argv)
    python делает HTTPS-запрос через HTTPS_PROXY+CA → сервис видит впрыснутый
    Bearer, а КОМАНДА значения секрета не видела (§4.2). Разделение прод/тест:
    прод-путь (WalletModule) — дефолтный системный upstream_ssl; тестовый trust к
    Vault-CA передаётся ТОЛЬКО в пул этого теста (иначе самозванец под сервис
    получил бы кред — CRITICAL). Всё под таймаутами — не виснет.

Запуск: .venv/bin/python tests/vault_bwrap_intercept_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent

try:
    from orchestrator.modules.wallet.module import (  # noqa: E402
        CA_BUNDLE_NAME, WalletModule, _system_ca_pem,
    )
    from orchestrator.runners import sandbox  # noqa: E402
    from vault.daemon import VaultDaemon  # noqa: E402
    from vault.proxy_pool import SessionProxyPool  # noqa: E402
    from vault.secret import Secret  # noqa: E402
    from vault.tls import VaultCA  # noqa: E402
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERR = exc

_TIMEOUT = 20
_SECRET_VALUE = "PROXY-SECRET-value-DO-NOT-LEAK-777"


def _skip(reason: str) -> bool:
    print(f"SKIP {reason}")
    return True


def _proxy_secret(sessions=("*",)) -> Secret:
    """Прокси-секрет (§4.5): connector+value+scope, без env/commands/shared."""
    return Secret(
        name="svc", value=_SECRET_VALUE, env="", description="",
        sessions=sessions, commands=(), deny=(), allow_unsafe=False,
        confirm=False, shared=False,
        connector="generic-bearer",
        scope={"url_prefixes": ["https://localhost/allowed"]},
    )


def _inject_secret() -> Secret:
    """Обычный inject-секрет (не прокси) — для проверки обратной совместимости."""
    return Secret(
        name="tok", value="INJ-VALUE", env="API_TOKEN", description="",
        sessions=("*",), commands=("curl *",), deny=(), allow_unsafe=False,
        confirm=False, shared=False,
    )


def _module(store, ca, pool, home: Path) -> WalletModule:
    """WalletModule с ЗАРАНЕЕ проставленными ca/proxies (обходим start()/openssl-
    генерацию в дефолтном каталоге). daemon только для делегирования пулу."""
    m = WalletModule.__new__(WalletModule)
    m.store = store
    m.ca = ca
    m.proxies = pool
    m._proxy_env = {}
    m.daemon = VaultDaemon(store, host=None, guard_on=False, proxies=pool)
    m.config = SimpleNamespace(orch_host="127.0.0.1", guest_orch_host="127.0.0.1")
    session_dir = home
    m.core = SimpleNamespace(
        manager=SimpleNamespace(session_home=lambda s, _h=session_dir: _h)
    )
    return m


# ── ЮНИТ ────────────────────────────────────────────────────────────

async def test_proxy_env_and_ca_bundle():
    """launch-хук: HTTPS_PROXY+trust-bundle только при прокси-секрете; CA-bundle
    0644 в доме сессии с корнем Vault; стоп освобождает порт; прод-trust дефолтный."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт не удался: {_IMPORT_ERR}")
    tmp = Path(tempfile.mkdtemp(prefix="bwrap_intercept_unit_"))
    ca = VaultCA(tmp / "ca")
    secret = _proxy_secret()
    store = SimpleNamespace(load=lambda: {"svc": secret, "tok": _inject_secret()})
    pool = SessionProxyPool(ca, store)  # upstream_ssl дефолтный (как в проде)
    home = tmp / "home"
    home.mkdir()
    m = _module(store, ca, pool, home)
    session = SimpleNamespace(name="dev")
    try:
        await m._start_session_proxies(session)
        env = m.session_env(session)
        # HTTPS_PROXY указывает на живой порт прокси пары (dev, svc).
        port = pool.port("dev", "svc")
        assert port is not None, "прокси не поднят"
        assert env["HTTPS_PROXY"] == f"http://127.0.0.1:{port}", env
        assert env["https_proxy"] == f"http://127.0.0.1:{port}", env
        ca_path = env["SSL_CERT_FILE"]
        assert env["REQUESTS_CA_BUNDLE"] == ca_path and env["CURL_CA_BUNDLE"] == ca_path
        # HTTP_PROXY НЕ ставим (прокси только CONNECT/HTTPS).
        assert "HTTP_PROXY" not in env, env
        # NO_PROXY уводит контрольный трафик claude (loopback) мимо MITM.
        assert "127.0.0.1" in env["NO_PROXY"] and "localhost" in env["NO_PROXY"], env
        assert env["no_proxy"] == env["NO_PROXY"], env
        # inject-секрет по-прежнему маркером — перехват его не трогает.
        assert env["API_TOKEN"] == "<<wallet:tok>>", env
        print("OK env: HTTPS_PROXY+trust-bundle+NO_PROXY только при прокси-секрете")

        # CA-bundle: файл в доме сессии, 0644, содержит корень Vault (+ систему).
        bundle_file = home / CA_BUNDLE_NAME
        assert bundle_file.exists(), "CA-bundle не записан"
        assert oct(bundle_file.stat().st_mode & 0o777) == "0o644"
        text = bundle_file.read_text()
        assert ca.ca_cert_pem().strip() in text, "корень Vault не в bundle"
        sysca = _system_ca_pem()
        if sysca is not None:
            assert "BEGIN CERTIFICATE" in text and text.count("BEGIN CERTIFICATE") >= 2, (
                "системные корни не добавлены в bundle")
        # Путь в env — ВНУТРИ песочницы ($HOME/<файл>), не хостовый дом сессии.
        assert ca_path == str(Path.home() / CA_BUNDLE_NAME), ca_path
        print("OK CA-bundle: 0644 в доме сессии, содержит корень Vault + систему")

        # Прод-trust (регресс, CRITICAL-класс): пул этого модуля НЕ переопределяет
        # upstream_ssl — реориджин проверяет НАСТОЯЩИЙ серт сервиса.
        assert pool._upstream_ssl is None, "upstream_ssl не дефолтный — риск выдачи кред самозванцу"
        print("OK прод-trust: upstream_ssl пула дефолтный (системный)")

        # Стоп освобождает порт (пара снята из пула).
        await m.daemon.stop_session_proxies("dev")
        assert pool.port("dev", "svc") is None, "порт не освобождён после стопа"
        assert pool.ports("dev") == {}, "остались живые прокси после стопа"
        print("OK стоп: прокси снят, порт освобождён")
    finally:
        await pool.stop_all()


async def test_no_proxy_secret_backward_compat():
    """Сессия без прокси-секрета: launch-хук ничего не поднимает, env без HTTPS_PROXY."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт не удался: {_IMPORT_ERR}")
    tmp = Path(tempfile.mkdtemp(prefix="bwrap_intercept_compat_"))
    store = SimpleNamespace(load=lambda: {"tok": _inject_secret()})
    # Пул НЕ создан (нет прокси-секретов) — как решает _make_proxy_pool.
    m = WalletModule.__new__(WalletModule)
    m.store = store
    m.ca = None
    m.proxies = None
    m._proxy_env = {}
    m.daemon = None
    m.core = SimpleNamespace(manager=SimpleNamespace(session_home=lambda s: tmp))
    session = SimpleNamespace(name="dev")
    await m._start_session_proxies(session)   # no-op без пула
    env = m.session_env(session)
    assert "HTTPS_PROXY" not in env and "SSL_CERT_FILE" not in env, env
    assert env["API_TOKEN"] == "<<wallet:tok>>", env  # обычный кошелёк не сломан
    assert not (tmp / CA_BUNDLE_NAME).exists(), "CA-bundle записан без прокси-секрета"
    print("OK обратная совместимость: без прокси-секрета env как раньше, CA не пишется")


async def test_symlink_no_arbitrary_write():
    """CRITICAL-регресс (репро ревью): модель подкладывает в доме сессии симлинк
    на victim-файл под именем нашего файла; провижн CA-bundle и wallet.json НЕ
    затирает victim (arbitrary write / сандбокс-эскейп) и заменяет симлинк
    обычным файлом. Дом сессии RW-виден модели в песочнице — вектор реален."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт не удался: {_IMPORT_ERR}")
    tmp = Path(tempfile.mkdtemp(prefix="bwrap_intercept_symlink_"))
    home = tmp / "home"
    home.mkdir()
    session = SimpleNamespace(name="dev")

    # (1) CA-bundle: симлинк ~/.vault-ca-bundle.crt → victim.
    try:
        ca = VaultCA(tmp / "ca")
    except Exception as exc:  # noqa: BLE001 — нет openssl
        return _skip(f"VaultCA недоступен: {exc}")
    victim = tmp / "victim"
    victim.write_text("VICTIM-ORIGINAL")
    store = SimpleNamespace(load=lambda: {"svc": _proxy_secret()})
    m = _module(store, ca, None, home)
    (home / CA_BUNDLE_NAME).symlink_to(victim)
    ca_path = m._provision_ca_bundle(session)
    assert ca_path is not None, "CA-bundle не записан"
    assert victim.read_text() == "VICTIM-ORIGINAL", "victim затёрт через симлинк (CA-bundle)!"
    bundle_file = home / CA_BUNDLE_NAME
    assert not bundle_file.is_symlink(), "симлинк не заменён обычным файлом"
    assert ca.ca_cert_pem().strip() in bundle_file.read_text(), "bundle не записан"
    assert oct(bundle_file.stat().st_mode & 0o777) == "0o644"
    print("OK симлинк-защита: _provision_ca_bundle не затирает victim, симлинк заменён")

    # (2) wallet.json: симлинк ~/.wallet.json → victim2.
    victim2 = tmp / "victim2"
    victim2.write_text("VICTIM2-ORIGINAL")
    m2 = WalletModule.__new__(WalletModule)
    m2.store = store
    m2.daemon = VaultDaemon(store, host=None, guard_on=False)
    m2.core = SimpleNamespace(manager=SimpleNamespace(
        session_home=lambda s: home, effective_cwd=lambda s: tmp))
    (home / ".wallet.json").symlink_to(victim2)
    await m2._provision(session)
    assert victim2.read_text() == "VICTIM2-ORIGINAL", "victim затёрт через симлинк (wallet.json)!"
    wj = home / ".wallet.json"
    assert not wj.is_symlink(), "симлинк .wallet.json не заменён обычным файлом"
    assert json.loads(wj.read_text())["session"] == "dev", "wallet.json не записан"
    assert oct(wj.stat().st_mode & 0o777) == "0o600"
    print("OK симлинк-защита: _provision (wallet.json) не затирает victim, 0600")


# ── ЖИВОЙ bwrap ─────────────────────────────────────────────────────

class _Service:
    """Локальный HTTPS-«реальный сервис»: запоминает увиденный Authorization,
    в теле секрет НЕ отражает (чтобы доказать: команда не получала значение)."""

    def __init__(self, ctx: ssl.SSLContext) -> None:
        self._ctx = ctx
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.seen_auth: list[str] = []

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self._ctx)
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
            await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_TIMEOUT)
            auth = ""
            while True:
                hl = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=_TIMEOUT)
                if hl in (b"\r\n", b"\n"):
                    break
                name, _, value = hl.rstrip(b"\r\n").decode("latin-1").partition(":")
                if name.strip().lower() == "authorization":
                    auth = value.strip()
            self.seen_auth.append(auth)
            body = b"INTERCEPT-OK"  # секрет в теле НЕ фигурирует
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
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
    leaf = ca.issue("localhost")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(leaf.cert_path), keyfile=str(leaf.key_path))
    return ctx


def _upstream_trust(ca: VaultCA) -> ssl.SSLContext:
    """ТЕСТОВЫЙ upstream trust к CA Vault — ТОЛЬКО для пула этого теста, чтобы
    реориджин доверял локальному «сервису» (у прода тут системный дефолт)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=ca.ca_cert_pem())
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


_SANDBOX_SCRIPT = (
    "import os, ssl, urllib.request\n"
    "ctx = ssl.create_default_context()\n"  # честит SSL_CERT_FILE из env
    "op = urllib.request.build_opener(\n"
    "    urllib.request.ProxyHandler(urllib.request.getproxies()),\n"
    "    urllib.request.HTTPSHandler(context=ctx))\n"
    "r = op.open(os.environ['TARGET_URL'], timeout=10)\n"
    "print('STATUS', r.status)\n"
    "print(r.read().decode())\n"
)


async def test_live_bwrap_intercept():
    """ЖИВОЙ: внутри bwrap python через HTTPS_PROXY+CA бьёт в сервис; сервис видит
    впрыснутый Bearer, команда значения секрета не видела."""
    if _IMPORT_ERR is not None:
        return _skip(f"импорт не удался: {_IMPORT_ERR}")
    ok, why = sandbox.available()
    if not ok:
        return _skip(f"bwrap недоступен: {why}")
    tmp = Path(tempfile.mkdtemp(prefix="bwrap_intercept_live_"))
    try:
        ca = VaultCA(tmp / "ca")
    except Exception as exc:  # noqa: BLE001 — нет openssl
        return _skip(f"VaultCA недоступен: {exc}")

    service = _Service(_service_ctx(ca))
    await service.start()
    scope = {"url_prefixes": [f"https://localhost:{service.port}/allowed"]}
    secret = Secret(
        name="svc", value=_SECRET_VALUE, env="", description="", sessions=("*",),
        commands=(), deny=(), allow_unsafe=False, confirm=False, shared=False,
        connector="generic-bearer", scope=scope,
    )
    store = SimpleNamespace(load=lambda: {"svc": secret})
    # ТЕСТОВЫЙ upstream trust — только для этого пула (прод оставляет дефолт).
    pool = SessionProxyPool(ca, store, upstream_ssl=_upstream_trust(ca))
    home = tmp / "home"
    home.mkdir()
    m = _module(store, ca, pool, home)
    session = SimpleNamespace(name="dev")
    try:
        await m._start_session_proxies(session)
        proxy_env = m.session_env(session)
        assert "HTTPS_PROXY" in proxy_env and "SSL_CERT_FILE" in proxy_env, proxy_env

        # env субпроцесса: наследуем хост + перехват-вклад + цель. Значения
        # секрета здесь НЕТ — команда его не видит. no_proxy чистим: у оператора
        # обычно localhost в NO_PROXY, а наш «сервис» ради теста на localhost (в
        # проде реальный хост сервиса в NO_PROXY не значится).
        run_env = {**os.environ, **proxy_env, "HOME": str(Path.home()),
                   "no_proxy": "", "NO_PROXY": "",
                   "TARGET_URL": f"https://localhost:{service.port}/allowed/x"}
        assert _SECRET_VALUE not in "\n".join(f"{k}={v}" for k, v in run_env.items()), (
            "значение секрета попало в env команды")

        argv = sandbox.build_argv(
            home=Path.home(), chdir=tmp, rw_paths=[tmp], ro_paths=[ROOT],
            home_dir=home,  # монтируется как $HOME → SSL_CERT_FILE=$HOME/<bundle>
        )
        argv += ["python3", "-c", _SANDBOX_SCRIPT]
        proc = await asyncio.create_subprocess_exec(
            *argv, env=run_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
        stdout = out.decode(errors="replace")
        stderr = err.decode(errors="replace")
        assert proc.returncode == 0, f"код {proc.returncode}\nSTDOUT:{stdout}\nSTDERR:{stderr}"
        assert "STATUS 200" in stdout, f"нет 200: {stdout!r} / {stderr!r}"
        assert "INTERCEPT-OK" in stdout, f"нет тела сервиса: {stdout!r}"
        # Команда НЕ видела значение секрета — его нет в её выводе.
        assert _SECRET_VALUE not in stdout and _SECRET_VALUE not in stderr, (
            "значение секрета утекло в вывод команды")
        # Сервис УВИДЕЛ впрыснутый Bearer (§4.2: подставил прокси, не команда).
        assert service.seen_auth == [f"Bearer {_SECRET_VALUE}"], (
            f"сервис не увидел впрыснутый Bearer: {service.seen_auth}")
        print("OK ЖИВОЙ bwrap: сервис увидел Bearer, команда секрет не видела (§4.2)")
    finally:
        await pool.stop_all()
        await service.stop()


def main() -> None:
    if _IMPORT_ERR is not None:
        _skip(f"vault/wallet недоступны: {_IMPORT_ERR}")
        return
    for coro in (
        test_proxy_env_and_ca_bundle,
        test_no_proxy_secret_backward_compat,
        test_symlink_no_arbitrary_write,
        test_live_bwrap_intercept,
    ):
        asyncio.run(coro())
    print("ALL VAULT-BWRAP-INTERCEPT OK")


if __name__ == "__main__":
    main()
