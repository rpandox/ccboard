/* ccboard project page (#/p/<project>[/<repo>][?tab=sessions|tasks|schedules|files|memory&path=<dir or file>]): one page per project.
   Header: name, path, one chip per repo (branch, dirty dot, state badge, cost) with its code-server link, cost today / 7 d from the state and the
   active hours of the last 7 days from GET /api/usage/summary (fetched once per mount, quiet on error), + session / + task / + schedule (the create
   sheets with the project and repo preselected), Add repo, code-server for the folder and an overflow menu that opens the repos-and-danger panel
   (Remove repo, Delete project: two taps each, through confirmButton). Tabs: Sessions (the project's rows grouped like Home), Tasks (the kanban
   columns with an empty Backlog first), Schedules (jobs with next and last run and their last three runs), Files (the lazy tree of Tree.mount, a
   repo switcher and the file preview beside it from 840 px and below it on phones), Memory (only once state.memory exists).
   A route change inside the page (tab, repo, path, even another project) goes through onRoute and never remounts it; leaving the Files tab or the
   page destroys the tree and its timers. A repo in the route (#/p/<project>/<repo>) narrows the lists to that repo (a line says so, with a link
   to all repos), picks the repo of the Files tab and is the one the + buttons preselect. Opening a file in the tree rewrites ?path= and keeps
   the route's repo as it was (none stays none: the Files tab then shows the first repo and the other tabs stay whole-project). */
'use strict';

const PROJECT_TABS = [['sessions', 'Sessions'], ['tasks', 'Tasks'], ['schedules', 'Schedules'], ['files', 'Files']];
const PROJECT_FLAGS_KEY = 'ccboard:tree:flags';                    // {hidden, ignored}: the Files tab's two toggles, shared by every repo
const projectPage = { cur: null, ids: 0 };

function pjState() { return typeof currentState === 'function' ? currentState() : null; }
function pjFind(st, name) { return (st && (st.projects || []).find((p) => p.name === name)) || null; }
function pjSig(v) { try { return JSON.stringify(v); } catch (_) { return String(Math.random()); } }

function pjHash(project, repo, query) {
  try { return buildHash('project', repo ? { project, repo } : { project }, query || {}); } catch (_) { return '#/'; }
}

function pjGo(hash, replace) {
  if (typeof navigate === 'function') navigate(hash, replace ? { replace: true } : undefined);
  else if (typeof location !== 'undefined') location.hash = hash;
}

/* What the route says: the project, the repo of the path, the tab (an unknown one, or memory before the state has it, is sessions) and ?path=. */
function pjParse(route, st) {
  const p = (route && route.params) || {};
  const q = (route && route.query) || {};
  const memory = !!(st && st.memory);
  const tab = PROJECT_TABS.some((t) => t[0] === q.tab) || (q.tab === 'memory' && memory) ? q.tab : 'sessions';
  return { project: p.project || '', repo: p.repo || q.repo || '', tab, path: typeof q.path === 'string' ? q.path : '' };
}

/* The repos the Files tab can browse: the repos (not one still cloning), then the project folder (unless the folder is the one repo). */
function pjFileRepos(p) {
  const out = [];
  for (const r of (p.repos || [])) if (r.state !== 'cloning' && r.state !== 'clone-failed') out.push({ name: r.name, label: r.name });
  const same = (p.repos || []).length === 1 && p.repos[0].name === p.name;
  if (!same) out.push({ name: 'root', label: 'project folder' });
  return out;
}

function pjFilesRepo(p, repo) {
  const list = pjFileRepos(p);
  return list.some((r) => r.name === repo) ? repo : (list[0] ? list[0].name : 'root');
}

function pjFlags() {
  try { const v = JSON.parse(localStorage.getItem(PROJECT_FLAGS_KEY) || '{}'); return { hidden: !!(v && v.hidden), ignored: !!(v && v.ignored) }; } catch (_) { return { hidden: false, ignored: false }; }
}

function pjSaveFlags(f) { try { localStorage.setItem(PROJECT_FLAGS_KEY, JSON.stringify(f)); } catch (_) { /* storage may be unavailable */ } }

function pjCodePort(st) { return !!(st && st.config && st.config.code_https_port); }

function pjCodeLink(st, path, text, cls, title) {
  if (!pjCodePort(st) || !path) return null;
  return el('a', { class: 'btn small ' + (cls || ''), href: codeServerUrl(path), target: '_blank', rel: 'noopener', title: title || path }, ic('code'), text || null);
}

