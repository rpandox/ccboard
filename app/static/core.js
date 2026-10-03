/* ccboard core: DOM factory with the Blueprint mapping, API client, shared state, formatting, storage, PWA
   helpers and the state poll. Definitions only: nothing here touches the DOM or starts a timer at load;
   main.js boots the page. Classic script (no modules), loaded first by index.html and term.html. */
'use strict';

const $ = (sel) => document.querySelector(sel);

/* Blueprint (vendored CSS, dark theme): the semantic classes used below are mapped to bp5-* classes here, so the
   renderers stay readable. primary/danger -> intents, state/badge -> tags, card -> card, inputs -> bp5-input. */
const INTENT = { primary: 'bp5-intent-primary', danger: 'bp5-intent-danger', ok: 'bp5-intent-success', bad: 'bp5-intent-danger',
                 warn: 'bp5-intent-warning', working: 'bp5-intent-primary', waiting: 'bp5-intent-warning', done: 'bp5-intent-success',
                 errored: 'bp5-intent-danger' };
/* Structural Blueprint widgets (pure CSS): the semantic class maps to its bp5-* class and nothing else is added, whatever the tag.
   callout and progress also take the intent classes (ok, warn, bad, primary, danger). Used by tabs(), menu(), emptyState() in components.js. */
const SEMANTIC = { tabs: 'bp5-tabs', tablist: 'bp5-tab-list', tab: 'bp5-tab', tabpanel: 'bp5-tab-panel', menu: 'bp5-menu', menuitem: 'bp5-menu-item',
                   callout: 'bp5-callout', nonideal: 'bp5-non-ideal-state', progress: 'bp5-progress-bar', meter: 'bp5-progress-meter', navbar: 'bp5-navbar',
                   skeleton: 'bp5-skeleton', table: 'bp5-html-table bp5-compact',
                   /* the file tree (tree.js, hand-written bp5-tree markup): root, list, node, content row, caret, label, secondary label */
                   tree: 'bp5-tree', treelist: 'bp5-tree-node-list', treenode: 'bp5-tree-node', treecontent: 'bp5-tree-node-content',
                   treecaret: 'bp5-tree-node-caret bp5-icon-standard', treelabel: 'bp5-tree-node-label', treesecondary: 'bp5-tree-node-secondary-label' };
function blueprint(n, tag, cls) {
  const list = cls ? cls.split(/\s+/) : [];
  const has = (c) => list.includes(c);
  const sem = list.filter((c) => ownKey(SEMANTIC, c));
  if (sem.length) {
    for (const c of sem) n.classList.add(...SEMANTIC[c].split(/\s+/));   // a value may name several classes (table): classList.add rejects a token with a space
    if (has('callout') || has('progress')) for (const c of list) if (ownKey(INTENT, c)) n.classList.add(INTENT[c]);
    return;
  }
  if (tag === 'button' || (tag === 'a' && has('btn'))) {
    n.classList.add('bp5-button');
    for (const c of list) if (INTENT[c] && (c === 'primary' || c === 'danger')) n.classList.add(INTENT[c]);
    if (has('icon') || has('minimal')) n.classList.add('bp5-minimal');
    if (has('small')) n.classList.add('bp5-small');
  } else if (tag === 'input') {
    const t = n.getAttribute('type') || 'text';
    if (t !== 'checkbox' && t !== 'radio') n.classList.add('bp5-input');
  } else if (tag === 'textarea') {
    n.classList.add('bp5-text-area', 'bp5-fill');
  } else if (has('card')) {
    n.classList.add('bp5-card', 'bp5-elevation-1');
  } else if (has('state') || has('badge')) {
    n.classList.add('bp5-tag', 'bp5-minimal', 'bp5-round');
    for (const c of list) if (INTENT[c] && c !== 'primary' && c !== 'danger') n.classList.add(INTENT[c]);
  }
}
/* Write text only when it changed: the poll patches the shell in place every few seconds and must not touch the DOM for nothing. */
function setText(node, text) { const t = String(text); if (node.textContent !== t) node.textContent = t; }
function ic(name) { return el('span', { class: 'bp5-icon bp5-icon-' + name, 'aria-hidden': 'true' }); }

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  blueprint(n, tag, (attrs && attrs.class) || '');
  const kids = children.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false);
  const isButton = n.classList.contains('bp5-button');
  for (const c of kids) {
    // Blueprint spaces a button's element children (icon + text) only when the text is an element too
    if (typeof c === 'string') n.append(isButton && kids.length > 1 ? el('span', { class: 'bp5-button-text', text: c }) : document.createTextNode(c));
    else n.append(c);
  }
  if (tag === 'label' && n.firstElementChild && n.firstElementChild.type === 'checkbox') {
    n.classList.add('bp5-control', 'bp5-checkbox');
    n.firstElementChild.after(el('span', { class: 'bp5-control-indicator' }));
  }
  return n;
}

