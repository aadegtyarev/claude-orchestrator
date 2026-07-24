"""Реестр коннекторов Vault (§4.5). «Выключено = не существует»: если policy
секрета ссылается на неизвестный connector — секрет НЕ активируется, а факт
громко логируется (не молчаливый фолбэк на «пропустить всё»).

Пакет автономен: только stdlib + vault.* (ни строки из orchestrator).
"""

from __future__ import annotations

import logging

from .contract import Connector, HttpReq, ScopeGrant, ScopeVerdict, with_header
from .gdocs import GDocsConnector
from .generic_bearer import GenericBearerConnector

logger = logging.getLogger("vault.connectors")

# Реестр встроенных коннекторов: имя → экземпляр. Расширяется по мере добавления
# (gdocs/jenkins/…) через register().
_REGISTRY: dict[str, Connector] = {}


def register(connector: Connector) -> None:
    """Зарегистрировать коннектор под его `name` (перекрывает одноимённый)."""
    _REGISTRY[connector.name] = connector


def get_connector(name: str) -> Connector | None:
    """Коннектор по имени из policy, либо None для неизвестного.

    None — сигнал вызывающему НЕ активировать секрет (выключено = не существует).
    Неизвестное имя логируем громко (WARNING): скорее опечатка/выключенная фича в
    secrets.toml, чем норма — молчать нельзя (прозрачность для оператора, Р0).
    """
    connector = _REGISTRY.get(name)
    if connector is None:
        logger.warning(
            "неизвестный connector %r в policy — секрет НЕ активен "
            "(выключено = не существует); известные: %s",
            name,
            ", ".join(available()) or "(нет)",
        )
    return connector


def available() -> tuple[str, ...]:
    """Имена зарегистрированных коннекторов (отсортированы)."""
    return tuple(sorted(_REGISTRY))


# Встроенные коннекторы.
register(GenericBearerConnector())
register(GDocsConnector())

__all__ = [
    "Connector",
    "HttpReq",
    "ScopeGrant",
    "ScopeVerdict",
    "with_header",
    "GenericBearerConnector",
    "GDocsConnector",
    "register",
    "get_connector",
    "available",
]
