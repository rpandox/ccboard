// Contract tests for the epic view of issue #147 (app/static/pages/epic.js, pages/epic.css, the Tasks page's Epics tab, the palette's epic rows, the router's #/epic/ address and the demo's answers).
// Real scripts on minidom's DOM through tests/js/world.mjs; api() is the recorder, and its answers are the demo fixture (app/static/demo/epic.json) or a changed copy of it.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { STATIC } from './harness.mjs';
import { calls, fakeState, homeWorld, page, plain, text, tick } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'epic.json'), 'utf8'));
const KEY = 'example/website#147';
const HASH = '#/epic/example/website/147';
const clone = (v) => JSON.parse(JSON.stringify(v));
const detail = (over = {}) => ({ ...clone(FX.details[KEY]), ...over });

const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('epUnmount(); Epics.stopList()'); } catch (_) { /* never mounted */ } } });

const settle = async (n = 4) => { for (let i = 0; i < n; i++) await tick(); };
const later = (ms) => new Promise((r) => setTimeout(r, ms));

/** A world on the epic page. GET /api/epics/... answers __detail (a value or a function of the request); PUT .../settings answers __put (default: the request merged, like the board). */
async function epicWorld({ data = detail(), hash = HASH, state = fakeState(), put = null, list = clone(FX.list), mount = true } = {}) {
  const { w } = homeWorld({ state });
  made.push(w);
  w.load('pages/epic.js');
  w.ctx.__detail = data;
  w.ctx.__list = list;
  w.ctx.__put = put;
  w.run(`__answers['/api/epics'] = (req) => {
    if (req.method === 'PUT') return typeof __put === 'function' ? __put(req) : (__put || { ...__detail, ...(req.body || {}), pins: { ...__detail.pins, ...Object.fromEntries(Object.entries((req.body && req.body.pins) || {}).filter(([, h]) => h).map(([k, h]) => [k, { handle: h, node_name: h, source: 'page', stale: false }])) } });
    if (req.path === '/api/epics') return typeof __list === 'function' ? __list(req) : __list;
    return typeof __detail === 'function' ? __detail(req) : __detail;
  }`);
  if (mount) { w.location.hash = hash; await settle(); }
  return w;
}

const root = (w) => page(w).querySelector('.ep-page');
const rows = (w) => page(w).querySelectorAll('.ep-row');
const rowOf = (w, n) => rows(w).find((r) => r.getAttribute('data-n') === String(n));
const btn = (scope, label) => scope.querySelectorAll('button').find((b) => text(b).trim() === label);
const toasts = (w) => plain(w.get('__toasts'));
const puts = (w) => calls(w).filter((c) => c.method === 'PUT');
const gets = (w) => calls(w).filter((c) => c.method === 'GET');

test('the page mounts on #/epic/<owner>/<repo>/<n> and asks the board for that epic once', async () => {
  const w = await epicWorld();
  assert.ok(root(w));
  assert.deepEqual(gets(w).map((c) => c.path), ['/api/epics/example/website/147']);
  assert.equal(text(page(w).querySelector('.ep-h1')), 'Checkout rework');
  assert.equal(rows(w).length, 7);
});

// ---------------------------------------------------------------- the fixture itself (demo/epic.json is what ?demo=1 shows)

test('the demo epic has seven children across two nodes: done, running, queued, failed, unclaimed, a stale claim, a claim from a person who is not allowed and one whose claim is not known', () => {
  const d = FX.details[KEY];
  assert.equal(d.children.length, 7);
  const states = d.children.map((c) => `${c.bucket.node || '-'}:${c.bucket.state}`);
  assert.deepEqual(states, ['node-a:done', 'node-a:running', 'node-b:queued', 'node-b:failed', '-:open', '-:open', '-:unknown']);
  const unk = d.children.find((c) => c.claim_state === 'unknown');
  assert.deepEqual([unk.number, unk.claim, unk.claim_unknown], [207, null, { reason: 'too_long', text: 'too many comments to read' }]);
  assert.ok(d.children.filter((c) => c.claim_state !== 'unknown').every((c) => c.claim_unknown === null && (c.claim ? c.claim_state === 'claimed' : c.claim_state === 'none')));
  assert.ok(d.children.some((c) => c.claim && c.claim.live === false && c.claim.status === 'claimed'), 'a claim that was not renewed');
  assert.ok(d.children.some((c) => !c.claim && c.claim_rejected > 0 && c.claim_note === 'not from an allowed author'), 'a claim comment from someone not allowed');
  assert.deepEqual(d.nodes.map((n) => n.handle), ['local', 'node-b']);
});

test('the fixture adds up: the strip counts are the children buckets, closed ones included, and unclaimed + nodes = total', () => {
  const d = FX.details[KEY];
  const by = {};
  const un = { open: 0, closed: 0 };
  let unknown = 0;
  for (const c of d.children) {
    if (!c.bucket.node && c.bucket.state === 'unknown') { unknown += 1; continue; }
    if (!c.bucket.node) { un[c.bucket.state === 'closed' ? 'closed' : 'open'] += 1; continue; }
    by[c.bucket.node] = by[c.bucket.node] || { done: 0, running: 0, queued: 0, failed: 0 };
    by[c.bucket.node][c.bucket.state] += 1;
  }
  for (const [alias, p] of Object.entries(d.progress.by_node)) for (const k of ['done', 'running', 'queued', 'failed']) assert.equal(p[k], by[alias][k], `${alias} ${k}`);
  assert.deepEqual(d.progress.unclaimed, un);
  assert.equal(d.progress.unknown, unknown, 'the unknown ones are counted apart from the unclaimed');
  const sum = Object.values(d.progress.by_node).reduce((a, p) => a + p.done + p.running + p.queued + p.failed, 0) + un.open + un.closed + unknown;
  assert.equal(sum, d.progress.total);
  assert.equal(d.progress.total, d.progress.open + d.progress.closed);
});

test('the pin the page works out (page pin, then label pin, stale ones last) is the pin the board sent for every child of the fixture', async () => {
  const w = await epicWorld({ mount: false });
  const d = FX.details[KEY];
  for (const c of d.children) {
    w.ctx.__c = c; w.ctx.__d = d;
    const got = plain(w.run('epPinOf(__d, __c)'));
    assert.deepEqual(got && { handle: got.handle, source: got.source, stale: got.stale }, c.pin && { handle: c.pin.handle, source: c.pin.source, stale: c.pin.stale }, `#${c.number}`);
  }
});

// ---------------------------------------------------------------- the router address

test('#/epic/<owner>/<repo>/<n> parses and builds with GitHub\'s own name rules; the twelve routes of ROUTES are untouched', async () => {
  const w = await epicWorld({ mount: false });
  const parse = (h) => plain(w.run(`parseHash(${JSON.stringify(h)})`));
  assert.deepEqual(parse(HASH), { id: 'epic', params: { owner: 'example', repo: 'website', n: '147' }, query: {} });
  assert.equal(parse('#/epic/example/foo.js/9').params.repo, 'foo.js', 'a repository may hold a dot');
  for (const bad of ['#/epic/example/website/0', '#/epic/example/website/01', '#/epic/example/website/x', '#/epic/example/.hidden/1', '#/epic/example/-lead/1', '#/epic/example/../1', '#/epic/-lead/website/1',
    '#/epic/example/website', '#/epic/example/website/1/extra', '#/epic/a%2Fb/website/1', '#/epic/example/website/1234567890']) {
    assert.equal(parse(bad).unknown, true, bad);
  }
  assert.equal(w.run("buildHash('epic', { owner: 'example', repo: 'website', n: 147 })"), HASH);
  assert.equal(w.run("buildHash('epic', { owner: 'example', repo: 'foo.js', n: '9' })"), '#/epic/example/foo.js/9');
  for (const bad of ["{ owner: 'a/b', repo: 'c', n: 1 }", "{ owner: 'a', repo: '..', n: 1 }", "{ owner: 'a', repo: 'b', n: 0 }", "{ owner: 'a', repo: 'b' }", "{ owner: 'a', repo: 'b/c', n: 1 }"]) {
    assert.throws(() => w.run(`buildHash('epic', ${bad})`), /bad route param/, bad);
  }
  assert.deepEqual(plain(w.run('ROUTES.map((r) => r.id)')), ['home', 'inbox', 'tasks', 'agents', 'project', 'quad', 'usage', 'memory', 'onboarding', 'settings', 'search', 'session']);
});

// ---------------------------------------------------------------- the header

