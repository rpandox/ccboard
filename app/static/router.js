/* ccboard router: hash routes inside "/" (#/, #/inbox, #/agents, #/p/<project>[/<repo>], ...). Pure parse/build helpers,
   a page registry, one hashchange handler and the page plumbing every page shares (title, aria-current, scroll restore,
   keyed lists, toast/empty fallbacks). Classic script (no modules). Load-time side effects: the hashchange listener and,
   when a service worker exists, its 'nav' message listener. history.pushState and history.replaceState live here and
   nowhere else. */
'use strict';

const ROUTES = [
  { id: 'home', re: /^\/?$/ },
  { id: 'inbox', re: /^\/inbox$/ },
  { id: 'tasks', re: /^\/tasks$/ },
  { id: 'agents', re: /^\/agents$/ },
  { id: 'project', re: /^\/p\/([A-Za-z0-9_-]+)(?:\/([A-Za-z0-9_-]+))?$/, params: ['project', 'repo'] },
  { id: 'quad', re: /^\/quad$/ },
  { id: 'usage', re: /^\/usage$/ },
  { id: 'memory', re: /^\/memory(?:\/([A-Za-z0-9_-]+))?$/, params: ['project'] },
  { id: 'onboarding', re: /^\/onboarding(?:\/(project))?$/, params: ['step'] },
  { id: 'settings', re: /^\/settings$/ },
  { id: 'search', re: /^\/search$/ },
  { id: 'session', re: /^\/s\/([A-Za-z0-9_-]+)$/, params: ['tmux'] },
];

const ROUTE_PARAM_RE = /^[A-Za-z0-9_-]+$/;
const LEGACY_HASH_RE = /^#s=([A-Za-z0-9_-]+)$/;

function parseHash(hash) {
  let h = hash === null || hash === undefined ? '' : String(hash);
  if (h.charAt(0) === '#') h = h.slice(1);
  const legacy = /^s=([A-Za-z0-9_-]+)$/.exec(h);   // the old ntfy click target, #s=<tmux>
  if (legacy) return { id: 'session', params: { tmux: legacy[1] }, query: {}, legacy: true };
  const q = h.indexOf('?');
  const path = q >= 0 ? h.slice(0, q) : h;
  const query = {};
  if (q >= 0) for (const [k, v] of new URLSearchParams(h.slice(q + 1))) query[k] = v;
  for (const r of ROUTES) {
    const m = r.re.exec(path);
    if (!m) continue;
    const params = {};
    (r.params || []).forEach((name, i) => { if (m[i + 1] !== undefined) params[name] = m[i + 1]; });
    return { id: r.id, params, query };
  }
  return { id: 'home', params: {}, query, unknown: true };
}

function buildHash(id, params, query) {
  const p = params || {};
  for (const v of Object.values(p)) {
    if (v === null || v === undefined) continue;
    if ((typeof v !== 'string' && typeof v !== 'number') || !ROUTE_PARAM_RE.test(String(v))) throw new Error('bad route param');
  }
  const need = (k) => {
    const v = p[k];
    if (v === null || v === undefined || !ROUTE_PARAM_RE.test(String(v))) throw new Error('bad route param');
    return String(v);
  };
  const opt = (k) => (p[k] === null || p[k] === undefined ? '' : String(p[k]));
  if (typeof id !== 'string' || !ROUTE_PARAM_RE.test(id)) throw new Error('bad route param');
  let h;
  if (id === 'home') h = '#/';
  else if (id === 'project') h = '#/p/' + need('project') + (opt('repo') ? '/' + opt('repo') : '');
  else if (id === 'session') h = '#/s/' + need('tmux');
  else if (id === 'memory') h = '#/memory' + (opt('project') ? '/' + opt('project') : '');
  else if (id === 'onboarding') {
    if (opt('step') && opt('step') !== 'project') throw new Error('bad route param');
    h = '#/onboarding' + (opt('step') ? '/project' : '');
  } else h = '#/' + id;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(query || {})) if (v !== null && v !== undefined) qs.set(k, String(v));
  const s = qs.toString();
  return s ? h + '?' + s : h;
}

