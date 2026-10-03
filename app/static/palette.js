/* ccboard command palette and shortcut help (v0.5.3b): one modal <dialog id="helpdlg"> (class 'palette') that holds either the palette or the list of
   shortcuts. Definition only at load; keymap.js opens it (mod+k, ?), main.js opens it for a shared page or snippet. Classic script, one namespace (Palette).

   Palette.open({ mode?: 'default' | 'send', text? }) draws a search box and grouped results, filtered by fuzzy match as you type:
     Sessions   every live session in the Agents order, state glyph, project/repo; Enter opens its peek (mod+1..9 jump straight there)
     Routes     Home, Needs you, Agents, Tasks, Usage, Memory, Settings, Search (and "search transcripts for <what you typed>")
     Nudges     for the selected or peeked session: continue, merge, push, pr, add commit push, do it (typed into it with Enter)
     Controls   for the same session: /compact /context /cost /usage /status, typed with Enter and confirmed by a toast
                (/clear, /effort and /model wait for the guarded command endpoint of v0.5.7)
     Modes      ultracode / plan, inserted into the peek's send box
     More       collapsed; the fun commands (/color /copy /rewind /radio /stickers /tui /passes) as plain inserts into that box
   mode 'send' is the share target: the box holds the shared text (editable) and the list is "send to session"; Enter sends it with Enter,
   Shift+Enter only types it. Closing the dialog in that mode strips the shared params from the address (Router.stripShare).
   Palette.openHelp() draws Keymap.help() grouped. Everything here is built with el() and textContent. */
'use strict';

const Palette = {
  ui: null,                 // { dlg, view: 'palette' | 'help', mode, input, list, foot, ctx, shown, nodes, sel, selId, moreOpen, timer, sig } while open
  ROUTES: [['Home', '#/', 'g h'], ['Needs you', '#/inbox', 'g i'], ['Agents', '#/agents', 'g a'], ['Tasks', '#/tasks', 'g t'],
    ['Usage', '#/usage', 'g u'], ['Memory', '#/memory', 'g m'], ['Settings', '#/settings', 'g s'], ['Search', '#/search', '/']],
  NUDGES: ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'],            // the same six as the session cards (SESSION_NUDGES in pages/agents.js)
  CONTROLS: ['/compact', '/context', '/cost', '/usage', '/status'],
  MODES: ['ultracode ', 'plan '],
  MORE: ['/color', '/copy', '/rewind', '/radio', '/stickers', '/tui', '/passes'],
  NUDGE_STATES: ['waiting', 'idle', 'done', 'working', 'errored'],
  SESSIONS_SHOWN: 9,        // the Sessions group of an empty search: the nine mod+1..9 reach
  PER_GROUP: 12,            // most rows of one group while searching
  TICK_MS: 1500,
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

Palette.sessionHash = function (tmux) { return typeof sessionHash === 'function' ? sessionHash(tmux) : '#/s/' + tmux; };

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
  return { sessions, target: t ? (sessions.find((x) => x.tmux === t) || null) : null, targetTmux: t || null, box: P && typeof P.sendBox === 'function' ? P.sendBox() : null };
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

/* Put text into the peek's send box: modes go in front, a command goes in at the caret (or alone in an empty box). With no peek open the
   selected session's peek is opened first; with neither there is no box to fill. */
Palette.insert = function (text, how) {
  const P = Palette.pages();
  const fill = (box) => {
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
  if (!P || typeof P.withSendBox !== 'function') return false;
  if (!P.sendBox()) {
    const t = P.target();
    if (!t) { Palette.say('Open a session first: the text goes into its send box.', 'warn'); return false; }
    if (P.peekTmux() !== t) Palette.go(Palette.sessionHash(t));
  }
  return P.withSendBox(fill);
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
    return { id: 's:' + s.tmux, label: s.name || s.tmux, hint: `${Palette.where(s)} · ${GLYPH_LABEL[st]}`, kbd: i < 9 ? Palette.modLabel() + (i + 1) : '', glyph,
      keywords: `${s.project || ''} ${s.repo || ''} ${s.tmux}`, run: () => { close(); Palette.go(Palette.sessionHash(s.tmux)); } };
  });
  groups.push({ id: 'sessions', title: 'Sessions', items: sessions, limit: query ? Palette.PER_GROUP : Palette.SESSIONS_SHOWN });

  // With nothing typed, Search is a plain route. With a query it moves to a group of its own at the very end (never filtered out, never ranked above
  // a real match): Enter on the best match then runs that match, and "search transcripts for ..." is the last row.
  const routes = Palette.ROUTES.filter((r) => r[1] !== '#/search' || !query).map(([label, hash, kbd]) => ({ id: 'r:' + hash, label, hint: hash === '#/search' ? 'transcripts' : '', kbd, run: () => { close(); Palette.go(hash); } }));
  groups.push({ id: 'routes', title: 'Routes', items: routes });

  const t = ctx.target;
  if (t && Palette.nudgeable(t)) {
    const name = t.name || t.tmux;
    const nudges = typeof SESSION_NUDGES !== 'undefined' ? SESSION_NUDGES : Palette.NUDGES;
    groups.push({ id: 'nudges', title: `Nudge · ${name}`, items: nudges.map((text) => ({ id: 'n:' + text, label: text, hint: 'typed into ' + name, run: () => { close(); Palette.send(t.tmux, text, name); } })) });
    groups.push({ id: 'controls', title: `Controls · ${name}`, items: Palette.CONTROLS.map((text) => ({ id: 'c:' + text, label: text, hint: 'typed into ' + name, run: () => { close(); Palette.send(t.tmux, text, name); } })) });
  }

  if (ctx.box || ctx.targetTmux) {
    groups.push({ id: 'modes', title: 'Modes', items: Palette.MODES.map((text) => ({ id: 'm:' + text, label: text.trim(), hint: 'insert into the send box', run: () => { close(); Palette.insert(text, 'prefix'); } })) });
    groups.push({ id: 'more', title: 'More', collapsed: true, items: Palette.MORE.map((text) => ({ id: 'x:' + text, label: text, hint: 'insert into the send box', run: () => { close(); Palette.insert(text, 'command'); } })) });
  }
  if (query) {
    groups.push({ id: 'search', title: 'Search', items: [{ id: 'r:search', label: `Search transcripts for "${query}"`, hint: '', kbd: '', always: true,
      run: () => { close(); Palette.go('#/search?q=' + encodeURIComponent(query)); } }] });
  }
  return groups;
};

