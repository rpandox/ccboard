// Contract tests for issue #151 on the page side: the merged inbox of other nodes ("On other nodes": one row per node id, session, kind and time, gone when the next reading stops
// listing it, stale while the node is not online), the one line per used up account window on the Home strip (two nodes under one subscription share a line), and the rows of
// Settings > Notifications, one for each paired node. Real scripts on minidom's DOM (tests/js/hubworld.mjs); the board's answers are a fake fetch; nothing here leaves the page.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { answer, boardState, hubWorld, iso, page, plain, poll, rec, setState, text, tick } from './hubworld.mjs';

const all = (n, sel) => (n.document || n).querySelectorAll(sel);
const go = async (w, hash) => { w.location.hash = hash; await tick(); };
const ready = async (nodes, over = {}) => { const h = hubWorld({ state: boardState(), nodes, ...over }); await poll(h.w); return h; };
const sess = (tmux, over = {}) => ({ tmux, project: 'shop', repo: 'api', session: tmux.split('--')[2], agent: 'claude', state: 'waiting', needs_you: true, kind: 'user', since: iso(300), model: null, ...over });
const withSessions = (handle, sessions, over = {}) => { const r = rec(handle, over); r.state.sessions = sessions; return r; };
const group = (w) => w.document.querySelector('.nd-inbox');
const rows = (w) => all(group(w), '.nd-item');
const KEY = 'a1b2c3d4e5f6';
const acct = (agent, key, over = {}) => ({ agent, label: 'Work', current: true, subscription_key: key, window: { pct: 100, resets_at: 1790000000, backoff_until: null, known: true }, limited: false, ...over });
const limitedNode = (handle, items, over = {}) => { const r = rec(handle, over); r.card.accounts = { supported: true, items }; r.state.usage = { claude: { pct: 100, resets_at: 1790000000, known: true, limited: true } }; return r; };

// ---------------------------------------------------------------- the merged inbox

test('a remote item appears once however many polls list it, keyed by node id, session, kind and time', async () => {
  const r = withSessions('build-box', [sess('shop--api--s1', { since: iso(300) })]);
  const { w } = await ready([r]);
  await go(w, '#/inbox');
  for (let i = 0; i < 3; i += 1) { answer(w, 200, { nodes: [withSessions('build-box', [sess('shop--api--s1', { since: r.state.sessions[0].since })], { etag: `e${i}` })], at: iso(0) }, `W/"${i + 2}"`); await poll(w); }
  assert.equal(rows(w).length, 1, 'one row after four polls');
  assert.match(text(group(w).querySelector('h2')), /^On other nodes \(1\)$/);
  const items = plain(w.run('Nodes.attention()'));
  assert.equal(items.length, 1);
  assert.equal(items[0].dk, ['ts:nBUILDBOX', 'shop--api--s1', 'input', r.state.sessions[0].since].join('|'));
  assert.deepEqual([items[0].attn, items[0].stale], ['input', false]);
});

test('an item goes when the next reading no longer lists it, and a new event of the same session is a new row', async () => {
  const first = sess('shop--api--s1', { since: iso(300) });
  const { w } = await ready([withSessions('build-box', [first, sess('shop--api--s2', { needs_you: false, state: 'working' })])]);
  await go(w, '#/inbox');
  assert.equal(rows(w).length, 1);
  const key1 = plain(w.run('Nodes.attention()'))[0].dk;
  answer(w, 200, { nodes: [withSessions('build-box', [sess('shop--api--s1', { since: iso(30) })])], at: iso(0) }, 'W/"n2"');
  await poll(w);
  assert.equal(rows(w).length, 1, 'still one row: the same session, a newer event');
  assert.notEqual(plain(w.run('Nodes.attention()'))[0].dk, key1, 'a different `since` is a different item');
  answer(w, 200, { nodes: [withSessions('build-box', [sess('shop--api--s1', { needs_you: false, state: 'working' })])], at: iso(0) }, 'W/"n3"');
  await poll(w);
  assert.equal(rows(w).length, 0, 'the node stopped listing it');
  assert.ok(group(w).classList.contains('hidden'), 'and with nothing left the group is gone');
  assert.deepEqual(plain(w.run('Nodes.attention()')), []);
});

test('each row carries the node chip, the session, the kind in words and the age; an error is told apart from a question', async () => {
  const { w } = await ready([withSessions('build-box', [sess('shop--api--s1', { since: iso(300) }), sess('shop--api--s3', { state: 'errored', since: iso(120) })])]);
  await go(w, '#/inbox');
  const [a, b] = rows(w);
  assert.equal(text(a.querySelector('.nchip-n')), 'build-box');
  assert.equal(text(a.querySelector('a.nd-name')), 's1');
  assert.match(text(a.querySelector('.nd-meta')), /^needs input · 5m/);
  assert.equal(text(b.querySelector('.nchip-n')), 'build-box');
  assert.match(text(b.querySelector('.nd-meta')), /^error · 2m/);
});

