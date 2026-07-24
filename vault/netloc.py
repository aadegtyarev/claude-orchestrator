"""Сетевые хелперы, общие для оркестратора и Launcher (claude-box).

Здесь живёт `host_lan_ip` — LAN-адрес хоста, по которому его видит гость microVM.
Он нужен ДВУМ независимым слоям: оркестратору (config.py переписывает
CLAUDE_ENV_* с loopback на этот адрес) и Launcher'у (box_cli/wallet.py биндит
vault-прокси кошелька на этот адрес, чтобы гость agent-vm достучался до него
через `--egress-proxy`).

Почему модуль в `vault/`, а не в `orchestrator/`: box_cli.wallet обязан остаться
автономным от оркестратора (он уже тянет только vault.*), а вынести хелпер в
общее место всё равно надо — иначе Launcher импортировал бы orchestrator.config
целиком (десятки полей UX-оркестратора, TELEGRAM_BOT_TOKEN и пр.). vault —
общий нижний слой, и это чистый stdlib-хелпер без секретной логики, так что
автономность самого vault (только stdlib) не страдает. orchestrator.config
реэкспортирует имя для обратной совместимости — поведение 1:1.
"""

from __future__ import annotations

import os
import re
import subprocess


def host_lan_ip() -> str | None:
    """LAN-адрес хоста, по которому его видит гость microVM.

    Зачем не `host.microsandbox.internal`: замерено живьём — agent-vm гонит
    egress гостя через свой HTTP-CONNECT прокси, и хостовое gateway-имя он не
    маршрутизирует (запрос к сервису на хосте не доходит). А вот LAN-адрес
    хоста прокси обходит (он сам кладёт его в `no_proxy` гостя), и с
    `--allow-egress <этот адрес>` сервис на хосте из гостя ДОСТУПЕН —
    проверено: гость получил ответ от хостового сервиса.

    Берём `src` ДЕФОЛТНОГО маршрута с наименьшей метрикой — ровно тот адрес,
    что выбирает сам agent-vm (сверено: он положил его в `no_proxy` гостя).
    Трюк «UDP-сокет на 8.8.8.8» здесь НЕ годится: при поднятом VPN он вернёт
    адрес туннеля (более специфичный маршрут), а гость ходит не туда.

    Переопределяется явно — `AGENT_VM_HOST_IP` (если авто-выбор промахнулся).
    """
    override = os.getenv("AGENT_VM_HOST_IP", "").strip()
    if override:
        return override

    try:
        out = subprocess.run(
            ["ip", "-o", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    best: tuple[int, str] | None = None
    for line in out.splitlines():
        m = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", line)
        if not m:
            continue
        metric = int(mm.group(1)) if (mm := re.search(r"\bmetric\s+(\d+)", line)) else 0
        if best is None or metric < best[0]:
            best = (metric, m.group(1))
    return best[1] if best else None


__all__ = ["host_lan_ip"]
