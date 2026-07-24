"""Снятие ANSI/управляющих escape-последовательностей из байтов вывода PTY.

Реализация переехала в автономный пакет `box/` (launcher, Слой 2 редизайна):
от неё зависит матчер стартовых диалогов (box.dialog), а `box/` обязан
импортироваться без orchestrator. Этот модуль — тонкий РЕЭКСПОРТ из box.ansi
для обратной совместимости: sessions.py/turn.py/bashshell.py импортируют
strip_ansi из `orchestrator.core.ansi` как раньше (REVIEW.md D2 — единое место
снятия ANSI: раньше regex дублировался в sessions/bashshell/bot, теперь один
источник в box.ansi).
"""

from __future__ import annotations

from box.ansi import ANSI_RE, strip_ansi

__all__ = ["ANSI_RE", "strip_ansi"]
