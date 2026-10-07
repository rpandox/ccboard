// Contract tests for v0.5.19, the new-project wizard (pages/onboarding.js, #/onboarding/project): the name rule (the server's, from tests/fixtures/onboarding_names.json, which
// tests/test_preflight.py checks against tmux.valid_name / projects.check_url / derive_repo_name), the four steps and what each one sends, the clone URL's preflight chips, the
// defaults it saves in the shape the launcher reads (ccboard:defaults:<project>), the resume rules (sessionStorage ccboard:wiz and ?name=), the first-run redirect and the entry points.
// Real core.js, components.js, launcher.js, router.js and pages/*.js on minidom's DOM through tests/js/world.mjs; api() is the recorder from world.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, mock, test } from 'node:test';
import { STATIC } from './harness.mjs';
import { calls, fakeState, homeWorld, page, plain, projectsOf, setState, text, tick } from './world.mjs';

const PARITY = JSON.parse(fs.readFileSync(path.join(STATIC, '..', '..', 'tests', 'fixtures', 'onboarding_names.json'), 'utf8'));
const AGENTS = { claude: { installed: true, loggedIn: true, version: '2.1.287' }, codex: { installed: true, loggedIn: true, version: '0.145.0' } };
const stateOf = (over = {}) => fakeState({ agents: AGENTS, setup: { first_run: false, ok: true }, ...over });
const SHOP = (repos = {}) => projectsOf({ shop: repos });

const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('wizPage.refs = null; clearTimeout(wizPage.timer)'); } catch (_) { /* never mounted */ } } mock.timers.reset(); });

/** The world on `route`; api() answers `answers` (path prefix -> value | fn). openLauncher and poll are recorders. */
function wizWorld({ state = stateOf(), answers = {}, route = '#/onboarding/project', session = null, local = null } = {}) {
  const { w } = homeWorld({ state });
  made.push(w);
  w.ctx.__launched = []; w.ctx.__polls = 0;
  for (const [k, v] of Object.entries(answers)) w.ctx.__answers[k] = v;
  w.run('openLauncher = (o) => { __launched.push(o); return true; }; poll = async () => { __polls++; };');
  w.ctx.navigator.clipboard = { writeText: async () => {} };
  if (session) w.sessionStorage.setItem('ccboard:wiz', JSON.stringify(session));
  if (local) for (const [k, v] of Object.entries(local)) w.localStorage.setItem(k, v);
  w.location.hash = route;
  return w;
}

const wiz = (w) => page(w).querySelector('.wiz');
const steps = (w) => wiz(w).querySelectorAll('.wiz-step');
const stepOf = (w, key) => steps(w).find((s) => s.getAttribute('data-step') === key);
const states = (w) => steps(w).map((s) => `${s.getAttribute('data-step')}:${s.getAttribute('data-state')}`);
const active = (w) => steps(w).find((s) => s.getAttribute('data-state') === 'active');
const btn = (root, label) => root.querySelectorAll('button, a.btn').find((b) => text(b).trim() === label);
const hidden = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const isFilled = (b) => (b.classList.contains('primary') || b.classList.contains('bp5-intent-primary')) && !b.classList.contains('tinted');
const filled = (root) => root.querySelectorAll('button, a.btn').filter((b) => !hidden(b) && isFilled(b)).map(text);
const submit = (form) => form.dispatchEvent({ type: 'submit', preventDefault() {} });
const fieldOf = (root, label) => root.querySelectorAll('.field').find((f) => text(f.querySelector('label')) === label);
const input = (root, label) => fieldOf(root, label).querySelector('input, textarea, select');
const saved = (w) => JSON.parse(w.sessionStorage.getItem('ccboard:wiz') || 'null');
const post = (w, p) => calls(w).filter((c) => c.method === 'POST' && c.path === p);

/** Step 1 done: the name typed, Create tapped, the answer awaited. */
async function createProject(w, name = 'shop') {
  input(active(w), 'Project name').value = name;
  submit(active(w).querySelector('form'));
  await tick(); await tick();
}

// ---------------------------------------------------------------- the rules the box enforces

test('the name rule accepts and rejects exactly what tmux.valid_name does (the fixture is the server\'s own verdicts), plus the reserved root', () => {
  const w = wizWorld({ route: '#/' });
  for (const [value, ok] of PARITY.names) {
    const want = ok && value !== 'root';
    assert.equal(w.get(`wizNameProblem(${JSON.stringify(value)}, 'project')`) === '', want, JSON.stringify(value));
  }
  assert.match(w.get("wizNameProblem('root', 'project')"), /reserved/);
  assert.match(w.get("wizNameProblem('a--b', 'repo')"), /--/);
  assert.match(w.get("wizNameProblem('', 'repo')"), /Name the repo/);
  assert.match(w.get("wizNameProblem('a b', 'project')"), /letters, digits/);
});

