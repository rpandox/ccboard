// Contract tests for v0.5.19, Settings > Agents (settingsAgents in pages/settings.js): a labelled section each for Claude and Codex with the version, the hooks and their trust state, the size
// of the launcher's catalogue, the agent's own doctor checks (pages/doctor.js, the login buttons left out), the Codex threads started outside the board with an Open each, and Log in
// leading to Settings > Accounts. Real scripts on minidom's DOM through tests/js/world.mjs; api() is the recorder (GET /api/doctor and /api/external answer from __answers).
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { ISO, calls, fakeState, homeWorld, page, plain, text, tick } from './world.mjs';

const BASE = '/srv/projects';
const ID1 = '019a3c10-5e2d-7b41-8c0a-6d3f1e9a2b11', ID2 = '019a3b77-1c9e-70d2-9b4e-0f8a5c3d7e22';
const CLAUDE = { installed: true, version: '2.1.287', hooks: { installed: true, trust: 'accept' } };
const CODEX = { installed: true, loggedIn: true, version: '0.145.0', hooks: { installed: true, trust: 'review' } };
const stateOf = (over = {}) => fakeState({
  claude: { installed: true, loggedIn: true, email: 'a@example.com', subscriptionType: 'max' },
  agents: { claude: CLAUDE, codex: CODEX }, config: { projects_dir: BASE, code_https_port: 10000, ntfy: { enabled: false }, backup: {} }, ...over,
});
const chk = (id, group, status, over = {}) => ({ id, group, label: `${id} label`, status, detail: `${id} detail`, fix: null, ...over });
const CHECKS = () => [
  chk('claude-bin', 'claude', 'pass'),
  chk('claude-login', 'claude', 'fail', { fix: { text: 'Sign in to Claude', action: 'claude_login' } }),
  chk('memory-plugin', 'memory', 'warn'),
  chk('codex-bin', 'codex', 'pass'),
  chk('codex-login', 'codex', 'warn', { fix: { text: 'Sign in to Codex', action: 'codex_login' } }),
  chk('codex-hooks', 'codex', 'warn', { fix: { text: 'Install the hooks', cmd: 'ccboard hooks install --codex' } }),
  chk('ntfy', 'notify', 'warn'),
];
const doctor = (checks = CHECKS()) => ({ ok: true, summary: { pass: 1, warn: 1, fail: 1, skip: 0 }, checks });
const thread = (id, over = {}) => ({ agent: 'codex', source: 'rollout', id, session_id: id, name: 'Wire the push test', title: 'Wire the push test', cwd: `${BASE}/ccboard/ccboard`, model: 'gpt-5.5', effort: 'high',
  tokens: 2140000, updated_at: ISO(14), branch: 'main', originator: 'codex-tui', badge: null, client: 'cli', project: 'ccboard', repo: 'ccboard', openable: true, tmux: null, ...over });
const THREADS = () => [thread(ID1), thread(ID2, { title: 'Refactor checkout', name: 'Refactor checkout', cwd: `${BASE}/phasezero/website`, project: 'phasezero', repo: 'website', updated_at: ISO(300) })];

const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('doctorStore.seq += 1; agentsExt.seq += 1'); } catch (_) { /* never mounted */ } } });

/** A world on #/settings?sec=agents. __doctor / __ext / __open are what GET /api/doctor, GET /api/external and POST .../open answer (a function may throw or wait). */
function agWorld({ state = stateOf(), doctor: dr = doctor(), ext = { claude: [], codex: THREADS(), at: ISO(0) }, route = '#/settings?sec=agents' } = {}) {
  const { w } = homeWorld({ state });
  made.push(w);
  w.ctx.__doctor = dr; w.ctx.__ext = ext; w.ctx.__open = { ok: true, tmux: 'ccboard--ccboard--x1' };
  w.run(`__answers['/api/doctor'] = () => (typeof __doctor === 'function' ? __doctor() : __doctor);
         __answers['/api/external'] = (req) => { const v = req.method === 'POST' ? __open : __ext; return typeof v === 'function' ? v(req) : v; };
         poll = async () => {};`);
  w.location.hash = route;
  return w;
}
const mount = async (w) => { await tick(); await tick(); return w; };
const panel = (w) => page(w).querySelector('.settings-panel[data-sec=agents]');
const heads = (w) => panel(w).querySelectorAll('.set-h').map(text);
/** The nodes of the panel between the heading called `name` and the next heading. */
function under(w, name) {
  const out = [];
  let on = false;
  for (const n of panel(w).children) {
    const isHead = n.classList && n.classList.contains('set-h');
    if (isHead) on = text(n) === name; else if (on) out.push(n);
  }
  return out;
}
const kv = (nodes, label) => nodes.find((n) => n.classList.contains('kv') && text(n.querySelector('.k')) === label);
const val = (row) => text(row.querySelector('.kv-main'));
const btn = (root, label) => root.querySelectorAll('button').find((b) => text(b).trim() === label);
const getsOf = (w, prefix) => calls(w).filter((c) => c.method === 'GET' && c.path.startsWith(prefix)).map((c) => c.path);
const isFilled = (b) => (b.classList.contains('primary') || b.classList.contains('bp5-intent-primary')) && !b.classList.contains('tinted');
const hidden = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const filled = (root) => root.querySelectorAll('button, a.btn').filter((b) => !hidden(b) && isFilled(b)).map(text);

