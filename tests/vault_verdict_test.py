"""Движок решения policy (vault/verdict.py) — автономно, без оркестратора.

Проверяет evaluate(): нет секрета / sessions / commands / guard / deny /
allow_unsafe / guard_off / needs_confirm — приоритеты причин и флаг подтверждения.

Запуск: .venv/bin/python tests/vault_verdict_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.secret import Secret  # noqa: E402
from vault.verdict import evaluate  # noqa: E402


def _secret(**kw) -> Secret:
    base = dict(
        name="s", value="", env="", description="", sessions=("*",),
        commands=("gh",), deny=(), allow_unsafe=False, confirm=False, shared=False,
    )
    base.update(kw)
    return Secret(**base)


def test_no_secret():
    v = evaluate(None, ["gh", "pr", "list"], "dev", guard_on=True)
    assert not v.allowed and not v.needs_confirm
    assert "wallet ls" in v.reason
    print("OK нет секрета → deny с подсказкой wallet ls")


def test_session_not_allowed():
    v = evaluate(_secret(sessions=("prod-*",)), ["gh", "pr"], "dev", guard_on=True)
    assert not v.allowed and "sessions" in v.reason
    print("OK сессия вне policy → reason sessions")


def test_command_not_allowed():
    v = evaluate(_secret(commands=("gh",)), ["curl", "x"], "dev", guard_on=True)
    assert not v.allowed and "commands" in v.reason
    print("OK команда вне списка → reason commands")


def test_guard_blocks_token_print():
    v = evaluate(_secret(commands=("gh",)), ["gh", "auth", "token"], "dev", guard_on=True)
    assert not v.allowed and "токен" in v.reason
    print("OK guard: печать токена → deny с объяснением")


def test_guard_git_rce():
    v = evaluate(_secret(commands=("git",)), ["git", "-c", "x=y", "push"], "dev", guard_on=True)
    assert not v.allowed and v.reason is not None
    print("OK guard: git -c → deny")


def test_deny_pattern():
    s = _secret(commands=("git",), deny=("--force",))
    v = evaluate(s, ["git", "push", "--force"], "dev", guard_on=True)
    assert not v.allowed and "deny" in v.reason
    print("OK deny-шаблон → reason deny")


def test_allow_unsafe_bypasses_guard():
    s = _secret(commands=("gh",), allow_unsafe=True)
    v = evaluate(s, ["gh", "auth", "token"], "dev", guard_on=True)
    assert v.allowed and v.reason is None
    print("OK allow_unsafe: guard отключён на секрет → allowed")


def test_guard_off_global():
    s = _secret(commands=("gh",))
    v = evaluate(s, ["gh", "auth", "token"], "dev", guard_on=False)
    assert v.allowed and v.reason is None
    print("OK guard_on=False: guard глобально выключен → allowed")


def test_guard_precedes_deny():
    # и guard, и deny сработали бы — reason от guard (проверяется первым)
    s = _secret(commands=("gh",), deny=("token",))
    v = evaluate(s, ["gh", "auth", "token"], "dev", guard_on=True)
    assert not v.allowed and "токен" in v.reason  # guard-текст, не «deny: token»
    print("OK приоритет: guard раньше deny")


def test_allowed_no_confirm():
    v = evaluate(_secret(commands=("gh",), confirm=False), ["gh", "pr"], "dev", guard_on=True)
    assert v.allowed and not v.needs_confirm and v.reason is None
    print("OK разрешено без confirm → allowed, needs_confirm=False")


def test_allowed_needs_confirm():
    v = evaluate(_secret(commands=("gh",), confirm=True), ["gh", "pr"], "dev", guard_on=True)
    assert v.allowed and v.needs_confirm and v.reason is None
    print("OK разрешено с confirm → allowed, needs_confirm=True")


def main():
    test_no_secret()
    test_session_not_allowed()
    test_command_not_allowed()
    test_guard_blocks_token_print()
    test_guard_git_rce()
    test_deny_pattern()
    test_allow_unsafe_bypasses_guard()
    test_guard_off_global()
    test_guard_precedes_deny()
    test_allowed_no_confirm()
    test_allowed_needs_confirm()
    print("ALL VAULT-VERDICT OK")


if __name__ == "__main__":
    main()
