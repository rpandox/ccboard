// Team P3, issue #199: conflict warnings in the launcher (app/static/launcher.js launcherTeamKit + the issue picker, app/static/team.js launchView / launchLines / launchPaint).
// A chip on a row of the issue picker, one calm line above Start for the picked issue, the branch a title would get and the files of a task being started. Advice only: Start is the
// same button with or without a warning, nothing is written, and with no repository shared the sheet makes no team call and has no team node.
// Real core.js, components.js, launcher.js, team.js, router.js, widgets.js, pages/agents.js and shell.js on minidom's DOM; api(), toast() and poll() are recorders.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const tick = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');
const words = (n) => text(n).replace(/\s+/g, ' ').trim();
const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'team.json'), 'utf8'));
const clone = (v) => JSON.parse(JSON.stringify(v));

const STATE = (team) => ({
  user: 'alice@example.com', config: { projects_dir: '/srv/projects', code_https_port: 10000 },
  agents: { claude: { installed: true, loggedIn: true }, codex: { installed: true, loggedIn: true } },
  projects: [
    { name: 'shop', path: '/srv/projects/shop', root: { name: 'root', path: '/srv/projects/shop', root: true, state: 'project', branch: null, dirty: null, sessions: [] }, orphan_sessions: [],
      repos: [{ name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] }] },
  ],
  clone_queue: { queued: [], done: [], cap: 3 }, pending_permissions: [], tasks: [], jobs: [], runs: [], ...(team ? { team: { shared: true } } : {}),
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

const ISSUES = [152, 154, 156, 160, 162, 164, 47].map((number) => ({ number, title: `Issue ${number} title`, body: 'Do it.', url: `https://github.com/acme/shop/issues/${number}`, labels: [], author: 'alice', trusted: true }));
const issueAnswer = ({ path: p }) => {
  const m = /issues\/(\d+)$/.exec(p);
  if (!m) return { issues: ISSUES };
  const i = ISSUES.find((x) => String(x.number) === m[1]);
  return { ...i, who: { claude: null, codex: null, default_agent: null, warnings: [] } };
};

/** The website block of the demo, moved to the repository of this world and read just now. */
const webBlock = (over = {}) => {
  const b = clone(FX.base.repos[0]);
  b.local = [{ project: 'shop', repo: 'api' }];
  b.read_at = new Date().toISOString();
  b.age_s = 0;
  return { ...b, ...over };
};
const teamBody = (b) => ({ api: 1, enabled: true, me: { login: 'alice', id: 1001 }, at: new Date().toISOString(), repos: [b] });

function lWorld({ team = true, block = webBlock(), teamAnswer = null } = {}) {
  const w = makeWorld({});
  installDom(w);
  selectSemantics(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'team.js', 'router.js', 'pages/widgets.js', 'pages/agents.js', 'shell.js']) w.load(f);
  Object.assign(w.ctx, { __calls: [], __toasts: [], __errors: [], __st: STATE(team), __issue: issueAnswer, __team: teamAnswer || (() => teamBody(block)) });
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      if (path === '/api/team/state') return __team({ method, path, body });
      if (path.startsWith('/api/projects/shop/repos/api/issues')) return __issue({ method, path, body });
      if (path === '/api/tasks') return { id: 31, slug: 'x', tmux: 'shop--api--t-x', branch: 'worktree-x' };
      return { ok: true };
    };
    poll = async () => {};
    toast = (t, o) => { __toasts.push({ text: t, kind: o && o.kind }); };
    setError = (m) => { __errors.push(m); };
    openPage = () => {};
    renderBanner = () => {};
    state = __st;
  `);
  return w;
}

const calls = (w) => plain(w.get('__calls'));
const teamCalls = (w) => calls(w).filter((c) => c.path.startsWith('/api/team'));
const posts = (w, re) => calls(w).filter((c) => c.method === 'POST' && re.test(c.path));
const sheet = (w) => w.document.getElementById('sheet');
const form = (w) => sheet(w).querySelector('form.form');
const submit = (f) => f.dispatchEvent({ type: 'submit', preventDefault() {} });
const openTask = async (w, extra = {}) => { w.run(`openLauncher(${JSON.stringify({ project: 'shop', repo: 'api', mode: 'task', ...extra })})`); await tick(); await tick(); return form(w); };
const start = (f) => f.querySelector('button[type=submit]');
const off = (n) => n.disabled || n.hasAttribute('disabled');
const issueSel = (f) => f.querySelector('.lx-issue select');
const lines = (f) => f.querySelectorAll('.lx-team-l');
const options = (f) => issueSel(f).children.map((o) => text(o));
const row = (f, n) => options(f).find((t) => t.startsWith(`#${n} `));
const focusList = async (f) => { issueSel(f).dispatchEvent({ type: 'focus' }); await tick(); await tick(); };
const pick = async (f, n) => {
  await focusList(f);
  const sel = issueSel(f);
  sel.value = String(n);
  sel.dispatchEvent({ type: 'change' });
  await tick(); await tick();
};
const typeTitle = (f, t) => {
  const inp = f.querySelectorAll('input').find((n) => n.getAttribute('placeholder') === 'e.g. Fix the login redirect');
  inp.value = t;
  inp.dispatchEvent({ type: 'input' });
};

