// Contract tests for app/static/palette.js (the command palette, the shortcut help, the share prefill) and for the keyboard layer wired to the real
// pages (j / k / Enter / mod+1 / a / y / d / r through keymap.js and pages/*.js). A small DOM (tests/js/minidom.mjs) stands in for the browser.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

// ---------------------------------------------------------------- fixtures and the world

const T0 = Date.now();                                                    // one clock reading for every fixture: two sessions 30 min old must have the SAME state_at (the roster sorts on it to the millisecond; two readings flipped the order now and then)
const ISO = (minsAgo) => new Date(T0 - minsAgo * 60000).toISOString();
const sess = (name, over) => ({
  tmux: `shop--api--${name}`, name, created: Math.floor(Date.now() / 1000) - 7200, attached: 0, command: 'claude', launcher: 'claude',
  state: 'idle', state_at: ISO(30), needs_attention: false, last_prompt: 'fix the login bug', last_message: 'done, tests pass',
  stats: { model: 'Opus 5', context_pct: 42, cost_usd: 1.5 }, ...over,
});

// Roster order (waiting first, then by project): s2, s1, s3, sh
function fakeState(over = {}) {
  return {
    tmux_down: false, user: 'alice', version: 'test',
    config: { code_https_port: 10000, projects_dir: '/srv/projects', ntfy: { enabled: false }, backup: {} },
    claude: { installed: true, loggedIn: true, email: 'a@example.com', subscriptionType: 'max' }, login: { running: false },
    projects: [
      { name: 'shop', path: '/srv/projects/shop', root: null, orphan_sessions: [], repos: [
        { name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [
          sess('s1', { state: 'working', state_at: ISO(2) }),
          sess('s2', { state: 'waiting', needs_attention: true, state_at: ISO(10) }),
        ] }] },
      { name: 'blog', path: '/srv/projects/blog', root: null, orphan_sessions: [], repos: [
        { name: 'web', path: '/srv/projects/blog/web', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [
          { ...sess('s3', { state: 'idle' }), tmux: 'blog--web--s3' },
          { ...sess('sh', { state: 'idle', launcher: 'shell', command: 'bash', stats: null }), tmux: 'blog--web--sh' },
        ] }] },
    ],
    pending_permissions: [{ id: 7, tmux_name: 'shop--api--s2', tool_name: 'Bash', summary: 'Bash: npm test' }],
    tasks: [], jobs: [], runs: [], nodes: null, health: null, backup: null, ...over,
  };
}

const S2 = 'shop--api--s2';
const S1 = 'shop--api--s1';
const S3 = 'blog--web--s3';

/** Core, components, keymap, palette, router and every page, in index.html order (shell.js left out), with recorders for api, toast and window.open. */
function world({ state = fakeState(), platform = 'Linux x86_64', search = '', wide = false, keys = true } = {}) {
  const w = makeWorld({ navigator: { platform, userAgent: 'node-test' }, matchMedia: (q) => ({ matches: wide && /1024/.test(q), addEventListener() {}, removeEventListener() {} }) });
  const dom = installDom(w);
  w.location.search = search;
  for (const f of ['core.js', 'components.js', 'keymap.js', 'live.js', 'launcher.js', 'palette.js', 'router.js']) w.load(f);
  w.ctx.__calls = []; w.ctx.__toasts = []; w.ctx.__opened = [];
  w.run(`
    api = async (method, path, body) => { __calls.push({ method, path, body }); return { ok: true }; };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    poll = async () => {};
    window.open = (url) => { __opened.push(url); };
  `);
  for (const f of ['home', 'inbox', 'tasks', 'agents', 'settings', 'search', 'session', 'placeholders']) w.load(`pages/${f}.js`);
  w.ctx.__st = state;
  w.run('state = __st');
  if (keys) w.run('Keymap.install()');                    // main.js does this at boot; idempotent
  return { w, dom };
}

const calls = (w) => plain(w.get('__calls'));
const toasts = (w) => plain(w.get('__toasts'));
const tick = () => new Promise((r) => setImmediate(r));
const dlg = (w) => w.document.querySelector('#helpdlg');
const sheet = (w) => w.document.querySelector('#sheet');
const items = (w) => dlg(w).querySelectorAll('.pal-item');
const labels = (w) => items(w).map((n) => n.querySelector('.pal-label').textContent);
const groups = (w) => dlg(w).querySelectorAll('.pal-group').map((n) => n.textContent);
const selected = (w) => dlg(w).querySelectorAll('.pal-item').filter((n) => n.classList.contains('sel')).map((n) => n.querySelector('.pal-label').textContent);
const box = (w) => dlg(w).querySelector('.pal-input');

function key(k, mods = {}) {
  const e = { key: k, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, target: null, defaultPrevented: false, ...mods, prevented: false };
  e.preventDefault = () => { e.prevented = true; };
  return e;
}
const press = (w, k, mods) => { const e = key(k, mods); w.document.dispatch('keydown', e); return e; };
const typeInto = (w, text) => { const b = box(w); b.value = text; b.dispatchEvent({ type: 'input' }); };
const keyInBox = (w, k, mods) => { const e = key(k, { target: box(w), ...mods }); box(w).dispatchEvent({ type: 'keydown', ...e }); return e; };
const active = (w) => w.document.activeElement;
const mounted = (w, hash) => { w.location.hash = hash; };

// ---------------------------------------------------------------- load

test('palette.js defines only: nothing touches the DOM, a timer or a listener at load', () => {
  const trap = (what) => new Proxy(function () {}, {
    get(_t, prop) { if (prop === Symbol.toPrimitive || prop === 'then') return undefined; throw new Error(`load-time access to ${what}.${String(prop)}`); },
    apply() { throw new Error(`load-time call of ${what}()`); },
  });
  const boom = (what) => () => { throw new Error(`load-time call of ${what}()`); };
  const w = makeWorld({ document: trap('document'), localStorage: trap('localStorage'), navigator: trap('navigator'), location: trap('location'), history: trap('history'),
    addEventListener: boom('window.addEventListener'), setTimeout: boom('setTimeout'), setInterval: boom('setInterval'), fetch: boom('fetch') });
  w.load('core.js');
  w.load('palette.js');
  assert.equal(w.run('Palette.ui'), null);
});

// ---------------------------------------------------------------- fuzzy match

test('score: a substring beats a subsequence, a prefix beats the middle, a non-match is null, hits mark the letters', () => {
  const { w } = world();
  const score = (q, t) => plain(w.get('Palette').score(q, t));
  assert.equal(score('xyz', 'continue'), null);
  assert.equal(score('', 'anything').score, 0);
  const prefix = score('con', 'continue');
  const mid = score('tin', 'continue');
  const seq = score('cnt', 'continue');
  assert.deepEqual(prefix.hits, [0, 1, 2]);
  assert.deepEqual(mid.hits, [3, 4, 5]);
  assert.ok(prefix.score > mid.score, 'a prefix beats the middle of a word');
  assert.ok(mid.score > seq.score, 'a substring beats a scattered subsequence');
  assert.deepEqual(seq.hits, [0, 2, 3], 'subsequence hits are the letters, left to right');
  assert.ok(score('wd', 'shop/web-dev').score > score('wd', 'wxxxxxxxxd').score, 'word starts and a tight match beat a scattered one');
  assert.ok(score('API', 'shop/api'), 'case does not matter');
  assert.ok(score('s a', 'shop api'), 'spaces in the query are ignored');
});

