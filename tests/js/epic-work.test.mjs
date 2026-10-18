// Contract tests for the work half of the epic view (issue #149): the Work action of a row (one tinted action, optimistic, refused in plain words), the phase of the work with Open on
// <node>, the mark on issues by an author who is not allowed, the per-epic settings that start off (report, auto_pr, close) with the paragraph that says what will be posted and pushed,
// the task card's claim link and lost-claim state, and the demo of all of it. Real scripts on minidom's DOM through tests/js/world.mjs; api() is the recorder and its answers are the
// demo fixture (app/static/demo/epic.json) or a changed copy of it.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { afterEach, test } from 'node:test';
import { STATIC } from './harness.mjs';
import { calls, fakeState, homeWorld, page, plain, text, tick } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'epic.json'), 'utf8'));
const TASK_FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'task-epic.json'), 'utf8'));
const STATE_FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8'));
const KEY = 'example/website#147';
const HASH = '#/epic/example/website/147';
const WORK = '/api/epics/example/website/147/issues/205/work';          // the board checks the routing filters, then asks the node (issue #150)
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

/** A world on the epic page. GET answers __detail; POST .../issues/<n>/work answers __work (a value or a function of the request); PUT .../settings answers __put (default: the request merged). */
async function epicWorld({ data = detail(), work = null, put = null } = {}) {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.ctx.__detail = data;
  w.ctx.__work = work;
  w.ctx.__put = put;
  w.run(`__answers['/api/epics'] = (req) => {
    if (req.method === 'PUT') return typeof __put === 'function' ? __put(req) : (__put || Object.assign(__detail, req.body || {}));          // the board keeps what it was sent
    return JSON.parse(JSON.stringify(typeof __detail === 'function' ? __detail(req) : __detail));          // a fresh object per poll, as the wire gives
  };
  const WORKED = { node: 'node-b', age: 0, data: { claimed: true, reason: 'won', task_ref: 'node-b:51', id: 51, branch: 'worktree-issue-205', tmux: 'x', repo: 'example/website', issue: 205 } };
  __answers['/api/epics'] = ((was) => (req) => (req.method === 'POST' && /\\/issues\\/[0-9]+\\/work$/.test(req.path) ? (typeof __work === 'function' ? __work(req) : (__work || WORKED)) : was(req)))(__answers['/api/epics']);`);
  w.location.hash = HASH;
  await settle();
  return w;
}

const rows = (w) => page(w).querySelectorAll('.ep-row');
const rowOf = (w, n) => rows(w).find((r) => r.getAttribute('data-n') === String(n));
const workBtn = (w, n) => rowOf(w, n).querySelectorAll('button').find((b) => b.getAttribute('data-work') || b.getAttribute('data-work-off'));
const claimBtn = (w, n) => rowOf(w, n).querySelectorAll('button').find((b) => b.getAttribute('data-claim'));
const posts = (w) => calls(w).filter((c) => c.method === 'POST');
const puts = (w) => calls(w).filter((c) => c.method === 'PUT');
const toasts = (w) => plain(w.get('__toasts'));
const refusal = (status, body) => () => Promise.reject(Object.assign(new Error(body.error || 'refused'), { status, body }));
const cfg = (w) => page(w).querySelector('.ep-cfg');
const seg = (w, v) => cfg(w).querySelectorAll('.seg-btn').find((b) => b.getAttribute('data-report') === v);
const box = (w, k) => cfg(w).querySelectorAll('input').find((i) => i.getAttribute('data-flag') === k);

// ---------------------------------------------------------------- the fixture

test('the demo fixture carries the work shapes: authors, author and author_ok per child, work records, report as a word, close, plan; and the variants for each phase and refusal of the author check', () => {
  const d = FX.details[KEY];
  assert.deepEqual(d.authors, { allowed: ['maya-r'], epic: 'maya-r', epic_ok: true, blocked: [], text: null });
  assert.equal(d.report, 'phase');
  assert.equal(d.close, false);
  assert.equal(d.auto_pr, false);
  assert.deepEqual(Object.keys(d.plan), ['report', 'auto_pr', 'close']);
  assert.ok(d.children.every((c) => c.author === 'maya-r' && c.author_ok === true && 'work' in c && typeof c.phase === 'string'));
  assert.deepEqual(d.children.filter((c) => c.work).map((c) => [c.number, c.work.phase]), [[201, 'done'], [202, 'pr'], [203, 'lost']]);
  for (const c of d.children.filter((x) => x.work)) assert.equal(c.work.claim_url.startsWith('https://github.com/example/website/issues/'), true);
  assert.deepEqual(['workrun', 'workwait', 'workfail', 'workcancel'].map((n) => Object.values(FX.variants[n].child)[0].work.phase), ['running', 'waiting', 'failed', 'cancelled']);
  assert.equal(FX.variants.authors.child['205'].author_ok, false);
  assert.equal(FX.variants.epicauthor.detail.authors.epic_ok, false);
  for (const [k, v] of Object.entries(FX.details)) assert.ok(['phase', 'summary', 'none'].includes(v.report), k);
  const all = JSON.stringify(FX);
  assert.ok(!/claim_ref|\/Users\/|\/home\/|ghp_|github_pat_|@/.test(all.replace(/@@/g, '')), 'no login, path, token or address in the fixture');
});

// ---------------------------------------------------------------- the Work action

test('Work on <node> is the one tinted action of the issue that can be worked; Claim beside it is bordered; the page keeps one filled primary', async () => {
  const w = await epicWorld();
  const b = workBtn(w, 205);
  assert.equal(text(b).trim(), 'Work on node-b');
  assert.equal(b.getAttribute('data-work'), 'node-b');
  assert.equal(b.getAttribute('aria-label'), 'Work on issue #205 on node-b');
  assert.ok(b.classList.contains('primary') && b.classList.contains('tinted') && b.classList.contains('small'));
  assert.equal(b.getAttribute('style'), null);
  assert.match(b.getAttribute('title'), /claims the issue.*own worktree.*model line.*Nothing is pushed unless pull requests are on/);
  assert.ok(!claimBtn(w, 205).classList.contains('primary'));
  assert.equal(rowOf(w, 205).querySelectorAll('button.tinted').length, 1, 'one tinted action in the row');
  for (const r of rows(w)) assert.ok(r.querySelectorAll('button.tinted').length <= 1, `row ${r.getAttribute('data-n')}`);
  assert.equal(page(w).querySelectorAll('button.primary').filter((x) => !x.classList.contains('tinted')).length, 1, 'the one filled primary is still Run');
  for (const n of [201, 202, 203, 206, 207]) assert.equal(workBtn(w, n), undefined, `#${n}`);
});

