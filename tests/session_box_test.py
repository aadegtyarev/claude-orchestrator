"""Выбор изоляции на сессию: флаг `--box` у /new (дефолт — SANDBOX из .env).

Зачем. До этого движок изоляции был ОДИН на весь оркестратор: чтобы дать одной
сессии полный доступ к хосту (или наоборот запереть её), приходилось менять
SANDBOX и перезапускать всё. Теперь движок — свойство сессии: `/new имя --box
off` поднимает её без песочницы, остальные продолжают жить под дефолтным.

Проверяем контракт:
  • разбор флага: `--box off`, `--box=vm`, любое место в строке, имя/путь целы;
  • значение без флага → None (движок = дефолт .env), прежнее поведение;
  • неизвестное значение и недоступный движок → отказ С ПРИЧИНОЙ, не тихий
    откат к дефолту (иначе оператор думает, что сессия изолирована иначе);
  • движок живёт с сессией: пишется в .sessions.json и читается назад, старые
    записи (без поля) = дефолт;
  • ВСЯ обвязка сессии следует её движку, а не .env: раннер, префикс /bash,
    docker-прокси, кошелёк;
  • сессия без изоляции видна оператору (пометка в /new и /list).

Запуск: .venv/bin/python tests/session_box_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.config import Config  # noqa: E402
from orchestrator.core.app import OrchestratorCore, UserError  # noqa: E402
from orchestrator.core.texts import get_texts  # noqa: E402
from orchestrator.core.sessions import Session, SessionManager  # noqa: E402


def _cfg(sandbox: str = "bwrap", **kw) -> Config:
    return replace(Config.from_env(), sandbox=sandbox, **kw)


def _core(cfg: Config) -> OrchestratorCore:
    """Ядро без адаптеров: нужны только config/manager и тексты."""
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.config = cfg
    core._texts = get_texts(cfg.bot_lang)
    core.manager = SessionManager.__new__(SessionManager)
    core.manager.config = cfg
    return core


def _session(name: str = "s", box: str | None = None) -> Session:
    return Session(
        name=name, port=0, session_dir=Path("/tmp") / name,
        claude_session_id="00000000-0000-0000-0000-000000000000", sandbox=box,
    )


# ── разбор аргументов /new ──────────────────────────────────────────


def test_parse_flag_anywhere():
    """Флаг вырезается до разбора имени/пути — имя с пробелами не ломается."""
    p = OrchestratorCore.parse_new_args
    assert p("my project --box off") == ("my project", None, "off", None)
    assert p("--box off my project") == ("my project", None, "off", None)
    assert p("проект /home/u/p --box=vm") == ("проект", "/home/u/p", "vm", None)
    assert p("--box off /home/u/p") == ("p", "/home/u/p", "off", None)
    print("OK --box разбирается в любом месте строки, имя и путь целы")


def test_parse_without_flag_unchanged():
    """Без флагов — ровно прежний разбор (значения флагов None)."""
    p = OrchestratorCore.parse_new_args
    assert p("my project") == ("my project", None, None, None)
    assert p("'имя в кавычках'") == ("имя в кавычках", None, None, None)
    assert p("/home/u/proj") == ("proj", "/home/u/proj", None, None)
    assert p("имя ~/proj") == ("имя", "~/proj", None, None)
    assert p("") == ("", None, None, None)
    print("OK без --box разбор /new не изменился")


def test_parse_flag_without_value():
    """`--box` без значения → пустая строка, а не молчаливый дефолт."""
    assert OrchestratorCore.parse_new_args("имя --box") == ("имя", None, "", None)
    print("OK --box без значения не проглатывается как дефолт")


def test_parse_profile_flag():
    """`--profile` разбирается так же, как --box, и уживается с ним."""
    p = OrchestratorCore.parse_new_args
    assert p("имя --profile work") == ("имя", None, None, "work")
    assert p("--profile=work имя /home/u/p") == ("имя", "/home/u/p", None, "work")
    assert p("имя --box off --profile work") == ("имя", None, "off", "work")
    # Кавычки вокруг значения снимаются: `--profile ""` — «явно без профиля».
    assert p('имя --profile ""') == ("имя", None, None, "")
    assert p("имя --profile ''") == ("имя", None, None, "")
    assert p("имя --profile=") == ("имя", None, None, "")
    assert p("имя --profile") == ("имя", None, None, "")
    print("OK --profile разбирается в любом месте и уживается с --box")


# ── проверка значения ───────────────────────────────────────────────


def test_resolve_aliases():
    core = _core(_cfg("bwrap"))
    assert core.resolve_box(None) is None  # флага не было → дефолт .env
    for raw in ("off", "OFF", "none", "no", "0"):
        assert core.resolve_box(raw) == "off", raw
    assert core.resolve_box("bwrap") == "bwrap"
    print("OK синонимы --box: off/none/no/0 → off, bwrap → bwrap")


def test_resolve_rejects_unknown():
    core = _core(_cfg("bwrap"))
    try:
        core.resolve_box("firejail")
    except UserError as e:
        assert "firejail" in str(e) and "off" in str(e), e
    else:
        raise AssertionError("неизвестный движок должен отвергаться")
    # Пустое значение (`--box` без аргумента) — тоже отказ, а не тихий дефолт:
    # флаг набран, значит оператор чего-то хотел, и молча дать ему дефолтную
    # изоляцию значит соврать. (Проверка со `else` — иначе тест зелёный и когда
    # исключения нет.)
    for empty in ("", "   ", "="):
        try:
            core.resolve_box(empty)
        except UserError as e:
            assert "off" in str(e), e
        else:
            raise AssertionError(f"пустое --box {empty!r} должно отвергаться")
    print("OK неизвестное и пустое значение --box → отказ со списком допустимых")


def test_quoted_name_keeps_flag_literal():
    """Имя целиком в кавычках берётся буквально — «--box» внутри принадлежит ему.

    Иначе кавычки, которыми пользователь как раз защищает содержимое, молча
    съедали бы кусок имени."""
    got = OrchestratorCore.parse_new_args('"weird --box off name"')
    assert got == ("weird --box off name", None, None, None), got
    # То же и для --profile: внутри кавычек он часть имени.
    got = OrchestratorCore.parse_new_args('"имя --profile work"')
    assert got == ("имя --profile work", None, None, None), got
    # А снаружи кавычек флаг работает как обычно.
    assert OrchestratorCore.parse_new_args('"имя" --box off') == ("имя", None, "off", None)
    print("OK кавычки вокруг всего имени защищают «--box» внутри")


def test_resume_rechecks_engine():
    """Движок сессии перепроверяется на resume, а не только при создании.

    Оператор мог сменить SANDBOX между запусками: сохранённая agent-vm-сессия
    без своей стартовой обвязки поднялась бы битой вместо внятного отказа."""
    from orchestrator.core.sessions import SessionError

    with tempfile.TemporaryDirectory() as d:
        mgr = SessionManager(_cfg("bwrap", sessions_dir=Path(d)))
        stale = _session("stale", box="agent-vm")
        mgr._by_name["stale"] = stale
        try:
            asyncio.run(mgr._resume_locked(stale))
        except SessionError as e:
            assert "agent-vm" in str(e), e
        else:
            raise AssertionError("resume должен отказать на недоступном движке")
        assert stale.port == 0, "порт не должен выделяться до проверки движка"
        # clear() — тот же путь старта существующей сессии, тот же гвард:
        # /clear на давно остановленной сессии со stale-движком не должен
        # пытаться поднять её недоступным раннером.
        alive = stale.claude_session_id
        try:
            asyncio.run(mgr.clear(stale))
        except SessionError as e:
            assert "agent-vm" in str(e), e
        else:
            raise AssertionError("clear должен отказать на недоступном движке")
        assert stale.claude_session_id == alive, "clear сменил id, не проверив движок"
    print("OK resume и clear перепроверяют движок сессии (SANDBOX мог смениться)")


def test_resolve_rejects_engine_needing_startup_wiring():
    """agent-vm нельзя включить одной сессии: он перестраивает конфиг на старте.

    Молчаливо свалиться в bwrap тут нельзя — оператор попросил microVM и должен
    узнать, что её не будет, до того как отдаст сессии задачу."""
    core = _core(_cfg("bwrap"))
    try:
        core.resolve_box("vm")
    except UserError as e:
        assert "SANDBOX=agent-vm" in str(e), e
    else:
        raise AssertionError("agent-vm под bwrap-дефолтом должен отвергаться")
    # А под SANDBOX=agent-vm — можно (это и есть дефолт), как и уйти в off/bwrap.
    vm_core = _core(_cfg("agent-vm"))
    assert vm_core.resolve_box("vm") == "agent-vm"
    assert vm_core.resolve_box("off") == "off"
    assert vm_core.resolve_box("bwrap") == "bwrap"
    print("OK agent-vm выбирается только при SANDBOX=agent-vm, с внятной причиной")


# ── движок живёт с сессией ──────────────────────────────────────────


def test_engine_of_defaults_to_env():
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = _cfg("bwrap")
    assert mgr.engine_of(_session()) == "bwrap"          # None → дефолт
    assert mgr.engine_of(_session(box="off")) == "off"   # свой движок сильнее
    assert mgr.engine_of(None) == "bwrap"                # вне сессии — дефолт
    print("OK engine_of: своё значение сессии > SANDBOX > (вне сессии) дефолт")


def test_runner_follows_session():
    """Раннер берётся под движок СЕССИИ, а префикс /bash — соответственно пуст."""
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = _cfg("bwrap")
    mgr._docker_proxies = {}
    off = _session("off-one", box="off")
    assert mgr.runner_for(off).name == "direct", mgr.runner_for(off).name
    assert mgr.runner_for(_session()).name == "bwrap"
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        assert mgr.sandbox_prefix(work, [work], off) == []
        assert mgr.sandbox_prefix(work, [work], _session()) != []
    print("OK раннер и префикс /bash следуют движку сессии")


def test_state_roundtrip():
    """Движок переживает рестарт оркестратора (иначе resume поднял бы иначе)."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg("bwrap", sessions_dir=Path(d))
        mgr = SessionManager.__new__(SessionManager)
        mgr.config = cfg
        mgr._by_name = {
            "a": _session("a", box="off"),
            "b": _session("b"),
        }
        mgr.save_state()
        raw = json.loads((Path(d) / ".sessions.json").read_text())
        assert {i["name"]: i["sandbox"] for i in raw} == {"a": "off", "b": None}, raw
        # Запись, созданная ДО флага (поля нет вовсе) → дефолт, как раньше.
        raw.append({
            "name": "old", "bindings": {}, "port": 0, "cwd": str(Path(d) / "old"),
            "claude_session_id": "x", "title": "old",
        })
        (Path(d) / ".sessions.json").write_text(json.dumps(raw))
        fresh = SessionManager.__new__(SessionManager)
        fresh.config = cfg
        fresh._by_name = {}
        fresh.load_state()
        assert fresh._by_name["a"].sandbox == "off"
        assert fresh._by_name["b"].sandbox is None
        assert fresh._by_name["old"].sandbox is None
        assert fresh.engine_of(fresh._by_name["old"]) == "bwrap"
    print("OK движок сессии пишется в .sessions.json и читается назад")


