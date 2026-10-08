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
    for (const kind of ['import', 'batch']) {
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
  assert.deepEqual(labels, ['Agent', 'Model', 'Reasoning', 'Name', 'Prompt', 'Cron', 'Allowed tools', 'Permission mode', 'Max turns', 'Max $', 'Extra args'], 'v0.5.16: the agent picker leads, and Codex\'s model and reasoning follow it');
  assert.equal(f.querySelector('.lx-agentbox[data-agent=codex]').classList.contains('hidden'), true, 'Codex\'s options stay hidden while Claude is picked');
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

// ---------------------------------------------------------------- add repo, import, batch

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

test('the footer of every sheet form is the primary and Cancel, nothing else (list tools sit in the list header)', () => {
  const w = fWorld();
  for (const [kind, ctx, want] of [['import', null, ['Import selected', 'Cancel']], ['batch', null, ['Run on selected repos', 'Cancel']],
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
  for (const [kind, ctx] of [['import', null], ['batch', null], ['session', SHOP_API], ['task', SHOP_API], ['schedule', SHOP_API]]) {
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


// ---------------------------------------------------------------- v0.5.16: schedules and batches per agent

const CODEX_ON = { claude: { installed: true, loggedIn: true }, codex: { installed: true, loggedIn: true } };
const seg = (f, label) => f.querySelectorAll('.seg-ctl').find((g) => g.getAttribute('aria-label') === label);
const segBtn = (f, label, name) => seg(f, label).querySelectorAll('button').find((b) => text(b).trim() === name);
const segOn = (f, label) => seg(f, label).querySelectorAll('button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => text(b).trim());
const isOff = (n) => n.disabled || n.hasAttribute('disabled');
const hiddenIn = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };

test('the schedule form: Claude | Codex segmented; Codex is off with its reason when it is not installed, and a Claude body is exactly what it was', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/jobs': { id: 9 } } });
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  assert.deepEqual(segOn(f, 'Agent'), ['◆ Claude']);
  assert.equal(isOff(segBtn(f, 'Agent', '◇ Codex')), true, 'no codex on this box');
  assert.match(text(f.querySelector('.lx-why')), /Codex is not installed on this box/, 'said as text, not only as a title');
  segBtn(f, 'Agent', '◇ Codex').click();
  assert.deepEqual(segOn(f, 'Agent'), ['◆ Claude'], 'a disabled button does nothing');
  fieldOf(f, /^Name$/).querySelector('input').value = 'n';
  fieldOf(f, /^Prompt$/).querySelector('textarea').value = 'p';
  submit(f);
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.method === 'POST')[0].body, { name: 'n', prompt: 'p', permission_mode: 'acceptEdits', max_turns: 30, run_now: true }, 'no agent key for Claude: the server default stands');
});

test('the schedule form with Codex: model and reasoning, no turn or budget limit, the body names the agent, the sheet\'s prefs key remembers the choices', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/jobs': { id: 9 } } });
  w.ctx.__st.agents = CODEX_ON;
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  assert.equal(isOff(segBtn(f, 'Agent', '◇ Codex')), false);
  segBtn(f, 'Agent', '◇ Codex').click();
  assert.deepEqual(segOn(f, 'Agent'), ['◇ Codex']);
  const box = f.querySelector('.lx-agentbox[data-agent=codex]');
  assert.equal(hiddenIn(box), false, 'Codex\'s options show');
  assert.equal(hiddenIn(fieldOf(f, /^Max turns$/)), true, 'Codex has no turn limit');
  assert.equal(hiddenIn(fieldOf(f, /^Max \$$/)), true, 'nor a budget');
  assert.match(text(f.querySelector('.tf-lede')), /codex exec/);
  assert.match(fieldOf(f, /^Prompt$/).querySelector('textarea').getAttribute('placeholder'), /codex exec/);
  assert.match(fieldOf(f, /^Extra args$/).querySelector('input').getAttribute('placeholder'), /codex/);
  const model = fieldOf(f, /^Model$/).querySelector('select');
  assert.ok(model.querySelectorAll('option').map((o) => o.getAttribute('value')).includes('gpt-6-sol'), 'the models of the Codex schema');
  model.value = 'gpt-6-sol';
  model.dispatchEvent({ type: 'change' });
  segBtn(f, 'Reasoning', 'high').click();
  fieldOf(f, /^Name$/).querySelector('input').value = 'nightly-review';
  fieldOf(f, /^Prompt$/).querySelector('textarea').value = 'review main';
  fieldOf(f, /^Permission mode$/).querySelector('select').value = 'plan';
  f.querySelectorAll('button').find((b) => /nightly/i.test(text(b))).click();
  submit(f);
  await tick();
  const body = calls(w).filter((c) => c.method === 'POST')[0].body;
  assert.deepEqual(body, { name: 'nightly-review', prompt: 'review main', permission_mode: 'plan', run_now: false, cron: '30 2 * * *', agent: 'codex', model: 'gpt-6-sol', reasoning_effort: 'high' });
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:task:shop/api:codex')), { model: 'gpt-6-sol', reasoning_effort: 'high' }, 'the sheet\'s own key for Codex');
  assert.equal(JSON.parse(w.localStorage.getItem('ccboard:job:shop/api')).agent, 'codex');
  w.run('closeSheet()');
  open(w, 'schedule', SHOP_API);
  const g = form(w);
  assert.deepEqual(segOn(g, 'Agent'), ['◇ Codex'], 'the next schedule for this repo starts with Codex');
  assert.equal(fieldOf(g, /^Model$/).querySelector('select').value, 'gpt-6-sol');
  assert.deepEqual(segOn(g, 'Reasoning'), ['high']);
});

test('the schedule form with Codex: a refused argument lands under Extra args, a bad model under Model', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/jobs': { __error: 'argument not allowed for unattended runs: -c' } } });
  w.ctx.__st.agents = CODEX_ON;
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  segBtn(f, 'Agent', '◇ Codex').click();
  fieldOf(f, /^Name$/).querySelector('input').value = 'n';
  fieldOf(f, /^Prompt$/).querySelector('textarea').value = 'p';
  fieldOf(f, /^Extra args$/).querySelector('input').value = '-c x=1';
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Extra args$/).querySelector('.field-err')), /not allowed for unattended runs/);
  w.ctx.__answers['/api/projects/shop/repos/api/jobs'] = { __error: 'model: use a model slug such as gpt-6-sol' };
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Model$/).querySelector('.field-err')), /model slug/);
  assert.equal(text(fieldOf(f, /^Extra args$/).querySelector('.field-err')), '');
});

