"""Sync and outbox — the two endpoints the event depends on (§8.2, §10.3).

Design notes that are load-bearing, not decoration:

  * `accepted` means "durably known to the server", NOT "newly inserted". A
    replayed op is accepted. If it meant inserted, the second drain would return
    an empty list, the client would keep the op forever, and the outbox would
    wedge permanently — the mirror-image failure of duplicating leads.
  * `captured_at` is whatever the device said. Nothing here overwrites it.
"""

import csv
import io
import json
from datetime import datetime, timezone

import psycopg2

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import auth as A
from . import storage
from .db import audit, bump_version, get_db, new_id, now_iso

router = APIRouter(prefix="/api/v1")

SYNCED = [("client", "clients"), ("poc", "client_pocs"), ("document", "documents"),
          ("meeting", "meetings"), ("update", "updates")]
JSON_COLS = {"clients": ["tags", "talking_points", "avoid_points", "signals"]}


def member(event_id: str, conn, user) -> None:
    """§9.3: a user sees only the events they belong to."""
    if not conn.execute("SELECT 1 FROM events WHERE id = %s", (event_id,)).fetchone():
        raise A.ApiError(404, "NOT_FOUND", "No such event.")
    if not conn.execute("SELECT 1 FROM event_members WHERE event_id = %s AND user_id = %s",
                        (event_id, user["id"])).fetchone():
        raise A.ApiError(403, "FORBIDDEN", "You are not on this event.")


def _rows(conn, table: str, event_id: str, since: int) -> list[dict]:
    out = []
    for r in conn.execute(f"SELECT * FROM {table} WHERE event_id = %s AND version > %s",
                          (event_id, since)):
        d = dict(r)
        for col in JSON_COLS.get(table, []):
            default = [] if col != "signals" else {}
            v = d.get(col)
            if v is None:
                d[col] = default
            elif isinstance(v, str):
                # Shouldn't normally happen (the column is jsonb, and psycopg2 parses
                # jsonb values into dict/list automatically) but tolerate it defensively.
                try:
                    d[col] = json.loads(v)
                except (TypeError, ValueError):
                    d[col] = default
            # else: already a dict/list — psycopg2's jsonb typecaster parsed it for us.
        out.append(d)
    return out


# --- the core call (§10.3) ---------------------------------------------------

@router.get("/events/{event_id}/sync")
def sync(event_id: str, since: int = 0, user=Depends(A.current_user),
         conn = Depends(get_db)):
    member(event_id, conn, user)
    ev = conn.execute("SELECT id, version, name, venue, starts_on, ends_on, timezone"
                      " FROM events WHERE id = %s", (event_id,)).fetchone()

    payload = {
        "event": dict(ev),
        "version": ev["version"],
        # The client clears its replica stores before applying a `full` payload.
        # Everything else is a per-record upsert plus tombstones.
        "full": since <= 0,
        "server_time": now_iso(),
        "tombstones": {},
    }
    for entity, table in SYNCED:
        payload[table] = _rows(conn, table, event_id, since)
        payload["tombstones"][table] = [
            r["entity_id"] for r in conn.execute(
                "SELECT entity_id FROM deleted_rows WHERE event_id = %s AND entity = %s"
                " AND version > %s", (event_id, entity, since))
        ] if not payload["full"] else []

    # Read receipts are per-user, so they ride along rather than living in the replica.
    payload["my_receipts"] = [dict(r) for r in conn.execute(
        "SELECT update_id, read_at, done_at FROM update_receipts r"
        " WHERE user_id = %s AND update_id IN (SELECT id FROM updates WHERE event_id = %s)",
        (user["id"], event_id))]
    return payload


# --- outbox (§8.2) -----------------------------------------------------------

class Op(BaseModel):
    id: str = Field(min_length=8)        # client-generated uuid; the idempotency key
    type: str                            # lead | lead_edit | receipt
    payload: dict
    created_at: str | None = None


class OutboxIn(BaseModel):
    client_sent_at: str | None = None
    ops: list[Op]


