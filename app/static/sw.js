/* ccboard service worker: offline shell (network-first) + Web Push. API and terminal are never cached. */
'use strict';
const CACHE = 'ccboard-shell-v1';
const SHELL = ['/', '/static/app.js', '/static/style.css', '/static/manifest.webmanifest', '/static/icon-192.png'];

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

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/tty') || url.pathname === '/healthz') return;
  const isShell = req.mode === 'navigate' || SHELL.includes(url.pathname);
  if (!isShell) return;
  event.respondWith((async () => {
    try {
      const res = await fetch(req);
      if (res.ok) { const c = await caches.open(CACHE); c.put(req.mode === 'navigate' ? '/' : req, res.clone()); }
      return res;
    } catch (_) {
      const cached = await caches.match(req.mode === 'navigate' ? '/' : req);
      return cached || new Response('offline', { status: 503, headers: { 'Content-Type': 'text/plain' } });
    }
  })());
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
