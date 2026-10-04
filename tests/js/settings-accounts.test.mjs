// Contract tests for v0.5.17c, the Settings screen of saved Claude logins: the row actions (Switch, Log in again, Forget login), the one-tap switch
// (accountSwitch in pages/agents.js: optimistic paint, the persisted "type continue" choice, rollback with the server's reason), the add-account block of
// Settings > Accounts (the sign-in link to copy, the code to paste, one-second polling for the result, Cancel, Try again) and the Log in buttons of
// Agents and Home that lead there. Real core.js, components.js, router.js and pages/*.js on minidom's DOM through tests/js/world.mjs; api() is the
// recorder from world.mjs, so every request is asserted as {method, path, body}.
import assert from 'node:assert/strict';
import { afterEach, mock, test } from 'node:test';
import { calls, fakeState, homeWorld, page, plain, setState, text, tick } from './world.mjs';

const A1 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001', A2 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a002', A3 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a003';
const secs = (n) => Math.floor(Date.now() / 1000) + n;
const acct = (key, over = {}) => ({ key, email: `${key.slice(-4)}@example.com`, name: null, label: null, plan: 'max', rl_5h: 10, rl_7d: 20, resets_5h: secs(7200), resets_7d: secs(200000), current: false, saved: true, ...over });
const THREE = () => [acct(A1, { email: 'demo@example.com', name: 'Demo', current: true }), acct(A2, { email: 'work@example.com', name: 'Work', label: 'Work', plan: 'pro' }), acct(A3, { email: 'old@example.com', name: 'Old', saved: false })];
const accountsOf = (list, store = { supported: true, reason: null, count: 2 }) => ({ current: (list.find((a) => a.current) || {}).key || null, list, store });
const LOGIN = (over = {}) => ({ running: false, url: null, tail: [], adding: false, email: null, started_at: null, result: null, ...over });
const URL1 = 'https://claude.ai/oauth/authorize?code=true&client_id=x&state=y';
const CODE = 'abc123_DEF-456.7~8#state123-XYZ';

const stateOf = (over = {}) => fakeState({ accounts: accountsOf(THREE()), login: LOGIN(), ...over });
const defer = () => { let resolve, reject; const p = new Promise((a, b) => { resolve = a; reject = b; }); return { p, resolve, reject }; };
const hidden = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const visibleButtons = (root) => root.querySelectorAll('button, a.btn').filter((b) => !hidden(b));
const isFilled = (b) => (b.classList.contains('primary') || b.classList.contains('bp5-intent-primary')) && !b.classList.contains('tinted');
const filled = (root) => visibleButtons(root).filter(isFilled).map(text);
const hasCls = (n, c) => n.classList.contains(c);
const off = (b) => b.disabled || b.hasAttribute('disabled');
const submitBtn = (w) => block(w).querySelector('.add-form button.primary');

const made = [];
const settle = () => { for (const w of made.splice(0)) { try { w.run('settingsPage.add && settingsPage.add.dispose()'); } catch (_) { /* a world that never mounted Settings */ } } };
afterEach(settle);                                                       // a real one-second timer would keep node alive for a minute
const fake = (on) => { if (on) mock.timers.enable({ apis: ['setInterval', 'setTimeout'] }); else { settle(); mock.timers.reset(); } };   // timers are mocked before the world is made; the blocks go before the mock does

/** The world on #/settings?sec=accounts. api() answers `answers` (path prefix -> value | fn({method, path, body})); poll() counts; Shell.patchUsage counts. */
function acctWorld({ state = stateOf(), answers = {}, route = '#/settings?sec=accounts' } = {}) {
  const { w } = homeWorld({ state });
  made.push(w);
  w.ctx.__polls = 0; w.ctx.__paints = 0; w.ctx.__copied = [];
  w.run('poll = async () => { __polls++; }; Shell.patchUsage = () => { __paints++; };');
  for (const [k, v] of Object.entries(answers)) w.ctx.__answers[k] = v;
  w.ctx.navigator.clipboard = { writeText: async (t) => { w.ctx.__copied.push(t); } };
  w.location.hash = route;
  return w;
}
const panel = (w) => page(w).querySelector('.settings-panel[data-sec=accounts]');
const rows = (w) => panel(w).querySelectorAll('.kv.set-acct');
const rowOf = (w, key) => rows(w).find((r) => r.getAttribute('data-account') === key);
const btn = (root, label) => root.querySelectorAll('button, a.btn').find((b) => text(b).trim() === label);
const labels = (row) => row.querySelectorAll('.kv-act button').map((b) => text(b));
const toasts = (w) => plain(w.get('__toasts'));
const block = (w) => panel(w).querySelector('.set-add');
const apiCalls = (w, prefix = '/api/accounts') => calls(w).filter((c) => c.path.startsWith(prefix));
const polls = (w) => w.get('__polls');
const submitForm = (w) => block(w).querySelector('.add-form').dispatchEvent({ type: 'submit', preventDefault() {} });
const withLogin = (w, login, over = {}) => { const st = { ...w.get('state'), login: LOGIN(login), ...over }; setState(w, st); };
const inFlight = (w, over = {}) => withLogin(w, { running: true, adding: true, url: URL1, tail: ['Opening browser to sign in…', 'Paste code here if prompted >'], ...over });

// ---------------------------------------------------------------- the rows

