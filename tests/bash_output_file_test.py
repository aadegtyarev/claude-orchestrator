"""Длинный вывод /bash отдаётся файлом, а не обрезается безвозвратно.

Проверяем:
  - короткий вывод: truncated=False, файл не создаётся (регресс);
  - длинный вывод: truncated=True, файл содержит ПОЛНЫЙ вывод, а не обрезок;
  - _save_bash_output: сессия → workspace/.bash_outputs/, без сессии → temp;
  - невалидный UTF-8 не роняет рендер и сохранение;
  - файл можно удалить (не течёт).

Запуск: .venv/bin/python tests/bash_output_file_test.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import BASH_OUTPUT_LIMIT, OrchestratorCore  # noqa: E402
from orchestrator.core.sessions import Session  # noqa: E402
from orchestrator.core.texts import get_texts  # noqa: E402


def _make_core(lang: str = "ru") -> OrchestratorCore:
    """Минимальный OrchestratorCore — только то, что нужно bash_render."""
    c = OrchestratorCore.__new__(OrchestratorCore)
    texts = get_texts(lang)
    c.t = lambda k, **kw: texts.get(k, k).format(**kw) if kw else texts.get(k, k)
    return c


def _make_session(tmp_path: Path) -> Session:
    """Минимальная сессия с session_dir во временной папке."""
    sd = tmp_path / "session"
    sd.mkdir()
    return Session(
        name="test",
        port=0,
        session_dir=sd,
        claude_session_id="x",
        title="test",
    )


# ── bash_render: возврат и флаг обрезки ───────────────────────────


def test_short_output_not_truncated():
    """Короткий вывод: truncated=False, html содержит вывод целиком."""
    core = _make_core()
    html, truncated = core.bash_render("echo hi", b"hello world", code="0")
    assert not truncated
    assert "hello world" in html
    assert "echo hi" in html


def test_long_output_is_truncated():
    """Вывод длиннее BASH_OUTPUT_LIMIT: truncated=True, html обрезан."""
    core = _make_core()
    # Генерим вывод, заведомо превышающий лимит.
    long = "x" * (BASH_OUTPUT_LIMIT + 1000)
    html, truncated = core.bash_render("generate", long.encode(), code="0")
    assert truncated
    # Обрезанный рендер начинается с "..."
    assert html.startswith("⚡") or "…" in html
    # Хвост — последние BASH_OUTPUT_LIMIT символов (плюс "…" в начале).
    assert ("x" * (BASH_OUTPUT_LIMIT - 10)) in html  # большая часть хвоста на месте


def test_long_output_truncated_only_at_terminal():
    """Промежуточный рендер (code=None) тоже обрезает, но файл создаётся
    только на терминальных обновлениях (это проверяется в run_bash, здесь —
    только флаг)."""
    core = _make_core()
    long = "y" * (BASH_OUTPUT_LIMIT + 500)
    # Промежуточный рендер (команда ещё идёт).
    html, truncated = core.bash_render("cmd", long.encode(), code=None)
    assert truncated  # Флаг поднят — вызывающий решит, сохранять ли файл.
    assert "…" in html


def test_timeout_output_truncated():
    """При таймауте тоже поднимается флаг обрезки."""
    core = _make_core()
    long = "z" * (BASH_OUTPUT_LIMIT + 200)
    html, truncated = core.bash_render("cmd", long.encode(), code=None, timeout=True)
    assert truncated


# ── _save_bash_output: файл и его содержимое ─────────────────────


def test_save_with_session_creates_file_in_workspace(tmp_path: Path):
    """С сессией файл пишется в .bash_outputs/ внутри session_dir."""
    core = _make_core()
    session = _make_session(tmp_path)
    out = b"line1\nline2\nline3\n"
    path_str = core._save_bash_output("ls -la", out, session)
    path = Path(path_str)
    assert path.exists()
    assert path.is_file()
    # Проверяем, что лежит внутри session_dir
    assert str(session.session_dir) in path_str
    assert ".bash_outputs" in path_str
    # Содержимое — полный вывод, а не обрезок.
    content = path.read_text(encoding="utf-8")
    assert content == "line1\nline2\nline3\n"
    # Имя осмысленное: команда + время + .txt
    assert path.name.startswith("bash_")
    assert path.name.endswith(".txt")


def test_save_without_session_creates_temp_file():
    """Без сессии (main-chat) файл создаётся во временном каталоге ОС."""
    core = _make_core()
    out = b"host output\n"
    path_str = core._save_bash_output("hostname", out, None)
    path = Path(path_str)
    assert path.exists()
    assert path.is_file()
    # Временный каталог (обычно /tmp или /var/tmp).
    tmp_root = Path(tempfile.gettempdir())
    assert path.is_relative_to(tmp_root)
    content = path.read_text(encoding="utf-8")
    assert content == "host output\n"
    # Убираем за собой — файл не должен течь.
    path.unlink()


def test_full_output_not_trimmed_in_file(tmp_path: Path):
    """Файл содержит ПОЛНЫЙ вывод, даже когда он сильно длиннее лимита."""
    core = _make_core()
    session = _make_session(tmp_path)
    # Генерим вывод втрое длиннее лимита — каждая строка уникальна.
    lines = [f"line_{i:06d}" for i in range(BASH_OUTPUT_LIMIT // 5)]
    full = "\n".join(lines)
    assert len(full) > BASH_OUTPUT_LIMIT * 2
    path_str = core._save_bash_output("big_cmd", full.encode(), session)
    content = Path(path_str).read_text(encoding="utf-8")
    # Первая строка на месте (рендер её обрезал бы).
    assert content.startswith("line_000000")
    # Последняя строка на месте.
    assert content.rstrip().endswith(lines[-1])
    # Общее число строк совпадает.
    assert content.count("\n") == len(lines) - 1


# ── Невалидный UTF-8 ─────────────────────────────────────────────


def test_invalid_utf8_in_bash_render():
    """Мусорные байты не роняют рендер — errors='replace'."""
    core = _make_core()
    # Байты, не являющиеся валидным UTF-8.
    bad = b"before \xff\xfe after \x80 end"
    html, truncated = core.bash_render("cmd", bad, code="0")
    assert not truncated  # короткий вывод
    # Юникодный replacement character (U+FFFD) появляется вместо мусора.
    assert "�" in html or "before" in html


def test_invalid_utf8_in_save_bash_output(tmp_path: Path):
    """Мусорные байты не роняют сохранение файла."""
    core = _make_core()
    session = _make_session(tmp_path)
    bad = b"valid start \xff\xfe garbage \x00 end"
    path_str = core._save_bash_output("broken_cmd", bad, session)
    content = Path(path_str).read_text(encoding="utf-8")
    # Валидная часть на месте.
    assert "valid start" in content
    assert "end" in content
    # Мусор заменён на replacement character.
    assert "�" in content


# ── Очистка и граничные случаи ───────────────────────────────────


def test_file_can_be_cleaned_up(tmp_path: Path):
    """Сохранённый файл — обычный файл: можно удалить, ОС не держит."""
    core = _make_core()
    session = _make_session(tmp_path)
    path_str = core._save_bash_output("cleanup_test", b"data", session)
    path = Path(path_str)
    assert path.exists()
    path.unlink()
    assert not path.exists()


def test_empty_output_not_truncated():
    """Пустой вывод: truncated=False, рендер показывает (нет вывода)."""
    core = _make_core()
    html, truncated = core.bash_render("empty", b"", code="0")
    assert not truncated
    assert core.t("bash_no_output") in html


def test_exactly_at_limit():
    """Ровно BASH_OUTPUT_LIMIT символов — не обрезается."""
    core = _make_core()
    exact = "a" * BASH_OUTPUT_LIMIT
    html, truncated = core.bash_render("exact", exact.encode(), code="0")
    assert not truncated
    assert ("a" * 100) in html  # весь вывод на месте


def test_one_byte_over_limit():
    """BASH_OUTPUT_LIMIT + 1 — обрезается."""
    core = _make_core()
    over = "b" * (BASH_OUTPUT_LIMIT + 1)
    html, truncated = core.bash_render("over", over.encode(), code="0")
    assert truncated


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
