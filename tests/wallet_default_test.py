"""Дефолтный secrets.toml «из коробки»: если файла нет, кошелёк создаёт его с
host-passthrough (gh/git/ssh/scp на все сессии), 0600, и он валидно грузится.

Запуск: .venv/bin/python tests/wallet_default_test.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

try:
    import tomllib  # stdlib с 3.11
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.store import DEFAULT_SECRETS_TOML  # noqa: E402
from orchestrator.modules.wallet.module import (  # noqa: E402
    SecretStore,
    WalletModule,
)


def test_default_parses():
    d = tomllib.loads(DEFAULT_SECRETS_TOML)
    assert list(d["secrets"]) == ["host"], d
    print("OK дефолт валиден и содержит секрет host")


def test_write_default_creates_0600_host():
    # каталог ещё не существует — метод создаёт его и файл
    tmp = Path(tempfile.mkdtemp()) / "cfg" / "secrets.toml"
    m = WalletModule.__new__(WalletModule)
    m.config = SimpleNamespace(wallet_secrets_file=tmp)
    m._write_default_secrets()
    assert tmp.exists()
    assert oct(tmp.stat().st_mode & 0o777) == "0o600", oct(tmp.stat().st_mode)

    secs = SecretStore(tmp).load()
    h = secs["host"]
    assert h.mode == "host"                       # host-passthrough (нет value/env)
    assert h.sessions == ("*",)                   # все сессии
    assert h.effective_commands == ("gh", "git", "ssh", "scp")
    assert h.confirm is False
    assert h.session_allowed("любая")
    print("OK _write_default_secrets: 0600, host gh/git/ssh/scp на все сессии")


def test_write_default_no_clobber():
    # если файл уже есть — не перезаписываем (O_EXCL → FileExistsError гасится)
    tmp = Path(tempfile.mkdtemp()) / "secrets.toml"
    tmp.write_text('[secrets.mine]\nsessions=["dev-*"]\n')
    os.chmod(tmp, 0o600)
    m = WalletModule.__new__(WalletModule)
    m.config = SimpleNamespace(wallet_secrets_file=tmp)
    m._write_default_secrets()  # не должен упасть и не должен затереть
    secs = SecretStore(tmp).load()
    assert "mine" in secs and "host" not in secs, list(secs)
    print("OK _write_default_secrets: существующий файл не затирается")


def main():
    test_default_parses()
    test_write_default_creates_0600_host()
    test_write_default_no_clobber()
    print("ALL WALLET-DEFAULT OK")


if __name__ == "__main__":
    main()