// ---------------------------------------------------------------- off means off

test('with no repository shared the sheet makes no team call, has no team node and the picker rows are what they were', async () => {
  const w = lWorld({ team: false });
  const f = await openTask(w);
  await pick(f, 152);
  assert.deepEqual(teamCalls(w), [], 'no team call at all');
  assert.equal(f.querySelector('.lx-team'), null);
  assert.equal(row(f, 152), '#152 Issue 152 title');
  assert.ok(!off(start(f)));
});

test('a session form never builds the team node, even on a board that shares a repository', async () => {
  const w = lWorld();
  w.run(`openLauncher(${JSON.stringify({ project: 'shop', repo: 'api', mode: 'session' })})`);
  await tick(); await tick();
  assert.equal(form(w).querySelector('.lx-team'), null);
  assert.deepEqual(teamCalls(w), []);
});

test('on a board that shares a repository opening the sheet reads the cached team view once and nothing else, and no GitHub call goes out', async () => {
  const w = lWorld();
  await openTask(w);
  assert.deepEqual(teamCalls(w).map((c) => [c.method, c.path]), [['GET', '/api/team/state']]);
  assert.ok(calls(w).every((c) => c.method === 'GET'), 'a read, nothing written');
});

// ---------------------------------------------------------------- the chip on a row

