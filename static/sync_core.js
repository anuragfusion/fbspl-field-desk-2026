/* Pure sync logic. No IndexedDB, no fetch, no DOM — so Node can test it.
 *
 * Everything here is SYNCHRONOUS on purpose. applyDelta() runs inside a live
 * IndexedDB transaction, and an IndexedDB transaction auto-commits as soon as
 * the microtask queue drains. One stray `await` in here would silently close
 * the transaction mid-apply and lose half the delta without any error.
 */

/* The stores the server owns. `leads` and `outbox` are deliberately NOT here:
 * they are client-authored and must survive a wholesale replace. */
export const REPLICA_STORES = ['clients', 'client_pocs', 'documents', 'meetings', 'updates'];

export const CURSOR_KEY = 'cursor';
export const RECEIPTS_KEY = 'receipts';
export const REBASE_KEY = 'event_rebase';

const LEAD_FIELDS = ['name', 'company', 'email', 'phone', 'client_id', 'interest', 'next_step',
  'notes'];
const normName = (s) => String(s || '').trim().toLowerCase();

/* The phone last synced a DIFFERENT event than the one it is signed in to —
 * e.g. the server was rebuilt and the event recreated with a new id. Its cursor
 * then belongs to the old event and must not be used against the new one.
 * Cursors written before the server sent the event id have progress but no id;
 * those can't be told apart, so they get the (harmless, one-time) rebase too. */
export function eventChanged(cursor, eventId) {
  if (!cursor || !eventId) return false;
  if (cursor.event_id) return cursor.event_id !== eventId;
  return (cursor.version || 0) > 0;
}

/* Re-points every locally held lead at the new event's clients (matched by
 * name, since ids changed) and queues it again. The server dedupes leads on
 * their own id, so re-sending one it already has is harmless. */
export function rebaseLeads({ leads, outbox, oldClientNames, newClients }) {
  const newIds = new Set(newClients.map((c) => c.id));
  const byName = new Map(newClients.map((c) => [normName(c.name), c.id]));
  const remap = (clientId) => {
    if (!clientId) return { client_id: null, lostName: null };
    if (newIds.has(clientId)) return { client_id: clientId, lostName: null };
    const name = oldClientNames[clientId];
    const match = name ? byName.get(normName(name)) : null;
    return match ? { client_id: match, lostName: null } : { client_id: null, lostName: name || null };
  };

  const out = { leads: [], ops: [], unmatched: 0 };
  const seen = new Set();
  const requeue = (id, fields, captured_at) => {
    const { client_id, lostName } = remap(fields.client_id);
    if (fields.client_id && !client_id) out.unmatched += 1;
    const next = { ...fields, client_id };
    /* A client that no longer exists by name: keep what the rep saw in `company`. */
    if (!client_id && lostName && !next.company) next.company = lostName;
    const payload = { captured_at };
    for (const k of LEAD_FIELDS) if (next[k] !== undefined) payload[k] = next[k];
    out.ops.push({ id, type: 'lead', state: 'pending', created_at: captured_at, attempts: 0,
                   payload });
    seen.add(id);
    return next;
  };

  for (const lead of leads) {
    const next = requeue(lead.id, lead, lead.captured_at);
    out.leads.push({ ...lead, client_id: next.client_id, company: next.company, synced: 0 });
  }
  for (const op of outbox) {
    if (op.type === 'lead' && !seen.has(op.id)) {
      requeue(op.id, op.payload || {}, (op.payload && op.payload.captured_at) || op.created_at);
    }
  }
  return out;
}

export function docUrl(id) {
  return `/api/v1/documents/${id}/content`;
}

/* A captive portal answers every request with 200 + an HTML login page.
 * Believing that response is how the replica fills with garbage, so nothing
 * from the network is trusted until it has been through here. */
export function validateSync(payload, contentType, currentVersion = 0) {
  if (!contentType || !String(contentType).toLowerCase().includes('application/json')) {
    throw new Error('not-json: probably a captive portal, treating as offline');
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('bad-payload');
  }
  if (typeof payload.version !== 'number') {
    throw new Error('no-version');
  }
  if (payload.version < currentVersion) {
    throw new Error('stale-version: replayed or cached response');
  }
  return payload;
}

export function validateOutboxReply(reply, contentType) {
  if (!contentType || !String(contentType).toLowerCase().includes('application/json')) {
    throw new Error('not-json: probably a captive portal, treating as offline');
  }
  if (!reply || !Array.isArray(reply.accepted)) {
    throw new Error('bad-outbox-reply');
  }
  return reply;
}

/* Applies a sync payload to the replica.
 *
 * `store` is the minimal synchronous interface both the real IndexedDB adapter
 * and the Node test fake implement: { clear(name), put(name, obj), del(name, key) }.
 *
 * Note this iterates REPLICA_STORES, never Object.keys(payload). If the server
 * ever grows a `leads` key in its sync response, this ignores it rather than
 * handing it a path to overwrite unsynced captures.
 */
