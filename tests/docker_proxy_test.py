"""Живой тест тонкого прокси против НАСТОЯЩЕГО docker.sock через настоящий docker
CLI. Проверяем end-to-end: чужой bind отбит, свой проходит полным циклом
run --rm (create→attach-стрим→wait→delete — проверка сплайса и Connection: close на
отсутствие зависаний), privileged отбит, --device пущен, compose доходит до фильтра.

Жёсткий таймаут на каждую команду: если прокси зависнет — тест УПАДёт, не повиснет.
Мягкий скип, если докер недоступен.

Запуск: .venv/bin/python tests/docker_proxy_test.py
"""
import asyncio
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.docker.proxy import DockerProxy  # noqa: E402

IMG = "postgres:16-alpine"  # в кэше; гоняем через --entrypoint, БД не поднимаем
CMD_TIMEOUT = 45


def _docker_ok() -> bool:
    try:
        if subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                          capture_output=True, timeout=15).returncode != 0:
            return False
        return subprocess.run(["docker", "image", "inspect", IMG],
                              capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _compose_ok() -> bool:
    try:
        return subprocess.run(["docker", "compose", "version"],
                              capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


async def _docker(argv, sock, timeout=CMD_TIMEOUT, cwd=None):
    proc = await asyncio.create_subprocess_exec(
        "docker", *argv,
        env={"DOCKER_HOST": f"unix://{sock}", "PATH": "/usr/bin:/bin"},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise AssertionError(f"docker {argv[0]} завис > {timeout}s — прокси не отдал ответ")
    return proc.returncode, out.decode(), err.decode()


def _real(argv, timeout=30):
    return subprocess.run(["docker", *argv], capture_output=True, timeout=timeout)


async def scenario():
    tag = f"pxtest-{uuid.uuid4().hex[:8]}"
    work = Path(tempfile.mkdtemp())   # «проект сессии» = единственный корень allowlist
    sock = Path(tempfile.mkdtemp()) / "docker.sock"
    proxy = DockerProxy(sock, roots_provider=lambda: [work])
    await proxy.start()
    try:
        # 1) чужой bind (/etc) → отказ с нашим объяснением
        code, out, err = await _docker(
            ["run", "--rm", "--entrypoint", "echo", "-v", "/etc:/x", IMG, "hi"], sock)
        assert code != 0 and "docker-proxy" in err, (code, err)
        print("OK live: bind /etc → отказ")

        # 2) секрет ~/.ssh → отказ
        code, out, err = await _docker(
            ["run", "--rm", "--entrypoint", "echo", "-v",
             f"{Path.home()}/.ssh:/x", IMG, "hi"], sock)
        assert code != 0 and "docker-proxy" in err, (code, err)
        print("OK live: bind ~/.ssh → отказ")

        # 2b) /tmp — не секрет, но ВНЕ проекта → тоже отказ (это allowlist)
        code, out, err = await _docker(
            ["run", "--rm", "--entrypoint", "echo", "-v", "/tmp:/x", IMG, "hi"], sock)
        assert code != 0 and "docker-proxy" in err, (code, err)
        print("OK live: bind /tmp (вне проекта) → отказ (allowlist, не denylist)")

        # 3) свой рабочий путь → полный цикл run --rm без зависаний
        code, out, err = await _docker(
            ["run", "--rm", "--name", f"{tag}-ok", "-v", f"{work}:/x",
             "--entrypoint", "echo", IMG, "hi"], sock)
        assert code == 0 and "hi" in out, (code, out, err)
        print("OK live: свой bind → run --rm прошёл end-to-end (сплайс не завис)")

        # 4) две команды подряд — соединения не путаются (Connection: close)
        for i in (1, 2):
            code, out, err = await _docker(
                ["run", "--rm", "--entrypoint", "echo", IMG, f"n{i}"], sock)
            assert code == 0 and f"n{i}" in out, (i, code, out, err)
        print("OK live: две команды подряд — состояние соединений чистое")

        # 5) --privileged → отказ
        code, out, err = await _docker(
            ["run", "--rm", "--privileged", "--entrypoint", "echo", IMG, "hi"], sock)
        assert code != 0 and "docker-proxy" in err, (code, err)
        print("OK live: --privileged → отказ")

        # 6) --device → ПУЩЕН (сознательно)
        code, out, err = await _docker(
            ["run", "--rm", "--device", "/dev/null:/dev/xnull",
             "--entrypoint", "echo", IMG, "dev-ok"], sock)
        assert code == 0 and "dev-ok" in out, (code, out, err)
        print("OK live: --device /dev/null → пущен")

        # 7) compose доходит до фильтра: сервис с bind /etc → отказ
        if _compose_ok():
            (work / "docker-compose.yml").write_text(
                f"services:\n  bad:\n    image: {IMG}\n    entrypoint: echo hi\n"
                f"    volumes:\n      - /etc:/x\n"
            )
            code, out, err = await _docker(["compose", "up", "--abort-on-container-exit"],
                                           sock, cwd=str(work))
            assert code != 0 and "docker-proxy" in err, (code, err)
            print("OK live: compose с bind /etc → отказ (фильтр видит create от compose)")
        else:
            print("SKIP compose: docker compose недоступен")

        print("ALL DOCKER-PROXY LIVE OK")
    finally:
        await proxy.stop()
        ids = _real(["ps", "-aq", "--filter", f"name={tag}"]).stdout.decode().split()
        if ids:
            _real(["rm", "-f", *ids])


def main():
    if not _docker_ok():
        print(f"SKIP docker-proxy live: докер или образ {IMG} недоступен")
        return
    asyncio.run(asyncio.wait_for(scenario(), 300))


if __name__ == "__main__":
    main()
