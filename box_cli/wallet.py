"""Кошелёк для CLI `claude-box` (Launcher §5.2): standalone, БЕЗ оркестратора.

`--wallet <secret>` работает по виду секрета — оба пути отдают лончеру одно и то
же: env-довесок + доп. RW-бинд + асинхронный teardown (WalletIntercept).

  * ПРОКСИ-секрет (connector) → MITM-перехват TLS: поднимается standalone-прокси,
    в песочницу уходят HTTPS_PROXY + объединённый CA-bundle; кред подставляется
    прокси между машиной и сервисом, в песочницу значение не попадает (§4.2).
  * host/inject-секрет → ШИМЫ: поднимается standalone-демон кошелька
    (vault.cli.build_daemon + хост лончера, см. box_cli/tty.py) и каталог
    PATH-обёрток (vault.shims): модель зовёт `git push`/`gh`/`curl` как обычно, а
    обёртка уходит в `wallet exec` — команда исполняется НА ХОСТЕ с кредом, в
    песочницу возвращается только (отредактированный) вывод. Плюс WALLET_FILE →
    временный wallet.json (url+token демона) и обёртка самого клиента `wallet`.

Что делает прокси-путь по шагам:

  1. secrets.toml → SecretStore → секрет по имени. Не найден / не прокси-секрет /
     не разрешён этой «сессии» → honest-отказ (WalletError, код 2).
  2. VaultCA (корень для MITM) + SessionProxyPool(host=TtyVaultHost) → port.
     upstream_ssl НЕ передаём — СИСТЕМНЫЙ trust: реориджин к реальному сервису
     проверяет его настоящий серт, самозванец под сервис кред не получит (§4.2).
  3. CA-bundle (системные корни + корень Vault) во ВРЕМЕННЫЙ каталог + env-довесок
     (HTTPS_PROXY + *_CA_BUNDLE + NO_PROXY) — общий примитив vault.inject.
     proxy_sandbox_env.

Куда класть файлы в CLI-песочнице. У box_cli под bwrap $HOME — пустой tmpfs
(home_dir не задаётся), а RW-виден только рабочий каталог (bind src==dst). Класть
bundle/шимы/wallet.json в проект оператора — сорить в его дереве; поэтому кладём
во ВРЕМЕННЫЙ каталог и биндим его в песочницу тем же путём (extra_rw). Пути тогда
одинаковы внутри и снаружи (для engine=off песочницы нет — путь просто хостовый).
На выходе весь временный каталог сносится — не течём.

Секрет наружу не уходит: значение живёт только в прокси между машиной и сервисом,
в env/лог/bundle попадает лишь публичный CA и адрес loopback-прокси.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from vault.cli import build_daemon, write_wallet
from vault.daemon import VaultDaemon
from vault.inject import proxy_sandbox_env
from vault.host import VaultHost
from vault.policy import PolicyEditor
from vault.proxy_pool import ProxyPoolError, SessionProxyPool
from vault.secret import Secret
from vault.shims import SHIM_DIRNAME, install_cli, tool_names, write_shims
from vault.store import SecretStore
from vault.tls import VaultCA, VaultCAError
from vault.tty_host import TtyVaultHost

logger = logging.getLogger("claude-box.wallet")

# Синтетическое имя «сессии» для standalone-перехвата: у прокси-секрета в policy
# должно быть sessions = ["*"] или ["claude-box"], чтобы он разрешил этот запуск.
SESSION_NAME = "claude-box"

# Имя файла с url+token демона во временном каталоге. Не `.wallet.json` в $HOME:
# под bwrap $HOME — пустой tmpfs, туда писать бессмысленно; клиент находит файл
# по WALLET_FILE (bin/wallet смотрит его ПЕРВЫМ, до ~/.wallet.json).
WALLET_FILE_NAME = "wallet.json"

# Имя PEM-файла с корнем VaultCA для `--egress-ca` форка agent-vm (путь VM). Файл
# читает agent-vm НА ХОСТЕ (в гостя не биндится) — им intercept-прокси agent-vm
# доверяет НАШЕМУ vault-прокси на upstream-плече. Значение секрета сюда не входит:
# это ПУБЛИЧНЫЙ корень, которым наш прокси подписывает свои листовые серты.
EGRESS_CA_NAME = "vault-egress-ca.pem"
# POSIX-имя env-переменной: валидируем env-поле inject-секрета перед тем как
# положить его в env гостя и в имя файла (см. _setup_inject_env_vm).
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def box_policy_access(secrets_path: Path) -> tuple[PolicyEditor, bool]:
    """PolicyEditor для ASK-гранта «навсегда» + можно ли писать в secrets.toml.

    Для claude-box правка policy РАЗРЕШЕНА по умолчанию (в отличие от бота с
    тумблером WALLET_POLICY_EDIT): это файл самого оператора, и оператор сидит в
    tty — некому и незачем это запрещать. НО правка обязана быть физически
    возможной. Проверяем ДВА уровня прав, потому что защита именно здесь, в
    гейте, а НЕ в самом PolicyEditor: он пишет через временный файл в КАТАЛОГЕ +
    os.replace, поэтому запись в файл-на-RO он бы обошёл (rename проверяет права
    каталога, не файла). Поэтому здесь отдельно требуем и запись в каталог
    (создать tmp/lock), и запись в сам файл, если он есть (chmod 400 на файл —
    честный сигнал «правок не хочу», проверено: os.access(файл, W_OK)=False у
    владельца RO-файла). Не проходит любой из уровней → allow=False, и
    BoxVaultHost честно НЕ предложит «навсегда» (только разовый грант), а не
    упадёт при попытке записи. Гарантия держится, только если grant_scope зовут
    ПОСЛЕ этого гейта (как делает cli.py) — прямой вызов PolicyEditor его минует.

    Редактор возвращаем всегда (он читает файл заново на каждую правку и полезен
    для сообщений об отзыве); решает лишь второй элемент — разрешена ли запись.
    """
    editor = PolicyEditor(secrets_path)
    parent = secrets_path.parent
    # Каталог должен пускать на запись (создать tmp/lock) и обход (X). Файла может
    # ещё не быть — тогда важен только каталог; если файл есть и RO — не предлагаем.
    allow = os.access(parent, os.W_OK | os.X_OK)
    if allow and secrets_path.exists() and not os.access(secrets_path, os.W_OK):
        allow = False
    return editor, allow


class WalletError(Exception):
    """Отказ настройки перехвата с кодом выхода CLI (2 — ошибка ввода/policy,
    1 — сбой окружения: нет openssl/системных корней/прокси не поднялся)."""

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class WalletIntercept:
    """Результат настройки: env-довесок к box.launch, доп. RW-бинды песочницы и
    асинхронный teardown. Один тип на все пути (прокси/шимы/VM) — лончеру всё
    равно, что именно поднято: он делает env.update + extra_rw + close() в finally.

    Поля egress_*/env_files заполнены ТОЛЬКО на VM-пути. env в гостя не течёт (F1),
    поэтому env/extra_rw там пусты, а кошелёк доставляется гостю через флаги форка:
      * egress_proxy (прокси-секрет) — `http://127.0.0.1:<порт>` нашего vault-прокси.
        Прокси слушает на LOOPBACK: egress гостя re-originates сам agent-vm и делает
        CONNECT к нам С ХОСТА (замерено: peer=127.0.0.1), гость к прокси не ходит —
        LAN-bind был бы лишней поверхностью (порт слышала бы вся подсеть);
      * egress_ca — путь к PEM с корнем VaultCA для `--egress-ca` (файл читает
        agent-vm на ХОСТЕ, в гостя не едет);
      * env_files (inject-секрет) — `NAME=PATH` для `--env-file`: значение в env
        гостя, читается форком из файла (в argv не входит).
    allow_egress — None (для loopback-прокси --allow-egress не нужен). Под
    bwrap/off/шимы все поля пусты, лончер работает как раньше (env-довесок).
    """

    env: dict[str, str]
    extra_rw: list[Path] = field(default_factory=list)
    egress_proxy: str | None = None
    egress_ca: Path | None = None
    allow_egress: str | None = None
    # env_files — пары «env-имя=путь-к-файлу-со-значением» для `--env-file` форка
    # agent-vm (VM-путь inject-секрета): значение читается из временного файла
    # 0600, в argv/лог не попадает. Пусто на всех прочих путях. Файл лежит в
    # _tmpdir — teardown (close) сносит его вместе с прокси/CA.
    env_files: list[str] = field(default_factory=list)
    _pool: SessionProxyPool | None = None
    _daemon: VaultDaemon | None = None
    _tmpdir: Path | None = None

    async def close(self) -> None:
        """Снять прокси/остановить демон (порты освобождаются) и удалить временный
        каталог (bundle либо шимы+wallet.json). Идемпотентно и не роняет выход —
        teardown обязан отработать в finally."""
        if self._pool is not None:
            try:
                await self._pool.stop_all()
            except Exception:  # noqa: BLE001 — teardown не должен ронять выход CLI
                logger.warning("wallet: сбой снятия прокси на выходе", exc_info=True)
            self._pool = None
        if self._daemon is not None:
            try:
                await self._daemon.stop()
            except Exception:  # noqa: BLE001 — teardown не должен ронять выход CLI
                logger.warning("wallet: сбой остановки демона на выходе", exc_info=True)
            self._daemon = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None


async def setup_wallet_intercept(
    secret_name: str, *, secrets_path: Path, session_name: str = SESSION_NAME,
    host: VaultHost | None = None, vm: bool = False,
) -> WalletIntercept:
    """Поднять кошелёк под один секрет и вернуть WalletIntercept.

    Вид секрета И движок решают КАК:
      * прокси-секрет (connector) под bwrap/off → MITM-перехват TLS через env
        HTTPS_PROXY + CA-bundle (значение подставляет прокси на проводе);
      * прокси-секрет под --vm → egress-proxy форка agent-vm: наш vault-прокси на
        127.0.0.1, egress гостя заворачивает сам agent-vm с хоста (`--egress-proxy`);
      * host/inject-секрет под bwrap/off → PATH-шимы над standalone-демоном;
      * INJECT-секрет под --vm → значение в env гостя через `--env-file` форка
        (значение из файла, не в argv) — консистентно с inject под bwrap (env);
      * HOST-passthrough (шимы) под --vm → ЧЕСТНЫЙ отказ (код 2): git/gh ведёт сам
        agent-vm (§5.2), заворачивать/инъектить нечего.

    Бросает WalletError (с кодом выхода) на любой честный отказ; всё уже поднятое
    при отказе сворачивается (не течём).

    host — реализация VaultHost для confirm/ASK. У claude-box это ОБЯЗАН быть
    хост, который делит stdin с PTY-relay через арбитра терминала (box_cli.tty.
    BoxVaultHost): TtyVaultHost вешает СВОЙ add_reader на тот же fd, что и relay,
    а в finally снимает читателя целиком — после первого же confirm клавиатура в
    сессии умирала бы навсегда. Дефолт (None → TtyVaultHost) оставлен только для
    прямых вызовов из тестов/скриптов, где stdin ничей.
    """
    store = SecretStore(secrets_path)
    secret = store.load().get(secret_name)
    if secret is None:
        raise WalletError(
            f"секрет «{secret_name}» не найден в {secrets_path} "
            "(проверь имя и права файла 0600). См. `vault policy`.", code=2)
    if not secret.session_allowed(session_name):
        raise WalletError(
            f"секрет «{secret_name}» не разрешён «{session_name}»: добавь "
            f'sessions = ["{session_name}"] (или ["*"]) в его запись secrets.toml.',
            code=2)
    if secret.is_proxy:
        if vm:
            return await _setup_proxy_intercept_vm(
                secret_name, store=store, session_name=session_name, host=host)
        return await _setup_proxy_intercept(
            secret_name, store=store, session_name=session_name, host=host)
    # Не прокси-секрет. Под bwrap/off — PATH-шимы над демоном (inject и host
    # одинаково). Под --vm шимов нет (§5.2: git/gh ведёт сам agent-vm), но два
    # подвида расходятся:
    #   * inject (value+env) → значение доставляется прямой инъекцией в env гостя
    #     через `--env NAME=VALUE` форка agent-vm — то же, что inject под bwrap
    #     (значение в окружении процесса claude);
    #   * host-passthrough (только команды, без value/env) → честный отказ (код 2):
    #     заворачивать нечего (git/gh ведёт agent-vm) и инъектить нечего.
    if vm:
        if secret.value and secret.env:
            return _setup_inject_env_vm(secret_name, secret=secret)
        raise WalletError(
            f"секрет «{secret_name}» — host-passthrough (обёртки git/gh/curl), а под "
            "--vm они бессмысленны: git/gh ведёт сам agent-vm (§5.2), заворачивать "
            "нечего, а значения для инъекции у host-passthrough нет. Под --vm "
            "работают прокси-секрет (connector) и inject-секрет (value+env); "
            "host-passthrough — под --engine bwrap.", code=2)
    return await setup_wallet_shims(
        secret_name, secret=secret, secrets_path=secrets_path,
        session_name=session_name, host=host)


async def _setup_proxy_intercept(
    secret_name: str, *, store: SecretStore, session_name: str,
    host: VaultHost | None = None,
) -> WalletIntercept:
    """Прокси-секрет: MITM-перехват TLS (env HTTPS_PROXY + CA-bundle)."""
    # Корень CA для MITM. Нет openssl → перехват невозможен (сбой окружения, код 1).
    try:
        ca = VaultCA()
    except VaultCAError as e:
        raise WalletError(
            f"не удалось создать CA Vault ({e}) — нужен openssl. Перехват не поднят.",
            code=1) from e

    # upstream_ssl НЕ передаём (системный trust): реориджин проверяет НАСТОЯЩИЙ серт
    # сервиса — импостор под сервис кред не получит (§4.2). host — арбитр терминала
    # лончера (BoxVaultHost): ASK спрашивается на той же tty, что держит relay, но
    # через ЕДИНСТВЕННОГО владельца stdin, с эхом и самотаймаутом → DENY.
    pool = SessionProxyPool(ca, store, host=host or TtyVaultHost())
    try:
        port = await pool.start(session_name, secret_name)
    except ProxyPoolError as e:
        await pool.stop_all()
        raise WalletError(f"прокси-секрет «{secret_name}» не поднят: {e}", code=2) from e

    tmpdir = Path(tempfile.mkdtemp(prefix="claude-box-vault-"))
    result = proxy_sandbox_env(ca, port, bundle_dir=tmpdir)
    if result is None:
        await pool.stop_all()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise WalletError(
            "системный набор доверенных корней не найден — перехват не включён "
            "(SSL_CERT_FILE только на Vault CA сломал бы прочий TLS).", code=1)
    env, bundle_path, _cleanup = result
    logger.info(
        "wallet: перехват секрета «%s» через 127.0.0.1:%d (trust %s)",
        secret_name, port, bundle_path)
    # Временный каталог биндится в песочницу тем же путём — bundle виден изнутри.
    return WalletIntercept(env=env, extra_rw=[tmpdir], _pool=pool, _tmpdir=tmpdir)


async def _setup_proxy_intercept_vm(
    secret_name: str, *, store: SecretStore, session_name: str,
    host: VaultHost | None = None,
) -> WalletIntercept:
    """Прокси-секрет под --vm: vault-прокси на 127.0.0.1 + флаги форка.

    Топология (соблюдена точно, docs/ARCHITECTURE-claude-box.md):
      гость(claude) → intercept-прокси agent-vm (свой CA, НА ХОСТЕ) → CONNECT →
      НАШ vault-прокси (127.0.0.1, VaultCA) → реальный сервис.
    Ключевое: CONNECT к нашему прокси делает САМ agent-vm с хоста по loopback
    (замерено: peer=127.0.0.1), гость к прокси напрямую не ходит — поэтому
    loopback достаточно, а LAN-bind открыл бы порт всей подсети без аутентификации.

    Отличия от bwrap-пути:
      * env HTTPS_PROXY в гостя не течёт (F1), поэтому доставка — не через env, а
        через `--egress-proxy` форка agent-vm (agent-vm заворачивает egress сюда);
      * корень VaultCA кладётся во ВРЕМЕННЫЙ PEM (для `--egress-ca`): им
        intercept-прокси agent-vm доверяет нашему прокси на upstream-плече. Файл
        читает agent-vm на ХОСТЕ, в гостя не биндится;
      * env/extra_rw пусты — доставка целиком во флагах раннера.

    upstream_ssl НЕ переопределяем (СИСТЕМНЫЙ trust, как в bwrap-пути): реориджин
    к реальному сервису проверяет его настоящий серт — самозванец под сервис кред
    не получит (§4.2). fail-closed: прокси обязан подняться ДО старта VM (иначе
    agent-vm с недостижимым egress-proxy упадёт) — сюда мы возвращаемся только с
    уже поднятым прокси, а лончер строит и запускает VM после этого.

    ГРАНИЦА (image-pull). agent-vm заворачивает через --egress-proxy ВЕСЬ свой
    egress, включая скачивание OCI-образа. Наш vault-прокси MITM-ит и его своим
    VaultCA, а registry-клиент agent-vm нашему CA не доверяет — при ХОЛОДНОМ пуле
    (образа ещё нет в кэше) это сломает старт. Митигация: прогреть образ заранее
    (`agent-vm pull`/первый запуск БЕЗ --wallet) — на кэшированном образе pull не
    ходит в сеть, и перехват влияет только на egress самой сессии.
    """
    # Прокси на 127.0.0.1 (НЕ на LAN-адресе). Ключевой замер: с --egress-proxy
    # egress гостя re-originates САМ agent-vm и делает CONNECT к нашему прокси
    # С ХОСТА по loopback (проверено: CONNECT приходит с peer=127.0.0.1) — гость
    # к прокси напрямую НЕ ходит. Поэтому loopback достаточно, а LAN-bind был бы
    # лишней поверхностью: порт слышала бы вся подсеть без аутентификации
    # (нашло ревью). Как следствие и --allow-egress нашего прокси не нужен (гость
    # не делает RFC1918-соединения к нему; agent-vm bypass'ит loopback сам).
    try:
        ca = VaultCA()
    except VaultCAError as e:
        raise WalletError(
            f"не удалось создать CA Vault ({e}) — нужен openssl. Перехват не поднят.",
            code=1) from e

    # bind_host=127.0.0.1 — дефолт пула; upstream_ssl не трогаем (системный trust,
    # см. docstring). host — арбитр терминала лончера.
    pool = SessionProxyPool(ca, store, host=host or TtyVaultHost())
    try:
        port = await pool.start(session_name, secret_name)
    except ProxyPoolError as e:
        await pool.stop_all()
        raise WalletError(f"прокси-секрет «{secret_name}» не поднят: {e}", code=2) from e

    # Корень VaultCA во временный PEM для --egress-ca. Всё, что уже поднято,
    # сворачиваем при любом сбое записи — не оставляем висеть прокси/порт.
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="claude-box-vault-vm-"))
        ca_file = tmpdir / EGRESS_CA_NAME
        ca_file.write_text(ca.ca_cert_pem(), encoding="utf-8")
        os.chmod(ca_file, 0o644)  # публичный корень; читает agent-vm на хосте
    except OSError as e:
        await pool.stop_all()
        if "tmpdir" in dir():
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise WalletError(
            f"не удалось записать CA для egress-proxy ({e}). Проверь место на диске.",
            code=1) from e

    egress_proxy = f"http://127.0.0.1:{port}"
    # Прозрачность: кошелёк работает через egress-proxy на loopback, не через env.
    # В argv/лог уходит только адрес прокси и публичный CA — значение секрета нет.
    sys.stderr.write(
        f"claude-box: --wallet «{secret_name}» под --vm: кошелёк работает через "
        f"egress-proxy на {egress_proxy} (agent-vm заворачивает egress гостя сюда с "
        "хоста). Значение секрета в гостя/лог/argv не попадает; подстановку делает "
        "vault-прокси между хостом и сервисом.\n")
    logger.info(
        "wallet(vm): перехват секрета «%s» через egress-proxy %s (CA %s)",
        secret_name, egress_proxy, ca_file)
    return WalletIntercept(
        env={}, extra_rw=[], egress_proxy=egress_proxy, egress_ca=ca_file,
        allow_egress=None, _pool=pool, _tmpdir=tmpdir)


def _setup_inject_env_vm(secret_name: str, *, secret: Secret) -> WalletIntercept:
    """Inject-секрет под --vm: значение уходит в env гостя через `--env-file` форка.

    Под bwrap inject-секрет = значение в окружении процесса claude; под --vm ровно
    то же делает форковый `--env-file NAME=PATH` — переменная доезжает в env агента
    В ГОСТЕ, а ЗНАЧЕНИЕ форк читает из ФАЙЛА (флаг v0.1.28,
    docs/FORK-agent-vm-egress-proxy.md). Значит в argv agent-vm попадают только имя
    и путь — значение секрета в командную строку (ps на хосте) НЕ входит. Пишем
    значение во ВРЕМЕННЫЙ файл 0600 (как CA в прокси-пути); teardown (close) сносит
    каталог. Одиночный завершающий \\n форк срезает — значение пишем как есть.

    ГРАНИЦА (confirm). Под --vm нет per-command перехвата (значение просто в env),
    поэтому `confirm=true` тут не применяется — предупреждаем оператора явно.
    """
    # Имя env-переменной идёт И в имя файла (`{env}.env`), И в `NAME=PATH` для
    # форка. Валидируем СТРОГО (POSIX-идентификатор): иначе `env` со слэшем/`..`
    # увёл бы файл значения за пределы приватного tmpdir (Path/), а `=` расщепил
    # бы NAME=PATH на стороне agent-vm (нашло ревью). Граница доверия — оператор,
    # но проверка дешёвая и закрывает подлог/опечатку.
    if not _ENV_NAME_RE.fullmatch(secret.env or ""):
        raise WalletError(
            f"inject-секрет «{secret_name}»: env-имя «{secret.env}» недопустимо — "
            "нужен POSIX-идентификатор [A-Za-z_][A-Za-z0-9_]* (оно идёт в env гостя "
            "и в имя файла).", code=2)

    # Временный каталог 0700 + файл значения 0600. Всё в _tmpdir — teardown его
    # сносит; при сбое записи сворачиваемся сразу (не оставляем каталог висеть).
    tmpdir = None
    try:
        tmpdir = Path(tempfile.mkdtemp(prefix="claude-box-wallet-vm-"))
        os.chmod(tmpdir, 0o700)
        value_file = tmpdir / f"{secret.env}.env"  # имя ради читаемости; значение внутри
        fd = os.open(value_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(secret.value)
    except OSError as e:
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
        raise WalletError(
            f"не удалось записать значение inject-секрета «{secret_name}» ({e}). "
            "Проверь место на диске.", code=1) from e

    warn = ""
    if secret.confirm:
        warn = (" ВНИМАНИЕ: у секрета confirm=true, но под --vm per-command "
                "подтверждения нет — значение доступно агенту в госте сразу.")
    # Прозрачность БЕЗ значения: только имя env-переменной (значение — в файле).
    sys.stderr.write(
        f"claude-box: --wallet «{secret_name}» под --vm: значение уходит в env "
        f"гостя как {secret.env} (флаг --env-file agent-vm — значение читается из "
        "временного файла 0600, в argv/лог его нет)." + warn + "\n")
    logger.info(
        "wallet(vm): inject-секрет «%s» → env гостя %s (значение в файле, не логируем)",
        secret_name, secret.env)
    return WalletIntercept(
        env={}, extra_rw=[], env_files=[f"{secret.env}={value_file}"],
        _tmpdir=tmpdir)


# ── host/inject-секрет: шимы над standalone-демоном ──────────────────────────

async def _stop_quietly(daemon: VaultDaemon) -> None:
    """Остановить демон, не дав сбою остановки перекрыть исходную ошибку.

    Teardown на пути отказа обязан отработать до конца: если daemon.stop() сам
    бросит, мы потеряем причину отказа и оставим висеть порт."""
    try:
        await daemon.stop()
    except Exception:  # noqa: BLE001 — teardown не должен ронять честный отказ
        logger.warning("wallet: сбой остановки демона при сворачивании", exc_info=True)


def wallet_cli_path() -> Path:
    """Путь к stdlib-клиенту `wallet` в репозитории. Репозиторий RO-биндится в
    песочницу тем же путём (BwrapRunner биндит root), поэтому файл достижим и
    изнутри — не хватает лишь имени в PATH, его даёт обёртка в каталоге шимов."""
    return Path(__file__).resolve().parent.parent / "bin" / "wallet"


def build_shim_dir(tmpdir: Path, tools: set[str]) -> Path:
    """Создать каталог шимов внутри tmpdir: обёртки инструментов + сам `wallet`.

    Возвращает путь каталога (он же — первый элемент PATH песочницы). Права:
    каталог 0700, скрипты 0755 (см. vault.shims.write_shims). Клиент `wallet` —
    симлинк на bin/wallet, как в прод-провижне оркестратора (vault.shims.install_cli).
    """
    shim_dir = tmpdir / SHIM_DIRNAME
    write_shims(shim_dir, tools)
    # Каталог мог не появиться (пустой набор инструментов) — сам клиент кладём в
    # любом случае: без него шимы бесполезны, а ручной путь `wallet run` нужен.
    shim_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(shim_dir, 0o700)
    install_cli(shim_dir, wallet_cli_path())
    return shim_dir


def sandbox_path(shim_dir: Path) -> str:
    """PATH песочницы: каталог шимов ПЕРВЫМ, дальше исходный PATH.

    `os.environ.get("PATH", default)` тут был бы багом: дефолт подставляется
    только когда переменной НЕТ, а ПУСТОЙ (или заканчивающийся на «:») PATH даёт
    «пустой элемент» — POSIX трактует его как текущий каталог, и в PATH песочницы
    приезжал бы cwd (там лежит код проекта — `./fake-tool` подхватился бы вместо
    системного). Поэтому `or` (пустая строка → дефолт) и отсев пустых элементов.
    """
    raw = os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    parts = [p for p in raw.split(os.pathsep) if p]
    return os.pathsep.join([str(shim_dir), *parts])


async def setup_wallet_shims(
    secret_name: str, *, secret: Secret, secrets_path: Path, session_name: str,
    host: VaultHost | None = None,
) -> WalletIntercept:
    """host/inject-секрет: поднять standalone-демон и положить в PATH шимы.

    Что получает песочница: PATH=<каталог шимов>:<исходный PATH> и WALLET_FILE на
    временный wallet.json (url+token демона). Значение секрета в песочницу не
    попадает — команда исполняется демоном НА ХОСТЕ, в песочницу идёт только
    отредактированный вывод.

    Демон здесь ОДИН на процесс claude-box и знает всю policy файла secrets.toml
    (демон подбирает секрет по команде сам — так же, как у оркестратора). Шимы же
    ставим только под запрошенный `--wallet <секрет>`: явный запрос оператора, а
    не «всё, что лежит в secrets.toml».

    Секрет приходит УЖЕ загруженным (его достал вызывающий) — повторно парсить
    secrets.toml незачем: лишний I/O и окно рассинхрона, если файл поменяли между
    двумя чтениями. secrets_path остаётся — он нужен демону (policy целиком) и
    сообщениям об ошибке.
    """
    tools = tool_names(secret.effective_commands)
    if not tools:
        raise WalletError(
            f"у секрета «{secret_name}» нет ни одной команды для заворачивания: "
            "в commands либо пусто, либо только глобы (`*`). Добавь имя "
            'инструмента, напр. commands = ["gh", "git"], — тогда claude-box '
            "положит его обёртку в PATH песочницы. См. `vault policy`.", code=2)

    # Демон standalone. host — арбитр терминала лончера (BoxVaultHost): confirm/ASK
    # спрашиваются на ТОЙ ЖЕ tty, что держит relay claude, но через единственного
    # владельца stdin — терминал на время вопроса возвращается в нормальный режим
    # (эхо), ответ не воруется у claude, а после ответа ввод снова льётся в PTY.
    # Молчание оператора = DENY по таймауту. Без host (прямой вызов из тестов) —
    # TtyVaultHost, как у `vault serve`.
    daemon = build_daemon(secrets_path, host=host)
    try:
        await daemon.start()
    except Exception as e:  # noqa: BLE001 — сбой окружения, честный отказ (код 1)
        await _stop_quietly(daemon)  # частично поднятый демон не оставляем висеть
        raise WalletError(
            f"демон кошелька не поднят ({e}) — секрет «{secret_name}» недоступен.",
            code=1) from e

    tmpdir = Path(tempfile.mkdtemp(prefix="claude-box-wallet-"))
    os.chmod(tmpdir, 0o700)
    try:
        # Токен привязан к рабочему каталогу: команды под секретом исполняются на
        # хосте именно в нём (тот же каталог, что RW-биндится в песочницу).
        token = daemon.issue_token(session_name, Path.cwd())
        wallet_file = tmpdir / WALLET_FILE_NAME
        write_wallet(wallet_file, daemon.url, token, session_name)
        shim_dir = build_shim_dir(tmpdir, tools)
    except BaseException as e:
        # ЛЮБОЕ исключение, не только OSError: имя инструмента с NUL-байтом давало
        # ValueError из write_text, и тогда teardown не отрабатывал — демон
        # оставался поднятым, временный каталог переживал выход процесса, а
        # оператор видел сырой трейсбек. Ловим широко (включая CancelledError/
        # KeyboardInterrupt: свернуться надо и на Ctrl-C), сворачиваем всё и
        # пробрасываем честный отказ. Внутренний путь tmpdir из сообщения
        # вырезаем — оператору он ни о чём не говорит, а в лог уходит целиком.
        await _stop_quietly(daemon)
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.warning("wallet: подготовка обёрток провалилась", exc_info=True)
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        detail = str(e).replace(str(tmpdir), "<временный каталог>") or type(e).__name__
        raise WalletError(
            f"не удалось подготовить обёртки кошелька для «{secret_name}» ({detail}). "
            "Проверь commands в secrets.toml и место на диске.", code=1) from e

    env = {
        "PATH": sandbox_path(shim_dir),
        "WALLET_FILE": str(wallet_file),
    }
    # Прозрачность: оператор должен видеть, что именно завёрнуто (эти команды
    # уедут на хост с реальным кредом).
    git_note = " (git — только сетевые подкоманды)" if "git" in tools else ""
    sys.stderr.write(
        f"claude-box: --wallet «{secret_name}»: через кошелёк идут "
        + ", ".join(sorted(tools)) + git_note
        + "; клиент `wallet` в PATH.\n")
    logger.info(
        "wallet: шимы секрета «%s» в %s, демон %s", secret_name, shim_dir, daemon.url)
    return WalletIntercept(
        env=env, extra_rw=[tmpdir], _daemon=daemon, _tmpdir=tmpdir)