test('row actions per state: the account in use has Rename only; a saved one has Switch (primary tinted), Rename and a two-tap Forget login; an unsaved one has Log in again', () => {
  const w = acctWorld();
  const [cur, work, old] = [rowOf(w, A1), rowOf(w, A2), rowOf(w, A3)];
  assert.deepEqual(labels(cur), ['Rename'], 'no Switch and no Forget for the account in use');
  assert.deepEqual(labels(work), ['Switch', 'Rename', 'Forget login']);
  assert.deepEqual(labels(old), ['Log in again', 'Rename']);
  const sw = btn(work, 'Switch');
  assert.ok(hasCls(sw, 'primary') && hasCls(sw, 'tinted'), 'the repeated-row primary is the tinted one');
  assert.equal(sw.getAttribute('aria-label'), 'Switch to Work');
  const forget = btn(work, 'Forget login');
  assert.ok(hasCls(forget, 'danger'), 'destructive: red-outlined, never filled at rest');
  assert.equal(isFilled(forget), false);
  assert.equal(isFilled(btn(old, 'Log in again')), false, 'a quiet bordered button');
  assert.equal(isFilled(btn(work, 'Rename')), false);
  assert.deepEqual(cur.querySelectorAll('.badge').map(text), ['max', 'current', 'saved login']);
  assert.deepEqual(work.querySelectorAll('.badge').map(text), ['pro', 'saved login']);
  assert.ok(hasCls(work.querySelector('.badge.hue-slate'), 'badge'), 'the chip is slate');
  assert.deepEqual(old.querySelectorAll('.badge').map(text), ['max'], 'an unsaved account has no chip ...');
  assert.equal(text(old.querySelector('.set-chips .dim')), 'no saved login', '... but dim text that says so');
  assert.equal(visibleButtons(panel(w)).filter(isFilled).length, 0, 'idle with accounts: no filled primary on the screen');
});

