// Settings > Nodes (issue #134): three labelled sections (This node, Paired nodes, Found on your tailnet), the tailnet list asked for only when a person opens the tab or presses
// Refresh (never on a timer, never from a poll), a probe result in plain words per row, a Pair button that opens the Add node sheet (the pairing tests below), and every word the board's answer
// holds about a device shown as text. Real core.js, components.js, router.js, nodes.js and pages/settings.js on minidom's DOM; api() is the recorder of world.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { makeWorld } from './harness.mjs';
import { calls, fakeState, homeWorld, page, plain, tick, text } from './world.mjs';

const NOW_S = Math.floor(Date.now() / 1000);
const CARD = {
  app: 'ccboard', api: 1, node_id: 'ts:nDEMO1CNTRL', name: 'box', os: { system: 'Linux', release: '6.8.0-demo', tailscale_os: 'linux' },
  lanes: { cap: 3, running: 1, free: 2 }, handle: 'local', now: '2026-10-03T03:00:00.000+00:00',
};
const AGENTS = { claude: { installed: true, version: '2.1.288', loggedIn: true }, codex: { installed: true, version: '0.160.0', loggedIn: false }, shell: { installed: false } };

// A row as app/nodes_discovery.py _out_row answers it (flat: state, url, node_id, at, age, stale), times as ISO stamps on the box's clock.
const iso = (secsAgo) => new Date((NOW_S - secsAgo) * 1000).toISOString();
const row = (over) => ({ ts_id: 'r', name: 'r', dns_name: 'r.example.ts.net.', os: 'linux', online: true, last_seen: null, owner: 'user', tags: [], state: 'unchecked', url: null, node_id: null, at: null, age: null, stale: false, ...over });
const ROWS = [
  row({ ts_id: 'n1', name: 'node-a', dns_name: 'node-a.example.ts.net.', state: 'found', url: 'https://node-a.example.ts.net:8443', node_id: 'ts:nNODEA', at: iso(5), age: 5 }),
  row({ ts_id: 'n2', name: 'node-b', dns_name: 'node-b.example.ts.net.', os: 'macOS', owner: 'tag', tags: ['tag:ccboard'], state: 'refuses', at: iso(5), age: 5 }),
  row({ ts_id: 'n3', name: 'node-c', dns_name: 'node-c.example.ts.net.', os: 'windows', online: false, last_seen: iso(3 * 3600), state: 'offline' }),
  row({ ts_id: 'n4', name: 'node-d', dns_name: 'node-d.example.ts.net.', state: 'no_ccboard', at: iso(100), age: 100, stale: true }),
];
const ANSWER = { at: iso(0), tailscale: { ok: true, reason: null, variant: 'cli' }, rows: ROWS };

function nodesWorld({ answer = ANSWER, over = {}, hash = '#/settings?sec=nodes' } = {}) {
  const { w } = homeWorld({ state: fakeState({ node: CARD, node_name: 'box', agents: AGENTS, ...over }) });
  w.ctx.__answers['/api/nodes/discover'] = answer;
  if (hash) w.location.hash = hash;
  return w;
}
const panel = (w) => page(w).querySelector('.settings-panel[data-sec=nodes]');
const asks = (w) => calls(w).filter((c) => c.path.startsWith('/api/nodes/discover'));
const rowsOf = (w) => panel(w).querySelectorAll('.nd-row');
const rowNamed = (w, name) => rowsOf(w).find((r) => text(r.querySelector('.nd-name')) === name);
const chips = (r) => r.querySelectorAll('.nd-chips .badge').map(text);

// ---------------------------------------------------------------- the three sections

test('five labelled sections in order: This node, Paired nodes, Found on your tailnet, Who can control this node, Activity', async () => {
  const w = nodesWorld();
  await tick();
  assert.deepEqual(panel(w).querySelectorAll('h2.set-h').map(text), ['This node', 'Paired nodes', 'Found on your tailnet', 'Who can control this node', 'Activity']);
});

test('This node: name, short node id with a Copy, system, agents and free lanes, all from state.node', async () => {
  const w = nodesWorld();
  await tick();
  const kv = Object.fromEntries(panel(w).querySelectorAll('.kv').map((r) => [text(r.querySelector('.k')), r]));
  assert.equal(text(kv.Name.querySelector('.kv-main')), 'box');
  assert.equal(text(kv['Node id'].querySelector('.kv-main')), 'ts:nDEMO1CNTRL');
  assert.ok(kv['Node id'].querySelector('.copy-btn'), 'the full id can be copied');
  assert.equal(text(kv.System.querySelector('.kv-main')), 'Linux 6.8.0-demo');
  assert.deepEqual(kv.Agents.querySelectorAll('.badge').map(text), ['◆ Claude 2.1.288', '◇ Codex 0.160.0, not logged in'], 'installed agents only; a logout is said in words');
  assert.match(text(kv['Free lanes']), /^Free lanes2 of 3/);
});

test('a long node id is cut in the middle, the full one stays in the title', async () => {
  const id = 'ccb:3f9c1a7d0b25a81e5c20d7f4';
  const w = nodesWorld({ over: { node: { ...CARD, node_id: id } } });
  await tick();
  const v = panel(w).querySelectorAll('.kv').find((r) => text(r.querySelector('.k')) === 'Node id').querySelector('.v');
  assert.equal(text(v), 'ccb:3f9c...d7f4');
  assert.equal(v.getAttribute('title'), id);
});

test('a board whose state has no node card says so instead of throwing', async () => {
  const w = nodesWorld({ over: { node: undefined } });
  await tick();
  assert.match(text(panel(w)), /has not sent its node card yet/);
});

test('Paired nodes: empty says what is true and what to press', async () => {
  const w = nodesWorld();
  await tick();
  assert.match(text(panel(w)), /No node is paired\. Press Add node and type the address and code the other board shows, or press Pair on a device found below\./);
});

test('Paired nodes: the CCBOARD_NODES rows keep their https-only Open link and their offline text', async () => {
  const nodes = { value: [
    { name: 'mini', online: true, sessions: 3, attention: 1, url: 'https://mini.example.ts.net', health: { cpu_pct: 7 } },
    { name: 'old', online: false, error: 'timed out', url: 'http://old.example.ts.net' },
  ], at: 1 };
  const w = nodesWorld({ over: { nodes } });
  await tick();
  const kv = panel(w).querySelectorAll('.kv').filter((r) => ['mini', 'old'].includes(text(r.querySelector('.k'))));
  assert.equal(kv.length, 2);
  assert.match(text(kv[0]), /3 sess · 1 need you · cpu 7%/);
  assert.equal(kv[0].querySelector('a').getAttribute('href'), 'https://mini.example.ts.net/');
  assert.match(text(kv[1]), /offline · timed out/);
  assert.equal(kv[1].querySelector('a'), null, 'an http address gets no link');
  assert.doesNotMatch(text(panel(w)), /No node is paired/);
});

// ---------------------------------------------------------------- when the tailnet is asked

test('opening the Nodes tab asks once, with a plain GET; a poll repaint does not ask again', async () => {
  const w = nodesWorld();
  await tick();
  assert.deepEqual(asks(w).map((c) => [c.method, c.path]), [['GET', '/api/nodes/discover']]);
  for (let i = 0; i < 3; i++) w.run('updateCurrentPage(state)');
  await tick();
  assert.equal(asks(w).length, 1, 'no poll, no timer');
});

test('a Settings page opened on another tab makes no discovery request, and neither does a board with no tailnet until the tab opens', async () => {
  const w = nodesWorld({ hash: '#/settings?sec=box' });
  await tick();
  assert.equal(asks(w).length, 0);
  w.location.hash = '#/settings?sec=notify';
  await tick();
  assert.equal(asks(w).length, 0);
  w.location.hash = '#/settings?sec=nodes';
  await tick();
  assert.equal(asks(w).length, 1, 'the tab opening is the ask');
});

test('coming back to the tab inside 30 s does not ask again; the Refresh button does, with ?refresh=1', async () => {
  const w = nodesWorld();
  await tick();
  w.location.hash = '#/settings?sec=box';
  w.location.hash = '#/settings?sec=nodes';
  await tick();
  assert.equal(asks(w).length, 1);
  panel(w).querySelectorAll('button').find((b) => text(b) === 'Refresh').click();
  await tick();
  assert.deepEqual(asks(w).map((c) => c.path), ['/api/nodes/discover', '/api/nodes/discover?refresh=1']);
});

test('Refresh keeps its button (focus survives), ignores a press while one ask runs, and the list keeps its row nodes when nothing changed', async () => {
  const w = nodesWorld();
  await tick();
  const btn = () => panel(w).querySelectorAll('button').find((b) => /^Refresh/.test(text(b)));
  const before = btn();
  const rowA = rowNamed(w, 'node-a');
  let release;
  w.ctx.__answers['/api/nodes/discover'] = () => new Promise((r) => { release = () => r(ANSWER); });
  before.click();
  before.click();
  before.click();
  assert.equal(asks(w).length, 2, 'one more ask, not three');
  assert.equal(btn(), before, 'the same button while busy');
  assert.equal(before.getAttribute('aria-busy'), 'true');
  assert.equal(before.getAttribute('disabled'), null, 'aria-busy, not disabled: a disabled button drops the focus');
  assert.equal(text(before), 'Refreshing');
  assert.match(text(panel(w).querySelector('.nd-status')), /Looking at the tailnet/);
  release();
  await tick();
  assert.equal(btn(), before);
  assert.equal(text(before), 'Refresh');
  assert.equal(before.getAttribute('aria-busy'), 'false');
  assert.equal(rowNamed(w, 'node-a'), rowA, 'an unchanged row is not recreated under a finger');
});

test('no timer: the page script has no setInterval, setTimeout or poll for the tailnet', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'settings.js'), 'utf8');
  const start = src.indexOf('const settingsNd =');
  const end = src.indexOf('function settingsBox');
  const block = src.slice(start, end).replace(/\/\*[\s\S]*?\*\//g, '');
  assert.ok(start > 0 && end > start);
  assert.doesNotMatch(block, /setInterval|setTimeout|requestAnimationFrame|visibilitychange/);
  assert.equal((block.match(/api\('GET'/g) || []).length, 1, 'one place asks');
});

// ---------------------------------------------------------------- the rows

test('Found on your tailnet: one row per device, in the board\'s order, each with name, system, whose it is, the online word and the probe result in words', async () => {
  const w = nodesWorld();
  await tick();
  assert.deepEqual(rowsOf(w).map((r) => text(r.querySelector('.nd-name'))), ['node-a', 'node-b', 'node-c', 'node-d']);
  const a = rowNamed(w, 'node-a');
  assert.deepEqual(chips(a), ['Linux', 'your device', '✓ ccboard answers']);
  assert.equal(text(a.querySelector('.nd-online')), 'on the tailnet');
  assert.match(text(a.querySelector('.nd-note')), /Answers at node-a\.example\.ts\.net:8443\. Checked \d+s ago\./);
  assert.deepEqual(chips(rowNamed(w, 'node-b')), ['macOS', 'tag', '! ccboard refuses us']);
  assert.deepEqual(chips(rowNamed(w, 'node-d')), ['Linux', 'your device', '○ nothing listening']);
  const c = rowNamed(w, 'node-c');
  assert.deepEqual(chips(c), ['Windows', 'your device', '○ offline']);
  assert.equal(text(c.querySelector('.nd-online')), 'offline, last seen 3h ago');
  assert.doesNotMatch(text(c.querySelector('.nd-note')), /Checked/, 'an offline device was never probed: no check time');
  assert.equal(c.querySelector('.nd-online .dot').classList.contains('unknown'), true);
  assert.equal(a.querySelector('.nd-online .dot').classList.contains('unknown'), false);
});