test('the header: title, the epic on GitHub, the counts, the rate note, Pause (bordered) and Run (the one filled primary, disabled, with its reason)', async () => {
  const w = await epicWorld();
  const pg = page(w);
  assert.equal(text(pg.querySelector('.ep-h1')), 'Checkout rework');
  const ref = pg.querySelector('.ep-name a');
  assert.equal(ref.getAttribute('href'), 'https://github.com/example/website/issues/147');
  assert.equal(text(ref), 'example/website#147');
  assert.equal(ref.getAttribute('target'), '_blank');
  assert.equal(ref.getAttribute('rel'), 'noopener noreferrer');
  assert.equal(text(pg.querySelector('.ep-counts')), '6 open, 1 closed');
  assert.equal(text(pg.querySelector('.ep-rate')), 'GitHub requests left: 4990 of 5000');
  const run = btn(pg, 'Run');
  assert.equal(run.getAttribute('disabled'), null, 'Run is on: the board can route and the epic is not paused');
  assert.ok(run.classList.contains('primary'));
  assert.equal(pg.querySelectorAll('button.primary').filter((b) => !b.classList.contains('tinted')).length, 1, 'one filled primary on the page (a Claim button is the tinted kind)');
  const why = pg.querySelector('#' + run.getAttribute('aria-describedby'));
  assert.match(text(why), /Run sends each ready issue to the node that has a free lane/);
  assert.ok(!btn(pg, 'Pause').classList.contains('primary'), 'Pause is bordered, not filled');
  assert.match(text(pg), /Pause stops this board routing the epic\. Nothing changes on GitHub\./);
  assert.equal(w.document.title, 'example/website#147 · epic · ccboard');
});

test('Run asks this board once, with no body, and the page never writes to GitHub (every request goes to this board\'s own epic routes)', async () => {
  const w = await epicWorld();
  btn(page(w), 'Run').click();
  await settle();
  assert.deepEqual(calls(w).filter((c) => c.method !== 'GET').map((c) => [c.method, c.path]), [['POST', '/api/epics/example/website/147/run']]);
  assert.ok(calls(w).every((c) => c.path.startsWith('/api/epics/example/website/147')));
});

// ---------------------------------------------------------------- the strip

test('the strip: one row per node and an Unclaimed row, a bar of segments (done, running, queued, failed) and the numbers in words with a glyph each', async () => {
  const w = await epicWorld();
  const prows = page(w).querySelectorAll('.ep-prow');
  assert.deepEqual(prows.map((r) => r.getAttribute('data-node')), ['local', 'node-b', 'Unclaimed', 'Claim not known']);
  const a = prows[0];
  assert.equal(text(a.querySelector('.nchip-n')), 'node-a');
  assert.equal(a.querySelector('.ep-bar').getAttribute('aria-label'), 'node-a: 1 done, 1 running');
  assert.equal(a.querySelector('.ep-bar').getAttribute('role'), 'img');
  assert.deepEqual(a.querySelectorAll('.ep-seg').map((s) => s.getAttribute('data-seg')), ['done', 'running']);
  assert.deepEqual(a.querySelectorAll('.ep-words li').map((li) => text(li).trim()), ['✓ 1 done', '✽ 1 running']);
  const b = prows[1];
  assert.deepEqual(b.querySelectorAll('.ep-words li').map((li) => text(li).trim()), ['∙ 1 queued', '✕ 1 failed']);
  assert.equal(b.querySelector('.ep-bar').getAttribute('aria-label'), 'node-b: 1 queued, 1 failed');
  const u = prows[2];
  assert.equal(text(u.querySelector('.ep-plabel')), 'Unclaimed');
  assert.deepEqual(u.querySelectorAll('.ep-words li').map((li) => text(li).trim()), ['○ 2 open']);
  const k = prows[3];
  assert.equal(text(k.querySelector('.ep-plabel')), 'Claim not known');
  assert.deepEqual(k.querySelectorAll('.ep-words li').map((li) => text(li).trim()), ['? 1 claim not known']);
  assert.equal(k.querySelector('.ep-bar').getAttribute('aria-label'), 'Claim not known: 1 claim not known');
  assert.equal(u.querySelectorAll('.ep-words li').length, 1, 'the unknown one is not in the Unclaimed row');
  const total = prows.reduce((n, r) => n + Number(/^(\d+)/.exec(text(r.querySelector('.ep-ptot')))[1]), 0);
  assert.equal(total, 7, 'the strip adds up to the children');
});

test('a segment\'s width is its count (set on the node, never as an attribute), and the states differ by class as well as colour', async () => {
  const d = detail();
  d.progress.by_node['node-a'] = { done: 3, running: 1, queued: 0, failed: 2, handle: 'local' };
  const w = await epicWorld({ data: d });
  const segs = page(w).querySelectorAll('.ep-prow')[0].querySelectorAll('.ep-seg');
  assert.deepEqual(segs.map((s) => [s.getAttribute('data-seg'), s.style.flexGrow]), [['done', '3'], ['running', '1'], ['failed', '2']]);
  assert.ok(segs.every((s) => s.getAttribute('style') === null));
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'epic.css'), 'utf8');
  const fills = ['done', 'running', 'queued', 'failed'].map((k) => new RegExp(`\\.ep-seg\\.${k}[^{]*\\{([^}]*)\\}`).exec(css)[1].replace(/\s+/g, ' ').trim());
  assert.equal(new Set(fills).size, 4, 'four different looks');
  assert.match(fills[2], /dashed/, 'queued is hollow with a dashed edge');
  assert.match(fills[1], /repeating-linear-gradient/);
  assert.match(fills[3], /repeating-linear-gradient/);
});

test('a node the board does not know (a claim by an alias that is no paired node) still gets its row, with a ? and a title saying so', async () => {
  const d = detail();
  d.progress.by_node['node-z'] = { done: 0, running: 0, queued: 1, failed: 0, handle: null };
  const w = await epicWorld({ data: d });
  const row = page(w).querySelectorAll('.ep-prow').find((r) => r.getAttribute('data-node') === 'node-z');
  assert.ok(row);
  assert.equal(text(row.querySelector('.nchip-g')), '?');
  assert.match(row.querySelector('.nchip').getAttribute('title'), /not a node paired with this board/);
});

// ---------------------------------------------------------------- the rows

test('rows keep the queue order, split into the issues a node holds and the Unclaimed, each with a glyph and its word', async () => {
  const w = await epicWorld();
  const groups = page(w).querySelectorAll('section.ep-sec[data-group]');
  assert.deepEqual(groups.map((g) => g.getAttribute('data-group')), ['nodes', 'free', 'unknown']);
  assert.match(text(groups[0].querySelector('h2')), /^On nodes \(4\)/);
  assert.match(text(groups[1].querySelector('h2')), /^Unclaimed \(2\)/);
  assert.match(text(groups[2].querySelector('h2')), /^Claim not known \(1\)/);
  assert.deepEqual(groups[2].querySelectorAll('.ep-row').map((r) => r.getAttribute('data-n')), ['207']);
  assert.deepEqual(groups[0].querySelectorAll('.ep-row').map((r) => r.getAttribute('data-n')), ['201', '202', '203', '204']);
  assert.deepEqual(groups[1].querySelectorAll('.ep-row').map((r) => r.getAttribute('data-n')), ['205', '206']);
  const word = (n) => rowOf(w, n).querySelector('.ep-g').getAttribute('aria-label');
  assert.deepEqual([201, 202, 203, 204, 205, 207].map(word), ['done', 'running', 'queued', 'failed', 'open', 'claim not known']);
  assert.equal(text(rowOf(w, 201).querySelector('.ep-g')), '✓');
  assert.equal(text(rowOf(w, 204).querySelector('.ep-g')), '✕');
});

test('a row: number and title, the node chip, the claim and the task phase as different words, the pull request, the age by GitHub\'s clock', async () => {
  const w = await epicWorld();
  const r = rowOf(w, 202);
  assert.equal(text(r.querySelector('.ep-num')), '#202');
  assert.equal(r.querySelector('.ep-num').getAttribute('href'), 'https://github.com/example/website/issues/202');
  assert.equal(text(r.querySelector('.ep-title')), 'Checkout form validation');
  assert.equal(text(r.querySelector('.nchip-n')), 'node-a');
  assert.equal(text(r.querySelector('.ep-claim')), 'Running by node-a, renewed 3 min ago');
  assert.equal(text(r.querySelector('.ep-task')), 'Task running on node-a');
  const pr = r.querySelector('.ep-pr');
  assert.equal(text(pr), 'PR #77 open');
  assert.equal(pr.getAttribute('href'), 'https://github.com/example/website/pull/77');
  assert.equal(pr.getAttribute('target'), '_blank');
  assert.equal(pr.getAttribute('rel'), 'noopener noreferrer');
  assert.equal(text(r.querySelector('.ep-age')), 'updated 4 min ago');
  assert.equal(text(rowOf(w, 203).querySelector('.ep-claim')), 'Claimed by node-b, renewed 12 min ago', 'claimed, running and done are different words');
  assert.equal(text(rowOf(w, 201).querySelector('.ep-claim')), 'Done by node-a');
  assert.equal(text(rowOf(w, 203).querySelector('.ep-dep')), 'waits for #202');
});