/* + session / + task / + schedule: the sheet of Shell.openCreate with the project, and the repo the page is about, preselected. */
function pjCreate(kind) {
  const P = projectPage.cur;
  if (!P || typeof Shell === 'undefined' || !Shell || typeof Shell.openCreate !== 'function') return false;
  const st = pjState();
  const cur = pjParse(P.route, st);
  const repo = cur.repo || (P.view && P.view.repo) || undefined;
  return Shell.openCreate(kind, { project: cur.project, repo });
}

/* ---------- Add repo: the existing form in the sheet; its Cancel closes the sheet and a successful add closes it on the next render ---------- */

function pjAddRepo(p) {
  if (typeof addRepoForm !== 'function' || typeof openSheet !== 'function') return;
  const holder = el('div', { class: 'sheet-form' }, addRepoForm(p));
  holder.addEventListener('click', (ev) => {
    const b = ev.target && typeof ev.target.closest === 'function' ? ev.target.closest('button') : null;
    if (b && b.type === 'button' && b.textContent.trim() === 'Cancel') { ev.stopPropagation(); ev.preventDefault(); closeSheet(); }
  }, true);
  const shell = typeof Shell !== 'undefined' && Shell ? Shell : null;
  ui.openForm = 'sheet';
  if (shell) shell.formWatch = () => { if (ui.openForm !== 'sheet') { shell.formWatch = null; closeSheet(); } };
  openSheet({ title: `Add a repo to ${p.name}`, body: holder, onClose: () => { if (ui.openForm === 'sheet') ui.openForm = null; if (shell) shell.formWatch = null; } });
}

/* ---------- the header ---------- */

function pjHeader(P) {
  const name = el('h1', { class: 'pj-name' });
  const path = el('span', { class: 'pj-path mono dim' });
  const more = el('button', { class: 'icon minimal pj-more', type: 'button', 'aria-label': 'More actions', title: 'More: repos, delete project' }, ic('more'));
  menu(more, () => [
    { label: 'Repos and danger zone', icon: 'git-repo', onClick: () => { P.manage = !P.manage; pjUpdate(); } },
    { label: 'Delete project', icon: 'trash', onClick: () => { P.manage = true; ui.confirm = 'del:' + P.project; pjUpdate(); } },
  ]);
  const repos = el('div', { class: 'pj-repos', role: 'group', 'aria-label': 'Repos' });
  const stats = el('div', { class: 'pj-stats mono' });
  const actions = el('div', { class: 'pj-actions' },
    el('button', { class: 'small primary', type: 'button', text: '+ session', onclick: () => pjCreate('session') }),
    el('button', { class: 'small', type: 'button', text: '+ task', onclick: () => pjCreate('task') }),
    el('button', { class: 'small', type: 'button', text: '+ schedule', onclick: () => pjCreate('schedule') }),
    el('button', { class: 'small', type: 'button', text: 'Add repo', onclick: () => { const p = pjFind(pjState(), P.project); if (p) pjAddRepo(p); } }));
  const cs = el('span', { class: 'pj-cs-slot' });
  actions.append(cs);
  const manage = el('div', { class: 'pj-manage hidden', role: 'region', 'aria-label': 'Repos and danger zone' });
  const node = el('header', { class: 'pj-head' }, el('div', { class: 'pj-title' }, name, path, el('span', { class: 'spacer' }), more), repos, stats, actions, manage);
  return { node, name, path, repos, stats, cs, manage, sig: { repos: null, cs: null, manage: null } };
}

function pjRepoChip(st, p, r) {
  const bad = r.state && r.state !== 'ok' && r.state !== 'project';
  const cost = typeof repoCost === 'function' ? repoCost(p, r) : '';
  const chip = el('a', { class: 'pj-chip', href: pjHash(p.name, r.name, { tab: 'files' }), title: `${r.name}: browse its files` },
    ic('git-repo'), el('span', { class: 'pj-rname mono', text: r.name }),
    r.state === 'ok' && r.branch ? el('span', { class: 'pj-branch mono dim', text: r.branch }) : null,
    r.state === 'ok' ? el('span', { class: 'dot' + (r.dirty ? ' dirty' : ''), role: 'img', 'aria-label': r.dirty ? 'uncommitted changes' : 'clean', title: r.dirty ? 'uncommitted changes' : 'clean' }) : null,
    bad ? el('span', { class: 'badge warn', text: r.state }) : null,
    cost ? el('span', { class: 'pj-rcost mono dim', text: cost }) : null);
  return el('span', { class: 'pj-repo' }, chip, pjCodeLink(st, r.path, null, 'minimal pj-cs', `open ${r.name} in code-server`));
}

