#!/bin/sh
set -e

# Best-effort: these kernel modules are shared with the host (containers
# don't get their own kernel), so loading them here loads them for the
# real host too. If the host has already loaded them (e.g. via
# /etc/modules-load.d), these are no-ops.
modprobe usbip-core 2>/dev/null || true
modprobe usbip-host 2>/dev/null || true

exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${WEB_PORT:-8000}"
