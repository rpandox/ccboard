/* ccboard service worker: offline shell (network-first; vendor and fonts cache-first) + Web Push. API and terminal are never cached.
   This file is a template: /sw.js (app/main.py render_sw) fills in the build id and the list of static files,
   both generated from one glob over app/static, so no file can be forgotten. */
'use strict';
const CACHE = 'ccboard-shell-__ASSET_VERSION__';
const SHELL = JSON.parse('__SHELL_JSON__');

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    // cache: 'reload' skips the browser's HTTP cache: /static/vendor/** and fonts are served with a one-year immutable
    // Cache-Control, and only this versioned shell cache (its name carries the build id) decides when they change.
    try { const c = await caches.open(CACHE); await c.addAll(SHELL.map((u) => new Request(u, { cache: 'reload' }))); } catch (_) { /* offline or 403 at install: keep going */ }
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

function offline() { return new Response('offline', { status: 503, headers: { 'Content-Type': 'text/plain' } }); }

async function remember(key, res) {
  try { const c = await caches.open(CACHE); await c.put(key, res); } catch (_) { /* partial or opaque response: not cacheable */ }
}

// Network first, the cached copy when offline. `key` is what the response is stored under (the request, or '/' for a navigation).
async function networkFirst(req, key) {
  try {
    const res = await fetch(req);
    if (res.ok) remember(key, res.clone());
    return res;
  } catch (_) {
    const c = await caches.open(CACHE);
    return (await c.match(key)) || offline();
  }
}

// Cache first: vendor files and fonts are immutable under one build id, so a hit never touches the network; a miss (install
// failed offline) refetches past the HTTP cache so a stale immutable copy from an older build can never come back.
async function cacheFirst(req) {
  const c = await caches.open(CACHE);
  const hit = await c.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(new Request(req.url, { cache: 'reload' }));
    if (res.ok) remember(req, res.clone());
    return res;
  } catch (_) {
    return offline();
  }
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;
  if (path.startsWith('/api/') || path.startsWith('/tty') || path === '/healthz') return;
  if (req.mode === 'navigate') {
    if (path === '/') event.respondWith(networkFirst(req, '/'));   // /term/... and every other document: network only
    return;
  }
  if (path.startsWith('/static/vendor/') || path.endsWith('.woff2')) event.respondWith(cacheFirst(req));   // immutable (see main.py cache headers)
  else if (path.startsWith('/static/')) event.respondWith(networkFirst(req, req));
});

self.addEventListener('push', (event) => {
  let data = { title: 'ccboard', body: '', url: '/' };
  try { data = Object.assign(data, event.data ? event.data.json() : {}); } catch (_) { data.body = event.data ? event.data.text() : ''; }
  event.waitUntil(self.registration.showNotification(data.title, {
    body: data.body, tag: data.tag || undefined, data: { url: data.url || '/' }, icon: '/static/icon-192.png', badge: '/static/icon-192.png',
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of list) { if (new URL(c.url).origin === self.location.origin) { await c.navigate(url).catch(() => {}); return c.focus(); } }
    return self.clients.openWindow(url);
  })());
});
