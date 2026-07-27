"""Thin wrapper around the `wg`/`wg-quick` CLIs for the optional WireGuard
tunnel feature (server<->client USB/IP + API traffic).

Both apps already run `privileged: true` + `network_mode: host` (needed to
modprobe usbip-host/vhci-hcd into the *host* kernel and reach TCP 3240
directly) - that combination already grants everything WireGuard needs
(/dev/net/tun, NET_ADMIN, and - because there's no network namespace
boundary - a wg0 brought up here is the host's real wg0, exactly like the
existing kernel module loading). No sidecar container is used.

Peer data (who's allowed, their assigned IP) is *not* kept in the on-disk
wg-quick conf file - it's kept in this app's own JSON config store, same
as every other piece of durable state here, and replayed onto the live
interface with apply_peer() on every startup. The conf file on disk only
ever holds this box's own [Interface] section (private key, address,
listen port), so there is exactly one source of truth for peers.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile

from .procutil import run, validate_endpoint, validate_ipv4_cidr, validate_wg_key

logger = logging.getLogger("usbip.wireguard")

CONF_DIR = "/etc/wireguard"


class WireguardError(RuntimeError):
    pass


def available() -> bool:
    return (
        shutil.which("wg") is not None
        and shutil.which("wg-quick") is not None
        and shutil.which("ip") is not None
    )


def generate_keypair() -> tuple[str, str]:
    """Returns (private_key, public_key), both base64."""
    priv = run(["wg", "genkey"], timeout=10)
    if priv.returncode != 0:
        raise WireguardError((priv.stderr or "wg genkey failed").strip())
    private_key = priv.stdout.strip()
    pub = run(["wg", "pubkey"], timeout=10, input=private_key)
    if pub.returncode != 0:
        raise WireguardError((pub.stderr or "wg pubkey failed").strip())
    return private_key, pub.stdout.strip()


def _write_interface_conf(
    private_key: str, address: str, listen_port: int | None, interface: str
) -> None:
    validate_wg_key(private_key)
    validate_ipv4_cidr(address)
    lines = ["[Interface]", f"PrivateKey = {private_key}", f"Address = {address}"]
    if listen_port:
        lines.append(f"ListenPort = {listen_port}")
    text = "\n".join(lines) + "\n"

    os.makedirs(CONF_DIR, exist_ok=True)
    path = f"{CONF_DIR}/{interface}.conf"
    fd, tmp_path = tempfile.mkstemp(dir=CONF_DIR, prefix=f".tmp-{interface}-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _interface_exists(interface: str) -> bool:
    return run(["ip", "link", "show", interface], timeout=10).returncode == 0


def ensure_interface_up(
    private_key: str, address: str, listen_port: int | None = None, interface: str = "wg0"
) -> None:
    """Idempotent: writes this box's own [Interface] section and brings the
    interface up if it isn't already. Safe to call on every app startup -
    does not touch peers (those are reapplied separately via apply_peer,
    same on-startup-reconciliation shape as this project's other
    idempotent-reconcile loops)."""
    _write_interface_conf(private_key, address, listen_port, interface)
    if _interface_exists(interface):
        return
    proc = run(["wg-quick", "up", interface], timeout=15)
    if proc.returncode != 0:
        raise WireguardError((proc.stderr or proc.stdout or "wg-quick up failed").strip())
    logger.info("wireguard interface %s up (address=%s)", interface, address)


def tear_down(interface: str = "wg0") -> None:
    if not _interface_exists(interface):
        return
    proc = run(["wg-quick", "down", interface], timeout=15)
    if proc.returncode != 0:
        raise WireguardError((proc.stderr or proc.stdout or "wg-quick down failed").strip())


def apply_peer(
    pubkey: str, allowed_ips: str, endpoint: str | None = None, interface: str = "wg0"
) -> None:
    """Adds/updates a peer live. Unlike `wg-quick up` (which also installs
    a kernel route for each peer's AllowedIPs as part of bringing the
    whole interface up), a bare `wg set` does not touch the routing table
    - so this adds that route itself (`ip route replace`, idempotent, so
    reapplying an already-known peer on every startup is safe)."""
    validate_wg_key(pubkey)
    validate_ipv4_cidr(allowed_ips)
    args = ["wg", "set", interface, "peer", pubkey, "allowed-ips", allowed_ips]
    if endpoint:
        validate_endpoint(endpoint)
        args += ["endpoint", endpoint]
    proc = run(args, timeout=10)
    if proc.returncode != 0:
        raise WireguardError((proc.stderr or proc.stdout or "wg set failed").strip())
    route_proc = run(["ip", "route", "replace", allowed_ips, "dev", interface], timeout=10)
    if route_proc.returncode != 0:
        logger.warning(
            "could not install route for %s via %s: %s",
            allowed_ips,
            interface,
            (route_proc.stderr or route_proc.stdout or "").strip(),
        )


def remove_peer(pubkey: str, interface: str = "wg0") -> None:
    validate_wg_key(pubkey)
    run(["wg", "set", interface, "peer", pubkey, "remove"], timeout=10)


def interface_status(interface: str = "wg0") -> dict:
    """Best-effort live status via `wg show <iface> dump`. Returns
    {"up": False} if the interface doesn't exist (e.g. never configured),
    rather than raising - this is a read-only dashboard query."""
    if not _interface_exists(interface):
        return {"up": False, "public_key": "", "listen_port": None, "peers": []}
    proc = run(["wg", "show", interface, "dump"], timeout=10)
    if proc.returncode != 0:
        return {"up": False, "public_key": "", "listen_port": None, "peers": []}
    lines = [l for l in proc.stdout.split("\n") if l]
    if not lines:
        return {"up": True, "public_key": "", "listen_port": None, "peers": []}
    iface_fields = lines[0].split("\t")
    peers = []
    for line in lines[1:]:
        f = line.split("\t")
        if len(f) < 7:
            continue
        peers.append(
            {
                "public_key": f[0],
                "endpoint": f[2] if f[2] != "(none)" else None,
                "allowed_ips": f[3],
                "latest_handshake": int(f[4]) if f[4].isdigit() else 0,
                "transfer_rx": int(f[5]) if f[5].isdigit() else 0,
                "transfer_tx": int(f[6]) if f[6].isdigit() else 0,
            }
        )
    return {
        "up": True,
        "public_key": iface_fields[1] if len(iface_fields) > 1 else "",
        "listen_port": int(iface_fields[2])
        if len(iface_fields) > 2 and iface_fields[2].isdigit()
        else None,
        "peers": peers,
    }
