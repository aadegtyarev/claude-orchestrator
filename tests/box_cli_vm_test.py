"""`claude-box --vm`: microVM как движок CLI (Слой 2, фаза 3).

Проверяем контракт среза, не поднимая VM (agent-vm/KVM на машине разработки нет —
и не должно быть нужно):
  • --vm == --engine agent-vm (один путь, а не два режима);
  • --vm вместе с другим --engine → честный отказ (код 2);
  • AGENT_VM_* читаются в EngineConfig теми же именами, что в orchestrator/config;
  • argv действительно собирается AgentVmRunner'ом (флаги VM доезжают);
  • корень установки claude-box в гостя НЕ монтируется (у --mount нет RO —
    иначе гость получал бы запись в код и в .env оператора);
  • AGENT_VM_MEMORY_GIB/CPUS не текут в окружение дочернего agent-vm (иначе он
    прочитает их сам и упадёт на том, что мы обещали проигнорировать);
  • нет бинаря/KVM → внятный отказ preflight и код 1, а не трейсбек;
  • --profile/--wallet под --vm отвергаются (F4/F1/F10), а не делают вид, что
    применились;
  • --help больше не числит --vm нереализованным.

Запуск: .venv/bin/python tests/box_cli_vm_test.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from box_cli import cli  # noqa: E402
from orchestrator.runners import agentvm  # noqa: E402
from orchestrator.runners.agentvm import AgentVmRunner  # noqa: E402


@contextlib.contextmanager
def env(**kw: str | None):
    """Временно выставить/снять переменные окружения (и вернуть как было)."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _exit_code(fn, *args) -> int:
    """Код SystemExit из parse_args (или AssertionError, если отказа не было)."""
    try:
        fn(*args)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    raise AssertionError(f"{args} должно было отказать")


# ── --vm == --engine agent-vm ────────────────────────────────────────────────
def test_vm_is_short_form_of_engine_agent_vm():
    assert cli.parse_args(["--vm"]).engine == "agent-vm"
    assert cli.parse_args(["--engine", "agent-vm"]).engine == "agent-vm"
    assert cli.parse_args(["--engine=agent-vm"]).engine == "agent-vm"
    # Дублирование одного и того же — не ошибка (оператор ничего не перепутал).
    assert cli.parse_args(["--vm", "--engine", "agent-vm"]).engine == "agent-vm"
    # Сквозные аргументы claude не страдают.
    opts = cli.parse_args(["--vm", "--", "--model", "opus"])
    assert opts.passthrough == ["--model", "opus"]
    print("OK --vm == --engine agent-vm")