test('an issue whose last attempt ended offers Try again on the node that held it', async () => {
  const w = await epicWorld();
  const b = workBtn(w, 204);
  assert.equal(text(b).trim(), 'Try again on node-b');
  assert.equal(b.getAttribute('data-work'), 'node-b');
});

test('an issue claimed by a paired node that has not started it offers Work on that node', async () => {
  const d = detail();
  const c = d.children.find((x) => x.number === 206);
  c.claim = { alias: 'node-b', node: 'node-b', status: 'claimed', since: d.server_now, renewed_at: d.server_now, live: true, attempt: 1, seq: 1, task_ref: null, branch: null };
  c.task = null;
  const w = await epicWorld({ data: d });
  assert.equal(text(workBtn(w, 206)).trim(), 'Work on node-b');
});

test('Work asks the board, with the node and the repository only: the settings are the hub\'s, never sent by the page', async () => {
  const w = await epicWorld();
  workBtn(w, 205).click();
  await settle();
  assert.deepEqual(posts(w).map((c) => [c.path, c.body]), [[WORK, { node: 'node-b', repo: 'example/website' }]]);
  assert.ok(!/report|auto_pr|close|force|prompt/.test(JSON.stringify(posts(w)[0].body)));
});

test('Work paints at once: the row is held by the node and queued while the node answers, then running with Open on <node>; the next read only confirms', async () => {
  const gate = defer();
  const w = await epicWorld({ work: () => gate.p });
  workBtn(w, 205).click();
  await settle();
  const row = rowOf(w, 205);
  assert.equal(row.getAttribute('data-state'), 'queued', 'queued before the answer');
  assert.equal(text(row.querySelector('[data-work]')).trim(), 'Starting on node-b…');
  assert.notEqual(row.querySelector('[data-work]').getAttribute('disabled'), null, 'a second tap cannot start it twice');
  assert.match(text(row.querySelector('.ep-task')), /Task queued on node-b/);
  gate.resolve({ node: 'node-b', age: 0, data: { claimed: true, reason: 'won', task_ref: 'node-b:51', id: 51, branch: 'worktree-issue-205', tmux: 'x', repo: 'example/website', issue: 205 } });
  await settle();
  const after = rowOf(w, 205);
  assert.equal(after.getAttribute('data-state'), 'running');
  assert.match(text(after.querySelector('.ep-task')), /Task running on node-b/);
  const open = after.querySelector('a[data-open-task]');
  assert.equal(open.getAttribute('href'), '#/n/node-b/t/51');
  assert.equal(text(open).trim(), 'Open on node-b');
  assert.ok(toasts(w).some((t) => /node-b started #205/.test(t.text || t.msg || JSON.stringify(t))));
  assert.equal(posts(w).length, 1);
});

test('a poll that lands while the node is still starting keeps the row held, and a refusal then puts the row back as the poll read it', async () => {
  const gate = defer();
  const w = await epicWorld({ work: () => gate.p });
  workBtn(w, 205).click();
  await settle();
  await w.run('epFetch(epicPage.cur)');
  await settle();
  assert.equal(rowOf(w, 205).getAttribute('data-state'), 'queued', 'the poll answered Unclaimed; the ask is painted over it');
  gate.reject(Object.assign(new Error('refused'), { status: 409, body: { reason: 'work_refused', why: 'taken', winner: 'node-c', error: 'x' } }));
  await settle();
  assert.equal(rowOf(w, 205).getAttribute('data-state'), 'open');
  assert.equal(text(workBtn(w, 205)).trim(), 'Work on node-b');
});

const REFUSALS = [
  ['repo_not_here', 404, { reason: 'repo_not_here', why: 'repo_not_here' }, /node-b does not have this repository cloned, so #205 was not claimed\./],
  ['author_not_allowed', 409, { reason: 'work_refused', why: 'author_not_allowed', author: 'dana-w' }, /#205 was written by dana-w, who is not on the allowed list\. Nothing was claimed\./],
  ['epic_author_not_allowed', 409, { reason: 'work_refused', why: 'epic_author_not_allowed', author: 'dana-w' }, /The epic was written by dana-w, who is not on the allowed list\. Nothing in it will run\./],
  ['unreadable', 409, { reason: 'work_refused', why: 'unreadable' }, /could not read #205 or its epic from GitHub just now/],
  ['bypass_refused', 409, { reason: 'work_refused', why: 'bypass_refused' }, /#205 asks for bypass permissions\. node-b refused to run it, said so on the issue and released the claim\./],
  ['posture_refused', 409, { reason: 'work_refused', why: 'posture_refused' }, /asks for a permission setting that node-b does not use for work from an issue/],
  ['lanes_full', 409, { reason: 'work_refused', why: 'lanes_full' }, /already runs as many tasks as its lane cap advises\. Nothing was claimed\. You can start #205 anyway\./],
  ['launch_failed', 409, { reason: 'work_refused', why: 'launch_failed' }, /could not start the task for #205 and released the claim\./],
  ['taken', 409, { reason: 'work_refused', why: 'taken', winner: 'node-c' }, /node-c already holds #205\. Nothing was claimed\./],
  ['lost', 409, { reason: 'work_refused', why: 'lost', winner: 'node-c' }, /node-c was first on #205, so node-b withdrew its claim\./],
  ['claim_unknown', 409, { reason: 'work_refused', why: 'claim_unknown' }, /could not tell who holds #205 \(claim not known\)/],
  ['writes_off', 409, { reason: 'work_refused', why: 'writes_off' }, /GitHub writes are off on node-b/],
  ['closed', 409, { reason: 'work_refused', why: 'closed' }, /#205 is closed\. Nothing was claimed\./],
  ['done', 409, { reason: 'work_refused', why: 'done' }, /#205 is already done/],
  ['attempts_exhausted', 409, { reason: 'work_refused', why: 'attempts_exhausted' }, /Three attempts on #205 ended without a result\. node-b asked for a person and stopped\./],
  ['too_many_claims', 409, { reason: 'work_refused', why: 'too_many_claims' }, /already holds as many claims as it keeps/],
  ['rate_limited', 409, { reason: 'work_refused', why: 'rate_limited', wait_s: 40 }, /GitHub asked node-b to wait 40 s/],
  ['busy', 409, { reason: 'work_refused', why: 'busy' }, /busy with other claims right now/],
  ['no_access', 409, { reason: 'work_refused', why: 'no_access' }, /cannot push to that repository/],
  ['no_login', 409, { reason: 'work_refused', why: 'no_login' }, /gh is not logged in on node-b/],
  ['login_not_allowed', 409, { reason: 'work_refused', why: 'login_not_allowed' }, /not one whose claims count/],
  ['authors_unknown', 409, { reason: 'work_refused', why: 'authors_unknown' }, /cannot tell whose claims count/],
  ['not_epic_child', 409, { reason: 'work_refused', why: 'not_epic_child' }, /does not take #205 as an issue of this epic/],
  ['other_repo', 409, { reason: 'work_refused', why: 'other_repo' }, /does not have this repository as one of its projects/],
  ['outcome_unknown', 409, { reason: 'work_refused', why: 'outcome_unknown' }, /did not say whether the claim on #205 was written/],
  ['withdraw_failed', 409, { reason: 'work_refused', why: 'withdraw_failed', winner: 'node-c' }, /could not remove its own comment yet/],
  ['bad_request', 409, { reason: 'work_refused', why: 'bad_request' }, /could not claim #205/],
  ['error', 409, { reason: 'work_refused', why: 'error' }, /could not claim #205/],
  ['offline', 503, { reason: 'offline' }, /node-b is offline\. Nothing was claimed\./],
  ['scope', 409, { reason: 'scope' }, /paired without the tasks scope/],
  ['unconfirmed', 504, { reason: 'unconfirmed' }, /Outcome unknown, check node-b: it did not answer in time, so it is not known whether it started #205\. Look at its tasks and the issue on GitHub\. Nothing was retried\./],
];

test('every refusal word of the work route is said in plain words, the row goes back as it was, and nothing is retried', async () => {
  assert.ok(REFUSALS.length >= 30);
  for (const [name, status, body, want] of REFUSALS) {
    const w = await epicWorld({ work: refusal(status, { ...body, error: 'played' }) });
    workBtn(w, 205).click();
    await settle();
    const note = rowOf(w, 205).querySelector('[data-work-note]');
    assert.ok(note, name);
    assert.match(text(note), want, name);
    assert.equal(rowOf(w, 205).getAttribute('data-state'), 'open', `${name}: the row is back as it was`);
    assert.equal(text(workBtn(w, 205)).trim(), 'Work on node-b', name);
    assert.ok(toasts(w).length >= 1, name);
    assert.equal(posts(w).length, 1, `${name}: not retried`);
    assert.ok(!text(note).includes('—'), `${name}: no em-dash`);
  }
});

test('a full lane cap offers Start anyway, which sends force and nothing else new', async () => {
  let n = 0;
  const w = await epicWorld({ work: (req) => (++n === 1 ? Promise.reject(Object.assign(new Error('x'), { status: 409, body: { reason: 'work_refused', why: 'lanes_full', error: 'x' } }))
    : { node: 'node-b', age: 0, data: { claimed: true, reason: 'won', task_ref: 'node-b:52', id: 52, branch: 'worktree-issue-205', tmux: 'x', repo: 'example/website', issue: 205 } }) });
  workBtn(w, 205).click();
  await settle();
  const force = rowOf(w, 205).querySelectorAll('button').find((b) => b.getAttribute('data-work-force'));
  assert.equal(text(force).trim(), 'Start anyway');
  assert.ok(!force.classList.contains('primary'), 'still one tinted action in the row');
  force.click();
  await settle();
  assert.deepEqual(posts(w).map((c) => c.body), [{ node: 'node-b', repo: 'example/website' }, { node: 'node-b', repo: 'example/website', force: true }]);
  assert.equal(rowOf(w, 205).getAttribute('data-state'), 'running');
  assert.equal(rowOf(w, 205).querySelector('[data-work-note]'), null);
});

// ---------------------------------------------------------------- the routing filters, said before Work (issue #150)

const filtered = (code, error, extra = {}) => ({ ...detail(), children: detail().children.map((c) => (c.number === 205 ? { ...c, work_refused: { 'node-b': { code, text: error, until: null } }, ...extra } : c)) });
const STARTED = { node: 'node-b', age: 0, data: { claimed: true, reason: 'won', task_ref: 'node-b:52', id: 52, branch: 'branch-205', tmux: 'x', repo: 'example/website', issue: 205 } };

test('a node that fails a routing filter for the issue disables Work with the board\'s own words, and nothing is sent', async () => {
  const text0 = 'node-b 5-hour window is at 90 percent until 14:10 UTC';
  const w = await epicWorld({ data: filtered('window', text0) });
  const b = workBtn(w, 205);
  assert.equal(b.getAttribute('data-work-off'), 'filter');
  assert.equal(b.getAttribute('disabled') !== null, true);
  assert.equal(b.getAttribute('title'), text0);
  assert.match(b.getAttribute('aria-label'), /window is at 90 percent/);
  assert.equal(text(rowOf(w, 205).querySelector('[data-work-hint=filter]')).trim(), text0, 'the reason is on the row, not only in a tooltip');
  b.click();
  await settle();
  assert.deepEqual(posts(w), []);
  assert.equal(workBtn(w, 204).getAttribute('data-work'), 'node-b', 'an issue the node passes is still offered');
});

test('every filter code disables Work; a full lane count does not (Start anyway is the override), and an answer without the field changes nothing', async () => {
  for (const code of ['online', 'offline', 'stale', 'incompatible', 'unauthorized', 'scope', 'repo', 'repo_unknown', 'agent_unknown', 'agent_missing', 'agent_login', 'backoff', 'window', 'lane_unknown', 'refused']) {
    const w = await epicWorld({ data: filtered(code, `node-b fails ${code}`) });
    assert.equal(workBtn(w, 205).getAttribute('data-work-off'), 'filter', code);
  }
  const lane = await epicWorld({ data: filtered('lane', 'node-b has no free lane') });
  assert.equal(workBtn(lane, 205).getAttribute('data-work'), 'node-b', 'a full lane cap is the person\'s to override');
  const none = await epicWorld({ data: detail() });
  assert.equal(workBtn(none, 205).getAttribute('data-work'), 'node-b');
});

test('a filter the board finds after the page read is shown in the board\'s words, with no claim', async () => {
  const w = await epicWorld({ work: refusal(409, { error: 'node-b has not reported its task lanes, so a free lane is not known', reason: 'node_filtered', filter: 'lane_unknown', node: 'node-b' }) });
  workBtn(w, 205).click();
  await settle();
  const note = rowOf(w, 205).querySelector('[data-work-note]');
  assert.equal(text(note).trim(), 'node-b has not reported its task lanes, so a free lane is not known. Nothing was claimed.');
  assert.equal(rowOf(w, 205).querySelectorAll('button').find((b) => b.getAttribute('data-work-force')), undefined, 'a lane count that is not known has no Start anyway');
  assert.equal(posts(w).length, 1);
});

test('a full lane count found by the board offers Start anyway, which sends force', async () => {
  let n = 0;
  const w = await epicWorld({ work: () => (++n === 1 ? Promise.reject(Object.assign(new Error('x'), { status: 409, body: { reason: 'node_filtered', filter: 'lane', error: 'node-b has no free lane' } })) : STARTED) });
  workBtn(w, 205).click();
  await settle();
  assert.match(text(rowOf(w, 205).querySelector('[data-work-note]')), /node-b has no free lane\. You can start #205 anyway\./);
  rowOf(w, 205).querySelectorAll('button').find((b) => b.getAttribute('data-work-force')).click();
  await settle();
  assert.deepEqual(posts(w).map((c) => c.body), [{ node: 'node-b', repo: 'example/website' }, { node: 'node-b', repo: 'example/website', force: true }]);
  assert.equal(rowOf(w, 205).getAttribute('data-state'), 'running');
});

test('the demo plays a filter: ?epic=workfilter disables Work on #205 with the reason', async () => {
  const w = await epicWorld({ data: variant('workfilter') });
  assert.equal(workBtn(w, 205).getAttribute('data-work-off'), 'filter');
  assert.match(workBtn(w, 205).getAttribute('title'), /window is at 90 percent/);
});

// ---------------------------------------------------------------- the author check, said before Work

test('an issue by an author who is not allowed is marked, its Work action is disabled and names the author, and Run names it too', async () => {
  const w = await epicWorld({ data: variant('authors') });
  const row = rowOf(w, 205);
  const mark = row.querySelector('[data-author=blocked]');
  assert.equal(text(mark).trim(), '⚠ by dana-w, who is not on the allowed list');
  const b = workBtn(w, 205);
  assert.equal(b.getAttribute('data-work-off'), 'author');
  assert.notEqual(b.getAttribute('disabled'), null);
  assert.match(b.getAttribute('title'), /#205 was written by dana-w, who is not on the allowed list/);
  assert.match(b.getAttribute('aria-label'), /dana-w/);
  assert.match(text(row.querySelector('[data-work-hint=author]')), /dana-w/);
  b.click();
  await settle();
  assert.equal(posts(w).length, 0, 'a disabled action asks nothing');
  const run = page(w).querySelector('[data-run-authors]');
  assert.equal(text(run), 'Run would skip #205 (dana-w): its author is not on the allowed list.');
  assert.ok(!run.classList.contains('hidden'));
  assert.equal(rowOf(w, 206).querySelector('[data-author]'), null, 'other issues carry no mark');
});

test('an epic by an author who is not allowed disables Work on every row and Run says nothing in it will run', async () => {
  const w = await epicWorld({ data: variant('epicauthor') });
  assert.equal(workBtn(w, 205).getAttribute('data-work-off'), 'epic_author');
  assert.match(text(rowOf(w, 205).querySelector('[data-work-hint]')), /The epic was written by dana-w, who is not on the allowed list\. Nothing in it will run\./);
  assert.equal(text(page(w).querySelector('[data-run-authors]')), 'Run would skip everything: the epic was written by dana-w, who is not on the allowed list.');
});

test('when no author is known to be allowed, Work is disabled and says what to set; with nobody blocked Run says nothing about authors', async () => {
  const w = await epicWorld({ data: variant('noauthors') });
  assert.equal(workBtn(w, 205).getAttribute('data-work-off'), 'authors_unknown');
  assert.match(text(rowOf(w, 205).querySelector('[data-work-hint]')), /CCBOARD_EPIC_AUTHORS/);
  const ok = await epicWorld();
  assert.ok(page(ok).querySelector('[data-run-authors]').classList.contains('hidden'));
  assert.equal(text(page(ok).querySelector('[data-run-authors]')), '');
});

// ---------------------------------------------------------------- nothing beyond the writes-off note when the switch is off

test('with GitHub writes off the settings are not drawn and Work is a disabled action that says why; with no word about writes nothing is drawn', async () => {
  const quiet = detail({ writes: false });
  quiet.own_claims = [];
  const w = await epicWorld({ data: quiet });
  assert.ok(cfg(w).classList.contains('hidden'));
  const b = workBtn(w, 205);
  assert.equal(b.getAttribute('data-work-off'), 'writes');
  assert.notEqual(b.getAttribute('disabled'), null);
  assert.equal(b.getAttribute('title'), 'GitHub writes are off on this board (CCBOARD_EPIC_WRITES)');
  assert.equal(rowOf(w, 205).querySelectorAll('[data-claim-off=writes], [data-work-hint]').length, 1, 'one visible reason, not two');
  assert.equal(page(w).querySelector('[data-run-authors]').classList.contains('hidden'), true);
  const none = await epicWorld({ data: (({ writes, ...rest }) => rest)(detail()) });
  assert.equal(workBtn(none, 205), undefined);
  assert.ok(cfg(none).classList.contains('hidden'));
});

test('a node that is offline or paired without the tasks scope cannot work: the action is disabled and says so', async () => {
  const off = detail();
  off.nodes[1] = { ...off.nodes[1], status: 'offline' };
  const w = await epicWorld({ data: off });
  assert.equal(workBtn(w, 205).getAttribute('data-work-off'), 'offline');
  assert.equal(workBtn(w, 205).getAttribute('title'), 'node-b is offline: it cannot work.');
  const noscope = detail();
  noscope.nodes[1] = { ...noscope.nodes[1], tasks: false };
  const w2 = await epicWorld({ data: noscope });
  assert.equal(workBtn(w2, 205).getAttribute('data-work-off'), 'scope');
});

// ---------------------------------------------------------------- the phase of the work and Open on <node>

test('the work of this board shows its phase in words with a glyph, and a link to the task', async () => {
  const w = await epicWorld();
  const ph = (n) => rowOf(w, n).querySelector('[data-phase]');
  assert.equal(text(ph(201)).trim(), '✓ Work done (branch worktree-tax-rules)');
  assert.equal(text(ph(202)).trim(), '○ Work pull request opened (branch worktree-form-validation, PR #77)');
  assert.equal(text(ph(203)).trim(), '⚠ Work claim lost (branch worktree-address-autocomplete)');
  assert.ok(ph(203).classList.contains('warn'), 'attention hue, and the glyph and the words say it too');
  assert.match(ph(203).getAttribute('title'), /writes nothing more.*worktree is kept/);
  assert.equal(ph(204), null);
  assert.equal(text(rowOf(w, 202).querySelector('[data-open-task]')).trim(), 'Open on node-a');
  assert.equal(rowOf(w, 202).querySelector('[data-open-task]').getAttribute('href'), '#/tasks', 'a task on this board opens on its Tasks page');
  assert.equal(rowOf(w, 203).querySelector('[data-open-task]').getAttribute('href'), '#/n/node-b/t/12');
  assert.equal(rowOf(w, 205).querySelector('[data-open-task]'), null, 'no task, no link');
  for (const r of rows(w)) for (const a of r.querySelectorAll('a')) assert.ok(/^(https:\/\/github\.com\/|#\/)/.test(a.getAttribute('href')), a.getAttribute('href'));
});

test('every other phase has words: running, waiting for a person (attention), failed with its attempt, cancelled', async () => {
  const want = { workrun: ['running', '✽ Work running (branch worktree-form-validation)'], workwait: ['waiting', '✻ Work waiting for a person (branch worktree-form-validation)'],
    workfail: ['failed', '✕ Work failed (attempt 2, branch worktree-form-validation)'], workcancel: ['cancelled', '∙ Work cancelled (branch worktree-form-validation)'] };
  for (const [name, [phase, words]] of Object.entries(want)) {
    const w = await epicWorld({ data: variant(name) });
    const n = rowOf(w, 202).querySelector('[data-phase]');
    assert.equal(n.getAttribute('data-phase'), phase, name);
    assert.equal(text(n).trim(), words, name);
    assert.equal(n.classList.contains('warn'), phase === 'waiting' || phase === 'failed', name);
  }
});

test('work on a paired node shows the phase that node reports for its task (issue #149), and a node that reports none shows no work phase', async () => {
  const d = detail();
  const kid = d.children.find((x) => x.number === 205);
  const task = (extra) => ({ ref: 'node-b:9', phase: 'running', node: 'node-b', node_name: 'node-b', status: 'online', age_s: 0, updated_at: '2026-10-10T10:00:00+00:00', ...extra });
  kid.work = null;
  kid.task = task({ epic_phase: 'pr', epic_pr: 12 });
  let w = await epicWorld({ data: clone(d) });
  const n = rowOf(w, 205).querySelector('[data-phase]');
  assert.equal(n.getAttribute('data-phase'), 'pr');
  assert.equal(text(n).trim(), '○ Work pull request opened (PR #12)');
  kid.task = task({ epic_phase: 'waiting' });
  w = await epicWorld({ data: clone(d) });
  assert.equal(text(rowOf(w, 205).querySelector('[data-phase]')).trim(), '✻ Work waiting for a person');
  assert.ok(rowOf(w, 205).querySelector('[data-phase]').classList.contains('warn'));
  kid.task = task({ epic_phase: 'nonsense' });
  w = await epicWorld({ data: clone(d) });
  assert.equal(rowOf(w, 205).querySelector('[data-phase]').getAttribute('data-phase'), 'running', 'a word the page does not know is shown as running, never as markup');
  kid.task = task({});
  w = await epicWorld({ data: clone(d) });
  assert.equal(rowOf(w, 205).querySelector('[data-phase]'), null, 'an older node sends no epic phase: only the task line, as before');
});

test('the work phase never invents a link: a task reference that is not a number gets no Open link', async () => {
  const d = detail();
  d.children.find((x) => x.number === 203).task.ref = 'node-b:../x';
  const w = await epicWorld({ data: d });
  assert.equal(rowOf(w, 203).querySelector('[data-open-task]'), null);
});

// ---------------------------------------------------------------- the settings: all start off

test('the settings start off: phases only, no pull request, no Closes, with one paragraph that says in words what will be posted and pushed', async () => {
  const w = await epicWorld();
  assert.ok(!cfg(w).classList.contains('hidden'));
  assert.equal(text(cfg(w).querySelector('.ep-h')), 'When a node works an issue');
  assert.deepEqual(cfg(w).querySelectorAll('.seg-btn').map((b) => [text(b), b.getAttribute('aria-pressed')]), [['Phases only', 'true'], ['Summary', 'false'], ['None', 'false']]);
  assert.equal(box(w, 'auto_pr').checked, false);
  assert.equal(box(w, 'close').checked, false);
  assert.equal(text(cfg(w).querySelector('.ep-plan')), `${FX.details[KEY].plan.report} ${FX.details[KEY].plan.auto_pr} ${FX.details[KEY].plan.close}`, 'the page says what the board says');
  assert.match(text(cfg(w).querySelector('.ep-plan')), /No result text leaves the node\. Pushes nothing and opens no pull request\. Nothing says Closes #N\./);
  assert.equal(cfg(w).querySelector('.seg-ctl').getAttribute('role'), 'group');
  assert.equal(cfg(w).querySelectorAll('label').length, 3, 'pull request, Closes and Let MCP run this epic; the group boxes need two paired nodes');
  assert.equal(puts(w).length, 0, 'drawing the settings writes nothing');
});

test('the settings are drawn only with writes on and a paired node: not with writes off, and not on a board with no other node', async () => {
  const solo = detail();
  solo.nodes = solo.nodes.filter((n) => n.handle === 'local');
  assert.ok(cfg(await epicWorld({ data: solo })).classList.contains('hidden'));
  assert.ok(cfg(await epicWorld({ data: detail({ writes: false }) })).classList.contains('hidden'));
});

test('the report is a segmented control: a tap paints at once, sends only report, and the paragraph follows; the arrow keys move it', async () => {
  const gate = defer();
  const w = await epicWorld({ put: () => gate.p });
  seg(w, 'summary').click();
  await settle(2);
  assert.equal(seg(w, 'summary').getAttribute('aria-pressed'), 'true', 'painted before the board answers');
  assert.equal(seg(w, 'phase').getAttribute('aria-pressed'), 'false');
  assert.match(text(cfg(w).querySelector('.ep-plan')), /^Posts the phase comments and the first 1500 characters of each result, after home folders and anything shaped like a token are removed\./);
  assert.deepEqual(puts(w).map((c) => [c.path, c.body]), [['/api/epics/example/website/147/settings', { report: 'summary' }]]);
  gate.resolve({ ...detail(), report: 'summary' });
  await settle();
  assert.equal(seg(w, 'summary').getAttribute('aria-pressed'), 'true');
  w.ctx.__put = null;                                                          // from here the board keeps what it is sent
  seg(w, 'summary').dispatchEvent({ type: 'keydown', key: 'ArrowRight', preventDefault() {} });
  await settle();
  assert.equal(seg(w, 'none').getAttribute('aria-pressed'), 'true');
  assert.match(text(cfg(w).querySelector('.ep-plan')), /^Posts no phase comment and no result\./);
  assert.deepEqual(puts(w).map((c) => c.body), [{ report: 'summary' }, { report: 'none' }]);
});

test('a pull request and Closes are explicit toggles that start off: each sends only its own key, the paragraph follows, and Close without pull requests says it applies to nothing', async () => {
  const w = await epicWorld();
  const close = box(w, 'close');
  close.checked = true;
  close.dispatchEvent({ type: 'change' });
  await settle();
  assert.match(text(cfg(w).querySelector('.ep-plan')), /Close is on, but it only applies with pull requests on\./);
  const pr = box(w, 'auto_pr');
  pr.checked = true;
  pr.dispatchEvent({ type: 'change' });
  await settle();
  const plan = text(cfg(w).querySelector('.ep-plan'));
  assert.match(plan, /Pushes the task's own branch and opens a pull request when a task is done\. The pull request says Closes #N\./);
  assert.deepEqual(puts(w).map((c) => c.body), [{ close: true }, { auto_pr: true }]);
});

test('a refused change puts the old value back with the board\'s words, and the paragraph goes back too', async () => {
  const w = await epicWorld({ put: refusal(400, { error: 'report is phase, summary or none' }) });
  seg(w, 'none').click();
  await settle();
  assert.equal(seg(w, 'phase').getAttribute('aria-pressed'), 'true');
  assert.equal(seg(w, 'none').getAttribute('aria-pressed'), 'false');
  assert.match(text(cfg(w).querySelector('.ep-plan')), /^Posts short phase comments/);
  assert.ok(toasts(w).some((t) => /report is phase, summary or none\. Nothing was saved\./.test(JSON.stringify(t))));
  const pr = box(w, 'auto_pr');
  pr.checked = true;
  pr.dispatchEvent({ type: 'change' });
  await settle();
  assert.equal(box(w, 'auto_pr').checked, false);
});

test('an older board that sends report as true or false is read as summary and phases only', async () => {
  const w = await epicWorld({ data: detail({ report: true }) });
  assert.equal(seg(w, 'summary').getAttribute('aria-pressed'), 'true');
  const w2 = await epicWorld({ data: detail({ report: false }) });
  assert.equal(seg(w2, 'phase').getAttribute('aria-pressed'), 'true');
  const w3 = await epicWorld({ data: detail({ report: 'bypass' }) });
  assert.equal(seg(w3, 'phase').getAttribute('aria-pressed'), 'true', 'an unknown word is the default');
});

test('the words the page paints are the words the board gives as `plan`', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'epic.js'), 'utf8');
  for (const sentence of Object.values(FX.details[KEY].plan)) assert.ok(src.includes(sentence.replace(/'/g, "\\'")) || src.includes(sentence), sentence);
  for (const s of ['Posts the phase comments and the first 1500 characters of each result', 'Posts no phase comment and no result. The claim comment and a refusal still appear.',
    "Pushes the task\\'s own branch and opens a pull request when a task is done.", 'Close is on, but it only applies with pull requests on.']) assert.ok(src.includes(s), s);
});

// ---------------------------------------------------------------- the source

test('the work code builds nothing with innerHTML or style attributes and keeps UI text free of em-dashes', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'epic.js'), 'utf8');
  assert.ok(!/innerHTML|insertAdjacentHTML|cssText/.test(src));
  assert.ok(!src.includes('—'));
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'epic.css'), 'utf8');
  assert.ok(!css.includes('—') && !/data:/.test(css));
  assert.match(css, /\.ep-work-btn/);
});

// ---------------------------------------------------------------- the task card

const cardOf = (over) => {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.ctx.__t = { id: 23, project: 'ccboard', repo: 'ccboard', slug: 'audit', title: 'Audit the cache rules', branch: 'worktree-audit', base: 'main', worktree: '/w', tmux: 'ccboard--ccboard--t-audit', agent: 'claude', mode: 'worktree',
    phase: 'running', column: 'in_progress', session: { state: 'working', state_at: new Date().toISOString(), last_message: '', needs_attention: false, command: 'claude' }, overlap: [], created_at: new Date().toISOString(),
    cost_usd: null, pr_url: null, issue_number: 47, issue_url: 'https://github.com/example/board/issues/47', ...over };
  return w.run('taskCard(__t, {})');
};
const EPIC = TASK_FX.running.epic;

test('a task an epic started links the issue and the claim and says the phase of the work; any other task has neither', () => {
  const card = cardOf({ epic: EPIC });
  const links = card.querySelectorAll('.meta a').map((a) => [text(a), a.getAttribute('href')]);
  assert.deepEqual(links, [['#47', 'https://github.com/example/board/issues/47'], ['claim', 'https://github.com/example/board/issues/47#issuecomment-4701']]);
  assert.equal(text(card.querySelector('.tk-epic')).trim(), '✽ Epic work: running');
  assert.equal(card.querySelector('.tk-lostclaim'), null);
  const plainCard = cardOf({});
  assert.equal(plainCard.querySelector('.tk-epic'), null);
  assert.deepEqual(plainCard.querySelectorAll('.meta a').map((a) => text(a)), ['#47']);
});

test('a lost claim is its own state with a glyph: a chip, the words, and that the task is parked with its worktree kept', () => {
  const card = cardOf({ epic: TASK_FX.lost.epic });
  const chip = card.querySelector('.tk-lostclaim');
  assert.equal(text(chip), '⚠ lost claim');
  assert.ok(chip.classList.contains('state') && chip.classList.contains('waiting'));
  assert.match(text(card.querySelector('.tk-epic')), /⚠ Epic work: claim lost The issue may go to another node\. This task is parked and its worktree is kept\./);
  assert.ok(card.querySelector('.tk-epic').classList.contains('lost'));
});

test('every phase of the epic record has a glyph and words on the card, and a claim link that is not a GitHub comment address is not drawn', () => {
  const want = { running: '✽ Epic work: running', waiting: '✻ Epic work: waiting for a person', pr: '○ Epic work: pull request opened', failed: '✕ Epic work: failed (attempt 2)', cancelled: '∙ Epic work: cancelled', lost: '⚠ Epic work: claim lost' };
  for (const [name, words] of Object.entries(want)) assert.ok(text(cardOf({ epic: TASK_FX[name].epic }).querySelector('.tk-epic')).trim().startsWith(words), name);
  const evil = cardOf({ epic: { ...EPIC, claim_url: 'javascript:alert(1)#issuecomment-1' } });
  assert.deepEqual(evil.querySelectorAll('.meta a').map((a) => text(a)), ['#47']);
  const http = cardOf({ epic: { ...EPIC, claim_url: 'http://github.com/a/b/issues/1#issuecomment-2' } });
  assert.deepEqual(http.querySelectorAll('.meta a').map((a) => text(a)), ['#47']);
  assert.equal(cardOf({ epic: { ...EPIC, phase: 'weird' } }).querySelector('.tk-epic').getAttribute('data-epic-phase'), 'running', 'an unknown phase is not trusted as a class or a word');
});

// ---------------------------------------------------------------- the demo

async function demoWorld({ search = '' } = {}) {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.location.search = search;
  w.ctx.__fx = clone(FX);
  w.ctx.__tfx = clone(TASK_FX);
  w.ctx.__sfx = clone(STATE_FX);
  w.run(`globalThis.fetch = async (u) => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(/task-epic/.test(u) ? __tfx : /state\\.json|\\/api\\/state/.test(u) ? __sfx : __fx)) });`);
  return w;
}
const demoCall = (w, method, p, body) => { w.ctx.__a = [method, p, body]; return w.run('demoApi(...__a)').then(plain); };
const demoFail = (w, method, p, body) => { w.ctx.__a = [method, p, body]; return w.run('demoApi(...__a)').then(() => null, (e) => ({ status: e.status, body: plain(e.body) })); };
const ASK = { node: 'node-b', repo: 'example/website' };

test('demo: the work route is played here: the node starts the issue and the row shows its task running from then on, with a link to it', async () => {
  const w = await demoWorld();
  const r = await demoCall(w, 'POST', WORK, ASK);
  assert.equal(r.node, 'node-b');
  assert.equal(r.data.claimed, true);
  assert.match(r.data.task_ref, /^node-b:\d+$/);
  assert.match(r.data.branch, /^worktree-issue-205$/);
  const d = await demoCall(w, 'GET', '/api/epics/example/website/147');
  const c = d.children.find((x) => x.number === 205);
  assert.deepEqual(c.bucket, { node: 'node-b', state: 'running' });
  assert.equal(c.task.ref, r.data.task_ref);
  assert.equal(c.claim.status, 'running');
});

test('demo: ?work=<word> plays each refusal of the work route with the same body the board sends; lanes is refused until force is sent', async () => {
  const want = { edited: [409, 'edit_not_allowed'], editunknown: [409, 'edit_unknown'], nothere: [404, 'repo_not_here'], author: [409, 'author_not_allowed'], epicauthor: [409, 'epic_author_not_allowed'], unreadable: [409, 'unreadable'], bypass: [409, 'bypass_refused'], posture: [409, 'posture_refused'],
    lanes: [409, 'lanes_full'], launch: [409, 'launch_failed'], taken: [409, 'taken'], lost: [409, 'lost'], unknown: [409, 'claim_unknown'], writesoff: [409, 'writes_off'], closed: [409, 'closed'], done: [409, 'done'], attempts: [409, 'attempts_exhausted'] };
  for (const [flag, [status, why]] of Object.entries(want)) {
    const e = await demoFail(await demoWorld({ search: `?demo=1&work=${flag}` }), 'POST', WORK, ASK);
    assert.equal(e.status, status, flag);
    assert.equal(e.body.why, why, flag);
    assert.equal(e.body.reason, status === 404 ? 'repo_not_here' : 'work_refused', flag);
  }
  for (const [flag, status, reason] of [['offline', 503, 'offline'], ['scope', 409, 'scope'], ['unconfirmed', 504, 'unconfirmed']]) {
    const e = await demoFail(await demoWorld({ search: `?work=${flag}` }), 'POST', WORK, ASK);
    assert.deepEqual([e.status, e.body.reason], [status, reason]);
  }
  const lanes = await demoWorld({ search: '?work=lanes' });
  assert.equal((await demoFail(lanes, 'POST', WORK, ASK)).body.why, 'lanes_full');
  assert.equal((await demoCall(lanes, 'POST', WORK, { ...ASK, force: true })).data.claimed, true, 'Start anyway goes through');
  for (const word of ['constructor', 'tostring', 'nothing']) assert.equal((await demoCall(await demoWorld({ search: `?work=${word}` }), 'POST', WORK, ASK)).data.claimed, true, word);
});

test('demo: the settings are played here: a change is kept and the next read shows it; the answer carries report as a word, close and plan', async () => {
  const w = await demoWorld();
  const put = await demoCall(w, 'PUT', '/api/epics/example/website/147/settings', { report: 'summary', auto_pr: true, close: true });
  assert.deepEqual([put.report, put.auto_pr, put.close], ['summary', true, true]);
  const again = await demoCall(w, 'GET', '/api/epics/example/website/147');
  assert.deepEqual([again.report, again.auto_pr, again.close], ['summary', true, true]);
  const fresh = await demoCall(await demoWorld(), 'GET', '/api/epics/example/board/52');
  assert.equal(fresh.report, 'phase');
});

test('demo: ?epic=workrun, workwait, workfail, workcancel, authors, epicauthor and noauthors lay their variant over single children', async () => {
  const d = await demoCall(await demoWorld({ search: '?epic=workwait' }), 'GET', '/api/epics/example/website/147');
  assert.equal(d.children.find((x) => x.number === 202).work.phase, 'waiting');
  assert.equal(d.children.find((x) => x.number === 201).work.phase, 'done', 'the others are as they were');
  const a = await demoCall(await demoWorld({ search: '?epic=authors' }), 'GET', '/api/epics/example/website/147');
  assert.equal(a.children.find((x) => x.number === 205).author_ok, false);
  assert.equal(a.authors.blocked[0].number, 205);
  assert.equal((await demoCall(await demoWorld({ search: '?epic=epicauthor' }), 'GET', '/api/epics/example/website/147')).authors.epic_ok, false);
});

test('demo: ?taskepic=<name> lays the record of epic work over the demo task, for each phase; with none the task shows it done', async () => {
  const w0 = await demoWorld({ search: '' });
  const st0 = await demoCall(w0, 'GET', '/api/state');
  assert.equal(st0.tasks.find((t) => t.id === 23).epic.phase, 'done');
  for (const name of ['running', 'waiting', 'pr', 'failed', 'cancelled', 'lost']) {
    const st = await demoCall(await demoWorld({ search: `?demo=1&taskepic=${name}` }), 'GET', '/api/state');
    const t = st.tasks.find((x) => x.id === 23);
    assert.equal(t.epic.phase, name, name);
    assert.equal(t.phase, TASK_FX[name].task.phase, name);
  }
  const odd = await demoCall(await demoWorld({ search: '?taskepic=_note' }), 'GET', '/api/state');
  assert.equal(odd.tasks.find((t) => t.id === 23).epic.phase, 'done', 'a key that is not a variant changes nothing');
});

test('the demo page end to end: Work on node-b starts the issue, the row runs with Open on node-b, and a played refusal says why in words', async () => {
  const { w } = homeWorld({ state: fakeState() });
  made.push(w);
  w.load('pages/epic.js');
  w.ctx.__fx = clone(FX);
  w.run(`globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(__fx)) });
         api = (method, path, body) => demoEpics(method, path, body);`);
  w.location.hash = HASH;
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(text(workBtn(w, 205)).trim(), 'Work on node-b');
  workBtn(w, 205).click();
  await new Promise((r) => setTimeout(r, 500));
  assert.equal(rowOf(w, 205).getAttribute('data-state'), 'running');
  assert.match(rowOf(w, 205).querySelector('[data-open-task]').getAttribute('href'), /^#\/n\/node-b\/t\/\d+$/);
  const bad = homeWorld({ state: fakeState() }).w;
  made.push(bad);
  bad.load('pages/epic.js');
  bad.location.search = '?work=bypass';
  bad.ctx.__fx = clone(FX);
  bad.run(`globalThis.fetch = async () => ({ ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(__fx)) });
           api = (method, path, body) => demoEpics(method, path, body);`);
  bad.location.hash = HASH;
  await new Promise((r) => setTimeout(r, 400));
  workBtn(bad, 205).click();
  await new Promise((r) => setTimeout(r, 500));
  assert.match(text(rowOf(bad, 205).querySelector('[data-work-note]')), /asks for bypass permissions/);
  assert.equal(rowOf(bad, 205).getAttribute('data-state'), 'open');
});


test('edit history blocks Work with a glyph and words; an unchecked row has no edit mark', async () => {
  for (const state of ['blocked', 'unknown', null]) {
    const d = detail();
    const c = d.children.find((x) => x.number === 205);
    c.edit_state = state;
    c.edited_by = state === 'blocked' ? 'mallory' : null;
    const w = await epicWorld({ data: d });
    const mark = rowOf(w, 205).querySelector('[data-edit]');
    if (state) {
      assert.match(text(mark), /⚠/);
      assert.match(text(mark), state === 'blocked' ? /mallory.*not on the allowed list/ : /did not say who edited/);
      const btn = workBtn(w, 205);
      assert.notEqual(btn.getAttribute('disabled'), null);
      assert.match(btn.getAttribute('title'), /Work is blocked/);
    } else {
      assert.equal(mark, null);
      assert.equal(workBtn(w, 205).getAttribute('disabled'), null);
    }
  }
});

test('a terminal permission makes the epic wait for a person with the same Allow and Deny', async () => {
  const w = await epicWorld();
  w.ctx.__child = { work: { task_id: 71, phase: 'running' } };
  w.run(`state.tasks = [{ id: 71, tmux_name: 'shop--api--s1' }];
    state.pending_permissions = [{ id: 123, tmux_name: 'shop--api--s1', kind: 'terminal_prompt', summary: 'Bash command: git add src && git commit -m fix', choices: [{ number: '2', label: 'Yes' }, { number: '4', label: 'No' }] }];
    globalThis.__phase = epWorkPhaseNode(__child);
    globalThis.__permission = epPermissionNode(epPermission(__child));`);
  assert.match(text(w.get('__phase')), /waiting for a person/);
  const n = w.get('__permission');
  assert.ok(text(n).includes('Bash command: git add src && git commit -m fix'));
  assert.match(text(n), /2\. Yes · 4\. No/);
  assert.deepEqual(n.querySelectorAll('button').map(text), ['Allow', 'Deny']);
  n.querySelector('button').click();
  await tick();
  assert.ok(calls(w).some((c) => c.path === '/api/permission/123/allow'));
});
