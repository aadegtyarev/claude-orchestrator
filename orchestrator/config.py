"""Конфигурация: всё читается из .env / переменных окружения."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from urllib.parse import urlsplit, urlunsplit

# host_lan_ip вынесен в общий нижний слой (vault/netloc): его делят оркестратор
# и Launcher (box_cli/wallet.py биндит vault-прокси кошелька на этот адрес под
# --vm). Реэкспорт сохраняет обратную совместимость — `from orchestrator.config
# import host_lan_ip` и внутренние вызовы работают как раньше, поведение 1:1.
from vault.netloc import host_lan_ip

logger = logging.getLogger(__name__)

# Что сказать оператору, когда модуль пропущен из-за несовпадения песочницы:
# не просто «выключен», а чем это грозит и что делать.
_MODULE_SKIP_HINT = {
    "wallet": (
        "Секреты кошелька (ssh/scp, токены для curl и любых CLI, inject-секреты) "
        "в этой сессии НЕДОСТУПНЫ. Под agent-vm креды git/gh и Claude держит его "
        "собственный прокси (значения модели не выдаются) — этого хватает для "
        "git/gh; для остальных секретов запускай с SANDBOX=bwrap."
    ),
}


def _auto_orch_token() -> str:
    """Разовый токен внутреннего HTTP-API, если ORCH_TOKEN не задан явно.

    Локальный (127.0.0.1) токен — защита от любого локального процесса и
    DNS-rebinding из браузера (см. REVIEW.md S1). Перезапуск launcher'а убивает
    все процессы claude (manager.shutdown), значит и channel_server'ы, поэтому
    токен, сгенерированный на пуск, согласован со всеми сессиями этого пуска
    (resume переписывает .mcp.json/settings с актуальным токеном). Для
    предсказуемости лучше зафиксировать ORCH_TOKEN в .env.
    """
    tok = secrets.token_urlsafe(24)
    logger.warning(
        "ORCH_TOKEN не задан — сгенерирован разовый токен внутреннего API. "
        "Для стабильности между перезапусками зафиксируй ORCH_TOKEN в .env.",
    )
    return tok


# Имя, по которому ГОСТЬ microVM (SANDBOX=agent-vm) видит хост: microsandbox
# мапит его на хостовый 127.0.0.1 (аналог host.docker.internal), при условии
# --allow-host (его даёт AgentVmRunner). На самом ХОСТЕ это имя НЕ резолвится —
# поэтому оно только guest-facing (см. Config.guest_orch_host), а reply-сервер
# биндится на orch_host (host-side loopback). Подтверждено живым экспериментом,
# docs/agent-vm-integration.md.
AGENT_VM_GUEST_HOST = "host.microsandbox.internal"

# Хостовый loopback в значении CLAUDE_ENV_* (напр. ANTHROPIC_BASE_URL прокси).
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "0.0.0.0")
# То же, но как их отдаёт urlsplit().hostname (без скобок у IPv6, в нижнем).
LOOPBACK_HOSTS_PLAIN = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def env_number(name: str, cast):
    """Число из переменной окружения; пусто/не задано → None.

    Мусорное значение — SystemExit с внятным сообщением, а не сырой ValueError
    из недр загрузки конфига: оператор должен увидеть, КАКАЯ переменная плохая
    и что от неё ждут. То же поведение даёт standalone `claude-box`
    (box_cli/cli.py agent_vm_env_config) — расходиться на одних и тех же
    именах переменных нельзя.

    AGENT_VM_MEMORY_GIB/AGENT_VM_CPUS читаем как ЦЕЛЫЕ: сам agent-vm дробные
    отвергает («invalid value '2.5' for '--memory <GIB>'»), а раньше «8.5» в
    .env проходил конфиг, чтобы уронить КАЖДУЮ сессию уже внутри agent-vm.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return cast(raw)
    except ValueError:
        kind = "целое число" if cast is int else "число"
        raise SystemExit(
            f"{name}={raw!r} — ожидалось {kind}. Исправь значение в .env "
            f"или убери переменную (тогда возьмётся умолчание)."
        ) from None


