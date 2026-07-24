"""Готовность сессии: ждём по ТИШИНЕ, а не по общему времени.

Холодный старт agent-vm тянет OCI-образ минутами (замерено ~4 мин), и
фиксированные 60с убивали сессию до того, как Claude вообще стартовал. Но и
ждать «сколько угодно» нельзя — зависший процесс должен падать быстро.

Решение: дедлайн отсчитывается от ПОСЛЕДНЕГО признака жизни (растёт claude.log
= что-то происходит: тянется образ, грузится VM, стартует Claude). Молчание
дольше READY_SILENCE_SEC — сдаёмся. Сверху абсолютный потолок, чтобы
бесконечно «прогрессирующий» процесс не висел вечно.

Дедлайн переехал в автономный пакет box/ (box.ready, Слой 2 редизайна) —
импортим из источника; отдельным тестом проверяем, что sessions.py его
реэкспортит (обратная совместимость).

Запуск: .venv/bin/python tests/box_ready_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box.ready import (  # noqa: E402
    READY_SILENCE_SEC,
    READY_TIMEOUT_MAX,
    _ReadyDeadline,
)


def test_silence_expires():
    """Нет прогресса дольше окна тишины → сдаёмся."""
    d = _ReadyDeadline(started_at=1000.0)
    assert d.expired(1000.0 + READY_SILENCE_SEC - 1) is None
    assert d.expired(1000.0 + READY_SILENCE_SEC + 1) == "silence"
    print("OK молчание дольше окна тишины → отказ")


def test_no_cap_by_default_behaves_like_old_timeout():
    """Без явного запаса потолок = окно тишины: поведение как у прежнего
    фиксированного таймаута.

    Важно для bwrap (самый частый путь): зависший Claude может крутить спиннер
    и растить лог — тогда «признак жизни» держал бы нас до потолка. Запас даём
    только agent-vm, где загрузка образа реально идёт минутами.
    """
    d = _ReadyDeadline(started_at=1000.0)
    now = 1000.0
    size = 0
    while now < 1000.0 + READY_SILENCE_SEC + 5:
        now += 10
        size += 100
        d.note_progress(size=size, now=now)  # «спиннер» пишет в лог
    assert d.expired(now) == "cap", "без запаса растущий лог не должен держать нас"
    print("OK без запаса ведём себя как прежний фиксированный таймаут")


def test_progress_extends_deadline():
    """Растущий лог продлевает ожидание — холодная загрузка образа доживает."""
    d = _ReadyDeadline(started_at=1000.0, cap=READY_TIMEOUT_MAX)
    now = 1000.0
    # 5 минут «тянется образ»: лог растёт каждые 30с
    for i in range(10):
        now += 30
        d.note_progress(size=1000 * (i + 1), now=now)
        assert d.expired(now) is None, f"не должен сдаться на прогрессе (t={now})"
    assert now - 1000.0 > READY_SILENCE_SEC, "проверяем именно случай дольше окна"
    print("OK прогресс лога продлевает ожидание (холодный старт доживает)")


def test_progress_only_when_log_grows():
    """Тот же размер лога — не прогресс (иначе зависший процесс ждали бы вечно)."""
    d = _ReadyDeadline(started_at=1000.0)
    d.note_progress(size=500, now=1010.0)
    # дальше лог НЕ растёт: повторный тот же размер прогрессом не считается
    for t in (1020.0, 1030.0, 1040.0):
        d.note_progress(size=500, now=t)
    assert d.expired(1010.0 + READY_SILENCE_SEC + 1) == "silence"
    print("OK неизменный размер лога прогрессом не считается")


def test_absolute_cap():
    """Даже при бесконечном прогрессе есть потолок."""
    d = _ReadyDeadline(started_at=1000.0, cap=READY_TIMEOUT_MAX)
    now = 1000.0
    size = 0
    while now < 1000.0 + READY_TIMEOUT_MAX + 10:
        now += 30
        size += 1000
        d.note_progress(size=size, now=now)
    assert d.expired(now) == "cap"
    print("OK абсолютный потолок срабатывает даже при постоянном прогрессе")


def test_sessions_reexports_ready_symbols():
    """sessions.py реэкспортит _ReadyDeadline/READY_* из box — старый код и
    тесты, ссылающиеся на sessions._ReadyDeadline, не сломаны."""
    from orchestrator.core import sessions

    assert sessions._ReadyDeadline is _ReadyDeadline
    assert sessions.READY_SILENCE_SEC == READY_SILENCE_SEC
    assert sessions.READY_TIMEOUT_MAX == READY_TIMEOUT_MAX
    print("OK sessions.py реэкспортит _ReadyDeadline/READY_* из box.ready")


def main():
    test_silence_expires()
    test_no_cap_by_default_behaves_like_old_timeout()
    test_progress_extends_deadline()
    test_progress_only_when_log_grows()
    test_absolute_cap()
    test_sessions_reexports_ready_symbols()
    print("ALL BOX-READY OK")


if __name__ == "__main__":
    main()