test('every probe state reads in plain words and with a glyph: no state is colour only', async () => {
  const states = ['found', 'refuses', 'no_ccboard', 'no_tls', 'unreachable', 'offline', 'invalid'];
  const rows = [...states, 'unchecked'].map((s, i) => row({ ts_id: `s${i}`, name: `n-${s}`, online: s !== 'offline', state: s, url: 'https://n.example.ts.net', at: iso(1) }));
  const w = nodesWorld({ answer: { ...ANSWER, rows } });
  await tick();
  const words = rowsOf(w).map((r) => chips(r).at(-1));
  assert.deepEqual(words, ['✓ ccboard answers', '! ccboard refuses us', '○ nothing listening', '! no HTTPS', '✕ unreachable', '○ offline', '✕ invalid name', '· not checked yet']);
  assert.match(text(rowsOf(w).at(-1).querySelector('.nd-note')), /^Press Refresh to check this device\.$/, 'an unchecked device was not probed: no check time, and it says what to do');
  assert.equal(rowsOf(w).at(-1).querySelector('button'), null, 'nothing to pair until it answered');
});

test('Pair is a real button, only a node that answered has one, and it is not a second filled primary', async () => {
  const w = nodesWorld();
  await tick();
  const pairs = panel(w).querySelectorAll('.nd-row button');
  assert.equal(pairs.length, 1);
  const [pair] = pairs;
  assert.equal(text(pair), 'Pair');
  assert.equal(pair.getAttribute('disabled'), null, 'pairing exists now: the button can be pressed');
  assert.equal(pair.getAttribute('title'), null);
  assert.equal(pair.getAttribute('aria-label'), 'Pair node-a');
  assert.equal(pair.classList.contains('tinted'), true, 'the quiet row action, not a second filled primary');
  assert.equal(panel(w).querySelectorAll('button.primary').filter((b) => !b.classList.contains('tinted')).length, 0, 'no filled primary in the section');
  assert.equal(rowNamed(w, 'node-b').querySelector('button'), null);
  assert.equal(rowNamed(w, 'node-a').getAttribute('data-key'), 'n1');
});

test('the caption says finding gives no access and what online means', async () => {
  const w = nodesWorld();
  await tick();
  assert.match(text(panel(w)), /Finding a device gives it no access to this board: only pairing does\. "On the tailnet" means the device is connected to Tailscale, not that ccboard answers there\./);
});

test('a row older than 30 s is dimmed, says stale and how old, and is still listed', async () => {
  const rows = [{ ...ROWS[0], at: iso(125), age: 125, stale: true }];
  const w = nodesWorld({ answer: { ...ANSWER, rows } });
  await tick();
  const [r] = rowsOf(w);
  assert.equal(r.classList.contains('stale'), true);
  assert.match(text(r.querySelector('.nd-note')), /Checked 2m ago, stale: press Refresh\./);
  assert.equal(rowsOf(w).length, 1, 'stale rows are never deleted');
});

test('ages are on the box\'s clock: a browser clock an hour off does not make a fresh row stale or an old one fresh', async () => {
  const off = (s) => new Date((NOW_S - 3600 - s) * 1000).toISOString();           // the box says it is an hour earlier than this browser thinks
  const rows = [{ ...ROWS[0], at: off(5) }, { ...ROWS[3], at: off(100) }];
  const w = nodesWorld({ answer: { ...ANSWER, at: off(0), rows } });
  await tick();
  const [fresh, old] = rowsOf(w);
  assert.equal(fresh.classList.contains('stale'), false);
  assert.match(text(fresh.querySelector('.nd-note')), /Checked [5-9]s ago\.$/);
  assert.equal(old.classList.contains('stale'), true);
  assert.match(text(old.querySelector('.nd-note')), /Checked 1m ago, stale/);
  assert.match(text(panel(w).querySelector('.nd-status')), /^Looked \d+s ago\.$/);
});

test('two peers with one host name stay two rows (keyed by the Tailscale id)', async () => {
  const twin = (id) => row({ ts_id: id, name: 'laptop', dns_name: `laptop-${id}.example.ts.net.`, os: 'macOS', state: 'no_ccboard', at: iso(1) });
  const w = nodesWorld({ answer: { ...ANSWER, rows: [twin('t1'), twin('t2')] } });
  await tick();
  assert.deepEqual(rowsOf(w).map((r) => r.getAttribute('data-key')), ['t1', 't2']);
});

test('a repeated Tailscale id in the answer is listed once (the first one wins)', async () => {
  const w = nodesWorld({ answer: { ...ANSWER, rows: [ROWS[0], { ...ROWS[3], ts_id: 'n1', name: 'impostor' }] } });
  await tick();
  assert.deepEqual(rowsOf(w).map((r) => text(r.querySelector('.nd-name'))), ['node-a']);
});

test('a keyed repaint: a changed row is rebuilt, the others keep their nodes, new order is followed, a vanished row goes', async () => {
  const w = nodesWorld();
  await tick();
  const [a, b, , d] = rowsOf(w);
  const changed = { ...ROWS[1], state: 'found', url: 'https://node-b.example.ts.net', node_id: 'ts:nB', at: iso(0) };
  w.ctx.__answers['/api/nodes/discover'] = { ...ANSWER, rows: [ROWS[3], changed, ROWS[0]] };
  panel(w).querySelectorAll('button').find((x) => text(x) === 'Refresh').click();
  await tick();
  const now = rowsOf(w);
  assert.deepEqual(now.map((r) => r.getAttribute('data-key')), ['n4', 'n2', 'n1']);
  assert.equal(now[0], d, 'unchanged and moved, not rebuilt');
  assert.equal(now[2], a);
  assert.notEqual(now[1], b, 'node-b changed state, so its row is new');
  assert.deepEqual(chips(now[1]).at(-1), '✓ ccboard answers');
  assert.equal(panel(w).querySelectorAll('.nd-row').length, 3, 'node-c is gone from the list');
});

test('a node already paired is not listed twice (matched by host; a claimed node id alone hides nothing)', async () => {
  const nodes = { value: [
    { name: 'a-paired', online: true, sessions: 0, attention: 0, url: 'https://node-a.example.ts.net:8443' },
    { name: 'b-paired', online: true, sessions: 0, attention: 0, url: 'https://elsewhere.example.ts.net', node_id: 'ts:nOTHER' },
  ], at: 1 };
  const w = nodesWorld({ over: { nodes } });
  await tick();
  assert.deepEqual(rowsOf(w).map((r) => text(r.querySelector('.nd-name'))), ['node-b', 'node-c', 'node-d'], 'node-a is paired by its host');
  const byId = { ...ANSWER, rows: [{ ...ROWS[1], state: 'found', url: 'https://node-b.example.ts.net', node_id: 'ts:nOTHER', at: iso(0) }] };
  const w2 = nodesWorld({ over: { nodes }, answer: byId });
  await tick();
  assert.deepEqual(rowsOf(w2).map((r) => text(r.querySelector('.nd-name'))), ['node-b'], 'a paired board at another address claiming this id does not hide it');
});

// ---------------------------------------------------------------- states

test('Tailscale logged out: the reason, the command to copy, no rows, no error screen', async () => {
  const answer = { at: iso(0), tailscale: { ok: false, reason: 'Tailscale is not logged in on this device', variant: 'cli' }, rows: [] };
  const w = nodesWorld({ answer });
  await tick();
  const p = panel(w);
  assert.match(text(p.querySelector('.nd-problem .set-err')), /^Tailscale is not logged in on this device$/);
  assert.equal(text(p.querySelector('.nd-fix code')), 'tailscale up');
  assert.ok(p.querySelector('.nd-fix .copy-btn'), 'the remedy can be copied');
  assert.match(text(p.querySelector('.nd-fix')), /then press Refresh\./);
  assert.equal(rowsOf(w).length, 0);
  assert.equal(p.querySelector('.nd-empty').classList.contains('hidden'), true, 'the reason replaces the empty text');
  assert.equal(p.querySelector('.nd-problem').classList.contains('hidden'), false);
});

test('the fix line follows the board\'s own reasons (app/nodes_discovery.py read_tailscale, app/tailscale.py missing_reason), command line or Mac app', async () => {
  const cases = [
    ['Tailscale is not logged in on this device: run `tailscale up`', 'cmd'],
    ['Tailscale is not signed in on this device: open the Tailscale app and sign in', 'Do that on this device, then press Refresh.'],
    ['Tailscale is not running or cannot be reached from the board: open the Tailscale app and make sure it is signed in', 'Do that on this device, then press Refresh.'],
    ['Tailscale is not running or cannot be reached from the board: start it, then try again', 'Start Tailscale on this device, then press Refresh.'],
    ['MagicDNS is not turned on for this tailnet, so devices cannot be found by name: turn it on in the Tailscale admin console (DNS page)', 'Turn on MagicDNS in the Tailscale admin console (DNS page), then press Refresh.'],
    ['tailscale is not installed (https://tailscale.com/download)', 'Install Tailscale on this device and sign in, then press Refresh.'],
    ['tailscale was not found on PATH, in /usr/local/bin, in /opt/homebrew/bin or in /Applications/Tailscale.app; install Tailscale from the App Store', 'Install Tailscale on this device and sign in, then press Refresh.'],
    ['Finding nodes failed (OSError); try again', 'Check Tailscale on this device, then press Refresh.'],
    ['something else', 'Check Tailscale on this device, then press Refresh.'],
  ];
  for (const [reason, want] of cases) {
    const w = nodesWorld({ answer: { at: iso(0), tailscale: { ok: false, reason, variant: null }, rows: [] } });
    await tick();
    const fix = panel(w).querySelector('.nd-fix');
    assert.equal(text(panel(w).querySelector('.nd-problem .set-err')), reason, 'the reason is shown as the board wrote it');
    if (want === 'cmd') assert.equal(text(fix.querySelector('code')), 'tailscale up', reason);
    else { assert.equal(text(fix), want, reason); assert.equal(fix.querySelector('code'), null, `${reason}: no command to copy`); }
  }
});

test('the problem goes away when the next answer is fine, and rows come back', async () => {
  const w = nodesWorld({ answer: { at: iso(0), tailscale: { ok: false, reason: 'Tailscale is not logged in on this device' }, rows: [] } });
  await tick();
  w.ctx.__answers['/api/nodes/discover'] = ANSWER;
  panel(w).querySelectorAll('button').find((b) => text(b) === 'Refresh').click();
  await tick();
  assert.equal(panel(w).querySelector('.nd-problem').classList.contains('hidden'), true);
  assert.equal(rowsOf(w).length, 4);
});

