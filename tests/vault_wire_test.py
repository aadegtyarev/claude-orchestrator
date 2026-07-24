"""Проводка policy → per-session MITM-прокси (фаза 2, срез 2.4,
docs/ARCHITECTURE-claude-box.md §4.3 атрибуция по порту, §4.5 policy/реестр).
Автономно: только vault.* + stdlib.

Проверяем:
  * store парсит connector + [secrets.x.scope]; секрет без connector — как раньше;
    неизвестный connector → секрет НЕ активен (не загружен);
  * SessionProxyPool поднимает per-session прокси на выделенном порту; end-to-end
    через этот порт (переиспользуем харнес vault_proxy_test: локальный https-
    сервис + клиент через прокси-порт) — in-scope ALLOW с впрыском кред, вне
    scope DENY+remedy; секрет живёт у прокси, не у клиента;
  * атрибуция §4.3: разные сессии → разные порты; порт связан с (сессия, секрет,
    scope), запрос вне scope этого секрета режется на ЕГО порту;
  * жизненный цикл: старт/стоп прокси сессии освобождает порт;
  * демон делегирует start/stop_session_proxy в пул.

Мягкий скип, если ssl/openssl-среды нет. Всё под таймаутами — не виснет.

Запуск: .venv/bin/python tests/vault_wire_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))  # для import vault_proxy_test

try:
    import vault_proxy_test as vpt  # харнес: _Service/_service_ctx/_trust_ctx/_client_get  # noqa: E402
    from vault.proxy_pool import ProxyPoolError, SessionProxyPool  # noqa: E402
    from vault.store import SecretStore  # noqa: E402
    from vault.tls import VaultCA  # noqa: E402
    _IMPORT_ERR = vpt._IMPORT_ERR  # если сам харнес не собрался (нет ssl) — скипаем
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERR = exc

_TIMEOUT = 15
_TOKEN = "wire-secret-token-DO-NOT-LEAK"
_TOKEN_B = "wire-secret-token-B-DO-NOT-LEAK"


def _skip(reason: str) -> bool:
    print(f"SKIP {reason}")
    return True


def _store_with(tmp: Path, body: str) -> SecretStore:
    f = tmp / "secrets.toml"
    f.write_text(body)
    os.chmod(f, 0o600)
    return SecretStore(f)


# ── store: парсинг connector/scope ─────────────────────────────────────────

def test_store_parses_connector_and_scope():
    """connector + [secrets.x.scope] попадают в Secret; mode == 'proxy'; секрет
    без connector — как раньше (mode host/inject/shared, не задет)."""
    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_store_"))
    store = _store_with(tmp, (
        '[secrets.svc]\n'
        'connector = "generic-bearer"\n'
        'value = "TKN"\n'
        'sessions = ["proj-*"]\n'
        '[secrets.svc.scope]\n'
        'url_prefixes = ["https://api.svc/v1"]\n\n'
        '[secrets.host]\n'
        'sessions = ["*"]\n'
        'commands = ["gh"]\n'
    ))
    secrets = store.load()
    assert set(secrets) == {"svc", "host"}, secrets
    svc = secrets["svc"]
    assert svc.is_proxy and svc.mode == "proxy", svc
    assert svc.connector == "generic-bearer"
    assert svc.scope == {"url_prefixes": ["https://api.svc/v1"]}, svc.scope
    # прокси-секрет НЕ раздаёт host-passthrough команды, несмотря на value без env
    assert not svc.host_passthrough and svc.effective_commands == (), svc
    # секрет без connector не задет — сегодняшнее поведение
    assert not secrets["host"].is_proxy and secrets["host"].mode == "host"
    print("OK store: connector+scope распарсены (mode=proxy); секрет без connector как раньше")


def test_store_unknown_connector_inactive():
    """Неизвестный connector → секрет НЕ активен (не загружен). «Выключено = не
    существует» (реестр логирует WARNING)."""
    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_unk_"))
    store = _store_with(tmp, (
        '[secrets.ghost]\n'
        'connector = "does-not-exist"\n'
        'value = "TKN"\n'
        'sessions = ["*"]\n'
        '[secrets.ghost.scope]\n'
        'url_prefixes = ["https://api.svc/v1"]\n\n'
        '[secrets.ok]\n'
        'value = "V"\nenv = "TOK"\nsessions = ["*"]\n'
    ))
    secrets = store.load()
    assert "ghost" not in secrets, "неизвестный connector должен быть не активен"
    assert "ok" in secrets, "остальные секреты грузятся как обычно"
    print("OK store: неизвестный connector → секрет не активен, прочие грузятся")


def test_store_connector_without_value_skipped():
    """Прокси-секрет без value — нечего подставлять → пропущен."""
    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_noval_"))
    store = _store_with(tmp, (
        '[secrets.novalue]\n'
        'connector = "generic-bearer"\n'
        'sessions = ["*"]\n'
    ))
    assert "novalue" not in store.load()
    print("OK store: connector-секрет без value пропущен")


def test_store_connector_rejects_value_leaking_fields():
    """CRITICAL/HIGH: прокси-секрет с shared/env/commands раздал бы своё значение
    наружу (wallet get / маркер в песочнице / инъекция в env). store НЕ активирует
    такой секрет («выключено = не существует»). Валидный (value+scope) — грузится."""
    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_leak_"))
    store = _store_with(tmp, (
        '[secrets.leak_shared]\n'
        'connector = "generic-bearer"\n'
        'value = "SUPER-SECRET"\n'
        'shared = true\n'
        'sessions = ["*"]\n'
        '[secrets.leak_shared.scope]\n'
        'url_prefixes = ["https://api.svc/v1"]\n\n'
        '[secrets.leak_env]\n'
        'connector = "generic-bearer"\n'
        'value = "SUPER-SECRET"\n'
        'env = "LEAK"\n'
        'sessions = ["*"]\n\n'
        '[secrets.leak_cmds]\n'
        'connector = "generic-bearer"\n'
        'value = "SUPER-SECRET"\n'
        'commands = ["curl *"]\n'
        'sessions = ["*"]\n\n'
        '[secrets.ok]\n'
        'connector = "generic-bearer"\n'
        'value = "OK-TOKEN"\n'
        'sessions = ["*"]\n'
        '[secrets.ok.scope]\n'
        'url_prefixes = ["https://api.svc/v1"]\n'
    ))
    secrets = store.load()
    assert "leak_shared" not in secrets, "connector+shared должен быть не активен"
    assert "leak_env" not in secrets, "connector+env должен быть не активен"
    assert "leak_cmds" not in secrets, "connector+commands должен быть не активен"
    assert "ok" in secrets and secrets["ok"].mode == "proxy", secrets
    print("OK store: proxy-секрет с shared/env/commands не активен (утечка value закрыта)")


# ── пул: end-to-end через выделенный порт ──────────────────────────────────

async def _service_and_ca():
    """Поднять CA + локальный https-«сервис» (харнес vault_proxy_test)."""
    ca = VaultCA(Path(tempfile.mkdtemp(prefix="vault_wire_ca_")))
    service = vpt._Service(vpt._service_ctx(ca))
    await service.start()
    return ca, service


def _pool(ca, store):
    """Пул с upstream-trust к нашему CA (реориджин доверяет локальному сервису)."""
    return SessionProxyPool(ca, store, upstream_ssl=vpt._trust_ctx(ca))


async def test_pool_allow_injects_cred():
    """Пул поднимает per-session прокси; ALLOW под префиксом → 200; сервис видит
    впрыснутый Bearer, клиент секрет не слал."""
    if _IMPORT_ERR is not None:
        return _skip(f"ssl-среда недоступна: {_IMPORT_ERR}")
    ca, service = await _service_and_ca()
    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_allow_"))
    store = _store_with(tmp, (
        '[secrets.svc]\n'
        'connector = "generic-bearer"\n'
        f'value = "{_TOKEN}"\n'
        'sessions = ["proj-*"]\n'
        '[secrets.svc.scope]\n'
        f'url_prefixes = ["https://localhost:{service.port}/allowed"]\n'
    ))
    pool = _pool(ca, store)
    try:
        port = await pool.start("proj-1", "svc")
        assert isinstance(port, int) and port > 0
        assert pool.port("proj-1", "svc") == port
        code, text = await vpt._client_get(
            f"http://127.0.0.1:{port}", "localhost", service.port,
            "/allowed/data", vpt._trust_ctx(ca),
        )
        assert code == 200, f"ожидали 200, получили {code}: {text!r}"
        assert f"auth=Bearer {_TOKEN}" in text, f"сервис не увидел кред: {text!r}"
        print("OK пул: per-session прокси, ALLOW под префиксом, Bearer впрыснут сервису")
    finally:
        await pool.stop_all()
        await service.stop()


async def test_pool_deny_out_of_scope():
    """Запрос вне scope → 403 с предписывающим remedy; до сервиса не дошло; кред не утёк."""
    if _IMPORT_ERR is not None:
        return _skip(f"ssl-среда недоступна: {_IMPORT_ERR}")
    ca, service = await _service_and_ca()
    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_deny_"))
    store = _store_with(tmp, (
        '[secrets.svc]\n'
        'connector = "generic-bearer"\n'
        f'value = "{_TOKEN}"\n'
        'sessions = ["*"]\n'
        '[secrets.svc.scope]\n'
        f'url_prefixes = ["https://localhost:{service.port}/allowed"]\n'
    ))
    pool = _pool(ca, store)
    try:
        port = await pool.start("proj-1", "svc")
        code, text = await vpt._client_get(
            f"http://127.0.0.1:{port}", "localhost", service.port,
            "/forbidden/x", vpt._trust_ctx(ca),
        )
        assert code == 403, f"ожидали 403 вне scope, получили {code}: {text!r}"
        assert "scope" in text.lower() or "префикс" in text.lower(), text
        assert f"Bearer {_TOKEN}" not in text, "секрет утёк в DENY-ответе"
        assert service.seen == [], f"запрос дошёл до сервиса вопреки DENY: {service.seen}"
        print("OK пул: вне scope → 403 с remedy, до сервиса не дошло")
    finally:
        await pool.stop_all()
        await service.stop()


async def test_pool_distinct_sessions_distinct_ports_attribution():
    """§4.3: две сессии со СВОИМИ прокси-секретами → разные порты; порт связан с
    (сессия, секрет, scope). Запрос вне scope секрета сессии режется на ЕЁ порту,
    а свой scope — проходит с ЕЁ кредом. Плюс чужая сессия к чужому секрету → отказ."""
    if _IMPORT_ERR is not None:
        return _skip(f"ssl-среда недоступна: {_IMPORT_ERR}")
    ca, service = await _service_and_ca()
    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_attr_"))
    store = _store_with(tmp, (
        '[secrets.svcA]\n'
        'connector = "generic-bearer"\n'
        f'value = "{_TOKEN}"\n'
        'sessions = ["sess-1"]\n'
        '[secrets.svcA.scope]\n'
        f'url_prefixes = ["https://localhost:{service.port}/a"]\n\n'
        '[secrets.svcB]\n'
        'connector = "generic-bearer"\n'
        f'value = "{_TOKEN_B}"\n'
        'sessions = ["sess-2"]\n'
        '[secrets.svcB.scope]\n'
        f'url_prefixes = ["https://localhost:{service.port}/b"]\n'
    ))
    pool = _pool(ca, store)
    try:
        pa = await pool.start("sess-1", "svcA")
        pb = await pool.start("sess-2", "svcB")
        assert pa != pb, f"разные сессии/секреты должны дать разные порты: {pa} {pb}"

        trust = vpt._trust_ctx(ca)
        # Порт A: свой scope /a → ALLOW с кредом A.
        code, text = await vpt._client_get(
            f"http://127.0.0.1:{pa}", "localhost", service.port, "/a/x", trust)
        assert code == 200 and f"auth=Bearer {_TOKEN}" in text, text
        # Порт A: чужой scope /b → DENY (порт связан со scope секрета A).
        code, text = await vpt._client_get(
            f"http://127.0.0.1:{pa}", "localhost", service.port, "/b/x", trust)
        assert code == 403, f"порт A не должен пускать вне scope A: {code} {text!r}"
        # Порт B: свой scope /b → ALLOW с кредом B (не A!).
        code, text = await vpt._client_get(
            f"http://127.0.0.1:{pb}", "localhost", service.port, "/b/y", trust)
        assert code == 200 and f"auth=Bearer {_TOKEN_B}" in text, text
        assert f"Bearer {_TOKEN}" not in text, "порт B впрыснул чужой кред A"

        # Чужая сессия к чужому секрету → отказ поднять прокси (session_allowed).
        try:
            await pool.start("sess-2", "svcA")
            raise AssertionError("ожидали ProxyPoolError: sess-2 не разрешён svcA")
        except ProxyPoolError:
            pass
        print("OK пул: разные сессии → разные порты; атрибуция порт↔(сессия,секрет,scope) верна")
    finally:
        await pool.stop_all()
        await service.stop()


async def test_pool_lifecycle_releases_port():
    """Старт/стоп прокси сессии освобождает порт: после stop pool.port → None и к
    старому порту не подключиться; идемпотентный start отдаёт тот же порт."""
    if _IMPORT_ERR is not None:
        return _skip(f"ssl-среда недоступна: {_IMPORT_ERR}")
    ca, service = await _service_and_ca()
    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_life_"))
    store = _store_with(tmp, (
        '[secrets.svc]\n'
        'connector = "generic-bearer"\n'
        f'value = "{_TOKEN}"\n'
        'sessions = ["*"]\n'
        '[secrets.svc.scope]\n'
        f'url_prefixes = ["https://localhost:{service.port}/allowed"]\n'
    ))
    pool = _pool(ca, store)
    try:
        port = await pool.start("sess-1", "svc")
        # идемпотентность: тот же порт
        assert await pool.start("sess-1", "svc") == port
        assert pool.ports("sess-1") == {"svc": port}
        await pool.stop("sess-1")
        assert pool.port("sess-1", "svc") is None, "после stop порт не должен числиться"
        assert pool.ports("sess-1") == {}
        # к старому порту больше не подключиться (listener закрыт)
        try:
            _, w = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), timeout=2)
            w.close()
            raise AssertionError("старый порт всё ещё принимает соединения")
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            pass
        print("OK пул: stop освобождает порт (pool.port→None, соединение к порту отклонено)")
    finally:
        await pool.stop_all()
        await service.stop()


async def test_daemon_delegates_to_pool():
    """VaultDaemon делегирует start/stop_session_proxy в пул; без пула — понятная ошибка."""
    if _IMPORT_ERR is not None:
        return _skip(f"ssl-среда недоступна: {_IMPORT_ERR}")
    from vault.daemon import VaultDaemon

    ca, service = await _service_and_ca()
    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_daemon_"))
    store = _store_with(tmp, (
        '[secrets.svc]\n'
        'connector = "generic-bearer"\n'
        f'value = "{_TOKEN}"\n'
        'sessions = ["*"]\n'
        '[secrets.svc.scope]\n'
        f'url_prefixes = ["https://localhost:{service.port}/allowed"]\n'
    ))

    class _Host:  # минимальный VaultHost — прокси-путь его не трогает
        async def confirm(self, *a):
            return True

        async def observe(self, *a):
            return None

        def record(self, *a, **k):
            return None

        async def notify_denied(self, *a):
            return None

    # без пула → RuntimeError
    bare = VaultDaemon(store, _Host(), guard_on=True)
    try:
        await bare.start_session_proxy("sess-1", "svc")
        raise AssertionError("без пула ожидали RuntimeError")
    except RuntimeError:
        pass

    pool = _pool(ca, store)
    daemon = VaultDaemon(store, _Host(), guard_on=True, proxies=pool)
    try:
        port = await daemon.start_session_proxy("sess-1", "svc")
        code, text = await vpt._client_get(
            f"http://127.0.0.1:{port}", "localhost", service.port,
            "/allowed/z", vpt._trust_ctx(ca))
        assert code == 200 and f"auth=Bearer {_TOKEN}" in text, text
        await daemon.stop_session_proxies("sess-1")
        assert pool.port("sess-1", "svc") is None
        print("OK демон: делегирует start/stop_session_proxy в пул; без пула — RuntimeError")
    finally:
        await pool.stop_all()
        await service.stop()


async def test_get_denies_proxy_secret_value():
    """Defense-in-depth: /get на АКТИВНЫЙ proxy-секрет (mode=proxy) → 403, значение
    НЕ выдаётся (гейт по mode!='shared', а не по сырому shared)."""
    if _IMPORT_ERR is not None:
        return _skip(f"ssl-среда недоступна: {_IMPORT_ERR}")
    import aiohttp

    from vault.daemon import VaultDaemon

    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_get_"))
    store = _store_with(tmp, (
        '[secrets.psvc]\n'
        'connector = "generic-bearer"\n'
        'value = "PROXY-CRED-DO-NOT-LEAK"\n'
        'sessions = ["*"]\n'
        '[secrets.psvc.scope]\n'
        'url_prefixes = ["https://api.svc/v1"]\n'
    ))

    class _Host:
        async def confirm(self, *a):
            return True

        async def observe(self, *a):
            return None

        def record(self, *a, **k):
            return None

        async def notify_denied(self, *a):
            return None

    daemon = VaultDaemon(store, _Host(), guard_on=True)
    await daemon.start()
    try:
        token = daemon.issue_token("sess-1", tmp)
        async with aiohttp.ClientSession() as http:
            async with http.post(
                f"{daemon.url}/get",
                headers={"Authorization": f"Bearer {token}"},
                json={"secret": "psvc"},
            ) as r:
                assert r.status == 403, f"/get proxy-секрета должен быть 403, был {r.status}"
                data = await r.json()
        assert "PROXY-CRED-DO-NOT-LEAK" not in str(data), data
        print("OK /get: значение proxy-секрета не выдаётся (403, гейт по mode)")
    finally:
        await daemon.stop()


async def test_execute_sub_ignores_proxy_marker():
    """Defense-in-depth: литеральный маркер proxy-секрета в argv НЕ разворачивается
    в значение (execute._sub пропускает is_proxy) — кред не течёт в команду."""
    if _IMPORT_ERR is not None:
        return _skip(f"ssl-среда недоступна: {_IMPORT_ERR}")
    from vault.execute import run_secret_command
    from vault.secret import Secret

    tmp = Path(tempfile.mkdtemp(prefix="vault_wire_sub_"))
    proxy = Secret(
        name="psvc", value="PROXY-CRED-DO-NOT-LEAK", env="", description="",
        sessions=("*",), commands=(), deny=(), allow_unsafe=False,
        confirm=False, shared=False, connector="generic-bearer",
        scope={"url_prefixes": ["https://api.svc/v1"]},
    )
    runner = Secret(
        name="run", value="", env="", description="", sessions=("*",),
        commands=("echo",), deny=(), allow_unsafe=False, confirm=False,
        shared=False,
    )
    all_secrets = {"psvc": proxy, "run": runner}
    code, out, err = await run_secret_command(
        ["echo", "<<wallet:psvc>>"], runner,
        cwd=tmp, all_secrets=all_secrets, session_name="sess-1",
    )
    assert code == 0, (code, err)
    assert b"PROXY-CRED-DO-NOT-LEAK" not in out, out
    print("OK execute: маркер proxy-секрета не разворачивается в argv (кред не течёт)")


def main() -> None:
    if _IMPORT_ERR is not None:
        _skip(f"vault-проводка недоступна (нет ssl/openssl): {_IMPORT_ERR}")
        return
    test_store_parses_connector_and_scope()
    test_store_unknown_connector_inactive()
    test_store_connector_without_value_skipped()
    test_store_connector_rejects_value_leaking_fields()
    for coro in (
        test_pool_allow_injects_cred,
        test_pool_deny_out_of_scope,
        test_pool_distinct_sessions_distinct_ports_attribution,
        test_pool_lifecycle_releases_port,
        test_daemon_delegates_to_pool,
        test_get_denies_proxy_secret_value,
        test_execute_sub_ignores_proxy_marker,
    ):
        asyncio.run(coro())
    print("ALL VAULT-WIRE OK")


if __name__ == "__main__":
    main()
