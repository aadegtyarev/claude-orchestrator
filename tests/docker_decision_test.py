"""Модель угроз docker-прокси (orchestrator/modules/docker/decision.py) — ALLOWLIST.

Скоуп = папка проекта сессии + устройства. Монтировать можно ТОЛЬКО под
разрешёнными корнями; всё прочее (система, секреты, соседние пути) — отказ.

Запуск: .venv/bin/python tests/docker_decision_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.modules.docker.decision import Policy, evaluate  # noqa: E402

# Разрешённые корни = проект сессии.
POL = Policy.for_roots(["/home/op/proj", "/home/op/data"])


def _create(**hc):
    return {"Image": "postgres:16", "HostConfig": dict(hc)}


def ev(body, method="POST", uri="/v1.43/containers/create"):
    return evaluate(method, uri, body, policy=POL)


# ── не-create всегда пускаем ────────────────────────────────────────
def test_non_create_allowed():
    assert evaluate("GET", "/containers/json", None, policy=POL).allow
    assert evaluate("POST", "/containers/abc/start", None, policy=POL).allow
    assert evaluate("POST", "/images/create", None, policy=POL).allow
    print("OK не-create (list/start/pull) → allow")


def test_unparsed_create_denied():
    assert not ev(None).allow
    print("OK create без разобранного тела → deny")


# ── allowlist bind'ов: под проектом можно, вне — нет ────────────────
def test_bind_inside_project_allowed():
    for src in ("/home/op/proj", "/home/op/proj/db", "/home/op/data/x"):
        assert ev(_create(Binds=[f"{src}:/x"])).allow, src
    print("OK bind под корнями проекта → allow")


def test_bind_outside_denied():
    for src in ("/etc", "/etc/shadow", "/usr/bin", "/home/op/.ssh",
                "/home/op/.aws/credentials", "/home/op/other-project",
                "/var/run/docker.sock", "/tmp", "/"):
        v = ev(_create(Binds=[f"{src}:/x"]))
        assert not v.allow, f"должен быть deny (вне проекта): {src}"
    print("OK bind вне проекта (система/секреты/соседи/сокет/tmp//) → deny")


def test_bind_dotdot_escape_denied():
    # /home/op/proj/../.ssh → /home/op/.ssh (вне проекта) → deny
    assert not ev(_create(Binds=["/home/op/proj/../.ssh:/x"])).allow
    print("OK bind с .. за пределы проекта → deny (нормализация)")


def test_named_volume_allowed():
    assert ev(_create(Binds=["pgdata:/var/lib/postgresql/data"])).allow
    print("OK именованный том → allow")


def test_mount_bind_allowlist():
    inside = _create(Mounts=[{"Type": "bind", "Source": "/home/op/proj", "Target": "/x"}])
    assert ev(inside).allow
    outside = _create(Mounts=[{"Type": "bind", "Source": "/etc", "Target": "/x"}])
    assert not ev(outside).allow
    vol = _create(Mounts=[{"Type": "volume", "Source": "v", "Target": "/x"}])
    assert ev(vol).allow
    print("OK --mount bind: под проектом allow, вне deny, volume allow")


def test_volume_create_bind_device():
    ok = {"Name": "v", "Driver": "local",
          "DriverOpts": {"type": "none", "o": "bind", "device": "/home/op/proj/d"}}
    assert evaluate("POST", "/volumes/create", ok, policy=POL).allow
    bad = {"Name": "v", "Driver": "local",
           "DriverOpts": {"type": "none", "o": "bind", "device": "/home/op/.ssh"}}
    assert not evaluate("POST", "/volumes/create", bad, policy=POL).allow
    assert evaluate("POST", "/volumes/create", {"Name": "v"}, policy=POL).allow
    print("OK volume create o=bind: под проектом allow, вне deny; обычный allow")


# ── escape в обход скоупа → deny ────────────────────────────────────
def test_privileged_denied():
    assert not ev(_create(Privileged=True)).allow
    print("OK --privileged → deny")


def test_host_namespaces_denied():
    for key in ("PidMode", "IpcMode", "UTSMode", "UsernsMode"):
        assert not ev(_create(**{key: "host"})).allow, key
    assert ev(_create(PidMode="container:abc")).allow
    print("OK Pid/Ipc/UTS/Userns=host → deny; container:ns → allow")


def test_dangerous_caps_denied():
    assert not ev(_create(CapAdd=["SYS_ADMIN"])).allow
    assert not ev(_create(CapAdd=["net_admin", "SYS_PTRACE"])).allow
    assert ev(_create(CapAdd=["NET_BIND_SERVICE"])).allow
    print("OK опасные caps → deny; безобидная → allow")


# ── сознательно пущено ──────────────────────────────────────────────
def test_devices_allowed():
    assert ev(_create(Devices=[{"PathOnHost": "/dev/ttyUSB0"}])).allow
    print("OK --device (USB/TTY) → allow")


def test_publish_and_host_net_allowed():
    body = _create(NetworkMode="host",
                   PortBindings={"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "5432"}]})
    assert ev(body).allow
    print("OK network=host + публикация → allow")


def test_plain_create_allowed():
    assert ev(_create()).allow  # без bind'ов
    print("OK обычный запуск без bind'ов → allow")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL DOCKER-DECISION OK")


if __name__ == "__main__":
    main()
