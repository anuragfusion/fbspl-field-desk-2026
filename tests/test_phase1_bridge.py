"""The rollback bridge: Phase 2 -> Phase 1's native briefing-pack format.

The hash vectors below were produced by running Phase 1's own hashPass()
verbatim under Node. To regenerate after any change to the original:

    node tools/phase1_hash.mjs
"""

import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="fielddesk-p1-")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/postgres")

from app import auth as A                                    # noqa: E402
from app.db import db, init_db, new_id, now_iso              # noqa: E402
from app.phase1 import build_pack, phase1_hash               # noqa: E402
from app.storage import put_bytes                            # noqa: E402

# (username, password) -> hash, straight out of the original JavaScript.
GOLDEN = {
    ("admin", "FieldDesk2026"): "h1:lzk3r91jm9hk7",
    ("priya", "harbor-4821"): "h1:ycwnwl1b1w0l3",
    ("Ankit", "p@ssw0rd!"): "h1:vaiadh1yly9av",
    ("admin", ""): "h1:ktov0l15ns707",
    ("x", "ünïcodé-123"): "h1:14l9sedc39ozb",
    ("UPPER", "MiXeD-Case-9"): "h1:1ll4pvp1qg88mf",
}

PDF = b"%PDF-1.4\n% bridge test\n"


class HashPortTests(unittest.TestCase):
    def test_matches_phase1_javascript_exactly(self):
        for (user, password), expected in GOLDEN.items():
            self.assertEqual(phase1_hash(user, password), expected,
                             f"hash drifted for {user!r} — Phase 1 will reject this account")

    def test_username_is_the_salt_and_case_insensitive(self):
        self.assertEqual(phase1_hash("Admin", "x"), phase1_hash("admin", "x"))
        self.assertNotEqual(phase1_hash("admin", "x"), phase1_hash("priya", "x"))


