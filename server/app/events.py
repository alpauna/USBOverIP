"""Small in-memory recent-activity log for the server dashboard - mirrors
client/app/groups.py's EVENTS/log_event. Without this, diagnosing a failed
share/bind/client-auth issue required reading raw container logs; this
surfaces the same information in the web UI.
"""
from __future__ import annotations

import datetime
import logging
from collections import deque

logger = logging.getLogger("usbip.server.events")

EVENTS: deque[dict] = deque(maxlen=50)


def log_event(message: str) -> None:
    EVENTS.appendleft({"time": datetime.datetime.utcnow().isoformat() + "Z", "message": message})
    logger.info(message)