test('an empty tailnet says what to do next', async () => {
  const w = nodesWorld({ answer: { ...ANSWER, rows: [] } });
  await tick();
  const empty = panel(w).querySelector('.nd-empty');
  assert.equal(empty.classList.contains('hidden'), false);
  assert.match(text(empty), /No other devices of yours were found on the tailnet\. Install ccboard on another device, or tag it for ccboard in the tailnet, then press Refresh\./);
});

test('an ask that fails keeps the last list on screen and says what happened; an answer with no list is an error too', async () => {
  const w = nodesWorld();
  await tick();
  w.ctx.__answers['/api/nodes/discover'] = () => { throw new Error('HTTP 502'); };
  panel(w).querySelectorAll('button').find((b) => text(b) === 'Refresh').click();
  await tick();
  assert.equal(rowsOf(w).length, 4, 'the last good list stays');
  const status = panel(w).querySelector('.nd-status');
  assert.match(text(status), /^Could not ask the board: HTTP 502\. The list below is its last answer\.$/);
  assert.equal(status.classList.contains('bad'), true);
  w.ctx.__answers['/api/nodes/discover'] = { ok: true };
  panel(w).querySelectorAll('button').find((b) => text(b) === 'Refresh').click();
  await tick();
  assert.match(text(panel(w).querySelector('.nd-status')), /answered without a list/);
  const w2 = nodesWorld({ answer: () => { throw new Error('HTTP 500'); } });
  await tick();
  assert.match(text(panel(w2).querySelector('.nd-status')), /^Could not ask the board: HTTP 500\. Press Refresh to try again\.$/);
});

test('leaving the page drops an answer still on its way: nothing paints, nothing throws, and the next visit asks afresh', async () => {
  let release = null;
  const w = nodesWorld({ answer: () => new Promise((r) => { release = () => r(ANSWER); }) });
  assert.equal(asks(w).length, 1);
  assert.equal(typeof release, 'function');
  w.location.hash = '#/';
  release();
  await tick();
  w.ctx.__answers['/api/nodes/discover'] = ANSWER;
  w.location.hash = '#/settings?sec=nodes';
  await tick();
  assert.equal(rowsOf(w).length, 4, 'the new visit shows its own answer');
  assert.equal(asks(w).length, 2, 'a new visit asks again');
});

// ---------------------------------------------------------------- hostile text

test('a hostile device name, tag, system or address is text: no element is made from it', async () => {
  const evil = '<img src=x onerror=alert(1)>';
  const rows = [
    row({ ts_id: 'x1', name: evil, dns_name: 'x1.example.ts.net.', os: '<script>alert(2)</script>', owner: 'tag', tags: [evil], state: 'found', url: 'https://x1.example.ts.net:8443', node_id: evil, at: iso(1) }),
    row({ ts_id: 'x2', name: 'javascript:alert(3)', dns_name: 'x2.example.ts.net.', state: 'found', url: 'javascript:alert(4)', at: iso(1) }),
    row({ ts_id: 'x3', name: 'x3', state: 'constructor', url: 'https://x3.example.ts.net' }),
    row({ ts_id: 'x4', name: 'x4', state: '__proto__' }),
    row({ ts_id: 'x5', name: 'x5', owner: 'someone-else', state: 'found', url: 'https://u:p@x5.example.ts.net', at: iso(1) }),
  ];
  const w = nodesWorld({ answer: { ...ANSWER, rows }, over: { node: { ...CARD, name: evil } } });
  await tick();
  const p = panel(w);
  assert.equal(p.querySelectorAll('img').length, 0);
  assert.equal(p.querySelectorAll('script').length, 0);
  assert.equal(text(rowNamed(w, evil).querySelector('.nd-name')), evil);
  assert.deepEqual(chips(rowNamed(w, evil)), ['<script>alert(2)</sc', 'tag', '✓ ccboard answers']);
  assert.doesNotMatch(text(rowNamed(w, 'javascript:alert(3)').querySelector('.nd-note')), /javascript:alert\(4\)/, 'an address that is not http(s) is not shown');
  assert.equal(p.querySelectorAll('a').filter((a) => /javascript:/i.test(a.getAttribute('href') || '')).length, 0);
  assert.deepEqual(chips(rowNamed(w, 'x3')).slice(-1), ['· not checked yet'], 'a state outside the table is never read as found');
  assert.deepEqual(chips(rowNamed(w, 'x4')).slice(-1), ['· not checked yet']);
  assert.deepEqual(chips(rowNamed(w, 'x5')), ['Linux', '✓ ccboard answers'], 'an owner word outside user / tag is not shown');
  assert.doesNotMatch(text(rowNamed(w, 'x5')), /u:p/, 'an address with user info is not shown');
  assert.equal(text(p.querySelectorAll('.kv').find((r) => text(r.querySelector('.k')) === 'Name').querySelector('.kv-main')), evil, 'this node\'s own name is text too');
});

