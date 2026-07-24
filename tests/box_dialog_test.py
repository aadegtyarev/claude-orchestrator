"""_pty_driver диалоги: маркеры авто-ответа матчат ДИАЛОГИ, но НЕ постоянную
строку статуса и НЕ обычный текст беседы.

Два регресса, которые закрывают эти тесты:
  1. маркер «bypasspermissions» ложно совпадал с плашкой «⏵⏵ bypass permissions
     on» и слал «2» как сообщение (замечено под agent-vm);
  2. матчер работал ВСЮ жизнь сессии по скользящему окну вывода, поэтому текст
     самой беседы («yes, I accept») впечатывал «2\r» в stdin Claude — цифра
     уходила спурьёзным сообщением («your message was just 2»). Все маркеры —
     СТАРТОВЫЕ диалоги, поэтому матчер живёт только стартовое окно.

Матчер переехал в автономный пакет box/ (box.dialog, Слой 2 редизайна) —
импортим из источника; отдельным тестом проверяем, что sessions.py его
реэкспортит (обратная совместимость).

Запуск: .venv/bin/python tests/box_dialog_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box.dialog import _DIALOGS, _DialogAnswerer  # noqa: E402


def _screen(text: str) -> str:
    """Как _pty_driver готовит экран для матча: убрать пробелы, lower."""
    return text.replace(" ", "").lower()


# Постоянная UI-плашка внизу экрана Claude Code (НЕ диалог).
STATUS_BAR = _screen(
    "⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents"
)

# Реальные стартовые диалоги (упрощённо, важен текст пункта).
DIALOG_BYPASS = _screen(
    "Bypass Permissions mode\nBy proceeding you accept...\n"
    "❯ 1. No, exit\n  2. Yes, I accept\nEnter to confirm"
)
DIALOG_TRUST = _screen("Do you trust the files in this folder?\n1. Yes, I trust this folder")
# agent-vm: managed-settings гостя. Enter = дефолтный (первый) пункт.
DIALOG_MANAGED = _screen(
    "Managed settings require approval\n❯ 1. Continue\n  2. Exit\nEnter to confirm"
)


def test_no_marker_matches_status_bar():
    """НИ один маркер авто-ответа не срабатывает на строке статуса (иначе спам «2»)."""
    hit = [m for m, _ in _DIALOGS if m in STATUS_BAR]
    assert hit == [], f"маркеры ложно матчат статус-бар: {hit}"
    print("OK ни один маркер не матчит строку статуса «bypass permissions on»")


def test_bypass_marker_matches_dialog():
    """bypass-диалог (пункт «Yes, I accept») ловится и отвечает «2»."""
    matched = [(m, keys) for m, keys in _DIALOGS if m in DIALOG_BYPASS]
    assert any(keys == b"2\r" for _, keys in matched), matched
    print("OK bypass-диалог «Yes, I accept» → ответ «2»")


def test_trust_dialog_matches():
    assert any(m in DIALOG_TRUST for m, _ in _DIALOGS)
    print("OK trust-диалог матчится")


def test_managed_settings_matches_with_enter():
    """managed-settings диалог ловится и отвечает Enter (дефолтный пункт)."""
    matched = [(m, keys) for m, keys in _DIALOGS if m in DIALOG_MANAGED]
    assert matched, "managed-settings маркер не сматчил диалог"
    assert all(keys == b"\r" for _, keys in matched), matched
    print("OK managed-settings диалог → Enter")


# ── _DialogAnswerer: работает только до готовности сессии ─────────────────


def test_answerer_replies_before_ready():
    a = _DialogAnswerer()
    assert a.feed(DIALOG_BYPASS.encode()) == [b"2\r"]
    print("OK до готовности сессии диалог получает ответ")


def test_answerer_answers_each_marker_once():
    """Повторный показ того же диалога не шлёт клавиши второй раз."""
    a = _DialogAnswerer()
    assert a.feed(DIALOG_BYPASS.encode()) == [b"2\r"]
    assert a.feed(DIALOG_BYPASS.encode()) == []
    print("OK один маркер — один ответ")


def test_answerer_silent_after_stop():
    """После stop() (канал ответил на /ping) текст беседы НЕ пишется в stdin.

    Это и есть баг «your message was just 2»: модель пишет в чат «yes, I
    accept», матчер видит маркер и шлёт «2\\r» в сессию.
    """
    a = _DialogAnswerer()
    a.stop()
    assert a.feed(DIALOG_BYPASS.encode()) == []
    assert a.feed(_screen("sure, yes, I accept that plan").encode()) == []
    print("OK после stop() клавиши в stdin не пишутся")


def test_answerer_no_deadline_survives_slow_boot():
    """Матчер НЕ выключается по времени: под agent-vm первая загрузка образа
    занимает минуты, и диалог приходит сильно позже старта процесса."""
    a = _DialogAnswerer()
    a.feed(b"pulling image layer 21/22 ...")
    assert a.active, "матчер не должен гаснуть сам по себе"
    assert a.feed(DIALOG_TRUST.encode()) == [b"\r"], "диалог после долгого старта"
    print("OK долгий старт (загрузка образа) не оставляет диалог без ответа")


def test_answerer_multiple_dialogs_in_one_chunk():
    """Несколько диалогов, склеенных в один чанк, отвечаются все.

    Под agent-vm вывод идёт через проброшенный PTY, и перерисовки экрана
    реалистично склеиваются в один os.read() — раньше отвечался только
    первый маркер, буфер обнулялся, и сессия вешалась на втором диалоге.
    """
    a = _DialogAnswerer()
    keys = a.feed((DIALOG_TRUST + DIALOG_MANAGED).encode())
    assert len(keys) == 2, keys
    print("OK два диалога в одном чанке — два ответа")


def test_answerer_stops_when_all_answered():
    """Когда все диалоги отвечены, матчер выключается досрочно (не жжёт CPU)."""
    a = _DialogAnswerer()
    a.feed(
        (
            DIALOG_TRUST
            + DIALOG_BYPASS
            + DIALOG_MANAGED
            + _screen("I am using this for local development")
        ).encode()
    )
    assert not a.active, "матчер должен выключиться после всех диалогов"
    print("OK матчер выключается, когда все диалоги отвечены")


def test_answerer_strips_ansi_and_spaces():
    """Матч идёт по экрану без ANSI и пробелов (реальный вывод раскрашен)."""
    a = _DialogAnswerer()
    raw = b"\x1b[1m\x1b[32m  2. Yes,   I accept\x1b[0m\r\n"
    assert a.feed(raw) == [b"2\r"]
    print("OK ANSI и пробелы не мешают матчу")


def test_answerer_marker_split_across_chunks():
    """Маркер, разорванный между чанками, всё равно матчится (скользящее окно)."""
    a = _DialogAnswerer()
    assert a.feed(b"  2. Yes, I ac") == []
    assert a.feed(b"cept\r\n") == [b"2\r"]
    print("OK маркер на границе чанков матчится")


def test_sessions_reexports_dialog_symbols():
    """sessions.py реэкспортит _DialogAnswerer/_DIALOGS из box — старый код и
    тесты, ссылающиеся на sessions._DialogAnswerer, не сломаны."""
    from orchestrator.core import sessions

    assert sessions._DialogAnswerer is _DialogAnswerer
    assert sessions._DIALOGS is _DIALOGS
    print("OK sessions.py реэкспортит _DialogAnswerer/_DIALOGS из box.dialog")


def main():
    test_no_marker_matches_status_bar()
    test_bypass_marker_matches_dialog()
    test_trust_dialog_matches()
    test_managed_settings_matches_with_enter()
    test_answerer_replies_before_ready()
    test_answerer_answers_each_marker_once()
    test_answerer_silent_after_stop()
    test_answerer_no_deadline_survives_slow_boot()
    test_answerer_multiple_dialogs_in_one_chunk()
    test_answerer_stops_when_all_answered()
    test_answerer_strips_ansi_and_spaces()
    test_answerer_marker_split_across_chunks()
    test_sessions_reexports_dialog_symbols()
    print("ALL BOX-DIALOG OK")


if __name__ == "__main__":
    main()
