"""Image leads — server half. Storage is an in-memory fake; nothing reaches Supabase."""

import hashlib
import os
import unittest
from unittest import mock

os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/postgres")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth as A  # noqa: E402
from app import image_storage as S  # noqa: E402
from app.db import db, init_db, new_id, now_iso  # noqa: E402
from app.main import app  # noqa: E402

LOGGER = "fielddesk.image_leads"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-body" * 50
JPEG_B = b"\xff\xd8\xff\xe1" + b"another-jpeg" * 50
PNG = b"\x89PNG\r\n\x1a\n" + b"png-body" * 20
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"w" * 40
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"h" * 60
AVIF = b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00mif1avif" + b"a" * 60
HTML = b"<!doctype html><script>alert(1)</script>"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeStore:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.signed: list[str] = []
        self.puts = 0
        self.fail_delete = False

    def put(self, key, data, mime):
        self.puts += 1
        self.objects[key] = (data, mime)

    def signed_url(self, key):
        self.signed.append(key)
        if key not in self.objects:
            raise FileNotFoundError(key)
        return f"https://storage.test/signed/{key}?token=t"

    def delete_many(self, keys):
        if self.fail_delete:
            raise RuntimeError("storage down")
        for k in keys:
            self.objects.pop(k, None)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.client = TestClient(app)
        self.store = FakeStore()
        for name in ("put", "signed_url", "delete_many"):
            p = mock.patch.object(S, name, getattr(self.store, name))
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for k in ("IMAGE_MAX_DIMENSION", "IMAGE_JPEG_QUALITY", "IMAGE_MAX_UPLOAD_BYTES",
                  "IMAGE_LOG_LEVEL", "IMAGE_CONSOLE_LOG", "PUBLIC_BASE_URL"):
            os.environ.pop(k, None)

        with db() as conn:
            for t in ("image_lead_photos", "image_leads", "op_log", "deleted_rows",
                      "update_receipts", "leads", "updates", "meetings", "documents",
                      "client_pocs", "clients", "event_members", "events", "sessions",
                      "audit_log", "users"):
                conn.execute(f"DELETE FROM {t}")
            self.event_id, self.other_event = new_id(), new_id()
            for eid, name in ((self.event_id, "Applied Net 2026"), (self.other_event, "Other")):
                conn.execute("INSERT INTO events (id, name) VALUES (%s,%s)", (eid, name))
            self.client_id = self._client(conn, self.event_id, "Meridian Insurance")
            self.foreign_client = self._client(conn, self.other_event, "Elsewhere Ltd")
            self.admin = self._user(conn, "admin", "admin")
            self.priya = self._user(conn, "priya", "field")
            self.ankit = self._user(conn, "ankit", "field")
            self.outsider = self._user(conn, "outsider", "field", member=False)
        self.h_admin = self._auth("admin")
        self.h_priya = self._auth("priya")
        self.h_ankit = self._auth("ankit")
        self.h_outsider = self._auth("outsider")
        self.client.cookies.clear()

    def _client(self, conn, event_id, name):
        cid = new_id()
        conn.execute("INSERT INTO clients (id, event_id, name, created_at, updated_at)"
                     " VALUES (%s,%s,%s,%s,%s)", (cid, event_id, name, now_iso(), now_iso()))
        return cid

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

    def create(self, headers=None, event_id=None, **body):
        payload = {"id": new_id(), "client_id": self.client_id, "photo_count": 2,
                   "captured_at": "2026-09-28T14:03:11Z", **body}
        return self.client.post(f"/api/v1/events/{event_id or self.event_id}/image-leads",
                                headers=headers or self.h_priya, json=payload)

    def upload(self, lead_id, slot, data=JPEG, headers=None, photo_id=None, checksum=None,
               **form):
        fields = {"photo_id": photo_id or new_id(), "sha256": checksum or sha(data),
                  "width": "1600", "height": "1200", "compressed": "1", **form}
        return self.client.put(f"/api/v1/image-leads/{lead_id}/photos/{slot}",
                               headers=headers or self.h_priya, data=fields,
                               files={"file": ("x.bin", data, "application/octet-stream")})

    def lead_row(self, lead_id):
        with db() as conn:
            return conn.execute("SELECT * FROM image_leads WHERE id = %s", (lead_id,)).fetchone()

    def photo_rows(self, lead_id):
        with db() as conn:
            return conn.execute("SELECT * FROM image_lead_photos WHERE image_lead_id = %s"
                                " ORDER BY slot", (lead_id,)).fetchall()

    def complete_lead(self, headers=None, **body):
        r = self.create(headers=headers, **body)
        self.assertEqual(r.status_code, 200, r.text)
        lead_id = r.json()["id"]
        for slot, data in ((1, JPEG), (2, JPEG_B))[:r.json()["photo_count"]]:
            self.assertEqual(self.upload(lead_id, slot, data, headers=headers).status_code, 200)
        return lead_id


