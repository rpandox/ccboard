// Contract tests for the v0.5.14a task form (launcher.js taskForm(p, r, opts)) as the create sheet shows it (Shell.openCreate('task', ctx)):
// one form for three ways to make a task. Run: Now (start it), Later (a Backlog card), Schedule (the job form's fields). The tests pin the request
// bodies, the submit label, what is remembered per repo, the keyboard (Enter adds a line, Cmd/Ctrl+Enter submits), the inline errors, the
// double-submit guard and the targets (the project folder when it is a git repo). The 10x pass is measured here too: from a project page,
// + task, type, Start task is three actions (see tasks.test.mjs for the click count over the whole page).
// Real core.js, components.js, launcher.js, router.js, widgets.js and shell.js on minidom's DOM; api(), toast(), poll() and navigate() are recorders.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const tick = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');

/** shop has a project folder that is NOT a git repo (state 'project', no branch), two repos; blog's folder IS a git repo; docs has only a git folder. */
const STATE = () => ({
  user: 'alice@example.com', config: { projects_dir: '/srv/projects', code_https_port: 10000 },
  projects: [
    { name: 'shop', path: '/srv/projects/shop', root: { name: 'root', path: '/srv/projects/shop', root: true, state: 'project', branch: null, dirty: null, sessions: [] }, orphan_sessions: [],
      repos: [{ name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] },
        { name: 'web', path: '/srv/projects/shop/web', state: 'ok', branch: 'dev', dirty: false, devcontainer: false, sessions: [] }] },
    { name: 'blog', path: '/srv/projects/blog', root: { name: 'root', path: '/srv/projects/blog', root: true, state: 'ok', branch: 'main', dirty: false, sessions: [] }, orphan_sessions: [],
      repos: [{ name: 'site', path: '/srv/projects/blog/site', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] }] },
    { name: 'docs', path: '/srv/projects/docs', root: { name: 'root', path: '/srv/projects/docs', root: true, state: 'ok', branch: 'main', dirty: false, sessions: [] }, orphan_sessions: [], repos: [] },
  ],
  clone_queue: { queued: [], done: [], cap: 3 }, pending_permissions: [], tasks: [], jobs: [], runs: [],
});

/**
 * minidom keeps select.value as a plain property; a browser answers '' when it is set to a value that is not one of the options. The remembered
 * choice that no longer exists (a saved bypassPermissions, a model that was dropped) relies on that, so the selects of this world behave the same.
 * Prefilled text inputs are read through val(): el() sets the value attribute, which a browser reflects into .value and minidom does not.
 */
function selectSemantics(w) {
  const make = w.document.createElement;
  w.document.createElement = (tag) => {
    const n = make(tag);
    if (String(tag).toLowerCase() === 'select') {
      let v = '';
      Object.defineProperty(n, 'value', { configurable: true, get: () => v, set: (x) => { v = n.children.some((o) => o.getAttribute('value') === String(x)) ? String(x) : ''; } });
    }
    return n;
  };
}
const val = (i) => i.value || i.getAttribute('value') || '';

/**
 * answers: api path prefix -> value | function(request) | { __error, status?, data? } (api() then rejects with an Error that carries .status / .data / .body).
 * `confirm` answers window.confirm; navigate() and poll() only record.
 */
