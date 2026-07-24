"""vault.inject — общие примитивы TLS-перехвата для песочницы: сборка объединённого
CA-bundle (системные корни + корень Vault), атомарная запись БЕЗ следования
симлинку, env-довесок процесса (HTTPS_PROXY + *_CA_BUNDLE) и NO_PROXY.

Одна реализация на ДВА потребителя, без дубля (docs/ARCHITECTURE-claude-box.md §4.2/§5.2):
  * оркестраторный адаптер (orchestrator/modules/wallet/module.py, срез 2.5) —
    пишет bundle в приватный дом сессии, ссылается на него как $HOME/<файл>;
  * CLI-лончер claude-box (box_cli, срез 3.x) — пишет bundle во временный каталог,
    биндит его в песочницу по тому же пути и снимает на выходе.

Автономно: только stdlib + vault.tls (ни одного импорта оркестратора — держится
vault_domain_test через walk_packages).

Модель угроз (та же, что у адаптера): всё в песочнице читаемо моделью, поэтому в
bundle кладём ТОЛЬКО публичный CA (0644), а значение секрета сюда не приходит —
его подставляет прокси между машиной и сервисом (§4.4). upstream_ssl прокси при
этом обязан оставаться СИСТЕМНЫМ (не переопределять) — иначе самозванец под сервис
получил бы кред; это решение принимает вызывающий, здесь его нет.
"""

from __future__ import annotations

import contextlib
import logging
import os
import ssl
import tempfile
from pathlib import Path
from typing import Callable, Iterable

from .tls import VaultCA

logger = logging.getLogger(__name__)

# Имя объединённого trust-bundle по умолчанию (публичный серт, 0644).
DEFAULT_BUNDLE_NAME = ".vault-ca-bundle.crt"

