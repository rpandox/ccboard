// The account chip's menu (v0.5.101, issue #305): a tap on the chip in the topbar opens a small menu instead of going to #/usage: the Claude and Codex logins in use, one row per other
// saved login that switches on a tap (the same accountSwitch / cxSwitch the Settings rows call), a Log in again row for a login the box was told is no good, and the links to Usage and
// Settings > Accounts. Real core.js, components.js (menu()), launcher.js, router.js, widgets.js, pages/agents.js and shell.js on minidom's DOM.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const NOW = Math.floor(Date.now() / 1000);
const A1 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001', A2 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a002', A3 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a003';
const X1 = '3b9f1c2a7d4e5f6a8b0c1d2e', X2 = '7c8d9e0f1a2b3c4d5e6f7a8b';
const acct = (key, over = {}) => ({ key, email: `${key.slice(-4)}@example.com`, name: null, label: null, plan: 'max', rl_5h: 10, rl_7d: 20, resets_5h: NOW + 7200, resets_7d: NOW + 200000, current: false, saved: true, ...over });
const cx = (key, over = {}) => ({ key, label: key === X1 ? 'Codex main' : 'Codex work', plan: 'plus', saved: true, current: false, ...over });

/** A world with the topbar built and a recorder in place of api / toasts. */
function menuWorld() {
  const w = makeWorld();
  installDom(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', 'pages/agents.js', 'shell.js']) w.load(f);
  w.ctx.__calls = []; w.ctx.__toasts = [];
  w.run(`
    api = async (method, path, body) => { __calls.push({ method, path, body }); return { ok: true }; };
    pageToast = (text, kind) => { __toasts.push({ text, kind }); };
    poll = async () => {};
    updateCurrentPage = () => {};
    Shell.refs = {};
    Shell.buildTopbar(document.querySelector('#topbar'));
  `);
  return w;
}
const paint = (w, st) => { w.ctx.__st = st; w.run('state = __st; Shell.patchUsage(__st)'); };
const chip = (w) => w.document.querySelector('#topbar [data-pill=acct]');
const pop = (w) => w.document.querySelector('#topbar .menu-pop');
const rows = (w) => (pop(w) ? pop(w).querySelectorAll('.menuitem') : []);
const name = (r) => r.querySelector('.mi-name') ? r.querySelector('.mi-name').textContent : r.querySelector('.mi-text').textContent;
const sub = (r) => (r.querySelector('.mi-sub') ? r.querySelector('.mi-sub').textContent : '');
const names = (w) => rows(w).map(name);
const row = (w, label) => rows(w).find((r) => name(r) === label);
const calls = (w) => plain(w.get('__calls'));
const click = (extra = {}) => ({ type: 'click', detail: 1, isTrusted: false, prevented: false, preventDefault() { this.prevented = true; }, stopPropagation() { this._stopped = true; }, stopImmediatePropagation() { this._stopped = true; this._stoppedNow = true; }, ...extra });
const tap = (n) => { const ev = click(); n.dispatchEvent(ev); return ev; };
const tick = () => new Promise((r) => setImmediate(r));

const claudeState = (over = {}) => ({
  accounts: { current: A1, store: { supported: true }, list: [acct(A1, { current: true, label: 'Personal' }), acct(A2, { label: 'Work', plan: 'pro' })] },
  ...over,
});
const withCodex = (st, store = { supported: true, add: true }) => ({ ...st, codex_accounts: { current: X1, store, list: [cx(X1, { current: true }), cx(X2)] } });

test('the chip is a menu button: role button, a popup of kind menu, closed until tapped; its link (#/usage) stays for a modified click', () => {
  const w = menuWorld();
  paint(w, claudeState());
  const c = chip(w);
  assert.equal(c.getAttribute('role'), 'button');
  assert.equal(c.getAttribute('aria-haspopup'), 'menu');
  assert.equal(c.getAttribute('aria-expanded'), 'false');
  assert.equal(c.getAttribute('href'), '#/usage');
  assert.equal(pop(w), null, 'nothing open before the tap');
});

test('a plain tap opens the menu and does not follow the link; a ctrl / meta / shift click keeps the link and opens no menu', () => {
  const w = menuWorld();
  paint(w, claudeState());
  const ev = tap(chip(w));
  assert.equal(ev.prevented, true, 'the link is not followed');
  assert.ok(pop(w), 'the menu is open');
  assert.equal(chip(w).getAttribute('aria-expanded'), 'true');
  assert.equal(w.location.hash, '', 'nothing navigated');
  tap(chip(w));                                                    // a second tap closes it
  assert.equal(pop(w), null);
  for (const mod of ['ctrlKey', 'metaKey', 'shiftKey']) {
    const e = click({ [mod]: true });
    chip(w).dispatchEvent(e);
    assert.equal(e.prevented, false, `${mod}: the browser opens the link`);
    assert.equal(pop(w), null, `${mod}: no menu`);
  }
});

test('Space on the focused chip opens the menu, as on a button (a link answers Enter by itself)', () => {
  const w = menuWorld();
  paint(w, claudeState());
  const e = { type: 'keydown', key: ' ', prevented: false, preventDefault() { this.prevented = true; } };
  chip(w).dispatchEvent(e);
  assert.equal(e.prevented, true);
  assert.ok(pop(w));
});

test('the rows, Claude only: the login in use, a Switch row for the other saved login, then Usage and Settings > Accounts', () => {
  const w = menuWorld();
  paint(w, claudeState());
  tap(chip(w));
  assert.deepEqual(names(w), ['Claude: Personal', 'Switch to Work', 'Usage', 'Settings > Accounts']);
  const cur = row(w, 'Claude: Personal');
  assert.equal(sub(cur), 'in use · max');
  assert.ok(cur.classList.contains('is-current'));
  assert.equal(cur.getAttribute('aria-current'), 'true');
  assert.equal(sub(row(w, 'Switch to Work')), 'Claude · pro');
  assert.equal(row(w, 'Switch to Work').classList.contains('is-current'), false);
  assert.equal(pop(w).getAttribute('role'), 'menu');
  assert.ok(rows(w).every((r) => r.getAttribute('role') === 'menuitem'));
});

test('a saved login only: an account with no saved login, or a box that keeps no saved logins, offers no Switch row (Settings\' own rule)', () => {
  const w = menuWorld();
  const st = claudeState();
  st.accounts.list[1].saved = false;
  paint(w, st);
  tap(chip(w));
  assert.deepEqual(names(w), ['Claude: Personal', 'Usage', 'Settings > Accounts'], 'no saved login: nothing to switch to');
  tap(chip(w));
  const off = claudeState();
  off.accounts.store = { supported: false };
  paint(w, off);
  tap(chip(w));
  assert.deepEqual(names(w), ['Claude: Personal', 'Usage', 'Settings > Accounts'], 'unsupported store: the same');
});

test('Claude and Codex together: both logins in use lead, then every other saved login of either agent', () => {
  const w = menuWorld();
  const st = withCodex(claudeState());
  st.accounts.list.push(acct(A3, { label: 'Spare' }));
  paint(w, st);
  tap(chip(w));
  assert.deepEqual(names(w), ['Claude: Personal', 'Codex: Codex main', 'Switch to Work', 'Switch to Spare', 'Switch to Codex work', 'Usage', 'Settings > Accounts']);
  assert.equal(sub(row(w, 'Codex: Codex main')), 'in use · plus');
  assert.equal(sub(row(w, 'Switch to Codex work')), 'Codex · plus');
});

test('tapping a Switch row runs the Settings switch: POST /api/accounts/<key>/switch for Claude, /api/codex-accounts/<key>/switch for Codex; the menu closes and a toast says so', async () => {
  const w = menuWorld();
  paint(w, withCodex(claudeState()));
  tap(chip(w));
  tap(row(w, 'Switch to Work'));
  assert.equal(pop(w), null, 'the menu closes on the tap');
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.method === 'POST'), [{ method: 'POST', path: `/api/accounts/${A2}/switch`, body: { continue_parked: true } }]);
  assert.match(plain(w.get('__toasts')).map((t) => t.text).join('|'), /Switched to Work/);
  tap(chip(w));
  assert.ok(row(w, 'Claude: Work'), 'the account switched to is now the one in use (the chip menu repainted from the same state)');
  tap(chip(w));
  w.ctx.__calls.length = 0;
  tap(chip(w));
  tap(row(w, 'Switch to Codex work'));
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.method === 'POST'), [{ method: 'POST', path: `/api/codex-accounts/${X2}/switch` }]);
});

