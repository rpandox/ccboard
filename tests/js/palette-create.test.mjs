// The command palette's Create and Accounts groups (v0.5.101, issue #304): the six entries of the Create (+) menu with their chords, opening what the menu entries open, and one row
// per other saved login ("Switch Claude account to <name>", "Switch Codex account to <name>") and per login that stopped working ("Log in again as <name>"), running the same calls as
// Settings > Accounts. Real core.js, components.js, keymap.js, launcher.js, palette.js, router.js, widgets.js, pages/agents.js and shell.js on minidom's DOM.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const NOW = Math.floor(Date.now() / 1000);
const A1 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001', A2 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a002';
const X1 = '3b9f1c2a7d4e5f6a8b0c1d2e', X2 = '7c8d9e0f1a2b3c4d5e6f7a8b';
const acct = (key, over = {}) => ({ key, email: `${key.slice(-4)}@example.com`, name: null, label: null, plan: 'max', rl_5h: 10, rl_7d: 20, resets_5h: NOW + 7200, resets_7d: NOW + 200000, current: false, saved: true, ...over });
const cx = (key, over = {}) => ({ key, label: key === X1 ? 'Codex main' : 'Codex work', plan: 'plus', saved: true, current: false, ...over });

const STATE = (over = {}) => ({
  user: 'alice', config: { projects_dir: '/srv/projects', code_https_port: 10000 },
  projects: [
    { name: 'shop', path: '/srv/projects/shop', root: null, orphan_sessions: [],
      repos: [{ name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] },
        { name: 'web', path: '/srv/projects/shop/web', state: 'ok', branch: 'dev', dirty: false, devcontainer: false, sessions: [] }] },
  ],
  clone_queue: { queued: [], done: [], cap: 3 }, pending_permissions: [], tasks: [], jobs: [], runs: [],
  accounts: { current: A1, store: { supported: true }, list: [acct(A1, { current: true, label: 'Personal' }), acct(A2, { label: 'Work', plan: 'pro' })] },
  codex_accounts: { current: X1, store: { supported: true, add: true }, list: [cx(X1, { current: true }), cx(X2)] },
  ...over,
});

function world(state = STATE()) {
  const w = makeWorld({ navigator: { platform: 'Linux x86_64', userAgent: 'node-test' } });
  installDom(w);
  for (const f of ['core.js', 'components.js', 'keymap.js', 'live.js', 'launcher.js', 'palette.js', 'router.js', 'pages/widgets.js', 'pages/agents.js', 'shell.js']) w.load(f);
  w.ctx.__calls = []; w.ctx.__toasts = []; w.ctx.__st = state;
  w.run(`
    api = async (method, path, body) => { if (method === 'GET' && path === '/api/skills') return { skills: [] }; __calls.push({ method, path, body }); return { ok: true }; };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    pageToast = (text, kind) => { __toasts.push({ text, kind }); };
    updateCurrentPage = () => {};
    poll = async () => {};
    renderBanner = () => {};
    state = __st;
  `);
  w.run('Keymap.install()');
  return w;
}

const calls = (w) => plain(w.get('__calls')).filter((c) => c.method === 'POST');
const tick = () => new Promise((r) => setImmediate(r));
const dlg = (w) => w.document.querySelector('#helpdlg');
const sheet = (w) => w.document.getElementById('sheet');
const sheetTitle = (w) => (sheet(w).open ? sheet(w).querySelector('.sheet-title').textContent : '');
const rows = (w) => dlg(w).querySelectorAll('.pal-item');
const label = (n) => n.querySelector('.pal-label').textContent;
const kbd = (n) => (n.querySelector('.pal-kbd') ? n.querySelector('.pal-kbd').textContent : '');
const groups = (w) => dlg(w).querySelectorAll('.pal-group').map((n) => n.textContent);
const row = (w, text) => rows(w).find((n) => label(n) === text);
const box = (w) => dlg(w).querySelector('.pal-input');
const typeInto = (w, text) => { const b = box(w); b.value = text; b.dispatchEvent({ type: 'input' }); };
const choose = (w, text) => { const n = row(w, text); assert.ok(n, `a row "${text}" in: ${rows(w).map(label).join(' | ')}`); n.click(); };
const openPalette = (w) => { w.run('Palette.open()'); };

const CREATE = [['New session', 'c s', 'New session'], ['New task', 'c t', 'New task'], ['Schedule', '', 'Schedule a run'], ['New project', 'c p', ''],
  ['Import from GitHub', 'c r', 'Import repos from GitHub'], ['Batch prompt', '', 'Batch prompt across repos']];      // label, chord, the sheet title it opens ('' = the wizard page)

