"""Прозрачный шлюз: per-session обёртки в PATH + авто-подбор секрета по команде.

Модель зовёт `gh`/`git`/`curl` как обычно — обёртка в .wallet-bin заворачивает
вызов в `wallet exec`, демон подбирает секрет по команде и выполняет её на хосте.
Здесь проверяем генерацию обёрток (какие инструменты, содержимое, git-особый
случай, чистка устаревших, исключение shared) и _resolve_secret.

Запуск: .venv/bin/python tests/wallet_shims_test.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.wallet.module import (  # noqa: E402
    SHIM_DIRNAME,
    SecretStore,
    WalletModule,
)

SESSION = SimpleNamespace(name="dev")


def _store(toml: str) -> SecretStore:
    d = tempfile.mkdtemp()
    p = Path(d) / "s.toml"
    p.write_text(toml)
    os.chmod(p, 0o600)
    return SecretStore(p)


def _mod(store: SecretStore, home: Path) -> WalletModule:
    m = WalletModule.__new__(WalletModule)
    m.store = store
    m.core = SimpleNamespace(manager=SimpleNamespace(session_home=lambda s: home))
    return m


def test_wallet_shims():
    home = Path(tempfile.mkdtemp())
    st = _store(
        # host-passthrough без commands → дефолт gh/git/ssh/scp
        '[secrets.host]\nsessions=["*"]\n\n'
        # inject curl → обёртка curl (маркер развернётся на хосте)
        '[secrets.api]\nvalue="V"\nenv="OPENAI_KEY"\nsessions=["*"]\ncommands=["curl *"]\n\n'
        # shared → обёртки НЕ создаём (значение уже в env песочницы)
        '[secrets.pw]\nshared=true\nvalue="P"\nsessions=["*"]\ncommands=["somecli"]\n\n'
        # чужая сессия → её инструменты сюда не попадают
        '[secrets.other]\nsessions=["prod-*"]\ncommands=["kubectl"]\n'
    )
    m = _mod(st, home)

    tools = m._session_tools(SESSION)
    assert tools == {"gh", "git", "ssh", "scp", "curl"}, tools
    print("OK _session_tools: host-дефолт + inject, без shared/чужой сессии")

    m._provision_shims(SESSION)
    shim_dir = home / SHIM_DIRNAME
    got = {p.name for p in shim_dir.iterdir()}
    assert got == {"gh", "git", "ssh", "scp", "curl"}, got
    assert "somecli" not in got and "kubectl" not in got
    print("OK обёртки созданы для нужных инструментов")

    # Все исполняемые.
    for name in got:
        assert os.access(shim_dir / name, os.X_OK), name
    print("OK обёртки исполняемы (0755)")

    # Обычная обёртка → wallet exec <tool>.
    gh = (shim_dir / "gh").read_text()
    assert gh == '#!/bin/sh\nexec wallet exec gh "$@"\n', repr(gh)
    print("OK обычная обёртка: exec wallet exec gh \"$@\"")

    # git-обёртка: сетевые → wallet, локальные → настоящий git.
    git = (shim_dir / "git").read_text()
    assert "push|fetch|pull|clone" in git
    assert 'exec wallet exec git "$@"' in git
    assert git.rstrip().endswith('git "$@"')          # последняя строка — реальный git
    assert "status" not in git and "commit" not in git  # локальные не перечислены
    print("OK git-обёртка: сетевые через кошелёк, локальные напрямую")

    # session_path — путь, ВИДИМЫЙ в песочнице ($HOME/.wallet-bin), а не хостовый
    # session_home/.wallet-bin (под bwrap session_home смонтирован как $HOME).
    session_path = m.session_path(SESSION)
    assert session_path == [str(Path.home() / SHIM_DIRNAME)], session_path
    print("OK session_path → $HOME/.wallet-bin (путь внутри песочницы)")

    # Перегенерация: секрет api отозвали → curl-обёртка исчезает, gh остаётся.
    st2 = _store('[secrets.host]\nsessions=["*"]\n')
    m2 = _mod(st2, home)
    m2._provision_shims(SESSION)
    got2 = {p.name for p in shim_dir.iterdir()}
    assert got2 == {"gh", "git", "ssh", "scp"}, got2
    assert "curl" not in got2
    print("OK перегенерация чистит устаревшие обёртки (curl удалён)")

    # _resolve_secret: подбор секрета по команде (переехал в демон, берёт имя).
    from vault.daemon import VaultDaemon
    daemon = VaultDaemon(m.store, host=None, guard_on=False)
    assert daemon._resolve_secret(SESSION.name, ["gh", "pr", "list"]).name == "host"
    assert daemon._resolve_secret(SESSION.name, ["curl", "-H", "x", "https://api/x"]).name == "api"
    assert daemon._resolve_secret(SESSION.name, ["python", "app.py"]) is None
    assert daemon._resolve_secret("prod-1", ["kubectl", "get", "pods"]).name == "other"
    print("OK _resolve_secret: подбор по команде и по сессии")

    print("ALL WALLET-SHIMS OK")


if __name__ == "__main__":
    test_wallet_shims()
