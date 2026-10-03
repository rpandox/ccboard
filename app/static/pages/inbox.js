/* ccboard inbox page (#/inbox): every session that needs attention (waiting for you, done, failed and not acknowledged),
   oldest first, as the shared session card with its pending permission (Allow / Deny) and the nudge chips. The keys (j / k move,
   Enter open the peek, o terminal, a ack, y allow, d deny, r reply) come from keymap.js; the selection they move is Pages in
   pages/home.js (ui.inboxSel, painted as the 'sel' class on the row). */
'use strict';

const inboxPage = { refs: null, items: [] };

function inboxTitle() {
  const n = state ? inboxItems().length : 0;
  return (n ? `(${n}) ` : '') + 'Needs you';
}

registerPage('inbox', {
  title: () => inboxTitle(),
  mount(root) {
    const summary = el('p', { class: 'summary' });
    const hint = el('p', { class: 'hint', text: INBOX_HINT });
    const list = el('div', { class: 'roster inbox-list' });
    Pages.reset();
    const none = pageEmpty('inbox', 'Nothing needs you', 'Sessions that wait for you, finish or fail show up here until you acknowledge them.');
    root.append(el('div', { class: 'inbox-page' }, el('div', { class: 'page-head' }, el('h1', { text: 'Needs you' }), summary), hint, list, none));
    const select = (tmux) => {
      const i = inboxPage.items.findIndex((x) => x.tmux === tmux);
      if (i < 0) return;
      Pages.setIndex(i);
      repaintPage();
    };
    inboxPage.refs = { summary, hint, none, listNode: list, list: makeKeyedList(list, {
      key: (s) => s.tmux,
      create: (s) => {
        const n = sessionCard(s, { showProject: true, perm: true, cls: 'inbox-item' });
        n.addEventListener('click', () => select(s.tmux));
        return n;
      },
      patch: (n, s) => n.ccPatch(s),
    }) };
    startAgeTicker();
  },
  update() {
    const r = inboxPage.refs;
    if (!r) return;
    const items = Pages.sync();
    inboxPage.items = items;
    setTextIfChanged(r.summary, items.length ? `${STATE_GLYPH.waiting} ${items.length} need${items.length === 1 ? 's' : ''} you` : '');
    r.list.update(items);
    r.listNode.classList.toggle('cv-auto', items.length > CV_AUTO_ROWS);       // long lists skip the layout of rows that are off screen
    Pages.paint();
    r.none.classList.toggle('hidden', items.length > 0);
    r.hint.classList.toggle('hidden', !items.length);
    refreshTitle();
  },
  unmount() {
    inboxPage.refs = null;
    inboxPage.items = [];
    stopAgeTicker();
  },
});
