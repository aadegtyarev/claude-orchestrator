"""VaultDaemon — автономный демон кошелька: HTTP-API секретов на
127.0.0.1:<эфемерный порт> + реестр токенов сессий. Ни одной зависимости от
оркестратора: услуги окружения (подтверждение/бабл/аудит/notice) приходят через
VaultHost (по ИМЕНИ сессии), рабочий каталог — из контекста токена.

Модель угроз — в module-docstring адаптера (orchestrator/modules/wallet). Секрет
не появляется в песочнице: команда исполняется здесь, на хосте, с секретом в env
короткоживущего ребёнка, наружу — только вывод с вымаранными значениями.

cwd через issue_token(session_name, cwd): оркестратор при провижне снимает рабочий
каталог сессии ОДИН раз и отдаёт демону; auth возвращает ctx=(name, cwd); execute
берёт cwd из ctx. Так демон не перерезолвивает имя→cwd посреди запроса (гонка с
удалением сессии роняла бы effective_cwd(None) — урок ревью слайса 1.4a).
"""

from __future__ import annotations

import functools
import html
import logging
import os
import secrets
import socket
from pathlib import Path
from typing import NamedTuple

from aiohttp import web

from .execute import run_secret_command
from .host import VaultHost, deny_remedy
from .proxy_pool import SessionProxyPool
from .redact import _redact
from .secret import Secret, _prints_token
from .store import SecretStore
from .verdict import evaluate

logger = logging.getLogger(__name__)


class Ctx(NamedTuple):
    """Контекст аутентифицированного запроса: имя сессии + её рабочий каталог,
    снятый при выдаче токена (не перерезолвивается)."""

    name: str
    cwd: Path


def _authed(handler):
    """Декоратор роут-хендлеров: резолвит ctx через `_auth`, отдаёт 401 если
    Bearer-токен не признан, иначе зовёт хендлер с готовым `ctx`. Единая точка
    401-политики на все роуты."""
    @functools.wraps(handler)
    async def wrapper(self, request: web.Request) -> web.Response:
        ctx = self._auth(request)
        if ctx is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(self, request, ctx)
    return wrapper


