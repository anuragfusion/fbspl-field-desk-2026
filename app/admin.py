"""Admin UI — server-rendered Jinja2 forms.

Deliberately NOT offline-capable and deliberately not a JSON API. The admin sits
in the office on wifi; plain HTML forms are a native platform feature and cost
nothing to maintain. All the offline machinery lives on the floor side only.
"""

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth as A
from . import image_leads, storage
from .db import BASE_DIR, audit, bump_version, get_db, new_id, now_iso
from .importer import import_workbook
from .phase1 import build_pack

router = APIRouter(prefix="/admin", include_in_schema=False)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def admin_page(request: Request, conn = Depends(get_db)):
    """Like require_admin, but sends a browser to the login page instead of
    handing it a JSON 401 it cannot do anything with."""
    token = request.cookies.get(A.COOKIE_NAME)
    user = A.session_user(conn, token)
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    if user["role"] != "admin":
        raise HTTPException(status_code=303, headers={"Location": "/app"})
    return user


def back(msg: str = "", kind: str = "ok") -> RedirectResponse:
    q = f"?msg={msg}&kind={kind}" if msg else ""
    return RedirectResponse(f"/admin{q}", status_code=303)


# --- login -------------------------------------------------------------------

@router.get("/login")
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request=request, name="login.html",
                                      context={"error": error})


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...),
                 conn = Depends(get_db)):
    try:
        user = A.authenticate(conn, username, password,
                              request.client.host if request.client else "")
    except A.ApiError as e:
        return templates.TemplateResponse(request=request, name="login.html",
                                          context={"error": e.message}, status_code=e.status)
    if user["role"] != "admin":
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "That account is floor-team access. Use the app at /app."},
            status_code=403)

    token, _ = A.create_session(conn, user["id"], True, request.headers.get("user-agent", ""))
    audit(conn, user["id"], "login.ok", "user", user["id"], {"via": "admin"})
    res = RedirectResponse("/admin", status_code=303)
    res.set_cookie(A.COOKIE_NAME, token, httponly=True, samesite="lax",
                   secure=request.url.scheme == "https",
                   max_age=int(A.SESSION_LONG.total_seconds()), path="/")
    return res


@router.get("/logout")
def logout(request: Request, conn = Depends(get_db)):
    token = request.cookies.get(A.COOKIE_NAME)
    if token:
        A.revoke_session_by_token(conn, token)
    res = RedirectResponse("/admin/login", status_code=303)
    res.delete_cookie(A.COOKIE_NAME, path="/")
    return res


# --- dashboard ---------------------------------------------------------------

@router.get("")
def dashboard(request: Request, msg: str = "", kind: str = "ok",
              user=Depends(admin_page), conn = Depends(get_db)):
    event = conn.execute(
        "SELECT e.* FROM events e JOIN event_members m ON m.event_id = e.id"
        " WHERE m.user_id = %s AND e.archived_at IS NULL ORDER BY e.starts_on LIMIT 1",
        (user["id"],)).fetchone()

    ctx = {"user": user, "event": event, "msg": msg, "kind": kind,
           "users": conn.execute("SELECT * FROM users ORDER BY role, username").fetchall(),
           "clients": [], "updates": [], "documents": [], "leads": [], "image_leads": [], "counts": {}}

    if event:
        e = event["id"]
        ctx["clients"] = conn.execute(
            "SELECT id, name, priority, owner FROM clients WHERE event_id = %s"
            " ORDER BY name", (e,)).fetchall()
        ctx["updates"] = conn.execute(
            "SELECT u.*, (SELECT COUNT(*) FROM update_receipts r WHERE r.update_id = u.id"
            " AND r.done_at IS NOT NULL) AS done_count FROM updates u WHERE u.event_id = %s"
            " ORDER BY u.pinned DESC, u.created_at DESC", (e,)).fetchall()
        ctx["documents"] = conn.execute(
            "SELECT d.*, c.name AS client_name FROM documents d"
            " LEFT JOIN clients c ON c.id = d.client_id WHERE d.event_id = %s"
            " ORDER BY d.created_at DESC", (e,)).fetchall()
        ctx["leads"] = conn.execute(
            "SELECT l.*, u.full_name AS by_name, c.name AS client_name FROM leads l"
            " LEFT JOIN users u ON u.id = l.captured_by"
            " LEFT JOIN clients c ON c.id = l.client_id"
            " WHERE l.event_id = %s ORDER BY l.captured_at DESC LIMIT 100", (e,)).fetchall()
        img = conn.execute(
            "SELECT l.*, u.full_name AS by_name FROM image_leads l"
            " LEFT JOIN users u ON u.id = l.captured_by"
            " WHERE l.event_id = %s ORDER BY l.captured_at DESC LIMIT 200", (e,)).fetchall()
        photos = image_leads._photos_of(conn, [l["id"] for l in img])
        ctx["image_leads"] = [{**l, "photos": [image_leads._photo_out(p) for p in photos[l["id"]]]}
                              for l in img]
        ctx["members"] = {r["user_id"] for r in conn.execute(
            "SELECT user_id FROM event_members WHERE event_id = %s", (e,))}
    return templates.TemplateResponse(request=request, name="admin.html", context=ctx)


