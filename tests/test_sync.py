"""§12.3 sync/outbox and §12.6 lead rules — the server half.

The client half (IndexedDB replace, service worker) is covered by
tests/sync_core.test.js and static/selftest.html.
"""

import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="fielddesk-sync-")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/postgres")

import httpx                                                     # noqa: E402
from fastapi.testclient import TestClient                       # noqa: E402

from app import auth as A                                       # noqa: E402
from app.db import bump_version, db, init_db, new_id, now_iso   # noqa: E402
from app.main import app                                        # noqa: E402

PDF = b"%PDF-1.4\n% tiny but genuinely a pdf\n"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.client = TestClient(app)
        with db() as conn:
            for t in ("op_log", "deleted_rows", "update_receipts", "leads", "updates",
                      "meetings", "documents", "client_pocs", "clients",
                      "event_members", "events", "sessions", "audit_log", "users"):
                conn.execute(f"DELETE FROM {t}")
            self.event_id = new_id()
            conn.execute("INSERT INTO events (id, name) VALUES (%s,%s)",
                         (self.event_id, "Applied Net 2026"))
            self.admin = self._user(conn, "admin", "admin")
            self.priya = self._user(conn, "priya", "field")
            self.ankit = self._user(conn, "ankit", "field")
        self.h_admin = self._auth("admin")
        self.h_priya = self._auth("priya")
        self.h_ankit = self._auth("ankit")
        # Logging in set a session cookie on the client's jar. Drop it, so every
        # assertion below is authenticated by its explicit header and nothing else —
        # otherwise a "no credentials" test silently rides the last login.
        self.client.cookies.clear()

    def _user(self, conn, username, role, member=True):
        uid = new_id()
        conn.execute("INSERT INTO users (id, username, full_name, role, password_hash,"
                     " created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                     (uid, username, username.title(), role, A.hash_password("pw-" + username),
                      now_iso(), now_iso()))
        if member:
            conn.execute("INSERT INTO event_members (id, event_id, user_id, event_role, added_at)"
                         " VALUES (%s,%s,%s,%s,%s)", (new_id(), self.event_id, uid,
                                                 "organiser" if role == "admin" else "attendee",
                                                 now_iso()))
        return uid

    def _auth(self, username):
        r = self.client.post("/api/v1/auth/login",
                             json={"username": username, "password": "pw-" + username})
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def _lead_op(self, op_id, name="Dana Whitfield", captured_at="2026-09-28T14:03:11Z"):
        return {"id": op_id, "type": "lead",
                "payload": {"name": name, "company": "Meridian", "captured_at": captured_at}}

    def _drain(self, ops, headers=None):
        return self.client.post(f"/api/v1/events/{self.event_id}/outbox",
                                headers=headers or self.h_priya,
                                json={"client_sent_at": now_iso(), "ops": ops})


