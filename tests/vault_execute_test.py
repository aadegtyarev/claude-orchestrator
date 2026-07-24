"""Исполнение команды под секретом (vault/execute.py) — автономно, без оркестратора.

Проверяет: inject секрета в env, host-passthrough (без инъекции), разворачивание
маркеров inline и :file (файл 0600, чистится), маркер чужого/неизвестного секрета
→ пусто, cwd, коды 127 (не запустилось) и 124 (таймаут). Возвращаются СЫРЫЕ
bytes — редакция отдельно (redact.py).

Запуск: .venv/bin/python tests/vault_execute_test.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vault.execute import run_secret_command  # noqa: E402
from vault.secret import Secret  # noqa: E402


def _secret(**kw) -> Secret:
    base = dict(
        name="s", value="", env="", description="", sessions=("*",),
        commands=(), deny=(), allow_unsafe=False, confirm=False, shared=False,
    )
    base.update(kw)
    return Secret(**base)


def run(coro):
    return asyncio.run(coro)


def test_inject_env():
    s = _secret(name="tok", value="S3CR3T", env="TOK")
    code, out, err = run(run_secret_command(
        ["sh", "-c", "echo v=$TOK"], s,
        cwd=Path("/tmp"), all_secrets={"tok": s}, session_name="dev",
    ))
    assert code == 0 and out == b"v=S3CR3T\n", (code, out, err)
    print("OK inject: секрет в env ребёнка, сырой вывод")


def test_host_passthrough_no_injection():
    s = _secret(name="host")  # без value/env
    code, out, _ = run(run_secret_command(
        ["sh", "-c", "echo v=[$TOK]"], s,
        cwd=Path("/tmp"), all_secrets={"host": s}, session_name="dev",
    ))
    assert code == 0 and out == b"v=[]\n", out
    print("OK host-passthrough: инъекции секрета в env нет")


def test_marker_inline():
    s = _secret(name="tok", value="ABC", env="TOK")
    code, out, _ = run(run_secret_command(
        ["sh", "-c", "printf %s <<wallet:tok>>"], s,
        cwd=Path("/tmp"), all_secrets={"tok": s}, session_name="dev",
    ))
    assert code == 0 and out == b"ABC", out
    print("OK marker inline: <<wallet:имя>> → значение в аргументе")


def test_marker_file():
    s = _secret(name="key", value="PRIVKEY", env="KEY")
    # cat выведет содержимое временного файла, stat — права; файл чистится после.
    code, out, _ = run(run_secret_command(
        ["sh", "-c", "cat <<wallet:key:file>>; stat -c %a <<wallet:key:file>>"], s,
        cwd=Path("/tmp"), all_secrets={"key": s}, session_name="dev",
    ))
    assert code == 0 and out == b"PRIVKEY\n600\n", out
    print("OK marker :file: значение во временном 0600-файле")


def test_marker_disallowed_or_unknown_empty():
    tok = _secret(name="tok", value="V", env="TOK", sessions=("prod-*",))  # НЕ для dev
    code, out, _ = run(run_secret_command(
        ["sh", "-c", "printf [%s] <<wallet:tok>>; printf [%s] <<wallet:nope>>"],
        _secret(name="host"),
        cwd=Path("/tmp"), all_secrets={"tok": tok}, session_name="dev",
    ))
    # tok не разрешён сессии dev → пусто; nope неизвестен → пусто
    assert code == 0 and out == b"[][]", out
    print("OK marker: чужой сессии/неизвестный секрет → пусто (не течём)")


def test_cwd_respected():
    tmp = Path(tempfile.mkdtemp(prefix="vault_exec_"))
    s = _secret(name="host")
    code, out, _ = run(run_secret_command(
        ["sh", "-c", "pwd"], s,
        cwd=tmp, all_secrets={"host": s}, session_name="dev",
    ))
    assert code == 0 and out.decode().strip() == str(tmp), out
    print("OK cwd: команда исполняется в переданном каталоге")


def test_oserror_127():
    s = _secret(name="host")
    code, out, err = run(run_secret_command(
        ["/nonexistent/binary-xyz"], s,
        cwd=Path("/tmp"), all_secrets={"host": s}, session_name="dev",
    ))
    assert code == 127 and out == b"" and err, (code, err)
    print("OK не запустилось → 127 с текстом ошибки в stderr")


def test_timeout_124():
    s = _secret(name="host")
    code, _, _ = run(run_secret_command(
        ["sh", "-c", "sleep 5"], s,
        cwd=Path("/tmp"), all_secrets={"host": s}, session_name="dev",
        timeout=0.3,
    ))
    assert code == 124, code
    print("OK таймаут → 124 (группа убита)")


def main():
    test_inject_env()
    test_host_passthrough_no_injection()
    test_marker_inline()
    test_marker_file()
    test_marker_disallowed_or_unknown_empty()
    test_cwd_respected()
    test_oserror_127()
    test_timeout_124()
    print("ALL VAULT-EXECUTE OK")


if __name__ == "__main__":
    main()
