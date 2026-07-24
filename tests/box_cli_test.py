"""Тесты CLI `claude-box` (box_cli): сборка argv, relay-логика, e2e-запуск.

Запуск: .venv/bin/python tests/box_cli_test.py (как весь tests/, без pytest).

Что покрыто:
  • parse_args — engine, passthrough, заглушки (--vm/init/…) → честный отказ;
  • build_argv — bwrap-обёртка на месте, cwd RW; off — команда как есть;
  • run() e2e — команда в песочнице/direct: вывод «BOXOK» доходит через
    on_output, код 0, fd не текут (мягкий скип bwrap при отсутствии);
  • -p (unattended) — разбор всех форм и честные отказы, промпт в argv claude,
    сочетания с passthrough/--wallet/--profile/--vm; e2e: терминал оператора не
    в raw, его ввод не воруется, код выхода claude пробрасывается.
    Отказы кошелька в этом режиме — tests/box_unattended_test.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import pty
import select
import sys
import tempfile
import termios
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from box_cli import cli


def _fds() -> set[int]:
    """Открытые fd процесса — для проверки, что relay/драйвер их не течёт."""
    try:
        return {int(x) for x in os.listdir("/proc/self/fd")}
    except OSError:
        return set()


# ── parse_args ───────────────────────────────────────────────────────────────
def test_parse_default_engine_bwrap():
    opts = cli.parse_args([])
    assert opts.engine == "bwrap"
    assert opts.passthrough == []
    assert opts.wallet is None and opts.secrets is None


def test_parse_engine_and_passthrough():
    opts = cli.parse_args(["--engine", "off", "--", "--model", "opus"])
    assert opts.engine == "off"
    assert opts.passthrough == ["--model", "opus"]
    # форма --engine=off
    assert cli.parse_args(["--engine=off"]).engine == "off"


def test_parse_wallet_and_secrets():
    """--wallet <секрет> и --secrets <файл>: обе формы (пробел и =)."""
    opts = cli.parse_args(["--wallet", "svc", "--", "-p", "hi"])
    assert opts.wallet == "svc"
    assert opts.passthrough == ["-p", "hi"], "аргументы после -- не трогаем"
    opts2 = cli.parse_args(["--wallet=svc", "--secrets=/tmp/s.toml"])
    assert opts2.wallet == "svc"
    assert opts2.secrets == Path("/tmp/s.toml")
    # --secrets без --wallet — бессмысленно → отказ код 2.
    try:
        cli.parse_args(["--secrets", "/tmp/s.toml"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("--secrets без --wallet должен упасть")
    # --wallet без значения → отказ код 2.
    try:
        cli.parse_args(["--wallet"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("--wallet без значения должен упасть")


def test_parse_bad_engine_rejected():
    try:
        cli.parse_args(["--engine", "docker"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("плохой --engine должен упасть CliError")


def test_parse_stub_flags_and_subcommands_refused():
    # Заглушки: честный отказ (код 2), не тихий no-op и не «unknown».
    # (--wallet/--profile/--vm/-p больше НЕ заглушки — реализованы, как и
    # init/profile.)
    # connect остаётся заглушкой (коннекторы Vault — отдельный трек).
    try:
        cli.parse_args(["connect"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("подкоманда connect должна быть заглушена")


def test_parse_help_exits_zero():
    try:
        cli.parse_args(["--help"])
    except SystemExit as e:
        assert e.code == 0
    else:
        raise AssertionError("--help должен выйти с кодом 0")


# ── -p: unattended ───────────────────────────────────────────────────────────
def test_parse_prompt_forms():
    """`-p значение`, `-p=значение` и `--print` (та же форма у самого claude)."""
    opts = cli.parse_args(["-p", "задача"])
    assert opts.prompt == "задача" and opts.unattended is True
    assert cli.parse_args(["-p=задача"]).prompt == "задача"
    assert cli.parse_args(["--print", "задача"]).prompt == "задача"
    # Без -p режим интерактивный.
    assert cli.parse_args([]).prompt is None
    assert cli.parse_args([]).unattended is False


def test_parse_prompt_with_passthrough_and_flags():
    """-p сочетается со сквозными аргументами и с --wallet/--profile/--vm."""
    opts = cli.parse_args(["-p", "задача", "--engine", "off", "--", "--model", "opus"])
    assert opts.prompt == "задача" and opts.passthrough == ["--model", "opus"]
    assert cli.parse_args(["-p", "t", "--wallet", "svc"]).wallet == "svc"
    assert cli.parse_args(["-p", "t", "--profile", "work"]).profile == "work"
    # --vm с -p совместим: промпт уезжает в гостя обычным аргументом claude.
    vm = cli.parse_args(["--vm", "-p", "задача"])
    assert vm.engine == cli.ENGINE_VM and vm.prompt == "задача"


def test_parse_prompt_empty_or_missing_refused():
    """Пустой промпт и -p без значения → честный отказ кодом 2."""
    for args in (["-p"], ["-p", ""], ["-p="], ["-p", "   "]):
        try:
            cli.parse_args(args)
        except SystemExit as e:
            assert e.code == 2, args
        else:
            raise AssertionError(f"{args} должно быть отвергнуто")


def test_parse_prompt_in_passthrough():
    """Промпт после `--`: сам по себе — можно (с предупреждением), вместе с
    нашим -p — отказ (у claude было бы два промпта)."""
    for args in (["-p", "a", "--", "-p", "b"], ["-p", "a", "--", "--print", "b"]):
        try:
            cli.parse_args(args)
        except SystemExit as e:
            assert e.code == 2, args
        else:
            raise AssertionError(f"{args} должно быть отвергнуто (два промпта)")
    # Без нашего -p: аргумент уходит в claude как есть, но оператора предупреждаем,
    # что unattended-режима лончера (deny+log) он так НЕ получит.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        opts = cli.parse_args(["--", "-p", "задача"])
    assert opts.passthrough == ["-p", "задача"], "после -- не трогаем"
    assert opts.unattended is False, "лончер об этом режиме не знает — и говорит об этом"
    assert "claude-box -p" in err.getvalue(), f"нет предупреждения: {err.getvalue()!r}"


def test_build_command_prompt_goes_to_claude_argv():
    """Промпт доезжает до argv claude флагом -p, перед сквозными аргументами."""
    assert cli.build_command("claude", prompt=None, passthrough=[]) == ["claude"]
    assert cli.build_command("claude", prompt="задача", passthrough=[]) == [
        "claude", "-p", "задача"]
    assert cli.build_command("claude", prompt="задача", passthrough=["--model", "opus"]) == [
        "claude", "-p", "задача", "--model", "opus"]


def test_help_no_longer_calls_prompt_unimplemented():
    """--help: -p описан как рабочий, в «не реализовано» остаётся только connect."""
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            cli.parse_args(["--help"])
    except SystemExit:
        pass
    text = out.getvalue()
    tail = text[text.index("Не реализовано"):]
    assert "-p" not in tail, f"-p всё ещё числится нереализованным: {tail!r}"
    assert "connect" in tail
    assert "-p <промпт>" in text and "unattended" in text


def test_parse_unknown_arg_refused():
    try:
        cli.parse_args(["--nope"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("неизвестный аргумент должен упасть")


# ── build_argv ───────────────────────────────────────────────────────────────
def test_build_argv_off_is_command_asis():
    runner = cli.make_engine_runner("off", cli.repo_root())
    argv = cli.build_argv(runner, ["sh", "-c", "true"], Path("/tmp"))
    assert argv == ["sh", "-c", "true"]


def test_build_argv_bwrap_wraps_and_cwd_rw():
    runner = cli.make_engine_runner("bwrap", cli.repo_root())
    cwd = Path("/tmp")
    argv = cli.build_argv(runner, ["claude", "--model", "opus"], cwd)
    assert argv[0] == "bwrap", "должна быть bwrap-обёртка"
    assert "--" in argv, "префикс bwrap завершается '--'"
    # cwd смонтирован RW (--bind-try /tmp /tmp) и стоит как chdir.
    joined = " ".join(argv)
    assert "--bind-try /tmp /tmp" in joined, "cwd должен быть RW-биндом"
    assert "--chdir /tmp" in joined
    # команда — после '--', в исходном порядке.
    tail = argv[argv.index("--") + 1:]
    assert tail == ["claude", "--model", "opus"]


# ── e2e: run() через Engine ──────────────────────────────────────────────────
async def _run_capture(engine: str) -> tuple[int, bytes]:
    buf = bytearray()
    argv = cli.build_argv(
        cli.make_engine_runner(engine, cli.repo_root()),
        ["sh", "-c", "echo BOXOK"],
        Path(os.getcwd()),
    )
    code = await cli.run(
        argv, cwd=os.getcwd(), env=os.environ.copy(),
        on_output=buf.extend, interactive=False,
    )
    return code, bytes(buf)


async def test_e2e_off_boxok_reaches_output():
    """off (без изоляции) — всегда доступен: вывод доходит, код 0, fd не текут."""
    before = _fds()
    code, out = await _run_capture("off")
    assert code == 0, f"код возврата {code}"
    assert b"BOXOK" in out, f"вывод не дошёл: {out!r}"
    leaked = _fds() - before
    assert not leaked, f"утекли fd: {leaked}"


async def test_e2e_bwrap_boxok_or_skip():
    """bwrap — та же проверка в песочнице; мягкий скип, если bwrap недоступен."""
    runner = cli.make_engine_runner("bwrap", cli.repo_root())
    ok, why = runner.preflight()
    if not ok:
        print(f"SKIP bwrap e2e: {why}")
        return
    before = _fds()
    code, out = await _run_capture("bwrap")
    assert code == 0, f"код возврата {code}"
    assert b"BOXOK" in out, f"вывод не дошёл из песочницы: {out!r}"
    leaked = _fds() - before
    assert not leaked, f"утекли fd: {leaked}"


async def test_unattended_run_no_raw_no_stdin_steal():
    """e2e -p: промпт доезжает до argv, код выхода пробрасывается, терминал
    оператора НЕ трогаем (raw не включаем, stdin не читаем).

    Терминал подсовываем настоящий (pty-пара на fd 0) — иначе проверка «не в
    raw» ничего бы не значила: без tty raw_terminal и так no-op.
    """
    master, slave = pty.openpty()
    tmp = Path(tempfile.mkdtemp(prefix="box_unattended_cli_"))
    script, argv_file = tmp / "fake-claude", tmp / "argv.txt"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$@" > {argv_file}\n'
        "sleep 0.6\n"
        "exit 7\n"
    )
    os.chmod(script, 0o755)
    saved_stdin = os.dup(0)
    old_bin = os.environ.get("CLAUDE_BIN")
    os.environ["CLAUDE_BIN"] = str(script)
    os.dup2(slave, 0)
    try:
        task = asyncio.create_task(cli.main_async(["-p", "PROMPT-MARK", "--engine", "off"]))
        await asyncio.sleep(0.25)  # процесс уже поднят и ещё жив
        attrs = termios.tcgetattr(0)
        assert attrs[3] & termios.ECHO, "unattended не должен переводить tty в raw"
        assert attrs[3] & termios.ICANON, "unattended не должен ломать канонический режим"
        os.write(master, b"typed\n")  # «нажатия» оператора — не наши
        code = await task
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        if old_bin is None:
            os.environ.pop("CLAUDE_BIN", None)
        else:
            os.environ["CLAUDE_BIN"] = old_bin

    assert code == 7, f"код выхода claude должен стать кодом claude-box, получен {code}"
    got = argv_file.read_text().split("\n")
    assert got[:2] == ["-p", "PROMPT-MARK"], f"промпт не доехал до claude: {got!r}"

    # Байты оператора никто не забрал: без арбитра они лежат в буфере терминала.
    ready, _, _ = select.select([slave], [], [], 0.5)
    assert ready, "ввод оператора исчез — значит stdin всё-таки читали"
    assert os.read(slave, 64) == b"typed\n"
    os.close(master)
    os.close(slave)
    for p in sorted(tmp.rglob("*"), reverse=True):
        p.unlink()
    tmp.rmdir()
    print("OK unattended: промпт в argv, код 7, терминал и stdin оператора не тронуты")


# ── raw_terminal: не сломать терминал ────────────────────────────────────────
def test_raw_terminal_restores_on_normal_exit():
    """tty-настройки после нормального выхода из with == до входа."""
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        with cli.raw_terminal(slave):
            pass  # внутри — raw (termios изменён)
        after = termios.tcgetattr(slave)
        assert after == before, "raw_terminal не восстановил tty при нормальном выходе"
    finally:
        os.close(master)
        os.close(slave)


def test_raw_terminal_restores_on_exception():
    """Исключение внутри with НЕ оставляет терминал сломанным (restore в finally)."""
    master, slave = pty.openpty()

    class _Boom(Exception):
        pass

    try:
        before = termios.tcgetattr(slave)
        try:
            with cli.raw_terminal(slave):
                raise _Boom()
        except _Boom:
            pass
        after = termios.tcgetattr(slave)
        assert after == before, "raw_terminal не восстановил tty при исключении"
    finally:
        os.close(master)
        os.close(slave)


def test_raw_terminal_noop_on_non_tty():
    """Не-tty (pipe) — no-op, не падает (isatty False)."""
    r, w = os.pipe()
    try:
        with cli.raw_terminal(r):
            pass
    finally:
        os.close(r)
        os.close(w)


# ── интерактивный relay: stdin доходит до процесса ───────────────────────────
async def test_interactive_relay_stdin_reaches_process():
    """interactive=True: байты из stdin_fd доезжают в pty процесса (add_reader/
    арбитр stdin), процесс их отражает, fd после чисты."""
    stdin_r, stdin_w = os.pipe()
    before = _fds()
    buf = bytearray()
    argv = cli.build_argv(
        cli.make_engine_runner("off", cli.repo_root()),
        ["sh", "-c", "read x; echo GOT-$x"],
        Path(os.getcwd()),
    )
    # Данные готовы ДО запуска — add_reader сработает сразу, как поднимется цикл.
    os.write(stdin_w, b"HELLO\n")
    try:
        code = await cli.run(
            argv, cwd=os.getcwd(), env=os.environ.copy(),
            on_output=buf.extend, interactive=True, stdin_fd=stdin_r,
        )
    finally:
        os.close(stdin_w)
        os.close(stdin_r)
    assert code == 0, f"код возврата {code}"
    assert b"GOT-HELLO" in bytes(buf), f"stdin не дошёл до процесса: {bytes(buf)!r}"
    # stdin_r/stdin_w уже были в before (созданы раньше) → в leaked не попадут;
    # проверяем, что pty_master драйвер закрыл (не течёт).
    leaked = _fds() - before
    assert not leaked, f"утекли fd: {leaked}"


# ── честный отказ вместо трейсбека при сбое запуска ──────────────────────────
def test_nonexistent_claude_bin_honest_error():
    """CLAUDE_BIN на несуществующий бинарь → внятная ошибка + код 1, не трейсбек."""
    old = os.environ.get("CLAUDE_BIN")
    os.environ["CLAUDE_BIN"] = "/nonexistent/claude-xyz-42"
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            code = cli.main(["--engine", "off"])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_BIN", None)
        else:
            os.environ["CLAUDE_BIN"] = old
    assert code == 1, f"ожидался ненулевой код, получен {code}"
    assert "не удалось запустить" in err.getvalue(), (
        f"нет честного сообщения об ошибке: {err.getvalue()!r}"
    )


def main() -> None:
    test_parse_default_engine_bwrap()
    test_parse_engine_and_passthrough()
    test_parse_wallet_and_secrets()
    test_parse_bad_engine_rejected()
    test_parse_stub_flags_and_subcommands_refused()
    test_parse_help_exits_zero()
    test_parse_prompt_forms()
    test_parse_prompt_with_passthrough_and_flags()
    test_parse_prompt_empty_or_missing_refused()
    test_parse_prompt_in_passthrough()
    test_build_command_prompt_goes_to_claude_argv()
    test_help_no_longer_calls_prompt_unimplemented()
    test_parse_unknown_arg_refused()
    test_build_argv_off_is_command_asis()
    test_build_argv_bwrap_wraps_and_cwd_rw()
    test_raw_terminal_restores_on_normal_exit()
    test_raw_terminal_restores_on_exception()
    test_raw_terminal_noop_on_non_tty()
    test_nonexistent_claude_bin_honest_error()
    asyncio.run(test_interactive_relay_stdin_reaches_process())
    asyncio.run(test_e2e_off_boxok_reaches_output())
    asyncio.run(test_e2e_bwrap_boxok_or_skip())
    asyncio.run(test_unattended_run_no_raw_no_stdin_steal())
    print("ALL BOX-CLI OK")


if __name__ == "__main__":
    main()
