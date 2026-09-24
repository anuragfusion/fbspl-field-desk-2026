/* node --test tests/
 *
 * §12.3's load-bearing assertion lives here: a wholesale replace must not touch
 * locally captured leads. That is the single most likely bug in the system.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  REPLICA_STORES, applyDelta, readiness, formatReadiness, singleFlight, AUTO_PREFETCH_MAX_BYTES,
  validateSync, validateOutboxReply, storageWarning, docUrl,
  eventChanged, rebaseLeads,
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

test('a missing document says "not downloaded", not "Downloading", when nothing runs', () => {
  const docs = [{ id: 'a', size_bytes: 20e6 }, { id: 'b', size_bytes: 30e6 }];
  const r = readiness(docs, new Set([docUrl('a')]));
  assert.equal(formatReadiness(r), '1 document not downloaded — tap Sync & prepare offline');
  assert.equal(formatReadiness(readiness(docs, new Set())),
    '2 documents not downloaded — tap Sync & prepare offline');
  assert.match(formatReadiness(r, { busy: true }), /^Downloading — 1 of 2 documents/);
});

test('singleFlight: overlapping calls never run fn concurrently, and queue one follow-up', async () => {
  let running = 0; let maxRunning = 0; let runs = 0;
  const release = [];
  const job = singleFlight(async () => {
    running += 1; runs += 1; maxRunning = Math.max(maxRunning, running);
    await new Promise((r) => release.push(r));
    running -= 1;
    return runs;
  });
  const first = job();
  assert.equal(job.busy(), true);
  const second = job();        // mid-run: must not start now
  const third = job();         // shares the same follow-up as second
  assert.equal(second, third);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(runs, 1);
  release.shift()();
  assert.equal(await first, 1);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(runs, 2);       // the follow-up started only after the first ended
  release.shift()();
  assert.equal(await second, 2);
  assert.equal(maxRunning, 1);
  assert.equal(job.busy(), false);
});

test('singleFlight: calls queued mid-run are merged into one follow-up', async () => {
  const seen = [];
  const release = [];
  const job = singleFlight(async (o) => { seen.push(o); await new Promise((r) => release.push(r)); },
    { merge: ([a], [b]) => [{ auto: a.auto && b.auto }] });
  const first = job({ auto: true });
  const button = job({ auto: false });
  job({ auto: true });          // a poll after the button must not re-cap it
  await new Promise((r) => setTimeout(r, 0));
  release.shift()();
  await first;
  await new Promise((r) => setTimeout(r, 0));
  release.shift()();
  await button;
  assert.deepEqual(seen, [{ auto: true }, { auto: false }]);
});

test('auto-download cap is 20 MB', () => {
  assert.equal(AUTO_PREFETCH_MAX_BYTES, 20e6);
});

test('singleFlight: a failed run does not jam later ones', async () => {
  let n = 0;
  const job = singleFlight(async () => { n += 1; if (n === 1) throw new Error('wifi'); return n; });
  await assert.rejects(job(), /wifi/);
  assert.equal(await job(), 2);
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

test('a recreated event is detected; a first sign-in or the same event is not', () => {
  assert.equal(eventChanged({ version: 12, event_id: 'old' }, 'new'), true);
  assert.equal(eventChanged({ version: 12, event_id: 'same' }, 'same'), false);
  assert.equal(eventChanged({ version: 0, event_id: null }, 'new'), false, 'first sign-in');
  assert.equal(eventChanged(undefined, 'new'), false);
});

test('a cursor from before the server sent the event id gets the one-time rebase', () => {
  /* Every phone in the field today holds one of these: progress, no event id. */
  assert.equal(eventChanged({ version: 12, event_id: undefined }, 'any'), true);
  assert.equal(eventChanged({ version: 12 }, 'any'), true);
});

test('after a full sync the cursor carries the event id, so no second rebase', () => {
  const store = fakeStore();
  applyDelta(store, { full: true, version: 3, event: { id: 'ev-1' }, tombstones: {} });
  const cursor = store._get('meta', 'cursor');
  assert.equal(cursor.event_id, 'ev-1');
  assert.equal(eventChanged(cursor, 'ev-1'), false);
});

test('rebase re-points leads at the new clients by name and re-queues all of them', () => {
  const plan = rebaseLeads({
    leads: [
      { id: 'L1', name: 'Dana', client_id: 'old-meridian', captured_at: 't1', synced: 1,
        captured_by_name: 'Priya' },
      { id: 'L2', name: 'Sam', client_id: null, company: 'Walk-in', captured_at: 't2', synced: 1 },
      { id: 'L3', name: 'Jo', client_id: 'old-gone', captured_at: 't3', synced: 0 },
    ],
    outbox: [
      { id: 'L3', type: 'lead', state: 'rejected', payload: { name: 'Jo', client_id: 'old-gone',
        captured_at: 't3' } },
      { id: 'R1', type: 'receipt', state: 'pending', payload: { update_id: 'old-update' } },
    ],
    oldClientNames: { 'old-meridian': 'Meridian Insurance', 'old-gone': 'Closed Agency' },
    newClients: [{ id: 'new-meridian', name: '  meridian insurance ' }, { id: 'new-acme', name: 'Acme' }],
  });

  const lead = (id) => plan.leads.find((l) => l.id === id);
  const op = (id) => plan.ops.find((o) => o.id === id);
  assert.equal(lead('L1').client_id, 'new-meridian', 'matched by name, case/space-insensitive');
  assert.equal(lead('L1').synced, 0, 'marked unsynced so the UI shows it going up again');
  assert.equal(lead('L2').client_id, null);
  assert.equal(lead('L3').client_id, null, 'no client by that name any more');
  assert.equal(lead('L3').company, 'Closed Agency', 'what the rep saw is kept, not lost');
  assert.equal(plan.unmatched, 1);

  assert.deepEqual(plan.ops.map((o) => o.id).sort(), ['L1', 'L2', 'L3'], 'one op per lead, no duplicates');
  for (const o of plan.ops) assert.equal(o.state, 'pending', 'a rejected op is retried');
  assert.equal(op('L1').payload.client_id, 'new-meridian');
  assert.equal(op('L1').payload.captured_at, 't1', 'capture time survives the rebase');
  assert.equal(op('L1').payload.captured_by_name, undefined, 'only real lead fields are sent');
  assert.equal(op('R1'), undefined, 'receipts are left alone, not rewritten as leads');
});

test('rebase is idempotent: running it on already-rebased leads changes nothing', () => {
  const args = {
    leads: [{ id: 'L1', name: 'Dana', client_id: 'new-meridian', captured_at: 't1' }],
    outbox: [],
    oldClientNames: {},
    newClients: [{ id: 'new-meridian', name: 'Meridian Insurance' }],
  };
  const plan = rebaseLeads(args);
  assert.equal(plan.leads[0].client_id, 'new-meridian');
  assert.equal(plan.unmatched, 0);
});

test('an outbox lead with no local record is still re-queued and re-pointed', () => {
  const plan = rebaseLeads({
    leads: [],
    outbox: [{ id: 'L9', type: 'lead', state: 'pending', created_at: 't9',
               payload: { name: 'X', client_id: 'old-m', captured_at: 't9' } }],
    oldClientNames: { 'old-m': 'Meridian' },
    newClients: [{ id: 'new-m', name: 'Meridian' }],
  });
  assert.equal(plan.ops[0].payload.client_id, 'new-m');
  assert.equal(plan.ops[0].payload.captured_at, 't9');
});
