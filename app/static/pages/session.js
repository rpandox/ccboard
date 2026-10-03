/* ccboard session peek (#/s/<tmux>): one session at a glance with its state, age, model and context, last prompt and message, a
   pending permission (Allow / Deny), Open terminal / Ack / Kill, the nudge chips and a box that sends text to the session.
   Where it shows is decided on mount (and again on every update, so a resize switches it): the dock (#dock) from 1024 px up,
   the sheet (dialog#sheet through openSheet) below, and #page itself when neither exists. It is an ordinary routed page (the
   router mounts it into #page, unmounts it on the next route); #/s/<a> to #/s/<b> swaps the panel through onRoute. The card
   is the shared sessionCard from pages/agents.js. Closing goes back in history when the app itself opened the peek, else home. */
'use strict';

const sessionPeek = { tmux: null, surface: null, parts: null, pageRoot: null, token: 0 };

function peekLabel(tmux) {
  const p = String(tmux).split('--');
  return p.length === 3 ? `${p[0]}/${p[1] === 'root' ? 'project folder' : p[1]} · ${p[2]}` : String(tmux);
}

function peekSurface() {
  let wide = false;
  try { wide = !!(window.matchMedia && window.matchMedia('(min-width: 1024px)').matches); } catch (_) { /* no matchMedia */ }
  if (wide && $('#dock')) return 'dock';
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
    if (typeof composerGrow === 'function') composerGrow(input);
    pageToast('sent', 'ok');
  } catch (e) { pageToast(e.message, 'bad'); }
}

function peekBuild(tmux) {
  const title = el('span', { class: 'peek-title' });
  const head = el('div', { class: 'peek-head' }, title,
    el('button', { class: 'minimal small', type: 'button', 'aria-label': 'Close', title: 'Close', onclick: peekClose }, ic('cross')));
  const host = el('div', { class: 'peek-host' });
  const gone = el('div', { class: 'dim hidden', text: 'This session is not running any more.' });
  const input = composer({ placeholder: 'send · ⇧Enter new line', label: 'send', onSend: () => peekSend(input) });   // Enter sends, Shift+Enter newline
  const form = el('form', { class: 'peek-send', onsubmit: (e) => { e.preventDefault(); peekSend(input); } },
    input, el('button', { class: 'primary', type: 'submit', text: 'Send' }));
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
  if (s) {
    if (!p.card) { p.card = sessionCard(s, { peek: true, perm: true, showProject: true, link: false }); p.host.append(p.card); }
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
    sessionPeek.pageRoot = null;
    stopAgeTicker();
  },
});
