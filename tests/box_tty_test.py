"""Арбитр терминала claude-box (box_cli/tty.py): stdin у relay и кошелька ОДИН.

Регрессия, ради которой всё написано: раньше PTY-relay вешал свой add_reader на
stdin, а TtyVaultHost на первом же confirm вешал СВОЙ на тот же fd и в finally
делал remove_reader. В asyncio второй add_reader затирает колбэк, а remove_reader
снимает читателя целиком — после первого confirm ввод в сессию умирал НАВСЕГДА, а
ответ печатался вслепую (raw = нет эха) и воровал байты у claude.

Что проверяем (на реальной pty-паре, без интерактивного терминала):
  * байты доходят до PTY-приёмника ДО вопроса;
  * на время вопроса терминал возвращается в нормальный режим (ЭХО включено),
    ответ собирается строкой;
  * ПОСЛЕ вопроса raw восстановлен и байты СНОВА доходят до приёмника (ключевой
    тест — именно это ломалось);
  * таймаут: молчание оператора → «» → DENY (не зависание);
  * отмена/исключение посреди вопроса не оставляют терминал в сломанном режиме;
  * хвост строки после ответа не теряется, а уходит в сессию;
  * relay честно гаснет на EOF stdin и на мёртвом приёмнике;
  * BoxVaultHost: y/да → True, n/пусто/нет tty → False.

Запуск: .venv/bin/python tests/box_tty_test.py
"""
from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
import termios
import tty as ttymod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box_cli.tty import BoxVaultHost, StdinArbiter  # noqa: E402
from box_cli.wallet import box_policy_access  # noqa: E402
from vault.connectors.contract import ScopeGrant  # noqa: E402
from vault.host import AskResult, ask_grant  # noqa: E402
from vault.policy import PolicyEditor  # noqa: E402

try:
    import tomllib  # stdlib с 3.11  # noqa: E402
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # noqa: E402

SESSION = "claude-box"

# Узкий грант ровно на запрошенный ресурс — как его штампует коннектор+прокси
# (см. generic_bearer._narrow_grant + proxy._ask_grant, который проставляет secret).
_GRANT = ScopeGrant(
    key="url_prefixes",
    value="https://api.svc/v1/docs/42",
    label="доступ к «https://api.svc/v1/docs/42» и вложенным путям (навсегда)",
    secret="svc",
)

_SECRETS_TOML = """\
[secrets.svc]
connector = "generic-bearer"
value = "tok-secret"
sessions = ["*"]

[secrets.svc.scope]
url_prefixes = ["https://api.svc/v1/allowed"]
ask_prefixes = ["https://api.svc/v1/docs"]
"""


def _write_secrets(text: str = _SECRETS_TOML) -> Path:
    """Временный secrets.toml (0600) для ASK-гранта. Не трогает реальный файл."""
    fd, name = tempfile.mkstemp(prefix="box-ask-secrets-", suffix=".toml")
    os.close(fd)
    path = Path(name)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _scope_prefixes(path: Path) -> list[str]:
    """scope.url_prefixes секрета svc, прочитанные stdlib-парсером (как демон)."""
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    return list(doc["secrets"]["svc"].get("scope", {}).get("url_prefixes", []))


class _Pty:
    """pty-пара: master — «клавиатура оператора», slave — stdin арбитра."""

    def __init__(self) -> None:
        self.master, self.slave = os.openpty()
        self.sink: list[bytes] = []

    def sink_write(self, data: bytes) -> bool:
        self.sink.append(data)
        return True

    def typed(self, data: bytes) -> None:
        os.write(self.master, data)

    def relayed(self) -> bytes:
        return b"".join(self.sink)

    def echo_on(self) -> bool:
        return bool(termios.tcgetattr(self.slave)[3] & termios.ECHO)

    def close(self) -> None:
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


async def _until(cond, timeout: float = 2.0) -> bool:
    """Подождать условия, крутя event loop (relay асинхронный)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


async def test_relay_survives_prompt():
    """ГЛАВНОЕ: после отработавшего вопроса ввод снова доходит до PTY."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        raw_attrs_before = termios.tcgetattr(p.slave)
        ttymod.setraw(p.slave)  # как делает лончер на настоящем терминале
        assert arb.start(), "читатель stdin не повесился"
        assert not p.echo_on(), "raw: эха быть не должно"

        p.typed(b"before")
        assert await _until(lambda: p.relayed() == b"before"), p.relayed()

        task = asyncio.create_task(arb.prompt("вопрос?", "gh auth token"))
        assert await _until(p.echo_on), "на время вопроса эхо обязано включиться"
        p.typed(b"y\n")
        assert (await task).strip() == "y"
        assert not p.echo_on(), "после ответа raw должен вернуться"

        p.typed(b"after")
        assert await _until(lambda: p.relayed().endswith(b"after")), p.relayed()
        assert p.relayed().startswith(b"before"), p.relayed()

        arb.stop()
        termios.tcsetattr(p.slave, termios.TCSANOW, raw_attrs_before)
        print("OK арбитр: relay → вопрос (с эхом) → relay снова живой")
    finally:
        p.close()


