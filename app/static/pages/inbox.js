/* ccboard inbox page (#/inbox): every session that needs attention (waiting for you, done, failed and not acknowledged),
   oldest first, as the shared session card with its pending permission (Allow / Deny) and the nudge chips. The legacy keys
   (j / k move, Enter attach, a ack, y / n allow / deny) still work: inboxKey() in pages/home.js drives ui.inboxSel and asks
   the router to repaint this page. */
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
    const hint = el('p', { class: 'hint', text: 'j / k move · Enter attach · a ack · y / n allow / deny' });
    const list = el('div', { class: 'roster inbox-list' });
    const none = pageEmpty('inbox', 'Nothing needs you', 'Sessions that wait for you, finish or fail show up here until you acknowledge them.');
    root.append(el('div', { class: 'inbox-page' }, el('div', { class: 'page-head' }, el('h1', { text: 'Needs you' }), summary), hint, list, none));
    const select = (tmux) => {
      const i = inboxPage.items.findIndex((x) => x.tmux === tmux);
      if (i < 0) return;
      ui.inboxSel = i;
      repaintPage();
    };
    inboxPage.refs = { summary, hint, none, list: makeKeyedList(list, {
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
    const items = inboxItems();
    inboxPage.items = items;
    if (ui.inboxSel >= items.length) ui.inboxSel = items.length - 1;
    setTextIfChanged(r.summary, items.length ? `${STATE_GLYPH.waiting} ${items.length} need${items.length === 1 ? 's' : ''} you` : '');
    r.list.update(items);
    items.forEach((s, i) => { const n = r.list.nodes.get(s.tmux); if (n) n.classList.toggle('sel', i === ui.inboxSel); });
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
