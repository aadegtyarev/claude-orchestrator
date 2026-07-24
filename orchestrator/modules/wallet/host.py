"""OrchestratorVaultHost — реализация vault.host.VaultHost поверх ядра
оркестратора: подтверждение = кнопки (permission-relay), наблюдаемость = строка
в статус-бабл, аудит = core._record, уведомление = notice в чат, cwd = проект
сессии.

Демон vault обращается по ИМЕНИ сессии; адаптер резолвит имя в Session через
manager.get (сессия могла быть удалена после выдачи токена — тогда мягкая
деградация: confirm→False, остальное тихо пропускается). i18n текста notice
живёт здесь, а не в vault.

Здесь же живёт ПОСТОЯННЫЙ ASK-грант (§4.6): третья кнопка «разрешить навсегда»
и запись узкого гранта в policy через PolicyEditor. Почему тут, а не в vault:
решение «показывать ли кнопку» зависит от оркестраторного тумблера
WALLET_POLICY_EDIT и от способа доставки кнопок (permission-relay), а vault обязан
работать и без оркестратора — его дело отдать МАШИННЫЙ грант (ScopeGrant), не
рендерить UX.
"""

from __future__ import annotations

import asyncio
import logging

from vault.connectors.contract import ScopeGrant
from vault.host import AskResult

from .policy import PolicyEditor, PolicyError

logger = logging.getLogger(__name__)

# Таймаут спроса гранта у оператора. Держим ЗАВЕДОМО ниже страховочного
# потолка прокси (vault.proxy._ASK_TIMEOUT = 180с), чтобы наш собственный
# таймаут сработал первым: request_confirmation по истечении гасит кнопки во
# всех адаптерах (иначе прокси уже вернул бы DENY, а «висящие» ✅/❌ вводили бы
# оператора в заблуждение). Оператор не ответил → False (DENY, Р0).
_ASK_CONFIRM_TIMEOUT = 150.0

# Потолок доставки notice оператору. Клик (150с) + запись + notice обязаны
# укладываться в потолок прокси (180с), иначе висящий адаптер обрубил бы уже
# успешную запись гранта. 150 + ~1 + 15 < 180 — с запасом.
_NOTIFY_TIMEOUT = 15.0


