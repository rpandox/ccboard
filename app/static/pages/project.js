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

/* The project's own quad: #/quad?p=<project> (the header's Quad button). */
function pjQuadHash(project) {
  if (!/^[A-Za-z0-9_-]+$/.test(String(project || ''))) return '#/quad';
  try { return buildHash('quad', {}, { p: project }); } catch (_) { return '#/quad'; }
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

/* + session / + task / + schedule. A session or a task opens the launcher sheet (launch(), components.js) with this project and the repo the page is about filled in,
   so + session is one tap to Start & open; a schedule opens its form through Shell.openCreate. The repo is the route's, else the one the Files tab shows. A task or a
   schedule never stops at the picker: that repo, else the repo a task was last started in, else the first repo, else the project folder when it is a git repo (the form's
   "in" select switches). A session without a repo goes where the project last worked (launchPlace). */
function pjCreate(kind) {
  const P = projectPage.cur;
  if (!P) return false;
  const st = pjState();
  const cur = pjParse(P.route, st);
  const p = pjFind(st, cur.project);
  let repo = cur.repo || (P.view && P.view.repo) || undefined;
  if (kind === 'task' || kind === 'schedule' || kind === 'job') {
    const can = kind === 'task' ? taskTarget : gitTarget;                                           // a task may run in place in a non-git project folder
    const ok = (name) => !!p && (name === 'root' ? !!p.root && can(p, p.root) : (p.repos || []).some((r) => r.name === name && can(p, r)));
    if (p && !(repo && ok(repo))) repo = typeof Shell !== 'undefined' && Shell && typeof Shell.defaultRepo === 'function' ? Shell.defaultRepo(p, kind) : ((p.repos || []).find((r) => gitTarget(p, r)) || {}).name;
  }
  if ((kind === 'session' || kind === 'task') && p) {
    const r = repo ? (repo === 'root' ? p.root : (p.repos || []).find((x) => x.name === repo)) : null;
    const fits = !!r && (kind === 'task' ? taskTarget(p, r) : (r === p.root || r.state === 'ok' || r.state === 'unknown'));
    const place = fits ? r : (launchPlace(p, kind) || {}).repo;
    if (place) return launch({ mode: kind, project: p, repo: place });
  }
  if (typeof Shell === 'undefined' || !Shell || typeof Shell.openCreate !== 'function') return false;
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

/* The `...` menu: the things a project does rarely, so the header keeps three buttons. Add repo, the folder in code-server, the repos-and-danger panel. */
function pjMoreItems(P) {
  const st = pjState();
  const p = pjFind(st, P.project);
  const items = [];
  if (p) items.push({ label: 'Add repo', icon: 'plus', onClick: () => pjAddRepo(p) });
  if (p && p.path && pjCodePort(st)) items.push({ label: 'Open the folder in code-server', icon: 'code', onClick: () => openPage(codeServerUrl(p.path)) });
  items.push({ label: 'Repos and danger zone', icon: 'git-repo', onClick: () => { P.manage = !P.manage; pjUpdate(); } });
  items.push({ label: 'Delete project', icon: 'trash', onClick: () => { P.manage = true; ui.confirm = 'del:' + P.project; pjUpdate(); } });
  return items;
}

function pjHeader(P) {
  const name = el('h1', { class: 'pj-name' });
  const path = el('span', { class: 'pj-path mono dim' });
  const more = el('button', { class: 'icon minimal pj-more', type: 'button', 'aria-label': 'More actions', title: 'More: add repo, code-server, repos, delete project' }, ic('more'));
  menu(more, () => pjMoreItems(P));
  // the project's live sessions side by side (the quad scoped to it): a quiet bordered link beside the ... menu, on every width and on every tab
  const quad = el('a', { class: 'btn minimal small pj-quad', href: '#/quad', title: "This project's live sessions side by side (the quad)" }, ic('layout-grid'), 'Quad');
  const repos = el('div', { class: 'pj-repos', role: 'group', 'aria-label': 'Repos' });
  const stats = el('div', { class: 'pj-stats mono' });
  const actions = el('div', { class: 'pj-actions' });                 // pjPatchActions fills it: the filled primary follows the tab
  const manage = el('div', { class: 'pj-manage hidden', role: 'region', 'aria-label': 'Repos and danger zone' });
  const gotchas = el('div', { class: 'pj-gotchas hidden' });           // the newest claude-mem gotchas (pjGotchas fills it; it stays hidden unless there are some)
  const node = el('header', { class: 'pj-head' }, el('div', { class: 'pj-title' }, name, path, el('span', { class: 'spacer' }), quad, more), repos, stats, gotchas, actions, manage);
  return { node, name, path, quad, repos, stats, gotchas, actions, manage, sig: { repos: null, tab: null, manage: null } };
}

/* + session / + task / + schedule, built per tab (v0.5.6d): the Sessions tab's primary is + session; on Tasks and Schedules the tab's own button is the one
   primary and the header keeps the other two quiet (the tab's own is not repeated here). A different primary is a different button, so the three are rebuilt
   when the tab changes. */
function pjPatchActions(P, tab) {
  const h = P.head;
  if (h.sig.tab === tab) return;
  h.sig.tab = tab;
  h.actions.textContent = '';
  // a raw append(null) would draw the text "null": the buttons are collected first
  const btns = [el('button', { class: (tab === 'sessions' ? 'primary ' : '') + 'small', type: 'button', text: '+ session', onclick: () => pjCreate('session') })];
  if (tab !== 'tasks') btns.push(el('button', { class: 'small', type: 'button', text: '+ task', onclick: () => pjCreate('task') }));
  if (tab !== 'schedules') btns.push(el('button', { class: 'small', type: 'button', text: '+ schedule', onclick: () => pjCreate('schedule') }));
  h.actions.append(...btns);
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
  box.append(el('div', { class: 'pj-mrow' }, el('span', { class: 'dim', text: 'Project folder' }),
    pjCodeLink(st, p.path, 'code-server', '', `open ${p.path} in code-server`) || el('span', { class: 'dim', text: 'code-server port unknown (rerun install.sh)' })));
  box.append(el('div', { class: 'pj-mrow pj-danger' }, el('span', { class: 'dim', text: 'Deletes the project folder and every repo in it.' }),
    confirmButton('del:' + p.name, 'Delete project', async () => { await api('DELETE', `/api/projects/${encodeURIComponent(p.name)}`); navigate('#/'); })));
  return box;
}

function pjPatchHeader(P, st, p) {
  const h = P.head;
  setText(h.name, p.name);
  setText(h.path, p.path || '');
  h.quad.setAttribute('href', pjQuadHash(p.name));
  const sig = pjSig([p.name, (p.repos || []).map((r) => [r.name, r.path, r.state, r.branch, r.dirty, typeof repoCost === 'function' ? repoCost(p, r) : '']), pjCodePort(st)]);
  if (h.sig.repos !== sig) {
    h.sig.repos = sig;
    h.repos.textContent = '';
    for (const r of (p.repos || [])) h.repos.append(pjRepoChip(st, p, r));
    if (!(p.repos || []).length) h.repos.append(el('span', { class: 'dim', text: 'no repos yet: ... > Add repo, or start a session in the project folder' }));
  }
  pjPatchActions(P, pjParse(P.route, st).tab);
  const cost = typeof costText === 'function' ? costText(p) : '';
  const hrs = pjHours(P, p.name);
  setText(h.stats, [cost, hrs].filter(Boolean).join(' · '));
  h.stats.classList.toggle('hidden', !h.stats.textContent);
  const awaiting = typeof ui.confirm === 'string' && (ui.confirm === 'del:' + p.name || ui.confirm.startsWith('rm:' + p.name + '/'));
  const open = P.manage || awaiting;
  h.manage.classList.toggle('hidden', !open);
  const msig = pjSig([ui.confirm, (p.repos || []).map((r) => [r.name, r.state]), p.path, pjCodePort(st)]);
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
  // no button here: the header's + session is the primary of this tab and is already on screen
  const empty = pageEmpty('console', 'No sessions here', 'A session is a Claude, Codex or shell window in a repo of this project. Start one with + session above.');
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
      if (typeof Pages !== 'undefined' && Pages && typeof Pages.markLead === 'function') Pages.markLead();   // the first row with a permission waiting carries the filled Allow
    },
    onRoute(rt) { this.update(pjState(), rt); },
    destroy() { if (typeof sessionTailStopAll === 'function') sessionTailStopAll(); },
  };
}

