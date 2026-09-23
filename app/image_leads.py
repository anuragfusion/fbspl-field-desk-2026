"""Image leads: up to two photos of a card, captured on the floor, as a lead.

Separate from leads and documents end to end: own tables, own bucket
(app/image_storage.py), own endpoints. Load-bearing rules:

  * Every id is phone-generated, so any request can be retried safely.
  * A floor user sees and touches only their own image leads; admin sees all.
  * One immutable file per slot once uploaded.
  * Nothing the phone reports is trusted: the type is sniffed, the fingerprint
    (SHA-256 of the exact bytes received) is recomputed, and the uploader comes
    from the session. Width/height are phone-reported and display-only.
"""

import csv
import hashlib
import io
import logging
import os
import re
import sys
import time
from datetime import datetime

import httpx
import psycopg2
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import auth as A
from . import image_storage as S
from .api import _csv_safe, _skew_ms, member
from .db import audit, get_db, now_iso

router = APIRouter(prefix="/api/v1")

MAX_PHOTOS = 2
MAX_NAME = 200
MAX_NOTE = 1000
ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")      # also keeps storage paths safe
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
DEVICE_STAGES = {"compress", "upload", "error", "info"}
DEVICE_PCTS = {0, 25, 50, 75, 100}


# --- logging -----------------------------------------------------------------
# Own handler: uvicorn does not print INFO from application loggers otherwise.

log = logging.getLogger("fielddesk.image_leads")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    _fmt.converter = time.gmtime
    _h.setFormatter(_fmt)
    log.addHandler(_h)
    log.setLevel(logging.DEBUG)
    log.propagate = False


def _log_setting() -> str:
    v = os.environ.get("IMAGE_LOG_LEVEL", "info").strip().lower()
    return v if v in ("info", "debug", "off") else "info"


def _short(value: str | None) -> str:
    return (value or "-")[:8]


def _log(level: int, source: str, lead_id: str | None, text: str, photo: str = "") -> None:
    """Only ids, sizes, timings and codes. Never names, filenames, tokens or links."""
    setting = _log_setting()
    if setting == "off" or (level < logging.INFO and setting != "debug"):
        return
    tag = f"[image-lead] {source:<6} lead={_short(lead_id)}"
    if photo:
        tag += f" photo={photo}"
    log.log(level, "%s  %s", tag, text)


# --- config (env) ------------------------------------------------------------

