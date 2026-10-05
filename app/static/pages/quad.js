/* ccboard quad page (v0.5.9, route #/quad[?s=a,b,c,d&l=1|2|4&p=<project>]): up to four live terminals side by side, each labelled with its task.
   Classic script, one namespace (Quad). Definition only at load: nothing here touches the DOM, storage, the network or a timer until the page is
   mounted, and TermKit / Live / Keymap / Dnd are looked up when they are used (this file may load before or without them).

   Layouts 1, 2 (side by side) and 4 (2 x 2); the default is 2. A window under 840 px (the phone) or a quad column under 600 px (the dock open at
   1024) is the one-up chip switcher instead: one scroll-snap strip of 44 px chips above ONE mounted tile in full mode, so only one websocket is live.
   A tile is article.qtile[data-tmux][data-slot][data-mode=grid|full|ro|tail][data-drop=session] built ONCE per tmux name: its header (state and agent
   glyph, project/repo · session as a menu, the mode as a 4-way segmented control (a menu button under 366 px), ctx %, size chip, zoom, open, reconnect,
   close; pages.css sheds them as the tile narrows), the task line (task title, else the last prompt; the last message while it waits), the
   pending-permission line (Allow / Deny / In terminal) and the body (a ttyd iframe, or pre.tail fed by Live). The grid places a tile by its data-slot in
   CSS and nobody ever reorders the DOM: moving an iframe reloads it.
   Zoom is an overlay over an unchanged grid, so the other tiles keep their pixel size (no refit, no /resize POST). Tiles beyond the layout are torn
   down: iframe.src = 'about:blank', then remove(), observers off, Live.unsubscribe.

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
  LAYOUTS: [1, 2, 4],
  DEFAULT_LAYOUT: 2,
  SLOTS: 4,
  MODES: ['grid', 'full', 'ro', 'tail'],
  MODE_TITLE: { grid: 'Grid: a small tile that does not size the session', full: 'Full: a writable terminal that sizes the session', ro: 'Read only: watch it, nothing you type reaches the session', tail: 'Tail: the last lines of the pane, no terminal' },
  WIDE_MIN: 840,                   // under this window width the quad is the one-up chip switcher
  COLS_MIN: 600,                   // two columns need 300 px each: a quad column narrower than this is one-up as well (the dock open at 1024)
  STORE: 'ccboard:quad:',
  SCOPE_KEY: 'ccboard:quad:scope', // the scope last used: 'all' or a project name (a raw string, read by shell.js for the nav link)
  RESERVED: ['all', 'scope', 'keys'], // project names that would share a storage key with the all-projects scope, the scope pref and the key-bar pref: they save under ccboard:quad:p:<name>
  KEYS_PREF: 'ccboard:quad:keys',  // '1' shows the host key bar, '0' hides it; unset: shown on touch and in one-up
  MODES_MAX: 24,                   // remembered modes per scope
  HIDDEN_MS: 60000,                // a tab hidden longer than this reloads its live tiles when it comes back
  MEASURE_MS: 320,                 // the size chip and the auto-fit look after TermKit.fitSoon's 250 ms debounce
  FIT_REPEAT_MS: 60000,            // the same auto-fit target is not asked for again inside this window
  FIT_TRIES: 3,
  COLS: [40, 400],                 // POST /resize bounds (tmux.RESIZE_COLS / RESIZE_ROWS)
  ROWS: [10, 200],
  CHIPS_MAX: 20,                   // Live's cap on ?names=
  NAME_RE: /^[A-Za-z0-9_-]+--[A-Za-z0-9_-]+--[A-Za-z0-9_-]+$/,
  WORD_RE: /^[A-Za-z0-9_-]+$/,
  current: null,                   // the mounted page (see Quad.mount), for the tests and Quad.addToQuad
};

/* ---------- layout ---------- */

/* {n, cols, rows, oneUp, forced, capped} for a window `width`, the wanted layout (1 | 2 | 4) and the quad's own column width `avail` (optional).
   Under 840 px (the phone, and 600-839: one-up, not 'max 2') or with a column under 600 px the layout is one tile with the chip switcher (forced). */
