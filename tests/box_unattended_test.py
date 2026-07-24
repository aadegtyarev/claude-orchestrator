"""Unattended-режим claude-box (`-p 'задача'`): deny+log вместо спроса.

§4.6/§5.1 ARCHITECTURE-claude-box.md: в unattended оператора за терминалом нет,
поэтому вопросы кошелька (confirm/ASK) НЕ задаются, а сразу отклоняются с
записью в лог. Ключевое требование Р0 — «никогда не повисать»: модель обязана
получить внятный отказ немедленно, а не ждать таймаут и не зависнуть.

Что покрыто:
  * UnattendedVaultHost.confirm/ask → False БЫСТРО (проверяем wait_for'ом с
    крошечным таймаутом — ожидание ответа его бы завалило) + запись в лог с
    именем секрета и командой;
  * deny_remedy: у unattended-хоста он есть и предписывающий (объясняет модели
    режим и что делать), у attended/tty-хостов — отсутствует (старый текст);
  * ЖИВАЯ цепочка через настоящий VaultDaemon: секрет с confirm=true + вызов
    `/run` под unattended-хостом → 403 за доли секунды, а в теле — тот самый
    предписывающий текст (то, что увидит модель через шим `wallet`).

Запуск: .venv/bin/python tests/box_unattended_test.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402

from box_cli.tty import BoxVaultHost, StdinArbiter, UnattendedVaultHost  # noqa: E402
from vault.daemon import VaultDaemon  # noqa: E402
from vault.host import deny_remedy  # noqa: E402
from vault.store import SecretStore  # noqa: E402
from vault.tty_host import TtyVaultHost  # noqa: E402

SESSION = "claude-box"


class _CaptureLog(logging.Handler):
    """Собрать отформатированные записи лога (аудит отказа проверяем по ним)."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _host_with_log() -> tuple[UnattendedVaultHost, _CaptureLog]:
    log = logging.getLogger("test.unattended")
    log.handlers.clear()
    cap = _CaptureLog()
    log.addHandler(cap)
    log.setLevel(logging.INFO)
    log.propagate = False
    return UnattendedVaultHost(log), cap


async def test_unattended_denies_instantly_and_logs():
    """confirm/ask → False сразу (не ждём никакого таймаута) + строка в лог."""
    host, cap = _host_with_log()
    # wait_for с 0.2с: если бы хост ЖДАЛ ответа/таймаута, тест упал бы по времени.
    ok = await asyncio.wait_for(
        host.confirm(SESSION, "deploy → git push", "git push origin main"), 0.2)
    assert ok is False, "unattended обязан отказывать"
    granted = await asyncio.wait_for(
        host.ask(SESSION, "документ вне скоупа", "GET https://svc/x"), 0.2)
    assert granted is False, "ASK в unattended обязан отказывать"
    joined = "\n".join(cap.lines)
    assert "confirm" in joined and "git push origin main" in joined, (
        f"в логе нет отказа с командой: {cap.lines}")
    assert "ask" in joined and "GET https://svc/x" in joined, (
        f"в логе нет отказа ASK: {cap.lines}")
    print("OK unattended: confirm/ask мгновенно отказывают и пишут в лог")


async def test_unattended_observe_and_audit_still_work():
    """Наблюдаемость/аудит от attended не отличаются — просто идут в лог."""
    host, cap = _host_with_log()
    await host.observe(SESSION, "🔐 <b>wallet</b> <code>git push</code>")
    host.record(SESSION, secret="deploy", cmd="git push", allowed=False)
    await host.notify_denied(SESSION, "deploy → git push")
    joined = "\n".join(cap.lines)
    assert "<b>" not in joined, "HTML-разметка должна сниматься для лога"
    assert "denied" in joined and "ОТКАЗ" in joined, cap.lines
    print("OK unattended: observe/record/notify_denied идут в лог без разметки")


