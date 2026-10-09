/* ccboard quad page (v0.5.9, v3 in v0.5.9c; route #/quad[?s=a,b,c,...&l=1|2|4|6|8|10&p=<project>]): up to ten live terminals side by side, each labelled with its task.
   Classic script, one namespace (Quad). Definition only at load: nothing here touches the DOM, storage, the network or a timer until the page is
   mounted, and TermKit / Live / Keymap / Dnd are looked up when they are used (this file may load before or without them).

   Layouts 1, 2, 4 (2 x 2), 6 (3 x 2), 8 (4 x 2) and 10 (5 x 2), a numeric segmented control in the header; the default is 2. The window caps them: under 840 px (the
   phone) one tile with the chip switcher (a scroll-snap strip of 44 px chips above ONE mounted tile in full mode, so only one websocket is live); 840-1199 up to 4;
   1200-1599 up to 8; 1600 and up 10. A tile is never narrower than 240 px: a layout that cannot fit at that width gives way to the next one that does (Quad.layoutFor
   says why, in the control's title and a quiet caption; the wanted layout stays saved and comes back with the room). Quad.SLOTS is 10; the slots beyond the
   layout stay saved and their tiles are torn down.
   A tile is article.qtile[data-tmux][data-slot][data-mode=grid|full|ro|tail][data-drop=session] built ONCE per tmux name: its header (state and agent
   glyph, project/repo · session with a ▾ that opens the tile's view dropdown (TermKit.tileMenu: view, input, tune, session), the mode as a 4-way segmented control (a menu
   button under 352 px), ctx %, size chip, zoom, open, reconnect, close; pages.css sheds them as the tile narrows, down to glyphs, name and the ▾ at 300 px), the task line
   (task title, else the last prompt; the last message while it waits), the pending-permission line (Allow / Deny / In terminal), the body (a ttyd iframe,
   or pre.tail fed by Live) and, from 520 px up and when 'Show composer' is ticked, the docked composer. The grid places a tile by its data-slot with CSS `order` and
   nobody ever reorders the DOM: moving an iframe reloads it.
   Zoom is an overlay over an unchanged grid, so the other tiles keep their pixel size (no refit, no /resize POST). Tiles beyond the layout are torn
   down: iframe.src = 'about:blank', then remove(), observers off, Live.unsubscribe.
   Fullscreen (never entered by itself, never remembered): the header button or F or Ctrl+Alt+F puts html.quad-fs on the page (pages.css hides the topbar, sidebar, banner and
   bottom nav and makes the quad header one row) and asks the browser for real fullscreen on the document element, so a popover mounted outside the quad still paints; where
   the browser has no Fullscreen API (iOS Safari) or refuses, the class alone does the same inside the page. Esc or Exit fullscreen leaves.

   The scope (v0.5.9b): #/quad is every project, #/quad?p=<project> is one project's quad. The scope picks the sessions: auto-fill, the needs-you order, the sessions an
   empty tile offers, the tile menu's swaps and the phone chips all read Quad.candidates(st, project). A native select in the header (Quad.scopeOptions: All projects,
   then every project with a live session, a count in each label) changes it: the page pushes #/quad?s=<the slots that belong to the new project>&l=..&p=<project> (p
   dropped for All) and the route change keeps those tiles (not one reloads), drops the others and auto-fills the gaps. The last scope is remembered as
   ccboard:quad:scope ('all' or a project name): the sidebar's and drawer's Quad link, g q and the palette's Quad open it (Shell.quadHref puts ?p= in their address).
   A bare #/quad is always all projects (the dock's add-to-quad, the manifest shortcut and the page's own All projects links rely on that). A project with no
   live session shows one line and a New session button instead of the grid.

   State, per scope (all projects or one): ccboard:quad:<project|all> = {layout, slots[4], modes{tmux: mode}, zoom}. The URL (?s= ?l= ?p=) wins over the
   saved state and is written back with navigate(..., {replace: true}) once the page has settled (never inside mount). Sessions fill the empty slots
   by (needs you, working, recent). The default mode of a tile is grid, and full where the window forces one tile (a phone, a narrow column): only that
   changes it, never the person's choice of layout (setLayout leaves every tile's mode alone); only an explicit choice is saved.
   'Needs you' is the board's own list (Inbox.items, cached per state object) when pages/inbox.js is loaded. The quad owns the window: the terminal dock (shell.js) is
   suspended while it is mounted (Shell.dockSuspend / dockResume).

   Pure helpers (the tests drive these without a DOM): Quad.layoutFor(width, wanted, avail), Quad.parseQuery / buildQuery, Quad.slotsState, Quad.autoFill,
   Quad.candidates, Quad.taskLine, Quad.sizeInfo, Quad.needsFit, Quad.shortcutOf, Quad.attentionNext, Quad.load / save.
   Outside the page: Quad.addToQuad(tmux, project) puts a session into the saved slots of that scope and goes to #/quad (the dock's 'add to quad'). */
'use strict';

const Quad = {
  LAYOUTS: [1, 2, 4, 6, 8, 10],
  DEFAULT_LAYOUT: 2,
  SLOTS: 10,
  GRID: { 1: [1, 1], 2: [2, 1], 4: [2, 2], 6: [3, 2], 8: [4, 2], 10: [5, 2] },      // layout -> [columns, rows]
  WIDTH_AT: { 1: 0, 2: 840, 4: 840, 6: 1200, 8: 1200, 10: 1600 },                    // the window width a layout needs: 840-1199 up to 4, 1200-1599 up to 8, 1600 up 10
  TILE_MIN: 240,                   // a tile is never narrower than this many px
  DOCK_MIN: 520,                   // a docked composer needs a tile this wide (pages.css hides it under; the menu does not offer it there)
  GAP: 8,                          // the grid gap (pages.css)
  MODES: ['grid', 'full', 'ro', 'tail'],
  MODE_TITLE: { grid: 'Grid: a small tile that does not size the session', full: 'Full: a writable terminal that sizes the session', ro: 'Read only: watch it, nothing you type reaches the session', tail: 'Tail: the last lines of the pane, no terminal' },
  WIDE_MIN: 840,                   // under this window width the quad is the one-up chip switcher
  STORE: 'ccboard:quad:',
  SCOPE_KEY: 'ccboard:quad:scope', // the scope last used: 'all' or a project name (a raw string, read by shell.js for the nav link)
  RESERVED: ['all', 'scope', 'keys'], // project names that would share a storage key with the all-projects scope, the scope pref and the key-bar pref: they save under ccboard:quad:p:<name>
  KEYS_PREF: 'ccboard:quad:keys',  // '1' shows the host key bar, '0' hides it; unset: shown on touch and in one-up
  MODES_MAX: 24,                   // remembered modes (and docked-composer flags) per scope
  HIDDEN_MS: 60000,                // a tab hidden longer than this reloads its live tiles when it comes back
  MEASURE_MS: 320,                 // the size chip and the auto-fit look after TermKit.fitSoon's 250 ms debounce
  FIT_REPEAT_MS: 60000,            // the same auto-fit target is not asked for again inside this window
  FIT_TRIES: 3,
  COLS: [40, 400],                 // POST /resize bounds (tmux.RESIZE_COLS / RESIZE_ROWS)
  ROWS: [10, 200],
  CHIPS_MAX: 10,                   // the one-up switcher lists this many sessions (Live's cap on ?names= is 20)
  NAME_RE: /^[A-Za-z0-9_-]+--[A-Za-z0-9_-]+--[A-Za-z0-9_-]+$/,
  WORD_RE: /^[A-Za-z0-9_-]+$/,
  current: null,                   // the mounted page (see Quad.mount), for the tests and Quad.addToQuad
};

/* ---------- layout ---------- */

/* The most tiles a window of `width` px takes: 1 under 840, 4 under 1200, 8 under 1600, else 10. An unknown width is the phone. */
Quad.capFor = function (width) {
  const w = Number(width);
  if (!(w >= Quad.WIDE_MIN)) return 1;
  return w < 1200 ? 4 : (w < 1600 ? 8 : 10);
};

/* The most columns a quad column of `avail` px holds with every tile at least TILE_MIN wide (at least 1). */
Quad.maxCols = function (avail) {
  const a = Number(avail);
  return a > 0 ? Math.max(1, Math.floor((a + Quad.GAP) / (Quad.TILE_MIN + Quad.GAP))) : Infinity;
};

/* The largest layout <= `n` whose columns fit in `cols`. */
Quad.fitLayout = function (n, cols) {
  let i = Quad.LAYOUTS.lastIndexOf(n);
  if (i < 0) i = 0;
  while (i > 0 && Quad.GRID[Quad.LAYOUTS[i]][0] > cols) i--;
  return Quad.LAYOUTS[i];
};

/* The plain-word reason a wanted layout is not what shows ('' when it is): the window is too narrow for that many tiles, or the quad column is (a tile would be under 240 px). */
Quad.layoutNote = function (want, n, why) {
  if (!(n < want)) return '';
  if (why === 'room') return `${want} tiles would be narrower than ${Quad.TILE_MIN} px here: showing ${n}`;
  return `${want} tiles need a window ${Quad.WIDTH_AT[want]} px wide or more: showing ${n}`;
};

/* The layout that shows for a window `width`, the wanted layout (1 | 2 | 4 | 6 | 8 | 10) and the quad's own column width `avail` (optional; unknown skips the 240 px rule):
   {n, cols, rows, oneUp, forced, capped, want, cap, why, reason}. `cap` is the largest layout this window and column take (the control disables the ones above it); `capped` says
   n < want; why is '' | 'width' (the window caps it) | 'room' (the window would, the column does not); reason is the caption for it. Under 840 px (the phone, and 600-839: one
   tile, not 'max 2') it is one tile with the chip switcher (forced), whatever the column; a column that leaves only one tile at 240 px is the same (forced). */
Quad.layoutFor = function (width, wanted, avail) {
  const w = Number(width);
  const want = Quad.LAYOUTS.includes(Number(wanted)) ? Number(wanted) : Quad.DEFAULT_LAYOUT;
  if (!(w >= Quad.WIDE_MIN)) return { n: 1, cols: 1, rows: 1, oneUp: true, forced: true, capped: want !== 1, want, cap: 1, why: want !== 1 ? 'width' : '', reason: '' };
  const byWindow = Quad.capFor(w);
  const cap = Quad.fitLayout(byWindow, Quad.maxCols(avail));
  const n = Math.min(want, cap);
  const why = n < want ? (cap < byWindow && want <= byWindow ? 'room' : 'width') : '';
  return { n, cols: Quad.GRID[n][0], rows: Quad.GRID[n][1], oneUp: n === 1, forced: n === 1 && want > 1, capped: n < want, want, cap, why, reason: Quad.layoutNote(want, n, why) };
};

/* ---------- names, the URL and the saved state ---------- */

Quad.parts = function (tmux) {
  const p = String(tmux || '').split('--');
  return p.length === 3 ? p : [String(tmux || ''), '', ''];
};

/* 'phasezero/website · s1'; the project folder is just the project ('phasezero · s1'). */
Quad.label = function (tmux) {
  const k = Ref.splitKey(tmux);
  if (k && k.node) return `${Quad.label(k.tmux)} · on ${k.node}`;       // a slot key of another node: '<handle>/<name>'
  const [p, r, n] = Quad.parts(tmux);
  if (!r) return String(tmux || '');
  return `${p}${r === 'root' ? '' : '/' + r} · ${n}`;
};

/* Is this a slot key (nodes.js Ref.key): a tmux name of this board, or '<handle>/<tmux name>' for a session of another node? */
Quad.keyOk = function (k) {
  const p = Ref.splitKey(k);
  return !!p && Quad.NAME_RE.test(p.tmux);
};

/* Quad.SLOTS slot keys: a valid, not repeated key or ''. A slot is a Ref.key in memory and in the address (?s=): the bare name for this board's session, '<handle>/<name>'
   for another node's. Stored it is the bare name too, or {node, tmux} (Ref.slotValue / slotKey); an entry in neither shape is an empty slot. */
Quad.cleanSlots = function (list) {
  const out = [];
  const seen = new Set();
  for (let i = 0; i < Quad.SLOTS; i++) {
    const t = Array.isArray(list) ? Ref.slotKey(list[i]) : '';
    if (t && Quad.keyOk(t) && !seen.has(t)) { seen.add(t); out.push(t); } else out.push('');
  }
  return out;
};

/* The route query {s, l, p} as {slots: string[10] | null, layout: 1|2|4|6|8|10 | null, project: string | null, any}: whatever is not valid is left out. */
Quad.parseQuery = function (query) {
  const q = query && typeof query === 'object' ? query : {};
  const out = { slots: null, layout: null, project: null, any: false };
  if (typeof q.s === 'string' && q.s !== '') {
    const slots = Quad.cleanSlots(q.s.split(',').slice(0, Quad.SLOTS));
    if (slots.some(Boolean)) { out.slots = slots; out.any = true; }
  }
  if (typeof q.l === 'string' && Quad.LAYOUTS.includes(Number(q.l))) { out.layout = Number(q.l); out.any = true; }
  if (typeof q.p === 'string' && Quad.WORD_RE.test(q.p)) { out.project = q.p; out.any = true; }
  return out;
};

/* {s, l, p} for buildHash('quad', {}, ...): s up to the last filled slot, l always, p only inside a project. */
Quad.buildQuery = function (st) {
  const x = st || {};
  const slots = Quad.cleanSlots(x.slots);
  let last = slots.length;
  while (last > 0 && !slots[last - 1]) last--;
  const out = {};
  if (last) out.s = slots.slice(0, last).join(',');
  out.l = String(Quad.LAYOUTS.includes(Number(x.layout)) ? Number(x.layout) : Quad.DEFAULT_LAYOUT);
  if (typeof x.project === 'string' && Quad.WORD_RE.test(x.project)) out.p = x.project;
  return out;
};

Quad.hashFor = function (st) { return buildHash('quad', {}, Quad.buildQuery(st)); };

Quad.storageKey = function (project) {
  if (!(project && Quad.WORD_RE.test(project))) return Quad.STORE + 'all';
  return Quad.STORE + (Quad.RESERVED.includes(project) ? 'p:' : '') + project;
};

/* Remember the scope and let the sidebar's Quad link follow it (Shell.syncQuadLinks, when shell.js is there). */
Quad.rememberScope = function (project) {
  const v = project && Quad.WORD_RE.test(project) ? project : 'all';
  let was = null;
  try { was = localStorage.getItem(Quad.SCOPE_KEY); } catch (_) { was = null; }
  if (was !== v) { try { localStorage.setItem(Quad.SCOPE_KEY, v); } catch (_) { /* storage may be unavailable */ } }
  try { if (typeof Shell !== 'undefined' && Shell && typeof Shell.syncQuadLinks === 'function') Shell.syncQuadLinks(); } catch (e) { console.error('ccboard quad link', e); }
};

