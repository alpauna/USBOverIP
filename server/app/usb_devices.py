"""Enumeration and bind/unbind of local USB devices via the `usbip` CLI.

`usbip list -l` enumerates devices eligible for export. Once a device is
bound (usbip bind), the kernel's usbip-host driver exposes
/sys/bus/usb/devices/<busid>/usbip_status, which is the authoritative
signal for whether a client currently has the device attached (2) versus
merely shared-but-idle (1). We read that instead of keeping our own
attach-tracking state, so the server always reflects kernel reality.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from common.procutil import run, validate_busid

_BUSID_LINE = re.compile(
    r"^\s*-\s*busid\s+(?P<busid>\S+)\s+\((?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4})\)\s*$"
)

STATUS_MAP = {"1": "shared_idle", "2": "shared_in_use"}


@dataclass
class UsbDevice:
    busid: str
    vendor_id: str
    product_id: str
    description: str
    status: str  # unshared | shared_idle | shared_in_use | unknown
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _read_status(busid: str) -> str:
    try:
        with open(f"/sys/bus/usb/devices/{busid}/usbip_status") as f:
            value = f.read().strip()
    except (FileNotFoundError, OSError):
        return "unshared"
    return STATUS_MAP.get(value, "unknown")


def list_local_devices(labels: dict[str, str] | None = None) -> list[UsbDevice]:
    labels = labels or {}
    proc = run(["usbip", "list", "-l"])
    devices: list[UsbDevice] = []
    lines = proc.stdout.splitlines()
    i = 0
    while i < len(lines):
        m = _BUSID_LINE.match(lines[i])
        if m:
            busid = m.group("busid")
            vid = m.group("vid").lower()
            pid = m.group("pid").lower()
            desc = ""
            if i + 1 < len(lines):
                desc_line = lines[i + 1].strip()
                desc = re.sub(
                    r"\s*\(" + re.escape(vid) + ":" + re.escape(pid) + r"\)\s*$",
                    "",
                    desc_line,
                    flags=re.IGNORECASE,
                )
            devices.append(
                UsbDevice(
                    busid=busid,
                    vendor_id=vid,
                    product_id=pid,
                    description=desc,
                    status=_read_status(busid),
                    label=labels.get(busid, ""),
                )
            )
            i += 2
        else:
            i += 1
    return devices


def bind_device(busid: str) -> None:
    validate_busid(busid)
    proc = run(["usbip", "bind", "-b", busid])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "usbip bind failed").strip())


def unbind_device(busid: str) -> None:
    validate_busid(busid)
    proc = run(["usbip", "unbind", "-b", busid])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "usbip unbind failed").strip())
