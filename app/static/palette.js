/* ccboard command palette and shortcut help (v0.5.3b): one modal <dialog id="helpdlg"> (class 'palette') that holds either the palette or the list of
   shortcuts. Definition only at load; keymap.js opens it (mod+k, ?), main.js opens it for a shared page or snippet. Classic script, one namespace (Palette).

   Palette.open({ mode?: 'default' | 'send', text? }) draws a search box and grouped results, filtered by fuzzy match as you type:
     Sessions   every live session in the Agents order, state glyph, project/repo; Enter opens its peek (mod+1..9 jump straight there)
     Routes     Home, Needs you, Agents, Tasks, Usage, Memory, Settings, Search (and "search transcripts for <what you typed>")
     Nudges     for the selected or peeked session: continue, merge, push, pr, add commit push, do it (typed into it with Enter)
     Controls   for the same session: /compact /context /cost /usage /status through POST /api/sessions/<name>/command (v0.5.7), confirmed by
                a toast; a read command (/usage ...) shows the captured screen in the sheet and presses Escape when it closes; a 409 toasts the
                reason; an older server without the endpoint (404) gets the text through /keys. Labelled from the registry that the terminal
                page cached (GET /api/agents, sessionStorage) when there is one. /clear, /effort and /model stay in the terminal's tuning strip.
     Backlog    Move: <card>, the keyboard route of the drag (the Move sheet, as the m key on a card opens it); three cards with nothing typed
     Skills     (Claude sessions only) the skills installed on the box from GET /api/skills, most used first; the top five, "All skills…" opens the rest;
                Enter puts `/<name> ` into the composer you came from or the peek's send box and never sends
     Modes      ultracode / plan, inserted into the peek's send box
     More       collapsed; the fun commands (/color /copy /rewind /radio /stickers /tui /passes) as plain inserts into that box
   mode 'send' is the share target: the box holds the shared text (editable) and the list is "send to session"; Enter sends it with Enter,
   Shift+Enter only types it. Closing the dialog in that mode strips the shared params from the address (Router.stripShare).
   Nodes      (issue #139, only with a node paired) Go to <node>, Open board on <node>; remote sessions join Sessions with their node chip, remote tasks show while searching; `@box fix`
                narrows Sessions and the tasks of other nodes to the node `box` (its handle or name, or a unique start of the handle; `@local` is this board) and filters by `fix`
   Palette.openHelp() draws Keymap.help() grouped. Everything here is built with el() and textContent. */
'use strict';

const Palette = {
  ui: null,                 // { dlg, view: 'palette' | 'help', mode, input, list, foot, ctx, shown, nodes, sel, selId, moreOpen, timer, sig } while open
  ROUTES: [['Home', '#/', 'g h'], ['Needs you', '#/inbox', 'g i'], ['Agents', '#/agents', 'g a'], ['Tasks', '#/tasks', 'g t'],
    ['Quad', '#/quad', 'g q'], ['Usage', '#/usage', 'g u'], ['Memory', '#/memory', 'g m'], ['Settings', '#/settings', 'g s'], ['Search', '#/search', '/']],
  CONTROLS: ['/compact', '/context', '/cost', '/usage', '/status'],                    // read-only or harmless: no argument, nothing destructive
  AGENTS_KEY: 'ccboard:agents',                                                          // sessionStorage: the GET /api/agents answer, cached by the terminal page's tuning strip
  AGENTS_TTL: 10 * 60 * 1000,
  MODES: ['ultracode ', 'plan '],
  MORE: ['/color', '/copy', '/rewind', '/radio', '/stickers', '/tui', '/passes'],
  NUDGE_STATES: ['waiting', 'idle', 'done', 'working', 'errored'],
  SESSIONS_SHOWN: 9,        // the Sessions group of an empty search: the nine mod+1..9 reach
  PER_GROUP: 12,            // most rows of one group while searching
  TICK_MS: 1500,
  SKILLS_TOP: 5,            // the Skills group of an empty search: this many rows, then "All skills…"
  SKILLS_OPEN_MAX: 60,      // rows of the opened list
  SKILLS_TTL: 60 * 1000,    // the client keeps GET /api/skills this long (the server caches 60 s as well)
  skills: { list: null, at: 0, pending: null },
};

/* ---------- small helpers ---------- */

Palette.say = function (text, kind) {
  if (typeof toast === 'function') toast(text, { kind: kind || 'info' });
  else if (typeof pageToast === 'function') pageToast(text, kind || 'info');
};

Palette.go = function (hash) { if (typeof navigate === 'function') navigate(hash); else location.hash = hash; };

Palette.modLabel = function () {
  let mac = false;
  try { mac = typeof Keymap !== 'undefined' && typeof Keymap.isMac === 'function' ? Keymap.isMac() : /Mac|iP(?:hone|ad|od)/i.test(navigator.platform || ''); } catch (_) { /* no navigator */ }
  return mac ? '⌘' : 'Ctrl+';
};

Palette.pages = function () { return typeof Pages !== 'undefined' ? Pages : null; };

Palette.stateOf = function (s) { return typeof ownKey === 'function' && ownKey(STATE_GLYPH, s.state) ? s.state : 'unknown'; };

Palette.where = function (s) {
  if (typeof sessionWhere === 'function') return sessionWhere(s, true);
  return s.project ? `${s.project}/${s.repo || '?'}` : String(s.repo || '');
};

/* The address of a session: a tmux name or a roster row. A row of another node gets #/n/<handle>/s/<tmux> (nodes.js); this board's keep #/s/<tmux>. */
Palette.sessionHash = function (x) {
  if (x && typeof x === 'object' && Ref.nodeOf(x) !== null) return Ref.hash(x) || '#/';
  const tmux = x && typeof x === 'object' ? x.tmux : x;
  return typeof sessionHash === 'function' ? sessionHash(tmux) : '#/s/' + tmux;
};

