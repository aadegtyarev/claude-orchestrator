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

Кроме учётки профиль несёт СВОИ настройки процесса claude (`profile.toml`):
адрес API (`base_url`), токен из файла (`auth_token_file`) и произвольные
переменные окружения (`[env]`) — см. раздел «настройки профиля» ниже.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, MutableMapping

try:
    import tomllib  # stdlib с 3.11
except ModuleNotFoundError:  # Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # без парсера настройки профиля недоступны
        tomllib = None  # type: ignore[assignment]

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
        for p in (root, path):
            try:
                p.chmod(0o700)
            except OSError:
                pass
        # `.claude` разрешено быть симлинком на учётку оператора (профиль ВЫБИРАЕТ
        # учётку, не копируя её). chmod идёт ПО симлинку и молча поменял бы права
        # ЦЕЛИ (чужого каталога) — не наш, чтобы трогать. Поэтому его не chmod'им.
        if not claude.is_symlink():
            try:
                claude.chmod(0o700)
            except OSError:
                pass
    return path


def config_dir(name: str) -> Path:
    """CLAUDE_CONFIG_DIR профиля: <profile>/.claude (каталог может не существовать)."""
    return profile_dir(name) / ".claude"


def real_config_dir(name: str) -> Path:
    """Каталог учётки профиля С РАСКРЫТЫМИ симлинками — ЕДИНСТВЕННЫЙ путь,
    который уезжает и в CLAUDE_CONFIG_DIR, и в bind песочницы.

    `<profile>/.claude` разрешено делать симлинком на существующую учётку
    оператора (`profiles/work/.claude -> ~/.claude`): так профиль ВЫБИРАЕТ
    учётку, не копируя её гигабайты и не заводя вторую копию токенов. Сам
    ensure_profile такой симлинк уважает (каталог создаётся, только если его нет).

    Почему именно раскрытый путь. bwrap не умеет монтировать В
    назначение-симлинк («Unable to mount source on destination»), а держать env
    и bind на РАЗНЫХ путях (симлинк наружу, цель внутрь) нельзя: тогда внутри
    песочницы CLAUDE_CONFIG_DIR указывал бы на путь, которого там нет, — claude
    молча завёл бы пустую учётку. Один раскрытый путь снимает оба случая сразу,
    а для профиля без симлинка resolve() — тождество, поведение прежнее.

    Граница доверия. Цель симлинка НЕ валидируется — профиль доверяет оператору,
    что `.claude -> <учётка>` указывает на настоящую учётку. В standalone
    `claude-box --profile` каталог профиля RW-биндится как $HOME, поэтому МОДЕЛЬ
    внутри может переписать симлинк и на следующий запуск перенаправить бинд на
    произвольный каталог хоста (`rm .claude && ln -s ~/.ssh .claude`). Это
    осознанный риск изолированного лончера: не запускай `--profile` с
    симлинк-учёткой на модели, которой не доверяешь запись в её собственный HOME.
    В оркестраторе такого нет — каталог профиля в песочницу не биндится, и модель
    переписать симлинк не может.
    """
    return config_dir(name).resolve()


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


# ── настройки профиля (profile.toml) ─────────────────────────────────────────
# Имя файла настроек внутри каталога профиля и переменные, которыми он управляет.
SETTINGS_NAME = "profile.toml"
BASE_URL_VAR = "ANTHROPIC_BASE_URL"
AUTH_TOKEN_VAR = "ANTHROPIC_AUTH_TOKEN"
# Список ключей закрытый: неизвестный ключ — почти всегда опечатка, а промолчать
# здесь значит увести учётку не на тот эндпоинт (см. load_settings).
_SETTINGS_KEYS = ("auth_token_file", "base_url", "env")

# Имя переменной в [env] — то же, что допускает шелл: буква/подчёркивание, дальше
# буквы/цифры/подчёркивания. TOML разрешает в голых ключах ещё «-», а в кавычках —
# что угодно, поэтому проверяем своим regex.
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Префикс проброса из .env оркестратора: в [env] он бессмыслен (см. ниже).
_ENV_PREFIX_DENY = "CLAUDE_ENV_"
# Токен — короткая строка. Файл длиннее — почти наверняка указан не тот файл,
# и читать его целиком на старте сессии незачем.
MAX_TOKEN_LEN = 8192

