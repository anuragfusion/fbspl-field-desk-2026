#!/usr/bin/env python3
"""End-to-end smoke test against a RUNNING server. Stdlib only.

    .venv/bin/python smoke.py --url http://127.0.0.1:8099 --user admin --password '...'

Exercises the path the event depends on: sign in, sync, upload, delta, capture a
lead, drain it twice, export. Run it after every deploy and before travel.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

PDF = b"%PDF-1.4\n% smoke-test document\n"
OK, BAD = "\033[32m  ok \033[0m", "\033[31mFAIL \033[0m"
failures = []


def check(label, condition, detail=""):
    print(f"{OK if condition else BAD} {label}" + (f"  — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)
    return condition


class Client:
    def __init__(self, base):
        self.base, self.token = base.rstrip("/"), None

    def call(self, method, path, body=None, raw=None, ctype=None):
        req = urllib.request.Request(self.base + path, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        elif raw is not None:
            data, _ = raw, req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, data, timeout=30) as r:
                payload = r.read()
                ct = r.headers.get("content-type", "")
                return r.status, (json.loads(payload) if "json" in ct else payload)
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, payload


def multipart(filename, content, ctype):
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    body += content + f"\r\n--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8099")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", required=True)
    args = ap.parse_args()
    c = Client(args.url)

    print(f"\nFBSPL Field Desk — smoke test against {args.url}\n" + "-" * 62)

    # 1. auth
    st, body = c.call("POST", "/api/v1/auth/login",
                      {"username": args.user, "password": args.password, "remember": True})
    if not check("sign in", st == 200, str(body)):
        sys.exit("cannot continue without a session")
    c.token = body["token"]

    st, bad = c.call("POST", "/api/v1/auth/login",
                     {"username": args.user, "password": "definitely-wrong"})
    check("wrong password refused with the error envelope",
          st == 401 and bad.get("error", {}).get("code") == "INVALID_CREDENTIALS")

    st, me = c.call("GET", "/api/v1/auth/me")
    check("identity and event membership", st == 200 and me.get("events"), str(me))
    event_id = me["events"][0]["id"]
    print(f"      event: {me['events'][0]['name']}  ({event_id[:8]}…)")

    # 2. full sync
    st, full = c.call("GET", f"/api/v1/events/{event_id}/sync?since=0")
    check("first sync is a full snapshot", st == 200 and full.get("full") is True)
    check("clients present", len(full.get("clients", [])) > 0,
          "import a workbook first: app.cli import-workbook --commit")
    check("JSON columns decoded, not strings",
          all(isinstance(x.get("talking_points"), list) for x in full["clients"]))
    check("POCs merged across rows", len(full.get("client_pocs", [])) >= len(full["clients"]))
    v0 = full["version"]
    print(f"      version {v0}: {len(full['clients'])} clients, "
          f"{len(full.get('client_pocs', []))} POCs, {len(full.get('documents', []))} documents")

    # 3. document upload + sniffing
    raw, ctype = multipart("smoke.pdf", PDF, "application/pdf")
    st, doc = c.call("POST", f"/api/v1/events/{event_id}/documents", raw=raw, ctype=ctype)
    check("upload a real PDF", st == 200 and doc.get("size_bytes") == len(PDF), str(doc))

    raw, ctype = multipart("evil.pdf", b"<html>captive portal login</html>", "application/pdf")
    st, rej = c.call("POST", f"/api/v1/events/{event_id}/documents", raw=raw, ctype=ctype)
    check("HTML masquerading as a PDF is rejected on sniffed content", st == 400)

    if st := doc.get("id"):
        code, blob = c.call("GET", f"/api/v1/documents/{doc['id']}/content")
        check("document bytes round-trip exactly", code == 200 and blob == PDF)

    # 4. delta
    st, delta = c.call("GET", f"/api/v1/events/{event_id}/sync?since={v0}")
    check("delta is not a full replace", st == 200 and delta.get("full") is False)
    check("delta carries only the new document",
          len(delta.get("documents", [])) == 1 and not delta.get("clients"))

    # 5. the one that matters: idempotent outbox drain
    lead_id = str(uuid.uuid4())
    op = {"id": lead_id, "type": "lead",
          "payload": {"name": "Smoke Test Lead", "company": "Meridian",
                      "captured_at": "2026-09-28T14:03:11Z"}}
    sent = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    st, r1 = c.call("POST", f"/api/v1/events/{event_id}/outbox",
                    {"client_sent_at": sent, "ops": [op]})
    check("outbox accepts a captured lead", st == 200 and r1.get("accepted") == [lead_id])

    st, r2 = c.call("POST", f"/api/v1/events/{event_id}/outbox",
                    {"client_sent_at": sent, "ops": [op]})
    check("a replayed op is still ACCEPTED (or the client's outbox wedges forever)",
          r2.get("accepted") == [lead_id])

    st, leads = c.call("GET", f"/api/v1/events/{event_id}/leads")
    mine = [x for x in leads.get("leads", []) if x["id"] == lead_id]
    check("two drains produced exactly one lead", len(mine) == 1, f"found {len(mine)}")
    if mine:
        check("captured_at is capture time, not sync time",
              mine[0]["captured_at"] == "2026-09-28T14:03:11Z", mine[0]["captured_at"])
        check("synced_at recorded separately", mine[0]["synced_at"] != mine[0]["captured_at"])

    # 6. export
    st, csv_bytes = c.call("GET", f"/api/v1/events/{event_id}/leads/export.csv")
    text = csv_bytes.decode("utf-8") if isinstance(csv_bytes, bytes) else str(csv_bytes)
    check("CSV export opens cleanly in Excel (UTF-8 BOM)", st == 200 and text.startswith("﻿"))
    check("captured lead appears in the export", "Smoke Test Lead" in text)

    # 7. the shell the phones actually load
    st, _ = c.call("GET", "/app")
    check("app shell served", st == 200)
    st, _ = c.call("GET", "/sw.js")
    check("service worker served from root scope", st == 200)

    print("-" * 62)
    if failures:
        print(f"\n{len(failures)} FAILED: " + "; ".join(failures) + "\n")
        sys.exit(1)
    print("\nAll checks passed.\n")


if __name__ == "__main__":
    main()
