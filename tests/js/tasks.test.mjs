// Contract tests for the v0.5.14a backlog: the card (components.js taskCard for phase backlog|queued), its four actions (Start, Send to session,
// Edit, Delete), the optimistic move through store.tasksOverride, the 'in <session>' chip on a task that runs in an existing session, and the
// project page that draws it all. Also the 10x pass counted in clicks: from a project page to a running task is + task, type, Start task
// (3 actions), from a backlog card to a running session is 1 tap (Start).
// The real scripts in index.html order on minidom's DOM; api() is the fake of tests/js/treekit.mjs (every call lands in __calls, answers come from
// server.answers), toast() and poll() are recorders, navigate() records the hash it was asked for. poll() NEVER repaints here: whatever the board
// shows after a click it has to show without the poll (the 10x pass: never wait on the poll).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { fixtureState, sess } from './world.mjs';
import { makeNetworkWorld, settle } from './treekit.mjs';

const PAGE_FILES = ['home', 'inbox', 'tasks', 'project', 'agents', 'settings', 'search', 'session', 'placeholders'];

/** makeNetworkWorld's FakeDate starts here (treekit.mjs): ages in the page are measured from it, not from the host clock. */
const T0 = 1790000000000;
const agoIso = (min) => new Date(T0 - min * 60000).toISOString();
const textOf = (n) => (n ? n.textContent : '');
const all = (root, sel) => root.querySelectorAll(sel);
const byText = (root, sel, re) => all(root, sel).find((n) => re.test(n.textContent.trim()));
const button = (root, re) => byText(root, 'button', re);

/** A state.tasks row the way _tasks_view makes it (every key), `over` on top. */
function row(id, over = {}) {
  return {
    ci: null, pr: null, id, project: 'petroit', repo: 'api', slug: `task-${id}`, title: `Task ${id}`, branch: '', base: 'main', worktree: '', tmux: '',
    claude_session_id: null, pr_url: null, pr_number: null, pr_state: null, cost_usd: null, overlap: [], preview_port: null, preview_https: null,
    preview_url: null, created_at: agoIso(90), column: 'backlog', session: null, agent: 'claude', mode: 'worktree',
    auto_close: false, parent_id: null, chain_id: null, result: null, phase: 'backlog', session_row: null,
    prompt: 'Make the pagination consistent\nuse the cursor style everywhere', prompt_len: 62, ...over,
  };
}

const BACKLOG = (over = {}) => row(20, { title: 'Unify the devices pagination', ...over });

/** petroit/api has s1 (done), s2 (errored), s3 (waiting at its idle prompt, wait_kind idle_prompt): + s4 (working) and s5 (waiting on a permission); phasezero/NestJs has an idle s9. */
function stateWith(tasks, extra = {}) {
  const st = fixtureState({ tasks, ...extra });
  const petroit = st.projects.find((p) => p.name === 'petroit');
  const api = petroit.repos.find((r) => r.name === 'api');
  api.sessions.push(sess('petroit', 'api', 's4', { state: 'working', last_prompt: 'migrate the devices table' }),
    sess('petroit', 'api', 's5', { state: 'waiting', needs_attention: true, wait_kind: 'permission_prompt', last_prompt: 'run the seed script' }));
  api.sessions.find((x) => x.name === 's3').wait_kind = 'idle_prompt';        // waiting at its prompt: the fixture's "which one should GET /devices expose?"
  st.pending_permissions.push({ id: 31, tmux_name: 'petroit--api--s5', tool_name: 'Bash', summary: 'Bash: npm run seed', created_at: new Date().toISOString() });
  const nest = st.projects.find((p) => p.name === 'phasezero').repos.find((r) => r.name === 'NestJs-Ecommerce-Backend');
  nest.sessions.push(sess('phasezero', 'NestJs-Ecommerce-Backend', 's9', { state: 'idle', last_prompt: 'review the stock service' }));
  return st;
}

/**
 * A world with every page, the shell's create sheet and the fake network. `answers` are api path prefixes (a value, or a function of the request
 * that may throw). `confirm` answers window.confirm and records the messages in __confirms. poll() and navigate() only record.
 */
function tasksWorld({ state = stateWith([BACKLOG()]), answers = {}, confirm = () => true, keymap = false } = {}) {
  const env = makeNetworkWorld({ extra: { matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }), confirm: (m) => { env.w.ctx.__confirms.push(String(m)); return confirm(m); } } });
  const { w, server } = env;
  Object.assign(server.answers, answers);
  for (const f of ['live.js', 'launcher.js', 'tree.js', 'router.js', 'pages/widgets.js', 'shell.js', ...(keymap ? ['keymap.js'] : [])]) w.load(f);
  w.ctx.__toasts = []; w.ctx.__nav = []; w.ctx.__polls = 0; w.ctx.__confirms = []; w.ctx.__live = { subscribed: [] };
  w.run(`
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    poll = async () => { __polls++; };
    navigate = (hash, o) => { __nav.push(hash); };
    openPage = (url) => { __nav.push('page:' + url); };
    Live.subscribe = (tmux, fn) => { __live.subscribed.push(tmux); return () => {}; };
    Live.unsubscribe = () => {};
  `);
  for (const f of PAGE_FILES) if (fs.existsSync(path.join(STATIC, 'pages', `${f}.js`))) w.load(`pages/${f}.js`);
  w.ctx.__st = state;
  w.run('state = __st');
  w.run('globalThis.mkErr = (m, s, d) => { const e = new Error(m); e.status = s; e.data = d; e.body = d; e.mismatch = d && d.mismatch; return e; };');
  return { ...env, state, page: () => w.document.querySelector('#page'), sheet: () => w.document.querySelector('#sheet') };
}

