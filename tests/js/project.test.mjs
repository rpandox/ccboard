// Contract tests for the v0.5.6 project page (app/static/pages/project.js, route #/p/<project>[/<repo>]?tab=&path=): header, tabs without a remount,
// sessions grouped like Home, the tasks columns with Backlog, schedules, the files tab (tree + preview), Add repo, Remove repo and Delete project.
// The real scripts in index.html order on minidom's DOM; api() is the fake of tests/js/treekit.mjs (tree and file requests are answered from the demo
// fixtures, everything else from server.answers), Shell.openCreate and the toasts are recorders.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, plain } from './harness.mjs';
import { EPOCH, ISO, fixtureState } from './world.mjs';
import { byPath, focused, key, labelOf, makeNetworkWorld, pathOf, settle, treeItems, visibleItems, TREE_FIXTURE } from './treekit.mjs';

const PAGE_FILES = ['home', 'inbox', 'widgets', 'tasks', 'project', 'agents', 'settings', 'search', 'session', 'usage', 'placeholders'];       // widgets.js is also loaded earlier when the shell is (shell.js reads Widgets)

const SUMMARY = { windows: { '7d': { total: 171.4, by_project: [{ project: 'phasezero', total: 171.4, hours: 12.5 }, { project: 'ccboard', total: 58.1, hours: 8.6 }] } } };

/** The demo-like state of tests/js/world.mjs with what the project page reads: dirty repo, cost record, job runs, a closed task. */
function projectState(over = {}) {
  const st = fixtureState();
  const pz = st.projects.find((p) => p.name === 'phasezero');
  pz.repos.find((r) => r.name === 'NestJs-Ecommerce-Backend').branch = 'develop';
  pz.repos.find((r) => r.name === 'NestJs-Ecommerce-Backend').dirty = true;
  st.cost = { value: { projects: { phasezero: { total: 412.6, today: 38.2, week: 171.4, repos: { website: 196.3, 'NestJs-Ecommerce-Backend': 216.3 } },
    petroit: { total: 49.3, today: 15.7, week: 49.3, repos: { api: 49.3 } } } }, at: ISO(5) };
  st.runs = [1, 2, 3, 4].map((n) => ({ id: 10 + n, job_id: 3, started_at: ISO(60 * 24 * 7 * n), finished_at: ISO(60 * 24 * 7 * n - 6), status: n === 3 ? 'error' : 'ok',
    result: `digest ${n}`, error: null, session_id: null, cost_usd: 0.5 * n, num_turns: 4 + n, task_id: null }));
  st.tasks.push({ id: 9, slug: 'petroit-task', title: 'A petroit task', project: 'petroit', repo: 'api', tmux: 'petroit--api--t-x', column: 'merged', mode: 'worktree', pr_number: null, pr_url: null, pr_state: null });
  return { ...st, ...over };
}

/** A world with every page loaded; `answers` are api prefix answers. Returns helpers over it. */
function projectWorld({ state = projectState(), answers = {}, wide = false, shell = false, realCreate = false } = {}) {
  const env = makeNetworkWorld({ extra: { matchMedia: (q) => ({ matches: wide && /(1024|840)/.test(q), addEventListener() {}, removeEventListener() {} }) } });
  const { w, server } = env;
  Object.assign(server.answers, { '/api/usage/summary': SUMMARY }, answers);
  for (const f of ['live.js', 'launcher.js', 'tree.js', 'router.js', ...(shell ? ['pages/widgets.js', 'shell.js'] : [])]) w.load(f);
  w.ctx.__toasts = []; w.ctx.__created = []; w.ctx.__live = { subscribed: [] };
  w.run(`
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    poll = async () => {};
    ${shell ? (realCreate ? '' : 'Shell.openCreate = (kind, pre) => { __created.push({ kind, pre }); return true; };') : 'globalThis.Shell = { openCreate: (kind, pre) => { __created.push({ kind, pre }); return true; } };'}
    Live.subscribe = (tmux, fn) => { __live.subscribed.push(tmux); return () => {}; };
    Live.unsubscribe = () => {};
  `);
  for (const f of PAGE_FILES) if (fs.existsSync(path.join(STATIC, 'pages', `${f}.js`)) && !(shell && f === 'widgets')) w.load(`pages/${f}.js`);
  w.run("for (const id of Object.keys(pages)) { const m = pages[id].mount; globalThis.__mounts = globalThis.__mounts || {}; __mounts[id] = 0; pages[id].mount = function (...a) { __mounts[id]++; return m.apply(this, a); }; }");
  w.ctx.__st = state;
  w.run('state = __st');
  if (shell) w.run('installShell(); renderShell(state);');
  return { ...env, state, page: () => w.document.querySelector('#page'), mounts: (id) => w.get('__mounts')[id] };
}

const go = async (env, hash) => { env.w.location.hash = hash; await settle(); return env.page(); };
const textOf = (n) => (n ? n.textContent : '');
const all = (root, sel) => root.querySelectorAll(sel);
const byText = (root, sel, re) => all(root, sel).find((n) => re.test(n.textContent));
const button = (root, re) => byText(root, 'button', re);
const callsOf = (env, method, prefix) => plain(env.w.get('__calls')).filter((c) => c.method === method && c.path.startsWith(prefix));
const created = (env) => plain(env.w.get('__created'));
const tabIds = (root) => all(root, '[role=tab]').map((t) => t.getAttribute('data-tab'));
const selectedTab = (root) => all(root, '[role=tab]').filter((t) => t.getAttribute('aria-selected') === 'true').map((t) => t.getAttribute('data-tab'));
const shownNode = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return false; return true; };
const shownButtons = (root, re) => all(root, 'button').filter((b) => shownNode(b) && re.test(b.textContent));
const folderLinks = (root) => all(root, 'a').map((a) => a.getAttribute('href')).filter((h) => h && h.includes('folder='));

// ---------------------------------------------------------------- the route and the header

test('#/p/<project> mounts the real page, not the placeholder: name, path, per-repo branch chips with a dirty dot', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  assert.equal(env.mounts('project'), 1);
  assert.equal(env.w.document.body.getAttribute('data-page') ?? 'project', 'project');
  assert.doesNotMatch(textOf(page), /arrives in/);
  assert.match(textOf(page), /phasezero/);
  assert.match(textOf(page), /\/srv\/projects\/phasezero/, 'the folder path, in the mono font');
  assert.match(env.w.document.title, /phasezero/);
  const text = textOf(page);
  for (const [repo, branch] of [['website', 'main'], ['NestJs-Ecommerce-Backend', 'develop']]) {
    assert.ok(text.includes(repo) && text.includes(branch), `a chip for ${repo} · ${branch}`);
  }
  const dirty = all(page, '[class*=dirty]');
  assert.ok(dirty.length >= 1, 'the dirty repo has a dot');
  const chip = (repo) => all(page, '*').find((n) => n.children.length >= 1 && n.textContent.includes(repo) && n.textContent.length < 80 && n.querySelector && !n.querySelector('[role=tab]') && (n.className || '').includes('chip'));
  const dirtyChips = all(page, '[class*=chip]').filter((c) => c.querySelector('[class*=dirty]') || (c.className || '').includes('dirty'));
  assert.deepEqual(dirtyChips.map((c) => /NestJs-Ecommerce-Backend/.test(c.textContent)), [true], 'only the repo with changes is dirty');
  void chip;
});

