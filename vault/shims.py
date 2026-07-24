"""Обёртки-«шимы» кошелька: PATH-шлюз, чтобы модель звала git/gh/curl как обычно.

Смысл: секрет живёт на хосте, а команда бежит в песочнице. Чтобы модель не
дёргала `wallet exec` руками (и не изобретала обходы), в PATH песочницы первым
ставится каталог крошечных `/bin/sh`-обёрток: `gh` → `wallet exec gh "$@"`,
и демон уже на хосте подберёт секрет по команде. `git` — особый случай: сетевые
подкоманды (GIT_NETWORK) идут через кошелёк, локальные (status/commit/log) —
настоящим git прямо в песочнице (быстро, без хостового раунд-трипа).

Почему здесь, а не в модуле оркестратора. Потребителей два: оркестраторный
адаптер (per-session каталог в приватном $HOME сессии) и standalone CLI
claude-box (`--wallet` на host/inject-секрет, каталог во временной папке).
box_cli — Слой 2 редизайна, ему нельзя импортировать orchestrator.*, а две копии
генератора разошлись бы (git-шим — вещь, которую чинят один раз). Модуль
stdlib-only, как весь vault/.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import Iterable

from .secret import GIT_NETWORK

logger = logging.getLogger("vault.shims")

# Имя каталога обёрток. Оркестратор кладёт его в приватный $HOME сессии (это же
# имя знает ядро — session_home), claude-box — во временный каталог, который
# биндится в песочницу. Ставится ПЕРВЫМ в PATH, поэтому обёртки побеждают
# настоящие бинари.
SHIM_DIRNAME = ".wallet-bin"

# Допустимое имя инструмента: только то, что бывает именем бинаря в PATH.
# Всё остальное (пробел, кавычка, `$`, обратный апостроф, перевод строки, NUL,
# слэш, юникод) — НЕ имя, а попытка (пусть и случайная) влезть в шелл-скрипт
# обёртки или в путь файла. Граница доверия у secrets.toml высокая, но
# defense-in-depth: имя из конфига доезжает и до текста скрипта, и до имени
# файла, поэтому фильтруем на входе — там, где имя рождается.
_VALID_TOOL = re.compile(r"^[A-Za-z0-9_.+-]+$")
# Отдельно: `.`/`..` проходят регексп, но это не имена, а traversal (write_shims
# создал бы файл «.» — то есть попытался бы затереть сам каталог).
_RESERVED_TOOL_NAMES = {".", ".."}
# Символы глоба: `commands = ["*"]` — легальная запись policy («любая команда»),
# а не имя бинаря. Такие шаблоны пропускаем МОЛЧА (это норма), в отличие от
# мусорных имён (о них предупреждаем).
_GLOB_CHARS = "*?["


def valid_tool_name(tool: str) -> bool:
    """Годится ли имя как имя обёртки (файл в каталоге шимов + токен в скрипте)."""
    return bool(_VALID_TOOL.match(tool)) and tool not in _RESERVED_TOOL_NAMES


def tool_names(patterns: Iterable[str]) -> set[str]:
    """Голые имена инструментов, которые надо завернуть, из шаблонов `commands`.

    Берём первый токен шаблона (`curl https://api/*` → curl) и его basename;
    чистые глобы (`*`, `sub?cmd`) пропускаем молча — это не имя бинаря.
    Имя, не прошедшее allowlist (`gh$(id)`, «gh\\nrm -rf», имя с NUL/пробелом,
    `.`/`..`), ПРОПУСКАЕМ с предупреждением в лог: обёртку для него не создаём,
    но и весь запуск из-за одной кривой строки policy не валим. Дубликаты
    схлопываются (set).
    """
    tools: set[str] = set()
    for pat in patterns:
        parts = pat.split()
        tool = os.path.basename(parts[0]) if parts else ""
        if not tool or any(c in tool for c in _GLOB_CHARS):
            continue
        if not valid_tool_name(tool):
            logger.warning(
                "shims: имя инструмента %r отвергнуто (допустимо [A-Za-z0-9_.+-], "
                "не «.»/«..») — обёртка не создана", tool)
            continue
        tools.add(tool)
    return tools


def git_shim(real_git: str | None = None) -> str:
    """Обёртка git: сетевые подкоманды → на хост через кошелёк, локальные →
    настоящий git в песочнице. Путь настоящего git резолвим на хосте (/usr у
    песочницы — тот же RO-бинд, поэтому путь совпадает).

    Путь ОБЯЗАН быть заквотирован: git может лежать по пути с пробелом (nix-профиль,
    чекаут в «~/My Projects»), и тогда незаквотированный `exec {path} "$@"` тихо
    ломал бы КАЖДЫЙ локальный git внутри песочницы (127, «not found»), причём на
    старте ошибки не видно. shlex.quote для обычных путей возвращает строку
    БЕЗ изменений — байты обёрток на нормальных путях те же, что были."""
    real = real_git or shutil.which("git") or "/usr/bin/git"
    nets = "|".join(GIT_NETWORK)
    return (
        "#!/bin/sh\n"
        "# Обёртка кошелька (генерируется): сетевые git-подкоманды идут на\n"
        "# хост через `wallet exec` (креды хоста), локальные — настоящим git.\n"
        f'case "${{1:-}}" in\n'
        f'  {nets}) exec wallet exec git "$@" ;;\n'
        "esac\n"
        f'exec {shlex.quote(real)} "$@"\n'
    )


def tool_shim(tool: str) -> str:
    """Обёртка обычного инструмента: заворачиваем вызов целиком в кошелёк."""
    return f'#!/bin/sh\nexec wallet exec {tool} "$@"\n'


def shim_script(tool: str) -> str:
    """Скрипт обёртки для инструмента (git — особый случай)."""
    return git_shim() if tool == "git" else tool_shim(tool)


def install_cli(shim_dir: Path, cli_path: Path) -> Path:
    """Положить в каталог шимов сам клиент `wallet` — СИМЛИНКОМ на bin/wallet.

    Почему симлинк, а не обёртка. Так делает прод-путь оркестратора
    (~/.local/bin/wallet → <репо>/bin/wallet), и у симлинка нет ни одной болезни
    шелл-обёртки: путь с пробелом не ломается (интерполяции нет вообще), клиент
    не может случайно позвать сам себя через PATH (см. ниже), и обновление
    bin/wallet подхватывается само. Репозиторий RO-биндится в песочницу тем же
    путём, поэтому цель симлинка разрешается и изнутри.

    Фолбэк, если с bin/wallet снят бит исполнения (чекаут без прав): крошечная
    обёртка через АБСОЛЮТНЫЙ путь интерпретатора (sys.executable), а не `python3`
    из PATH — иначе, если оператор завернул сам `python3` (его шим стоит в PATH
    первым), обёртка звала бы шим, шим — `wallet exec python3`, и получался бы
    бесконечный exec-цикл. Клиент stdlib-only, любой python подойдёт.

    Возвращает путь созданного входа в PATH.
    """
    link = shim_dir / "wallet"
    if link.is_symlink() or link.exists():
        link.unlink()
    if os.access(cli_path, os.X_OK):
        link.symlink_to(cli_path)
        return link
    interp = sys.executable or "/usr/bin/python3"
    link.write_text(
        "#!/bin/sh\n"
        "# Обёртка клиента кошелька (генерируется): с bin/wallet снят бит\n"
        "# исполнения, зовём его интерпретатором по абсолютному пути.\n"
        f'exec {shlex.quote(interp)} {shlex.quote(str(cli_path))} "$@"\n'
    )
    os.chmod(link, 0o755)
    return link


def write_shims(shim_dir: Path, tools: Iterable[str]) -> list[str]:
    """Полная перегенерация каталога обёрток: каталог 0700, файлы 0755.

    Сначала сносим прежние файлы (отозвали секрет/команду — обёртка не должна
    пережить перегенерацию), потом пишем текущие. Пустой набор инструментов →
    ничего не создаём (каталог остаётся пустым/отсутствующим) и возвращаем [].
    Возвращает отсортированный список завёрнутых имён — для прозрачного
    сообщения оператору.

    Имена ещё раз проходят allowlist (tool_names уже отсеял их на входе): сюда
    зовут и напрямую, а имя доезжает и до пути файла, и до текста скрипта —
    вторая проверка стоит одну строку и снимает целый класс «а если позовут
    мимо». Отвергнутые пропускаются с предупреждением.
    """
    if shim_dir.exists():
        for old in shim_dir.iterdir():
            if old.is_file() or old.is_symlink():
                old.unlink()
    bad = sorted(t for t in set(tools) if not valid_tool_name(t))
    if bad:
        logger.warning("shims: имена отвергнуты (недопустимы): %s", ", ".join(map(repr, bad)))
    names = sorted(t for t in set(tools) if valid_tool_name(t))
    if not names:
        return []
    shim_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(shim_dir, 0o700)
    for tool in names:
        path = shim_dir / tool
        path.write_text(shim_script(tool))
        os.chmod(path, 0o755)
    return names
