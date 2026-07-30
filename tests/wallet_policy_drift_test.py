"""Сторож рассинхрона policy ↔ env живых сессий (кошелёк).

env процессу claude задаётся ОДИН РАЗ при старте: секрет, добавленный в
secrets.toml позже, в живую сессию не попадёт. Раньше это было НЕВИДИМО —
оператор видел только «модель не видит переменную» и искал причину в коде
(живой случай: секреты добавили через 48 минут после старта сессии).

Проверяем контракт:
  • policy изменилась → в топик сессии ОДИН раз уходит notice с ИМЕНАМИ
    появившихся/убранных переменных (значения секретов не печатаются);
  • повторные проверки той же policy молчат (не спамим каждые 30с);
  • сессия, у которой env совпадает с policy, notice НЕ получает;
  • остановленная сессия не тревожится (её env соберётся при старте);
  • после перезапуска сессии (session_env заново) сторож молчит до новой правки.

Запуск: .venv/bin/python tests/wallet_policy_drift_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.modules.wallet.module import WalletModule  # noqa: E402

_VALUE = "SHARED-VALUE-DO-NOT-LEAK"


def _write(path: Path, extra: str = "") -> None:
    path.write_text(
        "[secrets.llm]\n"
        f'value = "{_VALUE}"\n'
        'env = "IKAR_LLM_API_KEY"\n'
        'shared = true\n'
        'sessions = ["*"]\n' + extra,
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


_MATRIX = (
    "\n[secrets.matrix_login]\n"
    'value = "MX-LOGIN"\n'
    'env = "IKAR_MATRIX_LOGIN"\n'
    'shared = true\n'
    'sessions = ["*"]\n'
)


def _module(secrets: Path) -> tuple[WalletModule, list]:
    """Модуль с фейковым ядром: собираем notice'ы, ничего не запускаем."""
    notices: list[tuple[str, str]] = []
    cfg = SimpleNamespace(
        wallet_secrets_file=secrets, wallet_policy_edit=True,
        wallet_guard=True, sandbox="bwrap",
    )
    mod = WalletModule(cfg)

    sessions: list = []

    async def notice(session, text):
        notices.append((session.name, text))

    mod.core = SimpleNamespace(
        manager=SimpleNamespace(list_all=lambda: sessions),
        notice=notice,
        t=lambda key, **kw: f"{key}:{kw.get('changes', '')}",
    )
    return mod, notices


def _session(name: str, running: bool = True):
    return SimpleNamespace(name=name, running=running)


async def _tick(mod: WalletModule) -> None:
    """Один проход сторожа без ожидания интервала."""
    from orchestrator.modules.wallet import module as m
    old = m.POLICY_WATCH_SEC
    m.POLICY_WATCH_SEC = 0.01
    task = asyncio.create_task(mod._watch_policy())
    await asyncio.sleep(0.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    m.POLICY_WATCH_SEC = old


async def test_policy_change_notifies_once():
    tmp = Path(tempfile.mkdtemp(prefix="wallet_drift_"))
    secrets = tmp / "secrets.toml"
    _write(secrets)
    mod, notices = _module(secrets)
    sess = _session("ikar")
    mod.core.manager.list_all = lambda: [sess]

    # старт сессии: env зафиксирован (одна переменная)
    env = mod.session_env(sess)
    assert set(env) == {"IKAR_LLM_API_KEY"}, env
    assert env["IKAR_LLM_API_KEY"] == _VALUE

    # policy не менялась → сторож молчит
    await _tick(mod)
    assert notices == [], f"тревога на неизменной policy: {notices}"

    # добавили секрет ПОСЛЕ старта → ровно один notice с ИМЕНЕМ (не значением)
    _write(secrets, _MATRIX)
    await _tick(mod)
    assert len(notices) == 1, f"ожидался один notice: {notices}"
    name, text = notices[0]
    assert name == "ikar" and "IKAR_MATRIX_LOGIN" in text, text
    assert "появились" in text, text
    assert _VALUE not in text and "MX-LOGIN" not in text, "значение секрета в notice!"

    # повторные проходы той же policy — молчим (иначе спам каждые 30с)
    await _tick(mod)
    await _tick(mod)
    assert len(notices) == 1, f"сторож заспамил: {notices}"

    # сессию переоткрыли → env пересобран, тревоги снова нет
    mod.session_env(sess)
    await _tick(mod)
    assert len(notices) == 1, f"после переоткрытия должен молчать: {notices}"
    print("OK policy drift: один notice с именами, без значений, без спама")


async def test_stopped_session_is_not_bothered():
    """У остановленной сессии env соберётся при старте — тревожить не о чем."""
    tmp = Path(tempfile.mkdtemp(prefix="wallet_drift_stopped_"))
    secrets = tmp / "secrets.toml"
    _write(secrets)
    mod, notices = _module(secrets)
    sess = _session("ikar")
    mod.core.manager.list_all = lambda: [sess]
    mod.session_env(sess)

    sess.running = False
    _write(secrets, _MATRIX)
    await _tick(mod)
    assert notices == [], f"остановленную сессию тревожить не надо: {notices}"
    print("OK policy drift: остановленная сессия не тревожится")


async def test_resume_does_not_re_notify_known_drift():
    """Resume (session_env) не сбрасывает _env_notified — иначе дрифт, о
    котором уже сообщили, объявлялся бы при каждом возобновлении сессии.

    Раньше session_env делал _env_notified.pop на каждом вызове (а он зовётся
    на каждом resume), переоружая уведомление. Дедупликация (_env_notified ==
    fresh) обязана переживать resume, иначе она бесполезна.
    """
    tmp = Path(tempfile.mkdtemp(prefix="wallet_drift_resume_"))
    secrets = tmp / "secrets.toml"
    _write(secrets)
    mod, notices = _module(secrets)
    sess = _session("ikar")
    mod.core.manager.list_all = lambda: [sess]

    # Старт с базовой policy, затем секрет добавили на лету → дрифт.
    mod.session_env(sess)
    _write(secrets, _MATRIX)
    await _tick(mod)
    assert len(notices) == 1
    assert "ikar" in mod._env_notified, "сторож не пометил дрифт известным"

    # Resume: session_env зовётся снова — флаг «известно» должен остаться.
    mod.session_env(sess)
    assert "ikar" in mod._env_notified, "resume сбросил _env_notified — будет спам"


async def test_session_without_delivered_env_is_skipped():
    """Сессия, которой мы env не отдавали (стартовала до модуля), не сравнивается."""
    tmp = Path(tempfile.mkdtemp(prefix="wallet_drift_unknown_"))
    secrets = tmp / "secrets.toml"
    _write(secrets, _MATRIX)
    mod, notices = _module(secrets)
    mod.core.manager.list_all = lambda: [_session("someone")]
    await _tick(mod)
    assert notices == [], f"неизвестную сессию сравнивать не с чем: {notices}"
    print("OK policy drift: сессия без зафиксированного env пропускается")


def main() -> None:
    asyncio.run(test_policy_change_notifies_once())
    asyncio.run(test_resume_does_not_re_notify_known_drift())
    asyncio.run(test_stopped_session_is_not_bothered())
    asyncio.run(test_session_without_delivered_env_is_skipped())
    print("ALL WALLET-POLICY-DRIFT OK")


if __name__ == "__main__":
    main()
