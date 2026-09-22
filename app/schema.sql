-- FBSPL Field Desk — Phase 2 schema (handoff §9)
-- Written Day 1 and frozen. Mid-week schema churn is expensive.
--
-- Targets Postgres/Supabase, via DATABASE_URL. This file used to be ported down to
-- SQLite substitutions; most of that direction is now reversed:
--   citext  -> CITEXT   (native case-insensitive uniqueness, §12.2)
--   jsonb   -> JSONB    (native; psycopg2 round-trips these as dict/list automatically)
--   boolean -> BOOLEAN  (native)
-- Two substitutions are kept even on Postgres, deliberately:
--   uuid        -> TEXT  (ids are opaque strings everywhere in the app — JSON payloads,
--                          sync cursors, client-generated lead ids — and a real `uuid`
--                          column would come back from psycopg2 as a uuid.UUID object,
--                          which isn't directly JSON-serialisable)
--   timestamptz -> TEXT  (every timestamp in this app is produced by now_iso() as an
--                          ISO-8601 string and is compared/serialised as a string end to
--                          end, e.g. exact string equality in tests and in sync payloads;
--                          a real timestamptz column would come back as a datetime object)

CREATE EXTENSION IF NOT EXISTS citext;

-- §9.1 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id                   TEXT PRIMARY KEY,
  username             CITEXT NOT NULL UNIQUE,
  email                CITEXT UNIQUE,
  full_name            TEXT NOT NULL,
  role                 TEXT NOT NULL CHECK (role IN ('admin','field')),
  password_hash        TEXT,                    -- NULL when SSO-only (not used yet)
  must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
  disabled_at          TEXT,             -- soft disable, preserves lead attribution
  last_login_at        TEXT,
  failed_attempts      INTEGER NOT NULL DEFAULT 0,
  last_failed_at       TEXT,             -- so §12.1's "within 15 minutes" is a real window
  locked_until         TEXT,
  created_by           TEXT REFERENCES users(id),
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);

-- §9.2 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  venue       TEXT,
  city        TEXT,
  country     TEXT,
  starts_on   TEXT,                             -- date; drives the day tabs
  ends_on     TEXT,
  timezone    TEXT NOT NULL DEFAULT 'America/New_York',  -- office is IST; this matters
  version     INTEGER NOT NULL DEFAULT 0,       -- monotonic; bumped on any publish
  archived_at TEXT
);

-- §9.3 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event_members (
  id         TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  user_id    TEXT NOT NULL REFERENCES users(id),
  event_role TEXT NOT NULL CHECK (event_role IN ('organiser','attendee')),
  added_at   TEXT NOT NULL,
  UNIQUE (event_id, user_id)
);

