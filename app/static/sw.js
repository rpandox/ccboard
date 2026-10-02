/* ccboard service worker: offline shell (network-first) + Web Push. API and terminal are never cached.
   This file is a template: /sw.js (app/main.py render_sw) fills in the build id and the list of static files,
   both generated from one glob over app/static, so no file can be forgotten. */
'use strict';
const CACHE = 'ccboard-shell-__ASSET_VERSION__';
const SHELL = JSON.parse('__SHELL_JSON__');

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    try { const c = await caches.open(CACHE); await c.addAll(SHELL); } catch (_) { /* offline or 403 at install: keep going */ }
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

// Cache first: fonts never change under the same name, so a hit never touches the network.
async function cacheFirst(req) {
  const c = await caches.open(CACHE);
  const hit = await c.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
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
  if (path.startsWith('/static/vendor/fonts/')) event.respondWith(cacheFirst(req));
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
