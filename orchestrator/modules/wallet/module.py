"""Демон-кошелёк секретов: docs/secrets-wallet.md.

Модель угроз: всё, что лежит внутри песочницы, модель может прочитать (Bash и
Read работают от её имени). Поэтому секрет не появляется в песочнице вообще —
команда исполняется демоном НА ХОСТЕ с секретом в env короткоживущего ребёнка,
а в песочницу возвращается только вывод (значения ВСЕХ известных секретов в нём
заменены на плейсхолдер `•••`).

Прозрачный шлюз: чтобы модель не дёргала CLI вручную, при провижне сессии в её
приватный $HOME пишется каталог обёрток .wallet-bin (SHIM_DIRNAME), который ядро
ставит первым в PATH песочницы (SessionManager.path_hooks). Обёртка каждого
разрешённого инструмента — крошечный скрипт `wallet exec <tool> "$@"`; демон
подбирает секрет по команде и исполняет. git — особый случай (_git_shim): сетевые
подкоманды идут через кошелёк, локальные — настоящим git в песочнице. Inject-
секреты видны как env-переменные $NAME с МАРКЕРОМ вместо значения (session_env) —
демон разворачивает маркер в реальное значение на хосте. CLI `bin/wallet`
остаётся ручным путём (run/exec/get/env/ls/help).

Этот модуль — оркестраторный АДАПТЕР над автономным демоном (vault/daemon.py):
провижн (~/.wallet.json, обёртки, CLI-симлинк), хуки сессий (path/env/redact),
правка policy из бота, старт/стоп демона. Сам HTTP-API секретов, реестр токенов и
исполнение под секретом живут в vault/ (без зависимостей оркестратора).

Аутентификация per-session: при провижне демон выдаёт токен, привязанный к
рабочему каталогу сессии (issue_token снимает cwd ОДИН раз — без перерезолва
посреди запроса), а адаптер кладёт в ~/.wallet.json (0600) URL демона и токен.
Policy — TOML-файл config.wallet_secrets_file (0600, вне allowlist песочницы).
Отказ по умолчанию: секрет без sessions/commands не выдаётся никому и ни на что.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

# Домен и демон кошелька вынесены в автономный пакет vault/ (без зависимостей
# оркестратора) — фаза 1 редизайна, docs/ARCHITECTURE-claude-box.md. Модуль
# здесь — оркестраторный адаптер над ним (провижн/хуки сессий/старт демона).
from vault.daemon import VaultDaemon
# Примитивы TLS-перехвата (CA-bundle, атомарная запись, env-довесок, NO_PROXY) —
# общие с CLI-лончером claude-box (§5.2), одна реализация на обоих потребителей.
from vault.inject import (
    atomic_write as _atomic_write,  # локальное имя: симлинк-безопасная запись bundle/.wallet.json
    build_ca_bundle,
    merge_no_proxy,
    proxy_env_vars,
    system_ca_pem as _system_ca_pem,  # noqa: F401 — ре-экспорт для тестов
)
from vault.proxy_pool import ProxyPoolError, SessionProxyPool
from vault.redact import _redact_text
from vault.tls import VaultCA, VaultCAError
from vault.secret import (
    DEFAULT_HOST_COMMANDS,  # noqa: F401 — ре-экспорт для тестов/обратной совместимости
    Secret,  # noqa: F401 — ре-экспорт для тестов
    _always_denied,  # noqa: F401 — ре-экспорт для тестов (движок решения — vault.verdict)
    _prints_token,  # noqa: F401 — ре-экспорт для тестов (исполнение — vault.daemon)
    marker,
)
# Генератор обёрток общий с CLI claude-box (§5.2): один git-шим на обоих
# потребителей (box_cli не может импортировать orchestrator.*).
from vault.shims import SHIM_DIRNAME, git_shim, tool_names, write_shims
from vault.store import DEFAULT_SECRETS_TOML, SecretStore

from .host import OrchestratorVaultHost
from .policy import PolicyEditor, PolicyError

if TYPE_CHECKING:
    from ...config import Config

logger = logging.getLogger(__name__)

# SHIM_DIRNAME (каталог per-session обёрток в приватном доме сессии; ставится
# ПЕРВЫМ в PATH песочницы через SessionManager.path_hooks) — из vault.shims,
# ре-экспорт выше: имя знает и ядро (session_home), держим одно на всех.

# Объединённый trust-bundle (системные корни + CA Vault) в приватном доме
# сессии. SSL_CERT_FILE указывает СЮДА, а не только на CA Vault: иначе процесс
# перестал бы доверять всем прочим сертам (api.anthropic.com, github…) —
# системный trust надо СОХРАНИТЬ и лишь ДОБАВИТЬ к нему корень Vault. Публичный
# серт (0644), ключа CA в песочнице нет (каталог CA — вне allowlist).
CA_BUNDLE_NAME = ".vault-ca-bundle.crt"


class WalletModule:
    """Модуль ядра: адаптер над автономным VaultDaemon (провижн/хуки/старт)."""

    name = "wallet"

    def __init__(self, config: "Config"):
        self.config = config
        self.store = SecretStore(config.wallet_secrets_file)
        # Правка policy из бота (/wallet) — просмотр + sessions/commands/deny/
        # confirm/new/rm; значения токенов не показывает и не принимает.
        self.policy = PolicyEditor(config.wallet_secrets_file)
        self.core = None
        # VaultHost: услуги окружения для демона (подтверждение/бабл/аудит/notice).
        # Ставится в start() поверх ядра; демон ходит через него, не через core.
        self.host = None
        # Автономный демон секретов (vault/daemon.py) — создаётся в start().
        self.daemon: VaultDaemon | None = None
        # Перехват TLS (§4.2): общий CA Vault + пул per-session MITM-прокси.
        # Создаются в start() ТОЛЬКО если в policy есть прокси-секрет (иначе
        # openssl-генерация CA и слушающие сокеты не нужны — «выключено = не
        # существует», обратная совместимость для сессий без прокси-секретов).
        self.ca: VaultCA | None = None
        self.proxies: SessionProxyPool | None = None
        # session_name → env-вклад перехвата (HTTPS_PROXY + *_CA_BUNDLE). Пишет
        # launch-хук (порт прокси известен), читает session_env (env процесса).
        self._proxy_env: dict[str, dict[str, str]] = {}

    def handle_command(self, args_str: str) -> str:
        """`/wallet <args>` — просмотр/правка policy кошелька (HTML-текст).
        Ошибки policy возвращаем текстом (не исключением) — ядру не нужно знать
        внутренние типы модуля."""
        try:
            return self.policy.apply(
                (args_str or "").split(),
                allow_edit=self.config.wallet_policy_edit,
            )
        except PolicyError as e:
            return f"⚠️ {e}"

    # ── жизненный цикл ──────────────────────────────────────────

    async def start(self, core) -> None:
        self.core = core
        # policy+тумблер — только ради ASK-гранта «навсегда» (§4.6): при
        # WALLET_POLICY_EDIT=0 хост третью кнопку не покажет вовсе.
        self.host = OrchestratorVaultHost(
            core, policy=self.policy, allow_policy_edit=self.config.wallet_policy_edit
        )
        # Провижн (~/.wallet.json в приватном доме сессии) виден CLI только
        # когда дом смонтирован КАК $HOME процесса claude — это делает лишь
        # BwrapRunner. Под off/agent-vm CLI не найдёт конфиг → предупреждаем.
        if self.config.sandbox != "bwrap":
            # Кошелёк НЕ страхует без песочницы: без bwrap $HOME не изолирован,
            # модель читает secrets.toml/keyring/env напрямую — весь смысл
            # (секрет вне досягаемости модели) пропадает. Не блокируем (кошелёк
            # может работать), но громко предупреждаем — использовать ТОЛЬКО в
            # связке с SANDBOX=bwrap (см. docs/secrets-wallet.md).
            logger.warning(
                "wallet: SANDBOX=%s — кошелёк НЕ страхует от утечки секретов! Без "
                "SANDBOX=bwrap модель читает %s напрямую (нет изоляции $HOME). "
                "Используй кошелёк ТОЛЬКО вместе с SANDBOX=bwrap. Под off/agent-vm "
                "также `wallet` в сессии не найдёт ~/.wallet.json.",
                self.config.sandbox, self.config.wallet_secrets_file,
            )
        if not self.config.wallet_secrets_file.exists():
            self._write_default_secrets()  # прокол на хост gh/git/ssh/scp «из коробки»
        self.store.load()  # ранняя диагностика прав/синтаксиса — в лог на старте

        # Перехват TLS (§4.2/§4.3): поднимаем CA+пул прокси ТОЛЬКО при наличии
        # прокси-секрета в policy. upstream_ssl пула — ДЕФОЛТНЫЙ (системный
        # trust): НЕ переопределяем, иначе самозванец под сервис получил бы кред
        # (CRITICAL, см. vault_proxy_test.test_default_upstream_rejects_...).
        proxies = self._make_proxy_pool()

        self.daemon = VaultDaemon(
            self.store, self.host, guard_on=self.config.wallet_guard, proxies=proxies
        )
        await self.daemon.start()

        self._provision_cli()
        for session in core.manager.list_all():
            await self._provision(session)
        core.session_hooks.append(self._provision)
        # Удаление сессии → отзыв её токена у демона (иначе токен удалённой
        # сессии остался бы рабочим; раньше это давал _auth через manager.get).
        core.manager.session_delete_hooks.append(self._revoke)
        # Прозрачный шлюз: каталог обёрток (.wallet-bin) первым в PATH песочницы.
        core.manager.path_hooks.append(self.session_path)
        # Перехват TLS: поднять per-session прокси ДО старта claude, чтобы
        # session_env увидел его порт (launch_hooks выполняются перед env_hooks).
        core.manager.launch_hooks.append(self._start_session_proxies)
        # env для песочницы: shared → реальное значение, inject → маркер $NAME.
        core.manager.env_hooks.append(self.session_env)
        # Вымарывать значения секретов из чат-вывода (shared модель видит и может
        # случайно эхнуть — safety-net, чтобы не улетело в Telegram/историю).
        core.output_redactors.append(self.redact_output)

    # ── перехват TLS: пул прокси и его провижн (§4.2/§4.3) ──────

    def _make_proxy_pool(self) -> SessionProxyPool | None:
        """Создать общий CA Vault + пул прокси, ЕСЛИ в policy есть прокси-секрет.

        Нет прокси-секретов → None (демон работает как раньше, без CA/сокетов —
        обратная совместимость). upstream_ssl оставляем ДЕФОЛТНЫМ (системный
        trust): реориджин к реальному сервису обязан проверять его настоящий серт.
        """
        if not any(s.is_proxy for s in self.store.load().values()):
            return None
        try:
            self.ca = VaultCA()
        except VaultCAError as e:
            logger.error(
                "wallet: не удалось создать CA Vault (%s) — перехват TLS выключен, "
                "прокси-секреты работать не будут", e)
            self.ca = None
            return None
        # host=self.host — ASK-спрос гранта идёт через оркестраторный хост (§4.6);
        # пул прокидывает его + имя сессии в per-session VaultProxy. Без host ASK
        # трактовался бы как DENY (host=None), а заглушка ask была бы недостижима.
        self.proxies = SessionProxyPool(
            self.ca, self.store, host=self.host  # upstream_ssl=дефолт
        )
        logger.info("wallet: перехват TLS включён (есть прокси-секреты в policy)")
        return self.proxies

    async def _start_session_proxies(self, session) -> None:
        """launch-хук: поднять MITM-прокси для прокси-секрета сессии и подготовить
        её env-вклад (HTTPS_PROXY + trust-bundle). Идемпотентно (пул переиспользует
        порт). Нет пула/прокси-секретов → чистит вклад и выходит (обратная
        совместимость: обычная сессия ничего нового не получает).

        Несколько прокси-секретов у сессии: HTTPS_PROXY один на процесс, поэтому
        в этом срезе перехват включаем ТОЛЬКО для ПЕРВОГО (по имени) секрета и
        громко предупреждаем. Маршрутизация нескольких сервисов — следующий срез.
        """
        self._proxy_env.pop(session.name, None)
        if self.proxies is None:
            return
        names = sorted(
            s.name for s in self.store.load().values()
            if s.is_proxy and s.session_allowed(session.name)
        )
        if not names:
            return
        if len(names) > 1:
            logger.warning(
                "wallet: сессии %s назначено несколько прокси-секретов (%s) — "
                "HTTPS_PROXY один на процесс, поэтому перехват включён ТОЛЬКО для "
                "первого (%s); маршрутизация нескольких сервисов — следующий срез",
                session.name, ", ".join(names), names[0])
        secret_name = names[0]
        # Снять прежние прокси этой сессии для НЕ-выбранного секрета: если
        # алфавитно первый прокси-секрет сменился между рестартами, старый прокси
        # иначе висел бы орфаном (лишний listening-порт с валидным MITM). Свой
        # прокси (secret_name) не трогаем — start ниже переиспользует его порт.
        for stale in list(self.proxies.ports(session.name)):
            if stale != secret_name:
                await self.proxies.stop(session.name, stale)
        try:
            port = await self.daemon.start_session_proxy(session.name, secret_name)
        except ProxyPoolError as e:
            logger.error(
                "wallet: прокси сессии %s (секрет %s) не поднят: %s",
                session.name, secret_name, e)
            return
        ca_path = self._provision_ca_bundle(session)
        if ca_path is None:
            # Без trust-bundle клиент не доверится leaf прокси — перехват
            # бесполезен; снимаем прокси, чтобы не висел порт впустую.
            await self.daemon.stop_session_proxies(session.name)
            return
        proxy_url = f"http://127.0.0.1:{port}"
        # env-довесок перехвата — общий примитив vault.inject.proxy_env_vars
        # (HTTPS_PROXY + *_CA_BUNDLE + NO_PROXY). HTTP_PROXY НЕ ставится: прокси
        # обслуживает только CONNECT (HTTPS). NO_PROXY уводит контрольный трафик
        # самого claude (loopback + хост оркестратора/прокси-модели) МИМО MITM —
        # иначе одно-проходный форвард (Connection: close, без h2, лимит тела) ломал
        # бы его egress. Внешние сервисы под секретом на loopback не попадают.
        self._proxy_env[session.name] = proxy_env_vars(
            proxy_url, ca_path, self._no_proxy_value())
        logger.info(
            "wallet: сессия %s → перехват секрета %s через 127.0.0.1:%d (trust %s)",
            session.name, secret_name, port, ca_path)

    def _provision_ca_bundle(self, session) -> str | None:
        """Записать объединённый trust-bundle (системные корни + CA Vault) в
        приватный дом сессии (0644) и вернуть путь ВНУТРИ песочницы ($HOME/имя).

        Возвращаем None, если системный набор корней не найден: тогда указывать
        SSL_CERT_FILE только на CA Vault нельзя (сломало бы прочий TLS)."""
        bundle = build_ca_bundle(self.ca)
        if bundle is None:
            logger.error(
                "wallet: системный CA-bundle не найден — перехват TLS для сессии %s "
                "не включён (SSL_CERT_FILE на один Vault CA сломал бы прочий TLS)",
                session.name)
            return None
        path = self.core.manager.session_home(session) / CA_BUNDLE_NAME
        # Атомарно и БЕЗ следования симлинку (модель могла подложить симлинк на
        # victim-файл под этим именем — см. _atomic_write). 0644: публичный серт.
        _atomic_write(path, bundle, 0o644)
        # Под bwrap дом сессии смонтирован КАК $HOME — путь виден изнутри так.
        return str(Path.home() / CA_BUNDLE_NAME)

    def _no_proxy_value(self) -> str:
        """NO_PROXY для процесса claude при активном перехвате: loopback + хост
        оркестратора/прокси-модели, слитые с уже заданным оператором NO_PROXY.
        Контрольный трафик claude идёт мимо строгого одно-проходного форвард-
        прокси; внешние сервисы под секретом на loopback не попадают."""
        extra = [getattr(self.config, attr, "") or "" for attr in ("orch_host", "guest_orch_host")]
        existing = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
        return merge_no_proxy(extra, existing=existing)

    def redact_output(self, text: str) -> str:
        """Заменить значения ВСЕХ секретов (inject/shared) на •••. У host значения
        нет. Общий примитив с `_redact` — см. `_redact_text`."""
        return _redact_text(text, [s.value for s in self.store.load().values()])

    def session_env(self, session) -> dict[str, str]:
        """env для песочницы сессии — чтобы модель писала привычный `$ENV`:
          * shared  → РЕАЛЬНОЕ значение (модель может видеть/использовать);
          * inject  → МАРКЕР `<<wallet:имя>>` (скрыто) — обёртка развернёт его в
            значение на хосте, в песочницу значение не попадает;
          * host-passthrough (нет env) → ничего.
        Плюс маркер пути к файлу `<<wallet:имя:file>>` в `$ENV_FILE` (ssh-ключ,
        сертификат) — для inject-секретов."""
        out: dict[str, str] = {}
        for s in self.store.load().values():
            # proxy-секрет (connector) в песочницу НЕ эмитим: его кред живёт
            # только в прокси (§4.4), маркер/значение в env недопустимы. store уже
            # не активирует proxy-секрет с env — это второй рубеж.
            if s.is_proxy or not s.env or not s.session_allowed(session.name):
                continue
            if s.shared:
                out[s.env] = s.value
            else:
                out[s.env] = marker(s.name)
                out[s.env + "_FILE"] = marker(s.name, as_file=True)
        # Перехват TLS: HTTPS_PROXY + trust-bundle, подготовленные launch-хуком
        # (_start_session_proxies) ДО этого вызова. Пусто для сессий без
        # прокси-секретов — обычная сессия ничего нового не получает.
        out.update(self._proxy_env.get(session.name, {}))
        return out

    def _write_default_secrets(self) -> None:
        """Создать secrets.toml с дефолтным host-passthrough (gh/git/ssh/scp на
        все сессии), чтобы кошелёк работал «из коробки». Пишем 0600 через
        O_EXCL (ни мгновения без прав); гонку/недоступный каталог не роняем."""
        path = self.config.wallet_secrets_file
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(DEFAULT_SECRETS_TOML)
            logger.info(
                "wallet: создан дефолтный %s — прокол на хост gh/git/ssh/scp для "
                "всех сессий (правь через /wallet или руками)", path,
            )
        except FileExistsError:
            pass  # появился параллельно — просто загрузим существующий
        except OSError as e:
            logger.error("wallet: не удалось создать дефолтный %s: %s", path, e)

    @staticmethod
    def _discard(seq: list, item) -> None:
        """Снять хук из списка, молча стерпев отсутствие (идемпотентный stop)."""
        try:
            seq.remove(item)
        except ValueError:
            pass

    async def _revoke(self, session_name: str) -> None:
        """Хук удаления сессии: отозвать её токен у демона и СНЯТЬ её прокси
        (освободив порт). Корутина — SessionManager.delete её дожидается, поэтому
        стоп детерминированный: пересоздание сессии с тем же именем не гонится с
        ещё-не-снятым старым прокси (иначе фон погасил бы уже новый). Гарантийный
        сброс всех прокси — ещё и в stop() демона (stop_all)."""
        if self.daemon is not None:
            self.daemon.revoke_session(session_name)
        self._proxy_env.pop(session_name, None)
        if self.daemon is not None and self.proxies is not None:
            await self.daemon.stop_session_proxies(session_name)

    async def stop(self) -> None:
        if self.core is not None:
            self._discard(self.core.session_hooks, self._provision)
            self._discard(self.core.manager.session_delete_hooks, self._revoke)
            self._discard(self.core.manager.path_hooks, self.session_path)
            self._discard(self.core.manager.launch_hooks, self._start_session_proxies)
            self._discard(self.core.manager.env_hooks, self.session_env)
            self._discard(self.core.output_redactors, self.redact_output)
        self._proxy_env.clear()
        if self.daemon is not None:
            await self.daemon.stop()  # stop_all снимает все прокси, освобождает порты
            self.daemon = None

    def _provision_cli(self) -> None:
        """Симлинк ~/.local/bin/wallet → <репо>/bin/wallet, чтобы CLI был в PATH
        внутри песочницы.

        bwrap биндит и ~/.local/bin, и корень репо (RO, по тем же путям), поэтому
        симлинк разрешается внутри песочницы и самообновляется при обновлении
        bin/wallet. Без этого агент получит «wallet: command not found» — сам
        файл лежит в репо, а в PATH сессии его нет.
        """
        cli = Path(__file__).resolve().parents[3] / "bin" / "wallet"
        link = Path.home() / ".local" / "bin" / "wallet"
        try:
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink():
                if link.readlink() == cli:
                    return  # уже указывает куда надо
                link.unlink()
            elif link.exists():
                logger.warning("wallet: %s — не наш файл, CLI не провижу", link)
                return
            link.symlink_to(cli)
            logger.info("wallet: CLI в PATH сессии (%s → %s)", link, cli)
        except OSError as e:
            logger.error("wallet: не удалось провизить CLI %s: %s", link, e)

    async def _provision(self, session) -> None:
        """Выдать сессии токен (демон привязывает к нему её cwd) и записать
        ~/.wallet.json в её приватный $HOME.

        cwd снимаем ЗДЕСЬ, из уже-аутентифицированного объекта сессии, и отдаём
        демону при выдаче токена — дальше демон его не перерезолвивает (гонка с
        удалением сессии роняла бы effective_cwd(None)). Внутри песочницы
        session_home смонтирован КАК $HOME процесса claude — CLI найдёт файл по
        ~/.wallet.json без настройки.
        """
        cwd = self.core.manager.effective_cwd(session)
        token = self.daemon.issue_token(session.name, cwd)
        path = self.core.manager.session_home(session) / ".wallet.json"
        payload = {
            "url": self.daemon.url,
            "token": token,
            "session": session.name,
        }
        # Атомарно, 0600 и БЕЗ следования симлинку: session_home RW-виден модели в
        # песочнице, симлинком под этим именем она затёрла бы чужой файл токеном
        # (тот же класс, что у CA-bundle — см. _atomic_write).
        _atomic_write(path, json.dumps(payload), 0o600)
        self._provision_shims(session)

    # ── прозрачные обёртки (шлюз в PATH) ─────────────────────────

    def session_path(self, session) -> list[str]:
        """Каталог обёрток кошелька для PATH песочницы (SessionManager.path_hooks,
        prepend). ВАЖНО: путь должен быть таким, как его видит процесс в песочнице.
        Под bwrap session_home смонтирован как $HOME, поэтому обёртки, записанные
        на хосте в session_home/.wallet-bin, видны изнутри по $HOME/.wallet-bin —
        именно этот путь и кладём в PATH (хостовый .homes/<имя>/.wallet-bin внутри
        песочницы не существует). Возвращаем всегда: файлы наполняются из
        session_hooks уже после старта, а каталог bind-смонтирован живым."""
        return [str(Path.home() / SHIM_DIRNAME)]

    def _session_tools(self, session) -> set[str]:
        """Голые имена инструментов, которые надо завернуть для этой сессии —
        из `commands` её НЕ-shared секретов. shared пропускаем: их значение уже
        лежит в env песочницы (модель зовёт инструмент напрямую, хостовый
        раунд-трип не нужен). Отбор имён из шаблонов — vault.shims.tool_names."""
        tools: set[str] = set()
        for s in self.store.load().values():
            if s.shared or not s.session_allowed(session.name):
                continue
            tools |= tool_names(s.effective_commands)
        return tools

    def _git_shim(self) -> str:
        """Обёртка git — общий генератор vault.shims.git_shim (ре-экспорт для тестов)."""
        return git_shim()

    def _provision_shims(self, session) -> None:
        """Полная перегенерация обёрток в <дом-сессии>/.wallet-bin. Заворачиваем
        gh/curl/ssh/… целиком (→ `wallet exec <tool>`), git — особо (см. git_shim).
        Перегенерация чистит устаревшие обёртки, если секрет/команду отозвали."""
        shim_dir = self.core.manager.session_home(session) / SHIM_DIRNAME
        try:
            written = write_shims(shim_dir, self._session_tools(session))
            if written:
                logger.info(
                    "wallet: обёртки сессии %s: %s", session.name, ", ".join(written))
        except OSError as e:
            logger.error("wallet: обёртки для %s не созданы: %s", session.name, e)
