# FBSPL Field Desk — Engineering Handoff

**Product:** Field Desk (office → event-floor briefing tool)
**First deployment:** Applied Net 2026 — Sept 28 – Oct 1, 2026, Gaylord National Resort & Convention Center, Washington DC
**Document date:** 2026-09-18
**Owner:** Saurabh Sakkarwal, FBSPL
**Status:** Phase 1 built and tested; Phase 2 specified, not started

---

## 0. How to read this document

This is a two-phase handoff.

**Phase 1** describes software that already exists — a single-file HTML application sitting in this same folder (`FBSPL_Field_Desk_AppliedNet2026.html`). It is written to be handed to whoever is running the event so they can ship it as-is. It is not a proposal; it is documentation plus a pre-event checklist.

**Phase 2** is a full engineering specification for a server-backed product for future events. Nothing in Phase 2 has been built. A developer should be able to pick it up and start on it without further discovery, subject to the open questions in §12.

A note on honesty about the schedule: this document is dated **2026-09-18** and the event opens **2026-09-28**. That is ten calendar days, of which realistically five or six are working days before travel. **Phase 2 cannot be built in that window and should not be attempted.** Phase 1 exists precisely so that the event is covered while Phase 2 is built properly afterwards. Any plan that tries to compress Phase 2 into the pre-event window will ship an untested authentication system to a public conference floor, which is worse than shipping nothing.

Where this document recommends a technology, it is flagged as an **assumption**. The stack has not been chosen. See §11.

---

# PHASE 1 — Ship what exists

## 1. Problem being solved

A team travels to Applied Net. The office needs to push them client intelligence — who to meet, what to say, what to avoid saying, supporting PDFs, meeting times, and live updates during the event. The floor team needs to read that on a phone or laptop, in a convention centre, and log leads back.

The binding constraint is the venue. Convention centre wifi is unreliable and often paywalled, and roaming data in the exhibit hall of a US conference is not something to bet a client meeting on. **Everything must work with the network switched off.** That single constraint is what produced the current architecture.

## 2. What was built

One file. `FBSPL_Field_Desk_AppliedNet2026.html`, approximately 1,001,290 bytes. It is opened by double-clicking it. There is no server, no install, no build step, no CDN, and no network call of any kind. This has been verified: the file contains zero `fetch`, `XMLHttpRequest`, or `WebSocket` calls, and zero external URLs outside the one vendored library.

### 2.1 Modules

| Module | What it does |
|---|---|
| Sign-in gate | Username/password, roles, session, sign-out |
| Client briefs | Per-client card: priority, owner, account manager, location, products, talking points, things to avoid, POCs, derived health signals |
| Document library | PDFs and other files stored in-browser, opened offline |
| Schedule & roster | Meetings per event day, with owner, location, attendees, status |
| Live updates feed | Pinned/urgent/normal notes, action items, read-state |
| Lead capture | Name, company, contact, interest, next step, captured-by, notes |
| Admin: accounts | Create/edit/disable/remove users, reset passwords |
| Admin: import/export | Excel workbook import, CSV import, briefing-pack export/import |

### 2.2 Distribution model

There is no sync. The hand-off is explicit and manual:

1. Office admin loads the client workbook and attaches PDFs on an office laptop.
2. Office admin clicks **Export briefing pack** → a `.json` file containing all content *and the file bytes of every attached document, base64-encoded*.
3. That `.json` is shared via OneDrive or Teams — i.e. on the office network, before anyone travels.
4. Each floor device opens the same HTML file once, signs in with its own account, and clicks **Import briefing pack**. A `field` account can import — receiving is not an admin action, and nobody needs to be handed the admin password to get set up.
5. From that moment the device is self-sufficient. It needs no network for the rest of the event.

**Leads captured on a device are never overwritten by an import.** This is deliberate and load-bearing: a floor device that imports an updated pack mid-event keeps everything its user has logged. Leads flow back the other way, as a CSV export.

There is also **Export without documents**, which produces a much smaller pack for a quick text-only refresh mid-event (e.g. a changed meeting time) over a weak connection.

## 3. Architecture of the shipped file

### 3.1 Structure

Two `<script>` blocks:

1. **SheetJS (xlsx.js)**, vendored inline — approximately 881 KB of the file. This is what allows `.xlsx` reading with no network. It was lifted verbatim from the existing `FBSPL_Battlecards_Tool_5.html` in this folder, so the two tools read workbooks identically.
2. **The application**, a single IIFE in ES5-compatible vanilla JavaScript. No framework, no bundler, no transpiler. Event handling is delegated through `data-act` attributes. All user-supplied strings pass through `esc()` before being written into `innerHTML`.

### 3.2 Storage adapter

State lives in the browser. Two backends, chosen at runtime:

- **IndexedDB** (`DB_NAME = "fbspl_an26"`, `DB_VER = 1`) — preferred. Document blobs are stored as blobs.
- **localStorage** (`LSK_STATE = "fbspl_an26_state"`, file blobs under the `LSK_FILE = "fbspl_an26_f_"` prefix) — fallback.

The fallback is not theoretical. **Chrome blocks IndexedDB on `file://` origins**, and this file will be opened off disk from a OneDrive folder. In practice, the localStorage path is the one most users will hit. The app detects this, shows a notice, and keeps working — but localStorage caps at roughly 5 MB, which is the single most important operational limit in Phase 1 (see §5.2).

The current backend is displayed in the UI (`#storeInfo`), so a user can tell support which mode they are in.

Preferences (last tab, last mode) live separately under `LS = "fbspl_an26_prefs"`. The session lives under `SESS_KEY = "fbspl_an26_sess"`.

### 3.3 State shape

