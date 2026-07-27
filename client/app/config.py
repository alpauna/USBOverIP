import os
from pathlib import Path

from common.config_store import ConfigStore
from common.security import generate_session_secret

DATA_DIR = Path(os.environ.get("USBIP_DATA_DIR", "/data"))
CONFIG_PATH = DATA_DIR / "client_config.json"
WEB_PORT = int(os.environ.get("WEB_PORT", "8001"))
HTTPS_ONLY_COOKIE = os.environ.get("HTTPS_ONLY_COOKIE", "0") == "1"
CLIENT_NAME = os.environ.get("CLIENT_NAME", "usbip-client")


def _defaults() -> dict:
    return {
        "admin_password_hash": None,
        "session_secret": generate_session_secret(),
        # server_id -> {id, name, host, api_port, usbip_port, token, role}
        "servers": {},
        # group_id -> {id, name, candidates: [{server_id, busid, label}], auto_failover}
        "groups": {},
        # local_port(str) -> {server_id, busid, label, group_id, attached_at, auto_failover}
        "attachments": {},
        # This client's own WireGuard identity - one keypair, reused across
        # every server it tunnels to. Each server gets its own interface
        # (wg-<server_id>, see main.py's wireguard/enable endpoint) rather
        # than sharing one wg0, since WireGuard's anti-spoofing check
        # requires this client's local address to fall within whatever
        # AllowedIPs that specific server issued it - two different
        # servers' assignments can't both be true of a single address.
        # Per-server tunnel state (assigned IP, server pubkey/endpoint,
        # interface name) lives on the server record itself, under its own
        # "wireguard" key.
        "wireguard": {"private_key": "", "public_key": ""},
    }


store = ConfigStore(CONFIG_PATH, _defaults)
