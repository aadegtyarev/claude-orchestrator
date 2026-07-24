"""Раннеры: как превратить команду (claude, /bash) в argv для запуска.

Шов изоляции: SessionManager и /bash не знают, во что заворачивается процесс —
они дают команду, рабочий каталог, список RW-путей и (опционально) приватный
$HOME сессии; раннер возвращает готовый argv. Выбор — SANDBOX в .env:

  * direct   — без изоляции (SANDBOX=off): argv как есть;
  * bwrap    — файловая песочница bubblewrap (SANDBOX=bwrap, по умолчанию);
  * agent-vm — microVM через wirenboard/agent-vm (SANDBOX=agent-vm, каркас —
    см. docs/agent-vm-integration.md).

Новый раннер = модуль в этом пакете + запись в реестр make_runner + значение
в config._parse_sandbox. Больше ничего трогать не надо: и claude, и /bash
запускаются через раннер.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config


class Runner(Protocol):
    """Интерфейс раннера: argv команды → argv запуска."""

    name: str
    # Раннер допускает только одну сессию на рабочий каталог (agent-vm:
    # имя VM = hash(cwd), вторая сессия убьёт VM первой). SessionManager
    # проверяет это при создании сессии.
    unique_cwd: bool
    supports_prefix: bool  # можно ли изолировать отдельный /bash (agent-vm — нет)

    def preflight(self) -> tuple[bool, str]:
        """Готов ли раннер к работе: (ok, причина-если-нет).

        Вызывается один раз на старте оркестратора; при ok=False запуск
        прерывается с понятной ошибкой (молча без изоляции не работаем).
        """
        ...

    def wrap(
        self,
        argv: Sequence[str],
        *,
        chdir: Path,
        extra_rw: list[Path],
        home_dir: Path | None = None,
        publish_ports: Sequence[int] = (),
        docker_sock: Path | None = None,
    ) -> list[str]:
        """Завернуть команду. chdir — рабочий каталог процесса; extra_rw —
        рабочие пути этой сессии (папка сессии/проекта), доступные на запись;
        home_dir — персистентный приватный $HOME сессии (None = эфемерный);
        publish_ports — localhost-порты процесса, которые должны быть доступны
        оркестратору снаружи изоляции (channel-сервер; важно для agent-vm)."""
        ...


def make_runner(config: "Config", root: Path) -> Runner:
    """Раннер по конфигу (валидацию значения SANDBOX делает config.py)."""
    from .agentvm import AgentVmRunner
    from .bwrap import BwrapRunner
    from .direct import DirectRunner

    registry = {
        "bwrap": lambda: BwrapRunner(config, root),
        "agent-vm": lambda: AgentVmRunner(config, root),
        "off": DirectRunner,
    }
    return registry[config.sandbox]()