```js
function blank(){
  return { v:1, packVersion:0, packUpdated:null, packBy:"",
           users:[], clients:[], docs:[], meetings:[], updates:[], leads:[] };
}
```

Record shapes, as shipped:

```
user     { id, user, name, role:'admin'|'field', hash, disabled, seed, added, lastIn }
client   { id, name, priority:'must'|'target'|'watch', owner, am, location, product,
           tags[], summary, say[], avoid[], pocs:[{name,title,note}], sig{} }
doc      { id, name, type, size, clientId, note, added }
meeting  { id, date, time, dur, title, clientId, location, owner, attendees, status, notes }
update   { id, title, body, level, pinned, action, doneAt, clientId, author, ts }
lead     { id, ts, name, company, email, phone, clientId, interest, next, by, notes }
```

`sig{}` is the derived-signal bag rendered as chips on the client card — health, feedback, AI interest, FTE, tenure, billing, book, revenue, line-of-business mix, account manager, team, in-house.

## 4. Authentication and account management (as shipped)

This section answers the specific request: *the admin panel should have the functionality to create users and manage their passwords.* **It already does.** What follows is what exists today, so it can be reviewed rather than rebuilt.

### 4.1 Sign-in

- Seeded account on first run: username `admin`, password `FieldDesk2026` (`SEED_USER` / `SEED_PASS` in source).
- `signIn(username, password, keep)` returns one of three distinct errors, deliberately worded so a user knows which problem they have:
  - `"No account with that username on this device."`
  - `"That account has been disabled."`
  - `"That password does not match."`
- `findUser()` matches on username *or* email, tolerant of case and stray whitespace (`normUser()`).
- "Keep me signed in" → session never expires. Unticked → session expires after 12 hours (`writeSession(u, keep)`).
- Sessions are re-validated on every load; a disabled or deleted account drops straight back to the gate.

### 4.2 Roles

Two roles, enforced in `applyRole()` and `setMode()`:

| | Admin | Field |
|---|---|---|
| Read briefs, docs, schedule, feed | Yes | Yes |
| Capture and edit leads | Yes | Yes |
| Export leads | Yes | Yes |
| Import a briefing pack (receive) | Yes | Yes |
| Change own password | Yes | Yes |
| Create/edit clients, docs, meetings, updates | Yes | No |
| Import workbook / CSV | Yes | No |
| Export briefing pack (publish) | Yes | No |
| Manage accounts and passwords | Yes | No |

A field user attempting to force admin mode gets `"Your account is floor-team access — ask an admin to publish changes"` and is returned to view mode. The admin nav button (`#mAdmin`) is hidden for field users.

**Corrections applied 2026-09-18**, after this matrix was checked against the shipped markup rather than against intent. Three rows above were aspirational; they are now true:

- *Export briefing pack* was reachable by a field user — the card carried no `adminonly` class. The pack contains the whole account list, so the guard now sits inside `exportPack()` itself (both the full and lite buttons route through it) and the card is `adminonly` as well.
- *Change my password* was `adminonly`, so a floor user could be handed a temporary password and then never change it. The card is now visible to both roles; the `#chgPin` handler already required the current password.
- *Import briefing pack* is deliberately open to both roles. It is the receive path — gating it would mean sharing the admin password with every traveller, which is worse than the thing the gate protects.

### 4.3 Account management functions

| Function | Behaviour |
|---|---|
| `renderUsers()` | Renders the Team accounts table (`#userBody`): name, username, role, actions |
| `editUser(id)` | Create/edit modal — full name, username, role, temporary password on create, disable checkbox on edit |
| `suggestPass()` | Generates a readable temporary password, e.g. `harbor-4821` |
| `resetPass(id)` | Admin sets another user's password; shown in plain text so it can be copied and passed on |
| `delUser(id)` | Confirm-gated removal |
| `#chgPin` handler | "Change my password" — requires the current password, then new + repeat |

Guards that are already in place:

- Username is required and must be unique.
- At least one active admin must always remain — you cannot delete, demote, or disable the last one.
- You cannot disable your own account.
- You cannot remove yourself (the action is hidden on your own row).
- Minimum password length is 6 characters.
- **Changing a username nulls that user's password hash** and prompts for a reset, because the username is used as the hash salt. This is intentional; the alternative is a silently broken login.
- Resetting your own password rewrites your session so you are not kicked out.

### 4.4 Password hashing — read this before relying on it

```js
function hashPass(user, pass){
  var s = "fbspl:an26:" + String(user||"").toLowerCase() + ":" + String(pass||"");
  // 1200 rounds of a paired DJB2-style mix
}
```

This is **not cryptography**. It is a deliberately non-cryptographic, username-salted digest whose only job is to stop a casual reader of the local storage from seeing plaintext passwords over someone's shoulder.

Stating it plainly: **Phase 1 is not a security boundary.** Everything runs in one HTML file on the client. The account list — including every hash — sits in browser storage and is copied into every briefing pack that gets shared. Anyone with the pack file or the device has the material. The sign-in exists to (a) stop a passer-by at a booth from editing client briefs, and (b) attribute captured leads to a person. It does not protect the data from a determined party, and the tool should not carry anything that would be damaging if the pack leaked.

Mitigation shipped: a persistent red banner (`#seedWarn`) appears in the Team accounts card whenever the seed password is still in use *and* real data has been loaded. This exists because `doImport` deliberately re-adds the importing admin to prevent lockout, which can otherwise leave a known-password account alive on a booth device.

### 4.5 Where the accounts UI actually is — read this before reporting it missing

Account management is **not a tab**. It is the *Team accounts* card at the bottom of **Sync & settings**, and that card is `adminonly`: it exists in the markup at all times, but CSS shows it only when `body[data-mode="admin"]`. The mode switch is the **Floor view / Admin** pair at the top right of the masthead.

