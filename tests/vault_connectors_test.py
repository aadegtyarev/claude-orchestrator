"""Коннекторы Vault (vault/connectors) — автономно, без оркестратора.

Проверяет: authorize подставляет Bearer-заголовок; in_scope ALLOW под префиксом
и DENY вне (с предписывающим remedy, Р0); нормализацию URL против traversal;
реестр (известный/неизвестный коннектор, громкий лог); автономность пакета.

Запуск: .venv/bin/python tests/vault_connectors_test.py
"""
import logging
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.connectors import (  # noqa: E402
    Connector,
    GenericBearerConnector,
    HttpReq,
    ScopeVerdict,
    available,
    get_connector,
)
from vault.secret import Secret  # noqa: E402


def _secret(value: str = "TOK") -> Secret:
    return Secret(
        name="svc", value=value, env="", description="", sessions=("*",),
        commands=(), deny=(), allow_unsafe=False, confirm=False, shared=False,
    )


def _req(url: str, headers: dict | None = None) -> HttpReq:
    return HttpReq(method="GET", url=url, headers=dict(headers or {}))


def test_authorize_sets_bearer():
    conn = GenericBearerConnector()
    req = _req("https://api.svc/v1/x", {"Accept": "application/json"})
    out = conn.authorize(req, _secret("SEKRET"))
    assert out.headers["Authorization"] == "Bearer SEKRET"
    assert out.headers["Accept"] == "application/json"        # прочие сохранены
    assert "Authorization" not in req.headers                 # исходный не мутирован
    print("OK authorize: Bearer подставлен, исходный req не тронут")


def test_authorize_overrides_existing():
    conn = GenericBearerConnector()
    req = _req("https://api.svc/v1/x", {"authorization": "Bearer OLD"})
    out = conn.authorize(req, _secret("NEW"))
    # старый заголовок (иной регистр) заменён, дубля нет
    auth = [v for k, v in out.headers.items() if k.lower() == "authorization"]
    assert auth == ["Bearer NEW"], out.headers
    print("OK authorize: существующий Authorization заменён (без учёта регистра)")


def test_in_scope_allow():
    conn = GenericBearerConnector()
    scope = {"url_prefixes": ["https://api.svc/v1/", "https://api.svc/v2/"]}
    assert conn.in_scope(_req("https://api.svc/v1/docs/42"), scope).is_allow
    assert conn.in_scope(_req("https://api.svc/v1/"), scope).is_allow      # сам префикс
    assert conn.in_scope(_req("https://api.svc/v2/jobs"), scope).is_allow
    # регистр схемы/хоста не важен
    assert conn.in_scope(_req("HTTPS://API.SVC/v1/docs"), scope).is_allow
    print("OK in_scope ALLOW: под префиксом, включая сам префикс и регистр")


def test_in_scope_deny_outside():
    conn = GenericBearerConnector()
    scope = {"url_prefixes": ["https://api.svc/v1/"]}
    v = conn.in_scope(_req("https://api.svc/admin"), scope)
    assert v.is_deny and v.remedy                              # remedy обязателен (Р0)
    assert "https://api.svc/v1/" in v.remedy                   # перечислен доступный
    assert v.reason and "вне выданного скоупа" in v.reason
    # граница сегмента: /v1abc не под /v1/
    assert conn.in_scope(_req("https://api.svc/v1abc"), scope).is_deny
    # другой хост/схема — вне
    assert conn.in_scope(_req("https://evil.svc/v1/x"), scope).is_deny
    assert conn.in_scope(_req("http://api.svc/v1/x"), scope).is_deny
    print("OK in_scope DENY: вне префикса, граница сегмента, чужой хост/схема + remedy")


def test_in_scope_traversal_blocked():
    conn = GenericBearerConnector()
    scope = {"url_prefixes": ["https://api.svc/v1/"]}
    # dot-traversal и percent-encoded — резолвятся ДО проверки → DENY
    assert conn.in_scope(_req("https://api.svc/v1/../admin"), scope).is_deny
    assert conn.in_scope(_req("https://api.svc/v1/%2e%2e/admin"), scope).is_deny
    assert conn.in_scope(_req("https://api.svc/v1%2f..%2fadmin"), scope).is_deny
    print("OK in_scope: traversal (../ и %2e/%2f) не обходит скоуп")


def test_in_scope_multi_encoding_blocked():
    conn = GenericBearerConnector()
    scope = {"url_prefixes": ["https://api.svc/v1/"]}
    # двойное/тройное кодирование `..` и `/` — цикл-декод раскрывает → DENY
    assert conn.in_scope(_req("https://api.svc/v1/%252e%252e/admin"), scope).is_deny
    assert conn.in_scope(_req("https://api.svc/v1/..%252fadmin"), scope).is_deny
    assert conn.in_scope(_req("https://api.svc/v1/%25252e%25252e/admin"), scope).is_deny
    # многослойное кодирование самого /v1/ тоже не даёт ложный ALLOW на /admin
    assert conn.in_scope(_req("https://api.svc/v1%252f..%252fadmin"), scope).is_deny
    print("OK in_scope: двойное/тройное кодирование ../ и / не обходит скоуп")