/* The repos and danger panel: Remove per repo and Delete project, two taps each. Open from the overflow menu, or while one of them awaits its confirmation. */
function pjManage(P, st, p) {
  const box = el('div', { class: 'pj-manage-body' }, el('div', { class: 'row head' }, el('b', { text: 'Repos and danger zone' }),
    el('button', { class: 'minimal small', type: 'button', text: 'Close', onclick: () => { P.manage = false; ui.confirm = null; pjUpdate(); } })));
  for (const r of (p.repos || [])) {
    const bad = r.state && r.state !== 'ok' && r.state !== 'project';
    box.append(el('div', { class: 'pj-mrow' }, el('span', { class: 'mono', text: r.name }), bad ? el('span', { class: 'badge warn', text: r.state }) : null,
      confirmButton('rm:' + p.name + '/' + r.name, 'Remove', () => api('DELETE', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}`), true)));
  }
  if (!(p.repos || []).length) box.append(el('div', { class: 'dim', text: 'no repos yet' }));
  box.append(el('div', { class: 'pj-mrow pj-danger' }, el('span', { class: 'dim', text: 'Deletes the project folder and every repo in it.' }),
    confirmButton('del:' + p.name, 'Delete project', async () => { await api('DELETE', `/api/projects/${encodeURIComponent(p.name)}`); navigate('#/'); })));
  return box;
}

function pjPatchHeader(P, st, p) {
  const h = P.head;
  setText(h.name, p.name);
  setText(h.path, p.path || '');
  const sig = pjSig([p.name, (p.repos || []).map((r) => [r.name, r.path, r.state, r.branch, r.dirty, typeof repoCost === 'function' ? repoCost(p, r) : '']), pjCodePort(st)]);
  if (h.sig.repos !== sig) {
    h.sig.repos = sig;
    h.repos.textContent = '';
    for (const r of (p.repos || [])) h.repos.append(pjRepoChip(st, p, r));
    if (!(p.repos || []).length) h.repos.append(el('span', { class: 'dim', text: 'no repos yet: Add repo, or start a session in the project folder' }));
    h.cs.textContent = '';
    const link = pjCodeLink(st, p.path, 'code-server', '', `open ${p.path} in code-server`);
    if (link) h.cs.append(link);
    else h.cs.append(el('span', { class: 'dim', text: 'code-server port unknown (rerun install.sh)' }));
  }
  const cost = typeof costText === 'function' ? costText(p) : '';
  const hrs = pjHours(P, p.name);
  setText(h.stats, [cost, hrs].filter(Boolean).join(' · '));
  h.stats.classList.toggle('hidden', !h.stats.textContent);
  const awaiting = typeof ui.confirm === 'string' && (ui.confirm === 'del:' + p.name || ui.confirm.startsWith('rm:' + p.name + '/'));
  const open = P.manage || awaiting;
  h.manage.classList.toggle('hidden', !open);
  const msig = pjSig([ui.confirm, (p.repos || []).map((r) => [r.name, r.state])]);
  if (h.sig.manage !== msig) {                                  // built even while it is closed: the overflow menu only reveals it
    h.sig.manage = msig;
    h.manage.textContent = '';
    h.manage.append(pjManage(P, st, p));
  }
}

/* Active hours over 7 days of this project, from the usage summary the mount fetched ('' until it arrived or when it has no number). */
function pjHours(P, name) {
  const w = P.summary && P.summary.windows && P.summary.windows['7d'];
  const row = w && Array.isArray(w.by_project) ? w.by_project.find((x) => x && x.project === name) : null;
  return row && typeof row.hours === 'number' && Number.isFinite(row.hours) ? `${row.hours.toFixed(1)} h active 7d` : '';
}

function pjFetchSummary(P) {
  const tz = typeof Widgets !== 'undefined' && Widgets && typeof Widgets.tzMin === 'function' ? Widgets.tzMin() : 345;
  Promise.resolve().then(() => api('GET', `/api/usage/summary?days=7&tz_min=${tz}`)).then((sum) => {
    if (projectPage.cur !== P) return;
    P.summary = sum && sum.windows ? sum : null;
    const p = pjFind(pjState(), P.project);
    if (p && P.head) pjPatchHeader(P, pjState(), p);
  }).catch(() => { /* quiet: the header just shows no hours */ });
}

/* ---------- the tab views: {node, update(st, route), onRoute(route), destroy(), repo?} ---------- */

function pjToolbar(...kids) { return el('div', { class: 'pj-toolbar' }, ...kids); }

/* The dim line under the tab bar when the address names a repo: the lists below are narrowed to it, and a link goes back to all repos. */
function pjScopeNote(P, route) {
  const node = el('p', { class: 'pj-scope dim hidden' });
  node.ccPatch = (c) => {
    node.classList.toggle('hidden', !c.repo);
    node.textContent = '';
    if (c.repo) node.append(`Only ${c.repo === 'root' ? 'the project folder' : c.repo} · `, el('a', { href: pjHash(c.project, '', c.tab === 'sessions' ? {} : { tab: c.tab }), text: 'all repos' }));
  };
  node.ccPatch(pjParse(route, pjState()));
  return node;
}

function pjSessionsView(P, route) {
  const scopeNote = pjScopeNote(P, route);
  const body = el('div', { class: 'pb-body' });
  const block = el('section', { class: 'pblock pb-project', 'data-block': 'sessions' }, body);
  const empty = pageEmpty('console', 'No sessions here', 'A session is a Claude, Codex or shell window in a repo of this project. Start one with + session.');
  empty.append(el('button', { class: 'primary', type: 'button', text: '+ session', onclick: () => pjCreate('session') }));
  const project = () => pjFind(pjState(), P.project);
  const groups = makeKeyedList(body, { key: (g) => 'g:' + g.key, create: (g) => homeGroupNode(g, 'project', project), patch: (n, g) => n.ccPatch(g) });
  const node = el('div', { class: 'pj-sessions' }, scopeNote, block, empty);
  return {
    node,
    update(st, rt) {
      const cur = pjParse(rt, st);
      scopeNote.ccPatch(cur);
      const hit = homeScan(st).projects.find((x) => x.p.name === cur.project);
      const list = hit ? hit.groups.map((g) => ({ ...g, items: g.items.slice().sort(sessionCompare), showHead: true }))
        .filter((g) => g.items.length && (!cur.repo || (g.repo && g.repo.name === cur.repo))) : [];
      groups.update(list);
      block.classList.toggle('hidden', !list.length);
      empty.classList.toggle('hidden', !!list.length);
      if (typeof sessionTailSweep === 'function') sessionTailSweep();
    },
    onRoute(rt) { this.update(pjState(), rt); },
    destroy() { if (typeof sessionTailStopAll === 'function') sessionTailStopAll(); },
  };
}

function pjTasksView(P, route) {
  const scopeNote = pjScopeNote(P, route);
  const grid = el('div', { class: 'kanban pj-kanban' });
  const none = el('p', { class: 'dim hidden', text: 'No tasks in this project yet. A task is one worktree and branch per piece of work: start one with + task.' });
  const bar = pjToolbar(el('button', { class: 'small primary', type: 'button', text: '+ task', onclick: () => pjCreate('task') }),
    el('span', { class: 'dim', text: 'one worktree and branch per task; the columns follow the session state' }));
  let sig = null;
  const node = el('div', { class: 'pj-tasks' }, bar, scopeNote, grid, none);
  return {
    node,
    update(st, rt) {
      const cur = pjParse(rt, st);
      scopeNote.ccPatch(cur);
      const tasks = (st.tasks || []).filter((t) => t.project === cur.project && (!cur.repo || t.repo === cur.repo));
      none.classList.toggle('hidden', !!tasks.length);
      const next = pjSig([tasks, ui.confirm]);
      if (sig === next) return;
      sig = next;
      grid.textContent = '';
      for (const [key, label] of [['backlog', 'Backlog'], ...COLUMNS]) {
        const items = tasks.filter((t) => t.column === key);
        const col = el('div', { class: 'col', 'data-col': key }, el('h3', { text: `${label} (${items.length})` }));
        if (!items.length && key === 'backlog') col.append(el('div', { class: 'dim', text: 'nothing queued' }));
        for (const t of items) col.append(taskCard(t));
        grid.append(col);
      }
    },
    onRoute(rt) { this.update(pjState(), rt); },
    destroy() { /* nothing is subscribed */ },
  };
}

function pjJobRow(j, runs) {
  const jr = runs.filter((r) => r.job_id === j.id).slice(0, 3);
  const row = el('div', { class: 'sess pj-job' + (j.enabled ? '' : ' muted'), 'data-job': j.id },
    el('div', { class: 'main' },
      agentGlyph(j.agent || 'claude'),
      el('span', { class: 'name', text: j.name }),
      el('span', { class: j.enabled && j.next_run_at ? 'state' : 'state ended', text: j.enabled && j.next_run_at ? 'next ' + fmtTs(j.next_run_at) : 'disabled' }),
      el('span', { class: 'meta', text: `${j.project}/${j.repo} · ${j.cron ? 'cron ' + j.cron : 'one-off'} · ${j.permission_mode} · ≤${j.max_turns} turns${j.max_budget_usd ? ' · ≤$' + j.max_budget_usd : ''}${j.last_status ? ' · last: ' + j.last_status + (j.last_run_at ? ' ' + fmtTs(j.last_run_at) : '') : ''}` })),
    el('div', { class: 'actions' },
      el('button', { type: 'button', onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/run`); } catch (e) { setError(e.message); } await poll(true); } }, ic('play'), 'Run now'),
      el('button', { type: 'button', onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/toggle`); } catch (e) { setError(e.message); } await poll(true); }, text: j.enabled ? 'Disable' : 'Enable' }),
      confirmButton('job:' + j.id, 'Delete', () => api('DELETE', `/api/jobs/${j.id}`))));
  for (const r of jr) {
    row.append(el('div', { class: 'last' },
      el('span', { class: 'dim', text: `run #${r.id} ${fmtTs(r.started_at)} · ${r.status}${typeof r.cost_usd === 'number' ? ' · $' + r.cost_usd.toFixed(2) : ''}${r.num_turns ? ' · ' + r.num_turns + ' turns' : ''}${r.error ? ' · ' + r.error : ''}` }),
      r.result ? el('span', { text: String(r.result).slice(0, 300) }) : null,
      r.task_id ? el('span', { class: 'row' },
        el('button', { type: 'button', onclick: async () => { try { const x = await api('POST', `/api/runs/${r.id}/resume`); openPage(x.attach_url); } catch (e) { setError(e.message); } await poll(true); }, text: 'Resume in terminal' }),
        el('span', { class: 'dim', text: `task card: ${r.branch}` })) : null));
  }
  return row;
}

