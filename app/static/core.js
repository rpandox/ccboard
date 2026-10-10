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
   screenshot rule depend on it). README, "Demo mode" says what is faked and how the fixtures are refreshed. */
let demoFlag = null;
function demoOn() {
  if (demoFlag === null) { try { demoFlag = /[?&]demo=1/.test(location.search) || localStorage.getItem('ccboard:demo') === '1'; } catch (_) { demoFlag = false; } }
  return demoFlag;
}
/* The fixtures were captured at demo.epoch: shift every timestamp by the elapsed time so ages, countdowns and the 'older' cut stay live. */
const ISO_TS = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/;
const EPOCH_KEYS = new Set(['created', 'resets_at', 'created_at_epoch', 'expires_at', 'resets_5h', 'resets_7d']);   // resets_5h / resets_7d: state.accounts.list[] (v0.5.17b)
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

/* The demo's tailnet list (issue #134): the fixture's ISO times (the answer's at, last_seen, a row's at) follow the clock like state.json's (demoRebase), and a Refresh
   (?refresh=1) is answered as if every probe had just run, so the demo shows a stale row at first and a fresh one after the tap. */
function demoDiscover(data0, refresh) {
  const data = demoRebase(data0);
  if (!refresh || !data || typeof data !== 'object' || !Array.isArray(data.rows)) return data;
  const iso = new Date().toISOString();
  return { ...data, at: iso, rows: data.rows.map((r) => (r && r.at ? { ...r, at: iso, age: 0, stale: false } : r)) };
}

/* The demo's pairing (issue #135, Settings > Nodes): demo/nodes.json holds the three lists (outgoing pairs, incoming pairs, activity) and, because a POST never reaches a box in demo
   mode, a code, a pair, a rotation and a removal are played here for the life of the page: the code is a made-up one, a new pair is listed as waiting for its first contact, a
   removed one disappears (one of them answers "the other node was not told") and each leaves a row in Activity. Nothing of this is a credential. */