/* Glyphs: state is never colour-only. Every state has a fixed-width glyph plus a text label (aria-label and title);
   the agent glyph says which tool a session runs. Unknown keys fall back instead of leaking into class names. */
const STATE_GLYPH = { working: '✽', waiting: '✻', idle: '∙', done: '✓', errored: '✕', ended: '○', unknown: '·' };
const AGENT_GLYPH = { claude: '◆', codex: '◇', shell: '▸' };
const GLYPH_LABEL = { working: 'working', waiting: 'needs you', idle: 'idle', done: 'done', errored: 'error', ended: 'ended', unknown: 'unknown' };

function ownKey(map, key) { return Object.prototype.hasOwnProperty.call(map, key); }

function stateGlyph(state) {
  const st = ownKey(STATE_GLYPH, state) ? state : 'unknown';
  return el('span', { class: 'glyph ' + st, role: 'img', 'aria-label': GLYPH_LABEL[st], title: GLYPH_LABEL[st], text: STATE_GLYPH[st] });
}

function agentGlyph(agent) {
  const glyph = ownKey(AGENT_GLYPH, agent) ? AGENT_GLYPH[agent] : AGENT_GLYPH.shell;
  const label = agent || 'shell';
  return el('span', { class: 'glyph agent', role: 'img', 'aria-label': label, title: label, text: glyph });
}

/* svg(): el() for SVG nodes (namespaced create, setAttribute for every attribute, 'class' included). The tofu fallback
   for the glyphs above and the base of the hand-rolled charts; nothing calls it yet. */
const SVG_NS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs, ...children) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    n.append(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return n;
}

/* Demo mode (?demo=1 or localStorage ccboard:demo=1): GETs read fixtures under /static/demo, writes resolve {ok:true}. The fixtures are
   excluded from the service-worker shell and the asset version. The try/catch keeps this definition-only: a missing location or
   storage reads as "not demo". */
let demoFlag = null;
function demoOn() {
  if (demoFlag === null) { try { demoFlag = /[?&]demo=1/.test(location.search) || localStorage.getItem('ccboard:demo') === '1'; } catch (_) { demoFlag = false; } }
  return demoFlag;
}
/* The fixtures were captured at demo.epoch: shift every timestamp by the elapsed time so ages, countdowns and the 'older' cut stay live. */
const ISO_TS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;
const EPOCH_KEYS = new Set(['created', 'resets_at', 'created_at_epoch', 'expires_at']);
function demoRebase(data) {
  const epoch = data && data.demo && data.demo.epoch;
  if (!epoch) return data;
  const dt = Math.floor(Date.now() / 1000 - (typeof epoch === 'number' ? epoch : Date.parse(epoch) / 1000));
  const walk = (v, key) => {
    if (Array.isArray(v)) return v.map((x) => walk(x, key));
    if (v && typeof v === 'object') { const o = {}; for (const [k, x] of Object.entries(v)) o[k] = k === 'demo' ? x : walk(x, k); return o; }
    if (typeof v === 'number' && EPOCH_KEYS.has(key) && v > 1e9) return v + dt;
    if (typeof v === 'string' && ISO_TS.test(v)) { const t = Date.parse(v); if (!Number.isNaN(t)) return new Date(t + dt * 1000).toISOString(); }
    return v;
  };
  return walk(data, '');
}

