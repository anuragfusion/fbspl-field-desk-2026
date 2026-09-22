"""Auth: argon2id, server-side sessions, lockout, role gates.

Sessions are rows, not JWTs. That is deliberate: §9.12 wants a lost phone at a
conference revoked within seconds, and one DELETE does it. Token-family rotation
would buy nothing here and cost a subsystem.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import Cookie, Depends, Header, Request

from .db import audit, get_db, new_id, now_iso

ph = PasswordHasher()

COOKIE_NAME = "fd_session"
LOCKOUT_THRESHOLD = 10          # §12.1
LOCKOUT_WINDOW = timedelta(minutes=15)
LOCKOUT_DURATION = timedelta(minutes=15)
SESSION_SHORT = timedelta(hours=12)     # "remember me" unticked
SESSION_LONG = timedelta(days=30)       # ticked

# Verified against on unknown usernames so login costs the same either way.
# Without this, response time leaks which accounts exist (§12.1).
_DUMMY_HASH = ph.hash("dummy-password-for-constant-time-login")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- passwords ---------------------------------------------------------------

def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        ph.verify(stored_hash or _DUMMY_HASH, password)
        return stored_hash is not None
    except (VerifyMismatchError, VerificationError):
        return False


# --- sessions ----------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn, user_id: str, remember: bool, user_agent: str = "",
                   device_label: str = "") -> tuple[str, str]:
    """Returns (raw_token, expires_at). Only the hash is stored."""
    token = secrets.token_urlsafe(32)
    expires = _utcnow() + (SESSION_LONG if remember else SESSION_SHORT)
    expires_at = expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn.execute(
        "INSERT INTO sessions (id, user_id, token_hash, device_label, user_agent,"
        " issued_at, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (new_id(), user_id, _token_hash(token), device_label, user_agent[:300],
         now_iso(), expires_at),
    )
    return token, expires_at


def revoke_session_by_token(conn, token: str) -> None:
    conn.execute("UPDATE sessions SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL",
                 (now_iso(), _token_hash(token)))


def revoke_all_sessions(conn, user_id: str, except_token: str | None = None) -> int:
    """§12.2: an admin password reset revokes the user's sessions; changing your own
    password signs you out everywhere else but keeps the current device."""
    sql = "UPDATE sessions SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL"
    params: list = [now_iso(), user_id]
    if except_token:
        sql += " AND token_hash != %s"
        params.append(_token_hash(except_token))
    return conn.execute(sql, params).rowcount


def session_user(conn, token: str | None):
    """Resolve a token to a live user, or None. A disabled or deleted account
    drops out here, which is how §12.1's 'sessions stop working' is satisfied."""
    if not token:
        return None
    row = conn.execute(
        "SELECT u.*, s.token_hash AS _tok FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > %s",
        (_token_hash(token), now_iso()),
    ).fetchone()
    if row is None or row["disabled_at"]:
        return None
    return row


# --- authentication ----------------------------------------------------------

def authenticate(conn, username: str, password: str, ip: str = ""):
    """Raises ApiError on any failure. Returns the user row on success."""
    row = conn.execute(
        "SELECT * FROM users WHERE username = %s"
        " OR (email IS NOT NULL AND email = %s)",
        (username.strip(), username.strip()),
    ).fetchone()

    if row is None:
        # Burn the same CPU as a real verify so timing does not leak existence.
        verify_password(None, password)
        audit(conn, None, "login.failed", "user", None, {"username": username}, ip)
        conn.commit()          # see _commit_then_raise note below
        raise ApiError(401, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    locked_until = _parse(row["locked_until"])
    if locked_until and locked_until > _utcnow():
        # §12.1: locked accounts return 423 regardless of whether the password is right.
        verify_password(None, password)
        raise ApiError(423, "ACCOUNT_LOCKED",
                       "Too many failed attempts. Try again in a few minutes.")

    if row["disabled_at"]:
        verify_password(None, password)
        audit(conn, None, "login.disabled", "user", row["id"], {}, ip)
        conn.commit()
        raise ApiError(401, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    if not verify_password(row["password_hash"], password):
        # Consecutive failures only count inside the window; an old stray failure
        # should not contribute to locking someone out days later.
        last = _parse(row["last_failed_at"])
        attempts = row["failed_attempts"] + 1 if last and _utcnow() - last < LOCKOUT_WINDOW else 1
        lock = (_utcnow() + LOCKOUT_DURATION).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z") if attempts >= LOCKOUT_THRESHOLD else None
        conn.execute(
            "UPDATE users SET failed_attempts = %s, last_failed_at = %s, locked_until = %s"
            " WHERE id = %s", (attempts, now_iso(), lock, row["id"]))
        audit(conn, None, "login.failed", "user", row["id"], {"attempts": attempts}, ip)
        # The failure counter and its audit row are their own unit of work. The request
        # is about to fail, and the request-scoped connection rolls back on exception —
        # without this commit the counter resets every time and lockout never triggers.
        conn.commit()
        raise ApiError(401, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    conn.execute(
        "UPDATE users SET failed_attempts = 0, last_failed_at = NULL, locked_until = NULL,"
        " last_login_at = %s WHERE id = %s", (now_iso(), row["id"]))
    return row


# --- FastAPI dependencies ----------------------------------------------------

def _token_from(cookie: str | None, authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return cookie


def current_user(
    request: Request,
    conn = Depends(get_db),
    fd_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
    authorization: str | None = Header(default=None),
):
    token = _token_from(fd_session, authorization)
    user = session_user(conn, token)
    if user is None:
        raise ApiError(401, "NOT_AUTHENTICATED", "Sign in to continue.")
    request.state.token = token
    return user


def require(*roles: str):
    """Server-side role gate. §10.4: the client hiding a button is never the control."""
    def dep(user=Depends(current_user)):
        if user["role"] not in roles:
            raise ApiError(403, "FORBIDDEN", "Your account does not have access to this.")
        return user
    return dep


require_admin = require("admin")
require_member = require("admin", "field")
