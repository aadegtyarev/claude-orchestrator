"""Маркеры секрета в команде: <<wallet:имя>> (inline) и <<wallet:имя:file>>.

Модель пишет привычный `$ENV`, шелл разворачивает его в маркер, а демон
подставляет РЕАЛЬНОЕ значение на хосте (в аргумент curl или во временный файл
для ssh-ключа). Значение к модели/в песочницу не попадает; маркер недоступного
секрета → пусто.

Запуск: .venv/bin/python tests/wallet_inject_test.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.wallet.module import Secret, marker  # noqa: E402
from vault.daemon import Ctx, VaultDaemon  # noqa: E402


def _secret(**kw) -> Secret:
    d = dict(name="api", value="TOPSECRET-abc", env="API_TOKEN", description="",
             sessions=("*",), commands=("*",), deny=(), allow_unsafe=False, confirm=False,
             shared=False)
    d.update(kw)
    return Secret(**d)


def _daemon(secret: Secret) -> VaultDaemon:
    # _execute берёт cwd из ctx токена и store.load; host для _execute не нужен.
    store = SimpleNamespace(load=lambda: {secret.name: secret})
    return VaultDaemon(store, host=None, guard_on=False)


async def test_wallet_inject():
    cwd = Path(tempfile.mkdtemp())
    ctx = Ctx("noos", cwd)             # контекст токена: имя сессии + её cwd
    s = _secret()
    daemon = _daemon(s)
    tmproot = tempfile.gettempdir()

    # <<wallet:api>> → реальное значение в аргумент.
    code, out, err = await daemon._execute(ctx, s, ["printf", "%s", marker("api")])
    assert code == 0 and out == b"TOPSECRET-abc", (code, out, err)
    print("OK <<wallet:api>> → значение подставлено в аргумент")

    # <<wallet:api:file>> → путь к 0600-файлу; каталог убран после.
    before = {d for d in os.listdir(tmproot) if d.startswith("wallet-")}
    code, out, err = await daemon._execute(ctx, s, ["cat", marker("api", as_file=True)])
    assert code == 0 and out == b"TOPSECRET-abc\n", (code, out, err)
    after = {d for d in os.listdir(tmproot) if d.startswith("wallet-")}
    assert after == before, f"временный каталог не убран: {after - before}"
    print("OK <<wallet:api:file>> → 0600-файл, путь подставлен, каталог убран")

    # inject env: значение доступно процессу на хосте (для gh/aws/kubectl).
    code, out, err = await daemon._execute(ctx, s, ["sh", "-c", "printf %s \"$API_TOKEN\""])
    assert code == 0 and out == b"TOPSECRET-abc", (code, out, err)
    print("OK env $API_TOKEN доступен процессу на хосте")

    # Маркер секрета БЕЗ значения (host-passthrough) → пусто, не течёт.
    hp = _secret(value="", env="")
    daemon_hp = _daemon(hp)
    code, out, err = await daemon_hp._execute(ctx, hp, ["printf", "[%s]", marker("api")])
    assert code == 0 and out == b"[]", (code, out, err)
    print("OK маркер секрета без значения → пусто")


def main():
    asyncio.run(test_wallet_inject())
    print("ALL WALLET-INJECT OK")


if __name__ == "__main__":
    main()
