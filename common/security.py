"""Password hashing and API token helpers.

Admin passwords are hashed with bcrypt and never stored or logged in
plaintext. We call the `bcrypt` library directly rather than going through
passlib: passlib 1.7.x's bcrypt backend self-test is broken against
bcrypt>=4.1 (it crashes with "password cannot be longer than 72 bytes"
during backend detection, unrelated to the actual password being hashed)
and the project is unmaintained, so it's not worth carrying as a
dependency here.

API tokens (used for server<->client auth) are generated with
secrets.token_urlsafe, shown to the operator exactly once, and stored only
as a salted sha256 digest.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

import bcrypt

MAX_PASSWORD_BYTES = 72  # bcrypt's own hard limit


class PasswordTooLongError(ValueError):
    pass


def hash_password(password: str) -> str:
    pw_bytes = password.encode("utf-8")
    if len(pw_bytes) > MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str, salt: str) -> str:
    return hashlib.sha256((salt + token).encode("utf-8")).hexdigest()


def verify_token(token: str, salt: str, token_hash: str) -> bool:
    if not token or not token_hash:
        return False
    candidate = hash_token(token, salt)
    return hmac.compare_digest(candidate, token_hash)


def generate_salt() -> str:
    return secrets.token_hex(16)


def generate_session_secret() -> str:
    return secrets.token_urlsafe(48)
