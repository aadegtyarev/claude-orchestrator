"""Исполнение команды под секретом НА ХОСТЕ (вне песочницы): инъекция секрета в
env, разворачивание маркеров <<wallet:имя>> в аргументах, запуск подпроцесса с
таймаутом/killpg. Без зависимостей оркестратора.

Суть дизайна: секрет/auth живёт только на хосте, никогда — в адресном
пространстве песочницы. Возвращает СЫРЫЕ bytes (stdout/stderr) — редакция
значений (redact.py) делается вызывающим над полным набором секретов.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
from pathlib import Path

from .secret import MARKER_RE, Secret

# Таймаут выполнения самой команды под секретом. Бюджет CLI-обёртки
# (bin/wallet HTTP_TIMEOUT=660с) должен покрывать confirm (request_confirmation
# по умолчанию 300с) + RUN_TIMEOUT + накладные: 300+300 < 660. Меняешь одно —
# держи неравенство, иначе CLI отвалится по таймауту, пока команда ещё бежит.
RUN_TIMEOUT = 300.0


async def run_secret_command(
    cmd: list[str],
    secret: Secret,
    *,
    cwd: Path,
    all_secrets: dict[str, Secret],
    session_name: str,
    timeout: float = RUN_TIMEOUT,
) -> tuple[int, bytes, bytes]:
    """Запустить команду НА ХОСТЕ (вне песочницы) под секретом. Возвращает
    (код возврата, stdout-bytes, stderr-bytes). Коды: 127 — не запустилось
    (OSError), 124 — таймаут (группа убита SIGKILL).

    Два режима (см. Secret):
      * inject — секрет в env ребёнка (env=…, value=…);
      * host-passthrough — чистое хостовое окружение (keyring, gh/git auth):
        ничего не инжектим, глобальный git-конфиг НЕ обнуляем (в нём живёт
        gh credential helper).

    ⚠️ ОГРАНИЧЕНИЕ (docs/secrets-wallet.md): команда исполняется в cwd проекта,
    куда модель пишет из песочницы. Узкий шаблон НЕ гарантирует безопасность —
    модель может подложить `./.git/config` и получить исполнение на хосте.
    Барьер — policy (sessions/commands/confirm).
    """
    env = dict(os.environ)
    # Строго НЕинтерактивно: команда бежит без TTY (демон). Любой интерактив
    # (ssh host-verify/пароль, git credential-промпт) иначе всплывает
    # GUI-диалогом askpass (Ksshaskpass) на десктопе хоста — висит невидимо
    # для модели. Глушим GUI: пусть команда падает с понятной ошибкой в
    # stderr (модель увидит и починит/скажет оператору), а не подвешивает.
    env["SSH_ASKPASS_REQUIRE"] = "never"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("DISPLAY", None)
    env.pop("SSH_ASKPASS", None)

    tmpdir: str | None = None
    # Основной секрет-inject → реальное значение в env (env-читающие
    # инструменты, gh->GH_TOKEN, получают его на хосте).
    if not secret.host_passthrough:
        env[secret.env] = secret.value
        # Частичная защита git от подложенного локального конфига (inject).
        env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
        env.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    # Развернуть маркеры <<wallet:имя>> / <<wallet:имя:file>> в аргументах в
    # РЕАЛЬНОЕ значение на хосте (curl-заголовок, ssh-ключ): модель писала
    # $ENV → шелл развернул в маркер → тут подставляем значение. Файл — во
    # временный 0600 на хосте (песочнице невидим, tmpfs /tmp), сносится в
    # finally. Маркер неизвестного/недоступного секрета → пусто (не течём).

    def _sub(arg: str) -> str:
        nonlocal tmpdir

        def repl(m) -> str:  # m: re.Match от MARKER_RE.sub
            nonlocal tmpdir
            s = all_secrets.get(m.group(1))
            # proxy-секрет (connector) НЕ разворачиваем даже по литеральному
            # маркеру: его value обязан жить только в прокси (§4.4), а не попадать
            # в argv любой команды. store уже не активирует proxy-секрет с env/
            # commands — это второй рубеж (defense-in-depth).
            if (s is None or s.is_proxy or not s.session_allowed(session_name)
                    or not s.value):
                return ""
            if not m.group(2):
                return s.value
            if tmpdir is None:
                tmpdir = tempfile.mkdtemp(prefix="wallet-")
            path = os.path.join(tmpdir, m.group(1))
            if not os.path.exists(path):
                data = s.value if s.value.endswith("\n") else s.value + "\n"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(data)
            return path

        return MARKER_RE.sub(repl, arg)

    cmd = [_sub(a) for a in cmd]

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # своя группа процессов — killpg по таймауту
            )
        except OSError as e:
            return 127, b"", str(e).encode()
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
            return proc.returncode if proc.returncode is not None else 1, out, err
        except asyncio.TimeoutError:
            # Убиваем всю группу: сама команда могла наплодить детей.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except OSError:
                    pass
            try:
                out, err = await proc.communicate()
            except Exception:
                out = err = b""
            return 124, out, err
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
