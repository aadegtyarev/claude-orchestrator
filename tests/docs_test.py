"""Документация не разошлась с кодом (и сама с собой).

Проза не тестируется, но у неё есть проверяемые обещания: каждый параметр и
каждая команда должны быть описаны — и ровно в той доке своего слоя, где их
ищут. Всё это молча протухает при первой же правке кода: параметр добавили — в
доке его нет, файл переименовали — ссылка ведёт в пустоту. Живые случаи
(2026-07): `.env`-параметр `SANDBOX_BWRAP_DOCKER` работал и был описан только в
комментариях кода; README обещал `AGENT_VM_HOST_IP`, которого оркестратор не
читал вовсе; `claude-box` не был описан нигде, кроме `--help`.

Слои и их доки: `claude-box` → docs/BOX.md, кошелёк → docs/WALLET.md,
оркестратор → docs/ORCHESTRATOR.md + docs/CONFIG.md.

Проверяем:
  • каждый параметр .env описан в CONFIG.md или BOX.md (движковые переменные
    принадлежат нижнему слою) и наоборот — дока не обещает того, чего код не
    читает;
  • каждая команда Telegram-адаптера описана в ORCHESTRATOR.md;
  • каждый флаг и подкоманда claude-box описаны в BOX.md;
  • относительные ссылки во всех доках ведут на существующие файлы;
  • README остаётся входом и ведёт в доку каждого слоя.

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


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _config_env_names() -> set[str]:
    src = _read("orchestrator", "config.py")
    # Читаем оба пути: прямой os.getenv и хелпер env_number (числовые настройки).
    names = set(re.findall(r'os\.getenv\(\s*"([A-Z_][A-Z0-9_]*)"', src))
    names |= set(re.findall(r'env_number\(\s*"([A-Z_][A-Z0-9_]*)"', src))
    return {n for n in names if n not in _NOT_OUR_SETTINGS}


def test_every_setting_is_documented():
    """Параметр описан в доке своего слоя: настройки оркестратора — в CONFIG.md,
    движковые (AGENT_VM_*) — в BOX.md, потому что их читает и claude-box."""
    docs = _read("docs", "CONFIG.md") + _read("docs", "BOX.md")
    missing = sorted(n for n in _config_env_names() if n not in docs)
    assert not missing, f"параметры .env без описания в CONFIG.md/BOX.md: {missing}"
    print(f"OK описаны все {len(_config_env_names())} параметров .env")


def test_documented_settings_exist_in_code():
    """Обратная сторона: справочник не обещает того, чего код не читает.

    Именно так README обещал `AGENT_VM_HOST_IP` — параметр выглядел рабочим,
    а оркестратор его игнорировал."""
    doc = _read("docs", "CONFIG.md")
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
    src = _read("orchestrator", "adapters", "telegram", "adapter.py")
    names: set[str] = set()
    # Именно фильтр Command("new", "list"…), а не BotCommand(...) из меню —
    # последний берёт подписи из текстов и дал бы ключи вида menu_new.
    for group in re.findall(r"(?<![A-Za-z])Command\(([^)]*)\)", src):
        names |= set(re.findall(r'"([a-z_]+)"', group))
    doc = _read("docs", "ORCHESTRATOR.md")
    missing = sorted(n for n in names if f"/{n}" not in doc)
    assert not missing, f"команды без описания в docs/ORCHESTRATOR.md: {missing}"
    print(f"OK docs/ORCHESTRATOR.md описывает все {len(names)} команд бота")


def test_box_cli_surface_is_documented():
    """Флаги и подкоманды `claude-box` — в BOX.md.

    Базовый слой раньше документировался только через --help: человек не знал,
    что CLI вообще существует, пока не наткнётся на него в исходниках."""
    flags = set(re.findall(r'"(--[a-z-]{3,})"', _read("box_cli", "cli.py")))
    doc = _read("docs", "BOX.md")
    missing = sorted(f for f in flags if f not in doc)
    assert not missing, f"флаги claude-box без описания в docs/BOX.md: {missing}"
    for sub in ("init", "profile", "config"):
        assert f"claude-box {sub}" in doc, f"подкоманда {sub} не описана в BOX.md"
    for env in ("CLAUDE_BIN", "CLAUDE_BOX_HOME", "CLAUDE_BOX_CONFIG"):
        assert env in doc, f"переменная {env} не описана в BOX.md"
    # Файл умолчаний — единственный способ настроить CLI без флагов; если его
    # перестанут описывать, человек снова окажется без ответа на «как задать
    # профиль по умолчанию».
    from box_cli.config import DEFAULT_PATH
    assert DEFAULT_PATH in doc, "путь файла умолчаний не описан в BOX.md"
    print(f"OK docs/BOX.md описывает все {len(flags)} флагов и подкоманды claude-box")


def _markdown_files() -> list[Path]:
    return [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md")),
            ROOT / "box" / "README.md", ROOT / "vault" / "README.md"]


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


def test_readme_leads_to_every_layer():
    """README — вход: ведёт в доку каждого слоя, а не заменяет их собой."""
    readme = _read("README.md")
    for target in ("docs/BOX.md", "docs/WALLET.md", "docs/ORCHESTRATOR.md",
                   "docs/CONFIG.md", "docs/DEVELOPMENT.md"):
        assert target in readme, f"README не ведёт в {target}"
    print("OK README ведёт в доку каждого слоя")


def main() -> None:
    test_every_setting_is_documented()
    test_documented_settings_exist_in_code()
    test_every_command_is_documented()
    test_box_cli_surface_is_documented()
    test_relative_links_resolve()
    test_readme_leads_to_every_layer()
    print("ALL DOCS OK")


if __name__ == "__main__":
    main()
