// Settings > Nodes (issue #134): three labelled sections (This node, Paired nodes, Found on your tailnet), the tailnet list asked for only when a person opens the tab or presses
// Refresh (never on a timer, never from a poll), a probe result in plain words per row, a Pair button that cannot be pressed yet, and every word the board's answer
// holds about a device shown as text. Real core.js, components.js, router.js, nodes.js and pages/settings.js on minidom's DOM; api() is the recorder of world.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { makeWorld } from './harness.mjs';
import { calls, fakeState, homeWorld, page, tick, text } from './world.mjs';

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

test('three labelled sections in order: This node, Paired nodes, Found on your tailnet', async () => {
  const w = nodesWorld();
  await tick();
  assert.deepEqual(panel(w).querySelectorAll('h2.set-h').map(text), ['This node', 'Paired nodes', 'Found on your tailnet']);
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

test('Paired nodes: empty says what is true (nothing configured, pairing comes next)', async () => {
  const w = nodesWorld();
  await tick();
  assert.match(text(panel(w)), /No other nodes are configured\. Pairing comes in the next release\./);
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
  assert.doesNotMatch(text(panel(w)), /No other nodes are configured/);
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

test('Pair is a disabled button with its reason, and only a node that answered has one', async () => {
  const w = nodesWorld();
  await tick();
  const pairs = panel(w).querySelectorAll('.nd-row button');
  assert.equal(pairs.length, 1);
  const [pair] = pairs;
  assert.equal(text(pair), 'Pair');
  assert.notEqual(pair.getAttribute('disabled'), null);
  assert.equal(pair.getAttribute('title'), 'Pairing comes in the next release');
  assert.match(pair.getAttribute('aria-label'), /^Pair node-a: pairing comes in the next release$/);
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

test('a node already paired is not listed twice (matched by node id, else by host)', async () => {
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
  assert.equal(rowsOf(w2).length, 0, 'matched by node id');
  assert.match(text(panel(w2)), /Every device found here is already paired\./);
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
