"""Manages one nftables table that, once an admin has verified their
WireGuard tunnel works, restricts the raw USB/IP port (3240 - the
protocol with no authentication of its own, see README's Security model)
to the tunnel subnet and loopback.

Deliberately does *not* touch the web/API port: that port is how a brand
new client bootstraps a tunnel in the first place
(/api/wireguard/register), and it's also the admin's own dashboard -
blocking it from the LAN would be a self-lockout, not a security
improvement. A client that has switched to using its assigned WireGuard
IP for that server's `host` field already gets the API traffic (including
the bearer token) inside the tunnel for free, with no firewall change
needed - see common/wireguard_helper.py's docstring.

Kept in its own nftables table (usbip_web) so enabling/disabling this
never touches any other firewall rules already on the box.
"""
from __future__ import annotations

import logging
import shutil

from common.procutil import run, validate_ipv4_cidr, validate_port_number

logger = logging.getLogger("usbip.firewall")

TABLE = "usbip_web"


class FirewallError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("nft") is not None


def is_enabled() -> bool:
    return run(["nft", "list", "table", "inet", TABLE], timeout=10).returncode == 0


def disable() -> None:
    # Ignore failure: the table may simply not exist yet, which is the
    # desired end state either way.
    run(["nft", "delete", "table", "inet", TABLE], timeout=10)


def enable(subnet: str, ports: list[int]) -> None:
    """Idempotent: safe to call repeatedly (e.g. replayed on every app
    startup like every other reconcile loop in this project) - always
    deletes and recreates the table rather than diffing existing rules."""
    validate_ipv4_cidr(subnet)
    for p in ports:
        validate_port_number(p)
    port_list = ", ".join(str(p) for p in ports)
    ruleset = f"""\
table inet {TABLE} {{
    chain input {{
        type filter hook input priority filter; policy accept;
        tcp dport {{ {port_list} }} ip saddr {subnet} accept
        tcp dport {{ {port_list} }} ip saddr 127.0.0.1 accept
        tcp dport {{ {port_list} }} drop
    }}
}}
"""
    disable()
    proc = run(["nft", "-f", "-"], timeout=10, input=ruleset)
    if proc.returncode != 0:
        raise FirewallError((proc.stderr or proc.stdout or "nft apply failed").strip())
    logger.info("firewall: restricted ports %s to %s + loopback", ports, subnet)
