"""Свободные ресурсы машины и цена сессии — чистые функции, без побочек.

Зачем: сессия Claude Code — это процесс на сотни мегабайт (под `agent-vm` —
целая microVM на гигабайты). Лимит `MAX_INSTANCES` считает ШТУКИ и ничего не
знает про то, сколько на машине осталось памяти: пять сессий на 32 GiB
проходят легко, пять же на ноутбуке с 8 GiB уводят машину в своп, а то и под
OOM-killer — причём убьёт он обычно не новую сессию, а случайную соседнюю.

Поэтому перед стартом спрашиваем машину, а не только счётчик. Решение принимает
`check_memory`: она НЕ читает /proc и ничего не запускает — всё приходит
аргументами, поэтому её можно проверить тестом на любых числах, включая те,
которые на живой машине не воспроизвести.

Читает систему ровно одна функция — `available_ram_mb`. На не-Linux (или если
/proc/meminfo недоступен) она возвращает None = «не знаю», и проверка тогда
пропускается: неизвестность не повод отказывать оператору в сессии.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MEMINFO = Path("/proc/meminfo")

# Во что обходится сессия под bwrap/off: сам claude плюс его channel-сервер.
# Оценка снята с живого прода (claude ~0.4-0.5 GiB RSS на разогретой сессии,
# channel_server — десятки мегабайт) и намеренно взята с запасом: занизить
# опаснее, чем завысить — заниженная цена пропускает старт, после которого
# машина уходит в своп.
SESSION_COST_MB = 700


@dataclass(frozen=True)
class MemoryVerdict:
    """Итог проверки: пускаем ли старт и что сказать оператору.

    `allowed=False` — старт отклоняем, `reason` уже человекочитаемый.
    `reason` непустой и при allowed=True ровно в одном случае: проверка
    выключена или память неизвестна (тогда reason пуст) — см. check_memory.
    """

    allowed: bool
    reason: str = ""
    available_mb: int | None = None
    needed_mb: int = 0


def available_ram_mb(meminfo: Path = MEMINFO) -> int | None:
    """Сколько мегабайт памяти реально доступно под новый процесс.

    Берём MemAvailable, а не MemFree: MemFree на Linux почти всегда мал (ядро
    держит кеш страниц), и по нему любой старт выглядел бы невозможным.
    MemAvailable — оценка самого ядра «сколько можно занять, не уходя в своп»,
    то есть ровно то, что нас интересует.

    None = не смогли узнать (не Linux, файла нет, формат другой). Вызывающий
    трактует None как «проверять нечем», а не как «памяти нет».
    """
    try:
        raw = meminfo.read_text()
    except OSError:
        return None
    for line in raw.splitlines():
        if not line.startswith("MemAvailable:"):
            continue
        parts = line.split()
        if len(parts) < 2:
            return None
        try:
            return int(parts[1]) // 1024  # /proc/meminfo — килобайты
        except ValueError:
            return None
    return None


def session_cost_mb(sandbox: str, vm_memory_gib: int | None = None) -> int:
    """Во сколько мегабайт обойдётся сессия с этим движком изоляции.

    Под `agent-vm` цена известна ТОЧНО: столько памяти получит microVM
    (`AGENT_VM_MEMORY_GIB`, дефолт самого agent-vm — 2 GiB). Под bwrap/off
    точной цены нет, берём оценку с запасом.
    """
    if sandbox == "agent-vm":
        # Именно `is None` (не `or`): 0 в конфиге — это ошибка оператора, и
        # подменять её дефолтом значит прятать её ценой заниженной цены сессии.
        gib = 2 if vm_memory_gib is None else vm_memory_gib
        return gib * 1024
    return SESSION_COST_MB


def check_memory(
    available_mb: int | None,
    needed_mb: int,
    min_free_mb: int,
) -> MemoryVerdict:
    """Хватит ли памяти на новую сессию, чтобы машине осталось min_free_mb.

    Пускаем, если после старта останется не меньше `min_free_mb`. Отказываем
    ДО запуска: сессия, которую OOM-killer прибьёт через минуту, хуже честного
    отказа — она успевает утащить за собой соседнюю.

    Пропускаем проверку (allowed=True, reason пуст), если она выключена
    (`min_free_mb <= 0`) или память неизвестна (`available_mb is None`).
    """
    if min_free_mb <= 0 or available_mb is None:
        return MemoryVerdict(True, available_mb=available_mb, needed_mb=needed_mb)
    if available_mb - needed_mb >= min_free_mb:
        return MemoryVerdict(True, available_mb=available_mb, needed_mb=needed_mb)
    return MemoryVerdict(
        False,
        reason=(
            f"свободно {available_mb} МБ, сессии нужно ~{needed_mb} МБ, "
            f"порог запаса — {min_free_mb} МБ"
        ),
        available_mb=available_mb,
        needed_mb=needed_mb,
    )