test('the batch form with Codex: every job runs with it, no turn or budget is sent, the choice is remembered', async () => {
  const w = fWorld({ answers: { '/api/batch': { batch_id: 'b9', jobs: [{ id: 1 }], started: [1] } } });
  w.ctx.__st.agents = CODEX_ON;
  open(w, 'batch');
  let f = form(w);
  assert.deepEqual(segOn(f, 'Agent'), ['◆ Claude']);
  segBtn(f, 'Agent', '◇ Codex').click();
  assert.match(text(f.querySelector('.dim')), /codex exec/);
  assert.equal(hiddenIn(fieldOf(f, /^Max turns$/)), true);
  assert.equal(hiddenIn(fieldOf(f, /^Max \$ per repo$/)), true);
  fieldOf(f, /^Model$/).querySelector('select').value = 'gpt-6-sol';
  fieldOf(f, /^Model$/).querySelector('select').dispatchEvent({ type: 'change' });
  f.querySelector('textarea').value = 'update the changelog';
  f.querySelectorAll('.batch-repos input[type=checkbox]')[0].checked = true;
  f.querySelector('.submit button.primary').click();
  await tick();
  const body = calls(w).filter((c) => c.method === 'POST' && c.path === '/api/batch')[0].body;
  assert.equal(body.agent, 'codex');
  assert.equal(body.model, 'gpt-6-sol');
  assert.ok(!('max_turns' in body) && !('max_budget_usd' in body), 'Codex has neither');
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:batch')), { mode: 'acceptEdits', turns: 30, budget: '', agent: 'codex', cx_model: 'gpt-6-sol', cx_reasoning: '' });
  w.run('closeSheet()');
  open(w, 'batch');
  f = form(w);
  assert.deepEqual(segOn(f, 'Agent'), ['◇ Codex']);
  assert.equal(fieldOf(f, /^Model$/).querySelector('select').value, 'gpt-6-sol');
});

