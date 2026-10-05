// Contract tests for v0.5.17g, a login that expires and logging a saved account in again (the UI half): the row of Settings > Accounts the box was told about (amber chip, Log in
// again as the row's lead action), "login saved <age> ago" on every saved row, Log in again on every row of both agents (Claude: the fresh login replaces the saved one, the
// block says "Log in again as <name>", a login as somebody else says so; Codex: POST {replace_key}, no name asked for, "not in use yet" when a board session keeps the live
// file), the Home banner with its bordered actions (Log in again, Switch back to <name>, dismiss), the topbar chip, the deep link #/settings?sec=accounts&acct=<key> and the
// demo board's ?problem=1. Real core.js, components.js, router.js, shell.js and pages/*.js on minidom's DOM through tests/js/world.mjs; api() is the recorder from world.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { STATIC, makeWorld } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { calls, fakeState, homeWorld, page, plain, setState, text, tick } from './world.mjs';

const A1 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001', A2 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a002', A3 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a003';
const K1 = '3b9f1c2a7d4e5f6a8b0c1d2e', K2 = '7c8d9e0f1a2b3c4d5e6f7a8b';
const NOW = Math.floor(Date.now() / 1000);
const ago = (s) => new Date((NOW - s) * 1000).toISOString();
const acct = (key, over = {}) => ({ key, email: `${key.slice(-4)}@example.com`, name: null, label: null, plan: 'max', rl_5h: 10, rl_7d: 20, resets_5h: NOW + 7200, resets_7d: NOW + 200000, current: false, saved: true, saved_at: ago(3 * 3600), ...over });
const SWITCHED = () => THREE().map((a) => ({ ...a, current: a.key === A2 }));                // the person switched to Work, whose saved login turned out dead
const THREE = () => [acct(A1, { email: 'demo@example.com', name: 'Demo', current: true }), acct(A2, { email: 'work@example.com', name: 'Work', label: 'Work', plan: 'pro', saved_at: ago(41 * 86400) }), acct(A3, { email: 'old@example.com', name: 'Old', saved: false, saved_at: null })];
const PROBLEM = (over = {}) => ({ at: ago(120), agent: 'claude', account: A2, label: 'Work', session: 'shop--api--s1', message: 'Invalid API key · Please run /login', back: null, ...over });
const accountsOf = (list, problem = null, store = { supported: true, reason: null, count: 2 }) => ({ current: (list.find((a) => a.current) || {}).key || null, list, store, problem });
const LOGIN = (over = {}) => ({ running: false, url: null, tail: [], adding: false, email: null, started_at: null, result: null, ...over });
const cxAcct = (key, over = {}) => ({ key, label: 'Home', account_id: null, plan: null, saved: true, saved_at: ago(2 * 86400), current: false, added_at: ago(9 * 86400), last_seen: null, ...over });
const CX_LOGIN = (over = {}) => ({ running: false, adding: false, label: null, replace_key: null, started_at: null, url: null, code: null, result: null, ...over });
const cxOf = (list, login = CX_LOGIN()) => ({ current: (list.find((a) => a.current) || {}).key || null, list, store: { supported: true, add: true, reason: null, count: list.filter((a) => a.saved).length }, login });
const URL1 = 'https://claude.ai/oauth/authorize?code=true&client_id=x&state=y';

const stateOf = (over = {}) => fakeState({ accounts: accountsOf(THREE()), login: LOGIN(), codex_accounts: cxOf([cxAcct(K1, { label: 'Main', current: true }), cxAcct(K2)]), ...over });
const hidden = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const hasCls = (n, c) => n.classList.contains(c);
const isFilled = (b) => (hasCls(b, 'primary') || hasCls(b, 'bp5-intent-primary')) && !hasCls(b, 'tinted');
const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('settingsPage.add && settingsPage.add.dispose(); settingsPage.cx && settingsPage.cx.dispose()'); } catch (_) { /* never mounted */ } } });