test('highlight wraps the matched characters in pal-hit spans and leaves the rest as text', () => {
  const { w } = world();
  const nodes = w.run("Palette.highlight('continue', [0, 1, 7])");
  const hits = plain(nodes.filter((n) => n.nodeType === 1).map((n) => n.textContent));
  assert.deepEqual(hits, ['co', 'e']);
  assert.equal(nodes.map((n) => (typeof n === 'string' ? n : n.textContent)).join(''), 'continue');
  assert.deepEqual(plain(w.run("Palette.highlight('plain', [])")), ['plain']);
});

// ---------------------------------------------------------------- the palette

test('open(): one modal dialog#helpdlg with class palette, the box focused, Sessions in roster order then Routes; no nudges without a session in focus', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.open()');
  assert.equal(dlg(w).open, true);
  assert.ok(dlg(w).classList.contains('palette'));
  assert.equal(active(w), box(w), 'typing starts at once');
  assert.deepEqual(groups(w), ['Sessions', 'Routes']);
  const l = labels(w);
  assert.deepEqual(l.slice(0, 4), ['s2', 's1', 's3', 'sh'], 'the Agents order: needs-you first');
  assert.deepEqual(l.slice(4), ['Home', 'Needs you', 'Agents', 'Tasks', 'Quad', 'Usage', 'Memory', 'Settings', 'Search'], 'Quad sits after Tasks, as in the nav (g q)');
  assert.deepEqual(selected(w), ['s2'], 'the first row is highlighted');
  assert.equal(box(w).getAttribute('role'), 'combobox');
  assert.ok(box(w).getAttribute('aria-activedescendant'), 'the highlighted row is announced');
  const first = items(w)[0];
  assert.match(first.textContent, /shop\/api/);
  assert.match(first.textContent, /needs you/, 'the state is spelled out beside the glyph');
  assert.equal(first.querySelector('kbd').textContent, 'Ctrl+1');
  assert.equal(items(w)[1].querySelector('kbd').textContent, 'Ctrl+2');
  assert.ok(first.querySelector('.glyph.waiting'));
});

test('on a Mac the session numbers read ⌘1..9', () => {
  const { w } = world({ platform: 'MacIntel' });
  mounted(w, '#/');
  w.run('Palette.open()');
  assert.equal(items(w)[0].querySelector('kbd').textContent, '⌘1');
});

test('an empty search shows nine sessions at most; a search finds the rest', () => {
  const sessions = [];
  for (let i = 1; i <= 12; i++) sessions.push({ ...sess('x' + i, { state: 'idle' }), tmux: `big--repo--x${i}`, name: 'x' + i });
  const st = fakeState();
  st.projects = [{ name: 'big', path: '/srv/projects/big', root: null, orphan_sessions: [], repos: [{ name: 'repo', path: '/srv/projects/big/repo', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions }] }];
  const { w } = world({ state: st });
  mounted(w, '#/');
  w.run('Palette.open()');
  assert.equal(items(w).filter((n) => n.id.startsWith('pal-o-')).length - 9, 9, 'nine session rows plus the nine routes');
  typeInto(w, 'x12');
  assert.deepEqual(labels(w).slice(0, 1), ['x12']);
});

test('typing filters with a fuzzy match, ranks the best first, drops empty groups and keeps the first row selected', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.open()');
  typeInto(w, 'api');
  assert.deepEqual(groups(w), ['Sessions', 'Search'], 'no route matches api; the query is still offered to the transcript search, last');
  assert.deepEqual(labels(w).slice(0, 2).sort(), ['s1', 's2']);
  assert.ok(!labels(w).includes('s3'), 'blog/web does not match api');
  assert.equal(labels(w).at(-1), 'Search transcripts for "api"', 'the search row closes the list, always');
  typeInto(w, 'agt');
  assert.deepEqual(groups(w), ['Routes', 'Search']);
  assert.equal(labels(w)[0], 'Agents', 'subsequence match');
  assert.deepEqual(selected(w), ['Agents']);
  typeInto(w, 'zzzqq');
  assert.deepEqual(labels(w), ['Search transcripts for "zzzqq"']);
});

test('searching ranks across groups: the group holding the best match leads, so Enter runs the best match; the search row stays last', () => {
  const { w } = world();
  mounted(w, `#/s/${S1}`);
  w.run('Palette.open()');
  typeInto(w, 'pl');
  assert.equal(labels(w)[0], 'plan', 'a prefix of a mode beats scattered letters of a project path');
  assert.equal(groups(w)[0], 'Modes');
  assert.equal(groups(w).at(-1), 'Search');
  typeInto(w, 'cont');
  assert.deepEqual([groups(w)[0], labels(w)[0]], ['Nudge · s1', 'continue']);
  typeInto(w, 'blog');
  assert.deepEqual([groups(w)[0], labels(w)[0]], ['Sessions', 's3'], 'a project name finds its sessions through the hint');
  assert.ok(!labels(w).includes('s1'), 'shop/api does not contain blog');
});

test('hint text matches by substring only: letters scattered across a long path rank nothing', () => {
  const { w } = world();
  const item = { label: 'plan', hint: 'phasezero (project folder) · ended', keywords: '' };
  assert.equal(w.get('Palette').matchItem('pl', { label: 's1', hint: 'phasezero (project folder) · ended' }), null);
  assert.ok(w.get('Palette').matchItem('proj', { label: 's1', hint: 'phasezero (project folder) · ended' }), 'a word of the hint is a hit');
  assert.ok(w.get('Palette').matchItem('pl', item));
});

test('Enter on a session navigates to its peek and closes the palette; Esc closes without doing anything', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.open()');
  typeInto(w, 'blog');
  const e = keyInBox(w, 'Enter');
  assert.equal(e.prevented, true);
  assert.equal(w.location.hash, `#/s/${S3}`);
  assert.equal(dlg(w).open, false);
  assert.equal(dlg(w).textContent, '', 'the dialog is emptied on close');
  assert.equal(w.run('Palette.ui'), null);
  w.run('Palette.open()');
  keyInBox(w, 'Escape');
  assert.equal(dlg(w).open, false);
  assert.equal(w.location.hash, `#/s/${S3}`, 'unchanged');
});

test('arrow keys, Ctrl+N / Ctrl+P and the page keys move the highlight, wrapping at the ends', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.open()');
  const total = items(w).length;
  keyInBox(w, 'ArrowDown');
  assert.deepEqual(selected(w), ['s1']);
  keyInBox(w, 'n', { ctrlKey: true });
  assert.deepEqual(selected(w), ['s3']);
  keyInBox(w, 'ArrowUp');
  keyInBox(w, 'p', { ctrlKey: true });
  assert.deepEqual(selected(w), ['s2']);
  keyInBox(w, 'ArrowUp');
  assert.equal(selected(w)[0], 'Search', 'wraps to the last row');
  keyInBox(w, 'ArrowDown');
  assert.deepEqual(selected(w), ['s2'], 'and back to the first');
  keyInBox(w, 'PageDown');
  assert.equal(items(w).filter((n) => n.classList.contains('sel')).length, 1);
  keyInBox(w, 'PageUp');
  assert.deepEqual(selected(w), ['s2']);
  assert.ok(total > 8);
});

test('an IME composition does not drive the list', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.open()');
  keyInBox(w, 'ArrowDown', { isComposing: true });
  keyInBox(w, 'Enter', { isComposing: true });
  assert.equal(dlg(w).open, true);
  assert.deepEqual(selected(w), ['s2']);
});

