"""Реэкспорт профилей из автономного пакета `box/` — совместимость импортов.

Профили переехали в `box/profiles.py`: их спрашивает не только CLI (Слой 2), но
и оркестратор (Слой 3, флаг `/new --profile`), а тянуть CLI-пакет в оркестратор
нельзя. Модуль на stdlib, зависимостей оркестратора не имеет — автономность
`box/` сохраняется (tests/box_autonomy_test.py).

Здесь остаётся только реэкспорт: `from box_cli.profiles import …` и
`from .profiles import …` внутри CLI работают как раньше, источник истины —
один, дублировать логику безопасности (валидация имени, симлинк-гигиена) в двух
местах было бы опаснее любого удобства.
"""

from __future__ import annotations

from box.profiles import (  # noqa: F401
    BASE_URL_VAR,
    MAX_NAME_LEN,
    ProfileError,
    ProfileSettings,
    SETTINGS_NAME,
    apply_settings,
    config_dir,
    ensure_profile,
    list_profiles,
    load_settings,
    profile_dir,
    profile_env,
    profiles_root,
    real_config_dir,
    remove_profile,
    settings_path,
    validate_name,
)

__all__ = [
    "BASE_URL_VAR",
    "MAX_NAME_LEN",
    "ProfileError",
    "ProfileSettings",
    "SETTINGS_NAME",
    "apply_settings",
    "config_dir",
    "ensure_profile",
    "list_profiles",
    "load_settings",
    "profile_dir",
    "profile_env",
    "profiles_root",
    "real_config_dir",
    "remove_profile",
    "settings_path",
    "validate_name",
]
