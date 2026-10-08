// v0.5.20, work from GitHub issues: launcherIssueChoices (the pure part), the launcher sheet's "from a GitHub issue" (preselect after the schema check, the quiet note, Reset,
// the untrusted callout and its tick, the issue link on the request) and the task card's "#N" link and two-tap "Comment on the issue".
// Real core.js, components.js, launcher.js, router.js, widgets.js, pages/agents.js and shell.js on minidom's DOM; api(), toast() and poll() are recorders.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const tick = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');

const STATE = () => ({
  user: 'alice@example.com', config: { projects_dir: '/srv/projects', code_https_port: 10000 },
  agents: { claude: { installed: true, loggedIn: true }, codex: { installed: true, loggedIn: true } },
  projects: [
    { name: 'shop', path: '/srv/projects/shop', root: { name: 'root', path: '/srv/projects/shop', root: true, state: 'project', branch: null, dirty: null, sessions: [] }, orphan_sessions: [],
      repos: [{ name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] }] },
  ],
  clone_queue: { queued: [], done: [], cap: 3 }, pending_permissions: [], tasks: [], jobs: [], runs: [],
});

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

const LIST = [
  { number: 47, title: 'Dispatch from issues', body: 'Make it so.', url: 'https://github.com/acme/board/issues/47', labels: [], author: 'acme', trusted: true },
  { number: 48, title: 'Run this please', body: 'Do it.', url: 'https://github.com/acme/board/issues/48', labels: [], author: 'mallory', trusted: false },
  { number: 49, title: 'No model line', body: 'Plain.', url: 'https://github.com/acme/board/issues/49', labels: [], author: 'acme', trusted: true },
];
const WHO = (claude, codex, extra = {}) => ({ claude: claude || null, codex: codex || null, default_agent: claude ? 'claude' : codex ? 'codex' : null, warnings: [], ...extra });
const DETAIL = {
  47: { ...LIST[0], who: WHO({ model: 'opus', effort: 'high', permission_mode: 'acceptEdits' }, { model: 'gpt-6.1-sol', reasoning: 'low', sandbox: 'workspace-write', approval: 'on-request' }) },
  48: { ...LIST[1], who: WHO() },
  49: { ...LIST[2], who: WHO() },
  50: { number: 50, title: 'Odd', body: '', url: 'https://github.com/acme/board/issues/50', labels: [], author: 'acme', trusted: true,
    who: WHO({ model: 'claude-future-9', effort: 'turbo', permission_mode: 'bypassPermissions' }, null, { warnings: ['ignored a bypass setting'] }) },
  51: { number: 51, title: 'Codex only', body: '', url: 'https://github.com/acme/board/issues/51', labels: [], author: 'acme', trusted: true,
    who: WHO(null, { model: 'gpt-6-luna', reasoning: 'high', sandbox: 'read-only', approval: 'on-request' }) },
};

