// Contract tests for the routing half of the epic view (issues #150 and #152): Run (the one filled primary, off with its reason), Pause and Resume as posts to the board, the choice and the
// wait shown per issue, the "Run on..." menu of one issue (Auto or a node), the window limit, the hand-made group of nodes on one subscription, "Let MCP run this epic", and the demo of all of it.
// Real scripts on minidom's DOM through tests/js/world.mjs; api() is the recorder and its answers are the demo fixture (app/static/demo/epic.json) or a changed copy of it.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { STATIC } from './harness.mjs';
import { calls, fakeState, homeWorld, page, plain, text, tick } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'epic.json'), 'utf8'));
const KEY = 'example/website#147';
const HASH = '#/epic/example/website/147';
const BASE = '/api/epics/example/website/147';
const clone = (v) => JSON.parse(JSON.stringify(v));
const detail = (over = {}) => ({ ...clone(FX.details[KEY]), ...over });
const variant = (name) => {                                                       // what ?epic=<name> lays over the detail, as the demo does
  const v = FX.variants[name];
  const d = { ...detail(), ...clone(v.detail || {}) };
  for (const [n, patch] of Object.entries(v.child || {})) d.children = d.children.map((c) => (String(c.number) === n ? { ...c, ...clone(patch) } : c));
  return d;
};
const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('epUnmount(); Epics.stopList()'); } catch (_) { /* never mounted */ } } });
const settle = async (n = 5) => { for (let i = 0; i < n; i++) await tick(); };
const defer = () => { let resolve, reject; const p = new Promise((a, b) => { resolve = a; reject = b; }); return { p, resolve, reject }; };
const refusal = (status, body) => Promise.reject(Object.assign(new Error(body.error || 'refused'), { status, body }));

/** A world on the epic page. GET answers __detail; POST answers __post(req) (a value, a promise or a function's result); PUT answers __put(req), default: the request merged into the detail. */
async function routeWorld({ data = detail(), post = null, put = null } = {}) {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.ctx.__detail = data;
  w.ctx.__post = post;
  w.ctx.__put = put;
  w.run(`__answers['/api/epics'] = (req) => {
    if (req.method === 'POST') return typeof __post === 'function' ? __post(req) : (__post || {});
    if (req.method === 'PUT') return typeof __put === 'function' ? __put(req) : (__put || Object.assign(__detail, req.body || {}));
    return JSON.parse(JSON.stringify(__detail));
  }`);
  w.location.hash = HASH;
  await settle();
  return w;
}

const pg = (w) => page(w);
const rows = (w) => pg(w).querySelectorAll('.ep-row');
const rowOf = (w, n) => rows(w).find((r) => r.getAttribute('data-n') === String(n));
const btn = (scope, label) => scope.querySelectorAll('button').find((b) => text(b).trim() === label);
const runBtn = (w) => pg(w).querySelector('.ep-run');
const runOn = (w, n) => rowOf(w, n).querySelectorAll('button').find((b) => b.hasAttribute('data-runon'));
const posts = (w) => calls(w).filter((c) => c.method === 'POST');
const puts = (w) => calls(w).filter((c) => c.method === 'PUT');
const toasts = (w) => plain(w.get('__toasts'));
const menuLabels = (w) => w.document.querySelectorAll('.menu-pop .menuitem').map((i) => text(i.querySelector('.mi-name') || i).trim());
const menuPick = (w, label) => w.document.querySelectorAll('.menu-pop .menuitem').find((i) => text(i.querySelector('.mi-name') || i).trim() === label).click();
const off = (n) => n.disabled === true || n.hasAttribute('disabled');
const why = (w) => text(pg(w).querySelector('#ep-run-why'));
const cfg = (w) => pg(w).querySelector('.ep-cfg');
const winIn = (w) => cfg(w).querySelector('[data-window]');
const flagBox = (w, k) => cfg(w).querySelectorAll('input').find((i) => i.getAttribute('data-flag') === k);
const groupBoxes = (w) => cfg(w).querySelectorAll('[data-group-node]');
const change = (el, value) => { if (value !== undefined) el.value = value; el.dispatchEvent({ type: 'change' }); };
const tickBox = (el, on) => { el.checked = on; el.dispatchEvent({ type: 'change' }); };