class PackTests(unittest.TestCase):
    def setUp(self):
        init_db()
        with db() as conn:
            for t in ("op_log", "update_receipts", "leads", "client_pocs", "clients",
                      "documents", "meetings", "updates", "event_members", "events",
                      "sessions", "audit_log", "users"):
                conn.execute(f"DELETE FROM {t}")
            self.event_id = new_id()
            conn.execute("INSERT INTO events (id, name, version) VALUES (%s,%s,%s)",
                         (self.event_id, "Applied Net 2026", 7))
            self.uid = new_id()
            conn.execute("INSERT INTO users (id, username, full_name, role, password_hash,"
                         " created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (self.uid, "priya", "Priya Nair", "field",
                          A.hash_password("irrelevant"), now_iso(), now_iso()))
            conn.execute("INSERT INTO event_members (id, event_id, user_id, event_role, added_at)"
                         " VALUES (%s,%s,%s,%s,%s)",
                         (new_id(), self.event_id, self.uid, "attendee", now_iso()))

            self.cid = new_id()
            conn.execute(
                "INSERT INTO clients (id, event_id, name, priority, owner, account_manager,"
                " location, product, tags, summary, talking_points, avoid_points, signals,"
                " created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (self.cid, self.event_id, "Meridian", "must", "Priya Nair", "R. Shah",
                 "Columbus, OH", "Applied Epic", '["P&C"]', "Renewal signed.",
                 '["Thank Dana"]', '["The March dispute"]', '{"health":"Green"}',
                 now_iso(), now_iso()))
            conn.execute("INSERT INTO client_pocs (id, client_id, event_id, name, title, note,"
                         " sort_order) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                         (new_id(), self.cid, self.event_id, "Dana Whitfield",
                          "Operations Director", "Decision maker", 0))
            conn.execute(
                "INSERT INTO meetings (id, event_id, client_id, title, meeting_date, start_time,"
                " duration_minutes, location, owner, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (new_id(), self.event_id, self.cid, "Renewal review", "2026-09-28",
                 "10:30 AM", 30, "FBSPL booth", "Priya Nair", "scheduled"))
            conn.execute(
                "INSERT INTO updates (id, event_id, title, body, level, pinned, is_action,"
                " author_id, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (new_id(), self.event_id, "Pricing approved", "Go ahead.", "urgent", True, True,
                 self.uid, now_iso()))
            digest, size = put_bytes(PDF)
            conn.execute(
                "INSERT INTO documents (id, event_id, client_id, filename, mime_type, size_bytes,"
                " checksum_sha256, storage_key, uploaded_by, created_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (new_id(), self.event_id, self.cid, "brief.pdf", "application/pdf", size,
                 digest, digest, self.uid, now_iso()))

    def test_pack_has_the_shape_phase1_imports(self):
        with db() as conn:
            pack, creds = build_pack(conn, self.event_id, packed_by="Office admin")
        self.assertEqual(pack["kind"], "fbspl-an26-briefing-pack")
        self.assertEqual(pack["v"], 1)
        self.assertEqual(pack["packVersion"], 7)
        for key in ("users", "clients", "docs", "meetings", "updates", "files"):
            self.assertIn(key, pack)

    def test_field_names_are_renamed_to_phase1s(self):
        with db() as conn:
            pack, _ = build_pack(conn, self.event_id)
        c = pack["clients"][0]
        self.assertEqual(c["say"], ["Thank Dana"])          # talking_points -> say
        self.assertEqual(c["avoid"], ["The March dispute"])  # avoid_points  -> avoid
        self.assertEqual(c["am"], "R. Shah")                 # account_manager -> am
        self.assertEqual(c["sig"], {"health": "Green"})      # signals -> sig
        self.assertEqual(c["pocs"][0]["name"], "Dana Whitfield")

        m = pack["meetings"][0]
        self.assertEqual(m["date"], "2026-09-28")
        self.assertEqual(m["dur"], "30 min")
        self.assertEqual(m["status"], "planned", "Phase 1 says 'planned', not 'scheduled'")

        u = pack["updates"][0]
        self.assertTrue(u["action"])                         # is_action -> action
        self.assertEqual(u["author"], "Priya Nair", "author resolved to a name string")

    def test_documents_travel_as_base64_bytes(self):
        import base64
        with db() as conn:
            pack, _ = build_pack(conn, self.event_id)
        self.assertEqual(pack["docs"][0]["name"], "brief.pdf")
        self.assertEqual(base64.b64decode(pack["files"][0]["b64"]), PDF)

    def test_lite_export_omits_the_file_bytes(self):
        with db() as conn:
            pack, _ = build_pack(conn, self.event_id, include_files=False)
        self.assertEqual(pack["files"], [])
        self.assertEqual(len(pack["docs"]), 1, "metadata still travels")

    def test_accounts_get_fresh_credentials_that_phase1_will_accept(self):
        with db() as conn:
            pack, creds = build_pack(conn, self.event_id)
        self.assertEqual(len(creds), 1)
        cred = creds[0]
        user = pack["users"][0]
        # The exported hash must verify under Phase 1's own algorithm.
        self.assertEqual(user["hash"], phase1_hash(cred["username"], cred["temporary_password"]))
        self.assertEqual(user["user"], "priya")
        self.assertEqual(user["role"], "field")

    def test_plaintext_credentials_are_not_inside_the_shared_pack(self):
        """The pack gets dropped in a Teams channel. Passwords must not ride along."""
        import json
        with db() as conn:
            pack, creds = build_pack(conn, self.event_id)
        blob = json.dumps(pack)
        for c in creds:
            self.assertNotIn(c["temporary_password"], blob)

    def test_disabled_users_are_not_exported(self):
        with db() as conn:
            conn.execute("UPDATE users SET disabled_at = %s WHERE id = %s", (now_iso(), self.uid))
            pack, creds = build_pack(conn, self.event_id)
        self.assertEqual(pack["users"], [])
        self.assertEqual(creds, [])


if __name__ == "__main__":
    unittest.main()
