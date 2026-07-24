"""Точка входа claude-orchestrator: python -m orchestrator.

Собирает компоненты и работает до SIGINT/SIGTERM:
  config.py           — конфигурация из .env (ADAPTERS/MODULES/SANDBOX…)
  core/sessions.py    — SessionManager: процессы Claude Code
  core/app.py         — OrchestratorCore: ядро команд и маршрутизации
  adapters/           — транспорты (telegram, web) по реестру
  modules/            — модули (wallet, …) по реестру
  runners/            — изоляция процессов (bwrap | agent-vm | off)
  core/reply_server.py — HTTP: ответы Claude и события хуков -> ядро
  channel_server.py   — MCP-канал (запускается самим Claude, не отсюда)
"""

from __future__ import annotations

import asyncio
import logging
import signal

from .adapters import make_adapters
from .config import Config
from .core.app import OrchestratorCore
from .core.reply_server import start_reply_server
from .core.sessions import ROOT, SessionManager
from .modules import make_modules
from .runners import make_runner

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = Config.from_env()
    config.sessions_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Сессии: %s, максимум %d", config.sessions_dir, config.max_instances)
    logger.info("Адаптеры: %s", ", ".join(config.adapters))

    # Preflight раннера: молча деградировать до «без изоляции» нельзя.
    runner = make_runner(config, ROOT)
    ok, why = runner.preflight()
    if not ok:
        raise SystemExit(
            f"SANDBOX={config.sandbox}, но раннер недоступен: {why}\n"
            "Исправь окружение либо смени SANDBOX в .env (off — без изоляции, "
            "менее безопасно)."
        )
    if runner.name == "direct":
        logger.warning("Песочница отключена (SANDBOX=off): claude имеет доступ ко всей ФС")
    else:
        logger.info("Раннер: %s — claude и /bash изолированы", runner.name)

    # docker в песочнице (SANDBOX_BWRAP_DOCKER): у КАЖДОЙ сессии свой прокси-фильтр
    # (поднимается в SessionManager при старте сессии, allowlist = её проект).
    # Имеет смысл только под bwrap — иначе громко предупреждаем, а не молча no-op.
    if config.sandbox_docker:
        if runner.name != "bwrap":
            logger.warning(
                "SANDBOX_BWRAP_DOCKER=1, но раннер %s (не bwrap) — docker в песочнице "
                "недоступен. Работает только под SANDBOX=bwrap.", runner.name)
        else:
            logger.info("docker в песочнице включён: per-session прокси, "
                        "allowlist = папка проекта сессии + устройства")

    if not config.allowed_user_ids:
        logger.warning(
            "ALLOWED_USER_IDS пуст — оркестратор игнорирует ВСЕ сообщения. "
            "Добавь свой user id в .env, чтобы пользоваться ботом."
        )

    manager = SessionManager(config)
    manager.load_state()  # сессии с прошлого запуска: возобновятся по сообщению
    restored = manager.count()

    core = OrchestratorCore(config, manager)
    for adapter in make_adapters(config, core):
        core.register_adapter(adapter)
    core.modules = make_modules(config)

    reply_runner = await start_reply_server(
        config,
        core.handle_reply,
        core.handle_tool_event,
        core.handle_permission_request,
        core.handle_stop_event,
    )

    sweeper = asyncio.create_task(_idle_sweeper(config, manager, core))
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    try:
        await core.start()
        # Убрать баблы, осиротевшие при НЕ-graceful смерти прошлого процесса
        # (краш/SIGKILL — тогда close_all не отработал). Адаптеры уже подняты.
        await core.cleanup_stale_bubbles()
        # Стартовое уведомление после короткой паузы (адаптеры должны
        # подняться). Держим ссылку на задачу — asyncio хранит только слабую,
        # без ссылки её мог бы собрать GC; отменяем при shutdown, иначе
        # уведомление выстрелило бы уже во время остановки.
        async def _delayed_startup() -> None:
            await asyncio.sleep(2)
            await core.notify_startup(restored)

        startup_task = asyncio.ensure_future(_delayed_startup())
        await stop_event.wait()
    finally:
        logger.info("Останавливаю сессии (записи сохраняются)…")
        startup_task.cancel()
        sweeper.cancel()
        core.save_history()  # веб-история переживёт рестарт
        # Убрать активные баблы, ПОКА адаптеры живы: иначе при рестарте refs
        # теряются и бабл висит сиротой с мёртвыми кнопками.
        try:
            await asyncio.wait_for(core.bubbles.close_all(), timeout=8)
        except Exception as e:
            logger.debug("close_all при остановке: %s", e)
        await manager.shutdown()
        await reply_runner.cleanup()
        await core.close()
        logger.info("Готово.")


async def _idle_sweeper(config, manager, core) -> None:
    """Периодически останавливает сессии, простаивавшие дольше лимита."""
    if config.idle_timeout_h <= 0:
        return
    while True:
        await asyncio.sleep(600)  # проверка раз в 10 минут
        try:
            closed = await manager.close_idle()
            if closed:
                await core.notify_idle_closed(closed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка sweeper'а простоя")


if __name__ == "__main__":
    asyncio.run(main())
