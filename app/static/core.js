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

/* The scroll behaviour for scrollIntoView / scrollTo: 'smooth' unless the person asked for reduced motion (prefers-reduced-motion: reduce), then 'auto' (an instant jump). */
function scrollBehavior() {
  try { return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'; } catch (_) { return 'auto'; }
}

function ownKey(map, key) { return Object.prototype.hasOwnProperty.call(map, key); }

/* Team (issue #200): the few bytes the sidebar, Home and the palette need to ask for the Team view. Everything else is pages/team.js, the lazy 'teamview' bundle, which loads only while the
   board says some repository is shared (state.team.shared, app/main.py). With nothing shared nothing here loads a script, asks the network or changes a screen. */
let teamViewBad = 0;
function teamShared(st) { const s = st || (typeof state !== 'undefined' ? state : null); return !!s && !!s.team && s.team.shared === true; }
/* All optional component mounts share the bundle's script AND CSS barrier.
   Eager/terminal contexts have no loader; callers guard their available exports.
   isCurrent prevents a late load from mounting into an abandoned route. */
function mountBundle(name, mount, isCurrent = () => true, failed = null) {
  const apply = () => { if (isCurrent()) return mount(); };
  if (typeof Lazy === 'undefined' || Lazy.done(name)) return apply();
  return Lazy.load(name).then(apply).catch(err => {
    if (!isCurrent()) return;
    if (failed) failed(err); else Lazy.fail(name, err, () => mountBundle(name, mount, isCurrent, failed));
  });
}

function teamUse(fn) {
  if (!teamShared()) return false;
  if (!(teamViewBad && Date.now() - teamViewBad < 60000)) mountBundle('teamview', () => { if (typeof TeamView !== 'undefined' && TeamView.ready) fn(); }, () => teamShared(), () => { teamViewBad = Date.now(); });
  return true;
}
function teamSync(st) { if (teamShared(st)) teamUse(() => TeamView.start()); else if (typeof TeamView !== 'undefined' && TeamView.ready) TeamView.stop(); }

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
/* Demo mode (?demo=1, or localStorage ccboard:demo = 1) is the board's QA harness: demoApi() below answers GETs from app/static/demo/*.json and
   every write with {ok:true}, so each screen renders with realistic data and no tmux. Decision v0.5.21: kept (scripts/qa-ui.sh and the
   screenshot rule depend on it). docs/development.md says what is faked and how the fixtures are refreshed. */
let demoFlag = null;
function demoOn() {
  if (demoFlag === null) { try { demoFlag = /[?&]demo=1/.test(location.search) || localStorage.getItem('ccboard:demo') === '1'; } catch (_) { demoFlag = false; } }
  return demoFlag;
}
async function api(method, path, body) {
  if (demoOn()) { if (typeof demoApi !== 'function') await (typeof Lazy !== 'undefined' ? Lazy.load('demo') : loadAsset('/static/demo-api.js')); return demoApi(method, path, body); }
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

/* An error from api() or a bare fetch, in words for a panel (#309). The board's own sentence (body {detail|error}) is kept as written; a failure with no
   such sentence says what happened instead of the transport text: no response at all (TypeError 'Failed to fetch', Safari's 'Load failed', Firefox's
   'NetworkError ...') is 'the board did not answer', an HTTP status without a board sentence ('503 Service Unavailable' from a proxy) names the status.
   Any other error (one a caller threw itself) keeps its message. api()'s own throw contract is unchanged. */
function errorText(e) {
  const b = e && e.body && typeof e.body === 'object' ? e.body : null;
  const said = b && [b.detail, b.error].find((x) => typeof x === 'string' && x.trim());
  if (said) return said;
  const msg = String((e && e.message) || '').trim();
  if (e && e.status && (!msg || /^\d{3}(\s|$)/.test(msg))) return `the board answered with an error (HTTP ${e.status})`;     // api()'s own fallback message is '<status> <statusText>'
  if (!e || e instanceof TypeError || /failed to fetch|load failed|networkerror|network request failed/i.test(msg)) return 'the board did not answer';
  return msg || 'the board did not answer';
}

const ui = { openForm: null, confirm: null, error: null, notice: null, lastJson: null, inboxSel: -1, notifyPanel: false, deepLinked: false };
let state = null;
let pollTimer = null;

/* Client-side optimism that outlives one render (v0.5.14a): store.tasksOverride[taskId] is a state.tasks row painted over the poll's own
   until the poll agrees (components.js boardTasks merges and clears it): a Start, a card added to the backlog, an edit or a delete shows
   at once instead of after the next poll. Memory only; nothing here survives a reload. */
const store = { tasksOverride: {} };

function setError(msg) { ui.error = msg; renderBanner(); }

/* Auto-continue, per session (#84). The board types `continue` once into a session parked on a limit after its window resets (and after an account switch), and
   into one that was working before a reboot once its relaunch is back at the prompt. flags.no_autoresume on the session row opts one session out of all three;
   CCBOARD_AUTO_CONTINUE=0 turns the first and the last off for every session (state.config.auto_continue says which). One writer for every surface (the row
   menu, the Quad tile menu, Tune): POST /api/sessions/<tmux>/flags {no_autoresume}. The switch is optimistic: `o.apply(off)` paints the new value at once, the
   answer's own read-back of the row settles it, a refusal puts the old value back with the server's words in a toast. */
const AUTO_CONTINUE_WHAT = 'Types continue once after a limit reset or an account switch, and after a reboot if the session was working. This session only.';
const AUTO_CONTINUE_SUB = 'after a limit reset or a reboot, this session only';
const AUTO_CONTINUE_BOARD_OFF = 'Off for the whole board (CCBOARD_AUTO_CONTINUE=0), so this switch changes nothing until the board setting is on again.';
const autoContinueBusy = new Set();
function sessionAutoContinueOff(s) { return !!(s && s.flags && typeof s.flags === 'object' && s.flags.no_autoresume); }
function boardAutoContinueOff(st) {
  const c = (st || (typeof state !== 'undefined' ? state : null) || {}).config;
  return !!c && c.auto_continue === false;
}
function autoContinueLabel(off) { return 'Auto-continue: ' + (off ? 'off' : 'on'); }
/* '22:05', with the weekday when it is a day away. */
function clockAt(epoch, calendar = false) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return (calendar ? d.toDateString() === new Date().toDateString() : Math.abs(epoch - Date.now() / 1000) < 20 * 3600) ? hm : `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()]} ${hm}`;
}
/* #71: a session parked on a rate limit (s.parked {kind, resets_at}, from the server) says what happens next, not a bare "error". */
function limitParkedText(s) {
  const p = s && s.parked;
  if (!p) return '';
  if (!p.resets_at || sessionAutoContinueOff(s) || boardAutoContinueOff()) return 'limit reached, auto-continue is off';
  return p.resets_at * 1000 > Date.now() ? `limit reached, continues at ${clockAt(p.resets_at)}` : 'limit reset, continuing shortly';
}
/* resolves true when the row now shows the wanted value; false when nothing changed (a second tap while one is in flight, or a refusal) */
async function setAutoContinue(tmux, off, o) {
  const x = o || {};
  if (!tmux || autoContinueBusy.has(tmux)) return false;
  autoContinueBusy.add(tmux);
  const apply = typeof x.apply === 'function' ? x.apply : () => {};
  apply(!!off);
  try {
    const res = await api('POST', `/api/sessions/${encodeURIComponent(tmux)}/flags`, { no_autoresume: !!off });
    const f = res && res.flags && typeof res.flags === 'object' ? res.flags : null;        // no flags in the answer (the demo board): the optimistic value stands
    const got = f && typeof f.no_autoresume === 'boolean' ? f.no_autoresume : !!off;
    if (got !== !!off) { apply(got); return false; }
    try { if (typeof poll === 'function') poll(true); } catch (_) { /* no board poll on this page */ }
    return true;
  } catch (e) {
    apply(!off);
    const why = (e && e.message) || 'refused';
    if (typeof toast === 'function') toast('Auto-continue not changed: ' + why, { kind: 'bad' });
    return false;
  } finally {
    autoContinueBusy.delete(tmux);
  }
}

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

/* ---------- the windows of a subscription at a given moment (v0.5.17f) ----------
   Every surface that shows a 5-hour or 7-day window (the topbar pills, the Usage gauges and account rows, Settings > Accounts, the Home card) goes through
   limitWindowNow, so they all say the same thing, and the server's accounts.window_now / headroom is its twin (tests/fixtures/window_now.json drives both):
     a reading whose reset time is still ahead keeps its percentage and counts down to it;
     a reading whose reset time has passed with no newer one is a window that rolled over, and nothing has been counted in the new one yet: 0 % used and the
     NEXT reset, resets_at + n x period for the first n that puts it past now (the windows nobody read in between are assumed to follow each other).
   The 5-hour window really starts with the first message after the last one ran out, so the next 5H reset of a quiet account is a projection: its title says so
   ('no usage recorded since the window reset'). */
const LIMIT_PERIOD = { '5h': 18000, '7d': 604800 };       // seconds

/* An epoch in seconds from a number (seconds, or milliseconds above 1e11) or an ISO text; 0 for anything else. */
function limitEpoch(v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v > 1e11 ? v / 1000 : v;
  if (typeof v === 'string' && v) { const t = Date.parse(v); return Number.isNaN(t) ? 0 : t / 1000; }
  return 0;
}

/* One window of a reading {pct, resets_at (epoch s), at (epoch s), source} at `now` (epoch s, default the clock), with `period` seconds between its resets, as
   {pct: what to show (0 once rolled over), last: the reading's own percentage, resets_at: the reset to count down to (the next one once rolled; 0 unknown),
   rolled, missed: whole windows skipped, at, source: 'cache' | 'statusline'}; null without a numeric reading. */
function limitWindowNow(reading, period, now) {
  if (!reading || typeof reading.pct !== 'number' || !Number.isFinite(reading.pct)) return null;
  const t = typeof now === 'number' ? now : Date.now() / 1000;
  const r = typeof reading.resets_at === 'number' && reading.resets_at > 0 ? reading.resets_at : 0;
  const last = Math.max(0, Math.min(100, reading.pct));
  const rolled = r > 0 && r <= t;
  const missed = rolled && period > 0 ? Math.floor((t - r) / period) : 0;
  return { pct: rolled ? 0 : last, last, resets_at: !r ? 0 : !rolled ? r : (period > 0 ? r + (missed + 1) * period : 0), rolled, missed,
    at: limitEpoch(reading.at), source: reading.source === 'cache' ? 'cache' : 'statusline' };
}

/* 'updated 5m ago · from the last session' (a statusline reading), 'updated 5m ago · from Claude Code's cache' (the cache source), 'no reading yet' without a time. */
function limitFreshness(at, source) {
  const t = limitEpoch(at);
  if (!t) return 'no reading yet';
  return `updated ${fmtAge(t)} ago · from ${source === 'cache' ? "Claude Code's cache" : 'the last session'}`;
}

/* The caption of a window (a limitWindowNow answer) for a title: its freshness, led by 'no usage recorded since the window reset' once it rolled over. '' for a
   window that carries no reading time and has not rolled. */
function limitCaption(w) {
  if (!w) return '';
  const fresh = w.at ? limitFreshness(w.at, w.source) : '';
  return w.rolled ? ['no usage recorded since the window reset', fresh].filter(Boolean).join(' · ') : fresh;
}

/* One Claude window ('5h' | '7d') of state.usage, the record the topbar pills read (a session's statusline, or Claude Code's cache): a limitWindowNow answer, null when
   the record has no such window. The reading's time is the window's own `at` (a window the server filled in from an older reading), else the record's. */
function limitUsageWindow(st, win, now) {
  const u = st && st.usage;
  const rl = (u && u.value) || {};
  const w = win === '5h' ? rl.five_hour : rl.seven_day;
  if (!w || typeof w.used_percentage !== 'number' || !Number.isFinite(w.used_percentage)) return null;
  return limitWindowNow({ pct: w.used_percentage, resets_at: w.resets_at, at: w.at || (u && u.at), source: w.source || rl.source }, LIMIT_PERIOD[win], now);
}


function fmtTs(s) { return s ? s.replace('T', ' ').slice(0, 16) : ''; }

const LAST_KEY = 'ccboard:last-state';

/* The offline snapshot (#46): the poll used to write the whole state to localStorage on every answer. Now it is written at most once per
   SNAPSHOT_EVERY ms, only when the state changed since the last write (a change is kept and saved at the next allowed time), and never
   above SNAPSHOT_MAX characters of JSON (one console warning; the older snapshot stays). `json` is the text poll() already made for its
   change check, so nothing is stringified twice. A full or blocked storage never breaks the poll. Returns true when it wrote. */
const SNAPSHOT_EVERY = 30000;
const SNAPSHOT_MAX = 200 * 1024;
const offlineSnap = { at: 0, dirty: false, warned: false };
function rememberState(json, changed) {
  if (changed) offlineSnap.dirty = true;
  const now = Date.now();
  if (!offlineSnap.dirty || (offlineSnap.at && now - offlineSnap.at < SNAPSHOT_EVERY)) return false;
  if (typeof json !== 'string' || json.length > SNAPSHOT_MAX) {
    if (!offlineSnap.warned) { offlineSnap.warned = true; try { console.warn(`ccboard: the state is ${json && json.length} characters, over the offline snapshot's ${SNAPSHOT_MAX}; not saved`); } catch (_) { /* ignore */ } }
    return false;
  }
  offlineSnap.at = now;                                     // a failed write waits its turn too: no retry on every poll
  try { localStorage.setItem(LAST_KEY, '{"at":' + now + ',"state":' + json + '}'); offlineSnap.dirty = false; return true; } catch (_) { return false; }
}
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
  setError(null); if (typeof renderNotifyPanel === 'function') renderNotifyPanel();      // settings.js (a lazy bundle) defines it; only its buttons call this
}