def _skew_ms(client_sent_at: str | None) -> int | None:
    """Recorded so admin can spot a phone whose clock is hours off.
    Surfaced, never silently corrected — a rewritten timestamp is worse than a
    visibly wrong one."""
    if not client_sent_at:
        return None
    try:
        sent = datetime.fromisoformat(client_sent_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((datetime.now(timezone.utc) - sent).total_seconds() * 1000)


@router.post("/events/{event_id}/outbox")
def outbox(event_id: str, body: OutboxIn, user=Depends(A.current_user),
           conn = Depends(get_db)):
    member(event_id, conn, user)
    skew = _skew_ms(body.client_sent_at)
    accepted: list[str] = []
    rejected: list[dict] = []

    for op in body.ops:
        # Unlike SQLite, Postgres aborts the whole transaction on a failed statement —
        # every later command on the connection errors until rollback. A SAVEPOINT per
        # op is what actually gives us "reject just this op, earlier/later accepted ops
        # in this batch are unaffected and still commit" on Postgres.
        conn.execute("SAVEPOINT op")
        try:
            if op.type == "lead":
                _apply_lead(conn, event_id, user, op, skew)
            elif op.type == "lead_edit":
                _apply_lead_edit(conn, user, op)
            elif op.type == "receipt":
                _apply_receipt(conn, event_id, user, op)
            else:
                conn.execute("RELEASE SAVEPOINT op")
                rejected.append({"id": op.id, "code": "UNKNOWN_OP",
                                 "message": f"Unknown op type {op.type}."})
                continue
            conn.execute("RELEASE SAVEPOINT op")
            accepted.append(op.id)
        except A.ApiError as e:
            conn.execute("ROLLBACK TO SAVEPOINT op")
            # A 4xx-class rejection is permanent — tell the client so it stops
            # retrying and surfaces the problem instead of looping forever.
            rejected.append({"id": op.id, "code": e.code, "message": e.message})
        except psycopg2.errors.IntegrityError:
            conn.execute("ROLLBACK TO SAVEPOINT op")
            # The device is referencing an event/client/lead that no longer
            # exists server-side (e.g. it was captured against stale cached
            # data). Reject just this op — the savepoint above means earlier
            # accepted ops in this same batch are unaffected and still commit.
            rejected.append({"id": op.id, "code": "STALE_REFERENCE",
                             "message": "This references data that no longer exists on the "
                                        "server. Refresh and re-enter it."})

    return {"accepted": accepted, "rejected": rejected, "server_time": now_iso()}


def _apply_lead(conn, event_id, user, op, skew) -> None:
    p = op.payload
    conn.execute(
        "INSERT INTO leads (id, event_id, client_id, name, company, email, phone, interest,"
        " next_step, notes, captured_by, captured_at, synced_at, clock_skew_ms,"
        " created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT(id) DO NOTHING",            # <- the whole duplicate story
        (op.id, event_id, p.get("client_id"), p.get("name"), p.get("company"),
         p.get("email"), p.get("phone"), p.get("interest"), p.get("next_step"),
         p.get("notes"), user["id"],
         p.get("captured_at") or op.created_at or now_iso(),   # device time, kept verbatim
         now_iso(), skew, now_iso(), now_iso()))


def _apply_lead_edit(conn, user, op) -> None:
    if conn.execute("SELECT 1 FROM op_log WHERE id = %s", (op.id,)).fetchone():
        return                                     # already applied; still "accepted"
    lead_id = op.payload.get("lead_id")
    row = conn.execute("SELECT captured_by FROM leads WHERE id = %s", (lead_id,)).fetchone()
    if row is None:
        raise A.ApiError(404, "NOT_FOUND", "That lead no longer exists.")
    # §12.6: a field user may edit a lead they captured and no one else's.
    if user["role"] != "admin" and row["captured_by"] != user["id"]:
        raise A.ApiError(403, "FORBIDDEN", "You can only edit leads you captured.")

    allowed = ["name", "company", "email", "phone", "interest", "next_step", "notes", "client_id"]
    sets = [f"{k} = %s" for k in allowed if k in op.payload]
    if sets:
        conn.execute(f"UPDATE leads SET {', '.join(sets)}, updated_at = %s WHERE id = %s",
                     (*[op.payload[k] for k in allowed if k in op.payload], now_iso(), lead_id))
    conn.execute("INSERT INTO op_log (id, kind, applied_at) VALUES (%s,%s,%s)",
                 (op.id, "lead_edit", now_iso()))


def _apply_receipt(conn, event_id, user, op) -> None:
    uid = op.payload.get("update_id")
    if not conn.execute("SELECT 1 FROM updates WHERE id = %s AND event_id = %s",
                        (uid, event_id)).fetchone():
        raise A.ApiError(404, "NOT_FOUND", "That update no longer exists.")
    conn.execute(
        "INSERT INTO update_receipts (update_id, user_id, read_at, done_at) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT(update_id, user_id) DO UPDATE SET"
        " read_at = COALESCE(update_receipts.read_at, excluded.read_at),"
        " done_at = excluded.done_at",
        (uid, user["id"], op.payload.get("read_at") or now_iso(), op.payload.get("done_at")))


# --- documents ---------------------------------------------------------------

@router.get("/documents/{doc_id}/content")
def document_content(doc_id: str, user=Depends(A.current_user),
                     conn = Depends(get_db)):
    row = conn.execute("SELECT * FROM documents WHERE id = %s", (doc_id,)).fetchone()
    if row is None:
        raise A.ApiError(404, "NOT_FOUND", "No such document.")
    member(row["event_id"], conn, user)
    try:
        url = storage.signed_url(row["storage_key"])
    except FileNotFoundError:
        raise A.ApiError(410, "GONE", "The stored file is missing from this server.")
    except storage.StorageNotConfigured:
        raise A.ApiError(503, "STORAGE_UNAVAILABLE", "Document storage is not configured.")
    # A short-lived signed URL — the browser downloads straight from Supabase
    # Storage, this server never sees the bytes.
    return RedirectResponse(url, status_code=307)


@router.post("/events/{event_id}/documents")
async def upload_document(event_id: str, request: Request, file: UploadFile = File(...),
                          client_id: str = Form(default=""), note: str = Form(default=""),
                          user=Depends(A.require_admin),
                          conn = Depends(get_db)):
    member(event_id, conn, user)
    data = await file.read()
    try:
        mime = storage.sniff(data, file.filename or "upload")
    except storage.RejectedUpload as e:
        raise A.ApiError(400, "REJECTED_UPLOAD", str(e))

    try:
        digest, size = storage.put_bytes(data)
    except storage.StorageNotConfigured:
        raise A.ApiError(503, "STORAGE_UNAVAILABLE", "Document storage is not configured.")
    version = bump_version(conn, event_id)
    doc_id = new_id()
    conn.execute(
        "INSERT INTO documents (id, event_id, client_id, filename, mime_type, size_bytes,"
        " checksum_sha256, storage_key, note, uploaded_by, version, created_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (doc_id, event_id, client_id or None, file.filename, mime, size, digest, digest,
         note, user["id"], version, now_iso()))
    audit(conn, user["id"], "document.upload", "document", doc_id,
          {"filename": file.filename, "bytes": size})
    return {"id": doc_id, "filename": file.filename, "mime_type": mime,
            "size_bytes": size, "checksum_sha256": digest, "version": version}


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str, user=Depends(A.require_admin),
                    conn = Depends(get_db)):
    row = conn.execute("SELECT * FROM documents WHERE id = %s", (doc_id,)).fetchone()
    if row is None:
        raise A.ApiError(404, "NOT_FOUND", "No such document.")
    member(row["event_id"], conn, user)
    version = bump_version(conn, row["event_id"])
    conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
    # Tombstone, or the file lingers on every device that already synced it.
    conn.execute(
        "INSERT INTO deleted_rows (entity, entity_id, event_id, version) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (entity, entity_id) DO UPDATE SET"
        " event_id = excluded.event_id, version = excluded.version",
        ("document", doc_id, row["event_id"], version))
    audit(conn, user["id"], "document.delete", "document", doc_id, {})
    return {"ok": True, "version": version}


