"""Учётка Claude Code: статус профиля и вход из чата (core/auth.py).

Гоняем на ФЕЙКОВОМ `claude` — питон-скрипте, повторяющем реальный диалог CLI
2.1.233 дословно (снято живьём):

    Opening browser to sign in…
    If the browser didn't open, visit: <OSC-8 ссылка, URL напечатан дважды>
    Paste code here if prompted >

Проверяем:
  • разбор `auth status --json` и «явное не залогинен» (пустой ответ ≠ разлогин);
  • ссылка вынимается ОДНА, несмотря на удвоение в OSC-8 гиперссылке;
  • вход: старт → ссылка → код в stdin → статус залогинен, процесс прибран;
  • код НЕ утекает в хвост вывода (PTY эхает его обратно — маскируем);
  • второй /login не плодит процессы, отмена убивает процесс;
  • нет бинаря / нет ссылки → AuthError, а не зависание.

Запуск: .venv/bin/python -m pytest tests/auth_login_test.py
"""
from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import auth  # noqa: E402

# Фейковый claude: `auth status --json` печатает состояние из файла профиля,
# `auth login` — диалог CLI и после кода дописывает «залогинен».
FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, os, sys, time
cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
state = os.path.join(cfg, "state.json")
argv = sys.argv[1:]
if argv[:2] == ["auth", "status"]:
    try:
        with open(state) as fh:
            print(fh.read())
    except OSError:
        print(json.dumps({"loggedIn": False}))
    sys.exit(0)
if argv[:2] == ["auth", "login"]:
    url = ("https://claude.com/cai/oauth/authorize?code=true&client_id=x"
           "&state=STATE-%s" % os.getpid())
    sys.stdout.write("Opening browser to sign in\\u2026\\r\\n")
    # OSC-8 гиперссылка: URL печатается дважды подряд, как в настоящем CLI.
    sys.stdout.write("If the browser didn't open, visit: "
                     "\\x1b]8;;%s\\x1b\\\\%s\\x1b]8;;\\x1b\\\\\\r\\n" % (url, url))
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "BAD":
        sys.stdout.write("\\r\\nInvalid code\\r\\n")
        sys.exit(1)
    with open(state, "w") as fh:
        json.dump({"loggedIn": True, "authMethod": "claude.ai",
                   "email": "op@example.com", "subscriptionType": "max"}, fh)
    sys.stdout.write("\\r\\nLogin successful\\r\\n")
    sys.exit(0)
