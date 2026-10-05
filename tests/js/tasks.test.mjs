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
const sendBtn = (n) => button(n, /^Move/i);
const ownerChip = (n) => n.querySelector('.tk-owner');
const laneBtn = (sh, agent) => all(sh, 'button').find((b) => b.getAttribute('data-agent') === agent);
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
  const chip = ownerChip(c);
  assert.ok(chip, 'the owner chip');
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
  assert.deepEqual(['s2', 's5'].map(off), [true, true], 'errored and waiting-on-a-permission are not');
  assert.equal(off('s4'), false, 'a working Claude session takes the prompt into its queue (the same rule as a drop on its row)');
  assert.match(textOf(rowFor(sh, 's4')), /will be queued after the current turn/i);
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

test('the Move sheet is never a dead end: with no ready session it still starts a new Claude session; a session that has just started is listed as "starting…"', async () => {
  const st = stateWith([BACKLOG()]);
  const api = st.projects.find((p) => p.name === 'petroit').repos.find((r) => r.name === 'api');
  for (const x of api.sessions) x.state = 'errored';                 // every one busy for real (a working Claude session would take a queued prompt)
  api.sessions.push(sess('petroit', 'api', 's6', { state: 'unknown' }));
  const env = tasksWorld({ state: st, answers: { '/api/tasks/20/dispatch': { id: 20, phase: 'running', tmux: 'petroit--api--t-unify', session_row: 9, attach_url: '/term/petroit--api--t-unify' } } });
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  const sh = env.sheet();
  const rows = all(sh, '[data-tmux]');
  assert.ok(rows.length >= 6 && rows.every((r) => r.getAttribute('disabled') !== null), 'every session is listed and off: all are busy');
  assert.match(textOf(rowFor(sh, 's6')), /starting…/, 'a session whose first hook has not arrived is shown, not dropped');
  assert.match(textOf(sh), /None of these can take it right now/, 'and the sheet says so');
  const claude = laneBtn(sh, 'claude');
  assert.ok(claude, 'the way out is always there: a new Claude session of its own');
  claude.click();
  await settle();
  assert.deepEqual(posts(env, /dispatch$/).map((c) => c.body), [{ mode: 'lane' }]);
  assert.equal(env.sheet().open, false, 'the sheet closes');
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
  const say = all(sh, '.field-err').find((n) => /title is required/i.test(textOf(n)));
  assert.ok(say, 'the inline error next to the title says what is missing');
  assert.equal(say.getAttribute('role'), 'alert', 'announced as an alert');
  assert.equal(env.w.document.activeElement.getAttribute('aria-invalid'), 'true', 'and the title has the focus, flagged'); 
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
  const chip = ownerChip(card(env, 24));
  assert.ok(chip, 'the owner chip is on the handed card');
  assert.equal(chip.getAttribute('href'), '#/s/petroit--api--s4');
  assert.match(textOf(chip), /s4/);
  assert.match(textOf(ownerChip(card(env, 25))), /t-own/, 'a task with its own worktree names its own session in the chip');
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
  for (const id of [26, 27]) assert.ok(ownerChip(card(env, id)), `task ${id} has the owner chip`);
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
  const chip = ownerChip(card(env, 6));
  assert.ok(chip, 'the session-mode demo task has the owner chip');
  assert.equal(chip.getAttribute('href'), '#/s/phasezero--NestJs-Ecommerce-Backend--t-stock-sync');
  assert.match(textOf(ownerChip(card(env, 5))), /t-stock-sync/, 'the worktree task names its own session');
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
  assert.ok(cardsIn(env, 'Backlog').includes(8), 'the demo backlog card (chain steps waiting in the Backlog may sit beside it)');
  const quick = button(card(env, 8), /^→\s*s1$/);
  assert.ok(quick, 'ccboard/s1 is idle: the only ready Claude session of the repo (cx1 is Codex)');
  quick.click();
  await settle();
  assert.equal(colOf(card(env, 8)), 'In progress');
  const chip = ownerChip(card(env, 8));
  assert.ok(chip && chip.getAttribute('href') === '#/s/ccboard--ccboard--s1', 'with the owner chip');
  await env.clock.advance(120000);
  env.w.run('state = demoRebase(JSON.parse(__demo)); __st = state; updateCurrentPage(state)');
  assert.equal(colOf(card(env, 8)), 'In progress', 'and it stays');
  await go(env, '#/p/phasezero?tab=tasks');
  sendBtn(card(env, 7)).click();
  const rows = all(env.sheet(), '[data-tmux]');
  assert.ok(rows.length >= 2, 'every phasezero session is listed');
  assert.ok(rows.every((r) => (r.getAttribute('disabled') !== null) === /permission/i.test(textOf(r))), 'the one waiting on a permission is off; a working one is a queued hand-over');
  assert.ok(rows.every((r) => /queued|permission/i.test(textOf(r))), 'and each says what would happen');
  assert.ok(!button(card(env, 7), /^→/), 'so there is no one-tap button: a queue is never one tap');
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

// ---------------------------------------------------------------- the 10x pass: button hierarchy, the phone layout, no browser prompt()

const hasCls = (n, c) => n.classList.contains(c);
const STARTED_ROW = (id, over = {}) => row(id, { title: `Running ${id}`, phase: 'running', column: 'in_progress', tmux: `petroit--api--t-r${id}`, slug: `r${id}`, branch: `worktree-r${id}`,
  session: sess('petroit', 'api', `t-r${id}`, { state: 'working' }), ...over });
const narrow = (env) => env.w.run(`matchMedia = (q) => ({ matches: /max-width:\\s*599px/.test(q), addEventListener() {}, removeEventListener() {} })`);

test('a backlog card: Start is a tinted primary (the cards repeat it), Delete rests red-outlined and quiet, nothing is a filled red button', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  const c = card(env, 20);
  assert.ok(hasCls(startBtn(c), 'bp5-intent-primary') && hasCls(startBtn(c), 'tinted'), 'Start: primary, tinted');
  assert.ok(!hasCls(sendBtn(c), 'bp5-intent-primary') && !hasCls(editBtn(c), 'bp5-intent-primary'), 'the others are plain');
  assert.ok(hasCls(deleteBtn(c), 'bp5-intent-danger') && hasCls(deleteBtn(c), 'bp5-minimal'), 'Delete: danger, quiet (outlined by the stylesheet)');
  deleteBtn(c).click();
  const armed = button(env.page(), /^Confirm Delete$/);
  assert.ok(hasCls(armed, 'bp5-intent-danger') && hasCls(armed, 'confirm'), 'only the armed second tap carries .confirm, the one filled red button');
});

test('under 600 px a backlog card keeps Start and folds Send to session, Edit and Delete into a ... menu; Delete stays two taps', async () => {
  const env = tasksWorld();
  narrow(env);
  await go(env, PETROIT);
  const c = card(env, 20);
  assert.ok(startBtn(c), 'Start stays');
  assert.equal(sendBtn(c), undefined, 'Send to session is in the menu');
  assert.equal(editBtn(c), undefined, 'Edit too');
  assert.equal(deleteBtn(c), undefined, 'and Delete');
  const more = all(c, 'button').find((b) => b.getAttribute('aria-label') === 'More actions');
  assert.ok(more, 'one icon button with a name');
  assert.equal(more.getAttribute('title'), 'More actions');
  assert.equal(all(c, '.tk-acts button').length, 2, 'Start and the menu: two controls, not five');
  more.click();
  const items = () => env.w.document.querySelectorAll('.menuitem');
  assert.deepEqual(items().map((i) => i.textContent.trim()), ['Move…', 'Edit', 'Delete']);
  items().find((i) => /^Edit$/.test(i.textContent.trim())).click();
  assert.equal(env.sheet().open, true, 'Edit opens the edit sheet');
  assert.match(textOf(env.sheet().querySelector('.sheet-title')), /Edit task/);
  env.sheet().close();
  all(card(env, 20), 'button').find((b) => b.getAttribute('aria-label') === 'More actions').click();
  items().find((i) => /^Delete$/.test(i.textContent.trim())).click();
  assert.equal(calls(env).filter((c2) => c2.method === 'DELETE').length, 0, 'picking Delete only asks');
  const confirm = button(card(env, 20), /^Confirm Delete$/);
  assert.ok(confirm, 'the card shows Confirm Delete');
  assert.ok(button(card(env, 20), /^Cancel$/));
  assert.equal(all(card(env, 20), 'button').find((b) => b.getAttribute('aria-label') === 'More actions'), undefined, 'in place of the menu button');
  confirm.click();
  await settle();
  assert.equal(calls(env).filter((c2) => c2.method === 'DELETE').length, 1);
  assert.equal(card(env, 20), undefined, 'gone at once');
});

test('under 600 px a queued card (no Start, no Move) keeps Edit on the card and folds Delete into the menu (never a lone ... cell), and the one-tap session target stays on a backlog card', async () => {
  const env = tasksWorld({ state: stateWith([BACKLOG({ phase: 'queued' })]) });
  narrow(env);
  await go(env, PETROIT);
  assert.deepEqual(labelsIn(R1(card(env, 20))), ['Edit'], 'one visible action');
  all(card(env, 20), 'button').find((b) => b.getAttribute('aria-label') === 'More actions').click();
  assert.deepEqual(env.w.document.querySelectorAll('.menuitem').map((i) => i.textContent.trim()), ['Delete']);
  const solo = tasksWorld({ state: stateWith([BACKLOG({ id: 21, repo: 'api' })]) });          // petroit/api has two ready sessions: no guess
  narrow(solo);
  await go(solo, PETROIT);
  assert.ok(startBtn(card(solo, 21)));
});

test('a started task: the Terminal link is a tinted primary only on the card that needs you; Archive is quiet red; Fix CI is not a danger button', async () => {
  const waiting = STARTED_ROW(30, { column: 'needs_you', session: sess('petroit', 'api', 't-r30', { state: 'waiting', needs_attention: true }) });
  const failing = STARTED_ROW(31, { pr_url: 'https://example.invalid/pr/7', pr_number: 7, pr_state: 'OPEN', ci: { bucket: 'fail', checks: [{ name: 'unit', bucket: 'fail' }] } });
  const env = tasksWorld({ state: stateWith([waiting, failing]) });
  await go(env, PETROIT);
  const term = (n) => all(n, 'a').find((a) => /Terminal/.test(textOf(a)));
  assert.ok(term(card(env, 30)) && hasCls(term(card(env, 30)), 'bp5-intent-primary') && hasCls(term(card(env, 30)), 'tinted'), 'the waiting card leads');
  assert.ok(term(card(env, 31)) && !hasCls(term(card(env, 31)), 'bp5-intent-primary'), 'a card that is only running keeps Terminal plain');
  const arch = button(card(env, 31), /^Archive$/);
  assert.ok(hasCls(arch, 'bp5-intent-danger') && hasCls(arch, 'bp5-minimal') && !hasCls(arch, 'confirm'), 'Archive rests quiet, red-outlined; only Confirm Archive is filled');
  const fix = button(card(env, 31), /^Fix CI$/);
  assert.ok(fix, 'Fix CI shows on a failing PR');
  assert.ok(!hasCls(fix, 'bp5-intent-danger'), 'it is not a destructive action: no danger style');
  assert.ok(hasCls(fix, 'tk-fixci'), 'it carries its own hook for the warn colour');
  assert.equal(all(env.page(), '.task .tk-acts .bp5-intent-primary').filter((n) => !hasCls(n, 'tinted')).length, 0, 'no card carries a filled primary: the cards repeat the action, tinted');
});

test('Preview asks for a port in a labelled sheet, never window.prompt(): empty says so next to the field, a port posts {port}, the sheet closes', async () => {
  const started = STARTED_ROW(32);
  const env = tasksWorld({ state: stateWith([started]), answers: { '/api/tasks/32/preview': (req) => {
    if (!req.body || !req.body.port) throw env.w.get('mkErr')('no listening port found in this session', 400);
    return { url: 'https://box.example:9443', port: req.body.port };
  } } });
  env.w.ctx.prompt = () => { throw new Error('window.prompt must not be used'); };
  await go(env, PETROIT);
  button(card(env, 32), /^Preview$/).click();
  await settle();
  const sh = env.sheet();
  assert.equal(sh.open, true, 'the port sheet is open');
  assert.match(textOf(sh.querySelector('.sheet-title')), /Preview/);
  const fieldNode = all(sh, '.field').find((f) => /Dev server port/.test(textOf(f.querySelector('label'))));
  assert.ok(fieldNode, 'a labelled Dev server port field');
  assert.match(textOf(fieldNode.querySelector('.field-hint')), /no listening port found/, 'the server\'s reason is the helper text');
  const input = fieldNode.querySelector('input');
  assert.equal(fieldNode.querySelector('label').getAttribute('for'), input.getAttribute('id'), 'the label is tied to the input');
  all(sh, 'form')[0].dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.match(textOf(fieldNode.querySelector('.field-err')), /between 1 and 65535/, 'an empty port is said next to the field');
  assert.equal(calls(env).filter((c) => /preview$/.test(c.path) && c.body && c.body.port).length, 0, 'nothing posted');
  input.value = '3000';
  all(sh, 'form')[0].dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  const posted = calls(env).filter((c) => c.method === 'POST' && /\/tasks\/32\/preview$/.test(c.path));
  assert.deepEqual(posted.map((c) => c.body), [{}, { port: 3000 }], 'the blind try, then the port');
  assert.equal(sh.open, false, 'the sheet closes');
  assert.match(toasts(env).map((t) => t.text).join(' | '), /preview at https:\/\/box\.example:9443/);
});

// ================================================================ v0.5.14b / v0.5.15: the two-row card, owner / auto-close / result / chain / limit chips,
// the Move sheet, the long press and the m key, the board that waits for a drag

const iso = (offsetMs) => new Date(T0 + offsetMs).toISOString();
const rowsOf = (c) => all(c, '.tk-r');
const labelsIn = (n) => all(n, 'button, a').map((b) => textOf(b).trim()).filter(Boolean);
const R1 = (c) => c.querySelector('.tk-r1');
const R2 = (c) => c.querySelector('.tk-r2');
const withAgents = (tasks, over = {}) => stateWith(tasks, { agents: { claude: { installed: true }, codex: { installed: true } }, ...over });
const live = (id, over = {}) => STARTED_ROW(id, { tmux: `petroit--api--s2`, session: { state: 'working', state_at: agoIso(3), last_message: 'reading the handlers', needs_attention: false, command: 'claude' }, ...over });
const asTouch = (env) => { env.w.document.documentElement.classList.add('force-coarse'); };
const down = (n, extra = {}) => n.dispatchEvent({ type: 'pointerdown', pointerType: 'touch', clientX: 20, clientY: 20, button: 0, ...extra });
const MOVE_SHEET_TITLE = /^Move/;
const fieldOf = (f, re) => all(f, '.field').find((x) => re.test(textOf(x.querySelector('label'))));
const selectOf = (f, re) => fieldOf(f, re).querySelector('select');

test('a backlog card is two rows on one grid: Start and the quick target above, Move…, Edit and Delete (the destructive one last) below', async () => {
  const task = row(28, { project: 'phasezero', repo: 'NestJs-Ecommerce-Backend', title: 'Add an index on stock.variant_id' });
  const env = tasksWorld({ state: stateWith([task]) });
  await go(env, '#/p/phasezero?tab=tasks');
  const c = card(env, 28);
  assert.equal(rowsOf(c).length, 2, 'two rows, no wrapping flex row');
  assert.deepEqual(labelsIn(R1(c)), ['Start', '→ s9'], 'the primary and the owner / quick target');
  assert.deepEqual(labelsIn(R2(c)), ['Move…', 'Edit', 'Delete'], 'the quiet actions, Delete last');
  assert.ok(rowsOf(c).every((r) => r.children.length <= 3), 'at most three equal cells per row');
  assert.ok(hasCls(button(R2(c), /^Delete$/), 'bp5-intent-danger'));
  assert.ok(hasCls(startBtn(c), 'tinted') && !all(R2(c), 'button').some((b) => hasCls(b, 'bp5-intent-primary')), 'one tinted primary per card');
  assert.equal(c.getAttribute('tabindex'), '0', 'a card is a tab stop (the m key)');
  assert.equal(c.getAttribute('aria-keyshortcuts'), 'm');
  assert.equal(all(c, '.actions').length, 0, 'no ragged .actions wrap any more');
});

test('a started card: Terminal and Fix CI on row 1, the quiet actions on row 2 with Archive last, the rest in a ... cell; every row on the same 3-column grid', async () => {
  const t = live(31, { pr_url: 'https://example.invalid/pr/7', pr_number: 7, pr_state: 'OPEN', ci: { bucket: 'fail', checks: [{ name: 'unit', bucket: 'fail' }] } });
  const env = tasksWorld({ state: stateWith([t]) });
  await go(env, PETROIT);
  const c = card(env, 31);
  assert.deepEqual(labelsIn(R1(c)), ['Terminal', 'Fix CI'], 'row 1 (the ... cell has no text)');
  assert.deepEqual(labelsIn(R2(c)), ['PR #7', 'Preview', 'Archive']);
  assert.ok(rowsOf(c).every((r) => r.children.length <= 3));
  const more = all(R1(c), 'button').find((b) => b.getAttribute('aria-label') === 'More actions');
  assert.ok(more, 'what does not fit is one tap away');
  more.click();
  assert.deepEqual(env.w.document.querySelectorAll('.menuitem').map((i) => i.textContent.trim()), ['Diff', 'Refresh PR status']);
  assert.ok(hasCls(button(R2(c), /^Archive$/), 'bp5-intent-danger'), 'Archive is the red-outlined last cell');
});

test('the owner chip says who runs the task: agent glyph, session name, state glyph, state and age; it opens the session', async () => {
  const env = tasksWorld({ state: stateWith([live(32)]) });
  await go(env, PETROIT);
  const chip = ownerChip(card(env, 32));
  assert.equal(textOf(chip).replace(/\s+/g, ' ').trim(), '◆ s2 ✽ working · 3m');
  assert.equal(chip.getAttribute('href'), '#/s/petroit--api--s2');
  const cx = tasksWorld({ state: withAgents([live(33, { agent: 'codex', tmux: 'petroit--api--cx1' })]) });
  await go(cx, PETROIT);
  assert.match(textOf(ownerChip(card(cx, 33))), /^◇ cx1/, 'a Codex task wears the Codex glyph');
});

test('auto-close: a pending close shows the countdown and Keep open; the countdown ticks every second; Keep open posts, paints at once and stops the ticker', async () => {
  const t = live(34, { phase: 'done', title: 'Pending close', branch: '', session: { state: 'done', state_at: agoIso(1), last_message: 'done', needs_attention: false, command: 'claude' },
    auto_close: true, autoclose: { task: 34, due: iso(45000) }, result: 'All done.', result_at: agoIso(1), done_at: agoIso(1) });
  const env = tasksWorld({ state: stateWith([t]), answers: { '/api/tasks/34/keep-open': { ok: true, kept: true } } });
  await go(env, PETROIT);
  const c = card(env, 34);
  const strip = c.querySelector('.tk-ac');
  assert.ok(strip, 'the strip is there');
  assert.match(textOf(strip), /auto-close\s*0:45/);
  const keep = button(strip, /^Keep open$/);
  assert.ok(keep, 'with Keep open');
  const pendingBefore = env.clock.pending;
  await env.clock.advance(1000);
  assert.match(textOf(c.querySelector('.tk-count')), /^0:44$/, 'one second later');
  await env.clock.advance(44000);
  assert.match(textOf(c.querySelector('.tk-count')), /closing…/, 'at the deadline');
  keep.click();
  await settle();
  assert.deepEqual(posts(env, /keep-open$/).map((p) => p.path), ['/api/tasks/34/keep-open']);
  assert.equal(card(env, 34).querySelector('.tk-ac'), null, 'the strip is gone before any poll');
  assert.match(toasts(env).pop().text, /kept open/);
  await env.clock.advance(1000);
  assert.ok(env.clock.pending < pendingBefore + 1, 'nothing is left to tick: the interval stops itself when no countdown is on the page');
});

test('auto-close: {closing: true} says so without a button, {waiting} says why the close was postponed, and a held question marks the card needs-you with no countdown', async () => {
  const base = { phase: 'done', branch: '', session: { state: 'done', state_at: agoIso(1), last_message: 'done', needs_attention: false, command: 'claude' }, auto_close: true, result: 'ok' };
  const closing = live(35, { ...base, title: 'Closing', autoclose: { task: 35, closing: true } });
  const waiting = live(36, { ...base, title: 'Waiting', tmux: 'petroit--api--s3', autoclose: { task: 36, due: iso(15000), waiting: 'someone is attached' } });
  const asked = live(37, { ...base, title: 'Asked', tmux: 'petroit--api--s1', column: 'needs_you', result: 'Done. Should I also translate it?', autoclose: { task: 37, held: 'question' },
    session: { ...base.session, needs_attention: true } });
  const env = tasksWorld({ state: stateWith([closing, waiting, asked]) });
  await go(env, PETROIT);
  assert.match(textOf(card(env, 35).querySelector('.tk-ac')), /closing…/);
  assert.equal(button(card(env, 35), /^Keep open$/), undefined, 'too late to keep it open');
  assert.match(textOf(card(env, 36).querySelector('.tk-ac')), /someone is attached/);
  assert.ok(button(card(env, 36), /^Keep open$/));
  const c = card(env, 37);
  assert.equal(c.querySelector('.tk-ac'), null, 'a held card has no countdown');
  assert.match(textOf(c.querySelector('.tk-asked')), /needs you/, 'it says it wants an answer');
  assert.ok(c.classList.contains('attn'), 'and wears the attention border');
  assert.equal(colOf(c), 'Needs you');
});

test('a task whose session closed after the stop: "closed after stop", the result under a Result summary, New PR… and Reopen on row 1, no Terminal', async () => {
  const t = STARTED_ROW(38, { title: 'Closed one', phase: 'done', column: 'done', session: null, closed_at: agoIso(2), auto_close: true, branch: 'worktree-closed',
    result: 'Added the cursor to every list endpoint.\nTests are green.', result_at: agoIso(3), done_at: agoIso(3), tmux: 'petroit--api--t-r38' });
  const env = tasksWorld({ state: stateWith([t]) });
  await go(env, PETROIT);
  const c = card(env, 38);
  assert.match(textOf(c.querySelector('.tk-closed')), /closed after stop · 2m/);
  assert.deepEqual(labelsIn(R1(c)), ['New PR…', 'Reopen'], 'the PR action before Reopen');
  assert.ok(hasCls(button(R1(c), /^New PR/), 'tinted') && !hasCls(button(R1(c), /^Reopen$/), 'bp5-intent-primary'));
  assert.equal(all(c, 'a').filter((a) => /\/term\//.test(a.getAttribute('href') || '')).length, 0, 'no terminal for a session that is gone');
  assert.equal(ownerChip(c), null, 'and no owner chip');
  const d = c.querySelector('details.tk-result');
  assert.ok(d, 'the result is an expandable excerpt');
  assert.equal(textOf(d.querySelector('.tk-result-first')), 'Added the cursor to every list endpoint.', 'its first line while closed');
  assert.match(textOf(d.querySelector('.tk-result-body')), /Tests are green/, 'the excerpt inside');
});

test('Reopen posts, paints the card into In progress at once and opens the new session; a refusal puts it back and says why', async () => {
  const t = STARTED_ROW(39, { title: 'Reopen me', phase: 'done', column: 'done', session: null, closed_at: agoIso(2), branch: '', tmux: 'petroit--api--t-r39', slug: 'r39' });
  const env = tasksWorld({ state: stateWith([t]), answers: { '/api/tasks/39/reopen': { id: 39, phase: 'running', tmux: 'petroit--api--t-r39', session_row: 120, attach_url: '/term/x' } } });
  await go(env, PETROIT);
  assert.deepEqual(labelsIn(R1(card(env, 39))), ['Reopen'], 'nothing to push: Reopen leads');
  assert.ok(hasCls(button(R1(card(env, 39)), /^Reopen$/), 'tinted'));
  button(card(env, 39), /^Reopen$/).click();
  await settle();
  assert.deepEqual(posts(env, /reopen$/).map((p) => p.path), ['/api/tasks/39/reopen']);
  assert.equal(colOf(card(env, 39)), 'In progress', 'no waiting for the poll');
  assert.ok(ownerChip(card(env, 39)), 'it has an owner again');
  assert.deepEqual(navs(env), ['#/s/petroit--api--t-r39'], 'and its session opens');
  const bad = tasksWorld({ state: stateWith([t]), answers: { '/api/tasks/39/reopen': () => { throw bad.w.get('mkErr')('already running', 409); } } });
  await go(bad, PETROIT);
  button(card(bad, 39), /^Reopen$/).click();
  await settle();
  assert.equal(colOf(card(bad, 39)), 'Done', 'back where it was');
  assert.match(toasts(bad).pop().text, /already running/);
});

test('a finished task with a pull request: PR and Merge… come before Reopen; a failed one leads with Reopen', async () => {
  const done = STARTED_ROW(40, { title: 'With a PR', phase: 'done', column: 'pr', session: null, closed_at: agoIso(5), branch: 'worktree-pr', pr_url: 'https://example.invalid/pr/9', pr_number: 9, pr_state: 'OPEN', tmux: 'petroit--api--t-r40' });
  const failed = STARTED_ROW(41, { title: 'Failed one', phase: 'failed', column: 'needs_you', session: null, branch: 'worktree-f', tmux: 'petroit--api--t-r41', result: 'rate limited' });
  const env = tasksWorld({ state: stateWith([done, failed]) });
  await go(env, PETROIT);
  assert.deepEqual(labelsIn(R1(card(env, 40))), ['PR #9', 'Merge…']);
  assert.deepEqual(labelsIn(R2(card(env, 40))), ['Reopen', 'Diff', 'Archive'], 'Reopen follows the PR actions');
  assert.match(textOf(card(env, 41)), /failed/);
  assert.deepEqual(labelsIn(R1(card(env, 41))), ['Reopen', 'New PR…'], 'a failed task is retried first');
});

test('a long result shows its first 300 characters and loads the rest on request; the expanded state survives a repaint', async () => {
  const full = 'First line.\n' + 'x'.repeat(500) + '\nTHE END';
  const t = STARTED_ROW(42, { title: 'Long result', phase: 'done', column: 'done', session: null, closed_at: agoIso(1), branch: '', tmux: 'petroit--api--t-r42', result: full.slice(0, 300) });
  const env = tasksWorld({ state: stateWith([t]), answers: { '/api/tasks/42': (req) => (req.method === 'GET' ? { id: 42, result: full } : { ok: true }) } });
  await go(env, PETROIT);
  const more = button(card(env, 42), /^Show the whole result$/);
  assert.ok(more, 'the poll carries only the head');
  more.click();
  await settle();
  assert.ok(calls(env).some((c) => c.method === 'GET' && c.path === '/api/tasks/42'));
  assert.match(textOf(card(env, 42).querySelector('.tk-result-body')), /THE END$/, 'all of it');
  assert.equal(button(card(env, 42), /^Show the whole result$/), undefined);
  const d = card(env, 42).querySelector('details.tk-result');
  d.open = true;
  d.dispatchEvent({ type: 'toggle' });
  setState(env, stateWith([{ ...t }]));
  assert.ok(card(env, 42).querySelector('details.tk-result').getAttribute('open') !== null, 'still open after the poll rebuilt the card');
});

test('chains: "step 2 of 3" from the server position or from the parent links, and a strip of connected steps with a status each', async () => {
  const mk = (id, over) => row(id, { chain_id: 'c1', title: `Step ${id}`, ...over });
  const s1 = mk(50, { phase: 'done', column: 'done', prompt: null, session: null, closed_at: agoIso(5), tmux: 'petroit--api--t-s50', result: 'one', chain: { i: 1, n: 3 } });
  const s2 = mk(51, { phase: 'running', column: 'in_progress', prompt: null, parent_id: 50, tmux: 'petroit--api--s4', session: { state: 'working', state_at: agoIso(1), last_message: '', needs_attention: false, command: 'claude' }, chain: { i: 2, n: 3 } });
  const s3 = mk(52, { phase: 'queued', parent_id: 51, chain: { i: 3, n: 3 } });
  const env = tasksWorld({ state: stateWith([s1, s2, s3]) });
  await go(env, PETROIT);
  assert.match(textOf(card(env, 51).querySelector('.tk-chain-badge')), /^step 2 of 3$/);
  assert.match(textOf(card(env, 52).querySelector('.tk-chain-badge')), /^step 3 of 3$/);
  const strip = env.page().querySelector('.tk-chain');
  assert.ok(strip, 'the chain is drawn above the columns');
  const steps = all(strip, '.tk-step');
  assert.deepEqual(steps.map((s) => s.getAttribute('data-step')), ['50', '51', '52']);
  assert.deepEqual(steps.map((s) => textOf(s.querySelector('.tk-step-st')).replace(/[^a-z ]/g, '').trim()), ['done', 'working', 'waiting'], 'a status per step');
  assert.equal(all(strip, '.tk-link').length, 2, 'connected');
  assert.match(textOf(env.page().querySelector('.tk-chains')), /Chains/, 'a labelled section');
  steps[2].click();
  assert.equal(env.w.document.activeElement, card(env, 52), 'a step takes you to its card');
  const noServer = tasksWorld({ state: stateWith([{ ...s1, chain: null }, { ...s2, chain: null }, { ...s3, chain: null }]) });
  await go(noServer, PETROIT);
  assert.match(textOf(card(noServer, 51).querySelector('.tk-chain-badge')), /^step 2 of 3$/, 'worked out from parent_id when the server sends no position');
});

test('the Tasks page draws the chain between the head and the columns too', async () => {
  const a = row(60, { chain_id: 'c9', phase: 'done', column: 'done', prompt: null, session: null, closed_at: agoIso(2), tmux: 'petroit--api--t-s60', result: 'r' });
  const b = row(61, { chain_id: 'c9', phase: 'queued', parent_id: 60 });
  const env = tasksWorld({ state: stateWith([a, b]) });
  await go(env, '#/tasks');
  const sec = env.page().querySelector('#tasks');
  const strip = sec.querySelector('.tk-chains');
  assert.ok(strip && all(strip, '.tk-step').length === 2);
  assert.ok(strip.nextElementSibling && strip.nextElementSibling.classList.contains('kanban'), 'right above the columns');
  assert.match(textOf(card(env, 61).querySelector('.tk-chain-badge')), /step 2 of 2/, 'the card knows its place even though home.js draws it');
  setState(env, stateWith([a, b]));
  assert.equal(sec.querySelectorAll('.tk-chains').length, 1, 'repainting never stacks a second strip');
});

test('a step the limit gate holds says when the window resets; one the gate does not hold says nothing', async () => {
  const resets = Math.floor(T0 / 1000) + 3 * 3600;
  const held = row(70, { phase: 'queued', chain_id: 'c2', parent_id: 69, limit_hold: { kind: '5h', resets_at: resets, pct: 91 } });
  const free = row(71, { phase: 'queued', chain_id: 'c2', parent_id: 70 });
  const env = tasksWorld({ state: stateWith([held, free]) });
  await go(env, PETROIT);
  assert.match(textOf(card(env, 70).querySelector('.tk-hold')), /^\s*waiting for the limit window \(resets \d\d:\d\d\)\s*$/);
  assert.equal(card(env, 71).querySelector('.tk-hold'), null);
  const stripSt = all(env.page(), '.tk-step').map((s) => textOf(s.querySelector('.tk-step-st')).replace(/[^a-z ]/g, '').trim());
  assert.ok(stripSt.includes('held'), 'the chain strip marks the held step');
});

test('a hand start inside the limit window goes ahead and warns: the limit_warning of the answer becomes a warning toast', async () => {
  const env = tasksWorld({ answers: { '/api/tasks/20/dispatch': { ...DISPATCHED(20, 'petroit--api--t-x'), limit_warning: { kind: '5h', resets_at: Math.floor(T0 / 1000) + 7200, pct: 91.4 } } } });
  await go(env, PETROIT);
  startBtn(card(env, 20)).click();
  await settle();
  assert.equal(colOf(card(env, 20)), 'In progress', 'never blocked');
  const warn = toasts(env).find((t) => t.kind === 'warn');
  assert.ok(warn, 'a warning');
  assert.match(warn.text, /5h usage window is at 91%/);
  assert.match(warn.text, /resets \d\d:\d\d/);
});

// ---------------------------------------------------------------- the Move sheet

test('Move… opens a bottom sheet in labelled sections: two 52 px lane buttons (Codex off until installed) and the running sessions', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  const sh = env.sheet();
  assert.equal(sh.open, true);
  assert.ok(sh.classList.contains('bottom'), 'a bottom sheet, wherever the window is wide');
  assert.match(textOf(sh.querySelector('.sheet-title')), /^Move “Unify the devices pagination”$/);
  assert.deepEqual(all(sh, '.tk-sec').map(textOf), ['Start in a new session', 'Hand to a running session']);
  const claude = laneBtn(sh, 'claude');
  const codex = laneBtn(sh, 'codex');
  assert.ok(claude && codex);
  assert.equal(claude.getAttribute('disabled'), null);
  assert.notEqual(codex.getAttribute('disabled'), null, 'Codex is not installed on this box');
  assert.match(codex.getAttribute('title'), /not installed/);
  assert.ok(hasCls(claude, 'tinted'), 'the task\'s own agent leads');
  assert.match(textOf(claude), /Claude/);
  assert.match(textOf(claude.querySelector('.tk-lane-sub')), /repo defaults/, 'nothing saved yet');
  assert.ok(button(sh, /^Options…$/) && button(sh, /^Cancel$/));
  button(sh, /^Cancel$/).click();
  assert.equal(env.sheet().open, false);
});

test('the lane buttons show the repo\'s saved launch defaults; Codex starts the card in a Codex session, Claude in a Claude one', async () => {
  const env = tasksWorld({ state: withAgents([BACKLOG()]), answers: { '/api/tasks/20/dispatch': DISPATCHED(20, 'petroit--api--t-x') } });
  env.w.localStorage.setItem('ccboard:task:petroit/api', JSON.stringify({ model: 'opus', effort: 'high', permission_mode: 'acceptEdits' }));
  env.w.localStorage.setItem('ccboard:task:petroit/api:codex', JSON.stringify({ reasoning_effort: 'medium', sandbox: 'workspace-write' }));
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  assert.equal(textOf(laneBtn(env.sheet(), 'claude').querySelector('.tk-lane-sub')), 'opus · high · acceptEdits');
  assert.equal(textOf(laneBtn(env.sheet(), 'codex').querySelector('.tk-lane-sub')), 'medium · workspace-write');
  assert.equal(laneBtn(env.sheet(), 'codex').getAttribute('disabled'), null, 'installed');
  laneBtn(env.sheet(), 'codex').click();
  await settle();
  assert.deepEqual(posts(env, /dispatch$/).map((c) => c.body), [{ mode: 'lane', agent: 'codex' }]);
  assert.equal(colOf(card(env, 20)), 'In progress');
  assert.equal(env.sheet().open, false);
});

test('the running-session list names the state and the task a session already carries, and says why a row is off (busy, a prompt, the other agent)', async () => {
  const handedTask = row(44, { title: 'Already here', phase: 'running', column: 'in_progress', tmux: 'petroit--api--s4', prompt: null, session: { state: 'working', state_at: agoIso(1), last_message: '', needs_attention: false, command: 'claude' } });
  const st = withAgents([BACKLOG(), handedTask]);
  st.projects.find((p) => p.name === 'petroit').repos.find((r) => r.name === 'api').sessions.push(sess('petroit', 'api', 'cx1', { state: 'idle', agent: 'codex', launcher: 'codex', command: 'codex' }));
  const env = tasksWorld({ state: st });
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  const sh = env.sheet();
  const off = (n) => rowFor(sh, n).getAttribute('disabled') !== null;
  assert.equal(off('s1'), false);
  assert.match(textOf(rowFor(sh, 's1')), /done/, 'its state');
  assert.match(textOf(rowFor(sh, 's4')), /task: Already here/, 'the task it carries, not only its last prompt');
  assert.equal(off('cx1'), true);
  assert.match(textOf(rowFor(sh, 'cx1')), /a Codex session: this task is for Claude/);
  assert.ok(sessionRows(sh).indexOf(rowFor(sh, 's1')) < sessionRows(sh).indexOf(rowFor(sh, 'cx1')), 'ready first');
});

test('Options… opens the launcher in dispatch mode: a new session of either agent with its launch choices, or a running session; only what differs from the defaults is posted', async () => {
  const env = tasksWorld({ state: withAgents([BACKLOG()]), answers: { '/api/tasks/20/dispatch': DISPATCHED(20, 'petroit--api--t-x') } });
  await go(env, PETROIT);
  sendBtn(card(env, 20)).click();
  button(env.sheet(), /^Options…$/).click();
  let f = env.sheet().querySelector('form.tk-dispatch');
  assert.ok(f, 'the dispatch form');
  assert.deepEqual(all(f, '.seg-btn').map((b) => textOf(b).trim()), ['New session', 'Running session', '◆ Claude', '◇ Codex']);
  assert.deepEqual(all(f, '.seg-btn').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => textOf(b).trim()), ['New session', '◆ Claude']);
  const close = f.querySelector('input[type=checkbox]');
  assert.equal(close.checked, true, 'a new session closes itself by default');
  const model = fieldOf(f, /^Model$/).querySelector('select');
  model.value = 'opus';
  f.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.deepEqual(posts(env, /dispatch$/).map((c) => c.body), [{ mode: 'lane', model: 'opus' }], 'Claude, auto-close on: neither is sent, the changed model is');
  // a Codex session with the switch off
  const env2 = tasksWorld({ state: withAgents([BACKLOG()]), answers: { '/api/tasks/20/dispatch': DISPATCHED(20, 'petroit--api--t-x') } });
  await go(env2, PETROIT);
  sendBtn(card(env2, 20)).click();
  button(env2.sheet(), /^Options…$/).click();
  f = env2.sheet().querySelector('form.tk-dispatch');
  all(f, '.seg-btn').find((b) => /Codex/.test(textOf(b))).click();
  const sw = f.querySelector('input[type=checkbox]');
  sw.checked = false;
  sw.dispatchEvent({ type: 'change' });
  selectOf(f, /Reasoning effort/).value = 'high';
  f.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.deepEqual(posts(env2, /dispatch$/).map((c) => c.body), [{ mode: 'lane', agent: 'codex', auto_close: false, reasoning_effort: 'high' }]);
  // a running session: auto-close is off by default, and sent only when turned on
  const env3 = tasksWorld({ state: withAgents([BACKLOG()]), answers: { '/api/tasks/20/dispatch': { id: 20, phase: 'running', tmux: 'petroit--api--s1', session_row: 4, pasted: true } } });
  await go(env3, PETROIT);
  sendBtn(card(env3, 20)).click();
  button(env3.sheet(), /^Options…$/).click();
  f = env3.sheet().querySelector('form.tk-dispatch');
  all(f, '.seg-btn').find((b) => /Running session/.test(textOf(b))).click();
  assert.equal(f.querySelector('input[type=checkbox]').checked, false);
  selectOf(f, /^Session$/).value = 'petroit--api--s1';
  f.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.deepEqual(posts(env3, /dispatch$/).map((c) => c.body), [{ session: 'petroit--api--s1' }]);
});

// ---------------------------------------------------------------- the long press and the m key

test('a held touch (450 ms) on a Backlog card opens the Move sheet; 449 ms does not', async () => {
  const env = tasksWorld();
  asTouch(env);
  await go(env, PETROIT);
  const c = card(env, 20);
  down(c);
  await env.clock.advance(449);
  assert.equal(env.sheet().open, false, 'not yet');
  await env.clock.advance(1);
  assert.equal(env.sheet().open, true);
  assert.match(textOf(env.sheet().querySelector('.sheet-title')), MOVE_SHEET_TITLE);
});

test('a long press is cancelled by a move of more than 8 px, a scroll, a lift and a cancel; 8 px is still a hold', async () => {
  const env = tasksWorld();
  asTouch(env);
  await go(env, PETROIT);
  const c = card(env, 20);
  const press = async (cancel) => {
    env.sheet().close();
    down(c);
    await env.clock.advance(200);
    cancel();
    await env.clock.advance(400);
    return env.sheet().open;
  };
  assert.equal(await press(() => c.dispatchEvent({ type: 'pointermove', clientX: 29, clientY: 20 })), false, '9 px: a swipe');
  assert.equal(await press(() => env.w.fire('scroll', {})), false, 'a scroll');
  assert.equal(await press(() => c.dispatchEvent({ type: 'pointerup' })), false, 'a lift');
  assert.equal(await press(() => c.dispatchEvent({ type: 'pointercancel' })), false, 'the browser took the touch for a scroll');
  assert.equal(await press(() => c.dispatchEvent({ type: 'pointermove', clientX: 28, clientY: 20 })), true, '8 px of finger wobble is still a hold');
});

test('a mouse on a fine pointer never long-presses; a press that starts on a button is left to the button; the click after a fired press is swallowed', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  const c = card(env, 20);
  down(c, { pointerType: 'mouse' });
  await env.clock.advance(800);
  assert.equal(env.sheet().open, false, 'desktop has the Move button and drag and drop');
  asTouch(env);
  down(startBtn(c));
  await env.clock.advance(800);
  assert.equal(env.sheet().open, false, 'a long press on Start is Start\'s business');
  down(c);
  await env.clock.advance(450);
  assert.equal(env.sheet().open, true);
  env.sheet().close();
  const click = { type: 'click', defaultPrevented: false, preventDefault() { click.defaultPrevented = true; }, stopPropagation() {} };
  c.dispatchEvent(click);
  assert.equal(click.defaultPrevented, true, 'the lift that ends the press must not also tap whatever is under it');
  const ctx = { type: 'contextmenu', defaultPrevented: false, preventDefault() { ctx.defaultPrevented = true; } };
  c.dispatchEvent(ctx);
  assert.equal(ctx.defaultPrevented, true, 'and the browser\'s own long-press menu stays away on a touch screen');
});

