"""Коннектор gdocs (vault/connectors/gdocs) — автономно, без оркестратора.

Проверяет: authorize подставляет OAuth-Bearer; in_scope ALLOW чтения/правки
in-scope документа (UI + Drive/Docs/Sheets/Slides API); DENY чужого docId,
DENY share/export/copy/delete ДАЖЕ для in-scope id, DENY чужого хоста/поддомен-
обмана/кодирования, DENY листинга; remedy перечисляет доступные docs/folders и
предписывает (Р0). Реестр отдаёт gdocs. Автономность пакета (walk_packages).

Запуск: .venv/bin/python tests/vault_gdocs_test.py
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.connectors import (  # noqa: E402
    Connector,
    GDocsConnector,
    HttpReq,
    available,
    get_connector,
)
from vault.secret import Secret  # noqa: E402

DOC = "1AbcDEF_docId-in-scope-000000000000000000000"
DOC2 = "2XyzUVW_secondDoc-in-scope-00000000000000000"
FOLDER = "0BfolderId-in-scope-0000000000000000000000"
OUTSIDER = "9ZZZoutsideDoc-not-in-scope-999999999999999"

SCOPE = {"docs": [DOC, DOC2], "folders": [FOLDER]}


def _secret(value: str = "OAUTH-ACCESS-TOKEN") -> Secret:
    return Secret(
        name="gd", value=value, env="", description="", sessions=("*",),
        commands=(), deny=(), allow_unsafe=False, confirm=False, shared=False,
    )


def _req(url: str, method: str = "GET", headers: dict | None = None) -> HttpReq:
    return HttpReq(method=method, url=url, headers=dict(headers or {}))


def test_authorize_sets_oauth_bearer():
    conn = GDocsConnector()
    req = _req("https://docs.googleapis.com/v1/documents/x", headers={"Accept": "*/*"})
    out = conn.authorize(req, _secret("TOK123"))
    assert out.headers["Authorization"] == "Bearer TOK123"
    assert out.headers["Accept"] == "*/*"                     # прочие сохранены
    assert "Authorization" not in req.headers                 # исходный не мутирован
    print("OK authorize: OAuth-Bearer подставлен, исходный req не тронут")


def test_allow_read_edit_in_scope():
    conn = GDocsConnector()
    # UI-хосты
    assert conn.in_scope(_req(f"https://docs.google.com/document/d/{DOC}/edit"), SCOPE).is_allow
    assert conn.in_scope(_req(f"https://docs.google.com/spreadsheets/d/{DOC2}/edit"), SCOPE).is_allow
    assert conn.in_scope(_req(f"https://docs.google.com/presentation/d/{DOC}/view"), SCOPE).is_allow
    assert conn.in_scope(_req(f"https://drive.google.com/file/d/{DOC}/view"), SCOPE).is_allow
    assert conn.in_scope(_req(f"https://drive.google.com/open?id={DOC}"), SCOPE).is_allow
    # REST API
    assert conn.in_scope(_req(f"https://www.googleapis.com/drive/v3/files/{DOC}"), SCOPE).is_allow
    assert conn.in_scope(_req(f"https://www.googleapis.com/drive/v3/files/{DOC}?alt=media"), SCOPE).is_allow
    assert conn.in_scope(_req(f"https://docs.googleapis.com/v1/documents/{DOC}"), SCOPE).is_allow
    # правка через custom-verb :batchUpdate (POST)
    assert conn.in_scope(
        _req(f"https://docs.googleapis.com/v1/documents/{DOC}:batchUpdate", "POST"), SCOPE
    ).is_allow
    assert conn.in_scope(
        _req(f"https://sheets.googleapis.com/v4/spreadsheets/{DOC}:batchUpdate", "POST"), SCOPE
    ).is_allow
    # чтение диапазона Sheets (двоеточие в диапазоне не путается с verb)
    assert conn.in_scope(
        _req(f"https://sheets.googleapis.com/v4/spreadsheets/{DOC}/values/Sheet1!A1:B2"), SCOPE
    ).is_allow
    assert conn.in_scope(
        _req(f"https://slides.googleapis.com/v1/presentations/{DOC}"), SCOPE
    ).is_allow
    # папка из scope как адресуемый ресурс
    assert conn.in_scope(_req(f"https://drive.google.com/drive/folders/{FOLDER}"), SCOPE).is_allow
    # регистр хоста не важен
    assert conn.in_scope(_req(f"HTTPS://DOCS.GOOGLE.COM/document/d/{DOC}/edit"), SCOPE).is_allow
    # явный :443 эквивалентен отсутствию порта
    assert conn.in_scope(_req(f"https://www.googleapis.com:443/drive/v3/files/{DOC}"), SCOPE).is_allow
    print("OK in_scope ALLOW: чтение/правка in-scope документа (UI + Drive/Docs/Sheets/Slides)")


def test_deny_foreign_doc():
    conn = GDocsConnector()
    for url in (
        f"https://docs.google.com/document/d/{OUTSIDER}/edit",
        f"https://www.googleapis.com/drive/v3/files/{OUTSIDER}",
        f"https://docs.googleapis.com/v1/documents/{OUTSIDER}:batchUpdate",
    ):
        v = conn.in_scope(_req(url), SCOPE)
        assert v.is_deny and v.remedy, url
        assert OUTSIDER in v.reason, url
    print("OK in_scope DENY: документ вне scope (UI + API)")


def test_deny_dangerous_ops_even_in_scope():
    conn = GDocsConnector()
    # шаринг/permissions, экспорт, копирование, подписка, ревизии — по IN-SCOPE id
    dangerous = (
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}/permissions", "POST"),
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}/permissions/anyone", "PATCH"),
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}/export?mimeType=application/pdf"),
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}/copy", "POST"),
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}/watch", "POST"),
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}/revisions"),
        _req(f"https://docs.google.com/spreadsheets/d/{DOC}/export?format=xlsx"),
        _req(f"https://docs.google.com/document/d/{DOC}/copy"),
        _req(f"https://drive.google.com/uc?export=download&id={DOC}"),
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}", "DELETE"),
    )
    for req in dangerous:
        v = conn.in_scope(req, SCOPE)
        assert v.is_deny and v.remedy, req.url
        # даже in-scope id — remedy предписывает и перечисляет разрешённое
        assert DOC in v.remedy, req.url
    print("OK in_scope DENY: share/export/copy/watch/revisions/delete даже для in-scope id")


def test_deny_metadata_mutation():
    conn = GDocsConnector()
    # перенос/переименование/корзина через Drive files PATCH/PUT — эксфильтрация
    # одним запросом; тело не нужно (addParents/removeParents/name — чисто query).
    for req in (
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}?addParents={OUTSIDER}", "PATCH"),
        _req(
            f"https://www.googleapis.com/drive/v3/files/{DOC}?removeParents={FOLDER}",
            "PATCH",
        ),
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}?name=hacked", "PATCH"),
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}", "PUT"),
        _req(f"https://www.googleapis.com/upload/drive/v3/files/{DOC}?uploadType=media", "PATCH"),
    ):
        v = conn.in_scope(req, SCOPE)
        assert v.is_deny and v.remedy, req.url
        assert DOC in v.remedy, req.url          # in-scope id, но операция запрещена
    print("OK in_scope DENY: PATCH/PUT метаданных Drive files (перенос/переименование)")


def test_allow_content_write_endpoints():
    conn = GDocsConnector()
    # правка СОДЕРЖИМОГО через специфичные endpoint'ы — ALLOW (in-scope)
    assert conn.in_scope(
        _req(f"https://docs.googleapis.com/v1/documents/{DOC}:batchUpdate", "PATCH"), SCOPE
    ).is_allow
    assert conn.in_scope(
        _req(f"https://sheets.googleapis.com/v4/spreadsheets/{DOC}/values/Sheet1!A1:B2", "PUT"),
        SCOPE,
    ).is_allow
    assert conn.in_scope(
        _req(f"https://sheets.googleapis.com/v4/spreadsheets/{DOC}/values:batchUpdate", "POST"),
        SCOPE,
    ).is_allow
    print("OK in_scope ALLOW: content-write (:batchUpdate/values) для in-scope id")


def test_deny_forged_content_write_on_drive_host():
    conn = GDocsConnector()
    # Подделка content-write формы на Drive-хосте: у Drive НЕТ files:batchUpdate /
    # files/<id>/values — это прикрытие переноса/переименования метаданных через
    # query. Проверка host+marker → форма невалидна → DENY (PATCH метаданных).
    for req in (
        _req(
            f"https://www.googleapis.com/drive/v3/files/{DOC}:batchUpdate?addParents={OUTSIDER}",
            "PATCH",
        ),
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}/values?name=pwned", "PATCH"),
        # verb batchUpdate на правильном marker, но на Drive-хосте — тоже DENY
        _req(f"https://www.googleapis.com/drive/v3/documents/{DOC}:batchUpdate?name=x", "PATCH"),
    ):
        v = conn.in_scope(req, SCOPE)
        assert v.is_deny and v.remedy, req.url
        assert DOC in v.remedy, req.url
    print("OK in_scope DENY: подделка content-write формы на Drive-хосте (перенос/переименование)")


def test_deny_ambiguous_query_id():
    conn = GDocsConnector()
    # дубль id-ключа (parser differential)
    assert conn.in_scope(_req(f"https://drive.google.com/open?id={DOC}&id={OUTSIDER}"), SCOPE).is_deny
    assert conn.in_scope(_req(f"https://drive.google.com/open?id={OUTSIDER}&id={DOC}"), SCOPE).is_deny
    # разные id-ключи с разными значениями
    assert conn.in_scope(
        _req(f"https://drive.google.com/uc?fileId={DOC}&id={OUTSIDER}"), SCOPE
    ).is_deny
    # id в пути ≠ id в query
    assert conn.in_scope(
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}?fileId={OUTSIDER}"), SCOPE
    ).is_deny
    print("OK in_scope DENY: неоднозначный id (дубль ключа / путь≠query)")


def test_deny_malformed_ipv6_no_crash():
    conn = GDocsConnector()
    # кривая IPv6-скобка роняла urlsplit ValueError'ом — теперь DENY без краша
    v = conn.in_scope(_req(f"https://[::1]docs.google.com/document/d/{DOC}/edit"), SCOPE)
    assert v.is_deny and v.remedy
    print("OK in_scope DENY: malformed IPv6 → отказ без краша")


def test_deny_listing_and_create():
    conn = GDocsConnector()
    for url, method in (
        ("https://www.googleapis.com/drive/v3/files", "GET"),                     # листинг всего
        ("https://www.googleapis.com/drive/v3/files?q=name+contains+'x'", "GET"),  # листинг с q
        ("https://docs.googleapis.com/v1/documents", "POST"),                     # создание дока
        ("https://slides.googleapis.com/v1/presentations", "POST"),               # создание слайдов
    ):
        v = conn.in_scope(_req(url, method), SCOPE)
        assert v.is_deny and v.remedy, url
    print("OK in_scope DENY: листинг коллекции и создание без конкретного id")


def test_deny_foreign_host_and_subdomain_trick():
    conn = GDocsConnector()
    for url in (
        f"https://evil.com/document/d/{DOC}/edit",              # чужой хост
        f"https://docs.google.com.evil.com/document/d/{DOC}/edit",  # поддомен-обман
        f"https://docs.google.com@evil.com/document/d/{DOC}/edit",  # userinfo-обман
        f"https://sub.docs.google.com/document/d/{DOC}/edit",   # лишний поддомен
        f"http://docs.google.com/document/d/{DOC}/edit",        # не https
        f"https://docs.google.com:8443/document/d/{DOC}/edit",  # нестандартный порт
    ):
        v = conn.in_scope(_req(url), SCOPE)
        assert v.is_deny and v.remedy, url
    print("OK in_scope DENY: чужой хост, поддомен/userinfo-обман, http, нестандартный порт")


def test_deny_encoding_and_traversal():
    conn = GDocsConnector()
    # traversal из in-scope id к чужому ресурсу — normpath резолвит → DENY
    assert conn.in_scope(
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}/../files/{OUTSIDER}"), SCOPE
    ).is_deny
    # percent-encoded traversal
    assert conn.in_scope(
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}%2f..%2ffiles%2f{OUTSIDER}"), SCOPE
    ).is_deny
    # двойное кодирование `..` (%252e%252e) — декод до неподвижной точки раскрывает → DENY
    assert conn.in_scope(
        _req(f"https://www.googleapis.com/drive/v3/files/{DOC}/%252e%252e/{OUTSIDER}"), SCOPE
    ).is_deny
    print("OK in_scope DENY: traversal и много-кодирование не обходят скоуп")


def test_remedy_lists_scope_and_prescribes():
    conn = GDocsConnector()
    v = conn.in_scope(_req(f"https://docs.google.com/document/d/{OUTSIDER}/edit"), SCOPE)
    assert v.is_deny
    r = v.remedy
    # перечисляет доступные docs И folders (кэш скоупа)
    assert DOC in r and DOC2 in r and FOLDER in r
    # называет запрещённое (шаринг/экспорт) и предписывает действие (оператор/scope)
    assert "permissions" in r and "export" in r
    assert "оператора" in r and "scope" in r
    print("OK remedy: перечисляет docs/folders, называет запрещённое, предписывает")


def test_empty_scope_denies():
    conn = GDocsConnector()
    v = conn.in_scope(_req(f"https://docs.google.com/document/d/{DOC}/edit"), {})
    assert v.is_deny and v.remedy
    assert "scope.docs" in v.remedy or "scope.folders" in v.remedy
    print("OK in_scope: пустой scope → DENY с подсказкой про scope.docs/folders")


def test_optional_capabilities_stubbed():
    conn = GDocsConnector()
    assert conn.oauth_flow() is None                          # live-OAuth — следующий срез
    assert conn.mint({}) is None
    assert conn.refresh(_secret()) is None
    assert conn.resolve_scope({"team": "X"}) == {"team": "X"}  # резолв — следующий срез
    print("OK gdocs: oauth/mint/refresh/resolve_scope — заглушки (live — следующий срез)")


def test_registry_returns_gdocs():
    assert "gdocs" in available()
    conn = get_connector("gdocs")
    assert conn is not None and conn.name == "gdocs"
    assert isinstance(conn, Connector)                        # @runtime_checkable
    # публичный host-set экспонирован для VaultProxy.service_hosts (интеграция)
    assert "docs.google.com" in conn.service_hosts
    assert "www.googleapis.com" in conn.service_hosts
    print("OK реестр: gdocs зарегистрирован, соответствует контракту, service_hosts экспонирован")


def test_no_orchestrator_dependency():
    """vault.connectors (включая gdocs) импортируется без orchestrator."""
    root = str(Path(__file__).parent.parent)
    code = (
        f"import sys; sys.path.insert(0, {root!r});"
        "import importlib, pkgutil, vault.connectors as c;"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(c.__path__, 'vault.connectors.')];"
        "leaked=[m for m in sys.modules if m=='orchestrator' or m.startswith('orchestrator.')];"
        "sys.exit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"gdocs затянул orchestrator:\n{r.stdout}\n{r.stderr}"
    print("OK vault.connectors автономен: свежий процесс без orchestrator")


def main():
    test_authorize_sets_oauth_bearer()
    test_allow_read_edit_in_scope()
    test_deny_foreign_doc()
    test_deny_dangerous_ops_even_in_scope()
    test_deny_metadata_mutation()
    test_allow_content_write_endpoints()
    test_deny_forged_content_write_on_drive_host()
    test_deny_ambiguous_query_id()
    test_deny_malformed_ipv6_no_crash()
    test_deny_listing_and_create()
    test_deny_foreign_host_and_subdomain_trick()
    test_deny_encoding_and_traversal()
    test_remedy_lists_scope_and_prescribes()
    test_empty_scope_denies()
    test_optional_capabilities_stubbed()
    test_registry_returns_gdocs()
    test_no_orchestrator_dependency()
    print("ALL VAULT-GDOCS OK")


if __name__ == "__main__":
    main()
