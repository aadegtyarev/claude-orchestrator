"""Юнит-контракт core/hookscript.render — рендер хук-диспетчера Claude Code.

Важно для agent-vm: адрес оркестратора в хук-скрипте должен быть параметризован
(host-gateway гостя), а не хардкод 127.0.0.1 — иначе PreToolUse/Stop-хуки из
гостя VM не достучатся до хоста. Под bwrap/off host=127.0.0.1 → как раньше.

Вторая половина — PreCompact: его stdout Claude Code дописывает в промпт
суммаризатора, и это единственное место, где можно запретить записывать
«канал — не мой пользователь» как установленный факт (см. originprompt.py).
Тесты гоняют НАСТОЯЩИЙ отрендеренный скрипт подпроцессом: важен не текст
шаблона, а то, что доезжает до stdout — и что при недоступном оркестраторе
инструкция всё равно печатается, а сжатие не блокируется.

Запуск: .venv/bin/python tests/hookscript_test.py
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import hookscript  # noqa: E402
from orchestrator.core.originprompt import compact_trust_instruction  # noqa: E402


def _leftover(src: str) -> list[str]:
    """Неподставленные плейсхолдеры — рендер обязан не оставлять ни одного."""
    return re.findall(r"__[A-Z_]+__", src)


def _run(event: dict, *, host: str = "127.0.0.1", port: int = 1,
         name: str = "ikar", token: str = "tok") -> subprocess.CompletedProcess:
    """Отрендерить скрипт и скормить ему событие на stdin — как делает Claude.

    Порт по умолчанию заведомо мёртвый: проверяем ровно то, что инструкция не
    зависит от доступности оркестратора."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "hook_dispatch.py"
        script.write_text(hookscript.render(host, port, name, token))
        return subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(event), capture_output=True, text=True, timeout=30,
        )


def test_local_host_substitution():
    """host=127.0.0.1 (bwrap/off) → URL как раньше, все плейсхолдеры подставлены."""
    out = hookscript.render("127.0.0.1", 18080, "noos", "tok-abc")
    assert '_ORCH = "http://127.0.0.1:18080"' in out
    assert '_NAME = "noos"' in out
    assert '_TOKEN = "tok-abc"' in out
    assert not _leftover(out), _leftover(out)
    print("OK render: host=127.0.0.1 — прежний URL, плейсхолдеры подставлены")


def test_gateway_host_for_agentvm():
    """host=host-gateway IP (agent-vm) → адрес оркестратора указывает на хост."""
    out = hookscript.render("10.0.2.2", 18080, "noos", "t")
    assert '_ORCH = "http://10.0.2.2:18080"' in out
    assert "127.0.0.1" not in out  # хардкода больше нет
    assert not _leftover(out), _leftover(out)  # ни одного плейсхолдера
    print("OK render: host-gateway IP — хуки бьют на хост, не на loopback гостя")


def test_special_chars_in_token():
    """Токен подставляется НЕ через .format — '{' в нём не ломает рендер."""
    out = hookscript.render("127.0.0.1", 1, "s", "a{b}c-$%")
    assert '_TOKEN = "a{b}c-$%"' in out
    print("OK render: спецсимволы в токене не ломают подстановку")


def test_token_with_quotes_stays_data():
    """Кавычка/обратный слэш/перевод строки в ORCH_TOKEN (его можно задать
    руками в .env) обязаны остаться ДАННЫМИ, а не синтаксисом: раньше голая
    подстановка давала SyntaxError, и хук молча не работал вовсе."""
    token = 'a"b\\c\nd'
    src = hookscript.render("127.0.0.1", 1, "s", token)
    compile(src, "hook_dispatch.py", "exec")
    ns: dict = {}
    exec(compile("\n".join(  # вытаскиваем только константы, main() не зовём
        ln for ln in src.splitlines() if ln.startswith("_")), "c", "exec"), ns)
    assert ns["_TOKEN"] == token, ns["_TOKEN"]
    print("OK render: кавычки/слэши в токене остаются данными")


def test_placeholder_lookalike_name_leaks_nothing():
    """Имя сессии, похожее на плейсхолдер, НЕ должно вытянуть токен в текст.

    Живая дыра, найденная ревью: имя доезжает внутрь PreCompact-инструкции
    (channel-<имя>), а цепочка .replace() шла по уже собранной строке — сессия
    `/new __TOKEN__` печатала бы боевой ORCH_TOKEN в промпт суммаризатора, то
    есть в контекст модели и в саммари. Одним проходом re.sub этого нет."""
    secret = "REALSECRET"
    for name in ("__TOKEN__", "x__TOKEN__y", "__ORCH__", "__COMPACT__", "__NAME__"):
        out = hookscript.render("127.0.0.1", 1, name, secret)
        compile(out, "hook_dispatch.py", "exec")
        compact = out.split("_COMPACT = ")[1].splitlines()[0]
        assert secret not in compact, (name, "секрет уехал в текст инструкции")
        assert name in compact, (name, "имя сессии потерялось при подстановке")
        assert out.count(secret) == 1, (name, "токен размножился по скрипту")
    print("OK render: имя-двойник плейсхолдера не вытягивает токен в инструкцию")


