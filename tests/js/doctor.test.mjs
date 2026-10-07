// Contract tests for v0.5.19, Settings > Doctor (pages/doctor.js drawn by pages/settings.js): the four sections (terminal folds into Box, memory into Claude), worst first inside each,
// the glyph / detail / fix text of a row, the three kinds of fix (text only, a command with Copy, a button for claude_login / codex_login / notify_test), Re-check (?refresh=1),
// the visibilitychange ask and its 5 s guard, the quiet "Doctor: n checks failing" line under the tabs, and that nothing opens Doctor by itself.
// Real scripts on minidom's DOM through tests/js/world.mjs; api() is the recorder (GET /api/doctor answers `doctor` below).
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { calls, fakeState, homeWorld, page, plain, text, tick } from './world.mjs';

const chk = (id, group, status, over = {}) => ({ id, group, label: `${id} label`, status, detail: `${id} detail`, fix: null, ...over });
const CHECKS = () => [
  chk('tmux', 'terminal', 'pass'),
  chk('ttyd', 'terminal', 'fail', { fix: { text: 'Start ttyd', cmd: 'systemctl --user start ttyd' } }),
  chk('code-server', 'box', 'warn', { fix: { text: 'Merge the settings' } }),
  chk('git', 'box', 'pass', { fix: { text: 'never shown on a pass', action: 'notify_test' } }),
  chk('gh', 'box', 'skip'),
  chk('claude-bin', 'claude', 'pass'),
  chk('claude-login', 'claude', 'fail', { fix: { text: 'Sign in', action: 'claude_login' } }),
  chk('memory-plugin', 'memory', 'warn', { fix: { text: 'Enable the plugin' } }),
  chk('codex-bin', 'codex', 'pass'),
  chk('codex-login', 'codex', 'warn', { fix: { text: 'Sign in to Codex', action: 'codex-login' } }),
  chk('ntfy', 'notify', 'warn', { fix: { text: 'Check ntfy', cmd: 'journalctl -u ntfy', action: 'notify_test' } }),
  chk('push', 'notify', 'fail', { fix: { text: 'Allow notifications', action: 'notify_test' } }),
];
const tally = (checks) => { const s = { pass: 0, warn: 0, fail: 0, skip: 0 }; for (const c of checks) s[c.status] += 1; return s; };
const doctor = (checks = CHECKS(), over = {}) => ({ generated_at: '2026-10-06T08:00:00+00:00', ok: !checks.some((c) => c.status === 'fail'), summary: tally(checks), checks, ...over });

const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('doctorStore.seq += 1'); } catch (_) { /* never mounted */ } } });

/** A world on `route` (the Doctor tab by default). GET /api/doctor answers __doctor (a value or a function); the recorder lists every call in __calls. */
function docWorld({ answer = doctor(), route = '#/settings?sec=doctor', state = fakeState() } = {}) {
  const { w } = homeWorld({ state });
  made.push(w);
  w.ctx.__doctor = answer;
  w.ctx.__copied = [];
  w.ctx.navigator.clipboard = { writeText: async (t) => { w.ctx.__copied.push(t); } };
  w.run(`__answers['/api/doctor'] = (req) => (typeof __doctor === 'function' ? __doctor(req) : __doctor);
         __answers['/api/notify/test'] = { ok: true }; __answers['/api/push/test'] = { sent: 1, subscriptions: 1 };`);
  w.location.hash = route;
  return w;
}
const mount = async (w) => { await tick(); await tick(); return w; };
const panel = (w) => page(w).querySelector('.settings-panel[data-sec=doctor]');
const secs = (w) => panel(w).querySelectorAll('.doc-sec');
const secOf = (w, id) => secs(w).find((s) => s.getAttribute('data-doc') === id);
const rowIds = (sec) => sec.querySelectorAll('.doc-row').map((r) => r.getAttribute('data-check'));
const rowOf = (w, id) => panel(w).querySelectorAll('.doc-row').find((r) => r.getAttribute('data-check') === id);
const btn = (root, label) => root.querySelectorAll('button').find((b) => text(b).trim() === label);
const gets = (w) => calls(w).filter((c) => c.path.startsWith('/api/doctor')).map((c) => c.path);
const line = (w) => page(w).querySelector('.settings-doc-line');
const age = (w, ms = 6000) => w.run(`doctorStore.at -= ${ms}`);
const toasts = (w) => plain(w.get('__toasts'));
const defer = () => { let resolve, reject; const p = new Promise((a, b) => { resolve = a; reject = b; }); return { p, resolve, reject }; };

