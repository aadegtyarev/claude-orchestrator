"""gh/git-шимы standalone CLI `claude-box` (--wallet на host/inject-секрет).

Что проверяем:
  * vault.shims: отбор инструментов из commands (глобы мимо, дубликаты схлопнуты),
    ЖИВОЕ поведение git-шима (сетевая подкоманда → wallet exec, локальная →
    настоящий git), права каталога/скриптов;
  * box_cli.wallet.setup_wallet_shims: env-довесок (PATH начинается с каталога
    шимов, WALLET_FILE указывает на существующий 0600-файл с url+token), обёртка
    самого клиента `wallet` в PATH, teardown (демон остановлен, каталог снесён,
    повторный close не падает);
  * честные отказы с кодами: нет секрета / не разрешён сессии / нечего
    заворачивать → 2, демон не поднялся → 1.

Демон НЕ поднимается по-настоящему (это TCP-сокет и tty-хост) — подменяем
build_daemon фейком; всё, что касается шимов/env/прав/teardown, проверяется живьём.

Запуск: .venv/bin/python tests/box_cli_shims_test.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box_cli import wallet as boxwallet  # noqa: E402
from box_cli.wallet import WalletError  # noqa: E402
from vault.shims import (  # noqa: E402
    SHIM_DIRNAME,
    git_shim,
    install_cli,
    tool_names,
    write_shims,
)

SESSION = "claude-box"


def _write_secrets(path: Path, body: str) -> None:
    """secrets.toml 0600 — иначе SecretStore его не загрузит."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)


_SECRETS = (
    # host-passthrough: дубликат gh и чистый глоб — в шимы попасть не должны.
    '[secrets.hostcred]\nsessions=["*"]\n'
    'commands=["gh", "git", "curl https://api/*", "*", "gh"]\n\n'
    # inject-секрет для другой сессии — claude-box его не получает.
    '[secrets.foreign]\nvalue="V"\nenv="TOK"\nsessions=["prod-*"]\ncommands=["kubectl"]\n\n'
    # секрет без единой команды (только глоб) — заворачивать нечего.
    '[secrets.nocmd]\nvalue="V"\nenv="TOK2"\nsessions=["*"]\ncommands=["*"]\n'
)


def _secrets_file() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="box_shims_"))
    path = tmp / "secrets.toml"
    _write_secrets(path, _SECRETS)
    return path


# ── фейковый демон ───────────────────────────────────────────────────