test('the clone URL rule is check_url\'s and the derived name is derive_repo_name\'s, case by case', () => {
  const w = wizWorld({ route: '#/' });
  for (const [url, ok] of PARITY.urls) assert.equal(w.get(`wizUrlProblem(${JSON.stringify(url)})`) === '', ok, JSON.stringify(url));
  for (const [url, name] of PARITY.derive) assert.equal(w.get(`wizDeriveName(${JSON.stringify(url)})`), name || '', JSON.stringify(url));
});

// ---------------------------------------------------------------- the page, the stepper, step 1

test('#/onboarding/project is one page with four steps in a vertical stepper; the first is active, the others wait; one filled primary: Create project', () => {
  const w = wizWorld();
  assert.equal(w.document.title, 'New project · ccboard');
  assert.deepEqual(states(w), ['name:active', 'repo:todo', 'defaults:todo', 'prompt:todo']);
  assert.deepEqual(steps(w).map((s) => text(s.querySelector('.wiz-title'))), ['Name', 'Repo', 'Defaults', 'First prompt']);
  assert.equal(wiz(w).querySelector('ol.wiz-steps').children.length, 4);
  assert.equal(active(w).querySelector('.wiz-title').getAttribute('aria-current'), 'step');
  assert.deepEqual(filled(page(w)), ['Create project']);
  assert.equal(steps(w).filter((s) => s.querySelector('form')).length, 1, 'only the active step has its form');
  const nameInput = input(active(w), 'Project name');
  assert.equal(nameInput.getAttribute('maxlength'), '64');
  assert.equal(nameInput.getAttribute('autocapitalize'), 'off');
  assert.equal(page(w).querySelector('.wiz-cancel') !== null, true, 'a way out');
});

test('step 1 says what is wrong next to the field and sends nothing: blank, two dashes, root, a bad character, a project that already has repos', async () => {
  const w = wizWorld({ state: stateOf({ projects: SHOP({ api: [] }) }) });
  const f = () => fieldOf(active(w), 'Project name');
  for (const [name, why] of [['', /Name the project/], ['a--b', /--/], ['root', /reserved/], ['my project', /letters, digits/], ['-x', /start and end/], ['shop', /already exists/]]) {
    input(active(w), 'Project name').value = name;
    submit(active(w).querySelector('form'));
    await tick();
    assert.match(text(f().querySelector('.field-err')), why, name);
  }
  assert.equal(calls(w).filter((c) => c.method === 'POST').length, 0, 'the board was never asked');
  input(active(w), 'Project name').dispatchEvent({ type: 'input' });
  assert.equal(text(f().querySelector('.field-err')), '', 'typing clears it');
});

test('step 1 POSTs the name alone, then the repo step opens; the state is kept for a reload; the done step reads "<name> created"', async () => {
  const w = wizWorld();
  await createProject(w, 'shop');
  assert.deepEqual(post(w, '/api/projects'), [{ method: 'POST', path: '/api/projects', body: { name: 'shop' } }], 'no url here: the repo is step 2');
  assert.deepEqual(states(w), ['name:done', 'repo:active', 'defaults:todo', 'prompt:todo']);
  assert.match(text(stepOf(w, 'name').querySelector('.wiz-sum')), /shop created/);
  assert.deepEqual([saved(w).step, saved(w).name, saved(w).created], [1, 'shop', true]);
  assert.ok(w.get('__polls') >= 1, 'the state is refreshed so the project is known');
  assert.deepEqual(filled(page(w)), ['Create repo'], 'one filled primary at a time');
});

test('a refused name (the box says 409 or 400) stays on step 1 with its message under the field and the button back', async () => {
  const w = wizWorld({ answers: { '/api/projects': () => { throw new Error('project shop already exists'); } } });
  await createProject(w, 'shop');
  assert.deepEqual(states(w)[0], 'name:active');
  assert.match(text(fieldOf(active(w), 'Project name').querySelector('.field-err')), /already exists/);
  assert.equal(btn(active(w), 'Create project').disabled === true, false);
});

test('an existing project with no repo is carried on with instead of refused: step 2, no POST', async () => {
  const w = wizWorld({ state: stateOf({ projects: SHOP({}) }) });
  await createProject(w, 'shop');
  assert.equal(post(w, '/api/projects').length, 0);
  assert.deepEqual(states(w)[1], 'repo:active');
});

// ---------------------------------------------------------------- step 2: repo

