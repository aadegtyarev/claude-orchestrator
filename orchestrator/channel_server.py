#!/usr/bin/env python3
"""MCP channel-сервер для Claude Code — raw JSON-RPC 2.0 на stdio.

Запускается самим Claude Code как stdio-подпроцесс (через .mcp.json).
Raw JSON-RPC, а не MCP SDK: SDK не умеет push-уведомления вне запроса,
а channels-режим построен именно на них.

Потоки данных:
  launcher ──HTTP POST /notify──> этот процесс ──JSON-RPC push──> Claude
  Claude ──tools/call reply_to_user──> этот процесс ──HTTP POST /reply──> launcher
"""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request

# Канал общается с оркестратором ТОЛЬКО по stdlib (urllib + http.server), без
# aiohttp: под SANDBOX=agent-vm channel_server живёт ВНУТРИ гостя microVM, где
# aiohttp не установлен, а python-stdlib есть везде. Один канал на оба движка —
# см. docs/agent-vm-integration.md.
#
# POST'ы к оркестратору форсим БЕЗ прокси (aiohttp прокси не использовал, а urllib
# по умолчанию уважает http_proxy env): 127.0.0.1 (bwrap) и host.microsandbox.internal
# (agent-vm) должны идти напрямую.
_HTTP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CHANNEL_PORT = int(os.environ.get("CHANNEL_PORT", "18761"))
# Интерфейс bind push-сервера. bwrap: 127.0.0.1 (общий loopback с хостом).
# agent-vm: 0.0.0.0 — docker-style `--publish` форвардит на сетевой интерфейс
# гостя, а не на loopback, поэтому канал на 127.0.0.1 в госте недостижим с хоста.
# Гость сетево-изолирован (public_only) + токен ORCH_TOKEN, так что 0.0.0.0 ок.
CHANNEL_HOST = os.environ.get("CHANNEL_HOST", "127.0.0.1")
SESSION_NAME = os.environ.get("SESSION_NAME", "default")
ORCH_HOST = os.environ.get("ORCH_HOST", "127.0.0.1")
ORCH_PORT = int(os.environ.get("ORCH_PORT", "18080"))
ORCH_URL = f"http://{ORCH_HOST}:{ORCH_PORT}/reply"
# Общий секрет с оркестратором (REVIEW.md S1). Приходят из env, который
# sessions.py кладёт в .mcp.json; пустой = оркестратор без токена (старый режим).
ORCH_TOKEN = os.environ.get("ORCH_TOKEN", "")

# Минимальный интервал между push-уведомлениями в Claude.
PUSH_INTERVAL = 0.5

logger = logging.getLogger("ch-srv")

# Контракт каналов: https://code.claude.com/docs/en/channels-reference
# capability строго {"experimental": {"claude/channel": {}}}, tools: {} — для
# reply-тула, claude/channel/permission — приём запросов разрешений (relay).
CAPABILITIES = {
    "experimental": {
        "claude/channel": {},
        "claude/channel/permission": {},
    },
    "tools": {},
}

