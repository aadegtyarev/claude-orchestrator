"""AgentVmRunner — сессии в microVM через wirenboard/agent-vm (каркас).

Дизайн: docs/agent-vm-integration.md. Статус: сборка argv и preflight готовы
и покрыты тестами; живой сквозной прогон (handshake канала через границу VM,
транскрипты, /stats) требует машины с KVM и отмечен в дизайн-доке как
отдельный ручной эксперимент — до него SANDBOX=agent-vm считать
экспериментальным.

Ключевые свойства agent-vm, на которые опирается сборка argv:
  * stateless CLI: `agent-vm claude [...args]` сам поднимает microVM и
    пробрасывает stdin/stdout — наш PTY-запуск совместим;
  * одна VM на директорию (имя = hash(cwd)) → unique_cwd=True, гвард
    в SessionManager не даст создать вторую сессию на тот же проект;
  * сеть гостя public_only → нужен --allow-host (хуки/канал стучатся на
    хостовый оркестратор) и --publish порта channel-сервера (оркестратор
    стучится внутрь);
  * креды Claude agent-vm сам держит на хосте и подменяет прокси.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
from pathlib import Path
from typing import Sequence, TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from ..config import Config

AGENT_VM_BIN = "agent-vm"

# Чем claude может авторизоваться у СВОЕГО (не agent-vm) эндпоинта.
AUTH_KEYS = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}

# Переменные, которые agent-vm читает САМ (env-алиасы своих флагов, см.
# `agent-vm claude --help`: «[env: …]») И которые читаем мы, чтобы решить,
# эмитить ли соответствующий флаг. Раз решение наше, окружение дочернего
# процесса должно быть от них очищено: иначе наша нормализация («пусто или
# мусор = не задано, флаг не эмитим») врёт — переменная доезжает до agent-vm,
# и его собственный, более строгий парсер падает на том, что мы обещали
# проигнорировать (`AGENT_VM_CPUS=` → «cannot parse integer from empty
# string»). AGENT_VM_IMAGE сюда НЕ входит: agent-vm читает AGENT_VM_IMAGE_TAG,
# это другое имя и другой смысл — коллизии нет.
OWN_ENV_VARS = ("AGENT_VM_MEMORY_GIB", "AGENT_VM_CPUS")


def strip_own_env(env: dict[str, str]) -> None:
    """Убрать из env переменные, которые мы уже превратили во флаги (на месте)."""
    for name in OWN_ENV_VARS:
        env.pop(name, None)


def auth_problem(claude_env: dict[str, str]) -> str | None:
    """Почему свой ANTHROPIC_BASE_URL не заработает под agent-vm (или None).

    Замерено живьём: в госте у claude СВОИХ кред нет — их подставляет прокси
    agent-vm на проводе, и только для СВОЕГО эндпоинта. При своём base_url
    подстановки нет, и claude падает «Execution error» ещё до запроса. С явным
    токеном тот же путь работает (запросы дошли до хостового прокси). Молчать
    нельзя — оператор получил бы нерабочие сессии без объяснения.
    """
    if "ANTHROPIC_BASE_URL" not in claude_env or (AUTH_KEYS & claude_env.keys()):
        return None
    keys = " или ".join(f"CLAUDE_ENV_{k}" for k in sorted(AUTH_KEYS))
    return (
        f"CLAUDE_ENV_ANTHROPIC_BASE_URL задан, но нет токена ({keys}). "
        "Под SANDBOX=agent-vm кред-прокси agent-vm подставляет токен только "
        "для своего эндпоинта — для своего прокси нужен свой токен, иначе "
        "сессии падают без внятной ошибки. Добавь токен или убери свой "
        "base_url (в VM трафик к Anthropic ведёт сам agent-vm)."
    )


def egress_hosts(claude_env: dict[str, str], host_ip: str | None) -> list[str]:
    """Хостовые адреса из CLAUDE_ENV_*, которым нужен `--allow-egress`.

    Config уже переписал loopback на LAN-адрес хоста; здесь ищем именно его,
    потому что политика гостя по умолчанию (`public_only`) запрещает RFC1918 —
    без явного разрешения прокси оператора из VM недостижим.
    """
    if not host_ip:
        return []
    return [host_ip] if any(host_ip in v for v in claude_env.values()) else []


def egress_proxy_allow(egress_proxy: str | None) -> list[str]:
    """Адрес самого egress-прокси, которому нужен `--allow-egress` (или []).

    Если egress-прокси слушает на ПРИВАТНОМ (RFC1918) LAN-адресе — гость по
    умолчанию (public_only) не имеет права к нему обратиться, и egress молча
    упал бы; открываем ровно этот адрес. LOOPBACK (127.0.0.0/8) исключаем: наш
    vault-прокси под --wallet --vm слушает на 127.0.0.1, но гость к нему НЕ
    ходит — egress заворачивает сам agent-vm с хоста и loopback bypass'ит
    (no_proxy), так что `--allow-egress 127.0.0.1` был бы бессмыслен (для гостя
    это его собственный loopback). Публичный прокси допущен политикой без allow
    (прод-путь оркестратора 1:1). Хост не распарсился/не IP — [].
    """
    if not egress_proxy:
        return []
    host = urlsplit(egress_proxy).hostname
    if not host:
        return []
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return []
    return [host] if ip.is_private and not ip.is_loopback else []


class AgentVmRunner:
    name = "agent-vm"
    unique_cwd = True  # имя VM = hash(cwd): вторая сессия убила бы VM первой
    # Отдельный /bash в VM не изолировать (unique_cwd) — отказываем, а не гоним
    # без изоляции (см. run_bash).
    supports_prefix = False

    def __init__(self, config: "Config", root: Path, *, mount_root: bool = True):
        """root — корень установки оркестратора; mount_root — монтировать ли его
        в гостя RW.

        mount_root=True (умолчание, прод-путь оркестратора): в госте нужен код
        оркестратора — хуки сессии запускают оттуда channel_server.py.

        mount_root=False — для запусков, которым код оркестратора в госте не
        нужен (standalone `claude-box --vm`: ни сессии, ни канала, ни хуков).
        Флаг обязателен, потому что у `--mount` в agent-vm НЕТ режима
        только-чтение: смонтировать корень значит отдать гостю запись в код и
        в `.env` оператора (боевые токены бота). Для чужого проекта под
        `claude-box --vm` это чистая утечка, поэтому там монтирования нет.
        """
        self.config = config
        self.root = root
        self.mount_root = mount_root

    def preflight(self) -> tuple[bool, str]:
        if shutil.which(AGENT_VM_BIN) is None:
            return False, (
                "agent-vm не установлен (см. github.com/wirenboard/agent-vm)"
            )
        if not Path("/dev/kvm").exists():
            return False, "нет /dev/kvm — agent-vm требует KVM"
        problem = auth_problem(self.config.claude_env)
        if problem:
            return False, problem
        if self.config.agent_vm_egress_proxy and not self._supports_egress_flags():
            return False, (
                "AGENT_VM_EGRESS_PROXY задан, но установленный agent-vm не знает "
                "--egress-proxy: это флаги форка (docs/FORK-agent-vm-egress-proxy.md). "
                "Апстримный бинарь упал бы на «unexpected argument», а без флага "
                "egress гостя молча MITM-ит собственный прокси agent-vm и кошелёк "
                "обойдён — поэтому отказываем сразу. Поставь форк или убери "
                "AGENT_VM_EGRESS_PROXY."
            )
        ca = self.config.agent_vm_egress_ca
        if ca and not ca.is_file():
            return False, f"AGENT_VM_EGRESS_CA={ca} — файла нет (нужен PEM CA прокси)."
        # Inject-секрет кошелька под --vm доставляется флагом --env-file (значение
        # из файла, не в argv). Он есть только в форке v0.1.28+ — на старом бинаре
        # честный отказ, а не падение каждой сессии на «unexpected argument».
        if getattr(self.config, "agent_vm_env_files", ()) and \
                not self._help_contains(b"--env-file"):
            return False, (
                "inject-секрет под --vm требует флаг --env-file, но установленный "
                "agent-vm его не знает (нужен форк v0.1.28+, "
                "docs/FORK-agent-vm-egress-proxy.md). Обнови agent-vm, либо используй "
                "прокси-секрет под --vm, либо inject-секрет под --engine bwrap."
            )
        return True, "ok"

    def _supports_egress_flags(self) -> bool:
        """Знает ли установленный agent-vm про --egress-proxy (форк или апстрим).

        Спрашиваем сам бинарь (`--help`), а не версию: флаг может приехать в
        апстрим, и тогда форк перестанет быть нужен — проверка переживёт это
        без правок. Сбой запуска считаем «не знает»: лучше честный отказ на
        старте, чем падение каждой сессии.
        """
        return self._help_contains(b"--egress-proxy")

    def _help_contains(self, flag: bytes) -> bool:
        """Есть ли `flag` в выводе `agent-vm claude --help` (форк vs апстрим).

        Сбой запуска бинаря считаем «нет флага»: лучше честный отказ на старте,
        чем падение каждой сессии на «unexpected argument»."""
        try:
            out = subprocess.run(
                [AGENT_VM_BIN, "claude", "--help"],
                capture_output=True, timeout=20,
            )
        except Exception:  # noqa: BLE001 — любой сбой = считаем, что флага нет
            return False
        return flag in out.stdout + out.stderr

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
        """agent-vm <cmd> [--опции] -- <аргументы cmd>.

        cwd монтируется самим agent-vm; докидываем рабочие пути сессии и —
        только при mount_root — корень оркестратора (channel_server внутри
        гостя; в standalone CLI он не нужен и не монтируется). home_dir
        не пробрасывается: у гостя свой $HOME (Debian-образ), персистентность
        дома решается state-каталогом agent-vm, не нами.
        """
        if not argv:
            # Префикс-режим (sandbox_prefix для /bash): в VM интерактивный
            # bash отдельно от claude не заворачиваем — поднимать вторую VM
            # на тот же cwd нельзя (unique_cwd). /bash идёт без изоляции VM.
            return []
        cmd, *rest = argv
        out = [AGENT_VM_BIN, Path(cmd).name]
        # Хуки и channel_server внутри гостя должны достучаться до
        # оркестратора на хосте; оркестратор — до /notify канала в госте.
        out += ["--allow-host"]
        # Прокси оператора (CLAUDE_ENV_ANTHROPIC_BASE_URL и т.п.) живёт на
        # ХОСТЕ. Гостю его LAN-адрес по умолчанию запрещён политикой
        # public_only — открываем ровно этот адрес, не всю LAN (--allow-lan
        # дал бы гостю всю подсеть). Config уже переписал loopback на него.
        # Плюс адрес самого egress-прокси, если он приватный (кошелёк под --vm:
        # наш vault-прокси на LAN-адресе хоста — без allow гость до него не
        # достучится). dict.fromkeys — стабильный порядок и дедуп (LAN-адрес
        # может прийти из обоих источников сразу).
        allow = dict.fromkeys(
            egress_hosts(self.config.claude_env, self.config.agent_vm_host_ip)
            + egress_proxy_allow(self.config.agent_vm_egress_proxy)
        )
        for host_ip in allow:
            out += ["--allow-egress", host_ip]
        # Egress гостя на наш прокси (кошелёк). Без этого весь HTTPS гостя
        # прозрачно MITM-ит собственный прокси agent-vm своим CA, и наш
        # перехват обойдён (замер F10 в ARCHITECTURE-claude-box.md). Флаги
        # понимает форк (docs/FORK-agent-vm-egress-proxy.md); preflight
        # проверяет их наличие, чтобы апстримный бинарь не падал на
        # «unexpected argument» без объяснения.
        if self.config.agent_vm_egress_proxy:
            out += ["--egress-proxy", self.config.agent_vm_egress_proxy]
            if self.config.agent_vm_egress_ca:
                # CA доверяется на UPSTREAM-плече перехвата, в гостя не едет.
                out += ["--egress-ca", str(self.config.agent_vm_egress_ca)]
        # Инъекция env в гостя из ФАЙЛА (VM-путь inject-секрета кошелька). Флаг
        # форка `--env-file NAME=PATH` кладёт переменную в окружение агента в
        # госте, читая значение из файла — в argv (ps на хосте) значение НЕ
        # попадает, только имя и путь. Поле только у EngineConfig (кошелёк
        # claude-box) — orchestrator.Config его не имеет, поэтому getattr с
        # дефолтом ().
        for pair in getattr(self.config, "agent_vm_env_files", ()):
            out += ["--env-file", pair]
        for port in publish_ports:
            out += ["--publish", f"{port}:{port}"]
        # cwd монтирует сам agent-vm — второй --mount на тот же гостевой путь он
        # отвергает («multiple volumes cannot mount the same guest path») ещё до
        # загрузки образа. Отсеиваем chdir из ВСЕГО набора, включая root: у
        # оркестратора репозиторий и рабочий каталог сессии разные, а у
        # standalone `claude-box` из корня самого репозитория они совпадают —
        # и запуск падал (поймано живым прогоном --vm).
        # root монтируем ТОЛЬКО при mount_root (см. __init__): у --mount нет
        # режима RO, и в standalone CLI корень репозитория — это .env оператора.
        roots = {str(self.root)} if self.mount_root else set()
        mounts = {
            m for m in {*roots, *(str(p) for p in extra_rw)}
            if m != str(chdir)
        }
        for m in sorted(mounts):
            out += ["--mount", f"{m}:{m}"]
        if self.config.agent_vm_memory_gib:
            # `--memory <GIB>` — ГОЛОЕ число (agent-vm 0.1.25: «Sandbox memory,
            # in GiB»). Суффикс «G» тот же бинарь отвергает: «invalid value
            # '8G' for '--memory <GIB>': invalid digit found in string».
            out += ["--memory", f"{self.config.agent_vm_memory_gib:g}"]
        if self.config.agent_vm_cpus:
            out += ["--cpus", str(self.config.agent_vm_cpus)]
        if self.config.agent_vm_image:
            out += ["--image", self.config.agent_vm_image]
        return out + ["--", *rest]
