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
    # Именно total(): сразу после load_state все записи числятся остановленными
    # (процессов ещё нет), а count() считает только живые и дал бы здесь 0.
    restored = manager.total()

    core = OrchestratorCore(config, manager)
    for adapter in make_adapters(config, core):
        core.register_adapter(adapter)
    core.modules = make_modules(config)

    reply_runner = None
    sweeper = None
    startup_task = None
    core_start_task = None
    stop_wait_task = None
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async def _cleanup_step(label, action) -> None:
        try:
            await action()
        except Exception:
            logger.exception("Ошибка при остановке: %s", label)

    async def _cancel_task(task, label) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Фоновая задача завершилась с ошибкой при остановке: %s", label)

    try:
        reply_runner = await start_reply_server(
            config,
            core.handle_reply,
            core.handle_tool_event,
            core.handle_permission_request,
            core.handle_stop_event,
        )
        sweeper = asyncio.create_task(_idle_sweeper(config, manager, core))
        # Startup может включать отменяемый сетевой backoff (Telegram). SIGTERM
        # должен прервать его, а не ждать завершения core.start().
        core_start_task = asyncio.create_task(core.start(), name="core-start")
        stop_wait_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
        done, _ = await asyncio.wait(
            (core_start_task, stop_wait_task), return_when=asyncio.FIRST_COMPLETED
        )
        if core_start_task in done:
            await core_start_task  # сохранить исходную startup-ошибку
        else:
            await _cancel_task(core_start_task, "core-start")

        if core_start_task in done and not stop_event.is_set():
            # Убрать баблы после поднятия адаптеров и сохранения состояния.
            await core.cleanup_stale_bubbles()

            # Стартовое уведомление после короткой паузы (адаптеры уже готовы).
            async def _delayed_startup() -> None:
                await asyncio.sleep(2)
                await core.notify_startup(restored)

            startup_task = asyncio.create_task(_delayed_startup(), name="startup-notification")

        if not stop_event.is_set():
            await stop_wait_task
    finally:
        logger.info("Останавливаю сессии (записи сохраняются)…")
        await _cancel_task(startup_task, "startup-notification")
        await _cancel_task(core_start_task, "core-start")
        await _cancel_task(sweeper, "idle-sweeper")
        await _cancel_task(stop_wait_task, "shutdown-signal")
        try:
            core.save_history()  # веб-история переживёт рестарт
        except Exception:
            logger.exception("Не удалось сохранить веб-историю")
        # Баблы удаляем ПОКА адаптеры живы; последующие shutdown-шаги
        # выполняются независимо, даже если этот запрос/cleanup завершится ошибкой.
        await _cleanup_step(
            "закрытие статус-баблов",
            lambda: asyncio.wait_for(core.bubbles.close_all(), timeout=8),
        )
        await _cleanup_step("завершение сессий", manager.shutdown)
        if reply_runner is not None:
            await _cleanup_step("reply server", reply_runner.cleanup)
        await _cleanup_step("ядро и адаптеры", core.close)
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
