"""Постоянный bash-терминал на топик — в обход Claude Code, напрямую в систему.

Один PTY-процесс `bash -i` на топик, живёт между вызовами /bash (поэтому `cd`
внутри одной команды сохраняется для следующей). /bash пишет команду + маркер
конца, стримит вывод в статус-сообщение, ждёт маркер. /bashin пишет сырой ввод
в тот же PTY — ответ на «y/n» интерактивной команды, которая всё ещё крутится
в текущем /bash.

Права и cwd — как у процесса бота (мимо Claude Code, без permission relay).
Скоуп задаётся вызывающим (core/app.py run_bash) через параметр wrapper:
  * /bash В СЕССИИ (топик) при SANDBOX=bwrap — та же файловая песочница, что и
    claude (папка сессии/проекта, конфиг Claude Code); wrapper = sandbox_prefix;
  * /bash СНАРУЖИ сессии (main-chat) — на ХОСТЕ, полный доступ (systemctl и т.п.),
    wrapper = None. Это операторская команда (ALLOWED_USER_IDS), не модели.
См. runners/sandbox.py.
"""

from __future__ import annotations

import logging
import os
import pty
import re
import signal
import subprocess
import threading
from pathlib import Path

from .ansi import strip_ansi

logger = logging.getLogger(__name__)

_BUF_CAP = 200_000  # держим только хвост — на случай болтливой команды
# Сколько байт вывода команды держим в памяти для показа. Больше в сообщение
# всё равно не влезет, а полный вывод уезжает в файл (см. OutputCapture).
_TAIL_CAP = 64_000


def clean(raw: bytes) -> bytes:
    """Снять ANSI-раскраску/управляющие коды и \\r — для показа в <pre>.

    Тонкая обёртка над общим ansi.strip_ansi (раньше свой дубликат regex)."""
    return strip_ansi(raw)


