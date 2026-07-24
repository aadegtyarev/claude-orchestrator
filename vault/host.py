"""VaultHost — интерфейс окружения, которое даёт демону vault его «внешние»
услуги: подтверждение, наблюдаемость, аудит, уведомление об отказе, рабочий
каталог сессии. Демон говорит ИМЕНАМИ сессий (str) и НЕ знает про Session/бота/
бабл/Telegram — их знает реализация host.

Реализации:
  * оркестратор — кнопки подтверждения, строка в статус-бабл, аудит-хук, notice
    в чат (orchestrator/modules/wallet/host.py);
  * standalone-лончер (позже) — tty-вопрос/deny, лог, cwd из аргументов.

Без зависимостей оркестратора: чистый Protocol.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Protocol

from .connectors.contract import ScopeGrant

logger = logging.getLogger("vault.host")


@dataclass(frozen=True)
class AskResult:
    """Исход ASK-спроса: разрешено ли и ЗАПИСАН ли грант в policy (§4.6).

    Зачем не голый bool. «Разрешить навсегда» меняет policy на диске, а живой
    прокси держит СВОЙ снимок scope (взят при подъёме) — не узнав о записи, он
    переспросил бы оператора на следующем таком же запросе, хотя в файле грант
    уже стоит. Поэтому хост сообщает факт записи, а прокси синхронно расширяет
    свой scope (см. proxy._ask_grant).

    `__bool__` — ради обратной совместимости: старый код (и `bool(granted)` в
    прокси) продолжает читать результат как «разрешено/нет», а хосты, ничего не
    знающие о persist, по-прежнему возвращают простой bool.
    """

    granted: bool
    persisted: bool = False

    def __bool__(self) -> bool:
        return self.granted


class VaultHost(Protocol):
    """Услуги окружения для демона vault (по имени сессии).

    Реализация сама резолвит сессию и решает, как доставить действие; если
    сессия уже недоступна (удалена) — confirm возвращает False (deny),
    наблюдаемость/аудит/уведомление тихо пропускаются.

    Примечание: рабочий каталог сессии (cwd для исполнения) сюда НЕ входит — это
    состояние сессии, а не «услуга»; демон получает его из контекста, снятого при
    аутентификации (см. слайс 1.4b), чтобы не перерезолвивать посреди запроса
    (гонка с удалением сессии → падение effective_cwd(None)).
    """

    async def confirm(self, session_name: str, description: str, preview: str) -> bool:
        """Спросить подтверждение перед выполнением. Оркестратор — кнопки;
        standalone — tty/deny. True = разрешено, False = отказано/некому спросить."""
        ...

    async def ask(
        self,
        session_name: str,
        description: str,
        preview: str,
        grant: ScopeGrant | None = None,
    ) -> bool | AskResult:
        """Спросить у оператора ГРАНТ доступа на ЭТОТ запрос (§4.6 ASK-flow:
        in_scope вернул ASK → прокси не пропускает и не отказывает сам, а
        поднимает спрос). Оркестратор — кнопки (эфемерный/persist грант),
        standalone — tty. True = разрешить этот запрос (прокси подставит кред и
        реоригинирует), False = отказать.

        `grant` (необязательный) — УЗКАЯ запись в policy, которой хост может
        предложить «разрешить навсегда»; None → узкого гранта из запроса не
        выводится, «навсегда» предлагать НЕЛЬЗЯ (только разово). Хост, умеющий
        писать policy, возвращает `AskResult(granted, persisted)`; хост, который
        не умеет (tty/standalone), — обычный bool, и это остаётся валидным.

        Р0 («никогда не повисать»): реализация ОБЯЗАНА иметь СВОЙ таймаут —
        оператор не ответил → безопасный дефолт False (DENY). Некому спросить
        (нет tty / сессия удалена) → тоже False. Значение секрета сюда не
        передаётся и не показывается: preview — это факт запроса (метод+URL)."""
        ...

    async def observe(self, session_name: str, line_html: str) -> None:
        """Наблюдаемость: показать факт вызова (НЕ значение секрета). Оркестратор —
        строка в статус-бабл, standalone — лог."""
        ...

    def record(self, session_name: str, *, secret: str, cmd: str, allowed: bool) -> None:
        """Аудит попытки (без значения секрета)."""
        ...

    async def notify_denied(self, session_name: str, cmd_display: str) -> None:
        """Уведомить оператора об отказе, требующем внимания (не self-correcting).
        Вызывающий сам решает, звать ли (напр. печать токена не уведомляет)."""
        ...


def deny_remedy(host: object | None) -> str | None:
    """Объяснение отказа ОТ ХОСТА для модели — необязательное расширение контракта.

    Зачем. Когда confirm/ask вернули False, демон/прокси знают лишь «не
    разрешено», а причина бывает разная: оператор нажал ✗, оператор молчал,
    спрашивать НЕКОГО (unattended `claude-box -p`, где вопросов не задают вовсе).
    По Р0 («никогда не повисать» + прозрачность для модели) модель должна
    получить не просто 403, а предписывающий текст: что произошло и что делать
    вместо этого. Знает это только реализация host, поэтому она может объявить
    строковый атрибут/свойство `deny_remedy`.

    Расширение НЕобязательное (getattr, а не член Protocol): у хостов
    оркестратора и `vault serve` его нет — там работают прежние формулировки, и
    ни один существующий host править не пришлось.
    """
    # Сбой host НЕ должен превращать честный 403 в 500: `deny_remedy` может быть
    # property, и её вычисление способно бросить. Тогда модель вместо
    # диагностируемого отказа получила бы Internal Server Error — ровно то, что
    # этот механизм и призван исключить. Тот же приём уже применён к host.ask()
    # в proxy._ask_grant (сбой хоста = DENY, а не падение).
    try:
        text = getattr(host, "deny_remedy", None)
    except Exception:  # noqa: BLE001 — любой сбой хоста = объяснения просто нет
        logger.warning("vault: deny_remedy у %r бросил — отказ без пояснения",
                       type(host).__name__, exc_info=True)
        return None
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def _accepts_grant(ask: object) -> bool:
    """Принимает ли `host.ask` параметр `grant` (расширение §4.6-persist).

    Контракт VaultHost.ask расширен четвёртым параметром, но реализации хоста
    бывают ЧУЖИЕ (standalone-сборки, тестовые фейки, будущие адаптеры) и написаны
    по старой сигнатуре из трёх аргументов. Слепой вызов с `grant=` уронил бы у
    них ASK в TypeError → по правилу «сбой хоста = DENY» доступ бы просто
    перестал спрашиваться: тихая деградация ровно того механизма, который мы
    расширяем. Поэтому спрашиваем сигнатуру и передаём `grant` только тем, кто
    его понимает; остальные получают прежние три аргумента и работают как раньше.

    Любой сбой интроспекции (C-функция, экзотический callable) → False:
    «не уверены — зовём по старому контракту», это всегда безопасно.
    """
    try:
        params = inspect.signature(ask).parameters
    except (TypeError, ValueError):  # не интроспектируется — зовём по-старому
        return False
    if "grant" in params:
        return True
    # **kwargs тоже считаем согласием: обёртки-декораторы прокидывают вслепую.
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def ask_grant(
    host: object,
    session_name: str,
    description: str,
    preview: str,
    grant: ScopeGrant | None,
) -> AskResult:
    """Позвать `host.ask`, нормализовав результат в `AskResult`.

    Единая точка совместимости: старый хост (bool, без `grant`) →
    `AskResult(bool, persisted=False)`; новый — как вернул. Таймауты/исключения
    НЕ ловим — ими владеет вызывающий (proxy._ask_grant, Р0)."""
    if grant is not None and _accepts_grant(host.ask):  # type: ignore[attr-defined]
        result = await host.ask(session_name, description, preview, grant=grant)  # type: ignore[attr-defined]
    else:
        result = await host.ask(session_name, description, preview)  # type: ignore[attr-defined]
    if isinstance(result, AskResult):
        return result
    return AskResult(granted=bool(result))