test('the header shows cost today and 7 d from the state and the active hours of the last 7 days from the usage summary (fetched once per mount)', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  assert.match(textOf(page), /\$38\.20/, 'cost today');
  assert.match(textOf(page), /\$171\.40/, 'cost over 7 days');
  assert.match(textOf(page), /12\.5\s?h|12h\s?30/, 'active hours, 7 d');
  assert.equal(callsOf(env, 'GET', '/api/usage/summary').length, 1);
  assert.match(callsOf(env, 'GET', '/api/usage/summary')[0].path, /days=7/);
  await go(env, '#/p/phasezero?tab=tasks');
  await go(env, '#/p/phasezero/website?tab=files');
  env.w.run('updateCurrentPage(state)');
  await settle();
  assert.equal(callsOf(env, 'GET', '/api/usage/summary').length, 1, 'a tab change, a repo change and a state update do not refetch');
});

test('the usage summary failing leaves the header without hours and shows no error', async () => {
  const env = projectWorld({ answers: { '/api/usage/summary': () => { throw new Error('summary down'); } } });
  const page = await go(env, '#/p/phasezero');
  assert.match(textOf(page), /\$38\.20/);
  assert.equal(plain(env.w.get('__toasts')).length, 0, 'quiet on error');
  assert.doesNotMatch(textOf(page), /summary down/);
  assert.doesNotMatch(textOf(page), /\bh\b.*\bh\b.*undefined|NaN|undefined/);
});

test('a project that has no cost record or hours yet renders its header', async () => {
  const st = projectState();
  st.cost = null;
  const env = projectWorld({ state: st, answers: { '/api/usage/summary': { windows: { '7d': { by_project: [] } } } } });
  const page = await go(env, '#/p/phasezero');
  assert.match(textOf(page), /phasezero/);
  assert.doesNotMatch(textOf(page), /NaN|undefined|null/);
});

test('+ session, + task and + schedule open the create sheets with the project (and the repo of the route) preselected', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero/website');
  for (const [re, kind] of [[/\+\s*session/i, 'session'], [/\+\s*task/i, 'task'], [/\+\s*schedule/i, 'schedule']]) {
    const b = button(page, re);
    assert.ok(b, `a ${kind} button in the header`);
    b.click();
  }
  const got = created(env);
  assert.deepEqual(got.map((c) => c.kind), ['session', 'task', 'schedule']);
  for (const c of got) assert.deepEqual([c.pre.project, c.pre.repo], ['phasezero', 'website'], `${c.kind} is preselected`);
});

test('without a repo in the route the sheets get the project and the repo the page would use (a repo of the project, never another project)', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  button(page, /\+\s*session/i).click();
  const c = created(env)[0];
  assert.equal(c.pre.project, 'phasezero');
  assert.ok([undefined, null, 'root', 'website', 'NestJs-Ecommerce-Backend'].includes(c.pre.repo), String(c.pre.repo));
});

test('code-server links: the project folder and every repo, built from the state', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  const links = folderLinks(page);
  for (const dir of ['/srv/projects/phasezero', '/srv/projects/phasezero/website', '/srv/projects/phasezero/NestJs-Ecommerce-Backend']) {
    assert.ok(links.includes(`https://box:10000/?folder=${encodeURIComponent(dir)}`), `a code-server link for ${dir}`);
  }
  for (const a of all(page, 'a').filter((x) => (x.getAttribute('href') || '').includes('folder='))) {
    assert.equal(a.getAttribute('target'), '_blank');
    assert.match(a.getAttribute('rel') || '', /noopener/);
  }
});

test('without a code-server port the page still renders and says why there is no link', async () => {
  const st = projectState();
  delete st.config.code_https_port;
  const env = projectWorld({ state: st });
  const page = await go(env, '#/p/phasezero');
  assert.equal(folderLinks(page).length, 0);
  assert.match(textOf(page), /code-server port unknown|install\.sh/i);
});

test('Add repo (in the ... menu) opens the sheet with addRepoForm; submitting it POSTs to the project', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  assert.equal(shownButtons(page, /add repo/i).length, 0, 'the header keeps three buttons: Add repo is in the ... menu');
  all(page, 'button').find((b) => b.getAttribute('aria-haspopup') === 'menu').click();
  env.w.document.querySelectorAll('.menuitem').find((i) => /^Add repo$/.test(i.textContent.trim())).click();
  const sheet = env.w.document.querySelector('#sheet');
  assert.equal(sheet.open, true, 'the sheet is open');
  const name = all(sheet, 'input').find((i) => /repo name/i.test(i.getAttribute('placeholder') || ''));
  assert.ok(name, 'the add-repo form');
  name.value = 'shop-admin';
  all(sheet, 'form')[0].dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  const posts = callsOf(env, 'POST', '/api/projects/phasezero/repos');
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { name: 'shop-admin' });
});

// ---------------------------------------------------------------- Remove repo and Delete project (overflow menu, panel, two taps)

const overflow = (env) => all(env.page(), 'button').find((b) => b.getAttribute('aria-haspopup') === 'menu');
const menuItem = (env, re) => env.w.document.querySelectorAll('.menuitem').find((i) => re.test(i.textContent));

test('the repos and danger panel is closed until the overflow menu opens it', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  assert.equal(shownButtons(page, /^Remove$/).length, 0, 'Remove is not on screen by default');
  assert.equal(shownButtons(page, /^Delete project$/).length, 0, 'neither is Delete project');
  const more = overflow(env);
  assert.ok(more, 'an overflow button');
  more.click();
  assert.deepEqual(env.w.document.querySelectorAll('.menuitem').map((i) => i.textContent.trim()), ['Add repo', 'Open the folder in code-server', 'Repos and danger zone', 'Delete project']);
  menuItem(env, /repos and danger/i).click();
  assert.equal(shownButtons(env.page(), /^Remove$/).length, 2, 'one Remove per repo');
  assert.equal(shownButtons(env.page(), /^Delete project$/).length, 1);
  button(env.page(), /^Close$/).click();
  assert.equal(shownButtons(env.page(), /^Remove$/).length, 0, 'Close puts it away again');
});

