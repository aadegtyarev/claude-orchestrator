"""Разрешение пути транскрипта клиента Claude Code (client-config, Слой 2).

§5.2 ARCHITECTURE-claude-box: часть «собрать конфиг клиента» — КУДА клиент
(claude) пишет свой транскрипт. Путь определяется профилем (CLAUDE_CONFIG_DIR,
иначе ~/.claude) и рабочим каталогом процесса claude: Claude Code кодирует cwd
в имя каталога проекта заменой '/' и '.' на '-' и складывает туда
`<session_id>.jsonl`. Оркестратор обязан вычислять ТОЧНО ТАКОЙ ЖЕ путь, чтобы
читать статистику/модель/загрязнение из того же файла.

Чистые функции без зависимостей от оркестратора: вход — пути/идентификатор,
выход — Path. Чтение самого файла (read_stats/scan_pollution и пр.) — это уже
надстройка оркестратора (orchestrator.core.transcript), не launcher.
"""

from __future__ import annotations

from pathlib import Path


def resolve_config_dir(claude_config_dir: Path | None) -> Path:
    """Каталог профиля Claude Code: заданный CLAUDE_CONFIG_DIR либо ~/.claude.

    Профиль хранит транскрипты (projects/…). §5.2: под bwrap профиль —
    CLAUDE_CONFIG_DIR (общий с хостом), под VM — $HOME гостя. None означает
    «переменная не задана» → штатный дефолт Claude Code ~/.claude.
    """
    return claude_config_dir or Path.home() / ".claude"


def transcript_path(config_dir: Path, cwd: Path, session_id: str) -> Path:
    """Транскрипт сессии в профиле Claude Code.

    Путь проекта (= cwd процесса claude) кодируется заменой '/' и '.' на '-'.
    """
    encoded = str(cwd).replace("/", "-").replace(".", "-")
    return config_dir / "projects" / encoded / f"{session_id}.jsonl"
