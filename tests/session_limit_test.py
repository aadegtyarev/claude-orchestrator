"""Лимит сессий — про ОДНОВРЕМЕННО ЗАПУЩЕННЫЕ, и ресурсный preflight.

Регресс: `count()` считал ВСЕ записи, включая восстановленные с диска
остановленные сессии. После рестарта оркестратора /new упирался в лимит,
хотя живых процессов было ноль.

Запуск: .venv/bin/python tests/session_limit_test.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.sessions import (  # noqa: E402
    Session,
    SessionError,
    SessionManager,
)


def make_manager(
    max_instances: int = 5,
    min_free_ram_mb: int = 0,
    sessions_dir: Path | None = None,
) -> SessionManager:
    config = SimpleNamespace(
        max_instances=max_instances,
        min_free_ram_mb=min_free_ram_mb,
        sandbox="bwrap",
        agent_vm_memory_gib=None,
        # Реальный create() делает mkdir — тесты, которые его зовут, передают
        # сюда tmp_path, чтобы не сорить в файловой системе.
        sessions_dir=sessions_dir or Path("/nonexistent-не-используется"),
    )
    # Без __init__: его побочки (порты, http-пул) этим проверкам не нужны.
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = config
    mgr._by_name = {}
    mgr._starting = set()
    return mgr


def add(mgr: SessionManager, name: str, *, running: bool) -> Session:
    session = Session(
        name=name,
        port=0,
        session_dir=Path(f"/tmp/{name}"),
        claude_session_id="uuid",
    )
    # running — свойство поверх process.returncode: живой процесс = ещё не вышел.
    session.process = SimpleNamespace(returncode=None if running else 0)
    mgr._by_name[name] = session
    return session


def test_stopped_sessions_do_not_hold_a_slot():
    mgr = make_manager()
    for i in range(4):
        add(mgr, f"stopped{i}", running=False)
    add(mgr, "alive", running=True)
    assert mgr.count() == 1  # лимит видит только живую
    assert mgr.total() == 5  # записи на месте: топики живы, resume возможен


def test_record_without_process_is_not_running():
    """Только что восстановленная с диска запись — процесса нет вообще."""
    mgr = make_manager()
    session = add(mgr, "restored", running=False)
    session.process = None
    assert mgr.count() == 0


def test_resume_guard_denies_when_all_slots_are_running():
    mgr = make_manager(max_instances=2)
    add(mgr, "a", running=True)
    add(mgr, "b", running=True)
    add(mgr, "c", running=False)
    with pytest.raises(SessionError, match="лимит"):
        mgr._guard_limit("c")


def test_resume_guard_allows_when_a_slot_freed():
    mgr = make_manager(max_instances=2)
    add(mgr, "a", running=True)
    add(mgr, "b", running=False)
    add(mgr, "c", running=False)
    mgr._guard_limit("c")  # одна живая из двух — место есть


def test_starting_session_holds_its_slot():
    """Пока процесс поднимается, его в count() ещё нет — держим резервом.

    Без этого два параллельных подъёма (/new и resume в соседнем топике)
    прошли бы проверку оба и превысили лимит.
    """
    mgr = make_manager(max_instances=1)
    mgr._guard_limit("первая")  # слот занят, процесса ещё нет
    assert mgr.count() == 0
    with pytest.raises(SessionError, match="лимит"):
        mgr._guard_limit("вторая")


def test_released_slot_is_reusable():
    """Провалившийся старт обязан вернуть слот, иначе он утечёт навсегда."""
    mgr = make_manager(max_instances=1)
    mgr._guard_limit("упавшая")
    mgr._release_slot("упавшая")
    mgr._guard_limit("следующая")  # место снова есть


async def test_create_ignores_stopped_records(tmp_path: Path):
    """Регресс: /new при N остановленных записях и нуле живых процессов.

    Именно этот путь оставался на len(_by_name) — после рестарта оркестратора
    (все записи восстановлены остановленными) /new отказывал на пустой машине.
    """
    mgr = make_manager(max_instances=2, sessions_dir=tmp_path)
    for i in range(2):
        add(mgr, f"stopped{i}", running=False)

    mgr._lock = asyncio.Lock()
    mgr._inflight_ports = set()
    started: list[str] = []

    mgr._check_engine = lambda engine: None
    mgr._allocate_port = lambda s: None
    mgr._link_project = lambda p: p
    mgr._guard_unique_cwd = lambda s: None
    mgr._write_configs = lambda s: None
    mgr._start_watcher = lambda s: None
    mgr.save_state = lambda: None

    async def fake_start(session, resume=False):
        started.append(session.name)
        session.process = SimpleNamespace(returncode=None)

    mgr._start_claude = fake_start
    mgr._wait_ready = lambda s: asyncio.sleep(0)

    session = await mgr.create("новая", project_path=None, sandbox=None)
    assert started == [session.name]
    # Слот отпущен: сессия теперь считается живой через count().
    assert mgr._starting == set()
    assert mgr.count() == 1


async def test_create_releases_slot_when_start_fails(tmp_path: Path):
    """Упавший старт возвращает слот: иначе имя «вечно стартует» и ест лимит."""
    mgr = make_manager(max_instances=1, sessions_dir=tmp_path)
    mgr._lock = asyncio.Lock()
    mgr._inflight_ports = set()
    mgr._check_engine = lambda engine: None
    mgr._allocate_port = lambda s: None
    mgr._guard_unique_cwd = lambda s: None
    mgr._write_configs = lambda s: None
    mgr.save_state = lambda: None
    mgr._terminate = lambda s: asyncio.sleep(0)

    async def boom(session, resume=False):
        raise SessionError("claude не поднялся")

    mgr._start_claude = boom
    with pytest.raises(SessionError, match="не поднялся"):
        await mgr.create("падучая", project_path=None, sandbox=None)
    assert mgr._starting == set()
    assert mgr.count() == 0


async def test_create_releases_slot_when_cancelled(tmp_path: Path):
    """Отмена задачи (CancelledError — не Exception) тоже возвращает слот."""
    mgr = make_manager(max_instances=1, sessions_dir=tmp_path)
    mgr._lock = asyncio.Lock()
    mgr._inflight_ports = set()
    mgr._check_engine = lambda engine: None
    mgr._allocate_port = lambda s: None
    mgr._guard_unique_cwd = lambda s: None
    mgr._write_configs = lambda s: None
    mgr.save_state = lambda: None
    mgr._terminate = lambda s: asyncio.sleep(0)

    async def cancelled(session, resume=False):
        raise asyncio.CancelledError()

    mgr._start_claude = cancelled
    with pytest.raises(asyncio.CancelledError):
        await mgr.create("отменённая", project_path=None, sandbox=None)
    assert mgr._starting == set()


def test_reserved_slot_survives_restart_of_running_session():
    """Перезапуск живой сессии (/clear, смена модели, revive) не отдаёт слот.

    В паузе между stop и start count() показывает на единицу меньше — без
    резерва конкурентный /new занял бы это место, и после подъёма живых стало
    бы больше лимита.
    """
    mgr = make_manager(max_instances=1)
    session = add(mgr, "живая", running=True)
    mgr._reserve_slot("живая")
    session.process = SimpleNamespace(returncode=0)  # процесс погашен
    assert mgr.count() == 0  # count() её уже не видит...
    with pytest.raises(SessionError, match="лимит"):
        mgr._guard_limit("чужая")  # ...но место занято резервом


def test_restart_of_running_session_is_not_denied_by_its_own_slot():
    """Свой же резерв не должен отказывать сессии в перезапуске."""
    mgr = make_manager(max_instances=1)
    add(mgr, "живая", running=True)
    mgr._reserve_slot("живая")
    mgr._guard_limit("живая")  # не бросает: слот уже её


async def test_create_denies_when_limit_of_running_reached():
    mgr = make_manager(max_instances=1)
    add(mgr, "живая", running=True)
    mgr._lock = asyncio.Lock()
    mgr._check_engine = lambda engine: None

    with pytest.raises(SessionError, match="лимит"):
        await mgr.create("ещё-одна", project_path=None, sandbox=None)


def test_resources_check_disabled_by_default_threshold_zero(monkeypatch):
    mgr = make_manager(min_free_ram_mb=0)
    monkeypatch.setattr(
        "orchestrator.core.resources.available_ram_mb", lambda *a, **kw: 10
    )
    mgr.check_resources(None)  # порог 0 = не проверяем даже при 10 МБ


def test_resources_check_denies_when_ram_low(monkeypatch):
    mgr = make_manager(min_free_ram_mb=1024)
    monkeypatch.setattr(
        "orchestrator.core.resources.available_ram_mb", lambda *a, **kw: 900
    )
    with pytest.raises(SessionError, match="памяти"):
        mgr.check_resources(None)


def test_resources_check_passes_when_ram_plenty(monkeypatch):
    mgr = make_manager(min_free_ram_mb=1024)
    monkeypatch.setattr(
        "orchestrator.core.resources.available_ram_mb", lambda *a, **kw: 8000
    )
    mgr.check_resources(None)


def test_vm_session_costs_its_vm_memory(monkeypatch):
    """agent-vm просит 4 GiB — при 4.5 GiB свободных с порогом 1 GiB не влезает."""
    mgr = make_manager(min_free_ram_mb=1024)
    mgr.config.agent_vm_memory_gib = 4
    monkeypatch.setattr(
        "orchestrator.core.resources.available_ram_mb", lambda *a, **kw: 4608
    )
    with pytest.raises(SessionError, match="памяти"):
        mgr.check_resources("agent-vm")
    # Той же машине сессия под bwrap (оценка ~700 МБ) по карману.
    mgr.check_resources("bwrap")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