@dataclass(frozen=True)
class Config:
    # Активные транспорт-адаптеры (ADAPTERS=telegram,web) и модули (MODULES=…).
    adapters: tuple[str, ...]
    modules: tuple[str, ...]
    telegram_bot_token: str
    telegram_chat_id: int | None
    web_host: str
    web_port: int
    web_token: str
    channel_port_start: int
    channel_port_end: int
    sessions_dir: Path
    max_instances: int
    claude_bin: str
    orch_host: str
    orch_port: int
    orch_token: str
    # systemd-юнит для команды /restart (self-restart). Пусто → авто-детект из
    # /proc/self/cgroup, фолбэк claude-orchestrator.service.
    orch_systemd_unit: str
    allowed_user_ids: frozenset[int]
    show_tool_calls: bool
    delete_bubble: bool
    show_command_menu: bool
    claude_config_dir: Path | None
    incoming_dir: str
    permission_mode: str
    bot_lang: str
    idle_timeout_h: float
    log_max_mb: float
    default_model: str | None  # --model по умолчанию (None = решение Claude/профиля/проекта)
    default_effort: str | None  # --effort по умолчанию (low/medium/high/xhigh/max)
    claude_env: dict[str, str]  # доп. env для процесса claude (CLAUDE_ENV_*)
    sandbox: str  # "bwrap" (файловая песочница) | "agent-vm" (microVM) | "off"
    sandbox_extra_rw: tuple[Path, ...]  # доп. пути, доступные из песочницы на запись
    sandbox_dbus: bool  # прокидывать ли system D-Bus для mDNS/avahi-browse (bwrap)
    # Прокидывать ли X/Wayland в песочницу (bwrap). Сеть у песочницы общая с
    # хостом, поэтому абстрактный сокет X достижим даже при tmpfs /tmp — с
    # $DISPLAY процесс мог бы дёрнуть хостовый GUI (askpass-диалоги, скриншоты,
    # ввод). Off (дефолт) вырезает DISPLAY/XAUTHORITY/WAYLAND_DISPLAY; on —
    # оставить (если модели действительно нужен X в песочнице).
    sandbox_x11: bool
    # Давать ли модели docker/compose в песочнице (bwrap). Off (дефолт). On: у
    # КАЖДОЙ сессии свой тонкий прокси-фильтр над docker.sock (per-session
    # изоляция) — внутрь биндится прокси-сокет на /run/docker.sock, настоящий
    # скрыт. Прокси пускает bind только под папкой проекта ЭТОЙ сессии + устройства,
    # режет систему/секреты/escape (см. modules/docker и docs/CONFIG.md).
    # По аналогии с sandbox_bwrap_wallet — фича именно bwrap-песочницы.
    sandbox_docker: bool
    # Раннер agent-vm (SANDBOX=agent-vm): ресурсы и пин образа гостя.
    agent_vm_memory_gib: int | None  # целые GiB: agent-vm дробные отвергает
    agent_vm_cpus: int | None
    agent_vm_image: str | None
    # LAN-адрес хоста для гостя (см. host_lan_ip). Фиксируется ОДИН раз на
    # старте: раннер должен открывать egress ровно к тому адресу, что уже
    # записан в claude_env, иначе при смене маршрута (VPN/DHCP) URL и
    # --allow-egress разъедутся и гость молча не достучится.
    agent_vm_host_ip: str | None
    # Egress гостя на ВНЕШНИЙ прокси (форк agent-vm, docs/FORK-agent-vm-egress-proxy.md).
    # Без них agent-vm ведёт себя как апстрим: весь egress гостя MITM-ит его
    # собственный прокси, и наш кошелёк обойдён (F10). С ними egress
    # переоткрывается на указанный прокси, а CA доверяется на UPSTREAM-плече
    # (гость его не видит — он по-прежнему доверяет CA перехвата agent-vm).
    agent_vm_egress_proxy: str | None
    agent_vm_egress_ca: Path | None
    # Кошелёк секретов (MODULES=wallet): файл секретов и политик (0600, вне
    # allowlist песочницы).
    wallet_secrets_file: Path
    # Guard кошелька: всегда-запрет опасных вызовов (печать токена, git-RCE через
    # флаги) поверх policy, с прозрачным объяснением модели. WALLET_GUARD=0 —
    # отключить (модель сможет что угодно в рамках commands; менее безопасно).
    wallet_guard: bool
    # Судья auto-режима проверяет ВСЕ bash-команды (classifyAllShell), а не только
    # «на вид рискованные» — чтобы хитрый secret-exfil не проскочил. Побочка:
    # больше подтверждений на легит-но-рискованное (force-push и т.п.).
    # AUTOMODE_CLASSIFY_ALL_SHELL=0 — выключить (меньше вопросов, слабее защита).
    automode_classify_all_shell: bool
    # Разрешить правку policy кошелька из чата (команда /wallet). По умолчанию
    # включено. WALLET_POLICY_EDIT=0 — только просмотр, правки лишь host-файлом.
    wallet_policy_edit: bool

    def guest_orch_host_for(self, engine: str) -> str:
        """То же, что `guest_orch_host`, но для КОНКРЕТНОГО движка сессии.

        Движок теперь выбирается на сессию (`/new … --box`), поэтому адрес
        оркестратора «глазами сессии» зависит от неё, а не только от SANDBOX."""
        return AGENT_VM_GUEST_HOST if engine == "agent-vm" else self.orch_host

    @property
    def guest_orch_host(self) -> str:
        """Адрес оркестратора С ТОЧКИ ЗРЕНИЯ СЕССИИ (гостя) — для `.mcp.json`
        (channel_server) и хук-диспетчера. Выводится из `sandbox`, поэтому
        переключение движка = ОДНО действие (SANDBOX в .env), без ручной правки
        адресов. Под agent-vm гость VM не видит хостовый loopback → имя
        host-gateway (`AGENT_VM_GUEST_HOST`); под bwrap/off — общий loopback
        `orch_host`. reply-сервер биндится на `orch_host` (host-side) — оно
        host-resolvable, а guest-facing имя резолвится только внутри гостя.

        Для сессии с собственным движком берут `guest_orch_host_for(engine)`."""
        return self.guest_orch_host_for(self.sandbox)

    # ── выбор движка изоляции на сессию (флаг --box у /new) ─────────────

    # Синонимы значений флага. Держим отдельно от _parse_sandbox: тот разбирает
    # .env и падает SystemExit'ом, а это ввод пользователя в чате.
    BOX_ALIASES = {
        "off": "off", "none": "off", "no": "off", "0": "off", "false": "off",
        "bwrap": "bwrap", "box": "bwrap",
        "vm": "agent-vm", "agent-vm": "agent-vm", "agentvm": "agent-vm",
        "microvm": "agent-vm",
    }

    @classmethod
    def parse_box(cls, raw: str) -> str | None:
        """Значение флага `--box` → имя движка (None = не распознано)."""
        return cls.BOX_ALIASES.get(raw.strip().lower())

    def box_choices(self) -> tuple[str, ...]:
        """Движки, которые эта сборка оркестратора умеет дать сессии.

        `bwrap`/`off` доступны всегда: им не нужно ничего, что делается ОДИН раз
        на старте. `agent-vm` — только если он и есть SANDBOX: под него конфиг
        перестраивается на старте (LAN-адрес хоста в CLAUDE_ENV_*, guest-имя
        оркестратора, отключённые /stats и кошелёк), и включить его одной сессии
        задним числом нельзя — она поднялась бы с адресами хоста, недостижимыми
        из гостя. Порядок: первым — дефолт (SANDBOX)."""
        rest = [e for e in ("bwrap", "off") if e != self.sandbox]
        return (self.sandbox, *rest)

    def box_reject_reason(self, engine: str) -> str | None:
        """Почему этот движок нельзя выбрать сессии (None = можно)."""
        if engine in self.box_choices():
            return None
        if engine == "agent-vm":
            return (
                "движок agent-vm настраивается на старте оркестратора "
                "(LAN-адрес хоста для гостя, guest-адреса, транскрипт внутри VM) "
                "— включить его одной сессии нельзя: задай SANDBOX=agent-vm."
            )
        return f"движок {engine} недоступен"

    def docker_sock_path(self, session_name: str) -> Path:
        """Путь unix-сокета per-session docker-прокси. В runtime-каталоге (tmpfs),
        падаем в sessions_dir. Знают и старт прокси (SessionManager), и бинд в
        песочницу (bwrap через docker_sock-параметр wrap)."""
        base = os.getenv("XDG_RUNTIME_DIR")
        root = Path(base) if base else self.sessions_dir
        return root / "claude-orchestrator" / f"docker-{session_name}.sock"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()

        adapters = cls._parse_adapters(os.getenv("ADAPTERS", "telegram"))
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if "telegram" in adapters and not token:
            raise SystemExit(
                "TELEGRAM_BOT_TOKEN не задан — заполни .env (см. .env.example) "
                "или убери telegram из ADAPTERS."
            )

        chat_id_raw = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

        sandbox = cls._parse_sandbox(os.getenv("SANDBOX", "bwrap"))
        # CLAUDE_ENV_ANTHROPIC_BASE_URL=... → в процесс claude уйдёт
        # ANTHROPIC_BASE_URL=... (префикс снимается).
        claude_env = {
            k[len("CLAUDE_ENV_"):]: v
            for k, v in os.environ.items()
            if k.startswith("CLAUDE_ENV_") and k != "CLAUDE_ENV_"
        }
        modules = cls._default_modules(
            os.getenv("MODULES"), sandbox, os.getenv("SANDBOX_BWRAP_WALLET")
        )
        # CLAUDE_ENV_* с адресом на хостовом loopback (типовой случай —
        # ANTHROPIC_BASE_URL локального прокси) под agent-vm переписываем на
        # LAN-адрес хоста: изнутри VM «127.0.0.1» указывал бы на сам гость.
        # Раннер к такому адресу добавляет `--allow-egress` (иначе RFC1918
        # запрещён политикой public_only). См. host_lan_ip.
        # Адрес хоста «глазами гостя». Обычно определяется сам (src маршрута по
        # умолчанию), но авто-выбор промахивается при VPN/нескольких интерфейсах,
        # и тогда оператору нужен ручной путь — иначе сессия в VM молча не
        # достучится до прокси и оркестратора.
        agent_vm_host_ip = None
        if sandbox == "agent-vm":
            agent_vm_host_ip = os.getenv("AGENT_VM_HOST_IP", "").strip() or host_lan_ip()
        if sandbox == "agent-vm":
            claude_env, unreachable = cls._rewrite_env_for_guest(
                claude_env, agent_vm_host_ip
            )
            if unreachable:
                logger.warning(
                    "%s: адрес на loopback хоста, а LAN-адрес хоста определить "
                    "не удалось — из microVM сервис недостижим, переменная "
                    "останется как есть и, скорее всего, сломает сессию. "
                    "Используй SANDBOX=bwrap или укажи внешний адрес.",
                    ", ".join(sorted(unreachable)),
                )

        # CLAUDE_CONFIG_DIR под agent-vm не применяется и применён быть НЕ может:
        # это хостовый путь с кредами, а claude живёт в госте. Смонтировать его
        # внутрь значит занести хостовые креды в VM и подраться с кред-прокси
        # agent-vm (он сам держит ~/.claude/.credentials.json на хосте и отдаёт
        # в гостя плейсхолдеры). Молчать нельзя — оператор считал бы, что его
        # профиль в деле. CLAUDE_ENV_* при этом доезжают (через env-блок
        # settings.local.json, см. sessions._write_claude_settings).
        if sandbox == "agent-vm" and os.getenv("CLAUDE_CONFIG_DIR", "").strip():
            logger.warning(
                "CLAUDE_CONFIG_DIR ИГНОРИРУЕТСЯ при SANDBOX=agent-vm: claude "
                "работает внутри microVM со своим ~/.claude, а профиль хоста "
                "туда не пробросить (это внесло бы хостовые креды в гостя). "
                "Профиль/креды Claude в VM держит сам agent-vm. Нужен свой "
                "профиль — используй SANDBOX=bwrap."
            )

        return cls(
            adapters=adapters,
            modules=modules,
            telegram_bot_token=token,
            telegram_chat_id=cls._parse_chat_id(chat_id_raw),
            web_host=os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1",
            web_port=int(os.getenv("WEB_PORT", "8180")),
            # Токен веб-интерфейса. Пустой = сгенерировать на запуск (URL с
            # токеном печатается в лог, как у Jupyter).
            web_token=os.getenv("WEB_TOKEN", "").strip(),
            # 0/не задано = авто: ОС выдаёт свободный localhost-порт на сессию.
            channel_port_start=int(os.getenv("CHANNEL_PORT_START", "0")),
            channel_port_end=int(os.getenv("CHANNEL_PORT_END", "0")),
            sessions_dir=Path(os.getenv("SESSIONS_DIR", "~/tg-claude-sessions")).expanduser(),
            max_instances=int(os.getenv("MAX_INSTANCES", "5")),
            claude_bin=os.getenv("CLAUDE_BIN", "claude"),
            orch_host=os.getenv("ORCH_HOST", "127.0.0.1"),
            orch_port=int(os.getenv("ORCH_PORT", "18080")),
            # Токен внутреннего HTTP-API (см. _auto_orch_token / REVIEW.md S1).
            orch_token=os.getenv("ORCH_TOKEN", "").strip() or _auto_orch_token(),
            orch_systemd_unit=os.getenv("ORCH_SYSTEMD_UNIT", "").strip(),
            allowed_user_ids=cls._parse_user_ids(os.getenv("ALLOWED_USER_IDS", "")),
            show_tool_calls=cls._parse_bool(os.getenv("SHOW_TOOL_CALLS", "true")),
            delete_bubble=cls._parse_bool(os.getenv("DELETE_BUBBLE", "true")),
            # Меню команд (кнопка «/»). В группе Telegram всё равно показывает
            # «/команда@бот» (клиентский роутинг) — false скрывает меню целиком.
            show_command_menu=cls._parse_bool(os.getenv("SHOW_COMMAND_MENU", "true")),
            claude_config_dir=(
                Path(raw).expanduser() if (raw := os.getenv("CLAUDE_CONFIG_DIR", "").strip()) else None
            ),
            incoming_dir=os.getenv("INCOMING_DIR", "incoming").strip() or "incoming",
            permission_mode=cls._parse_permission_mode(os.getenv("PERMISSION_MODE", "auto")),
            bot_lang=(os.getenv("BOT_LANG", "ru").strip().lower() or "ru"),
            idle_timeout_h=float(os.getenv("IDLE_TIMEOUT_H", "6")),
            log_max_mb=float(os.getenv("LOG_MAX_MB", "10")),
            # Модель/effort по умолчанию для всех сессий. Не заданы — Claude
            # берёт свои дефолты (или то, что в профиле/проекте). /model на
            # сессию перекрывает DEFAULT_MODEL.
            default_model=(raw.strip() or None) if (raw := os.getenv("DEFAULT_MODEL", "")).strip() else None,
            default_effort=(
                (raw.strip() or None) if (raw := os.getenv("DEFAULT_EFFORT", "")).strip() else None
            ),
            claude_env=claude_env,
            # Файловая песочница (bubblewrap). По умолчанию включена: процесс
            # claude и /bash видят только папку сессии/проекта и конфиг Claude
            # Code, всё остальное в $HOME и системе — недоступно. SANDBOX=off
            # отключает (нужно на машинах без bwrap/без unprivileged userns).
            sandbox=sandbox,
            sandbox_extra_rw=cls._parse_paths(os.getenv("SANDBOX_EXTRA_RW", "")),
            # mDNS/локальная сеть через system D-Bus. По умолчанию включено
            # (полезно: агент видит .local-хосты и сервисы), off — запретить
            # доступ к system D-Bus из песочницы.
            sandbox_dbus=cls._parse_bool(os.getenv("SANDBOX_DBUS", "true")),
            # X/Wayland в песочницу по умолчанию НЕ прокидываем (закрываем доступ
            # к хостовому GUI); SANDBOX_X11=1 — оставить, если модели нужен X.
            sandbox_x11=cls._parse_bool(os.getenv("SANDBOX_X11", "false")),
            sandbox_docker=cls._parse_bool(os.getenv("SANDBOX_BWRAP_DOCKER", "false")),
            agent_vm_memory_gib=env_number("AGENT_VM_MEMORY_GIB", int),
            agent_vm_cpus=env_number("AGENT_VM_CPUS", int),
            agent_vm_image=(os.getenv("AGENT_VM_IMAGE", "").strip() or None),
            agent_vm_host_ip=agent_vm_host_ip,
            agent_vm_egress_proxy=(
                os.getenv("AGENT_VM_EGRESS_PROXY", "").strip() or None
            ),
            agent_vm_egress_ca=(
                Path(raw).expanduser()
                if (raw := os.getenv("AGENT_VM_EGRESS_CA", "").strip())
                else None
            ),
            wallet_secrets_file=Path(
                os.getenv(
                    "WALLET_SECRETS_FILE",
                    "~/.config/claude-orchestrator/secrets.toml",
                )
            ).expanduser(),
            wallet_guard=cls._parse_bool_default_on(os.getenv("WALLET_GUARD", "1")),
            automode_classify_all_shell=cls._parse_bool_default_on(
                os.getenv("AUTOMODE_CLASSIFY_ALL_SHELL", "1")
            ),
            wallet_policy_edit=cls._parse_bool_default_on(os.getenv("WALLET_POLICY_EDIT", "1")),
        )

    @staticmethod
    def _parse_adapters(raw: str) -> tuple[str, ...]:
        valid = {"telegram", "web"}
        names = [p.strip().lower() for p in raw.split(",") if p.strip()]
        bad = [n for n in names if n not in valid]
        if bad:
            raise SystemExit(
                f"ADAPTERS: неизвестные адаптеры {', '.join(bad)} — "
                f"допустимо: {', '.join(sorted(valid))}"
            )
        if not names:
            raise SystemExit("ADAPTERS пуст — нужен хотя бы один адаптер (telegram, web)")
        return tuple(dict.fromkeys(names))  # без дублей, порядок сохранён

    # Модуль → песочница, без которой он не работает (None = работает при любой).
    # Кошелёк подключается к сессии через окружение процесса claude (шимы первыми
    # в PATH + env-маркеры секретов), а это возможно, только когда claude —
    # процесс на ХОСТЕ, т.е. под bwrap. Под agent-vm claude живёт в госте: env
    # туда не течёт (замерено) и домашний каталог сессии не монтируется, так что
    # включённый кошелёк был бы тихим no-op — демон поднят, а в сессии его нет.
    # Под sandbox=off кошелёк бессмыслен иначе: модель и так видит хостовые креды
    # напрямую (см. комментарий про «театр» в core/sessions.py).
    MODULE_REQUIRES_SANDBOX = {"wallet": "bwrap"}

    @staticmethod
    def _parse_modules(raw: str) -> tuple[str, ...]:
        valid = {"wallet"}
        names = [p.strip().lower() for p in raw.split(",") if p.strip()]
        bad = [n for n in names if n not in valid]
        if bad:
            raise SystemExit(
                f"MODULES: неизвестные модули {', '.join(bad)} — "
                f"допустимо: {', '.join(sorted(valid))}"
            )
        return tuple(dict.fromkeys(names))

    @classmethod
    def _rewrite_env_for_guest(
        cls, claude_env: dict[str, str], ip: str | None = None
    ) -> tuple[dict[str, str], list[str]]:
        """Переписать loopback-адреса в CLAUDE_ENV_* на LAN-адрес хоста.

        Возвращает (новый словарь, имена переменных, которые переписать не
        удалось). Внутри microVM «127.0.0.1» — это сам гость, поэтому прокси
        оператора надо адресовать LAN-адресом хоста (см. host_lan_ip).
        """
        if ip is None:
            ip = host_lan_ip()
        out, unreachable = {}, []
        for name, value in claude_env.items():
            # Разбираем URL, а не ищем подстроку: «http://127.0.0.1/v1» (без
            # порта) прежде проскакивал как «внешний», а внутри VM это сам
            # гость; и наоборот, loopback внутри query-параметра портился.
            try:
                parts = urlsplit(value)
            except ValueError:
                parts = None
            host = (parts.hostname if parts and parts.scheme else None)
            if host is None or host not in LOOPBACK_HOSTS_PLAIN:
                out[name] = value
                continue
            if ip is None:
                out[name] = value
                unreachable.append(name)
                continue
            netloc = parts.netloc.replace(parts.hostname, ip, 1)
            out[name] = urlunsplit(parts._replace(netloc=netloc))
        return out, unreachable

    @classmethod
    def _default_modules(
        cls, raw: str | None, sandbox: str, wallet_raw: str | None = None
    ) -> tuple[str, ...]:
        """Набор модулей.

        Кошелёк включается СВОЕЙ настройкой `SANDBOX_BWRAP_WALLET` (bool) —
        она же неявно объявляет модуль, писать `MODULES=wallet` не нужно. Имя
        отражает суть: кошелёк работает только в песочнице bwrap (его провода —
        окружение процесса claude на хосте), поэтому вне bwrap настройка
        игнорируется, а не спорит с реальностью.

        `MODULES` остаётся реестром модулей вообще (для будущих). Legacy
        `MODULES=wallet` продолжает работать; при конфликте с явной
        `SANDBOX_BWRAP_WALLET` побеждает последняя — с предупреждением, чтобы
        расхождение не пришлось искать глазами.

        Модуль, которому нужна другая песочница, не включаем даже по явной
        просьбе (MODULE_REQUIRES_SANDBOX): он всё равно не заработал бы, а
        «включён и молча ничего не делает» хуже, чем «не включён» — оператор
        считает, что секреты защищены. Отказ — громкий."""
        if raw is None:
            names = ("wallet",) if sandbox == "bwrap" else ()
        else:
            names = cls._parse_modules(raw)
        if wallet_raw is not None:
            want = cls._parse_bool(wallet_raw)
            if want and "wallet" not in names:
                names = (*names, "wallet")
            elif not want and "wallet" in names:
                if raw is not None and "wallet" in cls._parse_modules(raw):
                    logger.warning(
                        "MODULES содержит 'wallet', но SANDBOX_BWRAP_WALLET=%s — "
                        "кошелёк ВЫКЛЮЧЕН (явная настройка сильнее). Убери "
                        "'wallet' из MODULES, чтобы не путать.", wallet_raw,
                    )
                names = tuple(n for n in names if n != "wallet")
        out = []
        for name in names:
            need = cls.MODULE_REQUIRES_SANDBOX.get(name)
            if need is not None and sandbox != need:
                logger.warning(
                    "Модуль '%s' НЕ включён: он требует SANDBOX=%s, а сейчас "
                    "SANDBOX=%s. %s",
                    name, need, sandbox,
                    _MODULE_SKIP_HINT.get(name, ""),
                )
                continue
            out.append(name)
        return tuple(out)

    @staticmethod
    def _parse_sandbox(raw: str) -> str:
        mode = raw.strip().lower() or "bwrap"
        # Синонимы «выключено».
        if mode in ("off", "none", "0", "false", "no"):
            return "off"
        if mode == "vm":  # тот же синоним, что у флага --box (BOX_ALIASES)
            return "agent-vm"
        if mode in ("bwrap", "agent-vm"):
            return mode
        raise SystemExit(f"SANDBOX={raw!r} — допустимо: bwrap | agent-vm | off")

    @staticmethod
    def _parse_paths(raw: str) -> tuple[Path, ...]:
        """Список путей из PATH-подобной строки (разделитель ':').

        Широкий RW-путь (дом/корень) молча разворачивает песочницу — ругаемся
        в лог, чтобы оператор не открыл наружу больше, чем хотел."""
        out: list[Path] = []
        home = Path.home()
        for part in raw.split(":"):
            part = part.strip()
            if not part:
                continue
            p = Path(part).expanduser()
            out.append(p)
            try:
                rp = p.resolve()
            except OSError:
                continue
            if rp == home or rp in home.parents:
                logger.warning(
                    "SANDBOX_EXTRA_RW открывает широкий путь %s (дом/корень) — "
                    "песочница фактически разворачивается. Указывай узкие каталоги.",
                    p,
                )
        return tuple(out)

    @staticmethod
    def _parse_chat_id(raw: str) -> int | None:
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            raise SystemExit(
                f"TELEGRAM_CHAT_ID={raw!r} — должно быть целое число (ID группы). "
                "Узнать: добавь бота в группу и пошли /chat_id."
            )

    @staticmethod
    def _parse_permission_mode(raw: str) -> str:
        mode = raw.strip()
        # По `claude --help`: acceptEdits, auto, bypassPermissions, manual,
        # dontAsk, plan. "bypass" — наш синоним --dangerously-skip-permissions.
        valid = {"bypass", "auto", "acceptEdits", "manual", "dontAsk", "plan", "default"}
        if mode not in valid:
            raise SystemExit(
                f"PERMISSION_MODE={mode!r} — допустимые значения: {', '.join(sorted(valid))}"
            )
        return mode

    @staticmethod
    def _parse_bool(raw: str) -> bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _parse_bool_default_on(raw: str) -> bool:
        """Как `_parse_bool`, но fail-safe: ЛЮБОЕ незнакомое значение → True.

        Для security-флагов (wallet_guard, automode_classify_all_shell,
        wallet_policy_edit), где опечатка/мусор в env должны оставлять защиту
        ВКЛючённой, а не молча снимать её (в отличие от `_parse_bool`, где
        незнакомое = False)."""
        return raw.strip().lower() not in ("0", "false", "no", "off")

    @staticmethod
    def _parse_user_ids(raw: str) -> frozenset[int]:
        ids: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                logger.warning("ALLOWED_USER_IDS: пропущено некорректное значение %r", part)
        return frozenset(ids)