def _env_number(name: str, default, lo, hi, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        value = None
    if value is None or not lo <= value <= hi:
        _log(logging.WARNING, "SERVER", None,
             f"config {name} is invalid (allowed {lo}-{hi}); using default {default}")
        return default
    return value


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in allowed:
        _log(logging.WARNING, "SERVER", None,
             f"config {name} is invalid (allowed {', '.join(sorted(allowed))}); using {default}")
        return default
    return raw


def config() -> dict:
    return {
        "max_dimension": _env_number("IMAGE_MAX_DIMENSION", 1600, 640, 4096, int),
        "jpeg_quality": _env_number("IMAGE_JPEG_QUALITY", 0.8, 0.3, 1.0, float),
        "max_upload_bytes": _env_number("IMAGE_MAX_UPLOAD_BYTES", 5 * 1024 * 1024,
                                        100 * 1024, 20 * 1024 * 1024, int),
        "log_level": _env_choice("IMAGE_LOG_LEVEL", "info", {"info", "debug", "off"}),
        "console_log": _env_choice("IMAGE_CONSOLE_LOG", "on", {"on", "off"}) == "on",
        "max_photos": MAX_PHOTOS,
        "accepted_types": list(S.MIME_EXT),
    }


@router.get("/image-leads/config")
def get_config(user=Depends(A.require_member)):
    return config()


# --- helpers -----------------------------------------------------------------

def _bad(code: str, message: str) -> A.ApiError:
    return A.ApiError(400, code, message)


def _iso_or_none(value: str | None) -> str | None:
    if not value or len(value) > 40:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _dimension(value: str) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 20000 else None


def _lead_for(conn, lead_id: str, user, allow_admin: bool):
    """The only way an endpoint gets at a lead: exists, user is on its event,
    and user owns it (or is admin, where allowed). Checked before anything else."""
    lead = conn.execute("SELECT * FROM image_leads WHERE id = %s", (lead_id,)).fetchone()
    if lead is None:
        raise A.ApiError(404, "NOT_FOUND", "No such image lead.")
    member(lead["event_id"], conn, user)
    if lead["captured_by"] != user["id"] and not (allow_admin and user["role"] == "admin"):
        raise A.ApiError(403, "FORBIDDEN", "You can only access image leads you captured.")
    return lead


_PHOTO_COLS = ("id, image_lead_id, slot, mime_type, size_bytes, checksum_sha256, width, height,"
               " compressed, uploaded_at")


def _photo_out(p) -> dict:
    d = dict(p)
    d["content_url"] = f"/api/v1/image-photos/{p['id']}/content"
    return d


def _lead_out(lead, photos: list) -> dict:
    d = {k: lead[k] for k in ("id", "event_id", "client_id", "client_name", "note",
                              "photo_count", "captured_by", "captured_at", "synced_at",
                              "clock_skew_ms", "created_at", "updated_at")}
    if "captured_by_name" in lead:
        d["captured_by_name"] = lead["captured_by_name"]
    d["photos"] = [_photo_out(p) for p in photos]
    d["status"] = "complete" if len(photos) >= lead["photo_count"] else "uploading"
    return d


def _photos_of(conn, lead_ids: list[str]) -> dict[str, list]:
    out: dict[str, list] = {i: [] for i in lead_ids}
    if lead_ids:
        for p in conn.execute(f"SELECT {_PHOTO_COLS} FROM image_lead_photos"
                              " WHERE image_lead_id = ANY(%s) ORDER BY slot", (lead_ids,)):
            out[p["image_lead_id"]].append(p)
    return out


# --- create ------------------------------------------------------------------

class ImageLeadIn(BaseModel):
    id: str
    client_id: str | None = None
    other_name: str | None = None
    note: str | None = None
    photo_count: int
    captured_at: str | None = None
    client_sent_at: str | None = None


@router.post("/events/{event_id}/image-leads")
def create_image_lead(event_id: str, body: ImageLeadIn, user=Depends(A.require_member),
                      conn = Depends(get_db)):
    member(event_id, conn, user)
    if not ID_RE.match(body.id):
        raise _bad("INVALID_ID", "Lead id must be 8-64 letters, digits or dashes.")
    if body.photo_count not in (1, MAX_PHOTOS):
        raise _bad("INVALID_PHOTO_COUNT", "An image lead has 1 or 2 photos.")

    client_id = (body.client_id or "").strip() or None
    other = _CTRL.sub(" ", body.other_name or "").strip()
    if bool(client_id) == bool(other):
        raise _bad("INVALID_CLIENT", "Pick a client or type a name — exactly one of the two.")
    if client_id:
        row = conn.execute("SELECT name FROM clients WHERE id = %s AND event_id = %s",
                           (client_id, event_id)).fetchone()
        if row is None:
            raise _bad("INVALID_CLIENT", "That client is not part of this event.")
        client_name = row["name"]
    else:
        if len(other) > MAX_NAME:
            raise _bad("INVALID_CLIENT", f"The name is longer than {MAX_NAME} characters.")
        client_name = other

    note = (body.note or "").replace("\x00", "").strip() or None
    if note and len(note) > MAX_NOTE:
        raise _bad("INVALID_NOTE", f"The note is longer than {MAX_NOTE} characters.")

    ts = now_iso()
    inserted = conn.execute(
        "INSERT INTO image_leads (id, event_id, client_id, client_name, note, photo_count,"
        " captured_by, captured_at, synced_at, clock_skew_ms, created_at, updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING RETURNING id",
        (body.id, event_id, client_id, client_name, note, body.photo_count, user["id"],
         _iso_or_none(body.captured_at) or ts, ts, _skew_ms(body.client_sent_at), ts, ts),
    ).fetchone()

    lead = conn.execute("SELECT * FROM image_leads WHERE id = %s", (body.id,)).fetchone()
    if lead["captured_by"] != user["id"] or lead["event_id"] != event_id:
        raise A.ApiError(409, "ID_CONFLICT", "That id is already in use.")
    if not inserted and lead["photo_count"] != body.photo_count:
        raise A.ApiError(409, "LEAD_LOCKED", "This image lead was already uploaded differently.")

    if inserted:
        audit(conn, user["id"], "image_lead.create", "image_lead", body.id,
              {"photo_count": body.photo_count, "other": client_id is None})
        _log(logging.INFO, "SERVER", body.id,
             f"created photos={body.photo_count} client={'picked' if client_id else 'other'}"
             f" user={_short(user['id'])}")
    else:
        _log(logging.DEBUG, "SERVER", body.id, "create replayed (already known)")

    return {**_lead_out(lead, _photos_of(conn, [body.id])[body.id]),
            "replayed": inserted is None}


# --- photo upload ------------------------------------------------------------

@router.put("/image-leads/{lead_id}/photos/{slot}")
def upload_photo(lead_id: str, slot: int, file: UploadFile = File(...),
                 photo_id: str = Form(...), sha256: str = Form(...),
                 width: str = Form(default=""), height: str = Form(default=""),
                 compressed: str = Form(default="1"),
                 user=Depends(A.require_member), conn = Depends(get_db)):
    if slot not in (1, MAX_PHOTOS):
        raise _bad("INVALID_SLOT", "Slot must be 1 or 2.")
    lead = _lead_for(conn, lead_id, user, allow_admin=False)
    if slot > lead["photo_count"]:
        raise _bad("INVALID_SLOT", f"This image lead has {lead['photo_count']} photo(s).")
    if not ID_RE.match(photo_id):
        raise _bad("INVALID_ID", "Photo id must be 8-64 letters, digits or dashes.")
    claimed = sha256.strip().lower()
    if not SHA_RE.match(claimed):
        raise _bad("INVALID_CHECKSUM", "sha256 must be 64 hex characters.")

    photo = f"{slot}/{lead['photo_count']}"
    limit = config()["max_upload_bytes"]
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise A.ApiError(413, "TOO_LARGE", f"Photos are limited to {limit // 1024} KB.")
    try:
        mime = S.sniff_image(data)
    except S.RejectedImage as e:
        _log(logging.WARNING, "SERVER", lead_id, "rejected: not an accepted image type", photo)
        raise _bad("REJECTED_UPLOAD", str(e))

    actual = hashlib.sha256(data).hexdigest()
    if actual != claimed:
        _log(logging.WARNING, "SERVER", lead_id,
             f"WARNING fingerprint mismatch: rejected user={_short(user['id'])}", photo)
        raise _bad("CHECKSUM_MISMATCH",
                   "The photo changed or was damaged on the way. It will be retried.")

    existing = conn.execute(f"SELECT {_PHOTO_COLS} FROM image_lead_photos"
                            " WHERE image_lead_id = %s AND slot = %s", (lead_id, slot)).fetchone()
    if existing:
        if existing["checksum_sha256"] == actual and existing["id"] == photo_id:
            _log(logging.DEBUG, "SERVER", lead_id, "upload replayed (already stored)", photo)
            return {"photo": _photo_out(existing), "replayed": True}
        raise A.ApiError(409, "SLOT_LOCKED", "This photo slot is already uploaded and locked.")
    if conn.execute("SELECT 1 FROM image_lead_photos WHERE id = %s", (photo_id,)).fetchone():
        raise A.ApiError(409, "ID_CONFLICT", "That photo id is already in use.")

    # The fingerprint is in the key, so a lost race for the slot can never
    # overwrite the winner's file.
    key = f"{lead['event_id']}/{lead_id}/{slot}-{actual[:16]}.{S.MIME_EXT[mime]}"
    try:
        S.put(key, data, mime)
    except S.StorageNotConfigured:
        raise A.ApiError(503, "STORAGE_UNAVAILABLE", "Photo storage is not configured.")
    except (RuntimeError, httpx.HTTPError):
        _log(logging.WARNING, "SERVER", lead_id, "storage upload failed", photo)
        raise A.ApiError(502, "STORAGE_ERROR", "Could not store the photo. It will be retried.")

    ts = now_iso()
    conn.execute("SAVEPOINT photo")
    try:
        conn.execute(
            "INSERT INTO image_lead_photos (id, image_lead_id, slot, storage_key, mime_type,"
            " size_bytes, checksum_sha256, width, height, compressed, uploaded_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (photo_id, lead_id, slot, key, mime, len(data), actual, _dimension(width),
             _dimension(height), compressed.strip().lower() in ("1", "true", "yes"), ts))
        conn.execute("RELEASE SAVEPOINT photo")
    except psycopg2.errors.UniqueViolation:
        conn.execute("ROLLBACK TO SAVEPOINT photo")
        winner = conn.execute("SELECT storage_key FROM image_lead_photos"
                              " WHERE image_lead_id = %s AND slot = %s",
                              (lead_id, slot)).fetchone()
        if winner is None or winner["storage_key"] != key:
            try:
                S.delete_many([key])
            except Exception:
                _log(logging.WARNING, "SERVER", lead_id, "orphan cleanup failed", photo)
        raise A.ApiError(409, "SLOT_LOCKED", "This photo slot is already uploaded and locked.")

    conn.execute("UPDATE image_leads SET updated_at = %s WHERE id = %s", (ts, lead_id))
    row = conn.execute(f"SELECT {_PHOTO_COLS} FROM image_lead_photos WHERE id = %s",
                       (photo_id,)).fetchone()
    received = conn.execute("SELECT COUNT(*) AS n FROM image_lead_photos"
                            " WHERE image_lead_id = %s", (lead_id,)).fetchone()["n"]
    _log(logging.INFO, "SERVER", lead_id,
         f"verified: {mime.split('/')[1]}, {len(data) // 1024} KB, fingerprint OK,"
         f" compressed={'yes' if row['compressed'] else 'no'}, stored in bucket,"
         f" user={_short(user['id'])}", photo)
    return {"photo": _photo_out(row), "replayed": False,
            "status": "complete" if received >= lead["photo_count"] else "uploading"}


