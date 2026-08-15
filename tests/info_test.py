"""Паспорт сессии (`/info`): чем сессия РАБОТАЕТ, а не чем должна была бы.

Настройки сессии размазаны по трём источникам (`.env`, `profile.toml` профиля,
флаги `/new`), и живая сессия могла подняться ДО правки любого из них. По
интерфейсу это не было видно никак: 15.08.2026 сессия молча ходила через
прокси-релей, из-за чего до учётки Team не доезжали managed-настройки
организации и умирал dev-канал, — ни в `/list`, ни в `/stats` не было поля, по
которому это можно заметить.

Отсюда главный инвариант: адрес API у ЗАПУЩЕННОЙ сессии читается из окружения
её процесса, а не из конфига. Конфиг — это намерение; факт живёт в /proc.

Что проверяем:
  • запущенная сессия — адрес берётся из процесса, даже если конфиг с тех пор
    поменяли (и наоборот: остановленная считается из конфига, с пометкой);
  • профиль перекрывает общий CLAUDE_ENV_ANTHROPIC_BASE_URL и в паспорте тоже;
  • отсутствие переменной показывается как «прямой api.anthropic.com»;
  • состояние канала, движок, каталог, модель попадают в текст;
  • под agent-vm не врём про хостовый каталог учётки;
  • plain() снимает разметку — веб печатает текст экранированным.

Запуск: .venv/bin/python tests/info_test.py
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from box import profiles  # noqa: E402
from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.sessions import Session, SessionManager  # noqa: E402

PROXY = "http://127.0.0.1:8787"


def make_core(claude_env=None, profile=None, sandbox="bwrap") -> OrchestratorCore:
    config = SimpleNamespace(
        claude_env=claude_env or {},
        claude_profile=profile,
        claude_config_dir=Path("/home/u/.claude-proxy"),
        sandbox=sandbox,
        modules=["wallet"],
        default_model=None,
        default_effort="high",
        permission_mode="auto",
        bot_lang="ru",
        delete_bubble=False,
        sessions_dir=Path("/tmp"),
        context_window=200_000,
    )
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = config
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.config = config
    core.manager = mgr
    from orchestrator.core.texts import get_texts
    core._texts = get_texts("ru")
    return core


def make_session(profile=None, sandbox=None, running=False, blocked=None,
                 pid=None) -> Session:
    s = Session(
        name="s", port=0, session_dir=Path("/tmp/s"), claude_session_id="uuid",
        profile=profile, sandbox=sandbox,
    )
    s.channel_blocked = blocked
    if running:
        s.process = SimpleNamespace(pid=pid or os.getpid(), returncode=None)
    return s


@contextlib.contextmanager
def live_process(**env_extra):
    """Настоящий процесс с заданным окружением — его /proc и читает api_base_url.

    Именно процесс, а не подмена os.environ: /proc/<pid>/environ — снимок на
    момент СТАРТА, и правка os.environ в него не попадает. Тест обязан ходить
    тем же путём, что и бой, иначе он проверяет не то.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
    env.update(env_extra)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=env)
    # Между fork и exec /proc/<pid>/environ пуст — ждём, пока окружение появится,
    # иначе тест гоняет гонку, а не проверку.
    environ = Path(f"/proc/{proc.pid}/environ")
    for _ in range(200):
        if environ.exists() and environ.read_bytes():
            break
        time.sleep(0.01)
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=5)


# ── адрес API: факт из процесса против намерения из конфига ──────────────────
def test_running_session_reads_live_process():
    """У запущенной сессии адрес берётся из окружения процесса, не из конфига."""
    core = make_core(claude_env={"ANTHROPIC_BASE_URL": PROXY})
    with live_process() as pid:                   # процесс поднят БЕЗ переменной
        session = make_session(running=True, pid=pid)
        url, live = core.manager.api_base_url(session)
        assert live is True, "должны были прочитать живой процесс"
        assert url is None, url                   # конфиг говорил про прокси — врал
        assert "прямой api.anthropic.com" in core.info_text(session)


def test_running_session_shows_variable_from_process():
    """Переменная в процессе — она и в паспорте."""
    core = make_core()                            # конфиг пуст…
    with live_process(ANTHROPIC_BASE_URL=PROXY) as pid:   # …а процесс с прокси
        session = make_session(running=True, pid=pid)
        url, live = core.manager.api_base_url(session)
        assert (url, live) == (PROXY, True)
        assert PROXY in core.info_text(session)


def test_stopped_session_falls_back_to_config_with_mark():
    """Остановленная сессия: считаем из конфига и честно помечаем это."""
    core = make_core(claude_env={"ANTHROPIC_BASE_URL": PROXY})
    text = core.info_text(make_session())
    assert PROXY in text
    assert "из конфига" in text


def test_profile_url_wins_in_passport():
    """Профиль с base_url = "" виден в паспорте как прямой адрес."""
    old = os.environ.get("CLAUDE_BOX_HOME")
    with tempfile.TemporaryDirectory(prefix="info-test-") as d:
        os.environ["CLAUDE_BOX_HOME"] = d
        try:
            profiles.ensure_profile("work")
            profiles.settings_path("work").write_text('base_url = ""\n', encoding="utf-8")
            core = make_core(claude_env={"ANTHROPIC_BASE_URL": PROXY})
            text = core.info_text(make_session(profile="work"))
            assert "прямой api.anthropic.com" in text
            assert PROXY not in text
            assert "work" in text
        finally:
            if old is None:
                os.environ.pop("CLAUDE_BOX_HOME", None)
            else:
                os.environ["CLAUDE_BOX_HOME"] = old


# ── остальные поля паспорта ──────────────────────────────────────────────────
def test_channel_state_visible():
    """Канал: заблокированный виден отдельной строкой, живой — тоже."""
    core = make_core()
    with live_process() as pid:
        assert "не загрузился" in core.info_text(
            make_session(running=True, blocked=True, pid=pid))
        assert "живой" in core.info_text(
            make_session(running=True, blocked=False, pid=pid))
    # Не запущена и не смотрели — не выдумываем.
    assert "не запущена" in core.info_text(make_session())


def test_engine_model_and_dir():
    """Движок, модель, effort, пермиссии и каталог — в тексте."""
    core = make_core()
    session = make_session(sandbox="off")
    session.model = "opus"
    text = core.info_text(session)
    for expected in ("off", "opus", "high", "auto", "/tmp/s"):
        assert expected in text, expected


def test_agentvm_does_not_lie_about_account():
    """Под agent-vm учётка в госте — хостовый каталог показывать нельзя."""
    core = make_core(sandbox="agent-vm")
    text = core.info_text(make_session())
    assert "microVM" in text
    assert ".claude-proxy" not in text


def test_plain_strips_markup():
    """plain() снимает разметку ядра — веб печатает нотисы экранированными."""
    core = make_core()
    text = core.plain(core.info_text(make_session()))
    assert "<b>" not in text and "<code>" not in text
    assert "&lt;" not in text and "&amp;" not in text


def main() -> None:
    test_running_session_reads_live_process()
    test_running_session_shows_variable_from_process()
    test_stopped_session_falls_back_to_config_with_mark()
    test_profile_url_wins_in_passport()
    test_channel_state_visible()
    test_engine_model_and_dir()
    test_agentvm_does_not_lie_about_account()
    test_plain_strips_markup()
    print("ALL INFO OK")


if __name__ == "__main__":
    main()