# --- users -------------------------------------------------------------------

@router.post("/users/new")
def create_user(request: Request, username: str = Form(...), full_name: str = Form(""),
                role: str = Form("field"), password: str = Form(...), event_id: str = Form(""),
                user=Depends(admin_page), conn = Depends(get_db)):
    if len(password) < 8:
        return back("Password must be at least 8 characters", "err")
    if conn.execute("SELECT 1 FROM users WHERE username = %s",
                    (username.strip(),)).fetchone():
        return back(f"Username {username} is already taken", "err")     # §12.2 -> 409 equivalent

    uid = new_id()
    conn.execute(
        "INSERT INTO users (id, username, full_name, role, password_hash, must_change_password,"
        " created_by, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,TRUE,%s,%s,%s)",
        (uid, username.strip(), full_name.strip() or username.strip(), role,
         A.hash_password(password), user["id"], now_iso(), now_iso()))
    if event_id:
        conn.execute("INSERT INTO event_members (id, event_id, user_id, event_role,"
                     " added_at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (event_id, user_id) DO NOTHING",
                     (new_id(), event_id, uid, "attendee", now_iso()))
    audit(conn, user["id"], "user.create", "user", uid, {"username": username, "role": role})
    return back(f"Created {username} — send them the password over Teams, not in the pack")


@router.post("/users/{uid}/password")
def reset_password(uid: str, password: str = Form(...), user=Depends(admin_page),
                   conn = Depends(get_db)):
    if len(password) < 8:
        return back("Password must be at least 8 characters", "err")
    row = conn.execute("SELECT username FROM users WHERE id = %s", (uid,)).fetchone()
    if row is None:
        return back("No such user", "err")
    conn.execute(
        "UPDATE users SET password_hash = %s, must_change_password = TRUE, failed_attempts = 0,"
        " last_failed_at = NULL, locked_until = NULL, updated_at = %s WHERE id = %s",
        (A.hash_password(password), now_iso(), uid))
    n = A.revoke_all_sessions(conn, uid)          # §12.2
    audit(conn, user["id"], "user.reset_password", "user", uid, {"sessions_revoked": n})
    return back(f"Password reset for {row['username']}; {n} session(s) signed out")


@router.post("/users/{uid}/toggle")
def toggle_user(uid: str, user=Depends(admin_page),
                conn = Depends(get_db)):
    row = conn.execute("SELECT * FROM users WHERE id = %s", (uid,)).fetchone()
    if row is None:
        return back("No such user", "err")
    if uid == user["id"]:
        return back("You cannot disable your own account", "err")       # §12.2
    if not row["disabled_at"] and row["role"] == "admin":
        live = conn.execute("SELECT COUNT(*) c FROM users WHERE role='admin'"
                            " AND disabled_at IS NULL").fetchone()["c"]
        if live < 2:
            return back("At least one enabled admin must remain", "err")  # §12.2
    now = None if row["disabled_at"] else now_iso()
    conn.execute("UPDATE users SET disabled_at = %s, updated_at = %s WHERE id = %s",
                 (now, now_iso(), uid))
    if now:
        A.revoke_all_sessions(conn, uid)
    audit(conn, user["id"], "user.disable" if now else "user.enable", "user", uid, {})
    return back(f"{row['username']} {'disabled' if now else 're-enabled'}")


# --- image leads -------------------------------------------------------------

@router.post("/image-leads/{lead_id}/delete")
def delete_image_lead(lead_id: str, user=Depends(admin_page), conn = Depends(get_db)):
    try:
        n = image_leads.delete_lead(conn, lead_id, user)
    except A.ApiError as e:
        # get_db commits on clean exit; without this the rows would go while the files stay.
        conn.rollback()
        return back(e.message, "err")
    return back(f"Image lead deleted — {n} photo(s) removed")


# --- updates -----------------------------------------------------------------

@router.post("/updates/new")
def post_update(event_id: str = Form(...), title: str = Form(...), body: str = Form(""),
                level: str = Form("info"), pinned: str = Form(""), is_action: str = Form(""),
                client_id: str = Form(""), user=Depends(admin_page),
                conn = Depends(get_db)):
    if not title.strip():
        return back("An update needs a headline", "err")
    version = bump_version(conn, event_id)
    conn.execute(
        "INSERT INTO updates (id, event_id, client_id, title, body, level, pinned, is_action,"
        " author_id, version, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (new_id(), event_id, client_id or None, title.strip(), body.strip(), level,
         bool(pinned), bool(is_action), user["id"], version, now_iso()))
    return back("Posted — it reaches devices on their next sync")


