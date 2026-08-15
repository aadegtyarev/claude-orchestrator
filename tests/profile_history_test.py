"""Перенос диалога при смене учётки (`/profile` → «Перенести историю»).

Смена профиля меняет каталог, где Claude Code ищет транскрипт: `--resume
<uuid>` не находит файл в НОВОЙ учётке и стартует с чистого листа, а переписка
остаётся в прежней. Но транскрипт самодостаточен — обычный
`<учётка>/projects/<кодированный cwd>/<uuid>.jsonl`, и cwd при смене профиля не
меняется. Копия в новую учётку возвращает resume к жизни (проверено живьём
15.08.2026 на сессии ikar: 3 МБ диалога развернулись под другой учёткой).

Что проверяем:
  • copy_transcript переносит файл байт-в-байт и ставит права 0600;
  • переносить нечего / та же учётка / сбой ФС — честный False, без исключения
    (смена профиля не должна падать из-за истории);
  • set_profile(keep_history=True) кладёт файл ДО подъёма процесса и НЕ меняет
    UUID диалога — иначе resume не нашёл бы его и всё это было бы зря;
  • set_profile без флага не трогает новую учётку (умолчание — не копировать
    переписку в чужую учётку);
  • has_transcript отвечает на вопрос «есть ли что переносить».

Запуск: .venv/bin/python tests/profile_history_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.core.sessions import Session, SessionManager  # noqa: E402

UUID = "9fd20ab4-7bcb-4a03-b40a-2ffd54dd6196"
CWD = Path("/home/u/sessions/ikar")
BODY = '{"type":"mode","mode":"normal"}\n{"type":"user","text":"привет"}\n'.encode()


def make_manager(config_dir: Path) -> SessionManager:
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = SimpleNamespace(
        claude_profile=None, claude_config_dir=config_dir, sandbox="bwrap",
        claude_env={},
    )
    mgr.effective_cwd = lambda session: CWD
    return mgr


def make_session(profile=None) -> Session:
    return Session(name="ikar", port=0, session_dir=Path("/tmp/ikar"),
                   claude_session_id=UUID, profile=profile)


def put_transcript(config_dir: Path, body: bytes = BODY) -> Path:
    path = config_dir / "projects" / str(CWD).replace("/", "-") / f"{UUID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


# ── копирование ──────────────────────────────────────────────────────────────
def test_copies_byte_for_byte():
    with tempfile.TemporaryDirectory() as d:
        src_dir, dst_dir = Path(d) / "a", Path(d) / "b"
        put_transcript(src_dir)
        mgr = make_manager(src_dir)
        session = make_session()
        assert mgr.copy_transcript(session, src_dir, dst_dir) is True
        dst = mgr.transcript_of(session, dst_dir)
        assert dst.read_bytes() == BODY
        assert oct(dst.stat().st_mode)[-3:] == "600", "переписка — не для всех"


def test_nothing_to_copy_is_false_not_crash():
    """Диалога ещё нет — False, а не исключение: смена профиля продолжается."""
    with tempfile.TemporaryDirectory() as d:
        mgr = make_manager(Path(d) / "a")
        assert mgr.copy_transcript(make_session(), Path(d) / "a", Path(d) / "b") is False


def test_same_account_is_noop():
    """Учётка не менялась — копировать некуда и незачем."""
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "a"
        put_transcript(cfg)
        mgr = make_manager(cfg)
        assert mgr.copy_transcript(make_session(), cfg, cfg) is True


def test_fs_error_is_survivable():
    """Не удалось записать (каталог занят файлом) — False, сессия не падает."""
    with tempfile.TemporaryDirectory() as d:
        src_dir, dst_dir = Path(d) / "a", Path(d) / "b"
        put_transcript(src_dir)
        blocker = dst_dir / "projects"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("я файл, а не каталог")
        mgr = make_manager(src_dir)
        assert mgr.copy_transcript(make_session(), src_dir, dst_dir) is False


def test_has_transcript():
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "a"
        mgr = make_manager(cfg)
        assert mgr.has_transcript(make_session()) is False
        put_transcript(cfg)
        assert mgr.has_transcript(make_session()) is True


# ── смена профиля целиком ────────────────────────────────────────────────────
def run_switch(keep_history: bool):
    """Сменить учётку на профиль «work» и вернуть (наблюдения, каталоги)."""
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    old_dir = root / "shared"
    put_transcript(old_dir)
    os.environ["CLAUDE_BOX_HOME"] = str(root / "box")
    mgr = make_manager(old_dir)
    session = make_session()
    seen = {}

    async def fake_resume(s):
        # Момент истины: к подъёму процесса транскрипт обязан уже лежать в новой
        # учётке — иначе --resume его не найдёт и UUID сменится на новый.
        seen["at_resume"] = mgr.transcript_of(s).is_file()
        seen["uuid"] = s.claude_session_id
        return seen["at_resume"]

    mgr._resume_locked = fake_resume
    resumed = asyncio.run(mgr.set_profile(session, "work", keep_history=keep_history))
    new_dir = mgr.config_dir_of(session)
    os.environ.pop("CLAUDE_BOX_HOME", None)
    d.cleanup()
    return resumed, seen, new_dir


def test_keep_history_transfers_before_start():
    resumed, seen, _ = run_switch(keep_history=True)
    assert seen["at_resume"] is True, "файл должен лежать в новой учётке ДО старта"
    assert seen["uuid"] == UUID, "UUID диалога менять нельзя — resume ищет по нему"
    assert resumed is True


def test_without_flag_history_stays_put():
    _, seen, _ = run_switch(keep_history=False)
    assert seen["at_resume"] is False
    assert seen["uuid"] == UUID  # UUID здесь ещё прежний; новый выдаст _resume_started


def main() -> None:
    test_copies_byte_for_byte()
    test_nothing_to_copy_is_false_not_crash()
    test_same_account_is_noop()
    test_fs_error_is_survivable()
    test_has_transcript()
    test_keep_history_transfers_before_start()
    test_without_flag_history_stays_put()
    print("ALL PROFILE-HISTORY OK")


if __name__ == "__main__":
    main()
