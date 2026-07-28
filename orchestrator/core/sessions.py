"""Управление сессиями Claude Code: запуск, останов, мониторинг процессов.

Схема одной сессии:

    claude (CLI, интерактивный, под PTY) ──спавнит по .mcp.json──> channel_server.py
    launcher ──HTTP POST /notify──> channel_server ──JSON-RPC push──> claude

channel_server запускается ТОЛЬКО самим Claude Code (через .mcp.json) —
иначе два процесса дерутся за один порт.

Все операции жизненного цикла одной сессии (create/close/resume/clear/
set_model) сериализованы её локом `Session.ops` — параллельные команды
не могут запустить два процесса или осиротить один из них.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shutil
import socket
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Awaitable, Callable

import aiohttp

from . import hookscript
from . import resources
from . import transcript
from .ansi import strip_ansi
from .proctree import proc_tree_signals
from .slug import slugify  # реэкспорт: адаптеры и тесты ждут sessions.slugify
from .. import runners as runner_mod
from ..config import Config
from ..modules.docker.proxy import DockerProxy

# Launch-механика вынесена в автономный пакет box/ (Слой 2 редизайна,
# docs/ARCHITECTURE-claude-box.md §5/§11). Реэкспорт для обратной
# совместимости: код/тесты ссылаются на sessions._DIALOGS/_DialogAnswerer/
# _ReadyDeadline/READY_* как раньше. Ноль изменений поведения.
from box import transcript_path as box_transcript
from box.dialog import _DialogAnswerer, _DIALOGS  # noqa: F401
from box.launch import launch as box_launch
# open_pty/start_driver больше не зовутся здесь напрямую (спавн+драйвер собраны
# в box.launch), но реэкспортятся для обратной совместимости кода/тестов.
from box.pty import open_pty, start_driver  # noqa: F401
from box.ready import (  # noqa: F401
    READY_SILENCE_SEC,
    READY_TIMEOUT_MAX,
    _ReadyDeadline,
)

logger = logging.getLogger(__name__)

# Каталог пакета orchestrator/ (channel_server.py) и корень репозитория
# (.venv, RO-бинд песочницы) — этот модуль лежит в orchestrator/core/.
PKG_DIR = Path(__file__).resolve().parent.parent
ROOT = PKG_DIR.parent

# READY_SILENCE_SEC/READY_TIMEOUT_MAX переехали в box.ready (реэкспорт выше) —
# они неотделимы от _ReadyDeadline. Ниже — launch-timing, который читают методы
# SessionManager (resume/send); эти методы пока живут здесь, поэтому и константы
# остаются в sessions.py до соответствующих срезов.
# Пауза после resume: «claude --resume» без транскрипта умирает не сразу,
# а через несколько секунд после старта.
RESUME_GRACE = 5.0
# Ретрай доставки в channel-сервер, пока он ещё поднимается. Гонка: сообщение №2
# (media group / быстрый повтор) видит session.running=True сразу после старта
# процесса claude, но channel-сервер (MCP-подпроцесс) на порту ещё не слушает →
# ConnectionRefused. Короткий ретрай закрывает стартовое окно (обычно 1-3с).
SEND_RETRY_TIMEOUT = 20.0


class SessionError(Exception):
    """Ошибка создания/работы сессией — текст показывается пользователю."""


@dataclass
class _EngineProbe:
    """Заглушка «сессии» для запроса раннера по имени движка (см. runner_for)."""
    sandbox: str


# _DIALOGS/_DialogAnswerer, _ReadyDeadline/READY_* и ядро PTY-запуска (open_pty/
# start_driver — openpty+winsize и поток-драйвер) переехали в пакет box/
# (box.dialog, box.ready, box.pty) — самодостаточные launch-хелперы без
# зависимостей от SessionManager. _DialogAnswerer/_ReadyDeadline/READY_*
# реэкспортированы в шапке модуля для обратной совместимости. Драйвер box'а
# отдаёт вывод через колбэк on_output — оркестратор пишет его в claude.log
# (см. _start_claude), а «куда/как поднят процесс» остаётся здесь же.


@dataclass
class Session:
    name: str  # slug: первичный ключ, папка, MCP-ключ, хук — только [A-Za-z0-9_-]
    port: int
    session_dir: Path
    claude_session_id: str
    title: str = ""  # отображаемое имя (топик, сообщения); по умолчанию = name
    # Адреса сессии в транспорт-адаптерах: имя адаптера -> непрозрачная строка
    # (Telegram: id форум-топика). Заполняется ядром через Transport.bind_session.
    bindings: dict[str, str] = field(default_factory=dict)
    linked_path: str | None = None
    model: str | None = None  # None = модель Claude Code по умолчанию
    # Движок изоляции ЭТОЙ сессии (флаг `--box` у /new): "bwrap" | "agent-vm" |
    # "off". None = как в .env (SANDBOX). Выбирается при создании и живёт с
    # сессией: resume/clear поднимают её тем же движком, что и первый старт.
    sandbox: str | None = None
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    process: asyncio.subprocess.Process | None = None
    pty_master: int | None = None
    log_file: IO[bytes] | None = None
    watcher: asyncio.Task | None = None
    # Авто-ответчик стартовых диалогов; глушится, как только канал ответил
    # на /ping (дальше вывод — беседа, писать в stdin нельзя).
    dialog_answerer: "_DialogAnswerer | None" = None
    # Снимок фоновых задач/кронов харнесса из последнего Stop-payload
    # (авторитетно: {id,type,status,description,command}). Для прозрачности —
    # что модель оставила крутиться в фоне; см. app.handle_stop_event / bg_text.
    background_tasks: list = field(default_factory=list)
    session_crons: list = field(default_factory=list)
    # id фоновых задач, о которых оператора уже уведомили (дедуп, без спама).
    bg_seen: set = field(default_factory=set)
    # Сериализация операций жизненного цикла этой сессии.
    ops: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        if not self.title:
            self.title = self.name

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None


class SessionManager:
    """Хранилище активных сессий + жизненный цикл процессов Claude."""

    def __init__(self, config: Config):
        self.config = config
        self._lock = asyncio.Lock()  # защищает _by_name и выдачу портов
        self._by_name: dict[str, Session] = {}
        # Порты, выданные стартующим сессиям, но ещё не занятые их channel-
        # сервером (окно гонки при фиксированном пуле) — см. _find_free_port.
        self._inflight_ports: set[int] = set()
        # Имена сессий, которым слот уже выдан, но процесс ещё поднимается: от
        # проверки лимита до появления session.process живых сессий по count()
        # меньше, чем реально стартует, и два параллельных подъёма (create и/или
        # resume разных сессий) прошли бы лимит оба. Резерв снимается, когда
        # процесс поднялся или старт провалился — см. _guard_limit/_release_slot.
        self._starting: set[str] = set()
        # Базовый CPU-отсчёт дерева процессов для вотчдога (см. is_busy).
        self._cpu: dict[str, int] = {}
        # Раннеры по имени движка изоляции (сессия выбирает свой — см.
        # engine_of/runner_for); ленивые, без per-session состояния.
        self._runners: dict[str, runner_mod.Runner] = {}
        # Общий HTTP-пул к channel-серверам (keep-alive, без сессии на запрос —
        # REVIEW.md E1). Ленивый: создаётся в event loop при первом обращении.
        self._http: aiohttp.ClientSession | None = None
        # Вызывается при внезапной смерти Claude (session, exit_code);
        # назначается в launcher.
        self.on_dead: Callable[[Session, int], Awaitable[None]] | None = None
        # Модули дописывают env для процесса claude (напр. wallet: $NAME для
        # секретов — маркер/значение). Синхронные, session -> {ИМЯ: значение}.
        self.env_hooks: list[Callable[[Session], dict[str, str]]] = []
        # Модули добавляют каталоги в НАЧАЛО PATH песочницы (напр. wallet:
        # обёртки-шлюз gh/git/curl). Синхронные, session -> [каталоги].
        self.path_hooks: list[Callable[[Session], list[str]]] = []
        # Модули поднимают ресурсы, чей результат нужен env/PATH процесса ДО его
        # старта (напр. wallet: per-session MITM-прокси — HTTPS_PROXY зависит от
        # выданного порта). Асинхронные, session -> None; выполняются в начале
        # _start_claude (перед сборкой env), поэтому env_hooks уже видят порт.
        # Гоняются на КАЖДОМ старте (create/resume/clear/set_model), чтобы прокси
        # переустанавливался при возобновлении. Падение хука не роняет запуск.
        self.launch_hooks: list[Callable[[Session], Awaitable[None]]] = []
        # Модули узнают об УДАЛЕНИИ сессии (напр. wallet: отзыв токена демона +
        # снятие её прокси, иначе токен/прокси удалённой сессии остались бы
        # живыми). По имени; синхронные ИЛИ корутины — delete их дожидается
        # (детерминированный teardown: пересоздание сессии не гонится со стопом).
        self.session_delete_hooks: list[Callable[[str], Awaitable[None] | None]] = []
        # Per-session docker-прокси (SANDBOX_BWRAP_DOCKER): у каждой сессии свой,
        # allowlist = только её проект. Живёт, пока жив процесс сессии.
        self._docker_proxies: dict[str, "DockerProxy"] = {}

    def _http_session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self._http

    def _channel_headers(self) -> dict[str, str]:
        """Auth на эндпоинты channel-сервера (/notify /permission /ping) —
        симметрично ORCH_TOKEN на стороне оркестратора: без него локальный
        процесс мог бы POST /notify и вбросить промпт в Claude или POST
        /permission behavior=allow и авто-разрешить запрос."""
        return {"Authorization": f"Bearer {self.config.orch_token}"}

    # ── состояние на диске ──────────────────────────────────────
    # Записи переживают /close_session и рестарт launcher'а: топик остаётся,
    # сессия возобновляется по первому сообщению (resume).

    def save_state(self) -> None:
        items = [
            {
                "name": s.name,
                "bindings": s.bindings,
                "cwd": str(s.session_dir),
                "port": s.port,
                "claude_session_id": s.claude_session_id,
                "title": s.title,
                "linked_path": s.linked_path,
                "model": s.model,
                "sandbox": s.sandbox,
            }
            for s in self._by_name.values()
        ]
        state_file = self.config.sessions_dir / ".sessions.json"
        tmp = state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False))
        os.replace(tmp, state_file)  # атомарно: битого файла не бывает

    def load_state(self) -> None:
        state_file = self.config.sessions_dir / ".sessions.json"
        if not state_file.exists():
            return
        try:
            items = json.loads(state_file.read_text())
        except (OSError, ValueError) as e:
            logger.error("Не удалось прочитать %s: %s", state_file, e)
            return
        for item in items:
            bindings = dict(item.get("bindings") or {})
            session = Session(
                name=item["name"],
                bindings=bindings,
                port=item.get("port", 0),
                session_dir=Path(item["cwd"]),
                claude_session_id=item["claude_session_id"],
                title=item.get("title", ""),
                linked_path=item.get("linked_path"),
                model=item.get("model"),
                # Записи, созданные до флага --box, движка не помнят → None =
                # дефолт из .env (их прежнее поведение).
                sandbox=item.get("sandbox"),
            )
            self._by_name[session.name] = session
            logger.info("Восстановлена запись сессии %s (остановлена)", session.name)

    # ── чтение (без блокировки: единственный поток event loop) ──

    def get(self, name: str) -> Session | None:
        return self._by_name.get(name)

    # Синоним для явности в местах, где имя приходит извне (хуки, URL).
    get_by_name = get

    def list_all(self) -> list[Session]:
        return list(self._by_name.values())

    def count(self) -> int:
        """Сколько сессий РЕАЛЬНО запущено (лимит MAX_INSTANCES — про них).

        Записи остановленных сессий живут в _by_name (топик на месте, resume
        возможен), но ресурсов не занимают: процесса нет. Раньше лимит считал
        все записи, и после рестарта оркестратора, где восстанавливаются ВСЕ
        записи с диска сразу как остановленные, /new упирался в лимит при нуле
        живых процессов. Имена по-прежнему уникальны среди всех записей
        (has_name), так что «слот» занят только процессом, а не именем.
        """
        return sum(1 for s in self._by_name.values() if s.running)

    def total(self) -> int:
        """Сколько записей существует всего — с остановленными."""
        return len(self._by_name)

    def has_name(self, name: str) -> bool:
        return name in self._by_name

    def get_by_binding(self, adapter: str, address: str) -> Session | None:
        """Сессия по адресу в адаптере (Telegram: id топика)."""
        return next(
            (s for s in self._by_name.values() if s.bindings.get(adapter) == address),
            None,
        )

    # ── создание ────────────────────────────────────────────────

    async def create(
        self,
        title: str,
        project_path: str | None = None,
        sandbox: str | None = None,
    ) -> Session:
        """sandbox — движок изоляции ЭТОЙ сессии (флаг `--box`); None = дефолт
        .env. Готовность движка проверяем ЗДЕСЬ, до создания папок и старта:
        на старте оркестратора preflight проходил только дефолтный раннер."""
        slug = slugify(title)
        if sandbox is not None and sandbox != self.config.sandbox:
            self._check_engine(sandbox)
        self.check_resources(sandbox)
        session = Session(
            name=slug,
            port=0,
            session_dir=self.config.sessions_dir / slug,
            claude_session_id=str(uuid.uuid4()),
            title=title,
            sandbox=sandbox,
        )
        async with session.ops:
            # Проверки и регистрация — под общим локом (два /new подряд
            # не создадут дубль имени и не превысят лимит).
            async with self._lock:
                if self.has_name(slug):
                    raise SessionError(f"Сессия «{slug}» уже существует.")
                # count(), не len(_by_name): лимит — про ЖИВЫЕ процессы.
                # Остановленные записи (после /close_session, авто-останова по
                # простою, рестарта оркестратора) слот не держат.
                self._guard_limit(slug)
                self._allocate_port(session)
                self._by_name[slug] = session

            # finally, а не только except: при отмене задачи (CancelledError —
            # не Exception) слот иначе остался бы занятым до перезапуска
            # оркестратора. Освобождение идемпотентно (discard).
            try:
                try:
                    session.session_dir.mkdir(parents=True, exist_ok=True)
                    if project_path:
                        session.linked_path = self._link_project(project_path)
                    self._guard_unique_cwd(session)
                    self._write_configs(session)
                    await self._start_claude(session)
                    await self._wait_ready(session)
                except Exception:
                    await self._terminate(session)
                    self._inflight_ports.discard(session.port)
                    async with self._lock:
                        self._by_name.pop(slug, None)
                    self.save_state()
                    raise
            finally:
                # Успех — сессию дальше считает count(); провал или отмена —
                # слот обязан вернуться, иначе он утечёт до перезапуска.
                self._release_slot(slug)

            self._start_watcher(session)
            self.save_state()
            return session

    def _guard_limit(self, name: str | None = None) -> None:
        """Занять слот под запуск ещё одной сессии. Нет места — SessionError.

        Считаем живые процессы ПЛЮС те, что прямо сейчас поднимаются: между
        проверкой и появлением session.process проходит несколько секунд
        (старт claude, ожидание готовности), и без учёта стартующих два
        параллельных подъёма — /new и resume в соседнем топике — прошли бы
        лимит оба.

        Вызывать под self._lock. Слот освобождает _release_slot: его обязан
        позвать и успешный старт, и любой провал, иначе имя останется
        «вечно стартующим» и съест слот до перезапуска оркестратора.

        Слот уже за этим именем (перезапуск живой сессии — см. _reserve_slot):
        проверять нечего, иначе перезапуск отказывал бы сам себе.
        """
        if name is not None and name in self._starting:
            return
        if self.count() + len(self._starting) >= self.config.max_instances:
            raise SessionError(
                f"Достигнут лимит одновременно запущенных сессий "
                f"({self.config.max_instances}). Останови ненужную "
                f"(/close_session) или подними MAX_INSTANCES в .env."
            )
        if name is not None:
            self._starting.add(name)

    def _reserve_slot(self, name: str) -> None:
        """Придержать слот, который сессии УЖЕ принадлежит, — без проверки.

        Случай — /clear и смена модели на живой сессии: процесс гасится и
        поднимается снова, и в паузе между stop и start count() показывает на
        единицу меньше. Без резерва конкурентный /new успел бы занять
        освободившееся на секунду место, и после рестарта живых стало бы
        больше лимита. Проверять лимит тут НЕЛЬЗЯ: слот и так её, отказ
        означал бы «сессию нельзя перезапустить, потому что она запущена».
        """
        self._starting.add(name)

    def _release_slot(self, name: str) -> None:
        """Снять резерв слота: процесс поднялся (учтётся в count()) или упал.

        БЕЗ self._lock, намеренно: discard не делает await, а event loop у нас
        один — влезть между чтением и записью некому. Зато вызов безопасен из
        finally при отмене задачи, где взятие лока само бросило бы
        CancelledError и слот утёк бы навсегда, срезав лимит до перезапуска.
        """
        self._starting.discard(name)

    def check_resources(self, sandbox: str | None) -> None:
        """Хватит ли машине памяти на ещё одну сессию? Иначе SessionError.

        Зовётся перед КАЖДЫМ подъёмом процесса (create и resume/revive), а не
        только при создании: остановленная сессия ресурсов не занимала, и её
        возобновление для машины — такой же новый процесс, как /new.

        Проверка выключена (MIN_FREE_RAM_MB=0) или память неизвестна — молча
        пропускаем: см. resources.check_memory.
        """
        engine = sandbox if sandbox is not None else self.config.sandbox
        needed = resources.session_cost_mb(engine, self.config.agent_vm_memory_gib)
        verdict = resources.check_memory(
            resources.available_ram_mb(),
            needed,
            self.config.min_free_ram_mb,
        )
        if not verdict.allowed:
            raise SessionError(
                f"Недостаточно свободной памяти: {verdict.reason}. "
                f"Останови ненужную сессию (/close_session) или снизь "
                f"порог MIN_FREE_RAM_MB в .env."
            )

    def _check_engine(self, engine: str) -> None:
        """Движок доступен этому оркестратору и готов к работе? Иначе SessionError
        с причиной (нет бинаря bwrap, agent-vm требует настройки на старте…)."""
        reason = self.config.box_reject_reason(engine)
        if reason is not None:
            raise SessionError(f"Изоляция «{engine}» недоступна: {reason}")
        checked = getattr(self, "_engines_ok", None)
        if checked is None:
            checked = set()
            self._engines_ok = checked
        if engine in checked:
            return
        ok, why = self.runner_for(_EngineProbe(engine)).preflight()
        if not ok:
            raise SessionError(f"Изоляция «{engine}» не готова: {why}")
        checked.add(engine)  # preflight движка — один раз за жизнь процесса

    def _guard_engine(self, session: Session) -> None:
        """Движок сессии всё ещё доступен? Зовётся на КАЖДОМ старте существующей
        сессии (resume/clear), а не только при создании: движок сохранён в
        .sessions.json и может стать недоступен между запусками — оператор
        сменил SANDBOX, и agent-vm остался без своей стартовой обвязки
        (LAN-адрес хоста, guest-адреса). Такая сессия должна получить внятный
        отказ ДО побочных эффектов, а не подняться битой."""
        if session.sandbox is not None and session.sandbox != self.config.sandbox:
            self._check_engine(session.sandbox)

    def _guard_unique_cwd(self, session: Session) -> None:
        """Раннеры с unique_cwd (agent-vm: имя VM = hash(cwd)) не допускают
        двух сессий на один рабочий каталог — вторая молча убила бы VM первой.

        Сравниваем с сессиями ТОГО ЖЕ движка: bwrap-сессия на том же каталоге
        VM не поднимает и мешать не может (движок теперь на сессию, `--box`)."""
        runner = self.runner_for(session)
        if not getattr(runner, "unique_cwd", False):
            return
        engine = self.engine_of(session)
        cwd = self.effective_cwd(session)
        clash = next(
            (
                s for s in self._by_name.values()
                if s is not session
                and self.engine_of(s) == engine
                and self.effective_cwd(s) == cwd
            ),
            None,
        )
        if clash is not None:
            raise SessionError(
                f"Раннер «{runner.name}» допускает одну сессию на каталог: "
                f"{cwd} уже занят сессией «{clash.name}»."
            )

    def _allocate_port(self, session: Session) -> None:
        """Зарезервировать свободный порт под сессию (или отказать). ВЫЗЫВАТЬ ПОД
        `self._lock`: `_find_free_port` помечает порт в `_inflight_ports` — только
        под локом это атомарно относительно конкурентного старта другой сессии.
        Единая точка для create/resume/clear (текст ошибки/логика — в одном месте)."""
        port = self._find_free_port()
        if port is None:
            raise SessionError("Нет свободных портов для channel-сервера.")
        session.port = port

    def _write_configs(self, session: Session) -> None:
        """Записать mcp.json + settings.json перед стартом Claude — общий пролог
        provisioning для create/resume/clear."""
        self._write_mcp_json(session)
        self._write_claude_settings(session)

    def _find_free_port(self) -> int | None:
        lo, hi = self.config.channel_port_start, self.config.channel_port_end
        # Авто-режим (пул не задан): ОС выдаёт свободный порт на localhost.
        if lo <= 0 or hi <= 0:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            self._inflight_ports.add(port)
            return port
        # Фиксированный пул: порт остановленной сессии уже свободен (процесс
        # убит) — учитываем только работающие, иначе resume ложно упадёт.
        # Плюс _inflight_ports: порты сессий, которые СЕЙЧАС стартуют, но ещё
        # не подняли channel-сервер (в этом окне они не running и порт не занят
        # реально) — иначе конкурентный /new выдал бы тот же порт, и сообщения
        # одной сессии ушли бы в channel-сервер другой.
        used = {sess.port for sess in self._by_name.values() if sess.running}
        used |= self._inflight_ports
        for port in range(lo, hi + 1):
            if port in used:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                except OSError:
                    continue
            self._inflight_ports.add(port)  # снимется в _start_watcher/_stop_process
            return port
        return None

    @staticmethod
    def _link_project(project_path: str) -> str:
        """Рабочая директория проекта: claude запускается прямо в ней
        (натуральный cwd — грузит CLAUDE.md/.mcp.json/.claude проекта).
        Несуществующая директория создаётся автоматически.

        `/new` — команда главного чата, поэтому относительный путь резолвим ОТ
        дома пользователя (а не от cwd процесса-оркестратора = репозитория);
        абсолютный путь и `~` — как указано.
        """
        p = Path(project_path).expanduser()
        real_path = (p if p.is_absolute() else Path.home() / p).resolve()
        if real_path.is_file():
            raise SessionError(f"Это файл, а не директория: {project_path}")
        real_path.mkdir(parents=True, exist_ok=True)
        SessionManager._warn_project_trust(real_path)
        return str(real_path)

    @staticmethod
    def _warn_project_trust(project_dir: Path) -> None:
        """Ругнуться в лог, если linked-папка несёт доверяемые при запуске
        настройки. При linked-сессии claude стартует прямо в этой папке и
        АВТО-ДОВЕРЯЕТ ей (стартовый диалог «trust this folder» отвечается Yes,
        см. _DIALOGS) → исполнятся её project-хуки (.claude/settings[.local].json).
        MCP-авто-старт для linked уже снят (см. _write_claude_settings), но хуки
        грузятся через доверие. Не hard-block: модель угроз — страховки от
        случайных глупостей, а папку оператор выбрал сам; но это должно быть видно.
        """
        concerns = []
        for rel in (".claude/settings.json", ".claude/settings.local.json"):
            p = project_dir / rel
            try:
                if p.is_file() and '"hooks"' in p.read_text(errors="replace"):
                    concerns.append(f"{rel} (хуки)")
            except OSError:
                pass
        if (project_dir / ".mcp.json").is_file():
            concerns.append(".mcp.json (MCP; авто-старт снят, но папке доверяем)")
        if concerns:
            logger.warning(
                "Проект %s несёт доверяемые при запуске настройки: %s. "
                "Linked-сессия авто-доверяет папке и исполнит её project-хуки — "
                "линкуй только репозитории, которым доверяешь.",
                project_dir, ", ".join(concerns),
            )

    def _guest_python(self, session: Session) -> str:
        """python для channel_server и хук-диспетчера ВНУТРИ песочницы. Под
        agent-vm — системный `python3` гостя (хостового venv там нет; наш код на
        stdlib, зависимостей не требует), иначе — `sys.executable` (хостовый venv).
        Движок берём у САМОЙ сессии (`--box`), а не из .env."""
        return "python3" if self.engine_of(session) == "agent-vm" else sys.executable

    def _write_mcp_json(self, session: Session) -> None:
        """Конфиг, по которому Claude Code сам запустит channel_server.py.

        Интерпретатор — `_guest_python()` (под agent-vm системный python3 гостя).
        Путь к channel_server.py монтируется в гостя (раннер биндит корень репо
        тем же путём).
        """
        engine = self.engine_of(session)
        mcp = {
            "mcpServers": {
                f"channel-{session.name}": {
                    "command": self._guest_python(session),
                    "args": [str(PKG_DIR / "channel_server.py")],
                    "env": {
                        "CHANNEL_PORT": str(session.port),
                        # Интерфейс bind push-сервера. agent-vm: 0.0.0.0 — иначе
                        # docker-style `--publish` не достаёт loopback гостя; под
                        # bwrap: 127.0.0.1 (общий loopback с хостом).
                        "CHANNEL_HOST": (
                            "0.0.0.0" if engine == "agent-vm" else "127.0.0.1"
                        ),
                        "SESSION_NAME": session.name,
                        # guest-facing: под agent-vm — host-gateway имя (см.
                        # Config.guest_orch_host); под bwrap/off — orch_host.
                        "ORCH_HOST": self.config.guest_orch_host_for(engine),
                        "ORCH_PORT": str(self.config.orch_port),
                        "ORCH_TOKEN": self.config.orch_token,
                    },
                }
            }
        }
        (session.session_dir / ".mcp.json").write_text(json.dumps(mcp, indent=2))

    def _write_claude_settings(self, session: Session) -> None:
        """Настройки бота для headless-запуска. Грузятся через --settings и
        мержатся с профилем (CLAUDE_CONFIG_DIR) и проектом — не заменяют их.

        - enableAllProjectMcpServers: авто-апрув MCP из .mcp.json проекта,
          чтоб твои серверы стартовали без диалога «New MCP server found»
          (на канал из --mcp-config не влияет — он доверенный и так).
          Ставим ТОЛЬКО без linked_path: иначе /new к чужому проекту запустит
          его .mcp.json command без consent → RCE (REVIEW.md S3). Свой cwd
          (папка сессии) проектных .mcp.json не содержит, так что там флаг
          безвреден и сохраняет прежнее поведение;
        - permissions.allow для канал-тулов: только в небайпасных режимах,
          иначе Claude спросит разрешение на каждый ответ в Telegram;
        - PreToolUse-хук (если show_tool_calls): вызовы тулов → POST
          /event/<имя> → статус-бабл;
        - Stop-хук (всегда): конец хода → POST /stop/<имя> с
          last_assistant_message → фолбэк, если ход завершился голым текстом
          вместо reply_to_user (REVIEW: 9/9 длинных ходов в живой сессии
          теряли финал именно так — см. core/app.py handle_stop_event).
          Оба события ловит один скрипт-диспетчер (hookscript.py), никогда не
          блокирует Claude (except+exit 0).
        """
        settings: dict = {}
        settings_dir = session.session_dir / ".claude"
        settings_dir.mkdir(exist_ok=True)
        if session.linked_path is None:
            settings["enableAllProjectMcpServers"] = True
        deny = ["AskUserQuestion"]  # интерактивный вопрос-меню виснет без TUI;
        # Claude получит «tool not allowed» и переспросит через reply_to_user.

        # Анти-утечка секретов имеет смысл ТОЛЬКО в связке кошелёк + песочница —
        # это единая схема, звенья не работают порознь:
        #   • песочница прячет сырые креды хоста (~/.ssh, ~/.config/gh, keyring);
        #   • кошелёк даёт контролируемый доступ к ним (команда на хосте);
        #   • эти deny/autoMode-правила блокируют попытки достать секрет в обход.
        # Без песочницы модель видит все хостовые креды напрямую — правила были
        # бы театром; без кошелька нет и легитимного доступа, который защищаем.
        # Config._wallet_module уже не пускает кошелёк в набор при
        # другой песочнице, но проверку песочницы здесь оставляем осознанно:
        # Config — frozen dataclass без валидации инварианта, а
        # `dataclasses.replace(cfg, sandbox=…)` (так делают тесты) может
        # рассинхронизировать поля. Ошибка была бы тихой в обе стороны — правила
        # либо театр без кошелька, либо отсутствуют при активном. Дёшево.
        wallet_active = (
            "wallet" in self.config.modules and self.engine_of(session) == "bwrap"
        )
        if wallet_active:
            # Чтение кред-файлов/keyring — жёсткий deny (работает во ВСЕХ режимах,
            # включая bypass). `env`/`printenv` глобально НЕ режем (ломает легит
            # `env VAR=x cmd`) — это отдаём судье auto-режима, он умнее glob.
            deny += [
                "Read(~/.config/gh/**)",
                "Read(~/.netrc)",
                "Read(~/.ssh/**)",
                "Read(~/.aws/**)",
                "Read(~/.git-credentials)",
                "Bash(security find-generic-password*)",
                "Bash(pass show*)",
            ]
        perms: dict = {"deny": deny}
        if self.config.permission_mode != "bypass":
            perms["allow"] = [
                f"mcp__channel-{session.name}__reply_to_user",
                f"mcp__channel-{session.name}__send_file_to_user",
            ]
        settings["permissions"] = perms

        # Судья auto-режима — отдельный классификатор (в 2.1.2x по умолчанию
        # Sonnet 5). Его ПРОМПТ не кастомизируется, но кастомизируются ПРАВИЛА,
        # которые он читает. Дополняем их (только в auto + при активном кошельке):
        # жёстко запретить добычу секретов и проверять ВСЕ bash-команды.
        if wallet_active and self.config.permission_mode == "auto":
            settings["autoMode"] = {
                "environment": [
                    "$defaults",
                    "Секреты держатся вне досягаемости модели (кошелёк/keyring): "
                    "~/.config/gh, ~/.netrc, ~/.ssh, ~/.aws и env-переменные с токенами.",
                ],
                "hard_deny": [
                    "$defaults",
                    "Никогда не добывай значения секретов: не дампи переменные "
                    "окружения (env, printenv, set) ради токенов; не читай кред-сторы "
                    "(~/.config/gh, ~/.netrc, ~/.ssh, ~/.aws, keyring); не кодируй "
                    "(base64/hex/reverse) и не пересылай секреты наружу. git/gh/ssh "
                    "зови как обычно — обёртки кошелька сами завернут вызов на хост "
                    "с его кредами (принудительно — wallet run <секрет> -- <команда>). "
                    # Без этой строки запрет выше загонял в тупик: проверить «есть ли
                    # у меня переменная» нечем (env запрещён, классификатор режет), и
                    # отсутствие ДОСТУПА читалось как отсутствие СЕКРЕТА — живой
                    # случай, где модель часами искала несуществующий баг.
                    "Чтобы узнать, ЧТО у тебя есть: `wallet ls` (имена и режимы, без "
                    "значений), а `wallet doctor` покажет, доехали ли переменные "
                    "кошелька в твоё окружение и не затёрты ли они (частая причина — "
                    "`source .env`, где та же переменная пустая). Это разрешённый "
                    "путь проверки: дампить env вместо них не нужно.",
                ],
                "classifyAllShell": self.config.automode_classify_all_shell,
            }

        # Токен уходит в 0600-скрипт (см. hookscript.py), команда хука = только
        # путь к интерпретатору и скрипту — ничего секретного в
        # /proc/<pid>/cmdline и в самом settings.local.json не остаётся.
        hook_script = settings_dir / "hook_dispatch.py"
        hook_script.write_text(
            hookscript.render(
                self.config.guest_orch_host, self.config.orch_port,
                session.name, self.config.orch_token,
            )
        )
        os.chmod(hook_script, 0o600)
        hook_cmd = {
            "type": "command",
            "command": f'"{self._guest_python(session)}" "{hook_script}"',
        }
        hooks: dict = {"Stop": [{"hooks": [hook_cmd]}]}  # Stop не поддерживает matcher
        if self.config.show_tool_calls:
            hooks["PreToolUse"] = [{"matcher": "", "hooks": [hook_cmd]}]
            # Завершение вызова (bash: ✓/✗ + время) и сабагента; тот же
            # диспетчер — ядро роутит по hook_event_name (handle_tool_event).
            hooks["PostToolUse"] = [{"matcher": "", "hooks": [hook_cmd]}]
            hooks["SubagentStop"] = [{"matcher": "", "hooks": [hook_cmd]}]
        settings["hooks"] = hooks

        # CLAUDE_ENV_* под agent-vm иначе ПРОПАДАЮТ: мы задаём их в env процесса,
        # а claude живёт в госте, куда env не течёт (замерено: в госте
        # ANTHROPIC_BASE_URL=unset). Оператор при этом уверен, что его настройка
        # в деле. Файл настроек в гостя монтируется, а блок `env` читает САМ
        # клиент (проверено живьём), поэтому доставляем их так. Под bwrap/off
        # оставляем env процесса — проверенный путь, не трогаем.
        if self.engine_of(session) == "agent-vm" and self.config.claude_env:
            settings["env"] = dict(self.config.claude_env)
        settings_file = settings_dir / "settings.local.json"
        # 0600: в env-блоке может лежать токен прокси оператора. От МОДЕЛИ он
        # этим не закрыт (в госте она root и читает свой каталог — ровно как
        # под bwrap читает env процесса; для секретов, которые модель видеть не
        # должна, есть кошелёк), но от других пользователей хоста — да, и файл
        # не должен быть group/other-readable, как соседний hook_dispatch.py.
        fd = os.open(settings_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(settings, indent=2))
        os.chmod(settings_file, 0o600)

    async def _start_claude(self, session: Session, resume: bool = False) -> None:
        """Запустить интерактивный claude под PTY.

        Headless-запуск не работает: без TTY claude сваливается в --print,
        а в -p/stream-json режиме channel-события не запускают ход (проверено
        вживую). Интерактивная сессия под PTY — документированный сценарий
        «persistent terminal»: пуш в канал сам будит Claude.
        """
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        # Под bwrap по умолчанию вырезаем доступ к X/Wayland: сеть у песочницы
        # общая с хостом, поэтому абстрактный сокет X-сервера (@/tmp/.X11-unix/X0)
        # достижим даже при tmpfs /tmp — с $DISPLAY процесс в песочнице мог бы
        # дёрнуть хостовый GUI (диалоги askpass, скриншоты, ввод). CLI-режиму X не
        # нужен; убираем переменные, чтобы клиенты не знали, куда подключаться.
        # SANDBOX_X11=1 оставляет X (если модели он реально нужен).
        engine = self.engine_of(session)
        if engine == "bwrap" and not self.config.sandbox_x11:
            for var in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY"):
                env.pop(var, None)
        # Пре-старт ресурсов, чей результат нужен env/PATH ниже (wallet: подъём
        # per-session прокси перехвата, чтобы session_env увидел его порт).
        # Выполняется ДО сборки PATH/env. Падение хука не должно ронять запуск —
        # логируем и продолжаем (сессия поднимется, просто без этой обвязки).
        for hook in self.launch_hooks:
            try:
                await hook(session)
            except Exception:
                logger.exception("launch_hook для сессии %s", session.name)
        # CLI-обвязка оркестратора (bin/wallet и т.п.): репозиторий RO-виден
        # и в песочнице, поэтому PATH работает и там. Модульные path_hooks
        # (напр. обёртки-шлюз кошелька) кладём ещё раньше — они должны побеждать
        # настоящие бинари. Каталоги под session_home появляются/наполняются из
        # session_hooks уже после старта, но bind-смонтированы живыми.
        prepend = [str(ROOT / "bin")]
        for hook in self.path_hooks:
            try:
                prepend = [*hook(session), *prepend]
            except Exception:
                logger.exception("path_hook для сессии %s", session.name)
        env["PATH"] = ":".join([*prepend, env.get("PATH", "")])
        if self.config.claude_config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.config.claude_config_dir)
        # Явные переменные для Claude Code (CLAUDE_ENV_ANTHROPIC_BASE_URL=…
        # и т.п.). Сами CLAUDE_ENV_* в дочерний процесс не тащим.
        for key in [k for k in env if k.startswith("CLAUDE_ENV_")]:
            del env[key]
        # AGENT_VM_MEMORY_GIB/CPUS — env-алиасы флагов самого agent-vm, а мы их
        # уже прочитали (config) и превратили во флаги (раннер). Оставлять их в
        # окружении ребёнка значит держать второй источник истины: наш разбор
        # («0/пусто = не задано, флаг не эмитим») агент-vm бы не увидел и взял
        # значение сам. Один источник — наш.
        if engine == "agent-vm":
            from ..runners.agentvm import strip_own_env
            strip_own_env(env)
        env.update(self.config.claude_env)
        # Модульные env-вклады (wallet: $NAME для секретов). Env процесса claude
        # наследуют его Bash-тул и сервисы, что он запускает — значит переменная
        # доступна там, где сервис/команда её читает.
        for hook in self.env_hooks:
            try:
                env.update(hook(session))
            except Exception:
                logger.exception("env_hook для сессии %s", session.name)

        # --session-id обязан быть UUID; --resume продолжает прежний диалог.
        session_arg = (
            ["--resume", session.claude_session_id]
            if resume
            else [f"--session-id={session.claude_session_id}"]
        )
        # Модель: /model на сессию → DEFAULT_MODEL из .env → дефолт Claude.
        # Синонимы (opus/sonnet/haiku/…) и полные имена мапит сам Claude Code.
        model = session.model or self.config.default_model
        if model:
            session_arg += ["--model", model]
        # Effort по умолчанию из .env (low/medium/high/xhigh/max).
        if self.config.default_effort:
            session_arg += ["--effort", self.config.default_effort]
        # Режим разрешений: bypass — без ограничений; остальные режимы
        # спрашивают, запросы прилетают в Telegram (permission relay).
        if self.config.permission_mode == "bypass":
            session_arg += ["--dangerously-skip-permissions"]
        else:
            session_arg += ["--permission-mode", self.config.permission_mode]

        self._rotate_log(session.session_dir / "claude.log")
        session.log_file = open(session.session_dir / "claude.log", "ab")
        # box-драйвер отдаёт вывод claude через колбэк — пишем его в лог сессии.
        # Захватываем конкретный log_file (не session.log_file): при рестарте
        # сессии там окажется новый файл, а этот драйвер владеет прежним PTY и
        # должен писать в прежний лог, как и раньше. ValueError глотаем — лог мог
        # закрыться при остановке сессии (драйвер ещё дочитывает буфер PTY).
        log_file = session.log_file

        def _on_output(chunk: bytes) -> None:
            try:
                log_file.write(chunk)
                log_file.flush()
            except ValueError:  # лог уже закрыт при остановке сессии
                pass

        try:
            # cwd = папка проекта (если задан линк): натуральное поведение —
            # Claude грузит CLAUDE.md/.mcp.json/.claude проекта. Канал-сервер
            # и настройки бота подсасываем флагами ниже (consent не просят).
            cwd = str(self.effective_cwd(session))
            # Подсказка: под bwrap $HOME процесса подменён приватным домом
            # сессии, поэтому реальный ~/.venv и глобальные инструменты не видны —
            # окружение проекта держи В ПРОЕКТЕ (он смонтирован RW). Персистентный
            # дом (.homes/<имя>) переживает рестарты, если агент ставит в ~.
            if engine == "bwrap" and session.linked_path:
                logger.info(
                    "Сессия %s: под bwrap $HOME изолирован (реальный ~/.venv не "
                    "виден) — держи окружение в проекте %s (RW) или в ~ сессии "
                    "(персистентный дом)", session.name, session.linked_path,
                )
            extra: list[str] = []
            mcp_json = session.session_dir / ".mcp.json"
            if mcp_json.exists():
                extra += ["--mcp-config", str(mcp_json)]
            settings_file = session.session_dir / ".claude" / "settings.local.json"
            if settings_file.exists():
                extra += ["--settings", str(settings_file)]
            # dev-записи каналов передаются --dangerously-load-development-channels
            # (server:<имя>; определено в .mcp.json из --mcp-config).
            # Изоляция — через раннер (runner.py): при bwrap claude и все его
            # дети (channel_server, хуки, Bash-тул) заперты в mount-namespace —
            # видны только папка сессии, папка проекта и конфиг Claude Code.
            extra_rw = [session.session_dir, Path(cwd)]
            # Per-session docker-прокси (если включён) — сокет биндится в песочницу.
            docker_sock = await self._ensure_docker_proxy(session)
            argv = self.runner_for(session).wrap(
                [
                    self.config.claude_bin,
                    *session_arg,
                    *extra,
                    "--dangerously-load-development-channels",
                    f"server:channel-{session.name}",
                ],
                chdir=Path(cwd),
                extra_rw=extra_rw,
                home_dir=self.session_home(session),
                publish_ports=[session.port],
                docker_sock=docker_sock,
            )
            # Спавн процесса под PTY + запуск драйвера (дренаж вывода + авто-ответы
            # на стартовые диалоги) собран в box.launch: он открывает PTY нужного
            # размера (иначе winsize = 0×0 и claude мусорит зондами CPR), спавнит
            # процесс на slave и владеет master-fd (закроет сам). Оркестратор даёт
            # готовые argv/env/cwd и колбэк вывода, забирает ручки из handle.
            # Сбой спавна launch чистит свои fd (master/slave); лог закрываем мы.
            handle = await box_launch(
                argv, cwd=cwd, env=env, on_output=_on_output, name=session.name
            )
        except Exception:
            self._close_log(session)
            raise
        session.process = handle.process
        session.pty_master = handle.pty_master
        session.dialog_answerer = handle.answerer

    def _rotate_log(self, path: Path) -> None:
        """Если лог перерос лимит — сдвинуть в .old (одна копия), начать заново."""
        cap = int(self.config.log_max_mb * 1024 * 1024)
        try:
            if cap > 0 and path.exists() and path.stat().st_size > cap:
                os.replace(path, path.with_suffix(".log.old"))
        except OSError as e:
            logger.warning("Ротация лога %s не удалась: %s", path, e)

    async def _wait_ready(self, session: Session) -> None:
        """Готовность = channel-сервер отвечает на /ping (его поднял Claude)."""
        loop = asyncio.get_running_loop()
        # Запас сверх окна тишины нужен только agent-vm: там первый запуск
        # тянет OCI-образ минутами. Под bwrap/off — как раньше, без запаса.
        vm = self.engine_of(session) == "agent-vm"
        cap = READY_TIMEOUT_MAX if vm else READY_SILENCE_SEC
        deadline = _ReadyDeadline(started_at=loop.time(), cap=cap)
        log_path = session.session_dir / "claude.log"
        http = self._http_session()
        ping_timeout = aiohttp.ClientTimeout(total=2)
        while True:
            proc = session.process
            if proc is None or proc.returncode is not None:
                code = proc.returncode if proc else "?"
                raise SessionError(
                    f"Claude завершился при старте (код {code}). "
                    f"Лог: {session.session_dir / 'claude.log'}"
                )
            try:
                async with http.get(
                    f"http://127.0.0.1:{session.port}/ping",
                    timeout=ping_timeout,
                    headers=self._channel_headers(),
                ) as resp:
                    if resp.status == 200:
                        # Канал поднят ⇒ Claude стартовал, стартовые диалоги
                        # позади. Глушим авто-ответчик: дальше любой матч
                        # маркера — это уже текст беседы, и нажатие клавиши
                        # ушло бы спурьёзным сообщением от лица пользователя.
                        if session.dialog_answerer is not None:
                            session.dialog_answerer.stop()
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            # Признак жизни: лог растёт (под agent-vm — прогресс загрузки
            # образа и бута VM, под bwrap — вывод Claude). Пока растёт, ждём.
            try:
                deadline.note_progress(log_path.stat().st_size, loop.time())
            except OSError:
                pass
            reason = deadline.expired(loop.time())
            if reason is not None:
                # Частая причина при обновлении Claude Code: channels —
                # research preview, флаги/протокол могут поменяться, и тогда
                # claude не спавнит channel_server или не отвечает handshake.
                why = (
                    f"молчит {READY_SILENCE_SEC:.0f} с (лог не растёт)"
                    if reason == "silence"
                    else f"не уложился в потолок {READY_TIMEOUT_MAX / 60:.0f} мин"
                )
                extra = ""
                if vm:
                    extra = (
                        " Под agent-vm первый запуск ещё и тянет OCI-образ "
                        "(минуты) — прогрей заранее: `agent-vm setup`."
                    )
                raise SessionError(
                    f"Claude не поднял channel-сервер: {why}. "
                    "Возможно, обновилась версия Claude Code и изменился протокол "
                    f"каналов (research preview) — проверь лог и совместимость.{extra} "
                    f"Лог: {log_path}"
                )
            await asyncio.sleep(1)

    # ── жизненный цикл: close / resume / clear / set_model ─────

    async def close(self, session: Session) -> None:
        """Остановить процесс, сохранив запись: топик живёт, resume возможен."""
        async with session.ops:
            await self._stop_process(session)
            self.save_state()

    async def delete(self, session: Session) -> None:
        """Полностью удалить сессию (процесс + запись + приватный дом)."""
        async with session.ops:
            async with self._lock:
                self._by_name.pop(session.name, None)
                self._cpu.pop(session.name, None)
            await self._stop_process(session)
            # Модули: сессия удалена (напр. wallet отзывает её токен). До этого
            # токен удалённой сессии оставался бы рабочим у демона.
            for hook in self.session_delete_hooks:
                try:
                    result = hook(session.name)
                    if inspect.isawaitable(result):
                        await result  # корутинный хук (wallet: стоп прокси) дожидаем
                except Exception:
                    logger.exception("session_delete_hook для %s", session.name)
            # Приватный дом песочницы: без удаления /new с тем же slug
            # унаследовал бы прежний $HOME (ключи, ~/.wallet.json, ~/.bashrc-
            # foothold) и каталоги копились бы вечно.
            home = self.config.sessions_dir / ".homes" / session.name
            if home.is_dir():
                await asyncio.to_thread(shutil.rmtree, home, ignore_errors=True)
            self.save_state()

    async def resume(self, session: Session) -> bool:
        """Оживить остановленную сессию (после /close_session или рестарта).

        Сначала пробуем `claude --resume` (продолжить диалог); если резюмировать
        нечего — чистый старт с новым UUID. Возвращает True, если контекст
        удалось продолжить.
        """
        async with session.ops:
            if session.running:
                return True
            return await self._resume_locked(session)

    async def revive(self, session: Session) -> bool:
        """Поднять сессию заново, даже если её процесс ЧИСЛИТСЯ живым.

        Случай из жизни: процесс claude жив, а его MCP-канал мёртв (channel-сервер
        не поднялся/умер) — `running` по процессу True, поэтому обычный resume
        считает, что делать нечего, и сообщения оператора упираются в закрытый
        порт. Здесь гасим процесс принудительно и стартуем заново с --resume:
        для оператора это «сессия сама переподнялась», а не сырая ошибка сокета.

        Возвращает True, если контекст удалось продолжить (как resume).
        """
        async with session.ops:
            if session.running:
                logger.info(
                    "Сессия %s: канал мёртв при живом процессе — переподнимаю",
                    session.name)
                # Слот её; придерживаем на паузу между stop и start, иначе
                # конкурентный /new займёт освободившееся на секунду место.
                self._reserve_slot(session.name)
                await self._stop_process(session)
            return await self._resume_locked(session)

    async def _resume_locked(self, session: Session) -> bool:
        """Поднять процесс остановленной сессии. Слот держим весь подъём.

        Слот занимает _resume_started (под локом) и отпускает finally ниже —
        на успехе, на исключении и при отмене задачи. Иначе провалившийся
        resume навсегда занял бы место в лимите.
        """
        self._guard_engine(session)
        # Возобновление поднимает НОВЫЙ процесс — для машины это такая же
        # нагрузка, как /new, и лимит с ресурсами тут обязаны сработать.
        # Иначе через resume остановленных сессий оба ограничения обходятся.
        self.check_resources(session.sandbox)
        try:
            return await self._resume_started(session)
        finally:
            self._release_slot(session.name)

    async def _resume_started(self, session: Session) -> bool:
        async with self._lock:
            # Сама сессия ещё не запущена — в count() не входит, сравнение
            # прямое: столько уже живёт/стартует, влезет ли ещё одна.
            self._guard_limit(session.name)
        # Раннеры с unique_cwd (agent-vm): гвард нужен и на resume/clear, не
        # только на create — иначе восстановленные из .sessions.json две сессии
        # на один cwd убьют VM друг друга при первом сообщении.
        self._guard_unique_cwd(session)
        async with self._lock:
            self._allocate_port(session)
        self._write_configs(session)

        resumed = True
        await self._start_claude(session, resume=True)
        try:
            await self._wait_ready(session)
            # --resume без сохранённого транскрипта умирает не сразу,
            # а через несколько секунд после старта — даём ему проявиться.
            await asyncio.sleep(RESUME_GRACE)
            if not session.running:
                raise SessionError("resume-процесс умер после старта")
        except SessionError:
            # Транскрипта нет или resume не поддержался — чистый старт.
            logger.warning("Сессия %s: resume не удался, чистый старт", session.name)
            resumed = False
            await self._stop_process(session, save=False)
            # Чистый рестарт переиспользует session.port, но _stop_process снял
            # его из _inflight_ports — в окне до подъёма нового channel-сервера
            # конкурентный _find_free_port отдал бы этот порт другой сессии, и
            # два сервера сели бы на один порт (сообщения перепутались бы).
            # Заново резервируем; снимется в _start_watcher (успех) или в
            # _stop_process ниже (провал).
            async with self._lock:
                self._inflight_ports.add(session.port)
            await self._wait_port_free(session.port)
            session.claude_session_id = str(uuid.uuid4())
            try:
                await self._start_claude(session)
                await self._wait_ready(session)
            except Exception:
                # Не оставляем процесс-зомби без watcher'а.
                await self._stop_process(session, save=False)
                self.save_state()
                raise

        session.started_at = time.time()
        self._start_watcher(session)
        self.save_state()
        return resumed

    async def clear(self, session: Session) -> None:
        """Перезапустить Claude с чистым контекстом: та же папка, тот же топик."""
        async with session.ops:
            self._guard_engine(session)  # до остановки процесса и выдачи порта
            # На ЖИВОЙ сессии /clear — это stop+start: слот уже её, лимит по
            # нулям. А вот на ОСТАНОВЛЕННОЙ он поднимает процесс с нуля, ровно
            # как resume, — без этих проверок через /clear обходились бы и
            # лимит, и запас памяти.
            if session.running:
                # Слот уже её — не проверяем, а придерживаем на время
                # stop+start, иначе конкурентный /new займёт место, которое
                # освободилось на секунду.
                self._reserve_slot(session.name)
            else:
                self.check_resources(session.sandbox)
                async with self._lock:
                    self._guard_limit(session.name)
            try:
                await self._clear_locked(session)
            finally:
                self._release_slot(session.name)

    async def _clear_locked(self, session: Session) -> None:
        """Тело /clear. Зовётся под session.ops (см. clear)."""
        await self._stop_process(session, save=False)
        await self._wait_port_free(session.port)
        session.claude_session_id = str(uuid.uuid4())
        async with self._lock:
            self._allocate_port(session)
        self._write_configs(session)
        try:
            await self._start_claude(session)
            await self._wait_ready(session)
        except Exception:
            await self._stop_process(session, save=False)
            self.save_state()
            raise
        session.started_at = time.time()
        self._start_watcher(session)
        self.save_state()

    async def set_model(self, session: Session, model: str) -> bool:
        """Сменить модель: перезапуск с --model, контекст — через resume.

        Возвращает True, если контекст удалось продолжить. При ошибке
        (например, несуществующая модель) откатывает модель обратно.
        """
        old_model = session.model
        try:
            async with session.ops:
                session.model = model or None
                if session.running:
                    # Перезапуск живой сессии: слот её, придерживаем на паузу
                    # между stop и start (_resume_locked снимет резерв сам).
                    self._reserve_slot(session.name)
                    await self._stop_process(session, save=False)
                return await self._resume_locked(session)
        except Exception:
            session.model = old_model
            self.save_state()
            raise

    async def shutdown(self) -> None:
        """Остановка launcher'а: убить процессы, записи сохранить."""
        for session in self.list_all():
            await self._stop_process(session, save=False)
        self.save_state()
        if self._http is not None and not self._http.closed:
            await self._http.close()

    # ── внутренняя механика процессов ───────────────────────────

    def _start_watcher(self, session: Session) -> None:
        # Сессия поднялась и стабильна: её порт теперь реально держит живой
        # channel-сервер (учитывается через running), резерв «в полёте» снят.
        self._inflight_ports.discard(session.port)
        session.watcher = asyncio.create_task(
            self._watch(session), name=f"watch-{session.name}"
        )

    async def _watch(self, session: Session) -> None:
        """Ждёт завершения Claude; при внезапной смерти помечает остановленной."""
        proc = session.process
        assert proc is not None
        code = await proc.wait()
        if session.process is not proc:
            return  # процесс уже заменён resume/clear — не наш клиент
        if self._by_name.get(session.name) is not session:
            return  # уже удалена
        session.process = None
        session.watcher = None
        self._close_log(session)
        self.save_state()
        logger.warning("Сессия %s: Claude неожиданно завершился (код %s)", session.name, code)
        if self.on_dead is not None:
            # Колбэк (уведомление в Telegram) не должен ронять watcher-таск:
            # исключение здесь otherwise убило бы задачу молча (REVIEW.md B3).
            try:
                await self.on_dead(session, code)
            except Exception:
                logger.exception("on_dead для сессии %s — колбэк упал", session.name)

    async def _stop_process(self, session: Session, save: bool = True) -> None:
        """Погасить процесс сессии: watcher, группа процессов, лог."""
        if session.watcher is not None:
            session.watcher.cancel()
            session.watcher = None
        await self._terminate(session)
        await self._stop_docker_proxy(session)
        session.process = None
        # Старт мог упасть до _start_watcher — снимаем резерв порта и здесь,
        # иначе он утёк бы из _inflight_ports навсегда.
        self._inflight_ports.discard(session.port)
        if save:
            self.save_state()

    @staticmethod
    async def _terminate(session: Session) -> None:
        # Убиваем всю группу процессов (start_new_session=True): иначе
        # channel_server переживает claude, держит порт и отвечает на /ping.
        proc = session.process
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, 15)  # SIGTERM группе
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    os.killpg(proc.pid, 9)  # SIGKILL
                    await proc.wait()
            except (ProcessLookupError, PermissionError):
                pass
        SessionManager._close_log(session)

    @staticmethod
    async def _wait_port_free(port: int, timeout: float = 10.0) -> None:
        """Подождать, пока старый channel-сервер отпустит порт."""
        if port <= 0:
            return
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("127.0.0.1", port))
                    return
                except OSError:
                    pass
            await asyncio.sleep(0.3)
        logger.warning("Порт %d не освободился за %.0f с", port, timeout)

    @staticmethod
    def _close_log(session: Session) -> None:
        if session.log_file is not None and not session.log_file.closed:
            session.log_file.close()

    # ── связь с работающей сессией ──────────────────────────────

    async def send_to_claude(self, session: Session, text: str, context_id: str) -> None:
        session.last_activity = time.time()
        http = self._http_session()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SEND_RETRY_TIMEOUT
        while True:
            try:
                async with http.post(
                    f"http://127.0.0.1:{session.port}/notify",
                    json={"content": text, "context_id": context_id},
                    headers=self._channel_headers(),
                ) as resp:
                    resp.raise_for_status()
                return
            except (aiohttp.ClientConnectorError, asyncio.TimeoutError):
                # channel-сервер ещё поднимается (сессия только что resume, а это
                # параллельное сообщение) — ретраим, пока не встанет. Ловим и
                # TimeoutError: /ping отвечает раньше, чем завершится stdio-MCP
                # хендшейк, а /notify ждёт _initialized на сервере — при медленном
                # хендшейке POST упирается в ClientTimeout(total=10) и без этого
                # ретрая сообщение молча терялось бы. Дохлая сессия или выход за
                # SEND_RETRY_TIMEOUT → пробрасываем (не крутимся зря).
                if not session.running or loop.time() >= deadline:
                    raise
                await asyncio.sleep(0.3)

    def touch(self, session: Session) -> None:
        """Отметить активность (ответ Claude) — сброс таймера простоя."""
        session.last_activity = time.time()

    def tail_log(self, session: Session, lines: int = 15) -> str:
        """Последние строки claude.log без ANSI — для диагностики в чате."""
        path = session.session_dir / "claude.log"
        try:
            raw = path.read_bytes()[-16384:]
        except OSError:
            return ""
        clean = strip_ansi(raw)
        text = clean.decode(errors="replace")
        tail = [ln for ln in text.splitlines() if ln.strip()][-lines:]
        return "\n".join(tail)

    def is_busy(self, session: Session) -> bool:
        """Растёт ли CPU-время дерева процессов claude — признак жизни для вотчдога.

        Жив, если сумма CPU-тиков дерева (claude + запущенные им тулы) выросла с
        прошлой проверки. Раньше учитывался и `has_kids` («есть дочерний процесс»),
        но под bwrap у процесса сессии ВСЕГДА есть дочерний (внутренний bwrap
        pid-ns init), из-за чего is_busy был вечно True и вотчдог не срабатывал.
        Случай «тул идёт, но CPU на миг замер» (Bash `sleep`/сетевое ожидание)
        теперь ловит _watchdog_loop отдельным сигналом `_tool_inflight` (хуки).
        Если /proc недоступен — считаем живым: лучше пропустить редкое реальное
        зависание, чем спамить ложным.

        Вызывать из единственного места (_watchdog_loop): метод хранит
        предыдущий отсчёт CPU по имени сессии между вызовами.
        """
        pid = session.process.pid if session.running and session.process else None
        if pid is None:
            return False
        try:
            cpu, _has_kids = proc_tree_signals(pid)
        except Exception:  # /proc недоступен — перестраховочно «жив»
            logger.debug("is_busy: /proc недоступен для pid=%s", pid)
            return True
        prev = self._cpu.get(session.name)
        self._cpu[session.name] = cpu
        return prev is not None and cpu > prev

    async def run_and_capture(self, session: Session, cmd: str, wait: float = 6.0) -> str:
        """Ввести слэш-команду в PTY и вернуть новый вывод claude.log без ANSI.

        Для команд Claude Code (/cost, /context…), чей вывод — TUI-перерисовка.
        """
        log = session.session_dir / "claude.log"
        before = log.stat().st_size if log.exists() else 0
        self.type_into_pty(session, cmd)
        await asyncio.sleep(wait)

        def _read_delta() -> str:
            # Читаем приращение через seek, а не весь файл в память (лог под
            # LOG_MAX_MB — десятки МБ); в потоке, чтобы не стопорить event loop.
            try:
                with open(log, "rb") as fh:
                    fh.seek(before)
                    raw = fh.read()
            except OSError:
                return ""
            return strip_ansi(raw).decode(errors="replace")

        return await asyncio.to_thread(_read_delta)

    async def close_idle(self) -> list[Session]:
        """Остановить работающие сессии, простаивавшие дольше IDLE_TIMEOUT_H.

        Возвращает список остановленных (для уведомления в чат).
        """
        timeout = self.config.idle_timeout_h * 3600
        if timeout <= 0:
            return []
        now = time.time()
        stale = [
            s for s in self.list_all()
            if s.running and now - s.last_activity > timeout
        ]
        for session in stale:
            logger.info("Сессия %s простаивала — авто-останов", session.name)
            await self.close(session)
        return stale

    async def send_permission(self, session: Session, request_id: str, behavior: str) -> None:
        """Вердикт по запросу разрешения — обратно в channel_server."""
        http = self._http_session()
        async with http.post(
            f"http://127.0.0.1:{session.port}/permission",
            json={"request_id": request_id, "behavior": behavior},
            headers=self._channel_headers(),
        ) as resp:
            resp.raise_for_status()

    def _send_raw(self, session: Session, data: bytes) -> None:
        """Записать сырые байты (управляющие коды) прямо в PTY claude."""
        if session.pty_master is None or not session.running:
            raise SessionError("Сессия не запущена.")
        try:
            os.write(session.pty_master, data)
        except OSError as e:
            raise SessionError(f"Терминал сессии недоступен: {e}") from e

    def interrupt_turn(self, session: Session) -> None:
        """Жёстко прервать текущий ход: Esc (\\x1b) в PTY-терминал Claude.

        В channels-протоколе прерывания нет, но интерактивный claude живёт под
        нашим PTY — байт \\x1b эквивалентен нажатию Esc в TUI и обрывает ход
        немедленно (в отличие от «мягкого стопа» push-сообщением, которое
        модель прочитает только когда доберётся). Контекст сессии сохраняется.
        """
        self._send_raw(session, b"\x1b")

    def background_turn(self, session: Session) -> None:
        """Отправить текущую задачу в фон: Ctrl+B (\\x02) в PTY.

        В TUI Claude Code Ctrl+B переводит долгую задачу (bash-команду) в фон —
        ход продолжается, не блокируясь на ней. Эквивалент нажатия Ctrl+B в
        терминале сессии."""
        self._send_raw(session, b"\x02")

    def type_into_pty(self, session: Session, text: str) -> None:
        """Напечатать команду прямо в терминал Claude (слэш-команды CC)."""
        if session.pty_master is None or not session.running:
            raise SessionError("Сессия не запущена.")
        # Только печатные символы одной строки — никаких управляющих кодов.
        clean = "".join(ch for ch in text if ch.isprintable())
        # PTY может принять не весь буфер за раз — дописываем хвост, иначе
        # длинная слэш-команда обрежется.
        data = clean.encode() + b"\r"
        try:
            while data:
                n = os.write(session.pty_master, data)
                data = data[n:]
        except OSError as e:
            raise SessionError(f"Терминал сессии недоступен: {e}") from e

    # ── статистика по транскрипту ───────────────────────────────

    def effective_cwd(self, session: Session) -> Path:
        """Реальный cwd процесса claude: папка проекта, если задан линк,
        иначе папка сессии. Натуральное поведение «cd в проект и claude» —
        Claude грузит CLAUDE.md/.mcp.json/.claude из проекта.

        Эту же строку используем для кодирования пути транскрипта (см.
        transcript_path) — Claude хранит его по cwd, должны совпадать.
        """
        if session.linked_path:
            try:
                return Path(session.linked_path).resolve()
            except OSError:
                return session.session_dir
        return session.session_dir

    def engine_of(self, session: Session | None) -> str:
        """Движок изоляции сессии: её собственный (флаг `--box`) или дефолт .env.

        ЕДИНСТВЕННЫЙ источник истины «в чём живёт эта сессия»: всё, что раньше
        смотрело на `config.sandbox` в контексте конкретной сессии (какой python
        в госте, куда биндить channel-сервер, есть ли кошелёк/docker, сколько
        ждать готовности), спрашивает здесь. Без сессии (общий /bash оператора,
        preflight на старте) — дефолт."""
        if session is None:
            return self.config.sandbox
        return session.sandbox or self.config.sandbox

    def runner_for(self, session: Session | None) -> runner_mod.Runner:
        """Раннер под движок сессии. Кэш по имени движка: раннеры без
        per-session состояния (argv-обёртка), поэтому переиспользуемы."""
        engine = self.engine_of(session)
        # getattr-фолбэк: часть тестов конструирует менеджер через __new__ (без
        # __init__) — раньше ленивый `self._runner` это допускал, сохраняем.
        cache = getattr(self, "_runners", None)
        if cache is None:
            cache = {}
            self._runners = cache
        r = cache.get(engine)
        if r is None:
            r = runner_mod.make_runner(self.config, ROOT, engine)
            cache[engine] = r
        return r

    @property
    def runner(self) -> runner_mod.Runner:
        """Раннер процессов по ДЕФОЛТНОМУ движку (.env). Для сессии со своим
        движком — `runner_for(session)`."""
        return self.runner_for(None)

    def session_home(self, session: Session) -> Path:
        """Персистентный приватный $HOME сессии для песочницы.

        Живёт в SESSIONS_DIR/.homes/<имя> (вне папки сессии, чтобы не
        светиться в её RW-бинде вторым путём): venv/кэши, которые агент
        кладёт «к себе домой», переживают рестарты — в отличие от прежнего
        tmpfs. Каталог создаётся здесь же (раннеру нужен существующий путь).
        """
        home = self.config.sessions_dir / ".homes" / session.name
        home.mkdir(parents=True, exist_ok=True)
        return home

    def sandbox_prefix(
        self, chdir: Path, extra_rw: list[Path], session: Session | None = None
    ) -> list[str]:
        """Префикс argv для запуска команды в изоляции текущего раннера.

        Пусто при SANDBOX=off. Политика allowlist — в runners.bwrap.
        session задана — команда получает тот же персистентный $HOME, что и
        claude этой сессии (/bash видит venv, который агент себе поставил), и тот
        же docker-прокси (если поднят) — /bash тоже видит docker сессии.
        """
        home_dir = self.session_home(session) if session is not None else None
        dsock = (
            self.config.docker_sock_path(session.name)
            if session is not None and session.name in self._docker_proxies
            else None
        )
        return self.runner_for(session).wrap(
            [], chdir=chdir, extra_rw=extra_rw, home_dir=home_dir, docker_sock=dsock)

    # ── per-session docker-прокси (SANDBOX_BWRAP_DOCKER) ────────────────
    def _docker_roots(self, session: Session) -> list[Path]:
        """Разрешённые корни bind'ов = проект ЭТОЙ сессии (RW-область песочницы):
        папка сессии + рабочий каталог проекта + SANDBOX_EXTRA_RW."""
        roots = [session.session_dir, self.effective_cwd(session)]
        roots += list(self.config.sandbox_extra_rw)
        return roots

    async def _ensure_docker_proxy(self, session: Session) -> Path | None:
        """Поднять per-session docker-прокси (идемпотентно). Возвращает путь его
        сокета для бинда в песочницу или None, если docker в песочнице выключен."""
        if not self.config.sandbox_docker or self.engine_of(session) != "bwrap":
            return None
        sock = self.config.docker_sock_path(session.name)
        if session.name not in self._docker_proxies:
            proxy = DockerProxy(
                sock, roots_provider=lambda s=session: self._docker_roots(s))
            await proxy.start()
            self._docker_proxies[session.name] = proxy
            logger.info("docker-прокси сессии %s: %s (allowlist = проект сессии)",
                        session.name, sock)
        return sock

    async def _stop_docker_proxy(self, session: Session) -> None:
        proxy = self._docker_proxies.pop(session.name, None)
        if proxy is not None:
            await proxy.stop()

    def transcript_path(self, session: Session) -> Path:
        """Транскрипт сессии в профиле Claude Code (client-config → box)."""
        config_dir = box_transcript.resolve_config_dir(self.config.claude_config_dir)
        return box_transcript.transcript_path(
            config_dir, self.effective_cwd(session), session.claude_session_id
        )

    def read_stats(self, session: Session) -> dict | None:
        """Статистика из транскрипта (None — ещё не создан). Блокирующее
        чтение — вызывать через asyncio.to_thread. Логика — transcript.py."""
        return transcript.read_stats(self.transcript_path(session))

    def read_last_model(self, session: Session) -> str | None:
        """Реальная модель последнего ответа (после подмены прокси) — дёшево
        из хвоста транскрипта. Логика — transcript.read_last_model."""
        return transcript.read_last_model(self.transcript_path(session))

    def read_pollution_excerpt(self, session: Session, max_entries: int = 25) -> str | None:
        """Эксцепт загрязнения чужим бэкендом из хвоста транскрипта (или None).
        Блокирующее чтение — вызывать через asyncio.to_thread. Логика —
        transcript.py."""
        return transcript.read_pollution_excerpt(self.transcript_path(session), max_entries)
