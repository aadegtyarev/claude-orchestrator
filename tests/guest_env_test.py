"""CLAUDE_ENV_* доезжают в гостя agent-vm, а CLAUDE_CONFIG_DIR честно отказывает.

Баг: env процесса claude в гостя НЕ течёт (замерено живьём — в госте
`ANTHROPIC_BASE_URL=unset`). Оператор задаёт CLAUDE_ENV_ANTHROPIC_BASE_URL
(прокси modelpipe) и считает, что сессия ходит через него, а под agent-vm она
шла напрямую — молчаливая подмена бэкенда, маршрутизации моделей и биллинга.

Починка: под agent-vm кладём их в блок `env` файла настроек — он монтируется в
гостя, и его читает САМ клиент (проверено живьём: запросы уходят на заданный
адрес). Loopback при этом переписываем на адрес хоста с точки зрения гостя.

Запуск: .venv/bin/python tests/guest_env_test.py
"""
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.config import Config  # noqa: E402
from orchestrator.core.sessions import SessionManager  # noqa: E402
from orchestrator.runners.agentvm import auth_problem, egress_hosts  # noqa: E402


def _manager(sandbox: str, claude_env: dict) -> SessionManager:
    cfg = replace(Config.from_env(), sandbox=sandbox, claude_env=claude_env)
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = cfg
    return mgr


def test_rewrite_loopback_to_host_lan_ip():
    """Loopback в CLAUDE_ENV_* переписывается на LAN-адрес хоста.

    Внутри microVM «127.0.0.1» — это сам гость. Замерено живьём: хостовое
    gateway-имя прокси agent-vm не маршрутизирует, а LAN-адрес хоста он
    обходит (кладёт в no_proxy гостя) — по нему сервис хоста ДОСТУПЕН.
    """
    ip = "10.0.0.5"
    env, bad = Config._rewrite_env_for_guest(
        {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}, ip
    )
    assert not bad, bad
    assert env["ANTHROPIC_BASE_URL"] == f"http://{ip}:8787", env
    # URL БЕЗ порта тоже loopback (раньше проскакивал как «внешний»).
    env_np, _ = Config._rewrite_env_for_guest({"X": "http://127.0.0.1/v1"}, ip)
    assert env_np["X"] == f"http://{ip}/v1", env_np
    # Внешние адреса и не-URL не трогаем; loopback в query — не адрес сервиса.
    for value in ("https://api.anthropic.com", "opus",
                  "https://ext/cb?next=http://127.0.0.1:9/x"):
        keep, _ = Config._rewrite_env_for_guest({"X": value}, ip)
        assert keep["X"] == value, keep
    print("OK loopback переписан на LAN-адрес хоста")


def test_egress_allowed_for_host_proxy():
    """Раннер открывает гостю РОВНО адрес хоста (public_only запрещает RFC1918)."""
    ip = "10.0.0.5"
    assert egress_hosts({"ANTHROPIC_BASE_URL": f"http://{ip}:8787"}, ip) == [ip]
    # Без хостового адреса в env ничего не открываем.
    assert egress_hosts({"X": "https://api.anthropic.com"}, ip) == []
    print("OK --allow-egress выдаётся только под хостовый прокси")


def test_own_proxy_requires_token():
    """Свой base_url без токена → внятный отказ, а не «Execution error».

    Замерено живьём: в госте у claude своих кред нет — их подставляет прокси
    agent-vm и только для СВОЕГО эндпоинта. При своём base_url без токена
    claude падает до запроса; с токеном тот же путь работает (запросы дошли
    до хостового прокси). Проверяем чистую функцию, а не preflight целиком:
    тот сперва смотрит наличие бинаря/KVM и в CI отвечал бы «не установлен».
    """
    base = {"ANTHROPIC_BASE_URL": "http://192.168.1.44:8787"}
    msg = auth_problem(base)
    assert msg and "ANTHROPIC_AUTH_TOKEN" in msg, msg
    assert auth_problem({**base, "ANTHROPIC_AUTH_TOKEN": "t"}) is None
    assert auth_problem({**base, "ANTHROPIC_API_KEY": "k"}) is None
    # Без своего base_url претензий нет — трафик ведёт сам agent-vm.
    assert auth_problem({"ANTHROPIC_MODEL": "opus"}) is None
    print("OK свой base_url без токена — внятный отказ")


def _write_settings(mgr, tmp: Path, session_name="s1"):
    """Прогнать генератор настроек и вернуть разобранный JSON."""
    from types import SimpleNamespace

    session = SimpleNamespace(
        name=session_name, session_dir=tmp, linked_path=None, port=1234,
        sandbox=None,  # движок сессии = дефолт .env (флаг --box не задан)
    )
    mgr._write_claude_settings(session)
    return json.loads((tmp / ".claude" / "settings.local.json").read_text())