// ---------------------------------------------------------------- #107 / #108: pre-approved tools and the Fable acknowledgement on the headless job forms

const fableRow = (f) => f.querySelector('.job-fable');
const shown = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return false; return true; };
const tickBox = (cb, on = true) => { cb.checked = on; cb.dispatchEvent({ type: 'change' }); };
const typeIn = (n, v) => { n.value = v; n.dispatchEvent({ type: 'input' }); };
const fillSchedule = (f, args) => {
  typeIn(fieldOf(f, /^Name$/).querySelector('input'), 'nightly-fable');
  typeIn(fieldOf(f, /^Prompt$/).querySelector('textarea'), 'audit it');
  if (args !== undefined) typeIn(fieldOf(f, /^Extra args$/).querySelector('input'), args);
};

test('Allowed tools sit on the schedule form for Claude only, with the dontAsk hint, and travel as allowed_tools', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/jobs': { id: 7 } } });
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  const tools = fieldOf(f, /^Allowed tools$/);
  assert.ok(shown(tools) && /dontAsk permission mode plus allowed tools is the safest unattended pair/.test(text(tools.querySelector('.field-hint'))));
  fillSchedule(f);
  typeIn(tools.querySelector('input'), 'Bash(git diff *), Read');
  submit(f);
  await tick();
  const body = calls(w).find((c) => c.method === 'POST').body;
  assert.equal(body.allowed_tools, 'Bash(git diff *), Read');
  assert.ok(!('acknowledge_fable' in body), 'no Fable, no acknowledgement');
});

test('a tool the server refuses is shown beside Allowed tools with the typed value kept', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/jobs': { __error: 'tool pattern not allowed: \'Bash(ls; rm)\'' } } });
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  fillSchedule(f);
  typeIn(fieldOf(f, /^Allowed tools$/).querySelector('input'), 'Bash(ls; rm)');
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Allowed tools$/).querySelector('.field-err')), /tool pattern not allowed/);
  assert.equal(fieldOf(f, /^Allowed tools$/).querySelector('input').value, 'Bash(ls; rm)');
  assert.equal(sheet(w).open, true);
});

test('a model that resolves to Fable shows the unticked acknowledgement above the button and makes Max $ required; other models and Codex do not', () => {
  const w = fWorld();
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  const row = fableRow(f);
  assert.equal(shown(row), false);
  const args = fieldOf(f, /^Extra args$/).querySelector('input');
  const budget = fieldOf(f, /^Max \$$/).querySelector('input');
  for (const [model, want] of [['--model fable', true], ['--model best', true], ['--model=claude-fable-5-1', true], ['--model FABLE[1m]', true], ['--model sonnet --fallback-model haiku,fable', true],
    ['--model opus', false], ['--model sonnet', false], ['--verbose', false], ['--model opus[1m]', false]]) {
    typeIn(args, model);
    assert.equal(shown(row), want, model);
    assert.equal(!!budget.required, want, `${model}: Max $ is required only for Fable`);
  }
  typeIn(args, '--model fable');
  assert.equal(text(row.querySelector('label')).trim(), 'This run bills Fable usage credits without asking');
  assert.equal(row.querySelector('input').checked, false, 'never pre-ticked');
  assert.match(text(fieldOf(f, /^Max \$$/).querySelector('.field-hint')), /^Required for Fable, at most \$25\./);
  assert.match(text(row), /client-side estimate that counts subagent spend: it is not a billing ceiling set by the provider/);
  assert.ok(!/protected|hard cap/i.test(text(row)), 'no claim of a hard cap');
  const foot = f.querySelector('.submit');
  const kids = [...f.children];
  assert.ok(kids.indexOf(row) < kids.indexOf(foot) && kids.indexOf(row) >= kids.indexOf(foot) - 2, 'directly above the buttons (the status line may sit between)');
  typeIn(args, '');
  assert.match(text(fieldOf(f, /^Max \$$/).querySelector('.field-hint')), /^Optional\.$/);
});

