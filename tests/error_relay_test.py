"""Регрессия ретранслятора ошибок API: строгий баннер + классификация, без ложных алертов.

Раньше ретранслятор грепел claude.log по словам «rate-limit»/«api error» и ловил
собственный текст Клода — диагностику чужой сессии («9× ratelimit»), описание
самой фичи («ретрансляция ошибок API/rate-limit») — как ложный алерт о лимите.
Теперь триггер — только настоящий баннер TUI «API Error: <код> <детали>»,
класс определяет текст подсказки (rate-limit→/model, 400-протокол→/clear).

Запуск: .venv/bin/python tests/error_relay_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.logsignals import (  # noqa: E402
    API_ERR_BANNER_RE as _API_ERR_BANNER_RE,
    classify_api_error as _classify_api_error,
    detect_log_signals as _detect_log_signals,
)


def _hit(chunk: str) -> tuple[str, str] | None:
    """Сымитировать шаг ретранслятора: (code, klass) либо None (нет баннера)."""
    m = _API_ERR_BANNER_RE.search(chunk.encode())
    if not m:
        return None
    return m.group(1).decode(), _classify_api_error(m.group(1), m.group(2))


def main():
    # ── Реальные баннеры из TUI классифицируются верно ──
    assert _hit("●API Error: 400 messages.7.content.2.server_tool_use.id: "
                "String should match pattern '^srvtoolu_[a-zA-Z0-9_]+$'") == ("400", "protocol")
    assert _hit("●API Error: 400 messages.5: `tool_result` blocks can only be "
                "in `user` messages") == ("400", "protocol")
    # thinking-400: чужой (неподписанный) thinking в истории — корень cross-provider
    # мусора; теперь тоже классифицируется как protocol (→подсказка /clear + эксцепт).
    assert _hit("●API Error: 400 messages.3.content.1.thinking: must be passed back "
                "to the API") == ("400", "protocol")
    assert _hit("●API Error: 429 {\"type\":\"rate_limit_error\"}") == ("429", "ratelimit")
    assert _hit("●API Error: 529 {\"type\":\"overloaded_error\"}") == ("529", "ratelimit")
    assert _hit("●API Error: 503 service unavailable") == ("503", "generic")
    print("OK classify: 400+tool/thinking→protocol, 429/529→ratelimit, 5xx→generic")

    # ── Главный регресс: проза модели НЕ триггерит (никакого «API Error: <код>») ──
    # Реальные строки из claude.log сессии tg-claude-orchestrator:
    assert _hit("Результат расследования: Журнал сессии noos содержит 13×«APIerror»,"
                " 9×«ratelimit», 7×«exitcode»") is None
    assert _hit("Ретрансляция ошибок API/rate-limit в чат с подсказкой /model") is None
    assert _hit("Скажи, бывает ли api error 429 у Anthropic?") is None
    assert _hit("rate-limit relay, overloaded, 429, internal server error — "
                "просто перечисление слов без баннера") is None
    print("OK no false positives on model prose (был ложный алерт о лимите)")

    # ── Дедуп по сигнатуре code:class: корень один — сигнатура одна ──
    sigs = set()
    for c in ["API Error: 400 messages.7.content.2.server_tool_use.id: x",
              "API Error: 400 messages.9.server_tool_use.id: y",
              "API Error: 429 rate_limit_error"]:
        code, klass = _hit(c)
        sigs.add(f"{code}:{klass}")
    assert sigs == {"400:protocol", "429:ratelimit"}, sigs
    print("OK signatures: корень 400-protocol схлопывается в одну сигнатуру")

    # ── _detect_log_signals: разбор трёх классов сигналов из куска лога ──
    none = _detect_log_signals("●Думаю над задачей… ✻Crunched for 12s".encode())
    assert none["api_error"] is None and none["retry"] is None and none["restarts"] == 0, none
    print("OK detect: чистый лог → никаких сигналов")

    # retry: «Retrying in 1s · attempt 47/100» (живой прогресс ретраев)
    r = _detect_log_signals("API Error: 400 … ✻Retrying in 1s · attempt 47/100".encode())
    assert r["retry"] == (47, 100), r["retry"]
    assert r["api_error"] is not None  # в том же куске виден и баннер ошибки
    print("OK detect: retry attempt K/M + сопутствующая API-ошибка")

    # restart-loop: баннер «Resume this session» mid-хода = краш-рестарт
    rs = _detect_log_signals("Resume this session with:\nclaude --resume x\n…\nResume this session with:\nclaude --resume y".encode())
    assert rs["restarts"] == 2, rs["restarts"]
    print("OK detect: рестарт-баннеры считаются")

    # живой пульс: глагол + время + токены (всё из TUI-спиннера)
    s = _detect_log_signals("✻ Cogitating… (12s · ↓ 340 tokens)".encode())
    assert s["pulse"] == "Cogitating · 12s" and s["tokens"] == "340", (s["pulse"], s["tokens"])
    # токены с суффиксом «k» (реальный формат при >1000, прежде терялись целиком)
    k = _detect_log_signals("✻ Deciphering… (1m 11s · ↓2.6k tokens)".encode())
    assert k["tokens"] == "2.6k" and k["pulse"] == "Deciphering · 1m 11s", (k["tokens"], k["pulse"])
    # свежее время из блока статистики тикает, даже если строка глагола перебита
    # анимацией спиннера (реальный лог: «Deciphering…94✶13*…» + отдельный блок)
    g = _detect_log_signals("Deciphering…94✶13*✢·Deciphering… (33s · ↓ 683 tokens)".encode())
    assert g["pulse"] == "Deciphering · 33s" and g["tokens"] == "683", (g["pulse"], g["tokens"])
    assert _detect_log_signals("✻ Cogitated for 5m 57s".encode())["pulse"] == "Cogitated · 5m 57s"
    # активный побеждает завершённый в том же куске
    assert _detect_log_signals("Brewed for 1m 31s … Churning…".encode())["pulse"] == "Churning"
    # нет спиннера — None (не ложим прозу)
    assert _detect_log_signals("просто текст ответа модели".encode())["pulse"] is None
    # компакция контекста — явный пульс (тоже «тупняк», ввод заблокирован)
    assert _detect_log_signals("✻ Compacting conversation…".encode())["pulse"] == "Сжимаю контекст"
    # компакция ПОСЛЕ завершённого глагола в том же куске — компакция текущая
    assert _detect_log_signals(
        "Cogitated for 3s … Compacting conversation…".encode()
    )["pulse"] == "Сжимаю контекст"
    # активный глагол (модель уже думает после сжатия) побеждает компакцию
    assert _detect_log_signals(
        "Compacting conversation… ✻ Thundering…".encode()
    )["pulse"] == "Thundering"
    print("OK detect: пульс (глагол · время · токены; compact; active > compact > done)")

    # quota-баннер БЕЗ 3-значного кода (Weekly/Monthly exhausted) — реальный noos
    q = _detect_log_signals(
        ("API Error: Server is temporarily limiting requests (not your usage limit) "
         "· [1310][Weekly/Monthly Limit Exhausted. Your limit will reset at "
         "2026-07-23 07:17:46]").encode()
    )
    assert q["quota"] == "2026-07-23 07:17:46", q["quota"]
    assert q["api_error"] is None  # 3-значного кода тут нет
    # обычная 429 не попадает в quota, остаётся в api_error
    q2 = _detect_log_signals(b"API Error: 429 rate limit exceeded")
    assert q2["quota"] is None and q2["api_error"][1] == "ratelimit"
    print("OK detect: лимит-баннер (quota+дата сброса), 429 не ложно-quota")

    # ── баннер протухшей учётки (живой лог noos, 2026-08-17) ──
    # TUI рисует строку позиционированием курсора: после снятия ANSI пробелы
    # между словами теряются как попало — матчим «экран без пробелов».
    for raw in (
        "●Loginexpired·Pleaserun/login✻Churned for 0s",
        "●Login expired · Please run /login",
        "❯ ← for agents  99  Not logged in · Run /login",
        "●Invalid API key · Please run /login",
    ):
        assert _detect_log_signals(raw.encode())["login"], raw
    # Проза про эту самую фичу баннером не считается: нет «·» + «/login» подряд.
    for raw in (
        "Сессия отвечала «Login expired», это надо было ловить",
        "если учётка протухла — оператор запускает /login руками",
        "API Error: 429 rate limit exceeded",
    ):
        assert not _detect_log_signals(raw.encode())["login"], raw
    print("OK detect: баннер учётки (со слипшимися пробелами), проза не ловится")

    print("ALL ERROR-RELAY OK")


def test_error_relay():
    main()

if __name__ == "__main__":
    main()