# ── PreCompact: инструкция суммаризатору ───────────────────────


def test_precompact_prints_instruction():
    """Живой прогон: stdout хука = инструкция, код 0 (иначе сжатие блокируется)."""
    r = _run({"hook_event_name": "PreCompact", "trigger": "auto",
              "custom_instructions": None})
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert r.stdout.strip() == compact_trust_instruction("ikar").strip(), r.stdout
    assert "channel-ikar" in r.stdout, "инструкция называет чужой канал"
    print("OK PreCompact: инструкция уехала в stdout, код 0")


def test_precompact_manual_trigger_too():
    """matcher='' ловит оба триггера — ручной /compact ничем не хуже авто."""
    r = _run({"hook_event_name": "PreCompact", "trigger": "manual",
              "custom_instructions": "сохрани план"})
    assert r.returncode == 0 and "channel-ikar" in r.stdout, (r.returncode, r.stdout)
    print("OK PreCompact: ручной триггер даёт ту же инструкцию")


def test_precompact_forbids_the_refusal_facts():
    """Инструкция должна запрещать ровно то, что зациклило живую сессию.

    Петля (ikar, 2026-08-31): отказ «это не от пользователя» попал в саммари как
    факт, следующий отрезок прочитал его как свою историю и повторил. Без этих
    формулировок хук печатает вежливый шум."""
    text = compact_trust_instruction("ikar").lower()
    assert "must therefore not record" in text, text
    assert "third-party, external or untrusted content" in text, text
    assert "the operator is not the user" in text, text
    assert "personal channel" in text, text
    assert "invented" in text, text
    assert "should stay declined" in text, text
    print("OK инструкция запрещает записывать отказ как факт")


def test_precompact_keeps_care():
    """Лекарство не должно снимать осторожность: данные — данными,
    необратимое — с подтверждением. Иначе хук развязывает руки на сжатии."""
    text = compact_trust_instruction("ikar").lower()
    assert "nothing else is relaxed" in text, text
    assert "never instructions" in text, text
    assert "confirmed" in text and "irreversible" in text, text
    print("OK инструкция не отменяет осторожность (данные, подтверждение)")


def test_other_events_print_nothing():
    """Не-PreCompact печатать не должен: stdout Stop/PreToolUse Claude не ждёт,
    а лишний вывод — это лишний текст в чужих местах."""
    for event in ("Stop", "PreToolUse", "PostToolUse", "SubagentStop", "PostCompact"):
        r = _run({"hook_event_name": event})
        assert r.returncode == 0, (event, r.returncode, r.stderr)
        assert r.stdout == "", (event, r.stdout)
    print("OK прочие события молчат в stdout (и не падают на мёртвом порту)")


def test_broken_stdin_survives():
    """Мусор на stdin: код 0 и пустой stdout — хук не роняет и не сжатие."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "h.py"
        script.write_text(hookscript.render("127.0.0.1", 1, "ikar", "t"))
        r = subprocess.run([sys.executable, str(script)], input="не json",
                           capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == "", (r.returncode, r.stdout)
    print("OK мусорный stdin: код 0, ничего не напечатано")


def test_instruction_survives_quotes_and_newlines():
    """Текст уезжает в скрипт через json.dumps: кавычки и переводы строк —
    литерал, а не синтаксическая ошибка в хуке."""
    src = hookscript.render("127.0.0.1", 1, "ikar", "t")
    compile(src, "hook_dispatch.py", "exec")  # рендер обязан быть валидным python
    assert not _leftover(src), _leftover(src)
    print("OK инструкция вшита валидным литералом (кавычки/переводы строк целы)")


def test_precompact_registered_in_settings():
    """Хук бесполезен, если не зарегистрирован — и обязан быть ВНЕ
    show_tool_calls: он не про баблы тул-вызовов."""
    import inspect

    from orchestrator.core import sessions

    src = inspect.getsource(sessions.SessionManager._write_claude_settings)
    assert '"PreCompact"' in src, "PreCompact не регистрируется в settings"
    head, _, tail = src.partition("if self.config.show_tool_calls:")
    assert '"PreCompact"' in head, "PreCompact спрятан за show_tool_calls"
    print("OK PreCompact зарегистрирован безусловно (не за show_tool_calls)")


def main():
    test_local_host_substitution()
    test_gateway_host_for_agentvm()
    test_special_chars_in_token()
    test_token_with_quotes_stays_data()
    test_placeholder_lookalike_name_leaks_nothing()
    test_precompact_prints_instruction()
    test_precompact_manual_trigger_too()
    test_precompact_forbids_the_refusal_facts()
    test_precompact_keeps_care()
    test_other_events_print_nothing()
    test_broken_stdin_survives()
    test_instruction_survives_quotes_and_newlines()
    test_precompact_registered_in_settings()
    print("ALL HOOKSCRIPT OK")


if __name__ == "__main__":
    main()