test('longPress is a plain helper any node can use, with its own timing', async () => {
  const env = tasksWorld();
  asTouch(env);
  const n = env.w.document.createElement('div');
  env.w.document.body.append(n);
  env.w.ctx.__fired = [];
  env.w.ctx.__node = n;
  env.w.run('globalThis.__lp = longPress(__node, (e) => __fired.push(e.type), { ms: 100, slop: 4 })');
  n.dispatchEvent({ type: 'pointerdown', pointerType: 'touch', clientX: 0, clientY: 0, button: 0 });
  await env.clock.advance(99);
  assert.deepEqual(plain(env.w.get('__fired')), []);
  await env.clock.advance(1);
  assert.deepEqual(plain(env.w.get('__fired')), ['pointerdown']);
  n.dispatchEvent({ type: 'pointerdown', pointerType: 'touch', clientX: 0, clientY: 0, button: 0 });
  await env.clock.advance(50);
  env.w.run('__lp.cancel()');
  await env.clock.advance(100);
  assert.equal(plain(env.w.get('__fired')).length, 1, 'cancel() stops one in flight');
});

test('the m key opens the Move sheet from a focused Backlog card; typing in a field, a modifier or a queued card do not', async () => {
  const queued = row(21, { phase: 'queued', title: 'Queued one' });
  const env = tasksWorld({ state: stateWith([BACKLOG(), queued]) });
  await go(env, PETROIT);
  const key = (n, k, extra = {}) => { const e = { type: 'keydown', key: k, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, isComposing: false, defaultPrevented: false, preventDefault() { e.defaultPrevented = true; }, stopPropagation() {}, target: n, ...extra }; n.dispatchEvent(e); return e; };
  const c = card(env, 20);
  key(c, 'm', { ctrlKey: true });
  key(c, 'x');
  assert.equal(env.sheet().open, false, 'a modifier and another key do nothing');
  key(startBtn(c), 'm');
  assert.equal(env.sheet().open, true, 'from a button inside the card the key bubbles to it');
  env.sheet().close();
  const input = env.w.document.createElement('input');
  c.append(input);
  key(input, 'm');
  assert.equal(env.sheet().open, false, 'a field keeps its m');
  const e = key(c, 'm');
  assert.equal(env.sheet().open, true);
  assert.equal(e.defaultPrevented, true);
  env.sheet().close();
  key(card(env, 21), 'm');
  assert.equal(env.sheet().open, false, 'a queued card cannot be moved: the server would answer 409');
});

