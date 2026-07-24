"""vault — host-side CLI автономного кошелька: запуск демона БЕЗ оркестратора и
управление policy. Зовётся `python -m vault` или `bin/vault`.

  vault serve   — поднять демон секретов standalone (TtyVaultHost) и записать
                  ~/.wallet.json (url+token) — дальше клиент `wallet` (bin/wallet)
                  работает против него без всякого оркестратора.
  vault policy  — просмотр/правка secrets.toml (PolicyEditor): new/cmd/sessions/
                  deny/confirm/scope/rm. То же, что `/wallet` в боте, но из
                  терминала (в т.ч. отзыв ASK-гранта: `policy scope <имя> -<url>`).

Клиентские команды (ls/run/exec/get/env) остаются в stdlib-only `bin/wallet` —
он работает и внутри песочницы, где vault-пакет/venv недоступны.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from .daemon import VaultDaemon
from .host import VaultHost
from .policy import PolicyEditor, PolicyError
from .store import SecretStore
from .tty_host import TtyVaultHost

logger = logging.getLogger("vault")

DEFAULT_SECRETS = "~/.config/claude-orchestrator/secrets.toml"
DEFAULT_WALLET = "~/.wallet.json"


def _secrets_path(raw: str) -> Path:
    return Path(os.path.expanduser(raw))


def build_daemon(
    secrets_path: Path, *, guard_on: bool = True, assume_yes: bool = False,
    host: VaultHost | None = None,
) -> VaultDaemon:
    """Собрать standalone-демон: store из secrets.toml + host. Без оркестратора.
    Короткий shutdown_timeout — чтобы SIGINT не ждал зависший confirm-хендлер.

    host по умолчанию — TtyVaultHost (`vault serve`: stdin ничей, читаем его сами).
    Вызывающий, который УЖЕ владеет stdin (claude-box держит на нём PTY-relay),
    обязан передать свой хост: два независимых add_reader на один fd затирают друг
    друга, и после первого же confirm ввод в сессию умирает."""
    return VaultDaemon(SecretStore(secrets_path),
                       host or TtyVaultHost(assume_yes=assume_yes),
                       guard_on=guard_on, shutdown_timeout=2.0)


def write_wallet(path: Path, url: str, token: str, session: str) -> None:
    """Записать ~/.wallet.json (0600) с url+token — как это делает провижн
    оркестратора, чтобы клиент `wallet` нашёл демон по ~/.wallet.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"url": url, "token": token, "session": session}, f)
    os.chmod(path, 0o600)


async def _serve(args: argparse.Namespace) -> int:
    daemon = build_daemon(
        _secrets_path(args.secrets), guard_on=not args.no_guard, assume_yes=args.yes)
    await daemon.start()
    cwd = Path(args.cwd).resolve()
    token = daemon.issue_token(args.session, cwd)
    wallet = _secrets_path(args.wallet_file)
    write_wallet(wallet, daemon.url, token, args.session)
    logger.info("vault готов: %s | %s | сессия=%s cwd=%s",
                daemon.url, wallet, args.session, cwd)
    logger.info("клиент: WALLET_FILE=%s wallet ls   (или просто `wallet ls`, "
                "если это ~/.wallet.json)", wallet)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await daemon.stop()
        logger.info("vault остановлен")
    return 0


def cmd_policy(args: argparse.Namespace) -> int:
    """Просмотр/правка policy — делегат в PolicyEditor (тот же, что у бота)."""
    editor = PolicyEditor(_secrets_path(args.secrets))
    try:
        print(editor.apply(args.policy_args, allow_edit=True))
        return 0
    except PolicyError as e:
        print(f"⚠️ {e}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vault", description="автономный кошелёк секретов")
    # --secrets перед сабкомандой: `vault --secrets X policy` (на сабпарсерах не
    # дублируем — там дефолт затирал бы значение родителя).
    p.add_argument("--secrets", default=DEFAULT_SECRETS,
                   help=f"путь к secrets.toml (по умолчанию {DEFAULT_SECRETS})")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="поднять демон standalone (без оркестратора)")
    s.add_argument("--cwd", default=".", help="рабочий каталог для команд под секретом")
    s.add_argument("--session", default="local", help="имя сессии (для policy sessions)")
    s.add_argument("--wallet-file", default=DEFAULT_WALLET,
                   help=f"куда записать url+token (по умолчанию {DEFAULT_WALLET})")
    s.add_argument("--yes", action="store_true", help="подтверждать всё без tty-вопроса")
    s.add_argument("--no-guard", action="store_true", help="выключить жёсткий guard")

    pol = sub.add_parser("policy", help="просмотр/правка secrets.toml")
    pol.add_argument("policy_args", nargs=argparse.REMAINDER,
                     help="аргументы (пусто = просмотр; new/cmd/sessions/deny/confirm/rm)")

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return asyncio.run(_serve(args))
    if args.command == "policy":
        return cmd_policy(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
