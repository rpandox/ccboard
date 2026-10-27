/* Release-scoped offline shell + Web Push. /sw.js stamps the board's asset
   version and complete shell list. Installation must finish before activation. */
'use strict';
const RELEASE = '__ASSET_VERSION__';
const CACHE = 'ccboard-shell-' + RELEASE;
const SHELL = JSON.parse('__SHELL_JSON__');

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    // addAll is atomic. A missing file rejects installation; the active worker
    // and its complete release remain available. Never activate a partial shell.
    const c = await caches.open(CACHE);
    await c.addAll(SHELL.map((u) => new Request(u, { cache: 'reload' })));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  // Keep older releases for documents still running their boot.js. Navigation
  // uses only this worker's complete shell; static lookups use the URL's release.
  event.waitUntil(self.clients.claim());
});

function offline() { return new Response('offline', { status: 503, headers: { 'Content-Type': 'text/plain' } }); }

async function releaseAsset(req, version) {
  const c = await caches.open('ccboard-shell-' + version);
  const hit = await c.match(req);
  if (hit) return hit;
  // Do not populate a complete release with responses from a later deployment.
  // The server refuses a version it no longer has. Exact URL lookup also keeps
  // pre-versioning workers from supplying an unversioned, incompatible loader.
  try { return await fetch(req); } catch (_) { return offline(); }
}

async function navigation(req) {
  try {
    const res = await fetch(req);
    if (res.ok || res.status < 500) return res;
  } catch (_) { /* use the installed release as one complete unit */ }
  const c = await caches.open(CACHE);
  return (await c.match('/?v=' + RELEASE)) || offline();
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const path = url.pathname;
  if (path.startsWith('/api/') || path.startsWith('/tty') || path === '/healthz') return;
  if (req.mode === 'navigate') {
    if (path === '/') event.respondWith(navigation(req));
    return;
  }
  if (path.startsWith('/static/')) {
    const version = url.searchParams.get('v');
    if (version) event.respondWith(releaseAsset(req, version));
    else if (path.endsWith('.woff2') || path.endsWith('.png') || path.startsWith('/static/vendor/')) {
      // CSS-relative fonts and icons have no query of their own. Use the
      // stylesheet's release when supplied, otherwise the active offline shell.
      const parent = req.referrer ? new URL(req.referrer) : null;
      const release = parent?.searchParams.get('v') || RELEASE;
      url.searchParams.set('v', release);
      event.respondWith(releaseAsset(new Request(url.href), release));
    }
  }
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