test('where saved logins are not supported (macOS): no chip, Switch, Log in again, Forget or add buttons; one dim line with the reason and the /login how-to', () => {
  const reason = "saved logins need Claude's file credentials (Linux)";
  const w = acctWorld({ state: stateOf({ accounts: accountsOf(THREE(), { supported: false, reason, count: 0 }) }) });
  for (const r of rows(w)) assert.deepEqual(labels(r), ['Rename']);
  assert.equal(panel(w).querySelectorAll('.badge').filter((b) => /saved login/.test(text(b))).length, 0);
  assert.ok(!/no saved login/.test(text(panel(w))));
  assert.deepEqual(visibleButtons(panel(w)).map(text).sort(), ['Rename', 'Rename', 'Rename']);
  const off = panel(w).querySelector('.add-off');
  assert.equal(hidden(off), false);
  assert.match(text(off.querySelector('.add-reason')), /^Saved logins need Claude's file credentials \(Linux\)/);
  assert.match(text(off), /run \/login in any terminal/);
  assert.equal(hidden(panel(w).querySelector('.set-switching')), true, 'no switch choice either');
  assert.equal(hidden(panel(w).querySelector('.add-idle')), true);
  // a state without `store` (an older board) reads the same way
  const w2 = acctWorld({ state: stateOf({ accounts: { current: A1, list: THREE() } }) });
  assert.deepEqual(visibleButtons(panel(w2)).map(text), ['Rename', 'Rename', 'Rename']);
  assert.ok(!/undefined|NaN|null/.test(text(panel(w2))));
});

test('the recovery line is always under the list; the add how-to names both ways; the headings are labelled sections', () => {
  for (const accounts of [accountsOf(THREE()), accountsOf(THREE(), { supported: false, reason: 'x', count: 0 }), accountsOf([])]) {
    const w = acctWorld({ state: stateOf({ accounts }) });
    assert.match(text(panel(w)), /If anything looks wrong, run \/login in any terminal; the board records it\./);
  }
  const w = acctWorld();
  assert.deepEqual(panel(w).querySelectorAll('.set-h').map(text), ['Subscription accounts', 'When you switch', 'Add another subscription']);
  assert.match(text(block(w).querySelector('.add-idle')), /open the link, sign in, paste the code\. Running \/login in any terminal works too/);
  const kids = panel(w).children.map((n) => n.className.split(' ')[0]);
  assert.ok(kids.indexOf('set-acct-list') < kids.findIndex((c, i) => c === 'dim' && /If anything looks wrong/.test(text(panel(w).children[i]))), 'the recovery line follows the list');
});

// ---------------------------------------------------------------- the switch

test('Switch is one tap: the row is painted as in use at once with every button disabled, the POST carries continue_parked from the persisted choice, success takes the server accounts', async () => {
  const d = defer();
  const w = acctWorld({ answers: { '/api/accounts/': () => d.p } });
  btn(rowOf(w, A2), 'Switch').click();
  assert.deepEqual(rows(w).map((r) => !!r.querySelector('.badge.cur')), [false, true, false], 'painted before the answer');
  assert.equal(w.get('state').accounts.current, A2);
  assert.deepEqual(rows(w).flatMap((r) => r.querySelectorAll('button')).filter((b) => /Switch|Log in again|Forget/.test(text(b))).map(off), [true, true, true], 'buttons are disabled while the request runs');
  assert.deepEqual(apiCalls(w).filter((c) => c.method === 'POST'), [{ method: 'POST', path: `/api/accounts/${A2}/switch`, body: { continue_parked: true } }]);
  assert.ok(w.get('__paints') >= 1, 'the topbar chip repaints at once');
  btn(rowOf(w, A1), 'Switch').click();
  assert.equal(apiCalls(w).filter((c) => c.method === 'POST').length, 1, 'one switch at a time');
  const after = accountsOf([acct(A1, { email: 'demo@example.com', name: 'Demo' }), acct(A2, { email: 'work@example.com', name: 'Work', label: 'Work', plan: 'pro', current: true }), acct(A3, { saved: false })]);
  d.resolve({ ok: true, already: false, from: A1, to: A2, continued: [], accounts: after });
  await tick(); await tick();
  assert.equal(w.get('state').accounts, w.get('state').accounts);
  assert.deepEqual(plain(w.get('state').accounts), plain(after), 'the server\'s accounts are the state now');
  assert.deepEqual(rows(w).map((r) => !!r.querySelector('.badge.cur')), [false, true, false]);
  assert.deepEqual(toasts(w).pop(), { text: 'Switched to Work · running sessions follow within seconds', kind: 'ok' });
  assert.deepEqual(labels(rowOf(w, A1)), ['Switch', 'Rename', 'Forget login'], 'the old account is switchable now, enabled again');
  assert.equal(off(btn(rowOf(w, A1), 'Switch')), false);
  assert.ok(polls(w) >= 1, 'a fresh state is asked for');
});

test('the switch names the sessions that got `continue`, says when the account was already in use, and the Usage/topbar surfaces repaint', async () => {
  const w = acctWorld({ answers: { '/api/accounts/': { ok: true, already: false, continued: ['shop--api--s2', 'blog--web--s3'], accounts: accountsOf([acct(A1), acct(A2, { current: true, label: 'Work' }), acct(A3, { saved: false })]) } } });
  btn(rowOf(w, A2), 'Switch').click();
  await tick(); await tick();
  assert.equal(toasts(w).pop().text, 'Switched to Work · running sessions follow within seconds · continue typed in 2 sessions');
  const w1 = acctWorld({ answers: { '/api/accounts/': { ok: true, already: false, continued: ['shop--api--s2'], accounts: accountsOf([acct(A1), acct(A2, { current: true }), acct(A3, { saved: false })]) } } });
  btn(rowOf(w1, A2), 'Switch').click();
  await tick(); await tick();
  assert.match(toasts(w1).pop().text, / · continue typed in 1 session$/);
  const w2 = acctWorld({ answers: { '/api/accounts/': { ok: true, already: true, continued: [], accounts: accountsOf([acct(A1), acct(A2, { current: true, label: 'Work' }), acct(A3, { saved: false })]) } } });
  btn(rowOf(w2, A2), 'Switch').click();
  await tick(); await tick();
  assert.deepEqual(toasts(w2).pop(), { text: 'Work is already in use', kind: 'ok' });
  assert.ok(w.get('__paints') >= 2, 'Shell.patchUsage ran for the optimistic paint and for the answer');
});

test('a refused switch puts the old account back everywhere and shows the server reason in the panel and in a toast; nothing stays half-switched', async () => {
  const w = acctWorld({ answers: { '/api/accounts/': () => { const e = new Error('no saved login for that account'); e.status = 409; e.body = { detail: 'no saved login for that account', error: 'no saved login for that account' }; throw e; } } });
  const before = plain(w.get('state').accounts);
  btn(rowOf(w, A2), 'Switch').click();
  await tick(); await tick();
  assert.deepEqual(plain(w.get('state').accounts), before, 'every flag and `current` are back');
  assert.deepEqual(rows(w).map((r) => !!r.querySelector('.badge.cur')), [true, false, false]);
  assert.equal(off(btn(rowOf(w, A2), 'Switch')), false, 'tappable again');
  const err = panel(w).querySelector('.set-err');
  assert.equal(hidden(err), false);
  assert.equal(text(err), 'Switch failed: no saved login for that account');
  assert.deepEqual(toasts(w).pop(), { text: 'Switch failed: no saved login for that account', kind: 'bad' });
  // the next switch clears the line
  w.ctx.__answers['/api/accounts/'] = { ok: true, accounts: accountsOf([acct(A1), acct(A2, { current: true }), acct(A3, { saved: false })]) };
  btn(rowOf(w, A2), 'Switch').click();
  await tick(); await tick();
  assert.equal(hidden(panel(w).querySelector('.set-err')), true);
});

test('a poll that lands while the switch runs does not flip the rows back; a poll after a refusal shows the truth', async () => {
  const d = defer();
  const w = acctWorld({ answers: { '/api/accounts/': () => d.p } });
  btn(rowOf(w, A2), 'Switch').click();
  const stale = { ...stateOf() };                                       // the box still says A1 while the request runs
  w.ctx.__stale = stale;
  w.run('state = __stale; accountOverlay(state); updateCurrentPage(state)');
  assert.equal(w.get('state').accounts.current, A2, 'the overlay keeps the painted switch');
  assert.deepEqual(rows(w).map((r) => !!r.querySelector('.badge.cur')), [false, true, false]);
  d.reject(Object.assign(new Error('boom'), { status: 500 }));
  await tick(); await tick();
  w.ctx.__fresh = stateOf();
  w.run('state = __fresh; accountOverlay(state); updateCurrentPage(state)');
  assert.deepEqual(rows(w).map((r) => !!r.querySelector('.badge.cur')), [true, false, false], 'the hold ended with the request');
});

test('the "type continue" choice: on by default, a checkbox in the panel, persisted in localStorage ccboard:acct:continue; off sends continue_parked false; unusable storage still works', async () => {
  const w = acctWorld({ answers: { '/api/accounts/': { ok: true, continued: [] } } });
  const cb = panel(w).querySelector('.set-check input');
  assert.equal(cb.checked, true, 'on by default');
  assert.match(text(panel(w).querySelector('.set-check')), /After a switch, type continue in sessions parked on a limit/);
  cb.checked = false;
  cb.dispatchEvent({ type: 'change' });
  assert.equal(w.localStorage.getItem('ccboard:acct:continue'), '0');
  btn(rowOf(w, A2), 'Switch').click();
  await tick(); await tick();
  assert.deepEqual(apiCalls(w).filter((c) => c.method === 'POST').pop(), { method: 'POST', path: `/api/accounts/${A2}/switch`, body: { continue_parked: false } });
  // a fresh page: the saved choice is the checkbox
  w.location.hash = '#/settings?sec=box';
  w.location.hash = '#/settings?sec=accounts';
  w.run('updateCurrentPage(state)');
  assert.equal(panel(w).querySelector('.set-check input').checked, false);
  w.localStorage.setItem('ccboard:acct:continue', '1');
  assert.equal(w.run('acctContinuePref()'), true);
  w.localStorage.removeItem('ccboard:acct:continue');
  assert.equal(w.run('acctContinuePref()'), true, 'unset means on');
  // storage that throws: the choice lasts in memory and nothing breaks
  const w2 = acctWorld({ answers: { '/api/accounts/': { ok: true, continued: [] } } });
  w2.ctx.localStorage = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  assert.equal(w2.run('acctContinuePref()'), true);
  w2.run('acctContinueSet(false)');
  assert.equal(w2.run('acctContinuePref()'), false);
});

// ---------------------------------------------------------------- Forget login

test('Forget login takes two taps (Confirm + Cancel appear first), then DELETEs .../saved and the row says "no saved login"', async () => {
  const after = accountsOf([acct(A1, { current: true }), acct(A2, { label: 'Work', saved: false }), acct(A3, { saved: false })], { supported: true, reason: null, count: 1 });
  const w = acctWorld({ answers: { '/api/accounts/': { ok: true, accounts: after } } });
  btn(rowOf(w, A2), 'Forget login').click();
  assert.equal(apiCalls(w).filter((c) => c.method === 'DELETE').length, 0, 'the first tap only arms it');
  assert.deepEqual(labels(rowOf(w, A2)).filter((l) => /Confirm|Cancel/.test(l)), ['Confirm Forget login', 'Cancel']);
  btn(rowOf(w, A2), 'Cancel').click();
  assert.deepEqual(labels(rowOf(w, A2)), ['Switch', 'Rename', 'Forget login'], 'Cancel disarms');
  btn(rowOf(w, A2), 'Forget login').click();
  btn(rowOf(w, A2), 'Confirm Forget login').click();
  await tick(); await tick();
  assert.deepEqual(apiCalls(w).filter((c) => c.method === 'DELETE'), [{ method: 'DELETE', path: `/api/accounts/${A2}/saved` }]);
  assert.equal(text(rowOf(w, A2).querySelector('.set-chips .dim')), 'no saved login');
  assert.deepEqual(labels(rowOf(w, A2)), ['Log in again', 'Rename']);
  assert.deepEqual(toasts(w).pop(), { text: 'Forgot the saved login of Work', kind: 'ok' });
});

test('a refused Forget (409, the account in use) says why in the panel and changes nothing', async () => {
  const w = acctWorld({ answers: { '/api/accounts/': () => { const e = new Error('that login is the one in use'); e.body = { detail: 'that login is the one in use' }; throw e; } } });
  btn(rowOf(w, A2), 'Forget login').click();
  btn(rowOf(w, A2), 'Confirm Forget login').click();
  await tick(); await tick();
  assert.match(text(panel(w).querySelector('.set-err')), /that login is the one in use/);
  assert.deepEqual(labels(rowOf(w, A2)), ['Switch', 'Rename', 'Forget login']);
});

// ---------------------------------------------------------------- the add-account block

test('idle: Add account is a bordered default button while someone is signed in; with an empty list or nobody signed in it is the one filled primary and reads Log in', () => {
  const w = acctWorld();
  const idle = block(w).querySelector('.add-idle');
  assert.equal(hidden(idle), false);
  assert.deepEqual(visibleButtons(idle).map(text), ['Add account']);
  assert.equal(isFilled(visibleButtons(idle)[0]), false);
  for (const st of [stateOf({ accounts: accountsOf([]) }), stateOf({ accounts: accountsOf([acct(A1, { saved: false })]) , claude: { installed: true, loggedIn: false } })]) {
    const w2 = acctWorld({ state: st });
    const b = visibleButtons(block(w2).querySelector('.add-idle'));
    assert.deepEqual(b.map(text), ['Log in']);
    assert.ok(isFilled(b[0]));
    assert.deepEqual(filled(panel(w2)), ['Log in'], 'one filled primary');
  }
});

test('Add account starts the login (POST /api/accounts/login), shows "starting the login…" with every control disabled until the url arrives, then the two steps', async () => {
  const w = acctWorld();
  btn(block(w), 'Add account').click();
  await tick();
  assert.deepEqual(apiCalls(w).filter((c) => c.method === 'POST'), [{ method: 'POST', path: '/api/accounts/login', body: {} }]);
  const b = block(w);
  assert.equal(hidden(b.querySelector('.add-flight')), false);
  assert.equal(hidden(b.querySelector('.add-idle')), true);
  assert.equal(hidden(b.querySelector('.add-wait')), false);
  assert.equal(text(b.querySelector('.add-wait')), 'starting the login…');
  const url = b.querySelector('.add-url'), code = b.querySelector('.add-code'), copy = btn(b, 'Copy link'), open = btn(b, 'Open'), add = submitBtn(w);
  assert.equal(copy.disabled, true);
  assert.equal(code.disabled, true);
  assert.equal(add.disabled, true);
  assert.equal(open.getAttribute('aria-disabled'), 'true');
  assert.equal(open.getAttribute('href'), null);
  assert.equal(url.value, '');
  inFlight(w);
  assert.equal(hidden(b.querySelector('.add-wait')), true);
  assert.equal(url.value, URL1);
  assert.equal(url.getAttribute('readonly'), '', 'read-only');
  assert.equal(copy.disabled, false);
  assert.equal(code.disabled, false);
  assert.equal(add.disabled, false);
  assert.equal(open.getAttribute('href'), URL1);
  assert.equal(open.getAttribute('target'), '_blank');
  assert.equal(open.getAttribute('rel'), 'noopener');
  assert.equal(open.getAttribute('aria-disabled'), null);
  assert.deepEqual(b.querySelectorAll('.add-k').map(text), ['Step 1', 'Step 2']);
  assert.match(text(b), /Open this link and sign in with the account you want to add\./);
  assert.match(text(b), /Paste the code the page shows\./);
  assert.deepEqual(filled(panel(w)), ['Add account'], 'Add account in step 2 is the one filled primary');
  assert.deepEqual(['autocomplete', 'autocapitalize', 'spellcheck'].map((k) => code.getAttribute(k)), ['off', 'off', 'false']);
  assert.equal(b.querySelector('.add-tail').textContent, 'Opening browser to sign in…\nPaste code here if prompted >', 'the terminal output, one line per row');
  const out = b.querySelector('details.add-out');
  assert.equal(out.open, false, 'closed until asked for');
  assert.equal(out.querySelector('a').getAttribute('href'), '/term/_ccboard-login');
  assert.equal(out.querySelector('a').getAttribute('target'), '_blank');
});

test('an unsafe link is shown but never made a link; an https one is', () => {
  const w = acctWorld();
  btn(block(w), 'Add account').click();
  inFlight(w, { url: 'javascript:alert(1)' });
  assert.equal(btn(block(w), 'Open').getAttribute('href'), null);
  assert.equal(btn(block(w), 'Open').getAttribute('aria-disabled'), 'true');
});

test('Copy link: the clipboard API first, then selecting the field and execCommand, then the failure message; the field selects itself on focus', async () => {
  const w = acctWorld();
  btn(block(w), 'Add account').click();
  inFlight(w);
  const b = block(w);
  const url = b.querySelector('.add-url');
  let selected = 0;
  url.select = () => { selected++; };
  url.dispatchEvent({ type: 'focus' });
  assert.equal(selected, 1, 'a tap on the field selects the whole link');
  btn(b, 'Copy link').click();
  await tick();
  assert.deepEqual(plain(w.get('__copied')), [URL1]);
  assert.deepEqual(toasts(w).pop(), { text: 'Link copied', kind: 'ok' });
  // no clipboard API: select the field and execCommand('copy')
  w.ctx.navigator.clipboard = { writeText: async () => { throw new Error('denied'); } };
  const cmds = [];
  w.document.execCommand = (c) => { cmds.push(c); return true; };
  btn(b, 'Copy link').click();
  await tick();
  assert.deepEqual(cmds, ['copy']);
  assert.equal(selected, 2, 'the field was selected for the fallback');
  assert.deepEqual(toasts(w).pop(), { text: 'Link copied', kind: 'ok' });
  w.document.execCommand = () => false;
  btn(b, 'Copy link').click();
  await tick();
  assert.deepEqual(toasts(w).pop(), { text: 'Copy failed: select the link and copy it', kind: 'warn' });
  delete w.document.execCommand;
  btn(b, 'Copy link').click();
  await tick();
  assert.equal(toasts(w).pop().kind, 'warn', 'no execCommand at all: the same advice');
});

test('the code is checked the way the server checks it before anything is sent; a bad one says what is wrong under the field and keeps what was typed', async () => {
  const w = acctWorld();
  btn(block(w), 'Add account').click();
  inFlight(w);
  const b = block(w);
  const code = b.querySelector('.add-code');
  for (const bad of ['', 'nohash', 'a#', '#b', 'a b#c', 'a#b#c', 'x'.repeat(257) + '#y']) {
    code.value = bad;
    submitForm(w);
    assert.equal(text(b.querySelector('.field-err')), "paste the whole code shown by the browser, including the part after '#'", JSON.stringify(bad.slice(0, 12)));
  }
  assert.equal(apiCalls(w, '/api/accounts/login/code').length, 0, 'nothing was sent');
  assert.equal(code.value, 'x'.repeat(257) + '#y');
  code.value = '  ' + CODE + '  ';
  submitForm(w);
  await tick();
  assert.deepEqual(apiCalls(w, '/api/accounts/login/code'), [{ method: 'POST', path: '/api/accounts/login/code', body: { code: CODE } }], 'trimmed, then sent');
  assert.equal(text(b.querySelector('.field-err')), '');
});

test('sending the code: "Checking the code…", the field is emptied and locked, and the box is asked for the result every second; a refusal of the code is shown under the field', async () => {
  fake(true);
  try {
    const w = acctWorld({ answers: { '/api/accounts/login/code': () => { const e = new Error('no login is in progress'); e.status = 409; e.body = { detail: 'no login is in progress' }; throw e; } } });
    btn(block(w), 'Add account').click();
    inFlight(w);
    const b = block(w);
    const code = b.querySelector('.add-code');
    code.value = CODE;
    submitForm(w);
    await tick();
    assert.equal(text(b.querySelector('.field-err')), 'no login is in progress');
    assert.equal(code.value, CODE, 'a refused code stays for another try');
    assert.equal(code.disabled, false);
    assert.equal(w.run('settingsPage.add.timers'), 0, 'no polling for a code that was not accepted');
    w.ctx.__answers['/api/accounts/login/code'] = { ok: true };
    submitForm(w);
    await tick();
    assert.equal(text(b.querySelector('.add-status')), 'Checking the code…');
    assert.equal(hidden(b.querySelector('.add-status')), false);
    assert.equal(code.value, '', 'the code does not stay in the page');
    assert.equal(code.disabled, true);
    assert.equal(submitBtn(w).disabled, true);
    const base = polls(w);
    mock.timers.tick(3000);
    assert.equal(polls(w) - base, 3, 'poll(true) once a second');
    assert.equal(w.run('settingsPage.add.timers'), 1);
  } finally { fake(false); }
});

test('the one-second polling stops after 60 seconds, on unmount and when the result arrives; there is never more than one timer', async () => {
  fake(true);
  try {
    const w = acctWorld({ answers: { '/api/accounts/login/code': { ok: true } } });
    btn(block(w), 'Add account').click();
    inFlight(w);
    const send = () => { block(w).querySelector('.add-code').value = CODE; submitForm(w); };
    send();
    await tick();
    send();                                                              // a second submit while checking: nothing new
    await tick();
    assert.equal(apiCalls(w, '/api/accounts/login/code').length, 1);
    const base = polls(w);
    mock.timers.tick(59000);
    assert.equal(polls(w) - base, 59);
    mock.timers.tick(1000);
    assert.equal(polls(w) - base, 60);
    mock.timers.tick(10000);
    assert.equal(polls(w) - base, 60, 'stopped at a minute');
    assert.equal(w.run('settingsPage.add.timers'), 0);
    assert.match(text(block(w).querySelector('.add-status')), /Still checking the code/);
    // result arrives while checking: the timer goes
    const w2 = acctWorld({ answers: { '/api/accounts/login/code': { ok: true } } });
    btn(block(w2), 'Add account').click();
    inFlight(w2);
    block(w2).querySelector('.add-code').value = CODE;
    submitForm(w2);
    await tick();
    assert.equal(w2.run('settingsPage.add.timers'), 1);
    withLogin(w2, { result: { ok: true, key: A3, name: 'Old', live: false, at: '2026-10-04T10:00:00Z' } });
    assert.equal(w2.run('settingsPage.add.timers'), 0);
    const b2 = polls(w2);
    mock.timers.tick(5000);
    assert.equal(polls(w2), b2, 'no more polling once the result is in');
    // unmount while checking
    const w3 = acctWorld({ answers: { '/api/accounts/login/code': { ok: true } } });
    btn(block(w3), 'Add account').click();
    inFlight(w3);
    block(w3).querySelector('.add-code').value = CODE;
    submitForm(w3);
    await tick();
    w3.location.hash = '#/agents';
    const b3 = polls(w3);
    mock.timers.tick(5000);
    assert.equal(polls(w3), b3, 'leaving the page stops it');
  } finally { fake(false); }
});

test('a successful result: toast "Added <name>" (+ " · it is the account in use now"), the block is idle again, the row is in the list; announced once per result.at', async () => {
  const w = acctWorld({ answers: { '/api/accounts/login/code': { ok: true } } });
  btn(block(w), 'Add account').click();
  inFlight(w);
  block(w).querySelector('.add-code').value = CODE;
  submitForm(w);
  await tick();
  const NEW = acct('7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a009', { email: 'new@example.com', name: 'Newbie', plan: 'pro' });
  const done = { ok: true, key: NEW.key, name: 'Newbie', live: false, at: '2026-10-04T10:00:00Z' };
  setState(w, { ...w.get('state'), accounts: accountsOf([...THREE(), NEW]), login: LOGIN({ result: done }) });
  assert.equal(toasts(w).filter((t) => /^Added/.test(t.text)).length, 1);
  assert.deepEqual(toasts(w).pop(), { text: 'Added Newbie', kind: 'ok' });
  assert.equal(hidden(block(w).querySelector('.add-idle')), false, 'idle again');
  assert.equal(hidden(block(w).querySelector('.add-flight')), true);
  assert.equal(rows(w).length, 4, 'the new row is in the list');
  assert.equal(text(rowOf(w, NEW.key).querySelector('.k')), 'Newbie');
  for (let i = 0; i < 3; i++) setState(w, { ...w.get('state'), login: LOGIN({ result: done }) });
  assert.equal(toasts(w).filter((t) => /^Added/.test(t.text)).length, 1, 'not again on every poll');
  // a second login, live this time
  btn(block(w), 'Add account').click();
  inFlight(w);
  block(w).querySelector('.add-code').value = CODE;
  submitForm(w);
  await tick();
  withLogin(w, { result: { ok: true, key: NEW.key, name: 'Newbie', live: true, at: '2026-10-04T10:05:00Z' } });
  assert.equal(toasts(w).pop().text, 'Added Newbie · it is the account in use now');
});

test('a result nobody here waited for is not announced (a reload within its ten minutes, a login started elsewhere)', () => {
  const w = acctWorld({ state: stateOf({ login: LOGIN({ result: { ok: true, key: A3, name: 'Old', live: false, at: '2026-10-04T09:50:00Z' } }) }) });
  assert.equal(toasts(w).length, 0);
  assert.equal(hidden(block(w).querySelector('.add-error')), true);
  w.location.hash = '#/settings?sec=box';
  w.location.hash = '#/settings?sec=accounts';
  assert.equal(toasts(w).length, 0, 'and not on a remount either');
});

test('a failed login shows the reason in the block with Try again (the one filled primary), which restarts it with {restart: true, email}; Dismiss goes back to idle', async () => {
  const w = acctWorld({ answers: { '/api/accounts/login/code': { ok: true } } });
  btn(panel(w), 'Log in again').click();
  await tick();
  assert.deepEqual(apiCalls(w).filter((c) => c.method === 'POST').pop(), { method: 'POST', path: '/api/accounts/login', body: { email: 'old@example.com' } });
  inFlight(w, { email: 'old@example.com' });
  block(w).querySelector('.add-code').value = CODE;
  submitForm(w);
  await tick();
  withLogin(w, { result: { ok: false, error: 'the login did not complete', at: '2026-10-04T10:00:00Z' } });
  const failed = block(w).querySelector('.add-error');
  assert.equal(hidden(failed), false);
  assert.equal(text(failed.querySelector('.add-err')), 'the login did not complete');
  assert.deepEqual(filled(panel(w)), ['Try again']);
  assert.equal(hidden(block(w).querySelector('.add-flight')), true);
  assert.equal(toasts(w).filter((t) => /^Added/.test(t.text)).length, 0);
  btn(failed, 'Try again').click();
  await tick();
  assert.deepEqual(apiCalls(w).filter((c) => c.method === 'POST').pop(), { method: 'POST', path: '/api/accounts/login', body: { email: 'old@example.com', restart: true } });
  assert.equal(hidden(block(w).querySelector('.add-error')), true, 'the error leaves while the new login starts');
  assert.equal(hidden(block(w).querySelector('.add-flight')), false);
  // a refused start (409 already running) lands in the same place, and Dismiss clears it
  const w2 = acctWorld({ answers: { '/api/accounts/login': () => { const e = new Error('a login is already running'); e.body = { detail: 'a login is already running' }; throw e; } } });
  btn(block(w2), 'Add account').click();
  await tick();
  assert.equal(text(block(w2).querySelector('.add-err')), 'a login is already running');
  assert.deepEqual(filled(panel(w2)), ['Try again']);
  btn(block(w2), 'Dismiss').click();
  assert.equal(hidden(block(w2).querySelector('.add-idle')), false);
});

test('Cancel sends DELETE /api/accounts/login, empties the code and returns to idle even while the poll still says `adding`', async () => {
  const w = acctWorld();
  btn(block(w), 'Add account').click();
  inFlight(w);
  const code = block(w).querySelector('.add-code');
  code.value = 'half-typed#';
  btn(block(w), 'Cancel').click();
  await tick();
  assert.deepEqual(apiCalls(w).filter((c) => c.method === 'DELETE'), [{ method: 'DELETE', path: '/api/accounts/login' }]);
  assert.equal(code.value, '');
  assert.equal(hidden(block(w).querySelector('.add-idle')), false);
  inFlight(w);                                                          // a poll that was already on its way
  assert.equal(hidden(block(w).querySelector('.add-flight')), true, 'the stale `adding` does not bring the steps back');
  withLogin(w, { adding: false });                                      // the box caught up
  inFlight(w);                                                          // a later login (from a terminal, say) shows again
  assert.equal(hidden(block(w).querySelector('.add-flight')), false);
});

test('the block\'s inputs are THE SAME nodes after state updates while they hold text, and the typed code is still there', () => {
  const w = acctWorld();
  btn(block(w), 'Add account').click();
  inFlight(w);
  const code = block(w).querySelector('.add-code');
  const url = block(w).querySelector('.add-url');
  const root = block(w);
  code.value = 'half-typed-code#';
  code.focus();
  const renamed = THREE();
  renamed[1].label = 'Client work';
  setState(w, { ...w.get('state'), accounts: accountsOf(renamed) });         // the rows are rebuilt
  const moved = THREE();
  moved[0].current = false; moved[1].current = true; moved[1].label = 'Client work';
  setState(w, { ...w.get('state'), accounts: accountsOf(moved), login: LOGIN({ running: true, adding: true, url: URL1, tail: ['more output'] }) });
  assert.equal(text(rows(w)[1].querySelector('.k')), 'Client work', 'the list did change');
  assert.equal(block(w), root, 'the block');
  assert.equal(block(w).querySelector('.add-code'), code, 'the code input');
  assert.equal(block(w).querySelector('.add-url'), url, 'the link input');
  assert.equal(code.value, 'half-typed-code#');
  assert.equal(code.isConnected, true);
  assert.equal(w.document.activeElement, code, 'and it keeps the focus');
  assert.equal(block(w).querySelector('.add-tail').textContent, 'more output');
});

test('exactly one filled primary on the screen in every state of the add flow (none only in the idle state with accounts)', async () => {
  const w = acctWorld({ answers: { '/api/accounts/login/code': { ok: true } } });
  assert.deepEqual(filled(panel(w)), []);                                    // idle, someone signed in
  btn(block(w), 'Add account').click();
  assert.deepEqual(filled(panel(w)), ['Add account'], 'starting: the steps show, their Add account is disabled until the link arrives');
  assert.equal(submitBtn(w).disabled, true);
  inFlight(w);
  assert.deepEqual(filled(panel(w)), ['Add account']);
  block(w).querySelector('.add-code').value = CODE;
  submitForm(w);
  await tick();
  assert.ok(filled(panel(w)).length <= 1, 'checking');
  withLogin(w, { result: { ok: false, error: 'nope', at: 'a' } });
  assert.deepEqual(filled(panel(w)), ['Try again']);
  const w2 = acctWorld({ state: stateOf({ accounts: accountsOf([]) }) });
  assert.deepEqual(filled(panel(w2)), ['Log in']);
});

// ---------------------------------------------------------------- the Log in buttons lead to the Accounts section

test('Settings > Agents: Log in goes to Settings > Accounts and starts the add flow there (no modal, no /api/claude/login)', async () => {
  const w = acctWorld({ state: stateOf({ claude: { installed: true, loggedIn: false }, accounts: accountsOf([]) }), route: '#/settings?sec=agents' });
  const agents = page(w).querySelector('.settings-panel[data-sec=agents]');
  const login = agents.querySelectorAll('button').find((b) => text(b) === 'Log in');
  assert.ok(login, 'the Log in button is there');
  assert.ok(!agents.querySelectorAll('button').some((b) => /in progress/.test(text(b))), 'no "Login in progress…" button any more');
  login.click();
  await tick();
  assert.equal(w.location.hash, '#/settings?sec=accounts');
  assert.deepEqual(apiCalls(w, '/api/').map((c) => `${c.method} ${c.path}`).filter((s) => /login/.test(s)), ['POST /api/accounts/login'], 'one POST, to the account flow');
  assert.equal(hidden(block(w).querySelector('.add-flight')), false, 'the steps are showing');
  assert.equal(w.get('ui.modal'), false);
});

test('Home\'s "not logged in" banner button does the same: Settings > Accounts, flow started', async () => {
  const w = acctWorld({ state: stateOf({ claude: { installed: true, loggedIn: false }, accounts: accountsOf([]) }), route: '#/' });
  w.run('renderBanner()');
  const login = w.document.querySelector('#banner').querySelectorAll('button').find((b) => text(b) === 'Log in');
  assert.ok(login, 'the banner has Log in');
  login.click();
  await tick();
  assert.equal(w.location.hash, '#/settings?sec=accounts');
  assert.deepEqual(apiCalls(w).filter((c) => c.method === 'POST').map((c) => c.path), ['/api/accounts/login']);
  assert.equal(hidden(block(w).querySelector('.add-flight')), false);
});

test('where saved logins are not supported the Log in buttons still lead to the Accounts section (the /login how-to), and start nothing', async () => {
  const reason = "saved logins need Claude's file credentials (Linux)";
  const w = acctWorld({ state: stateOf({ claude: { installed: true, loggedIn: false }, accounts: accountsOf([], { supported: false, reason, count: 0 }) }), route: '#/' });
  w.run('renderBanner()');
  w.document.querySelector('#banner').querySelectorAll('button').find((b) => text(b) === 'Log in').click();
  await tick();
  assert.equal(w.location.hash, '#/settings?sec=accounts');
  assert.equal(apiCalls(w).filter((c) => c.method === 'POST').length, 0);
  assert.match(text(panel(w)), /run \/login in any terminal/);
  assert.equal(w.get('acctFlow.want'), null, 'the wish is not kept for later');
});

test('the old modal login is gone: no startLogin / openModal / updateModal / modalParts, the legacy #modal and closeModal stay for the task dialog', () => {
  const { w } = homeWorld();
  for (const name of ['startLogin', 'openModal', 'updateModal', 'updateModalSafe', 'modalParts']) assert.equal(w.run(`typeof ${name}`), 'undefined', name);
  for (const name of ['closeModal', 'openTaskModal', 'logout', 'accountLogin', 'accountSwitch']) assert.equal(w.run(`typeof ${name}`), 'function', name);
});

// ---------------------------------------------------------------- the demo board

test('demo: a switch resolves {ok: true}, moves `current` locally, toasts, and survives the poll; Forget moves the row to "no saved login"; no errors', async () => {
  const w = acctWorld();
  w.location.search = '?demo=1';
  btn(rowOf(w, A2), 'Switch').click();
  await tick(); await tick();
  assert.equal(toasts(w).pop().text, 'Switched to Work · running sessions follow within seconds');
  assert.deepEqual(rows(w).map((r) => !!r.querySelector('.badge.cur')), [false, true, false]);
  w.ctx.__fresh = stateOf();                                              // the fixture again, as the next poll reads it
  w.run('state = __fresh; accountOverlay(state); updateCurrentPage(state)');
  assert.deepEqual(rows(w).map((r) => !!r.querySelector('.badge.cur')), [false, true, false], 'the demo board keeps its move');
  btn(rowOf(w, A1), 'Forget login').click();
  btn(rowOf(w, A1), 'Confirm Forget login').click();
  await tick(); await tick();
  assert.equal(text(rowOf(w, A1).querySelector('.set-chips .dim')), 'no saved login');
  w.ctx.__fresh = stateOf();
  w.run('state = __fresh; accountOverlay(state); updateCurrentPage(state)');
  assert.equal(text(rowOf(w, A1).querySelector('.set-chips .dim')), 'no saved login', 'and keeps that too');
  assert.equal(hidden(panel(w).querySelector('.set-err')), true);
});

test('demo: the add flow shows a made-up link after a moment, accepts a code and adds a row; Cancel works', async () => {
  fake(true);
  try {
    const w = acctWorld({ answers: { '/api/accounts/login/code': { ok: true } } });
    w.location.search = '?demo=1';
    w.run('poll = async () => { __polls++; const s = stateOf(); }');         // placeholder, replaced below
    w.ctx.__fix = () => stateOf();
    w.run('poll = async () => { __polls++; state = accountOverlay(__fix()); updateCurrentPage(state); };');
    btn(block(w), 'Add account').click();
    await tick();
    assert.equal(text(block(w).querySelector('.add-wait')), 'starting the login…');
    mock.timers.tick(1000);
    await tick();
    assert.equal(block(w).querySelector('.add-url').value.startsWith('https://claude.ai/'), true);
    block(w).querySelector('.add-code').value = CODE;
    submitForm(w);
    await tick();
    mock.timers.tick(3000);
    await tick();
    assert.equal(rows(w).length, 4);
    assert.match(toasts(w).pop().text, /^Added New account/);
    btn(block(w), 'Add account').click();
    await tick();
    mock.timers.tick(1000);
    await tick();
    btn(block(w), 'Cancel').click();
    await tick();
    assert.equal(hidden(block(w).querySelector('.add-flight')), true);
  } finally { fake(false); }
});

test('the overlay changes nothing on a live board without a running switch', () => {
  const w = acctWorld();
  const st = stateOf();
  const before = JSON.stringify(st);
  w.ctx.__st2 = st;
  w.run('accountOverlay(__st2)');
  assert.equal(JSON.stringify(w.get('__st2')), before);
  assert.doesNotThrow(() => w.run('accountOverlay(null); accountOverlay({}); accountOverlay({ accounts: null })'));
});