// ---------------------------------------------------------------- the sections

test('four sections in order: Box, Claude, Codex, Notifications; terminal folds into Box and memory into Claude', async () => {
  const w = await mount(docWorld());
  assert.deepEqual(secs(w).map((s) => s.getAttribute('data-doc')), ['box', 'claude', 'codex', 'notify']);
  assert.deepEqual(secs(w).map((s) => s.getAttribute('aria-label')), ['Box', 'Claude', 'Codex', 'Notifications']);
  assert.deepEqual(secs(w).map((s) => text(s.querySelector('.set-h'))), ['Box', 'Claude', 'Codex', 'Notifications']);
  assert.deepEqual(rowIds(secOf(w, 'box')).sort(), ['code-server', 'gh', 'git', 'tmux', 'ttyd'], 'tmux and ttyd are terminal checks, shown under Box');
  assert.deepEqual(rowIds(secOf(w, 'claude')).sort(), ['claude-bin', 'claude-login', 'memory-plugin'], 'the claude-mem check is under Claude');
  assert.deepEqual(rowIds(secOf(w, 'codex')).sort(), ['codex-bin', 'codex-login']);
  assert.deepEqual(rowIds(secOf(w, 'notify')).sort(), ['ntfy', 'push']);
  assert.equal(page(w).querySelectorAll('.tab').find((t) => t.getAttribute('data-tab') === 'doctor').getAttribute('aria-selected'), 'true');
});

test('worst first inside a section: failing, then warnings, then passes, then skipped, the box\'s own order within a status', async () => {
  const w = await mount(docWorld());
  assert.deepEqual(rowIds(secOf(w, 'box')), ['ttyd', 'code-server', 'tmux', 'git', 'gh']);
  assert.deepEqual(rowIds(secOf(w, 'claude')), ['claude-login', 'memory-plugin', 'claude-bin']);
  assert.deepEqual(rowIds(secOf(w, 'notify')), ['push', 'ntfy']);
});

test('a section head counts what needs a look: "1 failing · 1 warning", "all passing"; a group the page does not know gets its own section after the four; an empty section is left out', async () => {
  const checks = [chk('git', 'box', 'pass'), chk('tmux', 'terminal', 'pass'), chk('ntfy', 'notify', 'warn'), chk('push', 'notify', 'fail'), chk('x1', 'future', 'warn'), chk('x2', 'future', 'warn')];
  const w = await mount(docWorld({ answer: doctor(checks) }));
  assert.deepEqual(secs(w).map((s) => s.getAttribute('data-doc')), ['box', 'notify', 'future'], 'no Claude or Codex checks: no such sections');
  assert.equal(text(secOf(w, 'box').querySelector('.doc-count')), 'all passing');
  assert.equal(text(secOf(w, 'notify').querySelector('.doc-count')), '1 failing · 1 warning');
  assert.equal(text(secOf(w, 'future').querySelector('.doc-count')), '2 warnings');
  assert.equal(text(secOf(w, 'future').querySelector('.set-h')), 'Future');
  assert.ok(secOf(w, 'notify').querySelector('.doc-count').classList.contains('bad'));
  assert.ok(secOf(w, 'future').querySelector('.doc-count').classList.contains('warn'));
});

// ---------------------------------------------------------------- a row

