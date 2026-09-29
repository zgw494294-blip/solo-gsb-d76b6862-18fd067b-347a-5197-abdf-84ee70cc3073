"""SQLite persistence for snapshots and log segments."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator

DB_PATH = os.environ.get("RECOVERY_DB", "/data/recovery.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    applied_seq INTEGER NOT NULL CHECK (applied_seq >= 0),
    format      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    format     TEXT NOT NULL,
    first_seq  INTEGER NOT NULL CHECK (first_seq >= 1),
    last_seq   INTEGER NOT NULL CHECK (last_seq >= 1),
    CHECK (first_seq <= last_seq)
);
"""


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # check_same_thread=False: FastAPI may resolve the dependency in a worker
    # thread while an async endpoint uses it in the event-loop thread. Each
    # request owns its own connection, so access is never concurrent.
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create schema if it does not exist yet (idempotent)."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: one connection per request, with rollback on error."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
