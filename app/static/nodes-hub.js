/* ccboard hub view, lazy half (issue #139). Loaded by lazy.js as the 'nodeshub' bundle (with pages/node.js and pages/nodes.css) once state.nodes_enabled is true, or for a #/n/ address;
   nodes.js holds the few eager bytes (Nodes.enabled, Nodes.chip, Nodes.use) and every page calls into this file through them. Classic script, definition-only at load: it adds to the
   `Nodes` namespace, registers one keyboard chord and sets Nodes.ready; the poll starts with Nodes.start() (nodes.js Nodes.sync, on every state).

   The model. Nodes.poll() reads GET /api/nodes/state on this board's own origin (never another node: the page may only talk to its own origin, the hub's backend polls the nodes) every
   6 s while the page is visible, with the held ETag as If-None-Match. 304 keeps the model, a failure keeps the last model and says how old it is, a hidden tab asks nothing. In demo mode
   the answer is demo/nodes.json (key `hub`), through core.js's demo handler (demoHubState below). A record is {handle, name, url, status, last_ok_at, age_s, skew_warn, card, state, scopes, ...}
   (app/nodes_hub.py _public). Its `age_s` is as of the last 200; the page adds the time since.

   Peer data is untrusted: every name, title and session name goes through textContent (el's `text`), never into markup; the only attribute built from peer data is an href that is
   a #/ address made by Ref or an https tailnet address that Nodes.boardUrl accepts, opened with rel="noopener noreferrer" in a new tab. No iframe, no request to another origin. */
'use strict';

Nodes.POLL_MS = 6000;
Nodes.SB_MAX = 20;                                           // sessions of one node in the sidebar before "more"
Nodes.M = { recs: [], by: new Map(), etag: null, recvAt: 0, err: null, errAt: 0, loaded: false, busy: false, timer: null, subs: new Set(), rev: 0, tfilter: 'all', hidden: null };

/* ---------- words ---------- */

