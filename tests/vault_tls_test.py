"""VaultCA — корневой CA переиспользуется, leaf валиден, host строго валидируется.

Фундамент TLS-перехвата (фаза 2, docs/ARCHITECTURE-claude-box.md §4.2). host в
проде приходит из ПЕРЕХВАЧЕННОГО запроса (под контролем сети/модели), поэтому
здесь же — тесты на инъекции в openssl-форматы (SAN-запятая, `${ENV::X}` утечка
окружения, `/` в subj, перевод строки/пробел/пусто/переросток) и на коллизию
кэша (IPv6 c ':'). Цепочку доверия проверяем НАСТОЯЩИМ TLS-хендшейком.

Автономность (без orchestrator) держит соседний vault_domain_test через
walk_packages — vault.tls тянет только stdlib + openssl CLI.

Запуск: .venv/bin/python tests/vault_tls_test.py
"""
import ipaddress
import os
import socket
import ssl
import stat
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.tls import LeafCert, VaultCA, VaultCAError  # noqa: E402


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="vault_ca_"))


def _handshake(ca: VaultCA, host: str, *, family: int, bind_addr: str, server_hostname: str):
    """Поднять ssl-server с leaf(host) + client с CA в trust; вернуть getpeercert().

    Успешный хендшейк доказывает подпись цепочки (leaf ← CA) и совпадение SAN.
    """
    leaf = ca.issue(host)
    srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    srv_ctx.load_cert_chain(certfile=str(leaf.cert_path), keyfile=str(leaf.key_path))

    cli_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    cli_ctx.load_verify_locations(cadata=ca.ca_cert_pem())
    cli_ctx.check_hostname = True
    cli_ctx.verify_mode = ssl.CERT_REQUIRED

    listener = socket.socket(family, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((bind_addr, 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    server_error: list[Exception] = []

    def serve():
        try:
            raw, _ = listener.accept()
            with srv_ctx.wrap_socket(raw, server_side=True) as ss:
                ss.recv(16)
                ss.sendall(b"ok")
        except Exception as exc:  # noqa: BLE001 — прокидываем в основной поток
            server_error.append(exc)

    t = threading.Thread(target=serve)
    t.start()
    peer = None
    try:
        with socket.create_connection((bind_addr, port), timeout=5) as raw:
            with cli_ctx.wrap_socket(raw, server_hostname=server_hostname) as cs:
                cs.sendall(b"ping")
                assert cs.recv(16) == b"ok"
                peer = cs.getpeercert()
    finally:
        t.join(timeout=5)
        listener.close()
    assert not server_error, f"ошибка TLS-сервера (цепочка невалидна?): {server_error}"
    return peer


def test_ca_generated_and_reused():
    """Второй VaultCA на том же каталоге НЕ регенерит корень — тот же CA-серт."""
    d = _tmp()
    ca1 = VaultCA(d)
    pem1 = ca1.ca_cert_pem()
    key1 = ca1.ca_key.read_bytes()
    assert "BEGIN CERTIFICATE" in pem1
    ca2 = VaultCA(d)
    assert ca2.ca_cert_pem() == pem1, "корень регенерирован — trust-store инвалидируется"
    assert ca2.ca_key.read_bytes() == key1, "ключ CA перегенерён"
    print("OK VaultCA: корень генерируется и переиспользуется")


def test_ca_key_perms_0600():
    ca = VaultCA(_tmp())
    mode = stat.S_IMODE(ca.ca_key.stat().st_mode)
    assert mode == 0o600, f"ключ CA имеет права {oct(mode)}, ожидали 0600"
    print("OK VaultCA: ключ корня 0600")


def test_leaf_cached_by_host():
    """issue(host) дважды → тот же leaf (кэш), другой host → другие файлы."""
    ca = VaultCA(_tmp())
    a1 = ca.issue("api.example.com")
    a2 = ca.issue("api.example.com")
    assert a1 is a2, "leaf не закэширован по host"
    b = ca.issue("other.example.com")
    assert b.cert_path != a1.cert_path
    assert stat.S_IMODE(a1.key_path.stat().st_mode) == 0o600
    print("OK VaultCA: leaf кэшируется по host, ключ leaf 0600")


def test_leaf_chain_valid_via_tls_handshake():
    """Живой TLS на DNS-имени: leaf ← CA + SAN=DNS:localhost."""
    ca = VaultCA(_tmp())
    peer = _handshake(ca, "localhost", family=socket.AF_INET,
                      bind_addr="127.0.0.1", server_hostname="localhost")
    sans = {v for typ, v in peer.get("subjectAltName", ()) if typ == "DNS"}
    assert "localhost" in sans, f"SAN не содержит localhost: {peer}"
    print("OK VaultCA: leaf(DNS) подписан корнем — TLS-хендшейк проходит")


def test_leaf_valid_for_ipv4():
    """Живой TLS на IPv4: SAN=IP:127.0.0.1, check_hostname по IP проходит."""
    ca = VaultCA(_tmp())
    peer = _handshake(ca, "127.0.0.1", family=socket.AF_INET,
                      bind_addr="127.0.0.1", server_hostname="127.0.0.1")
    ips = {v for typ, v in peer.get("subjectAltName", ()) if typ == "IP Address"}
    assert "127.0.0.1" in ips, f"SAN не содержит IP 127.0.0.1: {peer}"
    print("OK VaultCA: leaf(IPv4) валиден — TLS-хендшейк по IP проходит")


def test_leaf_valid_for_ipv6():
    """IPv6 (host c ':') — leaf выпускается, имя файла ФС-безопасно, хендшейк ок."""
    if not socket.has_ipv6:
        print("SKIP IPv6 недоступен")
        return
    ca = VaultCA(_tmp())
    leaf = ca.issue("::1")
    assert ":" not in leaf.cert_path.name, f"':' в имени файла кэша: {leaf.cert_path.name}"
    peer = _handshake(ca, "::1", family=socket.AF_INET6,
                      bind_addr="::1", server_hostname="::1")
    # openssl печатает IPv6 в развёрнутом виде — сравниваем нормализованно.
    ips = {ipaddress.ip_address(v) for typ, v in peer.get("subjectAltName", ())
           if typ == "IP Address"}
    assert ipaddress.ip_address("::1") in ips, f"SAN не содержит IP ::1: {peer}"
    print("OK VaultCA: leaf(IPv6) валиден, имя файла без ':', хендшейк проходит")


def test_leaf_reused_from_disk():
    """Новый VaultCA на том же каталоге отдаёт leaf с диска (не переиздаёт)."""
    d = _tmp()
    first = VaultCA(d).issue("cache.example.com")
    first_bytes = first.cert_path.read_bytes()
    second = VaultCA(d).issue("cache.example.com")
    assert isinstance(second, LeafCert)
    assert second.cert_path.read_bytes() == first_bytes, "leaf переиздан вместо кэша с диска"
    print("OK VaultCA: leaf переиспользуется с диска между запусками")


def test_rejects_injection_hosts():
    """Все векторы инъекции в openssl-форматы → VaultCAError, ничего не выпущено."""
    ca = VaultCA(_tmp())
    bad = [
        "attacker.com,DNS:trusted-internal,IP:10.0.0.1",  # SAN-инъекция запятой
        "x${ENV::SOME_SECRET}",                           # NCONF-раскрытие переменной
        "a/b",                                            # разделитель RDN в -subj
        "a\nb",                                           # перевод строки
        "a b",                                            # пробел
        "",                                               # пусто
        "a" * 254,                                        # переросток > 253
        "-lead.example.com",                              # метка с ведущим '-'
        "trail-.example.com",                             # метка с хвостовым '-'
        "пример.рф",                                      # юникод
        "a:b",                                            # ':' у не-IPv6
        "a..b",                                           # пустая метка
    ]
    for host in bad:
        try:
            ca.issue(host)
            raise AssertionError(f"ожидали VaultCAError на host={host!r}")
        except VaultCAError:
            pass
    # Ни одного файла в leaf/ не создано — отказ ДО подстановки/выпуска.
    assert not list(ca.leaf_dir.iterdir()), "инъекционный host всё же что-то записал"
    print("OK VaultCA: инъекционные host отвергнуты, ничего не выпущено")


def test_env_not_leaked_into_cert():
    """host=x${ENV::VAR} НЕ выпускается и значение env НЕ попадает в каталог CA."""
    canary = "vault-ca-env-canary-value"
    os.environ["VAULT_CA_TEST_SECRET"] = canary
    try:
        ca = VaultCA(_tmp())
        try:
            ca.issue("host${ENV::VAULT_CA_TEST_SECRET}")
            raise AssertionError("ожидали VaultCAError на ${ENV::...}")
        except VaultCAError:
            pass
        for p in ca.ca_dir.rglob("*"):
            if p.is_file():
                assert canary.encode() not in p.read_bytes(), f"окружение утекло в {p}"
    finally:
        del os.environ["VAULT_CA_TEST_SECRET"]
    print("OK VaultCA: ${ENV::X} отвергнут, окружение не утекло в серт")


def test_cache_no_collision_distinct_hosts():
    """Разные валидные host → разные файлы кэша (хеш канонизированного host)."""
    ca = VaultCA(_tmp())
    a = ca.issue("a-b.example.com")
    b = ca.issue("a.b.example.com")
    assert a.cert_path != b.cert_path, "разные host делят файл кэша"
    # Канонизация IP: разные записи одного адреса → один leaf.
    if socket.has_ipv6:
        assert ca.issue("::1") is ca.issue("0:0:0:0:0:0:0:1"), "IPv6 не канонизирован в кэше"
    print("OK VaultCA: имена кэша без коллизий, IP канонизируется")


def main():
    test_ca_generated_and_reused()
    test_ca_key_perms_0600()
    test_leaf_cached_by_host()
    test_leaf_chain_valid_via_tls_handshake()
    test_leaf_valid_for_ipv4()
    test_leaf_valid_for_ipv6()
    test_leaf_reused_from_disk()
    test_rejects_injection_hosts()
    test_env_not_leaked_into_cert()
    test_cache_no_collision_distinct_hosts()
    print("ALL VAULT-TLS OK")


if __name__ == "__main__":
    main()
