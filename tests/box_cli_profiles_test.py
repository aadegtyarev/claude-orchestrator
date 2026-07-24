"""Тесты профилей claude-box (box_cli.profiles + разбор/диспетчеризация в cli).

Профиль = изолированная идентичность claude: свой CLAUDE_CONFIG_DIR и, под bwrap,
свой $HOME. Что покрыто:
  • parse_args — --profile (формы пробел и =);
  • validate_name — traversal/инъекции имени (../, абсолют, ~, foo/bar, ., .., пусто,
    ведущий -, длина) отвергаются кодом 2; валидные имена проходят;
  • init — идемпотентно создаёт каталог (+ .claude, 0700) и печатает путь;
  • profile — список (пусто/непусто), rm удаляет, неизвестный подарг → код 2;
  • profile_env — CLAUDE_CONFIG_DIR всегда, HOME=<profile> только под bwrap;
  • предупреждение в stderr под --engine off (нет изоляции $HOME);
  • симлинк-гигиена — профиль-симлинк отвергается;
  • автономность — box_cli.profiles импортится в свежем процессе без orchestrator.

Запуск: .venv/bin/python tests/box_cli_profiles_test.py (как весь tests/).
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from box_cli import cli, profiles


@contextlib.contextmanager
def isolated_root():
    """Временный CLAUDE_BOX_HOME — профили не трогают реальный ~/.local/share."""
    old = os.environ.get("CLAUDE_BOX_HOME")
    with tempfile.TemporaryDirectory(prefix="box-profiles-test-") as d:
        os.environ["CLAUDE_BOX_HOME"] = d
        try:
            yield Path(d)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_BOX_HOME", None)
            else:
                os.environ["CLAUDE_BOX_HOME"] = old


# ── parse_args --profile ─────────────────────────────────────────────────────
def test_parse_profile_flag():
    assert cli.parse_args(["--profile", "work"]).profile == "work"
    assert cli.parse_args(["--profile=work"]).profile == "work"
    assert cli.parse_args([]).profile is None
    # --profile без значения → отказ код 2.
    try:
        cli.parse_args(["--profile"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("--profile без значения должен упасть")


# ── validate_name: безопасность ──────────────────────────────────────────────
def test_validate_name_rejects_traversal_and_injection():
    bad = [
        "", ".", "..", "../etc", "../../root", "/abs/path", "~", "~root",
        "foo/bar", "a/b/c", "-flag", "-", "with space", "на русском",
        "semi;colon", "dollar$", "star*", "a" * 65,
    ]
    for name in bad:
        try:
            profiles.validate_name(name)
        except profiles.ProfileError as e:
            assert e.code == 2, name
        else:
            raise AssertionError(f"имя {name!r} должно быть отвергнуто")


def test_validate_name_accepts_sane():
    for name in ("work", "a", "A1", "my.profile", "my_profile", "my-profile",
                 "v1.2.3", "x" * 64):
        assert profiles.validate_name(name) == name


def test_profile_dir_traversal_rejected_before_join():
    """profile_dir отвергает traversal-имя ДО построения пути (не выходит за корень)."""
    with isolated_root():
        try:
            profiles.profile_dir("../escape")
        except profiles.ProfileError as e:
            assert e.code == 2
        else:
            raise AssertionError("../escape должен быть отвергнут")


# ── init: идемпотентность и приватность ──────────────────────────────────────
def test_ensure_profile_idempotent_and_private():
    with isolated_root() as root:
        p1 = profiles.ensure_profile("work")
        assert p1 == root / "profiles" / "work"
        assert p1.is_dir()
        assert (p1 / ".claude").is_dir()
        # 0700 — приватно (креды/транскрипты).
        assert (p1.stat().st_mode & 0o777) == 0o700
        # Повтор — тот же путь, без ошибки, содержимое цело.
        (p1 / ".claude" / "marker").write_text("x")
        p2 = profiles.ensure_profile("work")
        assert p2 == p1
        assert (p1 / ".claude" / "marker").read_text() == "x"


def test_ensure_profile_symlink_rejected():
    """Профиль-симлинк отвергается (увёл бы HOME/CONFIG_DIR за корень профилей)."""
    with isolated_root() as root:
        pr = root / "profiles"
        pr.mkdir(parents=True, exist_ok=True)
        target = root / "outside"
        target.mkdir()
        (pr / "evil").symlink_to(target)
        try:
            profiles.ensure_profile("evil")
        except profiles.ProfileError as e:
            assert e.code == 2
        else:
            raise AssertionError("симлинк-профиль должен быть отвергнут")


# ── list / rm ────────────────────────────────────────────────────────────────
def test_list_and_remove_profiles():
    with isolated_root():
        assert profiles.list_profiles() == []
        profiles.ensure_profile("beta")
        profiles.ensure_profile("alpha")
        assert profiles.list_profiles() == ["alpha", "beta"]  # отсортировано
        removed = profiles.remove_profile("alpha")
        assert not removed.exists()
        assert profiles.list_profiles() == ["beta"]
        # Удалить несуществующий → ProfileError код 2.
        try:
            profiles.remove_profile("ghost")
        except profiles.ProfileError as e:
            assert e.code == 2
        else:
            raise AssertionError("rm несуществующего должен упасть")


# ── profile_env: CLAUDE_CONFIG_DIR всегда, HOME только под bwrap ──────────────
def test_profile_env_bwrap_sets_home_and_config():
    with isolated_root():
        env, pdir = profiles.profile_env("work", engine="bwrap")
        assert env["CLAUDE_CONFIG_DIR"] == str(pdir / ".claude")
        assert env["HOME"] == str(pdir)
        # CONFIG_DIR лежит ВНУТРИ каталога профиля (RW-бинд src==dst накрывает оба).
        assert Path(env["CLAUDE_CONFIG_DIR"]).parent == pdir


def test_profile_env_off_no_home():
    with isolated_root():
        env, pdir = profiles.profile_env("work", engine="off")
        assert env["CLAUDE_CONFIG_DIR"] == str(pdir / ".claude")
        assert "HOME" not in env, "под off $HOME не изолируем — HOME не задаём"


# ── подкоманды через cli.main ────────────────────────────────────────────────
def test_cmd_init_prints_path_and_is_idempotent():
    with isolated_root() as root:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["init", "work"])
        assert code == 0
        printed = out.getvalue().strip()
        assert printed == str(root / "profiles" / "work")
        assert Path(printed).is_dir()
        # Идемпотентно: повтор тоже код 0.
        with contextlib.redirect_stdout(io.StringIO()):
            assert cli.main(["init", "work"]) == 0


def test_cmd_init_bad_name_code_2():
    with isolated_root():
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["init", "../escape"])
        assert code == 2
        assert "claude-box:" in err.getvalue()


def test_cmd_profile_list_and_rm():
    with isolated_root():
        # Пусто → сообщение, код 0.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.main(["profile"]) == 0
        assert "нет профилей" in out.getvalue()
        # Создать пару и вывести список.
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["init", "alpha"])
            cli.main(["init", "beta"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.main(["profile"]) == 0
        assert out.getvalue().split() == ["alpha", "beta"]
        # rm.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.main(["profile", "rm", "alpha"]) == 0
        assert "удалён профиль" in out.getvalue()
        # Неизвестный подарг → код 2.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert cli.main(["profile", "nonsense"]) == 2


def test_connect_still_stub():
    """connect остаётся честной заглушкой (agent-vm-трек заблокирован), код 2."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = cli.main(["connect"])
    assert code == 2
    assert "connect" in err.getvalue()


