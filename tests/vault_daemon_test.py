"""VaultDaemon — автономный демон секретов БЕЗ оркестратора. Поднимаем его с
фейковым VaultHost (никакого core/Session/manager), бьём по HTTP как настоящий
CLI. Ключевое: cwd приходит из контекста ТОКЕНА (issue_token), демон его не
перерезолвивает — работает даже когда никакого manager нет вообще.

Запуск: .venv/bin/python tests/vault_daemon_test.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp  # noqa: E402

from vault.daemon import VaultDaemon  # noqa: E402
from vault.store import SecretStore  # noqa: E402


class FakeHost:
    """Минимальный VaultHost: ничего от оркестратора не знает, копит вызовы."""

    def __init__(self, confirm_ok: bool = True):
        self.confirm_ok = confirm_ok
        self.observed: list[tuple[str, str]] = []
        self.records: list[tuple] = []
        self.denied: list[tuple[str, str]] = []

    async def confirm(self, session_name, description, preview) -> bool:
        return self.confirm_ok

    async def observe(self, session_name, line_html) -> None:
        self.observed.append((session_name, line_html))

    def record(self, session_name, *, secret, cmd, allowed) -> None:
        self.records.append((session_name, secret, cmd, allowed))

    async def notify_denied(self, session_name, cmd_display) -> None:
        self.denied.append((session_name, cmd_display))


def _store(tmp: Path) -> SecretStore:
    f = tmp / "secrets.toml"
    f.write_text(
        '[secrets.deploy]\nvalue="S3CR3T"\nenv="TOK"\nsessions=["de*"]\n'
        'commands=["sh -c *"]\nconfirm=false\n\n'
        '[secrets.key]\nshared=true\nvalue="SHV"\nenv="OPENAI"\nsessions=["*"]\nconfirm=false\n'
    )
    os.chmod(f, 0o600)
    return SecretStore(f)


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="vault_daemon_"))
    cwd = tmp / "proj"
    cwd.mkdir()
    host = FakeHost()
    daemon = VaultDaemon(_store(tmp), host, guard_on=True)
    await daemon.start()
    try:
        # cwd СНИМАЕТСЯ при выдаче токена — никакого manager/effective_cwd.
        token = daemon.issue_token("dev", cwd)
        url = daemon.url
        good = {"Authorization": f"Bearer {token}"}

        async with aiohttp.ClientSession() as http:
            async with http.get(f"{url}/secrets",
                                headers={"Authorization": "Bearer wrong"}) as r:
                assert r.status == 401
            print("OK autonomy: чужой токен → 401")

            async with http.get(f"{url}/secrets", headers=good) as r:
                listed = await r.json()
            names = {s["name"] for s in listed}
            assert names == {"deploy", "key"} and "S3CR3T" not in json.dumps(listed)
            print("OK autonomy: /secrets по policy сессии, без значений")

            # /run: исполнение на хосте в CWD ИЗ ТОКЕНА (pwd = cwd), значение → •••
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy",
                                       "cmd": ["sh", "-c", "echo t=$TOK; pwd"]}) as r:
                data = await r.json()
            assert data["code"] == 0 and "t=•••" in data["stdout"], data
            assert str(cwd.resolve()) in data["stdout"], (data, cwd)
            print("OK autonomy: /run на хосте в cwd ИЗ ТОКЕНА, значение вымарано")

            # side-effects прошли через фейк-host (не через оркестратор)
            assert host.observed and host.records, (host.observed, host.records)
            print("OK autonomy: наблюдаемость/аудит — через VaultHost")

            # /get shared → значение выдаётся
            async with http.post(f"{url}/get", headers=good,
                                 json={"secret": "key"}) as r:
                assert (await r.json())["value"] == "SHV"
            print("OK autonomy: /get shared → значение")

        # перевыдача токена отзывает прежний
        token2 = daemon.issue_token("dev", cwd)
        assert token2 != token
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{url}/secrets",
                                headers={"Authorization": f"Bearer {token}"}) as r:
                assert r.status == 401  # старый токен больше не признаётся
        print("OK autonomy: перевыдача токена отзывает прежний")

        # revoke_session (= удаление сессии оркестратором) → токен мгновенно мёртв.
        # Инвариант «удалил сессию → её доступ к секретам умер» (раньше давал
        # _auth через manager.get; теперь — явный отзыв по хуку удаления).
        daemon.revoke_session("dev")
        async with aiohttp.ClientSession() as http:
            async with http.post(f"{url}/run", headers={"Authorization": f"Bearer {token2}"},
                                 json={"secret": "deploy", "cmd": ["sh", "-c", "true"]}) as r:
                assert r.status == 401, "токен удалённой сессии не должен работать"
        print("OK autonomy: revoke_session → токен сессии мёртв (инвариант удаления)")
    finally:
        await daemon.stop()
    print("ALL VAULT-DAEMON OK")


async def test_vault_daemon():
    await main()


if __name__ == "__main__":
    asyncio.run(main())