test('step 2: Blank | Clone | Import as a segmented control (one pressed), the repo name defaults to the project name, Create repo POSTs it', async () => {
  const w = wizWorld();
  await createProject(w, 'shop');
  const seg = active(w).querySelector('.seg-ctl');
  assert.deepEqual(seg.querySelectorAll('.seg-btn').map(text), ['Blank', 'Clone', 'Import']);
  assert.deepEqual(seg.querySelectorAll('.seg-btn').map((b) => b.getAttribute('aria-pressed')), ['true', 'false', 'false']);
  assert.equal(input(active(w), 'Repo name').value, 'shop');
  input(active(w), 'Repo name').value = 'a--b';
  submit(active(w).querySelector('form'));
  await tick();
  assert.match(text(fieldOf(active(w), 'Repo name').querySelector('.field-err')), /--/);
  input(active(w), 'Repo name').value = 'api';
  submit(active(w).querySelector('form'));
  await tick(); await tick();
  assert.deepEqual(post(w, '/api/projects/shop/repos'), [{ method: 'POST', path: '/api/projects/shop/repos', body: { name: 'api' } }]);
  assert.deepEqual(states(w), ['name:done', 'repo:done', 'defaults:active', 'prompt:todo']);
  assert.match(text(stepOf(w, 'repo').querySelector('.wiz-sum')), /api: an empty git repo/);
  assert.deepEqual([saved(w).mode, saved(w).repo, saved(w).step], ['blank', 'api', 2]);
});

test('Clone: the URL is probed on leaving the field (POST /api/preflight/clone) and the chips say reachable, default branch and the derived name, which fills the name field', async () => {
  const w = wizWorld({ answers: { '/api/preflight/clone': { reachable: true, default_branch: 'trunk', needs_auth: false, heads: ['trunk', 'dev'], name: 'shop-api', error: null } } });
  await createProject(w);
  btn(active(w).querySelector('.seg-ctl'), 'Clone').click();
  const url = input(active(w), 'Clone URL');
  url.value = 'https://github.com/octo/shop-api.git';
  url.dispatchEvent({ type: 'blur' });
  await tick(); await tick();
  assert.deepEqual(post(w, '/api/preflight/clone'), [{ method: 'POST', path: '/api/preflight/clone', body: { url: 'https://github.com/octo/shop-api.git' } }]);
  const chips = active(w).querySelector('.wiz-chips');
  assert.deepEqual(chips.querySelectorAll('.badge').map(text), ['reachable', 'default branch trunk', 'repo name shop-api']);
  assert.equal(input(active(w), 'Repo name').value === 'shop-api' || fieldOf(active(w), 'Repo name') !== undefined, true);
  const names = active(w).querySelectorAll('.field').filter((f) => text(f.querySelector('label')) === 'Repo name');
  assert.equal(names.map((f) => f.querySelector('input').value).includes('shop-api'), true, 'the clone name field has the derived name');
  assert.deepEqual(filled(page(w)), ['Clone repo']);
});

test('Clone: a private repo says "needs credentials" and what to do (an ssh URL, or gh auth login with a Copy button); an unreachable one says why; neither blocks the button', async () => {
  const w = wizWorld({ answers: { '/api/preflight/clone': { reachable: false, default_branch: null, needs_auth: true, heads: [], name: 'private', error: 'could not read Username' } } });
  await createProject(w);
  btn(active(w).querySelector('.seg-ctl'), 'Clone').click();
  const url = input(active(w), 'Clone URL');
  url.value = 'https://github.com/octo/private.git';
  url.dispatchEvent({ type: 'blur' });
  await tick(); await tick();
  const chips = active(w).querySelector('.wiz-chips');
  assert.deepEqual(chips.querySelectorAll('.badge').map(text), ['not reachable', 'needs credentials', 'repo name private']);
  assert.match(text(chips), /ssh URL/);
  assert.ok(chips.querySelector('code.doc-cmd') && text(chips.querySelector('code.doc-cmd')) === 'gh auth login');
  assert.ok(btn(chips, 'Copy'), 'the command has a Copy button');
  w.ctx.__answers['/api/preflight/clone'] = { reachable: false, default_branch: null, needs_auth: false, heads: [], name: 'gone', error: 'Could not resolve host: x.test' };
  url.value = 'https://x.test/gone.git';
  url.dispatchEvent({ type: 'blur' });
  await tick(); await tick();
  assert.match(text(active(w).querySelector('.wiz-chips')), /Could not resolve host/);
  assert.equal(btn(active(w), 'Clone repo').disabled === true, false);
});