# Попадает в системный промпт Claude (поле instructions в initialize).
INSTRUCTIONS = (
    f'Messages from the user arrive as <channel source="channel-{SESSION_NAME}" '
    'context_id="...">text</channel>. The channel connects you to the user\'s '
    "chat (Telegram, web console, ...). Always reply with the reply_to_user "
    "tool, passing the context_id attribute exactly as received. Use "
    "complete=false for short intermediate progress updates while you work and "
    "complete=true exactly once with the final answer.\n"
    "CRITICAL: plain text you write is INVISIBLE to the user — this channel "
    "only relays explicit reply_to_user tool calls, nothing else. If you "
    "stop working (end your turn) without having called reply_to_user since "
    "your last one, whatever you were thinking never reaches the user, even if "
    "it reads like a message to them (\"I'll continue once CI is green\", \"done "
    "for now\"). Before ending ANY turn — including turns that end while you're "
    "waiting on a background command, a CI run, or another async condition — "
    "call reply_to_user one more time (complete=false if you'll keep going "
    "when it resolves, complete=true if you're truly done) to state where things "
    "stand. Never let a turn end on bare text.\n"
    "The user only sees your tool calls and reply_to_user messages — nothing "
    "else. A long silent stretch (reading files, thinking, running a slow "
    "command) looks identical to being stuck from their side. So: right after "
    "you receive a message, send one short complete=false line — what you "
    "understand the task to be and what you're about to do first — before you "
    "start working. Then keep sending short complete=false updates (one "
    "sentence — what you're doing / what you found) whenever you're about to do "
    "something that takes a while, and again after any noteworthy finding — "
    "don't wait for the whole task to finish. Err toward more of these, not "
    "fewer.\n"
    "Keep these updates and any status check-in answers (e.g. \"how's it "
    "going?\", \"что как?\") brief — one or two sentences. The user wants a "
    "quick pulse, not an essay; save detail for the final answer.\n"
    "For any task with more than one step, FIRST send a numbered plan via "
    "reply_to_user (complete=false) before starting — in the user's language, "
    "e.g.:\n"
    "План работ:\n1. …\n2. …\n3. …\n"
    "Then, as you finish each item, send a short updated status marking what is "
    "done (✅) and what is in progress (🔄), e.g. '1. ✅ … 2. 🔄 … 3. …'. This "
    "lets the operator follow progress point by point. Keep it terse; don't "
    "repeat the full plan every time unless it changed.\n"
    "send_file_to_user is ONLY for when the user explicitly asks you to send "
    "them a file, image, or artifact. Files you create or edit while working stay "
    "in the project — do NOT push them to the chat automatically. When you write "
    "code or files, just report what you did via reply_to_user; attach a file "
    "only on explicit request (\"send me the file\", \"скинь файл\").\n"
    "You have NO interactive terminal — the user is only reachable through this "
    "channel. Never use plan mode, ExitPlanMode, or interactive question tools "
    "like AskUserQuestion: they would block invisibly. When you need a decision "
    "or clarification, ask the user via reply_to_user (list options as "
    "numbered choices) and stop your turn; the user's answer arrives as the next "
    "channel message. Permission prompts are handled automatically — just proceed."
)