// ---------------------------------------------------------------- the two cards

test('a section each for Claude and Codex, in that order; the outside threads come last under their own label', async () => {
  const w = await mount(agWorld());
  assert.deepEqual(heads(w), ['Claude', 'Codex', 'Import external sessions']);
  assert.match(val(kv(under(w, 'Claude'), 'Claude')), /Claude: a@example\.com \(max\)/);
  assert.match(val(kv(under(w, 'Codex'), 'Codex')), /Codex: logged in/);
});

test('Claude: version, hooks with the trust state, and how many models the launcher offers (the embedded catalogue or what GET /api/agents said)', async () => {
  const w = await mount(agWorld());
  w.run("LX_LOAD.agents = { claude: { models: ['opus', 'fable', 'sonnet', 'haiku', 'opus-4-1'] } }");
  w.run('settingsFill("agents", true)');
  const c = under(w, 'Claude');
  assert.equal(val(kv(c, 'Version')), '2.1.287');
  assert.equal(val(kv(c, 'Hooks')).startsWith('installed · trust: accept'), true);
  assert.match(val(kv(c, 'Models')), /^5 in the catalogue/);
  assert.equal(kv(c, 'Hooks').querySelector('.warn'), null, 'installed hooks are not a warning');
});

test('Codex: the same facts, with its own version, hooks trust (review) and catalogue size', async () => {
  const w = await mount(agWorld());
  w.run("LX_LOAD.agents = { codex: { models: ['gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-astra'] } }");
  w.run('settingsFill("agents", true)');
  const x = under(w, 'Codex');
  assert.equal(val(kv(x, 'Version')), '0.145.0');
  assert.equal(val(kv(x, 'Hooks')).startsWith('installed · trust: review'), true);
  assert.match(val(kv(x, 'Models')), /^3 in the catalogue/);
});

