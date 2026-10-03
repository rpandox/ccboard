// Contract tests for Shell.openCreate(kind) (shell.js) and the forms behind it (launcher.js): the + menu entries, the project sheet with its clone
// queue, the GitHub import and the batch prompt moved out of the v0.4 board into the sheet with the same API calls, and the rate-limit callout
// that render() puts back after renderBanner(). Real core.js, components.js, launcher.js, router.js, widgets.js and shell.js on minidom's DOM.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const tick = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');
const ISO = (minsAgo) => new Date(Date.now() - minsAgo * 60000).toISOString();

const STATE = () => ({
  user: 'alice@example.com', config: { projects_dir: '/srv/projects', code_https_port: 10000 },
  projects: [
    { name: 'shop', path: '/srv/projects/shop', root: { name: 'root', path: '/srv/projects/shop', root: true, state: 'project', sessions: [] }, orphan_sessions: [],
      repos: [{ name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] },
        { name: 'web', path: '/srv/projects/shop/web', state: 'ok', branch: 'dev', dirty: false, devcontainer: false, sessions: [] }] },
  ],
  clone_queue: { queued: [], done: [], cap: 3 }, pending_permissions: [], tasks: [], jobs: [], runs: [],
});

function sWorld({ state = STATE(), answers = {} } = {}) {
  const w = makeWorld();
  installDom(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', 'shell.js']) w.load(f);
  w.ctx.__calls = [];
  w.ctx.__toasts = [];
  w.ctx.__answers = answers;
  w.ctx.__polls = 0;
  w.ctx.__st = state;
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      for (const [prefix, v] of Object.entries(__answers)) if (path.startsWith(prefix)) { if (v && v.__error) throw new Error(v.__error); return typeof v === 'function' ? v({ method, path, body }) : v; }
      return { ok: true };
    };
    poll = async () => { __polls++; };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    renderBanner = () => {};
    state = __st;
  `);
  return w;
}

const calls = (w) => plain(w.get('__calls'));
const toasts = (w) => plain(w.get('__toasts'));
const sheet = (w) => w.document.getElementById('sheet');
const title = (w) => text(sheet(w).querySelector('.sheet-title'));
const field = (n, label) => n.querySelectorAll('.field').find((f) => text(f.querySelector('span')) === label);
const submit = (form) => form.dispatchEvent({ type: 'submit', preventDefault() {} });
const open = (w, kind) => w.run(`Shell.openCreate(${JSON.stringify(kind)})`);
const closed = (w) => { sheet(w).close(); };

// ---------------------------------------------------------------- the entry point

test('openCreate opens the sheet for every kind and says so; an unknown kind, no state or no sheet answers false', () => {
  const w = sWorld();
  const titles = { session: 'New session', task: 'New task', schedule: 'Schedule a run', project: 'New project', import: 'Import repos from GitHub', batch: 'Batch prompt across repos' };
  for (const [kind, want] of Object.entries(titles)) {
    assert.equal(open(w, kind), true, kind);
    assert.equal(sheet(w).open, true, kind);
    assert.equal(title(w), want, kind);
    closed(w);
  }
  assert.equal(open(w, 'nonsense'), false);
  assert.equal(sheet(w).open, false, 'nothing opened');
  w.run('state = null');
  assert.equal(open(w, 'project'), false, 'every form lists the box\'s repos: it waits for the first state');
  w.run('state = __st');
  sheet(w).remove();
  assert.equal(open(w, 'project'), false, 'without the sheet dialog (a partial page) a key is left alone');
});

test('the + menu entries call Shell.openCreate with session, task, schedule, project, import and batch', () => {
  const w = sWorld();
  w.run('globalThis.__kinds = []; Shell.openCreate = (k) => { __kinds.push(k); return true; };');
  const items = plain(w.run('Shell.createItems().map((i) => i.label)'));
  assert.deepEqual(items, ['New session', 'New task', 'Schedule', 'New project', 'Import from GitHub', 'Batch prompt']);
  w.run('Shell.createItems().forEach((i) => i.onClick())');
  assert.deepEqual(plain(w.get('__kinds')), ['session', 'task', 'schedule', 'project', 'import', 'batch']);
});

test('session, task and schedule go through the repo picker, then the launcher form', () => {
  const w = sWorld();
  open(w, 'session');
  const rows = sheet(w).querySelectorAll('.pick-row');
  assert.deepEqual(rows.map((r) => text(r.querySelector('.pr-name'))), ['shop/', 'shop/api', 'shop/web'], 'the project folder is a target for sessions');
  rows[1].click();
  assert.equal(title(w), 'New session · shop/api');
  assert.ok(sheet(w).querySelector('form.form'), 'the launcher form');
  closed(w);
  open(w, 'task');
  assert.deepEqual(sheet(w).querySelectorAll('.pick-row').map((r) => text(r.querySelector('.pr-name'))), ['shop/api', 'shop/web'], 'a task needs a repo');
  closed(w);
  open(w, 'schedule');
  assert.equal(title(w), 'Schedule a run');
});

// ---------------------------------------------------------------- the project sheet

test('the project sheet: POST /api/projects {name}, a toast, and the sheet closes for a blank project', async () => {
  const w = sWorld();
  open(w, 'project');
  const form = sheet(w).querySelector('form.form');
  assert.match(text(sheet(w).querySelector('.sheet-body')), /A folder per project under \/srv\/projects/);
  field(form, 'name').querySelector('input').value = ' shop2 ';
  submit(form);
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/projects', body: { name: 'shop2' } });
  assert.deepEqual(toasts(w).pop(), { text: 'Project shop2 created', kind: 'ok' });
  assert.equal(sheet(w).open, false);
  assert.equal(w.get('__polls'), 1, 'the board refreshes');
});

test('a project with a clone URL keeps the sheet open so the queue shows the clone', async () => {
  const w = sWorld();
  open(w, 'project');
  const form = sheet(w).querySelector('form.form');
  field(form, 'name').querySelector('input').value = 'blog';
  field(form, 'clone URL').querySelector('input').value = 'https://example.invalid/blog.git';
  submit(form);
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/projects', body: { name: 'blog', url: 'https://example.invalid/blog.git' } });
  assert.equal(sheet(w).open, true);
  assert.equal(field(form, 'name').querySelector('input').value, '', 'the form is ready for the next one');
});

test('a failed create stays open and says why, inline and as a toast through setError', async () => {
  const w = sWorld({ answers: { '/api/projects': { __error: 'project name already exists' } } });
  w.run('globalThis.__errors = []; setError = (m) => { __errors.push(m); };');
  open(w, 'project');
  const form = sheet(w).querySelector('form.form');
  field(form, 'name').querySelector('input').value = 'shop';
  submit(form);
  await tick();
  assert.equal(sheet(w).open, true);
  assert.equal(text(form.querySelector('.form-status')), 'project name already exists');
  assert.ok(form.querySelector('.form-status').classList.contains('bad'));
  assert.deepEqual(plain(w.get('__errors')), ['project name already exists']);
});

test('the clone queue line follows st.clone_queue through the sheet watch: queued clones, failures with Clear', async () => {
  const w = sWorld();
  open(w, 'project');
  const line = () => sheet(w).querySelector('.clone-queue');
  assert.equal(text(line()), '', 'nothing queued');
  assert.equal(typeof w.get('Shell.formWatch'), 'function', 'render() calls the watch');
  w.ctx.__st.clone_queue = { queued: [{ project: 'blog', repo: 'web' }, { project: 'blog', repo: 'api' }], done: [], cap: 3 };
  w.run('Shell.formWatch()');
  assert.equal(text(line()), '2 clones queued (max 3 at once) ');
  w.ctx.__st.clone_queue = { queued: [{ project: 'blog', repo: 'web' }], done: [{ status: 'failed', repo: 'blog/api', error: 'auth failed' }, { status: 'ok', repo: 'blog/web' }], cap: 3 };
  w.run('Shell.formWatch()');
  assert.match(text(line()), /^1 clone queued \(max 3 at once\) 1 failed: blog\/api \(auth failed\) Clear$/);
  const keep = line().querySelector('.bad');
  w.run('Shell.formWatch()');
  assert.equal(line().querySelector('.bad'), keep, 'unchanged: not rebuilt');
  line().querySelector('button').click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/clone-queue/clear' });
  closed(w);
  assert.equal(w.get('Shell.formWatch'), null, 'the watch goes with the sheet');
});

test('the project sheet offers the two bulk entries the v0.4 board had beside its form', () => {
  const w = sWorld();
  open(w, 'project');
  const links = sheet(w).querySelectorAll('.sheet-links button');
  assert.deepEqual(links.map(text), ['Import from GitHub…', 'Batch prompt…']);
  links[0].click();
  assert.equal(title(w), 'Import repos from GitHub', 'the same sheet is swapped in place');
  assert.equal(w.get('Shell.formWatch'), null, 'and the project sheet\'s watch is dropped');
});

// ---------------------------------------------------------------- GitHub import

const REPOS = { protocol: 'https', repos: [
  { name: 'alpha', url: 'https://example.invalid/me/alpha.git', private: true, fork: false, description: 'the first' },
  { name: 'beta', url: 'https://example.invalid/me/beta.git', private: false, fork: true, description: 'the second' },
] };

test('import: Load lists the repos (GET /api/github/repos), filter narrows, Import selected POSTs the chosen ones to the target project', async () => {
  const w = sWorld({ answers: { '/api/github/repos': REPOS, '/api/projects/me/repos/bulk': { project: 'me' } } });
  open(w, 'import');
  const form = sheet(w).querySelector('form.form');
  const [owner, target, filter] = form.querySelectorAll('input[type=text]');
  const buttons = (label) => form.querySelectorAll('button').find((b) => text(b) === label);
  buttons('Load').click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'GET', path: '/api/github/repos' });
  assert.equal(target.value, 'alice', 'the target defaults to the user name');
  assert.equal(text(form.querySelector('.form-status')), '2 repos (https)');
  const boxes = () => form.querySelectorAll('input[type=checkbox]');
  assert.equal(boxes().length, 2);
  owner.value = 'some-org';
  buttons('Load').click();
  await tick();
  assert.equal(calls(w).pop().path, '/api/github/repos?owner=some-org');
  filter.value = 'bet';
  filter.dispatchEvent({ type: 'input' });
  assert.equal(boxes().length, 1, 'the filter narrows the list');
  assert.equal(boxes()[0].getAttribute('data-name'), 'beta');
  filter.value = '';
  filter.dispatchEvent({ type: 'input' });
  target.value = 'me';
  boxes().forEach((b) => { b.checked = true; });
  boxes()[0].checked = false;                                             // only beta
  buttons('Import selected').click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/projects/me/repos/bulk', body: { repos: [{ name: 'beta', url: 'https://example.invalid/me/beta.git' }] } });
  assert.deepEqual(toasts(w).pop(), { text: 'Import queued', kind: 'ok' });
  assert.equal(sheet(w).open, false);
});

test('import: nothing selected, or no target, says so and posts nothing; a failed load stays open', async () => {
  const w = sWorld({ answers: { '/api/github/repos': { __error: 'gh is not logged in' } } });
  open(w, 'import');
  const form = sheet(w).querySelector('form.form');
  const buttons = (label) => form.querySelectorAll('button').find((b) => text(b) === label);
  buttons('Load').click();
  await tick();
  assert.equal(text(form.querySelector('.form-status')), 'gh is not logged in');
  const before = calls(w).length;
  buttons('Import selected').click();
  await tick();
  assert.equal(text(form.querySelector('.form-status')), 'nothing selected');
  assert.equal(calls(w).length, before, 'no POST');
  assert.equal(sheet(w).open, true);
});

// ---------------------------------------------------------------- batch prompt

test('batch: pick repos and write a prompt, then POST /api/batch with the mode, turns and budget', async () => {
  const w = sWorld({ answers: { '/api/batch': { batch_id: 'b1', jobs: [{ id: 1 }, { id: 2 }], started: [1] } } });
  open(w, 'batch');
  const form = sheet(w).querySelector('form.form');
  const run = form.querySelectorAll('button').find((b) => text(b) === 'Run on selected repos');
  run.click();
  await tick();
  assert.equal(text(form.querySelector('.form-status')), 'pick repos and write a prompt');
  assert.equal(calls(w).length, 0, 'nothing posted');
  assert.deepEqual(form.querySelectorAll('.batch-repos label').map(text), ['shop/api', 'shop/web'], 'repos with a working clone; the project folder is not a repo');
  form.querySelectorAll('.batch-repos input[type=checkbox]').forEach((b) => { b.checked = true; });
  form.querySelector('textarea').value = 'update the changelog';
  field(form, 'name').querySelector('input').value = 'changelog';
  field(form, 'permission mode').querySelector('select').value = 'plan';
  field(form, 'max turns').querySelector('input').value = '12';
  field(form, 'max $ per repo').querySelector('input').value = '2.5';
  run.click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/batch', body: { prompt: 'update the changelog', repos: ['shop/api', 'shop/web'], name: 'changelog', permission_mode: 'plan', max_turns: 12, max_budget_usd: 2.5 } });
  assert.deepEqual(toasts(w).pop(), { text: 'Batch queued', kind: 'ok' });
  assert.equal(sheet(w).open, false);
});

test('batch: a failed call stays open with the reason; Invert flips the checks', async () => {
  const w = sWorld({ answers: { '/api/batch': { __error: 'too many repos' } } });
  open(w, 'batch');
  const form = sheet(w).querySelector('form.form');
  const boxes = form.querySelectorAll('.batch-repos input[type=checkbox]');
  boxes[0].checked = true;
  form.querySelectorAll('button').find((b) => text(b) === 'Invert').click();
  assert.deepEqual(boxes.map((b) => !!b.checked), [false, true]);
  form.querySelector('textarea').value = 'go';
  form.querySelectorAll('button').find((b) => text(b) === 'Run on selected repos').click();
  await tick();
  assert.equal(text(form.querySelector('.form-status')), 'too many repos');
  assert.equal(sheet(w).open, true);
});

test('the v0.4 entry points openImport() and openBatch() open the same sheets', () => {
  const w = sWorld();
  w.run('openImport()');
  assert.equal(title(w), 'Import repos from GitHub');
  w.run('openBatch()');
  assert.equal(title(w), 'Batch prompt across repos');
  assert.equal(sheet(w).querySelectorAll('.modal-box').length, 0, 'not the legacy #modal');
  assert.equal(w.get('ui.modal'), false);
});

test('Cancel closes the sheet from every form', () => {
  const w = sWorld();
  for (const kind of ['project', 'import', 'batch']) {
    open(w, kind);
    sheet(w).querySelectorAll('button').find((b) => text(b) === 'Cancel').click();
    assert.equal(sheet(w).open, false, kind);
  }
});

// ---------------------------------------------------------------- the callout in render()

test('render() puts the rate-limit callout back right after renderBanner() emptied the banner', () => {
  const w = sWorld();
  w.run(`globalThis.__log = [];
    renderBanner = () => { const b = document.getElementById('banner'); b.textContent = ''; b.className = ''; __log.push('renderBanner'); };
    const real = Widgets.limitBanner; Widgets.limitBanner = (st) => { __log.push('limitBanner'); return real(st); };`);
  w.ctx.__st.rate_limited = { value: { session: 'shop--api--s1', message: 'limit', kind: '5h', resets_at: Math.floor(Date.now() / 1000) + 3600 }, at: ISO(1) };
  w.run('render(true)');
  assert.deepEqual(plain(w.get('__log')), ['renderBanner', 'limitBanner']);
  assert.ok(w.document.querySelector('#banner .limit-callout'));
  w.run('render(true)');
  assert.equal(w.document.querySelectorAll('#banner .limit-callout').length, 1);
  w.ctx.__st.rate_limited = null;
  w.run('render(true)');
  assert.equal(w.document.querySelector('#banner .limit-callout'), null, 'gone with the limit');
});

test('setError (wrapped by the shell) puts the callout back too, and a missing Widgets.limitBanner is not an error', () => {
  const w = sWorld();
  w.run('Shell.listen()');                                               // wraps setError once
  w.run('globalThis.__kept = 0; Widgets.limitBanner = () => { __kept++; return null; };');
  w.run('setError("boom")');
  assert.equal(w.get('__kept'), 1, 'limitBanner ran after the banner repainted');
  w.run('Widgets.limitBanner = undefined');
  assert.doesNotThrow(() => w.run('Shell.limit(state)'));
  w.run('Widgets.limitBanner = () => { throw new Error("widget bug"); }; console = { error() {} }');
  assert.doesNotThrow(() => w.run('Shell.limit(state)'), 'a widget error never breaks render()');
});