# --- leads read + export -----------------------------------------------------

@router.get("/events/{event_id}/export/phase1-pack.json")
def export_phase1_pack(event_id: str, files: int = 1, user=Depends(A.require_admin),
                       conn = Depends(get_db)):
    """The rollback lever. Emits Phase 1's native briefing-pack format so the
    single-file HTML tool can take over at any point.

    `files=0` gives the much smaller text-only pack, for a mid-event refresh over
    weak hotel wifi.

    Credentials are NOT in the response: the pack gets dropped in a Teams
    channel. Use `python -m app.cli export-phase1` to get them at a terminal.
    """
    from .phase1 import build_pack
    pack, _creds = build_pack(conn, event_id, packed_by=user["full_name"],
                              include_files=bool(files))
    audit(conn, user["id"], "pack.export", "event", event_id,
          {"clients": len(pack["clients"]), "files": len(pack["files"])})
    suffix = "" if files else "-lite"
    return StreamingResponse(
        iter([json.dumps(pack)]), media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="FBSPL-pack-v{pack["packVersion"]}{suffix}.json"'})


@router.get("/events/{event_id}/leads")
def list_leads(event_id: str, user=Depends(A.current_user),
               conn = Depends(get_db)):
    member(event_id, conn, user)
    rows = conn.execute(
        "SELECT l.*, u.full_name AS captured_by_name FROM leads l"
        " LEFT JOIN users u ON u.id = l.captured_by"
        " WHERE l.event_id = %s ORDER BY l.captured_at DESC", (event_id,)).fetchall()
    return {"leads": [dict(r) for r in rows]}


def _csv_safe(v) -> str:
    """§12.6: neutralise spreadsheet formula injection."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@") else s


@router.get("/events/{event_id}/leads/export.csv")
def export_leads(event_id: str, user=Depends(A.current_user),
                 conn = Depends(get_db)):
    member(event_id, conn, user)
    rows = conn.execute(
        "SELECT l.captured_at, l.name, l.company, l.email, l.phone, c.name AS client,"
        " l.interest, l.next_step, u.full_name AS captured_by, l.notes, l.synced_at"
        " FROM leads l LEFT JOIN users u ON u.id = l.captured_by"
        " LEFT JOIN clients c ON c.id = l.client_id"
        " WHERE l.event_id = %s ORDER BY l.captured_at", (event_id,)).fetchall()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Captured", "Name", "Company", "Email", "Phone", "Existing client",
                "Interest", "Next step", "Captured by", "Notes", "Synced"])
    for r in rows:
        # RealDictRow is dict-like: iterating it directly yields keys, not values
        # (sqlite3.Row iterated as values) — go through .values() explicitly.
        w.writerow([_csv_safe(v) for v in r.values()])
    buf.seek(0)
    # BOM so Excel opens UTF-8 cleanly (§12.6).
    return StreamingResponse(
        iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="fbspl-leads-{event_id[:8]}.csv"'})
