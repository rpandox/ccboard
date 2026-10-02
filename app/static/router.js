/* ccboard router: hash routes inside "/" (#/, #/inbox, #/p/<project>[/<repo>], ...). Pure parse/build helpers plus a
   page registry and one hashchange handler. Classic script (no modules). The only load-time side effect is the
   hashchange listener; v0.5.1 registers no pages, so route() leaves the legacy single page alone. history.pushState
   and history.replaceState live here and nowhere else. */
'use strict';

const ROUTES = [
  { id: 'home', re: /^\/?$/ },
  { id: 'inbox', re: /^\/inbox$/ },
  { id: 'tasks', re: /^\/tasks$/ },
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

/* Page registry: page = { mount(root, route), update(state, route), onRoute?(route), unmount?(), title? }. */
const pages = {};
let currentRouteValue = null;
let mountedId = null;

function registerPage(id, page) { pages[id] = page; }

function currentRoute() {
  if (!currentRouteValue && typeof location !== 'undefined') currentRouteValue = parseHash(location.hash);
  return currentRouteValue;
}

function sameParams(a, b) { return JSON.stringify(a || {}) === JSON.stringify(b || {}); }

function route() {
  const r = parseHash(location.hash);
  const prev = currentRouteValue;
  currentRouteValue = r;
  const page = pages[r.id];
  if (!page) return;                      // nothing registered: the legacy single page stays as it is
  const root = document.querySelector('#page');
  if (!root) return;
  if (mountedId === r.id && prev && sameParams(prev.params, r.params) && typeof page.onRoute === 'function') {
    try { page.onRoute(r); } catch (e) { console.error('ccboard route', r.id, e); }
    return;
  }
  const old = mountedId && pages[mountedId];
  if (old && typeof old.unmount === 'function') { try { old.unmount(); } catch (e) { console.error('ccboard unmount', mountedId, e); } }
  root.textContent = '';
  mountedId = r.id;
  try {
    page.mount(root, r);
    if (page.title) document.title = (typeof page.title === 'function' ? page.title(r) : page.title) + ' · ccboard';
    if (typeof state !== 'undefined' && state && typeof page.update === 'function') page.update(state, r);
  } catch (e) { console.error('ccboard mount', r.id, e); }
}

function navigate(hash, opts) {
  const h = String(hash).charAt(0) === '#' ? String(hash) : '#' + hash;
  if (opts && opts.replace) { history.replaceState(null, '', h); route(); }
  else location.hash = h;
}

/* The old ntfy deep link #s=<tmux> becomes #/s/<tmux> in place (no reload, no new history entry). */
function rewriteLegacyHash() {
  const m = LEGACY_HASH_RE.exec(location.hash || '');
  if (!m) return false;
  history.replaceState(null, '', '#/s/' + m[1]);
  return true;
}

if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('hashchange', route);