Palette.nudgeable = function (s) {
  if (typeof sessionNudgeable === 'function') return sessionNudgeable(s);
  const agent = typeof sessionAgent === 'function' ? sessionAgent(s) : (s.agent || 'claude');
  return agent !== 'shell' && Palette.NUDGE_STATES.includes(s.state);
};

/* ---------- fuzzy match ---------- */

/* Score `text` against `query` (null when it does not match): a substring beats a subsequence, a prefix and word starts beat the middle of a word,
   and a tight match beats a scattered one. hits are the matched character positions, for the highlight. `strict` allows substrings only (the
   hint and keyword text of a row: a scattered match in a long project path would rank noise above a real command). */
Palette.score = function (query, text, strict) {
  const q = String(query).toLowerCase().replace(/\s+/g, '');
  const t = String(text).toLowerCase();
  if (!q) return { score: 0, hits: [] };
  const boundary = (i) => i === 0 || ' /-_·.:'.includes(t.charAt(i - 1));
  const at = t.indexOf(q);
  if (at >= 0) {
    const hits = [];
    for (let i = 0; i < q.length; i++) hits.push(at + i);
    return { score: 200 + (at === 0 ? 60 : boundary(at) ? 30 : 0) - at - (t.length - q.length) / 10, hits };
  }
  if (strict) return null;
  const hits = [];
  let from = 0;
  let last = -2;
  let score = 0;
  for (const ch of q) {
    const i = t.indexOf(ch, from);
    if (i < 0) return null;
    score += 10 + (i === last + 1 ? 8 : 0) + (boundary(i) ? 6 : 0) - Math.min(i - from, 6);
    hits.push(i);
    last = i;
    from = i + 1;
  }
  return { score, hits };
};

Palette.matchItem = function (query, item) {
  const lab = Palette.score(query, item.label);
  const rest = Palette.score(query, [item.hint, item.keywords].filter(Boolean).join(' '), true);
  if (!lab && !rest) return null;
  const a = lab ? lab.score : -Infinity;
  const b = rest ? rest.score / 2 : -Infinity;
  return { score: Math.max(a, b), hits: lab ? lab.hits : [] };
};

/* label as text and <span class="pal-hit"> runs for the matched characters */
Palette.highlight = function (label, hits) {
  if (!hits || !hits.length) return [label];
  const set = new Set(hits);
  const out = [];
  let run = '';
  let on = false;
  const flush = () => { if (run) out.push(on ? el('span', { class: 'pal-hit', text: run }) : run); run = ''; };
  for (let i = 0; i < label.length; i++) {
    const h = set.has(i);
    if (h !== on) { flush(); on = h; }
    run += label.charAt(i);
  }
  flush();
  return out;
};

/* ---------- what the palette can do right now ---------- */

Palette.context = function () {
  const P = Palette.pages();
  const sessions = P ? P.order() : [];
  const t = P ? P.target() : null;
  return { sessions, target: t ? (sessions.find((x) => Ref.key(x) === t) || null) : null, targetTmux: t || null, box: P && typeof P.sendBox === 'function' ? P.sendBox() : null,
    skills: Palette.skills.list, composer: Palette.composer() };
};

/* The skills of the box (GET /api/skills), once per palette open and at most every SKILLS_TTL: a failed or empty answer leaves the group out (no toast on every open).
   `after` runs when an answer lands, to redraw the open palette. */
Palette.loadSkills = function (after) {
  const k = Palette.skills;
  if (k.list !== null && Date.now() - k.at < Palette.SKILLS_TTL) return Promise.resolve(k.list);
  if (k.pending) return k.pending;
  if (typeof api !== 'function') return Promise.resolve(k.list);
  k.pending = Promise.resolve(api('GET', '/api/skills')).then((r) => {
    const list = r && Array.isArray(r.skills) ? r.skills.filter((x) => x && typeof x.name === 'string' && /^[a-z0-9][a-z0-9:_-]{0,63}$/.test(x.name)).map((x) => ({
      name: x.name, description: typeof x.description === 'string' ? x.description : '', source: typeof x.source === 'string' ? x.source : '', uses: typeof x.uses === 'number' && x.uses > 0 ? x.uses : 0 }))
      .sort((a, b) => b.uses - a.uses || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) : [];                  // most used first, then by name, whatever order the server sent
    k.list = list;
    k.at = Date.now();
    return list;
  }).catch(() => k.list).then((list) => { k.pending = null; if (typeof after === 'function') after(list); return list; });
  return k.pending;
};

/* The composer the person came from: the field that had the focus when the palette opened, when it is a text box (the peek's send box, a terminal composer); else null. */
Palette.composer = function () {
  const u = Palette.ui;
  const n = u && u.from;
  if (!n || n.isConnected === false || !/^(?:TEXTAREA|INPUT)$/.test(String(n.tagName || '').toUpperCase())) return null;
  if (u.input && n === u.input) return null;
  return n;
};

/* POST the text into the session's terminal (enter: true presses Enter), then say so. */
Palette.send = async function (tmux, text, name, enter) {
  const shown = text.length > 48 ? text.slice(0, 48) + '…' : text;
  try {
    await api('POST', `/api/sessions/${encodeURIComponent(tmux)}/keys`, { text, enter: enter !== false });
    Palette.say(`sent "${shown}" to ${name || tmux}`, 'ok');
    return true;
  } catch (e) { Palette.say(e.message, 'bad'); return false; }
};

/* The slash registry ({cmd: {label, arg, read, weight, ...}}) of `agent` from the sessionStorage copy of GET /api/agents (written by the terminal
   page's tuning strip, at most AGENTS_TTL old), or null when there is none. Tolerates a wrapped value ({at|t, data|value|v}) and the bare answer. */
