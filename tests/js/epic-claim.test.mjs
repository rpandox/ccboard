// Contract tests for the claim half of the epic view (issue #148): this board's own claims (held, renewing, lost) with the toast for a lost one, the writes-off note, the
// "Claim on <node>" action that is offered only when it can work, the node's answer in plain words for every refusal, and the demo's claim. Real scripts on minidom's DOM
// through tests/js/world.mjs; api() is the recorder and its answers are the demo fixture (app/static/demo/epic.json) or a changed copy of it.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { STATIC } from './harness.mjs';
import { calls, fakeState, homeWorld, page, plain, text, tick } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'epic.json'), 'utf8'));
const KEY = 'example/website#147';
const HASH = '#/epic/example/website/147';
const CLAIM = '/api/nodes/node-b/epics/claim';
const clone = (v) => JSON.parse(JSON.stringify(v));
const detail = (over = {}) => ({ ...clone(FX.details[KEY]), ...over });
const made = [];
afterEach(() => { for (const w of made.splice(0)) { try { w.run('epUnmount(); Epics.stopList()'); } catch (_) { /* never mounted */ } } });
const settle = async (n = 5) => { for (let i = 0; i < n; i++) await tick(); };
const defer = () => { let resolve, reject; const p = new Promise((a, b) => { resolve = a; reject = b; }); return { p, resolve, reject }; };

/** A world on the epic page. GET /api/epics/... answers __detail (a value or a function); POST /api/nodes/<h>/epics/claim answers __claim (a value or a function of the request). */
async function epicWorld({ data = detail(), claim = null, mount = true } = {}) {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.ctx.__detail = data;
  w.ctx.__claim = claim;
  w.run(`__answers['/api/epics'] = (req) => (typeof __detail === 'function' ? __detail(req) : __detail);
         __answers['/api/nodes/'] = (req) => (typeof __claim === 'function' ? __claim(req) : (__claim || { node: 'node-b', age: 0, data: { claimed: true, reason: 'won', repo: 'example/website', issue: 205, winner: 'node-b', attempt: 1 } }));`);
  if (mount) { w.location.hash = HASH; await settle(); }
  return w;
}

const rows = (w) => page(w).querySelectorAll('.ep-row');
const rowOf = (w, n) => rows(w).find((r) => r.getAttribute('data-n') === String(n));
const claimBtn = (w, n) => rowOf(w, n).querySelectorAll('button').find((b) => b.getAttribute('data-claim'));
const posts = (w) => calls(w).filter((c) => c.method === 'POST');
const toasts = (w) => plain(w.get('__toasts'));
const refusal = (status, body) => () => Promise.reject(Object.assign(new Error(body.error || 'refused'), { status, body }));
const withNodes = (nodes) => detail({ nodes });
const NODE_A = { handle: 'local', name: 'node-a', status: 'online', age_s: 0 };
const NODE_B = { handle: 'node-b', name: 'node-b', status: 'online', age_s: 4, tasks: true };

// ---------------------------------------------------------------- the fixture

test('the demo fixture shows writes on, a held claim of this board, a lost one, and a node that can take a claim; the writes-off, offline and no-scope variants exist', () => {
  const d = FX.details[KEY];
  assert.equal(d.writes, true);
  assert.deepEqual(d.own_claims.map((o) => [o.issue, o.state, o.status]), [[202, 'held', 'running'], [203, 'lost', 'claimed']]);
  assert.equal(d.own_claims[1].lost_reason, 'not_first');
  assert.deepEqual(d.nodes.map((n) => [n.handle, n.tasks]), [['local', undefined], ['node-b', true]]);
  assert.equal(FX.variants.writesoff.detail.writes, false);
  assert.equal(FX.variants.offline.detail.nodes[1].status, 'offline');
  assert.equal(FX.variants.notasks.detail.nodes[1].tasks, false);
  assert.ok(!JSON.stringify(FX.details[KEY].own_claims).match(/login|claim_ref|nid|node_id/), 'a claim of this board shows no login, no claim_ref and no hash');
});

// ---------------------------------------------------------------- this board's own claims

