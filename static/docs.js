/* Document pre-fetch and offline readiness.
 *
 * Documents are pulled IN FULL before the team leaves the office (§8.2). A PDF
 * that streams on demand is a PDF you cannot open in the exhibit hall.
 */

import * as idb from './idb.js';
import { docUrl, formatReadiness, readiness, storageWarning } from './sync_core.js';

export const DOCS_CACHE = 'docs-v1';
export const bus = new EventTarget();
const emit = (name, detail) => bus.dispatchEvent(new CustomEvent(name, { detail }));

async function cachedUrls() {
  const cache = await caches.open(DOCS_CACHE);
  return new Set((await cache.keys()).map((r) => new URL(r.url).pathname));
}

/* Always recomputed from the cache itself. Never a stored counter — iOS can
 * evict overnight, and a counter would cheerfully keep reporting 14 of 14. */
export async function status() {
  const docs = await idb.getAll('documents');
  const r = readiness(docs, await cachedUrls());
  return { ...r, label: formatReadiness(r) };
}

/* THE guard. Paywalled convention wifi answers every request with 200 + an HTML
 * login page. cache.add() would store that page as a PDF, readiness would then
 * report "14 of 14 ready", and every document would open as a wifi portal on the
 * floor. So: never cache.add — fetch, verify the bytes are the bytes the server
 * said, and only then put. */
async function prefetchOne(doc) {
  const url = docUrl(doc.id);
  /* The header tells the service worker to let this one reach the network;
   * every other request for these bytes is answered from the cache only. */
  const res = await fetch(url, { cache: 'no-store', headers: { 'x-fielddesk-prefetch': '1' } });
  if (!res.ok) throw new Error(`http ${res.status}`);

  const blob = await res.blob();
  if (blob.size !== Number(doc.size_bytes)) {
    throw new Error(`size ${blob.size} != ${doc.size_bytes} — refusing to cache`);
  }
  if (doc.mime_type && blob.type && blob.type.split('/')[0] !== doc.mime_type.split('/')[0]) {
    throw new Error(`type ${blob.type} != ${doc.mime_type} — refusing to cache`);
  }

  const cache = await caches.open(DOCS_CACHE);
  await cache.put(url, new Response(blob, {
    headers: { 'content-type': doc.mime_type, 'content-length': String(blob.size) },
  }));
}

export async function prefetchAll({ force = false } = {}) {
  const docs = await idb.getAll('documents');
  const have = await cachedUrls();
  const todo = force ? docs : docs.filter((d) => !have.has(docUrl(d.id)));

  if (todo.length) {
    const need = todo.reduce((s, d) => s + (Number(d.size_bytes) || 0), 0);
    const estimate = navigator.storage && navigator.storage.estimate
      ? await navigator.storage.estimate() : null;
    const warning = storageWarning(estimate, need);
    if (warning) emit('storage-warning', { message: warning });
  }

  const failed = [];
  for (const doc of todo) {
    try {
      await prefetchOne(doc);
    } catch (err) {
      failed.push({ doc, message: err.message });
    }
    emit('progress', await status());
  }
  const final = await status();
  emit('done', { ...final, failed });
  return { ...final, failed };
}

/* Open path is cache-only. Never `await fetch` here: offline that hangs until
 * the OS timeout, which is exactly the "loading state that never resolves"
 * §12.3 forbids. A miss fails immediately and says so. */
export async function openDocument(doc) {
  const cache = await caches.open(DOCS_CACHE);
  const hit = await cache.match(docUrl(doc.id));
  if (!hit) {
    const e = new Error('not-downloaded');
    e.code = 'NOT_DOWNLOADED';
    throw e;
  }
  const blob = await hit.blob();
  return URL.createObjectURL(blob);
}

/* Drop cached bytes for documents the server deleted, so a tombstoned file does
 * not sit on the device forever. */
export async function reconcile() {
  const docs = await idb.getAll('documents');
  const wanted = new Set(docs.map((d) => docUrl(d.id)));
  const cache = await caches.open(DOCS_CACHE);
  for (const req of await cache.keys()) {
    if (!wanted.has(new URL(req.url).pathname)) await cache.delete(req);
  }
  return status();
}

export async function requestPersistence() {
  if (!navigator.storage || !navigator.storage.persist) return null;
  const already = await navigator.storage.persisted();
  return already ? true : navigator.storage.persist();
}
