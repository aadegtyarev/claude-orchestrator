"""Контракт транспорт-адаптера: как ядро говорит с мессенджером/веб-интерфейсом.

Ядро (core/app.py OrchestratorCore) не знает ни про aiogram, ни про WebSocket.
Оно работает с адаптерами через этот протокол: адаптер отвечает за доставку
исходящего (сообщения, файлы, статус-бабл, permission-кнопки) и за приём
входящего (команды и сообщения пользователя — их адаптер сам транслирует в
вызовы методов ядра).

Принципы:
  * Сессия — понятие ядра; адаптер хранит для неё свой «адрес» (binding) в
    session.bindings[имя-адаптера]: у Telegram это id форум-топика, у веба
    адрес не нужен (адресация по имени). Нет binding'а — адаптер молча
    пропускает доставку (сессия для него невидима).
  * Все методы доставки — best-effort: адаптер не бросает исключения наружу
    (журналирует сам), ядро шлёт во все адаптеры и не зависит от каждого.
  * Origin — откуда пришло сообщение пользователя: адаптер + непрозрачный
    токен адаптера (у Telegram — chat:thread:message для reply-цитирования).
    context_id, который ездит через Claude, собирается ядром как
    "<адаптер>:<имя-сессии>:<токен>" — см. core/app.py.
  * Форматы текста: ядро оперирует «лёгким markdown» ответов Claude и готовыми
    HTML-строками бабла/текстов (подмножество Telegram: b/i/code/pre/s/a).
    Рендер в родной формат транспорта — забота адаптера
    (у Telegram/веба HTML нативен, mdrender.md_to_html общий).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TYPE_CHECKING, runtime_checkable

if TYPE_CHECKING:
    from .sessions import Session


@dataclass(frozen=True)
class Origin:
    """Происхождение сообщения пользователя: какой адаптер и его токен адреса.

    token — непрозрачная строка адаптера (может содержать ':'); ядро её не
    интерпретирует, только возвращает адаптеру при доставке ответа (reply-to).
    """

    adapter: str
    token: str


@dataclass(frozen=True)
class PermissionRequest:
    """Запрос разрешения от Claude Code (permission relay).

    `always_label` — текст ТРЕТЬЕЙ кнопки («разрешить навсегда», §4.6 ASK-грант
    кошелька). None (умолчание) → кнопок ровно две, как было всегда: адаптер
    третью НЕ рисует. Метка приходит от того, кто поднял запрос, вместе с
    описанием того, что именно будет записано — оператор обязан видеть это ДО
    нажатия.
    """

    request_id: str
    tool: str
    description: str
    preview: str
    always_label: str | None = None


@runtime_checkable
class Transport(Protocol):
    """Адаптер транспорта. Все корутины доставки — best-effort (не бросают)."""

    name: str
    # Нужна ли адаптеру поверхность на сессию, чтобы она была ему видна.
    # True (Telegram: без топика писать некуда) — провал bind_session при
    # создании откатывает всю сессию с ошибкой, чтобы не рождать «сессию-
    # призрак» без интерфейса. False (веб: адресация по имени) — bind
    # возвращает None штатно, сессия видна и так.
    requires_binding: bool

    async def start(self) -> None:
        """Начать обслуживание (поллинг/HTTP-сервер). Не блокирует: длинные
        циклы адаптер запускает своими задачами."""
        ...

    async def stop(self) -> None:
        """Остановить обслуживание и прибрать ресурсы."""
        ...

    # ── привязка сессий ─────────────────────────────────────────

    async def bind_session(self, session: "Session") -> str | None:
        """Создать поверхность для сессии (Telegram: форум-топик) и вернуть
        адрес-строку для session.bindings. None — адаптер не привязывает
        (сессия ему видна без адреса или не видна вовсе)."""
        ...

    async def unbind_session(self, session: "Session", address: str) -> None:
        """Убрать поверхность сессии (Telegram: удалить топик)."""
        ...

    # ── доставка ────────────────────────────────────────────────

    async def deliver_text(
        self, session: "Session", text: str, *, origin: Origin | None = None,
        intermediate: bool = False,
    ) -> None:
        """Ответ Claude (markdown-текст). origin задан, только если сообщение
        пришло из ЭТОГО адаптера — тогда можно ответить reply-цитатой.
        intermediate=True — промежуточный ответ (💬), не финал."""
        ...

    async def deliver_file(
        self, session: "Session", path: Path, caption: str, *,
        origin: Origin | None = None,
    ) -> None:
        """Файл от Claude (путь уже проверен jail'ом ядра)."""
        ...

    async def notify(self, session: "Session | None", text: str) -> None:
        """Служебное уведомление (смерть сессии, авто-останов, стартап).
        session=None — общее, вне контекста сессии."""
        ...

    async def typing(self, session: "Session") -> bool:
        """Показать «печатает…». False — адаптеру некуда слать (не привязан):
        циклу typing нет смысла жить ради него."""
        ...

    # ── статус-бабл ─────────────────────────────────────────────
    # Состояние бабла (строки, схлопывание, заморозка) держит ядро
    # (core/bubble.py); адаптер только материализует его: одно
    # редактируемое сообщение с кнопкой «Стоп». ref — непрозрачный
    # идентификатор сообщения в адаптере.

    async def bubble_post(
        self, session: "Session", html: str, *, stop_button: bool, unblock_active: bool = False
    ) -> str | None:
        """Создать сообщение бабла; вернуть ref (None — не доставлено).
        unblock_active — можно ли сейчас свернуть задачу в фон (Ctrl+B): у веба
        управляет активностью кнопки ⏬, у Telegram может игнорироваться."""
        ...

    async def bubble_edit(
        self, session: "Session", ref: str, html: str, *, stop_button: bool,
        unblock_active: bool = False,
    ) -> None:
        ...

    async def bubble_finish(
        self, session: "Session", ref: str, *, delete: bool
    ) -> None:
        """Финал: удалить сообщение (delete=True) или оставить журналом,
        сняв кнопку «Стоп»."""
        ...

    async def bubble_freeze(self, session: "Session", ref: str) -> None:
        """Заморозка: снять кнопку «Стоп», сообщение больше не редактируется."""
        ...

    # ── permission relay ────────────────────────────────────────

    async def permission_prompt(
        self, session: "Session", request: PermissionRequest
    ) -> None:
        """Показать запрос разрешения с кнопками ✅/❌; вердикт адаптер
        возвращает через core.permission_verdict(...)."""
        ...

    async def permission_resolved(
        self, session: "Session", request_id: str, behavior: str, via: str
    ) -> None:
        """Запрос разрешён/отклонён (возможно, другим адаптером via) —
        обновить/погасить свой prompt."""
        ...

    async def session_state_changed(self, session: "Session | None") -> None:
        """Состав или статус сессий изменился (создана/удалена/запущена/
        остановлена/умерла) — адаптеру, показывающему список (веб), стоит
        обновиться. Ядро зовёт при КАЖДОМ переходе, из любого источника
        (другой адаптер, idle-sweep, смерть процесса), поэтому веб больше не
        залипает на изменениях, инициированных не им. Best-effort; адаптер без
        списка (Telegram) — no-op."""
        ...