test('ages come from GitHub\'s clock (server_now), not the browser\'s: moving the browser clock changes nothing, a missing server_now shows no age', async () => {
  const w = await epicWorld();
  const before = text(rowOf(w, 202).querySelector('.ep-age'));
  w.run('Date.now = () => 4102444800000');                           // the browser says 2100
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.equal(text(rowOf(w, 202).querySelector('.ep-age')), before);
  const d = detail({ server_now: null });
  const w2 = await epicWorld({ data: d });
  assert.equal(rowOf(w2, 202).querySelector('.ep-age'), null);
  assert.doesNotMatch(text(rowOf(w2, 202).querySelector('.ep-claim')), /renewed/);
});

test('a claim that was not renewed shows as not live, with when it was last renewed; a claim from someone who is not allowed is only the sentence "not from an allowed author"', async () => {
  const w = await epicWorld();
  const stale = rowOf(w, 206).querySelector('.ep-claim');
  assert.equal(text(stale).replace(/^⚠\s*/, ''), 'Claimed by node-a, not live: last renewed 4 h ago');
  assert.ok(stale.classList.contains('warn'));
  const bad = rowOf(w, 205);
  assert.equal(text(bad.querySelector('.ep-claim')).replace(/^⚠\s*/, ''), 'not from an allowed author');
  assert.equal(bad.querySelector('.nchip'), null, 'no node chip: it is not a claim');
  assert.doesNotMatch(text(bad), /Claimed by|Running by/);
  assert.equal(bad.getAttribute('data-state'), 'open');
});

test('an offline node shows its last phase and how long ago', async () => {
  const d = detail();
  const c = d.children.find((x) => x.number === 203);
  c.task = { ...c.task, phase: 'running', status: 'offline', age_s: 7500 };
  const w = await epicWorld({ data: d });
  assert.equal(text(rowOf(w, 203).querySelector('.ep-task')), 'node-b is offline: last seen running 2 h ago');
});

test('a child whose claim is not known says why, in its own group, and is never shown as free; the reason is the board\'s sentence', async () => {
  const d = detail({ partial: true, comments_pending: 2 });
  for (const [n, reason, text_] of [[206, 'budget', 'not read yet in this refresh'], [205, 'rate_limited', 'GitHub asked us to wait']]) {
    const c = d.children.find((x) => x.number === n);
    Object.assign(c, { claim: null, claim_rejected: 0, claim_note: null, claim_state: 'unknown', claim_unknown: { reason, text: text_ }, bucket: { node: null, state: 'unknown' } });
  }
  d.progress.unclaimed.open = 0;
  d.progress.unknown = 3;
  const w = await epicWorld({ data: d });
  assert.equal(text(rowOf(w, 206).querySelector('.ep-claim')), '? Claim not known: not read yet in this refresh');
  assert.equal(text(rowOf(w, 205).querySelector('.ep-claim')), '? Claim not known: GitHub asked us to wait');
  assert.equal(text(rowOf(w, 207).querySelector('.ep-claim')), '? Claim not known: too many comments to read');
  const groups = page(w).querySelectorAll('section.ep-sec[data-group]').filter((g) => !g.classList.contains('hidden'));
  assert.deepEqual(groups.map((g) => g.getAttribute('data-group')), ['nodes', 'unknown'], 'nothing is Unclaimed: the free group is empty and hidden');
  assert.ok(page(w).querySelector('section[data-group="free"]').classList.contains('hidden'));
  assert.deepEqual(groups[1].querySelectorAll('.ep-row').map((r) => r.getAttribute('data-n')), ['205', '206', '207']);
  assert.match(text(groups[1]), /Only "Unclaimed" means free\./);
  assert.ok(rowOf(w, 206).querySelector('.ep-claim').classList.contains('warn'), 'attention, with a glyph and words');
  assert.match(text(page(w).querySelector('.ep-banner')), /2 issues still to be checked for claims/);
  const strip = page(w).querySelectorAll('.ep-prow');
  assert.equal(text(strip.find((r) => r.getAttribute('data-node') === 'Claim not known').querySelector('.ep-ptot')), '3 issues');
  assert.equal(strip.find((r) => r.getAttribute('data-node') === 'Unclaimed'), undefined, 'no Unclaimed row when nothing was read as free');
  assert.equal(rowOf(w, 202).querySelector('.dim.ep-claim'), null);
});

test('each open issue says when its claims were read, by GitHub\'s clock: "Unclaimed" is true for that moment only', async () => {
  const w = await epicWorld();
  const read = (n) => text(rowOf(w, n).querySelector('.ep-read'));
  assert.equal(read(205), 'claims read just now', 'the fixture\'s server_now is 09:00:00 and 205 was read at 08:59:40');
  assert.equal(read(206), 'claims read 4 min ago');
  assert.equal(rowOf(w, 201).querySelector('.ep-read'), null, 'a closed issue has no claim to be free');
  w.run("Date.now = () => Date.parse('2030-01-01T00:00:00Z')");
  w.run('epicPage.cur.data = epicPage.cur.data');
  assert.equal(read(206), 'claims read 4 min ago', 'the browser\'s clock decides nothing');
  const d = detail();
  d.children.find((c) => c.number === 205).claims_read_at = null;
  const w2 = await epicWorld({ data: d });
  assert.equal(rowOf(w2, 205).querySelector('.ep-read'), null, 'nothing to say when the board has no reading time');
});

test('the two newest reasons a claim is not known are shown as the board words them', async () => {
  const d = detail();
  Object.assign(d.children.find((c) => c.number === 205), { claim: null, claim_state: 'unknown', claim_unknown: { reason: 'not_recent', text: 'not read recently' }, bucket: { node: null, state: 'unknown' } });
  Object.assign(d.children.find((c) => c.number === 206), { claim: null, claim_state: 'unknown', claim_unknown: { reason: 'changed', text: 'changed while it was read' }, bucket: { node: null, state: 'unknown' } });
  const w = await epicWorld({ data: d });
  assert.equal(text(rowOf(w, 205).querySelector('.ep-claim')), '? Claim not known: not read recently');
  assert.equal(text(rowOf(w, 206).querySelector('.ep-claim')), '? Claim not known: changed while it was read');
  assert.equal(page(w).querySelectorAll('section[data-group="unknown"] .ep-row').length, 3);
});

test('a claim that is not known is also said on a closed issue only by its group: a closed child says nothing about claims', async () => {
  const d = detail();
  Object.assign(d.children[0], { claim_state: 'unknown', claim_unknown: { reason: 'budget', text: 'not read yet in this refresh' } });
  const w = await epicWorld({ data: d });
  assert.ok(!/Claim not known/.test(text(rowOf(w, 201))));
});

test('when the board cannot tell who may claim it says so, with what to do', async () => {
  const d = detail({ claims_note: 'cannot tell who may claim: gh login unknown' });
  const w = await epicWorld({ data: d });
  const note = text(page(w).querySelector('.ep-banner'));
  assert.match(note, /cannot tell who may claim: gh login unknown/);
  assert.match(note, /CCBOARD_EPIC_AUTHORS/);
});

test('a dependency cycle and an issue of another repository are marked', async () => {
  const d = detail();
  d.children[1].dependency_cycle = true;
  d.children[2].repo = 'example/api';
  const w = await epicWorld({ data: d });
  assert.match(text(rowOf(w, 202).querySelector('.ep-meta2')), /dependency cycle/);
  assert.equal(text(rowOf(w, 203).querySelector('.ep-other')), 'example/api');
});

// ---------------------------------------------------------------- pins

const menuLabels = (w) => w.document.querySelectorAll('.menu-pop .menuitem').map((i) => text(i.querySelector('.mi-name') || i).trim());
const menuPick = (w, label) => w.document.querySelectorAll('.menu-pop .menuitem').find((i) => text(i.querySelector('.mi-name') || i).trim().replace(/^✓ /, '') === label).click();
const pinBtn = (w, n) => rowOf(w, n).querySelector('.ep-pin-btn');
const defer = () => { let resolve, reject; const p = new Promise((a, b) => { resolve = a; reject = b; }); return { p, resolve, reject }; };

test('the pin menu lists No pin and every paired node, marks the current one, and choosing one writes the pin and shows it at once', async () => {
  const gate = defer();
  const w = await epicWorld({ put: () => gate.p });
  pinBtn(w, 203).click();
  assert.deepEqual(menuLabels(w), ['✓ No pin', 'node-a', 'node-b']);
  assert.equal(pinBtn(w, 203).getAttribute('aria-expanded'), 'true');
  menuPick(w, 'node-b');
  assert.deepEqual(puts(w).map((c) => [c.path, c.body]), [['/api/epics/example/website/147/settings', { pins: { 203: 'node-b' } }]]);
  assert.equal(text(rowOf(w, 203).querySelector('.ep-pintext')), 'pinned: node-b', 'painted before the answer');
  gate.resolve({ pins: { 203: { handle: 'node-b', node_name: 'node-b', source: 'page', stale: false }, 205: FX.details[KEY].pins[205] }, epic_pin: null, paused: false, report: false, auto_pr: false, nodes: FX.details[KEY].nodes });
  await settle();
  assert.equal(text(rowOf(w, 203).querySelector('.ep-pintext')), 'pinned: node-b');
});