test('Remove repo is a two-tap button per repo and DELETEs that repo', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero');
  overflow(env).click();
  menuItem(env, /repos and danger/i).click();
  const rm = shownButtons(env.page(), /^Remove$/);
  assert.equal(rm.length, 2, 'one per repo');
  rm[0].click();
  assert.equal(callsOf(env, 'DELETE', '/api/projects').length, 0, 'the first tap only asks');
  const confirm = shownButtons(env.page(), /^Confirm Remove$/)[0];
  assert.ok(confirm, 'Confirm Remove appears');
  assert.ok(shownButtons(env.page(), /^Cancel$/).length >= 1, 'with a Cancel');
  confirm.click();
  await settle();
  const del = callsOf(env, 'DELETE', '/api/projects/phasezero/repos/');
  assert.equal(del.length, 1);
  assert.match(del[0].path, /^\/api\/projects\/phasezero\/repos\/(NestJs-Ecommerce-Backend|website)$/);
});

test('Cancel after the first tap puts the Remove button back and deletes nothing', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero');
  overflow(env).click();
  menuItem(env, /repos and danger/i).click();
  shownButtons(env.page(), /^Remove$/)[1].click();
  shownButtons(env.page(), /^Cancel$/)[0].click();
  assert.equal(shownButtons(env.page(), /^Remove$/).length, 2);
  assert.equal(shownButtons(env.page(), /^Confirm/).length, 0);
  assert.equal(callsOf(env, 'DELETE', '/api/projects').length, 0);
});

test('Delete project (overflow menu, then a Confirm tap) DELETEs the project and goes home', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero');
  overflow(env).click();
  menuItem(env, /delete project/i).click();
  await settle();
  const isDelete = (c) => c.path === '/api/projects/phasezero';
  assert.equal(callsOf(env, 'DELETE', '/api/projects/phasezero').filter(isDelete).length, 0, 'the menu item deletes nothing');
  const confirm = shownButtons(env.page(), /^Confirm Delete project$/)[0];
  assert.ok(confirm, 'the second tap is a Confirm button, in the panel');
  confirm.click();
  await settle();
  assert.equal(callsOf(env, 'DELETE', '/api/projects/phasezero').filter(isDelete).length, 1);
  assert.equal(env.w.location.hash, '#/', 'and the page navigates home');
});

test('Delete project from the panel needs the same two taps', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero');
  overflow(env).click();
  menuItem(env, /repos and danger/i).click();
  shownButtons(env.page(), /^Delete project$/)[0].click();
  assert.equal(callsOf(env, 'DELETE', '/api/projects/phasezero').length, 0);
  shownButtons(env.page(), /^Confirm Delete project$/)[0].click();
  await settle();
  assert.equal(callsOf(env, 'DELETE', '/api/projects/phasezero').filter((c) => c.path === '/api/projects/phasezero').length, 1);
});

// ---------------------------------------------------------------- tabs

test('tabs: sessions (default), tasks, schedules, files; memory only once the state has it', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  assert.deepEqual(tabIds(page), ['sessions', 'tasks', 'schedules', 'files']);
  assert.deepEqual(selectedTab(page), ['sessions']);
  env.w.ctx.__st = { ...env.state, memory: { enabled: true } };
  env.w.run('state = __st; updateCurrentPage(state)');
  assert.deepEqual(tabIds(env.page()), ['sessions', 'tasks', 'schedules', 'files', 'memory']);
});

test('?tab= selects the tab, a hash change within the page takes onRoute (no remount), and a tab click navigates', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero?tab=tasks');
  assert.deepEqual(selectedTab(env.page()), ['tasks']);
  assert.equal(env.mounts('project'), 1);
  await go(env, '#/p/phasezero?tab=schedules');
  assert.deepEqual(selectedTab(env.page()), ['schedules']);
  await go(env, '#/p/phasezero/website?tab=files');
  assert.deepEqual(selectedTab(env.page()), ['files']);
  await go(env, '#/p/petroit');
  assert.equal(env.mounts('project'), 1, 'another project is the same page: onRoute, not mount');
  assert.match(textOf(env.page()), /petroit/);
  assert.doesNotMatch(env.w.document.title, /phasezero/);
  await go(env, '#/p/phasezero');
  all(env.page(), '[role=tab]').find((t) => t.getAttribute('data-tab') === 'tasks').click();
  assert.match(env.w.location.hash, /^#\/p\/phasezero\?tab=tasks$/, 'a click on a tab puts it in the address');
  await settle();
  assert.deepEqual(selectedTab(env.page()), ['tasks']);
  assert.equal(env.mounts('project'), 1);
});

test('leaving the page and coming back mounts again; an unknown tab falls back to sessions', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero?tab=nonsense');
  assert.deepEqual(selectedTab(env.page()), ['sessions']);
  await go(env, '#/usage');
  await go(env, '#/p/phasezero');
  assert.equal(env.mounts('project'), 2);
});

test('an unknown project shows a message and a way back instead of throwing', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/nope');
  assert.match(textOf(page), /nope/);
  assert.doesNotMatch(textOf(page), /arrives in/);
  assert.ok(all(page, 'a').some((a) => a.getAttribute('href') === '#/'), 'a link home');
});

// ---------------------------------------------------------------- sessions tab

const homeGroups = async (env, project) => {
  await go(env, '#/');
  const block = env.page().querySelector(`[data-project=${project}]`);
  return all(block, '.pgroup').map((g) => ({ label: textOf(g.querySelector('.pg-name')), rows: all(g, '.rrow').map((r) => r.getAttribute('data-tmux')) }));
};

test('the sessions tab lists the project\'s rows grouped like Home: project folder first, then the repos, rows in Home\'s order, no other project', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  const groups = all(page, '.pgroup').map((g) => ({ label: textOf(g.querySelector('.pg-name')), rows: all(g, '.rrow').map((r) => r.getAttribute('data-tmux')) }));
  assert.deepEqual(groups.map((g) => g.label), ['project folder', 'NestJs-Ecommerce-Backend', 'website']);
  assert.deepEqual(groups.map((g) => g.rows), [['phasezero--root--s1'], ['phasezero--NestJs-Ecommerce-Backend--t-stock-sync'], ['phasezero--website--t-checkout-redesign']]);
  assert.equal(all(page, '.rrow').length, 3, 'only this project\'s sessions');
  for (const r of all(page, '.rrow')) assert.ok(r.getAttribute('data-tmux').startsWith('phasezero--'));
  const onHome = await homeGroups(env, 'phasezero');
  assert.deepEqual(onHome.map((g) => g.label), groups.map((g) => g.label), 'the same group order as Home');
});

test('inside a repo the rows follow Home\'s attention-first order, and a state update patches them in place', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/petroit');
  const rows = () => all(env.page(), '.rrow').map((r) => r.getAttribute('data-tmux'));
  const onHome = (await homeGroups(env, 'petroit'))[0].rows;
  await go(env, '#/p/petroit');
  assert.deepEqual(rows(), onHome);
  assert.equal(rows().length, 3);
  const node = env.page().querySelector('.rrow');
  const mountsBefore = env.mounts('project');
  const st = projectState();
  st.projects.find((p) => p.name === 'petroit').repos[0].sessions[0].state = 'working';
  env.w.ctx.__st = st;
  env.w.run('state = __st; updateCurrentPage(state)');
  assert.equal(env.page().querySelector('.rrow'), node, 'the same node: patched, not rebuilt');
  assert.equal(env.mounts('project'), mountsBefore, 'a state update never remounts');
  void page;
});