test('an item of a node that is not online is marked stale with the age of its last reading; an online one is not', async () => {
  const { w } = await ready([withSessions('build-box', [sess('shop--api--s1')]), withSessions('alice-mac', [sess('notes--site--s1')], { status: 'stale', age_s: 190 })]);
  await go(w, '#/inbox');
  const [live, old] = rows(w);
  assert.ok(!live.classList.contains('stale'));
  assert.doesNotMatch(text(live.querySelector('.nd-meta')), /stale/);
  assert.ok(old.classList.contains('stale'));
  assert.match(text(old.querySelector('.nd-meta')), /stale: last reading 3 min ago/);
});

test('a session of the same name on this board and on a node is two items, and the local card is not duplicated', async () => {
  const st = boardState();
  const { w } = await ready([withSessions('build-box', [sess('ccboard--ccboard--s2')])]);
  setState(w, st);
  w.run('updateCurrentPage(state)');
  await go(w, '#/inbox');
  assert.equal(all(page(w), '.inbox-list .inbox-card').length, 1, 'this board lists its own waiting session once');
  assert.equal(rows(w).length, 1, 'and the node lists its own once');
  assert.deepEqual(plain(w.run('Nodes.attention().map((s) => [s.node, s.tmux])')), [['build-box', 'ccboard--ccboard--s2']]);
});

test('a hostile node or session name is text in the row, never markup', async () => {
  const HOSTILE = '<img src=x onerror=alert(1)>';
  const { w } = await ready([withSessions('evil', [sess('shop--api--s1', { session: HOSTILE })], { name: HOSTILE })]);
  await go(w, '#/inbox');
  assert.ok(text(group(w)).includes(HOSTILE));
  assert.equal(all(w, 'img, iframe, script').length, 0);
});

// ---------------------------------------------------------------- one line for one account window

test('two nodes under one subscription give one line for the window; different subscriptions give a line each', async () => {
  const shared = [limitedNode('build-box', [acct('claude', KEY)]), limitedNode('alice-mac', [acct('claude', KEY)])];
  const { w } = await ready(shared);
  const lines = plain(w.run('Nodes.limitLines()'));
  assert.equal(lines.length, 1);
  assert.equal(lines[0], 'Claude limit reached on build-box and alice-mac: one account, one window. Each node sends its own notice; turn it off on one (Settings > Notifications).');
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  assert.deepEqual(all(w, '.nd-lims .nd-lim').map(text), lines, 'the Home strip shows it');
  answer(w, 200, { nodes: [limitedNode('build-box', [acct('claude', KEY)]), limitedNode('alice-mac', [acct('claude', 'ffffffffffff')])], at: iso(0) }, 'W/"d"');
  await poll(w);
  assert.deepEqual(plain(w.run('Nodes.limitLines()')), ['Claude limit reached on build-box.', 'Claude limit reached on alice-mac.']);
  assert.deepEqual(all(w, '.nd-lims .nd-lim').map(text), ['Claude limit reached on build-box.', 'Claude limit reached on alice-mac.']);
});

test('no line while the window is not used up, for a node that is offline, for a saved login nobody uses, or for an account without a key it shares', async () => {
  const open = limitedNode('build-box', [acct('claude', KEY)]);
  open.state.usage.claude.limited = false;
  const { w } = await ready([open, limitedNode('alice-mac', [acct('claude', KEY, { current: false })]), limitedNode('old-laptop', [acct('claude', KEY)], { status: 'offline', age_s: 21600 })]);
  assert.deepEqual(plain(w.run('Nodes.limitLines()')), []);
  answer(w, 200, { nodes: [limitedNode('build-box', [acct('claude', null)]), limitedNode('alice-mac', [acct('claude', null)])], at: iso(0) }, 'W/"k"');
  await poll(w);
  assert.equal(plain(w.run('Nodes.limitLines()')).length, 2, 'no key, no claim of a shared window');
});

test('three nodes under one subscription: one line naming all three', async () => {
  const { w } = await ready([limitedNode('a-one', [acct('claude', KEY)]), limitedNode('b-two', [acct('claude', KEY)]), limitedNode('c-three', [acct('claude', KEY)])]);
  assert.match(plain(w.run('Nodes.limitLines()'))[0], /^Claude limit reached on a-one, b-two and c-three: one account, one window\./);
});

// ---------------------------------------------------------------- Settings > Notifications

async function settingsWorld(nodes) {
  const h = hubWorld({ state: boardState(), nodes });
  h.w.load('pages/settings.js');
  await poll(h.w);
  h.w.run('settingsNotifyState.at = Date.now(); settingsNotifyState.samples = {}; globalThis.pushSubscription = async () => null;');
  await go(h.w, '#/settings');
  await tick();
  return h.w;
}
const nrows = (w) => all(page(w), '.nd-notify .nd-nrow');

