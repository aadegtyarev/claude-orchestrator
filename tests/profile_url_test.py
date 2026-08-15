"""Адрес API на профиле (`profile.toml`, ключ `base_url`).

Зачем это есть. Адрес API — свойство УЧЁТКИ, а не машины. Учётка Team берёт
managed-настройки организации (`channelsEnabled`, плагины, объявления) с
эндпоинта настроек прямого api.anthropic.com; локальный прокси-релей отдаёт
только `/v1/messages`, поэтому под ним орг-настройки не доезжают и Claude Code
считает каналы выключенными — при живом `/notify` 200 сообщение уходит в никуда
(живой замер 15.08.2026, см. box/profiles.apply_settings). Один общий
`CLAUDE_ENV_ANTHROPIC_BASE_URL` этот выбор выразить не мог: личной учётке прокси
нужен, командной противопоказан.

Что проверяем:
  • файла нет → окружение не тронуто (прежнее поведение);
  • `base_url = "…"` → перебивает унаследованный глобальный адрес;
  • `base_url = ""` → СНИМАЕТ переменную (иначе от прокси не отписаться);
  • мусор в файле (неизвестный ключ, не строка, URL без схемы, битый TOML) —
    честный отказ, а не тихий игнор;
  • оркестратор накладывает настройки поверх config.claude_env, а сессии без
    профиля и под agent-vm не трогает;
  • битый profile.toml на старте сессии → SessionError, а не трейсбек.

Запуск: .venv/bin/python tests/profile_url_test.py
"""

from __future__ import annotations

import contextlib
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from box import profiles  # noqa: E402
from orchestrator.core.sessions import Session, SessionError, SessionManager  # noqa: E402

PROXY = "http://127.0.0.1:8787"


@contextlib.contextmanager
def isolated_root():
    """Временный CLAUDE_BOX_HOME — тесты не трогают реальные профили."""
    old = os.environ.get("CLAUDE_BOX_HOME")
    with tempfile.TemporaryDirectory(prefix="profile-url-test-") as d:
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


def expect_error(fn, what: str) -> str:
    try:
        fn()
    except profiles.ProfileError as e:
        return str(e)
    raise AssertionError(f"{what} должен был отвергнуться")


# ── чтение profile.toml ──────────────────────────────────────────────────────
def test_no_file_means_untouched():
    """Профиль без profile.toml окружение не трогает — поведение как раньше."""
    with isolated_root():
        profiles.ensure_profile("work")
        assert profiles.load_settings("work").base_url is None
        env = {"ANTHROPIC_BASE_URL": PROXY}
        profiles.apply_settings(env, "work")
        assert env == {"ANTHROPIC_BASE_URL": PROXY}