Until 2026-09-18 an admin signing in landed in **Floor view** unless they had previously chosen Admin on that device (`applyRole()` only restored Admin when the stored preference said so). The result: a freshly provisioned admin opened Sync & settings, saw Publish / Receive / Storage, saw no Team accounts card, and concluded the feature had not been built. It had — it was one toggle away, with nothing on screen pointing at the toggle.

`applyRole()` now defaults an `admin` account to Admin mode and only drops to Floor view if that admin explicitly chose Floor view on that device (`lastMode!=="view"`). Field accounts are unaffected; they cannot enter Admin mode at all.

So, to create an account: sign in as an admin → **Sync & settings** → **Team accounts** → **+ Add account** → fill in name, username, role, and the pre-filled temporary password (`suggestPass()`, e.g. `harbor-4821`) → *Create account*. Send that password over Teams, not inside the pack.

## 5. Known limits of Phase 1

These are the four things that will bite, listed so nobody discovers them at the venue.

### 5.1 Client-side auth only
Covered in §4.4. Accept it for this event; Phase 2 fixes it.

### 5.2 Roughly 5 MB of documents on the localStorage path
When IndexedDB is unavailable — which is the likely case opening from a OneDrive folder — total document storage is capped near 5 MB. **Curate the PDF set.** Two or three compressed one-pagers, not a 40 MB capability deck. If more is needed, either compress hard or accept that documents live in OneDrive and the Field Desk carries only the briefs.

### 5.3 Distribution is manual
There is no sync. If the office changes a brief on Tuesday, somebody must export a fresh pack, put it somewhere the floor team can reach it, tell them, and each of them must import it. Nominate one person for this and agree the channel (Teams chat is the obvious choice) before anyone flies.

### 5.4 No real-browser or mobile visual QA
The build environment could not run a real browser. Verification was done with jsdom — 105 automated checks across two harnesses, each run in both storage modes, all passing. That covers logic thoroughly and **covers rendering not at all**. Nobody has yet looked at this on an actual iPhone. This is the highest-value thing to do first (§6).

## 6. Pre-event checklist

Ordered by value per hour, for the ten days available.

**Must, before anyone travels**

1. **Open the file on a real iPhone and a real Android phone.** Check the sign-in gate, the client cards, opening a PDF, and the lead capture form. This is the only untested surface. Budget half a day including fixes.
2. **Open it in Chrome, Edge, and Safari on a laptop.** Confirm which storage backend each reports in the footer.
3. **Change the seed password.** Sign in as `admin` / `FieldDesk2026`, confirm the masthead shows **Admin** selected, then use *Sync & settings → Change my password*. Do not travel with the default.
4. **Create one real account per traveller** with role `field` (*Sync & settings → Team accounts → + Add account*, see §4.5), and hand each person their temporary password in a way that isn't a group chat. Ask each of them to change it on their own device after the first sign-in — that path works for field accounts as of 2026-09-18.
5. **Load the real client workbook** and confirm the columns map correctly. Spot-check three clients against the source sheet.
6. **Attach the actual PDFs** and confirm the total stays inside the storage limit on the *fallback* path, not just on IndexedDB.
7. **Do one full dry run of the hand-off**: export a pack on the office laptop, share it, import it on a phone that has never seen the file, sign in as a field user, open a PDF with wifi off, log a test lead, export leads to CSV.

**Should, if time allows**

8. Populate the schedule with confirmed meetings.
9. Agree and write down the mid-event refresh protocol (who exports, where it goes, how people are told).
10. Decide who collects lead CSVs at the end of each day and how they are merged.

**Do not attempt before the event**

- Any part of Phase 2.
- Any change to the storage adapter or the auth module. They are tested; leave them.

---

# PHASE 2 — Server-backed product

## 7. Goals and non-goals

**Goals**

- Same content model, but with a real server as the source of truth, so the office publishes once and every device sees it.
- Real authentication, with hashes that are actually hashes and a session that can be revoked.
- Full offline capability at the venue — this is not relaxed by having a server, it is the whole point of the product.
- Multi-event: an event is a first-class entity, so Applied Net 2027 does not mean a new HTML file.
- Leads sync back automatically rather than by CSV.

**Non-goals**

- Not a CRM. Leads are captured and exported; whatever system owns the customer record stays the owner.
- Not a replacement for the battlecards tool. Both consume the same workbook.
- Not public. Every user is an FBSPL employee.
- No real-time collaboration. Eventual consistency on a poll or push is sufficient.

## 8. Architecture

### 8.1 Shape

A conventional three-tier app with an offline-first client:

```
  Phone / laptop browser
    └── PWA (installable, service worker, local DB)
          │  sync API (HTTPS, JSON)
          ▼
        API service  ──►  Relational database
                     ──►  Object storage (documents)
                     ──►  Auth / identity provider
```

**Assumption, not a decision.** A reasonable default stack would be a TypeScript API (Node with Fastify or NestJS), PostgreSQL, S3-compatible object storage, and a React PWA with IndexedDB via Dexie. FBSPL is a Microsoft 365 shop, so Azure App Service + Azure SQL/Postgres + Blob Storage + Entra ID single sign-on is at least as defensible and removes the password-management problem entirely. **This choice has not been made** and should be made by whoever will maintain it. Everything below is written to be stack-agnostic; the endpoints and tables translate directly either way.

### 8.2 Offline strategy — the part that must not be got wrong

The venue is the design constraint, not an edge case. The client is offline-first, not online-with-a-cache.

