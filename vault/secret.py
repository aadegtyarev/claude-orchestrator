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
        if "-c" in toks:  # -c core.sshCommand=… / protocol.ext.allow=… / core.fsmonitor=…
            return ("Флаг `git -c` переопределяет конфиг и может запустить произвольный "
                    "код на хосте — поэтому запрещён. Запусти git push/pull/fetch БЕЗ "
                    "`-c`; если нужен особый git-конфиг, попроси оператора настроить "
                    "его на хосте.")
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