test('a project with no sessions shows an empty state with + session', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/mailgate');
  assert.equal(all(page, '.rrow').length, 0);
  assert.match(textOf(page), /no sessions|nothing running|start/i);
  const add = button(page, /\+\s*session/i);
  assert.ok(add);
  add.click();
  assert.equal(created(env)[0].kind, 'session');
  assert.equal(created(env)[0].pre.project, 'mailgate');
});

// ---------------------------------------------------------------- tasks tab

test('the tasks tab draws the kanban columns with an empty Backlog first, only this project\'s tasks, and a + task button', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero?tab=tasks');
  const heads = all(page, '.col h3').map((h) => h.textContent.replace(/\s*\(\d+\)\s*$/, ''));
  assert.deepEqual(heads, ['Backlog', 'In progress', 'Needs you'], 'Backlog always, then only the columns that have a card (no empty Done / PR open / Merged headings)');
  assert.deepEqual(all(page, '.task').map((t) => t.getAttribute('data-task')).sort(), ['4', '5'], 'phasezero\'s two tasks; petroit\'s is not here');
  const backlog = all(page, '.col').find((c) => /^Backlog/.test(c.querySelector('h3').textContent));
  assert.equal(all(backlog, '.task').length, 0, 'the Backlog is empty when no task is waiting (a task added with Later lands here: tests/js/tasks.test.mjs)');
  const inProgress = all(page, '.col').find((c) => /^In progress/.test(c.querySelector('h3').textContent));
  assert.deepEqual(all(inProgress, '.task').map((t) => t.getAttribute('data-task')), ['5']);
  const needs = all(page, '.col').find((c) => /^Needs you/.test(c.querySelector('h3').textContent));
  assert.deepEqual(all(needs, '.task').map((t) => t.getAttribute('data-task')), ['4']);
  button(page, /\+\s*task/i).click();
  assert.equal(created(env).at(-1).kind, 'task');
  assert.equal(created(env).at(-1).pre.project, 'phasezero');
});

test('a backlog task is a card in the Backlog column of its own project, counted on the tab, with Start, Send to session, Edit and Delete', async () => {
  const st = projectState();
  const row = { ci: null, pr: null, id: 30, project: 'phasezero', repo: 'website', slug: 'alt-text', title: 'Add alt text to the gallery', branch: '', base: 'main', worktree: '', tmux: '', claude_session_id: null,
    pr_url: null, pr_number: null, pr_state: null, cost_usd: null, overlap: [], preview_port: null, preview_https: null, preview_url: null, created_at: ISO(20), column: 'backlog', session: null,
    agent: 'claude', mode: 'worktree', auto_close: false, parent_id: null, chain_id: null, result: null, phase: 'backlog', session_row: null, prompt: 'every gallery img has an empty alt', prompt_len: 35 };
  st.tasks.unshift(row, { ...row, id: 31, project: 'petroit', repo: 'api', title: 'Another project\'s idea' });
  const env = projectWorld({ state: st });
  const page = await go(env, '#/p/phasezero?tab=tasks');
  const backlog = all(page, '.col').find((c) => /^Backlog/.test(c.querySelector('h3').textContent));
  assert.deepEqual(all(backlog, '.task').map((t) => t.getAttribute('data-task')), ['30'], 'only this project\'s backlog card');
  const card = all(backlog, '.task')[0];
  assert.deepEqual(all(card, 'button').map((b) => b.textContent.trim()).filter((l) => /^(Start|Send to session|Edit|Delete)$/.test(l)), ['Start', 'Send to session', 'Edit', 'Delete']);
  assert.match(card.textContent, /Add alt text to the gallery/);
  assert.match(card.textContent, /phasezero\/website/);
  const count = all(page, '[role=tab]').find((t) => t.getAttribute('data-tab') === 'tasks').querySelector('.tab-count');
  assert.equal(count.textContent, '3', 'the tab counts the backlog card with the two running tasks');
});

test('+ task on the project page never stops at a picker: it names a git repo of the project (the project folder only when that is the git repo); + session still asks', async () => {
  const st = projectState();
  st.projects.push({ name: 'notes', path: '/srv/projects/notes', root: { name: 'root', path: '/srv/projects/notes', root: true, state: 'ok', branch: 'main', dirty: false, sessions: [] }, orphan_sessions: [], repos: [] });
  const env = projectWorld({ state: st, shell: true });                                      // the real Shell.defaultRepo; only openCreate is a recorder
  let page = await go(env, '#/p/phasezero?tab=tasks');
  button(page, /\+\s*task/i).click();
  const t1 = created(env).at(-1);
  assert.equal(t1.kind, 'task');
  assert.ok(['website', 'NestJs-Ecommerce-Backend'].includes(t1.pre.repo), `a repo of the project, not the non-git folder: ${t1.pre.repo}`);
  page = await go(env, '#/p/phasezero/website?tab=tasks');
  button(page, /\+\s*task/i).click();
  assert.equal(created(env).at(-1).pre.repo, 'website', 'the repo of the address wins');
  page = await go(env, '#/p/phasezero?tab=files');
  button(page, /\+\s*task/i).click();
  assert.ok(created(env).at(-1).pre.repo, 'on the Files tab the repo the tree shows (or the first)');
  page = await go(env, '#/p/notes?tab=tasks');
  button(page, /\+\s*task/i).click();
  assert.equal(created(env).at(-1).pre.repo, 'root', 'a project whose only git place is its folder');
  button(page, /\+\s*schedule/i).click();
  assert.equal(created(env).at(-1).kind, 'schedule');
  assert.equal(created(env).at(-1).pre.repo, 'root');
  page = await go(env, '#/p/phasezero?tab=tasks');
  button(page, /\+\s*session/i).click();
  assert.notEqual(created(env).at(-1).kind, 'task');
});

test('a tap on + task / + session before the first /api/state says "still loading the board…" instead of doing nothing', async () => {
  const env = projectWorld({ shell: true, realCreate: true });                              // the real Shell.openCreate behind the page's buttons
  const page = await go(env, '#/p/phasezero?tab=tasks');
  env.w.run('state = null');                                                                // the board has not delivered a state (the buttons are drawn from the route already)
  for (const re of [/\+\s*task/i, /\+\s*schedule/i]) {
    const before = plain(env.w.get('__toasts')).length;
    const b = button(page, re);
    assert.ok(b, `a ${re} button`);
    b.click();
    const said = plain(env.w.get('__toasts')).slice(before);
    assert.deepEqual(said.map((t) => t.text), ['still loading the board…'], `${re} answers the tap`);
  }
  assert.equal(env.w.document.getElementById('sheet').open, false, 'and opens no sheet');
  env.w.run('state = __st');                                                                // the state arrives: the same tap opens the sheet
  button(page, /\+\s*task/i).click();
  assert.equal(env.w.document.getElementById('sheet').open, true);
});