- **Service worker** caches the application shell. The app boots with the network off.
- **Local database** (IndexedDB) holds a full replica of the current event's briefs, documents, schedule, and feed. All reads hit the local replica. The UI never blocks on the network.
- **Documents are pre-fetched in full**, not lazily. When a device joins an event it downloads every document for that event before it leaves the office. A PDF that streams on demand is a PDF you cannot open in the exhibit hall. Show the user an explicit "Ready for offline — 14 of 14 documents" state.
- **Writes are queued.** Lead capture, lead edits, and update read-state go into a local outbox with a client-generated UUID and a local timestamp, and drain when connectivity returns. The user sees "saved" immediately and a small pending-sync count.
- **Conflict rule: leads are append-only from the client and never overwritten by the server.** This carries forward from Phase 1 and is non-negotiable. For briefs, documents, schedule, and feed, the server is authoritative and the client replica is replaced wholesale; those are published artefacts, not collaborative documents.
- **Sync is a delta pull plus an outbox push**, keyed on a monotonic `version` per event. No websockets required; a poll on foreground plus a manual pull-to-refresh is sufficient and far easier to make reliable.
- **Storage estimate and warning.** Call `navigator.storage.estimate()` and warn before a device runs short. Request persistent storage so the browser does not evict the replica overnight.

## 9. Data model

Relational. Table and column names below are the contract; types are given in PostgreSQL terms and translate obviously.

### 9.1 `users`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `username` | citext unique | Login handle |
| `email` | citext unique nullable | Also accepted at login |
| `full_name` | text not null | Shown on lead attribution |
| `role` | text not null | `admin` \| `field` (see §10.4) |
| `password_hash` | text nullable | Argon2id. Null when SSO-only |
| `must_change_password` | boolean default false | Set true on admin reset |
| `disabled_at` | timestamptz nullable | Soft disable, preserves attribution |
| `last_login_at` | timestamptz nullable | |
| `failed_attempts` | int default 0 | For lockout |
| `locked_until` | timestamptz nullable | |
| `created_by` | uuid FK users | |
| `created_at` / `updated_at` | timestamptz | |

Users are never hard-deleted while they have attributed leads; `disabled_at` is the removal mechanism.

### 9.2 `events`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `name` | text | "Applied Net 2026" |
| `venue` | text | |
| `city` / `country` | text | |
| `starts_on` / `ends_on` | date | Drives the day tabs |
| `timezone` | text | IANA, e.g. `America/New_York`. Matters: the office is in IST |
| `version` | bigint default 0 | Bumped on any publish; drives delta sync |
| `archived_at` | timestamptz nullable | |

### 9.3 `event_members`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `event_id` | uuid FK events | |
| `user_id` | uuid FK users | |
| `event_role` | text | `organiser` \| `attendee` — per-event, distinct from the global role |
| `added_at` | timestamptz | |

Unique on (`event_id`, `user_id`). A user sees only the events they are a member of.

### 9.4 `clients`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `event_id` | uuid FK events | |
| `name` | text not null | |
| `priority` | text | `must` \| `target` \| `watch` |
| `owner` | text | FBSPL person meeting them |
| `account_manager` | text | |
| `location` | text | |
| `product` | text | |
| `tags` | text[] | |
| `summary` | text | |
| `talking_points` | text[] | "say" in Phase 1 |
| `avoid_points` | text[] | "avoid" in Phase 1 |
| `signals` | jsonb | Derived chips — health, tenure, billing, mix, etc. |
| `source_row_hash` | text nullable | For idempotent workbook re-import |
| `created_at` / `updated_at` | timestamptz | |

Index on (`event_id`, `priority`), and a trigram or full-text index on `name` for the search box.

### 9.5 `client_pocs`

| Column | Type |
|---|---|
| `id` | uuid PK |
| `client_id` | uuid FK clients on delete cascade |
| `name` | text |
| `title` | text |
| `note` | text |
| `sort_order` | int |

### 9.6 `documents`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `event_id` | uuid FK events | |
| `client_id` | uuid FK clients nullable | Null = general document |
| `filename` | text | |
| `mime_type` | text | |
| `size_bytes` | bigint | |
| `checksum_sha256` | text | Lets the client skip re-downloading |
| `storage_key` | text | Object-storage path |
| `note` | text | |
| `uploaded_by` | uuid FK users | |
| `created_at` | timestamptz | |

Enforce an allow-list of MIME types (PDF, PNG, JPEG, DOCX, XLSX, PPTX) and a per-file size cap. Never trust the client-supplied MIME type — sniff it server-side.

### 9.7 `meetings`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `event_id` | uuid FK events | |
| `client_id` | uuid FK clients nullable | |
| `title` | text | |
| `meeting_date` | date | |
| `start_time` | time | Local to the event timezone |
| `duration_minutes` | int | |
| `location` | text | Booth number, restaurant, hall |
| `owner` | text | |
| `attendees` | text | |
| `status` | text | `scheduled` \| `done` \| `cancelled` |
| `notes` | text | |

Index on (`event_id`, `meeting_date`, `start_time`).

### 9.8 `updates`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `event_id` | uuid FK events | |
| `client_id` | uuid FK clients nullable | |
| `title` | text | |
| `body` | text | |
| `level` | text | `info` \| `urgent` |
| `pinned` | boolean | |
| `is_action` | boolean | Requires acknowledgement |
| `author_id` | uuid FK users | |
| `created_at` | timestamptz | |

### 9.9 `update_receipts`

| Column | Type |
|---|---|
| `update_id` | uuid FK updates |
| `user_id` | uuid FK users |
| `read_at` | timestamptz nullable |
| `done_at` | timestamptz nullable |

Composite PK on (`update_id`, `user_id`). This is what lets the office see that the floor team has actually read the urgent note.