/* A demo answer that is an HTTP error: the tree and file previews read err.status (the real endpoints answer 403 / 404 / 415 the same way). */
function demoError(status, message) {
  const e = new Error(message || `${status}`);
  e.status = status;
  return e;
}

/* Demo tree and file fixtures are maps keyed '<repo>|<path>' ('root' is the project folder, path '' a repo's top level); the query's
   hidden / ignored / repos flags are ignored. tree.json holds the /tree payloads; file.json holds {status: 200, body} or
   {status: 403|415, error, reveal?: {status: 200, body}} per file. An absent key answers 404. */
function demoPick(kind, bare, query, data) {
  const m = /^\/api\/projects\/[^/]+\/repos\/([^/]+)\//.exec(bare);
  const q = new URLSearchParams(query || '');
  const key = decodeURIComponent(m ? m[1] : '') + '|' + (q.get('path') || '');
  const hit = data && ownKey(data, key) ? data[key] : null;
  if (!hit) throw demoError(404, 'not found');
  if (kind === 'tree') return hit;
  const r = hit.status === 403 && q.get('reveal') === '1' && hit.reveal ? hit.reveal : hit;
  if (r.status === 200) return r.body;
  throw demoError(r.status, r.error);
}

async function demoApi(method, path) {
  if (method !== 'GET') { await new Promise((resolve) => setTimeout(resolve, 150)); return { ok: true }; }
  const bare = path.split('?')[0];
  let name = null;
  if (bare === '/api/state') name = 'state';
  else if (bare.startsWith('/api/search')) name = 'search';
  else if (/^\/api\/projects\/[^/]+\/repos\/[^/]+\/tree$/.test(bare)) name = 'tree';
  else if (/^\/api\/projects\/[^/]+\/repos\/[^/]+\/file$/.test(bare)) name = 'file';
  else if (bare.startsWith('/api/series/events')) name = 'series_events';   // before the '/api/series' prefix: the Gantt must not draw the limit series as sessions
  else if (bare.startsWith('/api/series')) name = 'series';
  else if (bare === '/api/usage/summary') name = 'usage_summary';
  else if (bare.startsWith('/api/memory/')) name = 'memory';
  if (!name) return {};
  const r = await fetch(`/static/demo/${name}.json`);
  if (!r.ok) throw new Error(`demo fixture ${name}.json: ${r.status} ${r.statusText}`);
  const data = await r.json();
  if (name === 'tree' || name === 'file') return demoPick(name, bare, path.slice(bare.length + 1), data);
  if (name === 'series_events' && data && data.demo && data.demo.epoch && Array.isArray(data.events)) {   // keep the fixture's 24 h alive, like state.json
    const dt = Math.floor(Date.now() / 1000 - data.demo.epoch);
    return { ...data, events: data.events.map((e) => ({ ...e, t: e.t + dt })) };
  }
  return name === 'state' ? demoRebase(data) : data;
}

async function api(method, path, body) {
  if (demoOn()) return demoApi(method, path);
  const headers = { 'X-CCBoard': '1' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await r.json(); } catch (_) { /* not json */ }
  if (!r.ok) {
    const err = new Error((data && data.error) || `${r.status} ${r.statusText}`);
    err.status = r.status;                       // callers branch on these, not on the message text (components.js taskIsMismatch)
    err.body = data;
    throw err;
  }
  return data;
}