test('a project with no tasks shows only the empty state with one + task primary: no column headings, no second button', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/mailgate?tab=tasks');
  assert.equal(all(page, '.task').length, 0);
  assert.match(textOf(all(page, '.nonideal').find((n) => shownNode(n))), /No tasks in this project yet/);
  assert.equal(all(page, '.col').filter(shownNode).length, 0, 'no (0) column headings above the hint');
  const plus = shownButtons(page, /\+\s*task/i);
  assert.equal(plus.length, 1, 'one + task on screen: the empty state\'s (the toolbar is hidden with the columns)');
  assert.ok(plus[0].classList.contains('bp5-intent-primary'), 'and it is the filled primary');
  assert.equal(shownButtons(page, /\+\s*session/i).filter((b) => b.classList.contains('bp5-intent-primary')).length, 0, 'the header + session is plain on this tab');
});

test('the header\'s filled primary follows the tab: + session on Sessions, the tab\'s own button on Tasks and Schedules, none on Files', async () => {
  const env = projectWorld();
  const primaries = (page) => shownButtons(page, /^\+ (session|task|schedule)$/).filter((b) => b.classList.contains('bp5-intent-primary')).map((b) => b.textContent.trim());
  const headBtns = (page) => all(page.querySelector('.pj-actions'), 'button').map((b) => b.textContent.trim());
  let page = await go(env, '#/p/phasezero');
  assert.deepEqual(headBtns(page), ['+ session', '+ task', '+ schedule'], 'three buttons, no Add repo, no code-server link');
  assert.deepEqual(primaries(page), ['+ session']);
  page = await go(env, '#/p/phasezero?tab=tasks');
  assert.deepEqual(headBtns(page), ['+ session', '+ schedule'], 'the tab\'s own + task is not repeated in the header');
  assert.deepEqual(primaries(page), ['+ task'], 'exactly one filled primary: the toolbar\'s');
  page = await go(env, '#/p/phasezero?tab=schedules');
  assert.deepEqual(headBtns(page), ['+ session', '+ task']);
  assert.deepEqual(primaries(page), ['+ schedule']);
  page = await go(env, '#/p/phasezero?tab=files');
  assert.deepEqual(primaries(page), [], 'the Files tab has no primary');
});

test('the repos are one scrolling row, and the repos panel keeps the folder\'s code-server link', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  assert.equal(all(page, '.pj-repos .pj-repo').length, 2, 'both repos in the one row');
  assert.equal(all(page, '.pj-actions a').length, 0, 'code-server is not an orphan beside the buttons');
  assert.ok(all(page.querySelector('.pj-manage'), 'a').some((a) => /folder=|code-server/.test((a.getAttribute('href') || '') + a.textContent)), 'the folder link lives in the repos and danger panel');
});

// ---------------------------------------------------------------- schedules tab

test('the schedules tab lists the project\'s jobs with next and last run, and the last three runs only', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero?tab=schedules');
  const text = textOf(page);
  assert.match(text, /Weekly changelog digest/);
  assert.doesNotMatch(text, /Nightly dependency audit|Prune stale worktrees|Draft the release notes/, 'jobs of other projects are not listed');
  assert.match(text, /0 9 \* \* 1/, 'the cron');
  assert.match(text, /next/i);
  const runs = text.match(/run #\d+/g) || [];
  assert.equal(runs.length, 3, 'three of the four runs');
  assert.ok(text.includes('run #11') && text.includes('run #12') && text.includes('run #13'), 'the newest three (the state lists them newest first)');
  assert.ok(!text.includes('run #14'));
});

test('+ schedule on the schedules tab opens the job sheet for this project; the cron presets are chips that fill the cron field', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero?tab=schedules');
  button(page, /\+\s*schedule/i).click();
  assert.equal(created(env).at(-1).kind, 'schedule');
  assert.equal(created(env).at(-1).pre.project, 'phasezero');
  // the form itself (jobForm), as the sheet shows it
  const p = env.state.projects.find((x) => x.name === 'phasezero');
  const form = env.w.get('jobForm')(p, p.repos[1]);
  const cron = all(form, 'input').find((i) => /cron/i.test(i.getAttribute('placeholder') || ''));
  assert.ok(cron, 'the cron field');
  const chip = (re) => byText(form, 'button', re);
  const presets = [[/nightly/i, '30 2 * * *'], [/weekdays/i, '0 9 * * 1-5'], [/hourly/i, '0 * * * *']];
  for (const [re, value] of presets) {
    const c = chip(re);
    assert.ok(c, `a ${re} chip`);
    c.click();
    assert.equal(cron.value, value, `${re} fills the cron field`);
  }
  chip(/one-?off/i).click();
  assert.equal(cron.value, '', 'one-off clears it: a blank cron runs once now');
  assert.match(textOf(form), /02:30/);
  assert.match(textOf(form), /09:00/);
});

test('the schedules tab offers the Batch prompt entry', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero?tab=schedules');
  const batch = byText(page, 'button, a', /batch prompt/i);
  assert.ok(batch, 'a Batch prompt entry');
  batch.click();
  const kinds = created(env).map((c) => c.kind);
  const sheetOpen = env.w.document.querySelector('#sheet').open === true;
  assert.ok(kinds.includes('batch') || sheetOpen, 'it opens the batch form');
});

test('a project with no jobs says what a schedule is, and + schedule is on screen once (the toolbar\'s, not repeated in the empty state)', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/mailgate?tab=schedules');
  assert.match(textOf(page), /no schedules|nothing scheduled|no jobs/i);
  const plus = shownButtons(page, /^\+\s*schedule$/);
  assert.equal(plus.length, 1, 'one + schedule');
  assert.ok(plus[0].classList.contains('bp5-intent-primary'));
  assert.equal(all(page, '.nonideal button').length, 0, 'the empty state carries no button of its own');
});

test('a job row: Run now is plain, Disable is quiet, Delete rests red-outlined (quiet danger) and only the armed Confirm is filled', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero?tab=schedules');
  const row = all(page, '.pj-job')[0];
  const btn = (re) => all(row, 'button').find((b) => re.test(b.textContent.trim()));
  const labels = all(row.querySelector('.actions'), 'button').map((b) => b.textContent.trim());
  assert.equal(labels[0], 'Run now');
  assert.match(labels[1], /^(Disable|Enable)$/);
  assert.equal(labels[2], 'Delete', 'Delete is last');
  assert.ok(!btn(/Run now/).classList.contains('bp5-intent-danger') && !btn(/Run now/).classList.contains('bp5-minimal'));
  assert.ok(btn(/Disable|Enable/).classList.contains('bp5-minimal'), 'Disable is quiet');
  const del = btn(/^Delete$/);
  assert.ok(del.classList.contains('bp5-intent-danger') && del.classList.contains('bp5-minimal'), 'Delete: danger, quiet');
  assert.ok(!del.classList.contains('confirm'));
  del.click();
  const armed = shownButtons(env.page(), /^Confirm Delete$/)[0];
  assert.ok(armed && armed.classList.contains('confirm'), 'the second tap is the one filled red button');
});