function lWorld({ state = STATE(), answers = {} } = {}) {
  const w = makeWorld({});
  installDom(w);
  selectSemantics(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', 'pages/agents.js', 'shell.js']) w.load(f);
  const issueAnswers = {
    '/api/projects/shop/repos/api/issues': ({ path }) => {
      const m = /issues\/(\d+)$/.exec(path);
      if (!m) return { issues: [...LIST, { ...DETAIL[50], who: undefined }, { ...DETAIL[51], who: undefined }] };
      if (!DETAIL[m[1]]) return { __error: 'gh: could not resolve to an issue', status: 422 };
      return DETAIL[m[1]];
    },
  };
  Object.assign(w.ctx, { __calls: [], __toasts: [], __errors: [], __nav: [], __polls: 0, __answers: { ...answers, ...issueAnswers, ...answers }, __st: state });
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      for (const [prefix, v] of Object.entries(__answers)) {
        if (!path.startsWith(prefix)) continue;
        const r = typeof v === 'function' ? v({ method, path, body }) : v;
        if (r && r.__error) { const e = new Error(r.__error); e.status = r.status; e.body = r.data; throw e; }
        return r;
      }
      return { ok: true };
    };
    poll = async () => { __polls++; };
    toast = (t, o) => { __toasts.push({ text: t, kind: o && o.kind }); };
    setError = (m) => { __errors.push(m); };
    openPage = (u) => { __nav.push(u); };
    renderBanner = () => {};
    state = __st;
  `);
  return w;
}

const calls = (w) => plain(w.get('__calls'));
const posts = (w, re) => calls(w).filter((c) => c.method === 'POST' && re.test(c.path));
const sheet = (w) => w.document.getElementById('sheet');
const form = (w) => sheet(w).querySelector('form.form');
const submit = (f) => f.dispatchEvent({ type: 'submit', preventDefault() {} });
const openTask = (w) => w.run(`openLauncher(${JSON.stringify({ project: 'shop', repo: 'api', mode: 'task' })})`);
const group = (f, label) => f.querySelectorAll('.seg-ctl').find((n) => n.getAttribute('aria-label') === label);
const pressed = (f, label) => group(f, label).querySelectorAll('button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => text(b).trim());
const btn = (f, label, name) => group(f, label).querySelectorAll('button').find((b) => text(b).trim() === name);
const start = (f) => f.querySelector('button[type=submit]');
const off = (n) => n.disabled || n.hasAttribute('disabled');
const hidden = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const issueSel = (f) => f.querySelector('.lx-issue select');
const note = (f) => text(f.querySelector('.lx-issue-note-t'));
const reset = (f) => f.querySelector('.lx-issue-reset');
const callout = (f) => f.querySelector('.lx-issue-warn');
const permSel = (f) => f.querySelectorAll('select').find((s) => s.children.some((o) => o.getAttribute('value') === 'acceptEdits'));
const pickIssue = async (w, f, n) => {
  const sel = issueSel(f);
  sel.dispatchEvent({ type: 'focus' });
  await tick();
  sel.value = String(n);
  sel.dispatchEvent({ type: 'change' });
  await tick(); await tick();
};
const store = (w, key) => { const v = w.localStorage.getItem(key); return v === null ? null : JSON.parse(v); };

// ---------------------------------------------------------------- the pure part

test('launcherIssueChoices checks every value against the installed schema and never lets a bypass through', () => {
  const w = lWorld();
  const run = (who, o) => plain(w.run(`launcherIssueChoices(${JSON.stringify(who)}, ${JSON.stringify(o || {})})`));
  const full = run(DETAIL[47].who);
  assert.equal(full.agent, 'claude');
  assert.deepEqual(full.claude, { model: 'opus', effort: 'high', permission_mode: 'acceptEdits' });
  assert.deepEqual(full.parts.claude, ['opus', 'high', 'acceptEdits']);
  assert.deepEqual(full.codex, { model: 'gpt-6.1-sol', reasoning: 'low', cx_mode: 'default' });
  const odd = run(DETAIL[50].who);
  assert.equal(odd.agent, '', 'an unknown model, an unknown effort and a bypass leave nothing to preselect');
  assert.deepEqual(odd.skipped, ['model claude-future-9', 'effort turbo', 'permissions bypassPermissions']);
  assert.equal(run(DETAIL[51].who).agent, 'codex');
  assert.equal(run(DETAIL[51].who).codex.cx_mode, 'read-only');
  assert.equal(run(DETAIL[47].who, { agents: ['codex'] }).agent, 'codex', 'only the agents the form offers');
  assert.equal(run(DETAIL[47].who, { agents: ['claude', 'codex'], installed: undefined }).agent, 'claude');
  const dangerous = run({ claude: null, codex: { model: 'gpt-6.1-sol', sandbox: 'danger-full-access', approval: 'never' }, default_agent: 'codex' });
  assert.equal(dangerous.codex.cx_mode, undefined, 'danger-full-access is never a mode');
  assert.ok(dangerous.skipped.length);
  assert.equal(run(null).agent, '');
});

// ---------------------------------------------------------------- the sheet

test('picking an issue preselects agent, model, effort and mode from its block, keeps Closes #N, shows the note and never starts a task', async () => {
  const w = lWorld();
  openTask(w);
  const f = form(w);
  await pickIssue(w, f, 47);
  assert.deepEqual(pressed(f, 'Agent'), ['◆ Claude']);
  assert.deepEqual(pressed(f, 'Model'), ['opus']);
  assert.deepEqual(pressed(f, 'Effort'), ['high']);
  assert.equal(permSel(f).value, 'acceptEdits');
  assert.equal(note(f), 'from the issue: opus, high, acceptEdits');
  assert.ok(!hidden(reset(f)), 'Reset to my defaults is there');
  const prompt = f.querySelector('textarea.lx-prompt').value;
  assert.match(prompt, /Dispatch from issues/);
  assert.match(prompt, /Closes #47/);
  assert.equal(posts(w, /\/api\/tasks/).length, 0, 'picking starts nothing');
  assert.ok(calls(w).some((c) => c.method === 'GET' && c.path === '/api/projects/shop/repos/api/issues/47'));
  assert.match(text(f.querySelector('.lx-issue-meta')), /#47 by acme/);
  assert.equal(f.querySelector('.lx-issue-meta a').getAttribute('href'), 'https://github.com/acme/board/issues/47');
  assert.ok(hidden(callout(f)), 'the owner is not asked to tick');
  assert.ok(!off(start(f)));
});

test('Start sends the issue link, and the issue model line is not remembered as the person default', async () => {
  const w = lWorld({ answers: { '/api/tasks': { id: 31, slug: 'dispatch', tmux: 'shop--api--t-dispatch', branch: 'worktree-dispatch' } } });
  w.localStorage.setItem('ccboard:task:shop/api', JSON.stringify({ model: 'haiku', effort: 'low', when: 'now' }));
  openTask(w);
  const f = form(w);
  await pickIssue(w, f, 47);
  assert.deepEqual(pressed(f, 'Model'), ['opus']);
  submit(f);
  await tick(); await tick();
  const body = posts(w, /\/api\/tasks$/)[0].body;
  assert.equal(body.issue_number, 47);
  assert.equal(body.issue_url, 'https://github.com/acme/board/issues/47');
  assert.equal(body.model, 'opus');
  assert.equal(body.effort, 'high');
  assert.equal(body.permission_mode, 'acceptEdits');
  const kept = store(w, 'ccboard:task:shop/api');
  assert.equal(kept.model, 'haiku', 'the dated or per-issue model never becomes the remembered default');
  assert.equal(kept.effort, 'low');
});

test('Reset to my defaults restores what the person had, and keeps the prompt and the link', async () => {
  const w = lWorld();
  w.localStorage.setItem('ccboard:task:shop/api', JSON.stringify({ model: 'haiku', effort: 'low' }));
  openTask(w);
  const f = form(w);
  assert.deepEqual(pressed(f, 'Model'), ['haiku']);
  await pickIssue(w, f, 47);
  assert.deepEqual(pressed(f, 'Model'), ['opus']);
  reset(f).click();
  assert.deepEqual(pressed(f, 'Model'), ['haiku']);
  assert.deepEqual(pressed(f, 'Effort'), ['low']);
  assert.equal(permSel(f).value, '');
  assert.match(f.querySelector('textarea.lx-prompt').value, /Closes #47/);
  assert.ok(hidden(reset(f)));
});

test('an issue by someone who is not the owner: callout, a tick before Start, nothing preselected', async () => {
  const w = lWorld({ answers: { '/api/tasks': { id: 33, slug: 'run', tmux: 'x', branch: 'b' } } });
  openTask(w);
  const f = form(w);
  await pickIssue(w, f, 48);
  assert.ok(!hidden(callout(f)));
  assert.match(text(callout(f)), /Issue by mallory: its text becomes the agent's prompt/);
  assert.deepEqual(pressed(f, 'Model'), ['opus'], 'the remembered default, not the issue block');
  assert.deepEqual(pressed(f, 'Effort'), ['high']);
  assert.ok(!calls(w).some((c) => /issues\/48$/.test(c.path)), 'nothing to read from an untrusted block');
  assert.ok(off(start(f)), 'Start waits for the tick');
  submit(f);
  await tick();
  assert.equal(posts(w, /\/api\/tasks$/).length, 0);
  const tickBox = callout(f).querySelector('input[type=checkbox]');
  tickBox.checked = true;
  tickBox.dispatchEvent({ type: 'change' });
  assert.ok(!off(start(f)));
  submit(f);
  await tick(); await tick();
  assert.equal(posts(w, /\/api\/tasks$/).length, 1);
  assert.equal(posts(w, /\/api\/tasks$/)[0].body.issue_number, 48);
});

test('a value the installed agent does not list is skipped and said; a bypass in the block shows the warning', async () => {
  const w = lWorld();
  openTask(w);
  const f = form(w);
  await pickIssue(w, f, 50);
  assert.deepEqual(pressed(f, 'Model'), ['opus']);
  assert.match(note(f), /not offered here: model claude-future-9, effort turbo, permissions bypassPermissions/);
  assert.match(note(f), /ignored a bypass setting/);
  assert.ok(hidden(reset(f)), 'nothing was preselected, so nothing to reset');
});

test('a Codex-only block switches to Codex (one tap back), and an issue without a block keeps the defaults', async () => {
  const w = lWorld();
  openTask(w);
  const f = form(w);
  await pickIssue(w, f, 51);
  assert.deepEqual(pressed(f, 'Agent'), ['◇ Codex']);
  assert.match(note(f), /from the issue: gpt-6-luna, high, read-only/);
  btn(f, 'Agent', '◆ Claude').click();
  assert.deepEqual(pressed(f, 'Agent'), ['◆ Claude']);
  await pickIssue(w, f, 49);
  assert.match(note(f), /no model line/);
  assert.deepEqual(pressed(f, 'Agent'), ['◆ Claude']);
});

test('when gh cannot read the issue the next step is said, the prompt stays and the rest works', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/issues/47': { __error: 'gh: not logged in, run gh auth login', status: 422 } } });
  openTask(w);
  const f = form(w);
  await pickIssue(w, f, 47);
  assert.match(note(f), /Could not read the issue's model line/);
  assert.match(note(f), /gh auth login/);
  assert.match(f.querySelector('textarea.lx-prompt').value, /Closes #47/);
  assert.ok(!off(start(f)));
});

// ---------------------------------------------------------------- the card

const DONE = (over = {}) => ({ id: 7, project: 'shop', repo: 'api', slug: 'fix', title: 'Fix it', branch: 'worktree-fix', base: 'main', worktree: '/w', tmux: 'shop--api--t-fix', agent: 'claude', mode: 'worktree',
  phase: 'done', column: 'done', auto_close: false, session: null, result: 'All fixed.', done_at: '2026-10-01T00:00:00+00:00', result_at: '2026-10-01T00:00:00+00:00', pr_url: null, pr_number: null, pr_state: null,
  cost_usd: null, overlap: [], ci: null, pr: null, created_at: '2026-10-01T00:00:00+00:00', issue_number: 47, issue_url: 'https://github.com/acme/board/issues/47', issue_commented_at: null, ...over });

test('the task card links #N to the issue (https only)', () => {
  const w = lWorld();
  const a = w.run(`startedCard(${JSON.stringify(DONE())}, {})`);
  const link = a.querySelector('a.tk-issue-link');
  assert.equal(text(link), '#47');
  assert.equal(link.getAttribute('href'), 'https://github.com/acme/board/issues/47');
  const b = w.run(`startedCard(${JSON.stringify(DONE({ issue_url: 'javascript:alert(1)' }))}, {})`);
  assert.equal(b.querySelector('a.tk-issue-link'), null, 'a link that is not https is plain text');
  assert.equal(text(b.querySelector('.tk-issue-link')), '#47');
  const c = w.run(`startedCard(${JSON.stringify(DONE({ issue_number: null, issue_url: null }))}, {})`);
  assert.equal(c.querySelector('.tk-issue-link'), null);
});

test('Comment on the issue: only when done with a result, two taps, the body shown while armed, one POST, then Posted', async () => {
  const w = lWorld({ answers: { '/api/tasks/7/issue-comment': ({ method }) => (method === 'GET' ? { body: 'ccboard task finished: Fix it\n\nAll fixed.\n\nPosted from ccboard' } : { ok: true }) } });
  const row = (t) => w.run(`taskIssueNode(${JSON.stringify(t)})`);
  assert.equal(row(DONE({ result: '' })), null, 'no result, no comment');
  assert.equal(row(DONE({ phase: 'running', column: 'working' })), null, 'not done');
  assert.equal(row(DONE({ issue_number: null })), null);
  assert.match(text(row(DONE({ issue_commented_at: '2026-10-02T00:00:00+00:00' }))), /Posted on #47/);
  const node = row(DONE());
  const first = node.querySelector('button');
  assert.equal(text(first).trim(), 'Comment on the issue');
  first.click();
  await tick();
  assert.equal(posts(w, /issue-comment/).length, 0, 'the first tap posts nothing');
  w.run('ui.confirm = "issue:7"');
  const armed = row(DONE());
  assert.match(text(armed.querySelector('pre.tk-issue-body')), /Posted from ccboard/);
  const go = armed.querySelectorAll('button').find((b) => /^Confirm/.test(text(b)));
  assert.ok(go, 'the second tap is a Confirm button');
  go.click();
  await tick(); await tick();
  assert.equal(posts(w, /\/api\/tasks\/7\/issue-comment$/).length, 1);
  assert.match(text(row(DONE())), /Posted on #47/);
  assert.ok(plain(w.get('__toasts')).some((t) => /posted on #47/.test(t.text)));
});

test('a failed comment shows its error and keeps the button', async () => {
  const w = lWorld({ answers: { '/api/tasks/7/issue-comment': ({ method }) => (method === 'GET' ? { body: 'b' } : { __error: 'HTTP 403: nope', status: 422 }) } });
  w.run('ui.confirm = "issue:7"');
  const armed = w.run(`taskIssueNode(${JSON.stringify(DONE())})`);
  armed.querySelectorAll('button').find((b) => /^Confirm/.test(text(b))).click();
  await tick(); await tick();
  assert.deepEqual(plain(w.get('__errors')).filter(Boolean), ['HTTP 403: nope']);
  const again = w.run(`taskIssueNode(${JSON.stringify(DONE())})`);
  assert.equal(text(again.querySelector('button')).trim(), 'Comment on the issue');
});
