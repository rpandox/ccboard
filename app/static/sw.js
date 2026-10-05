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
    if (res.ok) { remember(key, res.clone()); return res; }
    if (res.status >= 500) {                                   // the proxy answered for a board that is restarting (a deploy, a busy box):
      const c = await caches.open(CACHE);                      // show the last shell; the page's own poll paints 'offline' and retries
      const hit = await c.match(key);
      if (hit) return hit;
    }
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

/* ---- Web Push (app/push.py payload_for, app/notify.py Notice.web_extra) ----
   payload: {title, body, url, tag, renotify, agent, state, tmux, perm_id, actions:[{action,title}], badge, ts}. Only title/body/url/tag
   are guaranteed: an older board sends just those and the notification still renders. iOS ignores action buttons: the notification
   itself (title, body, tap -> #/s/<tmux>) carries everything there and the peek sheet has Allow / Deny / Ack. */
const ICON = '/static/icon-192.png';

function pushData(event) {
  let data = { title: 'ccboard', body: '', url: '/' };
  try { data = Object.assign(data, event.data ? event.data.json() : {}); } catch (_) { data.body = event.data ? event.data.text() : ''; }
  return data;
}

function hasId(v) { return v !== undefined && v !== null && v !== ''; }

/* showNotification options for a payload. renotify only with a tag (the browser throws otherwise); two action buttons at most (Chrome on
   Android shows two); a notification that waits on you or on a permission stays until it is dealt with. */
function pushOptions(data) {
  const o = { body: String(data.body || ''), icon: ICON, badge: ICON,
              data: { url: data.url || '/', tmux: data.tmux || '', perm_id: hasId(data.perm_id) ? data.perm_id : null, state: data.state || '', agent: data.agent || '' } };
  if (data.tag) { o.tag = String(data.tag); if (data.renotify) o.renotify = true; }
  if (data.state === 'waiting' || hasId(data.perm_id)) o.requireInteraction = true;
  if (typeof data.ts === 'number' && data.ts > 0) o.timestamp = data.ts;
  if (Array.isArray(data.actions)) {
    const acts = data.actions.filter((a) => a && a.action && a.title).slice(0, 2).map((a) => ({ action: String(a.action), title: String(a.title) }));
    if (acts.length) o.actions = acts;
  }
  return o;
}

async function setBadge(n) {
  if (typeof n !== 'number' || n < 0) return;
  try {
    const nav = self.navigator;
    if (n > 0 && nav && nav.setAppBadge) await nav.setAppBadge(n);
    else if (n === 0 && nav && nav.clearAppBadge) await nav.clearAppBadge();
  } catch (_) { /* no Badging API here, or the badge was refused */ }
}

self.addEventListener('push', (event) => {
  const data = pushData(event);
  event.waitUntil(Promise.all([self.registration.showNotification(String(data.title || 'ccboard'), pushOptions(data)), setBadge(data.badge)]));
});

/* A short note in place of the notification the person just answered from the lock screen; it replaces it (same tag), makes no sound and
   goes away by itself. */
async function confirmation(n, d, text, url) {
  const o = { body: text, icon: ICON, badge: ICON, silent: true, data: { url: url || d.url || '/', tmux: d.tmux || '' } };
  if (n.tag) o.tag = n.tag;
  await self.registration.showNotification(n.title || 'ccboard', o);
  await new Promise((r) => setTimeout(r, 4000));
  try { for (const x of await self.registration.getNotifications(n.tag ? { tag: n.tag } : {})) if (x.body === text) x.close(); } catch (_) { /* nothing to close */ }
}

async function answer(n, d, verb, path, ok409) {
  try {
    const r = await self.fetch(path, { method: 'POST', headers: { 'X-CCBoard': '1' }, credentials: 'same-origin' });
    if (r.ok) return confirmation(n, d, verb);
    if (r.status === 409 || r.status === 404) return confirmation(n, d, ok409);
    return confirmation(n, d, 'ccboard said ' + r.status + '. Tap to open the board.');
  } catch (_) {
    return confirmation(n, d, 'Could not reach ccboard. Tap to open the board.');
  }
}

function sameOrigin(url) {
  try { const u = new URL(url, self.location.origin); return u.origin === self.location.origin ? u.href : null; } catch (_) { return null; }
}

/* Plain tap: focus the open board window and tell it where to go (router.js listens for {type:'nav', url}); with none open, a new window. */
async function openBoard(url) {
  const href = sameOrigin(url || '/') || self.location.origin + '/';
  const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const board = list.find((c) => { try { const u = new URL(c.url); return u.origin === self.location.origin && u.pathname === '/'; } catch (_) { return false; } });
  if (board) {
    try { board.postMessage({ type: 'nav', url: url || '/' }); await board.focus(); return; } catch (_) { /* fall through */ }
    try { await board.navigate(href); return; } catch (_) { /* fall through */ }
  }
  return self.clients.openWindow(href);
}

self.addEventListener('notificationclick', (event) => {
  const n = event.notification;
  const d = n.data || {};
  const action = event.action || '';
  n.close();
  if ((action === 'allow' || action === 'deny') && hasId(d.perm_id)) {
    event.waitUntil(answer(n, d, action === 'allow' ? 'Allowed.' : 'Denied.', '/api/permission/' + encodeURIComponent(d.perm_id) + '/' + action, 'Already answered.'));
  } else if (action === 'ack') {                               // Ack with no session (the settings test push) only closes the notification
    if (d.tmux) event.waitUntil(answer(n, d, 'Marked as seen.', '/api/sessions/' + encodeURIComponent(d.tmux) + '/ack', 'Already gone.'));
  } else if (action === 'terminal' && d.tmux) {
    event.waitUntil(self.clients.openWindow(sameOrigin('/term/' + encodeURIComponent(d.tmux)) || self.location.origin + '/'));
  } else {
    event.waitUntil(openBoard(d.url));
  }
});
