"""Снятие ANSI/управляющих escape-последовательностей из байтов вывода PTY.

Живёт в автономном пакете `box/` (launcher), потому что от него зависит матчер
стартовых диалогов (dialog.py) — а пакет обязан импортироваться без
orchestrator. Ровно тот же regex/поведение, что и прежде в
orchestrator.core.ansi; последний теперь реэкспортит отсюда, поэтому
sessions.py/turn.py/bashshell.py и тесты продолжают работать без изменений
(REVIEW.md D2 — единое место снятия ANSI сохраняется).
"""

from __future__ import annotations

import re

ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]")


def strip_ansi(raw: bytes) -> bytes:
    """Убрать ANSI-раскраску/управление и \\r — для показа в <pre> и парсинга лога."""
    return ANSI_RE.sub(b"", raw).replace(b"\r", b"")