class OutboxTests(Base):
    def test_double_drain_produces_exactly_one_lead(self):
        """§12.3: 'forcing two sync cycles' must not duplicate."""
        op_id = new_id()
        first = self._drain([self._lead_op(op_id)])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["accepted"], [op_id])

        second = self._drain([self._lead_op(op_id)])
        self.assertEqual(second.json()["accepted"], [op_id],
                         "a replay must still be ACCEPTED or the client's outbox wedges forever")

        with db() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_captured_at_is_capture_time_not_sync_time(self):
        op_id = new_id()
        self._drain([self._lead_op(op_id, captured_at="2026-09-28T14:03:11Z")])
        with db() as conn:
            row = conn.execute("SELECT captured_at, synced_at FROM leads").fetchone()
        self.assertEqual(row["captured_at"], "2026-09-28T14:03:11Z")
        self.assertNotEqual(row["captured_at"], row["synced_at"])

    def test_clock_skew_is_recorded_not_corrected(self):
        op_id = new_id()
        self.client.post(f"/api/v1/events/{self.event_id}/outbox", headers=self.h_priya,
                         json={"client_sent_at": "2026-09-28T10:00:00Z",
                               "ops": [self._lead_op(op_id)]})
        with db() as conn:
            row = conn.execute("SELECT captured_at, clock_skew_ms FROM leads").fetchone()
        self.assertIsNotNone(row["clock_skew_ms"])
        self.assertEqual(row["captured_at"], "2026-09-28T14:03:11Z", "never rewritten")

    def test_a_batch_mixes_accepted_and_rejected_without_losing_the_good_ones(self):
        good, bad = new_id(), new_id()
        r = self._drain([
            self._lead_op(good),
            {"id": bad, "type": "receipt", "payload": {"update_id": "does-not-exist"}},
        ])
        self.assertEqual(r.json()["accepted"], [good])
        self.assertEqual(r.json()["rejected"][0]["id"], bad)
        with db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"], 1)

    def test_unknown_op_type_is_rejected_not_silently_dropped(self):
        r = self._drain([{"id": new_id(), "type": "sabotage", "payload": {}}])
        self.assertEqual(r.json()["accepted"], [])
        self.assertEqual(r.json()["rejected"][0]["code"], "UNKNOWN_OP")

    # §12.6 ---------------------------------------------------------------
    def test_field_user_cannot_edit_someone_elses_lead(self):
        lead_id = new_id()
        self._drain([self._lead_op(lead_id)], headers=self.h_priya)
        r = self._drain([{"id": new_id(), "type": "lead_edit",
                          "payload": {"lead_id": lead_id, "notes": "hijacked"}}],
                        headers=self.h_ankit)
        self.assertEqual(r.json()["rejected"][0]["code"], "FORBIDDEN")
        with db() as conn:
            self.assertIsNone(conn.execute("SELECT notes FROM leads").fetchone()["notes"])

    def test_field_user_can_edit_their_own_lead_and_the_edit_is_idempotent(self):
        lead_id = new_id()
        self._drain([self._lead_op(lead_id)], headers=self.h_priya)
        edit = {"id": new_id(), "type": "lead_edit",
                "payload": {"lead_id": lead_id, "notes": "Send the deck Monday"}}
        self._drain([edit], headers=self.h_priya)
        self._drain([edit], headers=self.h_priya)      # replayed
        with db() as conn:
            rows = conn.execute("SELECT notes FROM leads").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["notes"], "Send the deck Monday")

    def test_admin_may_edit_any_lead(self):
        lead_id = new_id()
        self._drain([self._lead_op(lead_id)], headers=self.h_priya)
        r = self._drain([{"id": new_id(), "type": "lead_edit",
                          "payload": {"lead_id": lead_id, "notes": "reviewed"}}],
                        headers=self.h_admin)
        self.assertEqual(r.json()["rejected"], [])

    def test_csv_export_neutralises_formula_injection_and_keeps_utf8(self):
        self._drain([{"id": new_id(), "type": "lead",
                      "payload": {"name": "=cmd|'/c calc'!A1", "company": "Ünïcodé Ltd",
                                  "captured_at": "2026-09-28T09:00:00Z"}}])
        r = self.client.get(f"/api/v1/events/{self.event_id}/leads/export.csv",
                            headers=self.h_admin)
        body = r.content.decode("utf-8")
        self.assertTrue(body.startswith("﻿"), "BOM so Excel reads UTF-8")
        self.assertIn("Ünïcodé Ltd", body)
        self.assertIn("'=cmd", body)
        self.assertNotIn(",=cmd", body, "a bare = would execute on open")


class SyncTests(Base):
    def _add_client(self, name="Meridian", version=None):
        with db() as conn:
            v = version if version is not None else bump_version(conn, self.event_id)
            cid = new_id()
            conn.execute(
                "INSERT INTO clients (id, event_id, name, priority, version, created_at,"
                " updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (cid, self.event_id, name, "must", v, now_iso(), now_iso()))
            return cid, v

    def test_first_sync_is_full_and_carries_everything(self):
        self._add_client()
        r = self.client.get(f"/api/v1/events/{self.event_id}/sync?since=0", headers=self.h_priya)
        body = r.json()
        self.assertTrue(body["full"], "since=0 must tell the client to clear its replica")
        self.assertEqual(len(body["clients"]), 1)
        self.assertEqual(body["version"], 1)

    def test_delta_returns_only_what_changed(self):
        self._add_client("Meridian")
        r1 = self.client.get(f"/api/v1/events/{self.event_id}/sync?since=0",
                             headers=self.h_priya).json()
        self._add_client("Harborline")

        r2 = self.client.get(f"/api/v1/events/{self.event_id}/sync?since={r1['version']}",
                             headers=self.h_priya).json()
        self.assertFalse(r2["full"])
        self.assertEqual([c["name"] for c in r2["clients"]], ["Harborline"])

    def test_json_columns_come_back_as_structures_not_strings(self):
        with db() as conn:
            v = bump_version(conn, self.event_id)
            conn.execute(
                "INSERT INTO clients (id, event_id, name, tags, talking_points, signals,"
                " version, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (new_id(), self.event_id, "Meridian", '["P&C"]', '["Thank them"]',
                 '{"health":"Green"}', v, now_iso(), now_iso()))
        c = self.client.get(f"/api/v1/events/{self.event_id}/sync?since=0",
                            headers=self.h_priya).json()["clients"][0]
        self.assertEqual(c["tags"], ["P&C"])
        self.assertEqual(c["talking_points"], ["Thank them"])
        self.assertEqual(c["signals"]["health"], "Green")

    def test_deleted_document_arrives_as_a_tombstone(self):
        up = self.client.post(
            f"/api/v1/events/{self.event_id}/documents", headers=self.h_admin,
            files={"file": ("brief.pdf", PDF, "application/pdf")})
        doc_id = up.json()["id"]
        after_upload = self.client.get(f"/api/v1/events/{self.event_id}/sync?since=0",
                                       headers=self.h_priya).json()["version"]

        self.client.delete(f"/api/v1/documents/{doc_id}", headers=self.h_admin)
        d = self.client.get(f"/api/v1/events/{self.event_id}/sync?since={after_upload}",
                            headers=self.h_priya).json()
        self.assertIn(doc_id, d["tombstones"]["documents"])

    def test_non_member_is_refused_the_event(self):
        with db() as conn:
            self._user(conn, "outsider", "field", member=False)
        r = self.client.get(f"/api/v1/events/{self.event_id}/sync",
                            headers=self._auth("outsider"))
        self.assertEqual(r.status_code, 403)

    def test_unauthenticated_sync_is_401(self):
        self.assertEqual(
            self.client.get(f"/api/v1/events/{self.event_id}/sync").status_code, 401)


