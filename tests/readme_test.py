"""README пакетов vault/ и box/ существуют и не разошлись с кодом.

README не тестируется как проза — но он опирается на конкретные инварианты
публичного контракта (какие коннекторы есть, какие подкоманды у CLI, какие
клиентские команды у bin/wallet). Если эти инварианты изменятся, README
устареет молча. Тест сторожит их: упал → обнови и код-факт, и README.

Запуск: .venv/bin/python tests/readme_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent


def test_readmes_exist():
    for rel in ("vault/README.md", "box/README.md"):
        p = ROOT / rel
        assert p.is_file() and p.read_text(encoding="utf-8").strip(), rel
    print("OK README: vault/ и box/ на месте и непустые")


def test_vault_readme_connectors_match_registry():
    """vault/README называет коннекторы generic-bearer и gdocs — они обязаны быть
    зарегистрированы (иначе README врёт про доступные коннекторы)."""
    from vault.connectors import get_connector
    readme = (ROOT / "vault/README.md").read_text(encoding="utf-8")
    for name in ("generic-bearer", "gdocs"):
        assert name in readme, f"README не упоминает коннектор {name}"
        assert get_connector(name) is not None, f"коннектор {name} не в реестре"
    print("OK vault/README: названные коннекторы зарегистрированы")


def test_vault_cli_subcommands_match_readme():
    """vault/README документирует `vault serve` и `vault policy` — парсер обязан
    их принимать."""
    from vault.cli import build_parser
    parser = build_parser()
    # достаём имена подкоманд из subparsers-действия
    subcmds: set[str] = set()
    for action in parser._actions:
        if hasattr(action, "choices") and action.choices:
            subcmds |= set(action.choices)
    for cmd in ("serve", "policy"):
        assert cmd in subcmds, f"vault CLI не знает подкоманду {cmd}"
    print("OK vault/README: подкоманды serve/policy есть в CLI")


def test_box_readme_launch_api_exists():
    """box/README описывает box.launch.launch(...) и LaunchHandle — они должны
    существовать с задокументированной формой."""
    import inspect
    from box.launch import LaunchHandle, launch
    sig = inspect.signature(launch)
    for param in ("argv", "cwd", "env", "on_output"):
        assert param in sig.parameters, f"launch() без параметра {param}"
    for field in ("process", "pty_master", "answerer", "driver_thread"):
        assert field in LaunchHandle.__annotations__, f"LaunchHandle без {field}"
    print("OK box/README: launch()/LaunchHandle совпадают с документированной формой")


def test_wallet_client_commands_match_vault_readme():
    """vault/README называет клиентские команды bin/wallet (ls/run/exec/get/env) —
    они должны реально распознаваться клиентом."""
    text = (ROOT / "bin/wallet").read_text(encoding="utf-8")
    readme = (ROOT / "vault/README.md").read_text(encoding="utf-8")
    for cmd in ("ls", "run", "exec", "get", "env"):
        assert f'"{cmd}"' in text, f"bin/wallet не обрабатывает {cmd}"
        assert cmd in readme, f"README не упоминает клиентскую команду {cmd}"
    print("OK vault/README: клиентские команды wallet совпадают")


def main() -> None:
    test_readmes_exist()
    test_vault_readme_connectors_match_registry()
    test_vault_cli_subcommands_match_readme()
    test_box_readme_launch_api_exists()
    test_wallet_client_commands_match_vault_readme()
    print("ALL README OK")


if __name__ == "__main__":
    main()
