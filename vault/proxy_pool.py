"""SessionProxyPool — пул per-session MITM-прокси (фаза 2, срез 2.4).

Связывает policy (прокси-секрет с коннектором+scope) с живым `VaultProxy` на
ВЫДЕЛЕННОМ на сессию listen-порту (§4.3: атрибуция по порту, а не по токену в
запросе). Лончер при `launch` просит поднять прокси для (сессия, секрет) и
получает порт — чтобы прописать `HTTP_PROXY` в песочницу (обвязка bwrap — уже
СЛЕДУЮЩИЙ срез, здесь только vault-сторона). При завершении сессии прокси
снимаются, порты освобождаются — жизненный цикл как у токенов демона.

Атрибуция (§4.3). Порт → (сессия, секрет, scope): прокси создаётся под ОДИН
секрет/коннектор/scope (см. vault/proxy.py), слушает свой эфемерный порт, поэтому
токен в самом запросе не нужен — источник (порт) уже говорит, чей это трафик и в
каком скоупе. Разные сессии/секреты → разные порты → разные прокси.

Автономно: только stdlib + vault.* (держится vault_domain_test через
walk_packages). Значение секрета в песочницу не входит — его подставляет прокси
между машиной и сервисом (§4.4); сюда из policy приходят лишь connector/scope и
сам секрет, дальше их разбирает VaultProxy.
"""

from __future__ import annotations

import logging
import ssl

from .connectors import get_connector
from .host import VaultHost
from .proxy import VaultProxy
from .store import SecretStore
from .tls import VaultCA

logger = logging.getLogger(__name__)


class ProxyPoolError(RuntimeError):
    """Секрет не годится для прокси (нет/чужая сессия/не прокси/неизвестный коннектор)."""


class SessionProxyPool:
    """Пул прокси по ключу (имя сессии, имя секрета).

    ca — общий корневой CA Vault (его корень лежит в trust-store песочницы);
    store — источник policy/значений (тот же, что у демона); upstream_ssl — trust
    для реориджина к РЕАЛЬНОМУ сервису (None → системный дефолт; в тестах — trust
    к локальному «сервису»). bind_host — где слушать (127.0.0.1). host — VaultHost
    для ASK-спроса гранта (§4.6); None → ASK трактуется как DENY (спрашивать
    некому). Прокси per-session, поэтому имя сессии прокидывается в VaultProxy.
    """

    def __init__(
        self,
        ca: VaultCA,
        store: SecretStore,
        *,
        bind_host: str = "127.0.0.1",
        upstream_ssl: ssl.SSLContext | None = None,
        host: VaultHost | None = None,
    ) -> None:
        self._ca = ca
        self._store = store
        self._bind_host = bind_host
        self._upstream_ssl = upstream_ssl
        self._host = host
        # (session_name, secret_name) → живой VaultProxy.
        self._proxies: dict[tuple[str, str], VaultProxy] = {}

    async def start(self, session_name: str, secret_name: str) -> int:
        """Поднять (или вернуть уже поднятый) прокси для (сессия, секрет) на
        выделенном эфемерном порту; вернуть порт для HTTP_PROXY лончера.

        Идемпотентно: повторный вызов для той же пары отдаёт прежний порт.
        Отказ (ProxyPoolError), если секрета нет, он не разрешён этой сессии, он
        не прокси-секрет или коннектор неизвестен (реестр уже залогировал)."""
        key = (session_name, secret_name)
        existing = self._proxies.get(key)
        if existing is not None and existing.port is not None:
            return existing.port

        secret = self._store.load().get(secret_name)
        if secret is None:
            raise ProxyPoolError(
                f"секрет {secret_name!r} не найден в policy (прокси не поднят)")
        if not secret.session_allowed(session_name):
            raise ProxyPoolError(
                f"секрет {secret_name!r} не разрешён сессии {session_name!r}")
        if not secret.is_proxy:
            raise ProxyPoolError(
                f"секрет {secret_name!r} без connector — не прокси-секрет; "
                "прокси поднимается только под секрет с коннектором (§4.5)")
        connector = get_connector(secret.connector)
        if connector is None:
            # get_connector уже залогировал WARNING (выключено = не существует).
            raise ProxyPoolError(
                f"секрет {secret_name!r}: неизвестный connector "
                f"{secret.connector!r} — прокси не поднят")

        proxy = VaultProxy(
            self._ca,
            connector,
            secret,
            dict(secret.scope),
            # Тот же store, что у пула/демона: прокси перечитывает СВЕЖИЙ scope
            # секрета из secrets.toml по mtime (единый источник правды). Так грант,
            # записанный другим живым прокси того же секрета, и операторский отзыв
            # действуют на этот прокси на лету, без перезапуска (§4.6).
            store=self._store,
            host=self._host,
            session_name=session_name,
            upstream_ssl=self._upstream_ssl,
            bind_host=self._bind_host,
            bind_port=0,
        )
        port = await proxy.start()
        self._proxies[key] = proxy
        logger.info(
            "SessionProxyPool: сессия %s, секрет %s (connector %s) → прокси на порту %d",
            session_name, secret_name, secret.connector, port)
        return port

    def port(self, session_name: str, secret_name: str) -> int | None:
        """Порт живого прокси пары, либо None если не поднят."""
        proxy = self._proxies.get((session_name, secret_name))
        return proxy.port if proxy is not None else None

    def ports(self, session_name: str) -> dict[str, int]:
        """{имя секрета → порт} всех живых прокси сессии."""
        return {
            secret_name: proxy.port
            for (sess, secret_name), proxy in self._proxies.items()
            if sess == session_name and proxy.port is not None
        }

    async def stop(self, session_name: str, secret_name: str | None = None) -> None:
        """Снять прокси сессии (все или один по secret_name), освободив порт(ы)."""
        keys = [
            k for k in self._proxies
            if k[0] == session_name and (secret_name is None or k[1] == secret_name)
        ]
        for k in keys:
            proxy = self._proxies.pop(k)
            await proxy.stop()
            logger.info(
                "SessionProxyPool: снят прокси сессии %s секрет %s (порт освобождён)",
                k[0], k[1])

    async def stop_all(self) -> None:
        """Снять ВСЕ прокси (teardown демона)."""
        for k in list(self._proxies):
            await self._proxies.pop(k).stop()


__all__ = ["SessionProxyPool", "ProxyPoolError"]
