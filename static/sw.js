/* Service worker. Served from / (see app/main.py) — a worker can only control
 * paths at or below its own URL, so it cannot live under /static.
 *
 * Bump SHELL_VERSION by hand when any shell file changes. No build step means no
 * content hashes; the app compares this against /api/v1/version when online and
 * nags if they differ.
 */

const SHELL_VERSION = 6;
const SHELL = `shell-v${SHELL_VERSION}`;
const DOCS = 'docs-v1';

const SHELL_URLS = [
  '/app',
  '/static/app.css',
  '/static/app.js',
  '/static/client_filters.js',
  '/static/idb.js',
  '/static/sync.js',
  '/static/sync_core.js',
  '/static/docs.js',
  '/static/manifest.webmanifest',
  '/static/fbspl_logo.png',
];

/* cache.addAll() reads through the HTTP cache. /static is served without a
 * Cache-Control header, so Chrome caches it heuristically and a bumped shell
 * happily fills itself with the SAME STALE FILES — the version number changes,
 * the bug does not, and the fix you shipped never reaches the device. Fetch each
 * one with cache:'reload' so install always goes to the network, and fail the
 * install (rather than caching an error page) if any of them is not a 200. */
self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(SHELL)
      .then((c) => Promise.all(SHELL_URLS.map(async (u) => {
        const res = await fetch(u, { cache: 'reload' });
        if (!res.ok) throw new Error(`shell install: ${u} -> ${res.status}`);
        return c.put(u, res);
      })))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        /* Sweep old SHELL caches only. The usual prefix-sweep snippet would
         * delete DOCS on every shell bump — 62 MB of prefetched PDFs gone, on
         * the morning of the event, silently. */
        keys.filter((k) => k !== SHELL && k !== DOCS).map((k) => caches.delete(k)),
      ))
      .then(() => self.clients.claim()),
  );
});

const DOC_RE = /^\/api\/v1\/documents\/[^/]+\/content$/;
const SHELL_ROUTES = new Set(['/', '/app']);
const PREFETCH_HEADER = 'x-fielddesk-prefetch';

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  /* Outbox POSTs and every other write go straight to the network. */
  if (e.request.method !== 'GET') return;
  if (url.origin !== self.location.origin) return;

  /* Document bytes: cache-only. A miss returns 504 immediately rather than
   * falling through to the network and hanging offline (§12.3). */
  if (DOC_RE.test(url.pathname)) {
    /* ...except for the pre-fetcher, which is the only thing that ever fills
     * this cache. Without this it asks for bytes, the worker answers 504 from
     * an empty cache, every document fails, readiness sits at 0 of N forever
     * and the floor team has no documents at all. */
    if (e.request.headers.get(PREFETCH_HEADER)) return;
    e.respondWith(
      caches.open(DOCS)
        .then((c) => c.match(e.request))
        .then((hit) => hit || new Response(
          JSON.stringify({ error: { code: 'NOT_DOWNLOADED', message: 'Not downloaded.' } }),
          { status: 504, headers: { 'content-type': 'application/json' } },
        )),
    );
    return;
  }

  /* API JSON: network-only, never cached. The IndexedDB replica is the single
   * offline source of truth; a second copy in Cache Storage would drift, and
   * you would be debugging that drift in a convention hall. */
  if (url.pathname.startsWith('/api/')) return;

  /* Navigations: serve the app shell for the floor app's own routes so a cold
   * offline launch works — and ONLY those. /admin, /admin/login and /docs are
   * server-rendered pages; answering them with the cached shell makes the whole
   * office console disappear behind a copy of the floor app, at its own URL,
   * with no error to go on. Those are useless offline anyway, so they go to the
   * network and fail honestly. */
  if (e.request.mode === 'navigate') {
    if (SHELL_ROUTES.has(url.pathname)) {
      e.respondWith(caches.match('/app').then((hit) => hit || fetch(e.request)));
    }
    return;
  }

  /* Everything else (shell assets): cache-first. */
  e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
});

self.addEventListener('message', (e) => {
  if (e.data === 'skip-waiting') self.skipWaiting();
});