# ── предупреждение под off ───────────────────────────────────────────────────
def test_profile_off_warns_no_home_isolation():
    """--profile --engine off печатает честное предупреждение про отсутствие изоляции
    $HOME (по образцу --wallet без bwrap). Запуск падает на несуществующем CLAUDE_BIN,
    но предупреждение уже выведено раньше."""
    with isolated_root():
        old_bin = os.environ.get("CLAUDE_BIN")
        os.environ["CLAUDE_BIN"] = "/nonexistent/claude-xyz-42"
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                cli.main(["--engine", "off", "--profile", "work"])
        finally:
            if old_bin is None:
                os.environ.pop("CLAUDE_BIN", None)
            else:
                os.environ["CLAUDE_BIN"] = old_bin
        assert "не изолирует $home" in err.getvalue().lower()


# ── кредлы оператора не уезжают в чужую идентичность ─────────────────────────
def test_profile_env_strips_operator_credentials():
    """Профиль изолирует не только ФС, но и кредлы в окружении: иначе «изолированная
    идентичность» аутентифицировалась бы как оператор (живой репро ревью: OAuth-токен
    прокси был виден внутри профиля). Не-кредлы (PATH и т.п.) остаются."""
    env = {
        "PATH": "/usr/bin", "LANG": "C.UTF-8",
        "CLAUDE_VM_PROXY_ACCESS_TOKEN": "sk-ant-oat01-secret",
        "ANTHROPIC_API_KEY": "sk-ant-api-secret",
        "GH_TOKEN": "ghp_x", "AWS_SECRET_ACCESS_KEY": "aws",
        "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
        "MY_PASSWORD": "p", "SOME_CREDENTIALS_FILE": "/x",
    }
    dropped = cli.strip_credentials(env)
    for gone in ("CLAUDE_VM_PROXY_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "GH_TOKEN",
                 "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK", "MY_PASSWORD",
                 "SOME_CREDENTIALS_FILE"):
        assert gone not in env, f"кредл {gone} утёк в профиль"
        assert gone in dropped
    assert env["PATH"] == "/usr/bin" and env["LANG"] == "C.UTF-8"

    # build_env: чистка только под --profile, обычный запуск окружение не трогает.
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    try:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with_profile = cli.build_env("bwrap", profile=True)
        assert "ANTHROPIC_API_KEY" not in with_profile
        assert "ANTHROPIC_API_KEY" in err.getvalue()  # прозрачность в stderr
        assert cli.build_env("bwrap")["ANTHROPIC_API_KEY"] == "sk-ant-test"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


