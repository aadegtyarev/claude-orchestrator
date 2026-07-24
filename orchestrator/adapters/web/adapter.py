"""Веб-адаптер: локальный веб-интерфейс оркестратора поверх aiohttp.

Реализует Transport (core/transport.py): вместо привязок «сессия ↔ топик»
у веба адресация по имени сессии — все доставки транслируются JSON-событиями
во все подключённые WebSocket-клиенты, а команды пользователя приходят через
REST API (/api/*). Статика (SPA на vanilla JS) отдаётся без токена, но данные
без авторизации не получить: все /api/* и WS требуют токен.

Авторизация как у Jupyter: токен из WEB_TOKEN (или сгенерированный на запуск)
печатается в лог готовой ссылкой /?token=…; заход по ней ставит HttpOnly-cookie,
дальше работают cookie, заголовок Authorization: Bearer или ?token=.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import logging
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from ...config import Config
from ...core.app import OrchestratorCore, UserError
from ...core.mdrender import md_to_html
from ...core.sessions import Session
from ...core.transport import Origin, PermissionRequest

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "orch_web_token"


def with_session(handler):
    """Декоратор для h_*-хендлеров с адресацией по {name}: резолвит сессию через
    `_session_of`, отдаёт 404 «session not found» если её нет, иначе зовёт
    хендлер с готовым `session`. Убирает ~19 копий guard-пролога — правка текста
    ошибки/кода статуса теперь в одном месте."""
    @functools.wraps(handler)
    async def wrapper(self, request: web.Request):
        session = self._session_of(request)
        if session is None:
            return self._err("session not found", 404)
        return await handler(self, request, session)
    return wrapper


class WebAdapter:
    name = "web"
    requires_binding = False  # адресация по имени сессии, поверхность не нужна

    def __init__(self, config: Config, core: OrchestratorCore):
        self.config = config
        self.core = core
        self.manager = core.manager
        self.t = core.t
        # Пустой WEB_TOKEN = сгенерировать на запуск (в start(), как Jupyter).
        self._token = config.web_token
        self._ws_clients: set[web.WebSocketResponse] = set()
        # ref статус-бабла — просто счётчик: клиенту важна только уникальность
        # в рамках процесса, само состояние бабла держит ядро.
        self._bubble_seq = itertools.count(1)
        # Последнее состояние бабла на сессию — для /bubble (снапшот при
        # переключении/реконнекте): {ref, html, stop_button}.
        self._bubble_state: dict[str, dict] = {}
        self._runner: web.AppRunner | None = None
        self.app = self._build_app()

    # ── Transport: жизненный цикл ───────────────────────────────

    async def start(self) -> None:
        if not self._token:
            self._token = secrets.token_urlsafe(18)
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.web_host, self.config.web_port)
        await site.start()
        logger.info(
            "Веб-интерфейс: http://%s:%s/?token=%s",
            self.config.web_host, self.config.web_port, self._token,
        )

    def public_url(self) -> str:
        """URL веб-интерфейса с токеном (для команды /orchestrator_web). Для
        host 0.0.0.0/:: показываем 127.0.0.1 — по нему открывают локально."""
        host = self.config.web_host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self.config.web_port}/?token={self._token}"

    async def stop(self) -> None:
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ── Transport: привязка сессий ──────────────────────────────

    async def bind_session(self, session: Session) -> str | None:
        # У веба нет поверхности на сессию — адресация по имени, binding не нужен.
        return None

    async def unbind_session(self, session: Session, address: str) -> None:
        return None

    # ── Transport: доставка (всё — событиями в WS) ──────────────

    async def _broadcast(self, event: dict) -> None:
        """Разослать событие всем WS-клиентам параллельно и с таймаутом.

        Раньше слали последовательным await: один живой-но-зависший клиент
        (усыплённая вкладка, полный TCP-буфер — heartbeat заметит лишь через
        цикл) блокировал send_json, а через него — весь _broadcast и все
        доставки ядра ПО ВСЕМ адаптерам (включая Telegram), т.к. ядро обходит
        транспорты последовательно. gather + wait_for изолируют такого клиента.
        """
        clients = list(self._ws_clients)
        if not clients:
            return

        async def _one(ws):
            try:
                await asyncio.wait_for(ws.send_json(event), timeout=5)
            except Exception:
                self._ws_clients.discard(ws)

        await asyncio.gather(*(_one(ws) for ws in clients))

    async def deliver_text(
        self, session: Session, text: str, *, origin: Origin | None = None,
        intermediate: bool = False,
    ) -> None:
        await self._broadcast({
            "type": "reply", "session": session.name, "text": text,
            "html": md_to_html(text), "intermediate": intermediate,
        })

    async def deliver_file(
        self, session: Session, path: Path, caption: str, *,
        origin: Origin | None = None,
    ) -> None:
        # Файл не гоняем через WS — клиент скачает по /api/…/file (jail ядра
        # проверяется и там, путь из события — только ссылка).
        await self._broadcast({
            "type": "file", "session": session.name, "path": str(path),
            "name": path.name, "caption": caption,
        })

    async def notify(self, session: Session | None, text: str) -> None:
        await self._broadcast({
            "type": "notice", "session": session.name if session else None,
            "text": text, "html": md_to_html(text),
        })

    async def typing(self, session: Session) -> bool:
        await self._broadcast({"type": "typing", "session": session.name})
        # Некому слать — циклу typing нет смысла жить ради веба.
        return bool(self._ws_clients)

    async def bubble_post(
        self, session: Session, html: str, *, stop_button: bool, unblock_active: bool = False
    ) -> str | None:
        ref = str(next(self._bubble_seq))
        # Запоминаем последнее состояние бабла: клиент, переключившийся на
        # работающую сессию (или переподключившийся), запросит его через
        # /bubble — иначе бабл и кнопки ⏹/⛔ были бы невидимы до след. события.
        state = {
            "ref": ref, "html": html, "stop_button": stop_button, "unblock_active": unblock_active,
        }
        self._bubble_state[session.name] = state
        await self._broadcast({"type": "bubble", "session": session.name, **state})
        return ref

    async def bubble_edit(
        self, session: Session, ref: str, html: str, *, stop_button: bool,
        unblock_active: bool = False,
    ) -> None:
        state = {
            "ref": ref, "html": html, "stop_button": stop_button, "unblock_active": unblock_active,
        }
        self._bubble_state[session.name] = state
        await self._broadcast({"type": "bubble", "session": session.name, **state})

    async def bubble_finish(self, session: Session, ref: str, *, delete: bool) -> None:
        self._bubble_state.pop(session.name, None)
        await self._broadcast({
            "type": "bubble_close", "session": session.name, "ref": ref, "delete": delete,
        })

    async def bubble_freeze(self, session: Session, ref: str) -> None:
        self._bubble_state.pop(session.name, None)
        await self._broadcast({
            "type": "bubble_freeze", "session": session.name, "ref": ref,
        })

    async def permission_prompt(
        self, session: Session, request: PermissionRequest
    ) -> None:
        await self._broadcast({
            "type": "perm_request", "session": session.name,
            "request_id": request.request_id, "tool": request.tool,
            "description": request.description, "preview": request.preview,
            # Третья кнопка («разрешить навсегда», ASK-грант кошелька) — только
            # если её предложил запросивший; иначе поле пустое и фронт рисует
            # прежние две. Без этого грант был бы доступен в Telegram и
            # недоступен в вебе — разное поведение одного вердикта.
            "always_label": request.always_label or "",
        })

    async def permission_resolved(
        self, session: Session, request_id: str, behavior: str, via: str
    ) -> None:
        await self._broadcast({
            "type": "perm_resolved", "session": session.name,
            "request_id": request_id, "behavior": behavior, "via": via,
        })

    # ── HTTP: приложение и авторизация ──────────────────────────

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        r = app.router
        r.add_get("/", self.h_index)
        r.add_static("/static/", STATIC_DIR, name="static")
        r.add_get("/api/ws", self.h_ws)
        r.add_get("/api/sessions", self.h_sessions)
        r.add_post("/api/sessions", self.h_create)
        r.add_post("/api/sessions/{name}/message", self.h_message)
        r.add_get("/api/sessions/{name}/bg", self.h_bg)
        r.add_post("/api/sessions/{name}/close", self.h_close)
        r.add_post("/api/sessions/{name}/clear", self.h_clear)
        r.add_post("/api/sessions/{name}/delete", self.h_delete)
        r.add_post("/api/sessions/{name}/compact", self.h_compact)
        r.add_post("/api/sessions/{name}/stop", self.h_stop)
        r.add_post("/api/sessions/{name}/interrupt", self.h_interrupt)
        r.add_post("/api/sessions/{name}/unblock", self.h_unblock)
        r.add_post("/api/sessions/{name}/model", self.h_model)
        r.add_get("/api/sessions/{name}/bubble", self.h_bubble)
        r.add_get("/api/sessions/{name}/stats", self.h_stats)
        r.add_get("/api/sessions/{name}/usage", self.h_usage)
        r.add_get("/api/sessions/{name}/history", self.h_history)
        r.add_post("/api/sessions/{name}/permission", self.h_permission)
        r.add_post("/api/sessions/{name}/upload", self.h_upload)
        r.add_get("/api/sessions/{name}/file", self.h_file)
        r.add_get("/api/sessions/{name}/log", self.h_log)
        r.add_post("/api/sessions/{name}/bash", self.h_bash)
        r.add_post("/api/sessions/{name}/bash_input", self.h_bash_input)
        r.add_get("/api/ls", self.h_ls)
        r.add_get("/api/skills", self.h_skills)
        return app

    def _authorized(self, request: web.Request) -> bool:
        """Токен из cookie, Authorization: Bearer или ?token=. Сравнение
        constant-time (compare_digest) — тайминг не выдаёт префикс токена."""
        if not self._token:
            return False
        candidates = []
        if (c := request.cookies.get(COOKIE_NAME)) is not None:
            candidates.append(c)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            candidates.append(auth[len("Bearer "):])
        if (q := request.query.get("token")) is not None:
            candidates.append(q)
        # Сравниваем на байтах: compare_digest на str бросает TypeError при
        # не-ASCII вводе (кривой cookie/заголовок) → был бы 500 вместо чистого
        # 401. errors="replace" делает вход всегда байтами (fail-closed).
        expected = self._token.encode("utf-8")
        return any(
            secrets.compare_digest(c.encode("utf-8", "replace"), expected)
            for c in candidates
        )

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Статика открыта (SPA грузится без токена), данные — только /api/*
        # и только с токеном.
        if request.path.startswith("/api/") and not self._authorized(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def h_index(self, request: web.Request) -> web.StreamResponse:
        """SPA. Заход по /?token=<верный> ставит HttpOnly-cookie — дальше
        браузер авторизован без токена в каждой ссылке (как у Jupyter)."""
        resp = web.FileResponse(STATIC_DIR / "index.html")
        tok = request.query.get("token")
        # Сравнение на байтах (как _authorized): compare_digest на str бросает
        # TypeError при не-ASCII ?token= → был бы 500 вместо тихого игнора.
        if tok and self._token and secrets.compare_digest(
            tok.encode("utf-8", "replace"), self._token.encode("utf-8")
        ):
            resp.set_cookie(
                COOKIE_NAME, self._token, httponly=True, samesite="Lax", path="/"
            )
        return resp

    # ── HTTP: помощники ─────────────────────────────────────────

    @staticmethod
    def _err(text: str, status: int = 400) -> web.Response:
        return web.json_response({"error": text}, status=status)

    def _session_of(self, request: web.Request) -> Session | None:
        return self.manager.get(request.match_info.get("name", ""))

    @staticmethod
    async def _json_body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _session_info(self, session: Session) -> dict:
        running = bool(session.running)
        return {
            "name": session.name,
            "title": session.title,
            "status": self.core.session_status(session),
            "model": session.model,
            "linked_path": session.linked_path,
            "running": running,
            "uptime": (
                self.core.fmt_duration(time.time() - session.started_at) if running else None
            ),
        }

    def _sessions_info(self) -> list[dict]:
        return [self._session_info(s) for s in self.manager.list_all()]

    async def _sessions_changed(self) -> None:
        # Клиенту достаточно сигнала — список он перезапрашивает сам.
        await self._broadcast({"type": "sessions_changed"})

    async def session_state_changed(self, session: "Session | None") -> None:
        """Transport-хук: ядро сообщило о переходе состояния сессий (в т.ч.
        инициированном не вебом — Telegram/idle/смерть). Шлём клиентам сигнал
        обновить список — без этого веб залипал на чужих изменениях до F5."""
        await self._sessions_changed()

    def _origin(self) -> Origin:
        # У веба нет reply-цитирования — токен произвольный, ядро его не трактует.
        return Origin(self.name, "0")

    # ── HTTP: WebSocket ─────────────────────────────────────────

    async def h_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        try:
            await ws.send_json({
                "type": "hello",
                "sessions": self._sessions_info(),
                # Что реально работает при этой конфигурации (решает ядро):
                # выключенная фича не должна оставлять артефактов в UI —
                # кнопка, ведущая к отказу, это ложное обещание.
                "features": self.core.features(),
            })
            async for msg in ws:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
                # Входящие сообщения клиент не шлёт (всё через REST) — игнор.
        finally:
            self._ws_clients.discard(ws)
        return ws

    # ── HTTP: сессии ────────────────────────────────────────────

    async def h_sessions(self, request: web.Request) -> web.Response:
        return web.json_response(self._sessions_info())

    async def h_create(self, request: web.Request) -> web.Response:
        data = await self._json_body(request)
        title = str(data.get("title", "")).strip()
        # data.get("path") может быть None (пустое поле формы шлёт null) —
        # str(None) дал бы литерал "None" и сессия линковалась бы в каталог
        # ./None. Берём path только если это непустая строка.
        raw_path = data.get("path")
        path = raw_path.strip() or None if isinstance(raw_path, str) else None
        if not title:
            return self._err("title required")
        try:
            session = await self.core.create_session(title, path)
        except UserError as e:
            return self._err(str(e))
        except Exception:
            logger.exception("Веб: ошибка создания сессии %s", title)
            return self._err(self.t("create_fail", error="internal error"), 500)
        await self._sessions_changed()
        return web.json_response(self._session_info(session))

    @with_session
    async def h_message(self, request: web.Request, session: Session) -> web.Response:
        data = await self._json_body(request)
        text = str(data.get("text", ""))
        if not text.strip():
            return self._err("text required")
        # Слэш-команда — только однострочный ввод, начинающийся с '/':
        # многострочный текст с '/' в первой строке (вставленный лог/diff) —
        # это обычное сообщение, а не команда Claude Code (она всегда одна
        # строка). Иначе первая строка ушла бы в PTY, остальное потерялось.
        is_slash = text.lstrip().startswith("/") and "\n" not in text.strip()
        try:
            state = await self.core.ensure_running(session)
            if is_slash:
                await self.core.slash_command(session, text.strip())
            else:
                await self.core.user_message(session, text, self._origin())
        except UserError as e:
            return self._err(str(e))
        except Exception:
            logger.exception("Веб: ошибка сообщения в сессию %s", session.name)
            return self._err(self.t("forward_fail", error="internal error"), 500)
        if state != "running":
            await self._sessions_changed()  # resume — статус в списке поменялся
        return web.json_response({"ok": True, "slash": is_slash})

    @with_session
    async def h_close(self, request: web.Request, session: Session) -> web.Response:
        await self.core.close_session(session)
        await self._sessions_changed()
        return web.json_response({"ok": True})

    @with_session
    async def h_clear(self, request: web.Request, session: Session) -> web.Response:
        try:
            await self.core.clear_session(session)
        except UserError as e:
            return self._err(str(e))
        await self._sessions_changed()
        return web.json_response({"ok": True})

    @with_session
    async def h_delete(self, request: web.Request, session: Session) -> web.Response:
        await self.core.delete_session(session)
        await self._sessions_changed()
        return web.json_response({"ok": True})

    @with_session
    async def h_compact(self, request: web.Request, session: Session) -> web.Response:
        try:
            await self.core.compact(session)
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"ok": True})

    @with_session
    async def h_stop(self, request: web.Request, session: Session) -> web.Response:
        try:
            await self.core.request_report(session, self._origin())
        except Exception as e:
            logger.error("Сессия %s: не удалось отправить стоп: %s", session.name, e)
            return self._err(str(e))
        return web.json_response({"ok": True})

    @with_session
    async def h_interrupt(self, request: web.Request, session: Session) -> web.Response:
        try:
            await self.core.hard_stop(session)
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"ok": True})

    @with_session
    async def h_unblock(self, request: web.Request, session: Session) -> web.Response:
        try:
            await self.core.unblock(session)
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"ok": True})

    @with_session
    async def h_model(self, request: web.Request, session: Session) -> web.Response:
        data = await self._json_body(request)
        model = str(data.get("model", "")).strip()
        if not model:
            return self._err("model required")
        try:
            resumed = await self.core.switch_model(session, model)
        except UserError as e:
            return self._err(str(e))
        await self._sessions_changed()
        return web.json_response({"resumed": resumed})

    @with_session
    async def h_stats(self, request: web.Request, session: Session) -> web.Response:
        # Блокирующее чтение транскрипта — в поток, event loop не стопорим.
        text = await asyncio.to_thread(self.core.stats_text, session)
        return web.json_response({"text": text})

    @with_session
    async def h_usage(self, request: web.Request, session: Session) -> web.Response:
        try:
            text = await self.core.usage_text(session)
        except UserError as e:
            return self._err(str(e))  # остановленная сессия и т.п.
        return web.json_response({"text": text})  # null — распарсить не удалось

    @with_session
    async def h_bubble(self, request: web.Request, session: Session) -> web.Response:
        """Снапшот активного статус-бабла (или null) — клиент запрашивает при
        переключении/реконнекте, иначе бабл и кнопки ⏹/⛔ невидимы до
        следующего события."""
        return web.json_response(self._bubble_state.get(session.name))

    @with_session
    async def h_history(self, request: web.Request, session: Session) -> web.Response:
        def _render() -> list[dict]:
            items = []
            for ev in self.core.history(session.name):
                ev = dict(ev)
                if ev.get("kind") in ("reply", "intermediate", "notice"):
                    ev["html"] = md_to_html(str(ev.get("text", "")))
                items.append(ev)
            return items

        # md_to_html по до 300 событиям — в поток, чтобы клик по сессии не
        # стопорил event loop (regex-рендер каждого блока).
        return web.json_response(await asyncio.to_thread(_render))

    @with_session
    async def h_permission(self, request: web.Request, session: Session) -> web.Response:
        data = await self._json_body(request)
        request_id = str(data.get("request_id", ""))
        behavior = str(data.get("behavior", ""))
        # allow_always — постоянный грант ASK кошелька (§4.6). Ядро само проверит,
        # что у ЭТОГО запроса третья кнопка предлагалась (иначе → отказ), поэтому
        # здесь достаточно пропустить значение как допустимое.
        if not request_id or behavior not in ("allow", "deny", "allow_always"):
            return self._err("request_id and behavior allow|deny|allow_always required")
        try:
            handled = await self.core.permission_verdict(
                session, request_id, behavior, via=self.name
            )
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"handled": handled})

    # ── HTTP: файлы ─────────────────────────────────────────────

    @with_session
    async def h_upload(self, request: web.Request, session: Session) -> web.Response:
        try:
            reader = await request.multipart()
        except Exception:
            return self._err("multipart body required")
        caption, dest = "", None
        # incoming-каталог — единый источник правды ядра (тот же, что видит
        # jail send_file); иначе загрузка легла бы вне whitelist'а скачивания.
        incoming = self.core.incoming_dir(session)
        async for part in reader:
            if part.name == "caption":
                caption = (await part.text()).strip()
            elif part.name == "file" and part.filename:
                # .name отрезает возможные ../ из имени файла клиента.
                fname = Path(part.filename).name or f"file_{int(time.time())}"
                await asyncio.to_thread(incoming.mkdir, parents=True, exist_ok=True)
                dest = incoming / fname
                tmp = incoming / f".{fname}.{int(time.time())}.part"
                # Запись — в поток: синхронный f.write per-chunk на event loop
                # заморозил бы все сессии на время большой загрузки. Пишем во
                # временный файл и атомарно переименовываем — при обрыве
                # (закрыл вкладку) в incoming не осядет обрезок, который модель
                # приняла бы за целый файл.
                try:
                    with tmp.open("wb") as f:
                        while chunk := await part.read_chunk(65536):
                            await asyncio.to_thread(f.write, chunk)
                    await asyncio.to_thread(tmp.replace, dest)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
        if dest is None:
            return self._err("file field required")
        text = self.t("file_received", path=dest)
        if caption:
            text += "\n" + self.t("file_caption", caption=caption)
        try:
            await self.core.ensure_running(session)
            await self.core.user_message(session, text, self._origin())
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"ok": True, "path": str(dest)})

    @with_session
    async def h_file(self, request: web.Request, session: Session) -> web.StreamResponse:
        raw = request.query.get("path", "")
        if not raw:
            return self._err("path required")
        path = Path(raw).expanduser()
        # Jail ядра: наружу отдаются только файлы рабочих папок сессии —
        # иначе ссылкой можно было бы выкачать любой файл хоста.
        if not self.core.path_in_workspace(path, session):
            return self._err("path outside session workspace", 403)
        if not path.is_file():
            return self._err("file not found", 404)
        # ВСЕГДА как вложение + nosniff: иначе присланный моделью report.html с
        # <script> отрендерился бы на origin веб-интерфейса и через cookie угнал
        # бы API (bash/approve/чтение сессий). Ссылки на файлы и так download.
        return web.FileResponse(path, headers={
            "Content-Disposition": f'attachment; filename="{path.name}"',
            "X-Content-Type-Options": "nosniff",
        })

    @with_session
    async def h_log(self, request: web.Request, session: Session) -> web.StreamResponse:
        """Скачать полный claude.log сессии (для отладки формата Claude Code).
        Отдаём как вложение с осмысленным именем; путь фиксирован (session_dir),
        поэтому jail не нужен."""
        log = session.session_dir / "claude.log"
        if not log.is_file():
            return self._err("log not found", 404)
        return web.FileResponse(log, headers={
            "Content-Disposition": f'attachment; filename="{session.name}-claude.log"',
            "Content-Type": "text/plain; charset=utf-8",
        })

    # ── HTTP: bash-терминал ─────────────────────────────────────

    @with_session
    async def h_bash(self, request: web.Request, session: Session) -> web.Response:
        data = await self._json_body(request)
        cmd = str(data.get("cmd", "")).strip()
        if not cmd:
            return self._err("cmd required")
        key = self.core.bash_key(session, "web")
        sname = session.name

        async def on_update(html_text: str, done: bool) -> None:
            await self._broadcast(
                {"type": "bash", "session": sname, "html": html_text, "done": done}
            )

        try:
            # Держим запрос до конца команды: busy-конфликт отдаётся кодом 400,
            # а стрим вывода клиент всё равно получает по WS.
            await self.core.run_bash(key, session, cmd, on_update)
        except UserError as e:
            return self._err(str(e))
        return web.json_response({"ok": True})

    @with_session
    async def h_bash_input(self, request: web.Request, session: Session) -> web.Response:
        data = await self._json_body(request)
        text = str(data.get("text", ""))
        key = self.core.bash_key(session, "web")
        if not self.core.bash_input(key, text):
            return self._err(self.t("bash_not_running"))
        return web.json_response({"ok": True})

    # ── HTTP: справки ───────────────────────────────────────────

    async def h_ls(self, request: web.Request) -> web.Response:
        path = request.query.get("path") or None
        return web.json_response({"text": self.core.ls_text(path)})

    @with_session
    async def h_bg(self, request: web.Request, session: Session) -> web.Response:
        """Фоновые процессы/кроны сессии (снимок из последнего Stop-хука)."""
        return web.json_response({
            "text": self.core.bg_text(session),
            "background_tasks": session.background_tasks,
            "session_crons": session.session_crons,
        })

    async def h_skills(self, request: web.Request) -> web.Response:
        skills = await asyncio.to_thread(self.core.collect_skills)
        return web.json_response(
            [{"name": name, "description": desc} for name, desc in skills]
        )
