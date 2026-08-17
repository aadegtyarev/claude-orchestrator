"""Чистые парсеры/форматтеры отчётов (без состояния и без ядра).

Пока — только разбор вывода `/cost` из TUI Claude Code. Живёт отдельно от app.py:
это чистая regex-логика, которую удобно тестировать и переиспользовать без
инстанса координатора. Форматтеры, которым нужны manager/тексты/session
(stats_text/usage_text/model_display), остаются в OrchestratorCore — они
stateless-оркестрация над ядром, а не самостоятельные единицы.
"""

from __future__ import annotations

import re


def parse_cost(text: str) -> dict:
    """Выдрать цифры из TUI-каши `/cost` (наложенные кадры, рамки псевдографики).

    Возвращает подмножество ключей: `cost`, `session_pct`, `week_pct`,
    `session_reset`/`week_reset`, `models` [(имя, pct), …]. Мусор/пустой ввод →
    пустой dict (адаптер деградирует в usage_failed).
    """
    t = re.sub(r"[│▏▐▔▕█▌▊▋▉▛▜✶✢·…✻✽✼✾*]+", " ", text)
    t = re.sub(r"\s+", " ", t)
    out: dict = {}
    if m := re.search(r"cost:\s*\$([\d.]+)", t):
        out["cost"] = m.group(1)
    if m := re.search(r"Current session.*?(\d+)%\s*used", t):
        out["session_pct"] = m.group(1)
    if m := re.search(r"Current week \(all models\).*?(\d+)%\s*used", t):
        out["week_pct"] = m.group(1)
    # Дедуп по имени модели: в дельте лога экран /cost почти всегда лежит
    # ДВАЖДЫ (TUI перерисовывает его целиком), и без этого «Неделя, Fable: 19%»
    # приезжало в чат двумя одинаковыми строками (живой /usage ikar).
    seen_models: set[str] = set()
    for mm in re.finditer(r"Current week \((?!all models)([^)]+)\).*?(\d+)%\s*used", t):
        name = mm.group(1).strip()
        if name in seen_models:
            continue
        seen_models.add(name)
        out.setdefault("models", []).append((name, mm.group(2)))
    resets = re.findall(r"Resets? ([A-Za-z0-9:, ]+?\([^)]+\))", t)
    if resets:
        out["session_reset"] = resets[0].strip()
        if len(resets) > 1:
            out["week_reset"] = resets[1].strip()
    return out
