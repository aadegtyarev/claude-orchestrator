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
from orchestrator.core.bashshell import OutputCapture  # noqa: E402
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


# ── путь файла и запись через OutputCapture ──────────────────────
#
# Раньше вывод сохранялся отдельным проходом (_save_bash_output) уже ПОСЛЕ
# команды — из кольцевого буфера PTY, который к тому моменту мог потерять
# начало. Теперь пишет OutputCapture по ходу выполнения, поэтому проверяем
# путь (куда) и запись (что именно легло).


def capture_to(path: Path, payload: bytes, tail_cap: int = 100):
    """Прогнать вывод через перехват и вернуть его же (файл уже закрыт)."""
    cap = OutputCapture(path, "__B__", "__D__", tail_cap=tail_cap)
    cap.feed(b"__B__\n" + payload + b"__D__ 0\n")
    cap.close()
    return cap


def test_path_with_session_is_inside_workspace(tmp_path: Path):
    """С сессией файл лежит в .bash_outputs/ её папки — веб отдаёт из jail."""
    core = _make_core()
    session = _make_session(tmp_path)
    path = core.bash_output_path("ls -la", session)
    assert path.parent == session.session_dir / ".bash_outputs"
    assert path.name.startswith("bash_") and path.name.endswith(".txt")


def test_path_without_session_is_in_tempdir():
    """Без сессии (главный чат) — временный каталог ОС."""
    core = _make_core()
    path = core.bash_output_path("hostname", None)
    assert path.is_relative_to(Path(tempfile.gettempdir()))


def test_full_output_not_trimmed_in_file(tmp_path: Path):
    """В файле ПОЛНЫЙ вывод, включая начало, которое не влезло в хвост."""
    lines = [f"line_{i:06d}" for i in range(BASH_OUTPUT_LIMIT // 5)]
    payload = ("\n".join(lines) + "\n").encode()
    cap = capture_to(tmp_path / "out.txt", payload)
    content = cap.path.read_text(encoding="utf-8")
    assert content.startswith("line_000000")     # начало на месте
    assert content.rstrip().endswith(lines[-1])  # и конец тоже
    assert content.strip().count("\n") == len(lines) - 1  # все строки на месте


def test_no_file_for_short_output(tmp_path: Path):
    """Короткий вывод файла не заводит — не сорим на каждом `ls`."""
    cap = capture_to(tmp_path / "out.txt", "коротко\n".encode(), tail_cap=10_000)
    assert not cap.overflowed
    assert not cap.path.exists()


def test_symlinked_folder_is_refused(tmp_path: Path):
    """Папку вывода модель видит на запись: симлинк наружу — отказ.

    Иначе и запись, и чистка ушли бы по ссылке и удаляли чужие bash_*.txt.
    """
    victim = tmp_path / "чужое"
    victim.mkdir()
    link = tmp_path / ".bash_outputs"
    link.symlink_to(victim)
    cap = capture_to(link / "out.txt", b"x" * 5000)
    assert not cap.path.exists()          # ничего не записали
    assert list(victim.iterdir()) == []    # и чужую папку не тронули


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


def test_invalid_utf8_written_to_file(tmp_path: Path):
    """Мусорные байты не роняют запись — в файл едут как есть, а показ
    декодируется с заменой (см. bash_render)."""
    bad = b"valid start \xff\xfe garbage \x00 end\n"
    cap = capture_to(tmp_path / "out.txt", bad + b"x" * 5000)
    content = cap.path.read_bytes()
    assert b"valid start" in content and b"end" in content


# ── Очистка и граничные случаи ───────────────────────────────────


def test_old_files_are_trimmed(tmp_path: Path):
    """Файлы вывода не копятся: держим только последние (BASH_OUTPUTS_KEEP)."""
    import os
    for i in range(5):
        f = tmp_path / f"bash_2026010{i}_00000{i}_cmd.txt"
        f.write_text("данные", encoding="utf-8")
        os.utime(f, (1000 + i, 1000 + i))
    OrchestratorCore._trim_bash_outputs(tmp_path, keep=2)
    assert len(list(tmp_path.glob("bash_*.txt"))) == 2


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
