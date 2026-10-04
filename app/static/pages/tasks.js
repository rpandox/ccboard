/* ccboard tasks page (#/tasks): the tasks kanban (columns follow the session state), rendered by renderTasks() in
   pages/home.js into a #tasks section: Backlog first (cards with Start / Send to session / Edit / Delete), a + task button in its head.
   The legacy renderer hides the section when there are no tasks; this page shows an empty state with + task instead. */
'use strict';

const tasksPage = { empty: null };

/* + task: the create sheet (Shell.openCreate('task')): the repo picker, or the form at once when the route names the place. */
function tasksCreate() { return typeof Shell !== 'undefined' && Shell && typeof Shell.openCreate === 'function' ? Shell.openCreate('task') : false; }

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
  },
  unmount() { tasksPage.empty = null; },
});
