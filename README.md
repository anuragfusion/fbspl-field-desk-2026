# FBSPL Field Desk — Phase 2

Office publishes client briefs, documents, schedule and live updates. The floor team reads them
on a phone **with the network switched off** and captures leads that sync back on their own.

FastAPI + SQLite + vanilla JS. No React, no bundler, no npm, no build step.

> **Offline is requirement number one.** Any change that makes the app depend on the network to
> render content the user already has is a regression, regardless of what else it improves.

---

## Quick start

```bash
uv venv --python 3.14
uv pip install fastapi uvicorn jinja2 python-multipart openpyxl argon2-cffi

python -m app.cli create-admin --username admin --name "Office admin"
EVENT=$(python -m app.cli create-event --name "Applied Net 2026" \
  --venue "Gaylord National Resort & Convention Center" --city "Washington DC" \
  --starts 2026-09-28 --ends 2026-10-01)

python -m app.cli import-workbook --event $EVENT --file clients.xlsx            # preview
python -m app.cli import-workbook --event $EVENT --file clients.xlsx --commit   # apply

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

| URL | Who |
|---|---|
| `/app` | Floor team. Installable, works offline. |
| `/admin` | Office. Publish updates, manage accounts, upload documents, import the workbook. |

The service worker only answers navigations to `/` and `/app` from cache. `/admin` is server-rendered
and must reach the network — a broader rule serves the floor shell at `/admin` and the console simply
is not there, with nothing in the console log to say why. `tests/sw_routes.test.js` holds that line.
**Shipping a change to `static/`:** nothing to bump. The server fills in `sw.js` on every request
with a fingerprint of the shell files and the list of every file in `static/` (except `sw.js`,
`selftest.html` and `index.html`, which is served as `/app`), so any edit or new file ships a new
worker and a fresh offline cache; `tests/test_shell.py` holds that line. The app calls `registration.update()` on every
boot and reloads itself once the new worker claims the page, so a device picks the change up on the
next launch without anyone opening DevTools. Install fetches every shell file with `cache: 'reload'`;
without that, `/static` (served with no `Cache-Control`) is cached heuristically by the browser and a
bumped shell refills itself with the same stale files.
| `/static/selftest.html` | Device check. Run on **every** phone that travels. |

---

## ⚠️ Read this before anything else

**1. Service workers require HTTPS.** A LAN IP over plain `http://` will not register one, and
without a service worker there is no offline mode at all. `localhost` is exempt, so it works on
your laptop and then fails on the first phone you try.

Fix it before writing any other code — Cloudflare Tunnel or Tailscale gives a real certificate in
minutes with no IT ticket:

```bash
cloudflared tunnel --url http://localhost:8000
```

**If the service worker will not register on a real iPhone, the offline premise is dead.**
Find that out on day one, not on the Thursday before travel.

**2. Each person must sign in once while they have wifi.** Sign-in is the enrollment step — it is
what puts the session, the replica and the documents on the device. After that the app works with
the network off. Nobody can first-run this at the venue.

**3. "Prepare for offline" is not optional.** Documents are downloaded in full, ahead of time.
Tapping it is what makes PDFs open in the exhibit hall. Re-run it every morning of the event.

---

## Runbook

```bash
# people
python -m app.cli add-user --username priya --name "Priya Nair" --role field
python -m app.cli add-member --event $EVENT --username priya
python -m app.cli reset-password --username priya
python -m app.cli list-users

# content
python -m app.cli import-workbook --event $EVENT --file clients.xlsx [--commit]

# the rollback lever — also prints Phase 1 passwords to this terminal
python -m app.cli export-phase1 --event $EVENT --out pack.json

# verify a deployment
python smoke.py --url https://your-host --password '...'

# backup (safe while running — WAL)
sqlite3 fielddesk.db ".backup backup-$(date +%F).db" && tar czf docs-$(date +%F).tgz storage/
```

Everything the CLI does is also in `/admin`, except exporting Phase 1 passwords — those print to a
terminal on purpose, because the pack file gets shared in a Teams channel.

---

## Tests

```bash
python -m unittest discover -s tests -t .     # 60 server tests
node --test tests/sync_core.test.js tests/sw_routes.test.js   # 18 client tests, stdlib runner
python smoke.py --password '...'              # 23 end-to-end checks against a running server
```

Open `/static/selftest.html` on each phone. It runs the **real** IndexedDB and Cache Storage code —
Node fakes cannot reproduce IndexedDB's transaction auto-commit, so that class of bug only ever
shows up there.

---

## Pre-travel dry run

Per device, **by the person who will carry it**. 45 minutes, plus an overnight gap.

**Evening**
1. Clear site data. Load the URL over `https://`. Sign in with that person's real account.
2. Add to home screen. Close the browser completely.
3. Launch from the icon. Wait for sync. Tap **Prepare for offline** → "Ready for offline — N of N".
4. Open a document for five different clients.
5. **Airplane mode ON.** Force-quit (swipe away, not background).
6. Relaunch from the icon → briefs render in under 5s.
7. **Open a document you did not open in step 4.** ← catches "only what I visited got cached".
8. Capture 3 leads: one with a long note, one with accented characters.
9. **Hard power-cycle the phone.** Relaunch, still offline → briefs render, 3 leads still pending.
10. **Leave it overnight, offline, untouched.**

