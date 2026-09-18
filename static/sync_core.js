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

export function formatReadiness(r) {
  const mb = (n) => `${Math.round(n / 1e6)} MB`;   // 1e6: a consumer-facing number
  if (r.total === 0) return 'No documents for this event';
  return r.ready
    ? `Ready for offline — ${r.total} of ${r.total} documents, ${mb(r.totalBytes)}`
    : `Downloading — ${r.haveCount} of ${r.total} documents, ${mb(r.haveBytes)} of ${mb(r.totalBytes)}`;
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