/* Page registry: page = { mount(root, route), update(state, route), onRoute?(route), unmount?(), title?, noFocus? }.
   title is a string or (route) => string. */
const pages = {};
let currentRouteValue = null;
let mountedId = null;
let mountedHash = '';
let overlayId = null;        // an overlay page (the session peek) mounted on top of the base page in mountedId
let routeCount = 0;          // routes handled in this document: more than one means history.back() stays inside the app
let routing = false;
let lazyToken = 0;           // bumped by every route(): a bundle that arrives after the person went elsewhere is ignored
let lazyRetry = false;       // route() is running again for a bundle that just arrived: not a new navigation (routeCount stays)
let lazyHint = null;         // the timer of the 'Loading…' line

function registerPage(id, page) { pages[id] = page; }

function currentRoute() {
  if (!currentRouteValue && typeof location !== 'undefined') currentRouteValue = parseHash(location.hash);
  return currentRouteValue;
}

function hashNow() { return location.hash ? location.hash : '#/'; }

function currentState() { return typeof state !== 'undefined' && state ? state : null; }

/* The one call the shell's render() makes after patching its chrome: update the mounted page with the new state. */
function updateCurrentPage(st) {
  const s = st || currentState();
  if (!s) return;
  const r = currentRoute();
  for (const id of [mountedId, overlayId]) {
    const page = id && pages[id];
    if (!page || typeof page.update !== 'function') continue;
    try { page.update(s, r); } catch (e) { console.error('ccboard update', id, e); }
  }
}

function unmountOverlay() {
  const o = overlayId && pages[overlayId];
  if (o && typeof o.unmount === 'function') { try { o.unmount(); } catch (e) { console.error('ccboard unmount', overlayId, e); } }
  overlayId = null;
}

/* Repaint after a local change (confirmButton calls renderProjects(), which falls through to this when the board is not mounted). */
function repaintPage() { updateCurrentPage(currentState()); }

function refreshTitle() {
  const page = (overlayId && pages[overlayId]) || (mountedId && pages[mountedId]);
  if (!page || !page.title || typeof document === 'undefined') return;
  const t = typeof page.title === 'function' ? page.title(currentRoute()) : page.title;
  document.title = t ? t + ' · ccboard' : 'ccboard';
}

/* The drawer closes on every route; the sheet only when a page is remounted (a page that takes onRoute keeps its own sheet open). */
function routeCloseChrome(withSheet) {
  try { if (typeof closeDrawer === 'function') closeDrawer(); } catch (e) { console.error('ccboard closeDrawer', e); }
  if (!withSheet) return;
  try { if (typeof closeSheet === 'function') closeSheet(); } catch (e) { console.error('ccboard closeSheet', e); }
}

/* aria-current on every nav link of the shell: <a href="#/..."> inside the topbar, sidebar, bottom nav or drawer, and
   anything carrying data-route="<route id>". A link matches when its route id (and the params it names) match. */
function updateNav() {
  const cur = currentRouteValue;
  if (!cur || typeof document.querySelectorAll !== 'function') return;
  let nodes = [];
  try {
    nodes = Array.from(document.querySelectorAll('#topbar a[href^="#/"], #sidebar a[href^="#/"], #bnav a[href^="#/"], #drawer a[href^="#/"], #topbar [data-route], #sidebar [data-route], #bnav [data-route], #drawer [data-route]'));
  } catch (_) { return; }
  for (const n of nodes) {
    let target = null;
    const dr = n.getAttribute('data-route');
    if (dr) target = { id: dr, params: {} };
    else { const t = parseHash(n.getAttribute('href')); if (!t.unknown) target = t; }
    let on = !!target && target.id === cur.id;
    if (on) for (const [k, v] of Object.entries(target.params || {})) if (cur.params[k] !== v) on = false;
    if (on) n.setAttribute('aria-current', 'page'); else n.removeAttribute('aria-current');
  }
}

function scrollKey(hash) { return 'ccboard:scroll:' + hash; }

function saveScroll() {
  if (!mountedHash) return;
  try {
    const m = document.querySelector('#main');
    const y = Math.round(typeof window.scrollY === 'number' ? window.scrollY : 0);
    const mt = m ? Math.round(m.scrollTop || 0) : 0;
    if (y || mt) sessionStorage.setItem(scrollKey(mountedHash), y + ':' + mt); else sessionStorage.removeItem(scrollKey(mountedHash));
  } catch (_) { /* storage may be unavailable */ }
}