test('settings.js and nodes.js build no markup from text: no innerHTML, no inline style, no cssText', () => {
  for (const f of ['pages/settings.js', 'nodes.js']) {
    const code = fs.readFileSync(path.join(STATIC, f), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
    assert.doesNotMatch(code, /innerHTML|insertAdjacentHTML|outerHTML|cssText|style=|\.style\b/, f);
  }
});

// ---------------------------------------------------------------- demo mode

test('demo mode: /api/nodes/discover is served from demo/nodes-discover.json, its times follow the clock, and a Refresh answers as if every probe just ran', async () => {
  const back = (s) => iso(86400 + s);                   // a day before now: the clock of the fixture
  const fixture = { demo: { epoch: back(0) }, at: back(0), tailscale: { ok: true, reason: null, variant: 'cli' }, rows: [
    row({ ts_id: 'd1', name: 'node-a', state: 'found', url: 'https://node-a.example.ts.net', at: back(70), age: 70, stale: true }),
    row({ ts_id: 'd2', name: 'node-c', online: false, last_seen: back(7200), state: 'offline' }),
  ] };
  const asked = [];
  const w = makeWorld({ fetch: async (url) => { asked.push(String(url)); return { ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(JSON.stringify(fixture)) }; } });
  w.location.search = '?demo=1';
  w.load('core.js');
  const plainAns = await w.run('api("GET", "/api/nodes/discover")');
  assert.deepEqual(asked, ['/static/demo/nodes-discover.json']);
  const secs = (s) => Date.parse(s) / 1000;
  assert.ok(Math.abs(secs(plainAns.at) - NOW_S) <= 3, 'the answer was made now');
  assert.ok(Math.abs(secs(plainAns.rows[0].at) - (NOW_S - 70)) <= 3, 'a probe keeps its age');
  assert.ok(Math.abs(secs(plainAns.rows[1].last_seen) - (NOW_S - 7200)) <= 3, 'last seen follows the clock');
  const fresh = await w.run('api("GET", "/api/nodes/discover?refresh=1")');
  assert.ok(Math.abs(secs(fresh.at) - NOW_S) <= 3);
  assert.ok(Math.abs(secs(fresh.rows[0].at) - NOW_S) <= 3, 'a refresh just probed');
  assert.equal(fresh.rows[0].stale, false);
  assert.equal(fresh.rows[1].at, null, 'an offline row has no check time, refreshed or not');
});

test('the shipped demo fixture, if it is there, renders the section with found, refusing, offline and nothing-listening rows', async (t) => {
  const file = path.join(STATIC, 'demo', 'nodes-discover.json');
  if (!fs.existsSync(file)) { t.skip('demo/nodes-discover.json is written by the Python area'); return; }
  const data = JSON.parse(fs.readFileSync(file, 'utf8'));
  const w = nodesWorld({ answer: data });
  await tick();
  const states = new Set(rowsOf(w).map((r) => r.getAttribute('data-state')));
  for (const s of ['found', 'refuses', 'offline', 'no_ccboard']) assert.ok(states.has(s), `the demo shows a ${s} row`);
});

// ================================================================ pairing (issue #135)
// Create pairing code, Add node, the paired list (Rotate token, Remove), Who can control this node (Revoke) and Activity. api() is the recorder of world.mjs, answered by a small
// fake of the board's routes (GET /api/nodes, /api/nodes/pairs, /api/nodes/audit, POST /api/nodes/pair-code, POST /api/nodes, POST /api/nodes/<peer>/rotate, DELETE /api/nodes/<peer>).

const CODE = 'K7Q2M-4XD9R';
const peer = (over) => ({ peer_id: 'p1', handle: 'build-box', node_id: 'ts:nBOX', name: 'build-box', url: 'https://build-box.example.ts.net:8443', scopes: ['read', 'tasks'], created_at: iso(86400), last_seen: iso(120), direction: 'out', ...over });
const caller = (over) => ({ peer_id: 'c1', peer_node_id: 'ts:nDESK', name: 'desk-pc', url: 'https://desk-pc.example.ts.net', scopes: ['read', 'tasks', 'sessions'], created_at: iso(86400), last_used_at: iso(600), direction: 'in', ...over });
const defer = () => { let resolve; const p = new Promise((r) => { resolve = r; }); return { p, resolve }; };
const settle = async () => { for (let i = 0; i < 8; i++) await tick(); };
const refuse = (status, message, reason) => { const e = new Error(message); e.status = status; if (reason) e.body = { error: message, reason }; return e; };

function pairWorld({ out = [], inn = [], audit = [], over = {}, fail = {}, hold = {}, answer = ANSWER } = {}) {
  const w = nodesWorld({ hash: '', over, answer });
  const server = { out: out.map((x) => ({ ...x })), inn: inn.map((x) => ({ ...x })), audit: audit.map((x) => ({ ...x })), codeBody: null, cancelled: 0, added: null, peerNotified: undefined, others: [] };
  w.ctx.__copied = [];
  w.ctx.navigator.clipboard = { writeText: async (t) => { w.ctx.__copied.push(t); } };
  w.run('globalThis.__errors = []; setError = (m) => { if (m) __errors.push(m); };');
  w.ctx.__answers['/api/nodes'] = async ({ method, path: p, body }) => {
    const bare = p.split('?')[0];
    if (method === 'GET' && bare === '/api/nodes') { if (fail.list) throw refuse(500, fail.list); return { nodes: server.out.map((x) => ({ ...x })), pairs: server.inn.map((x) => ({ ...x })), at: iso(0) }; }
    if (method === 'GET' && bare === '/api/nodes/pairs') return { pairs: server.inn.map((x) => ({ ...x })), at: iso(0) };
    if (method === 'GET' && bare === '/api/nodes/audit') return { rows: server.audit.map((x) => ({ ...x })), at: iso(0) };
    if (method === 'POST' && bare === '/api/nodes/pair-code') {
      if (hold.code) await hold.code.p;
      if (fail.code) throw refuse(500, fail.code);
      server.codeBody = JSON.parse(JSON.stringify(body));
      return fail.noCode ? { ok: true } : { code: CODE.replace('-', '').toLowerCase(), expires_at: new Date(Date.now() + body.minutes * 60000).toISOString(), scopes: body.scopes };
    }
    if (method === 'DELETE' && bare === '/api/nodes/pair-code') { if (fail.cancel) throw refuse(500, fail.cancel); server.cancelled++; return { cancelled: true }; }
    if (method === 'POST' && bare === '/api/nodes') {
      if (hold.add) await hold.add.p;
      if (fail.add) throw refuse(fail.addStatus || 400, fail.add, fail.reason);
      server.added = JSON.parse(JSON.stringify(body));
      const rec = peer({ peer_id: 'p9', handle: body.handle || 'node-b', name: body.handle || 'node-b', url: body.url, last_seen: null });
      server.out.push(rec);
      return rec;
    }
    let m = /^\/api\/nodes\/([^/]+)\/rotate$/.exec(bare);
    if (method === 'POST' && m) { if (hold.rotate) await hold.rotate.p; if (fail.rotate) throw refuse(502, fail.rotate); return { rotated: true, grace_s: server.grace || 60 }; }
    m = /^\/api\/nodes\/([^/]+)\/remove-preview$/.exec(bare);
    if (method === 'POST' && m) {
      if (hold.preview) await hold.preview.p;
      if (fail.preview) throw refuse(500, fail.preview);
      return { auto: [], others: server.others.map((x) => ({ ...x })), at: iso(0) };
    }
    m = /^\/api\/nodes\/([^/]+)$/.exec(bare);
    if (method === 'DELETE' && m) {
      if (hold.remove) await hold.remove.p;
      if (fail.remove) throw refuse(fail.removeStatus || 400, fail.remove);
      const id = decodeURIComponent(m[1]);
      const asked = body && Array.isArray(body.also_revoke) ? body.also_revoke : [];
      server.out = server.out.filter((x) => x.peer_id !== id);
      server.inn = server.inn.filter((x) => x.peer_id !== id && !asked.includes(x.peer_id));
      return { removed: true, peer_notified: server.peerNotified === undefined ? null : server.peerNotified, also_revoked: asked, other_pairs: server.others.filter((x) => !asked.includes(x.peer_id)) };
    }
    return { ok: true };
  };
  w.location.hash = '#/settings?sec=nodes';
  return { w, server };
}
const dlg = (w) => w.document.querySelector('dialog.nd-sheet');
const btn = (root, label) => root.querySelectorAll('button').find((b) => text(b).trim() === label);
const isFilled = (b) => b.classList.contains('primary') && !b.classList.contains('tinted');
const filledIn = (root) => root.querySelectorAll('button').filter(isFilled).map((b) => text(b).trim());
const nodeCalls = (w, method) => calls(w).filter((c) => /^\/api\/nodes(\/|$|\?)/.test(c.path) && !c.path.startsWith('/api/nodes/discover') && (!method || c.method === method));
const writes = (w) => nodeCalls(w).filter((c) => c.method !== 'GET').map((c) => `${c.method} ${c.path}`);
const pairedRows = (w) => panel(w).querySelectorAll('.nd-row[data-kind=out]');
const incomingRows = (w) => panel(w).querySelectorAll('.nd-row[data-kind=in]');
const lastToast = (w) => plain(w.get('__toasts')).pop();
const flip = (input, on) => { input.checked = on; input.dispatchEvent({ type: 'change' }); };
const submitForm = (root) => root.querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
const type = (input, v) => { input.value = v; input.dispatchEvent({ type: 'input' }); };
const fieldErr = (root, label) => {
  const f = root.querySelectorAll('.field').find((x) => text(x.querySelector('.field-label')) === label);
  return f ? text(f.querySelector('.field-err')) : null;
};
const selectOf = (d) => d.querySelector('select');
const foundRow = (w, name) => panel(w).querySelectorAll('.nd-row[data-state]').find((r) => text(r.querySelector('.nd-name')) === name);

test('the paired list: name, short name, address, scope chips, and Paired only once the board has reached the node', async () => {
  const { w } = pairWorld({ out: [peer(), peer({ peer_id: 'p2', handle: 'alice-mac', name: 'alice-mac', url: 'https://alice-mac.example.ts.net', scopes: ['read'], last_seen: null }),
    peer({ peer_id: 'p3', handle: 'old-laptop', name: 'old-laptop', needs_repair: true, last_error: 'the saved token was refused (401)', url: 'https://old-laptop.example.ts.net' })] });
  await settle();
  const rows = pairedRows(w);
  assert.equal(rows.length, 3);
  const [a, b, c] = rows;
  assert.equal(text(a.querySelector('.nd-name')), 'build-box');
  assert.deepEqual(a.querySelectorAll('.nd-chips .badge').map(text), ['✓ paired', 'read', 'tasks']);
  assert.match(text(a.querySelector('.nd-note')), /^build-box\.example\.ts\.net:8443 · last reached 2m ago$/);
  assert.deepEqual(b.querySelectorAll('.nd-chips .badge').map(text), ['· waiting for first contact', 'read'], 'not reached yet: not called paired, and no time is invented from the day it was saved');
  assert.match(text(b.querySelector('.nd-note')), /not reached yet/);
  assert.ok(c.classList.contains('repair'));
  assert.deepEqual(c.querySelectorAll('.nd-chips .badge').map(text).slice(0, 1), ['! re-pair needed'], 'amber with a glyph and words');
  assert.ok(c.querySelector('.nd-chips .badge.warn'));
  assert.match(text(c.querySelector('.nd-note')), /last error: the saved token was refused \(401\)/);
});

test('Rotate token and Remove are two-tap red-outlined buttons: one tap arms, only the armed Confirm is filled', async () => {
  const { w } = pairWorld({ out: [peer()] });
  await settle();
  const row = () => pairedRows(w)[0];
  const rot = btn(row(), 'Rotate token');
  const rm = btn(row(), 'Remove');
  assert.ok(rot.classList.contains('danger') && rm.classList.contains('danger'));
  assert.ok(!isFilled(rot) && !isFilled(rm), 'red-outlined at rest');
  rot.click();
  await settle();
  assert.deepEqual(writes(w), [], 'one tap only arms it');
  const confirm = btn(row(), 'Confirm Rotate token');
  assert.ok(confirm && confirm.classList.contains('danger') && confirm.classList.contains('confirm'));
  btn(row(), 'Cancel').click();
  await settle();
  assert.ok(btn(row(), 'Rotate token'), 'Cancel disarms it');
  assert.deepEqual(writes(w), []);
});

test('Rotate token posts to the pair, says the old token works for 60 seconds, shows it on the row, and nothing about a token is on the page', async () => {
  const { w } = pairWorld({ out: [peer()] });
  await settle();
  btn(pairedRows(w)[0], 'Rotate token').click();
  await settle();
  btn(pairedRows(w)[0], 'Confirm Rotate token').click();
  await settle();
  assert.deepEqual(writes(w), ['POST /api/nodes/p1/rotate']);
  const t = lastToast(w);
  assert.match(t.text, /build-box rotated\. The old one still works for 60 seconds\./);
  assert.equal(t.kind, 'ok');
  assert.match(text(pairedRows(w)[0].querySelector('.nd-note')), /token rotated, the old one still works for 60 s/);
  assert.ok(btn(pairedRows(w)[0], 'Rotate token'), 'the button is back, armed state gone');
  assert.doesNotMatch(text(panel(w)), /ccbnode_/);
});

test('the toast says how long the old token lasts as the board answered it', async () => {
  const { w, server } = pairWorld({ out: [peer()] });
  server.grace = 45;
  await settle();
  btn(pairedRows(w)[0], 'Rotate token').click();
  await settle();
  btn(pairedRows(w)[0], 'Confirm Rotate token').click();
  await settle();
  assert.match(lastToast(w).text, /The old one still works for 45 seconds\./);
});

test('a rotation that failed says so and changes nothing on the row', async () => {
  const { w } = pairWorld({ out: [peer()], fail: { rotate: 'the node did not answer' } });
  await settle();
  btn(pairedRows(w)[0], 'Rotate token').click();
  await settle();
  btn(pairedRows(w)[0], 'Confirm Rotate token').click();
  await settle();
  assert.deepEqual(lastToast(w), { text: 'Token for build-box not rotated: the node did not answer', kind: 'warn' });
  assert.doesNotMatch(text(pairedRows(w)[0].querySelector('.nd-note')), /rotated/);
});

test('Remove: two taps, the row stays (and reads Removing) until the board answers, then it goes; an offline node is said not to have been told', async () => {
  const hold = defer();
  const { w, server } = pairWorld({ out: [peer(), peer({ peer_id: 'p2', handle: 'alice-mac', name: 'alice-mac', url: 'https://alice-mac.example.ts.net' })], hold: { remove: hold } });
  server.peerNotified = false;
  await settle();
  btn(pairedRows(w)[0], 'Remove').click();
  await settle();
  assert.deepEqual(writes(w), ['POST /api/nodes/p1/remove-preview'], 'the sheet only asks what would be affected');
  btn(dlg(w), 'Remove').click();
  await settle();
  assert.deepEqual(writes(w), ['POST /api/nodes/p1/remove-preview', 'DELETE /api/nodes/p1']);
  assert.equal(pairedRows(w).length, 2, 'still listed while the answer is on its way');
  assert.notEqual(btn(pairedRows(w)[0], 'Removing…').getAttribute('disabled'), null);
  assert.notEqual(btn(dlg(w), 'Removing…').getAttribute('disabled'), null, 'the sheet waits too');
  hold.resolve();
  await settle();
  assert.equal(dlg(w), null, 'the sheet closes when the board has answered');
  assert.deepEqual(pairedRows(w).map((r) => text(r.querySelector('.nd-name'))), ['alice-mac']);
  assert.deepEqual(lastToast(w), { text: 'build-box removed. It was not told (offline or no answer), so remove this board there too.', kind: 'warn' });
});

test('Remove when the other node was told is a plain confirmation, and the empty text comes back', async () => {
  const { w, server } = pairWorld({ out: [peer()] });
  server.peerNotified = true;
  await settle();
  btn(pairedRows(w)[0], 'Remove').click();
  await settle();
  assert.match(text(dlg(w)), /No other pair uses this node's id\./);
  btn(dlg(w), 'Remove').click();
  await settle();
  assert.deepEqual(lastToast(w), { text: 'build-box removed.', kind: 'ok' });
  assert.equal(pairedRows(w).length, 0);
  assert.match(text(panel(w)), /No node is paired\./);
});

test('Who can control this node: the callers with scopes and last use, a flag for a callback that was not verified, and a two-tap Revoke', async () => {
  const { w } = pairWorld({ inn: [caller(), caller({ peer_id: 'c2', name: 'phone-board', scopes: ['read'], last_used_at: null, callback_unverified: true })] });
  await settle();
  const rows = incomingRows(w);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0].querySelectorAll('.nd-chips .badge').map(text), ['✓ in use', 'read', 'tasks', 'sessions']);
  assert.match(text(rows[0].querySelector('.nd-note')), /desk-pc\.example\.ts\.net · last used 10m ago/);
  assert.deepEqual(rows[1].querySelectorAll('.nd-chips .badge').map(text), ['· never used', 'read', '! callback not verified']);
  assert.match(text(rows[1].querySelector('.nd-note')), /never used/);
  assert.equal(btn(rows[0], 'Rotate token'), undefined, 'a caller is revoked, not rotated, from this side');
  btn(rows[0], 'Revoke').click();
  await settle();
  assert.deepEqual(writes(w), []);
  btn(incomingRows(w)[0], 'Confirm Revoke').click();
  await settle();
  assert.deepEqual(writes(w), ['DELETE /api/nodes/c1']);
  assert.deepEqual(incomingRows(w).map((r) => text(r.querySelector('.nd-name'))), ['phone-board']);
  assert.deepEqual(lastToast(w), { text: 'desk-pc can no longer call this board.', kind: 'ok' });
});

