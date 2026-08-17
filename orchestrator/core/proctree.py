"""Сигналы живости дерева процессов по /proc (Linux).

Вотчдог судит «завис/не завис» не только по байтам лога: спиннер «almost
done» может на секунды замолчать в нормальной работе — это не зависание.
Надёжный сигнал — CPU-время дерева процессов claude (он сам + запущенные
им тулы): если сумма utime+stime не растёт и дочерних процессов нет,
процесс правда стоит на месте.
"""

from __future__ import annotations

import os
import re

# Преамбула, которой Claude Code оборачивает КАЖДУЮ команду Bash-тула: подгрузка
# shell-снапшота, снятие алиасов и `eval '<настоящая команда>'`. В списке
# процессов она занимает всю ширину и прячет то единственное, что интересно —
# саму команду. Достаём её из eval, если он есть.
_EVAL_RE = re.compile(r"eval '(.+?)'(?:\s*<|\s*&&|\s*$)", re.DOTALL)
# Наша собственная обвязка сессии: channel-сервер живёт в дереве claude всегда и
# ни к какому «фону модели» отношения не имеет — в /bg это чистый шум.
_OURS_RE = re.compile(r"orchestrator/channel_server\.py")


def proc_tree_signals(root: int) -> tuple[int, bool]:
    """(сумма CPU-тиков дерева root, есть ли у root живые дочерние процессы).

    Один проход по /proc: для каждого процесса берём PPID (поле 4) и
    utime+stime (поля 14+15). Поле comm (2) может содержать пробелы и скобки,
    поэтому режем по последней ')' и нумеруем поля от неё.
    """
    by_ppid: dict[int, list[int]] = {}
    ticks: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return 0, False
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                raw = fh.read()
            after = raw[raw.rindex(b")") + 1:].split()
            ppid = int(after[1])              # поле 4 (ppid)
            tick = int(after[11]) + int(after[12])  # поля 14+15 (utime+stime)
        except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
            continue
        by_ppid.setdefault(ppid, []).append(int(name))
        ticks[int(name)] = tick
    if root not in ticks:
        return 0, False
    total = 0
    frontier = [root]
    seen: set[int] = set()
    while frontier:
        nxt: list[int] = []
        for pid in frontier:
            if pid in seen:
                continue
            seen.add(pid)
            total += ticks.get(pid, 0)
            nxt.extend(by_ppid.get(pid, ()))
        frontier = nxt
    return total, bool(by_ppid.get(root))


def short_cmd(cmdline: str, limit: int = 110) -> str:
    """Команда процесса без обвязки Bash-тула — то, что человек и хотел увидеть.

    Claude Code запускает каждую команду через shell-снапшот и `eval '…'`; в
    сыром виде это сотни символов служебного текста, одинаковых у всех
    процессов. Внутри eval — настоящая команда; её и берём.
    """
    text = " ".join(cmdline.split())
    m = _EVAL_RE.search(text)
    if m:
        text = " ".join(m.group(1).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def list_descendants(root: int, limit: int = 15) -> list[tuple[int, float, str]]:
    """Живые потомки процесса: [(pid, сколько секунд живёт, короткая команда)].

    Долгожители первыми: в «бесконечном ходе» интересна именно команда, которая
    висит часами (живой случай ikar 2026-08-17 — девять петель `until grep …;
    do sleep …; done`, ждавших сутки строку в мёртвом логе).

    Ядерных потоков в дереве не бывает, а процессы с пустым cmdline (зомби,
    гонка чтения) пропускаем. /proc недоступен → пустой список: это справка,
    падать из-за неё нельзя.
    """
    by_ppid: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return []
    hz = os.sysconf("SC_CLK_TCK") or 100
    starts: dict[int, float] = {}
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                raw = fh.read()
            after = raw[raw.rindex(b")") + 1:].split()
            ppid = int(after[1])          # поле 4 (ppid)
            start = int(after[19]) / hz   # поле 22 (starttime), тики → секунды
        except (OSError, ValueError, IndexError):
            continue
        by_ppid.setdefault(ppid, []).append(int(name))
        starts[int(name)] = start
    out: list[tuple[int, float, str]] = []
    frontier = list(by_ppid.get(root, ()))
    seen: set[int] = set()
    while frontier:
        pid, frontier = frontier[0], frontier[1:]
        if pid in seen:
            continue
        seen.add(pid)
        frontier.extend(by_ppid.get(pid, ()))
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if not cmdline.strip() or _OURS_RE.search(cmdline):
            continue
        out.append((pid, max(0.0, uptime - starts.get(pid, uptime)), short_cmd(cmdline)))
    out.sort(key=lambda row: row[1], reverse=True)
    return out[:limit]