function restoreScroll(hash) {
  let y = 0;
  let mt = 0;
  try {
    const v = sessionStorage.getItem(scrollKey(hash));
    if (v) { const parts = v.split(':'); y = parseInt(parts[0], 10) || 0; mt = parseInt(parts[1], 10) || 0; }
  } catch (_) { /* storage may be unavailable */ }
  const apply = () => {
    try {
      if (typeof window.scrollTo === 'function') window.scrollTo(0, y);
      const m = document.querySelector('#main');
      if (m) m.scrollTop = mt;
    } catch (_) { /* no layout yet */ }
  };
  apply();
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(apply);
}

function focusMain() {
  try {
    const m = document.querySelector('#main');
    if (m && typeof m.focus === 'function') m.focus({ preventScroll: true });
  } catch (_) { /* not focusable yet */ }
}

function route() {
  let r = parseHash(location.hash);
  if (r.legacy) rewriteLegacyHash();
  else if (r.unknown) {
    const h = location.hash || '';
    const anchor = h.charAt(1) === '/' ? '' : h.slice(1);
    // an in-page anchor such as the skip link (#main) is not a route: leave the page alone
    if (anchor && typeof document.getElementById === 'function' && document.getElementById(anchor)) return;
    history.replaceState(null, '', '#/');
    r = parseHash('#/');
  }
  if (!lazyRetry) { routeCount += 1; lazyToken += 1; }
  clearLazyHint();
  const prev = currentRouteValue;
  currentRouteValue = r;
  const page = pages[r.id];
  const wait = typeof Lazy !== 'undefined' && !lazyRetry ? Lazy.pending(r.id) : [];
  if (wait.length) { routeLazy(r, wait); return; }       // the page's script (and its sheet) is not here yet: load it, then come back to this route
  if (!page) return;                      // nothing registered: the page that is mounted stays as it is
  const root = document.querySelector('#page');
  if (!root) return;
  routing = true;
  try {
    if (page.overlay) {
      // the peek opens over whatever is mounted (the home page when nothing is yet) and never clears #page
      if (!mountedId && pages.home) {
        const home = parseHash('#/');
        mountedId = 'home'; mountedHash = '#/';
        try { pages.home.mount(root, home); const st0 = currentState(); if (st0 && typeof pages.home.update === 'function') pages.home.update(st0, home); } catch (e) { console.error('ccboard mount home', e); }
      }
      if (overlayId && overlayId !== r.id) unmountOverlay();
      routeCloseChrome(false);
      if (overlayId === r.id && typeof page.onRoute === 'function') {
        try { page.onRoute(r); } catch (e) { console.error('ccboard route', r.id, e); }
      } else {
        overlayId = r.id;
        try { page.mount(root, r); const st = currentState(); if (st && typeof page.update === 'function') page.update(st, r); } catch (e) { console.error('ccboard mount', r.id, e); }
      }
      try { document.body.setAttribute('data-page', r.id); } catch (_) { /* no body */ }
      refreshTitle();
      updateNav();
      return;
    }
    if (overlayId) {
      unmountOverlay();
      if (mountedId === r.id && hashNow() === mountedHash) {      // closing the peek: the base page is still there
        routeCloseChrome(false);
        try { document.body.setAttribute('data-page', r.id); } catch (_) { /* no body */ }
        refreshTitle();
        updateNav();
        return;
      }
    }
    if (mountedId === r.id && prev && typeof page.onRoute === 'function') {
      routeCloseChrome(false);
      saveScroll();
      mountedHash = hashNow();
      try { page.onRoute(r); } catch (e) { console.error('ccboard route', r.id, e); }
      refreshTitle();
      updateNav();
      return;
    }
    routeCloseChrome(true);               // before mounting: the peek opens a sheet and must not be closed by this route
    saveScroll();
    const old = mountedId && pages[mountedId];
    if (old && typeof old.unmount === 'function') { try { old.unmount(); } catch (e) { console.error('ccboard unmount', mountedId, e); } }
    root.textContent = '';
    mountedId = r.id;
    mountedHash = hashNow();
    try { document.body.setAttribute('data-page', r.id); } catch (_) { /* no body */ }
    try {
      page.mount(root, r);
      const st = currentState();
      if (st && typeof page.update === 'function') page.update(st, r);
    } catch (e) { console.error('ccboard mount', r.id, e); }
    refreshTitle();
    updateNav();
    if (!page.noFocus) focusMain();
    restoreScroll(mountedHash);
  } finally { routing = false; }
}