test('callers are listed from GET /api/nodes alone when GET /api/nodes/pairs cannot be read, and never twice when both answer', async () => {
  const { w } = pairWorld({ inn: [caller()] });
  await settle();
  assert.equal(incomingRows(w).length, 1, 'both lists carry the caller: one row');
  const base = w.ctx.__answers['/api/nodes'];
  w.ctx.__answers['/api/nodes'] = async (req) => { if (req.path === '/api/nodes/pairs') throw refuse(500, 'busy'); return base(req); };
  btn(panel(w), 'Reload').click();
  await settle();
  assert.equal(incomingRows(w).length, 1, 'still listed from the other answer');
});

test('with nobody paired the sections say what to press, and nothing is listed', async () => {
  const { w } = pairWorld();
  await settle();
  assert.equal(pairedRows(w).length + incomingRows(w).length, 0);
  assert.match(text(panel(w)), /No node can control this board\. Press Create pairing code, then type the code on the other board\./);
  assert.match(text(panel(w)), /Nothing yet\. Making a code, pairing, rotating and removing show here\./);
});

test('Activity: the board\'s audit rows (its own action and status words) in plain words, a failed row says so and has a cross, hostile detail is text', async () => {
  const { w } = pairWorld({ audit: [
    { id: 3, at: iso(60), direction: 'in', peer: 'c1', node_name: 'desk-pc', user: null, action: 'code_used', target: null, status: 'ok', detail: 'scopes read,tasks' },
    { id: 2, at: iso(3600), direction: 'out', peer: '', node_name: 'alice-mac', user: 'alice@example.com', action: 'pair_failed', target: null, status: 'failed', detail: '<img src=x onerror=alert(1)>' },
    { id: 1, at: iso(86400 * 2), direction: 'out', peer: 'p1', node_name: 'build-box', user: null, action: 'rotated', target: null, status: 'ok', detail: '' },
  ] });
  await settle();
  const rows = panel(w).querySelectorAll('.nd-audit');
  assert.equal(rows.length, 3);
  assert.equal(text(rows[0].querySelector('.nd-audit-word')), 'Pairing code used');
  assert.equal(text(rows[0].querySelector('.nd-audit-where')), 'from desk-pc · 1m ago');
  assert.equal(text(rows[1].querySelector('.nd-audit-word')), 'Pairing failed');
  assert.equal(text(rows[1].querySelector('.nd-glyph')), '✕');
  assert.ok(rows[1].classList.contains('bad'));
  assert.equal(text(rows[1].querySelector('.nd-audit-detail')), '<img src=x onerror=alert(1)>');
  assert.equal(rows[1].querySelector('img'), null, 'text, not markup');
  assert.equal(text(rows[2].querySelector('.nd-audit-word')), 'Token rotated');
  assert.equal(text(rows[2].querySelector('.nd-audit-where')), 'to build-box · 2d ago');
});

test('opening the tab reads the three lists once; a poll repaint does not read them again, and Reload does', async () => {
  const { w } = pairWorld({ out: [peer()] });
  await settle();
  assert.deepEqual(nodeCalls(w).map((c) => c.path), ['/api/nodes', '/api/nodes/pairs', '/api/nodes/audit?limit=50']);
  for (let i = 0; i < 3; i++) w.run('updateCurrentPage(state)');
  await settle();
  assert.equal(nodeCalls(w).length, 3, 'no poll, no timer');
  btn(panel(w), 'Reload').click();
  await settle();
  assert.equal(nodeCalls(w).length, 6);
});

test('a Settings page on another tab reads none of the pairing lists', async () => {
  const w = nodesWorld({ hash: '#/settings?sec=box' });
  await settle();
  assert.equal(nodeCalls(w).length, 0);
});

test('a list that cannot be read keeps the last one on screen and says so; the others still show', async () => {
  const { w } = pairWorld({ out: [peer()], audit: [{ id: 1, at: iso(60), direction: 'out', node_name: 'x', action: 'paired', status: 'ok' }] });
  await settle();
  w.ctx.__answers['/api/nodes'] = ((orig) => async (req) => { if (req.method === 'GET' && req.path === '/api/nodes') throw refuse(500, 'the board is busy'); return orig(req); })(w.ctx.__answers['/api/nodes']);
  btn(panel(w), 'Reload').click();
  await settle();
  assert.equal(pairedRows(w).length, 1, 'the last good list stays');
  assert.match(text(panel(w).querySelector('.nd-pstatus')), /^Could not read the paired list: the board is busy\. The list below is the last answer\./);
  assert.equal(panel(w).querySelectorAll('.nd-audit').length, 1);
});

test('a legacy CCBOARD_NODES row is read only with a Pair button; once the same address is paired the row is gone (the pair replaces it)', async () => {
  const nodes = { value: [{ name: 'mini', online: true, sessions: 3, attention: 0, url: 'https://mini.example.ts.net', health: {} }], at: 1 };
  const { w } = pairWorld({ over: { nodes } });
  await settle();
  const kv = panel(w).querySelectorAll('.kv').find((r) => text(r.querySelector('.k')) === 'mini');
  assert.match(text(kv), /read only, pair to enable actions/);
  const pair = btn(kv, 'Pair');
  assert.ok(pair && pair.classList.contains('tinted'));
  pair.click();
  await settle();
  assert.equal(dlg(w).querySelector('.nd-addr-in').value, 'https://mini.example.ts.net', 'the address comes along');
  const { w: w2 } = pairWorld({ over: { nodes }, out: [peer({ peer_id: 'pm', handle: 'mini', name: 'mini', url: 'https://mini.example.ts.net' })] });
  await settle();
  assert.equal(pairedRows(w2).length, 1);
  assert.equal(panel(w2).querySelectorAll('.kv').filter((r) => text(r.querySelector('.k')) === 'mini').length, 0, 'the legacy row is replaced');
});

test('the board\'s registry copy of a CCBOARD_NODES row does not hide the state\'s row: the health line and the Open link stay, and it is listed once', async () => {
  const nodes = { value: [{ name: 'mini', online: true, sessions: 3, attention: 1, url: 'https://mini.example.ts.net', health: { cpu_pct: 7 } }], at: 1 };
  const { w } = pairWorld({ over: { nodes }, out: [peer({ peer_id: 'pl', handle: 'mini', name: 'mini', url: 'https://mini.example.ts.net', legacy: true, scopes: ['read'], last_seen: iso(60) })] });
  await settle();
  assert.equal(pairedRows(w).length, 0, 'no second row for the same address');
  const kv = panel(w).querySelectorAll('.kv').filter((r) => text(r.querySelector('.k')) === 'mini');
  assert.equal(kv.length, 1);
  assert.match(text(kv[0]), /3 sess · 1 need you · cpu 7%/);
  assert.equal(kv[0].querySelector('a').getAttribute('href'), 'https://mini.example.ts.net/');
  assert.match(text(kv[0]), /read only, pair to enable actions/);
  assert.ok(btn(kv[0], 'Pair'));
});

test('a registry row flagged legacy says read only (never paired, even when it was read a minute ago) and offers Pair, not Rotate or Remove', async () => {
  const { w } = pairWorld({ out: [peer({ legacy: true, last_seen: iso(60) })] });
  await settle();
  const r = pairedRows(w)[0];
  assert.deepEqual(r.querySelectorAll('.nd-chips .badge').map(text), ['· read only', 'read', 'tasks']);
  assert.match(text(r.querySelector('.nd-note')), /read only, pair to enable actions/);
  assert.deepEqual(r.querySelectorAll('button').map(text), ['Pair']);
});

test('a device found on the tailnet that is already paired in the registry is not listed under Found', async () => {
  const { w } = pairWorld({ out: [peer({ peer_id: 'pa', handle: 'node-a', name: 'node-a', node_id: 'ts:nNODEA', url: 'https://node-a.example.ts.net:8443' })] });
  await settle();
  assert.equal(foundRow(w, 'node-a'), undefined);
  assert.equal(pairedRows(w).length, 1);
});

// ---------------------------------------------------------------- Create pairing code

const openCode = async (w) => { btn(panel(w), 'Create pairing code').click(); await settle(); return dlg(w); };
const codeOf = (d) => d.querySelector('.nd-code');

