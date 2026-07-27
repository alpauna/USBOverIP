import os
from pathlib import Path

from common.config_store import ConfigStore
from common.security import generate_salt, generate_session_secret

DATA_DIR = Path(os.environ.get("USBIP_DATA_DIR", "/data"))
CONFIG_PATH = DATA_DIR / "server_config.json"
USBIPD_PORT = int(os.environ.get("USBIPD_PORT", "3240"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8000"))
HTTPS_ONLY_COOKIE = os.environ.get("HTTPS_ONLY_COOKIE", "0") == "1"


def _defaults() -> dict:
    return {
        "admin_password_hash": None,
        "session_secret": generate_session_secret(),
        # client_id -> {id, name, host, api_port, token, created_at, last_seen}
        # `token` is stored in plaintext (not hashed): it's a mutual secret
        # the client already holds in plaintext to call us, and the server
        # must be able to present the same value when it pushes reconnect
        # notifications to that client - a one-way hash can't be presented.
        "clients": {},
        # busids the admin has shared; auto-rebound on every startup and
        # watched continuously so a server reboot - or a device dropping
        # off and reappearing at a different busid - doesn't silently
        # drop the share.
        "shared_devices": [],
        "device_labels": {},
        # busid -> serial, a snapshot updated on every successful scan of a
        # shared device. USB busids are not stable identifiers - a whole
        # bus renumbering (seen in the wild after a host-level USB reset)
        # can shift every device to a different busid with no warning. When
        # a tracked busid goes missing, this snapshot lets us search current
        # devices for the same serial and relocate automatically instead of
        # requiring an admin to notice and remap everything by hand.
        "known_serials": {},
        "server_name": os.environ.get("SERVER_NAME", "usbip-server"),
        # Optional WireGuard tunnel (server is the hub, each registered
        # client is a spoke) so the USB/IP wire protocol and the API
        # bearer token don't have to travel the LAN in cleartext. Own
        # keypair generated on first startup; `peers` (client_id -> {pubkey,
        # wg_ip, added_at}) is populated by /api/wireguard/register and
        # replayed onto the live interface on every startup - this dict is
        # the single source of truth, the on-disk wg-quick conf only ever
        # holds this box's own [Interface] section.
        "wireguard": {
            "private_key": "",
            "public_key": "",
            "listen_port": 51820,
            "address": "10.99.0.1/24",
            "subnet": "10.99.0.0/24",
            "next_host": 2,  # .1 is the server itself
            "peers": {},
            # The real LAN address clients should dial to reach this box's
            # WireGuard listen port. Deliberately NOT derived from whatever
            # address a /register request arrived on - a client that's
            # already tunneled and re-registers via its tunnel IP would
            # otherwise get told "reach me at my own tunnel address," which
            # is circular. Auto-detected once (see _ensure_wireguard_up)
            # unless SERVER_WG_ENDPOINT_HOST is set.
            "endpoint_host": os.environ.get("SERVER_WG_ENDPOINT_HOST", ""),
        },
        "require_wireguard": False,
    }


store = ConfigStore(CONFIG_PATH, _defaults)