test('a route row navigates; the search row carries what was typed', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.open()');
  typeInto(w, 'usage');
  assert.equal(labels(w)[0], 'Usage');
  keyInBox(w, 'Enter');
  assert.equal(w.location.hash, '#/usage');
  w.run('Palette.open()');
  typeInto(w, 'stack trace');
  keyInBox(w, 'ArrowDown');
  assert.equal(selected(w)[0].startsWith('Search transcripts for'), true);
  keyInBox(w, 'Enter');
  assert.equal(w.location.hash, '#/search?q=stack%20trace');
});

test('clicking a row runs it; a click on the backdrop closes', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.open()');
  items(w).find((n) => n.querySelector('.pal-label').textContent === 'Tasks').click();
  assert.equal(w.location.hash, '#/tasks');
  assert.equal(dlg(w).open, false);
  w.run('Palette.open()');
  dlg(w).dispatchEvent({ type: 'click', target: dlg(w) });
  assert.equal(dlg(w).open, false);
});

test('toggle() opens, closes, and swaps the help for the palette', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.toggle()');
  assert.equal(dlg(w).open, true);
  w.run('Palette.toggle()');
  assert.equal(dlg(w).open, false);
  w.run('Palette.openHelp()');
  assert.ok(dlg(w).classList.contains('help'));
  w.run('Palette.toggle()');
  assert.equal(dlg(w).open, true);
  assert.ok(dlg(w).classList.contains('palette') && !dlg(w).classList.contains('help'), 'the same dialog, now the palette');
  assert.ok(box(w));
});

// ---------------------------------------------------------------- nudges, controls, modes

test('nudges POST {text, enter: true} through /keys and controls POST {cmd} through /command, each toasting the outcome', async () => {
  const { w } = world();
  mounted(w, '#/agents');
  press(w, 'j');
  press(w, 'j');                                           // roster order: s2, s1 -> the second row
  assert.equal(w.run('Pages.target()'), S1);
  w.run('Palette.open()');
  assert.deepEqual(groups(w), ['Sessions', 'Routes', 'Nudge · s1', 'Controls · s1', 'Modes', 'More']);
  const nudge = (label) => items(w).find((n) => n.querySelector('.pal-label').textContent === label);
  assert.deepEqual(items(w).filter((n) => n.id).slice(13, 19).map((n) => n.querySelector('.pal-label').textContent), ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it']);
  typeInto(w, 'add commit');
  assert.equal(labels(w)[0], 'add commit push');
  keyInBox(w, 'Enter');
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S1}/keys`, body: { text: 'add commit push', enter: true } });
  assert.deepEqual(toasts(w).pop(), { text: 'sent "add commit push" to s1', kind: 'ok' });
  assert.equal(dlg(w).open, false);
  w.run('Palette.open()');
  nudge('/compact').click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S1}/command`, body: { cmd: 'compact' } }, 'a control goes through the guarded endpoint, cmd without the slash');
  assert.deepEqual(toasts(w).pop(), { text: 'sent "/compact" to s1', kind: 'ok' });
  assert.equal(calls(w).filter((c) => c.path.endsWith('/keys')).length, 1, 'only the nudge used /keys');
});

test('the controls are exactly /compact /context /cost /usage /status: /clear, /effort and /model wait for the guarded endpoint', () => {
  const { w } = world();
  mounted(w, '#/agents');
  press(w, 'j');
  w.run('Palette.open()');
  const all = labels(w);
  for (const c of ['/compact', '/context', '/cost', '/usage', '/status']) assert.ok(all.includes(c), c);
  for (const c of ['/clear', '/effort', '/model']) assert.ok(!all.includes(c), `${c} is not offered yet`);
  typeInto(w, '/clear');
  assert.ok(!labels(w).includes('/clear'));
});

// ---- controls through POST /command (v0.5.8): 404 falls back to /keys, 409 toasts the reason, a read command shows its screen, registry labels

const httpError = (status, message, body) => Object.assign(new Error(message), { status, body });
/** api answers through fn(method, path, body) (return a value or throw), recording every call. */
function replying(w, fn) {
  w.ctx.__reply = fn;
  w.run('api = async (method, path, body) => { __calls.push({ method, path, body }); return __reply(method, path, body); }');
}
const kind = (c) => c.path.split('/').pop();
/** The world with s1 selected and the palette open (the roster order is s2, s1). */
function withS1() {
  const env = world();
  mounted(env.w, '#/agents');
  press(env.w, 'j');
  press(env.w, 'j');
  return env;
}
const pickControl = async (w, query) => { w.run('Palette.open()'); typeInto(w, query); keyInBox(w, 'Enter'); await tick(); };

test('a control posts /command {cmd} for the session in focus: cmd has no slash, nothing is typed through /keys', async () => {
  const { w } = withS1();
  await pickControl(w, '/status');
  assert.deepEqual(calls(w), [{ method: 'POST', path: `/api/sessions/${S1}/command`, body: { cmd: 'status' } }]);
  assert.deepEqual(toasts(w).pop(), { text: 'sent "/status" to s1', kind: 'ok' });
  assert.equal(dlg(w).open, false);
});

test('an older server without /command (404 with no error body) gets the control typed through /keys instead', async () => {
  const { w } = withS1();
  replying(w, (m, path) => { if (path.endsWith('/command')) throw httpError(404, '404 Not Found', { detail: 'Not Found' }); return { ok: true }; });
  await pickControl(w, '/compact');
  assert.deepEqual(calls(w).map((c) => [kind(c), c.body]), [['command', { cmd: 'compact' }], ['keys', { text: '/compact', enter: true }]]);
  assert.deepEqual(toasts(w).pop(), { text: 'sent "/compact" to s1', kind: 'ok' });
});

test('a 404 that names the session ("session ... not found") is the real answer: no fallback, the message is toasted', async () => {
  const { w } = withS1();
  replying(w, () => { throw httpError(404, `session ${S1} not found`, { error: `session ${S1} not found` }); });
  await pickControl(w, '/compact');
  assert.deepEqual(calls(w).map(kind), ['command'], 'nothing is typed into a session that is gone');
  assert.deepEqual(toasts(w).pop(), { text: `session ${S1} not found`, kind: 'bad' });
});

test('a 409 toasts the reason and when to ask again as a warning; no fallback, no retry on its own', async () => {
  const { w } = withS1();
  replying(w, () => { throw httpError(409, 'working', { error: 'working', message: 'the session is working', state: 'working', wait_kind: null, retry: 5 }); });
  await pickControl(w, '/compact');
  assert.deepEqual(calls(w).map(kind), ['command']);
  assert.deepEqual(toasts(w).pop(), { text: 'the session is working (try again in 5 s)', kind: 'warn' });
  replying(w, () => { throw httpError(409, 'ended', { error: 'ended', message: 'the session has ended', state: 'ended', wait_kind: null, retry: null }); });
  await pickControl(w, '/cost');
  assert.deepEqual(toasts(w).pop(), { text: 'the session has ended', kind: 'warn' }, 'retry: null means waiting will not help: no suffix');
  replying(w, () => { throw httpError(400, 'unknown command; allowed: /clear', { error: 'unknown command; allowed: /clear' }); });
  await pickControl(w, '/usage');
  assert.equal(toasts(w).pop().kind, 'bad', 'anything but a 409 is an error');
});

