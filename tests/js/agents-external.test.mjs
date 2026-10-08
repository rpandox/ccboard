// Contract tests for the v0.5.12 "Started outside the board" section of the Agents page (pages/agents.js: agentsExt*): the Codex threads GET /api/external lists, each with an Open
// that runs POST /api/external/codex/<id>/open. Real scripts on minidom's DOM through world.mjs's homeWorld (api() records its calls and answers from __answers).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ISO, calls, fakeState, homeWorld, page, plain, setState, text, tick, EPOCH } from './world.mjs';

const ID1 = '019a3c10-5e2d-7b41-8c0a-6d3f1e9a2b11', ID2 = '019a3b77-1c9e-70d2-9b4e-0f8a5c3d7e22', ID3 = '019a39d4-83fa-7a15-b2c6-4e1d0a9f6c33', ID4 = '019a3611-27be-7c88-91d0-5b7e3f2a8d44';
const BASE = '/srv/projects';
const CODEX = { installed: true, version: '0.160.0', loggedIn: true };

/** A thread the way app/agents/codex_discovery.py lists it. */
const thread = (id, over = {}) => ({ agent: 'codex', source: 'rollout', id, session_id: id, name: 'Wire the push test', title: 'Wire the push test', cwd: `${BASE}/ccboard/ccboard`, model: 'gpt-5.5', effort: 'high',
  tokens: 2140000, updated_at: ISO(14), branch: 'main', originator: 'codex-tui', badge: null, client: 'cli', project: 'ccboard', repo: 'ccboard', openable: true, tmux: null, ...over });
const ANSWER = (codex) => ({ claude: [], codex, at: ISO(0) });
const THREADS = () => [
  thread(ID1),
  thread(ID2, { title: 'Summarise last night\'s inbox', name: 'Summarise last night\'s inbox', cwd: '/home/user/brain', originator: 'hermes', badge: 'hermes', client: 'vscode', project: null, repo: null, openable: false, updated_at: ISO(150), branch: null, tokens: 310000, effort: 'medium' }),
  thread(ID3, { title: 'Refactor the checkout step 2 layout so the order summary stays above the fold on a phone', cwd: `${BASE}/phasezero/website`, project: 'phasezero', repo: 'website', updated_at: ISO(300), branch: 'feat/checkout', tokens: 640000, effort: 'medium' }),
];

function world({ state = fakeState({ agents: { codex: CODEX }, config: { projects_dir: BASE, code_https_port: 10000, ntfy: { enabled: false }, backup: {} } }), answer = ANSWER(THREADS()), timers = false } = {}) {
  const t = [];
  const extra = timers ? { setInterval: (fn, ms) => { t.push({ fn, ms }); return t.length; }, clearInterval: (id) => { t[id - 1] = null; } } : {};
  const env = homeWorld({ state, extra });
  env.w.ctx.__list = answer;                                                        // what GET /api/external answers; __open: what POST .../open answers (a function may throw)
  env.w.ctx.__open = { ok: true };
  env.w.run(`__answers['/api/external'] = (req) => { const v = req.method === 'POST' ? __open : __list; return typeof v === 'function' ? v(req) : v; };`);
  env.timers = t;
  return env;
}
const sec = (w) => page(w).querySelector('[data-sec=external]');
const rows = (w) => page(w).querySelectorAll('.xrow');
const ids = (w) => rows(w).map((r) => r.getAttribute('data-ext'));
const open = (w, id) => page(w).querySelector(`.xrow[data-ext="${id}"] .xr-open`);
const gets = (w) => calls(w).filter((c) => c.path.startsWith('/api/external') && c.method === 'GET');
const posts = (w) => calls(w).filter((c) => c.method === 'POST');
const mount = async (w) => { w.location.hash = '#/agents'; await tick(); await tick(); };

