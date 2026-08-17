"""Видимость лимита сессии и живого фона — регресс по инциденту ikar 2026-08-17.

Сессия ikar сутки выглядела мёртвой, хотя всё работало штатно: выжжено
5-часовое окно подписки (100%), Claude Code ретраил сам («Session limit reached
· Retrying in 39m (9pm) · attempt 1/15»), а в чат уходил только безымянный
«ретрай 1/15» в бабле. Одновременно `/bg` показывал пустоту, пока в сессии
сутки крутились девять петель `until grep …; do sleep …; done`, ждавших строку
в логе, который перестал расти накануне.

Проверяем:
  • баннер лимита распознаётся вместе с часом сброса — в т.ч. в изуродованном
    перерисовкой виде («Retrying n 1h», «atempt 1/15») из живого лога;
  • релей говорит о лимите один раз и повторяет, если час сброса сменился;
  • /cost больше не двоит строки моделей (экран в дельте лежит дважды);
  • живые процессы сессии видны с возрастом и без обвязки Bash-тула.

Запуск: .venv/bin/python tests/limit_bg_test.py
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import proctree  # noqa: E402
from orchestrator.core import turn as turnmod  # noqa: E402
from orchestrator.core.logsignals import detect_log_signals, find_session_limit  # noqa: E402
from orchestrator.core.reports import parse_cost  # noqa: E402
from orchestrator.core.turn import TurnSupervisor  # noqa: E402

# Дословно из claude.log ikar: TUI роняет символы при перерисовке.
REAL_BANNERS = [
    "Session limit reached · Retrying in 39m (9pm) · attempt 1/15",
    "Session limit reached· Retrying in 49m (9pm) · attempt 1/15",
    "Session limit reached ·Retrying n 1h (9pm) · atempt 1/15",
]


def test_session_limit_detection():
    for raw in REAL_BANNERS:
        assert find_session_limit(raw.encode()) == "9pm", raw
    # Часы в другом формате тоже забираем.
    assert find_session_limit(b"Session limit reached, retrying (21:00)") == "21:00"
    # Баннер есть, часов нет — всё равно сигнал (пустая строка, не None).
    assert find_session_limit(b"Session limit reached - retrying later") == ""
    # Проза и соседние баннеры лимитом не считаются.
    assert find_session_limit(b"limit reached for this file (9pm)") is None
    assert find_session_limit("Обсуждали лимит сессии, 5 часов".encode()) is None
    assert detect_log_signals(b"API Error: 429 rate limit")["session_limit"] is None
    print("OK лимит сессии: распознан в трёх живых вариантах, проза не ловится")


def _sup(session, sends):
    async def send(_s, text):
        sends.append(text)

    async def typing(_s):
        return False

    mgr = SimpleNamespace(
        get=lambda name: session,
        is_busy=lambda s: False,
        tail_log=lambda s, lines=15: "",
        read_last_model=lambda s: "opus",
        read_pollution_excerpt=lambda s, max_entries=25: None,
    )
    return TurnSupervisor(mgr, t=lambda k, **kw: f"{k}:{kw.get('reset', '')}",
                          send=send, typing=typing)


async def test_relay_surfaces_session_limit(tmp_path):
    """Лимит уходит в чат один раз, а при смене часа сброса — заново."""
    turnmod.WATCHDOG_GRACE = 0.01
    turnmod.ERROR_RELAY_INTERVAL = 0.01
    log = tmp_path / "claude.log"
    log.write_bytes(b"")
    session = SimpleNamespace(name="ikar", session_dir=tmp_path)
    sends: list = []
    sup = _sup(session, sends)
    task = asyncio.create_task(sup._error_relay_loop("ikar"))
    await asyncio.sleep(0.05)
    with open(log, "ab") as f:
        f.write(("\n" + REAL_BANNERS[0] + "\n").encode())
    for _ in range(200):
        if sends:
            break
        await asyncio.sleep(0.01)
    assert sends == ["session_limit:9pm"], sends

    # Тот же баннер тикает каждые несколько секунд — второй раз не пишем.
    with open(log, "ab") as f:
        f.write(("\n" + REAL_BANNERS[1] + "\n").encode())
    await asyncio.sleep(0.15)
    assert sends == ["session_limit:9pm"], sends

    # Новое окно (другой час сброса) — говорим снова.
    with open(log, "ab") as f:
        f.write(b"\nSession limit reached - Retrying in 12m (2am) - attempt 1/15\n")
    for _ in range(200):
        if len(sends) > 1:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    assert sends == ["session_limit:9pm", "session_limit:2am"], sends
    print("OK релей: лимит один раз, повтор только на новом окне сброса")


def test_parse_cost_dedupes_models():
    """Экран /cost в дельте лежит дважды — строки моделей не должны двоиться."""
    screen = (
        "Current session (5h)\n██ 100% used\n"
        "Current week (all models)\n██ 66% used\n"
        "Current week (Fable)\n█ 19% used\n"
        "Total cost: $1440.67\n"
    )
    data = parse_cost(screen * 2)  # ровно то, что приезжает из claude.log
    assert data["session_pct"] == "100" and data["week_pct"] == "66", data
    assert data["models"] == [("Fable", "19")], data["models"]
    assert data["cost"] == "1440.67", data
    print("OK /cost: строки моделей не двоятся при двойной перерисовке")


def test_short_cmd_unwraps_bash_tool_preamble():
    """Обвязка Bash-тула не должна прятать саму команду (живая строка ikar)."""
    raw = ("/bin/bash -c source /home/u/.claude/shell-snapshots/snapshot-bash-178.sh "
           "2>/dev/null || true && shopt -u extglob 2>/dev/null || true && "
           "eval 'L=/tmp/x/full2.log until grep -qE \"[0-9]+ (passed|failed)\" \"$L\" "
           "2>/dev/null; do sleep 60; done echo DONE' < /dev/null && pwd -P")
    short = proctree.short_cmd(raw)
    assert short.startswith("L=/tmp/x/full2.log until grep"), short
    assert "shell-snapshots" not in short, short
    # Команда без eval остаётся собой, длинная — обрезается с многоточием.
    assert proctree.short_cmd("pytest -q tests/") == "pytest -q tests/"
    assert proctree.short_cmd("x" * 300).endswith("…")
    print("OK короткая команда: eval развёрнут, преамбула снапшота убрана")


def test_list_descendants_sees_live_children():
    """Живые потомки видны с возрастом; долгожитель — первым."""
    old = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
    time.sleep(1.2)
    new = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
    try:
        time.sleep(0.3)
        rows = proctree.list_descendants(__import__("os").getpid())
        pids = [r[0] for r in rows]
        assert old.pid in pids and new.pid in pids, (pids, old.pid, new.pid)
        ages = {r[0]: r[1] for r in rows}
        assert ages[old.pid] > ages[new.pid], ages
        assert rows == sorted(rows, key=lambda r: r[1], reverse=True), rows
        assert any("sleep 30" in r[2] for r in rows), rows
        # Несуществующий pid — пустой список, а не исключение.
        assert proctree.list_descendants(999_999_99) == []
        # Свой channel-сервер в дереве есть всегда и фоном модели не является —
        # в списке ему не место (иначе /bg врёт «что-то крутится» на пустой сессии).
        ours = subprocess.Popen(
            ["/bin/sh", "-c", "sleep 20  # python orchestrator/channel_server.py"])
        time.sleep(0.3)
        try:
            assert ours.poll() is None, "процесс-маркер должен быть жив"
            listed = [r[0] for r in proctree.list_descendants(__import__("os").getpid())]
            assert ours.pid not in listed, listed
        finally:
            ours.kill()
            ours.wait()
    finally:
        old.kill(), new.kill()
        old.wait(), new.wait()
    print("OK живые процессы: возраст считается, долгожитель первым")


def _core():
    from orchestrator.core.app import OrchestratorCore
    from orchestrator.core.texts import get_texts

    core = OrchestratorCore.__new__(OrchestratorCore)
    core._texts = get_texts("ru")
    return core


def test_bg_text_is_honest_about_stale_snapshot():
    """Пустой снимок ≠ «в сессии ничего не крутится» — главный дефект ikar."""
    import os

    core = _core()
    child = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
    time.sleep(0.3)
    session = SimpleNamespace(
        name="ikar", title="ikar", running=True,
        process=SimpleNamespace(pid=os.getpid()),
        background_tasks=[], session_crons=[],
        bg_snapshot_at=time.time() - 3600,
    )
    try:
        text = core.bg_text(session)
        assert "Фоновых задач и кронов в сессии нет" in text, text
        assert "1 ч 0 мин" in text, text          # возраст снимка назван прямо
        assert "Живые процессы сессии" in text, text
        assert "sleep 30" in text, text            # то, что снимок не показал бы

        # Снимка не было вовсе — говорим и это.
        session.bg_snapshot_at = None
        assert "Снимка ещё не было" in core.bg_text(session)

        # Свежий снимок — лишней пометки нет, живые процессы всё равно видны.
        session.bg_snapshot_at = time.time()
        fresh = core.bg_text(session)
        assert "Снимку" not in fresh and "Живые процессы сессии" in fresh, fresh

        # Остановленная сессия: живых процессов нет, падать не на чем.
        session.running = False
        assert "Живые процессы" not in core.bg_text(session)
    finally:
        child.kill()
        child.wait()
    print("OK /bg: возраст снимка назван, живые процессы показаны")


def main():
    import tempfile

    test_session_limit_detection()
    test_parse_cost_dedupes_models()
    test_short_cmd_unwraps_bash_tool_preamble()
    test_list_descendants_sees_live_children()
    test_bg_text_is_honest_about_stale_snapshot()
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(test_relay_surfaces_session_limit(Path(d)))
    print("ALL LIMIT-BG OK")


if __name__ == "__main__":
    main()