const go = async (env, hash) => { env.w.location.hash = hash; await settle(); return env.page(); };
const calls = (env) => plain(env.w.get('__calls'));
const posts = (env, re) => calls(env).filter((c) => c.method === 'POST' && re.test(c.path));
const toasts = (env) => plain(env.w.get('__toasts'));
const navs = (env) => plain(env.w.get('__nav'));
const card = (env, id) => all(env.page(), '.task').find((t) => t.getAttribute('data-task') === String(id));
const colOf = (n) => { const c = n && n.closest('.col'); return c ? c.querySelector('h3').textContent.replace(/\s*\(\d+\)\s*$/, '') : null; };
const colTitles = (env) => all(env.page(), '.col h3').map((h) => h.textContent.replace(/\s*\(\d+\)\s*$/, ''));
const cardsIn = (env, label) => { const c = all(env.page(), '.col').find((x) => x.querySelector('h3').textContent.startsWith(label)); return c ? all(c, '.task').map((t) => Number(t.getAttribute('data-task'))) : []; };
const startBtn = (n) => button(n, /^Start$/);
const sendBtn = (n) => button(n, /^Send to/i);
const editBtn = (n) => button(n, /^Edit/i);
const deleteBtn = (n) => button(n, /^Delete$/);
const setState = (env, st) => { env.w.ctx.__st = st; env.w.run('state = __st; updateCurrentPage(state)'); };
const sessionRows = (sh) => { const a = all(sh, '[data-tmux]'); return a.length ? a : all(sh, 'button').filter((b) => b.getAttribute('aria-label') !== 'Close'); };
const rowFor = (sh, name) => sessionRows(sh).find((r) => r.getAttribute('data-tmux') === name || (r.getAttribute('data-tmux') || '').endsWith(`--${name}`) || (!r.getAttribute('data-tmux') && new RegExp(`(^|[^\\w-])${name}([^\\w]|$)`).test(textOf(r))));
const DISPATCHED = (id, tmux, rowId = 90) => ({ id, phase: 'running', tmux, session_row: rowId, attach_url: `/term/${tmux}` });
const PETROIT = '#/p/petroit?tab=tasks';

// ---------------------------------------------------------------- the card

test('a backlog card: title, the head of the prompt, the repo and its age; Start, Send to session, Edit, Delete; no terminal, no PR, no branch', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  const c = card(env, 20);
  assert.ok(c, 'the card is on the project page');
  assert.equal(colOf(c), 'Backlog');
  const t = textOf(c);
  assert.match(t, /Unify the devices pagination/);
  assert.match(t, /Make the pagination consistent/, 'the prompt head');
  assert.match(t, /petroit\/api/, 'the target repo');
  assert.match(t, /added 1h/, 'when it was added: 90 minutes before the fake clock');
  for (const [name, b] of [['Start', startBtn(c)], ['Send to session', sendBtn(c)], ['Edit', editBtn(c)], ['Delete', deleteBtn(c)]]) assert.ok(b, `a ${name} button`);
  assert.equal(all(c, 'a').filter((a) => /\/term\//.test(a.getAttribute('href') || '')).length, 0, 'there is no terminal before there is a session');
  assert.ok(!button(c, /Archive|Diff \/ PR|Preview|Fix CI/), 'none of the started-task buttons');
  assert.doesNotMatch(t, /undefined|null|no session|worktree-/, 'no branch, no placeholder text');
});

test('a queued card (waiting for a chain step) sits in the Backlog too: it can be edited or deleted but not started, the server would refuse', async () => {
  const env = tasksWorld({ state: stateWith([row(21, { phase: 'queued', title: 'Queued one' })]) });
  await go(env, PETROIT);
  const c = card(env, 21);
  assert.equal(colOf(c), 'Backlog');
  assert.match(textOf(c), /waiting for a step/);
  assert.ok(editBtn(c) && deleteBtn(c), 'Edit and Delete work on a queued task');
  assert.ok(!startBtn(c) && !sendBtn(c), 'dispatching a queued task answers 409: this task is queued behind another step');
});

test('the Backlog column comes first and says what to do when it is empty', async () => {
  const env = tasksWorld({ state: stateWith([row(5, { phase: 'running', column: 'in_progress', tmux: 'petroit--api--s4', title: 'Running one', prompt: null, session: { state: 'working', state_at: new Date().toISOString(), last_message: '', needs_attention: false, command: 'claude' } })]) });
  await go(env, PETROIT);
  assert.equal(colTitles(env)[0], 'Backlog');
  assert.match(textOf(all(env.page(), '.col').find((c) => /^Backlog/.test(c.querySelector('h3').textContent))), /nothing queued|\+ task|add/i);
  assert.ok(button(env.page(), /\+\s*task/i), 'with the + task button one tap away');
});

// ---------------------------------------------------------------- Start

test('Start POSTs dispatch {mode: "lane"}, toasts, opens the peek of the new session, and the card is in In progress before any poll', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': DISPATCHED(20, 'petroit--api--t-unify-the-devices-pagination') } });
  await go(env, PETROIT);
  startBtn(card(env, 20)).click();
  await settle();
  const p = posts(env, /\/api\/tasks\/20\/dispatch$/);
  assert.equal(p.length, 1, 'one request');
  assert.deepEqual(p[0].body, { mode: 'lane' });
  assert.match(toasts(env).pop().text, /started/i);
  assert.deepEqual(navs(env), ['#/s/petroit--api--t-unify-the-devices-pagination'], 'to the session it just started');
  assert.equal(env.w.get('__polls') >= 0, true);
  const c = card(env, 20);
  assert.ok(c, 'the card is still on the board');
  assert.equal(colOf(c), 'In progress', 'it moved without waiting for the poll');
  assert.deepEqual(cardsIn(env, 'Backlog'), [], 'and left the Backlog');
  assert.ok(!startBtn(c) && !sendBtn(c) && !editBtn(c), 'a started card has no backlog actions');
});

test('Start with an answer that has no session (demo mode) neither navigates to #/s/undefined nor throws', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': { ok: true } } });
  await go(env, PETROIT);
  startBtn(card(env, 20)).click();
  await settle();
  assert.ok(!navs(env).some((h) => /undefined|null/.test(h)), `navigated to ${navs(env)}`);
  assert.ok(!toasts(env).some((t) => t.kind === 'bad'), 'no error');
});