test('a started task has no Move: no m key, no long press', async () => {
  const env = tasksWorld({ state: stateWith([live(45)]) });
  asTouch(env);
  await go(env, PETROIT);
  down(card(env, 45));
  await env.clock.advance(600);
  assert.equal(env.sheet().open, false);
  assert.equal(card(env, 45).getAttribute('data-movable'), null);
});

test('the stylesheet takes the iOS callout and the text selection away from a movable card, and the touch rules have their force-coarse twins', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages.css'), 'utf8');
  assert.match(css, /\.task\[data-movable\]\s*\{[^}]*-webkit-touch-callout:\s*none/);
  assert.match(css, /html\.force-coarse \.task\[data-movable\]\s*\{[^}]*user-select:\s*none/);
  assert.match(css, /\.tk-lane-btn\.bp5-button\s*\{[^}]*min-height:\s*52px/, 'the 52 px lane buttons');
  assert.match(css, /\.task \.tk-r\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/, 'one 3-column grid for both rows');
});

// ---------------------------------------------------------------- the board waits for a drag

/** A Dnd stand-in with the three things the board uses: dragging, afterDrag (queue the repaint while a card is held) and laneBar (one node with ccPatch). */
function withDnd(env) {
  env.w.run(`globalThis.Dnd = { dragging: null, held: [], bars: 0, afterDrag(fn) { if (!Dnd.dragging) return false; if (!Dnd.held.includes(fn)) Dnd.held.push(fn); return true; },
    laneBar(o) { Dnd.bars++; const n = document.createElement('div'); n.setAttribute('class', 'dnd-bar'); n.setAttribute('data-project', (o && o.project) || ''); n.ccPatch = (st) => { n.setAttribute('data-patched', String(((st && st.tasks) || []).length)); }; return n; } };`);
}

