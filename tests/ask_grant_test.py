"""Постоянный ASK-грант «разрешить навсегда» (§4.6 docs/ARCHITECTURE-claude-box.md).

Срез покрывает четыре стыка, и здесь проверяется каждый:

  1. коннектор — УЗОСТЬ гранта: из запроса выводится префикс ровно на этот
     ресурс, а из запроса к корню сервиса грант не выводится вовсе;
  2. адаптеры (telegram/web) — третья кнопка появляется ТОЛЬКО когда ядро дало
     `always_label`, и появляется в ОБОИХ интерфейсах (иначе грант был бы в
     одном и отсутствовал в другом);
  3. запись в policy — атомарность, права 0600, симлинк-безопасность,
     межпроцессный лок (гонка двух ASK), отзыв через `/wallet scope -`;
  4. прокси — после гранта ТАКОЙ ЖЕ запрос идёт БЕЗ спроса, а похожий-но-другой
     спрашивает снова.

Три исхода релея и его обратная совместимость — в tests/permission_test.py,
поведение хоста (кнопка/notice/сбой записи) — в tests/wallet_host_test.py.

Запуск: .venv/bin/python tests/ask_grant_test.py
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.transport import PermissionRequest  # noqa: E402
from orchestrator.modules.wallet.policy import PolicyEditor, PolicyError  # noqa: E402
from vault.connectors import GenericBearerConnector  # noqa: E402
from vault.connectors.contract import HttpReq, ScopeGrant  # noqa: E402
from vault.host import AskResult, ask_grant  # noqa: E402

SESSION = SimpleNamespace(name="dev", bindings={"telegram": "7"})

_SRC = """[secrets.svc]
value = "TOKVALUE"
connector = "generic-bearer"
sessions = ["*"]

