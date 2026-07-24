"""Чтение транскриптов Claude Code: путь, статистика, скан загрязнения.

Транскрипт — JSONL в профиле Claude Code (CLAUDE_CONFIG_DIR/projects/…),
путь кодируется от cwd процесса claude. Все функции — чистые над файлом
и записями, без Telegram/сессий; блокирующее чтение файлов дёргать через
asyncio.to_thread.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Путь транскрипта — это «конфиг клиента» (КУДА claude пишет транскрипт), а не
# чтение файла; он вынесен в автономный пакет box/ (Слой 2 редизайна,
# docs/ARCHITECTURE-claude-box.md §5.2). Реэкспорт: код/тесты ссылаются на
# transcript.transcript_path как раньше — поведение байт-в-байт то же.
from box.transcript_path import transcript_path  # noqa: F401

# id «честного» server_tool_use от Anthropic — srvtoolu_<base>; чужой бэкенд
# (z.ai/GLM) лепит id другого формата, на нём реальный Anthropic падает с 400.
_SRVTOOLU_RE = re.compile(r"^srvtoolu_[A-Za-z0-9_]+$")


def block_snippet(block: dict, limit: int = 280) -> str:
    """Сжатый человекочитаемый обрезок содержимого блока транскрипта."""
    t = block.get("type")
    if t in ("text", "thinking"):
        body = str(block.get("text") or block.get("thinking") or "")
    elif t in ("tool_use", "server_tool_use"):
        body = f"{block.get('name', '?')}({json.dumps(block.get('input', {}), ensure_ascii=False)})"
    elif t == "tool_result":
        c = block.get("content")
        body = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    else:
        body = json.dumps(block, ensure_ascii=False)
    body = " ".join(body.split())
    return body[:limit] + ("…" if len(body) > limit else "")


def scan_pollution(entries) -> str | None:
    """Найти загрязнение чужим бэкендом в записях транскрипта (новейшие — в конце).

    Возвращает 'роль: маркер → обрезок' для самого свежего загрязнённого блока
    либо None. Чистая функция — тестируется без файла/Telegram. Маркеры:
      • thinking без signature — настоящий Anthropic ВСЕГДА подписывает thinking,
        неподписанный = история пришла с другого бэкенда (z.ai/GLM);
      • server_tool_use с id не формата srvtoolu_…;
      • tool_result внутри assistant-сообщения (смещённый/чужой).
    """
    for entry in reversed(entries):
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or entry.get("type") or "?"
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            marker = None
            if btype == "thinking" and not b.get("signature"):
                marker = "thinking без подписи (чужой бэкенд)"
            elif btype == "server_tool_use":
                if not _SRVTOOLU_RE.match(str(b.get("id", ""))):
                    marker = "server_tool_use с чужим id (не srvtoolu_…)"
            elif btype == "tool_result" and role == "assistant":
                marker = "tool_result в assistant-сообщении (чужой бэкенд)"
            if marker:
                return f"{role}: {marker} → {block_snippet(b)}"
    return None


def read_stats(path: Path) -> dict | None:
    """Статистика из транскрипта. None — транскрипт ещё не создан.

    Блокирующее чтение файла — вызывать через asyncio.to_thread.
    """
    if not path.exists():
        return None
    turns = 0
    total_output = 0
    last_usage: dict = {}
    model = ""
    parsed_lines = 0
    try:
        f = open(path, encoding="utf-8")
    except OSError:
        return None
    with f:
        for line in f:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            parsed_lines += 1
            if entry.get("type") == "user":
                content = (entry.get("message") or {}).get("content")
                # tool_result тоже приходит user-записью — не считаем его.
                if isinstance(content, str) or (
                    isinstance(content, list)
                    and not any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content
                    )
                ):
                    turns += 1
            elif entry.get("type") == "assistant":
                message = entry.get("message") or {}
                usage = message.get("usage") or {}
                if usage:
                    last_usage = usage
                    total_output += usage.get("output_tokens", 0)
                model = message.get("model") or model
    context = (
        last_usage.get("input_tokens", 0)
        + last_usage.get("cache_read_input_tokens", 0)
        + last_usage.get("cache_creation_input_tokens", 0)
    )
    size = path.stat().st_size
    # Детект «формат Claude Code изменился»: транскрипт заметного размера
    # распарсился (JSONL валиден), но НИ одного ожидаемого поля не извлекли
    # (ни ходов, ни usage, ни модели) — вероятно, поменялась схема записей.
    # Пустой/крошечный транскрипт (новая сессия) под это не подпадает.
    stale_schema = (
        size > 4096 and parsed_lines > 5
        and turns == 0 and total_output == 0 and not last_usage and not model
    )
    return {
        "model": model,
        "context_tokens": context,
        "output_tokens": total_output,
        "turns": turns,
        "transcript_bytes": size,
        "stale_schema": stale_schema,
    }


_MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"]+)"')


def read_last_model(path: Path, tail_bytes: int = 65536) -> str | None:
    """Реальная модель последнего ответа (после подмены прокси) — из хвоста
    транскрипта. Дёшево: читаем только последние tail_bytes и берём последний
    `"model":"…"`. Прокси может подменять opus→glm и т.п., и это меняется по
    ходу — показываем в бабле фактическую. `<synthetic>` (служебные записи)
    игнорируем. None — не нашли."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            raw = f.read()
    except OSError:
        return None
    for m in reversed(_MODEL_RE.findall(raw)):
        val = m.decode("utf-8", "replace")
        if val and val != "<synthetic>":
            return val
    return None


def read_pollution_excerpt(path: Path, max_entries: int = 25) -> str | None:
    """Эксцепт загрязнения чужим бэкендом из хвоста транскрипта (или None).

    Мусор лежит в недавнем хвосте, поэтому смотрим последние записи и
    отдаём результат scan_pollution. Блокирующее чтение — вызывать через
    asyncio.to_thread (как read_stats).
    """
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-max_entries * 2:]
    except OSError:
        return None
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return scan_pollution(entries)
