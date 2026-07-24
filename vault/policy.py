"""Правка policy кошелька из бота: просмотр + surgical-edit secrets.toml.

Границы безопасности (важно):
  * значения токенов (value/env для inject) НЕ показываются и НЕ вводятся через
    бот — только host-файлом. Через бот правится ЛИШЬ policy: sessions,
    commands, deny, confirm, а также создание/удаление host-passthrough секрета;
  * `allow_unsafe` (отключение guard'а) через бот не трогаем — это security-
    downgrade, только host-файлом;
  * команда `/wallet` — пользовательская (ALLOWED_USER_IDS), модель её вызвать
    не может (приходит от юзера, не из Claude).

tomlkit сохраняет комментарии/форматирование при правке; запись атомарна
(tmp+os.replace, симлинк-безопасно) с валидацией (результат перечитываем tomllib
перед заменой) и правами 0600.

Гонка «читаем-правим-пишем». Тот же файл правят бот (`/wallet`), CLI (`vault
policy`) и ASK-грант «навсегда» (§4.6) — из разных процессов. Read-modify-write
без блокировки терял бы правку, пришедшую вторым (последний dumps затирает файл
целиком). Поэтому весь цикл идёт под межпроцессным `flock` на файле-локе рядом с
secrets.toml (см. `_locked`). Ручную правку в редакторе лок, конечно, не
остановит — это честное ограничение, оно и раньше было таким.

Без зависимостей оркестратора: только stdlib + tomlkit/tomllib.
"""
from __future__ import annotations

import contextlib
import fcntl
import html
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib  # stdlib с 3.11
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import tomlkit

# под-команда правки списка → ключ в TOML
_LIST_FIELDS = {"session": "sessions", "cmd": "commands", "deny": "deny"}

# Ключи внутри [secrets.<имя>.scope], которые вообще разрешено править (и куда
# может писать ASK-грант «навсегда»). Allow-лист, а не «любой ключ»: scope —
# машинная настройка коннектора, и запись произвольного ключа из чата/гранта
# была бы способом молча поменять поведение прокси. `ask_prefixes` тут нет
# намеренно: расширять список «что вообще можно спросить» — решение оператора
# host-файлом, грант этого делать не должен.
_SCOPE_KEYS = ("url_prefixes",)

# Сколько ждём межпроцессный лок policy, прежде чем честно отказать. Правки
# короткие (прочитать-записать килобайты), поэтому секунды хватает с запасом;
# ждать дольше нельзя — вызов синхронный и держит event-loop бота.
_LOCK_TIMEOUT = 1.0
_LOCK_POLL = 0.02

# Список под-команд правки — моноширинным блоком (ровно, без «плавающих»
# переносов построчных <code>). Внутри <pre> экранируем только &<> (quote=False).
_EDIT_CMDS = (
    "/wallet confirm <имя> on|off\n"
    "/wallet session <имя> +<шаблон> | -<шаблон>\n"
    "/wallet cmd     <имя> +<шаблон> | -<шаблон>\n"
    "/wallet deny    <имя> +<шаблон> | -<шаблон>\n"
    "/wallet scope   <имя> +<url-префикс> | -<url-префикс>\n"
    "/wallet new <имя>\n"
    "/wallet rm  <имя>\n"
    "/wallet help"
)

USAGE = (
    "🔐 <b>/wallet</b> — policy кошелька (значения токенов не видны и не вводятся):\n"
    "<pre>" + html.escape(_EDIT_CMDS, quote=False) + "</pre>\n"
    "session/cmd/deny — fnmatch-шаблоны (напр. <code>*</code>, <code>dev-*</code>, "
    "<code>gh api *</code>). scope — URL-префиксы прокси-секрета (сюда же пишутся "
    "гранты «разрешить навсегда» из ASK; чтобы отозвать — "
    "<code>/wallet scope &lt;имя&gt; -&lt;url&gt;</code>). "
    "Значения (inject value/env) и allow_unsafe — только host-файлом."
)


class PolicyError(Exception):
    """Ошибка правки policy — текст показывается пользователю."""