class ConfigTests(Base):
    def test_defaults(self):
        c = self.client.get("/api/v1/image-leads/config", headers=self.h_priya).json()
        self.assertEqual((c["max_dimension"], c["jpeg_quality"], c["max_upload_bytes"]),
                         (1600, 0.8, 5 * 1024 * 1024))
        self.assertEqual(c["max_photos"], 2)
        self.assertTrue(c["console_log"])

    def test_valid_env_values_are_used(self):
        os.environ.update(IMAGE_MAX_DIMENSION="2048", IMAGE_JPEG_QUALITY="0.65",
                          IMAGE_MAX_UPLOAD_BYTES="2000000", IMAGE_CONSOLE_LOG="off")
        c = self.client.get("/api/v1/image-leads/config", headers=self.h_priya).json()
        self.assertEqual((c["max_dimension"], c["jpeg_quality"], c["max_upload_bytes"]),
                         (2048, 0.65, 2000000))
        self.assertFalse(c["console_log"])

    def test_invalid_env_values_fall_back_with_a_warning(self):
        os.environ.update(IMAGE_MAX_DIMENSION="abc", IMAGE_JPEG_QUALITY="5",
                          IMAGE_MAX_UPLOAD_BYTES="10", IMAGE_CONSOLE_LOG="maybe")
        with self.assertLogs(LOGGER, "WARNING") as logs:
            c = self.client.get("/api/v1/image-leads/config", headers=self.h_priya).json()
        self.assertEqual((c["max_dimension"], c["jpeg_quality"], c["max_upload_bytes"]),
                         (1600, 0.8, 5 * 1024 * 1024))
        self.assertTrue(c["console_log"])
        self.assertEqual(len(logs.records), 4)

    def test_requires_sign_in(self):
        self.assertEqual(self.client.get("/api/v1/image-leads/config").status_code, 401)