function tWorld({ state = STATE(), answers = {}, confirm = () => true } = {}) {
  const w = makeWorld({ confirm });
  installDom(w);
  selectSemantics(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', 'shell.js']) w.load(f);
  w.ctx.__calls = []; w.ctx.__toasts = []; w.ctx.__nav = []; w.ctx.__answers = answers; w.ctx.__polls = 0; w.ctx.__st = state;
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      for (const [prefix, v] of Object.entries(__answers)) {
        if (!path.startsWith(prefix)) continue;
        const r = typeof v === 'function' ? v({ method, path, body }) : v;
        if (r && r.__error) { const e = new Error(r.__error); e.status = r.status; e.data = r.data; e.body = r.data; throw e; }
        return r;
      }
      return { ok: true };
    };
    poll = async () => { __polls++; };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    navigate = (hash, o) => { __nav.push(hash); };
    openPage = (url) => { __nav.push('page:' + url); };
    renderBanner = () => {};
    state = __st;
  `);
  return w;
}

const calls = (w) => plain(w.get('__calls'));
const posts = (w, prefix) => calls(w).filter((c) => c.method === 'POST' && c.path.startsWith(prefix));
const toasts = (w) => plain(w.get('__toasts'));
const navs = (w) => plain(w.get('__nav'));
const sheet = (w) => w.document.getElementById('sheet');
const title = (w) => text(sheet(w).querySelector('.sheet-title'));
const openTask = (w, ctx) => w.run(`Shell.openCreate('task', ${JSON.stringify(ctx)})`);
const form = (w) => sheet(w).querySelector('form.form');
const submit = (f) => f.dispatchEvent({ type: 'submit', preventDefault() {} });
const segs = (f) => f.querySelectorAll('.seg-btn');
const seg = (f, label) => segs(f).find((b) => text(b).trim() === label);
const pressed = (f) => segs(f).filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => text(b).trim());
const submitBtn = (f) => f.querySelector('button[type=submit]');
const label = (f) => text(submitBtn(f)).trim();
const titleInput = (f) => f.querySelectorAll('input').find((i) => /title/i.test(i.getAttribute('placeholder') || i.getAttribute('aria-label') || '')) || f.querySelectorAll('input').find((i) => i.getAttribute('maxlength') === '120');
const promptBox = (f) => f.querySelector('textarea');
const fieldOf = (f, re) => f.querySelectorAll('.field').find((x) => re.test(text(x.querySelector('span'))));
const selectOf = (f, re) => fieldOf(f, re).querySelector('select');
const status = (f) => f.querySelector('.form-status');
const has = (actual, expected, msg) => { for (const [k, v] of Object.entries(expected)) assert.deepStrictEqual(actual[k], v, `${msg || 'body'}.${k}`); };
const press = (n, key, mods = {}) => {
  const ev = { type: 'keydown', key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, isComposing: false, defaultPrevented: false, preventDefault() { ev.defaultPrevented = true; }, stopPropagation() {}, ...mods };
  n.dispatchEvent(ev);
  return ev;
};
/** minidom has no requestSubmit(): a form that calls it (Cmd+Enter) gets the same submit event a browser would fire. */
const shimSubmit = (f) => { f.requestSubmit = () => f.dispatchEvent({ type: 'submit', preventDefault() {} }); return f; };
const type = (f, t, p) => { titleInput(f).value = t; titleInput(f).dispatchEvent({ type: 'input' }); promptBox(f).value = p; promptBox(f).dispatchEvent({ type: 'input' }); };

const STARTED = { id: 31, slug: 'fix-login', tmux: 'shop--api--t-fix-login', branch: 'worktree-fix-login', attach_url: '/term/shop--api--t-fix-login' };
const LATER = { id: 32, slug: 'fix-login', phase: 'backlog', tmux: null };

// ---------------------------------------------------------------- the form

test('the task form: title, a prompt box, the Run control (Now | Later | Schedule, Now pressed), Options closed, submit says Start task', () => {
  const w = tWorld();
  assert.equal(openTask(w, { project: 'shop', repo: 'api' }), true);
  assert.equal(title(w), 'New task · shop/api');
  const f = form(w);
  assert.ok(titleInput(f), 'a title field');
  assert.ok(promptBox(f), 'a prompt box');
  assert.deepEqual(segs(f).map((b) => text(b).trim()), ['Now', 'Later', 'Schedule']);
  assert.deepEqual(pressed(f), ['Now'], 'exactly one is pressed, Now by default');
  assert.equal(label(f), 'Start task');
  const opts = f.querySelector('details');
  assert.ok(opts, 'the Claude options sit in a <details>');
  assert.ok(/options/i.test(text(opts.querySelector('summary'))), 'whose summary says Options');
  assert.ok(!opts.getAttribute('open') && !opts.open, 'collapsed: a task needs a title and a prompt, nothing else');
  for (const re of [/model/i, /effort/i, /permission/i]) assert.ok(opts.contains(fieldOf(f, re)), `${re} is under Options`);
});

test('the permission control never offers bypass for a task, and a remembered bypass is not posted', async () => {
  const w = tWorld();
  w.localStorage.setItem('ccboard:task:shop/api', JSON.stringify({ permission_mode: 'bypassPermissions', effort: 'high' }));
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  const perm = selectOf(f, /permission/i);
  assert.ok(!perm.querySelectorAll('option').some((o) => o.getAttribute('value') === 'bypassPermissions'), 'no bypass option');
  assert.equal(perm.value, '', 'a remembered choice that no longer exists falls back to the default');
  type(f, 'Fix login', 'Fix the redirect');
  submit(f);
  await tick();
  const body = posts(w, '/api/tasks')[0].body;
  assert.notEqual(body.permission_mode, 'bypassPermissions');
  assert.equal(body.effort, 'high', 'the rest of the remembered options still apply');
});

test('the submit label follows the Run control and the pressed state follows the taps', () => {
  const w = tWorld();
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  seg(f, 'Later').click();
  assert.deepEqual(pressed(f), ['Later']);
  assert.equal(label(form(w)), 'Add to backlog');
  seg(form(w), 'Schedule').click();
  assert.deepEqual(pressed(form(w)), ['Schedule']);
  assert.equal(label(form(w)), 'Schedule');
  seg(form(w), 'Now').click();
  assert.deepEqual(pressed(form(w)), ['Now']);
  assert.equal(label(form(w)), 'Start task');
  for (const b of segs(form(w))) assert.equal(b.getAttribute('type'), 'button', 'a Run button never submits the form');
});

// ---------------------------------------------------------------- Now

test('Now: POST /api/tasks {project, repo, title, prompt, when: "now"}, then a toast and the peek of the new session', async () => {
  const w = tWorld({ answers: { '/api/tasks': STARTED } });
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  type(f, '  Fix login  ', 'Fix the login redirect\nand add a test');
  submit(f);
  await tick();
  const p = posts(w, '/api/tasks');
  assert.equal(p.length, 1, 'one request');
  assert.equal(p[0].path, '/api/tasks', 'the new endpoint, not the per-repo one');
  has(p[0].body, { project: 'shop', repo: 'api', title: 'Fix login', prompt: 'Fix the login redirect\nand add a test', when: 'now' });
  assert.ok(p[0].body.agent === undefined || p[0].body.agent === 'claude', 'Claude is the only agent for now');
  assert.deepEqual(toasts(w).pop(), { text: 'started fix-login', kind: 'ok' });
  assert.deepEqual(navs(w), ['#/s/shop--api--t-fix-login'], 'the peek of the session it just made (the terminal is one tap from there)');
  assert.equal(sheet(w).open, false, 'the sheet is done');
  assert.ok(w.get('__polls') >= 1, 'the board refreshes behind the navigation');
});

test('Now with the options set posts them; blank options are left out', async () => {
  const w = tWorld({ answers: { '/api/tasks': STARTED } });
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  type(f, 'Fix login', 'go');
  submit(f);
  await tick();
  const plainBody = posts(w, '/api/tasks')[0].body;
  for (const k of ['model', 'effort', 'permission_mode', 'args']) assert.ok(!(k in plainBody), `${k} is not sent when it is blank`);
  openTask(w, { project: 'shop', repo: 'web' });
  const g = form(w);
  type(g, 'Fix web', 'go');
  selectOf(g, /model/i).value = 'opus';
  selectOf(g, /effort/i).value = 'high';
  selectOf(g, /permission/i).value = 'plan';
  submit(g);
  await tick();
  has(posts(w, '/api/tasks')[1].body, { project: 'shop', repo: 'web', model: 'opus', effort: 'high', permission_mode: 'plan', when: 'now' });
});

test('an empty prompt posts nothing and says so inline (the title is optional)', async () => {
  const w = tWorld({ answers: { '/api/tasks': STARTED } });
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  submit(f);
  await tick();
  assert.equal(posts(w, '/api/tasks').length, 0, 'nothing posted');
  assert.equal(sheet(w).open, true);
  type(f, 'Fix login', '   ');
  submit(f);
  await tick();
  assert.equal(posts(w, '/api/tasks').length, 0, 'a blank prompt is no prompt');
  assert.ok(status(f) && /what Claude should do|prompt/i.test(text(status(f))) && status(f).classList.contains('bad'), `inline: ${text(status(f))}`);
});

test('a task without a title takes the first line of the prompt, cut at a word near 80 characters', async () => {
  const w = tWorld({ answers: { '/api/tasks': STARTED } });
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  promptBox(f).value = '\n  Fix the login redirect   when the session cookie expires.\nSecond line is the detail';
  submit(f);
  await tick();
  assert.equal(posts(w, '/api/tasks')[0].body.title, 'Fix the login redirect when the session cookie expires', 'whitespace collapsed, trailing full stop dropped');
  openTask(w, { project: 'shop', repo: 'web' });
  const g = form(w);
  promptBox(g).value = 'word '.repeat(40);
  submit(g);
  await tick();
  const long = posts(w, '/api/tasks')[1].body.title;
  assert.ok(long.length <= 80 && !/\s$/.test(long) && /^(word ?)+$/.test(long), `a word boundary: ${long.length}`);
});

// ---------------------------------------------------------------- Later

test('Later: POST /api/tasks with when "later", a toast, the sheet closes and the user stays where they are', async () => {
  const w = tWorld({ answers: { '/api/tasks': LATER } });
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  seg(f, 'Later').click();
  type(form(w), 'Fix login', 'Fix the redirect');
  submit(form(w));
  await tick();
  const p = posts(w, '/api/tasks');
  assert.equal(p.length, 1);
  has(p[0].body, { project: 'shop', repo: 'api', title: 'Fix login', prompt: 'Fix the redirect', when: 'later' });
  assert.match(toasts(w).pop().text, /backlog/i, 'the toast says where it went');
  assert.deepEqual(navs(w), [], 'a task for later is not a reason to leave the page');
  assert.equal(sheet(w).open, false);
  assert.ok(w.get('__polls') >= 1);
});

// ---------------------------------------------------------------- Schedule

test('Schedule swaps the lower half to the job fields: the name comes from the title, the prompt is carried over, the presets fill the cron', async () => {
  const w = tWorld();
  openTask(w, { project: 'shop', repo: 'api' });
  type(form(w), 'Nightly audit', 'Audit the dependencies and open a PR for safe bumps');
  seg(form(w), 'Schedule').click();
  const f = form(w);
  assert.deepEqual(pressed(f), ['Schedule']);
  assert.equal(label(f), 'Schedule');
  const name = f.querySelectorAll('input').find((i) => /name/i.test(i.getAttribute('placeholder') || i.getAttribute('aria-label') || ''));
  const cron = f.querySelectorAll('input').find((i) => /cron/i.test(i.getAttribute('placeholder') || ''));
  assert.ok(name && cron, 'the job form: a name and a cron field');
  assert.equal(name.value, 'Nightly audit', 'the name is prefilled from the title');
  assert.equal(promptBox(f).value, 'Audit the dependencies and open a PR for safe bumps', 'the prompt is carried over');
  const chips = f.querySelectorAll('button').filter((b) => b.getAttribute('type') === 'button' && /nightly|weekdays|hourly|one-off/i.test(text(b)));
  assert.deepEqual(chips.map((b) => text(b).trim()), ['nightly 02:30', 'weekdays 09:00', 'hourly', 'one-off']);
  chips[0].click();
  assert.equal(cron.value, '30 2 * * *');
  chips[1].click();
  assert.equal(cron.value, '0 9 * * 1-5');
  chips[3].click();
  assert.equal(cron.value, '', 'one-off clears it: a blank cron runs once now');
  chips[0].click();
  submit(f);
  await tick();
  const p = posts(w, '/api/projects/shop/repos/api/jobs');
  assert.equal(p.length, 1, 'the job endpoint of the repo');
  has(p[0].body, { name: 'Nightly audit', prompt: 'Audit the dependencies and open a PR for safe bumps', cron: '30 2 * * *', run_now: false });
  assert.ok(p[0].body.permission_mode && p[0].body.max_turns, 'the job form\'s own defaults ride along');
  assert.equal(posts(w, '/api/tasks').length, 0, 'a schedule makes no task now');
  assert.match(toasts(w).pop().text, /schedul/i);
  assert.equal(sheet(w).open, false);
});

test('Schedule with the one-off preset runs once now: run_now true and no cron', async () => {
  const w = tWorld();
  openTask(w, { project: 'shop', repo: 'api' });
  type(form(w), 'Once', 'do it once');
  seg(form(w), 'Schedule').click();
  const f = form(w);
  f.querySelectorAll('button').find((b) => /one-off/i.test(text(b))).click();
  submit(f);
  await tick();
  const body = posts(w, '/api/projects/shop/repos/api/jobs')[0].body;
  has(body, { name: 'Once', prompt: 'do it once', run_now: true });
  assert.ok(!('cron' in body), 'no cron');
});

test('going back from Schedule to Now keeps what was typed', () => {
  const w = tWorld();
  openTask(w, { project: 'shop', repo: 'api' });
  type(form(w), 'Fix login', 'the prompt');
  seg(form(w), 'Schedule').click();
  seg(form(w), 'Now').click();
  const f = form(w);
  assert.equal(titleInput(f).value, 'Fix login');
  assert.equal(promptBox(f).value, 'the prompt');
  assert.equal(label(f), 'Start task');
});

// ---------------------------------------------------------------- the keyboard

test('Enter in the prompt adds a line and does not submit; Cmd+Enter and Ctrl+Enter submit', async () => {
  const w = tWorld({ answers: { '/api/tasks': STARTED } });
  openTask(w, { project: 'shop', repo: 'api' });
  const f = shimSubmit(form(w));
  type(f, 'Fix login', 'line one');
  const plainEnter = press(promptBox(f), 'Enter');
  await tick();
  assert.equal(posts(w, '/api/tasks').length, 0, 'Enter alone never starts a task');
  assert.ok(!plainEnter.defaultPrevented || /\n$/.test(promptBox(f).value), 'and still ends up adding a line (the browser default, or the form inserting it)');
  press(promptBox(f), 'Enter', { shiftKey: true });
  await tick();
  assert.equal(posts(w, '/api/tasks').length, 0, 'Shift+Enter neither');
  press(promptBox(f), 'Enter', { metaKey: true });
  await tick();
  assert.equal(posts(w, '/api/tasks').length, 1, 'Cmd+Enter submits');
  assert.equal(posts(w, '/api/tasks')[0].body.when, 'now');
  const w2 = tWorld({ answers: { '/api/tasks': LATER } });
  openTask(w2, { project: 'shop', repo: 'api' });
  const g = shimSubmit(form(w2));
  seg(g, 'Later').click();
  type(form(w2), 'Fix login', 'go');
  shimSubmit(form(w2));
  press(promptBox(form(w2)), 'Enter', { ctrlKey: true });
  await tick();
  assert.equal(posts(w2, '/api/tasks').length, 1, 'Ctrl+Enter submits');
  assert.equal(posts(w2, '/api/tasks')[0].body.when, 'later', 'in the mode that is selected');
});

test('Cmd+Enter with an empty prompt posts nothing', async () => {
  const w = tWorld({ answers: { '/api/tasks': STARTED } });
  openTask(w, { project: 'shop', repo: 'api' });
  const f = shimSubmit(form(w));
  titleInput(f).value = 'Fix login';
  press(promptBox(f), 'Enter', { metaKey: true });
  await tick();
  assert.equal(posts(w, '/api/tasks').length, 0);
});

test('the prompt box has the focus when the form opens: three actions from a project page are + task, type, Start task', () => {
  const w = tWorld();
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  const focus = w.document.activeElement;
  assert.ok(focus === titleInput(f) || focus === promptBox(f), 'a field of the form has the focus: typing needs no tap');
});

// ---------------------------------------------------------------- remembered per repo

test('the Run choice and the options are remembered per repo in ccboard:task:<project>/<repo>', async () => {
  const w = tWorld({ answers: { '/api/tasks': LATER } });
  openTask(w, { project: 'shop', repo: 'api' });
  seg(form(w), 'Later').click();
  const f = form(w);
  type(f, 'Fix login', 'go');
  selectOf(f, /model/i).value = 'sonnet';
  selectOf(f, /effort/i).value = 'high';
  selectOf(f, /permission/i).value = 'plan';
  submit(f);
  await tick();
  const saved = JSON.parse(w.localStorage.getItem('ccboard:task:shop/api'));
  has(saved, { when: 'later', effort: 'high', permission_mode: 'plan' }, 'saved');
  assert.ok(Object.values(saved).includes('sonnet'), 'the model is remembered too');
  openTask(w, { project: 'shop', repo: 'api' });
  const g = form(w);
  assert.deepEqual(pressed(g), ['Later'], 'the next task for this repo starts in Later');
  assert.equal(label(g), 'Add to backlog');
  assert.equal(selectOf(g, /effort/i).value, 'high');
  assert.equal(selectOf(g, /permission/i).value, 'plan');
  assert.equal(selectOf(g, /model/i).value, 'sonnet');
  openTask(w, { project: 'shop', repo: 'web' });
  assert.deepEqual(pressed(form(w)), ['Now'], 'another repo has its own defaults');
  assert.equal(selectOf(form(w), /effort/i).value, '');
  assert.equal(w.localStorage.getItem('ccboard:task:shop/web'), null, 'and nothing is written before a task is made there');
});

const cronInput = (f) => f.querySelectorAll('input').find((i) => /cron/i.test(i.getAttribute('placeholder') || ''));

test('a schedule is never the remembered mode: after a nightly schedule, + task opens in Now with a blank cron, and Cmd+Enter makes a task, not a recurring job', async () => {
  const w = tWorld({ answers: { '/api/projects/shop/repos/api/jobs': { id: 1 }, '/api/tasks': STARTED } });
  openTask(w, { project: 'shop', repo: 'api' });
  type(form(w), 'Nightly', 'audit');
  seg(form(w), 'Schedule').click();
  form(w).querySelectorAll('button').find((b) => /nightly/i.test(text(b))).click();
  submit(form(w));
  await tick();
  assert.equal(posts(w, '/api/projects/shop/repos/api/jobs').length, 1);
  const saved = JSON.parse(w.localStorage.getItem('ccboard:task:shop/api'));
  assert.notEqual(saved.when, 'schedule', 'what is remembered is now | later, never schedule');
  for (const again of [() => openTask(w, { project: 'shop', repo: 'api' }), () => w.run(`Shell.openCreate('task', { project: 'shop', repo: 'api' })`)]) {
    again();
    const f = form(w);
    assert.deepEqual(pressed(f), ['Now'], 'the next task opens in Now');
    assert.equal(label(f), 'Start task');
    assert.equal(val(cronInput(f)), '', 'no cron prefilled');
  }
  const f = shimSubmit(form(w));
  type(f, 'Fix login', 'go');
  press(promptBox(f), 'Enter', { metaKey: true });
  await tick();
  assert.equal(posts(w, '/api/tasks').length, 1, 'Cmd+Enter created a task');
  assert.equal(posts(w, '/api/projects/shop/repos/api/jobs').length, 1, 'and no second recurring job');
  openTask(w, { project: 'shop', repo: 'web' });
  assert.deepEqual(pressed(form(w)), ['Now'], 'another repo is unaffected');
});

test('a Later task before a schedule stays the remembered mode: the schedule leaves {when: later} alone', async () => {
  const w = tWorld({ answers: { '/api/tasks': LATER, '/api/projects/shop/repos/api/jobs': { id: 1 } } });
  openTask(w, { project: 'shop', repo: 'api' });
  seg(form(w), 'Later').click();
  type(form(w), 'Park it', 'later');
  submit(form(w));
  await tick();
  assert.equal(JSON.parse(w.localStorage.getItem('ccboard:task:shop/api')).when, 'later');
  openTask(w, { project: 'shop', repo: 'api' });
  seg(form(w), 'Schedule').click();
  type(form(w), 'Nightly', 'audit');
  form(w).querySelectorAll('button').find((b) => /nightly/i.test(text(b))).click();
  submit(form(w));
  await tick();
  assert.equal(posts(w, '/api/projects/shop/repos/api/jobs').length, 1);
  assert.equal(JSON.parse(w.localStorage.getItem('ccboard:task:shop/api')).when, 'later', 'the schedule did not overwrite it');
  openTask(w, { project: 'shop', repo: 'api' });
  assert.deepEqual(pressed(form(w)), ['Later'], 'the next task is a Later task again');
  assert.equal(val(cronInput(form(w))), '');
});

test('prefs saved by an earlier build with {when: schedule, cron} open in Now with a blank cron (the form fixes what is already in storage)', () => {
  const w = tWorld();
  w.localStorage.setItem('ccboard:task:shop/api', JSON.stringify({ when: 'schedule', cron: '30 2 * * *', effort: 'high' }));
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  assert.deepEqual(pressed(f), ['Now']);
  assert.equal(label(f), 'Start task');
  assert.equal(val(cronInput(f)), '', 'the stored cron is not shown in Now');
  assert.equal(selectOf(f, /effort/i).value, 'high', 'the rest of the options still apply');
  seg(f, 'Later').click();
  assert.equal(val(cronInput(f)), '', 'nor in Later');
  seg(f, 'Schedule').click();
  assert.equal(val(cronInput(f)), '', 'a schedule picked by hand starts as a one-off (blank cron: runs once, now), never as a recurring job nobody typed');
});

test('prefs saved before v0.5.14a (no "when") and unreadable storage both open on Now', () => {
  const w = tWorld();
  w.localStorage.setItem('ccboard:task:shop/api', JSON.stringify({ model_sel: 'opus', effort: 'high', permission_mode: '' }));
  openTask(w, { project: 'shop', repo: 'api' });
  assert.deepEqual(pressed(form(w)), ['Now']);
  assert.equal(selectOf(form(w), /effort/i).value, 'high', 'the old options still apply');
  w.localStorage.setItem('ccboard:task:shop/web', JSON.stringify({ when: 'sometime', effort: 'nonsense' }));
  openTask(w, { project: 'shop', repo: 'web' });
  assert.deepEqual(pressed(form(w)), ['Now'], 'an unknown mode is Now');
  w.localStorage.setItem('ccboard:task:blog/site', '{not json');
  openTask(w, { project: 'blog', repo: 'site' });
  assert.deepEqual(pressed(form(w)), ['Now'], 'a corrupt record is no record');
});

// ---------------------------------------------------------------- the place a task goes when the page names none

const at = (w, hash) => { w.location.hash = hash; w.run('currentRouteValue = null;'); };      // the router caches its parse of the address

test('after a task on shop/api, + task from Home (and the c t chord, and the + menu) opens that form at once, with the "in" select to switch', async () => {
  const w = tWorld({ answers: { '/api/tasks': LATER } });
  assert.equal(w.localStorage.getItem('ccboard:task:last'), null, 'nothing is remembered before the first task');
  at(w, '#/');
  openTask(w, null);
  assert.equal(title(w), 'New task', 'with no history it is still the repo picker');
  w.run('closeSheet()');
  openTask(w, { project: 'shop', repo: 'api' });
  seg(form(w), 'Later').click();
  type(form(w), 'Park it', 'later');
  submit(form(w));
  await tick();
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:task:last')), { project: 'shop', repo: 'api' }, 'the last place is kept across projects');
  w.run('closeSheet()');
  for (const hash of ['#/', '#/tasks']) {
    at(w, hash);
    assert.equal(openTask(w, null), true);
    assert.equal(title(w), 'New task · shop/api', `from ${hash} the form opens at once: no picker, 1 tap instead of 3`);
    const f = form(w);
    assert.ok(f && promptBox(f), 'the form, ready to type');
    const where = selectOf(f, /^in$/i);
    assert.ok(where, 'the "in" select is there to switch targets');
    assert.ok(where.querySelectorAll('option').length >= 3, 'shop/api, shop/web and blog/site are all offered');
    w.run('closeSheet()');
  }
  at(w, '#/');
  w.run(`Shell.createItems().find((i) => i.label === 'New task').onClick()`);                  // the top + menu entry
  assert.equal(title(w), 'New task · shop/api');
  w.run('closeSheet()');
  w.run(`Shell.openCreate('session')`);
  assert.equal(title(w), 'New session', 'sessions have no remembered place: they still ask');
});

test('the remembered place is only used while it still takes a task: a project that is gone, or a repo that is not there, gets the picker', async () => {
  const w = tWorld();
  at(w, '#/');
  for (const last of [{ project: 'ghost', repo: 'api' }, { project: 'shop', repo: 'gone' }, 'not json', { project: 'shop' }]) {
    w.localStorage.setItem('ccboard:task:last', typeof last === 'string' ? last : JSON.stringify(last));
    openTask(w, null);
    assert.equal(title(w), 'New task', `${JSON.stringify(last)} falls back to the picker`);
    w.run('closeSheet()');
  }
  w.localStorage.setItem('ccboard:task:last', JSON.stringify({ project: 'shop', repo: 'root' }));        // shop's folder is not a git repo: a task runs in place there
  openTask(w, null);
  assert.equal(title(w), 'New task · shop · project folder', 'the project folder is a valid place');
});

test('a project page keeps its own place: the route wins over the last task\'s place', async () => {
  const w = tWorld();
  w.localStorage.setItem('ccboard:task:last', JSON.stringify({ project: 'shop', repo: 'api' }));
  at(w, '#/p/blog');
  openTask(w, null);
  assert.equal(title(w), 'New task · blog/site');
});

test('a tap before the first /api/state toasts "still loading the board…", opens nothing and returns false', async () => {
  const w = tWorld();
  w.run('state = null');
  at(w, '#/');
  for (const kind of ['task', 'session', 'schedule']) assert.equal(w.run(`Shell.openCreate(${JSON.stringify(kind)})`), false, kind);
  assert.deepEqual(toasts(w).map((t) => t.text), ['still loading the board…', 'still loading the board…', 'still loading the board…']);
  assert.equal(sheet(w).open, false);
  assert.equal(w.run(`Shell.openCreate('nonsense')`), false);
  assert.equal(toasts(w).length, 3, 'an unknown kind is no tap on a button: no toast');
  w.run('state = __st');
  assert.equal(openTask(w, { project: 'shop', repo: 'api' }), true, 'with the state in place the same call opens the sheet');
});

// ---------------------------------------------------------------- errors and the double tap

test('a failed create stays open and says why inline, never only in the console; nothing navigates; the next try works', async () => {
  const answers = { '/api/tasks': { __error: 'codex arrives in v0.5.11', status: 400 } };
  const w = tWorld({ answers });
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  type(f, 'Fix login', 'go');
  submit(f);
  await tick();
  assert.equal(sheet(w).open, true, 'the form stays so nothing typed is lost');
  assert.equal(text(status(f)), 'codex arrives in v0.5.11');
  assert.ok(status(f).classList.contains('bad'));
  assert.deepEqual(navs(w), []);
  assert.equal(titleInput(f).value, 'Fix login');
  assert.equal(promptBox(f).value, 'go');
  w.ctx.__answers['/api/tasks'] = STARTED;
  submit(f);
  await tick();
  assert.equal(posts(w, '/api/tasks').length, 2, 'a second try is not blocked by the first failure');
  assert.deepEqual(navs(w), ['#/s/shop--api--t-fix-login']);
});

test('a failed Later and a failed Schedule are inline too', async () => {
  const w = tWorld({ answers: { '/api/tasks': { __error: 'title and prompt are required', status: 400 }, '/api/projects/shop/repos/api/jobs': { __error: 'bad cron "x"', status: 400 } } });
  openTask(w, { project: 'shop', repo: 'api' });
  seg(form(w), 'Later').click();
  type(form(w), 'T', 'P');
  submit(form(w));
  await tick();
  assert.equal(text(status(form(w))), 'title and prompt are required');
  seg(form(w), 'Schedule').click();
  submit(form(w));
  await tick();
  assert.match(text(status(form(w))) + text(sheet(w)), /bad cron/);
  assert.equal(sheet(w).open, true);
});

test('submitting twice before the first answer posts once (a double Cmd+Enter must not start two worktrees)', async () => {
  const w = tWorld({ answers: { '/api/tasks': () => STARTED } });
  openTask(w, { project: 'shop', repo: 'api' });
  const f = form(w);
  type(f, 'Fix login', 'go');
  submit(f);
  submit(f);
  await tick();
  assert.equal(posts(w, '/api/tasks').length, 1);
});

// ---------------------------------------------------------------- targets: the project folder

test('the project folder is a target for a task when it is a git repo: createFor opens the form for repo root', async () => {
  const w = tWorld({ answers: { '/api/tasks': { id: 40, slug: 'x', tmux: 'blog--root--t-x', branch: 'worktree-x', attach_url: '/term/blog--root--t-x' } } });
  assert.equal(w.run("Shell.createFor('task', { project: 'blog', repo: 'root' })"), true);
  assert.equal(title(w), 'New task · blog · project folder');
  const f = form(w);
  type(f, 'Tidy the blog', 'tidy');
  submit(f);
  await tick();
  has(posts(w, '/api/tasks')[0].body, { project: 'blog', repo: 'root', when: 'now' });
  assert.deepEqual(navs(w), ['#/s/blog--root--t-x']);
});

test('a project with only a git folder opens the task form for it without a picker; Schedule posts the job to repo root', async () => {
  const w = tWorld();
  assert.equal(openTask(w, { project: 'docs' }), true);
  assert.equal(title(w), 'New task · docs · project folder');
  type(form(w), 'Edit docs', 'edit');
  seg(form(w), 'Schedule').click();
  submit(form(w));
  await tick();
  assert.equal(posts(w, '/api/projects/docs/repos/root/jobs').length, 1);
});

test('a project folder that is not a git repo: a task runs there in place (createFor says yes, the picker offers it), a schedule still needs git', () => {
  const w = tWorld();
  assert.equal(w.run("Shell.createFor('task', { project: 'shop', repo: 'root' })"), true, 'a task may run in place in shop\'s folder');
  sheet(w).close();
  assert.equal(w.run("Shell.createFor('job', { project: 'shop', repo: 'root' })"), false, 'a schedule needs a worktree, so a git repo');
  openTask(w, { project: 'shop' });
  const rows = sheet(w).querySelectorAll('.pick-row');
  assert.deepEqual(rows.map((r) => text(r.querySelector('.pr-name'))), ['shop/api', 'shop/web', 'shop · project folder'], 'the folder is offered after the repos');
  assert.match(text(sheet(w)), /in place \(not a git repo\)/, 'and says the task runs in place');
  assert.doesNotMatch(text(sheet(w)), /tasks need git/);
  assert.equal(w.run("Shell.createFor('session', { project: 'shop', repo: 'root' })"), true, 'sessions still run in any folder');
});

test('the picker lists a git project folder for task and schedule, labelled as the project folder', async () => {
  const w = tWorld({ answers: { '/api/tasks': STARTED } });
  w.run("Shell.openCreate('task')");
  let rows = sheet(w).querySelectorAll('.pick-row');
  const names = rows.map((r) => text(r));
  assert.ok(names.some((n) => /blog/.test(n) && /project folder/.test(n)), `blog's folder is offered: ${names.join(' | ')}`);
  assert.ok(names.some((n) => /docs/.test(n) && /project folder/.test(n)), 'and docs\'');
  assert.ok(names.some((n) => /shop · project folder/.test(n) && /in place/.test(n)), 'shop\'s folder is offered too, marked in place (not a git repo)');
  const blog = rows.find((r) => /blog/.test(text(r)) && /project folder/.test(text(r)));
  blog.click();
  assert.equal(title(w), 'New task · blog · project folder');
  sheet(w).close();
  w.run("Shell.openCreate('schedule')");
  rows = sheet(w).querySelectorAll('.pick-row');
  assert.ok(rows.some((r) => /docs/.test(text(r)) && /project folder/.test(text(r))), 'a schedule can target a git project folder too');
  assert.ok(!rows.some((r) => /shop · project folder/.test(text(r))), 'but not a folder without git: a run needs a worktree');
  assert.equal(title(w), 'Schedule a run');
});

test('with a project in the context the picker is narrowed to it and still offers its git folder', () => {
  const w = tWorld();
  openTask(w, { project: 'blog' });
  const rows = sheet(w).querySelectorAll('.pick-row');
  assert.equal(rows.length, 2, 'the folder and the one repo');
  assert.ok(rows.every((r) => /blog/.test(text(r))));
});