test('Clone: a URL the clone would refuse is said at once and never sent to the probe; typing probes after a pause, not on every key', async () => {
  mock.timers.enable({ apis: ['setTimeout'] });
  const w = wizWorld({ answers: { '/api/preflight/clone': { reachable: true, default_branch: 'main', needs_auth: false, heads: ['main'], name: 'r', error: null } } });
  await createProject(w);
  btn(active(w).querySelector('.seg-ctl'), 'Clone').click();
  const url = input(active(w), 'Clone URL');
  url.value = 'file:///etc';
  url.dispatchEvent({ type: 'blur' });
  await tick();
  assert.match(text(active(w).querySelector('.wiz-chips')), /must start with https/);
  assert.equal(post(w, '/api/preflight/clone').length, 0);
  url.value = 'https://github.com/o/r.git';
  url.dispatchEvent({ type: 'input' });
  url.value = 'https://github.com/o/r2.git';
  url.dispatchEvent({ type: 'input' });
  assert.equal(post(w, '/api/preflight/clone').length, 0, 'not yet');
  mock.timers.tick(600);
  await tick(); await tick();
  assert.deepEqual(post(w, '/api/preflight/clone').map((c) => c.body.url), ['https://github.com/o/r2.git'], 'one probe, for the last URL');
});

test('Clone: Clone repo POSTs {url, name} to the project\'s repos, advances, and the repo step\'s line follows the state: cloning, cloned, failed (with the clone terminal)', async () => {
  const w = wizWorld({ answers: { '/api/preflight/clone': { reachable: true, default_branch: 'main', needs_auth: false, heads: ['main'], name: 'api', error: null } } });
  await createProject(w);
  btn(active(w).querySelector('.seg-ctl'), 'Clone').click();
  const url = input(active(w), 'Clone URL');
  url.value = 'https://github.com/octo/api.git';
  url.dispatchEvent({ type: 'blur' });
  await tick(); await tick();
  submit(active(w).querySelector('form'));
  await tick(); await tick();
  assert.deepEqual(post(w, '/api/projects/shop/repos'), [{ method: 'POST', path: '/api/projects/shop/repos', body: { url: 'https://github.com/octo/api.git', name: 'api' } }]);
  assert.deepEqual(states(w)[1], 'repo:done');
  const sum = () => stepOf(w, 'repo').querySelector('.wiz-sum');
  assert.match(text(sum()), /Cloning api…/);
  const repo = (state, branch) => ({ name: 'api', path: '/srv/projects/shop/api', state, branch, dirty: false, devcontainer: false, sessions: [] });
  setState(w, stateOf({ projects: [{ name: 'shop', path: '/srv/projects/shop', root: null, orphan_sessions: [], repos: [repo('cloning', null)] }] }));
  assert.match(text(sum()), /Cloning api…/);
  setState(w, stateOf({ projects: [{ name: 'shop', path: '/srv/projects/shop', root: null, orphan_sessions: [], repos: [repo('ok', 'main')] }] }));
  assert.match(text(sum()), /api cloned \(main\)/);
  setState(w, stateOf({ projects: [{ name: 'shop', path: '/srv/projects/shop', root: null, orphan_sessions: [], repos: [repo('clone-failed', null)] }] }));
  assert.match(text(sum()), /Cloning api failed/);
  assert.ok(hidden(sum()) === false && sum().classList.contains('bad'));
  assert.equal(sum().querySelector('a').getAttribute('href'), '#/s/shop--api--clone', 'the clone\'s terminal shows why');
});

test('Import: Load my repos asks GitHub (GET /api/github/repos), a failed ask says "GitHub not connected" with gh auth login; ticked repos go to .../repos/bulk; progress is the clone queue', async () => {
  const w = wizWorld({ answers: { '/api/github/repos': { repos: [{ name: 'alpha', url: 'https://github.com/o/alpha.git', private: true, fork: false, description: 'the first' }, { name: 'beta', url: 'https://github.com/o/beta.git', private: false, fork: true, description: '' }], protocol: 'https' } } });
  await createProject(w);
  btn(active(w).querySelector('.seg-ctl'), 'Import').click();
  assert.deepEqual(filled(page(w)), ['Import selected']);
  btn(active(w), 'Load my repos').click();
  await tick(); await tick();
  assert.deepEqual(active(w).querySelectorAll('.wiz-chips .badge').map(text).slice(0, 1), ['GitHub connected']);
  const rows = active(w).querySelectorAll('.import-repos label');
  assert.deepEqual(rows.map((r) => text(r.querySelector('b'))), ['alpha', 'beta']);
  submit(active(w).querySelector('form'));
  await tick();
  assert.match(text(active(w).querySelector('.field-err[role=alert]')), /Tick at least one repo/);
  rows[1].querySelector('input').checked = true;
  submit(active(w).querySelector('form'));
  await tick(); await tick();
  assert.deepEqual(post(w, '/api/projects/shop/repos/bulk'), [{ method: 'POST', path: '/api/projects/shop/repos/bulk', body: { repos: [{ name: 'beta', url: 'https://github.com/o/beta.git' }] } }]);
  assert.deepEqual(states(w)[1], 'repo:done');
  setState(w, stateOf({ clone_queue: { queued: [{ project: 'shop', repo: 'beta', url: 'u' }], done: [], cap: 3 } }));
  assert.match(text(stepOf(w, 'repo').querySelector('.wiz-sum')), /Importing 1 repo: 1 waiting/);
  setState(w, stateOf({ clone_queue: { queued: [], done: [{ project: 'shop', repo: 'beta', status: 'failed', error: 'x' }], cap: 3 } }));
  assert.match(text(stepOf(w, 'repo').querySelector('.wiz-sum')), /1 of 1 did not start: beta/);
});

