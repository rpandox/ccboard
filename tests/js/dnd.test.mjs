// Contract tests for dnd.js (v0.5.15, namespace Dnd): the accept rules, the drag lifecycle and its cleanup, the confirm popover and the dispatch
// bodies of a lane drop vs a session drop, the optimistic move through store.tasksOverride and its release when the poll agrees, the data-drop
// attributes on the dispatch bar's lanes and chips (components.js), the session rows (agents.js) and the sidebar (shell.js), and the cancel rules of
// the long press the Move sheet uses (components.js longPress: touch is the Move sheet's path, a mouse drags).
// The real scripts in index.html order on minidom's DOM. minidom's dispatchEvent bubbles through elements only, so a document-level event
// (what dnd.js listens for) is fired with document.dispatch(type, event) and carries its own target; window events go through w.fire.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, plain } from './harness.mjs';
import { fixtureState, sess } from './world.mjs';
import { makeNetworkWorld, settle } from './treekit.mjs';

const PAGE_FILES = ['home', 'inbox', 'tasks', 'agents', 'doctor', 'settings', 'search', 'session', 'onboarding', 'placeholders'];
const T0 = 1790000000000;
const agoIso = (min) => new Date(T0 - min * 60000).toISOString();
const all = (root, sel) => root.querySelectorAll(sel);

/** A state.tasks row the way _tasks_view makes it, `over` on top. */
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

/**
 * petroit/api: s1 done, s2 errored, s3 waiting (no wait_kind: a dialog), s4 working, s5 waiting on a permission, s6 waiting at its idle prompt;
 * petroit/web: w1 idle; ccboard: s1 idle + a working Codex cx1; phasezero: an ended s1 in the project folder. Both agents installed.
 */
function stateWith(tasks = [BACKLOG()], extra = {}) {
  const st = fixtureState({ tasks, ...extra });
  const petroit = st.projects.find((p) => p.name === 'petroit');
  const api = petroit.repos.find((r) => r.name === 'api');
  api.sessions.push(
    sess('petroit', 'api', 's4', { state: 'working', last_prompt: 'migrate the devices table' }),
    sess('petroit', 'api', 's5', { state: 'waiting', needs_attention: true, wait_kind: 'permission_prompt' }),
    sess('petroit', 'api', 's6', { state: 'waiting', wait_kind: 'idle_prompt' }));
  petroit.repos.push({ name: 'web', path: '/srv/projects/petroit/web', state: 'ok', branch: 'main', dirty: false, devcontainer: false,
    sessions: [sess('petroit', 'web', 'w1', { state: 'idle' })] });
  st.pending_permissions.push({ id: 31, tmux_name: 'petroit--api--s5', tool_name: 'Bash', summary: 'Bash: npm run seed', created_at: new Date().toISOString() });
  st.agents = { claude: { installed: true }, codex: { installed: true } };
  return st;
}

/**
 * A world with the pages, dnd.js and the fake network. `fine` says whether (pointer: fine) matches. api() is the fake of treekit.mjs: every call
 * lands in __calls, answers come from server.answers; toast() and poll() are recorders and navigate() records. poll() never repaints.
 */
function dndWorld({ state = stateWith(), fine = true, answers = {}, extra = {}, install = true, launcher = 'legacy' } = {}) {
  const env = makeNetworkWorld({ extra: { matchMedia: (q) => ({ matches: fine && /pointer:\s*fine/.test(q), addEventListener() {}, removeEventListener() {} }), ...extra } });
  const { w, server } = env;
  Object.assign(server.answers, answers);
  for (const f of ['live.js', 'launcher.js', 'tree.js', 'router.js', 'pages/widgets.js', 'shell.js', 'dnd.js']) w.load(f);
  w.ctx.__toasts = []; w.ctx.__nav = []; w.ctx.__polls = 0;
  w.run(`
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    poll = async () => { __polls++; };
    navigate = (hash) => { __nav.push(hash); };
    Live.subscribe = () => () => {};
    Live.unsubscribe = () => {};
  `);
  for (const f of PAGE_FILES) if (fs.existsSync(path.join(STATIC, 'pages', `${f}.js`))) w.load(`pages/${f}.js`);
  w.ctx.__st = state;
  w.run('state = __st');
  // v0.5.13: Edit options… goes through launch() (components.js): the launcher in dispatch mode when openLauncher exists, else taskDispatchSheet. 'legacy' (the default) takes
  // openLauncher away so these tests pin the older sheet; 'stub' makes it a recorder (__launched); 'real' leaves launcher.js's own.
  if (launcher === 'legacy') w.run('openLauncher = undefined');
  else if (launcher === 'stub') w.run('globalThis.__launched = []; globalThis.openLauncher = (o) => { __launched.push(o); return true; };');
  const Dnd = w.get('Dnd');
  if (install) Dnd.install();            // in the app the first target that is drawn (a row, a lane bar) does this
  return { ...env, state, Dnd, page: () => w.document.querySelector('#page') };
}

const calls = (env) => plain(env.w.get('__calls'));
const posts = (env, re = /dispatch/) => calls(env).filter((c) => c.method === 'POST' && re.test(c.path));
const toasts = (env) => plain(env.w.get('__toasts'));

// ---------------------------------------------------------------- synthetic events

function dataTransfer() {
  return { data: {}, effectAllowed: '', dropEffect: '', setData(k, v) { this.data[k] = String(v); }, getData(k) { return this.data[k] || ''; } };
}
function mkEvent(type, extra = {}) {
  const e = { type, defaultPrevented: false, preventDefault() { e.defaultPrevented = true; }, stopPropagation() {}, clientX: 100, clientY: 100, ...extra };
  return e;
}
/** Fire a document-level event at `target` (dnd.js delegates on document). Returns the event so a test can read defaultPrevented / dataTransfer. */
const fire = (env, type, target, extra = {}) => { const e = mkEvent(type, { target, ...extra }); env.w.document.dispatch(type, e); return e; };

/** A Backlog card node and a drop target in the document body, the way the pages draw them. */
function card(env, id = 20, phase = 'backlog') {
  const n = env.w.document.createElement('div');
  n.className = 'task backlog';
  n.setAttribute('data-task', String(id));
  n.setAttribute('data-phase', phase);
  env.w.document.body.append(n);
  return n;
}
function target(env, attrs) {
  const n = env.w.document.createElement('div');
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  env.w.document.body.append(n);
  return n;
}
const sessionTarget = (env, tmux) => target(env, { 'data-drop': 'session', 'data-tmux': tmux });
const laneTarget = (env, agent, project) => target(env, { 'data-drop': 'lane', 'data-agent': agent, ...(project ? { 'data-project': project } : {}) });

/** Start a drag of task `id` from a card (a mouse first moves over it, which is what makes it draggable). */
function startDrag(env, id = 20) {
  const c = card(env, id);
  fire(env, 'pointerover', c, { pointerType: 'mouse' });
  const dt = dataTransfer();
  const e = fire(env, 'dragstart', c, { dataTransfer: dt });
  return { card: c, dt, e };
}