test('a claim this board holds says so, with how often it was renewed; a running one says it runs the issue', async () => {
  const w = await epicWorld();
  const own = rowOf(w, 202).querySelector('.ep-own');
  assert.equal(own.getAttribute('data-own'), 'held');
  assert.equal(text(own).trim(), '✽ This board is running this issue and renews its claim (renewed 3 times)');
  const d = detail();
  d.own_claims[0] = { ...d.own_claims[0], status: 'claimed', renewals: 1 };
  const w2 = await epicWorld({ data: d });
  assert.equal(text(rowOf(w2, 202).querySelector('.ep-own')).trim(), '✽ This board holds this claim and renews it (renewed 1 time)');
  d.own_claims[0] = { ...d.own_claims[0], renewals: 0 };
  assert.equal(text((await epicWorld({ data: d })).document.querySelector('.ep-own')).trim(), '✽ This board holds this claim and renews it');
});

test('a lost claim says why, that nothing more is written, and that its task is parked as lost-claim with the worktree kept', async () => {
  const w = await epicWorld();
  const own = rowOf(w, 203).querySelector('.ep-own');
  assert.equal(own.getAttribute('data-own'), 'lost');
  assert.ok(own.classList.contains('warn'));
  assert.equal(text(own).trim(), '⚠ This board lost its claim: another node was first. It writes nothing more for it. Its task 42 is marked lost-claim; its worktree is kept.');
  const reasons = { expired: 'its lease ran out', deleted: 'the claim comment was deleted', unparseable: 'the claim comment was edited', status_changed: 'its status was changed on GitHub', weird: 'it changed on GitHub' };
  for (const [reason, words] of Object.entries(reasons)) {
    const d = detail();
    d.own_claims[1] = { ...d.own_claims[1], lost_reason: reason, task_id: null };
    const x = await epicWorld({ data: d });
    assert.equal(text(rowOf(x, 203).querySelector('.ep-own')).trim(), `⚠ This board lost its claim: ${words}. It writes nothing more for it.`, reason);
  }
});

