"""Профили claude-box (Слой 2, docs/ARCHITECTURE-claude-box.md): изолированная
идентичность claude — свой CLAUDE_CONFIG_DIR (креды/транскрипты/настройки) и, под
bwrap, свой $HOME. Модель НЕ видит реальные ~/.claude / ~/.claude-proxy / ~/.ssh
оператора, а профили не пересекаются между собой.

Раскладка: ${CLAUDE_BOX_HOME:-~/.local/share/claude-box}/profiles/<name>, внутри
подкаталог .claude (== CLAUDE_CONFIG_DIR). Под bwrap каталог профиля RW-биндится в
песочницу ТЕМ ЖЕ путём (src==dst), поэтому HOME=<profile> и
CLAUDE_CONFIG_DIR=<profile>/.claude валидны и снаружи, и изнутри изоляции.

Это забота Слоя-CLI (box_cli), не автономного пакета box/: здесь только stdlib,
никакого orchestrator — box_cli докидывает env-редирект + RW-бинд поверх Engine.

БЕЗОПАСНОСТЬ. Имя профиля идёт в path-join, поэтому валидируется СТРОГО и ДО
любого пути (validate_name): allowlist [A-Za-z0-9._-], без пустого/`.`/`..`/`/`/
ведущего `-`, длина ≤ 64. Так `../`, абсолютный путь, `~`, `foo/bar` отвергаются —
выйти за корень профилей нельзя. CLAUDE_BOX_HOME — осознанный конфиг оператора
(доверяем как secrets-путям), валидируем только <name>.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from pathlib import Path
from typing import Iterator

# Разрешённый набор символов имени. Полное совпадение (fullmatch) само по себе
# режет `/`, `~`, пробелы, абсолютный путь; пустое/`.`/`..`/ведущий `-`/длину
# добиваем отдельными проверками ниже (они дают внятную причину отказа).
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
MAX_NAME_LEN = 64

# Дефолтный корень, если CLAUDE_BOX_HOME не задан (XDG-подобный data-каталог).
_DEFAULT_HOME = Path("~/.local/share/claude-box")


class ProfileError(Exception):
    """Отказ работы с профилем; code — код выхода CLI (2 = плохой ввод/имя)."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


@contextlib.contextmanager
def _fs_errors(what: str) -> Iterator[None]:
    """Превратить сбой ФС в ProfileError с кодом 1 (честный отказ, не трейсбек).

    Плохой ввод — это код 2 (validate_name); а «CLAUDE_BOX_HOME указывает в файл»,
    «нет прав», «профиль занят файлом» — среда, код 1. ProfileError сквозь
    менеджер проходит как есть, чтобы не переклеить ему код.
    """
    try:
        yield
    except ProfileError:
        raise
    except OSError as e:
        raise ProfileError(f"{what}: {e.strerror or e} ({e.filename})", code=1) from e


def validate_name(name: str) -> str:
    """Проверить имя профиля ДО path-join. Вернуть его же или бросить ProfileError.

    Инвариант безопасности: имя не должно уводить путь за пределы корня профилей.
    Порядок проверок — от самых наглядных причин к общему allowlist.
    """
    if not name:
        raise ProfileError("имя профиля пустое.")
    if name.startswith("-"):
        # Иначе спутается с флагом CLI и ломает разбор аргументов.
        raise ProfileError(f"имя профиля «{name}» не может начинаться с «-».")
    if name in (".", ".."):
        raise ProfileError(f"имя профиля «{name}» недопустимо (traversal).")
    if len(name) > MAX_NAME_LEN:
        raise ProfileError(
            f"имя профиля длиннее {MAX_NAME_LEN} символов — сократи.")
    if not _NAME_RE.fullmatch(name):
        raise ProfileError(
            f"имя профиля «{name}» содержит недопустимые символы; "
            "разрешены [A-Za-z0-9._-] (без «/», «~», пробелов).")
    return name


def profiles_root() -> Path:
    """Корень всех профилей: ${CLAUDE_BOX_HOME:-~/.local/share/claude-box}/profiles.

    CLAUDE_BOX_HOME — доверенный конфиг оператора: expanduser, но без валидации
    (за пределы уводит только <name>, который проверен отдельно). Относительный
    путь приводим к абсолютному: иначе корень профилей (а с ним HOME/CONFIG_DIR и
    bind в песочницу) молча зависел бы от текущего каталога запуска.
    """
    base = os.environ.get("CLAUDE_BOX_HOME", "").strip()
    home = Path(base).expanduser() if base else _DEFAULT_HOME.expanduser()
    if not home.is_absolute():
        home = Path.cwd() / home
    return home / "profiles"