test('a failed Start keeps the card in the Backlog and says why', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': () => { throw env.w.get('mkErr')('claude is not installed on this box', 400); } } });
  await go(env, PETROIT);
  startBtn(card(env, 20)).click();
  await settle();
  assert.equal(colOf(card(env, 20)), 'Backlog', 'nothing moved');
  assert.deepEqual(navs(env), []);
  const said = [...toasts(env).map((t) => t.text), env.w.get('ui.error') || '', textOf(env.w.document.querySelector('#banner'))].join(' | ');
  assert.match(said, /claude is not installed on this box/, 'the reason reaches the screen, not only the console');
});

test('Start twice before the answer posts once', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': DISPATCHED(20, 'petroit--api--t-x') } });
  await go(env, PETROIT);
  const b = startBtn(card(env, 20));
  b.click();
  b.click();
  await settle();
  assert.equal(posts(env, /\/api\/tasks\/20\/dispatch$/).length, 1);
});

// ---------------------------------------------------------------- Send to session

test('Send to session lists the project\'s live sessions, ready ones (idle, done, waiting without a permission) before busy ones', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  const sh = env.sheet();
  assert.equal(sh.open, true);
  assert.match(textOf(sh), /Unify the devices pagination/, 'the sheet says which task is being sent');
  const names = ['s1', 's3', 's2', 's4', 's5'];
  const rows = Object.fromEntries(names.map((n) => [n, rowFor(sh, n)]));
  for (const n of names) assert.ok(rows[n], `a row for ${n}`);
  const order = sessionRows(sh);
  const idx = (n) => order.indexOf(rows[n]);
  for (const ready of ['s1', 's3']) for (const busy of ['s2', 's4', 's5']) assert.ok(idx(ready) < idx(busy), `${ready} (ready) is listed before ${busy}`);
  assert.match(textOf(rows.s1), /fix the login bug/, 'the last prompt tells which session is which');
  assert.match(textOf(rows.s4), /migrate the devices table/);
  assert.equal(all(sh, '[data-tmux]').filter((r) => !/^petroit--/.test(r.getAttribute('data-tmux'))).length, 0, 'only this project\'s sessions');
});

test('picking a ready session POSTs dispatch {session}, closes the sheet, toasts, and the card is In progress with an "in <session>" chip', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': { id: 20, phase: 'running', tmux: 'petroit--api--s1', session_row: 4, pasted: true } } });
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  rowFor(env.sheet(), 's1').click();
  await settle();
  const p = posts(env, /\/api\/tasks\/20\/dispatch$/);
  assert.equal(p.length, 1);
  assert.deepEqual(p[0].body, { session: 'petroit--api--s1' });
  assert.equal(env.sheet().open, false, 'the sheet closes');
  assert.ok(toasts(env).length >= 1 && !toasts(env).some((t) => t.kind === 'bad'));
  assert.deepEqual(navs(env), [], 'no navigation: the session was already running and the user stays on the board (the chip opens it)');
  const c = card(env, 20);
  assert.equal(colOf(c), 'In progress');
  const chip = all(c, 'a').find((a) => /^in\s/.test(textOf(a).trim()));
  assert.ok(chip, 'the in <session> chip');
  assert.equal(chip.getAttribute('href'), '#/s/petroit--api--s1');
  assert.match(textOf(chip), /s1/);
});

test('busy sessions are listed but cannot be picked, with the reason; ready ones can', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  const sh = env.sheet();
  const off = (n) => rowFor(sh, n).getAttribute('disabled') !== null;
  assert.deepEqual(['s1', 's3'].map(off), [false, false], 'done and waiting-for-an-answer are ready');
  assert.deepEqual(['s2', 's4', 's5'].map(off), [true, true, true], 'errored, working and waiting-on-a-permission are not');
  assert.match(textOf(rowFor(sh, 's4')), /working/i);
  assert.match(textOf(rowFor(sh, 's5')), /permission/i);
});

test('a waiting session is sendable only at its idle prompt: a permission prompt or dialog with no pending row is off, "waiting on a prompt"', async () => {
  const st = stateWith([BACKLOG()]);
  const api = st.projects.find((p) => p.name === 'petroit').repos.find((r) => r.name === 'api');
  api.sessions.push(sess('petroit', 'api', 's6', { state: 'waiting', needs_attention: true, wait_kind: 'permission_prompt', last_prompt: 'dialog without a permission row' }),
    sess('petroit', 'api', 's7', { state: 'waiting', needs_attention: true, wait_kind: 'elicitation_dialog' }),
    sess('petroit', 'api', 's8', { state: 'waiting', needs_attention: true }));             // no wait_kind (an older server, or no event on record): fail closed
  const env = tasksWorld({ state: st });
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  const sh = env.sheet();
  const off = (n) => rowFor(sh, n).getAttribute('disabled') !== null;
  assert.deepEqual(['s1', 's3'].map(off), [false, false], 'done and waiting at the idle prompt are ready');
  assert.deepEqual(['s6', 's7', 's8'].map(off), [true, true, true], 'waiting on a dialog, or on nothing the board knows, is not');
  for (const n of ['s6', 's7', 's8']) assert.match(textOf(rowFor(sh, n)), /waiting on a prompt/i, `${n} says why`);
  assert.match(textOf(rowFor(sh, 's5')), /permission/i, 'a pending permission keeps its own reason');
  assert.equal(posts(env, /dispatch$/).length, 0, 'nothing was sent');                // (minidom still fires click on a disabled button; a browser does not, so the disabled attribute is the pin)
  const alone = stateWith([BACKLOG()]);
  const only = alone.projects.find((p) => p.name === 'petroit').repos.find((r) => r.name === 'api');
  only.sessions = only.sessions.filter((x) => x.name === 's3');
  only.sessions[0].wait_kind = 'permission_prompt';
  const env2 = tasksWorld({ state: alone });
  await go(env2, PETROIT);
  assert.ok(!button(card(env2, 20), /^→/), 'no one-tap button to a session that sits in a dialog');
});

