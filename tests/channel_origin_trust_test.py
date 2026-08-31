"""Канал оператора — не «внешний источник», а выдуманный адрес — не доставка.

Две стороны одной живой поломки (2026-08-31, сессия ikar). Claude Code штампует
каждое сообщение из MCP-канала как недоверенное («This is NOT from your user…»),
а у наших сессий канал — единственный вход оператора: сессия отказалась
работать, потребовала подтверждения «в личном канале 126610519», выдумала этот
id — и наш адаптер всё равно доставил её ответ в привязанный топик, вернув
модели «Reply sent». Петля замкнулась: она считала, что говорит не туда, а
ответы оператора из группы читала как чужие.

Проверяем контракт обоих лекарств:
  • в argv сессии есть --append-system-prompt с блоком про канал оператора, и
    блок называет ИМЕННО её канал (channel-<имя>);
  • блок не отменяет осторожность: подтверждение необратимых действий остаётся;
  • свой context_id (наш чат + топик сессии) принимается, origin доезжает до
    доставки — reply-цитата работает как раньше;
  • выдуманный чат/топик, неизвестная сессия и мусорная строка → ReplyRejected
    с текстом ДЛЯ МОДЕЛИ, доставки нет;
  • адаптер без known_origin (сторонний транспорт) ничего не ломает;
  • reply-сервер отдаёт такой отказ как 422 с телом, а канал-сервер передаёт
    тело модели дословно (isError), а не «HTTP Error 422».

Запуск: .venv/bin/python tests/channel_origin_trust_test.py
"""
from __future__ import annotations

import asyncio
import sys
import urllib.error
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import orchestrator.channel_server as cs  # noqa: E402
from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.errors import ReplyRejected  # noqa: E402
from orchestrator.core.originprompt import channel_trust_prompt  # noqa: E402
from orchestrator.core.transport import Origin  # noqa: E402

SESSION = SimpleNamespace(
    name="ikar", title="ikar", bindings={"telegram": "77"}, auto_allow=False,
)


class _Telegram:
    """Транспорт с проверкой токена — как настоящий Telegram-адаптер."""

    name = "telegram"

    def __init__(self, chat_id: int = -100123):
        self.chat_id = chat_id
        self.delivered: list[tuple[str, Origin | None]] = []

    def known_origin(self, session, token: str) -> bool:
        parts = token.split(":")
        if len(parts) != 3:
            return False
        chat, thread, _msg = parts
        if self.chat_id is None:
            return True
        own = session.bindings.get("telegram")
        if own is None:
            return False
        try:
            if int(chat) != self.chat_id:
                return False
            return int(thread) == int(own)
        except ValueError:
            return False

    async def deliver_text(self, session, text, *, origin=None, intermediate=False):
        self.delivered.append((text, origin))


class _Legacy:
    """Транспорт БЕЗ known_origin: метод в протоколе опционален."""

    name = "legacy"

    def __init__(self):
        self.delivered: list[tuple[str, Origin | None]] = []

    async def deliver_text(self, session, text, *, origin=None, intermediate=False):
        self.delivered.append((text, origin))


class _Mgr:
    def get(self, name):
        return SESSION if name == "ikar" else None

    def touch(self, session):
        pass


def _core(*transports) -> OrchestratorCore:
    core = OrchestratorCore.__new__(OrchestratorCore)
    core.manager = _Mgr()
    core.adapters = {tr.name: tr for tr in transports}
    core.scrubbers = []
    core.journal = SimpleNamespace(record=lambda *a, **kw: None)
    core.tools = SimpleNamespace(forget=lambda name: None)
    core.turns = SimpleNamespace(stop=lambda name: None)
    core.bubbles = SimpleNamespace(
        has=lambda name: False,
        close=lambda name: asyncio.sleep(0),
        freeze_and_open=lambda name: asyncio.sleep(0),
    )
    return core


# ── системный промпт ────────────────────────────────────────────


def test_prompt_names_own_channel():
    text = channel_trust_prompt("ikar")
    assert "channel-ikar" in text, text
    assert "channel-other" not in text
    print("OK блок системного промпта называет канал именно этой сессии")


def test_prompt_keeps_confirmation():
    """Промпт снимает недоверие к ОТПРАВИТЕЛЮ, а не осторожность к действиям.

    Иначе лекарство хуже болезни: сессия перестала бы спрашивать перед
    деплоем/удалением/публикацией, а это ровно то, что оператор подтверждает
    руками.
    """
    text = channel_trust_prompt("ikar").lower()
    assert "confirm" in text, "исчезло требование подтверждать необратимое"
    for word in ("deploying", "deleting", "publishing"):
        assert word in text, f"в промпте не осталось примера «{word}»"
    # И данные остаются данными: файлы/вывод команд/веб — не инструкции.
    assert "file contents" in text and "never instructions" in text, text
    print("OK промпт оставляет подтверждение необратимого и данные — данными")