class OutputCapture:
    """Перехват вывода ОДНОЙ команды потоком: файл целиком + хвост в памяти.

    Раньше вывод искали заново в кольцевом буфере PTY (_BUF_CAP): каждый опрос
    заново находил метки начала/конца в общем снепшоте. Команда, напечатавшая
    больше размера буфера, вытесняла из него метку начала — оркестратор терял
    точку отсчёта, не находил метку конца и досиживал до таймаута с пустым
    результатом. То есть «полный вывод файлом» отказывал ровно на сборках и
    больших прогонах тестов, ради которых и задумывался.

    Здесь разбор идёт по мере поступления байтов и состояние не переоткрывается:
    увидели метку начала — пишем всё дальнейшее, увидели метку конца — закрылись.
    Сколько бы команда ни напечатала, ничего не теряется.

    Файл заводим ЛЕНИВО — только когда вывод перестал влезать в хвост: иначе
    каждый `ls` оставлял бы за собой файл на диске.
    """

    def __init__(
        self,
        path: Path,
        start_marker: str,
        done_marker: str,
        tail_cap: int = _TAIL_CAP,
    ) -> None:
        self.path = path
        self.tail_cap = tail_cap
        self.done = False
        self._closed = False  # перехват закрыт: новые байты игнорируем
        self._write_failed = False  # запись сорвалась — больше не пытаемся
        # Очередь на запись в файл: наполняется под локом разбора, пишется
        # вне его (см. _flush), чтобы медленный диск не тормозил event loop.
        self._queued: list[bytes] = []
        self._queued_size = 0
        self.code: str | None = None
        self.overflowed = False  # вывод не влез в хвост → есть файл
        self._beg = start_marker.encode()
        self._done_re = re.compile(re.escape(done_marker.encode()) + rb"[ \t]+(\d+)")
        self._done_head = done_marker.encode()
        self._started = False
        # Перевод строки сразу за меткой начала ещё не пришёл (см. _consume).
        self._eat_newline = False
        self._tail = bytearray()
        self._pending = b""  # хвост куска: метка/escape могли разорваться
        self._fh = None
        # Лок разбора: под ним только работа с байтами в памяти. tail() из
        # event loop берёт ЕГО ЖЕ, поэтому держать под ним файловый I/O
        # нельзя — на медленном диске весь оркестратор (все сессии, все
        # адаптеры) вставал бы на время записи каждого куска.
        self._lock = threading.Lock()
        # Отдельный лок файла: его берёт только читающий поток PTY.
        self._file_lock = threading.Lock()

    # Сколько байт может «не хватать» на границе кусков: самая длинная метка
    # плюс запас на escape-последовательность и код возврата.
    def _keep(self) -> int:
        return max(len(self._beg), len(self._done_head)) + 16

    def feed(self, chunk: bytes) -> None:
        """Скормить очередной кусок сырого вывода PTY."""
        with self._lock:
            if self.done or self._closed:
                return
            self._consume(strip_ansi(self._pending + chunk))
        # Запись — уже без лока разбора: под ним ждёт tail() из event loop.
        self._flush()

    @staticmethod
    def _find_own_line(data: bytes, marker: bytes) -> int:
        """Позиция метки, которую НАПЕЧАТАЛ echo, а не отразил ввод.

        При нормальной работе метка приходит один раз: отражение ввода мы
        гасим при старте оболочки (`stty -echo`). Эта проверка — страховка на
        случай, когда эхо всё-таки включено: сам оператор сделал `stty sane`
        или `reset`, либо этого потребовала запущенная программа. Тогда метка
        приезжает дважды, и отличить вывод от эха можно по строке: у эха перед
        меткой стоит приглашение шелла и слово `echo`, у вывода — ничего.
        """
        pos = 0
        while True:
            idx = data.find(marker, pos)
            if idx < 0:
                return -1
            line_start = data.rfind(b"\n", 0, idx) + 1
            prefix = data[line_start:idx]
            # Пусто перед меткой — это её вывод. Иначе строка вида
            # `…$ echo __BEG__` или `echo __BEG__` — отражённый ввод.
            if not prefix.strip():
                return idx
            pos = idx + len(marker)

    def _echo_line_start(self, data: bytes, head: int) -> int:
        """Где кончается вывод команды перед меткой конца в позиции head.

        Строка над меткой — это эхо `…$ echo __DONE__ $?`, которое bash
        отразил при вводе. Она часть служебного протокола, а не вывода, и в
        файл попадать не должна. Если такой строки нет (вывод и метка пришли
        вплотную), режем прямо по началу строки метки.
        """
        line_start = data.rfind(b"\n", 0, head)
        line_start = line_start + 1 if line_start >= 0 else 0
        prev_end = data.rfind(b"\n", 0, max(line_start - 1, 0))
        prev_start = prev_end + 1 if prev_end >= 0 else 0
        prev_line = data[prev_start:line_start]
        if self._done_head in prev_line:
            return prev_start  # это эхо — отрезаем вместе с ним
        return line_start

    def _consume(self, data: bytes) -> None:
        """Разобрать накопленное; неразобранный хвост оставить в _pending.

        Хвост придерживаем потому, что метка могла НАЧАТЬСЯ в конце куска и
        продолжиться в следующем: разобрав его сразу, мы записали бы половину
        метки в вывод, а вторую половину не узнали бы никогда.
        """
        keep = self._keep()
        if not self._started:
            # Ищем метку, стоящую одна на строке, — это её вывод (см.
            # _find_own_line: страховка на случай включённого эха).
            idx = self._find_own_line(data, self._beg)
            if idx < 0:
                # Метки ещё нет — до неё идут приглашение шелла и эхо команды,
                # и это не вывод. Держим только хвост: в нём метка могла начаться.
                self._pending = data[-keep:] if len(data) > keep else data
                return
            self._started = True
            # Всё до метки — приглашение шелла и эхо команды; в вывод не идёт.
            # Записанного ещё нет: до старта мы ничего не пишем.
            data = data[idx + len(self._beg):]
            # Перевод строки после метки мог ещё не прийти (побайтовый ввод) —
            # тогда снимем его при следующем куске, иначе он утёк бы в вывод
            # пустой первой строкой.
            self._eat_newline = True
        if self._eat_newline and data:
            if data.startswith(b"\n"):
                data = data[1:]
            self._eat_newline = False
        # Метка конца тоже приходит дважды: сначала эхом строки
        # `echo __DONE__ $?` (там `$?` — не цифра, поэтому под _done_re оно не
        # подходит), потом её выводом с настоящим кодом. Вывод команды режем по
        # ПЕРВОМУ вхождению метки, до начала её строки: иначе в файл попадали
        # бы приглашение шелла и текст echo-строки.
        head = self._find_own_line(data, self._done_head)
        if head >= 0:
            # Вывод команды кончается ДО эха строки `echo __DONE__ $?`, а не до
            # самой метки: между ними стоит приглашение шелла с этим эхом, и
            # по началу строки метки оно утекло бы в файл последней строкой.
            body_end = self._echo_line_start(data, head)
            m = self._done_re.search(data[head:])
            if m:
                self._write(data[:body_end])
                self.code = m.group(1).decode()
                self.done = True
                self._pending = b""
                return
            # Код возврата ещё не пришёл — записываем тело, а строку с меткой
            # придерживаем до следующего куска. Придержанное ОБРЕЗАЕМ: если
            # метку съела интерактивная программа (`cat`, `ssh`) и отразила
            # её без раскрытия `$?`, кода не будет никогда, и без обрезки
            # весь дальнейший вывод копился бы в памяти вместо файла.
            self._write(data[:body_end])
            rest = data[body_end:]
            if len(rest) > keep:
                self._write(rest[:-keep])
                self._pending = rest[-keep:]
            else:
                self._pending = rest
            return
        if len(data) > keep:
            self._write(data[:-keep])
            self._pending = data[-keep:]
        else:
            self._pending = data

    def _write(self, data: bytes) -> None:
        """Учесть кусок вывода: в хвост сразу, в файл — отложенной очередью.

        Сам файл пишет _flush вне лока разбора: под ним ждёт tail() из event
        loop, и запись на медленном диске застопорила бы весь оркестратор.
        """
        if not data:
            return
        # Всё, что прошло разбор, копим на запись — очередь и решает, писать
        # ли файл: как только суммарный вывод перестал влезать в хвост, она
        # уезжает на диск целиком, с самого начала.
        self._queued.append(data)
        self._queued_size += len(data)
        self._tail.extend(data)
        if len(self._tail) > self.tail_cap:
            del self._tail[: len(self._tail) - self.tail_cap]

    def _flush(self) -> None:
        """Слить накопленное в файл. Зовётся ВНЕ лока разбора (из feed/close)."""
        with self._file_lock:
            with self._lock:
                if self._write_failed or not self._queued or (
                    self._fh is None and self._queued_size <= self.tail_cap
                ):
                    return  # вывод пока влезает в сообщение — файл не нужен
                queued, self._queued = self._queued, []
                self._queued_size = 0
            if self._fh is None:
                self._open_file()
                if self._fh is None:
                    return
            try:
                for part in queued:
                    self._fh.write(part)
            except OSError as e:
                # Диск кончился/файл пропал: сдаёмся НАСОВСЕМ. Без флага
                # следующий же кусок открыл бы файл заново (обнулив его), и
                # так на каждом куске — тысячи попыток в секунду, лог в
                # варнингах и добитый раздел.
                logger.warning("Вывод команды не пишется в файл: %s", e)
                self._close_file()
                with self._lock:
                    self._write_failed = True
                    self.overflowed = False  # файла с полным выводом нет

    def _open_file(self) -> None:
        """Завести файл под полный вывод (очередь запишет _flush)."""
        try:
            # Папку сессии модель видит на запись и может подложить туда
            # симлинк — тогда и запись, и последующая чистка ушли бы наружу,
            # удаляя чужие bash_*.txt. Симлинк не наш: отказываемся.
            if self.path.parent.is_symlink():
                raise OSError(f"{self.path.parent} — симлинк, отказ записи вывода")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("wb")
            self.overflowed = True
        except OSError as e:
            # Диск полон / нет прав — команда уже отработала, терять её вывод
            # целиком из-за файла нельзя: продолжаем с одним хвостом.
            logger.warning("Не удалось завести файл вывода %s: %s", self.path, e)
            self._fh = None
            self._write_failed = True  # не долбимся на каждом куске

    def _close_file(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def tail(self) -> bytes:
        """Последние байты вывода — то, что показываем в сообщении.

        Придержанный хвост тоже показываем: он ещё может оказаться началом
        метки, но чаще это обычный текст, и без него показ отставал бы от
        команды на десяток байт — заметно на медленном выводе построчно.
        """
        with self._lock:
            if self.done or not self._started:
                return bytes(self._tail)
            shown = bytes(self._tail) + self._pending
            return shown[-self.tail_cap:] if len(shown) > self.tail_cap else shown

    def close(self) -> None:
        with self._lock:
            # Придержанный хвост уже без продолжения — отдаём как есть.
            if self._pending and not self._closed:
                pending, self._pending = self._pending, b""
                self._consume(pending)
                self._pending = b""
            # ВАЖНО: закрыться раньше, чем читающий поток отцепят. Между
            # close() и attach_capture(None) убежавшая команда продолжает
            # лить байты, и без этого флага следующий feed завёл бы файл
            # заново поверх готового — оператор получил бы вместо полного
            # лога последние 64 КБ. Ровно тот отказ, который чинит ветка.
            self._closed = True
        # Дописываем остаток очереди и закрываем файл — снова вне лока разбора.
        self._flush()
        with self._file_lock:
            self._close_file()


class BashSession:
    """Один PTY с интерактивным bash и потоком, дренирующим его вывод."""

    def __init__(self, cwd: Path, wrapper: list[str] | None = None):
        self.cwd = cwd
        self.busy = False  # активен ли /bash (для /bashin — не проверяется)
        self._buf = bytearray()
        self._lock = threading.Lock()
        # Перехват вывода текущей команды (см. attach_capture/_reader).
        self._capture: "OutputCapture | None" = None
        master, slave = pty.openpty()
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        # wrapper — префикс песочницы (bwrap … --), пусто если SANDBOX=off.
        argv = [*(wrapper or []), "/bin/bash", "-i"]
        try:
            # stderr → /dev/null ТОЛЬКО на подъём: под bwrap (--unshare-pid)
            # bash не может стать foreground process group терминала (TTY —
            # снаружи pid-namespace, TIOCSPGRP через границу не работает —
            # ограничение ядра, не наше), и на КАЖДОМ старте печатает в
            # stderr «не удаётся задать группу процесса терминала» + подсказку
            # sudo. Само предупреждение безвредно (job control нам не нужен,
            # Ctrl-C работает через отдельный сигнальный путь TTY — не через
            # process group), но пугает оператора в /bashin и сыром выводе
            # /term. Первая же команда ниже (exec 2>&1) возвращает stderr в
            # общий с stdout канал для ВСЕХ последующих команд оператора —
            # их собственные ошибки (`cat: файл не найден` и т.п.) видны как
            # прежде, глушится только этот стартовый баннер самого bash.
            self.proc = subprocess.Popen(
                argv,
                stdin=slave, stdout=slave, stderr=subprocess.DEVNULL,
                cwd=str(cwd), env=env, start_new_session=True,
            )
        finally:
            os.close(slave)
        self.master = master
        threading.Thread(target=self._reader, name="bashshell-reader", daemon=True).start()
        # Гасим отражение ввода и приглашение: иначе в поток вывода лезут
        # `…$ echo __BEG__` и сами приглашения, которые пришлось бы отличать
        # от настоящего вывода эвристиками — а они рвутся на границах кусков
        # PTY (замерено живьём: приглашение приезжает отдельным чтением).
        # Оператору это не видно: он и так видит только вывод своей команды.
        # `exec 2>&1` первой строкой — ДО stty: возвращает stderr процесса в
        # PTY (см. комментарий у Popen выше), иначе оболочка навсегда
        # осталась бы без стандартной ошибки для всех команд оператора.
        self.write("exec 2>&1\nstty -echo 2>/dev/null; PS1=''; PS2=''\n")

    def _reader(self) -> None:
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._buf.extend(chunk)
                if len(self._buf) > _BUF_CAP:
                    del self._buf[:-_BUF_CAP]
                capture = self._capture
            # Перехват кормим ЗДЕСЬ, а не из snapshot(): буфер выше кольцевой,
            # и вывод больше _BUF_CAP вытесняет из него метку начала — читатель
            # снепшотов терял бы точку отсчёта ровно на больших выводах, ради
            # которых перехват и заведён. Вне лока: feed пишет в файл (I/O),
            # держать под ним общий буфер значило бы тормозить дренаж PTY.
            if capture is not None:
                try:
                    capture.feed(chunk)
                except Exception:
                    logger.exception("Перехват вывода команды упал")

    def attach_capture(self, capture: "OutputCapture | None") -> None:
        """Подключить перехват вывода текущей команды (None — отключить)."""
        with self._lock:
            self._capture = capture

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    def write(self, text: str) -> None:
        # os.write на PTY может записать не всё за раз (буфер строки заполнен) —
        # длинная команда (>~4КБ) иначе обрежется и исказится. Дописываем хвост.
        data = text.encode()
        while data:
            try:
                n = os.write(self.master, data)
            except OSError:
                return
            data = data[n:]

    def interrupt(self) -> None:
        """Послать Ctrl-C (SIGINT фоновому пайплайну bash) — прервать убежавшую
        команду, не убивая саму оболочку. Используется при таймауте /bash,
        чтобы процесс не гадил в общий буфер следующему вызову."""
        try:
            os.write(self.master, b"\x03")
        except OSError:
            pass

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        # Интерактивный bash -i игнорирует SIGTERM — terminate() не дорабатывал,
        # и оболочка утекала (REVIEW.md B1). Поэтому SIGKILL по всей группе
        # процессов (bash + её дети: sleep/сборки/...). start_new_session=True
        # делает bash лидером группы, killpg(pid) накрывает её целиком и не
        # задевает процесс бота (другая сессия). Близко к мгновенному, без
        # долгого ожидания — close не stall-ит event loop из хендлеров.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=3)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, OSError):
            try:
                self.proc.kill()
                self.proc.wait(timeout=1)
            except Exception:
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


class BashShellManager:
    """ключ -> BashSession. Одна оболочка на ключ, создаётся лениво.

    Ключи даёт ядро (OrchestratorCore.bash_key): "s:<имя-сессии>:<scope>" для
    оболочек сессии (scope — контекст адаптера) и "main:<scope>" вне сессий.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, BashSession] = {}

    def get_or_create(
        self, key: str, cwd: Path, wrapper: list[str] | None = None
    ) -> BashSession:
        sess = self._sessions.get(key)
        if sess is None or not sess.running:
            if sess is not None:
                sess.close()  # умершую оболочку закрыть — иначе утечёт master fd
            sess = BashSession(cwd, wrapper)
            self._sessions[key] = sess
            logger.info("bash-терминал открыт (%s, cwd %s)", key, cwd)
        return sess

    def get(self, key: str) -> BashSession | None:
        sess = self._sessions.get(key)
        return sess if sess is not None and sess.running else None

    def close(self, key: str) -> None:
        sess = self._sessions.pop(key, None)
        if sess is not None:
            sess.close()
            logger.info("bash-терминал закрыт (%s)", key)

    def close_for_session(self, name: str) -> None:
        """Закрыть все оболочки сессии (все адаптеры) — при её остановке."""
        for key in [k for k in self._sessions if k.startswith(f"s:{name}:")]:
            self.close(key)

    def close_all(self) -> None:
        """Прибрать все bash-оболочки (остановка оркестратора)."""
        for key in list(self._sessions):
            self.close(key)
