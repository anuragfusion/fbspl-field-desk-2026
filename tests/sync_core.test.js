/* node --test tests/
 *
 * §12.3's load-bearing assertion lives here: a wholesale replace must not touch
 * locally captured leads. That is the single most likely bug in the system.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  REPLICA_STORES, applyDelta, readiness, formatReadiness,
  validateSync, validateOutboxReply, storageWarning, docUrl,
} from '../static/sync_core.js';

/* Fake store with the same synchronous shape as the IndexedDB adapter.
 * Crucially it also enforces transaction SCOPE: the real apply transaction is
 * opened over REPLICA_STORES + meta only, so touching `leads` throws in the
 * browser. The fake throws too, or the test would be weaker than production. */
function fakeStore(seed = {}, scope = [...REPLICA_STORES, 'meta']) {
  const data = new Map();
  for (const [name, rows] of Object.entries(seed)) {
    data.set(name, new Map(rows.map((r) => [r.id ?? r.key ?? r.uuid, r])));
  }
  const bag = (name) => {
    if (!scope.includes(name)) {
      throw new Error(`NotFoundError: '${name}' is not in the transaction scope`);
    }
    if (!data.has(name)) data.set(name, new Map());
    return data.get(name);
  };
  return {
    clear: (n) => bag(n).clear(),
    put: (n, o) => bag(n).set(o.id ?? o.key ?? o.uuid, o),
    del: (n, k) => bag(n).delete(k),
    _raw: data,
    _count: (n) => (data.get(n) ? data.get(n).size : 0),
    _get: (n, k) => (data.get(n) ? data.get(n).get(k) : undefined),
  };
}

test('full replace keeps unsynced leads — §12.3', () => {
  const store = fakeStore({
    leads: [
      { id: 'a', name: 'Dana', synced: 0 },
      { id: 'b', name: 'Marc', synced: 0 },
      { id: 'c', name: 'Rosa', synced: 0 },
    ],
    outbox: [{ id: 'a', type: 'lead', state: 'pending' }],
    clients: [{ id: 'old-client' }],
  });

  applyDelta(store, {
    full: true,
    version: 9,
    clients: [{ id: 'new-client', name: 'Meridian' }],
    tombstones: {},
  });

  assert.equal(store._count('leads'), 3, 'unsynced leads must survive a wholesale replace');
  assert.equal(store._count('outbox'), 1, 'the outbox must survive too');
  assert.equal(store._get('leads', 'a').synced, 0, 'and must still be marked unsynced');
  assert.equal(store._count('clients'), 1);
  assert.ok(store._get('clients', 'new-client'), 'server records replaced');
  assert.equal(store._get('clients', 'old-client'), undefined, 'stale record cleared');
  assert.equal(store._get('meta', 'cursor').version, 9);
});

test('applyDelta cannot reach leads even if the server sends them', () => {
  const store = fakeStore({ leads: [{ id: 'a', synced: 0 }] });
  // A hostile or buggy server response carrying a `leads` key must be ignored,
  // not iterated. If applyDelta ever used Object.keys(payload), this throws.
  applyDelta(store, { full: true, version: 2, leads: [{ id: 'x' }], tombstones: {} });
  assert.equal(store._count('leads'), 1);
  assert.equal(store._get('leads', 'a').synced, 0);
});

test('delta upserts by id without clearing', () => {
  const store = fakeStore({
    clients: [{ id: '1', name: 'Meridian', priority: 'watch' }, { id: '2', name: 'Harborline' }],
  });
  applyDelta(store, {
    full: false,
    version: 4,
    clients: [{ id: '1', name: 'Meridian', priority: 'must' }],
    tombstones: {},
  });
  assert.equal(store._count('clients'), 2, 'untouched records stay');
  assert.equal(store._get('clients', '1').priority, 'must', 'changed record replaced whole');
});

test('tombstones delete records the server dropped', () => {
  const store = fakeStore({ documents: [{ id: 'd1' }, { id: 'd2' }] });
  applyDelta(store, { full: false, version: 5, tombstones: { documents: ['d1'] } });
  assert.equal(store._get('documents', 'd1'), undefined);
  assert.ok(store._get('documents', 'd2'));
});

test('cursor advances with the data, in the same call', () => {
  const store = fakeStore();
  applyDelta(store, { full: true, version: 12, event: { id: 'ev' }, server_time: 'T', tombstones: {} });
  const cur = store._get('meta', 'cursor');
  assert.equal(cur.version, 12);
  assert.equal(cur.event_id, 'ev');
});

test('per-user read receipts ride in meta, in the same transaction', () => {
  const store = fakeStore();
  applyDelta(store, {
    full: true, version: 3, tombstones: {},
    my_receipts: [{ update_id: 'U1', read_at: 'T', done_at: 'T' }],
  });
  assert.equal(store._get('meta', 'receipts').items.length, 1);
  assert.equal(store._get('meta', 'cursor').version, 3, 'cursor still advanced alongside');
});

test('a payload without receipts leaves the stored ones alone', () => {
  const store = fakeStore({ meta: [{ key: 'receipts', items: [{ update_id: 'U1' }] }] });
  applyDelta(store, { full: false, version: 4, tombstones: {} });
  assert.equal(store._get('meta', 'receipts').items.length, 1);
});

test('captive portal HTML is rejected, not parsed', () => {
  assert.throws(() => validateSync({ version: 1 }, 'text/html'), /not-json/);
  assert.throws(() => validateOutboxReply({ accepted: [] }, 'text/html'), /not-json/);
});

test('a version that went backwards is rejected as stale', () => {
  assert.throws(() => validateSync({ version: 3 }, 'application/json', 7), /stale-version/);
  assert.doesNotThrow(() => validateSync({ version: 9 }, 'application/json', 7));
});

test('malformed payloads are rejected', () => {
  assert.throws(() => validateSync(null, 'application/json'), /bad-payload/);
  assert.throws(() => validateSync([], 'application/json'), /bad-payload/);
  assert.throws(() => validateSync({}, 'application/json'), /no-version/);
  assert.throws(() => validateOutboxReply({}, 'application/json'), /bad-outbox-reply/);
});

test('readiness counts only documents actually in the cache', () => {
  const docs = [
    { id: 'a', size_bytes: 20e6 },
    { id: 'b', size_bytes: 30e6 },
    { id: 'c', size_bytes: 12e6 },
  ];
  const partial = readiness(docs, new Set([docUrl('a'), docUrl('b')]));
  assert.equal(partial.haveCount, 2);
  assert.equal(partial.ready, false);
  assert.deepEqual(partial.missing.map((d) => d.id), ['c']);

  const all = readiness(docs, new Set(docs.map((d) => docUrl(d.id))));
  assert.equal(all.ready, true);
  assert.equal(formatReadiness(all), 'Ready for offline — 3 of 3 documents, 62 MB');
});

test('an evicted cache reports not-ready rather than a stale count', () => {
  const docs = [{ id: 'a', size_bytes: 20e6 }];
  const gone = readiness(docs, new Set());
  assert.equal(gone.ready, false);
  assert.equal(gone.haveCount, 0);
});

test('storage warning fires before the download, not during', () => {
  assert.ok(storageWarning({ usage: 900e6, quota: 1000e6 }, 200e6));
  assert.equal(storageWarning({ usage: 100e6, quota: 4000e6 }, 62e6), null);
  assert.equal(storageWarning(undefined, 62e6), null, 'estimate() may be unavailable');
});
