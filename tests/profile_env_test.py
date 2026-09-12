"""Переменные окружения и токен из файла на профиле (`profile.toml`).

Профиль несёт не только учётку и адрес API, но и всё, чем обычно обвешивают
`claude` в шелле (bashrc-обёртки claude-ds/claude-openrouter): model-маппинги
ANTHROPIC_* и CLAUDE_CODE_*, токен провайдера из файла.

Что проверяем:
  • `[env]` — таблица переменных: ставит их на процесс claude, пустое значение
    СНИМАЕТ унаследованную (как `base_url = ""`), значения strip'ятся;
  • `[env]` работает и без `base_url` (профиль-только-переменные);
  • мусор — честный отказ: не строка, имя не из шелл-алфавита, переменные,
    которыми управляет сам профиль (CLAUDE_CONFIG_DIR, HOME, XDG_STATE_HOME,
    PATH, адрес и токен) и префикс CLAUDE_ENV_ — с подсказкой на правильный ключ;
  • `auth_token_file` — содержимое файла становится ANTHROPIC_AUTH_TOKEN только
    на СТАРТЕ: load_settings и `claude-box profile` файла не читают (список
    дешёвый, пропавший токен его не ломает), путь относительный считается от
    каталога профиля, отсутствующий/пустой/многострочный/великий файл — отказ;
  • оркестратор накладывает [env] и токен через тот же путь, что и адрес, а
    пропавший токен на старте сессии — SessionError, не трейсбек;
  • список профилей показывает число переменных и путь токена, но не значение.

Запуск: .venv/bin/python tests/profile_env_test.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from box import profiles  # noqa: E402
from box_cli import cli  # noqa: E402
from orchestrator.core.sessions import Session, SessionError, SessionManager  # noqa: E402

PROXY = "http://127.0.0.1:8787"


@contextlib.contextmanager
def isolated_root():
    """Временный CLAUDE_BOX_HOME — тесты не трогают реальные профили."""
    old = os.environ.get("CLAUDE_BOX_HOME")
    with tempfile.TemporaryDirectory(prefix="profile-env-test-") as d:
        os.environ["CLAUDE_BOX_HOME"] = d
        try:
            yield Path(d)
        finally:
            if old is None:
                os.environ.pop("CLAUDE_BOX_HOME", None)
            else:
                os.environ["CLAUDE_BOX_HOME"] = old


def write_settings(name: str, text: str) -> None:
    profiles.ensure_profile(name)
    profiles.settings_path(name).write_text(text, encoding="utf-8")


def write_token(name: str, text: str) -> Path:
    """Файл-токен внутри каталога профиля (для относительного auth_token_file)."""
    profiles.ensure_profile(name)
    path = profiles.profile_dir(name) / "token"
    path.write_text(text, encoding="utf-8")
    return path


def expect_error(fn, what: str) -> str:
    try:
        fn()
    except profiles.ProfileError as e:
        return str(e)
    raise AssertionError(f"{what} должен был отвергнуться")


# ── [env]: ставим/снимаем переменные ─────────────────────────────────────────
def test_env_table_sets_and_pops():
    """[env] ставит переменные; пустое значение снимает унаследованную."""
    with isolated_root():
        write_settings("ds", (
            "[env]\n"
            'ANTHROPIC_MODEL = "deepseek-v4-pro"\n'
            'CLAUDE_CODE_SUBAGENT_MODEL = "deepseek-flash"\n'
            'CLAUDE_CODE_EFFORT_LEVEL = "max"\n'
            'NO_PROXY = ""\n'  # пустое значение — снять унаследованную
        ))
        env = {"NO_PROXY": "example.org", "PATH": "/bin"}
        profiles.apply_settings(env, "ds")
        assert env["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
        assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-flash"
        assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "max"
        assert "NO_PROXY" not in env  # пустое значение СНЯЛО унаследованную
        assert env["PATH"] == "/bin"  # чужие ключи не трогаем
        # Идемпотентность: второй проход ничего не меняет.
        profiles.apply_settings(env, "ds")
        assert env["ANTHROPIC_MODEL"] == "deepseek-v4-pro"


def test_env_applies_without_base_url():
    """[env] без base_url работает (регрессия раннего return в apply_settings)."""
    with isolated_root():
        write_settings("ds", '[env]\nANTHROPIC_MODEL = "deepseek-v4-pro"\n')
        env = {"ANTHROPIC_BASE_URL": PROXY}
        profiles.apply_settings(env, "ds")
        assert env["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
        assert env["ANTHROPIC_BASE_URL"] == PROXY  # адресом профиль не управляет


def test_base_url_env_and_token_together():
    """Все три ключа разом; base_url = "" не мешает [env] и токену."""
    with isolated_root():
        write_token("ds", "sk-token\n")
        write_settings("ds", (
            'base_url = ""\n'
            'auth_token_file = "token"\n'
            '[env]\nANTHROPIC_MODEL = "deepseek-v4-pro"\n'
        ))
        env = {"ANTHROPIC_BASE_URL": PROXY}
        profiles.apply_settings(env, "ds")
        assert "ANTHROPIC_BASE_URL" not in env
        assert env["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-token"


# ── [env]: честный отказ на мусоре ───────────────────────────────────────────
def test_env_bad_names_rejected():
    """Имя переменной — шелл-алфавит: кавычки в TOML это не обходят."""
    with isolated_root():
        for bad in ('"PATH:x"', '"a b"', '"1FOO"', "MY-VAR"):
            write_settings("ds", f"[env]\n{bad} = 'x'\n")
            msg = expect_error(
                lambda: profiles.load_settings("ds"), "плохое имя переменной")
            assert bad.strip('"') in msg, msg


def test_env_prefix_and_managed_keys_rejected():
    """Переменные, которыми управляет профиль/движок, в [env] запрещены.

    Иначе env разъедется с биндом/каталогом/движком, и симптом не будет похож
    на причину. Каждый отказ подсказывает правильный ключ.
    """
    with isolated_root():
        cases = (
            ("CLAUDE_ENV_ANTHROPIC_MODEL", "CLAUDE_ENV_"),
            ("CLAUDE_CONFIG_DIR", ".claude"),
            ("HOME", "движок"),
            ("XDG_STATE_HOME", "фиксирует"),
            ("PATH", "собирают"),
            ("ANTHROPIC_BASE_URL", "base_url"),
            ("ANTHROPIC_AUTH_TOKEN", "auth_token_file"),
        )
        for key, hint in cases:
            write_settings("ds", f'[env]\n{key} = "x"\n')
            msg = expect_error(
                lambda: profiles.load_settings("ds"), f"запрещённый ключ {key}")
            assert key in msg, msg
            assert hint in msg, msg


def test_env_non_string_rejected():
    """Значение [env] — строка в кавычках: числа, булевы, списки — отказ."""
    with isolated_root():
        for text in (
            "[env]\nCLAUDE_CODE_AUTO_COMPACT_WINDOW = 1000000\n",
            "[env]\nX = true\n",
            '[env]\nX = ["a"]\n',
            "[env.sub]\nX = 'a'\n",
        ):
            write_settings("ds", text)
            assert "строкой" in expect_error(
                lambda: profiles.load_settings("ds"), "не строка")


def test_env_not_table_rejected():
    """env = "…" — не таблица: отказ с подсказкой про [env]."""
    with isolated_root():
        write_settings("ds", 'env = "x"\n')
        assert "таблицей" in expect_error(
            lambda: profiles.load_settings("ds"), "env не таблица")


def test_env_values_stripped():
    """Значения strip'ятся; пустое-после-strip — снять переменную."""
    with isolated_root():
        write_settings("ds", '[env]\nANTHROPIC_MODEL = "  deepseek-flash  "\n')
        s = profiles.load_settings("ds")
        assert s.env["ANTHROPIC_MODEL"] == "deepseek-flash"
        env = {"LEGACY": "1"}
        profiles.apply_settings(env, "ds")
        assert env["ANTHROPIC_MODEL"] == "deepseek-flash"
        # Пробелы вместо значения — то же «снять», а не переменная из пробелов.
        write_settings("ds", '[env]\nANTHROPIC_MODEL = "   "\n')
        env = {"ANTHROPIC_MODEL": "old"}
        profiles.apply_settings(env, "ds")
        assert "ANTHROPIC_MODEL" not in env


