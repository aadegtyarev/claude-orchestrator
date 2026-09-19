"""Домен секрета: dataclass Secret (policy одной записи), guard опасных вызовов,
маркеры inject-секретов. Без зависимостей оркестратора и без aiohttp.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field

# Маркер секрета для скрытых inject-секретов. В env песочницы вместо реального
# значения кладётся `<<wallet:имя>>`; модель пишет привычный `$ENV`, шелл
# разворачивает его в маркер, а демон подставляет РЕАЛЬНОЕ значение на хосте
# (в аргумент — inline, либо `:file` — во временный 0600-файл). Значение в
# песочницу/контекст модели не попадает; из вывода редактируется.
MARKER_RE = re.compile(r"<<wallet:([A-Za-z0-9_-]+)(:file)?>>")


def marker(name: str, as_file: bool = False) -> str:
    return f"<<wallet:{name}{':file' if as_file else ''}>>"


# Дефолтный набор для host-passthrough, когда `commands` не задан: инструменты,
# которые обычно уже авторизованы на хосте и не отдают сам секрет наружу. Смысл
# кошелька — «используй, но не читай»: gh/git/ssh/scp применяют креды сами,
# echo/cat/sh их бы просто распечатали, поэтому в дефолт НЕ входят. Хочешь
# curl/kubectl/своё — допиши commands явно.
DEFAULT_HOST_COMMANDS = ("gh", "git", "ssh", "scp")
# Сетевые подкоманды git заворачиваем в кошелёк (креды хоста); всё остальное
# (status/add/commit/log/diff) бежит настоящим git прямо в песочнице — быстро и
# без хостового раунд-трипа. Список намеренно узкий: только то, что ходит в сеть.
GIT_NETWORK = ("push", "fetch", "pull", "clone", "ls-remote", "send-pack", "fetch-pack")

# SSH-хосты, чьи git-remote'ы (`git@host:...`) переписываем на HTTPS изнутри
# песочницы (git `url.<base>.insteadOf`, GIT_CONFIG_COUNT/KEY/VALUE в env
# процесса claude — см. WalletModule.session_env). ~/.ssh намеренно не
# монтируется в bwrap (файловая изоляция, orchestrator/runners/sandbox.py) —
# SSH-клоны/встроенные обновители плагинов (`claude plugin update`) внутри
# сессии иначе не проходят handshake («remote end hung up unexpectedly»).
# Переписывание на HTTPS уводит их на уже рабочий путь: gh credential helper
# на ХОСТЕ авторизует HTTPS без единого ключа внутри песочницы — тот же канал,
# что уже используют сетевые git-подкоманды через git_shim (см. GIT_NETWORK).
# Список намеренно узкий (только github.com) — расширять по мере надобности,
# не «все возможные git-хосты» по умолчанию.
GIT_SSH_HTTPS_REWRITE = {"github.com": "https://github.com/"}


def git_ssh_rewrite_env() -> dict[str, str]:
    """env-довесок (GIT_CONFIG_COUNT/KEY_N/VALUE_N — git 2.31+), переписывающий
    `git@<host>:...` в HTTPS для хостов из GIT_SSH_HTTPS_REWRITE.

    Через env, а не файл `~/.gitconfig` в приватном $HOME сессии: не требует
    отдельной атомарной/симлинк-безопасной записи (модель RW-видит session_home
    и могла бы подложить туда свой конфиг/симлинк), не конфликтует с
    GIT_CONFIG_NOSYSTEM/GIT_CONFIG_GLOBAL=/dev/null из vault.execute (тот
    контур — ХОСТОВОЕ исполнение через wallet exec, другой процесс/env), и не
    может быть случайно перезаписан моделью через собственный `git config`.
    Применяется общей утилитой GIT_CONFIG_* — git видит их как обычную
    конфигурацию, не более приоритетную и не менее переопределяемую, чем
    ~/.gitconfig был бы."""
    out: dict[str, str] = {}
    for i, (host, https_base) in enumerate(GIT_SSH_HTTPS_REWRITE.items()):
        out[f"GIT_CONFIG_KEY_{i}"] = f"url.{https_base}.insteadOf"
        out[f"GIT_CONFIG_VALUE_{i}"] = f"git@{host}:"
    if out:
        out["GIT_CONFIG_COUNT"] = str(len(GIT_SSH_HTTPS_REWRITE))
    return out


def _prints_token(cmd: list[str]) -> bool:
    """gh-команда, печатающая сам токен: `gh auth token` или `… --show-token`.

    Guard её и так режет (см. `_always_denied`). Выделено отдельно, чтобы НЕ
    поднимать operator-notice на этот отказ: он самокорректирующийся (модель
    получает предписывающий reason в stderr), а фоновый поллер Claude Code
    («PR status» в футере) зовёт ровно `gh auth token --hostname github.com`
    периодически — с notice это спамило бы чат на каждый опрос. Первая
    не-флаговая подкоманда, как в guard: `gh --флаг auth token` не проскочит,
    `gh pr create --title "auth token"` не ложно-сработает."""
    if not cmd or os.path.basename(cmd[0]) != "gh":
        return False
    subs = [a for a in cmd[1:] if not a.startswith("-")]
    return subs[:1] == ["auth"] and (subs[1:2] == ["token"] or "--show-token" in cmd)


# ── git -c: не флаг целиком, а пара key=value ───────────────────────────────
# Раньше guard рубил ЛЮБОЙ `git -c`. Это ломало не модель, а сам Claude Code:
# он добавляет к своим внутренним git-вызовам набор «закаливающих» пар
# (`core.hooksPath=/dev/null`, `core.fsmonitor=`, `core.askPass=`,
# `protocol.ext.allow=never`, `submodule.recurse=false`, `log.showSignature=
# false`, `http.sslVerify=true`, …) — ровно чтобы репозиторий НЕ мог исполнить
# свой код. Когда подкоманда сетевая (fetch/ls-remote/push), такой вызов уходит
# в кошелёк, и оператор получал отказ на пустом месте, а фоновый опрос (PR
# status) спамил чат.
#
# Поэтому смотрим на пары. Исполнение/увод кредов в git дают КОНКРЕТНЫЕ ключи
# конфига (их список исторически известен: *Command/*.helper/hooks/filter/
# alias/include/…). Такой ключ пропускаем ТОЛЬКО с обезвреживающим значением
# (пусто / `false` / `never` / `/dev/null`), любое другое — отказ; остальные
# ключи (fsck, pack.*, log.*, submodule.recurse, http.proxy…) кодом не
# оборачиваются и проходят как есть. Пустое множество = ключ запрещён всегда.
#
# Ключ сравниваем в нижнем регистре: git считает регистр значимым только в
# середине (`url.<ЭТО>.insteadOf`), а lowercase лишь РАСШИРЯЕТ совпадение —
# промахнуться мимо запрета так нельзя.
_GIT_C_NEUTRAL: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    # Запускают программу (askPass/pager/editor/fsmonitor/hooks/ssh/proxy).
    (re.compile(r"core\.sshcommand"),
     frozenset({"", "ssh -o batchmode=yes -o stricthostkeychecking=yes"})),
    (re.compile(r"core\.askpass"), frozenset({"", "true"})),
    # core.pager и per-подкомандный pager.<sub> (`pager.log=evil`) — оба
    # запускают программу на вывод; `cat`/`false` безобидны.
    (re.compile(r"core\.pager|pager\..+"), frozenset({"", "false", "cat"})),
    (re.compile(r"(core|sequence)\.editor"), frozenset({"", "true"})),
    (re.compile(r"core\.fsmonitor"), frozenset({"", "false"})),
    (re.compile(r"core\.hookspath"), frozenset({"/dev/null"})),
    (re.compile(r"core\.(alternaterefscommand|externaldiff|gitproxy)"), frozenset({""})),
    (re.compile(r"credential(\..*)?\.helper"), frozenset({""})),
    (re.compile(r"diff\.external"), frozenset({""})),
    (re.compile(r"diff\..*\.(command|textconv)"), frozenset({""})),
    # filter.<x>.clean/smudge/process — код на checkout/add; `required=false`
    # и `enabled=false` из того же набора Claude Code безобидны.
    (re.compile(r"filter\..*"), frozenset({"", "false"})),
    (re.compile(r"merge\..*\.driver"), frozenset({""})),
    (re.compile(r"(diff|merge)tool\..*\.cmd"), frozenset({""})),
    (re.compile(r"gpg(\..*)?\.program"), frozenset({""})),
    (re.compile(r"uploadpack\.packobjectshook"), frozenset({""})),
    # sendemail.smtpServer, будучи путём, запускается как программа-sendmail.
    (re.compile(r"sendemail(\..*)?\.smtpserver"), frozenset({""})),
    (re.compile(r"remote\..*\.(uploadpack|receivepack)"), frozenset({""})),
    # Транспорт ext:: (произвольная команда) — только `never`.
    (re.compile(r"protocol(\..*)?\.allow"), frozenset({"never"})),
    # Подтягивают ЧУЖОЙ конфиг (в нём — любой ключ выше): запрещены всегда.
    (re.compile(r"include(if\..*)?\.path"), frozenset()),
    # alias.x=!команда — исполнение; пустой алиас безобиден (его ставит сам CC).
    (re.compile(r"alias\..*"), frozenset({""})),
    # templateDir приносит в новый репозиторий свои хуки.
    (re.compile(r"init\.templatedir"), frozenset({""})),
    # Отключённая проверка TLS = MITM хостовых кредов. CC ставит только `true`.
    (re.compile(r"http(\..*)?\.sslverify"), frozenset({"true"})),
)

# url.<base>.insteadOf переписывает remote — уводит push/fetch (и хостовые креды)
# на чужой хост. Claude Code ставит ТОЖДЕСТВЕННОЕ правило (`url.<u>.insteadOf=<u>`,
# чтобы запретить чужие переписывания из конфига репозитория) — его и пропускаем.
_GIT_C_INSTEADOF = re.compile(r"url\.(?P<base>.+)\.(insteadof|pushinsteadof)$")

# Сеть второго рубежа: ключей, оборачивающих КОМАНДУ, в git больше, чем принято
# помнить (`pager.<подкоманда>`, `trailer.<x>.command`, `imap.tunnel`,
# `instaweb.httpd`, `browser.<x>.cmd`, …), и в новых версиях их прибавляется.
# Поэтому любой НЕизвестный ключ, чьё имя оканчивается «командным» словом,
# пропускаем только с обезвреживающим значением. Список суффиксов намеренно без
# `path` — под него попал бы безобидный `core.quotePath=true`, который ставит
# сам Claude Code (а опасные `core.hooksPath`/`include.path` разобраны выше).
_GIT_C_CODE_SUFFIX = re.compile(
    r".*\.(command|cmd|program|helper|hook|pager|editor|askpass|browser|tunnel"
    r"|driver|textconv|smudge|clean|process|httpd)$"
)
_GIT_C_CODE_SUFFIX_NEUTRAL = frozenset({"", "false", "never", "0", "/dev/null"})


def _git_config_denied(pair: str) -> str | None:
    """Причина отказа для одной пары `git -c key[=value]` либо None.

    Ключ без `=` git трактует как `key=true` — так же трактуем и мы (для
    «опасных» ключей `true` не входит в обезвреживающие значения → отказ).
    """
    key, sep, value = pair.partition("=")
    key_l = key.strip().lower()
    value_l = (value if sep else "true").strip().lower()
    bad = (f"Пара `git -c {pair[:80]}` может запустить произвольный код на хосте "
           "или увести креды — поэтому запрещена. Безопасные «закаливающие» пары "
           "(core.hooksPath=/dev/null, core.fsmonitor=, core.askPass=, "
           "protocol.ext.allow=never …) кошелёк пропускает. Запусти git без этой "
           "пары; нужен особый git-конфиг — попроси оператора настроить его на хосте.")
    if (m := _GIT_C_INSTEADOF.match(key_l)) is not None:
        # Тождественное правило (или пустое) — no-op; всё прочее уводит remote.
        base = m.group("base")
        return None if value_l in ("", base) else bad
    for rx, neutral in _GIT_C_NEUTRAL:
        if rx.fullmatch(key_l):
            return None if value_l in neutral else bad
    if _GIT_C_CODE_SUFFIX.fullmatch(key_l):
        return None if value_l in _GIT_C_CODE_SUFFIX_NEUTRAL else bad
    return None


def _git_config_pairs(toks: list[str]) -> tuple[list[str], bool]:
    """Пары из `-c`/`--config`(`=`) и признак `--config-env` в argv.

    `-c` ищем ВЕЗДЕ, а не только до подкоманды: у `git clone` есть свой `-c`
    (`--config`), который пишет ключ в конфиг нового репозитория — та же
    поверхность. Ложных срабатываний это не даёт: до кошелька доезжают только
    сетевые подкоманды (GIT_NETWORK), и ни у одной из них `-c` не значит
    что-то другое.

    `--config-env=KEY=VAR` берёт значение из переменной окружения песочницы —
    проверить его тут нельзя, поэтому он запрещён целиком (второй элемент).
    """
    pairs: list[str] = []
    config_env = False
    i = 0
    while i < len(toks):
        t = toks[i]
        if t in ("-c", "--config"):
            if i + 1 < len(toks):
                pairs.append(toks[i + 1])
            i += 2
            continue
        if t.startswith("--config="):
            pairs.append(t[len("--config="):])
        elif t == "--config-env" or t.startswith("--config-env="):
            config_env = True
        i += 1
    return pairs, config_env


def _always_denied(cmd: list[str]) -> str | None:
    """Опасные вызовы, запрещённые guard'ом — при любой policy, даже `commands=["gh"]`.

    Смысл: голое имя инструмента должно оставаться удобным, не превращаясь в
    утечку токена или запуск произвольного кода на хосте. Возвращает ПРОЗРАЧНОЕ
    сообщение модели (что не так + как правильно) либо None. Guard включается
    флагом WALLET_GUARD (по умолчанию on); применяется в _handle_run.

    Это НЕ полная защита — безфлаговый вектор (подложить `./.git/config` в
    проекте, который `git push` всё равно прочитает) закрыть аргументами нельзя,
    только доверием к сессии (см. docs «Известные дыры»).
    """
    binary = os.path.basename(cmd[0]) if cmd else ""
    # 1. Печатают сам секрет — редакция literal-only их не всегда ловит. Смотрим
    # ПЕРВУЮ не-флаговую подкоманду (чтобы `gh --флаг auth token` не проскочил, но
    # `gh pr create --title "auth token"` не ложно-сработал: там первая подкоманда
    # «pr», а не «auth»).
    if binary == "gh":
        if _prints_token(cmd):
            return ("Эта команда печатает сам токен, а кошелёк не выдаёт значения "
                    "секретов. Токен и НЕ нужен: `git push`/`fetch`/`pull`/`clone` по "
                    "HTTPS авторизуются на хосте через кошелёк (gh credential helper "
                    "выдаёт токен внутри себя, НЕ печатая его) — делай обычный "
                    "`git push`, HTTPS-remote работает из коробки, SSH-костыль не "
                    "нужен. Для GitHub-операций — gh напрямую (gh pr …, gh api …, "
                    "gh release …). Печатать токен незачем.")
    # 2. git → произвольное исполнение на хосте через конфиг/транспорт/флаги.
    if binary == "git":
        toks = cmd[1:]
        pairs, config_env = _git_config_pairs(toks)
        if config_env:
            return ("Флаг `git --config-env` берёт значение конфига из переменной "
                    "окружения песочницы — проверить его на хосте нельзя, поэтому он "
                    "запрещён. Передай пару явно через `-c key=value` (безопасные "
                    "«закаливающие» пары кошелёк пропускает).")
        for pair in pairs:
            if (reason := _git_config_denied(pair)) is not None:
                return reason
        for t in toks:
            if t.startswith("ext::"):
                return ("git-транспорт `ext::` запускает произвольную команду — запрещён. "
                        "Используй обычный remote (https или ssh) для push/pull/fetch.")
            if t.startswith(("--receive-pack", "--upload-pack", "--exec")):
                return ("Флаги --receive-pack/--upload-pack/--exec запускают произвольную "
                        "команду на той стороне — запрещены. Запусти git push/pull/fetch "
                        "без них.")
    return None


@dataclass(frozen=True)
class Secret:
    """Один секрет/доступ из secrets.toml вместе со своей policy.

    Два вида:
      * inject — value+env: команда получает секрет в env-переменной (env=…,
        value=…). Классический кошелёк.
      * host-passthrough — БЕЗ value/env: команда просто исполняется на ХОСТЕ
        с хостовым окружением (keyring, gh/git auth). Для инструментов, уже
        авторизованных на хосте (gh, git), чьи токены лежат в keyring/файле
        вне песочницы — модель их не видит, а команда работает. Ничего в env
        не инжектим.
    """

    name: str
    value: str  # "" для host-passthrough
    env: str    # "" для host-passthrough
    description: str
    sessions: tuple[str, ...]  # fnmatch-шаблоны имён сессий; пусто = никому
    # commands: где кошелёк доступен (allow-лист). Голое имя инструмента («gh»,
    # «ssh») = любой его вызов; строка с пробелом/глобом («curl https://api/*») =
    # fnmatch по всей команде (тонкая настройка). Для host-passthrough пустое
    # поле = DEFAULT_HOST_COMMANDS; для inject пустое = ничего (сырой токен не
    # открываем без явного списка).
    commands: tuple[str, ...]
    # deny: точечный запрет ПОВЕРХ commands (deny побеждает allow). Голый токен
    # («--force», «--hard») = блок этого флага/аргумента где угодно; строка с
    # пробелом/глобом = fnmatch по всей команде. Для «разрешаю инструмент, но
    # не эти опасные флаги».
    deny: tuple[str, ...]
    # allow_unsafe: точечно отключить встроенный guard (печать токена, git-RCE)
    # для ЭТОГО секрета — для доверенных специфичных случаев. Глобально guard
    # рубится WALLET_GUARD=0; это — гранулярно, на один секрет.
    allow_unsafe: bool
    confirm: bool  # спрашивать ли подтверждение кнопками перед запуском
    # shared: секрет, значение которого модель ДОЛЖНА получить (dev-ключ для её
    # сервиса, логин/пароль для ввода в браузер). Не про конфиденциальность от
    # модели — про хранение вне чата/репо. Выдаётся `wallet get`/`wallet env`;
    # при заданном `env` реальное значение сразу лежит в env песочницы (в отличие
    # от inject, где там маркер). host/inject значения НЕ выдаются никогда.
    shared: bool
    # connector — имя коннектора (§4.5): секрет с коннектором — НЕ host/inject/
    # shared, а «прокси-секрет»: его кред подставляется MITM-прокси МЕЖДУ машиной
    # и сервисом (§4.4), в env песочницы значение не входит. Пусто = сегодняшний
    # секрет (host/inject/shared), прокси не поднимается. Поля с дефолтом — в
    # конце dataclass (обратная совместимость позиционных конструкций).
    connector: str = ""
    # scope — машинный скоуп прокси-секрета, как его понимает коннектор (для
    # generic-bearer: {"url_prefixes": [...]}). Пусто для не-прокси-секретов.
    # NB: dict-поле делает Secret нехешируемым (frozen лишь запрещает переприсвоение
    # атрибута, но не мутацию dict). SecretStore кэширует и переиспользует Secret
    # между load(), поэтому МУТИРОВАТЬ secret.scope на месте нельзя — испортит
    # общий кэш; потребители берут защитную копию (см. proxy_pool.start → dict()).
    scope: dict = field(default_factory=dict)

    @property
    def is_proxy(self) -> bool:
        """Прокси-секрет (§4.5): кред подставляет MITM-прокси по коннектору, а не
        env-инъекция/host-passthrough. Определяется наличием connector."""
        return bool(self.connector)

    @property
    def host_passthrough(self) -> bool:
        # Прокси-секрет НЕ проходной на хост: у него value без env, но команды им
        # не запускаются (иначе он молча раздал бы DEFAULT_HOST_COMMANDS).
        return not self.is_proxy and not (self.value and self.env)

    @property
    def mode(self) -> str:
        if self.is_proxy:
            return "proxy"
        if self.shared:
            return "shared"
        return "host" if self.host_passthrough else "inject"

    @property
    def effective_commands(self) -> tuple[str, ...]:
        if self.commands:
            return self.commands
        return DEFAULT_HOST_COMMANDS if self.host_passthrough else ()

    def session_allowed(self, session_name: str) -> bool:
        return any(fnmatch.fnmatch(session_name, pat) for pat in self.sessions)

    @staticmethod
    def _matches(pat: str, binary: str, cmd: list[str], cmd_str: str) -> bool:
        """Один шаблон против команды: голый токен = имя инструмента или любой
        аргумент; строка с пробелом/глобом = fnmatch по всей строке команды."""
        if " " not in pat and not any(c in pat for c in "*?["):
            return binary == pat or pat in cmd[1:]
        return fnmatch.fnmatch(cmd_str, pat)

    def denied_by(self, cmd: list[str]) -> str | None:
        """Точечный запрет секрета (deny). Возвращает сматчивший шаблон или None."""
        if not cmd:
            return None
        binary = os.path.basename(cmd[0])
        cmd_str = " ".join(cmd)
        for pat in self.deny:
            if self._matches(pat, binary, cmd, cmd_str):
                return pat
        return None

    def command_allowed(self, cmd: list[str]) -> bool:
        """Allow-проверка по commands (guard и deny — отдельно, в _handle_run)."""
        if not cmd:
            return False
        binary = os.path.basename(cmd[0])
        cmd_str = " ".join(cmd)
        for pat in self.effective_commands:
            # allow голым именем — только имя инструмента (не «аргумент где-то»),
            # иначе commands=["gh"] разрешил бы «git … gh …». Потому не _matches.
            if " " not in pat and not any(c in pat for c in "*?["):
                if binary == pat:
                    return True
            elif fnmatch.fnmatch(cmd_str, pat):
                return True
        return False