Palette.registry = function (agent) {
  try {
    const raw = typeof sessionStorage !== 'undefined' ? sessionStorage.getItem(Palette.AGENTS_KEY) : null;
    if (!raw) return null;
    const v = JSON.parse(raw);
    if (!v || typeof v !== 'object') return null;
    let at = [v.at, v.t, v.ts, v.time].find((x) => typeof x === 'number');
    if (typeof at === 'number') {
      if (at < 1e11) at *= 1000;                                                         // seconds
      if (Date.now() - at > Palette.AGENTS_TTL) return null;
    }
    const data = [v.data, v.value, v.v].find((x) => x && typeof x === 'object') || v;
    const agents = data.agents && typeof data.agents === 'object' ? data.agents : data;
    const a = agents[agent || 'claude'];
    return a && a.slash && typeof a.slash === 'object' ? a.slash : null;
  } catch (_) { return null; }
};

/* The controls offered for session `s`: [{ text: '/compact', key: 'compact', label }]. With the registry cached, the registry's labels and order
   (heaviest first) and only what that agent has; otherwise the static list. */
Palette.controlsFor = function (s) {
  const reg = Palette.registry(s && (s.agent || (typeof sessionAgent === 'function' ? sessionAgent(s) : null)));
  const out = [];
  Palette.CONTROLS.forEach((text, i) => {
    const key = text.slice(1);
    const spec = reg && ownKey(reg, key) ? reg[key] : null;
    if (reg && !spec) return;
    out.push({ text, key, label: spec && spec.label ? spec.label : text, weight: spec && typeof spec.weight === 'number' ? spec.weight : 0, i });
  });
  if (reg) out.sort((a, b) => b.weight - a.weight || a.i - b.i);
  return out;
};

/* Why /command said no, for a toast: the message of a 409 (error is only its code) and when asking again can work. */
Palette.refusal = function (e) {
  const b = e && e.body && typeof e.body === 'object' ? e.body : {};
  const wait = typeof b.retry === 'number' && b.retry > 0 ? ` (try again in ${b.retry} s)` : '';
  return (b.message || (e && e.message) || 'the command was refused') + wait;
};

/* A read command leaves Claude's own dialog open on the pane: show what it printed and press Escape when the readout closes. The readout is its
   own <dialog> (components.js modalShell), not the sheet: a peek that is open in the sheet must survive it. */
Palette.readout = function (tmux, cmd, name, screen) {
  if (typeof modalShell !== 'function' || typeof el !== 'function') return false;
  const title = `/${cmd} · ${name || tmux}`;
  const shut = () => { Promise.resolve(api('POST', `/api/sessions/${encodeURIComponent(tmux)}/keys`, { keys: ['Escape'] })).catch(() => {}); };
  const shell = modalShell('qr-editor readout', title, shut);
  shell.dlg.append(el('div', { class: 'qr-box' },
    el('h2', { class: 'qr-title', text: title }),
    el('pre', { class: 'cmd-readout', text: String(screen).replace(/\s+$/, '') }),
    el('div', { class: 'qr-actions' }, el('button', { type: 'button', class: 'primary', onclick: () => shell.close(), text: 'Close' }))));
  shell.show();
  return true;
};

/* One control through the guarded endpoint: POST /command {cmd} ('compact', no slash). 409 says why (working, permission, compacting ...) and
   when to retry; 404 without an error body is a server that has no /command yet, so the old way (type it through /keys) is used. */
Palette.command = async function (tmux, cmd, name) {
  const base = `/api/sessions/${encodeURIComponent(tmux)}`;
  try {
    const r = await api('POST', base + '/command', { cmd });
    if (r && typeof r.screen === 'string' && r.screen.trim() && Palette.readout(tmux, cmd, name, r.screen)) return true;
    Palette.say(`sent "/${cmd}" to ${name || tmux}`, 'ok');
    return true;
  } catch (e) {
    if (e && e.status === 404 && !(e.body && e.body.error)) return Palette.send(tmux, '/' + cmd, name);
    Palette.say(Palette.refusal(e), e && e.status === 409 ? 'warn' : 'bad');
    return false;
  }
};

/* Put text into the peek's send box: modes go in front, a command goes in at the caret (or alone in an empty box). With no peek open the
   selected session's peek is opened first; with neither there is no box to fill. */
Palette.fillBox = function (box, text, how) {
  const v = box.value || '';
  let next;
  let caret;
  if (how === 'prefix') { next = v.startsWith(text) ? v : text + v; caret = next.length; }
  else if (!v) { next = text; caret = text.length; }
  else {
    const a = typeof box.selectionStart === 'number' ? box.selectionStart : v.length;
    const b = typeof box.selectionEnd === 'number' ? box.selectionEnd : a;
    next = v.slice(0, a) + text + v.slice(b);
    caret = a + text.length;
  }
  box.value = next;
  box.focus();
  try { box.setSelectionRange(caret, caret); } catch (_) { /* not a text input */ }
  try { box.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) { /* no Event constructor */ }
};

Palette.insert = function (text, how, into) {
  const P = Palette.pages();
  const fill = (box) => Palette.fillBox(box, text, how);
  if (into) { fill(into); return true; }                          // the composer the palette was opened from
  if (!P || typeof P.withSendBox !== 'function') return false;
  if (!P.sendBox()) {
    const t = P.target();
    if (!t) { Palette.say('Open a session first: the text goes into its send box.', 'warn'); return false; }
    if (P.peekTmux() !== t) Palette.go(Palette.sessionHash(t));
  }
  return P.withSendBox(fill);
};

/* `@box fix`: a leading @token that names a paired node (its handle or name, or the one handle it starts; @local or @this is this board) narrows Sessions and the tasks of other
   nodes to that node and filters by the rest. null for anything else, so a text that merely starts with @ is searched as typed. */