def test_agentvm_puts_claude_env_into_settings(tmp_path=None):
    tmp = Path(tmp_path or "/tmp/orch-guest-env-test-vm")
    (tmp / ".claude").mkdir(parents=True, exist_ok=True)
    mgr = _manager("agent-vm", {"ANTHROPIC_MODEL": "opus"})
    data = _write_settings(mgr, tmp)
    assert data.get("env", {}).get("ANTHROPIC_MODEL") == "opus", data.get("env")
    # В env-блоке может лежать токен прокси → файл не должен быть читаем
    # другими пользователями хоста (как соседний hook_dispatch.py).
    mode = (tmp / ".claude" / "settings.local.json").stat().st_mode & 0o777
    assert mode == 0o600, oct(mode)
    print("OK под agent-vm CLAUDE_ENV_* в env-блоке, файл 0600")


def test_bwrap_settings_have_no_env_block(tmp_path=None):
    """Под bwrap ничего не меняем: там env процесса работает (проверенный путь)."""
    tmp = Path(tmp_path or "/tmp/orch-guest-env-test-bw")
    (tmp / ".claude").mkdir(parents=True, exist_ok=True)
    mgr = _manager("bwrap", {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"})
    data = _write_settings(mgr, tmp)
    assert "env" not in data, data.get("env")
    print("OK под bwrap env-блок не добавляется (поведение не меняется)")


def test_interactive_tools_denied(tmp_path=None):
    """Режим плана и вопрос-меню запрещены механически, а не только промптом.

    Инцидент ikar 2026-08-12: модель вошла в план-режим (промпт канала это
    запрещал словами) и позвала ExitPlanMode. Диалог одобрения плана в
    headless-PTY не рисуется и в релей разрешений не уходит — PreToolUse
    прилетел, PostToolUse нет, ход завис навсегда, очередь сообщений
    оператора перестала разбираться. Запрет в deny превращает это в
    «tool not allowed» и продолжение текстом.
    """
    tmp = Path(tmp_path or "/tmp/orch-guest-env-test-deny")
    (tmp / ".claude").mkdir(parents=True, exist_ok=True)
    mgr = _manager("bwrap", {})
    deny = _write_settings(mgr, tmp)["permissions"]["deny"]
    for tool in ("AskUserQuestion", "EnterPlanMode", "ExitPlanMode"):
        assert tool in deny, (tool, deny)
    print("OK AskUserQuestion и режим плана запрещены в настройках сессии")


def test_host_ip_override_wins_over_autodetect():
    """`AGENT_VM_HOST_IP` — ручной путь, когда авто-выбор промахнулся.

    Автоопределение берёт src маршрута по умолчанию и ошибается при VPN или
    нескольких интерфейсах; без оверрайда сессия в microVM молча не достучится
    до прокси оператора и оркестратора. Документация обещала этот параметр
    раньше, чем он появился в коде — тест сторожит обещание."""
    keep = os.environ.get("AGENT_VM_HOST_IP")
    os.environ["AGENT_VM_HOST_IP"] = "10.11.12.13"
    try:
        cfg = Config.from_env()
        if cfg.sandbox != "agent-vm":  # дефолт .env — bwrap: подменяем движок
            os.environ["SANDBOX"] = "agent-vm"
            cfg = Config.from_env()
        assert cfg.agent_vm_host_ip == "10.11.12.13", cfg.agent_vm_host_ip
        # Пустое значение = «не задано» → возвращаемся к автоопределению.
        os.environ["AGENT_VM_HOST_IP"] = "   "
        auto = Config.from_env().agent_vm_host_ip
        assert auto != "10.11.12.13"
    finally:
        os.environ.pop("SANDBOX", None)
        if keep is None:
            os.environ.pop("AGENT_VM_HOST_IP", None)
        else:
            os.environ["AGENT_VM_HOST_IP"] = keep
    print("OK AGENT_VM_HOST_IP: ручное значение сильнее автоопределения")


def main():
    test_rewrite_loopback_to_host_lan_ip()
    test_egress_allowed_for_host_proxy()
    test_own_proxy_requires_token()
    test_agentvm_puts_claude_env_into_settings()
    test_bwrap_settings_have_no_env_block()
    test_interactive_tools_denied()
    test_host_ip_override_wins_over_autodetect()
    print("ALL GUEST-ENV OK")


if __name__ == "__main__":
    main()
