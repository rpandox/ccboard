// Contract tests for Auto in the launcher's node picker (issue #150; app/static/launcher-node.js lxnAuto): the Auto cell for a new task, the question put to the board, the node and the
// reason in the command preview before Start, the waiting state with the nodes that were left out, Check again, the agent it asks for, and that nothing starts on a guess. Real scripts on
// minidom's DOM (tests/js/hubworld.mjs); the board's answers are a recorder (api()). Nothing opens a connection.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { boardState, hubWorld, iso, plain, poll, rec, text, tick } from './hubworld.mjs';
import { projectsOf, sess } from './world.mjs';

const LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];
const opt = (key, def) => ({ key, label: key, kind: 'select', choices: null, default: def, help: '', group: 'basic', danger: false, when: null });
const AGENTS = () => ({ agents: [
  { name: 'claude', label: 'Claude', glyph: '◆', installed: true, version: '2.1', logged_in: true, hooks: true, options: [opt('model', 'sonnet'), opt('effort', 'medium')],
    permission_modes: ['manual', 'acceptEdits', 'plan'], efforts: LEVELS, models: ['sonnet', 'haiku'], reasoning_by_model: {} },
  { name: 'codex', label: 'Codex', glyph: '◇', installed: true, version: '0.145', logged_in: true, hooks: true, options: [opt('model', null)],
    permission_modes: ['default'], efforts: LEVELS, models: ['gpt-6-sol'], reasoning_by_model: { 'gpt-6-sol': LEVELS } },
] });
const card = () => ({ app: 'ccboard', api: 1, node_id: 'ts:nX', name: 'x', version: '0.5.41', os: { system: 'Linux' }, runtime: 'docker',
  agents: [{ id: 'claude', installed: true, version: '2', logged_in: true, hooks: true, login_problem: false }, { id: 'codex', installed: true, version: '1', logged_in: true, hooks: true, login_problem: false }] });
const node = (handle, over = {}) => rec(handle, { scopes: ['read', 'tasks', 'sessions'], card: card(), ...over });
const ENVELOPE = (h, data) => ({ node: h, age: 0, data });
const STARTED = { ref: 'build-box:42', id: 42, slug: 'fix-it', tmux: 'shop--api--t-fix-it', branch: 'task/fix-it', phase: 'running',
  task: { id: 42, title: 'Fix it', phase: 'running', agent: 'claude', project: 'shop', repo: 'api', branch: 'task/fix-it', tmux: 'shop--api--t-fix-it', issue_ref: null, updated_at: new Date().toISOString() } };
const CHOSEN = { handle: 'build-box', name: 'build-box', why: 'chose build-box: 2 free lanes, 5-hour window 31 percent', waiting: null, pin: null, refused: [], ranked: [], agent: 'claude', slug: 'example/shop-api' };
const WAITING = { handle: null, name: null, why: 'waiting: build-box 5-hour window is at 90 percent until 14:10', waiting: { code: 'window', reason: 'build-box 5-hour window is at 90 percent until 14:10', until: 1, until_iso: '2026-10-11T14:10:00Z', text: 'waiting: build-box 5-hour window is at 90 percent until 14:10', pinned: null },
  pin: null, refused: [{ handle: 'build-box', name: 'build-box', code: 'window', text: '5-hour window is at 90 percent until 14:10', until: 1 }, { handle: 'old', name: 'old', code: 'offline', text: 'is offline', until: null }], ranked: [], agent: 'claude', slug: 'example/shop-api' };

