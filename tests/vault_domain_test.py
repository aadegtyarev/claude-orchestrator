"""Домен пакета `vault/` работает БЕЗ оркестратора — доказательство автономности
(фаза 1 редизайна claude-box). Импортируем ТОЛЬКО из `vault.*`, проверяем что
`orchestrator` не затянут в sys.modules, и гоняем ключевые инварианты
Secret/guard/redact/store/policy.

Запуск: .venv/bin/python tests/vault_domain_test.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.policy import PolicyEditor, PolicyError  # noqa: E402
from vault.redact import REDACTED, _redact, _redact_text  # noqa: E402
from vault.secret import (  # noqa: E402
    DEFAULT_HOST_COMMANDS,
    Secret,
    _always_denied,
    _prints_token,
    marker,
)
from vault.store import DEFAULT_SECRETS_TOML, SecretStore  # noqa: E402


def _secret(**kw) -> Secret:
    base = dict(
        name="s", value="", env="", description="", sessions=("*",),
        commands=(), deny=(), allow_unsafe=False, confirm=False, shared=False,
    )
    base.update(kw)
    return Secret(**base)


def test_no_orchestrator_dependency():
    """vault импортируется в СВЕЖЕМ интерпретаторе, НЕ затягивая orchestrator.

    Проверка в отдельном процессе, а НЕ по sys.modules текущего: под `pytest`
    все тест-модули делят один процесс, и orchestrator там уже загружен
    соседними тестами (wallet_test и др.) — sys.modules текущего процесса не
    показатель. Свежий интерпретатор — честная проверка автономности пакета
    (ловится и через run_all.sh, и через pytest)."""
    root = str(Path(__file__).parent.parent)
    # Импортируем ВСЕ подмодули vault (pkgutil.walk_packages) — автономность
    # обязана держаться для всего пакета, включая будущие модули, без правки теста.
    code = (
        f"import sys; sys.path.insert(0, {root!r});"
        "import importlib, pkgutil, vault;"
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages(vault.__path__, 'vault.')];"
        "leaked=[m for m in sys.modules if m=='orchestrator' or m.startswith('orchestrator.')];"
        "sys.exit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, (
        "vault затянул orchestrator в свежем процессе:\n"
        f"stdout={r.stdout}\nstderr={r.stderr}"
    )
    print("OK vault автономен: свежий процесс импортит vault без orchestrator")


def test_secret_modes_and_commands():
    host = _secret(name="host")                      # без value/env
    inject = _secret(name="inj", value="V", env="TOK")
    shared = _secret(name="sh", value="V", env="TOK", shared=True)
    assert host.host_passthrough and host.mode == "host"
    assert not inject.host_passthrough and inject.mode == "inject"
    assert shared.mode == "shared"
    # host с пустым commands → дефолтный набор; inject с пустым → ничего
    assert host.effective_commands == DEFAULT_HOST_COMMANDS
    assert inject.effective_commands == ()
    # allow голым именем — только сам инструмент, не «аргумент где-то»
    gh_only = _secret(commands=("gh",))
    assert gh_only.command_allowed(["gh", "pr", "list"])
    assert not gh_only.command_allowed(["git", "x", "gh"])  # gh как аргумент — нет
    glob = _secret(commands=("curl https://api/*",))
    assert glob.command_allowed(["curl", "https://api/x"])
    assert not glob.command_allowed(["curl", "https://evil/x"])
    print("OK Secret: режимы, effective_commands, allow (имя vs глоб)")


def test_secret_sessions_and_deny():
    s = _secret(sessions=("dev-*",), deny=("--force", "rm *"))
    assert s.session_allowed("dev-1") and not s.session_allowed("prod-1")
    assert s.denied_by(["git", "push", "--force"]) == "--force"   # флаг где угодно
    assert s.denied_by(["git", "push"]) is None
    assert s.denied_by(["rm", "-rf", "/"]) == "rm *"              # глоб по строке
    print("OK Secret: sessions (fnmatch) и deny (флаг/глоб)")


def test_guard():
    # печать токена
    assert _prints_token(["gh", "auth", "token"])
    assert _prints_token(["gh", "--verbose", "auth", "token"])   # флаг до auth не спасает
    assert not _prints_token(["gh", "pr", "create", "--title", "auth token"])
    assert _always_denied(["gh", "auth", "token"]) is not None
    assert _always_denied(["gh", "auth", "status", "--show-token"]) is not None
    assert _always_denied(["gh", "auth", "status"]) is None
    # git-RCE
    assert _always_denied(["git", "-c", "core.sshCommand=evil", "push"]) is not None
    assert _always_denied(["git", "-C", "/repo", "push"]) is None  # changedir безопасен
    assert _always_denied(["git", "push", "ext::sh -c evil"]) is not None
    assert _always_denied(["git", "push", "--receive-pack=evil"]) is not None
    assert _always_denied(["git", "push", "origin", "main"]) is None
    print("OK guard: печать токена + git-RCE (-C не путается с -c)")


def test_redact():
    assert _redact_text("a=SEC b=SEC2", ["SEC", "SEC2"]) == f"a={REDACTED} b={REDACTED}"
    # длинные значения первыми — вложенные не оставляют хвостов
    assert _redact_text("XY", ["X", "XY"]) == REDACTED
    out = _redact(b"t=TOKEN", ["TOKEN"])
    assert out == f"t={REDACTED}"
    print("OK redact: вымарывание всех значений, длинные первыми")


def test_marker():
    assert marker("gh") == "<<wallet:gh>>"
    assert marker("key", as_file=True) == "<<wallet:key:file>>"
    print("OK marker: inline и :file")


def test_store_load_and_perms():
    tmp = Path(tempfile.mkdtemp(prefix="vault_domain_"))
    f = tmp / "secrets.toml"
    f.write_text(
        '[secrets.host]\nsessions=["*"]\ncommands=["gh"]\nconfirm=false\n'
        '[secrets.inj]\nvalue="V"\nenv="TOK"\nsessions=["dev-*"]\nconfirm=false\n'
    )
    os.chmod(f, 0o600)
    store = SecretStore(f)
    secrets = store.load()
    assert set(secrets) == {"host", "inj"}, secrets
    assert secrets["host"].mode == "host" and secrets["inj"].mode == "inject"
    # кэш: тот же mtime/size/mode → тот же объект-словарь
    assert store.load() is secrets
    # права шире 0600 → секреты не грузятся целиком (защита от чтения группой)
    os.chmod(f, 0o644)
    assert store.load() == {}
    print("OK store: парсинг, кэш по (mtime,mode,size), отказ при широких правах")


def test_store_default_toml_parses():
    tmp = Path(tempfile.mkdtemp(prefix="vault_default_"))
    f = tmp / "secrets.toml"
    f.write_text(DEFAULT_SECRETS_TOML)
    os.chmod(f, 0o600)
    secrets = SecretStore(f).load()
    assert "host" in secrets and secrets["host"].mode == "host"
    assert secrets["host"].command_allowed(["gh", "pr", "list"])
    print("OK store: дефолтный DEFAULT_SECRETS_TOML парсится «из коробки»")


def test_policy_view_and_edit():
    tmp = Path(tempfile.mkdtemp(prefix="vault_policy_"))
    f = tmp / "secrets.toml"
    f.write_text('[secrets.host]\nsessions=["*"]\ncommands=[]\nconfirm=false\n')
    os.chmod(f, 0o600)
    pe = PolicyEditor(f)
    assert "host" in pe.render()                     # просмотр без значений
    pe.apply(["new", "gd"])                           # создать host-passthrough
    assert "gd" in pe.render()
    pe.apply(["cmd", "gd", "+gh"])                     # добавить команду
    assert "gh" in SecretStore(f).load()["gd"].effective_commands
    try:
        pe.apply(["new", "gd"])                        # дубль → PolicyError
        raise AssertionError("ожидали PolicyError на дубль")
    except PolicyError:
        pass
    # правка выключена → отказ на операцию, просмотр работает
    assert "host" in pe.apply(["ls"], allow_edit=False)
    try:
        pe.apply(["new", "z"], allow_edit=False)
        raise AssertionError("ожидали PolicyError при allow_edit=False")
    except PolicyError:
        pass
    print("OK policy: просмотр, new/cmd, дубль→ошибка, allow_edit=False")


def main():
    test_no_orchestrator_dependency()
    test_secret_modes_and_commands()
    test_secret_sessions_and_deny()
    test_guard()
    test_redact()
    test_marker()
    test_store_load_and_perms()
    test_store_default_toml_parses()
    test_policy_view_and_edit()
    print("ALL VAULT-DOMAIN OK")


if __name__ == "__main__":
    main()