test('a read control shows the captured screen in its own dialog instead of a toast, and presses Escape when it closes (Claude keeps its dialog open)', async () => {
  const { w } = withS1();
  replying(w, (m, path) => (path.endsWith('/command') ? { ok: true, sent: '/usage', verified: false, screen: 'Usage\n  5h  12%\n  7d  40%\n\n' } : { ok: true }));
  const before = toasts(w).length;
  await pickControl(w, '/usage');
  const rd = w.document.querySelector('dialog.readout');
  assert.ok(rd && rd.open, 'a modal <dialog>');
  assert.equal(rd.querySelector('.qr-title').textContent, '/usage · s1');
  assert.equal(rd.querySelector('pre.cmd-readout').textContent, 'Usage\n  5h  12%\n  7d  40%', 'the screen, trailing blank lines trimmed');
  assert.equal(toasts(w).length, before, 'no "sent" toast: the readout is the answer');
  assert.deepEqual(calls(w).map(kind), ['command'], 'Escape waits until the readout closes');
  rd.querySelector('button').click();
  await tick();
  assert.equal(w.document.querySelector('dialog.readout'), null);
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S1}/keys`, body: { keys: ['Escape'] } });
  assert.equal(calls(w).filter((c) => kind(c) === 'keys').length, 1, 'exactly one Escape');
  // Esc closes it the same way; a set command has no screen and gets the toast
  await pickControl(w, '/usage');
  w.document.querySelector('dialog.readout').dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {} });
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S1}/keys`, body: { keys: ['Escape'] } });
  replying(w, (m, path) => (path.endsWith('/command') ? { ok: true, sent: '/compact', verified: false } : { ok: true }));
  await pickControl(w, '/compact');
  assert.equal(toasts(w).pop().text, 'sent "/compact" to s1');
  assert.equal(w.document.querySelector('dialog.readout'), null);
});

test('a readout opened over an open peek leaves the peek alone (it is its own dialog, not the sheet)', async () => {
  const { w } = world();
  mounted(w, `#/s/${S1}`);
  assert.equal(sheet(w).open, true);
  replying(w, (m, path) => (path.endsWith('/command') ? { ok: true, sent: '/status', verified: false, screen: 'Status: ok' } : { ok: true }));
  w.run('Palette.open()');
  typeInto(w, '/status');
  keyInBox(w, 'Enter');
  await tick();
  assert.ok(w.document.querySelector('dialog.readout').open);
  assert.equal(sheet(w).open, true, 'the peek is still there');
  assert.ok(sheet(w).querySelector('.peek-send textarea'), 'with its content');
  assert.equal(w.location.hash, `#/s/${S1}`);
});

const REGISTRY = { agents: { claude: { slash: {
  clear: { cmd: '/clear', label: 'Clear', weight: 155, destructive: true }, compact: { cmd: '/compact', label: 'Compact', weight: 120 }, usage: { cmd: '/usage', label: 'Usage', read: true, weight: 100 },
  context: { cmd: '/context', label: 'Context', read: true, weight: 9 }, status: { cmd: '/status', label: 'Status', read: true, weight: 3 }, cost: { cmd: '/cost', label: 'Cost', read: true, weight: 0 } } } } };
const controlLabels = (w) => plain(w.run("Palette.ui.shown.filter((i) => i.id.startsWith('c:')).map((i) => i.label)"));   // the Controls group (a route is also called Usage)
const STATIC_CONTROLS = ['/compact', '/context', '/cost', '/usage', '/status'];
const BY_WEIGHT = ['Compact', 'Usage', 'Context', 'Status', 'Cost'];

test('with the registry cached by the terminal page the controls carry its labels, heaviest first (only the five: /clear stays in the tuning strip), and a typed /usage still finds Usage', async () => {
  const { w } = withS1();
  w.sessionStorage.setItem('ccboard:agents', JSON.stringify({ at: Date.now(), data: REGISTRY }));
  w.run('Palette.open()');
  assert.deepEqual(controlLabels(w), BY_WEIGHT);
  assert.ok(!labels(w).includes('Clear'));
  const hint = plain(w.run("Palette.ui.shown.find((i) => i.id === 'c:/usage').hint"));
  assert.equal(hint, '/usage · typed into s1', 'the slash text stays visible next to the registry label');
  typeInto(w, '/usage');
  assert.equal(labels(w)[0], 'Usage', 'the slash command is still searchable');
  keyInBox(w, 'Enter');
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S1}/command`, body: { cmd: 'usage' } });
});

test('the cached registry is read as the bare answer too, ignored when older than 10 minutes or unreadable, and only offers what the agent has', () => {
  const read = (raw) => {
    const { w } = withS1();
    if (raw !== null) w.sessionStorage.setItem('ccboard:agents', typeof raw === 'string' ? raw : JSON.stringify(raw));
    w.run('Palette.open()');
    return controlLabels(w);
  };
  assert.deepEqual(read(null), STATIC_CONTROLS, 'nothing cached: the static list');
  assert.deepEqual(read('not json'), STATIC_CONTROLS);
  assert.deepEqual(read({ at: Date.now() - 11 * 60 * 1000, data: REGISTRY }), STATIC_CONTROLS, 'older than 10 minutes');
  assert.deepEqual(read(REGISTRY), BY_WEIGHT, 'the bare GET /api/agents answer');
  assert.deepEqual(read({ at: Date.now(), agents: REGISTRY.agents }), BY_WEIGHT, 'the entry the terminal page writes (term.js: {at, agents}, key TermPage.AGENTS_KEY)');
  assert.deepEqual(read({ t: Math.floor(Date.now() / 1000), v: { agents: { claude: { slash: { compact: { label: 'Compact', weight: 1 } } } } } }), ['Compact'], 'a seconds stamp; a command the agent lacks is not offered');
  assert.deepEqual(read({ agents: { codex: { slash: {} } } }), STATIC_CONTROLS, 'no entry for this agent: the static list');
});

// ---- the peek's nudge chips share the quick-reply list with the terminal page

const DEFAULT_CHIPS = ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'];

test('the quick-reply defaults (components.js) are the nudges of the session cards and the palette: one list, three places', () => {
  const { w } = world();
  assert.deepEqual(plain(w.get('QUICK_DEFAULTS')), DEFAULT_CHIPS);
  assert.deepEqual(plain(w.get('SESSION_NUDGES')), DEFAULT_CHIPS);
  assert.deepEqual(plain(w.run('Palette.NUDGES')), DEFAULT_CHIPS);
});

test('the peek: chips are ccboard:quick:<tmux> (the agent defaults until edited), the pencil opens the dialog editor, Save re-renders them and stores the list', async () => {
  const { w } = world();
  mounted(w, `#/s/${S1}`);
  const chips = () => sheet(w).querySelector('.chips');
  const names = () => chips().querySelectorAll('.chip-btn').map((n) => n.textContent);
  assert.deepEqual(names(), DEFAULT_CHIPS);
  const pencil = chips().querySelector('.qr-edit');
  assert.ok(pencil && !pencil.classList.contains('chip-btn'), 'a pencil beside the chips, not counted as a reply');
  assert.equal(pencil.getAttribute('aria-label'), 'Edit quick replies');
  pencil.click();
  const ed = w.document.querySelector('dialog.qr-editor');
  assert.ok(ed && ed.open, 'a <dialog>, never window.prompt');
  const inputs = ed.querySelectorAll('input');
  assert.deepEqual(inputs.map((i) => i.value), DEFAULT_CHIPS);
  inputs[0].value = '  ship it  ';
  inputs[1].value = '';
  inputs[2].value = 'ship it';                                          // a duplicate of the first
  ed.querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
  assert.equal(w.document.querySelector('dialog.qr-editor'), null, 'the dialog is gone after Save');
  assert.deepEqual(names(), ['ship it', 'pr', 'add commit push', 'do it']);
  assert.deepEqual(JSON.parse(w.localStorage.getItem(`ccboard:quick:${S1}`)), ['ship it', 'pr', 'add commit push', 'do it'], 'the key the terminal page reads');
  chips().querySelectorAll('.chip-btn')[0].click();
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S1}/keys`, body: { text: 'ship it', enter: true } }, 'a tap sends the (edited) reply like the card chips do');
  mounted(w, '#/agents');
  mounted(w, `#/s/${S3}`);
  assert.deepEqual(chips().querySelectorAll('.chip-btn').map((n) => n.textContent), DEFAULT_CHIPS, 'another session has its own list');
});