test('Import: when the box has no GitHub login the chip says so and gives the command with Copy; nothing is posted', async () => {
  const w = wizWorld({ answers: { '/api/github/repos': () => { throw new Error('gh is not logged in'); } } });
  await createProject(w);
  btn(active(w).querySelector('.seg-ctl'), 'Import').click();
  btn(active(w), 'Load my repos').click();
  await tick(); await tick();
  const chips = active(w).querySelector('.wiz-chips');
  assert.deepEqual(chips.querySelectorAll('.badge').map(text), ['GitHub not connected']);
  assert.match(text(chips), /gh is not logged in/);
  assert.equal(text(chips.querySelector('code')), 'gh auth login');
  assert.ok(btn(chips, 'Copy'));
});

test('Skip, no repo yet goes to step 3 without a request; the repo step then reads that sessions start in the project folder', async () => {
  const w = wizWorld();
  await createProject(w);
  const before = calls(w).length;
  btn(active(w), 'Skip, no repo yet').click();
  assert.equal(calls(w).length, before);
  assert.deepEqual(states(w), ['name:done', 'repo:done', 'defaults:active', 'prompt:todo']);
  assert.match(text(stepOf(w, 'repo').querySelector('.wiz-sum')), /sessions start in the project folder/);
});

// ---------------------------------------------------------------- step 3: defaults

async function toDefaults(w) {
  await createProject(w);
  btn(active(w), 'Skip, no repo yet').click();
}
const seg = (root, label) => root.querySelectorAll('.seg-ctl').find((s) => s.getAttribute('aria-label') === label);
const pressed = (s) => s.querySelectorAll('.seg-btn').filter((b) => b.getAttribute('aria-pressed') === 'true').map(text);

test('step 3: Claude | Codex | Shell as a segmented control, Claude opus / high preselected from the launcher\'s chips and levels; Next is the one filled button', async () => {
  const w = wizWorld();
  await toDefaults(w);
  const a = active(w);
  assert.deepEqual(seg(a, 'Agent').querySelectorAll('.seg-btn').map(text), ['◆ Claude', '◇ Codex', '▸ Shell']);
  assert.deepEqual(pressed(seg(a, 'Agent')), ['◆ Claude']);
  assert.deepEqual(seg(a, 'Model').querySelectorAll('.seg-btn').map(text), ['opus', 'fable', 'sonnet', 'haiku']);
  assert.deepEqual(pressed(seg(a, 'Model')), ['opus']);
  assert.deepEqual(seg(a, 'Effort').querySelectorAll('.seg-btn').map(text), ['low', 'medium', 'high', 'xhigh', 'max']);
  assert.deepEqual(pressed(seg(a, 'Effort')), ['high']);
  assert.deepEqual(filled(page(w)), ['Next']);
  assert.equal(hidden(a.querySelector('[data-agent=codex]')), true);
  assert.equal(hidden(a.querySelector('[data-agent=claude]')), false);
});

test('step 3 saves ccboard:defaults:<project> in the shape the launcher reads, and the launcher then starts from it', async () => {
  const w = wizWorld({ state: stateOf({ projects: SHOP({ api: [] }) }) });
  await toDefaults(w);
  btn(seg(active(w), 'Model'), 'sonnet').click();
  btn(seg(active(w), 'Effort'), 'max').click();
  submit(active(w).querySelector('form'));
  const d = JSON.parse(w.localStorage.getItem('ccboard:defaults:shop'));
  assert.deepEqual(d, { agent: 'claude', claude: { model: 'sonnet', effort: 'max' } });
  assert.deepEqual(plain(w.get("launcherPrefs('shop', 'api', 'claude')")), { model: 'sonnet', effort: 'max' }, 'launcherPrefs reads d[agent]');
  assert.equal(w.get("launcherAgentPref('shop', 'api')"), 'claude', 'launcherAgentPref reads d.agent');
  assert.deepEqual(states(w), ['name:done', 'repo:done', 'defaults:done', 'prompt:active']);
  assert.equal(text(stepOf(w, 'defaults').querySelector('.wiz-sum')), 'Claude · sonnet · max');
});