test('the links: Usage goes to #/usage, Settings > Accounts to #/settings?sec=accounts, and the row of the login in use to its own row there', () => {
  const w = menuWorld();
  paint(w, withCodex(claudeState()));
  tap(chip(w));
  tap(row(w, 'Usage'));
  assert.equal(w.location.hash, '#/usage');
  assert.equal(pop(w), null);
  tap(chip(w));
  tap(row(w, 'Settings > Accounts'));
  assert.equal(w.location.hash, '#/settings?sec=accounts');
  tap(chip(w));
  tap(row(w, 'Claude: Personal'));
  assert.equal(w.location.hash, `#/settings?sec=accounts&acct=${A1}`);
  tap(chip(w));
  tap(row(w, 'Codex: Codex main'));
  assert.equal(w.location.hash, `#/settings?sec=accounts&acct=${X1}`);
});

test('Usage stays one tap from the menu: it is the first link row after the accounts, in every state', () => {
  const w = menuWorld();
  for (const st of [claudeState(), withCodex(claudeState()), (() => { const s = claudeState(); s.accounts.store = { supported: false }; return s; })()]) {
    paint(w, st);
    tap(chip(w));
    const all = names(w);
    assert.equal(all[all.length - 2], 'Usage');
    assert.equal(all[all.length - 1], 'Settings > Accounts');
    tap(chip(w));
  }
});

