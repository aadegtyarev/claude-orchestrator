"""Учётка Claude Code: статус профиля и вход прямо из чата.

Зачем модуль. Протухшая учётка НЕ роняет сессию, и это худший вид поломки:
интерактивный claude жив, но на каждое сообщение отвечает сам за 0 с —
«Login expired · Please run /login», без тулов и без reply_to_user. Каналу
ретранслировать нечего, вотчдогу нечего ловить (лог растёт, процесс жив) — в
чат не приходит НИЧЕГО, а сообщения оператора молча съедаются. Живой случай:
сессия noos 2026-08-17, так потерялись три сообщения подряд.

Вход делаем не через TUI сессии, а отдельным процессом `claude auth login`: у
CLI ровно тот диалог, который ложится на чат (проверено на 2.1.233)

    Opening browser to sign in…
    If the browser didn't open, visit: <url>
    Paste code here if prompted >

URL уезжает в чат ссылкой, код оператора уходит процессу в stdin. Живые сессии
перезапускать не нужно: claude сам перечитывает обновлённый
<config_dir>/.credentials.json (на noos сессия ожила и добила очередь).

Процесс поднимаем на ХОСТЕ правами оркестратора, с CLAUDE_CONFIG_DIR профиля:
это операторское действие, ему нужны сеть и запись в учётку, а песочница — для
модели. Учётка общая на профиль, поэтому один вход чинит все сессии профиля.

Истина о результате — не текст TUI, а `claude auth status --json`
(loggedIn/authMethod/email/subscriptionType): переименуют строку — вход всё
равно подтвердится верно.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from box.launch import LaunchHandle, launch as box_launch

from .ansi import strip_ansi

logger = logging.getLogger(__name__)

# Сколько ждём ответа CLI на каждом шаге входа.
STATUS_TIMEOUT = 25.0     # `auth status --json`
URL_TIMEOUT = 45.0        # печать ссылки после старта `auth login`
RESULT_TIMEOUT = 90.0     # завершение процесса после отправки кода
# Брошенный вход (оператор ушёл, кода нет) не должен висеть процессом вечно.
FLOW_TTL = 900.0

# Ссылка авторизации в выводе CLI. Печатается дважды подряд (OSC-8 гиперссылка:
# `ESC]8;;<url>ESC\<url>ESC]8;;`), поэтому режем по первому же ESC/BEL/пробелу —
# иначе в ссылку склеятся оба экземпляра и она не откроется.
_URL_RE = re.compile(rb"https://[^\s\x1b\x07\"'<>]+/oauth/authorize\?[^\s\x1b\x07\"'<>]+")
# Промпт кода: значит CLI ждёт ввода (а не упал на печати ссылки).
_PROMPT_RE = re.compile(rb"Paste code here", re.IGNORECASE)
# `state` из ссылки: страница авторизации отдаёт код как «<код>#<state>».
_STATE_RE = re.compile(r"[?&]state=([A-Za-z0-9_.\-]+)")


class AuthError(Exception):
    """Вход невозможен/сорвался — сообщение уже человеческое, для чата."""


def env_for(config_dir: Path | None) -> dict[str, str]:
    """Окружение процесса учётки: профиль + запрет открывать браузер.

    Браузера на сервере нет, а `claude auth login` первым делом пытается его
    открыть: без BROWSER=/bin/true он зовёт xdg-open и на голом хосте может
    подвиснуть. Ссылку всё равно открывает оператор на телефоне.
    """
    env = dict(os.environ)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["BROWSER"] = "/bin/true"
    env.pop("DISPLAY", None)
    return env


def parse_status(raw: bytes) -> dict:
    """Разобрать вывод `auth status --json` (JSON может ехать в обёртке).

    Вокруг JSON бывает шум (баннер автообновления), поэтому берём кусок от
    первой `{` до последней `}`. Не разобралось — пустой dict.
    """
    text = strip_ansi(raw).decode(errors="replace")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def logged_out(status: dict) -> bool:
    """Явное «не залогинен».

    Именно явное: пустой ответ (нет бинаря, таймаут) — это «не знаю», и
    поднимать по нему тревогу о протухшей учётке нельзя.
    """
    return status.get("loggedIn") is False


def account_line(status: dict) -> str:
    """Короткая строка про учётку для чата: почта · способ · подписка."""
    parts = [
        str(status.get("email") or ""),
        str(status.get("authMethod") or ""),
        str(status.get("subscriptionType") or ""),
    ]
    return " · ".join(p for p in parts if p)


def extract_url(raw: bytes) -> str | None:
    """Ссылка авторизации из вывода CLI (первая), либо None."""
    m = _URL_RE.search(raw)
    return m.group(0).decode("ascii", "replace") if m else None


def looks_like_code(text: str, state: str | None = None) -> bool:
    """Похоже ли сообщение на код авторизации со страницы OAuth.

    Признак нужен адаптеру: пока идёт вход, топик ждёт код, но перехватывать
    ЛЮБОЕ сообщение нельзя — оператор в этот момент вполне может написать
    модели. Не похоже на код → уходит claude как обычно.

    Надёжный признак — `state` из ссылки, которую мы же и выдали: страница
    отдаёт код в виде «<код>#<state>». Если его нет (страница показала голый
    код), остаётся форма: длинная строка без пробелов. Ссылки исключаем явно —
    единственный правдоподобный «длинный токен без пробелов», который оператор
    шлёт модели, это URL.
    """
    stripped = (text or "").strip()
    if state and state in stripped:
        return True
    if len(stripped) < 16 or stripped.startswith("/"):
        return False
    if stripped.lower().startswith(("http://", "https://")):
        return False
    return not any(ch.isspace() for ch in stripped)


async def status(
    claude_bin: str, config_dir: Path | None, timeout: float = STATUS_TIMEOUT
) -> dict:
    """`claude auth status --json` для профиля → dict (пустой = не смогли узнать)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "auth", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env_for(config_dir),
        )
    except OSError as e:
        logger.debug("auth status: не запустился %s: %s", claude_bin, e)
        return {}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        logger.debug("auth status: таймаут %.0f с", timeout)
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {}
    return parse_status(out)


