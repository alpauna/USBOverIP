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
        "token_salt": generate_salt(),
        "token_hash": None,
        "token_created_at": None,
        "device_labels": {},
        "server_name": os.environ.get("SERVER_NAME", "usbip-server"),
    }


store = ConfigStore(CONFIG_PATH, _defaults)