/* The shown groups for `query`: fuzzy-filtered and ranked, empty groups dropped, More collapsed to one row until opened or searched. */
Palette.groups = function (ctx, query, moreOpen) {
  const q = String(query || '').trim();
  const out = [];
  for (const g of Palette.catalog(ctx, q)) {
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
    if (items.length) out.push({ id: g.id, title: g.title, items, best });
  }
  if (q) out.sort((a, b) => b.best - a.best);       // searching: the group that holds the best match leads, so Enter runs the best match
  return out;
};

/* send mode: the sessions that can take text, the peeked or selected one first */
Palette.sendGroups = function (ctx) {
  const list = ctx.sessions.filter((s) => Palette.nudgeable(s));
  list.sort((a, b) => (b.tmux === ctx.targetTmux ? 1 : 0) - (a.tmux === ctx.targetTmux ? 1 : 0));
  const items = list.map((s) => {
    const st = Palette.stateOf(s);
    const glyph = stateGlyph(st);
    glyph.setAttribute('aria-hidden', 'true');
    glyph.removeAttribute('title');
    return { id: 'send:' + s.tmux, label: s.name || s.tmux, hint: `${Palette.where(s)} · ${GLYPH_LABEL[st]}`, glyph, session: s, run: (e) => Palette.sendShared(s, e) };
  });
  return items.length ? [{ id: 'send', title: 'Send to session', items }] : [];
};

Palette.sendShared = function (s, e) {
  const u = Palette.ui;
  const text = u && u.input ? u.input.value.trim() : '';
  if (!text) { Palette.say('Nothing to send.', 'warn'); return; }
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
  const u = { dlg, view: 'palette', mode, input, list, foot, ctx: Palette.context(), shown: [], nodes: [], sel: 0, selId: null, moreOpen: false, timer: null, sig: typeof ui !== 'undefined' ? ui.lastJson : null };
  Palette.ui = u;
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

Palette.itemNode = function (it, id) {
  return el('div', { class: 'pal-item', role: 'option', id, 'aria-selected': 'false' },
    it.glyph || null,
    el('span', { class: 'pal-label' }, ...Palette.highlight(it.label, it.hits)),
    it.hint ? el('span', { class: 'pal-hint', text: it.hint }) : null,
    it.kbd ? el('kbd', { class: 'pal-kbd', text: it.kbd }) : null);
};

Palette.footText = function (mode) {
  return mode === 'send' ? '↑↓ choose a session · ↵ send · ⇧↵ type without Enter · esc cancel' : `↑↓ move · ↵ run · esc close · ${Palette.modLabel()}K toggles`;
};

Palette.render = function () {
  const u = Palette.ui;
  if (!u || u.view !== 'palette') return;
  const q = u.mode === 'send' ? '' : u.input.value.trim();
  const groups = u.mode === 'send' ? Palette.sendGroups(u.ctx) : Palette.groups(u.ctx, q, u.moreOpen);
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
