"""Контракт коннектора Vault (§4.5 ARCHITECTURE-claude-box) — плагин, знающий
КОНКРЕТНЫЙ сервис: как подставить кред и что считать «в скоупе». Без зависимостей
оркестратора и без aiohttp: HTTP-запрос описан своим лёгким dataclass `HttpReq`.

Здесь только форма (Protocol + типы данных). Реализации — в модулях-коннекторах
(generic_bearer.py и далее gdocs/jenkins/…), реестр — в __init__.py.

О решении скоупа. Команда под секретом уже имеет свой `Verdict`
(vault/verdict.py: allowed/needs_confirm/reason) — это policy ДО подтверждения
кнопкой, форма «прошло/не прошло». Коннектор решает ДРУГОЕ: попадает ли КОНКРЕТНЫЙ
HTTP-запрос в выданный скоуп, и это трёхзначное решение ALLOW | DENY | ASK
(ASK поднимает хук «спроса» у оператора — §4.6). Поэтому для скоупа — отдельный
тип `ScopeVerdict`, а не перегрузка командного `Verdict`: разные оси, разные
поля. См. отчёт по расхождению с §4.5 (там в наброске Protocol стоит просто
`Verdict`).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from ..secret import Secret


@dataclass
class HttpReq:
    """Минимальное описание исходящего HTTP-запроса — БЕЗ aiohttp/requests.

    Коннектор читает/меняет его (authorize подставляет кред, in_scope смотрит
    url). `headers` — обычный dict (регистр имён как есть); `body` — сырые байты
    или None. Транспорт (MITM/демон) конвертирует в/из свой формат сам.
    """

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class ScopeGrant:
    """УЗКИЙ постоянный грант, который оператор может записать в policy (§4.6,
    «разрешить навсегда»).

    Зачем отдельный тип, а не строка в descr. Кнопка «навсегда» пишет в
    `secrets.<имя>.scope`, и запись обязана быть МАШИННОЙ (ключ+значение), а не
    выуженной регуляркой из человеческого текста: иначе «узость» гранта была бы
    вопросом форматирования. Знает, что записать, ТОЛЬКО коннектор (он же владеет
    семантикой scope), поэтому грант рождается вместе с ASK-вердиктом.

    Поля:
      * key    — ключ внутри `scope` (для generic-bearer: "url_prefixes");
      * value  — КОНКРЕТНОЕ значение из запроса (URL-префикс без query), а не «*»
                 и не «весь хост»: грант должен покрывать запрошенный ресурс и
                 почти ничего сверх;
      * label  — человеческий текст «что именно разрешается навсегда»; хост
                 показывает его оператору ДО нажатия кнопки;
      * secret — имя секрета в policy. Коннектор его не знает (in_scope секрета
                 не видит) — штампует прокси, который под этот секрет и поднят.

    Грант может отсутствовать (ASK без grant): тогда «навсегда» невозможно и
    хост обязан НЕ показывать третью кнопку (например, запрос к корню сервиса —
    узкого префикса из него не вывести).
    """

    key: str
    value: str
    label: str
    secret: str = ""


@dataclass(frozen=True)
class ScopeVerdict:
    """Решение коннектора по одному запросу: ALLOW | DENY | ASK.

      * ALLOW — запрос в выданном скоупе, пропускаем (с авторизацией);
      * DENY  — вне скоупа; `reason` (что не так) и `remedy` ОБЯЗАТЕЛЕН и
                ПРЕДПИСЫВАЮЩИЙ (Р0): что не так + что доступно вместо (из scope)
                + что делать. Гасит поиск обходов;
      * ASK   — скоуп не покрывает, но расширяем спросом у оператора (§4.6);
                `descr` — человеческое описание запрашиваемого доступа для кнопок,
                `grant` (опционально) — узкая запись в policy для «навсегда».

    Собирать через фабрики allow()/deny()/ask(), не напрямую — они держат
    инвариант «у DENY есть remedy, у ASK есть descr».
    """

    kind: str  # "allow" | "deny" | "ask"
    reason: str | None = None
    remedy: str | None = None
    descr: str | None = None
    grant: ScopeGrant | None = None

    @classmethod
    def allow(cls) -> ScopeVerdict:
        return cls(kind="allow")

    @classmethod
    def deny(cls, reason: str, remedy: str) -> ScopeVerdict:
        if not remedy or not remedy.strip():
            raise ValueError("DENY без remedy запрещён (Р0: отказ предписывающий)")
        return cls(kind="deny", reason=reason, remedy=remedy)

    @classmethod
    def ask(cls, descr: str, grant: ScopeGrant | None = None) -> ScopeVerdict:
        """ASK. `grant` — узкая запись в policy, если из запроса её МОЖНО вывести;
        None → «навсегда» недоступно (только разовый грант)."""
        if not descr or not descr.strip():
            raise ValueError("ASK без descr запрещён (нужно описание для оператора)")
        return cls(kind="ask", descr=descr, grant=grant)

    @property
    def is_allow(self) -> bool:
        return self.kind == "allow"

    @property
    def is_deny(self) -> bool:
        return self.kind == "deny"

    @property
    def is_ask(self) -> bool:
        return self.kind == "ask"


def with_header(req: HttpReq, name: str, value: str) -> HttpReq:
    """Вернуть КОПИЮ req с добавленным/переопределённым заголовком (не мутируем
    исходный — authorize по контракту отдаёт req, вызывающий не ждёт side-effect
    на своём объекте). Существующий заголовок с тем же именем (без учёта регистра)
    заменяется."""
    headers = {k: v for k, v in req.headers.items() if k.lower() != name.lower()}
    headers[name] = value
    return replace(req, headers=headers)


@runtime_checkable
class Connector(Protocol):
    """Плагин под конкретный сервис (§4.5). Обязательны для КАЖДОГО коннектора
    `name`, `authorize`, `in_scope`; остальное — по мере умений сервиса
    (generic-bearer их не поддерживает и возвращает None/скоуп как есть).

    `@runtime_checkable` → `isinstance(x, Connector)` в рантайме проверяет
    НАЛИЧИЕ методов (не сигнатуры) — ловит забытый метод без mypy в CI."""

    name: str

    def authorize(self, req: HttpReq, secret: Secret) -> HttpReq:
        """Подставить кред в запрос (заголовок/query/подпись — знает коннектор).
        Возвращает авторизованный запрос (обычно копию)."""
        ...

    def in_scope(self, req: HttpReq, scope: dict) -> ScopeVerdict:
        """ALLOW | DENY(reason, remedy) | ASK(descr). Без живых вызовов сервиса:
        «что доступно» берётся из scope (кэш лончера), не из сети."""
        ...

    # Ниже — опциональные умения (OAuth/минт/рефреш/резолв скоупа). Объявлены в
    # контракте (§4.5), но требуются НЕ от всех коннекторов; generic-bearer их
    # не поддерживает.
    def oauth_flow(self) -> object | None:
        """Интерактивный OAuth-флоу для `vault connect <svc>`; None — не умеет."""
        ...

    def resolve_scope(self, human: dict) -> dict:
        """Человеческий скоуп («Team/X») → машинный (folder id). Для коннектора
        без резолва — вернуть как есть."""
        ...

    def mint(self, scope: dict) -> Secret | None:
        """Тир 3: выпустить суб-токен под скоуп; None — не умеет."""
        ...

    def refresh(self, secret: Secret) -> Secret | None:
        """Обновить свой сервисный OAuth-токен; None — не умеет/не нужно."""
        ...
