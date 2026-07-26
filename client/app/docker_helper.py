"""Optional helper for hosts where this client runs alongside other Docker
containers on the same machine (e.g. a Home Assistant container).

Attaching a device with usbip makes it appear at /dev/bus/usb/<bus>/<dev>
on the *host*. A sibling container only sees it live, without a restart,
if it already bind-mounts /dev/bus/usb (or /dev) and allows the USB
device-cgroup class - see README's Home Assistant section. This module
just gives the admin visibility into local containers and a way to
restart one if it needs to re-scan a specific device path.

Requires /var/run/docker.sock to be mounted into this container. If it
is not present, every function here is a no-op / returns empty so the UI
can hide the panel gracefully.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from common.procutil import run, validate_container_name

_SOCK_PATH = Path("/var/run/docker.sock")


def available() -> bool:
    return _SOCK_PATH.exists() and shutil.which("docker") is not None


def list_containers() -> list[dict]:
    if not available():
        return []
    proc = run(
        ["docker", "ps", "-a", "--format", "{{.ID}}|{{.Names}}|{{.Status}}|{{.Image}}"],
        timeout=15,
    )
    if proc.returncode != 0:
        return []
    containers = []
    for line in proc.stdout.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            containers.append(
                {"id": parts[0], "name": parts[1], "status": parts[2], "image": parts[3]}
            )
    return containers


def restart_container(name: str) -> None:
    validate_container_name(name)
    proc = run(["docker", "restart", name], timeout=60)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "docker restart failed").strip())
