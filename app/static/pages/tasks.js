/* ccboard tasks page (#/tasks): the tasks kanban (columns follow the session state), rendered by renderTasks() in
   pages/home.js into a #tasks section. The legacy renderer hides the section when there are no tasks; this page shows an
   empty state instead. */
'use strict';

const tasksPage = { empty: null };

registerPage('tasks', {
  title: 'Tasks',
  mount(root) {
    const section = el('section', { id: 'tasks', class: 'card hidden' });
    tasksPage.empty = pageEmpty('git-branch', 'No tasks yet', 'A task is one worktree and branch per piece of work. Create one from the + menu.');
    tasksPage.empty.classList.add('hidden');
    root.append(section, tasksPage.empty);   // the kanban card carries its own 'Tasks (N)' heading
  },
  update(st) {
    const section = $('#tasks');
    if (!section) return;
    renderTasks();
    const none = !(st.tasks || []).length;
    section.classList.toggle('hidden', none);
    if (tasksPage.empty) tasksPage.empty.classList.toggle('hidden', !none);
  },
  unmount() { tasksPage.empty = null; },
});
