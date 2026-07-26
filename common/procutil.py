"""Safe subprocess helpers.

Every external command invocation in this project goes through here so
there is exactly one place that (a) never uses shell=True and (b) validates
any user-influenced tokens (bus IDs, VM IDs, host names) before they reach
argv.
"""
from __future__ import annotations

import re
import subprocess

BUSID_RE = re.compile(r"^\d+-\d+(\.\d+)*$")
VMID_RE = re.compile(r"^\d{1,6}$")
USBSLOT_RE = re.compile(r"^usb\d$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,253}$")
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


class ValidationError(ValueError):
    pass


def validate_busid(busid: str) -> str:
    if not isinstance(busid, str) or not BUSID_RE.match(busid):
        raise ValidationError(f"invalid USB bus id: {busid!r}")
    return busid


def validate_vmid(vmid: str | int) -> str:
    s = str(vmid)
    if not VMID_RE.match(s):
        raise ValidationError(f"invalid Proxmox VM id: {vmid!r}")
    return s


def validate_usb_slot(slot: str) -> str:
    if not USBSLOT_RE.match(slot):
        raise ValidationError(f"invalid usb slot: {slot!r}")
    return slot


def validate_hostname(host: str) -> str:
    if not isinstance(host, str) or not HOSTNAME_RE.match(host):
        raise ValidationError(f"invalid host: {host!r}")
    return host


def validate_container_name(name: str) -> str:
    if not isinstance(name, str) or not CONTAINER_NAME_RE.match(name):
        raise ValidationError(f"invalid container name: {name!r}")
    return name


def validate_port_number(port: str | int) -> int:
    try:
        p = int(port)
    except (TypeError, ValueError):
        raise ValidationError(f"invalid port: {port!r}")
    if not (1 <= p <= 65535):
        raise ValidationError(f"invalid port: {port!r}")
    return p


def run(args: list[str], timeout: int = 20, check: bool = False) -> subprocess.CompletedProcess:
    """Run a command with a list of args only. Never pass shell=True here."""
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValidationError("args must be a list[str]")
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )
