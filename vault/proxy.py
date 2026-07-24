"""VaultProxy — HTTP-forward-прокси с MITM-перехватом HTTPS (§4.2 «произвольный
клиент → MITM-прокси: расшифрованный запрос»). Значение секрета в машину НЕ
входит: прокси подставляет кред МЕЖДУ машиной и сервисом (§4.4, тир «дефолт»).

Автономно: только stdlib + vault.* (ни строки orchestrator — держится
vault_domain_test через walk_packages). Транспорт — asyncio (как daemon.py).

Границы ЭТОГО среза (2.3): ОДИН коннектор + ОДИН секрет + ОДИН scope,
standalone-компонент. Маршрутизация per-session (свой listen-порт на сессию,
§4.3) и интеграция в демон/лончер — следующий срез (2.4), здесь НЕ делается.

Поток на соединение:
  1. Клиент шлёт `CONNECT host:port` (обычный HTTPS-через-прокси). Отвечаем
     `200 Connection established` по plaintext, затем ТЕРМИНИРУЕМ TLS на своей
     стороне leaf-сертом `VaultCA.issue(host)` (server-side `start_tls`). Клиент
     доверяет, т.к. корень Vault лежит у него в trust-store (§4.2).
  2. Читаем расшифрованный HTTP-запрос → собираем `HttpReq` (method, реальный
     url=`https://host/path`, headers, body).
  3. Если host — «свой» для секрета (входит в service_hosts): коннектор решает
     скоуп `in_scope(req, scope)`:
       * ALLOW → `authorize(req, secret)` подставляет кред (значение НЕ логируем)
         и РЕОРИГИНИРУЕМ: TLS-коннект к РЕАЛЬНОМУ host (обычный trust, НЕ наш
         CA), шлём авторизованный запрос, стримим ответ обратно клиенту.
       * DENY → отвечаем 403 с `reason`+`remedy` в теле (Р0: предписывающе), к
         сервису НЕ ходим.
       * ASK → поднимаем спрос гранта у оператора через `host.ask` (§4.6):
         True → продолжаем как ALLOW (authorize+реоригин); False/некому/таймаут →
         403 с предписывающим remedy. Спрос ПОД таймаутом (`ask_timeout`) — Р0
         «никогда не повисать»: реализация host.ask обязана иметь свой таймаут, но
         прокси дополнительно страхует wait_for'ом. Без host (standalone-сборка
         2.3) ASK трактуется как DENY, как раньше. См. _ask_grant/_respond_ask.
  4. host НЕ покрыт секретом → обычный форвард БЕЗ инъекции (§4.4 «путь открыт,
     но пуст»): реоригинируем как есть, сервис сам вернёт 401. Egress сверх
     scope-DENY не режем (Р7).

Про «не зависать» (урок docker-прокси: обработчик оставался pending при обрыве):
  * все чтения — под `asyncio.wait_for`; backpressure на запись — по РАЗМЕРУ
    write-буфера транспорта (`_drain`), а НЕ по `drain()`-callbacks: не-читающий
    клиент иначе устроил бы resource-hang/DoS, а на 3.10 после ручного TLS-
    reattach flow-control-callbacks вообще не доезжают (см. `_drain`);
  * закрытие — через `_hard_close` (`close()` без ожидания `wait_closed()`, а при
    непустом буфере — `transport.abort()`): ожидание `wait_closed()` зависало бы на
    недосливаемом буфере, а на 3.10 после провала start_tls и вовсе бессрочно;
  * реориджин ОДНОНАПРАВЛЕННЫЙ: запрос дослан целиком, затем качаем upstream→
    client до EOF; upstream принуждаем к EOF заголовком `Connection: close`, так
    не нужно парсить фрейминг ответа и нет висящего keep-alive;
  * обе стороны закрываются в finally; обрыв любой стороны роняет качалку и
    закрывает встречную (нет осиротевшей задачи).

Про server-side TLS-апгрейд: делаем его через кросс-версионный `_upgrade_tls`,
т.к. `StreamWriter.start_tls` появился только в 3.11, а проект держит
`requires-python >=3.10`. На 3.11+ — нативный метод (сам выводит server_side из
протокола); на 3.10 — нижний `loop.start_tls(transport, protocol, ctx,
server_side=True, …)` (есть с 3.7) под тем же StreamReaderProtocol, так что
reader продолжает читать. Реориджин к сервису — обычный `open_connection(ssl=…)`,
TLS сразу на коннекте, без ручного апгрейда. Выбор asyncio (а не поток-на-
соединение) оправдан: single-loop без блокировок, таймауты на каждом await.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from contextlib import suppress
from dataclasses import replace
from urllib.parse import urlsplit

from .connectors import HttpReq
from .connectors.contract import Connector, ScopeGrant, ScopeVerdict
from .host import VaultHost, ask_grant, deny_remedy
from .secret import Secret
from .store import SecretStore
from .tls import VaultCA, VaultCAError

logger = logging.getLogger(__name__)

# Таймауты (сек): держат прокси от зависаний на медленном/оборванном пире.
_CONNECT_TIMEOUT = 30      # приём CONNECT-строки и коннект к upstream
_HANDSHAKE_TIMEOUT = 30    # MITM-хендшейк с клиентом
_READ_TIMEOUT = 60         # чтение одного запроса (строка+заголовки+тело)
_IDLE_TIMEOUT = 300        # застой (нет прогресса слива буфера) → рвём
_ASK_TIMEOUT = 180         # страховочный потолок ASK-спроса у host (Р0: не висеть,
                           # даже если host.ask забыл свой таймаут → дефолт DENY)
# Интервал опроса размера write-буфера в backpressure. Опрашиваем ТОЛЬКО пока
# буфер выше high-water (при нормальном темпе — ноль опросов), поэтому 50мс не
# создают busy-loop и достаточно мелкозернисты для стрима.
_DRAIN_POLL = 0.05
# Фолбэк high-water, если транспорт не отдаёт лимиты.
_DRAIN_HIGH_FALLBACK = 256 * 1024
# Потолки: защита от заливки памяти гигабайтами (как STREAM_LIMIT в redact).
_MAX_LINE = 64 * 1024
_MAX_HEADERS = 200
_MAX_BODY = 32 * 1024 * 1024
_CHUNK = 64 * 1024

# Заголовки хоп-бай-хоп/фрейминга: снимаем перед реориджином, выставляем свои
# (Content-Length по факту + Connection: close, чтобы upstream дал EOF).
_HOP_HEADERS = frozenset({
    "connection", "proxy-connection", "keep-alive", "transfer-encoding",
    "content-length", "te", "trailer", "upgrade", "proxy-authenticate",
    "proxy-authorization",
})


class VaultProxyError(RuntimeError):
    """Сбой конфигурации/парсинга на уровне прокси."""


def _hosts_from_scope(scope: dict) -> set[str]:
    """Хосты, которыми «владеет» секрет — из url_prefixes его scope. Запрос к
    такому хосту обязан быть в скоупе (иначе DENY); к прочим — сквозной форвард."""
    hosts: set[str] = set()
    for pref in scope.get("url_prefixes") or []:
        host = urlsplit(pref).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def _split_authority(authority: str) -> tuple[str, int]:
    """`host:port` из CONNECT → (host, port). Поддержка `[::1]:443` (IPv6)."""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest.lstrip(":") or "443"
    else:
        host, _, port = authority.partition(":")
        port = port or "443"
    try:
        return host, int(port)
    except ValueError as exc:
        raise VaultProxyError(f"нечисловой порт в CONNECT {authority!r}") from exc


async def _upgrade_tls(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ctx: ssl.SSLContext,
    *,
    server_side: bool,
    server_hostname: str | None = None,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """TLS-апгрейд УЖЕ установленного plaintext-соединения — совместимо с 3.10+.

    `StreamWriter.start_tls` появился только в 3.11, а проект держит
    `requires-python >=3.10` (CI гоняет 3.10). На 3.11+ зовём нативный метод (он
    сам выводит server_side из протокола и переносит writer). На 3.10 — нижний
    `loop.start_tls(transport, protocol, ctx, server_side=…, server_hostname=…)`
    (есть с 3.7): StreamReaderProtocol ПЕРЕИСПОЛЬЗУЕТСЯ (loop.start_tls сохраняет
    его), поэтому reader продолжает получать данные через тот же protocol; нам
    остаётся указать writer на новый TLS-транспорт.

    server_side=True — термируем клиента leaf-сертом (прокси); server_side=False
    (+ server_hostname) — клиентская сторона. Возвращает (reader, writer) поверх
    TLS. NB: 3.10-путь на этой машине не проверить (тут 3.12) — верификация в CI.
    """
    if hasattr(writer, "start_tls"):  # 3.11+: нативный, сам переносит writer
        await asyncio.wait_for(
            writer.start_tls(ctx, server_hostname=server_hostname), timeout
        )
        return reader, writer
    # 3.10: апгрейд транспорта под тем же StreamReaderProtocol. Повторяем то, что
    # нативный start_tls делает через _replace_writer (в 3.10 его нет). Важно:
    # loop.start_tls зовёт SSLProtocol с call_connection_made=False, поэтому
    # protocol.connection_made ПОВТОРНО НЕ вызывается — нет двойного запуска
    # client_connected_cb на server-side, а данные и так идут через data_received
    # того же protocol → reader продолжает читать.
    loop = asyncio.get_running_loop()
    protocol = writer._protocol  # noqa: SLF001 — у StreamWriter нет публичного геттера
    transport = writer.transport
    await asyncio.wait_for(writer.drain(), timeout)  # дослать буфер до хендшейка
    new_transport = await asyncio.wait_for(
        loop.start_tls(
            transport, protocol, ctx,
            server_side=server_side, server_hostname=server_hostname,
        ),
        timeout,
    )
    # Перевести writer И protocol на новый TLS-транспорт (как _replace_writer).
    writer._transport = new_transport  # noqa: SLF001 — кросс-версионный шов
    protocol._transport = new_transport  # noqa: SLF001
    protocol._over_ssl = True  # noqa: SLF001
    return reader, writer


class VaultProxy:
    """MITM-forward-прокси под ОДИН секрет/коннектор/scope (срез 2.3).

    Параметры:
      * ca — VaultCA (корень в trust-store клиента; issue(host) даёт leaf);
      * connector — плагин сервиса (authorize/in_scope);
      * secret — секрет, чей кред подставляем на ALLOW;
      * scope — машинный скоуп (для generic-bearer: {"url_prefixes": [...]}). Это
        СТАРТОВЫЙ снимок; при заданном `store` scope на каждом запросе берётся
        свежим из secrets.toml (см. store + _current_scope), snapshot остаётся
        лишь fail-safe-фолбэком на случай ошибки чтения файла;
      * store — общий SecretStore (тот же, что у пула/демона): источник СВЕЖЕГО
        scope секрета. С ним прокси перечитывает scope по mtime/size (дёшево —
        кэш store), поэтому грант, записанный ДРУГИМ живым прокси, и операторский
        отзыв в файле действуют на лету, без перезапуска (§4.6). None → scope
        статичен (standalone-сборка/тесты) — прежнее поведение;
      * host — VaultHost для ASK-спроса гранта (§4.6); None → ASK трактуется как
        DENY (standalone-сборка 2.3, спрашивать некому);
      * session_name — имя сессии, от чьего лица спрашиваем host.ask (прокси —
        per-session, §4.3); при host=None не используется;
      * service_hosts — хосты, которыми владеет секрет; None → вывести из scope
        (при `store` — из СВЕЖЕГО scope на каждом запросе);
      * upstream_ssl — SSLContext для коннекта к РЕАЛЬНОМУ сервису (обычный
        trust); None → системный default. В тесте сюда кладут trust к локальному
        «сервису» (его серт), чтобы реориджин ему доверял;
      * bind_host/bind_port — где слушать (0 = эфемерный).
    """

    def __init__(
        self,
        ca: VaultCA,
        connector: Connector,
        secret: Secret,
        scope: dict,
        *,
        store: SecretStore | None = None,
        host: VaultHost | None = None,
        session_name: str = "",
        service_hosts: set[str] | None = None,
        upstream_ssl: ssl.SSLContext | None = None,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
        connect_timeout: float = _CONNECT_TIMEOUT,
        handshake_timeout: float = _HANDSHAKE_TIMEOUT,
        read_timeout: float = _READ_TIMEOUT,
        idle_timeout: float = _IDLE_TIMEOUT,
        ask_timeout: float = _ASK_TIMEOUT,
    ) -> None:
        self.ca = ca
        self.connector = connector
        self.secret = secret
        self.scope = scope
        self._store = store
        # Последний ВАЛИДНЫЙ scope секрета — fail-safe-фолбэк для динамического
        # режима: если secrets.toml в момент чтения недоступен/битый, держим его
        # (ошибка чтения НЕ должна ни ронять сессию, ни расширять доступ). Стартово
        # — снимок из конструктора (то же, что видит статический режим).
        self._last_valid_scope = dict(scope)
        self.host = host
        self.session_name = session_name
        # service_hosts либо ЗАФИКСИРОВАНЫ коннектором (gdocs и пр. знают свой набор
        # хостов независимо от scope), либо выводятся из url_prefixes scope. Во
        # втором случае при динамическом scope их надо пересчитывать (грант на новый
        # хост / отзыв меняют набор) — см. _current_service_hosts.
        self._service_hosts_fixed = service_hosts is not None
        self.service_hosts = (
            {h.lower() for h in service_hosts}
            if service_hosts is not None
            else _hosts_from_scope(scope)
        )
        self.upstream_ssl = (
            upstream_ssl if upstream_ssl is not None else ssl.create_default_context()
        )
        self.bind_host = bind_host
        self.bind_port = bind_port
        # Таймауты — атрибуты (не только модульные константы): тесты подставляют
        # малые значения, чтобы проверить размыкание по таймауту без 5-мин ожиданий.
        self._connect_timeout = connect_timeout
        self._handshake_timeout = handshake_timeout
        self._read_timeout = read_timeout
        self._idle_timeout = idle_timeout
        self._ask_timeout = ask_timeout
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        # Кэш server-SSLContext по host (leaf грузим с диска один раз на host).
        self._leaf_ctx: dict[str, ssl.SSLContext] = {}

    # --- жизненный цикл ----------------------------------------------------

    async def start(self) -> int:
        """Поднять listener, вернуть фактический порт."""
        self._server = await asyncio.start_server(
            self._handle_client, self.bind_host, self.bind_port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("VaultProxy слушает на %s:%s", self.bind_host, self.port)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    @property
    def proxy_url(self) -> str:
        if self.port is None:
            raise VaultProxyError("прокси не запущен (нет порта)")
        return f"http://{self.bind_host}:{self.port}"

    async def __aenter__(self) -> VaultProxy:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # --- обработка соединения ---------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._serve(reader, writer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass  # клиент оборвался / молчит — просто закрываемся
        except Exception:  # noqa: BLE001 — прокси не должен падать на одном коннекте
            logger.exception("VaultProxy: необработанная ошибка на соединении")
        finally:
            self._hard_close(writer)

    async def _drain(self, writer: asyncio.StreamWriter) -> None:
        """Backpressure, НЕ зависящая от `drain()`-callbacks (кросс-версионно).

        Не-читающий пир заполняет write-буфер; полагаться на `StreamWriter.drain()`
        нельзя: на 3.10 после ручного TLS-reattach (loop.start_tls) старый
        SSLProtocol НЕ пробрасывает pause/resume_writing в app-протокол, поэтому
        `drain()` не блокируется и не размыкается (нативный start_tls 3.11+ это
        чинит, но проекту нужен 3.10). Зато `transport.get_write_buffer_size()`
        отдаёт реальный размер буфера на ОБЕИХ версиях — по нему и держим
        backpressure: ждём, пока буфер не опустится ниже high-water, но НЕ дольше
        idle_timeout БЕЗ ПРОГРЕССА (буфер не убывает). Застой → TimeoutError вверх
        → соединение рвётся (как раньше), без бесконечного роста памяти."""
        transport = writer.transport
        try:
            high = transport.get_write_buffer_limits()[1] or _DRAIN_HIGH_FALLBACK
        except Exception:  # noqa: BLE001 — не все транспорты отдают лимиты
            high = _DRAIN_HIGH_FALLBACK
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._idle_timeout
        last: int | None = None
        while True:
            try:
                size = transport.get_write_buffer_size()
            except Exception:  # noqa: BLE001 — транспорт закрылся → считаем слитым
                return
            if size <= high:
                return
            if last is not None and size < last:
                deadline = loop.time() + self._idle_timeout  # есть прогресс — продлеваем
            last = size
            if loop.time() >= deadline:
                raise asyncio.TimeoutError  # застой пира: рвём соединение
            await asyncio.sleep(_DRAIN_POLL)

    def _hard_close(self, writer: asyncio.StreamWriter) -> None:
        """Закрыть соединение, НЕ зависая. `wait_closed()` НЕ ждём вовсе: (а) для
        живого пира `close()` и так дошлёт мелкий буфер в ОС и отправит FIN, а
        доигрывание закрытия доведёт сам loop — держать на этом задачу-обработчик
        незачем; (б) на 3.10 после ПРОВАЛА server-side start_tls close-waiter
        StreamReaderProtocol может не резолвиться (connection_lost ушёл в
        SSLProtocol после set_protocol) → `wait_closed()` завис бы бессрочно,
        оставляя залипшую задачу. Если же в буфере ОСТАЛИСЬ данные (не-читающий
        пир — застой), они бы висели недосливаемыми: рвём `transport.abort()`,
        освобождая сокет/память. Для живого пира буфер уже пуст → abort не тронет."""
        transport = writer.transport
        with suppress(Exception):
            writer.close()
        with suppress(Exception):
            if transport.get_write_buffer_size() > 0:
                transport.abort()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        line = await asyncio.wait_for(
            reader.readuntil(b"\r\n"), timeout=self._connect_timeout
        )
        parts = line.rstrip(b"\r\n").decode("latin-1").split()
        if len(parts) < 2:
            await self._send_response(writer, 400, "Bad Request", b"malformed request line\n")
            return
        method, authority = parts[0].upper(), parts[1]
        if method != "CONNECT":
            # Форвард plain-HTTP absolute-form — вне этого среза (сервисы под
            # секретом всегда HTTPS/CONNECT). Явный, не молчаливый отказ.
            await self._send_response(
                writer, 501, "Not Implemented",
                b"VaultProxy build handles only CONNECT (HTTPS) tunnels\n",
            )
            return
        await self._drain_headers(reader)  # дочитать заголовки CONNECT до пустой

        try:
            host, port = _split_authority(authority)
        except VaultProxyError:
            await self._send_response(writer, 400, "Bad Request", b"bad CONNECT target\n")
            return

        # leaf под host: невалидный host → VaultCAError → 502, не падаем.
        try:
            leaf_ctx = self._leaf_context(host)
        except VaultCAError as exc:
            logger.warning("VaultProxy: не выпустить leaf на %r: %s", host, exc)
            await self._send_response(
                writer, 502, "Bad Gateway",
                f"VaultProxy: cannot issue certificate for host: {exc}\n".encode(),
            )
            return

        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await self._drain(writer)

        # Апгрейд клиентской стороны в TLS server-side (leaf-серт host). Через
        # кросс-версионный _upgrade_tls (нативный start_tls только с 3.11).
        try:
            reader, writer = await _upgrade_tls(
                reader, writer, leaf_ctx,
                server_side=True, timeout=self._handshake_timeout,
            )
        except (ssl.SSLError, asyncio.TimeoutError, ConnectionError) as exc:
            logger.info("VaultProxy: MITM-хендшейк с клиентом не удался (%s): %s", host, exc)
            return

        await self._serve_decrypted(reader, writer, host, port, authority)

    # --- живой (перечитываемый) scope секрета ------------------------------

    def _current_scope(self) -> dict:
        """АКТУАЛЬНЫЙ scope секрета на момент запроса.

        Без store — статичный снимок `self.scope` (standalone/тесты, прежнее
        поведение). Со store — свежий scope секрета из secrets.toml: `store.load()`
        дёшев (кэш по mtime/mode/size — диск не читается, если файл не менялся) и
        перечитывает файл, если он изменился. Так грант, дописанный в файл ДРУГИМ
        живым прокси того же секрета, и операторский отзыв (сужение scope) действуют
        на лету, без перезапуска этого прокси.

        Fail-safe (Р0/безопасность — ошибка чтения НЕ должна ни ронять сессию, ни
        расширять доступ):
          * `store.load()` бросил / файл недоступен / права шире 0600 / битый TOML
            (`last_load_ok == False`) → держим ПОСЛЕДНИЙ ВАЛИДНЫЙ scope. Это не
            расширение (тот же уже-разрешённый набор), а транзиентная ошибка чтения
            не рвёт живую работу. Осознанное следствие: правку, сделанную в БИТЫЙ
            файл, прокси не подхватит — но операторский отзыв идёт валидной
            атомарной записью (PolicyEditor), которую мы читаем чисто.
          * файл валиден, но секрета в нём больше нет / сменился connector / он
            перестал быть прокси-секретом → honest degrade: ПУСТОЙ scope. Не роняем
            прокси, но и не пускаем по старому снимку — исчезновение секрета в
            валидном файле это намеренное состояние; пустой scope → его хосты
            перестают быть «своими» (см. _current_service_hosts) → кред не
            подставляется (сквозной форвард без инъекции, сервис сам вернёт 401).
        """
        if self._store is None:
            return self.scope
        try:
            secrets = self._store.load()
        except Exception:  # noqa: BLE001 — чтение policy не должно ронять прокси
            logger.warning(
                "VaultProxy: store.load() упал — держу последний валидный scope "
                "(секрет %s)", self.secret.name)
            return self._last_valid_scope
        # last_load_ok есть у настоящего SecretStore всегда; in-memory-двойник
        # (тест/standalone), который отдаёт лишь валидные данные и не знает ошибок
        # чтения, атрибута может не иметь → трактуем как «прочитано успешно».
        if not getattr(self._store, "last_load_ok", True):
            logger.warning(
                "VaultProxy: policy недоступна/битая — держу последний валидный "
                "scope (секрет %s, fail-safe)", self.secret.name)
            return self._last_valid_scope
        fresh = secrets.get(self.secret.name)
        if (fresh is None or not fresh.is_proxy
                or fresh.connector != self.connector.name):
            logger.info(
                "VaultProxy: секрет %r исчез/сменил connector в валидной policy → "
                "scope пуст (honest degrade, доступ не расширяем)", self.secret.name)
            return {}
        self._last_valid_scope = dict(fresh.scope)
        return self._last_valid_scope

    def _current_service_hosts(self, scope: dict) -> set[str]:
        """Хосты, которыми «владеет» секрет, для ТЕКУЩЕГО scope. Зафиксированные
        коннектором (gdocs) — как есть. Выведенные из url_prefixes — статичны без
        store (их доращивает _apply_grant), но при динамическом scope
        пересчитываются из свежего scope (грант добавил хост / отзыв убрал)."""
        if self._service_hosts_fixed or self._store is None:
            return self.service_hosts
        return _hosts_from_scope(scope)

    async def _serve_decrypted(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
        authority: str,
    ) -> None:
        method, target, headers, body = await self._read_request(reader)
        # Маршрут — ТОЛЬКО по CONNECT-authority. Валидный запрос в туннеле —
        # origin-form (`/path`). Absolute-form (`GET https://evil/…`) или
        # authority-form — попытка увести реориджин/скоуп на чужой ресурс мимо
        # authority: fail-closed 400, к in_scope мусорный url не пускаем (иначе
        # urlsplit(url).port на битом порту бросал бы ValueError → сырой обрыв).
        if not target.startswith("/"):
            await self._send_response(
                writer, 400, "Bad Request",
                b"VaultProxy: only origin-form request targets are accepted; "
                b"the tunnel routes by the CONNECT authority, not by an "
                b"absolute-form URL or Host header\n",
            )
            return
        url = f"https://{authority}{target}"
        req = HttpReq(method=method, url=url, headers=dict(headers), body=body)

        # Свежий scope и производные от него хосты берём ОДИН раз на запрос: со
        # store это перечитывание secrets.toml по mtime (грант другого прокси и
        # операторский отзыв действуют на лету), без store — статичный снимок.
        scope = self._current_scope()
        if host.lower() in self._current_service_hosts(scope):
            verdict = self.connector.in_scope(req, scope)
            if verdict.is_deny:
                await self._respond_deny(writer, verdict)
                return
            if verdict.is_ask:
                # ASK: спрашиваем грант у оператора через host (§4.6). Отказ/
                # некому/таймаут → 403, к сервису НЕ идём. Грант → как ALLOW ниже.
                granted = await self._ask_grant(verdict, method, url)
                if not granted:
                    await self._respond_ask(writer, verdict)
                    return
                logger.info("VaultProxy: ASK→грант %s %s (кред подставлен)", method, url)
            # ALLOW или гранто-ASK — подставляем кред (значение в лог НЕ попадает).
            req = self.connector.authorize(req, self.secret)
            if verdict.is_allow:
                logger.info("VaultProxy: ALLOW %s %s (кред подставлен)", method, url)
        else:
            logger.info("VaultProxy: сквозной форвард %s %s (без кред)", method, url)

        await self._reoriginate(writer, host, port, method, target, req.headers, req.body)

    # --- реориджин к реальному сервису ------------------------------------

    async def _reoriginate(
        self,
        client_writer: asyncio.StreamWriter,
        host: str,
        port: int,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, ssl=self.upstream_ssl, server_hostname=host
                ),
                timeout=self._connect_timeout,
            )
        except (ssl.SSLError, OSError, asyncio.TimeoutError) as exc:
            # Ошибку логируем/отдаём БЕЗ заголовков (там может быть кред).
            logger.warning("VaultProxy: коннект к сервису %s:%s не удался: %s", host, port, exc)
            await self._send_response(
                client_writer, 502, "Bad Gateway",
                f"VaultProxy: upstream connection failed: {exc}\n".encode(),
            )
            return
        try:
            up_writer.write(self._build_request(method, target, headers, body))
            await self._drain(up_writer)
            # Однонаправленная качалка: upstream принуждён к EOF (Connection:close),
            # поэтому дойдём до конца без парсинга фрейминга ответа и не зависнем.
            await self._pump(up_reader, client_writer)
        finally:
            self._hard_close(up_writer)

    async def _pump(
        self, src: asyncio.StreamReader, dst: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                chunk = await asyncio.wait_for(src.read(_CHUNK), timeout=self._idle_timeout)
                if not chunk:
                    break
                dst.write(chunk)
                # Backpressure по размеру буфера (не по drain-callbacks): если
                # КЛИЕНТ перестал читать большой ответ, _drain разомкнётся по
                # застою и качалка прекратится — без роста памяти и без зависания.
                await self._drain(dst)
        except (asyncio.TimeoutError, ssl.SSLError, OSError):
            pass  # любая сторона отвалилась — прекращаем, finally закроет сокеты

    # --- парсинг запроса ---------------------------------------------------

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str], bytes]:
        line = await asyncio.wait_for(
            reader.readuntil(b"\r\n"), timeout=self._read_timeout
        )
        fields = line.rstrip(b"\r\n").decode("latin-1").split(" ", 2)
        if len(fields) < 2:
            raise VaultProxyError("битая строка запроса")
        method, target = fields[0], fields[1]
        headers = await self._read_headers(reader)
        body = await self._read_body(reader, headers)
        return method, target, headers, body

    async def _read_headers(self, reader: asyncio.StreamReader) -> dict[str, str]:
        headers: dict[str, str] = {}
        for _ in range(_MAX_HEADERS):
            hl = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=self._read_timeout)
            if hl in (b"\r\n", b"\n"):
                return headers
            name, sep, value = hl.rstrip(b"\r\n").decode("latin-1").partition(":")
            if not sep:
                continue  # строка без ':' — пропускаем (толерантно)
            headers[name.strip()] = value.strip()
        raise VaultProxyError("слишком много заголовков")

    async def _drain_headers(self, reader: asyncio.StreamReader) -> None:
        """Дочитать заголовки CONNECT до пустой строки (их не форвардим)."""
        for _ in range(_MAX_HEADERS):
            hl = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=self._read_timeout)
            if hl in (b"\r\n", b"\n"):
                return
        raise VaultProxyError("слишком много заголовков CONNECT")

    async def _read_body(
        self, reader: asyncio.StreamReader, headers: dict[str, str]
    ) -> bytes:
        te = _get(headers, "transfer-encoding")
        if te and "chunked" in te.lower():
            return await self._read_chunked(reader)
        cl = _get(headers, "content-length")
        if cl is None:
            return b""
        try:
            length = int(cl)
        except ValueError as exc:
            raise VaultProxyError(f"нечисловой Content-Length: {cl!r}") from exc
        if length < 0 or length > _MAX_BODY:
            raise VaultProxyError(f"Content-Length вне допустимого: {length}")
        if length == 0:
            return b""
        return await asyncio.wait_for(reader.readexactly(length), timeout=self._read_timeout)

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        buf = bytearray()
        while True:
            size_line = await asyncio.wait_for(
                reader.readuntil(b"\r\n"), timeout=self._read_timeout
            )
            size_hex = size_line.split(b";", 1)[0].strip()
            try:
                size = int(size_hex, 16)
            except ValueError as exc:
                raise VaultProxyError("битый chunk-size") from exc
            if size == 0:
                # трейлеры до пустой строки
                while True:
                    tl = await asyncio.wait_for(
                        reader.readuntil(b"\r\n"), timeout=self._read_timeout
                    )
                    if tl in (b"\r\n", b"\n"):
                        break
                return bytes(buf)
            if len(buf) + size > _MAX_BODY:
                raise VaultProxyError("тело chunked превысило лимит")
            buf += await asyncio.wait_for(reader.readexactly(size), timeout=self._read_timeout)
            await asyncio.wait_for(reader.readexactly(2), timeout=self._read_timeout)  # CRLF
        # недостижимо

    # --- сборка исходящего запроса / ответов ------------------------------

    def _build_request(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> bytes:
        out: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() in _HOP_HEADERS:
                continue
            out[name] = value
        out["Content-Length"] = str(len(body or b""))
        out["Connection"] = "close"  # заставляем сервис дать EOF после ответа
        head = f"{method} {target} HTTP/1.1\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in out.items())
        head += "\r\n"
        return head.encode("latin-1") + (body or b"")

    async def _respond_deny(
        self, writer: asyncio.StreamWriter, verdict: ScopeVerdict
    ) -> None:
        # remedy берётся у коннектора и НЕ проходит через host.deny_remedy —
        # и это ПРАВИЛЬНО (ревью #77 предполагало иначе, проверено — ложная
        # тревога). Сюда попадает ЖЁСТКИЙ DENY коннектора: запрос вне scope,
        # спрос НЕ поднимался. remedy коннектора предметный и режим-независимый
        # («вне scope; доступно: X; попроси оператора расширить scope») — он
        # корректен и под unattended (модель не может расширить scope сама, и
        # remedy честно указывает на нужное человеческое действие). host.deny_remedy
        # объясняет ДРУГОЕ — «спросить было некого» — и нужен лишь там, где отказ
        # вызван невозможностью ASK (ASK→deny под unattended, _ask_grant ниже уже
        # его применяет). Здесь ASK не было, поэтому подмешивать его нечего.
        body = f"{verdict.reason}\n\n{verdict.remedy}\n".encode()
        await self._send_response(writer, 403, "Forbidden", body)

    async def _ask_grant(
        self, verdict: ScopeVerdict, method: str, url: str
    ) -> bool:
        """Поднять ASK-спрос гранта у оператора через host (§4.6). True = грант на
        ЭТОТ запрос. Р0 «не висеть»: host.ask обязан иметь свой таймаут, но
        дополнительно страхуем wait_for'ом (`ask_timeout`); молчание/сбой/некому
        спросить → False (DENY). preview — факт запроса (метод+URL), значение
        секрета в спрос НЕ передаём.

        Постоянный грант (§4.6, «навсегда»): хост может ЗАПИСАТЬ узкую запись из
        `verdict.grant` в policy и сообщить об этом (`AskResult.persisted`) —
        тогда синхронно расширяем и СВОЙ живой scope (`_apply_grant`), иначе
        следующий такой же запрос снова поднял бы спрос, хотя в файле грант уже
        стоит. Имя секрета штампуем здесь: коннектор его не знает, а прокси
        поднят ровно под один секрет."""
        if self.host is None:
            return False  # standalone-сборка без host — спрашивать некому → DENY
        preview = f"{method} {url}"
        grant = verdict.grant
        if grant is not None and not grant.secret:
            grant = replace(grant, secret=self.secret.name)
        # Таймаут-вотчдог оборачивает ВЕСЬ ask_grant (ожидание клика + запись в
        # policy + notice оператору) — но через shield: истечение бюджета НЕ
        # обрывает уже начатую работу хоста. Иначе возникала гонка (нашло ревью):
        # оператор кликает «навсегда» у самой границы бюджета, хост успевает
        # записать грант в secrets.toml, но CancelledError из wait_for рвёт ask
        # ДО возврата — грант в файле есть, а этот запрос получает DENY, живой
        # scope не расширяется и оператор не уведомлён (ровно тот исход, который
        # _persist_grant клялся исключить). Условие «нет гонки»: host ОБЯЗАН
        # вернуть вердикт сразу после записи гранта, не блокируясь на доставке
        # notice оператору (наш OrchestratorVaultHost шлёт notice фоном — см. его
        # _persist_grant). Тогда ask_grant успевает вернуться в бюджет ask_timeout,
        # и запись+DENY-гонка не возникает; watchdog остаётся простой страховкой
        # Р0 от host, который завис на самом ожидании клика.
        try:
            result = await asyncio.wait_for(
                ask_grant(self.host, self.session_name, verdict.descr or "", preview, grant),
                timeout=self._ask_timeout,
            )
        except asyncio.TimeoutError:
            logger.info("VaultProxy: ASK без ответа (таймаут) → DENY %s %s", method, url)
            return False
        except Exception:  # noqa: BLE001 — сбой host не должен ронять прокси → DENY
            logger.exception("VaultProxy: host.ask упал → DENY %s %s", method, url)
            return False
        if result.persisted and grant is not None:
            self._apply_grant(grant)
        return bool(result)

    def _apply_grant(self, grant: ScopeGrant) -> None:
        """Расширить СТАТИЧНЫЙ снимок scope записанным в policy грантом (без
        перезапуска). Нужен для режима БЕЗ store (standalone/тесты): там `self.scope`
        — единственный источник решения и сам по себе запись в файл не подхватывает,
        поэтому без этого мгновенного расширения тот же запрос спрашивался бы снова.

        В ДИНАМИЧЕСКОМ режиме (со store) собственный грант виден и без этого:
        хост пишет его в secrets.toml ДО возврата вердикта, а `_current_scope`
        перечитывает файл по mtime/size на следующем же запросе (размер файла
        меняется → кэш store инвалидируется) — источник правды один, файл. Мутация
        снимка тут в этом случае на решение не влияет (решает свежий scope), но
        безвредна и оставлена ради единого кода пути ASK-гранта. Дублирование
        проверяем — иначе повторные гранты пухли бы списком.

        Кросс-прокси видимость (грант ДРУГОГО живого прокси того же секрета) со
        store решается тем же перечитыванием файла — см. _current_scope."""
        values = list(self.scope.get(grant.key) or [])
        if grant.value not in values:
            values.append(grant.value)
            self.scope[grant.key] = values
            # service_hosts выведены из url_prefixes при инициализации: грант на
            # НОВЫЙ хост без этого не считался бы «своим» для секрета.
            host = urlsplit(grant.value).hostname
            if host:
                self.service_hosts.add(host.lower())
            logger.info(
                "VaultProxy: постоянный грант записан в policy и применён к живому "
                "scope: %s += %s (секрет %s)", grant.key, grant.value, grant.secret)

    async def _respond_ask(
        self, writer: asyncio.StreamWriter, verdict: ScopeVerdict
    ) -> None:
        """ASK не привёл к гранту → предписывающий DENY (403). Разделяем случай
        «спрашивать некому» (host=None, standalone) и «оператор не подтвердил»."""
        if self.host is None:
            body = (
                f"Доступ требует подтверждения оператора: {verdict.descr}.\n\n"
                "В этой сборке кошелька интерактивный спрос не подключён "
                "(нет host), поэтому запрос не пропускается. Оставайся в пределах "
                "уже разрешённого скоупа секрета либо попроси оператора расширить "
                "его заранее.\n"
            ).encode()
        else:
            # Если host умеет объяснить СВОЙ отказ (напр. unattended-запуск, где
            # оператора не спрашивают вовсе) — берём его текст: иначе модели
            # соврали бы «оператор не подтвердил», хотя вопроса никто не задавал.
            explain = deny_remedy(self.host) or (
                "Оператор не подтвердил запрос (отказ либо нет ответа в срок), "
                "поэтому кошелёк его не пропускает. Оставайся в пределах уже "
                "разрешённого скоупа секрета либо попроси оператора подтвердить "
                "или расширить скоуп заранее."
            )
            body = (
                f"Доступ требует подтверждения оператора: {verdict.descr}.\n\n"
                f"{explain}\n"
            ).encode()
        await self._send_response(writer, 403, "Forbidden", body)

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        code: int,
        phrase: str,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        head = (
            f"HTTP/1.1 {code} {phrase}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        with suppress(Exception):
            await self._drain(writer)  # под таймаутом — не виснем на не-читающем пире

    # --- вспомогательное ---------------------------------------------------

    def _leaf_context(self, host: str) -> ssl.SSLContext:
        cached = self._leaf_ctx.get(host)
        if cached is not None:
            return cached
        leaf = self.ca.issue(host)  # валидирует host, VaultCAError на плохом
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(leaf.cert_path), keyfile=str(leaf.key_path))
        self._leaf_ctx[host] = ctx
        return ctx


def _get(headers: dict[str, str], name: str) -> str | None:
    """Регистронезависимый доступ к заголовку."""
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return None
