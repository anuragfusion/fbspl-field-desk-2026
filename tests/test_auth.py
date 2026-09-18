"""§12.1 authentication and §12.2 account-management criteria.

    .venv/bin/python -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="fielddesk-test-")
os.environ["FIELDDESK_DB"] = str(Path(_TMP) / "test.db")
os.environ["FIELDDESK_STORAGE"] = str(Path(_TMP) / "storage")

from fastapi.testclient import TestClient          # noqa: E402

from app import auth as A                          # noqa: E402
from app.db import db, init_db, new_id, now_iso    # noqa: E402
from app.main import app                           # noqa: E402


def iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_user(username, password, role="field", disabled=False):
    with db() as conn:
        uid = new_id()
        conn.execute(
            "INSERT INTO users (id, username, full_name, role, password_hash,"
            " disabled_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, username, username.title(), role, A.hash_password(password),
             now_iso() if disabled else None, now_iso(), now_iso()))
        return uid


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.client = TestClient(app)
        with db() as conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM audit_log")
            conn.execute("DELETE FROM users")

    def login(self, username, password, remember=False):
        return self.client.post("/api/v1/auth/login", json={
            "username": username, "password": password, "remember": remember})

    # --- §12.1 ---------------------------------------------------------------

    def test_valid_login_returns_token_and_me_reports_role(self):
        make_user("priya", "correct-horse", role="field")
        r = self.login("priya", "correct-horse")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["token"])

        me = self.client.get("/api/v1/auth/me",
                             headers={"Authorization": f"Bearer {r.json()['token']}"})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["role"], "field")

    def test_wrong_password_is_401_and_increments_failed_attempts(self):
        uid = make_user("priya", "correct-horse")
        r = self.login("priya", "wrong")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"]["code"], "INVALID_CREDENTIALS")
        with db() as conn:
            row = conn.execute("SELECT failed_attempts FROM users WHERE id=?", (uid,)).fetchone()
        self.assertEqual(row["failed_attempts"], 1)

    def test_ten_failures_locks_account_even_for_the_right_password(self):
        make_user("priya", "correct-horse")
        for _ in range(A.LOCKOUT_THRESHOLD):
            self.login("priya", "wrong")
        # §12.1: "further attempts return 423 regardless of correctness"
        r = self.login("priya", "correct-horse")
        self.assertEqual(r.status_code, 423)
        self.assertEqual(r.json()["error"]["code"], "ACCOUNT_LOCKED")

    def test_stale_failures_outside_the_window_do_not_accumulate(self):
        uid = make_user("priya", "correct-horse")
        old = iso(datetime.now(timezone.utc) - A.LOCKOUT_WINDOW - timedelta(minutes=1))
        with db() as conn:
            conn.execute("UPDATE users SET failed_attempts=9, last_failed_at=? WHERE id=?",
                         (old, uid))
        self.login("priya", "wrong")
        with db() as conn:
            row = conn.execute("SELECT failed_attempts FROM users WHERE id=?", (uid,)).fetchone()
        self.assertEqual(row["failed_attempts"], 1, "counter should restart outside the window")

    def test_unknown_username_is_indistinguishable_from_a_wrong_password(self):
        make_user("priya", "correct-horse")
        a = self.login("priya", "wrong").json()["error"]
        b = self.login("nobody-at-all", "wrong").json()["error"]
        self.assertEqual(a, b, "error body must not reveal whether the account exists")

    def test_disabled_account_cannot_log_in_and_its_session_stops_working(self):
        uid = make_user("priya", "correct-horse")
        token = self.login("priya", "correct-horse").json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=hdr).status_code, 200)

        with db() as conn:
            conn.execute("UPDATE users SET disabled_at=? WHERE id=?", (now_iso(), uid))

        self.assertEqual(self.client.get("/api/v1/auth/me", headers=hdr).status_code, 401)
        self.assertEqual(self.login("priya", "correct-horse").status_code, 401)

    def test_remember_me_controls_session_length(self):
        make_user("priya", "correct-horse")
        short = self.login("priya", "correct-horse", remember=False).json()["expires_at"]
        long_ = self.login("priya", "correct-horse", remember=True).json()["expires_at"]
        now = datetime.now(timezone.utc)
        self.assertLess(datetime.fromisoformat(short.replace("Z", "+00:00")) - now,
                        timedelta(hours=13))
        self.assertGreater(datetime.fromisoformat(long_.replace("Z", "+00:00")) - now,
                           timedelta(days=29))

    def test_expired_session_is_rejected(self):
        make_user("priya", "correct-horse")
        token = self.login("priya", "correct-horse").json()["token"]
        with db() as conn:
            conn.execute("UPDATE sessions SET expires_at=?",
                         (iso(datetime.now(timezone.utc) - timedelta(minutes=1)),))
        self.assertEqual(self.client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_logout_revokes_only_that_session(self):
        make_user("priya", "correct-horse")
        a = self.login("priya", "correct-horse").json()["token"]
        b = self.login("priya", "correct-horse").json()["token"]
        self.client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {a}"})
        self.assertEqual(self.client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {a}"}).status_code, 401)
        self.assertEqual(self.client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {b}"}).status_code, 200)

    # --- §12.2 ---------------------------------------------------------------

    def test_change_password_requires_the_correct_current_password(self):
        make_user("priya", "correct-horse")
        token = self.login("priya", "correct-horse").json()["token"]
        r = self.client.post("/api/v1/auth/change-password",
                             headers={"Authorization": f"Bearer {token}"},
                             json={"current_password": "nope", "new_password": "brand-new-pass"})
        self.assertEqual(r.status_code, 400)
        # ...and nothing changed
        self.assertEqual(self.login("priya", "correct-horse").status_code, 200)

    def test_change_password_keeps_this_device_and_signs_out_the_others(self):
        make_user("priya", "correct-horse")
        keep = self.login("priya", "correct-horse").json()["token"]
        other = self.login("priya", "correct-horse").json()["token"]

        r = self.client.post("/api/v1/auth/change-password",
                             headers={"Authorization": f"Bearer {keep}"},
                             json={"current_password": "correct-horse",
                                   "new_password": "brand-new-pass"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {keep}"}).status_code, 200)
        self.assertEqual(self.client.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {other}"}).status_code, 401)
        self.assertEqual(self.login("priya", "brand-new-pass").status_code, 200)

    def test_every_auth_action_writes_an_audit_row(self):
        make_user("priya", "correct-horse")
        self.login("priya", "wrong")
        self.login("priya", "correct-horse")
        with db() as conn:
            actions = {r["action"] for r in
                       conn.execute("SELECT action FROM audit_log").fetchall()}
        self.assertIn("login.failed", actions)
        self.assertIn("login.ok", actions)

    def test_self_service_reset_is_honestly_unavailable(self):
        r = self.client.post("/api/v1/auth/forgot-password", json={"email": "x@y.z"})
        self.assertEqual(r.status_code, 501)
        self.assertIn("administrator", r.json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
