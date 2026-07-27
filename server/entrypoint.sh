#!/bin/sh
set -e

# Best-effort: these kernel modules are shared with the host (containers
# don't get their own kernel), so loading them here loads them for the
# real host too. If the host has already loaded them (e.g. via
# /etc/modules-load.d), these are no-ops.
modprobe usbip-core 2>/dev/null || true
modprobe usbip-host 2>/dev/null || true
# wireguard.ko is mainline (5.6+) and usually already loaded by the host,
# but load it explicitly too rather than relying on link-creation
# autoload, same reasoning as the two modprobes above.
modprobe wireguard 2>/dev/null || true

# The app's own startup (_ensure_wireguard_up in app/main.py) generates a
# keypair if needed, brings wg0 up, and replays every known peer onto it -
# no separate `wg-quick up` needed here, since only the Python app has
# access to the peer data (server_config.json), not this shell script.
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${WEB_PORT:-8000}"