test('the project Tasks tab mounts the lane bar for its project once and keeps patching it; the cards are not rebuilt while one is dragged and repaint at the end', async () => {
  const env = tasksWorld();
  withDnd(env);
  await go(env, PETROIT);
  const bar = env.page().querySelector('.dnd-bar');
  assert.ok(bar, 'the dispatch bar sits above the columns');
  assert.equal(bar.getAttribute('data-project'), 'petroit', 'it lists this project\'s sessions');
  const dragged = card(env, 20);
  env.w.run('Dnd.dragging = { id: 20 }');
  setState(env, stateWith([BACKLOG(), row(21, { title: 'Arrived mid-drag' })]));
  assert.equal(card(env, 20), dragged, 'the dragged node is still the one in the page');
  assert.equal(card(env, 21), undefined, 'and nothing is rebuilt under the pointer');
  env.w.run('Dnd.dragging = null; Dnd.held.splice(0).forEach((fn) => fn())');
  assert.ok(card(env, 21), 'the repaint that waited runs when the drag ends');
  assert.equal(env.page().querySelector('.dnd-bar'), bar, 'the bar is one node across repaints');
  assert.equal(env.w.get('Dnd.bars'), 1);
  assert.equal(bar.getAttribute('data-patched'), '2', 'and it was patched with the new state');
});