export function applyDelta(store, payload) {
  const stats = { put: 0, deleted: 0, cleared: false };

  if (payload.full) {
    for (const name of REPLICA_STORES) store.clear(name);
    stats.cleared = true;
  }

  for (const name of REPLICA_STORES) {
    const records = payload[name];
    if (!Array.isArray(records)) continue;
    for (const rec of records) {
      if (rec && rec.id) { store.put(name, rec); stats.put++; }
    }
  }

  const tombstones = payload.tombstones || {};
  for (const name of REPLICA_STORES) {
    const ids = tombstones[name];
    if (!Array.isArray(ids)) continue;
    for (const id of ids) { store.del(name, id); stats.deleted++; }
  }

  /* Read receipts are per-user rather than per-event, so they are not a replica
   * store — they ride in the payload and live in meta. Still written inside this
   * transaction so they cannot disagree with the cursor. */
  if (Array.isArray(payload.my_receipts)) {
    store.put('meta', { key: RECEIPTS_KEY, items: payload.my_receipts });
    stats.receipts = payload.my_receipts.length;
  }

  /* The cursor moves in the SAME transaction as the data it describes. If these
   * were split, a crash between them would leave the client believing it holds
   * records it never received — and it would never re-request them. */
  store.put('meta', {
    key: CURSOR_KEY,
    version: payload.version,
    event_id: payload.event && payload.event.id,
    last_sync_at: payload.server_time || null,
  });

  return stats;
}

/* "Ready for offline — 14 of 14 documents, 62 MB".
 *
 * Always derived, never a stored counter: iOS can evict the cache overnight and
 * a counter would happily keep reporting 14 of 14 over an empty one. */
export function readiness(docs, cachedUrls) {
  const list = Array.isArray(docs) ? docs : [];
  const have = list.filter((d) => cachedUrls.has(docUrl(d.id)));
  const bytes = (xs) => xs.reduce((s, d) => s + (Number(d.size_bytes) || 0), 0);
  return {
    total: list.length,
    haveCount: have.length,
    totalBytes: bytes(list),
    haveBytes: bytes(have),
    missing: list.filter((d) => !cachedUrls.has(docUrl(d.id))),
    ready: list.length > 0 && have.length === list.length,
  };
}

/* `busy` is whether a download is actually running. Without it a missing file
 * reads "Downloading…" forever, and nobody taps the button that would fetch it. */
export function formatReadiness(r, { busy = false } = {}) {
  const mb = (n) => `${Math.round(n / 1e6)} MB`;   // 1e6: a consumer-facing number
  if (r.total === 0) return 'No documents for this event';
  if (r.ready) return `Ready for offline — ${r.total} of ${r.total} documents, ${mb(r.totalBytes)}`;
  if (busy) {
    return `Downloading — ${r.haveCount} of ${r.total} documents, ${mb(r.haveBytes)} of ${mb(r.totalBytes)}`;
  }
  const n = r.total - r.haveCount;
  return `${n} document${n === 1 ? '' : 's'} not downloaded — tap Sync & prepare offline`;
}

/* The automatic after-sync download skips files above this; the button fetches
 * everything. A size cap, not a wifi check: iOS Safari cannot tell us which it is
 * on. 1e6 to match the MB the badge shows. */
export const AUTO_PREFETCH_MAX_BYTES = 20e6;

/* One run at a time. The 60 s poll must not start a second download on top of
 * the first — but a call made mid-run may know about a document the running one
 * does not, so it queues ONE follow-up run instead of just joining. Any number
 * of calls during a run share that single follow-up; `merge` folds their
 * arguments together (default: the latest call's). */
export function singleFlight(fn, { merge = (_prev, next) => next } = {}) {
  let inflight = null;
  let queued = null;
  let queuedArgs = null;
  const start = (args) => {
    inflight = Promise.resolve().then(() => fn(...args)).finally(() => { inflight = null; });
    return inflight;
  };
  const run = (...args) => {
    if (!inflight) return start(args);
    queuedArgs = queued ? merge(queuedArgs, args) : args;
    if (!queued) {
      const next = () => { queued = null; return start(queuedArgs); };
      queued = inflight.then(next, next);
    }
    return queued;
  };
  run.busy = () => inflight !== null;
  return run;
}

/* Warn BEFORE starting a download that will not fit, not halfway through it. */
export function storageWarning(estimate, needBytes) {
  if (!estimate || !estimate.quota) return null;
  const projected = (estimate.usage || 0) + needBytes;
  if (projected > estimate.quota * 0.8) {
    return `This device may not have room: needs ${Math.round(needBytes / 1e6)} MB, `
         + `about ${Math.round((estimate.quota - (estimate.usage || 0)) / 1e6)} MB free. `
         + 'Free up space before travelling.';
  }
  return null;
}
