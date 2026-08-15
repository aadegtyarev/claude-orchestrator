"""Канал сессии не загрузился (орг-политика учётки): видимость + фолбэк.

Живой случай 14-15.08.2026 (сессия ikar после `/profile work`): учётка профиля
принадлежит Team-оргу, где dev-каналы выключены. Claude Code рисует «Channels
are not enabled for your org», канал НЕ грузится — но channel-сервер стартует
как обычный MCP (он подключается --mcp-config, а не гейтом каналов). Поэтому
/ping отвечает 200, сессия числится готовой, `POST /notify` возвращает 200 —
и сообщение растворяется. Оператор видел «сессия работает» и полное молчание.

Проверяем контракт:
  • детектор состояния канала по claude.log (баннеры со сломанными пробелами —
    TUI рвёт строку; берём ПОСЛЕДНИЙ баннер, лог накопительный);
  • probe_channel читает только вывод ТЕКУЩЕГО процесса (от log_offset);
  • send_to_claude при заблокированном канале НЕ ходит по HTTP, а печатает в
    PTY, в том же конверте <channel …>, что даёт push (иначе модель ответит с
    чужим context_id);
  • многострочный текст уходит в bracketed paste, Enter — отдельной записью;
  • нормальный канал ничего не меняет: как раньше, HTTP.

Запуск: .venv/bin/python tests/channel_blocked_test.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import sessions  # noqa: E402
from orchestrator.core.channelstate import (  # noqa: E402
    BLOCKED,
    OK,
    UNKNOWN,
    channel_state,
)
from orchestrator.core.sessions import SessionManager  # noqa: E402

# Куски настоящего claude.log (ikar). Пробелы внутри баннера TUI ставит как
# придётся — детектор обязан их игнорировать.
BANNER_OK = (
    b"\x1b[38;5;246mgh auth login for PR status\x1b[39m"
    b"\x1b[38;5;220m\xe2\x96\x8eChannels (experimental) messages from "
    b"server:channel-ikar inject directly in this session \xc2\xb7 restart "
    b"without \xe2\x96\x8e--dangerously-load-development-channels to stop\x1b[39m"
)
BANNER_BLOCKED = (
    b"\x1b[38;5;246mgh auth login for PR status \xc2\xb7 \xe2\x86\x90 1 agent\x1b[39m"
    b"\x1b[40;1H\x1b[38;3H\x1b[?25h\x1b[?25l\x1b[H\r\x1b[12C\x1b[35B"
    b"\x1b[38;5;220mChannels are not enabled for your org \xc2\xb7 have an "
    b"administrator set channelsEnabled: true in managed settings\x1b[39m"
)
# Тот же баннер, но разорванный переносом строки посреди слова — так он и
# приходит, когда TUI перерисовывает статус в узком окне.
BANNER_BLOCKED_WRAPPED = (
    b"Channels are not enabled for your\r\n org \xc2\xb7 have an administrator "
    b"set channelsEnabled:\r\n true in managed settings"
)


def test_channel_state():
    assert channel_state(BANNER_OK) == OK
    assert channel_state(BANNER_BLOCKED) == BLOCKED
    assert channel_state(BANNER_BLOCKED_WRAPPED) == BLOCKED
    assert channel_state(b"just some output, no banners at all") == UNKNOWN
    # Лог накопительный: побеждает ПОСЛЕДНИЙ баннер, а не первый.
    assert channel_state(BANNER_OK + b"...restart..." + BANNER_BLOCKED) == BLOCKED
    assert channel_state(BANNER_BLOCKED + b"...restart..." + BANNER_OK) == OK
    print("OK детектор: живой/заблокированный канал, перенос строки, последний баннер")


def _session(tmp_path: Path, **kw):
    log = tmp_path / "claude.log"
    session = SimpleNamespace(
        name="ikar",
        port=33941,
        session_dir=tmp_path,
        channel_blocked=None,
        log_offset=0,
        last_activity=0.0,
        running=True,
        pty_master=None,
    )
    for key, value in kw.items():
        setattr(session, key, value)
    return session, log


def test_probe_reads_only_current_process(tmp_path: Path):
    """Баннеры прошлого запуска не должны становиться вердиктом нынешнего."""
    mgr = SessionManager.__new__(SessionManager)
    session, log = _session(tmp_path)

    # Прошлый процесс работал с живым каналом, нынешний — уже без.
    log.write_bytes(BANNER_OK)
    session.log_offset = log.stat().st_size
    log.write_bytes(BANNER_OK + BANNER_BLOCKED)
    assert mgr.probe_channel(session) is True
    assert session.channel_blocked is True

    # И наоборот: прошлый упирался в блок, нынешний поднял канал.
    session2, log2 = _session(tmp_path)
    log2.write_bytes(BANNER_BLOCKED)
    session2.log_offset = log2.stat().st_size
    with open(log2, "ab") as fh:
        fh.write(BANNER_OK)
    assert mgr.probe_channel(session2) is False
    assert session2.channel_blocked is False
    print("OK probe_channel: смотрит только вывод текущего процесса (log_offset)")


def test_probe_unknown_keeps_verdict(tmp_path: Path):
    """Баннера ещё нет — прежний вердикт не трогаем (неизвестность не лечим)."""
    mgr = SessionManager.__new__(SessionManager)
    session, log = _session(tmp_path, channel_blocked=True)
    log.write_bytes(b"no banner here yet")
    assert mgr.probe_channel(session) is True, "вердикт не должен сбрасываться"

    session.channel_blocked = None
    assert mgr.probe_channel(session) is None
    print("OK probe_channel: без баннера прежний вердикт сохраняется")

    # Лога вовсе нет (сессия только создана) — не падаем.
    session2, _ = _session(tmp_path / "nope")
    assert mgr.probe_channel(session2) is None
    print("OK probe_channel: отсутствующий лог не роняет пробу")


class _FakeHttp:
    def __init__(self):
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        raise AssertionError("при заблокированном канале HTTP ходить нельзя")


def _mgr_with_pty(http, writes: list[bytes]):
    mgr = SessionManager.__new__(SessionManager)
    mgr._http = http
    mgr._http_session = lambda: http
    mgr._channel_headers = lambda: {}
    mgr._send_raw = lambda session, data: writes.append(data)
    return mgr


async def test_blocked_delivers_via_pty(tmp_path: Path):
    """Канал заблокирован → печатаем в PTY в конверте канала, HTTP не трогаем."""
    sessions.PASTE_ENTER_DELAY = 0.0  # тест не должен ждать
    writes: list[bytes] = []
    http = _FakeHttp()
    mgr = _mgr_with_pty(http, writes)
    session, _ = _session(tmp_path, channel_blocked=True)

    await mgr.send_to_claude(session, "первая строка\nвторая строка", "telegram:x:1:2:3")

    assert http.calls == 0, "HTTP-путь обязан быть пропущен"
    assert len(writes) == 2, f"ожидались вставка и Enter, было {len(writes)}"
    payload, enter = writes
    assert enter == b"\r", enter
    assert payload.startswith(b"\x1b[200~") and payload.endswith(b"\x1b[201~"), payload
    text = payload.decode()
    assert '<channel source="channel-ikar" context_id="telegram:x:1:2:3">' in text, text
    assert "первая строка\nвторая строка</channel>" in text, text
    print("OK фолбэк: bracketed paste + отдельный Enter, конверт канала на месте")


class _Resp:
    def raise_for_status(self):
        return None


class _CM:
    async def __aenter__(self):
        return _Resp()

    async def __aexit__(self, *a):
        return False


class _OkHttp:
    def __init__(self):
        self.calls = 0

    def post(self, *a, **k):
        self.calls += 1
        return _CM()


async def test_healthy_channel_uses_http(tmp_path: Path):
    """Живой канал — прежний путь: HTTP push, в PTY ничего не пишем."""
    writes: list[bytes] = []
    http = _OkHttp()
    mgr = _mgr_with_pty(http, writes)
    session, _ = _session(tmp_path, channel_blocked=False)

    await mgr.send_to_claude(session, "привет", "ctx")

    assert http.calls == 1, http.calls
    assert writes == [], f"в PTY писать не должны: {writes}"
    print("OK живой канал: доставка как прежде, по HTTP")


async def test_unknown_probes_before_sending(tmp_path: Path):
    """Вердикта ещё нет → доспрашиваем лог прямо перед отправкой."""
    sessions.PASTE_ENTER_DELAY = 0.0
    writes: list[bytes] = []
    http = _FakeHttp()
    mgr = _mgr_with_pty(http, writes)
    reported: list[str] = []
    mgr._report_channel_blocked = lambda s: reported.append(s.name) or asyncio.sleep(0)
    session, log = _session(tmp_path)
    log.write_bytes(BANNER_BLOCKED)

    await mgr.send_to_claude(session, "привет", "ctx")

    assert session.channel_blocked is True
    assert reported == ["ikar"], f"оператору не сказали: {reported}"
    assert len(writes) == 2, writes
    print("OK неизвестное состояние: проба перед отправкой + заметка оператору")


def main():
    import tempfile

    test_channel_state()
    with tempfile.TemporaryDirectory() as raw:
        tmp_path = Path(raw)
        test_probe_reads_only_current_process(tmp_path)
        test_probe_unknown_keeps_verdict(tmp_path)
        asyncio.run(test_blocked_delivers_via_pty(tmp_path))
        asyncio.run(test_healthy_channel_uses_http(tmp_path))
        asyncio.run(test_unknown_probes_before_sending(tmp_path))
    print("ALL CHANNEL-BLOCKED OK")


if __name__ == "__main__":
    main()
