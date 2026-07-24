"""box.transcript_path — «конфиг клиента»: путь транскрипта claude (Слой 2).

Вынос из orchestrator.core.transcript в автономный box/. Проверяем, что путь
кодируется БАЙТ-В-БАЙТ как прежняя inline-реализация (Claude Code хранит
транскрипт по cwd — расхождение сломало бы чтение статистики), и что дефолт
профиля (CLAUDE_CONFIG_DIR|~/.claude) резолвится идентично старому выражению.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box.transcript_path import resolve_config_dir, transcript_path  # noqa: E402


def _legacy_transcript_path(config_dir: Path, cwd: Path, session_id: str) -> Path:
    """Точная копия прежней реализации (orchestrator.core.transcript до выноса)."""
    encoded = str(cwd).replace("/", "-").replace(".", "-")
    return config_dir / "projects" / encoded / f"{session_id}.jsonl"


CWDS = [
    Path("/home/user/proj"),
    Path("/home/user/.config/app"),          # точки в пути
    Path("/srv/a.b.c/d"),
    Path("/home/user/proj.d/sub.dir/x"),
    Path("/"),                                # корень
    Path("/opt/repo-name_1"),                # дефис/подчёркивание сохраняются
]


def test_transcript_path_byte_for_byte_matches_legacy():
    """Путь идентичен прежней inline-реализации для всех форм cwd."""
    config_dir = Path("/home/u/.claude")
    sid = "1234abcd-0000-0000-0000-000000000000"
    for cwd in CWDS:
        got = transcript_path(config_dir, cwd, sid)
        want = _legacy_transcript_path(config_dir, cwd, sid)
        assert got == want, f"cwd={cwd}: {got} != {want}"


def test_transcript_path_encoding_shape():
    """'/' и '.' → '-'; итог: <config>/projects/<encoded>/<sid>.jsonl."""
    p = transcript_path(
        Path("/prof/dir"), Path("/home/user/.a.b/proj"), "sid-1"
    )
    assert p == Path("/prof/dir/projects/-home-user--a-b-proj/sid-1.jsonl")


def test_resolve_config_dir_explicit_wins():
    """Заданный CLAUDE_CONFIG_DIR используется как есть."""
    explicit = Path("/custom/claude-config")
    assert resolve_config_dir(explicit) == explicit


def test_resolve_config_dir_default_matches_legacy():
    """None → ~/.claude — идентично старому выражению в SessionManager/app."""
    assert resolve_config_dir(None) == Path.home() / ".claude"
    # То же выражение, что раньше жило в оркестраторе (оператор precedence: '/'
    # связывает сильнее 'or') — фиксируем идентичность.
    legacy = None or Path.home() / ".claude"
    assert resolve_config_dir(None) == legacy


def test_reexport_is_same_object():
    """orchestrator.core.transcript.transcript_path — тот же объект, что в box
    (реэкспорт, не копия): старые импорты дают идентичное поведение."""
    from orchestrator.core import transcript as core_transcript

    assert core_transcript.transcript_path is transcript_path


def main():
    test_transcript_path_byte_for_byte_matches_legacy()
    test_transcript_path_encoding_shape()
    test_resolve_config_dir_explicit_wins()
    test_resolve_config_dir_default_matches_legacy()
    test_reexport_is_same_object()
    print("ALL BOX-TRANSCRIPT-PATH OK")


if __name__ == "__main__":
    main()