function acctWorld({ state = stateOf(), answers = {}, route = '#/settings?sec=accounts' } = {}) {
  const { w } = homeWorld({ state });
  made.push(w);
  w.ctx.__polls = 0;
  w.run('poll = async () => { __polls++; }; Shell.patchUsage = () => {};');
  for (const [k, v] of Object.entries(answers)) w.ctx.__answers[k] = v;
  w.location.hash = route;
  return w;
}
const panel = (w) => page(w).querySelector('.settings-panel[data-sec=accounts]');
const rowOf = (w, key) => panel(w).querySelectorAll('.kv.set-acct').find((r) => r.getAttribute('data-account') === key);
const cxRow = (w, key) => panel(w).querySelectorAll('.kv.set-cx').find((r) => r.getAttribute('data-cx-account') === key);
const btn = (root, label) => root.querySelectorAll('button, a.btn').find((b) => text(b).trim() === label);
const labels = (row) => row.querySelectorAll('.kv-act button').map((b) => text(b));
const toasts = (w) => plain(w.get('__toasts'));
const addBlock = (w) => panel(w).querySelectorAll('.set-add')[0];                 // the Claude block comes first; the Codex one is also .cx-add
const cxBlock = (w) => panel(w).querySelector('.cx-add');
const apiCalls = (w, prefix) => calls(w).filter((c) => c.path.startsWith(prefix));
const setSt = (w, st) => setState(w, st);

// ---------------------------------------------------------------- the row the box was told about

test('the account whose login the box was told is dead wears the amber chip first and Log in again is its one lead action; no other row does, and nothing is filled', () => {
  const w = acctWorld({ state: stateOf({ accounts: accountsOf(THREE(), PROBLEM()) }) });
  const work = rowOf(w, A2);
  assert.deepEqual(work.querySelectorAll('.badge').map(text), ['login not valid any more', 'pro', 'saved login'], 'the attention chip leads');
  const chip = work.querySelector('.badge.warn');
  assert.ok(chip && /does not work any more/.test(chip.getAttribute('title')));
  assert.ok(hasCls(work, 'is-flagged'));
  const again = btn(work, 'Log in again');
  assert.ok(hasCls(again, 'primary') && hasCls(again, 'tinted'), 'the lead action of the row, tinted like every repeated-row primary');
  assert.ok(hasCls(again, 'bp5-intent-primary'), 'made with its class (el() maps primary at creation), so it is drawn as the lead action and not as a plain button');
  assert.ok(!hasCls(btn(work, 'Switch'), 'primary'), 'Switch steps back: switching to a dead login would only fail');
  assert.deepEqual(labels(work), ['Switch', 'Log in again', 'Rename', 'Forget login']);
  for (const k of [A1, A3]) {
    const r = rowOf(w, k);
    assert.ok(!hasCls(r, 'is-flagged') && !r.querySelector('.badge.warn') && !hasCls(btn(r, 'Log in again'), 'primary'), k);
  }
  const all = panel(w).querySelectorAll('button, a.btn').filter((b) => !hidden(b));
  assert.equal(all.filter(isFilled).length, 0, 'no filled primary anywhere on the idle screen');
});

test('a problem on the account in use: its row carries the chip and the lead action too; a problem with no account means the account in use; another agent\'s problem flags nothing here', () => {
  let w = acctWorld({ state: stateOf({ accounts: accountsOf(THREE(), PROBLEM({ account: A1, label: 'Demo' })) }) });
  assert.ok(hasCls(rowOf(w, A1), 'is-flagged') && hasCls(btn(rowOf(w, A1), 'Log in again'), 'tinted'));
  w = acctWorld({ state: stateOf({ accounts: accountsOf(THREE(), PROBLEM({ account: null, label: null })) }) });
  assert.ok(hasCls(rowOf(w, A1), 'is-flagged') && !hasCls(rowOf(w, A2), 'is-flagged'));
  w = acctWorld({ state: stateOf({ accounts: accountsOf(THREE(), PROBLEM({ agent: 'codex', account: K1 })) }) });
  assert.equal(panel(w).querySelectorAll('.is-flagged').filter((n) => n.getAttribute('data-account')).length, 0, 'a Codex problem is not a Claude row\'s');
  assert.ok(hasCls(cxRow(w, K1), 'is-flagged') && cxRow(w, K1).querySelector('.badge.warn') && hasCls(btn(cxRow(w, K1), 'Log in again'), 'tinted') && hasCls(btn(cxRow(w, K1), 'Log in again'), 'bp5-intent-primary'));
});