@dataclass
class LoginFlow:
    """Один вход в учётку профиля: процесс `claude auth login` под PTY.

    Живёт между сообщениями чата: старт печатает ссылку, оператор открывает её
    на телефоне и присылает код, код уходит процессу в stdin. Вывод копим в
    буфер (пишет поток-драйвер PTY, отсюда лок).
    """

    config_dir: Path | None
    claude_bin: str
    console: bool = False
    started_at: float = field(default_factory=time.time)
    url: str | None = None
    code_sent: bool = False
    _handle: LaunchHandle | None = None
    _buf: bytearray = field(default_factory=bytearray)
    _lock: Lock = field(default_factory=Lock)
    # Секреты, которые нельзя показывать в чате (эхо PTY возвращает введённый
    # код в буфер вывода — без этого он уехал бы в сообщение «хвост вывода»).
    _secrets: list[str] = field(default_factory=list)

    # ── вывод процесса ──────────────────────────────────────────

    def _on_output(self, chunk: bytes) -> None:
        with self._lock:
            self._buf.extend(chunk)
            del self._buf[:-65536]  # держим только хвост

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    def tail(self, lines: int = 6) -> str:
        """Хвост вывода без ANSI и БЕЗ секретов — для показа в чате."""
        text = strip_ansi(self.snapshot()).decode(errors="replace")
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "***")
        rows = [ln.strip() for ln in text.replace("\r", "\n").splitlines() if ln.strip()]
        return "\n".join(rows[-lines:])

    # ── шаги входа ──────────────────────────────────────────────

    async def start(self) -> str:
        """Поднять `claude auth login` и дождаться ссылки авторизации.

        Возвращает URL; бросает AuthError, если CLI не запустился или ссылку
        так и не напечатал (тогда процесс уже прибран).
        """
        argv = [self.claude_bin, "auth", "login",
                "--console" if self.console else "--claudeai"]
        cwd = str(self.config_dir if self.config_dir and self.config_dir.is_dir()
                  else Path.home())
        try:
            self._handle = await box_launch(
                argv, cwd=cwd, env=env_for(self.config_dir),
                on_output=self._on_output, name="auth-login",
            )
        except Exception as e:  # нет бинаря, нет прав — наружу человеческим текстом
            raise AuthError(str(e)) from e
        # Авто-ответчик стартовых диалогов тут не нужен (это не сессия claude):
        # пусть не печатает клавиши в stdin, где ждут код.
        self._handle.answerer.stop()
        url = await self._wait_url()
        if url is None:
            tail = self.tail()
            await self.close()
            raise AuthError(tail or "")
        self.url = url
        return url

    async def _wait_url(self, timeout: float | None = None) -> str | None:
        """Ждать ссылку в выводе; ранняя смерть процесса — не ждать до таймаута.

        Таймаут читаем в момент вызова (а не в значении по умолчанию) — иначе
        его не подменить ни тестом, ни настройкой.
        """
        deadline = time.monotonic() + (timeout or URL_TIMEOUT)
        while time.monotonic() < deadline:
            url = extract_url(self.snapshot())
            if url:
                return url
            if self._handle is not None and self._handle.process.returncode is not None:
                return extract_url(self.snapshot())
            await asyncio.sleep(0.3)
        return None

    def waiting_code(self) -> bool:
        """CLI напечатал промпт кода и процесс жив — можно слать код."""
        if self._handle is None or self._handle.process.returncode is not None:
            return False
        return bool(_PROMPT_RE.search(strip_ansi(self.snapshot())))

    async def submit(self, code: str) -> dict:
        """Отдать код процессу и вернуть статус учётки после его завершения.

        Статус берём у `auth status --json`, а не из текста TUI: он авторитетен
        и переживёт переименование строк. Пустой статус = «узнать не вышло».
        """
        if self._handle is None:
            raise AuthError("")
        code = code.strip()
        self._secrets.append(code)
        try:
            os.write(self._handle.pty_master, (code + "\r").encode())
        except OSError as e:
            raise AuthError(str(e)) from e
        self.code_sent = True
        try:
            await asyncio.wait_for(self._handle.process.wait(), RESULT_TIMEOUT)
        except asyncio.TimeoutError:
            logger.debug("auth login: процесс не завершился за %.0f с", RESULT_TIMEOUT)
        st = await status(self.claude_bin, self.config_dir)
        await self.close()
        return st

    async def close(self) -> None:
        """Прибрать процесс (драйвер PTY закроет master сам)."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        if handle.process.returncode is None:
            try:
                handle.process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(handle.process.wait(), 5)
            except asyncio.TimeoutError:
                logger.debug("auth login: процесс не умер за 5 с")

    @property
    def state(self) -> str | None:
        """`state` из выданной ссылки — им страница помечает код («код#state»).

        Точный признак «это код, а не сообщение модели» для адаптера.
        """
        if not self.url:
            return None
        m = _STATE_RE.search(self.url)
        return m.group(1) if m else None

    def expired(self, now: float | None = None) -> bool:
        """Брошенный вход: ссылку выдали, кода нет дольше FLOW_TTL."""
        return (now or time.time()) - self.started_at > FLOW_TTL


class LoginManager:
    """Входы по профилям: на профиль — не больше одного живого процесса.

    Ключ — каталог учётки (CLAUDE_CONFIG_DIR профиля): учётка общая для всех
    сессий профиля, значит и вход общий. Второй /login по тому же профилю не
    плодит процесс, а перевыдаёт ссылку текущего (или начинает заново, если
    прошлый протух).
    """

    def __init__(self, claude_bin: str) -> None:
        self.claude_bin = claude_bin
        self._flows: dict[str, LoginFlow] = {}

    @staticmethod
    def key(config_dir: Path | None) -> str:
        return str(config_dir) if config_dir is not None else "-"

    def get(self, config_dir: Path | None) -> LoginFlow | None:
        return self._flows.get(self.key(config_dir))

    async def status(self, config_dir: Path | None) -> dict:
        return await status(self.claude_bin, config_dir)

    async def start(self, config_dir: Path | None, console: bool = False) -> LoginFlow:
        """Начать (или перевыдать) вход для профиля. Бросает AuthError."""
        key = self.key(config_dir)
        flow = self._flows.get(key)
        if flow is not None:
            if flow.url and not flow.code_sent and not flow.expired():
                return flow  # ссылка ещё жива — не плодим процессы
            await flow.close()
        flow = LoginFlow(config_dir=config_dir, claude_bin=self.claude_bin,
                         console=console)
        self._flows[key] = flow
        try:
            await flow.start()
        except AuthError:
            self._flows.pop(key, None)
            raise
        return flow

    async def submit(self, config_dir: Path | None, code: str) -> dict:
        """Отправить код текущему входу профиля → статус учётки после него."""
        key = self.key(config_dir)
        flow = self._flows.get(key)
        if flow is None:
            raise AuthError("")
        try:
            return await flow.submit(code)
        finally:
            self._flows.pop(key, None)

    async def cancel(self, config_dir: Path | None) -> bool:
        """Отменить незавершённый вход. True — было что отменять."""
        flow = self._flows.pop(self.key(config_dir), None)
        if flow is None:
            return False
        await flow.close()
        return True

    async def close_all(self) -> None:
        flows, self._flows = list(self._flows.values()), {}
        for flow in flows:
            await flow.close()