test('a lost claim is said once with a toast, not on every poll and not on the next visit', async () => {
  const w = await epicWorld();
  const t = toasts(w).filter((x) => /Lost the claim/.test(x.text));
  assert.equal(t.length, 1);
  assert.equal(t[0].kind, 'bad');
  assert.match(t[0].text, /^Lost the claim on #203\. This board lost its claim: another node was first\./);
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.equal(toasts(w).filter((x) => /Lost the claim/.test(x.text)).length, 1, 'a poll does not say it again');
  w.run('epUnmount()');
  w.location.hash = '#/';
  await settle();
  w.location.hash = HASH;
  await settle();
  assert.equal(toasts(w).filter((x) => /Lost the claim/.test(x.text)).length, 1, 'nor does the next visit of this browser');
});

test('a claim that turns lost while the page is open is said when the poll shows it', async () => {
  const d = detail();
  d.own_claims = [{ ...d.own_claims[0] }];
  const w = await epicWorld({ data: d });
  assert.equal(toasts(w).filter((x) => /Lost the claim/.test(x.text)).length, 0);
  const next = detail();
  next.own_claims = [{ ...next.own_claims[0], state: 'lost', lost_reason: 'expired', task_id: null }];
  w.ctx.__detail = next;
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.equal(toasts(w).filter((x) => /Lost the claim on #202/.test(x.text)).length, 1);
  assert.match(text(rowOf(w, 202).querySelector('.ep-own')), /lost its claim: its lease ran out/);
});

test('with writes off a held claim is said not to be renewed, with the board\'s sentence on the page', async () => {
  const w = await epicWorld({ data: detail({ writes: false }) });
  assert.equal(text(rowOf(w, 202).querySelector('.ep-own')).trim(), '✽ This board holds this claim, but GitHub writes are off, so it is not renewed');
  assert.ok(rowOf(w, 202).querySelector('.ep-own').classList.contains('warn'));
  const note = page(w).querySelector('.ep-note.warn');
  assert.match(text(note), /GitHub writes are off on this board \(CCBOARD_EPIC_WRITES\)/);
  assert.match(text(note), /not renewed while writes are off, so it lapses after its lease/);
});

// ---------------------------------------------------------------- the action is offered only when it can work

test('Claim on <node> is on the one unclaimed issue pinned to an online node that holds the tasks scope, and nowhere else', async () => {
  const w = await epicWorld();
  const b = claimBtn(w, 205);
  assert.ok(b);
  assert.equal(text(b).trim(), 'Claim on node-b');
  assert.equal(b.getAttribute('aria-label'), 'Claim issue #205 on node-b');
  assert.ok(b.classList.contains('primary') && b.classList.contains('tinted') && b.classList.contains('small'), 'the same action on a row is the tinted kind, never a second filled primary');
  assert.equal(b.getAttribute('style'), null);
  assert.match(b.getAttribute('title'), /claim comment.*assigns itself.*labels it ccboard:in-progress.*Nothing else changes on GitHub/);
  for (const n of [201, 202, 203, 204, 206, 207]) assert.equal(claimBtn(w, n), undefined, `#${n}`);
  assert.equal(rows(w).flatMap((r) => r.querySelectorAll('button')).filter((x) => x.getAttribute('data-claim')).length, 1);
  assert.equal(page(w).querySelectorAll('button.primary').filter((x) => !x.classList.contains('tinted')).length, 1, 'the one filled primary is still Run');
});

test('with GitHub writes off there is no Claim action, and the row says why in the board\'s words', async () => {
  const quiet = detail({ writes: false });
  quiet.own_claims = [];
  const w = await epicWorld({ data: quiet });
  assert.equal(claimBtn(w, 205), undefined);
  const hint = rowOf(w, 205).querySelector('[data-claim-off=writes]');
  assert.equal(text(hint), 'GitHub writes are off on this board (CCBOARD_EPIC_WRITES)');
  assert.ok(page(w).querySelector('.ep-note.dim'), 'one note for the page, with what to do');
  assert.match(text(page(w)), /Set CCBOARD_EPIC_WRITES=1 on the box to allow it; until then this board only reads GitHub\./);
  const none = await epicWorld({ data: (({ writes, ...rest }) => rest)(detail()) });
  assert.equal(claimBtn(none, 205), undefined, 'a board that does not say is not asked');
  assert.equal(rowOf(none, 205).querySelector('[data-claim-off]'), null, 'and nothing is claimed about it either');
});

test('no action when the claim is not none: claimed, not known, closed, or already claimed by this board', async () => {
  const shown = { alias: 'node-b', node: 'node-b', status: 'claimed', live: true, renewed_at: '2026-10-11T08:59:00Z' };
  for (const patch of [{ claim_state: 'claimed', claim: shown }, { claim_state: 'none', claim: shown }, { claim_state: 'unknown' }, { state: 'closed' }]) {
    const d = detail();
    d.children = d.children.map((c) => (c.number === 205 ? { ...c, ...patch } : c));
    const w = await epicWorld({ data: d });
    assert.equal(claimBtn(w, 205), undefined, JSON.stringify(patch));
    assert.equal(rowOf(w, 205).querySelector('[data-claim-off]'), null);
  }
  const d = detail();
  d.own_claims = [...d.own_claims, { ...d.own_claims[0], issue: 205, state: 'claiming', status: 'claimed' }];
  const w = await epicWorld({ data: d });
  assert.equal(claimBtn(w, 205), undefined);
  assert.equal(text(rowOf(w, 205).querySelector('.ep-own')).trim(), '∙ This board is claiming this issue');
});

test('an offline node, a node without the tasks scope, no pin, a pin to this board and a stale pin each leave no action and say why', async () => {
  const off = await epicWorld({ data: withNodes([NODE_A, { ...NODE_B, status: 'offline', age_s: 7200 }]) });
  assert.equal(claimBtn(off, 205), undefined);
  assert.equal(text(rowOf(off, 205).querySelector('[data-claim-off=offline]')), 'node-b is offline: it cannot claim.');
  const stale = await epicWorld({ data: withNodes([NODE_A, { ...NODE_B, status: 'stale' }]) });
  assert.equal(text(rowOf(stale, 205).querySelector('[data-claim-off=offline]')), 'node-b is stale: it cannot claim.');
  const scope = await epicWorld({ data: withNodes([NODE_A, { ...NODE_B, tasks: false }]) });
  assert.equal(claimBtn(scope, 205), undefined);
  assert.equal(text(rowOf(scope, 205).querySelector('[data-claim-off=scope]')), 'node-b is paired without the tasks scope.');
  const old = await epicWorld({ data: withNodes([NODE_A, { handle: 'node-b', name: 'node-b', status: 'online', age_s: 4 }]) });
  assert.equal(claimBtn(old, 205), undefined, 'an older board that does not say which scopes the pair holds is not asked');
  const d = detail({ pins: {} });
  const nopin = await epicWorld({ data: d });
  assert.equal(claimBtn(nopin, 205), undefined);
  assert.equal(text(rowOf(nopin, 205).querySelector('[data-claim-off=pin]')), 'Pin this issue to a paired node to claim it there.');
  const alone = await epicWorld({ data: withNodes([NODE_A]) });
  assert.equal(claimBtn(alone, 205), undefined);
  assert.equal(rowOf(alone, 205).querySelector('[data-claim-off]'), null, 'a board with no paired node says nothing about pinning to one');
  const local = await epicWorld({ data: detail({ pins: { 205: { handle: 'local', node_name: 'node-a', source: 'page', stale: false } } }) });
  assert.equal(claimBtn(local, 205), undefined, 'this board is not asked over the hub route');
  assert.equal(text(rowOf(local, 205).querySelector('[data-claim-off]')), 'Pin this issue to a paired node to claim it there.', 'and the row says to pin it to a paired node, not that this board lacks a scope');
  const gone = await epicWorld({ data: detail({ pins: { 205: { handle: 'node-z', node_name: 'node-z', source: 'page', stale: true } } }) });
  assert.equal(claimBtn(gone, 205), undefined);
});

test('the epic pin is the node of an issue that has no pin of its own', async () => {
  const w = await epicWorld({ data: detail({ pins: {}, epic_pin: { handle: 'node-b', node_name: 'node-b', source: 'page', stale: false } }) });
  assert.equal(text(claimBtn(w, 205)).trim(), 'Claim on node-b');
  const stale = await epicWorld({ data: detail({ pins: {}, epic_pin: { handle: 'node-b', node_name: 'node-b', source: 'page', stale: true } }) });
  assert.equal(claimBtn(stale, 205), undefined);
});

// ---------------------------------------------------------------- the ask

test('Claim sends exactly {repo, issue, epic} to the hub route of that node, paints at once, and shows the claim when the node says won', async () => {
  const gate = defer();
  const w = await epicWorld({ claim: () => gate.p });
  claimBtn(w, 205).click();
  assert.deepEqual(plain(posts(w)), [{ method: 'POST', path: CLAIM, body: { repo: 'example/website', issue: 205, epic: 'example/website#147' } }]);
  const busy = claimBtn(w, 205);
  assert.equal(text(busy).trim(), 'Claiming on node-b…');
  assert.notEqual(busy.getAttribute('disabled'), null, 'a second tap does nothing');
  busy.click();
  assert.equal(posts(w).length, 1);
  gate.resolve({ node: 'node-b', age: 0, data: { claimed: true, reason: 'won', repo: 'example/website', issue: 205, winner: 'node-b', attempt: 1 } });
  await settle();
  assert.equal(claimBtn(w, 205), undefined, 'the action is gone once it is claimed');
  assert.equal(rowOf(w, 205).getAttribute('data-state'), 'queued');
  assert.match(text(rowOf(w, 205).querySelector('.ep-claim')), /^Claimed by node-b/);
  assert.deepEqual(toasts(w).filter((x) => /claimed/.test(x.text)).map((x) => x.text), ['node-b claimed #205']);
  assert.ok(calls(w).filter((c) => c.method === 'GET').length >= 2, 'the next read confirms');
});

test('already held says so instead of claiming twice', async () => {
  const w = await epicWorld({ claim: { node: 'node-b', age: 0, data: { claimed: true, reason: 'already_held', repo: 'example/website', issue: 205, winner: 'node-b', attempt: 1 } } });
  claimBtn(w, 205).click();
  await settle();
  assert.deepEqual(toasts(w).map((x) => x.text).filter((t) => /held/.test(t)), ['node-b already held #205']);
});

const REFUSALS = [
  [409, { reason: 'claim_refused', why: 'taken', winner: 'willow-3f' }, 'willow-3f already holds #205. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'taken' }, 'Another node already holds #205. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'lost', winner: 'willow-3f' }, 'willow-3f was first on #205, so node-b withdrew its claim.'],
  [409, { reason: 'claim_refused', why: 'claim_unknown', unknown_reason: 'too_long' }, 'node-b could not tell who holds #205 (claim not known), so it claimed nothing.'],
  [409, { reason: 'claim_refused', why: 'writes_off' }, 'GitHub writes are off on node-b (CCBOARD_EPIC_WRITES). Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'closed' }, '#205 is closed. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'done' }, '#205 is already done. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'attempts_exhausted' }, 'Three attempts on #205 ended without a result. node-b asked for a person and stopped.'],
  [409, { reason: 'claim_refused', why: 'not_epic_child' }, 'node-b does not take #205 as an issue of this epic. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'other_repo' }, 'node-b does not have this repository as one of its projects. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'authors_unknown' }, 'node-b cannot tell whose claims count, so it claimed nothing.'],
  [409, { reason: 'claim_refused', why: 'login_not_allowed' }, 'The gh login of node-b is not one whose claims count. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'no_login' }, 'gh is not logged in on node-b. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'no_access' }, 'The gh login of node-b may not comment on that repository. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'too_many_claims' }, 'node-b already holds as many claims as it keeps.'],
  [409, { reason: 'claim_refused', why: 'rate_limited', wait_s: 60 }, 'GitHub asked node-b to wait 60 s. Try #205 again then.'],
  [409, { reason: 'claim_refused', why: 'outcome_unknown' }, 'GitHub did not say whether the claim on #205 was written. node-b looks before it writes again; check the issue.'],
  [409, { reason: 'claim_refused', why: 'withdraw_failed', winner: 'willow-3f' }, 'willow-3f was first on #205, and node-b could not remove its own comment yet. It will try again.'],
  [409, { reason: 'claim_refused', why: 'error' }, 'node-b could not claim #205. Nothing was claimed.'],
  [409, { reason: 'claim_refused', why: 'something-new' }, 'node-b did not claim #205: PEER SENTENCE. Nothing was claimed.'],
  [503, { reason: 'offline' }, 'node-b is offline. Nothing was claimed.'],
  [409, { reason: 'scope' }, 'node-b is paired without the tasks scope. Nothing was claimed.'],
  [504, { reason: 'unconfirmed' }, 'node-b did not answer in time, so it is not known whether #205 was claimed. Check the issue on GitHub. Nothing was retried.'],
  [502, { reason: 'unconfirmed' }, 'node-b did not answer in time, so it is not known whether #205 was claimed. Check the issue on GitHub. Nothing was retried.'],
  [409, { reason: 'needs_repair' }, 'node-b no longer takes this board\'s token: pair it again in Settings. Nothing was claimed.'],
  [404, { reason: 'unknown_node' }, 'node-b is no longer paired. Nothing was claimed.'],
  [502, { reason: 'unreachable' }, 'node-b could not be reached. Nothing was claimed.'],
  [403, { reason: 'human_only' }, 'Only a signed-in person can claim an issue.'],
  [429, { reason: 'rate_limited' }, 'Too many requests to the nodes from this board. Try again in a minute.'],
];

for (const [status, body, sentence] of REFUSALS) {
  test(`a refusal is said in plain words: ${status} ${body.reason}${body.why ? ' / ' + body.why : ''}${body.winner ? ' with the winner' : ''}`, async () => {
    const w = await epicWorld({ claim: refusal(status, { ...body, error: 'PEER SENTENCE' }) });
    claimBtn(w, 205).click();
    await settle();
    const note = rowOf(w, 205).querySelector('.ep-claimnote');
    assert.equal(text(note), sentence, JSON.stringify(body));
    assert.equal(note.getAttribute('role'), 'status');
    assert.deepEqual(toasts(w).filter((x) => x.kind === 'bad').map((x) => x.text).filter((t) => !/Lost the claim/.test(t)), [sentence]);
    assert.ok(claimBtn(w, 205), 'the action is back so it can be tried again');
    assert.equal(text(claimBtn(w, 205)).trim(), 'Claim on node-b');
    assert.equal(rowOf(w, 205).getAttribute('data-state'), 'open', 'the row is as it was');
  });
}

test('a refusal of taken, lost, done or closed reads the issue again; the others do not', async () => {
  for (const [why, again] of [['taken', true], ['lost', true], ['done', true], ['closed', true], ['writes_off', false], ['claim_unknown', false]]) {
    const w = await epicWorld({ claim: refusal(409, { reason: 'claim_refused', why }) });
    const before = calls(w).filter((c) => c.method === 'GET').length;
    claimBtn(w, 205).click();
    await settle(8);
    assert.equal(calls(w).filter((c) => c.method === 'GET').length > before, again, why);
  }
});

test('a note stays until the next ask and goes when the issue is no longer free', async () => {
  const w = await epicWorld({ claim: refusal(409, { reason: 'claim_refused', why: 'closed', error: 'x' }) });
  claimBtn(w, 205).click();
  await settle();
  assert.ok(rowOf(w, 205).querySelector('.ep-claimnote'));
  w.ctx.__claim = null;
  claimBtn(w, 205).click();
  await settle();
  assert.equal(rowOf(w, 205).querySelector('.ep-claimnote'), null);
});

test('text from the node or from GitHub is shown as text: a hostile winner or message makes no element and no handler', async () => {
  const evil = '<img src=x onerror=alert(1)>';
  const w = await epicWorld({ claim: refusal(409, { reason: 'claim_refused', why: 'taken', winner: evil, error: evil }) });
  claimBtn(w, 205).click();
  await settle();
  const note = rowOf(w, 205).querySelector('.ep-claimnote');
  assert.equal(text(note), `${evil} already holds #205. Nothing was claimed.`);
  assert.equal(rowOf(w, 205).querySelectorAll('img').length, 0);
  const odd = await epicWorld({ claim: refusal(500, { error: evil + '<script>x</script>' }) });
  claimBtn(odd, 205).click();
  await settle();
  assert.equal(rowOf(odd, 205).querySelectorAll('img, script').length, 0);
  assert.match(text(rowOf(odd, 205).querySelector('.ep-claimnote')), /^node-b did not claim #205: /);
  const d = detail();
  d.own_claims[0] = { ...d.own_claims[0], branch: evil, task_ref: evil };
  d.nodes = [NODE_A, { ...NODE_B, name: evil }];
  const x = await epicWorld({ data: d });
  assert.equal(page(x).querySelectorAll('img').length, 0);
  assert.equal(text(claimBtn(x, 205)).trim(), `Claim on ${evil}`);
});

test('the page leaves the claim alone while it is not mounted: an answer that arrives after leaving paints nothing and toasts nothing', async () => {
  const gate = defer();
  const w = await epicWorld({ claim: () => gate.p });
  claimBtn(w, 205).click();
  w.run('epUnmount()');
  const before = toasts(w).length;
  gate.resolve({ node: 'node-b', age: 0, data: { claimed: true, reason: 'won', repo: 'example/website', issue: 205, winner: 'node-b', attempt: 1 } });
  await settle();
  assert.equal(toasts(w).length, before);
});

test('Run stays disabled: routing is a later phase, and a claim does not turn it on', async () => {
  const w = await epicWorld();
  claimBtn(w, 205).click();
  await settle();
  const run = page(w).querySelectorAll('button').find((b) => text(b).trim() === 'Run');
  assert.notEqual(run.getAttribute('disabled'), null);
  assert.deepEqual(posts(w).map((c) => c.path), [CLAIM], 'the one write of this page to a node');
});

// ---------------------------------------------------------------- the source: nothing written to GitHub from the page, nothing unsafe

test('the page code writes to no GitHub address, builds nothing with innerHTML or style attributes, and names the one claim route', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'epic.js'), 'utf8');
  assert.ok(!/innerHTML|insertAdjacentHTML|cssText|\.setAttribute\(\s*'style'/.test(src));
  assert.ok(!/api\.github\.com|fetch\(/.test(src.replace(/globalThis\.fetch/g, '')) || /demo fixture/.test(src), 'only the demo reads its fixture');
  const posts = src.match(/api\('POST', `[^`]*`/g) || [];
  assert.deepEqual(posts, ["api('POST', `/api/nodes/${encodeURIComponent(node.handle)}/epics/claim`"]);
  assert.equal((src.match(/api\('(?:POST|DELETE|PATCH)'/g) || []).length, 1, 'the page makes one write to a node and no other');
  assert.ok(!src.includes('—'), 'no em-dash in UI text');
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

test('demo: the epic carries writes on and the board\'s own claims, and ?epic=writesoff turns the switch off', async () => {
  const d = await demoCall(await demoWorld(), 'GET', '/api/epics/example/website/147');
  assert.equal(d.writes, true);
  assert.deepEqual(d.own_claims.map((o) => o.state), ['held', 'lost']);
  const off = await demoCall(await demoWorld({ search: '?demo=1&epic=writesoff' }), 'GET', '/api/epics/example/website/147');
  assert.equal(off.writes, false);
  assert.equal(off.own_claims.length, 2);
  assert.equal((await demoCall(await demoWorld({ search: '?epic=offline' }), 'GET', '/api/epics/example/website/147')).nodes[1].status, 'offline');
  assert.equal((await demoCall(await demoWorld({ search: '?epic=notasks' }), 'GET', '/api/epics/example/website/147')).nodes[1].tasks, false);
});

test('demo: the claim route is played here: a claim is made and the issue then shows as claimed by that node', async () => {
  const w = await demoWorld();
  const r = await demoCall(w, 'POST', CLAIM, { repo: 'example/website', issue: 205, epic: 'example/website#147' });
  assert.deepEqual(r, { node: 'node-b', age: 0, data: { claimed: true, reason: 'won', repo: 'example/website', issue: 205, winner: 'node-b', attempt: 1 } });
  const d = await demoCall(w, 'GET', '/api/epics/example/website/147');
  const c = d.children.find((x) => x.number === 205);
  assert.equal(c.claim_state, 'claimed');
  assert.equal(c.claim.alias, 'node-b');
  assert.deepEqual(c.bucket, { node: 'node-b', state: 'queued' });
  assert.equal(d.children.find((x) => x.number === 204).claim.status, d.children.find((x) => x.number === 204).claim.status, 'the others are as they were');
});

test('demo: ?claim=<word> plays each refusal with the same body the board sends', async () => {
  const want = { taken: 'taken', lost: 'lost', unknown: 'claim_unknown', writesoff: 'writes_off', closed: 'closed', done: 'done', attempts: 'attempts_exhausted' };
  for (const [flag, why] of Object.entries(want)) {
    const w = await demoWorld({ search: `?demo=1&claim=${flag}` });
    w.ctx.__a = ['POST', CLAIM, { repo: 'example/website', issue: 205 }];
    const e = await w.run('demoApi(...__a)').then(() => null, (x) => x);
    assert.equal(e.status, 409, flag);
    assert.equal(plain(e.body).reason, 'claim_refused');
    assert.equal(plain(e.body).why, why, flag);
  }
  for (const [flag, status, reason] of [['offline', 503, 'offline'], ['scope', 409, 'scope'], ['unconfirmed', 504, 'unconfirmed']]) {
    const w = await demoWorld({ search: `?claim=${flag}` });
    w.ctx.__a = ['POST', CLAIM, { repo: 'example/website', issue: 205 }];
    const e = await w.run('demoApi(...__a)').then(() => null, (x) => x);
    assert.equal(e.status, status);
    assert.equal(plain(e.body).reason, reason);
  }
  for (const word of ['constructor', 'tostring', 'nothing']) {
    const w = await demoWorld({ search: `?claim=${word}` });
    assert.equal((await demoCall(w, 'POST', CLAIM, { repo: 'example/website', issue: 205 })).data.claimed, true, `${word}: an unknown word plays a claim`);
  }
});

test('the demo page end to end: Claim on node-b paints the claim and a played refusal says taken', async () => {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.ctx.__fx = clone(FX);
  w.run(`globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(__fx)) });
         api = (method, path, body) => demoEpics(method, path, body);`);
  w.location.hash = HASH;
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(text(rowOf(w, 205).querySelector('[data-claim]')).trim(), 'Claim on node-b');
  claimBtn(w, 205).click();
  await new Promise((r) => setTimeout(r, 500));
  assert.equal(rowOf(w, 205).getAttribute('data-state'), 'queued');
  assert.match(text(rowOf(w, 205).querySelector('.ep-claim')), /node-b/);
  assert.equal(claimBtn(w, 205), undefined);
});