async def test_prompt_timeout_denies():
    """Молчание оператора: таймаут → «» (= DENY у хоста), а не зависание."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=0.15)
        ttymod.setraw(p.slave)
        arb.start()
        assert await arb.prompt("вопрос?", "preview") == ""
        assert not p.echo_on(), "терминал вернулся в raw и после таймаута"
        # После таймаута арбитр снова релеит.
        p.typed(b"z")
        assert await _until(lambda: p.relayed() == b"z"), p.relayed()
        arb.stop()
        print("OK арбитр: таймаут вопроса → пустой ответ (DENY), relay жив")
    finally:
        p.close()


async def test_prompt_cancel_restores_terminal():
    """Отмена посреди вопроса (Ctrl-C/shutdown) не оставляет терминал сломанным."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=5.0)
        ttymod.setraw(p.slave)
        raw = termios.tcgetattr(p.slave)
        arb.start()
        task = asyncio.create_task(arb.prompt("вопрос?", "preview"))
        assert await _until(p.echo_on)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert termios.tcgetattr(p.slave) == raw, "raw-настройки не восстановлены"
        p.typed(b"q")
        assert await _until(lambda: p.relayed() == b"q"), p.relayed()
        arb.stop()
        print("OK арбитр: отмена вопроса возвращает raw, relay продолжает работать")
    finally:
        p.close()


async def test_tail_after_answer_goes_to_session():
    """Байты, напечатанные ПОСЛЕ ответа в той же порции, не теряются."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        ttymod.setraw(p.slave)
        arb.start()
        task = asyncio.create_task(arb.prompt("вопрос?", "preview"))
        assert await _until(p.echo_on)
        p.typed(b"y\nhello")  # канонический режим отдаёт строку, хвост — следом
        assert (await task).strip() == "y"
        assert await _until(lambda: p.relayed().endswith(b"hello")), p.relayed()
        arb.stop()
        print("OK арбитр: хвост после ответа уходит в сессию, а не в никуда")
    finally:
        p.close()


async def test_relay_stops_on_eof_and_dead_sink():
    """Relay честно останавливается: EOF stdin и мёртвый приёмник (процесс закрыл
    PTY) не дают ни исключений, ни бесконечного цикла колбэков."""
    # EOF stdin: пайп, запись закрыли.
    r, w = os.pipe()
    arb = StdinArbiter(r, write_bytes=lambda d: True, timeout=0.1)
    assert arb.start()
    os.write(w, b"tail")
    os.close(w)
    assert await _until(lambda: not arb._reader_on, 1.0), "читатель не снят на EOF"
    os.close(r)

    # Приёмник умер (write вернул False) — relay выключается, арбитр жив.
    p = _Pty()
    try:
        dead = StdinArbiter(p.slave, write_bytes=lambda d: False, timeout=0.1)
        ttymod.setraw(p.slave)
        dead.start()
        p.typed(b"x")
        assert await _until(lambda: not dead._relay_on, 1.0), "relay не выключился"
        assert await dead.prompt("вопрос?", "preview") == ""  # вопросы ещё работают
        dead.stop()
    finally:
        p.close()
    print("OK relay: EOF снимает читателя, мёртвый приёмник гасит relay без падений")


async def test_box_vault_host_verdicts():
    """BoxVaultHost: y/да → True, n и пустой ответ → False, нет tty → False."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=1.0)
        ttymod.setraw(p.slave)
        arb.start()
        host = BoxVaultHost(arb)

        for typed, expect in ((b"y\n", True), ("да\n".encode(), True), (b"n\n", False),
                              (b"\n", False)):
            task = asyncio.create_task(host.confirm(SESSION, "git push", "origin main"))
            assert await _until(p.echo_on)
            p.typed(typed)
            got = await task
            assert got is expect, f"{typed!r}: {got} != {expect}"
        # ask — тот же путь (и тоже с таймаутом: молчание = отказ). Без grant
        # ask возвращает AskResult (granted-флаг), а не голый bool.
        task = asyncio.create_task(host.ask(SESSION, "GET api", "https://api/x"))
        assert await _until(p.echo_on)
        p.typed(b"\n")  # пустой ответ → отказ
        assert (await task).granted is False
        arb.stop()
        print("OK BoxVaultHost: y/да → разрешить, n/пусто/таймаут → отказ")
    finally:
        p.close()

    # Нет tty (пайп вместо терминала) — спрашивать некому, отказ без вопроса.
    r, w = os.pipe()
    try:
        host = BoxVaultHost(StdinArbiter(r, timeout=0.1))
        assert await host.confirm(SESSION, "git push", "x") is False
        assert (await host.ask(SESSION, "GET", "x")).granted is False
        print("OK BoxVaultHost: без tty confirm/ask = отказ (не вопрос в никуда)")
    finally:
        os.close(r)
        os.close(w)