async function disablePush() {
  const sub = await pushSubscription();
  if (sub) { await api('DELETE', '/api/push/subscribe', { subscription: sub.toJSON() }); await sub.unsubscribe(); }
  if (typeof renderNotifyPanel === 'function') renderNotifyPanel();
}

function loadPrefs(key) { try { return JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (_) { return {}; } }
function savePrefs(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (_) { /* storage may be unavailable */ } }

/* Lazy vendored assets (diff2html today, uPlot later): one <link>/<script> injection per file, memoised. */
const assetLoading = {};
function loadAsset(path) {
  const url = typeof Lazy !== 'undefined' ? Lazy.url(path) : path;
  if (assetLoading[path]) return assetLoading[path];
  assetLoading[path] = new Promise((resolve, reject) => {
    if (path.endsWith('.css')) { const css = el('link', { rel: 'stylesheet', href: url }); css.onload = () => resolve(); css.onerror = () => reject(new Error('could not load ' + path)); document.head.append(css); return; }
    const s = el('script', { src: url }); s.defer = true; s.async = false; s.onload = () => resolve(); s.onerror = () => reject(new Error('could not load ' + path)); document.head.append(s);
  });
  assetLoading[path].catch(() => { delete assetLoading[path]; });
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
  if (a.getAttribute('data-standalone') === 'skip') return;      // a quad tile's Open: leave the click to the browser so the other tiles' iframes stay
  const href = a.getAttribute('href') || '';
  if (href.startsWith('/term/') || href.startsWith('/tty/')) { e.preventDefault(); location.assign(href); }
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshNow(); });
window.addEventListener('pageshow', (e) => { if (e.persisted) refreshNow(); });
window.addEventListener('focus', () => refreshNow());
window.addEventListener('online', () => refreshNow());
}