function pjTasksView(P, route) {
  const scopeNote = pjScopeNote(P, route);
  let scope = '';                                                    // the project the tab shows: the dispatch bar lists its sessions
  const board = makeTaskBoard({ cls: 'pj-kanban', project: () => scope, empty: 'nothing queued' });
  // nothing yet: only the empty state, with the tab's one primary (the toolbar and the columns would just be headings above it)
  const none = pageEmpty('git-branch', 'No tasks in this project yet', 'A task is one worktree and branch per piece of work: + task starts one now, parks it in the Backlog, or schedules it.');
  none.append(el('button', { class: 'primary', type: 'button', text: '+ task', onclick: () => pjCreate('task') }));
  none.classList.add('hidden');
  const bar = pjToolbar(el('button', { class: 'small primary', type: 'button', text: '+ task', onclick: () => pjCreate('task') }),
    el('span', { class: 'dim', text: 'one worktree and branch per task; the columns follow the session state' }));
  const node = el('div', { class: 'pj-tasks' }, bar, scopeNote, board.node, none);
  return {
    node,
    update(st, rt) {
      const cur = pjParse(rt, st);
      scopeNote.ccPatch(cur);
      scope = cur.project;
      const tasks = boardTasks(st).filter((t) => t.project === cur.project && (!cur.repo || t.repo === cur.repo));       // the poll's rows with the optimistic Start / add / edit / delete laid over them
      none.classList.toggle('hidden', !!tasks.length);
      bar.classList.toggle('hidden', !tasks.length);
      board.node.classList.toggle('hidden', !tasks.length);
      board.update(tasks, st);          // the dispatch bar, the chains and the columns; it repaints only when something it shows changed, and never mid-drag
    },
    onRoute(rt) { this.update(pjState(), rt); },
    destroy() { /* nothing is subscribed */ },
  };
}

