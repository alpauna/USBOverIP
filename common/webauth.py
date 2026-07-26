"""Shared web-auth helpers for the two FastAPI apps (server + client).

Session cookie auth is used for the human-facing web UI. A lightweight
in-memory throttle slows down password guessing. State-changing API calls
made by our own front-end JS must include the X-Requested-With header;
combined with a SameSite=Lax session cookie this blocks trivial
cross-site-form CSRF without the overhead of a full token scheme, which is
proportionate for a LAN-only admin tool.
"""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

AJAX_HEADER = "x-requested-with"
AJAX_HEADER_VALUE = "usbip-web"


class LoginThrottle:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        now = time.time()
        attempts = [t for t in self._attempts[key] if now - t < self.window_seconds]
        self._attempts[key] = attempts
        if len(attempts) >= self.max_attempts:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many login attempts, try again later",
            )

    def record_failure(self, key: str) -> None:
        self._attempts[key].append(time.time())

    def reset(self, key: str) -> None:
        self._attempts.pop(key, None)


def require_session_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return user


def require_safe_ajax(request: Request) -> None:
    if request.headers.get(AJAX_HEADER) != AJAX_HEADER_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing required header")