test('Send to session is never a dead end: with no ready session the sheet offers Start in a new session; a session that has just started is listed as "starting…"', async () => {
  const st = stateWith([BACKLOG()]);
  const api = st.projects.find((p) => p.name === 'petroit').repos.find((r) => r.name === 'api');
  for (const x of api.sessions) x.state = 'working';
  api.sessions.push(sess('petroit', 'api', 's6', { state: 'unknown' }));
  const env = tasksWorld({ state: st, answers: { '/api/tasks/20/dispatch': { id: 20, phase: 'running', tmux: 'petroit--api--t-unify', session_row: 9, attach_url: '/term/petroit--api--t-unify' } } });
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  const sh = env.sheet();
  const rows = all(sh, '[data-tmux]');
  assert.ok(rows.length >= 6 && rows.every((r) => r.getAttribute('disabled') !== null), 'every session is listed and off: all are busy');
  assert.match(textOf(rowFor(sh, 's6')), /starting…/, 'a session whose first hook has not arrived is shown, not dropped');
  const start = button(sh, /Start in a new session/);
  assert.ok(start, 'the way out: start the task in a session of its own');
  start.click();
  await settle();
  assert.deepEqual(posts(env, /dispatch$/).map((c) => c.body), [{ mode: 'lane' }]);
  assert.equal(env.sheet().open, false, 'the sheet closes');
  // with a ready session in the list the sheet stays a plain list: the card's own Start is the other way
  const env2 = tasksWorld();
  await go(env2, PETROIT);
  sendBtn(card(env2, 20)).click();
  assert.ok(!button(env2.sheet(), /Start in a new session/), 'no second Start when a session can take it');
});

test('with exactly one ready session in the task\'s own repo the card has a one-tap button that sends the prompt there', async () => {
  const task = row(28, { project: 'phasezero', repo: 'NestJs-Ecommerce-Backend', title: 'Add an index on stock.variant_id' });
  const env = tasksWorld({ state: stateWith([task]), answers: { '/api/tasks/28/dispatch': { id: 28, phase: 'running', tmux: 'phasezero--NestJs-Ecommerce-Backend--s9', session_row: 12, pasted: true } } });
  await go(env, '#/p/phasezero?tab=tasks');
  const quick = button(card(env, 28), /^→\s*s9$/);
  assert.ok(quick, 'a → s9 button beside Start (stock-sync is working, so s9 is the only ready one)');
  quick.click();
  await settle();
  assert.deepEqual(posts(env, /dispatch$/).map((c) => c.body), [{ session: 'phasezero--NestJs-Ecommerce-Backend--s9' }]);
  assert.equal(colOf(card(env, 28)), 'In progress');
  const other = tasksWorld({ state: stateWith([BACKLOG()]) });                                // petroit/api has two ready sessions: no guess, only Send to session
  await go(other, PETROIT);
  assert.ok(!button(card(other, 20), /^→/), 'no quick button when the choice is not obvious');
});

test('a 409 mismatch asks "work there anyway?" and retries with force; declining sends nothing more', async () => {
  const mismatch = { error: 'session is in another repo', mismatch: { task: 'phasezero/website', session: 'phasezero/NestJs-Ecommerce-Backend' } };
  const answer = (req) => {
    if (req.body && req.body.force) return { id: 22, phase: 'running', tmux: 'phasezero--NestJs-Ecommerce-Backend--s9', session_row: 77, pasted: true };
    throw env.w.get('mkErr')(mismatch.error, 409, mismatch);
  };
  const task = row(22, { project: 'phasezero', repo: 'website', title: 'Alt text for the gallery' });
  let decide = false;
  const env = tasksWorld({ state: stateWith([task]), answers: { '/api/tasks/22/dispatch': answer }, confirm: () => decide });
  await go(env, '#/p/phasezero?tab=tasks');
  sendBtn(card(env, 22)).click();
  rowFor(env.sheet(), 's9').click();
  await settle();
  assert.deepEqual(posts(env, /dispatch$/).map((c) => c.body), [{ session: 'phasezero--NestJs-Ecommerce-Backend--s9' }], 'the first try is without force');
  assert.equal(env.w.get('__confirms').length, 1, 'asked once');
  assert.match(env.w.get('__confirms')[0], /work there anyway/i);
  assert.equal(posts(env, /dispatch$/).length, 1, 'declined: no retry');
  assert.equal(colOf(card(env, 22)), 'Backlog', 'and the card stays where it was');
  decide = true;
  const sh = env.sheet();
  if (!sh.open) { sendBtn(card(env, 22)).click(); }
  rowFor(env.sheet(), 's9').click();
  await settle();
  assert.deepEqual(posts(env, /dispatch$/).map((c) => c.body).at(-1), { session: 'phasezero--NestJs-Ecommerce-Backend--s9', force: true }, 'accepted: the same session with force');
  assert.equal(colOf(card(env, 22)), 'In progress');
});

test('a 409 because the session is busy shows the reason and keeps the card in the Backlog', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': () => { throw env.w.get('mkErr')('session is working; wait for it to finish', 409, { error: 'session is working; wait for it to finish', state: 'working' }); } } });
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  rowFor(env.sheet(), 's1').click();
  await settle();
  assert.equal(env.w.get('__confirms').length, 0, 'no force prompt: that is for a repo mismatch only');
  assert.equal(posts(env, /dispatch$/).length, 1, 'and no retry');
  assert.equal(colOf(card(env, 20)), 'Backlog');
  const said = [...toasts(env).map((t) => t.text), env.w.get('ui.error') || '', textOf(env.sheet()), textOf(env.w.document.querySelector('#banner'))].join(' | ');
  assert.match(said, /session is working/);
});