async def test_assume_yes_does_not_ask():
    """assume_yes — подтверждаем без вопроса (неинтерактивный сценарий)."""
    r, w = os.pipe()
    try:
        host = BoxVaultHost(StdinArbiter(r, timeout=0.1), assume_yes=True)
        assert await host.confirm(SESSION, "x", "y") is True
        res = await host.ask(SESSION, "x", "y")
        assert res.granted is True and res.persisted is False, (
            "assume_yes: разово да, но НЕ пишем policy вслепую")
        print("OK BoxVaultHost: assume_yes подтверждает без вопроса (и не пишет policy)")
    finally:
        os.close(r)
        os.close(w)


async def _ask_typing(host: BoxVaultHost, p: "_Pty", answer: bytes,
                      grant: ScopeGrant | None) -> tuple[AskResult, str]:
    """Прогнать host.ask на pty, напечатав `answer`, и вернуть (результат, текст
    вопроса в stderr). stderr перехватываем — там печатается и подсказка ответа
    ([y/N] vs [y/N/a]), и точная будущая запись в policy (правило прозрачности)."""
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        task = asyncio.create_task(host.ask(SESSION, "GET docs", "GET https://api.svc/v1/docs/42", grant))
        assert await _until(p.echo_on), "вопрос не дошёл (эхо не включилось)"
        p.typed(answer)
        res = await task
    finally:
        sys.stderr = old
    return res, buf.getvalue()


async def test_ask_always_writes_narrow_grant():
    """grant + правка policy разрешена → предложен [y/N/a]; «a» пишет УЗКИЙ грант в
    policy, возвращает persisted=True, а текст ДО ввода показывает будущую запись."""
    secrets = _write_secrets()
    p = _Pty()
    try:
        before = _scope_prefixes(secrets)
        editor, allow = box_policy_access(secrets)
        assert allow, "временный secrets.toml обязан быть доступен на запись"
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        ttymod.setraw(p.slave)
        arb.start()
        host = BoxVaultHost(arb, policy=editor, allow_policy_edit=True)

        res, text = await _ask_typing(host, p, "a\n".encode(), _GRANT)
        arb.stop()
        # Прозрачность: подсказка [y/N/a] и точная будущая запись + отзыв — ДО ввода.
        assert "[y/N/a]" in text, text
        assert _GRANT.value in text and "scope.url_prefixes" in text, text
        assert "vault policy scope svc" in text, text
        # Запись узкая и ровно та, что показали.
        assert isinstance(res, AskResult) and res.granted and res.persisted, res
        after = _scope_prefixes(secrets)
        assert after == before + [_GRANT.value], (before, after)
        print("OK ASK «a»: узкий грант записан в policy, persisted=True, "
              "будущая запись показана до ввода")
    finally:
        p.close()
        secrets.unlink(missing_ok=True)
        Path(str(secrets) + ".lock").unlink(missing_ok=True)