// ---------------------------------------------------------------- files tab

test('the files tab mounts the tree of the first repo, and ?path= opens that directory', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero?tab=files');
  assert.equal(all(page, '[role=tree]').length, 1);
  assert.ok(env.server.treePaths().includes('NestJs-Ecommerce-Backend|'), 'the first repo of the project');
  assert.deepEqual(visibleItems(page).map(labelOf).slice(0, 3), ['prisma', 'src', 'test']);
  const env2 = projectWorld();
  const page2 = await go(env2, '#/p/phasezero/website?tab=files&path=src/components');
  assert.ok(env2.server.treePaths().includes('website|'));
  assert.ok(byPath(page2, 'src/components/Header.tsx'), '?path= expanded the directory (and its parents)');
  assert.equal(byPath(page2, 'src').getAttribute('aria-expanded'), 'true');
});

test('the repo switcher lists the repos (and the project folder) and moves the tree to the chosen one', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero/website?tab=files');
  const sw = all(page, 'button, a, [role=tab], option').filter((n) => /^(website|NestJs-Ecommerce-Backend|project folder|root)$/i.test(n.textContent.trim()));
  const labels = sw.map((n) => n.textContent.trim());
  assert.ok(labels.includes('website') && labels.includes('NestJs-Ecommerce-Backend'), `a switcher with both repos, got ${labels}`);
  sw.find((n) => n.textContent.trim() === 'NestJs-Ecommerce-Backend').click();
  assert.match(env.w.location.hash, /^#\/p\/phasezero\/NestJs-Ecommerce-Backend\?tab=files/);
  await settle();
  assert.ok(env.server.treePaths().includes('NestJs-Ecommerce-Backend|'));
  assert.equal(env.mounts('project'), 1, 'a repo change is a route change within the page');
  assert.ok(byPath(env.page(), 'prisma'));
  assert.equal(byPath(env.page(), 'public'), null, 'the website tree is gone');
});

test('opening a file in the tree shows its preview, with a gutter and the code-server link', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero/website?tab=files');
  const item = byPath(page, 'package.json');
  item.focus();
  key(env.w, 'Enter');
  await settle();
  assert.ok(env.server.log.some((r) => r.kind === 'file' && r.repo === 'website' && r.path === 'package.json'), 'the file was requested');
  assert.match(textOf(env.page()), /phasezero-website/);
  assert.ok(all(env.page(), 'ol li').length >= 10, 'a line gutter');
  const link = all(env.page(), 'a').find((a) => (a.getAttribute('href') || '').includes('payload='));
  assert.ok(link, 'open in code-server');
  assert.ok(decodeURIComponent(link.getAttribute('href')).includes('vscode-remote:///srv/projects/phasezero/website/package.json'));
});

test('leaving the files tab destroys the tree: no more revalidation and no tree left behind', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero/website?tab=files');
  await go(env, '#/p/phasezero/website?tab=sessions');
  assert.equal(all(env.page(), '[role=tree]').length, 0);
  env.server.clear();
  await env.clock.advance(60000);
  assert.equal(env.server.tree().length, 0);
  await go(env, '#/usage');
  await go(env, '#/p/phasezero/website?tab=files');
  await go(env, '#/');
  env.server.clear();
  await env.clock.advance(60000);
  assert.equal(env.server.tree().length, 0, 'unmounting the page destroys it too');
});

test('every route and state in the files tab leaves exactly one tree', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero/website?tab=files');
  await go(env, '#/p/phasezero/website?tab=files&path=src');
  await go(env, '#/p/phasezero/website?tab=files&path=src/lib');
  env.w.run('updateCurrentPage(state)');
  await settle();
  assert.equal(all(env.page(), '[role=tree]').length, 1);
  assert.equal(treeItems(env.page()).filter((i) => i.getAttribute('tabindex') === '0').length, 1);
  assert.equal(env.mounts('project'), 1);
});


// ---------------------------------------------------------------- one repo in the address, tab counts

test('a repo in the address narrows every tab to it, says so, and offers all repos again', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero/website');
  assert.deepEqual(all(page, '.rrow').map((r) => r.getAttribute('data-tmux')), ['phasezero--website--t-checkout-redesign']);
  const note = page.querySelector('.pj-scope');
  assert.ok(note && !note.classList.contains('hidden'));
  assert.match(textOf(note), /Only website/);
  assert.equal(note.querySelector('a').getAttribute('href'), '#/p/phasezero', 'all repos');
  await go(env, '#/p/phasezero/website?tab=tasks');
  assert.deepEqual(all(env.page(), '.task').map((t) => t.getAttribute('data-task')), ['4'], 'only the website task');
  await go(env, '#/p/phasezero/NestJs-Ecommerce-Backend?tab=tasks');
  assert.deepEqual(all(env.page(), '.task').map((t) => t.getAttribute('data-task')), ['5']);
  await go(env, '#/p/phasezero/NestJs-Ecommerce-Backend?tab=schedules');
  assert.doesNotMatch(textOf(env.page()), /Weekly changelog digest/, 'that job belongs to website');
  await go(env, '#/p/phasezero/website?tab=schedules');
  assert.match(textOf(env.page()), /Weekly changelog digest/);
  await go(env, '#/p/phasezero?tab=tasks');
  assert.equal(all(env.page(), '.task').length, 2);
  assert.ok(env.page().querySelector('.pj-scope').classList.contains('hidden'), 'no note for the whole project');
});

test('the tabs carry counts of sessions, tasks and schedules (not files)', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  const count = (id) => { const t = all(page, '[role=tab]').find((x) => x.getAttribute('data-tab') === id); const c = t.querySelector('.tab-count'); return c ? c.textContent : null; };
  assert.deepEqual(['sessions', 'tasks', 'schedules', 'files'].map(count), ['3', '2', '1', null]);
  await go(env, '#/p/phasezero/website');
  assert.deepEqual(['sessions', 'tasks', 'schedules'].map(count), ['1', '1', '1'], 'narrowed to one repo');
});

test('the repo chips link to that repo\'s files tab', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero');
  const hrefs = all(page, 'a').map((a) => a.getAttribute('href'));
  assert.ok(hrefs.includes('#/p/phasezero/website?tab=files'));
  assert.ok(hrefs.includes('#/p/phasezero/NestJs-Ecommerce-Backend?tab=files'));
});