test('the current pin carries the tick, and No pin removes a page pin', async () => {
  const w = await epicWorld();
  pinBtn(w, 205).click();
  assert.deepEqual(menuLabels(w), ['No pin', 'node-a', '✓ node-b']);
  menuPick(w, 'No pin');
  assert.deepEqual(puts(w).map((c) => c.body), [{ pins: { 205: null } }]);
  await settle();
  assert.equal(rowOf(w, 205).querySelector('.ep-pintext'), null);
});

test('a refused pin puts the old one back and says why in a toast; nothing else changes', async () => {
  const w = await epicWorld({ put: () => Promise.reject(Object.assign(new Error('a pin names a node that is not paired with this board'), { status: 400 })) });
  assert.equal(text(rowOf(w, 205).querySelector('.ep-pintext')), 'pinned: node-b');
  pinBtn(w, 205).click();
  menuPick(w, 'node-a');
  assert.equal(text(rowOf(w, 205).querySelector('.ep-pintext')), 'pinned: node-a', 'painted at once');
  await settle();
  assert.equal(text(rowOf(w, 205).querySelector('.ep-pintext')), 'pinned: node-b', 'restored');
  assert.match(toasts(w).at(-1).text, /a pin names a node that is not paired with this board\. Nothing was saved\./);
  assert.equal(toasts(w).at(-1).kind, 'bad');
});

test('a pin to a node that is no longer paired is shown as ignored; a label pin is read; the page pin wins over a label pin', async () => {
  const d = detail();
  d.pins = { 203: { handle: 'node-gone', node_name: null, source: 'page', stale: true }, 204: { handle: 'local', node_name: 'node-a', source: 'page', stale: false } };
  d.children[3].label_pin = { alias: 'node-b', handle: 'node-b', stale: false };                    // 204: a page pin to node-a and a label pin to node-b
  d.children[4].label_pin = { alias: 'node-b', handle: 'node-b', stale: false };                    // 205: a label pin only (its page pin is dropped below)
  const w = await epicWorld({ data: d });
  assert.equal(text(rowOf(w, 203).querySelector('.ep-pintext')), 'pinned to node-gone, which is no longer paired: ignored');
  assert.ok(rowOf(w, 203).querySelector('.ep-pintext').classList.contains('warn'));
  assert.equal(text(rowOf(w, 204).querySelector('.ep-pintext')), 'pinned: node-a', 'the page pin wins');
  assert.equal(text(rowOf(w, 205).querySelector('.ep-pintext')), 'label pin: node-b');
  assert.equal(text(rowOf(w, 206).querySelector('.ep-pintext')), 'label names node-c, which is not paired: ignored');
});

test('a stale page pin falls back to a good label pin', async () => {
  const d = detail();
  d.pins = { 203: { handle: 'node-gone', node_name: null, source: 'page', stale: true } };
  d.children[2].label_pin = { alias: 'node-b', handle: 'node-b', stale: false };
  const w = await epicWorld({ data: d });
  assert.equal(text(rowOf(w, 203).querySelector('.ep-pintext')), 'label pin: node-b');
});

test('the epic pin: Pin epic opens the same menu, writes epic_pin and shows it; a refusal restores', async () => {
  const w = await epicWorld();
  assert.equal(text(page(w).querySelector('.ep-epicpin')), 'Epic: no pin');
  btn(page(w), 'Pin epic').click();
  assert.deepEqual(menuLabels(w), ['✓ No pin', 'node-a', 'node-b']);
  menuPick(w, 'node-b');
  assert.deepEqual(puts(w).map((c) => c.body), [{ epic_pin: 'node-b' }]);
  assert.equal(text(page(w).querySelector('.ep-epicpin')), 'Epic pinned to node-b');
  const w2 = await epicWorld({ put: () => Promise.reject(new Error('nope')) });
  btn(page(w2), 'Pin epic').click();
  menuPick(w2, 'node-a');
  await settle();
  assert.equal(text(page(w2).querySelector('.ep-epicpin')), 'Epic: no pin');
  assert.match(toasts(w2).at(-1).text, /nope\. Nothing was saved\./);
});

test('the pin button is a labelled menu button that names the issue and says nothing changes on GitHub', async () => {
  const w = await epicWorld();
  const b = pinBtn(w, 202);
  assert.equal(b.getAttribute('aria-label'), 'Pin issue 202 to a node');
  assert.match(b.getAttribute('title'), /nothing changes on GitHub/);
  assert.equal(b.getAttribute('aria-haspopup'), 'menu');
});

// ---------------------------------------------------------------- Pause and Resume

const posted = (w) => calls(w).filter((c) => c.method === 'POST');

test('Pause flips to Resume at once, posts to /pause, and says this board stops routing while GitHub is untouched; Resume posts to /resume and flips back', async () => {
  const gate = defer();
  const w = await epicWorld({ put: () => gate.p });
  w.run(`__answers['/api/epics'] = ((was) => (req) => (req.method === 'POST' ? (req.path.endsWith('/pause') ? __gate : { paused: false, route: { ...__detail.route, paused: false } }) : was(req)))(__answers['/api/epics'])`);
  w.ctx.__gate = gate.p;
  const pg = page(w);
  btn(pg, 'Pause').click();
  assert.ok(btn(pg, 'Resume'), 'painted before the answer');
  assert.equal(btn(pg, 'Resume').getAttribute('aria-pressed'), 'true');
  assert.match(text(pg), /Paused on this board: it does not route this epic\. Nothing changes on GitHub\./);
  assert.equal(btn(pg, 'Run').getAttribute('disabled'), '', 'Run is off while paused');
  assert.match(text(pg.querySelector('#ep-run-why')), /Run is off while this epic is paused\. Resume first\./);
  assert.deepEqual(posted(w).map((c) => [c.path, c.body]), [['/api/epics/example/website/147/pause', undefined]]);
  assert.equal(puts(w).length, 0, 'Pause is no longer a settings write');
  gate.resolve({ paused: true, route: { ...FX.details[KEY].route, paused: true } });
  await settle();
  assert.ok(btn(pg, 'Resume'));
  btn(pg, 'Resume').click();
  assert.ok(btn(pg, 'Pause'));
  await settle();
  assert.deepEqual(posted(w).map((c) => c.path), ['/api/epics/example/website/147/pause', '/api/epics/example/website/147/resume']);
  assert.equal(btn(pg, 'Run').getAttribute('disabled'), null, 'Run is back after Resume');
});

test('a refused Pause puts Pause back and shows the board\'s words in a toast', async () => {
  const w = await epicWorld({ put: () => Promise.reject(Object.assign(new Error('epic pins and pause are set by a signed-in person'), { status: 403 })) });
  w.run(`__answers['/api/epics'] = ((was) => (req) => (req.method === 'POST' ? Promise.reject(Object.assign(new Error('epic pins and pause are set by a signed-in person'), { status: 403 })) : was(req)))(__answers['/api/epics'])`);
  btn(page(w), 'Pause').click();
  assert.ok(btn(page(w), 'Resume'));
  await settle();
  assert.ok(btn(page(w), 'Pause'));
  assert.equal(btn(page(w), 'Pause').getAttribute('aria-pressed'), 'false');
  assert.match(toasts(w).at(-1).text, /epic pins and pause are set by a signed-in person\. Nothing was saved\./);
});

test('an epic that is paused opens as Resume', async () => {
  const w = await epicWorld({ data: detail({ paused: true }) });
  assert.ok(btn(page(w), 'Resume'));
  assert.match(text(page(w)), /Paused on this board/);
});

// ---------------------------------------------------------------- errors and empties: each says what to do next, none is a blank page, the last read stays

const variant = (name) => detail(clone(FX.variants[name].detail));
const banner = (w) => text(page(w).querySelector('.ep-banner'));

test('no gh login: the board\'s sentence and the next step, the header stays, there are no rows and no empty-epic message', async () => {
  const w = await epicWorld({ data: variant('nologin') });
  assert.match(banner(w), /gh is not logged in on this board: run gh auth login there/);
  assert.match(banner(w), /Run gh auth login on this board, then reload this page\./);
  assert.equal(rows(w).length, 0);
  assert.equal(page(w).querySelectorAll('.ep-prow').length, 0);
  assert.ok(btn(page(w), 'Pause'), 'the header is still there');
  assert.equal(text(page(w).querySelector('.ep-h1')), 'Epic #147', 'no title is known');
  assert.doesNotMatch(text(page(w)), /This epic has no issues yet/);
  assert.ok(page(w).querySelector('.ep-note.bad'));
});