Palette.nodePrefix = function (query) {
  const m = /^@([A-Za-z0-9-]+)(?:\s+([\s\S]*))?$/.exec(String(query || '').trim());
  if (!m || typeof Nodes === 'undefined' || !Nodes.ready || !Nodes.enabled()) return null;
  const tok = m[1].toLowerCase();
  const rest = (m[2] || '').trim();
  if (tok === 'local' || tok === 'this') return { node: 'local', rest };
  const recs = Nodes.list();
  // A handle is ours (the registry's); a name is what the node called itself when it paired. So an exact handle wins over every name, a name counts only when exactly
  // one node has it, and a start matches handles only: a node cannot take another node's @handle by naming itself after it.
  let hit = recs.find((r) => r.handle === tok);
  if (!hit) { const named = recs.filter((r) => String(r.name || '').toLowerCase() === tok); if (named.length === 1) hit = named[0]; }
  if (!hit) { const some = recs.filter((r) => r.handle.startsWith(tok)); if (some.length === 1) hit = some[0]; }
  return hit ? { node: hit.handle, rest } : null;
};

/* The groups the palette offers, as [{ id, title, items: [{ id, label, hint, kbd, glyph, keywords, run, keep? }] }] before any filtering. */
Palette.catalog = function (ctx, query) {
  const groups = [];
  const close = () => Palette.close();
  const sessions = ctx.sessions.map((s, i) => {
    const st = Palette.stateOf(s);
    const glyph = stateGlyph(st);
    glyph.setAttribute('aria-hidden', 'true');
    glyph.removeAttribute('title');
    return { id: 's:' + Ref.key(s), label: s.name || s.tmux, hint: `${Palette.where(s)} · ${GLYPH_LABEL[st]}`, kbd: i < 9 ? Palette.modLabel() + (i + 1) : '', glyph,
      keywords: `${s.project || ''} ${s.repo || ''} ${s.tmux}${Ref.nodeOf(s) ? ' ' + Ref.nodeOf(s) : ''}`, run: () => { close(); Palette.go(Palette.sessionHash(s)); } };
  });
  const hub = typeof Nodes !== 'undefined' && Nodes.enabled() && Nodes.ready && Nodes.list().length;
  if (hub) {                                                   // paired nodes (nodes-hub.js): their sessions after this board's, each with its node chip; the tasks and the Nodes group below
    for (const s of Nodes.sessions()) {
      const st = Palette.stateOf(s);
      const glyph = stateGlyph(st);
      glyph.setAttribute('aria-hidden', 'true');
      glyph.removeAttribute('title');
      sessions.push({ id: 's:' + Ref.key(s), label: s.session || s.tmux, hint: `${Palette.where(s)} · ${GLYPH_LABEL[st]}`, kbd: '', glyph, chip: Nodes.chip(s.node), node: s.node,
        keywords: `${s.project || ''} ${s.repo || ''} ${s.tmux} ${s.node}`, run: () => { close(); Palette.go(Palette.sessionHash(s)); } });
    }
  }
  groups.push({ id: 'sessions', title: 'Sessions', items: sessions, limit: query || ctx.scope ? Palette.PER_GROUP : Palette.SESSIONS_SHOWN });
  if (hub && (query || ctx.scope)) {
    groups.push({ id: 'rtasks', title: 'Tasks on other nodes', limit: Palette.PER_GROUP, items: Nodes.tasks().map((t) => ({ id: 't:' + Ref.key({ kind: 'task', node: t.node, id: t.id }), label: String(t.title || `Task ${t.id}`),
      hint: `${Palette.where(t)} · ${t.phase || ''}`, chip: Nodes.chip(t.node), node: t.node, keywords: `task ${t.node} ${t.issue_ref || ''}`, run: () => { close(); Palette.go(Ref.hash(Ref.task(t.node, t.id)) || '#/'); } })) });
  }
  if (ctx.scope) return groups.filter((g) => g.id === 'sessions' || g.id === 'rtasks').map((g) => ({ ...g, items: g.items.filter((it) => (Ref.nodeOf(it.node) || 'local') === ctx.scope) }));

  // With nothing typed, Search is a plain route. With a query it moves to a group of its own at the very end (never filtered out, never ranked above
  // a real match): Enter on the best match then runs that match, and "search transcripts for ..." is the last row.
  const routes = Palette.ROUTES.filter((r) => r[1] !== '#/search' || !query).map(([label, hash, kbd]) => ({ id: 'r:' + hash, label, hint: hash === '#/search' ? 'transcripts' : '', kbd,
    run: () => { close(); Palette.go(hash === '#/quad' && typeof Shell !== 'undefined' && Shell && typeof Shell.quadHref === 'function' ? Shell.quadHref() : hash); } }));      // the quad opens the scope last used
  groups.push({ id: 'routes', title: 'Routes', items: routes });
  if (hub) {
    const items = [];
    Nodes.list().forEach((r, i) => {
      const name = typeof r.name === 'string' && r.name ? r.name : r.handle;
      const url = Nodes.boardUrl(r.url);
      items.push({ id: 'nd:' + r.handle, label: `Go to ${name}`, hint: Nodes.view(r).word, kbd: i === 0 ? 'g n' : '', keywords: `node ${r.handle}`, run: () => { close(); Palette.go(Ref.hash(Ref.node(r.handle)) || '#/'); } });
      items.push({ id: 'nb:' + r.handle, label: `Open board on ${name}`, hint: url ? 'new tab' : '', keywords: `node ${r.handle} open board`, off: url ? '' : 'The address saved for this node is not an https tailnet address.',
        run: () => { if (!url) { Palette.say('The address saved for this node is not an https tailnet address.', 'warn'); return; } close(); window.open(url, '_blank', 'noopener,noreferrer'); } });
    });
    groups.push({ id: 'nodes', title: 'Nodes', items, limit: query ? Palette.PER_GROUP : 4 });
  }

  // Quad: <project> for every project with a live session: only for a search that starts like the word ("qu", "quad", "quad pet"), so a project name alone still finds its
  // sessions first and the empty palette keeps its short list
  const first = String(query || '').trim().toLowerCase().split(/[\s:]+/)[0];
  if (first.length >= 2 && 'quad'.startsWith(first)) {
    const live = new Map();
    for (const s of ctx.sessions) if (s.project && s.state !== 'ended' && /^[A-Za-z0-9_-]+$/.test(s.project)) live.set(s.project, (live.get(s.project) || 0) + 1);
    const quads = Array.from(live).sort((a, b) => a[0].localeCompare(b[0])).map(([project, n]) => ({ id: 'q:' + project, label: `Quad: ${project}`, hint: `${n} live session${n === 1 ? '' : 's'}`, keywords: 'terminals side by side',
      run: () => { close(); Palette.go(typeof buildHash === 'function' ? buildHash('quad', {}, { p: project }) : '#/quad?p=' + project); } }));
    groups.push({ id: 'quads', title: 'Quad', items: quads });
  }

  // Backlog cards: Move… is the keyboard route of the drag (the card's m key opens the same sheet). Three with nothing typed.
  if (typeof boardTasks === 'function' && typeof taskMoveable === 'function' && typeof taskMoveSheet === 'function' && typeof state !== 'undefined' && state) {
    let cards = [];
    try { cards = boardTasks(state).filter((x) => taskMoveable(x)); } catch (_) { cards = []; }
    if (cards.length) groups.push({ id: 'backlog', title: 'Backlog', limit: query ? Palette.PER_GROUP : 3, items: cards.map((x) => ({ id: 'k:' + x.id, label: `Move: ${x.title}`, hint: `${x.project}/${x.repo}`,
      keywords: 'task backlog start dispatch hand', run: () => { close(); taskMoveSheet(x); } })) });
  }

  const t = ctx.target;
  if (t && Palette.nudgeable(t)) {
    const name = t.name || t.tmux;
    const nudges = quickLoad(t, sessionAgent(t));                  // #69: the session's quick replies (components.js), its agent's defaults until edited
    groups.push({ id: 'nudges', title: `Nudge · ${name}`, items: nudges.map((text) => ({ id: 'n:' + text, label: text, hint: 'typed into ' + name, run: () => { close(); Palette.send(t.tmux, text, name); } })) });
    groups.push({ id: 'controls', title: `Controls · ${name}`, items: Palette.controlsFor(t).map((c) => ({ id: 'c:' + c.text, label: c.label, hint: c.label === c.text ? 'typed into ' + name : `${c.text} · typed into ${name}`, keywords: c.text,
      run: () => { close(); Palette.command(t.tmux, c.key, name); } })) });
  }

  // Skills (Claude sessions only: a Codex or shell session has no list of its own that is verified). Insert, never send; a row says why it cannot when there is no composer.
  const agentOf = t ? (typeof sessionAgent === 'function' ? sessionAgent(t) : (t.agent || 'claude')) : null;
  if (Array.isArray(ctx.skills) && ctx.skills.length && (!t || agentOf === 'claude')) {
    const canInsert = !!(ctx.composer || ctx.box || ctx.targetTmux);
    const open = !!ctx.skillsOpen;
    const rows = ctx.skills.map((sk) => ({ id: 'sk:' + sk.name, label: sk.name, hint: sk.description, act: 'Insert', note: sk.uses ? `seen ${sk.uses}×` : '', keywords: `skill ${sk.source}`,
      off: canInsert ? '' : 'Open a Claude session first: the command goes into its send box.',
      run: () => {
        if (!canInsert) { Palette.say('Open a Claude session first: the command goes into its send box.', 'warn'); return; }
        const into = ctx.composer;
        close();
        Palette.insert(`/${sk.name} `, 'command', into);
      } }));
    groups.push({ id: 'skills', title: t ? `Skills · ${t.name || t.tmux}` : 'Skills', items: rows, limit: query ? (open ? Palette.SKILLS_OPEN_MAX : Palette.PER_GROUP) : (open ? Palette.SKILLS_OPEN_MAX : Palette.SKILLS_TOP),
      toggle: !query && ctx.skills.length > Palette.SKILLS_TOP ? { open, count: ctx.skills.length } : null });
  }

  if (ctx.box || ctx.targetTmux) {
    // #78: the typed word `ultracode` starts a workflow for that one prompt; the session setting is Tune's Ultracode switch (/effort ultracode on | off)
    const modeHint = (text) => (text.trim() === 'ultracode' ? 'insert: a workflow for this one prompt (the setting is in Tune)' : 'insert into the send box');
    groups.push({ id: 'modes', title: 'Modes', items: Palette.MODES.map((text) => ({ id: 'm:' + text, label: text.trim(), hint: modeHint(text), run: () => { close(); Palette.insert(text, 'prefix'); } })) });
    groups.push({ id: 'more', title: 'More', collapsed: true, items: Palette.MORE.map((text) => ({ id: 'x:' + text, label: text, hint: 'insert into the send box', run: () => { close(); Palette.insert(text, 'command'); } })) });
  }
  if (query) {
    groups.push({ id: 'search', title: 'Search', items: [{ id: 'r:search', label: `Search transcripts for "${query}"`, hint: '', kbd: '', always: true,
      run: () => { close(); Palette.go('#/search?q=' + encodeURIComponent(query)); } }] });
  }
  return groups;
};

