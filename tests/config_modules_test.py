"""Кошелёк включается ровно одной настройкой и только под bwrap.

Раньше источников истины было два: реестр `MODULES=wallet` и выключатель
`SANDBOX_BWRAP_WALLET`. Они разъезжались (какой сильнее? что при конфликте?),
и оператор не мог по .env сказать, включён кошелёк или нет. Реестр убран:
модуль объявляет свой выключатель сам.

Проверяем контракт:
  • не задано → кошелёк включён под bwrap и выключен под остальными;
  • явная настройка сильнее умолчания;
  • вне bwrap кошелёк НЕ включается даже по явной просьбе (был тихий no-op:
    демон поднят, шимы и ~/.wallet.json лежат в доме сессии, а в гостя VM не
    попадают ни они, ни env-маркеры) — отказ громкий, с объяснением;
  • старый `MODULES` в .env → внятный отказ на старте, а не тихое игнорирование
    (иначе оператор считает, что его настройка действует).

Запуск: .venv/bin/python tests/config_modules_test.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.config import Config  # noqa: E402


def test_default_follows_sandbox():
    assert Config._wallet_module("bwrap") == ("wallet",)
    assert Config._wallet_module("off") == ()
    assert Config._wallet_module("agent-vm") == ()
    print("OK умолчание: bwrap → кошелёк, off/agent-vm → нет")


def test_explicit_switch_wins():
    assert Config._wallet_module("bwrap", "false") == ()
    assert Config._wallet_module("bwrap", "0") == ()
    assert Config._wallet_module("bwrap", "true") == ("wallet",)
    print("OK явная SANDBOX_BWRAP_WALLET сильнее умолчания")


def test_wallet_never_starts_outside_bwrap():
    """Даже явное «включить» вне bwrap не поднимает кошелёк (не тихий no-op)."""
    assert Config._wallet_module("agent-vm", "true") == ()
    assert Config._wallet_module("off", "true") == ()
    print("OK вне bwrap кошелёк не включается даже по явной просьбе")


def test_legacy_modules_is_rejected_loudly():
    """`MODULES` в .env → отказ старта с инструкцией, а не молчаливый игнор."""
    keep = os.environ.get("MODULES")
    os.environ["MODULES"] = "wallet"
    try:
        Config.from_env()
    except SystemExit as e:
        assert "SANDBOX_BWRAP_WALLET" in str(e), e
    else:
        raise AssertionError("MODULES в .env должен приводить к отказу")
    finally:
        if keep is None:
            os.environ.pop("MODULES", None)
        else:
            os.environ["MODULES"] = keep
    print("OK старый MODULES отвергается с объяснением, чем его заменить")


def main():
    test_default_follows_sandbox()
    test_explicit_switch_wins()
    test_wallet_never_starts_outside_bwrap()
    test_legacy_modules_is_rejected_loudly()
    print("ALL CONFIG-MODULES OK")


if __name__ == "__main__":
    main()