sys.exit(2)
'''


def _fake_claude(tmp: Path) -> str:
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / "fake-claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _profile(tmp: Path, logged_in: bool | None) -> Path:
    """Каталог профиля с заданным состоянием учётки (None — состояния нет)."""
    cfg = tmp / "profile"
    cfg.mkdir(parents=True, exist_ok=True)
    if logged_in is not None:
        (cfg / "state.json").write_text(json.dumps({
            "loggedIn": logged_in, "authMethod": "claude.ai",
            "email": "op@example.com", "subscriptionType": "max",
        }))
    return cfg


def test_parse_status_and_logged_out():
    """JSON вынимается из шума; «не знаю» и «разлогинен» — разные вещи."""
    st = auth.parse_status(b'Update installed\n{"loggedIn": true, "email": "a@b.c"}\n')
    assert st["loggedIn"] is True and st["email"] == "a@b.c", st
    assert not auth.logged_out(st)
    assert auth.logged_out(auth.parse_status(b'{"loggedIn": false}'))
    # Пустой/битый ответ — НЕ повод объявлять разлогин (иначе ложная тревога
    # на каждом таймауте `auth status`).
    assert auth.parse_status(b"") == {}
    assert not auth.logged_out({})
    assert not auth.logged_out(auth.parse_status(b"claude: command not found"))
    print("OK статус: разбор JSON, пустой ответ ≠ разлогин")


def test_extract_url_survives_osc8_duplication():
    """OSC-8 печатает URL дважды подряд — забираем ровно один, целиком."""
    url = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a"
           "&state=7Ycb9hIQvP6ReWgScK9puJMirGOW")
    raw = ("If the browser didn't open, visit: "
           f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\\r\n").encode()
    assert auth.extract_url(raw) == url, auth.extract_url(raw)
    assert auth.extract_url(b"no link here") is None
    print("OK ссылка: одна, не склеенная с дублем из OSC-8")


def test_account_line():
    assert auth.account_line({"email": "op@example.com", "authMethod": "claude.ai",
                              "subscriptionType": "max"}) == \
        "op@example.com · claude.ai · max"
    assert auth.account_line({}) == ""
    print("OK строка учётки для чата")


async def test_login_flow_end_to_end(tmp_path):
    """Полный вход: ссылка → код → залогинен; код не утёк в хвост вывода."""
    claude = _fake_claude(tmp_path)
    cfg = _profile(tmp_path, logged_in=False)
    mgr = auth.LoginManager(claude)

    assert auth.logged_out(await mgr.status(cfg)), "профиль должен быть разлогинен"

    flow = await mgr.start(cfg)
    assert flow.url and flow.url.startswith("https://claude.com/cai/oauth/authorize"), flow.url
    # Ссылка выдана и процесс ждёт код — второй /login не поднимает второй процесс.
    for _ in range(50):
        if flow.waiting_code():
            break
        await asyncio.sleep(0.1)
    assert flow.waiting_code(), flow.tail()
    assert await mgr.start(cfg) is flow

    st = await mgr.submit(cfg, "CODE-FROM-PAGE-4242")
    assert st.get("loggedIn") is True, st
    assert auth.account_line(st) == "op@example.com · claude.ai · max"
    # Секрет не должен всплыть в чате: PTY эхает введённый код обратно в вывод.
    assert "CODE-FROM-PAGE-4242" not in flow.tail(), flow.tail()
    assert mgr.get(cfg) is None, "завершённый вход не остаётся в реестре"
    print("OK вход: ссылка → код → залогинен, код замаскирован")


async def test_login_bad_code_reports_not_logged_in(tmp_path):
    """Неверный код — процесс падает, статус остаётся «не залогинен»."""
    claude = _fake_claude(tmp_path)
    cfg = _profile(tmp_path, logged_in=False)
    mgr = auth.LoginManager(claude)
    await mgr.start(cfg)
    st = await mgr.submit(cfg, "BAD")
    assert auth.logged_out(st), st
    print("OK неверный код: остаёмся разлогинены, без вранья об успехе")


async def test_cancel_kills_process(tmp_path):
    """Отмена входа убивает процесс и чистит реестр (брошенный вход не висит)."""
    claude = _fake_claude(tmp_path)
    cfg = _profile(tmp_path, logged_in=False)
    mgr = auth.LoginManager(claude)
    flow = await mgr.start(cfg)
    proc = flow._handle.process
    assert await mgr.cancel(cfg) is True
    await asyncio.wait_for(proc.wait(), 5)
    assert mgr.get(cfg) is None
    assert await mgr.cancel(cfg) is False
    print("OK отмена: процесс убит, реестр пуст")


async def test_missing_binary_raises(tmp_path):
    """Нет бинаря claude — человеческая ошибка, а не зависание на таймауте."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    mgr = auth.LoginManager(str(tmp_path / "no-such-claude"))
    try:
        await mgr.start(tmp_path)
    except auth.AuthError:
        pass
    else:
        raise AssertionError("ожидали AuthError")
    assert await mgr.status(tmp_path) == {}
    print("OK нет бинаря: AuthError и пустой статус")


async def test_no_url_raises_and_cleans_up(tmp_path):
    """CLI не напечатал ссылку — AuthError с хвостом вывода, процесс прибран."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    silent = tmp_path / "silent-claude"
    silent.write_text("#!/bin/sh\necho 'auth disabled by policy'\nsleep 30\n")
    silent.chmod(silent.stat().st_mode | stat.S_IEXEC)
    mgr = auth.LoginManager(str(silent))
    auth.URL_TIMEOUT_SAVED = auth.URL_TIMEOUT
    auth.URL_TIMEOUT = 2.0
    try:
        try:
            await mgr.start(tmp_path)
        except auth.AuthError as e:
            assert "auth disabled by policy" in str(e), str(e)
        else:
            raise AssertionError("ожидали AuthError")
    finally:
        auth.URL_TIMEOUT = auth.URL_TIMEOUT_SAVED
    assert mgr.get(tmp_path) is None
    print("OK нет ссылки: AuthError с хвостом, реестр чист")


def test_looks_like_code():
    """Что считаем кодом: точный признак — `state` из выданной нами ссылки."""
    state = "7Ycb9hIQvP6ReWgScK9puJMirGOW"
    assert auth.looks_like_code(f"ac_01H8xk9#{state}", state)
    assert auth.looks_like_code("aBcD1234efGH5678ijKL")           # голый длинный код
    # НЕ код: обычные сообщения модели, команды и ссылки (единственный
    # правдоподобный «длинный токен без пробелов», который шлют модели).
    assert not auth.looks_like_code("посмотри логи сессии noos, там пусто")
    assert not auth.looks_like_code("/login")
    assert not auth.looks_like_code("https://github.com/aadegtyarev/claude-orchestrator/pull/1")
    assert not auth.looks_like_code("короткий")
    assert not auth.looks_like_code("")
    print("OK признак кода: state точный, ссылки/команды/проза не перехватываются")


def _core(claude_bin: str, config_dir: Path, sessions=()):
    """Ядро-заглушка: ровно те поля, которые трогает ветка учётки."""
    from types import SimpleNamespace

    from orchestrator.core.app import OrchestratorCore
    from orchestrator.core.texts import get_texts

    core = OrchestratorCore.__new__(OrchestratorCore)
    core._texts = get_texts("ru")
    core.config = SimpleNamespace(claude_bin=claude_bin)
    core.manager = SimpleNamespace(
        config_dir_of=lambda session: config_dir,
        list_all=lambda: list(sessions),
        profile_of=lambda session: "work",
    )
    core.logins = auth.LoginManager(claude_bin)
    core._logged_out = set()
    return core


async def test_core_login_command_live_account(tmp_path):
    """Голый /login на живой учётке не логинится заново, а показывает аккаунт."""
    claude = _fake_claude(tmp_path)
    cfg = _profile(tmp_path, logged_in=True)
    core = _core(claude, cfg)
    text = await core.login_command(None, "")
    assert "op@example.com" in text and "/login force" in text, text
    assert core.logins.get(cfg) is None, "процесс входа поднимать было незачем"
    assert not core.auth_known_out(None)
    print("OK /login на живой учётке: аккаунт + подсказка force, без процесса")


async def test_core_login_command_expired_account(tmp_path):
    """Учётка протухла → /login поднимает вход и присылает ссылку; код чинит всё."""
    from types import SimpleNamespace

    claude = _fake_claude(tmp_path)
    cfg = _profile(tmp_path, logged_in=False)
    sessions = [SimpleNamespace(name="noos", running=True)]
    core = _core(claude, cfg, sessions)

    # Релей спросил подтверждение — учётка помечена протухшей, и с этого момента
    # каждое сообщение оператора получает пометку «модель его не увидит».
    assert await core._auth_expired(sessions[0]) is True
    assert core.auth_known_out(None) is True

    text = await core.login_command(None, "")
    assert "https://claude.com/cai/oauth/authorize" in text, text
    assert core.login_active(None) is True
    flow = core.logins.get(cfg)
    assert flow is not None and flow.state, "state нужен для распознавания кода"
    assert core.is_login_code(None, f"ac_01H8xk9#{flow.state}") is True

    result = await core.login_code(None, f"ac_01H8xk9#{flow.state}")
    assert "op@example.com" in result and "noos" in result, result
    assert core.auth_known_out(None) is False, "флаг протухшей учётки снят"
    assert core.login_active(None) is False
    print("OK /login при протухшей учётке: ссылка → код → аккаунт жив, флаг снят")


async def test_core_login_cancel_and_unknown_arg(tmp_path):
    claude = _fake_claude(tmp_path)
    cfg = _profile(tmp_path, logged_in=False)
    core = _core(claude, cfg)
    assert "не начат" in await core.login_command(None, "cancel")
    await core.login_command(None, "force")
    assert core.login_active(None) is True
    assert "отмен" in (await core.login_command(None, "cancel")).lower()
    assert core.login_active(None) is False
    try:
        await core.login_command(None, "чепуха")
    except Exception as e:
        assert "чепуха" in str(e), e
    else:
        raise AssertionError("ожидали отказ на неизвестный аргумент")
    print("OK /login cancel и неизвестный аргумент")


def main():
    test_parse_status_and_logged_out()
    test_looks_like_code()
    test_extract_url_survives_osc8_duplication()
    test_account_line()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        asyncio.run(test_login_flow_end_to_end(tmp / "a"))
        asyncio.run(test_login_bad_code_reports_not_logged_in(tmp / "b"))
        asyncio.run(test_cancel_kills_process(tmp / "c"))
        asyncio.run(test_missing_binary_raises(tmp / "d"))
        asyncio.run(test_no_url_raises_and_cleans_up(tmp / "e"))
        asyncio.run(test_core_login_command_live_account(tmp / "f"))
        asyncio.run(test_core_login_command_expired_account(tmp / "g"))
        asyncio.run(test_core_login_cancel_and_unknown_arg(tmp / "h"))
    print("OK auth_login_test")


if __name__ == "__main__":
    main()
