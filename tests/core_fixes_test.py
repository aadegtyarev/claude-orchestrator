"""Регрессы корректностных фиксов ревью в OrchestratorCore.

- точное имя канального тула (endswith) vs подстрока — Stop-фолбэк не глушится
  чужим MCP-тулом;
- _pending_perms/_local_perms чистятся при close/clear/delete;
- request_confirmation на таймауте гасит кнопки (permission_resolved);
- create_session откатывает сессию, если requires_binding-адаптер не привязал;
- bubble._flush фиксирует sent_text только при реальной доставке.

Запуск: .venv/bin/python tests/core_fixes_test.py
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import toolactivity  # noqa: E402
from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.history import HistoryLog  # noqa: E402
from orchestrator.core.permission import PermissionRelay  # noqa: E402
from orchestrator.core.subagentnaming import SubagentNaming  # noqa: E402
from orchestrator.core.toolactivity import ToolActivity  # noqa: E402
from orchestrator.core.bubble import BubbleManager  # noqa: E402
from orchestrator.core.texts import get_texts  # noqa: E402
from orchestrator.core.turn import TurnSupervisor  # noqa: E402

SESSION = SimpleNamespace(name="noos", title="noos", bindings={})


class FakeMgr:
    def __init__(self):
        self.perm_calls = []

    def get(self, name):
        return SESSION if name == "noos" else None

    get_by_name = get

    def check_resources(self, sandbox=None):
        """Ресурсный preflight создания сессии: этим тестам он не мешает."""

    async def send_permission(self, session, request_id, behavior):
        self.perm_calls.append((request_id, behavior))


def make_core():
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.manager = FakeMgr()
    core._texts = get_texts("ru")
    core.config = SimpleNamespace(max_instances=5)
    core.journal = HistoryLog(Path("unused-in-tests.json"))
    core.tools = ToolActivity()
    core.naming = SubagentNaming()
    core.adapters = {}
    core.perms = PermissionRelay(
        core.manager, core.t, core._each_transport, core._record
    )
    core.turns = TurnSupervisor(
        core.manager, core.t,
        lambda session, text: asyncio.sleep(0),
        lambda session: asyncio.sleep(0),
    )
    core.bubbles = SimpleNamespace(
        append=lambda *a, **kw: asyncio.sleep(0),
        close=lambda name: asyncio.sleep(0),
    )
    return core


async def test_exact_tool_name():
    core = make_core()
    deliveries = []

    async def fake_deliver(session, text, origin, intermediate):
        deliveries.append(text)
    core._deliver_text = fake_deliver

    # Чужой MCP-тул с подстрокой reply_to_user НЕ должен ставить reply-флаг.
    await core.handle_tool_event("noos", {"tool_name": "mcp__notes__draft_reply_to_user_email"})
    await core.handle_stop_event("noos", {"last_assistant_message": "финал хода"})
    assert deliveries == ["финал хода"], deliveries
    print("OK точное имя тула: чужой *_reply_to_user_* не глушит Stop-фолбэк")

    deliveries.clear()
    # Настоящий канальный тул — ставит флаг, фолбэк не срабатывает.
    await core.handle_tool_event("noos", {"tool_name": "mcp__channel-noos__reply_to_user"})
    await core.handle_stop_event("noos", {"last_assistant_message": "уже отправлено"})
    assert deliveries == [], deliveries
    print("OK настоящий reply_to_user ставит флаг (без дубля)")


async def test_taskoutput_and_bg_available():
    core = make_core()
    lines = []

    async def fake_append(name, html, **kw):
        lines.append(html)
    core.bubbles = SimpleNamespace(
        append=fake_append, close=lambda n: asyncio.sleep(0),
        complete=lambda *a, **kw: asyncio.sleep(0),
    )

    # Кнопка ⏭ теперь на in-flight тулах (хуки PreToolUse/PostToolUse), НЕ на
    # /proc: под bwrap у процесса сессии всегда есть дочерний → has_children там
    # вечно True. Bash стартовал (Pre без Post) → можно свернуть в фон (Ctrl+B).
    await core.handle_tool_event("noos", {"tool_name": "Bash", "tool_use_id": "t1",
                                          "tool_input": {"command": "make build"}})
    assert core._unblock_available("noos") is True
    assert core.unblock_action("noos") == "background"
    # Bash завершился (PostToolUse) → в grace-окне кнопка ещё держится (дебаунс
    # против мигания на паузах между тулами).
    await core.handle_tool_event("noos", {"hook_event_name": "PostToolUse",
                                          "tool_name": "Bash", "tool_use_id": "t1",
                                          "tool_response": {}})
    assert core._unblock_available("noos") is True, "grace: сразу после Post ещё активна"
    # Grace истёк (нет тулов дольше окна) → ⏭ гаснет.
    core.tools._cleared_at["noos"] = time.monotonic() - toolactivity.UNBLOCK_GRACE - 1
    assert core._unblock_available("noos") is False, "grace истёк — ⏭ гаснет"
    # TaskOutput → «ждёт фон»: строка в бабле + ⏭ активна (пнуть Esc'ом).
    await core.handle_tool_event("noos", {"tool_name": "TaskOutput", "tool_input": {}})
    assert any("Ждёт фоновую задачу" in ln for ln in lines), lines
    assert core._unblock_available("noos") is True, "ожидание фона можно прервать"
    assert core.unblock_action("noos") == "kick"
    # Простой (нет тула в работе, не ждёт фон) → ⏭ неактивна.
    core.tools.forget("noos")
    core.tools.note_tool("noos", "Read")
    assert core._unblock_available("noos") is False
    print("OK ⏭ разблокировка: Bash-inflight→background, Post→гаснет, TaskOutput→kick, покой→неактивна")


async def test_pending_perms_cleanup():
    core = make_core()
    resolved = []

    class FakeTr:
        name = "tg"

        async def permission_prompt(self, session, request):
            pass

        async def permission_resolved(self, session, rid, behavior, via):
            resolved.append((rid, behavior, via))

    core.adapters = {"tg": FakeTr()}
    await core.handle_permission_request("noos", {"request_id": "r1", "tool": "Bash"})
    assert ("noos", "r1") in core.perms._pending
    # close чистит — старая кнопка потом не пройдёт гейт + гасится в адаптере.
    await core.perms.forget(SESSION)
    assert ("noos", "r1") not in core.perms._pending
    assert ("r1", "deny", "cancelled") in resolved  # кнопка погашена
    handled = await core.permission_verdict(SESSION, "r1", "allow", via="tg")
    assert handled is False and core.manager.perm_calls == []
    print("OK _pending_perms чистится: кнопка гасится и не бьёт по мёртвому запросу")


async def test_confirmation_timeout_clears_buttons():
    core = make_core()
    resolved = []

    class FakeTr:
        name = "tg"

        async def permission_prompt(self, session, request):
            pass

        async def permission_resolved(self, session, rid, behavior, via):
            resolved.append((rid, behavior, via))

    core.adapters = {"tg": FakeTr()}
    ok = await core.request_confirmation(SESSION, "wallet", "deploy", "gh pr", timeout=0.05)
    assert ok is False
    assert resolved and resolved[-1][1] == "deny" and resolved[-1][2] == "timeout"
    print("OK request_confirmation таймаут → deny + гашение кнопок (permission_resolved)")


async def test_create_rollback_on_bind_fail():
    core = make_core()
    created = {"n": 0}
    deleted = []

    class Mgr(FakeMgr):
        def has_name(self, n):
            return False

        def count(self):
            return 0

        async def create(self, title, project_path=None, sandbox=None):
            created["n"] += 1
            return SimpleNamespace(name="x", title=title, bindings={})

        async def delete(self, session):
            deleted.append(session.name)

        def save_state(self):
            pass

    core.manager = Mgr()

    class BadTelegram:
        name = "telegram"
        requires_binding = True

        async def bind_session(self, session):
            return None  # не смог создать топик

        async def unbind_session(self, session, address):
            pass

    core.adapters = {"telegram": BadTelegram()}
    core.session_hooks = []
    try:
        await core.create_session("proj")
        assert False, "должно было откатиться"
    except Exception as e:
        assert "телеграм" in str(e).lower() or "telegram" in str(e).lower() or "adapter" in str(e).lower() \
            or "поверхность" in str(e).lower() or "surface" in str(e).lower(), str(e)
    assert deleted == ["x"], deleted
    print("OK create_session откатывает сессию при провале обязательной привязки")


async def test_bubble_sent_text_only_on_delivery():
    delivered = {"ok": False}

    class FlakyTr:
        name = "tg"

        async def bubble_post(self, session, html, *, stop_button, unblock_active=False):
            if not delivered["ok"]:
                raise RuntimeError("429")  # первый раз сбой
            return "1"

        async def bubble_edit(self, session, ref, html, *, stop_button, unblock_active=False):
            pass

    tr = FlakyTr()
    bm = BubbleManager(lambda: [tr], lambda n: SESSION if n == "noos" else None,
                       lambda k, **kw: {"bubble_working": "Работаю"}.get(k, k), delete_after=True)
    bm.open("noos")
    await bm.append("noos", "🔧 tool")
    await asyncio.wait_for(bm._bubbles["noos"].flush_task, timeout=5)
    # Доставка упала → sent_text НЕ зафиксирован, бабл не «залип».
    assert bm._bubbles["noos"].sent_text == "", bm._bubbles["noos"].sent_text
    # Следующий flush при живой доставке должен пройти (text != sent_text).
    delivered["ok"] = True
    await bm.append("noos", "🔧 tool2")
    await asyncio.wait_for(bm._bubbles["noos"].flush_task, timeout=5)
    assert bm._bubbles["noos"].refs.get("tg") == "1"
    print("OK bubble sent_text фиксируется только при реальной доставке")


async def test_teardown_runtime_unified():
    core = make_core()
    events = []
    core.turns = SimpleNamespace(
        stop=lambda n: events.append(("stop", n)),
        forget=lambda n: events.append(("forget", n)),
    )

    async def fake_drop(s):
        events.append(("drop", s.name))
    core.perms = SimpleNamespace(forget=fake_drop)
    core.bash = SimpleNamespace(
        close_for_session=lambda n: events.append(("bash", n))
    )

    async def fake_bclose(n):
        events.append(("bubble", n))
    core.bubbles = SimpleNamespace(close=fake_bclose)

    # Терминальная остановка: stop + drop + сброс ВСЕГО hook-состояния хода + bash
    # + бабл. Пришпиливаем контракт, который скоро съест HookTracker.forget():
    # teardown обязан забыть tool-activity (last_tool/inflight/grace — иначе кнопка
    # ⏭ и вотчдог унесли бы состояние прошлого хода в следующий) И именование
    # сабагентов (agent_types/spawns — иначе имя чужого сабагента прилипнет к
    # следующему). Arrange пишет в поля напрямую; при выносе состояния в HookTracker
    # эти строки переедут на hooks.* — assert'ы поведенческие/по наблюдаемому.
    # ВАЖНО: grace-хвост ставим свежим (start→finish даёт _cleared_at=now), а не
    # протухшим: иначе foreground_active вернул бы False и без очистки — тест был
    # бы ложно-зелёным на регресс grace-очистки в forget().
    core.tools.note_tool("noos", "TaskOutput")  # → был бы "kick"
    core.tools.start("noos", "t1")
    core.tools.finish("noos", "t1")             # inflight пуст, _cleared_at=now (grace)
    core.naming.note_child("noos", "a1", "dev-planner")
    core.naming.note_spawn("noos", "dev-builder")
    await core._teardown_runtime(SESSION)
    assert ("stop", "noos") in events and ("drop", "noos") in events
    assert core.unblock_action("noos") is None, \
        "teardown не сбросил tool-activity (last_tool/inflight/grace)"
    # naming.pop → "" значит и _types, и _spawns очищены (иначе фолбэк вернул бы
    # 'dev-builder' — имя чужого сабагента протекло бы в следующий ход).
    assert core.naming.pop("noos", "a1") == "", "teardown не сбросил именование сабагентов"
    assert ("bash", "noos") in events and ("bubble", "noos") in events

    # Продолжение (clear/switch): forget_turn + НЕ трогаем bash.
    events.clear()
    core.tools.note_tool("noos", "Read")
    core.tools.start("noos", "t2")  # in-flight → был бы "background"
    await core._teardown_runtime(SESSION, close_bash=False, forget_turn=True)
    assert ("forget", "noos") in events
    assert not any(e[0] == "bash" for e in events), events
    assert core.unblock_action("noos") is None
    print("OK _teardown_runtime: единый разбор; close_bash/forget_turn ветвятся")


async def test_notify_state_changed_broadcasts():
    core = make_core()
    seen = []

    class Tr:
        name = "web"

        async def session_state_changed(self, s):
            seen.append(s.name if s else None)

    class Bad:
        name = "boom"

        async def session_state_changed(self, s):
            raise RuntimeError("adapter down")

    core.adapters = {"web": Tr(), "boom": Bad()}
    # Сбой одного адаптера не ломает операцию, остальные получают событие.
    await core._notify_state_changed(SESSION)
    assert seen == ["noos"], seen
    print("OK _notify_state_changed: бродкаст всем, сбой адаптера не ломает")


def test_record_delegates_to_journal():
    """core._record (API для wallet/PermissionRelay) и core.history проходят
    через HistoryLog — сквозной smoke делегации."""
    core = make_core()
    core._record(SESSION, "reply", text="привет")
    core._record(SESSION, "status", status="stopped")
    ev = core.history("noos")
    assert [e["kind"] for e in ev] == ["reply", "status"]
    assert ev[0]["text"] == "привет" and core.history("ghost") == []
    print("OK _record/history делегируют в journal (сквозь HistoryLog)")


async def main():
    test_record_delegates_to_journal()
    await test_teardown_runtime_unified()
    await test_notify_state_changed_broadcasts()
    await test_exact_tool_name()
    await test_taskoutput_and_bg_available()
    await test_pending_perms_cleanup()
    await test_confirmation_timeout_clears_buttons()
    await test_create_rollback_on_bind_fail()
    await test_bubble_sent_text_only_on_delivery()
    print("ALL CORE-FIXES OK")


if __name__ == "__main__":
    asyncio.run(main())
