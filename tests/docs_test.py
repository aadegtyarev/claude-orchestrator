"""Документация не разошлась с кодом (и сама с собой).

Проза не тестируется, но у неё есть проверяемые обещания: справочник настроек
обязан перечислять ВСЕ параметры `.env`, руководство — все команды бота, а
ссылки между документами обязаны вести на существующие файлы. Всё это молча
протухает при первой же правке кода: параметр добавили — в доке его нет, файл
переименовали — ссылка ведёт в пустоту. Живой случай (2026-07): `.env`-параметр
`SANDBOX_BWRAP_DOCKER` работал и был описан только в комментариях кода, а
README обещал `AGENT_VM_HOST_IP`, которого оркестратор не читал вовсе.

Проверяем:
  • каждый os.getenv в orchestrator/config.py упомянут в docs/CONFIG.md;
  • каждая команда Telegram-адаптера упомянута в docs/GUIDE.md;
  • относительные ссылки в README и docs/*.md ведут на существующие файлы;
  • README остаётся входом (ссылается на руководство и на доку разработчика).

Запуск: .venv/bin/python tests/docs_test.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Переменные окружения, которые читает config.py, но которые НЕ являются
# настройками проекта: их задаёт система/пользовательская сессия.
_NOT_OUR_SETTINGS = {"XDG_RUNTIME_DIR", "HOME", "PATH", "CLAUDE_ENV_"}


def _config_env_names() -> set[str]:
    src = (ROOT / "orchestrator" / "config.py").read_text(encoding="utf-8")
    # Читаем оба пути: прямой os.getenv и хелпер env_number (числовые настройки).
    names = set(re.findall(r'os\.getenv\(\s*"([A-Z_][A-Z0-9_]*)"', src))
    names |= set(re.findall(r'env_number\(\s*"([A-Z_][A-Z0-9_]*)"', src))
    return {n for n in names if n not in _NOT_OUR_SETTINGS}


def test_every_setting_is_documented():
    doc = (ROOT / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    missing = sorted(n for n in _config_env_names() if n not in doc)
    assert not missing, f"параметры .env без описания в docs/CONFIG.md: {missing}"
    print(f"OK docs/CONFIG.md описывает все {len(_config_env_names())} параметров .env")


def test_documented_settings_exist_in_code():
    """Обратная сторона: справочник не обещает того, чего код не читает.

    Именно так README обещал `AGENT_VM_HOST_IP` — параметр выглядел рабочим,
    а оркестратор его игнорировал."""
    doc = (ROOT / "docs" / "CONFIG.md").read_text(encoding="utf-8")
    # Имена параметров в таблицах справочника — в бэктиках, ЗАГЛАВНЫМИ.
    promised = {
        m for m in re.findall(r"`([A-Z][A-Z0-9_]{3,})`", doc)
        if not m.startswith(("CLAUDE_ENV_",))
    }
    known = _config_env_names() | {
        # Значения настроек и внешние имена, а не параметры .env.
        "SANDBOX", "MODULES", "TLS", "DNS", "SPA", "PATH", "HOME", "PEM", "KVM",
        "MCP", "HTTP", "HTTPS", "PTY",
        # Переменные, которые песочница ВЫРЕЗАЕТ, а не читает как настройку.
        "DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
    }
    unknown = sorted(p for p in promised if p not in known)
    assert not unknown, f"docs/CONFIG.md обещает то, чего код не читает: {unknown}"
    print("OK docs/CONFIG.md не обещает несуществующих параметров")


def test_every_command_is_documented():
    src = (ROOT / "orchestrator" / "adapters" / "telegram" / "adapter.py").read_text(
        encoding="utf-8")
    names: set[str] = set()
    # Именно фильтр Command("new", "list"…), а не BotCommand(...) из меню —
    # последний берёт подписи из текстов и дал бы ключи вида menu_new.
    for group in re.findall(r"(?<![A-Za-z])Command\(([^)]*)\)", src):
        names |= set(re.findall(r'"([a-z_]+)"', group))
    guide = (ROOT / "docs" / "GUIDE.md").read_text(encoding="utf-8")
    missing = sorted(n for n in names if f"/{n}" not in guide)
    assert not missing, f"команды без описания в docs/GUIDE.md: {missing}"
    print(f"OK docs/GUIDE.md описывает все {len(names)} команд бота")


def _markdown_files() -> list[Path]:
    return [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def test_relative_links_resolve():
    broken: list[str] = []
    for md in _markdown_files():
        text = md.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            path = (md.parent / target.split("#", 1)[0]).resolve()
            if not path.exists():
                broken.append(f"{md.relative_to(ROOT)} → {target}")
    assert not broken, f"битые ссылки в документации: {broken}"
    print("OK все относительные ссылки в документации ведут на существующие файлы")


def test_readme_is_the_entry_point():
    """README — вход для незнакомого человека: он обязан вести и в руководство
    пользователя, и в доку разработчика, а не заменять их собой."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for target in ("docs/GUIDE.md", "docs/CONFIG.md", "docs/DEVELOPMENT.md"):
        assert target in readme, f"README не ведёт в {target}"
    print("OK README ведёт в руководство, справочник настроек и доку разработчика")


def main() -> None:
    test_every_setting_is_documented()
    test_documented_settings_exist_in_code()
    test_every_command_is_documented()
    test_relative_links_resolve()
    test_readme_is_the_entry_point()
    print("ALL DOCS OK")


if __name__ == "__main__":
    main()