[secrets.svc.scope]
url_prefixes = ["https://api.svc/v1"]
ask_prefixes = ["https://api.svc/admin"]
"""


def _editor(src: str = _SRC):
    d = Path(tempfile.mkdtemp(prefix="ask_grant_"))
    path = d / "secrets.toml"
    path.write_text(src)
    os.chmod(path, 0o600)
    return PolicyEditor(path), path


def _cleanup(path: Path) -> None:
    """Убрать временный каталог теста (ТОЧНЫЙ путь, созданный этим же тестом).
    Зовётся в конце УСПЕШНОГО теста: после падения каталог остаётся для разбора."""
    shutil.rmtree(path.parent, ignore_errors=True)


# ── 1. узость гранта (коннектор) ─────────────────────────────────

def test_grant_is_narrow():
    """ASK несёт грант ровно на запрошенный ресурс: тот же хост, тот же путь,
    без query и без расширения на весь сервис."""
    c = GenericBearerConnector()
    scope = {"url_prefixes": ["https://api.svc/v1"],
             "ask_prefixes": ["https://api.svc/admin"]}
    v = c.in_scope(HttpReq("POST", "https://api.svc/admin/reboot?force=1"), scope)
    assert v.is_ask and v.grant is not None
    assert v.grant.key == "url_prefixes"
    assert v.grant.value == "https://api.svc/admin/reboot", v.grant
    assert "?" not in v.grant.value, "query в префикс не входит — матчер его не смотрит"
    assert v.grant.label, "оператору нужен человеческий текст, что разрешается"
    print("OK грант узкий: ровно запрошенный ресурс, без query и без «всего сервиса»")


def test_no_grant_for_service_root():
    """Запрос к корню сервиса → гранта НЕТ (иначе «навсегда» = весь сервис)."""
    c = GenericBearerConnector()
    scope = {"ask_prefixes": ["https://api.svc/"]}
    v = c.in_scope(HttpReq("GET", "https://api.svc/"), scope)
    assert v.is_ask and v.grant is None, v.grant
    print("OK корень сервиса: узкого гранта нет → третья кнопка не появится")


def test_grant_survives_dot_segments():
    """Грант строится из КАНОНИЗИРОВАННОГО пути: `/admin/../admin/x` не запишет
    в policy строку, которая матчится не так, как выглядит."""
    c = GenericBearerConnector()
    scope = {"ask_prefixes": ["https://api.svc/admin"]}
    v = c.in_scope(HttpReq("GET", "https://api.svc/admin/./sub/../x"), scope)
    assert v.is_ask and v.grant.value == "https://api.svc/admin/x", v.grant
    print("OK грант канонизирован (dot-segments схлопнуты) — что видно, то и матчится")


# ── 2. адаптеры: третья кнопка ровно там, где запрошена ──────────

def _tg_adapter():
    """TelegramAdapter без сети: только то, что нужно permission_prompt."""
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")
    from orchestrator.adapters.telegram.adapter import TelegramAdapter
    a = TelegramAdapter.__new__(TelegramAdapter)
    a.chat_id = 1
    a.t = lambda k, **kw: k
    a._perm_msgs = {}
    sent = []

    class FakeBot:
        async def send_message(self, **kw):
            sent.append(kw)
            return SimpleNamespace(message_id=42)

    a.bot = FakeBot()
    return a, sent


def test_telegram_third_button_only_when_offered():
    a, sent = _tg_adapter()
    plain = PermissionRequest(request_id="r1", tool="Bash", description="d", preview="p")
    asyncio.run(a.permission_prompt(SESSION, plain))
    rows = sent[-1]["reply_markup"].inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 2, "у обычного подтверждения 2 кнопки"
    assert all("allow_always" not in b.callback_data for b in rows[0])

    ask = PermissionRequest(request_id="r2", tool="wallet", description="d",
                            preview="p", always_label="🔒 навсегда")
    asyncio.run(a.permission_prompt(SESSION, ask))
    rows = sent[-1]["reply_markup"].inline_keyboard
    assert len(rows) == 2 and rows[1][0].text == "🔒 навсегда"
    assert rows[1][0].callback_data.endswith(":allow_always")
    # 64 байта — жёсткий лимит Telegram на callback_data.
    assert len(rows[1][0].callback_data.encode()) <= 64
    print("OK telegram: третья кнопка только при always_label, callback влезает в лимит")


def test_web_third_button_only_when_offered():
    """Веб получает метку тем же полем: грант доступен в ОБОИХ интерфейсах."""
    from orchestrator.adapters.web.adapter import WebAdapter
    a = WebAdapter.__new__(WebAdapter)
    events = []

    async def broadcast(ev):
        events.append(ev)

    a._broadcast = broadcast
    asyncio.run(a.permission_prompt(
        SESSION, PermissionRequest("r1", "Bash", "d", "p")))
    assert events[-1]["always_label"] == "", "без метки фронт кнопку не рисует"
    asyncio.run(a.permission_prompt(
        SESSION, PermissionRequest("r2", "wallet", "d", "p", always_label="🔒 нав")))
    assert events[-1]["always_label"] == "🔒 нав"
    # Фронт обязан уметь отправить этот вердикт, а эндпоинт — принять его.
    js = (Path(__file__).parent.parent / "orchestrator/adapters/web/static/app.js").read_text()
    assert "allow_always" in js and "always_label" in js, "фронт не знает про грант"
    src = (Path(__file__).parent.parent / "orchestrator/adapters/web/adapter.py").read_text()
    assert '"allow", "deny", "allow_always"' in src, "эндпоинт не принимает грант"
    print("OK web: метка и вердикт allow_always поддержаны (кнопка не только в TG)")


def test_history_keeps_always_label():
    """Метка едет и в журнал: веб рисует карточку из истории при перезагрузке
    страницы — без неё третья кнопка пропала бы у того, кто обновил вкладку."""
    from orchestrator.core.permission import PermissionRelay
    records = []

    async def each_transport(action, label, **kw):
        return None

    relay = PermissionRelay(
        SimpleNamespace(send_permission=None), lambda k, **kw: k, each_transport,
        lambda s, kind, **payload: records.append((kind, payload)),
    )

    async def go():
        task = asyncio.ensure_future(relay.request_choice(
            SESSION, "wallet", "d", "p", timeout=0.05, always_label="🔒 нав"))
        await task

    asyncio.run(go())
    kinds = dict((k, v) for k, v in records)
    assert kinds["perm_request"]["always_label"] == "🔒 нав", records
    print("OK журнал: always_label сохранён (перезагрузка веба не теряет кнопку)")


# ── 3. запись в policy: безопасность и отзыв ─────────────────────

def test_write_is_atomic_symlink_safe_and_0600():
    """Запись гранта: права 0600, никаких «хвостов» tmp, симлинк рядом не
    разыменовывается (подложенный `secrets.toml.tmp` НЕ становится жертвой)."""
    ed, path = _editor()
    victim = path.parent / "victim.txt"
    victim.write_text("НЕ ТРОГАТЬ")
    # Классическая ловушка: предсказуемое имя временного файла → симлинк на жертву.
    (path.parent / (path.name + ".tmp")).symlink_to(victim)
    ed.grant_scope("svc", "url_prefixes", "https://api.svc/admin/reboot")
    assert victim.read_text() == "НЕ ТРОГАТЬ", "запись пошла по симлинку — RCE-класс!"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    leftovers = [p.name for p in path.parent.iterdir()
                 if p.name.endswith(".tmp") and not p.is_symlink()]
    assert leftovers == [], f"остался временный файл: {leftovers}"
    _cleanup(path)
    print("OK запись гранта: 0600, атомарно, симлинк рядом не разыменован")


def test_write_rejects_broad_and_bogus_values():
    """Валидация значения: глоб и «не URL» в scope не пишутся (иначе оператор
    увидел бы в policy правило, которое матчится не так, как читается)."""
    ed, path = _editor()
    before = path.read_text()
    for bad in ("*", "https://api.svc/*", "api.svc/admin", "  ",
                # backtick/перевод строки/таб/DEL — исказили бы markdown-notice и
                # копируемую команду отзыва, либо расщепили бы secrets.toml (ревью).
                "https://api.svc/x`inject", "https://api.svc/x\nSECOND=1",
                "https://api.svc/x\ty", "https://api.svc/x\x7f"):
        try:
            ed.grant_scope("svc", "url_prefixes", bad)
            assert False, f"«{bad}» не должен был записаться"
        except PolicyError:
            pass
    try:
        ed.grant_scope("svc", "ask_prefixes", "https://api.svc/x")
        assert False, "ask_prefixes через грант править нельзя"
    except PolicyError:
        pass
    assert path.read_text() == before, "файл не должен был измениться"
    _cleanup(path)
    print("OK валидация: глоб/не-URL/чужой ключ scope отвергнуты, файл цел")


def test_concurrent_grants_do_not_lose_each_other():
    """Гонка: два процесса пишут гранты одновременно — оба обязаны оказаться в
    файле (без лока второй затёр бы первого целиком)."""
    ed, path = _editor()
    root = str(Path(__file__).parent.parent)
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from vault.policy import PolicyEditor\n"
        "PolicyEditor(__import__('pathlib').Path(%r)).grant_scope("
        "'svc','url_prefixes',sys.argv[1], exist_ok=True)\n" % (root, str(path))
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", code, f"https://api.svc/admin/r{i}"])
        for i in range(6)
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0
    text = path.read_text()
    missing = [i for i in range(6) if f"https://api.svc/admin/r{i}" not in text]
    assert not missing, f"гранты потеряны при гонке: {missing}\n{text}"
    _cleanup(path)
    print("OK гонка: 6 одновременных грантов записаны все (межпроцессный лок)")


def test_revoke_via_wallet_scope():
    """Отзыв гранта — ровно та команда, которую хост называет оператору."""
    ed, path = _editor()
    ed.grant_scope("svc", "url_prefixes", "https://api.svc/admin/reboot")
    out = ed.apply(["scope", "svc", "-https://api.svc/admin/reboot"])
    assert "✅" in out and "https://api.svc/admin/reboot" not in path.read_text()
    # Показ policy обязан показывать выданные префиксы — иначе отзывать вслепую.
    ed.grant_scope("svc", "url_prefixes", "https://api.svc/admin/reboot")
    assert "https://api.svc/admin/reboot" in ed.render()
    # Выключенная правка запрещает и грант из чата.
    try:
        ed.apply(["scope", "svc", "+https://api.svc/x"], allow_edit=False)
        assert False, "правка при WALLET_POLICY_EDIT=0 должна отклоняться"
    except PolicyError:
        pass
    _cleanup(path)
    print("OK отзыв: /wallet scope -<url> убирает грант, render его показывает")


# ── 4. прокси: повторный запрос уже не спрашивает ────────────────

class _GrantingHost:
    """Хост, который «нажимает навсегда»: разрешает и сообщает persisted=True."""

    def __init__(self, editor: PolicyEditor, secret: str = "svc"):
        self.editor = editor
        self.secret = secret
        self.calls: list[tuple[str, ScopeGrant | None]] = []

    async def ask(self, session_name, description, preview, grant=None):
        self.calls.append((preview, grant))
        if grant is None:
            return AskResult(granted=True)
        self.editor.grant_scope(grant.secret, grant.key, grant.value, exist_ok=True)
        return AskResult(granted=True, persisted=True)


class _LegacyHost:
    """Хост СТАРОГО контракта (три аргумента, bool) — обязан продолжать работать."""

    def __init__(self):
        self.calls = 0

    async def ask(self, session_name, description, preview):
        self.calls += 1
        return True


def _proxy(host, scope, secret_name="svc"):
    """VaultProxy без сети: только скоуп-решение и ASK-путь."""
    from vault.proxy import VaultProxy
    p = VaultProxy.__new__(VaultProxy)
    p.connector = GenericBearerConnector()
    p.secret = SimpleNamespace(name=secret_name, value="TOKVALUE")
    p.scope = dict(scope)
    p.host = host
    p.session_name = "dev"
    p._ask_timeout = 5.0
    p.service_hosts = {"api.svc"}
    return p


def _decide(proxy, method, url):
    """Одно решение прокси по запросу: ("allow"|"deny"|"ask+грант"|"ask+отказ")."""
    verdict = proxy.connector.in_scope(HttpReq(method, url), proxy.scope)
    if not verdict.is_ask:
        return verdict.kind
    granted = asyncio.get_event_loop().run_until_complete(
        proxy._ask_grant(verdict, method, url))
    return "ask+грант" if granted else "ask+отказ"


def test_after_grant_same_request_passes_without_asking():
    """После «навсегда»: тот же запрос — ALLOW без спроса; похожий-но-другой —
    снова ASK. Плюс policy на диске содержит ровно узкий грант."""
    ed, path = _editor()
    host = _GrantingHost(ed)
    proxy = _proxy(host, {"url_prefixes": ["https://api.svc/v1"],
                          "ask_prefixes": ["https://api.svc/admin"]})
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        assert _decide(proxy, "POST", "https://api.svc/admin/reboot") == "ask+грант"
        assert len(host.calls) == 1
        # 1) ТОТ ЖЕ запрос — уже в скоупе живого прокси, спроса нет.
        assert _decide(proxy, "POST", "https://api.svc/admin/reboot") == "allow"
        # …и подресурс выданного гранта тоже (префикс на границе сегмента).
        assert _decide(proxy, "GET", "https://api.svc/admin/reboot/log") == "allow"
        assert len(host.calls) == 1, f"повторный спрос: {host.calls}"
        # 2) ПОХОЖИЙ, но другой ресурс — спрашивает снова (грант узкий!).
        assert _decide(proxy, "POST", "https://api.svc/admin/shutdown") == "ask+грант"
        assert len(host.calls) == 2
        text = path.read_text()
        assert "https://api.svc/admin/reboot" in text and "https://api.svc/admin/shutdown" in text
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    _cleanup(path)
    print("OK после гранта: тот же запрос без спроса, другой ресурс — спрашивает снова")


def test_persisted_grant_notice_is_off_the_critical_path():
    """Инвариант §4.6 против гонки (нашло ревью): доставка notice оператору НЕ
    должна блокировать возврат вердикта — иначе клик у границы ask-вотчдога дал бы
    «грант записан, а запрос DENY». Проверяем на РЕАЛЬНОМ OrchestratorVaultHost с
    заведомо зависшим `notice`: ask обязан вернуться сразу, грант — persisted.
    """
    from orchestrator.modules.wallet.host import OrchestratorVaultHost

    ed, path = _editor()
    notice_started = asyncio.Event()

    class _Session:
        name = "dev"

    class _Core:
        def t(self, _text_key, **kw):
            return _text_key
        def _record(self, *a, **k):
            pass
        async def notice(self, session, text):
            notice_started.set()
            await asyncio.sleep(3600)  # доставка «висит» — не должна держать ask

    host = OrchestratorVaultHost.__new__(OrchestratorVaultHost)
    host._core = _Core()
    host._policy = ed
    host._allow_policy_edit = True
    host._notify_tasks = set()

    grant = ScopeGrant(key="url_prefixes", value="https://api.svc/admin/reboot",
                       label="x", secret="svc")

    async def run():
        # вердикт обязан вернуться, не дожидаясь висящего notice
        res = await asyncio.wait_for(
            host._persist_grant(_Session(), grant), timeout=2.0)
        assert res.granted and res.persisted, res
        # notice запущен фоном (задача есть), но возврат его НЕ ждал
        assert host._notify_tasks, "notice не поставлен в фон"
        # даём loop прокрутиться — фоновая доставка реально стартует и «виснет»
        await asyncio.sleep(0.05)
        assert notice_started.is_set(), "фоновый notice не стартовал"
        return res

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run())
        assert "https://api.svc/admin/reboot" in path.read_text(), "грант не записан"
    finally:
        # снять висящую фоновую задачу notice, чтобы loop закрылся чисто
        pending = list(host._notify_tasks)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)
    _cleanup(path)
    print("OK гонка: notice фоном — вердикт возвращается сразу, грант записан")


def test_legacy_host_without_grant_still_works():
    """Хост старого контракта (bool, без параметра grant) не ломается: ASK
    работает как раньше, persisted=False, живой scope не расширяется."""
    host = _LegacyHost()
    proxy = _proxy(host, {"ask_prefixes": ["https://api.svc/admin"]})
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        assert _decide(proxy, "GET", "https://api.svc/admin/x") == "ask+грант"
        assert _decide(proxy, "GET", "https://api.svc/admin/x") == "ask+грант"
        assert host.calls == 2, "старый хост обязан спрашиваться каждый раз"
        assert proxy.scope.get("url_prefixes") in (None, [])
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    print("OK старый хост (bool, 3 аргумента): ASK работает, scope не расширяется")


def test_ask_grant_normalizes_results():
    """Совместимость на уровне helper'а: bool → AskResult, AskResult → как есть."""
    class Bools:
        async def ask(self, s, d, p):
            return True

    class New:
        async def ask(self, s, d, p, grant=None):
            return AskResult(granted=True, persisted=grant is not None)

    g = ScopeGrant(key="url_prefixes", value="https://a/b", label="x", secret="svc")
    r1 = asyncio.run(ask_grant(Bools(), "s", "d", "p", g))
    assert r1.granted is True and r1.persisted is False
    r2 = asyncio.run(ask_grant(New(), "s", "d", "p", g))
    assert r2.granted is True and r2.persisted is True
    print("OK ask_grant: bool-хост и новый хост нормализуются в AskResult")


def main():
    test_grant_is_narrow()
    test_no_grant_for_service_root()
    test_grant_survives_dot_segments()
    test_telegram_third_button_only_when_offered()
    test_web_third_button_only_when_offered()
    test_history_keeps_always_label()
    test_write_is_atomic_symlink_safe_and_0600()
    test_write_rejects_broad_and_bogus_values()
    test_concurrent_grants_do_not_lose_each_other()
    test_revoke_via_wallet_scope()
    test_after_grant_same_request_passes_without_asking()
    test_persisted_grant_notice_is_off_the_critical_path()
    test_legacy_host_without_grant_still_works()
    test_ask_grant_normalizes_results()
    print("ALL ASK-GRANT OK")


if __name__ == "__main__":
    main()