function pjSchedulesView(P, route) {
  const scopeNote = pjScopeNote(P, route);
  const list = el('div', { class: 'pj-jobs' });
  const none = pageEmpty('time', 'No schedules in this project', 'A schedule runs a headless prompt on a cron, or once now, in a fresh worktree. Presets: nightly 02:30, weekdays 09:00, hourly.');
  none.append(el('button', { class: 'primary', type: 'button', text: '+ schedule', onclick: () => pjCreate('schedule') }));
  const bar = pjToolbar(el('button', { class: 'small primary', type: 'button', text: '+ schedule', onclick: () => pjCreate('schedule') }),
    el('button', { class: 'small', type: 'button', onclick: () => { if (typeof Shell !== 'undefined' && Shell && typeof Shell.openCreate === 'function') Shell.openCreate('batch'); } }, ic('layers'), 'Batch prompt'),
    el('span', { class: 'dim', text: 'headless claude -p runs · at most 2 at once · paused above 85% of the 5-hour window' }));
  let sig = null;
  const node = el('div', { class: 'pj-schedules' }, bar, scopeNote, list, none);
  return {
    node,
    update(st, rt) {
      const cur = pjParse(rt, st);
      scopeNote.ccPatch(cur);
      const jobs = (st.jobs || []).filter((j) => j.project === cur.project && (!cur.repo || j.repo === cur.repo));
      const ids = new Set(jobs.map((j) => j.id));
      const runs = (st.runs || []).filter((r) => ids.has(r.job_id));
      none.classList.toggle('hidden', !!jobs.length);
      list.classList.toggle('hidden', !jobs.length);
      const next = pjSig([jobs, runs, ui.confirm]);
      if (sig === next) return;
      sig = next;
      list.textContent = '';
      for (const j of jobs) list.append(pjJobRow(j, runs));
    },
    onRoute(rt) { this.update(pjState(), rt); },
    destroy() { /* nothing is subscribed */ },
  };
}