async function aWorld({ nodes = [node('build-box')], route = CHOSEN, extra = {} } = {}) {
  const projects = projectsOf({ ccboard: { ccboard: [sess('ccboard', 'ccboard', 's1')], web: [] } });
  const h = hubWorld({ state: boardState({ projects }), nodes });
  const { w } = h;
  Object.assign(w.ctx, { __answers: { '/api/route/auto': route, '/api/nodes/build-box/agents': ENVELOPE('build-box', AGENTS()), '/api/nodes/build-box/tasks': ENVELOPE('build-box', STARTED), ...extra }, __gate: null, __nav: [] });
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      if (__gate && path.startsWith('/api/route/auto')) await __gate;
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
const asks = (w) => calls(w).filter((c) => c.path.startsWith('/api/route/auto'));
const posts = (w, re) => calls(w).filter((c) => c.method === 'POST' && re.test(c.path));
const sheet = (w) => w.document.getElementById('sheet');
const all = (n, sel) => n.querySelectorAll(sel);
const hidden = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const off = (n) => n.disabled === true || n.hasAttribute('disabled');
const open = (w, o) => w.run(`openLauncher(${JSON.stringify({ project: 'ccboard', repo: 'ccboard', mode: 'task', ...o })})`);
const picker = (w) => sheet(w).querySelector('.lx-nodepick');
const pickBtns = (w) => all(picker(w), 'button');
const pickAuto = (w) => { const b = pickBtns(w).find((x) => text(x).trim() === 'Auto'); assert.ok(b, 'an Auto cell'); b.click(); return b; };
const remote = (w) => sheet(w).querySelector('form.lx-remote');
const autoBox = (w) => sheet(w).querySelector('.lx-auto');
const stateOf = (w) => text(autoBox(w).querySelector('[data-auto-state]'));
const localForm = (w) => all(sheet(w), 'form.lx-form').find((f) => !f.classList.contains('lx-remote'));
const group = (f, label) => all(f, '.seg-ctl').find((n) => n.getAttribute('aria-label') === label);
const typeInto = (n, v) => { n.value = v; n.dispatchEvent({ type: 'input' }); };
const submit = (f) => f.dispatchEvent({ type: 'submit', preventDefault() {} });
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };

// ---------------------------------------------------------------- the cell

test('Auto is a cell of the picker for a new task in a repo of this board, after This node; a session, a dispatch and a board with no online node have none', async () => {
  const { w } = await aWorld();
  open(w, {});
  assert.deepEqual(pickBtns(w).map((b) => text(b).trim()), ['This node', 'Auto', 'build-box']);
  assert.match(pickBtns(w)[1].getAttribute('title'), /The board picks the node that has this repository, the agent ready, a free lane and room in its rate-limit window/);
  assert.equal(asks(w).length, 0, 'opening the sheet asks nothing: the question is put when Auto is picked');
  const ses = await aWorld();
  open(ses.w, { mode: 'session' });
  assert.deepEqual(pickBtns(ses.w).map((b) => text(b).trim()), ['This node', 'build-box'], 'a session has no Auto');
  const off1 = await aWorld({ nodes: [node('old', { status: 'offline', age_s: 9000 })] });
  open(off1.w, {});
  assert.equal(picker(off1.w), null);
});

test('with more than four entries the picker is a native select and Auto is its second option', async () => {
  const { w } = await aWorld({ nodes: [node('a1'), node('b2'), node('c3')] });
  open(w, {});
  const s = picker(w).querySelector('select');
  assert.deepEqual(all(s, 'option').map((o) => o.getAttribute('value')), ['', '~auto', 'a1', 'b2', 'c3']);
});

// ---------------------------------------------------------------- the choice

test('picking Auto asks the board about this repo and the agent, says "Choosing a node..." while it answers, and hides the local form', async () => {
  let release;
  const { w } = await aWorld();
  w.ctx.__gate = new Promise((r) => { release = r; });
  open(w, {});
  const local = localForm(w);
  pickAuto(w);
  await tick();
  assert.ok(hidden(local), 'the local form is out of the way');
  assert.equal(stateOf(w), 'Choosing a node…');
  assert.ok(off(autoBox(w).querySelector('[data-auto-start]')), 'Start waits for the answer');
  assert.deepEqual(asks(w).map((c) => [c.method, c.path]), [['GET', '/api/route/auto?agent=claude&project=ccboard&repo=ccboard']]);
  assert.match(sheet(w).textContent, /New task · Auto/);
  release();
  await settle();
  assert.ok(remote(w), 'the node came back: its form is shown');
});

test('the node and the reason are in the command preview before Start; the form is the node\'s own, in the repo the board named, with the agent fixed', async () => {
  const { w } = await aWorld();
  open(w, {});
  pickAuto(w);
  await settle();
  const f = remote(w);
  assert.equal(f.getAttribute('data-node'), 'build-box');
  const head = text(f.querySelector('.lx-cmdhead'));
  assert.equal(head, 'Auto: command on build-box');
  assert.match(text(f.querySelector('.lx-cmdbox')), /Auto chose build-box: 2 free lanes, 5-hour window 31 percent\. Approximate: build-box builds the real command\./);
  assert.match(text(f.querySelector('button[type=submit]')), /^Start on build-box$/);
  const sel = (label) => all(f, 'select').find((s) => s.getAttribute('aria-label') === label);
  assert.equal(sel('Project').value, 'shop');
  assert.equal(sel('Repo').value, 'api', 'the node\'s own name for the repository the board named by its slug');
  assert.ok(hidden(group(f, 'Agent')), 'the agent is the one Auto asked for: the form does not change it');
  assert.equal(hidden(autoBox(w).querySelector('[data-auto-state]')), true);
});