# Переменные, которыми управляет САМ профиль (или его движок): задать их в [env]
# нельзя — иначе env разъедется с биндом/каталогом/движком и симптом будет не
# похож на причину. Значение — причина отказа с указанием правильного ключа.
_MANAGED_ENV_KEYS = {
    "CLAUDE_CONFIG_DIR": "каталог учётки задаёт сам профиль (<профиль>/.claude)",
    "HOME": "домашний каталог задаёт движок изоляции (bwrap $HOME / HOME гостя)",
    "XDG_STATE_HOME": "его фиксирует профиль (state agent-vm остаётся реальным)",
    "PATH": "PATH собирают claude-box и оркестратор (bin/ репозитория, шимы кошелька)",
    BASE_URL_VAR: "адрес API задаёт ключ base_url (пустая строка снимает переменную)",
    AUTH_TOKEN_VAR: "токен берут из файла: auth_token_file (в profile.toml секрет не храним)",
}


@dataclass(frozen=True)
class ProfileSettings:
    """Настройки профиля из `profile.toml`.

    base_url: None — ключа нет, окружение не трогаем (поведение как раньше);
    "" — ходить НАПРЯМУЮ (снять унаследованный ANTHROPIC_BASE_URL);
    строка — этот адрес API для сессий профиля.

    auth_token_file: абсолютный путь к файлу, содержимое которого станет
    ANTHROPIC_AUTH_TOKEN при запуске (см. apply_settings). Относительный путь
    считается от каталога профиля. None — ключа нет, переменную не трогаем.
    Файл читается ТОЛЬКО на запуске: load_settings его не открывает, иначе
    `claude-box profile` падал бы от переехавшего токена.

    env: переменные для процесса claude в порядке объявления. Значение "" —
    СНЯТЬ переменную (как base_url = ""), иначе от унаследованной (в
    оркестраторе — из CLAUDE_ENV_*) было бы не отписаться. Ключи, которыми
    управляет сам профиль, отвергнуты ещё при разборе (_MANAGED_ENV_KEYS).
    """

    base_url: str | None = None
    auth_token_file: str | None = None
    env: dict[str, str] = field(default_factory=dict)


def settings_path(name: str) -> Path:
    """Путь `profile.toml` профиля <name> (файла может не быть)."""
    return profile_dir(name) / SETTINGS_NAME


def load_settings(name: str) -> ProfileSettings:
    """Прочитать `profile.toml` профиля. Файла нет — пустые настройки.

    Мусор в файле — ЧЕСТНЫЙ отказ, а не тихий игнор: опечатка в `base_url`
    иначе молча оставила бы сессию на прежнем адресе, и разбираться пришлось бы
    по симптому, который на адрес совсем не похож (см. apply_settings).

    Читается ТОЛЬКО сам `profile.toml`: файл токена (auth_token_file) здесь не
    открывается — его читает apply_settings на старте сессии, а `claude-box
    profile` (и паспорт сессии) остаются дешёвыми и не падают от переехавшего
    токена.
    """
    path = settings_path(name)
    if not path.is_file():
        return ProfileSettings()
    if tomllib is None:  # Python 3.10 без tomli
        raise ProfileError(
            f"{path}: нет TOML-парсера (Python 3.10 без tomli) — настройки "
            "профиля прочитать нечем; поставь tomli или Python ≥ 3.11.", code=1)
    with _fs_errors(f"не удалось прочитать {path}"):
        raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ProfileError(f"{path}: не разобрать TOML: {e}") from e
    unknown = sorted(set(data) - set(_SETTINGS_KEYS))
    if unknown:
        raise ProfileError(
            f"{path}: неизвестные ключи: {', '.join(unknown)}; "
            f"допустимо: {', '.join(_SETTINGS_KEYS)}.")
    base = data.get("base_url")
    if base is not None:
        if not isinstance(base, str):
            raise ProfileError(f"{path}: base_url должен быть строкой.")
        base = base.strip()
        if base and "://" not in base:
            raise ProfileError(
                f"{path}: base_url «{base}» — ожидался URL со схемой "
                "(https://api.anthropic.com); пустая строка = ходить напрямую.")

    raw_env = data.get("env")
    env: dict[str, str] = {}
    if raw_env is not None:
        if not isinstance(raw_env, dict):
            raise ProfileError(
                f"{path}: env должен быть таблицей ([env] и строки "
                'ИМЯ = "значение").')
        for key, value in raw_env.items():
            if not _ENV_NAME_RE.fullmatch(key):
                raise ProfileError(
                    f"{path}: [env]: «{key}» — недопустимое имя переменной; "
                    "разрешены буквы, цифры и «_», первый символ — не цифра.")
            if key.startswith(_ENV_PREFIX_DENY):
                raise ProfileError(
                    f"{path}: [env]: {key} — префикс {_ENV_PREFIX_DENY} служит "
                    "пробросу из .env оркестратора, а не процессу claude; "
                    "пиши имя без него.")
            reason = _MANAGED_ENV_KEYS.get(key)
            if reason is not None:
                raise ProfileError(f"{path}: [env]: {key} — {reason}.")
            if not isinstance(value, str):
                raise ProfileError(
                    f"{path}: [env]: {key} должен быть строкой в кавычках "
                    f'({key} = "значение"): окружение — это строки, а числа, '
                    "списки и вложенные таблицы — нет.")
            env[key] = value.strip()

    token_file = data.get("auth_token_file")
    token_path: str | None = None
    if token_file is not None:
        if not isinstance(token_file, str):
            raise ProfileError(
                f"{path}: auth_token_file должен быть строкой (путь к файлу с токеном).")
        token_file = token_file.strip()
        if not token_file:
            raise ProfileError(
                f"{path}: auth_token_file пустой — убери ключ, если токен из файла не нужен.")
        p = Path(token_file).expanduser()
        if not p.is_absolute():  # относительный путь — от каталога профиля
            p = profile_dir(name) / p
        token_path = str(p)

    return ProfileSettings(base_url=base, auth_token_file=token_path, env=env)


