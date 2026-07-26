"""Proxmox VE integration for hosts where this client runs directly on a
Proxmox node.

The container does not carry a Proxmox install (pmxcfs/cluster config
live only on the real host at /etc/pve). Rather than trying to replicate
that environment inside the image, we execute `qm`/`pvesh` in the host's
namespaces via `nsenter --target 1`, which requires the container to run
with `pid: host` and `privileged: true` (see docker-compose.client.yml).
On hosts that are not Proxmox nodes (e.g. the Home Assistant VM), these
calls simply fail/return empty and the UI hides the Proxmox panel.
"""
from __future__ import annotations

import re

from common.procutil import run, validate_busid, validate_usb_slot, validate_vmid

_NSENTER_PREFIX = ["nsenter", "--target", "1", "--mount", "--uts", "--ipc", "--net", "--pid", "--"]
_USB_SLOT_LINE_RE = re.compile(r"^usb(\d):\s")


class ProxmoxError(RuntimeError):
    pass


def _run_on_host(args: list[str], timeout: int = 20):
    return run(_NSENTER_PREFIX + args, timeout=timeout)


def available() -> bool:
    proc = _run_on_host(["which", "qm"], timeout=5)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def list_vms() -> list[dict]:
    proc = _run_on_host(["qm", "list"])
    if proc.returncode != 0:
        raise ProxmoxError((proc.stderr or "qm list failed").strip())
    vms = []
    lines = proc.stdout.splitlines()
    for line in lines[1:]:  # skip header: VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID
        parts = line.split()
        if len(parts) >= 3:
            vms.append({"vmid": parts[0], "name": parts[1], "status": parts[2]})
    return vms


def _used_usb_slots(vmid: str) -> set[str]:
    proc = _run_on_host(["qm", "config", vmid])
    if proc.returncode != 0:
        raise ProxmoxError((proc.stderr or f"qm config {vmid} failed").strip())
    used = set()
    for line in proc.stdout.splitlines():
        m = _USB_SLOT_LINE_RE.match(line)
        if m:
            used.add(f"usb{m.group(1)}")
    return used


def _next_free_slot(vmid: str) -> str:
    used = _used_usb_slots(vmid)
    for i in range(5):  # Proxmox supports usb0..usb4
        slot = f"usb{i}"
        if slot not in used:
            return slot
    raise ProxmoxError("no free usb slot (usb0-usb4 all in use) on this VM")


def attach_usb_to_vm(vmid: str, busid: str) -> str:
    vmid = validate_vmid(vmid)
    validate_busid(busid)
    slot = _next_free_slot(vmid)
    proc = _run_on_host(["qm", "set", vmid, f"-{slot}", f"host={busid}"])
    if proc.returncode != 0:
        raise ProxmoxError((proc.stderr or proc.stdout or "qm set failed").strip())
    return slot


def detach_usb_from_vm(vmid: str, slot: str) -> None:
    vmid = validate_vmid(vmid)
    validate_usb_slot(slot)
    proc = _run_on_host(["qm", "set", vmid, "-delete", slot])
    if proc.returncode != 0:
        raise ProxmoxError((proc.stderr or proc.stdout or "qm set -delete failed").strip())