# Авто-подсказка про кошелёк секретов: добавляется в системный промпт ТОЛЬКО
# когда сессии выдан ~/.wallet.json (MODULES=wallet + сессия в чьей-то policy).
# Смысл — прозрачный шлюз: обёртки в PATH (.wallet-bin) заворачивают gh/git/curl/
# ssh в кошелёк, поэтому модель просто зовёт инструмент как обычно, а токен
# подставляется на хосте. Промпт лишь называет, ЧТО доступно (из policy). Под
# bwrap $HOME подменён приватным домом сессии, файл лежит ровно по ~/.wallet.json.
# Значений секретов тут нет — только факт наличия; конкретику — из `wallet ls`.
def _wallet_catalog() -> str | None:
    """Каталог доступных сессии секретов ИЗ POLICY (демон /secrets), а не хардкод.

    Возвращает готовый текст «доступно: <секрет> — команды …» либо None, если
    кошелёк сессии не выдан. Список строится из secrets.toml (что оператор
    разрешил, то и в подсказке) — не зашиваем gh/git/ssh руками. Ошибка запроса
    → общий текст (кошелёк есть, конкретику возьми из `wallet ls`)."""
    wf = os.path.expanduser("~/.wallet.json")
    if not os.path.exists(wf):
        return None
    try:
        cfg = json.loads(open(wf, encoding="utf-8").read())
        req = urllib.request.Request(
            cfg["url"] + "/secrets",
            headers={"Authorization": "Bearer " + cfg["token"]},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            secrets_list = json.load(r)
    except Exception:
        return "Run `wallet ls` to see which secrets and commands you may use."
    if not secrets_list:
        return "Run `wallet ls` — this session currently has no wallet secrets."
    lines = []
    any_inject = any_shared = False
    for s in secrets_list:
        desc = f" ({s['description']})" if s.get("description") else ""
        mode = s.get("mode")
        if mode == "shared":
            any_shared = True
            how = f"`wallet get {s['name']}`"
            if s.get("env"):
                how += f" or `wallet env {s['name']}` (${s['env']})"
            lines.append(f"  • `{s['name']}`{desc} — SHARED value, get it with {how}")
            continue
        cmds = ", ".join(s.get("commands", [])) or "—"
        if mode == "inject" and s.get("env"):
            any_inject = True
            inj = f" [inject: use ${s['env']} in the command; ${s['env']}_FILE for a file]"
        else:
            inj = ""
        lines.append(f"  • `{s['name']}`{desc} — {cmds}{inj}")
    out = (
        "These tools are wired through the wallet TRANSPARENTLY — just run them "
        "NORMALLY (e.g. `gh pr create`, `git push`, `curl ...`); a wrapper on your "
        "PATH routes the call to the host with the right credential attached, and you "
        "never handle the token. They run on the host IN YOUR PROJECT DIRECTORY, so a "
        "`cd` in your shell does not move them. Use each tool's own watch (e.g. `gh "
        "run watch`) — do NOT wrap calls in a `while`/poll loop. `wallet ls` lists "
        "this again; `wallet help` has the full reference.\n"
        + "\n".join(lines)
    )
    if any_shared:
        out += (
            "\nShared secrets are values you MAY read and use (a dev API key for a "
            "service you build, a login/password to enter somewhere): `wallet get "
            "<name>` prints the value, `wallet env <name>` prints `export VAR=value` "
            "for `eval \"$(wallet env <name>)\"`. They live in the wallet so they "
            "aren't pasted in chat or committed — don't echo them into the chat."
        )
    if any_inject:
        # Inject-секрет отдаётся привычной env-переменной $ИМЯ (в песочнице там
        # маркер, реальное значение подставляется на хосте). Модель просто
        # использует $ИМЯ; значение читать/выводить не надо.
        out += (
            "\nInject secrets appear as an env var — use it as usual: the tool reads "
            "$NAME itself (gh/aws/kubectl), or you drop $NAME into the command "
            "(curl -H 'Authorization: Bearer $OPENAI_KEY'); use $NAME_FILE where a "
            "FILE path is needed (ssh -i $DEPLOY_KEY_FILE). In the sandbox $NAME "
            "holds only a marker — the real value is filled in on the host, you never "
            "see it. Don't try to print or transform the value."
        )
    return out


_WALLET_CATALOG = _wallet_catalog()
if _WALLET_CATALOG is not None:
    INSTRUCTIONS += (
        "\nSECRETS WALLET: some tools are wired to run with a host-side credential you "
        "cannot see. For the tools listed below, just RUN THEM NORMALLY — a wrapper on "
        "your PATH forwards the call to the host with the right secret attached, and any "
        "secret value is auto-redacted from the output. " + _WALLET_CATALOG + "\n"
        "For git this covers the network subcommands (push/fetch/pull/clone/…); local git "
        "(status/commit/diff/log) just runs in place. A secret's value reaches a command "
        "through its env var $NAME — the tool reads it (gh/aws/kubectl), or you put $NAME "
        "in the command (curl -H 'Authorization: Bearer $OPENAI_KEY'), or $NAME_FILE "
        "where a file is needed (ssh -i $DEPLOY_KEY_FILE). Keep these commands "
        "non-interactive (ssh `-o BatchMode=yes -o StrictHostKeyChecking=accept-new`).\n"
        "To force a specific secret, `wallet run <name> -- <cmd>` picks it by name; "
        "`wallet ls`/`wallet help` show what's available. Use the wallet only for a "
        "credential you do NOT have — one you already have (the user gave it to you, or "
        "you generated your own for a NEW resource) you just use directly. If the wallet "
        "can't do it (secret not listed, denied, or it keeps failing), don't improvise a "
        "workaround — tell the operator via reply_to_user with the exact command and the "
        "error.\n"
        "The wallet hides values by design: don't try to print or transform a token "
        "to see its value — if you think you need the raw value, you don't."
    )

TOOLS = [
    {
        "name": "reply_to_user",
        "description": (
            "Send the user a reply for a message received via this channel. "
            "Use complete=false liberally for short intermediate progress "
            "updates while you work — each one is sent as its own message, so "
            "the user can see you're alive and what you're doing/finding, not "
            "just a spinning status. When the task is done, call it exactly "
            "once with complete=true and the final answer — that is what the "
            "user keeps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context_id": {
                    "type": "string",
                    "description": (
                        "Context ID from the incoming notification "
                        "(e.g. telegram:demo:123:45:6). REQUIRED — pass it exactly as received."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Reply text to send to the user.",
                },
                "complete": {
                    "type": "boolean",
                    "description": (
                        "true — final answer (regular message), "
                        "false — intermediate progress (status bubble)."
                    ),
                    "default": False,
                },
            },
            "required": ["context_id", "text"],
        },
    },
    {
        "name": "send_file_to_user",
        "description": (
            "Send a local file (document, image, archive, ...) to the user. "
            "Use ONLY when the user explicitly asks to receive a "
            "file — do not push files you created while working. Max 50 MB."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "context_id": {
                    "type": "string",
                    "description": "Context ID from the incoming notification, exactly as received.",
                },
                "file_path": {
                    "type": "string",
                    "description": "Absolute path to an existing local file.",
                },
                "caption": {
                    "type": "string",
                    "description": "Optional short caption for the file.",
                },
            },
            "required": ["context_id", "file_path"],
        },
    },
]