/* ---------- lazy pages (lazy.js): a route whose script is not loaded yet ---------- */

function clearLazyHint() {
  if (lazyHint) { clearTimeout(lazyHint); lazyHint = null; }
  try { document.body.removeAttribute('data-page-loading'); } catch (_) { /* no body */ }
}

/* The mounted page (and a peek over it) goes, as it does just before the next page mounts. A lazy route does this when it starts to load, not when it is ready: a page that is
   still up keeps working, and some of them (the quad, the filters of a list) write their own address now and then, which would put the old route back in the address bar
   and make the late mount follow it instead of the click. */
function takeDownPage() {
  const root = document.querySelector('#page');
  routeCloseChrome(true);
  unmountOverlay();
  saveScroll();
  const old = mountedId && pages[mountedId];
  if (old && typeof old.unmount === 'function') { try { old.unmount(); } catch (e) { console.error('ccboard unmount', mountedId, e); } }
  mountedId = null;
  mountedHash = '';
  if (root) root.textContent = '';
  return root;
}

/* Load the bundles a route waits for, then route() again for the same address. The page that was up goes at once (takeDownPage); 'Loading…' shows after a beat for a slow load;
   a bundle that cannot be fetched leaves an error state with a Retry, never a blank page. If the person moved on meanwhile (lazyToken changed) a late arrival only registers its page. */
function routeLazy(r, wait) {
  const token = lazyToken;
  updateNav();
  let root = null;
  routing = true;                         // a close event of the sheet or peek that goes down with the page is not a navigation (goBack looks at this)
  try { root = takeDownPage(); } finally { routing = false; }
  try { document.body.setAttribute('data-page-loading', r.id); } catch (_) { /* no body */ }
  if (root) {
    lazyHint = setTimeout(() => {
      lazyHint = null;
      if (token !== lazyToken || mountedId) return;
      root.textContent = '';
      root.append(el('div', { class: 'dim lazy-hint', role: 'status', text: 'Loading…' }));
    }, 250);
  }
  Promise.all(wait.map((n) => Lazy.load(n))).then(() => {
    if (token !== lazyToken) return;
    clearLazyHint();
    lazyRetry = true;
    try { route(); } finally { lazyRetry = false; }
  }, (err) => {
    if (token !== lazyToken) return;
    clearLazyHint();
    showLoadError(r, err);
  });
}

/* The page could not be loaded: say so, with a Retry that asks again (Lazy forgot the failure). */
function showLoadError(r, err) {
  console.error('ccboard load', r.id, err);
  routing = true;
  try {
    const root = takeDownPage();
    if (!root) return;
    try { document.body.setAttribute('data-page', r.id); } catch (_) { /* no body */ }
    const retry = el('button', { class: 'btn small', type: 'button', text: 'Retry', onclick: () => { lazyToken += 1; lazyRetry = false; route(); } });
    root.append(el('div', { class: 'load-error', role: 'alert' }, pageEmpty('warning-sign', 'This page could not be loaded', 'The connection dropped or the board is restarting. Try again in a moment.'), el('div', { class: 'actions' }, retry)));
  } finally { routing = false; }
}

function navigate(hash, opts) {
  const h = String(hash).charAt(0) === '#' ? String(hash) : '#' + hash;
  if (opts && opts.replace) { history.replaceState(null, '', h); route(); }
  else location.hash = h;
}

/* Close a panel that was opened by a route (the session peek): back when the app itself got us here, else to `fallback`. */
function goBack(fallback) {
  if (routing) return;                    // a close event fired by this very route change: already navigating
  if (routeCount > 1 && typeof history.back === 'function') { history.back(); return; }
  navigate(fallback || '#/', { replace: true });
}