# Loopback, всегда уводимый мимо MITM: контрольный трафик самого claude идёт на
# 127.0.0.1 (оркестратор/прокси-модель) и не должен ломаться о строгий
# одно-проходный форвард-прокси. Внешние сервисы под секретом на loopback не
# попадают, перехват для них сохраняется.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def atomic_write(path: Path, data: str, mode: int) -> None:
    """Атомарно записать файл БЕЗ следования симлинку.

    Каталог назначения может быть RW-виден модели в песочнице — она способна
    подложить симлинк на victim-файл под именем нашего файла (напр. `ln -s
    ~/.ssh/authorized_keys ~/.vault-ca-bundle.crt`). Прямой `os.open(path,
    O_TRUNC)` пошёл бы ПО симлинку и затёр цель правами хоста (arbitrary write,
    сандбокс-эскейп). Поэтому пишем во временный файл в ТОЙ ЖЕ директории и
    `os.replace`: rename(2) заменяет саму запись назначения (в т.ч. симлинк), НЕ
    разыменовывая её — victim остаётся нетронутым. Права выставляем ДО replace,
    чтобы файл ни мгновения не жил с более широкими, чем нужно (mkstemp даёт 0600).
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def system_ca_pem() -> str | None:
    """PEM системного набора доверенных корней (для объединения с CA Vault).

    Берём файл по умолчанию OpenSSL (Debian/Ubuntu: /etc/ssl/certs/ca-
    certificates.crt). None — если файл не найден/не читается: тогда перехват
    НЕ включаем (лучше без перехвата, чем сломать весь TLS урезанным trust-store).
    """
    paths = ssl.get_default_verify_paths()
    candidates = [paths.cafile, paths.openssl_cafile, "/etc/ssl/certs/ca-certificates.crt"]
    for p in candidates:
        if p and os.path.exists(p):
            try:
                text = Path(p).read_text(encoding="utf-8")
            except OSError:
                continue
            if text.strip():
                return text
    return None


def build_ca_bundle(ca: VaultCA) -> str | None:
    """Объединённый trust-bundle: системные корни + корень Vault (в этом порядке).

    None, если системный набор корней не найден: указывать SSL_CERT_FILE ТОЛЬКО на
    CA Vault нельзя — процесс перестал бы доверять всем прочим сертам (api.anthropic
    .com, github…). Системный trust надо СОХРАНИТЬ и лишь ДОБАВИТЬ к нему корень Vault.
    """
    system = system_ca_pem()
    if system is None:
        return None
    return system.rstrip("\n") + "\n" + ca.ca_cert_pem().rstrip("\n") + "\n"


def merge_no_proxy(extra_hosts: Iterable[str] = (), *, existing: str = "") -> str:
    """NO_PROXY для процесса под перехватом: loopback + extra_hosts, слитые с уже
    заданным оператором NO_PROXY (existing). Порядок стабилен, дубли убраны."""
    hosts = list(LOOPBACK_HOSTS)
    for h in extra_hosts:
        if h and h not in hosts:
            hosts.append(h)
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    for h in hosts:
        if h not in parts:
            parts.append(h)
    return ",".join(parts)


def proxy_env_vars(proxy_url: str, ca_path: str, no_proxy: str) -> dict[str, str]:
    """env-довесок процесса под MITM-перехватом.

    HTTP_PROXY НЕ ставим намеренно: прокси обслуживает только CONNECT (HTTPS);
    plain-HTTP через него получил бы 501. SSL_CERT_FILE/REQUESTS_CA_BUNDLE/
    CURL_CA_BUNDLE указывают на объединённый bundle (системный trust + корень Vault),
    а не только на корень Vault — иначе процесс перестал бы доверять прочим сертам.
    """
    return {
        "HTTPS_PROXY": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
        "SSL_CERT_FILE": ca_path,
        "REQUESTS_CA_BUNDLE": ca_path,
        "CURL_CA_BUNDLE": ca_path,
    }


def proxy_sandbox_env(
    ca: VaultCA,
    port: int,
    *,
    bundle_dir: Path,
    bundle_name: str = DEFAULT_BUNDLE_NAME,
    no_proxy: str | None = None,
) -> tuple[dict[str, str], Path, Callable[[], None]] | None:
    """CLI-примитив: записать CA-bundle в bundle_dir и собрать env-довесок перехвата.

    Пишет объединённый trust-bundle (системные корни + корень Vault) в
    bundle_dir/bundle_name (0644, атомарно, БЕЗ следования симлинку) и возвращает:
      * env — HTTPS_PROXY/https_proxy + NO_PROXY/no_proxy + SSL_CERT_FILE/
        REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE (все указывают на записанный bundle);
      * bundle_path — путь записанного файла. Он же путь ВНУТРИ песочницы: вызывающий
        обязан сделать bundle_dir видимым по ЭТОМУ ЖЕ пути внутри изоляции (для bwrap
        — bind src==dst, для engine=off песочницы нет и путь хостовый);
      * cleanup — снять bundle-файл (идемпотентно).

    None, если системный набор корней не найден: перехват включать нельзя
    (SSL_CERT_FILE только на CA Vault сломал бы прочий TLS).
    """
    bundle = build_ca_bundle(ca)
    if bundle is None:
        return None
    bundle_path = bundle_dir / bundle_name
    atomic_write(bundle_path, bundle, 0o644)
    if no_proxy is None:
        no_proxy = merge_no_proxy(
            existing=os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "")
    env = proxy_env_vars(f"http://127.0.0.1:{port}", str(bundle_path), no_proxy)

    def cleanup() -> None:
        with contextlib.suppress(OSError):
            bundle_path.unlink()

    return env, bundle_path, cleanup


__all__ = [
    "DEFAULT_BUNDLE_NAME",
    "LOOPBACK_HOSTS",
    "atomic_write",
    "system_ca_pem",
    "build_ca_bundle",
    "merge_no_proxy",
    "proxy_env_vars",
    "proxy_sandbox_env",
]
