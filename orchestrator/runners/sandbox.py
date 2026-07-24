"""Файловая песочница для процессов Claude Code и /bash-терминала (bubblewrap).

Весь процесс claude — вместе с его детьми (MCP-канал channel_server.py, хуки,
встроенный Bash-тул) — запирается в отдельный mount-namespace. Там $HOME это
пустой tmpfs, и наружу торчат ТОЛЬКО явно разрешённые пути:

    RW  — папка сессии, папка проекта (linked_path), конфиг Claude Code
          (CLAUDE_CONFIG_DIR + ~/.claude.json): токены, скиллы, plugins,
          транскрипты;
    RO  — сам бинарь claude, репозиторий оркестратора (channel_server.py +
          .venv для канала и хуков), системный рантайм (/usr, /etc, …).

Всё остальное в $HOME (другие проекты, ~/.ssh, ~/.aws, история) не видно —
ни на чтение, ни на запись. Сеть общая с хостом: нужна для API Anthropic и
localhost-оркестратора; фильтрации сети по доменам здесь нет (bwrap её не
умеет — потребовался бы отдельный sandbox-runtime).

Почему обёртка вокруг всего процесса, а не нативная песочница Claude Code:
нативный /sandbox изолирует только Bash-тул, а Read/Write/Edit, MCP и хуки
работают на хосте без ограничений. Обёртка вокруг всего claude накрывает их
разом.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

BWRAP = "bwrap"

# Системный рантайм, монтируемый только на чтение: интерпретаторы, библиотеки,
# TLS-сертификаты (/etc/ssl), DNS (/etc/resolv.conf), /etc/passwd.
_SYSTEM_RO = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc")


def available() -> tuple[bool, str]:
    """Готова ли песочница: есть bwrap и ядро разрешает unprivileged userns.

    Возвращает (ok, причина). Причина осмысленна только при ok=False.
    """
    if shutil.which(BWRAP) is None:
        return False, "bwrap не установлен (apt install bubblewrap)"
    probe = [BWRAP, "--proc", "/proc", "--dev", "/dev", "--unshare-pid"]
    for p in _SYSTEM_RO:
        probe += ["--ro-bind-try", p, p]  # ld-linux/libc обязательны для exec
    probe += ["--", "true"]
    try:
        proc = subprocess.run(probe, capture_output=True, timeout=10)
    except Exception as e:  # noqa: BLE001 — любой сбой = песочница недоступна
        return False, f"bwrap не запускается: {e}"
    if proc.returncode != 0:
        why = proc.stderr.decode(errors="replace").strip() or "неизвестная ошибка"
        return False, (
            "ядро отклонило unprivileged user namespace: " + why +
            " (Ubuntu 24.04+: см. kernel.apparmor_restrict_unprivileged_userns)"
        )
    return True, "ok"


def _bind(args: list[str], flag: str, path: Path | str | None) -> None:
    """Добавить bind в обе стороны (src==dst). Используются `*-try`-флаги —
    несуществующий источник bwrap молча пропускает, падать не нужно."""
    if path is None:
        return
    args += [flag, str(path), str(path)]


def build_argv(
    *,
    home: Path,
    chdir: Path,
    rw_paths: list[Path],
    ro_paths: list[Path],
    home_dir: Path | None = None,
    system_dbus: bool = True,
    docker_sock: Path | None = None,
) -> list[str]:
    """Собрать argv-префикс bwrap. Итог: [bwrap, …, "--"] — дальше сама команда.

    Порядок важен: сначала подмена $HOME (прячет всё домашнее), затем RO-биндами
    кладём рантайм и код, и в самом конце RW-биндами — рабочие каталоги, чтобы при
    пересечении путей запись всегда побеждала (например, linked_path == репозиторий
    оркестратора: он и RO как код, и RW как проект → останется RW).

    home_dir — персистентный приватный дом сессии: монтируется НА МЕСТО $HOME
    вместо пустого tmpfs. Реальный дом по-прежнему скрыт, но всё, что процесс
    пишет «к себе домой» (venv, кэши pip/npm, dotfiles), переживает рестарт —
    иначе агентские окружения испарялись вместе с tmpfs (живой инцидент:
    пропавший ~/.venv). None = прежнее поведение (эфемерный tmpfs).
    """
    args: list[str] = [
        BWRAP,
        "--die-with-parent",     # bwrap умрёт с ботом → канал/хуки внутри тоже
        "--unshare-pid",         # чистое дерево процессов; kill накрывает всех
        "--unshare-ipc",
        "--unshare-uts",
        # сеть НЕ изолируем: нужна для api.anthropic.com и 127.0.0.1:orch_port
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
    ]

    # Резолюция имён. Сеть песочницы НЕ изолируем (общая с хостом), поэтому
    # возвращаем в /run рантайм-сокеты резолверов, спрятанные tmpfs /run выше.
    #
    # Обычный DNS — ВСЕГДА: /etc/resolv.conf часто симлинк в /run (systemd-
    # resolved → /run/systemd/resolve, NetworkManager → /run/NetworkManager,
    # resolvconf → /run/resolvconf); без цели симлинка внутри песочницы умирает
    # вся DNS-резолюция (живой инцидент: github.com не резолвится, curl/gh глохнут).
    for p in ("/run/systemd/resolve", "/run/NetworkManager", "/run/resolvconf"):
        _bind(args, "--ro-bind-try", p)
    try:
        real_resolv = Path("/etc/resolv.conf").resolve()
        if str(real_resolv).startswith("/run/"):
            _bind(args, "--ro-bind-try", real_resolv)
    except OSError:
        pass

    # System D-Bus (SANDBOX_DBUS, опционально). Главный юзкейс — mDNS/локальная
    # сеть: `.local`-хосты и обзор сервисов (avahi-browse, avahi-resolve,
    # DNS-SD) идут через system D-Bus, где зарегистрирован Avahi. ⚠️ Это
    # открывает ВЕСЬ system D-Bus, не только Avahi (systemd, logind,
    # NetworkManager…): read-методы работают, мутации остаются под polkit.
    # Осознанное расширение поверхности — выключается SANDBOX_DBUS=off.
    # Базовый `.local`-резолв хоста работает и без D-Bus (nss-mdns minimal шлёт
    # multicast напрямую); D-Bus нужен для service discovery и avahi-client.
    if system_dbus:
        for p in ("/run/dbus", "/run/avahi-daemon"):
            _bind(args, "--ro-bind-try", p)

    # 1) системный рантайм — только чтение.
    for p in _SYSTEM_RO:
        _bind(args, "--ro-bind-try", p)

    # 2) $HOME: персистентный приватный дом сессии или пустой tmpfs.
    #    В обоих случаях реальное содержимое $HOME скрыто.
    if home_dir is not None:
        args += ["--bind", str(home_dir), str(home)]
    else:
        args += ["--tmpfs", str(home)]

    # 3) RO-пути под $HOME (бинарь claude, репозиторий с channel_server/venv).
    for p in ro_paths:
        _bind(args, "--ro-bind-try", p)

    # 4) RW-пути (последними — побеждают при пересечении с RO).
    for p in rw_paths:
        _bind(args, "--bind-try", p)

    # 5) docker: возвращаем ПРОКСИ-сокет на место /run/docker.sock (скрытого
    #    tmpfs /run выше). Настоящий /run/docker.sock внутрь НЕ попадает —
    #    прокси фильтрует опасное (см. modules/docker). DOCKER_HOST — чтобы
    #    docker/compose нашли сокет (в песочнице /var/run может не быть).
    if docker_sock is not None:
        args += ["--bind-try", str(docker_sock), "/run/docker.sock"]
        args += ["--setenv", "DOCKER_HOST", "unix:///run/docker.sock"]

    args += ["--chdir", str(chdir), "--"]
    return args