/* The saved {layout, slots, modes, zoom, composers} of a scope, or null when nothing (usable) was saved. `composers` is {tmux: true} for the tiles whose docked composer is on. */
Quad.load = function (project) {
  let raw = null;
  try { raw = JSON.parse(localStorage.getItem(Quad.storageKey(project)) || 'null'); } catch (_) { raw = null; }
  if (!raw || typeof raw !== 'object') return null;
  const modes = {};
  if (raw.modes && typeof raw.modes === 'object') for (const [k, v] of Object.entries(raw.modes)) if (Quad.keyOk(k) && Quad.MODES.includes(v)) modes[k] = v;
  const composers = {};
  if (raw.composers && typeof raw.composers === 'object') for (const [k, v] of Object.entries(raw.composers)) if (Quad.keyOk(k) && v === true) composers[k] = true;
  const zoom = Number.isInteger(raw.zoom) && raw.zoom >= 0 && raw.zoom < Quad.SLOTS ? raw.zoom : null;
  return { layout: Quad.LAYOUTS.includes(raw.layout) ? raw.layout : Quad.DEFAULT_LAYOUT, slots: Quad.cleanSlots(raw.slots), modes, zoom, composers };
};

Quad.save = function (project, st) {
  const modes = {};
  const keys = Object.keys(st.modes || {});
  for (const k of keys.slice(Math.max(0, keys.length - Quad.MODES_MAX))) modes[k] = st.modes[k];
  const composers = {};
  const docked = Object.keys(st.composers || {}).filter((k) => st.composers[k] === true);
  for (const k of docked.slice(Math.max(0, docked.length - Quad.MODES_MAX))) composers[k] = true;
  const zoom = Number.isInteger(st.zoom) && st.zoom >= 0 && st.zoom < Quad.SLOTS ? st.zoom : null;
  const value = { layout: Quad.LAYOUTS.includes(Number(st.layout)) ? Number(st.layout) : Quad.DEFAULT_LAYOUT, slots: Quad.cleanSlots(st.slots).map(Ref.slotValue), modes, zoom, composers };      // this board's slots stay bare names; another node's are {node, tmux}
  if (typeof savePrefs === 'function') savePrefs(Quad.storageKey(project), value);
  else { try { localStorage.setItem(Quad.storageKey(project), JSON.stringify(value)); } catch (_) { /* storage may be unavailable */ } }
  return value;
};

/* ---------- sessions: who is there, who needs you, who goes where ---------- */

/* Every session of a state payload with its project and repo. */
Quad.roster = function (st) {
  const out = [];
  for (const p of ((st && st.projects) || [])) {
    for (const r of [p.root, ...(p.repos || [])]) {
      if (!r) continue;
      for (const s of (r.sessions || [])) out.push({ ...s, project: p.name, repo: r.name });
    }
    for (const s of (p.orphan_sessions || [])) out.push({ ...s, project: p.name, repo: s.repo || '?' });
  }
  return out;
};

Quad.agentOf = function (s) { return s.agent || (s.launcher === 'shell' || s.launcher === 'clone' ? 'shell' : 'claude'); };

Quad.activity = function (s) {
  const t = s.state_at ? Date.parse(s.state_at) / 1000 : NaN;
  return Number.isFinite(t) ? t : (s.created || 0);
};

/* `tmux` is a slot key: a pending permission is this board's, so only a bare name (never '<handle>/<name>') can match its tmux_name. */
Quad.perm = function (st, tmux) {
  for (const pr of ((st && st.pending_permissions) || [])) if (pr.tmux_name === tmux) return pr;
  return null;
};

/* The board's own 'needs you' list for a state payload (Inbox.items: permissions, plans, questions, blocked jobs, limits, errors, done-with-a-question, waiting; the
   same set the sidebar badge counts), as {rank: Map(tmux -> index in the inbox's order)}; null when pages/inbox.js is not loaded (the rule below stands in). Cached per
   state object, so a poll does one Inbox pass however many sessions the quad sorts. */
Quad.inboxFor = function (st) {
  if (!st || typeof st !== 'object' || typeof Inbox === 'undefined' || !Inbox || typeof Inbox.items !== 'function') return null;
  if (!Quad.inboxCache) Quad.inboxCache = new WeakMap();
  let hit = Quad.inboxCache.get(st);
  if (hit === undefined) {
    hit = null;
    try { hit = { rank: new Map(Inbox.items(st).map((it, i) => [Ref.key(it), i])) }; } catch (_) { hit = null; }       // an inbox that throws leaves the quad on its own rule
    Quad.inboxCache.set(st, hit);
  }
  return hit;
};

/* The board's 'needs you' (the inbox list), else: a pending permission, a session that waits, or one whose last turn wants an acknowledgement. */
Quad.needsYou = function (s, st) {
  if (!s) return false;
  const ib = Quad.inboxFor(st);
  if (ib) return ib.rank.has(Ref.key(s));
  return !!(Quad.perm(st, Ref.key(s)) || s.state === 'waiting' || s.needs_attention === true);
};

/* 0 needs you (with the inbox: its order; without: 0 a permission, 1 waiting, 2 needs an ack), 3 working, 4 the rest, 5 a plain shell. */
Quad.group = function (s, st) {
  const ib = Quad.inboxFor(st);
  if (ib) {
    if (ib.rank.has(Ref.key(s))) return 0;
  } else {
    if (Quad.perm(st, Ref.key(s))) return 0;
    if (s.state === 'waiting') return 1;
    if (s.needs_attention === true) return 2;
  }
  if (s.state === 'working') return 3;
  return Quad.agentOf(s) === 'shell' ? 5 : 4;
};

/* Needs-you first (the inbox's order, else the one that has waited longest first), then working, then the most recent. */
Quad.compare = function (a, b, st) {
  const ga = Quad.group(a, st);
  const gb = Quad.group(b, st);
  if (ga !== gb) return ga - gb;
  const ib = ga === 0 ? Quad.inboxFor(st) : null;
  if (ib) return ib.rank.get(Ref.key(a)) - ib.rank.get(Ref.key(b));
  const d = ga <= 2 ? Quad.activity(a) - Quad.activity(b) : Quad.activity(b) - Quad.activity(a);
  return d || Ref.key(a).localeCompare(Ref.key(b));
};

/* The sessions a tile can show (not ended), in auto-fill order; `project` limits it to one project. */
Quad.candidates = function (st, project) {
  return Quad.roster(st).filter((s) => s.tmux && s.state !== 'ended' && (!project || s.project === project)).sort((a, b) => Quad.compare(a, b, st));
};

/* The scope select's options: [{project: '' (all) | name, count, label}]: All projects first, then every project with a live session (the sessions a tile can show,
   not ended) by name, a count in each label. `current` is always listed (with 0 when it has none): a select that did not could not show where the page is. */
Quad.scopeOptions = function (st, current) {
  const counts = new Map();
  for (const s of Quad.roster(st)) if (s.tmux && s.state !== 'ended' && Quad.WORD_RE.test(String(s.project))) counts.set(s.project, (counts.get(s.project) || 0) + 1);
  if (current && Quad.WORD_RE.test(current) && !counts.has(current)) counts.set(current, 0);
  const total = Array.from(counts.values()).reduce((a, b) => a + b, 0);
  const out = [{ project: '', count: total, label: `All projects (${total})` }];
  for (const name of Array.from(counts.keys()).sort((a, b) => a.localeCompare(b))) out.push({ project: name, count: counts.get(name), label: `${name} (${counts.get(name)})` });
  return out;
};

/* Does this session belong to `project`? ('' is every project.) The roster knows; a session that has gone is told by the project of its name. */
Quad.inProject = function (st, tmux, project) {
  if (!project) return true;
  for (const s of Quad.roster(st)) if (Ref.key(s) === tmux) return s.project === project;
  return Quad.parts((Ref.splitKey(tmux) || { tmux }).tmux)[0] === project;
};

/* The tmux names that auto-fill picks for `n` tiles: opts {project, exclude: names already placed}. */
Quad.autoFill = function (st, n, opts) {
  const o = opts || {};
  const skip = new Set(o.exclude || []);
  return Quad.candidates(st, o.project).map((s) => Ref.key(s)).filter((t) => !skip.has(t)).slice(0, Math.max(0, n));
};

/* `slots` with the empty ones among the indexes `only` (default: the first n) filled: first by the live sessions that sit in the hidden slots (n and up: a smaller layout
   left them there, no tile shows them), in slot order, each moving into the empty visible slot (its hidden slot is cleared: a session is in one slot only); then by auto-fill;
   the rest is untouched. Only slot numbers change, never a tile node: a hidden slot has no tile, so no iframe moves in the DOM. */
Quad.fillSlots = function (slots, st, n, opts) {
  const o = opts || {};
  const out = Quad.cleanSlots(slots);
  const shown = Math.min(Math.max(0, n), Quad.SLOTS);
  const idx = (o.only || Array.from({ length: shown }, (_, i) => i)).filter((i) => i >= 0 && i < Quad.SLOTS && !out[i]);
  const live = new Set(Quad.candidates(st, o.project).map((s) => Ref.key(s)));
  const hidden = [];
  for (let i = shown; i < Quad.SLOTS; i++) if (out[i] && live.has(out[i])) hidden.push(i);
  let k = 0;
  while (k < hidden.length && k < idx.length) { out[idx[k]] = out[hidden[k]]; out[hidden[k]] = ''; k++; }
  const rest = idx.slice(k);
  const picks = Quad.autoFill(st, rest.length, { project: o.project, exclude: out.filter(Boolean) });
  rest.forEach((i, j) => { if (picks[j]) out[i] = picks[j]; });
  return out;
};

/* What the page starts from, URL over saved over auto-fill. o = {query (the route query), saved (Quad.load), state (the /api/state payload, null before
   the first poll), project, n (visible slots; default the layout)} -> {layout, slots, modes, zoom, composers, fromUrl}. A saved slot whose session is gone is
   dropped; a URL slot is kept as it is (the tile then says the session is not running); every empty visible slot is then filled by auto-fill (an empty
   tile is something the person does inside one visit, with Close: a reload starts from what needs you again). */
Quad.slotsState = function (o) {
  const parsed = Quad.parseQuery(o.query);
  const saved = o.saved || null;
  const layout = parsed.layout || (saved && saved.layout) || Quad.DEFAULT_LAYOUT;
  const n = Math.min(Quad.SLOTS, Math.max(1, o.n || layout));
  const st = o.state || null;
  let slots = parsed.slots ? parsed.slots.slice() : (saved ? saved.slots.slice() : Quad.cleanSlots([]));
  if (o.project) slots = slots.map((t) => (t && !Quad.inProject(st, t, o.project) ? '' : t));         // a project's quad shows that project's sessions only
  const modes = saved ? { ...saved.modes } : {};
  const composers = saved && saved.composers ? { ...saved.composers } : {};
  let zoom = saved && saved.zoom !== null && saved.zoom < n ? saved.zoom : null;
  if (st) {
    const live = new Set(Quad.roster(st).map((s) => Ref.key(s)));
    if (!parsed.slots) slots = slots.map((t) => (t && !live.has(t) ? '' : t));
    slots = Quad.fillSlots(slots, st, n, { project: o.project || '' });
  }
  if (zoom !== null && !slots[zoom]) zoom = null;
  return { layout, slots, modes, zoom, composers, fromUrl: { slots: !!parsed.slots, layout: !!parsed.layout } };
};

/* ---------- one tile ---------- */

/* grid, except where the window forces one tile (a phone, a narrow column): there it is full. A person who picks 1 on a wide screen keeps grid. */
Quad.defaultMode = function (forced) { return forced ? 'full' : 'grid'; };

Quad.modeOf = function (modes, tmux, forced) {
  const m = modes && modes[tmux];
  return Quad.MODES.includes(m) ? m : Quad.defaultMode(forced);
};

function quadOneLine(text, n) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  return t.length > n ? t.slice(0, n - 1).trimEnd() + '…' : t;
}

/* {text, kind}: the task title, else the last prompt; while the session waits, its last message (the question, so its tail). */
Quad.taskLine = function (s) {
  const x = s || {};
  const msg = String(x.last_message || '').replace(/\s+/g, ' ').trim();
  if (x.state === 'waiting' && msg) return { text: msg.length > 240 ? '…' + msg.slice(-239).trimStart() : msg, kind: 'ask' };
  const task = x.task && typeof x.task === 'object' && x.task.title ? quadOneLine(x.task.title, 200) : '';
  if (task) return { text: task, kind: 'task' };
  const prompt = quadOneLine(x.last_prompt, 200);
  return prompt ? { text: prompt, kind: 'prompt' } : { text: '', kind: '' };
};

/* Can this session take a typed command or prompt right now? {show, ok, why}: show is false for a shell and a row that is not there (nothing to tune); ok is true when the pane sits
   at its prompt (idle, done, errored, or waiting on the idle prompt: the server's TYPEABLE_STATES) with no compaction and no permission request open; why is the sentence
   for what is disabled otherwise. The same rule as the terminal page's tuning strip (term.js tuneGate, which the board does not load) and the server's 409. */
Quad.atPrompt = function (s, st) {
  const r = s && typeof s === 'object' ? s : null;
  const agent = r ? Quad.agentOf(r) : '';
  if (!r || agent === 'shell') return { show: false, ok: false, why: '' };
  const flags = r.flags && typeof r.flags === 'object' ? r.flags : {};
  const at = r.state === 'idle' || r.state === 'done' || r.state === 'errored' || (r.state === 'waiting' && flags.wait_kind === 'idle');
  const open = (Array.isArray(r.pending) && r.pending.length > 0) || !!Quad.perm(st, r.tmux);
  const ok = at && !flags.compacting && !open;
  return { show: true, ok, why: ok ? '' : 'Available when the session is at its prompt' };
};

/* The size chip: the session window ('45x30') or 'cropped' when the tile's terminal is narrower than the window (a grid tile does not size it).
   win = [cols, rows] | null, dims = {cols, rows} | null (the tile's xterm). */
Quad.sizeInfo = function (win, dims) {
  const w = Array.isArray(win) && win.length === 2 && win[0] > 0 && win[1] > 0 ? win : null;
  const d = dims && dims.cols > 0 && dims.rows > 0 ? dims : null;
  if (w && d && d.cols < w[0] - 2) return { text: 'cropped', cropped: true, title: `the window is ${w[0]}x${w[1]}, this tile shows ${d.cols}x${d.rows}` };
  if (w) return { text: `${w[0]}x${w[1]}`, cropped: false, title: `the session window is ${w[0]} columns by ${w[1]} rows` };
  if (d) return { text: `${d.cols}x${d.rows}`, cropped: false, title: `this tile shows ${d.cols} columns by ${d.rows} rows` };
  return { text: '', cropped: false, title: '' };
};