def test_prompt_forbids_invented_context():
    text = channel_trust_prompt("ikar").lower()
    assert "verbatim" in text, text
    assert "never invent" in text, text
    print("OK промпт прямо запрещает выдумывать адрес ответа")


def test_argv_carries_prompt():
    """Флаг реально уезжает в argv — иначе промпт лежит мёртвым модулем."""
    import inspect

    from orchestrator.core import sessions

    src = inspect.getsource(sessions.SessionManager._start_claude)
    assert "--append-system-prompt" in src, "флаг не собирается в argv"
    assert "channel_trust_prompt(session.name)" in src, (
        "в промпт уходит не имя сессии — блок назовёт чужой канал"
    )
    print("OK --append-system-prompt собирается в argv запуска сессии")


# ── адресат ответа ──────────────────────────────────────────────


async def test_own_context_delivers_with_origin():
    tg = _Telegram()
    core = _core(tg)
    await core.handle_reply({
        "context_id": "telegram:ikar:-100123:77:4242",
        "text": "готово", "complete": True,
    })
    assert len(tg.delivered) == 1, tg.delivered
    text, origin = tg.delivered[0]
    assert text == "готово"
    assert origin is not None and origin.token.endswith(":4242"), origin
    print("OK свой context_id доставляется, origin (цитата) сохраняется")


async def test_invented_chat_rejected():
    """Ровно живой случай: выдуманный «личный канал» вместо привязанной группы."""
    tg = _Telegram()
    core = _core(tg)
    try:
        await core.handle_reply({
            "context_id": "telegram:ikar:126610519:126610519:60",
            "text": "жду подтверждения", "complete": True,
        })
    except ReplyRejected as e:
        assert not tg.delivered, "отклонённый ответ не должен доставляться"
        msg = str(e)
        assert "126610519" in msg, msg
        assert "nothing was delivered" in msg, msg
        print("OK выдуманный чат отклонён, ответ никуда не ушёл")
        return
    raise AssertionError("выдуманный context_id должен подниматься ReplyRejected")


async def test_foreign_thread_rejected():
    tg = _Telegram()
    core = _core(tg)
    try:
        await core.handle_reply({
            "context_id": "telegram:ikar:-100123:999:1",
            "text": "не туда", "complete": True,
        })
    except ReplyRejected:
        assert not tg.delivered
        print("OK чужой топик (наш чат, но не эта сессия) отклонён")
        return
    raise AssertionError("чужой топик должен отклоняться")


async def test_unknown_session_and_garbage_rejected():
    tg = _Telegram()
    core = _core(tg)
    for ctx in ("telegram:нет-такой:-100123:77:1", "мусор", "", "a:b"):
        try:
            await core.handle_reply({"context_id": ctx, "text": "x", "complete": True})
        except ReplyRejected as e:
            assert "nothing was delivered" in str(e), (ctx, str(e))
            continue
        raise AssertionError(f"context_id {ctx!r} должен отклоняться")
    assert not tg.delivered
    print("OK неизвестная сессия и мусорный context_id отклонены")


async def test_unbound_chat_does_not_reject():
    """Холодный старт (нет TELEGRAM_CHAT_ID, ни одного сообщения не было):
    сверять не с чем — отказывать нельзя, иначе законные ответы съедаются."""
    tg = _Telegram(chat_id=None)
    core = _core(tg)
    await core.handle_reply({
        "context_id": "telegram:ikar:-100123:77:5", "text": "ok", "complete": True,
    })
    assert len(tg.delivered) == 1, tg.delivered
    print("OK непривязанный чат не отвергает ответ (отказ только доказанный)")


async def test_unbound_session_in_live_chat_rejected():
    """Чат привязан, а у СЕССИИ топика нет — сверка провалилась, не «нечем».

    Тихая потеря, которую ловит этот тест: known_origin принимал thread=0
    (отсутствие топика читалось как топик №0), handle_reply не бросал, модель
    получала «Reply sent» — а deliver_text выходил на thread_id is None и не
    отправлял ничего. Оператор не видел ответа.
    """
    tg = _Telegram()
    session = SimpleNamespace(
        name="ikar", title="ikar", bindings={}, auto_allow=False,
    )
    core = _core(tg)
    core.manager.get = lambda name: session if name == "ikar" else None
    try:
        await core.handle_reply({
            "context_id": "telegram:ikar:-100123:0:1", "text": "x", "complete": True,
        })
    except ReplyRejected as e:
        assert "nothing was delivered" in str(e), str(e)
    else:
        raise AssertionError("сессия без топика должна отклоняться, а не теряться")
    assert not tg.delivered
    print("OK сессия без топика в живом чате отклонена (нет тихой потери)")


