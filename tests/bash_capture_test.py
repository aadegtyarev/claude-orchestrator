"""Перехват вывода команды потоком: файл + хвост в памяти.

Регресс, ради которого это сделано: раньше вывод искали заново в кольцевом
буфере PTY (200 КБ). Команда, напечатавшая больше, ВЫТЕСНЯЛА из буфера метку
начала — оркестратор переставал понимать, где начался вывод, не находил метку
конца и досиживал до таймаута в 10 минут с пустым результатом. То есть фича
«полный вывод файлом» отказывала ровно на тех командах, ради которых делалась
(сборка, pytest на большом наборе).

Теперь разбор идёт ПОТОКОМ, по мере поступления байтов: ничего не теряется,
сколько бы команда ни напечатала.

Запуск: .venv/bin/python tests/bash_capture_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.bashshell import OutputCapture  # noqa: E402

BEG = "__BEG_x__"
DONE = "__DONE_x__"


def make(tmp_path: Path, **kw) -> OutputCapture:
    return OutputCapture(tmp_path / "out.txt", BEG, DONE, **kw)


def test_ignores_everything_before_start_marker(tmp_path: Path):
    """До метки начала — приглашение шелла и эхо самой команды, это не вывод."""
    cap = make(tmp_path)
    cap.feed(b"user@host:~$ echo " + BEG.encode() + b"\n")
    assert cap.tail() == b""
    assert not cap.done


def test_captures_output_between_markers(tmp_path: Path):
    cap = make(tmp_path)
    cap.feed(f"{BEG}\nстрока 1\nстрока 2\n{DONE} 0\n".encode())
    assert cap.done
    assert cap.code == "0"
    assert cap.tail() == "строка 1\nстрока 2\n".encode()


def test_exit_code_is_captured(tmp_path: Path):
    cap = make(tmp_path)
    cap.feed(f"{BEG}\nупало\n{DONE} 127\n".encode())
    assert cap.code == "127"


def test_survives_marker_split_across_chunks(tmp_path: Path):
    """PTY отдаёт данные кусками произвольной длины — метка рвётся пополам.

    Без склейки на границе кусков команда «зависала» бы навсегда.
    """
    cap = make(tmp_path)
    data = f"{BEG}\nвывод\n{DONE} 0\n".encode()
    for i in range(len(data)):  # худший случай: по одному байту
        cap.feed(data[i:i + 1])
    assert cap.done and cap.code == "0"
    assert cap.tail() == "вывод\n".encode()


def test_huge_output_does_not_break_detection(tmp_path: Path):
    """ГЛАВНЫЙ регресс: вывод сильно больше прежнего буфера в 200 КБ."""
    cap = make(tmp_path)
    cap.feed(f"{BEG}\n".encode())
    line = b"x" * 100 + b"\n"
    for _ in range(5000):  # ~500 КБ
        cap.feed(line)
    cap.feed(f"{DONE} 0\n".encode())
    cap.close()
    assert cap.done and cap.code == "0"
    # Файл получил ВЕСЬ вывод, а не последние 200 КБ (± придержанный хвост
    # последнего куска, который в файл не попадает — он уже за меткой конца).
    assert cap.path.stat().st_size >= 5000 * len(line) - cap.tail_cap


def test_tail_is_bounded(tmp_path: Path):
    """В памяти держим только хвост — показывать всё равно негде."""
    cap = make(tmp_path, tail_cap=1000)
    cap.feed(f"{BEG}\n".encode())
    cap.feed(b"y" * 50_000 + b"\n")
    cap.feed(f"{DONE} 0\n".encode())
    assert len(cap.tail()) <= 1000
    assert cap.tail().endswith(b"y\n")


def test_full_output_written_to_file(tmp_path: Path):
    """Переполнивший хвост вывод попадает в файл ЦЕЛИКОМ, с самого начала.

    Важно не только «файл есть», но и что в нём начало вывода: именно оно
    раньше терялось, когда хвост вытеснялся кольцевым буфером.
    """
    cap = make(tmp_path, tail_cap=100)
    cap.feed(f"{BEG}\n".encode())
    cap.feed("НАЧАЛО\n".encode())
    cap.feed(b"m" * 5000 + b"\n")
    cap.feed("КОНЕЦ\n".encode())
    cap.feed(f"{DONE} 0\n".encode())
    cap.close()
    text = cap.path.read_text(encoding="utf-8")
    assert text.startswith("НАЧАЛО\n")
    assert text.endswith("КОНЕЦ\n")


def test_ansi_is_stripped(tmp_path: Path):
    """Раскраска не должна ни ломать поиск меток, ни лезть в файл."""
    cap = make(tmp_path)
    cap.feed(f"{BEG}\n".encode())
    cap.feed("\x1b[31mкрасный\x1b[0m\n".encode())
    cap.feed(f"{DONE} 0\n".encode())
    assert cap.tail() == "красный\n".encode()


def test_ansi_split_across_chunks(tmp_path: Path):
    """Escape-последовательность тоже рвётся между кусками."""
    cap = make(tmp_path)
    cap.feed(f"{BEG}\n".encode())
    for part in (b"\x1b", b"[31m", "текст".encode(), b"\x1b[0m", b"\n"):
        cap.feed(part)
    cap.feed(f"{DONE} 0\n".encode())
    assert cap.tail() == "текст\n".encode()


def test_nothing_captured_after_done(tmp_path: Path):
    """Приглашение шелла после команды — уже не её вывод."""
    cap = make(tmp_path)
    cap.feed(f"{BEG}\nвывод\n{DONE} 0\nuser@host:~$ ".encode())
    assert cap.tail() == "вывод\n".encode()


def test_no_file_when_output_is_small(tmp_path: Path):
    """Файл не создаём, пока вывод влезает в хвост — не сорим на каждый ls."""
    cap = make(tmp_path, tail_cap=10_000)
    cap.feed(f"{BEG}\nкоротко\n{DONE} 0\n".encode())
    cap.close()
    assert not cap.path.exists()
    assert cap.overflowed is False


def test_file_appears_only_on_overflow(tmp_path: Path):
    cap = make(tmp_path, tail_cap=100)
    cap.feed(f"{BEG}\n".encode())
    cap.feed(b"z" * 5000)
    cap.feed(f"{DONE} 0\n".encode())
    cap.close()
    assert cap.overflowed is True
    # Размер с точностью до придержанного хвоста (см. _keep): он попадает в
    # файл только когда придёт продолжение или закроется перехват.
    assert cap.path.exists() and cap.path.stat().st_size >= 5000 - 64


def test_pending_does_not_grow_unbounded(tmp_path: Path):
    """Придержанный хвост зажат размером метки, а не растёт с выводом.

    Иначе гигабайтная команда без метки конца съела бы память процесса.
    """
    cap = make(tmp_path, tail_cap=1000)
    cap.feed(f"{BEG}\n".encode())
    for _ in range(200):
        cap.feed(b"q" * 5000)
    assert len(cap._pending) <= cap._keep()


def test_output_survives_shell_echo_turned_back_on(tmp_path: Path):
    """Оператор сделал `stty sane` — эхо вернулось, разбор обязан выжить.

    Тогда метки приходят дважды: эхом строки `…$ echo __BEG__` и выводом.
    Взять первую значило бы записать в вывод приглашение и текст команды.
    """
    cap = make(tmp_path)
    cap.feed(f"user@host:/tmp$ echo {BEG}\n{BEG}\nнастоящий вывод\n".encode())
    cap.feed(f"user@host:/tmp$ echo {DONE} $?\n{DONE} 0\n".encode())
    assert cap.done and cap.code == "0"
    assert cap.tail() == "настоящий вывод\n".encode()


def test_feed_after_close_does_not_truncate_file(tmp_path: Path):
    """Байты, пришедшие ПОСЛЕ close, не должны обнулять готовый файл.

    Регресс: между close() и отцепкой перехвата убежавшая команда продолжает
    печатать. Без флага «закрыт» следующий кусок заводил файл заново поверх
    полного лога, и оператор получал вместо него последние килобайты —
    ровно тот отказ, который эта ветка чинит, только молчаливый.
    """
    cap = make(tmp_path, tail_cap=1000)
    cap.feed(f"{BEG}\n".encode())
    cap.feed(b"A" * 200_000 + b"\n")
    size = cap.path.stat().st_size
    assert size > 100_000
    cap.close()
    cap.feed(b"B" * 5000)  # команда ещё льёт
    assert cap.path.stat().st_size == size


def test_output_between_show_limit_and_tail_reaches_file(tmp_path: Path):
    """Вывод, не влезший в сообщение, ОБЯЗАН приехать файлом.

    Регресс: хвост был крупнее лимита показа, и вывод на 20 КБ оператор видел
    обрезанным, а файла не получал вовсе — то есть терял то, что до этой
    ветки доезжало целиком. Поэтому run_bash держит хвост ровно по размеру
    показа; тест фиксирует саму связку.
    """
    cap = make(tmp_path, tail_cap=3500)
    cap.feed(f"{BEG}\n".encode())
    cap.feed(b"x" * 20_000 + b"\n")
    cap.feed(f"{DONE} 0\n".encode())
    cap.close()
    assert cap.overflowed and cap.path.exists()
    assert cap.path.stat().st_size >= 20_000


def test_pending_bounded_when_done_marker_has_no_code(tmp_path: Path):
    """Метку конца съела интерактивная программа — память не должна расти.

    `cat`/`ssh` получают строку `echo __DONE__ $?` себе на вход и отражают её
    БЕЗ подстановки кода. Кода не будет никогда, и без обрезки весь
    дальнейший вывод копился бы в памяти вместо файла.
    """
    cap = make(tmp_path, tail_cap=1000)
    cap.feed(f"{BEG}\n".encode())
    cap.feed(f"{DONE} $?\n".encode())  # эхо без кода
    for _ in range(50):
        cap.feed(b"z" * 5000)
    assert not cap.done
    assert len(cap._pending) <= cap._keep()
    assert cap.overflowed  # вывод уехал в файл, а не в память
    cap.close()


def test_write_failure_is_not_retried_every_chunk(tmp_path: Path, monkeypatch):
    """Диск кончился — сдаёмся насовсем, а не открываем файл на каждом куске.

    Иначе тысячи попыток в секунду: лог в варнингах и добитый раздел.
    """
    cap = make(tmp_path, tail_cap=100)
    opens = {"n": 0}
    real_open = Path.open

    def counting_open(self, *a, **kw):
        opens["n"] += 1
        raise OSError("диск полон")

    monkeypatch.setattr(Path, "open", counting_open)
    cap.feed(f"{BEG}\n".encode())
    for _ in range(30):
        cap.feed(b"q" * 500)
    monkeypatch.setattr(Path, "open", real_open)
    assert opens["n"] == 1          # ровно одна попытка
    assert cap.overflowed is False  # и честно: файла нет


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
