"""Ядро PTY-запуска (box.pty): open_pty ставит размер терминала, драйвер
дренирует вывод процесса, отвечает на стартовые диалоги и на выходе закрывает
master-fd, не подвисая.

Проверяем на РЕАЛЬНОМ процессе под PTY (`cat` — эхо stdin→stdout):
  • open_pty задаёт winsize (иначе Claude Code зондирует размер и мусорит);
  • драйвер отдаёт вывод процесса через колбэк on_output;
  • увидев маркер стартового диалога, драйвер пишет клавиши-ответ в PTY
    (процесс их эхом возвращает — значит ушли в stdin);
  • многобайтовый ответ с ESC-последовательностью уходит ОДНОЙ записью
    (побайтовая запись разрывала ↓ на голый ESC → «Esc to cancel» в
    trust-диалоге → выход claude, инцидент 2026-09-18, сессия ad-coder);
  • ответ уходит только после тишины в выводе: клавиши в середину рендера
    диалога обрабатывались криво (тот же инцидент — диалог крутился
    ❯No→❯Yes→❯No без подтверждения);
  • на завершении процесса драйвер выходит и закрывает master (join с таймаутом
    — стоп не виснет).

Ядро переехало в автономный пакет box/ (box.pty, Слой 2 редизайна) — импортим из
источника; отдельным тестом проверяем реэкспорт open_pty/start_driver в
sessions.py (обратная совместимость).

Запуск: .venv/bin/python tests/box_pty_test.py
"""
import fcntl
import os
import select
import shutil
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box.pty import (  # noqa: E402
    TERM_COLS,
    TERM_ROWS,
    open_pty,
    pty_driver,
    start_driver,
)


class _FakeAnswerer:
    """Мини-двойник _DialogAnswerer: на маркер PROMPT один раз отвечает OK."""

    def __init__(self) -> None:
        self._buf = b""
        self._answered = False
        self.active = True

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf += chunk
        if not self._answered and b"PROMPT" in self._buf:
            self._answered = True
            self.active = False
            return [b"OK\n"]
        return []


def _spawn_cat(slave: int) -> subprocess.Popen:
    """Поднять `cat` (эхо stdin→stdout) на slave-конце PTY — как оркестратор
    спавнит claude, только процесс тривиальный и синхронный."""
    cat = shutil.which("cat") or "/bin/cat"
    proc = subprocess.Popen(
        [cat],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)  # slave теперь у процесса; родителю не нужен
    return proc