// ---------------------------------------------------------------- the fixture

test('the demo fixture carries the routing shapes: a route per detail, a route (null) per child, the settings, and a variant for each state', () => {
  const d = FX.details[KEY];
  assert.deepEqual(Object.keys(d.route).sort(), ['assigned', 'blocked', 'by', 'can_run', 'cycle_at', 'ended', 'ended_at', 'error', 'held', 'left', 'loop', 'max_window_pct', 'mcp_run', 'paused', 'running', 'started_at', 'via', 'waiting']);
  assert.equal(d.route.can_run, true);
  assert.deepEqual([d.max_window_pct, d.max_window_pct_set, d.group, d.mcp_run], [85, false, [], false]);
  assert.ok(d.children.every((c) => c.route === null));
  for (const n of ['run', 'runwait', 'runpaused', 'runagent', 'runfinished', 'runlimit', 'nonodes', 'paused', 'groups', 'writesoff']) assert.ok(FX.variants[n].detail.route, n);
  assert.equal(FX.variants.writesoff.detail.route.blocked.code, 'writes_off');
  assert.equal(FX.variants.nonodes.detail.route.blocked.code, 'no_nodes');
  assert.deepEqual(FX.variants.run.child['205'].route.state, 'assigned');
  assert.deepEqual(['waiting', 'held'], [FX.variants.run.child['206'].route.state, FX.variants.run.child['207'].route.state]);
  const all = JSON.stringify(FX);
  assert.ok(!/\/Users\/|\/home\/|ghp_|github_pat_|@/.test(all.replace(/@@/g, '')), 'no login, path, token or address in the fixture');
});

// ---------------------------------------------------------------- Run

