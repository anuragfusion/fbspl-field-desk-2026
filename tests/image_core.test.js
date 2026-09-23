/* node --test tests/ — pure image-lead helpers. */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_CONFIG, sanitizeConfig, sniffImage, fitWithin, shouldKeepOriginal, crossedMilestones,
  nextRetryDelayMs, classifyError, uploadTimeoutMs, formatBytes, shortId, logLine, logEntry,
  leadStatus, canUpload, renumberSlots, newId, ownerKey, ID_RE,
} from '../static/image_core.js';

const bytes = (...parts) => new Uint8Array(parts.flatMap((p) => (
  typeof p === 'string' ? [...p].map((c) => c.charCodeAt(0)) : p)));
const ftyp = (brand) => bytes([0, 0, 0, 0x18], 'ftyp', brand, [0, 0, 0, 0]);

test('DEFAULT_CONFIG matches the server defaults', () => {
  assert.deepEqual({ ...DEFAULT_CONFIG }, {
    max_dimension: 1600, jpeg_quality: 0.8, max_upload_bytes: 5242880,
    log_level: 'info', console_log: true, max_photos: 2,
  });
});

test('sanitizeConfig keeps valid values and defaults each bad field', () => {
  assert.deepEqual(sanitizeConfig({
    max_dimension: 2048, jpeg_quality: 0.5, max_upload_bytes: 102400,
    log_level: 'debug', console_log: false, max_photos: 9, accepted_types: [],
  }), {
    max_dimension: 2048, jpeg_quality: 0.5, max_upload_bytes: 102400,
    log_level: 'debug', console_log: false, max_photos: 2,
  });
  const bad = sanitizeConfig({
    max_dimension: 639, jpeg_quality: 1.01, max_upload_bytes: 20971521,
    log_level: 'loud', console_log: 'yes',
  });
  assert.deepEqual(bad, { ...DEFAULT_CONFIG });
  assert.equal(sanitizeConfig({ max_dimension: 1600.5 }).max_dimension, 1600);
  assert.equal(sanitizeConfig({ max_dimension: 4096 }).max_dimension, 4096);
  assert.equal(sanitizeConfig({ jpeg_quality: 0.3 }).jpeg_quality, 0.3);
  assert.equal(sanitizeConfig({ jpeg_quality: NaN }).jpeg_quality, 0.8);
  assert.equal(sanitizeConfig({ max_upload_bytes: 102399 }).max_upload_bytes, 5242880);
  assert.equal(sanitizeConfig({ log_level: 'OFF' }).log_level, 'off');
  assert.deepEqual(sanitizeConfig(null), { ...DEFAULT_CONFIG });
  assert.deepEqual(sanitizeConfig('nope'), { ...DEFAULT_CONFIG });
});

test('sniffImage uses magic bytes, including HEIC/HEIF brands, and rejects AVIF', () => {
  assert.equal(sniffImage(bytes([0xff, 0xd8, 0xff, 0xe0])), 'image/jpeg');
  assert.equal(sniffImage(bytes([0x89], 'PNG', [0x0d, 0x0a, 0x1a, 0x0a])), 'image/png');
  assert.equal(sniffImage(bytes('RIFF', [1, 2, 3, 4], 'WEBP')), 'image/webp');
  for (const b of ['heic', 'heix', 'heim', 'heis', 'hevc', 'hevx']) assert.equal(sniffImage(ftyp(b)), 'image/heic');
  for (const b of ['mif1', 'msf1', 'heif']) assert.equal(sniffImage(ftyp(b)), 'image/heif');
  assert.equal(sniffImage(ftyp('avif')), null);
  assert.equal(sniffImage(ftyp('avis')), null);
  assert.equal(sniffImage(bytes('RIFF', [1, 2, 3, 4], 'WAVE')), null);
  assert.equal(sniffImage(bytes('GIF89a')), null);
  assert.equal(sniffImage(bytes([0xff, 0xd8])), null);
  assert.equal(sniffImage(new Uint8Array()), null);
  assert.equal(sniffImage(null), null);
  assert.equal(sniffImage(bytes([0xff, 0xd8, 0xff]).buffer), 'image/jpeg');
});