test('a project with no live session still has a next step in the sheet', async () => {
  const st = stateWith([row(23, { project: 'mailgate', repo: 'mailgate', title: 'Lonely task' })]);
  const env = tasksWorld({ state: st });
  await go(env, '#/p/mailgate?tab=tasks');
  sendBtn(card(env, 23)).click();
  assert.equal(env.sheet().open, true);
  assert.match(textOf(env.sheet()), /no (live |running )?sessions?/i);
  assert.ok(button(env.sheet(), /start|new session|close|cancel/i), 'something to press');
  assert.equal(posts(env, /dispatch$/).length, 0);
});

// ---------------------------------------------------------------- Edit and Delete

test('Edit opens a sheet with the title and prompt, saving PATCHes them and the card shows the new title at once', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  editBtn(card(env, 20)).click();
  const sh = env.sheet();
  assert.equal(sh.open, true);
  const title = all(sh, 'input').find((i) => (i.value || i.getAttribute('value')) === 'Unify the devices pagination');
  const prompt = all(sh, 'textarea').find((t) => /Make the pagination consistent/.test(t.value));
  assert.ok(title && prompt, 'both fields are prefilled');
  title.value = 'Unify pagination everywhere';
  prompt.value = 'Use cursor pagination on every list endpoint';
  all(sh, 'form')[0].dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  const p = calls(env).filter((c) => c.method === 'PATCH');
  assert.equal(p.length, 1);
  assert.equal(p[0].path, '/api/tasks/20');
  assert.deepEqual(p[0].body, { title: 'Unify pagination everywhere', prompt: 'Use cursor pagination on every list endpoint' });
  assert.equal(env.sheet().open, false);
  assert.match(textOf(card(env, 20)), /Unify pagination everywhere/, 'the card is rewritten without the poll');
  assert.match(textOf(card(env, 20)), /cursor pagination on every list endpoint/);
});

test('Edit with a prompt the poll only carried the head of loads the whole one first (prompt_len says it is longer)', async () => {
  const full = 'A'.repeat(600) + '\nTHE TAIL THAT THE POLL DID NOT CARRY';
  const env = tasksWorld({ state: stateWith([BACKLOG({ prompt: 'A'.repeat(600), prompt_len: full.length })]), answers: { '/api/tasks/20': (req) => (req.method === 'GET' ? { id: 20, prompt: full, title: 'Unify the devices pagination' } : { ok: true }) } });
  await go(env, PETROIT);
  editBtn(card(env, 20)).click();
  await settle();
  const prompt = all(env.sheet(), 'textarea').find((t) => /^A{50}/.test(t.value));
  assert.ok(prompt, 'a prompt box');
  assert.match(prompt.value, /THE TAIL THAT THE POLL DID NOT CARRY$/, 'saving the head would have cut the prompt');
  assert.ok(calls(env).some((c) => c.method === 'GET' && c.path === '/api/tasks/20'));
});

test('Edit with an empty title or prompt PATCHes nothing', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  editBtn(card(env, 20)).click();
  const sh = env.sheet();
  all(sh, 'input').find((i) => (i.value || i.getAttribute('value')) === 'Unify the devices pagination').value = '   ';
  all(sh, 'form')[0].dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.equal(calls(env).filter((c) => c.method === 'PATCH').length, 0, 'a blank title is never sent (the old title stays)');
  assert.equal(sh.open, true, 'the sheet stays open: closing it would look like a save');
  const say = all(sh, '.form-status').find((n) => /title is required/i.test(textOf(n)));
  assert.ok(say, 'the inline status says what is missing');
  assert.ok(say.classList.contains('bad'), 'as an error');
  assert.deepEqual(toasts(env).filter((t) => /saved/.test(t.text)), [], 'and nothing toasts "saved"');
  all(sh, 'input').find((i) => i.value === '   ').value = 'A better title';              // typing a title and saving again goes through
  all(sh, 'form')[0].dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.deepEqual(calls(env).filter((c) => c.method === 'PATCH').map((c) => c.body), [{ title: 'A better title' }]);
  assert.equal(sh.open, false);
});

test('Delete is two taps (Delete, then Confirm Delete) and the card goes at once', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  deleteBtn(card(env, 20)).click();
  assert.equal(calls(env).filter((c) => c.method === 'DELETE').length, 0, 'the first tap only asks');
  const confirm = button(env.page(), /^Confirm Delete$/);
  assert.ok(confirm, 'Confirm Delete appears');
  assert.ok(button(env.page(), /^Cancel$/), 'with a Cancel');
  confirm.click();
  await settle();
  const d = calls(env).filter((c) => c.method === 'DELETE');
  assert.equal(d.length, 1);
  assert.equal(d[0].path, '/api/tasks/20');
  assert.equal(card(env, 20), undefined, 'gone without waiting for the poll');
});

test('Cancel after the first Delete tap puts the button back and deletes nothing', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  deleteBtn(card(env, 20)).click();
  button(env.page(), /^Cancel$/).click();
  assert.ok(deleteBtn(card(env, 20)));
  assert.equal(calls(env).filter((c) => c.method === 'DELETE').length, 0);
});

test('a failed Delete brings the card back and says why (a started task cannot be deleted: archive it)', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20': (req) => { if (req.method === 'DELETE') throw env.w.get('mkErr')('archive a started task instead', 409); return { ok: true }; } } });
  await go(env, PETROIT);
  deleteBtn(card(env, 20)).click();
  button(env.page(), /^Confirm Delete$/).click();
  await settle();
  assert.ok(card(env, 20), 'the card is back');
  const said = [...toasts(env).map((t) => t.text), env.w.get('ui.error') || ''].join(' | ');
  assert.match(said, /archive a started task instead/);
});

// ---------------------------------------------------------------- the optimistic override and the poll

