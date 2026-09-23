"""Image leads in the server-rendered admin console."""

import os
import unittest
from urllib.parse import unquote

# Must run before anything imports app.db, whose load_dotenv() would otherwise
# pick up the real DATABASE_URL from .env — and these tests wipe tables.
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/postgres")

from tests.test_image_leads import Base  # noqa: E402
from app.db import db, new_id  # noqa: E402


class AdminImageLeadTests(Base):
    def login(self, username):
        self.client.cookies.clear()
        r = self.client.post("/admin/login", data={"username": username,
                                                   "password": "pw-" + username},
                             follow_redirects=False)
        return r

    def admin_login(self):
        r = self.login("admin")
        self.assertEqual(r.status_code, 303, r.text)

    def post_delete(self, lead_id):
        return self.client.post(f"/admin/image-leads/{lead_id}/delete", follow_redirects=False)

    def audit_count(self):
        with db() as conn:
            return conn.execute("SELECT COUNT(*) n FROM audit_log"
                                " WHERE action = 'image_lead.delete'").fetchone()["n"]

    def test_dashboard_lists_every_users_image_leads_with_thumbnails(self):
        mine = self.complete_lead(note="wants pricing")
        theirs = self.complete_lead(headers=self.h_ankit, client_id=None, photo_count=1,
                                    other_name="Walk-in Broker")
        pending = self.create(headers=self.h_ankit).json()["id"]
        self.admin_login()
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        html = r.text
        self.assertIn("Image leads from the floor", html)
        self.assertIn(f"/api/v1/events/{self.event_id}/image-leads/export.csv", html)
        for lead_id in (mine, theirs):
            for p in self.photo_rows(lead_id):
                self.assertIn(f'src="/api/v1/image-photos/{p["id"]}/content"', html)
        for text in ("Meridian Insurance", "Walk-in Broker", "wants pricing", "Priya", "Ankit",
                     "uploading 0/2", f"/admin/image-leads/{pending}/delete"):
            self.assertIn(text, html)
        self.assertEqual(html.count('<span class="badge ok">complete</span>'), 2)
        self.assertNotIn("No image leads yet.", html)

    def test_empty_state(self):
        self.admin_login()
        self.assertIn("No image leads yet.", self.client.get("/admin").text)

    def test_floor_user_is_sent_to_the_app(self):
        self.client.cookies.set("fd_session", self.h_priya["Authorization"].split()[1])
        r = self.client.get("/admin", follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/app"))

    def test_admin_delete_removes_rows_and_files(self):
        lead_id = self.complete_lead()
        keys = [p["storage_key"] for p in self.photo_rows(lead_id)]
        self.admin_login()
        r = self.post_delete(lead_id)
        self.assertEqual(r.status_code, 303)
        self.assertIn("kind=ok", r.headers["location"])
        self.assertIn("2 photo(s) removed", unquote(r.headers["location"]))
        self.assertIsNone(self.lead_row(lead_id))
        self.assertEqual(self.photo_rows(lead_id), [])
        for k in keys:
            self.assertNotIn(k, self.store.objects)
        self.assertEqual(self.audit_count(), 1)

    def test_storage_failure_keeps_everything(self):
        lead_id = self.complete_lead()
        self.store.fail_delete = True
        self.admin_login()
        r = self.post_delete(lead_id)
        self.assertEqual(r.status_code, 303)
        self.assertIn("kind=err", r.headers["location"])
        self.assertIsNotNone(self.lead_row(lead_id), "rows must survive a failed file delete")
        self.assertEqual(len(self.photo_rows(lead_id)), 2)
        self.assertEqual(len(self.store.objects), 2)
        self.assertEqual(self.audit_count(), 0)

    def test_floor_user_cannot_delete(self):
        lead_id = self.complete_lead()
        self.client.cookies.set("fd_session", self.h_priya["Authorization"].split()[1])
        r = self.post_delete(lead_id)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/app"))
        self.assertIsNotNone(self.lead_row(lead_id))
        self.assertEqual(len(self.store.objects), 2)

    def test_signed_out_cannot_delete(self):
        lead_id = self.complete_lead()
        r = self.post_delete(lead_id)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/admin/login"))
        self.assertIsNotNone(self.lead_row(lead_id))

    def test_unknown_lead_is_an_error(self):
        self.admin_login()
        r = self.post_delete(new_id())
        self.assertEqual(r.status_code, 303)
        self.assertIn("kind=err", r.headers["location"])

    def test_user_data_is_escaped(self):
        self.complete_lead(client_id=None, photo_count=1, other_name="<script>alert(1)</script>",
                           note="<img src=x onerror=alert(2)>")
        self.admin_login()
        html = self.client.get("/admin").text
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<img src=x onerror", html)


if __name__ == "__main__":
    unittest.main()