/* The cron presets of the schedule form, as plain words under the list when it is empty. */
const PJ_PRESET_WORDS = 'Presets: nightly 02:30, weekdays 09:00, hourly, one-off.';

function pjJobRow(j, runs) {
  const jr = runs.filter((r) => r.job_id === j.id).slice(0, 3);
  const agent = j.agent || 'claude';
  const row = el('div', { class: 'sess pj-job' + (j.enabled ? '' : ' muted'), 'data-job': j.id, 'data-agent': agent },
    el('div', { class: 'main' },
      agentGlyph(agent),
      el('span', { class: 'name', text: j.name }),
      el('span', { class: j.enabled && j.next_run_at ? 'state' : 'state ended', text: j.enabled && j.next_run_at ? 'next ' + fmtTs(j.next_run_at) : 'disabled' }),
      el('span', { class: 'meta', text: `${j.project}/${j.repo} · ${j.cron ? 'cron ' + j.cron : 'one-off'} · ${j.permission_mode}${jobLimitText(j)}${j.last_status ? ' · last: ' + j.last_status + (j.last_run_at ? ' ' + fmtTs(j.last_run_at) : '') : ''}` })),
    el('div', { class: 'actions' },
      jobFableButton(j),
      el('button', { type: 'button', onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/run`); } catch (e) { setError(e.message); } await poll(true); } }, ic('play'), 'Run now'),
      el('button', { class: 'minimal', type: 'button', onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/toggle`); } catch (e) { setError(e.message); } await poll(true); }, text: j.enabled ? 'Disable' : 'Enable' }),
      confirmButton('job:' + j.id, 'Delete', () => api('DELETE', `/api/jobs/${j.id}`), true)));       // quiet: red-outlined until the second tap, then filled
  for (const r of jr) {
    const ra = r.agent || agent;
    row.append(el('div', { class: 'last', 'data-run': r.id, 'data-agent': ra },
      el('span', { class: 'dim pj-run-line' }, agentGlyph(ra), ' ', runLineText(r, agent)),
      r.result ? el('span', { text: String(r.result).slice(0, 300) }) : null,
      r.task_id ? el('span', { class: 'row' },
        el('button', { type: 'button', title: ra === 'codex' && r.session_id ? `codex resume ${r.session_id}` : null, onclick: async () => { try { const x = await api('POST', `/api/runs/${r.id}/resume`); openPage(x.attach_url); } catch (e) { setError(e.message); } await poll(true); }, text: 'Resume in terminal' }),
        el('span', { class: 'dim', text: `task card: ${r.branch}` })) : null));
  }
  return row;
}