test('after Start the poll that still says backlog does not put the card back; the poll that has the new tmux clears the override', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': DISPATCHED(20, 'petroit--api--t-unify-the-devices-pagination', 91) } });
  await go(env, PETROIT);
  startBtn(card(env, 20)).click();
  await settle();
  assert.equal(colOf(card(env, 20)), 'In progress');
  assert.ok(env.w.get('Object.keys(store.tasksOverride)').includes('20'), 'the override is held');
  setState(env, stateWith([BACKLOG()]));                                              // a poll that was in flight before the click answers: still backlog, no tmux
  assert.equal(colOf(card(env, 20)), 'In progress', 'a stale poll must not flip the card back');
  assert.ok(env.w.get('Object.keys(store.tasksOverride)').includes('20'), 'and the override stays');
  const live = BACKLOG({ phase: 'running', column: 'in_progress', tmux: 'petroit--api--t-unify-the-devices-pagination', session_row: 91, branch: 'worktree-unify', prompt: null,
    session: { state: 'working', state_at: new Date().toISOString(), last_message: '', needs_attention: false, command: 'claude' } });
  setState(env, stateWith([live]));
  assert.equal(colOf(card(env, 20)), 'In progress');
  assert.deepEqual(plain(env.w.get('Object.keys(store.tasksOverride)')), [], 'the poll caught up: the override is dropped');
  setState(env, stateWith([{ ...live, column: 'needs_you', session: { ...live.session, state: 'waiting', needs_attention: true } }]));
  assert.equal(colOf(card(env, 20)), 'Needs you', 'from here on the poll decides');
});

test('an override never outlives its task: a poll without the task paints nothing, and the override is dropped once it is stale', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': DISPATCHED(20, 'petroit--api--t-x') } });
  await go(env, PETROIT);
  startBtn(card(env, 20)).click();
  await settle();
  setState(env, stateWith([]));
  assert.equal(card(env, 20), undefined, 'a task the poll no longer has is not painted back');
  await env.clock.advance(60000);
  setState(env, stateWith([]));
  assert.deepEqual(plain(env.w.get('Object.keys(store.tasksOverride)')), [], 'and memory holds nothing for it a little later');
});

// ---------------------------------------------------------------- the 'in <session>' chip

test('a task that runs in an existing session shows in In progress with an "in <session>" chip to its peek; a worktree task has none', async () => {
  const handed = row(24, { title: 'Review the stock service', phase: 'running', mode: 'session', column: 'in_progress', tmux: 'petroit--api--s4', session_row: 5, prompt: null, prompt_len: 80,
    session: { state: 'working', state_at: new Date().toISOString(), last_message: 'looking at the reconcile function', needs_attention: false, command: 'claude' } });
  const own = row(25, { title: 'Has its own worktree', phase: 'running', mode: 'worktree', column: 'in_progress', tmux: 'petroit--api--t-own', branch: 'worktree-own', prompt: null, prompt_len: 50,
    session: { state: 'working', state_at: new Date().toISOString(), last_message: '', needs_attention: false, command: 'claude' } });
  const env = tasksWorld({ state: stateWith([own, handed]) });
  await go(env, PETROIT);
  assert.deepEqual(cardsIn(env, 'In progress').sort(), [24, 25]);
  const chip = all(card(env, 24), 'a').find((a) => /^in\s/.test(textOf(a).trim()));
  assert.ok(chip, 'the chip is on the handed card');
  assert.equal(chip.getAttribute('href'), '#/s/petroit--api--s4');
  assert.match(textOf(chip), /s4/);
  assert.ok(!all(card(env, 25), 'a').some((a) => /^in\s/.test(textOf(a).trim())), 'a task with its own worktree has no chip');
  assert.doesNotMatch(textOf(card(env, 24)), /undefined|null|worktree-/, 'a session-mode task has no branch to show');
  assert.ok(!startBtn(card(env, 24)) && !sendBtn(card(env, 24)), 'a started task has no backlog actions');
});

test('the same task in the needs-you and done columns keeps its chip (the session state decides the column)', async () => {
  const mk = (id, column, state) => row(id, { title: `T${id}`, phase: 'running', mode: 'session', column, tmux: 'petroit--api--s3', session_row: 8, prompt: null,
    session: { state, state_at: new Date().toISOString(), last_message: '', needs_attention: state === 'waiting', command: 'claude' } });
  const env = tasksWorld({ state: stateWith([mk(26, 'needs_you', 'waiting'), mk(27, 'done', 'done')]) });
  await go(env, PETROIT);
  assert.equal(colOf(card(env, 26)), 'Needs you');
  assert.equal(colOf(card(env, 27)), 'Done');
  for (const id of [26, 27]) assert.ok(all(card(env, id), 'a').some((a) => /^in\s/.test(textOf(a).trim())), `task ${id} has the chip`);
});

// ---------------------------------------------------------------- the project page and the demo fixture

test('the demo fixture: the Tasks tab of phasezero shows its backlog card and its in-progress cards, one of them with the chip', async () => {
  const demo = fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8');
  const env = tasksWorld({ state: fixtureState() });
  env.w.ctx.__demo = demo;
  env.w.run('state = demoRebase(JSON.parse(__demo)); __st = state');
  await go(env, '#/p/phasezero?tab=tasks');
  assert.deepEqual(colTitles(env).slice(0, 2), ['Backlog', 'In progress']);
  assert.deepEqual(cardsIn(env, 'Backlog'), [7], 'the demo backlog card');
  assert.deepEqual(cardsIn(env, 'In progress').sort(), [5, 6]);
  assert.ok(startBtn(card(env, 7)) && sendBtn(card(env, 7)));
  const chip = all(card(env, 6), 'a').find((a) => /^in\s/.test(textOf(a).trim()));
  assert.ok(chip, 'the session-mode demo task has the chip');
  assert.equal(chip.getAttribute('href'), '#/s/phasezero--NestJs-Ecommerce-Backend--t-stock-sync');
  assert.ok(!all(card(env, 5), 'a').some((a) => /^in\s/.test(textOf(a).trim())), 'the worktree task has none');
});

