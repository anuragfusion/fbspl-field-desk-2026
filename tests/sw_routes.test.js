/* node --test tests/
 *
 * The service worker controls the whole origin. A navigation rule that is one
 * condition too broad serves the floor shell for /admin, and the office console
 * vanishes at its own URL with no error anywhere. Assert the scope.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const SW = fileURLToPath(new URL('../static/sw.js', import.meta.url));

/* The server fills these in (app/main.py render_service_worker); stand in for it. */
const TEST_URLS = ['/app', '/static/app.css', '/static/app.js', '/static/client_filters.js',
  '/static/idb.js', '/static/sync.js', '/static/sync_core.js', '/static/docs.js'];
function renderedSw() {
  return fs.readFileSync(SW, 'utf8')
    .replace("'__SHELL_VERSION__'", JSON.stringify('test'))
    .replace('[/* __SHELL_URLS__ */]', JSON.stringify(TEST_URLS));
}

/* Run sw.js in a bare context and hand back its fetch handler. */
function loadSw(spy = {}) {
  const handlers = {};
  const self = {
    addEventListener: (type, fn) => { handlers[type] = fn; },
    location: { origin: 'http://127.0.0.1:8099' },
    clients: { claim: () => Promise.resolve() },
    skipWaiting: () => {},
  };
  const ctx = {
    self,
    caches: { open: () => Promise.resolve({ match: () => Promise.resolve(null) }),
              match: () => Promise.resolve(null), keys: () => Promise.resolve([]) },
    fetch: (u, opts) => { (spy.fetched ||= []).push([u, opts]); return Promise.resolve({ ok: true, url: u }); },
    Response: class {},
    URL,
    JSON,
    Promise,
    Set,
  };
  ctx.caches.open = () => Promise.resolve({
    match: () => Promise.resolve(null),
    put: (u, res) => { (spy.put ||= []).push(u); return Promise.resolve(); },
  });
  vm.createContext(ctx);
  vm.runInContext(renderedSw(), ctx);
  return Object.assign(handlers.fetch, { handlers });
}

/* Returns true if the worker took over the response for this request. */
function intercepted(onFetch, path, mode = 'navigate', method = 'GET', headers = {}) {
  let took = false;
  onFetch({
    request: {
      url: 'http://127.0.0.1:8099' + path,
      mode,
      method,
      headers: { get: (k) => headers[k.toLowerCase()] ?? null },
    },
    respondWith: () => { took = true; },
  });
  return took;
}

test('server-rendered pages are never answered with the app shell', () => {
  const onFetch = loadSw();
  for (const p of ['/admin', '/admin/login', '/admin/users/new', '/docs', '/healthz']) {
    assert.equal(intercepted(onFetch, p), false, `worker hijacked navigation to ${p}`);
  }
});

test('the floor app still boots from cache', () => {
  const onFetch = loadSw();
  assert.equal(intercepted(onFetch, '/app'), true);
  assert.equal(intercepted(onFetch, '/'), true);
});

test('shell assets stay cache-first and API calls stay network-only', () => {
  const onFetch = loadSw();
  assert.equal(intercepted(onFetch, '/static/app.js', 'no-cors'), true);
  assert.equal(intercepted(onFetch, '/api/v1/events/x/sync', 'cors'), false);
  assert.equal(intercepted(onFetch, '/api/v1/documents/abc/content', 'cors'), true);
});

test('the pre-fetcher can reach the network, everyone else cannot', () => {
  const onFetch = loadSw();
  const doc = '/api/v1/documents/abc/content';
  /* Without the marker the worker answers from cache only — that is the whole
   * offline guarantee. With it, the request must pass through, or nothing ever
   * fills the cache and every document stays undownloadable. */
  assert.equal(intercepted(onFetch, doc, 'cors'), true);
  assert.equal(intercepted(onFetch, doc, 'cors', 'GET', { 'x-fielddesk-prefetch': '1' }), false);
});

test('install goes to the network, never through the HTTP cache', async () => {
  /* /static has no Cache-Control, so a plain addAll() refills a bumped shell
   * with the same stale files: version changes, bug stays, fix never lands. */
  const spy = {};
  const onFetch = loadSw(spy);
  let work;
  onFetch.handlers.install({ waitUntil: (p) => { work = p; } });
  await work;

  assert.ok(spy.fetched.length >= 8, 'shell files were not fetched on install');
  for (const [url, opts] of spy.fetched) {
    assert.equal(opts && opts.cache, 'reload', `${url} was allowed to come from the HTTP cache`);
  }
  assert.ok(spy.put.includes('/static/app.js'), 'app.js was not cached on install');
});