const ui = { openForm: null, confirm: null, error: null, notice: null, modal: false, lastJson: null, inboxSel: -1, notifyPanel: false, deepLinked: false };
let state = null;
let pollTimer = null;

/* Client-side optimism that outlives one render (v0.5.14a): store.tasksOverride[taskId] is a state.tasks row painted over the poll's own
   until the poll agrees (components.js boardTasks merges and clears it): a Start, a card added to the backlog, an edit or a delete shows
   at once instead of after the next poll. Memory only; nothing here survives a reload. */
const store = { tasksOverride: {} };

function setError(msg) { ui.error = msg; renderBanner(); }

function codeServerUrl(path) {
  return `https://${location.hostname}:${state.config.code_https_port}/?folder=${encodeURIComponent(path)}`;
}

/* The authority of the vscode-remote:// file URI in codeServerFileUrl: '' gives vscode-remote:///<abs> (the contract); the box check
   (plan v0.5.6, "Verify on box") may freeze it as the host or host:port. The one place to change it. */
let CODE_SERVER_AUTHORITY = '';

/* Open one file in code-server, at a line (and column) when given: the same folder link as codeServerUrl plus the openFile payload
   ([["openFile", "vscode-remote://<authority><abs>:<line>:<col>"]], url-encoded). folder defaults to the file's own directory;
   the preview passes the repo's absolute path. The only builder of this URL. */
function codeServerFileUrl(abs, line, folder, col) {
  const where = typeof line === 'number' && line > 0 ? `:${Math.floor(line)}${typeof col === 'number' && col > 0 ? ':' + Math.floor(col) : ''}` : '';
  const dir = folder || String(abs).replace(/\/[^/]*$/, '') || '/';
  const payload = JSON.stringify([['openFile', `vscode-remote://${CODE_SERVER_AUTHORITY}${abs}${where}`]]);
  return `https://${location.hostname}:${state.config.code_https_port}/?folder=${encodeURIComponent(dir)}&payload=${encodeURIComponent(payload)}`;
}

function fmtAge(epoch) {
  if (!epoch) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}


function fmtIn(epochSeconds) {
  if (!epochSeconds) return '';
  const s = Math.floor(epochSeconds - Date.now() / 1000);
  if (s <= 0) return 'now';
  if (s < 3600) return `${Math.ceil(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d${Math.floor((s % 86400) / 3600)}h`;
}

function fmtTs(s) { return s ? s.replace('T', ' ').slice(0, 16) : ''; }

const LAST_KEY = 'ccboard:last-state';

function rememberState(json) { try { localStorage.setItem(LAST_KEY, JSON.stringify({ at: Date.now(), state: json })); } catch (_) { /* storage may be unavailable */ } }
function recallState() { try { const v = JSON.parse(localStorage.getItem(LAST_KEY) || 'null'); return v && v.state ? v : null; } catch (_) { return null; } }

function registerServiceWorker() {
if ('serviceWorker' in navigator) {
  const hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => { /* no SW: the board still works */ });
  let reloaded = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    // a new worker took over after a deploy: load the new shell once (never on the very first install, and not
    // again right after a version-change reload)
    let justReloaded = false;
    try { justReloaded = sessionStorage.getItem('ccboard:reloaded') === '1'; sessionStorage.removeItem('ccboard:reloaded'); } catch (_) { /* ignore */ }
    if (hadController && !reloaded && !justReloaded) { reloaded = true; location.reload(); }
  });
}
}

function b64ToBytes(s) {
  const pad = '='.repeat((4 - s.length % 4) % 4);
  const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, ch => ch.charCodeAt(0));
}

async function pushSubscription() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

async function enablePush() {
  if (!('PushManager' in window)) { setError('Web Push is not available in this browser (on iOS, add the board to the Home Screen first).'); return; }
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') { setError('Notifications were not allowed.'); return; }
  const { key } = await api('GET', '/api/push/vapid');
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
  await api('POST', '/api/push/subscribe', { subscription: sub.toJSON() });
  setError(null); renderNotifyPanel();
}

