"""Tiny SQLite storage: users, a key/value table (portfolios, market snapshot) and login-attempt tracking.

SQLite lives on local disk. On hosts with an ephemeral filesystem (e.g. Streamlit Community Cloud) point
DAYBREAK_DB at a persistent location, or swap this module for a hosted database (same four functions).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("DAYBREAK_DB", Path(__file__).parent / "data" / "daybreak.db"))
# Serialises read-modify-write cycles on portfolios across Streamlit session threads.
LOCK = threading.RLock()


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, salt TEXT NOT NULL, hash TEXT NOT NULL, "
                     "created_at TEXT NOT NULL, failed INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0)")
        yield conn
        conn.commit()
    finally:
        conn.close()


def kv_get(key: str):
    with _db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def kv_set(key: str, value) -> None:
    with _db() as conn:
        conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))


def user_get(username: str):
    with _db() as conn:
        return conn.execute("SELECT username, salt, hash, failed, locked_until FROM users WHERE username = ?", (username,)).fetchone()


def user_create(username: str, salt: str, pw_hash: str, created_at: str) -> bool:
    try:
        with _db() as conn:
            conn.execute("INSERT INTO users(username, salt, hash, created_at) VALUES(?, ?, ?, ?)", (username, salt, pw_hash, created_at))
        return True
    except sqlite3.IntegrityError:
        return False


def user_set_attempts(username: str, failed: int, locked_until: float) -> None:
    with _db() as conn:
        conn.execute("UPDATE users SET failed = ?, locked_until = ? WHERE username = ?", (failed, locked_until, username))