/* The shown groups for `query`: fuzzy-filtered and ranked, empty groups dropped, More collapsed to one row until opened or searched. */
Palette.groups = function (ctx, query, moreOpen, skillsOpen) {
  const pre = Palette.nodePrefix(query);
  const q = pre ? pre.rest : String(query || '').trim();
  const out = [];
  for (const g of Palette.catalog({ ...ctx, skillsOpen: !!skillsOpen, scope: pre ? pre.node : null }, q)) {
    let items = g.items;
    let best = 0;
    if (q) {
      const scored = [];
      for (const it of items) {
        const m = it.always ? { score: -1e9, hits: [] } : Palette.matchItem(q, it);
        if (m) scored.push({ it: { ...it, hits: m.hits }, score: m.score });
      }
      scored.sort((a, b) => b.score - a.score);
      items = scored.slice(0, g.limit || Palette.PER_GROUP).map((x) => x.it);
      best = scored.length ? scored[0].score : -Infinity;
    } else if (g.collapsed && !moreOpen) {
      items = [{ id: 'more:toggle', label: 'More commands…', hint: `${g.items.length} commands`, keep: true, run: () => { const u = Palette.ui; if (u) { u.moreOpen = true; u.selId = 'x:' + Palette.MORE[0]; Palette.render(); } } }];
    } else if (g.limit) items = items.slice(0, g.limit);
    if (g.toggle && items.length) {                                             // "All skills…" opens the rest (and the search box filters them); "Fewer skills" closes it again
      items = [...items, { id: 'skills:toggle', label: g.toggle.open ? 'Fewer skills' : 'All skills…', hint: g.toggle.open ? `top ${Palette.SKILLS_TOP}` : `${g.toggle.count} installed on the box`, keep: true,
        run: () => { const u = Palette.ui; if (u) { u.skillsOpen = !g.toggle.open; u.selId = g.toggle.open ? 'skills:toggle' : items[Palette.SKILLS_TOP] && items[Palette.SKILLS_TOP].id; Palette.render(); } } }];
    }
    if (items.length) out.push({ id: g.id, title: g.title, items, best });
  }
  if (q) out.sort((a, b) => b.best - a.best);       // searching: the group that holds the best match leads, so Enter runs the best match
  return out;
};

