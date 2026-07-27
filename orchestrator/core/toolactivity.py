"""Активность инструментов сессии: какой foreground-тул Claude выполняет ПРЯМО СЕЙЧАС.

Единственный источник правды для двух вещей:
  * кнопка ⏭ «свернуть в фон» (Ctrl+B) — активна, пока идёт foreground-команда;
  * сигнал «сессия жива» для hang-вотчдога (`turn.TurnSupervisor`) в случае, когда
    тул идёт, но CPU дерева на миг замер (Bash `sleep`, сетевое ожидание).

Состояние выводится ТОЛЬКО из хуков Claude Code (`PreToolUse` открывает тул,
`PostToolUse` закрывает), а НЕ из скана `/proc`. Причина: под `SANDBOX=bwrap` у
процесса сессии всегда есть живой дочерний процесс (внутренний bwrap как init
pid-namespace), поэтому проверка «есть ли дети» структурно всегда истинна —
кнопка ⏭ висела бы вечно, а вотчдог считал бы сессию живой всегда. Подробнее —
docs/archive/REVIEW-2026-07-21.md и память проекта bwrap-has-children-pitfall.

Чистый объект состояния, без внешних зависимостей: оркестратор (app.py) кормит его
из hook-хендлеров (`note_tool`/`start`/`finish`) и читает (`unblock_action`/
`inflight`), а на границе хода/teardown зовёт `forget(name)` — вместо ручной
очистки трёх словарей в нескольких местах (источник дрейф-багов до выноса).
"""

from __future__ import annotations

import time

# Сколько секунд держать кнопку ⏭ активной ПОСЛЕ завершения последнего тула —
# дебаунс против мигания на паузах-размышлениях между быстрыми тулами. Кнопка
# гаснет, только если foreground-тулов не было дольше этого окна (или ход завершён).
UNBLOCK_GRACE = 4.0


class ToolActivity:
    """Per-session состояние «какой тул идёт сейчас» + производные сигналы.

    Владеет тремя словарями (ключ — имя сессии) и НИКОГДА не делит их наружу:
      * `_last_tool`   — последний значимый тул (для детекта ожидания фон-задачи);
      * `_inflight`    — tool_use_id'ы тулов, стартовавших без PostToolUse;
      * `_cleared_at`  — monotonic-момент, когда `_inflight` опустел (grace-хвост).

    Инвариант: всё состояние сессии снимается одним `forget(name)` — вызывать на
    каждой границе хода и при teardown, чтобы состояние прошлого хода не утекло в
    следующий (иначе кнопка ⏭/вотчдог судили бы по устаревшему).
    """

    def __init__(self) -> None:
        self._last_tool: dict[str, str] = {}
        self._inflight: dict[str, set[str]] = {}
        self._cleared_at: dict[str, float] = {}

    # ── запись (из hook-хендлеров) ──────────────────────────────

    def note_tool(self, name: str, tool: str) -> None:
        """Запомнить последний значимый тул сессии (в т.ч. `TaskOutput` — модель
        ждёт фоновую задачу; в этом состоянии ⏭ означает «пнуть», а не «свернуть»)."""
        self._last_tool[name] = tool

    def start(self, name: str, tool_use_id: str) -> None:
        """PreToolUse: тул стартовал. Пустой id игнорируем (нечего сматчить с Post)."""
        if tool_use_id:
            self._inflight.setdefault(name, set()).add(tool_use_id)

    def finish(self, name: str, tool_use_id: str) -> None:
        """PostToolUse: тул завершился. Когда in-flight сессии пустеет — засекаем
        момент для grace-дебаунса кнопки ⏭."""
        inflight = self._inflight.get(name)
        if inflight is not None:
            inflight.discard(tool_use_id)
            if not inflight:
                self._cleared_at[name] = time.monotonic()

    # ── чтение (для кнопки ⏭ и вотчдога) ────────────────────────

    def last_tool(self, name: str) -> str | None:
        return self._last_tool.get(name)

    def inflight(self, name: str) -> bool:
        """Идёт ли ПРЯМО СЕЙЧАС хотя бы один тул (для hang-вотчдога)."""
        return bool(self._inflight.get(name))

    def foreground_active(self, name: str) -> bool:
        """Идёт ли foreground-тул сейчас ИЛИ завершился < UNBLOCK_GRACE назад
        (дебаунс кнопки ⏭ против мигания в паузах между быстрыми тулами)."""
        if self._inflight.get(name):
            return True
        cleared = self._cleared_at.get(name)
        return cleared is not None and (time.monotonic() - cleared) < UNBLOCK_GRACE

    def unblock_action(self, name: str) -> str | None:
        """Что сделает кнопка ⏭ сейчас: `"kick"` (Esc — прервать ожидание фон-задачи,
        когда последний тул `TaskOutput`), `"background"` (Ctrl+B — свернуть идущую
        foreground-команду) или `None` (нечего)."""
        if self._last_tool.get(name) == "TaskOutput":
            return "kick"
        if self.foreground_active(name):
            return "background"
        return None

    # ── жизненный цикл ──────────────────────────────────────────

    def forget(self, name: str) -> None:
        """Забыть всё состояние сессии — на границе хода и при teardown."""
        self._last_tool.pop(name, None)
        self._inflight.pop(name, None)
        self._cleared_at.pop(name, None)