test('the chip and the lead action follow the poll at once, and go when the problem does', () => {
  const w = acctWorld();
  assert.equal(rowOf(w, A2).querySelector('.badge.warn'), null);
  setSt(w, stateOf({ accounts: accountsOf(THREE(), PROBLEM()) }));
  assert.ok(rowOf(w, A2).querySelector('.badge.warn'));
  setSt(w, stateOf());
  assert.equal(rowOf(w, A2).querySelector('.badge.warn'), null);
  assert.ok(!hasCls(btn(rowOf(w, A2), 'Log in again'), 'primary'));
});

test('every saved row says how old its saved login is, in a quiet line; a row without a saved login or an age says nothing', () => {
  const w = acctWorld();
  const line = (r) => (r.querySelector('.set-saved') ? text(r.querySelector('.set-saved')) : null);
  assert.equal(line(rowOf(w, A1)), 'login saved 3h ago');
  assert.equal(line(rowOf(w, A2)), 'login saved 41d ago');
  assert.equal(line(rowOf(w, A3)), null, 'no saved login: no age');
  assert.ok(hasCls(rowOf(w, A2).querySelector('.set-saved'), 'dim'));
  assert.equal(line(cxRow(w, K1)), 'login saved 2d ago');
  assert.equal(line(cxRow(w, K2)), 'login saved 2d ago');
  setSt(w, stateOf({ accounts: accountsOf([acct(A1, { current: true, saved_at: null })]), codex_accounts: cxOf([cxAcct(K1, { saved_at: undefined, current: true })]) }));
  assert.equal(line(rowOf(w, A1)), null);
  assert.equal(line(cxRow(w, K1)), null, 'an older box without saved_at: nothing invented');
  assert.ok(!/undefined|NaN|null/.test(text(panel(w))));
});

// ---------------------------------------------------------------- Claude: log in again

test('Claude Log in again on a row signs THAT account in (its email), the block says "Log in again as <name>", and a page opened mid-login knows whose it is from the state', async () => {
  const w = acctWorld();
  btn(rowOf(w, A2), 'Log in again').click();
  await tick();
  assert.deepEqual(apiCalls(w, '/api/accounts/login').filter((c) => c.method === 'POST'), [{ method: 'POST', path: '/api/accounts/login', body: { email: 'work@example.com' } }]);
  const title = addBlock(w).querySelector('.add-title');
  assert.equal(text(title), 'Log in again as Work');
  assert.equal(hidden(title), false);
  setSt(w, stateOf({ login: LOGIN({ running: true, adding: true, url: URL1, email: 'work@example.com' }) }));
  assert.equal(text(addBlock(w).querySelector('.add-title')), 'Log in again as Work');
  // a login for a NEW account has no such line
  const w2 = acctWorld();
  btn(addBlock(w2), 'Add account').click();
  assert.equal(hidden(addBlock(w2).querySelector('.add-title')), true);
  // a page that did not start it but sees it in the state
  const w3 = acctWorld({ state: stateOf({ login: LOGIN({ running: true, adding: true, url: URL1, email: 'work@example.com' }) }) });
  assert.equal(text(addBlock(w3).querySelector('.add-title')), 'Log in again as Work');
  const w4 = acctWorld({ state: stateOf({ login: LOGIN({ running: true, adding: true, url: URL1, email: null }) }) });
  assert.equal(hidden(addBlock(w4).querySelector('.add-title')), true);
});

