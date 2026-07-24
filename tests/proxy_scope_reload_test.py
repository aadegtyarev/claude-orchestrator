"""Живая синхронизация scope прокси из secrets.toml по mtime (фаза 4/6 редизайна
claude-box, docs/ARCHITECTURE-claude-box.md §4.6).

Проблема, которую закрывает срез: VaultProxy держал ВЕЧНЫЙ снимок scope, снятый
при подъёме (proxy_pool → dict(secret.scope)). Из-за этого постоянный грант,
записанный ОДНИМ прокси, не видел ДРУГОЙ живой прокси того же секрета до
перезапуска, а операторский отзыв (сужение scope в файле) на живой прокси не
действовал вовсе. Теперь прокси при заданном `store` берёт СВЕЖИЙ scope секрета
из secrets.toml, перечитывая файл по mtime/size (кэш SecretStore — диск не
трогается, если файл не менялся).

Что проверяем (каждый — стык из задания):
  1. грант, дописанный «другим» прокси (просто правка файла) → виден живому
     прокси в следующем запросе БЕЗ перезапуска;
  2. сужение scope в файле (убрали префикс) → прокси DENY на лету (отзыв работает);
  3. СОБСТВЕННЫЙ грант (через ASK-путь, host пишет в файл) виден сразу — в первом
     же следующем запросе того же прокси;
  4. битый файл → fail-safe: не падаем, держим последний валидный scope (доступ
     не сброшен и не расширен);
  5. исчез файл целиком → тот же fail-safe;
  6. секрет исчез из ВАЛИДНОГО файла → honest degrade: пустой scope, хост
     перестаёт быть «своим» (сквозной форвард без инъекции, не ALLOW);
  7. секрет сменил connector / стал не-прокси → тот же honest degrade;
  8. статический режим (без store) → прежнее поведение (снимок + _apply_grant);
  9. перф: кэш store не перечитывает файл, если mtime/size не менялись.

Границы решения — секция «Про решение» в vault/proxy.py::_current_scope.

Автономно: только vault.* + stdlib (держится vault_domain_test через walk_packages,
как остальные vault-тесты). Прокси строим через реальный __init__, но БЕЗ сети —
ca не нужен (__init__ его лишь сохраняет), start() не зовём; проверяем ровно
скоуп-решение через connector.in_scope на живом scope (как tests/ask_grant_test.py).

Запуск: .venv/bin/python tests/proxy_scope_reload_test.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import vault.store as _store_mod  # noqa: E402
    from vault.connectors import GenericBearerConnector  # noqa: E402
    from vault.connectors.contract import HttpReq, ScopeGrant  # noqa: E402
    from vault.host import AskResult  # noqa: E402
    from vault.policy import PolicyEditor  # noqa: E402
    from vault.proxy import VaultProxy  # noqa: E402
    from vault.store import SecretStore  # noqa: E402
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001 — среды без cryptography/tomli мягко скипаем
    _IMPORT_ERR = exc


# Базовый secrets.toml: прокси-секрет generic-bearer с allow- и ask-префиксами
# на одном хосте (api.svc в url_prefixes → он «свой», см. service_hosts).
_SRC = """\
[secrets.svc]
value = "TOKVALUE"
connector = "generic-bearer"
sessions = ["*"]