/* 45 s, 3 min, 2 h, 1 d */
function nhSpan(sec) {
  const s = Math.max(0, Math.floor(sec));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h`;
  return `${Math.floor(s / 86400)} d`;
}

/* 45 seconds, 1 minute, 4 minutes, 2 hours, 1 day: the whole-word form of nhSpan, for a sentence ("last seen 4 minutes ago") */
function nhAgo(sec) {
  const s = Math.max(0, Math.floor(sec));
  const [n, u] = s < 60 ? [s, 'second'] : s < 3600 ? [Math.floor(s / 60), 'minute'] : s < 86400 ? [Math.floor(s / 3600), 'hour'] : [Math.floor(s / 86400), 'day'];
  return `${n} ${u}${n === 1 ? '' : 's'}`;
}

/* 09:14, or 7 Oct 09:14 when it was more than a day ago */
function nhClock(iso, nowMs) {
  const t = typeof iso === 'string' ? Date.parse(iso) : NaN;
  if (Number.isNaN(t)) return '';
  const d = new Date(t);
  const pad = (n) => String(n).padStart(2, '0');
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (nowMs - t < 86400000) return hm;
  return `${d.getDate()} ${['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.getMonth()]} ${hm}`;
}

/* Seconds since the hub's last good reading of a node: its age_s as of the last answer, plus what has passed here since. null: never read. */
Nodes.ageOf = function (rec, nowMs) {
  if (!rec || typeof rec.age_s !== 'number') return null;
  return rec.age_s + Math.max(0, ((nowMs || Date.now()) - Nodes.M.recvAt) / 1000);
};

/* The status of a record as words: {status, glyph, word, tone, age}. Glyph and word always travel together (a state is never colour only). */
Nodes.view = function (rec, nowMs) {
  const now = nowMs || Date.now();
  const age = Nodes.ageOf(rec, now);
  const status = ['online', 'stale', 'offline', 'unauthorized', 'unpaired'].includes(rec && rec.status) ? rec.status : 'offline';
  const since = rec && rec.last_ok_at ? nhClock(rec.last_ok_at, now) : '';
  const v = {
    online: { glyph: '●', word: 'online', tone: 'ok' },
    stale: { glyph: '◐', word: age === null ? 'stale' : `stale ${nhSpan(age)}`, tone: 'warn' },
    offline: { glyph: '○', word: since ? `offline since ${since}` : 'offline, never read', tone: '' },
    unauthorized: { glyph: '!', word: 're-pair', tone: 'bad' },
    unpaired: { glyph: '?', word: 'not paired any more', tone: 'bad' },
  }[status];
  return { status, age, ...v };
};

/* ---------- the model ---------- */

Nodes.list = function () { return Nodes.M.recs.slice(); };
Nodes.get = function (handle) { return Nodes.M.by.get(handle) || null; };

function nhSessions(rec) {
  const rows = rec && rec.state && Array.isArray(rec.state.sessions) ? rec.state.sessions : [];
  const now = Date.now();
  const rev = Nodes.M.rev;
  return rows.filter((s) => s && typeof s.tmux === 'string' && !nsClosed(rec.handle, s.tmux, now))
    .map((s) => (Nodes.acked.get(`${rec.handle}/${s.tmux}`) === rev ? { ...s, needs_you: false, node: rec.handle } : { ...s, node: rec.handle }));
}
function nhTasks(rec) {
  const rows = rec && rec.state && Array.isArray(rec.state.tasks) ? rec.state.tasks : [];
  return rows.filter((t) => t && (typeof t.id === 'number' || /^\d+$/.test(String(t.id)))).map((t) => ({ ...t, node: rec.handle }));
}
Nodes.sessions = function (handle) { return nhMerge('session', handle, (handle ? [Nodes.get(handle)] : Nodes.M.recs).filter(Boolean).flatMap(nhSessions)); };
Nodes.tasks = function (handle) { return nhMerge('task', handle, (handle ? [Nodes.get(handle)] : Nodes.M.recs).filter(Boolean).flatMap(nhTasks)); };

/* Optimistic rows (issue #141): a task or a session started on a node shows at once, before the node's next reading carries it. Nodes.pend({kind, node, ...row}) adds one with
   pending 'starting' ("Starting on <node>"), Nodes.pended(p, patch) turns it into 'started' when the node's answer came (its id or tmux is then known), Nodes.unpend(p) takes it
   away (a refusal). A row goes by itself when the reading carries the same task id or session name, and 3 minutes after it was made. A row with `replace` (the dispatch of a task
   that is already in the reading) stands in the place of the reading's row until that row leaves the phase it had (`was`). Memory only, never stored. */
Nodes.pending = [];
const NH_PENDING_MS = 180000;
function nhMerge(kind, handle, rows) {
  const now = Date.now();
  const keyOf = (r) => (kind === 'task' ? `${r.node}:${r.id}` : `${r.node}/${r.tmux}`);
  const gone = new Set(Nodes.pending.filter((p) => now - p.at >= NH_PENDING_MS));
  const live = [];
  const hide = new Set();
  for (const p of Nodes.pending) {
    if (gone.has(p) || p.kind !== kind || (handle && p.node !== handle)) continue;
    const seen = rows.find((r) => keyOf(r) === keyOf(p));
    if (p.replace) {
      if (seen && seen.phase !== p.was) { gone.add(p); continue; }                      // the reading has moved on: it is the truth now
      if (seen) hide.add(keyOf(p));
      live.push(p);
    } else if (seen) gone.add(p);                                                         // the reading carries it now
    else live.push(p);
  }
  if (gone.size) Nodes.pending = Nodes.pending.filter((p) => !gone.has(p));
  return rows.filter((r) => !hide.has(keyOf(r))).concat(live);
}
Nodes.pend = function (row) {
  const p = { ...row, pending: 'starting', at: Date.now() };
  Nodes.pending.push(p);
  Nodes.changed(false);
  return p;
};
Nodes.pended = function (p, patch) { Object.assign(p, patch || {}, { pending: 'started', at: Date.now() }); Nodes.changed(false); return p; };
Nodes.unpend = function (p) { Nodes.pending = Nodes.pending.filter((x) => x !== p); Nodes.changed(false); };

/* Can an action that needs `scope` be sent to this node now? {ok, why}: why is the plain sentence a disabled control shows, in the order the hub refuses (a paired token, the
   pair's scope, the reading: nothing is queued for later). A stale node (its last try failed, the reading is younger than 10 minutes) is still called. */
Nodes.can = function (rec, scope) {
  if (!rec) return { ok: false, why: 'this node is not paired any more' };
  const name = nhName(rec);
  if (rec.legacy) return { ok: false, why: `${name} was added without a token: pair it to act on it` };
  if (!(Array.isArray(rec.scopes) && rec.scopes.includes(scope))) return { ok: false, why: `needs the ${scope} scope on ${name}` };
  if (rec.status === 'unauthorized') return { ok: false, why: `${name} no longer takes this board's token: re-pair it in Settings, Nodes` };
  if (rec.status === 'unpaired') return { ok: false, why: `${name} answers as another node: remove it and pair it again` };
  if (rec.status === 'offline') {
    const age = Nodes.ageOf(rec);
    return { ok: false, why: age === null ? `${name} has not answered yet` : `${name} is offline, last seen ${nhAgo(age)} ago` };
  }
  return { ok: true, why: '' };
};

/* {sessions, working, needs, tasks} of a record, null for each while it has no reading. needs = sessions that need you plus the permission requests waiting (as the hub counts it). */
Nodes.counts = function (rec) {
  const st = rec && rec.state;
  if (!st) return { sessions: null, working: null, needs: null, tasks: null };
  const ss = nhSessions(rec);
  const ny = st.needs_you && typeof st.needs_you === 'object' ? st.needs_you : {};
  return { sessions: ss.length, working: ss.filter((s) => s.state === 'working').length, needs: ss.filter((s) => s.needs_you).length + (Number(ny.permissions) || 0),
    tasks: nhTasks(rec).filter((t) => !['done', 'failed', 'cancelled'].includes(t.phase)).length };
};

/* The 5-hour window of a record's Claude account: {pct, limited} or null. */
Nodes.five = function (rec) {
  const w = rec && rec.state && rec.state.usage && rec.state.usage.claude;
  return w && w.known && typeof w.pct === 'number' ? { pct: Math.round(w.pct), limited: !!w.limited } : null;
};

Nodes.refreshInfo = function () {
  const now = Date.now();
  const keep = new Set();
  for (const r of Nodes.M.recs) {
    const v = Nodes.view(r, now);
    keep.add(r.handle);
    Nodes.info[r.handle] = { name: typeof r.name === 'string' && r.name ? r.name : r.handle, status: v.status, word: v.word, glyph: v.status === 'online' ? '' : v.glyph };
  }
  for (const k of Object.keys(Nodes.info)) if (!keep.has(k)) delete Nodes.info[k];
};

/* Take an answer in. Returns true when the model changed. */
Nodes.apply = function (data, etag) {
  const M = Nodes.M;
  const recs = (data && Array.isArray(data.nodes) ? data.nodes : []).filter((r) => r && typeof r === 'object' && Ref.isHandle(r.handle));
  M.recs = recs;
  M.by = new Map(recs.map((r) => [r.handle, r]));
  M.etag = etag || null;
  M.recvAt = Date.now();
  M.err = null;
  M.loaded = true;
  M.rev += 1;
  Nodes.refreshInfo();
  return true;
};

/* One request: {status: 200, etag, data} or {status: 304}. In demo mode the fixture answers, through api(). */
async function nhFetch(etag) {
  if (typeof demoOn === 'function' && demoOn()) return { status: 200, etag: null, data: await api('GET', '/api/nodes/state') };
  const headers = { 'X-CCBoard': '1' };
  if (etag) headers['If-None-Match'] = etag;
  const r = await fetch('/api/nodes/state', { headers });
  if (r.status === 304) return { status: 304 };
  if (!r.ok) throw new Error(`${r.status} ${r.statusText || ''}`.trim());
  return { status: 200, etag: r.headers && typeof r.headers.get === 'function' ? r.headers.get('ETag') : null, data: await r.json() };
}

/* Ask the board for the nodes now: nothing while the view is off or the tab is hidden (force: true asks anyway), nothing while one request is out. */
Nodes.poll = async function (o) {
  const M = Nodes.M;
  if (!Nodes.enabled()) return null;
  if (typeof document !== 'undefined' && document.hidden && !(o && o.force)) return null;
  if (M.busy) return null;
  M.busy = true;
  let changed = false;
  try {
    const r = await nhFetch(M.etag);
    if (r.status === 304) { changed = M.err !== null; M.err = null; }
    else changed = Nodes.apply(r.data, r.etag);
  } catch (e) {
    changed = !M.err;
    M.err = (e && e.message) || 'no answer';
    M.errAt = Date.now();
  } finally { M.busy = false; }
  Nodes.changed(changed);
  Nodes.permsSync();                                   // the pending permission requests of the nodes that report one (issue #142)
  return changed;
};

/* Repaint what shows nodes: the slots and the node page always (their ages tick), the sidebar and the page when the model changed. */
Nodes.changed = function (modelChanged) {
  const M = Nodes.M;
  for (const fn of Array.from(M.subs)) { try { fn(); } catch (e) { console.error('ccboard nodes', e); } }
  if (!modelChanged) return;
  try { if (typeof settingsFill === 'function') settingsFill('nodes', false); } catch (e) { console.error('ccboard nodes settings', e); }   // Settings, Nodes: the read model's line under each paired node
  try { if (typeof Shell !== 'undefined' && Shell && typeof Shell.patchTrees === 'function') Shell.patchTrees(); } catch (e) { console.error('ccboard nodes sidebar', e); }
  try { if (typeof Palette !== 'undefined' && Palette && typeof Palette.refresh === 'function') Palette.refresh(); } catch (e) { console.error('ccboard nodes palette', e); }
};

Nodes.start = function () {
  const M = Nodes.M;
  if (M.timer || !Nodes.enabled()) return;
  M.timer = setInterval(() => Nodes.poll(), Nodes.POLL_MS);
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function' && !M.hidden) {
    M.hidden = () => { if (!document.hidden) Nodes.poll(); };                       // back on screen: one reading at once
    document.addEventListener('visibilitychange', M.hidden);
  }
  Nodes.poll();
};

Nodes.stop = function () {
  const M = Nodes.M;
  if (!M.timer && !M.loaded && !M.subs.size) return;
  if (M.timer) { clearInterval(M.timer); M.timer = null; }
  for (const host of Array.from(Nodes.hosts || [])) { if (host.remove) host.remove(); }
  Nodes.hosts = new Set();
  M.subs.clear();
  M.recs = []; M.by = new Map(); M.etag = null; M.loaded = false; M.err = null;
  Nodes.pending = [];
  Nodes.steerClear();
  Nodes.refreshInfo();
  try { if (typeof Shell !== 'undefined' && Shell && typeof Shell.patchTrees === 'function') Shell.patchTrees(); } catch (_) { /* no shell */ }
};

/* ---------- addresses ---------- */

/* The hostname is a tailnet name (<machine>.<tailnet>.ts.net), a tailnet IPv4 (100.64.0.0/10) or a tailnet IPv6 (fd7a:115c:a1e0::/48). */
Nodes.tailnetHost = function (host) {
  const h = String(host).toLowerCase();
  if (/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.ts\.net$/.test(h)) return true;
  const v4 = /^100\.(\d{1,3})\.\d{1,3}\.\d{1,3}$/.exec(h);
  if (v4) return Number(v4[1]) >= 64 && Number(v4[1]) <= 127;
  return /^\[fd7a:115c:a1e0:[0-9a-f:]*\]$/.test(h);
};

/* The node's own address as a link target, or null: https only, a tailnet name or address, an optional port, no user info, no path. The registry's url comes from pairing, but it
   is still checked here, because it becomes an href. */
Nodes.boardUrl = function (url) {
  if (typeof url !== 'string' || !/^https:\/\//i.test(url) || !/^https:\/\/[^/?#]+\/?$/i.test(url)) return null;
  const hp = NodeView.hostOf(url);                                  // host[:port], '' for user info or a bad address
  const m = /^(\[[0-9a-fA-F:]+\]|[^:\[\]]+)(?::(\d{1,5}))?$/.exec(hp);
  if (!m || !Nodes.tailnetHost(m[1])) return null;
  if (m[2] && (Number(m[2]) < 1 || Number(m[2]) > 65535)) return null;
  return `https://${m[1].toLowerCase()}${m[2] ? ':' + m[2] : ''}/`;
};

/* A link to this node's own page for a thing on it: its board address plus a #/ route of that board (a session peek, the task board). null when the address is not safe. */
Nodes.openUrl = function (rec, route) {
  const base = Nodes.boardUrl(rec && rec.url);
  if (!base) return null;
  return route && /^#\/[A-Za-z0-9_\/-]*$/.test(route) ? base + route : base;
};

/* An external link to a node's page: a new tab, no opener, no referrer. */
function nhOpenLink(label, rec, route, cls) {
  const href = Nodes.openUrl(rec, route);
  if (!href) return null;
  return el('a', { class: cls || 'btn small', href, target: '_blank', rel: 'noopener noreferrer', text: label });
}

/* ---------- controls that need a relay action: present, never active in this version ---------- */

let nhSeq = 0;
Nodes.reason = function (rec, scope) {
  const name = rec && typeof rec.name === 'string' && rec.name ? rec.name : (rec && rec.handle) || 'the node';
  const scopes = rec && Array.isArray(rec.scopes) ? rec.scopes : [];
  return scopes.includes(scope) ? `read only in this version` : `needs the ${scope} scope on ${name}`;
};

/* A disabled control that says why: aria-disabled (so keyboard and touch can reach it and read the reason), described by its reason, which Nodes.acts prints once under the grid.
   `short` is how the reason's sentence names the control ("New task: needs the tasks scope on build-box."). */
Nodes.off = function (label, reason, short) {
  nhSeq += 1;
  const id = `nd-why-${nhSeq}`;
  const b = el('button', { class: 'small nd-off', type: 'button', 'aria-disabled': 'true', 'aria-describedby': id, title: reason, text: label,
    onclick: (e) => { e.preventDefault(); if (typeof toast === 'function') toast(`${label}: ${reason}`, { kind: 'warn' }); } });
  b.ccWhy = { id, name: short || label, reason };
  return b;
};

/* The actions of a node page or a peek on one grid of equal cells (the first is the page's filled primary), and under it one short note with the reason of every disabled cell. */
Nodes.acts = function (...cells) {
  const list = cells.flat().filter(Boolean);
  const whys = list.filter((c) => c.ccWhy);
  return el('div', { class: 'nd-actbox' }, el('div', { class: 'nd-acts' }, ...list),
    whys.length ? el('p', { class: 'dim nd-whys' }, ...whys.map((c, i) => [i ? ' ' : '', el('span', { id: c.ccWhy.id, class: 'nd-why', text: `${c.ccWhy.name}: ${c.ccWhy.reason}.` })])) : null);
};

/* ---------- small builders ---------- */

function nhAgent(a) { return a === 'codex' || a === 'shell' ? a : 'claude'; }
function nhName(rec) { return typeof rec.name === 'string' && rec.name ? rec.name : rec.handle; }
function nhStatusLine(v) { return el('span', { class: `nd-status ${v.tone}`.trim() }, el('span', { class: 'nd-sg', 'aria-hidden': 'true', text: v.glyph }), el('span', { text: v.word })); }
function nhAgeText(iso) { const t = Date.parse(iso); return Number.isNaN(t) ? '' : fmtAge(t / 1000); }
function nhWhere(s) { return s.project ? `${s.project}/${s.repo === 'root' ? 'project folder' : s.repo || '?'}` : String(s.repo || ''); }
function nhAttached(n) { return !!n && n.isConnected !== false; }

/* A keyed list of rows whose content is rebuilt only when its signature changed. build(item) makes the row (a fresh element); sigOf(item) is what it shows. */
function nhList(parent, keyOf, build, sigOf) {
  const fill = (n, item) => {
    const f = build(item);
    n.className = f.className;
    for (const a of ['aria-label', 'data-status', 'data-phase']) { const v = f.getAttribute(a); if (v === null) n.removeAttribute(a); else n.setAttribute(a, v); }
    n.textContent = '';
    n.append(...Array.from(f.childNodes));
    n.ccSig = sigOf(item);
  };
  return makeKeyedList(parent, {
    key: keyOf,
    create: (item) => { const n = build(item); n.ccSig = sigOf(item); return n; },
    patch: (n, item) => { if (n.ccSig !== sigOf(item)) fill(n, item); },
  });
}

/* The row of one remote session: state and agent glyphs, the name (a link to its read only peek), where it runs, its node chip and its age. */
function nhSessionRow(s) {
  const href = Ref.hash({ tmux: s.tmux, node: s.node });
  const name = String(s.session || s.tmux);
  const label = el(href ? 'a' : 'span', { class: 'nd-name', href: href || null, text: name });
  const st = ownKey(STATE_GLYPH, s.state) ? s.state : 'unknown';
  return el('div', { class: 'nd-item' + (s.needs_you ? ' attn' : ''), 'data-key': Ref.key({ tmux: s.tmux, node: s.node }) },
    el('span', { class: 'nd-g' }, stateGlyph(st), agentGlyph(nhAgent(s.agent))), label, el('span', { class: 'dim nd-where', text: nhWhere(s) }),
    Nodes.chip(s.node), el('span', { class: 'dim nd-meta', text: s.pending === 'starting' ? `Starting on ${Nodes.nameOf(s.node)}` : [s.pending === 'started' ? 'started' : GLYPH_LABEL[st], s.since ? nhAgeText(s.since) : ''].filter(Boolean).join(' · ') }));
}
function nhSessionSig(s) { return JSON.stringify([s.tmux, s.session, s.state, s.needs_you, s.agent, s.project, s.repo, s.since, s.pending, Math.floor(Date.now() / 60000), Nodes.sig(s)]); }

const NH_PHASE = { queued: '○', running: '✽', review: '✻', done: '✓', failed: '✕', cancelled: '○', backlog: '∙' };
function nhTaskRow(t) {
  const href = Ref.hash(Ref.task(t.node, t.id));
  const title = String(t.title || `Task ${t.id}`);
  const phase = String(t.phase || 'running');
  return el('div', { class: 'nd-item nd-task', 'data-key': Ref.key({ kind: 'task', node: t.node, id: t.id }), 'data-phase': phase },
    el('span', { class: 'nd-g' }, el('span', { class: 'glyph', 'aria-hidden': 'true', text: ownKey(NH_PHASE, phase) ? NH_PHASE[phase] : '·' }), agentGlyph(nhAgent(t.agent))),
    el(href ? 'a' : 'span', { class: 'nd-name', href: href || null, text: title }), el('span', { class: 'dim nd-where', text: nhWhere(t) + (t.issue_ref ? ` ${t.issue_ref}` : '') }),
    Nodes.chip(t.node), el('span', { class: 'dim nd-meta', text: t.pending === 'starting' ? `Starting on ${Nodes.nameOf(t.node)}` : [phase, t.updated_at ? nhAgeText(t.updated_at) : ''].filter(Boolean).join(' · ') }));
}
function nhTaskSig(t) { return JSON.stringify([t.id, t.title, t.phase, t.agent, t.project, t.repo, t.issue_ref, t.updated_at, t.pending, Math.floor(Date.now() / 60000), Nodes.sig(t)]); }

/* ---------- slots: the sections other pages host ---------- */

Nodes.hosts = new Set();

/* Nodes.slot(name, anchor, opts): make (once) and repaint a section next to `anchor`: 'strip' (Home, after the away line), 'inbox' (Home and #/inbox, after the local cards), 'tasks' (the filter
   before the board, the tasks of other nodes after it). The section registers its repaint, which the poll calls every 6 s. */
Nodes.slot = function (name, anchor, opts) {
  if (!anchor || !Nodes.SLOTS[name]) return;
  let s = anchor.ccSlots && anchor.ccSlots[name];
  if (s && !nhAttached(s.hosts[0])) { delete anchor.ccSlots[name]; s = null; }          // Nodes.stop took its sections away: build them again when nodes come back
  const o = Object.assign((s && s.o) || {}, opts || {});                    // the page's latest facts (a limit, how many local cards): the section reads them when it paints
  if (!s) {
    s = Nodes.SLOTS[name](anchor, o);
    s.o = o;
    (anchor.ccSlots ||= {})[name] = s;
    for (const h of s.hosts) Nodes.hosts.add(h);
    const paint = () => { if (!nhAttached(anchor)) { Nodes.M.subs.delete(paint); return; } s.paint(); };
    Nodes.M.subs.add(paint);
  }
  s.paint();
};

function nhInsertAfter(anchor, node) { anchor.parentNode.insertBefore(node, anchor.nextSibling); return node; }

/* The cells of the strip, local first. A cell: the node's name, its status as glyph and word, live sessions, who needs you, the 5-hour pill; offline cells are dim and say how long ago. */
function nhLocalCell() {
  const st = typeof state !== 'undefined' ? state : null;
  const card = st && st.node && st.node.sessions ? st.node.sessions : null;
  const live = card && typeof card.live === 'number' ? card.live : (typeof rosterSessions === 'function' && st ? rosterSessions(st).filter((x) => x.state !== 'ended').length : null);
  const needs = card && typeof card.needs_you === 'number' ? card.needs_you : (typeof Inbox !== 'undefined' && st ? Inbox.items(st).length : 0);
  let five = null;
  try { const w = typeof limitUsageWindow === 'function' && st ? limitUsageWindow(st, '5h') : null; if (w) five = { pct: Math.round(w.pct), limited: false }; } catch (_) { five = null; }
  return { key: 'local', handle: null, name: Nodes.nameOf(null), href: '#/agents', view: { status: 'online', glyph: '●', word: 'online', tone: 'ok' }, sessions: live, needs, five, local: true };
}
function nhRemoteCell(rec) {
  const c = Nodes.counts(rec);
  return { key: rec.handle, handle: rec.handle, name: nhName(rec), href: Ref.hash(Ref.node(rec.handle)) || '#/', view: Nodes.view(rec), sessions: c.sessions, needs: c.needs, five: Nodes.five(rec), rec };
}
function nhCellNode(c) {
  const dim = c.view.status !== 'online' && !c.local;
  const sub = [];
  if (c.sessions === null) sub.push('no reading yet'); else sub.push(`${c.sessions} live`, `${c.needs} need you`);
  const ago = !c.local && c.view.status !== 'online' && c.view.age !== null ? `last reading ${nhSpan(c.view.age)} ago` : '';
  const a = el('a', { class: 'nd-cell' + (dim ? ' dim' : '') + (c.needs > 0 ? ' attn' : ''), href: c.href, 'data-key': c.key, 'data-status': c.view.status,
    'aria-label': `${c.name}: ${c.view.word}, ${sub.join(', ')}` },
    el('span', { class: 'nd-cell-h' }, el('span', { class: 'nd-sg', 'aria-hidden': 'true', text: c.view.glyph }), el('b', { class: 'nd-cell-n', text: c.local ? `${c.name} (this node)` : c.name })),
    el('span', { class: 'nd-cell-s ' + c.view.tone, text: c.view.word }),
    el('span', { class: 'nd-cell-k', text: sub.join(' · ') }),
    ago ? el('span', { class: 'nd-cell-ago', text: ago }) : null,
    c.five ? el('span', { class: 'nd-pill' + (c.five.limited ? ' warn' : ''), title: '5-hour window of the Claude account', text: `5h ${c.five.pct}%${c.five.limited ? ' limit' : ''}` }) : el('span', { class: 'nd-pill none', title: 'No 5-hour reading', text: '5h n/a' }));
  return a;
}
function nhCellSig(c) { return JSON.stringify([c.key, c.name, c.view.status, c.view.word, c.sessions, c.needs, c.five, Math.floor(Date.now() / 60000), c.view.age === null ? null : Math.floor(c.view.age / 60)]); }

function nhArrowKeys(e, root) {
  if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
  const cells = Array.from(root.querySelectorAll('a.nd-cell'));
  const i = cells.indexOf(e.target && e.target.closest ? e.target.closest('a.nd-cell') : e.target);
  if (i < 0) return;
  const j = e.key === 'Home' ? 0 : e.key === 'End' ? cells.length - 1 : Math.max(0, Math.min(cells.length - 1, i + (e.key === 'ArrowRight' ? 1 : -1)));
  e.preventDefault();
  if (cells[j]) cells[j].focus();
}

Nodes.SLOTS = {
  strip(anchor) {
    const attn = el('div', { class: 'nd-attn hidden', role: 'status' });
    const cells = el('div', { class: 'nd-cells', role: 'list', 'aria-label': 'Nodes' });
    const note = el('p', { class: 'dim nd-note hidden', role: 'status' });
    const host = el('section', { class: 'nd-strip hidden', 'aria-label': 'Nodes' },
      el('div', { class: 'nd-strip-h' }, el('h2', { class: 'nd-h', text: 'Nodes' }), el('a', { class: 'nd-manage', href: '#/settings?sec=nodes', text: 'Manage' })), attn, cells, note);
    nhInsertAfter(anchor, host);
    cells.addEventListener('keydown', (e) => nhArrowKeys(e, cells));
    const list = nhList(cells, (c) => c.key, nhCellNode, nhCellSig);
    return { hosts: [host], paint() {
      const M = Nodes.M;
      const show = M.loaded || !!M.err;
      host.classList.toggle('hidden', !show || (M.loaded && !M.recs.length && !M.err));
      if (!show) return;
      const items = [nhLocalCell(), ...M.recs.map(nhRemoteCell)];
      list.update(items);
      for (const n of cells.children) n.setAttribute('role', 'listitem');
      const need = items.filter((c) => !c.local && c.needs > 0);
      attn.textContent = '';
      need.forEach((c, i) => { if (i) attn.append(' · '); attn.append(el('a', { class: 'nd-attn-a', href: c.href }, el('span', { class: 'glyph', 'aria-hidden': 'true', text: STATE_GLYPH.waiting }), ` ${c.needs} need you on `, el('b', { text: c.name }))); });
      attn.classList.toggle('hidden', !need.length);
      const skew = M.recs.filter((r) => r.skew_warn);
      let text = '';
      if (M.err) text = M.loaded ? `Could not refresh the nodes (${M.err}). Showing the last reading from ${nhSpan((Date.now() - M.recvAt) / 1000)} ago.` : `Could not read the nodes (${M.err}). Trying again.`;
      else if (skew.length) text = `The clock of ${skew.map(nhName).join(', ')} differs from this board's by more than 5 s: its ages may be off.`;
      setTextIfChanged(note, text);
      note.classList.toggle('hidden', !text);
    } };
  },

  inbox(anchor, opts) {
    const title = el('h2', { class: 'nd-h' });
    const more = el('a', { class: 'btn small hidden', href: '#/inbox' });
    const perms = el('div', { class: 'roster nd-list nd-perms' });
    const list = el('div', { class: 'roster nd-list' });
    const note = el('p', { class: 'dim nd-ro' });
    const host = el('section', { class: 'nd-inbox hidden', 'aria-label': 'On other nodes' }, el('div', { class: 'row head' }, title, more), perms, list, note);
    nhInsertAfter(anchor, host);
    const pl = nhList(perms, (c) => c.key, nsPermNode, nsPermSig);
    const kl = nhList(list, (s) => Ref.key({ tmux: s.tmux, node: s.node }), nhSessionRow, nhSessionSig);
    return { hosts: [host], paint() {
      const cards = Nodes.permCards();
      const real = cards.filter((c) => !c.stub);
      const stubs = cards.filter((c) => c.stub);
      const owned = new Set(real.map((c) => `${c.node}/${c.tmux}`));
      const rows = Nodes.sessions().filter((s) => s.needs_you && !owned.has(`${s.node}/${s.tmux}`));
      const total = real.length + rows.length;
      const lim = opts.limit > 0 ? opts.limit : Infinity;
      const localN = typeof opts.local === 'number' ? opts.local : nsLocalCount();
      const lead = localN === 0 ? real.find((c) => c.phase === 'ask' || c.phase === 'failed') : null;      // one filled primary per screen: Allow is it only when no local card leads
      const shownCards = real.slice(0, lim).map((c) => (c === lead ? { ...c, lead: true } : c));
      const shownRows = rows.slice(0, Math.max(0, lim - shownCards.length));
      pl.update(shownCards.concat(stubs));
      kl.update(shownRows);
      setTextIfChanged(title, `On other nodes (${total})`);
      setTextIfChanged(more, `Show all ${total}`);
      more.classList.toggle('hidden', !(opts.limit > 0 && total > shownCards.length + shownRows.length));
      setTextIfChanged(note, real.length ? 'Allow and Deny go to the node through this board. The node trusts this board to pass on a person\'s choice and records the name this board reports; it cannot check it.'
        : 'Open a session to reply to it, press keys in it or acknowledge it. A permission request can be answered here when the pair holds the permissions scope.');
      const any = total > 0 || stubs.length > 0;
      host.classList.toggle('hidden', !any);
      if (opts.none) opts.none.classList.toggle('hidden', !!opts.local || any);      // "Nothing needs you" is not true while another node's items wait
    } };
  },

  tasks(anchor) {
    const bar = el('div', { class: 'nd-tfilter hidden' });
    const list = el('div', { class: 'roster nd-list' });
    const empty = el('p', { class: 'dim nd-empty hidden' });
    const title = el('h2', { class: 'nd-h' });
    const after = el('section', { class: 'nd-tasks hidden', 'aria-label': 'Tasks on other nodes' }, title, list, empty, el('p', { class: 'dim nd-ro', text: 'Read only here: tasks are started and changed on their own node.' }));
    anchor.parentNode.insertBefore(bar, anchor);
    nhInsertAfter(anchor, after);
    const kl = nhList(list, (t) => Ref.key({ kind: 'task', node: t.node, id: t.id }), nhTaskRow, nhTaskSig);
    let barSig = '';
    let sel = null;
    try { const v = localStorage.getItem('ccboard:nodes:tfilter'); if (v) Nodes.M.tfilter = v; } catch (_) { /* storage may be unavailable */ }
    const pick = (v) => { Nodes.M.tfilter = v; try { localStorage.setItem('ccboard:nodes:tfilter', v); } catch (_) { /* storage may be unavailable */ } paint(); };
    function paint() {
      const M = Nodes.M;
      let f = M.tfilter;
      if (f !== 'all' && f !== 'local' && !M.by.has(f)) f = 'all';
      const opts = [['all', 'All nodes'], ['local', 'This node'], ...M.recs.map((r) => [r.handle, nhName(r)])];
      const asSelect = opts.length > 4;                         // more than four options is a native select, up to four a segmented control
      const sig = JSON.stringify(asSelect ? [opts] : [opts, f]);
      if (sig !== barSig) {
        barSig = sig;
        bar.textContent = '';
        sel = null;
        if (asSelect) {
          sel = el('select', { class: 'nd-tsel', 'aria-label': 'Node', onchange: () => pick(sel.value) });
          for (const [v, text] of opts) sel.append(el('option', { value: v, text }));
          bar.append(el('label', { class: 'nd-tlabel' }, el('span', { class: 'seg-k dim', text: 'Node' }), sel));
        } else {
        const seg = el('div', { class: 'seg-ctl', role: 'group', 'aria-label': 'Show tasks of' }, el('span', { class: 'seg-k dim', text: 'node' }));
        opts.forEach(([v, text], i) => seg.append(el('button', { class: 'seg-btn', type: 'button', 'data-v': v, 'aria-pressed': v === f ? 'true' : 'false', text, onclick: () => pick(v),
          onkeydown: (e) => {
            if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
            e.preventDefault();
            const j = (i + (e.key === 'ArrowRight' ? 1 : opts.length - 1)) % opts.length;
            const nb = seg.querySelectorAll('.seg-btn')[j];
            pick(opts[j][0]);
            const again = bar.querySelectorAll('.seg-btn')[j] || nb;
            if (again) again.focus();
          } })));
        bar.append(seg);
        }
      }
      if (sel) sel.value = f;
      bar.classList.toggle('hidden', !(M.loaded && M.recs.length));
      const remoteOnly = f !== 'all' && f !== 'local';
      anchor.classList.toggle('nd-hide', remoteOnly);
      if (typeof tasksPage !== 'undefined' && tasksPage.empty && tasksPage.empty.classList) tasksPage.empty.classList.toggle('nd-hide', remoteOnly);
      const rows = f === 'local' ? [] : Nodes.tasks(remoteOnly ? f : undefined);
      kl.update(rows);
      const rec = remoteOnly ? M.by.get(f) : null;
      setTextIfChanged(title, remoteOnly ? `Tasks on ${nhName(rec)} (${rows.length})` : `On other nodes (${rows.length})`);
      const none = remoteOnly && !rows.length;
      setTextIfChanged(empty, none ? `No task in the last reading of ${nhName(rec)}. Open its board to add one.` : '');
      empty.classList.toggle('hidden', !none);
      after.classList.toggle('hidden', !M.loaded || f === 'local' || (!rows.length && !none));
    }
    return { hosts: [bar, after], paint };
  },
};

/* ---------- the sidebar: "This node" as today, then one collapsed group per paired node ---------- */

const NH_RANK = { waiting: 0, errored: 1, working: 2, idle: 3, done: 4 };

function nhSbSessions(rec) {
  return nhSessions(rec).map((s) => ({ s, rank: s.needs_you ? -1 : ownKey(NH_RANK, s.state) ? NH_RANK[s.state] : 5 }))
    .sort((a, b) => a.rank - b.rank || String(b.s.since || '').localeCompare(String(a.s.since || '')))
    .map(({ s }) => ({ key: 's:' + Ref.key({ tmux: s.tmux, node: s.node }), kind: 'sess', tmux: s.tmux, node: s.node, name: s.session || s.tmux, repo: s.repo || '', state: s.state || 'unknown',
      agent: nhAgent(s.agent), needs: !!s.needs_you }));
}

function nhGroupNode() {
  const dot = el('span', { class: 'tn-dot hidden', role: 'img', 'aria-label': 'needs you', title: 'needs you' });
  const sg = el('span', { class: 'nd-sg', 'aria-hidden': 'true' });
  const go = el('a', { class: 'tn-go', tabindex: '-1', title: 'Open node page', 'aria-label': 'Open node page' }, ic('chevron-right'));
  const g = Shell.groupRow('proj-row nd-grp', 1, '', [dot]);
  g.row.insertBefore(sg, g.name);
  g.row.append(go);
  const kids = el('div', { class: 'tn-kids hidden', role: 'group' });
  const node = el('div', { class: 'tn proj nd-group' }, g.row, kids);
  node._r = { ...g, dot, sg, go, kids };
  return node;
}

function nhGroupPatch(node, it) {
  const r = node._r;
  const open = Shell.open.has(it.key);
  node.setAttribute('data-key', it.key);
  node.classList.toggle('open', open);
  node.classList.toggle('attn', it.needs > 0);
  r.row.setAttribute('aria-expanded', open ? 'true' : 'false');
  setText(r.name, it.name);
  setText(r.sg, it.view.glyph);
  setText(r.cnt, it.needs ? String(it.needs) : '');
  r.cnt.setAttribute('title', it.needs ? `${it.needs} need you on ${it.name}` : '');
  r.row.setAttribute('title', `${it.name}: ${it.view.word}`);
  r.dot.classList.toggle('hidden', !it.needs);
  r.go.setAttribute('href', it.href);
  r.kids.classList.toggle('hidden', !open);
  if (open) {
    const kids = it.sessions.slice(0, Nodes.SB_MAX);
    if (it.sessions.length > Nodes.SB_MAX) kids.push({ key: `more:${it.handle}`, kind: 'more', href: it.href, text: `more (${it.sessions.length - Nodes.SB_MAX})` });
    else if (!kids.length) kids.push({ key: `none:${it.handle}`, kind: 'note', text: it.view.status === 'online' ? 'no live session' : it.view.word });
    Shell.sync(r.kids, kids, (k) => (k.kind === 'more' ? el('a', { class: 'tn-row r-row nd-more', role: 'treeitem', tabindex: '-1', 'aria-level': '2' }) : Shell.kidNode(k)),
      (n, k) => { if (k.kind === 'more') { n.setAttribute('data-key', k.key); n.setAttribute('href', k.href); setText(n, k.text); } else Shell.patchKid(n, k); });
  } else if (r.kids.firstChild) r.kids.textContent = '';
}

function nhLabelNode() { return el('div', { class: 'sb-h nd-sbh', role: 'presentation' }); }

/* items: the tree's own (projects, older). Without a paired node's reading they come back unchanged. */
Nodes.sbItems = function (items) {
  const M = Nodes.M;
  if (!Nodes.enabled() || !M.recs.length) return items;
  const label = (key, text) => ({ key, make: nhLabelNode, patch: (n) => { n.setAttribute('data-key', key); setText(n, text); } });
  const groups = M.recs.map((rec) => {
    const c = Nodes.counts(rec);
    return { key: 'n:' + rec.handle, handle: rec.handle, name: nhName(rec), href: Ref.hash(Ref.node(rec.handle)) || '#/', view: Nodes.view(rec), needs: c.needs || 0, sessions: nhSbSessions(rec),
      make: nhGroupNode, patch: nhGroupPatch };
  });
  return [label('nodes-this', 'This node'), ...items, label('nodes-other', 'Other nodes'), ...groups];
};

/* ---------- keyboard and palette hooks ---------- */

Nodes.goFirst = function () {
  const r = Nodes.M.recs[0];
  if (!r) { if (typeof toast === 'function') toast('No node is paired yet. Pair one in Settings, Nodes.', { kind: 'info' }); return false; }
  if (typeof navigate === 'function') navigate(Ref.hash(Ref.node(r.handle))); else location.hash = Ref.hash(Ref.node(r.handle));
  return true;
};

if (typeof Keymap !== 'undefined' && Keymap && typeof Keymap.bindKey === 'function') {
  Keymap.bindKey('g n', () => { if (!Nodes.enabled()) return false; return Nodes.goFirst() || true; }, { help: 'Go to the first paired node', group: 'Go to' });
}

/* ---------- Settings, Nodes: the read model's line under a paired node's row ---------- */

/* The short lines under a Mac or WSL2 node (issue #154): what the card cannot do or describes. [] for any other platform. */
Nodes.platNotes = function (card, name) {
  const mac = name === 'Mac';
  return [mac && card && card.accounts && card.accounts.supported === false ? 'Saved logins are not available on a Mac. Normal login still works.' : '',
    mac ? 'A sleeping Mac shows as stale.' : name ? 'Load describes the distro and its VM, not Windows.' : ''].filter(Boolean);
};

/* Status with its age, the skew warning above 5 s, the agents and the accounts with their windows, from the node's card. null while the hub has no record of that handle. */
Nodes.settingsLine = function (handle) {
  const rec = Nodes.get(handle);
  if (!rec) return null;
  const v = Nodes.view(rec);
  const kids = [nhStatusLine(v)];
  if (v.age !== null && v.status !== 'online') kids.push(el('span', { class: 'dim', text: `last reading ${nhSpan(v.age)} ago` }));
  if (rec.skew_warn) kids.push(el('span', { class: 'warn', text: `clock differs by ${Math.round(Math.abs(rec.skew_ms || 0) / 1000)} s` }));
  const card = rec.card && typeof rec.card === 'object' ? rec.card : null;
  const agents = card && Array.isArray(card.agents) ? card.agents.filter((a) => a && typeof a.id === 'string') : [];
  if (agents.length) kids.push(el('span', { class: 'dim', text: agents.map((a) => `${a.id} ${a.installed ? (a.logged_in ? 'ready' : 'not logged in') : 'not installed'}`).join(', ') }));
  const plat = NodeView.platform(card);
  if (plat.name) kids.push(NodeView.chip(plat.name, '', '', 'Platform'), el('span', { class: 'dim', text: `load ${plat.load}` }), ...Nodes.platNotes(card, plat.name).map((t) => el('span', { class: 'dim', text: t })));
  const items = card && card.accounts && Array.isArray(card.accounts.items) ? card.accounts.items : [];
  if (items.length) {
    const word = (a) => `${a.agent} ${a.label || 'account'}${a.window && a.window.known && typeof a.window.pct === 'number' ? ` ${Math.round(a.window.pct)}%` : ''}${a.limited ? ' limit' : ''}`;
    kids.push(el('span', { class: 'dim', text: `${items.length} account${items.length === 1 ? '' : 's'}: ${items.slice(0, 4).map(word).join(', ')}` }));
  }
  return el('div', { class: 'nd-set-line', 'data-handle': handle }, ...kids);
};

/* ---------- steering a session and answering a permission request (issue #142) ----------

   Every call goes to this board's own origin: POST /api/nodes/<handle>/sessions/<tmux>/prompt | keys | ack, DELETE .../sessions/<tmux>, GET .../permissions and
   POST .../permissions/<id>/allow | deny. The hub checks the person (a signed-in login with the CSRF header), the pair's scope, the text and the key, and the peer checks it all again; this
   file checks the same things first only to refuse early with a plain sentence. Nothing here retries a write: a timeout is "could not confirm", and the draft stays in its box.

   The text a person types is sent as it is (CRLF becomes LF); a control, format or line-separator character is refused here and by both boards, never stripped. A key comes from the closed
   list Nodes.KEYS (C-c only after a second tap). A permission is answered with Allow or Deny only, bound to the request id the card shows. Every string of a node (a tool, a summary, a
   session name) is peer data and goes into the page as text.

   The permission list is read only for a node whose pair holds the `permissions` scope AND whose reading says a request is waiting (state.needs_you.permissions > 0), right after the
   hub poll that said so, and only while the page is visible: every read leaves an audit row on both boards, so a quiet node costs nothing. A board with no node paired runs none of it. */

Nodes.KEYS = ['Enter', 'Escape', 'Up', 'Down', 'Tab', 'y', 'n', '1', '2', '3', '4', '5', '6', '7', '8', '9'];      // plus C-c, which needs a second tap (Nodes.CONFIRM_KEY)
Nodes.CONFIRM_KEY = 'C-c';
Nodes.KEY_WORD = { Escape: 'Esc' };
Nodes.PROMPT_MAX = 8192;
Nodes.acked = new Map();                      // '<handle>/<tmux>' -> the reading (Nodes.M.rev) it was acknowledged in: needs_you is false until a newer reading says otherwise
Nodes.closed = new Map();                     // '<handle>/<tmux>' -> when it was closed: the session is left out of the lists for a short while (the next reading is the truth)
const NS_CLOSED_MS = 20000;
const NS_DONE_MS = 20000;
const NS_SHOW_MS = 10000;
const NS_CTRL = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u;
const NS_TEXT_BAD = 'That text has a character a terminal cannot take (a control, format or line-separator character). Tab and newline are fine. Remove it and send again.';
const NS_SLASH = 'Slash commands are not sent to another node. Type the words you want the session to read, or use the session on its own board.';
const NS_WIDE = 'That session runs with wider permissions than another node may use, or its mode cannot be read. Start it from its own board.';
const NS_ANSWERS = {
  answered: 'This request was already answered. Open terminal to see the current prompt.',
  expired: 'This request has expired. Open terminal to see the current prompt.',
  gone: 'This request is gone. Open terminal to see the current prompt.',
};

function nsClosed(handle, tmux, now) {
  const at = Nodes.closed.get(`${handle}/${tmux}`);
  return at !== undefined && now - at < NS_CLOSED_MS;
}

function nsSentence(t) {
  const s = String(t === undefined || t === null ? '' : t).replace(/\s+/g, ' ').trim().slice(0, 200);
  return s ? s.charAt(0).toUpperCase() + s.slice(1) + (/[.!?]$/.test(s) ? '' : '.') : '';
}

function nsQuiet(text, kind) { if (typeof toast === 'function') toast(String(text), { kind: kind || 'info' }); }
Nodes.say = nsQuiet;

/* A failed relay call as one plain sentence. err.body is the hub's {error, reason, node, age, code, retry}: the reason picks the words; the hub's own text (already cleaned) fills the
   cases that carry something only the peer knows (why a session is busy, why it cannot be steered). o.what: prompt | keys | ack | close | perms | perm. */
Nodes.errText = function (err, handle, o) {
  const what = (o && o.what) || 'prompt';
  const perm = what === 'perm' || what === 'perms';
  const b = err && err.body && typeof err.body === 'object' ? err.body : {};
  const name = Nodes.nameOf(handle);
  const own = typeof b.error === 'string' ? b.error : '';
  const reason = typeof b.reason === 'string' ? b.reason : '';
  const unsure = `Could not confirm whether ${name} got this. Check the terminal before answering again.`;
  if (!reason && (!err || !err.status || err.status >= 502)) return what === 'perms' ? `${name} could not be reached` : unsure;     // no answer at all, or a proxy's: a write may have gone through
  switch (reason) {
    case 'unconfirmed': return unsure;
    case 'offline': return typeof b.age === 'number' ? `${name} is offline, last seen ${nhAgo(b.age)} ago` : `${name} has not answered yet`;
    case 'scope': return /^needs the /.test(own) ? own.slice(0, 200) : `needs the ${perm ? 'permissions' : 'sessions'} scope on ${name}`;
    case 'needs_repair': return `${name} no longer takes this board's token: re-pair it in Settings, Nodes`;
    case 'unpaired': return `${name} answers as another node or no longer knows this board: remove it and pair it again`;
    case 'not_read_yet': return `${name} has not been read yet: wait a few seconds and try again`;
    case 'rate_limited': return perm ? 'Too many requests in a minute. Try again in a moment.' : 'Too many prompts in a minute for this session. Try again in a moment.';
    case 'busy': {
      const retry = typeof b.retry === 'number' && b.retry >= 0 && b.retry <= 86400 ? Math.max(1, Math.floor(b.retry)) : null;
      return `${nsSentence(own) || 'The session is busy.'} ${retry === null ? 'Waiting will not help.' : `Try again in ${nhAgo(retry)}.`}`;
    }
    case 'answered': case 'expired': case 'gone': return NS_ANSWERS[reason];
    case 'not_found': return perm ? NS_ANSWERS.gone : `That session is gone from ${name}.`;
    case 'refused': return nsSentence(own) || NS_WIDE;
    case 'too_large': return 'The text is over 8 KB. Shorten it and send again.';
    case 'invalid': return what === 'prompt' ? (own && !/control|format|line.?sep/i.test(own) ? nsSentence(own) : NS_TEXT_BAD) : what === 'keys' ? 'That key is not on the list this board may send.' : 'That is not an answer a node takes.';
    case 'bad_answer': return `${name} sent an answer this board could not read`;
    case 'unreachable': case 'peer_error': case 'bad_address': case 'redirect': return `${name} could not be reached`;
    default: return nsSentence(own) || (err && err.message ? nsSentence(err.message) : 'The request failed.');
  }
};

/* The text to send: CRLF to LF, not empty, up to 8192 characters, no control, format or line-separator character (tab and newline are text). {ok, text} or {ok: false, why}. */
Nodes.promptCheck = function (text) {
  const t = String(text === undefined || text === null ? '' : text).replace(/\r\n?/g, '\n');
  if (!t.trim()) return { ok: false, why: 'Nothing to send.' };
  if (/^\s*\//.test(t)) return { ok: false, why: NS_SLASH };                                        // slash commands are not relayed in v1
  const n = Array.from(t).length;
  if (n > Nodes.PROMPT_MAX) return { ok: false, why: `The text is ${n} characters; a prompt can be up to 8 KB (${Nodes.PROMPT_MAX} characters). Shorten it and send again.` };
  if (NS_CTRL.test(t.replace(/[\t\n]/g, ''))) return { ok: false, why: NS_TEXT_BAD };
  return { ok: true, text: t };
};

/* The checks made before a call: a handle and a session name the grammar allows, and Nodes.can for the scope. null when the call may go. */
function nsGate(handle, tmux, scope) {
  if (!Ref.isHandle(handle) || (tmux !== null && !Ref.isTmux(tmux))) return { ok: false, local: true, text: 'That is not a session this board can reach.' };
  const c = Nodes.can(Nodes.get(handle), scope);
  return c.ok ? null : { ok: false, local: true, text: c.why };
}

function nsPath(handle, tmux, tail) { return `/api/nodes/${encodeURIComponent(handle)}${tmux === null ? '' : '/sessions/' + encodeURIComponent(tmux)}${tail || ''}`; }

function nsFail(e, handle, what) {
  const b = e && e.body && typeof e.body === 'object' ? e.body : {};
  const out = { ok: false, text: Nodes.errText(e, handle, { what }), reason: typeof b.reason === 'string' ? b.reason : '', status: e && e.status ? e.status : 0 };
  if (out.reason === 'busy') out.busy = { code: typeof b.code === 'string' ? b.code : '', retry: typeof b.retry === 'number' ? b.retry : null };
  return out;
}

/* Each of these resolves to {ok, text?, data?, ...} and never throws or retries. */
Nodes.sendPrompt = async function (handle, tmux, text, o) {
  const gate = nsGate(handle, tmux, 'sessions');
  if (gate) return gate;
  const chk = Nodes.promptCheck(text);
  if (!chk.ok) return { ok: false, local: true, text: chk.why };
  try {
    const r = await api('POST', nsPath(handle, tmux, '/prompt'), { text: chk.text, queue: !!(o && o.queue) });
    const d = r && r.data && typeof r.data === 'object' ? r.data : {};
    if (d.ok !== true) return { ok: false, text: Nodes.errText({ status: 502 }, handle, { what: 'prompt' }), reason: 'unconfirmed' };
    return { ok: true, pasted: d.pasted === true, queued: d.queued === true };
  } catch (e) { return nsFail(e, handle, 'prompt'); }
};

Nodes.sendKey = async function (handle, tmux, key, confirm) {
  const gate = nsGate(handle, tmux, 'sessions');
  if (gate) return gate;
  const risky = key === Nodes.CONFIRM_KEY;
  if (!(Nodes.KEYS.includes(key) || risky)) return { ok: false, local: true, text: 'That key is not on the list this board may send.' };
  if (risky && confirm !== true) return { ok: false, local: true, text: 'C-c needs a second tap to confirm.' };
  try {
    const r = await api('POST', nsPath(handle, tmux, '/keys'), risky ? { key, confirm: true } : { key });
    if (!r || !r.data || r.data.ok !== true) return { ok: false, text: Nodes.errText({ status: 502 }, handle, { what: 'keys' }), reason: 'unconfirmed' };
    return { ok: true, key };
  } catch (e) { return nsFail(e, handle, 'keys'); }
};

/* Ack paints at once (the session stops asking for you until a newer reading says otherwise) and puts the old state back when the node refuses. */
Nodes.ackSession = async function (handle, tmux) {
  const gate = nsGate(handle, tmux, 'sessions');
  if (gate) return gate;
  const key = `${handle}/${tmux}`;
  Nodes.acked.set(key, Nodes.M.rev);
  Nodes.changed(false);
  try {
    const r = await api('POST', nsPath(handle, tmux, '/ack'));
    if (!r || !r.data || typeof r.data.acked !== 'string') throw Object.assign(new Error('no answer'), { status: 502 });
    return { ok: true };
  } catch (e) {
    Nodes.acked.delete(key);
    Nodes.changed(false);
    return nsFail(e, handle, 'ack');
  }
};

/* Close ends the session on that node only. The session leaves the lists once the node confirmed it (a pending line shows until then). */
Nodes.closeSession = async function (handle, tmux) {
  const gate = nsGate(handle, tmux, 'sessions');
  if (gate) return gate;
  try {
    const r = await api('DELETE', nsPath(handle, tmux, ''));
    if (!r || !r.data || typeof r.data.killed !== 'string') throw Object.assign(new Error('no answer'), { status: 502 });
    Nodes.closed.set(`${handle}/${tmux}`, Date.now());
    Nodes.changed(false);
    return { ok: true };
  } catch (e) { return nsFail(e, handle, 'close'); }
};

/* ---------- the permission requests of the nodes ---------- */

/* P.by: handle -> {list: [{id, tmux, tool, summary, since}], at, err}; P.st: '<handle>/<id>' -> {phase: sending | failed | closed, decision, text, at}; P.done: ids answered here, hidden
   from the list for a while (a reading made before the answer must not bring the card back); P.busy: handles being read. */
Nodes.P = { by: new Map(), st: new Map(), done: new Map(), busy: new Set() };

Nodes.steerClear = function () {
  const P = Nodes.P;
  P.by.clear(); P.st.clear(); P.done.clear(); P.busy.clear();
  Nodes.acked.clear(); Nodes.closed.clear();
};

function nsKey(handle, id) { return `${handle}/${id}`; }
function nsWaiting(rec) { return Math.max(0, Math.floor(Number(rec && rec.state && rec.state.needs_you && rec.state.needs_you.permissions) || 0)); }

/* The list a node answered, rebuilt field by field: a request needs a positive id and a session name; the rest is cut. */
function nsCleanPerms(body) {
  const rows = body && Array.isArray(body.permissions) ? body.permissions.slice(0, 50) : [];
  const out = [];
  for (const r of rows) {
    if (!r || typeof r !== 'object' || !Number.isSafeInteger(r.id) || r.id < 1 || !Ref.isTmux(r.tmux)) continue;
    out.push({ id: r.id, tmux: r.tmux, tool: typeof r.tool === 'string' && r.tool ? r.tool.slice(0, 60) : 'tool', summary: typeof r.summary === 'string' ? r.summary.slice(0, 300) : '',
      since: typeof r.since === 'string' && !Number.isNaN(Date.parse(r.since)) ? r.since : null });
  }
  return out;
}

async function nsPermsFetch(rec) {
  const P = Nodes.P;
  const h = rec.handle;
  P.busy.add(h);
  try {
    const r = await api('GET', `/api/nodes/${encodeURIComponent(h)}/permissions`);
    if (!r || !r.data || !Array.isArray(r.data.permissions)) throw Object.assign(new Error('no list'), { status: 502, body: { reason: 'bad_answer' } });
    P.by.set(h, { list: nsCleanPerms(r.data), tried: Date.now(), n: nsWaiting(rec), err: null });
  } catch (e) {
    const old = P.by.get(h);
    P.by.set(h, { list: old ? old.list : [], tried: Date.now(), n: nsWaiting(rec), err: Nodes.errText(e, h, { what: 'perms' }) });
  } finally { P.busy.delete(h); }
  Nodes.changed(false);
}

/* Called after every hub poll: read the list of each node that may be asked and has a request waiting. Nothing while the view is off or the page is hidden. Every read leaves an
   audit row on both boards, so a list already held is read again only when the reading's count changed since that read, or after NS_PERMS_MS (a request replaced by another keeps the
   count); a failed read is tried again after the same time. */
const NS_PERMS_MS = 15000;
Nodes.permsSync = function () {
  const P = Nodes.P;
  if (!Nodes.enabled()) return;
  const hidden = typeof document !== 'undefined' && document.hidden === true;
  const now = Date.now();
  const keep = new Set();
  for (const rec of Nodes.M.recs) {
    const n = nsWaiting(rec);
    if (!Nodes.can(rec, 'permissions').ok || n < 1) continue;
    keep.add(rec.handle);
    const got = P.by.get(rec.handle);
    const due = !got || now - got.tried >= NS_PERMS_MS || (!got.err && got.n !== n);
    if (!hidden && due && !P.busy.has(rec.handle)) nsPermsFetch(rec);
  }
  for (const h of Array.from(P.by.keys())) if (!keep.has(h)) P.by.delete(h);
  const known = new Set(Nodes.M.recs.map((r) => r.handle));
  for (const k of Array.from(P.st.keys())) if (!known.has(k.split('/')[0])) P.st.delete(k);
  for (const k of Array.from(P.done.keys())) if (!known.has(k.split('/')[0])) P.done.delete(k);
};

/* The requests to show, oldest first, then one notice per node whose request cannot be shown ({stub: true}: no scope, offline, not read yet). A card has its phase:
   ask | sending | failed | closed. */
Nodes.permCards = function () {
  const P = Nodes.P;
  const now = Date.now();
  for (const [k, v] of Array.from(P.done)) if (now - v >= NS_DONE_MS) P.done.delete(k);
  for (const [k, v] of Array.from(P.st)) if (v.phase === 'closed' && now - v.at >= NS_SHOW_MS) { P.st.delete(k); P.done.set(k, now); }
  const cards = [];
  const stubs = [];
  for (const rec of Nodes.M.recs) {
    const n = nsWaiting(rec);
    if (n < 1) continue;
    const c = Nodes.can(rec, 'permissions');
    const got = P.by.get(rec.handle);
    if (c.ok && got && !got.err) {
      for (const p of got.list) {
        const key = nsKey(rec.handle, p.id);
        if (P.done.has(key)) continue;
        const st = P.st.get(key);
        cards.push({ ...p, node: rec.handle, key, phase: st ? st.phase : 'ask', decision: st ? st.decision : '', text: st ? st.text : '' });
      }
    } else stubs.push({ stub: true, node: rec.handle, key: `stub:${rec.handle}`, count: n, why: !c.ok ? c.why : got && got.err ? got.err : `reading the requests on ${nhName(rec)}` });
  }
  const at = (c) => (c.since ? Date.parse(c.since) : Infinity);
  cards.sort((a, b) => at(a) - at(b) || a.id - b.id);
  return cards.concat(stubs);
};

/* The request Allow and Deny act on from the palette and the keys: the first card that can still be answered. */
Nodes.permLead = function () { return Nodes.permCards().find((c) => !c.stub && (c.phase === 'ask' || c.phase === 'failed')) || null; };
Nodes.permFor = function (handle, tmux) { return Nodes.permCards().find((c) => !c.stub && c.node === handle && c.tmux === tmux && (c.phase === 'ask' || c.phase === 'failed')) || null; };

/* Allow or Deny one request. The card turns into a pending line at once (the buttons are gone, so a double tap cannot answer twice); "Allowed on <node>" appears only when the node
   confirmed; a refusal puts the card back with the sentence (an answered, expired or gone request stays as a sentence without buttons, and goes by itself). */
Nodes.permAnswer = async function (card, decision) {
  const P = Nodes.P;
  if (!card || card.stub || !(decision === 'allow' || decision === 'deny') || !Ref.isHandle(card.node) || !Number.isSafeInteger(card.id) || card.id < 1) return { ok: false, local: true, text: 'That is not a request this board can answer.' };
  const key = nsKey(card.node, card.id);
  const cur = P.st.get(key);
  if (cur && (cur.phase === 'sending' || cur.phase === 'closed')) return { ok: false, local: true, text: 'That request is already being answered.' };
  const name = Nodes.nameOf(card.node);
  const gate = nsGate(card.node, null, 'permissions');
  if (gate) { nsQuiet(gate.text, 'warn'); return gate; }
  P.st.set(key, { phase: 'sending', decision, text: '', at: Date.now() });
  Nodes.changed(false);
  try {
    const r = await api('POST', `/api/nodes/${encodeURIComponent(card.node)}/permissions/${card.id}/${decision}`);
    if (!r || !r.data || r.data.ok !== true || r.data.decision !== decision) throw Object.assign(new Error('no answer'), { status: 502 });
    P.st.delete(key);
    P.done.set(key, Date.now());
    nsQuiet(decision === 'allow' ? `Allowed on ${name}` : `Denied on ${name}`, decision === 'allow' ? 'ok' : 'warn');
    Nodes.changed(false);
    return { ok: true };
  } catch (e) {
    const f = nsFail(e, card.node, 'perm');
    const final = f.reason === 'answered' || f.reason === 'expired' || f.reason === 'gone' || f.reason === 'not_found';
    P.st.set(key, { phase: final ? 'closed' : 'failed', decision: '', text: f.text, at: Date.now() });
    nsQuiet(f.text, final ? 'warn' : 'bad');
    Nodes.changed(false);
    return f;
  }
};

/* ---------- the cards ---------- */

function nsLocalCount() {
  try { return typeof Inbox !== 'undefined' && typeof state !== 'undefined' && state ? Inbox.items(state).length : 0; } catch (_) { return 0; }
}

function nsSessionName(handle, tmux) {
  const row = nhSessions(Nodes.get(handle)).find((s) => s.tmux === tmux);
  return String((row && row.session) || tmux);
}

function nsPermSig(c) { return JSON.stringify([c.key, c.stub, c.count, c.why, c.tool, c.summary, c.since && Math.floor(Date.parse(c.since) / 60000), c.phase, c.decision, c.text, !!c.lead, c.tmux, Math.floor(Date.now() / 60000), Nodes.sig({ node: c.node })]); }

/* The notice of a node whose requests cannot be answered from here: how many wait, why the buttons are off, and a way to answer on the node's own board. */
function nsPermStub(c) {
  const rec = Nodes.get(c.node);
  const name = rec ? nhName(rec) : c.node;
  const open = rec ? nhOpenLink(`Open on ${name}`, rec, '', 'btn small') : null;
  return el('div', { class: 'inbox-card nd-pcard nd-pstub attn', 'data-key': c.key },
    el('div', { class: 'ib-lead' }, el('span', { class: 'ib-kind', text: 'permission' }), el('div', { class: 'ib-ctx', text: `${c.count} permission request${c.count === 1 ? ' is' : 's are'} waiting on ${name}.` })),
    el('div', { class: 'ib-sub' }, Nodes.chip(c.node)),
    Nodes.acts(Nodes.off('Allow', c.why), Nodes.off('Deny', c.why), open));
}

function nsPermNode(c) {
  if (c.stub) return nsPermStub(c);
  const rec = Nodes.get(c.node);
  const name = rec ? nhName(rec) : c.node;
  const who = nsSessionName(c.node, c.tmux);
  const href = Ref.hash({ tmux: c.tmux, node: c.node });
  const link = el(href ? 'a' : 'span', { class: 'ib-name', href: href || null, text: who });
  const sub = el('div', { class: 'ib-sub' }, el('span', { class: 'ib-glyphs' }, stateGlyph('waiting')), link, Nodes.chip(c.node), c.since ? el('span', { class: 'dim', text: `waiting ${nhAgeText(c.since)}` }) : null);
  if (c.phase === 'sending') {
    return el('div', { class: 'inbox-card nd-pcard pending', role: 'status', 'data-key': c.key },
      el('div', { class: 'ib-ctx', text: `${c.decision === 'deny' ? 'Denying' : 'Allowing'} on ${name}…` }), sub);
  }
  const dup = !!c.tool && String(c.summary).toLowerCase().startsWith(c.tool.toLowerCase());
  const open = rec ? nhOpenLink(`Open on ${name}`, rec, href && Ref.isTmux(c.tmux) ? `#/s/${c.tmux}` : '', 'btn small') : null;
  const live = c.phase === 'ask' || c.phase === 'failed';
  const acts = el('div', { class: 'ib-actions' });
  if (live) {
    acts.append(el('span', { class: 'actions perm-btns' },
      el('button', { class: c.lead ? 'primary small' : 'primary tinted small', type: 'button', 'data-act': 'allow', title: `Allow this on ${name}`, onclick: () => Nodes.permAnswer(c, 'allow'), text: 'Allow' }),
      el('button', { class: 'small', type: 'button', 'data-act': 'deny', title: `Deny this on ${name}`, onclick: () => Nodes.permAnswer(c, 'deny'), text: 'Deny' })));
  }
  if (open) acts.append(open);
  return el('div', { class: 'inbox-card nd-pcard attn kind-permission' + (c.lead ? ' lead' : '') + (c.phase === 'closed' ? ' closed' : ''), 'data-key': c.key },
    el('div', { class: 'ib-lead' }, el('span', { class: 'ib-kind', text: 'permission' }),
      el('div', { class: 'ib-ctx mono', title: c.summary, text: c.summary || c.tool }),
      el('div', { class: 'ib-extra' }, c.tool && !dup ? el('span', { class: 'ib-note dim', text: c.tool }) : null)),
    sub,
    c.text ? el('p', { class: 'nd-perr ' + (c.phase === 'closed' ? 'warn' : 'bad'), role: 'alert', text: c.text }) : null,
    acts);
}

/* ---------- keys on a remote session's peek ---------- */

if (typeof Keymap !== 'undefined' && Keymap && typeof Keymap.bindKey === 'function') {
  /* The peek (pages/node.js) registers itself in Nodes.peek while a remote session is open. These are the keys the local peek has (Pages.target is empty on #/n/ pages), same letters:
     r focuses the send box, a acknowledges, y and d allow or deny the request waiting on that session, o opens the session on its own board. No help text: the help already lists them. */
  const on = () => !!Nodes.peek;
  const run = (name) => () => { const p = Nodes.peek; return p && typeof p[name] === 'function' ? p[name]() : false; };
  Keymap.bindKey('r', run('focus'), { when: on });
  Keymap.bindKey('a', run('ack'), { when: on });
  Keymap.bindKey('y', run('allow'), { when: on });
  Keymap.bindKey('d', run('deny'), { when: on });
  Keymap.bindKey('o', run('open'), { when: on });
}

/* Run fn(peek) as soon as the peek of this session has registered (a palette row navigates to it first): now, or within about half a second. */
Nodes.withPeek = function (handle, tmux, fn, tries) {
  const p = Nodes.peek;
  if (p && p.handle === handle && p.tmux === tmux) { fn(p); return true; }
  const left = typeof tries === 'number' ? tries : 8;
  if (left <= 0 || typeof setTimeout !== 'function') return false;
  setTimeout(() => Nodes.withPeek(handle, tmux, fn, left - 1), 60);
  return true;
};

/* ---------- demo ---------- */

/* GET /api/nodes/state in demo mode (core.js demoApi): the fixture's `hub` records, as the board would answer them, with what was done to them on this page (an Ack, a Kill session,
   a permission answered) left out the way the next reading would. */
const demoSteerMade = { acked: new Set(), closed: new Set(), answered: new Set() };
function demoSteered(r) {
  const st = r && r.state;
  if (!st) return r;
  const m = demoSteerMade;
  const sessions = (Array.isArray(st.sessions) ? st.sessions : []).filter((s) => !m.closed.has(`${r.handle}/${s.tmux}`)).map((s) => (m.acked.has(`${r.handle}/${s.tmux}`) ? { ...s, needs_you: false } : s));
  const done = Array.from(m.answered).filter((k) => k.startsWith(`${r.handle}/`)).length;
  return { ...r, state: { ...st, sessions, needs_you: { ...(st.needs_you || {}), permissions: Math.max(0, Number(st.needs_you && st.needs_you.permissions || 0) - done) } } };
}
function demoHubState(data, at) {
  const recs = Array.isArray(data && data.hub) ? data.hub : [];
  const m = demoSteerMade;
  return { nodes: m.acked.size || m.closed.size || m.answered.size ? recs.map(demoSteered) : recs, at };
}

/* The relay's answers in demo mode (core.js demoApi hands every /api/nodes/<handle>/... path here; nothing leaves the page): GET .../agents is the node's agents schema and GET
   .../sessions/<name>/pane its tail, both from demo/nodes.json (`agents` per handle, `tails` per '<handle>/<tmux>'); a POST to .../tasks, .../tasks/<id>/dispatch or .../sessions is
   answered the way the hub answers a start: {node, age, data: {ref, id, ...}}. The refusals are the real ones' reasons: a node that is offline, a scope the pair lacks, a repo the node
   does not list (the demo's way to see each: old-laptop is offline, alice-mac holds read only, a repo that is not in the reading). A task started with effort max carries the
   limit warning. The ids count up from 100 for the life of the page. */
let demoRelaySeq = 100;
function demoRelayFail(status, reason, error, extra) {
  const e = demoError(status, error);
  e.body = { error, reason, ...(extra || {}) };
  return e;
}
function demoRelayCan(rec, scope) {
  const h = rec.handle;
  if (!(rec.scopes || []).includes(scope)) throw demoRelayFail(409, 'scope', `needs the ${scope} scope on ${rec.name}`, { node: h });
  if (rec.status === 'offline') throw demoRelayFail(503, 'offline', `${rec.name} is offline; nothing was sent`, { node: h, age: rec.age_s });
  if (rec.status === 'unauthorized') throw demoRelayFail(409, 'needs_repair', `${rec.name} no longer takes this board's token: re-pair`, { node: h });
}
function demoRelayRead(data, bare) {
  const m = /^\/api\/nodes\/([^/]+)\/(agents|permissions|sessions\/([^/]+)\/pane)$/.exec(bare);
  if (!m) return {};
  const h = decodeURIComponent(m[1]);
  const rec = (data && Array.isArray(data.hub) ? data.hub : []).find((r) => r.handle === h);
  if (!rec) throw demoRelayFail(404, 'unknown_node', 'no paired node has that handle');
  if (m[2] === 'permissions') {                                                  // the pending requests (the fixture's extra demo_answer says how an answer to it goes; a node never sends it)
    demoRelayCan(rec, 'permissions');
    const left = ((data.permissions && data.permissions[h]) || []).filter((p) => !demoSteerMade.answered.has(`${h}/${p.id}`)).map(({ demo_answer: _a, ...p }) => p);
    return { node: h, age: 0, data: { permissions: left } };
  }
  if (m[2] === 'agents') return { node: h, age: 0, data: { agents: (data.agents && data.agents[h] && data.agents[h].agents) || [] } };
  const tmux = decodeURIComponent(m[3]);
  const tail = (data.tails && data.tails[`${h}/${tmux}`]) || (data.tails && data.tails.default) || [];
  return { node: h, age: 0, data: { name: tmux, lines: tail, cap: 40 } };
}
/* The steering rows in demo mode (issue #142), the way the hub answers them. Where each refusal comes from: alice-mac holds `read` only (needs the sessions or permissions scope),
   old-laptop is offline, a session of the reading that is working is busy unless `queue` is set, the fixture's `steer` map (key '<handle>/<tmux>') makes shop--api--s3 refuse as a
   session with wider permissions and infra--deploy--s2 time out on a prompt ("could not confirm") and be gone on a Close; a permission's `demo_answer` (answered, expired, gone) says how
   its answer is refused. Nothing leaves the page; the ids and names are made up. */
function demoSteerWrite(method, path, body, data) {
  const hubOf = (h) => (data && Array.isArray(data.hub) ? data.hub : []).find((r) => r.handle === h);
  const b = body && typeof body === 'object' ? body : {};
  const pm = /^\/api\/nodes\/([^/]+)\/permissions\/(\d+)\/([a-z]+)$/.exec(path);
  if (pm && method === 'POST') {
    const h = decodeURIComponent(pm[1]);
    const rec = hubOf(h);
    if (!rec) throw demoRelayFail(404, 'unknown_node', 'no paired node has that handle');
    demoRelayCan(rec, 'permissions');
    if (pm[3] !== 'allow' && pm[3] !== 'deny') throw demoRelayFail(422, 'invalid', 'decision is not valid', { node: h });
    const p = ((data.permissions && data.permissions[h]) || []).find((x) => String(x.id) === pm[2]);
    const key = `${h}/${pm[2]}`;
    const out = !p || demoSteerMade.answered.has(key) ? 'gone' : p.demo_answer || 'ok';
    demoSteerMade.answered.add(key);
    if (out !== 'ok') {
      const words = { answered: 'This request was already answered. Open terminal to see the current prompt.', expired: 'This request has expired. Open terminal to see the current prompt.', gone: 'This request is gone. Open terminal to see the current prompt.' };
      throw demoRelayFail(out === 'gone' ? 404 : 409, out, words[out], { node: h });
    }
    return { node: h, age: 0, data: { ok: true, id: Number(pm[2]), decision: pm[3] } };
  }
  const sm = /^\/api\/nodes\/([^/]+)\/sessions\/([^/]+?)(?:\/(prompt|keys|ack))?$/.exec(path);
  if (!sm || !((method === 'POST' && sm[3]) || (method === 'DELETE' && !sm[3]))) return null;
  const h = decodeURIComponent(sm[1]);
  const tmux = decodeURIComponent(sm[2]);
  const rec = hubOf(h);
  if (!rec) throw demoRelayFail(404, 'unknown_node', 'no paired node has that handle');
  demoRelayCan(rec, 'sessions');
  const key = `${h}/${tmux}`;
  const row = ((rec.state && rec.state.sessions) || []).find((s) => s.tmux === tmux);
  const rule = (data.steer && data.steer[key]) || {};
  if (!row || demoSteerMade.closed.has(key)) throw demoRelayFail(404, 'not_found', 'no such session', { node: h });
  if (sm[3] === 'ack') { demoSteerMade.acked.add(key); return { node: h, age: 0, data: { acked: tmux } }; }
  if (!sm[3]) {
    demoSteerMade.closed.add(key);
    if (rule.close === 'gone') throw demoRelayFail(404, 'not_found', 'no such session', { node: h });
    return { node: h, age: 0, data: { killed: tmux } };
  }
  if (rule.refuse === 'wide') throw demoRelayFail(409, 'refused', 'that session runs with wider permissions than another node may use, or its mode cannot be read; start it from its own board', { node: h });
  if (sm[3] === 'keys') {
    const ok = ['Enter', 'Escape', 'Up', 'Down', 'Tab', 'y', 'n', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'C-c'];
    if (!ok.includes(b.key) || (b.key === 'C-c') !== (b.confirm === true)) throw demoRelayFail(422, 'invalid', 'key is not valid', { node: h });
    return { node: h, age: 0, data: { ok: true, key: b.key } };
  }
  const text = typeof b.text === 'string' ? b.text : '';
  if (!text.trim()) throw demoRelayFail(422, 'invalid', 'text: text is empty', { node: h });
  if (Array.from(text).length > 8192) throw demoRelayFail(413, 'too_large', 'the text is over 8 KB', { node: h });
  if (rule.prompt === 'unconfirmed') throw demoRelayFail(504, 'unconfirmed', `${rec.name} did not answer in 8 s: it could not be confirmed and it was not retried`, { node: h });
  if (row.state === 'working' && b.queue !== true) throw demoRelayFail(409, 'busy', 'the session is working', { node: h, code: 'working', retry: 8, state: 'working', wait_kind: null });
  return { node: h, age: 0, data: { ok: true, pasted: row.state !== 'working', queued: row.state === 'working' } };
}
function demoRelayWrite(method, path, body, data) {
  const steered = demoSteerWrite(method, path, body, data);
  if (steered) return steered;
  const m = /^\/api\/nodes\/([^/]+)\/(tasks|sessions|tasks\/(\d+)\/dispatch)$/.exec(path);
  if (!m || method !== 'POST') return null;
  const h = decodeURIComponent(m[1]);
  const b = body && typeof body === 'object' ? body : {};
  const rec = (data && Array.isArray(data.hub) ? data.hub : []).find((r) => r.handle === h);
  if (!rec) throw demoRelayFail(404, 'unknown_node', 'no paired node has that handle');
  const scope = m[2] === 'sessions' ? 'sessions' : 'tasks';
  if (!(rec.scopes || []).includes(scope)) throw demoRelayFail(409, 'scope', `needs the ${scope} scope on ${rec.name}`, { node: h });
  if (b.mode === 'session' && !(rec.scopes || []).includes('sessions')) throw demoRelayFail(409, 'scope', `needs the sessions scope on ${rec.name}`, { node: h });
  if (rec.status === 'offline') throw demoRelayFail(503, 'offline', `${rec.name} is offline; nothing was sent`, { node: h, age: rec.age_s });
  if (rec.status === 'unauthorized') throw demoRelayFail(409, 'needs_repair', `${rec.name} no longer takes this board's token: re-pair`, { node: h });
  const now = new Date().toISOString();
  const slug = (t) => String(t || 'task').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 30) || 'task';
  if (m[2] === 'sessions') {
    const p = (rec.state.projects || []).find((x) => x.name === b.project);
    if (!p || !(p.repos || []).some((r) => r.name === b.repo)) throw demoRelayFail(404, 'repo_missing', `${b.repo} is not on ${rec.name}. Nothing was created.`, { node: h });
    demoRelaySeq += 1;
    const tmux = `${b.project}--${b.repo}--${b.name || `s${demoRelaySeq}`}`;
    return { node: h, age: 0, data: { ref: `${h}/${tmux}`, tmux, agent: b.agent || 'claude', project: b.project, repo: b.repo } };
  }
  const warn = b.effort === 'max' ? { limit_warning: { kind: '5h', resets_at: Math.floor(Date.now() / 1000) + 5400, pct: 91 } } : {};
  if (m[3]) {
    const t = (rec.state.tasks || []).find((x) => String(x.id) === m[3]);
    if (!t) throw demoRelayFail(404, 'not_found', 'no such task', { node: h });
    const tmux = b.mode === 'session' ? b.session : `${t.project}--${t.repo}--t-${slug(t.title)}`;
    const row = { id: t.id, title: t.title, phase: 'running', agent: b.agent || t.agent, project: t.project, repo: t.repo, branch: `task/${slug(t.title)}`, tmux, issue_ref: t.issue_ref || null, updated_at: now };
    return { node: h, age: 0, data: { ref: `${h}:${t.id}`, id: t.id, slug: slug(t.title), tmux, branch: row.branch, phase: 'running', task: row, ...(b.mode === 'session' ? { pasted: true, queued: false, held: false } : {}), ...warn } };
  }
  const p = (rec.state.projects || []).find((x) => x.name === b.project);
  if (!p || !(p.repos || []).some((r) => r.name === b.repo)) throw demoRelayFail(404, 'repo_missing', `${b.repo} is not on ${rec.name}. Nothing was created.`, { node: h });
  demoRelaySeq += 1;
  const now1 = b.when !== 'later';
  const sl = slug(b.title);
  const tmux = now1 ? `${b.project}--${b.repo}--t-${sl}` : null;
  const row = { id: demoRelaySeq, title: b.title, phase: now1 ? 'running' : 'backlog', agent: b.agent || 'claude', project: b.project, repo: b.repo, branch: now1 ? `task/${sl}` : '', tmux, issue_ref: b.issue_ref ? `#${String(b.issue_ref).split('#')[1]}` : null, updated_at: now };
  return { node: h, age: 0, data: { ref: `${h}:${demoRelaySeq}`, id: demoRelaySeq, slug: sl, tmux, branch: row.branch, phase: row.phase, task: row, ...(now1 ? warn : {}) } };
}

Nodes.ready = true;