test('Claude: a finished log-in-again says "Logged in again as <name>" (and that it is in use when it is), not "Added"', async () => {
  const w = acctWorld();
  btn(rowOf(w, A2), 'Log in again').click();
  await tick();
  setSt(w, stateOf({ login: LOGIN({ running: true, adding: true, url: URL1, email: 'work@example.com' }) }));
  setSt(w, stateOf({ login: LOGIN({ result: { ok: true, key: A2, name: 'Work', live: false, replaced: true, at: '2026-10-05T10:00:00Z' } }) }));
  assert.deepEqual(toasts(w).pop(), { text: 'Logged in again as Work', kind: 'ok' });
  assert.equal(hidden(addBlock(w).querySelector('.add-note')), true);
  assert.equal(hidden(addBlock(w).querySelector('.add-title')), true, 'the line leaves with the flight');
  btn(rowOf(w, A1), 'Log in again').click();
  await tick();
  setSt(w, stateOf({ login: LOGIN({ running: true, adding: true, url: URL1, email: 'demo@example.com' }) }));
  setSt(w, stateOf({ login: LOGIN({ result: { ok: true, key: A1, name: 'Demo', live: true, replaced: true, at: '2026-10-05T10:01:00Z' } }) }));
  assert.deepEqual(toasts(w).pop(), { text: 'Logged in again as Demo · it is the account in use now', kind: 'ok' });
});

test('Claude: a login as somebody else says it was a different account and kept as its own row, in a toast and in a line that stays', async () => {
  const w = acctWorld();
  btn(rowOf(w, A2), 'Log in again').click();
  await tick();
  setSt(w, stateOf({ login: LOGIN({ running: true, adding: true, url: URL1, email: 'work@example.com' }) }));
  setSt(w, stateOf({ login: LOGIN({ result: { ok: true, key: A3, name: 'Old', live: false, different_account: 'old@example.com', at: '2026-10-05T10:02:00Z' } }) }));
  const msg = 'That login was a different account (old@example.com); it was saved as its own row.';
  assert.deepEqual(toasts(w).pop(), { text: msg, kind: 'warn' });
  const note = addBlock(w).querySelector('.add-note');
  assert.equal(hidden(note), false);
  assert.equal(text(note), msg);
  btn(rowOf(w, A2), 'Log in again').click();                               // the next login clears it
  await tick();
  assert.equal(hidden(addBlock(w).querySelector('.add-note')), true);
});

test('Claude: Try again after a failed log-in-again keeps whose login it is', async () => {
  const w = acctWorld();
  btn(rowOf(w, A2), 'Log in again').click();
  await tick();
  setSt(w, stateOf({ login: LOGIN({ running: true, adding: true, url: URL1, email: 'work@example.com' }) }));
  setSt(w, stateOf({ login: LOGIN({ result: { ok: false, error: 'the login did not complete', at: '2026-10-05T10:03:00Z' } }) }));
  btn(addBlock(w), 'Try again').click();
  await tick();
  assert.deepEqual(apiCalls(w, '/api/accounts/login').filter((c) => c.method === 'POST').pop(), { method: 'POST', path: '/api/accounts/login', body: { email: 'work@example.com', restart: true } });
  assert.equal(text(addBlock(w).querySelector('.add-title')), 'Log in again as Work');
});

// ---------------------------------------------------------------- Codex: log in again

const cxFlight = (w, over = {}) => setSt(w, stateOf({ codex_accounts: cxOf([cxAcct(K1, { label: 'Main', current: true }), cxAcct(K2)], CX_LOGIN({ running: true, adding: true, label: 'Home', replace_key: K2, url: 'https://auth.openai.com/codex/device', code: 'ABCD-12345', ...over })) }));
const cxResult = (w, result) => setSt(w, stateOf({ codex_accounts: cxOf([cxAcct(K1, { label: 'Main', current: true }), cxAcct(K2)], CX_LOGIN({ result })) }));

