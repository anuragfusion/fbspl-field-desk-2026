/* Image-lead store and upload queue. Everything is saved on the phone first;
 * uploads happen later, with retries, and every request is safe to repeat
 * because every id is generated here. */

import * as idb from './idb.js';
import {
  DEFAULT_CONFIG, MIME_EXT, canUpload, classifyError, crossedMilestones, formatBytes, logEntry,
  logLine, newId, nextRetryDelayMs, ownerKey, renumberSlots, sanitizeConfig, sniffImage,
  uploadTimeoutMs,
} from './image_core.js';
import { processPhoto, sha256Hex } from './image_compress.js';

const LEADS = 'image_leads';
const PHOTOS = 'image_photos';
const LOGS = 'image_logs';
const CONFIG = 'image_config';
const LOCK = 'fielddesk-image-queue';
const JSON_TIMEOUT_MS = 20000;
const POLL_MS = 60000;
const MAX_LOGS_PER_LEAD = 50;
const LOG_BATCH = 100;
const MAX_CHECKSUM_RETRIES = 3;

export const bus = new EventTarget();
const emit = (name, detail = {}) => bus.dispatchEvent(new CustomEvent(name, { detail }));
const changed = (leadId) => emit('changed', { lead_id: leadId });

let CFG = { ...DEFAULT_CONFIG };
const activeJobs = new Set();
let current = null;
let authStopped = false;
const remember = (session) => { if (session && session.user && session.event) current = session; };

const nowIso = () => new Date().toISOString();
const bySlot = (a, b) => a.slot - b.slot;

/* --- config ------------------------------------------------------------- */

export async function loadConfig() {
  try {
    const rec = await idb.get(CONFIG, 'config');
    CFG = rec ? sanitizeConfig(rec) : { ...DEFAULT_CONFIG };
  } catch {
    CFG = { ...DEFAULT_CONFIG };
  }
  return CFG;
}

export async function refreshConfig() {
  if (!navigator.onLine) return loadConfig();
  try {
    const cfg = sanitizeConfig(await fetchJson('/api/v1/image-leads/config'));
    await idb.put(CONFIG, { key: 'config', ...cfg, fetched_at: nowIso() });
    CFG = cfg;
  } catch (err) {
    if (err.status === 401) authFailed();
    await loadConfig();
  }
  return CFG;
}

/* --- logging ------------------------------------------------------------ */

let logSeq = 0;
const logId = () => `${String(Date.now()).padStart(15, '0')}-${String(logSeq++ % 1e6).padStart(6, '0')}`;

export function log(lead_id, photo, photo_count, pct, stage, msg, { server = true } = {}) {
  if (CFG.log_level === 'off') return Promise.resolve();
  if (CFG.console_log) console.log(logLine({ lead_id, photo, photo_count, pct, stage, msg }));
  if (!server || !lead_id) return Promise.resolve();
  const entry = { id: logId(), lead_id, ...logEntry({ at: nowIso(), photo, stage, pct, msg }) };
  return idb.imageTx([LOGS], 'readwrite', (s) => {
    const store = s[LOGS];
    store.put(entry);
    const req = store.index('by_lead').getAllKeys(lead_id);
    req.onsuccess = () => {
      const keys = req.result.sort();
      for (const k of keys.slice(0, Math.max(0, keys.length - MAX_LOGS_PER_LEAD))) store.delete(k);
    };
  }).catch(() => {});
}

async function flushLogs(lead) {
  try {
    const entries = (await idb.getIndex(LOGS, 'by_lead', lead.id)).sort((a, b) => (a.id < b.id ? -1 : 1));
    for (let i = 0; i < entries.length; i += LOG_BATCH) {
      const batch = entries.slice(i, i + LOG_BATCH);
      await fetchJson(`/api/v1/image-leads/${lead.id}/logs`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ entries: batch.map(logEntry) }),
      });
      await idb.imageTx([LOGS], 'readwrite', (s) => { for (const e of batch) s[LOGS].delete(e.id); });
    }
  } catch {
    /* best effort: they go with the next run */
  }
}