test('a Fable schedule posts nothing without the tick and the cap, says what to do beside each, then sends acknowledge_fable with Max $', async () => {
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/jobs': { id: 9 } } });
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  fillSchedule(f, '--model fable');
  submit(f);
  await tick();
  assert.equal(calls(w).length, 0);
  assert.match(text(fableRow(f).querySelector('.field-err')), /bills Fable usage credits without asking.*Tick the box/);
  tickBox(fableRow(f).querySelector('input'));
  submit(f);
  await tick();
  assert.equal(calls(w).length, 0, 'ticked but no cap');
  assert.match(text(fieldOf(f, /^Max \$$/).querySelector('.field-err')), /Set Max \$ for this Fable run \(at most \$25\)/);
  typeIn(fieldOf(f, /^Max \$$/).querySelector('input'), '30');
  submit(f);
  await tick();
  assert.equal(calls(w).length, 0, 'over the ceiling');
  typeIn(fieldOf(f, /^Max \$$/).querySelector('input'), '5');
  assert.equal(fableRow(f).querySelector('input').checked, false, 'editing the cap clears the acknowledgement');
  tickBox(fableRow(f).querySelector('input'));
  submit(f);
  await tick();
  const body = calls(w).find((c) => c.method === 'POST').body;
  assert.equal(body.acknowledge_fable, true);
  assert.equal(body.max_budget_usd, 5);
  assert.equal(body.args, '--model fable');
  const kept = JSON.parse(w.localStorage.getItem('ccboard:job:shop/api'));
  assert.ok(!JSON.stringify(kept).includes('fable') && !('acknowledge_fable' in kept), 'the acknowledgement is never remembered with the form');
});

test('editing the model text after the tick clears it; the 422 from the server lands beside the checkbox and keeps every typed value', async () => {
  const msg = 'This run bills Fable usage credits without asking (claude -p never asks first). Tick the box to acknowledge it for this job and set Max $ (at most $25); nothing was saved.';
  const w = fWorld({ answers: { '/api/projects/shop/repos/api/jobs': { __error: msg } } });
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  fillSchedule(f, '--model fable');
  typeIn(fieldOf(f, /^Max \$$/).querySelector('input'), '3');
  tickBox(fableRow(f).querySelector('input'));
  typeIn(fieldOf(f, /^Extra args$/).querySelector('input'), '--model best');
  assert.equal(fableRow(f).querySelector('input').checked, false, 'a new model text needs its own acknowledgement');
  tickBox(fableRow(f).querySelector('input'));
  submit(f);
  await tick();
  assert.match(text(fableRow(f).querySelector('.field-err')), /bills Fable usage credits without asking/);
  assert.equal(fieldOf(f, /^Name$/).querySelector('input').value, 'nightly-fable');
  assert.equal(fieldOf(f, /^Max \$$/).querySelector('input').value, '3');
  assert.equal(sheet(w).open, true, 'the form stays open');
});

test('a Codex schedule shows neither Allowed tools nor the Fable box', () => {
  const w = fWorld();
  w.ctx.__st.agents = { claude: { installed: true, loggedIn: true }, codex: { installed: true, loggedIn: true } };
  open(w, 'schedule', SHOP_API);
  const f = form(w);
  typeIn(fieldOf(f, /^Extra args$/).querySelector('input'), '--model fable');
  assert.equal(shown(fableRow(f)), true);
  const seg = f.querySelectorAll('.seg-ctl').find((n) => n.getAttribute('aria-label') === 'Agent');
  const codex = seg.querySelectorAll('button').find((b) => /Codex/.test(text(b)));
  assert.ok(!codex.hasAttribute('disabled'), 'Codex is installed in this world');
  codex.click();
  assert.equal(shown(fableRow(f)), false);
  assert.equal(shown(fieldOf(f, /^Allowed tools$/)), false);
});