test('Run is the one filled primary and is on when the board can route; it says in words what it does and that headroom is an estimate', async () => {
  const w = await routeWorld();
  assert.ok(runBtn(w).classList.contains('primary'));
  assert.equal(text(runBtn(w)), 'Run');
  assert.ok(!off(runBtn(w)));
  assert.match(why(w), /^Run sends each ready issue to the node that has a free lane and room in the agent's rate-limit window, one at a time\./);
  assert.match(why(w), /Headroom is an estimate/);
  assert.equal(pg(w).querySelectorAll('button.primary').filter((b) => !b.classList.contains('tinted')).length, 1, 'one filled primary on the page');
});

test('Run is off, with its reason in words, when GitHub writes are off, when no node is paired, and when the epic is paused', async () => {
  const wo = await routeWorld({ data: variant('writesoff') });
  assert.ok(off(runBtn(wo)));
  assert.match(why(wo), /GitHub writes are off on this board \(CCBOARD_EPIC_WRITES\), so nothing can be sent to a node/);
  assert.match(runBtn(wo).getAttribute('title'), /CCBOARD_EPIC_WRITES/);
  const nn = await routeWorld({ data: variant('nonodes') });
  assert.ok(off(runBtn(nn)));
  assert.match(why(nn), /No node is paired with this board, so there is nowhere to send the work\./);
  const pa = await routeWorld({ data: variant('paused') });
  assert.ok(off(runBtn(pa)));
  assert.match(why(pa), /Run is off while this epic is paused\. Resume first\./);
  assert.ok(btn(pg(pa), 'Resume'));
  for (const x of [wo, nn, pa]) assert.equal(posts(x).length, 0, 'no request was made');
});

test('Run is off while GitHub cannot be read and nothing is shown, and on a board without routing it says to update', async () => {
  const bad = await routeWorld({ data: { ...detail(), children: [], error: { reason: 'no_login', message: 'gh is not logged in' } } });
  assert.ok(off(runBtn(bad)));
  assert.match(why(bad), /GitHub could not be read, so there is nothing to send yet\./);
  const old = await routeWorld({ data: (() => { const d = detail(); delete d.route; return d; })() });
  assert.ok(off(runBtn(old)));
  assert.match(why(old), /Update this board\./);
});

test('Run says Running at once, posts to /run with no body, takes the board\'s answer and then reads the epic again', async () => {
  const gate = defer();
  const w = await routeWorld({ post: () => gate.p });
  runBtn(w).click();
  assert.equal(text(runBtn(w)), 'Starting…', 'while the board answers it says Starting');
  assert.ok(off(runBtn(w)));
  assert.match(why(w), /^Starting the run/);
  assert.deepEqual(posts(w).map((c) => [c.path, c.body]), [[`${BASE}/run`, undefined]]);
  const route = { ...FX.details[KEY].route, running: true, by: 'maya-r', via: 'page', started_at: '2026-10-11T08:59:30Z', loop: true };
  w.ctx.__detail.route = route;                                                  // the board now says the run is going, so the next read agrees
  gate.resolve({ route });
  await settle();
  assert.equal(text(runBtn(w)), 'Running');
  assert.ok(off(runBtn(w)));
  assert.match(why(w), /^Running, started .* by maya-r\. Pause stops new assignments; issues already sent go on\./);
  assert.ok(toasts(w).some((t) => /^Run started/.test(t.text)));
  assert.ok(calls(w).filter((c) => c.method === 'GET').length >= 2, 'the epic is read again so the choices show');
  const pauseWhy = pg(w).querySelector('[data-pause-why]');
  assert.equal(text(pauseWhy), '', 'a recorded run says what Pause does in its own line, so the older Pause sentence is not said twice');
  assert.ok(pauseWhy.classList.contains('hidden'));
});

test('a refused Run puts the page back and says the board\'s words under the button and in a toast', async () => {
  const w = await routeWorld({ post: () => refusal(409, { error: 'GitHub writes are off on this board (CCBOARD_EPIC_WRITES), so nothing can be sent to a node.', reason: 'writes_off' }) });
  runBtn(w).click();
  await settle();
  assert.equal(text(runBtn(w)), 'Run');
  assert.ok(!off(runBtn(w)), 'the page is as it was');
  assert.match(why(w), /Run did not start: GitHub writes are off on this board/);
  assert.match(toasts(w).at(-1).text, /Run did not start: GitHub writes are off/);
  const w2 = await routeWorld({ post: () => refusal(403, { error: 'x' }) });
  runBtn(w2).click();
  await settle();
  assert.match(toasts(w2).at(-1).text, /^Only a signed-in person can do this\.$/);
});

test('Run lifts nothing by itself on the page: a paused epic has Run off, and Resume turns it back on with a post to /resume', async () => {
  const w = await routeWorld({ data: variant('paused'), post: () => ({ paused: false, route: { ...FX.details[KEY].route, paused: false } }) });
  assert.ok(off(runBtn(w)));
  btn(pg(w), 'Resume').click();
  await settle();
  assert.deepEqual(posts(w).map((c) => c.path), [`${BASE}/resume`]);
  assert.ok(!off(runBtn(w)));
});

test('a run that is going shows who started it and how many issues an agent may still send, and the page asks again every ten seconds', async () => {
  const w = await routeWorld({ data: variant('runagent') });
  assert.equal(text(runBtn(w)), 'Running');
  assert.match(why(w), /by an agent \(mcp:laptop-agent\), 3 more issues allowed\./);
  assert.equal(w.run('epDelay(epicPage.cur)'), 10000);
  const idle = await routeWorld();
  assert.equal(idle.run('epDelay(epicPage.cur)'), 30000);
});

test('a run that is paused reads Paused, not Running, and a recorded run that cannot go on says so instead of Running', async () => {
  const w = await routeWorld({ data: variant('runpaused') });
  assert.equal(text(runBtn(w)), 'Paused');
  assert.ok(off(runBtn(w)));
  assert.match(why(w), /^Paused: no new assignments until Resume\. Issues already sent go on\.$/);
  assert.ok(btn(pg(w), 'Resume'));
  const stuck = variant('run');
  stuck.route = { ...stuck.route, can_run: false, blocked: { code: 'no_nodes', text: 'No node is paired with this board, so there is nowhere to send the work.' } };
  const x = await routeWorld({ data: stuck });
  assert.equal(text(runBtn(x)), 'Run');
  assert.match(why(x), /^No node is paired with this board, so there is nowhere to send the work\. A run is recorded and goes on when this clears\.$/);
});

test('a board that cannot be reached before the first read says why Run is off', async () => {
  const w = await routeWorld({ data: () => Promise.reject(Object.assign(new Error('offline'), { status: 0 })) });
  assert.ok(off(runBtn(w)));
  assert.match(why(w), /^Could not reach this board, so Run is off\./);
});

test('the last run is named when it ended: finished, or stopped because the agent\'s allowance ran out', async () => {
  const done = await routeWorld({ data: variant('runfinished') });
  assert.ok(!off(runBtn(done)));
  assert.match(why(done), /The last run finished: nothing is left to send\./);
  const lim = await routeWorld({ data: variant('runlimit') });
  assert.match(why(lim), /The last run stopped: the number of issues the agent was allowed ran out\./);
});

// ---------------------------------------------------------------- the choice and the wait, per issue

test('a run shows, per issue, the board\'s own words: what it chose and why, "waiting: <reason> until <time>", and a hold; each with a glyph, never colour alone', async () => {
  const w = await routeWorld({ data: variant('run') });
  const bit = (n) => rowOf(w, n).querySelector('.ep-route');
  assert.equal(bit(205).getAttribute('data-route'), 'assigned');
  assert.equal(text(bit(205)), '→ chose node-b: 2 free lanes, 5-hour window 31 percent');
  assert.equal(bit(206).getAttribute('data-route'), 'waiting');
  assert.equal(text(bit(206)), '✻ waiting: node-b 5-hour window is at 90 percent until 14:10');
  assert.ok(bit(206).classList.contains('warn'));
  assert.equal(bit(206).getAttribute('data-route-code'), 'window');
  assert.equal(bit(207).getAttribute('data-route'), 'held');
  assert.equal(text(bit(207)), '⚠ held: too many comments to read');
  assert.equal(rowOf(w, 202).querySelector('.ep-route'), null, 'an issue the run has no word about shows none');
  assert.equal(pg(w).querySelectorAll('.ep-route[role=status]').length, 0, 'no bare spinner: a wait is words');
});

test('a wait for a lane names the pin, and a poll does not reorder the rows', async () => {
  const w = await routeWorld({ data: variant('runwait') });
  assert.equal(text(rowOf(w, 205).querySelector('.ep-route')), "✻ waiting: node-b (pinned by the issue's pin) has no free lane");
  const before = rows(w).map((r) => r.getAttribute('data-n'));
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.deepEqual(rows(w).map((r) => r.getAttribute('data-n')), before);
});

// ---------------------------------------------------------------- Run on... for one issue

test('Run on... is a menu on an open, unclaimed issue: Auto first, then each paired node (never this board), and nowhere else', async () => {
  const w = await routeWorld();
  assert.equal(runOn(w, 205).getAttribute('aria-haspopup'), 'menu');
  assert.match(runOn(w, 205).getAttribute('aria-label'), /^Run issue #205 on a node$/);
  for (const n of [201, 202, 203, 204, 206, 207]) assert.equal(runOn(w, n), undefined, `#${n} is closed, claimed or not known: no Run on...`);
  runOn(w, 205).click();
  assert.deepEqual(menuLabels(w), ['Auto', 'node-b']);
  assert.match(w.document.querySelectorAll('.menu-pop .menuitem')[0].querySelector('.mi-sub').textContent, /^follows the pin: node-b$/);
});

test('Run on... Auto paints "Choosing a node..." at once, posts {node: "auto"}, and the answer paints the node, the reason and the running task', async () => {
  const gate = defer();
  const w = await routeWorld({ post: () => gate.p });
  runOn(w, 205).click();
  menuPick(w, 'Auto');
  assert.equal(text(rowOf(w, 205).querySelector('.ep-route')), '∙ Choosing a node…');
  assert.ok(off(runOn(w, 205)));
  assert.deepEqual(posts(w).map((c) => [c.path, c.body]), [[`${BASE}/issues/205/run`, { node: 'auto' }]]);
  gate.resolve({ ok: true, handle: 'node-b', name: 'node-b', why: 'chose node-b: 2 free lanes, 5-hour window 31 percent, pinned by the issue\'s pin', data: { claimed: true, reason: 'won', task_ref: 'node-b:55', id: 55, branch: 'worktree-issue-205' } });
  await settle();
  assert.equal(text(rowOf(w, 205).querySelector('.ep-route')), "→ chose node-b: 2 free lanes, 5-hour window 31 percent, pinned by the issue's pin");
  assert.equal(rowOf(w, 205).getAttribute('data-state'), 'running');
  assert.ok(toasts(w).some((t) => t.text === 'node-b started #205'));
});

test('Run on... a named node posts that handle; a node that cannot take it makes the row wait with the board\'s words and puts the row back', async () => {
  const w = await routeWorld({ post: () => refusal(409, { ok: false, error: 'waiting: node-b (named by you) has no free lane', reason: 'waiting', waiting: { code: 'lane', reason: 'node-b has no free lane', until: null, until_iso: null, text: 'waiting: node-b (named by you) has no free lane' }, refused: [] }) });
  runOn(w, 205).click();
  menuPick(w, 'node-b');
  assert.equal(text(rowOf(w, 205).querySelector('.ep-route')), '∙ Sending to node-b…');
  await settle();
  assert.deepEqual(posts(w).map((c) => c.body), [{ node: 'node-b' }]);
  assert.equal(rowOf(w, 205).querySelector('.ep-route'), null, 'the row is as it was');
  assert.equal(rowOf(w, 205).getAttribute('data-state'), 'open');
  const note = rowOf(w, 205).querySelector('[data-run-note]');
  assert.equal(note.getAttribute('data-run-note'), 'waiting');
  assert.equal(text(note), 'waiting: node-b (named by you) has no free lane');
  assert.ok(!off(runOn(w, 205)), 'it can be tried again');
});

test('every other refusal of Run on... is said in the board\'s words, the row goes back, and nothing is retried', async () => {
  for (const [status, body] of [[409, { error: 'That issue cannot be sent now: held by node-b.', reason: 'running' }], [404, { error: 'that issue is not one of the epic\'s issues this board has read', reason: 'not_found' }],
    [409, { error: 'The run is busy sending other issues. Try again in a moment.', reason: 'busy' }]]) {
    const w = await routeWorld({ post: () => refusal(status, body) });
    runOn(w, 205).click();
    menuPick(w, 'Auto');
    await settle();
    assert.equal(text(rowOf(w, 205).querySelector('[data-run-note]')), `#205 did not start: ${body.error}`);
    assert.equal(posts(w).length, 1, 'no retry');
    assert.equal(rowOf(w, 205).getAttribute('data-state'), 'open');
  }
});

test('Run on... is a disabled button with the reason when the board cannot route, and its menu is not wired', async () => {
  const w = await routeWorld({ data: variant('writesoff') });
  assert.ok(off(runOn(w, 205)));
  assert.match(runOn(w, 205).getAttribute('title'), /CCBOARD_EPIC_WRITES/);
  runOn(w, 205).click();
  assert.deepEqual(menuLabels(w), []);
  const nn = await routeWorld({ data: variant('nonodes') });
  assert.equal(runOn(nn, 205), undefined, 'with no paired node there is no menu at all');
});

// ---------------------------------------------------------------- the settings

test('the window limit shows the board\'s own setting until the epic has one; a number from 10 to 100 is written, anything else is said and not written, empty goes back to the board\'s', async () => {
  const w = await routeWorld({ put: (req) => ({ ...FX.details[KEY], max_window_pct: req.body.max_window_pct === null ? 85 : req.body.max_window_pct, max_window_pct_set: req.body.max_window_pct !== null, pins: FX.details[KEY].pins, epic_pin: null, paused: false, nodes: FX.details[KEY].nodes, group: [], mcp_run: false }) });
  assert.equal(winIn(w).value, '');
  assert.equal(winIn(w).placeholder, '85');
  assert.match(text(cfg(w)), /Using the board's setting: 85 percent\./);
  change(winIn(w), '70');
  await settle();
  assert.deepEqual(puts(w).map((c) => c.body), [{ max_window_pct: 70 }]);
  assert.equal(winIn(w).value, '70');
  assert.match(text(cfg(w)), /This epic skips a node at 70 percent or more\./);
  for (const bad of ['9', '101', 'abc', '70.5', '-5']) {
    change(winIn(w), bad);
    await settle();
    assert.match(text(cfg(w).querySelector('#ep-win-err')), /^Use a whole number from 10 to 100/, bad);
    assert.equal(winIn(w).getAttribute('aria-invalid'), 'true');
  }
  assert.equal(puts(w).length, 1, 'a bad number is never sent');
  change(winIn(w), '');
  await settle();
  assert.deepEqual(puts(w).map((c) => c.body).at(-1), { max_window_pct: null });
  assert.match(text(cfg(w)), /Using the board's setting: 85 percent\./);
});

test('a refused window limit puts the old value back and says nothing was saved', async () => {
  const w = await routeWorld({ put: () => refusal(400, { error: 'max_window_pct is a whole number from 10 to 100' }) });
  change(winIn(w), '60');
  await settle();
  assert.equal(winIn(w).value, '');
  assert.match(toasts(w).at(-1).text, /Nothing was saved\.$/);
});

test('Let MCP run this epic is a checkbox that starts off, sends only mcp_run, and says in one paragraph what it allows and that an agent cannot turn it on', async () => {
  const w = await routeWorld({ put: (req) => ({ ...FX.details[KEY], mcp_run: req.body.mcp_run, pins: FX.details[KEY].pins, epic_pin: null, paused: false, nodes: FX.details[KEY].nodes }) });
  assert.equal(flagBox(w, 'mcp_run').checked, false);
  assert.match(text(cfg(w)), /Let MCP run this epic/);
  assert.match(text(cfg(w)), /epic_run tool: up to 10 issues a call and 6 calls an hour, each recorded with the token's name\. It cannot change settings\. Off by default\./);
  tickBox(flagBox(w, 'mcp_run'), true);
  await settle();
  assert.deepEqual(puts(w).map((c) => c.body), [{ mcp_run: true }]);
  assert.equal(flagBox(w, 'mcp_run').checked, true);
  const on = await routeWorld({ data: variant('groups') });
  assert.equal(flagBox(on, 'mcp_run').checked, true, 'an epic that allows it opens on');
});

test('the group boxes appear only with two paired nodes; two ticked make one group, one ticked is said and not written, none clears it, and several groups set elsewhere are shown, not edited', async () => {
  const solo = await routeWorld();
  assert.ok(cfg(solo).querySelector('[data-cfg-group]').classList.contains('hidden'), 'one paired node: no group to make');
  const w = await routeWorld({ data: variant('groups'), put: (req) => ({ ...FX.variants.groups.detail, ...req.body, pins: {}, epic_pin: null, paused: false, nodes: FX.variants.groups.detail.nodes }) });
  assert.ok(!cfg(w).querySelector('[data-cfg-group]').classList.contains('hidden'));
  assert.deepEqual(groupBoxes(w).map((b) => [b.getAttribute('data-group-node'), b.checked]), [['node-b', true], ['node-c', true]]);
  tickBox(groupBoxes(w)[1], false);
  await settle();
  assert.match(text(cfg(w).querySelector('#ep-group-err')), /^A group needs two or more nodes\./);
  assert.equal(puts(w).length, 0, 'one node is not a group: nothing is written');
  tickBox(groupBoxes(w)[0], false);
  await settle();
  assert.deepEqual(puts(w).map((c) => c.body), [{ group: [] }]);
  tickBox(groupBoxes(w)[0], true);
  tickBox(groupBoxes(w)[1], true);
  await settle();
  assert.deepEqual(puts(w).map((c) => c.body).at(-1), { group: [['node-b', 'node-c']] });
  const many = await routeWorld({ data: { ...variant('groups'), group: [['node-b', 'node-c'], ['node-x', 'node-y']] } });
  assert.ok(groupBoxes(many).every((b) => b.disabled === true || b.hasAttribute('disabled')));
  assert.match(text(many.run('document').querySelector('#ep-group-err')), /^Several groups are set for this epic\./);
});

test('the settings explain the window limit in words, including that an unknown window counts as 50', async () => {
  const w = await routeWorld();
  assert.match(text(cfg(w)), /skips a node whose rate-limit window for the issue's agent is at or above this percent\. A window that is not known counts as 50\. Headroom is an estimate\./);
  assert.match(text(cfg(w)), /Two nodes logged in to one subscription share its rate-limit window\./);
});

// ---------------------------------------------------------------- the demo

async function demoWorld({ search = '' } = {}) {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.location.search = search;
  w.ctx.__fx = clone(FX);
  w.run(`globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(__fx)) });`);
  return w;
}
const demoCall = (w, method, p, body) => { w.ctx.__a = [method, p, body]; return w.run('demoApi(...__a)').then(plain); };
const demoErr = (w, method, p, body) => demoCall(w, method, p, body).then(() => null, (e) => ({ status: e.status, body: plain(e.body) }));

test('demo: Run is played here: the route then says running, the ready issue is sent to node-b with its reason, and Pause and Resume are posts the demo answers', async () => {
  const w = await demoWorld();
  const run = await demoCall(w, 'POST', `${BASE}/run`);
  assert.equal(run.route.running, true);
  const d = await demoCall(w, 'GET', BASE);
  assert.equal(d.route.running, true);
  assert.equal(d.children.find((c) => c.number === 205).route.state, 'assigned');
  assert.match(d.children.find((c) => c.number === 205).route.text, /^chose node-b: 2 free lanes, 5-hour window 31 percent$/);
  const p = await demoCall(w, 'POST', `${BASE}/pause`);
  assert.deepEqual([p.paused, p.route.paused], [true, true]);
  assert.equal((await demoCall(w, 'GET', BASE)).paused, true);
  const r = await demoCall(w, 'POST', `${BASE}/resume`);
  assert.equal(r.paused, false);
});

test('demo: Run is refused with the board\'s sentence when writes are off or no node is paired', async () => {
  const off1 = await demoErr(await demoWorld({ search: '?epic=writesoff' }), 'POST', `${BASE}/run`);
  assert.deepEqual([off1.status, off1.body.reason], [409, 'writes_off']);
  const off2 = await demoErr(await demoWorld({ search: '?epic=nonodes' }), 'POST', `${BASE}/run`);
  assert.deepEqual([off2.status, off2.body.reason], [409, 'no_nodes']);
});

test('demo: Run on... plays a sent issue, and ?run=<word> plays each refusal with the same body the board sends', async () => {
  const w = await demoWorld();
  const ok = await demoCall(w, 'POST', `${BASE}/issues/205/run`, { node: 'auto' });
  assert.deepEqual([ok.ok, ok.handle, ok.name], [true, 'node-b', 'node-b']);
  assert.match(ok.why, /^chose node-b: 2 free lanes, 5-hour window 31 percent, pinned by the issue's pin$/);
  assert.equal(ok.data.task_ref, 'node-b:55');
  const want = { waiting: [409, 'waiting'], claimed: [409, 'running'], busy: [409, 'busy'], notfound: [404, 'not_found'] };
  for (const [flag, [status, reason]] of Object.entries(want)) {
    const e = await demoErr(await demoWorld({ search: `?demo=1&run=${flag}` }), 'POST', `${BASE}/issues/205/run`, { node: 'auto' });
    assert.deepEqual([e.status, e.body.reason], [status, reason], flag);
    assert.equal(e.body.ok, false);
  }
  const wait = await demoErr(await demoWorld({ search: '?run=waiting' }), 'POST', `${BASE}/issues/205/run`, { node: 'auto' });
  assert.match(wait.body.waiting.text, /^waiting: node-b 5-hour window is at 90 percent until 14:10$/);
  const loc = await demoErr(await demoWorld(), 'POST', `${BASE}/issues/205/run`, { node: 'local' });
  assert.match(loc.body.waiting.text, /^waiting: local is not a node paired with this board$/, 'routing never picks this board');
  const off1 = await demoErr(await demoWorld({ search: '?epic=offline' }), 'POST', `${BASE}/issues/205/run`, { node: 'node-b' });
  assert.match(off1.body.waiting.text, /^waiting: node-b \(named by you\) is offline$/);
});

test('demo: the settings PUT takes the routing keys and refuses what the board refuses', async () => {
  const w = await demoWorld({ search: '?epic=groups' });
  const put = await demoCall(w, 'PUT', `${BASE}/settings`, { max_window_pct: 60, mcp_run: false, group: [] });
  assert.deepEqual([put.max_window_pct, put.max_window_pct_set, put.mcp_run, put.group], [60, true, false, []]);
  const back = await demoCall(w, 'PUT', `${BASE}/settings`, { max_window_pct: null });
  assert.deepEqual([back.max_window_pct, back.max_window_pct_set], [85, false]);
  for (const bad of [{ max_window_pct: 5 }, { max_window_pct: 101 }, { max_window_pct: 'x' }, { group: [['node-b']] }, { group: [['node-b', 'local']] }, { group: [['node-b', 'node-z']] }, { group: [['node-b', 'node-c'], ['node-c', 'node-b']] }]) {
    const e = await demoErr(w, 'PUT', `${BASE}/settings`, bad);
    assert.equal(e.status, 400, JSON.stringify(bad));
  }
  const ok = await demoCall(w, 'PUT', `${BASE}/settings`, { group: [['node-b', 'node-c']] });
  assert.deepEqual(ok.group, [['node-b', 'node-c']]);
});

test('demo: every routing variant renders its state on the page', async () => {
  const cases = { run: () => 'Running', runwait: () => 'Running', runpaused: () => 'Resume', runagent: () => 'Running', runfinished: () => 'Run', runlimit: () => 'Run', nonodes: () => 'Run', paused: () => 'Resume', groups: () => 'Pause', writesoff: () => 'Run' };
  for (const [name, label] of Object.entries(cases)) {
    const w = await routeWorld({ data: variant(name) });
    const want = label();
    const shown = want === 'Resume' ? btn(pg(w), 'Resume') : (want === 'Pause' ? btn(pg(w), 'Pause') : runBtn(w));
    assert.ok(shown, name);
    if (want === 'Running' || want === 'Run') assert.equal(text(runBtn(w)), want, name);
    assert.ok(rows(w).length >= 1, name);
  }
});

test('the end to end demo page: Run, then Pause, then Resume, painted at once and confirmed by the next read', async () => {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.ctx.__fx = clone(FX);
  w.run(`globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(__fx)) });
         api = (method, path, body) => demoEpics(method, path, body);`);
  w.location.hash = HASH;
  await new Promise((r) => setTimeout(r, 400));
  assert.ok(!off(runBtn(w)));
  runBtn(w).click();
  await new Promise((r) => setTimeout(r, 500));
  assert.equal(text(runBtn(w)), 'Running');
  assert.equal(text(rowOf(w, 205).querySelector('.ep-route')), '→ chose node-b: 2 free lanes, 5-hour window 31 percent');
  btn(pg(w), 'Pause').click();
  await new Promise((r) => setTimeout(r, 400));
  assert.ok(btn(pg(w), 'Resume'));
  btn(pg(w), 'Resume').click();
  await new Promise((r) => setTimeout(r, 400));
  assert.ok(btn(pg(w), 'Pause'));
});

// ---------------------------------------------------------------- the source

test('the page code makes its routing writes to this board only, sets nothing through innerHTML or a style attribute, and puts no em-dash in the words', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'epic.js'), 'utf8');
  const run = src.slice(src.indexOf('/* ---------- routing: Run'), src.indexOf('/* ---------- the progress strip'));
  assert.ok(run.length > 3000);
  assert.ok(!/innerHTML|insertAdjacentHTML|cssText|setAttribute\('style'/.test(run));
  assert.ok(!run.includes('—'));
  const writes = run.match(/api\('(?:POST|PUT|DELETE|PATCH)', [^\n]*/g) || [];
  assert.equal(writes.length, 3, 'Run, Pause or Resume, and Run on... for one issue');
  assert.ok(writes.every((x) => x.startsWith("api('POST', epPath(P, ")), 'every write goes to this board\'s own epic routes');
  assert.ok(!/\bpermission|bypass|sandbox|--yolo|danger-full-access/i.test(run), 'the routing code names no permission setting');
});
