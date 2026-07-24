"""Ядро PTY-запуска: поднять процесс под псевдотерминалом и дренировать вывод.

Самодостаточный кусок launch-механики (Слой 2, docs/ARCHITECTURE-claude-box.md
§5): открыть PTY нужного размера и гонять драйвер, который читает вывод
процесса, отвечает на стартовые диалоги (`box.dialog._DialogAnswerer`) и отдаёт
сырые байты наружу через колбэк. Никаких зависимостей от SessionManager/
оркестратора — куда писать вывод (лог/экран сессии) и как поднят сам процесс,
решает вызывающий; сюда он передаёт master-fd, ответчик и `on_output`.

Что ЗДЕСЬ (box): openpty + размер терминала, поток-драйвер (дренаж PTY, чтобы
буфер не переполнился и процесс не встал; авто-ответы на стартовые диалоги;
владение master-fd и его закрытие на выходе).

Что НЕ здесь (оркестратор): сборка argv/env/cwd, изоляция раннером, сам спавн
процесса (`asyncio.create_subprocess_exec` со slave-fd), запись вывода в
`claude.log`, ожидание готовности по HTTP `/ping` к channel-серверу. Оркестратор
открывает PTY через `open_pty()`, спавнит процесс на slave, затем запускает
драйвер `start_driver(master, on_output=…, answerer=…)` — где `on_output` пишет
в лог/экран сессии.
"""

from __future__ import annotations

import fcntl
import logging
import os
import pty
import struct
import termios
import threading
import time
from typing import Callable

from .dialog import _DialogAnswerer

logger = logging.getLogger(__name__)

# Фиксированный размер терминала. Без него winsize = 0×0, и Claude Code
# агрессивно зондирует размер через CPR (\x1b[6n); под agent-vm (двойной PTY
# через attach) ответы на зонды текут одиночными цифрами в stdin — мусор в
# экране, а изредка (если следом CR) уходит спурьёзным сообщением. Фиксированный
# размер это гасит.
TERM_ROWS = 40
TERM_COLS = 120

# Пауза между байтами-ответами на стартовый диалог: клавиши шлём по одной с
# задержкой, иначе TUI не успевает перерисоваться и «проглатывает» ввод.
KEY_DELAY_SEC = 0.3


def open_pty(rows: int = TERM_ROWS, cols: int = TERM_COLS) -> tuple[int, int]:
    """Открыть PTY и задать размер терминала. Возвращает (master, slave).

    Размер ставится на slave (сторона процесса). OSError при ioctl глотаем —
    как в исходном коде: даже без размера процесс поднимется (просто зонды CPR).
    Вызывающий обязан закрыть slave после спавна процесса и master — через
    драйвер (или сам, если драйвер не запущен).
    """
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass
    return master, slave


def pty_driver(
    master: int,
    on_output: Callable[[bytes], None],
    answerer: _DialogAnswerer,
    *,
    name: str = "",
    key_delay: float = KEY_DELAY_SEC,
) -> None:
    """Тело потока-драйвера PTY: дренирует вывод процесса, отдаёт его через
    `on_output` и отвечает на стартовые диалоги.

    Дренаж обязателен: без чтения буфер PTY переполнится и процесс встанет.
    Каждый прочитанный кусок уходит в `on_output` (вызывающий пишет его в лог/
    экран). Тот же кусок скармливается `answerer`; на матч стартового диалога
    его клавиши пишутся обратно в PTY по одной с паузой `key_delay`.

    Поток ВЛАДЕЕТ master-fd и сам закрывает его на выходе — закрытие из другого
    потока/цикла событий могло бы освободить номер fd, пока драйвер блокирован в
    read. `on_output` обязан сам глотать свои ошибки (напр. запись в уже закрытый
    лог) — прочее исключение из него, как и раньше, завершит драйвер (fd закрыт в
    finally).
    """
    try:
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            on_output(chunk)
            for keys in answerer.feed(chunk):
                logger.info("Драйвер %s: отвечаю на стартовый диалог", name)
                for key in keys:
                    try:
                        os.write(master, bytes([key]))
                    except OSError:
                        return
                    time.sleep(key_delay)
    finally:
        try:
            os.close(master)
        except OSError:
            pass


def start_driver(
    master: int,
    on_output: Callable[[bytes], None],
    answerer: _DialogAnswerer,
    *,
    name: str = "",
) -> threading.Thread:
    """Запустить `pty_driver` в демон-потоке и вернуть его.

    Удобная обёртка для оркестратора: он держит master-fd/ответчик в объекте
    сессии, а драйвер владеет fd и гасится сам, когда процесс закрывает PTY.
    """
    thread = threading.Thread(
        target=pty_driver,
        args=(master, on_output, answerer),
        kwargs={"name": name},
        name=f"pty-{name}" if name else "pty-driver",
        daemon=True,
    )
    thread.start()
    return thread