class ChannelServer:
    def __init__(self) -> None:
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()
        self._initialized = asyncio.Event()
        self._stopped = asyncio.Event()
        self._last_push = 0.0
        # Ссылки на фоновые задачи: event loop держит task слабой ссылкой,
        # без сохранения задача может быть собрана GC до завершения.
        self._tasks: set[asyncio.Task] = set()
        # event loop и HTTP-сервер (поток) — заполняются в _start_http.
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {ORCH_TOKEN}"} if ORCH_TOKEN else {}

    async def _post(self, url: str, payload: dict, timeout: float) -> None:
        """POST JSON оркестратору (без прокси). Блокирующий urllib — в потоке,
        чтобы не тормозить event loop. Соединение на запрос (пул aiohttp-сессии
        снят вместе с зависимостью, REVIEW E1) — приемлемо: исходящих POST у канала
        немного (reply/permission). urllib.urlopen сам кидает HTTPError на 4xx/5xx
        (как aiohttp raise_for_status) → провал /reply даст isError модели."""
        def _do() -> None:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", **self._auth_headers},
                method="POST",
            )
            with _HTTP_OPENER.open(req, timeout=timeout) as r:
                r.read()
        await asyncio.to_thread(_do)

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        reader = await self._open_stdio()
        httpd = self._start_http()
        try:
            await self._serve_rpc(reader)
        finally:
            self._stopped.set()
            # shutdown() из ДРУГОГО потока, чем serve_forever (тот в ch-http
            # daemon-потоке) — корректно. В to_thread, чтобы блокирующее ожидание
            # (до poll_interval) не морозило event loop, пока in-flight /notify
            # ещё домостится через run_coroutine_threadsafe.
            await asyncio.to_thread(httpd.shutdown)
            httpd.server_close()

    # ── stdio / JSON-RPC ────────────────────────────────────────

    async def _open_stdio(self) -> asyncio.StreamReader:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        transport, protocol = await loop.connect_write_pipe(
            lambda: asyncio.streams.FlowControlMixin(), sys.stdout
        )
        self._writer = asyncio.StreamWriter(transport, protocol, None, loop)
        return reader

    async def _read_message(self, reader: asyncio.StreamReader) -> dict | None:
        """Одно JSON-RPC-сообщение (newline-delimited). None = поток закрыт.

        Без таймаута: канал обязан жить, пока жив Claude, даже если
        сообщений нет часами. Битая строка не роняет цикл.
        """
        while True:
            raw = await reader.readline()
            if not raw:
                return None
            # errors="replace": один битый байт не должен ронять весь RPC-цикл.
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("Пропускаю битую JSON-RPC строку: %s", e)

    async def _write_message(self, msg: dict) -> None:
        assert self._writer is not None
        payload = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            self._writer.write(payload)
            await self._writer.drain()

    async def _serve_rpc(self, reader: asyncio.StreamReader) -> None:
        # Handshake: initialize -> notifications/initialized
        init = await self._read_message(reader)
        if init is None or init.get("method") != "initialize":
            logger.error("Ожидался 'initialize', получено: %s", init)
            return
        await self._write_message({
            "jsonrpc": "2.0",
            "id": init.get("id", 0),
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": CAPABILITIES,
                "serverInfo": {"name": f"channel-{SESSION_NAME}", "version": "2.1.0"},
                "instructions": INSTRUCTIONS,
            },
        })

        # Клиент может прислать запрос (с id) ещё до notifications/initialized —
        # обслуживаем его и продолжаем ждать нотификацию, иначе он повиснет на
        # этом id (раньше сообщение молча глоталось).
        while True:
            notif = await self._read_message(reader)
            if notif is None:
                logger.warning("Соединение закрыто до notifications/initialized")
                return
            if notif.get("method") == "notifications/initialized":
                break
            if notif.get("id") is not None:
                await self._handle_request(
                    notif["id"], notif.get("method"), notif.get("params") or {}
                )
            else:
                logger.debug("До initialized пришло уведомление: %s", notif.get("method"))
        self._initialized.set()
        logger.info("Handshake завершён, канал '%s' активен", SESSION_NAME)

        while True:
            msg = await self._read_message(reader)
            if msg is None:
                logger.info("Claude закрыл соединение")
                return
            if msg.get("id") is not None:
                await self._handle_request(msg["id"], msg.get("method"), msg.get("params") or {})
            elif msg.get("method") == "notifications/claude/channel/permission_request":
                # Запрос разрешения от Claude Code — переслать в Telegram.
                self._spawn(self._relay_permission(msg.get("params") or {}))
            else:
                logger.debug("Уведомление: %s", msg.get("method"))

    async def _handle_request(self, msg_id: int, method: str | None, params: dict) -> None:
        if method == "tools/list":
            await self._write_message({
                "jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS},
            })
        elif method == "tools/call" and params.get("name") == "reply_to_user":
            args = params.get("arguments") or {}
            text = args.get("text") or ""
            if not text.strip():
                # Пустой text = молчаливая потеря ответа: пользователь получал
                # пустое сообщение, хотя модель ответ нарратила (наблюдалось под
                # agent-vm, «reply len=0»). Первопричина не установлена, но
                # пересылать пустоту бессмысленно при любой из них — просим
                # прислать текст и логируем сырые arguments для диагностики
                # (лог доезжает до хоста, в отличие от stderr гостя).
                logger.warning(
                    "reply_to_user с пустым text — отклоняю. Сырые arguments: %s",
                    json.dumps(args, ensure_ascii=False)[:2000],
                )
                await self._write_message({
                    "jsonrpc": "2.0", "id": msg_id,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": (
                            "Empty text — nothing was delivered to the user. "
                            "Call reply_to_user again with the actual message "
                            "text in the 'text' argument."
                        )}],
                    },
                })
                return
            # В фоне: пока оркестратор отвечает, read-loop должен читать stdin
            # (иначе блокируются permission_request и следующие запросы).
            self._spawn(self._forward_to_orchestrator(msg_id, {
                "context_id": args.get("context_id", ""),
                "text": text,
                "complete": bool(args.get("complete", False)),
            }, ok_text="Reply sent"))
        elif method == "tools/call" and params.get("name") == "send_file_to_user":
            args = params.get("arguments") or {}
            self._spawn(self._forward_to_orchestrator(msg_id, {
                "context_id": args.get("context_id", ""),
                "file_path": args.get("file_path", ""),
                "caption": args.get("caption", ""),
            }, ok_text="File sent"))
        elif method == "tools/call":
            await self._write_message({
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Unknown tool: {params.get('name')}"}],
                },
            })
        else:
            await self._write_message({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            })

    async def _forward_to_orchestrator(self, msg_id: int, payload: dict, ok_text: str) -> None:
        try:
            await self._post(ORCH_URL, payload, timeout=30)
            result = {"content": [{"type": "text", "text": f"{ok_text} (ctx={payload.get('context_id')})"}]}
        except urllib.error.HTTPError as e:
            # 422 = оркестратор отверг адресата (кривой/чужой context_id) и
            # положил в тело объяснение ДЛЯ МОДЕЛИ. Отдаём его дословно: иначе
            # она увидит бессмысленное «HTTP Error 422» и не поймёт, что
            # чинить. Прочие коды — обычная авария доставки.
            body = ""
            try:
                body = e.read().decode("utf-8", "replace").strip()
            except Exception:
                pass
            text = body if (e.code == 422 and body) else f"Failed: {e}"
            logger.error("Оркестратор отверг %s: %s %s", ORCH_URL, e.code, body[:300])
            result = {"isError": True, "content": [{"type": "text", "text": text}]}
        except Exception as e:
            logger.error("Не удалось передать оркестратору: %s", e)
            result = {
                "isError": True,
                "content": [{"type": "text", "text": f"Failed: {e}"}],
            }
        await self._write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})

    async def _relay_permission(self, params: dict) -> None:
        """Переслать permission_request оркестратору (POST /permission/<имя>)."""
        url = f"http://{ORCH_HOST}:{ORCH_PORT}/permission/{SESSION_NAME}"
        try:
            await self._post(url, params, timeout=10)
        except Exception as e:
            logger.error("Не удалось переслать permission_request: %s", e)

    # ── HTTP: приём push от оркестратора (stdlib http.server в потоке) ──

    def _start_http(self) -> _PushHTTPServer:
        # Auth на /notify /permission /ping тем же ORCH_TOKEN (симметрично
        # оркестратору): без него локальный процесс мог бы POST /notify и вбросить
        # промпт в Claude или POST /permission behavior=allow и авто-разрешить
        # запрос. Пустой ORCH_TOKEN (тестовый запуск без .env) = режим открыт —
        # в проде config всегда генерирует/читает токен.
        self._loop = asyncio.get_running_loop()
        expected = (f"Bearer {ORCH_TOKEN}").encode() if ORCH_TOKEN else b""
        if not ORCH_TOKEN:
            logger.warning(
                "ORCH_TOKEN пуст — channel-сервер в ОТКРЫТОМ режиме (любой "
                "локальный процесс может /notify//permission). Только для тестов!"
            )
        httpd = _PushHTTPServer((CHANNEL_HOST, CHANNEL_PORT), _PushHandler)
        httpd.channel = self
        httpd.loop = self._loop
        httpd.expected = expected
        threading.Thread(
            target=httpd.serve_forever, name="ch-http", daemon=True
        ).start()
        logger.info("HTTP push-сервер на порту %d", CHANNEL_PORT)
        return httpd

    # Тела эндпоинтов — корутины на event loop (мост из HTTP-потока —
    # run_coroutine_threadsafe в _PushHandler). Возврат: (http-код, тело).

    async def _do_notify(self, data: dict) -> tuple[int, str]:
        try:
            await asyncio.wait_for(self._initialized.wait(), timeout=30)
        except asyncio.TimeoutError:
            return 503, "MCP handshake not completed"
        # Троттлинг: не чаще одного push в PUSH_INTERVAL. Слот резервируем
        # сразу — конкурентные /notify не проснутся одновременно.
        now = time.monotonic()
        slot = max(now, self._last_push + PUSH_INTERVAL)
        self._last_push = slot
        if slot > now:
            await asyncio.sleep(slot - now)
        # Формат по контракту: {content, meta}. meta-ключи становятся
        # атрибутами тега <channel>.
        await self._write_message({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel",
            "params": {
                "content": str(data.get("content", "")),
                "meta": {"context_id": str(data.get("context_id", ""))},
            },
        })
        return 200, "OK"

    async def _do_permission(self, data: dict) -> tuple[int, str]:
        """Вердикт пользователя — уведомлением в Claude Code."""
        await self._write_message({
            "jsonrpc": "2.0",
            "method": "notifications/claude/channel/permission",
            "params": {
                "request_id": str(data.get("request_id", "")).lower(),
                "behavior": "allow" if data.get("behavior") == "allow" else "deny",
            },
        })
        return 200, "OK"


