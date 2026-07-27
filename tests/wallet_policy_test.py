"""Правка policy кошелька из бота (/wallet): PolicyEditor + провязка ядра.

Проверяем: значения токенов НЕ утекают в вывод; правки sessions/commands/deny/
confirm/new/rm пишутся в файл; комментарии сохраняются; права 0600; валидация;
понятные ошибки; core.wallet_command находит модуль и отдаёт текст.

Запуск: .venv/bin/python tests/wallet_policy_test.py
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import OrchestratorCore, UserError  # noqa: E402
from orchestrator.core.texts import get_texts  # noqa: E402
from vault.policy import PolicyEditor, PolicyError  # noqa: E402

SRC = '''# Шапка-комментарий не должна пропасть при правке.
[secrets.host]
description = "хостовые креды"
sessions = ["*"]
commands = ["gh", "git"]
confirm = false

[secrets.tok]
value = "SUPERSECRET_TOKEN_VALUE"
env = "GH_TOKEN"
sessions = ["noos"]
commands = ["gh"]
confirm = true
'''


def _editor():
    d = tempfile.mkdtemp()
    p = Path(d) / "secrets.toml"
    p.write_text(SRC)
    os.chmod(p, 0o600)
    return PolicyEditor(p), p


def test_render_hides_values():
    ed, _ = _editor()
    r = ed.render()
    assert "SUPERSECRET" not in r, "значение токена утекло в вывод!"
    assert "inject" in r and "$GH_TOKEN" in r  # тип и имя env — можно, значение — нет
    assert "host-passthrough" in r
    assert "<pre>" in r and "</pre>" in r, "policy должна быть в код-блоке"
    assert "/wallet confirm" in r, "справка по правке должна быть в ответе"
    print("OK render: значения скрыты, код-блок + справка по правке в ответе")


def test_policy_edit_toggle():
    ed, p = _editor()
    before = p.read_text()
    # allow_edit=False: просмотр работает, но любая правка отклоняется и файл цел.
    r = ed.apply([], allow_edit=False)
    assert "<pre>" in r and "выключена" in r
    try:
        ed.apply(["confirm", "host", "on"], allow_edit=False)
        assert False, "правка должна была отклониться"
    except PolicyError as e:
        assert "WALLET_POLICY_EDIT" in str(e)
    assert p.read_text() == before, "файл не должен меняться при выключенной правке"
    # allow_edit=True: та же правка проходит.
    ed.apply(["confirm", "host", "on"], allow_edit=True)
    assert p.read_text() != before
    print("OK WALLET_POLICY_EDIT: off → просмотр да, правка нет; on → правка да")


def test_edits_persist_and_keep_comments():
    ed, p = _editor()
    ed.apply(["confirm", "host", "on"])
    ed.apply(["session", "host", "+dev-*"])
    ed.apply(["cmd", "host", "-git"])
    ed.apply(["deny", "host", "+--force"])
    ed.apply(["new", "proj"])
    ed.apply(["rm", "tok"])
    try:
        import tomllib  # stdlib с 3.11
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    d = tomllib.loads(p.read_text())
    s = d["secrets"]
    assert s["host"]["confirm"] is True
    assert s["host"]["sessions"] == ["*", "dev-*"]
    assert s["host"]["commands"] == ["gh"]
    assert s["host"]["deny"] == ["--force"]
    assert "proj" in s and "tok" not in s  # new/rm
    txt = p.read_text()
    assert "Шапка-комментарий" in txt, "комментарий шапки пропал"
    assert "SUPERSECRET" not in txt  # tok удалён целиком
    assert oct(os.stat(p).st_mode)[-3:] == "600"
    print("OK правки персистятся, комменты и 0600 сохранены, new/rm работают")


def test_errors_are_clear():
    ed, _ = _editor()
    for bad in (["confirm", "host", "maybe"], ["session", "nope", "+x"],
                ["cmd", "host", "noplus"], ["frob"], ["rm", "ghost"], ["new", "bad name"]):
        try:
            ed.apply(bad)
            assert False, f"не бросил на {bad}"
        except PolicyError:
            pass
    print("OK ошибки: неверный аргумент/несуществующий секрет/плохое имя — PolicyError")


def test_core_wallet_command():
    core = OrchestratorCore.__new__(OrchestratorCore)
    core._texts = get_texts("ru")

    ed, _ = _editor()
    fake_mod = SimpleNamespace(name="wallet", handle_command=lambda a: ed.apply(a.split()))
    core.modules = [fake_mod]
    out = core.wallet_command("")
    assert "Секреты кошелька" in out
    # кошелёк не подключён → UserError
    core.modules = []
    try:
        core.wallet_command("")
        assert False, "ожидался UserError"
    except UserError:
        pass
    print("OK core.wallet_command: находит модуль; без модуля — UserError")


def test_module_handle_command_wraps_error():
    # WalletModule.handle_command ошибку policy отдаёт ТЕКСТОМ (не исключением).
    from orchestrator.modules.wallet.module import WalletModule
    ed, _ = _editor()
    mod = WalletModule.__new__(WalletModule)
    mod.policy = ed
    mod.config = SimpleNamespace(wallet_policy_edit=True)
    assert mod.handle_command("frob").startswith("⚠️")
    assert "Секреты кошелька" in mod.handle_command("")
    # тумблер выключен → правка отклоняется текстом ⚠️
    mod.config = SimpleNamespace(wallet_policy_edit=False)
    assert mod.handle_command("confirm host on").startswith("⚠️")
    print("OK module.handle_command: ошибка/выключено → текст ⚠️, не исключение")


def main():
    test_render_hides_values()
    test_policy_edit_toggle()
    test_edits_persist_and_keep_comments()
    test_errors_are_clear()
    test_core_wallet_command()
    test_module_handle_command_wraps_error()
    print("ALL WALLET-POLICY OK")


if __name__ == "__main__":
    main()