class _FakeDaemon:
    """Заменитель VaultDaemon: без сокетов и tty. Помнит, что его остановили."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.started = False
        self.stopped = False
        self.token_cwd: Path | None = None
        self.url = "http://127.0.0.1:65000"

    async def start(self) -> None:
        if self.fail:
            raise RuntimeError("порт занят")
        self.started = True

    def issue_token(self, session: str, cwd: Path) -> str:
        self.token_cwd = cwd
        return "TOKEN-FAKE"

    async def stop(self) -> None:
        self.stopped = True


def _patch_daemon(daemon: _FakeDaemon):
    """Подменить build_daemon на фейк; вернуть восстановитель."""
    original = boxwallet.build_daemon
    boxwallet.build_daemon = lambda *a, **kw: daemon  # noqa: ARG005
    def restore() -> None:
        boxwallet.build_daemon = original
    return restore


# ── vault.shims ──────────────────────────────────────────────────────

def test_tool_names():
    """Отбор инструментов: basename первого токена, глобы мимо, дубликаты схлопнуты."""
    got = tool_names(["gh", "git", "curl https://api/*", "*", "gh", "/usr/bin/ssh", "s?h"])
    assert got == {"gh", "git", "curl", "ssh"}, got
    assert tool_names([]) == set()
    assert tool_names(["*", "?"]) == set(), "чистые глобы не имена бинарей"
    print("OK tool_names: глобы пропущены, дубликаты схлопнуты, basename взят")


def test_git_shim_behaviour():
    """ЖИВОЕ поведение git-шима: `push` уходит в wallet exec, `status` — настоящему
    git (подставляем эхо-заглушки, чтобы увидеть, кого позвали)."""
    tmp = Path(tempfile.mkdtemp(prefix="box_shims_git_"))
    fake_git = tmp / "realgit"
    fake_git.write_text('#!/bin/sh\necho "REALGIT $*"\n')
    os.chmod(fake_git, 0o755)
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "wallet").write_text('#!/bin/sh\necho "WALLET $*"\n')
    os.chmod(bindir / "wallet", 0o755)

    shim = tmp / "git"
    shim.write_text(git_shim(str(fake_git)))
    os.chmod(shim, 0o755)

    env = {**os.environ, "PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
    for args, expect in (
        (["push", "origin", "main"], "WALLET exec git push origin main"),
        (["fetch"], "WALLET exec git fetch"),
        (["clone", "u"], "WALLET exec git clone u"),
        (["status"], "REALGIT status"),
        (["commit", "-m", "x"], "REALGIT commit -m x"),
        ([], "REALGIT "),
    ):
        out = subprocess.run([str(shim), *args], env=env, capture_output=True,
                             text=True, check=True).stdout.strip()
        assert out == expect.strip(), f"{args}: {out!r} != {expect!r}"
    print("OK git-шим: сетевые подкоманды → wallet exec, локальные → настоящий git")


def test_write_shims_permissions():
    """Каталог 0700, скрипты 0755; перегенерация чистит устаревшие; пусто = пусто."""
    tmp = Path(tempfile.mkdtemp(prefix="box_shims_perm_"))
    shim_dir = tmp / SHIM_DIRNAME
    names = write_shims(shim_dir, {"gh", "git", "curl"})
    assert names == ["curl", "gh", "git"], names
    assert oct(shim_dir.stat().st_mode & 0o777) == "0o700", oct(shim_dir.stat().st_mode)
    for n in names:
        p = shim_dir / n
        assert oct(p.stat().st_mode & 0o777) == "0o755", n
        assert os.access(p, os.X_OK), n
    assert (shim_dir / "gh").read_text() == '#!/bin/sh\nexec wallet exec gh "$@"\n'
    # Перегенерация под меньший набор — старое не переживает.
    write_shims(shim_dir, {"gh"})
    assert {p.name for p in shim_dir.iterdir()} == {"gh"}
    assert write_shims(shim_dir, []) == [] and not list(shim_dir.iterdir())
    print("OK write_shims: каталог 0700, скрипты 0755, перегенерация чистит старое")


def test_tool_names_rejects_hostile_names():
    """Allowlist имени: инъекция в скрипт и traversal не доезжают до генератора.

    Граница доверия у secrets.toml высокая, но нулевая валидация — это отсутствие
    defense-in-depth: имя из конфига попадает и в текст /bin/sh-скрипта, и в имя
    файла. Кривое имя пропускаем (с предупреждением), запуск не валим."""
    hostile = [
        "gh$(id)",            # подстановка команды
        "gh`id`",             # она же бэктиками
        "gh;rm -rf /",        # разделитель statement (первый токен целиком)
        "gh|tee",             # пайп
        "gh'",                # ломает кавычку
        'gh"',
        "tool\x00hack",       # NUL: валидный TOML-эскейп, ValueError в write_text
        ".",                  # traversal: «файл .» = сам каталог
        "..",
        "гит",                # юникод — не имя бинаря в PATH
        "gh\x1b[31m",         # escape-последовательность
    ]
    for name in hostile:
        assert tool_names([name]) == set(), f"имя {name!r} не должно пройти"
    # Перевод строки/таб внутри шаблона: это РАЗДЕЛИТЕЛИ (pat.split), поэтому имя —
    # только первый токен; второй statement в скрипт не попадает.
    assert tool_names(["gh\ntouch /tmp/pwned"]) == {"gh"}
    assert "touch" not in write_shims(
        Path(tempfile.mkdtemp(prefix="box_shims_nl_")) / SHIM_DIRNAME,
        tool_names(["gh\ntouch /tmp/pwned"]))
    # Валидные имена (и всё, что реально бывает в policy) проходят как раньше.
    assert tool_names(["gh", "git", "docker-compose", "python3.11", "a_b", "c+d",
                       "/usr/bin/ssh", "curl https://api/*"]) == {
        "gh", "git", "docker-compose", "python3.11", "a_b", "c+d", "ssh", "curl"}
    # Один кривой шаблон не выкидывает соседние валидные.
    assert tool_names(["gh$(id)", "gh"]) == {"gh"}
    print("OK tool_names: инъекция/traversal/NUL/юникод отвергнуты, валидные целы")


def test_write_shims_rejects_hostile_names():
    """Вторая линия: даже если кривое имя дошло до write_shims — файла не будет."""
    tmp = Path(tempfile.mkdtemp(prefix="box_shims_bad_"))
    shim_dir = tmp / SHIM_DIRNAME
    names = write_shims(shim_dir, {"gh", ".", "..", "gh$(id)", "tool\x00hack"})
    assert names == ["gh"], names
    assert {p.name for p in shim_dir.iterdir()} == {"gh"}
    # traversal не создал ничего рядом с каталогом
    assert {p.name for p in tmp.iterdir()} == {SHIM_DIRNAME}, list(tmp.iterdir())
    print("OK write_shims: недопустимые имена пропущены, каталог чист")


def test_git_shim_path_with_space():
    """Путь к настоящему git с пробелом: обёртка ОБЯЗАНА его заквотировать.

    Без кавычек `exec /home/My Projects/git "$@"` даёт 127 «not found» на КАЖДЫЙ
    локальный git внутри песочницы, причём молча — на старте ошибки нет."""
    tmp = Path(tempfile.mkdtemp(prefix="box shims space_"))  # пробел в самом пути
    weird = tmp / "my git dir"
    weird.mkdir()
    fake_git = weird / "realgit"
    fake_git.write_text('#!/bin/sh\necho "REALGIT $*"\n')
    os.chmod(fake_git, 0o755)

    shim = tmp / "git"
    shim.write_text(git_shim(str(fake_git)))
    os.chmod(shim, 0o755)
    r = subprocess.run([str(shim), "status"], capture_output=True, text=True)
    assert r.returncode == 0, f"код {r.returncode}: {r.stderr!r}"
    assert r.stdout.strip() == "REALGIT status", r.stdout
    # На обычном пути (без спецсимволов) байты обёртки прежние — прод не тронут.
    assert 'exec /usr/bin/git "$@"' in git_shim("/usr/bin/git")
    print("OK git-шим: путь с пробелом заквотирован, обычный путь не изменился")


def test_install_cli_symlink_and_fallback():
    """Клиент `wallet`: симлинк на bin/wallet; без бита исполнения — обёртка через
    АБСОЛЮТНЫЙ интерпретатор (`python3` из PATH дал бы exec-цикл, если завёрнут
    сам python3)."""
    tmp = Path(tempfile.mkdtemp(prefix="box_shims_cli_"))
    shim_dir = tmp / SHIM_DIRNAME
    shim_dir.mkdir()

    cli = tmp / "wallet-cli"
    cli.write_text('#!/bin/sh\necho "CLI $*"\n')
    os.chmod(cli, 0o755)
    link = install_cli(shim_dir, cli)
    assert link.is_symlink() and link.readlink() == cli, link
    out = subprocess.run([str(link), "ls"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "CLI ls", out.stdout

    # Бит исполнения снят → фолбэк-обёртка. Проверяем: не «python3», а абсолютный
    # путь интерпретатора, и она действительно работает.
    noexec = tmp / "wallet-noexec"
    noexec.write_text("import sys\nprint('PY', *sys.argv[1:])\n")
    os.chmod(noexec, 0o644)
    link = install_cli(shim_dir, noexec)
    body = link.read_text()
    assert not link.is_symlink(), "без бита исполнения нужен фолбэк, а не симлинк"
    assert sys.executable in body, body
    assert "exec python3 " not in body, "интерпретатор из PATH = риск exec-цикла"
    out = subprocess.run([str(link), "x"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "PY x", out.stdout
    print("OK клиент wallet: симлинк на bin/wallet, фолбэк — абсолютный интерпретатор")


def test_sandbox_path_never_leaks_cwd():
    """PATH песочницы: пустой элемент (= текущий каталог!) не должен просочиться."""
    saved = os.environ.get("PATH")
    try:
        for raw, expect_tail in (
            ("/does/not/exist:", ["/does/not/exist"]),      # trailing colon → cwd
            (":/bin", ["/bin"]),                            # leading colon → cwd
            ("/a::/b", ["/a", "/b"]),                       # пустой в середине
        ):
            os.environ["PATH"] = raw
            got = boxwallet.sandbox_path(Path("/shims")).split(os.pathsep)
            assert got == ["/shims", *expect_tail], (raw, got)
        # Пустой PATH и отсутствующий PATH — оба дают дефолт, а не «».
        os.environ["PATH"] = ""
        got = boxwallet.sandbox_path(Path("/shims")).split(os.pathsep)
        assert got == ["/shims", "/usr/local/bin", "/usr/bin", "/bin"], got
        del os.environ["PATH"]
        assert boxwallet.sandbox_path(Path("/shims")).split(os.pathsep) == got
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved
    print("OK PATH песочницы: пустые элементы (cwd) отсеяны, дефолт при пустом PATH")


# ── box_cli.wallet: env, teardown, отказы ────────────────────────────

async def test_setup_shims_env_and_teardown():
    """setup_wallet_intercept на host-секрете: PATH с каталогом шимов впереди,
    WALLET_FILE на существующий 0600-файл, `wallet` и обёртки на месте, close()
    останавливает демон и сносит каталог (повторный вызов не падает)."""
    secrets = _secrets_file()
    daemon = _FakeDaemon()
    restore = _patch_daemon(daemon)
    intercept = None
    try:
        intercept = await boxwallet.setup_wallet_intercept(
            "hostcred", secrets_path=secrets, session_name=SESSION)
        env = intercept.env
        assert set(env) == {"PATH", "WALLET_FILE"}, env
        shim_dir = Path(env["PATH"].split(os.pathsep)[0])
        assert shim_dir.name == SHIM_DIRNAME, shim_dir
        assert env["PATH"].split(os.pathsep)[1:] == os.environ["PATH"].split(os.pathsep), (
            "исходный PATH должен сохраниться после каталога шимов")

        # Обёртки: ровно инструменты секрета + сам клиент wallet.
        got = {p.name for p in shim_dir.iterdir()}
        assert got == {"gh", "git", "curl", "wallet"}, got
        assert "kubectl" not in got, "чужая сессия не должна попасть в шимы"
        assert os.access(shim_dir / "wallet", os.X_OK)
        # Клиент — СИМЛИНК на bin/wallet (как в прод-провижне оркестратора), а не
        # шелл-обёртка: путь с пробелом её ломал бы, а `exec python3 …` из PATH мог
        # бы самозациклиться, если завёрнут и сам python3.
        cli_link = shim_dir / "wallet"
        assert cli_link.is_symlink(), "клиент wallet должен быть симлинком"
        assert cli_link.readlink() == boxwallet.wallet_cli_path(), cli_link.readlink()

        # WALLET_FILE: существует, 0600, содержит url+token демона.
        wf = Path(env["WALLET_FILE"])
        assert wf.exists() and oct(wf.stat().st_mode & 0o777) == "0o600", wf
        import json
        payload = json.loads(wf.read_text())
        assert payload == {"url": daemon.url, "token": "TOKEN-FAKE", "session": SESSION}
        assert daemon.token_cwd == Path.cwd(), daemon.token_cwd

        # Каталог отдан в RW-бинды песочницы (иначе шимы изнутри не видны).
        tmpdir = shim_dir.parent
        assert intercept.extra_rw == [tmpdir], intercept.extra_rw
        assert oct(tmpdir.stat().st_mode & 0o777) == "0o700", oct(tmpdir.stat().st_mode)

        await intercept.close()
        assert daemon.stopped, "демон не остановлен на выходе"
        assert not tmpdir.exists(), "временный каталог не снесён"
        await intercept.close()  # идемпотентность
        print("OK шимы: PATH+WALLET_FILE, обёртки+wallet, teardown снёс каталог и демон")
    finally:
        restore()
        if intercept is not None:
            await intercept.close()


async def test_shims_refusals():
    """Честные отказы: нет секрета / не разрешён сессии / нечего заворачивать → 2,
    демон не поднялся → 1."""
    secrets = _secrets_file()

    async def _expect(name: str, code: int, needle: str, *, daemon=None):
        restore = _patch_daemon(daemon or _FakeDaemon())
        try:
            await boxwallet.setup_wallet_intercept(
                name, secrets_path=secrets, session_name=SESSION)
        except WalletError as e:
            assert e.code == code, f"{name}: код {e.code} != {code} ({e})"
            assert needle in str(e), f"{name}: нет «{needle}» в «{e}»"
        else:
            raise AssertionError(f"{name}: ожидался WalletError")
        finally:
            restore()

    await _expect("nope", 2, "не найден")
    await _expect("foreign", 2, "не разрешён")
    await _expect("nocmd", 2, "нет ни одной команды")
    await _expect("hostcred", 1, "демон кошелька не поднят", daemon=_FakeDaemon(fail=True))
    print("OK отказы: нет секрета/не разрешён/нечего заворачивать → 2, сбой демона → 1")


async def test_non_oserror_cleans_up_everything():
    """Не-OSError при генерации обёрток НЕ должен ронять CLI трейсбеком и оставлять
    за собой демон и временный каталог.

    Живой репро был такой: commands=["tool\\u0000hack"] → имя с NUL → write_text
    бросает ValueError → прежний `except OSError` его не ловил → teardown не
    отрабатывал (демон висит, /tmp/claude-box-wallet-* переживает выход процесса),
    а оператор видел сырой трейсбек. Здесь имя отсекает allowlist, поэтому сбой
    воспроизводим напрямую — и проверяем, что сворачивание полное."""
    secrets = _secrets_file()
    daemon = _FakeDaemon()
    restore = _patch_daemon(daemon)
    original = boxwallet.build_shim_dir
    before = set(Path(tempfile.gettempdir()).glob("claude-box-wallet-*"))

    def boom(tmpdir, tools):  # noqa: ARG001
        raise ValueError("embedded null byte")

    boxwallet.build_shim_dir = boom
    try:
        await boxwallet.setup_wallet_intercept(
            "hostcred", secrets_path=secrets, session_name=SESSION)
    except WalletError as e:
        assert e.code == 1, e.code
        assert "не удалось подготовить обёртки" in str(e), str(e)
        assert "/tmp/claude-box-wallet-" not in str(e), (
            "внутренний путь во временный каталог оператору ничего не говорит")
    else:
        raise AssertionError("ожидался WalletError, а не трейсбек")
    finally:
        boxwallet.build_shim_dir = original
        restore()
    assert daemon.stopped, "демон не остановлен — остался висеть после отказа"
    leftovers = set(Path(tempfile.gettempdir()).glob("claude-box-wallet-*")) - before
    assert not leftovers, f"временные каталоги не снесены: {leftovers}"

    # И сквозной путь: NUL-имя в policy теперь честный отказ (код 2), не крах.
    tmp = Path(tempfile.mkdtemp(prefix="box_shims_nul_"))
    bad = tmp / "secrets.toml"
    _write_secrets(bad, '[secrets.nul]\nsessions=["*"]\ncommands=["tool\\u0000hack"]\n')
    restore = _patch_daemon(_FakeDaemon())
    try:
        await boxwallet.setup_wallet_intercept(
            "nul", secrets_path=bad, session_name=SESSION)
    except WalletError as e:
        assert e.code == 2 and "нет ни одной команды" in str(e), (e.code, str(e))
    else:
        raise AssertionError("ожидался честный отказ на имени с NUL")
    finally:
        restore()
    print("OK не-OSError: демон остановлен, каталог снесён, честный код без tmp-пути")


async def test_host_is_passed_to_daemon():
    """Хост confirm/ASK передаётся в демон кошелька насквозь.

    Это и есть провод, из-за которого claude-box не может пользоваться
    TtyVaultHost: тот вешает свой add_reader на stdin, уже занятый PTY-relay, и
    первый же confirm убивал бы ввод в сессию навсегда."""
    secrets = _secrets_file()
    daemon = _FakeDaemon()
    seen: dict = {}
    original = boxwallet.build_daemon

    def spy(path, **kw):
        seen.update(kw)
        return daemon

    boxwallet.build_daemon = spy
    sentinel = object()
    intercept = None
    try:
        intercept = await boxwallet.setup_wallet_intercept(
            "hostcred", secrets_path=secrets, session_name=SESSION, host=sentinel)
        assert seen.get("host") is sentinel, seen
    finally:
        boxwallet.build_daemon = original
        if intercept is not None:
            await intercept.close()

    # И сам build_daemon уважает host (иначе провод обрывается в vault.cli).
    from vault.cli import build_daemon as real_build_daemon
    d = real_build_daemon(secrets, host=sentinel)
    assert d.host is sentinel
    print("OK хост confirm/ASK доезжает от лончера до демона (не TtyVaultHost)")


def main() -> None:
    test_tool_names()
    test_tool_names_rejects_hostile_names()
    test_write_shims_rejects_hostile_names()
    test_git_shim_behaviour()
    test_git_shim_path_with_space()
    test_install_cli_symlink_and_fallback()
    test_sandbox_path_never_leaks_cwd()
    test_write_shims_permissions()
    asyncio.run(test_setup_shims_env_and_teardown())
    asyncio.run(test_shims_refusals())
    asyncio.run(test_non_oserror_cleans_up_everything())
    asyncio.run(test_host_is_passed_to_daemon())
    print("ALL BOX-CLI-SHIMS OK")


if __name__ == "__main__":
    main()
