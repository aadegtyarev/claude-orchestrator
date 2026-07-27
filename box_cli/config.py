"""Умолчания `claude-box`: файл настроек вместо повторяющихся флагов.

Зачем. У CLI не было способа сказать «я всегда работаю под этим профилем и с
кошельком»: каждый запуск требовал полного набора флагов, и человек либо писал
алиас в шелле, либо забывал флаг и получал не ту изоляцию, чем думал. Файл
описывает, что значит «просто claude-box» на этой машине.

Приоритет — флаг сильнее файла, всегда. `--profile ""` (пустое значение) —
осознанное «в этот раз без профиля», а не «возьми из файла»: иначе умолчание
нельзя было бы отключить, не правя файл.

Формат — TOML, рядом с прочими настройками пользователя:

    # ~/.config/claude-box/config.toml
    engine = "bwrap"     # bwrap | off | agent-vm (он же --vm)
    profile = "work"     # имя профиля; "" — без профиля
    wallet = true        # true — все секреты, доступные claude-box по policy;
                         # "имя" — конкретный секрет
    secrets = "~/.config/claude-orchestrator/secrets.toml"

Файла нет — поведение ровно как раньше. Неизвестный ключ или мусорное значение —
ЧЕСТНЫЙ отказ, а не тихий игнор: опечатка в `engine` иначе молча запускала бы
сессию в другой изоляции.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # stdlib с 3.11
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # без парсера умолчания недоступны
        tomllib = None  # type: ignore[assignment]

# Ключи, которые понимает файл. Список закрытый: неизвестный ключ — почти всегда
# опечатка, и промолчать значит применить не те настройки.
_KEYS = ("engine", "profile", "wallet", "secrets")

DEFAULT_PATH = "~/.config/claude-box/config.toml"


def config_path() -> Path:
    """Где лежит файл умолчаний (переопределяется CLAUDE_BOX_CONFIG)."""
    raw = os.getenv("CLAUDE_BOX_CONFIG", "").strip() or DEFAULT_PATH
    return Path(raw).expanduser()


@dataclass(frozen=True)
class BoxDefaults:
    """Умолчания из файла. None — не задано, поведение прежнее."""

    engine: str | None = None
    profile: str | None = None
    # str — имя секрета; True — «все, что разрешает policy»; None — не задано.
    wallet: str | bool | None = None
    secrets: Path | None = None

    @property
    def empty(self) -> bool:
        return all(
            getattr(self, f) is None for f in ("engine", "profile", "wallet", "secrets")
        )


class ConfigError(Exception):
    """Файл умолчаний непригоден — запуск прерывается с объяснением."""


def load_defaults(path: Path | None = None) -> BoxDefaults:
    """Прочитать умолчания. Нет файла — пустые (не ошибка)."""
    p = path or config_path()
    try:
        raw_text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return BoxDefaults()
    except OSError as e:
        raise ConfigError(f"не читается {p}: {e}") from e

    if tomllib is None:  # Python 3.10 без tomli
        sys.stderr.write(
            f"claude-box: {p} не прочитан — на Python 3.10 нужен пакет tomli "
            "(pip install tomli). Запуск идёт с умолчаниями по коду.\n"
        )
        return BoxDefaults()

    try:
        data = tomllib.loads(raw_text)
    except Exception as e:  # noqa: BLE001 — любой сбой парсера, текст в отказ
        raise ConfigError(f"{p}: не разбирается как TOML ({e})") from e

    unknown = sorted(k for k in data if k not in _KEYS)
    if unknown:
        raise ConfigError(
            f"{p}: неизвестные ключи {unknown}. Допустимо: {', '.join(_KEYS)}."
        )

    engine = _as_str(data, "engine", p)
    profile = _as_str(data, "profile", p)
    secrets_raw = _as_str(data, "secrets", p)
    wallet = data.get("wallet")
    if wallet is not None and not isinstance(wallet, (str, bool)):
        raise ConfigError(
            f"{p}: wallet — либо true (все доступные секреты), либо имя секрета "
            f"строкой; получено {wallet!r}."
        )
    if wallet is False:  # явное «выключено» = как будто не задано
        wallet = None

    return BoxDefaults(
        engine=engine,
        profile=profile,
        wallet=wallet,
        secrets=Path(secrets_raw).expanduser() if secrets_raw else None,
    )


def render(defaults: BoxDefaults) -> str:
    """Умолчания → текст файла. Пишем только заданное: пустой ключ в файле
    ничего не значит, а комментарий-шапка объясняет читателю, откуда файл."""
    lines = [
        "# Умолчания claude-box (флаг в командной строке всегда сильнее).",
        "# Меняется командой `claude-box config`; справочник — docs/BOX.md.",
    ]
    if defaults.engine:
        lines.append(f'engine = "{defaults.engine}"')
    if defaults.profile:
        lines.append(f'profile = "{defaults.profile}"')
    if defaults.wallet is True:
        lines.append("wallet = true")
    elif isinstance(defaults.wallet, str) and defaults.wallet:
        lines.append(f'wallet = "{defaults.wallet}"')
    if defaults.secrets:
        lines.append(f'secrets = "{defaults.secrets}"')
    return "\n".join(lines) + "\n"


def save(defaults: BoxDefaults, path: Path | None = None) -> Path:
    """Записать умолчания. Каталог создаётся — иначе первый же `config` требовал
    бы от человека вручную делать ~/.config/claude-box."""
    p = path or config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(defaults), encoding="utf-8")
    return p


def _as_str(data: dict, key: str, path: Path) -> str | None:
    value = data.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ConfigError(f"{path}: {key} должен быть строкой, получено {value!r}.")