test('without dnd.js the board still draws (no lane bar, no drag to wait for)', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  assert.ok(card(env, 20));
  assert.equal(env.page().querySelector('.dnd-bar'), null);
});

test('a board repaints at most when what it shows changed: the same state twice keeps the very same card nodes', async () => {
  const env = tasksWorld();
  await go(env, PETROIT);
  const c = card(env, 20);
  setState(env, stateWith([BACKLOG()]));
  assert.equal(card(env, 20), c, 'a poll that changes nothing leaves the cards alone (a focused button keeps its focus)');
  setState(env, stateWith([BACKLOG({ title: 'Renamed' })]));
  assert.notEqual(card(env, 20), c);
  assert.match(textOf(card(env, 20)), /Renamed/);
});

test('the Move sheet offers a working Claude session as a queued hand-over (the drop popover does the same), never as the one-tap target', async () => {
  const st = stateWith([BACKLOG()]);
  const api = st.projects.find((p) => p.name === 'petroit').repos.find((r) => r.name === 'api');
  api.sessions = [sess('petroit', 'api', 'w1', { state: 'working', agent: 'claude' }), sess('petroit', 'api', 'i1', { state: 'idle', agent: 'claude' })];
  const env = tasksWorld({ state: st });
  await go(env, PETROIT);
  env.w.ctx.__t = { id: 20, project: 'petroit', repo: 'api', agent: 'claude', title: 'x', prompt: 'p', phase: 'backlog' };
  const rows = plain(env.w.run('taskSessionTargets(__t, state)'));
  const byName = Object.fromEntries(rows.map((r) => [r.s.name, r]));
  assert.equal(byName.w1.ok, true, 'a working Claude session can take the prompt');
  assert.equal(byName.w1.queue, true);
  assert.equal(byName.w1.note, 'will be queued after the current turn');
  assert.equal(byName.i1.queue, false);
  assert.deepEqual(rows.map((r) => r.s.name), ['i1', 'w1'], 'the session at its prompt ranks first');
  assert.ok(!button(card(env, 20), /^→ w1/), 'the one-tap target is never the queue');
});
