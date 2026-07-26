"""Wrapper around the local `usbip` CLI for attaching/detaching remote
devices to this host's vhci-hcd virtual USB controller.

Note: the server's TCP port is a *global* usbip option (--tcp-port), not a
per-subcommand flag - `usbip --tcp-port <port> attach -r <host> -b <busid>`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from common.procutil import run, validate_busid, validate_hostname, validate_port_number

_ATTACHED_PORT_RE = re.compile(r"port\s+(\d+)", re.IGNORECASE)
# `usbip port` output looks like:
#   Port 00: <Port in Use> at Full Speed(12Mbps)
#          unknown vendor : unknown product (1a86:7523)
#          3-1 -> usbip://192.168.1.129:3240/1-1
#              -> remote bus/dev 001/004
# We want the port number and the *remote* busid (after the usbip:// URL).
_PORT_BLOCK_RE = re.compile(
    r"^Port (?P<port>\d+):.*$\n"
    r"(?:^[ \t].*$\n?)*?"
    r"^\s*(?P<local_busid>\d+-\d+(?:\.\d+)*)\s*->\s*usbip://[^/]+/(?P<remote_busid>\d+-\d+(?:\.\d+)*)\s*$",
    re.MULTILINE,
)


class UsbipCommandError(RuntimeError):
    pass


@dataclass
class AttachedPort:
    port: str
    local_busid: str | None
    remote_busid: str | None


def attach(host: str, usbip_port: int, busid: str) -> str:
    validate_hostname(host)
    usbip_port = validate_port_number(usbip_port)
    validate_busid(busid)
    proc = run(
        ["usbip", "--tcp-port", str(usbip_port), "attach", "-r", host, "-b", busid],
        timeout=30,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    if proc.returncode != 0:
        raise UsbipCommandError(output.strip() or "usbip attach failed")
    m = _ATTACHED_PORT_RE.search(output)
    if m:
        return str(int(m.group(1)))
    # Fall back to reconciling against `usbip port` if the CLI didn't print
    # the port number in this usbip version's output format.
    for p in list_ports():
        if p.remote_busid == busid:
            return p.port
    raise UsbipCommandError(f"attach appeared to succeed but no local port was found: {output.strip()}")


def detach(local_port: str) -> None:
    if not local_port.isdigit():
        raise UsbipCommandError(f"invalid local port: {local_port!r}")
    proc = run(["usbip", "detach", "-p", local_port], timeout=20)
    if proc.returncode != 0:
        raise UsbipCommandError((proc.stderr or proc.stdout or "usbip detach failed").strip())


def list_ports() -> list[AttachedPort]:
    proc = run(["usbip", "port"], timeout=10)
    ports: list[AttachedPort] = []
    for m in _PORT_BLOCK_RE.finditer(proc.stdout):
        ports.append(
            AttachedPort(
                port=str(int(m.group("port"))),
                local_busid=m.group("local_busid"),
                remote_busid=m.group("remote_busid"),
            )
        )
    return ports


def is_port_active(local_port: str) -> bool:
    return any(p.port == local_port for p in list_ports())