test('fitWithin keeps aspect ratio, rounds to integers and never upscales', () => {
  assert.deepEqual(fitWithin(4032, 3024, 1600), { width: 1600, height: 1200, scaled: true });
  assert.deepEqual(fitWithin(3024, 4032, 1600), { width: 1200, height: 1600, scaled: true });
  assert.deepEqual(fitWithin(3000, 3000, 1600), { width: 1600, height: 1600, scaled: true });
  assert.deepEqual(fitWithin(1000, 333, 640), { width: 640, height: 213, scaled: true });
  assert.deepEqual(fitWithin(10000, 1, 1600), { width: 1600, height: 1, scaled: true });
  assert.deepEqual(fitWithin(800, 600, 1600), { width: 800, height: 600, scaled: false });
  assert.deepEqual(fitWithin(1600, 900, 1600), { width: 1600, height: 900, scaled: false });
  const r = fitWithin(4000, 2999, 1600);
  assert.ok(Number.isInteger(r.width) && Number.isInteger(r.height));
});

test('shouldKeepOriginal only for small JPEGs within the dimension limit', () => {
  const cfg = DEFAULT_CONFIG;
  assert.equal(shouldKeepOriginal({ mime: 'image/jpeg', width: 1600, height: 1200, size: 1048576 }, cfg), true);
  assert.equal(shouldKeepOriginal({ mime: 'image/jpeg', width: 1601, height: 1200, size: 500000 }, cfg), false);
  assert.equal(shouldKeepOriginal({ mime: 'image/jpeg', width: 1200, height: 1600, size: 1048577 }, cfg), false);
  assert.equal(shouldKeepOriginal({ mime: 'image/png', width: 800, height: 600, size: 1000 }, cfg), false);
  assert.equal(shouldKeepOriginal({ mime: 'image/heic', width: 800, height: 600, size: 1000 }, cfg), false);
  assert.equal(shouldKeepOriginal({ mime: 'image/jpeg', width: 2000, height: 100, size: 1000 },
    { ...cfg, max_dimension: 2048 }), true);
});

test('crossedMilestones reports each newly passed milestone once', () => {
  assert.deepEqual(crossedMilestones(0, 0, 100), { pct: 0, crossed: [] });
  assert.deepEqual(crossedMilestones(0, 24, 100), { pct: 24, crossed: [] });
  assert.deepEqual(crossedMilestones(24, 25, 100), { pct: 25, crossed: [25] });
  assert.deepEqual(crossedMilestones(25, 60, 100), { pct: 60, crossed: [50] });
  assert.deepEqual(crossedMilestones(0, 100, 100), { pct: 100, crossed: [25, 50, 75, 100] });
  assert.deepEqual(crossedMilestones(0, 5000, 5000), { pct: 100, crossed: [25, 50, 75, 100] });
  assert.deepEqual(crossedMilestones(100, 100, 100), { pct: 100, crossed: [] });
  assert.deepEqual(crossedMilestones(50, 10, 100), { pct: 50, crossed: [] });
  assert.deepEqual(crossedMilestones(0, 10, 0), { pct: 0, crossed: [] });
  assert.deepEqual(crossedMilestones(0, 200, 100), { pct: 100, crossed: [25, 50, 75, 100] });
});

test('nextRetryDelayMs doubles from 30 s and caps at 15 min', () => {
  assert.equal(nextRetryDelayMs(0), 30000);
  assert.equal(nextRetryDelayMs(1), 60000);
  assert.equal(nextRetryDelayMs(4), 480000);
  assert.equal(nextRetryDelayMs(5), 900000);
  assert.equal(nextRetryDelayMs(50), 900000);
});