# ── auth_token_file: токен читается только на старте ─────────────────────────
def test_auth_token_file_sets_variable():
    """Содержимое файла (с обрезкой) → ANTHROPIC_AUTH_TOKEN, поверх унаследованного."""
    with isolated_root():
        write_token("ds", "sk-test\n")
        write_settings("ds", 'auth_token_file = "token"\n')
        env = {"ANTHROPIC_AUTH_TOKEN": "stale"}
        profiles.apply_settings(env, "ds")
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test"


def test_auth_token_file_relative_to_profile_dir():
    """Относительный путь токена — от каталога профиля; ~ раскрывается."""
    with isolated_root():
        p = write_token("ds", "sk-test")
        write_settings("ds", 'auth_token_file = "token"\n')
        assert profiles.load_settings("ds").auth_token_file == str(p)
        write_settings("ds", 'auth_token_file = "~/token"\n')
        assert profiles.load_settings("ds").auth_token_file == str(
            Path("~/token").expanduser())


def test_auth_token_file_missing_is_refusal():
    """Нет файла токена — честный отказ (код 1: среда), и CLI выходит без трейсбека."""
    with isolated_root():
        write_settings("ds", 'auth_token_file = "nope"\n')
        try:
            profiles.apply_settings({}, "ds")
        except profiles.ProfileError as e:
            assert e.code == 1
            assert "ds" in str(e) and "nope" in str(e)
        else:
            raise AssertionError("пропавший токен должен отказать")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = cli.main(["--engine", "off", "--profile", "ds"])
        assert code == 1
        assert "nope" in err.getvalue()
        assert "Traceback" not in err.getvalue()