const bodyHas = (env, cls) => env.w.document.body.classList.contains(cls);
const popover = (env) => env.w.document.querySelector('dialog.dnd-pop');
const hint = (env) => env.w.document.querySelector('.dnd-hint');

// ---------------------------------------------------------------- accept rules

test('accept rules: lanes take a backlog task, Codex only once it is installed', () => {
  const env = dndWorld();
  const D = env.Dnd;
  const t = BACKLOG();
  assert.deepEqual(plain(D.verdict(t, { kind: 'lane', agent: 'claude' }, env.state)), { ok: true, kind: 'lane', agent: 'claude', note: '', autoClose: true });
  assert.equal(D.verdict(t, { kind: 'lane', agent: 'codex' }, env.state).ok, true);
  env.state.agents.codex.installed = false;
  const no = D.verdict(t, { kind: 'lane', agent: 'codex' }, env.state);
  assert.equal(no.ok, false);
  assert.match(no.why, /Codex is not installed/);
  assert.equal(D.verdict(t, { kind: 'lane', agent: 'claude', project: 'phasezero' }, env.state).ok, false, 'a lane of another project');
  assert.equal(D.verdict(t, { kind: 'lane', agent: 'nope' }, env.state).ok, false);
});

test('accept rules: only a backlog task is dispatchable (queued waits for its step, running and done are already somewhere)', () => {
  const env = dndWorld();
  const D = env.Dnd;
  for (const phase of ['queued', 'running', 'done', 'failed', 'cancelled']) {
    const v = D.verdict(BACKLOG({ phase }), { kind: 'lane', agent: 'claude' }, env.state);
    assert.equal(v.ok, false, phase);
    assert.match(v.why, /already started/);
  }
  assert.equal(D.draggable(BACKLOG()), true);
  assert.equal(D.draggable(null), false);
  assert.equal(D.draggable(BACKLOG({ phase: 'queued' })), false);
});

test('accept rules: a session takes the prompt when it is idle, done or at its idle prompt; ended, unknown, shells and other agents are refused', () => {
  const env = dndWorld();
  const D = env.Dnd;
  const t = BACKLOG();
  const at = (name) => plain(D.verdict(t, { kind: 'session', tmux: `petroit--api--${name}` }, env.state));
  assert.equal(at('s1').ok, true, 'done');
  assert.equal(at('s1').autoClose, false, 'a session drop does not close the session by default');
  assert.equal(at('s1').queue, undefined);
  assert.equal(at('s6').ok, true, 'waiting at its idle prompt');
  assert.match(at('s2').why, /stopped with an error/);
  assert.match(at('s3').why, /waiting on a prompt/);
  assert.match(at('s5').why, /permission decision/);
  const ended = D.verdict(t, { kind: 'session', tmux: 'phasezero--root--s1' }, env.state);
  assert.equal(ended.ok, false);
  assert.match(ended.why, /session ended/);
  assert.match(D.verdict(t, { kind: 'session', tmux: 'petroit--api--zz' }, env.state).why, /unknown session/);
  assert.match(D.verdict(t, { kind: 'session', tmux: '' }, env.state).why, /unknown session/);
  const cx = D.verdict(t, { kind: 'session', tmux: 'ccboard--ccboard--cx1' }, env.state);
  assert.equal(cx.ok, false);
  assert.match(cx.why, /Claude task cannot go to a Codex session/);
  const other = D.verdict(t, { kind: 'session', tmux: 'ccboard--ccboard--s1' }, env.state);
  assert.equal(other.ok, false);
  assert.match(other.why, /another project/);
  const shell = stateWith();
  shell.projects.find((p) => p.name === 'petroit').repos[0].sessions.push(sess('petroit', 'api', 'sh1', { agent: 'shell', launcher: 'shell', command: 'bash' }));
  assert.match(D.verdict(t, { kind: 'session', tmux: 'petroit--api--sh1' }, shell).why, /not an agent session/);
});

test('accept rules: a working Claude session is queued after its turn; another repo of the project goes with force; a Codex task goes to Codex sessions only', () => {
  const env = dndWorld();
  const D = env.Dnd;
  const t = BACKLOG();
  const working = plain(D.verdict(t, { kind: 'session', tmux: 'petroit--api--s4' }, env.state));
  assert.equal(working.ok, true);
  assert.equal(working.queue, true);
  assert.equal(working.note, 'will be queued after the current turn');
  const s4 = env.state.projects.find((p) => p.name === 'petroit').repos.find((r) => r.name === 'api').sessions.find((s) => s.name === 's4');
  s4.flags = { ...(s4.flags || {}), compacting: true };
  const compacting = D.verdict(t, { kind: 'session', tmux: 'petroit--api--s4' }, env.state);
  assert.equal(compacting.ok, false, 'the server answers 409 for a queued prompt during /compact');
  assert.match(compacting.why, /compacting/);
  delete s4.flags.compacting;
  const web =plain(D.verdict(t, { kind: 'session', tmux: 'petroit--web--w1' }, env.state));
  assert.equal(web.ok, true);
  assert.equal(web.force, true);
  assert.match(web.note, /works in web/);
  assert.match(web.note, /Work in api/);
  assert.equal(D.verdict(BACKLOG({ agent: 'codex' }), { kind: 'session', tmux: 'ccboard--ccboard--cx1' }, env.state).ok, false, 'a working Codex session does not queue: its project differs here');
  const cxState = stateWith([BACKLOG({ project: 'ccboard', repo: 'ccboard', agent: 'codex' })]);
  const cxWorking = D.verdict(cxState.tasks[0], { kind: 'session', tmux: 'ccboard--ccboard--cx1' }, cxState);
  assert.equal(cxWorking.ok, false);
  assert.match(cxWorking.why, /working/);
  cxState.projects.find((p) => p.name === 'ccboard').repos[0].sessions.find((s) => s.name === 'cx1').state = 'idle';
  const cxIdle = D.verdict(cxState.tasks[0], { kind: 'session', tmux: 'ccboard--ccboard--cx1' }, cxState);
  assert.equal(cxIdle.ok, true);
  assert.equal(cxIdle.agent, 'codex');
});

// ---------------------------------------------------------------- the drag lifecycle

test('bindCard makes a backlog card draggable under a fine pointer only; a queued card, a coarse pointer and a touch screen never', () => {
  const fineEnv = dndWorld();
  const c = card(fineEnv);
  assert.equal(c.getAttribute('draggable'), null);
  fineEnv.Dnd.bindCard(c, BACKLOG());
  assert.equal(c.getAttribute('draggable'), 'true');
  const queued = card(fineEnv, 21, 'queued');
  fineEnv.Dnd.bindCard(queued, row(21, { phase: 'queued', column: 'backlog' }));
  assert.equal(queued.getAttribute('draggable'), null, 'a queued card is not a source');
  const done = card(fineEnv, 22, 'done');
  fineEnv.Dnd.bindCard(done, row(22, { phase: 'done', column: 'done' }));
  assert.equal(done.getAttribute('draggable'), null);

  const coarse = dndWorld({ fine: false });
  const t2 = card(coarse);
  coarse.Dnd.bindCard(t2, BACKLOG());
  assert.equal(t2.getAttribute('draggable'), null, '(pointer: fine) does not match');
  const e = fire(coarse, 'dragstart', t2, { dataTransfer: dataTransfer() });
  assert.equal(e.defaultPrevented, true, 'a drag that starts anyway is refused');
  assert.equal(coarse.Dnd.dragging, null);

  const forced = dndWorld();
  forced.w.document.documentElement.classList.add('force-coarse');
  const t3 = card(forced);
  forced.Dnd.bindCard(t3, BACKLOG());
  assert.equal(t3.getAttribute('draggable'), null, 'html.force-coarse (the QA switch for a touch window)');
});