test('Codex Log in again sends {replace_key} and nothing else, asks for no name, and the block says "Log in again as <label>"', async () => {
  const w = acctWorld();
  btn(cxRow(w, K2), 'Log in again').click();
  await tick();
  assert.deepEqual(apiCalls(w, '/api/codex-accounts/login'), [{ method: 'POST', path: '/api/codex-accounts/login', body: { replace_key: K2 } }]);
  assert.equal(text(cxBlock(w).querySelector('.add-title')), 'Log in again as Home');
  assert.equal(hidden(cxBlock(w).querySelector('.add-title')), false);
  assert.equal(cxBlock(w).querySelector('.cx-name').value, '', 'the name field is not used');
  cxFlight(w);
  assert.equal(text(cxBlock(w).querySelector('.add-title')), 'Log in again as Home', 'and from the state alone, on a page that did not start it');
  const w2 = acctWorld({ state: stateOf({ codex_accounts: cxOf([cxAcct(K1, { label: 'Main', current: true }), cxAcct(K2)], CX_LOGIN({ running: true, adding: true, label: 'Home', replace_key: K2, url: 'https://auth.openai.com/codex/device', code: 'ABCD-12345' })) }) });
  assert.equal(text(cxBlock(w2).querySelector('.add-title')), 'Log in again as Home');
  const w3 = acctWorld({ state: stateOf({ codex_accounts: cxOf([cxAcct(K1, { label: 'Main', current: true }), cxAcct(K2)], CX_LOGIN({ running: true, adding: true, label: 'Fresh', replace_key: null, url: 'https://auth.openai.com/codex/device', code: 'ABCD-12345' })) }) });
  assert.equal(hidden(cxBlock(w3).querySelector('.add-title')), true, 'a new account has no such line');
});

test('Codex: a finished log-in-again is "Logged in again as <label>" (in use when it is); a fresh login that could not go live says why in a warn toast and a line that stays', async () => {
  const w = acctWorld();
  btn(cxRow(w, K2), 'Log in again').click();
  await tick();
  cxFlight(w);
  cxResult(w, { ok: true, key: K2, label: 'Home', live: false, replaced: true, why: null, at: '2026-10-05T10:00:00Z' });
  assert.deepEqual(toasts(w).pop(), { text: 'Logged in again as Home', kind: 'ok' });
  assert.equal(hidden(cxBlock(w).querySelector('.add-note')), true);
  btn(cxRow(w, K1), 'Log in again').click();
  await tick();
  cxFlight(w, { label: 'Main', replace_key: K1 });
  cxResult(w, { ok: true, key: K1, label: 'Main', live: true, replaced: true, why: null, at: '2026-10-05T10:01:00Z', warnings: ['other Codex processes on this box keep the previous login until they restart'] });
  assert.deepEqual(toasts(w).pop(), { text: 'Logged in again as Main · it is the Codex account in use now · other Codex processes on this box keep the previous login until they restart', kind: 'warn' });
  btn(cxRow(w, K1), 'Log in again').click();
  await tick();
  cxFlight(w, { label: 'Main', replace_key: K1 });
  const why = "close the board's Codex sessions first; a running Codex keeps its login and would write it back";
  cxResult(w, { ok: true, key: K1, label: 'Main', live: false, replaced: true, why, at: '2026-10-05T10:02:00Z' });
  const msg = `Logged in again as Main, but the fresh login is not in use yet: ${why}. It goes in by itself once none is open.`;
  assert.deepEqual(toasts(w).pop(), { text: msg, kind: 'warn' });
  assert.equal(text(cxBlock(w).querySelector('.add-note')), msg);
  assert.equal(hidden(cxBlock(w).querySelector('.add-note')), false);
});