def test_vm_conflicts_with_other_engine():
    """--vm --engine bwrap|off → код 2 (а не молчаливый приоритет одного из них)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert _exit_code(cli.parse_args, ["--vm", "--engine", "bwrap"]) == 2
        assert _exit_code(cli.parse_args, ["--engine=off", "--vm"]) == 2
    assert "несовместим" in err.getvalue(), err.getvalue()
    print("OK --vm + чужой --engine → честный отказ (код 2)")


def test_other_engines_untouched():
    """Существующее поведение не поехало: дефолт bwrap, off, мусор — отказ."""
    assert cli.parse_args([]).engine == "bwrap"
    assert cli.parse_args(["--engine", "off"]).engine == "off"
    with contextlib.redirect_stderr(io.StringIO()):
        assert _exit_code(cli.parse_args, ["--engine", "docker"]) == 2
    print("OK bwrap/off не изменились")


# ── EngineConfig: AGENT_VM_* ─────────────────────────────────────────────────
def test_engine_config_reads_agent_vm_env():
    with env(AGENT_VM_MEMORY_GIB="8", AGENT_VM_CPUS="4",
             AGENT_VM_IMAGE="ghcr.io/x:pin", AGENT_VM_HOST_IP="192.168.1.44",
             AGENT_VM_EGRESS_PROXY="http://192.168.1.44:9000",
             AGENT_VM_EGRESS_CA="/tmp/vault-ca.pem"):
        runner = cli.make_engine_runner("agent-vm", cli.repo_root())
        cfg = runner.config
        assert isinstance(runner, AgentVmRunner)
        assert cfg.agent_vm_memory_gib == 8.0 and cfg.agent_vm_cpus == 4
        assert cfg.agent_vm_image == "ghcr.io/x:pin"
        assert cfg.agent_vm_host_ip == "192.168.1.44"
        assert cfg.agent_vm_egress_proxy == "http://192.168.1.44:9000"
        assert cfg.agent_vm_egress_ca == Path("/tmp/vault-ca.pem")
        # claude_env в standalone пуст — это механизм оркестратора (F1);
        # раннер обязан это пережить: ни auth-конфликта, ни --allow-egress.
        assert cfg.claude_env == {}
        assert agentvm.auth_problem(cfg.claude_env) is None
        assert agentvm.egress_hosts(cfg.claude_env, cfg.agent_vm_host_ip) == []

        # «Выключено = не существует»: под bwrap этих значений в конфиге нет.
        bw = cli.make_engine_runner("bwrap", cli.repo_root()).config
        assert bw.agent_vm_image is None and bw.agent_vm_cpus is None

    # Пусто/не задано → None (пустая строка не значение, как в config.py).
    with env(AGENT_VM_MEMORY_GIB=None, AGENT_VM_CPUS="", AGENT_VM_IMAGE=None,
             AGENT_VM_HOST_IP=None, AGENT_VM_EGRESS_PROXY=None,
             AGENT_VM_EGRESS_CA=None):
        cfg = cli.make_engine_runner("agent-vm", cli.repo_root()).config
        assert cfg.agent_vm_cpus is None and cfg.agent_vm_memory_gib is None
        assert cfg.agent_vm_egress_ca is None
    print("OK AGENT_VM_* → EngineConfig (и только для agent-vm)")


def test_bad_agent_vm_number_is_honest_error():
    """AGENT_VM_CPUS=abc → отказ кодом 2 с внятным текстом, а не ValueError-трейсбек."""
    err = io.StringIO()
    with env(AGENT_VM_CPUS="abc"), contextlib.redirect_stderr(err):
        code = _exit_code(cli.make_engine_runner, "agent-vm", cli.repo_root())
    assert code == 2
    assert "AGENT_VM_CPUS" in err.getvalue(), err.getvalue()

    # Дробная память тоже отказ: agent-vm принимает только целые GiB («invalid
    # value '2.5' for '--memory <GIB>'»), и пропустить её значило бы уронить
    # запуск уже внутри agent-vm — после того как CLI сделал вид, что понял.
    err = io.StringIO()
    with env(AGENT_VM_MEMORY_GIB="2.5"), contextlib.redirect_stderr(err):
        code = _exit_code(cli.make_engine_runner, "agent-vm", cli.repo_root())
    assert code == 2
    assert "AGENT_VM_MEMORY_GIB" in err.getvalue(), err.getvalue()
    print("OK кривой AGENT_VM_CPUS/MEMORY_GIB → честный отказ")


# ── argv: флаги VM доезжают ──────────────────────────────────────────────────
def build_vm_argv() -> list[str]:
    """Собрать argv VM-запуска ровно так, как это делает main_async."""
    runner = cli.make_engine_runner("agent-vm", cli.repo_root())
    return cli.build_argv(runner, ["claude", "--model", "opus"], Path("/tmp"))


def test_vm_argv_via_agentvm_runner():
    with env(AGENT_VM_MEMORY_GIB="8", AGENT_VM_CPUS="4",
             AGENT_VM_IMAGE="ghcr.io/x:pin",
             AGENT_VM_EGRESS_PROXY="http://192.168.1.44:9000",
             AGENT_VM_EGRESS_CA="/tmp/vault-ca.pem"):
        argv = build_vm_argv()
    s = " ".join(argv)
    assert argv[:2] == ["agent-vm", "claude"], argv[:2]
    assert "--allow-host" in argv
    # Корень установки claude-box в гостя НЕ монтируется: у --mount в agent-vm
    # нет режима RO, а в корне лежит .env оператора с боевыми токенами бота.
    # Standalone CLI код оркестратора в госте не запускает (ни канала, ни хуков).
    assert f"--mount {cli.repo_root()}" not in s, "утечка корня установки в VM: " + s
    assert "--mount /tmp:/tmp" not in s, "cwd монтирует сам agent-vm"
    assert "--mount" not in s, "монтировать в standalone нечего: " + s
    assert "--memory 8 " in s + " " and "--memory 8G" not in s, s
    assert "--cpus 4" in s and "--image ghcr.io/x:pin" in s, s
    assert "--egress-proxy http://192.168.1.44:9000" in s, s
    assert "--egress-ca /tmp/vault-ca.pem" in s, s
    tail = argv[argv.index("--") + 1:]
    assert tail == ["--model", "opus"], tail
    print("OK argv VM: " + s)


def test_vm_argv_from_foreign_project_has_no_repo_mount():
    """Запуск над ЧУЖИМ проектом: в гостя едет только его каталог.

    Раньше wrap() безусловно добавлял `--mount <корень claude-box>`, а у
    `--mount` нет режима только-чтение — агент над любым чужим проектом получал
    RW-доступ к коду оркестратора и к `.env` с боевыми токенами оператора.
    """
    runner = cli.make_engine_runner("agent-vm", cli.repo_root())
    argv = cli.build_argv(runner, ["claude"], Path("/tmp/foreign-project"))
    s = " ".join(argv)
    assert str(cli.repo_root()) not in s, "корень установки утёк в VM: " + s
    assert "--mount" not in s, s
    assert argv[:2] == ["agent-vm", "claude"] and "--allow-host" in argv, argv
    print("OK чужой проект: корень claude-box в VM не монтируется")


def test_build_env_strips_agent_vm_aliases():
    """AGENT_VM_MEMORY_GIB/CPUS не доезжают до дочернего agent-vm.

    Эти имена agent-vm читает сам (env-алиасы своих флагов). Мы обещаем, что
    пустое/мусорное значение считается «не задано» — значит и в окружении его
    остаться не должно, иначе agent-vm упадёт на нём своим парсером
    («cannot parse integer from empty string»). AGENT_VM_IMAGE — наше имя
    (у agent-vm это AGENT_VM_IMAGE_TAG), его не трогаем.
    """
    with env(AGENT_VM_CPUS="", AGENT_VM_MEMORY_GIB="8",
             AGENT_VM_IMAGE="ghcr.io/x:pin"):
        vm_env = cli.build_env("agent-vm")
        assert "AGENT_VM_CPUS" not in vm_env, vm_env.get("AGENT_VM_CPUS")
        assert "AGENT_VM_MEMORY_GIB" not in vm_env
        assert vm_env["AGENT_VM_IMAGE"] == "ghcr.io/x:pin"
        # Под bwrap чистка не нужна и не делается («выключено = не существует»).
        assert cli.build_env("bwrap")["AGENT_VM_CPUS"] == ""
    print("OK build_env: алиасы agent-vm вычищены из окружения ребёнка")


def test_main_returns_code_on_bad_number_without_traceback():
    """cli.main([...]) с кривым AGENT_VM_CPUS отдаёт 2, а не выбрасывает SystemExit.

    Отказ рождается в main_async (внутри asyncio.run) — без перехвата SystemExit
    в main() он летел бы наружу сквозь sys.exit(main()).
    """
    err = io.StringIO()
    with env(AGENT_VM_CPUS="abc"), contextlib.redirect_stderr(err):
        code = cli.main(["--vm", "--", "--version"])
    assert code == 2, code
    assert "AGENT_VM_CPUS" in err.getvalue(), err.getvalue()
    print("OK main(): кривой AGENT_VM_CPUS → код 2 без трейсбека")


# ── preflight: нет agent-vm / нет KVM ────────────────────────────────────────
def _main_vm_with(which, kvm_exists: bool) -> tuple[int, str]:
    """cli.main(["--vm"]) с подменённым окружением preflight; (код, stderr)."""
    err = io.StringIO()
    with mock.patch.object(agentvm.shutil, "which", return_value=which), \
         mock.patch.object(agentvm.Path, "exists", return_value=kvm_exists), \
         contextlib.redirect_stderr(err):
        code = cli.main(["--vm"])
    return code, err.getvalue()


def test_vm_preflight_refuses_without_binary():
    code, msg = _main_vm_with(None, True)
    assert code == 1, code
    assert "agent-vm не установлен" in msg and "не готов" in msg, msg
    print("OK нет бинаря agent-vm → внятный отказ, код 1")


def test_vm_preflight_refuses_without_kvm():
    code, msg = _main_vm_with("/usr/bin/agent-vm", False)
    assert code == 1, code
    assert "/dev/kvm" in msg, msg
    # Фолбэк не должен звать в --engine off: «в VM» и «без изоляции» — разные
    # гарантии, подсовывать одно вместо другого нельзя.
    assert "--engine off" not in msg, msg
    print("OK нет /dev/kvm → внятный отказ, код 1")


# ── границы: --profile/--wallet под --vm ─────────────────────────────────────
def test_profile_under_vm_refused():
    """F4: agent-vm игнорирует CLAUDE_CONFIG_DIR — молча «применить» профиль нельзя."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert _exit_code(cli.parse_args, ["--profile", "work", "--vm"]) == 2
        assert _exit_code(cli.parse_args, ["--vm", "--profile=work"]) == 2
    msg = err.getvalue()
    assert "--profile" in msg and "CLAUDE_CONFIG_DIR" in msg, msg
    assert "bwrap" in msg, "надо сказать, где профиль работает"
    # Под bwrap/off профиль по-прежнему разбирается.
    assert cli.parse_args(["--profile", "work"]).profile == "work"
    print("OK --profile + --vm → честный отказ (код 2)")