def test_create_rejects_unavailable_engine():
    """manager.create — второй рубеж: отказ ДО создания папок и старта."""
    from orchestrator.core.sessions import SessionError

    with tempfile.TemporaryDirectory() as d:
        mgr = SessionManager(_cfg("bwrap", sessions_dir=Path(d)))
        try:
            asyncio.run(mgr.create("x", None, "agent-vm"))
        except SessionError as e:
            assert "agent-vm" in str(e), e
        else:
            raise AssertionError("create должен отказать на недоступном движке")
        assert not (Path(d) / "x").exists(), "папка сессии не должна появиться"
    print("OK create отказывает на недоступном движке до создания сессии")


# ── обвязка сессии: docker и кошелёк ────────────────────────────────


def test_docker_proxy_skipped_without_bwrap():
    """docker-прокси — фича bwrap: сессии без изоляции сокет не подсовываем."""
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = _cfg("bwrap", sandbox_docker=True)
    mgr._docker_proxies = {}
    assert asyncio.run(mgr._ensure_docker_proxy(_session(box="off"))) is None
    print("OK docker-прокси не поднимается для сессии без bwrap")


def test_wallet_skipped_without_bwrap():
    """Кошелёк не притворяется работающим в сессии, где его провода не подключены.

    Под `--box off` $HOME сессии — настоящий дом оператора: ~/.wallet.json туда
    не пишем, шимы в PATH не кладём, env-маркеры не выдаём."""
    from orchestrator.modules.wallet.module import WalletModule

    mod = WalletModule.__new__(WalletModule)
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = _cfg("bwrap")
    mod.core = SimpleNamespace(manager=mgr)
    off, normal = _session(box="off"), _session()
    assert mod.applies_to(normal) and not mod.applies_to(off)
    assert mod.session_path(off) == []
    mod.store = SimpleNamespace(load=lambda: {})
    assert mod.session_env(off) == {}
    print("OK кошелёк выключен в сессии без bwrap (нет провижна/шимов/env)")