test('pointerover or pointerdown on a Backlog card marks it draggable under a mouse: no card code is needed; touch, a queued card and a coarse pointer never', () => {
  const env = dndWorld();
  const c = card(env);
  assert.equal(c.getAttribute('draggable'), null);
  fire(env, 'pointerover', c, { pointerType: 'mouse' });
  assert.equal(c.getAttribute('draggable'), 'true');
  const inner = env.w.document.createElement('span');
  const c2 = card(env, 23);
  env.state.tasks.push(row(23));
  c2.append(inner);
  fire(env, 'pointerdown', inner, { pointerType: 'mouse' });
  assert.equal(c2.getAttribute('draggable'), 'true', 'from a child of the card');
  const queued = card(env, 21, 'queued');
  env.state.tasks.push(row(21, { phase: 'queued', column: 'backlog' }));
  fire(env, 'pointerover', queued, { pointerType: 'mouse' });
  assert.equal(queued.getAttribute('draggable'), null, 'a queued card is not a source');

  const touch = dndWorld();
  const t1 = card(touch);
  fire(touch, 'pointerover', t1, { pointerType: 'touch' });
  assert.equal(t1.getAttribute('draggable'), null);
  const coarse = dndWorld({ fine: false });
  const t2 = card(coarse);
  fire(coarse, 'pointerover', t2, { pointerType: 'mouse' });
  assert.equal(t2.getAttribute('draggable'), null, '(pointer: fine) does not match');
});

test('a drag that starts on the grip inside the card moves the whole card: the drag image is the card, the faded look goes on the card', async () => {
  const env = dndWorld();
  const c = card(env);
  const grip = env.w.document.createElement('span');
  grip.className = 'tk-grip';
  grip.setAttribute('draggable', 'true');
  grip.setAttribute('data-task', '20');
  c.append(grip);
  env.Dnd.bindCard(c, BACKLOG());
  c.classList.add('task');
  const dt = dataTransfer();
  const images = [];
  dt.setDragImage = (node) => images.push(node);
  const e = fire(env, 'dragstart', grip, { dataTransfer: dt });
  assert.equal(e.defaultPrevented, false);
  assert.equal(plain(env.Dnd.dragging).id, 20);
  assert.equal(dt.data['application/x-ccboard-task'], '20');
  assert.deepEqual(images, [c], 'the whole card');
  assert.equal(c.classList.contains('dnd-src'), false, 'not before the browser has taken the drag image');
  await env.clock.advance(1);
  assert.equal(c.classList.contains('dnd-src'), true);
  assert.equal(grip.classList.contains('dnd-src'), false);
  fire(env, 'dragend', grip);
  assert.equal(c.classList.contains('dnd-src'), false, 'cleared at dragend');
});

test('dragstart sets Dnd.dragging, the MIME data, effectAllowed move and body.dnd-task, and marks every target ok or no', () => {
  const env = dndWorld();
  const ok = sessionTarget(env, 'petroit--api--s1');
  const working = sessionTarget(env, 'petroit--api--s4');
  const ended = sessionTarget(env, 'phasezero--root--s1');
  const unknown = sessionTarget(env, 'petroit--api--nope');
  const lane = laneTarget(env, 'claude');
  const { dt, e } = startDrag(env);
  assert.equal(e.defaultPrevented, false);
  assert.equal(plain(env.Dnd.dragging).id, 20);
  assert.equal(dt.data['application/x-ccboard-task'], '20');
  assert.equal(dt.effectAllowed, 'move');
  assert.equal(bodyHas(env, 'dnd-task'), true);
  for (const n of [ok, working, lane]) { assert.equal(n.classList.contains('drop-ok'), true); assert.equal(n.classList.contains('drop-no'), false); }
  for (const n of [ended, unknown]) { assert.equal(n.classList.contains('drop-no'), true); assert.equal(n.classList.contains('drop-ok'), false); }
});

test('dragover: a valid target is a drop target with the move effect, a refused one is not and says why; a working row says it will be queued', () => {
  const env = dndWorld();
  const ok = sessionTarget(env, 'petroit--api--s1');
  const working = sessionTarget(env, 'petroit--api--s4');
  const ended = sessionTarget(env, 'phasezero--root--s1');
  startDrag(env);
  const dt = dataTransfer();
  const good = fire(env, 'dragover', ok, { dataTransfer: dt });
  assert.equal(good.defaultPrevented, true);
  assert.equal(dt.dropEffect, 'move');
  assert.equal(ok.classList.contains('drop-over'), true);
  assert.equal(hint(env), null, 'an idle row has nothing to add');
  const q = fire(env, 'dragover', working, { dataTransfer: dataTransfer() });
  assert.equal(q.defaultPrevented, true);
  assert.equal(ok.classList.contains('drop-over'), false, 'the ring follows the pointer');
  assert.equal(working.getAttribute('data-drop-note'), 'will be queued after the current turn');
  assert.equal(hint(env).textContent, 'will be queued after the current turn');
  const dt2 = dataTransfer();
  const bad = fire(env, 'dragover', ended, { dataTransfer: dt2 });
  assert.equal(bad.defaultPrevented, false, 'no preventDefault: the browser shows the not-allowed cursor and fires no drop');
  assert.equal(dt2.dropEffect, 'none');
  assert.match(hint(env).textContent, /session ended/);
  assert.equal(hint(env).classList.contains('no'), true);
  fire(env, 'dragover', env.w.document.body, { dataTransfer: dataTransfer() });
  assert.equal(hint(env), null, 'off every target: no hint');
});

test('dragleave only clears the ring when the pointer leaves the window; crossing into a child keeps it (the next dragover decides)', () => {
  const env = dndWorld();
  const row = sessionTarget(env, 'petroit--api--s1');
  const inner = env.w.document.createElement('span');
  row.append(inner);
  startDrag(env);
  fire(env, 'dragover', row, { dataTransfer: dataTransfer() });
  assert.equal(row.classList.contains('drop-over'), true);
  fire(env, 'dragleave', row, { relatedTarget: inner, clientX: 40, clientY: 40 });
  assert.equal(row.classList.contains('drop-over'), true, 'into a child');
  fire(env, 'dragleave', row, { relatedTarget: null, clientX: 40, clientY: 40 });
  assert.equal(row.classList.contains('drop-over'), true, 'a browser that gives no relatedTarget: the pointer is still inside the window');
  fire(env, 'dragleave', row, { relatedTarget: null, clientX: 0, clientY: 0 });
  assert.equal(row.classList.contains('drop-over'), false, 'out of the window');
  assert.equal(env.Dnd.over, null);
});

