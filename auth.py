"""Username/password accounts (scrypt) with a lockout after repeated failures."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from datetime import datetime, timezone

import storage

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
_DUMMY_SALT = os.urandom(16)


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32).hex()


def register(username: str, password: str) -> str:
    """Creates an account and returns its user key. Raises ValueError with a user-facing message."""
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        raise ValueError("Username must be 3–32 characters: letters, digits, underscore, dot or hyphen.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    salt = os.urandom(16)
    if not storage.user_create(username, salt.hex(), _hash(password, salt), datetime.now(timezone.utc).isoformat()):
        raise ValueError("That username is already taken.")
    return f"pw:{username}"


def login(username: str, password: str, now: float | None = None) -> str:
    """Verifies credentials and returns the user key. The same generic error is used for unknown users and bad passwords."""
    now = time.time() if now is None else now
    username = username.strip().lower()
    row = storage.user_get(username)
    if row and row[4] > now:
        raise ValueError(f"Too many failed attempts. Try again in {int((row[4] - now) // 60) + 1} minute(s).")
    salt = bytes.fromhex(row[1]) if row else _DUMMY_SALT
    candidate = _hash(password, salt)  # always hash, so timing does not reveal whether the user exists
    if row and hmac.compare_digest(candidate, row[2]):
        storage.user_set_attempts(username, 0, 0)
        return f"pw:{username}"
    if row:
        failed = row[3] + 1
        storage.user_set_attempts(username, 0 if failed >= MAX_FAILURES else failed, now + LOCKOUT_SECONDS if failed >= MAX_FAILURES else 0)
    raise ValueError("Incorrect username or password.")