/* {cols, rows} to POST to /resize, or null: nobody full is attached, the window is more than 2 columns off the tile, and the size is one the server takes. */
Quad.needsFit = function (viewers, win, dims) {
  const full = Number((viewers && viewers.full) || 0);
  if (full > 0 || !Array.isArray(win) || win.length !== 2 || !dims) return null;
  const cols = Math.round(Number(dims.cols));
  const rows = Math.round(Number(dims.rows));
  if (!(cols >= Quad.COLS[0] && cols <= Quad.COLS[1] && rows >= Quad.ROWS[0] && rows <= Quad.ROWS[1])) return null;
  if (Math.abs(Number(win[0]) - cols) <= 2) return null;
  return { cols, rows };
};

/* Ctrl+Alt+1..9 and Ctrl+Alt+0 focus tiles 1..10, Z zooms the active one, F goes full screen (or back), K goes to the next session that needs you, R reloads every tile.
   e.code first: Option+digit types another character on a Mac; AltGr (Ctrl+Alt on Windows, it types characters on some layouts) is not a shortcut. */
Quad.shortcutOf = function (e) {
  if (!e || !e.ctrlKey || !e.altKey || e.metaKey || e.shiftKey || e.isComposing) return null;
  try { if (typeof e.getModifierState === 'function' && e.getModifierState('AltGraph')) return null; } catch (_) { /* no modifier state */ }      // AltGr is Ctrl+Alt on Windows: it types ~ { # on some layouts
  const code = typeof e.code === 'string' ? e.code : '';
  const key = typeof e.key === 'string' ? e.key.toLowerCase() : '';
  const m = /^(?:Digit|Numpad)([0-9])$/.exec(code) || (/^[0-9]$/.test(key) ? [key, key] : null);
  if (m) return { act: 'focus', n: m[1] === '0' ? 10 : Number(m[1]) };
  if (code === 'KeyZ' || (!code && key === 'z')) return { act: 'zoom' };
  if (code === 'KeyF' || (!code && key === 'f')) return { act: 'fullscreen' };
  if (code === 'KeyK' || (!code && key === 'k')) return { act: 'attention' };
  if (code === 'KeyR' || (!code && key === 'r')) return { act: 'reload' };
  return null;
};

/* The next session after `current` that needs you (permission first, then the longest waiting), or '' when nothing does. */
Quad.attentionNext = function (st, project, current) {
  const list = Quad.candidates(st, project).filter((s) => Quad.needsYou(s, st)).map((s) => Ref.key(s));
  if (!list.length) return '';
  return list[(list.indexOf(current) + 1) % list.length];
};

/* Put a session into the saved slots of a scope (the first free visible slot, else the last visible one) and go to #/quad there: the dock's 'add to quad'. The layout grows
   (1, 2, 4, 6, 8, 10) rather than evict a tile, as far as this window takes tiles. */
Quad.addToQuad = function (tmux, project) {
  if (typeof tmux !== 'string' || !Quad.keyOk(tmux)) return false;
  const scope = project && Quad.WORD_RE.test(project) ? project : '';
  const cur = Quad.current;
  if (cur && cur.project === scope) {
    cur.pick(tmux);
    return true;
  }
  const saved = Quad.load(scope) || { layout: Quad.DEFAULT_LAYOUT, slots: Quad.cleanSlots([]), modes: {}, zoom: null, composers: {} };
  const slots = saved.slots.slice();
  let layout = saved.layout;
  if (!slots.includes(tmux)) {
    let width = 0;
    try { width = typeof window !== 'undefined' && window.innerWidth > 0 ? window.innerWidth : 0; } catch (_) { width = 0; }
    const cap = width > 0 ? Math.max(4, Quad.capFor(width)) : 4;                                  // a phone or an unknown window grows to the 4-up this function always grew to
    const shown = () => Math.min(layout, cap);
    let free = slots.findIndex((t, i) => !t && i < shown());
    while (free < 0 && shown() < cap && layout < Quad.SLOTS) { layout = Quad.LAYOUTS[Quad.LAYOUTS.indexOf(layout) + 1]; free = slots.findIndex((t, i) => !t && i < shown()); }     // full: the layout grows rather than evict a tile
    slots[free >= 0 ? free : shown() - 1] = tmux;
  }
  Quad.save(scope, { ...saved, layout, slots });
  if (typeof navigate === 'function') navigate(buildHash('quad', {}, scope ? { p: scope } : {}));
  return true;
};

/* ---------- the page ---------- */