test('dragend clears every mark, the hint, body.dnd-task and Dnd.dragging, and runs the repaints that waited for the drag', () => {
  const env = dndWorld();
  const a = sessionTarget(env, 'petroit--api--s1');
  const b = sessionTarget(env, 'phasezero--root--s1');
  startDrag(env);
  fire(env, 'dragover', b, { dataTransfer: dataTransfer() });
  assert.equal(env.Dnd.afterDrag(() => {}), true);
  env.w.ctx.__ran = 0;
  const wait = env.w.run('(() => { __ran = 0; const f = () => { __ran++; }; Dnd.afterDrag(f); Dnd.afterDrag(f); return f; })()');
  assert.equal(typeof wait, 'function');
  fire(env, 'dragend', env.w.document.body);
  assert.equal(env.Dnd.dragging, null);
  assert.equal(bodyHas(env, 'dnd-task'), false);
  for (const n of [a, b]) for (const cls of ['drop-ok', 'drop-no', 'drop-over']) assert.equal(n.classList.contains(cls), false, cls);
  assert.equal(b.getAttribute('data-drop-note'), null);
  assert.equal(hint(env), null);
  assert.equal(env.w.get('__ran'), 1, 'a deferred repaint runs once, even when it was asked for twice');
  assert.equal(env.Dnd.afterDrag(() => {}), false, 'idle: repaint now');
});

test('the drag also ends on a window blur, on Escape, and on the first mouse move long after the last drag event', () => {
  const env = dndWorld();
  startDrag(env);
  env.w.fire('blur', {});
  assert.equal(env.Dnd.dragging, null, 'window blur');
  assert.equal(bodyHas(env, 'dnd-task'), false);

  startDrag(env);
  fire(env, 'keydown', env.w.document.body, { key: 'Escape' });
  assert.equal(env.Dnd.dragging, null, 'Escape');

  startDrag(env);
  fire(env, 'mousemove', env.w.document.body);
  assert.notEqual(env.Dnd.dragging, null, 'a mouse move right after dragstart is not the end');
  env.w.run('Dnd.tick = Date.now() - 5000');
  fire(env, 'mousemove', env.w.document.body);
  assert.equal(env.Dnd.dragging, null, 'a drag whose dragend never came');
  assert.equal(bodyHas(env, 'dnd-task'), false);
});

test('a synthetic drop with no dragstart before it (a hand-built DragEvent carrying the MIME and the card id) still opens the popover and dispatches', async () => {
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': LANE_ANSWER } });
  const lane = laneTarget(env, 'claude');
  assert.equal(env.Dnd.dragging, null);
  const dt = dataTransfer();
  dt.setData('application/x-ccboard-task', '20');
  const e = fire(env, 'drop', lane, { dataTransfer: dt, clientX: 200, clientY: 120 });
  assert.equal(e.defaultPrevented, true);
  assert.match(popover(env).textContent, /Start in a new Claude session/);
  assert.match(popover(env).textContent, /Unify the devices pagination/);
  env.w.localStorage.setItem('ccboard:dnd:instant', '1');
  popover(env).querySelector('.dnd-cancel').click();
  const dt2 = dataTransfer();
  dt2.setData('application/x-ccboard-task', '20');
  fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dt2 });
  await settle();
  assert.deepEqual(plain(posts(env)[0].body), { mode: 'lane', agent: 'claude', auto_close: true }, 'instant: no popover');
  const unknown = dataTransfer();
  unknown.setData('application/x-ccboard-task', '999');
  assert.equal(fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: unknown }).defaultPrevented, false, 'a card that is not on the board is left alone');
  assert.equal(fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dataTransfer() }).defaultPrevented, false, 'no MIME: not ours');
});

test('only a .task card is a source: a chain-strip step button carries data-task and data-phase but never becomes draggable', () => {
  const env = dndWorld();
  const step = env.w.document.createElement('button');
  step.className = 'tk-step';
  step.setAttribute('data-task', '20');
  step.setAttribute('data-phase', 'backlog');
  env.w.document.body.append(step);
  fire(env, 'pointerover', step, { pointerType: 'mouse' });
  assert.equal(step.getAttribute('draggable'), null);
  const e = fire(env, 'dragstart', step, { dataTransfer: dataTransfer() });
  assert.equal(e.defaultPrevented, false);
  assert.equal(env.Dnd.dragging, null, 'not ours: nothing starts');
});

test('a drop outside every target, or on a refused one, only cleans up', async () => {
  const env = dndWorld();
  const ended = sessionTarget(env, 'phasezero--root--s1');
  startDrag(env);
  fire(env, 'drop', env.w.document.body, { dataTransfer: dataTransfer() });
  assert.equal(env.Dnd.dragging, null);
  startDrag(env);
  fire(env, 'drop', ended, { dataTransfer: dataTransfer() });
  await settle();
  assert.equal(env.Dnd.dragging, null);
  assert.equal(popover(env), null);
  assert.equal(posts(env).length, 0);
  assert.equal(fire(env, 'drop', env.w.document.body, { dataTransfer: dataTransfer() }).defaultPrevented, false, 'not our drag: left alone');
});

// ---------------------------------------------------------------- the confirm popover and the dispatch bodies

const LANE_ANSWER = { id: 20, phase: 'running', tmux: 'petroit--api--t-task-20', session_row: 41, slug: 'task-20', branch: 'task/task-20', task: { id: 20, tmux: 'petroit--api--t-task-20', session_row: 41, slug: 'task-20' } };
const submit = (env) => popover(env).querySelector('form').dispatchEvent(mkEvent('submit'));

test('a lane drop opens the confirm popover (agent, repo, launch summary, auto-close ON) and Start posts a lane dispatch', async () => {
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': LANE_ANSWER } });
  env.w.localStorage.setItem('ccboard:task:petroit/api', JSON.stringify({ model_sel: 'opus', permission_mode: 'acceptEdits' }));
  const lane = laneTarget(env, 'claude');
  startDrag(env);
  const drop = fire(env, 'drop', lane, { dataTransfer: dataTransfer(), clientX: 300, clientY: 200 });
  assert.equal(drop.defaultPrevented, true);
  assert.equal(env.Dnd.dragging, null, 'the drag is over once dropped');
  const pop = popover(env);
  assert.ok(pop, 'the popover is open');
  const text = pop.textContent;
  assert.match(text, /Start in a new Claude session/);
  assert.match(text, /Unify the devices pagination/);
  assert.match(text, /petroit\/api/);
  assert.match(text, /opus · acceptEdits/, 'the saved launch defaults');
  const sw = pop.querySelector('.dnd-switch input');
  assert.equal(sw.checked, true, 'auto-close defaults ON for a lane');
  assert.equal(env.w.document.activeElement.className.includes('dnd-go'), true, 'Enter confirms: the primary has the focus');
  assert.equal(posts(env).length, 0, 'nothing is sent before it is confirmed');
  submit(env);
  await settle();
  assert.equal(popover(env), null, 'the popover closes');
  const sent = posts(env);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].path, '/api/tasks/20/dispatch');
  assert.deepEqual(plain(sent[0].body), { mode: 'lane', agent: 'claude', auto_close: true });
  assert.ok(toasts(env).some((t) => /started task-20/.test(t.text)));
});

