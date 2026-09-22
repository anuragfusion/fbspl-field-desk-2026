"""Postgres access (via psycopg2). One connection per request.

Runs against Supabase-hosted Postgres in production, and against a real Postgres
instance in dev/tests too (DATABASE_URL always points at Postgres — there is no
sqlite fallback).
"""

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extensions
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

# Picks up a local .env file (gitignored) so DATABASE_URL etc. are available without
# having to export them by hand in a dev shell. In prod the platform sets real env vars,
# and load_dotenv() is a no-op if no .env file is present.
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def now_iso() -> str:
    """UTC, second precision, always suffixed Z. Every timestamp in the DB uses this."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


class Connection:
    """Wraps a psycopg2 connection so call sites can keep using the sqlite3-style
    `conn.execute(sql, params)` shortcut. psycopg2 connections don't have .execute()
    themselves — only cursors do — so this opens a cursor per call and hands it back;
    it supports fetchone()/fetchall()/rowcount/iteration same as before. Cursors use
    RealDictCursor, so rows behave like sqlite3.Row: dict(row) and row["col"] both work.
    """

    def __init__(self, pg_conn: "psycopg2.extensions.connection"):
        self._conn = pg_conn

    def execute(self, sql, params=None):
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        return cur

    def cursor(self, *args, **kwargs):
        kwargs.setdefault("cursor_factory", RealDictCursor)
        return self._conn.cursor(*args, **kwargs)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Point it at your Postgres instance "
            "(Supabase in production, a local/Docker Postgres for dev and tests)."
        )
    return url


def connect() -> Connection:
    pg_conn = psycopg2.connect(_database_url())
    # Postgres enforces foreign keys unconditionally (no per-connection PRAGMA needed)
    # and has no WAL/busy-timeout knobs to set here — those were sqlite-only concerns.
    return Connection(pg_conn)


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
        # psycopg2 connections start a transaction implicitly on the first statement
        # (autocommit is off by default) — no equivalent of sqlite's BEGIN IMMEDIATE
        # is needed to get the same all-or-nothing contract.
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
    pg_conn = psycopg2.connect(_database_url())
    try:
        cur = pg_conn.cursor()
        # A plain cursor.execute() with no params runs the whole semicolon-separated
        # schema file in one call via the simple query protocol (params=None means
        # psycopg2 doesn't try to do %-style substitution on the string).
        cur.execute(SCHEMA_PATH.read_text())
        pg_conn.commit()
    finally:
        pg_conn.close()


def bump_version(conn: Connection, event_id: str) -> int:
    """Advance the event's monotonic sync version and return the new value.

    Every publish writes rows stamped with this number. Callers MUST do the bump
    and the row writes inside one transaction — a version that advances without
    its data is how clients end up permanently missing records.
    """
    conn.execute("UPDATE events SET version = version + 1 WHERE id = %s", (event_id,))
    row = conn.execute("SELECT version FROM events WHERE id = %s", (event_id,)).fetchone()
    if row is None:
        raise LookupError(f"no such event: {event_id}")
    return row["version"]


def audit(conn, actor_id, action, entity_type=None, entity_id=None, detail=None, ip=None):
    """§9.11. Every account and password action writes here."""
    import json

    conn.execute(
        "INSERT INTO audit_log (actor_id, action, entity_type, entity_id, detail, ip, created_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (actor_id, action, entity_type, entity_id,
         json.dumps(detail or {}), ip, now_iso()),
    )