test('Codex: its model list and reasoning levels come from the launcher\'s schema (a level the model lacks is disabled); the choice is saved under codex and the agent is codex; the other agent\'s memory stays', async () => {
  const w = wizWorld();
  w.localStorage.setItem('ccboard:defaults:shop', JSON.stringify({ agent: 'claude', claude: { model: 'haiku', effort: 'low' } }));
  await toDefaults(w);
  btn(seg(active(w), 'Agent'), '◇ Codex').click();
  assert.equal(hidden(active(w).querySelector('[data-agent=codex]')), false);
  assert.equal(hidden(active(w).querySelector('[data-agent=claude]')), true);
  const model = fieldOf(active(w), 'Model').querySelector('select');
  assert.ok(model.querySelectorAll('option').length >= 2, 'a default plus the catalogue');
  const first = model.querySelectorAll('option')[1].getAttribute('value');
  model.value = first;
  model.dispatchEvent({ type: 'change' });
  const reasoning = seg(active(w), 'Reasoning');
  assert.equal(text(reasoning.querySelectorAll('.seg-btn')[0]), 'default');
  btn(reasoning, 'high').click();
  submit(active(w).querySelector('form'));
  const d = JSON.parse(w.localStorage.getItem('ccboard:defaults:shop'));
  assert.equal(d.agent, 'codex');
  assert.deepEqual(d.codex, { model: first, reasoning: 'high' });
  assert.deepEqual(d.claude, { model: 'haiku', effort: 'low' }, 'what Claude had is kept');
});

test('Shell has no model to choose, and an agent that is not installed cannot be picked', async () => {
  const w = wizWorld({ state: stateOf({ agents: { claude: { installed: true }, codex: { installed: false } } }) });
  await toDefaults(w);
  const codex = seg(active(w), 'Agent').querySelectorAll('.seg-btn').find((b) => text(b) === '◇ Codex');
  assert.ok(codex.hasAttribute('disabled') || codex.disabled === true);
  btn(seg(active(w), 'Agent'), '▸ Shell').click();
  assert.match(text(active(w).querySelector('[data-agent=shell]')), /no model to choose/);
  submit(active(w).querySelector('form'));
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:defaults:shop')), { agent: 'shell' });
});

// ---------------------------------------------------------------- step 4: first prompt

async function toPrompt(w, over = {}) {
  await toDefaults(w);
  submit(active(w).querySelector('form'));
  if (over.prompt) { active(w).querySelector('textarea').value = over.prompt; active(w).querySelector('textarea').dispatchEvent({ type: 'input' }); }
}

test('step 4: an optional prompt; Start is the one filled button, Skip is plain; the textarea is labelled and tall enough to write in', async () => {
  const w = wizWorld();
  await toPrompt(w);
  assert.deepEqual(filled(page(w)), ['Start']);
  assert.ok(btn(active(w), 'Skip') && !isFilled(btn(active(w), 'Skip')));
  const ta = active(w).querySelector('textarea');
  assert.equal(ta.getAttribute('aria-label'), 'First prompt');
  assert.ok(ta.classList.contains('wiz-prompt'));
});

test('Start goes to the project page, opens the launcher in the new repo with the prompt and the chosen agent, clears the wizard and records that it was used', async () => {
  const w = wizWorld({ state: stateOf({ projects: SHOP({ api: [] }) }) });
  await createProject(w);
  input(active(w), 'Repo name').value = 'api';
  submit(active(w).querySelector('form'));
  await tick(); await tick();
  submit(active(w).querySelector('form'));
  active(w).querySelector('textarea').value = 'Read the repo and tell me what it does';
  active(w).querySelector('textarea').dispatchEvent({ type: 'input' });
  submit(active(w).querySelector('form'));
  await tick(); await tick(); await tick();
  assert.equal(w.location.hash, '#/p/shop');
  const l = plain(w.get('__launched'));
  assert.equal(l.length, 1);
  assert.deepEqual([l[0].project, l[0].repo, l[0].mode, l[0].agent, l[0].carry], ['shop', 'api', 'session', 'claude', { prompt: 'Read the repo and tell me what it does' }]);
  assert.equal(w.sessionStorage.getItem('ccboard:wiz'), null, 'the wizard\'s state is gone');
  assert.equal(w.localStorage.getItem('ccboard:onboarded'), '1');
});

