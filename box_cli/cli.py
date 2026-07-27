"""CLI `claude-box`: запустить команду в песочнице через box.launch + Engine.

Первый срез Launcher (Слой 2, docs/ARCHITECTURE-claude-box.md §5.1). Что делает:

  1. Сборка argv: минимальный Engine-конфиг (только то, что читают make_runner
     и Runner.wrap) → раннер bwrap|off → wrap([claude, …], chdir=cwd,
     extra_rw=[cwd]). БЕЗ session/channel/hooks/wallet — их докидывает Слой 3.
  2. Запуск: box.launch(argv, …) поднимает процесс под PTY и драйвером вывода.
  3. PTY-relay: stdin терминала → master-fd процесса, вывод (on_output) → stdout;
     терминал в raw при tty, восстановление на выходе; дождаться процесса,
     вернуть его код. Ctrl-C уходит в процесс (raw), fd не текут (драйвер владеет
     и закрывает master; мы джойним его поток).

НЕ в этом срезе (честная заглушка, а не тихий no-op — правило прозрачности):
подкоманда `connect` (коннекторы Vault — отдельный трек).

`-p <промпт>` — unattended-запуск (§4.6/§5.1): промпт уходит claude его же
штатным headless-флагом `-p`, и меняется ТРИ вещи (см. build_command, main_async
и box_cli/tty.py UnattendedVaultHost):
  1. stdin не релеится и арбитр не поднимается — читать чужой терминал (шелл
     оператора, CI) нам нечего и незачем;
  2. терминал не переводится в raw — он не наш; вывод claude по-прежнему идёт в
     stdout через драйвер PTY, код выхода claude становится кодом claude-box;
  3. вопросы кошелька (confirm/ASK) не задаются, а СРАЗУ отклоняются с записью в
     лог: спрашивать некого, а ждать таймаут значило бы держать модель висящей
     (Р0 «никогда не повисать»). Модель получает предписывающий текст —
     UnattendedVaultHost.deny_remedy.

Честная граница unattended: пайп в stdin (`cat f | claude-box -p …`) до claude
НЕ доходит. Причина не в relay, а в конструкции лончера: процесс всегда живёт под
PTY (это его основной продукт — Слой 3 получает именно PTY), и stdin для claude —
терминал, а не наш пайп. Эмулировать «headless stdin» тут значило бы отказаться от
PTY ради одного режима; вместо этого честно пишем границу в --help.

`--vm` — тот же запуск, но в microVM (Engine agent-vm). Это РОВНО короткая форма
`--engine agent-vm`: по UX-доку (§5.1) «то же в microVM — ЕДИНСТВЕННОЕ отличие»,
поэтому оба пути ведут в один и тот же код, а не в две ветки. Одновременное
`--vm --engine bwrap|off` — честный отказ (код 2), а не молчаливый приоритет
одного флага над другим: оператор должен узнать, что его просьбу поняли не так.

Границы VM-режима (честно, см. «Проверенные факты» F1/F4/F10 в
docs/ARCHITECTURE-claude-box.md) — под `--vm` НЕ работают и потому отвергаются:
  * `--profile` — agent-vm ИГНОРИРУЕТ CLAUDE_CONFIG_DIR и сеет креды из $HOME
    процесса agent-vm (F4); наш env-редирект под VM был бы враньём. Подмена
    $HOME сюда не заведена осознанно: по §4.7 она может пересеять отработанный
    одноразовый refresh-токен и РАЗЛОГИНИТЬ учётку оператора — нужен отдельный
    живой smoke.
  * `--wallet` с ПРОКСИ-секретом (connector) — РАБОТАЕТ: под VM env в гостя не
    течёт (F1) и HTTPS_PROXY нашего перехвата не сработал бы, но кошелёк поднимает
    vault-прокси на LAN-адресе хоста (F2 — гость loopback не видит) и направляет
    egress гостя в него через `--egress-proxy`/`--egress-ca` форка agent-vm
    (+ `--allow-egress <LAN-IP>`, т.к. public_only режет RFC1918). Значение
    секрета в гостя/argv/лог не попадает — подстановку делает прокси на проводе.
  * `--wallet` с host/inject-секретом — под VM ЧЕСТНЫЙ отказ (код 2): env в
    гостя не течёт (F1), а PATH-шимов там нет (§5.2: git/gh ведёт сам agent-vm).

Что уезжает в гостя под `--vm`: ТОЛЬКО текущий каталог (его монтирует сам
agent-vm). Корень установки claude-box в VM не монтируется — раннер строится с
`mount_root=False`. Оркестратору этот mount нужен (в госте из него стартует
channel_server.py), нам — нет, а у `--mount` в agent-vm нет режима
только-чтение: гость получил бы запись в код и в `.env` оператора с боевыми
токенами. Под bwrap корень репозитория, наоборот, нужен (шимы кошелька) и
биндится только на чтение — там ничего не меняется.

`--profile <name>` — изолированная идентичность claude (свой CLAUDE_CONFIG_DIR и,
под bwrap, свой $HOME): модель не видит реальные ~/.claude / ~/.ssh оператора,
профили не пересекаются. Подкоманды `init`/`profile` управляют каталогами
профилей. Реализация — box_cli/profiles.py.

Границы профиля (честно, а не «полная изоляция»): изолируются ФС-идентичность и
кредлы в окружении (strip_credentials); НЕ изолируются текущий каталог (он всегда
RW-бинд — запуск из $HOME вернёт домашку) и установка claude (~/.local/... RO).

`--wallet <secret>` — кошелёк для одного секрета (Launcher §5.2). Что именно
поднимается, решает вид секрета:
  * прокси-секрет (connector) → MITM-перехват TLS: standalone-прокси + HTTPS_PROXY
    и объединённый CA-bundle в песочнице;
  * host/inject-секрет → PATH-шимы: standalone-демон кошелька на хосте, а в
    песочнице каталог обёрток (git/gh/curl…) первым в PATH — модель зовёт
    инструменты как обычно, вызов уходит на хост через `wallet exec`.
В обоих случаях значение секрета в песочницу не попадает. Реализация —
box_cli/wallet.py.

Кто владеет stdin. Ровно один арбитр (box_cli/tty.py, StdinArbiter): он и релеит
нажатия в PTY, и задаёт вопросы кошелька (confirm/ASK) — с эхом, с таймаутом
(нет ответа = отказ) и с возвратом raw-режима после ответа. Двух независимых
читателей на одном fd тут быть не может: в asyncio второй add_reader затирает
колбэк первого, а remove_reader снимает читателя целиком — так первый же confirm
навсегда убивал бы клавиатуру в сессии.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import termios
import tty
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from box.launch import launch
from box.pty import TERM_COLS, TERM_ROWS
from orchestrator.runners import Runner, make_runner

from .config import BoxDefaults
from .tty import StdinArbiter


# ── Минимальный Engine-конфиг ────────────────────────────────────────────────
# make_runner читает только .sandbox; BwrapRunner.wrap — .claude_config_dir,
# .sandbox_extra_rw, .sandbox_dbus; DirectRunner (off) — ничего; AgentVmRunner —
# .claude_env и группу .agent_vm_* (см. ниже). Полный orchestrator.config.Config
# для этого не нужен (и требовал бы TELEGRAM_BOT_TOKEN и десятки полей
# UX-оркестратора — это Слой 3). Здесь ровно нужный минимум.
@dataclass(frozen=True)
class EngineConfig:
    sandbox: str  # "bwrap" | "off" | "agent-vm"
    claude_config_dir: Path | None = None
    sandbox_extra_rw: tuple[Path, ...] = ()
    sandbox_dbus: bool = True
    # Docker-прокси в песочнице — ПЕР-СЕССИОННАЯ фича оркестратора: сокет поднимает
    # SessionManager (allowlist на папку проекта сессии) и передаёт раннеру. У
    # standalone claude-box сессий/менеджера нет, поднимать и фильтровать сокет
    # некому — поэтому False, и BwrapRunner docker_sock не биндит. Не «выключено
    # молча»: включать флаг без прокси-сокета было бы враньём.
    sandbox_docker: bool = False
    # ── agent-vm ────────────────────────────────────────────────────────────
    # claude_env в standalone CLI ПУСТ: CLAUDE_ENV_* — механизм оркестратора
    # (он доставляет их в гостя через env-блок settings, F1), а тут никакого
    # settings-провижна нет. Раннер это переживает без обходных путей:
    # auth_problem({}) → None (нет своего ANTHROPIC_BASE_URL — значит и
    # конфликта с кред-прокси agent-vm нет, F3), egress_hosts({}, ip) → []
    # (открывать гостю адрес хоста незачем, к нам никто не ходит).
    claude_env: dict[str, str] = field(default_factory=dict)
    agent_vm_host_ip: str | None = None
    agent_vm_memory_gib: int | None = None  # целые GiB (agent-vm: --memory <GIB>)
    agent_vm_cpus: int | None = None
    agent_vm_image: str | None = None
    agent_vm_egress_proxy: str | None = None
    agent_vm_egress_ca: Path | None = None
    # Инъекция env в гостя через `--env-file NAME=PATH` форка agent-vm (VM-путь
    # inject-секрета кошелька): значение читается из файла, в argv не попадает.
    # Список строк «NAME=PATH». Пусто вне --wallet --vm. Читается только раннером
    # agent-vm; orchestrator.Config такого поля не имеет (его кошелёк работает
    # иначе) — раннер берёт его через getattr с дефолтом [].
    agent_vm_env_files: tuple[str, ...] = ()


ENGINE_VM = "agent-vm"
ENGINES = ("bwrap", "off", ENGINE_VM)
# init/profile реализованы (диспетчеризуются в subcommand_result до parse_args);
# connect — заглушка (коннекторы Vault — отдельный трек).
_STUB_SUBCOMMANDS = ("connect",)
# Формы флага промпта у самого claude: `-p` и `--print`. Ловим ОБЕ и в
# passthrough (см. parse_args), чтобы не собрать `claude -p A --print B`.
_PROMPT_FLAGS = ("-p", "--print")

DEFAULT_SECRETS = "~/.config/claude-orchestrator/secrets.toml"

_USAGE = (
    "claude-box [--engine bwrap|off|agent-vm | --vm] [--profile <имя>] "
    "[--wallet <секрет> [--secrets <файл>]] [-p <промпт>] "
    "[-- <аргументы claude>]\n"
    "  Запустить claude (или CLAUDE_BIN) в песочнице и отдать терминал.\n"
    "  --engine bwrap   файловая песочница bubblewrap (по умолчанию)\n"
    "  --engine off     без изоляции\n"
    "  --engine agent-vm  microVM (нужны бинарь agent-vm и /dev/kvm)\n"
    "  --vm             короткая форма --engine agent-vm (то же в microVM).\n"
    "                   Ресурсы/образ VM — из AGENT_VM_MEMORY_GIB, AGENT_VM_CPUS,\n"
    "                   AGENT_VM_IMAGE, AGENT_VM_EGRESS_PROXY/CA, AGENT_VM_HOST_IP.\n"
    "                   Что НЕ работает под --vm (запуск откажет с кодом 2, а не\n"
    "                   сделает вид, что флаг применён):\n"
    "                     --profile — внутри VM свой claude со своим ~/.claude,\n"
    "                       и профиль хоста туда не пробросить: agent-vm\n"
    "                       игнорирует CLAUDE_CONFIG_DIR и держит креды у себя.\n"
    "                       Нужны профили — запускай без --vm (движок bwrap).\n"
    "                     --wallet с host/inject-секретом (PATH-шимы) —\n"
    "                       окружение в гостя не течёт, git/gh ведёт сам\n"
    "                       agent-vm, шимам не за что зацепиться. Нужны такие\n"
    "                       секреты — запускай без --vm (движок bwrap).\n"
    "                   --wallet с ПРОКСИ-секретом (connector) под --vm РАБОТАЕТ:\n"
    "                   кошелёк поднимает vault-прокси на LAN-адресе хоста и\n"
    "                   направляет egress гостя в него через --egress-proxy форка\n"
    "                   agent-vm; значение секрета в гостя/argv/лог не попадает.\n"
    "                   В госте claude ходит в сеть и в git/gh средствами\n"
    "                   agent-vm; каталог запуска монтируется в VM, ничего\n"
    "                   другого с хоста туда не уезжает\n"
    "  --profile <имя>  изолированная идентичность claude: свой CLAUDE_CONFIG_DIR\n"
    "                   и (под bwrap) свой $HOME; реальные ~/.claude/~/.ssh скрыты.\n"
    "                   Граница: из env вырезаются кредлы (*TOKEN/*SECRET/*KEY,\n"
    "                   SSH_AUTH_SOCK), остальное окружение хоста наследуется;\n"
    "                   текущий каталог и установка claude остаются видны\n"
    "  --wallet         без имени — поднять ВСЕ секреты, которые policy разрешает\n"
    "                   claude-box (набор решает policy, как у сессии\n"
    "                   оркестратора); прокси-секреты в набор не входят — их\n"
    "                   называют явно\n"
    "  --wallet <секрет> дать сессии секрет, не показывая его значение:\n"
    "                   прокси-секрет (connector) → перехват TLS (HTTPS_PROXY+CA),\n"
    "                   host/inject-секрет → обёртки git/gh/curl первыми в PATH\n"
    "                   (вызов уходит на хост через кошелёк, `wallet` тоже в PATH)\n"
    f"  --secrets <файл> путь к secrets.toml (по умолчанию {DEFAULT_SECRETS})\n"
    "  -p <промпт>      unattended: выполнить задачу без терминала и выйти\n"
    "                   (это флаг -p самого claude). Ввод оператора не\n"
    "                   релеится, терминал не переводится в raw, код выхода\n"
    "                   claude становится кодом claude-box.\n"
    "                   ВАЖНО про кошелёк: спрашивать некого, поэтому вопросы\n"
    "                   (confirm=true, ASK вне скоупа) не задаются, а сразу\n"
    "                   отклоняются с записью в лог — модель получает внятный\n"
    "                   отказ, а не зависание. Нужны такие секреты — запускай\n"
    "                   без -p либо разреши их в policy заранее.\n"
    "                   С --vm сочетается (промпт уезжает в гостя как аргумент\n"
    "                   claude), с --profile — тоже.\n"
    "                   Граница: пайп в stdin (cat f | claude-box -p …) в claude\n"
    "                   НЕ пробрасывается — процесс всегда под PTY, и stdin для\n"
    "                   него выглядит терминалом. Данные передавай в самом\n"
    "                   промпте или файлом в рабочем каталоге.\n"
    "  --               всё, что после, пробрасывается в claude\n"
    "  -h, --help       эта справка\n"
    "\nПодкоманды:\n"
    "  init <имя>       создать профиль (идемпотентно) и напечатать путь\n"
    "  profile          список профилей;  profile rm <имя>  удалить профиль\n"
    "  config           умолчания запуска (движок, кошелёк, профиль): без\n"
    "                   аргументов — мастер, `config show` — показать,\n"
    "                   `config engine=off wallet=true` — точечно; пустое\n"
    "                   значение убирает ключ\n"
    "\nНе реализовано (следующий трек): connect."
)


@dataclass(frozen=True)
class Options:
    """Разобранные аргументы CLI. wallet=None — перехват не запрошен."""

    engine: str
    passthrough: list[str]
    wallet: str | None = None
    # --wallet без имени: поднять всё, что policy разрешает claude-box. Отдельным
    # полем, а не «звёздочкой» в wallet: имя секрета и «все секреты» — разные
    # вещи, и склеивать их в одну строку значит однажды перепутать.
    wallet_all: bool = False
    secrets: Path | None = None
    profile: str | None = None  # имя профиля (CLAUDE_CONFIG_DIR/HOME-редирект)
    prompt: str | None = None  # -p <промпт>: unattended (None — интерактив)

    @property
    def wallet_requested(self) -> bool:
        """Запрошен ли кошелёк вообще (конкретным секретом или целиком)."""
        return self.wallet is not None or self.wallet_all

    @property
    def unattended(self) -> bool:
        """Режим без оператора: терминал не наш, вопросы кошелька отклоняются."""
        return self.prompt is not None


class CliError(SystemExit):
    """Ошибка разбора аргументов: печатает в stderr и выходит с кодом 2."""

    def __init__(self, message: str):
        sys.stderr.write("claude-box: " + message + "\n")
        super().__init__(2)


def repo_root() -> Path:
    """Корень репозитория (RO-виден в bwrap как код/венв)."""
    return Path(__file__).resolve().parent.parent


def _config_dir_from_env() -> Path | None:
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


# ── Разбор аргументов ────────────────────────────────────────────────────────
def parse_args(argv: Sequence[str], defaults: BoxDefaults | None = None) -> Options:
    """Разобрать argv → Options. Заглушки/ошибки → CliError.

    Всё после первого `--` — сквозные аргументы claude (не парсятся здесь).

    defaults — умолчания из файла настроек (box_cli.config); флаг всегда сильнее
    файла, а `--profile ""` означает «в этот раз без профиля» (иначе умолчание
    нельзя было бы отключить, не правя файл).
    """
    defaults = defaults or BoxDefaults()
    argv = list(argv)
    if "--" in argv:
        cut = argv.index("--")
        opts, passthrough = argv[:cut], argv[cut + 1:]
    else:
        opts, passthrough = argv, []

    if opts and opts[0] in _STUB_SUBCOMMANDS:
        raise CliError(
            f"подкоманда «{opts[0]}» ещё не реализована (коннекторы Vault — "
            "отдельный трек). Доступно: запуск claude-box "
            "[--engine bwrap|off|agent-vm | --vm] [--profile <имя>] [-- …], "
            "init <имя>, profile."
        )

    engine_flag: str | None = None  # что задано явным --engine (None = не задан)
    vm = False  # был ли --vm (короткая форма --engine agent-vm)
    wallet: str | None = None
    wallet_all = False
    wallet_flag_given = False  # просили флагом (а не унаследовали из файла)
    secrets_flag_given = False
    secrets: Path | None = None
    profile: str | None = None
    prompt: str | None = None

    def _value(flag: str, idx: int, inline: str | None) -> tuple[str, int]:
        """Значение флага: из `--flag=val` (inline) либо из следующего аргумента."""
        if inline is not None:
            return inline, idx + 1
        if idx + 1 >= len(opts):
            raise CliError(f"{flag} требует значение")
        return opts[idx + 1], idx + 2

    i = 0
    while i < len(opts):
        a = opts[i]
        key, eq, inline_val = a.partition("=")
        inline = inline_val if eq else None
        if a in ("-h", "--help"):
            sys.stdout.write(_USAGE + "\n")
            raise SystemExit(0)
        if key == "--engine":
            engine_flag, i = _value("--engine", i, inline)
            continue
        if a == "--vm":
            # Не отдельный режим, а именно короткая форма --engine agent-vm:
            # один путь исполнения, одно поведение (§5.1 «единственное отличие»).
            vm = True
            i += 1
            continue
        if key == "--wallet":
            # Имя необязательно: `--wallet` без значения — «всё, что policy
            # разрешает claude-box» (как у сессии оркестратора), `--wallet gh` —
            # конкретный секрет. Следующий токен считаем значением, только если
            # он не похож на флаг: иначе `--wallet --engine off` съел бы движок.
            nxt = opts[i + 1] if i + 1 < len(opts) else None
            if inline is not None:
                wallet, i = inline, i + 1
            elif nxt is not None and not nxt.startswith("-"):
                wallet, i = nxt, i + 2
            else:
                wallet_all, i = True, i + 1
            wallet_flag_given = True
            continue
        if key == "--secrets":
            raw, i = _value("--secrets", i, inline)
            secrets = Path(raw).expanduser()
            secrets_flag_given = True
            continue
        if key == "--profile":
            profile, i = _value("--profile", i, inline)
            continue
        if key in _PROMPT_FLAGS:
            # Обе формы: `-p задача` и `-p=задача` (вторая — ради единообразия с
            # остальными флагами CLI: `--engine=off`, `--wallet=svc`).
            prompt, i = _value(key, i, inline)
            continue
        raise CliError(f"неизвестный аргумент «{a}». См. claude-box --help.")

    # --vm == --engine agent-vm. Конфликт («--vm --engine bwrap») — честный отказ,
    # а не молчаливый приоритет: оба флага про одно и то же поле, и угадывать, чего
    # хотел оператор, значит запустить его НЕ там, где он просил.
    if vm and engine_flag not in (None, ENGINE_VM):
        raise CliError(
            f"--vm и --engine {engine_flag} несовместимы: --vm — это короткая форма "
            f"--engine {ENGINE_VM}. Оставь что-то одно."
        )
    engine = ENGINE_VM if vm else (engine_flag or defaults.engine or "bwrap")

    if engine not in ENGINES:
        where = "--engine" if (engine_flag or vm) else "engine в файле настроек"
        raise CliError(f"{where}={engine!r} — допустимо: {' | '.join(ENGINES)}")

    # Умолчания из файла — только там, где оператор не сказал иначе флагом.
    if profile is None and defaults.profile:
        profile = defaults.profile
    if profile == "":
        profile = None  # `--profile ""` — «в этот раз без профиля»
    if wallet == "":
        # Симметрично `--profile ""`: пустое значение флага — осознанное «в этот
        # раз без кошелька». Без этого умолчание из файла нельзя было отключить,
        # не правя файл, а флаг переставал быть сильнее файла.
        wallet, wallet_all = None, False
    elif not (wallet or wallet_all) and defaults.wallet is not None:
        if isinstance(defaults.wallet, str):
            wallet = defaults.wallet
        else:
            wallet_all = True
    if secrets is None and defaults.secrets is not None:
        secrets = defaults.secrets

    # Кошелёк из ФАЙЛА умолчаний под движком без изоляции не поднимаем: оператор
    # просил «кошелёк, когда есть песочница», а под `off` он не страхует —
    # процесс и так идёт с его правами и читает secrets.toml напрямую. Явный
    # `--wallet` остаётся рабочим (это осознанный выбор: аутентифицированный
    # канал без изоляции), но main_async печатает про него предупреждение.
    if engine == "off" and (wallet or wallet_all) and not wallet_flag_given:
        sys.stderr.write(
            "claude-box: кошелёк из файла настроек НЕ поднят — движок off не даёт "
            "изоляции, и секреты в такой сессии читаются напрямую.\n"
        )
        wallet, wallet_all = None, False

    # Путь к policy без кошелька бессмыслен, но ругаться на это стоит только
    # когда путь дал ФЛАГ: в файле умолчаний `secrets` — просто «где лежит моя
    # политика», и запуск без кошелька (или с погашенным выше) он ломать не должен.
    if secrets is not None and not (wallet or wallet_all):
        if secrets_flag_given:
            raise CliError("--secrets имеет смысл только с --wallet.")
        secrets = None

    # Пустой промпт (`-p ''`, `-p=`) — честный отказ, а не запуск claude с пустой
    # задачей: он бы отработал вхолостую, а оператор решил бы, что задача ушла.
    if prompt is not None and not prompt.strip():
        raise CliError(
            "-p требует непустой промпт: claude-box -p 'задача'. Нужен "
            "интерактивный запуск — вызывай claude-box без -p."
        )
    # Промпт в passthrough. Правило «после `--` не трогаем ничего» остаётся в
    # силе, но про ЭТОТ флаг молчать нельзя — он меняет режим запуска:
    #   * вместе с нашим -p → отказ: мы кладём `-p <промпт>` в argv ПЕРВЫМ, а
    #     сквозные аргументы после, и claude получил бы два промпта — какой из
    #     них победит, оператор не выбирал;
    #   * вместо нашего -p → запуск идёт (аргумент claude — дело оператора), но с
    #     предупреждением: claude-box об unattended не узнает и оставит relay
    #     stdin, raw-терминал и вопросы кошелька как в интерактиве.
    dup = next((x for x in passthrough if x.partition("=")[0] in _PROMPT_FLAGS), None)
    if dup is not None and prompt is not None:
        raise CliError(
            f"промпт задан дважды: флагом claude-box (-p) и «{dup}» после `--`. "
            "claude получил бы два промпта. Оставь что-то одно — лучше -p самого "
            "claude-box: claude-box -p 'задача' [-- прочие аргументы]."
        )
    if dup is not None:
        sys.stderr.write(
            f"claude-box: «{dup}» после `--` уходит в claude как есть, но сам "
            "claude-box об этом режиме не знает: он оставит relay stdin, "
            "raw-терминал и будет спрашивать подтверждения кошелька у оператора. "
            "Нужен unattended (deny+log вместо спроса) — передавай промпт флагом "
            "claude-box: claude-box -p 'задача'.\n"
        )

    # Границы VM-режима. Отказываем ЗДЕСЬ (код 2), а не «применяем как получится»:
    # под agent-vm оба флага выглядели бы работающими, ничего при этом не делая, —
    # это ровно то враньё, которое запрещает правило прозрачности.
    # --wallet под --vm ЗДЕСЬ не отвергаем: работоспособность зависит от ВИДА
    # секрета (прокси-секрет — работает через --egress-proxy форка; host/inject —
    # нет), а вид известен только после загрузки secrets.toml. Решение принимает
    # box_cli/wallet.setup_wallet_intercept (honest-отказ кодом 2 для host/inject
    # под --vm), а не парсер, который секрета не видит.
    return Options(
        engine=engine, passthrough=passthrough, wallet=wallet,
        wallet_all=wallet_all, secrets=secrets, profile=profile, prompt=prompt,
    )


# ── Сборка запуска ───────────────────────────────────────────────────────────
def agent_vm_env_config() -> dict[str, object]:
    """Поля agent-vm из окружения — ТЕ ЖЕ имена и та же семантика, что в
    orchestrator/config.py (пустая строка = не задано, `~` разворачивается).

    Единственное расхождение с оркестратором сознательное: `agent_vm_host_ip` мы
    берём ТОЛЬКО из явного AGENT_VM_HOST_IP и не запускаем автоопределение
    (`host_lan_ip()` дёргает `ip route`). Причина: раннер использует этот адрес
    ровно в одном месте — `egress_hosts(claude_env, host_ip)`, чтобы открыть
    гостю доступ к хостовому прокси оператора; в standalone CLI claude_env пуст,
    и автоопределение было бы гарантированно мёртвым кодом. Явный оверрайд
    оставлен — он ничего не стоит и понадобится, когда сюда приедет кошелёк.

    Плохое число (AGENT_VM_CPUS=abc) — честный отказ CLI (код 2), а не трейсбек
    посреди запуска. Память и vCPU — ЦЕЛЫЕ (agent-vm 0.1.25: «--memory <GIB>»
    парсится как целое, «2.5» он отвергает — принять дробное значило бы обещать
    то, чего не будет, и уронить запуск уже внутри agent-vm).
    """
    def _num(name: str, cast):
        raw = os.getenv(name, "").strip()
        if not raw:
            return None
        try:
            return cast(raw)
        except ValueError as e:
            raise CliError(f"{name}={raw!r} — ожидалось целое число ({e}).") from e

    ca = os.getenv("AGENT_VM_EGRESS_CA", "").strip()
    return {
        "agent_vm_memory_gib": _num("AGENT_VM_MEMORY_GIB", int),
        "agent_vm_cpus": _num("AGENT_VM_CPUS", int),
        "agent_vm_image": os.getenv("AGENT_VM_IMAGE", "").strip() or None,
        "agent_vm_host_ip": os.getenv("AGENT_VM_HOST_IP", "").strip() or None,
        "agent_vm_egress_proxy": os.getenv("AGENT_VM_EGRESS_PROXY", "").strip() or None,
        "agent_vm_egress_ca": Path(ca).expanduser() if ca else None,
    }


def make_engine_runner(
    engine: str, root: Path, *, claude_config_dir: Path | None = None,
    wallet_vm: dict[str, object] | None = None,
) -> Runner:
    """Раннер Engine (Слой 0) по минимальному конфигу.

    wallet_vm — переопределение полей конфига под `--wallet --vm`. Оно ПЕРЕКРЫВАЕТ
    значения из окружения (кошелёк ведёт egress сам, а не то, что в .env). Два вида:
      * прокси-секрет → {agent_vm_egress_proxy, agent_vm_egress_ca,
        agent_vm_host_ip}: раннер эмитит `--egress-proxy`/`--egress-ca` и, т.к.
        адрес приватный, `--allow-egress <LAN-IP>` (AgentVmRunner.wrap /
        egress_proxy_allow);
      * inject-секрет → {agent_vm_env_files}: раннер эмитит `--env-file NAME=PATH`
        (значение читается из файла, в argv не попадает).
    Вне VM-кошелька — None, конфиг из окружения как раньше.

    claude_config_dir — куда указывает CLAUDE_CONFIG_DIR (профиль передаёт свой
    <profile>/.claude). Без него — из окружения. Важно для bwrap: раннер биндит
    ИМЕННО этот каталог RW; если оставить None у профиля, раннер прибиндил бы
    реальный ~/.claude оператора (утечка) — поэтому профиль всегда его задаёт.
    (Под agent-vm это поле не читается вообще: F4 — CLAUDE_CONFIG_DIR он
    игнорирует; поэтому же --profile под --vm отвергается в parse_args.)

    AGENT_VM_* читаем только для agent-vm — «выключено = не существует»: под
    bwrap/off этих полей в конфиге нет и быть не должно.

    Под agent-vm раннер строим НАПРЯМУЮ, с mount_root=False. Причина в том, что
    у `--mount` в agent-vm нет режима только-чтение: корень установки
    claude-box уехал бы в гостя НА ЗАПИСЬ вместе с `.env` оператора (боевые
    токены). Оркестратору этот mount нужен (в госте из него стартует
    channel_server.py), standalone CLI — нет: тут ни сессии, ни канала, ни
    хуков (см. build_argv). Под bwrap корень репозитория, наоборот, нужен и
    биндится только на чтение — тот путь не трогаем.
    """
    vm_fields = agent_vm_env_config() if engine == ENGINE_VM else {}
    # Кошелёк под --vm перекрывает egress окружения своими значениями (прокси на
    # LAN-адресе хоста + временный CA). Только для agent-vm — под bwrap/off этих
    # полей в конфиге нет («выключено = не существует»), и wallet_egress туда не
    # приходит (VM-путь кошелька поднимается лишь для engine==agent-vm).
    if wallet_vm and engine == ENGINE_VM:
        vm_fields = {**vm_fields, **wallet_vm}
    config = EngineConfig(
        sandbox=engine,
        claude_config_dir=claude_config_dir or _config_dir_from_env(),
        **vm_fields,
    )
    if engine == ENGINE_VM:
        from orchestrator.runners.agentvm import AgentVmRunner
        return AgentVmRunner(config, root, mount_root=False)
    return make_runner(config, root)


def build_command(
    claude_bin: str, *, prompt: str | None, passthrough: Sequence[str],
) -> list[str]:
    """Командная строка claude: `claude [-p <промпт>] [сквозные аргументы]`.

    `-p` — штатный headless-флаг самого claude (он же `--print`), поэтому мы его
    не эмулируем, а просто передаём: claude сам отработает задачу и завершится.

    Порядок «сначала наш -p, потом passthrough» выбран сознательно: `-p` — флаг
    со значением, и вклинивать его в середину чужих аргументов небезопасно
    (значение могло бы приклеиться к чужому флагу), а хвост оператора
    (`-- --model opus`) при таком порядке разбирается claude как обычно. Свой
    `-p`/`--print` в passthrough отвергается ещё в parse_args — иначе у claude
    было бы два промпта, а лончер считал бы запуск интерактивным.
    """
    prompt_part = ["-p", prompt] if prompt is not None else []
    return [claude_bin, *prompt_part, *passthrough]


def build_argv(
    runner: Runner, command: Sequence[str], cwd: Path,
    extra_rw: Sequence[Path] = (),
) -> list[str]:
    """Завернуть команду раннером: cwd — рабочий каталог и RW-путь; extra_rw —
    дополнительные RW-бинды (напр. временный каталог CA-bundle для --wallet, чтобы
    он был виден в песочнице тем же путём).

    Без session/channel/hooks: только «запусти это в песочнице в этом каталоге».
    """
    return runner.wrap(list(command), chdir=cwd, extra_rw=[cwd, *extra_rw])


# Имена env-переменных, которые не должны уезжать в чужую идентичность (--profile).
# Денилист по подстроке: покрывает ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN,
# CLAUDE_VM_PROXY_ACCESS_TOKEN, GH_TOKEN, AWS_SECRET_ACCESS_KEY и т.п.
_CRED_SUBSTRINGS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "APIKEY", "API_KEY",
                    "CREDENTIAL", "PRIVATE_KEY", "ACCESS_KEY")
# Точечно: сокет ssh-агента — это ключи оператора, доступные без файлов в $HOME.
_CRED_EXACT = ("SSH_AUTH_SOCK",)


def strip_credentials(env: dict[str, str]) -> list[str]:
    """Вырезать кредлы оператора из env на месте; вернуть имена вырезанного.

    Профиль изолирует ФС (свой $HOME/CONFIG_DIR), но окружение наследуется от
    оператора целиком — и там живут его же токены (живой пример: OAuth-токен
    прокси). Без этой чистки «изолированная идентичность» аутентифицировалась бы
    как оператор. Денилист, а не allowlist: allowlist сломал бы PATH/локаль/
    прокси-настройки, от которых зависит запуск; границу честно проговариваем в
    --help (остальное окружение хоста наследуется).
    """
    dropped = [
        k for k in env
        if k in _CRED_EXACT or any(s in k.upper() for s in _CRED_SUBSTRINGS)
    ]
    for k in dropped:
        env.pop(k, None)
    return sorted(dropped)


def build_env(engine: str, *, profile: bool = False) -> dict[str, str]:
    """Окружение процесса: копия текущего + TERM; под bwrap вырезаем X/Wayland.

    Зеркалит минимум из sessions._start_claude: без $DISPLAY процесс в песочнице
    не дёрнет хостовый GUI (сеть у bwrap общая с хостом, X-сокет достижим).

    profile=True — ещё и чистка кредлов оператора (см. strip_credentials).

    Под agent-vm убираем AGENT_VM_MEMORY_GIB/AGENT_VM_CPUS: это env-алиасы
    флагов самого agent-vm, и мы их уже прочитали и превратили во флаги. Оставь
    мы их в окружении — наша нормализация («мусор/пусто = не задано») стала бы
    враньём: значение доехало бы до agent-vm, и его парсер упал бы на том, что
    CLI обещал проигнорировать. Единственный источник истины — наш разбор.
    """
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    if engine == "bwrap":
        for var in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY"):
            env.pop(var, None)
    if engine == ENGINE_VM:
        from orchestrator.runners.agentvm import strip_own_env
        strip_own_env(env)
    if profile:
        dropped = strip_credentials(env)
        if dropped:
            # Прозрачность: оператор должен видеть, что из окружения убрано.
            sys.stderr.write(
                "claude-box: --profile: из окружения убраны кредлы оператора: "
                + ", ".join(dropped) + "\n")
    return env


# ── PTY-relay и очистка терминала ────────────────────────────────────────────
@contextlib.contextmanager
def raw_terminal(fd: int):
    """Перевести tty в raw на время блока, гарантированно вернуть настройки.

    Не-tty (пайп/файл) — no-op. Восстановление в finally: даже при Ctrl-C или
    исключении терминал не остаётся сломанным.
    """
    if not os.isatty(fd):
        yield
        return
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


async def run(
    argv: Sequence[str],
    *,
    cwd: str,
    env: dict[str, str],
    on_output: Callable[[bytes], None],
    interactive: bool,
    stdin_fd: int = 0,
    rows: int = TERM_ROWS,
    cols: int = TERM_COLS,
    arbiter: StdinArbiter | None = None,
) -> int:
    """Поднять argv под PTY, (при interactive) релеить stdin, дождаться кода.

    Драйвер box.launch дренирует вывод процесса в on_output и владеет master-fd;
    мы джойним его поток после смерти процесса — так весь вывод дослан, а master
    закрыт (fd не течёт).

    Ввод идёт через АРБИТРА (box_cli.tty.StdinArbiter) — единственного владельца
    stdin: он вешает один add_reader и льёт байты в master процесса, а на время
    вопроса кошелька (confirm/ASK) сам переключает терминал и собирает ответ.
    Никто больше на этот fd читателя не вешает: второй add_reader затирает
    колбэк, а remove_reader снимает читателя целиком — так ввод в сессию умирал
    после первого же confirm. arbiter=None — создать своего (тесты/простой запуск).
    """
    handle = await launch(
        argv, cwd=cwd, env=env, on_output=on_output, name="claude-box",
        rows=rows, cols=cols,
    )
    arb = arbiter
    if interactive:
        if arb is None:
            arb = StdinArbiter(stdin_fd)
        arb.set_sink(lambda data: _write_all(handle.pty_master, data))
        arb.start()  # False (fd не селектится) — просто без relay ввода

    try:
        await handle.process.wait()
    finally:
        if arb is not None:
            arb.stop()
        # Дать драйверу дочитать буфер PTY и закрыть master (иначе fd утечёт).
        handle.driver_thread.join(timeout=5)

    return handle.process.returncode or 0


def _write_all(fd: int, data: bytes) -> bool:
    """Записать порцию в fd. False — писать больше некуда (процесс закрыл PTY)."""
    try:
        os.write(fd, data)
    except OSError:
        return False
    return True


async def main_async(argv: Sequence[str]) -> int:
    from .config import ConfigError, load_defaults
    try:
        defaults = load_defaults()
    except ConfigError as e:
        raise CliError(str(e)) from e
    opts = parse_args(argv, defaults)
    engine = opts.engine
    root = repo_root()

    # Профиль (--profile): изолированная идентичность claude. Резолвим ДО раннера,
    # чтобы передать ему <profile>/.claude как CLAUDE_CONFIG_DIR (иначе раннер
    # прибиндил бы реальный ~/.claude). env-довесок (CLAUDE_CONFIG_DIR + HOME под
    # bwrap) и RW-бинд каталога профиля добавляются ниже.
    profile_env_extra: dict[str, str] = {}
    profile_rw: list[Path] = []
    profile_config_dir: Path | None = None
    if opts.profile is not None:
        from .profiles import ProfileError, profile_env
        if engine == ENGINE_VM:
            # Под VM профиль выбирает УЧЁТКУ (agent-vm сеет креды из $HOME своего
            # процесса, F4), а не изолирует ФС — изоляция там своя, у самой VM.
            # Честно говорим про ротацию: refresh-токен claude.ai одноразовый, и
            # им пользуется ТА учётка, чей файл прочитал agent-vm (§4.7).
            sys.stderr.write(
                f"claude-box: --profile «{opts.profile}» под --vm выбирает УЧЁТКУ: "
                "agent-vm возьмёт креды из <профиль>/.claude/.credentials.json "
                "(CLAUDE_CONFIG_DIR он игнорирует). Файловой изоляции профиля тут "
                "нет — её даёт сама VM. ВНИМАНИЕ: refresh-токен claude.ai "
                "одноразовый и ротируется — если тот же аккаунт параллельно живёт "
                "на хосте, обновление в одном месте разлогинит другое.\n")
        elif engine != "bwrap":
            sys.stderr.write(
                "claude-box: --profile без bwrap НЕ изолирует $HOME — модель видит "
                "реальную домашку оператора; редирект CLAUDE_CONFIG_DIR работает, но "
                "полную изоляцию профиля даёт только --engine bwrap.\n")
        try:
            profile_env_extra, pdir = profile_env(opts.profile, engine=engine)
        except ProfileError as e:
            sys.stderr.write(f"claude-box: --profile: {e}\n")
            return e.code
        # Под VM каталог профиля НЕ биндим и config-dir раннеру не отдаём:
        # песочницы bwrap здесь нет, а agent-vm читает профиль на ХОСТЕ сам.
        if engine != ENGINE_VM:
            profile_config_dir = pdir / ".claude"
            profile_rw = [pdir]

    # Раннер и preflight строятся НИЖЕ, внутри try/finally: под --wallet --vm
    # раннеру нужны egress-поля, которые отдаёт уже поднятый кошелёк, а его
    # teardown обязан отработать даже если preflight упадёт (машина без KVM).
    cwd = os.getcwd()
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    command = build_command(
        claude_bin, prompt=opts.prompt, passthrough=opts.passthrough)
    env = build_env(engine, profile=opts.profile is not None)
    # Профиль-редирект поверх окружения (CLAUDE_CONFIG_DIR + HOME под bwrap);
    # wallet ниже добавляет свой HTTPS_PROXY/CA — не пересекается с этими ключами.
    env.update(profile_env_extra)

    # Кошелёк (--wallet): поднять ДО песочницы прокси-перехват (прокси-секрет) либо
    # демон+шимы (host/inject-секрет) и получить env-довесок (HTTPS_PROXY+CA либо
    # PATH+WALLET_FILE) + доп. RW-бинд временного каталога. Отказ (нет секрета/не
    # разрешён/нечего заворачивать/сбой окружения) — честное сообщение + код из
    # WalletError, без трейсбека. Teardown — в finally ниже.
    intercept = None
    # extra_rw начинается с каталога профиля (RW-бинд src==dst → HOME/CONFIG_DIR
    # валидны изнутри песочницы); wallet при наличии докидывает свой bundle-каталог.
    extra_rw: list[Path] = list(profile_rw)
    # Переопределение раннера под --wallet --vm: egress-поля (прокси-секрет) либо
    # inject-env (inject-секрет). None вне этого случая — раннер строится из
    # окружения как раньше (bwrap/off/шимы кошелька env не меняют этот путь).
    wallet_vm: dict[str, object] | None = None
    # Арбитр stdin создаём ЗДЕСЬ — до raw_terminal (он запоминает нормальные
    # настройки терминала, чтобы возвращать эхо на время вопроса) и до подъёма
    # кошелька (демону нужен хост, который спрашивает через арбитра).
    #
    # В unattended (-p) арбитра НЕТ вовсе: спрашивать некого и релеить нечего —
    # поднимать читателя чужого stdin (терминал оператора, из которого запущен
    # скрипт/CI) значило бы воровать у него ввод. Терминал по той же причине не
    # переводится в raw (см. ниже): claude-box в этом режиме — обычная утилита,
    # которая печатает вывод и завершается.
    arbiter = None if opts.unattended else StdinArbiter(0)
    if opts.wallet_requested:
        from .tty import BoxVaultHost, UnattendedVaultHost
        from .wallet import (
            WalletError, box_policy_access, setup_wallet_all, setup_wallet_intercept,
        )
        if engine == "off":
            sys.stderr.write(
                "claude-box: --wallet с --engine off: песочницы НЕТ. Модель и так "
                "работает на хосте оператора без изоляции, а кошелёк ДОБАВЛЯЕТ ей "
                "поверх этого рабочий аутентифицированный канал (git push/gh с "
                "реальными кредами) — это расширение прав, а не страховка. "
                "Кошелёк страхует только вместе с --engine bwrap.\n")
        if opts.unattended:
            # Сочетание рабочее (перехват поднимается как обычно, значение
            # секрета в песочницу не попадает), но половина policy в нём
            # неприменима — оператор должен узнать это ДО запуска, а не по
            # непонятному провалу задачи.
            sys.stderr.write(
                "claude-box: -p с --wallet: в unattended-режиме вопросы кошелька "
                "не задаются, а сразу отклоняются с записью в лог — секреты с "
                "confirm=true (и любой ASK вне скоупа) в этом запуске НЕ "
                "сработают. Нужны они — запускай без -p (вопрос придёт в "
                "терминал) либо заранее сними confirm/расширь скоуп в policy.\n")
        secrets_path = opts.secrets or Path(DEFAULT_SECRETS).expanduser()
        # Хост кошелька = кто отвечает на confirm/ASK. Attended — арбитр
        # терминала; unattended — deny+log (§4.6), без таймаутов и без stdin.
        # BoxVaultHost получает PolicyEditor и флаг «можно ли писать policy» — для
        # третьего исхода ASK «разрешить навсегда» (§4.6). Правка разрешена, если
        # secrets.toml физически пишется; иначе хост честно предложит только
        # разовый грант (см. box_policy_access / BoxVaultHost._grant_offer).
        if arbiter is None:
            host = UnattendedVaultHost()
        else:
            policy, allow_edit = box_policy_access(secrets_path)
            host = BoxVaultHost(
                arbiter, policy=policy, allow_policy_edit=allow_edit)
        try:
            if opts.wallet_all:
                intercept = await setup_wallet_all(
                    secrets_path=secrets_path, host=host, vm=engine == ENGINE_VM)
            else:
                intercept = await setup_wallet_intercept(
                    opts.wallet, secrets_path=secrets_path, host=host,
                    vm=engine == ENGINE_VM)
        except WalletError as e:
            sys.stderr.write(f"claude-box: --wallet: {e}\n")
            return e.code
        env.update(intercept.env)
        extra_rw.extend(intercept.extra_rw)
        # Кошелёк под --vm доставляется НЕ через env (в гостя не течёт), а флагами
        # форка agent-vm — собрать переопределение для раннера:
        #   * прокси-секрет → --egress-proxy на наш прокси (LAN-IP) + --egress-ca +
        #     --allow-egress LAN-IP (public_only гостя иначе режет RFC1918);
        #   * inject-секрет → --env-file NAME=PATH (значение из файла, не в argv).
        if intercept.egress_proxy is not None:
            wallet_vm = {
                "agent_vm_egress_proxy": intercept.egress_proxy,
                "agent_vm_egress_ca": intercept.egress_ca,
                "agent_vm_host_ip": intercept.allow_egress,
            }
        elif intercept.env_files:
            wallet_vm = {"agent_vm_env_files": tuple(intercept.env_files)}

    # try/finally оборачивает ВСЁ после подъёма перехвата (раннер, preflight,
    # build_argv, замер терминала, запуск) — иначе исключение в этом узком окне
    # утекло бы прокси-порт и временный каталог до входа в защищённый блок (ревью
    # LOW-4). Раннер строим ЗДЕСЬ, а не раньше: под --wallet --vm ему нужны
    # egress-поля от уже поднятого кошелька (wallet_egress), а его teardown обязан
    # отработать, даже если preflight откажет на машине без KVM.
    try:
        runner = make_engine_runner(
            engine, root, claude_config_dir=profile_config_dir,
            wallet_vm=wallet_vm)

        # Preflight движка. Для agent-vm он проверяет отсутствие бинаря/KVM и
        # egress-флаги форка (под --wallet --vm форк ОБЯЗАН знать --egress-proxy —
        # иначе кошелёк был бы тихо обойдён). Оператор обязан увидеть внятную
        # причину, а не трейсбек и не молчаливый откат в bwrap: «в VM» и «не в VM» —
        # разные гарантии, подменять их нельзя.
        ok, why = runner.preflight()
        if not ok:
            fallback = (
                "Без microVM: claude-box (bwrap) — файловая песочница на этой машине.\n"
                if engine == ENGINE_VM else
                "Попробуй --engine off (без изоляции).\n"
            )
            sys.stderr.write(
                f"claude-box: движок «{engine}» не готов: {why}\n" + fallback)
            return 1

        full_argv = build_argv(runner, command, Path(cwd), extra_rw)

        stdin_fd, stdout_fd = 0, 1
        # interactive = «релеить stdin оператора в PTY процесса». В unattended
        # (-p) — нет, даже если запуск идёт из терминала: задача уже задана
        # промптом, а нажатия оператора принадлежат его шеллу/скрипту.
        interactive = os.isatty(stdin_fd) and not opts.unattended
        rows, cols = TERM_ROWS, TERM_COLS
        # Размер PTY берём от терминала вывода, если он есть, — и в unattended
        # тоже: релея ввода там нет, но перенос строк в выводе claude зависит от
        # ширины, и «как в моём терминале» лучше дефолтных 80 колонок. Нет
        # терминала (пайп/файл/CI) — остаются дефолты.
        try:
            size = os.get_terminal_size(stdout_fd)
            rows, cols = size.lines, size.columns
        except OSError:
            pass

        def on_output(chunk: bytes) -> None:
            with contextlib.suppress(OSError):
                os.write(stdout_fd, chunk)

        # raw_terminal возвращает терминал в finally — обёртка исключений НЕ должна
        # его перекрыть, поэтому try внутри with (терминал восстановлен раньше, чем
        # печатаем ошибку). Сбой спавна/запуска (напр. CLAUDE_BIN на несуществующий
        # бинарь) → честный отказ в stderr + код 1, а не сырой трейсбек: остальной
        # CLI держит эту планку (CliError), держим и здесь.
        #
        # raw нужен ТОЛЬКО когда мы релеим ввод (иначе нажатия оператора шли бы
        # построчно и с двойным эхом). В unattended терминал оператора не наш —
        # трогать его настройки нельзя: `claude-box -p … &` в фоне сломал бы
        # шелл, из которого запущен (raw на общем tty).
        with raw_terminal(stdin_fd) if interactive else contextlib.nullcontext():
            try:
                return await run(
                    full_argv, cwd=cwd, env=env, on_output=on_output,
                    interactive=interactive, stdin_fd=stdin_fd, rows=rows, cols=cols,
                    arbiter=arbiter,
                )
            except OSError as e:
                sys.stderr.write(
                    f"claude-box: не удалось запустить «{claude_bin}»: {e}\n"
                    "Проверь CLAUDE_BIN и что бинарь есть в PATH.\n"
                )
                return 1
            except Exception as e:  # noqa: BLE001 — любой сбой запуска = честный отказ
                sys.stderr.write(f"claude-box: сбой запуска: {e}\n")
                return 1
    finally:
        # Снять читатель stdin (кошелёк мог поднять его раньше запуска, спросив
        # confirm), снять прокси (порт освобождается) и снести временный каталог
        # bundle — даже при исключении/Ctrl-C. Не течём.
        if arbiter is not None:  # в unattended арбитра нет — снимать нечего
            arbiter.stop()
        if intercept is not None:
            await intercept.close()


# ── Подкоманды управления профилями ──────────────────────────────────────────
# init/profile НЕ запускают claude (нет PTY/asyncio) — диспетчеризуются в main до
# запуска рантайма. Логика профилей — в box_cli.profiles (stdlib, без оркестратора).
def cmd_init(args: Sequence[str]) -> int:
    """`init <имя>`: идемпотентно создать профиль и напечатать его путь (код 0)."""
    from .profiles import ProfileError, ensure_profile
    if len(args) != 1:
        raise CliError("init требует ровно один аргумент: init <имя>.")
    try:
        path = ensure_profile(args[0])
    except ProfileError as e:
        raise CliError(str(e)) from e
    sys.stdout.write(f"{path}\n")
    return 0


def cmd_profile(args: Sequence[str]) -> int:
    """`profile` — список профилей; `profile rm <имя>` — удалить каталог профиля."""
    from .profiles import ProfileError, list_profiles, remove_profile
    if not args:
        names = list_profiles()
        if not names:
            sys.stdout.write("нет профилей (создай: claude-box init <имя>)\n")
        else:
            sys.stdout.write("\n".join(names) + "\n")
        return 0
    if args[0] == "rm":
        if len(args) != 2:
            raise CliError("profile rm требует имя: profile rm <имя>.")
        try:
            path = remove_profile(args[1])
        except ProfileError as e:
            raise CliError(str(e)) from e
        sys.stdout.write(f"удалён профиль: {path}\n")
        return 0
    raise CliError(
        f"неизвестный подарг profile «{args[0]}». Доступно: profile, profile rm <имя>.")


def cmd_config(args: Sequence[str]) -> int:
    """`claude-box config` — посмотреть и изменить умолчания запуска.

    Три формы, потому что у них разные потребители:
      * без аргументов на терминале — мастер (те же вопросы, что задаёт
        install.sh, но с текущими значениями): перенастроить можно когда угодно,
        а не только при первой установке;
      * `config show` (и любой вызов без терминала — CI, пайп) — печать текущего
        состояния и пути файла, без интерактива;
      * `config engine=off wallet=true` — точечная правка без вопросов; пустое
        значение (`profile=`) убирает ключ.
    """
    from .config import BoxDefaults, ConfigError, config_path, load_defaults, save

    path = config_path()
    try:
        current = load_defaults(path)
    except ConfigError as e:
        # Файл сломан — именно эта команда и должна помочь его починить,
        # поэтому не падаем, а показываем причину и правим поверх.
        sys.stderr.write(f"claude-box: {e}\n")
        current = BoxDefaults()

    if args and args[0] == "show":
        if args[1:]:
            raise CliError("`config show` не принимает аргументов.")
        _print_config(current, path)
        return 0

    if args:
        try:
            updated = _apply_settings(current, args)
        except ValueError as e:
            raise CliError(str(e)) from e
    elif sys.stdin.isatty():
        updated = _ask_config(current)
    else:
        _print_config(current, path)
        return 0

    _print_config(updated, save(updated, path))
    return 0


def _print_config(defaults, path: Path) -> None:
    from .config import render
    suffix = "" if path.exists() else " (файла нет — умолчания встроенные)"
    sys.stdout.write(f"{path}{suffix}\n")
    sys.stdout.write(render(defaults) if not defaults.empty else "# ничего не задано\n")


def _apply_settings(current, args: Sequence[str]):
    """`ключ=значение` → новые умолчания. Пустое значение убирает ключ."""
    from dataclasses import replace as _replace

    out = current
    for arg in args:
        key, eq, value = arg.partition("=")
        if not eq:
            raise ValueError(
                f"«{arg}» — ожидалось ключ=значение (engine, profile, wallet, "
                "secrets). Показать текущее: claude-box config show."
            )
        key, value = key.strip(), value.strip()
        if key == "engine":
            engine = None
            if value:
                engine = ENGINE_VM if value in ("vm", ENGINE_VM) else value
                if engine not in ENGINES:
                    raise ValueError(
                        f"engine={value!r} — допустимо: {' | '.join(ENGINES)} (или vm)")
            out = _replace(out, engine=engine)
        elif key == "profile":
            if value:
                _validate_profile(value)
            out = _replace(out, profile=value or None)
        elif key == "wallet":
            low = value.lower()
            if low in ("true", "yes", "on", "1"):
                wallet: str | bool | None = True
            elif low in ("", "false", "no", "off", "0"):
                wallet = None
            else:
                wallet = value  # имя конкретного секрета
            out = _replace(out, wallet=wallet)
        elif key == "secrets":
            out = _replace(out, secrets=Path(value).expanduser() if value else None)
        else:
            raise ValueError(
                f"неизвестный ключ «{key}». Допустимо: engine, profile, wallet, secrets."
            )
    return out


def _validate_profile(name: str) -> None:
    """Имя профиля идёт и в файл настроек, и в путь каталога — проверяем тем же
    правилом, что и сам запуск, иначе кривое имя ломало бы каждый следующий вызов."""
    from .profiles import ProfileError, validate_name
    try:
        validate_name(name)
    except ProfileError as e:
        raise ValueError(str(e)) from e


def _ask_config(current):
    """Мастер: те же вопросы, что при установке, но с текущими значениями."""
    from dataclasses import replace as _replace

    engine_now = current.engine or "bwrap"
    answer = input(f"Изоляция по умолчанию [bwrap/off/vm] ({engine_now}): ").strip()
    engine = engine_now if not answer else (
        ENGINE_VM if answer in ("vm", ENGINE_VM) else answer)
    if engine not in ENGINES:
        raise CliError(f"engine={answer!r} — допустимо: {' | '.join(ENGINES)} (или vm)")

    wallet_now = "да" if current.wallet else "нет"
    answer = input(
        f"Кошелёк, когда изоляция есть? [y/n] ({wallet_now}): ").strip().lower()
    if not answer:
        wallet = current.wallet
    elif answer in ("n", "no", "н", "нет"):
        wallet = None
    else:
        wallet = True

    profile_now = current.profile or "общий ~/.claude"
    answer = input(
        f"Профиль claude по умолчанию ({profile_now}; «-» — убрать): ").strip()
    if not answer:
        profile = current.profile
    elif answer == "-":
        profile = None
    else:
        try:
            _validate_profile(answer)
        except ValueError as e:
            raise CliError(str(e)) from e
        profile = answer
    return _replace(current, engine=engine, wallet=wallet, profile=profile)


def subcommand_result(argv: Sequence[str]) -> int | None:
    """Если argv — подкоманда, выполнить её и вернуть код; иначе None (запуск claude).

    Подкомандой считаем только первый токен-НЕ-флаг из известного набора, чтобы
    `--engine`/`--profile …` уходили в обычный разбор parse_args.
    """
    if not argv:
        return None
    cmd = argv[0]
    if cmd == "init":
        return cmd_init(argv[1:])
    if cmd == "profile":
        return cmd_profile(argv[1:])
    if cmd == "config":
        return cmd_config(argv[1:])
    if cmd == "connect":
        # Заглушка: коннекторы Vault — отдельный трек. Честный отказ (код 2).
        raise CliError(
            "подкоманда «connect» ещё не реализована (коннекторы Vault — "
            "отдельный трек).")
    return None


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sub = subcommand_result(args)
    except SystemExit as e:  # CliError (код 2) — уже напечатал в stderr
        return e.code if isinstance(e.code, int) else 2
    if sub is not None:
        return sub
    try:
        return asyncio.run(main_async(args))
    except SystemExit as e:
        # CliError изнутри main_async (напр. кривой AGENT_VM_CPUS при сборке
        # конфига движка) — сообщение уже в stderr; отдаём её код как обычный
        # результат, чтобы SystemExit не летел сквозь sys.exit(main()).
        return e.code if isinstance(e.code, int) else 2
    except KeyboardInterrupt:
        return 130
