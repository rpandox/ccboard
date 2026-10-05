// Contract tests for the 10x forms pass (v0.5.6d, rule 3): the labelled field() with its inline error slot, the first field focused only on a
// fine pointer, the dialog (not the close button) taking the focus on a phone, the failed launch that says why NEXT TO the field (the sheet
// covers the banner), the schedule form (labels above, Advanced collapsed, remembered per repo), the add-repo form, the back button in the sheet
// header, and the list tools that left the footer. Real core.js, components.js, launcher.js, router.js, widgets.js and shell.js on minidom's DOM.
// v0.5.13: + session and + task now open the launcher sheet (launch() -> openLauncher; launcher.test.mjs tests it). This file pins the OLD forms, sessionForm / taskForm / jobForm, which stay
// working for the code and the tests that call them, so fWorld sends the last hop of the create route (Shell.launchAt) to Shell.showForm, the way Shell.openCreate reached them before.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const tick = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');

const STATE = () => ({
  user: 'alice@example.com', config: { projects_dir: '/srv/projects', code_https_port: 10000 },
  projects: [
    { name: 'shop', path: '/srv/projects/shop', root: { name: 'root', path: '/srv/projects/shop', root: true, state: 'project', branch: null, dirty: null, sessions: [] }, orphan_sessions: [],
      repos: [{ name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] },
        { name: 'web', path: '/srv/projects/shop/web', state: 'ok', branch: 'dev', dirty: false, devcontainer: false, sessions: [] }] },
  ],
  clone_queue: { queued: [], done: [], cap: 3 }, pending_permissions: [], tasks: [], jobs: [], runs: [],
});

