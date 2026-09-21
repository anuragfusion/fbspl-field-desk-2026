"""FBSPL Field Desk — FastAPI app."""

import os
import sqlite3

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import admin, api
from . import auth as A
from .db import BASE_DIR, audit, db, new_id, get_db, init_db, now_iso

STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="FBSPL Field Desk", docs_url="/api/docs", openapi_url="/api/openapi.json")


def _seed_admin_from_env() -> None:
    """First boot on a fresh (e.g. Render free-tier ephemeral) DB has no admin
    and no shell access to run app.cli. If ADMIN_USERNAME/ADMIN_PASSWORD are
    set, create the admin once; otherwise this is a no-op."""
    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    if not username or not password:
        return
    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role='admin' AND disabled_at IS NULL").fetchone():
            return
        uid = new_id()
        conn.execute(
            "INSERT INTO users (id, username, full_name, role, password_hash,"
            " must_change_password, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, username, username, "admin", A.hash_password(password),
             0, now_iso(), now_iso()))
        audit(conn, None, "user.create", "user", uid, {"username": username, "role": "admin"})


def _seed_event_from_env() -> None:
    """Same rationale as _seed_admin_from_env: free-tier has no shell to run
    app.cli create-event. If EVENT_NAME is set and no event exists yet,
    create one and add every admin as an organiser."""
    name = os.environ.get("EVENT_NAME")
    if not name:
        return
    with db() as conn:
        if conn.execute("SELECT 1 FROM events LIMIT 1").fetchone():
            return
        eid = new_id()
        conn.execute(
            "INSERT INTO events (id, name, venue, city, country, starts_on, ends_on, timezone)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (eid, name, os.environ.get("EVENT_VENUE", ""), os.environ.get("EVENT_CITY", ""),
             os.environ.get("EVENT_COUNTRY", ""), os.environ.get("EVENT_STARTS", ""),
             os.environ.get("EVENT_ENDS", ""), os.environ.get("EVENT_TIMEZONE", "UTC")))
        for u in conn.execute("SELECT id FROM users WHERE role='admin'").fetchall():
            conn.execute(
                "INSERT INTO event_members (id, event_id, user_id, event_role, added_at)"
                " VALUES (?,?,?,?,?)", (new_id(), eid, u["id"], "organiser", now_iso()))
        audit(conn, None, "event.create", "event", eid, {"name": name})


@app.on_event("startup")
def _startup() -> None:
    init_db()
    _seed_admin_from_env()
    _seed_event_from_env()


@app.exception_handler(A.ApiError)
async def _api_error(request: Request, exc: A.ApiError):
    """§10: one error envelope everywhere."""
    return JSONResponse(status_code=exc.status,
                        content={"error": {"code": exc.code, "message": exc.message}})


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else ""))


def _set_session_cookie(response: Response, request: Request, token: str, remember: bool) -> None:
    response.set_cookie(
        A.COOKIE_NAME, token,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=int((A.SESSION_LONG if remember else A.SESSION_SHORT).total_seconds()),
        path="/",
    )


# --- auth (§10.1) ------------------------------------------------------------

class LoginIn(BaseModel):
    username: str
    password: str
    remember: bool = False
    device_label: str = ""


@app.post("/api/v1/auth/login")
def login(body: LoginIn, request: Request, response: Response,
          conn: sqlite3.Connection = Depends(get_db)):
    user = A.authenticate(conn, body.username, body.password, _client_ip(request))
    token, expires_at = A.create_session(
        conn, user["id"], body.remember,
        request.headers.get("user-agent", ""), body.device_label)
    audit(conn, user["id"], "login.ok", "user", user["id"], {}, _client_ip(request))
    _set_session_cookie(response, request, token, body.remember)
    return {
        "token": token,
        "expires_at": expires_at,
        "user": {"id": user["id"], "username": user["username"], "full_name": user["full_name"],
                 "role": user["role"], "must_change_password": bool(user["must_change_password"])},
    }


@app.post("/api/v1/auth/logout")
def logout(request: Request, response: Response, user=Depends(A.current_user),
           conn: sqlite3.Connection = Depends(get_db)):
    A.revoke_session_by_token(conn, request.state.token)
    response.delete_cookie(A.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/v1/auth/me")
def me(user=Depends(A.current_user), conn: sqlite3.Connection = Depends(get_db)):
    events = conn.execute(
        "SELECT e.id, e.name, e.venue, e.starts_on, e.ends_on, e.timezone, e.version,"
        " m.event_role FROM events e JOIN event_members m ON m.event_id = e.id"
        " WHERE m.user_id = ? AND e.archived_at IS NULL ORDER BY e.starts_on",
        (user["id"],),
    ).fetchall()
    return {
        "id": user["id"], "username": user["username"], "full_name": user["full_name"],
        "role": user["role"], "must_change_password": bool(user["must_change_password"]),
        "events": [dict(e) for e in events],
    }


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


@app.post("/api/v1/auth/change-password")
def change_password(body: ChangePasswordIn, request: Request, user=Depends(A.current_user),
                    conn: sqlite3.Connection = Depends(get_db)):
    # §10.1: always requires the current password, even for admins.
    if not A.verify_password(user["password_hash"], body.current_password):
        raise A.ApiError(400, "INVALID_CREDENTIALS", "Your current password is incorrect.")
    conn.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ?"
        " WHERE id = ?", (A.hash_password(body.new_password), now_iso(), user["id"]))
    # §12.2: keeps this device signed in, signs out everywhere else.
    n = A.revoke_all_sessions(conn, user["id"], except_token=request.state.token)
    audit(conn, user["id"], "user.change_password", "user", user["id"],
          {"sessions_revoked": n}, _client_ip(request))
    return {"ok": True, "other_sessions_revoked": n}


@app.post("/api/v1/auth/forgot-password", status_code=501)
@app.post("/api/v1/auth/reset-password", status_code=501)
def no_self_service_reset():
    """Deliberately not implemented. Passwords are set by an admin — there is no
    mail server in this deployment, so a token flow would be a stub that looks
    like a feature. Say so plainly instead."""
    raise A.ApiError(501, "NOT_IMPLEMENTED",
                     "Password resets are done by an administrator. Contact whoever "
                     "publishes the briefing pack.")


# --- service worker must be served from root scope ---------------------------

@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """A service worker can only control paths at or below its own URL, so this
    cannot live under /static. no-cache or a shell update takes a day to land."""
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/app")


@app.get("/app", include_in_schema=False)
def app_shell():
    """The floor UI. Cached by the service worker and served for navigations to
    / and /app, so a cold launch in airplane mode still boots. The worker must
    not answer other navigations with it — /admin is a server-rendered page."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/v1/version", include_in_schema=False)
def version():
    """The shell has no content hashes (no build step), so the app compares its
    own SHELL_VERSION against this and nags when they drift."""
    return {"shell_version": 7, "time": now_iso()}


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "time": now_iso()}


app.include_router(api.router)
app.include_router(admin.router)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
