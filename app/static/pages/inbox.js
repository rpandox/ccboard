/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

/* ---------- #/inbox ---------- */

const inboxPage = { refs: null, items: [], st: null };

function inboxTitle() {
  const n = typeof state !== 'undefined' && state ? Inbox.items(state).length : 0;
  return (n ? `(${n}) ` : '') + 'Needs you';
}

function inboxSummary(items) {
  if (!items.length) return '';
  const counts = Inbox.counts(items);
  const parts = [`${STATE_GLYPH.waiting} ${items.length} need${items.length === 1 ? 's' : ''} you`];
  for (const k of Inbox.ORDER) if (counts[k]) parts.push(`${counts[k]} ${Inbox.LABEL[k]}`);
  return parts.join(' · ');
}

registerPage('inbox', {
  title: () => inboxTitle(),
  mount(root) {
    const summary = el('p', { class: 'summary' });
    const hint = el('p', { class: 'hint', text: Inbox.HINT });
    const list = el('div', { class: 'roster inbox-list' });
    Pages.reset();
    // the next step depends on the board: no sessions at all -> New session (and New project when there is no project to start it in); sessions running -> See all agents
    const acts = { session: pageCreateButton('session', 'New session', true), project: pageCreateButton('project', 'New project', false),
      agents: el('a', { class: 'btn', href: '#/agents', text: 'See all agents' }) };
    const none = pageEmpty('inbox', 'Nothing needs you', 'Sessions that wait for you, finish or fail show up here until you acknowledge them.', [acts.session, acts.project, acts.agents]);
    root.append(el('div', { class: 'inbox-page' }, el('div', { class: 'page-head' }, el('h1', { text: 'Needs you' }), summary), hint, list, none));
    // a click on a card selects it (the keyboard's selection walks Pages.items(), so look the session up by name, not by position)
    const select = (tmux) => {
      const i = Pages.items().findIndex((x) => x.tmux === tmux);
      if (i < 0) return;
      Pages.setIndex(i);
      repaintPage();
    };
    inboxPage.refs = { summary, hint, none, acts, listNode: list, list: makeKeyedList(list, {
      key: (s) => s.tmux,
      create: (s) => {
        const n = inboxCard(s, inboxPage.st, {});
        n.addEventListener('click', () => select(s.tmux));
        return n;
      },
      patch: (n, s) => n.ccPatch(s, inboxPage.st),
    }) };
    startAgeTicker();
  },
  update(st) {
    const r = inboxPage.refs;
    if (!r) return;
    const items = Inbox.items(st);
    inboxPage.st = st;
    inboxPage.items = items;
    Pages.sync();
    setTextIfChanged(r.summary, inboxSummary(items));
    r.list.update(items);
    r.listNode.classList.toggle('cv-auto', items.length > CV_AUTO_ROWS);       // long lists skip the layout of rows that are off screen
    Pages.paint();
    r.none.classList.toggle('hidden', items.length > 0);
    const live = rosterSessions(st).length > 0;
    r.acts.session.classList.toggle('hidden', live);
    r.acts.project.classList.toggle('hidden', live || (st.projects || []).length > 0);
    r.acts.agents.classList.toggle('hidden', !live);
    r.hint.classList.toggle('hidden', !items.length);
    Nodes.use(() => Nodes.slot('inbox', r.listNode, { none: r.none, local: items.length }));     // paired nodes: "On other nodes" under the local cards (nodes-hub.js)
    refreshTitle();
  },
  unmount() {
    inboxPage.refs = null;
    inboxPage.items = [];
    inboxPage.st = null;
    stopAgeTicker();
  },
});
