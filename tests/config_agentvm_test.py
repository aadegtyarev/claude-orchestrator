"""Config.guest_orch_host — адрес оркестратора С ТОЧКИ ЗРЕНИЯ СЕССИИ (гостя).

Переключение движка изоляции — ОДНО действие (SANDBOX в .env): guest-facing адрес
выводится автоматически. Под agent-vm гость VM не видит хостовый loopback —
microsandbox мапит host.microsandbox.internal на хостовый 127.0.0.1 (+ раннер даёт
--allow-host); reply-сервер при этом остаётся на orch_host (bind, host-side).

Запуск: .venv/bin/python tests/config_agentvm_test.py
"""
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.config import (  # noqa: E402
    AGENT_VM_GUEST_HOST, Config, env_number,
)


def _base() -> Config:
    return Config.from_env()


def test_guest_host_bwrap_off_is_loopback():
    """bwrap/off: гость = orch_host (общий с хостом loopback) — как раньше."""
    base = _base()
    for sb in ("bwrap", "off"):
        c = replace(base, sandbox=sb, orch_host="127.0.0.1")
        assert c.guest_orch_host == "127.0.0.1", sb
    print("OK guest_orch_host: bwrap/off → orch_host (127.0.0.1)")


def test_guest_host_agentvm_is_gateway():
    """agent-vm: гость видит хост по имени microsandbox-гейтвея; bind (orch_host)
    остаётся хостовым loopback — reply_server биндится на него, не на имя гостя."""
    c = replace(_base(), sandbox="agent-vm", orch_host="127.0.0.1")
    assert c.guest_orch_host == AGENT_VM_GUEST_HOST == "host.microsandbox.internal"
    assert c.orch_host == "127.0.0.1", "bind остаётся host-resolvable loopback"
    print("OK guest_orch_host: agent-vm → host.microsandbox.internal (bind ≠ гость)")


def test_one_action_switch():
    """Смена ТОЛЬКО sandbox переключает guest-facing адрес — без правки orch_host."""
    base = replace(_base(), orch_host="127.0.0.1")
    assert replace(base, sandbox="bwrap").guest_orch_host == "127.0.0.1"
    assert replace(base, sandbox="agent-vm").guest_orch_host == "host.microsandbox.internal"
    print("OK переключение движка = одно действие (только SANDBOX)")


def test_bad_agent_vm_number_is_honest_error():
    """AGENT_VM_MEMORY_GIB=abc в .env → внятный SystemExit, а не сырой ValueError.

    Оркестратор — прод-путь: раньше кривое значение роняло старт трейсбеком
    `ValueError: could not convert string to float: 'abc'`, из которого не видно
    ни имени переменной, ни того, что от неё ждут. standalone claude-box на том
    же имени давал честный отказ — расхождение убрано.
    """
    # «8.5» — тоже отказ: agent-vm принимает только целые GiB, и раньше такое
    # значение проходило конфиг, чтобы уронить КАЖДУЮ сессию уже внутри VM.
    cases = (("AGENT_VM_MEMORY_GIB", "abc"), ("AGENT_VM_MEMORY_GIB", "8.5"),
             ("AGENT_VM_CPUS", "2.5"))
    for name, raw in cases:
        old = os.environ.get(name)
        os.environ[name] = raw
        try:
            Config.from_env()
        except SystemExit as e:
            msg = str(e)
            assert name in msg and raw in msg, msg
            assert "ожидалось" in msg, msg
        except ValueError as e:  # pragma: no cover — это и есть починенный баг
            raise AssertionError(f"сырой ValueError вместо отказа: {e}") from e
        else:
            raise AssertionError(f"{name}={raw} должно было отказать")
        finally:
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    # Пусто/не задано — по-прежнему None (пустая строка не значение).
    assert env_number("AGENT_VM_CPUS_NO_SUCH_VAR", int) is None
    os.environ["AGENT_VM_CPUS_TMP"] = "  "
    assert env_number("AGENT_VM_CPUS_TMP", int) is None
    os.environ["AGENT_VM_CPUS_TMP"] = "4"
    assert env_number("AGENT_VM_CPUS_TMP", int) == 4
    os.environ.pop("AGENT_VM_CPUS_TMP", None)
    print("OK config: кривой AGENT_VM_* → честный отказ, а не ValueError")


def main():
    test_guest_host_bwrap_off_is_loopback()
    test_guest_host_agentvm_is_gateway()
    test_one_action_switch()
    test_bad_agent_vm_number_is_honest_error()
    print("ALL CONFIG-AGENTVM OK")


if __name__ == "__main__":
    main()