class _PushHTTPServer(http.server.ThreadingHTTPServer):
    """HTTP-сервер приёма push от оркестратора (/ping /notify /permission).
    Живёт в отдельном daemon-потоке; хендлеры мостят тела в event loop канала."""

    daemon_threads = True
    allow_reuse_address = True
    channel: "ChannelServer"
    loop: asyncio.AbstractEventLoop
    expected: bytes = b""  # b"Bearer <token>" или b"" (открытый режим)


class _PushHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D401 — глушим access-лог в stderr
        pass

    def _authed(self) -> bool:
        expected: bytes = self.server.expected  # type: ignore[attr-defined]
        if not expected:
            return True
        sent = self.headers.get("Authorization", "").encode("utf-8", "replace")
        return secrets.compare_digest(sent, expected)

    def _send(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self._authed():
            return self._send(401, "unauthorized")
        self._send(200, "OK") if self.path == "/ping" else self._send(404, "not found")

    def do_POST(self) -> None:
        # ВСЕГДА вычитываем тело ДО любого ответа: при HTTP/1.1 keep-alive
        # невычитанный остаток рассинхронил бы соединение (оркестратор шлёт /notify
        # через пул) — следующий запрос на том же сокете ломался бы (парсер прочёл
        # бы хвост тела как request-line). Поэтому read перед auth-гейтом.
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        if not self._authed():
            return self._send(401, "unauthorized")
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return self._send(400, "invalid json")
        ch: ChannelServer = self.server.channel      # type: ignore[attr-defined]
        loop = self.server.loop                       # type: ignore[attr-defined]
        if self.path == "/notify":
            code, text = _run_on_loop(loop, ch._do_notify(data), timeout=35)
        elif self.path == "/permission":
            code, text = _run_on_loop(loop, ch._do_permission(data), timeout=10)
        else:
            code, text = 404, "not found"
        self._send(code, text)


def _run_on_loop(loop, coro, timeout: float) -> tuple[int, str]:
    """Выполнить корутину на event loop канала из HTTP-потока и вернуть (код, тело)."""
    try:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)
    except Exception as e:
        logger.error("HTTP-мост: %s", e)
        return 500, "internal error"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(levelname)s] ch-srv({SESSION_NAME}): %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(ChannelServer().run())
    except KeyboardInterrupt:
        pass
    logger.info("Channel-сервер остановлен")


if __name__ == "__main__":
    main()