class CreateTests(Base):
    def test_picked_client_snapshots_the_name(self):
        r = self.create()
        self.assertEqual(r.status_code, 200, r.text)
        row = self.lead_row(r.json()["id"])
        self.assertEqual(row["client_name"], "Meridian Insurance")
        self.assertEqual(row["captured_by"], self.priya)
        self.assertEqual(row["captured_at"], "2026-09-28T14:03:11Z", "device time kept verbatim")
        self.assertEqual(r.json()["status"], "uploading")

    def test_other_name(self):
        r = self.create(client_id=None, other_name="  Walk-in Broker  ", note="wants a demo")
        self.assertEqual(r.status_code, 200, r.text)
        row = self.lead_row(r.json()["id"])
        self.assertIsNone(row["client_id"])
        self.assertEqual(row["client_name"], "Walk-in Broker")
        self.assertEqual(row["note"], "wants a demo")

    def test_exactly_one_of_client_or_other_name(self):
        self.assertEqual(self.create(other_name="Both").json()["error"]["code"], "INVALID_CLIENT")
        self.assertEqual(self.create(client_id=None).json()["error"]["code"], "INVALID_CLIENT")
        self.assertEqual(self.create(client_id=None, other_name="   ").status_code, 400)

    def test_client_from_another_event_is_refused(self):
        r = self.create(client_id=self.foreign_client)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, "INVALID_CLIENT"))

    def test_photo_count_must_be_1_or_2(self):
        for n in (0, 3, -1):
            self.assertEqual(self.create(photo_count=n).json()["error"]["code"],
                             "INVALID_PHOTO_COUNT")
        self.assertEqual(self.create(photo_count=1).status_code, 200)

    def test_bad_ids_are_refused(self):
        for bad in ("short", "../../etc/passwd", "a" * 65, "id with spaces"):
            self.assertEqual(self.create(id=bad).json()["error"]["code"], "INVALID_ID", bad)

    def test_length_limits(self):
        self.assertEqual(self.create(client_id=None, other_name="x" * 201).status_code, 400)
        self.assertEqual(self.create(note="x" * 1001).json()["error"]["code"], "INVALID_NOTE")

    def test_retry_with_same_id_creates_one_lead(self):
        lead_id = new_id()
        first = self.create(id=lead_id)
        second = self.create(id=lead_id)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["replayed"])
        self.assertTrue(second.json()["replayed"])
        with db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) n FROM image_leads").fetchone()["n"], 1)

    def test_retry_that_changes_photo_count_is_refused(self):
        lead_id = new_id()
        self.create(id=lead_id, photo_count=2)
        self.assertEqual(self.create(id=lead_id, photo_count=1).json()["error"]["code"],
                         "LEAD_LOCKED")

    def test_another_user_cannot_claim_an_existing_id(self):
        lead_id = new_id()
        self.create(id=lead_id)
        r = self.create(id=lead_id, headers=self.h_ankit)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "ID_CONFLICT"))
        self.assertEqual(self.lead_row(lead_id)["captured_by"], self.priya)

    def test_unauthenticated_and_non_member_are_refused(self):
        self.assertEqual(self.client.post(f"/api/v1/events/{self.event_id}/image-leads",
                                          json={"id": new_id(), "photo_count": 1}).status_code, 401)
        self.assertEqual(self.create(headers=self.h_outsider).status_code, 403)

    def test_wrong_event_is_refused(self):
        r = self.create(event_id=self.other_event, client_id=self.foreign_client)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.create(event_id="no-such-event").status_code, 404)

    def test_does_not_touch_the_sync_version(self):
        """Image leads live outside sync; creating one must not make every phone re-pull."""
        with db() as conn:
            before = conn.execute("SELECT version FROM events WHERE id = %s",
                                  (self.event_id,)).fetchone()["version"]
        self.complete_lead()
        with db() as conn:
            after = conn.execute("SELECT version FROM events WHERE id = %s",
                                 (self.event_id,)).fetchone()["version"]
        self.assertEqual(before, after)


