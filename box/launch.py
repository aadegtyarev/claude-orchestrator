"""Композиция запуска: поднять готовую команду под PTY и запустить драйвер.

Слой 2 редизайна (docs/ARCHITECTURE-claude-box.md §5): единая точка «поднять
готовую команду под псевдотерминалом». Собирает вместе примитивы box.pty
(open_pty + драйвер дренажа/авто-ответов из box.dialog) и спавн процесса на
slave-конце PTY, чтобы оркестратор не делал это инлайном.

Что ЗДЕСЬ (box): open_pty → спавн процесса на slave (asyncio) → закрытие slave →
создание _DialogAnswerer → запуск драйвера. Возвращает LaunchHandle с ручками,
которые нужны вызывающему для трекинга (process/pty_master/answerer/driver).

Что НЕ здесь (оркестратор): сборка argv/env/cwd, изоляция раннером, запись
вывода в лог (передаётся колбэком on_output), ожидание готовности по /ping,
resume/clear, состояние сессии — всё это надстройка над launch. argv/env/cwd и
колбэк приходят аргументами: ноль зависимостей от SessionManager/оркестратора.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from threading import Thread
from typing import Callable, Sequence

from .dialog import _DialogAnswerer
from .pty import TERM_COLS, TERM_ROWS, open_pty, start_driver


@dataclass
class LaunchHandle:
    """Ручки поднятой под PTY команды — то, что нужно вызывающему для трекинга.

    process       — спавненный процесс (asyncio); вызывающий следит за его смертью;
    pty_master    — master-fd PTY; им ВЛАДЕЕТ драйвер и закроет сам на выходе,
                    вызывающий держит номер для записи в stdin (ввод команд/Esc);
    answerer      — авто-ответчик стартовых диалогов; глушится (stop) по готовности;
    driver_thread — демон-поток драйвера (дренаж вывода + авто-ответы на диалоги).
    """

    process: asyncio.subprocess.Process
    pty_master: int
    answerer: _DialogAnswerer
    driver_thread: Thread


def _oom_setter(adj: int | None) -> Callable[[], None] | None:
    """preexec_fn, выставляющий oom_score_adj ребёнку. None -> не трогать.

    Между fork и exec: значение переживает exec и наследуется потомками, то есть
    накрывает всё дерево сессии, а не только сам claude.

    Ошибка записи ГЛУШИТСЯ намеренно: исключение из preexec_fn убивает спавн, а
    отказ ядра (понижение adj непривилегированному — EACCES) не повод не
    поднимать сессию. Логировать отсюда нельзя — мы уже в форкнутом ребёнке.
    """
    if adj is None:
        return None

    def _set() -> None:
        try:
            with open("/proc/self/oom_score_adj", "w") as f:
                f.write(str(adj))
        except OSError:
            pass

    return _set


async def launch(
    argv: Sequence[str],
    *,
    cwd: str,
    env: dict[str, str],
    on_output: Callable[[bytes], None],
    name: str = "",
    rows: int = TERM_ROWS,
    cols: int = TERM_COLS,
    oom_score_adj: int | None = None,
) -> LaunchHandle:
    """Поднять готовую команду `argv` под PTY и запустить драйвер вывода.

    Открывает PTY заданного размера, спавнит процесс на его slave-конце
    (stdin/out/err = slave, своя сессия процессов — start_new_session), закрывает
    slave, создаёт авто-ответчик стартовых диалогов и запускает поток-драйвер,
    который дренирует вывод процесса в `on_output` и печатает клавиши-ответы в PTY.

    Размер терминала ставит open_pty: без него winsize = 0×0, и claude агрессивно
    зондирует размер через CPR — под двойным PTY (agent-vm) ответы на зонды текут
    мусором в stdin. Драйвер владеет master-fd и закроет его сам, когда процесс
    закроет PTY.

    oom_score_adj — сделать процесс более привлекательной жертвой для OOM-killer,
    чем оркестратор, который им управляет. Ставится в preexec_fn (между fork и
    exec), поэтому наследуется ВСЕМ деревом сессии: течёт обычно не сам claude,
    а то, что он запустил. Ядро умеет только ПОВЫШАТЬ adj непривилегированному —
    попытка понизить даёт EACCES и намеренно игнорируется: не выставленный adj
    это упущенная оптимизация выбора жертвы, а упавший спавн — потерянная
    сессия. None = не трогать.

    Сбой спавна: master и slave закрываются (fd не текут), исключение
    пробрасывается — вызывающий чистит своё (лог) и решает, что делать.
    """
    master, slave = open_pty(rows, cols)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
            preexec_fn=_oom_setter(oom_score_adj),
        )
    except Exception:
        os.close(master)
        raise
    finally:
        os.close(slave)
    answerer = _DialogAnswerer()
    driver_thread = start_driver(master, on_output, answerer, name=name)
    return LaunchHandle(
        process=process,
        pty_master=master,
        answerer=answerer,
        driver_thread=driver_thread,
    )
