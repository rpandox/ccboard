/* ccboard tasks page (#/tasks): the tasks kanban (columns follow the session state), rendered by renderTasks() in
   pages/home.js into a #tasks section: the page head with + task, the dispatch bar of lanes (dnd.js), then the columns, Backlog first (cards with
   Start / → s1 and Move… / Edit / Delete). This page adds the chains (connected steps with a status each, between the bar and the columns) and shows an
   empty state with + task when there are no tasks. */
'use strict';

const tasksPage = { empty: null, root: null, section: null, view: 'board', bar: null, host: null };

/* Tasks > Epics (issue #147): a Board | Epics switch above the board, drawn only when this board has a reason to look for epics (a task from a GitHub issue, a paired node, a visit
   before, or #/tasks?tab=epics itself), so a board with none keeps its Tasks page byte for byte. The list lives in pages/epic.js (the lazy 'epic' bundle) and loads on the tap. */
function tasksEpicsKnown(st, route) {
  if (route && route.query && route.query.tab === 'epics') return true;
  try { if (localStorage.getItem('ccboard:epics') === '1') return true; } catch (_) { /* storage may be unavailable */ }
  return !!st && (st.nodes_enabled === true || (Array.isArray(st.tasks) && st.tasks.some((t) => t && (t.issue_url || t.issue_number))));
}

function tasksTabs(st) {
  const T = tasksPage;
  if (!T.root) return;
  const route = typeof currentRoute === 'function' ? currentRoute() : null;
  T.view = route && route.query && route.query.tab === 'epics' ? 'epics' : 'board';
  if (!tasksEpicsKnown(st, route)) {
    if (T.bar) { T.bar.remove(); T.host.remove(); T.bar = T.host = null; }
    return;
  }
  if (!T.bar) {
    const go = (v) => { const h = v === 'epics' ? '#/tasks?tab=epics' : '#/tasks'; if (location.hash !== h) location.hash = h; };
    T.bar = el('div', { class: 'seg-ctl tk-tabs', role: 'group', 'aria-label': 'Tasks view' }, ...[['board', 'Board'], ['epics', 'Epics']].map(([v, text]) => el('button', { class: 'seg-btn', type: 'button', 'data-tab': v, text, onclick: () => go(v),
      onkeydown: (e) => { if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { e.preventDefault(); go(v === 'board' ? 'epics' : 'board'); } } })));
    T.host = el('section', { id: 'tasks-epics', class: 'hidden', 'aria-label': 'Epics' });
    T.root.insertBefore(T.bar, T.section);
    T.root.append(T.host);
  }
  const on = T.view === 'epics';
  for (const b of T.bar.querySelectorAll('.seg-btn')) b.setAttribute('aria-pressed', b.getAttribute('data-tab') === T.view ? 'true' : 'false');
  T.host.classList.toggle('hidden', !on);
  for (const n of T.root.querySelectorAll('.nd-tfilter, .nd-tasks')) n.classList.toggle('ep-hide', on);        // the paired nodes' task filter and their tasks belong to the board, not to the Epics list
  if (on) {
    T.section.classList.add('hidden');
    if (T.empty) T.empty.classList.add('hidden');
    if (typeof Lazy !== 'undefined') Lazy.run('epic', () => { if (T.view === 'epics' && T.host) Epics.paintList(T.host); }, 'the Epics list');
  } else if (typeof Epics !== 'undefined') Epics.stopList();
}

/* + task: the create sheet (Shell.openCreate('task')): the repo picker, or the form at once when the route names the place. */
function tasksCreate() { return typeof Shell !== 'undefined' && Shell && typeof Shell.openCreate === 'function' ? Shell.openCreate('task') : false; }

/* The chains of the board, drawn above its columns: connected steps with a status each (components.js taskChainStrips). renderTasks() wipes the section on
   every repaint, so this runs after it, and again after a drag that held it back. */
function tasksChainsPaint() {
  const section = $('#tasks');
  if (!section || section.classList.contains('hidden')) return;
  if (typeof Dnd !== 'undefined' && Dnd && typeof Dnd.afterDrag === 'function' && Dnd.afterDrag(tasksChainsPaint)) return;
  for (const n of section.querySelectorAll('.tk-chains')) n.remove();
  const list = boardTasks(state);
  const strips = taskChainStrips(list, taskChainInfo(list));
  if (!strips.length) return;
  const host = el('div', { class: 'tk-chains' }, el('h2', { class: 'tk-sec', text: 'Chains' }), ...strips);
  const grid = section.querySelector('.kanban');
  if (grid) section.insertBefore(host, grid); else section.append(host);
}

registerPage('tasks', {
  title: 'Tasks',
  mount(root) {
    const section = el('section', { id: 'tasks', class: 'tasks-board hidden' });   // no card around the board: the task cards are the only bordered boxes
    tasksPage.empty = pageEmpty('git-branch', 'No tasks yet', 'A task is one worktree and branch per piece of work: start one now, park it in the Backlog for later, or schedule it.');
    tasksPage.empty.append(el('button', { class: 'primary', type: 'button', text: '+ task', onclick: () => tasksCreate() }));
    tasksPage.empty.classList.add('hidden');
    root.append(section, tasksPage.empty);   // the board carries its own 'Tasks (N)' page head
    tasksPage.root = root;
    tasksPage.section = section;
  },
  update(st) {
    const section = $('#tasks');
    if (!section) return;
    renderTasks();
    const none = !boardTasks(st).length;
    section.classList.toggle('hidden', none);
    if (tasksPage.empty) tasksPage.empty.classList.toggle('hidden', !none);
    tasksChainsPaint();
    Nodes.use(() => Nodes.slot('tasks', section, {}));                         // paired nodes: the All nodes / This node / <node> filter and the read only tasks of other nodes (nodes-hub.js)
    tasksTabs(st);
  },
  onRoute(r) { if (typeof state !== 'undefined' && state) this.update(state, r); },                         // Board <-> Epics changes the address only: the same page repaints
  unmount() {
    if (typeof Epics !== 'undefined') Epics.stopList();
    Object.assign(tasksPage, { empty: null, root: null, section: null, view: 'board', bar: null, host: null });
  },
});