### 9.10 `leads`

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | **Client-generated.** Enables offline creation and idempotent sync |
| `event_id` | uuid FK events | |
| `client_id` | uuid FK clients nullable | |
| `name` | text | |
| `company` | text | |
| `email` | text | |
| `phone` | text | |
| `interest` | text | |
| `next_step` | text | |
| `notes` | text | |
| `captured_by` | uuid FK users | Defaults to the signed-in user |
| `captured_at` | timestamptz | **Device time at capture**, not server receipt time |
| `synced_at` | timestamptz | Server receipt |
| `created_at` / `updated_at` | timestamptz | |

`captured_at` and `synced_at` being separate columns is what makes the offline story honest — a lead captured at 10:04 in the hall and synced at 18:30 in the hotel should report 10:04.

### 9.11 `audit_log`

| Column | Type |
|---|---|
| `id` | bigserial PK |
| `actor_id` | uuid FK users nullable |
| `action` | text (`user.create`, `user.reset_password`, `pack.publish`, `document.delete`, `login.failed`, …) |
| `entity_type` / `entity_id` | text / uuid |
| `detail` | jsonb |
| `ip` | inet |
| `created_at` | timestamptz |

Every account and password action writes here. Not optional — it is the thing that makes §4.4's problem go away.

### 9.12 `sessions`

| Column | Type |
|---|---|
| `id` | uuid PK |
| `user_id` | uuid FK users |
| `refresh_token_hash` | text |
| `device_label` | text |
| `user_agent` | text |
| `issued_at` / `expires_at` / `revoked_at` | timestamptz |

Server-side sessions rather than stateless-only tokens, so a lost phone at a conference can be revoked from the admin panel within seconds.

### 9.13 `password_resets`

| Column | Type |
|---|---|
| `id` | uuid PK |
| `user_id` | uuid FK users |
| `token_hash` | text |
| `expires_at` | timestamptz |
| `used_at` | timestamptz nullable |
| `issued_by` | uuid FK users nullable |

Single-use, 30-minute expiry, hashed at rest.

## 10. API

REST, JSON, HTTPS only. All paths prefixed `/api/v1`. All responses use a consistent error envelope:

```json
{ "error": { "code": "INVALID_CREDENTIALS", "message": "Human-readable text" } }
```

### 10.1 Auth

| Method | Path | Body / notes |
|---|---|---|
| `POST` | `/auth/login` | `{username, password, remember}` → access token (15 min) + refresh token (httpOnly cookie, 30 days or session) |
| `POST` | `/auth/refresh` | Rotates the refresh token; reuse of a rotated token revokes the family |
| `POST` | `/auth/logout` | Revokes the current session |
| `GET` | `/auth/me` | Current user, role, event memberships |
| `POST` | `/auth/change-password` | `{current_password, new_password}` — always requires the current password, even for admins |
| `POST` | `/auth/forgot-password` | `{email}` — always returns 204 regardless of whether the account exists |
| `POST` | `/auth/reset-password` | `{token, new_password}` |

### 10.2 Admin — users

| Method | Path | Notes |
|---|---|---|
| `GET` | `/admin/users` | Paginated; filter by role, disabled |
| `POST` | `/admin/users` | `{username, email, full_name, role}` → creates with a one-time invite link, or a generated temporary password with `must_change_password = true` |
| `PATCH` | `/admin/users/:id` | Name, email, role |
| `POST` | `/admin/users/:id/reset-password` | Issues a reset link; optionally returns a temporary password for out-of-band delivery |
| `POST` | `/admin/users/:id/disable` | Soft disable; revokes all sessions |
| `POST` | `/admin/users/:id/enable` | |
| `GET` | `/admin/users/:id/sessions` | |
| `DELETE` | `/admin/users/:id/sessions/:sid` | Remote sign-out — the lost-phone case |
| `GET` | `/admin/audit` | Filter by actor, action, date range |

Server-side invariants, enforced in a transaction, not in the UI: at least one enabled admin must always exist; an admin cannot disable, demote, or delete their own account; usernames and emails are unique case-insensitively.

### 10.3 Content

| Method | Path | Notes |
|---|---|---|
| `GET` | `/events` | Events the caller belongs to |
| `POST` | `/events` | Admin |
| `GET` | `/events/:id/sync?since=<version>` | **The core call.** Returns changed clients, documents (metadata), meetings, updates, plus the new `version` and a tombstone list of deleted ids |
| `POST` | `/events/:id/outbox` | Batched client writes — leads, lead edits, update receipts. Idempotent on client-supplied ids |
| `GET/POST/PATCH/DELETE` | `/events/:id/clients[/:cid]` | Admin for writes |
| `GET/POST/PATCH/DELETE` | `/events/:id/meetings[/:mid]` | Admin for writes |
| `GET/POST/PATCH/DELETE` | `/events/:id/updates[/:uid]` | Admin for writes |
| `POST` | `/events/:id/updates/:uid/ack` | Any member — marks read / done |
| `GET` | `/events/:id/documents` | Metadata incl. checksum |
| `POST` | `/events/:id/documents` | Admin. Multipart, or pre-signed upload URL |
| `GET` | `/documents/:id/content` | Short-lived signed URL or a streamed response |
| `DELETE` | `/documents/:id` | Admin |
| `GET/POST/PATCH` | `/events/:id/leads[/:lid]` | Field users may create and edit their own; admins see all |
| `GET` | `/events/:id/leads/export.csv` | |
| `POST` | `/events/:id/import/workbook` | Admin. Multipart `.xlsx` → preview + commit, see §10.5 |
| `POST` | `/events/:id/import/pack` | Admin. Accepts a **Phase 1 briefing pack** — see §11 |

### 10.4 Roles matrix

Two global roles plus a per-event role. The per-event role exists so an office admin who is not attending can publish without appearing on the booth roster.