class DocumentTests(Base):
    def test_upload_sniffs_content_and_ignores_the_declared_type(self):
        """§12.5: rejected on sniffed content type, not the declared one."""
        r = self.client.post(
            f"/api/v1/events/{self.event_id}/documents", headers=self.h_admin,
            files={"file": ("evil.pdf", b"<html>wifi login page</html>", "application/pdf")})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "REJECTED_UPLOAD")

    def test_extension_mismatch_is_refused(self):
        r = self.client.post(
            f"/api/v1/events/{self.event_id}/documents", headers=self.h_admin,
            files={"file": ("brief.png", PDF, "image/png")})
        self.assertEqual(r.status_code, 400)

    def test_real_pdf_round_trips_with_a_checksum_and_exact_length(self):
        up = self.client.post(
            f"/api/v1/events/{self.event_id}/documents", headers=self.h_admin,
            files={"file": ("brief.pdf", PDF, "application/pdf")}).json()
        self.assertEqual(up["size_bytes"], len(PDF))

        # TestClient's transport routes every request (even to another host) back
        # into this same app, so it can't actually follow a redirect out to Supabase
        # — check the redirect itself here, then hit the signed URL for real below.
        got = self.client.get(f"/api/v1/documents/{up['id']}/content", headers=self.h_priya,
                              follow_redirects=False)
        self.assertEqual(got.status_code, 307)
        signed = got.headers["location"]
        self.assertIn("supabase.co", signed)

        real = httpx.get(signed)
        self.assertEqual(real.status_code, 200)
        self.assertEqual(real.content, PDF)
        # The client compares this against the blob it received before caching it —
        # that check is what stops a captive portal poisoning the document cache.
        self.assertEqual(real.headers["content-length"], str(len(PDF)))

    def test_field_user_cannot_upload(self):
        r = self.client.post(
            f"/api/v1/events/{self.event_id}/documents", headers=self.h_priya,
            files={"file": ("brief.pdf", PDF, "application/pdf")})
        self.assertEqual(r.status_code, 403)

    def test_identical_files_deduplicate_on_disk(self):
        a = self.client.post(f"/api/v1/events/{self.event_id}/documents", headers=self.h_admin,
                             files={"file": ("a.pdf", PDF, "application/pdf")}).json()
        b = self.client.post(f"/api/v1/events/{self.event_id}/documents", headers=self.h_admin,
                             files={"file": ("b.pdf", PDF, "application/pdf")}).json()
        self.assertEqual(a["checksum_sha256"], b["checksum_sha256"])
        self.assertNotEqual(a["id"], b["id"])


class ShellVersionTest(unittest.TestCase):
    """No build step, no content hashes: the shell cache is keyed on a number a
    human types twice. Get the two out of step and devices either nag forever or,
    worse, keep serving a stale app.js from cache after a fix has shipped."""

    def test_service_worker_and_server_agree(self):
        import re
        from pathlib import Path
        sw = Path(__file__).resolve().parent.parent / "static" / "sw.js"
        m = re.search(r"const SHELL_VERSION = (\d+);", sw.read_text())
        self.assertIsNotNone(m, "SHELL_VERSION not found in sw.js")
        served = TestClient(app).get("/api/v1/version").json()["shell_version"]
        self.assertEqual(int(m.group(1)), served,
                         "bump SHELL_VERSION in sw.js and shell_version in app/main.py together")


if __name__ == "__main__":
    unittest.main()