test('the Codex lane posts agent codex and the auto-close switch changes auto_close', async () => {
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': LANE_ANSWER } });
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'codex'), { dataTransfer: dataTransfer() });
  assert.match(popover(env).textContent, /Start in a new Codex session/);
  popover(env).querySelector('.dnd-switch input').checked = false;
  submit(env);
  await settle();
  assert.deepEqual(plain(posts(env)[0].body), { mode: 'lane', agent: 'codex', auto_close: false });
});

test('Edit options… hands the drop to the launcher in dispatch mode (taskDispatchSheet) with the agent and the switch, and sends nothing itself', async () => {
  const env = dndWorld();
  env.w.run('globalThis.__sheets = []; taskDispatchSheet = (t, preset) => { __sheets.push({ id: t.id, preset }); };');
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dataTransfer() });
  popover(env).querySelector('.dnd-switch input').checked = false;
  popover(env).querySelector('.dnd-optsbtn').click();
  assert.equal(popover(env), null, 'the popover gives way to the sheet');
  assert.deepEqual(plain(env.w.get('__sheets')), [{ id: 20, preset: { agent: 'claude', auto_close: false } }]);
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'codex'), { dataTransfer: dataTransfer() });
  popover(env).querySelector('.dnd-optsbtn').click();
  assert.equal(plain(env.w.get('__sheets'))[1].preset.agent, 'codex');
  startDrag(env);
  fire(env, 'drop', sessionTarget(env, 'petroit--api--s1'), { dataTransfer: dataTransfer() });
  assert.equal(popover(env).querySelector('.dnd-optsbtn'), null, 'launch options are about a new session');
  await settle();
  assert.equal(posts(env).length, 0);
});

test('Edit options… with openLauncher: the launcher in dispatch mode with the card, its place (state objects), the agent and the switch; the popover gives way and nothing is sent', async () => {
  const env = dndWorld({ launcher: 'stub' });
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dataTransfer() });
  popover(env).querySelector('.dnd-switch input').checked = false;
  popover(env).querySelector('.dnd-optsbtn').click();
  assert.equal(popover(env), null);
  const got = plain(env.w.run('__launched.map((o) => ({ mode: o.mode, task: o.task.id, project: o.project.name, repo: o.repo.name, agent: o.agent, auto_close: o.auto_close }))'));
  assert.deepEqual(got, [{ mode: 'dispatch', task: 20, project: 'petroit', repo: 'api', agent: 'claude', auto_close: false }]);
  assert.equal(env.w.run('__launched[0].project === state.projects.find((p) => p.name === "petroit")'), true);
  await settle();
  assert.equal(posts(env).length, 0);
  startDrag(env);
  fire(env, 'drop', sessionTarget(env, 'petroit--api--s1'), { dataTransfer: dataTransfer() });
  assert.equal(popover(env).querySelector('.dnd-optsbtn'), null, 'launch options are about a new session');
});

test('without the launcher sheet, Edit options opens the launch controls in place (Claude lane only) and sends what the person touched', async () => {
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': LANE_ANSWER } });
  env.w.run('taskDispatchSheet = undefined;');                // no openLauncher either (the legacy world): neither sheet exists
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'codex'), { dataTransfer: dataTransfer() });
  assert.equal(popover(env).querySelector('.dnd-optsbtn'), null, 'Claude launch controls are not offered for Codex');
  popover(env).querySelector('.dnd-cancel').click();
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dataTransfer() });
  const btn = popover(env).querySelector('.dnd-optsbtn');
  assert.ok(btn, 'Edit options… on a Claude lane');
  assert.equal(popover(env).querySelector('.dnd-opts').classList.contains('hidden'), true);
  btn.click();
  assert.equal(popover(env).querySelector('.dnd-opts').classList.contains('hidden'), false);
  const [model, effort, perm] = all(popover(env), '.dnd-opts select');
  model.value = 'opus'; effort.value = 'high'; perm.value = 'acceptEdits';
  submit(env);
  await settle();
  assert.deepEqual(plain(posts(env)[0].body), { mode: 'lane', agent: 'claude', auto_close: true, model: 'opus', effort: 'high', permission_mode: 'acceptEdits' });
});

test('a session drop posts {session, auto_close: false}; a working one adds queue, another repo adds force', async () => {
  const answers = { '/api/tasks/20/dispatch': (req) => ({ id: 20, phase: 'running', tmux: req.body.session, session_row: 5, pasted: true }) };
  const env = dndWorld({ answers });
  startDrag(env);
  fire(env, 'drop', sessionTarget(env, 'petroit--api--s1'), { dataTransfer: dataTransfer() });
  assert.match(popover(env).textContent, /Hand to s1/);
  assert.equal(popover(env).querySelector('.dnd-switch input').checked, false, 'auto-close defaults OFF for a running session');
  assert.equal(popover(env).querySelector('.dnd-optsbtn'), null);
  submit(env);
  await settle();
  assert.deepEqual(plain(posts(env)[0].body), { session: 'petroit--api--s1', auto_close: false });

  const env2 = dndWorld({ answers });
  startDrag(env2);
  fire(env2, 'drop', sessionTarget(env2, 'petroit--api--s4'), { dataTransfer: dataTransfer() });
  assert.match(popover(env2).textContent, /will be queued after the current turn/);
  submit(env2);
  await settle();
  assert.deepEqual(plain(posts(env2)[0].body), { session: 'petroit--api--s4', auto_close: false, queue: true });
  assert.ok(toasts(env2).some((t) => /queued in s4/.test(t.text)));

  const env3 = dndWorld({ answers });
  startDrag(env3);
  fire(env3, 'drop', sessionTarget(env3, 'petroit--web--w1'), { dataTransfer: dataTransfer() });
  assert.match(popover(env3).textContent, /works in web/);
  popover(env3).querySelector('.dnd-switch input').checked = true;
  submit(env3);
  await settle();
  assert.deepEqual(plain(posts(env3)[0].body), { session: 'petroit--web--w1', auto_close: true, force: true });
});

test('Cancel, Escape, a click outside and a route change close the popover without sending anything', async () => {
  const env = dndWorld();
  const open = () => { startDrag(env); fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dataTransfer() }); assert.ok(popover(env)); };
  open();
  popover(env).querySelector('.dnd-cancel').click();
  assert.equal(popover(env), null, 'Cancel');
  open();
  fire(env, 'keydown', env.w.document.body, { key: 'Escape' });
  assert.equal(popover(env), null, 'Escape');
  open();
  fire(env, 'pointerdown', env.w.document.body);
  assert.equal(popover(env), null, 'a click outside');
  open();
  fire(env, 'pointerdown', popover(env).querySelector('.dnd-pop-head'));
  assert.ok(popover(env), 'a click inside stays');
  env.w.fire('hashchange', {});
  assert.equal(popover(env), null, 'a route change');
  await settle();
  assert.equal(posts(env).length, 0);
  assert.equal(plain(env.w.get('store.tasksOverride')[20]), undefined);
});