# ── сбои ФС дают честный отказ, а не трейсбек ────────────────────────────────
def test_fs_errors_are_honest_not_traceback():
    """CLAUDE_BOX_HOME указывает в ФАЙЛ / профиль занят файлом → ProfileError код 1
    и внятное сообщение из CLI, а не сырой NotADirectoryError/FileExistsError."""
    old = os.environ.get("CLAUDE_BOX_HOME")
    with tempfile.TemporaryDirectory(prefix="box-profiles-fs-") as d:
        blocker = Path(d) / "notadir"
        blocker.write_text("я файл")
        os.environ["CLAUDE_BOX_HOME"] = str(blocker)
        try:
            try:
                profiles.ensure_profile("work")
                raise AssertionError("ожидался ProfileError")
            except profiles.ProfileError as e:
                assert e.code == 1, "сбой среды — код 1 (не 2: ввод-то корректный)"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                assert cli.main(["init", "work"]) == 2  # CliError печатает и выходит 2
            assert "work" in err.getvalue()
            assert "Traceback" not in err.getvalue()

            # Профиль занят файлом → тоже честный отказ.
            os.environ["CLAUDE_BOX_HOME"] = d
            (Path(d) / "profiles").mkdir()
            (Path(d) / "profiles" / "busy").write_text("файл на месте профиля")
            try:
                profiles.ensure_profile("busy")
                raise AssertionError("ожидался ProfileError")
            except profiles.ProfileError as e:
                assert e.code == 1
        finally:
            if old is None:
                os.environ.pop("CLAUDE_BOX_HOME", None)
            else:
                os.environ["CLAUDE_BOX_HOME"] = old


def test_relative_box_home_is_absolute():
    """Относительный CLAUDE_BOX_HOME приводится к абсолютному: иначе HOME/CONFIG_DIR
    и bind в песочницу молча зависели бы от каталога запуска."""
    old = os.environ.get("CLAUDE_BOX_HOME")
    os.environ["CLAUDE_BOX_HOME"] = "relhome"
    try:
        root = profiles.profiles_root()
        assert root.is_absolute(), root
        assert root == Path.cwd() / "relhome" / "profiles"
    finally:
        if old is None:
            os.environ.pop("CLAUDE_BOX_HOME", None)
        else:
            os.environ["CLAUDE_BOX_HOME"] = old


def test_remove_dangling_symlink_profile():
    """Битый симлинк-профиль удаляется (иначе имя заклинивало: init его отвергает,
    а rm говорил «не найден»)."""
    with isolated_root() as root:
        (root / "profiles").mkdir(parents=True, exist_ok=True)
        link = root / "profiles" / "broken"
        link.symlink_to(root / "nope-does-not-exist")
        assert profiles.remove_profile("broken") == link
        assert not link.is_symlink()


# ── автономность box_cli.profiles ────────────────────────────────────────────
def test_profiles_module_is_stdlib_only():
    """box_cli.profiles импортится в СВЕЖЕМ процессе, НЕ затягивая orchestrator
    (логика профилей — забота Слоя-CLI, но сама по себе только stdlib)."""
    root = str(Path(__file__).resolve().parent.parent)
    code = (
        f"import sys; sys.path.insert(0, {root!r});"
        "import box_cli.profiles;"
        "leaked=[m for m in sys.modules if m=='orchestrator' or m.startswith('orchestrator.')];"
        "sys.exit(1 if leaked else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, (
        "box_cli.profiles затянул orchestrator:\n"
        f"stdout={r.stdout}\nstderr={r.stderr}")


def main() -> None:
    test_parse_profile_flag()
    test_validate_name_rejects_traversal_and_injection()
    test_validate_name_accepts_sane()
    test_profile_dir_traversal_rejected_before_join()
    test_ensure_profile_idempotent_and_private()
    test_ensure_profile_symlink_rejected()
    test_list_and_remove_profiles()
    test_profile_env_bwrap_sets_home_and_config()
    test_profile_env_off_no_home()
    test_cmd_init_prints_path_and_is_idempotent()
    test_cmd_init_bad_name_code_2()
    test_cmd_profile_list_and_rm()
    test_connect_still_stub()
    test_profile_off_warns_no_home_isolation()
    test_profile_env_strips_operator_credentials()
    test_fs_errors_are_honest_not_traceback()
    test_relative_box_home_is_absolute()
    test_remove_dangling_symlink_profile()
    test_profiles_module_is_stdlib_only()
    print("ALL BOX-CLI-PROFILES OK")


if __name__ == "__main__":
    main()