/* send mode: the sessions that can take text, the peeked or selected one first */
Palette.sendGroups = function (ctx) {
  const list = ctx.sessions.filter((s) => Palette.nudgeable(s) && Ref.nodeOf(s) === null);      // text goes to this board's sessions only until the relay phase
  list.sort((a, b) => (Ref.key(b) === ctx.targetTmux ? 1 : 0) - (Ref.key(a) === ctx.targetTmux ? 1 : 0));
  const items = list.map((s) => {
    const st = Palette.stateOf(s);
    const glyph = stateGlyph(st);
    glyph.setAttribute('aria-hidden', 'true');
    glyph.removeAttribute('title');
    return { id: 'send:' + Ref.key(s), label: s.name || s.tmux, hint: `${Palette.where(s)} · ${GLYPH_LABEL[st]}`, glyph, session: s, run: (e) => Palette.sendShared(s, e) };
  });
  return items.length ? [{ id: 'send', title: 'Send to session', items }] : [];
};

Palette.sendShared = function (s, e) {
  const u = Palette.ui;
  const text = u && u.input ? u.input.value.trim() : '';
  if (!text) { Palette.say('Nothing to send.', 'warn'); return; }
  if (Ref.nodeOf(s) !== null) { Palette.say('That session is on another node: sending to it arrives with the relay.', 'warn'); return; }      // Palette.send posts to this board by tmux name
  Palette.close();
  Palette.send(s.tmux, text, s.name || s.tmux, !(e && e.shiftKey));
};

/* ---------- the dialog ---------- */

Palette.dialog = function () { return typeof document !== 'undefined' ? document.getElementById('helpdlg') : null; };

Palette.isOpen = function () { return !!(Palette.ui && Palette.ui.dlg && Palette.ui.dlg.open); };

Palette.wire = function (dlg) {
  if (dlg._palWired) return;
  dlg._palWired = true;
  dlg.addEventListener('close', () => Palette.closed(dlg));
  dlg.addEventListener('click', (e) => { if (e.target === dlg) Palette.close(); });     // the dialog has no padding: a click on itself is the backdrop
};

Palette.stopTimer = function () {
  const u = Palette.ui;
  if (u && u.timer) { clearInterval(u.timer); u.timer = null; }
};

/* Leaving whatever is open (a new open() swaps content in place, close() ends it): stop its timer, and drop the share params of a send palette. */
Palette.leave = function () {
  const u = Palette.ui;
  Palette.stopTimer();
  Palette.ui = null;
  if (u && u.mode === 'send' && typeof Router !== 'undefined' && typeof Router.stripShare === 'function') Router.stripShare();
};

Palette.closed = function (dlg) {
  if (dlg.open) return;                                   // the 'close' event is queued: a palette opened again in the meantime is not this one
  if (!Palette.ui || Palette.ui.dlg !== dlg) { dlg.textContent = ''; return; }
  Palette.leave();
  dlg.textContent = '';
  dlg.classList.remove('palette', 'help');
};

Palette.close = function () {
  const dlg = (Palette.ui && Palette.ui.dlg) || Palette.dialog();
  if (!dlg) return;
  if (dlg.open) {
    try { dlg.close(); } catch (_) { dlg.removeAttribute('open'); Palette.closed(dlg); }
  } else if (Palette.ui) Palette.closed(dlg);
};

Palette.show = function (dlg) {
  if (!dlg.open) { try { dlg.showModal(); } catch (_) { dlg.setAttribute('open', ''); } }
};

Palette.toggle = function () {
  if (Palette.isOpen() && Palette.ui.view === 'palette' && Palette.ui.mode === 'default') Palette.close();
  else Palette.open();
};