function pjMemoryView(P) {
  const node = el('div', { class: 'pj-memory' }, pageEmpty('database', 'Project memory', 'The memory palace of this project: observations, timeline and summaries.'),
    el('a', { class: 'btn primary', href: `#/memory/${encodeURIComponent(P.project)}`, text: 'Open memory' }));
  return { node, update() { /* static */ }, onRoute() { /* static */ }, destroy() { /* nothing */ } };
}

/* The Files tab: a repo switcher and the toggles, the tree on the left (full width on a phone), the preview beside it (below it on a phone). */
function pjFilesView(P, route) {
  const st0 = pjState();
  const p0 = pjFind(st0, P.project);
  const repo = pjFilesRepo(p0 || { repos: [] }, pjParse(route, st0).repo);
  const fl = { repo, tree: null, preview: null, shown: '', seq: 0, dead: false, flags: pjFlags(), route };
  const project = P.project;
  const treeHost = el('div', { class: 'pj-tree' });
  const hint = el('p', { class: 'pj-hint dim', text: 'Pick a file to read it. This is a read-only view: edits go through code-server.' });
  const previewHost = el('div', { class: 'pj-preview-host' });
  const closeBtn = el('button', { class: 'minimal small pj-close hidden', type: 'button', 'aria-label': 'Close the preview', title: 'Close the preview' }, ic('cross'));
  const previewBox = el('div', { class: 'pj-previewbox' }, el('div', { class: 'pj-previewbar' }, closeBtn), previewHost, hint);
  const branch = el('span', { class: 'pj-branchinfo mono dim' });
  const switcher = el('div', { class: 'pj-switch', role: 'group', 'aria-label': 'Repository' });
  const flagBtn = (key, label, title) => {
    const b = el('button', { class: 'small', type: 'button', 'aria-pressed': fl.flags[key] ? 'true' : 'false', title, text: label });
    b.addEventListener('click', () => {
      fl.flags[key] = !fl.flags[key];
      b.setAttribute('aria-pressed', fl.flags[key] ? 'true' : 'false');
      pjSaveFlags(fl.flags);
      mountTree();
    });
    return b;
  };
  const refreshBtn = el('button', { class: 'small', type: 'button', title: 'Reload the tree and the open file', onclick: () => { if (fl.tree) fl.tree.refresh(); if (fl.preview) fl.preview.reload(); } }, ic('refresh'), 'Refresh');
  const bar = pjToolbar(switcher, branch, el('span', { class: 'spacer' }), flagBtn('hidden', 'hidden', 'show dotfiles'), flagBtn('ignored', 'ignored', 'show git-ignored files'), refreshBtn);
  const node = el('div', { class: 'pj-files' }, bar, el('div', { class: 'pj-split' }, el('div', { class: 'pj-treebox' }, treeHost), previewBox));
  let swSig = null;

  const paintSwitcher = (st) => {
    const p = pjFind(st, project);
    if (!p) return;
    const sig = pjSig([pjFileRepos(p), fl.repo]);
    if (sig === swSig) return;
    swSig = sig;
    switcher.textContent = '';
    for (const r of pjFileRepos(p)) {
      switcher.append(el('button', { class: 'small pj-repo-btn', type: 'button', 'aria-pressed': r.name === fl.repo ? 'true' : 'false', text: r.label,
        onclick: () => { if (r.name !== fl.repo) pjGo(pjHash(project, r.name, { tab: 'files' })); } }));
    }
  };

  const paintBranch = (data) => {
    if (!data) { setText(branch, ''); return; }
    if (!data.git) { setText(branch, 'not a git repo'); return; }
    setText(branch, `${data.branch || 'detached'}${data.ahead ? ` ↑${data.ahead}` : ''}${data.behind ? ` ↓${data.behind}` : ''}${data.status_stale ? ' · status may be stale' : ''}`);
  };

  const clearPreview = () => {
    if (fl.preview) { fl.preview.destroy(); fl.preview = null; }
    fl.shown = '';
    hint.classList.remove('hidden');
    closeBtn.classList.add('hidden');
    previewBox.classList.remove('open');
  };

  const showFile = (path, info) => {
    if (fl.preview) fl.preview.destroy();
    fl.shown = path;
    hint.classList.add('hidden');
    closeBtn.classList.remove('hidden');
    previewBox.classList.add('open');
    fl.preview = Tree.previewFile(previewHost, { project, repo: info.repo, path: info.rel });
    if (typeof matchMedia === 'function' && !matchMedia('(min-width: 840px)').matches && typeof previewBox.scrollIntoView === 'function') {
      try { previewBox.scrollIntoView({ block: 'start' }); } catch (_) { /* no layout */ }
    }
  };

  /* The route's ?path= to the tree: open the directories down to it, select it, and preview it when it is a file. */
  const syncPath = async (path) => {
    const token = ++fl.seq;
    if (!path) { clearPreview(); return; }
    if (path === fl.shown) return;
    const hit = await fl.tree.expand(path);
    if (token !== fl.seq || fl.dead) return;
    if (!hit) { clearPreview(); hint.textContent = `${path} is not in this repo (any more).`; return; }
    fl.tree.select(path);
    if (hit.entry.type === 'file') showFile(path, { repo: hit.repo, rel: hit.rel });
    else clearPreview();
  };

  /* A click on a file in the tree: the address carries it (replace: no history entry per file), onRoute then finds it already shown. */
  const openFile = (entry, path, info) => {
    showFile(path, info);
    pjGo(pjHash(project, pjParse(fl.route, pjState()).repo, { tab: 'files', path }), true);
    if (typeof Shell !== 'undefined' && Shell && typeof Shell.openCurrent === 'function') Shell.openCurrent();    // replace-navigation fires no hashchange: the sidebar follows by hand
  };

  closeBtn.addEventListener('click', () => { clearPreview(); pjGo(pjHash(project, pjParse(fl.route, pjState()).repo, { tab: 'files' }), true); });

  function mountTree() {
    if (fl.tree) { fl.tree.destroy(); fl.tree = null; }
    treeHost.textContent = '';
    if (typeof Tree === 'undefined') { treeHost.append(el('p', { class: 'bad', text: 'tree.js did not load' })); return; }
    const sel = fl.shown;
    fl.tree = Tree.mount(treeHost, { project, repo: fl.repo, hidden: fl.flags.hidden, ignored: fl.flags.ignored, repos: true,
      storageKey: `ccboard:tree:${project}/${fl.repo}`, onOpen: openFile, onLoad: paintBranch,
      onSelect: (entry) => {                                               // a symlink is never followed: say so instead of leaving the previous file up
        if (!entry || entry.type !== 'symlink') return;
        if (fl.preview && typeof fl.preview.destroy === 'function') { try { fl.preview.destroy(); } catch (_) { /* already gone */ } }
        fl.preview = null;
        previewHost.textContent = '';
        previewHost.append(el('div', { class: 'dim pj-note', text: `${entry.name}: a symlink, not followed (open its target instead)` }));
      } });
    const want = pjParse(fl.route, pjState()).path || sel;
    fl.shown = '';
    syncPath(want);
  }

  mountTree();
  return {
    node,
    get repo() { return fl.repo; },
    update(st) { paintSwitcher(st); },
    onRoute(rt) {
      fl.route = rt;
      paintSwitcher(pjState());
      syncPath(pjParse(rt, pjState()).path);
    },
    destroy() {
      fl.dead = true;
      fl.seq += 1;
      if (fl.tree) { fl.tree.destroy(); fl.tree = null; }
      if (fl.preview) { fl.preview.destroy(); fl.preview = null; }
    },
  };
}