def profile_dir(name: str) -> Path:
    """Путь каталога профиля <name> (валидирует имя; каталог может не существовать)."""
    return profiles_root() / validate_name(name)


def ensure_profile(name: str) -> Path:
    """Идемпотентно создать каталог профиля (+ его .claude) и вернуть его путь.

    Приватность: каталоги 0700 (креды/транскрипты). Симлинк-гигиена: конечный
    компонент профиля не должен быть симлинком — mkdir(exist_ok) молча принял бы
    существующий симлинк-на-каталог и увёл бы HOME/CONFIG_DIR за корень профилей
    (напр. подложенный `profiles/x -> ~/.ssh`); поэтому такой профиль отвергаем и
    дополнительно сверяем, что реальный путь лежит ВНУТРИ реального корня.
    """
    root = profiles_root()
    name = validate_name(name)
    # Любой отказ ФС (CLAUDE_BOX_HOME указывает в файл, нет прав, профиль занят
    # файлом) — честное сообщение с кодом 1, а не сырой трейсбек: планка CLI.
    with _fs_errors(f"не удалось создать профиль «{name}»"):
        # Базу (CLAUDE_BOX_HOME) ужимаем до 0700 ТОЛЬКО если создали её сами:
        # чужой существующий каталог (напр. CLAUDE_BOX_HOME=/tmp) не наш, чтобы
        # менять ему права.
        base_is_new = not root.parent.exists()
        root.mkdir(parents=True, exist_ok=True)
        if base_is_new:
            try:
                root.parent.chmod(0o700)
            except OSError:
                pass
        path = root / name

        if path.is_symlink():
            raise ProfileError(
                f"профиль «{name}» — симлинк; отказ (симлинк-гигиена).")
        path.mkdir(mode=0o700, exist_ok=True)

        # Инвариант: реальный путь профиля не вышел за реальный корень (защита от
        # симлинков в родительских компонентах корня).
        real_root = root.resolve()
        real_path = path.resolve()
        if real_root != real_path and real_root not in real_path.parents:
            raise ProfileError(
                f"каталог профиля «{name}» вне корня профилей — отказ.")

        # Приватность каталогов явно (umask мог ослабить mkdir-mode; сам
        # CLAUDE_BOX_HOME тоже — иначе world-writable родитель позволил бы
        # подменить каталог profiles целиком).
        claude = path / ".claude"
        claude.mkdir(mode=0o700, exist_ok=True)
        for p in (root, path, claude):
            try:
                p.chmod(0o700)
            except OSError:
                pass
    return path


def config_dir(name: str) -> Path:
    """CLAUDE_CONFIG_DIR профиля: <profile>/.claude (каталог может не существовать)."""
    return profile_dir(name) / ".claude"


def list_profiles() -> list[str]:
    """Имена существующих профилей (отсортированы). Нет корня → пусто."""
    root = profiles_root()
    if not root.is_dir():
        return []
    with _fs_errors("не удалось прочитать список профилей"):
        return sorted(p.name for p in root.iterdir() if p.is_dir())


def remove_profile(name: str) -> Path:
    """Удалить каталог профиля целиком; вернуть удалённый путь. Нет → ProfileError."""
    path = profile_dir(name)
    # exists() идёт ПО ссылке: битый симлинк иначе считался бы «не найден», но и
    # создать профиль с таким именем нельзя — имя заклинивало бы навсегда.
    if not path.exists() and not path.is_symlink():
        raise ProfileError(f"профиль «{name}» не найден.")
    with _fs_errors(f"не удалось удалить профиль «{name}»"):
        # rmtree по симлинку удалил бы цель, а не сам линк — на всякий случай снимаем
        # линк отдельно (симлинк-гигиена: не чистим чужой каталог по подлогу).
        if path.is_symlink():
            path.unlink()
        else:
            shutil.rmtree(path)
    return path


def profile_env(name: str, *, engine: str) -> tuple[dict[str, str], Path]:
    """Создать профиль и вернуть (env-довесок, каталог профиля) для лончера.

    env: всегда CLAUDE_CONFIG_DIR=<profile>/.claude; под bwrap ещё HOME=<profile>
    (изоляция домашки). Под off HOME не трогаем — изоляции $HOME нет, только
    редирект CONFIG_DIR (лончер честно предупреждает про это в stderr).

    Каталог профиля возвращается, чтобы лончер RW-биндил его в песочницу тем же
    путём (src==dst) — тогда HOME/CONFIG_DIR валидны изнутри.
    """
    path = ensure_profile(name)
    env = {"CLAUDE_CONFIG_DIR": str(path / ".claude")}
    if engine == "bwrap":
        env["HOME"] = str(path)
    return env, path
