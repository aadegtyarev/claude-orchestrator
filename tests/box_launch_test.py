"""Композиция запуска (box.launch): поднять готовую команду под PTY и запустить
драйвер вывода/авто-ответов — на РЕАЛЬНОМ процессе (`sh`/`cat`), без оркестратора.

Проверяем:
  • launch спавнит процесс, on_output получает его вывод, handle.process живой;
  • стоп процесса не виснет: драйвер выходит, master закрыт (таймауты на join);
  • авто-ответ на стартовый диалог: `cat`-эхо кормит встроенный в launch
    _DialogAnswerer текстом диалога, тот пишет клавиши-ответ в PTY (видно по эху);
  • сбой спавна (несуществующий бинарь) не течёт fd и пробрасывает исключение.

box автономен — импортим из источника; launch зовётся из event loop (asyncio),
поэтому тесты — корутины (conftest.py гоняет их без pytest-asyncio).

Запуск: .venv/bin/python -m pytest tests/box_launch_test.py
"""
import asyncio
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box.launch import LaunchHandle, launch  # noqa: E402


def _collector():
    """(on_output, snapshot) с потокобезопасным накоплением байтов."""
    buf = bytearray()
    lock = threading.Lock()

    def on_output(chunk: bytes) -> None:
        with lock:
            buf.extend(chunk)

    def snapshot() -> bytes:
        with lock:
            return bytes(buf)

    return on_output, snapshot


async def test_launch_spawns_and_streams_output():
    """launch поднимает процесс; on_output получает вывод; handle — валидный;
    смерть процесса гасит драйвер (не виснет) и закрывает master."""
    on_output, snapshot = _collector()
    handle = await launch(
        ["/bin/sh", "-c", "echo hi; sleep 0.2"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        on_output=on_output,
        name="stream",
    )
    assert isinstance(handle, LaunchHandle)
    assert handle.process.returncode is None  # живой в момент возврата launch
    assert isinstance(handle.pty_master, int)
    assert handle.driver_thread.is_alive()

    await asyncio.wait_for(handle.process.wait(), timeout=5)
    handle.driver_thread.join(timeout=5)
    assert not handle.driver_thread.is_alive(), "драйвер завис после смерти процесса"
    assert b"hi" in snapshot(), snapshot()
    # master закрыт драйвером на выходе — повторное закрытие даёт OSError.
    try:
        os.close(handle.pty_master)
        raise AssertionError("master не был закрыт драйвером")
    except OSError:
        pass
    print("OK launch спавнит процесс, стримит вывод, чисто завершается")


async def test_launch_auto_answers_dialog():
    """Встроенный в launch _DialogAnswerer отвечает на стартовый диалог.

    `cat` эхом возвращает написанный в PTY текст диалога «Yes, I accept»
    (bypass-диалог); драйвер launch скармливает эхо своему _DialogAnswerer, тот
    матчит маркер и пишет клавиши-ответ «2\\r» в PTY — cat возвращает «2» эхом.
    Цифры «2» во вводе не было: её появление доказывает авто-ответ."""
    on_output, snapshot = _collector()
    handle = await launch(
        ["/bin/cat"],  # эхо stdin -> stdout
        cwd=os.getcwd(),
        env=dict(os.environ),
        on_output=on_output,
        name="dialog",
    )
    os.write(handle.pty_master, b"Yes, I accept\n")
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        # b"2" появится только как эхо клавиши-ответа авто-ответчика.
        if b"2" in snapshot():
            break
        await asyncio.sleep(0.05)
    assert b"2" in snapshot(), f"авто-ответ не ушёл в PTY: {snapshot()!r}"

    handle.process.terminate()
    await asyncio.wait_for(handle.process.wait(), timeout=5)
    handle.driver_thread.join(timeout=5)
    assert not handle.driver_thread.is_alive()
    print("OK launch авто-отвечает на стартовый диалог (клавиши уходят в PTY)")


async def test_launch_spawn_failure_no_fd_leak():
    """Сбой спавна (нет бинаря): исключение пробрасывается, fd не текут."""
    on_output, _ = _collector()
    fds_before = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    raised = False
    try:
        await launch(
            ["/nonexistent/binary/xyz"],
            cwd=os.getcwd(),
            env=dict(os.environ),
            on_output=on_output,
            name="fail",
        )
    except (FileNotFoundError, OSError):
        raised = True
    assert raised, "launch не пробросил ошибку спавна"
    # PTY-пара (master+slave) должна быть закрыта — иначе счётчик fd подрастёт.
    fds_after = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    assert fds_after <= fds_before, f"fd утекли: {fds_before} -> {fds_after}"
    print("OK сбой спавна: исключение проброшено, fd не утекли")


def main():
    asyncio.run(test_launch_spawns_and_streams_output())
    asyncio.run(test_launch_auto_answers_dialog())
    asyncio.run(test_launch_spawn_failure_no_fd_leak())
    print("ALL BOX-LAUNCH OK")


if __name__ == "__main__":
    main()