test('the peek chips pick up what the terminal page saved, and a long press on a chip opens the same editor', () => {
  const { w } = world();
  w.localStorage.setItem(`ccboard:quick:${S1}`, JSON.stringify(['y', 'n']));
  mounted(w, `#/s/${S1}`);
  const chip = sheet(w).querySelectorAll('.chips .chip-btn')[0];
  assert.deepEqual(sheet(w).querySelectorAll('.chips .chip-btn').map((n) => n.textContent), ['y', 'n']);
  chip.dispatchEvent({ type: 'contextmenu', preventDefault() {} });
  const ed = w.document.querySelector('dialog.qr-editor');
  assert.ok(ed && ed.open);
  assert.deepEqual(ed.querySelectorAll('input').map((i) => i.value), ['y', 'n']);
});

test('a failed send says so as a bad toast, and the palette is already closed', async () => {
  const { w } = world();
  mounted(w, '#/agents');
  press(w, 'j');
  w.run("api = async () => { throw new Error('tmux is down'); }");
  w.run('Palette.open()');
  typeInto(w, 'continue');
  keyInBox(w, 'Enter');
  await tick();
  assert.deepEqual(toasts(w).pop(), { text: 'tmux is down', kind: 'bad' });
});

test('a plain shell session gets no nudges and no controls (typing "push" into bash would run it)', () => {
  const { w } = world();
  mounted(w, '#/agents');
  w.run(`Pages.setIndex(Pages.items().findIndex((s) => s.tmux === 'blog--web--sh'))`);
  assert.equal(w.run('Pages.target()'), 'blog--web--sh');
  w.run('Palette.open()');
  assert.ok(!groups(w).some((g) => /^(Nudge|Controls)/.test(g)), groups(w).join());
});

test('the peeked session is the target when nothing is selected', async () => {
  const { w } = world();
  mounted(w, '#/agents');
  mounted(w, `#/s/${S3}`);
  assert.equal(w.run('Pages.target()'), S3);
  w.run('Palette.open()');
  assert.ok(groups(w).includes('Nudge · s3'));
  typeInto(w, 'do it');
  keyInBox(w, 'Enter');
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S3}/keys`, body: { text: 'do it', enter: true } });
});

test('modes insert into the peek send box and More is collapsed until opened or searched', () => {
  const { w } = world();
  mounted(w, `#/s/${S1}`);
  const send = sheet(w).querySelector('.peek-send textarea');
  assert.ok(send, 'the peek (a sheet here) has its send box');
  w.run('Palette.open()');
  assert.deepEqual(groups(w).slice(-3), ['Controls · s1', 'Modes', 'More']);
  const rows = labels(w);
  assert.deepEqual(rows.slice(-3), ['ultracode', 'plan', 'More commands…']);
  assert.ok(!rows.includes('/color'), 'the fun commands stay folded');
  typeInto(w, 'plan');
  keyInBox(w, 'Enter');
  assert.equal(send.value, 'plan ');
  assert.equal(active(w), send, 'focus lands in the send box');
  send.value = 'refactor the parser';
  w.run('Palette.open()');
  typeInto(w, 'ultra');
  keyInBox(w, 'Enter');
  assert.equal(send.value, 'ultracode refactor the parser', 'a mode goes in front of what is typed');
  w.run('Palette.open()');
  typeInto(w, 'ultra');
  keyInBox(w, 'Enter');
  assert.equal(send.value, 'ultracode refactor the parser', 'and only once');
});

test('More: Enter on the folded row opens it; a typed query finds the commands without opening it; a command inserts as plain text', () => {
  const { w } = world();
  mounted(w, `#/s/${S1}`);
  const send = sheet(w).querySelector('.peek-send textarea');
  w.run('Palette.open()');
  const folded = items(w).at(-1);
  assert.equal(folded.querySelector('.pal-label').textContent, 'More commands…');
  assert.match(folded.textContent, /7 commands/);
  folded.click();
  assert.equal(dlg(w).open, true, 'opening More keeps the palette open');
  assert.deepEqual(labels(w).slice(-7), ['/color', '/copy', '/rewind', '/radio', '/stickers', '/tui', '/passes']);
  assert.deepEqual(selected(w), ['/color']);
  keyInBox(w, 'Enter');
  assert.equal(send.value, '/color');
  send.value = 'hello ';
  send.selectionStart = send.selectionEnd = 6;
  w.run('Palette.open()');
  typeInto(w, 'stick');
  assert.deepEqual(labels(w)[0], '/stickers');
  keyInBox(w, 'Enter');
  assert.equal(send.value, 'hello /stickers');
});

test('modes with no peek and nothing selected explain themselves instead of failing', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.open()');
  assert.ok(!groups(w).includes('Modes') && !groups(w).includes('More'), 'no send box and no target: nothing to insert into');
  assert.equal(w.run("Palette.insert('plan ', 'prefix')"), false);
  assert.deepEqual(toasts(w).pop(), { text: 'Open a session first: the text goes into its send box.', kind: 'warn' });
});

test('modes with a selected session and no peek open its peek first, then fill the box', async () => {
  const { w } = world();
  mounted(w, '#/agents');
  press(w, 'j');
  w.run('Palette.open()');
  typeInto(w, 'plan');
  keyInBox(w, 'Enter');
  assert.equal(w.location.hash, `#/s/${S2}`, 'the peek opens');
  const send = sheet(w).querySelector('.peek-send textarea');
  assert.ok(send);
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(send.value, 'plan ');
});

// ---------------------------------------------------------------- share target

test('shareFromQuery: text or url make a share, the parts are joined and de-duplicated, anything else is not one', () => {
  const { w } = world();
  const share = (s) => w.get('Palette').shareFromQuery(s);
  assert.equal(share(''), null);
  assert.equal(share('?demo=1'), null);
  assert.equal(share('?title=Only'), null, 'a bare title is not a share');
  assert.equal(share('?share=1&title=Only'), 'Only', 'unless the old marker says so');
  assert.equal(share('?text=fix%20this'), 'fix this');
  assert.equal(share('?url=https%3A%2F%2Fexample.com%2Fa'), 'https://example.com/a');
  assert.equal(share('?title=Docs&text=read%20this&url=https%3A%2F%2Fexample.com'), 'Docs read this https://example.com');
  assert.equal(share('?title=Docs&text=Docs&url=https%3A%2F%2Fexample.com'), 'Docs https://example.com', 'the same words are not repeated');
  assert.equal(share('?text=see%20https%3A%2F%2Fexample.com&url=https%3A%2F%2Fexample.com'), 'see https://example.com', 'the url inside the text is not repeated');
  assert.equal(share('?text=a%0A%0Ab%20%20c'), 'a b c', 'newlines would press Enter in the terminal: whitespace is flattened');
  assert.equal(share('?text=%20%20'), null);
});

