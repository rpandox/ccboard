// Contract tests for the launcher on another node (issue #141; app/static/launcher-node.js, with launcher.js openLauncher and nodes-hub.js): the node picker, the remote form with the
// lists of the chosen node, no danger option, per-node remembered defaults, the optimistic row, the toast with its Open link, every refusal as a plain sentence, Start off with its
// reason, and the strict bodies that leave this board. Real scripts on minidom's DOM (tests/js/hubworld.mjs); the board's answers are a recorder (api()). Nothing opens a connection.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { boardState, hubWorld, iso, page, plain, poll, rec, text, tick } from './hubworld.mjs';
import { projectsOf, sess } from './world.mjs';

const HOSTILE = '<img src=x onerror=alert(1)>';
const LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];
const opt = (key, def) => ({ key, label: key, kind: 'select', choices: null, default: def, help: '', group: 'basic', danger: false, when: null });
const AGENTS = () => ({ agents: [
  { name: 'claude', label: 'Claude', glyph: '◆', installed: true, version: '2.1', logged_in: true, hooks: true, options: [opt('model', 'sonnet'), opt('effort', 'medium')],
    permission_modes: ['manual', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions'], efforts: LEVELS, models: ['sonnet', 'haiku'], reasoning_by_model: {} },
  { name: 'codex', label: 'Codex', glyph: '◇', installed: true, version: '0.145', logged_in: true, hooks: true, options: [opt('model', null)],
    permission_modes: ['default', 'bypassPermissions'], efforts: LEVELS, models: ['gpt-6-sol', 'gpt-6-luna'], reasoning_by_model: { 'gpt-6-sol': LEVELS, 'gpt-6-luna': ['low', 'medium'] } },
] });
const card = (extra = {}) => ({ app: 'ccboard', api: 1, node_id: 'ts:nX', name: 'x', version: '0.5.41', os: { system: 'Linux' }, runtime: 'docker',
  agents: [{ id: 'claude', installed: true, version: '2', logged_in: true, hooks: true, login_problem: false }, { id: 'codex', installed: true, version: '1', logged_in: true, hooks: true, login_problem: false }], ...extra });
const node = (handle, over = {}) => rec(handle, { scopes: ['read', 'tasks', 'sessions'], card: card(), ...over });
const ENVELOPE = (h, data) => ({ node: h, age: 0, data });
const STARTED = { ref: 'build-box:42', id: 42, slug: 'fix-it', tmux: 'shop--api--t-fix-it', branch: 'task/fix-it', phase: 'running',
  task: { id: 42, title: 'Fix it', phase: 'running', agent: 'claude', project: 'shop', repo: 'api', branch: 'task/fix-it', tmux: 'shop--api--t-fix-it', issue_ref: null, updated_at: new Date().toISOString() } };

/** A world with the hub view on, this board's own projects, the nodes read, and an api() that answers by path prefix (value | fn | {__error, status, data}). */
async function lWorld({ nodes = [node('build-box')], answers = {}, local = true, st = {} } = {}) {
  const projects = projectsOf({ ccboard: { ccboard: [sess('ccboard', 'ccboard', 's1')], web: [] } });
  const h = hubWorld({ state: boardState({ projects: local ? projects : [], ...st }), nodes });
  const { w } = h;
  Object.assign(w.ctx, { __answers: { '/api/nodes/build-box/agents': ENVELOPE('build-box', AGENTS()), ...answers }, __gate: null, __nav: [], __links: [] });
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      if (__gate && method === 'POST') await __gate;
      for (const [prefix, v] of Object.entries(__answers)) {
        if (!path.startsWith(prefix)) continue;
        const r = typeof v === 'function' ? v({ method, path, body }) : v;
        if (r && r.__error) { const e = new Error(r.__error); e.status = r.status; e.body = r.data; throw e; }
        return r;
      }
      return { ok: true };
    };
    toast = (t, o) => { __toasts.push({ text: t, kind: o && o.kind }); const n = el('div', { class: 'toast ' + (o && o.kind) }, el('span', { class: 'toast-text', text: t })); document.getElementById('toasts').append(n); return n; };
    navigate = (hash) => { __nav.push(hash); };
    setError = () => {};
  `);
  await poll(w);
  return h;
}
const calls = (w) => plain(w.get('__calls'));
const posts = (w, re) => calls(w).filter((c) => c.method === 'POST' && re.test(c.path));
const sheet = (w) => w.document.getElementById('sheet');
const all = (n, sel) => n.querySelectorAll(sel);
const hidden = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const off = (n) => n.disabled || n.hasAttribute('disabled');
const open = (w, o) => w.run(`openLauncher(${JSON.stringify({ project: 'ccboard', repo: 'ccboard', mode: 'session', ...o })})`);
const picker = (w) => sheet(w).querySelector('.lx-nodepick');
const pickBtns = (w) => all(picker(w), 'button');
const pickNode = (w, label) => { const b = pickBtns(w).find((x) => text(x).trim().startsWith(label)); assert.ok(b, `a picker entry ${label}`); b.click(); return b; };
const remote = (w) => sheet(w).querySelector('form.lx-remote');
const localForm = (w) => all(sheet(w), 'form.lx-form').find((f) => !f.classList.contains('lx-remote'));
const group = (f, label) => all(f, '.seg-ctl').find((n) => n.getAttribute('aria-label') === label);
const labels = (f, label) => all(group(f, label), 'button').map((b) => text(b).trim());
const pressed = (f, label) => all(group(f, label), 'button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => text(b).trim());
const sel = (f, label) => all(f, 'select').find((s) => s.getAttribute('aria-label') === label);
const optionsOf = (s) => all(s, 'option').map((o) => o.getAttribute('value'));
const start = (f) => f.querySelector('button[type=submit]');
const whyOf = (f) => text(f.querySelector('.lx-nodewhy'));
const statusOf = (f) => text(f.querySelector('.form-status'));
const typeInto = (n, v) => { n.value = v; n.dispatchEvent({ type: 'input' }); };
const choose = (s, v) => { s.value = v; s.dispatchEvent({ type: 'change' }); };
const submit = (f) => f.dispatchEvent({ type: 'submit', preventDefault() {} });
const store = (w, key) => { const v = w.localStorage.getItem(key); return v === null ? null : (v.startsWith('{') ? JSON.parse(v) : v); };
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };
const SESSION_KEYS = ['project', 'repo', 'agent', 'name', 'model', 'effort', 'reasoning_effort', 'permission_mode', 'mode'];
const TASK_KEYS = ['project', 'repo', 'title', 'prompt', 'when', 'agent', 'model', 'effort', 'reasoning_effort', 'auto_close', 'issue_ref'];
const DISPATCH_KEYS = ['mode', 'session', 'agent', 'model', 'effort', 'reasoning_effort', 'auto_close'];
const within = (body, keys) => assert.deepEqual(Object.keys(body).filter((k) => !keys.includes(k)), [], `only ${keys.join(', ')}: ${JSON.stringify(body)}`);

// ---------------------------------------------------------------- the picker

test('the picker appears only while the hub view is on and a paired node is online; with it off, or with every node offline, the sheet is the local form alone', async () => {
  const { w } = await lWorld();
  open(w, {});
  assert.ok(picker(w), 'a node is online: the picker is there');
  assert.deepEqual(pickBtns(w).map((b) => text(b).trim()), ['This node', 'build-box']);
  assert.deepEqual(pressed(sheet(w), 'Node'), ['This node'], 'the sheet opens on this node');
  assert.ok(localForm(w), 'the local form is the one shown');
  assert.equal(hidden(localForm(w)), false);

  const none = await lWorld({ nodes: [node('old', { status: 'offline', age_s: 9000 })] });
  open(none.w, {});
  assert.equal(picker(none.w), null, 'only an offline node: no picker');

  const offView = hubWorld({ state: boardState({ nodes_enabled: false, projects: projectsOf({ ccboard: { ccboard: [] } }) }), nodes: [node('build-box')] });
  offView.w.run("api = async () => ({ ok: true })");
  open(offView.w, {});
  assert.equal(picker(offView.w), null, 'the hub view is off: the launcher is what it was');
  assert.ok(all(sheet(offView.w), 'form.lx-form').length === 1);
});

test('four options or fewer are a segmented control, more are a native select', async () => {
  const four = await lWorld({ nodes: [node('a1'), node('b2'), node('c3')] });
  open(four.w, {});
  assert.equal(all(picker(four.w), '.seg-ctl').length, 1);
  assert.equal(pickBtns(four.w).length, 4);
  const five = await lWorld({ nodes: [node('a1'), node('b2'), node('c3'), node('d4')] });
  open(five.w, {});
  assert.equal(all(picker(five.w), '.seg-ctl').length, 0, 'five options: no segmented control');
  const s = picker(five.w).querySelector('select');
  assert.deepEqual(optionsOf(s), ['', 'a1', 'b2', 'c3', 'd4']);
  choose(s, 'c3');
  assert.ok(remote(five.w), 'the select swaps the form too');
  assert.equal(remote(five.w).getAttribute('data-node'), 'c3');
});

test('choosing a node swaps in its projects and repos, its agents, models and efforts; the local form stays hidden and untouched; This node brings it back', async () => {
  const { w } = await lWorld();
  open(w, {});
  const local = localForm(w);
  pickNode(w, 'build-box');
  await settle();
  const f = remote(w);
  assert.ok(f && !hidden(f), 'the remote form is shown');
  assert.ok(hidden(local), 'the local form is hidden');
  assert.deepEqual(pressed(sheet(w), 'Node'), ['build-box']);
  assert.deepEqual(optionsOf(sel(f, 'Project')), ['shop'], "the node's project, not this board's ccboard");
  assert.deepEqual(optionsOf(sel(f, 'Repo')), ['api']);
  assert.deepEqual(labels(f, 'Agent'), ['◆ Claude', '◇ Codex']);
  assert.deepEqual(optionsOf(sel(f, 'Model')), ['', 'sonnet', 'haiku', 'custom'], "the node's models (opus is this board's)");
  assert.deepEqual(optionsOf(sel(f, 'Effort')), ['', ...LEVELS]);
  assert.equal(sel(f, 'Model').value, 'sonnet', "the node's default model");
  assert.equal(sel(f, 'Effort').value, 'medium');
  assert.match(text(sheet(w).querySelector('.sheet-title')), /New session · build-box/);
  pickNode(w, 'This node');
  assert.ok(!hidden(local) && !sheet(w).contains(f), 'the local form is back and the remote one is out of the sheet');
  assert.match(text(sheet(w).querySelector('.sheet-title')), /New session · ccboard\/ccboard/);
});

test("the agents schema is read once per node and kept 10 minutes: opening the sheet again asks nothing; after 10 minutes it asks again", async () => {
  const { w } = await lWorld();
  const asked = () => calls(w).filter((c) => c.path === '/api/nodes/build-box/agents').length;
  open(w, { node: 'build-box' });
  await settle();
  assert.equal(asked(), 1);
  w.run('closeSheet()');
  open(w, { node: 'build-box' });
  await settle();
  assert.equal(asked(), 1, 'cached');
  const later = Date.now() + 11 * 60000;
  w.run(`Date.now = () => ${later}`);
  w.run('closeSheet()');
  open(w, { node: 'build-box' });
  await settle();
  assert.equal(asked(), 2, 'older than 10 minutes: read again');
});

test('the node only offers what it has: a Codex that is not installed there is off with the reason, an agent not logged in too', async () => {
  const c = card({ agents: [{ id: 'claude', installed: true, version: '2', logged_in: true, hooks: true, login_problem: false }, { id: 'codex', installed: false, version: null, logged_in: false, hooks: false, login_problem: false }] });
  const agents = AGENTS();
  agents.agents[1].installed = false; agents.agents[1].logged_in = false;
  const { w } = await lWorld({ nodes: [node('build-box', { card: c })], answers: { '/api/nodes/build-box/agents': ENVELOPE('build-box', agents) } });
  open(w, { node: 'build-box' });
  await settle();
  const f = remote(w);
  const codex = all(group(f, 'Agent'), 'button').find((b) => /Codex/.test(text(b)));
  assert.ok(off(codex));
  assert.match(text(f.querySelector('.lx-why')), /Codex is not installed on build-box/);
  codex.click();
  assert.deepEqual(pressed(f, 'Agent'), ['◆ Claude']);
});

// ---------------------------------------------------------------- no danger option

test('a remote form has no danger option: no bypass, auto or custom mode, no I understand box, no extra arguments, no tools, no directories; the line says why', async () => {
  const { w } = await lWorld();
  open(w, { node: 'build-box' });
  await settle();
  const f = remote(w);
  assert.deepEqual(labels(f, 'Permissions'), ['ask (default)', 'accept edits', 'plan'], 'the schema lists bypass and auto, the form offers neither');
  assert.equal(text(f.querySelector('.lx-nobypass')), 'Bypass is only available on the node itself.');
  assert.doesNotMatch(text(f), /I understand|bypass: never|Extra args|Allowed tools|Append to system|Also give access|worktree/i);
  assert.equal(all(f, 'input[type=checkbox]').filter((c) => !hidden(c)).length, 0, 'a session form shows no checkbox at all');
  // Codex: default and read-only only
  all(group(f, 'Agent'), 'button').find((b) => /Codex/.test(text(b))).click();
  assert.deepEqual(labels(f, 'Codex mode'), ['default', 'read-only']);
  assert.deepEqual(optionsOf(sel(f, 'Model')), ['', 'gpt-6-sol', 'gpt-6-luna', 'custom']);
  // the local form of the same sheet still has its danger gate
  pickNode(w, 'This node');
  assert.ok(localForm(w).querySelector('.lx-danger'), 'the local gate is where it was');
});

test('a bypass value can never reach a body, whatever the form holds', async () => {
  const { w } = await lWorld();
  const body = (R) => plain(w.run(`lxnSessionBody(${JSON.stringify(R)})`));
  const base = { project: 'shop', repo: 'api', agent: 'claude', name: '', claude: { model: 'sonnet', model_custom: '', effort: 'high', permission_mode: 'bypassPermissions' }, codex: { model: '', model_custom: '', reasoning: '', cx_mode: 'bypass' } };
  assert.equal(body(base).permission_mode, undefined);
  assert.equal(body({ ...base, agent: 'codex' }).mode, undefined);
  for (const pm of ['auto', 'dontAsk', 'manual', 'default']) assert.equal(body({ ...base, claude: { ...base.claude, permission_mode: pm } }).permission_mode, undefined, pm);
  assert.equal(body({ ...base, claude: { ...base.claude, permission_mode: 'acceptEdits' } }).permission_mode, 'acceptEdits');
  assert.equal(body({ ...base, claude: { ...base.claude, permission_mode: 'plan' } }).permission_mode, 'plan');
  assert.equal(body({ ...base, agent: 'codex', codex: { ...base.codex, cx_mode: 'read-only' } }).mode, 'read-only');
});

test('the three bodies carry exactly the fields the hub model takes, for every combination of agent, mode and option', async () => {
  const { w } = await lWorld();
  const call = (fn, R, t) => plain(w.run(`${fn}(${JSON.stringify(R)}, ${JSON.stringify(t || null)})`));
  for (const agent of ['claude', 'codex']) for (const perm of ['', 'acceptEdits', 'plan', 'bypassPermissions']) for (const cx of ['default', 'read-only', 'bypass', 'custom']) {
    const R = { project: 'shop', repo: 'api', agent, name: 'n1', title: 'T', prompt: 'do it', issue: 'o/r#1', when: 'later', autoClose: false, where: 'lane', sess: 'shop--api--s1',
      claude: { model: 'custom', model_custom: 'my-model', effort: 'max', permission_mode: perm }, codex: { model: 'gpt-6-sol', model_custom: '', reasoning: 'high', cx_mode: cx } };
    within(call('lxnSessionBody', R), SESSION_KEYS);
    within(call('lxnTaskBody', R), TASK_KEYS);
    within(call('lxnDispatchBody', R, { agent: 'claude' }), DISPATCH_KEYS);
    within(call('lxnDispatchBody', { ...R, where: 'session' }, { agent: 'claude' }), DISPATCH_KEYS);
  }
  const s = call('lxnSessionBody', { project: 'shop', repo: 'api', agent: 'claude', name: '', claude: { model: '', model_custom: '', effort: '', permission_mode: '' }, codex: {} });
  assert.deepEqual(s, { project: 'shop', repo: 'api', agent: 'claude' }, 'nothing but what was chosen: no prompt, no launcher');
  const t = call('lxnTaskBody', { project: 'shop', repo: 'api', agent: 'codex', title: '', prompt: 'Fix the login redirect.\nMore.', when: 'now', autoClose: true, issue: '', claude: {}, codex: { model: '', model_custom: '', reasoning: 'low', cx_mode: 'default' } });
  assert.deepEqual(t, { project: 'shop', repo: 'api', title: 'Fix the login redirect', prompt: 'Fix the login redirect.\nMore.', when: 'now', agent: 'codex', reasoning_effort: 'low' }, 'a title from the prompt; auto close only when off');
  assert.equal(call('lxnTaskBody', { project: 'shop', repo: 'api', agent: 'claude', title: 'x', prompt: 'y', when: 'schedule', autoClose: true, claude: {}, codex: {} }).when, 'now', 'a schedule is not a remote choice');
  assert.deepEqual(call('lxnDispatchBody', { agent: 'codex', where: 'lane', autoClose: true, claude: {}, codex: { model: '', model_custom: '', reasoning: '', cx_mode: 'default' } }, { agent: 'claude' }), { mode: 'lane', auto_close: true, agent: 'codex' });
  assert.deepEqual(call('lxnDispatchBody', { agent: 'claude', where: 'lane', autoClose: true, claude: {}, codex: {} }, { agent: 'claude' }), { mode: 'lane', auto_close: true }, "the task's own agent is not repeated");
});

// ---------------------------------------------------------------- remembered defaults, per node and per repo

test("what is remembered for a node is stored under the node's own keys; the local keys are never written, and another node starts from its own defaults", async () => {
  const { w } = await lWorld({ nodes: [node('build-box'), node('desk-pc')], answers: { '/api/nodes/desk-pc/agents': ENVELOPE('desk-pc', AGENTS()), '/api/nodes/build-box/sessions': ENVELOPE('build-box', { ref: 'build-box/shop--api--s4', tmux: 'shop--api--s4', agent: 'claude', project: 'shop', repo: 'api' }) } });
  w.localStorage.setItem('ccboard:launch:ccboard/ccboard:claude', JSON.stringify({ model: 'haiku' }));
  open(w, { node: 'build-box' });
  await settle();
  let f = remote(w);
  choose(sel(f, 'Model'), 'haiku');
  choose(sel(f, 'Effort'), 'low');
  all(group(f, 'Permissions'), 'button').find((b) => text(b).trim() === 'plan').click();
  submit(f);
  await settle();
  assert.deepEqual(store(w, 'ccboard:launch:build-box/shop/api:claude'), { model: 'haiku', effort: 'low', permission_mode: 'plan' });
  assert.equal(store(w, 'ccboard:agent:build-box/shop/api'), 'claude');
  assert.equal(store(w, 'ccboard:nlast:build-box'), 'shop/api');
  assert.deepEqual(store(w, 'ccboard:launch:ccboard/ccboard:claude'), { model: 'haiku' }, 'the local key is byte for byte what it was');
  assert.deepEqual(plain(w.get('Object.keys(localStorage)')).filter((k) => /ccboard:launch:shop|ccboard:agent:shop|ccboard:task:shop/.test(k)), [], 'no local-shaped key for the remote repo');
  // the same repo name on another node starts from that node's defaults
  w.run('closeSheet()');
  open(w, { node: 'desk-pc' });
  await settle();
  f = remote(w);
  assert.equal(sel(f, 'Model').value, 'sonnet', "desk-pc's own default, not build-box's haiku");
  assert.deepEqual(pressed(f, 'Permissions'), ['ask (default)']);
  // coming back to build-box restores its choices
  w.run('closeSheet()');
  open(w, { node: 'build-box' });
  await settle();
  f = remote(w);
  assert.equal(sel(f, 'Model').value, 'haiku');
  assert.equal(sel(f, 'Effort').value, 'low');
  assert.deepEqual(pressed(f, 'Permissions'), ['plan']);
});

test('a saved default the node no longer offers is explained and replaced by a valid choice', async () => {
  const { w } = await lWorld();
  w.localStorage.setItem('ccboard:launch:build-box/shop/api:claude', JSON.stringify({ model: 'fable', effort: 'ultra' }));
  open(w, { node: 'build-box' });
  await settle();
  const f = remote(w);
  assert.equal(sel(f, 'Model').value, 'sonnet', 'replaced by the node default');
  assert.match(text(f), /Your saved model \(fable\) is not offered by build-box\. Using the default\./);
  assert.match(text(f), /Your saved effort \(ultra\) is not offered by build-box/);
  assert.ok(optionsOf(sel(f, 'Model')).every((v) => v !== 'fable'));
});

// ---------------------------------------------------------------- Start: reasons it is off

test('Start is off with the reason when the node is offline, unauthorized, lacks the scope or has not been read; the reason is on the form, not only in a title', async () => {
  const cases = [
    [node('build-box', { status: 'offline', age_s: 240, state: node('x').state }), /^build-box is offline, last seen 4 minutes ago$/],
    [node('build-box', { status: 'unauthorized' }), /^build-box no longer takes this board's token: re-pair it in Settings, Nodes$/],
    [node('build-box', { status: 'unpaired' }), /^build-box answers as another node: remove it and pair it again$/],
    [node('build-box', { scopes: ['read'] }), /^needs the sessions scope on build-box$/],
    [node('build-box', { legacy: true }), /^build-box was added without a token: pair it to act on it$/],
    [node('build-box', { state: null }), /^build-box has not been read yet$/],
  ];
  for (const [n, re] of cases) {
    const { w } = await lWorld({ nodes: [node('other'), n] });
    open(w, { node: 'build-box' });
    if (n.legacy) { assert.equal(picker(w).querySelectorAll('button').length, 2, 'a node without a token is not in the picker'); continue; }
    await settle();
    const f = remote(w);
    assert.ok(f, JSON.stringify(n.status));
    assert.ok(off(start(f)), `Start is off: ${n.status} ${n.scopes}`);
    assert.match(whyOf(f), re);
    assert.match(start(f).getAttribute('title'), re);
    submit(f);
    await settle();
    assert.equal(posts(w, /api\/nodes/).length, 0, 'nothing is sent');
  }
});

test('Start for a task needs the tasks scope; a node with only sessions is off for it and on for a session', async () => {
  const { w } = await lWorld({ nodes: [node('build-box', { scopes: ['read', 'sessions'] })] });
  open(w, { node: 'build-box', mode: 'task' });
  await settle();
  assert.ok(off(start(remote(w))));
  assert.match(whyOf(remote(w)), /^needs the tasks scope on build-box$/);
  w.run('closeSheet()');
  open(w, { node: 'build-box', mode: 'session' });
  await settle();
  assert.ok(!off(start(remote(w))));
});

// ---------------------------------------------------------------- a session

test('Start opens a session: the body is the strict one, the row shows at once as "Starting on <node>", the answer makes it started, the toast has Open on <node>, and the node page opens', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const { w } = await lWorld({ answers: { '/api/nodes/build-box/sessions': ENVELOPE('build-box', { ref: 'build-box/shop--api--s4', tmux: 'shop--api--s4', agent: 'claude', project: 'shop', repo: 'api' }) } });
  w.ctx.__gate = gate;
  open(w, { node: 'build-box' });
  await settle();
  const f = remote(w);
  assert.equal(text(start(f)), 'Start on build-box');
  assert.equal(all(w.document, '.primary').filter((b) => !hidden(b)).length, 1, 'one filled primary');
  typeInto(f.querySelector('input[placeholder^="auto"]'), 'review');
  submit(f);
  await tick();
  const pending = plain(w.run("Nodes.sessions('build-box').filter((s) => s.pending)"));
  assert.equal(pending.length, 1, 'the row is there before the answer');
  assert.equal(pending[0].pending, 'starting');
  assert.equal(pending[0].session, 'review');
  assert.ok(off(start(f)), 'Start is off while the request is in flight');
  assert.equal(text(start(f)), 'Starting…');
  release();
  await settle();
  const sent = posts(w, /api\/nodes\/build-box\/sessions$/);
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].body, { project: 'shop', repo: 'api', agent: 'claude', name: 'review', model: 'sonnet', effort: 'medium' });
  within(sent[0].body, SESSION_KEYS);
  const row = plain(w.run("Nodes.sessions('build-box').filter((s) => s.pending)"))[0];
  assert.equal(row.pending, 'started');
  assert.equal(row.tmux, 'shop--api--s4');
  assert.equal(w.get('document.getElementById("sheet").open'), false, 'the sheet is closed');
  assert.deepEqual(plain(w.get('__nav')), ['#/n/build-box']);
  const toast = plain(w.get('__toasts'));
  assert.equal(toast[0].text, 'Started on build-box');
  const link = w.document.getElementById('toasts').querySelector('a.toast-link');
  assert.equal(text(link), 'Open on build-box');
  assert.deepEqual([link.getAttribute('href'), link.getAttribute('target'), link.getAttribute('rel')], ['https://build-box.example.ts.net/#/s/shop--api--s4', '_blank', 'noopener noreferrer']);
});

test('a session that the node refuses removes the row and keeps what was typed; the sheet stays open; nothing is retried', async () => {
  const { w } = await lWorld({ answers: { '/api/nodes/build-box/sessions': { __error: 'session s9 already exists', status: 409, data: { error: 'session s9 already exists', reason: 'refused', node: 'build-box' } } } });
  open(w, { node: 'build-box' });
  await settle();
  const f = remote(w);
  typeInto(f.querySelector('input[placeholder^="auto"]'), 's9');
  submit(f);
  await settle();
  assert.equal(plain(w.run("Nodes.sessions('build-box').filter((s) => s.pending)")).length, 0, 'the row is gone');
  assert.equal(statusOf(f), 'session s9 already exists');
  assert.equal(f.querySelector('input[placeholder^="auto"]').value, 's9', 'the name is still there');
  assert.equal(w.get('document.getElementById("sheet").open'), true);
  assert.ok(!off(start(f)), 'the person can try again');
  assert.equal(posts(w, /sessions$/).length, 1, 'one request, never retried');
  assert.deepEqual(plain(w.get('__nav')), []);
});

// ---------------------------------------------------------------- a task

test('Start a task now: title from the prompt, the strict body, the row on the Tasks page at once, the toast with Open on <node> and the limit warning, the Tasks page filtered to the node', async () => {
  const warn = { ...STARTED, limit_warning: { kind: '5h', resets_at: Math.floor(Date.now() / 1000) + 3600, pct: 91 } };
  let release;
  const { w } = await lWorld({ answers: { '/api/nodes/build-box/tasks': ENVELOPE('build-box', warn) } });
  w.ctx.__gate = new Promise((r) => { release = r; });
  open(w, { node: 'build-box', mode: 'task' });
  await settle();
  const f = remote(w);
  assert.deepEqual(labels(f, 'Run'), ['Now', 'Later']);
  assert.equal(all(f, 'input').filter((i) => /owner\/name/.test(i.getAttribute('placeholder') || '')).length, 1, 'the optional issue reference');
  typeInto(f.querySelector('textarea'), 'Fix the login redirect.\nIt loops.');
  submit(f);
  await tick();
  const row = plain(w.run("Nodes.tasks('build-box').filter((t) => t.pending)"))[0];
  assert.equal(row.pending, 'starting');
  assert.equal(row.title, 'Fix the login redirect');
  release();
  await settle();
  const sent = posts(w, /api\/nodes\/build-box\/tasks$/);
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].body, { project: 'shop', repo: 'api', title: 'Fix the login redirect', prompt: 'Fix the login redirect.\nIt loops.', when: 'now', agent: 'claude', model: 'sonnet', effort: 'medium' });
  within(sent[0].body, TASK_KEYS);
  const started = plain(w.run("Nodes.tasks('build-box').filter((t) => t.pending)"))[0];
  assert.deepEqual([started.pending, started.id, started.phase, started.tmux], ['started', 42, 'running', 'shop--api--t-fix-it']);
  const toasts = plain(w.get('__toasts')).map((t) => t.text);
  assert.equal(toasts[0], 'Started on build-box');
  assert.match(toasts[1], /^Started anyway: the 5h usage window is at 91%/, "the peer's limit warning, and a hand start is not held");
  assert.equal(text(w.document.getElementById('toasts').querySelector('a.toast-link')), 'Open on build-box');
  assert.equal(w.get('document.getElementById("toasts").querySelector("a.toast-link").getAttribute("href")'), 'https://build-box.example.ts.net/#/s/shop--api--t-fix-it');
  assert.deepEqual(plain(w.get('__nav')), ['#/tasks']);
  assert.equal(w.run('Nodes.M.tfilter'), 'build-box');
  assert.equal(w.localStorage.getItem('ccboard:nodes:tfilter'), 'build-box');
  assert.deepEqual(store(w, 'ccboard:task:build-box/shop/api'), { model: 'sonnet', effort: 'medium', auto_close: true, when: 'now' });
});

test('Later parks the card in the node\'s backlog; the body says so and the toast does not say Started', async () => {
  const later = { ...STARTED, tmux: null, branch: '', phase: 'backlog', task: { ...STARTED.task, phase: 'backlog', tmux: null } };
  const { w } = await lWorld({ answers: { '/api/nodes/build-box/tasks': ENVELOPE('build-box', later) } });
  open(w, { node: 'build-box', mode: 'task' });
  await settle();
  const f = remote(w);
  all(group(f, 'Run'), 'button').find((b) => text(b) === 'Later').click();
  assert.equal(text(start(f)), "Add to build-box's backlog");
  typeInto(f.querySelector('textarea'), 'Write the migration');
  typeInto(f.querySelectorAll('input').find((i) => /owner\/name/.test(i.getAttribute('placeholder') || '')), 'example/shop-api#12');
  all(f, 'input[type=checkbox]')[0].checked = false;
  all(f, 'input[type=checkbox]')[0].dispatchEvent({ type: 'change' });
  submit(f);
  await settle();
  const body = posts(w, /tasks$/)[0].body;
  assert.equal(body.when, 'later');
  assert.equal(body.issue_ref, 'example/shop-api#12');
  assert.equal(body.auto_close, false);
  assert.equal(plain(w.get('__toasts'))[0].text, 'Added to the backlog on build-box');
  assert.equal(store(w, 'ccboard:task:build-box/shop/api').when, 'later', 'the choice is remembered for this node and repo');
});

test('a task without a prompt, or with a bad issue reference, is stopped in the form: no request, the field says why', async () => {
  const { w } = await lWorld();
  open(w, { node: 'build-box', mode: 'task' });
  await settle();
  const f = remote(w);
  submit(f);
  await settle();
  assert.match(text(f), /Write what it should do\./);
  typeInto(f.querySelector('textarea'), 'go');
  typeInto(f.querySelectorAll('input').find((i) => /owner\/name/.test(i.getAttribute('placeholder') || '')), 'not a ref');
  submit(f);
  await settle();
  assert.match(text(f), /An issue reference looks like owner\/name#123\./);
  assert.equal(posts(w, /api\/nodes/).length, 0);
});

// ---------------------------------------------------------------- every refusal as a plain sentence

const REFUSALS = [
  ['offline', 503, { error: 'x is offline (last answered 4 min ago); nothing was sent', reason: 'offline', node: 'build-box', age: 240 }, 'build-box is offline, last seen 4 minutes ago'],
  ['offline, never answered', 503, { error: 'x', reason: 'offline', node: 'build-box' }, 'build-box has not answered yet'],
  ['scope', 409, { error: 'needs the tasks scope on build-box', reason: 'scope', node: 'build-box' }, 'needs the tasks scope on build-box'],
  ['repair', 409, { error: 'x', reason: 'needs_repair', node: 'build-box' }, "build-box no longer takes this board's token: re-pair it in Settings, Nodes"],
  ['unpaired', 409, { error: 'x', reason: 'unpaired', node: 'build-box' }, 'build-box answers as another node or no longer knows this board: remove it and pair it again'],
  ['not read yet', 409, { error: 'x', reason: 'not_read_yet', node: 'build-box' }, 'build-box has not been read yet: wait a few seconds and try again'],
  ['missing repo', 404, { error: 'api is not on build-box. Nothing was created.', reason: 'repo_missing', node: 'build-box' }, 'api is not on build-box. Nothing was created.'],
  ['missing repo with a clone hint', 404, { error: 'api is not on build-box. It is example/shop-api on GitHub: clone it in Settings > Projects on build-box. Nothing was cloned or created.', reason: 'repo_missing', node: 'build-box' },
    'api is not on build-box. It is example/shop-api on GitHub: clone it in Settings > Projects on build-box. Nothing was cloned or created.'],
  ['timeout after sending', 504, { error: 'x', reason: 'unconfirmed', node: 'build-box' }, 'unconfirmed: build-box did not answer in time; check its board before trying again'],
  ['connection broke', 502, { error: 'x', reason: 'unconfirmed', node: 'build-box' }, 'unconfirmed: the connection to build-box broke; check its board before trying again'],
  ['rate limit', 429, { error: 'slow down', reason: 'rate_limited', node: 'build-box' }, 'Too many requests in a minute. Try again in a moment.'],
  ['the peer\'s own conflict', 409, { error: 'already dispatched', reason: 'refused', node: 'build-box' }, 'already dispatched'],
  ['a guard sentence', 422, { error: 'permission_mode must be default, acceptEdits or plan', reason: 'invalid', node: 'build-box' }, 'permission_mode must be default, acceptEdits or plan'],
];
for (const [name, status, data, want] of REFUSALS) {
  test(`refusal: ${name} reads "${want.slice(0, 60)}" in the form, the row is removed, the typed text stays, and the same request is not sent twice`, async () => {
    const { w } = await lWorld({ answers: { '/api/nodes/build-box/tasks': { __error: data.error, status, data } } });
    open(w, { node: 'build-box', mode: 'task' });
    await settle();
    const f = remote(w);
    typeInto(f.querySelector('textarea'), 'keep this text');
    submit(f);
    await settle();
    assert.equal(statusOf(f), want);
    assert.equal(f.querySelector('textarea').value, 'keep this text');
    assert.equal(plain(w.run("Nodes.tasks('build-box').filter((t) => t.pending)")).length, 0, 'no row left');
    assert.equal(posts(w, /tasks$/).length, 1);
    assert.equal(w.get('document.getElementById("sheet").open'), true);
    assert.doesNotMatch(statusOf(f), /—/, 'no em-dash in UI text');
  });
}

test('a peer\'s words are text: a hostile refusal, project, repo and agent label make no element', async () => {
  const bad = node('build-box', { state: { ...node('x').state, projects: [{ name: 'shop', repos: [{ name: 'api', slug: HOSTILE, branch: HOSTILE, dirty: false }, { name: HOSTILE, slug: null, branch: 'x', dirty: false }] }] } });
  const { w } = await lWorld({ nodes: [bad], answers: { '/api/nodes/build-box/tasks': { __error: HOSTILE, status: 409, data: { error: HOSTILE, reason: 'refused', node: 'build-box' } } } });
  open(w, { node: 'build-box', mode: 'task' });
  await settle();
  const f = remote(w);
  assert.deepEqual(optionsOf(sel(f, 'Repo')), ['api'], 'a repo name the node could not accept is not offered');
  typeInto(f.querySelector('textarea'), 'go');
  submit(f);
  await settle();
  assert.equal(statusOf(f), HOSTILE);
  assert.equal(all(w.document, 'img, script, iframe').length, 0);
});

// ---------------------------------------------------------------- dispatch of a task that lives on the node

test('the dispatch form is for a task of the node: the picker is fixed on it, a lane dispatch posts the strict body, and a running session needs the sessions scope', async () => {
  const n = node('build-box');
  n.state.tasks = [{ id: 9, title: 'Write docs', phase: 'backlog', agent: 'claude', project: 'shop', repo: 'api', branch: '', tmux: null, issue_ref: null, updated_at: iso(60) }];
  n.state.sessions = [{ tmux: 'shop--api--s7', project: 'shop', repo: 'api', session: 's7', agent: 'claude', state: 'idle', needs_you: false, kind: 'user', since: iso(30), model: 'Opus 5' }];
  const { w } = await lWorld({ nodes: [n], answers: { '/api/nodes/build-box/tasks/9/dispatch': ENVELOPE('build-box', { ...STARTED, ref: 'build-box:9', id: 9, tmux: 'shop--api--t-write-docs' }) } });
  const task = plain(w.run("Nodes.tasks('build-box')[0]"));
  w.run(`openLauncher(${JSON.stringify({ mode: 'dispatch', node: 'build-box', remoteTask: task })})`);
  await settle();
  const f = remote(w);
  assert.ok(f && !hidden(f));
  assert.ok(pickBtns(w).find((b) => /This node/.test(text(b))).hasAttribute('disabled'), 'this board cannot take it: the task lives on the node');
  assert.match(text(sheet(w).querySelector('.sheet-title')), /Start “Write docs” on build-box/);
  assert.deepEqual(labels(f, 'Where'), ['New session', 'Running session']);
  submit(f);
  await settle();
  const sent = posts(w, /tasks\/9\/dispatch$/);
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].body, { mode: 'lane', auto_close: true, model: 'sonnet', effort: 'medium' });
  within(sent[0].body, DISPATCH_KEYS);
  assert.equal(plain(w.get('__toasts'))[0].text, 'Started on build-box');
  // a running session
  w.run('closeSheet()');
  w.run(`openLauncher(${JSON.stringify({ mode: 'dispatch', node: 'build-box', remoteTask: task })})`);
  await settle();
  const g = remote(w);
  all(group(g, 'Where'), 'button').find((b) => text(b) === 'Running session').click();
  assert.deepEqual(optionsOf(sel(g, 'Session')), ['shop--api--s7']);
  submit(g);
  await settle();
  assert.deepEqual(posts(w, /tasks\/9\/dispatch$/)[1].body, { mode: 'session', session: 'shop--api--s7', auto_close: true });
});

test('a dispatch into a session needs the sessions scope: the choice is off with the reason, and nothing is sent', async () => {
  const n = node('build-box', { scopes: ['read', 'tasks'] });
  n.state.tasks = [{ id: 9, title: 'Write docs', phase: 'backlog', agent: 'claude', project: 'shop', repo: 'api', branch: '', tmux: null, issue_ref: null, updated_at: iso(60) }];
  const { w } = await lWorld({ nodes: [n] });
  const task = plain(w.run("Nodes.tasks('build-box')[0]"));
  w.run(`openLauncher(${JSON.stringify({ mode: 'dispatch', node: 'build-box', remoteTask: task })})`);
  await settle();
  const f = remote(w);
  const b = all(group(f, 'Where'), 'button').find((x) => text(x) === 'Running session');
  assert.ok(off(b));
  assert.equal(b.getAttribute('title'), 'needs the sessions scope on build-box');
  b.click();
  assert.deepEqual(pressed(f, 'Where'), ['New session']);
});

test('a local task cannot be dispatched to another node: the dispatch sheet of this board has no picker', async () => {
  const { w } = await lWorld();
  const task = { id: 3, title: 'Local', prompt: 'p', project: 'ccboard', repo: 'ccboard', agent: 'claude', phase: 'backlog', slug: 'local', tmux: '' };
  w.run(`openLauncher(${JSON.stringify({ project: 'ccboard', repo: 'ccboard', mode: 'dispatch', task })})`);
  assert.equal(picker(w), null);
});

// ---------------------------------------------------------------- the node page opens it

test('New task here and New session here on the node page open the launcher with that node chosen when the scope is there; without it they stay off with the reason', async () => {
  const { w } = await lWorld({ nodes: [node('build-box'), node('read-only', { scopes: ['read'] })] });
  w.location.hash = '#/n/build-box';
  await tick();
  const acts = page(w).querySelector('.nd-acts');
  const bt = all(acts, 'button').find((b) => text(b) === 'New task here');
  assert.ok(!bt.classList.contains('nd-off'));
  bt.click();
  await settle();
  const f = remote(w);
  assert.ok(f, 'the launcher opened');
  assert.equal(f.getAttribute('data-node'), 'build-box');
  assert.deepEqual(pressed(sheet(w), 'Node'), ['build-box']);
  assert.match(text(sheet(w).querySelector('.sheet-title')), /^New task · build-box/);
  w.run('closeSheet()');
  w.location.hash = '#/n/read-only';
  await tick();
  const offs = all(page(w), '.nd-acts button.nd-off').map(text);
  assert.deepEqual(offs, ['New task here', 'New session here']);
  assert.match(text(page(w).querySelector('.nd-whys')), /needs the tasks scope on read-only/);
});

test('with no local repo the sheet still opens on the node and This node is off with its reason', async () => {
  const { w } = await lWorld({ local: false });
  w.run(`openLauncher(${JSON.stringify({ mode: 'session', node: 'build-box' })})`);
  await settle();
  assert.ok(remote(w));
  const b = pickBtns(w).find((x) => /This node/.test(text(x)));
  assert.ok(off(b));
});

// ---------------------------------------------------------------- the files

test('launcher-node.js is definition only, builds no markup from text and keeps the rules of the frontend', () => {
  const src = fs.readFileSync(path.join(STATIC, 'launcher-node.js'), 'utf8');
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
  assert.doesNotMatch(code, /innerHTML|insertAdjacentHTML|outerHTML|document\.write|\beval\(|new Function|style\s*=|cssText|\.style\./);
  assert.doesNotMatch(src, /—/, 'no em-dash');
  assert.doesNotMatch(src, /fetch\(|XMLHttpRequest|EventSource|WebSocket/, 'the page talks to its own board through api() only');
  const first = src.split('\n').find((l) => l.startsWith("'use strict'"));
  assert.ok(first);
});

// ---------------------------------------------------------------- ?demo=1, through core.js's demo handlers and the real fixtures

import { setState } from './hubworld.mjs';

const slow = async () => { await new Promise((r) => setTimeout(r, 260)); await settle(); };          // the demo handler waits 150 ms before it answers a write

async function demoWorld() {
  const h = hubWorld({ search: '?demo=1', realApi: true });
  const { w } = h;
  w.run(`
    toast = (t, o) => { __toasts.push({ text: t, kind: o && o.kind }); const n = el('div', { class: 'toast ' + (o && o.kind) }, el('span', { class: 'toast-text', text: t })); document.getElementById('toasts').append(n); return n; };
    navigate = (hash) => { __nav.push(hash); };
    setError = () => {};
  `);
  w.ctx.__nav = [];
  setState(w, plain(await w.run("api('GET', '/api/state')")));
  await poll(w);
  return h;
}

test('demo: the picker lists the three fixture nodes, a remote session and task start are played in the page, and each way a start can be off is one tap away', async () => {
  const { w } = await demoWorld();
  const proj = plain(w.run('state.projects[0]'));
  const repo = proj.repos[0].name;
  w.run(`openLauncher(${JSON.stringify({ project: proj.name, repo, mode: 'session' })})`);
  await settle();
  assert.deepEqual(pickBtns(w).map((b) => text(b).trim()), ['This node', 'build-box', 'alice-mac ◐', 'old-laptop ○']);
  pickNode(w, 'build-box');
  await settle();
  let f = remote(w);
  assert.deepEqual(optionsOf(sel(f, 'Project')), ['shop', 'infra']);
  assert.deepEqual(optionsOf(sel(f, 'Model')), ['', 'opus', 'sonnet', 'haiku', 'custom'], "the fixture node's own models");
  assert.ok(!off(start(f)));
  submit(f);
  await slow();
  assert.equal(plain(w.get('__toasts'))[0].text, 'Started on build-box');
  assert.match(text(w.document.getElementById('toasts').querySelector('a.toast-link')), /^Open on build-box$/);
  assert.deepEqual(plain(w.get('__nav')), ['#/n/build-box']);
  assert.equal(plain(w.run("Nodes.sessions('build-box').filter((s) => s.pending).length")), 1);

  w.run('openLauncher({ mode: "task", node: "build-box" })');
  await settle();
  f = remote(w);
  choose(sel(f, 'Effort'), 'max');
  typeInto(f.querySelector('textarea'), 'Add a cart test');
  submit(f);
  await slow();
  const toasts = plain(w.get('__toasts')).map((t) => t.text);
  assert.ok(toasts.includes('Started on build-box'));
  assert.ok(toasts.some((t) => /^Started anyway: the 5h usage window is at 91%/.test(t)), 'effort max shows the limit warning in the demo');
  assert.equal(plain(w.run("Nodes.tasks('build-box').filter((t) => t.pending).length")), 1);

  w.run('closeSheet()');
  w.run(`openLauncher(${JSON.stringify({ project: proj.name, repo, mode: 'session' })})`);
  await settle();
  pickNode(w, 'alice-mac');
  assert.match(whyOf(remote(w)), /^needs the sessions scope on alice-mac$/);
  w.run('closeSheet()');
  w.run(`openLauncher(${JSON.stringify({ project: proj.name, repo, mode: 'task' })})`);
  await settle();
  pickNode(w, 'old-laptop');
  assert.match(whyOf(remote(w)), /^old-laptop is offline, last seen 6 hours ago$/);
  assert.ok(off(start(remote(w))));
});

test('demo: a repo the node does not list is the missing-repo refusal, played by the demo handler', async () => {
  const { w } = await demoWorld();
  w.ctx.__a = ['POST', '/api/nodes/build-box/sessions', { project: 'shop', repo: 'ghost', agent: 'claude' }];
  const err = await w.run('api(__a[0], __a[1], __a[2]).then(() => null, (e) => e)');
  assert.deepEqual([err.status, err.body.reason, err.message], [404, 'repo_missing', 'ghost is not on build-box. Nothing was created.']);
});