const PROJECT_VIEWS = { sessions: pjSessionsView, tasks: pjTasksView, schedules: pjSchedulesView, files: pjFilesView, memory: pjMemoryView };

/* ---------- the page ---------- */

function pjTabItems(P, st) {
  const p = pjFind(st, P.project);
  const items = PROJECT_TABS.map(([id, label]) => ({ id, label }));
  if (st && st.memory) items.push({ id: 'memory', label: 'Memory' });
  void p;
  return items;
}

function pjCounts(P, st, cur) {
  const tasks = (st.tasks || []).filter((t) => t.project === cur.project && (!cur.repo || t.repo === cur.repo)).length;
  const jobs = (st.jobs || []).filter((j) => j.project === cur.project && (!cur.repo || j.repo === cur.repo)).length;
  const hit = homeScan(st).projects.find((x) => x.p.name === cur.project);
  const sessions = hit ? hit.groups.filter((g) => !cur.repo || (g.repo && g.repo.name === cur.repo)).reduce((n, g) => n + g.items.length, 0) : 0;
  return { sessions, tasks, schedules: jobs };
}

function pjSetTabs(P, st, cur) {
  const items = pjTabItems(P, st);
  const sig = pjSig(items.map((i) => i.id));
  if (P.tabSig !== sig) {
    P.tabSig = sig;
    const t = tabs(items.map((i) => (i.id === 'files' || i.id === 'memory' ? i : { ...i, count: 0 })), cur.tab, (id) => {
      const c = pjParse(P.route, pjState());
      pjGo(pjHash(c.project, c.repo, id === 'sessions' ? {} : { tab: id }));
    });
    if (P.tabs) P.tabs.root.remove();
    P.tabs = t;
    P.body.insertBefore(t.root, P.panel);
  }
  P.tabs.set(cur.tab);
}

