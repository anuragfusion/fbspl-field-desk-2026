/* Pure image-lead logic. No IndexedDB, no fetch, no DOM — so Node can test it.
 * Log messages built from these helpers carry ids, sizes, timings and codes
 * only: never client names, notes, filenames, tokens or links. */

export const DEFAULT_CONFIG = Object.freeze({
  max_dimension: 1600,
  jpeg_quality: 0.8,
  max_upload_bytes: 5242880,
  log_level: 'info',
  console_log: true,
  max_photos: 2,
});

export const ACCEPTED_TYPES = ['image/jpeg', 'image/png', 'image/webp', 'image/heic', 'image/heif'];
export const MIME_EXT = {
  'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp',
  'image/heic': 'heic', 'image/heif': 'heif',
};
export const MAX_NAME = 200;
export const MAX_NOTE = 1000;
export const ID_RE = /^[A-Za-z0-9-]{8,64}$/;

const KEEP_ORIGINAL_BYTES = 1024 * 1024;
const MILESTONES = [25, 50, 75, 100];
const STAGES = new Set(['compress', 'upload', 'error', 'info']);
const PCTS = new Set([0, 25, 50, 75, 100]);
const CTRL = /[\x00-\x1f\x7f]+/g;

const num = (v) => {
  if (typeof v === 'number') return v;
  if (typeof v === 'string' && v.trim() !== '') return Number(v);
  return NaN;
};

const inRange = (v, lo, hi, int) => Number.isFinite(v) && v >= lo && v <= hi
  && (!int || Number.isInteger(v));

export function sanitizeConfig(raw) {
  const r = raw && typeof raw === 'object' ? raw : {};
  const dim = num(r.max_dimension);
  const q = num(r.jpeg_quality);
  const bytes = num(r.max_upload_bytes);
  const level = typeof r.log_level === 'string' ? r.log_level.trim().toLowerCase() : '';
  return {
    max_dimension: inRange(dim, 640, 4096, true) ? dim : DEFAULT_CONFIG.max_dimension,
    jpeg_quality: inRange(q, 0.3, 1.0, false) ? q : DEFAULT_CONFIG.jpeg_quality,
    max_upload_bytes: inRange(bytes, 102400, 20971520, true) ? bytes : DEFAULT_CONFIG.max_upload_bytes,
    log_level: ['info', 'debug', 'off'].includes(level) ? level : DEFAULT_CONFIG.log_level,
    console_log: typeof r.console_log === 'boolean' ? r.console_log : DEFAULT_CONFIG.console_log,
    max_photos: DEFAULT_CONFIG.max_photos,
  };
}

const HEIC_BRANDS = new Set(['heic', 'heix', 'heim', 'heis', 'hevc', 'hevx']);
const HEIF_BRANDS = new Set(['mif1', 'msf1', 'heif']);
const ascii = (b, from, to) => String.fromCharCode(...b.subarray(from, to));