test('Codex: Try again after a failed log-in-again repeats it for the same account', async () => {
  const w = acctWorld();
  btn(cxRow(w, K2), 'Log in again').click();
  await tick();
  cxFlight(w);
  cxResult(w, { ok: false, error: 'the login did not complete', at: '2026-10-05T10:03:00Z' });
  btn(cxBlock(w), 'Try again').click();
  await tick();
  assert.deepEqual(apiCalls(w, '/api/codex-accounts/login').pop(), { method: 'POST', path: '/api/codex-accounts/login', body: { replace_key: K2, restart: true } });
});

// ---------------------------------------------------------------- the Home banner

const bannerWorld = (accounts, over = {}) => {
  const w = acctWorld({ state: stateOf({ accounts, ...over }) });
  w.run('renderBanner()');
  return w;
};
const banner = (w) => w.document.querySelector('#banner');

test('the banner says whose login is not valid any more and offers bordered actions only: Log in again, Switch back to <name> (right after a switch), dismiss', () => {
  const w = bannerWorld(accountsOf(SWITCHED(), PROBLEM({ back: { key: A1, label: 'Demo' } })));
  assert.equal(text(banner(w).querySelector('span')), "Claude's login (Work) is not valid any more");
  assert.deepEqual(banner(w).querySelectorAll('button').map(text), ['Log in again', 'Switch back to Demo', '×']);
  assert.ok(hasCls(banner(w), 'warn') && hasCls(banner(w), 'login'));
  assert.equal(banner(w).querySelectorAll('button').filter(isFilled).length, 0, 'bordered: no filled primary');
  const w2 = bannerWorld(accountsOf(THREE(), PROBLEM()));
  assert.deepEqual(banner(w2).querySelectorAll('button').map(text), ['Log in again', '×'], 'no switch to go back to');
  const w3 = bannerWorld(accountsOf(THREE(), PROBLEM({ label: null, account: null })));
  assert.equal(text(banner(w3).querySelector('span')), "Claude's login is not valid any more");
  const w4 = bannerWorld(accountsOf(THREE(), PROBLEM({ agent: 'codex', label: 'Main' })));
  assert.equal(text(banner(w4).querySelector('span')), "Codex's login (Main) is not valid any more");
});

test('the banner is there while the problem is and gone when it is; a repaint leaves nothing behind', () => {
  const w = bannerWorld(accountsOf(THREE()));
  assert.equal(banner(w).children.length, 0);
  assert.ok(!hasCls(banner(w), 'login'));
  setSt(w, stateOf({ accounts: accountsOf(THREE(), PROBLEM()) }));
  w.run('renderBanner(); renderBanner()');
  assert.equal(banner(w).querySelectorAll('span').length, 1);
  setSt(w, stateOf());
  w.run('renderBanner()');
  assert.equal(banner(w).children.length, 0);
  assert.ok(!hasCls(banner(w), 'login') && !hasCls(banner(w), 'warn'));
});

test('Log in again in the banner goes to the account\'s row in Settings (#/settings?sec=accounts&acct=<key>) and starts nothing', async () => {
  const w = bannerWorld(accountsOf(THREE(), PROBLEM()));
  w.location.hash = '#/';
  btn(banner(w), 'Log in again').click();
  await tick();
  assert.equal(w.location.hash, `#/settings?sec=accounts&acct=${A2}`);
  assert.deepEqual(apiCalls(w, '/api/accounts/login'), [], 'it only leads to the row, whose Log in again is the lead action');
});

test('Switch back to <name> is the one-tap switch to the account that was in use (continue_parked from the persisted choice)', async () => {
  const w = bannerWorld(accountsOf(SWITCHED(), PROBLEM({ back: { key: A1, label: 'Demo' } })));
  btn(banner(w), 'Switch back to Demo').click();
  await tick();
  assert.deepEqual(apiCalls(w, '/api/accounts/').filter((c) => c.method === 'POST'), [{ method: 'POST', path: `/api/accounts/${A1}/switch`, body: { continue_parked: true } }]);
});

