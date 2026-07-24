"""Решение policy для команды под секретом: guard (печать токена/git-RCE) + deny
+ sessions + commands → Verdict. Чистая функция, без зависимостей оркестратора и
без side-effect'ов (подтверждение кнопкой — забота вызывающего).

Причина отказа ПРЕДПИСЫВАЮЩАЯ и прозрачная (Р0): модель получает её в stderr и
идёт по верному пути, не ища обход.
"""

from __future__ import annotations

from dataclasses import dataclass

from .secret import Secret, _always_denied


@dataclass(frozen=True)
class Verdict:
    """Итог проверки policy ДО подтверждения кнопкой.

      * allowed       — команда прошла guard+deny+sessions+commands (осталось,
                        возможно, подтверждение — см. needs_confirm);
      * needs_confirm — allowed И у секрета confirm=true: вызывающий обязан
                        спросить подтверждение и, при отказе, трактовать как deny;
      * reason        — причина отказа (если НЕ allowed), иначе None. Для
                        confirm-decline reason проставляет вызывающий (side-effect).
    """

    allowed: bool
    needs_confirm: bool
    reason: str | None


def evaluate(
    secret: Secret | None,
    cmd: list[str],
    session_name: str,
    *,
    guard_on: bool,
) -> Verdict:
    """Проверить команду против policy секрета. `secret is None` → нет секрета,
    разрешающего эту команду (deny с подсказкой про `wallet ls`)."""
    cmd_str = " ".join(cmd)
    # Причина отказа: 1) встроенный guard (печать токена, git-RCE),
    # 2) точечный deny секрета. guard можно отключить на секрет (allow_unsafe)
    # или глобально (guard_on=False).
    reason: str | None = None
    if secret is not None:
        if guard_on and not secret.allow_unsafe:
            reason = _always_denied(cmd)
        if reason is None and (pat := secret.denied_by(cmd)) is not None:
            reason = f"заблокировано policy этого секрета (deny: {pat})"
    allowed = (
        secret is not None
        and secret.session_allowed(session_name)
        and secret.command_allowed(cmd)
        and reason is None
    )
    if not allowed and reason is None:
        if secret is None:
            reason = f"нет секрета для «{cmd_str[:80]}» (проверь `wallet ls`)"
        elif not secret.session_allowed(session_name):
            reason = "секрет не разрешён этой сессии (policy sessions)"
        elif not secret.command_allowed(cmd):
            reason = "команда не в списке разрешённых (policy commands)"
    needs_confirm = allowed and secret is not None and secret.confirm
    return Verdict(allowed=allowed, needs_confirm=needs_confirm, reason=reason)