async def test_transport_without_check_still_works():
    """Адаптер без known_origin (протокол не обязывает) работает как раньше."""
    legacy = _Legacy()
    core = _core(legacy)
    await core.handle_reply({
        "context_id": "legacy:ikar:что-угодно", "text": "ok", "complete": True,
    })
    assert len(legacy.delivered) == 1, legacy.delivered
    print("OK транспорт без known_origin не сломан (проверка опциональна)")


async def test_disabled_adapter_delivers_without_origin():
    """Адаптер выключен: сессия найдена — доставляем без адресности, не роняем.

    Живой сценарий — ответ с telegram-контекстом при выключенном Telegram
    (веб-only инстанс): терять ответ оператора из-за этого нельзя.
    """
    legacy = _Legacy()
    core = _core(legacy)
    await core.handle_reply({
        "context_id": "telegram:ikar:-100123:77:1", "text": "ok", "complete": True,
    })
    assert len(legacy.delivered) == 1, legacy.delivered
    assert legacy.delivered[0][1] is None, "цитировать нечем — origin должен быть None"
    print("OK выключенный адаптер: доставка без origin, без отказа")


# ── проводка отказа до модели ───────────────────────────────────


def test_reply_server_maps_rejection_to_422():
    import inspect

    from orchestrator.core import reply_server

    src = inspect.getsource(reply_server._make_route)
    assert "ReplyRejected" in src and "422" in src, (
        "отказ адресата должен ехать 422 с телом, а не 500"
    )
    print("OK reply-сервер отдаёт ReplyRejected как 422 с текстом для модели")


async def test_channel_server_relays_422_body_verbatim():
    """Модель должна увидеть объяснение, а не «HTTP Error 422»."""
    server = cs.ChannelServer()
    written: list = []

    async def fake_write(msg):
        written.append(msg)

    explanation = (
        "Unknown address in context_id 'telegram:ikar:126610519:126610519:60' — "
        "nothing was delivered."
    )

    async def fake_post(url, payload, timeout=30):
        raise urllib.error.HTTPError(
            url, 422, "Unprocessable Entity", {},
            BytesIO(explanation.encode("utf-8")),
        )

    server._write_message = fake_write
    server._post = fake_post
    await server._handle_request(1, "tools/call", {
        "name": "reply_to_user",
        "arguments": {"context_id": "telegram:ikar:126610519:126610519:60",
                      "text": "привет", "complete": True},
    })
    for _ in range(5):
        await asyncio.sleep(0)
    result = written[-1]["result"]
    assert result.get("isError") is True, result
    got = result["content"][0]["text"]
    assert got == explanation, got
    assert "422" not in got, "модели уехал HTTP-код вместо объяснения"
    print("OK канал передаёт текст отказа модели дословно (isError)")


async def test_channel_server_other_errors_still_generic():
    """Не-422 остаётся обычной аварией доставки (регресс на широкий except)."""
    server = cs.ChannelServer()
    written: list = []

    async def fake_write(msg):
        written.append(msg)

    async def fake_post(url, payload, timeout=30):
        raise urllib.error.HTTPError(
            url, 500, "Server Error", {}, BytesIO(b"handler error")
        )

    server._write_message = fake_write
    server._post = fake_post
    await server._handle_request(2, "tools/call", {
        "name": "reply_to_user",
        "arguments": {"context_id": "telegram:ikar:-100123:77:1", "text": "x"},
    })
    for _ in range(5):
        await asyncio.sleep(0)
    result = written[-1]["result"]
    assert result.get("isError") is True, result
    assert result["content"][0]["text"].startswith("Failed:"), result
    print("OK прочие HTTP-ошибки остаются обычным Failed")


async def _main():
    test_prompt_names_own_channel()
    test_prompt_keeps_confirmation()
    test_prompt_forbids_invented_context()
    test_argv_carries_prompt()
    await test_own_context_delivers_with_origin()
    await test_invented_chat_rejected()
    await test_foreign_thread_rejected()
    await test_unknown_session_and_garbage_rejected()
    await test_unbound_chat_does_not_reject()
    await test_unbound_session_in_live_chat_rejected()
    await test_transport_without_check_still_works()
    await test_disabled_adapter_delivers_without_origin()
    test_reply_server_maps_rejection_to_422()
    await test_channel_server_relays_422_body_verbatim()
    await test_channel_server_other_errors_still_generic()
    print("\nВСЕ ТЕСТЫ OK")


if __name__ == "__main__":
    asyncio.run(_main())
