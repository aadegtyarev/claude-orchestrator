"""Режим shared: секрет, значение которого ВЫДаётся сессии (dev-ключ, логин/
пароль). Проверяем парсинг и эндпоинт /get: shared отдаёт значение, host/inject —
никогда; отказ кнопкой не выдаёт значение.

Запуск: .venv/bin/python tests/wallet_shared_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.wallet.host import OrchestratorVaultHost  # noqa: E402
from orchestrator.modules.wallet.module import SecretStore, WalletModule  # noqa: E402
from vault.daemon import Ctx, VaultDaemon  # noqa: E402

SESSION = SimpleNamespace(name="noos")


def _store(toml: str) -> SecretStore:
    d = tempfile.mkdtemp()
    p = Path(d) / "s.toml"
    p.write_text(toml)
    os.chmod(p, 0o600)
    return SecretStore(p)


class FakeReq:
    def __init__(self, token: str, body: dict):
        self.headers = {"Authorization": f"Bearer {token}"}
        self._b = body

    async def json(self):
        return self._b


def _daemon(store: SecretStore, confirm_ok: bool = True) -> VaultDaemon:
    async def rc(*a, **k):
        return confirm_ok

    async def bg(*a, **k):
        return None

    core = SimpleNamespace(
        manager=SimpleNamespace(get=lambda n: SESSION if n == "noos" else None),
        request_confirmation=rc,
        bubbles=SimpleNamespace(append_background=bg),
        _record=lambda *a, **k: None,
    )
    # side-effects _handle_get (confirm/observe/record) — через host над фейк-ядром.
    daemon = VaultDaemon(store, OrchestratorVaultHost(core), guard_on=True)
    # Токен "tok" привязан к сессии noos (cwd для _handle_get не нужен).
    daemon._tokens = {"tok": Ctx("noos", Path("/tmp"))}
    return daemon


def _body(resp) -> dict:
    return json.loads(resp.body)


async def test_wallet_shared():
    st = _store(
        '[secrets.openai]\nshared=true\nvalue="SHV-123"\nenv="OPENAI_API_KEY"\n'
        'sessions=["*"]\nconfirm=false\n\n'
        '[secrets.pw]\nshared=true\nvalue="hunter2"\nsessions=["*"]\nconfirm=false\n\n'
        '[secrets.bad]\nshared=true\nsessions=["*"]\n\n'
        '[secrets.host]\nsessions=["*"]\ncommands=["gh"]\n'
    )
    secrets = st.load()
    assert secrets["openai"].mode == "shared"
    assert secrets["pw"].env == ""          # value без env — допустимо для shared
    assert "bad" not in secrets             # shared без value отброшен
    assert secrets["host"].mode == "host"
    print("OK parse: shared с value ок (env опц.), без value отброшен")

    d = _daemon(st)
    r = await d._handle_get(FakeReq("tok", {"secret": "openai"}))
    assert r.status == 200
    j = _body(r)
    assert j["value"] == "SHV-123" and j["env"] == "OPENAI_API_KEY", j
    print("OK get shared: значение и env выданы")

    r = await d._handle_get(FakeReq("tok", {"secret": "host"}))
    assert r.status == 403 and b"SHV" not in r.body
    assert "не shared" in _body(r).get("reason", "")
    print("OK get не-shared (host) → отказ, значение НЕ выдаётся")

    r = await d._handle_get(FakeReq("tok", {"secret": "ghost"}))
    assert r.status == 403
    print("OK get неизвестного секрета → отказ")

    st2 = _store('[secrets.k]\nshared=true\nvalue="V-secret"\nsessions=["*"]\nconfirm=true\n')
    d2 = _daemon(st2, confirm_ok=False)
    r = await d2._handle_get(FakeReq("tok", {"secret": "k"}))
    assert r.status == 403 and b"V-secret" not in r.body
    print("OK get с confirm: отказ кнопкой → значение не выдаётся")

    # session_env: shared→реальное значение, inject→маркер, host→нет, чужая→нет.
    st3 = _store(
        '[secrets.svc]\nshared=true\nvalue="KEYVAL"\nenv="OPENAI_API_KEY"\nsessions=["*"]\n\n'
        '[secrets.tok]\nvalue="INJ"\nenv="API_TOKEN"\nsessions=["*"]\ncommands=["curl *"]\n\n'
        '[secrets.other]\nshared=true\nvalue="Z"\nenv="Y"\nsessions=["prod-*"]\n\n'
        '[secrets.host]\nsessions=["*"]\ncommands=["gh"]\n'
    )
    m3 = WalletModule.__new__(WalletModule)
    m3.store = st3
    m3._proxy_env = {}  # без launch-хука перехвата: вклад пуст
    env = m3.session_env(SESSION)
    assert env["OPENAI_API_KEY"] == "KEYVAL"                 # shared → реальное
    assert env["API_TOKEN"] == "<<wallet:tok>>"             # inject → маркер
    assert env["API_TOKEN_FILE"] == "<<wallet:tok:file>>"
    assert "Y" not in env                                    # other: сессия не подходит
    print("OK session_env: shared→значение, inject→маркер, host/чужая сессия→нет")

    # redact_output: значения (shared/inject) вымарываются из чат-текста.
    st4 = _store(
        '[secrets.a]\nshared=true\nvalue="SHARED-VAL-123"\nsessions=["*"]\n\n'
        '[secrets.b]\nvalue="INJ-VAL"\nenv="T"\nsessions=["*"]\ncommands=["gh"]\n\n'
        '[secrets.h]\nsessions=["*"]\ncommands=["gh"]\n'  # host: значения нет
    )
    m4 = WalletModule.__new__(WalletModule)
    m4.store = st4
    scrubbed = m4.redact_output("ключ SHARED-VAL-123 и токен INJ-VAL в тексте")
    assert "SHARED-VAL-123" not in scrubbed and "INJ-VAL" not in scrubbed
    assert scrubbed.count("•••") == 2, scrubbed
    print("OK redact_output: значения shared/inject → •••")


def main():
    asyncio.run(test_wallet_shared())
    print("ALL WALLET-SHARED OK")


if __name__ == "__main__":
    main()