test('no access to the repository, no such issue, no gh: each has its own sentence and its own next step', async () => {
  const cases = [['noaccess', /this gh login cannot read that repository/, /Check that the gh login on this board can read this repository/],
    ['notfound', /GitHub has no such issue, or this login cannot see it/, /Check the address, or that the gh login on this board can see this issue/]];
  for (const [name, a, b] of cases) {
    const w = await epicWorld({ data: variant(name) });
    assert.match(banner(w), a, name);
    assert.match(banner(w), b, name);
    assert.equal(rows(w).length, 0, name);
  }
  const w = await epicWorld({ data: detail({ ...clone(FX.blank), error: { reason: 'no_gh', message: 'gh is not installed on this board, so epics cannot be read from GitHub', wait_s: null } }) });
  assert.match(banner(w), /Install the GitHub CLI \(gh\) on this board, then run gh auth login\./);
});

test('a rate-limit pause says "GitHub asked us to wait 40 s", keeps the last read with its age, and the page asks again after the wait, not before and not in a loop', async () => {
  const w = await epicWorld({ data: detail(clone(FX.variants.wait.detail)) });
  assert.match(banner(w), /GitHub asked us to wait 40 s/);
  assert.match(banner(w), /asks again by itself once the time is up/);
  assert.match(banner(w), /Showing the last read, from 2 min ago\./);
  assert.equal(rows(w).length, 7, 'the last read stays');
  assert.ok(page(w).querySelector('.ep-note.warn'));
  assert.equal(text(page(w).querySelector('.ep-rate')), '', 'no "requests left" line while waiting');
  assert.equal(w.run('epDelay(epicPage.cur)'), 41000);
  w.ctx.__d = { error: { reason: 'rate_limited', message: 'x', wait_s: 3600 } };
  assert.equal(w.run('epDelay({ data: __d })'), 65000, 'a long wait is capped; the page looks again and the board answers from its own back-off');
  assert.equal(w.run("epDelay({ data: { error: null } })"), 30000);
});

test('a slow or broken GitHub keeps the last read and says how old it is', async () => {
  const w = await epicWorld({ data: detail(clone(FX.variants.stale.detail)) });
  assert.match(banner(w), /gh did not answer in time/);
  assert.match(banner(w), /GitHub is slow to answer\./);
  assert.match(banner(w), /Showing the last read, from 2 min ago\./);
  assert.equal(rows(w).length, 7);
});

test('the board itself failing (a 5xx or no network) is a quiet note with the page kept, never a blank screen; the page asks again in 15 s', async () => {
  const w = await epicWorld({ data: () => Promise.reject(Object.assign(new Error('503 Service Unavailable'), { status: 503 })) });
  assert.match(banner(w), /Could not reach this board: 503 Service Unavailable\./);
  assert.ok(page(w).querySelector('.ep-h1'));
  assert.ok(btn(page(w), 'Pause'));
  assert.equal(w.run('epDelay(epicPage.cur)'), 15000);
  w.ctx.__detail = detail();
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.equal(banner(w), '');
  assert.equal(rows(w).length, 7, 'it recovers on the next read');
});

test('a 400 (an address the board refuses) reads as a plain sentence', async () => {
  const w = await epicWorld({ data: () => Promise.reject(Object.assign(new Error('not a valid GitHub owner, repository or issue number'), { status: 400 })) });
  assert.match(banner(w), /that is not a GitHub epic address/);
});

test('an empty epic says "This epic has no issues yet. Add sub-issues on GitHub." with a link to it, and shows no strip', async () => {
  const w = await epicWorld({ data: variant('empty') });
  const pg = page(w);
  assert.match(text(pg.querySelector('.ep-emptybox')), /This epic has no issues yet\. Add sub-issues on GitHub\./);
  assert.equal(pg.querySelector('.ep-emptybox a').getAttribute('href'), 'https://github.com/example/website/issues/1', 'the link is the one the board built');
  assert.equal(pg.querySelectorAll('.ep-prow').length, 0);
  assert.equal(rows(w).length, 0);
  assert.ok(pg.querySelector('.ep-emptybox').classList.contains('hidden') === false);
});

test('the label fallback gives a quiet note, not an error; truncation and pending claims are plain notes too', async () => {
  const w = await epicWorld({ data: detail({ ...clone(FX.variants.labels.detail), truncated: true }) });
  const notes = page(w).querySelectorAll('.ep-note');
  assert.deepEqual(notes.map((n) => text(n)), ['This repository has no sub-issues endpoint, so the children are the issues labelled epic:147.',
    'This epic has more issues than shown: only the first 100 are read, so the numbers here are a floor.']);
  assert.ok(notes[0].classList.contains('dim'), 'the label note is quiet');
  assert.ok(notes[1].classList.contains('warn'), 'an incomplete epic is not quiet: it never presents itself as complete');
  assert.equal(rows(w).length, 7);
});

// ---------------------------------------------------------------- untrusted text

const HOSTILE = '<img src=x onerror=alert(1)><script>alert(2)</script>';

test('a hostile title, label, alias, branch and note render as text: no element is made from them', async () => {
  const d = detail();
  d.epic.title = HOSTILE;
  d.children[0].title = HOSTILE;
  d.children[1].labels = [HOSTILE];
  d.children[1].claim.alias = HOSTILE;
  d.children[1].claim.branch = HOSTILE;
  d.children[1].task.node_name = HOSTILE;
  d.children[3].claim.alias = HOSTILE;
  d.children[4].claim_note = HOSTILE;
  d.note = HOSTILE;
  d.error = { reason: 'timeout', message: HOSTILE, wait_s: null };
  d.progress.by_node[HOSTILE] = { done: 1, running: 0, queued: 0, failed: 0, handle: null };
  const w = await epicWorld({ data: d });
  const pg = page(w);
  for (const tag of ['img', 'script']) assert.equal(pg.querySelectorAll(tag).length, 0, tag);
  assert.equal(text(pg.querySelector('.ep-h1')), HOSTILE);
  assert.equal(text(rowOf(w, 201).querySelector('.ep-title')), HOSTILE);
  assert.ok(text(pg.querySelector('.ep-banner')).includes(HOSTILE));
  assert.ok(text(pg).includes(HOSTILE));
  const all = [];
  const walk = (n) => { for (const c of n.children) { all.push(c); walk(c); } };
  walk(pg);
  assert.ok(all.every((n) => !/^(IMG|SCRIPT|IFRAME|OBJECT)$/.test(n.tagName)));
  assert.ok(all.every((n) => n.getAttribute('onerror') === null && n.getAttribute('style') === null));
});

test('a link is drawn only for a github.com issue or pull address; any other address from the payload is plain text', async () => {
  const d = detail();
  d.epic.url = 'javascript:alert(1)';
  d.children[0].url = 'https://evil.example/example/website/issues/201';
  d.children[1].url = 'https://github.com.evil.example/example/website/issues/202';
  d.children[1].pr.url = 'https://github.com/example/website/pull/77/../../../x';
  d.children[2].url = 'http://github.com/example/website/issues/203';
  d.children[3].url = 'https://github.com/example/website/issues/204?x=1';
  d.children[5].url = 'https://github.com/example/website/issues/206';
  const w = await epicWorld({ data: d });
  const links = page(w).querySelectorAll('a').map((a) => a.getAttribute('href'));
  assert.ok(links.every((h) => h.startsWith('#/') || /^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+\/(issues|pull)\/[0-9]+$/.test(h)), links.join(' '));
  assert.ok(links.includes('https://github.com/example/website/issues/206'));
  assert.equal(rowOf(w, 201).querySelector('a.ep-num'), null);
  assert.equal(text(rowOf(w, 201).querySelector('.ep-num')), '#201');
  assert.equal(rowOf(w, 202).querySelector('a.ep-pr'), null);
  assert.equal(page(w).querySelector('.ep-name a'), null);
  for (const a of page(w).querySelectorAll('a')) {
    if (a.getAttribute('href').startsWith('https://')) { assert.equal(a.getAttribute('target'), '_blank'); assert.equal(a.getAttribute('rel'), 'noopener noreferrer'); }
  }
});