/* The chains of the scope as connected cards, one row per chain: a card per step with its agent glyph, its state (taskStepStatus) and the head of its result, joined by arrows.
   A step that is waiting says what for. Built from the board's task rows (chain_id / parent_id link the steps; taskChainInfo orders them). */
function pjChainCards(tasks) {
  const info = taskChainInfo(tasks);
  const groups = new Map();
  for (const t of tasks) {
    const i = info.get(t.id);
    if (!i) continue;
    if (!groups.has(i.chain)) groups.set(i.chain, []);
    groups.get(i.chain).push({ t, i });
  }
  const out = [];
  for (const [key, steps] of groups) {
    if (steps.length < 2) continue;
    steps.sort((a, b) => a.i.step - b.i.step || Number(a.t.id) - Number(b.t.id));
    const done = steps.filter(({ t }) => taskPhase(t) === 'done').length;
    const list = el('ol', { class: 'pj-steps' });
    steps.forEach(({ t, i }) => {
      const st = taskStepStatus(t);
      const hold = taskLimitHold(t);
      const ph = taskPhase(t);
      const head = String(t.result || '').replace(/\s+/g, ' ').trim().slice(0, 140);
      const parent = steps.find((x) => String(x.t.id) === String(t.parent_id));
      const note = head || (ph === 'queued' ? (hold ? hold.text : `starts when step ${parent ? parent.i.step : Math.max(1, i.step - 1)} is done`) : ph === 'backlog' ? 'ready: dispatch it from the Tasks tab' : ph === 'cancelled' ? 'cancelled' : '');
      const live = t.tmux && (ph === 'running' || ph === 'done') ? t.tmux : '';
      list.append(el('li', { class: 'pj-step', 'data-step': t.id, 'data-phase': ph, 'data-agent': t.agent || 'claude' },
        el('span', { class: 'pj-step-n', text: String(i.step) }),
        el('span', { class: 'pj-step-head' }, agentGlyph(t.agent || 'claude'), el('span', { class: 'pj-step-title', text: t.title })),
        el('span', { class: 'pj-step-st' }, stateGlyph(st.glyph), ' ', st.label),
        note ? el('span', { class: 'pj-step-res dim', text: note }) : null,
        live ? el('button', { class: 'small minimal', type: 'button', title: `open ${t.title}`, onclick: () => { if (typeof taskOpenPeek === 'function') taskOpenPeek(live); }, text: 'Open' }) : null));
    });
    const flow = steps.map(({ t }) => AGENT_GLYPH[t.agent || 'claude'] || AGENT_GLYPH.shell).join(' → ');
    out.push(el('section', { class: 'pj-chain', 'data-chain': key, role: 'group', 'aria-label': `A chain of ${steps.length} steps` },
      el('div', { class: 'pj-chain-head' }, el('h3', { class: 'pj-chain-title', text: `Chain of ${steps.length}` }), el('span', { class: 'dim mono', text: flow }),
        el('span', { class: 'dim', text: `${done} of ${steps.length} done` })),
      list));
  }
  return out;
}