test('an empty palette has a Create group after Routes with the six entries of the + menu, in its order, each with its chord as the row\'s kbd', () => {
  const w = world();
  openPalette(w);
  const g = groups(w);
  assert.ok(g.includes('Create'), g.join(','));
  assert.ok(g.indexOf('Create') > g.indexOf('Routes'), 'after the pages');
  const menu = plain(w.run('Shell.createItems().map((i) => i.label)'));
  assert.deepEqual(CREATE.map((c) => c[0]), menu, 'the same six, in the + menu\'s order');
  const inGroup = rows(w).filter((n) => CREATE.some((c) => c[0] === label(n)));
  assert.deepEqual(inGroup.map(label), menu);
  assert.deepEqual(inGroup.map(kbd), CREATE.map((c) => c[1]));
});

test('the chords on the rows are the ones keymap.js binds to the same openCreate (c s, c t, c p, c r); Schedule and Batch prompt have none', () => {
  const w = world();
  const bound = plain(w.run("Keymap.help().filter((r) => r.group === 'Create').map((r) => [r.spec, r.help])"));
  assert.deepEqual(bound.map((b) => b[0]).sort(), ['c p', 'c r', 'c s', 'c t']);
  const chords = plain(w.run('Shell.CREATE.map((e) => [e.kind, e.chord])'));
  for (const [, chord] of chords.filter((c) => c[1])) assert.ok(bound.some((b) => b[0] === chord), `${chord} is bound`);
  assert.deepEqual(chords.filter((c) => !c[1]).map((c) => c[0]), ['schedule', 'batch']);
});

test('each Create row opens what the menu entry opens: the same sheet (or the wizard page for New project), the palette closed', () => {
  for (const [name, , want] of CREATE) {
    const viaMenu = world();
    viaMenu.run(`Shell.createItems().find((i) => i.label === ${JSON.stringify(name)}).onClick()`);
    const menuTitle = sheetTitle(viaMenu);
    const menuHash = viaMenu.location.hash;
    const w = world();
    openPalette(w);
    choose(w, name);
    assert.equal(dlg(w).open, false, `${name}: the palette closes`);
    assert.equal(sheetTitle(w), menuTitle, `${name}: the menu's sheet`);
    assert.equal(w.location.hash, menuHash, `${name}: the menu's page`);
    if (want) assert.equal(sheetTitle(w), want, name);
    else assert.equal(w.location.hash, '#/onboarding/project', 'New project: the wizard');
  }
});

test('New session and New task from the palette take the place the page names, like the keys: on a project page the launcher form opens for that project at once', () => {
  const w = world();
  w.location.hash = '#/p/shop?repo=api';
  openPalette(w);
  choose(w, 'New task');
  assert.equal(sheetTitle(w), 'New task · shop/api');
  w.run('closeSheet()');
  openPalette(w);
  choose(w, 'New session');
  assert.equal(sheetTitle(w), 'New session · shop/api');
});

test('typing finds them: "new" lists the three New ..., "task", "schedule", "batch", "import", "create" and "cron" each find their row, the chord still on it', () => {
  const w = world();
  openPalette(w);
  const find = (q) => { typeInto(w, q); return rows(w).map(label); };
  assert.deepEqual(find('new').filter((l) => /^New /.test(l)).sort(), ['New project', 'New session', 'New task']);
  assert.ok(find('task').includes('New task'));
  assert.equal(kbd(row(w, 'New task')), 'c t');
  assert.ok(find('schedule').includes('Schedule'));
  assert.ok(find('batch').includes('Batch prompt'));
  assert.ok(find('import').includes('Import from GitHub'));
  assert.ok(find('create').includes('New session') && find('create').includes('Batch prompt'), 'the word "create" finds all six');
  assert.ok(find('cron').includes('Schedule'));
});

test('Enter on "new task" opens the task sheet', () => {
  const w = world();
  openPalette(w);
  typeInto(w, 'new task');
  box(w).dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() {}, isComposing: false });
  assert.equal(sheetTitle(w), 'New task');
});

test('without the shell there is no Create group (the palette alone has nothing to open)', () => {
  const w = makeWorld({ navigator: { platform: 'Linux x86_64', userAgent: 'node-test' } });
  installDom(w);
  for (const f of ['core.js', 'components.js', 'keymap.js', 'live.js', 'launcher.js', 'palette.js', 'router.js', 'pages/widgets.js']) w.load(f);
  assert.equal(plain(w.run('Palette.createGroup(() => {})')), null);
  assert.equal(plain(w.run('Palette.accountGroup(() => {}, "")')), null, 'nor an Accounts group without pages/agents.js');
});

