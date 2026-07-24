"""Ре-экспорт правки policy кошелька из автономного пакета `vault`.

Логика переехала в `vault/policy.py` (фаза 1 редизайна — домен кошелька без
зависимостей оркестратора). Здесь — тонкий шим для обратной совместимости:
модуль и тесты продолжают импортировать `PolicyEditor`/`PolicyError` отсюда.
"""
from __future__ import annotations

from vault.policy import USAGE, PolicyEditor, PolicyError

__all__ = ["PolicyEditor", "PolicyError", "USAGE"]