async def test_ask_offer_sanitizes_terminal_control_bytes():
    """Управляющие байты в grant.value (модель управляет URL запроса, коннектор
    percent-декодирует путь) НЕ должны попадать на реальный tty: иначе ESC-склейка
    очистила бы экран и подрисовала фальшивый «безопасный» текст поверх ASK-вопроса
    (спуфинг прозрачности — нашло ревью). Проверяем, что в напечатанном тексте нет
    сырых control-байт, хотя грант их «содержит»."""
    evil = ScopeGrant(
        key="url_prefixes",
        # \x1b[2J = очистка экрана, \x1b[3J = скроллбэка, \x07 = звонок, \r = возврат
        value="https://api.svc/v1/docs/42\x1b[2J\x1b[3JВСЁ-ХОРОШО\x07\r",
        label="метка\x1b[31mкрасным\x1b[0m",
        secret="svc",
    )
    secrets = _write_secrets()
    p = _Pty()
    try:
        editor, _ = box_policy_access(secrets)
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        ttymod.setraw(p.slave)
        arb.start()
        host = BoxVaultHost(arb, policy=editor, allow_policy_edit=True)
        # печатаем «n» — нас интересует ТОЛЬКО текст вопроса, не запись
        _res, text = await _ask_typing(host, p, "n\n".encode(), evil)
        arb.stop()
        # ни одного управляющего байта (кроме наших \r\n\t разметки) в выводе
        bad = [c for c in text if ord(c) < 0x20 and c not in "\r\n\t"] + \
              [c for c in text if 0x7f <= ord(c) <= 0x9f]
        assert not bad, f"control-байты утекли на tty: {bad!r}\nтекст={text!r}"
        assert "\x1b" not in text, "ESC утёк — экран можно подделать"
        print("OK ASK-инъекция: управляющие байты grant.value обезврежены до вывода")
    finally:
        p.close()
        secrets.unlink(missing_ok=True)
        Path(str(secrets) + ".lock").unlink(missing_ok=True)


async def test_ask_yes_is_once_policy_untouched():
    """«y» при доступной «навсегда» → разовый грант: granted, НЕ persisted, policy
    не тронут (кред уходит только в этот запрос)."""
    secrets = _write_secrets()
    p = _Pty()
    try:
        before = _scope_prefixes(secrets)
        editor, allow = box_policy_access(secrets)
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        ttymod.setraw(p.slave)
        arb.start()
        host = BoxVaultHost(arb, policy=editor, allow_policy_edit=allow)
        res, text = await _ask_typing(host, p, b"y\n", _GRANT)
        arb.stop()
        assert res.granted and not res.persisted, res
        assert _scope_prefixes(secrets) == before, "policy тронут при разовом гранте"
        print("OK ASK «y»: разовый грант, policy не тронут")
    finally:
        p.close()
        secrets.unlink(missing_ok=True)
        Path(str(secrets) + ".lock").unlink(missing_ok=True)


async def test_ask_no_grant_only_binary():
    """grant=None → «навсегда» не предлагается: подсказка [y/N], и «a» трактуется
    как НЕ-да (отказ), policy недостижим."""
    secrets = _write_secrets()
    p = _Pty()
    try:
        editor, allow = box_policy_access(secrets)
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        ttymod.setraw(p.slave)
        arb.start()
        host = BoxVaultHost(arb, policy=editor, allow_policy_edit=allow)
        res, text = await _ask_typing(host, p, b"a\n", None)  # grant=None
        arb.stop()
        assert "[y/N]" in text and "[y/N/a]" not in text, text
        assert "только разово" in text, text  # честно объяснили, почему нет «навсегда»
        assert not res.granted and not res.persisted, res  # «a» без предложения = отказ
        assert _scope_prefixes(secrets) == ["https://api.svc/v1/allowed"], "policy тронут"
        print("OK ASK без grant: только [y/N], «a» = отказ, policy не тронут")
    finally:
        p.close()
        secrets.unlink(missing_ok=True)
        Path(str(secrets) + ".lock").unlink(missing_ok=True)


async def test_ask_policy_edit_disabled_only_binary():
    """Правка policy запрещена (allow_policy_edit=False / policy=None) → [y/N], без
    третьего варианта; «a» = отказ, policy не пишется."""
    secrets = _write_secrets()
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        ttymod.setraw(p.slave)
        arb.start()
        # editor есть, но правка запрещена (эмулируем RO secrets.toml).
        host = BoxVaultHost(arb, policy=PolicyEditor(secrets), allow_policy_edit=False)
        res, text = await _ask_typing(host, p, b"a\n", _GRANT)
        arb.stop()
        assert "[y/N]" in text and "[y/N/a]" not in text, text
        assert "policy недоступна на запись" in text, text
        assert not res.granted and not res.persisted, res
        assert _scope_prefixes(secrets) == ["https://api.svc/v1/allowed"], "policy тронут"
        print("OK ASK при запрете правки: только [y/N], «a» = отказ")
    finally:
        p.close()
        secrets.unlink(missing_ok=True)
        Path(str(secrets) + ".lock").unlink(missing_ok=True)