@router.post("/updates/{update_id}/delete")
def delete_update(update_id: str, user=Depends(admin_page),
                  conn = Depends(get_db)):
    row = conn.execute("SELECT event_id FROM updates WHERE id = %s", (update_id,)).fetchone()
    if row is None:
        return back("No such update", "err")
    version = bump_version(conn, row["event_id"])
    conn.execute("DELETE FROM updates WHERE id = %s", (update_id,))
    conn.execute(
        "INSERT INTO deleted_rows (entity, entity_id, event_id, version) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (entity, entity_id) DO UPDATE SET"
        " event_id = excluded.event_id, version = excluded.version",
        ("update", update_id, row["event_id"], version))
    return back("Update removed")


# --- documents ---------------------------------------------------------------

@router.post("/documents")
async def upload(event_id: str = Form(...), client_id: str = Form(""), note: str = Form(""),
                 file: UploadFile = File(...), user=Depends(admin_page),
                 conn = Depends(get_db)):
    data = await file.read()
    try:
        mime = storage.sniff(data, file.filename or "upload")
    except storage.RejectedUpload as e:
        return back(str(e), "err")
    digest, size = storage.put_bytes(data)
    version = bump_version(conn, event_id)
    conn.execute(
        "INSERT INTO documents (id, event_id, client_id, filename, mime_type, size_bytes,"
        " checksum_sha256, storage_key, note, uploaded_by, version, created_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (new_id(), event_id, client_id or None, file.filename, mime, size, digest, digest,
         note, user["id"], version, now_iso()))
    return back(f"Uploaded {file.filename} ({size // 1024} KB)")


@router.post("/documents/{doc_id}/delete")
def remove_document(doc_id: str, user=Depends(admin_page),
                    conn = Depends(get_db)):
    row = conn.execute("SELECT event_id, filename FROM documents WHERE id = %s",
                       (doc_id,)).fetchone()
    if row is None:
        return back("No such document", "err")
    version = bump_version(conn, row["event_id"])
    conn.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
    conn.execute(
        "INSERT INTO deleted_rows (entity, entity_id, event_id, version) VALUES (%s,%s,%s,%s)"
        " ON CONFLICT (entity, entity_id) DO UPDATE SET"
        " event_id = excluded.event_id, version = excluded.version",
        ("document", doc_id, row["event_id"], version))
    audit(conn, user["id"], "document.delete", "document", doc_id, {})
    return back(f"Removed {row['filename']} — devices drop it on next sync")


# --- workbook import (two-step, §10.5) ---------------------------------------

@router.post("/import")
async def workbook(request: Request, event_id: str = Form(...), commit: str = Form(""),
                   file: UploadFile = File(...), user=Depends(admin_page),
                   conn = Depends(get_db)):
    """Preview first, always. An import that silently rewrites 300 client briefs
    the night before an event is a bad afternoon."""
    suffix = Path(file.filename or "book.xlsx").suffix or ".xlsx"
    tmp = Path(tempfile.mkdtemp()) / f"upload{suffix}"
    with open(tmp, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    try:
        result = import_workbook(conn, event_id, tmp, commit=bool(commit))
    except Exception as e:
        return back(f"Could not read that workbook: {e}", "err")
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)

    if commit:
        audit(conn, user["id"], "workbook.import", "event", event_id, dict(result))
        return back(f"Imported — {result['new']} new, {result['updated']} updated, "
                    f"{result['unchanged']} unchanged (version {result['version']})")
    unmapped = ", ".join(str(c) for c in result["unmapped_columns"][:6])
    return back(f"PREVIEW of '{result['sheet']}': {result['new']} new, "
                f"{result['updated']} updated, {result['unchanged']} unchanged. "
                + (f"Unmapped columns ignored: {unmapped}. " if unmapped else "")
                + "Nothing written — tick Apply and upload again to commit.", "warn")


# --- pack export -------------------------------------------------------------

@router.post("/export-pack")
def export_pack(event_id: str = Form(...), user=Depends(admin_page),
                conn = Depends(get_db)):
    pack, creds = build_pack(conn, event_id, packed_by=user["full_name"])
    listing = " · ".join(f"{c['username']}: {c['temporary_password']}" for c in creds)
    return back(f"Pack ready — {len(pack['clients'])} clients, {len(pack['files'])} files. "
                f"Download it from the link below. Phase 1 passwords — {listing}", "warn")