test('mode send: the box holds the shared text, the list is the sessions that can take it (not the shell one), the peeked one first', () => {
  const { w } = world();
  mounted(w, `#/s/${S3}`);
  w.run("Palette.open({ mode: 'send', text: 'look at https://example.com' })");
  assert.equal(dlg(w).getAttribute('aria-label'), 'Send to a session');
  assert.equal(box(w).value, 'look at https://example.com');
  assert.deepEqual(groups(w), ['Send to session']);
  assert.deepEqual(labels(w), ['s3', 's2', 's1'], 'the open peek leads; the bash session is not offered');
  typeInto(w, 'look at this instead');
  assert.deepEqual(labels(w), ['s3', 's2', 's1'], 'typing edits the text, it does not filter');
  assert.match(dlg(w).querySelector('.pal-foot').textContent, /send/);
});

test('mode send: Enter sends the text with Enter and strips the share params; Shift+Enter only types it', async () => {
  const { w } = world({ search: '?text=hello%20there&demo=1' });
  mounted(w, '#/agents');
  w.run("Palette.open({ mode: 'send', text: 'hello there' })");
  keyInBox(w, 'ArrowDown');
  keyInBox(w, 'Enter');
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S1}/keys`, body: { text: 'hello there', enter: true } });
  assert.deepEqual(toasts(w).pop(), { text: 'sent "hello there" to s1', kind: 'ok' });
  assert.equal(dlg(w).open, false);
  const strip = plain(w.history.calls).filter((c) => c.method === 'replaceState').pop();
  assert.equal(strip.args[2], '/?demo=1#/agents', 'text is gone; demo=1 and the hash stay');
  w.run("Palette.open({ mode: 'send', text: 'plain note' })");
  keyInBox(w, 'Enter', { shiftKey: true });
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S2}/keys`, body: { text: 'plain note', enter: false } });
});

test('mode send: an empty box sends nothing; dismissing the dialog still strips the share from the address', () => {
  const { w } = world({ search: '?title=T&text=hi&url=https%3A%2F%2Fexample.com&keep=1' });
  mounted(w, '#/');
  w.run("Palette.open({ mode: 'send', text: 'hi' })");
  typeInto(w, '   ');
  const before = calls(w).length;
  keyInBox(w, 'Enter');
  assert.equal(calls(w).length, before, 'nothing was posted');
  assert.deepEqual(toasts(w).pop(), { text: 'Nothing to send.', kind: 'warn' });
  assert.equal(dlg(w).open, true, 'and the dialog stays');
  keyInBox(w, 'Escape');
  assert.equal(dlg(w).open, false);
  const strip = plain(w.history.calls).filter((c) => c.method === 'replaceState').pop();
  assert.equal(strip.args[2], '/?keep=1#/', 'title, text and url go; other params and the hash stay');
});

test('mode send with no session able to take text says so', () => {
  const st = fakeState();
  st.projects = [];
  const { w } = world({ state: st });
  mounted(w, '#/');
  w.run("Palette.open({ mode: 'send', text: 'x' })");
  assert.equal(items(w).length, 0);
  assert.match(dlg(w).querySelector('.pal-empty').textContent, /No session can take text/);
});

test('Router.stripShare removes text, title, url and share; keeps the hash and other params; does nothing when there is nothing to strip', () => {
  const { w } = world({ search: '?title=a&text=b&url=c&share=1&demo=1' });
  w.setHash('#/inbox', { silent: true });
  assert.equal(w.run('Router.stripShare()'), true);
  const last = plain(w.history.calls).pop();
  assert.equal(last.method, 'replaceState');
  assert.equal(last.args[2], '/?demo=1#/inbox');
  const n = w.history.calls.length;
  w.location.search = '?demo=1';
  assert.equal(w.run('Router.stripShare()'), false);
  w.location.search = '';
  assert.equal(w.run('Router.stripShare()'), false);
  assert.equal(w.history.calls.length, n, 'no replaceState without a share');
  w.location.search = '?text=x';
  w.run('Router.stripShare()');
  assert.equal(plain(w.history.calls).pop().args[2], '/#/inbox', 'a lone share leaves a bare address');
});

// ---------------------------------------------------------------- the help

test('openHelp lists the keyboard layer grouped, with the platform key names, and Esc / the close button end it', () => {
  const { w } = world({ platform: 'MacIntel' });
  mounted(w, '#/');
  w.run('Palette.openHelp()');
  assert.equal(dlg(w).open, true);
  assert.ok(dlg(w).classList.contains('palette') && dlg(w).classList.contains('help'));
  const heads = dlg(w).querySelectorAll('h3').map((n) => n.textContent);
  for (const h of ['General', 'Go to', 'Sessions', 'In the palette']) assert.ok(heads.includes(h), h);
  const rows = dlg(w).querySelectorAll('.help-row').map((n) => n.textContent);
  assert.ok(rows.some((r) => /⌘K/.test(r) && /palette/i.test(r)), 'the palette key, as ⌘K');
  assert.ok(rows.some((r) => /g.*then.*a/.test(r) && /Agents/.test(r)));
  assert.ok(rows.some((r) => /⌘1…9/.test(r)));
  dlg(w).querySelector('button').click();
  assert.equal(dlg(w).open, false);
});

test('openHelp focuses the dialog itself (tabindex -1), not its Close button: no cyan ring on open', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run('Palette.openHelp()');
  assert.equal(w.document.activeElement, dlg(w), 'the dialog holds the focus, like a sheet');
  assert.equal(dlg(w).getAttribute('tabindex'), '-1');
  assert.notEqual(w.document.activeElement.tagName, 'BUTTON');
  w.run('Palette.close()');
  w.run('Palette.open()');
  assert.equal(w.document.activeElement, dlg(w).querySelector('input.pal-input'), 'the palette still focuses its box');
});

test('Keymap.openHelp (what Settings asks for) opens the same dialog', () => {
  const { w } = world();
  mounted(w, '#/');
  assert.equal(w.run('Keymap.openHelp()'), true);
  assert.ok(dlg(w).classList.contains('help'));
});

// ---------------------------------------------------------------- live refresh

test('while open the palette follows the state: a new session appears without losing the text or the highlight', () => {
  const { w } = world();
  mounted(w, '#/');
  w.run("ui.lastJson = 'a'");
  w.run('Palette.open()');
  typeInto(w, 's');
  keyInBox(w, 'ArrowDown');
  const keep = selected(w)[0];
  const st = fakeState();
  st.projects[1].repos[0].sessions.push({ ...sess('s9', { state: 'working' }), tmux: 'blog--web--s9' });
  w.ctx.__st = st;
  w.run('state = __st; ui.lastJson = "b"; Palette.tick();');
  assert.equal(box(w).value, 's', 'the text stays');
  assert.ok(labels(w).includes('s9'));
  assert.equal(selected(w)[0], keep, 'so does the highlighted row');
  const n = items(w).length;
  w.run('Palette.tick();');
  assert.equal(items(w).length, n, 'an unchanged state is not redrawn');
  w.run('Palette.close()');
  w.run('Palette.tick()');
});

// ---------------------------------------------------------------- the keyboard layer on the real pages