test('classifyError table', () => {
  const cases = [
    [0, null, 'retry'], [401, 'UNAUTHORISED', 'auth'], [408, null, 'retry'], [429, null, 'retry'],
    [500, null, 'retry'], [502, 'STORAGE_ERROR', 'retry'], [503, 'STORAGE_UNAVAILABLE', 'retry'],
    [504, null, 'retry'], [400, 'CHECKSUM_MISMATCH', 'retry'], [400, 'INVALID_CLIENT', 'permanent'],
    [400, 'REJECTED_UPLOAD', 'permanent'], [400, null, 'permanent'], [403, 'FORBIDDEN', 'permanent'],
    [404, 'NOT_FOUND', 'permanent'], [409, 'ID_CONFLICT', 'permanent'], [409, 'LEAD_LOCKED', 'permanent'],
    [409, 'SLOT_LOCKED', 'permanent'], [413, 'TOO_LARGE', 'permanent'],
  ];
  for (const [status, code, kind] of cases) assert.equal(classifyError(status, code), kind, `${status} ${code}`);
});

test('uploadTimeoutMs grows with size and is capped', () => {
  assert.equal(uploadTimeoutMs(0), 30000);
  assert.equal(uploadTimeoutMs(1), 31000);
  assert.equal(uploadTimeoutMs(10240), 31000);
  assert.equal(uploadTimeoutMs(10241), 32000);
  assert.equal(uploadTimeoutMs(1024 * 1024), 30000 + 103 * 1000);
  assert.equal(uploadTimeoutMs(20 * 1024 * 1024), 300000);
});

test('formatBytes', () => {
  assert.equal(formatBytes(900), '900 B');
  assert.equal(formatBytes(0), '0 B');
  assert.equal(formatBytes(412 * 1024), '412 KB');
  assert.equal(formatBytes(3.8 * 1024 * 1024), '3.8 MB');
  assert.equal(formatBytes(1024 * 1024), '1.0 MB');
});

test('shortId and ownerKey', () => {
  assert.equal(shortId('abcdef1234567890'), 'abcdef12');
  assert.equal(shortId(null), '-');
  assert.equal(ownerKey({ user: { id: 'u1' }, event: { id: 'e1' } }), 'u1|e1');
});

test('logLine matches the server DEVICE format', () => {
  const id = '1234567890abcdef';
  assert.equal(logLine({ lead_id: id, photo: 1, photo_count: 2, pct: 25, stage: 'compress', msg: 'read 4032x3024' }),
    '[image-lead] DEVICE lead=12345678 photo=1/2   25% compress read 4032x3024');
  assert.equal(logLine({ lead_id: id, photo: 2, photo_count: 2, pct: 100, stage: 'upload', msg: 'received by server' }),
    '[image-lead] DEVICE lead=12345678 photo=2/2  100% upload received by server');
  assert.equal(logLine({ lead_id: id, photo: null, photo_count: 2, pct: null, stage: 'info', msg: 'submitted' }),
    '[image-lead] DEVICE lead=12345678  info submitted');
  assert.equal(logLine({ lead_id: id, photo: 1, photo_count: 1, pct: 0, stage: 'error', msg: 'a\nb' }),
    '[image-lead] DEVICE lead=12345678 photo=1/1    0% error a b');
});

test('logEntry strips control characters and caps fields', () => {
  const e = logEntry({ at: '2026-09-23T10:00:00Z\n', photo: 2, stage: 'compress', pct: 50, msg: 'line1\nfake [image-lead] SERVER\r\x00x' });
  assert.equal(e.at, '2026-09-23T10:00:00Z');
  assert.equal(e.msg, 'line1 fake [image-lead] SERVER x');
  assert.ok(!/[\x00-\x1f\x7f]/.test(e.msg));
  assert.deepEqual([e.photo, e.stage, e.pct], [2, 'compress', 50]);
  assert.equal(logEntry({ msg: 'x'.repeat(500) }).msg.length, 200);
  const odd = logEntry({ photo: 0, stage: 'hack', pct: 33, msg: null });
  assert.deepEqual([odd.photo, odd.stage, odd.pct, odd.msg], [null, 'info', null, '']);
  assert.ok(odd.at.length > 0);
});