test('dismiss sends DELETE /api/accounts/problem and the banner is gone at once; a refusal leaves it and says why', async () => {
  const w = bannerWorld(accountsOf(THREE(), PROBLEM()));
  btn(banner(w), '×').click();
  await tick(); await tick();
  assert.deepEqual(apiCalls(w, '/api/accounts/problem'), [{ method: 'DELETE', path: '/api/accounts/problem' }]);
  assert.equal(w.get('state').accounts.problem, null);
  assert.equal(banner(w).children.length, 0);
  const w2 = acctWorld({ state: stateOf({ accounts: accountsOf(THREE(), PROBLEM()) }), answers: { '/api/accounts/problem': () => { throw new Error('the box refused it'); } } });
  w2.run('renderBanner()');
  btn(banner(w2), '×').click();
  await tick(); await tick();
  assert.ok(w2.get('state').accounts.problem, 'still there');
});

// ---------------------------------------------------------------- the topbar chip and the deep link

const sWorld = () => {                                                                // the topbar's own world (as tests/js/shell-accounts.test.mjs builds it): shell.js is not part of homeWorld
  const w = makeWorld();
  installDom(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', 'pages/agents.js', 'shell.js']) w.load(f);
  w.run('Shell.refs = {}; Shell.buildTopbar(document.querySelector("#topbar"))');
  return w;
};
const chipOf = (w) => w.document.querySelector('#topbar [data-pill=acct]');

test('the topbar chip of the account in use turns amber with "Claude\'s login (<label>) is not valid any more · Log in again" and links to its row; the problem outranks "more room elsewhere"', () => {
  const w = sWorld();
  const paint = (st) => { w.ctx.__st = st; w.run('state = __st; Shell.patchUsage(__st)'); };
  paint(stateOf({ accounts: accountsOf(THREE(), PROBLEM({ account: A1, label: 'Demo' })) }));
  const c = chipOf(w);
  assert.ok(hasCls(c, 'hue-amber'));
  assert.equal(c.getAttribute('title'), "Claude's login (Demo) is not valid any more · Log in again");
  assert.equal(c.getAttribute('href'), `#/settings?sec=accounts&acct=${A1}`);
  assert.match(c.getAttribute('aria-label'), /not valid any more/);
  // the account in use is nearly out and another has room: the amber title would be "Work has N % ..."; the dead login wins
  const busy = THREE();
  busy[0].rl_7d = 95;
  w.ctx.__usage = { value: { five_hour: { used_percentage: 10, resets_at: NOW + 7200 }, seven_day: { used_percentage: 95, resets_at: NOW + 200000 } } };
  paint({ ...stateOf({ accounts: accountsOf(busy, PROBLEM({ account: A1, label: 'Demo' })) }), usage: plain(w.ctx.__usage) });
  assert.match(chipOf(w).getAttribute('title'), /^Claude's login \(Demo\) is not valid any more/);
  // no problem: back to the plain chip and the usage link
  paint(stateOf());
  assert.equal(chipOf(w).getAttribute('href'), '#/usage');
  assert.match(chipOf(w).getAttribute('title'), /^Account: Demo/);
  // a problem on an account that is not in use leaves the chip alone
  paint(stateOf({ accounts: accountsOf(THREE(), PROBLEM()) }));
  assert.equal(chipOf(w).getAttribute('href'), '#/usage');
  assert.ok(!hasCls(chipOf(w), 'hue-amber'));
});

test('#/settings?sec=accounts&acct=<key> scrolls to that account\'s row once, Claude or Codex, and an unknown key does nothing', () => {
  for (const [key, attr] of [[A2, 'data-account'], [K2, 'data-cx-account']]) {
    const { w } = homeWorld({ state: stateOf() });
    made.push(w);
    w.run('poll = async () => {}; Shell.patchUsage = () => {};');
    w.ctx.__scrolled = [];
    w.run('const __mk = document.createElement; document.createElement = (t) => { const n = __mk.call(document, t); n.scrollIntoView = function (o) { __scrolled.push([this.getAttribute("data-account") || this.getAttribute("data-cx-account"), o && o.block]); }; return n; }');
    w.location.hash = `#/settings?sec=accounts&acct=${key}`;
    assert.deepEqual(plain(w.get('__scrolled')), [[key, 'center']], attr);
    setState(w, stateOf());
    assert.equal(w.get('__scrolled').length, 1, 'once: the minute repaint does not scroll again');
  }
  const { w } = homeWorld({ state: stateOf() });
  made.push(w);
  w.run('poll = async () => {}; Shell.patchUsage = () => {};');
  w.ctx.__scrolled = [];
  w.run('const __mk = document.createElement; document.createElement = (t) => { const n = __mk.call(document, t); n.scrollIntoView = function () { __scrolled.push(1); }; return n; }');
  w.location.hash = '#/settings?sec=accounts&acct=nope';
  assert.equal(w.get('__scrolled').length, 0);
});

// ---------------------------------------------------------------- the demo board

test('demo ?problem=1: the second saved account is in use and dead, the first is offered as the way back, a switch moves it along, a log-in-again or dismiss ends it', async () => {
  const { w } = homeWorld({ state: stateOf() });
  made.push(w);
  w.location.search = '?demo=1&problem=1';
  w.run('poll = async () => {}; Shell.patchUsage = () => {};');
  w.ctx.__s = plain(stateOf());
  const overlaid = () => plain(w.run('accountOverlay(JSON.parse(JSON.stringify(__s)))').accounts);
  let a = overlaid();
  assert.equal(a.current, A2);
  assert.equal(a.problem.account, A2);
  assert.deepEqual(a.problem.back, { key: A1, label: 'Demo' });
  assert.equal(a.problem.agent, 'claude');
  assert.ok(Date.parse(a.problem.at) <= Date.now() && Date.now() - Date.parse(a.problem.at) < 600000, 'a few minutes ago');
  w.run('acctFlow.hold = { key: __s.accounts.list[0].key }');                         // the person tapped Switch back
  a = overlaid();
  assert.equal(a.current, A1);
  assert.equal(a.problem.account, A2);
  assert.equal(a.problem.back, null, 'the dead account is not in use any more');
  w.run('acctFlow.hold = null; acctDemo().problemOff = true');
  assert.equal(overlaid().problem, null, 'dismissed: gone');
  // without ?problem=1 the demo shows no problem, whatever the fixture says
  w.location.search = '?demo=1';
  w.run('acctFlow.demo = null');
  assert.equal(overlaid().problem, null);
});

test('demo: a log-in-again of an account renews its saved_at, keeps one row and, for the dead account, ends the problem', async () => {
  fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8');
  const { w } = homeWorld({ state: stateOf() });
  made.push(w);
  w.location.search = '?demo=1&problem=1';
  w.run('poll = async () => {}; Shell.patchUsage = () => {};');
  w.ctx.__s = plain(stateOf());
  w.run('acctDemo().renewed[__s.accounts.list[1].key] = new Date().toISOString(); acctDemo().problemOff = true');
  const a = plain(w.run('accountOverlay(JSON.parse(JSON.stringify(__s)))').accounts);
  assert.equal(a.list.length, 3);
  assert.ok(Date.now() - Date.parse(a.list[1].saved_at) < 5000, 'saved just now');
  assert.equal(a.problem, null);
});

test('the demo fixture carries the new keys the live state has (problem, saved_at, replace_key) and ?problem=1 is not baked into it', () => {
  const st = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8'));
  assert.equal(st.accounts.problem, null);
  assert.ok(st.accounts.list.every((a) => !a.saved || typeof a.saved_at === 'string'));
  assert.ok(st.codex_accounts.list.every((a) => !a.saved || typeof a.saved_at === 'string'));
  assert.equal(st.codex_accounts.login.replace_key, null);
});