Quad.layoutFor = function (width, wanted, avail) {
  const w = Number(width);
  const want = Quad.LAYOUTS.includes(Number(wanted)) ? Number(wanted) : Quad.DEFAULT_LAYOUT;
  const one = () => ({ n: 1, cols: 1, rows: 1, oneUp: true, forced: true, capped: want !== 1 });
  if (!(w >= Quad.WIDE_MIN)) return one();
  const a = Number(avail);
  if (want > 1 && a > 0 && a < Quad.COLS_MIN) return one();
  return { n: want, cols: want === 1 ? 1 : 2, rows: want === 4 ? 2 : 1, oneUp: want === 1, forced: false, capped: false };
};

/* ---------- names, the URL and the saved state ---------- */

Quad.parts = function (tmux) {
  const p = String(tmux || '').split('--');
  return p.length === 3 ? p : [String(tmux || ''), '', ''];
};

/* 'phasezero/website · s1'; the project folder is just the project ('phasezero · s1'). */
Quad.label = function (tmux) {
  const [p, r, n] = Quad.parts(tmux);
  if (!r) return String(tmux || '');
  return `${p}${r === 'root' ? '' : '/' + r} · ${n}`;
};

/* Four strings: a valid, not repeated tmux name or ''. */
Quad.cleanSlots = function (list) {
  const out = [];
  const seen = new Set();
  for (let i = 0; i < Quad.SLOTS; i++) {
    const t = Array.isArray(list) && typeof list[i] === 'string' ? list[i] : '';
    if (t && Quad.NAME_RE.test(t) && !seen.has(t)) { seen.add(t); out.push(t); } else out.push('');
  }
  return out;
};

/* The route query {s, l, p} as {slots: string[4] | null, layout: 1|2|4 | null, project: string | null, any}: whatever is not valid is left out. */
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

/* The saved {layout, slots, modes, zoom} of a scope, or null when nothing (usable) was saved. */
Quad.load = function (project) {
  let raw = null;
  try { raw = JSON.parse(localStorage.getItem(Quad.storageKey(project)) || 'null'); } catch (_) { raw = null; }
  if (!raw || typeof raw !== 'object') return null;
  const modes = {};
  if (raw.modes && typeof raw.modes === 'object') for (const [k, v] of Object.entries(raw.modes)) if (Quad.NAME_RE.test(k) && Quad.MODES.includes(v)) modes[k] = v;
  const zoom = Number.isInteger(raw.zoom) && raw.zoom >= 0 && raw.zoom < Quad.SLOTS ? raw.zoom : null;
  return { layout: Quad.LAYOUTS.includes(raw.layout) ? raw.layout : Quad.DEFAULT_LAYOUT, slots: Quad.cleanSlots(raw.slots), modes, zoom };
};

Quad.save = function (project, st) {
  const modes = {};
  const keys = Object.keys(st.modes || {});
  for (const k of keys.slice(Math.max(0, keys.length - Quad.MODES_MAX))) modes[k] = st.modes[k];
  const zoom = Number.isInteger(st.zoom) && st.zoom >= 0 && st.zoom < Quad.SLOTS ? st.zoom : null;
  const value = { layout: Quad.LAYOUTS.includes(Number(st.layout)) ? Number(st.layout) : Quad.DEFAULT_LAYOUT, slots: Quad.cleanSlots(st.slots), modes, zoom };
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
    try { hit = { rank: new Map(Inbox.items(st).map((it, i) => [it.tmux, i])) }; } catch (_) { hit = null; }       // an inbox that throws leaves the quad on its own rule
    Quad.inboxCache.set(st, hit);
  }
  return hit;
};

/* The board's 'needs you' (the inbox list), else: a pending permission, a session that waits, or one whose last turn wants an acknowledgement. */
Quad.needsYou = function (s, st) {
  if (!s) return false;
  const ib = Quad.inboxFor(st);
  if (ib) return ib.rank.has(s.tmux);
  return !!(Quad.perm(st, s.tmux) || s.state === 'waiting' || s.needs_attention === true);
};