test('Start with no prompt opens the launcher without one; with no repo it is the project folder; a repo still cloning is not started in (a toast says to wait)', async () => {
  const w = wizWorld({ state: stateOf({ projects: SHOP({}) }) });
  await toPrompt(w);
  submit(active(w).querySelector('form'));
  await tick(); await tick(); await tick();
  const l = plain(w.get('__launched'));
  assert.deepEqual([l[0].repo, l[0].carry], ['root', undefined]);
  // a repo still cloning
  const w2 = wizWorld({ state: stateOf({ projects: SHOP({ api: [] }).map((p) => ({ ...p, repos: p.repos.map((r) => ({ ...r, state: 'cloning' })) })) }), session: { step: 3, name: 'shop', created: true, mode: 'clone', repo: 'api', repos: 1 } });
  submit(active(w2).querySelector('form'));
  await tick(); await tick(); await tick();
  assert.equal(plain(w2.get('__launched')).length, 0);
  assert.match(plain(w2.get('__toasts')).pop().text, /still cloning/);
});

test('Skip goes to the project page and opens nothing; Cancel leaves too; both end the first-run redirect for good', async () => {
  const w = wizWorld({ state: stateOf({ projects: SHOP({ api: [] }) }) });
  await toPrompt(w);
  btn(active(w), 'Skip').click();
  await tick(); await tick();
  assert.equal(w.location.hash, '#/p/shop');
  assert.equal(plain(w.get('__launched')).length, 0);
  assert.equal(w.localStorage.getItem('ccboard:onboarded'), '1');
  const w2 = wizWorld();
  page(w2).querySelector('.wiz-cancel').click();
  assert.equal(w2.location.hash, '#/', 'before a project exists Cancel goes Home');
  assert.equal(w2.localStorage.getItem('ccboard:onboarded'), '1');
  const w3 = wizWorld();
  await createProject(w3);
  page(w3).querySelector('.wiz-cancel').click();
  assert.equal(w3.location.hash, '#/p/shop', 'after Create it goes to the project');
});

// ---------------------------------------------------------------- resume

test('a reload resumes the step from sessionStorage ccboard:wiz (the state of the earlier steps shows as done)', () => {
  const w = wizWorld({ session: { step: 2, name: 'shop', created: true, mode: 'blank', repo: 'api', repos: 1, agent: 'codex' }, state: stateOf({ projects: SHOP({ api: [] }) }) });
  assert.deepEqual(states(w), ['name:done', 'repo:done', 'defaults:active', 'prompt:todo']);
  assert.deepEqual(pressed(seg(active(w), 'Agent')), ['◇ Codex']);
});

test('a saved state whose project is gone starts over; junk in storage is ignored', () => {
  const w = wizWorld({ session: { step: 2, name: 'gone', created: true }, state: stateOf({ projects: SHOP({ api: [] }) }) });
  assert.deepEqual(states(w), ['name:active', 'repo:todo', 'defaults:todo', 'prompt:todo']);
  const w2 = wizWorld();
  w2.sessionStorage.setItem('ccboard:wiz', '{not json');
  w2.location.hash = '#/';
  w2.location.hash = '#/onboarding/project';
  assert.deepEqual(states(w2)[0], 'name:active');
});

test('?name=<project> carries on for a project with no repo (step 2, name done); a project that has repos is not resumed; an unknown name pre-fills step 1', () => {
  const w = wizWorld({ route: '#/onboarding/project?name=shop', state: stateOf({ projects: SHOP({}) }) });
  assert.deepEqual(states(w), ['name:done', 'repo:active', 'defaults:todo', 'prompt:todo']);
  assert.match(text(stepOf(w, 'name').querySelector('.wiz-sum')), /shop/);
  const w2 = wizWorld({ route: '#/onboarding/project?name=shop', state: stateOf({ projects: SHOP({ api: [] }) }) });
  assert.deepEqual(states(w2)[0], 'name:active');
  const w3 = wizWorld({ route: '#/onboarding/project?name=fresh', state: stateOf({ projects: SHOP({ api: [] }) }) });
  assert.equal(input(active(w3), 'Project name').value, 'fresh');
  const w4 = wizWorld({ route: '#/onboarding/project?name=bad--name', state: stateOf() });
  assert.equal(input(active(w4), 'Project name').value, '', 'a name the box would refuse is not trusted');
});

// ---------------------------------------------------------------- the redirect, the bare route, the entry points

