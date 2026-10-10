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
  return rows.filter((s) => s && typeof s.tmux === 'string').map((s) => ({ ...s, node: rec.handle }));
}
function nhTasks(rec) {
  const rows = rec && rec.state && Array.isArray(rec.state.tasks) ? rec.state.tasks : [];
  return rows.filter((t) => t && (typeof t.id === 'number' || /^\d+$/.test(String(t.id)))).map((t) => ({ ...t, node: rec.handle }));
}
Nodes.sessions = function (handle) { return (handle ? [Nodes.get(handle)] : Nodes.M.recs).filter(Boolean).flatMap(nhSessions); };
Nodes.tasks = function (handle) { return (handle ? [Nodes.get(handle)] : Nodes.M.recs).filter(Boolean).flatMap(nhTasks); };

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
    Nodes.chip(s.node), el('span', { class: 'dim nd-meta', text: [GLYPH_LABEL[st], s.since ? nhAgeText(s.since) : ''].filter(Boolean).join(' · ') }));
}
function nhSessionSig(s) { return JSON.stringify([s.tmux, s.session, s.state, s.needs_you, s.agent, s.project, s.repo, s.since, Math.floor(Date.now() / 60000), Nodes.sig(s)]); }

const NH_PHASE = { queued: '○', running: '✽', review: '✻', done: '✓', failed: '✕', cancelled: '○', backlog: '∙' };
function nhTaskRow(t) {
  const href = Ref.hash(Ref.task(t.node, t.id));
  const title = String(t.title || `Task ${t.id}`);
  const phase = String(t.phase || 'running');
  return el('div', { class: 'nd-item nd-task', 'data-key': Ref.key({ kind: 'task', node: t.node, id: t.id }), 'data-phase': phase },
    el('span', { class: 'nd-g' }, el('span', { class: 'glyph', 'aria-hidden': 'true', text: ownKey(NH_PHASE, phase) ? NH_PHASE[phase] : '·' }), agentGlyph(nhAgent(t.agent))),
    el(href ? 'a' : 'span', { class: 'nd-name', href: href || null, text: title }), el('span', { class: 'dim nd-where', text: nhWhere(t) + (t.issue_ref ? ` ${t.issue_ref}` : '') }),
    Nodes.chip(t.node), el('span', { class: 'dim nd-meta', text: [phase, t.updated_at ? nhAgeText(t.updated_at) : ''].filter(Boolean).join(' · ') }));
}
function nhTaskSig(t) { return JSON.stringify([t.id, t.title, t.phase, t.agent, t.project, t.repo, t.issue_ref, t.updated_at, Math.floor(Date.now() / 60000), Nodes.sig(t)]); }

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
    const list = el('div', { class: 'roster nd-list' });
    const host = el('section', { class: 'nd-inbox hidden', 'aria-label': 'On other nodes' }, el('div', { class: 'row head' }, title, more), list,
      el('p', { class: 'dim nd-ro', text: 'Read only here: open the session on its node to answer it.' }));
    nhInsertAfter(anchor, host);
    const kl = nhList(list, (s) => Ref.key({ tmux: s.tmux, node: s.node }), nhSessionRow, nhSessionSig);
    return { hosts: [host], paint() {
      const all = Nodes.sessions().filter((s) => s.needs_you);
      const items = opts.limit > 0 ? all.slice(0, opts.limit) : all;
      kl.update(items);
      setTextIfChanged(title, `On other nodes (${all.length})`);
      setTextIfChanged(more, `Show all ${all.length}`);
      more.classList.toggle('hidden', !(opts.limit > 0 && all.length > items.length));
      host.classList.toggle('hidden', !all.length);
      if (opts.none) opts.none.classList.toggle('hidden', !!opts.local || all.length > 0);      // "Nothing needs you" is not true while another node's items wait
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
  const items = card && card.accounts && Array.isArray(card.accounts.items) ? card.accounts.items : [];
  if (items.length) {
    const word = (a) => `${a.agent} ${a.label || 'account'}${a.window && a.window.known && typeof a.window.pct === 'number' ? ` ${Math.round(a.window.pct)}%` : ''}${a.limited ? ' limit' : ''}`;
    kids.push(el('span', { class: 'dim', text: `${items.length} account${items.length === 1 ? '' : 's'}: ${items.slice(0, 4).map(word).join(', ')}` }));
  }
  return el('div', { class: 'nd-set-line', 'data-handle': handle }, ...kids);
};

/* ---------- demo ---------- */

/* GET /api/nodes/state in demo mode (core.js demoApi): the fixture's `hub` records, as the board would answer them. */
function demoHubState(data, at) {
  return { nodes: Array.isArray(data && data.hub) ? data.hub : [], at };
}

Nodes.ready = true;