/** coarse: the phone (matchMedia answers pointer:coarse); forceCoarse: the html.force-coarse QA switch. */
function fWorld({ answers = {}, coarse = false, forceCoarse = false } = {}) {
  const tabs = [];
  const extra = { open: () => { const t = { close() { t.closed = true; }, location: '' }; tabs.push(t); return t; }, __tabs: tabs };
  if (coarse) extra.matchMedia = (q) => ({ matches: /pointer:\s*coarse/.test(q), addEventListener() {}, removeEventListener() {} });
  const w = makeWorld(extra);
  installDom(w);
  if (forceCoarse) w.document.documentElement.classList.add('force-coarse');
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', 'shell.js']) w.load(f);
  w.ctx.__tabs = tabs; w.ctx.__calls = []; w.ctx.__errors = []; w.ctx.__answers = answers; w.ctx.__polls = 0; w.ctx.__st = STATE(); w.ctx.__nav = [];
  w.run('Shell.launchAt = (kind, e) => Shell.showForm(kind, e);');          // the old forms, not the launcher sheet (see the header)
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      for (const [prefix, v] of Object.entries(__answers)) {
        if (!path.startsWith(prefix)) continue;
        const r = typeof v === 'function' ? v({ method, path, body }) : v;
        if (r && r.__error) throw new Error(r.__error);
        return r;
      }
      return { ok: true };
    };
    poll = async () => { __polls++; };
    toast = () => {};
    setError = (m) => { __errors.push(m); };
    openPage = (u) => { __nav.push(u); };
    renderBanner = () => {};
    renderProjects = () => {};
    state = __st;
  `);
  return w;
}

const calls = (w) => plain(w.get('__calls'));
const sheet = (w) => w.document.getElementById('sheet');
const form = (w) => sheet(w).querySelector('form.form');
const submit = (f) => f.dispatchEvent({ type: 'submit', preventDefault() {} });
const fieldOf = (n, re) => n.querySelectorAll('.field').find((f) => re.test(text(f.querySelector('label'))));
const open = (w, kind, ctx) => w.run(`Shell.openCreate(${JSON.stringify(kind)}, ${JSON.stringify(ctx || null)})`);
const SHOP_API = { project: 'shop', repo: 'api' };

// ---------------------------------------------------------------- field()

test('field(): the label is tied to an input, select or textarea with for/id; aria-describedby names the hint and the error slot', () => {
  const w = fWorld();
  const input = w.document.createElement('input');
  const f = w.get('field')('Session name', input, 'Blank takes the next free one.');
  const label = f.querySelector('label');
  assert.equal(text(label), 'Session name');
  assert.ok(input.getAttribute('id'), 'the control got an id');
  assert.equal(label.getAttribute('for'), input.getAttribute('id'));
  const hint = f.querySelector('.field-hint');
  const err = f.querySelector('.field-err');
  assert.equal(text(hint), 'Blank takes the next free one.');
  assert.equal(err.getAttribute('role'), 'alert');
  assert.equal(err.getAttribute('class').includes('bad'), true, 'red through the existing .bad until the stylesheet restyles it');
  assert.deepEqual(input.getAttribute('aria-describedby').split(' '), [hint.getAttribute('id'), err.getAttribute('id')]);
  const keep = w.document.createElement('select');
  keep.setAttribute('id', 'mine');
  const g = w.get('field')('Effort', keep);
  assert.equal(g.querySelector('label').getAttribute('for'), 'mine', 'an id that is there is kept');
  assert.equal(g.querySelector('.field-hint'), null, 'no hint, no hint node');
  assert.deepEqual(keep.getAttribute('aria-describedby').split(' '), [g.querySelector('.field-err').getAttribute('id')]);
});

test('field(): a wrapper (checks, the segmented control) is named with aria-labelledby, so a click on the label never toggles its first checkbox; _labelFor picks the control inside', () => {
  const w = fWorld();
  const checks = w.document.createElement('div');
  checks.append(w.document.createElement('input'));
  const f = w.get('field')('Repos', checks);
  assert.equal(f.querySelector('label').getAttribute('for'), null, 'no for: the wrapper is not labelable');
  assert.equal(checks.getAttribute('aria-labelledby'), f.querySelector('label').getAttribute('id'));
  assert.equal(checks.getAttribute('role'), 'group');
  const box = w.document.createElement('div');
  const sel = w.document.createElement('select');
  box.append(sel, w.document.createElement('input'));
  box._labelFor = sel;
  const g = w.get('field')('Model', box);
  assert.equal(g.querySelector('label').getAttribute('for'), sel.getAttribute('id'), 'the label names the select, not the wrapper');
  assert.equal(box.getAttribute('aria-labelledby'), null);
});

test('fieldError(): fills the slot, flags the control, clears again, and focuses on request', () => {
  const w = fWorld();
  const input = w.document.createElement('input');
  const f = w.get('field')('Name', input);
  w.get('fieldError')(f, 'Name the project.');
  assert.equal(text(f.querySelector('.field-err')), 'Name the project.');
  assert.equal(input.getAttribute('aria-invalid'), 'true');
  assert.notEqual(w.document.activeElement, input, 'no focus unless asked');
  w.get('fieldError')(f, 'Still wrong.', true);
  assert.equal(w.document.activeElement, input);
  w.get('fieldError')(f, '');
  assert.equal(text(f.querySelector('.field-err')), '');
  assert.equal(input.getAttribute('aria-invalid'), null);
  assert.doesNotThrow(() => w.get('fieldError')(null, 'x'));
});

// ---------------------------------------------------------------- focus: fine pointers only

test('a fine pointer: the session form opens with the session name focused (the first field), the task form with its prompt', () => {
  const w = fWorld();
  open(w, 'session', SHOP_API);
  const name = fieldOf(form(w), /^Session name$/).querySelector('input');
  assert.equal(w.document.activeElement, name);
  open(w, 'task', SHOP_API);
  assert.equal(w.document.activeElement, form(w).querySelector('textarea'));
});

test('a phone: nothing is focused when a sheet opens (no soft keyboard, no ring), and the dialog itself holds the focus instead of the close button', () => {
  for (const world of [fWorld({ coarse: true }), fWorld({ forceCoarse: true })]) {
    for (const kind of ['session', 'task', 'schedule']) {
      open(world, kind, SHOP_API);
      assert.equal(world.document.activeElement, sheet(world), `${kind}: the dialog has the focus`);
      assert.equal(sheet(world).getAttribute('tabindex'), '-1');
      assert.equal(sheet(world).style.outline, 'none', 'and the dialog itself draws no focus ring');
      sheet(world).close();
    }
    for (const kind of ['project', 'import', 'batch']) {
      open(world, kind);
      assert.equal(world.document.activeElement, sheet(world), `${kind}: the dialog has the focus`);
      sheet(world).close();
    }
    open(world, 'session');                                                    // the repo picker: its filter is only there past 8 repos, the rows are never focused
    assert.equal(world.document.activeElement, sheet(world));
  }
});

test('the repo picker focuses its filter on a fine pointer and not on a phone', () => {
  const many = STATE();
  for (let i = 0; i < 10; i++) many.projects[0].repos.push({ name: `r${i}`, path: `/srv/projects/shop/r${i}`, state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] });
  for (const [coarse, want] of [[false, true], [true, false]]) {
    const w = fWorld({ coarse });
    w.ctx.__st = many; w.run('state = __st');
    open(w, 'session');
    const filter = sheet(w).querySelector('input');
    assert.ok(filter, 'a filter past 8 places');
    assert.equal(w.document.activeElement === filter, want, coarse ? 'phone' : 'desktop');
  }
});

test('the form sheet has a back chevron in its header (the way to the repo picker) and no Repos button in the body', () => {
  const w = fWorld();
  open(w, 'session');
  sheet(w).querySelectorAll('.pick-row')[1].click();
  assert.match(text(sheet(w).querySelector('.sheet-title')), /^New session · shop\/api$/, 'the title text is only the title');
  const back = sheet(w).querySelector('.sheet-head .sheet-back');
  assert.ok(back, 'a back button in the header');
  assert.equal(back.getAttribute('aria-label'), 'Back to the repo list');
  assert.equal(text(back).trim(), '', 'an icon, no text');
  assert.ok(!sheet(w).querySelector('.sheet-body').querySelectorAll('button').some((b) => text(b).trim() === 'Repos'), 'the old in-body button is gone');
  back.click();
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New session', 'back to the picker');
  assert.equal(sheet(w).querySelector('.sheet-back'), null, 'the picker has nothing above it');
});

// ---------------------------------------------------------------- the session form

test('the session form: labels above every control, Launch / Session name side by side, the footer is Start and Cancel only', () => {
  const w = fWorld();
  open(w, 'session', SHOP_API);
  const f = form(w);
  const labels = f.querySelectorAll('.field-label').map(text);
  for (const want of ['Launch', 'Session name', 'Model', 'Effort', 'Permissions', 'Allowed tools', 'Disallowed tools', 'Append to system prompt', 'Extra args']) assert.ok(labels.includes(want), `a "${want}" label`);
  assert.deepEqual(f.querySelector('.submit').querySelectorAll('button').map((b) => text(b).trim()), ['Start & open terminal', 'Cancel']);
  assert.ok(f.querySelector('.submit button.primary'));
  const model = fieldOf(f, /^Model$/);
  assert.equal(model.querySelector('label').getAttribute('for'), model.querySelector('select').getAttribute('id'), 'the Model label names the select');
  const resume = fieldOf(f, /^Session id to resume$/);
  assert.ok(resume.classList.contains('hidden'), 'the resume id field is hidden (label and all) until claude --resume is chosen');
  const launch = fieldOf(f, /^Launch$/).querySelector('select');
  launch.value = 'resume';
  launch.dispatchEvent({ type: 'change' });
  assert.ok(!resume.classList.contains('hidden'));
});

test('a failed launch says why: a message about the name lands under Session name, one about --resume under the resume id, anything else under the form; the sheet stays open and the banner is told', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/sessions': { __error: 'session s1 already exists' } } });
  open(w, 'session', SHOP_API);
  const f = form(w);
  submit(f);
  await tick();
  assert.equal(sheet(w).open, true, 'the form stays so nothing typed is lost');
  assert.match(text(fieldOf(f, /^Session name$/).querySelector('.field-err')), /already exists/, 'next to the field it is about');
  assert.equal(text(f.querySelector('.form-status')), '', 'and not twice');
  assert.equal(w.document.activeElement, fieldOf(f, /^Session name$/).querySelector('input'), 'the offending field has the focus');
  assert.deepEqual(plain(w.get('__errors')), ['session s1 already exists'], 'setError keeps the banner honest (the shell toasts it while a sheet is open)');
  w.ctx.__answers['/api/projects/shop/repos/api/sessions'] = { __error: 'claude is not installed on this box' };
  submit(f);
  await tick();
  assert.equal(text(fieldOf(f, /^Session name$/).querySelector('.field-err')), '', 'the old message is cleared on the next try');
  assert.equal(text(f.querySelector('.form-status')), 'claude is not installed on this box', 'a message about nothing in particular goes under the form');
  assert.ok(f.querySelector('.form-status').classList.contains('bad'));
  assert.equal(f.querySelector('.form-status').getAttribute('role'), 'alert', 'announced');
  w.ctx.__answers['/api/projects/shop/repos/api/sessions'] = { __error: 'resume id must be a UUID' };
  const launch = fieldOf(f, /^Launch$/).querySelector('select');
  launch.value = 'resume'; launch.dispatchEvent({ type: 'change' });
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Session id to resume$/).querySelector('.field-err')), /UUID/);
  w.ctx.__answers['/api/projects/shop/repos/api/sessions'] = { tmux: 'shop--api--s1' };
  submit(f);
  await tick();
  assert.equal(w.ctx.__tabs.at(-1).location, '/term/shop--api--s1', 'and a good try goes on to the terminal (the tab opened by the tap)');
});

// ---------------------------------------------------------------- the schedule form

test('the schedule form: every control has its label above it, the options are under Advanced, the footer is Schedule and Cancel', () => {
  const w = fWorld();
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  const labels = f.querySelectorAll('.field-label').map(text);
  assert.deepEqual(labels, ['Name', 'Prompt', 'Cron', 'Permission mode', 'Max turns', 'Max $', 'Extra args']);
  const adv = f.querySelector('details');
  assert.equal(text(adv.querySelector('summary')), 'Advanced');
  assert.ok(!adv.getAttribute('open'), 'collapsed');
  for (const re of [/^Permission mode$/, /^Max turns$/, /^Max \$$/, /^Extra args$/]) assert.ok(adv.contains(fieldOf(f, re)), `${re} sits under Advanced`);
  assert.ok(!adv.contains(fieldOf(f, /^Cron$/)), 'the cron is not advanced');
  assert.deepEqual(f.querySelector('.submit').querySelectorAll('button').map((b) => text(b).trim()), ['Schedule / run', 'Cancel']);
  const cron = fieldOf(f, /^Cron$/).querySelector('input');
  assert.match(cron.getAttribute('placeholder'), /cron/i, 'the placeholder still says what it is (other tests find the field by it)');
  assert.equal(text(f.querySelector('.tf-cronnote')), 'Runs once, right now.');
  assert.equal(w.document.activeElement, fieldOf(f, /^Name$/).querySelector('input'), 'the name has the focus on a desktop');
});

test('the schedule form says what is missing next to the field, posts nothing, and remembers cron, mode, turns and budget per repo', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/jobs': { id: 7 } } });
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Name$/).querySelector('.field-err')), /name/i);
  assert.equal(calls(w).length, 0, 'nothing posted without a name');
  fieldOf(f, /^Name$/).querySelector('input').value = 'nightly-audit';
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Prompt$/).querySelector('.field-err')), /prompt/i);
  assert.equal(calls(w).length, 0, 'nor without a prompt');
  fieldOf(f, /^Prompt$/).querySelector('textarea').value = 'audit the dependencies';
  f.querySelectorAll('button').find((b) => /nightly/i.test(text(b))).click();
  assert.equal(text(f.querySelector('.tf-cronnote')), 'Runs on cron 30 2 * * *.');
  assert.equal(text(f.querySelector('.submit button.primary')).trim(), 'Schedule');
  const pressed = f.querySelectorAll('.cron-presets button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => text(b).trim());
  assert.deepEqual(pressed, ['nightly 02:30'], 'the preset that matches is pressed');
  fieldOf(f, /^Permission mode$/).querySelector('select').value = 'plan';
  fieldOf(f, /^Max turns$/).querySelector('input').value = '12';
  fieldOf(f, /^Max \$$/).querySelector('input').value = '2.5';
  submit(f);
  await tick();
  const p = calls(w).filter((c) => c.method === 'POST');
  assert.equal(p.length, 1);
  assert.deepEqual(p[0].body, { name: 'nightly-audit', prompt: 'audit the dependencies', permission_mode: 'plan', max_turns: 12, run_now: false, cron: '30 2 * * *', max_budget_usd: 2.5 });
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:job:shop/api')), { cron: '30 2 * * *', mode: 'plan', turns: 12, budget: '2.5' });
  open(w, 'schedule', SHOP_API);
  const g = form(w);
  assert.equal(fieldOf(g, /^Cron$/).querySelector('input').getAttribute('value') || fieldOf(g, /^Cron$/).querySelector('input').value, '30 2 * * *', 'the next schedule for this repo starts from it');
  assert.equal(text(g.querySelector('.submit button.primary')).trim(), 'Schedule');
  assert.equal(fieldOf(g, /^Permission mode$/).querySelector('select').value, 'plan');
  assert.equal(fieldOf(g, /^Max turns$/).querySelector('input').getAttribute('value'), '12');
  open(w, 'schedule', { project: 'shop', repo: 'web' });
  assert.equal(fieldOf(form(w), /^Cron$/).querySelector('input').getAttribute('value') || '', '', 'another repo has its own');
});

test('a failed schedule: a message about the cron goes under Cron, the rest under the form', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/jobs': { __error: 'bad cron "x"' } } });
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  fieldOf(f, /^Name$/).querySelector('input').value = 'n';
  fieldOf(f, /^Prompt$/).querySelector('textarea').value = 'p';
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Cron$/).querySelector('.field-err')), /bad cron/);
  assert.equal(sheet(w).open, true);
  w.ctx.__answers['/api/projects/shop/repos/api/jobs'] = { __error: 'too many jobs' };
  submit(f);
  await tick();
  assert.equal(text(fieldOf(f, /^Cron$/).querySelector('.field-err')), '');
  assert.equal(text(f.querySelector('.form-status')), 'too many jobs');
});

// ---------------------------------------------------------------- add repo, project, import, batch

test('addRepoForm: two labelled fields with helper text, an empty submit says what to give, a failed clone lands under Clone URL', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos': { __error: 'clone failed: could not resolve host' } } });
  const f = w.run("addRepoForm(state.projects[0])");
  assert.deepEqual(f.querySelectorAll('.field-label').map(text), ['New repo', 'Clone URL']);
  assert.ok(f.querySelectorAll('.field-hint').length === 2, 'helper text under both');
  assert.deepEqual(f.querySelector('.submit').querySelectorAll('button').map((b) => text(b).trim()), ['Add repo', 'Cancel']);
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^New repo$/).querySelector('.field-err')), /name a new repo, or give a URL/i);
  assert.equal(calls(w).length, 0, 'the server would say the same: not asked');
  fieldOf(f, /^Clone URL$/).querySelector('input').value = 'https://nope.invalid/x.git';
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Clone URL$/).querySelector('.field-err')), /could not resolve host/);
});

test('the project form: a blank name is said next to the field instead of a browser bubble (novalidate), typing clears it', async () => {
  const w = fWorld();
  open(w, 'project');
  const f = form(w);
  assert.equal(f.getAttribute('novalidate'), '', 'our message, not the browser\'s');
  submit(f);
  await tick();
  const name = fieldOf(f, /^Name$/);
  assert.match(text(name.querySelector('.field-err')), /name the project/i);
  assert.equal(calls(w).length, 0);
  name.querySelector('input').dispatchEvent({ type: 'input' });
  assert.equal(text(name.querySelector('.field-err')), '');
  assert.match(text(fieldOf(f, /^Clone URL$/).querySelector('.field-hint')), /optional/i);
});

test('the footer of every sheet form is the primary and Cancel, nothing else (list tools sit in the list header)', () => {
  const w = fWorld();
  for (const [kind, ctx, want] of [['project', null, ['Create project', 'Cancel']], ['import', null, ['Import selected', 'Cancel']], ['batch', null, ['Run on selected repos', 'Cancel']],
    ['session', SHOP_API, ['Start & open terminal', 'Cancel']], ['task', SHOP_API, ['Start task', 'Cancel']], ['schedule', SHOP_API, ['Schedule / run', 'Cancel']]]) {
    open(w, kind, ctx);
    const foot = form(w).querySelector('.submit');
    assert.deepEqual(foot.querySelectorAll('button').map((b) => text(b).trim()), want, kind);
    assert.equal(foot.querySelectorAll('.bp5-intent-primary').length, 1, `${kind}: exactly one primary`);
    assert.equal(foot.children.length, want.length, `${kind}: nothing but the buttons in the footer (the keyboard hint moved under the prompt)`);
    sheet(w).close();
  }
});

test('the task form puts the keyboard hint under the prompt on a desktop and drops it on a phone; its Repo and When fields are labelled', () => {
  const desk = fWorld();
  open(desk, 'task', SHOP_API);
  const hint = fieldOf(form(desk), /^Prompt$/).querySelector('.field-hint');
  assert.match(text(hint), /Cmd\/Ctrl\+Enter submits/);
  const when = fieldOf(form(desk), /^When$/);
  assert.ok(when.querySelector('.seg-ctl'), 'the Now / Later / Schedule control is under its label');
  assert.equal(when.querySelector('.seg-ctl').getAttribute('aria-labelledby'), when.querySelector('label').getAttribute('id'));
  const phone = fWorld({ coarse: true });
  open(phone, 'task', SHOP_API);
  assert.equal(fieldOf(form(phone), /^Prompt$/).querySelector('.field-hint'), null, 'no Cmd+Enter hint on a touch keyboard');
});

test('Cancel stays a type=button with the exact text the sheet looks for, in every form', () => {
  const w = fWorld();
  for (const [kind, ctx] of [['project', null], ['import', null], ['batch', null], ['session', SHOP_API], ['task', SHOP_API], ['schedule', SHOP_API]]) {
    open(w, kind, ctx);
    const cancel = form(w).querySelector('.submit').querySelectorAll('button').find((b) => text(b) === 'Cancel');
    assert.ok(cancel && cancel.getAttribute('type') === 'button', kind);
    if (['session', 'schedule'].includes(kind)) continue;                       // their Cancel is closed by Shell.showForm's capturing click handler, which minidom (bubbling only) does not run
    cancel.click();
    assert.equal(sheet(w).open, false, `${kind}: Cancel closes the sheet`);
  }
});

// ---------------------------------------------------------------- batch and import details

test('the batch form remembers mode, turns and budget (one key: it spans repos) and says what is missing next to the prompt and the list', async () => {
  const w = fWorld({ answers: { '/api/batch': { batch_id: 'b9', jobs: [{ id: 1 }], started: [1] } } });
  open(w, 'batch');
  let f = form(w);
  const run = () => f.querySelector('.submit button.primary');
  assert.equal(w.document.activeElement, f.querySelector('textarea'), 'the prompt has the focus on a desktop');
  fieldOf(f, /^Permission mode$/).querySelector('select').value = 'plan';
  fieldOf(f, /^Max turns$/).querySelector('input').value = '9';
  fieldOf(f, /^Max \$ per repo$/).querySelector('input').value = '1.5';
  f.querySelector('textarea').value = 'update the changelog';
  f.querySelectorAll('.batch-repos input[type=checkbox]')[0].checked = true;
  run().click();
  await tick();
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:batch')), { mode: 'plan', turns: 9, budget: '1.5' });
  w.run('closeSheet()');
  open(w, 'batch');
  f = form(w);
  assert.equal(fieldOf(f, /^Permission mode$/).querySelector('select').value, 'plan');
  assert.equal(fieldOf(f, /^Max turns$/).querySelector('input').getAttribute('value'), '9');
  assert.equal(fieldOf(f, /^Max \$ per repo$/).querySelector('input').getAttribute('value'), '1.5');
  assert.ok(f.querySelector('details'), 'the knobs are under a disclosure: the prompt and the repos are the form');
  assert.ok(!f.querySelector('details').getAttribute('open'));
});

test('import: Enter in the live filter does not re-fetch the list from GitHub; Enter in the owner loads it', async () => {
  const w = fWorld({ answers: { '/api/github/repos': { protocol: 'https', repos: [{ name: 'alpha', url: 'u', private: false, fork: false, description: '' }] } } });
  open(w, 'import');
  const f = form(w);
  const [owner, , filter] = f.querySelectorAll('input[type=text]');
  let prevented = 0;
  filter.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() { prevented += 1; } });
  assert.equal(prevented, 1, 'the filter swallows Enter');
  assert.equal(calls(w).length, 0);
  assert.equal(w.document.activeElement, owner, 'the owner (the first field) has the focus');
  submit(f);                                                                   // Enter in a text field submits the form: load
  await tick();
  assert.equal(calls(w).length, 1);
  assert.equal(filter.getAttribute('aria-label'), 'Filter repos', 'the filter sits in the repo list block and names itself');
});

test('on a phone, tapping a Run mode or a cron preset does not raise the keyboard; on a desktop it moves on to the prompt', () => {
  const phone = fWorld({ coarse: true });
  open(phone, 'task', SHOP_API);
  const segBtn = form(phone).querySelectorAll('.seg-btn').find((b) => text(b) === 'Later');
  segBtn.dispatchEvent({ type: 'click', detail: 1, preventDefault() {} });
  assert.notEqual(phone.document.activeElement, form(phone).querySelector('textarea'));
  const desk = fWorld();
  open(desk, 'task', SHOP_API);
  desk.document.activeElement && desk.document.activeElement.blur();
  form(desk).querySelectorAll('.seg-btn').find((b) => text(b) === 'Later').dispatchEvent({ type: 'click', detail: 1, preventDefault() {} });
  assert.equal(desk.document.activeElement, form(desk).querySelector('textarea'));
});
