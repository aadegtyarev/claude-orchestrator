"""Файловая песочница (bubblewrap): структура argv + реальная изоляция.

Покрыто:
  - build_argv: порядок (tmpfs $HOME до биндов, RW после RO), «-try»-флаги;
  - sandbox_prefix у SessionManager: пусто при SANDBOX=off, allowlist при bwrap;
  - интеграция: настоящий bash под bwrap видит cwd на запись, но НЕ видит
    ~/.ssh и не пишет на реальный диск (если bwrap доступен в окружении).

Запуск: .venv/bin/python tests/sandbox_test.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.runners import sandbox  # noqa: E402
from orchestrator.core.bashshell import BashSession  # noqa: E402
from orchestrator.core.sessions import SessionManager  # noqa: E402


def test_build_argv_order():
    home = Path("/home/tester")
    argv = sandbox.build_argv(
        home=home,
        chdir=home / "proj",
        rw_paths=[home / "proj"],
        ro_paths=[home / "code"],
    )
    s = " ".join(argv)
    assert argv[0] == "bwrap"
    assert argv[-1] == "--"
    assert "--chdir" in argv and argv[argv.index("--chdir") + 1] == str(home / "proj")
    # tmpfs $HOME должен идти РАНЬШЕ биндов под ним, иначе они затрутся.
    i_tmpfs = argv.index(str(home))  # аргумент "--tmpfs <home>"
    i_ro = s.index("--ro-bind-try /home/tester/code")
    i_rw = s.index("--bind-try /home/tester/proj")
    assert (len(" ".join(argv[:i_tmpfs]))) < i_ro < i_rw, "порядок tmpfs<RO<RW нарушен"
    # безопасные флаги присутствуют
    for flag in ("--die-with-parent", "--unshare-pid", "--proc", "--dev"):
        assert flag in argv, flag
    # сеть НЕ изолируется (нужна для API/localhost)
    assert "--unshare-net" not in argv
    # DNS при systemd-resolved: цель симлинка /etc/resolv.conf возвращена в /run
    assert "--ro-bind-try /run/systemd/resolve /run/systemd/resolve" in s
    # system D-Bus (по умолчанию вкл): проброшен для mDNS/avahi-browse
    assert "--ro-bind-try /run/dbus /run/dbus" in s
    print("OK build_argv: порядок tmpfs<RO<RW, die-with-parent, сеть общая, DNS+D-Bus")


def test_build_argv_dbus_off():
    argv = sandbox.build_argv(
        home=Path("/home/tester"), chdir=Path("/home/tester/proj"),
        rw_paths=[], ro_paths=[], system_dbus=False,
    )
    s = " ".join(argv)
    # Базовый DNS остаётся, а system D-Bus — нет.
    assert "/run/systemd/resolve" in s, "базовый DNS должен остаться"
    assert "/run/dbus" not in s, "SANDBOX_DBUS=off не должен пробрасывать system D-Bus"
    print("OK build_argv: system_dbus=False убирает D-Bus, оставляет DNS")


def test_build_argv_persistent_home():
    home = Path("/home/tester")
    argv = sandbox.build_argv(
        home=home, chdir=home / "proj", rw_paths=[], ro_paths=[],
        home_dir=Path("/data/homes/sess"),
    )
    s = " ".join(argv)
    # Персистентный дом монтируется НА МЕСТО $HOME вместо tmpfs.
    assert "--bind /data/homes/sess /home/tester" in s
    assert f"--tmpfs {home}" not in s
    print("OK build_argv: персистентный $HOME вместо tmpfs")


def _mgr(mode: str) -> SessionManager:
    cfg = SimpleNamespace(
        sandbox=mode,
        sandbox_extra_rw=(),
        sandbox_dbus=True,
        sandbox_docker=False,
        claude_config_dir=Path("/home/tester/.claude-proxy"),
        claude_profile=None,  # профиля нет → config_dir_of отдаст путь выше
    )
    m = SessionManager.__new__(SessionManager)
    m.config = cfg
    return m


def test_prefix_off_empty():
    assert _mgr("off").sandbox_prefix(Path("/x"), [Path("/x")]) == []
    print("OK sandbox_prefix: SANDBOX=off → пустой префикс")


def test_prefix_allowlist():
    m = _mgr("bwrap")
    work = str(Path.home() / "proj")
    argv = m.sandbox_prefix(Path(work), [Path(work)])
    s = " ".join(argv)
    # claude_config_dir из конфига — RW (контролируемое значение)
    assert "--bind-try /home/tester/.claude-proxy" in s
    # При заданном config_dir отдельного бинда ~/.claude.json НЕТ: claude кладёт
    # свой .claude.json ВНУТРЬ config-dir, который уже прибинден выше. Реальный
    # ~/.claude.json оператора в песочницу не попадает.
    assert f"--bind-try {Path.home() / '.claude.json'}" not in s
    assert "--bind-try /home/tester/.claude.json" not in s
    assert f"--bind-try {work}" in s                       # рабочая папка RW
    assert "--ro-bind-try" in s and "/.local/share/claude" in s  # бинарь RO
    print("OK sandbox_prefix: конфиг+проект RW, бинарь+репозиторий RO")


def test_docker_sock_bound_when_passed():
    # build_argv с docker_sock → прокси-сокет биндится на /run/docker.sock + DOCKER_HOST
    dsock = Path("/run/user/1000/claude-orchestrator/docker-noos.sock")
    s = " ".join(sandbox.build_argv(
        home=Path("/home/t"), chdir=Path("/x"), rw_paths=[Path("/x")], ro_paths=[],
        docker_sock=dsock))
    assert f"--bind-try {dsock} /run/docker.sock" in s
    assert "--setenv DOCKER_HOST unix:///run/docker.sock" in s
    # без docker_sock → ни сокета, ни DOCKER_HOST
    s2 = " ".join(sandbox.build_argv(
        home=Path("/home/t"), chdir=Path("/x"), rw_paths=[Path("/x")], ro_paths=[]))
    assert "docker.sock" not in s2 and "DOCKER_HOST" not in s2
    print("OK build_argv: docker.sock+DOCKER_HOST только при переданном docker_sock")


def test_real_isolation():
    ok, why = sandbox.available()
    if not ok:
        print(f"SKIP real_isolation: bwrap недоступен ({why})")
        return
    home = Path.home()
    work = Path(tempfile.mkdtemp(prefix="sbx_", dir=home / "claude-orchestrator-sessions"
                                  if (home / "claude-orchestrator-sessions").exists() else None))
    try:
        wrapper = sandbox.build_argv(
            home=home, chdir=work, rw_paths=[work],
            ro_paths=[home / ".local"],
        )
        sh = BashSession(work, wrapper)
        try:
            marker = "SBXDONE"
            # Результат — через код возврата ($?), чтобы токен результата
            # («SSHRC=1») не совпадал с текстом команды («SSHRC=$?»): иначе
            # эхо интерактивного bash фальшиво «подтверждает» проверку.
            sh.write(f"echo -n canwrite > {work}/in.txt; "
                     f"test -e ~/.ssh; echo \"SSHRC=$?\"; "
                     f"echo -n leak > ~/leaktest.txt 2>/dev/null; "
                     f"echo {marker}\n")
            deadline = time.time() + 15
            while time.time() < deadline:
                if marker.encode() in sh.snapshot():
                    break
                time.sleep(0.3)
            out = sh.snapshot().decode(errors="replace")
            assert "SSHRC=1" in out, f"~/.ssh должен быть невидим (SSHRC=1)\n{out}"
            assert "SSHRC=0" not in out, f"~/.ssh виден в песочнице!\n{out}"
            assert (work / "in.txt").read_text() == "canwrite", "не записал в рабочую папку"
            # запись в ~ уходит в эфемерный tmpfs — на реальном диске файла нет
            assert not (home / "leaktest.txt").exists(), "УТЕЧКА: файл появился в реальном $HOME"
            print("OK real_isolation: cwd пишется, ~/.ssh скрыт, записи в $HOME не текут на диск")
        finally:
            sh.close()
        # Персистентный дом: запись в ~ остаётся в home_dir и переживает шелл.
        priv = work / "privhome"
        priv.mkdir()
        wrapper2 = sandbox.build_argv(
            home=home, chdir=work, rw_paths=[work],
            ro_paths=[home / ".local"], home_dir=priv,
        )
        sh2 = BashSession(work, wrapper2)
        try:
            sh2.write("echo -n kept > ~/kept.txt; echo PHDONE\n")
            deadline = time.time() + 15
            while time.time() < deadline:
                if b"PHDONE" in sh2.snapshot():
                    break
                time.sleep(0.3)
        finally:
            sh2.close()
        assert (priv / "kept.txt").read_text() == "kept", "персистентный $HOME не сохранил файл"
        assert not (home / "kept.txt").exists(), "УТЕЧКА в реальный $HOME"
        print("OK real_isolation: персистентный $HOME сохраняет записи")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        (home / "leaktest.txt").unlink(missing_ok=True)


def test_no_job_control_warning_stderr_still_works():
    """bwrap --unshare-pid: bash не может стать foreground process group
    терминала (TTY снаружи pid-namespace) и на КАЖДОМ старте печатает в
    STDERR «не удаётся задать группу процесса терминала» — ограничение
    ядра, безвредное (Ctrl-C идёт через сигнальный путь TTY, не через
    process group), но пугающее в сыром выводе /bashin и /term.

    ⚠️ НЕ путать с sudo-подсказкой того же баннера («To run a command as
    administrator…») — та печатается через STDOUT (`cat <<-EOF` в
    /etc/bash.bashrc, читается только т.к. мы запускаем `bash -i`) и этим
    фиксом сознательно НЕ подавляется: единственный надёжный способ убрать
    её отсюда — `--norc`, а он заодно отключил бы персональный ~/.bashrc
    оператора (алиасы, PATH, virtualenv) — функциональная потеря, а не
    косметика, менять без спроса нельзя. Штатное решение вне нашего кода:
    `touch ~/.sudo_as_admin_successful` на хосте — тот же файл-маркер, что
    сам sudo создаёт после первого использования.

    Регресс-контракт: warning не должен попадать в снапшот оболочки, а
    stderr КОМАНД ОПЕРАТОРА (не самого bash) обязан работать как раньше —
    иначе оператор перестал бы видеть ошибки компиляции/тестов."""
    ok, why = sandbox.available()
    if not ok:
        print(f"SKIP no_job_control_warning: bwrap недоступен ({why})")
        return
    home = Path.home()
    work = Path(tempfile.mkdtemp(prefix="sbx_jc_", dir=home / "claude-orchestrator-sessions"
                                  if (home / "claude-orchestrator-sessions").exists() else None))
    try:
        wrapper = sandbox.build_argv(
            home=home, chdir=work, rw_paths=[work], ro_paths=[home / ".local"],
        )
        sh = BashSession(work, wrapper)
        try:
            marker = "JCDONE"
            sh.write(f"echo START{marker}; cat /no/such/file/xyz; echo {marker}\n")
            deadline = time.time() + 15
            while time.time() < deadline:
                if marker.encode() in sh.snapshot():
                    break
                time.sleep(0.3)
            out = sh.snapshot().decode(errors="replace")
            assert "не удаётся задать группу процесса терминала" not in out, (
                f"job-control warning (STDERR bash) просочился в вывод оболочки:\n{out}")
            assert "не может управлять заданиями" not in out, (
                f"job-control warning (STDERR bash) просочился в вывод оболочки:\n{out}")
            # Регресс-барьер: ошибка ЧУЖОЙ команды (не самого bash) обязана
            # быть видна — иначе оператор потерял бы диагностику навсегда.
            assert "No such file or directory" in out or "нет такого файла" in out.lower(), (
                f"stderr команды оператора пропал вместе с warning'ом:\n{out}")
            print("OK sandbox: job-control warning (STDERR) подавлен, "
                  "stderr команд оператора цел")
        finally:
            sh.close()
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_git_ssh_rewrite_inside_bwrap():
    """SSH→HTTPS переписывание git-remote реально видно ВНУТРИ bwrap-песочницы,
    когда её процессу передан GIT_CONFIG_* довесок (см.
    vault.secret.git_ssh_rewrite_env / WalletModule.session_env) — не просто на
    хосте (уже проверено в vault_domain_test.py), а именно там, где ~/.ssh
    недоступен и где переписывание реально нужно.

    Без сетевого клона (дорого/недетерминированно для CI): `git config --get`
    внутри песочницы — то, что git реально смотрит перед тем как открыть
    SSH-соединение; этого достаточно, чтобы поймать регресс в передаче env
    через bwrap (например, если --unshare-* или чистка env когда-нибудь
    начнёт резать переменные с префиксом GIT_)."""
    ok, why = sandbox.available()
    if not ok:
        print(f"SKIP git_ssh_rewrite_inside_bwrap: bwrap недоступен ({why})")
        return
    from vault.secret import git_ssh_rewrite_env
    home = Path.home()
    work = Path(tempfile.mkdtemp(prefix="sbx_git_", dir=home / "claude-orchestrator-sessions"
                                  if (home / "claude-orchestrator-sessions").exists() else None))
    try:
        wrapper = sandbox.build_argv(
            home=home, chdir=work, rw_paths=[work], ro_paths=[home / ".local"],
        )
        env = os.environ.copy()
        env.update(git_ssh_rewrite_env())
        r = subprocess.run(
            [*wrapper, "git", "config", "--get", "url.https://github.com/.insteadOf"],
            env=env, capture_output=True, text=True, timeout=15,
        )
        assert r.returncode == 0 and r.stdout.strip() == "git@github.com:", (
            f"git внутри bwrap не видит insteadOf: rc={r.returncode} "
            f"stdout={r.stdout!r} stderr={r.stderr!r}")
        print("OK sandbox: GIT_CONFIG_* доезжает до git внутри bwrap-песочницы")
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def test_available_no_bwrap():
    """Нет bwrap в PATH → (False, «не установлен»), probe не запускается."""
    from unittest import mock
    with mock.patch.object(sandbox.shutil, "which", return_value=None), \
         mock.patch.object(sandbox.subprocess, "run") as run:
        ok, why = sandbox.available()
    assert ok is False and "не установлен" in why
    run.assert_not_called()  # без bwrap probe даже не пробуем
    print("OK available: нет bwrap -> (False, установить)")


def test_available_probe_raises():
    """bwrap есть, но probe-subprocess падает (exec/таймаут) → (False, «не запускается»)."""
    from unittest import mock
    with mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/bwrap"), \
         mock.patch.object(sandbox.subprocess, "run", side_effect=OSError("boom")):
        ok, why = sandbox.available()
    assert ok is False and "не запускается" in why and "boom" in why
    print("OK available: probe бросил -> (False, не запускается)")


def test_available_userns_rejected():
    """Ядро отвергает unpriv userns (Ubuntu 24.04+ AppArmor): probe returncode!=0
    → (False, «ядро отклонило …»), stderr прокидывается в причину."""
    from unittest import mock
    probe = subprocess.CompletedProcess([], returncode=1, stdout=b"", stderr=b"bwrap: setting up uid map: Permission denied")
    with mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/bwrap"), \
         mock.patch.object(sandbox.subprocess, "run", return_value=probe):
        ok, why = sandbox.available()
    assert ok is False and "ядро отклонило" in why and "uid map" in why
    print("OK available: userns отвергнут -> (False, ядро отклонило)")


def test_available_ok():
    """bwrap есть и probe returncode=0 → (True, ok)."""
    from unittest import mock
    probe = subprocess.CompletedProcess([], returncode=0, stdout=b"", stderr=b"")
    with mock.patch.object(sandbox.shutil, "which", return_value="/usr/bin/bwrap"), \
         mock.patch.object(sandbox.subprocess, "run", return_value=probe):
        ok, why = sandbox.available()
    assert ok is True and why == "ok"
    print("OK available: bwrap+userns -> (True, ok)")


def _bwrap_rw_paths(claude_config_dir: Path) -> str:
    """Собрать argv BwrapRunner.wrap с данным claude_config_dir и вернуть строку."""
    from orchestrator.runners.bwrap import BwrapRunner
    cfg = SimpleNamespace(
        sandbox="bwrap",
        claude_config_dir=claude_config_dir,
        sandbox_extra_rw=(),
        sandbox_dbus=False,
        sandbox_docker=False,  # docker-сокет — пер-сессионный, здесь не проверяем
    )
    runner = BwrapRunner(cfg, root=Path("/repo"))
    argv = runner.wrap([], chdir=Path("/w"), extra_rw=[Path("/w")])
    return " ".join(argv)


def test_bwrap_claude_json_only_without_config_dir():
    """Реальный ~/.claude.json биндится ТОЛЬКО когда config-dir не задан.

    При заданном CLAUDE_CONFIG_DIR claude ведёт свой .claude.json внутри него, и тот
    уже покрыт биндом самого config_dir; отдельный бинд утащил бы в песочницу
    постороннее (под профилем — глобальное состояние оператора):
      1) дефолт (config_dir=None) → home/.claude.json (как раньше);
      2) вынесенный config_dir (оркестратор) → бинда нет, файл внутри config_dir;
      3) профиль (config_dir=<profile>/.claude) → реального home/.claude.json НЕТ.
    """
    home = Path.home()

    # 1) Дефолт: claude_config_dir=None → нужен явный бинд ~/.claude.json.
    s_default = _bwrap_rw_paths(None)
    assert f"--bind-try {home / '.claude.json'}" in s_default
    assert f"--bind-try {home / '.claude'} " in s_default + " "

    # 2) Оркестратор: CLAUDE_CONFIG_DIR задан → биндится он сам, отдельного файла нет.
    s_orch = _bwrap_rw_paths(home / ".claude-proxy")
    assert f"--bind-try {home / '.claude-proxy'}" in s_orch
    assert f"--bind-try {home / '.claude.json'}" not in s_orch

    # 3) Профиль: config_dir вынесен → реальный ~/.claude.json скрыт.
    profile = Path("/tmp/box-profiles/work")
    s_profile = _bwrap_rw_paths(profile / ".claude")
    assert f"--bind-try {profile / '.claude'}" in s_profile
    assert f"--bind-try {home / '.claude.json'}" not in s_profile, (
        "УТЕЧКА: реальный ~/.claude.json биндится в песочницу профиля"
    )
    print("OK bwrap: ~/.claude.json только при пустом config-dir (профиль изолирован)")


def main():
    test_build_argv_order()
    test_build_argv_dbus_off()
    test_build_argv_persistent_home()
    test_prefix_off_empty()
    test_prefix_allowlist()
    test_bwrap_claude_json_only_without_config_dir()
    test_real_isolation()
    test_no_job_control_warning_stderr_still_works()
    test_git_ssh_rewrite_inside_bwrap()
    test_available_no_bwrap()
    test_available_probe_raises()
    test_available_userns_rejected()
    test_available_ok()
    print("ALL SANDBOX OK")


if __name__ == "__main__":
    main()