async def test_ask_persist_failure_is_honest():
    """Сбой записи гранта (битый secrets.toml → PolicyError) → доступ РАЗОВО
    (granted=True, persisted=False) и честное «в policy НЕ записано», не падение."""
    secrets = _write_secrets("это = не ] валидный [ TOML\n")  # правка упадёт
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        ttymod.setraw(p.slave)
        arb.start()
        host = BoxVaultHost(arb, policy=PolicyEditor(secrets), allow_policy_edit=True)
        res, text = await _ask_typing(host, p, "a\n".encode(), _GRANT)
        arb.stop()
        assert res.granted and not res.persisted, res
        assert "НЕ записано" in text, text  # честно сказали в терминал
        print("OK ASK сбой записи: granted=True, persisted=False, честное сообщение")
    finally:
        p.close()
        secrets.unlink(missing_ok=True)
        Path(str(secrets) + ".lock").unlink(missing_ok=True)


async def test_ask_grant_wrapper_calls_new_host():
    """Обратная совместимость: прокси зовёт vault.host.ask_grant — он должен
    подхватить новый 4-арг BoxVaultHost.ask и вернуть persisted=True на «a»."""
    secrets = _write_secrets()
    p = _Pty()
    try:
        before = _scope_prefixes(secrets)
        editor, allow = box_policy_access(secrets)
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        ttymod.setraw(p.slave)
        arb.start()
        host = BoxVaultHost(arb, policy=editor, allow_policy_edit=allow)
        # grant.secret пустой — как приходит от коннектора; прокси штампует secret,
        # но ask_grant передаёт grant как есть. Возьмём _GRANT с уже проставленным.
        task = asyncio.create_task(
            ask_grant(host, SESSION, "GET docs", "GET https://api.svc/v1/docs/42", _GRANT))
        assert await _until(p.echo_on)
        p.typed("a\n".encode())
        res = await task
        arb.stop()
        assert isinstance(res, AskResult) and res.granted and res.persisted, res
        assert _scope_prefixes(secrets) == before + [_GRANT.value]
        print("OK ask_grant: обёртка совместимости подхватила новый BoxVaultHost.ask")
    finally:
        p.close()
        secrets.unlink(missing_ok=True)
        Path(str(secrets) + ".lock").unlink(missing_ok=True)


async def test_ask_timeout_denies():
    """Молчание оператора на ASK-грант → таймаут → отказ (granted=False), не запись."""
    secrets = _write_secrets()
    p = _Pty()
    try:
        editor, allow = box_policy_access(secrets)
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=0.15)
        ttymod.setraw(p.slave)
        arb.start()
        host = BoxVaultHost(arb, policy=editor, allow_policy_edit=allow)
        res = await host.ask(SESSION, "GET docs", "GET https://api.svc/v1/docs/42", _GRANT)
        arb.stop()
        assert not res.granted and not res.persisted, res
        assert _scope_prefixes(secrets) == ["https://api.svc/v1/allowed"]
        print("OK ASK-грант: таймаут → отказ, policy не тронут")
    finally:
        p.close()
        secrets.unlink(missing_ok=True)
        Path(str(secrets) + ".lock").unlink(missing_ok=True)


def test_box_policy_access_readonly():
    """box_policy_access: RO secrets.toml → allow=False (не предлагаем «навсегда»),
    записываемый → allow=True. Синхронный тест (файловые права)."""
    secrets = _write_secrets()
    try:
        _editor, allow = box_policy_access(secrets)
        assert allow, "0600-файл в записываемом каталоге должен быть доступен на запись"
        os.chmod(secrets, 0o400)  # только чтение
        _editor, allow_ro = box_policy_access(secrets)
        assert not allow_ro, "RO secrets.toml → правка недоступна"
        print("OK box_policy_access: RW → allow, RO-файл → deny (честно, без падения)")
    finally:
        os.chmod(secrets, 0o600)
        secrets.unlink(missing_ok=True)


def main() -> None:
    asyncio.run(test_relay_survives_prompt())
    asyncio.run(test_prompt_timeout_denies())
    asyncio.run(test_prompt_cancel_restores_terminal())
    asyncio.run(test_tail_after_answer_goes_to_session())
    asyncio.run(test_relay_stops_on_eof_and_dead_sink())
    asyncio.run(test_box_vault_host_verdicts())
    asyncio.run(test_assume_yes_does_not_ask())
    asyncio.run(test_ask_always_writes_narrow_grant())
    asyncio.run(test_ask_offer_sanitizes_terminal_control_bytes())
    asyncio.run(test_ask_yes_is_once_policy_untouched())
    asyncio.run(test_ask_no_grant_only_binary())
    asyncio.run(test_ask_policy_edit_disabled_only_binary())
    asyncio.run(test_ask_persist_failure_is_honest())
    asyncio.run(test_ask_grant_wrapper_calls_new_host())
    asyncio.run(test_ask_timeout_denies())
    test_box_policy_access_readonly()
    print("ALL BOX-TTY OK")


if __name__ == "__main__":
    main()