| Capability | `admin` | `field` | Notes |
|---|:--:|:--:|---|
| Sign in | ✓ | ✓ | |
| Change own password | ✓ | ✓ | Requires current password |
| View briefs / docs / schedule / feed for own events | ✓ | ✓ | Membership-scoped |
| Acknowledge an update | ✓ | ✓ | |
| Create a lead | ✓ | ✓ | |
| Edit a lead | ✓ (any) | ✓ (own only) | |
| Delete a lead | ✓ | ✗ | |
| Export leads | ✓ | ✓ | |
| Create/edit/delete clients, meetings, updates | ✓ | ✗ | |
| Upload/delete documents | ✓ | ✗ | |
| Import workbook or pack | ✓ | ✗ | |
| Create/edit/disable users | ✓ | ✗ | |
| Reset another user's password | ✓ | ✗ | |
| Revoke another user's session | ✓ | ✗ | |
| Create an event / manage membership | ✓ | ✗ | |
| Read the audit log | ✓ | ✗ | |

Authorisation is enforced server-side on every endpoint. The client hiding a button is a convenience, never a control.

### 10.5 Workbook import

Carry over the ingestion logic from Phase 1 verbatim — it is already tested against the real FBSPL sheet and shared with the battlecards tool.

- `scoreHeaders()` scores every sheet against the first five candidate header rows; requires a score of at least 3 to proceed. Scoring: company +3, client name +3, account manager +2, and +1 each for FTE, health, POC.
- `findCol()` resolves each logical column by exact match, then prefix, then substring.
- Recognised columns: `company, client, priority, status, health, remarks, errors, start, fte, billUsd, city, state, country, am, team, um, owner, ams, inhouse, growth, aiLevel, aiDet, feedback, fbDate, sentiment, book, plPct, clPct, ebPct, revenue, pocName, pocTitle, pocCmt, hobbies, staff, avoid`.
- Rows are grouped by lowercased company name; multiple rows for one company merge their POCs.
- Derived priority: an explicit Priority column wins (`must|^1$|high|p1` → must; `watch|low|p3` → watch; otherwise target). With no such column: critical health or high AI interest → `must`; good health and no AI interest → `watch`; otherwise `target`.
- Derived health tone: red/at-risk/critical/escalation, or negative/unhappy/vocal sentiment → `crit`; amber/watch/moderate → `warn`; green/healthy/strong, or positive/advocate → `good`.

Import must be **two-step**: `POST` the file, get back a preview with counts (new / updated / unchanged) and any unmapped columns, then confirm. An import that silently rewrites 300 client briefs the night before an event is a bad afternoon.

## 11. Migration from Phase 1

The Phase 1 export is the migration path. Its schema:

```json
{
  "kind": "fbspl-an26-briefing-pack",
  "v": 1,
  "exported": "<ISO timestamp>",
  "packVersion": 0,
  "packUpdated": null,
  "packBy": "<name>",
  "users":    [ { "id","user","name","role","hash","disabled","seed","added","lastIn" } ],
  "clients":  [ { "id","name","priority","owner","am","location","product",
                  "tags":[], "summary","say":[], "avoid":[],
                  "pocs":[{"name","title","note"}], "sig":{} } ],
  "docs":     [ { "id","name","type","size","clientId","note","added" } ],
  "meetings": [ { "id","date","time","dur","title","clientId","location",
                  "owner","attendees","status","notes" } ],
  "updates":  [ { "id","title","body","level","pinned","action","doneAt",
                  "clientId","author","ts" } ],
  "files":    [ { "id", "name", "type", "data": "<base64 data URL from blobToB64()>" } ]
}
```

Leads are exported separately as CSV, by design — they are the return path, not part of the published pack.

**Mapping into Phase 2:**

| Pack field | Destination |
|---|---|
| `clients[].say` | `clients.talking_points` |
| `clients[].avoid` | `clients.avoid_points` |
| `clients[].am` | `clients.account_manager` |
| `clients[].sig` | `clients.signals` (jsonb, as-is) |
| `clients[].pocs[]` | `client_pocs` rows |
| `docs[]` + matching `files[].data` | `documents` row + object-storage upload; compute `checksum_sha256` on ingest. `files[].data` is what `blobToB64()` produced, so strip the data-URL prefix before decoding |
| `meetings[].dur` | `meetings.duration_minutes` |
| `meetings[].date` / `.time` | `meeting_date` / `start_time`, interpreted in the event timezone |
| `updates[].action` / `.doneAt` | `updates.is_action` / an `update_receipts` row |
| `updates[].author` (a name string) | Resolve to `users.id`; fall back to storing the literal in `detail` |
| `users[].user` | `users.username` |
| `users[].role` | `users.role` (values already align) |
| Lead CSV | `leads`, with `captured_at` from the CSV timestamp |

**Explicitly do not migrate `users[].hash`.** Those digests are not password hashes in any meaningful sense (§4.4). Every user gets a fresh credential in Phase 2 — an invite link, or an SSO identity if that route is taken. Import their name, username, role, and nothing else.

The pack importer should also be kept as a permanent feature, not just a one-off script. It is the disaster-recovery path: if the server is unreachable from a venue, an admin can still export from a device and rebuild.

**Assumptions flagged, since the stack was not chosen:**

1. The database is relational. If a document store is preferred, the tables above become collections and the foreign keys become references; nothing else changes.
2. Passwords are hashed with Argon2id (or bcrypt at an appropriate cost). If Entra ID SSO is adopted instead, §10.1 and §10.2's password endpoints, plus `password_resets`, all disappear — which is a meaningful simplification and worth considering seriously given FBSPL is already on Microsoft 365.
3. Documents live in object storage, not in the database.
4. Hosting has not been specified. If it must sit inside FBSPL's existing Azure tenancy, say so before design starts — it affects auth, storage, and CI.