test('a row: a glyph that is not colour alone (✕ ! ✓ –, each with its word), the label, the detail, and the fix text for what is not passing', async () => {
  const w = await mount(docWorld());
  const glyph = (id) => { const g = rowOf(w, id).querySelector('.doc-glyph'); return [text(g), g.getAttribute('aria-label'), rowOf(w, id).getAttribute('data-status')]; };
  assert.deepEqual(glyph('ttyd'), ['✕', 'failing', 'fail']);
  assert.deepEqual(glyph('code-server'), ['!', 'warning', 'warn']);
  assert.deepEqual(glyph('tmux'), ['✓', 'passing', 'pass']);
  assert.deepEqual(glyph('gh'), ['–', 'skipped', 'skip']);
  const r = rowOf(w, 'ttyd');
  assert.equal(text(r.querySelector('.doc-label')), 'ttyd label');
  assert.equal(text(r.querySelector('.doc-detail')), 'ttyd detail');
  assert.equal(text(r.querySelector('.doc-fix')), 'Start ttyd');
  assert.equal(rowOf(w, 'git').querySelector('.doc-fix'), null, 'a passing check shows no fix, even when the box sent one');
  assert.equal(btn(rowOf(w, 'git'), 'Send test'), undefined, 'and no button');
});

test('a status the page does not know reads as a warning; a check without a label shows its id', async () => {
  const w = await mount(docWorld({ answer: doctor([chk('odd', 'box', 'maybe', { label: '' }), chk('git', 'box', 'pass')]) }));
  assert.equal(rowOf(w, 'odd').getAttribute('data-status'), 'warn');
  assert.equal(text(rowOf(w, 'odd').querySelector('.doc-label')), 'odd');
});

// ---------------------------------------------------------------- the three kinds of fix

test('fix kind 1, text only: the sentence and nothing to press', async () => {
  const w = await mount(docWorld());
  const r = rowOf(w, 'code-server');
  assert.equal(text(r.querySelector('.doc-fix')), 'Merge the settings');
  assert.equal(r.querySelector('.doc-cmd'), null);
  assert.equal(r.querySelectorAll('button').length, 0);
});

test('fix kind 2, a command: the text, the command in mono and a Copy button that puts exactly the command on the clipboard', async () => {
  const w = await mount(docWorld());
  const r = rowOf(w, 'ttyd');
  assert.equal(text(r.querySelector('code.doc-cmd')), 'systemctl --user start ttyd');
  const copy = btn(r, 'Copy');
  assert.ok(copy, 'Copy sits next to the command');
  assert.equal(copy.getAttribute('aria-label'), 'Copy the command');
  copy.click();
  await tick(); await tick();
  assert.deepEqual(plain(w.get('__copied')), ['systemctl --user start ttyd']);
  assert.equal(text(copy), 'Copied');
  assert.match(toasts(w).pop().text, /copied/i);
});

test('fix kind 3, an action button: claude_login and codex_login say Log in, notify_test says Send test; the hyphenated spelling counts; an unknown action shows its text and no button', async () => {
  const w = await mount(docWorld());
  assert.ok(btn(rowOf(w, 'claude-login'), 'Log in'));
  assert.ok(btn(rowOf(w, 'codex-login'), 'Log in'), 'codex-login (hyphen) is read as codex_login');
  assert.ok(btn(rowOf(w, 'push'), 'Send test'));
  const ntfy = rowOf(w, 'ntfy');
  assert.ok(btn(ntfy, 'Send test') && btn(ntfy, 'Copy'), 'a command and an action can sit on one row');
  assert.equal(btn(rowOf(w, 'push'), 'Send test').getAttribute('aria-label'), 'Send test: push label');
  const w2 = await mount(docWorld({ answer: doctor([chk('odd', 'box', 'warn', { fix: { text: 'Do the odd thing', action: 'format_disk' } }), chk('git', 'box', 'pass')]) }));
  assert.equal(text(rowOf(w2, 'odd').querySelector('.doc-fix')), 'Do the odd thing');
  assert.equal(rowOf(w2, 'odd').querySelectorAll('button').length, 0, 'an action the page does not know is ignored');
});

