"""VaultCA — центр сертификации Vault для TLS-перехвата (MITM) под песочницей.

Фундамент фазы 2 редизайна (docs/ARCHITECTURE-claude-box.md §4.2): чтобы
терминировать TLS внутри Vault-прокси, нам нужен собственный корневой CA (его
серт кладётся в trust-store песочницы) и leaf-серты, выпускаемые на лету под
каждый перехватываемый host. Здесь ТОЛЬКО CA-слой: генерация/переиспользование
корня и выпуск leaf. Сам MITM-цикл прокси — следующий срез.

Реализация — через `openssl` CLI (subprocess), БЕЗ пакета `cryptography`:
  * репо ставится клоном (install.sh), тяжёлые бинарные зависимости нежелательны;
  * `openssl` есть на хосте (как и у остального домена vault — stdlib-only);
  * автономность vault/ сохраняется тривиально: ни одного импорта оркестратора.

Корневой CA переиспользуется между запусками (ключ+серт лежат на диске): если
регенерить его каждый раз, trust-store песочницы мгновенно инвалидируется.
Leaf-серты кэшируются по host (в памяти и на диске) — не выпускаем на каждый
коннект. Ключи (CA и leaf) хранятся с правами 0600, каталог CA — 0700.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Срок жизни: корень — долгий (реже трогаем trust-store), leaf — короткий (это
# эфемерные серты под MITM конкретного host, кэшируются и переиздаются свободно).
_CA_DAYS = 3650
_LEAF_DAYS = 30
_KEY_BITS = 2048
# Таймаут openssl: MITM-путь синхронный, зависший openssl не должен вешать issue().
_OPENSSL_TIMEOUT = 30
# Максимальная длина DNS-имени (RFC 1035), без учёта опц. trailing dot.
_MAX_DNS_LEN = 253
_MAX_LABEL_LEN = 63


def _default_ca_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "claude-orchestrator" / "ca"


def _run(args: list[str]) -> None:
    """Запустить openssl, подняв понятную ошибку с stderr при сбое."""
    try:
        r = subprocess.run(
            ["openssl", *args],
            capture_output=True,
            text=True,
            timeout=_OPENSSL_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise VaultCAError(f"openssl {args[0]} завис (>{_OPENSSL_TIMEOUT}s)") from exc
    if r.returncode != 0:
        raise VaultCAError(
            f"openssl {args[0]} завершился с кодом {r.returncode}:\n{r.stderr.strip()}"
        )


def _validate_host(host: str) -> tuple[str, str]:
    """Строго провалидировать host ДО любой подстановки в openssl-форматы.

    host в проде приходит из ПЕРЕХВАЧЕННОГО запроса (§4.2 — под контролем сети/
    модели), поэтому подстановка в SAN/subj/NCONF-extfile без валидации — вектор
    инъекции (запятая в SAN, `${ENV::X}` раскрытие переменных, `/` в subj).

    Принимаем ТОЛЬКО: валидный IP (канонизируем через ipaddress) ЛИБО валидное
    DNS-имя (≤253; метки 1–63 из [A-Za-z0-9-], не начинать/кончать `-`; опц.
    trailing dot). Всё прочее (запятая, `$`, `{}`, `/`, `\\n`, пробел, юникод,
    пусто, `:` кроме IPv6) → VaultCAError. Возвращает (kind, canonical).
    """
    if not host or not isinstance(host, str):
        raise VaultCAError("пустой host")
    # IP (в т.ч. IPv6 с ':') — канонизируем, чтобы разные записи одного адреса
    # не плодили разные leaf и совпадали в кэше.
    try:
        return ("ip", str(ipaddress.ip_address(host)))
    except ValueError:
        pass
    name = host[:-1] if host.endswith(".") else host  # опц. trailing dot
    if not name or len(name) > _MAX_DNS_LEN:
        raise VaultCAError(f"host не валидное DNS-имя (длина): {host!r}")
    labels = name.split(".")
    for label in labels:
        if not (1 <= len(label) <= _MAX_LABEL_LEN):
            raise VaultCAError(f"host: недопустимая длина метки в {host!r}")
        if label[0] == "-" or label[-1] == "-":
            raise VaultCAError(f"host: метка начинается/кончается на '-' в {host!r}")
        if not all(c.isascii() and (c.isalnum() or c == "-") for c in label):
            raise VaultCAError(f"host: недопустимый символ в {host!r}")
    return ("dns", host)


def _san_entry(kind: str, value: str) -> str:
    """SAN-запись из УЖЕ провалидированного host: IP:… либо DNS:…."""
    return f"IP:{value}" if kind == "ip" else f"DNS:{value}"


def _cache_name(canonical: str) -> str:
    """Имя файла кэша БЕЗ потерь: хеш канонизированного host + читаемый префикс.

    Хеш гарантирует, что разные host НИКОГДА не делят файл (IPv6 содержит ':',
    негодный для имени файла). Префикс — только для читаемости каталога.
    """
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    prefix = "".join(c if c.isalnum() or c in ".-" else "_" for c in canonical)[:40]
    return f"{prefix}-{digest}"


class VaultCAError(RuntimeError):
    """Сбой при валидации/генерации/выпуске сертификата."""


@dataclass(frozen=True)
class LeafCert:
    """Выпущенный leaf: пути к серту и ключу (для ssl.load_cert_chain в прокси)."""

    host: str
    cert_path: Path
    key_path: Path

    def cert_pem(self) -> str:
        return self.cert_path.read_text()

    def key_pem(self) -> str:
        return self.key_path.read_text()


class VaultCA:
    """Корневой CA Vault + выпуск leaf-сертов под MITM.

    Каталог хранит: ca.key (0600), ca.crt (0644), ca.srl (серийники), leaf/<slug>.{crt,key}.
    Корень создаётся при первом обращении и переиспользуется; leaf кэшируются по host.
    """

    def __init__(self, ca_dir: Path | str | None = None) -> None:
        self.ca_dir = Path(ca_dir) if ca_dir is not None else _default_ca_dir()
        self.ca_key = self.ca_dir / "ca.key"
        self.ca_cert = self.ca_dir / "ca.crt"
        self.leaf_dir = self.ca_dir / "leaf"
        self._leaf_cache: dict[str, LeafCert] = {}
        self._ensure_ca()

    # --- корень ------------------------------------------------------------

    def _ensure_ca(self) -> None:
        self.ca_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.ca_dir, 0o700)
        self.leaf_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.leaf_dir, 0o700)
        # Уже есть корень — переиспользуем (НЕ регенерим: иначе trust-store битый).
        if self.ca_key.exists() and self.ca_cert.exists():
            return
        _run([
            "req", "-x509", "-newkey", f"rsa:{_KEY_BITS}", "-nodes",
            "-keyout", str(self.ca_key),
            "-out", str(self.ca_cert),
            "-days", str(_CA_DAYS),
            "-subj", "/CN=Vault CA/O=claude-orchestrator",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        ])
        os.chmod(self.ca_key, 0o600)
        os.chmod(self.ca_cert, 0o644)
        logger.info("VaultCA: создан корневой CA в %s", self.ca_dir)

    def ca_cert_pem(self) -> str:
        """PEM корневого серта — для укладки в trust-store песочницы."""
        return self.ca_cert.read_text()

    # --- leaf --------------------------------------------------------------

    def issue(self, host: str) -> LeafCert:
        """Выпустить (или отдать из кэша) leaf-серт на host, подписанный корнем.

        host валидируется СТРОГО ДО любой подстановки (см. _validate_host); всё
        подозрительное → VaultCAError, при этом НИЧЕГО не выпускается/не пишется.
        Кэш и имя файла — по канонизированному host.
        """
        kind, canonical = _validate_host(host)

        cached = self._leaf_cache.get(canonical)
        if cached is not None:
            return cached

        name = _cache_name(canonical)
        cert_path = self.leaf_dir / f"{name}.crt"
        key_path = self.leaf_dir / f"{name}.key"

        # На диске уже есть — переиспользуем (стабильный серт между рестартами).
        if cert_path.exists() and key_path.exists():
            leaf = LeafCert(host=canonical, cert_path=cert_path, key_path=key_path)
            self._leaf_cache[canonical] = leaf
            return leaf

        with tempfile.TemporaryDirectory(prefix="vault_leaf_") as td:
            tmp = Path(td)
            csr = tmp / "leaf.csr"
            ext = tmp / "leaf.ext"
            ext.write_text(
                f"subjectAltName={_san_entry(kind, canonical)}\n"
                "basicConstraints=critical,CA:FALSE\n"
                "keyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\n"
            )
            _run([
                "req", "-new", "-newkey", f"rsa:{_KEY_BITS}", "-nodes",
                "-keyout", str(key_path),
                "-out", str(csr),
                "-subj", f"/CN={canonical}",
            ])
            _run([
                "x509", "-req", "-in", str(csr),
                "-CA", str(self.ca_cert), "-CAkey", str(self.ca_key),
                "-CAcreateserial",
                "-days", str(_LEAF_DAYS),
                "-out", str(cert_path),
                "-extfile", str(ext),
            ])
        os.chmod(key_path, 0o600)
        os.chmod(cert_path, 0o644)

        leaf = LeafCert(host=canonical, cert_path=cert_path, key_path=key_path)
        self._leaf_cache[canonical] = leaf
        logger.info("VaultCA: выпущен leaf на %s", canonical)
        return leaf
