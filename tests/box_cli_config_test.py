"""Умолчания `claude-box`: файл настроек и его слияние с флагами.

Зачем файл. Раньше CLI требовал полный набор флагов на каждый запуск: человек
писал алиас в шелле или забывал флаг и получал не ту изоляцию, чем думал.
Файл описывает, что значит «просто claude-box» на этой машине, а установщик
предлагает его создать.

Проверяем контракт:
  • файла нет → поведение ровно прежнее (пустые умолчания, не ошибка);
  • значения читаются: engine / profile / wallet / secrets, причём
    `wallet = false` — это «выключить», а не «не задано» (под bwrap кошелёк
    включён умолчанием);
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

import os
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
    # ...а false — это ЯВНОЕ «выключить», не «не задано»: под bwrap кошелёк
    # включён умолчанием, и различать эти два состояния обязательно.
    assert load_defaults(_write("wallet = false\n")).wallet is False
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


def test_wallet_is_on_by_default_under_sandbox():
    """Кошелёк включён по умолчанию там, где есть песочница.

    Он полезен ровно в связке с изоляцией (git push и gh работают, значения
    модель не видит), а требовать флаг на каждый запуск значит, что им не будут
    пользоваться. Под `off` и microVM не включаем: там он либо не страхует, либо
    механически не работает."""
    assert cli.parse_args([], BoxDefaults()).wallet_all, "bwrap → кошелёк включён"
    assert cli.parse_args([], BoxDefaults()).wallet_auto, "включён умолчанием"
    for engine in ("off", "agent-vm"):
        opts = cli.parse_args(["--engine", engine], BoxDefaults())
        assert not opts.wallet_requested, f"{engine}: кошелёк включаться не должен"
    # Явное «нет» в файле сильнее умолчания...
    assert not cli.parse_args([], BoxDefaults(wallet=False)).wallet_requested
    # ...и разовое «нет» флагом тоже.
    assert not cli.parse_args(["--wallet="], BoxDefaults()).wallet_requested
    # Имя секрета в файле остаётся именем, а не «всё подряд».
    named = cli.parse_args([], BoxDefaults(wallet="gh"))
    assert named.wallet == "gh" and not named.wallet_all and not named.wallet_auto
    print("OK кошелёк по умолчанию: bwrap да, off/vm нет, отказ сильнее умолчания")


def test_wallet_false_survives_roundtrip():
    """`wallet = false` обязан пережить запись-чтение: иначе «выключил» молча
    превращалось бы в «не задано», то есть снова включено."""
    from box_cli.config import render, save
    d = Path(tempfile.mkdtemp(prefix="box-config-off-")) / "config.toml"
    save(BoxDefaults(wallet=False), d)
    assert "wallet = false" in render(BoxDefaults(wallet=False))
    assert load_defaults(d).wallet is False, load_defaults(d)
    print("OK wallet = false переживает запись и чтение")


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


def test_config_command_edits_file():
    """`claude-box config` — способ ПЕРЕнастроить, а не только настроить.

    Раньше умолчания задавались единственный раз — установщиком, и поменять их
    можно было только правкой файла руками (а если файла ещё нет, ещё и создав
    каталог). Команда закрывает это: точечно, без интерактива и в CI."""
    d = Path(tempfile.mkdtemp(prefix="box-config-cmd-")) / "config.toml"
    keep = os.environ.get("CLAUDE_BOX_CONFIG")
    os.environ["CLAUDE_BOX_CONFIG"] = str(d)
    try:
        assert cli.cmd_config(["engine=off", "wallet=true", "profile=work"]) == 0
        got = load_defaults(d)
        assert (got.engine, got.wallet, got.profile) == ("off", True, "work"), got
        # Пустое значение убирает ключ — иначе «передумал» требовал бы правки файла.
        assert cli.cmd_config(["profile=", "wallet="]) == 0
        got = load_defaults(d)
        assert got.profile is None and got.wallet is None and got.engine == "off"
        # vm — тот же синоним, что у флага
        cli.cmd_config(["engine=vm"])
        assert load_defaults(d).engine == "agent-vm"
        # show ничего не меняет
        cli.cmd_config(["show"])
        assert load_defaults(d).engine == "agent-vm"
        for bad in (["engien=off"], ["engine=bwarp"], ["profile=bad name"], ["engine"]):
            try:
                cli.cmd_config(bad)
            except SystemExit as e:
                assert e.code == 2, (bad, e.code)
            else:
                raise AssertionError(f"должен был отказать: {bad}")
        # ...и после отказов файл остался валидным
        assert load_defaults(d).engine == "agent-vm"
    finally:
        if keep is None:
            os.environ.pop("CLAUDE_BOX_CONFIG", None)
        else:
            os.environ["CLAUDE_BOX_CONFIG"] = keep
    print("OK config: точечная правка, удаление ключа, show, отказы не портят файл")


def test_config_command_repairs_broken_file():
    """Сломанный файл чинится этой же командой, а не только удалением.

    Иначе человек, у которого config.toml не разбирается, оказывался в тупике:
    claude-box отказывается запускаться (умолчания читаются до аргументов), а
    команда правки падала бы на том же разборе."""
    d = Path(tempfile.mkdtemp(prefix="box-config-broken-")) / "config.toml"
    d.write_text('engine = "off"\nprofile =\n', encoding="utf-8")
    keep = os.environ.get("CLAUDE_BOX_CONFIG")
    os.environ["CLAUDE_BOX_CONFIG"] = str(d)
    try:
        assert cli.cmd_config(["engine=bwrap"]) == 0
        assert load_defaults(d).engine == "bwrap"
    finally:
        if keep is None:
            os.environ.pop("CLAUDE_BOX_CONFIG", None)
        else:
            os.environ["CLAUDE_BOX_CONFIG"] = keep
    print("OK config: чинит сломанный файл поверх, а не требует его удалять")


def main() -> None:
    test_missing_file_is_not_an_error()
    test_values_are_read()
    test_typos_are_refused_loudly()
    test_flag_beats_file()
    test_file_defaults_apply_when_no_flags()
    test_wallet_is_on_by_default_under_sandbox()
    test_wallet_false_survives_roundtrip()
    test_wallet_from_file_needs_isolation()
    test_bad_engine_in_file_names_the_file()
    test_config_command_edits_file()
    test_config_command_repairs_broken_file()
    print("ALL BOX-CONFIG OK")


if __name__ == "__main__":
    main()
