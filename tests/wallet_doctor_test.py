"""`wallet doctor` — сверка «policy обещает» ↔ «что реально в окружении».

Зачем команда. Переменные кошелька задаются процессу ОДИН РАЗ при старте сессии,
а потерять их легко уже после: `source .env` со строкой `NAME=` перезапишет
значение пустым; секрет, добавленный в policy позже, не появится вовсе. Снаружи
это выглядит как «кошелёк не работает», а проверить `env`/`printenv` модель НЕ
может — это запрещено промптом и режется классификатором. Живой случай (ikar,
2026-07-25): модель час искала несуществующий баг, сама затерев значение
`source .env`, потому что отличить «нет секрета» от «нельзя посмотреть» было
нечем.

Проверяем контракт:
  • всё на месте (shared=значение, inject=маркер) → код 0, «Всё на месте»;
  • ПУСТАЯ переменная → диагноз «затёрли после старта» + подсказка про source .env;
  • переменной НЕТ → диагноз «добавили в policy после старта, переоткрой сессию»;
  • inject-маркер перезаписан чужим значением → «инъекция не сработает»;
  • ЗНАЧЕНИЯ секретов не печатаются ни в одном из случаев;
  • секретов с env нет → честное «сверять нечего», код 0.

Запуск: .venv/bin/python tests/wallet_doctor_test.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

WALLET = Path(__file__).parent.parent / "bin" / "wallet"
_SHARED_VALUE = "SHARED-VALUE-DO-NOT-LEAK"
_INJECT_VALUE = "INJECT-VALUE-DO-NOT-LEAK"

_SECRETS = (
    "[secrets.llm]\n"
    f'value = "{_SHARED_VALUE}"\n'
    'env = "MY_LLM_KEY"\n'
    "shared = true\n"
    'sessions = ["*"]\n'
    "\n"
    "[secrets.deploy]\n"
    f'value = "{_INJECT_VALUE}"\n'
    'env = "DEPLOY_TOKEN"\n'
    'sessions = ["*"]\n'
    'commands = ["curl *"]\n'
)


def _run_doctor(tmp: Path, env_extra: dict[str, str | None]) -> subprocess.CompletedProcess:
    """Поднять демон на временном secrets.toml и позвать `wallet doctor`."""
    env = dict(os.environ)
    env["WALLET_FILE"] = str(tmp / "w.json")
    for k, v in env_extra.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return subprocess.run(
        [sys.executable, str(WALLET), "doctor"],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp),
    )


def _with_daemon(tmp: Path, body):
    """Запустить демон в отдельном процессе на время body() (stdlib-CLI ходит по HTTP)."""
    runner = tmp / "d.py"
    runner.write_text(
        "import asyncio, sys\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent)!r})\n"
        "from pathlib import Path\n"
        "from vault.cli import build_daemon, write_wallet\n"
        "async def main():\n"
        "    d = build_daemon(Path('s.toml'), assume_yes=True)\n"
        "    await d.start()\n"
        "    write_wallet(Path('w.json'), d.url, d.issue_token('dev', Path.cwd()), 'dev')\n"
        "    print('READY', flush=True)\n"
        "    await asyncio.sleep(90)\n"
        "    await d.stop()\n"
        "asyncio.run(main())\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(runner)], cwd=str(tmp),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(200):  # ждём READY, не спим вслепую
            line = proc.stdout.readline()
            if not line or "READY" in line:
                break
        body()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _tmp_with_secrets(body_secrets: str = _SECRETS) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="wallet_doctor_"))
    secrets = tmp / "s.toml"
    secrets.write_text(body_secrets, encoding="utf-8")
    os.chmod(secrets, 0o600)
    return tmp


def test_doctor_reports_healthy_env():
    tmp = _tmp_with_secrets()

    def body():
        r = _run_doctor(tmp, {"MY_LLM_KEY": _SHARED_VALUE,
                              "DEPLOY_TOKEN": "<<wallet:deploy>>"})
        assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
        assert "Всё на месте" in r.stdout, r.stdout
        assert _SHARED_VALUE not in r.stdout, "значение shared-секрета напечатано!"
        assert _INJECT_VALUE not in r.stdout, "значение inject-секрета напечатано!"
    _with_daemon(tmp, body)
    print("OK doctor: здоровое окружение → код 0, значения не печатаются")


def test_doctor_detects_clobbered_empty():
    """Случай ikar: `source .env` со строкой `NAME=` затёр значение пустым."""
    tmp = _tmp_with_secrets()

    def body():
        r = _run_doctor(tmp, {"MY_LLM_KEY": "", "DEPLOY_TOKEN": "<<wallet:deploy>>"})
        assert r.returncode == 1, (r.returncode, r.stdout)
        assert "ПУСТО" in r.stdout and "source .env" in r.stdout, r.stdout
        assert _SHARED_VALUE not in r.stdout
    _with_daemon(tmp, body)
    print("OK doctor: пустая переменная → диагноз «затёрли», подсказка про source .env")


def test_doctor_detects_missing_after_policy_change():
    """Секрет добавлен в policy ПОСЛЕ старта — в окружении его нет вовсе."""
    tmp = _tmp_with_secrets()

    def body():
        r = _run_doctor(tmp, {"MY_LLM_KEY": None, "DEPLOY_TOKEN": None})
        assert r.returncode == 1, (r.returncode, r.stdout)
        assert "НЕТ в окружении" in r.stdout and "переоткрыть" in r.stdout, r.stdout
    _with_daemon(tmp, body)
    print("OK doctor: переменной нет → «добавили после старта, переоткрой сессию»")


def test_doctor_detects_overwritten_marker():
    """inject-маркер перезаписан чужим значением — инъекция на хосте не сработает."""
    tmp = _tmp_with_secrets()

    def body():
        r = _run_doctor(tmp, {"MY_LLM_KEY": _SHARED_VALUE, "DEPLOY_TOKEN": "чужое"})
        assert r.returncode == 1, (r.returncode, r.stdout)
        assert "НЕ маркер" in r.stdout, r.stdout
    _with_daemon(tmp, body)
    print("OK doctor: подменённый маркер inject распознан")


def test_doctor_without_env_secrets():
    """Только host-passthrough (без env) — сверять нечего, но не ошибка."""
    tmp = _tmp_with_secrets(
        "[secrets.host]\nsessions = [\"*\"]\ncommands = [\"gh\"]\n")

    def body():
        r = _run_doctor(tmp, {})
        assert r.returncode == 0, (r.returncode, r.stdout)
        assert "сверять нечего" in r.stdout, r.stdout
    _with_daemon(tmp, body)
    print("OK doctor: нет секретов с env → честное «сверять нечего», код 0")


def main() -> None:
    test_doctor_reports_healthy_env()
    test_doctor_detects_clobbered_empty()
    test_doctor_detects_missing_after_policy_change()
    test_doctor_detects_overwritten_marker()
    test_doctor_without_env_secrets()
    print("ALL WALLET-DOCTOR OK")


if __name__ == "__main__":
    main()