def test_auth_token_file_empty_and_multiline_rejected():
    """Пустой файл и файл из нескольких строк — указан не тот файл (код 2)."""
    with isolated_root():
        write_token("ds", "   \n")
        write_settings("ds", 'auth_token_file = "token"\n')
        msg = expect_error(
            lambda: profiles.apply_settings({}, "ds"), "пустой токен")
        assert "пуст" in msg
        write_token("ds", "sk-a\nsk-b\n")
        msg = expect_error(
            lambda: profiles.apply_settings({}, "ds"), "многострочный токен")
        assert "строки" in msg


def test_auth_token_file_too_long_rejected():
    """Файл больше MAX_TOKEN_LEN байт не читаем вовсе — почти наверняка не токен."""
    with isolated_root():
        write_token("ds", "x" * (profiles.MAX_TOKEN_LEN + 1))
        write_settings("ds", 'auth_token_file = "token"\n')
        msg = expect_error(
            lambda: profiles.apply_settings({}, "ds"), "великий токен")
        assert "велик" in msg


def test_load_settings_never_reads_token_file():
    """Разбор настроек файла токена не касается: список профилей не падает."""
    with isolated_root():
        profiles.ensure_profile("ds")
        profiles.settings_path("ds").write_text(
            'auth_token_file = "missing-dir/token"\n', encoding="utf-8")
        s = profiles.load_settings("ds")  # файла нет — а разбор прошёл
        assert s.auth_token_file and s.auth_token_file.endswith("missing-dir/token")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.main(["profile"]) == 0
        assert "ds" in out.getvalue()


# ── оркестратор: [env] и токен через путь профиля ────────────────────────────
def make_manager(claude_profile=None, sandbox="bwrap") -> SessionManager:
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = SimpleNamespace(
        claude_profile=claude_profile, claude_config_dir=None, sandbox=sandbox)
    return mgr


def make_session(profile=None, sandbox=None) -> Session:
    return Session(
        name="s", port=0, session_dir=Path("/tmp/s"),
        claude_session_id="uuid", profile=profile, sandbox=sandbox,
    )