[secrets.svc.scope]
url_prefixes = ["https://api.svc/v1"]
ask_prefixes = ["https://api.svc/admin"]
"""


def _skip(reason: str) -> bool:
    print(f"SKIP {reason}")
    return True


def _mk(src: str = _SRC, host=None):
    """Store над временным secrets.toml + прокси с этим store под секрет svc."""
    d = Path(tempfile.mkdtemp(prefix="proxy_scope_"))
    path = d / "secrets.toml"
    path.write_text(src)
    os.chmod(path, 0o600)
    store = SecretStore(path)
    secret = store.load()["svc"]
    proxy = VaultProxy(
        None, GenericBearerConnector(), secret, dict(secret.scope),
        store=store, host=host, session_name="dev",
    )
    proxy._ask_timeout = 5.0
    return proxy, store, path


def _rewrite(path: Path, src: str) -> None:
    """Перезаписать файл (0600) и заведомо сдвинуть mtime — чтобы кэш store
    инвалидировался даже при неизменной длине содержимого."""
    path.write_text(src)
    os.chmod(path, 0o600)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def _cleanup(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


def _decide(proxy: VaultProxy, method: str, url: str) -> str:
    """Решение прокси по запросу — ровно как _serve_decrypted маршрутизирует:
    свежий scope → его service_hosts → in_scope. «passthrough» = хост не «свой»
    для секрета (кред НЕ подставляется, сквозной форвард)."""
    scope = proxy._current_scope()
    hosts = proxy._current_service_hosts(scope)
    if (urlsplit(url).hostname or "").lower() not in hosts:
        return "passthrough"
    return proxy.connector.in_scope(HttpReq(method, url), scope).kind


class _GrantingHost:
    """Хост, «нажимающий навсегда»: пишет грант в secrets.toml и рапортует
    persisted=True (как настоящий OrchestratorVaultHost)."""

    def __init__(self, editor: PolicyEditor):
        self.editor = editor
        self.calls: list[str] = []

    async def ask(self, session_name, description, preview, grant=None):
        self.calls.append(preview)
        if grant is None:
            return AskResult(granted=True)
        self.editor.grant_scope(grant.secret, grant.key, grant.value, exist_ok=True)
        return AskResult(granted=True, persisted=True)


# ── 1. грант другого прокси виден живому ─────────────────────────

def test_other_proxy_grant_visible_live():
    """Грант, дописанный в файл «другим» прокси (правка url_prefixes), виден
    ЖИВОМУ прокси в следующем же запросе — без перезапуска."""
    proxy, store, path = _mk()
    # /admin/x под ask-префиксом → ASK (в auto-скоуп не входит).
    assert _decide(proxy, "GET", "https://api.svc/admin/x") == "ask"
    # «другой прокси» дописал грант на этот ресурс в url_prefixes.
    _rewrite(path, _SRC.replace(
        'url_prefixes = ["https://api.svc/v1"]',
        'url_prefixes = ["https://api.svc/v1", "https://api.svc/admin/x"]'))
    # Живой прокси перечитал файл → теперь ALLOW, никто его не перезапускал.
    assert _decide(proxy, "GET", "https://api.svc/admin/x") == "allow"
    _cleanup(path)
    print("OK грант другого прокси: правка файла → следующий запрос ALLOW без перезапуска")


# ── 2. сужение scope действует на лету (отзыв) ───────────────────

def test_narrowing_scope_denies_live():
    """Оператор сузил scope в файле (убрал префикс) → живой прокси DENY на лету."""
    src = _SRC.replace(
        'url_prefixes = ["https://api.svc/v1"]',
        'url_prefixes = ["https://api.svc/v1", "https://api.svc/admin/reboot"]')
    proxy, store, path = _mk(src)
    assert _decide(proxy, "POST", "https://api.svc/admin/reboot") == "allow"
    # Отозвали: вернули url_prefixes к одному /v1 (как /wallet scope -<url>).
    _rewrite(path, _SRC)  # только /v1 в url_prefixes
    assert _decide(proxy, "POST", "https://api.svc/admin/reboot") == "ask", "снят из allow"
    # А ресурс, которого нет и в ask, — прямой DENY на лету.
    assert _decide(proxy, "POST", "https://api.svc/deep/thing") == "deny"
    _cleanup(path)
    print("OK сужение scope: убрали префикс → живой прокси перестал пускать (отзыв на лету)")


# ── 3. собственный грант виден сразу ─────────────────────────────

def test_own_grant_visible_immediately():
    """ASK→«навсегда» этого же прокси: host пишет грант в файл, и ПЕРВЫЙ ЖЕ
    следующий запрос того же прокси идёт ALLOW без нового спроса (перечитал файл)."""
    proxy, store, path = _mk(host=None)  # host выставим гранто-пишущий ниже
    host = _GrantingHost(PolicyEditor(path))
    proxy.host = host

    assert _decide(proxy, "POST", "https://api.svc/admin/reboot") == "ask"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        v = proxy.connector.in_scope(
            HttpReq("POST", "https://api.svc/admin/reboot"), proxy._current_scope())
        granted = loop.run_until_complete(
            proxy._ask_grant(v, "POST", "https://api.svc/admin/reboot"))
        assert granted, "host дал грант"
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert len(host.calls) == 1
    # СРАЗУ же: тот же запрос — ALLOW, без второго спроса.
    assert _decide(proxy, "POST", "https://api.svc/admin/reboot") == "allow"
    assert len(host.calls) == 1, f"переспросил, хотя грант в файле: {host.calls}"
    assert "https://api.svc/admin/reboot" in path.read_text()
    _cleanup(path)
    print("OK собственный грант: записан в файл → следующий запрос ALLOW сразу, без переспроса")


# ── 4. битый файл → fail-safe ────────────────────────────────────

def test_corrupt_file_keeps_last_valid_scope():
    """secrets.toml стал битым → fail-safe: не падаем, держим ПОСЛЕДНИЙ валидный
    scope (уже-разрешённый доступ не сброшен, но и не расширен)."""
    proxy, store, path = _mk()
    assert _decide(proxy, "GET", "https://api.svc/v1/x") == "allow"  # прогрев last-valid
    good = dict(proxy._last_valid_scope)
    _rewrite(path, "это = = не валидный toml [[[\n")
    assert store.load() == {} and store.last_load_ok is False
    # держим последний валидный scope — не {} и не падение
    assert proxy._current_scope() == good
    assert _decide(proxy, "GET", "https://api.svc/v1/x") == "allow", "доступ не сброшен"
    assert _decide(proxy, "GET", "https://api.svc/other") == "deny", "и не расширен"
    _cleanup(path)
    print("OK битый файл: fail-safe — последний валидный scope, без падения и без расширения")


# ── 5. удалили файл целиком → ЭКСТРЕННЫЙ ОТЗЫВ (не held) ──────────

def test_deleted_file_revokes_live():
    """`rm secrets.toml` — экстренный отзыв, действует на лету. Легитимная запись
    (PolicyEditor: mkstemp+os.replace) атомарна и ENOENT-окна не создаёт, поэтому
    отсутствие файла = намеренное удаление → доступ отзываем, а не держим снимок
    (иначе оператор думал бы, что отрубил доступ, а прокси пускал бы — нашло ревью).
    Отзыв = honest-degrade (пустой scope): хост перестаёт быть «своим», кред не
    подставляется — сквозной форвард, а не ALLOW."""
    proxy, store, path = _mk()
    assert _decide(proxy, "GET", "https://api.svc/v1/x") == "allow"
    path.unlink()
    assert store.load() == {} and store.last_load_ok is True, "удаление ≠ ошибка чтения"
    assert proxy._current_scope() == {}, "удалённый файл → пустой scope (отзыв)"
    assert _decide(proxy, "GET", "https://api.svc/v1/x") != "allow", "доступ отозван на лету"
    _cleanup(path)
    print("OK файл удалён: экстренный отзыв на лету (не held), доступ снят")


# ── 6. секрет исчез из валидного файла → honest degrade ──────────

def test_secret_gone_from_valid_file_degrades():
    """Файл валиден, но нашего секрета в нём больше нет → honest degrade: пустой
    scope, хост перестаёт быть «своим» → сквозной форвард без инъекции (не ALLOW)."""
    proxy, store, path = _mk()
    assert _decide(proxy, "GET", "https://api.svc/v1/x") == "allow"
    other = ('[secrets.notsvc]\nvalue = "X"\nconnector = "generic-bearer"\n'
             'sessions = ["*"]\n\n[secrets.notsvc.scope]\n'
             'url_prefixes = ["https://api.svc/v1"]\n')
    _rewrite(path, other)
    scope = proxy._current_scope()
    assert store.last_load_ok is True, "файл валиден"
    assert scope == {}, "секрета нет → пустой scope"
    assert _decide(proxy, "GET", "https://api.svc/v1/x") == "passthrough", "кред не подставляем"
    _cleanup(path)
    print("OK секрет исчез: валидный файл → пустой scope, хост не «свой» (без инъекции), не падаем")


# ── 7. сменился connector / стал не-прокси → honest degrade ──────

def test_secret_changed_connector_or_kind_degrades():
    """Тот же секрет, но сменил connector ЛИБО стал не-прокси → тот же honest
    degrade: пустой scope (не пускаем по старому снимку)."""
    proxy, store, path = _mk()
    proxy._current_scope()  # прогрев
    # (а) сменился connector на другой зарегистрированный (gdocs)
    _rewrite(path, '[secrets.svc]\nvalue = "X"\nconnector = "gdocs"\nsessions = ["*"]\n')
    assert proxy._current_scope() == {} and store.last_load_ok
    # (б) перестал быть прокси-секретом (нет connector → host-passthrough)
    _rewrite(path, '[secrets.svc]\nsessions = ["*"]\ncommands = ["gh"]\n')
    assert proxy._current_scope() == {} and store.last_load_ok
    _cleanup(path)
    print("OK смена connector / не-прокси: пустой scope (honest degrade), не падаем")


# ── 8. статический режим (без store) → как раньше ────────────────

def test_static_mode_without_store_unchanged():
    """Без store scope статичен: снимок из конструктора, а _apply_grant его
    доращивает (прежнее поведение standalone/тестов — правки файла ни при чём)."""
    scope = {"url_prefixes": ["https://api.svc/v1"]}
    proxy = VaultProxy(
        None, GenericBearerConnector(),
        SimpleNamespace(name="svc", value="X"), scope)  # store=None по умолчанию
    assert proxy._current_scope() is proxy.scope, "снимок, не перечитывание"
    assert _decide(proxy, "GET", "https://api.svc/v1/x") == "allow"
    # api.svc — «свой» (в url_prefixes), поэтому не-скоуп это DENY, не passthrough.
    assert _decide(proxy, "GET", "https://api.svc/admin/x") == "deny"
    # _apply_grant расширяет снимок и его service_hosts — как до среза.
    proxy._apply_grant(ScopeGrant(
        key="url_prefixes", value="https://api.svc/admin/x", label="l", secret="svc"))
    assert _decide(proxy, "GET", "https://api.svc/admin/x") == "allow"
    print("OK статический режим: без store — снимок + _apply_grant, прежнее поведение")


# ── 9. перф: кэш store не читает диск впустую ────────────────────

def test_store_cache_avoids_reread_when_unchanged():
    """Между запросами файл не менялся → store НЕ перечитывает/не парсит его
    (иначе диск дёргался бы на каждый запрос). После одной правки — ровно один
    повторный парсинг, дальше снова ноль."""
    proxy, store, path = _mk()
    real = _store_mod.tomllib
    n = {"parse": 0}

    def _counting_loads(s):
        n["parse"] += 1
        return real.loads(s)

    _store_mod.tomllib = types.SimpleNamespace(
        loads=_counting_loads, TOMLDecodeError=real.TOMLDecodeError)
    try:
        _rewrite(path, _SRC)          # одна правка → один парсинг на следующем чтении
        proxy._current_scope()
        assert n["parse"] == 1, n
        for _ in range(5):
            proxy._current_scope()    # файл не менялся → парсинга нет
        assert n["parse"] == 1, f"перечитал неизменный файл: {n}"
    finally:
        _store_mod.tomllib = real
    _cleanup(path)
    print("OK перф: неизменный файл не перечитывается (парсинг ровно на смену mtime/size)")


def main():
    if _IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_IMPORT_ERR}")
    test_other_proxy_grant_visible_live()
    test_narrowing_scope_denies_live()
    test_own_grant_visible_immediately()
    test_corrupt_file_keeps_last_valid_scope()
    test_deleted_file_revokes_live()
    test_secret_gone_from_valid_file_degrades()
    test_secret_changed_connector_or_kind_degrades()
    test_static_mode_without_store_unchanged()
    test_store_cache_avoids_reread_when_unchanged()
    print("ALL PROXY-SCOPE-RELOAD OK")


if __name__ == "__main__":
    main()
