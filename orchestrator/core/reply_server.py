"""HTTP-сервер оркестратора.

POST /reply         — ответы Claude (тул reply_to_user через channel_server)
POST /event/{name}  — события PreToolUse-хука Claude Code (вызовы инструментов)
POST /stop/{name}   — конец хода (Stop-хук) — фолбэк на «потерянный финал»
"""

from __future__ import annotations

import logging
import secrets
from typing import Awaitable, Callable

from aiohttp import web

from ..config import Config
from .errors import ReplyRejected

logger = logging.getLogger(__name__)

ReplyHandler = Callable[[dict], Awaitable[None]]
NamedHandler = Callable[[str, dict], Awaitable[None]]


def _make_route(handler, *, needs_name: bool, swallow_errors: bool, what: str):
    """Собрать aiohttp-хендлер: общий json-parse (400 на битом теле) + dispatch +
    политика ошибок в ОДНОМ месте (раньше — ×4 копии пролога).

    `swallow_errors=True` — краш хендлера логируется и отдаётся 200 (хук-эндпоинты
    /event и /stop: хук НЕ должен блокировать Claude); иначе 500 (/reply,
    /permission). Флаг ЯВНЫЙ — это security-значимое различие политик, его видно
    на месте регистрации роута. `needs_name` — брать ли `{name}` из пути."""
    async def route(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except ValueError:
            return web.Response(status=400, text="invalid json")
        args = (request.match_info["name"], payload) if needs_name else (payload,)
        try:
            await handler(*args)
        except ReplyRejected as e:
            # Не авария, а промах модели адресом: текст исключения написан ДЛЯ
            # НЕЁ и должен доехать до неё как результат тула. 422 (а не 500,
            # который канал показал бы как «Failed: HTTP Error 500») — тело
            # запроса синтаксически валидно, но адресат не существует.
            logger.warning("%s отклонён: %s", what, e)
            return web.Response(status=422, text=str(e))
        except Exception:
            logger.exception("Ошибка обработки %s", what)
            if not swallow_errors:
                return web.Response(status=500, text="handler error")
        return web.Response(text="OK")
    return route


async def start_reply_server(
    config: Config,
    reply_handler: ReplyHandler,
    tool_event_handler: NamedHandler,
    permission_handler: NamedHandler,
    stop_handler: NamedHandler,
) -> web.AppRunner:
    """Поднять сервер; вернуть runner (закрывать через runner.cleanup())."""

    # Один общий секрет на все эндпоинты: внутренний API живёт на 127.0.0.1,
    # но без токена любой локальный процесс (и вкладка браузера через DNS
    # rebinding) мог бы POST /reply с file_path и выгрузить任意 файл в чат
    # (REVIEW.md S1). Канал-сервер и curl-хук пробрасывают тот же токен.
    expected_auth = f"Bearer {config.orch_token}".encode()

    @web.middleware
    async def _auth(request: web.Request, handler):
        sent = request.headers.get("Authorization", "").encode("utf-8", "replace")
        # constant-time сравнение; на байтах — безопасно при любом (в т.ч.
        # не-ASCII) вводе, без TypeError.
        if not secrets.compare_digest(sent, expected_auth):
            return web.Response(status=401, text="unauthorized")
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    # swallow_errors=True → 200 даже при краше хендлера: /event и /stop — это хуки
    # Claude Code, они НЕ должны блокировать модель. /reply и /permission ждут
    # результата → 500 при краше (см. _make_route).
    app.router.add_post(
        "/reply", _make_route(reply_handler, needs_name=False, swallow_errors=False, what="reply")
    )
    app.router.add_post(
        "/event/{name}",
        _make_route(tool_event_handler, needs_name=True, swallow_errors=True, what="tool-события"),
    )
    app.router.add_post(
        "/permission/{name}",
        _make_route(
            permission_handler, needs_name=True, swallow_errors=False, what="permission_request"
        ),
    )
    app.router.add_post(
        "/stop/{name}",
        _make_route(stop_handler, needs_name=True, swallow_errors=True, what="Stop-события"),
    )

    runner = web.AppRunner(app)
    await runner.setup()
    # Bind на orch_host (host-side, дефолт 127.0.0.1) — оно ДОЛЖНО быть
    # host-resolvable. Под agent-vm гость достучится сюда через host-gateway имя
    # (guest_orch_host в .mcp.json/хуках), которое microsandbox+--allow-host мапят
    # на этот хостовый loopback. Т.е. bind тут НЕ гостевое имя (оно бы упало
    # gaierror), а guest-facing адрес выводится отдельно — см. Config.guest_orch_host.
    site = web.TCPSite(runner, host=config.orch_host, port=config.orch_port)
    await site.start()
    logger.info("Reply-сервер слушает %s:%d", config.orch_host, config.orch_port)
    return runner