-- §9.4 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS clients (
  id              TEXT PRIMARY KEY,
  event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  priority        TEXT CHECK (priority IN ('must','target','watch')),
  owner           TEXT,                          -- FBSPL person meeting them
  account_manager TEXT,
  location        TEXT,
  product         TEXT,
  tags            JSONB NOT NULL DEFAULT '[]',   -- JSON array
  summary         TEXT,
  talking_points  JSONB NOT NULL DEFAULT '[]',   -- JSON array ("say" in Phase 1)
  avoid_points    JSONB NOT NULL DEFAULT '[]',   -- JSON array ("avoid" in Phase 1)
  signals         JSONB NOT NULL DEFAULT '{}',   -- JSON object; derived chips
  source_row_hash TEXT,                          -- idempotent workbook re-import
  version         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_clients_event_priority ON clients (event_id, priority);
CREATE INDEX IF NOT EXISTS ix_clients_version        ON clients (event_id, version);
CREATE INDEX IF NOT EXISTS ix_clients_name           ON clients (event_id, name);

-- §9.5 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_pocs (
  id         TEXT PRIMARY KEY,
  client_id  TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  name       TEXT,
  title      TEXT,
  note       TEXT,
  sort_order INTEGER NOT NULL DEFAULT 0,
  version    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_pocs_client  ON client_pocs (client_id);
CREATE INDEX IF NOT EXISTS ix_pocs_version ON client_pocs (event_id, version);

-- §9.6 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
  id              TEXT PRIMARY KEY,
  event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  client_id       TEXT REFERENCES clients(id) ON DELETE SET NULL,  -- NULL = general
  filename        TEXT NOT NULL,
  mime_type       TEXT NOT NULL,                 -- sniffed server-side, never trusted
  size_bytes      INTEGER NOT NULL,              -- the captive-portal guard compares this
  checksum_sha256 TEXT NOT NULL,                 -- lets the client skip re-downloading
  storage_key     TEXT NOT NULL,                 -- ./storage/<sha256>
  note            TEXT,
  uploaded_by     TEXT REFERENCES users(id),
  version         INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_docs_event   ON documents (event_id);
CREATE INDEX IF NOT EXISTS ix_docs_version ON documents (event_id, version);

-- §9.7 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meetings (
  id               TEXT PRIMARY KEY,
  event_id         TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  client_id        TEXT REFERENCES clients(id) ON DELETE SET NULL,
  title            TEXT,
  meeting_date     TEXT,                         -- date
  start_time       TEXT,                         -- local to the event timezone
  duration_minutes INTEGER,
  location         TEXT,                         -- booth number, restaurant, hall
  owner            TEXT,
  attendees        TEXT,
  status           TEXT NOT NULL DEFAULT 'scheduled'
                     CHECK (status IN ('scheduled','done','cancelled')),
  notes            TEXT,
  version          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_meetings_when    ON meetings (event_id, meeting_date, start_time);
CREATE INDEX IF NOT EXISTS ix_meetings_version ON meetings (event_id, version);

-- §9.8 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS updates (
  id         TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  client_id  TEXT REFERENCES clients(id) ON DELETE SET NULL,
  title      TEXT NOT NULL,
  body       TEXT,
  level      TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info','urgent')),
  pinned     BOOLEAN NOT NULL DEFAULT FALSE,
  is_action  BOOLEAN NOT NULL DEFAULT FALSE,      -- requires acknowledgement
  author_id  TEXT REFERENCES users(id),
  version    INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_updates_version ON updates (event_id, version);

-- §9.9 -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS update_receipts (
  update_id TEXT NOT NULL REFERENCES updates(id) ON DELETE CASCADE,
  user_id   TEXT NOT NULL REFERENCES users(id),
  read_at   TEXT,
  done_at   TEXT,
  PRIMARY KEY (update_id, user_id)
);

-- §9.10 ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leads (
  id            TEXT PRIMARY KEY,                -- CLIENT-generated; offline + idempotent sync
  event_id      TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  client_id     TEXT REFERENCES clients(id) ON DELETE SET NULL,
  name          TEXT,
  company       TEXT,
  email         TEXT,
  phone         TEXT,
  interest      TEXT,
  next_step     TEXT,
  notes         TEXT,
  captured_by   TEXT REFERENCES users(id),
  captured_at   TEXT NOT NULL,             -- DEVICE time at capture. Never overwritten.
  synced_at     TEXT NOT NULL,             -- server receipt
  clock_skew_ms INTEGER,                          -- surfaced to admin, never auto-corrected
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_leads_event ON leads (event_id, captured_at);
CREATE INDEX IF NOT EXISTS ix_leads_by    ON leads (captured_by);

-- §9.11 ----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
  id          BIGSERIAL PRIMARY KEY,
  actor_id    TEXT REFERENCES users(id),
  action      TEXT NOT NULL,                     -- user.create, user.reset_password, login.failed, ...
  entity_type TEXT,
  entity_id   TEXT,
  detail      JSONB NOT NULL DEFAULT '{}',       -- JSON
  ip          TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log (created_at);
CREATE INDEX IF NOT EXISTS ix_audit_actor   ON audit_log (actor_id, created_at);

-- §9.12 ----------------------------------------------------------------------
-- Server-side sessions rather than stateless tokens, so a lost phone at a
-- conference can be revoked from the admin panel within seconds.
CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   TEXT NOT NULL UNIQUE,             -- sha256 of the opaque bearer token
  device_label TEXT,
  user_agent   TEXT,
  issued_at    TEXT NOT NULL,
  expires_at   TEXT NOT NULL,
  revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions (user_id);

-- §9.13 ----------------------------------------------------------------------
-- Created to match the spec. Stays EMPTY by design: the admin sets passwords
-- directly, so there is no token flow and no mail server to depend on.
CREATE TABLE IF NOT EXISTS password_resets (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  used_at    TEXT,
  issued_by  TEXT REFERENCES users(id)
);

-- Delta sync support (§8.2) --------------------------------------------------
-- Tombstones. A deleted row must still be reportable to a client whose cursor
-- predates the deletion, or the record lingers on the device forever.
CREATE TABLE IF NOT EXISTS deleted_rows (
  entity    TEXT NOT NULL CHECK (entity IN ('client','poc','document','meeting','update')),
  entity_id TEXT NOT NULL,
  event_id  TEXT NOT NULL,
  version   INTEGER NOT NULL,
  PRIMARY KEY (entity, entity_id)
);
CREATE INDEX IF NOT EXISTS ix_tombstone_version ON deleted_rows (event_id, version);

-- Idempotency ledger for non-lead outbox ops (lead edits, update receipts).
-- Leads dedupe on their own client-generated primary key; everything else
-- needs somewhere to record "this op was already applied".
CREATE TABLE IF NOT EXISTS op_log (
  id         TEXT PRIMARY KEY,                   -- client-generated op uuid
  kind       TEXT NOT NULL,
  applied_at TEXT NOT NULL
);