test('hooks that are not installed are said as a warning with the reason; an unknown version reads "unknown"', async () => {
  const w = await mount(agWorld({ state: stateOf({ agents: { claude: { installed: true, hooks: { installed: false } }, codex: { installed: true, loggedIn: true, hooks: { installed: false, trust: 'review' } } } }) }));
  const hooks = kv(under(w, 'Claude'), 'Hooks');
  assert.equal(hooks.querySelector('.v').textContent, 'not installed');
  assert.ok(hooks.querySelector('.v').classList.contains('warn'));
  assert.match(text(hooks), /only guesses a session's state from its screen/);
  assert.equal(val(kv(under(w, 'Claude'), 'Version')), 'unknown');
  assert.equal(kv(under(w, 'Codex'), 'Hooks').querySelector('.v').textContent, 'not installed · trust: review');
});

test('Claude not installed: the red badge and no facts; no Codex on the box: no Codex section and no outside threads asked for', async () => {
  const w = await mount(agWorld({ state: stateOf({ claude: { installed: false, loggedIn: false }, agents: {} }) }));
  assert.deepEqual(heads(w), ['Claude']);
  const badge = kv(under(w, 'Claude'), 'Claude').querySelector('.badge');
  assert.equal(text(badge), 'claude not installed');
  assert.ok(badge.classList.contains('bad'));
  assert.equal(kv(under(w, 'Claude'), 'Version'), undefined);
  assert.equal(getsOf(w, '/api/external').length, 0);
});

test('Codex installed false: "not installed" in plain words, no facts, no Log in, no outside threads', async () => {
  const w = await mount(agWorld({ state: stateOf({ agents: { claude: CLAUDE, codex: { installed: false } } }) }));
  assert.deepEqual(heads(w), ['Claude', 'Codex']);
  const x = under(w, 'Codex');
  assert.equal(val(kv(x, 'Codex')), 'not installed');
  assert.equal(kv(x, 'Version'), undefined);
  assert.equal(x.some((n) => btn(n, 'Log in') || btn(n, 'Add account')), false);
  assert.equal(getsOf(w, '/api/external').length, 0);
});

// ---------------------------------------------------------------- the agent's own checks

test('each card carries that agent\'s doctor checks (group claude, group codex), worst first, without the login button the card already has; other groups stay on the Doctor tab', async () => {
  const w = await mount(agWorld());
  const rowIds = (nodes) => nodes.flatMap((n) => n.querySelectorAll('.doc-row')).map((r) => r.getAttribute('data-check'));
  assert.deepEqual(rowIds(under(w, 'Claude')), ['claude-login', 'claude-bin'], 'memory-plugin and ntfy are not Claude\'s own');
  assert.deepEqual(rowIds(under(w, 'Codex')), ['codex-login', 'codex-hooks', 'codex-bin']);
  const loginRow = panel(w).querySelectorAll('.doc-row').find((r) => r.getAttribute('data-check') === 'claude-login');
  assert.equal(text(loginRow.querySelector('.doc-fix')), 'Sign in to Claude');
  assert.equal(loginRow.querySelectorAll('button').length, 0, 'the card\'s own Log in is the one button');
  const hooksRow = panel(w).querySelectorAll('.doc-row').find((r) => r.getAttribute('data-check') === 'codex-hooks');
  assert.equal(text(hooksRow.querySelector('code.doc-cmd')), 'ccboard hooks install --codex');
  assert.ok(btn(hooksRow, 'Copy'), 'a command keeps its Copy button here too');
});

test('before the first answer a card says Checking…; when the box could not run the checks it says so; the answer repaints the open panel without a reload', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const w = agWorld({ doctor: () => gate });
  await tick();
  assert.equal(panel(w).querySelectorAll('[role=status]').filter((n) => text(n) === 'Checking…').length, 2, 'one under each card');
  release(doctor());
  await tick(); await tick();
  assert.equal(panel(w).querySelectorAll('.doc-row').length, 5);
  assert.equal(panel(w).querySelectorAll('[role=status]').filter((n) => text(n) === 'Checking…').length, 0);
  const bad = await mount(agWorld({ doctor: () => { throw new Error('the board is busy'); } }));
  assert.equal(panel(bad).querySelectorAll('.set-note').filter((n) => /Checks could not run: the board is busy/.test(text(n))).length, 2);
});

// ---------------------------------------------------------------- Log in leads to Accounts