test('leadStatus keys and labels', () => {
  const ph = (...states) => states.map((state, i) => ({ slot: i + 1, state }));
  assert.deepEqual(leadStatus({ state: 'draft', photo_count: 0 }, []), { key: 'draft', label: 'Draft', uploaded: 0, total: 0 });
  assert.equal(leadStatus({ state: 'queued', photo_count: 2 }, ph('ready', 'ready')).label, 'Waiting for signal');
  assert.equal(leadStatus({ state: 'uploading', photo_count: 2 }, ph('ready', 'ready')).label, 'Uploading 1 of 2');
  assert.deepEqual(leadStatus({ state: 'uploading', photo_count: 2 }, ph('uploaded', 'ready')),
    { key: 'uploading', label: 'Uploading 2 of 2', uploaded: 1, total: 2 });
  assert.equal(leadStatus({ state: 'done', photo_count: 1 }, ph('uploaded')).label, 'Uploaded');
  assert.equal(leadStatus({ state: 'failed', photo_count: 1 }, ph('ready')).label, 'Upload failed');
  assert.equal(leadStatus({ state: 'weird' }, []).key, 'draft');
});

test('canUpload reasons', () => {
  const lead = { state: 'draft', client_id: 'c1', other_name: null, note: null };
  const ready = [{ slot: 1, state: 'ready' }];
  assert.deepEqual(canUpload(lead, ready), { ok: true, reason: null });
  assert.equal(canUpload({ ...lead, client_id: null, other_name: 'Acme' }, ready).ok, true);
  const no = (l, p) => { const r = canUpload(l, p); assert.equal(r.ok, false); return r.reason; };
  assert.match(no({ ...lead, client_id: null, other_name: '   ' }, ready), /client/);
  assert.match(no({ ...lead, client_id: null, other_name: 'x'.repeat(201) }, ready), /200/);
  assert.equal(canUpload({ ...lead, client_id: null, other_name: 'x'.repeat(200) }, ready).ok, true);
  assert.match(no({ ...lead, note: 'n'.repeat(1001) }, ready), /1000/);
  assert.match(no(lead, []), /photo/);
  assert.match(no(lead, [{ slot: 1, state: 'processing' }]), /finish/);
  assert.match(no(lead, [{ slot: 1, state: 'ready' }, { slot: 2, state: 'failed' }]), /Replace or remove/);
  assert.match(no({ ...lead, state: 'queued' }, ready), /already/);
  assert.match(no(lead, [...ready, ...ready, ...ready]), /At most 2/);
});

test('renumberSlots sorts and closes gaps without mutating input', () => {
  const input = [{ id: 'b', slot: 2 }];
  assert.deepEqual(renumberSlots(input), [{ id: 'b', slot: 1 }]);
  assert.equal(input[0].slot, 2);
  assert.deepEqual(renumberSlots([{ id: 'y', slot: 2 }, { id: 'x', slot: 1 }]).map((p) => [p.id, p.slot]),
    [['x', 1], ['y', 2]]);
  assert.deepEqual(renumberSlots([]), []);
});

test('newId matches the server id rule, with and without randomUUID', () => {
  for (let i = 0; i < 20; i++) assert.match(newId(), ID_RE);
  assert.match(newId(), /^[A-Za-z0-9-]{8,64}$/);
  const desc = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  const real = globalThis.crypto;
  try {
    Object.defineProperty(globalThis, 'crypto', {
      value: { getRandomValues: (b) => real.getRandomValues(b) }, configurable: true,
    });
    const a = newId();
    assert.match(a, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    assert.notEqual(a, newId());
    Object.defineProperty(globalThis, 'crypto', { value: undefined, configurable: true });
    assert.match(newId(), ID_RE);
  } finally {
    Object.defineProperty(globalThis, 'crypto', desc);
  }
});