def _read_auth_token(name: str, path: Path) -> str:
    """Содержимое файла-токена: одна непустая строка (ключ auth_token_file).

    Читается только здесь — на старте сессии (см. apply_settings). Файл больше
    MAX_TOKEN_LEN байт не читаем вовсе: такой файл можно указать только по
    ошибке (креды, лог, ключ), а тянуть что попало в память на старте незачем.
    """
    with _fs_errors(f"не удалось прочитать токен профиля «{name}» ({path})"):
        size = path.stat().st_size
        if size > MAX_TOKEN_LEN:
            raise ProfileError(
                f"{path}: файл с токеном подозрительно велик ({size} байт) — "
                "похоже, указан не тот файл.")
        raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProfileError(f"{path}: токен — не текст в UTF-8: {e}") from e
    token = text.strip()
    if not token:
        raise ProfileError(
            f"{path}: файл с токеном пуст — профиль «{name}» не аутентифицируется.")
    if "\n" in token:
        raise ProfileError(
            f"{path}: в файле с токеном больше одной строки — похоже, указан не тот файл.")
    return token


def _set_base_url(env: MutableMapping[str, str], base_url: str | None) -> None:
    """Наложить адрес профиля: None — не трогать, "" — снять, строка — поставить."""
    if base_url is None:
        return
    if base_url:
        env[BASE_URL_VAR] = base_url
    else:
        env.pop(BASE_URL_VAR, None)


def apply_base_url(env: MutableMapping[str, str], name: str) -> None:
    """Наложить ТОЛЬКО адрес API профиля (файлов не читает).

    Нужен там, где адрес показывают, а сессию не запускают: у остановленной
    сессии /info считает адрес из конфига (SessionManager.api_base_url). Полный
    apply_settings тут не годится — он читает файл токена, а пропавший токен не
    должен ломать паспорт: /info как раз и зовут, чтобы понять, почему сессия
    не работает.
    """
    _set_base_url(env, load_settings(name).base_url)


