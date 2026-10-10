/* ccboard create flow (the other half of shell.js): what a tap on + session, + task or Schedule does once Shell.openCreate has checked it can: the place the route
   names (routeCtx), the launcher form for a place that is already clear (createFor, launchAt), the repo picker sheet (pickRepo) and the launcher form in a sheet (showForm).
   Shell.openCreate stays in shell.js (the c-chords and every page button ask it for a yes or no at once) and runs createPlace from here through Shell.withCreate: now
   when this file is here, else after the shellcreate bundle has loaded (lazy.js). Classic script, definition-only: it adds methods to Shell and runs nothing at load. */
'use strict';

/* What Shell.openCreate does for a session, a task or a schedule once this file is here: the ctx the caller passed, else the one the route names (routeCtx), opens the launcher
   form at once when that place is clear (createFor), else the repo picker narrowed to it. */
Shell.createPlace = function (kind, ctx) {
  const pre = (ctx && typeof ctx === 'object' && ctx.project ? ctx : null) || Shell.routeCtx(kind);
  const form = kind === 'session' || kind === 'task' ? kind : 'job';                    // a schedule is the job form
  if (!(pre && Shell.createFor(form, pre))) Shell.pickRepo(form, pre);
};

/* The project (and repo) the current route is about: the project page, or the session open in the peek (its tmux name is project--repo--name).
   For a task or schedule without a repo in the route the repo is Shell.defaultRepo's (the one used last, else the first). Off a project page a TASK
   goes where the last task went (launcher.js taskLastAny: ccboard:task:last), so + task on #/tasks, the + menu and c t from Home open the form at
   once, with its "in" select to switch; null when nothing was saved, or the place is gone or no longer takes a task (then the picker asks). */
Shell.routeCtx = function (kind) {
  let r = null;
  try { r = typeof currentRoute === 'function' ? currentRoute() : null; } catch (_) { r = null; }
  let project = '';
  let repo = '';
  if (r && r.params) {
    if (r.id === 'project') { project = r.params.project || ''; repo = r.params.repo || (r.query && r.query.repo) || ''; }
    else if (r.id === 'session' && r.params.tmux) { const parts = String(r.params.tmux).split('--'); if (parts.length >= 3) { project = parts[0]; repo = parts[1]; } }
  }
  if (!project) {
    const last = kind === 'task' && typeof taskLastAny === 'function' ? taskLastAny() : null;
    const lp = last && (state.projects || []).find((x) => x.name === last.project);
    const lr = lp && (last.repo === 'root' ? lp.root : (lp.repos || []).find((x) => x.name === last.repo));
    return lr && taskTarget(lp, lr) ? { project: last.project, repo: last.repo } : null;
  }
  const p = (state.projects || []).find((x) => x.name === project);
  if (!p) return null;
  if (!repo && (kind === 'task' || kind === 'schedule' || kind === 'job')) repo = Shell.defaultRepo(p, 'task') || '';
  return repo ? { project, repo } : { project };
};

/* The launcher form for a project (and repo) that is already known: true when it opened, false when the picker has to ask. */
Shell.createFor = function (kind, ctx) {
  const p = (state.projects || []).find((x) => x.name === ctx.project);
  if (!p) return false;
  const git = kind === 'task' || kind === 'job';                                          // tasks and schedules need a git repo
  const ok = (r) => r.state === 'ok' || r.state === 'unknown';
  let r = null;
  if (ctx.repo) r = ctx.repo === 'root' ? p.root : (p.repos || []).find((x) => x.name === ctx.repo && ok(x));
  else {
    const where = git ? taskTargets(p, kind).map((x) => x.r) : [...(p.root ? [p.root] : []), ...(p.repos || []).filter(ok)];
    if (where.length === 1) r = where[0];
    else if (kind === 'session' && typeof launchPlace === 'function') { const hit = launchPlace(p, 'session'); if (hit && hit.project === p) r = hit.repo; }       // a project page without a repo: where the project worked last, the sheet's chevron goes back to the list
  }
  if (!r || (git && !(kind === 'task' ? taskTarget : gitTarget)(p, r))) return false;      // a task may run in place in a non-git project folder
  Shell.launchAt(kind, { p, r, label: Shell.targetLabel(p, r) });
  return true;
};

/* A place is chosen (the picker row, or a route that names it): a session or a task opens the launcher sheet (launch(), components.js: the project and the repo are filled in,
   the chevron goes back to the repo list); a schedule its own form. e = {p, r, label}. */
Shell.launchAt = function (kind, e) {
  if (kind === 'session' || kind === 'task') return launch({ mode: kind, project: e.p, repo: e.r, label: e.label, back: { label: 'Back to the repo list', onClick: () => Shell.pickRepo(kind, e) } });
  return Shell.showForm(kind, e);
};

Shell.PICK_TITLES = { session: 'New session', task: 'New task', job: 'Schedule a run' };