test('Pages: j / k walk the Agents roster in screen order and paint the sel class; Esc clears', () => {
  const { w } = world();
  mounted(w, '#/agents');
  const rows = () => w.document.querySelectorAll('.rrow');
  const sel = () => rows().filter((r) => r.classList.contains('sel')).map((r) => r.getAttribute('data-tmux'));
  assert.deepEqual(rows().map((r) => r.getAttribute('data-tmux')), [S2, S1, S3, 'blog--web--sh']);
  assert.deepEqual(sel(), []);
  assert.equal(press(w, 'j').prevented, true);
  assert.deepEqual(sel(), [S2]);
  press(w, 'j'); press(w, 'j');
  assert.deepEqual(sel(), [S3]);
  press(w, 'k');
  assert.deepEqual(sel(), [S1]);
  for (let i = 0; i < 8; i++) press(w, 'k');
  assert.deepEqual(sel(), [S2], 'clamped at the top');
  for (let i = 0; i < 8; i++) press(w, 'j');
  assert.deepEqual(sel(), ['blog--web--sh'], 'and at the bottom');
  press(w, 'Escape');
  assert.deepEqual(sel(), []);
  assert.equal(w.get('ui').inboxSel, -1);
});

test('Pages: the selection follows its session when a poll reorders the roster, and moves on when the session ends', () => {
  const { w } = world();
  mounted(w, '#/agents');
  press(w, 'j'); press(w, 'j');                                   // s1
  assert.equal(w.run('Pages.selected().tmux'), S1);
  const st = fakeState();
  st.projects[0].repos[0].sessions[0].state = 'waiting';           // s1 now needs you too, and sorts differently
  st.projects[0].repos[0].sessions[0].needs_attention = true;
  st.projects[0].repos[0].sessions[0].state_at = ISO(1);
  w.ctx.__st = st;
  w.run('state = __st; repaintPage();');
  assert.equal(w.run('Pages.selected().tmux'), S1, 'the same session stays selected');
  const rows = w.document.querySelectorAll('.rrow').filter((r) => r.classList.contains('sel'));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].getAttribute('data-tmux'), S1);
  const gone = fakeState();
  gone.projects[0].repos[0].sessions = gone.projects[0].repos[0].sessions.filter((s) => s.name !== 's1');
  w.ctx.__st = gone;
  w.run('state = __st; repaintPage();');
  assert.ok(w.run('Pages.selected()'), 'the index stays: the next session takes the highlight');
});

test('Pages: j / k are left alone where there is no session list (Settings, Tasks)', () => {
  const { w } = world();
  mounted(w, '#/settings');
  assert.equal(press(w, 'j').prevented, false);
  mounted(w, '#/tasks');
  assert.equal(press(w, 'k').prevented, false);
  assert.equal(w.run('Pages.active()'), false);
});

test('Pages: Enter opens the selected session as the peek; o opens its terminal; a mouse click selects too', () => {
  const { w } = world();
  mounted(w, '#/agents');
  press(w, 'j'); press(w, 'j');
  press(w, 'Enter');
  assert.equal(w.location.hash, `#/s/${S1}`);
  assert.ok(sheet(w).querySelector('.peek[data-tmux=shop--api--s1]'));
  sheet(w).close();
  w.location.hash = '#/agents';
  w.document.querySelector(`.rrow[data-tmux=${S3}]`).click();
  assert.equal(w.run('Pages.selected().tmux'), S3);
  press(w, 'o');
  assert.deepEqual(plain(w.get('__opened')), [`/term/${S3}`]);
});

test('Pages: mod+1..9 open the nth session of the roster order; a missing nth session is left to the browser', () => {
  const { w } = world();
  mounted(w, '#/');
  assert.equal(press(w, '1', { ctrlKey: true }).prevented, true);
  assert.equal(w.location.hash, `#/s/${S2}`);
  press(w, '3', { ctrlKey: true });
  assert.equal(w.location.hash, `#/s/${S3}`);
  assert.equal(press(w, '9', { ctrlKey: true }).prevented, false);
  assert.equal(w.location.hash, `#/s/${S3}`);
});

test('Pages: a acknowledges, y allows and d denies the pending permission of the target; nothing without one', async () => {
  const { w } = world();
  mounted(w, '#/inbox');
  assert.deepEqual(w.document.querySelectorAll('.inbox-card').map((r) => r.getAttribute('data-tmux')), [S2]);
  press(w, 'j');
  assert.equal(w.run('Pages.targetPerm().id'), 7);
  press(w, 'y');
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/permission/7/allow' });
  press(w, 'd');
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: '/api/permission/7/deny' });
  press(w, 'a');
  await tick();
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S2}/ack` });
  assert.deepEqual(toasts(w).map((t) => t.text).slice(-3), ['allowed s2', 'denied s2', 'acknowledged s2']);
  mounted(w, '#/agents');
  press(w, 'j'); press(w, 'j');                                   // s1 has no permission and does not need attention
  const n = calls(w).length;
  press(w, 'y'); press(w, 'd'); press(w, 'a');
  await tick();
  assert.equal(calls(w).length, n, 'y and d are left alone without a permission to answer, and a session that needs nothing is not acknowledged');
  assert.deepEqual(toasts(w).pop(), { text: 's1 has nothing to acknowledge', kind: 'info' });
});

test('Pages: r focuses the peek send box, opening the peek first when needed', async () => {
  const { w } = world();
  mounted(w, `#/s/${S1}`);
  press(w, 'r');
  const send = sheet(w).querySelector('.peek-send textarea');
  assert.equal(active(w), send);
  sheet(w).close();
  w.location.hash = '#/agents';
  press(w, 'j');
  press(w, 'r');
  assert.equal(w.location.hash, `#/s/${S2}`, 'the peek opens for the selected row');
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(active(w), sheet(w).querySelector('.peek-send textarea'));
});

test('Pages: with the peek open over the sheet the peek keys still work, but the page keys do not', () => {
  const { w } = world();
  mounted(w, '#/agents');
  mounted(w, `#/s/${S3}`);
  assert.equal(sheet(w).open, true);
  assert.equal(press(w, 'j').prevented, false, 'j does not move a hidden list under a modal sheet');
  press(w, 'o');
  assert.deepEqual(plain(w.get('__opened')), [`/term/${S3}`], 'o acts on the peeked session');
});

test('Pages: a selection made before the peek opened does not beat it; one made after does', () => {
  const { w } = world({ wide: true });                          // the peek sits in the dock: no modal dialog, so every key is live
  mounted(w, '#/agents');
  press(w, 'j');                                                // s2 selected
  mounted(w, `#/s/${S3}`);                                      // the peek opens on s3 (a link click)
  assert.equal(w.run('Pages.target()'), S3, 'the peek wins over a stale selection');
  press(w, 'j');                                                // a new selection made with the peek open
  assert.equal(w.run('Pages.target()'), w.run('Pages.selected().tmux'));
});

test('Pages: mounting a list page starts with nothing selected', () => {
  const { w } = world();
  mounted(w, '#/agents');
  press(w, 'j');
  assert.ok(w.run('Pages.selected()'));
  mounted(w, '#/inbox');
  assert.equal(w.run('Pages.selected()'), null);
  mounted(w, '#/agents');
  assert.equal(w.run('Pages.selected()'), null);
});

// ---------------------------------------------------------------- skeleton and long lists

test('first paint: Home and Agents show three skeleton rows until the first state, which replaces them', () => {
  for (const [hash, page] of [['#/', 'home'], ['#/agents', 'agents']]) {
    const { w } = world();
    w.run('state = null');
    mounted(w, hash);
    const skel = w.document.querySelectorAll('#page .skel-rows .skel-row');
    assert.equal(skel.length, 3, `${page}: three skeleton rows`);
    assert.ok(w.document.querySelector('#page .skel-rows .skeleton'), 'the skeleton class maps to Blueprint through el()');
    assert.ok([...skel[0].querySelector('.skeleton').classList.contains ? ['bp5-skeleton'] : []].length, 'mapped');
    assert.equal(w.document.querySelectorAll('.rrow').length, 0, 'skeleton rows are not roster rows');
    if (page === 'agents') assert.ok(w.document.querySelectorAll('.nonideal').every((n) => n.classList.contains('hidden')), 'no "No live sessions" flash before the state');
    w.ctx.__st = fakeState();
    w.run('state = __st; updateCurrentPage(state);');
    assert.equal(w.document.querySelectorAll('#page .skel-rows').length, 0, `${page}: replaced by the first update`);
    if (page === 'agents') assert.equal(w.document.querySelectorAll('.rrow').length, 4);
  }
});

