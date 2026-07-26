#!/bin/sh
set -e

# vhci-hcd is the kernel driver behind `usbip attach`. Loading it here
# loads it into the real host kernel (shared with the container). No-op
# if the host already loaded it.
modprobe vhci-hcd 2>/dev/null || true

exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${WEB_PORT:-8001}"