def _toml_list(seq) -> str:
    """Список → TOML-массив-строка ["a", "b"] (для вывода «как из файла»)."""
    return "[" + ", ".join(f'"{x}"' for x in seq) + "]"


def _edit_footer(allow_edit: bool) -> str:
    """Подсказка по правке прямо в ответе /wallet (или заметка, что выключено)."""
    if not allow_edit:
        return (
            "✏️ <i>Правка из чата выключена (WALLET_POLICY_EDIT=0) — "
            "меняй host-файл secrets.toml.</i>"
        )
    return (
        "✏️ <i>Правка (значения токенов — только host-файлом):</i>\n"
        "<pre>" + html.escape(_EDIT_CMDS, quote=False) + "</pre>"
    )


class PolicyEditor:
    """Читает/правит secrets.toml. Один экземпляр на модуль; без своего кэша —
    каждый вызов читает файл заново (источник правды — файл, его же читает
    демон по mtime)."""

    def __init__(self, path: Path):
        self.path = path

    # ── чтение ──────────────────────────────────────────────────

    def _load_doc(self) -> tomlkit.TOMLDocument:
        try:
            return tomlkit.parse(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return tomlkit.document()
        except Exception as e:  # noqa: BLE001 — любой сбой чтения → понятный текст
            raise PolicyError(f"не читается {self.path}: {e}") from e

    @staticmethod
    def _secrets(doc: tomlkit.TOMLDocument):
        sec = doc.get("secrets")
        return sec if sec is not None else {}

    def render(self, allow_edit: bool = True) -> str:
        """Список секретов и policy — БЕЗ значений токенов. Тело — TOML-подобным
        текстом в <pre>-блоке (видно, что это из файла) + подсказка по правке."""
        secrets = self._secrets(self._load_doc())
        if not secrets:
            body = "# секретов нет"
        else:
            blocks = []
            for name, s in secrets.items():
                inject = bool(s.get("value")) and bool(s.get("env"))
                connector = str(s.get("connector", ""))
                if connector:
                    # Прокси-секрет (§4.5): ни env, ни commands у него нет —
                    # показывать его «host-passthrough» значило бы врать про то,
                    # как выдаётся кред (и куда пишется ASK-грант).
                    mode = f"proxy (connector {connector})"
                elif inject:
                    mode = f"inject (env ${s.get('env')})"
                else:
                    mode = "host-passthrough"
                conf = "on" if s.get("confirm", True) else "off"
                lines = [f"[secrets.{name}]   # {mode} · confirm = {conf}"]
                lines.append(f"sessions = {_toml_list(s.get('sessions', ()))}")
                cmds = s.get("commands", ())
                if not connector:
                    lines.append(
                        f"commands = {_toml_list(cmds)}"
                        + ("" if cmds else "   # пусто = дефолт host: gh/git/ssh/scp")
                    )
                if s.get("deny"):
                    lines.append(f"deny = {_toml_list(s.get('deny'))}")
                # Скоуп прокси-секрета: url_prefixes (в т.ч. записанные ASK-гранты
                # «навсегда») и ask_prefixes — оператор должен ВИДЕТЬ, что выдано,
                # иначе отозвать грант нечем.
                scope = s.get("scope") or {}
                if scope.get("url_prefixes"):
                    lines.append(
                        f"scope.url_prefixes = {_toml_list(scope.get('url_prefixes'))}")
                if scope.get("ask_prefixes"):
                    lines.append(
                        f"scope.ask_prefixes = {_toml_list(scope.get('ask_prefixes'))}")
                if s.get("description"):
                    lines.append(f"# {s.get('description')}")
                blocks.append("\n".join(lines))
            body = "\n\n".join(blocks)
        return (
            "🔐 <b>Секреты кошелька</b> (значения токенов скрыты):\n"
            # quote=False: внутри <pre> экранируем только &<> — кавычки Telegram
            # покажет как есть (hex-entity &#x27; он бы не декодировал).
            "<pre>" + html.escape(body, quote=False) + "</pre>\n"
            + _edit_footer(allow_edit)
        )

    # ── правка ──────────────────────────────────────────────────

    def apply(self, args: list[str], allow_edit: bool = True) -> str:
        """Диспетчер под-команд. args — токены после «/wallet». allow_edit=False
        (WALLET_POLICY_EDIT=0): просмотр работает, операции правки отклоняются."""
        if args and args[0] in ("help", "?"):
            return USAGE
        if not args or args[0] in ("list", "ls"):
            return self.render(allow_edit)
        if not allow_edit:
            raise PolicyError(
                "правка policy из чата выключена (WALLET_POLICY_EDIT=0) — "
                "меняй host-файл secrets.toml"
            )
        sub, rest = args[0], args[1:]
        if sub == "confirm":
            return self._set_confirm(rest)
        if sub in _LIST_FIELDS:
            return self._edit_list(sub, rest)
        if sub == "scope":
            return self._edit_scope(rest)
        if sub == "new":
            return self._new(rest)
        if sub == "rm":
            return self._rm(rest)
        raise PolicyError(f"неизвестная под-команда «{sub}». <code>/wallet help</code>")

    def _get_secret(self, doc, name: str):
        sec = doc.get("secrets")
        if sec is None or name not in sec:
            raise PolicyError(f"нет секрета «{name}». <code>/wallet</code> — список")
        return sec[name]

    def _set_confirm(self, rest: list[str]) -> str:
        if len(rest) != 2 or rest[1] not in ("on", "off"):
            raise PolicyError("использование: <code>/wallet confirm &lt;имя&gt; on|off</code>")
        name, val = rest[0], rest[1] == "on"
        with self._edit() as doc:
            self._get_secret(doc, name)["confirm"] = val
        return f"✅ {name}: confirm = {'on' if val else 'off'}"

    def _edit_list(self, sub: str, rest: list[str]) -> str:
        key = _LIST_FIELDS[sub]
        if len(rest) != 2 or rest[1][0] not in "+-":
            raise PolicyError(
                f"использование: <code>/wallet {sub} &lt;имя&gt; +&lt;шаблон&gt; | -&lt;шаблон&gt;</code>"
            )
        name, op, pat = rest[0], rest[1][0], rest[1][1:]
        if not pat:
            raise PolicyError("пустой шаблон")
        with self._edit() as doc:
            secret = self._get_secret(doc, name)
            cur = list(secret.get(key, []))
            if op == "+":
                if pat in cur:
                    raise PolicyError(f"«{pat}» уже в {key} секрета {name}")
                cur.append(pat)
                verb = "добавлен"
            else:
                if pat not in cur:
                    raise PolicyError(f"«{pat}» нет в {key} секрета {name}")
                cur.remove(pat)
                verb = "убран"
            secret[key] = cur
        shown = ", ".join(cur) or "— (пусто)"
        return f"✅ {name}: {key} {verb} «{pat}» → [{shown}]"

    def _new(self, rest: list[str]) -> str:
        if len(rest) != 1:
            raise PolicyError("использование: <code>/wallet new &lt;имя&gt;</code>")
        name = rest[0]
        if not name.replace("-", "").replace("_", "").isalnum():
            raise PolicyError("имя секрета: буквы/цифры/дефис/подчёркивание")
        with self._edit() as doc:
            if "secrets" not in doc:
                doc["secrets"] = tomlkit.table(is_super_table=True)
            if name in doc["secrets"]:
                raise PolicyError(f"секрет «{name}» уже есть")
            t = tomlkit.table()
            t["description"] = "host-passthrough (создан через /wallet)"
            t["sessions"] = []            # deny-by-default: пока никому
            t["commands"] = []            # пусто = дефолт host-команд (gh/git/ssh/scp)
            t["confirm"] = True           # безопасный дефолт: спрашивать
            doc["secrets"][name] = t
        return (
            f"✅ создан host-passthrough секрет «{name}» (deny-by-default).\n"
            f"Дальше: <code>/wallet session {name} +*</code> и "
            f"<code>/wallet cmd {name} +gh</code>."
        )

    def _rm(self, rest: list[str]) -> str:
        if len(rest) != 1:
            raise PolicyError("использование: <code>/wallet rm &lt;имя&gt;</code>")
        name = rest[0]
        with self._edit() as doc:
            self._get_secret(doc, name)  # проверка существования
            del doc["secrets"][name]
        return f"✅ секрет «{name}» удалён"

    # ── scope прокси-секрета (в т.ч. ASK-гранты «навсегда») ─────

    def _edit_scope(self, rest: list[str]) -> str:
        """`/wallet scope <имя> +<url-префикс> | -<url-префикс>` — ручной просмотр/
        отзыв того же поля, куда пишет ASK-грант «навсегда». Без этой команды
        грант было бы нечем отозвать из чата (правило прозрачности)."""
        if len(rest) != 2 or rest[1][0] not in "+-":
            raise PolicyError(
                "использование: <code>/wallet scope &lt;имя&gt; "
                "+&lt;url-префикс&gt; | -&lt;url-префикс&gt;</code>"
            )
        name, op, value = rest[0], rest[1][0], rest[1][1:]
        if op == "+":
            return self.grant_scope(name, "url_prefixes", value)
        return self.revoke_scope(name, "url_prefixes", value)

    @staticmethod
    def _check_prefix(key: str, value: str) -> None:
        """Валидация значения scope перед записью — общая для чата и ASK-гранта.

        Требуем ПОЛНЫЙ URL (схема+хост) и запрещаем глоб-символы: `url_prefixes`
        сравниваются literal-префиксом (см. generic_bearer), поэтому «*» в них не
        волшебный, но выглядел бы как «разрешено всё» и вводил бы оператора в
        заблуждение. Пустой путь/корень (`https://svc` или `https://svc/`) — это
        грант на ВЕСЬ сервис; из чата так можно (осознанное решение оператора,
        это его файл), но ASK-грант такого не предлагает (см. _narrow_grant)."""
        if key not in _SCOPE_KEYS:
            raise PolicyError(
                f"в scope через policy-правку доступны только {', '.join(_SCOPE_KEYS)}")
        if not value.strip():
            raise PolicyError("пустой префикс")
        if any(ch in value for ch in "*?["):
            raise PolicyError(
                "URL-префикс сравнивается буквально — глоб-символы (*, ?, []) в нём "
                "не работают; укажи полный префикс вида https://api.svc/v1/x")
        # Управляющие символы и backtick в URL аномальны (в ASK-гранте значение
        # приходит из URL запроса, которым управляет модель). Запрет их прямо на
        # записи закрывает разом две вещи: перевод строки/`%0a` не расщепит
        # secrets.toml, а backtick не исказит markdown-notice оператору и
        # копируемую команду отзыва (нашло ревью). Легитимный URL их не содержит.
        if "`" in value or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
            raise PolicyError(
                "URL-префикс содержит управляющие символы или backtick — "
                "недопустимо для записи в policy")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise PolicyError(
                f"«{value}» не похож на URL-префикс: нужен полный вид "
                "https://api.svc/путь")

    def _scope_table(self, secret, create: bool):
        """Под-таблица [secrets.<имя>.scope] (создать при необходимости).

        Если в файле на месте scope лежит НЕ таблица (руками испорчено) — честная
        ошибка, а не AttributeError наружу: демон такой секрет всё равно не
        грузит (см. store.py), и молча перезаписывать чужую строку нельзя."""
        scope = secret.get("scope")
        if scope is None:
            if not create:
                return None
            scope = tomlkit.table()
            secret["scope"] = scope
        elif not hasattr(scope, "get"):
            raise PolicyError(
                "scope секрета — не таблица ([secrets.<имя>.scope]); почини "
                "secrets.toml вручную")
        return scope

    def grant_scope(
        self, name: str, key: str, value: str, exist_ok: bool = False
    ) -> str:
        """Добавить значение в `secrets.<name>.scope.<key>` (ASK-грант «навсегда»
        и `/wallet scope +`).

        `exist_ok=True` (путь ASK-гранта): значение уже в policy — это УСПЕХ, а не
        ошибка (грант стоит, цель достигнута; так бывает при гонке двух одинаковых
        ASK). Из чата (`exist_ok=False`) то же самое — ошибка: оператор явно
        добавляет то, что уже есть, и должен это увидеть."""
        self._check_prefix(key, value)
        already = False
        with self._edit() as doc:
            secret = self._get_secret(doc, name)
            scope = self._scope_table(secret, create=True)
            cur = list(scope.get(key, []))
            if value in cur:
                if not exist_ok:
                    raise PolicyError(f"«{value}» уже в scope.{key} секрета {name}")
                already = True   # запись всё равно случится, но документ не изменён
            else:
                cur.append(value)
                scope[key] = cur
        if already:
            return f"✅ {name}: scope.{key} уже содержит «{value}»"
        return f"✅ {name}: scope.{key} += «{value}» → [{', '.join(cur)}]"

    def revoke_scope(self, name: str, key: str, value: str) -> str:
        """Убрать значение из `secrets.<name>.scope.<key>` (отзыв гранта)."""
        if key not in _SCOPE_KEYS:
            raise PolicyError(
                f"в scope через policy-правку доступны только {', '.join(_SCOPE_KEYS)}")
        with self._edit() as doc:
            secret = self._get_secret(doc, name)
            scope = self._scope_table(secret, create=False)
            cur = list(scope.get(key, [])) if scope is not None else []
            if value not in cur:
                raise PolicyError(f"«{value}» нет в scope.{key} секрета {name}")
            cur.remove(value)
            scope[key] = cur
        shown = ", ".join(cur) or "— (пусто)"
        return f"✅ {name}: scope.{key} -= «{value}» → [{shown}]"

    # ── запись ──────────────────────────────────────────────────

    @contextlib.contextmanager
    def _locked(self):
        """Межпроцессный лок на время цикла «прочитать-правку-записать».

        Без него две одновременные правки (бот `/wallet`, `vault policy`, ASK-
        грант «навсегда») читали бы один и тот же документ и вторая затирала бы
        первую целиком — грант молча пропал бы, а оператору отрапортовали бы об
        успехе. flock на отдельном файле-локе (не на самом secrets.toml: его мы
        заменяем через rename, и лок на старом inode ничего не значил бы).

        Ждём НЕблокирующе с коротким опросом: вызов синхронный и живёт внутри
        event-loop бота, вешать луп на чужую правку нельзя. Не дождались —
        честный PolicyError («занято, повтори»), а не тихая потеря правки.
        """
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + _LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise PolicyError(
                            "policy сейчас правит другой процесс (бот/CLI) — "
                            "повтори через секунду"
                        ) from None
                    time.sleep(_LOCK_POLL)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _edit(self):
        """Транзакция правки: под локом прочитать документ, отдать его наружу и
        записать. Исключение внутри → файл не трогаем (правка не применилась)."""
        with self._locked():
            doc = self._load_doc()
            yield doc
            self._write(doc)

    def _write(self, doc: tomlkit.TOMLDocument) -> None:
        """Записать документ атомарно, симлинк-безопасно, правами 0600.

        Зовётся ТОЛЬКО из `_edit` (под локом) — отдельная запись без чтения под
        тем же локом снова открыла бы гонку.
        """
        text = tomlkit.dumps(doc)
        # Валидация: результат обязан парситься stdlib-парсером (тем же, что у
        # демона) — иначе не заменяем оригинал, чтобы правка не сломала policy.
        try:
            tomllib.loads(text)
        except Exception as e:  # noqa: BLE001
            raise PolicyError(f"внутренняя ошибка: результат не валиден ({e})") from e
        # Временный файл со СЛУЧАЙНЫМ именем в той же директории + os.replace —
        # та же схема, что vault/inject.py::atomic_write (её саму не импортируем:
        # она тянет vault.tls → cryptography, а policy обязан работать на голом
        # stdlib+tomlkit, им пользуется CLI). Предсказуемое имя `secrets.toml.tmp`
        # + O_CREAT БЕЗ O_EXCL следовало бы за подложенным симлинком и записало бы
        # секреты в чужой файл; mkstemp создаёт файл сам (O_EXCL|O_NOFOLLOW-
        # эквивалент) и с правами 0600. replace заменяет саму запись назначения,
        # не разыменовывая её.
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o600)  # secrets.toml с реальными значениями — только владельцу
            os.replace(tmp, self.path)  # атомарно; демон перечитает по mtime
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