// ---------------------------------------------------------------- the account rows

test('Accounts group: a "Switch <agent> account to <name>" row for each OTHER saved login, none for the one in use', () => {
  const w = world();
  openPalette(w);
  assert.ok(groups(w).includes('Accounts'));
  const ls = rows(w).map(label).filter((l) => /^Switch /.test(l));
  assert.deepEqual(ls, ['Switch Claude account to Work', 'Switch Codex account to Codex work']);
  assert.equal(row(w, 'Switch Claude account to Personal'), undefined, 'the account in use');
  assert.equal(row(w, 'Switch Codex account to Codex main'), undefined);
});

test('no row for a login that cannot be switched to: no saved copy, or a box that keeps no saved logins (Settings\' own rule)', () => {
  const st = STATE();
  st.accounts.list[1].saved = false;
  st.codex_accounts.store = { supported: false, add: false };
  const w = world(st);
  openPalette(w);
  assert.equal(groups(w).includes('Accounts'), false);
  assert.deepEqual(rows(w).map(label).filter((l) => /^Switch |^Log in/.test(l)), []);
});

test('typing "switch", "account", "claude", "codex" or a name finds the rows', () => {
  const w = world();
  openPalette(w);
  const find = (q) => { typeInto(w, q); return rows(w).map(label); };
  assert.ok(find('switch').includes('Switch Claude account to Work') && find('switch').includes('Switch Codex account to Codex work'));
  assert.ok(find('account').includes('Switch Claude account to Work'));
  assert.ok(find('codex').includes('Switch Codex account to Codex work'));
  assert.ok(find('work').includes('Switch Claude account to Work'));
  assert.ok(find('login').includes('Switch Claude account to Work'), 'a saved login');
});

test('running a Claude switch row calls what the Settings row calls: POST /api/accounts/<key>/switch with the continue choice; the palette closes first', async () => {
  const w = world();
  openPalette(w);
  choose(w, 'Switch Claude account to Work');
  assert.equal(dlg(w).open, false);
  await tick();
  assert.deepEqual(calls(w), [{ method: 'POST', path: `/api/accounts/${A2}/switch`, body: { continue_parked: true } }]);
  assert.match(plain(w.get('__toasts')).map((t) => t.text).join('|'), /Switched to Work/);
  assert.equal(plain(w.run('state.accounts.current')), A2);
});

test('running a Codex switch row posts /api/codex-accounts/<key>/switch', async () => {
  const w = world();
  openPalette(w);
  choose(w, 'Switch Codex account to Codex work');
  await tick();
  assert.deepEqual(calls(w), [{ method: 'POST', path: `/api/codex-accounts/${X2}/switch` }]);
  assert.equal(plain(w.run('state.codex_accounts.current')), X2);
});

test('a login the box was told is no good gets "Log in again as <name>": a Claude row starts the sign-in in Settings > Accounts, a Codex row opens its row there', () => {
  const st = STATE();
  st.accounts.problem = { at: NOW, agent: 'claude', account: A1, label: 'Personal', message: 'auth', back: null };
  const w = world(st);
  openPalette(w);
  assert.ok(row(w, 'Log in again as Personal'));
  assert.equal(row(w, 'Log in again as Codex main'), undefined, 'only the account the problem names');
  choose(w, 'Log in again as Personal');
  assert.equal(w.location.hash, '#/settings?sec=accounts');
  assert.deepEqual(plain(w.run('acctFlow.want')), { email: `${A1.slice(-4)}@example.com` });
  const st2 = STATE();
  st2.codex_accounts.problem = null;
  st2.accounts.problem = { at: NOW, agent: 'codex', account: X1, label: 'Codex main', message: 'auth', back: null };
  const w2 = world(st2);
  openPalette(w2);
  assert.equal(row(w2, 'Log in again as Personal'), undefined);
  choose(w2, 'Log in again as Codex main');
  assert.equal(w2.location.hash, `#/settings?sec=accounts&acct=${X1}`);
  openPalette(w2);
  typeInto(w2, 'log in');
  assert.ok(rows(w2).map(label).includes('Log in again as Codex main'));
});

test('an empty palette shows at most four account rows; a search shows them all', () => {
  const st = STATE();
  for (let i = 3; i < 9; i++) st.accounts.list.push(acct(`7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a00${i}`, { label: `Spare ${i}` }));
  const w = world(st);
  openPalette(w);
  assert.equal(rows(w).filter((n) => /^Switch Claude/.test(label(n))).length, 4);
  typeInto(w, 'spare');
  assert.equal(rows(w).filter((n) => /^Switch Claude/.test(label(n))).length, 6);
});