test('Start sends the node form\'s ordinary task body to the chosen node and nowhere else', async () => {
  const { w } = await aWorld();
  open(w, {});
  pickAuto(w);
  await settle();
  typeInto(remote(w).querySelector('textarea'), 'Fix the login redirect.');
  submit(remote(w));
  await settle();
  const sent = posts(w, /^\/api\/nodes\//);
  assert.deepEqual(sent.map((c) => c.path), ['/api/nodes/build-box/tasks']);
  assert.deepEqual(sent[0].body, { project: 'shop', repo: 'api', title: 'Fix the login redirect', prompt: 'Fix the login redirect.', when: 'now', agent: 'claude', model: 'sonnet', effort: 'medium' });
  assert.deepEqual(Object.keys(sent[0].body).filter((k) => !['project', 'repo', 'title', 'prompt', 'when', 'agent', 'model', 'effort', 'reasoning_effort', 'auto_close', 'issue_ref'].includes(k)), []);
  assert.equal(posts(w, /route/).length, 0, 'asking Auto is a read: it starts nothing');
});

test('the agent Auto asks for is a segmented control of its own: Codex asks again with agent=codex and keeps what was typed', async () => {
  const { w } = await aWorld({ route: (req) => ({ ...CHOSEN, agent: /agent=codex/.test(req.path) ? 'codex' : 'claude', why: /agent=codex/.test(req.path) ? 'chose build-box: 2 free lanes, weekly window 12 percent' : CHOSEN.why }) });
  open(w, {});
  pickAuto(w);
  await settle();
  typeInto(remote(w).querySelector('textarea'), 'Write the tests');
  const codex = all(autoBox(w), '.seg-ctl').find((n) => n.getAttribute('aria-label') === 'Agent');
  all(codex, 'button').find((b) => /Codex/.test(text(b))).click();
  await settle();
  assert.deepEqual(asks(w).map((c) => c.path.match(/agent=([a-z]+)/)[1]), ['claude', 'codex']);
  assert.match(text(remote(w).querySelector('.lx-cmdbox')), /weekly window 12 percent/);
  assert.equal(remote(w).querySelector('textarea').value, 'Write the tests', 'what was typed is carried over');
  submit(remote(w));
  await settle();
  assert.equal(posts(w, /tasks$/)[0].body.agent, 'codex');
});

// ---------------------------------------------------------------- waiting

test('when no node can take it Auto says "waiting: <reason>", lists the nodes left out and why, builds no form and offers Check again', async () => {
  const { w } = await aWorld({ route: WAITING });
  open(w, {});
  pickAuto(w);
  await settle();
  assert.equal(remote(w), null, 'no form: nothing can start');
  assert.match(stateOf(w), /^waiting: build-box 5-hour window is at 90 percent until 14:10\. Pick a node above to start it there, or check again\.$/);
  assert.deepEqual(all(autoBox(w), '.lx-autoleft li').map((li) => text(li).trim()), ['∙ build-box 5-hour window is at 90 percent until 14:10', '∙ old is offline']);
  const start = autoBox(w).querySelector('[data-auto-start]');
  assert.ok(off(start));
  assert.match(start.getAttribute('title'), /^waiting: build-box 5-hour window/);
  assert.equal(posts(w, /^\/api\/nodes\//).length, 0);
  assert.ok(!off(autoBox(w).querySelector('[data-auto-again]')));
});

test('Check again asks the board once more, and a node that has room by then replaces the wait with its form', async () => {
  let n = 0;
  const { w } = await aWorld({ route: () => (++n === 1 ? WAITING : CHOSEN) });
  open(w, {});
  pickAuto(w);
  await settle();
  assert.equal(remote(w), null);
  autoBox(w).querySelector('[data-auto-again]').click();
  await settle();
  assert.equal(asks(w).length, 2);
  assert.ok(remote(w));
  assert.equal(remote(w).getAttribute('data-node'), 'build-box');
});

test('an answer that cannot be used is said in words and never starts anything: an error, a node that does not list the repo, a handle that is not a paired node', async () => {
  const err = await aWorld({ route: { __error: 'x', status: 500, data: { error: 'the board could not read its nodes' } } });
  open(err.w, {});
  pickAuto(err.w);
  await settle();
  assert.match(stateOf(err.w), /^Auto could not ask the board: the board could not read its nodes\. Check again, or pick a node above\.$/);
  assert.equal(remote(err.w), null);
  const noRepo = await aWorld({ route: { ...CHOSEN, slug: 'example/other' } });
  open(noRepo.w, {});
  pickAuto(noRepo.w);
  await settle();
  assert.match(stateOf(noRepo.w), /^Auto chose build-box, but its last reading does not list example\/other\./);
  assert.equal(remote(noRepo.w), null, 'a node is never given a repository it does not list');
  const ghost = await aWorld({ route: { ...CHOSEN, handle: 'ghost-9' } });
  open(ghost.w, {});
  pickAuto(ghost.w);
  await settle();
  assert.equal(remote(ghost.w), null);
  assert.match(stateOf(ghost.w), /^waiting: no node can take it now/);
  for (const x of [err, noRepo, ghost]) assert.equal(posts(x.w, /^\/api\/nodes\//).length, 0);
});

test('text from the board in an Auto answer is shown as text: a hostile reason or node name makes no element', async () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const { w } = await aWorld({ route: { ...WAITING, waiting: { ...WAITING.waiting, text: `waiting: ${hostile}` }, refused: [{ handle: 'x', name: hostile, code: 'offline', text: hostile, until: null }] } });
  open(w, {});
  pickAuto(w);
  await settle();
  assert.equal(autoBox(w).querySelectorAll('img').length, 0);
  assert.match(stateOf(w), /<img src=x/);
});

test('This node brings the local form back unchanged, and Auto asked again when it is picked a second time', async () => {
  const { w } = await aWorld();
  open(w, {});
  const local = localForm(w);
  pickAuto(w);
  await settle();
  pickBtns(w).find((b) => text(b).trim() === 'This node').click();
  assert.ok(!hidden(local));
  assert.equal(autoBox(w), null, 'Auto is taken out of the sheet');
  pickAuto(w);
  await settle();
  assert.equal(asks(w).length, 2, 'a second pick asks again: the answer may have changed');
  assert.ok(!hidden(autoBox(w)));
});

// ---------------------------------------------------------------- the demo

async function demoWorld(search) {
  const h = hubWorld({ search, realApi: true });
  const { w } = h;
  w.run(`
    toast = (t, o) => { __toasts.push({ text: t, kind: o && o.kind }); const n = el('div', { class: 'toast ' + (o && o.kind) }, el('span', { class: 'toast-text', text: t })); document.getElementById('toasts').append(n); return n; };
    navigate = (hash) => { __nav.push(hash); };
    setError = () => {};
  `);
  w.ctx.__nav = [];
  w.run(`state = ${JSON.stringify(plain(await w.run("api('GET', '/api/state')")))}`);
  await poll(w);
  return h;
}

test('demo: Auto picks build-box with its reason, and ?auto=wait plays the wait with the nodes left out', async () => {
  const { w } = await demoWorld('?demo=1');
  const proj = plain(w.run('state.projects[0]'));
  w.run(`openLauncher(${JSON.stringify({ project: proj.name, repo: proj.repos[0].name, mode: 'task' })})`);
  await settle();
  const btns = all(picker(w), 'button').length ? all(picker(w), 'button') : [];
  assert.ok(btns.length === 0, 'three fixture nodes make five entries: a native select');
  const s = picker(w).querySelector('select');
  assert.deepEqual(all(s, 'option').map((o) => o.getAttribute('value')).slice(0, 3), ['', '~auto', 'build-box']);
  s.value = '~auto';
  s.dispatchEvent({ type: 'change' });
  await settle();
  await settle();
  assert.ok(remote(w), 'the demo node came back');
  assert.equal(remote(w).getAttribute('data-node'), 'build-box');
  assert.match(text(remote(w).querySelector('.lx-cmdbox')), /Auto chose build-box: 2 free lanes, 5-hour window 31 percent\./);

  const wait = await demoWorld('?demo=1&auto=wait');
  const proj2 = plain(wait.w.run('state.projects[0]'));
  wait.w.run(`openLauncher(${JSON.stringify({ project: proj2.name, repo: proj2.repos[0].name, mode: 'task' })})`);
  await settle();
  const s2 = picker(wait.w).querySelector('select');
  s2.value = '~auto';
  s2.dispatchEvent({ type: 'change' });
  await settle();
  await settle();
  assert.equal(remote(wait.w), null);
  assert.match(stateOf(wait.w), /^waiting: build-box 5-hour window is at 90 percent until 14:10; old-laptop is offline\./);
  assert.equal(all(autoBox(wait.w), '.lx-autoleft li').length, 2);
});
