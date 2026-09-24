"""FBSPL Field Desk — FastAPI app."""

import hashlib
import json
import os

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import admin, api, image_leads, image_storage, storage
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
            " must_change_password, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (uid, username, username, "admin", A.hash_password(password),
             False, now_iso(), now_iso()))
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
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (eid, name, os.environ.get("EVENT_VENUE", ""), os.environ.get("EVENT_CITY", ""),
             os.environ.get("EVENT_COUNTRY", ""), os.environ.get("EVENT_STARTS", ""),
             os.environ.get("EVENT_ENDS", ""), os.environ.get("EVENT_TIMEZONE", "UTC")))
        for u in conn.execute("SELECT id FROM users WHERE role='admin'").fetchall():
            conn.execute(
                "INSERT INTO event_members (id, event_id, user_id, event_role, added_at)"
                " VALUES (%s,%s,%s,%s,%s)", (new_id(), eid, u["id"], "organiser", now_iso()))
        audit(conn, None, "event.create", "event", eid, {"name": name})


@app.on_event("startup")
def _startup() -> None:
    init_db()
    _seed_admin_from_env()
    _seed_event_from_env()
    try:
        storage.ensure_bucket()
    except storage.StorageNotConfigured:
        # Document upload/download will 500 until these are set, but the rest of
        # the app (sync, leads, admin) doesn't depend on them — don't block boot.
        pass
    try:
        image_storage.ensure_bucket()
    except image_storage.StorageNotConfigured:
        pass
    except Exception as e:
        # A new feature's bucket must never stop the existing app from booting.
        image_leads.log.warning("[image-lead] SERVER images bucket not verified: %s", e)


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
          conn = Depends(get_db)):
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
           conn = Depends(get_db)):
    A.revoke_session_by_token(conn, request.state.token)
    response.delete_cookie(A.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/v1/auth/me")
def me(user=Depends(A.current_user), conn = Depends(get_db)):
    events = conn.execute(
        "SELECT e.id, e.name, e.venue, e.starts_on, e.ends_on, e.timezone, e.version,"
        " m.event_role FROM events e JOIN event_members m ON m.event_id = e.id"
        " WHERE m.user_id = %s AND e.archived_at IS NULL ORDER BY e.starts_on",
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
                    conn = Depends(get_db)):
    # §10.1: always requires the current password, even for admins.
    if not A.verify_password(user["password_hash"], body.current_password):
        raise A.ApiError(400, "INVALID_CREDENTIALS", "Your current password is incorrect.")
    conn.execute(
        "UPDATE users SET password_hash = %s, must_change_password = FALSE, updated_at = %s"
        " WHERE id = %s", (A.hash_password(body.new_password), now_iso(), user["id"]))
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

# sw.js is the worker itself, selftest.html is a dev page, index.html is served as /app.
SHELL_EXCLUDE = {"sw.js", "selftest.html", "index.html"}
SW_VERSION_SLOT = "'__SHELL_VERSION__'"
SW_URLS_SLOT = "[/* __SHELL_URLS__ */]"


def shell_files():
    return sorted(
        p for p in STATIC_DIR.rglob("*")
        if p.is_file()
        and p.name not in SHELL_EXCLUDE
        and not any(part.startswith(".") for part in p.relative_to(STATIC_DIR).parts))


def shell_urls():
    return ["/app"] + [f"/static/{p.relative_to(STATIC_DIR).as_posix()}" for p in shell_files()]


def shell_version():
    """Fingerprint of every file a phone caches, so any change in static/ ships
    a new worker and a fresh cache without anyone bumping a number."""
    h = hashlib.sha256()
    for p in [STATIC_DIR / "index.html", STATIC_DIR / "sw.js", *shell_files()]:
        h.update(p.relative_to(STATIC_DIR).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:16]


def render_service_worker():
    src = (STATIC_DIR / "sw.js").read_text()
    # A worker with an empty file list installs fine and then breaks offline
    # silently, so a missing placeholder must fail loudly instead.
    for slot in (SW_VERSION_SLOT, SW_URLS_SLOT):
        if src.count(slot) != 1:
            raise RuntimeError(f"static/sw.js must contain {slot} exactly once")
    return (src.replace(SW_VERSION_SLOT, json.dumps(shell_version()))
               .replace(SW_URLS_SLOT, json.dumps(shell_urls())))


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    """A service worker can only control paths at or below its own URL, so this
    cannot live under /static. no-cache or a shell update takes a day to land."""
    return Response(render_service_worker(), media_type="application/javascript",
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
    """Same fingerprint the served sw.js carries."""
    return {"shell_version": shell_version(), "time": now_iso()}


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "time": now_iso()}


app.include_router(api.router)
app.include_router(image_leads.router)
app.include_router(admin.router)

class RevalidatedStaticFiles(StaticFiles):
    """no-cache (keep a copy, but check the ETag before using it), not no-store.
    The service worker fetches the shell with cache:'reload' anyway; this covers
    a browser with no worker running, which would otherwise keep a stale app.js
    from its heuristic HTTP cache for days after a deploy."""

    def file_response(self, *args, **kwargs):
        res = super().file_response(*args, **kwargs)
        res.headers["Cache-Control"] = "no-cache"
        return res


STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")