def test_base_url_overrides_global():
    """Заданный адрес профиля перебивает унаследованный глобальный."""
    with isolated_root():
        write_settings("work", 'base_url = "https://api.anthropic.com"\n')
        env = {"ANTHROPIC_BASE_URL": PROXY, "PATH": "/bin"}
        profiles.apply_settings(env, "work")
        assert env["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
        assert env["PATH"] == "/bin"  # чужие ключи не трогаем


def test_empty_base_url_unsets_variable():
    """`base_url = ""` — ходить напрямую: переменную СНИМАЕМ, а не пустим пустой.

    Пустое значение ANTHROPIC_BASE_URL Claude Code трактует иначе, чем её
    отсутствие, — и без удаления от глобального прокси было бы не отписаться.
    """
    with isolated_root():
        write_settings("work", 'base_url = ""\n')
        env = {"ANTHROPIC_BASE_URL": PROXY}
        profiles.apply_settings(env, "work")
        assert "ANTHROPIC_BASE_URL" not in env
        # Идемпотентность: снимать нечего — не падаем.
        profiles.apply_settings(env, "work")
        assert "ANTHROPIC_BASE_URL" not in env


def test_whitespace_base_url_is_direct():
    """Пробелы — то же «напрямую», а не адрес из пробелов."""
    with isolated_root():
        write_settings("work", 'base_url = "   "\n')
        env = {"ANTHROPIC_BASE_URL": PROXY}
        profiles.apply_settings(env, "work")
        assert "ANTHROPIC_BASE_URL" not in env


def test_garbage_is_honest_refusal():
    """Мусор в profile.toml — отказ с причиной, а не тихий игнор."""
    with isolated_root():
        write_settings("work", 'base_ulr = "https://api.anthropic.com"\n')
        assert "base_ulr" in expect_error(
            lambda: profiles.load_settings("work"), "неизвестный ключ")

        write_settings("work", "base_url = 42\n")
        assert "строкой" in expect_error(
            lambda: profiles.load_settings("work"), "не строка")

        write_settings("work", 'base_url = "api.anthropic.com"\n')
        assert "схемой" in expect_error(
            lambda: profiles.load_settings("work"), "URL без схемы")

        write_settings("work", "base_url = \n")
        assert "TOML" in expect_error(
            lambda: profiles.load_settings("work"), "битый TOML")


def test_bad_profile_name_still_rejected():
    """Настройки не обходят валидацию имени: путь строится после проверки."""
    with isolated_root():
        expect_error(lambda: profiles.load_settings("../etc"), "traversal-имя")


# ── оркестратор: профиль поверх общих CLAUDE_ENV_* ───────────────────────────
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


def test_session_profile_wins_over_env_proxy():
    """Сессия под профилем: адрес профиля ложится поверх CLAUDE_ENV_*."""
    with isolated_root():
        write_settings("work", 'base_url = ""\n')
        mgr = make_manager()
        env = {"ANTHROPIC_BASE_URL": PROXY}
        mgr._apply_profile_env(make_session(profile="work"), env)
        assert "ANTHROPIC_BASE_URL" not in env


def test_default_profile_from_env_applies():
    """Дефолтный профиль из .env (CLAUDE_PROFILE) — тоже со своим адресом."""
    with isolated_root():
        write_settings("home", f'base_url = "{PROXY}"\n')
        mgr = make_manager(claude_profile="home")
        env: dict[str, str] = {}
        mgr._apply_profile_env(make_session(), env)
        assert env["ANTHROPIC_BASE_URL"] == PROXY


def test_no_profile_untouched():
    """Сессия без профиля (`--profile ""`) остаётся на общем адресе."""
    with isolated_root():
        mgr = make_manager(claude_profile="home")
        env = {"ANTHROPIC_BASE_URL": PROXY}
        mgr._apply_profile_env(make_session(profile=""), env)
        assert env["ANTHROPIC_BASE_URL"] == PROXY


def test_agentvm_has_no_profile_and_no_url():
    """Под agent-vm профиля нет ни у кого — значит и адреса профиля нет.

    Иначе оператор считал бы, что VM-сессия ходит по адресу профиля, хотя
    учётку гость берёт свою: «выключено значит не существует».
    """
    with isolated_root():
        write_settings("work", 'base_url = "https://api.anthropic.com"\n')
        mgr = make_manager(sandbox="agent-vm")
        env = {"ANTHROPIC_BASE_URL": PROXY}
        mgr._apply_profile_env(make_session(profile="work"), env)
        assert env["ANTHROPIC_BASE_URL"] == PROXY


def test_broken_settings_fail_session_not_traceback():
    """Битый profile.toml на старте — SessionError с именем профиля."""
    with isolated_root():
        write_settings("work", "base_url = 42\n")
        mgr = make_manager()
        try:
            mgr._apply_profile_env(make_session(profile="work"), {})
        except SessionError as e:
            assert "work" in str(e)
        else:
            raise AssertionError("битый profile.toml должен валить старт внятно")


def main() -> None:
    test_no_file_means_untouched()
    test_base_url_overrides_global()
    test_empty_base_url_unsets_variable()
    test_whitespace_base_url_is_direct()
    test_garbage_is_honest_refusal()
    test_bad_profile_name_still_rejected()
    test_session_profile_wins_over_env_proxy()
    test_default_profile_from_env_applies()
    test_no_profile_untouched()
    test_agentvm_has_no_profile_and_no_url()
    test_broken_settings_fail_session_not_traceback()
    print("ALL PROFILE-URL OK")


if __name__ == "__main__":
    main()