test('ccboard:dnd:instant=1 skips the popover: the drop dispatches with the defaults at once', async () => {
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': LANE_ANSWER } });
  env.w.localStorage.setItem('ccboard:dnd:instant', '1');
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dataTransfer() });
  assert.equal(popover(env), null);
  await settle();
  assert.deepEqual(plain(posts(env)[0].body), { mode: 'lane', agent: 'claude', auto_close: true });
  const env2 = dndWorld({ answers: { '/api/tasks/20/dispatch': { id: 20, phase: 'running', tmux: 'petroit--api--s1', session_row: 5, pasted: true } } });
  env2.w.localStorage.setItem('ccboard:dnd:instant', '1');
  startDrag(env2);
  fire(env2, 'drop', sessionTarget(env2, 'petroit--api--s1'), { dataTransfer: dataTransfer() });
  await settle();
  assert.deepEqual(plain(posts(env2)[0].body), { session: 'petroit--api--s1', auto_close: false });
});

// ---------------------------------------------------------------- the optimistic move

test('the card moves to In progress at once, before the answer; the override is released when the poll returns the task with the new tmux', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': async () => { await gate; return LANE_ANSWER; } } });
  env.w.localStorage.setItem('ccboard:dnd:instant', '1');
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dataTransfer() });
  await settle();
  const during = plain(env.w.run('boardTasks(state)')).find((t) => t.id === 20);
  assert.equal(during.phase, 'running');
  assert.equal(during.column, 'in_progress');
  assert.equal(during.mode, 'worktree');
  assert.equal(during.agent, 'claude');
  assert.equal(during.auto_close, true);
  assert.equal(plain(env.w.get('store.tasksOverride'))[20]._busy, true);
  release();
  await settle();
  const after = plain(env.w.run('boardTasks(state)')).find((t) => t.id === 20);
  assert.equal(after.tmux, 'petroit--api--t-task-20');
  assert.equal(after.column, 'in_progress');
  assert.equal(plain(env.w.get('store.tasksOverride'))[20]._busy, undefined);
  assert.ok(env.w.get('__polls') >= 1, 'poll(true) asked for the real state');
  // the poll catches up: the same task, running in the new tmux
  env.w.ctx.__st = stateWith([BACKLOG({ phase: 'running', column: 'in_progress', tmux: 'petroit--api--t-task-20', session_row: 41, prompt: null })]);
  env.w.run('state = __st');
  const settled = plain(env.w.run('boardTasks(state)')).find((t) => t.id === 20);
  assert.equal(settled.tmux, 'petroit--api--t-task-20');
  assert.deepEqual(Object.keys(plain(env.w.get('store.tasksOverride'))), [], 'the override is gone once the poll agrees');
});

test('a session drop paints the card into the session at once and keeps the hand-over mode', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': async () => { await gate; return { id: 20, phase: 'running', tmux: 'petroit--api--s1', session_row: 5, pasted: true }; } } });
  env.w.localStorage.setItem('ccboard:dnd:instant', '1');
  startDrag(env);
  fire(env, 'drop', sessionTarget(env, 'petroit--api--s1'), { dataTransfer: dataTransfer() });
  await settle();
  const during = plain(env.w.run('boardTasks(state)')).find((t) => t.id === 20);
  assert.equal(during.phase, 'running');
  assert.equal(during.tmux, 'petroit--api--s1');
  assert.equal(during.mode, 'session');
  release();
  await settle();
  const after = plain(env.w.run('boardTasks(state)')).find((t) => t.id === 20);
  assert.equal(after.mode, 'session');
  assert.equal(after.tmux, 'petroit--api--s1');
});

test('a refused dispatch puts the card back and toasts the reason; a second drop on a card that is on its way is ignored', async () => {
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': () => { const e = new Error('already dispatched'); e.status = 409; throw e; } } });
  env.w.localStorage.setItem('ccboard:dnd:instant', '1');
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dataTransfer() });
  await settle();
  const t = plain(env.w.run('boardTasks(state)')).find((x) => x.id === 20);
  assert.equal(t.phase, 'backlog', 'back in the Backlog');
  assert.equal(plain(env.w.get('store.tasksOverride'))[20], undefined);
  assert.ok(toasts(env).some((x) => x.kind === 'bad' && /already dispatched/.test(x.text)));

  const env2 = dndWorld({ answers: { '/api/tasks/20/dispatch': () => new Promise(() => {}) } });
  env2.w.localStorage.setItem('ccboard:dnd:instant', '1');
  startDrag(env2);
  fire(env2, 'drop', laneTarget(env2, 'claude'), { dataTransfer: dataTransfer() });
  await settle();
  const second = await env2.Dnd.dispatch(env2.Dnd.plan(BACKLOG(), { kind: 'lane', agent: 'claude', autoClose: true }), {});
  assert.equal(second, null);
  assert.equal(posts(env2).length, 1);
});

test('the answer\'s limit_warning shows as a warning toast', async () => {
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': { ...LANE_ANSWER, limit_warning: 'the 5h window is at 91%: resets 16:15' } } });
  env.w.localStorage.setItem('ccboard:dnd:instant', '1');
  startDrag(env);
  fire(env, 'drop', laneTarget(env, 'claude'), { dataTransfer: dataTransfer() });
  await settle();
  assert.ok(toasts(env).some((t) => t.kind === 'warn' && /91%/.test(t.text)));
});

// ---------------------------------------------------------------- the lane bar

test('the lane bar: a Claude lane and a Codex lane (hidden until installed), each with a chip per running session of its agent', () => {
  const env = dndWorld();
  const bar = env.w.run("Dnd.laneBar({ project: 'petroit' })");
  const lane = (a) => bar.querySelector(`.dnd-lane[data-agent="${a}"]`);
  assert.equal(lane('claude').getAttribute('data-drop'), 'lane');
  assert.equal(lane('claude').getAttribute('data-project'), 'petroit');
  assert.match(lane('claude').textContent, /Claude/);
  assert.match(lane('codex').textContent, /Codex/);
  const names = (a) => all(lane(a), '.dnd-chip').map((c) => c.textContent.replace(/^\S/, '').trim());
  assert.deepEqual(names('claude'), ['s1', 's2', 's3', 's4', 's5', 's6', 'w1']);
  assert.deepEqual(names('codex'), [], 'petroit runs no Codex session');
  for (const c of all(lane('claude'), '.dnd-chip')) {
    assert.equal(c.getAttribute('data-drop'), 'session');
    assert.match(c.getAttribute('data-tmux'), /^petroit--/);
  }
  assert.equal(lane('codex').classList.contains('hidden'), false);
  env.state.agents.codex.installed = false;
  bar.ccPatch(env.state);
  assert.equal(lane('codex').classList.contains('hidden'), true);
  const global = env.w.run('Dnd.laneBar({})');
  assert.deepEqual(all(global.querySelector('.dnd-lane[data-agent="codex"]'), '.dnd-chip').map((c) => c.getAttribute('data-tmux')), [], 'a hidden lane lists nothing');
  env.state.agents.codex.installed = true;
  global.ccPatch(env.state);
  assert.deepEqual(all(global.querySelector('.dnd-lane[data-agent="codex"]'), '.dnd-chip').map((c) => c.getAttribute('data-tmux')), ['ccboard--ccboard--cx1']);
  assert.ok(all(global, '.dnd-chip').every((c) => !/phasezero--root--s1/.test(c.getAttribute('data-tmux'))), 'an ended session has no chip');
});