/** The demo world: the fixture rebased the way api() does in demo mode, and the demo flag on (nothing answers a write, so nothing can confirm an optimistic one). */
function demoTasksWorld() {
  const env = tasksWorld({ state: fixtureState() });
  env.w.localStorage.setItem('ccboard:demo', '1');
  env.w.ctx.__demo = fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8');
  env.w.run('state = demoRebase(JSON.parse(__demo)); __st = state');
  return env;
}

test('demo mode: Start moves the demo backlog card to In progress and it stays there through polls; nothing navigates to a session that does not exist', async () => {
  const env = demoTasksWorld();
  await go(env, '#/p/phasezero?tab=tasks');
  startBtn(card(env, 7)).click();
  await settle();
  assert.equal(colOf(card(env, 7)), 'In progress');
  assert.deepEqual(navs(env), [], 'no peek of a made-up session');
  assert.ok(toasts(env).some((x) => /started/.test(x.text)));
  await env.clock.advance(120000);
  env.w.run('state = demoRebase(JSON.parse(__demo)); __st = state; updateCurrentPage(state)');       // the poll in demo mode: the same fixture, the task still in the backlog
  assert.equal(colOf(card(env, 7)), 'In progress', 'the demo has no server to catch up, so the override is kept');
  assert.deepEqual(cardsIn(env, 'Backlog'), []);
});

test('demo mode: the ccboard backlog card sends to its one ready session in one tap and stays In progress with the chip; the phasezero card\'s sheet shows only busy sessions, each with its reason', async () => {
  const env = demoTasksWorld();
  await go(env, '#/p/ccboard?tab=tasks');
  assert.deepEqual(cardsIn(env, 'Backlog'), [8]);
  const quick = button(card(env, 8), /^→\s*s1$/);
  assert.ok(quick, 'ccboard/s1 is idle: the only ready Claude session of the repo (cx1 is Codex)');
  quick.click();
  await settle();
  assert.equal(colOf(card(env, 8)), 'In progress');
  const chip = all(card(env, 8), 'a').find((a) => /^in\s/.test(textOf(a).trim()));
  assert.ok(chip && chip.getAttribute('href') === '#/s/ccboard--ccboard--s1', 'with the chip');
  await env.clock.advance(120000);
  env.w.run('state = demoRebase(JSON.parse(__demo)); __st = state; updateCurrentPage(state)');
  assert.equal(colOf(card(env, 8)), 'In progress', 'and it stays');
  await go(env, '#/p/phasezero?tab=tasks');
  sendBtn(card(env, 7)).click();
  const rows = all(env.sheet(), '[data-tmux]');
  assert.ok(rows.length >= 2 && rows.every((r) => r.getAttribute('disabled') !== null), 'every phasezero session is busy: working, or waiting on the permission');
  assert.ok(rows.every((r) => /working|permission/i.test(textOf(r))), 'and each says why');
  assert.ok(!button(card(env, 7), /^→/), 'so there is no one-tap button');
});

test('the Tasks tab + task opens the form for the repo the Files tab shows, else the first repo', async () => {
  const env = tasksWorld({ state: stateWith([]) });
  await go(env, '#/p/phasezero/website?tab=tasks');
  button(env.page(), /\+\s*task/i).click();
  assert.equal(env.sheet().open, true);
  assert.equal(textOf(env.sheet().querySelector('.sheet-title')), 'New task · phasezero/website', 'the repo of the address');
  env.sheet().close();
  await go(env, '#/p/phasezero?tab=files');
  button(env.page(), /\+\s*task/i).click();
  const first = fixtureState().projects.find((p) => p.name === 'phasezero').repos[0].name;
  assert.equal(textOf(env.sheet().querySelector('.sheet-title')), `New task · phasezero/${first}`, 'the first repo, never the picker');
  env.sheet().close();
  await go(env, '#/p/phasezero?tab=tasks');
  button(env.page(), /\+\s*task/i).click();
  assert.match(textOf(env.sheet().querySelector('.sheet-title')), /^New task · phasezero\//, 'the project is preselected from the Tasks tab too');
  assert.equal(env.sheet().querySelectorAll('.pick-row').length, 0, 'no repo picker');
});

test('a project whose only git repo is its folder gets the task form for the folder', async () => {
  const st = stateWith([]);
  st.projects.push({ name: 'notes', path: '/srv/projects/notes', root: { name: 'root', path: '/srv/projects/notes', root: true, state: 'ok', branch: 'main', dirty: false, sessions: [] }, orphan_sessions: [], repos: [] });
  const env = tasksWorld({ state: st });
  await go(env, '#/p/notes?tab=tasks');
  button(env.page(), /\+\s*task/i).click();
  assert.equal(textOf(env.sheet().querySelector('.sheet-title')), 'New task · notes · project folder');
});

// ---------------------------------------------------------------- Later paints the card at once

test('adding a task with Later shows its card in the Backlog column at once, before the poll', async () => {
  const env = tasksWorld({ state: stateWith([]), answers: { '/api/tasks': { id: 40, slug: 'fix-the-footer', phase: 'backlog', tmux: null } } });
  await go(env, PETROIT);
  assert.deepEqual(cardsIn(env, 'Backlog'), []);
  button(env.page(), /\+\s*task/i).click();
  const f = env.sheet().querySelector('form.form');
  all(f, '.seg-btn').find((b) => textOf(b).trim() === 'Later').click();
  const g = env.sheet().querySelector('form.form');
  const title = all(g, 'input').find((i) => /title/i.test(i.getAttribute('placeholder') || '') || i.getAttribute('maxlength') === '120');
  title.value = 'Fix the footer';
  g.querySelector('textarea').value = 'The footer links wrap on 390 px';
  g.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  const p = posts(env, /\/api\/tasks$/);
  assert.equal(p.length, 1);
  assert.equal(p[0].body.when, 'later');
  assert.deepEqual(cardsIn(env, 'Backlog'), [40], 'the card is there with the id the server gave it');
  assert.match(textOf(card(env, 40)), /Fix the footer/);
  assert.match(textOf(card(env, 40)), /footer links wrap/, 'with the head of its prompt');
  assert.ok(startBtn(card(env, 40)), 'and it can be started right away');
});

// ---------------------------------------------------------------- the 10x pass, counted

test('project page to a running task is three actions: + task, type, Start task', async () => {
  const env = tasksWorld({ state: stateWith([]), answers: { '/api/tasks': { id: 41, slug: 'fix-the-footer', tmux: 'petroit--api--t-fix-the-footer', branch: 'worktree-fix-the-footer', attach_url: '/term/petroit--api--t-fix-the-footer' } } });
  await go(env, PETROIT);
  let taps = 0;
  const tap = (b) => { taps++; b.click(); };
  tap(button(env.page(), /\+\s*task/i));                                                    // 1: + task (project and repo preselected, Now selected, nothing else to choose)
  const f = env.sheet().querySelector('form.form');
  all(f, 'input').find((i) => /title/i.test(i.getAttribute('placeholder') || '') || i.getAttribute('maxlength') === '120').value = 'Fix the footer';   // 2: type
  f.querySelector('textarea').value = 'The footer links wrap on 390 px';
  assert.equal(textOf(f.querySelector('button[type=submit]')).trim(), 'Start task');
  taps++;                                                                                   // 3: Start task
  f.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.equal(taps, 2, 'two taps and one typing step');
  const p = posts(env, /\/api\/tasks$/);
  assert.equal(p.length, 1);
  assert.equal(p[0].body.project, 'petroit');
  assert.equal(p[0].body.repo, 'api');
  assert.equal(p[0].body.when, 'now');
  assert.deepEqual(navs(env), ['#/s/petroit--api--t-fix-the-footer'], 'and it ends on the running session');
});

test('a backlog card to a running session is one tap', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': DISPATCHED(20, 'petroit--api--t-x') } });
  await go(env, PETROIT);
  startBtn(card(env, 20)).click();                                                          // the one tap
  await settle();
  assert.equal(posts(env, /dispatch$/).length, 1);
  assert.equal(navs(env).length, 1);
  assert.equal(colOf(card(env, 20)), 'In progress');
});