/* 0 needs you (with the inbox: its order; without: 0 a permission, 1 waiting, 2 needs an ack), 3 working, 4 the rest, 5 a plain shell. */
Quad.group = function (s, st) {
  const ib = Quad.inboxFor(st);
  if (ib) {
    if (ib.rank.has(s.tmux)) return 0;
  } else {
    if (Quad.perm(st, s.tmux)) return 0;
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
  if (ib) return ib.rank.get(a.tmux) - ib.rank.get(b.tmux);
  const d = ga <= 2 ? Quad.activity(a) - Quad.activity(b) : Quad.activity(b) - Quad.activity(a);
  return d || String(a.tmux).localeCompare(String(b.tmux));
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
  for (const s of Quad.roster(st)) if (s.tmux === tmux) return s.project === project;
  return Quad.parts(tmux)[0] === project;
};

/* The tmux names that auto-fill picks for `n` tiles: opts {project, exclude: names already placed}. */
Quad.autoFill = function (st, n, opts) {
  const o = opts || {};
  const skip = new Set(o.exclude || []);
  return Quad.candidates(st, o.project).map((s) => s.tmux).filter((t) => !skip.has(t)).slice(0, Math.max(0, n));
};

/* `slots` with the empty ones among the indexes `only` (default: the first n) filled by auto-fill; the rest is untouched. */
Quad.fillSlots = function (slots, st, n, opts) {
  const o = opts || {};
  const out = Quad.cleanSlots(slots);
  const idx = (o.only || Array.from({ length: Math.min(n, Quad.SLOTS) }, (_, i) => i)).filter((i) => i >= 0 && i < Quad.SLOTS && !out[i]);
  const picks = Quad.autoFill(st, idx.length, { project: o.project, exclude: out.filter(Boolean) });
  idx.forEach((i, k) => { if (picks[k]) out[i] = picks[k]; });
  return out;
};

/* What the page starts from, URL over saved over auto-fill. o = {query (the route query), saved (Quad.load), state (the /api/state payload, null before
   the first poll), project, n (visible slots; default the layout)} -> {layout, slots, modes, zoom, fromUrl}. A saved slot whose session is gone is
   dropped; a URL slot is kept as it is (the tile then says the session is not running); every empty visible slot is then filled by auto-fill (an empty
   tile is something the person does inside one visit, with Close: a reload starts from what needs you again). */
Quad.slotsState = function (o) {
  const parsed = Quad.parseQuery(o.query);
  const saved = o.saved || null;
  const layout = parsed.layout || (saved && saved.layout) || Quad.DEFAULT_LAYOUT;
  const n = Math.min(Quad.SLOTS, Math.max(1, o.n || layout));
  const st = o.state || null;
  let slots = parsed.slots ? parsed.slots.slice() : (saved ? saved.slots.slice() : ['', '', '', '']);
  if (o.project) slots = slots.map((t) => (t && !Quad.inProject(st, t, o.project) ? '' : t));         // a project's quad shows that project's sessions only
  const modes = saved ? { ...saved.modes } : {};
  let zoom = saved && saved.zoom !== null && saved.zoom < n ? saved.zoom : null;
  if (st) {
    const live = new Set(Quad.roster(st).map((s) => s.tmux));
    if (!parsed.slots) slots = slots.map((t) => (t && !live.has(t) ? '' : t));
    slots = Quad.fillSlots(slots, st, n, { project: o.project || '' });
  }
  if (zoom !== null && !slots[zoom]) zoom = null;
  return { layout, slots, modes, zoom, fromUrl: { slots: !!parsed.slots, layout: !!parsed.layout } };
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

/* Ctrl+Alt+1..4 focus a tile, Z zooms the active one, K goes to the next session that needs you, R reloads every tile. e.code first: Option+digit types
   another character on a Mac; AltGr (Ctrl+Alt on Windows, it types characters on some layouts) is not a shortcut. */
Quad.shortcutOf = function (e) {
  if (!e || !e.ctrlKey || !e.altKey || e.metaKey || e.shiftKey || e.isComposing) return null;
  try { if (typeof e.getModifierState === 'function' && e.getModifierState('AltGraph')) return null; } catch (_) { /* no modifier state */ }      // AltGr is Ctrl+Alt on Windows: it types ~ { # on some layouts
  const code = typeof e.code === 'string' ? e.code : '';
  const key = typeof e.key === 'string' ? e.key.toLowerCase() : '';
  const m = /^(?:Digit|Numpad)([1-4])$/.exec(code) || (/^[1-4]$/.test(key) ? [key, key] : null);
  if (m) return { act: 'focus', n: Number(m[1]) };
  if (code === 'KeyZ' || (!code && key === 'z')) return { act: 'zoom' };
  if (code === 'KeyK' || (!code && key === 'k')) return { act: 'attention' };
  if (code === 'KeyR' || (!code && key === 'r')) return { act: 'reload' };
  return null;
};

/* The next session after `current` that needs you (permission first, then the longest waiting), or '' when nothing does. */
Quad.attentionNext = function (st, project, current) {
  const list = Quad.candidates(st, project).filter((s) => Quad.needsYou(s, st)).map((s) => s.tmux);
  if (!list.length) return '';
  return list[(list.indexOf(current) + 1) % list.length];
};

/* Put a session into the saved slots of a scope (the first free visible slot, else the last slot) and go to #/quad there: the dock's 'add to quad'. */
Quad.addToQuad = function (tmux, project) {
  if (typeof tmux !== 'string' || !Quad.NAME_RE.test(tmux)) return false;
  const scope = project && Quad.WORD_RE.test(project) ? project : '';
  const cur = Quad.current;
  if (cur && cur.project === scope) {
    cur.pick(tmux);
    return true;
  }
  const saved = Quad.load(scope) || { layout: Quad.DEFAULT_LAYOUT, slots: ['', '', '', ''], modes: {}, zoom: null };
  const slots = saved.slots.slice();
  let layout = saved.layout;
  if (!slots.includes(tmux)) {
    let free = slots.findIndex((t, i) => !t && i < layout);
    while (free < 0 && layout < 4) { layout = layout === 1 ? 2 : 4; free = slots.findIndex((t, i) => !t && i < layout); }     // full: the layout grows rather than evict a tile
    slots[free >= 0 ? free : layout - 1] = tmux;
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
    layout: parsed0.layout || Quad.DEFAULT_LAYOUT, slots: ['', '', '', ''], modes: {}, zoom: null,
    n: 1, oneUp: true, forced: true, reMode: false, focused: false, touch: false, resolved: false, st: null, roster: [], index: new Map(),
    active: '', tiles: new Map(), empties: new Map(), manualEmpty: new Set(),
    chipTails: new Map(), chipLines: new Map(), chipList: null,
    hiddenAt: 0, writeTimer: null, writing: false, disposed: false, listeners: [], offs: [], chipOrder: [], chipSel: '',
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
  function snapshot() { return { layout: I.layout, slots: I.slots, modes: I.modes, zoom: I.zoom, project: I.project }; }

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
    if (!(slot >= 0 && slot < Quad.SLOTS) || (tmux && !Quad.NAME_RE.test(tmux))) return false;
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
    if (!Quad.NAME_RE.test(tmux)) return false;
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
      dims: null, session: null, perm: null, permSig: '', decided: null, sig: '', missing: 0, gone: false, zoomed: false, fit: { key: '', at: 0, tries: 0, blocked: '' }, modeBtns: [] };
    const label = Quad.label(tmux);
    tile.glyphs = el('span', { class: 'qt-glyphs' });
    const [proj, repo, sname] = Quad.parts(tmux);
    // two spans so a narrow header drops the project/repo first and keeps the session name: 'phasezero/website · ' shrinks, 't-checkout-redesign' stays
    tile.where = el('span', { class: 'qt-where', text: repo ? `${proj}${repo === 'root' ? '' : '/' + repo} · ` : '' });
    tile.name = el('span', { class: 'qt-name' }, tile.where, el('span', { class: 'qt-sess', text: repo ? sname : tmux }));
    tile.title = el('button', { class: 'minimal small qt-title', type: 'button', title: `${tmux}: swap, reconnect, close`, 'aria-label': `${label}: tile menu` },
      tile.name, el('span', { class: 'qt-caret', 'aria-hidden': 'true', text: '▾' }));
    if (typeof menu === 'function') menu(tile.title, () => titleItems(tile));
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
    const open = el('a', { class: 'btn icon minimal small qt-open', href: `/term/${enc(tmux)}`, target: '_blank', rel: 'noopener', 'data-dock': 'skip', 'aria-label': 'Open the terminal page', title: 'Open the terminal page' }, ic('share'));
    const again = el('button', { class: 'icon minimal small qt-reconnect', type: 'button', 'aria-label': 'Reconnect', title: 'Reconnect this tile', onclick: () => reconnect(tile) }, ic('refresh'));
    const shut = el('button', { class: 'icon minimal small qt-close', type: 'button', 'aria-label': 'Close this tile', title: 'Close this tile', onclick: () => close(tile) }, ic('cross'));
    const head = el('header', { class: 'qt-head' }, tile.glyphs, tile.title, tile.modes, tile.modeMenu, tile.ctx, tile.size, tile.zoomBtn, open, again, shut);
    tile.task = el('div', { class: 'qt-task hidden' });
    tile.permText = el('span', { class: 'qt-perm-text' });
    tile.permBtns = el('div', { class: 'qt-perm-btns' });
    tile.permNode = el('div', { class: 'qt-perm hidden' }, tile.permText, tile.permBtns);
    tile.body = el('div', { class: 'qt-body' });
    tile.node = el('article', { class: 'qtile', 'data-tmux': tmux, 'data-slot': '-1', 'data-mode': '', 'data-drop': 'session', tabindex: '-1', 'aria-label': label }, head, tile.task, tile.permNode, tile.body);
    tile.node.addEventListener('pointerdown', () => { touched(); setActive(tmux); }, true);
    tile.node.addEventListener('focusin', () => setActive(tmux));                // ttyd focuses its own terminal on load: that is no use of the tile
    if (typeof Dnd !== 'undefined' && typeof Dnd.bind === 'function') Dnd.bind(tile.node, { drop: 'session', tmux });      // a backlog card dropped on the tile is handed to the session
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
    if (tile.ro) { try { tile.ro.disconnect(); } catch (_) { /* gone */ } tile.ro = null; }
    dropTail(tile);
    dropFrame(tile);
    try { tile.node.remove(); } catch (_) { /* detached */ }
    I.tiles.delete(tile.tmux);
    if (I.active === tile.tmux) { I.active = ''; patchKeys(); }
  }

  function titleItems(tile) {
    // everything the header sheds as the tile narrows is here: the menu is the one place that always has it
    const items = [
      { label: 'Open terminal page', icon: 'share', onClick: () => { if (typeof openPage === 'function') openPage(`/term/${enc(tile.tmux)}`); } },
      { label: 'Reconnect', icon: 'refresh', onClick: () => reconnect(tile) },
    ];
    if (I.n >= 2) items.push({ label: tile.slot === I.zoom ? 'Back to the grid' : 'Zoom this tile', icon: tile.slot === I.zoom ? 'minimize' : 'maximize', onClick: () => zoomSlot(tile.slot) });
    items.push({ label: 'Close tile', icon: 'cross', onClick: () => close(tile) });
    const here = new Set(visibleSlots().filter(Boolean));
    for (const s of Quad.candidates(I.st, I.project).filter((x) => !here.has(x.tmux)).slice(0, 10)) {
      items.push({ label: `Swap in ${Quad.label(s.tmux)}`, onClick: () => assign(tile.slot, s.tmux) });
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
    tile.perm = pr || null;
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
    const line = Quad.taskLine(s);
    setTextIfChanged(tile.task, line.text);
    tile.task.classList.toggle('hidden', !line.text);
    tile.task.setAttribute('data-kind', line.kind);
    patchPerm(tile, Quad.perm(st, tile.tmux), s);
    patchSize(tile);
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

  function patchEmpty(e) {
    const free = Quad.candidates(I.st, I.project).filter((s) => !I.slots.includes(s.tmux)).slice(0, 6);
    const sig = `${I.st ? 1 : 0}|${free.map((s) => `${s.tmux}:${s.state}`).join(',')}`;
    if (e.sig === sig) return;
    e.sig = sig;
    e.node.textContent = '';
    e.node.append(el('div', { class: 'qe-title', text: !I.st ? 'Loading sessions…' : (free.length ? 'Pick a session for this tile' : (I.project ? `No other session in ${I.project}` : 'No other session is running')) }));
    if (!I.st) return;
    const list = el('div', { class: 'qe-list' });
    for (const s of free) {
      list.append(el('button', { class: 'small qe-pick', type: 'button', 'data-tmux': s.tmux, onclick: () => assign(e.slot, s.tmux) },
        stateGlyph(s.state), el('span', { class: 'qe-name', text: Quad.label(s.tmux) })));
    }
    e.node.append(list);
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
    const node = el('button', { class: 'q-chip', type: 'button', role: 'tab', 'data-tmux': s.tmux, 'aria-selected': 'false', title: s.tmux, onclick: () => assign(0, s.tmux) },
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
    setTextIfChanged(node.ccTail, I.chipLines.get(s.tmux) || '');
  }

  function renderChips() {
    if (!I.chipsHost) return;
    I.chipsHost.classList.toggle('hidden', !I.oneUp || I.noScope);
    if (!I.oneUp || !I.st || I.noScope) { chipTailsOff(); return; }
    const list = Quad.candidates(I.st, I.project);
    const cur = I.slots[0] ? sessionOf(I.slots[0]) : null;
    if (cur && !list.some((s) => s.tmux === cur.tmux)) list.unshift(cur);          // an ended session that is on screen stays a chip
    const shown = list.slice(0, Quad.CHIPS_MAX);
    const order = new Set(I.chipOrder);
    const present = new Set(shown.map((s) => s.tmux));
    I.chipOrder = I.chipOrder.filter((t) => present.has(t));
    for (const s of shown) if (!order.has(s.tmux)) I.chipOrder.push(s.tmux);        // a chip keeps its place; a new one joins at its rank
    const byTmux = new Map(shown.map((s) => [s.tmux, s]));
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
    if (!force && L.n === I.n && L.oneUp === I.oneUp && L.forced === I.forced) return false;
    const grew = L.n > I.n;
    if (L.forced !== I.forced) I.reMode = true;                        // the one thing that changes a tile's default mode: the window going to or from 'one tile because it is too narrow'
    I.n = L.n; I.oneUp = L.oneUp; I.forced = L.forced;
    if (I.zoom !== null && (I.zoom >= I.n || I.n < 2)) I.zoom = null;
    if (grew && I.resolved && I.st) {                                // new visible slots start filled, unless the person emptied them
      const only = [];
      for (let i = 0; i < I.n; i++) if (!I.slots[i] && !I.manualEmpty.has(i)) only.push(i);
      if (only.length) I.slots = Quad.fillSlots(I.slots, I.st, I.n, { project: I.project, only });
    }
    return true;
  }

  function patchHead() {
    const h = I.host;
    h.setAttribute('data-layout', String(I.n));
    if (I.oneUp) h.setAttribute('data-oneup', 'true'); else h.removeAttribute('data-oneup');
    if (I.forced) h.setAttribute('data-forced', 'true'); else h.removeAttribute('data-forced');
    I.grid.setAttribute('data-layout', String(I.n));
    const touch = coarse();
    if (touch) h.setAttribute('data-touch', 'true'); else h.removeAttribute('data-touch');         // pages.css: the touch tiers of the tile header (44 px controls need more room)
    if (touch !== I.touch) {                                                                       // the header's pieces changed size: no tile body resized, so look at the titles again
      I.touch = touch;
      if (typeof TermKit !== 'undefined' && TermKit && typeof TermKit.fitName === 'function') for (const t of I.tiles.values()) TermKit.fitName(t.where);
    }
    if (I.zoom !== null) I.grid.setAttribute('data-zoom', String(I.zoom)); else I.grid.removeAttribute('data-zoom');
    for (const b of I.layoutBtns) b.setAttribute('aria-pressed', Number(b.getAttribute('data-layout')) === I.layout ? 'true' : 'false');
    I.fillBtn.disabled = !I.st;
    I.grid.classList.toggle('hidden', I.noScope);
    I.actions.classList.toggle('hidden', I.noScope);
    patchScope();
    patchNone();
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
    I.layout = r.layout; I.slots = r.slots; I.modes = r.modes; I.zoom = r.zoom;
    if (I.zoom !== null && I.zoom >= I.n) I.zoom = null;
    I.resolved = true;
  }

  function update(st) {
    if (I.disposed) return;
    I.st = st || null;
    I.roster = Quad.roster(st);
    I.index = new Map(I.roster.map((s) => [s.tmux, s]));
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
    if (I.resolved) Quad.save(I.project, snapshot());
    I.project = project;
    I.resolved = false;
    I.manualEmpty.clear();
    Quad.rememberScope(project);
    if (I.st) {
      resolveInitial(carry);
      const here = new Set(I.slots.filter(Boolean));
      for (const [tmux, mode] of Object.entries(before)) if (here.has(tmux) && !I.modes[tmux]) I.modes[tmux] = mode;         // a mode that was chosen on a tile that stays stays
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
    else if (a.act === 'zoom') { const t = I.tiles.get(I.active); if (t) zoomSlot(t.slot); }
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
    if (relayout(false)) { sync(); persist(); }
    fitAll();
  }

  /* ----- build the page ----- */
  I.layoutBtns = Quad.LAYOUTS.map((n) => el('button', { class: 'seg-btn', type: 'button', 'data-layout': String(n), 'aria-pressed': 'false',
    title: n === 1 ? 'One terminal' : (n === 2 ? 'Two side by side' : 'Four, 2 by 2'), text: String(n), onclick: () => setLayout(n) }));
  I.layoutCtl = el('div', { class: 'seg-ctl q-layout', role: 'group', 'aria-label': 'Layout' }, el('span', { class: 'seg-k dim', text: 'tiles' }), ...I.layoutBtns);
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
  I.actions = el('div', { class: 'actions q-actions' }, I.layoutCtl, I.fillBtn, I.reloadBtn, I.keysBtn);
  const head = el('div', { class: 'page-head q-head' }, el('h1', { text: 'Quad' }), I.scope, I.actions);
  I.noneLine = el('p', { class: 'q-none-line' });
  I.noneNew = el('button', { class: 'small q-none-new hidden', type: 'button', text: 'New session', onclick: () => newSession() });
  I.none = el('div', { class: 'q-none hidden', role: 'status' }, I.noneLine, el('div', { class: 'q-none-actions' }, I.noneNew, el('a', { class: 'btn minimal small q-none-all', href: '#/quad', text: 'All projects' })));
  I.chipsHost = el('div', { class: 'q-chips hidden', role: 'tablist', 'aria-label': 'Sessions' });
  I.chipList = makeKeyedList(I.chipsHost, { key: (s) => s.tmux, create: chipNode, patch: patchChip });
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
  I.host = el('div', { class: 'quad', 'data-layout': '1' }, head, I.chipsHost, I.none, I.grid, I.keysHost);
  root.append(I.host);
  Quad.rememberScope(I.project);

  if (typeof document !== 'undefined') listen(document, 'keydown', onKey, true);
  if (typeof document !== 'undefined') listen(document, 'visibilitychange', onVisibility);
  listen(window, 'resize', onWindowSize);
  listen(window, 'orientationchange', onWindowSize);
  if (typeof ResizeObserver === 'function') {
    I.rootRO = new ResizeObserver(() => onWindowSize());
    I.rootRO.observe(I.host);
  }
  if (typeof Keymap !== 'undefined' && typeof Keymap.bindKey === 'function') {
    // help-only entries: the page's own capture listener above runs first and handles these keys (also inside the terminals, where Keymap is inert)
    for (const [spec, help, label] of [['mod+alt+1', 'Quad: focus tile 1 to 4', 'Ctrl+Alt+1…4'], ['mod+alt+z', 'Quad: zoom the active tile', 'Ctrl+Alt+Z'],
      ['mod+alt+k', 'Quad: the next session that needs you', 'Ctrl+Alt+K'], ['mod+alt+r', 'Quad: reload every tile', 'Ctrl+Alt+R']]) {
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
    if (I.resolved) Quad.save(I.project, snapshot());
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