## 12. Acceptance criteria

Written so they can be pasted into tickets. Each is independently testable.

### 12.1 Authentication

- A valid username and password returns an access token and sets a refresh cookie; `GET /auth/me` then returns the correct role.
- An invalid password returns `401 INVALID_CREDENTIALS` and increments `failed_attempts`.
- Ten consecutive failures within 15 minutes sets `locked_until` to 15 minutes ahead; further attempts return `423` regardless of correctness.
- Login response time is within 50 ms whether or not the username exists (no user enumeration by timing).
- A disabled account cannot log in and its existing sessions stop working within one refresh cycle.
- A refresh token, once rotated, cannot be reused; reuse revokes the entire session family and writes an audit entry.
- "Remember me" unticked → the session expires 12 hours after issue. Ticked → 30 days.

### 12.2 Account management

- An admin can create a user with a username, email, full name, and role; the user appears in `GET /admin/users` immediately.
- Creating a user with an existing username (differing only by case or surrounding whitespace) returns `409`.
- A newly created user with a temporary password is forced to change it on first sign-in and cannot reach any other screen until they do.
- An admin resetting another user's password revokes that user's sessions and writes an audit entry naming both parties.
- A user changing their own password must supply the correct current password; an incorrect one returns `400` and does not change anything.
- Changing your own password keeps you signed in on the current device and signs you out everywhere else.
- Attempting to disable, demote, or delete the last enabled admin returns `409` with a clear message, and the attempt is made concurrently in a test to prove the check is transactional, not a read-then-write race.
- An admin cannot disable or demote their own account.
- A disabled user's name still renders correctly on leads they captured.
- Every one of the above writes an `audit_log` row with actor, action, target, and IP.

### 12.3 Offline behaviour — test these with the network genuinely disabled, not throttled

- With the device in airplane mode from a cold start, the app loads, the user signs in with a cached session, and all briefs, schedule entries, and feed items render.
- Every document listed for the event opens offline. No document shows a loading state that never resolves.
- The device reports an explicit readiness state before travel, e.g. "Ready for offline — 14 of 14 documents, 62 MB".
- A lead captured offline is visible in the list immediately and survives a full app restart and a device reboot.
- On reconnection the outbox drains without duplicating any lead, verified by capturing the same lead while offline and forcing two sync cycles.
- `captured_at` on a synced lead reflects the time of capture, not the time of sync.
- A `sync` pull replaces briefs, documents, meetings, and updates wholesale — and **does not** touch locally captured leads. Assert this explicitly with unsynced leads present.
- Storage pressure is surfaced to the user before it becomes a failure.

### 12.4 Content publishing

- An admin uploading an `.xlsx` gets a preview showing counts of new, updated, and unchanged clients, plus any unrecognised columns, before anything is committed.
- Cancelling at the preview stage leaves the database untouched.
- Re-importing the identical workbook produces zero updates (idempotent on `source_row_hash`).
- A client with multiple rows in the workbook produces one client record with merged POCs.
- Derived priority and health tone match the rules in §10.5, verified against a fixture workbook containing at least one case of each.
- A field user receives `403` from every write endpoint in §10.4, tested endpoint by endpoint rather than by checking the UI.
- Deleting a client soft-deletes and emits a tombstone that removes it from every device's replica on the next sync.

### 12.5 Documents

- Uploads outside the MIME allow-list are rejected on sniffed content type, not the declared one.
- Document content URLs expire and cannot be shared with an unauthenticated party.
- A document's checksum lets a client skip re-downloading an unchanged file.

### 12.6 Leads

- A field user can edit a lead they captured and cannot edit one captured by someone else.
- CSV export opens cleanly in Excel with UTF-8 characters intact and no formula injection (leading `=`, `+`, `-`, `@` are neutralised).

## 13. Non-functional requirements

**Offline capability is requirement number one.** Any change that makes the app depend on the network to render content the user already has is a regression, regardless of what else it improves.

| Area | Requirement |
|---|---|
| Availability | 99.5% during an event window. Outside it, best-effort. A server outage must not stop a device that has already synced. |
| Performance | Local reads render in under 100 ms. A sync delta for a typical event completes in under 5 seconds on 4G. Initial document pre-fetch is bounded and resumable. |
| Scale | Design target: 50 events, 500 clients per event, 100 documents per event, 50 concurrent users. These are small numbers; do not over-engineer. |
| Data residency | Client commercial data. Confirm whether it may leave India or the US before choosing a region. **Open question — see §14.** |
| Encryption | TLS 1.2+ in transit. Encryption at rest for the database and object storage. |
| Password storage | Argon2id, or a documented equivalent. Never a bespoke hash. The Phase 1 approach must not survive into Phase 2. |
| Secrets | In a managed secret store, never in source or environment files committed to the repo. |
| Backups | Daily database backup with a tested restore. A verified restore drill before the first event the product is used at. |
| Logging | Structured logs, request ids, no passwords or tokens in log output. Audit log retained 12 months minimum. |
| Accessibility | WCAG 2.1 AA for the floor-facing screens. People will read this in bad light with one hand. |
| Devices | iOS Safari and Android Chrome, last two major versions. Desktop Chrome, Edge, Safari. Phone-first layout. |
| Branding | FBSPL tokens as used in Phase 1: `--blue:#002CCE`, `--blue-hover:#0023A6`, `--lav-100:#E4E4FF`, `--lav-50:#F2F2FF`, `--lav-30:#F8F8FF`, `--lav-15:#FCFCFF`, `--orange:#FC921F` (critical accent only), `--black:#221F1F`, `--gray-700:#6D6E71`, `--gray-400:#B8B9BB`, `--ok:#0E8F55`, `--err:#D92D20`. Font stack Futura PT → Century Gothic → URW Gothic → Avenir. Radii 4/8/12px. |
| Localisation | English only. Event timezone is stored per event; do not assume the office timezone. |
| Testing | Unit coverage on the ingestion and authorisation layers. An end-to-end suite that runs the full office-to-floor journey offline. Phase 1's two jsdom harnesses are a reasonable starting fixture set. |