test('a row of the issue picker carries a glyph and words: taken, seen lately, last seen with its age, a teammate whose login is hidden, and a second person', async () => {
  const w = lWorld();
  const f = await openTask(w);
  await focusList(f);
  assert.equal(row(f, 152), '#152 Issue 152 title · ! taken by @bob');
  assert.equal(row(f, 154), '#154 Issue 154 title · ! taken by @bob');
  assert.match(row(f, 156), /^#156 Issue 156 title · ◌ @bob, seen 7 min ago$/);
  assert.match(row(f, 160), /^#160 Issue 160 title · ◌ @bob, last seen 3 h ago$/);
  assert.equal(row(f, 162), '#162 Issue 162 title · ! taken by a teammate');
  assert.equal(row(f, 164), '#164 Issue 164 title · ! taken by @bob, +1');
  assert.equal(row(f, 47), '#47 Issue 47 title', 'an issue nobody holds has no chip');
  for (const t of options(f)) assert.ok(t.length <= 90, t);
});

test('a chip that arrives after the list is put on the rows that are already there', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const w = lWorld({ teamAnswer: () => gate });
  const f = await openTask(w);
  await focusList(f);
  assert.equal(row(f, 152), '#152 Issue 152 title', 'the team view has not come yet');
  release(teamBody(webBlock()));
  await tick(); await tick(); await tick();
  assert.equal(row(f, 152), '#152 Issue 152 title · ! taken by @bob');
});

test('a long title is cut before the chip, never the chip', async () => {
  const w = lWorld();
  ISSUES.find((i) => i.number === 152).title = 'T'.repeat(200);
  const f = await openTask(w);
  await focusList(f);
  const t = row(f, 152);
  ISSUES.find((i) => i.number === 152).title = 'Issue 152 title';
  assert.ok(t.length <= 90 && t.endsWith(' · ! taken by @bob'), t);
});

// ---------------------------------------------------------------- the line above Start

test('picking a taken issue shows who holds it and since when above Start; Start is the same button and still starts', async () => {
  const w = lWorld();
  const f = await openTask(w);
  const label = text(start(f));
  await pick(f, 152);
  const first = lines(f)[0];
  assert.equal(first.getAttribute('data-kind'), 'issue');
  assert.equal(first.getAttribute('data-status'), 'taken');
  assert.match(words(first), /^! #152 is being worked on by @bob since (?:[A-Z][a-z]{2} \d{1,2} )?\d{2}:\d{2}/);
  assert.match(words(first), /assigned on GitHub · live status: running · branch bob\/coupon-total · draft PR #158/);
  const kids = f.children;
  assert.ok(kids.indexOf(f.querySelector('.lx-team')) < kids.indexOf(f.querySelector('.lx-foot')), 'above Start');
  assert.equal(text(start(f)), label, 'the same button');
  assert.ok(!off(start(f)));
  submit(f);
  await tick(); await tick();
  assert.equal(posts(w, /\/api\/tasks$/).length, 1, 'it starts');
  assert.equal(posts(w, /\/api\/tasks$/)[0].body.issue_number, 152);
  assert.deepEqual(teamCalls(w).map((c) => c.method), ['GET'], 'no write to the team routes, no further read');
});

test('an issue held by an assignment and a claim says so and claims no time; recent and stale say how long ago; a hidden login is "a teammate"; two people are two lines', async () => {
  const w = lWorld();
  const f = await openTask(w);
  await pick(f, 154);
  assert.match(words(lines(f)[0]), /^! #154 is being worked on by @bob assigned on GitHub · holds a claim \(running\)$/, 'no "since": an assignment has no time');
  await pick(f, 156);
  assert.match(words(lines(f)[0]), /^◌ #156: @bob may be working on it, seen 7 min ago/);
  assert.equal(lines(f)[0].getAttribute('data-status'), 'recent');
  await pick(f, 160);
  assert.match(words(lines(f)[0]), /^◌ #160: @bob worked on it, last seen 3 h ago/);
  assert.equal(lines(f)[0].getAttribute('data-status'), 'stale');
  await pick(f, 162);
  assert.match(words(lines(f)[0]), /^! #162 is being worked on by a teammate/);
  assert.ok(!/@null|null/.test(words(f.querySelector('.lx-team'))));
  await pick(f, 164);
  assert.equal(lines(f).length, 2);
  assert.match(words(lines(f)[0]), /by @bob/);
  assert.match(words(lines(f)[1]), /by a teammate/);
  await pick(f, 47);
  assert.equal(f.querySelector('.lx-team').classList.contains('hidden'), true, 'nobody holds #47: nothing to say');
});

test('the branch a title would get is looked up: one line, gone when the title changes', async () => {
  const b = webBlock();
  b.warnings.branches['worktree-fix-the-login-redirect'] = [{ name: 'worktree-fix-the-login-redirect', url: 'https://github.com/example/website/tree/worktree-fix-the-login-redirect', person: { id: 1002, login: 'bob' },
    sources: ['pushed'], issue: null, pr: null, at: '2026-10-03T02:20:00Z', old: false, advice: true }];
  const w = lWorld({ block: b });
  const f = await openTask(w);
  await tick();
  assert.equal(f.querySelector('.lx-team').classList.contains('hidden'), true);
  typeTitle(f, 'Fix the login redirect');
  const l = lines(f);
  assert.equal(l.length, 1);
  assert.equal(l[0].getAttribute('data-kind'), 'branch');
  assert.match(words(l[0]), /^! Branch worktree-fix-the-login-redirect is already on GitHub under @bob pushed/);
  assert.ok(!off(start(f)));
  typeTitle(f, 'Something else');
  assert.equal(lines(f).length, 0);
});

test('picking an issue fills the title, and the branch that title makes is looked up too', async () => {
  const w = lWorld();
  const f = await openTask(w);
  const i154 = ISSUES.find((i) => i.number === 154);
  i154.title = 'Add a gift wrap option';
  const sel = issueSel(f);
  sel.dispatchEvent({ type: 'focus' });
  await tick(); await tick();
  sel.value = '154';
  sel.dispatchEvent({ type: 'change' });
  await tick(); await tick();
  i154.title = 'Issue 154 title';
  const kinds = lines(f).map((l) => l.getAttribute('data-kind'));
  assert.deepEqual(kinds, ['issue', 'branch'], '#154 and the branch worktree-154-add-a-gift-wrap-option');
});

test('starting a task shows its own issue, its branch and the files a pull request also changes, with the names and a link; more than six names say how many are left', async () => {
  const w = lWorld({ block: (() => { const b = webBlock(); b.warnings.tasks['7'].files[1].files.push('src/z.ts'); b.warnings.tasks['7'].files[1].count = 3000; b.warnings.tasks['7'].files[1].more = true; return b; })() });
  const task = { id: 7, title: 'Add alt text to the product gallery images', prompt: 'p', project: 'shop', repo: 'api', agent: 'claude', phase: 'backlog', slug: 'alt', tmux: '' };
  w.run(`openLauncher(${JSON.stringify({ project: 'shop', repo: 'api', mode: 'dispatch', task })})`);
  await tick(); await tick();
  const f = form(w);
  const l = lines(f).filter((x) => x.getAttribute('data-kind') === 'files');
  assert.equal(l.length, 2);
  assert.match(words(l[0]), /^! PR #151 by @bob also changes 1 of these files src\/components\/ProductGallery\.tsx$/);
  assert.match(words(l[1]), /^! PR #158 by @bob \(draft\) also changes 3000 or more of these files /);
  assert.match(words(l[1]), /and 2994 more/, 'a pull request with 3,000 files is cut to six names and a count');
  assert.equal(l[1].querySelectorAll('.lx-team-file').length, 6);
  const a = l[0].querySelector('a');
  assert.equal(a.getAttribute('href'), 'https://github.com/example/website/pull/151');
  assert.equal(a.getAttribute('target'), '_blank');
  assert.ok(a.getAttribute('rel').includes('noopener'));
  assert.ok(!off(start(f)), 'Start is still Start');
  assert.deepEqual(teamCalls(w).map((c) => c.method), ['GET']);
});

// ---------------------------------------------------------------- quiet states

test('a block that is loading, rate limited, old, or from a login the board does not know says so quietly, never hides the form and never disables Start', async () => {
  const cases = [
    [webBlock({ state: 'loading', warnings: null, message: 'Reading GitHub for the first time.' }), /GitHub is being read for the first time\. Nothing is checked yet\./],
    [webBlock({ state: 'rate_limited', message: 'GitHub asked this board to wait 60 s.', wait_s: 60, stale: true }), /Team: waiting for github\. Warnings are from the last read, just now\./i],
    [webBlock({ stale: true, read_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() }), /last read 3 h ago, so warnings may be out of date/],
    [(() => { const b = webBlock(); b.warnings.identity_known = false; return b; })(), /who you are on GitHub is not known yet, so nobody is checked/],
    [webBlock({ state: 'no_access', warnings: null }), /no access\. Nothing is checked yet\./i],
  ];
  for (const [block, re] of cases) {
    const w = lWorld({ block });
    const f = await openTask(w);
    const n = f.querySelector('.lx-team');
    assert.ok(n && !n.classList.contains('hidden'), block.state);
    assert.match(words(n.querySelector('.lx-team-n')), re);
    assert.ok(n.classList.contains('quiet'));
    assert.ok(!off(start(f)));
    assert.ok(f.querySelector('textarea'), 'the form is all there');
  }
});

test('a block read lately with no warning for the picked issue says nothing; warnings of null in an ok block say nothing either', async () => {
  const w = lWorld({ block: webBlock({ warnings: null }) });
  const f = await openTask(w);
  assert.ok(f.querySelector('.lx-team').classList.contains('hidden'));
});

test('a repository of another project is not this one: its warnings never show', async () => {
  const w = lWorld({ block: webBlock({ local: [{ project: 'other', repo: 'thing' }] }) });
  const f = await openTask(w);
  await pick(f, 152);
  assert.equal(row(f, 152), '#152 Issue 152 title');
  assert.ok(f.querySelector('.lx-team').classList.contains('hidden'));
});

test('the team view that cannot be read leaves the sheet as it was', async () => {
  const w = lWorld({ teamAnswer: () => { throw new Error('the board did not answer'); } });
  const f = await openTask(w);
  await pick(f, 152);
  assert.equal(row(f, 152), '#152 Issue 152 title');
  assert.ok(f.querySelector('.lx-team').classList.contains('hidden'));
  assert.ok(!off(start(f)));
});

// ---------------------------------------------------------------- hostile text

test('GitHub text is text: a login with markup, control and bidi characters, a path with HTML and a javascript: link are plain, capped, and never an element', async () => {
  const b = webBlock();
  const hostile = '<img src=x onerror=alert(1)>';
  b.warnings.issues['152'][0].person.login = hostile;
  b.warnings.tasks['7'].files[0].person.login = 'bob';
  b.warnings.tasks['7'].files[0].files = ['<b>x</b>‮\u0007.py', 'A'.repeat(900)];
  b.warnings.tasks['7'].files[0].count = 2;
  b.warnings.tasks['7'].files[0].pr.url = 'javascript:alert(1)';
  b.warnings.tasks['7'].files[0].pr.number = 151;
  const w = lWorld({ block: b });
  const f = await openTask(w);
  await pick(f, 152);
  assert.match(words(lines(f)[0]), /#152 is being worked on by a teammate/, 'a login that fails the rule is "a teammate", never shown');
  assert.equal(f.querySelectorAll('img').length, 0);
  assert.ok(!words(f.querySelector('.lx-team')).includes('onerror'));
  const task = { id: 7, title: 't', prompt: 'p', project: 'shop', repo: 'api', agent: 'claude', phase: 'backlog', slug: 't', tmux: '' };
  w.run(`openLauncher(${JSON.stringify({ project: 'shop', repo: 'api', mode: 'dispatch', task })})`);
  await tick(); await tick();
  const g = form(w);
  const fl = lines(g).filter((x) => x.getAttribute('data-kind') === 'files')[0];
  assert.equal(g.querySelectorAll('b').length, 0);
  assert.ok(words(fl).includes('<b>x</b> .py'), 'the markup is text, control and bidi characters are gone');
  assert.ok(words(fl).includes('…'), 'a 900 character path is cut');
  assert.equal(fl.querySelector('a'), null, 'a javascript: address is not a link');
});

test('the warning code never writes markup, sets a style, or touches a control: the launcher kit and the builders in team.js are text, classes and a repaint', () => {
  const launcher = fs.readFileSync(path.join(STATIC, 'launcher.js'), 'utf8');
  const kit = launcher.slice(launcher.indexOf('function launcherTeamKit'), launcher.indexOf('/* The picker both task forms share:'));
  const teamJs = fs.readFileSync(path.join(STATIC, 'team.js'), 'utf8');
  const sec = teamJs.slice(teamJs.indexOf('/* ---------- conflict warnings (issue #199'), teamJs.indexOf("/* ---------- the project page's Team tab"));
  assert.ok(kit.length > 800 && sec.length > 3000);
  for (const code of [kit, sec]) {
    assert.ok(!/innerHTML|insertAdjacentHTML|outerHTML|cssText|\.style\b|style:|document\.write|eval\(|new Function/.test(code));
    assert.ok(!/disabled|\.click\(|submit\(|dispatchEvent|confirm\(|'PUT'|'POST'|'DELETE'|'PATCH'/.test(code), 'no control is touched and nothing is written');
  }
  assert.ok(/api\(/.test(kit) === false, 'the kit makes no request of its own');
});

// ---------------------------------------------------------------- the older task form (taskForm) shares the picker and the kit

test('the task form of the create flow (taskForm) has the same chip and the same line above its Start, and the same off switch', async () => {
  for (const shared of [false, true]) {
    const w = lWorld({ team: shared });
    const f = w.run('taskForm(state.projects[0], state.projects[0].repos[0], {})');
    await tick(); await tick();
    const sel = f.querySelector('.lx-issue select');
    sel.dispatchEvent({ type: 'focus' });
    await tick(); await tick();
    const row152 = sel.children.map((o) => text(o)).find((t) => t.startsWith('#152 '));
    if (!shared) {
      assert.equal(f.querySelector('.lx-team'), null);
      assert.deepEqual(teamCalls(w), []);
      assert.equal(row152, '#152 Issue 152 title');
      continue;
    }
    assert.equal(row152, '#152 Issue 152 title · ! taken by @bob');
    sel.value = '152';
    sel.dispatchEvent({ type: 'change' });
    await tick(); await tick();
    assert.match(words(f.querySelector('.lx-team-l')), /^! #152 is being worked on by @bob since /);
    const go = f.querySelector('button[type=submit]');
    assert.ok(!off(go), 'Start is not touched');
    assert.ok(f.children.indexOf(f.querySelector('.lx-team')) < f.children.indexOf(f.querySelector('.submit')), 'above Start');
    assert.deepEqual(teamCalls(w).map((c) => c.method), ['GET']);
  }
});

test('while some pull requests have no file list yet, starting a task says that file overlap is not fully checked; creating one does not', async () => {
  const w = lWorld();
  const task = { id: 7, title: 'Add alt text to the product gallery images', prompt: 'p', project: 'shop', repo: 'api', agent: 'claude', phase: 'backlog', slug: 'alt', tmux: '' };
  w.run(`openLauncher(${JSON.stringify({ project: 'shop', repo: 'api', mode: 'dispatch', task })})`);
  await tick(); await tick();
  const notes = form(w).querySelectorAll('.lx-team-n').map((n) => words(n));
  assert.deepEqual(notes, ['Team: the changed files of 1 pull request are not read yet, so file overlap is not fully checked.']);
  const g = lWorld();
  const f = await openTask(g);
  await pick(f, 152);
  assert.equal(f.querySelectorAll('.lx-team-n').length, 0, 'a new task has no files to compare yet');
  const none = lWorld({ block: (() => { const b = webBlock(); b.warnings.files.pending = 0; return b; })() });
  none.run(`openLauncher(${JSON.stringify({ project: 'shop', repo: 'api', mode: 'dispatch', task })})`);
  await tick(); await tick();
  assert.equal(form(none).querySelectorAll('.lx-team-n').length, 0);
});

test('a link in a warning is 44 px to touch inside the sheet, which is outside #page', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'team.css'), 'utf8');
  assert.match(css, /@media \(pointer:coarse\) \{ \.lx-team a \{ padding:12px 0; \} \}/);
  assert.match(css, /html\.force-coarse \.lx-team a \{ padding:12px 0; \}/);
});