/* Build or swap the view for the route: a new tab (or the Files repo) replaces it; anything else is the view's own onRoute (a route change) or
   update (a state change). */
function pjSyncView(P, st, route, routed) {
  const cur = pjParse(route, st);
  const p = pjFind(st, cur.project);
  const viewKey = cur.tab === 'files' ? `files|${pjFilesRepo(p || { repos: [] }, cur.repo)}` : cur.tab;
  if (P.viewKey === viewKey && P.view) {
    if (routed) P.view.onRoute(route); else P.view.update(st, route);
    return;
  }
  if (P.view) { P.view.destroy(); P.view.node.remove(); P.view = null; }
  P.viewKey = viewKey;
  P.panel.textContent = '';
  const view = PROJECT_VIEWS[cur.tab](P, route);
  P.view = view;
  P.panel.append(view.node);
  view.update(st, route);
}

function pjShowKnown(P, known) {
  P.body.classList.toggle('hidden', !known);
  P.head.node.classList.toggle('hidden', !known);
  P.unknown.classList.toggle('hidden', known || !!P.loading);
  P.skel.classList.toggle('hidden', !P.loading);
}

function pjUpdate(routed) {
  const P = projectPage.cur;
  if (!P) return;
  const st = pjState();
  const route = P.route;
  const cur = pjParse(route, st);
  if (cur.project !== P.project) { P.project = cur.project; P.viewKey = null; P.tabSig = null; P.manage = false; P.head.sig = { repos: null, cs: null, manage: null }; if (P.view) { P.view.destroy(); P.view.node.remove(); P.view = null; } }
  P.loading = !st;
  const p = pjFind(st, cur.project);
  if (typeof Pages !== 'undefined' && Pages && typeof Pages.dropSkeleton === 'function' && st) Pages.dropSkeleton();
  pjShowKnown(P, !!p);
  if (!p) {
    setText(P.unknownTitle, cur.project);
    return;
  }
  pjPatchHeader(P, st, p);
  pjSetTabs(P, st, cur);
  pjSyncView(P, st, route, !!routed);
  const counts = pjCounts(P, st, cur);
  for (const id of ['sessions', 'tasks', 'schedules']) P.tabs.setCount(id, counts[id]);
}

