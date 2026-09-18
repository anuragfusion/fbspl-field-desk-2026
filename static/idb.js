/* Minimal IndexedDB promise wrapper. Raw API, no Dexie, no build step. */

import { REPLICA_STORES } from './sync_core.js';

export const DB_NAME = 'fielddesk';
export const DB_VERSION = 1;

/* `leads` and `outbox` are client-authored and never appear in a sync payload.
 * They are also kept OUT of the apply transaction's scope — see applyTx below. */
const ALL_STORES = [...REPLICA_STORES, 'meta', 'leads', 'outbox'];

let _db = null;

export function open() {
  if (_db) return Promise.resolve(_db);
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      for (const name of REPLICA_STORES) {
        if (!db.objectStoreNames.contains(name)) db.createObjectStore(name, { keyPath: 'id' });
      }
      if (!db.objectStoreNames.contains('meta')) {
        db.createObjectStore('meta', { keyPath: 'key' });
      }
      if (!db.objectStoreNames.contains('leads')) {
        const s = db.createObjectStore('leads', { keyPath: 'id' });
        /* 0/1, not a boolean: booleans are not valid IndexedDB index keys. */
        s.createIndex('by_synced', 'synced');
      }
      if (!db.objectStoreNames.contains('outbox')) {
        const s = db.createObjectStore('outbox', { keyPath: 'id' });
        s.createIndex('by_state', 'state');
      }
    };
    req.onsuccess = () => { _db = req.result; resolve(_db); };
    req.onerror = () => reject(req.error);
  });
}

function wrap(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function done(tx) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || new Error('transaction aborted'));
  });
}

export async function getAll(name) {
  const db = await open();
  return wrap(db.transaction(name, 'readonly').objectStore(name).getAll());
}

export async function get(name, key) {
  const db = await open();
  return wrap(db.transaction(name, 'readonly').objectStore(name).get(key));
}

export async function put(name, value) {
  const db = await open();
  const tx = db.transaction(name, 'readwrite');
  tx.objectStore(name).put(value);
  return done(tx);
}

export async function del(name, key) {
  const db = await open();
  const tx = db.transaction(name, 'readwrite');
  tx.objectStore(name).delete(key);
  return done(tx);
}

export async function getIndex(name, index, value) {
  const db = await open();
  return wrap(db.transaction(name, 'readonly').objectStore(name).index(index).getAll(value));
}

/* Runs applyDelta against a real transaction.
 *
 * SCOPE IS THE GUARD. The transaction is opened over the replica stores plus
 * meta and nothing else, so IndexedDB itself throws NotFoundError if any code
 * path in here ever tries to touch `leads` or `outbox`. The browser enforces
 * the invariant; we do not rely on remembering it.
 *
 * `fn` must be entirely synchronous. Awaiting anything non-IDB inside a
 * transaction lets it auto-commit early and silently drops the rest of the work.
 */
export async function applyTx(fn) {
  const db = await open();
  const scope = [...REPLICA_STORES, 'meta'];
  const tx = db.transaction(scope, 'readwrite');
  const store = {
    clear: (n) => tx.objectStore(n).clear(),
    put: (n, o) => tx.objectStore(n).put(o),
    del: (n, k) => tx.objectStore(n).delete(k),
  };
  const result = fn(store);           // synchronous by contract
  await done(tx);
  return result;
}

/* Lead capture and its outbox op are ONE transaction. Both or neither — that is
 * what makes the "saved" we show the user honest. */
export async function captureLead(lead, op) {
  const db = await open();
  const tx = db.transaction(['leads', 'outbox'], 'readwrite');
  tx.objectStore('leads').put(lead);
  tx.objectStore('outbox').put(op);
  return done(tx);
}

/* Marks drained ops synced. Also one transaction, so a crash cannot leave a
 * lead marked synced while its op is still queued (or the reverse). */
export async function settleDrain(acceptedIds, rejected) {
  const db = await open();
  const tx = db.transaction(['leads', 'outbox'], 'readwrite');
  const outbox = tx.objectStore('outbox');
  const leads = tx.objectStore('leads');
  for (const id of acceptedIds) {
    outbox.delete(id);
    const req = leads.get(id);
    req.onsuccess = () => {
      const lead = req.result;
      if (lead) { lead.synced = 1; leads.put(lead); }
    };
  }
  for (const r of rejected || []) {
    const req = outbox.get(r.id);
    req.onsuccess = () => {
      const op = req.result;
      if (op) { op.state = 'rejected'; op.error = r.message || r.code; outbox.put(op); }
    };
  }
  return done(tx);
}

export async function wipeEventData() {
  const db = await open();
  const tx = db.transaction(ALL_STORES, 'readwrite');
  for (const name of ALL_STORES) tx.objectStore(name).clear();
  await done(tx);
  for (const key of await caches.keys()) await caches.delete(key);
}
