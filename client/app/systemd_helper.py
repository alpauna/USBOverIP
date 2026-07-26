"""Restart a systemd service on the host, for boxes where a device's
downstream consumer is a native service rather than a Docker container
(e.g. a non-containerized usbip client, or any other host service that
needs to notice a USB device came back).

Same nsenter pattern as proxmox.py: the container has no systemd of its
own, so `systemctl` runs in the host's namespaces via
`nsenter --target 1`, which requires `pid: host` + `privileged: true`
(already set in docker-compose.client.yml for the Proxmox integration).
"""
from __future__ import annotations

from common.procutil import run, validate_service_name

_NSENTER_PREFIX = ["nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--"]


class SystemdError(RuntimeError):
    pass


def _run_on_host(args: list[str], timeout: int = 20):
    return run(_NSENTER_PREFIX + args, timeout=timeout)


def available() -> bool:
    proc = _run_on_host(["which", "systemctl"], timeout=5)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def restart_service(name: str) -> None:
    validate_service_name(name)
    proc = _run_on_host(["systemctl", "restart", name], timeout=30)
    if proc.returncode != 0:
        raise SystemdError((proc.stderr or proc.stdout or "systemctl restart failed").strip())