registerPage('project', {
  title(route) {
    const c = pjParse(route, pjState());
    return `${c.project}${c.repo ? '/' + c.repo : ''}${c.tab && c.tab !== 'sessions' ? ' · ' + c.tab : ''}`;
  },
  mount(root, route) {
    const id = projectPage.ids + 1;
    projectPage.ids = id;
    const st = pjState();
    const cur = pjParse(route, st);
    const P = { id, route, project: cur.project, head: null, tabs: null, tabSig: null, panel: null, body: null, view: null, viewKey: null, summary: null, manage: false, loading: !st };
    P.head = pjHeader(P);
    P.panel = el('div', { class: 'pj-panel', role: 'tabpanel' });
    P.body = el('div', { class: 'pj-body' }, P.panel);
    P.unknownTitle = el('b');
    P.unknown = el('div', { class: 'pj-unknown hidden' }, pageEmpty('folder-close', 'Unknown project', 'No project with this name on this box: it may have been deleted.'),
      el('p', { class: 'dim' }, 'Looked for ', P.unknownTitle), el('a', { class: 'btn primary', href: '#/', text: 'Home' }));
    P.skel = typeof Pages !== 'undefined' && Pages && typeof Pages.skeleton === 'function' ? Pages.skeleton(3) : el('div');
    P.skel.classList.add('hidden');
    root.append(el('div', { class: 'proj' }, P.head.node, P.body, P.unknown, P.skel));
    projectPage.cur = P;
    if (typeof startAgeTicker === 'function') startAgeTicker();
    pjFetchSummary(P);
  },
  update(st, route) {
    const P = projectPage.cur;
    if (!P) return;
    if (route) P.route = route;
    pjUpdate(false);
  },
  onRoute(route) {
    const P = projectPage.cur;
    if (!P) return;
    P.route = route;
    pjUpdate(true);
  },
  unmount() {
    const P = projectPage.cur;
    if (!P) return;
    projectPage.cur = null;
    if (P.view) { P.view.destroy(); P.view = null; }
    if (typeof stopAgeTicker === 'function') stopAgeTicker();
  },
});