test('the lane bar keeps its chip nodes across polls, drops a chip whose session ended, and marks new chips while a card is held', () => {
  const env = dndWorld();
  const bar = env.w.run("Dnd.laneBar({ project: 'petroit' })");
  env.w.document.body.append(bar);
  const chip = (t) => bar.querySelector(`.dnd-chip[data-tmux="${t}"]`);
  const s1 = chip('petroit--api--s1');
  env.w.run("state.projects.find(p => p.name === 'petroit').repos[0].sessions.find(s => s.name === 's1').state = 'working'");
  bar.ccPatch(env.state);
  assert.equal(chip('petroit--api--s1'), s1, 'the same node');
  assert.match(s1.querySelector('.glyph').getAttribute('aria-label'), /working/);
  startDrag(env);
  assert.equal(chip('petroit--api--s1').classList.contains('drop-ok'), true);
  assert.equal(chip('petroit--api--s2').classList.contains('drop-no'), true, 'an errored session refuses');
  env.w.run("state.projects.find(p => p.name === 'petroit').repos[0].sessions.push(Object.assign({}, state.projects.find(p => p.name === 'petroit').repos[0].sessions[0], { tmux: 'petroit--api--s9', name: 's9', state: 'idle' }))");
  bar.ccPatch(env.state);
  assert.equal(chip('petroit--api--s9').classList.contains('drop-ok'), true, 'a chip that appears mid-drag is marked at once');
  env.w.run("state.projects.find(p => p.name === 'petroit').repos[0].sessions.find(s => s.name === 's9').state = 'ended'");
  bar.ccPatch(env.state);
  assert.equal(chip('petroit--api--s9'), null);
});

test('a chip is a link, not a drag source; the bar is hidden on touch by CSS', () => {
  const css = fs.readFileSync(path.join(STATIC, 'shell.css'), 'utf8');
  assert.match(css, /@media \(pointer:coarse\) \{ \.dnd-bar \{ display:none; \} \}/);
  assert.match(css, /html\.force-coarse \.dnd-bar \{ display:none; \}/);
  const env = dndWorld();
  const bar = env.w.run('Dnd.laneBar({})');
  assert.ok(all(bar, '.dnd-chip').length > 0);
  for (const c of all(bar, '.dnd-chip')) assert.equal(c.getAttribute('draggable'), 'false');
});

// ---------------------------------------------------------------- the CSS contract

