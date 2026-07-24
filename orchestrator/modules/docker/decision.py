"""Модель угроз docker-прокси в чистом виде: (метод, URI, тело) → allow/deny.
Ни сети, ни сокета — только решение. Транспорт (тонкий прокси над сокетом) в
proxy.py; прокси стоит ТОЛЬКО в песочнице модели — личный докер оператора мимо
него, поэтому оператору тут ничего не запрещено.

Скоуп = ПАПКА ПРОЕКТА + устройства (оператор задал явно). Это ALLOWLIST (не
денайлист): монтировать можно ТОЛЬКО под разрешёнными корнями (RW-область
песочницы: папки проектов сессий), всё прочее — отказ. Так нет дыр «забыл путь», а
секреты (~/.ssh, ~/.aws, secrets.toml) и система (/etc, /usr) отсекаются сами —
они вне папок проектов. Не от намеренного побега — та граница у agent-vm; это
соломка от СЛУЧАЙНОСТЕЙ.

Запрещаем (у модели):
  * bind ЛЮБОГО пути вне разрешённых корней проекта (allowlist)
  * то же для --mount type=bind и volume create с o=bind,device
  * escape в обход скоупа: --privileged, Pid/Ipc/UTS/Userns=host, опасные --cap-add

Сознательно РАЗРЕШАЕМ:
  * --device (USB/TTY — оператор просил)
  * bind под корнями проекта; именованные тома
  * публикацию портов и network=host — песочница и так на сети хоста

create без разобранного тела → deny (безопасная сторона). Корни резолвятся при
запросе (сессии приходят/уходят) — Policy строится динамически.

См. [[docker-in-sandbox]].
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

# Опасные capabilities: фактический выход на хост в обход скоупа. Узкий список.
_DANGEROUS_CAPS = {"SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "SYS_BOOT",
                   "SYS_RAWIO", "DAC_READ_SEARCH", "ALL"}

# host-namespace режимы в HostConfig.<ключ>.
_HOST_NS = ("PidMode", "IpcMode", "UTSMode", "UsernsMode")

# Путь версионируется: /v1.43/... — терпим к префиксу.
_VERSION_RE = re.compile(r"^/v[0-9]+\.[0-9]+")


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str = ""


@dataclass(frozen=True)
class Policy:
    """Разрешённые корни (папки проектов) + опасные caps. Строится динамически из
    текущих сессий (allowed_roots) — прокси зовёт evaluate с актуальной Policy."""

    allowed_roots: tuple[str, ...]
    dangerous_caps: frozenset[str] = field(default_factory=lambda: frozenset(_DANGEROUS_CAPS))

    @classmethod
    def for_roots(cls, roots) -> "Policy":
        norm = tuple(str(Path(r)) for r in roots)
        return cls(allowed_roots=norm)


def _norm(path: str) -> str:
    return posixpath.normpath(path)


def _under(path: str, root: str) -> bool:
    p = PurePosixPath(_norm(path))
    r = PurePosixPath(_norm(root))
    return p == r or r in p.parents


def _is_host_path(src: str) -> bool:
    """bind хоста (абсолютный путь) против именованного тома (`myvol`)."""
    return src.startswith("/")


def _allowed(src: str, roots: tuple[str, ...]) -> bool:
    return any(_under(src, r) for r in roots)


def _deny_bind(src: str, roots: tuple[str, ...]) -> str:
    allowed = ", ".join(roots) or "(нет разрешённых корней)"
    return (
        f"монтирование `{src}` вне папки проекта. Смонтировать можно только под: "
        f"{allowed}. Секреты и системные пути недоступны из песочницы."
    )


def _check_binds(binds, roots) -> str | None:
    for entry in binds or []:
        parts = str(entry).split(":")
        if len(parts) < 2:
            continue  # анонимный том без src
        src = parts[0]
        if not _is_host_path(src):
            continue  # именованный том — не путь хоста
        if not _allowed(_norm(src), roots):
            return _deny_bind(src, roots)
    return None


def _check_mounts(mounts, roots) -> str | None:
    for mnt in mounts or []:
        if (mnt.get("Type") or "volume") != "bind":
            continue
        src = mnt.get("Source") or mnt.get("source") or ""
        if src and _is_host_path(src) and not _allowed(_norm(src), roots):
            return _deny_bind(src, roots)
    return None


def _strip_version(path: str) -> str:
    return _VERSION_RE.sub("", path, count=1)


def endpoint(method: str, uri: str) -> str:
    """Ключевой эндпоинт: 'create'|'volume_create'|'other'. Прокси по нему решает,
    надо ли читать и проверять тело запроса (только у create/volume_create)."""
    p = _strip_version(uri).split("?", 1)[0].rstrip("/")
    if method.upper() == "POST" and p == "/containers/create":
        return "create"
    if method.upper() == "POST" and p == "/volumes/create":
        return "volume_create"
    return "other"


def evaluate(method: str, uri: str, body: dict | None, *, policy: Policy) -> Verdict:
    """Решение по одному запросу. body — распарсенное тело (None, если тела нет/не
    JSON). Не create/volume — разрешаем (host-ФС оно не монтирует)."""
    kind = endpoint(method, uri)
    if kind == "other":
        return Verdict(True)

    if body is None:
        return Verdict(False, "тело create не разобрано — отказано на всякий случай")

    roots = policy.allowed_roots

    if kind == "volume_create":
        opts = (body.get("DriverOpts") or {})
        dev = opts.get("device") or opts.get("Device") or ""
        o = (opts.get("o") or opts.get("O") or "")
        if dev and "bind" in str(o) and _is_host_path(dev) and not _allowed(_norm(dev), roots):
            return Verdict(False, _deny_bind(dev, roots))
        return Verdict(True)

    # kind == 'create'
    hc = body.get("HostConfig") or {}

    if hc.get("Privileged"):
        return Verdict(False, (
            "--privileged даёт доступ к устройствам и ядру хоста в обход скоупа — "
            "это выход из песочницы. Для сервисов/тестов он не нужен."
        ))

    for key in _HOST_NS:
        if (hc.get(key) or "") == "host":
            return Verdict(False, (
                f"{key}=host разделяет namespace с хостом (его процессы/IPC) — "
                f"пролом песочницы. Убери host-режим."
            ))

    caps = {str(c).upper() for c in (hc.get("CapAdd") or [])}
    if bad := (caps & policy.dangerous_caps):
        return Verdict(False, (
            f"capabilities {', '.join(sorted(bad))} дают выход на хост. Убери --cap-add."
        ))

    # --device (Devices) СОЗНАТЕЛЬНО пускаем: USB/TTY по делу (оператор просил).

    if reason := _check_binds(hc.get("Binds"), roots):
        return Verdict(False, reason)
    if reason := _check_mounts(hc.get("Mounts"), roots):
        return Verdict(False, reason)

    return Verdict(True)


__all__ = ["Verdict", "Policy", "evaluate", "endpoint"]