Shell.pickRepo = function (kind, ctx) {
  if (typeof state === 'undefined' || !state) return;
  const only = ctx && ctx.project ? ctx.project : null;                                  // narrowed to one project (the project page's buttons)
  const git = kind === 'task' || kind === 'job';
  const entries = [];
  const notGit = [];                                                                     // projects whose folder is no git repo: a hint under the list for tasks
  if (kind === 'session') {
    for (const p of state.projects || []) {
      if (!p.root || (only && p.name !== only)) continue;
      entries.push({ p, r: p.root, label: `${p.name}/`, sub: gitTarget(p, p.root) ? 'project folder' : 'project folder · not a git repo' });
    }
  }
  for (const p of state.projects || []) {
    if (only && p.name !== only) continue;
    const here = [];
    for (const x of allRepos()) {
      if (x.project !== p.name) continue;
      const r = p.repos.find((q) => q.name === x.repo);
      if (r) here.push({ p, r, label: x.id, sub: r.path && r.path === p.path ? 'project folder' : (r.branch || '') });
    }
    if (git) {
      const rootOk = p.root && (kind === 'task' ? taskTarget(p, p.root) : gitTarget(p, p.root));
      if (rootOk) here.push({ p, r: p.root, label: Shell.targetLabel(p, p.root), sub: rootIsGit(p.root) ? (p.root.branch || '') : 'in place (not a git repo)' });
      else if (p.root && !(p.repos || []).some((q) => q.path === p.root.path)) notGit.push(p.name);      // schedules: the folder needs git
      const last = typeof taskLastRepo === 'function' ? taskLastRepo(p.name) : '';
      const i = last ? here.findIndex((e) => e.r.name === last) : -1;
      if (i > 0) here.unshift(...here.splice(i, 1));                                     // the repo used last comes first
    }
    entries.push(...here);
  }
  const list = el('div', { class: 'pick-list' });
  const fill = (q) => {
    list.textContent = '';
    const f = q.trim().toLowerCase();
    for (const e of entries) {
      if (f && !e.label.toLowerCase().includes(f)) continue;
      list.append(el('button', { class: 'minimal pick-row', type: 'button', onclick: () => Shell.launchAt(kind, e) },
        el('span', { class: 'pr-name mono', text: e.label }), e.sub ? el('span', { class: 'dim', text: e.sub }) : null));
    }
    if (!list.childElementCount) list.append(el('div', { class: 'dim', text: 'no match' }));
  };
  const filter = entries.length > 8 ? el('input', { type: 'search', placeholder: 'filter repos…', 'aria-label': 'Filter repos', oninput: (ev) => fill(ev.target.value) }) : null;
  fill('');
  const hint = git && notGit.length ? el('p', { class: 'dim pick-hint', text: notGit.length === 1 ? `The folder of ${notGit[0]} is not a git repo: tasks need git (git init there to run tasks in it).`
    : `The folders of ${notGit.slice(0, 4).join(', ')}${notGit.length > 4 ? '…' : ''} are not git repos: tasks need git (git init there to run tasks in them).` }) : null;
  openSheet({ title: Shell.PICK_TITLES[kind], body: entries.length ? [filter, list, hint] : emptyState('folder-close', 'No repos yet', 'Create a project and add a repo first.'),
    onClose: () => { if (ui.openForm === 'sheet') ui.openForm = null; Shell.formWatch = null; } });
  focusFine(filter);                                                                    // a phone keeps its keyboard down until a field is tapped
};

/* The launcher form in the sheet. The task form takes a "where" select over the project's places (a switch re-opens it for the other repo with the
   typed text carried over) and closes the sheet itself once its call worked; the session and schedule forms close it through the next render. */
Shell.showForm = function (kind, e, carry) {
  if (typeof Lazy !== 'undefined' && !Lazy.done('launcher')) { Lazy.run('launcher', () => Shell.showForm(kind, e, carry), 'the launcher'); return undefined; }
  const form = kind === 'session' ? sessionForm(e.p, e.r)
    : kind === 'task' ? taskForm(e.p, e.r, { carry, targets: taskTargets(e.p), onTarget: (x, c) => Shell.showForm('task', { p: x.p, r: x.r, label: x.label }, c),
      onDone: () => { closeSheet(); }, onCancel: () => closeSheet() })
      : jobForm(e.p, e.r);
  const holder = el('div', { class: 'sheet-form' }, form);
  // The launcher forms end with a Cancel that re-renders the home board: inside the sheet it only closes the sheet.
  holder.addEventListener('click', (ev) => {
    const b = ev.target.closest && ev.target.closest('button');
    if (b && b.type === 'button' && b.textContent.trim() === 'Cancel') { ev.stopPropagation(); ev.preventDefault(); closeSheet(); }
  }, true);
  // The forms set ui.openForm = null and poll(true) once the launch worked: the next forced render closes the sheet.
  ui.openForm = 'sheet';
  Shell.formWatch = () => { if (ui.openForm !== 'sheet') { Shell.formWatch = null; closeSheet(); } };
  openSheet({ title: `${Shell.PICK_TITLES[kind]} · ${e.label}`, body: holder, back: { label: 'Back to the repo list', onClick: () => Shell.pickRepo(kind, e) },
    onClose: () => { if (ui.openForm === 'sheet') ui.openForm = null; Shell.formWatch = null; } });
  if (typeof form.focusFirst === 'function') form.focusFirst();          // focusFine inside: the first field on a fine pointer, nothing on touch
};