# --- device logs -------------------------------------------------------------

class DeviceLog(BaseModel):
    at: str | None = None
    photo: int | None = None
    stage: str
    pct: int | None = None
    msg: str = ""


class DeviceLogsIn(BaseModel):
    entries: list[DeviceLog] = Field(max_length=100)


@router.post("/image-leads/{lead_id}/logs")
def device_logs(lead_id: str, body: DeviceLogsIn, user=Depends(A.require_member),
                conn = Depends(get_db)):
    """The phone's compression/upload report. Printed, labelled DEVICE, and never
    trusted for anything. Control characters are stripped so a phone cannot
    inject lines that look like the server's own."""
    lead = _lead_for(conn, lead_id, user, allow_admin=False)
    accepted = 0
    for e in body.entries:
        if e.stage not in DEVICE_STAGES or (e.pct is not None and e.pct not in DEVICE_PCTS):
            continue
        photo = (f"{e.photo}/{lead['photo_count']}"
                 if e.photo is not None and 1 <= e.photo <= lead["photo_count"] else "")
        pct = f"{e.pct:>3}% " if e.pct is not None else ""
        at = _CTRL.sub("", e.at or "")[:40] or "-"
        text = f"device_at={at}  {pct}{e.stage} {_CTRL.sub(' ', e.msg)[:200].strip()}"
        _log(logging.WARNING if e.stage == "error" else logging.INFO, "DEVICE", lead_id,
             text, photo)
        accepted += 1
    return {"accepted": accepted, "dropped": len(body.entries) - accepted}