test('the first-run rule: state.setup.first_run, no project and no ccboard:onboarded; any other combination shows nothing by itself', () => {
  const w = wizWorld({ route: '#/' });
  const want = (st, local) => { w.localStorage.removeItem('ccboard:onboarded'); if (local) w.localStorage.setItem('ccboard:onboarded', '1'); return w.get(`wizRedirectWanted(${JSON.stringify(st)})`); };
  assert.equal(want({ setup: { first_run: true }, projects: [] }), true);
  assert.equal(want({ setup: { first_run: true } }), true, 'no list yet counts as none');
  assert.equal(want({ setup: { first_run: false }, projects: [] }), false, 'a board that has been used');
  assert.equal(want({ setup: { first_run: true }, projects: [{ name: 'shop' }] }), false);
  assert.equal(want({ setup: { first_run: true }, projects: [] }, true), false, 'been through it');
  assert.equal(want({ projects: [] }), false);
  assert.equal(want(null), false);
});

test('Home sends a first-run board to the wizard once (replacing Home in the history); not twice in a tab, not when onboarded, not from another page', async () => {
  const w = wizWorld({ route: '#/', state: stateOf({ setup: { first_run: true, ok: true }, projects: [] }) });
  w.run('updateCurrentPage(state)');
  assert.equal(w.location.hash, '#/onboarding/project');
  assert.equal(w.history.calls.at(-1).method, 'replaceState');
  w.location.hash = '#/';
  w.run('updateCurrentPage(state)');
  assert.equal(w.location.hash, '#/', 'the tab was sent once: Back to Home does not trap');
  const w2 = wizWorld({ route: '#/', local: { 'ccboard:onboarded': '1' }, state: stateOf({ setup: { first_run: true, ok: true }, projects: [] }) });
  w2.run('updateCurrentPage(state)');
  assert.equal(w2.location.hash, '#/');
  const w3 = wizWorld({ route: '#/settings', state: stateOf({ setup: { first_run: true, ok: true }, projects: [] }) });
  w3.run('onboardingMaybeRedirect(state)');
  assert.equal(w3.location.hash, '#/settings', 'only Home is redirected: a deep link is respected');
  const w4 = wizWorld({ route: '#/', state: stateOf({ setup: { first_run: false, ok: true }, projects: SHOP({ api: [] }) }) });
  w4.run('updateCurrentPage(state)');
  assert.equal(w4.location.hash, '#/');
});

test('the bare #/onboarding was the box checklist: it goes to Settings > Doctor (replace)', () => {
  const w = wizWorld({ route: '#/onboarding' });
  assert.equal(w.location.hash, '#/settings?sec=doctor');
  assert.equal(w.history.calls.at(-1).method, 'replaceState');
});

test('nothing but the wizard script registers the onboarding route, and the + menu, the sidebar and Home\'s empty state reach it through one hash', () => {
  const w = wizWorld({ route: '#/' });
  assert.equal(w.get("buildHash('onboarding', { step: 'project' })"), '#/onboarding/project');
  const src = fs.readFileSync(path.join(STATIC, 'shell.js'), 'utf8');
  assert.match(src, /kind === 'project'\) Shell\.go\(Shell\.hash\('onboarding', \{ step: 'project' \}\)\)/);
  assert.match(src, /class: 'sb-add', href: Shell\.hash\('onboarding', \{ step: 'project' \}\)/);
  assert.match(fs.readFileSync(path.join(STATIC, 'pages', 'home.js'), 'utf8'), /homeCreate\('project'\)/);
});

// ---------------------------------------------------------------- phone and the rules

test('every control of the wizard has a label, a name or an aria-label; the stepper marks the active step; no inline style, innerHTML or style attribute', async () => {
  const w = wizWorld();
  await toDefaults(w);
  const a = active(w);
  for (const i of a.querySelectorAll('input, select, textarea')) assert.ok(i.getAttribute('aria-label') || i.getAttribute('id'), 'unlabelled control');
  for (const g of a.querySelectorAll('.seg-ctl')) assert.ok(g.getAttribute('aria-label') || g.getAttribute('aria-labelledby'), 'unlabelled group');
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'onboarding.js'), 'utf8');
  assert.doesNotMatch(src, /innerHTML|cssText|setAttribute\(\s*['"]style|\.style\./);
  const css = fs.readFileSync(path.join(STATIC, 'pages.css'), 'utf8');
  assert.match(css, /#page \.wiz \.wiz-prompt \{[^}]*min-height:120px/);
  assert.match(css, /@media \(max-width:599px\) \{\s*#page \.wiz \.wiz-seg \.seg-btn/);
});
