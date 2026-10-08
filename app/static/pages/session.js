/* ccboard session peek (#/s/<tmux>): one session at a glance with its state, age, model and context, last prompt and message, a
   pending permission (Allow / Deny), Open terminal / Ack / Kill, the nudge chips and a box that sends text to the session.
   Where it shows is decided on mount (and again on every update, so a resize switches it): the dock (#dock) from 1024 px up unless the terminal
   dock is on (v0.5.9, shell.js Shell.dockOn: that dock shows the live terminal and the peek moves to the sheet), the sheet (dialog#sheet through
   openSheet) below, and #page itself when neither exists. It is an ordinary routed page (the
   router mounts it into #page, unmounts it on the next route); #/s/<a> to #/s/<b> swaps the panel through onRoute. The card
   is the shared sessionCard from pages/agents.js. Closing goes back in history when the app itself opened the peek, else home. */
'use strict';

const sessionPeek = { tmux: null, surface: null, parts: null, pageRoot: null, token: 0, row: null };

function peekLabel(tmux) {
  const p = String(tmux).split('--');
  return p.length === 3 ? `${p[0]}/${p[1] === 'root' ? 'project folder' : p[1]} · ${p[2]}` : String(tmux);
}

function peekSurface() {
  let wide = false;
  try { wide = !!(window.matchMedia && window.matchMedia('(min-width: 1024px)').matches); } catch (_) { /* no matchMedia */ }
  const termDock = typeof Shell !== 'undefined' && Shell && typeof Shell.dockOn === 'function' && Shell.dockOn();       // the terminal dock (shell.js) owns #dock: the peek is a sheet then
  if (wide && $('#dock') && !termDock) return 'dock';
  const dlg = $('#sheet');
  if (typeof openSheet === 'function' && dlg && typeof dlg.showModal === 'function') return 'sheet';
  return 'page';
}

function peekClose() { goBack('#/'); }

async function peekSend(input) {
  const text = String(input.value || '').replace(/\r\n?/g, '\n');
  const tmux = sessionPeek.tmux;
  if (!text.trim() || !tmux) return;
  try {
    await api('POST', `/api/sessions/${encodeURIComponent(tmux)}/keys`, { text, enter: true });
    input.value = '';
    if (typeof rowCleared === 'function') rowCleared(input);
    else if (typeof composerGrow === 'function') composerGrow(input);
    pageToast('sent', 'ok');
  } catch (e) { pageToast(e.message, 'bad'); }
}

/* The nudge chips of the peek's card come from the same list as the terminal page's quick replies (components.js quickLoad, localStorage
   ccboard:quick:<tmux>; the agent defaults until edited). A hold on a chip or the pencil chip opens the <dialog> editor (quickReplyEditor).
   The chips div itself stays the card's own: agents.js ccPatch toggles its `hidden` class with the session's state. */
function peekChips(card, tmux) {
  const chips = card && typeof card.querySelector === 'function' ? card.querySelector('.chips') : null;
  if (!chips || typeof quickLoad !== 'function') return;
  const target = () => sessionPeek.row || { tmux, name: typeof sessionNameOf === 'function' ? sessionNameOf(tmux) : tmux };
  const ag = () => sessionAgent(target());                            // #69: the session's agent picks the default list
  const edit = () => quickReplyEditor({ items: quickLoad(tmux, ag()), defaults: quickDefaults(ag()), onSave: (items) => { quickSave(tmux, items, ag()); fill(); } });
  function fill() {
    chips.textContent = '';
    for (const text of quickLoad(tmux, ag())) chips.append(quickChip(text, { cls: 'chip-btn', onSend: (b) => sessionNudge(target(), text, b), onEdit: edit }));
    chips.append(el('button', { class: 'icon minimal qr-edit', type: 'button', title: 'Edit quick replies', 'aria-label': 'Edit quick replies', onclick: edit }, ic('edit')));
  }
  fill();
}