def test_live_two_sessions_different_isolation():
    """ЖИВЬЁМ: соседние сессии одного оркестратора реально изолированы по-разному.

    Гоняем настоящую команду под префиксом каждой сессии: у дефолтной (bwrap)
    домашние секреты хоста НЕ видны, у `--box off` — видны (в этом и смысл
    флага). Без bwrap в окружении тест пропускаем."""
    import shutil
    import subprocess

    if shutil.which("bwrap") is None:
        print("SKIP живой прогон: bwrap не установлен")
        return
    mgr = SessionManager.__new__(SessionManager)
    mgr.config = _cfg("bwrap")
    mgr._docker_proxies = {}
    probe = Path.home() / ".ssh"
    if not probe.exists():
        probe = Path.home()
    with tempfile.TemporaryDirectory() as d:
        work = Path(d)
        home = work / "home"
        home.mkdir()

        def seen(session) -> bool:
            prefix = mgr.sandbox_prefix(work, [work], session)
            r = subprocess.run(
                [*prefix, "test", "-e", str(probe)], timeout=60,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return r.returncode == 0

        boxed = _session("boxed")
        boxed.session_dir = work
        opened = _session("opened", box="off")
        opened.session_dir = work
        mgr.session_home = lambda s, _h=home: _h  # приватный $HOME под bwrap
        assert not seen(boxed), f"{probe} видно из песочницы bwrap — изоляции нет"
        assert seen(opened), f"{probe} не видно при --box off — движок не применился"
    print("OK живьём: bwrap-сессия не видит дом хоста, --box off видит")


# ── видно оператору ─────────────────────────────────────────────────


def test_operator_sees_non_default_box():
    core = _core(_cfg("bwrap"))
    assert core.box_note(_session()) == "" and core.box_mark(_session()) == ""
    off = _session(box="off")
    note, mark = core.box_note(off), core.box_mark(off)
    assert "off" in note and "bwrap" in note, note
    # При активном кошельке оператору сразу говорим, что в этой сессии его нет
    # (модель там видит и файл секретов, и провижн соседей — тот же UID).
    with_wallet = _core(_cfg("bwrap", modules=("wallet",)))
    assert "ошелёк" in with_wallet.box_note(off), with_wallet.box_note(off)
    assert "ошелёк" not in with_wallet.box_note(_session()), "ложная тревога"
    assert "off" in mark, mark
    # agent-vm оператор видит коротким именем — тем же, что пишет во флаге.
    assert core.box_name("agent-vm") == "vm"
    assert "off" in core.box_choices_label()
    print("OK нестандартная изоляция помечена в ответе /new и в /list")


def main() -> None:
    test_parse_flag_anywhere()
    test_parse_without_flag_unchanged()
    test_parse_flag_without_value()
    test_resolve_aliases()
    test_resolve_rejects_unknown()
    test_quoted_name_keeps_flag_literal()
    test_resolve_rejects_engine_needing_startup_wiring()
    test_engine_of_defaults_to_env()
    test_runner_follows_session()
    test_state_roundtrip()
    test_create_rejects_unavailable_engine()
    test_resume_rechecks_engine()
    test_docker_proxy_skipped_without_bwrap()
    test_wallet_skipped_without_bwrap()
    test_live_two_sessions_different_isolation()
    test_operator_sees_non_default_box()
    print("ALL SESSION-BOX OK")


if __name__ == "__main__":
    main()
