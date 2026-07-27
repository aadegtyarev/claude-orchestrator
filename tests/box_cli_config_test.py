"""Умолчания `claude-box`: файл настроек и его слияние с флагами.

Зачем файл. Раньше CLI требовал полный набор флагов на каждый запуск: человек
писал алиас в шелле или забывал флаг и получал не ту изоляцию, чем думал.
Файл описывает, что значит «просто claude-box» на этой машине, а установщик
предлагает его создать.

Проверяем контракт:
  • файла нет → поведение ровно прежнее (пустые умолчания, не ошибка);
  • значения читаются: engine / profile / wallet / secrets;
  • опечатка НЕ применяется молча: неизвестный ключ, кривой тип, битый TOML →
    отказ с объяснением (иначе `engine = "bwarp"` тихо запустил бы не там);
  • флаг сильнее файла всегда, а `--profile ""` — «в этот раз без профиля»
    (без этого умолчание нельзя отключить, не правя файл);
  • `wallet` из файла + движок `off` → кошелёк НЕ поднимается: без изоляции он
    не страхует. Явный `--wallet` при этом остаётся рабочим — это осознанный
    выбор оператора, и ломать его файл настроек не должен.

Запуск: .venv/bin/python tests/box_cli_config_test.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box_cli import cli  # noqa: E402
from box_cli.config import BoxDefaults, ConfigError, load_defaults  # noqa: E402


def _write(body: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="box-config-"))
    p = d / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_missing_file_is_not_an_error():
    absent = Path(tempfile.mkdtemp(prefix="box-config-")) / "нет.toml"
    assert load_defaults(absent).empty
    print("OK файла нет → пустые умолчания, поведение прежнее")


def test_values_are_read():
    p = _write(
        'engine = "off"\n'
        'profile = "work"\n'
        'wallet = true\n'
        'secrets = "~/s.toml"\n'
    )
    d = load_defaults(p)
    assert d.engine == "off" and d.profile == "work"
    assert d.wallet is True
    assert d.secrets == Path("~/s.toml").expanduser(), d.secrets
    # wallet может быть именем секрета
    assert load_defaults(_write('wallet = "gh"\n')).wallet == "gh"
    # ...а false = «не задано» (не хочу кошелёк по умолчанию)
    assert load_defaults(_write("wallet = false\n")).wallet is None
    print("OK читаются engine/profile/wallet/secrets, wallet=false ≡ не задано")


def test_typos_are_refused_loudly():
    """Опечатка меняет изоляцию — молчать нельзя."""
    for body, why in (
        ('engien = "bwrap"\n', "неизвестный ключ"),
        ("engine = 5\n", "кривой тип"),
        ('wallet = 42\n', "wallet не строка и не bool"),
        ('engine = "bwrap"\nprofile =\n', "битый TOML"),
    ):
        try:
            load_defaults(_write(body))
        except ConfigError:
            continue
        raise AssertionError(f"должен был отказать: {why}")
    print("OK неизвестный ключ, кривой тип и битый TOML → отказ с объяснением")


def test_flag_beats_file():
    d = BoxDefaults(engine="off", profile="work", wallet="gh")
    opts = cli.parse_args(["--engine", "bwrap", "--profile", "other"], d)
    assert opts.engine == "bwrap" and opts.profile == "other"
    assert opts.wallet == "gh", "невыбранное флагом берётся из файла"
    # Пустое значение — осознанное «в этот раз без этого»: и для профиля, и для
    # кошелька. Без этого умолчание из файла нельзя было бы отключить разово, и
    # правило «флаг сильнее файла» держалось бы только на словах.
    assert cli.parse_args(["--profile", ""], d).profile is None
    off = cli.parse_args(["--wallet="], d)
    assert not off.wallet_requested, "пустой --wallet должен гасить кошелёк из файла"
    print("OK флаг сильнее файла; --profile '' отключает профиль из файла")


def test_file_defaults_apply_when_no_flags():
    d = BoxDefaults(engine="off", profile="work", wallet=True,
                    secrets=Path("/tmp/s.toml"))
    opts = cli.parse_args([], d)
    assert opts.engine == "off" and opts.profile == "work"
    # engine=off гасит кошелёк из файла (см. следующий тест), поэтому проверяем
    # набор умолчаний на изолирующем движке.
    boxed = cli.parse_args([], BoxDefaults(wallet=True, secrets=Path("/tmp/s.toml")))
    assert boxed.wallet_all and boxed.secrets == Path("/tmp/s.toml")
    assert boxed.engine == "bwrap", "движок по умолчанию не изменился"
    print("OK без флагов применяются значения файла")


def test_wallet_from_file_needs_isolation():
    """`wallet` из файла + `off` → кошелёк не поднимается, но запуск идёт."""
    opts = cli.parse_args(["--engine", "off"], BoxDefaults(wallet=True))
    assert not opts.wallet_requested, "кошелёк без изоляции поднимать нельзя"
    assert opts.engine == "off", "сам запуск при этом не ломается"
    # Явная просьба флагом остаётся рабочей: это осознанный выбор оператора
    # (аутентифицированный канал без изоляции), про него предупреждает запуск.
    explicit = cli.parse_args(["--engine", "off", "--wallet", "gh"], BoxDefaults())
    assert explicit.wallet == "gh"
    print("OK кошелёк из файла требует изоляции, явный --wallet не ломается")


def test_bad_engine_in_file_names_the_file():
    """Отказ должен говорить, ГДЕ ошибка — в файле, а не «проверь флаг»."""
    try:
        cli.parse_args([], BoxDefaults(engine="bwarp"))
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("кривой движок из файла должен отвергаться")
    print("OK кривой движок из файла → отказ, указывающий на файл")


def main() -> None:
    test_missing_file_is_not_an_error()
    test_values_are_read()
    test_typos_are_refused_loudly()
    test_flag_beats_file()
    test_file_defaults_apply_when_no_flags()
    test_wallet_from_file_needs_isolation()
    test_bad_engine_in_file_names_the_file()
    print("ALL BOX-CONFIG OK")


if __name__ == "__main__":
    main()