function pjSchedulesView(P, route) {
  const scopeNote = pjScopeNote(P, route);
  const list = el('div', { class: 'pj-jobs' });
  const chainsHost = el('div', { class: 'pj-chains hidden' });
  // no button in the empty state: the toolbar's + schedule is the tab's primary and stays on screen
  const none = pageEmpty('time', 'No schedules in this project', `A schedule runs a headless prompt on a cron, or once now, in a fresh worktree, with Claude or Codex. ${PJ_PRESET_WORDS}`);
  const note = el('span', { class: 'dim pj-windows' });
  const bar = pjToolbar(el('button', { class: 'small primary', type: 'button', text: '+ schedule', onclick: () => pjCreate('schedule') }),
    el('button', { class: 'small', type: 'button', onclick: () => { if (typeof Shell !== 'undefined' && Shell && typeof Shell.openCreate === 'function') Shell.openCreate('batch'); } }, ic('layers'), 'Batch prompt'),
    note);
  let sig = null;
  const node = el('div', { class: 'pj-schedules' }, bar, scopeNote, list, none, chainsHost);
  return {
    node,
    update(st, rt) {
      const cur = pjParse(rt, st);
      scopeNote.ccPatch(cur);
      const jobs = (st.jobs || []).filter((j) => j.project === cur.project && (!cur.repo || j.repo === cur.repo));
      const ids = new Set(jobs.map((j) => j.id));
      const runs = (st.runs || []).filter((r) => ids.has(r.job_id));
      const chained = boardTasks(st).filter((t) => t.project === cur.project && (!cur.repo || t.repo === cur.repo) && t.chain_id);
      const notes = schedWindowNotes(st, jobs);
      none.classList.toggle('hidden', !!jobs.length || !!chained.length);
      list.classList.toggle('hidden', !jobs.length);
      const next = pjSig([jobs, runs, ui.confirm, chained, notes]);
      if (sig === next) return;
      sig = next;
      note.textContent = `headless runs · at most 2 at once · each agent pauses above 85% of its window · ${notes.map((x) => x.text).join(' · ')}`;
      list.textContent = '';
      for (const j of jobs) list.append(pjJobRow(j, runs));
      const cards = pjChainCards(chained);
      chainsHost.textContent = '';
      if (cards.length) chainsHost.append(el('h2', { class: 'pj-sec', text: 'Chains' }), ...cards);
      chainsHost.classList.toggle('hidden', !cards.length);
    },
    onRoute(rt) { this.update(pjState(), rt); },
    destroy() { /* nothing is subscribed */ },
  };
}

/* The Memory tab (v0.5.20): the palace of this project, the same renderer, filters (the repo kept by the Memory page) and states as #/memory (pages/memory.js). */
function pjMemoryView(P) {
  const project = P.project;
  const palace = typeof memoryPalaceView === 'function' ? memoryPalaceView(project, { repo: typeof memStoredFilters === 'function' ? memStoredFilters(project).repo : '' }) : null;
  const node = el('div', { class: 'pj-memory' },
    el('div', { class: 'pj-memory-head' }, el('a', { class: 'btn small', href: `#/memory/${encodeURIComponent(project)}`, text: 'Open in Memory' }),
      el('span', { class: 'dim', text: 'What claude-mem kept from past sessions here.' })),
    palace ? palace.node : pageEmpty('database', 'Project memory', 'The memory palace of this project: observations, timeline and summaries.'));
  if (palace) palace.load();
  return { node, update() { /* the palace is fetched once per visit; Retry and the Memory page refresh it */ }, onRoute() { /* static */ }, destroy() { if (palace) palace.destroy(); } };
}

/* The newest gotchas in the header (v0.5.20): fetched once per project and visit, only while state.memory exists, and never a toast, a banner or a wait: a stopped worker,
   a slow one or a project without gotchas leaves the strip hidden. */
function pjGotchas(P, st) {
  const h = P.head.gotchas;
  if (!st || !st.memory || typeof memoryGotchasMount !== 'function') { if (P.gotchFor) { P.gotchFor = null; h.textContent = ''; h.classList.add('hidden'); } return; }
  if (P.gotchFor === P.project) return;
  P.gotchFor = P.project;
  h.textContent = '';
  h.classList.add('hidden');
  const project = P.project;
  memoryGotchasMount(h, project, { isCurrent: () => projectPage.cur === P && P.project === project });
}

