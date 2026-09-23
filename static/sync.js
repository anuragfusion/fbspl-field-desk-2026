/* Sync orchestration: push the outbox, then pull the delta.
 *
 * Push first. The outbox holds leads that exist nowhere else on earth; briefs
 * are recoverable from the server forever. Spend a flaky convention-centre
 * connection on the irreplaceable bytes first.
 */

import * as idb from './idb.js';
import {
  REBASE_KEY, applyDelta, eventChanged, rebaseLeads, validateOutboxReply, validateSync,
} from './sync_core.js';

const TIMEOUT_MS = 15000;
const BATCH = 100;

export const bus = new EventTarget();
const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));

async function fetchJson(url, options = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(url, { ...options, signal: ctrl.signal, cache: 'no-store' });
    const ct = res.headers.get('content-type') || '';
    if (res.status === 401) throw Object.assign(new Error('unauthorised'), { status: 401 });
    if (!res.ok) throw Object.assign(new Error(`http ${res.status}`), { status: res.status });
    /* Read as text and parse ourselves so a captive portal's HTML fails here
     * rather than somewhere deep in the apply path. */
    const body = await res.text();
    let parsed;
    try { parsed = JSON.parse(body); } catch { throw new Error('not-json'); }
    return { parsed, contentType: ct };
  } finally {
    clearTimeout(timer);
  }
}

export async function getCursor() {
  return (await idb.get('meta', 'cursor')) || { version: 0, event_id: null };
}

async function pushOutbox(eventId) {
  let pushed = 0;
  for (;;) {
    const pending = (await idb.getIndex('outbox', 'by_state', 'pending')).slice(0, BATCH);
    if (pending.length === 0) return pushed;

    const { parsed, contentType } = await fetchJson(
      `/api/v1/events/${eventId}/outbox`,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          client_sent_at: new Date().toISOString(),
          ops: pending.map((o) => ({
            id: o.id, type: o.type, payload: o.payload, created_at: o.created_at,
          })),
        }),
      },
    );
    const reply = validateOutboxReply(parsed, contentType);

    await idb.settleDrain(reply.accepted, reply.rejected);
    pushed += reply.accepted.length;
    emit('outbox', { accepted: reply.accepted.length, rejected: reply.rejected });

    if (reply.accepted.length === 0 && (reply.rejected || []).length === 0) return pushed;
    if (pending.length < BATCH) return pushed;
  }
}

async function pullDelta(eventId, { full = false } = {}) {
  const cursor = await getCursor();
  const since = full ? 0 : (cursor.version || 0);
  const { parsed, contentType } = await fetchJson(
    `/api/v1/events/${eventId}/sync?since=${since}`,
  );
  const payload = validateSync(parsed, contentType, since);

  /* Everything is fetched and validated BEFORE the transaction opens. Nothing
   * inside applyTx awaits — see the note in idb.applyTx. */
  const stats = await idb.applyTx((store) => applyDelta(store, payload));
  emit('pulled', { version: payload.version, ...stats });
  return payload;
}

/* Runs before the push: leads captured against the old event reference client
 * ids that no longer exist and would be rejected. The old client names are
 * saved to meta FIRST, because the full pull below wipes the clients store —
 * if the phone dies mid-way, the next sync resumes from that marker. */
async function rebaseIfEventChanged(eventId) {
  let marker = await idb.get('meta', REBASE_KEY);
  if (!marker) {
    if (!eventChanged(await getCursor(), eventId)) return null;
    const clients = await idb.getAll('clients');
    marker = { key: REBASE_KEY, client_names: Object.fromEntries(clients.map((c) => [c.id, c.name])) };
    await idb.put('meta', marker);
  }
  if ((await getCursor()).event_id !== eventId) await pullDelta(eventId, { full: true });

  const [leads, outbox, newClients] = await Promise.all([
    idb.getAll('leads'), idb.getAll('outbox'), idb.getAll('clients'),
  ]);
  const plan = rebaseLeads({ leads, outbox, oldClientNames: marker.client_names || {}, newClients });
  await idb.applyRebase(plan, REBASE_KEY);
  emit('rebased', { leads: plan.ops.length, unmatched: plan.unmatched });
  return plan;
}

/* Single-flight across tabs, for free, via the native Web Locks API. */
export async function sync(eventId, { reason = 'manual' } = {}) {
  if (!navigator.onLine) { emit('offline', { reason }); return { skipped: 'offline' }; }

  return navigator.locks.request('fielddesk-sync', { ifAvailable: true }, async (lock) => {
    if (!lock) return { skipped: 'in-flight' };
    emit('start', { reason });
    try {
      await rebaseIfEventChanged(eventId);
      const pushed = await pushOutbox(eventId);
      const payload = await pullDelta(eventId);
      emit('done', { reason, pushed, version: payload.version });
      return { pushed, version: payload.version, payload };
    } catch (err) {
      if (err.status === 401) {
        /* The session was revoked or expired. Say so plainly instead of
         * rendering a white screen. */
        emit('unauthorised', {});
      } else {
        emit('failed', { reason, message: err.message });
      }
      return { error: err.message };
    }
  });
}

/* Triggers: foreground, reconnect, manual. A 60s poll runs only while the page
 * is visible — no background polling burning battery on the exhibition floor. */
export function startAutoSync(eventId) {
  let timer = null;
  const tick = () => sync(eventId, { reason: 'poll' });

  const arm = () => {
    clearInterval(timer);
    if (document.visibilityState === 'visible') timer = setInterval(tick, 60000);
  };

  document.addEventListener('visibilitychange', () => {
    arm();
    if (document.visibilityState === 'visible') sync(eventId, { reason: 'foreground' });
  });
  window.addEventListener('online', () => sync(eventId, { reason: 'reconnect' }));
  arm();
}