class UploadTests(Base):
    def setUp(self):
        super().setUp()
        self.lead_id = self.create().json()["id"]

    def test_happy_path(self):
        pid = new_id()
        r = self.upload(self.lead_id, 1, photo_id=pid)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "uploading")
        (row,) = self.photo_rows(self.lead_id)
        self.assertEqual((row["id"], row["mime_type"], row["size_bytes"], row["checksum_sha256"]),
                         (pid, "image/jpeg", len(JPEG), sha(JPEG)))
        self.assertEqual((row["width"], row["height"], row["compressed"]), (1600, 1200, True))
        self.assertTrue(row["storage_key"].startswith(f"{self.event_id}/{self.lead_id}/1-"))
        self.assertEqual(self.store.objects[row["storage_key"]], (JPEG, "image/jpeg"))
        self.assertEqual(self.upload(self.lead_id, 2, JPEG_B).json()["status"], "complete")

    def test_retry_of_the_same_photo_is_accepted_once(self):
        pid = new_id()
        self.upload(self.lead_id, 1, photo_id=pid)
        again = self.upload(self.lead_id, 1, photo_id=pid)
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["replayed"])
        self.assertEqual(self.store.puts, 1)
        self.assertEqual(len(self.photo_rows(self.lead_id)), 1)

    def test_filled_slot_is_immutable(self):
        self.upload(self.lead_id, 1, JPEG)
        r = self.upload(self.lead_id, 1, JPEG_B)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (409, "SLOT_LOCKED"))
        self.assertEqual(self.photo_rows(self.lead_id)[0]["checksum_sha256"], sha(JPEG))
        self.assertEqual(self.store.puts, 1, "the replacement never reached storage")

    def test_slot_must_be_1_or_2_and_within_photo_count(self):
        for slot in (0, 3, -1):
            self.assertEqual(self.upload(self.lead_id, slot).json()["error"]["code"],
                             "INVALID_SLOT", slot)
        one = self.create(photo_count=1).json()["id"]
        self.assertEqual(self.upload(one, 2).json()["error"]["code"], "INVALID_SLOT")
        self.assertEqual(self.upload(one, 1).status_code, 200)

    def test_fingerprint_mismatch_is_rejected_and_logged(self):
        with self.assertLogs(LOGGER, "WARNING") as logs:
            r = self.upload(self.lead_id, 1, JPEG, checksum=sha(JPEG_B))
        self.assertEqual(r.json()["error"]["code"], "CHECKSUM_MISMATCH")
        self.assertTrue(any("fingerprint mismatch" in m for m in logs.output))
        self.assertEqual((self.photo_rows(self.lead_id), self.store.objects), ([], {}))

    def test_type_comes_from_the_bytes_not_the_name(self):
        r = self.upload(self.lead_id, 1, HTML)
        self.assertEqual(r.json()["error"]["code"], "REJECTED_UPLOAD")
        self.assertEqual(self.upload(self.lead_id, 1, AVIF).json()["error"]["code"],
                         "REJECTED_UPLOAD")

    def test_heic_original_keeps_its_real_type(self):
        r = self.upload(self.lead_id, 1, HEIC, compressed="0")
        self.assertEqual(r.status_code, 200, r.text)
        (row,) = self.photo_rows(self.lead_id)
        self.assertEqual(row["mime_type"], "image/heic")
        self.assertTrue(row["storage_key"].endswith(".heic"))
        self.assertFalse(row["compressed"])

    def test_png_and_webp_are_accepted(self):
        self.assertEqual(self.upload(self.lead_id, 1, PNG).json()["photo"]["mime_type"],
                         "image/png")
        self.assertEqual(self.upload(self.lead_id, 2, WEBP).json()["photo"]["mime_type"],
                         "image/webp")

    def test_size_limit(self):
        os.environ["IMAGE_MAX_UPLOAD_BYTES"] = str(100 * 1024)
        big = b"\xff\xd8\xff" + b"x" * (100 * 1024)
        self.assertEqual(self.upload(self.lead_id, 1, big).status_code, 413)
        self.assertEqual(self.store.puts, 0)

    def test_phone_dimensions_are_display_only_and_sanitised(self):
        self.upload(self.lead_id, 1, width="abc", height="-5")
        (row,) = self.photo_rows(self.lead_id)
        self.assertEqual((row["width"], row["height"]), (None, None))

    def test_only_the_owner_can_upload(self):
        for h in (self.h_ankit, self.h_admin, self.h_outsider):
            self.assertEqual(self.upload(self.lead_id, 1, headers=h).status_code, 403)
        r = self.client.put(f"/api/v1/image-leads/{self.lead_id}/photos/1",
                            data={"photo_id": new_id(), "sha256": sha(JPEG)},
                            files={"file": ("x", JPEG, "image/jpeg")})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.store.puts, 0)

    def test_unknown_lead(self):
        self.assertEqual(self.upload(new_id(), 1).status_code, 404)