/* The poll cadence (#31): every 3 s while the tab is visible, every 15 s while it is hidden (the tab title and badge may lag that much; the
   server's own notifications do not). Becoming visible, focus, pageshow and online poll at once through refreshNow(). A 5-minute idle
   stretch for a visible tab was considered and dropped: on the box a scan costs about 0.1 s and the server already backs off when busy. */
const POLL_VISIBLE = 3000;
const POLL_HIDDEN = 15000;
function pollDelay() { return document.hidden ? POLL_HIDDEN : POLL_VISIBLE; }
function schedulePoll() {
  clearTimeout(pollTimer);
  ui.pollDelayArmed = pollDelay();
  pollTimer = setTimeout(() => poll(false), ui.pollDelayArmed);
}

function refreshNow() {
  if (Date.now() - (ui.lastPollAt || 0) < 500) {          // several lifecycle events fire together: poll once
    if ((ui.pollDelayArmed || 0) > pollDelay()) schedulePoll();   // but a hidden-tab timer armed just now must not hold a visible tab for 15 s
    return;
  }
  clearTimeout(pollTimer);
  if ('serviceWorker' in navigator) navigator.serviceWorker.getRegistration().then(r => r && r.update()).catch(() => { /* ignore */ });
  poll(true);
}

async function poll(force) {
  ui.lastPollAt = Date.now();
  const seq = ui.pollSeq = (ui.pollSeq || 0) + 1;         // two polls in flight: the older answer never paints over the newer one
  try {
    const s = await api('GET', '/api/state');
    if (seq < (ui.pollApplied || 0)) return;
    ui.pollApplied = seq;
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
    if (typeof accountOverlay === 'function') accountOverlay(s);       // an account switch still running, the demo's make-believe login (pages/agents.js)
    ui.offline = false;
    let survived = typeof sessionClosingPrune === 'function' && sessionClosingPrune(s);
    if (typeof pendingApply === 'function' && pendingApply(s)) survived = true;      // #313: Allow / Deny / Ack still in flight
    if (changed || force || survived) render(force || survived);
    else { renderHeader(); renderUsage(); }
    rememberState(j, changed);
  } catch (e) {
    if (seq < (ui.pollApplied || 0)) return;               // a newer poll already answered
    if (!state) {
      const last = recallState();
      if (last) { state = last.state; ui.offline = last.at; render(true); }
      else { $('#banner').textContent = 'Cannot reach ccboard: ' + e.message; }
    } else { ui.offline = ui.offline || Date.now(); renderBanner(); }
  }
  if (seq === ui.pollSeq) schedulePoll();                  // only the newest poll arms the next one
}

function startStatePolling() { return poll(true); }

function linkedBadge(item) {
  return item && item.linked ? el('span', { class: 'badge hue-slate', title: item.target || '', text: 'Linked' }) : null;
}
function patchLinkedName(node, name, item) {
  node.textContent = name;
  if (item && item.linked) node.append(' ', linkedBadge(item));
}