test('shell.css carries the drag styles: the iframe rule, the ring, the dimming; the popover is a plain class rule (no style attribute anywhere)', () => {
  const css = fs.readFileSync(path.join(STATIC, 'shell.css'), 'utf8');
  assert.match(css, /body\.dnd-task iframe \{ pointer-events:none; \}/);
  assert.match(css, /\.drop-ok \{ outline:2px solid var\(--sig\)/);
  assert.match(css, /\.drop-no \{ opacity:\.45; \}/);
  assert.match(css, /dialog\.dnd-pop \{ position:fixed;/);
  assert.match(css, /\.task\[data-phase="backlog"\]::before/, 'the CSS-only grip');
  const js = fs.readFileSync(path.join(STATIC, 'dnd.js'), 'utf8');
  assert.doesNotMatch(js, /innerHTML|insertAdjacentHTML|cssText|setAttribute\(\s*['"]style/);
});

// ---------------------------------------------------------------- the board: lanes and chips are targets, a poll cannot rebuild it mid-drag

const go = async (env, hash) => { env.w.location.hash = hash; await settle(); return env.page(); };
const laneOf = (page, agent) => page.querySelector(`[data-drop="lane"][data-agent="${agent}"]`);
const poll = (env) => env.w.run('updateCurrentPage(state)');                  // what the state poll does to the mounted page

test('the Tasks page mounts the dispatch bar: a lane per agent is a data-drop lane, each running session of the agent a data-drop session chip (Codex once installed)', async () => {
  const env = dndWorld();
  const page = await go(env, '#/tasks');
  const claude = laneOf(page, 'claude');
  assert.ok(claude, 'a ◆ Claude lane is on the Tasks page');
  assert.equal(page.querySelector('#tasks').children[1], claude.closest('.dnd-bar'), 'between the head and the board');
  assert.ok(laneOf(page, 'codex'), 'a ◇ Codex lane: both agents are installed');
  assert.equal(claude.getAttribute('data-drop'), 'lane');
  const chips = all(page, '[data-drop="session"]').filter((n) => !n.classList.contains('rrow'));
  const tmuxes = chips.map((c) => c.getAttribute('data-tmux'));
  assert.ok(tmuxes.includes('petroit--api--s1'), 'a chip per running session');
  assert.ok(tmuxes.includes('ccboard--ccboard--cx1'));
  assert.ok(!tmuxes.includes('phasezero--root--s1'), 'an ended session has none');
  assert.deepEqual(all(laneOf(page, 'codex'), '[data-drop="session"]').map((c) => c.getAttribute('data-tmux')), ['ccboard--ccboard--cx1'], 'the Codex lane lists the Codex sessions only');
  assert.equal(laneOf(page, 'codex').classList.contains('hidden'), false);
  env.state.agents.codex.installed = false;
  poll(env);
  assert.equal(laneOf(page, 'codex').classList.contains('hidden'), true, 'no Codex lane until it is installed');
  // the verdicts the lanes and chips get when a card is held
  startDrag(env);
  assert.equal(laneOf(page, 'claude').classList.contains('drop-ok'), true);
  const chip = (t) => all(page, '[data-drop="session"]').find((n) => n.getAttribute('data-tmux') === t && !n.classList.contains('rrow'));
  assert.equal(chip('petroit--api--s1').classList.contains('drop-ok'), true, 'a done session takes it');
  assert.equal(chip('petroit--api--s2').classList.contains('drop-no'), true, 'an errored session refuses');
});

test('a drag from the real card to the Claude lane posts the dispatch and the card moves; a poll in between cannot rebuild the board', async () => {
  const env = dndWorld({ answers: { '/api/tasks/20/dispatch': LANE_ANSWER } });
  const page = await go(env, '#/tasks');
  const lane = laneOf(page, 'claude');
  const cardNode = page.querySelector('.task[data-task="20"]');
  assert.ok(cardNode);
  assert.equal(cardNode.getAttribute('data-phase'), 'backlog');
  fire(env, 'pointerover', cardNode, { pointerType: 'mouse' });
  assert.equal(cardNode.getAttribute('draggable'), 'true', 'a mouse over a Backlog card makes it a source');
  fire(env, 'dragstart', cardNode, { dataTransfer: dataTransfer() });
  assert.equal(plain(env.Dnd.dragging).id, 20);
  assert.equal(lane.classList.contains('drop-ok'), true);
  env.state.tasks.push(row(21, { title: 'A second card' }));
  poll(env);
  assert.equal(page.querySelector('.task[data-task="20"]'), cardNode, 'the dragged node is still the one in the page');
  assert.equal(page.querySelector('.task[data-task="21"]'), null, 'the repaint waits for the drag');
  fire(env, 'drop', lane, { dataTransfer: dataTransfer() });
  assert.ok(popover(env), 'the confirm popover');
  submit(env);
  await settle();
  assert.equal(posts(env).length, 1);
  assert.equal(posts(env)[0].path, '/api/tasks/20/dispatch');
  assert.deepEqual(plain(posts(env)[0].body), { mode: 'lane', agent: 'claude', auto_close: true });
  const moved = all(page, '.col').find((c) => c.querySelector(`.task[data-task="20"]`));
  assert.ok(moved, 'the card is on the board');
  assert.notEqual(moved.getAttribute('data-col'), 'backlog', 'moved out of the Backlog at once');
  fire(env, 'dragend', cardNode);
  env.w.fire('dragend', {});
  await env.clock.advance(5);
  assert.ok(page.querySelector('.task[data-task="21"]'), 'the held-back repaint ran after the drop');
});

// ---------------------------------------------------------------- the rows are targets

test('session rows (agents.js sessionCard) and the sidebar rows (shell.js) are data-drop session targets with their tmux', async () => {
  const env = dndWorld({ install: false });
  assert.equal(env.Dnd.ready, false, 'nothing is listening before the first target is drawn');
  const page = await go(env, '#/agents');
  const rows = all(page, '.rrow');
  assert.ok(rows.length > 3, 'the roster has rows');
  for (const r of rows) {
    assert.equal(r.getAttribute('data-drop'), 'session');
    assert.ok(r.getAttribute('data-tmux'));
  }
  assert.equal(env.Dnd.ready, true, 'binding a row installed the delegated listeners');
  const Shell = env.w.get('Shell');
  const kid = Shell.kidNode({ kind: 'session', key: 's:petroit--api--s1', tmux: 'petroit--api--s1', name: 's1', repo: 'api', state: 'done', agent: 'claude', needs: false });
  assert.equal(kid.getAttribute('data-drop'), 'session');
  Shell.patchKid(kid, { kind: 'session', key: 's:petroit--api--s1', tmux: 'petroit--api--s1', name: 's1', repo: 'api', state: 'done', agent: 'claude', needs: false });
  assert.equal(kid.getAttribute('data-tmux'), 'petroit--api--s1');
  const nodes = [...all(page, '.rrow'), kid];
  startDrag(env);
  const okRow = nodes.find((n) => n.getAttribute('data-tmux') === 'petroit--api--s1');
  fire(env, 'dragover', okRow, { dataTransfer: dataTransfer() });
  assert.equal(okRow.classList.contains('drop-over'), true);
});

test('dragging over a child of a row targets the row; the nearest data-drop wins (a chip inside a lane)', () => {
  const env = dndWorld();
  const lane = laneTarget(env, 'claude');
  const chip = env.w.document.createElement('a');
  chip.setAttribute('data-drop', 'session');
  chip.setAttribute('data-tmux', 'petroit--api--s1');
  const inner = env.w.document.createElement('span');
  chip.append(inner);
  lane.append(chip);
  startDrag(env);
  fire(env, 'dragover', inner, { dataTransfer: dataTransfer() });
  assert.equal(chip.classList.contains('drop-over'), true);
  assert.equal(lane.classList.contains('drop-over'), false);
  fire(env, 'dragover', lane, { dataTransfer: dataTransfer() });
  assert.equal(lane.classList.contains('drop-over'), true);
  assert.equal(chip.classList.contains('drop-over'), false);
});

// ---------------------------------------------------------------- long press (components.js longPress: the Move sheet's touch path)

function pressWorld() {
  const env = dndWorld();
  const node = env.w.document.createElement('div');
  env.w.document.body.append(node);
  env.w.ctx.__fired = [];
  const ctl = env.w.run('(n) => longPress(n, (e) => { __fired.push(e.clientX + "," + e.clientY); })')(node);
  const down = (extra = {}) => node.dispatchEvent(mkEvent('pointerdown', { pointerType: 'touch', button: 0, clientX: 50, clientY: 60, ...extra }));
  const on = (type, extra = {}) => node.dispatchEvent(mkEvent(type, extra));
  return { env, node, ctl, down, on, fired: () => plain(env.w.get('__fired')) };
}

test('long press: 450 ms on a touch pointer fires once; 449 does not', async () => {
  const { env, down, fired } = pressWorld();
  down();
  await env.clock.advance(449);
  assert.deepEqual(fired(), []);
  await env.clock.advance(2);
  assert.deepEqual(fired(), ['50,60']);
  await env.clock.advance(2000);
  assert.deepEqual(fired(), ['50,60'], 'once');
});

test('long press: a move of more than 8 px, a scroll, a pointerup, a pointercancel and a pointerleave each cancel it; 7.8 px does not', async () => {
  const { env, down, on, fired } = pressWorld();
  const idle = env.clock.pending;
  down();
  await env.clock.advance(300);
  on('pointermove', { clientX: 50 + 6, clientY: 60 + 5 });                 // 7.8 px
  await env.clock.advance(200);
  assert.equal(fired().length, 1, 'inside the slop it still fires');

  down({ clientX: 10, clientY: 10 });
  await env.clock.advance(100);
  on('pointermove', { clientX: 10 + 9, clientY: 10 });
  await env.clock.advance(1000);
  assert.equal(fired().length, 1, 'a 9 px move cancels');

  down();
  await env.clock.advance(100);
  env.w.fire('scroll', {});
  await env.clock.advance(1000);
  assert.equal(fired().length, 1, 'a scroll cancels');

  for (const type of ['pointerup', 'pointercancel', 'pointerleave']) {
    down();
    await env.clock.advance(100);
    on(type);
    await env.clock.advance(1000);
    assert.equal(fired().length, 1, `${type} cancels`);
  }
  assert.equal(env.clock.pending, idle, 'no timer is left running');
});

test('long press: a mouse on a fine pointer never long-presses; cancel() stops one in flight; the click after a fired press is swallowed', async () => {
  const { env, ctl, down, on, fired } = pressWorld();
  down({ pointerType: 'mouse' });
  await env.clock.advance(1000);
  assert.equal(fired().length, 0, 'a mouse has the Move button, the m key and the drag');
  down();
  await env.clock.advance(100);
  ctl.cancel();
  await env.clock.advance(1000);
  assert.equal(fired().length, 0);
  down();
  await env.clock.advance(500);
  assert.equal(fired().length, 1);
  let swallowed = 0;
  on('click', { stopPropagation() { swallowed++; }, preventDefault() { swallowed++; } });
  assert.equal(swallowed, 2, 'the lift after a long press does not click through');
  let again = 0;
  on('click', { stopPropagation() { again++; }, preventDefault() { again++; } });
  assert.equal(again, 0, 'only that one click');
  const ctx = mkEvent('contextmenu');
  down();
  await env.clock.advance(500);
  on('contextmenu', ctx);
  assert.equal(ctx.defaultPrevented, true, 'the browser\'s own menu never opens over a fired press');
});
