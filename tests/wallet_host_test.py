"""OrchestratorVaultHost — адаптер vault.host.VaultHost поверх ядра.

Проверяет контракт мягкой деградации: если сессия удалена (manager.get→None),
confirm→False, а record/notify_denied тихо ничего не делают (не падают, не зовут
ядро). При живой сессии — проксируют в core с верными аргументами. observe
адресуется по имени и резолв не делает.

Плюс ASK-грант «навсегда» (§4.6): когда третья кнопка предлагается (узкий грант +
WALLET_POLICY_EDIT=1), что именно уходит в policy, и что происходит при сбое
записи (честный notice, persisted=False).

Запуск: .venv/bin/python tests/wallet_host_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.wallet.host import OrchestratorVaultHost  # noqa: E402

SESSION = SimpleNamespace(name="dev")


def _core(session, verdict=True, choice=None):
    """Фейк-ядро; session=None имитирует удалённую сессию (manager.get→None).
    verdict — что вернёт двоичный relay (True=✅, False=❌/таймаут); choice —
    исход трёхкнопочного request_choice ("allow"/"deny"/"allow_always"); по
    умолчанию выводится из verdict (обратная совместимость старых проверок)."""
    calls = {"confirm": [], "record": [], "notice": [], "observe": [], "choice": []}

    async def rc(sess, *, tool, description, preview, timeout=300.0):
        calls["confirm"].append((sess, tool, description, preview, timeout))
        return verdict

    async def rch(sess, *, tool, description, preview, timeout=300.0, always_label=None):
        calls["choice"].append(
            (sess, tool, description, preview, timeout, always_label))
        # Кнопки не было — «навсегда» невозможно (ровно как в PermissionRelay).
        want = choice if choice is not None else ("allow" if verdict else "deny")
        if want == "allow_always" and always_label is None:
            want = "deny"
        return want

    async def bg(name, line, *, tool):
        calls["observe"].append((name, line, tool))

    async def notice(sess, text):
        calls["notice"].append((sess, text))

    # t: подставляет line (для notice), иначе «ключ[значения-подстановок]» —
    # так тест видит И какой текст выбран, И что в него подставлено (важно для
    # гранта: оператор обязан видеть секрет/ключ/значение ДО нажатия).
    def t(k, **kw):
        if "line" in kw:
            return kw["line"]
        if kw:
            return f"{k}[{','.join(str(v) for v in kw.values())}]"
        return k

    core = SimpleNamespace(
        manager=SimpleNamespace(get=lambda n: session),
        request_confirmation=rc,
        request_choice=rch,
        bubbles=SimpleNamespace(append_background=bg),
        _record=lambda s, tool, **kw: calls["record"].append((s, tool, kw)),
        notice=notice,
        t=t,
    )
    return core, calls


def run(coro):
    return asyncio.run(coro)


async def _ask_and_drain(h, *args):
    """ASK + дождаться фоновой доставки notice в ТОМ ЖЕ loop.

    notice уходит фоновой задачей (host не блокирует вердикт на доставке — см.
    _persist_grant), поэтому проверять calls["notice"] сразу после ask нельзя:
    задача ещё не прокрутилась. Дренируем её здесь."""
    res = await h.ask(*args)
    pending = list(getattr(h, "_notify_tasks", ()) or ())
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return res


def test_live_session_proxies_to_core():
    core, calls = _core(SESSION)
    h = OrchestratorVaultHost(core)
    assert run(h.confirm("dev", "descr", "prev")) is True
    assert calls["confirm"] == [(SESSION, "wallet", "descr", "prev", 300.0)]
    run(h.observe("dev", "<b>line</b>"))
    assert calls["observe"] == [("dev", "<b>line</b>", "wallet")]
    h.record("dev", secret="s", cmd="gh pr", allowed=True)
    assert calls["record"] == [(SESSION, "wallet", {"secret": "s", "cmd": "gh pr", "allowed": True})]
    run(h.notify_denied("dev", "s → gh auth"))
    assert len(calls["notice"]) == 1
    text = calls["notice"][0][1]
    assert "gh auth" in text and "wallet_denied" in text  # cmd_display + t(wallet_denied)
    print("OK живая сессия: confirm/observe/record/notify проксируют в core")


def test_deleted_session_degrades_gracefully():
    core, calls = _core(None)  # manager.get → None
    h = OrchestratorVaultHost(core)
    assert run(h.confirm("dev", "d", "p")) is False       # deny
    assert calls["confirm"] == []                          # ядро не звалось
    h.record("dev", secret="s", cmd="c", allowed=True)     # тихо
    assert calls["record"] == []
    run(h.notify_denied("dev", "s → x"))                   # тихо
    assert calls["notice"] == []
    # observe адресуется по имени — работает и без резолва сессии
    run(h.observe("dev", "line"))
    assert calls["observe"] == [("dev", "line", "wallet")]
    print("OK удалённая сессия: confirm→False, record/notify no-op, observe по имени")


def test_ask_live_grant_returns_true():
    """ask при живой сессии зовёт permission-relay с ask-маркировкой; ✅ → True.
    Без гранта третьей кнопки нет (always_label=None) и текст объясняет почему."""
    from orchestrator.modules.wallet.host import _ASK_CONFIRM_TIMEOUT
    core, calls = _core(SESSION, verdict=True)
    h = OrchestratorVaultHost(core)
    # description от коннектора, preview = метод+URL (значение секрета НЕ передаём).
    res = run(h.ask("dev", "push в чужой репо", "POST https://api.example/x"))
    assert bool(res) is True and res.persisted is False
    assert len(calls["choice"]) == 1
    sess, tool, desc, preview, timeout, always_label = calls["choice"][0]
    assert sess is SESSION
    # tool/desc отличимы от штатного confirm ("wallet"): это ЗАПРОС РАСШИРЕНИЯ.
    assert tool == "wallet_ask_tool"                       # свой i18n-ключ, не "wallet"
    assert desc.startswith("wallet_ask_desc[push в чужой репо]")  # descr коннектора вшит
    assert preview == "POST https://api.example/x"         # факт запроса (метод+URL)
    assert timeout == _ASK_CONFIRM_TIMEOUT                 # свой таймаут < страховки прокси
    assert always_label is None, "без гранта третья кнопка не предлагается"
    assert "wallet_ask_always_off" in desc, "нет объяснения, почему только разово"
    # Значение секрета нигде не фигурирует.
    assert all("secret" not in str(x).lower() for x in (tool, desc, preview))
    print("OK ask живая+✅: relay зван с ask-маркировкой, свой таймаут, → True")


def test_ask_deny_and_timeout_return_false():
    """Оператор ❌ / таймаут (relay вернул deny) → ask False."""
    core, _ = _core(SESSION, verdict=False)
    h = OrchestratorVaultHost(core)
    assert bool(run(h.ask("dev", "d", "GET https://x/y"))) is False
    print("OK ask ❌/таймаут: relay=deny → ask=False")


def test_ask_deleted_session_denies():
    """Сессия удалена (manager.get→None) → ask False, relay не звался (Р0)."""
    core, calls = _core(None)
    h = OrchestratorVaultHost(core)
    assert bool(run(h.ask("dev", "d", "GET https://x/y"))) is False
    assert calls["choice"] == []
    print("OK ask удалённая сессия: relay не зван, → False")


def test_ask_reaches_operator_from_vault_proxy():
    """E2E vault-сторона: proxy._ask_grant(host.ask) реально доходит до relay при
    живом OrchestratorVaultHost и возвращает вердикт оператора (не заглушку)."""
    from vault.connectors.contract import ScopeVerdict
    from vault.proxy import VaultProxy

    core, calls = _core(SESSION, verdict=True)
    host = OrchestratorVaultHost(core)
    proxy = VaultProxy.__new__(VaultProxy)  # без сети: только _ask_grant + host
    proxy.host = host
    proxy.session_name = "dev"
    proxy._ask_timeout = 5.0
    verdict = ScopeVerdict.ask("нужен доступ к соседнему репо")
    granted = run(proxy._ask_grant(verdict, "GET", "https://api.example/repo"))
    assert granted is True                                 # дошло до оператора → ✅
    assert len(calls["choice"]) == 1                       # relay реально позван
    _, _, desc, preview, _, _ = calls["choice"][0]
    assert "нужен доступ к соседнему репо" in desc         # descr коннектора виден
    assert preview == "GET https://api.example/repo"       # метод+URL проброшены
    # А ❌ оператора (relay=False) прокси трактует как отказ гранта.
    core2, _ = _core(SESSION, verdict=False)
    proxy.host = OrchestratorVaultHost(core2)
    assert run(proxy._ask_grant(verdict, "GET", "https://api.example/repo")) is False
    print("OK e2e: proxy ASK→host.ask→relay→вердикт (✅→True, ❌→False)")


# ── ASK-грант «навсегда» (§4.6) ──────────────────────────────────

_SRC = '''[secrets.svc]
value = "TOKVALUE"
connector = "generic-bearer"
sessions = ["*"]

[secrets.svc.scope]
url_prefixes = ["https://api.svc/v1"]
ask_prefixes = ["https://api.svc/admin"]
'''


def _policy_editor():
    """PolicyEditor на ВРЕМЕННОМ secrets.toml (0600) — реальная запись, не мок."""
    import os
    import tempfile
    from orchestrator.modules.wallet.policy import PolicyEditor
    d = Path(tempfile.mkdtemp(prefix="wallet_host_ask_"))
    path = d / "secrets.toml"
    path.write_text(_SRC)
    os.chmod(path, 0o600)
    return PolicyEditor(path), path


def _cleanup(path: Path) -> None:
    """Убрать временный каталог теста (ТОЧНЫЙ путь, созданный им же). Только при
    успехе: после падения каталог остаётся для разбора."""
    import shutil
    shutil.rmtree(path.parent, ignore_errors=True)


def _grant(secret="svc", value="https://api.svc/admin/reboot"):
    from vault.connectors.contract import ScopeGrant
    return ScopeGrant(key="url_prefixes", value=value, label="узкий доступ",
                      secret=secret)


def test_always_button_offered_and_shows_what_is_written():
    """Есть узкий грант + правка policy включена → третья кнопка предлагается, и
    оператор ВИДИТ в тексте, что именно уйдёт в policy (секрет/ключ/значение)."""
    core, calls = _core(SESSION, choice="allow")
    ed, path = _policy_editor()
    h = OrchestratorVaultHost(core, policy=ed, allow_policy_edit=True)
    run(h.ask("dev", "нужен /admin", "POST https://api.svc/admin/reboot", _grant()))
    _, _, desc, _, _, always_label = calls["choice"][0]
    assert always_label == "wallet_ask_always_btn"
    assert "wallet_ask_always_offer" in desc
    # Ключевые поля гранта видны ДО нажатия (в фейке t их подставляет kwargs).
    assert "svc" in desc and "url_prefixes" in desc and "api.svc/admin/reboot" in desc
    _cleanup(path)
    print("OK «навсегда»: кнопка есть, в тексте видно что именно запишется")


def test_always_button_hidden_when_policy_edit_off():
    """WALLET_POLICY_EDIT=0 → третьей кнопки НЕТ вовсе (выключено = не существует),
    а причина названа. Даже вердикт allow_always в этом случае не пишет policy."""
    core, calls = _core(SESSION, choice="allow_always")
    ed, path = _policy_editor()
    before = path.read_text()
    h = OrchestratorVaultHost(core, policy=ed, allow_policy_edit=False)
    res = run(_ask_and_drain(h, "dev", "d", "POST https://api.svc/admin/reboot", _grant()))
    _, _, desc, _, _, always_label = calls["choice"][0]
    assert always_label is None, "кнопка не должна появляться при выключенной правке"
    assert "wallet_ask_always_off" in desc and "disabled" in desc
    # Фейк-relay без кнопки исход allow_always превращает в deny (как ядро).
    assert bool(res) is False and res.persisted is False
    assert path.read_text() == before, "policy не должна меняться"
    _cleanup(path)
    print("OK «навсегда»: WALLET_POLICY_EDIT=0 → кнопки нет, policy не тронута")


def test_always_button_hidden_without_narrow_grant():
    """Коннектор не дал узкого гранта (grant=None) → кнопки нет и объяснено почему."""
    core, calls = _core(SESSION, choice="allow")
    ed, path = _policy_editor()
    h = OrchestratorVaultHost(core, policy=ed, allow_policy_edit=True)
    run(h.ask("dev", "d", "GET https://api.svc/", None))
    _, _, desc, _, _, always_label = calls["choice"][0]
    assert always_label is None
    assert "narrow" in desc, "нет объяснения про невыводимый узкий грант"
    _cleanup(path)
    print("OK «навсегда»: без узкого гранта кнопки нет, причина названа")


def test_always_writes_narrow_grant_and_notifies():
    """🔒 → УЗКАЯ запись в policy (ровно запрошенный ресурс), notice оператору с
    тем, что записано и как отозвать; persisted=True (прокси расширит свой scope)."""
    core, calls = _core(SESSION, choice="allow_always")
    ed, path = _policy_editor()
    h = OrchestratorVaultHost(core, policy=ed, allow_policy_edit=True)
    res = run(_ask_and_drain(h, "dev", "d", "POST https://api.svc/admin/reboot", _grant()))
    assert bool(res) is True and res.persisted is True
    text = path.read_text()
    prefixes = [ln for ln in text.splitlines() if ln.startswith("url_prefixes")][0]
    assert '"https://api.svc/admin/reboot"' in prefixes, prefixes
    # Узость: в url_prefixes не появилось ни «*», ни хоста целиком.
    assert '"https://api.svc"' not in prefixes and "*" not in prefixes, prefixes
    assert '"https://api.svc/v1"' in prefixes, "прежний скоуп затирать нельзя"
    assert calls["notice"], "оператору не сказали, что записано"
    assert "wallet_ask_written" in calls["notice"][0][1]
    assert calls["record"], "запись гранта не попала в аудит"
    _cleanup(path)
    print("OK «навсегда»: узкая запись в policy + notice оператору + persisted=True")


def test_always_write_failure_is_honest():
    """Сбой записи → доступ разово (granted=True), НО persisted=False и честный
    notice оператору: «грант не записан». Молчаливого вранья нет."""
    core, calls = _core(SESSION, choice="allow_always")
    ed, path = _policy_editor()
    path.unlink()                      # файла нет → секрет не найден → PolicyError
    h = OrchestratorVaultHost(core, policy=ed, allow_policy_edit=True)
    res = run(_ask_and_drain(h, "dev", "d", "POST https://api.svc/admin/reboot", _grant()))
    assert bool(res) is True, "разовый доступ оператор уже одобрил"
    assert res.persisted is False, "прокси не должен думать, что грант записан"
    assert calls["notice"] and "wallet_ask_write_failed" in calls["notice"][0][1]
    _cleanup(path)
    print("OK «навсегда»: сбой записи → честный notice, persisted=False")


def main():
    test_live_session_proxies_to_core()
    test_deleted_session_degrades_gracefully()
    test_ask_live_grant_returns_true()
    test_ask_deny_and_timeout_return_false()
    test_ask_deleted_session_denies()
    test_ask_reaches_operator_from_vault_proxy()
    test_always_button_offered_and_shows_what_is_written()
    test_always_button_hidden_when_policy_edit_off()
    test_always_button_hidden_without_narrow_grant()
    test_always_writes_narrow_grant_and_notifies()
    test_always_write_failure_is_honest()
    print("ALL WALLET-HOST OK")


if __name__ == "__main__":
    main()