def test_in_scope_default_port_normalized():
    conn = GenericBearerConnector()
    scope = {"url_prefixes": ["https://api.svc/v1/", "http://plain.svc/a/"]}
    # явный дефолтный порт эквивалентен его отсутствию
    assert conn.in_scope(_req("https://api.svc:443/v1/docs"), scope).is_allow
    assert conn.in_scope(_req("http://plain.svc:80/a/b"), scope).is_allow
    # НЕдефолтный порт — другой netloc, вне скоупа
    assert conn.in_scope(_req("https://api.svc:8443/v1/docs"), scope).is_deny
    # и наоборот: префикс с явным :443 матчит запрос без порта
    scope2 = {"url_prefixes": ["https://api.svc:443/v1/"]}
    assert conn.in_scope(_req("https://api.svc/v1/x"), scope2).is_allow
    print("OK in_scope: дефолтный порт :443/:80 нормализуется, :8443 — нет")


def _capture_warnings(fn):
    """Выполнить fn под перехватом WARNING логгера vault.connectors (без pytest
    caplog — тест обязан работать и standalone из main())."""
    logger = logging.getLogger("vault.connectors")
    records = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        result = fn()
    finally:
        logger.removeHandler(handler)
    return result, records


def test_in_scope_prefix_without_scheme_warns_and_ignored():
    conn = GenericBearerConnector()
    scope = {"url_prefixes": ["api.svc/v1/"]}      # нет схемы — непригоден
    v, records = _capture_warnings(
        lambda: conn.in_scope(_req("https://api.svc/v1/x"), scope)
    )
    assert v.is_deny                               # битый префикс не открывает доступ
    assert any(r.levelno == logging.WARNING for r in records), "ждали WARNING"
    print("OK in_scope: префикс без схемы → громкий WARNING и игнор (DENY)")


def test_in_scope_empty_scope_denies():
    conn = GenericBearerConnector()
    v = conn.in_scope(_req("https://api.svc/v1/x"), {})
    assert v.is_deny and v.remedy and "url_prefixes" in v.remedy
    print("OK in_scope: пустой scope → DENY с подсказкой про url_prefixes")


def test_optional_capabilities_unsupported():
    conn = GenericBearerConnector()
    assert conn.oauth_flow() is None
    assert conn.mint({}) is None
    assert conn.refresh(_secret()) is None
    assert conn.resolve_scope({"a": 1}) == {"a": 1}            # без резолва — как есть
    print("OK generic-bearer: oauth/mint/refresh не поддержаны, resolve_scope=id")


def test_registry_known_and_unknown():
    assert "generic-bearer" in available()
    assert get_connector("generic-bearer") is not None
    # неизвестный → None + громкий WARNING
    logger = logging.getLogger("vault.connectors")
    records = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        assert get_connector("nope-does-not-exist") is None
    finally:
        logger.removeHandler(handler)
    assert any(r.levelno == logging.WARNING for r in records), "ждали громкий WARNING"
    print("OK реестр: известный → коннектор, неизвестный → None + WARNING")


def test_scope_verdict_invariants():
    assert ScopeVerdict.allow().is_allow
    for bad in ("", "   ", "\t\n"):          # пустой И пробельный remedy/descr
        try:
            ScopeVerdict.deny("r", bad)      # DENY без внятного remedy — запрещён (Р0)
            raise AssertionError(f"ждали ValueError на DENY remedy={bad!r}")
        except ValueError:
            pass
        try:
            ScopeVerdict.ask(bad)            # ASK без внятного descr — запрещён
            raise AssertionError(f"ждали ValueError на ASK descr={bad!r}")
        except ValueError:
            pass
    assert ScopeVerdict.ask("нужен доступ к доку Х").is_ask
    print("OK ScopeVerdict: инварианты allow/deny(remedy)/ask(descr), пробельные тоже")


def test_connector_runtime_checkable():
    # реальный isinstance (Protocol @runtime_checkable): коннектор проходит…
    assert isinstance(GenericBearerConnector(), Connector)

    class Broken:                            # …а класс без методов — нет
        name = "broken"

    assert not isinstance(Broken(), Connector)
    print("OK Connector: @runtime_checkable ловит наличие методов")


def test_no_orchestrator_dependency():
    """vault.connectors импортируется в СВЕЖЕМ процессе без orchestrator."""
    root = str(Path(__file__).parent.parent)
    code = (
        f"import sys; sys.path.insert(0, {root!r});"
        "import importlib, pkgutil, vault.connectors as c;"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(c.__path__, 'vault.connectors.')];"
        "leaked=[m for m in sys.modules if m=='orchestrator' or m.startswith('orchestrator.')];"
        "sys.exit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"connectors затянул orchestrator:\n{r.stdout}\n{r.stderr}"
    print("OK vault.connectors автономен: свежий процесс без orchestrator")


def main():
    test_authorize_sets_bearer()
    test_authorize_overrides_existing()
    test_in_scope_allow()
    test_in_scope_deny_outside()
    test_in_scope_traversal_blocked()
    test_in_scope_multi_encoding_blocked()
    test_in_scope_default_port_normalized()
    test_in_scope_prefix_without_scheme_warns_and_ignored()
    test_in_scope_empty_scope_denies()
    test_optional_capabilities_unsupported()
    test_registry_known_and_unknown()
    test_scope_verdict_invariants()
    test_connector_runtime_checkable()
    test_no_orchestrator_dependency()
    print("ALL VAULT-CONNECTORS OK")


if __name__ == "__main__":
    main()
