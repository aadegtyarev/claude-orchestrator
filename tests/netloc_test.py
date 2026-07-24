"""host_lan_ip вынесен в общий нижний слой vault/netloc — его делят оркестратор
и Launcher (box_cli/wallet.py биндит vault-прокси кошелька на этот адрес под
--vm). Проверяем:
  • orchestrator.config.host_lan_ip — ТОТ ЖЕ объект (реэкспорт 1:1, поведение не
    поехало для прод-пути оркестратора);
  • AGENT_VM_HOST_IP override работает как раньше (явный адрес побеждает авто);
  • автономность box_cli.wallet: в СВЕЖЕМ процессе импорт box_cli.wallet НЕ
    затягивает orchestrator (host_lan_ip берётся из vault, не из config).

Запуск: .venv/bin/python tests/netloc_test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.netloc import host_lan_ip  # noqa: E402


def test_reexport_is_same_object():
    """orchestrator.config.host_lan_ip — тот же объект, что vault.netloc (1:1)."""
    from orchestrator import config
    assert config.host_lan_ip is host_lan_ip, "реэкспорт разошёлся с источником"
    print("OK host_lan_ip: orchestrator.config реэкспортит vault.netloc (тот же объект)")


def test_override_wins():
    """Явный AGENT_VM_HOST_IP побеждает авто-определение (поведение как было)."""
    old = os.environ.get("AGENT_VM_HOST_IP")
    try:
        os.environ["AGENT_VM_HOST_IP"] = "10.9.8.7"
        assert host_lan_ip() == "10.9.8.7"
        # Пустая строка/пробелы = не задано (падаем в авто; тут просто не падаем).
        os.environ["AGENT_VM_HOST_IP"] = "   "
        assert host_lan_ip() != "   "
    finally:
        if old is None:
            os.environ.pop("AGENT_VM_HOST_IP", None)
        else:
            os.environ["AGENT_VM_HOST_IP"] = old
    print("OK host_lan_ip: AGENT_VM_HOST_IP override побеждает авто")


def test_wallet_autonomous_from_orchestrator():
    """box_cli.wallet в СВЕЖЕМ процессе НЕ затягивает orchestrator.

    host_lan_ip кошелёк берёт из vault.netloc, а не из orchestrator.config —
    иначе Слой-2 wallet тянул бы весь UX-оркестратор (TELEGRAM_BOT_TOKEN и пр.).
    Как в vault_domain_test/box_autonomy_test — только отдельный интерпретатор
    честен: под pytest orchestrator уже загружен соседями.
    """
    root = str(Path(__file__).parent.parent)
    code = (
        f"import sys; sys.path.insert(0, {root!r});"
        "import box_cli.wallet;"
        "leaked=[m for m in sys.modules if m=='orchestrator' or m.startswith('orchestrator.')];"
        "sys.exit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, (
        "box_cli.wallet затянул orchestrator в свежем процессе:\n"
        f"stdout={r.stdout}\nstderr={r.stderr}"
    )
    print("OK box_cli.wallet автономен: host_lan_ip из vault, orchestrator не затянут")


def main() -> None:
    test_reexport_is_same_object()
    test_override_wins()
    test_wallet_autonomous_from_orchestrator()
    print("ALL NETLOC OK")


if __name__ == "__main__":
    main()
