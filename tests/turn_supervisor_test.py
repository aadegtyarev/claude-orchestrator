"""Регресс: жизненный цикл фоновых задач хода (TurnSupervisor).

Раньше в тестах гонялись только note_tool/pop_reply_flag (Stop-гейт). Здесь —
сами циклы: _guarded (падение задачи логируется, CancelledError пробрасывается),
start/stop/forget (реестры задач), watchdog (срабатывает на зависании, молчит
пока сессия жива) и error-relay (транслирует API-ошибку один раз, дедуп душит
повтор). Интервалы уменьшены, ожидание — через Event с таймаутом (без флейка).

Запуск: .venv/bin/python tests/turn_supervisor_test.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import turn as turnmod  # noqa: E402
from orchestrator.core.turn import TurnSupervisor  # noqa: E402


def _fast_intervals():
    turnmod.WATCHDOG_GRACE = 0.01
    turnmod.WATCHDOG_CHECK = 0.01
    turnmod.STALL_CHECKS = 2
    turnmod.ERROR_RELAY_INTERVAL = 0.01
    # COOLDOWN/REPEAT НЕ трогаем — на них держится дедуп, который проверяем.


class _Mgr:
    def __init__(self, session, busy=False, tail=""):
        self._session = session
        self._busy = busy
        self._tail = tail

    def get(self, name):
        return self._session

    def is_busy(self, session):
        return self._busy

    def tail_log(self, session, lines=15):
        return self._tail

    def read_last_model(self, session):
        return "opus"

    def read_pollution_excerpt(self, session, max_entries=25):
        return None


def _sup(mgr, sends, tool_inflight=None, auth_expired=None):
    async def send(session, text):
        sends.append(text)

    async def typing(session):
        return False  # typing-цикл гаснет сразу, он не под тестом

    return TurnSupervisor(
        mgr, t=lambda k, **kw: k, send=send, typing=typing, tool_inflight=tool_inflight,
        auth_expired=auth_expired,
    )


async def _wait(cond, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return False


# ── _guarded ────────────────────────────────────────────────────

async def test_guarded_swallows_and_reraises_cancel():
    async def boom():
        raise ValueError("oops")

    # Обычное исключение — проглочено (иначе «Task exception never retrieved»).
    await TurnSupervisor._guarded(boom(), "x")

    async def cancelled():
        raise asyncio.CancelledError()

    try:
        await TurnSupervisor._guarded(cancelled(), "x")
        assert False, "CancelledError должен пробрасываться"
    except asyncio.CancelledError:
        pass
    print("OK _guarded: обычное исключение проглочено, CancelledError проброшен")


# ── start / stop / forget ───────────────────────────────────────

async def test_lifecycle_registers_and_clears():
    mgr = _Mgr(session=None)  # get→None: все циклы выходят сразу, без сайд-эффектов
    sup = _sup(mgr, [])
    sup.start("s")
    assert "s" in sup._typing and "s" in sup._watchdogs and "s" in sup._error_relays
    print("OK start: заведены три задачи хода")

    await asyncio.sleep(0)  # дать задачам стартовать (get→None → выходят сами)
    sup.stop("s")
    assert "s" not in sup._typing and "s" not in sup._watchdogs
    assert "s" not in sup._error_relays
    print("OK stop: реестры очищены (задачи отменены)")

    sup._last_action_was_reply["s"] = True
    sup.forget("s")
    assert "s" not in sup._last_action_was_reply
    print("OK forget: Stop-флаг сброшен")
    await asyncio.sleep(0)  # дать отменённым задачам финализироваться


# ── watchdog ────────────────────────────────────────────────────

async def test_watchdog_fires_on_stall(tmp_path):
    _fast_intervals()
    log = tmp_path / "claude.log"
    log.write_bytes(b"start")  # не растёт дальше → зависание
    session = SimpleNamespace(name="s", session_dir=tmp_path)
    sends: list = []
    sup = _sup(_Mgr(session, busy=False), sends)
    task = asyncio.create_task(sup._watchdog_loop("s"))
    fired = await _wait(lambda: sends)
    task.cancel()
    assert fired and sends == ["stalled"], sends
    print("OK watchdog: лог не растёт + не busy → «stalled» один раз")


async def test_watchdog_silent_while_busy(tmp_path):
    _fast_intervals()
    log = tmp_path / "claude.log"
    log.write_bytes(b"start")
    session = SimpleNamespace(name="s", session_dir=tmp_path)
    sends: list = []
    sup = _sup(_Mgr(session, busy=True), sends)  # is_busy=True → живой
    task = asyncio.create_task(sup._watchdog_loop("s"))
    await asyncio.sleep(0.2)  # несколько проверок
    task.cancel()
    assert sends == [], sends
    print("OK watchdog: сессия busy → не срабатывает")


async def test_watchdog_silent_while_tool_inflight(tmp_path):
    """Лог не растёт И CPU не растёт (is_busy=False), НО идёт foreground-тул
    (Bash `sleep`/сетевое ожидание) → живой, вотчдог молчит. Это ровно кейс,
    который под bwrap раньше держал has_kids, а теперь — сигнал хуков."""
    _fast_intervals()
    log = tmp_path / "claude.log"
    log.write_bytes(b"start")  # не растёт
    session = SimpleNamespace(name="s", session_dir=tmp_path)
    sends: list = []
    sup = _sup(_Mgr(session, busy=False), sends, tool_inflight=lambda name: True)
    task = asyncio.create_task(sup._watchdog_loop("s"))
    await asyncio.sleep(0.2)  # несколько проверок
    task.cancel()
    assert sends == [], sends
    print("OK watchdog: тул in-flight (CPU замер) → не срабатывает")


# ── error-relay ─────────────────────────────────────────────────

async def test_error_relay_surfaces_api_error_once(tmp_path):
    _fast_intervals()
    log = tmp_path / "claude.log"
    log.write_bytes(b"")  # пусто → offset=0, всё дальнейшее «наше»
    session = SimpleNamespace(name="s", session_dir=tmp_path)
    sends: list = []
    sup = _sup(_Mgr(session), sends)
    task = asyncio.create_task(sup._error_relay_loop("s"))
    await asyncio.sleep(0.05)  # пройти WATCHDOG_GRACE, зафиксировать offset
    with open(log, "ab") as f:
        f.write(b"\nAPI Error: 429 rate-limit exceeded\n")
    fired = await _wait(lambda: sends)
    assert fired and sends == ["api_error_ratelimit"], sends
    print("OK error-relay: баннер «API Error: 429» → api_error_ratelimit")

    # Дедуп: та же ошибка снова в пределах COOLDOWN → повторно НЕ шлём.
    with open(log, "ab") as f:
        f.write(b"\nAPI Error: 429 rate-limit exceeded\n")
    await asyncio.sleep(0.15)
    task.cancel()
    assert sends == ["api_error_ratelimit"], sends
    print("OK error-relay: повтор той же ошибки в COOLDOWN задушен (дедуп)")


async def test_error_relay_surfaces_expired_login(tmp_path):
    """Баннер протухшей учётки + подтверждение `auth status` → заметка в топик.

    Ровно тот отказ, из-за которого сессия выглядит мёртвой: claude отвечает
    сам за 0 с, модель сообщений не видит, а в чат раньше не приходило ничего.
    """
    _fast_intervals()
    log = tmp_path / "claude.log"
    log.write_bytes(b"")
    session = SimpleNamespace(name="s", session_dir=tmp_path)
    sends: list = []
    asked: list = []

    async def expired(sess):
        asked.append(sess.name)
        return True

    sup = _sup(_Mgr(session), sends, auth_expired=expired)
    task = asyncio.create_task(sup._error_relay_loop("s"))
    await asyncio.sleep(0.05)
    with open(log, "ab") as f:
        # Дословно из лога noos: TUI рисует баннер позиционированием курсора,
        # пробелы между словами теряются.
        f.write("\n●Loginexpired·Pleaserun/login✻Churned for 0s\n".encode())
    fired = await _wait(lambda: sends)
    task.cancel()
    assert fired and sends == ["login_expired"], sends
    assert asked == ["s"], asked
    print("OK error-relay: баннер учётки + подтверждение → login_expired")


async def test_error_relay_login_banner_without_confirmation_is_silent(tmp_path):
    """Тот же баннер, но `auth status` говорит «залогинен» → молчим.

    Строку «Login expired · Please run /login» модель может процитировать в
    ответе (например обсуждая эту самую фичу). Тревога «вы разлогинены» на
    рабочей учётке дороже задержки — поэтому баннер только повод спросить.
    """
    _fast_intervals()
    log = tmp_path / "claude.log"
    log.write_bytes(b"")
    session = SimpleNamespace(name="s", session_dir=tmp_path)
    sends: list = []

    async def not_expired(sess):
        return False

    sup = _sup(_Mgr(session), sends, auth_expired=not_expired)
    task = asyncio.create_task(sup._error_relay_loop("s"))
    await asyncio.sleep(0.05)
    with open(log, "ab") as f:
        f.write("\nВ ответе модели: «Login expired · Please run /login»\n".encode())
    await asyncio.sleep(0.15)
    task.cancel()
    assert sends == [], sends
    print("OK error-relay: без подтверждения `auth status` — ни слова")


def main():
    import tempfile

    asyncio.run(test_guarded_swallows_and_reraises_cancel())
    asyncio.run(test_lifecycle_registers_and_clears())
    for fn in (
        test_watchdog_fires_on_stall,
        test_watchdog_silent_while_busy,
        test_watchdog_silent_while_tool_inflight,
        test_error_relay_surfaces_api_error_once,
        test_error_relay_surfaces_expired_login,
        test_error_relay_login_banner_without_confirmation_is_silent,
    ):
        d = Path(tempfile.mkdtemp())
        asyncio.run(fn(d))
    print("ALL TURN-SUPERVISOR OK")


if __name__ == "__main__":
    main()