test('memory is a tab only when the state has it, and its view links to the memory page of the project', async () => {
  const env = projectWorld({ state: projectState({ memory: { enabled: true } }) });
  const page = await go(env, '#/p/phasezero?tab=memory');
  assert.deepEqual(selectedTab(page), ['memory']);
  assert.ok(all(page, 'a').some((a) => a.getAttribute('href') === '#/memory/phasezero'));
  const none = projectWorld();
  await go(none, '#/p/phasezero?tab=memory');
  assert.deepEqual(selectedTab(none.page()), ['sessions'], 'no memory in the state: the tab does not exist');
});

// ---------------------------------------------------------------- the files tab in detail

const treeOf = (env) => env.page().querySelector('[role=tree]');
const openItem = (env, p) => { const it = byPath(treeOf(env), p); it.focus(); it.dispatchEvent({ type: 'focusin' }); return it; };
const lastReplace = (env) => plain(env.w.history.calls).filter((c) => c.method === 'replaceState').at(-1);

test('the files toolbar names the branch of the repo (ahead and behind) and says when the folder is not a git repo', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero/website?tab=files');
  assert.match(textOf(page.querySelector('.pj-branchinfo')), /main/);
  assert.match(textOf(page.querySelector('.pj-branchinfo')), /↑1/);
  await go(env, '#/p/phasezero/root?tab=files');
  assert.match(textOf(env.page().querySelector('.pj-branchinfo')), /not a git repo/);
  assert.ok(byPath(treeOf(env), 'assets'), 'the project folder lists its directories');
  assert.ok(byPath(treeOf(env), 'NestJs-Ecommerce-Backend'), 'and its repos');
});

test('opening a file puts it in the address without a history entry, and a deep link to a file opens its folders, selects it and previews it', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero/website?tab=files');
  const item = openItem(env, 'package.json');
  key(env.w, 'Enter');
  await settle();
  assert.equal(lastReplace(env).args[2], '#/p/phasezero/website?tab=files&path=package.json', 'replaceState, not pushState');
  assert.equal(plain(env.w.history.calls).filter((c) => c.method === 'pushState').length, 0);
  assert.match(textOf(env.page()), /phasezero-website/);
  assert.equal(item.getAttribute('aria-selected'), 'true');
  // deep link in a fresh page
  const env2 = projectWorld();
  const page = await go(env2, '#/p/phasezero/website?tab=files&path=src/lib/api.ts');
  assert.equal(byPath(page, 'src').getAttribute('aria-expanded'), 'true');
  assert.equal(byPath(page, 'src/lib').getAttribute('aria-expanded'), 'true');
  assert.equal(byPath(page, 'src/lib/api.ts').getAttribute('aria-selected'), 'true');
  assert.ok(env2.server.log.some((r) => r.kind === 'file' && r.path === 'src/lib/api.ts'));
  assert.match(textOf(page), /NEXT_PUBLIC_API_URL/, 'the preview shows the file');
});

test('?path= on a directory opens it without a preview; on something that is not there it says so; the close button clears the file', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero/website?tab=files&path=src/components');
  assert.equal(byPath(page, 'src/components').getAttribute('aria-expanded'), 'true');
  assert.equal(env.server.log.filter((r) => r.kind === 'file').length, 0, 'a directory has no preview');
  await go(env, '#/p/phasezero/website?tab=files&path=nope/gone.txt');
  assert.match(textOf(env.page()), /nope\/gone\.txt is not in this repo/);
  await go(env, '#/p/phasezero/website?tab=files&path=package.json');
  assert.match(textOf(env.page()), /phasezero-website/);
  const close = env.page().querySelector('.pj-close');
  assert.ok(!close.classList.contains('hidden'), 'a close button while a file is open');
  close.click();
  assert.equal(lastReplace(env).args[2], '#/p/phasezero/website?tab=files', 'the file leaves the address');
  assert.doesNotMatch(textOf(env.page().querySelector('.pj-previewbox')), /phasezero-website/);
});

test('the hidden and ignored toggles remount the tree with the flags, and are remembered for every repo', async () => {
  const env = projectWorld();
  const page = await go(env, '#/p/phasezero/website?tab=files');
  const flag = (re) => shownButtons(env.page(), re)[0];
  assert.equal(flag(/^hidden$/).getAttribute('aria-pressed'), 'false');
  assert.equal(flag(/^ignored$/).getAttribute('aria-pressed'), 'false');
  env.server.clear();
  flag(/^hidden$/).click();
  await settle();
  assert.equal(env.server.tree().at(-1).hidden, '1', 'hidden=1 on the next request');
  assert.equal(env.server.tree().at(-1).ignored, '0');
  assert.equal(flag(/^hidden$/).getAttribute('aria-pressed'), 'true');
  flag(/^ignored$/).click();
  await settle();
  assert.equal(env.server.tree().at(-1).ignored, '1');
  assert.deepEqual(plain(JSON.parse(env.w.localStorage.getItem('ccboard:tree:flags'))), { hidden: true, ignored: true });
  assert.equal(env.page().querySelectorAll('[role=tree]').length, 1, 'one tree after the remounts');
  // another repo, and a new page, start with them on
  await go(env, '#/p/phasezero/NestJs-Ecommerce-Backend?tab=files');
  assert.equal(env.server.tree().at(-1).hidden, '1');
  assert.equal(shownButtons(env.page(), /^hidden$/)[0].getAttribute('aria-pressed'), 'true');
  void page;
});

test('Refresh reloads the tree and the open file', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero/website?tab=files&path=package.json');
  env.server.clear();
  shownButtons(env.page(), /Refresh/)[0].click();
  await settle();
  assert.ok(env.server.tree().length >= 1, 'the tree is read again');
  assert.ok(env.server.log.some((r) => r.kind === 'file' && r.path === 'package.json'), 'and so is the file');
});

test('the preview and the tree each keep their own scroll box: the open file survives a tab round trip as ?path=', async () => {
  const env = projectWorld();
  await go(env, '#/p/phasezero/website?tab=files&path=package.json');
  await go(env, '#/p/phasezero/website?tab=tasks');
  assert.equal(all(env.page(), '[role=tree]').length, 0);
  await go(env, '#/p/phasezero/website?tab=files&path=package.json');
  assert.equal(all(env.page(), '[role=tree]').length, 1);
  assert.match(textOf(env.page()), /phasezero-website/);
});

// ---------------------------------------------------------------- the sidebar (shell.js): repos and folders of the current project

const sideNode = (env, k, where = '#sidebar') => env.w.document.querySelector(where).querySelectorAll('.tn').find((n) => n.getAttribute('data-key') === k);
const sideKeys = (env, where = '#sidebar') => env.w.document.querySelector(where).querySelectorAll('.dirn').map((n) => n.getAttribute('data-key'));
const sideRow = (n) => n.querySelector('.d-row');