# --- read --------------------------------------------------------------------

@router.get("/events/{event_id}/image-leads")
def list_image_leads(event_id: str, user=Depends(A.require_member), conn = Depends(get_db)):
    """Floor users get their own only; admin gets everyone's. Enforced here."""
    member(event_id, conn, user)
    sql = ("SELECT l.*, u.full_name AS captured_by_name FROM image_leads l"
           " LEFT JOIN users u ON u.id = l.captured_by WHERE l.event_id = %s")
    params: list = [event_id]
    if user["role"] != "admin":
        sql += " AND l.captured_by = %s"
        params.append(user["id"])
    leads = conn.execute(sql + " ORDER BY l.captured_at DESC", params).fetchall()
    photos = _photos_of(conn, [l["id"] for l in leads])
    return {"image_leads": [_lead_out(l, photos[l["id"]]) for l in leads]}


@router.get("/image-photos/{photo_id}/content")
def photo_content(photo_id: str, user=Depends(A.require_member), conn = Depends(get_db)):
    row = conn.execute("SELECT image_lead_id, storage_key FROM image_lead_photos WHERE id = %s",
                       (photo_id,)).fetchone()
    if row is None:
        raise A.ApiError(404, "NOT_FOUND", "No such photo.")
    # Authorisation first; a signed link is only ever created after this passes.
    _lead_for(conn, row["image_lead_id"], user, allow_admin=True)
    try:
        url = S.signed_url(row["storage_key"])
    except FileNotFoundError:
        raise A.ApiError(410, "GONE", "The stored photo is missing.")
    except S.StorageNotConfigured:
        raise A.ApiError(503, "STORAGE_UNAVAILABLE", "Photo storage is not configured.")
    except (RuntimeError, httpx.HTTPError):
        raise A.ApiError(502, "STORAGE_ERROR", "Could not reach photo storage.")
    return RedirectResponse(url, status_code=307, headers={"Cache-Control": "no-store"})


