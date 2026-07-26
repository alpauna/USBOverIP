"""Wrapper around the local `usbip` CLI and the vhci-hcd kernel driver for
attaching/detaching remote devices to this host's virtual USB controller.

Note: the server's TCP port is a *global* usbip option (--tcp-port), not a
per-subcommand flag - `usbip --tcp-port <port> attach -r <host> -b <busid>`.

We do not rely on `usbip port`'s text output to discover what's attached:
in the field it has proven unreliable on at least one real Debian install
("libusbip: error: fopen" / "read_record" - it fails to read its own
state-tracking file and its listing lags behind reality). Instead we read
the kernel's own /sys/devices/platform/vhci_hcd.*/status directly, which is
always live and exact, and is also what `usbip port` is documented to be
based on. This also means we never trust attach's stdout for the assigned
port (some usbip builds only print "using port <TCP-PORT>", which is the
*remote* TCP port, not the local one) - we snapshot ports before/after and
diff instead.

Known limitation: each vhci_hcd.N status file numbers its own ports
starting at 0, so if a system ever attaches enough devices to spill into a
second vhci_hcd instance, port numbers could collide across instances.
Realistic homelab use (a handful of shared devices) stays within the first
instance's ~30 ports, so this isn't handled beyond reading every instance.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import time
from dataclasses import dataclass

from common.procutil import run, validate_busid, validate_hostname, validate_port_number

logger = logging.getLogger("usbip.client.usbip_client")

_VHCI_STATUS_GLOB = "/sys/devices/platform/vhci_hcd.*/status"
_STATUS_LINE_RE = re.compile(
    r"^\s*\S+\s+(?P<port>\d+)\s+(?P<sta>\d+)\s+\S+\s+\S+\s+\S+\s+(?P<busid>\S+)\s*$"
)
# Fallback text parser for `usbip port`, used only if the sysfs status
# files aren't present on this system at all.
_PORT_BLOCK_RE = re.compile(
    r"^Port (?P<port>\d+):.*$\n"
    r"(?:^[ \t].*$\n?)*?"
    r"^\s*(?P<local_busid>\d+-\d+(?:\.\d+)*)\s*->",
    re.MULTILINE,
)


class UsbipCommandError(RuntimeError):
    pass


@dataclass
class AttachedPort:
    port: str
    local_busid: str | None


def attach(host: str, usbip_port: int, busid: str) -> str:
    validate_hostname(host)
    usbip_port = validate_port_number(usbip_port)
    validate_busid(busid)
    before_ports = {p.port for p in list_ports()}
    proc = run(
        ["usbip", "--tcp-port", str(usbip_port), "attach", "-r", host, "-b", busid],
        timeout=30,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        raise UsbipCommandError(output.strip() or "usbip attach failed")

    # The kernel briefly lags behind a successful `usbip attach` before the
    # new port shows up in sysfs - poll for it instead of checking once.
    new_ports: list[AttachedPort] = []
    for _ in range(10):
        after_ports = list_ports()
        new_ports = [p for p in after_ports if p.port not in before_ports]
        if new_ports:
            break
        time.sleep(0.3)
    if len(new_ports) == 1:
        return new_ports[0].port
    if len(new_ports) > 1:
        # Concurrent attach elsewhere raced us; pick the lowest new port and
        # log it so it's visible if this ever actually happens.
        new_ports.sort(key=lambda p: int(p.port))
        logger.warning(
            "multiple new vhci ports appeared after attach (%s); using the lowest",
            [p.port for p in new_ports],
        )
        return new_ports[0].port
    raise UsbipCommandError(
        f"usbip attach reported success but no new local port appeared: {output.strip()}"
    )


def detach(local_port: str) -> None:
    if not local_port.isdigit():
        raise UsbipCommandError(f"invalid local port: {local_port!r}")
    proc = run(["usbip", "detach", "-p", local_port], timeout=20)
    if proc.returncode != 0:
        raise UsbipCommandError((proc.stderr or proc.stdout or "usbip detach failed").strip())


def _list_ports_via_sysfs() -> list[AttachedPort] | None:
    status_files = sorted(glob.glob(_VHCI_STATUS_GLOB))
    if not status_files:
        return None
    ports: list[AttachedPort] = []
    for path in status_files:
        try:
            with open(path) as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in lines[1:]:  # skip "hub port sta spd dev sockfd local_busid" header
            m = _STATUS_LINE_RE.match(line)
            if not m:
                continue
            busid = m.group("busid")
            if busid == "0-0":
                continue  # empty slot
            ports.append(AttachedPort(port=str(int(m.group("port"))), local_busid=busid))
    return ports


def _list_ports_via_cli() -> list[AttachedPort]:
    proc = run(["usbip", "port"], timeout=10)
    ports: list[AttachedPort] = []
    for m in _PORT_BLOCK_RE.finditer(proc.stdout):
        ports.append(
            AttachedPort(port=str(int(m.group("port"))), local_busid=m.group("local_busid"))
        )
    return ports


def list_ports() -> list[AttachedPort]:
    ports = _list_ports_via_sysfs()
    if ports is not None:
        return ports
    return _list_ports_via_cli()


def is_port_active(local_port: str) -> bool:
    return any(p.port == local_port for p in list_ports())


def resolve_device_paths(local_busid: str) -> dict:
    """Best-effort mapping from a local (post-attach) busid to the actual
    /dev nodes it produced, so an admin can wire a new Docker container's
    `devices:` mapping straight to it. Every USB device gets a raw usbfs
    node (/dev/bus/usb/BBB/DDD); a serial-class device (the common case
    here - USB-UART bridges) also gets a /dev/ttyUSBx and, if udev created
    one, a stable /dev/serial/by-id/... symlink."""
    result: dict = {"tty": None, "by_id": [], "raw": None}
    if not local_busid:
        return result

    # The interface subdirectory (busid:config.interface) holds the tty
    # device - as either <iface>/ttyUSBx/ directly, or <iface>/tty/ttyUSBx/
    # depending on kernel/driver version. `tty?*` (not bare `tty`) avoids
    # matching the nested-layout directory itself as if it were the device.
    # Both patterns are a single, bounded glob level - deliberately NOT a
    # recursive/`**` glob into sysfs, which can hang for a very long time
    # chasing its symlink-heavy structure.
    tty_dirs = glob.glob(f"/sys/bus/usb/devices/{local_busid}:*/tty?*") or glob.glob(
        f"/sys/bus/usb/devices/{local_busid}:*/tty/tty?*"
    )
    if tty_dirs:
        result["tty"] = "/dev/" + tty_dirs[0].rstrip("/").rsplit("/", 1)[-1]

    try:
        with open(f"/sys/bus/usb/devices/{local_busid}/busnum") as f:
            busnum = int(f.read().strip())
        with open(f"/sys/bus/usb/devices/{local_busid}/devnum") as f:
            devnum = int(f.read().strip())
        result["raw"] = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
    except (FileNotFoundError, OSError, ValueError):
        pass

    if result["tty"]:
        target = result["tty"].rsplit("/", 1)[-1]
        for link in glob.glob("/dev/serial/by-id/*"):
            try:
                if os.path.realpath(link).rsplit("/", 1)[-1] == target:
                    result["by_id"].append(link)
            except OSError:
                continue

    return result