/* The old ntfy deep link #s=<tmux> becomes #/s/<tmux> in place (no reload, no new history entry). */
function rewriteLegacyHash() {
  const m = LEGACY_HASH_RE.exec(location.hash || '');
  if (!m) return false;
  history.replaceState(null, '', '#/s/' + m[1]);
  return true;
}

/* A page or snippet shared to the installed app arrives as GET /?title=&text=&url= (the manifest share_target); Palette opens its "send to session"
   list with it. Once handled (sent or dismissed) the three params leave the address so a reload does not offer them again. The hash and every
   other param (demo=1) stay; one replaceState, no history entry. Returns whether anything was removed. */
const SHARE_PARAMS = ['text', 'title', 'url', 'share'];

function stripShare() {
  if (typeof location === 'undefined' || !location.search) return false;
  const q = new URLSearchParams(location.search);
  let had = false;
  for (const k of SHARE_PARAMS) if (q.has(k)) { q.delete(k); had = true; }
  if (!had) return false;
  const s = q.toString();
  history.replaceState(null, '', (location.pathname || '/') + (s ? '?' + s : '') + (location.hash || ''));
  return true;
}

const Router = { stripShare };

/* ---------- helpers shared by the pages ---------- */

function setTextIfChanged(node, text) { if (node.textContent !== text) node.textContent = text; }

/* A keyed, in-place list: nodes live in a Map by key, are created once, patched on every update and only moved when
   their position changed. A node that holds focus is never moved or removed-and-recreated, so a poll cannot steal
   focus from a button or input. opts = { key(item), create(item) -> node, patch?(node, item) }. */
function makeKeyedList(parent, opts) {
  const nodes = new Map();
  const holdsFocus = (n) => { const a = document.activeElement; return !!(a && typeof n.contains === 'function' && n.contains(a)); };
  return {
    nodes,
    update(items) {
      const order = [];
      const seen = new Set();
      for (const item of items) {
        const key = opts.key(item);
        let n = nodes.get(key);
        if (!n) { n = opts.create(item); nodes.set(key, n); }
        else if (opts.patch) opts.patch(n, item);
        seen.add(key);
        order.push(n);
      }
      for (const [key, n] of Array.from(nodes)) {
        if (seen.has(key)) continue;
        nodes.delete(key);
        if (typeof n.remove === 'function') n.remove();
      }
      let ref = parent.firstElementChild;
      for (const n of order) {
        if (n === ref) { ref = ref.nextElementSibling; continue; }
        if (n.parentNode === parent && holdsFocus(n)) continue;
        parent.insertBefore(n, ref);
      }
    },
    clear() {
      for (const n of nodes.values()) if (typeof n.remove === 'function') n.remove();
      nodes.clear();
    },
  };
}

/* components.js's emptyState / toast belong to the shell slice: degrade to plain nodes and the banner when they are missing. */
function pageEmpty(icon, title, hint) {
  if (typeof emptyState === 'function') return emptyState(icon, title, hint);
  return el('div', { class: 'empty dim' }, el('b', { text: title }), hint ? el('div', { text: hint }) : null);
}

function pageToast(text, kind) {
  if (typeof toast === 'function') { toast(text, { kind: kind || 'info' }); return; }
  if (kind === 'bad') { setError(text); return; }
  ui.notice = text;
  if (typeof renderBanner === 'function') renderBanner();
}

if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('hashchange', route);

/* A service worker can ask the page to navigate (push second tap, notification actions): {type:'nav', url:'/#/s/<tmux>'}. */
if (typeof navigator !== 'undefined' && navigator.serviceWorker && typeof navigator.serviceWorker.addEventListener === 'function') {
  navigator.serviceWorker.addEventListener('message', (e) => {
    const d = e && e.data;
    if (!d || d.type !== 'nav' || typeof d.url !== 'string') return;
    const i = d.url.indexOf('#');
    const h = i >= 0 ? d.url.slice(i) : '#/';
    if (!/^#(\/|s=)/.test(h)) return;
    if (location.hash === h) route(); else location.hash = h;
  });
}