const demoNodesMade = { gone: new Set(), added: [], audit: [], seq: 0 };
const DEMO_PAIR_CODE = 'K7Q2M-4XD9R';
function demoNodesNote(action, who, ok, detail, direction) {
  demoNodesMade.audit.unshift({ id: `demo-w${++demoNodesMade.seq}`, at: new Date().toISOString(), direction: direction || 'out', peer: '', node_name: who || '', action, status: ok === false ? 'failed' : 'ok', detail: detail || '' });
}
function demoNodesWrite(method, path, body) {
  const b = body && typeof body === 'object' ? body : {};
  let m = null;
  if (method === 'POST' && path === '/api/nodes/pair-code') {
    const mins = Number.isFinite(b.minutes) ? Math.min(30, Math.max(1, b.minutes)) : 10;
    demoNodesNote('code_created', '', true, `scopes ${(Array.isArray(b.scopes) ? b.scopes : []).join(', ')}`, 'in');
    return { code: DEMO_PAIR_CODE, expires_at: new Date(Date.now() + mins * 60000).toISOString(), scopes: Array.isArray(b.scopes) ? b.scopes : ['read', 'tasks'] };
  }
  if (method === 'DELETE' && path === '/api/nodes/pair-code') { demoNodesNote('code_cancelled', '', true, '', 'in'); return { cancelled: true }; }
  if (method === 'POST' && path === '/api/nodes') {
    const host = String(b.url || '').replace(/^https?:\/\//, '').split(/[.:/]/)[0] || 'node';
    const handle = String(b.handle || host).toLowerCase().slice(0, 31);
    const row = { peer_id: `demo-${handle}`, handle, node_id: `ts:nDEMO-${handle}`, name: handle, url: String(b.url || ''), scopes: ['read', 'tasks'], created_at: new Date().toISOString(), last_seen: null, direction: 'out' };
    demoNodesMade.added.push(row);
    demoNodesNote('paired', handle, true, b.both_ways ? 'both ways' : '');
    return row;
  }
  if (method === 'POST' && (m = /^\/api\/nodes\/([^/]+)\/rotate$/.exec(path))) { demoNodesNote('rotated', decodeURIComponent(m[1]), true, 'the old token works 60 s more there'); return { rotated: true, grace_s: 60 }; }
  if (method === 'DELETE' && (m = /^\/api\/nodes\/([^/]+)$/.exec(path))) {
    const id = decodeURIComponent(m[1]);
    demoNodesMade.gone.add(id);
    demoNodesNote('unpair', id, true, '');
    return { removed: true, peer_notified: id !== 'p-old-laptop' };
  }
  return null;
}
/* The three GETs of demo/nodes.json, shaped like the board's ({nodes, pairs, at}, {pairs, at}, {rows, at}): the fixture's rows (times follow the clock like state.json's), minus the ones removed here, plus the ones paired here, plus the activity made here. */
function demoNodes(data0, bare) {
  const data = demoRebase(data0);
  const gone = (r) => demoNodesMade.gone.has(r.peer_id);
  const at = new Date().toISOString();
  const pairs = (data.pairs || []).filter((r) => !gone(r));
  if (bare === '/api/nodes/pairs') return { pairs, at };
  if (bare === '/api/nodes/audit') return { rows: [...demoNodesMade.audit, ...(data.audit || [])], at };
  return { nodes: [...(data.nodes || []).filter((r) => !gone(r)), ...demoNodesMade.added.filter((r) => !gone(r))], pairs, at };
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

/* The demo's make-believe usage refresh (v0.5.17f, part 2): in demo mode a POST never reaches a box, so the Usage page's Refresh is played here. A tap is answered
   at once; the reading the state carries holds still for 3 s ('refreshing…') and then moves to that moment, from Claude Code's cache. A query flag picks the state:
     ?refresh=none   no Claude session is live (the button reads 'start a session to refresh'; a POST would be refused with a 409)
     ?refresh=stale  the newest reading is 10 minutes old (opening the Usage page asks once by itself)
     ?refresh=stuck  a tap is answered but the reading never moves (the wait ends with 'no new reading yet')
   Only demoApi calls these, so a real board never sees them. */
const demoRefresh = { tap: 0, base: 0, shown: 0 };       // tap: when the make-believe ask was made (epoch s), base: the reading's time then, shown: the last one handed out
const DEMO_REFRESH_HOLD = 3;                              // seconds the reading holds still after a tap
function demoRefreshFlag() { try { const m = /[?&]refresh=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }
function demoUsageRefresh(st) {
  if (!st || !st.usage || typeof st.usage !== 'object') return st;
  const flag = demoRefreshFlag();
  const now = Date.now() / 1000;
  let at = flag === 'stale' ? now - 600 : Date.parse(st.usage.at) / 1000;
  let cache = false;
  if (demoRefresh.tap) {
    if (flag === 'stuck' || now - demoRefresh.tap < DEMO_REFRESH_HOLD) at = demoRefresh.base;
    else { at = demoRefresh.tap + DEMO_REFRESH_HOLD; cache = true; }
  }
  if (!Number.isFinite(at)) return st;
  demoRefresh.shown = at;
  const usage = { ...st.usage, at: new Date(at * 1000).toISOString(), value: { ...(st.usage.value || {}), ...(cache ? { source: 'cache' } : {}) } };
  if (flag !== 'none') return { ...st, usage };
  const notClaude = (list) => (Array.isArray(list) ? list.filter((x) => !x || x.agent !== 'claude') : list);
  const projects = (st.projects || []).map((p) => ({ ...p, root: p.root ? { ...p.root, sessions: notClaude(p.root.sessions) } : p.root,
    repos: (p.repos || []).map((r) => ({ ...r, sessions: notClaude(r.sessions) })), orphan_sessions: notClaude(p.orphan_sessions) }));
  return { ...st, usage, projects };
}

/* The demo's make-believe projects (v0.5.19, the new-project wizard): in demo mode a POST never reaches a box, so a project, a blank repo, a clone or a bulk import made in the wizard
   is remembered here for the life of the page and laid over the fixture's state, so the project page the wizard ends on has what was just made (a cloned repo reads as cloned, on main). */
const demoMade = { projects: [] };
function demoRepoName(url) {
  const tail = String(url || '').trim().replace(/\/+$/, '').split(/[/:]/).pop().replace(/\.git$/, '');
  return tail.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^[-_]+|[-_]+$/g, '').slice(0, 64);
}
function demoMakeRepo(project, name) {
  let p = demoMade.projects.find((x) => x.name === project);
  if (!p) { p = { name: project, path: `/home/demo/projects/${project}`, root: null, orphan_sessions: [], repos: [] }; demoMade.projects.push(p); }
  if (name && !p.repos.some((r) => r.name === name)) p.repos.push({ name, path: `${p.path}/${name}`, state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] });
}
const demoNoAuto = {};                                 // #84: tmux name -> the Auto-continue switch as the visitor left it (the fixture has no board to store it)
function demoMake(method, path, body) {
  const b = body && typeof body === 'object' ? body : {};
  let m = null;
  if (method === 'POST' && (m = /^\/api\/sessions\/([^/]+)\/flags$/.exec(path)) && typeof b.no_autoresume === 'boolean') {
    demoNoAuto[decodeURIComponent(m[1])] = b.no_autoresume;
  } else if (method === 'POST' && path === '/api/projects' && typeof b.name === 'string' && b.name) {
    demoMakeRepo(b.name, '');
    if (b.url) demoMakeRepo(b.name, demoRepoName(b.url));
  } else if (method === 'POST' && (m = /^\/api\/projects\/([^/]+)\/repos(\/bulk)?$/.exec(path))) {
    const project = decodeURIComponent(m[1]);
    demoMakeRepo(project, '');
    if (m[2]) for (const r of Array.isArray(b.repos) ? b.repos : []) demoMakeRepo(project, (r && (r.name || demoRepoName(r.url))) || '');
    else demoMakeRepo(project, b.name || demoRepoName(b.url));
  }
}
function demoMadeState(st) {
  if (st && Array.isArray(st.projects) && Object.keys(demoNoAuto).length) {
    const lay = (list) => (Array.isArray(list) ? list.map((x) => (ownKey(demoNoAuto, x.tmux) ? { ...x, flags: { ...(x.flags || {}), no_autoresume: demoNoAuto[x.tmux] || undefined } } : x)) : list);
    st = { ...st, projects: st.projects.map((p) => ({ ...p, root: p.root ? { ...p.root, sessions: lay(p.root.sessions) } : p.root, repos: (p.repos || []).map((r) => ({ ...r, sessions: lay(r.sessions) })) })) };
  }
  if (!demoMade.projects.length || !st || !Array.isArray(st.projects)) return st;
  const made = new Map(demoMade.projects.map((p) => [p.name, p]));
  const merged = st.projects.map((p) => (made.has(p.name) ? { ...p, repos: [...p.repos, ...made.get(p.name).repos.filter((r) => !p.repos.some((x) => x.name === r.name))] } : p));
  for (const p of demoMade.projects) if (!st.projects.some((x) => x.name === p.name)) merged.push(p);
  return { ...st, projects: merged };
}

/* The Memory page's degraded states in demo mode: ?mem=down|degraded|stale|partial|ambiguous|incompatible|untested picks a variant from
   demo/memory_states.json for every /api/memory/<project>/* answer. A 503 variant is thrown the way api() throws one (err.status, err.body);
   the others are laid over the route's own fixture. No flag (or an unknown one) answers the fixture as it is. */
function demoMemFlag() { try { const m = /[?&]mem=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }
async function demoMemVariant(data0, bare) {
  const pm = /^\/api\/memory\/([^/]+)\//.exec(bare || '');
  let name = null;
  try { name = pm ? decodeURIComponent(pm[1]) : null; } catch (_) { name = null; }
  const data = name && data0 && typeof data0 === 'object' && typeof data0.project === 'string' ? { ...data0, project: name } : data0;   // every demo project answers the same rows, under its own name
  const flag = demoMemFlag();
  if (!flag) return data;
  const vname = 'memory_states';
  const r = await fetch(`/static/demo/${vname}.json`);
  const states = r.ok ? await r.json() : {};
  const v = flag !== '_note' && ownKey(states, flag) ? states[flag] : null;
  if (!v) return data;
  if (v.status !== 200) { const e = demoError(v.status, v.body && v.body.error); e.body = v.body; throw e; }
  return { ...data, ...v.body };
}

// demo/state.json must carry every key of the real /api/state: tests/test_api_v054.py (the demo state test) fails when build_state() gains one.
async function demoApi(method, path, body) {
  if (method !== 'GET') {
    await new Promise((resolve) => setTimeout(resolve, 150));
    demoMake(method, path, body);
    if (/^\/api\/nodes(\/|$)/.test(path)) { const made = demoNodesWrite(method, path, body); if (made) return made; }   // pairing (issue #135): played here, nothing leaves the page
    if (path === '/api/usage/refresh') {
      if (demoRefreshFlag() === 'none') throw demoError(409, 'no Claude session is at its prompt; start one to refresh');
      demoRefresh.tap = Date.now() / 1000;
      demoRefresh.base = demoRefresh.shown;
      return { ok: true, session: 'ccboard--ccboard--s1', started_at: new Date().toISOString() };
    }
    return { ok: true };
  }
  const bare = path.split('?')[0];
  let name = null;
  if (bare === '/api/state') name = 'state';
  else if (bare === '/api/node') name = 'node';   // this board's node card (GET /api/node, issue #133): the same shape the route answers
  else if (bare === '/api/nodes' || bare === '/api/nodes/pairs' || bare === '/api/nodes/audit') name = 'nodes';   // Settings > Nodes > Paired nodes, Who can control this node and Activity (issue #135): one fixture, three answers
  else if (bare === '/api/nodes/discover') name = 'nodes-discover';   // Settings > Nodes > Found on your tailnet (issue #134): a found node, a refusing one, an offline one and one with nothing listening
  else if (/^\/api\/sessions\/[^/]+$/.test(bare)) name = 'session';    // the terminal page's own read: the row of state.json with that tmux name (its agent drives Tune and the quick replies)
  else if (bare === '/api/skills') name = 'skills';   // the palette's Skills group (issue #102): the same shape GET /api/skills answers
  else if (bare === '/api/agents') name = 'agents';   // the launcher's option schemas (v0.5.13); the same shape GET /api/agents answers
  else if (bare.startsWith('/api/search')) name = 'search';
  else if (/^\/api\/projects\/[^/]+\/repos\/[^/]+\/tree$/.test(bare)) name = 'tree';
  else if (/^\/api\/projects\/[^/]+\/repos\/[^/]+\/file$/.test(bare)) name = 'file';
  else if (bare.startsWith('/api/series/events')) name = 'series_events';   // before the '/api/series' prefix: the Gantt must not draw the limit series as sessions
  else if (bare.startsWith('/api/series')) name = 'series';
  else if (bare === '/api/usage/summary') name = 'usage_summary';
  else if (bare === '/api/memory/health') name = 'memory_health';   // the Memory proxy (v0.5.20, docs/memory-api.md): one fixture per route, the same for every project
  else if (bare === '/api/memory/prefs') name = 'memory_prefs';
  else if (/^\/api\/memory\/[^/]+\/observations$/.test(bare)) name = 'memory_observations';
  else if (/^\/api\/memory\/[^/]+\/summaries$/.test(bare)) name = 'memory_summaries';
  else if (/^\/api\/memory\/[^/]+\/search$/.test(bare)) name = 'memory_search';
  else if (/^\/api\/memory\/[^/]+\/timeline$/.test(bare)) name = 'memory_timeline';
  else if (/^\/api\/memory\/[^/]+\/palace$/.test(bare)) name = 'memory_palace';
  else if (bare.startsWith('/api/memory/')) name = 'memory';
  else if (/^\/api\/tasks\/[^/]+\/diff$/.test(bare)) name = 'diff';   // the Tasks card's Diff/PR sheet (v0.5.21): one made-up branch, two commits, three files
  else if (bare === '/api/doctor') name = 'doctor';       // the Settings > Doctor checklist (v0.5.19)
  else if (/^\/api\/projects\/[^/]+\/repos\/[^/]+\/issues(\/\d+)?$/.test(bare)) name = 'issues';   // the launcher's "from a GitHub issue" (v0.5.20): one made-up list and its details
  if (!name) return {};
  if (name === 'session') {
    let tmuxName = '';
    try { tmuxName = decodeURIComponent(bare.slice('/api/sessions/'.length)); } catch (_) { tmuxName = ''; }
    const rs = await fetch('/static/demo/state.json');
    if (!rs.ok) throw new Error(`demo fixture state.json: ${rs.status} ${rs.statusText}`);
    const st = demoRebase(await rs.json());
    for (const p of st.projects || []) for (const r of p.repos || []) for (const s of r.sessions || []) if (s && s.tmux === tmuxName) return { ...s, project: p.name, repo: r.name };
    throw demoError(404, 'no such session');
  }
  const r = await fetch(`/static/demo/${name}.json`);
  if (!r.ok) throw new Error(`demo fixture ${name}.json: ${r.status} ${r.statusText}`);
  const data = await r.json();
  if (name === 'issues') {
    const one = /\/issues\/(\d+)$/.exec(bare);
    if (!one) return { issues: data.issues };
    if (!ownKey(data.detail, one[1])) throw demoError(404, 'not found');
    return data.detail[one[1]];
  }
  if (name === 'usage_summary' && /[?&]basis=est(&|$)/.test(path)) return { ...demoRebase(data), basis: 'est' };   // the Estimated basis (issue #95): the demo has nothing to estimate, so the same numbers under the other name (rebased like the reported one, or the windows read as rolled over)
  if (name === 'nodes') return demoNodes(data, bare);
  if (name === 'nodes-discover') return demoDiscover(data, /[?&]refresh=1(&|$)/.test(path));
  if (name === 'tree' || name === 'file') return demoPick(name, bare, path.slice(bare.length + 1), data);
  if (/^memory_(observations|summaries|search|timeline|palace)$/.test(name)) return demoMemVariant(data, bare);
  if (name === 'series_events' && data && data.demo && data.demo.epoch && Array.isArray(data.events)) {   // keep the fixture's 24 h alive, like state.json
    const dt = Math.floor(Date.now() / 1000 - data.demo.epoch);
    return { ...data, events: data.events.map((e) => ({ ...e, t: e.t + dt })) };
  }
  if (name === 'series' && data && data.demo && data.demo.epoch && data.meta && typeof data.meta === 'object') {   // the window resets of the series follow the clock too: a Codex window that ends after the fixture's moment must not read as 'rolled over' now
    const dt = Math.floor(Date.now() / 1000 - data.demo.epoch);
    const meta = {};
    for (const [k, m] of Object.entries(data.meta)) meta[k] = m && typeof m.resets_at === 'number' ? { ...m, resets_at: m.resets_at + dt } : m;
    return { ...data, meta };
  }
  if (name === 'state') return demoMadeState(demoUsageRefresh(demoRebase(data)));                 // the usage reading's own time follows the make-believe refresh above
  return name === 'usage_summary' ? demoRebase(data) : data;                         // both carry demo.epoch: the summary's reset times and ISO stamps ride along with the state's
}

async function api(method, path, body) {
  if (demoOn()) return demoApi(method, path, body);
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
    if (changed || force) render(force);
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