test('sidebar: the current project\'s repos and its folder are directory nodes; other projects keep plain repo links', async () => {
  const env = projectWorld({ shell: true });
  await go(env, '#/p/phasezero?tab=files');
  assert.deepEqual(sideKeys(env), ['d:phasezero/NestJs-Ecommerce-Backend/', 'd:phasezero/website/', 'd:phasezero/root/']);
  assert.equal(sideNode(env, 'p:phasezero').classList.contains('open'), true, 'the current project is open');
  assert.equal(textOf(sideRow(sideNode(env, 'd:phasezero/root/'))), 'project folder');
  assert.equal(sideRow(sideNode(env, 'd:phasezero/website/')).getAttribute('href'), '#/p/phasezero/website?tab=files');
  assert.equal(sideRow(sideNode(env, 'd:phasezero/root/')).getAttribute('href'), '#/p/phasezero/root?tab=files');
  assert.equal(env.w.document.querySelector('#sidebar').querySelectorAll('.dirn').filter((n) => n.getAttribute('data-key').startsWith('d:petroit/')).length, 0);
  await go(env, '#/p/ccboard');
  assert.deepEqual(sideKeys(env), ['d:ccboard/ccboard/'], 'a project whose one repo is the project has no separate folder node');
  await go(env, '#/');
  assert.deepEqual(sideKeys(env), [], 'off a project page: the plain links');
});

test('sidebar: the phone drawer holds the same tree', async () => {
  const env = projectWorld({ shell: true });
  await go(env, '#/p/phasezero');
  assert.deepEqual(sideKeys(env, '#drawer'), sideKeys(env));
  assert.equal(sideKeys(env, '#drawer').length, 3);
});

test('sidebar: the chevron of a repo node loads its folders, which link to the Files tab with their path and show a dot when they have changes', async () => {
  const env = projectWorld({ shell: true });
  await go(env, '#/p/phasezero');                                     // sessions tab: nothing but the sidebar reads the tree
  env.server.clear();
  const row = sideRow(sideNode(env, 'd:phasezero/website/'));
  assert.equal(row.getAttribute('aria-expanded'), 'false');
  row.querySelector('.tw').click();
  await settle();
  assert.deepEqual(env.server.treePaths(), ['website|'], 'one request, for the repo\'s top level');
  assert.equal(sideRow(sideNode(env, 'd:phasezero/website/')).getAttribute('aria-expanded'), 'true');
  const keys = sideKeys(env);
  for (const d of ['config', 'public', 'src', 'tests']) assert.ok(keys.includes(`d:phasezero/website/${d}`), d);
  assert.ok(!keys.some((k) => k.endsWith('/package.json')), 'only directories');
  const src = sideNode(env, 'd:phasezero/website/src');
  assert.equal(sideRow(src).getAttribute('href'), '#/p/phasezero/website?tab=files&path=src');
  assert.equal(sideRow(src).getAttribute('aria-level'), '3');
  const dirty = (d) => !sideRow(sideNode(env, `d:phasezero/website/${d}`)).querySelector('.tn-dot').classList.contains('hidden');
  assert.deepEqual(['config', 'public', 'src', 'tests'].map(dirty), [false, false, true, true]);
  // a folder opens one level further
  sideRow(src).querySelector('.tw').click();
  await settle();
  assert.deepEqual(env.server.treePaths().at(-1), 'website|src');
  assert.ok(sideKeys(env).includes('d:phasezero/website/src/lib'));
  assert.ok(sideKeys(env).includes('d:phasezero/website/src/components'));
});

test('sidebar: the chevron toggles without navigating', async () => {
  const env = projectWorld({ shell: true });
  await go(env, '#/p/phasezero');
  const hash = env.w.location.hash;
  const row = sideRow(sideNode(env, 'd:phasezero/website/'));
  let prevented = false;
  row.querySelector('.tw').dispatchEvent({ type: 'click', preventDefault() { prevented = true; }, stopPropagation() {} });
  await settle();
  assert.equal(prevented, true, 'the link\'s navigation is cancelled');
  assert.equal(env.w.location.hash, hash);
});

test('sidebar: a deep link opens the repo and the folders down to ?path= and marks the folder the Files tab shows', async () => {
  const env = projectWorld({ shell: true });
  await go(env, '#/p/phasezero/website?tab=files&path=src/lib');
  await settle();
  const open = (k) => sideRow(sideNode(env, k)).getAttribute('aria-expanded');
  assert.equal(open('d:phasezero/website/'), 'true');
  assert.equal(open('d:phasezero/website/src'), 'true');
  const lib = sideRow(sideNode(env, 'd:phasezero/website/src/lib'));
  assert.equal(lib.getAttribute('aria-selected'), 'true', 'the current folder is selected');
  assert.ok(lib.classList.contains('cur'));
  assert.equal(sideRow(sideNode(env, 'd:phasezero/website/src/components')).getAttribute('aria-selected'), null);
  assert.ok(env.w.get('Shell.open.has("d:phasezero/website/src")'));
  // moving the path within the page moves the mark
  await go(env, '#/p/phasezero/website?tab=files&path=src/components');
  await settle();
  assert.equal(sideRow(sideNode(env, 'd:phasezero/website/src/components')).getAttribute('aria-selected'), 'true');
  assert.equal(sideRow(sideNode(env, 'd:phasezero/website/src/lib')).getAttribute('aria-selected'), null);
});

test('sidebar: a nested repo of the project folder is a repo node of its own, and the folder lists the other directories', async () => {
  const env = projectWorld({ shell: true });
  await go(env, '#/p/phasezero/root?tab=files');
  await settle();
  const keys = sideKeys(env);
  assert.ok(keys.includes('d:phasezero/root/assets') && keys.includes('d:phasezero/root/docs'), 'plain directories of the folder');
  assert.equal(keys.filter((k) => /NestJs-Ecommerce-Backend\/$/.test(k)).length, 1, 'the repos are top-level nodes already: not listed twice');
});

test('sidebar: arrow keys and Enter work on directory nodes (Enter follows the link)', async () => {
  const env = projectWorld({ shell: true });
  await go(env, '#/p/phasezero');
  const row = sideRow(sideNode(env, 'd:phasezero/website/'));
  row.focus();
  let clicked = false;
  row.addEventListener('click', () => { clicked = true; });
  const ev = { type: 'keydown', key: 'Enter', preventDefault() {}, stopPropagation() {} };
  row.dispatchEvent(ev);
  assert.equal(clicked, true);
  const right = { type: 'keydown', key: 'ArrowRight', preventDefault() {}, stopPropagation() {} };
  env.server.clear();
  row.dispatchEvent(right);
  await settle();
  assert.equal(sideRow(sideNode(env, 'd:phasezero/website/')).getAttribute('aria-expanded'), 'true', 'Right opens a closed directory node');
  assert.deepEqual(env.server.treePaths(), ['website|']);
});

void EPOCH; void TREE_FIXTURE; void focused; void pathOf;
