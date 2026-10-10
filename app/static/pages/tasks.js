/* ccboard tasks page (#/tasks): the tasks kanban (columns follow the session state), rendered by renderTasks() in
   pages/home.js into a #tasks section: the page head with + task, the dispatch bar of lanes (dnd.js), then the columns, Backlog first (cards with
   Start / → s1 and Move… / Edit / Delete). This page adds the chains (connected steps with a status each, between the bar and the columns) and shows an
   empty state with + task when there are no tasks. */
'use strict';

const tasksPage = { empty: null };

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
  },
  unmount() { tasksPage.empty = null; },
});