test('Create pairing code opens a native dialog: read and tasks ticked, sessions and permissions not, each in plain words; Create code is the one filled primary', async () => {
  const { w } = pairWorld();
  await settle();
  const d = await openCode(w);
  assert.ok(d && d.open && d.tagName.toLowerCase() === 'dialog');
  const ticks = Object.fromEntries(d.querySelectorAll('input[data-scope]').map((c) => [c.getAttribute('data-scope'), !!c.checked]));
  assert.deepEqual(ticks, { read: true, tasks: true, sessions: false, permissions: false });
  assert.match(text(d), /Read.*See this board's name, system and a summary of its sessions\./);
  assert.match(text(d), /A request is still answered only by a signed-in person on the calling board, never by the token alone\./);
  assert.deepEqual(filledIn(d), ['Create code']);
  assert.equal(selectOf(d).value, '10', 'ten minutes by default');
  assert.equal(codeOf(d), null, 'no code until one is asked for');
});

test('Create code posts the chosen scopes and minutes; the code is shown once in large type as XXXXX-XXXXX with Copy, a countdown, and Done as the one filled primary', async () => {
  const { w, server } = pairWorld();
  await settle();
  const d = await openCode(w);
  flip(d.querySelector('input[data-scope=sessions]'), true);
  selectOf(d).value = '20';
  submitForm(d);
  await settle();
  assert.deepEqual(server.codeBody, { scopes: ['read', 'tasks', 'sessions'], minutes: 20 });
  assert.equal(text(codeOf(d)), CODE);
  assert.equal(codeOf(d).tagName.toLowerCase(), 'output');
  assert.match(text(d.querySelector('.nd-count')), /^Ends in (19:5\d|20:00)$/);
  assert.match(text(d), /It lets that node use: read, tasks, sessions\./);
  assert.match(text(d), /Closing this sheet does not cancel the code\. Cancel code does\./);
  assert.deepEqual(filledIn(d), ['Done']);
  btn(d, 'Copy').click();
  await settle();
  assert.deepEqual(plain(w.get('__copied')), [CODE]);
  assert.equal(d.querySelector('.nd-count').getAttribute('role'), 'timer', 'a timer role is not read out every second');
});

test('the sheet shows this board\'s address to type on the other board when the card has one', async () => {
  const { w } = pairWorld({ over: { node: { ...CARD, url: 'https://box.example.ts.net' } } });
  await settle();
  const d = await openCode(w);
  submitForm(d);
  await settle();
  assert.equal(text(d.querySelector('.nd-addr code')), 'https://box.example.ts.net');
});

test('unticking every scope is refused beside the scopes; nothing is asked for', async () => {
  const { w } = pairWorld();
  await settle();
  const d = await openCode(w);
  for (const c of d.querySelectorAll('input[data-scope]')) flip(c, false);
  submitForm(d);
  await settle();
  assert.deepEqual(writes(w), []);
  assert.match(fieldErr(d, 'What the other node may do here'), /Pick at least one thing/);
});

test('the countdown runs on the sheet\'s own timer, ends in "Create a new code", wipes the code, and the next form remembers the scopes', async () => {
  const { w } = pairWorld();
  w.run('globalThis.__now = Date.now(); Date.now = () => __now;');
  await settle();
  const d = await openCode(w);
  flip(d.querySelector('input[data-scope=tasks]'), false);
  submitForm(d);
  await settle();
  assert.equal(text(d.querySelector('.nd-count')), 'Ends in 10:00');
  w.run('__now += 61000; settingsNdSheet.tick()');
  assert.equal(text(d.querySelector('.nd-count')), 'Ends in 8:59');
  w.run('__now += 9 * 60000; settingsNdSheet.tick()');
  assert.ok(!text(d).includes(CODE), 'the expired code is wiped from the sheet');
  assert.equal(w.get('settingsNdSheet.code'), '');
  assert.equal(w.get('settingsNdSheet.timer'), null, 'the timer stopped');
  assert.match(text(d), /This code has run out\. It can no longer pair anything\./);
  assert.deepEqual(filledIn(d), ['Create a new code']);
  btn(d, 'Create a new code').click();
  await settle();
  const ticks = Object.fromEntries(d.querySelectorAll('input[data-scope]').map((c) => [c.getAttribute('data-scope'), !!c.checked]));
  assert.deepEqual(ticks, { read: true, tasks: false, sessions: false, permissions: false }, 'the choice made before comes back');
});

test('the countdown does not steal the focus', async () => {
  const { w } = pairWorld();
  await settle();
  const d = await openCode(w);
  submitForm(d);
  await settle();
  const copy = btn(d, 'Copy');
  copy.focus();
  w.run('settingsNdSheet.tick()');
  assert.equal(w.document.activeElement, copy);
});

test('Done closes the sheet: every node is emptied, the dialog is removed, the timer stops, the code is nowhere in the page, storage or URL, and the focus returns to the button that opened it', async () => {
  const { w, server } = pairWorld();
  await settle();
  const d = await openCode(w);
  submitForm(d);
  await settle();
  assert.ok(text(d).includes(CODE));
  btn(d, 'Done').click();
  await settle();
  assert.equal(d.open, false);
  assert.equal(text(d), '');
  assert.equal(dlg(w), null);
  assert.equal(w.get('settingsNdSheet.code'), '');
  assert.equal(w.get('settingsNdSheet.timer'), null);
  assert.ok(!text(w.document.body).includes('K7Q2M'));
  for (const store of [w.localStorage, w.sessionStorage]) for (let i = 0; i < store.length; i++) assert.ok(!String(store.getItem(store.key(i))).includes('K7Q2M'));
  assert.ok(!String(w.location.hash).includes('K7Q2M'));
  assert.ok(!JSON.stringify(plain(w.get('state'))).includes('K7Q2M'));
  assert.equal(w.document.activeElement, btn(panel(w), 'Create pairing code'));
  assert.equal(server.cancelled, 0, 'closing does not cancel the code (the sheet says so)');
});

test('Escape closes it the same way', async () => {
  const { w } = pairWorld();
  await settle();
  const d = await openCode(w);
  submitForm(d);
  await settle();
  d.dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {} });
  await settle();
  assert.equal(dlg(w), null);
  assert.equal(w.get('settingsNdSheet.code'), '');
});

test('Cancel code asks the board to cancel it and closes; if the board cannot, the sheet stays and says the code still works', async () => {
  const { w, server } = pairWorld({ fail: { cancel: 'the board is busy' } });
  await settle();
  let d = await openCode(w);
  submitForm(d);
  await settle();
  btn(d, 'Cancel code').click();
  await settle();
  assert.ok(dlg(w), 'still open');
  assert.match(text(d.querySelector('.qr-err')), /The code was not cancelled: the board is busy\. It still works until it ends\./);
  assert.equal(btn(d, 'Cancel code').disabled, false);
  btn(d, 'Done').click();
  await settle();
  const { w: w2, server: s2 } = pairWorld();
  await settle();
  d = await openCode(w2);
  submitForm(d);
  await settle();
  btn(d, 'Cancel code').click();
  await settle();
  assert.equal(s2.cancelled, 1);
  assert.equal(dlg(w2), null);
  assert.equal(server.cancelled, 0);
  assert.deepEqual(lastToast(w2), { text: 'Pairing code cancelled.', kind: 'ok' });
});

test('a refused or code-less answer shows an error in the sheet and no code', async () => {
  const { w } = pairWorld({ fail: { code: 'too many codes' } });
  await settle();
  let d = await openCode(w);
  submitForm(d);
  await settle();
  assert.match(text(d.querySelector('.qr-err')), /No code was made: too many codes/);
  assert.equal(codeOf(d), null);
  const { w: w2 } = pairWorld({ fail: { noCode: true } });
  await settle();
  d = await openCode(w2);
  submitForm(d);
  await settle();
  assert.match(text(d.querySelector('.qr-err')), /The board answered without a code\./);
  assert.equal(codeOf(d), null);
});

test('a sheet closed while the board is still making the code leaves no live code behind', async () => {
  const hold = defer();
  const { w, server } = pairWorld({ hold: { code: hold } });
  await settle();
  const d = await openCode(w);
  submitForm(d);
  await settle();
  btn(d, 'Cancel').click();
  await settle();
  hold.resolve();
  await settle();
  assert.equal(server.cancelled, 1, 'the code nobody saw is cancelled');
  assert.equal(dlg(w), null);
  assert.ok(!text(w.document.body).includes('K7Q2M'));
});

test('leaving Settings closes an open sheet and stops its timer', async () => {
  const { w } = pairWorld();
  await settle();
  const d = await openCode(w);
  submitForm(d);
  await settle();
  w.location.hash = '#/';
  await settle();
  assert.equal(dlg(w), null);
  assert.equal(w.get('settingsNdSheet.timer'), null);
  assert.equal(w.get('settingsNdSheet.code'), '');
  assert.ok(!text(w.document.body).includes('K7Q2M'));
});

// ---------------------------------------------------------------- Add node

const openAdd = async (w) => { btn(panel(w), 'Add node').click(); await settle(); return dlg(w); };
const fillAdd = (d, { url, code, handle, both } = {}) => {
  if (url !== undefined) type(d.querySelector('.nd-addr-in'), url);
  if (code !== undefined) type(d.querySelector('.nd-code-in'), code);
  if (handle !== undefined) type(d.querySelectorAll('input[type=text]')[2], handle);
  if (both !== undefined) flip(d.querySelector('input[type=checkbox]'), both);
};

test('Add node: address, code, optional short name and an "also let that node control this one" box (off); Pair is the one filled primary', async () => {
  const { w } = pairWorld();
  await settle();
  const d = await openAdd(w);
  assert.ok(d && d.open);
  assert.deepEqual(d.querySelectorAll('.field-label').map(text), ['Address of the other board', 'Pairing code', 'Short name (optional)']);
  assert.equal(!!d.querySelector('input[type=checkbox]').checked, false);
  assert.match(text(d), /Also let that node control this one.*It gets Read and Tasks on this board\./);
  assert.deepEqual(filledIn(d), ['Pair']);
  assert.equal(d.querySelector('.nd-code-in').getAttribute('placeholder'), 'XXXXX-XXXXX');
});

test('a pasted code in lower case with spaces, and a bare host name, are sent as XXXXX-XXXXX and an https address', async () => {
  const { w, server } = pairWorld();
  await settle();
  const d = await openAdd(w);
  fillAdd(d, { url: 'Node-B.example.ts.net:8443', code: ' k7q2m 4xd9r ' });
  submitForm(d);
  await settle();
  assert.deepEqual(server.added, { url: 'https://node-b.example.ts.net:8443', code: CODE, both_ways: false });
});

test('a handle and both ways go with the request; success closes the sheet, clears the code, toasts, reads the lists again and returns the focus', async () => {
  const { w, server } = pairWorld();
  await settle();
  const d = await openAdd(w);
  const before = nodeCalls(w, 'GET').length;
  fillAdd(d, { url: 'https://node-b.example.ts.net', code: CODE, handle: 'Box-2', both: true });
  submitForm(d);
  await settle();
  assert.deepEqual(server.added, { url: 'https://node-b.example.ts.net', code: CODE, handle: 'box-2', both_ways: true });
  assert.equal(dlg(w), null);
  assert.equal(text(d), '');
  assert.deepEqual(lastToast(w), { text: 'Paired with box-2.', kind: 'ok' });
  assert.equal(nodeCalls(w, 'GET').length, before + 3, 'the three lists are read again');
  assert.equal(pairedRows(w).length, 1);
  assert.match(text(pairedRows(w)[0].querySelector('.nd-chips')), /waiting for first contact/, 'Paired is said after the first read, not at saving');
  assert.equal(w.document.activeElement, btn(panel(w), 'Add node'));
  assert.ok(!text(w.document.body).includes('K7Q2M'));
});

test('mistakes are caught beside the field before anything is sent, and what was typed stays', async () => {
  const { w } = pairWorld();
  await settle();
  const d = await openAdd(w);
  submitForm(d);
  await settle();
  assert.equal(fieldErr(d, 'Address of the other board'), 'Type the address of the other board.');
  fillAdd(d, { url: 'http://node-b.example.ts.net', code: 'nope', handle: 'Bad Name!' });
  submitForm(d);
  await settle();
  assert.match(fieldErr(d, 'Address of the other board'), /Use an https address/);
  assert.equal(fieldErr(d, 'Pairing code'), 'A pairing code is 10 letters and numbers, like K7Q2M-4XD9R.');
  assert.match(fieldErr(d, 'Short name (optional)'), /1 to 31 lower case letters, numbers or dashes/);
  assert.equal(d.querySelector('.nd-code-in').value, 'nope');
  assert.equal(d.querySelector('.nd-addr-in').value, 'http://node-b.example.ts.net');
  assert.deepEqual(writes(w), [], 'nothing was sent');
  fillAdd(d, { url: 'https://node-b.example.ts.net/some/path' });
  submitForm(d);
  assert.match(fieldErr(d, 'Address of the other board'), /no path/);
  fillAdd(d, { url: 'https://node-b.example.ts.net', code: CODE, handle: 'self' });
  submitForm(d);
  assert.match(fieldErr(d, 'Short name (optional)'), /"self" is taken/);
});