test('the source never writes HTML: no innerHTML, no insertAdjacentHTML, no style attribute, no inline handler', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'epic.js'), 'utf8');
  assert.doesNotMatch(src, /innerHTML|insertAdjacentHTML|outerHTML|cssText|document\.write|eval\(|new Function/);
  assert.doesNotMatch(src, /setAttribute\(\s*['"]style/);
  assert.doesNotMatch(src, /[^\w]style:\s/);
  assert.doesNotMatch(src, /—/, 'no em-dash in the UI text');
});

// ---------------------------------------------------------------- polling: only while visible, never rebuilding a row that looks the same

/** Replace the world's timers with a recorder: every setTimeout call is {fn, ms}; clearTimeout marks it cleared. */
function fakeTimers(w) {
  w.ctx.__timers = [];
  w.run(`globalThis.setTimeout = (fn, ms) => { const t = { ms, cleared: false }; t.fn = () => { t.cleared = true; fn(); }; __timers.push(t); return __timers.length; };
         globalThis.clearTimeout = (id) => { if (__timers[id - 1]) __timers[id - 1].cleared = true; };`);
  return () => w.ctx.__timers;
}

test('the page asks again every 30 s while it is shown; a hidden tab does not ask, and asks once when it comes back; leaving stops everything', async () => {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  const timers = fakeTimers(w);
  w.ctx.__detail = detail();
  w.run("__answers['/api/epics'] = () => __detail");
  w.location.hash = HASH;
  await settle();
  const live = () => timers().filter((t) => !t.cleared);
  assert.deepEqual(live().map((t) => t.ms), [30000]);
  assert.equal(gets(w).length, 1);
  live()[0].fn();
  await settle();
  assert.equal(gets(w).length, 2, 'the timer asks');
  assert.deepEqual(live().map((t) => t.ms), [30000], 'and sets the next one');
  w.document.hidden = true;
  timers().at(-1).fn();
  await settle();
  assert.equal(gets(w).length, 2, 'hidden: no ask');
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  await settle();
  assert.equal(gets(w).length, 3, 'visible again: one ask');
  w.location.hash = '#/inbox';
  await settle();
  const before = gets(w).length;
  w.document.dispatch('visibilitychange');
  await settle();
  assert.equal(gets(w).length, before, 'unmounted: the listener is gone');
  assert.deepEqual(live().filter((t) => t.ms === 30000), [], 'no timer is left');
});

test('a poll with the same data rebuilds nothing: the rows, their buttons and the strip are the same nodes; one changed issue repaints only its own row', async () => {
  const w = await epicWorld();
  const r202 = rowOf(w, 202);
  const r203 = rowOf(w, 203);
  const pin202 = pinBtn(w, 202);
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.equal(rowOf(w, 202), r202);
  assert.equal(pinBtn(w, 202), pin202, 'the button inside it is the same node');
  const d = detail();
  d.children[2].task = { ...d.children[2].task, phase: 'running' };
  w.ctx.__detail = d;
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.equal(rowOf(w, 203), r203, 'same row node, repainted in place');
  assert.equal(text(rowOf(w, 203).querySelector('.ep-task')), 'Task running on node-b');
  assert.equal(pinBtn(w, 202), pin202, 'the other row was not touched');
});

test('a poll never moves the row you are on: a focused row keeps its place and its button, and repaints once focus leaves', async () => {
  const w = await epicWorld();
  const b = pinBtn(w, 203);
  b.focus();
  const d = detail();
  d.children[2].task = { ...d.children[2].task, phase: 'running' };
  d.children[2].title = 'Address autocomplete (renamed)';
  w.ctx.__detail = d;
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.equal(pinBtn(w, 203), b, 'the focused button is still the one in the page');
  assert.equal(text(rowOf(w, 203).querySelector('.ep-title')), 'Address autocomplete', 'held back while focus is inside');
  b.blur();
  rowOf(w, 203).dispatchEvent({ type: 'focusout' });
  await new Promise((r) => setTimeout(r, 5));
  assert.equal(text(rowOf(w, 203).querySelector('.ep-title')), 'Address autocomplete (renamed)');
});

test('an open pin menu holds its row back too', async () => {
  const w = await epicWorld();
  pinBtn(w, 204).click();
  const d = detail();
  d.children[3].title = 'Payment retry (renamed)';
  w.ctx.__detail = d;
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.equal(text(rowOf(w, 204).querySelector('.ep-title')), 'Payment retry logic');
});

test('children follow the order GitHub keeps: a re-ordered answer re-orders the rows in place', async () => {
  const w = await epicWorld();
  const d = detail();
  d.children = [d.children[1], d.children[0], ...d.children.slice(2)];
  w.ctx.__detail = d;
  await w.run('epFetch(epicPage.cur)');
  await settle();
  const group = page(w).querySelector('section[data-group=nodes]');
  assert.deepEqual(group.querySelectorAll('.ep-row').map((r) => r.getAttribute('data-n')), ['202', '201', '203', '204']);
});

test('opening an epic remembers that this viewer has epics, so the Tasks page shows its Epics tab next time', async () => {
  const w = await epicWorld();
  assert.equal(w.localStorage.getItem('ccboard:epics'), '1');
});

// ---------------------------------------------------------------- entry point: the Tasks page's Epics tab

const LAZY_STUB = "globalThis.Lazy = { done: () => true, run: (n, fn) => fn(), wants: () => false, load: () => Promise.resolve(), pending: () => [], later() {}, warm() {} };";

/** A world on the Tasks page with the epic bundle "loaded" (Lazy stubbed to run at once). `issue` puts a GitHub issue on one task, the sign the tab keys on. */
async function tasksWorld({ issue = true, extra = {}, hash = '#/tasks', list = clone(FX.list), storage = null } = {}) {
  const st = fakeState({ tasks: [
    { id: 5, slug: 'stock-sync', title: 'Sync variant stock', project: 'phasezero', repo: 'api', tmux: 'phasezero--api--t-stock', column: 'in_progress', mode: 'worktree', pr_number: null, pr_url: null, pr_state: null, phase: 'running', ...(issue ? { issue_number: 47, issue_url: 'https://github.com/example/board/issues/47' } : {}) },
  ], ...extra });
  const { w } = homeWorld({ state: st });
  made.push(w);
  w.load('pages/epic.js');
  w.run(LAZY_STUB);
  if (storage) w.localStorage.setItem('ccboard:epics', storage);
  w.ctx.__list = list;
  w.run(`__answers['/api/epics'] = (req) => (typeof __list === 'function' ? __list(req) : __list)`);
  w.location.hash = hash;
  await settle();
  return w;
}

const tabs = (w) => page(w).querySelector('.tk-tabs');
const tabBtn = (w, label) => tabs(w).querySelectorAll('button').find((b) => text(b) === label);

test('a board with no GitHub issue task, no paired node and no epic visit keeps its Tasks page as it was: no tab, no Epics panel, no request', async () => {
  const w = await tasksWorld({ issue: false });
  assert.equal(tabs(w), null);
  assert.equal(page(w).querySelector('#tasks-epics'), null);
  assert.deepEqual(calls(w).filter((c) => c.path.startsWith('/api/epics')), []);
});

test('a task from a GitHub issue, a paired node, or an earlier epic visit brings the Board | Epics switch; Board is the pressed one', async () => {
  for (const w of [await tasksWorld({ issue: true }), await tasksWorld({ issue: false, extra: { nodes_enabled: true } }), await tasksWorld({ issue: false, storage: '1' })]) {
    assert.deepEqual(tabs(w).querySelectorAll('button').map((b) => [text(b), b.getAttribute('aria-pressed')]), [['Board', 'true'], ['Epics', 'false']]);
    assert.equal(tabs(w).getAttribute('role'), 'group');
    assert.equal(tabs(w).getAttribute('aria-label'), 'Tasks view');
    assert.ok(page(w).querySelector('#tasks-epics').classList.contains('hidden'));
    assert.deepEqual(calls(w).filter((c) => c.path.startsWith('/api/epics')), [], 'the board tab asks GitHub nothing');
  }
});

test('Epics: the address becomes #/tasks?tab=epics, the board gives way to the list, the list is read once and shows each epic with its counts, a paused mark and links', async () => {
  const w = await tasksWorld();
  tabBtn(w, 'Epics').click();
  await settle();
  assert.equal(w.location.hash, '#/tasks?tab=epics');
  assert.equal(tabBtn(w, 'Epics').getAttribute('aria-pressed'), 'true');
  assert.equal(tabBtn(w, 'Board').getAttribute('aria-pressed'), 'false');
  assert.ok(page(w).querySelector('#tasks').classList.contains('hidden'));
  assert.ok(!page(w).querySelector('#tasks-epics').classList.contains('hidden'));
  assert.deepEqual(calls(w).filter((c) => c.path.startsWith('/api/epics')).map((c) => c.path), ['/api/epics']);
  const lrows = page(w).querySelectorAll('.ep-lrow');
  assert.deepEqual(lrows.map((r) => r.getAttribute('data-epic')), ['example/website#147', 'example/board#52']);
  const first = lrows[0];
  assert.equal(first.querySelector('.ep-ltitle').getAttribute('href'), '#/epic/example/website/147');
  assert.equal(text(first.querySelector('.ep-ltitle')), 'Checkout rework');
  assert.match(text(first.querySelector('.ep-lmeta')), /6 open, 1 closed/);
  assert.ok(!/more issues than shown/.test(text(first.querySelector('.ep-lmeta'))));
  assert.match(text(first.querySelector('.ep-lmeta')), /updated 4 min ago/);
  assert.equal(first.querySelector('.badge'), null);
  assert.equal(text(lrows[1].querySelector('.badge')), 'paused');
  const gh = first.querySelector('.ep-lgh');
  assert.equal(gh.getAttribute('href'), 'https://github.com/example/website/issues/147');
  assert.equal(gh.getAttribute('rel'), 'noopener noreferrer');
  assert.match(text(page(w)), /Looked in 2 repositories: example\/website, example\/board\./);
});

test('the Epics list never looks complete when it is not: more issues, more epics and more repositories than it reads are each said', async () => {
  const list = clone(FX.list);
  list.epics[0].children = { open: 100, closed: 0, total: 100, more: true };
  list.truncated = true;
  list.repos_truncated = true;
  const w = await tasksWorld({ list });
  tabBtn(w, 'Epics').click();
  await settle();
  const t = text(page(w));
  assert.match(text(page(w).querySelectorAll('.ep-lrow')[0].querySelector('.ep-lmeta')), /100 open, 0 closed, more issues than shown/);
  assert.match(t, /There are more epics than shown\./);
  assert.match(t, /This board has more repositories than it looks in\./);
});

test('an Epics list that stopped at its call limit says what it did not read, and an epic not read is never shown as having no issues (issue #147)', async () => {
  const list = clone(FX.list);
  list.epics[1].children = null;
  list.epics[1].children_state = 'budget';
  list.partial = true;
  list.deferred_repos = ['example/api', 'example/docs', 'example/site', 'example/tools', 'example/more'];
  list.deferred_counts = 1;
  list.budget = { calls: 40, max: 40 };
  const w = await tasksWorld({ list });
  tabBtn(w, 'Epics').click();
  await settle();
  const rows = page(w).querySelectorAll('.ep-lrow');
  assert.match(text(rows[0].querySelector('.ep-lmeta')), /6 open, 1 closed/);
  assert.match(text(rows[1].querySelector('.ep-lmeta')), /issues not read this time/);
  assert.ok(!/0 open|0 closed/.test(text(rows[1].querySelector('.ep-lmeta'))), 'unknown is not zero');
  const note = page(w).querySelector('[data-partial]');
  assert.equal(text(note.querySelector('.ep-note-t')), 'This list stopped at its GitHub call limit.');
  assert.equal(text(note.querySelector('.ep-note-n')), 'not looked in this time: example/api, example/docs, example/site, example/tools and 1 more; 1 epic with their issues not read this time. The next refresh carries on from there.');
  const w2 = await tasksWorld({ list: clone(FX.list) });
  tabBtn(w2, 'Epics').click();
  await settle();
  assert.equal(page(w2).querySelector('[data-partial]'), null, 'a complete list carries no such note');
  const odd = clone(FX.list);
  odd.partial = true;
  const w3 = await tasksWorld({ list: odd });
  tabBtn(w3, 'Epics').click();
  await settle();
  assert.match(text(page(w3).querySelector('[data-partial] .ep-note-n')), /^part of it was not read this time\./);
});

test('a partial Epics list is asked again after about 11 s and is never reused for 30 s; a complete one keeps the 60 s poll and the 30 s reuse (issue #147)', async () => {
  const listCalls = (w) => calls(w).filter((c) => c.path === '/api/epics').length;
  const partial = clone(FX.list);
  partial.partial = true;
  partial.deferred_counts = 1;
  const w = await tasksWorld({ list: partial });
  const timers = fakeTimers(w);
  tabBtn(w, 'Epics').click();
  await settle();
  const live = () => timers().filter((t) => !t.cleared && [11000, 60000].includes(t.ms));
  assert.deepEqual(live().map((t) => t.ms), [11000], 'a partial list polls again in 11 s, not 60');
  const before = listCalls(w);
  await w.run('Epics.fetchList(false)');
  assert.equal(listCalls(w), before + 1, 'a partial answer is not reused for 30 s');
  live().at(-1).fn();
  await settle();
  assert.equal(listCalls(w), before + 2, 'the 11 s timer asks again');
  w.ctx.__list = clone(FX.list);
  await w.run('Epics.fetchList(true)');
  await settle();
  const n = listCalls(w);
  await w.run('Epics.fetchList(false)');
  assert.equal(listCalls(w), n, 'a complete answer is reused for 30 s');
  w.run('Epics.stopList()');
});

test('opening #/tasks?tab=epics directly works even for a board with no sign of epics, and Board brings the task board back and stops the list', async () => {
  const w = await tasksWorld({ issue: false, hash: '#/tasks?tab=epics' });
  assert.ok(tabs(w), 'the address itself shows the switch');
  assert.equal(page(w).querySelectorAll('.ep-lrow').length, 2);
  tabBtn(w, 'Board').click();
  await settle();
  assert.equal(w.location.hash, '#/tasks');
  assert.ok(page(w).querySelector('#tasks-epics').classList.contains('hidden'));
  assert.equal(w.run('Epics.host'), null, 'the list stops polling');
  assert.equal(tabBtn(w, 'Board').getAttribute('aria-pressed'), 'true');
});

test('the paired nodes\' task filter and their tasks give way to the Epics list and come back with the board', async () => {
  const w = await tasksWorld();
  const filter = w.document.createElement('div');
  filter.className = 'nd-tfilter';
  page(w).append(filter);
  tabBtn(w, 'Epics').click();
  await settle();
  assert.ok(filter.classList.contains('ep-hide'));
  tabBtn(w, 'Board').click();
  await settle();
  assert.ok(!filter.classList.contains('ep-hide'));
  assert.match(fs.readFileSync(path.join(STATIC, 'pages', 'epic.css'), 'utf8'), /#page \.ep-hide \{ display:none !important; \}/);
});

test('the Epics list says what to do next: no login, no GitHub remote, no epics yet, a repository that cannot be read, a slow GitHub with the last list kept', async () => {
  const cases = [
    ['nologin', /gh is not logged in on this board: run gh auth login there/, /Run gh auth login on this board, then reload this page\./],
    ['norepo', /No GitHub remote here/, /No project here has a GitHub remote, so there is nowhere to look for epics\./],
    ['none', /No epics yet/, /An epic is an issue labelled ccboard:epic that has sub-issues\. Label one on GitHub and it shows up here\./],
    ['noaccess', /example\/website: this gh login cannot read that repository/, /Check that the gh login on this board can read this repository/],
    ['wait', /GitHub asked us to wait 40 s/, /Showing the last read, from 2 min ago\./],
    ['stale', /gh did not answer in time/, /Showing the last read, from 2 min ago\./],
  ];
  for (const [name, a, b] of cases) {
    const w = await tasksWorld({ hash: '#/tasks?tab=epics', list: { ...clone(FX.list), ...clone(FX.variants[name].list) } });
    const t = text(page(w).querySelector('#tasks-epics'));
    assert.match(t, a, name);
    assert.match(t, b, name);
  }
  const w = await tasksWorld({ hash: '#/tasks?tab=epics', list: { ...clone(FX.list), ...clone(FX.variants.wait.list) } });
  assert.equal(page(w).querySelectorAll('.ep-lrow').length, 2, 'the last list stays while GitHub asks us to wait');
});

test('a board that cannot be reached shows a quiet note in the Epics panel, never a blank panel; Refresh asks again', async () => {
  const w = await tasksWorld({ hash: '#/tasks?tab=epics', list: () => Promise.reject(new Error('503')) });
  assert.match(text(page(w).querySelector('#tasks-epics')), /Could not reach this board\./);
  w.ctx.__list = clone(FX.list);
  btn(page(w), 'Refresh').click();
  await settle();
  assert.equal(page(w).querySelectorAll('.ep-lrow').length, 2);
});

test('a hostile epic title in the list is text', async () => {
  const list = clone(FX.list);
  list.epics[0].title = HOSTILE;
  list.epics[0].url = 'https://evil.example/x';
  const w = await tasksWorld({ hash: '#/tasks?tab=epics', list });
  assert.equal(page(w).querySelectorAll('img, script').length, 0);
  assert.equal(text(page(w).querySelector('.ep-ltitle')), HOSTILE);
  assert.equal(page(w).querySelectorAll('.ep-lrow')[0].querySelector('a.ep-lgh'), null, 'a non-GitHub address is not a link');
});

test('the list is read again every 60 s only while shown (no timer once the tab is left)', async () => {
  const { w } = homeWorld({ state: fakeState({ tasks: [] }) });
  made.push(w);
  w.load('pages/epic.js');
  w.run(LAZY_STUB);
  const timers = fakeTimers(w);
  w.ctx.__list = clone(FX.list);
  w.run("__answers['/api/epics'] = () => __list");
  w.location.hash = '#/tasks?tab=epics';
  await settle();
  const live = () => timers().filter((t) => !t.cleared);
  assert.deepEqual(live().map((t) => t.ms), [60000]);
  live()[0].fn();
  await settle();
  assert.equal(calls(w).filter((c) => c.path === '/api/epics').length, 2);
  w.location.hash = '#/tasks';
  await settle();
  assert.deepEqual(live(), []);
});

// ---------------------------------------------------------------- entry point: the palette

async function paletteWorld() {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('epic.js'.replace('epic.js', 'pages/epic.js'));
  w.load('keymap.js');
  w.load('palette.js');
  w.run(LAZY_STUB);
  w.ctx.__list = clone(FX.list);
  w.run(`__answers['/api/epics'] = () => __list; __answers['/api/skills'] = { agent: 'claude', skills: [] };`);
  w.run('Palette.open()');
  return w;
}
const palType = async (w, q) => { w.run(`Palette.ui.input.value = ${JSON.stringify(q)}; Palette.render()`); await settle(); };
const palRows = (w) => w.document.querySelectorAll('#helpdlg .pal-item').map((n) => text(n.querySelector('.pal-label')));
const palGroups = (w) => w.document.querySelectorAll('#helpdlg .pal-group').map((n) => text(n));

test('the empty palette and an unrelated search have no Epics group (the short list is unchanged)', async () => {
  const w = await paletteWorld();
  assert.ok(!palGroups(w).includes('Epics'));
  for (const q of ['ccb', 'episode', 'e', 'fix']) { await palType(w, q); assert.ok(!palGroups(w).includes('Epics'), q); }
  assert.deepEqual(calls(w).filter((c) => c.path.startsWith('/api/epics')), [], 'nothing was read');
});

test('typing "ep" or "epic" offers the Epics page; Enter opens the Tasks Epics tab', async () => {
  const w = await paletteWorld();
  await palType(w, 'ep');
  assert.ok(palGroups(w).includes('Epics'));
  assert.deepEqual(calls(w).filter((c) => c.path.startsWith('/api/epics')), [], 'two letters read nothing');
  await palType(w, 'epic');
  assert.ok(palRows(w).includes('Epics'));
  w.run('Palette.runSelected({})');
  assert.equal(w.location.hash, '#/tasks?tab=epics');
});

test('"epic" reads the epics once and lists them by title; picking one opens its page', async () => {
  const w = await paletteWorld();
  await palType(w, 'epic');
  await settle();
  assert.deepEqual(calls(w).filter((c) => c.path.startsWith('/api/epics')).map((c) => c.path), ['/api/epics']);
  await palType(w, 'epic');
  await palType(w, 'open epic');
  assert.equal(calls(w).filter((c) => c.path.startsWith('/api/epics')).length, 1, 'asked once while the palette stays open');
  assert.ok(palRows(w).includes('Epic Checkout rework'));
  await palType(w, 'epic checkout');
  assert.deepEqual(palRows(w).filter((r) => r.startsWith('Epic ')), ['Epic Checkout rework']);
  const at = w.document.querySelectorAll('#helpdlg .pal-item').findIndex((n) => text(n.querySelector('.pal-label')) === 'Epic Checkout rework');
  w.run(`Palette.ui.sel = ${at}; Palette.runSelected({})`);
  assert.equal(w.location.hash, HASH);
});

test('"Open epic <address>": owner/repo#n, owner/repo/n and a GitHub issue link open the epic page; anything else offers nothing to open', async () => {
  for (const [q, want] of [['epic example/website#147', HASH], ['open epic example/website/147', HASH], ['epic https://github.com/example/website/issues/147', HASH], ['epic example/foo.js#9', '#/epic/example/foo.js/9']]) {
    const w = await paletteWorld();
    await palType(w, q);
    assert.ok(palRows(w)[0].startsWith('Open epic '), q);
    w.run('Palette.ui.sel = 0; Palette.runSelected({})');
    assert.equal(w.location.hash, want, q);
  }
  const w = await paletteWorld();
  for (const q of ['epic ../x#1', 'epic example/.x#1', 'epic example/website#0', 'epic nonsense', 'epic https://evil.example/example/website/issues/147']) {
    await palType(w, q);
    assert.ok(!palRows(w).some((r) => r.startsWith('Open epic ')), q);
  }
});

// ---------------------------------------------------------------- entry point: the project page

test('the project page has a quiet Epics link that opens the Epics tab', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'project.js'), 'utf8');
  assert.match(src, /class: 'btn minimal small pj-epics', href: '#\/tasks\?tab=epics'/);
  assert.match(src, /el\('span', \{ class: 'spacer' \}\), epics, quad, more\)/);
  const css = fs.readFileSync(path.join(STATIC, 'pages/project.css'), 'utf8');
  assert.match(css, /#page \.pj-epics \{ align-self:center; \}/);
  assert.match(css, /@media \(max-width:599px\) \{[\s\S]*?#page \.pj-epics \{ order:2; \}/);
});

// ---------------------------------------------------------------- the demo

async function demoWorld({ search = '' } = {}) {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.location.search = search;
  w.ctx.__fetches = [];
  w.run(`globalThis.fetch = async (url) => { __fetches.push(url); return __fixture(); };`);
  w.ctx.__fixture = async () => ({ ok: true, status: 200, json: async () => clone(FX) });
  return w;
}
const demoCall = (w, method, p, body) => { w.ctx.__a = [method, p, body]; return w.run('demoApi(...__a)').then(plain); };

test('demo mode routes /api/epics through the epic bundle: the list, one epic, and the unknown epic as an empty one', async () => {
  const w = await demoWorld();
  const l = await demoCall(w, 'GET', '/api/epics');
  assert.deepEqual(l.epics.map((e) => e.title), ['Checkout rework', 'Offline shell polish']);
  const d = await demoCall(w, 'GET', '/api/epics/example/website/147');
  assert.equal(d.children.length, 7);
  assert.deepEqual(d.progress.unclaimed, { open: 2, closed: 0 });
  assert.equal(d.progress.unknown, 1);
  const other = await demoCall(w, 'GET', '/api/epics/example/website/999');
  assert.deepEqual(other.children, []);
  assert.equal(other.epic.url, 'https://github.com/example/website/issues/999');
  assert.deepEqual(w.get('__fetches'), ['/static/demo/epic.json', '/static/demo/epic.json', '/static/demo/epic.json']);
});

test('demo pins and pause are played on the page: the PUT answers the settings shape, a bad handle is a 400, and the next read shows what was set', async () => {
  const w = await demoWorld();
  const put = await demoCall(w, 'PUT', '/api/epics/example/website/147/settings', { pins: { 203: 'node-b', 205: null }, paused: true, epic_pin: 'local' });
  assert.deepEqual(Object.keys(put).sort(), ['auto_pr', 'close', 'epic_pin', 'group', 'max_window_pct', 'max_window_pct_set', 'mcp_run', 'nodes', 'paused', 'pins', 'plan', 'report']);
  assert.deepEqual(Object.keys(put.pins), ['203']);
  assert.equal(put.paused, true);
  assert.equal(put.epic_pin.handle, 'local');
  await assert.rejects(demoCall(w, 'PUT', '/api/epics/example/website/147/settings', { pins: { 204: 'node-z' } }), /not paired/);
  const again = await demoCall(w, 'GET', '/api/epics/example/website/147');
  assert.equal(again.paused, true);
  assert.deepEqual(Object.keys(again.pins), ['203']);
});

test('?epic=<name> picks a degraded state of the demo: no login, no access, not found, wait, stale, labels, empty, no remote, none, down (a 503)', async () => {
  const want = { nologin: 'no_login', noaccess: 'no_access', notfound: 'not_found', wait: 'rate_limited', stale: 'timeout' };
  for (const [flag, reason] of Object.entries(want)) {
    const w = await demoWorld({ search: `?demo=1&epic=${flag}` });
    const d = await demoCall(w, 'GET', '/api/epics/example/website/147');
    assert.equal(d.error.reason, reason, flag);
  }
  assert.equal((await demoCall(await demoWorld({ search: '?epic=labels' }), 'GET', '/api/epics/example/website/147')).source, 'labels');
  assert.equal((await demoCall(await demoWorld({ search: '?epic=empty' }), 'GET', '/api/epics/example/website/147')).empty, true);
  assert.deepEqual((await demoCall(await demoWorld({ search: '?epic=none' }), 'GET', '/api/epics')).epics, []);
  assert.match((await demoCall(await demoWorld({ search: '?epic=norepo' }), 'GET', '/api/epics')).note, /No project here has a GitHub remote/);
  await assert.rejects(demoCall(await demoWorld({ search: '?epic=down' }), 'GET', '/api/epics/example/website/147'), /could not answer/);
});

test('the demo page end to end: the seven-child epic renders with all its cases and the pin menu works', async () => {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.ctx.__fx = clone(FX);
  w.run(`globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(__fx)) });
         api = (method, path, body) => demoEpics(method, path, body);`);
  w.location.hash = HASH;
  await later(400);
  assert.equal(rows(w).length, 7);
  assert.deepEqual(rows(w).map((r) => r.getAttribute('data-state')), ['done', 'running', 'queued', 'failed', 'open', 'open', 'unknown']);
  assert.equal(text(rowOf(w, 207).querySelector('.ep-claim')), '? Claim not known: too many comments to read');
  pinBtn(w, 202).click();
  menuPick(w, 'node-b');
  assert.equal(text(rowOf(w, 202).querySelector('.ep-pintext')), 'pinned: node-b');
  await later(400);
  assert.equal(text(rowOf(w, 202).querySelector('.ep-pintext')), 'pinned: node-b');
  btn(page(w), 'Pause').click();
  await later(400);
  assert.ok(btn(page(w), 'Resume'));
});
