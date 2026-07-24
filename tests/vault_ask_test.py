"""ASK-flow (§4.6 docs/ARCHITECTURE-claude-box.md) — vault-сторона плумбинга,
фаза 2. Проверяем, что `ScopeVerdict.ask` РЕАЛЬНО спрашивает грант через host (а
не тихо DENY), с таймаутом и per-request грантом, не вешая прокси (Р0).

Схема — переиспользует e2e-харнес vault_proxy_test (локальный HTTPS-«сервис» на
Vault-CA-серте, ручной CONNECT+MITM-клиент, trust к CA). Отличие: scope с
`ask_prefixes` → generic-bearer поднимает ASK; в прокси кладём фейк-host,
отвечающий на ask True/False/зависание.

Проверяем: (1) URL под ask-префиксом → in_scope=ASK; host.ask позван с именем
сессии; True → кред впрыснут + реоригин (сервис видит Bearer), 200; (2) False →
403 с remedy, к сервису НЕ пошло; (3) зависание host.ask → размыкается по
ask_timeout → DENY, прокси не виснет; (4) обычные allow/deny под url_prefixes НЕ
изменились, host.ask НЕ зовётся; (5) TtyVaultHost.ask: assume_yes→True, без
tty→False, конкурентные сериализованы (как confirm). Всё под таймаутами.

Запуск: .venv/bin/python tests/vault_ask_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))  # для import vault_proxy_test

# Переиспользуем e2e-харнес слайса 2.3 (сервис/клиент/CA/секрет).
from vault_proxy_test import (  # noqa: E402
    _IMPORT_ERR,
    _SECRET_VALUE,
    _TIMEOUT,
    _Service,
    _client_get,
    _mk_secret,
    _service_ctx,
    _skip,
    _trust_ctx,
)

try:
    import tempfile

    from vault.connectors import GenericBearerConnector  # noqa: E402
    from vault.connectors.contract import HttpReq  # noqa: E402
    from vault.proxy import VaultProxy  # noqa: E402
    from vault.tls import VaultCA  # noqa: E402
    from vault.tty_host import TtyVaultHost  # noqa: E402
    _ASK_IMPORT_ERR = _IMPORT_ERR
except Exception as exc:  # noqa: BLE001
    _ASK_IMPORT_ERR = exc

_SESSION = "dev-ask"


class _FakeHost:
    """Фейк-VaultHost: прокси в ASK-пути зовёт ТОЛЬКО ask. answer — bool или
    "hang" (симулирует host без своего таймаута → прокси обязан разомкнуть сам)."""

    def __init__(self, answer) -> None:
        self._answer = answer
        self.calls: list[tuple[str, str, str]] = []

    async def ask(self, session_name: str, description: str, preview: str) -> bool:
        self.calls.append((session_name, description, preview))
        if self._answer == "hang":
            await asyncio.sleep(3600)  # никогда не отвечаем — ждём таймаут прокси
        return bool(self._answer)


async def _setup_ask(answer, *, ask_timeout: float = 5.0):
    """CA+сервис+прокси со scope, где /ask/* — ask-префикс, /allowed/* — allow.
    Возвращает (ca, service, proxy, trust, host)."""
    ca = VaultCA(Path(tempfile.mkdtemp(prefix="vault_ask_")))
    service = _Service(_service_ctx(ca))
    await service.start()
    scope = {
        "url_prefixes": [f"https://localhost:{service.port}/allowed"],
        "ask_prefixes": [f"https://localhost:{service.port}/ask"],
    }
    host = _FakeHost(answer)
    proxy = VaultProxy(
        ca, GenericBearerConnector(), _mk_secret(), scope,
        host=host, session_name=_SESSION,
        upstream_ssl=_trust_ctx(ca), ask_timeout=ask_timeout,
    )
    await proxy.start()
    return ca, service, proxy, _trust_ctx(ca), host


# --- уровень коннектора: ask_prefixes → ASK -----------------------------------

def test_generic_bearer_emits_ask():
    """URL под ask-префиксом (и не под url_prefixes) → ASK с descr; под
    url_prefixes → ALLOW; вне обоих → DENY. Пересечение (allow>ask) → ALLOW."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    c = GenericBearerConnector()
    scope = {
        "url_prefixes": ["https://api.svc/v1"],
        "ask_prefixes": ["https://api.svc/admin", "https://api.svc/v1"],  # v1 и в allow
    }
    assert c.in_scope(HttpReq("GET", "https://api.svc/v1/x"), scope).is_allow
    ask = c.in_scope(HttpReq("POST", "https://api.svc/admin/reboot"), scope)
    assert ask.is_ask, ask
    assert ask.descr and "admin" in ask.descr
    assert c.in_scope(HttpReq("GET", "https://api.svc/other"), scope).is_deny
    # только ask_prefixes, без url_prefixes — не ранний DENY «нет скоупа»
    ask_only = {"ask_prefixes": ["https://api.svc/admin"]}
    assert c.in_scope(HttpReq("GET", "https://api.svc/admin/x"), ask_only).is_ask
    assert c.in_scope(HttpReq("GET", "https://api.svc/nope"), ask_only).is_deny
    print("OK generic-bearer: ask_prefixes → ASK (allow важнее ask, вне обоих → DENY)")


# --- e2e через прокси ---------------------------------------------------------

async def test_ask_grant_true_injects():
    """ASK + host.ask→True → кред впрыснут, реоригин, сервис видит Bearer (200)."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    ca, service, proxy, trust, host = await _setup_ask(True)
    try:
        code, text = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/ask/reboot", trust
        )
        assert code == 200, f"ожидали 200 на грант, получили {code}: {text!r}"
        assert f"auth=Bearer {_SECRET_VALUE}" in text, f"кред не впрыснут: {text!r}"
        assert service.seen == ["/ask/reboot"], f"к сервису не дошло: {service.seen}"
        # host.ask позван с именем сессии; секрет в спрос не передан.
        assert len(host.calls) == 1, host.calls
        sess, descr, preview = host.calls[0]
        assert sess == _SESSION and _SECRET_VALUE not in (descr + preview)
        print("OK ASK: host.ask→True → кред впрыснут + реоригин, сервис видит Bearer")
    finally:
        await proxy.stop()
        await service.stop()


async def test_ask_grant_false_denied():
    """ASK + host.ask→False → 403 с remedy, к сервису НЕ пошло, секрет не утёк."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    ca, service, proxy, trust, host = await _setup_ask(False)
    try:
        code, text = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/ask/reboot", trust
        )
        assert code == 403, f"ожидали 403 на отказ, получили {code}: {text!r}"
        # _client_get отдаёт latin-1-текст, а тело UTF-8 — восстановим для проверки.
        body = text.encode("latin-1").decode("utf-8", "replace")
        assert "оператор" in body.lower() or "подтверд" in body.lower(), (
            f"нет предписывающего remedy в 403: {body!r}"
        )
        assert service.seen == [], f"запрос дошёл до сервиса вопреки отказу: {service.seen}"
        assert _SECRET_VALUE not in text, "секрет утёк в ASK-DENY-ответе"
        assert len(host.calls) == 1, host.calls
        print("OK ASK: host.ask→False → 403 remedy, к сервису не пошло, секрет не утёк")
    finally:
        await proxy.stop()
        await service.stop()


async def test_ask_timeout_denies_without_hang():
    """host.ask зависает (нет своего таймаута) → прокси размыкает по ask_timeout →
    403 (DENY), к сервису не пошло; прокси остаётся живым (не завис)."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    # Малый ask_timeout — застой размыкается быстро, без 3-мин ожидания.
    ca, service, proxy, trust, host = await _setup_ask("hang", ask_timeout=0.5)
    try:
        code, text = await asyncio.wait_for(
            _client_get(proxy.proxy_url, "localhost", service.port, "/ask/x", trust),
            timeout=_TIMEOUT,
        )
        assert code == 403, f"ожидали 403 на таймаут ask, получили {code}: {text!r}"
        assert service.seen == [], f"дошло до сервиса при таймауте ask: {service.seen}"
        assert _SECRET_VALUE not in text, "секрет утёк при таймауте ask"
        # Прокси жив: следующий (allow) запрос проходит.
        code2, _ = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/allowed/ok", trust
        )
        assert code2 == 200, f"прокси завис после таймаута ask: {code2}"
        print("OK ASK: зависший host.ask размыкается по ask_timeout → DENY, прокси жив")
    finally:
        await proxy.stop()
        await service.stop()


async def test_ask_grant_is_per_request_no_cache():
    """Грант эфемерный/per-request: 2-й запрос под тем же ask-префиксом СНОВА
    зовёт host.ask (нет кэша гранта). Ревью проверило живьём — закрепляем."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    ca, service, proxy, trust, host = await _setup_ask(True)
    try:
        for i in range(2):
            code, _ = await _client_get(
                proxy.proxy_url, "localhost", service.port, f"/ask/r{i}", trust
            )
            assert code == 200, f"запрос {i} не 200: {code}"
        assert len(host.calls) == 2, (
            f"грант закэширован — host.ask не переспросил на 2-м запросе: {host.calls}"
        )
        assert service.seen == ["/ask/r0", "/ask/r1"], service.seen
        print("OK ASK: грант per-request — 2-й запрос снова зовёт host.ask (нет кэша)")
    finally:
        await proxy.stop()
        await service.stop()


async def test_allow_deny_unchanged_no_ask():
    """Обычные пути под url_prefixes НЕ трогают host.ask: allow→200 (Bearer),
    вне скоупа→403; host.ask не вызывался ни разу."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    ca, service, proxy, trust, host = await _setup_ask(True)
    try:
        code_a, text_a = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/allowed/data", trust
        )
        assert code_a == 200 and f"auth=Bearer {_SECRET_VALUE}" in text_a, text_a
        code_d, text_d = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/forbidden/x", trust
        )
        assert code_d == 403, f"ожидали 403 вне скоупа, получили {code_d}: {text_d!r}"
        assert host.calls == [], f"host.ask вызван на не-ASK путях: {host.calls}"
        print("OK ASK: allow/deny под url_prefixes не изменились, host.ask не зван")
    finally:
        await proxy.stop()
        await service.stop()


async def test_no_host_ask_is_deny():
    """host=None (standalone-сборка) → ASK трактуется как DENY, не виснет."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    ca = VaultCA(Path(tempfile.mkdtemp(prefix="vault_ask_nohost_")))
    service = _Service(_service_ctx(ca))
    await service.start()
    scope = {
        "url_prefixes": [f"https://localhost:{service.port}/allowed"],
        "ask_prefixes": [f"https://localhost:{service.port}/ask"],
    }
    proxy = VaultProxy(
        ca, GenericBearerConnector(), _mk_secret(), scope,
        upstream_ssl=_trust_ctx(ca),  # host=None по умолчанию
    )
    await proxy.start()
    try:
        code, text = await _client_get(
            proxy.proxy_url, "localhost", service.port, "/ask/x", _trust_ctx(ca)
        )
        assert code == 403, f"без host ASK должен быть DENY, получили {code}: {text!r}"
        assert service.seen == [], f"дошло до сервиса без host: {service.seen}"
        print("OK ASK: host=None → ASK трактуется как DENY (standalone), не виснет")
    finally:
        await proxy.stop()
        await service.stop()


# --- TtyVaultHost.ask ---------------------------------------------------------

class _PtyStdin:
    """Обёртка над slave-fd псевдотерминала: isatty()=True, fileno()=slave —
    чтобы TtyVaultHost.ask пошёл tty-путём (add_reader), а не «нет tty→False»."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._fd


async def test_tty_ask_assume_yes_and_no_tty():
    """assume_yes→True; без tty→False (некому спросить, безопасный дефолт Р0)."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    assert await TtyVaultHost(assume_yes=True).ask("s", "d", "GET /x") is True
    # Подменяем stdin на не-tty, чтобы результат не зависел от среды запуска.
    orig = sys.stdin

    class _NoTty:
        def isatty(self) -> bool:
            return False

    sys.stdin = _NoTty()  # type: ignore[assignment]
    try:
        assert await TtyVaultHost().ask("s", "d", "GET /x") is False
    finally:
        sys.stdin = orig
    print("OK TtyVaultHost.ask: assume_yes→True, без tty→False")


async def test_tty_ask_self_timeout_while_confirm_holds_lock():
    """HIGH-регресс: ask() САМОДОСТАТОЧЕН по таймауту. Параллельный confirm() (без
    таймаута by design) держит tty-лок и НИКОГДА не отвечает → ask() обязан
    разомкнуться по СВОЕМУ таймауту, а не застрять на захвате лока до начала
    отсчёта. Без внешней обёртки прокси (прямой вызов host.ask)."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    master, slave = os.openpty()
    orig_stdin, orig_stderr = sys.stdin, sys.stderr
    sys.stdin = _PtyStdin(slave)  # type: ignore[assignment]
    sys.stderr = open(os.devnull, "w")
    try:
        host = TtyVaultHost(ask_timeout=0.5)
        # confirm захватывает лок и ждёт ответа ВЕЧНО (мы не пишем в master).
        c = asyncio.create_task(host.confirm("s", "C", "GET /c"))
        await asyncio.sleep(0.2)
        assert not c.done(), "confirm должен висеть без ответа (держит лок)"
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        # ask НЕ обёрнут внешним таймаутом — проверяем его собственный.
        r = await asyncio.wait_for(host.ask("s", "A", "GET /a"), timeout=_TIMEOUT)
        dt = loop.time() - t0
        assert r is False, r
        assert dt < 5, f"ask не разомкнулся по своему таймауту (застрял на локе): {dt:.2f}s"
        # confirm всё ещё висит (держит лок) — снимаем для чистоты.
        c.cancel()
        try:
            await c
        except asyncio.CancelledError:
            pass
        print("OK TtyVaultHost.ask: самодостаточный таймаут даже под удержанным confirm-локом")
    finally:
        sys.stderr.close()
        sys.stdin, sys.stderr = orig_stdin, orig_stderr
        os.close(master)
        os.close(slave)


async def test_tty_ask_serialized_over_one_tty():
    """Конкурентные ask сериализованы одним локом (как confirm): на одной tty
    отвечаем по очереди, ранний не затирается поздним. Через реальный pty."""
    if _ASK_IMPORT_ERR is not None:
        return _skip(f"импорт vault не удался: {_ASK_IMPORT_ERR}")
    master, slave = os.openpty()
    orig_stdin, orig_stderr = sys.stdin, sys.stderr
    sys.stdin = _PtyStdin(slave)  # type: ignore[assignment]
    sys.stderr = open(os.devnull, "w")  # промпты в /dev/null — не шумим
    try:
        host = TtyVaultHost()
        a = asyncio.create_task(host.ask("s", "A", "GET /a"))
        b = asyncio.create_task(host.ask("s", "B", "GET /b"))
        # Дать A захватить лок и зарегистрировать reader (B ждёт лок).
        await asyncio.sleep(0.2)
        assert not a.done() and not b.done(), "ask не должен резолвиться без ответа"
        os.write(master, b"y\n")               # ответ первому (A)
        ra = await asyncio.wait_for(a, timeout=_TIMEOUT)
        os.write(master, b"n\n")               # затем второму (B), после релиза лока
        rb = await asyncio.wait_for(b, timeout=_TIMEOUT)
        assert ra is True and rb is False, (ra, rb)
        print("OK TtyVaultHost.ask: конкурентные сериализованы на одной tty (лок)")
    finally:
        sys.stderr.close()
        sys.stdin, sys.stderr = orig_stdin, orig_stderr
        os.close(master)
        os.close(slave)


def main() -> None:
    if _ASK_IMPORT_ERR is not None:
        _skip(f"vault недоступен: {_ASK_IMPORT_ERR}")
        return
    test_generic_bearer_emits_ask()
    for coro in (
        test_ask_grant_true_injects,
        test_ask_grant_false_denied,
        test_ask_timeout_denies_without_hang,
        test_ask_grant_is_per_request_no_cache,
        test_allow_deny_unchanged_no_ask,
        test_no_host_ask_is_deny,
        test_tty_ask_assume_yes_and_no_tty,
        test_tty_ask_self_timeout_while_confirm_holds_lock,
        test_tty_ask_serialized_over_one_tty,
    ):
        asyncio.run(coro())
    print("ALL VAULT-ASK OK")


if __name__ == "__main__":
    main()
