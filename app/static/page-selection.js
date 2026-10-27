/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

function inboxItems() {
  if (typeof Inbox !== 'undefined' && Inbox && typeof Inbox.items === 'function') return state ? Inbox.items(state) : [];   // pages/inbox.js: the nine kinds, in their order
  const items = [];
  if (!state) return items;
  const pend = {};
  for (const pr of (state.pending_permissions || [])) pend[pr.tmux_name] = pr;
  for (const p of state.projects) {
    for (const r of repoGroups(p)) for (const s of r.sessions) if (s.needs_attention) items.push({ ...s, project: p.name, repo: r.name, perm: pend[s.tmux] || null });
    for (const s of p.orphan_sessions) if (s.needs_attention) items.push({ ...s, project: p.name, perm: pend[s.tmux] || null });
  }
  return items.sort((a, b) => (a.state_at || '').localeCompare(b.state_at || ''));  // oldest first
}

/* ---------- the keyboard selection and the actions on it (keymap.js drives these; palette.js reads them) ----------
   One selection for every list of sessions: Home (the inbox section, then the open project blocks), the inbox page and the Agents roster. The index
   stays in ui.inboxSel (a click on a card sets it too); Pages remembers the tmux name beside it so a poll that reorders the list keeps
   the same session selected, and the 'sel' class is painted on every row or card of that session. Only the list on screen answers:
   on any other page items() is empty and j / k are not handled. */
const Pages = {
  selTmux: null,            // the selected session's tmux name (the index alone would point at another session after a reorder)
  selHash: '',              // the route the selection was made on: with a peek open, a selection made before it opened does not beat the peek
  retry: null,
};

Pages.hashNow = function () { return (typeof location !== 'undefined' && location.hash) || '#/'; };

Pages.listId = function () {
  const id = typeof mountedId !== 'undefined' && mountedId ? mountedId : (typeof currentRoute === 'function' && currentRoute() ? currentRoute().id : '');
  return id === 'agents' || id === 'inbox' || id === 'home' ? id : '';
};

/* Every live session in the order the Agents page shows them (blocked first, grouped by project): mod+1..9 and the palette count in it. */
Pages.order = function () {
  if (typeof state === 'undefined' || !state) return [];
  return agentsGroups(rosterSessions(state)).flatMap((g) => g.items);
};

/* The sessions the selection can walk on this page, in screen order ([] where the page has no session list). */
Pages.items = function () {
  if (typeof state === 'undefined' || !state) return [];
  const id = Pages.listId();
  if (id === 'agents') return Pages.order();
  if (id === 'home') return homeVisibleItems();
  if (id === 'inbox') return inboxItems();
  return [];
};

Pages.active = function () { return Pages.items().length > 0; };

/* Re-find the selection in the current list: by tmux name when it is still there, else by its index (clamped), else none. */
Pages.sync = function () {
  const items = Pages.items();
  if (!items.length) { ui.inboxSel = -1; Pages.selTmux = null; return items; }
  const at = Pages.selTmux ? items.findIndex((x) => x.tmux === Pages.selTmux) : -1;
  if (at >= 0) ui.inboxSel = at;
  else if (ui.inboxSel >= 0) { ui.inboxSel = Math.min(ui.inboxSel, items.length - 1); Pages.selTmux = items[ui.inboxSel].tmux; }
  else Pages.selTmux = null;
  return items;
};

Pages.selected = function () {
  const items = Pages.items();
  return ui.inboxSel >= 0 && ui.inboxSel < items.length ? items[ui.inboxSel] : null;
};

Pages.rowNodes = function () {
  try { return Array.from(document.querySelectorAll('#page .rrow, #page .inbox-item, #page .inbox-card')); } catch (_) { return []; }
};

Pages.nodeFor = function (tmux) { return tmux ? (Pages.rowNodes().find((n) => n.getAttribute('data-tmux') === tmux) || null) : null; };

Pages.paint = function () {
  const tmux = Pages.selected() ? Pages.selTmux : null;
  for (const n of Pages.rowNodes()) n.classList.toggle('sel', !!tmux && n.getAttribute('data-tmux') === tmux);
  Pages.markLead();
};

/* The screen's one filled primary (v0.5.6d): of the cards and rows that can carry one (an inbox card always can; a row only while a permission waits on it),
   the selected one leads, else the first in screen order. node.ccLead(true) paints that card's action filled and opens its chips and send box; every other
   one is told false and shows the same action tinted. On Home the inbox section comes first, so its first card leads and the rows below keep Allow tinted;
   on the Agents page and a project's Sessions tab the first row with a pending permission leads. */
Pages.markLead = function () {
  const nodes = Pages.rowNodes().filter((n) => typeof n.ccLead === 'function');
  const can = (n) => (typeof n.ccCanLead === 'function' ? !!n.ccCanLead() : true);
  const lead = nodes.find((n) => n.classList.contains('sel') && can(n)) || nodes.find(can) || null;
  for (const n of nodes) n.ccLead(n === lead);
};

Pages.setIndex = function (i) {
  const items = Pages.items();
  if (i < 0 || i >= items.length) { ui.inboxSel = -1; Pages.selTmux = null; }
  else { ui.inboxSel = i; Pages.selTmux = items[i].tmux; Pages.selHash = Pages.hashNow(); }
  Pages.paint();
};

/* Move the selection by delta (j = 1, k = -1): none starts at the first row. False when the page has no list, so the key is left alone. */
Pages.select = function (delta) {
  const items = Pages.sync();
  if (!items.length) return false;
  Pages.setIndex(Math.max(0, Math.min(items.length - 1, (ui.inboxSel < 0 ? -1 : ui.inboxSel) + delta)));
  const node = Pages.nodeFor(Pages.selTmux);
  if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' });
  return true;
};

Pages.clear = function () {
  if (ui.inboxSel < 0 && !Pages.selTmux) return;
  ui.inboxSel = -1;
  Pages.selTmux = null;
  Pages.paint();
};

/* A page mounts with nothing selected. */
Pages.reset = function () { ui.inboxSel = -1; Pages.selTmux = null; Pages.selHash = ''; };

/* First-paint placeholder: n grey rows (core.js maps 'skeleton' to bp5-skeleton) until the first /api/state arrives; the page's update() drops it. */
Pages.skeleton = function (n) {
  const wrap = el('div', { class: 'skel-rows', 'aria-hidden': 'true' });
  for (let i = 0; i < (n || 3); i++) {
    wrap.append(el('div', { class: 'skel-row' },
      el('span', { class: 'skeleton skel-g', text: '✽ ◆' }),
      el('span', { class: 'skeleton skel-main', text: 'project/repo · session name' }),
      el('span', { class: 'skeleton skel-meta', text: 'working · model · ctx 42%' })));
  }
  return wrap;
};

Pages.dropSkeleton = function () {
  let nodes = [];
  try { nodes = Array.from(document.querySelectorAll('#page .skel-rows')); } catch (_) { return; }
  for (const n of nodes) n.remove();
};

