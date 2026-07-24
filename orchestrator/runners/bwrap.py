"""BwrapRunner — файловая песочница bubblewrap вокруг всего процесса.

Allowlist: конфиг Claude Code (токены/скиллы/plugins/транскрипты) — RW,
бинарь claude и репозиторий оркестратора (channel_server + .venv) — RO,
плюс переданные рабочие каталоги (папка сессии/проекта) — RW.
Политика сборки argv — в sandbox.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence, TYPE_CHECKING

from . import sandbox

if TYPE_CHECKING:
    from ..config import Config


class BwrapRunner:
    name = "bwrap"
    unique_cwd = False
    supports_prefix = True  # /bash можно изолировать (или off — на хосте, как и весь режим)

    def __init__(self, config: "Config", root: Path):
        self.config = config
        self.root = root  # репозиторий оркестратора (channel_server.py + .venv)

    def preflight(self) -> tuple[bool, str]:
        return sandbox.available()

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
        # publish_ports не нужен: сеть у bwrap общая с хостом.
        home = Path.home()
        config_dir = self.config.claude_config_dir
        rw = [
            *extra_rw,
            config_dir or (home / ".claude"),
            # Глобальное состояние claude (~/.claude.json, пишется) — ТОЛЬКО когда
            # config-dir не задан. При заданном CLAUDE_CONFIG_DIR claude кладёт свой
            # .claude.json ВНУТРЬ него (проверено: живой ~/.claude-proxy/.claude.json),
            # т.е. файл уже покрыт биндом самого config_dir, и отдельный бинд лишь
            # утащил бы в песочницу постороннее состояние: под профилем claude-box
            # (config_dir = <profile>/.claude) это был бы реальный ~/.claude.json
            # оператора в обход изоляции профиля.
            *([home / ".claude.json"] if config_dir is None else []),
            *self.config.sandbox_extra_rw,
        ]
        ro = [
            home / ".local" / "share" / "claude",  # бинарь и versions/
            home / ".local" / "bin",               # симлинк claude
            self.root,                              # channel_server.py + .venv
        ]
        prefix = sandbox.build_argv(
            home=home, chdir=chdir, rw_paths=rw, ro_paths=ro, home_dir=home_dir,
            system_dbus=self.config.sandbox_dbus,
            # docker_sock — per-session прокси-сокет (SessionManager); внутрь
            # песочницы биндится на /run/docker.sock. Только при sandbox_docker.
            docker_sock=(docker_sock if self.config.sandbox_docker else None),
        )
        return prefix + list(argv)