test('Log in leads to Settings > Accounts (Codex also asks for its account name there); Send test posts to the check\'s own channel, says what happened and asks again', async () => {
  const w = await mount(docWorld());
  btn(rowOf(w, 'claude-login'), 'Log in').click();
  assert.match(w.location.hash, /^#\/settings\?.*sec=accounts/);
  const w2 = await mount(docWorld());
  btn(rowOf(w2, 'codex-login'), 'Log in').click();
  assert.match(w2.location.hash, /sec=accounts/);
  assert.equal(w2.get('settingsPage.wantCx'), true, 'the Codex add block gets the cursor');

  const w3 = await mount(docWorld());
  btn(rowOf(w3, 'ntfy'), 'Send test').click();
  await tick(); await tick(); await tick();
  assert.deepEqual(calls(w3).filter((c) => c.method === 'POST').map((c) => c.path), ['/api/notify/test']);
  assert.match(toasts(w3).pop().text, /Test sent to ntfy/);
  assert.equal(gets(w3).pop(), '/api/doctor?refresh=1', 'the checklist is read again after the test');
  btn(rowOf(w3, 'push'), 'Send test').click();
  await tick(); await tick(); await tick();
  assert.deepEqual(calls(w3).filter((c) => c.method === 'POST').map((c) => c.path), ['/api/notify/test', '/api/push/test']);
  assert.match(toasts(w3).pop().text, /Test push sent/);
});

test('Send test with no ntfy or no subscription says so as a warning, and a request that fails says why', async () => {
  const w = await mount(docWorld());
  w.run(`__answers['/api/notify/test'] = { ok: false }; __answers['/api/push/test'] = () => { throw new Error('push is off'); };`);
  btn(rowOf(w, 'ntfy'), 'Send test').click();
  await tick(); await tick(); await tick();
  assert.deepEqual([toasts(w).pop().kind, toasts(w).pop().text], ['warn', 'ntfy did not take the test (is it running?)']);
  btn(rowOf(w, 'push'), 'Send test').click();
  await tick(); await tick(); await tick();
  assert.deepEqual([toasts(w).pop().kind, toasts(w).pop().text], ['bad', 'Could not send the test: push is off']);
});

// ---------------------------------------------------------------- Re-check and the visibility ask

test('opening the tab asks GET /api/doctor once (quietly, from the cache); Re-check asks with ?refresh=1, is off while it runs, and paints the new answer', async () => {
  const gate = defer();
  const w = docWorld({ answer: (req) => (req.path.includes('refresh') ? gate.p : doctor()) });
  await mount(w);
  assert.deepEqual(gets(w), ['/api/doctor']);
  assert.match(text(panel(w).querySelector('.doc-stamp')), /^Checked .* ago$/);
  const recheck = btn(panel(w), 'Re-check');
  recheck.click();
  await tick();
  assert.deepEqual(gets(w), ['/api/doctor', '/api/doctor?refresh=1']);
  const busy = btn(panel(w), 'Checking…');
  assert.ok(busy && (busy.disabled === true || busy.hasAttribute('disabled')), 'one ask at a time: the button is off while it runs');
  gate.resolve(doctor([chk('git', 'box', 'pass')], {}));
  await tick(); await tick();
  assert.deepEqual(secs(w).map((s) => s.getAttribute('data-doc')), ['box'], 'the new answer is on screen');
  assert.ok(btn(panel(w), 'Re-check'));
});

test('a failed ask keeps the last good answer on screen and says so; an empty checklist says "Press Re-check"; an answer that is not a checklist is called that', async () => {
  const w = await mount(docWorld());
  w.ctx.__doctor = () => { throw new Error('the board is busy'); };
  btn(panel(w), 'Re-check').click();
  await tick(); await tick();
  assert.match(text(panel(w).querySelector('.set-err')), /Could not run the checks: the board is busy/);
  assert.equal(secs(w).length, 4, 'the last good answer stays');
  const w2 = await mount(docWorld({ answer: doctor([]) }));
  assert.match(text(panel(w2)), /Nothing to show yet\. Press Re-check\./);
  const w3 = await mount(docWorld({ answer: { ok: true } }));
  assert.match(text(panel(w3).querySelector('.set-err')), /the board answered without a checklist/);
});

test('coming back to the tab asks again, at most once per 5 s: forced (?refresh=1) on Doctor and Agents, plain elsewhere; a hidden page asks nothing', async () => {
  const w = await mount(docWorld());
  assert.equal(gets(w).length, 1);
  w.document.dispatch('visibilitychange');
  await tick();
  assert.equal(gets(w).length, 1, 'the answer is 0 s old: not repeated');
  age(w, 4000);
  w.document.dispatch('visibilitychange');
  await tick();
  assert.equal(gets(w).length, 1, 'still inside 5 s');
  age(w, 2000);
  w.document.hidden = true;
  w.document.dispatch('visibilitychange');
  await tick();
  assert.equal(gets(w).length, 1, 'a hidden page does not ask');
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  await tick(); await tick();
  assert.deepEqual(gets(w), ['/api/doctor', '/api/doctor?refresh=1']);
  const w2 = await mount(docWorld({ route: '#/settings?sec=box' }));
  age(w2);
  w2.document.dispatch('visibilitychange');
  await tick();
  assert.deepEqual(gets(w2), ['/api/doctor', '/api/doctor'], 'on another tab the ask can use the server\'s cache');
});

test('leaving Settings stops the listener: a visibilitychange on another page asks nothing', async () => {
  const w = await mount(docWorld());
  w.location.hash = '#/';
  await tick();
  age(w);
  w.document.dispatch('visibilitychange');
  await tick();
  assert.equal(gets(w).length, 1);
});

// ---------------------------------------------------------------- the quiet line, and Doctor never opens itself

test('the quiet line under the Settings tabs: "Doctor: 3 checks failing" with a link to the Doctor tab, shown only while something fails and not on the Doctor tab itself', async () => {
  const w = await mount(docWorld({ route: '#/settings?sec=box' }));
  assert.equal(line(w).classList.contains('hidden'), false);
  assert.equal(text(line(w)), 'Doctor: 3 checks failing');
  assert.equal(line(w).querySelector('a').getAttribute('href'), '#/settings?sec=doctor');
  assert.equal(line(w).getAttribute('role'), 'status');
  page(w).querySelectorAll('.tab').find((t) => t.getAttribute('data-tab') === 'doctor').click();
  await tick();
  assert.equal(line(w).classList.contains('hidden'), true, 'on the Doctor tab the list says it already');
});

test('one failing check reads "1 check failing"; warnings alone and a clean box show no line at all', async () => {
  const w = await mount(docWorld({ route: '#/settings?sec=box', answer: doctor([chk('ttyd', 'terminal', 'fail'), chk('git', 'box', 'warn')]) }));
  assert.equal(text(line(w)), 'Doctor: 1 check failing');
  assert.equal(line(w).classList.contains('hidden'), false);
  const warnOnly = await mount(docWorld({ route: '#/settings?sec=box', answer: doctor([chk('git', 'box', 'warn'), chk('tmux', 'terminal', 'pass')]) }));
  assert.equal(line(warnOnly).classList.contains('hidden'), true);
  const clean = await mount(docWorld({ route: '#/settings?sec=box', answer: doctor([chk('git', 'box', 'pass')]) }));
  assert.equal(line(clean).classList.contains('hidden'), true);
  const failed = await mount(docWorld({ route: '#/settings?sec=box', answer: () => { throw new Error('down'); } }));
  assert.equal(line(failed).classList.contains('hidden'), true, 'no answer, no line');
});

test('Doctor never opens by itself: failing checks do not move the route, raise a toast or draw a banner, and Home never asks for the checklist', async () => {
  const w = docWorld({ route: '#/' });
  await mount(w);
  assert.equal(w.location.hash, '#/');
  assert.equal(gets(w).length, 0, 'only the Settings page reads the checklist');
  assert.equal(line(w), null);
  const s = docWorld({ route: '#/settings' });
  await mount(s);
  assert.equal(s.location.hash, '#/settings', 'Settings stays on its own tab');
  assert.equal(panel(s).classList.contains('hidden'), true);
  assert.equal(page(s).querySelectorAll('.tab').filter((t) => t.getAttribute('aria-selected') === 'true').map((t) => t.getAttribute('data-tab')).includes('doctor'), false);
  assert.equal(toasts(s).length, 0);
  assert.equal(text(line(s)), 'Doctor: 3 checks failing', 'the only trace is the quiet line');
});