## 14. Open questions

These need answers from the business, not from engineering. They are ordered by how much they change the design.

1. **Stack and hosting.** Is this to live in FBSPL's Azure tenancy? If yes, Entra ID SSO becomes the obvious auth choice and removes a large slice of §10.1–10.2 entirely. This is the single highest-leverage decision.
2. **Data residency.** Client names, account health, billing figures, and revenue are commercial data about identifiable businesses. Where is it allowed to be stored, and does any client contract constrain this? Answer before choosing a region.
3. **Who owns the workbook?** Is the Excel sheet the permanent source of truth, or should Phase 2 eventually pull from the CRM directly? If the latter, the importer is a bridge rather than a feature.
4. **Where do leads go afterwards?** CSV into somebody's inbox, or a push into the CRM? A CRM integration changes the lead model.
5. **How many events per year, and what is the budget envelope?** If it is two events a year with ten people, a managed low-code approach may beat a bespoke build and this spec is over-scoped.
6. **Does the battlecards tool merge into this, or stay separate?** They already share ingestion logic. Two tools reading one sheet is fine; two tools with diverging ingestion logic is not.
7. **Retention.** How long are leads and event content kept? Is there a deletion obligation once a prospect goes cold?
8. **Who is on call during an event?** A US-timezone event run from an IST office needs a named person reachable at 09:00 Eastern.
9. **Device ownership.** Personal phones or company devices? If personal, remote session revocation (§10.2) moves from useful to mandatory.
10. **Is there an approval step before publishing?** Right now any admin can push a brief to every device instantly. Fine for a small team; worth revisiting if the admin group grows.

## 15. Suggested sequencing for Phase 2

Assuming a start after Applied Net 2026 concludes, and assuming a next event roughly two quarters out. These are rough orders of magnitude for a single full-time developer, not commitments — **treat them as estimates to be re-done by whoever actually builds it.**

| Milestone | Scope | Rough size |
|---|---|---|
| 0. Decisions | §14 items 1, 2, 5 answered; stack chosen; repo, CI, environments | 1 week |
| 1. Auth spine | Users, sessions, login, password reset, roles, audit log, admin user screens | 2–3 weeks |
| 2. Content core | Events, clients, POCs, meetings, updates; admin CRUD; workbook import with preview | 3–4 weeks |
| 3. Documents | Upload, storage, signed access, checksums | 1–2 weeks |
| 4. Offline client | PWA shell, local replica, delta sync, document pre-fetch, readiness state | 3–4 weeks |
| 5. Leads and outbox | Offline capture, queue, idempotent sync, CSV export | 2 weeks |
| 6. Migration and hardening | Pack importer, penetration review, backup/restore drill, accessibility pass | 2 weeks |
| 7. Dry run | Full offline rehearsal on real devices, in a building with bad wifi | 1 week |

Milestone 7 is not padding. The one thing Phase 1 could not do was test on real hardware, and it is the known gap. Do not repeat it.

---

## Appendix A — Phase 1 file reference

| Item | Value |
|---|---|
| File | `FBSPL_Field_Desk_AppliedNet2026.html` |
| Size | ~1,001,290 bytes |
| Seed credentials | `admin` / `FieldDesk2026` — **change before the event** |
| IndexedDB | `fbspl_an26`, version 1 |
| localStorage state key | `fbspl_an26_state` |
| localStorage file prefix | `fbspl_an26_f_` |
| Preferences key | `fbspl_an26_prefs` |
| Session key | `fbspl_an26_sess` |
| Pack identifier | `fbspl-an26-briefing-pack`, `v: 1` |
| Vendored library | SheetJS (xlsx.js), inline, no network |
| Verification | 105 automated checks, jsdom + fake-indexeddb, both storage modes, all passing. Plus `check_access_rules.js` in this folder (`node check_access_rules.js`), which asserts the six role/visibility rules of §4.2 and §4.5 against the markup. **No real-browser or mobile visual QA.** |
| Changed 2026-09-18 | `applyRole()` mode default; `exportPack()` role guard; `adminonly` added to the Publish card and removed from the Change-my-password card. Nothing else in the auth or storage modules was touched. |

## Appendix B — Key Phase 1 functions, for anyone reading the source

Auth and accounts: `hashPass`, `normUser`, `findUser`, `ensureSeedUser`, `usesSeedPassword`, `signIn`, `signOut`, `writeSession`, `readSession`, `clearSession`, `applyRole`, `setMode`, `showGate`, `renderUsers`, `editUser`, `suggestPass`, `resetPass`, `delUser`.

Ingestion: `importXlsx`, `importCsv`, `parseCsv`, `scoreHeaders`, `normHdr`, `findCol`, `ingestRows`, `healthTone`, `flagOf`, `tenureFrom`, `isNewClient`, `money`, `sigStrip`, `csvTemplate`.

Storage and sync: `openStore`, `save`, `blank`, `bump`, `exportPack`, `importPack`, `doImport`, `blobToB64`, `b64ToBlob`, `exportLeads`, `exportSched`, `showStorage`, `showLocalFileNotice`.

Rendering: `renderAll`, `renderClients`, `renderDocs`, `renderSched`, `renderFeed`, `renderLeads`, `renderToday`, `renderStats`, `renderCounts`, `renderCountdown`, `clientCard`, `meetingRow`.