def test_session_applies_env_and_token():
    """Сессия под профилем получает [env] и токен тем же путём, что и адрес."""
    with isolated_root():
        write_token("ds", "sk-session\n")
        write_settings("ds", (
            'auth_token_file = "token"\n'
            '[env]\nANTHROPIC_MODEL = "deepseek-v4-pro"\n'
        ))
        mgr = make_manager()
        env: dict[str, str] = {}
        mgr._apply_profile_env(make_session(profile="ds"), env)
        assert env["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-session"


def test_session_missing_token_is_session_error():
    """Пропавший токен на старте сессии — SessionError с именем профиля."""
    with isolated_root():
        write_settings("ds", 'auth_token_file = "nope"\n')
        mgr = make_manager()
        try:
            mgr._apply_profile_env(make_session(profile="ds"), {})
        except SessionError as e:
            assert "ds" in str(e)
        else:
            raise AssertionError("пропавший токен должен валить старт внятно")


def test_agentvm_profile_env_not_applied():
    """Под agent-vm профиля нет ни у кого — значит и [env]/токена профиля нет."""
    with isolated_root():
        write_token("ds", "sk-vm\n")
        write_settings("ds", (
            'auth_token_file = "token"\n'
            '[env]\nANTHROPIC_MODEL = "deepseek-v4-pro"\n'
        ))
        mgr = make_manager(sandbox="agent-vm")
        env: dict[str, str] = {}
        mgr._apply_profile_env(make_session(profile="ds"), env)
        assert env == {}


# ── список профилей: видно настройки, не секреты ─────────────────────────────
def test_profile_list_shows_env_and_token_path():
    """Список показывает число переменных и путь токена, но не значение."""
    with isolated_root():
        write_token("ds", "sk-secret-value-12345")
        write_settings("ds", (
            'base_url = "https://api.deepseek.com/anthropic"\n'
            'auth_token_file = "token"\n'
            '[env]\nANTHROPIC_MODEL = "deepseek-v4-pro"\n'
            'CLAUDE_CODE_SUBAGENT_MODEL = "deepseek-flash"\n'
        ))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.main(["profile"]) == 0
        line = out.getvalue()
        assert "env: 2" in line
        assert "токен:" in line
        assert "sk-secret-value-12345" not in line  # значение секрета не печатаем
        assert "deepseek-v4-pro" not in line  # и переменные списком не показываем


def test_profile_list_broken_settings_still_listed():
    """Битый profile.toml — строка ошибки рядом с именем, список живёт."""
    with isolated_root():
        write_settings("ds", "base_url = 42\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert cli.main(["profile"]) == 0
        assert "ds" in out.getvalue() and "←" in out.getvalue()


def test_unknown_key_message_lists_new_keys():
    """Сообщение о неизвестном ключе перечисляет весь допустимый набор."""
    with isolated_root():
        write_settings("ds", 'base_ulr = "https://api.anthropic.com"\n')
        msg = expect_error(
            lambda: profiles.load_settings("ds"), "неизвестный ключ")
        assert "auth_token_file" in msg
        assert "env" in msg


def main() -> None:
    test_env_table_sets_and_pops()
    test_env_applies_without_base_url()
    test_base_url_env_and_token_together()
    test_env_bad_names_rejected()
    test_env_prefix_and_managed_keys_rejected()
    test_env_non_string_rejected()
    test_env_not_table_rejected()
    test_env_values_stripped()
    test_auth_token_file_sets_variable()
    test_auth_token_file_relative_to_profile_dir()
    test_auth_token_file_missing_is_refusal()
    test_auth_token_file_empty_and_multiline_rejected()
    test_auth_token_file_too_long_rejected()
    test_load_settings_never_reads_token_file()
    test_session_applies_env_and_token()
    test_session_missing_token_is_session_error()
    test_agentvm_profile_env_not_applied()
    test_profile_list_shows_env_and_token_path()
    test_profile_list_broken_settings_still_listed()
    test_unknown_key_message_lists_new_keys()
    print("ALL PROFILE-ENV OK")


if __name__ == "__main__":
    main()