test('a refusal from the board lands beside the field it is about, keeps every value, and the sheet stays open', async () => {
  for (const [msg, label, status] of [
    ['that code is wrong, expired or already used', 'Pairing code', 400],
    ['the address is not on the tailnet', 'Address of the other board', 400],
    ['the other node could not be reached', 'Address of the other board', 502],
    ['handle build-box is already taken', 'Short name (optional)', 409],
  ]) {
    const { w, server } = pairWorld({ fail: { add: msg, addStatus: status } });
    await settle();
    const d = await openAdd(w);
    fillAdd(d, { url: 'https://node-b.example.ts.net', code: CODE, handle: 'box-2', both: true });
    submitForm(d);
    await settle();
    assert.equal(fieldErr(d, label), msg, msg);
    assert.ok(dlg(w), 'open');
    assert.equal(d.querySelector('.nd-code-in').value, CODE, 'the code stays so a typo can be fixed');
    assert.equal(d.querySelector('.nd-addr-in').value, 'https://node-b.example.ts.net');
    assert.equal(!!d.querySelector('input[type=checkbox]').checked, true);
    assert.equal(server.added, null);
    assert.equal(btn(d, 'Pair').disabled, false, 'Pair can be pressed again');
  }
});

test('the board\'s one-word reason decides the field: a wrong, used or burned code is the code, an address it may not call or cannot reach is the address, a refusal belongs to no field', async () => {
  for (const [reason, status, label] of [['wrong', 403, 'Pairing code'], ['expired', 403, 'Pairing code'], ['burned', 403, 'Pairing code'], ['none', 403, 'Pairing code'],
    ['bad_url', 400, 'Address of the other board'], ['unreachable', 502, 'Address of the other board'], ['callback_mismatch', 409, 'Address of the other board']]) {
    const { w } = pairWorld({ fail: { add: 'The board said no.', addStatus: status, reason } });
    await settle();
    const d = await openAdd(w);
    fillAdd(d, { url: 'https://node-b.example.ts.net', code: CODE });
    submitForm(d);
    await settle();
    assert.equal(fieldErr(d, label), 'The board said no.', reason);
  }
  for (const reason of ['refused', 'bad_request', 'store']) {
    const { w } = pairWorld({ fail: { add: 'The other node refused the pairing.', addStatus: 502, reason } });
    await settle();
    const d = await openAdd(w);
    fillAdd(d, { url: 'https://node-b.example.ts.net', code: CODE });
    submitForm(d);
    await settle();
    assert.equal(text(d.querySelector('.qr-err')), 'Not paired: The other node refused the pairing.', reason);
    assert.equal(fieldErr(d, 'Pairing code'), '');
  }
});

test('a rate limit (429) is shown on the code field in plain words; an unknown refusal is shown under the form', async () => {
  const { w } = pairWorld({ fail: { add: 'slow down', addStatus: 429 } });
  await settle();
  let d = await openAdd(w);
  fillAdd(d, { url: 'https://node-b.example.ts.net', code: CODE });
  submitForm(d);
  await settle();
  assert.equal(fieldErr(d, 'Pairing code'), 'Too many tries. Wait a minute, then try again.');
  const { w: w2 } = pairWorld({ fail: { add: 'something odd happened', addStatus: 500 } });
  await settle();
  d = await openAdd(w2);
  fillAdd(d, { url: 'https://node-b.example.ts.net', code: CODE });
  submitForm(d);
  await settle();
  assert.equal(text(d.querySelector('.qr-err')), 'Not paired: something odd happened');
});

test('a second press while the board is answering does nothing', async () => {
  const hold = defer();
  const { w, server } = pairWorld({ hold: { add: hold } });
  await settle();
  const d = await openAdd(w);
  fillAdd(d, { url: 'https://node-b.example.ts.net', code: CODE });
  submitForm(d);
  submitForm(d);
  await settle();
  assert.equal(nodeCalls(w, 'POST').filter((c) => c.path === '/api/nodes').length, 1);
  assert.equal(btn(d, 'Pairing…').disabled, true);
  hold.resolve();
  await settle();
  assert.ok(server.added);
});

test('a pair whose callback was not confirmed is said so in the toast', async () => {
  const { w } = pairWorld();
  await settle();
  const base = w.ctx.__answers['/api/nodes'];
  w.ctx.__answers['/api/nodes'] = async (req) => { const r = await base(req); return req.method === 'POST' && req.path === '/api/nodes' ? { ...r, callback_unverified: true } : r; };
  const d = await openAdd(w);
  fillAdd(d, { url: 'https://node-b.example.ts.net', code: CODE });
  submitForm(d);
  await settle();
  assert.deepEqual(lastToast(w), { text: 'Paired with node-b, but its own address did not confirm who it is.', kind: 'warn' });
});

test('Pair on a found device opens the sheet with the address that answered, ready for the code', async () => {
  const { w } = pairWorld();
  await settle();
  foundRow(w, 'node-a').querySelector('button').click();
  await settle();
  const d = dlg(w);
  assert.equal(d.querySelector('.nd-addr-in').value, 'https://node-a.example.ts.net:8443');
  assert.equal(d.querySelector('.nd-code-in').value, '');
  assert.equal(w.document.activeElement, d.querySelector('.nd-code-in'), 'the code is the next thing to type');
  btn(d, 'Cancel').click();
  await settle();
  assert.equal(dlg(w), null);
});

test('only one sheet is open at a time', async () => {
  const { w } = pairWorld();
  await settle();
  await openAdd(w);
  btn(panel(w), 'Create pairing code').click();
  await settle();
  assert.equal(w.document.body.querySelectorAll('dialog.nd-sheet').length, 1);
  assert.equal(text(dlg(w).querySelector('h2')), 'Create pairing code');
});

// ---------------------------------------------------------------- what a hostile board can make the page show

test('a hostile name, address, scope and error from the board is text: no element is made from it', async () => {
  const evil = '<img src=x onerror=alert(1)>';
  const { w } = pairWorld({
    out: [peer({ name: evil, handle: 'evil', url: 'https://x.example.ts.net:8443', scopes: ['read', evil], last_error: evil })],
    inn: [caller({ name: evil, scopes: [evil, 'read'] })],
    audit: [{ id: 1, at: iso(5), direction: 'in', node_name: evil, action: evil, status: 'ok', detail: evil }],
  });
  await settle();
  assert.equal(panel(w).querySelectorAll('img').length, 0);
  assert.equal(text(pairedRows(w)[0].querySelector('.nd-name')), evil);
  assert.deepEqual(pairedRows(w)[0].querySelectorAll('.nd-chips .badge').map(text), ['✓ paired', 'read'], 'a scope this page cannot explain is not shown as a word');
  assert.equal(text(incomingRows(w)[0].querySelector('.nd-name')), evil);
  assert.equal(text(panel(w).querySelector('.nd-audit-detail')), evil);
});

test('a peer with a path-like or odd id is only ever put in a URL encoded', async () => {
  const { w } = pairWorld({ out: [peer({ peer_id: 'a/../b c' })] });
  await settle();
  btn(pairedRows(w)[0], 'Remove').click();
  await settle();
  btn(dlg(w), 'Remove').click();
  await settle();
  assert.deepEqual(writes(w), ['POST /api/nodes/a%2F..%2Fb%20c/remove-preview', 'DELETE /api/nodes/a%2F..%2Fb%20c']);
});

// ---------------------------------------------------------------- the source

test('the pairing code never touches storage, the URL or an event: it is only ever the text of the open sheet', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'settings.js'), 'utf8');
  const start = src.indexOf('const settingsNdPeers =');
  const end = src.indexOf('const settingsNd =');
  assert.ok(start > 0 && end > start);
  const block = src.slice(start, end).replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.doesNotMatch(block, /localStorage|sessionStorage|indexedDB|location\.|history\.|BroadcastChannel|postMessage|console\.|document\.cookie|innerHTML|insertAdjacentHTML|\.style\b|cssText/);
  assert.doesNotMatch(block, /\.token\b|\btoken\s*[:=]/, 'a token is not read, kept or sent by this page');
  assert.equal((block.match(/setInterval\(/g) || []).length, 1, 'the one timer is the countdown');
  assert.ok(/clearInterval\(/.test(block));
  assert.doesNotMatch(block, /setTimeout|requestAnimationFrame/);
});

// ---------------------------------------------------------------- demo mode

const demoFile = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'nodes.json'), 'utf8'));

test('the shipped demo fixture lists paired nodes (reached, never reached, needing a re-pair), callers and activity', async () => {
  const { w } = pairWorld({ out: demoFile.nodes, inn: demoFile.pairs, audit: demoFile.audit });
  await settle();
  const chips = pairedRows(w).map((r) => text(r.querySelector('.nd-chips .badge')));
  assert.ok(chips.includes('✓ paired') && chips.includes('· waiting for first contact') && chips.includes('! re-pair needed'));
  assert.ok(incomingRows(w).length >= 2);
  assert.ok(incomingRows(w).some((r) => /callback not verified/.test(text(r))));
  const words = panel(w).querySelectorAll('.nd-audit-word').map(text);
  assert.ok(words.includes('Pairing failed') && words.includes('Token rotation failed') && words.includes('Token rotated') && words.includes('Pairing code created') && words.includes('Paired'));
});