test('with the state already there nothing is skeletoned', () => {
  const { w } = world();
  mounted(w, '#/agents');
  assert.equal(w.document.querySelectorAll('#page .skel-rows').length, 0);
  mounted(w, '#/');
  assert.equal(w.document.querySelectorAll('#page .skel-rows').length, 0);
});

test('a skeleton node carries bp5-skeleton from core.js, and the table class carries two classes without throwing', () => {
  const { w } = world();
  const n = w.run("el('span', { class: 'skeleton skel-main', text: 'x' })");
  assert.ok(n.classList.contains('bp5-skeleton') && n.classList.contains('skel-main'));
  const t = w.run("el('table', { class: 'table' })");
  assert.ok(t.classList.contains('bp5-html-table') && t.classList.contains('bp5-compact'));
});

test('lists longer than 30 rows get cv-auto (the CSS class that skips layout off screen), shorter ones do not', () => {
  const many = [];
  for (let i = 1; i <= 32; i++) many.push({ ...sess('n' + i, { state: 'waiting', needs_attention: true, state_at: ISO(i) }), tmux: `big--repo--n${i}`, name: 'n' + i });
  const big = (n) => {
    const st = fakeState();
    st.projects = [{ name: 'big', path: '/srv/projects/big', root: null, orphan_sessions: [], repos: [{ name: 'repo', path: '/srv/projects/big/repo', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: many.slice(0, n) }] }];
    return st;
  };
  for (const [hash, sel] of [['#/agents', '.rg-list'], ['#/inbox', '.inbox-list']]) {
    let { w } = world({ state: big(31) });
    mounted(w, hash);
    assert.equal(w.document.querySelector(`#page ${sel}`).classList.contains('cv-auto'), true, `${hash}: 31 rows`);
    ({ w } = world({ state: big(30) }));
    mounted(w, hash);
    assert.equal(w.document.querySelector(`#page ${sel}`).classList.contains('cv-auto'), false, `${hash}: 30 rows is not long`);
  }
});

// ---------------------------------------------------------------- main.js

test('main.js: installs the keyboard layer before the shell, marks html.pwa, and offers a share once the first state is in', async () => {
  const { w } = world({ search: '?text=read%20this&url=https%3A%2F%2Fexample.com', keys: false });
  assert.equal(w.get('Keymap.installed'), false);
  w.run('poll = async () => {}; registerServiceWorker = () => {}; installLifecycleListeners = () => {};');
  w.run('globalThis.__order = []; { const real = Keymap.install; Keymap.install = function () { __order.push("keymap"); return real.apply(this, arguments); }; }');
  w.run('globalThis.installShell = () => { __order.push("shell"); };');
  w.run('globalThis.render = () => {}; globalThis.renderHeader = () => {}; globalThis.renderUsage = () => {};');
  w.load('main.js');
  await tick();
  assert.deepEqual(plain(w.get('__order')), ['keymap', 'shell']);
  assert.equal(w.get('Keymap.installed'), true);
  assert.equal(dlg(w).open, true, 'the share opened the palette');
  assert.equal(box(w).value, 'read this https://example.com');
  assert.deepEqual(groups(w), ['Send to session']);
});

test('main.js: with focus-existing the running app gets shares and shortcuts through launchQueue, once, without a navigation', async () => {
  const { w } = world({ keys: false });
  w.run('poll = async () => {}; registerServiceWorker = () => {}; installLifecycleListeners = () => {};');
  w.run('globalThis.launchQueue = { consumer: null, setConsumer(fn) { this.consumer = fn; } };');
  w.load('main.js');
  await tick();
  assert.equal(dlg(w).open, false);
  const launch = (url) => w.run(`launchQueue.consumer({ targetURL: ${JSON.stringify(url)} })`);
  launch('https://box/?title=Docs&text=read%20this&url=https%3A%2F%2Fexample.com%2F');
  assert.equal(dlg(w).open, true, 'the share opened the send palette');
  assert.equal(box(w).value, 'Docs read this https://example.com/');
  typeInto(w, 'edited by hand');
  launch('https://box/?title=Docs&text=read%20this&url=https%3A%2F%2Fexample.com%2F');
  assert.equal(box(w).value, 'edited by hand', 'the same share a moment later is the same launch: the edit stays');
  assert.equal(w.history.calls.filter((c) => c.method !== 'replaceState').length, 0, 'no pushState');
  keyInBox(w, 'Escape');
  w.run('const real = Date.now; Date.now = () => real() + 60000;');                 // a minute later the same page shared again is a new share
  launch('https://box/?title=Docs&text=read%20this&url=https%3A%2F%2Fexample.com%2F');
  assert.equal(dlg(w).open, true);
  assert.equal(box(w).value, 'Docs read this https://example.com/');
  keyInBox(w, 'Escape');
  mounted(w, '#/');
  launch('https://box/#/inbox');
  assert.equal(w.location.hash, '#/inbox', 'a manifest shortcut reuses the window and navigates it');
  launch('https://box/#/inbox');
  launch('https://box/#/not-a-route-but-fine');
  launch('not a url');
  assert.equal(dlg(w).open, false);
});

test('main.js: a fresh launch with a share hands it over twice (the address and launchQueue) and opens it once', async () => {
  const { w } = world({ search: '?text=hello', keys: false });
  w.run('poll = async () => {}; registerServiceWorker = () => {}; installLifecycleListeners = () => {};');
  w.run('globalThis.launchQueue = { consumer: null, setConsumer(fn) { this.consumer = fn; } };');
  w.load('main.js');
  w.run("launchQueue.consumer({ targetURL: 'https://box/?text=hello' })");
  typeInto(w, 'hello, but I typed more');
  await tick();                                                   // the first state arrives and main.js offers the address's share
  assert.equal(box(w).value, 'hello, but I typed more', 'the second offer of the same share did not reopen the palette over the edit');
});

test('main.js: no share, no palette; html.pwa follows display-mode and navigator.standalone', async () => {
  let w = world({ keys: false }).w;
  w.run('poll = async () => {}; registerServiceWorker = () => {}; installLifecycleListeners = () => {};');
  w.load('main.js');
  await tick();
  assert.equal(dlg(w).open, false);
  assert.equal(w.document.documentElement.classList.contains('pwa'), false, 'a plain browser tab');
  for (const [name, extra] of [['standalone', { matchMedia: (q) => ({ matches: /standalone/.test(q) }) }], ['overlay', { matchMedia: (q) => ({ matches: /window-controls-overlay/.test(q) }) }],
    ['iOS home screen', { navigator: { platform: 'iPad', standalone: true } }]]) {
    const world2 = makeWorld(extra);
    installDom(world2);
    for (const f of ['core.js', 'components.js', 'keymap.js', 'palette.js', 'router.js']) world2.load(f);
    world2.run('poll = async () => {}; registerServiceWorker = () => {}; installLifecycleListeners = () => {};');
    world2.load('main.js');
    assert.equal(world2.document.documentElement.classList.contains('pwa'), true, name);
  }
});