test('Settings > Notifications has a row for each paired node, saying this page only knows this device, with Open <node> in a new tab', async () => {
  const w = await settingsWorld([rec('build-box'), rec('alice-mac', { status: 'stale', age_s: 190 })]);
  const sec = page(w).querySelector('.nd-notify');
  assert.ok(sec && !sec.classList.contains('hidden'));
  assert.equal(text(sec.querySelector('h2')), 'Other nodes');
  assert.deepEqual(nrows(w).map((r) => text(r.querySelector('.nd-ntext'))), ['Notifications from build-box: subscribe this device on that node', 'Notifications from alice-mac: subscribe this device on that node']);
  assert.match(text(sec.querySelector('.nd-ro')), /only knows this device on this board; it cannot see whether it is subscribed on another node/);
  const a = nrows(w)[0].querySelector('a');
  assert.equal(text(a), 'Open build-box');
  assert.deepEqual([a.getAttribute('href'), a.getAttribute('target'), a.getAttribute('rel')], ['https://build-box.example.ts.net/#/settings', '_blank', 'noopener noreferrer']);
  assert.equal(a.getAttribute('href'), w.run("Nodes.openUrl(Nodes.get('build-box'), '#/settings')"), 'the link builder made it');
  const order = Array.from(page(w).querySelector('.settings-panel[data-sec=notify]').children).map((n) => n.className || n.tagName);
  assert.deepEqual(order.slice(0, 4), ['set-h', 'kv', 'nd-notify', 'set-notify-kinds'], 'under This device, before What to notify');
});

test('an address that is not a tailnet https address gives no link, only a disabled button and the reason', async () => {
  const w = await settingsWorld([rec('odd', { url: 'http://odd.example.com/x' })]);
  const row = nrows(w)[0];
  assert.equal(row.querySelector('a'), null);
  const b = row.querySelector('button');
  assert.deepEqual([text(b), b.getAttribute('aria-disabled')], ['Open odd', 'true']);
  assert.match(text(row), /Its saved address is not an https tailnet address\./);
});

test('no row without a paired node, and a node name is text', async () => {
  const none = await settingsWorld([]);
  const sec = page(none).querySelector('.nd-notify');
  assert.ok(!sec || sec.classList.contains('hidden'));
  const HOSTILE = '<img src=x onerror=alert(1)>';
  const w = await settingsWorld([rec('evil', { name: HOSTILE })]);
  assert.ok(text(nrows(w)[0]).includes(HOSTILE));
  assert.equal(all(w, 'img, iframe, script').length, 0);
});

test('?demo=1 shows the rows for the demo nodes, the shared account line on Home and this board\'s name in the examples', async () => {
  const { w } = hubWorld({ search: '?demo=1', realApi: true });
  w.load('pages/settings.js');
  w.run('globalThis.pushSubscription = async () => null;');
  setState(w, plain(await w.run("api('GET', '/api/state')")));
  await poll(w);
  await go(w, '#/settings');
  await tick();
  assert.deepEqual(nrows(w).map((r) => text(r.querySelector('.nd-ntext'))), ['build-box', 'alice-mac', 'old-laptop'].map((n) => `Notifications from ${n}: subscribe this device on that node`));
  assert.equal(plain(w.run('Nodes.limitLines()')).length, 1, 'the demo nodes build-box and alice-mac share one account');
  assert.match(plain(w.run('settingsNotifyDemo().samples.needs.title')), /^box · ◆ shop\/api · s1: needs you$/);
});

// ---------------------------------------------------------------- the badge stays this board's own

test('the header count, which is also the app badge, is this board\'s own: items of other nodes never add to it', async () => {
  const { w } = await ready([withSessions('build-box', [sess('shop--api--s1'), sess('shop--api--s2')]), withSessions('alice-mac', [sess('notes--site--s1')])]);
  setState(w, boardState());
  const local = w.run('Inbox.items(state).length');
  assert.equal(local, 1);
  assert.equal(plain(w.run('Nodes.attention()')).length, 3, 'three items wait on other nodes');
  assert.equal(w.run('Shell.model(state).attn'), local, 'and the badge still counts the one on this board');
});

test('a section that left the page lets go of its host (a Settings panel rebuilt every minute does not pile up)', async () => {
  const w = await settingsWorld([rec('build-box')]);
  const before = w.run('Nodes.hosts.size');
  for (let i = 0; i < 3; i += 1) { w.run('settingsFill("notify", true)'); await tick(); await poll(w); }
  const after = w.run('Nodes.hosts.size');
  assert.ok(after <= before + 1, `hosts ${before} -> ${after}`);
});