class DeviceLogTests(Base):
    def setUp(self):
        super().setUp()
        self.lead_id = self.create().json()["id"]

    def send(self, entries, headers=None):
        return self.client.post(f"/api/v1/image-leads/{self.lead_id}/logs",
                                headers=headers or self.h_priya, json={"entries": entries})

    def test_lines_are_labelled_device_and_cannot_inject_new_lines(self):
        with self.assertLogs(LOGGER, "INFO") as logs:
            r = self.send([
                {"at": "2026-09-28T14:03:11Z", "photo": 1, "stage": "compress", "pct": 25,
                 "msg": "read 4032x3024 HEIC 3.8 MB"},
                {"stage": "compress", "pct": 50,
                 "msg": "ok\n2026-01-01 INFO [image-lead] SERVER lead=x verified"},
            ])
        self.assertEqual(r.json(), {"accepted": 2, "dropped": 0})
        self.assertEqual(len(logs.records), 2)
        for rec in logs.records:
            msg = rec.getMessage()
            self.assertNotIn("\n", msg)
            self.assertTrue(msg.startswith("[image-lead] DEVICE"), msg)
        self.assertIn("photo=1/2", logs.records[0].getMessage())
        self.assertIn(" 25% compress", logs.records[0].getMessage())

    def test_unknown_stages_and_percentages_are_dropped(self):
        r = self.send([{"stage": "hack", "msg": "x"}, {"stage": "upload", "pct": 33},
                       {"stage": "upload", "pct": 100, "msg": "done"}])
        self.assertEqual(r.json(), {"accepted": 1, "dropped": 2})

    def test_batch_size_is_capped(self):
        self.assertEqual(self.send([{"stage": "info"}] * 101).status_code, 422)

    def test_only_the_owner_can_send_logs(self):
        for h in (self.h_ankit, self.h_admin, self.h_outsider):
            self.assertEqual(self.send([{"stage": "info"}], headers=h).status_code, 403)

    def test_level_off_prints_nothing(self):
        os.environ["IMAGE_LOG_LEVEL"] = "off"
        with self.assertNoLogs(LOGGER):
            self.send([{"stage": "error", "msg": "boom"}])
            self.upload(self.lead_id, 1)


class NoSensitiveDataInLogsTest(Base):
    def test_server_lines_carry_no_names_or_notes(self):
        with self.assertLogs(LOGGER, "DEBUG") as logs:
            os.environ["IMAGE_LOG_LEVEL"] = "debug"
            lead_id = self.create(client_id=None, other_name="Secret Person",
                                  note="call 555-0101").json()["id"]
            self.upload(lead_id, 1)
            self.client.delete(f"/api/v1/image-leads/{lead_id}", headers=self.h_admin)
        text = "\n".join(logs.output)
        for secret in ("Secret Person", "555-0101", "Priya", "priya", "Meridian", "token",
                       "storage.test"):
            self.assertNotIn(secret, text)


class ListTests(Base):
    def test_floor_users_see_only_their_own_admin_sees_all(self):
        mine = self.complete_lead()
        theirs = self.complete_lead(headers=self.h_ankit)
        get = lambda h: [l["id"] for l in self.client.get(
            f"/api/v1/events/{self.event_id}/image-leads", headers=h).json()["image_leads"]]
        self.assertEqual(get(self.h_priya), [mine])
        self.assertEqual(get(self.h_ankit), [theirs])
        self.assertEqual(set(get(self.h_admin)), {mine, theirs})

    def test_list_carries_photos_and_status(self):
        lead_id = self.complete_lead()
        (lead,) = self.client.get(f"/api/v1/events/{self.event_id}/image-leads",
                                  headers=self.h_priya).json()["image_leads"]
        self.assertEqual(lead["id"], lead_id)
        self.assertEqual(lead["status"], "complete")
        self.assertEqual([p["slot"] for p in lead["photos"]], [1, 2])
        self.assertTrue(lead["photos"][0]["content_url"].startswith("/api/v1/image-photos/"))

    def test_non_member_and_unauthenticated_are_refused(self):
        url = f"/api/v1/events/{self.event_id}/image-leads"
        self.assertEqual(self.client.get(url, headers=self.h_outsider).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 401)


class PhotoContentTests(Base):
    def setUp(self):
        super().setUp()
        lead_id = self.complete_lead()
        self.photo_id = self.photo_rows(lead_id)[0]["id"]
        self.url = f"/api/v1/image-photos/{self.photo_id}/content"

    def get(self, headers=None):
        return self.client.get(self.url, headers=headers or {}, follow_redirects=False)

    def test_owner_and_admin_get_a_short_lived_link(self):
        for h in (self.h_priya, self.h_admin):
            r = self.get(h)
            self.assertEqual(r.status_code, 307)
            self.assertTrue(r.headers["location"].startswith("https://storage.test/signed/"))
            self.assertEqual(r.headers["cache-control"], "no-store")

    def test_others_are_refused_before_any_link_is_created(self):
        for h, code in ((self.h_ankit, 403), (self.h_outsider, 403), ({}, 401)):
            self.assertEqual(self.get(h).status_code, code)
        self.assertEqual(self.store.signed, [], "a signed link was created for a refused request")

    def test_unknown_photo(self):
        r = self.client.get(f"/api/v1/image-photos/{new_id()}/content", headers=self.h_admin,
                            follow_redirects=False)
        self.assertEqual(r.status_code, 404)