# --- admin: delete + export --------------------------------------------------

@router.delete("/image-leads/{lead_id}")
def delete_image_lead(lead_id: str, user=Depends(A.require_admin), conn = Depends(get_db)):
    delete_lead(conn, lead_id, user)
    return {"ok": True}


def delete_lead(conn, lead_id: str, user) -> int:
    """Rows are deleted first but not committed; files next; commit only once
    the files are gone. A storage failure raises before the commit, so the
    caller's rollback keeps the lead visible and the delete can be retried.
    Returns the number of photos removed."""
    lead = _lead_for(conn, lead_id, user, allow_admin=True)
    keys = [r["storage_key"] for r in conn.execute(
        "SELECT storage_key FROM image_lead_photos WHERE image_lead_id = %s", (lead_id,))]
    conn.execute("DELETE FROM image_leads WHERE id = %s", (lead_id,))
    audit(conn, user["id"], "image_lead.delete", "image_lead", lead_id, {"photos": len(keys)})
    try:
        S.delete_many(keys)
    except S.StorageNotConfigured:
        raise A.ApiError(503, "STORAGE_UNAVAILABLE", "Photo storage is not configured.")
    except (RuntimeError, httpx.HTTPError):
        _log(logging.WARNING, "SERVER", lead_id, "delete failed at storage; rolled back")
        raise A.ApiError(502, "STORAGE_ERROR", "Could not delete the photos. Nothing was removed.")
    conn.commit()
    _log(logging.INFO, "SERVER", lead_id,
         f"deleted with {len(keys)} photo(s) by admin={_short(user['id'])}"
         f" event={_short(lead['event_id'])}")
    return len(keys)


@router.get("/events/{event_id}/image-leads/export.csv")
def export_image_leads(event_id: str, request: Request, user=Depends(A.require_admin),
                       conn = Depends(get_db)):
    member(event_id, conn, user)
    # Links point at this app, not Supabase: they re-check sign-in on every click
    # and never expire. PUBLIC_BASE_URL overrides when a proxy hides the real scheme.
    base = (os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)).rstrip("/")
    leads = conn.execute(
        "SELECT l.*, u.full_name AS captured_by_name FROM image_leads l"
        " LEFT JOIN users u ON u.id = l.captured_by"
        " WHERE l.event_id = %s ORDER BY l.captured_at", (event_id,)).fetchall()
    photos = _photos_of(conn, [l["id"] for l in leads])

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Captured", "Client", "Existing client", "Note", "Captured by",
                "Photos", "Photo 1", "Photo 2", "Synced"])
    for l in leads:
        by_slot = {p["slot"]: f"{base}/api/v1/image-photos/{p['id']}/content"
                   for p in photos[l["id"]]}
        w.writerow([_csv_safe(v) for v in (
            l["captured_at"], l["client_name"], "yes" if l["client_id"] else "no", l["note"],
            l["captured_by_name"], f"{len(photos[l['id']])}/{l['photo_count']}",
            by_slot.get(1, ""), by_slot.get(2, ""), l["synced_at"])])
    audit(conn, user["id"], "image_lead.export", "event", event_id, {"rows": len(leads)})
    return StreamingResponse(
        iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="fbspl-image-leads-{event_id[:8]}.csv"'})