export function sniffImage(bytes) {
  if (!bytes) return null;
  const b = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (!b.length) return null;
  if (b.length >= 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return 'image/jpeg';
  if (b.length >= 8 && b[0] === 0x89 && ascii(b, 1, 4) === 'PNG'
      && b[4] === 0x0d && b[5] === 0x0a && b[6] === 0x1a && b[7] === 0x0a) return 'image/png';
  if (b.length >= 12 && ascii(b, 0, 4) === 'RIFF' && ascii(b, 8, 12) === 'WEBP') return 'image/webp';
  if (b.length >= 12 && ascii(b, 4, 8) === 'ftyp') {
    const brand = ascii(b, 8, 12);
    if (HEIC_BRANDS.has(brand)) return 'image/heic';
    if (HEIF_BRANDS.has(brand)) return 'image/heif';
  }
  return null;
}

export function fitWithin(w, h, max) {
  const width = Math.round(Number(w));
  const height = Math.round(Number(h));
  const longest = Math.max(width, height);
  if (!(width > 0) || !(height > 0) || !(max > 0) || longest <= max) {
    return { width, height, scaled: false };
  }
  const ratio = max / longest;
  return {
    width: width >= height ? Math.floor(max) : Math.max(1, Math.round(width * ratio)),
    height: height > width ? Math.floor(max) : Math.max(1, Math.round(height * ratio)),
    scaled: true,
  };
}

export function shouldKeepOriginal({ mime, width, height, size }, cfg) {
  const c = cfg || DEFAULT_CONFIG;
  const longest = Math.max(Number(width) || 0, Number(height) || 0);
  const bytes = Number(size);
  return mime === 'image/jpeg' && longest > 0 && longest <= c.max_dimension
    && bytes >= 0 && bytes <= KEEP_ORIGINAL_BYTES;
}

export function crossedMilestones(prevPct, loaded, total) {
  const prev = Number(prevPct) || 0;
  const raw = total > 0 ? Math.floor((loaded / total) * 100) : 0;
  const pct = Math.max(prev, Math.min(100, Math.max(0, raw)));
  return { pct, crossed: MILESTONES.filter((m) => m > prev && m <= pct) };
}

export function nextRetryDelayMs(attempts) {
  const n = Math.max(0, Number(attempts) || 0);
  return Math.min(30000 * 2 ** n, 900000);
}

export function classifyError(status, code) {
  const s = Number(status) || 0;
  if (s === 401) return 'auth';
  if (s === 0 || s === 408 || s === 429 || s >= 500) return 'retry';
  if (s === 400 && code === 'CHECKSUM_MISMATCH') return 'retry';
  return 'permanent';
}

export function uploadTimeoutMs(bytes) {
  const n = Math.max(0, Number(bytes) || 0);
  return Math.min(30000 + Math.ceil(n / 10240) * 1000, 300000);
}

export function formatBytes(n) {
  const b = Math.max(0, Math.round(Number(n) || 0));
  if (b < 1024) return `${b} B`;
  const kb = Math.round(b / 1024);
  if (kb < 1024) return `${kb} KB`;
  return `${(b / (1024 * 1024)).toFixed(1)} MB`;
}

export function shortId(id) {
  return String(id || '-').slice(0, 8);
}

const clean = (s, max) => String(s ?? '').replace(CTRL, ' ').trim().slice(0, max).trim();

export function logLine({ lead_id, photo, photo_count, pct, stage, msg }) {
  let tag = `[image-lead] DEVICE lead=${shortId(lead_id)}`;
  if (photo !== null && photo !== undefined && photo !== '') {
    tag += ` photo=${photo}/${photo_count || photo}`;
  }
  const p = pct === null || pct === undefined ? '' : `${String(pct).padStart(3)}% `;
  return `${tag}  ${p}${stage || 'info'} ${clean(msg, 200)}`.trimEnd();
}

export function logEntry({ at, photo, stage, pct, msg }) {
  const slot = Number(photo);
  return {
    at: clean(at, 40) || new Date().toISOString(),
    photo: Number.isInteger(slot) && slot >= 1 ? slot : null,
    stage: STAGES.has(stage) ? stage : 'info',
    pct: PCTS.has(pct) ? pct : null,
    msg: clean(msg, 200),
  };
}

const LABELS = {
  draft: 'Draft', queued: 'Waiting for signal', done: 'Uploaded', failed: 'Upload failed',
};

export function leadStatus(lead, photos) {
  const list = Array.isArray(photos) ? photos : [];
  const total = (lead && lead.photo_count) || list.length;
  const uploaded = list.filter((p) => p.state === 'uploaded').length;
  const state = lead && lead.state;
  const key = state === 'uploading' || LABELS[state] ? state : 'draft';
  const label = key === 'uploading'
    ? `Uploading ${Math.min(uploaded + 1, Math.max(total, 1))} of ${Math.max(total, 1)}`
    : LABELS[key];
  return { key, label, uploaded, total };
}

export function canUpload(lead, photos) {
  const no = (reason) => ({ ok: false, reason });
  if (!lead) return no('This lead no longer exists.');
  if (lead.state && lead.state !== 'draft') return no('This lead is already uploaded or queued.');
  const other = String(lead.other_name ?? '').trim();
  if (!lead.client_id && !other) return no('Pick a client or type a name.');
  if (!lead.client_id && other.length > MAX_NAME) {
    return no(`The name is longer than ${MAX_NAME} characters.`);
  }
  if (String(lead.note ?? '').trim().length > MAX_NOTE) {
    return no(`The note is longer than ${MAX_NOTE} characters.`);
  }
  const list = Array.isArray(photos) ? photos : [];
  if (list.length === 0) return no('Add at least one photo.');
  if (list.length > DEFAULT_CONFIG.max_photos) return no(`At most ${DEFAULT_CONFIG.max_photos} photos.`);
  if (list.some((p) => p.state === 'processing')) return no('Wait for the photos to finish saving.');
  if (list.some((p) => p.state !== 'ready')) return no('A photo could not be saved. Replace or remove it.');
  return { ok: true, reason: null };
}

export function renumberSlots(photos) {
  return [...(photos || [])]
    .sort((a, b) => a.slot - b.slot)
    .map((p, i) => ({ ...p, slot: i + 1 }));
}

export function newId() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const b = new Uint8Array(16);
  if (c && typeof c.getRandomValues === 'function') c.getRandomValues(b);
  else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, '0')).join('');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

export function ownerKey(session) {
  return `${session.user.id}|${session.event.id}`;
}