class DeleteTests(Base):
    def setUp(self):
        super().setUp()
        self.lead_id = self.complete_lead()
        self.keys = [p["storage_key"] for p in self.photo_rows(self.lead_id)]

    def delete(self, headers):
        return self.client.delete(f"/api/v1/image-leads/{self.lead_id}", headers=headers)

    def test_floor_users_cannot_delete_even_their_own(self):
        for h in (self.h_priya, self.h_ankit, self.h_outsider):
            self.assertEqual(self.delete(h).status_code, 403)
        self.assertIsNotNone(self.lead_row(self.lead_id))
        self.assertEqual(len(self.store.objects), 2)

    def test_admin_delete_removes_rows_and_files(self):
        self.assertEqual(self.delete(self.h_admin).status_code, 200)
        self.assertIsNone(self.lead_row(self.lead_id))
        self.assertEqual(self.photo_rows(self.lead_id), [])
        for k in self.keys:
            self.assertNotIn(k, self.store.objects)

    def test_storage_failure_rolls_the_rows_back_and_a_retry_works(self):
        self.store.fail_delete = True
        r = self.delete(self.h_admin)
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (502, "STORAGE_ERROR"))
        self.assertIsNotNone(self.lead_row(self.lead_id), "rows must survive a failed file delete")
        self.assertEqual(len(self.photo_rows(self.lead_id)), 2)
        with db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM audit_log WHERE action ="
                                           " 'image_lead.delete'").fetchone())
        self.store.fail_delete = False
        self.assertEqual(self.delete(self.h_admin).status_code, 200)
        self.assertIsNone(self.lead_row(self.lead_id))


class ExportTests(Base):
    def export(self, headers):
        return self.client.get(f"/api/v1/events/{self.event_id}/image-leads/export.csv",
                               headers=headers)

    def test_admin_only(self):
        for h in (self.h_priya, self.h_ankit, self.h_outsider):
            self.assertEqual(self.export(h).status_code, 403)

    def test_rows_links_and_formula_safety(self):
        self.complete_lead()
        self.complete_lead(headers=self.h_ankit, client_id=None, photo_count=1,
                           other_name="=HYPERLINK(\"http://evil\")")
        r = self.export(self.h_admin)
        self.assertEqual(r.status_code, 200)
        lines = r.text.lstrip("﻿").strip().splitlines()
        self.assertEqual(lines[0], "Captured,Client,Existing client,Note,Captured by,Photos,"
                                   "Photo 1,Photo 2,Synced")
        self.assertEqual(len(lines), 3)
        self.assertIn("http://testserver/api/v1/image-photos/", r.text)
        self.assertIn("'=HYPERLINK", r.text)
        self.assertNotIn(",=HYPERLINK", r.text)
        self.assertIn("2/2", r.text)
        self.assertIn("1/1", r.text)

    def test_public_base_url_override(self):
        os.environ["PUBLIC_BASE_URL"] = "https://desk.example.com/"
        self.complete_lead()
        self.assertIn("https://desk.example.com/api/v1/image-photos/",
                      self.export(self.h_admin).text)


class SniffTests(unittest.TestCase):
    def test_accepted_and_rejected_types(self):
        self.assertEqual(S.sniff_image(JPEG), "image/jpeg")
        self.assertEqual(S.sniff_image(PNG), "image/png")
        self.assertEqual(S.sniff_image(WEBP), "image/webp")
        self.assertEqual(S.sniff_image(HEIC), "image/heic")
        self.assertEqual(S.sniff_image(b"\x00\x00\x00\x18ftypmif1" + b"z" * 20), "image/heif")
        for bad in (b"", HTML, AVIF, b"%PDF-1.4", b"RIFF\x00\x00\x00\x00WAVE"):
            with self.assertRaises(S.RejectedImage):
                S.sniff_image(bad)


if __name__ == "__main__":
    unittest.main()
