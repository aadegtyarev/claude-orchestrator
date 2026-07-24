"""Регресс гонки портов в фиксированном пуле (CHANNEL_PORT_START/END).

Порт выданной, но ещё не поднявшейся сессии резервируется (_inflight_ports),
чтобы конкурентный /new не получил тот же порт и не увёл сообщения в чужой
channel-сервер. Резерв снимается при старте watcher'а и при провале старта.

Запуск: .venv/bin/python tests/portpool_test.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.sessions import SessionManager  # noqa: E402


def mgr(lo, hi):
    m = SessionManager.__new__(SessionManager)
    m.config = SimpleNamespace(channel_port_start=lo, channel_port_end=hi)
    m._by_name = {}
    m._inflight_ports = set()
    return m


def main():
    # Диапазон из двух фиксированных портов НИЖЕ эфемерного диапазона ОС
    # (Linux ip_local_port_range обычно 32768–60999). _find_free_port делает
    # реальный bind-проб: если взять порты ВНУТРИ эфемерного диапазона (напр.
    # 53001), любой сокет другого теста этого же процесса (TIME_WAIT от asyncio-/
    # aiohttp-тестов, ephemeral-listener) мог бы транзитно занять порт → bind
    # упал бы → тест ложно падал (флап). Порты 20001/20002 в эфемерный диапазон
    # не попадают, поэтому проб детерминирован.
    m = mgr(20001, 20002)
    p1 = m._find_free_port()
    assert p1 in (20001, 20002), p1
    # Второй вызов до старта первой сессии НЕ должен повторить порт (резерв).
    p2 = m._find_free_port()
    assert p2 in (20001, 20002) and p2 != p1, (p1, p2)
    # Пул исчерпан (оба «в полёте») → None, а не дубль.
    assert m._find_free_port() is None
    print("OK фиксированный пул: порт «в полёте» не выдаётся повторно")

    # Освобождение резерва → порт снова доступен.
    m._inflight_ports.discard(p1)
    p3 = m._find_free_port()
    assert p3 == p1, (p1, p3)
    print("OK освобождённый резерв снова доступен")

    # Авто-режим (пул не задан): порт от ОС тоже резервируется.
    a = mgr(0, 0)
    ap = a._find_free_port()
    assert ap and ap in a._inflight_ports
    print("OK авто-режим: порт от ОС попадает в резерв")

    print("ALL PORTPOOL OK")


def test_portpool():
    main()


if __name__ == "__main__":
    main()