/* --- network ------------------------------------------------------------ */

const httpError = (status, body, fallback) => {
  let code = null;
  let message = fallback;
  try {
    const parsed = typeof body === 'string' ? JSON.parse(body) : body;
    if (parsed && parsed.error) { code = parsed.error.code || null; message = parsed.error.message || message; }
  } catch { /* not JSON */ }
  return Object.assign(new Error(message || `HTTP ${status}`), { status, code });
};

async function fetchJson(url, options = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), JSON_TIMEOUT_MS);
  try {
    let res;
    try {
      res = await fetch(url, { ...options, signal: ctrl.signal, cache: 'no-store' });
    } catch {
      throw Object.assign(new Error('network'), { status: 0, code: null });
    }
    const text = await res.text();
    if (!res.ok) throw httpError(res.status, text, `HTTP ${res.status}`);
    const ct = res.headers.get('content-type') || '';
    /* A captive portal answers 200 + HTML: treat it as no signal. */
    if (!ct.includes('application/json')) throw Object.assign(new Error('not-json'), { status: 0, code: null });
    try { return JSON.parse(text); } catch { throw Object.assign(new Error('not-json'), { status: 0, code: null }); }
  } finally {
    clearTimeout(timer);
  }
}

function putPhoto(lead, photo, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/v1/image-leads/${encodeURIComponent(lead.id)}/photos/${photo.slot}`);
    xhr.timeout = uploadTimeoutMs(photo.size);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); } catch {
          reject(Object.assign(new Error('not-json'), { status: 0, code: null }));
        }
      } else {
        reject(httpError(xhr.status, xhr.responseText, `HTTP ${xhr.status}`));
      }
    };
    const net = (what) => () => reject(Object.assign(new Error(what), { status: 0, code: what.toUpperCase() }));
    xhr.onerror = net('network');
    xhr.ontimeout = net('timeout');
    xhr.onabort = net('aborted');
    const fd = new FormData();
    fd.append('photo_id', photo.id);
    fd.append('sha256', photo.sha256);
    fd.append('width', photo.width ? String(photo.width) : '');
    fd.append('height', photo.height ? String(photo.height) : '');
    fd.append('compressed', photo.compressed ? '1' : '0');
    fd.append('file', new Blob([photo.bytes], { type: photo.mime }), `photo.${MIME_EXT[photo.mime] || 'jpg'}`);
    xhr.send(fd);
  });
}

/* --- reads -------------------------------------------------------------- */

function readLeads(filterFn) {
  return (stores, leads) => {
    const out = [];
    leads.onsuccess = () => {
      for (const lead of leads.result.filter(filterFn)) {
        const item = { lead, photos: [] };
        out.push(item);
        const req = stores[PHOTOS].index('by_lead').getAll(lead.id);
        req.onsuccess = () => { item.photos = req.result.sort(bySlot); };
      }
    };
    return out;
  };
}

export async function listLeads(session, { filter = () => true } = {}) {
  remember(session);
  const out = await idb.imageTx([LEADS, PHOTOS], 'readonly', (s) => (
    readLeads(filter)(s, s[LEADS].index('by_owner').getAll(ownerKey(session)))
  ));
  return out.sort((a, b) => (a.lead.captured_at < b.lead.captured_at ? 1 : -1));
}

export async function getLead(id) {
  const out = await idb.imageTx([LEADS, PHOTOS], 'readonly', (s) => {
    const box = { value: null };
    const lr = s[LEADS].get(id);
    const pr = s[PHOTOS].index('by_lead').getAll(id);
    lr.onsuccess = () => { if (lr.result) box.value = { lead: lr.result, photos: [] }; };
    pr.onsuccess = () => { if (box.value) box.value.photos = pr.result.sort(bySlot); };
    return box;
  });
  return out.value;
}

export async function counts(session) {
  const items = await idb.getIndex(LEADS, 'by_owner', ownerKey(session));
  return {
    drafts: items.filter((l) => l.state === 'draft').length,
    unsent: items.filter((l) => ['queued', 'uploading', 'failed'].includes(l.state)).length,
  };
}

/* --- drafts ------------------------------------------------------------- */

const lockedError = () => Object.assign(new Error('This lead is already uploaded or queued and can no longer be changed.'), { code: 'LOCKED' });

async function draftOf(id, session) {
  const lead = await idb.get(LEADS, id);
  if (!lead) throw Object.assign(new Error('This lead no longer exists.'), { code: 'NOT_FOUND' });
  if (session && lead.owner_key !== ownerKey(session)) {
    throw Object.assign(new Error('This lead belongs to someone else.'), { code: 'FORBIDDEN' });
  }
  if (lead.state !== 'draft') throw lockedError();
  return lead;
}

const clientFields = (fields) => {
  const client_id = fields.client_id || null;
  const other = client_id ? null : (fields.other_name ?? null);
  return {
    client_id,
    client_name: client_id ? (fields.client_name || '') : String(other ?? '').trim(),
    other_name: other,
    note: fields.note ?? null,
    updated_at: nowIso(),
  };
};

export async function saveDraft(session, fields = {}) {
  remember(session);
  const ts = nowIso();
  let lead;
  if (fields.id) {
    lead = { ...(await draftOf(fields.id, session)), ...clientFields(fields) };
  } else {
    lead = {
      id: newId(), owner_key: ownerKey(session), user_id: session.user.id,
      event_id: session.event.id, ...clientFields(fields), state: 'draft', photo_count: 0,
      captured_at: ts, created_at: ts, updated_at: ts, server_created: false, attempts: 0,
      next_at: 0, error: null, error_permanent: false, from_server: false,
    };
  }
  await idb.put(LEADS, lead);
  changed(lead.id);
  return lead;
}

export async function deleteDraft(id) {
  await draftOf(id);
  await idb.imageTx([LEADS, PHOTOS, LOGS], 'readwrite', (s) => {
    s[LEADS].delete(id);
    for (const name of [PHOTOS, LOGS]) {
      const req = s[name].index('by_lead').getAllKeys(id);
      req.onsuccess = () => { for (const k of req.result) s[name].delete(k); };
    }
  });
  changed(id);
}

/* Writes a slot and resets the draft's running photo count, in one transaction.
 * keepOld: a replacement being processed sits next to the current photo, which
 * is only dropped once the new one is safely saved (see settlePhoto). */
function writeSlot(leadId, slot, photo, { keepOld = false } = {}) {
  return idb.imageTx([LEADS, PHOTOS], 'readwrite', (s) => {
    const box = { count: 0 };
    const req = s[PHOTOS].index('by_lead').getAll(leadId);
    req.onsuccess = () => {
      if (!keepOld) for (const p of req.result) if (p.slot === slot) s[PHOTOS].delete(p.id);
      if (photo) s[PHOTOS].put(photo);
      const slots = new Set(req.result.filter((p) => keepOld || p.slot !== slot).map((p) => p.slot));
      if (photo) slots.add(slot);
      box.count = slots.size;
      const lr = s[LEADS].get(leadId);
      lr.onsuccess = () => {
        if (lr.result) s[LEADS].put({ ...lr.result, photo_count: box.count, updated_at: nowIso() });
      };
    };
    return box;
  });
}

const userMessage = (err) => {
  if (err && err.name === 'QuotaExceededError') return 'The phone is out of storage. Free some space and try again.';
  if (err && typeof err.code === 'string' && err.message) return err.message;
  return 'This photo could not be saved. Try again.';
};

export async function addPhoto(session, leadId, slot, blob) {
  remember(session);
  if (!Number.isInteger(slot) || slot < 1 || slot > CFG.max_photos) {
    throw Object.assign(new Error(`Slot must be 1 to ${CFG.max_photos}.`), { code: 'INVALID_SLOT' });
  }
  await draftOf(leadId, session);
  const base = {
    id: newId(), lead_id: leadId, slot, bytes: null, mime: null, size: 0, width: null,
    height: null, compressed: false, sha256: null, state: 'processing', content_url: null, error: null,
  };
  activeJobs.add(base.id);
  try {
    const { count } = await writeSlot(leadId, slot, base, { keepOld: true });
    changed(leadId);

    const t0 = performance.now();
    let photo;
    try {
      const r = await processPhoto(blob, CFG, (pct, stage, msg) => log(leadId, slot, count, pct, stage, msg));
      photo = {
        ...base, bytes: r.bytes, mime: r.mime, size: r.size, width: r.width, height: r.height,
        compressed: r.compressed, sha256: r.sha256, state: 'ready',
      };
      const saved = await settlePhoto(photo, { replaceSlot: true });
      if (!saved) return photo;
      const smaller = r.original_size > 0 ? Math.max(0, Math.round((1 - r.size / r.original_size) * 100)) : 0;
      log(leadId, slot, count, 100, 'compress',
        `saved on phone, ${smaller}% smaller (${Math.round(performance.now() - t0)} ms)`);
    } catch (err) {
      const code = typeof err.code === 'string' ? err.code : (err.name || 'PROCESS_FAILED');
      log(leadId, slot, count, null, 'error', `${code} photo not saved`);
      /* A failed replacement leaves the current photo exactly as it was. */
      if (await dropIfReplacement(base)) {
        changed(leadId);
        throw Object.assign(new Error(`Kept the previous photo. ${userMessage(err)}`), { code: 'KEPT_PREVIOUS' });
      }
      photo = { ...base, state: 'failed', error: userMessage(err) };
      try { await settlePhoto(photo); } catch { /* recoverInterrupted() cleans it up */ }
    }
    changed(leadId);
    return photo;
  } finally {
    activeJobs.delete(base.id);
  }
}

/* Writes the processed result only if the photo was not replaced and the lead
 * is still a draft meanwhile; returns whether it was written. replaceSlot drops
 * the photo it replaces in the same transaction. */
function settlePhoto(photo, { replaceSlot = false } = {}) {
  return idb.imageTx([LEADS, PHOTOS], 'readwrite', (s) => {
    const box = { saved: false };
    const lr = s[LEADS].get(photo.lead_id);
    const pr = s[PHOTOS].get(photo.id);
    const all = s[PHOTOS].index('by_lead').getAll(photo.lead_id);
    all.onsuccess = () => {
      if (!(lr.result && lr.result.state === 'draft' && pr.result)) return;
      s[PHOTOS].put(photo);
      if (replaceSlot) {
        for (const p of all.result) if (p.slot === photo.slot && p.id !== photo.id) s[PHOTOS].delete(p.id);
      }
      box.saved = true;
    };
    return box;
  }).then((b) => b.saved);
}

/* Deletes a failed replacement's placeholder when an earlier photo still holds
 * the slot; returns whether there was one to fall back to. */
function dropIfReplacement(photo) {
  return idb.imageTx([PHOTOS], 'readwrite', (s) => {
    const box = { hadPrevious: false };
    const req = s[PHOTOS].index('by_lead').getAll(photo.lead_id);
    req.onsuccess = () => {
      box.hadPrevious = req.result.some((p) => p.slot === photo.slot && p.id !== photo.id
        && p.state !== 'processing');
      if (box.hadPrevious) s[PHOTOS].delete(photo.id);
    };
    return box;
  }).then((b) => b.hadPrevious);
}

/* A photo left 'processing' by a killed tab would block Upload forever. Anything
 * processing that this page isn't working on was interrupted. */
export async function recoverInterrupted(leadId) {
  const stuck = (await idb.getIndex(PHOTOS, 'by_lead', leadId))
    .filter((p) => p.state === 'processing' && !activeJobs.has(p.id));
  if (!stuck.length) return 0;
  await idb.imageTx([PHOTOS], 'readwrite', (s) => {
    const req = s[PHOTOS].index('by_lead').getAll(leadId);
    req.onsuccess = () => {
      for (const p of stuck) {
        const covered = req.result.some((o) => o.slot === p.slot && o.id !== p.id && o.state !== 'processing');
        if (covered) s[PHOTOS].delete(p.id);
        else s[PHOTOS].put({ ...p, state: 'failed', error: 'Interrupted — take this photo again.' });
      }
    };
  });
  changed(leadId);
  return stuck.length;
}

export async function removePhoto(leadId, slot) {
  await draftOf(leadId);
  await writeSlot(leadId, slot, null);
  changed(leadId);
}

/* --- submit / retry ----------------------------------------------------- */

export async function submit(leadId) {
  const found = await getLead(leadId);
  if (!found) return { ok: false, reason: 'This lead no longer exists.' };
  const { lead, photos } = found;
  const check = canUpload(lead, photos);
  if (!check.ok) return check;

  const renumbered = renumberSlots(photos);
  const slotMap = new Map(photos.slice().sort(bySlot).map((p, i) => [p.slot, i + 1]));
  const note = String(lead.note ?? '').replace(/\x00/g, '').trim() || null;
  const other = lead.client_id ? null : String(lead.other_name).trim();
  const next = {
    ...lead, note, other_name: other, client_name: lead.client_id ? lead.client_name : other,
    photo_count: renumbered.length, state: 'queued', attempts: 0, next_at: 0, error: null,
    error_permanent: false, checksum_retries: 0, updated_at: nowIso(),
  };
  await idb.imageTx([LEADS, PHOTOS, LOGS], 'readwrite', (s) => {
    s[LEADS].put(next);
    for (const p of renumbered) s[PHOTOS].put(p);
    const req = s[LOGS].index('by_lead').getAll(leadId);
    req.onsuccess = () => {
      for (const e of req.result) {
        if (e.photo == null) continue;
        s[LOGS].put({ ...e, photo: slotMap.get(e.photo) ?? null });
      }
    };
  });
  log(leadId, null, next.photo_count, null, 'info', `submitted photos=${next.photo_count}`);
  changed(leadId);
  run(current);
  return { ok: true, reason: null };
}

export async function retry(leadId) {
  const lead = await idb.get(LEADS, leadId);
  if (!lead || lead.state !== 'failed') return;
  await idb.put(LEADS, {
    ...lead, state: 'queued', attempts: 0, next_at: 0, error: null, error_permanent: false,
    checksum_retries: 0, updated_at: nowIso(),
  });
  changed(leadId);
  run(current);
}

/* A lead the server refused for good (e.g. its client no longer exists) would
 * otherwise fail on every Retry. If the server never created it, it can go
 * back to being an editable draft. */
export async function reopenFailed(leadId) {
  const lead = await idb.get(LEADS, leadId);
  if (!lead || lead.state !== 'failed' || lead.server_created) return false;
  await idb.put(LEADS, {
    ...lead, state: 'draft', attempts: 0, next_at: 0, error: null, error_permanent: false,
    checksum_retries: 0, updated_at: nowIso(),
  });
  changed(leadId);
  return true;
}

/* Removes a failed lead from this phone only. If the server already holds part
 * of it, it stays there for the office to see (floor users cannot delete). */
export async function discardFailed(leadId) {
  const lead = await idb.get(LEADS, leadId);
  if (!lead || lead.state !== 'failed') return false;
  await idb.imageTx([LEADS, PHOTOS, LOGS], 'readwrite', (s) => {
    s[LEADS].delete(leadId);
    for (const name of [PHOTOS, LOGS]) {
      const req = s[name].index('by_lead').getAllKeys(leadId);
      req.onsuccess = () => { for (const k of req.result) s[name].delete(k); };
    }
  });
  log(leadId, null, lead.photo_count, null, 'info', `removed from phone after failure server_created=${!!lead.server_created}`, { server: false });
  changed(leadId);
  return true;
}

/* --- upload run --------------------------------------------------------- */

const eligible = (l, now) => {
  const active = l.state === 'queued' || l.state === 'uploading'
    || (l.state === 'failed' && !l.error_permanent);
  return active && (l.next_at || 0) <= now;
};

async function patchLead(id, patch) {
  const lead = await idb.get(LEADS, id);
  if (!lead) return null;
  const next = { ...lead, ...patch, updated_at: nowIso() };
  await idb.put(LEADS, next);
  changed(id);
  return next;
}

async function uploadLead(leadId) {
  let lead = await idb.get(LEADS, leadId);
  if (!lead || !eligible(lead, Date.now())) return 'skip';
  const photos = (await idb.getIndex(PHOTOS, 'by_lead', leadId)).sort(bySlot);
  const n = lead.photo_count;
  let slot = null;
  try {
    if (photos.length !== n) {
      throw Object.assign(new Error('A photo is missing on this phone. Delete and capture again.'),
        { status: 400, code: 'LOCAL_MISSING' });
    }
    lead = await patchLead(leadId, { state: 'uploading' });
    if (!lead.server_created) {
      await fetchJson(`/api/v1/events/${encodeURIComponent(lead.event_id)}/image-leads`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          id: lead.id, client_id: lead.client_id || null,
          other_name: lead.client_id ? null : lead.other_name, note: lead.note || null,
          photo_count: n, captured_at: lead.captured_at, client_sent_at: nowIso(),
        }),
      });
      lead = await patchLead(leadId, { server_created: true });
      log(leadId, null, n, null, 'info', 'created on server');
    }

    for (const photo of photos) {
      if (photo.state === 'uploaded') continue;
      slot = photo.slot;
      if (!photo.bytes || !photo.sha256) {
        throw Object.assign(new Error('A photo is missing on this phone. Delete and capture again.'),
          { status: 400, code: 'LOCAL_MISSING' });
      }
      let pct = 0;
      const t0 = performance.now();
      const res = await putPhoto(lead, photo, (loaded, total) => {
        const m = crossedMilestones(pct, loaded, total);
        pct = m.pct;
        for (const c of m.crossed) {
          log(leadId, slot, n, c, 'upload',
            c === 100 ? 'all bytes sent, waiting for server' : `sent ${formatBytes(loaded)}`, { server: false });
        }
      });
      await idb.put(PHOTOS, {
        ...photo, state: 'uploaded', error: null,
        content_url: (res && res.photo && res.photo.content_url) || photo.content_url,
      });
      changed(leadId);
      log(leadId, slot, n, 100, 'upload',
        `received by server${res && res.replayed ? ' (already stored)' : ''} (${Math.round(performance.now() - t0)} ms)`,
        { server: false });
    }

    await flushLogs(lead);
    await patchLead(leadId, {
      state: 'done', error: null, error_permanent: false, attempts: 0, next_at: 0,
    });
    return 'done';
  } catch (err) {
    return handleError(leadId, slot, n, err);
  }
}

async function handleError(leadId, slot, n, err) {
  const status = Number(err.status) || 0;
  let kind = classifyError(status, err.code);
  const lead = await idb.get(LEADS, leadId);
  if (!lead) return 'skip';

  if (kind === 'auth') {
    await patchLead(leadId, { state: 'queued' });
    return 'auth';
  }
  let checksumRetries = lead.checksum_retries || 0;
  if (err.code === 'CHECKSUM_MISMATCH') {
    checksumRetries += 1;
    if (checksumRetries > MAX_CHECKSUM_RETRIES) kind = 'permanent';
  }
  log(leadId, slot, n, null, 'error', `upload failed status=${status} code=${err.code || '-'} kind=${kind}`);

  if (kind === 'retry') {
    const attempts = (lead.attempts || 0) + 1;
    const message = status === 0
      ? 'No connection. It will upload automatically.'
      : `${err.message || `Server error ${status}.`}`;
    await patchLead(leadId, {
      state: 'queued', attempts, next_at: Date.now() + nextRetryDelayMs(attempts - 1),
      error: message, error_permanent: false, checksum_retries: checksumRetries,
    });
    return 'retry';
  }
  await patchLead(leadId, {
    state: 'failed', error_permanent: true, checksum_retries: checksumRetries,
    error: err.message || `Upload refused (${status}).`,
  });
  if (lead.server_created) flushLogs(lead);
  return 'permanent';
}

function authFailed() {
  authStopped = true;
  emit('auth', {});
}

let inFlight = null;
let again = false;

async function runOnce(session) {
  const now = Date.now();
  const leads = (await idb.getIndex(LEADS, 'by_owner', ownerKey(session)))
    .filter((l) => eligible(l, now))
    .sort((a, b) => (a.captured_at < b.captured_at ? -1 : 1));
  const results = [];
  for (const l of leads) {
    if (!navigator.onLine) break;
    const r = await uploadLead(l.id);
    results.push(r);
    if (r === 'auth') { authFailed(); break; }
  }
  return results;
}

export function run(session) {
  const s = session || current;
  remember(s);
  if (!s || authStopped) return Promise.resolve({ skipped: 'no-session' });
  if (!navigator.onLine) { schedule(s); return Promise.resolve({ skipped: 'offline' }); }
  if (inFlight) { again = true; return inFlight; }

  const work = async () => {
    try {
      if (navigator.locks && navigator.locks.request) {
        return await navigator.locks.request(LOCK, { ifAvailable: true }, async (lock) => {
          if (!lock) return { skipped: 'in-flight' };
          return { results: await runOnce(s) };
        });
      }
      return { results: await runOnce(s) };
    } catch (err) {
      return { error: err.message };
    }
  };
  inFlight = work().finally(() => {
    inFlight = null;
    if (again) { again = false; run(s); } else schedule(s);
  });
  return inFlight;
}

let retryTimer = null;

async function schedule(session) {
  clearTimeout(retryTimer);
  retryTimer = null;
  try {
    const now = Date.now();
    const next = (await idb.getIndex(LEADS, 'by_owner', ownerKey(session)))
      .filter((l) => (l.state === 'queued' || (l.state === 'failed' && !l.error_permanent)) && l.next_at > now)
      .reduce((min, l) => Math.min(min, l.next_at), Infinity);
    if (next !== Infinity) {
      retryTimer = setTimeout(() => run(session), Math.min(next - now + 250, 2 ** 31 - 1));
    }
  } catch { /* the 60 s poll picks it up */ }
}

/* --- lifecycle ---------------------------------------------------------- */

/* Signal is back: don't make a lead that failed offline sit out a long backoff. */
async function wakeQueued(session) {
  if (!session) return;
  try {
    const now = Date.now();
    const leads = (await idb.getIndex(LEADS, 'by_owner', ownerKey(session)))
      .filter((l) => l.state === 'queued' && l.next_at > now);
    if (leads.length) {
      await idb.imageTx([LEADS], 'readwrite', (s) => { for (const l of leads) s[LEADS].put({ ...l, next_at: 0 }); });
    }
  } catch { /* the regular schedule still applies */ }
}

let started = false;
let pollTimer = null;

export function start(session) {
  remember(session);
  authStopped = false;
  const kick = () => run(current);
  if (!started) {
    started = true;
    const arm = () => {
      clearInterval(pollTimer);
      pollTimer = document.visibilityState === 'visible' ? setInterval(kick, POLL_MS) : null;
    };
    document.addEventListener('visibilitychange', () => {
      arm();
      if (document.visibilityState === 'visible') kick();
    });
    window.addEventListener('online', () => {
      wakeQueued(current).then(kick);
      restoreFromServer(current).catch(() => {});
    });
    arm();
  }
  return loadConfig()
    .then(refreshConfig)
    .then(() => run(session))
    .then(() => restoreFromServer(session))
    .catch(() => {});
}

export async function restoreFromServer(session) {
  remember(session);
  if (!session || !navigator.onLine) return { skipped: 'offline' };
  let body;
  try {
    body = await fetchJson(`/api/v1/events/${encodeURIComponent(session.event.id)}/image-leads`);
  } catch (err) {
    if (err.status === 401) authFailed();
    return { error: err.message };
  }
  const list = (body && Array.isArray(body.image_leads) ? body.image_leads : [])
    .filter((l) => l && l.id && (!l.captured_by || l.captured_by === session.user.id));
  const key = ownerKey(session);
  const ignoreExisting = (req) => {
    req.onerror = (e) => { e.preventDefault(); e.stopPropagation(); };
  };
  const added = await idb.imageTx([LEADS, PHOTOS], 'readwrite', (s) => {
    const box = { n: 0 };
    for (const r of list) {
      const probe = s[LEADS].count(r.id);
      probe.onsuccess = () => {
        if (probe.result > 0) return;
        box.n += 1;
        const photos = Array.isArray(r.photos) ? r.photos : [];
        ignoreExisting(s[LEADS].add({
          id: r.id, owner_key: key, user_id: session.user.id, event_id: session.event.id,
          client_id: r.client_id || null, client_name: r.client_name || '',
          other_name: r.client_id ? null : (r.client_name || ''), note: r.note ?? null,
          state: 'done', photo_count: r.photo_count || photos.length,
          captured_at: r.captured_at, created_at: r.created_at || r.captured_at,
          updated_at: r.updated_at || r.captured_at, server_created: true, attempts: 0,
          next_at: 0, error: null, error_permanent: false, from_server: true,
        }));
        for (const p of photos) {
          ignoreExisting(s[PHOTOS].add({
            id: p.id, lead_id: r.id, slot: p.slot, bytes: null, mime: p.mime_type || null,
            size: p.size_bytes || 0, width: p.width ?? null, height: p.height ?? null,
            compressed: !!p.compressed, sha256: p.checksum_sha256 || null, state: 'uploaded',
            content_url: p.content_url || null, error: null,
          }));
        }
      };
    }
    return box;
  });
  if (added.n) changed(null);
  return { added: added.n };
}

/* --- viewing / sign-out ------------------------------------------------- */

export async function clearUploadedBytes(session) {
  const done = (await idb.getIndex(LEADS, 'by_owner', ownerKey(session))).filter((l) => l.state === 'done');
  if (!done.length) return;
  await idb.imageTx([PHOTOS], 'readwrite', (s) => {
    for (const l of done) {
      const req = s[PHOTOS].index('by_lead').getAll(l.id);
      req.onsuccess = () => {
        for (const p of req.result) if (p.bytes) s[PHOTOS].put({ ...p, bytes: null });
      };
    }
  });
  changed(null);
}

export function photoUrl(photo) {
  if (!photo || !photo.bytes) return null;
  return URL.createObjectURL(new Blob([photo.bytes], { type: photo.mime || 'image/jpeg' }));
}

export async function fetchRemotePhoto(photo) {
  if (!photo || !photo.content_url) throw Object.assign(new Error('This photo is not available.'), { code: 'NOT_FOUND' });
  if (!navigator.onLine) throw Object.assign(new Error('You are offline. Connect to view this photo.'), { code: 'OFFLINE' });
  let res;
  try {
    res = await fetch(photo.content_url, { cache: 'no-store' });
  } catch {
    throw Object.assign(new Error('Could not load the photo. Check your connection.'), { code: 'NETWORK' });
  }
  if (res.status === 401) authFailed();
  if (!res.ok) throw Object.assign(new Error('Could not load the photo.'), { code: 'HTTP', status: res.status });
  const bytes = await res.arrayBuffer();
  if (!sniffImage(new Uint8Array(bytes.slice(0, 32)))) {
    throw Object.assign(new Error('Could not load the photo.'), { code: 'NOT_IMAGE' });
  }
  if (photo.sha256 && (await sha256Hex(bytes)) !== photo.sha256) {
    throw Object.assign(new Error('The photo was damaged on the way. Try again.'), { code: 'CHECKSUM_MISMATCH' });
  }
  const stored = await idb.get(PHOTOS, photo.id);
  const base = stored || photo;
  const next = { ...base, bytes, mime: base.mime || sniffImage(new Uint8Array(bytes.slice(0, 32))) };
  if (stored) {
    try { await idb.put(PHOTOS, next); } catch { /* storage full: still show it */ }
  }
  return photoUrl(next);
}