Quad.mount = function (root, route) {
  // the quad owns the window: the dock (shell.js) lets go BEFORE this page measures its column, and comes back in destroy()
  try { if (typeof Shell !== 'undefined' && Shell && typeof Shell.dockSuspend === 'function') Shell.dockSuspend(); } catch (e) { console.error('ccboard quad dock', e); }
  const parsed0 = Quad.parseQuery(route && route.query);
  const I = {
    root, host: null, project: parsed0.project || '', query: route && route.query ? route.query : {}, carry: null, noScope: false, scopeSig: null,
    layout: parsed0.layout || Quad.DEFAULT_LAYOUT, slots: Quad.cleanSlots([]), modes: {}, composers: {}, zoom: null,
    n: 1, oneUp: true, forced: true, cap: 1, reason: '', reMode: false, focused: false, touch: false, resolved: false, st: null, roster: [], index: new Map(),
    fs: false, fsApi: false, fsZoom: false, pop: null, menu: null, menuTile: null,
    active: '', tiles: new Map(), empties: new Map(), manualEmpty: new Set(),
    chipTails: new Map(), chipLines: new Map(), chipList: null,
    hiddenAt: 0, writeTimer: null, writing: false, disposed: false, listeners: [], offs: [], pointerOff: [], chipOrder: [], chipSel: '',
  };
  const enc = encodeURIComponent;

  /* ----- small helpers ----- */
  const note = (text, kind) => { if (typeof pageToast === 'function') pageToast(text, kind || 'bad'); };
  const timer = (fn, ms) => setTimeout(() => { if (!I.disposed) fn(); }, ms);
  const listen = (target, type, fn, opts) => { target.addEventListener(type, fn, opts); I.listeners.push(() => target.removeEventListener(type, fn, opts)); };
  const coarse = () => (typeof coarsePointer === 'function' ? coarsePointer() : false);

  function viewportWidth() {
    try { if (typeof window.innerWidth === 'number' && window.innerWidth > 0) return window.innerWidth; } catch (_) { /* no window size */ }
    try { return window.matchMedia && window.matchMedia('(min-width: 840px)').matches ? 1280 : 480; } catch (_) { return 1280; }
  }

  function columnWidth() { try { return (I.host && I.host.clientWidth) || 0; } catch (_) { return 0; } }

  function stored(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }

  function savedFont() {
    try { return typeof TermKit !== 'undefined' ? TermKit.clampFont(stored('ccboard:term:fs')) : null; } catch (_) { return null; }
  }

  const known = () => !!(I.st && !I.st.tmux_down);                 // a poll that failed leaves the old state; tmux down is the one 'I do not know' the server sends
  const sessionOf = (tmux) => I.index.get(tmux) || null;
  /* A project's quad with nothing to show: no live session in it and no tile on screen (the board knows: tmux is up). */
  const emptyScope = () => !!(I.project && known() && !Quad.candidates(I.st, I.project).length && !I.slots.slice(0, I.n).some(Boolean));
  const visibleSlots = () => I.slots.slice(0, I.n);
  const tileAt = (slot) => { for (const t of I.tiles.values()) if (t.slot === slot) return t; return null; };
  const modeFor = (tmux) => Quad.modeOf(I.modes, tmux, I.forced);

  /* ----- persistence and the address ----- */
  function snapshot() { return { layout: I.layout, slots: I.slots, modes: I.modes, composers: I.composers, zoom: I.zoom, project: I.project }; }

  function flush() {
    if (I.writeTimer !== null) { clearTimeout(I.writeTimer); I.writeTimer = null; }
    if (I.disposed || !I.resolved || typeof location === 'undefined' || typeof navigate !== 'function') return false;
    const h = Quad.hashFor(snapshot());
    if (location.hash === h) return false;
    I.writing = true;
    try { navigate(h, { replace: true }); } finally { I.writing = false; }
    try { if (typeof Shell !== 'undefined' && Shell && typeof Shell.syncCrumbs === 'function') Shell.syncCrumbs(); } catch (e) { console.error('ccboard quad crumb', e); }      // a replace fires no hashchange
    return true;
  }

  function persist() {
    if (!I.resolved) return;
    Quad.save(I.project, snapshot());
    if (I.writeTimer === null) I.writeTimer = setTimeout(flush, 0);
  }

  /* ----- tile bodies: the iframe, or the tail ----- */
  function urlFor(tmux, mode) {
    const o = { mode };
    if (mode !== 'grid') { const fs = savedFont(); if (fs !== null) o.fontSize = fs; }
    return TermKit.ttyUrl(tmux, o);
  }

  function frameDoc(frame) {
    try { const w = frame.contentWindow; return (w && w.document) || frame.contentDocument || null; } catch (_) { return null; }
  }

  function dropFrame(tile) {
    const f = tile.frame;
    if (!f) return;
    tile.frame = null;
    tile.url = '';
    try { if (tile.bound) tile.bound.destroy(); } catch (_) { /* a binding that is already gone */ }
    tile.bound = null;
    try { f.src = 'about:blank'; } catch (_) { /* detached */ }          // first the blank page (the websocket closes), then the node
    try { f.remove(); } catch (_) { /* detached */ }
  }

  function dropTail(tile) {
    const off = tile.tailOff;
    tile.tailOff = null;
    if (off) { try { off(); } catch (e) { console.error('ccboard quad tail', e); } }
    if (tile.pre) { try { tile.pre.remove(); } catch (_) { /* detached */ } tile.pre = null; }
  }

  function showNote(tile, text) {
    if (!tile.note) { tile.note = el('div', { class: 'qt-note' }); tile.body.append(tile.note); }
    setTextIfChanged(tile.note, text);
  }

  function hideNote(tile) {
    if (tile.note) { tile.note.remove(); tile.note = null; }
  }

  function ensureFrame(tile) {
    if (typeof TermKit === 'undefined') { showNote(tile, 'The terminal kit did not load.'); return; }
    hideNote(tile);
    let url = '';
    try { url = urlFor(tile.tmux, tile.mode); } catch (e) { showNote(tile, 'Not a terminal session name.'); return; }
    if (!tile.frame) {
      const frame = el('iframe', { class: 'qt-frame', title: `terminal ${Quad.label(tile.tmux)}`, allow: 'clipboard-write' });
      tile.frame = frame;
      tile.bound = TermKit.bind(frame, { onActive: (ev) => { if (ev !== 'focusin') touched(); setActive(tile.tmux); }, touchScroll: true });
      frame.addEventListener('load', () => onFrameLoad(tile, frame));
      tile.url = url;
      frame.src = url;                                              // before it is in the page: no initial about:blank load to bind to
      tile.body.append(frame);
      return;
    }
    if (tile.url !== url) { tile.url = url; tile.frame.src = url; }
  }

  /* A new document in the iframe: the shortcuts work with the terminal focused too (the host document never sees those keys), and the size is read again. */
  function onFrameLoad(tile, frame) {
    if (I.disposed || tile.frame !== frame) return;
    const d = frameDoc(frame);
    if (d && typeof d.addEventListener === 'function') { try { d.addEventListener('keydown', onKey, true); } catch (_) { /* cross-origin */ } }
    scheduleMeasure(tile);
  }

  function ensureTail(tile) {
    hideNote(tile);
    if (tile.pre) return;
    tile.pre = el('pre', { class: 'tail qt-tail', tabindex: '0', text: 'waiting for output…' });
    tile.body.append(tile.pre);
    if (typeof Live === 'undefined' || typeof Live.subscribe !== 'function') return;
    const fn = (lines) => {
      if (!tile.pre) return;
      const text = (Array.isArray(lines) ? lines : []).join('\n');
      setTextIfChanged(tile.pre, text || '(no output yet)');
      tile.pre.scrollTop = tile.pre.scrollHeight;
    };
    tile.tailOff = Live.subscribe(tile.tmux, fn);
  }

  /* The body follows the tile's mode (and whether its session is there at all). */
  function mountBody(tile) {
    tile.node.setAttribute('data-mode', tile.mode);
    patchModeButtons(tile);
    if (tile.gone) { dropTail(tile); dropFrame(tile); showNote(tile, 'This session is not running any more.'); return; }
    if ((Ref.splitKey(tile.tmux) || {}).node) { dropTail(tile); dropFrame(tile); showNote(tile, 'This session runs on another node. Its terminal opens from that board.'); return; }      // a remote tile (a later phase) never opens a local terminal by a name it only shares
    if (tile.mode === 'tail') { dropFrame(tile); ensureTail(tile); } else { dropTail(tile); ensureFrame(tile); }
  }

  function patchModeButtons(tile) {
    for (const b of tile.modeBtns) b.setAttribute('aria-pressed', b.getAttribute('data-mode') === tile.mode ? 'true' : 'false');
    setTextIfChanged(tile.modeLabel, tile.mode);
    tile.modeMenu.setAttribute('title', `View: ${tile.mode}. ${Quad.MODE_TITLE[tile.mode]}`);
    tile.modeMenu.setAttribute('aria-label', `View mode: ${tile.mode}`);
  }

  function setMode(tile, mode) {
    if (!Quad.MODES.includes(mode) || tile.mode === mode) return;
    I.modes[tile.tmux] = mode;
    tile.mode = mode;
    mountBody(tile);
    persist();
  }

  /* ----- fit: the terminal follows its tile, the session window follows the terminal when nobody full is attached ----- */
  function fitFrame(tile) {
    if (!tile.frame || typeof TermKit === 'undefined' || !visible(tile)) return;
    TermKit.fitSoon(tile.frame);
    scheduleMeasure(tile);
  }

  function visible(tile) { return I.zoom === null || tile.slot === I.zoom; }

  function fitAll() { for (const t of I.tiles.values()) fitFrame(t); }

  function scheduleMeasure(tile) {
    if (tile.measureTimer !== null) clearTimeout(tile.measureTimer);
    tile.measureTimer = timer(() => { tile.measureTimer = null; measure(tile); }, Quad.MEASURE_MS);
  }

  function dimsOf(tile) {
    try { const t = tile.bound ? tile.bound.term() : null; return t && t.cols > 0 && t.rows > 0 ? { cols: t.cols, rows: t.rows } : null; } catch (_) { return null; }
  }

  function measure(tile) {
    if (typeof TermKit !== 'undefined' && TermKit && typeof TermKit.fitName === 'function') TermKit.fitName(tile.where);
    tile.dims = dimsOf(tile);
    patchSize(tile);
    autoFit(tile);
  }

  function autoFit(tile) {
    const s = tile.session;
    if (!s || !tile.dims || !visible(tile) || (tile.mode !== 'grid' && tile.mode !== 'ro')) return;
    const need = Quad.needsFit(s.viewers, s.win, tile.dims);
    if (!need) { tile.fit.blocked = ''; return; }
    const key = `${need.cols}x${need.rows}`;
    const now = Date.now();
    if (tile.fit.blocked === `${key}|${Array.isArray(s.win) ? s.win.join('x') : ''}`) return;       // a 409: a full client sets the size; ask again once the window changes
    if (tile.fit.key === key && (tile.fit.tries >= Quad.FIT_TRIES || now - tile.fit.at < Quad.FIT_REPEAT_MS)) return;
    tile.fit.tries = tile.fit.key === key ? tile.fit.tries + 1 : 1;
    tile.fit.key = key;
    tile.fit.at = now;
    const winKey = Array.isArray(s.win) ? s.win.join('x') : '';
    Promise.resolve(api('POST', `/api/sessions/${enc(tile.tmux)}/resize`, { cols: need.cols, rows: need.rows }))
      .catch((e) => { if (e && e.status === 409) tile.fit.blocked = `${key}|${winKey}`; });
  }

  function patchSize(tile) {
    const info = Quad.sizeInfo(tile.session && tile.session.win, tile.dims);
    setTextIfChanged(tile.size, info.text);
    tile.size.classList.toggle('hidden', !info.text);
    tile.size.classList.toggle('cropped', info.cropped);
    tile.size.setAttribute('title', info.title);
  }

  function reloadFrame(tile) {
    const f = tile.frame;
    if (!f) return false;
    let done = false;
    try { const w = f.contentWindow; if (w && w.location && typeof w.location.reload === 'function') { w.location.reload(); done = true; } } catch (_) { done = false; }
    if (!done && tile.url) f.src = tile.url;                          // the same address again is a navigation too
    return true;
  }

  function reconnect(tile) {
    if (tile.gone) { tile.missing = 0; tile.gone = false; mountBody(tile); return; }
    if (tile.mode === 'tail') { dropTail(tile); ensureTail(tile); return; }
    if (!reloadFrame(tile)) mountBody(tile);
  }

  function reloadAll() {
    let n = 0;
    for (const t of I.tiles.values()) { reconnect(t); n += 1; }
    return n;
  }

  /* ----- the active tile and the host key bar ----- */
  function setActive(tmux) {
    if (I.active === tmux) return;
    I.active = tmux;
    for (const t of I.tiles.values()) t.node.classList.toggle('active', t.tmux === tmux);
    patchKeys();
  }

  function focusTile(tile) {
    if (!tile) return false;
    touched();
    setActive(tile.tmux);
    try {
      if (tile.frame) {
        if (typeof tile.frame.focus === 'function') tile.frame.focus();
        const t = tile.bound ? tile.bound.term() : null;
        if (t && typeof t.focus === 'function') t.focus();
      } else if (typeof tile.node.focus === 'function') tile.node.focus();
    } catch (_) { /* a frame that is not ready */ }
    return true;
  }

  async function sendKeys(keys) {
    const tmux = I.active;
    if (!tmux) { note('No tile is selected'); return; }
    try { await api('POST', `/api/sessions/${enc(tmux)}/keys`, { keys }); } catch (e) { note(e.message); }
  }

  async function sendText(text, enter) {
    const tmux = I.active;
    if (!tmux) return;
    try { await api('POST', `/api/sessions/${enc(tmux)}/keys`, { text, enter: !!enter }); } catch (e) { note(e.message); }
  }

  async function scrollActive(dir) {
    const tmux = I.active;
    if (!tmux) return;
    try { await api('POST', `/api/sessions/${enc(tmux)}/scroll`, { dir, n: 1 }); } catch (e) { note(e.message); }
  }

  /* The host key bar: the person's own choice first; else on touch and in one-up, except with four tiles, where it stays away until a tile has really been used
     (a tap, a key in a terminal, a shortcut: not the default active tile, not ttyd focusing itself on load): four small terminals need the room more. */
  function keysWanted() {
    const v = stored(Quad.KEYS_PREF);
    if (v === '1') return true;
    if (v === '0') return false;
    if (I.n >= 4) return I.focused;
    return coarse() || I.oneUp;
  }

  /* A real interaction with a tile: the key bar may show in 4-up. */
  function touched() {
    if (I.focused) return;
    I.focused = true;
    patchKeys();                                                      // a bar that appears shrinks the grid: every tile refits through its own ResizeObserver
  }

  function patchKeys() {
    if (!I.keysHost) return;
    const on = keysWanted() && !I.noScope;
    I.keysHost.classList.toggle('hidden', !on);
    I.keysBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    const t = I.tiles.get(I.active) || null;
    setTextIfChanged(I.keysTo, t ? `keys to ${Quad.label(t.tmux)}` : 'keys: no tile selected');
    I.keysHost.classList.toggle('off', !t || t.mode === 'ro' || t.mode === 'tail');
  }

  /* ----- slots ----- */
  function assign(slot, tmux) {
    if (!(slot >= 0 && slot < Quad.SLOTS) || (tmux && !Quad.keyOk(tmux))) return false;
    const j = tmux ? I.slots.indexOf(tmux) : -1;
    if (j === slot) return true;
    const old = I.slots[slot];
    I.slots[slot] = tmux;
    if (j >= 0) I.slots[j] = old;                                    // a swap: both tiles stay mounted, only their data-slot changes
    if (tmux) I.manualEmpty.delete(slot); else I.manualEmpty.add(slot);
    if (j >= 0 && !old) I.manualEmpty.add(j);
    if (I.zoom !== null && !I.slots[I.zoom]) I.zoom = null;
    changed();
    return true;
  }

  /* A session from outside (the dock, a chip): the first free visible slot, else the active tile's, else slot 0. */
  function pick(tmux) {
    if (!Quad.keyOk(tmux)) return false;
    if (I.slots.indexOf(tmux) >= 0 && I.slots.indexOf(tmux) < I.n) return focusTile(tileAt(I.slots.indexOf(tmux))) || true;
    let slot = I.slots.findIndex((t, i) => !t && i < I.n);
    if (slot < 0) { const a = I.tiles.get(I.active); slot = a ? a.slot : 0; }
    return assign(slot, tmux);
  }

  function close(tile) { assign(tile.slot, ''); }

  function zoomSlot(slot) {
    if (I.n < 2 || !(slot >= 0 && slot < I.n) || !I.slots[slot]) return false;
    I.zoom = I.zoom === slot ? null : slot;
    changed();
    return true;
  }

  function zoomActive() { const t = I.tiles.get(I.active); return t ? zoomSlot(t.slot) : false; }

  function focusSlot(i) {
    if (!(i >= 0 && i < I.n)) return false;
    const t = tileAt(i);
    if (!t) return false;
    if (I.zoom !== null && I.zoom !== i) { I.zoom = i; changed(); }      // zoomed on another tile: the overlay moves to this one (a covered tile cannot take focus)
    return focusTile(t);
  }

  /* Ctrl+Alt+K: the next session that needs you; one that is not on screen takes the active tile's slot (or one that does not need you). */
  function goAttention() {
    if (!I.st) return false;
    const next = Quad.attentionNext(I.st, I.project, I.active);
    if (!next) { note('Nothing needs you right now', 'info'); return false; }
    let slot = I.slots.indexOf(next);
    if (slot < 0 || slot >= I.n) {
      const calm = (t) => { const s = sessionOf(t); return !s || !Quad.needsYou(s, I.st); };
      const a = I.tiles.get(I.active);
      slot = a && calm(a.tmux) ? a.slot : visibleSlots().findIndex((t) => !t || calm(t));
      if (slot < 0) slot = a ? a.slot : 0;
      assign(slot, next);
    }
    return focusSlot(slot);
  }

  function autoFillAll() {
    I.manualEmpty.clear();
    I.slots = Quad.fillSlots(I.slots, I.st, I.n, { project: I.project });
    changed();
  }

  /* ----- tiles ----- */
  function buildTile(tmux) {
    const tile = { tmux, slot: -1, mode: '', node: null, body: null, frame: null, bound: null, url: '', pre: null, tailOff: null, note: null, ro: null, measureTimer: null,
      dims: null, session: null, perm: null, permSig: '', decided: null, sig: '', missing: 0, gone: false, zoomed: false, fit: { key: '', at: 0, tries: 0, blocked: '' }, modeBtns: [],
      kit: hasKit('tileMenu'), menuCtl: null, composerCtl: null, tuneCtl: null, docked: false, head: null, dock: null };
    const label = Quad.label(tmux);
    tile.glyphs = el('span', { class: 'qt-glyphs' });
    const [proj, repo, sname] = Quad.parts(tmux);
    // two spans so a narrow header drops the project/repo first and keeps the session name: 'phasezero/website · ' shrinks, 't-checkout-redesign' stays
    tile.where = el('span', { class: 'qt-where' + (repo ? ' pend' : ''), text: repo ? `${proj}${repo === 'root' ? '' : '/' + repo} · ` : '' });      // .pend: hidden until TermKit.fitName has measured it, so no sliver flashes at first paint
    tile.name = el('span', { class: 'qt-name' }, tile.where, el('span', { class: 'qt-sess', text: repo ? sname : tmux }));
    // the name and its ▾ open the tile's view dropdown (TermKit.tileMenu: view, input, tune, session); a kit without it (slice B not loaded) keeps the old title menu
    tile.title = el('button', { class: 'minimal small qt-title', type: 'button', title: tile.kit ? `${tmux}: view, input and tune options` : `${tmux}: swap, reconnect, close`, 'aria-label': `${label}: tile menu`, 'aria-haspopup': 'menu' },
      tile.name, el('span', { class: 'qt-caret', 'aria-hidden': 'true', text: '▾' }));
    if (tile.kit) tile.title.addEventListener('click', (e) => toggleTileMenu(tile, !!(e && e.detail === 0 && e.isTrusted)));
    else if (typeof menu === 'function') menu(tile.title, () => titleItems(tile));
    // the mode picker: a 4-way segmented control from 366 px up (a compact one under 460), one menu button under that (pages.css picks; both are always here)
    tile.modeBtns = Quad.MODES.map((m) => el('button', { class: 'seg-btn qt-mode', type: 'button', 'data-mode': m, 'aria-pressed': 'false', title: Quad.MODE_TITLE[m], text: m, onclick: () => setMode(tile, m) }));
    tile.modes = el('div', { class: 'seg-ctl qt-modes', role: 'group', 'aria-label': 'View mode' }, ...tile.modeBtns);
    tile.modeLabel = el('span', { class: 'qt-modelabel', text: 'grid' });
    tile.modeMenu = el('button', { class: 'small qt-modemenu', type: 'button' }, tile.modeLabel, el('span', { class: 'qt-caret', 'aria-hidden': 'true', text: '▾' }));
    if (typeof menu === 'function') menu(tile.modeMenu, () => Quad.MODES.map((m) => ({ label: Quad.MODE_TITLE[m], icon: m === tile.mode ? 'tick' : 'blank', onClick: () => setMode(tile, m) })));
    tile.ctx = el('span', { class: 'qt-ctx hidden', title: 'context window used' });
    tile.size = el('span', { class: 'qt-size hidden' });
    tile.zoomBtn = el('button', { class: 'icon minimal small qt-zoom', type: 'button', 'aria-label': 'Zoom this tile', title: 'Zoom this tile (Ctrl+Alt+Z)', 'aria-pressed': 'false', onclick: () => zoomSlot(tile.slot) }, ic('maximize'));
    // the link pops the terminal out into its own window: the dock never takes it (data-dock=skip; the dock is suspended on this page anyway)
    const open = el('a', { class: 'btn icon minimal small qt-open', href: `/term/${enc(tmux)}`, target: '_blank', rel: 'noopener', 'data-dock': 'skip', 'data-standalone': 'skip', 'aria-label': 'Open the terminal page', title: 'Open the terminal page' }, ic('share'));
    const again = el('button', { class: 'icon minimal small qt-reconnect', type: 'button', 'aria-label': 'Reconnect', title: 'Reconnect this tile', onclick: () => reconnect(tile) }, ic('refresh'));
    const shut = el('button', { class: 'icon minimal small qt-close', type: 'button', 'aria-label': 'Close this tile', title: 'Close this tile', onclick: () => close(tile) }, ic('cross'));
    tile.hooks = el('span', { class: 'qt-hooks hidden' });       // #96: 'no hooks (untrusted?)' while hooks_missing (patchTile)
    const head = el('header', { class: 'qt-head' }, tile.glyphs, tile.title, tile.hooks, tile.modes, tile.modeMenu, tile.ctx, tile.size, tile.zoomBtn, open, again, shut);
    tile.head = head;
    tile.task = el('div', { class: 'qt-task hidden' });
    tile.permText = el('span', { class: 'qt-perm-text' });
    tile.permBtns = el('div', { class: 'qt-perm-btns' });
    tile.permNode = el('div', { class: 'qt-perm hidden' }, tile.permText, tile.permBtns);
    tile.body = el('div', { class: 'qt-body' });
    tile.dock = el('div', { class: 'qt-dock hidden' });                 // the docked composer, from 520 px (pages.css), when 'Show composer' is ticked
    const kp = Ref.splitKey(tmux) || { node: null, tmux };               // tile key -> the name and the node a drop target needs (dnd.js): the tile key is never a name by itself
    tile.node = el('article', { class: 'qtile', 'data-tmux': kp.tmux, 'data-node': kp.node, 'data-slot': '-1', 'data-mode': '', 'data-drop': 'session', tabindex: '-1', 'aria-label': label }, head, tile.task, tile.permNode, tile.body, tile.dock);
    tile.node.addEventListener('pointerdown', () => { touched(); setActive(tmux); }, true);
    tile.node.addEventListener('focusin', () => setActive(tmux));                // ttyd focuses its own terminal on load: that is no use of the tile
    if (typeof Dnd !== 'undefined' && typeof Dnd.bind === 'function') Dnd.bind(tile.node, { drop: 'session', tmux: kp.tmux, node: kp.node });      // a backlog card dropped on the tile is handed to the session
    else if (typeof Lazy !== 'undefined') Lazy.later('dnd', () => { if (typeof Dnd !== 'undefined') Dnd.bind(tile.node, { drop: 'session', tmux: kp.tmux, node: kp.node }); });     // dnd.js is lazy (lazy.js)
    if (typeof ResizeObserver === 'function') {
      tile.ro = new ResizeObserver((entries) => {
        const r = entries && entries[0] && entries[0].contentRect;
        if (r && !(r.width > 0 && r.height > 0)) return;                    // a hidden or collapsed tile must never be fitted to nothing
        if (typeof TermKit !== 'undefined' && TermKit && typeof TermKit.fitName === 'function') TermKit.fitName(tile.where);       // a sliver of project/repo goes; the session name stays
        fitFrame(tile);
      });
      tile.ro.observe(tile.body);
    }
    return tile;
  }

  function createTile(tmux) {
    const tile = buildTile(tmux);
    I.tiles.set(tmux, tile);
    I.grid.append(tile.node);                                       // appended, never inserted: an iframe that moves in the DOM reloads
    tile.mode = modeFor(tmux);
    tile.gone = known() && !sessionOf(tmux);
    tile.node.classList.toggle('gone', tile.gone);
    mountBody(tile);
    const s = sessionOf(tmux);
    if (s) patchTile(tile, s);
    return tile;
  }

  function teardownTile(tile) {
    if (tile.measureTimer !== null) { clearTimeout(tile.measureTimer); tile.measureTimer = null; }
    if (I.menuTile === tile.tmux) closeMenu();
    if (I.pop === tile.composerCtl || I.pop === tile.tuneCtl) I.pop = null;
    dropCtls(tile);
    if (tile.ro) { try { tile.ro.disconnect(); } catch (_) { /* gone */ } tile.ro = null; }
    dropTail(tile);
    dropFrame(tile);
    try { tile.node.remove(); } catch (_) { /* detached */ }
    I.tiles.delete(tile.tmux);
    if (I.active === tile.tmux) { I.active = ''; patchKeys(); }
  }

  /* ----- the tile's view dropdown (TermKit.tileMenu) and what its items do ----- */
  function hasKit(name) { return typeof TermKit !== 'undefined' && !!TermKit && typeof TermKit[name] === 'function'; }

  /* One surface at a time: the tile menu (I.menu) and the composer or tune panel it leads to (I.pop). Opening a menu closes everything; opening a panel closes the other panel
     but NOT the menu it was picked from: the menu closes itself around the action (a sheet after it, so a sheet opened by the action is swapped in place, not wiped by the
     close event of the one before). */
  function closeMenu() {
    const m = I.menu;
    I.menu = null;
    I.menuTile = null;
    if (m && typeof m.close === 'function') { try { m.close(); } catch (e) { console.error('ccboard quad menu', e); } }
  }

  function closePop() {
    const p = I.pop;
    I.pop = null;
    if (p && typeof p.close === 'function') { try { p.close(); } catch (e) { console.error('ccboard quad popover', e); } }
  }

  function closeAll() { closeMenu(); closePop(); }

  function toggleTileMenu(tile, byKeyboard) {
    const cur = tile.menuCtl;
    if (cur && I.menu === cur && isOpen(cur)) { closeMenu(); return false; }                      // a second tap on the ▾ closes it
    closeAll();
    let ctl = null;
    try { ctl = TermKit.tileMenu(menuCtx(tile)); } catch (e) { console.error('ccboard quad menu', e); return false; }
    if (!ctl || typeof ctl.open !== 'function') return false;
    tile.menuCtl = ctl;
    I.menu = ctl;
    I.menuTile = tile.tmux;
    ctl.open(tile.title, !!byKeyboard);                              // a pointer highlights nothing; the keyboard focuses the first item (the menu's rule)
    return true;
  }

  /* What TermKit.tileMenu gets, built fresh at each open (the row, the pending permission and whether the prompt is free change with every poll). Items whose action is null
     are left out by the menu. Optional extras the menu knows: `schema` (the agent's registry, for which tune items exist), `zoomed` (Zoom says 'Back to the grid'),
     `composerDocked` + actions.dockComposer (the 'Show composer' tick) and `keysTarget` (the 'Keys here' tick). */
  function menuCtx(tile) {
    const s = tile.session || sessionOf(tile.tmux) || null;
    const agent = s ? Quad.agentOf(s) : 'claude';
    const gate = Quad.atPrompt(s, I.st);
    const live = !tile.gone && !!s;
    const pr = live && tile.perm ? tile.perm : null;
    const tune = live && gate.show;
    const actions = {
      setMode: (m) => setMode(tile, m),
      zoom: I.n >= 2 ? () => zoomSlot(tile.slot) : null,
      fullscreenTile: () => fullscreenTile(tile),
      popout: () => popOut(tile),
      openTerm: live ? () => openTerminal(tile) : null,
      dock: live && dockPossible() ? () => dockTile(tile) : null,
      reload: () => reconnect(tile),
      keysHere: live ? () => keysHere(tile) : null,
      allow: pr ? () => decide(tile, pr, 'allow') : null,
      deny: pr ? () => decide(tile, pr, 'deny') : null,
      tui: pr ? () => decide(tile, pr, 'tui') : null,
      composer: live && hasKit('composer') ? () => openComposer(tile) : null,
      dockComposer: live && hasKit('composer') && !tooNarrowForDock(tile) ? () => toggleDocked(tile) : null,
      tune: tune && hasKit('tune') ? () => openTune(tile) : null,
      compact: tune && hasKit('tune') ? () => tuneRun(tile, 'compact') : null,
      context: tune && hasKit('tune') ? () => tuneRun(tile, 'context') : null,
      usage: tune && hasKit('tune') ? () => tuneRun(tile, 'usage') : null,
      rename: tune && hasKit('tune') ? () => tuneRename(tile) : null,
      autoContinue: live && agent !== 'shell' && typeof setAutoContinue === 'function' ? () => tileAutoContinue(tile) : null,
      close: () => close(tile),
      kill: live ? () => kill(tile) : null,
    };
    return {
      tmux: tile.tmux, session: s, agent, mode: tile.mode, modes: Quad.MODES.slice(), touch: coarse(), actions,
      perm: { pending: !!pr, summary: pr ? String(pr.summary || pr.tool_name || '') : '' },
      atPrompt: gate.ok, why: gate.ok ? '' : (gate.show ? gate.why : ''), schema: schemaOf(agent),
      zoomed: I.n >= 2 && I.zoom === tile.slot, composerDocked: !!I.composers[tile.tmux], keysTarget: I.active === tile.tmux && keysShown(),
    };
  }

  /* #84 Auto-continue: the menu's switch. Optimistic on the tile's own copy of the row (the next poll replaces it), put back when the board refuses; the answer's
     read-back and the toast are setAutoContinue's (core.js). */
  function tileAutoContinue(tile) {
    const s = tile.session || sessionOf(tile.tmux) || null;
    if (!s) return false;
    setAutoContinue(tile.tmux, !sessionAutoContinueOff(s), { apply: (off) => {
      const f = Object.assign({}, s.flags || {});
      if (off) f.no_autoresume = true; else delete f.no_autoresume;
      s.flags = f;
    } });
    return true;
  }

  /* The docked composer is hidden under 520 px (pages.css, a container query): a menu row that ticks and shows nothing would lie, so it is left out there. An unmeasured tile (0) is not narrow. */
  function tooNarrowForDock(tile) {
    const w = Number(tile && tile.node && tile.node.offsetWidth);
    return w > 0 && w < Quad.DOCK_MIN;
  }

  function dockPossible() {
    try { return typeof Shell !== 'undefined' && !!Shell && typeof Shell.dockOn === 'function' && !!Shell.dockOn(); } catch (_) { return false; }
  }

  /* The dock and the quad are exclusive (shell.js: the quad owns the window), so 'Add to dock' keeps this session for the dock: it opens there when the Quad is left. */
  function dockTile(tile) {
    try {
      if (typeof Shell === 'undefined' || !Shell || !Shell.dock) return false;
      Shell.dock.wanted = tile.tmux;
      if (Shell.dockStore && typeof Shell.dockStore.set === 'function') Shell.dockStore.set('ccboard:dock', tile.tmux);
    } catch (e) { console.error('ccboard quad dock', e); return false; }
    note(`${Quad.label(tile.tmux)} opens in the dock when you leave the Quad`, 'info');
    return true;
  }

  /* Pop out: the terminal page in a window of its own beside the board (a tab when the browser blocks it, and inside an installed app). */
  function popOut(tile) {
    const url = `/term/${enc(tile.tmux)}`;
    let win = null;
    try {
      const app = typeof isStandalone === 'function' && isStandalone();
      if (!app && typeof window.open === 'function') win = window.open(url, `ccboard-term-${tile.tmux}`, 'popup=yes,width=1000,height=680');
    } catch (_) { win = null; }
    if (!win && typeof openPage === 'function') openPage(url);
    return true;
  }

  /* Open in terminal: the board's own way (the dock when it is on and free, else the terminal page in a new tab). */
  function openTerminal(tile) {
    if (typeof Shell !== 'undefined' && Shell && typeof Shell.openTerm === 'function') { Shell.openTerm(tile.tmux); return true; }
    if (typeof openPage === 'function') openPage(`/term/${enc(tile.tmux)}`);
    return true;
  }

  /* Keys here: the host key bar is shown and aims at this tile; the menu's tick says so, and a second pick hides the bar again. */
  function keysShown() { return keysWanted() && !I.noScope; }

  function keysHere(tile) {
    const off = I.active === tile.tmux && keysShown();
    if (!off) { touched(); setActive(tile.tmux); }
    try { localStorage.setItem(Quad.KEYS_PREF, off ? '0' : '1'); } catch (_) { /* storage may be unavailable */ }
    patchKeys();
    fitAll();
    return !off;
  }

  /* Fullscreen this tile: zoom it over the grid (nothing to zoom in one-up) and go full screen; leaving full screen puts the grid back when this call zoomed it. */
  function fullscreenTile(tile) {
    if (I.n >= 2 && I.zoom !== tile.slot) {
      if (I.zoom === null) I.fsZoom = true;
      I.zoom = tile.slot;
      changed();
    }
    setActive(tile.tmux);
    enterFullscreen();
    return true;
  }

  /* ----- composer and tune: TermKit's components, built once per tile and kept current ----- */
  const isOpen = (c) => !!c && (typeof c.isOpen === 'function' ? c.isOpen() : !!c.isOpen);

  function schemaOf(agent) { return (I.st && I.st.agents && typeof I.st.agents === 'object' && I.st.agents[agent]) || null; }

  /* The composer is built when first wanted and lives as long as the tile (a draft survives a close); its popover and the docked box are the same controller. */
  function getComposer(tile) {
    if (tile.composerCtl) return tile.composerCtl;
    if (!hasKit('composer')) return null;
    const s = tile.session || sessionOf(tile.tmux) || null;
    try { tile.composerCtl = TermKit.composer({ tmux: tile.tmux, session: s, agent: s ? Quad.agentOf(s) : 'claude', touch: coarse(), onSent: () => { setActive(tile.tmux); } }); } catch (e) { console.error('ccboard quad composer', e); }
    return tile.composerCtl || null;
  }

  /* The tune panel's controller, with the row, the stats and the prompt gate as of now (its values are read from them). */
  function getTune(tile) {
    const s = tile.session || sessionOf(tile.tmux) || null;
    if (!s || !hasKit('tune')) return null;
    const agent = Quad.agentOf(s);
    const ctx = { session: s, agent, stats: s.stats || {}, atPrompt: Quad.atPrompt(s, I.st).ok, touch: coarse(), schema: schemaOf(agent),
      onRestart: () => { setTimeout(() => { if (!tile.gone) reconnect(tile); }, 1500); } };    // a restarted Codex session is a new tmux session of the same name: the tile attaches again
    if (!tile.tuneCtl) { try { tile.tuneCtl = TermKit.tune({ tmux: tile.tmux, ...ctx }); } catch (e) { console.error('ccboard quad tune', e); } }
    else if (typeof tile.tuneCtl.update === 'function') tile.tuneCtl.update(ctx);
    return tile.tuneCtl || null;
  }

  /* The poll brought a new row: the composer's queue note and an open tune panel follow it. */
  function patchCtls(tile, s) {
    if (tile.composerCtl && typeof tile.composerCtl.update === 'function') tile.composerCtl.update({ session: s, agent: Quad.agentOf(s) });
    if (tile.tuneCtl && isOpen(tile.tuneCtl)) getTune(tile);
  }

  function dropCtls(tile) {
    for (const c of [tile.composerCtl, tile.tuneCtl]) {                  // destroy() when the kit has it: it also cancels the pending-setting timer (no toast about a dead tile) and takes the docked box out
      const end = c && (typeof c.destroy === 'function' ? c.destroy : (typeof c.close === 'function' ? c.close : null));
      if (end) { try { end.call(c); } catch (e) { console.error('ccboard quad popover', e); } }
    }
    tile.composerCtl = null;
    tile.tuneCtl = null;
    tile.docked = false;
    if (tile.dock) { tile.dock.textContent = ''; tile.dock.classList.add('hidden'); }
  }

  /* Send a prompt…: the composer as a popover under the tile header (a bottom sheet on touch). */
  function openComposer(tile) {
    const c = getComposer(tile);
    if (!c || typeof c.open !== 'function') return false;
    closePop();
    const s = tile.session || sessionOf(tile.tmux) || null;
    if (typeof c.update === 'function') c.update({ session: s, agent: s ? Quad.agentOf(s) : 'claude', touch: coarse() });      // the row as of now, and the pointer as of now (a sheet or a popover)
    I.pop = c;
    c.open(tile.head);
    return true;
  }

  /* Tune…: model, effort, fast, ultracode and the command cells. */
  function openTune(tile) {
    const t = getTune(tile);
    if (!t || typeof t.open !== 'function') return false;
    closePop();
    I.pop = t;
    t.open(tile.head);
    return true;
  }

  /* /compact, /context, /usage: the tune panel's own run() types the command and shows what a read command printed (a plain Tune… when a kit has no run()). */
  function tuneRun(tile, cmd) {
    const t = getTune(tile);
    if (!t) return false;
    if (typeof t.run !== 'function') return openTune(tile);
    closePop();
    t.run(cmd, '', { anchor: tile.head });
    return true;
  }

  /* Rename…: the tune panel's rename row (a plain Tune… when a kit has none). */
  function tuneRename(tile) {
    const t = getTune(tile);
    if (!t) return false;
    if (typeof t.rename !== 'function') return openTune(tile);
    closePop();
    I.pop = t;
    t.rename(tile.head);
    return true;
  }

  /* Show composer: the one-line composer docked at the bottom of this tile (tiles 520 px wide and up), remembered with the tile's mode. */
  function toggleDocked(tile) {
    if (I.composers[tile.tmux]) delete I.composers[tile.tmux]; else I.composers[tile.tmux] = true;
    patchDock(tile);
    persist();
    return !!I.composers[tile.tmux];
  }

  function patchDock(tile) {
    const c = I.composers[tile.tmux] && !tile.gone ? getComposer(tile) : null;
    if (c && c.el) {
      if (c.el.parentNode !== tile.dock) { tile.dock.textContent = ''; tile.dock.append(c.el); }
      tile.docked = true;
    } else if (tile.docked) {
      tile.docked = false;
      if (tile.composerCtl && tile.composerCtl.el) tile.composerCtl.el.remove();
      tile.dock.textContent = '';
    }
    tile.dock.classList.toggle('hidden', !tile.docked);
  }

  function settleSoon() { if (typeof poll === 'function') { try { Promise.resolve(poll(true)).catch(() => {}); } catch (_) { /* the next tick */ } } }

  /* Kill session: the end of the session itself (the menu asks twice, through confirmButton); the tile is emptied and the poll catches up. */
  async function kill(tile) {
    try { await api('DELETE', `/api/sessions/${enc(tile.tmux)}`); } catch (e) { note(e.message); return false; }
    note(`${Quad.label(tile.tmux)} ended`, 'ok');
    if (!I.disposed && I.tiles.get(tile.tmux) === tile) close(tile);
    settleSoon();
    return true;
  }

  /* ----- fullscreen ----- */
  function fsActive() {
    try { return typeof document !== 'undefined' && !!(document.fullscreenElement || document.webkitFullscreenElement); } catch (_) { return false; }
  }

  function setFsClass(on) {
    try { if (typeof document !== 'undefined' && document.documentElement) document.documentElement.classList.toggle('quad-fs', !!on); } catch (_) { /* no document element */ }
  }

  /* The page has gone full screen or come back: the head says so and every tile looks at its size again (the observers do it too, a hidden chrome resizes them). */
  function fsChanged() {
    patchHead();
    if (typeof window !== 'undefined') onWindowSize();
  }

  /* html.quad-fs first (it alone does the job where the browser has no Fullscreen API: iOS Safari), then the browser's full screen on the document element: the topbar's
     popovers (components.menu appends them there) and the sheet still paint, which they would not if only the quad root were full screen. */
  function enterFullscreen() {
    if (I.fs) return true;
    if (I.disposed || I.noScope) return false;                        // a project with nothing to show has no header with an Exit button
    I.fs = true;
    I.fsApi = false;
    setFsClass(true);
    closeAll();
    try {
      const root = typeof document !== 'undefined' ? document.documentElement : null;
      const req = root && (root.requestFullscreen || root.webkitRequestFullscreen);
      if (typeof req === 'function') {
        I.fsApi = true;
        const p = req.call(root, { navigationUI: 'hide' });
        if (p && typeof p.catch === 'function') p.catch(() => { I.fsApi = false; });             // refused (no gesture, an iframe policy): the class alone stands
      }
    } catch (_) { I.fsApi = false; }
    fsChanged();
    return true;
  }

  function leaveFullscreen() {
    if (!I.fs) return false;
    I.fs = false;
    setFsClass(false);
    if (fsActive()) {
      try {
        const out = document.exitFullscreen || document.webkitExitFullscreen;
        const p = typeof out === 'function' ? out.call(document) : null;
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (_) { /* already out */ }
    }
    I.fsApi = false;
    if (I.fsZoom) { I.fsZoom = false; if (I.zoom !== null && !I.disposed) { I.zoom = null; changed(); } }       // Fullscreen this tile zoomed it: the grid comes back with the page
    if (!I.disposed) fsChanged();
    return true;
  }

  function toggleFullscreen() { return I.fs ? leaveFullscreen() : enterFullscreen(); }

  /* The browser left full screen on its own (Esc, a gesture): the class goes with it. */
  function onFullscreenChange() { if (I.fs && I.fsApi && !fsActive()) leaveFullscreen(); }

  /* Esc leaves the in-page fullscreen (the browser's own handles its Esc itself); never with a menu, a popover, a sheet or a field that has its own use for it. */
  function onEscape(e) {
    if (!I.fs || I.disposed || !e || e.key !== 'Escape' || e.defaultPrevented) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(?:INPUT|TEXTAREA|SELECT)$/.test(String(t.tagName || '')))) return;       // a field's Esc is the field's own
    if (isOpen(I.menu) || isOpen(I.pop)) return;                      // the tile menu, the composer or the tune panel is up: its Esc closes it, not the page
    try { if (document.querySelector('dialog[open], .menu-pop')) return; } catch (_) { /* no document */ }
    if (typeof e.preventDefault === 'function') e.preventDefault();
    leaveFullscreen();
  }

  /* The old title menu, kept for a kit without TermKit.tileMenu. */
  function titleItems(tile) {
    // everything the header sheds as the tile narrows is here: the menu is the one place that always has it
    const items = [
      { label: 'Open terminal page', icon: 'share', onClick: () => { if (typeof openPage === 'function') openPage(`/term/${enc(tile.tmux)}`); } },
      { label: 'Reconnect', icon: 'refresh', onClick: () => reconnect(tile) },
    ];
    if (I.n >= 2) items.push({ label: tile.slot === I.zoom ? 'Back to the grid' : 'Zoom this tile', icon: tile.slot === I.zoom ? 'minimize' : 'maximize', onClick: () => zoomSlot(tile.slot) });
    items.push({ label: 'Close tile', icon: 'cross', onClick: () => close(tile) });
    const here = new Set(visibleSlots().filter(Boolean));
    /* Swap decision (v0.5.21): this fallback menu (no TermKit kit) keeps its swap rows; the kit menu does not list them, because an empty tile's pick rows and Close tile already
       move a session between tiles in two taps. A "Swap in..." row comes back only if those prove too little. */
    for (const s of Quad.candidates(I.st, I.project).filter((x) => !here.has(Ref.key(x))).slice(0, 10)) {
      items.push({ label: `Swap in ${Quad.label(Ref.key(s))}`, onClick: () => assign(tile.slot, Ref.key(s)) });
    }
    return items;
  }

  /* The line a pending permission gets: its summary, Allow / Deny / In terminal. The buttons are there whenever the request is still in pending_permissions: POST allow works
     whether or not a terminal is attached (a request that arrives while a full client is attached is already settled by the hook and never listed). The viewers count only
     drives the note: another full client than this tile's own (a full tile's iframe is one itself). */
  function patchPerm(tile, pr, s) {
    const full = Number((s && s.viewers && s.viewers.full) || 0) - (tile.mode === 'full' && tile.frame ? 1 : 0);
    const other = full > 0;
    const sig = pr && tile.decided !== pr.id ? `${pr.id}|${pr.summary || ''}|${pr.tool_name || ''}|${other ? 1 : 0}` : '';
    if (tile.permSig === sig) return;
    tile.permSig = sig;
    tile.permBtns.textContent = '';
    tile.permText.textContent = '';
    tile.perm = sig ? pr : null;                                    // the request the line shows (a decided one is gone)
    if (!sig) { tile.permNode.classList.add('hidden'); tile.permText.removeAttribute('title'); return; }
    const sum = String(pr.summary || pr.tool_name || 'permission request').slice(0, 300);
    tile.permText.append(el('span', { class: 'qt-perm-sum', text: sum }));
    if (other) tile.permText.append(el('span', { class: 'dim qt-perm-note', text: 'a terminal is attached' }));
    tile.permText.setAttribute('title', other ? `${sum} (another terminal is attached to this session)` : sum);
    tile.permBtns.append(
      el('button', { class: 'primary tinted small qt-allow', type: 'button', onclick: () => decide(tile, pr, 'allow'), text: 'Allow' }),
      el('button', { class: 'danger small qt-deny', type: 'button', onclick: () => decide(tile, pr, 'deny'), text: 'Deny' }),
      el('button', { class: 'minimal small qt-tui', type: 'button', title: 'Let Claude show its own prompt in the terminal', onclick: () => decide(tile, pr, 'tui'), text: 'In terminal' }));
    tile.permNode.classList.remove('hidden');
  }

  async function decide(tile, pr, decision) {
    tile.decided = pr.id;                                           // the line goes at once; the next poll only confirms
    patchPerm(tile, pr, tile.session);
    let ok = true;
    try {
      await api('POST', `/api/permission/${pr.id}/${decision}`);
    } catch (e) {
      ok = false;
      tile.decided = null;
      tile.permSig = '';
      patchPerm(tile, Quad.perm(I.st, tile.tmux), tile.session);
      note(e.message);
    }
    if (ok && typeof setError === 'function') { try { setError(null); } catch (_) { /* no banner */ } }
    if (typeof poll === 'function') { try { await poll(true); } catch (_) { /* the next tick */ } }
  }

  function patchTile(tile, s) {
    tile.session = s;
    const st = I.st;
    const sig = `${s.state}|${Quad.agentOf(s)}`;
    if (tile.sig !== sig) {
      tile.sig = sig;
      tile.glyphs.textContent = '';
      const ag = agentGlyph(Quad.agentOf(s));
      if (typeof chipHue === 'function') ag.classList.add(chipHue('agent', Quad.agentOf(s)));
      tile.glyphs.append(stateGlyph(s.state), ag);
      tile.node.setAttribute('data-state', s.state || 'unknown');
      tile.node.setAttribute('data-agent', Quad.agentOf(s));
    }
    tile.node.classList.toggle('needs', Quad.needsYou(s, st));
    const ci = typeof TermKit !== 'undefined' && TermKit && typeof TermKit.ctxInfo === 'function' ? TermKit.ctxInfo(s.stats && s.stats.context_pct) : null;      // the dock's chip reads the same
    setTextIfChanged(tile.ctx, ci ? ci.text : '');
    tile.ctx.classList.toggle('hidden', !ci);
    tile.ctx.classList.toggle('hi', !!ci && ci.level === 'hi');
    tile.ctx.classList.toggle('crit', !!ci && ci.level === 'crit');
    if (ci) tile.ctx.setAttribute('title', ci.title);
    const hm = (s && s.hooks_missing) || '';                  // #96: the same chip as the Agents row, after the glyphs
    if (tile.hm !== hm && typeof hooksMissingChip === 'function') {
      tile.hm = hm;
      tile.hooks.textContent = '';
      if (hm) tile.hooks.append(hooksMissingChip(hm));
      tile.hooks.classList.toggle('hidden', !hm);
    }
    const line = Quad.taskLine(s);
    setTextIfChanged(tile.task, line.text);
    tile.task.classList.toggle('hidden', !line.text);
    tile.task.setAttribute('data-kind', line.kind);
    patchPerm(tile, Quad.perm(st, tile.tmux), s);
    patchSize(tile);
    patchCtls(tile, s);
  }

  /* A tile whose session has been missing for two polls in a row says so instead of showing a dead iframe; a blip (tmux down, an empty first poll) never does. */
  function patchGone(tile) {
    const here = !!sessionOf(tile.tmux);
    if (here) tile.missing = 0; else if (known()) tile.missing += 1;
    const gone = here ? false : (known() && tile.missing >= 2 ? true : tile.gone);
    tile.node.classList.toggle('gone', gone);
    if (gone !== tile.gone) { tile.gone = gone; mountBody(tile); }
  }

  /* ----- empty slots ----- */
  function ensureEmpty(slot) {
    let e = I.empties.get(slot);
    if (!e) {
      e = { slot, node: el('div', { class: 'qtile qempty', 'data-slot': String(slot) }), sig: '' };
      I.grid.append(e.node);
      I.empties.set(slot, e);
    }
    return e;
  }

  /* + New session in an empty tile: the launcher sheet (launch(), components.js) for the quad's project (the one in the address), else where the board worked last.
     onDone(response) is the launcher's hook for a session that started: the sheet closes and the new session takes this tile (open: false asks it not to open the
     terminal anywhere else: the tile is where it is shown). Without a place anywhere it is the repo picker. */
  function newSessionHere(slot) {
    const p = I.project && I.st ? (I.st.projects || []).find((x) => x.name === I.project) : null;
    const hit = typeof launchPlace === 'function' ? launchPlace(p || null, 'session') : null;
    const onDone = (res) => {
      if (typeof closeSheet === 'function') closeSheet();
      if (!I.disposed && res && typeof res.tmux === 'string' && res.tmux) assign(slot, res.tmux);
    };
    if (hit && typeof launch === 'function') return launch({ mode: 'session', project: hit.project, repo: hit.repo, slot, open: false, onDone });
    if (typeof Shell !== 'undefined' && Shell && typeof Shell.openCreate === 'function') return Shell.openCreate('session', I.project ? { project: I.project } : undefined);
    return false;
  }

  function patchEmpty(e) {
    const shown = I.slots.slice(0, I.n);                                                      // a session in a hidden slot is free for a visible tile (picking it moves it)
    const free = Quad.candidates(I.st, I.project).filter((s) => !shown.includes(Ref.key(s))).slice(0, 6);
    const sig = `${I.st ? 1 : 0}|${free.map((s) => `${Ref.key(s)}:${s.state}`).join(',')}`;
    if (e.sig === sig) return;
    e.sig = sig;
    e.node.textContent = '';
    e.node.append(el('div', { class: 'qe-title', text: !I.st ? 'Loading sessions…' : (free.length ? 'Pick a session for this tile' : (I.project ? `No other session in ${I.project}` : 'No other session is running')) }));
    if (!I.st) return;
    const list = el('div', { class: 'qe-list' });
    for (const s of free) {
      list.append(el('button', { class: 'small qe-pick', type: 'button', 'data-tmux': Ref.key(s), title: Quad.label(Ref.key(s)), onclick: () => assign(e.slot, Ref.key(s)) },
        stateGlyph(s.state), el('span', { class: 'qe-name', text: Quad.label(Ref.key(s)) })));      // the full name stays in the title: the row cuts it with an ellipsis
    }
    e.node.append(list, el('button', { class: 'small qe-new', type: 'button', title: 'start a new session for this tile', onclick: () => newSessionHere(e.slot) }, ic('plus'), 'New session'));
  }

  /* ----- the one-up chip switcher ----- */
  function chipLine(lines) {
    if (!Array.isArray(lines)) return '';
    for (let i = lines.length - 1; i >= 0; i--) {
      const t = quadOneLine(lines[i], 80);
      if (t) return t;
    }
    return '';
  }

  function chipTailOn(tmux) {
    if (I.chipTails.has(tmux) || typeof Live === 'undefined' || typeof Live.subscribe !== 'function') return;
    if ((Ref.splitKey(tmux) || {}).node) return;                       // Live streams this board's panes: a session of another node has no tail here
    const fn = (lines) => {
      if (Live.isDemo && Live.isDemo()) return;                       // the demo note is not output
      I.chipLines.set(tmux, chipLine(lines));
      const n = I.chipList && I.chipList.nodes.get(tmux);
      if (n && n.ccTail) setTextIfChanged(n.ccTail, I.chipLines.get(tmux) || '');
    };
    I.chipTails.set(tmux, Live.subscribe(tmux, fn));
  }

  function chipTailsOff(keep) {
    for (const [tmux, off] of Array.from(I.chipTails)) {
      if (keep && keep.has(tmux)) continue;
      I.chipTails.delete(tmux);
      I.chipLines.delete(tmux);
      try { off(); } catch (e) { console.error('ccboard quad chip', e); }
    }
  }

  function chipNode(s) {
    const tail = el('span', { class: 'q-chip-tail' });
    const glyphs = el('span', { class: 'q-chip-glyphs' });
    const node = el('button', { class: 'q-chip', type: 'button', role: 'tab', 'data-tmux': Ref.key(s), 'aria-selected': 'false', title: Ref.key(s), onclick: () => assign(0, Ref.key(s)) },
      glyphs, el('span', { class: 'q-chip-text' }, el('span', { class: 'q-chip-name', text: s.name || Quad.parts(s.tmux)[2] }), tail));
    node.ccGlyphs = glyphs;
    node.ccTail = tail;
    node.ccSig = '';
    return node;
  }

  function patchChip(node, s) {
    const sig = `${s.state}|${Quad.agentOf(s)}`;
    if (node.ccSig !== sig) {
      node.ccSig = sig;
      node.ccGlyphs.textContent = '';
      const ag = agentGlyph(Quad.agentOf(s));
      if (typeof chipHue === 'function') ag.classList.add(chipHue('agent', Quad.agentOf(s)));
      node.ccGlyphs.append(stateGlyph(s.state), ag);
    }
    node.classList.toggle('needs', Quad.needsYou(s, I.st));
    node.setAttribute('aria-selected', node.getAttribute('data-tmux') === I.slots[0] ? 'true' : 'false');
    setTextIfChanged(node.ccTail, I.chipLines.get(Ref.key(s)) || '');
  }

  function renderChips() {
    if (!I.chipsHost) return;
    I.chipsHost.classList.toggle('hidden', !I.oneUp || I.noScope);
    if (!I.oneUp || !I.st || I.noScope) { chipTailsOff(); return; }
    const list = Quad.candidates(I.st, I.project);
    const cur = I.slots[0] ? sessionOf(I.slots[0]) : null;
    if (cur && !list.some((s) => Ref.key(s) === Ref.key(cur))) list.unshift(cur);          // an ended session that is on screen stays a chip
    const shown = list.slice(0, Quad.CHIPS_MAX);
    const order = new Set(I.chipOrder);
    const present = new Set(shown.map((s) => Ref.key(s)));
    I.chipOrder = I.chipOrder.filter((t) => present.has(t));
    for (const s of shown) if (!order.has(Ref.key(s))) I.chipOrder.push(Ref.key(s));        // a chip keeps its place; a new one joins at its rank
    const byTmux = new Map(shown.map((s) => [Ref.key(s), s]));
    I.chipList.update(I.chipOrder.map((t) => byTmux.get(t)));
    chipTailsOff(present);
    for (const t of I.chipOrder) chipTailOn(t);
    const sel = I.chipList.nodes.get(I.slots[0]);
    if (sel && I.chipSel !== I.slots[0]) {
      I.chipSel = I.slots[0];
      try { if (typeof sel.scrollIntoView === 'function') sel.scrollIntoView({ inline: 'nearest', block: 'nearest' }); } catch (_) { /* no layout */ }
    }
  }

  /* ----- layout, sync and the head ----- */
  function relayout(force) {
    const L = Quad.layoutFor(viewportWidth(), I.layout, columnWidth());
    I.cap = L.cap;                                                     // the control and its caption follow the window even when no tile count changed
    I.reason = L.reason;
    if (!force && L.n === I.n && L.oneUp === I.oneUp && L.forced === I.forced) return false;
    const countChanged = L.n !== I.n;
    if (L.forced !== I.forced) I.reMode = true;                        // the one thing that changes a tile's default mode: the window going to or from 'one tile because it is too narrow'
    I.n = L.n; I.oneUp = L.oneUp; I.forced = L.forced;
    if (I.zoom !== null && (I.zoom >= I.n || I.n < 2)) I.zoom = null;
    if (countChanged && I.resolved && I.st) {                        // a new count (growth or shrink): visible slots start filled, the live sessions of hidden slots first, unless the person emptied them
      const only = [];
      for (let i = 0; i < I.n; i++) if (!I.slots[i] && !I.manualEmpty.has(i)) only.push(i);
      if (only.length) I.slots = Quad.fillSlots(I.slots, I.st, I.n, { project: I.project, only });
    }
    return true;
  }

  /* The pointer type, as of now: data-touch on the grid (pages.css: the touch tiers of the tile header, 44 px controls need more room) and, when it changed, every tile's
     components (v0.5.21, #52). The sync pass calls it, and so do the media-query and html.force-coarse watchers below, so a flip lands within a frame instead of at the next
     poll. A flip re-sources, reorders and restarts nothing; an open tile menu or panel was built for the old tier, so it closes (the person taps again). */
  function applyTouch() {
    if (I.disposed || !I.host) return;
    const touch = coarse();
    if (touch) I.host.setAttribute('data-touch', 'true'); else I.host.removeAttribute('data-touch');
    if (touch === I.touch) return;
    I.touch = touch;
    closeAll();
    for (const t of I.tiles.values()) {
      for (const c of [t.menuCtl, t.composerCtl, t.tuneCtl]) {
        if (c && typeof c.update === 'function') { try { c.update({ touch }); } catch (e) { console.error('ccboard quad touch', e); } }
      }
    }
    if (typeof TermKit !== 'undefined' && TermKit && typeof TermKit.fitName === 'function') for (const t of I.tiles.values()) TermKit.fitName(t.where);      // the header's pieces changed size: no tile body resized, so look at the titles again
  }

  function patchHead() {
    const h = I.host;
    h.setAttribute('data-layout', String(I.n));
    if (I.oneUp) h.setAttribute('data-oneup', 'true'); else h.removeAttribute('data-oneup');
    if (I.forced) h.setAttribute('data-forced', 'true'); else h.removeAttribute('data-forced');
    I.grid.setAttribute('data-layout', String(I.n));
    applyTouch();
    if (I.zoom !== null) I.grid.setAttribute('data-zoom', String(I.zoom)); else I.grid.removeAttribute('data-zoom');
    for (const b of I.layoutBtns) {                                      // the one that shows is pressed (a saved layout above the cap shows the cap); the ones the window cannot take are off
      const n = Number(b.getAttribute('data-layout'));
      b.setAttribute('aria-pressed', n === I.n ? 'true' : 'false');
      b.disabled = n > I.cap;
      b.setAttribute('title', layoutTitle(n));
    }
    I.layoutCtl.setAttribute('title', I.reason || 'How many terminals to show');
    setTextIfChanged(I.capNote, I.reason);
    I.capNote.classList.toggle('hidden', !I.reason || I.forced || I.noScope);
    patchFullscreenBtn();
    I.fillBtn.disabled = !I.st;
    I.grid.classList.toggle('hidden', I.noScope);
    I.actions.classList.toggle('hidden', I.noScope);
    patchScope();
    patchNone();
  }

  /* The title of one cell of the layout control: what it is, or why this window cannot take it. */
  function layoutTitle(n) {
    const [c, r] = Quad.GRID[n];
    if (n <= I.cap) return n === 1 ? 'One terminal' : (n === 2 ? 'Two side by side' : `${n} terminals, ${c} by ${r}`);
    return n > Quad.capFor(viewportWidth()) ? `${n} terminals need a window ${Quad.WIDTH_AT[n]} px wide or more` : `${n} terminals would be narrower than ${Quad.TILE_MIN} px here`;
  }

  function patchFullscreenBtn() {
    if (!I.fsBtn) return;
    I.fsBtn.classList.toggle('on', I.fs);                           // the label says which way it goes (Exit fullscreen): no aria-pressed on top of it
    setTextIfChanged(I.fsLabel, I.fs ? 'Exit fullscreen' : 'Fullscreen');
    I.fsBtn.setAttribute('title', I.fs ? 'Leave full screen (Esc, F)' : 'Fill the screen with the tiles (F)');
    if (I.fsShown !== I.fs) { I.fsShown = I.fs; I.fsIcon.textContent = ''; I.fsIcon.append(ic(I.fs ? 'minimize' : 'fullscreen')); }
  }

  function sync() {
    const want = new Map();
    for (let i = 0; i < I.n; i++) if (I.slots[i]) want.set(I.slots[i], i);
    for (const t of Array.from(I.tiles.values())) if (!want.has(t.tmux)) teardownTile(t);
    for (const [tmux, i] of want) {
      let t = I.tiles.get(tmux);
      if (!t) t = createTile(tmux);
      if (t.slot !== i) { t.slot = i; t.node.setAttribute('data-slot', String(i)); }
      if (I.reMode) { const m = modeFor(tmux); if (t.mode !== m) { t.mode = m; mountBody(t); } }      // a chosen mode is in I.modes and never changes here; a layout change never gets this far
    }
    I.reMode = false;
    I.noScope = emptyScope();
    if (I.noScope && I.fs) leaveFullscreen();                         // nothing to show: the header that has the Exit button is hidden too
    for (const [slot, e] of Array.from(I.empties)) {
      if (!I.noScope && slot < I.n && !I.slots[slot]) continue;
      e.node.remove();
      I.empties.delete(slot);
    }
    if (!I.noScope) for (let i = 0; i < I.n; i++) if (!I.slots[i]) patchEmpty(ensureEmpty(i));
    for (const t of I.tiles.values()) {
      const z = I.zoom !== null && t.slot === I.zoom;
      t.node.classList.toggle('zoomed', z);
      t.zoomBtn.setAttribute('aria-pressed', z ? 'true' : 'false');
      t.zoomBtn.setAttribute('title', z ? 'Back to the grid (Ctrl+Alt+Z)' : 'Zoom this tile (Ctrl+Alt+Z)');
      t.zoomBtn.setAttribute('aria-label', z ? 'Back to the grid' : 'Zoom this tile');
      if (t.zoomed !== z) { t.zoomed = z; t.zoomBtn.textContent = ''; t.zoomBtn.append(ic(z ? 'minimize' : 'maximize')); }
      t.zoomBtn.classList.toggle('hidden', I.n < 2);
      patchDock(t);
      if (I.zoom !== null && !z) t.node.setAttribute('inert', ''); else t.node.removeAttribute('inert');      // under the zoomed tile: no focus, no pointer
    }
    if (!I.active || !I.tiles.has(I.active)) { const first = tileAt(0) || I.tiles.values().next().value; setActive(first ? first.tmux : ''); }
    for (const t of I.tiles.values()) t.node.classList.toggle('active', t.tmux === I.active);
    patchHead();
    renderChips();
    patchKeys();
  }

  function changed() { sync(); persist(); }           // a tile whose size changed refits through its ResizeObserver: zoom, an overlay, changes none

  function setLayout(n) {
    if (!Quad.LAYOUTS.includes(n) || I.layout === n) return;
    I.layout = n;
    relayout(true);
    changed();
  }

  /* ----- the first state, the polls ----- */
  /* `carry` ({slots, zoom}) is a scope change made with the select: those slots (already only the new project's) stand in for the saved ones, so the tiles that stay stay. */
  function resolveInitial(carry) {
    const stored = Quad.load(I.project);
    const saved = carry ? { layout: I.layout, slots: carry.slots, modes: stored ? stored.modes : {}, zoom: carry.zoom } : stored;
    const parsed = Quad.parseQuery(I.query);
    I.layout = parsed.layout || (saved && saved.layout) || Quad.DEFAULT_LAYOUT;
    relayout(true);
    const r = Quad.slotsState({ query: I.query, saved, state: I.st, project: I.project, n: I.n });
    I.layout = r.layout; I.slots = r.slots; I.modes = r.modes; I.composers = r.composers; I.zoom = r.zoom;
    if (I.zoom !== null && I.zoom >= I.n) I.zoom = null;
    I.resolved = true;
  }

  function update(st) {
    if (I.disposed) return;
    I.st = st || null;
    I.roster = Quad.roster(st);
    I.index = new Map(I.roster.map((s) => [Ref.key(s), s]));
    const first = !I.resolved && !!st;
    if (first) resolveInitial();
    if (!I.resolved) return;
    for (const t of I.tiles.values()) patchGone(t);
    let started = false;
    if (I.noScope && known() && Quad.candidates(I.st, I.project).length) {                // the project had nothing running and now does (the New session button): its tiles fill at once
      I.manualEmpty.clear();
      I.slots = Quad.fillSlots(I.slots, I.st, I.n, { project: I.project });
      started = true;
    }
    sync();
    for (const t of I.tiles.values()) { const s = sessionOf(t.tmux); if (s) { patchTile(t, s); autoFit(t); } }
    for (const e of I.empties.values()) patchEmpty(e);
    renderChips();
    if (first || started) persist();
  }

  /* The address changed under the page: a pasted link, the sidebar's Quad link, our own write-back (ignored). */
  function onRoute(r) {
    if (I.disposed || I.writing) return;
    I.query = r && r.query ? r.query : {};
    const parsed = Quad.parseQuery(I.query);
    const project = parsed.project || '';
    if (project !== I.project) { switchScope(project); return; }
    if (!parsed.any) { if (I.resolved && I.writeTimer === null) I.writeTimer = setTimeout(flush, 0); return; }
    if (!I.resolved) return;
    let diff = false;
    if (parsed.layout && parsed.layout !== I.layout) { I.layout = parsed.layout; relayout(true); diff = true; }
    if (parsed.slots && parsed.slots.join(',') !== I.slots.join(',')) { I.slots = parsed.slots.slice(); I.manualEmpty.clear(); diff = true; }
    if (diff) changed();
  }

  /* The scope changed under the page (the select, a pasted link, Back): the tiles whose session is still wanted are kept as they are (sync() only removes what is not
     wanted and only moves data-slot, so not one iframe reloads); the others go, and the gaps are filled by auto-fill. The slots come from the URL's ?s=, else from the select's
     carry, else from what the new scope saved. */
  function switchScope(project) {
    const carry = I.carry && I.carry.project === project ? I.carry : null;
    I.carry = null;
    const before = I.modes;
    const beforeComposers = I.composers;
    if (I.resolved) Quad.save(I.project, snapshot());
    I.project = project;
    I.resolved = false;
    I.manualEmpty.clear();
    Quad.rememberScope(project);
    if (I.st) {
      resolveInitial(carry);
      const here = new Set(I.slots.filter(Boolean));
      for (const [tmux, mode] of Object.entries(before)) if (here.has(tmux) && !I.modes[tmux]) I.modes[tmux] = mode;         // a mode that was chosen on a tile that stays stays
      for (const [tmux, on] of Object.entries(beforeComposers)) if (here.has(tmux) && !I.composers[tmux]) I.composers[tmux] = on;      // and so does its docked composer
      sync();
      for (const t of I.tiles.values()) { const s = sessionOf(t.tmux); if (s) patchTile(t, s); }
      persist();
    } else patchHead();
    if (typeof document !== 'undefined' && typeof refreshTitle === 'function') refreshTitle();
  }

  /* The select: changing it pushes the new address (Back returns to the scope before). The slots that belong to the chosen project ride in it; sessions of other projects are
     left out, and switchScope fills what is empty. */
  function pickScope(value) {
    const project = value && Quad.WORD_RE.test(value) ? value : '';
    if (project === I.project) return;
    const kept = I.slots.map((t) => (t && Quad.inProject(I.st, t, project) ? t : ''));
    I.carry = { project, slots: kept, zoom: I.zoom !== null && kept[I.zoom] ? I.zoom : null };
    const h = Quad.hashFor({ layout: I.layout, slots: kept, project });
    if (typeof navigate === 'function') navigate(h); else if (typeof location !== 'undefined') location.hash = h;
  }

  /* The select's options follow the sessions (counts in place; a new set of projects is built when the picker is not in use) and its value is the scope. */
  function patchScope() {
    const sel = I.scopeSel;
    if (!sel) return;
    const opts = Quad.scopeOptions(I.st, I.project);
    const sig = opts.map((o) => o.project).join('|');
    let focused = false;
    try { focused = typeof document !== 'undefined' && document.activeElement === sel; } catch (_) { focused = false; }
    const busy = focused && I.scopeSig !== null;                      // the picker may be open: a different set of projects waits for blur
    if (sig !== I.scopeSig && !busy) {
      I.scopeSig = sig;
      sel.textContent = '';
      for (const o of opts) sel.append(el('option', { value: o.project, text: o.label }));
    }
    if (sig === I.scopeSig) opts.forEach((o, i) => { if (sel.children[i]) setTextIfChanged(sel.children[i], o.label); });      // the counts follow the poll in place
    if (!busy) sel.value = I.project;
    I.scopeAll.classList.toggle('hidden', !I.project || I.noScope);                 // the empty-project block has its own All projects link: not two of them on one screen
  }

  /* One plain line for a project with nothing running, and what to do next. */
  function patchNone() {
    const on = I.noScope;
    I.none.classList.toggle('hidden', !on);
    if (!on) return;
    setTextIfChanged(I.noneLine, `no live session in ${I.project}`);
    const p = I.st && (I.st.projects || []).some((x) => x.name === I.project);
    I.noneNew.classList.toggle('hidden', !p || typeof Shell === 'undefined' || !Shell || typeof Shell.openCreate !== 'function');
  }

  function newSession() {
    if (typeof Shell !== 'undefined' && Shell && typeof Shell.openCreate === 'function' && I.project) Shell.openCreate('session', { project: I.project });
  }

  /* ----- keys and the window ----- */
  function onKey(e) {
    const a = Quad.shortcutOf(e);
    if (!a || I.disposed) return;
    if (typeof e.preventDefault === 'function') e.preventDefault();
    if (typeof e.stopPropagation === 'function') e.stopPropagation();
    if (a.act === 'focus') focusSlot(a.n - 1);
    else if (a.act === 'zoom') zoomActive();
    else if (a.act === 'fullscreen') toggleFullscreen();
    else if (a.act === 'attention') goAttention();
    else if (a.act === 'reload') reloadAll();
  }

  function onVisibility() {
    if (typeof document === 'undefined') return;
    if (document.hidden) { I.hiddenAt = Date.now(); return; }
    const was = I.hiddenAt;
    I.hiddenAt = 0;
    if (was && Date.now() - was > Quad.HIDDEN_MS) reloadAll();
    else fitAll();
  }

  function onWindowSize() {
    if (relayout(false)) { sync(); persist(); } else patchHead();
    fitAll();
  }

  /* ----- build the page ----- */
  I.layoutBtns = Quad.LAYOUTS.map((n) => el('button', { class: 'seg-btn', type: 'button', 'data-layout': String(n), 'aria-pressed': 'false',
    title: n === 1 ? 'One terminal' : (n === 2 ? 'Two side by side' : `${n} terminals, ${Quad.GRID[n][0]} by ${Quad.GRID[n][1]}`), text: String(n), onclick: () => setLayout(n) }));
  I.layoutCtl = el('div', { class: 'seg-ctl q-layout', role: 'group', 'aria-label': 'Tiles on screen' }, el('span', { class: 'seg-k dim', text: 'tiles' }), ...I.layoutBtns);
  I.capNote = el('p', { class: 'q-cap dim hidden', role: 'status' });                    // why the layout that shows is not the one asked for, in plain words
  I.fillBtn = el('button', { class: 'small q-fill', type: 'button', title: 'Fill the empty tiles with the sessions that need you first', text: 'Auto-fill', onclick: () => { if (I.st) autoFillAll(); } });
  I.reloadBtn = el('button', { class: 'small q-reload', type: 'button', title: 'Reload every terminal (Ctrl+Alt+R)', onclick: () => reloadAll() }, ic('refresh'), 'Reload all');
  I.keysBtn = el('button', { class: 'small q-keys-toggle', type: 'button', 'aria-pressed': 'false', title: 'Show or hide the key bar',
    onclick: () => { try { localStorage.setItem(Quad.KEYS_PREF, keysWanted() ? '0' : '1'); } catch (_) { /* storage may be unavailable */ } patchKeys(); fitAll(); }, text: 'Keys' });
  // the scope: a native select on every width (more than four options), and a quiet way back to all projects while one is chosen
  I.scopeSel = el('select', { class: 'q-scope-sel', 'aria-label': 'Project', title: 'Which project the tiles come from' });
  I.scopeSel.addEventListener('change', () => pickScope(I.scopeSel.value));
  I.scopeSel.addEventListener('blur', () => patchScope());                   // an option list that grew while the picker was open is built now
  I.scopeAll = el('a', { class: 'q-all hidden', href: '#/quad', text: 'all projects' });
  I.scope = el('span', { class: 'q-scope' }, I.scopeSel, I.scopeAll);
  I.fsIcon = el('span', { class: 'q-fs-ic' });
  I.fsLabel = el('span', { class: 'q-fs-label', text: 'Fullscreen' });
  I.fsBtn = el('button', { class: 'small q-fs', type: 'button', title: 'Fill the screen with the tiles (F)', onclick: () => toggleFullscreen() }, I.fsIcon, I.fsLabel);
  I.actions = el('div', { class: 'actions q-actions' }, I.layoutCtl, I.fillBtn, I.reloadBtn, I.keysBtn, I.fsBtn);
  const head = el('div', { class: 'page-head q-head' }, el('h1', { text: 'Quad' }), I.scope, I.actions);
  I.noneLine = el('p', { class: 'q-none-line' });
  I.noneNew = el('button', { class: 'small q-none-new hidden', type: 'button', text: 'New session', onclick: () => newSession() });
  I.none = el('div', { class: 'q-none hidden', role: 'status' }, I.noneLine, el('div', { class: 'q-none-actions' }, I.noneNew, el('a', { class: 'btn minimal small q-none-all', href: '#/quad', text: 'All projects' })));
  I.chipsHost = el('div', { class: 'q-chips hidden', role: 'tablist', 'aria-label': 'Sessions' });
  I.chipList = makeKeyedList(I.chipsHost, { key: (s) => Ref.key(s), create: chipNode, patch: patchChip });
  I.grid = el('div', { class: 'qgrid', 'data-layout': '1' });
  I.keysTo = el('span', { class: 'q-keys-to dim' });
  I.keysHost = el('div', { class: 'q-keys hidden' }, I.keysTo);
  if (typeof TermKit !== 'undefined') {
    const kb = TermKit.keyBar(null, { send: sendKeys, sendText, compact: true });
    const rail = el('div', { class: 'q-scroll', role: 'group', 'aria-label': 'Scroll' });
    for (const [icon, label, dir, repeat] of [['chevron-up', 'Page up (scroll back)', 'up', true], ['chevron-down', 'Page down (scroll forward)', 'down', true],
      ['double-chevron-up', 'Scroll to the top', 'top', false], ['double-chevron-down', 'Scroll to the bottom', 'bottom', false]]) {
      const b = el('button', { type: 'button', class: 'icon small q-scroll-btn', 'aria-label': label, title: label }, ic(icon));
      TermKit.pressable(b, () => scrollActive(dir), repeat ? { repeat: true, every: 150, backlog: false } : {});
      rail.append(b);
    }
    I.keysHost.append(el('div', { class: 'q-keys-row' }, kb.root, rail));
  }
  I.host = el('div', { class: 'quad', 'data-layout': '1' }, head, I.capNote, I.chipsHost, I.none, I.grid, I.keysHost);
  root.append(I.host);
  Quad.rememberScope(I.project);

  if (typeof document !== 'undefined') listen(document, 'keydown', onKey, true);
  if (typeof document !== 'undefined') listen(document, 'keydown', onEscape);                  // bubble phase: a menu or a field that took the Esc has already stopped it
  if (typeof document !== 'undefined') listen(document, 'fullscreenchange', onFullscreenChange);
  if (typeof document !== 'undefined') listen(document, 'webkitfullscreenchange', onFullscreenChange);
  if (typeof document !== 'undefined') listen(document, 'visibilitychange', onVisibility);
  listen(window, 'resize', onWindowSize);
  listen(window, 'orientationchange', onWindowSize);
  try {                                                                                    // a pointer flip (a mouse on an iPad) and the QA switch html.force-coarse both repaint the touch tiers at once
    const mq = typeof window.matchMedia === 'function' ? window.matchMedia('(pointer: coarse)') : null;
    if (mq && typeof mq.addEventListener === 'function') { mq.addEventListener('change', applyTouch); I.pointerOff.push(() => mq.removeEventListener('change', applyTouch)); }
    else if (mq && typeof mq.addListener === 'function') { mq.addListener(applyTouch); I.pointerOff.push(() => mq.removeListener(applyTouch)); }
  } catch (_) { /* no matchMedia: the sync pass still follows the pointer */ }
  try {
    if (typeof MutationObserver === 'function' && document.documentElement) {
      const mo = new MutationObserver(() => applyTouch());
      mo.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
      I.pointerOff.push(() => mo.disconnect());
    }
  } catch (_) { /* no MutationObserver */ }
  if (typeof ResizeObserver === 'function') {
    I.rootRO = new ResizeObserver(() => onWindowSize());
    I.rootRO.observe(I.host);
  }
  if (typeof Keymap !== 'undefined' && typeof Keymap.bindKey === 'function') {
    // help-only entries: the page's own capture listener above runs first and handles these keys (also inside the terminals, where Keymap is inert)
    for (const [spec, help, label] of [['mod+alt+1', 'Quad: focus tile 1 to 10 (0 is the tenth)', 'Ctrl+Alt+1…0'], ['mod+alt+z', 'Quad: zoom the active tile', 'Ctrl+Alt+Z'],
      ['mod+alt+k', 'Quad: the next session that needs you', 'Ctrl+Alt+K'], ['mod+alt+r', 'Quad: reload every tile', 'Ctrl+Alt+R'], ['mod+alt+f', 'Quad: full screen on or off, also inside a terminal', 'Ctrl+Alt+F']]) {
      try { I.offs.push(Keymap.bindKey(spec, () => false, { help, label, group: 'Quad' })); } catch (_) { /* a spec Keymap does not take */ }
    }
  }
  relayout(true);
  patchHead();
  renderChips();
  patchKeys();

  /* ----- the handle ----- */
  I.update = update;
  I.onRoute = onRoute;
  I.sync = sync;
  I.flush = flush;
  I.assign = assign;
  I.pick = pick;
  I.setLayout = setLayout;
  I.setMode = (tmux, mode) => { const t = I.tiles.get(tmux); if (t) setMode(t, mode); };
  I.focusSlot = focusSlot;
  I.zoomSlot = zoomSlot;
  I.zoomActive = zoomActive;
  I.toggleFullscreen = toggleFullscreen;
  I.enterFullscreen = enterFullscreen;
  I.leaveFullscreen = leaveFullscreen;
  I.menuCtx = (tmux) => { const t = I.tiles.get(tmux); return t ? menuCtx(t) : null; };      // what TermKit.tileMenu gets for a tile
  I.openMenu = (tmux, byKeyboard) => { const t = I.tiles.get(tmux); return t ? toggleTileMenu(t, !!byKeyboard) : false; };
  I.openComposer = (tmux) => { const t = I.tiles.get(tmux); return t ? openComposer(t) : false; };
  I.openTune = (tmux) => { const t = I.tiles.get(tmux); return t ? openTune(t) : false; };
  I.toggleDocked = (tmux) => { const t = I.tiles.get(tmux); return t ? toggleDocked(t) : false; };
  I.closePop = closeAll;
  I.goAttention = goAttention;
  I.reloadAll = reloadAll;
  I.autoFill = autoFillAll;
  I.setActive = setActive;
  I.onKey = onKey;
  I.relayout = onWindowSize;
  I.measure = (tmux) => { const t = I.tiles.get(tmux); if (t) measure(t); };
  I.destroy = function () {
    if (I.disposed) return;
    I.disposed = true;
    if (I.writeTimer !== null) { clearTimeout(I.writeTimer); I.writeTimer = null; }
    for (const off of I.pointerOff) { try { off(); } catch (_) { /* gone */ } }      // unsubscribe before the nodes go
    I.pointerOff.length = 0;
    if (I.fsZoom) { I.fsZoom = false; I.zoom = null; }               // the zoom that Fullscreen this tile made is not kept
    if (I.resolved) Quad.save(I.project, snapshot());
    closeAll();
    leaveFullscreen();                                                // the page owned the full screen: it goes back to the browser with the quad
    if (I.rootRO) { try { I.rootRO.disconnect(); } catch (_) { /* gone */ } I.rootRO = null; }
    for (const t of Array.from(I.tiles.values())) teardownTile(t);
    for (const e of I.empties.values()) e.node.remove();
    I.empties.clear();
    chipTailsOff();
    if (I.chipList) I.chipList.clear();
    for (const off of I.listeners) { try { off(); } catch (_) { /* gone */ } }
    I.listeners.length = 0;
    for (const off of I.offs) { try { off(); } catch (_) { /* gone */ } }
    I.offs.length = 0;
    try { I.host.remove(); } catch (_) { /* detached */ }
    try { if (typeof Shell !== 'undefined' && Shell && typeof Shell.dockResume === 'function') Shell.dockResume(); } catch (e) { console.error('ccboard quad dock', e); }
  };
  return I;
};

registerPage('quad', {
  title: (route) => {
    const p = Quad.parseQuery(route && route.query).project;
    return 'Quad' + (p ? ' · ' + p : '');
  },
  mount(root, route) {
    if (Quad.current) { Quad.current.destroy(); Quad.current = null; }
    Quad.current = Quad.mount(root, route);
  },
  update(st) { if (Quad.current) Quad.current.update(st); },
  onRoute(route) { if (Quad.current) Quad.current.onRoute(route); },
  unmount() {
    if (Quad.current) { Quad.current.destroy(); Quad.current = null; }
  },
});