function peekBuild(tmux) {
  const title = el('span', { class: 'peek-title' });
  const head = el('div', { class: 'peek-head' }, title,
    el('button', { class: 'minimal small', type: 'button', 'aria-label': 'Close', title: 'Close', onclick: peekClose }, ic('cross')));
  const host = el('div', { class: 'peek-host' });
  const gone = el('div', { class: 'dim hidden', text: 'This session is not running any more.' });
  const hint = 'Enter sends, Shift+Enter adds a line';
  const input = composer({ placeholder: typeof sessionPlaceholder === 'function' ? sessionPlaceholder('send', '') : 'send · ⇧Enter new line', label: 'send', onSend: () => peekSend(input) });   // Enter sends, Shift+Enter newline
  input.setAttribute('title', `send to the session: ${hint}`);
  const form = el('form', { class: 'peek-send', onsubmit: (e) => { e.preventDefault(); peekSend(input); } },
    input, el('button', { class: 'primary small', type: 'submit', text: 'Send' }));      // tinted until the box has text (style.css, .has-text on the form)
  const root = el('div', { class: 'peek', 'data-tmux': tmux }, head, host, gone, form);
  return { root, head, title, host, gone, input, card: null };
}

function peekClearSurface(surface) {
  if (surface === 'dock') {
    const dock = $('#dock');
    if (dock) { dock.textContent = ''; dock.classList.add('hidden'); }
  } else if (surface === 'sheet') {
    if (typeof closeSheet === 'function') closeSheet();
  }                                                                   // 'page': the router clears #page
}

/* Build the panel for `tmux` and put it on its surface, replacing whatever the peek showed before. */
function peekShow(tmux) {
  sessionPeek.token += 1;                                             // a close event of the previous panel is stale from here on
  const token = sessionPeek.token;
  const prev = sessionPeek.surface;
  const surface = peekSurface();
  const parts = peekBuild(tmux);
  const label = peekLabel(tmux);
  sessionPeek.tmux = tmux;
  sessionPeek.parts = parts;
  sessionPeek.surface = surface;
  parts.head.classList.toggle('hidden', surface === 'sheet');         // the sheet has its own title and close button
  parts.title.textContent = label;
  if (prev && prev !== surface) peekClearSurface(prev);
  if (surface === 'dock') {
    const dock = $('#dock');
    dock.textContent = '';
    dock.append(parts.root);
    dock.classList.remove('hidden');
  } else if (surface === 'sheet') {
    openSheet({ title: label, placement: 'bottom', body: parts.root, onClose: () => { if (token === sessionPeek.token) { sessionPeek.token += 1; goBack('#/'); } } });
  } else {
    sessionPeek.pageRoot.textContent = '';
    sessionPeek.pageRoot.append(parts.root);
  }
}

function peekUpdate(st) {
  const p = sessionPeek.parts;
  if (!p || !st) return;
  if (peekSurface() !== sessionPeek.surface) { peekShow(sessionPeek.tmux); return peekUpdate(st); }   // the window crossed 1024 px
  const s = rosterSessions(st).find((x) => x.tmux === sessionPeek.tmux);
  sessionPeek.row = s || null;
  if (s) {
    if (!p.card) { p.card = sessionCard(s, { peek: true, perm: true, showProject: true, link: false }); p.host.append(p.card); peekChips(p.card, sessionPeek.tmux); }
    else p.card.ccPatch(s);
  } else if (p.card) { p.card.remove(); p.card = null; }
  p.gone.classList.toggle('hidden', !!s);
}

registerPage('session', {
  overlay: true,                                                      // keeps the page it opened from mounted; lives in #dock or the sheet
  title: (r) => peekLabel((r && r.params && r.params.tmux) || ''),
  noFocus: true,                                                      // the sheet focuses itself; the page must not pull focus away
  mount(root, route) {
    sessionPeek.pageRoot = root;
    peekShow(route.params.tmux);
    startAgeTicker();
  },
  update(st) { peekUpdate(st); },
  onRoute(route) {
    if (!sessionPeek.parts) return;
    peekShow(route.params.tmux);
    peekUpdate(typeof state !== 'undefined' ? state : null);
  },
  unmount() {
    sessionPeek.token += 1;
    peekClearSurface(sessionPeek.surface);
    sessionPeek.parts = null;
    sessionPeek.surface = null;
    sessionPeek.tmux = null;
    sessionPeek.row = null;
    sessionPeek.pageRoot = null;
    stopAgeTicker();
  },
});