// ---------------------------------------------------------------- the keyboard

test('c then t on a project page opens the task form for that project, not the picker of every repo', async () => {
  const env = tasksWorld({ state: stateWith([]), keymap: true });
  env.w.run('Keymap.install()');
  await go(env, '#/p/petroit?tab=tasks');
  const press = (key) => env.w.document.dispatch('keydown', { key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, target: null, defaultPrevented: false, preventDefault() {} });
  press('c');
  press('t');
  await settle();
  assert.equal(env.sheet().open, true);
  assert.equal(textOf(env.sheet().querySelector('.sheet-title')), 'New task · petroit/api', 'petroit has one repo: the form, no picker');
  env.sheet().close();
  await go(env, '#/');
  press('c');
  press('t');
  await settle();
  assert.equal(env.sheet().open, true);
  assert.equal(textOf(env.sheet().querySelector('.sheet-title')), 'New task', 'from Home it is still the repo picker');
});

// ---------------------------------------------------------------- the real api() and the server's words

const SERVER_MISMATCH = 'this session works in another repo';
const SERVER_BUSY = 'the session is working; wait for it to finish or pick another';

/** What the client sees for a 409: the real api() (core.js) over a fake fetch, then components.js's own reading of the error. */
async function realError(body, status = 409) {
  const w = makeWorld({ fetch: async () => ({ ok: false, status, statusText: 'Conflict', json: async () => body }) });
  w.load('core.js');
  w.load('components.js');
  w.run('globalThis.__err = null; api("POST", "/api/tasks/1/dispatch", { session: "x" }).catch((e) => { __err = e; });');
  await settle();
  return { w, mismatch: w.run('taskIsMismatch(__err)'), message: w.run('__err && __err.message'), status: w.run('__err && __err.status') };
}

test('a mismatch 409 is told from a busy 409 using only what the real api() hands over (the message, plus the body or status when it keeps them)', async () => {
  const m = await realError({ error: SERVER_MISMATCH, mismatch: { task: 'a/b', session: 'a/c' } });
  assert.equal(m.message, SERVER_MISMATCH, 'the message is the server\'s error text');
  assert.equal(m.status, 409, 'api() keeps the status');
  assert.equal(m.mismatch, true, 'so "work there anyway?" is offered');
  const b = await realError({ error: SERVER_BUSY, state: 'working' });
  assert.equal(b.mismatch, false, 'and a busy session is not mistaken for it');
  assert.equal(b.message, SERVER_BUSY);
});

test('taskIsMismatch reads the body api() keeps before it falls back to the words of the message', async () => {
  const w = makeWorld();
  w.load('core.js');
  w.load('components.js');
  const is = (src) => w.run(`taskIsMismatch(${src})`);
  assert.equal(is(`Object.assign(new Error('the server changed its wording'), { status: 409, body: { error: 'x', mismatch: { task: 'a/b', session: 'a/c' } } })`), true, 'the body says mismatch, whatever the words');
  assert.equal(is(`Object.assign(new Error('this session works in another repo'), { status: 409, body: { error: 'x', state: 'working' } })`), true, 'the message is the fallback');
  assert.equal(is(`Object.assign(new Error('the session is working'), { status: 409, body: { error: 'x', state: 'working' } })`), false);
  assert.equal(is('null'), false);
  assert.equal(is(`new Error('the session is working; wait for it to finish')`), false);
});

test('the server still says "another repo" where the client looks for it (the one coupling between app/main.py and components.js)', () => {
  const src = fs.readFileSync(path.join(STATIC, '..', 'main.py'), 'utf8');
  assert.ok(src.includes(SERVER_MISMATCH), 'api_task_dispatch\'s mismatch message changed: update taskIsMismatch in components.js (or attach the body to api() errors) and this test');
  const w = makeWorld();
  w.load('core.js');
  w.load('components.js');
  assert.equal(w.run(`taskIsMismatch(new Error(${JSON.stringify(SERVER_MISMATCH)}))`), true);
});
