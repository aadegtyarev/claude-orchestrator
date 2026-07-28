"""Ресурсный preflight: цена сессии, чтение MemAvailable, вердикт по памяти.

Запуск: .venv/bin/python tests/resources_test.py
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.resources import (  # noqa: E402
    SESSION_COST_MB,
    available_ram_mb,
    check_memory,
    session_cost_mb,
)


def test_reads_mem_available(tmp_path: Path):
    f = tmp_path / "meminfo"
    f.write_text(
        "MemTotal:       32695888 kB\n"
        "MemFree:          812345 kB\n"
        "MemAvailable:   22489648 kB\n"
    )
    assert available_ram_mb(f) == 22489648 // 1024


def test_mem_available_unknown_when_file_missing(tmp_path: Path):
    assert available_ram_mb(tmp_path / "нет-такого") is None


def test_mem_available_unknown_when_field_absent(tmp_path: Path):
    """Ядро без MemAvailable (или чужой формат) — «не знаю», не ноль."""
    f = tmp_path / "meminfo"
    f.write_text("MemTotal: 32695888 kB\nMemFree: 812345 kB\n")
    assert available_ram_mb(f) is None


def test_mem_available_unknown_when_value_garbage(tmp_path: Path):
    f = tmp_path / "meminfo"
    f.write_text("MemAvailable:   много kB\n")
    assert available_ram_mb(f) is None


def test_cost_of_vm_is_its_memory():
    assert session_cost_mb("agent-vm", 4) == 4 * 1024
    # AGENT_VM_MEMORY_GIB не задан — дефолт самого agent-vm (2 GiB).
    assert session_cost_mb("agent-vm", None) == 2 * 1024


def test_cost_of_bwrap_and_off_is_process_estimate():
    assert session_cost_mb("bwrap") == SESSION_COST_MB
    assert session_cost_mb("off") == SESSION_COST_MB


def test_allows_when_enough_left():
    v = check_memory(available_mb=8000, needed_mb=700, min_free_mb=1024)
    assert v.allowed and not v.reason


def test_denies_when_below_threshold():
    v = check_memory(available_mb=1500, needed_mb=700, min_free_mb=1024)
    assert not v.allowed
    # Отказ объясняет себя числами: сколько есть, сколько надо, каков порог.
    assert "1500" in v.reason and "700" in v.reason and "1024" in v.reason


def test_boundary_is_allowed():
    """Ровно порог — ещё пускаем: отказ начинается СТРОГО ниже него."""
    v = check_memory(available_mb=1724, needed_mb=700, min_free_mb=1024)
    assert v.allowed


def test_disabled_check_allows_anything():
    v = check_memory(available_mb=10, needed_mb=4096, min_free_mb=0)
    assert v.allowed and not v.reason


def test_unknown_memory_allows():
    """Не смогли узнать память — не повод отказывать оператору в сессии."""
    v = check_memory(available_mb=None, needed_mb=4096, min_free_mb=1024)
    assert v.allowed and not v.reason


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