test('a login the box was told is no good: the first row is Log in again as <name> (the amber chip\'s old job), and it starts the sign-in in Settings > Accounts', () => {
  const w = menuWorld();
  const st = claudeState();
  st.accounts.problem = { at: NOW, agent: 'claude', account: A1, label: 'Personal', message: 'auth', back: null };
  paint(w, st);
  assert.ok(chip(w).classList.contains('hue-amber'));
  tap(chip(w));
  assert.deepEqual(names(w), ['Log in again as Personal', 'Claude: Personal', 'Switch to Work', 'Usage', 'Settings > Accounts']);
  assert.equal(sub(row(w, 'Log in again as Personal')), 'Claude · login not valid any more');
  tap(row(w, 'Log in again as Personal'));
  assert.equal(w.location.hash, '#/settings?sec=accounts');
  assert.deepEqual(plain(w.run('acctFlow.want')), { email: `${A1.slice(-4)}@example.com` }, 'the add flow is asked for, for that account');
});

test('the menu follows the state: it is built when opened, so a rename or a switch from Settings shows at once; the menu is also shut by a route change', () => {
  const w = menuWorld();
  const st = claudeState();
  paint(w, st);
  tap(chip(w));
  assert.ok(row(w, 'Switch to Work'));
  w.location.hash = '#/search';
  assert.equal(pop(w), null, 'a hash change closes it');
  st.accounts.list[1].label = 'Client';
  paint(w, st);
  tap(chip(w));
  assert.ok(row(w, 'Switch to Client'));
});

test('without pages/agents.js the menu is still two links; with fewer than two accounts the chip is hidden and cannot open one', () => {
  const w = makeWorld();
  installDom(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', 'shell.js']) w.load(f);
  w.run('Shell.refs = {}; Shell.buildTopbar(document.querySelector("#topbar"))');
  assert.deepEqual(plain(w.run('Shell.accountItems(null).map((i) => i.label)')), ['Usage', 'Settings > Accounts']);
  const one = menuWorld();
  paint(one, { accounts: { current: A1, list: [acct(A1, { current: true })] } });
  assert.ok(chip(one).classList.contains('hidden'));
});
