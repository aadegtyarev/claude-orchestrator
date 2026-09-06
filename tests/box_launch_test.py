"""Композиция запуска (box.launch): поднять готовую команду под PTY и запустить
драйвер вывода/авто-ответов — на РЕАЛЬНОМ процессе (`sh`/`cat`), без оркестратора.

Проверяем:
  • launch спавнит процесс, on_output получает его вывод, handle.process живой;
  • стоп процесса не виснет: драйвер выходит, master закрыт (таймауты на join);
  • авто-ответ на стартовый диалог: `cat`-эхо кормит встроенный в launch
    _DialogAnswerer текстом диалога, тот пишет клавиши-ответ в PTY (видно по эху);
  • сбой спавна (несуществующий бинарь) не течёт fd и пробрасывает исключение;
  • oom_score_adj: ребёнок поднимает себе adj (и передаёт его внукам), а
    невозможность записи не роняет запуск.

box автономен — импортим из источника; launch зовётся из event loop (asyncio),
поэтому тесты — корутины (conftest.py гоняет их без pytest-asyncio).

Запуск: .venv/bin/python -m pytest tests/box_launch_test.py
"""
import asyncio
import os
import re
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


async def test_launch_raises_oom_score_adj():
    """oom_score_adj поднимает adj ребёнку И его потомкам.

    Зачем: OOM-killer выбирает жертву по oom_score_adj, а у всего cgroup он
    одинаковый — под нож шёл случайный процесс, часто не виновник (живой
    инцидент 2026-09-02: течь в сессии уносила оркестратор и соседние сессии).
    Сессия обязана быть более привлекательной жертвой, чем ядро, которое ею
    управляет. Наследование внуками принципиально: течёт обычно не сам claude,
    а то, что он запустил.

    Непривилегированный процесс умеет только ПОВЫШАТЬ свой adj, поэтому берём
    заведомо большее значение, чем у самого теста."""
    on_output, snapshot = _collector()
    mine = int(Path(f"/proc/{os.getpid()}/oom_score_adj").read_text().strip())
    target = mine + 200
    handle = await launch(
        # внук: sh -c порождает второй sh, печатающий СВОЙ adj
        ["/bin/sh", "-c", "cat /proc/self/oom_score_adj; sh -c 'cat /proc/self/oom_score_adj'"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        on_output=on_output,
        name="oom",
        oom_score_adj=target,
    )
    await asyncio.wait_for(handle.process.wait(), timeout=5)
    handle.driver_thread.join(timeout=5)
    got = [int(x) for x in re.findall(rb"\d+", snapshot())]
    assert got == [target, target], f"adj не выставлен/не наследуется: {snapshot()!r}"
    print("OK launch поднимает oom_score_adj ребёнку и внукам")


async def test_launch_oom_score_adj_failure_is_not_fatal():
    """Невозможность записать adj НЕ роняет запуск сессии.

    Живой отказ, ради которого это написано, — понижение adj непривилегированному
    (EACCES). Но воспроизводить его понижением нельзя: под root (CI-раннер)
    понижение РАЗРЕШЕНО, и тест там проверял бы не то. Берём отказ, который ядро
    даёт всем одинаково: значение вне диапазона [-1000, 1000] -> EINVAL.

    Сессия важнее оптимизации выбора жертвы: процесс обязан подняться, просто
    со старым adj."""
    on_output, snapshot = _collector()
    mine = int(Path(f"/proc/{os.getpid()}/oom_score_adj").read_text().strip())
    handle = await launch(
        ["/bin/sh", "-c", "cat /proc/self/oom_score_adj"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        on_output=on_output,
        name="oom-deny",
        oom_score_adj=5000,  # вне диапазона -> EINVAL хоть под root, хоть под юзером
    )
    await asyncio.wait_for(handle.process.wait(), timeout=5)
    handle.driver_thread.join(timeout=5)
    assert handle.process.returncode == 0, "запуск упал из-за отказа записи adj"
    got = [int(x) for x in re.findall(rb"\d+", snapshot())]
    assert got == [mine], f"adj неожиданно изменился: {snapshot()!r}"
    print("OK отказ записи oom_score_adj не роняет запуск")


def main():
    asyncio.run(test_launch_spawns_and_streams_output())
    asyncio.run(test_launch_auto_answers_dialog())
    asyncio.run(test_launch_spawn_failure_no_fd_leak())
    asyncio.run(test_launch_raises_oom_score_adj())
    asyncio.run(test_launch_oom_score_adj_failure_is_not_fatal())
    print("ALL BOX-LAUNCH OK")


if __name__ == "__main__":
    main()