test('Claude logged out: Log in is the one filled button on the panel and leads to Settings > Accounts; logged in there is a two-tap Log out and no Log in', async () => {
  const w = await mount(agWorld({ state: stateOf({ claude: { installed: true, loggedIn: false } }) }));
  assert.deepEqual(filled(panel(w)), ['Log in']);
  const login = btn(kv(under(w, 'Claude'), 'Claude'), 'Log in');
  assert.equal(login.getAttribute('title'), 'Sign in from Settings > Accounts');
  login.click();
  assert.match(w.location.hash, /^#\/settings\?.*sec=accounts/);
  const ok = await mount(agWorld());
  assert.deepEqual(filled(panel(ok)), [], 'nothing on the panel is a filled primary while Claude is logged in');
  assert.equal(btn(kv(under(ok, 'Claude'), 'Claude'), 'Log in'), undefined);
  const out = btn(kv(under(ok, 'Claude'), 'Claude'), 'Log out');
  assert.ok(out && out.classList.contains('danger') && out.classList.contains('minimal'), 'red-outlined and quiet: it is not one tap to lose');
});

test('Codex: Log in (logged out) or Add account (logged in) leads to Settings > Accounts and asks for the account name there; the button is plain, never filled', async () => {
  const out = await mount(agWorld({ state: stateOf({ agents: { claude: CLAUDE, codex: { ...CODEX, loggedIn: false } } }) }));
  assert.match(val(kv(under(out, 'Codex'), 'Codex')), /Codex: not logged in/);
  assert.ok(kv(under(out, 'Codex'), 'Codex').querySelector('.warn'));
  const login = btn(kv(under(out, 'Codex'), 'Codex'), 'Log in');
  assert.equal(isFilled(login), false);
  login.click();
  assert.match(out.location.hash, /sec=accounts/);
  assert.equal(out.get('settingsPage.wantCx'), true);
  const inn = await mount(agWorld());
  const add = btn(kv(under(inn, 'Codex'), 'Codex'), 'Add account');
  assert.ok(add && !btn(kv(under(inn, 'Codex'), 'Codex'), 'Log in'));
  add.click();
  assert.match(inn.location.hash, /sec=accounts/);
});

test('the Codex row names the account in use (state.codex_accounts) with its plan', async () => {
  const st = stateOf({ codex_accounts: { current: 'k1', list: [{ key: 'k1', label: 'Work', plan: 'pro', current: true, saved: true }], store: { supported: true, add: true, reason: null, count: 1 }, login: { running: false } } });
  const w = await mount(agWorld({ state: st }));
  assert.match(val(kv(under(w, 'Codex'), 'Codex')), /Codex: Work \(pro\)/);
});

// ---------------------------------------------------------------- outside Codex threads

test('Codex threads started outside the board: asked once per visit (GET /api/external?agent=codex), listed with a count, newest first, each with Open', async () => {
  const w = await mount(agWorld());
  assert.deepEqual(getsOf(w, '/api/external'), ['/api/external?agent=codex']);
  const ext = under(w, 'Import external sessions');
  const head = kv(ext, 'Outside threads');
  assert.equal(text(head.querySelector('.v')), '2 threads');
  assert.match(text(head), /Codex threads of the last 14 days that were not started here\. Open resumes one in a board session, in its folder\./);
  assert.ok(btn(head, 'Look again'));
  const rows = panel(w).querySelectorAll('.xrow');
  assert.deepEqual(rows.map((r) => r.getAttribute('data-ext')), [ID1, ID2]);
  assert.equal(text(rows[0].querySelector('.xr-title')), 'Wire the push test');
  assert.deepEqual(rows.map((r) => text(r.querySelector('.xr-open'))), ['Open', 'Open']);
  w.run('settingsFill("agents", true)');
  assert.equal(getsOf(w, '/api/external').length, 1, 'a repaint does not ask again');
  btn(head, 'Look again').click();
  await tick();
  assert.equal(getsOf(w, '/api/external').length, 2, 'Look again does');
});

test('Open resumes the thread in a board session: POST /api/external/codex/<id>/open, then the session page', async () => {
  const w = await mount(agWorld());
  panel(w).querySelectorAll('.xrow')[0].querySelector('.xr-open').click();
  await tick(); await tick(); await tick();
  const posts = calls(w).filter((c) => c.method === 'POST');
  assert.deepEqual(posts.map((c) => c.path), [`/api/external/codex/${ID1}/open`]);
  assert.match(plain(w.get('__toasts')).pop().text, /Opened in the board as ccboard--ccboard--x1/);
  assert.match(w.location.hash, /ccboard--ccboard--x1/);
});

test('no outside threads says so; a failed read says why; a failed Open says why and the row stays', async () => {
  const none = await mount(agWorld({ ext: { claude: [], codex: [], at: ISO(0) } }));
  assert.match(text(panel(none)), /No outside Codex threads found\./);
  assert.equal(text(kv(under(none, 'Import external sessions'), 'Outside threads').querySelector('.v')), '0 threads');
  const bad = await mount(agWorld({ ext: () => { throw new Error('rollouts unreadable'); } }));
  assert.match(text(panel(bad)), /Could not read the outside threads: rollouts unreadable/);
  const w = await mount(agWorld());
  w.ctx.__open = () => { throw new Error('the thread is gone'); };
  panel(w).querySelectorAll('.xrow')[0].querySelector('.xr-open').click();
  await tick(); await tick();
  assert.match(plain(w.get('__toasts')).pop().text, /Could not open it: the thread is gone/);
  assert.equal(panel(w).querySelectorAll('.xrow').length, 2);
});