def test_open_pty_sets_winsize():
    """open_pty задаёт размер терминала (читаем обратно через TIOCGWINSZ)."""
    master, slave = open_pty()
    try:
        packed = fcntl.ioctl(master, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        rows, cols, _, _ = struct.unpack("HHHH", packed)
        assert (rows, cols) == (TERM_ROWS, TERM_COLS), (rows, cols)
    finally:
        os.close(master)
        os.close(slave)
    print(f"OK open_pty ставит winsize {TERM_ROWS}x{TERM_COLS}")


def test_driver_drains_output():
    """Драйвер отдаёт вывод процесса через on_output; на выходе закрывает master."""
    master, slave = open_pty()
    proc = _spawn_cat(slave)
    collected = bytearray()
    lock = threading.Lock()

    def on_output(chunk: bytes) -> None:
        with lock:
            collected.extend(chunk)

    answerer = _FakeAnswerer()
    thread = start_driver(master, on_output, answerer, name="drain")

    os.write(master, b"hello\n")
    time.sleep(0.5)
    proc.terminate()  # cat выходит → PTY закрывается → драйвер завершается
    proc.wait(timeout=5)
    thread.join(timeout=5)

    assert not thread.is_alive(), "драйвер завис после смерти процесса"
    with lock:
        out = bytes(collected)
    assert b"hello" in out, out
    # master закрыт драйвером на выходе — повторное закрытие даёт OSError.
    try:
        os.close(master)
        raise AssertionError("master не был закрыт драйвером")
    except OSError:
        pass
    print("OK драйвер дренирует вывод и закрывает master на выходе")


def test_driver_answers_dialog():
    """Драйвер, увидев маркер, пишет клавиши в PTY — процесс их эхом возвращает."""
    master, slave = open_pty()
    proc = _spawn_cat(slave)
    collected = bytearray()
    lock = threading.Lock()

    def on_output(chunk: bytes) -> None:
        with lock:
            collected.extend(chunk)

    answerer = _FakeAnswerer()
    # key_delay=0: не ждём межклавишную паузу в тесте.
    thread = threading.Thread(
        target=pty_driver,
        args=(master, on_output, answerer),
        kwargs={"name": "dialog", "key_delay": 0.0},
        daemon=True,
    )
    thread.start()

    os.write(master, b"PROMPT\n")  # cat вернёт → драйвер увидит маркер → ответит OK
    deadline = time.time() + 5
    while time.time() < deadline:
        with lock:
            if b"OK" in bytes(collected):
                break
        time.sleep(0.05)

    proc.terminate()
    proc.wait(timeout=5)
    thread.join(timeout=5)

    assert not thread.is_alive(), "драйвер завис"
    with lock:
        out = bytes(collected)
    assert b"OK" in out, f"клавиши-ответ не ушли в PTY: {out!r}"
    print("OK драйвер отвечает на стартовый диалог (клавиши уходят в PTY)")


def test_driver_writes_answer_in_one_shot():
    """Многобайтовый ответ (ESC-последовательность) уходит ОДНОЙ записью.

    Регресс: драйвер слал байты ответа по одному с паузой `key_delay` между
    ними — для «2\\r» безвредно, а у «\\x1b[B\\r» (↓ + Enter) голый ESC
    приходил отдельной клавишей. В trust-диалоге v2.1.275+ ESC = «Esc to
    cancel»: диалог отменялся, claude выходил, сессия крутилась в respawn-
    цикле на подтверждении папки (инцидент 2026-09-18, сессия ad-coder).
    """
    a, b = socket.socketpair()

    class _EscapeAnswerer:
        """Отвечает один раз последовательностью ↓+Enter (как trust-диалог)."""

        def __init__(self) -> None:
            self._sent = False

        def feed(self, chunk: bytes) -> list[bytes]:
            if not self._sent and b"PROMPT" in chunk:
                self._sent = True
                return [b"\x1b[B\r"]
            return []

    try:
        thread = threading.Thread(
            target=pty_driver,
            args=(a.fileno(), lambda _c: None, _EscapeAnswerer()),
            # settle_quiet=0: тест про атомарность записи, а не про паузу
            # перед ответом (её проверяет test_driver_settles_before_answer).
            kwargs={"name": "atomic", "key_delay": 0.5, "settle_quiet": 0.0},
            daemon=True,
        )
        thread.start()
        b.sendall(b"PROMPT\n")  # в socketpair данные с конца b читаются на a
        # key_delay=0.5: побайтовая запись отдала бы голый ESC сейчас, а
        # остальное — полсекунды спустя; атомарная отдаёт всё сразу.
        ready, _, _ = select.select([b], [], [], 0.2)
        assert ready, "ответ не пришёл"
        got = b.recv(64)
        assert got == b"\x1b[B\r", f"ESC-последовательность разорвана: {got!r}"
    finally:
        a.close()
        b.close()
        # Драйвер ждёт следующего чтения на закрытом fd — дожидаемся его
        # выхода, иначе зомби-поток перехватит PTY СЛЕДУЮЩЕГО теста (fd
        # переиспользуется) и сломает чужой прогон.
        thread.join(timeout=2)
    print("OK многобайтовый ответ уходит одной записью (ESC не разрывается)")


def test_driver_settles_before_answer():
    """Клавиши ответа уходят только после тишины в выводе.

    Регресс: драйвер отвечал в тот же миг, когда в выводе появлялся маркер
    диалога — то есть ПОСРЕДИ рендера. Trust-диалог v2.1.275+ обрабатывал
    такие клавиши криво: фокус переезжал ❯No→❯Yes→❯No, подтверждения не
    было, сессия умирала на молчании 60 с (инцидент 2026-09-18, сессия
    ad-coder). Живой тест диалога показал: на дорисованном экране те же
    клавиши подтверждают. Поэтому драйвер ждёт тишины settle_quiet и только
    тогда пишет клавиши; вывод в это время продолжает дренироваться.
    """
    a, b = socket.socketpair()

    class _EscapeAnswerer:
        """Отвечает один раз последовательностью ↓+Enter (как trust-диалог)."""

        def __init__(self) -> None:
            self._sent = False

        def feed(self, chunk: bytes) -> list[bytes]:
            if not self._sent and b"PROMPT" in chunk:
                self._sent = True
                return [b"\x1b[B\r"]
            return []

    try:
        thread = threading.Thread(
            target=pty_driver,
            args=(a.fileno(), lambda _c: None, _EscapeAnswerer()),
            kwargs={"name": "settle", "key_delay": 0.0, "settle_quiet": 0.3},
            daemon=True,
        )
        thread.start()
        b.sendall(b"PROMPT\n")
        # Рендер «ещё идёт»: шлём куски каждые 50 мс (чаще окна тишины) —
        # ответа в это время быть не должно.
        for _ in range(3):
            b.sendall(b"noise\n")
            time.sleep(0.05)
        ready, _, _ = select.select([b], [], [], 0.15)
        assert not ready, "драйвер ответил посреди рендера (не дождался тишины)"
        # Вывод затих → клавиши уходят.
        ready, _, _ = select.select([b], [], [], 0.8)
        assert ready, "после тишины ответ не пришёл"
        got = b.recv(64)
        assert got == b"\x1b[B\r", f"ESC-последовательность разорвана: {got!r}"
    finally:
        a.close()
        b.close()
        thread.join(timeout=2)
    print("OK ответ уходит только после тишины в выводе (диалог дорисовался)")


def test_driver_survives_on_output_valueerror():
    """Исключение ValueError из on_output (закрытый лог) драйвер не роняет —
    ловить его обязан сам колбэк; проверяем, что драйвер продолжает дренаж."""
    master, slave = open_pty()
    proc = _spawn_cat(slave)
    seen = []

    def on_output(chunk: bytes) -> None:
        try:
            raise ValueError("лог закрыт")
        except ValueError:
            pass
        seen.append(chunk)

    thread = start_driver(master, on_output, _FakeAnswerer(), name="ve")
    os.write(master, b"data\n")
    time.sleep(0.3)
    proc.terminate()
    proc.wait(timeout=5)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert any(b"data" in c for c in seen), seen
    print("OK колбэк с перехваченным ValueError не роняет драйвер")


def test_sessions_reexports_pty_symbols():
    """sessions.py реэкспортит open_pty/start_driver из box.pty."""
    from box.pty import open_pty as box_open_pty
    from box.pty import start_driver as box_start_driver
    from orchestrator.core import sessions

    assert sessions.open_pty is box_open_pty
    assert sessions.start_driver is box_start_driver
    print("OK sessions.py реэкспортит open_pty/start_driver из box.pty")


def main():
    test_open_pty_sets_winsize()
    test_driver_drains_output()
    test_driver_answers_dialog()
    test_driver_writes_answer_in_one_shot()
    test_driver_settles_before_answer()
    test_driver_survives_on_output_valueerror()
    test_sessions_reexports_pty_symbols()
    print("ALL BOX-PTY OK")


if __name__ == "__main__":
    main()