test('the batch form takes Extra args, Allowed tools and the same Fable acknowledgement, one per submission', async () => {
  const w = fWorld({ answers: { '/api/batch': { batch_id: 'b1', jobs: [1, 2], started: [] } } });
  const f = w.run('batchForm({})');
  const labels = f.querySelectorAll('.field-label').map(text);
  assert.ok(labels.includes('Allowed tools') && labels.includes('Extra args'), labels.join(','));
  const boxes = f.querySelectorAll('input').filter((i) => i.getAttribute('type') === 'checkbox' && !i.closest('.job-fable'));
  boxes[0].checked = true;
  typeIn(f.querySelector('textarea'), 'update deps');
  typeIn(fieldOf(f, /^Extra args$/).querySelector('input'), '--model best');
  assert.equal(shown(fableRow(f)), true);
  const go = f.querySelectorAll('button').find((b) => /Run on selected repos/.test(text(b)));
  go.click();
  await tick();
  assert.equal(calls(w).length, 0, 'nothing is posted without the tick');
  tickBox(fableRow(f).querySelector('input'));
  typeIn(fieldOf(f, /^Max \$ per repo$/).querySelector('input'), '2');
  tickBox(fableRow(f).querySelector('input'));
  typeIn(fieldOf(f, /^Allowed tools$/).querySelector('input'), 'Read');
  go.click();
  await tick();
  const body = calls(w).find((c) => c.method === 'POST').body;
  assert.deepEqual([body.args, body.max_budget_usd, body.acknowledge_fable, body.allowed_tools], ['--model best', 2, true, 'Read']);
});

test('the schedule row says a Fable job is held or acknowledged, with its cap, and a held one has an Acknowledge button that posts the edit', async () => {
  const w = fWorld({ answers: { '/api/jobs/5/acknowledge-fable': { id: 5 } } });
  const held = { id: 5, name: 'old fable job', max_turns: 30, max_budget_usd: null, agent: 'claude', fable: { state: 'held', reason: 'held: ...', max: 25 } };
  const ok = { id: 6, name: 'new', max_turns: 30, max_budget_usd: 4, agent: 'claude', fable: { state: 'acknowledged', cap: 4, at: '2026-10-08T00:00:00+00:00', max: 25 } };
  const plainJob = { id: 7, name: 'plain', max_turns: 30, max_budget_usd: 2, agent: 'claude', fable: null };
  assert.equal(w.run(`jobLimitText(${JSON.stringify(held)})`), ' · ≤30 turns · held: waiting for a Fable acknowledgement and a Max $');
  assert.equal(w.run(`jobLimitText(${JSON.stringify(ok)})`), ' · ≤30 turns · ≤$4 · Fable acknowledged (≤$4)');
  assert.equal(w.run(`jobLimitText(${JSON.stringify(plainJob)})`), ' · ≤30 turns · ≤$2');
  assert.equal(w.run(`jobFableButton(${JSON.stringify(ok)})`), null);
  assert.equal(w.run(`jobFableButton(${JSON.stringify(plainJob)})`), null);
  const b = w.run(`jobFableButton(${JSON.stringify(held)})`);
  assert.equal(text(b), 'Acknowledge Fable');
  b.click();
  const f = form(w);
  assert.match(text(f), /old fable job uses Fable.*never asks before billing/);
  assert.match(text(fieldOf(f, /^Max \$$/).querySelector('.field-hint')), /at most \$25.*not a billing ceiling set by the provider/);
  submit(f);
  await tick();
  assert.equal(calls(w).length, 0);
  assert.match(text(f.querySelector('.job-fable .field-err')), /Tick the box/);
  tickBox(f.querySelector('.job-fable input'));
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Max \$$/).querySelector('.field-err')), /Set Max \$/);
  typeIn(fieldOf(f, /^Max \$$/).querySelector('input'), '4');
  tickBox(f.querySelector('.job-fable input'));
  submit(f);
  await tick();
  const post = calls(w).find((c) => c.method === 'POST');
  assert.equal(post.path, '/api/jobs/5/acknowledge-fable');
  assert.deepEqual(post.body, { acknowledge_fable: true, max_budget_usd: 4 });
});