test('no Codex on the box: the section stays hidden and /api/external is never asked', async () => {
  const { w } = world({ state: fakeState({ agents: {} }) });
  await mount(w);
  assert.ok(sec(w).classList.contains('hidden'));
  assert.equal(gets(w).length, 0);
  const { w: w2 } = world({ state: fakeState({ agents: { codex: { installed: false } } }) });
  await mount(w2);
  assert.equal(gets(w2).length, 0, 'an uninstalled Codex has no threads to list');
});

test('with Codex installed the page asks GET /api/external?agent=codex once and lists the threads newest first, with their count', async () => {
  const { w } = world();
  await mount(w);
  assert.deepEqual(gets(w).map((c) => c.path), ['/api/external?agent=codex']);
  assert.equal(sec(w).classList.contains('hidden'), false);
  assert.deepEqual(ids(w), [ID1, ID2, ID3]);
  assert.match(text(sec(w).querySelector('.xsec-head')), /^Started outside the board3 threads$/);
  assert.match(text(sec(w).querySelector('.unote')), /Codex threads of the last 14 days that were not started here\. Open resumes one in a board session, in its folder\./);
  assert.equal(page(w).querySelectorAll('.rrow').length, 0, 'the threads are not session rows: j / k and the roster click handler never see them');
});

