"""vault CLI — автономный запуск БЕЗ оркестратора (критерий фазы 1).

Проверяем: (1) standalone-демон (build_daemon + TtyVaultHost) + клиент bin/wallet
через ~/.wallet.json работают end-to-end без всякого оркестратора; (2) TtyVaultHost
подтверждает по assume_yes и отказывает без tty; (3) policy-CLI просматривает и
правит secrets.toml.

Запуск: .venv/bin/python tests/vault_cli_test.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.cli import build_daemon, cmd_policy, main, write_wallet  # noqa: E402
from vault.tty_host import TtyVaultHost  # noqa: E402

ROOT = Path(__file__).parent.parent


def _secrets(tmp: Path) -> Path:
    f = tmp / "secrets.toml"
    f.write_text(
        '[secrets.tok]\nvalue="SEKRET-VAL"\nenv="TOK"\nsessions=["*"]\n'
        'commands=["sh -c *"]\nconfirm=false\n\n'
        '[secrets.key]\nshared=true\nvalue="SHV"\nsessions=["*"]\nconfirm=false\n'
    )
    os.chmod(f, 0o600)
    return f


# ── end-to-end: vault-демон + bin/wallet клиент, БЕЗ оркестратора ───
async def _standalone():
    tmp = Path(tempfile.mkdtemp(prefix="vault_cli_"))
    cwd = tmp / "proj"
    cwd.mkdir()
    daemon = build_daemon(_secrets(tmp), guard_on=True, assume_yes=True)
    await daemon.start()
    try:
        token = daemon.issue_token("local", cwd)
        wallet_file = tmp / ".wallet.json"
        write_wallet(wallet_file, daemon.url, token, "local")
        assert oct(wallet_file.stat().st_mode & 0o777) == "0o600"
        cfg = json.loads(wallet_file.read_text())
        assert cfg["url"] == daemon.url and cfg["session"] == "local"
        print("OK standalone: ~/.wallet.json (url+token, 0600) записан")

        env = {**os.environ, "WALLET_FILE": str(wallet_file)}

        async def wallet(*args):
            # subprocess.run в этом же event loop заморозил бы демона → в поток.
            return await asyncio.to_thread(
                subprocess.run,
                [sys.executable, str(ROOT / "bin" / "wallet"), *args],
                capture_output=True, text=True, env=env, timeout=30,
            )

        r = await wallet("ls")
        assert r.returncode == 0 and "tok" in r.stdout and "key" in r.stdout, (r.stdout, r.stderr)
        print("OK standalone: `wallet ls` через демон vault (без оркестратора)")

        # исполнение под секретом на хосте в cwd из токена; значение вымарано
        r = await wallet("run", "tok", "--", "sh", "-c", "echo t=$TOK; pwd")
        assert r.returncode == 0 and "t=•••" in r.stdout, (r.stdout, r.stderr)
        assert "SEKRET-VAL" not in r.stdout and str(cwd.resolve()) in r.stdout, r.stdout
        print("OK standalone: `wallet run` исполнил на хосте, значение → •••, cwd токена")

        # shared → значение выдаётся
        r = await wallet("get", "key")
        assert r.returncode == 0 and r.stdout.strip() == "SHV", (r.stdout, r.stderr)
        print("OK standalone: `wallet get` shared → значение")
    finally:
        await daemon.stop()


def test_standalone():
    asyncio.run(asyncio.wait_for(_standalone(), 120))


# ── TtyVaultHost ────────────────────────────────────────────────────
def test_tty_host():
    yes = TtyVaultHost(assume_yes=True)
    assert asyncio.run(yes.confirm("s", "descr", "prev")) is True
    # без tty и без assume_yes → отказ (безопасная сторона); stdin в тесте не tty
    no = TtyVaultHost()
    assert asyncio.run(no.confirm("s", "descr", "prev")) is False
    # observe/record/notify не падают (лог в stderr)
    asyncio.run(yes.observe("s", "<b>x</b> <code>y</code>"))
    yes.record("s", secret="a", cmd="gh pr", allowed=True)
    asyncio.run(yes.notify_denied("s", "gh auth token"))
    print("OK TtyVaultHost: assume_yes→confirm; без tty→отказ; observe/record/notify живы")


def test_confirm_concurrent_serialized():
    """Конкурентные confirm() на одной tty сериализуются (одна tty — по одному
    вопросу). Без сериализации второй add_reader затирал бы колбэк первого и
    ранний запрос висел бы вечно (нашло ревью 1.5). Fake-tty через os.pipe."""
    r, w = os.pipe()

    class FakeStdin:
        def isatty(self):
            return True

        def fileno(self):
            return r

    async def scenario():
        host = TtyVaultHost()
        orig = sys.stdin
        sys.stdin = FakeStdin()
        try:
            t1 = asyncio.create_task(host.confirm("s", "q1", "p1"))
            t2 = asyncio.create_task(host.confirm("s", "q2", "p2"))
            await asyncio.sleep(0.1)          # оба стартовали; lock держит t2
            await asyncio.to_thread(os.write, w, b"y\n")   # ответ ПЕРВОМУ
            r1 = await asyncio.wait_for(t1, 2)
            await asyncio.sleep(0.05)         # t1 отпустил lock → t2 читает
            await asyncio.to_thread(os.write, w, b"n\n")   # ответ ВТОРОМУ
            r2 = await asyncio.wait_for(t2, 2)
            assert r1 is True and r2 is False, (r1, r2)
        finally:
            sys.stdin = orig

    try:
        asyncio.run(asyncio.wait_for(scenario(), 8))
    finally:
        os.close(r)
        os.close(w)
    print("OK confirm: конкурентные запросы сериализованы (оба резолвятся по очереди)")


# ── policy CLI ──────────────────────────────────────────────────────
def test_policy_cli():
    tmp = Path(tempfile.mkdtemp(prefix="vault_pol_"))
    secrets = _secrets(tmp)
    ns = type("NS", (), {"secrets": str(secrets), "policy_args": []})()
    assert cmd_policy(ns) == 0
    print("OK policy CLI: просмотр secrets.toml")

    # правка через main(): добавить команду секрету tok
    rc = main(["--secrets", str(secrets), "policy", "cmd", "tok", "+curl *"])
    assert rc == 0
    assert "curl *" in secrets.read_text(), secrets.read_text()
    print("OK policy CLI: `policy cmd tok +curl *` записалось в файл")


# ── bin/vault как процесс (bootstrap: shebang/sys.path) ─────────────
def test_bin_vault_subprocess():
    tmp = Path(tempfile.mkdtemp(prefix="vault_bin_"))
    secrets = _secrets(tmp)
    r = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "vault"), "--secrets", str(secrets), "policy"],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0 and "tok" in r.stdout, (r.returncode, r.stdout, r.stderr)
    print("OK bin/vault: процесс поднимается, `policy` работает")


# ── регрессия ревью 1.5: SIGINT посреди висящего confirm не вешает serve ──
def test_sigint_during_confirm_does_not_hang():
    import pty
    import signal
    tmp = Path(tempfile.mkdtemp(prefix="vault_sig_"))
    sec = tmp / "s.toml"
    sec.write_text('[secrets.h]\nsessions=["*"]\ncommands=["sh -c *"]\nconfirm=true\n')
    os.chmod(sec, 0o600)
    wf = tmp / "w.json"
    cwd = tmp / "proj"
    cwd.mkdir()
    # serve нужен НАСТОЯЩИЙ tty на stdin (isatty→True), иначе confirm сразу False.
    master, slave = pty.openpty()
    serve = subprocess.Popen(
        [sys.executable, "-m", "vault", "--secrets", str(sec), "serve",
         "--cwd", str(cwd), "--wallet-file", str(wf), "--session", "local"],
        stdin=slave, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT),
    )
    os.close(slave)
    run = None
    try:
        for _ in range(200):
            if wf.exists():
                break
            time.sleep(0.05)
        assert wf.exists(), "vault serve не поднялся"
        # confirm=true секрет → wallet run повиснет на вопросе [y/N] (никто не отвечает)
        run = subprocess.Popen(
            [sys.executable, str(ROOT / "bin" / "wallet"), "run", "h", "--", "sh", "-c", "true"],
            env={**os.environ, "WALLET_FILE": str(wf)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)  # confirm теперь висит
        t0 = time.time()
        serve.send_signal(signal.SIGINT)
        serve.wait(timeout=10)  # с багом (thread в input) ждал бы вечно → TimeoutExpired
        dt = time.time() - t0
        assert dt < 6, f"serve вышел за {dt:.1f}s — слишком долго (висит на confirm?)"
        print(f"OK SIGINT посреди confirm: serve вышел за {dt:.2f}s (не завис)")
    finally:
        if run is not None:
            run.kill()
        if serve.poll() is None:
            serve.kill()
        os.close(master)


def main_all():
    test_tty_host()
    test_confirm_concurrent_serialized()
    test_policy_cli()
    test_bin_vault_subprocess()
    test_sigint_during_confirm_does_not_hang()
    test_standalone()
    print("ALL VAULT-CLI OK")


if __name__ == "__main__":
    main_all()