def test_wallet_under_vm_parses():
    """--wallet + --vm БОЛЬШЕ не отвергается парсером: работоспособность зависит
    от ВИДА секрета (прокси/inject работают, host-passthrough — отказ кодом 2), а
    вид известен только после загрузки secrets.toml. Решение принимает
    box_cli.wallet.setup_wallet_intercept, не парсер (см. box_cli_wallet_test)."""
    opts = cli.parse_args(["--wallet", "svc", "--vm"])
    assert opts.wallet == "svc" and opts.engine == "agent-vm", opts
    opts = cli.parse_args(["--vm", "--wallet=svc"])
    assert opts.wallet == "svc" and opts.engine == "agent-vm", opts
    # Без --vm по-прежнему разбирается.
    assert cli.parse_args(["--wallet", "svc"]).wallet == "svc"
    print("OK --wallet + --vm разбирается (вид секрета решает рантайм)")


# ── справка ──────────────────────────────────────────────────────────────────
def test_help_does_not_call_vm_unimplemented():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            cli.parse_args(["--help"])
        except SystemExit as e:
            assert e.code == 0
    text = out.getvalue()
    assert "--vm" in text, "справка должна упоминать --vm"
    unimplemented = text.split("Не реализовано")[-1]
    assert "--vm" not in unimplemented, unimplemented
    # -p тоже уехал из «не реализовано» (unattended-срез), остался connect.
    assert "-p" not in unimplemented, unimplemented
    assert "connect" in unimplemented, unimplemented
    assert "agent-vm" in text
    print("OK --help: --vm больше не в «не реализовано»")