test('demo mode: the three lists come from demo/nodes.json with times that follow the clock; a code, a pair, a rotation and a removal are played in the page, and nothing is a credential', async () => {
  const asked = [];
  const NOW_S = Math.floor(Date.now() / 1000);                          // now, not the moment the file was loaded: the demo writes wait 150 ms each
  const w = makeWorld({ fetch: async (url) => { asked.push(String(url)); return { ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(JSON.stringify(demoFile)) }; } });
  w.location.search = '?demo=1';
  w.load('core.js');
  const list = await w.run('api("GET", "/api/nodes")');
  const pairs = await w.run('api("GET", "/api/nodes/pairs")');
  const audit = await w.run('api("GET", "/api/nodes/audit?limit=50")');
  assert.deepEqual(asked, ['/static/demo/nodes.json', '/static/demo/nodes.json', '/static/demo/nodes.json']);
  assert.equal(list.nodes.length, 3);
  assert.equal(list.pairs.length, 5, 'GET /api/nodes carries both lists like the board\'s');
  assert.equal(pairs.pairs.length, 5);
  const old = pairs.pairs.find((x) => x.peer_id === 'p-build-box-old');
  assert.equal(old.superseded_by, 'p-build-box-new', 'the demo has one pair that a newer one replaced, for the screen to show');
  assert.equal(old.superseded_by_url, 'https://build-box.example.ts.net:8443');
  assert.equal(pairs.pairs.filter((x) => x.superseded_by).length, 1);
  assert.equal(audit.rows.length, demoFile.audit.length);
  const secs = (s) => Date.parse(s) / 1000;
  assert.ok(Math.abs(secs(list.nodes[0].last_seen) - (NOW_S - 30)) <= 8, 'last seen follows the clock');
  assert.ok(Math.abs(secs(audit.rows[0].at) - (NOW_S - 600)) <= 8, 'the newest row is ten minutes old, as the fixture says');
  const made = await w.run('api("POST", "/api/nodes/pair-code", { scopes: ["read"], minutes: 5 })');
  assert.equal(made.code, CODE);
  assert.deepEqual(plain(made.scopes), ['read']);
  assert.ok(Math.abs(secs(made.expires_at) - (Math.floor(Date.now() / 1000) + 300)) <= 3);
  const added = await w.run('api("POST", "/api/nodes", { url: "https://box-2.example.ts.net", code: "K7Q2M-4XD9R", handle: "box-2" })');
  assert.equal(added.peer_id, 'demo-box-2');
  assert.equal(added.last_seen, null, 'a new pair has not been reached yet');
  assert.equal((await w.run('api("GET", "/api/nodes")')).nodes.length, 4);
  assert.deepEqual(plain(await w.run('api("DELETE", "/api/nodes/p-old-laptop")')), { removed: true, peer_notified: false, also_revoked: [], other_pairs: [] });
  const pv = plain(await w.run('api("POST", "/api/nodes/p-build-box/remove-preview")'));
  assert.deepEqual(pv.auto.map((x) => x.peer_id), ['p-build-box-new']);
  assert.deepEqual(pv.others.map((x) => [x.peer_id, x.verified]), [['p-build-box-old', true], ['p-build-box-claim', false]]);
  const gone = plain(await w.run('api("DELETE", "/api/nodes/p-build-box", { also_revoke: ["p-build-box-old"] })'));
  assert.deepEqual([gone.removed, gone.peer_notified, gone.also_revoked, gone.other_pairs.map((x) => x.peer_id)], [true, true, ['p-build-box-old'], ['p-build-box-claim']]);
  assert.deepEqual(plain((await w.run('api("GET", "/api/nodes/pairs")')).pairs.map((x) => x.peer_id)), ['p-desk-pc', 'p-phone-board', 'p-build-box-claim'], 'the pair at its address and the ticked one went; the other stayed');
  assert.deepEqual(plain((await w.run('api("GET", "/api/nodes")')).nodes.map((n) => n.peer_id)), ['p-alice-mac', 'demo-box-2']);
  const acts = plain((await w.run('api("GET", "/api/nodes/audit")')).rows.slice(0, 4).map((r) => r.action));
  assert.deepEqual(acts, ['unpair', 'unpair', 'paired', 'code_created'], 'each write left a row, newest first');
  assert.doesNotMatch(JSON.stringify(plain([list, pairs, audit, made, added])), /ccbnode_|ccbmcp_|sha256|digest/);
});


// ---------------------------------------------------------------- a moved node is not silent (security finding 3): the replaced label and the remove sheet

const moved = (over) => caller({ peer_id: 'c-old', name: 'build-box', url: 'https://100.64.0.21', superseded_by: 'c-new', superseded_by_url: 'https://build-box.example.ts.net:8443', ...over });

test('a pair that a newer pair from another address replaced says so, with the new address, and its Revoke is the solid one', async () => {
  const { w } = pairWorld({ inn: [moved(), caller({ peer_id: 'c-new', name: 'build-box', url: 'https://build-box.example.ts.net:8443' })] });
  await settle();
  const [old, fresh] = incomingRows(w);
  assert.ok(old.classList.contains('superseded') && !fresh.classList.contains('superseded'));
  assert.equal(text(old.querySelector('.nd-replaced')), 'Replaced by a newer pair from build-box.example.ts.net:8443. This one still works until you revoke it.');
  assert.ok(old.querySelectorAll('.nd-chips .badge').map(text).includes('! replaced'));
  assert.equal(fresh.querySelector('.nd-replaced'), null);
  assert.ok(btn(old, 'Revoke').classList.contains('confirm'), 'solid red at rest on the replaced pair');
  assert.ok(!btn(fresh, 'Revoke').classList.contains('confirm'), 'the other stays red-outlined');
  btn(old, 'Revoke').click();
  await settle();
  assert.deepEqual(writes(w), [], 'it is still a two-tap Revoke');
  btn(incomingRows(w)[0], 'Confirm Revoke').click();
  await settle();
  assert.deepEqual(writes(w), ['DELETE /api/nodes/c-old']);
  assert.deepEqual(incomingRows(w).map((r) => text(r.querySelector('.nd-name'))), ['build-box']);
});

test('a replaced pair whose newer pair has no known address still says it was replaced', async () => {
  const { w } = pairWorld({ inn: [moved({ superseded_by_url: null })] });
  await settle();
  assert.equal(text(incomingRows(w)[0].querySelector('.nd-replaced')), 'Replaced by a newer pair. This one still works until you revoke it.');
});

test('the audit words for a replaced pair and for a kept outgoing pair are not "Node removed"', () => {
  const w = nodesWorld();
  assert.equal(w.run("NodeView.auditWord('superseded', true)"), 'Pair replaced, still active');
  assert.equal(w.run("NodeView.auditWord('unpair_kept_outgoing', true)"), 'Pair kept');
  assert.equal(w.run("NodeView.auditWord('unpair', true)"), 'Node removed');
});

const OTHERS = [{ peer_id: 'c-old', name: 'build-box', url: 'https://100.64.0.21', verified: true }, { peer_id: 'c-claim', name: 'maybe-build-box', url: 'https://100.64.0.66', verified: false }];

test('Remove opens a sheet that asks the board first and lists the other pairs of the node id as ticked boxes with their addresses and a not confirmed tag', async () => {
  const { w, server } = pairWorld({ out: [peer()], inn: [moved(), moved({ peer_id: 'c-claim', name: 'maybe-build-box', url: 'https://100.64.0.66', callback_unverified: true, superseded_by: null })] });
  server.others = OTHERS;
  await settle();
  btn(pairedRows(w)[0], 'Remove').click();
  await settle();
  assert.deepEqual(writes(w), ['POST /api/nodes/p1/remove-preview'], 'nothing is removed by opening the sheet');
  const d = dlg(w);
  assert.equal(text(d.querySelector('h2')), 'Remove build-box');
  const rows = d.querySelectorAll('.nd-rm-row');
  assert.deepEqual(rows.map((r) => [text(r.querySelector('b')), r.querySelector('input').checked, r.querySelector('input').getAttribute('data-pair')]), [['build-box', true, 'c-old'], ['maybe-build-box', true, 'c-claim']]);
  assert.match(text(rows[0]), /100\.64\.0\.21/);
  assert.match(text(rows[1]), /100\.64\.0\.66/);
  assert.equal(rows[0].querySelectorAll('.badge').length, 0, 'a confirmed pair carries no tag');
  assert.deepEqual(rows[1].querySelectorAll('.badge').map(text), ['! not confirmed']);
  assert.match(text(d), /These pairs use the same node id from another address, or never confirmed who they are, so they were not cut with it\. Tick the ones to revoke too\./);
});

test('Remove sends exactly the ticked pairs as also_revoke and says what happened to the rest', async () => {
  const { w, server } = pairWorld({ out: [peer()], inn: [moved(), moved({ peer_id: 'c-claim', name: 'maybe-build-box', url: 'https://100.64.0.66', callback_unverified: true, superseded_by: null })] });
  server.others = OTHERS;
  server.peerNotified = true;
  await settle();
  btn(pairedRows(w)[0], 'Remove').click();
  await settle();
  const boxes = dlg(w).querySelectorAll('.nd-rm-row input');
  flip(boxes[1], false);
  btn(dlg(w), 'Remove').click();
  await settle();
  const del = nodeCalls(w, 'DELETE');
  assert.equal(del.length, 1);
  assert.deepEqual(del[0].body, { also_revoke: ['c-old'] });
  assert.equal(dlg(w), null);
  assert.deepEqual(lastToast(w), { text: 'build-box removed. 1 other pair revoked. 1 other pair with its node id can still call this board.', kind: 'warn' });
  assert.deepEqual(incomingRows(w).map((r) => text(r.querySelector('.nd-name'))), ['maybe-build-box'], 'the list is read again: the unticked one is still there');
});

test('Remove with every box left ticked revokes all of them, and with no other pair sends no body at all', async () => {
  const a = pairWorld({ out: [peer()], inn: [moved(), moved({ peer_id: 'c-claim', name: 'x', callback_unverified: true, superseded_by: null })] });
  a.server.others = OTHERS;
  a.server.peerNotified = true;
  await settle();
  btn(pairedRows(a.w)[0], 'Remove').click();
  await settle();
  btn(dlg(a.w), 'Remove').click();
  await settle();
  assert.deepEqual(nodeCalls(a.w, 'DELETE')[0].body, { also_revoke: ['c-old', 'c-claim'] });
  assert.deepEqual(lastToast(a.w), { text: 'build-box removed. 2 other pairs revoked.', kind: 'ok' });
  const b = pairWorld({ out: [peer()] });
  b.server.peerNotified = true;
  await settle();
  btn(pairedRows(b.w)[0], 'Remove').click();
  await settle();
  btn(dlg(b.w), 'Remove').click();
  await settle();
  assert.equal(nodeCalls(b.w, 'DELETE')[0].body, undefined, 'a plain DELETE, as before');
});

test('the sheet cannot remove until the board has answered the question, Cancel removes nothing, and a failed question says so but still allows Remove', async () => {
  const hold = defer();
  const a = pairWorld({ out: [peer()], hold: { preview: hold } });
  await settle();
  btn(pairedRows(a.w)[0], 'Remove').click();
  await settle();
  assert.notEqual(btn(dlg(a.w), 'Remove').getAttribute('disabled'), null, 'Remove waits for the answer');
  btn(dlg(a.w), 'Cancel').click();
  await settle();
  assert.equal(dlg(a.w), null);
  assert.deepEqual(writes(a.w), ['POST /api/nodes/p1/remove-preview'], 'Cancel changes nothing');
  hold.resolve();
  await settle();
  const b = pairWorld({ out: [peer()], fail: { preview: 'busy' } });
  await settle();
  btn(pairedRows(b.w)[0], 'Remove').click();
  await settle();
  assert.match(text(dlg(b.w)), /Could not check which other pairs would stay \(busy\)\. Removing still works/);
  assert.equal(dlg(b.w).querySelectorAll('.nd-rm-row').length, 0);
  btn(dlg(b.w), 'Remove').click();
  await settle();
  assert.deepEqual(writes(b.w), ['POST /api/nodes/p1/remove-preview', 'DELETE /api/nodes/p1']);
});

test('a refused removal keeps the sheet open with the reason and the boxes as they were', async () => {
  const { w, server } = pairWorld({ out: [peer()], inn: [moved()], fail: { remove: 'only an active pair with the same node id can be revoked together with this node' } });
  server.others = [OTHERS[0]];
  await settle();
  btn(pairedRows(w)[0], 'Remove').click();
  await settle();
  flip(dlg(w).querySelector('.nd-rm-row input'), false);
  btn(dlg(w), 'Remove').click();
  await settle();
  assert.ok(dlg(w), 'still open');
  assert.match(text(dlg(w).querySelector('.qr-err')), /^build-box not removed: only an active pair/);
  assert.equal(dlg(w).querySelector('.nd-rm-row input').checked, false);
  assert.ok(btn(dlg(w), 'Remove') && !btn(dlg(w), 'Remove').disabled, 'it can be tried again');
});