def test_deny_remedy_is_prescriptive_only_for_unattended():
    """Модель должна узнать ПОЧЕМУ отказ; attended-хосты текст не подменяют."""
    text = deny_remedy(UnattendedVaultHost())
    assert text and "-p" in text, f"нет предписывающего текста: {text!r}"
    for word in ("unattended", "policy", "без -p"):
        assert word in text, f"в remedy нет «{word}»: {text!r}"
    # У остальных реализаций расширения нет → прежние формулировки.
    assert deny_remedy(BoxVaultHost(StdinArbiter(0))) is None
    assert deny_remedy(TtyVaultHost()) is None
    assert deny_remedy(None) is None
    print("OK deny_remedy: есть только у unattended-хоста")


def test_deny_remedy_survives_a_broken_host():
    """Сбой хоста НЕ должен превращать честный 403 в 500.

    `deny_remedy` может быть property, и её вычисление способно бросить. Если
    исключение прорастёт, обработчик демона упадёт, и модель вместо
    диагностируемого отказа получит Internal Server Error — ровно то, ради чего
    механизм и вводился (нашло ревью, MEDIUM-1). Тот же приём, что у host.ask()
    в proxy._ask_grant: сбой хоста = просто нет пояснения.
    """
    class Exploding:
        @property
        def deny_remedy(self):
            raise ValueError("хост сломан")

    class WrongType:
        deny_remedy = 42  # не строка — молча игнорируем

    class Blank:
        deny_remedy = "   "  # пробелы = пусто

    assert deny_remedy(Exploding()) is None, "исключение хоста должно гаситься"
    assert deny_remedy(WrongType()) is None
    assert deny_remedy(Blank()) is None
    print("OK deny_remedy: сломанный/кривой хост не роняет отказ")


def _store(tmp: Path) -> SecretStore:
    """Секрет с confirm=true — тот самый случай, который в unattended не пройдёт."""
    f = tmp / "secrets.toml"
    f.write_text(
        '[secrets.deploy]\nvalue="S3CR3T"\nenv="TOK"\nsessions=["*"]\n'
        'commands=["sh -c *"]\nconfirm=true\n'
    )
    os.chmod(f, 0o600)
    return SecretStore(f)


async def test_daemon_denies_confirm_secret_with_remedy():
    """Живая цепочка: демон + unattended-хост → 403 быстро, в теле — remedy."""
    tmp = Path(tempfile.mkdtemp(prefix="box_unattended_"))
    cwd = tmp / "proj"
    cwd.mkdir()
    host, _cap = _host_with_log()
    daemon = VaultDaemon(_store(tmp), host, guard_on=True)
    await daemon.start()
    try:
        token = daemon.issue_token(SESSION, cwd)
        headers = {"Authorization": f"Bearer {token}"}
        async with aiohttp.ClientSession() as http:
            # wait_for страхует главное требование Р0: ответ приходит, а не висит.
            async with await asyncio.wait_for(
                http.post(f"{daemon.url}/run", headers=headers,
                          json={"secret": "deploy", "cmd": ["sh", "-c", "echo hi"]}),
                timeout=5,
            ) as r:
                assert r.status == 403, f"ожидался отказ, получен {r.status}"
                body = await r.json()
        reason = body.get("reason", "")
        assert "unattended" in reason and "-p" in reason, (
            f"модель не узнает причину отказа: {reason!r}")
        assert "S3CR3T" not in reason, "значение секрета не должно светиться"
        print("OK unattended: секрет с confirm=true → мгновенный 403 с remedy")
    finally:
        await daemon.stop()
        for p in sorted(tmp.rglob("*"), reverse=True):
            p.rmdir() if p.is_dir() else p.unlink()
        tmp.rmdir()


def main() -> None:
    asyncio.run(test_unattended_denies_instantly_and_logs())
    asyncio.run(test_unattended_observe_and_audit_still_work())
    test_deny_remedy_is_prescriptive_only_for_unattended()
    test_deny_remedy_survives_a_broken_host()
    asyncio.run(test_daemon_denies_confirm_secret_with_remedy())
    print("ALL BOX-UNATTENDED OK")


if __name__ == "__main__":
    main()
