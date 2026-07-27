#!/bin/sh
set -e

# vhci-hcd is the kernel driver behind `usbip attach`. Loading it here
# loads it into the real host kernel (shared with the container). No-op
# if the host already loaded it.
modprobe vhci-hcd 2>/dev/null || true
# wireguard.ko is mainline (5.6+) and usually already loaded by the host,
# but load it explicitly too rather than relying on link-creation
# autoload, same reasoning as the modprobe above.
modprobe wireguard 2>/dev/null || true

# The app's own startup (_ensure_wireguard_tunnels_up in app/main.py)
# restores every previously-enabled tunnel from client_config.json - no
# separate `wg-quick up` needed here, since only the Python app has
# access to that state.
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${WEB_PORT:-8001}"
