"""Офлайн-тест кошелька секретов (modules/wallet + bin/wallet CLI).

Покрыто: авторизация per-session токеном, список секретов без значений и
только по policy, отказ по шаблону команды, подтверждение (allow/deny),
редакция значений секретов в выводе, отказ при широких правах файла,
CLI end-to-end через живого демона.

Запуск: .venv/bin/python tests/wallet_test.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp  # noqa: E402

from orchestrator.modules.wallet.module import WalletModule  # noqa: E402
from vault.redact import _redact  # noqa: E402

ROOT = Path(__file__).parent.parent


def make_env(tmp: Path):
    """Фейковое ядро + конфиг: одна сессия dev, secrets.toml с двумя секретами."""
    home = tmp / "home-dev"
    home.mkdir()
    cwd = tmp / "proj"
    cwd.mkdir()
    session = SimpleNamespace(name="dev", session_dir=tmp / "dev")

    secrets_file = tmp / "secrets.toml"
    secrets_file.write_text(
        '[secrets.deploy]\n'
        'value = "S3CR3T-DEPLOY"\n'
        'env = "GITHUB_TOKEN"\n'
        'description = "deploy token"\n'
        'sessions = ["de*"]\n'
        'commands = ["sh -c *", "gh *", "git *"]\n'
        'confirm = false\n'
        '\n'
        '[secrets.other]\n'
        'value = "S3CR3T-OTHER"\n'
        'env = "OTHER_TOKEN"\n'
        'sessions = ["prod-*"]\n'          # НЕ для сессии dev
        'commands = ["*"]\n'
        'confirm = false\n'
        '\n'
        '[secrets.careful]\n'
        'value = "S3CR3T-CAREFUL"\n'
        'env = "CAREFUL"\n'
        'sessions = ["*"]\n'
        'commands = ["sh -c *"]\n'
        'confirm = true\n'
        '\n'
        '[secrets.hostgh]\n'                # host-passthrough: без value/env
        'description = "gh на хосте (keyring)"\n'
        'sessions = ["dev"]\n'
        'commands = ["sh -c *"]\n'
        'confirm = false\n'
    )
    os.chmod(secrets_file, 0o600)

    confirm_answer = {"value": True, "asked": 0}

    async def request_confirmation(session, tool, description, preview, timeout=300.0):
        confirm_answer["asked"] += 1
        return confirm_answer["value"]

    notices = []

    async def notice(session, text):
        notices.append(text)

    core = SimpleNamespace(
        manager=SimpleNamespace(
            list_all=lambda: [session],
            get=lambda n: session if n == "dev" else None,
            effective_cwd=lambda s: cwd,
            session_home=lambda s: home,
            env_hooks=[],
            path_hooks=[],
            launch_hooks=[],
            session_delete_hooks=[],
        ),
        session_hooks=[],
        output_redactors=[],
        # has()=False → бабла нет (как фоновый вызов) → notice всегда шлётся,
        # чтобы тест проверял путь уведомления/редакции.
        bubbles=SimpleNamespace(
            append=_async_noop, append_background=_async_noop, has=lambda name: False,
        ),
        notice=notice,
        t=lambda k, **kw: kw.get("line", k),
        _record=lambda *a, **kw: None,
        request_confirmation=request_confirmation,
    )
    core.sent_notices = notices  # интроспекция для тестов notice-поведения
    config = SimpleNamespace(
        wallet_secrets_file=secrets_file, sandbox="bwrap", sessions_dir=tmp,
        wallet_guard=True,
        # Тумблер правки policy: модуль отдаёт его VaultHost'у (ASK-грант
        # «навсегда»). В этом харнесе кошелёк проверяется без ASK — держим off.
        wallet_policy_edit=False,
    )
    return core, config, home, secrets_file, confirm_answer


async def _async_noop(*a, **kw):
    return None


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="wallet_test_"))
    core, config, home, secrets_file, confirm = make_env(tmp)

    module = WalletModule(config)
    await module.start(core)
    try:
        wallet_cfg = json.loads((home / ".wallet.json").read_text())
        assert wallet_cfg["session"] == "dev" and wallet_cfg["token"]
        assert oct((home / ".wallet.json").stat().st_mode & 0o777) == "0o600"
        print("OK provision: ~/.wallet.json в доме сессии, 0600")

        url = wallet_cfg["url"]
        good = {"Authorization": f"Bearer {wallet_cfg['token']}"}
        bad = {"Authorization": "Bearer wrong-token"}
        async with aiohttp.ClientSession() as http:
            # 401 с чужим токеном
            async with http.get(f"{url}/secrets", headers=bad) as r:
                assert r.status == 401
            print("OK auth: чужой токен → 401")

            # список: только разрешённые сессии dev, БЕЗ значений
            async with http.get(f"{url}/secrets", headers=good) as r:
                assert r.status == 200
                listed = await r.json()
            names = {s["name"] for s in listed}
            assert names == {"deploy", "careful", "hostgh"}, names
            assert "S3CR3T" not in json.dumps(listed)
            print("OK /secrets: policy по сессии, значений нет")

            # команда вне шаблонов → 403 без исполнения
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy", "cmd": ["rm", "-rf", "/"]}) as r:
                assert r.status == 403
            print("OK /run: команда вне шаблона → 403")

            # чужой секрет → 403
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "other", "cmd": ["sh", "-c", "true"]}) as r:
                assert r.status == 403
            print("OK /run: секрет чужой сессии → 403")

            # исполнение: секрет в env ребёнка, значение отредактировано в выводе
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy",
                                       "cmd": ["sh", "-c", "echo t=$GITHUB_TOKEN; pwd"]}) as r:
                assert r.status == 200
                data = await r.json()
            assert data["code"] == 0, data
            assert "S3CR3T-DEPLOY" not in data["stdout"], data
            assert "t=•••" in data["stdout"], data
            assert str(core.manager.effective_cwd(None)) in data["stdout"]  # cwd = проект
            print("OK /run: исполнение на хосте, значение → •••, cwd проекта")

            # confirm=true: отказ кнопкой → 403, команда не исполнялась
            confirm["value"] = False
            marker = tmp / "should_not_exist"
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "careful",
                                       "cmd": ["sh", "-c", f"touch {marker}"]}) as r:
                assert r.status == 403
            assert confirm["asked"] == 1 and not marker.exists()
            print("OK confirm: deny кнопкой → 403, команда не исполнялась")

            # confirm=true: allow → исполняется
            confirm["value"] = True
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "careful",
                                       "cmd": ["sh", "-c", "echo c=$CAREFUL"]}) as r:
                data = await r.json()
            assert data["code"] == 0 and "c=•••" in data["stdout"], data
            print("OK confirm: allow → исполнено, вывод отредактирован")

            # права шире 0600 → секреты не грузятся, всё в отказ
            os.chmod(secrets_file, 0o644)
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy", "cmd": ["sh", "-c", "true"]}) as r:
                assert r.status == 403
            async with http.get(f"{url}/secrets", headers=good) as r:
                assert await r.json() == []
            os.chmod(secrets_file, 0o600)
            print("OK права 0644 → секреты отключены целиком")

            # host-passthrough (без value/env): команда на хосте с ХОСТОВЫМ
            # окружением, секрет в env НЕ инжектится. Проверяем: доступен, и в
            # env нет инъекции (GITHUB_TOKEN/CAREFUL не появились от wallet).
            async with http.get(f"{url}/secrets", headers=good) as r:
                names = {s["name"] for s in await r.json()}
            assert "hostgh" in names, names
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "hostgh",
                                       "cmd": ["sh", "-c", "echo hg=[$GITHUB_TOKEN][$CAREFUL]"]}) as r:
                data = await r.json()
            assert data["code"] == 0 and "hg=[][]" in data["stdout"], data
            print("OK host-passthrough: команда на хосте без инъекции секрета в env")

            # guard прозрачен: gh auth token разрешён commands (gh *), но guard
            # рубит его 403 с ОБЪЯСНЯЮЩИМ reason (доходит до терминала модели)
            n_before = len(core.sent_notices)
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy",
                                       "cmd": ["gh", "auth", "token"]}) as r:
                assert r.status == 403
                body = await r.json()
            assert "reason" in body and "токен" in body["reason"], body
            # reason уводит модель к обычному git push (HTTPS auth на хосте),
            # а не к SSH-костылю через печать токена (кейс noos).
            assert "git push" in body["reason"], body
            # notice НЕ шлётся: `gh auth token` — самокорректирующийся отказ,
            # который поллер Claude Code («PR status») дёргает периодически;
            # operator-notice спамил бы чат. Аудит-строка в бабл — отдельно.
            assert len(core.sent_notices) == n_before, core.sent_notices
            print("OK guard: gh auth token → 403 с объяснением, БЕЗ operator-notice")

            # guard git-RCE (`git -c`) — тоже 403, НО notice ШЛЁТСЯ: это реальный
            # сигнал оператору, не безобидный фоновый опрос.
            n_before = len(core.sent_notices)
            async with http.post(f"{url}/run", headers=good,
                                 json={"secret": "deploy",
                                       "cmd": ["git", "-c", "core.sshCommand=evil", "push"]}) as r:
                assert r.status == 403
            assert len(core.sent_notices) == n_before + 1, core.sent_notices
            print("OK guard: git -c push → 403 С operator-notice (сигнал оператору)")

        # редакция: вложенные значения, длинные первыми
        out = _redact(b"a=S3CR3T-DEPLOY b=S3CR3T-OTHER", ["S3CR3T-DEPLOY", "S3CR3T-OTHER"])
        assert out == "a=••• b=•••", out
        print("OK _redact: все известные значения вымараны")

        # ── policy команд: голые имена, дефолт host, guard, deny ──
        from orchestrator.modules.wallet.module import (
            Secret, DEFAULT_HOST_COMMANDS, _always_denied,
        )

        def mk(name, value, env, sessions, commands, deny=(), allow_unsafe=False):
            return Secret(name, value, env, "", sessions, commands, deny, allow_unsafe,
                          False, False)  # confirm, shared

        # commands (allow): голое имя = любой вызов; шаблон с пробелом = fnmatch
        s_bare = mk("x", "", "", ("*",), ("gh", "curl https://api/*"))
        assert s_bare.command_allowed(["gh", "pr", "create"])          # gh → любой
        assert s_bare.command_allowed(["curl", "https://api/v1/x"])    # шаблон
        assert not s_bare.command_allowed(["curl", "https://evil/x"])  # вне шаблона
        assert not s_bare.command_allowed(["wget", "x"])               # не в списке
        # голое имя в commands — только имя инструмента, не «аргумент где-то»
        assert not s_bare.command_allowed(["git", "remote", "add", "x", "gh"])
        # host-passthrough без commands → дефолтный набор gh/git/ssh/scp
        s_def = mk("h", "", "", ("*",), ())
        assert s_def.effective_commands == DEFAULT_HOST_COMMANDS
        assert s_def.command_allowed(["git", "push"]) and s_def.command_allowed(["ssh", "host"])
        assert not s_def.command_allowed(["cat", "/etc/passwd"])       # не инструмент
        # inject без commands → ничего (сырой токен не открываем по умолчанию)
        assert mk("i", "V", "TOK", ("*",), ()).effective_commands == ()

        # guard (_always_denied): печать токена и git-RCE — независимо от commands
        assert _always_denied(["gh", "auth", "token"]) is not None
        assert _always_denied(["gh", "--verbose", "auth", "token"]) is not None  # флаг до auth не спасает
        assert _always_denied(["gh", "pr", "create", "--title", "auth token"]) is None  # не ложно-блок
        assert _always_denied(["gh", "auth", "login"]) is None  # не печатает токен
        assert _always_denied(["gh", "auth", "status", "--show-token"]) is not None
        assert _always_denied(["gh", "auth", "status"]) is None        # без --show-token ок
        assert _always_denied(["git", "-c", "core.sshCommand=evil", "push"]) is not None
        assert _always_denied(["git", "push", "ext::sh -c evil"]) is not None
        assert _always_denied(["git", "push", "--receive-pack=evil"]) is not None
        assert _always_denied(["git", "push", "origin", "main"]) is None  # обычный push ок

        # deny (per-secret, поверх commands): инструмент разрешён, флаг заблокирован
        s_deny = mk("d", "", "", ("*",), ("git",), deny=("--force", "git push --hard*"))
        assert s_deny.command_allowed(["git", "push"])                     # allow есть
        assert s_deny.denied_by(["git", "push", "--force"]) == "--force"   # флаг где угодно
        assert s_deny.denied_by(["git", "push", "--hard", "x"]) is not None  # шаблон
        assert s_deny.denied_by(["git", "push"]) is None                   # обычный — можно
        print("OK policy: allow(голые/шаблон) + guard(gh-token/git-RCE) + deny(флаги)")

        # CLI end-to-end через живого демона (stdlib-скрипт, как в песочнице)
        # CLI — в потоке: subprocess.run в самом event loop заблокировал бы
        # демона (он крутится в этом же цикле) → дедлок.
        env = {**os.environ, "WALLET_FILE": str(home / ".wallet.json")}

        async def cli(*args):
            return await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(ROOT / "bin" / "wallet"), *args],
                capture_output=True, text=True, env=env, timeout=30,
            )

        r = await cli("ls")
        assert r.returncode == 0 and "deploy" in r.stdout, (r.stdout, r.stderr)
        r = await cli("run", "deploy", "--", "sh", "-c", "echo cli=$GITHUB_TOKEN")
        assert r.returncode == 0 and "cli=•••" in r.stdout, (r.stdout, r.stderr)
        r = await cli("run", "deploy", "--", "evil-cmd")
        # policy-промах теперь тоже даёт прозрачный reason (не глухое «denied»).
        assert r.returncode == 3 and "не в списке" in r.stderr, (r.returncode, r.stderr)
        print("OK bin/wallet: ls + run + отказ policy (end-to-end)")
    finally:
        await module.stop()

    print("ALL WALLET OK")


async def test_wallet():
    await main()

if __name__ == "__main__":
    asyncio.run(main())