class OrchestratorVaultHost:
    """VaultHost на ядре оркестратора. Один экземпляр на модуль кошелька.

    `policy`+`allow_policy_edit` нужны ТОЛЬКО для постоянного ASK-гранта (§4.6):
    без них (или при выключенном WALLET_POLICY_EDIT) хост работает как раньше и
    третью кнопку не предлагает вовсе — «выключено = не существует».
    """

    def __init__(
        self,
        core,
        policy: "PolicyEditor | None" = None,
        allow_policy_edit: bool = False,
    ):
        self._core = core
        self._policy = policy
        self._allow_policy_edit = allow_policy_edit
        # Фоновые задачи доставки notice: держим ссылки, чтобы их не собрал GC до
        # завершения (см. _persist_grant — notice уходит фоном, не в критическом
        # пути ответа на ASK).
        self._notify_tasks: set = set()

    async def confirm(self, session_name: str, description: str, preview: str) -> bool:
        session = self._core.manager.get(session_name)
        if session is None:
            return False
        return await self._core.request_confirmation(
            session, tool="wallet", description=description, preview=preview,
        )

    def _grant_offer(self, grant: ScopeGrant | None) -> tuple[str | None, str]:
        """Можно ли предложить «навсегда» → (метка кнопки | None, довесок к тексту).

        Кнопка появляется, только если ВСЁ сразу:
          * коннектор дал УЗКИЙ грант (grant не None — см. _narrow_grant: запрос к
            корню сервиса гранта не даёт, «разрешить всё» кнопкой не выдаём);
          * грант знает, в какой секрет писать (secret проставляет прокси);
          * правка policy включена и редактор есть (WALLET_POLICY_EDIT=1) —
            «выключено = не существует»: ни кнопки, ни намёка на неё.

        Когда нельзя — возвращаем метку None и ПИШЕМ ПОЧЕМУ: молчаливое
        исчезновение кнопки оператор прочитал бы как «фича сломалась».
        """
        if grant is None or not grant.secret:
            return None, self._core.t(
                "wallet_ask_always_off",
                reason=self._core.t("wallet_ask_reason_narrow"))
        if self._policy is None or not self._allow_policy_edit:
            return None, self._core.t(
                "wallet_ask_always_off",
                reason=self._core.t("wallet_ask_reason_disabled"))
        return (
            self._core.t("wallet_ask_always_btn"),
            self._core.t(
                "wallet_ask_always_offer",
                secret=grant.secret, key=grant.key, value=grant.value,
                label=grant.label),
        )

    async def ask(
        self,
        session_name: str,
        description: str,
        preview: str,
        grant: ScopeGrant | None = None,
    ) -> AskResult:
        """Спрос ГРАНТа доступа ВНЕ scope (§4.6 ASK-flow). Рендер — те же кнопки
        permission-relay, что и confirm, но с текстом, явно маркирующим ЗАПРОС
        РАСШИРЕНИЯ доступа (🔓 «доступ ВНЕ scope»), чтобы оператор не спутал его
        со штатным подтверждением команды под секретом.

        Исходов ТРИ (третий — этот срез):
          * ❌ / таймаут / сессия удалена → отказ (Р0: молчание = DENY);
          * ✅ → РАЗОВЫЙ грант: кред уходит только в этот запрос, policy не
            меняется (как было);
          * 🔒 «навсегда» → УЗКАЯ запись в policy (`grant`), после которой такой
            запрос проходит без спроса. Кнопка предлагается не всегда — см.
            _grant_offer; ЧТО именно запишется, оператор видит в тексте запроса
            ДО нажатия (иначе «навсегда» было бы подписью вслепую).

        description — от коннектора (что за ресурс/почему вне scope); preview —
        факт запроса (метод+URL), куда уйдёт кред. Значение секрета сюда НЕ
        приходит и в текст не попадает.
        """
        session = self._core.manager.get(session_name)
        if session is None:
            return AskResult(granted=False)
        always_label, offer_note = self._grant_offer(grant)
        decision = await self._core.request_choice(
            session,
            tool=self._core.t("wallet_ask_tool"),
            description=self._core.t("wallet_ask_desc", description=description) + offer_note,
            preview=preview,
            timeout=_ASK_CONFIRM_TIMEOUT,
            always_label=always_label,
        )
        if decision == "deny":
            return AskResult(granted=False)
        if decision != "allow_always":
            return AskResult(granted=True)          # разовый грант, policy не тронут
        return await self._persist_grant(session, grant)

    async def _persist_grant(self, session, grant: ScopeGrant) -> AskResult:
        """Записать постоянный грант в policy и ЧЕСТНО отчитаться оператору.

        Сбой записи (нет файла/лок занят/битый TOML) НЕ превращаем в тихое
        «разрешено разово»: доступ на этот запрос оператор уже одобрил (отзывать
        его назад — сюрприз похуже), но факт «в policy НЕ записано» уходит в чат
        отдельным notice, а persisted=False оставляет прокси со старым scope —
        следующий такой запрос честно спросит снова. Ситуации «оператор думает,
        что грант выдан навсегда, а его нет» не возникает.

        Доставка notice — ФОНОМ (_notify_async), не в критическом пути ответа.
        Иначе возникала гонка (нашло ревью): оператор кликает у самой границы
        ASK-вотчдога прокси, грант уже записан, а зависшая доставка notice не даёт
        ask вернуться в бюджет — прокси рвёт вызов по таймауту, и грант остаётся в
        файле, но запрос получает DENY. Раз вердикт известен сразу после записи —
        возвращаем его немедленно, а оператору сообщаем асинхронно.
        """
        assert grant is not None  # кнопка «навсегда» без гранта не рисуется
        try:
            self._policy.grant_scope(
                grant.secret, grant.key, grant.value, exist_ok=True)
        except (PolicyError, OSError) as e:
            logger.error(
                "wallet: постоянный грант НЕ записан (%s.%s += %s): %s",
                grant.secret, grant.key, grant.value, e)
            self._notify_bg(
                session, self._core.t("wallet_ask_write_failed", error=e))
            return AskResult(granted=True, persisted=False)
        self._notify_bg(
            session,
            self._core.t(
                "wallet_ask_written",
                secret=grant.secret, key=grant.key, value=grant.value),
        )
        self._core._record(
            session, "wallet", secret=grant.secret,
            cmd=f"policy scope.{grant.key} += {grant.value}", allowed=True)
        return AskResult(granted=True, persisted=True)

    def _notify_bg(self, session, text: str) -> None:
        """Запустить доставку notice ФОНОМ и вернуться сразу (см. _persist_grant).

        Ссылку на задачу держим в _notify_tasks до её завершения, иначе GC мог бы
        собрать её на полпути (asyncio задачи слабо удерживаются)."""
        task = asyncio.ensure_future(self._notify_async(session, text))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    async def _notify_async(self, session, text: str) -> None:
        """Фоновая доставка notice оператору. Сбой/зависание не влияют на вердикт:
        ловим всё и логируем, плюс собственный таймаут против зависшего адаптера."""
        try:
            await asyncio.wait_for(self._core.notice(session, text), timeout=_NOTIFY_TIMEOUT)
        except Exception:  # noqa: BLE001 — доставка notice не влияет на вердикт
            logger.exception("wallet: не удалось доставить notice оператору")

    async def observe(self, session_name: str, line_html: str) -> None:
        # append_background адресуется по имени сессии — резолв не нужен.
        await self._core.bubbles.append_background(session_name, line_html, tool="wallet")

    def record(self, session_name: str, *, secret: str, cmd: str, allowed: bool) -> None:
        session = self._core.manager.get(session_name)
        if session is None:
            return
        self._core._record(session, "wallet", secret=secret, cmd=cmd, allowed=allowed)

    async def notify_denied(self, session_name: str, cmd_display: str) -> None:
        session = self._core.manager.get(session_name)
        if session is None:
            return
        notice_md = f"🔐 wallet: `{cmd_display.replace('`', chr(39))}`"
        await self._core.notice(
            session,
            self._core.t("wallet_use", line=notice_md) + " — " + self._core.t("wallet_denied"),
        )