**Morning**
11. Relaunch, still offline → everything still there. ← the iOS eviction test; five minutes won't find it.
12. Wifi on → leads drain. Force re-sync twice → server still shows exactly 3.
13. Admin exports the CSV → accents intact, no formula injection.
14. Admin disables that account → the phone shows a clean "signed out" message, not a white screen.
15. Admin posts an update → it appears on the phone; acknowledge it; admin sees the count.

**Any step failing on any device means that device does not travel on Phase 2.**

---

## Go / no-go

Two physical phones, one iOS one Android, tested by someone who did not write the code.

| # | Must pass | Objective |
|---|---|---|
| 1 | Cold boot offline | Airplane mode + force-quit → briefs render <5s, count matches the workbook |
| 2 | Documents offline | Every document opens, including one never opened online |
| 3 | Leads survive reboot | 3 captured offline, phone power-cycled, all 3 present |
| 4 | Idempotent drain | Reconnect, drain, force twice more → exactly 3 on the server |
| 5 | Authorization | `field` gets 403 on admin endpoints **verified by curl**, not by UI absence |
| 6 | Multi-device | Two phones, two users, both sets of leads arrive |
| 7 | Session revoke | Revoked phone shows a message, not a white screen |
| 8 | Restore | Delete the DB, restore from backup, 1–2 pass again within 30 min |

1–4 are the product. Any failing → **no-go**, fall back to Phase 1.
**Phase 1's HTML file goes on every phone either way.** It costs nothing and turns a catastrophe
into a shrug.

---

## Rollback to Phase 1

1. `python -m app.cli export-phase1 --event $EVENT --out pack.json` (note the printed passwords)
2. Open the Phase 1 HTML file on each phone, **Import briefing pack**, select `pack.json`
3. Verify the client count and that a document opens
4. Export any leads already in Phase 2 to CSV and keep them out of Phase 1 — from that moment
   Phase 1 is the system of record and brings its own leads home

Latest clean switch: **Friday 17:00**, with time to load every phone and brief the team.
At the hotel on Sunday is survivable but only because the bridge exists and was rehearsed.

---

## Design notes worth knowing before you change anything

**The captive-portal guard.** Paywalled venue wifi answers every request with `200 text/html`.
`cache.add()` would store that login page as a PDF, readiness would report "14 of 14 ready", and
every document would open as a wifi portal on the floor. So documents are fetched, size-checked
against the server's `size_bytes`, and only then cached. Every JSON response is content-type
checked before it is believed. This is the most likely thing to actually go wrong at a US
convention centre and it is not in the original spec.

**Unsynced leads cannot be destroyed by a sync.** The apply transaction is opened over the replica
stores plus `meta` and nothing else, so IndexedDB itself throws if any code path tries to clear
`leads` or `outbox`. The browser enforces it; we don't rely on remembering.

**Never `await` anything non-IDB inside the apply transaction.** An IndexedDB transaction
auto-commits when the microtask queue drains — one stray `await fetch()` silently closes it and
loses half the delta with no error. Fetch and validate everything first, then open the transaction.

**`accepted` means "durably known", not "newly inserted".** A replayed op is accepted. If it meant
inserted, the second drain would return an empty list, the client would keep the op forever, and
the outbox would wedge permanently — the mirror image of duplicating leads.

**Push before pull.** The outbox holds leads that exist nowhere else on earth. Briefs are on the
server forever. Spend a flaky connection on the irreplaceable bytes first.

**Offline authentication is not authentication.** It is a UI gate. Anyone holding an unlocked
device has the whole event replica in plaintext: every brief, every PDF, every lead. A revoked user
keeps read access until they reconnect. Devices must have an OS passcode; there is no offline
revocation and there cannot be one. Say this to the team rather than implying the sign-in protects
the data.

**Never hand-edit `SHELL_VERSION` or `SHELL_URLS` in `static/sw.js`.** They are placeholders the
server fills in (`render_service_worker` in `app/main.py`); anything dropped into `static/` is
downloaded by every phone on install, so keep large files out of it.

**Image leads are walled off from everything else.** Own tables (`image_leads`,
`image_lead_photos`), own private bucket (`SUPABASE_IMAGES_BUCKET`, default `images`), own module
(`app/image_leads.py`, `app/image_storage.py`), never in the sync payload. A floor user sees and
touches only their own; admin sees all and is the only one who can delete or export. Every id is
phone-generated so every request is safely retryable, and a filled photo slot is immutable. The
server trusts nothing the phone says: type is sniffed from the bytes, the fingerprint (SHA-256 of
the exact bytes received) is recomputed, the uploader comes from the session. Width/height are
phone-reported and display-only. Delete removes rows, then files, and commits only if the files
are gone — a storage failure rolls the rows back. Compression settings are env vars served by
`GET /api/v1/image-leads/config`; logs are `[image-lead] SERVER` (checked by the server, trust
these) or `[image-lead] DEVICE` (reported by the phone) and carry only ids, sizes and timings.

---

## Not built yet

`/auth/refresh` (30-day sessions cover it) · audit log viewer (rows are written, no UI) · admin
session-listing screen (revoke endpoint exists, curl it) · client and meeting edit forms (re-import
instead) · multi-event switching in the UI (the API and schema support it) · `password_resets`
(table exists, empty by design — an admin sets passwords).

**Image leads backlog:** verify photo width/height on the server with Pillow (today they are
phone-reported, display-only) · clean up photo files left in the `images` bucket when a whole event
is deleted (the database cascades, the bucket does not) · read business cards automatically with
the Gemini API.

Add them after the conference, when a bug costs a bad afternoon instead of a client meeting.