def test_help_vm_boundaries_are_readable():
    """Текст границ --vm — законченные фразы, понятные без архитектурного дока.

    Была оборванная фраза («…CLI откажет и объяснит» → сразу описание --profile)
    и объяснение через внутренние термины (F4/F10/MITM/CLAUDE_CONFIG_DIR).
    """
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            cli.parse_args(["--help"])
        except SystemExit as e:
            assert e.code == 0
    text = out.getvalue()
    vm_block = text.split("\n  --vm ", 1)[1].split("\n  --profile <", 1)[0]
    assert "--profile" in vm_block and "--wallet" in vm_block, vm_block
    # Сказано, ЧТО будет (отказ с кодом) и ЧТО делать вместо этого.
    assert "кодом 2" in vm_block, vm_block
    assert vm_block.count("без --vm") >= 2, vm_block
    # Обрыв на полуслове больше не проходит: каждая строка блока — либо конец
    # предложения, либо продолжение (не «объяснит» в никуда).
    assert "откажет и объяснит\n" not in vm_block, vm_block
    print("OK --help: границы --vm объяснены целыми фразами")


def main() -> None:
    test_vm_is_short_form_of_engine_agent_vm()
    test_vm_conflicts_with_other_engine()
    test_other_engines_untouched()
    test_engine_config_reads_agent_vm_env()
    test_bad_agent_vm_number_is_honest_error()
    test_vm_argv_via_agentvm_runner()
    test_vm_argv_from_foreign_project_has_no_repo_mount()
    test_build_env_strips_agent_vm_aliases()
    test_main_returns_code_on_bad_number_without_traceback()
    test_help_vm_boundaries_are_readable()
    test_vm_preflight_refuses_without_binary()
    test_vm_preflight_refuses_without_kvm()
    test_profile_under_vm_refused()
    test_wallet_under_vm_parses()
    test_help_does_not_call_vm_unimplemented()
    print("ALL BOX-CLI-VM OK")


if __name__ == "__main__":
    main()