async function disablePush() {
  const sub = await pushSubscription();
  if (sub) { await api('DELETE', '/api/push/subscribe', { subscription: sub.toJSON() }); await sub.unsubscribe(); }
  renderNotifyPanel();
}

function loadPrefs(key) { try { return JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (_) { return {}; } }
function savePrefs(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (_) { /* storage may be unavailable */ } }

/* Lazy vendored assets (diff2html today, uPlot later): one <link>/<script> injection per file, memoised. */
const assetLoading = {};
function loadAsset(path) {
  if (assetLoading[path]) return assetLoading[path];
  assetLoading[path] = new Promise((resolve, reject) => {
    if (path.endsWith('.css')) { const css = el('link', { rel: 'stylesheet', href: path }); css.onload = () => resolve(); css.onerror = () => reject(new Error('could not load ' + path)); document.head.append(css); return; }
    const s = el('script', { src: path }); s.onload = () => resolve(); s.onerror = () => reject(new Error('could not load ' + path)); document.head.append(s);
  });
  return assetLoading[path];
}
function loadDiff2Html() {
  if (window.Diff2HtmlUI) return Promise.resolve();
  return Promise.all([loadAsset('/static/vendor/diff2html.min.css'), loadAsset('/static/vendor/diff2html-ui-base.min.js')]).then(() => undefined);
}

/* Installed PWA (iOS/Android "standalone"): a target=_blank link would open Safari and leave the app, so terminal
   pages navigate in place; the terminal's "‹ board" link comes back. Other origins (code-server, GitHub) still open
   outside, as they must. */
function isStandalone() { return window.navigator.standalone === true || (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches); }
function openPage(url) {
  if (isStandalone() && url.startsWith('/')) location.assign(url);
  else window.open(url, '_blank', 'noopener');
}

function installLifecycleListeners() {
document.addEventListener('click', (e) => {
  const a = e.target.closest && e.target.closest('a[target=_blank]');
  if (!a || !isStandalone()) return;
  const href = a.getAttribute('href') || '';
  if (href.startsWith('/term/') || href.startsWith('/tty/')) { e.preventDefault(); location.assign(href); }
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshNow(); });
window.addEventListener('pageshow', (e) => { if (e.persisted) refreshNow(); });
window.addEventListener('focus', () => refreshNow());
window.addEventListener('online', () => refreshNow());
}

function refreshNow() {
  if (Date.now() - (ui.lastPollAt || 0) < 500) return;   // several lifecycle events fire together
  clearTimeout(pollTimer);
  if ('serviceWorker' in navigator) navigator.serviceWorker.getRegistration().then(r => r && r.update()).catch(() => { /* ignore */ });
  poll(true);
}

async function poll(force) {
  ui.lastPollAt = Date.now();
  try {
    const s = await api('GET', '/api/state');
    if (s.version && ui.version && s.version !== ui.version) {
      // the box was updated while this page stayed open (an installed PWA restored from memory never navigates)
      try { sessionStorage.setItem('ccboard:reloaded', '1'); } catch (_) { /* ignore */ }
      location.reload();
      return;
    }
    if (s.version) ui.version = s.version;
    const j = JSON.stringify(s);
    const changed = j !== ui.lastJson;
    ui.lastJson = j;
    state = s;
    ui.offline = false;
    if (changed || force) render(force);
    else { renderHeader(); renderUsage(); updateModal(); }
    rememberState(s);
  } catch (e) {
    if (!state) {
      const last = recallState();
      if (last) { state = last.state; ui.offline = last.at; render(true); }
      else { $('#banner').textContent = 'Cannot reach ccboard: ' + e.message; }
    } else { ui.offline = ui.offline || Date.now(); renderBanner(); }
  }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => poll(false), ui.modal ? 2000 : 3000);
}

function startStatePolling() { return poll(true); }
