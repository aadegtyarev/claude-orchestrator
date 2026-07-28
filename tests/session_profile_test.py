"""Профиль Claude Code на сессию (`/new … --profile`).

Профиль — отдельная учётка claude: свои токены, скиллы и транскрипты. Дефолт
берётся из .env (CLAUDE_PROFILE), сессия может переопределить его при создании,
и выбор живёт с сессией (resume/clear поднимают её тем же профилем).

Ключевые инварианты, которые тут проверяются:
  • `--profile ""` побеждает дефолт .env (иначе от профиля не отказаться);
  • процесс claude, RW-бинд песочницы и путь транскрипта смотрят в ОДИН
    каталог (разъедутся — claude пишет в профиль, а /stats читает чужое);
  • под agent-vm профиль отвергается, а не проглатывается молча.

Запуск: .venv/bin/python tests/session_profile_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from box import profiles  # noqa: E402
from orchestrator.core.errors import UserError  # noqa: E402
from orchestrator.core.sessions import Session, SessionManager  # noqa: E402


def make_manager(claude_profile=None, claude_config_dir=None) -> SessionManager:
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = SimpleNamespace(
        claude_profile=claude_profile,
        claude_config_dir=claude_config_dir,
        sandbox="bwrap",
    )
    return mgr


def make_session(profile=None) -> Session:
    return Session(
        name="s", port=0, session_dir=Path("/tmp/s"),
        claude_session_id="uuid", profile=profile,
    )


# ── выбор профиля: сессия vs .env ────────────────────────────────────


def test_session_without_choice_takes_env_default():
    mgr = make_manager(claude_profile="work")
    assert mgr.profile_of(make_session(profile=None)) == "work"


def test_session_choice_wins_over_env():
    mgr = make_manager(claude_profile="work")
    assert mgr.profile_of(make_session(profile="private")) == "private"


def test_empty_string_means_explicitly_no_profile():
    """`--profile ""` обязан побеждать дефолт .env, иначе от профиля не уйти."""
    mgr = make_manager(claude_profile="work")
    assert mgr.profile_of(make_session(profile="")) is None


def test_no_profile_anywhere_is_none():
    mgr = make_manager(claude_profile=None)
    assert mgr.profile_of(make_session(profile=None)) is None


# ── каталог учётки ───────────────────────────────────────────────────


def test_config_dir_points_into_profile(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CLAUDE_BOX_HOME", str(tmp_path))
    mgr = make_manager(claude_profile=None)
    got = mgr.config_dir_of(make_session(profile="work"))
    assert got == tmp_path / "profiles" / "work" / ".claude"
    # Каталог создан: он нужен существующим и claude, и bind'у песочницы.
    assert got.is_dir()


def test_config_dir_falls_back_to_env_path(tmp_path: Path):
    """Без профиля — общий CLAUDE_CONFIG_DIR, ровно прежнее поведение."""
    mgr = make_manager(claude_profile=None, claude_config_dir=tmp_path / ".claude-bot")
    assert mgr.config_dir_of(make_session()) == tmp_path / ".claude-bot"


def test_explicit_no_profile_uses_env_path_even_with_default(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("CLAUDE_BOX_HOME", str(tmp_path))
    mgr = make_manager(
        claude_profile="work", claude_config_dir=tmp_path / ".claude-bot")
    assert mgr.config_dir_of(make_session(profile="")) == tmp_path / ".claude-bot"


def test_two_sessions_get_different_config_dirs(monkeypatch, tmp_path: Path):
    """Суть фичи: разные сессии — разные учётки."""
    monkeypatch.setenv("CLAUDE_BOX_HOME", str(tmp_path))
    mgr = make_manager(claude_profile=None)
    a = mgr.config_dir_of(make_session(profile="work"))
    b = mgr.config_dir_of(make_session(profile="private"))
    assert a != b


def test_transcript_reads_from_session_profile(monkeypatch, tmp_path: Path):
    """Транскрипт (/stats) ищется в профиле СЕССИИ, а не в общем конфиге.

    Разъедься эти пути — claude пишет в профиль, а статистика читает пустоту.
    """
    monkeypatch.setenv("CLAUDE_BOX_HOME", str(tmp_path))
    mgr = make_manager(claude_profile=None, claude_config_dir=tmp_path / "общий")
    session = make_session(profile="work")
    mgr.effective_cwd = lambda s: Path("/home/u/proj")
    path = mgr.transcript_path(session)
    assert str(path).startswith(str(tmp_path / "profiles" / "work" / ".claude"))
    assert path.name == "uuid.jsonl"


def test_agent_vm_session_has_no_profile(monkeypatch, tmp_path: Path):
    """Под agent-vm профиля нет ни у кого — даже дефолтного из .env.

    Гость игнорирует хостовый CLAUDE_CONFIG_DIR: выставить его значило бы
    показать оператору учётку, под которой сессия на самом деле НЕ работает.
    """
    monkeypatch.setenv("CLAUDE_BOX_HOME", str(tmp_path))
    mgr = make_manager(claude_profile="work")
    mgr.config.sandbox = "agent-vm"
    assert mgr.profile_of(make_session(profile=None)) is None
    # И каталог профиля впустую не создаётся.
    assert not (tmp_path / "profiles" / "work").exists()
    # Сессия под bwrap на той же машине профиль получает.
    bwrap_session = make_session(profile=None)
    bwrap_session.sandbox = "bwrap"
    assert mgr.profile_of(bwrap_session) == "work"


# ── что видит песочница ──────────────────────────────────────────────


def make_bwrap_argv(config_dir: Path | None, tmp_path: Path) -> list[str]:
    from orchestrator.runners.bwrap import BwrapRunner

    cfg = SimpleNamespace(
        claude_config_dir=None, claude_profile=None, sandbox="bwrap",
        sandbox_extra_rw=(), sandbox_dbus=False, sandbox_docker=False,
    )
    return BwrapRunner(cfg, tmp_path / "repo").wrap(
        ["claude"], chdir=tmp_path, extra_rw=[tmp_path],
        home_dir=tmp_path / "home", config_dir=config_dir,
    )


def rw_binds(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == "--bind-try"]


def test_sandbox_binds_session_profile(monkeypatch, tmp_path: Path):
    """Каталог профиля обязан попасть в песочницу RW — иначе claude его не увидит."""
    monkeypatch.setenv("CLAUDE_BOX_HOME", str(tmp_path / "box"))
    mgr = make_manager(claude_profile=None)
    config_dir = mgr.config_dir_of(make_session(profile="work"))
    binds = rw_binds(make_bwrap_argv(config_dir, tmp_path))
    assert str(config_dir) in binds


def test_sandbox_with_profile_hides_operator_claude_json(
    monkeypatch, tmp_path: Path
):
    """Под профилем реальный ~/.claude.json оператора в песочницу НЕ едет.

    Иначе изоляция учётки дырявая: профиль свой, а глобальное состояние
    claude (в т.ч. чужие проекты и авторизация) — общее.
    """
    monkeypatch.setenv("CLAUDE_BOX_HOME", str(tmp_path / "box"))
    mgr = make_manager(claude_profile=None)
    config_dir = mgr.config_dir_of(make_session(profile="work"))
    binds = rw_binds(make_bwrap_argv(config_dir, tmp_path))
    assert not any(b.endswith("/.claude.json") for b in binds)
    # Без профиля он по-прежнему нужен (прежнее поведение не сломано).
    assert any(b.endswith("/.claude.json") for b in rw_binds(make_bwrap_argv(None, tmp_path)))


# ── сохранение выбора между запусками ────────────────────────────────


def test_profile_survives_state_roundtrip(tmp_path: Path):
    """Профиль живёт с сессией: после рестарта она поднимется той же учёткой."""
    mgr = make_manager()
    mgr.config.sessions_dir = tmp_path
    mgr._by_name = {"s": make_session(profile="work")}
    mgr.save_state()

    restored = make_manager()
    restored.config.sessions_dir = tmp_path
    restored._by_name = {}
    restored.load_state()
    assert restored._by_name["s"].profile == "work"


def test_records_without_profile_field_load_as_default(tmp_path: Path):
    """Записи, созданные до фичи, профиля не помнят → дефолт .env."""
    import json
    (tmp_path / ".sessions.json").write_text(json.dumps([{
        "name": "old", "cwd": str(tmp_path / "old"),
        "claude_session_id": "uuid", "title": "old",
    }]))
    mgr = make_manager(claude_profile="work")
    mgr.config.sessions_dir = tmp_path
    mgr._by_name = {}
    mgr.load_state()
    assert mgr._by_name["old"].profile is None
    assert mgr.profile_of(mgr._by_name["old"]) == "work"


# ── проверка значения флага (ядро) ───────────────────────────────────


def make_core(sandbox="bwrap", claude_profile=None):
    from orchestrator.core.app import OrchestratorCore
    from orchestrator.core.texts import get_texts

    core = OrchestratorCore.__new__(OrchestratorCore)
    core.config = SimpleNamespace(sandbox=sandbox, claude_profile=claude_profile)
    core._texts = get_texts("ru")
    return core


def test_resolve_profile_absent_flag_is_none():
    assert make_core().resolve_profile(None, None) is None


def test_resolve_profile_empty_is_explicit_optout():
    assert make_core().resolve_profile("", None) == ""
    assert make_core().resolve_profile("   ", None) == ""


def test_resolve_profile_validates_name():
    """Имя идёт в path-join — traversal обязан отвергаться ДО создания сессии."""
    core = make_core()
    for bad in ("../escape", "a/b", "~root", "-flag", "x" * 65):
        with pytest.raises(UserError):
            core.resolve_profile(bad, None)


def test_resolve_profile_accepts_good_name():
    assert make_core().resolve_profile("work-1.2", None) == "work-1.2"


def test_resolve_profile_rejects_under_vm():
    """Под agent-vm профиль не работает — отказываем, а не молчим."""
    core = make_core(sandbox="agent-vm")
    with pytest.raises(UserError, match="agent-vm"):
        core.resolve_profile("work", None)
    # Движок сессии (её --box) важнее дефолта: bwrap-сессия профиль получает.
    assert core.resolve_profile("work", "bwrap") == "work"


def test_resolve_profile_rejects_when_session_chose_vm():
    core = make_core(sandbox="bwrap")
    with pytest.raises(UserError, match="agent-vm"):
        core.resolve_profile("work", "agent-vm")


def test_profile_name_rules_match_claude_box():
    """Правила имени общие с claude-box — один источник истины."""
    assert profiles.validate_name("work") == "work"
    with pytest.raises(profiles.ProfileError):
        profiles.validate_name("../escape")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
