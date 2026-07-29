"""Telegram-адаптер: транспорт ядра поверх aiogram (форум-топики).

Реализует Transport (core/transport.py): сессия ↔ форум-топик привязанной
группы, статус-бабл — редактируемое сообщение с кнопками «📋 Отчёт»/«⛔
Прервать», permission relay — кнопки ✅/❌, файлы в обе стороны,
реакции-ack (👀/👍/👎).

Команды пользователя (/new, /stats, /model, …) принимаются здесь и
транслируются в вызовы ядра (core/app.py OrchestratorCore) — сама логика
команд транспорт-независима и живёт там.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

from . import cbdata
from ...config import Config
from ...core.app import MODEL_ALIASES, OrchestratorCore, UserError
from ...core.mdrender import md_to_html, split_text
from ...core.sessions import Session
from ...core.transport import Origin, PermissionRequest

logger = logging.getLogger(__name__)

# Лимит файла Telegram Bot API.
FILE_LIMIT = 50 * 1024 * 1024
# Сколько привязок «сообщение → статус терминала» помним ради правок команд.
TERM_MSGS_LIMIT = 500


class TelegramAdapter:
    name = "telegram"
    requires_binding = True  # без форум-топика сессии писать некуда

    def __init__(self, config: Config, core: OrchestratorCore):
        self.config = config
        self.core = core
        self.manager = core.manager
        self.bot = Bot(token=config.telegram_bot_token)
        self.dp = Dispatcher()
        self.chat_id = config.telegram_chat_id
        self.t = core.t
        self._poll_task: asyncio.Task | None = None
        # (имя сессии, request_id) -> (message_id, исходный текст) — чтобы
        # погасить permission-кнопки, когда вердикт дан (в т.ч. из другого
        # адаптера).
        self._perm_msgs: dict[tuple[str, str], tuple[int, str]] = {}
        # Карта «id сообщения пользователя → id статус-сообщения терминала» —
        # чтобы правка сообщения переисполнила команду и обновила тот же ответ.
        # Значение: (status_msg_id, bash_key, последняя_выполненная_команда).
        self._term_msgs: dict[int, tuple[int, str, str | None]] = {}
        self._register_handlers()

    # ── Transport: жизненный цикл ───────────────────────────────

    async def start(self) -> None:
        await self._set_command_menu()
        # Поллинг — своей задачей; сигналы обрабатывает __main__ (адаптеров
        # может быть несколько, aiogram не владеет процессом).
        self._poll_task = asyncio.create_task(
            self.dp.start_polling(self.bot, handle_signals=False),
            name="tg-polling",
        )

    async def _set_command_menu(self) -> None:
        # Меню команд в интерфейсе Telegram (кнопка «/» у поля ввода).
        # Недоступные при этой конфигурации команды отсеиваем ниже одним
        # фильтром — решает ядро (core.command_available), не адаптер.
        commands = [
            BotCommand(command="new", description=self.t("menu_new")),
            BotCommand(command="list", description=self.t("menu_list")),
            BotCommand(command="ls", description=self.t("menu_ls")),
            BotCommand(command="bg", description=self.t("menu_bg")),
            BotCommand(command="wallet", description=self.t("menu_wallet")),
            BotCommand(command="orchestrator_restart", description=self.t("menu_restart")),
            BotCommand(command="orchestrator_web", description=self.t("menu_web")),
            BotCommand(command="stats", description=self.t("menu_stats")),
            BotCommand(command="usage", description=self.t("menu_usage")),
            BotCommand(command="model", description=self.t("menu_model")),
            BotCommand(command="skills", description=self.t("menu_skills")),
            BotCommand(command="compact", description=self.t("menu_compact")),
            BotCommand(command="clear", description=self.t("menu_clear")),
            BotCommand(command="close_session", description=self.t("menu_close")),
            BotCommand(command="delete_session", description=self.t("menu_delete")),
            BotCommand(command="bash", description=self.t("menu_bash")),
            # /bashin из меню убран: в липком режиме (/term on) ответ команде
            # уходит обычным сообщением, вспоминать отдельную команду больше не
            # нужно. Сама команда работает — она осталась для случая «терминал
            # занят, а режим выключен», просто не мозолит глаза в списке.
            BotCommand(command="term", description=self.t("menu_term")),
            BotCommand(command="chat_id", description=self.t("menu_chat_id")),
            BotCommand(command="help", description=self.t("menu_help")),
        ]
        commands = [c for c in commands if self.core.command_available(c.command)]
        # Бот работает в группах — меню «/» там берёт команды из группового
        # scope; без него список часто пуст. Ставим и default, и для групп.
        if self.config.show_command_menu:
            await self.bot.set_my_commands(commands)
            await self.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
        else:
            try:
                await self.bot.delete_my_commands()
            except Exception as e:
                logger.warning("Не удалось очистить меню команд: %s", e)

    async def stop(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            try:
                await self.dp.stop_polling()
            except Exception:
                self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.bot.session.close()

    # ── Transport: привязка сессий ──────────────────────────────

    def _thread_of(self, session: Session) -> int | None:
        raw = session.bindings.get(self.name)
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    def _session_by_thread(self, thread_id: int | None) -> Session | None:
        if thread_id is None:
            return None
        return self.manager.get_by_binding(self.name, str(thread_id))

    async def bind_session(self, session: Session) -> str | None:
        if self.chat_id is None:
            return None
        try:
            topic = await self.bot.create_forum_topic(
                chat_id=self.chat_id, name=session.title[:128]
            )
        except Exception as e:
            # Нет права «Manage Topics» или в группе выключены темы.
            logger.error("Не удалось создать топик для %s: %s", session.title, e)
            return None
        return str(topic.message_thread_id)

    async def unbind_session(self, session: Session, address: str) -> None:
        if self.chat_id is None:
            return
        try:
            await self.bot.delete_forum_topic(
                chat_id=self.chat_id, message_thread_id=int(address)
            )
        except Exception as e:
            logger.warning("Не удалось удалить топик %s: %s", address, e)
            await self._send(
                self.chat_id, None, self.t("topic_delete_fail", error=e)
            )

    # ── Transport: доставка ─────────────────────────────────────

    @staticmethod
    def _parse_token(token: str) -> int | None:
        """Токен origin: chat:thread:message → message_id для reply-цитаты."""
        parts = token.split(":")
        try:
            return int(parts[2]) or None if len(parts) == 3 else None
        except (ValueError, IndexError):
            return None

    async def deliver_text(
        self, session: Session, text: str, *, origin: Origin | None = None,
        intermediate: bool = False,
    ) -> None:
        if self.chat_id is None:
            return
        thread_id = self._thread_of(session)
        if thread_id is None:
            return
        if intermediate:
            await self._send(self.chat_id, thread_id, f"💬 {text}")
            return
        reply_to = self._parse_token(origin.token) if origin else None
        await self._send(self.chat_id, thread_id, text, reply_to)

    async def deliver_file(
        self, session: Session, path: Path, caption: str, *,
        origin: Origin | None = None,
    ) -> None:
        if self.chat_id is None:
            return
        thread_id = self._thread_of(session)
        if thread_id is None:
            return
        if path.stat().st_size > FILE_LIMIT:
            await self._send(self.chat_id, thread_id, self.t("sendfile_too_big", path=path))
            return
        kwargs: dict = {"chat_id": self.chat_id, "document": FSInputFile(path)}
        if caption:
            kwargs["caption"] = caption[:1024]
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        reply_to = self._parse_token(origin.token) if origin else None
        if reply_to:
            kwargs["reply_to_message_id"] = reply_to
        try:
            await self.bot.send_document(**kwargs)
        except Exception as e:
            logger.error("Не удалось отправить файл %s: %s", path, e)
            await self._send(self.chat_id, thread_id, self.t("sendfile_fail", error=e))

    async def notify(self, session: Session | None, text: str) -> None:
        if self.chat_id is None:
            return
        thread_id = self._thread_of(session) if session is not None else None
        if session is not None and thread_id is None:
            return  # сессия невидима для этого адаптера
        await self._send(self.chat_id, thread_id, text)

    async def typing(self, session: Session) -> bool:
        """Один chat-action «печатает…». False — слать некуда."""
        if self.chat_id is None:
            return False
        thread_id = self._thread_of(session)
        if thread_id is None:
            return False
        try:
            await self.bot.send_chat_action(
                chat_id=self.chat_id, action="typing", message_thread_id=thread_id
            )
        except Exception as e:
            logger.debug("typing (топик %s): %s", thread_id, e)
        return True

    # ── Transport: статус-бабл ──────────────────────────────────

    def _stop_markup(self, thread_id: int, unblock_active: bool = False) -> InlineKeyboardMarkup:
        # 📋 — отчёт: просим Claude дать статус-отчёт и ПРОДОЛЖИТЬ (не стоп —
        #     прежний «мягкий стоп» модель считала инъекцией; см. on_stop_button);
        # ⏭ — разблокировать ввод (Ctrl+B/Esc). Кнопка ВСЕГДА на своём месте
        #     (ряд не «прыгает» — иначе легко тапнуть по исчезнувшей и попасть в
        #     соседнюю). Когда сворачивать нечего — вместо ⏭ дефис-заглушка
        #     (bubble_unblock_idle), а тап по ней — молчаливый no-op (on_bg_button);
        # ⛔ — жёсткое прерывание хода: Esc в PTY (ход обрывается сразу).
        row = [
            InlineKeyboardButton(
                text=self.t("bubble_stop"), callback_data=cbdata.stop_cb(thread_id)),
            InlineKeyboardButton(
                text=self.t("bubble_unblock" if unblock_active else "bubble_unblock_idle"),
                callback_data=cbdata.bg_cb(thread_id)),
            InlineKeyboardButton(
                text=self.t("bubble_esc"), callback_data=cbdata.esc_cb(thread_id)),
        ]
        return InlineKeyboardMarkup(inline_keyboard=[row])

    async def bubble_post(
        self, session: Session, html_text: str, *, stop_button: bool, unblock_active: bool = False
    ) -> str | None:
        if self.chat_id is None:
            return None
        thread_id = self._thread_of(session)
        if thread_id is None:
            return None
        msg = await self.bot.send_message(
            chat_id=self.chat_id,
            text=html_text,
            message_thread_id=thread_id,
            disable_notification=True,
            reply_markup=self._stop_markup(thread_id, unblock_active) if stop_button else None,
            parse_mode="HTML",
        )
        return str(msg.message_id)

    async def bubble_edit(
        self, session: Session, ref: str, html_text: str, *, stop_button: bool,
        unblock_active: bool = False,
    ) -> None:
        if self.chat_id is None:
            return
        thread_id = self._thread_of(session)
        # reply_markup нужен и при edit — иначе Telegram снимает кнопку.
        await self.bot.edit_message_text(
            chat_id=self.chat_id,
            message_id=int(ref),
            text=html_text,
            reply_markup=(
                self._stop_markup(thread_id, unblock_active)
                if stop_button and thread_id else None
            ),
            parse_mode="HTML",
        )

    async def bubble_finish(self, session: Session, ref: str, *, delete: bool) -> None:
        if self.chat_id is None:
            return
        if delete:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=int(ref))
        else:
            # Бабл остаётся как журнал работы — снимаем только кнопку Стоп.
            await self._strip_stop_button(int(ref))

    async def bubble_freeze(self, session: Session, ref: str) -> None:
        # Заморозка = снять кнопку «Стоп», само сообщение не трогать.
        if self.chat_id is None:
            return
        await self._strip_stop_button(int(ref))

    async def _strip_stop_button(self, message_id: int) -> None:
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=self.chat_id, message_id=message_id, reply_markup=None
            )
        except Exception as e:
            # «message is not modified» и т.п. — кнопка и так снята, не критично.
            logger.debug("Не удалось снять кнопку бабла %s: %s", message_id, e)

    # ── Transport: permission relay ─────────────────────────────

    async def permission_prompt(
        self, session: Session, request: PermissionRequest
    ) -> None:
        if self.chat_id is None:
            return
        thread_id = self._thread_of(session)
        if thread_id is None:
            return
        # description/preview — недоверенный текст, экранируем. preview ядро
        # уже обрезало до лимита (второй срез резал бы маркер «…(обрезано)»).
        text = self.t(
            "perm_request",
            tool=html.escape(request.tool),
            desc=html.escape(request.description),
            preview=html.escape(request.preview),
        )
        buttons = [
            InlineKeyboardButton(
                text=self.t("perm_allow"),
                callback_data=cbdata.perm_cb(thread_id, request.request_id, "allow")),
            InlineKeyboardButton(
                text=self.t("perm_deny"),
                callback_data=cbdata.perm_cb(thread_id, request.request_id, "deny")),
        ]
        rows = [buttons]
        if request.always_label:
            # Третья кнопка появляется ТОЛЬКО когда запросивший её предложил
            # (ASK-грант кошелька): у подтверждений тулов always_label=None и
            # клавиатура остаётся прежней, байт в байт. Отдельной строкой —
            # чтобы «навсегда» не нажималось случайно рядом с разовым ✅.
            rows.append([InlineKeyboardButton(
                text=request.always_label,
                callback_data=cbdata.perm_cb(
                    thread_id, request.request_id, "allow_always"))])
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        try:
            msg = await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                message_thread_id=thread_id,
                parse_mode="HTML",
                reply_markup=markup,
            )
            self._perm_msgs[(session.name, request.request_id)] = (
                msg.message_id, request.tool
            )
        except Exception as e:
            logger.error("Не удалось отправить permission-запрос: %s", e)

    async def permission_resolved(
        self, session: Session, request_id: str, behavior: str, via: str
    ) -> None:
        stored = self._perm_msgs.pop((session.name, request_id), None)
        if stored is None or self.chat_id is None:
            return
        message_id, tool = stored
        # Три исхода: разово / навсегда (грант записан в policy) / отказ. Их
        # различие оператору видно в подтверждении — иначе «навсегда» выглядело
        # бы как обычное ✅ и правка policy прошла бы незаметно.
        verdict_key = {
            "allow": "perm_allowed",
            "allow_always": "perm_allowed_always",
        }.get(behavior, "perm_denied")
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=self.chat_id, message_id=message_id, reply_markup=None
            )
            await self._send(
                self.chat_id, self._thread_of(session),
                self.t(verdict_key, tool=tool), message_id,
            )
        except Exception as e:
            logger.debug("permission_resolved: %s", e)

    async def session_state_changed(self, session: Session | None) -> None:
        # Transport-хук: у Telegram нет живого списка сессий (каждая — свой
        # топик), обновлять нечего — no-op.
        return

    # ── регистрация и доступ ────────────────────────────────────

    def _register_handlers(self) -> None:
        dp = self.dp
        dp.message.register(self.cmd_help, Command("start", "help"))
        dp.message.register(self.cmd_new, Command("new"))
        dp.message.register(self.cmd_list, Command("list"))
        dp.message.register(self.cmd_ls, Command("ls"))
        dp.message.register(self.cmd_bg, Command("bg"))
        dp.message.register(self.cmd_wallet, Command("wallet"))
        dp.message.register(self.cmd_restart, Command("orchestrator_restart"))
        dp.message.register(self.cmd_web, Command("orchestrator_web"))
        dp.message.register(self.cmd_close, Command("close_session", "stop"))
        dp.message.register(self.cmd_delete, Command("delete_session"))
        dp.message.register(self.cmd_compact, Command("compact"))
        dp.message.register(self.cmd_clear, Command("clear"))
        dp.message.register(self.cmd_stats, Command("stats"))
        dp.message.register(self.cmd_log, Command("log"))
        dp.message.register(self.cmd_usage, Command("usage", "cost"))
        dp.message.register(self.cmd_model, Command("model"))
        dp.message.register(self.cmd_skills, Command("skills"))
        dp.message.register(self.cmd_chat_id, Command("chat_id"))
        dp.message.register(self.cmd_bash, Command("bash"))
        dp.message.register(self.cmd_bashin, Command("bashin"))
        dp.message.register(self.cmd_term, Command("term"))
        dp.message.register(self.on_text, F.text & ~F.text.startswith("/"))
        dp.message.register(self.on_file, F.photo | F.document)
        # Последним: неизвестные /команды уходят в терминал Claude.
        dp.message.register(self.on_slash, F.text.startswith("/"))
        dp.callback_query.register(self.on_stop_button, F.data.startswith("stop:"))
        dp.callback_query.register(self.on_esc_button, F.data.startswith("esc:"))
        dp.callback_query.register(self.on_bg_button, F.data.startswith("bg:"))
        dp.callback_query.register(self.on_model_button, F.data.startswith("model:"))
        dp.callback_query.register(self.on_session_button, F.data.startswith("sess:"))
        dp.callback_query.register(self.on_perm_button, F.data.startswith("perm:"))
        dp.callback_query.register(self.on_delete_button, F.data.startswith("del:"))
        dp.callback_query.register(
            self.on_termint_button, F.data.startswith("termint:"))
        dp.callback_query.register(
            self.on_termrepeat_button, F.data.startswith("termrepeat:"))
        dp.callback_query.register(
            self.on_termhist_button, F.data.startswith("termhist:"))
        dp.callback_query.register(
            self.on_termhistrun_button, F.data.startswith("termhistrun:"))
        dp.edited_message.register(self.on_edited_message)

    def _accept(self, message: Message) -> bool:
        """Доступ строго по ALLOWED_USER_IDS + привязка к одной группе.

        Пустой список = игнорировать всех. Чужих игнорируем молча
        (не выдаём существование бота).
        """
        if message.chat.type not in ("group", "supergroup"):
            return False
        if not self._user_allowed(message.from_user):
            return False
        if self.chat_id is None:
            self.chat_id = message.chat.id
            logger.info(
                "Чат привязан: %s (зафиксируй TELEGRAM_CHAT_ID=%s в .env)",
                message.chat.id, message.chat.id,
            )
        return message.chat.id == self.chat_id

    def _accept_callback(self, callback: CallbackQuery) -> bool:
        """Гейт для кнопок, которые ИСПОЛНЯЮТ команды в терминале.

        Кроме белого списка пользователей сверяем и чат: у обычных кнопок
        (отчёт, модель) хватает _user_allowed, но здесь нажатие запускает
        shell-команду, и брать message_thread_id из сообщения чужого чата —
        значит позволить адресоваться к сессии в обход привязки группы.
        """
        if not self._user_allowed(callback.from_user):
            return False
        msg = callback.message
        return msg is not None and msg.chat.id == self.chat_id

    def _user_allowed(self, user) -> bool:
        if user is None or user.id not in self.config.allowed_user_ids:
            logger.warning("Отказ в доступе: %s", user)
            return False
        return True

    def _topic_session(self, message: Message) -> Session | None:
        return self._session_by_thread(message.message_thread_id or 0)

    def _origin(self, message: Message, thread_id: int | None = None) -> Origin:
        tid = thread_id if thread_id is not None else (message.message_thread_id or 0)
        return Origin(self.name, f"{self.chat_id}:{tid}:{message.message_id}")

    # ── команды основного чата ──────────────────────────────────

    async def cmd_chat_id(self, message: Message) -> None:
        """Показать ID чата и привязать бота, если он ещё не привязан.

        Намеренно НЕ через _accept: должна работать и в чужой группе —
        иначе ID новой группы не узнать. Белый список пользователей действует.
        """
        if message.chat.type not in ("group", "supergroup"):
            return
        if not self._user_allowed(message.from_user):
            return
        chat_id = message.chat.id
        if self.chat_id is None:
            self.chat_id = chat_id
            logger.info("Чат привязан через /chat_id: %s", chat_id)
            text = self.t("chat_id_bound_now", id=chat_id)
        elif self.chat_id == chat_id:
            text = self.t("chat_id_current", id=chat_id)
        else:
            text = self.t("chat_id_other", id=chat_id, bound=self.chat_id)
        await message.reply(text, parse_mode="HTML")

    async def cmd_help(self, message: Message) -> None:
        if not self._accept(message):
            return
        await message.reply(self.core.help_text(), parse_mode="HTML")

    async def cmd_new(self, message: Message, command: CommandObject) -> None:
        if not self._accept(message):
            return
        if message.message_thread_id is not None:
            await message.reply(self.t("only_main_chat"))
            return
        title, project_path, box, profile = self.core.parse_new_args(command.args or "")
        if not title:
            await message.reply(self.t("new_usage"))
            return
        title = title[:128]  # предел названия топика в Telegram
        status = await message.reply(self.t("creating"))
        try:
            session = await self.core.create_session(title, project_path, box, profile)
        except UserError as e:
            await status.edit_text(self.t("create_fail", error=e))
            return
        except Exception as e:
            logger.exception("Ошибка создания сессии %s", title)
            await status.edit_text(self.t("create_fail", error=e))
            return
        # Без parse_mode: имя сессии задаёт пользователь, «<» в нём сломал бы
        # HTML-разбор уже СОЗДАННОЙ сессии (тексты про изоляцию — тоже простые).
        await status.edit_text(
            self.t("created", name=session.title)
            + self.core.box_note(session)
            + self.core.profile_note(session)
        )

    async def cmd_list(self, message: Message) -> None:
        if not self._accept(message):
            return
        sessions = self.manager.list_all()
        if not sessions:
            await message.reply(self.t("list_empty"))
            return
        lines, rows = [], []
        status_labels = {
            "stopped": self.t("st_stopped"),
            "working": self.t("st_working"),
            "waiting": self.t("st_waiting"),
        }
        for s in sessions:
            status = status_labels[self.core.session_status(s)]
            line = f"{status} — {s.title}"
            if s.model:
                line += f" [{s.model}]"
            line += self.core.box_mark(s) + self.core.profile_mark(s)
            if s.running:
                uptime = self.core.fmt_duration(time.time() - s.started_at)
                line += ", " + self.t("uptime", uptime=uptime)
            if s.linked_path:
                line += f"\n     📁 {s.linked_path}"
            lines.append(line)
            thread_id = self._thread_of(s)
            if thread_id is None:
                continue
            row = [InlineKeyboardButton(
                text=f"📊 {s.title}", callback_data=cbdata.sess_cb("stats", thread_id))]
            if s.running:
                row.append(InlineKeyboardButton(
                    text="⏸", callback_data=cbdata.sess_cb("close", thread_id)))
            rows.append(row)
        await message.reply(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
        )

    async def cmd_ls(self, message: Message, command: CommandObject) -> None:
        if not self._accept(message):
            return
        # cwd как у /bash: в топике сессии — папка проекта, в главном чате — дом.
        session = self._topic_session(message)
        await message.reply(self.core.ls_text(command.args, session))

    async def cmd_bg(self, message: Message, command: CommandObject) -> None:
        """Фоновые процессы/кроны сессии (снимок из последнего Stop-хука)."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("bg_no_session"))
            return
        await message.reply(self.core.bg_text(session), parse_mode="HTML")

    async def cmd_wallet(self, message: Message, command: CommandObject) -> None:
        """Policy кошелька: просмотр/правка (значения токенов не показываются)."""
        if not self._accept(message):
            return
        try:
            text = self.core.wallet_command(command.args or "")
        except UserError as e:
            text = str(e)
        await message.reply(text, parse_mode="HTML")

    async def cmd_restart(self, message: Message) -> None:
        """Перезапуск всего оркестратора (self-restart через systemd) — для
        деплоя без хостового терминала. Отвечаем ПЕРВЫМ (systemd остановит
        процесс сразу после), затем ставим задачу рестарта."""
        if not self._accept(message):
            return
        if message.message_thread_id is not None:
            await message.reply(self.t("only_main_chat"))
            return
        await message.reply(self.t("restarting"))
        try:
            await self.core.restart_service()
        except UserError as e:
            await message.reply(str(e))

    async def cmd_web(self, message: Message) -> None:
        """Ссылка на локальный веб-интерфейс с токеном — если веб запущен."""
        if not self._accept(message):
            return
        url = self.core.web_url()
        if url is None:
            await message.reply(self.t("web_disabled"))
            return
        await message.reply(self.t("web_url", url=url), parse_mode="HTML")

    async def cmd_skills(self, message: Message) -> None:
        if not self._accept(message):
            return
        # В топике — скиллы учётки ЭТОЙ сессии (у неё может быть свой профиль).
        skills = await asyncio.to_thread(
            self.core.collect_skills, self._topic_session(message))
        if not skills:
            await message.reply(self.t("skills_none"))
            return
        lines = [self.t("skills_header", n=len(skills))]
        for name, desc in skills:
            lines.append(f"• {name}" + (f" — {desc}" if desc else ""))
        for chunk in split_text("\n".join(lines)):
            await message.reply(chunk)

    # ── команды топика ──────────────────────────────────────────

    async def cmd_bash(self, message: Message, command: CommandObject) -> None:
        """Выполнить команду в постоянном bash-терминале топика, мимо Claude.

        Стримит вывод в одно редактируемое сообщение; исполнение и песочница —
        в ядре (core.run_bash). Ответ интерактивному промпту — /bashin.
        """
        if not self._accept(message):
            return
        cmd = (command.args or "").strip()
        if not cmd:
            await message.reply(self.t("bash_usage"))
            return
        session = self._topic_session(message)
        key = self.core.bash_key(session, f"tg{message.message_thread_id or 0}")
        await self._run_bash_cmd(message, session, key, cmd)

    async def _run_bash_cmd(
        self, message: Message, session: Session | None, key: str, cmd: str,
        *, edit_status_id: int | None = None,
    ) -> None:
        """Выполнить команду в bash-терминале топика со стримингом вывода.

        Общая часть для /bash и липкого режима терминала (/term on): статус-
        сообщение, стриминг рендера, финальный код возврата. Занятость оболочки
        проверяется здесь же — до поста статуса (иначе «терминал занят» висел бы
        рядом с «⏳ Выполняю <cmd>» вечно).

        edit_status_id — если задан, ОБНОВЛЯЕМ существующее сообщение вместо
        создания нового (правка сообщения пользователя → перезапуск команды).
        """
        if self.core.bash_busy(key):
            await message.reply(self.t("bash_busy"))
            return

        # Создаём или переиспользуем статус-сообщение.
        if edit_status_id is not None:
            # Обновляем существующее — правка сообщения переисполняет команду.
            status_msg = await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=edit_status_id,
                text=self.t("bash_running", cmd=cmd),
                parse_mode="HTML",
            )
            # edit_message_text возвращает Message | True; нам нужен message_id.
            if isinstance(status_msg, bool):
                status_id = edit_status_id
            else:
                status_id = status_msg.message_id
        else:
            # Новая команда — создаём статус-сообщение.
            status = await message.reply(self.t("bash_running", cmd=cmd))
            status_id = status.message_id

        last_edit = ""
        output_file: str | None = None
        # Сохраняем привязку «сообщение пользователя → статус-сообщение» для
        # правки сообщения (edited_message → перезапуск команды).
        self._term_msgs[message.message_id] = (status_id, key, cmd)
        # Карта не должна расти вечно — вытесняем самые старые сообщения.
        # id сообщений Telegram монотонно растут в пределах чата, поэтому
        # «самое маленькое id» и есть самое старое: не полагаемся на порядок
        # вставки в dict (он деталь реализации, а не гарантия семантики).
        while len(self._term_msgs) > TERM_MSGS_LIMIT:
            del self._term_msgs[min(self._term_msgs)]

        # Собираем inline-клавиатуру с кнопками управления терминалом.
        thread_id = message.message_thread_id or 0
        markup = self._term_keyboard(thread_id)

        async def on_update(shown: str, done: bool, file_path: str | None = None) -> None:
            nonlocal last_edit, output_file
            if shown != last_edit:
                try:
                    await self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=status_id,
                        text=shown,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                    last_edit = shown
                except Exception:
                    pass  # «текст не изменился» и т.п. — не критично
            # Сохраняем путь к файлу — отправим документом после финального
            # обновления статуса, чтобы бабл не перекрывал файл.
            if done and file_path:
                output_file = file_path

        try:
            await self.core.run_bash(key, session, cmd, on_update)
        except UserError as e:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=status_id,
                text=str(e),
                reply_markup=self._term_keyboard(thread_id, running=False),
            )

        # Команда закончилась — «Прервать» убираем, «Повторить» и «История»
        # остаются (см. _term_keyboard: running=False).
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=self.chat_id,
                message_id=status_id,
                reply_markup=self._term_keyboard(thread_id, running=False),
            )
        except Exception:
            pass

        # Отправляем полный вывод документом, если он не влез в сообщение.
        # Удаляем после отправки: для workspace-файлов (.bash_outputs/) это
        # предотвращает накопление; для временных (main-chat) — обязательно,
        # чтобы не забивать /tmp.
        if output_file:
            out_path = Path(output_file)
            keep_file = False
            try:
                size = out_path.stat().st_size
                if size > FILE_LIMIT:
                    keep_file = True  # оператору дали путь — файл нужен на месте
                    # Telegram больше не примет, а read_text ниже затянул бы
                    # гигабайты в память процесса. Честно говорим, где файл.
                    await message.reply(self.t(
                        "bash_output_too_big",
                        size=f"{size / 1024 / 1024:.0f} MB",
                        path=html.escape(str(out_path)),
                    ), parse_mode="HTML")
                    return
                # caption — краткая справка; текст уже в бабле, не дублируем.
                # Считаем строки потоком: файл может быть на десятки мегабайт.
                with out_path.open("rb") as fh:
                    lines = sum(chunk.count(b"\n") for chunk in iter(
                        lambda: fh.read(1 << 20), b"")) + 1
                caption = self.t(
                    "bash_output_truncated", lines=lines, size=f"{size / 1024:.0f} KB")
                await self.bot.send_document(
                    chat_id=self.chat_id,
                    document=FSInputFile(out_path),
                    caption=caption[:1024],
                    message_thread_id=message.message_thread_id or None,
                )
            except Exception as e:
                logger.error("Не удалось отправить файл вывода bash %s: %s", out_path, e)
            finally:
                # Удаляем в любом случае: отправка удалась — файл у пользователя,
                # не удалась — бесполезный артефакт. Ошибку удаления глушим.
                # Исключение — слишком большой файл: мы дали оператору путь к
                # нему, и стирать его сразу после этого было бы издевательством.
                if not keep_file:
                    try:
                        out_path.unlink(missing_ok=True)
                    except OSError:
                        pass

    def _term_keyboard(self, thread_id: int, *, running: bool = True) -> InlineKeyboardMarkup:
        """Inline-клавиатура под выводом команды терминала.

        running=False — команда уже завершилась, и «Прервать» убираем: живая
        кнопка, которой нечего прерывать, либо молча ничего не делает, либо
        (хуже) шлёт Ctrl-C в оболочку под следующую команду.
        """
        row = []
        if running:
            row.append(InlineKeyboardButton(
                text=self.t("term_btn_interrupt"),
                callback_data=cbdata.termint_cb(thread_id),
            ))
        row += [
            InlineKeyboardButton(
                text=self.t("term_btn_repeat"),
                callback_data=cbdata.termrepeat_cb(thread_id),
            ),
            InlineKeyboardButton(
                text=self.t("term_btn_history"),
                callback_data=cbdata.termhist_cb(thread_id),
            ),
        ]
        return InlineKeyboardMarkup(inline_keyboard=[row])

    async def cmd_bashin(self, message: Message, command: CommandObject) -> None:
        """Досыл сырого ввода в уже открытый /bash-терминал (ответ на y/n)."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        key = self.core.bash_key(session, f"tg{message.message_thread_id or 0}")
        text = command.args or ""
        if not self.core.bash_input(key, text):
            await message.reply(self.t("bash_not_running"))
            return
        await message.reply(self.t("bashin_sent", text=html.escape(text) or "⏎"))

    async def cmd_term(self, message: Message, command: CommandObject) -> None:
        """/term [on|off]: липкий режим терминала — обычные сообщения уходят в шелл.

        Без аргументов — краткий статус. Включение только в топике сессии:
        в основном чате шелл на хосте с правами оператора, и случайное
        сообщение там выполнилось бы без песочницы.
        """
        if not self._accept(message):
            return
        session = self._topic_session(message)
        key = self.core.bash_key(session, f"tg{message.message_thread_id or 0}")
        try:
            text = self.core.term_command(session, key, command.args or "")
        except UserError as e:
            text = str(e)
        await message.reply(text, parse_mode="HTML")

    async def cmd_close(self, message: Message) -> None:
        """Остановить сессию; топик и запись остаются, resume по сообщению."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        await self.core.close_session(session)
        await message.reply(self.t("close_done"))

    async def cmd_delete(self, message: Message) -> None:
        """Полностью удалить сессию вместе с топиком — с подтверждением."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        thread_id = self._thread_of(session) or 0
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text=self.t("delete_confirm_yes"),
                callback_data=cbdata.delete_cb(thread_id, "yes")),
            InlineKeyboardButton(
                text=self.t("delete_confirm_no"),
                callback_data=cbdata.delete_cb(thread_id, "no")),
        ]])
        await message.reply(
            self.t("delete_confirm", title=session.title), reply_markup=markup
        )

    async def on_delete_button(self, callback: CallbackQuery) -> None:
        """Подтверждение удаления: yes — снести сессию+топик, no — отменить."""
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        parsed = cbdata.parse_delete(callback.data or "")
        if parsed is None:
            session, verdict = None, "no"
        else:
            session, verdict = self._session_by_thread(parsed[0]), parsed[1]
        if session is None:
            await callback.answer(self.t("delete_gone"))
            await self._strip_markup(callback)
            return
        if verdict != "yes":
            await callback.answer()
            await self._edit_or_pass(callback, self.t("delete_canceled", title=session.title))
            return
        await callback.answer(self.t("delete_doing"))
        await self.core.delete_session(session)

    async def _strip_markup(self, callback: CallbackQuery) -> None:
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass

    async def _edit_or_pass(self, callback: CallbackQuery, text: str) -> None:
        if isinstance(callback.message, Message):
            try:
                await callback.message.edit_text(text)
            except Exception:
                pass

    async def cmd_clear(self, message: Message) -> None:
        """Чистый контекст: перезапуск Claude с новым UUID, топик остаётся."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        status = await message.reply(self.t("clear_progress"))
        try:
            await self.core.clear_session(session)
        except UserError as e:
            await status.edit_text(str(e))
            return
        await status.edit_text(self.t("clear_done"))

    async def cmd_compact(self, message: Message) -> None:
        """/compact — в терминал Claude (проверенный путь: PTY)."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        try:
            await self.core.compact(session)
        except UserError as e:
            await message.reply(str(e))
            return
        await message.reply(self.t("compact_sent"))

    async def cmd_stats(self, message: Message) -> None:
        """Контекст и статистика из транскрипта сессии."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        await message.reply(await asyncio.to_thread(self.core.stats_text, session))

    async def cmd_log(self, message: Message) -> None:
        """/log — прислать полный claude.log сессии документом (для отладки)."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        await self.core.send_log(session, self._origin(message))

    async def cmd_usage(self, message: Message) -> None:
        """Расходы и лимиты плана (парсит /cost Claude Code)."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        if not await self._ensure_running(session, message):
            return
        status = await message.reply(self.t("usage_collecting"))
        try:
            text = await self.core.usage_text(session)
        except UserError as e:
            # usage_text бросает UserError (напр. PTY умер между ensure_running и
            # /cost) — веб-адаптер её ловит, телеграмный раньше нет: «⏳ собираю…»
            # висело вечно.
            await status.edit_text(str(e))
            return
        await status.edit_text(text if text else self.t("usage_failed"))

    async def cmd_model(self, message: Message, command: CommandObject) -> None:
        """Модель сессии: /model — кнопки-синонимы, /model <имя> — установить."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None:
            await message.reply(self.t("only_topic"))
            return
        arg = (command.args or "").strip()
        if arg:
            await self._switch_model(session, arg, message)
            return
        thread_id = self._thread_of(session) or 0
        buttons = [
            InlineKeyboardButton(text=alias, callback_data=cbdata.model_cb(thread_id, alias))
            for alias in MODEL_ALIASES
        ]
        model = await asyncio.to_thread(self.core.model_display, session)
        await message.reply(
            self.t("model_prompt", model=model),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
        )

    async def on_model_button(self, callback: CallbackQuery) -> None:
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        parsed = cbdata.parse_model(callback.data or "")
        session = self._session_by_thread(parsed[0]) if parsed else None
        if parsed is None or session is None:
            await callback.answer(self.t("stop_not_active"))
            return
        alias = parsed[1]
        await callback.answer(self.t("model_switching_btn", model=alias))
        if isinstance(callback.message, Message):
            await self._switch_model(session, alias, callback.message)

    async def _switch_model(self, session: Session, model: str, message: Message) -> None:
        # message.answer() сам наследует message_thread_id исходного сообщения —
        # явно передавать нельзя (иначе TypeError: multiple values).
        status = await message.answer(
            self.t("model_switching", name=session.title, model=model)
        )
        try:
            resumed = await self.core.switch_model(session, model)
        except UserError as e:
            await status.edit_text(str(e))
            return
        note = "" if resumed else self.t("model_ctx_lost")
        await status.edit_text(self.t("model_done", model=model) + note)

    async def on_session_button(self, callback: CallbackQuery) -> None:
        """Кнопки в /list: статистика и остановка."""
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        parsed = cbdata.parse_sess(callback.data or "")
        session = self._session_by_thread(parsed[1]) if parsed else None
        if parsed is None or session is None or not isinstance(callback.message, Message):
            await callback.answer(self.t("session_not_found"))
            return
        action = parsed[0]
        if action == "stats":
            await callback.answer()
            await callback.message.answer(
                await asyncio.to_thread(self.core.stats_text, session)
            )
        elif action == "close":
            await self.core.close_session(session)
            await callback.answer(self.t("sess_closed", name=session.title))
        else:
            await callback.answer()

    # ── текст и файлы: Telegram -> Claude ───────────────────────

    async def _react(self, message: Message, emoji: str) -> None:
        """Реакция-отклик: 👀 «бот принял» → 👍 «дошло до модели» (👎 — нет).

        В чатах, где реакции боту запрещены, тихо мимо — косметика.
        """
        try:
            await self.bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)],
            )
        except Exception as e:
            logger.warning("react %s (msg %s) не удалась: %s", emoji, message.message_id, e)

    async def on_text(self, message: Message) -> None:
        if not self._accept(message):
            return
        await self._react(message, "👀")
        session = self._topic_session(message)
        if session is None or not message.text:
            return

        # Липкий режим терминала: обычные сообщения → шелл, `>` → claude.
        # Ключ ровно тот же, что у оболочки (bash_key) — см. termmode.
        key = self.core.bash_key(session, f"tg{message.message_thread_id or 0}")
        if self.core.term.is_on(key):
            where, text = self.core.term.split_escape(message.text)
            if where == "claude":
                # Сообщение начинается с `>` — уходит claude как обычно.
                if not await self._ensure_running(session, message):
                    return
                await self._forward(session, message, text)
                return
            # Команда ещё крутится — значит она чего-то ждёт (y/n, пароль,
            # ответ на промпт): отправляем текст ЕЙ НА ВХОД, а не отвечаем
            # «терминал занят». Ради этого и нужен липкий режим: вспоминать
            # про отдельный /bashin посреди диалога с командой — абсурд.
            # Одним вызовом, а не «спросить busy → записать»: между двумя
            # обращениями команда успевает завершиться, и ответ ей («y»,
            # пароль) достался бы свободной оболочке уже как КОМАНДА.
            if self.core.bash_input_if_busy(key, text):
                # Реакцией, а не сообщением: диалог с командой из пяти
                # «y» не должен рождать пять ответов бота. Эмодзи — из
                # разрешённого Telegram набора (произвольные он отвергает).
                await self._react(message, "✍")
                return
            # Обычный текст — выполняем в шелле (тот же путь, что /bash).
            await self._run_bash_cmd(message, session, key, text)
            return

        # Без липкого режима — поведение как раньше.
        if not await self._ensure_running(session, message):
            return
        await self._forward(session, message, self._with_quote(message))

    def _with_quote(self, message: Message) -> str:
        """Текст сообщения + процитированный фрагмент, если это reply-с-цитатой.

        Телеграм присылает цитату отдельно от текста; без склейки модель не
        видит, на что отвечает пользователь.
        """
        text = message.text or ""
        quoted = ""
        quote_txt = message.quote.text if message.quote else None
        reply_txt = message.reply_to_message.text if message.reply_to_message else None
        if quote_txt:
            quoted = quote_txt                                # выделенный фрагмент
        elif reply_txt:
            quoted = reply_txt                                # весь процитированный пост
        if not quoted:
            return text
        quoted = quoted.strip()
        if len(quoted) > 500:
            quoted = quoted[:500] + " …"
        block = "\n".join(f"> {ln}" for ln in quoted.splitlines())
        return f"{block}\n\n{text}"

    async def on_file(self, message: Message) -> None:
        """Фото (в т.ч. из буфера обмена) и документы: скачать в сессию."""
        if not self._accept(message):
            return
        await self._react(message, "👀")
        session = self._topic_session(message)
        if session is None:
            return
        if not await self._ensure_running(session, message):
            return
        # incoming-каталог — единый источник правды ядра (совпадает с jail'ом
        # скачивания, иначе положим туда, откуда потом нельзя отдать).
        incoming = self.core.incoming_dir(session)
        incoming.mkdir(parents=True, exist_ok=True)

        if message.document:
            file_id = message.document.file_id
            fname = Path(message.document.file_name or f"file_{message.message_id}").name
        else:
            file_id = message.photo[-1].file_id  # максимальное разрешение
            fname = f"photo_{message.message_id}.jpg"
        dest = incoming / fname
        try:
            await self.bot.download(file_id, destination=dest)
        except Exception as e:
            logger.error("Сессия %s: не удалось скачать файл: %s", session.name, e)
            await message.reply(self.t("file_dl_fail", error=e))
            return

        text = self.t("file_received", path=dest)
        if message.caption:
            text += "\n" + self.t("file_caption", caption=message.caption)
        await self._forward(session, message, text)

    async def on_slash(self, message: Message) -> None:
        """Неизвестные /команды — прямо в терминал Claude (команды Claude Code)."""
        if not self._accept(message):
            return
        session = self._topic_session(message)
        if session is None or not message.text:
            return
        if not await self._ensure_running(session, message):
            return
        cmd = message.text.strip().splitlines()[0]
        try:
            await self.core.slash_command(session, cmd)
        except UserError as e:
            await message.reply(str(e))
            return
        await message.reply(self.t("slash_sent", cmd=cmd))

    async def _ensure_running(self, session: Session, message: Message) -> bool:
        """Возобновить остановленную сессию перед пересылкой сообщения."""
        if session.running:
            return True
        status = await message.reply(self.t("resume_progress"))
        try:
            state = await self.core.ensure_running(session)
        except UserError as e:
            await status.edit_text(str(e))
            return False
        except Exception as e:
            logger.exception("Сессия %s: ошибка resume", session.name)
            await status.edit_text(self.t("resume_fail", error=e))
            return False
        await status.edit_text(
            self.t("resume_ok") if state != "fresh" else self.t("resume_fresh")
        )
        return True

    async def _forward(self, session: Session, message: Message, text: str) -> None:
        try:
            await self.core.user_message(session, text, self._origin(message))
        except UserError as e:
            await self._react(message, "👎")  # не дошло до модели
            await message.reply(str(e))
            return
        # Push в канал Клода прошёл — сообщение в стеке модели: 👀 → 👍.
        # 👍/👎, не ✅/❌: Telegram запрещает галочку/крестик как реакцию
        # (REACTION_INVALID). Ядро дублирует в бабле 📨-строку — она не
        # зависит от прав на реакции в чате.
        await self._react(message, "👍")

    # ── кнопки стоп/permission ──────────────────────────────────

    async def on_perm_button(self, callback: CallbackQuery) -> None:
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        parsed = cbdata.parse_perm(callback.data or "")
        session = self._session_by_thread(parsed[0]) if parsed else None
        if parsed is None or session is None:
            await callback.answer(self.t("stop_not_active"))
            return
        _, request_id, behavior = parsed
        try:
            handled = await self.core.permission_verdict(
                session, request_id, behavior, via=self.name
            )
        except UserError as e:
            await callback.answer(str(e))
            return
        await callback.answer()
        if not handled:
            await self._strip_markup(callback)

    async def on_stop_button(self, callback: CallbackQuery) -> None:
        """Кнопка 📋 «Отчёт»: просим Claude дать статус-отчёт и ПРОДОЛЖИТЬ работу
        (не останавливаться — прежний «стоп» модель считала инъекцией и
        игнорировала). Настоящее прерывание хода — кнопка ⛔ (on_esc_button →
        hard_stop, Esc в PTY)."""
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        thread_id = cbdata.parse_stop(callback.data or "")
        session = self._session_by_thread(thread_id)
        if session is None:
            await callback.answer(self.t("stop_not_active"))
            return
        origin = Origin(self.name, f"{self.chat_id}:{thread_id}:0")
        try:
            await self.core.request_report(session, origin)
            await callback.answer(self.t("stop_requested"))
            # Видимый отклик: кнопка «Стоп» → «⏹ Останавливаю…», чтобы было
            # ясно, что бот нажатие зафиксировал (тост легко пропустить).
            if isinstance(callback.message, Message):
                try:
                    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(
                                text=self.t("bubble_stopping"),
                                callback_data=cbdata.stop_cb(thread_id))]]))
                except Exception:
                    pass
        except Exception as e:
            logger.error("Сессия %s: не удалось отправить стоп: %s", session.name, e)
            await callback.answer(self.t("stop_fail"))

    async def on_esc_button(self, callback: CallbackQuery) -> None:
        """Жёсткое прерывание хода — Esc в PTY-терминал сессии (как в TUI)."""
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        thread_id = cbdata.parse_esc(callback.data or "")
        session = self._session_by_thread(thread_id)
        if session is None:
            await callback.answer(self.t("stop_not_active"))
            return
        try:
            await self.core.hard_stop(session)
            await callback.answer(self.t("esc_requested"))
        except UserError as e:
            await callback.answer(str(e))
        except Exception as e:
            logger.error("Сессия %s: не удалось прервать: %s", session.name, e)
            await callback.answer(self.t("stop_fail"))

    async def on_bg_button(self, callback: CallbackQuery) -> None:
        """Отправить текущую задачу в фон — Ctrl+B в PTY (ход не прерывается)."""
        if not self._user_allowed(callback.from_user):
            await callback.answer()
            return
        thread_id = cbdata.parse_bg(callback.data or "")
        session = self._session_by_thread(thread_id)
        if session is None:
            await callback.answer(self.t("stop_not_active"))
            return
        # Кнопка ⏭ всегда в ряду (место не «прыгает»), но при простое это дефис-
        # заглушка: сворачивать нечего → тихо гасим спиннер и НИЧЕГО не делаем
        # (без тоста). Решаем по актуальному состоянию, а не по отрисованной иконке.
        if self.core.unblock_action(session.name) is None:
            await callback.answer()
            return
        try:
            await self.core.unblock(session)
            await callback.answer(self.t("unblock_requested"))
        except UserError as e:
            await callback.answer(str(e))
        except Exception as e:
            logger.error("Сессия %s: не удалось отправить в фон: %s", session.name, e)
            await callback.answer(self.t("stop_fail"))

    # ── кнопки управления терминалом ─────────────────────────────

    async def on_edited_message(self, message: Message) -> None:
        """Правка сообщения пользователя = перезапуск команды терминала.

        Если оператор ошибся в команде и поправил своё сообщение — выполняем
        новый текст и обновляем ТОТ ЖЕ ответ. Правка постороннего сообщения
        (не того, которым запускалась команда терминала) — игнорируем молча.
        """
        if not self._accept(message):
            return
        if not message.text:
            return
        info = self._term_msgs.get(message.message_id)
        if info is None:
            return  # не наше сообщение — молча
        status_id, key, _ = info
        # Удаляем старую запись — сейчас создадим новую внутри _run_bash_cmd.
        # Статус-сообщение переиспользуем (edit_status_id).
        session = self._topic_session(message)
        # Ключ в карте — от момента запуска, а сессия резолвится заново: если
        # они разошлись (сессию удалили, топик переиспользовали под новую),
        # команда выполнилась бы в оболочке ОДНОЙ сессии с песочницей ДРУГОЙ.
        # Такую правку не исполняем: запись протухла.
        if key != self.core.bash_key(session, f"tg{message.message_thread_id or 0}"):
            del self._term_msgs[message.message_id]
            return
        new_cmd = message.text.strip()
        if not new_cmd:
            return
        # Если включён липкий режим — текст может уходить в шелл как есть;
        # иначе это был /bash — выполняем текст буквально.
        if self.core.term.is_on(key):
            where, text = self.core.term.split_escape(new_cmd)
            if where == "claude":
                return  # правка «> сообщение» — игнорируем, это не команда шелла
            new_cmd = text
        # Переисполняем с обновлением того же статус-сообщения.
        del self._term_msgs[message.message_id]
        await self._run_bash_cmd(message, session, key, new_cmd, edit_status_id=status_id)

    async def on_termint_button(self, callback: CallbackQuery) -> None:
        """⏹ Прервать бегущую команду терминала (Ctrl-C)."""
        if not self._accept_callback(callback):
            await callback.answer()
            return
        thread_id = cbdata.parse_termint(callback.data or "")
        if thread_id is None:
            await callback.answer()
            return
        session = self._topic_by_thread(thread_id)
        if session is None:
            # Нет сессии — main-ключ дал бы терминал на ХОСТЕ без песочницы.
            # Кнопка под выводом сессии не должна открывать такой путь.
            await callback.answer(self.t("term_history_gone"))
            return
        key = self.core.bash_key(session, f"tg{thread_id}")
        if self.core.interrupt_bash(key):
            await callback.answer(self.t("term_interrupted"))
        else:
            await callback.answer()

    async def on_termrepeat_button(self, callback: CallbackQuery) -> None:
        """↻ Повторить последнюю команду терминала."""
        if not self._accept_callback(callback):
            await callback.answer()
            return
        thread_id = cbdata.parse_termrepeat(callback.data or "")
        if thread_id is None:
            await callback.answer()
            return
        session = self._topic_by_thread(thread_id)
        if session is None:
            # Нет сессии — main-ключ дал бы терминал на ХОСТЕ без песочницы.
            # Кнопка под выводом сессии не должна открывать такой путь.
            await callback.answer(self.t("term_history_gone"))
            return
        key = self.core.bash_key(session, f"tg{thread_id}")
        cmds = self.core.term_last_commands(key, 1)
        if not cmds:
            await callback.answer(self.t("term_history_empty"))
            return
        await callback.answer()
        # Запускаем ту же команду заново. Сообщения-триггера нет (callback, не
        # message) — создаём фиктивное сообщение чтобы пройти через _run_bash_cmd.
        # Используем callback.message и reply.
        if isinstance(callback.message, Message):
            await self._run_bash_cmd(callback.message, session, key, cmds[0])

    async def on_termhist_button(self, callback: CallbackQuery) -> None:
        """🕘 Показать меню последних команд терминала."""
        if not self._accept_callback(callback):
            await callback.answer()
            return
        thread_id = cbdata.parse_termhist(callback.data or "")
        if thread_id is None:
            await callback.answer()
            return
        session = self._topic_by_thread(thread_id)
        if session is None:
            # Нет сессии — main-ключ дал бы терминал на ХОСТЕ без песочницы.
            # Кнопка под выводом сессии не должна открывать такой путь.
            await callback.answer(self.t("term_history_gone"))
            return
        key = self.core.bash_key(session, f"tg{thread_id}")
        cmds = self.core.term_last_commands(key)
        await callback.answer()
        if not cmds:
            # Без кнопки «Назад»: она вела на «повторить команду», то есть
            # подпись обещала одно, а нажатие делало другое.
            if isinstance(callback.message, Message):
                try:
                    await callback.message.edit_text(self.t("term_history_empty"))
                except Exception:
                    pass
            return
        # Формируем меню: каждая команда — кнопка, тап по ней выполняет.
        rows: list[list[InlineKeyboardButton]] = []
        for cmd in cmds:
            label = cmd[:50] + ("…" if len(cmd) > 50 else "")
            rows.append([InlineKeyboardButton(
                text=label,
                callback_data=cbdata.termhistrun_cb(thread_id, cmd),
            )])
        if isinstance(callback.message, Message):
            text = self.t("term_history_title") + "\n" + "\n".join(
                f"<code>{html.escape(cmd[:80])}</code>" for cmd in cmds
            )
            try:
                await callback.message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
                )
            except Exception:
                pass

    async def on_termhistrun_button(self, callback: CallbackQuery) -> None:
        """Выполнить выбранную из истории команду."""
        if not self._accept_callback(callback):
            await callback.answer()
            return
        parsed = cbdata.parse_termhistrun(callback.data or "")
        if parsed is None:
            await callback.answer()
            return
        thread_id, digest = parsed
        session = self._topic_by_thread(thread_id)
        if session is None:
            # Сессии нет (топик удалён/переиспользован) — молча уехать на
            # main-ключ значит выполнить команду на ХОСТЕ без песочницы.
            await callback.answer(self.t("term_history_gone"))
            return
        key = self.core.bash_key(session, f"tg{thread_id}")
        # Ищем по отпечатку, а не по позиции: история могла перестроиться,
        # и кнопка обязана запустить ровно ту команду, что на ней написана.
        cmd = next(
            (c for c in self.core.term_last_commands(key, 50)
             if cbdata.cmd_digest(c) == digest),
            None,
        )
        if cmd is None:
            await callback.answer(self.t("term_history_gone"))
            return
        await callback.answer()
        if isinstance(callback.message, Message):
            await self._run_bash_cmd(callback.message, session, key, cmd)

    def _topic_by_thread(self, thread_id: int) -> Session | None:
        """Найти сессию по thread_id (0 = главный чат → None)."""
        return self._session_by_thread(thread_id) if thread_id else None

    # ── отправка ────────────────────────────────────────────────

    async def _send(
        self, chat_id: int, thread_id: int | None, text: str, reply_to: int | None = None
    ) -> None:
        """Отправка с разметкой (markdown→HTML), фолбэк — чистый текст.

        Деградация: без reply (исходное сообщение удалено) → без топика
        (топик удалили руками) — финальный ответ не должен теряться.
        """
        for i, plain in enumerate(split_text(text)):
            hchunk = md_to_html(plain)
            kwargs: dict = {"chat_id": chat_id}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            if reply_to and i == 0:
                kwargs["reply_to_message_id"] = reply_to
            use_html = True
            while True:
                sent = False
                if use_html:
                    try:
                        await self.bot.send_message(text=hchunk, parse_mode="HTML", **kwargs)
                        sent = True
                    except Exception as e:
                        logger.warning("HTML-отправка не удалась, фолбэк на plain: %s", e)
                        use_html = False
                if not sent:
                    try:
                        await self.bot.send_message(text=plain, **kwargs)
                        sent = True
                    except Exception:
                        if "reply_to_message_id" in kwargs:
                            kwargs.pop("reply_to_message_id")
                        elif "message_thread_id" in kwargs:
                            kwargs.pop("message_thread_id")
                        else:
                            logger.error("Не удалось отправить сообщение в чат %s", chat_id)
                            return
                if sent:
                    break