class VaultDaemon:
    """HTTP-демон секретов. store — чтение policy/значений; host — услуги
    окружения (по имени сессии); guard_on — жёсткий щит (config.wallet_guard)."""

    def __init__(
        self, store: SecretStore, host: VaultHost, *, guard_on: bool,
        shutdown_timeout: float | None = None,
        proxies: SessionProxyPool | None = None,
    ) -> None:
        self.store = store
        self.host = host
        self.guard_on = guard_on
        # Пул per-session MITM-прокси (§4.3/§4.5). Опционален: без него демон
        # работает как раньше (только HTTP-API секретов). Лончер, если поднимает
        # прокси-секреты, передаёт готовый пул (общий CA + тот же store).
        self.proxies = proxies
        # Потолок ожидания активных хендлеров при stop(). None = дефолт aiohttp
        # (оркестратор не меняем). standalone ставит короткий: SIGINT посреди
        # висящего confirm не должен ждать зависший хендлер до дефолтных 60с.
        self.shutdown_timeout = shutdown_timeout
        self.port: int | None = None
        self._runner: web.AppRunner | None = None
        # Токен → (имя сессии, рабочий каталог). cwd снят при выдаче токена.
        self._tokens: dict[str, Ctx] = {}

    # ── жизненный цикл ──────────────────────────────────────────
    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/secrets", self._handle_secrets)
        app.router.add_post("/run", self._handle_run)
        app.router.add_post("/exec", self._handle_exec)
        app.router.add_post("/get", self._handle_get)
        runner_kw = {} if self.shutdown_timeout is None else {
            "shutdown_timeout": self.shutdown_timeout}
        self._runner = web.AppRunner(app, **runner_kw)
        await self._runner.setup()
        # Порт выдаёт ОС: свой сокет вместо TCPSite, чтобы узнать номер без
        # залезания в приватные поля aiohttp.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        await web.SockSite(self._runner, sock).start()
        logger.info("vault: демон на 127.0.0.1:%d", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._tokens.clear()
        if self.proxies is not None:
            await self.proxies.stop_all()

    # ── per-session прокси (§4.3/§4.5) ─────────────────────────
    async def start_session_proxy(self, session_name: str, secret_name: str) -> int:
        """Поднять MITM-прокси для (сессия, прокси-секрет) и вернуть его порт —
        лончер прописывает HTTP_PROXY из него. Требует сконфигурированный пул."""
        if self.proxies is None:
            raise RuntimeError("пул прокси не сконфигурирован (proxies=None)")
        return await self.proxies.start(session_name, secret_name)

    async def stop_session_proxies(self, session_name: str) -> None:
        """Снять все прокси сессии (teardown). Без пула — no-op."""
        if self.proxies is not None:
            await self.proxies.stop(session_name)

    @property
    def url(self) -> str:
        """URL демона для ~/.wallet.json сессии (провижн знает порт после start)."""
        return f"http://127.0.0.1:{self.port}"

    # ── токены сессий ───────────────────────────────────────────
    def issue_token(self, session_name: str, cwd: Path) -> str:
        """Выдать сессии токен, привязав к нему её рабочий каталог. Перевыдача
        (рестарт/повторный hook) отзывает прежний токен этой сессии."""
        token = secrets.token_urlsafe(32)
        self._tokens = {t: c for t, c in self._tokens.items() if c.name != session_name}
        self._tokens[token] = Ctx(session_name, Path(cwd))
        return token

    def revoke_session(self, session_name: str) -> None:
        self._tokens = {t: c for t, c in self._tokens.items() if c.name != session_name}

    def _auth(self, request: web.Request) -> Ctx | None:
        """Bearer-токен → Ctx. Сравнение constant-time (compare_digest), перебор
        без раннего выхода — тайминг не выдаёт «почти угадал»."""
        header = request.headers.get("Authorization", "")
        token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        token_b = token.encode("utf-8", "replace")
        found: Ctx | None = None
        for known, ctx in self._tokens.items():
            if secrets.compare_digest(known.encode("utf-8"), token_b):
                found = ctx
        if not token or found is None:
            return None
        return found

    # ── HTTP API ────────────────────────────────────────────────
    @_authed
    async def _handle_secrets(self, request: web.Request, ctx: Ctx) -> web.Response:
        """Список секретов, разрешённых этой сессии, — БЕЗ значений."""
        out = [
            {
                "name": s.name,
                "description": s.description,
                "commands": list(s.effective_commands),
                "confirm": s.confirm,
                # host — команда на хосте с его окружением; inject — значение в
                # env-переменную `env` дочернего процесса; shared — значение
                # ВЫДАётся сессии (wallet get/env), не прячется.
                "mode": s.mode,
                "env": s.env or None,
            }
            for s in self.store.load().values()
            if s.session_allowed(ctx.name)
        ]
        return web.json_response(out)

    @_authed
    async def _handle_get(self, request: web.Request, ctx: Ctx) -> web.Response:
        """Выдать сессии ЗНАЧЕНИЕ shared-секрета (dev-ключ, логин/пароль).

        Только для shared — host/inject значения не выдаются НИКОГДА (в этом их
        смысл). shared — про хранение вне чата/репо, не про сокрытие от модели."""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        name = str(data.get("secret", ""))
        secret = self.store.load().get(name)
        if secret is None or not secret.session_allowed(ctx.name):
            return web.json_response(
                {"error": "denied",
                 "reason": f"нет shared-секрета «{name}» для этой сессии (см. wallet ls)"},
                status=403,
            )
        if secret.mode != "shared":
            # Гейт по mode, НЕ по сырому `shared`: proxy-секрет (connector) может
            # нести shared=true в файле, но его значение выдавать нельзя — кред
            # живёт только в прокси (§4.4). store такой секрет уже не активирует,
            # это второй рубеж (defense-in-depth).
            return web.json_response(
                {"error": "denied",
                 "reason": f"секрет «{name}» не shared — значение не выдаётся "
                           "(для host/inject используй wallet run)"},
                status=403,
            )
        if secret.confirm:
            ok = await self.host.confirm(
                ctx.name,
                f"выдать значение shared-секрета «{name}» сессии",
                f"wallet get {name}",
            )
            if not ok:
                # Причину знает host: «оператор нажал ✗» и «спрашивать некого
                # (unattended)» — разные вещи, и модель должна видеть какая
                # именно (см. vault.host.deny_remedy).
                return web.json_response(
                    {"error": "denied",
                     "reason": deny_remedy(self.host) or "отклонено кнопкой подтверждения"},
                    status=403,
                )
        # Наблюдаемость: выдача видна строкой (без значения).
        await self.host.observe(
            ctx.name, f"🔐 <b>wallet get</b> <code>{html.escape(name)}</code>",
        )
        self.host.record(ctx.name, secret=name, cmd=f"get {name}", allowed=True)
        return web.json_response(
            {"name": name, "env": secret.env or None, "value": secret.value}
        )

    @_authed
    async def _handle_run(self, request: web.Request, ctx: Ctx) -> web.Response:
        """Явный вызов: `wallet run <name> -- <cmd>` — секрет задан именем."""
        try:
            body = await request.json()
            name = str(body["secret"])
            cmd = [str(c) for c in body["cmd"]]
            if not cmd:
                raise ValueError("пустая команда")
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad request"}, status=400)
        return await self._run_secret(ctx, self.store.load().get(name), cmd, name)

    @_authed
    async def _handle_exec(self, request: web.Request, ctx: Ctx) -> web.Response:
        """Прозрачный шлюз: `wallet exec <cmd>` (зовут обёртки в PATH песочницы)
        — секрет подбирается ПО КОМАНДЕ (чей `commands` её разрешает)."""
        try:
            body = await request.json()
            cmd = [str(c) for c in body["cmd"]]
            if not cmd:
                raise ValueError("пустая команда")
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "bad request"}, status=400)
        secret = self._resolve_secret(ctx.name, cmd)
        label = secret.name if secret is not None else os.path.basename(cmd[0])
        return await self._run_secret(ctx, secret, cmd, label)

    def _resolve_secret(self, session_name: str, cmd: list[str]) -> Secret | None:
        """Секрет, чьи commands разрешают эту команду для сессии (авто-подбор для
        /exec). Первый подходящий; None — если ни один не разрешает."""
        for s in self.store.load().values():
            if s.session_allowed(session_name) and s.command_allowed(cmd):
                return s
        return None

    async def _run_secret(self, ctx: Ctx, secret, cmd: list[str], label: str) -> web.Response:
        """Общий путь /run и /exec: policy (guard/deny/confirm) + наблюдаемость +
        выполнение на хосте + редакция вывода."""
        cmd_str = " ".join(cmd)
        all_secrets = self.store.load()
        # Решение policy (guard/deny/sessions/commands) — чистый движок vault.
        # Подтверждение кнопкой — side-effect здесь (движок лишь помечает needs_confirm).
        verdict = evaluate(secret, cmd, ctx.name, guard_on=self.guard_on)
        allowed = verdict.allowed
        if verdict.needs_confirm:
            allowed = await self.host.confirm(
                ctx.name, f"{label} → {cmd_str[:200]}", cmd_str,
            )
        # Наблюдаемость: КАЖДАЯ попытка видна строкой в бабле; отдельное
        # уведомление — только на ОТКАЗ (нужно внимание).
        cmd_disp = f"{label} → {cmd_str[:120]}"
        bubble_line = f"🔐 <b>wallet</b> <code>{html.escape(cmd_disp)}</code>"
        await self.host.observe(ctx.name, bubble_line)
        self.host.record(ctx.name, secret=label, cmd=cmd_str, allowed=bool(allowed))
        if not allowed:
            # verdict.reason покрывает policy-отказ; None → отказ пришёл от
            # confirm-кнопки (движок пропустил policy, но оператор нажал ✗).
            reason = verdict.reason if verdict.reason is not None else (
                deny_remedy(self.host) or "отклонено кнопкой подтверждения")
            # Operator-notice — только для отказов, требующих его внимания.
            # `gh auth token`/`--show-token` (печать токена) НЕ шлём: отказ
            # самокорректирующийся, а фоновый поллер Claude Code зовёт её
            # периодически — иначе спам в чат на каждый опрос.
            if not _prints_token(cmd):
                await self.host.notify_denied(ctx.name, cmd_disp)
            return web.json_response({"error": "denied", "reason": reason}, status=403)
        code, out, err = await self._execute(ctx, secret, cmd)
        values = [s.value for s in all_secrets.values()]
        return web.json_response(
            {"code": code, "stdout": _redact(out, values), "stderr": _redact(err, values)}
        )

    async def _execute(self, ctx: Ctx, secret: Secret, cmd: list[str]) -> tuple[int, bytes, bytes]:
        """Запустить команду НА ХОСТЕ под секретом — делегат в vault.execute.
        cwd берётся из контекста токена (снят при выдаче, без перерезолва)."""
        return await run_secret_command(
            cmd, secret,
            cwd=ctx.cwd,
            all_secrets=self.store.load(),
            session_name=ctx.name,
        )


__all__ = ["VaultDaemon", "Ctx"]
