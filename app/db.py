"""SQLite access. One connection per request — WAL makes that cheap."""

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("FIELDDESK_DB", BASE_DIR / "fielddesk.db"))
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
STORAGE_DIR = Path(os.environ.get("FIELDDESK_STORAGE", BASE_DIR / "storage"))


def now_iso() -> str:
    """UTC, second precision, always suffixed Z. Every timestamp in the DB uses this."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # foreign_keys is per-connection and defaults OFF — it must be set every time.
    # journal_mode is persistent on the database file, so setting it here is a no-op
    # after the first run and costs nothing.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def db():
    """Read-oriented connection. Commits on clean exit so simple writes work too."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def tx():
    """Explicit write transaction. Use for anything that must be all-or-nothing —
    notably bump_version() plus its row writes, which the sync cursor depends on."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db():
    """FastAPI dependency."""
    with db() as conn:
        yield conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()
    finally:
        conn.close()


def bump_version(conn: sqlite3.Connection, event_id: str) -> int:
    """Advance the event's monotonic sync version and return the new value.

    Every publish writes rows stamped with this number. Callers MUST do the bump
    and the row writes inside one transaction — a version that advances without
    its data is how clients end up permanently missing records.
    """
    conn.execute("UPDATE events SET version = version + 1 WHERE id = ?", (event_id,))
    row = conn.execute("SELECT version FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise LookupError(f"no such event: {event_id}")
    return row["version"]


def audit(conn, actor_id, action, entity_type=None, entity_id=None, detail=None, ip=None):
    """§9.11. Every account and password action writes here."""
    import json

    conn.execute(
        "INSERT INTO audit_log (actor_id, action, entity_type, entity_id, detail, ip, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (actor_id, action, entity_type, entity_id,
         json.dumps(detail or {}), ip, now_iso()),
    )
