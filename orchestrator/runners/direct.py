"""DirectRunner — без изоляции (SANDBOX=off)."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence


class DirectRunner:
    """Без изоляции: команда запускается как есть (SANDBOX=off)."""

    name = "direct"
    unique_cwd = False
    supports_prefix = True  # /bash можно изолировать (или off — на хосте, как и весь режим)

    def preflight(self) -> tuple[bool, str]:
        return True, "ok"

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
        return list(argv)