Palette.open = function (opts) {
  const dlg = Palette.dialog();
  if (!dlg) return null;
  const o = opts || {};
  const mode = o.mode === 'send' ? 'send' : 'default';
  Palette.wire(dlg);
  Palette.leave();
  const input = el('input', { class: 'pal-input', type: 'text', role: 'combobox', 'aria-expanded': 'true', 'aria-controls': 'pal-list', 'aria-autocomplete': 'list',
    'aria-label': mode === 'send' ? 'Text to send' : 'Search sessions, pages and commands', placeholder: mode === 'send' ? 'Text to send' : 'Search sessions, pages and commands',
    autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', enterkeyhint: mode === 'send' ? 'send' : 'go' });
  input.value = String(o.text || '');
  const list = el('div', { class: 'pal-list', id: 'pal-list', role: 'listbox', 'aria-label': mode === 'send' ? 'Sessions' : 'Results' });
  const foot = el('div', { class: 'pal-foot' });
  dlg.textContent = '';
  dlg.className = 'palette';
  dlg.setAttribute('aria-label', mode === 'send' ? 'Send to a session' : 'Command palette');
  dlg.append(el('div', { class: 'pal-head' }, input), list, foot);
  const from = typeof document !== 'undefined' ? document.activeElement : null;
  const u = { dlg, view: 'palette', mode, input, list, foot, from, ctx: null, shown: [], nodes: [], sel: 0, selId: null, moreOpen: false, skillsOpen: false, timer: null, sig: typeof ui !== 'undefined' ? ui.lastJson : null };
  Palette.ui = u;
  u.ctx = Palette.context();
  Nodes.use(() => Palette.refresh());                                                  // paired nodes: their rows join once the hub bundle is here
  input.addEventListener('keydown', Palette.key);
  input.addEventListener('input', () => { if (u.mode === 'default') { u.selId = null; Palette.render(); } });
  list.addEventListener('mousedown', (e) => e.preventDefault());                       // a click on a row must not take the focus from the box
  list.addEventListener('click', (e) => {
    const row = e.target && typeof e.target.closest === 'function' ? e.target.closest('.pal-item') : null;
    const i = row ? u.nodes.indexOf(row) : -1;
    if (i >= 0) { u.sel = i; Palette.paintSel(false); Palette.runSelected(e); }
  });
  Palette.render();
  Palette.show(dlg);
  input.focus();
  if (mode === 'default') Palette.loadSkills(() => { if (Palette.ui === u && u.view === 'palette') { u.ctx = Palette.context(); Palette.render(); } });
  try { input.setSelectionRange(input.value.length, input.value.length); } catch (_) { /* not a text input */ }
  if (typeof setInterval === 'function') {
    u.timer = setInterval(Palette.tick, Palette.TICK_MS);
    if (u.timer && typeof u.timer.unref === 'function') u.timer.unref();
  }
  return u;
};

/* The state moved on while the palette was open (a session appeared, the first poll arrived after a share): redraw, keeping the text and the row. */
Palette.tick = function () {
  const u = Palette.ui;
  if (!u || u.view !== 'palette' || !u.dlg.open) return;
  const sig = typeof ui !== 'undefined' ? ui.lastJson : null;
  if (sig === u.sig) return;
  u.sig = sig;
  u.ctx = Palette.context();
  Palette.render();
};

/* Redraw the open palette (the nodes' reading changed, or the hub bundle arrived), keeping the text and the row. */
Palette.refresh = function () {
  const u = Palette.ui;
  if (u && u.view === 'palette' && u.dlg.open) { u.ctx = Palette.context(); Palette.render(); }
};

Palette.itemNode = function (it, id) {
  return el('div', { class: 'pal-item' + (it.off ? ' off' : ''), role: 'option', id, 'aria-selected': 'false', 'aria-disabled': it.off ? 'true' : null },
    it.glyph || null,
    el('span', { class: 'pal-label' }, ...Palette.highlight(it.label, it.hits)),
    it.chip || null,
    it.off ? el('span', { class: 'pal-hint', text: it.off }) : (it.hint ? el('span', { class: 'pal-hint', text: it.hint }) : null),
    it.note ? el('span', { class: 'pal-note', text: it.note }) : null,
    it.act ? el('span', { class: 'pal-act', text: it.act }) : null,
    it.kbd ? el('kbd', { class: 'pal-kbd', text: it.kbd }) : null);
};

Palette.footText = function (mode) {
  return mode === 'send' ? '↑↓ choose a session · ↵ send · ⇧↵ type without Enter · esc cancel' : `↑↓ move · ↵ run · esc close · ${Palette.modLabel()}K toggles`;
};

Palette.render = function () {
  const u = Palette.ui;
  if (!u || u.view !== 'palette') return;
  const q = u.mode === 'send' ? '' : u.input.value.trim();
  const groups = u.mode === 'send' ? Palette.sendGroups(u.ctx) : Palette.groups(u.ctx, q, u.moreOpen, u.skillsOpen);
  u.list.textContent = '';
  u.shown = [];
  u.nodes = [];
  for (const g of groups) {
    u.list.append(el('div', { class: 'pal-group', role: 'presentation', text: g.title }));
    for (const it of g.items) {
      const node = Palette.itemNode(it, 'pal-o-' + u.nodes.length);
      u.list.append(node);
      u.shown.push(it);
      u.nodes.push(node);
    }
  }
  if (!u.shown.length) u.list.append(el('div', { class: 'pal-empty', text: u.mode === 'send' ? 'No session can take text right now.' : `Nothing matches "${q}".` }));
  const at = u.selId ? u.shown.findIndex((x) => x.id === u.selId) : -1;
  u.sel = at >= 0 ? at : 0;
  setText(u.foot, Palette.footText(u.mode));
  Palette.paintSel(true);
};

Palette.paintSel = function (scroll) {
  const u = Palette.ui;
  if (!u) return;
  u.nodes.forEach((n, i) => { n.classList.toggle('sel', i === u.sel); n.setAttribute('aria-selected', i === u.sel ? 'true' : 'false'); });
  const cur = u.nodes[u.sel];
  u.selId = u.shown[u.sel] ? u.shown[u.sel].id : null;
  if (cur) u.input.setAttribute('aria-activedescendant', cur.getAttribute('id')); else u.input.removeAttribute('aria-activedescendant');
  if (scroll && cur && typeof cur.scrollIntoView === 'function') cur.scrollIntoView({ block: 'nearest' });
};

Palette.move = function (d) {
  const u = Palette.ui;
  const n = u ? u.shown.length : 0;
  if (!n) return;
  u.sel = Math.abs(d) > 1 ? Math.max(0, Math.min(n - 1, u.sel + d)) : (u.sel + d + n) % n;
  Palette.paintSel(true);
};

Palette.runSelected = function (e) {
  const u = Palette.ui;
  const it = u && u.shown[u.sel];
  if (!it) return;
  try { it.run(e); } catch (err) { console.error('ccboard palette', it.id, err); }
};

Palette.key = function (e) {
  if (e.isComposing) return;
  const k = e.key;
  const ctrl = e.ctrlKey && !e.metaKey && !e.altKey;
  if (k === 'ArrowDown' || (ctrl && (k === 'n' || k === 'N'))) { e.preventDefault(); Palette.move(1); }
  else if (k === 'ArrowUp' || (ctrl && (k === 'p' || k === 'P'))) { e.preventDefault(); Palette.move(-1); }
  else if (k === 'PageDown') { e.preventDefault(); Palette.move(5); }
  else if (k === 'PageUp') { e.preventDefault(); Palette.move(-5); }
  else if (k === 'Enter') { e.preventDefault(); Palette.runSelected(e); }
  else if (k === 'Escape') { e.preventDefault(); Palette.close(); }
};

/* ---------- the shortcut help ---------- */

Palette.openHelp = function () {
  const dlg = Palette.dialog();
  if (!dlg) return null;
  Palette.wire(dlg);
  Palette.leave();
  const rows = typeof Keymap !== 'undefined' && typeof Keymap.help === 'function' ? Keymap.help() : [];
  const body = el('div', { class: 'help-body' });
  if (!rows.length) body.append(el('p', { class: 'pal-empty', text: 'The keyboard layer is not loaded.' }));
  const order = [];
  const by = new Map();
  for (const r of rows) { if (!by.has(r.group)) { by.set(r.group, []); order.push(r.group); } by.get(r.group).push(r); }
  const keys = (r) => {
    const out = [];
    r.keys.forEach((k, i) => { if (i) out.push(' then '); out.push(el('kbd', { class: 'pal-kbd', text: k })); });
    return out;
  };
  for (const g of order) {
    body.append(el('section', { class: 'help-group' }, el('h3', { text: g }),
      ...by.get(g).map((r) => el('div', { class: 'help-row' }, el('span', { class: 'help-keys' }, ...keys(r)), el('span', { class: 'help-text', text: r.help })))));
  }
  body.append(el('section', { class: 'help-group' }, el('h3', { text: 'In the palette' }),
    el('div', { class: 'help-row' }, el('span', { class: 'help-keys' }, el('kbd', { class: 'pal-kbd', text: '↑' }), ' ', el('kbd', { class: 'pal-kbd', text: '↓' })), el('span', { class: 'help-text', text: 'Move' })),
    el('div', { class: 'help-row' }, el('span', { class: 'help-keys' }, el('kbd', { class: 'pal-kbd', text: '↵' })), el('span', { class: 'help-text', text: 'Run the highlighted row' })),
    el('div', { class: 'help-row' }, el('span', { class: 'help-keys' }, el('kbd', { class: 'pal-kbd', text: 'Esc' })), el('span', { class: 'help-text', text: 'Close' }))));
  dlg.textContent = '';
  dlg.className = 'palette help';
  dlg.setAttribute('aria-label', 'Keyboard shortcuts');
  dlg.append(el('div', { class: 'pal-head help-head' }, el('h2', { class: 'help-title', text: 'Keyboard shortcuts' }),
    el('button', { class: 'icon minimal', type: 'button', 'aria-label': 'Close', title: 'Close', onclick: () => Palette.close() }, ic('cross'))), body);
  const u = { dlg, view: 'help', mode: 'default', timer: null, shown: [], nodes: [] };
  Palette.ui = u;
  Palette.show(dlg);
  try { dlg.setAttribute('tabindex', '-1'); dlg.focus(); } catch (_) { /* no focus API */ }       // the dialog itself, like openSheet: showModal would hand the focus (and a cyan ring) to the Close button
  return u;
};

/* ---------- share target ---------- */

/* The text a shared page or snippet arrives as (GET /?title=&text=&url=), or null when `search` carries no share. A share is a text or a url
   (a bare title only counts with the old share=1 marker); the parts are joined by single spaces, a part already contained in another is dropped. */
Palette.shareFromQuery = function (search) {
  let q;
  try { q = new URLSearchParams(search || ''); } catch (_) { return null; }
  const text = (q.get('text') || '').trim();
  const url = (q.get('url') || '').trim();
  const title = (q.get('title') || '').trim();
  if (!text && !url && !(q.get('share') === '1' && title)) return null;
  const parts = [];
  for (const p of [title, text, url]) {
    if (!p || parts.some((x) => x.includes(p))) continue;
    for (let i = parts.length - 1; i >= 0; i--) if (p.includes(parts[i])) parts.splice(i, 1);
    parts.push(p);
  }
  return parts.join(' ').replace(/\s+/g, ' ').trim() || null;
};
