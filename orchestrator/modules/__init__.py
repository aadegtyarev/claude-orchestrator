"""Реестр модулей ядра: имя → фабрика.

Модуль — необязательная функциональность поверх ядра (кошелёк секретов, …).
Контракт: объект с полем name и корутинами start(core) / stop(); ядро
запускает их после адаптеров и останавливает при завершении.

Новый модуль = подпакет здесь + запись в make_modules + свой выключатель в
config (общего списка MODULES нет: он давал два источника истины для одного
кошелька — см. Config._wallet_module).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


def make_modules(config: "Config") -> list:
    out = []
    for name in config.modules:
        if name == "wallet":
            from .wallet.module import WalletModule

            out.append(WalletModule(config))
    return out