test('a row: the title (or the id head), a Codex badge, project/repo in the project\'s hue, branch, model, effort, tokens, the age; Hermes carries its originator badge', async () => {
  const { w } = world();
  await mount(w);
  const r1 = rows(w)[0];
  assert.equal(text(r1.querySelector('.xr-title')), 'Wire the push test');
  const meta = r1.querySelector('.xr-meta');
  assert.match(text(meta), /^◇ Codexccboard\/ccboardmaingpt-5\.5high2\.1M tokens$/);
  assert.equal(r1.querySelector('.xr-origin'), null, 'a thread from a Codex terminal has no originator badge');
  assert.match(meta.querySelector('.xr-where').className, /hue-(blue|teal|green|violet|slate)/);
  assert.equal(meta.querySelector('.xr-where').getAttribute('title'), `${BASE}/ccboard/ccboard`);
  assert.ok(meta.querySelector('.bdg-model').className.includes('hue-teal'), 'a gpt model wears the Codex teal');
  assert.equal(r1.querySelector('time.age').getAttribute('data-epoch'), String(Math.floor(Date.parse(thread(ID1).updated_at) / 1000)));
  assert.match(text(r1.querySelector('time.age')), /^1[3-5]m$/);
  const hermes = rows(w)[1];
  assert.equal(text(hermes.querySelector('.xr-origin')), 'hermes');
  assert.match(hermes.querySelector('.xr-origin').getAttribute('title'), /Started by hermes, not from a Codex terminal/);
  assert.match(text(hermes.querySelector('.xr-where')), /^…\/user\/brain$/, 'outside the projects: the last two path segments');
  assert.equal(rows(w)[2].querySelector('.xr-where').textContent, 'phasezero/website');
  assert.doesNotMatch(text(sec(w)), /undefined|NaN|null|\[object/);
});

test('rows the page cannot use are skipped: no id, or already one of the board\'s own sessions (tmux set); legacy field names are read', async () => {
  const legacy = { id: ID4, title: 'Legacy names', cwd: `${BASE}/mailgate/mailgate`, model: 'gpt-5.5', reasoning_effort: 'xhigh', tokens_used: 8900000, updated_at: EPOCH(60), git_branch: 'fix/dkim', originator: 'hermes' };
  const { w } = world({ answer: ANSWER([thread(ID1, { tmux: 'ccboard--ccboard--s9' }), { title: 'no id' }, null, 'x', legacy]) });
  await mount(w);
  assert.deepEqual(ids(w), [ID4]);
  const r = rows(w)[0];
  assert.match(text(r.querySelector('.xr-meta')), /mailgate\/mailgatefix\/dkimgpt-5\.5xhigh8\.9M tokens/);
  assert.equal(text(r.querySelector('.xr-origin')), 'hermes', 'no badge field: a non-terminal originator is badged all the same');
  assert.equal(text(r.querySelector('time.age')).length > 0, true, 'epoch seconds are an age too');
  assert.equal(r.querySelector('.xr-open').disabled, false, 'no openable field: the folder under the projects directory is enough');
  const { w: w2 } = world({ answer: ANSWER([{ id: ID3, title: '', cwd: '/tmp/elsewhere' }]) });
  await mount(w2);
  assert.equal(text(rows(w2)[0].querySelector('.xr-title')), 'thread 019a39d4');
  assert.equal(open(w2, ID3).disabled, true, 'no openable field, a folder outside the projects directory');
});

test('without a `codex` list in the answer the page falls back to state.external.codex (the demo board\'s fixture)', async () => {
  const st = fakeState({ agents: { codex: CODEX }, config: { projects_dir: BASE }, external: { claude: [], codex: [thread(ID1)], at: ISO(0) } });
  const { w } = world({ state: st, answer: { ok: true } });
  await mount(w);
  assert.deepEqual(ids(w), [ID1]);
  setState(w, fakeState({ agents: { codex: CODEX }, config: { projects_dir: BASE }, external: { claude: [], codex: [thread(ID1), thread(ID3, { updated_at: ISO(300) })], at: ISO(0) } }));
  assert.deepEqual(ids(w), [ID1, ID3], 'the poll updates it');
  setState(w, fakeState({ agents: { codex: CODEX }, config: { projects_dir: BASE }, external: { sessions: [] } }));
  assert.deepEqual(ids(w), [], 'the v0.5.4 registry shape is not a Codex list');
  assert.ok(sec(w).classList.contains('hidden'));
});

test('Open: POST /api/external/codex/<id>/open, then a toast, the poll, a fresh list and the new session\'s page', async () => {
  const { w } = world();
  await mount(w);
  w.ctx.__open = { tmux: 'ccboard--ccboard--s4', attach_url: '/tty/?arg=ccboard--ccboard--s4', agent: 'codex', agent_session_id: ID1, project: 'ccboard', repo: 'ccboard' };
  w.run('globalThis.__polls = 0; poll = async () => { __polls++; };');
  assert.equal(open(w, ID1).disabled, false);
  assert.equal(text(open(w, ID1)), 'Open');
  open(w, ID1).click();
  assert.equal(text(open(w, ID1)), 'Opening…', 'the tapped row says so at once');
  assert.ok(rows(w).every((r) => r.querySelector('.xr-open').disabled), 'one open at a time');
  open(w, ID3).click();
  await tick(); await tick();
  assert.deepEqual(posts(w).map((c) => c.path), [`/api/external/codex/${ID1}/open`], 'a second tap while one runs does nothing');
  assert.deepEqual(plain(w.get('__toasts')).pop(), { text: 'Opened in the board as ccboard--ccboard--s4', kind: 'ok' });
  assert.equal(w.get('__polls'), 1, 'a fresh state, so the new session is on the board');
  assert.equal(gets(w).length, 2, 'and a fresh list');
  assert.equal(w.location.hash, '#/s/ccboard--ccboard--s4');
  assert.equal(text(open(w, ID1)), 'Open');
});

test('Open with no session name in the answer (the demo board) only toasts; a refusal is a toast with the board\'s reason, nothing else changes', async () => {
  const { w } = world();
  await mount(w);
  w.ctx.__open = { ok: true };
  open(w, ID1).click();
  await tick(); await tick();
  assert.deepEqual(plain(w.get('__toasts')).pop(), { text: 'Opened in a board session', kind: 'ok' });
  assert.equal(w.location.hash, '#/agents');
  const refuse = Object.assign(new Error('409 Conflict'), { status: 409, body: { detail: 'session 019a3c10 is already open on the board as ccboard--ccboard--s4', error: 'x' } });
  w.ctx.__open = () => { throw refuse; };
  open(w, ID1).click();
  await tick(); await tick();
  assert.deepEqual(plain(w.get('__toasts')).pop(), { text: 'Could not open it: session 019a3c10 is already open on the board as ccboard--ccboard--s4', kind: 'bad' });
  assert.equal(open(w, ID1).disabled, false, 'usable again');
  assert.equal(w.location.hash, '#/agents');
});

test('a thread whose folder is outside the projects directory has Open disabled, with the reason in its title; the others stay usable', async () => {
  const { w } = world();
  await mount(w);
  assert.equal(open(w, ID2).disabled, true);
  assert.match(open(w, ID2).getAttribute('title'), /outside the projects directory/);
  assert.equal(open(w, ID1).disabled, false);
  assert.match(open(w, ID1).getAttribute('title'), /Resume this thread in a board session/);
  open(w, ID2).click();
  await tick();
  assert.equal(posts(w).length, 0, 'a disabled button posts nothing');
  assert.match(open(w, ID1).getAttribute('aria-label'), /^Open Wire the push test in a board session$/);
});

test('an outside thread shows the chip and a one-line caption (no hover needed); a thread inside shows neither (#32 d)', async () => {
  const { w } = world();
  await mount(w);
  const row = (id) => w.document.querySelector(`.xrow[data-ext="${id}"]`);
  const outside = row(ID2);
  assert.equal(text(outside.querySelector('.xr-outside')), 'outside projects');
  assert.ok(outside.querySelector('.xr-outside').classList.contains('bdg'));
  const why = outside.querySelector('.xr-why');
  assert.equal(why.classList.contains('hidden'), false);
  assert.match(text(why), /^Open is off: its folder is outside the projects directory \(or gone\)\.$/);
  assert.match(open(w, ID2).getAttribute('title'), /outside the projects directory/, 'the tooltip stays');
  const inside = row(ID1);
  assert.equal(inside.querySelector('.xr-outside'), null);
  assert.ok(inside.querySelector('.xr-why').classList.contains('hidden'));
});

test('the list refreshes every 30 s while the page is open and stops at unmount; a thread that left the answer leaves the page', async () => {
  const { w, timers } = world({ timers: true });
  await mount(w);
  const t = timers.filter((x) => x && x.ms === 30000);
  assert.equal(t.length, 1, 'one 30 s timer');
  w.ctx.__list = ANSWER([thread(ID1)]);
  t[0].fn();
  await tick(); await tick();
  assert.equal(gets(w).length, 2);
  assert.deepEqual(ids(w), [ID1]);
  w.location.hash = '#/tasks';
  await tick();
  assert.equal(timers.filter((x) => x && x.ms === 30000).length, 0, 'the timer is cleared with the page');
});

test('a failed read with no rows says so; with rows on screen the last list stays and nothing is announced', async () => {
  const { w } = world({ answer: () => { throw new Error('the board did not answer'); } });
  await mount(w);
  assert.equal(sec(w).classList.contains('hidden'), false);
  assert.match(text(sec(w).querySelector('[role=alert]')), /^Could not read the outside threads: the board did not answer$/);
  assert.deepEqual(ids(w), []);
  w.ctx.__list = ANSWER(THREADS());
  w.get('agentsExtLoad')();
  await tick(); await tick();
  assert.deepEqual(ids(w), [ID1, ID2, ID3]);
  assert.ok(sec(w).querySelector('[role=alert]').classList.contains('hidden'));
  w.ctx.__list = () => { throw new Error('502'); };
  w.get('agentsExtLoad')();
  await tick(); await tick();
  assert.deepEqual(ids(w), [ID1, ID2, ID3], 'the last list stays');
  assert.ok(sec(w).querySelector('[role=alert]').classList.contains('hidden'));
});

test('a poll patches the rows in place: the node of a thread survives, its Open keeps focus', async () => {
  const { w } = world();
  await mount(w);
  const before = rows(w)[0];
  const btn = open(w, ID1);
  btn.focus();
  setState(w, fakeState({ agents: { codex: CODEX }, config: { projects_dir: BASE } }));
  assert.equal(rows(w)[0], before, 'the same node');
  assert.equal(w.document.activeElement, btn);
});