/* The Files tab: a repo switcher and the toggles, the tree on the left (full width on a phone), the preview beside it (below it on a phone). */
const pjHintGone = new Set();       // untracked-cache hints dismissed in this tab, for a browser that blocks storage

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
    const b = el('button', { class: 'minimal small', type: 'button', 'aria-pressed': fl.flags[key] ? 'true' : 'false', title, text: label });
    b.addEventListener('click', () => {
      fl.flags[key] = !fl.flags[key];
      b.setAttribute('aria-pressed', fl.flags[key] ? 'true' : 'false');
      pjSaveFlags(fl.flags);
      mountTree();
    });
    return b;
  };
  const refreshBtn = el('button', { class: 'minimal small', type: 'button', title: 'Reload the tree and the open file', onclick: () => { if (fl.tree) fl.tree.refresh(); if (fl.preview) fl.preview.reload(); } }, ic('refresh'), 'Refresh');
  const bar = pjToolbar(switcher, branch, el('span', { class: 'spacer' }), flagBtn('hidden', 'hidden', 'show dotfiles'), flagBtn('ignored', 'ignored', 'show git-ignored files'), refreshBtn);
  const ucHost = el('div', { class: 'pj-uc-host' });
  const node = el('div', { class: 'pj-files' }, bar, el('div', { class: 'pj-split' }, el('div', { class: 'pj-treebox' }, ucHost, treeHost), previewBox));
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

  /* The slow-scan hint (issue #112): one quiet line with the command to try and a Copy button. The board only says it: nothing runs here, and the
     owner's Dismiss is remembered per repo (blocked storage still hides it for this session). */
  const ucKey = () => `ccboard:hint:uc:${project}/${fl.repo}`;
  const ucGone = () => {
    if (pjHintGone.has(ucKey())) return true;
    try { return localStorage.getItem(ucKey()) === '1'; } catch (_) { return false; }
  };
  const paintHint = (h) => {
    if (!h || h.kind !== 'untracked_cache' || typeof h.cmd !== 'string' || !h.cmd || ucGone() || ucHost.firstChild) return;
    const dismiss = el('button', { class: 'minimal small', type: 'button', 'aria-label': 'Dismiss this hint', title: 'Do not show this hint for this repo again', text: 'Dismiss' });
    dismiss.addEventListener('click', () => {
      pjHintGone.add(ucKey());
      try { localStorage.setItem(ucKey(), '1'); } catch (_) { /* blocked storage: gone for this session only */ }
      ucHost.textContent = '';
    });
    ucHost.append(el('div', { class: 'pj-uchint dim', role: 'note' },
      el('span', { class: 'pj-uc-text', text: 'Scans of this repo are slow. This may help (run it yourself; the board never changes your git config):' }),
      el('span', { class: 'doc-cmdline' }, el('code', { class: 'doc-cmd', text: h.cmd }), copyButton(h.cmd, 'the command'), dismiss)));
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
    ucHost.textContent = '';
    if (typeof Tree === 'undefined') { treeHost.append(el('p', { class: 'bad', text: 'tree.js did not load' })); return; }
    const sel = fl.shown;
    fl.tree = Tree.mount(treeHost, { project, repo: fl.repo, hidden: fl.flags.hidden, ignored: fl.flags.ignored, repos: true,
      storageKey: `ccboard:tree:${project}/${fl.repo}`, onOpen: openFile, onLoad: paintBranch, onHint: paintHint,
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
  const tasks = boardTasks(st).filter((t) => t.project === cur.project && (!cur.repo || t.repo === cur.repo)).length;
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
    t.link(P.panel);                                                    // one panel, labelled by whichever tab is selected
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
  if (cur.project !== P.project) { P.project = cur.project; P.viewKey = null; P.tabSig = null; P.manage = false; P.gotchFor = null; P.head.sig = { repos: null, tab: null, manage: null }; if (P.view) { P.view.destroy(); P.view.node.remove(); P.view = null; } }
  P.loading = !st;
  const p = pjFind(st, cur.project);
  if (typeof Pages !== 'undefined' && Pages && typeof Pages.dropSkeleton === 'function' && st) Pages.dropSkeleton();
  pjShowKnown(P, !!p);
  if (!p) {
    setText(P.unknownTitle, cur.project);
    return;
  }
  pjPatchHeader(P, st, p);
  pjGotchas(P, st);
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
    if (route && route.id === 'project') P.route = route;     // under the session peek the router hands this page the peek's route: keep the project's own
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
