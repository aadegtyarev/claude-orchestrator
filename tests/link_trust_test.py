"""Регресс §1: linked-папка с project-хуками → предупреждение оператору.

При linked-сессии claude авто-доверяет чужой папке и исполняет её
project-хуки. Это не блокируется (модель угроз — страховки, папку выбрал
оператор), но должно быть ВИДНО в логах. Проверяем, что ругань появляется
на папке с хуками/.mcp.json и молчит на чистой.

Запуск: .venv/bin/python tests/link_trust_test.py
"""
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import sessions  # noqa: E402
from orchestrator.core.sessions import SessionManager  # noqa: E402


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs: list[str] = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def _warn_msgs(project_dir: Path) -> list[str]:
    cap = _Capture()
    sessions.logger.addHandler(cap)
    old = sessions.logger.level
    sessions.logger.setLevel(logging.WARNING)
    try:
        SessionManager._warn_project_trust(project_dir)
    finally:
        sessions.logger.removeHandler(cap)
        sessions.logger.setLevel(old)
    return cap.msgs


def test_warns_on_project_hooks():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "foreign"
        (proj / ".claude").mkdir(parents=True)
        (proj / ".claude" / "settings.json").write_text('{"hooks": {"Stop": []}}')
        msgs = _warn_msgs(proj)
    assert any("settings.json" in m for m in msgs), msgs
    print("OK предупреждение при linked-папке с project-хуками")


def test_warns_on_mcp_json():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "foreign"
        proj.mkdir()
        (proj / ".mcp.json").write_text("{}")
        msgs = _warn_msgs(proj)
    assert any(".mcp.json" in m for m in msgs), msgs
    print("OK предупреждение при .mcp.json в linked-папке")


def test_no_warn_on_clean():
    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "clean"
        proj.mkdir()
        # settings.json без хуков — не повод ругаться.
        (proj / ".claude").mkdir()
        (proj / ".claude" / "settings.json").write_text('{"model": "opus"}')
        msgs = _warn_msgs(proj)
    assert msgs == [], msgs
    print("OK нет предупреждения на чистой папке (без хуков/mcp)")


# ── _ensure_folder_trusted: доверие к папке сессии до запуска claude ─────

def test_ensure_folder_trusted_writes_flag():
    """Пустой конфиг → вписываем projects[<cwd>].hasTrustDialogAccepted=true."""
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "cfg"
        cfg.mkdir()
        SessionManager._ensure_folder_trusted(cfg, Path(d) / "proj")
        import json
        data = json.loads((cfg / ".claude.json").read_text())
        assert data["projects"][str(Path(d) / "proj")]["hasTrustDialogAccepted"] is True
    print("OK флаг доверия к папке вписывается в .claude.json")


def test_ensure_folder_trusted_preserves_other_projects():
    """Чужие записи в .claude.json не трогаем — только добавляем свою."""
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "cfg"
        cfg.mkdir()
        (cfg / ".claude.json").write_text(
            '{"projects": {"/other": {"hasTrustDialogAccepted": false, "x": 1}}}'
        )
        SessionManager._ensure_folder_trusted(cfg, Path(d) / "proj")
        import json
        data = json.loads((cfg / ".claude.json").read_text())
        assert data["projects"]["/other"] == {"hasTrustDialogAccepted": False, "x": 1}
        assert data["projects"][str(Path(d) / "proj")]["hasTrustDialogAccepted"] is True
    print("OK чужие проекты в .claude.json сохраняются")


def test_ensure_folder_trusted_already_trusted_no_rewrite():
    """Флаг уже стоит → файл не переписываем (mtime на месте)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "cfg"
        cfg.mkdir()
        cwd = Path(d) / "proj"
        p = cfg / ".claude.json"
        p.write_text('{"projects": {"%s": {"hasTrustDialogAccepted": true}}}' % cwd)
        before = p.stat().st_mtime_ns
        SessionManager._ensure_folder_trusted(cfg, cwd)
        assert p.stat().st_mtime_ns == before
    print("OK уже доверенная папка файл не переписывает")


def test_ensure_folder_trusted_bad_json_ignored():
    """Битый .claude.json не роняем и не трогаем — claude сам пересоздаст."""
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "cfg"
        cfg.mkdir()
        p = cfg / ".claude.json"
        p.write_text("{not json")
        SessionManager._ensure_folder_trusted(cfg, Path(d) / "proj")
        assert p.read_text() == "{not json"
    print("OK битый .claude.json не трогаем")


def test_ensure_folder_trusted_no_config_dir_noop():
    """config_dir не задан (учётка по умолчанию) — тихо проходим мимо."""
    SessionManager._ensure_folder_trusted(None, Path("/tmp/proj"))
    print("OK config_dir=None — no-op")


if __name__ == "__main__":
    test_warns_on_project_hooks()
    test_warns_on_mcp_json()
    test_no_warn_on_clean()
    test_ensure_folder_trusted_writes_flag()
    test_ensure_folder_trusted_preserves_other_projects()
    test_ensure_folder_trusted_already_trusted_no_rewrite()
    test_ensure_folder_trusted_bad_json_ignored()
    test_ensure_folder_trusted_no_config_dir_noop()
    print("ALL LINK-TRUST OK")