def apply_settings(env: MutableMapping[str, str], name: str) -> None:
    """Наложить настройки профиля на окружение claude (на месте).

    Три ключа `profile.toml`, в порядке наложения: `base_url` (адрес API) →
    `[env]` (переменные процесса; пустое значение снимает унаследованную) →
    `auth_token_file` (содержимое файла становится ANTHROPIC_AUTH_TOKEN; файл
    читается ЗДЕСЬ и на каждом запуске — ротация это подмена файла, а
    load_settings и `claude-box profile` файла не касаются).

    Зачем адрес вообще есть в профиле. Адрес API — свойство УЧЁТКИ, а не
    машины. Учётка Team берёт managed-настройки своей организации
    (`channelsEnabled`, `enabledPlugins`, объявления, policy-limits) с эндпоинта
    настроек — и он живёт только на прямом api.anthropic.com. Локальный
    прокси-релей обслуживает один `/v1/messages`, всё остальное отдаёт 404,
    поэтому под таким адресом орг-настройки до клиента НЕ доезжают: Claude Code
    честно считает каналы выключенными и рисует «Channels are not enabled for
    your org», хотя админ их включил. Проверено живьём 15.08.2026 на одних и тех
    же кредах Team-учётки: прямой адрес — сообщение из канала приходит в сессию;
    тот же запуск через прокси — /ping 200, /notify 200 и тишина (ровно баг из
    core/channelstate).

    Один общий `CLAUDE_ENV_ANTHROPIC_BASE_URL` на весь оркестратор такой выбор
    выразить не мог: личной учётке прокси нужен, командной — противопоказан.
    Поэтому адрес переехал к профилю, а `base_url = ""` умеет СНЯТЬ
    унаследованную переменную — иначе от глобального прокси было бы не отписаться.
    То же правило в `[env]`: пустое значение снимает унаследованную переменную.

    Профиль без `profile.toml` окружение не трогает — прежнее поведение.
    """
    settings = load_settings(name)
    _set_base_url(env, settings.base_url)
    # Пересечений с адресом и токеном быть не может: их ключи запрещены в [env].
    for key, value in settings.env.items():
        if value:
            env[key] = value
        else:
            env.pop(key, None)
    if settings.auth_token_file is not None:
        env[AUTH_TOKEN_VAR] = _read_auth_token(name, Path(settings.auth_token_file))


def profile_env(name: str, *, engine: str) -> tuple[dict[str, str], Path]:
    """Создать профиль и вернуть (env-довесок, каталог профиля) для лончера.

    env: всегда CLAUDE_CONFIG_DIR=<раскрытый real_config_dir профиля> (под
    симлинк-учёткой — путь цели, не сам линк); под bwrap ещё HOME=<profile>
    (изоляция домашки). Под off HOME не трогаем — изоляции $HOME нет, только
    редирект CONFIG_DIR (лончер честно предупреждает про это в stderr).

    Под agent-vm (`--vm`) механизм ДРУГОЙ. Гость — своя VM со своим claude, и
    хостовый CLAUDE_CONFIG_DIR он ИГНОРИРУЕТ (замер F4). Креды гостю сеет сам
    agent-vm, читая их по `$HOME/.claude/.credentials.json` СВОЕГО процесса
    (agent-vm/src/host_paths.rs). Значит выбор учётки под VM = подмена HOME
    процессу agent-vm — её и делаем; CLAUDE_CONFIG_DIR не ставим, чтобы не
    делать вид, будто он на что-то влияет.

    XDG_STATE_HOME при этом ФИКСИРУЕМ на реальный: state VM (кэш образа, каталог
    сессии, транскрипты — F5) живёт в `${XDG_STATE_HOME:-~/.local/state}/agent-vm`,
    и без этого подмена HOME увела бы его в профиль — каждая учётка тянула бы
    образ заново и теряла state существующих VM. Профиль выбирает УЧЁТКУ, а не
    переезд инфраструктуры.

    Каталог профиля возвращается, чтобы лончер RW-биндил его в песочницу тем же
    путём (src==dst) — тогда HOME/CONFIG_DIR валидны изнутри. Учётку лончер
    биндит отдельно, по real_config_dir: она может быть симлинком наружу.
    """
    path = ensure_profile(name)
    if engine == "agent-vm":
        env = {"HOME": str(path)}
        # Реальный state-корень: явный XDG_STATE_HOME, иначе он вычислится от
        # подменённого HOME. Берём уже заданный оператором либо дефолт от НАСТОЯЩЕЙ
        # домашки (Path.home() читает passwd, а не $HOME — подмена его не сдвинет).
        env["XDG_STATE_HOME"] = os.environ.get(
            "XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        return env, path
    # Раскрытый путь учётки (real_config_dir): под симлинк-профилем env и bind
    # обязаны совпадать, иначе внутри песочницы переменная указывала бы в
    # несуществующий путь. Без симлинка это тот же <profile>/.claude.
    env = {"CLAUDE_CONFIG_DIR": str(real_config_dir(name))}
    if engine == "bwrap":
        env["HOME"] = str(path)
    return env, path
