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
import time
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


def _store_with_git(tmp: Path) -> SecretStore:
    f = tmp / "secrets.toml"
    f.write_text(
        '[secrets.host]\nsessions=["*"]\ncommands=["git", "sh"]\nconfirm=false\n'
    )
    os.chmod(f, 0o600)
    return SecretStore(f)


async def test_git_calls_are_serialized_per_session_repo():
    """/exec сериализует git-команды на (сессия, cwd): без этого параллельный
    `git fetch`/`push` из разных тредов одной сессии на один .git — гонка
    записи в refs/packed-refs (живой случай: fetch за секунды вернул три
    разных снимка origin/master). Другие сессии/репозитории друг друга не
    ждут — проверяем, что лок именно per-(session,cwd), а не глобальный."""
    tmp = Path(tempfile.mkdtemp(prefix="vault_daemon_git_"))
    cwd = tmp / "proj"
    cwd.mkdir()
    other_cwd = tmp / "other"
    other_cwd.mkdir()
    host = FakeHost()
    daemon = VaultDaemon(_store_with_git(tmp), host, guard_on=False)
    await daemon.start()
    try:
        # sleep-скрипт под именем git (симлинк): _execute сериализует по
        # os.path.basename(cmd[0]) == "git" — как в проде, где это настоящий
        # системный git.
        fake_git = tmp / "fake-git"
        fake_git.write_text("#!/bin/sh\nsleep 0.3\necho done $$\n")
        os.chmod(fake_git, 0o755)
        git_link = tmp / "git"
        git_link.symlink_to(fake_git)

        # Токены выдаём ДО параллельных вызовов: issue_token отзывает
        # прежний токен той же сессии — выданные ОДНОВРЕМЕННО из двух call()
        # для одной сессии гонялись бы друг с другом за живой токен.
        token_dev = daemon.issue_token("dev", cwd)
        token_dev2 = daemon.issue_token("dev2", other_cwd)

        async def call(token: str) -> tuple[float, float]:
            hdrs = {"Authorization": f"Bearer {token}"}
            async with aiohttp.ClientSession() as http:
                t0 = time.monotonic()
                async with http.post(
                    f"{daemon.url}/exec", headers=hdrs,
                    json={"cmd": [str(git_link), "status"]},
                ) as r:
                    await r.json()
                return t0, time.monotonic()

        # Две параллельные git-команды в ОДНОМ репозитории (та же сессия) —
        # оба запроса СТАРТУЮТ одновременно (это gather), но лок заставляет
        # исполниться последовательно: суммарный интервал от первого старта
        # до последнего конца должен быть ~0.6с (2×sleep 0.3), а не ~0.3с
        # (как было бы при параллельном исполнении).
        results = await asyncio.gather(call(token_dev), call(token_dev))
        span = max(r[1] for r in results) - min(r[0] for r in results)
        assert span >= 0.5, (
            f"git-вызовы в одном репозитории не сериализованы (span={span:.2f}с): {results}")
        print("OK vault daemon: параллельный git в одном репозитории сериализован")

        # Два репозитория (сессии) — НЕ должны ждать друг друга. Свежие
        # токены: прежние ещё валидны (revoke ниже их не трогал), но снова
        # issue_token не зовём — он отозвал бы token_dev у себя же.
        t_start = time.monotonic()
        await asyncio.gather(call(token_dev), call(token_dev2))
        elapsed = time.monotonic() - t_start
        assert elapsed < 0.55, (
            f"git-вызовы в РАЗНЫХ репозиториях не должны сериализоваться: {elapsed}с")
        print("OK vault daemon: git в разных репозиториях выполняется параллельно")

        daemon.revoke_session("dev")
        assert not any(k[0] == "dev" for k in daemon._git_locks), (
            "revoke_session должен снести git-локи отозванной сессии")
        print("OK vault daemon: revoke_session чистит git-локи сессии")
    finally:
        await daemon.stop()


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

            # `gh auth token` (фоновый PR-поллер Claude Code): отказ НЕ всплывает
            # оператору ни notice'ом (PR #50), ни СТРОКОЙ В БАБЛЕ — между ходами
            # она открывала бы фоновый бабл, т.е. отдельное сообщение в чат на
            # каждый опрос. Аудит при этом остаётся: record() пишет попытку.
            before_obs, before_rec = len(host.observed), len(host.records)
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy",
                                       "cmd": ["gh", "auth", "token",
                                               "--hostname", "github.com"]}) as r:
                assert r.status == 403, "gh auth token обязан отвергаться guard'ом"
            assert len(host.observed) == before_obs, (
                "строка в бабл на gh auth token → фоновый бабл = спам в чат")
            assert len(host.records) == before_rec + 1, "аудит попытки потерян"
            assert not any("auth" in d[1] for d in host.denied), (
                "operator-notice на gh auth token (PR #50) вернулся")
            print("OK autonomy: отказ gh auth token не всплывает в чат, аудит цел")

            # А обычный отказ (не самокорректирующийся) — виден и строкой, и notice.
            before_obs, before_den = len(host.observed), len(host.denied)
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy",
                                       "cmd": ["gh", "repo", "delete", "x"]}) as r:
                assert r.status == 403
            assert len(host.observed) == before_obs + 1, "обычный отказ пропал из бабла"
            assert len(host.denied) == before_den + 1, "обычный отказ без notice"
            print("OK autonomy: обычный отказ по-прежнему виден (строка + notice)")

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
    await test_git_calls_are_serialized_per_session_repo()


if __name__ == "__main__":
    asyncio.run(test_vault_daemon())
