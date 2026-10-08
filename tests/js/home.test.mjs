// Contract tests for the v0.5.5 Home page (app/static/pages/home.js) and the rich session row it is made of (sessionCard(s, {rich: true}) in
// pages/agents.js): the summary line and its filters, the away strip and awayDigest(), grouping, the older block, the chips (model, context meter,
// compact, worktree, PR, subagents, cost, limit), the tail expander on Live, and the keyed rows that keep their nodes across polls. Runs on the
// real scripts inside the vm harness; Live.subscribe / unsubscribe are recorders (tests/js/world.mjs), api() answers {ok: true}.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { EPOCH, ISO, calls, fakeState, fixtureState, homeWorld, page, plain, projectsOf, rowMenu, rowMenuLabels, sess, setState, text, tick } from './world.mjs';

const CK = 'phasezero--website--t-checkout-redesign', S1 = 'ccboard--ccboard--s1', P2 = 'petroit--api--s2', P3 = 'petroit--api--s3', P1 = 'petroit--api--s1';
const WT = 'phasezero--NestJs-Ecommerce-Backend--t-stock-sync', CX = 'ccboard--ccboard--cx1', ROOT = 'phasezero--root--s1';

/** A rich row for `s` over the state `st`, built the way Home builds it. */
function rowFor(w, s, st = fakeState({ projects: projectsOf({ shop: { api: [s] } }) }), opts = {}) {
  w.ctx.__s = s; w.ctx.__stx = st; w.ctx.__o = opts;
  w.run('state = __stx; globalThis.__row = sessionCard({ ...__s, project: "shop", repo: "api" }, { rich: true, showProject: false, ...__o })');
  return w.get('__row');
}
/** Patch a row (the last one rowFor built unless `row` says which) with session s over state st, as a poll would. */
const repatch = (w, s, st, row) => { if (row) w.ctx.__row = row; w.ctx.__s = s; w.ctx.__stx = st; w.run('state = __stx; __row.ccPatch({ ...__s, project: "shop", repo: "api" })'); };
const shown = (n) => !!n && !n.classList.contains('hidden');
const bdg = (row, cls) => row.querySelector('.' + cls);

// ---------------------------------------------------------------- the rich row: chips

test('the model chip shows the statusline model in mono and goes away without one', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { stats: { model: 'Fable 5.1', context_pct: 10, cost_usd: 1 } });
  const row = rowFor(w, s);
  assert.ok(row.classList.contains('rich'));
  assert.equal(text(bdg(row, 'bdg-model')), 'Fable 5.1');
  assert.ok(bdg(row, 'bdg-model').classList.contains('mono') && shown(bdg(row, 'bdg-model')));
  repatch(w, { ...s, stats: { context_pct: 10 } }, w.get('state'));
  assert.equal(shown(bdg(row, 'bdg-model')), false);
  repatch(w, { ...s, stats: null }, w.get('state'));
  assert.equal(shown(bdg(row, 'bdg-model')), false, 'a shell session has no stats at all');
  assert.equal(shown(bdg(row, 'ctx')), false);
});

test('the context meter: a 40 px bar and the percentage; amber from 60 %, red from 85 %', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1');
  const row = rowFor(w, s);
  const ctx = bdg(row, 'ctx');
  const at = (pct) => { repatch(w, { ...s, stats: { ...s.stats, context_pct: pct } }, w.get('state')); return [text(ctx.querySelector('.ctx-pct')), ctx.querySelector('.ctx-bar i').style.width, ctx.classList.contains('ctx-hi'), ctx.classList.contains('ctx-crit')]; };
  assert.deepEqual(at(42), ['42%', '42%', false, false]);
  assert.deepEqual(at(59), ['59%', '59%', false, false]);
  assert.deepEqual(at(60), ['60%', '60%', true, false], 'ctx-hi at 60');
  assert.deepEqual(at(84.4), ['84%', '84%', true, false]);
  assert.deepEqual(at(85), ['85%', '85%', false, true], 'ctx-crit at 85 (and no longer ctx-hi)');
  assert.deepEqual(at(100), ['100%', '100%', false, true]);
  assert.deepEqual(at(130), ['100%', '100%', false, true], 'clamped');
  assert.deepEqual(at(0), ['0%', '0%', false, false]);
  assert.ok(shown(ctx));
  repatch(w, { ...s, stats: { model: 'x' } }, w.get('state'));
  assert.equal(shown(ctx), false, 'no number, no meter');
});

test('the compact chip shows from 60 % and types /compact with enter through /keys', async () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { stats: { model: 'Opus 5', context_pct: 59, cost_usd: 1 } });
  const row = rowFor(w, s);
  const chip = bdg(row, 'compact-chip');
  assert.equal(shown(chip), false, '59 %: not yet');
  repatch(w, { ...s, stats: { ...s.stats, context_pct: 60 } }, w.get('state'));
  assert.ok(shown(chip) && text(chip) === 'compact');
  chip.click();
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.path.endsWith('/keys')), [{ method: 'POST', path: '/api/sessions/shop--api--s1/keys', body: { text: '/compact', enter: true } }]);
  assert.deepEqual(plain(w.get('__toasts')), [{ text: 'sent "/compact" to s1', kind: 'ok' }]);
  assert.equal(chip.disabled, false);
  repatch(w, { ...s, stats: { ...s.stats, context_pct: 97 } }, w.get('state'));
  assert.ok(shown(chip), 'and stays at 97 %');
  // never offered where typing /compact would do harm: a shell pane, a Codex session, an ended pane
  for (const over of [{ launcher: 'shell', agent: 'shell', command: 'bash' }, { agent: 'codex', launcher: 'codex' }, { state: 'ended' }]) {
    repatch(w, { ...s, stats: { ...s.stats, context_pct: 90 }, ...over }, w.get('state'));
    assert.equal(shown(chip), false, JSON.stringify(over));
  }
});

test('the compact chip is not a click on the row: it does not select it or open anything', async () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { stats: { model: 'Opus 5', context_pct: 70, cost_usd: 1 } });
  const row = rowFor(w, s);
  let rowClicks = 0;
  row.addEventListener('click', () => { rowClicks++; });
  bdg(row, 'compact-chip').click();
  assert.equal(rowClicks, 0, 'the click stops at the chip');
});

test('the worktree badge: a .claude/worktrees path, or a task running in a worktree', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { path: '/srv/projects/shop/api' });
  const row = rowFor(w, s);
  const badge = bdg(row, 'bdg-wt');
  assert.equal(shown(badge), false);
  repatch(w, { ...s, path: '/srv/projects/shop/api/.claude/worktrees/fix-cart' }, w.get('state'));
  assert.ok(shown(badge) && text(badge) === 'worktree');
  repatch(w, { ...s, path: '/srv/projects/shop/api/.claude/worktrees-old/x' }, w.get('state'));
  assert.equal(shown(badge), false, 'only the exact directory name counts');
  const st = fakeState({ projects: projectsOf({ shop: { api: [s] } }), tasks: [{ id: 9, tmux: s.tmux, title: 't', mode: 'worktree', pr_number: null }] });
  repatch(w, { ...s, task: { id: 9, title: 't', phase: 'running', auto_close: false } }, st);
  assert.ok(shown(badge), "the task's mode says worktree");
  st.tasks[0].mode = 'session';
  repatch(w, { ...s, task: { id: 9, title: 't', phase: 'running', auto_close: false }, state_at: ISO(1) }, st);
  assert.equal(shown(badge), false);
});

test('the PR chip comes from the linked task and links to the pull request', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { task: { id: 4, title: 'Redesign', phase: 'running', auto_close: false } });
  const st = fakeState({ projects: projectsOf({ shop: { api: [s] } }), tasks: [{ id: 4, tmux: s.tmux, title: 'Redesign', mode: 'worktree', pr_number: null, pr_url: null, pr_state: null }] });
  const row = rowFor(w, s, st);
  const chip = bdg(row, 'bdg-pr');
  assert.equal(shown(chip), false, 'no PR yet');
  st.tasks[0] = { ...st.tasks[0], pr_number: 88, pr_url: 'https://example.invalid/pull/88', pr_state: 'OPEN', ci: { bucket: 'pass' } };
  repatch(w, s, st);
  assert.ok(shown(chip));
  assert.equal(text(chip), 'PR #88');
  assert.equal(chip.getAttribute('href'), 'https://example.invalid/pull/88');
  assert.ok(chip.classList.contains('ok'), 'green when CI passes');
  st.tasks[0].ci = { bucket: 'fail' };
  repatch(w, s, st);
  assert.ok(chip.classList.contains('bad') && !chip.classList.contains('ok'));
  const none = rowFor(w, sess('shop', 'api', 's2'), fakeState({ projects: projectsOf({ shop: { api: [sess('shop', 'api', 's2')] } }), tasks: st.tasks }));
  assert.equal(shown(bdg(none, 'bdg-pr')), false, 'a session with no task has no PR chip');
});

test('the subagents chip counts flags.subagents above zero', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1');
  const row = rowFor(w, s);
  const chip = bdg(row, 'bdg-sub');
  assert.equal(shown(chip), false);
  repatch(w, { ...s, flags: { subagents: 2 } }, w.get('state'));
  assert.ok(shown(chip) && text(chip) === '2 subagents');
  repatch(w, { ...s, flags: { subagents: 1 } }, w.get('state'));
  assert.equal(text(chip), '1 subagent');
  repatch(w, { ...s, flags: { subagents: 0 } }, w.get('state'));
  assert.equal(shown(chip), false);
  repatch(w, { ...s, flags: {} }, w.get('state'));
  assert.equal(shown(chip), false);
});

test('the cost chip: amber at $10, red at $100, hidden without a cost', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1');
  const row = rowFor(w, s);
  const chip = bdg(row, 'bdg-cost');
  const at = (usd) => { repatch(w, { ...s, stats: { ...s.stats, cost_usd: usd } }, w.get('state')); return [shown(chip), text(chip), chip.classList.contains('cost-warn'), chip.classList.contains('cost-bad')]; };
  assert.deepEqual(at(0.5), [true, '$0.50', false, false]);
  assert.deepEqual(at(9.99), [true, '$9.99', false, false]);
  assert.deepEqual(at(10), [true, '$10.00', true, false], 'cost-warn at 10');
  assert.deepEqual(at(12.5), [true, '$12.50', true, false]);
  assert.deepEqual(at(99.99), [true, '$99.99', true, false]);
  assert.deepEqual(at(100), [true, '$100', false, true], 'cost-bad at 100 (and no longer cost-warn)');
  assert.deepEqual(at(140), [true, '$140', false, true]);
  assert.equal(at(0)[0], false);
  assert.equal(at(null)[0], false);
});

test('the limit chip: only on the session the episode names, only while it is in force; both record shapes read the same', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1');
  const other = sess('shop', 'api', 's2');
  const st = fakeState({ projects: projectsOf({ shop: { api: [s, other] } }),
    rate_limited: { value: { session: s.tmux, message: '5-hour limit reached', kind: '5h', resets_at: EPOCH(-90) }, at: ISO(5) } });
  const row = rowFor(w, s, st);
  const row2 = rowFor(w, other, st);
  const repatch1 = (st2) => repatch(w, s, st2, row);
  assert.ok(shown(bdg(row, 'bdg-limit')) && text(bdg(row, 'bdg-limit')) === 'limit');
  assert.match(bdg(row, 'bdg-limit').getAttribute('title'), /5-hour limit reached/);
  assert.equal(shown(bdg(row2, 'bdg-limit')), false, 'another session');
  st.rate_limited = { session: s.tmux, message: 'flat', kind: '5h', resets_at: EPOCH(-90) };
  repatch1(st);
  assert.ok(shown(bdg(row, 'bdg-limit')), 'a flat record reads like the wrapper');
  st.rate_limited = { value: { session: s.tmux, message: 'old', kind: '5h', resets_at: EPOCH(10) }, at: ISO(400) };
  repatch1(st);
  assert.equal(shown(bdg(row, 'bdg-limit')), false, 'the window has reset');
  st.rate_limited = null;
  repatch1(st);
  assert.equal(shown(bdg(row, 'bdg-limit')), false);
});

test('a blocked background job shows a blocked chip carrying what it needs', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { flags: { registry: { status: 'idle', job: { state: 'blocked', tempo: 'blocked', needs: 'approve the plan', suggested_reply: 'approve' } } } });
  const row = rowFor(w, s);
  assert.ok(shown(bdg(row, 'bdg-blocked')));
  assert.equal(bdg(row, 'bdg-blocked').getAttribute('title'), 'approve the plan');
  repatch(w, { ...s, flags: { registry: { status: 'idle', job: { state: 'running', tempo: 'steady' } } } }, w.get('state'));
  assert.equal(shown(bdg(row, 'bdg-blocked')), false);
});

test('the plain row (Agents roster, inbox peek) carries none of the rich chips', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { stats: { model: 'Opus 5', context_pct: 90, cost_usd: 140 }, flags: { subagents: 3 } });
  w.ctx.__s = { ...s, project: 'shop', repo: 'api' };
  w.run('globalThis.__plain = sessionCard(__s, { compact: true })');
  const row = w.get('__plain');
  assert.equal(row.classList.contains('rich'), false);
  for (const cls of ['bdg-model', 'ctx', 'compact-chip', 'bdg-cost', 'bdg-sub']) assert.equal(row.querySelector('.' + cls), null, cls);
  assert.deepEqual(plain(rowMenuLabels(w, row)), ['Kill'], 'the plain row has no Reply and no Tail in its menu (and nothing to acknowledge)');
  assert.match(text(row.querySelector('.rr-meta')), /Opus 5 · ctx 90%/, 'the plain row keeps its meta line');
});

// ---------------------------------------------------------------- the tail expander

const toggleTail = (w, row) => assert.ok(rowMenu(w, row, /^(Tail|Hide tail)$/), 'the row menu offers the tail');   // the tail toggle is an item of the row's ... menu
const tailOpen = (row) => !row.querySelector('.rr-tail').classList.contains('hidden');
const pre = (row) => row.querySelector('pre.tail');

test('the tail expander subscribes once on expand and unsubscribes once on collapse, through Live', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1');
  const row = rowFor(w, s);
  const live = () => plain(w.get('__live'));
  assert.ok(rowMenuLabels(w, row).includes('Tail') && pre(row), 'a rich row has the toggle (in its ... menu) and its pre.tail');
  assert.equal(row.querySelector('.rr-tail').classList.contains('hidden'), true, 'collapsed at first');
  assert.equal(tailOpen(row), false);
  assert.deepEqual(live().subscribed, [], 'nothing is subscribed until it is expanded');
  toggleTail(w, row);
  assert.deepEqual(live().subscribed, ['shop--api--s1']);
  assert.equal(row.querySelector('.rr-tail').classList.contains('hidden'), false);
  assert.equal(tailOpen(row), true);
  assert.ok(rowMenuLabels(w, row).includes('Hide tail'), 'the menu item says what the next tap does');
  // lines arrive: the last 12 of them
  const lines = Array.from({ length: 20 }, (_, i) => `line ${i + 1}`);
  w.ctx.__lines = lines;
  w.run("__live.fns['shop--api--s1'](__lines)");
  assert.equal(text(pre(row)), lines.slice(-12).join('\n'));
  w.ctx.__lines = ['$ npm test', '12 passing'];
  w.run("__live.fns['shop--api--s1'](__lines)");
  assert.equal(text(pre(row)), '$ npm test\n12 passing');
  toggleTail(w, row);
  assert.deepEqual(live().unsubscribed, ['shop--api--s1']);
  assert.equal(live().subscribed.length, 1, 'one subscribe in all');
  assert.equal(row.querySelector('.rr-tail').classList.contains('hidden'), true);
  assert.equal(tailOpen(row), false);
  toggleTail(w, row);
  assert.equal(plain(w.get('__live')).subscribed.length, 2, 'expanding again subscribes again');
});

test('a state poll never touches the subscription: re-patching an expanded row neither subscribes nor unsubscribes', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1');
  const st = fakeState({ projects: projectsOf({ shop: { api: [s] } }) });
  const row = rowFor(w, s, st);
  toggleTail(w, row);
  for (let i = 0; i < 3; i++) repatch(w, { ...s, state_at: ISO(i), last_message: 'tick ' + i, stats: { ...s.stats, context_pct: 50 + i } }, st);
  const live = plain(w.get('__live'));
  assert.deepEqual([live.subscribed.length, live.unsubscribed.length], [1, 0]);
  assert.equal(row.querySelector('.rr-tail').classList.contains('hidden'), false, 'and the tail stays open');
});

test('a row that is destroyed or leaves the document drops its subscription', () => {
  const { w } = homeWorld();
  const a = rowFor(w, sess('shop', 'api', 'a'));
  toggleTail(w, a);
  a.ccDestroy();
  assert.deepEqual(plain(w.get('__live')).unsubscribed, ['shop--api--a']);
  a.ccDestroy();
  assert.equal(plain(w.get('__live')).unsubscribed.length, 1, 'destroying twice is harmless');
  const b = rowFor(w, sess('shop', 'api', 'b'));
  w.document.querySelector('#page').append(b);
  toggleTail(w, b);
  b.remove();                                                              // its block collapsed or the session ended
  w.run('sessionTailSweep()');
  assert.deepEqual(plain(w.get('__live')).unsubscribed, ['shop--api--a', 'shop--api--b']);
});

test('demo mode: the tail shows the one-line note and nothing else (Live is a quiet no-op there)', () => {
  const { w } = homeWorld({ realLive: true, extra: { location: undefined } });
  w.run('demoFlag = true');
  const row = rowFor(w, sess('shop', 'api', 's1'));
  toggleTail(w, row);
  assert.equal(text(pre(row)), 'demo: live tail unavailable');
  assert.equal(w.run('Live.es'), null);
  assert.equal(w.run('Live.subs.size'), 0, 'no subscriber is kept in demo mode');
});

// ---------------------------------------------------------------- awayDigest

const T0 = 1_790_000_000;                                          // an arbitrary "since", epoch seconds
const iso = (sec) => new Date(sec * 1000).toISOString();
const digest = (w, st, since, lim, prev) => { w.ctx.__a = [st, since, lim, prev]; return plain(w.run('awayDigest(...__a)')); };
const ZERO = { done: 0, needs: 0, errored: 0, jobs: 0, prs: 0, limits: 0, blocked: 0, total: 0 };

function awayFleet() {
  const at = (dt) => iso(T0 + dt);
  return fakeState({
    projects: projectsOf({ shop: { api: [
      sess('shop', 'api', 'd1', { state: 'done', state_at: at(100) }), sess('shop', 'api', 'd2', { state: 'done', state_at: at(5000) }),
      sess('shop', 'api', 'd0', { state: 'done', state_at: at(-100) }),                                 // finished before since: not new
      sess('shop', 'api', 'w1', { state: 'waiting', state_at: at(50) }), sess('shop', 'api', 'e1', { state: 'errored', state_at: at(60) }),
      sess('shop', 'api', 'i1', { state: 'idle', state_at: at(70) }), sess('shop', 'api', 'k1', { state: 'working', state_at: at(80) }),
    ] } }),
    jobs: [{ id: 1, name: 'a', last_run_at: at(300), enabled: 1 }, { id: 2, name: 'b', last_run_at: at(-300), enabled: 1 }, { id: 3, name: 'c', last_run_at: null, enabled: 1 }],
  });
}

test('awayDigest counts the sessions whose state changed to done, needs you and error after since', () => {
  const { w } = homeWorld();
  assert.deepEqual(digest(w, awayFleet(), T0), { ...ZERO, done: 2, needs: 1, errored: 1, jobs: 1, total: 5 });
});

test('awayDigest: the cut is strictly after since, in epoch seconds, milliseconds or an ISO string', () => {
  const { w } = homeWorld();
  const st = awayFleet();
  const exact = fakeState({ projects: projectsOf({ shop: { api: [sess('shop', 'api', 'x', { state: 'done', state_at: iso(T0) })] } }) });
  assert.equal(digest(w, exact, T0).done, 0, 'finished at the very second: not after');
  assert.equal(digest(w, exact, T0 - 1).done, 1);
  for (const since of [T0, T0 * 1000, iso(T0), String(T0)]) assert.deepEqual(digest(w, st, since), { ...ZERO, done: 2, needs: 1, errored: 1, jobs: 1, total: 5 }, String(since));
  assert.deepEqual(digest(w, st, T0 + 60), { ...ZERO, done: 2, jobs: 1, total: 3 }, 'a later since sees less: the waiting and the failed one (at +50 and +60) are old news');
  assert.deepEqual(digest(w, st, T0 + 400), { ...ZERO, done: 1, total: 1 }, 'and the job run at +300 is too');
});

test('awayDigest: jobs whose last run finished after since', () => {
  const { w } = homeWorld();
  const st = fakeState({ jobs: [{ id: 1, last_run_at: iso(T0 + 1) }, { id: 2, last_run_at: iso(T0) }, { id: 3, last_run_at: iso(T0 - 5) }, { id: 4, last_run_at: null }, { id: 5 }] });
  assert.equal(digest(w, st, T0).jobs, 1);
});

test('awayDigest: tasks that reached the pr or merged column after since (a change time, or the column last visit)', () => {
  const { w } = homeWorld();
  const tasks = [
    { id: 1, column: 'pr', updated: iso(T0 + 10) }, { id: 2, column: 'merged', updated: iso(T0 + 10) }, { id: 3, column: 'pr', updated: iso(T0 - 10) },
    { id: 4, column: 'in_progress', updated: iso(T0 + 10) }, { id: 5, column: 'done', updated: iso(T0 + 10) },
    { id: 6, column: 'pr', pr: { merged_at: iso(T0 + 99) } },
  ];
  assert.equal(digest(w, fakeState({ tasks }), T0).prs, 3, 'two with a fresh change time and the one with a fresh pr time; old and wrong-column ones do not count');
  const bare = [{ id: 7, column: 'pr' }, { id: 8, column: 'merged' }, { id: 9, column: 'in_progress' }];
  assert.equal(digest(w, fakeState({ tasks: bare }), T0).prs, 0, 'no change time and no earlier column: nothing to say');
  assert.equal(digest(w, fakeState({ tasks: bare }), T0, null, { 7: 'in_progress', 8: 'merged', 9: 'in_progress' }).prs, 1, 'it moved to pr since the last visit; the merged one was already merged');
});

test('awayDigest: one limit episode counts once however often it was retried; events at or before since are ignored', () => {
  const { w } = homeWorld();
  const ev = (dt, key, resets) => ({ t: T0 + dt, key, v: 1, m: { session: 'a--b--c', resets_at: resets } });
  const events = [ev(10, '5h', 111), ev(20, '5h', 111), ev(30, '5h', 111), ev(40, '7d', 222), ev(50, '5h', 333), ev(-5, '5h', 444), ev(0, '5h', 555)];
  assert.equal(digest(w, fakeState(), T0, events).limits, 3, 'three episodes: 5h@111, 7d@222, 5h@333');
  assert.equal(digest(w, fakeState(), T0, { events }).limits, 3, 'the endpoint body ({events: [...]}) works too');
  assert.equal(digest(w, fakeState(), T0, []).limits, 0);
  assert.equal(digest(w, fakeState(), T0, null).limits, 0);
  assert.equal(digest(w, fakeState(), T0, [{ t: T0 + 5 }, { t: T0 + 3600 * 5 + 5 }]).limits, 2, 'without a reset time, the same hour is one episode');
});

test('awayDigest: a blocked registry job counts whatever the time; total is the sum of the parts; nothing in, zeros out', () => {
  const { w } = homeWorld();
  const st = fixtureState();
  const d = digest(w, st, EPOCH(300), [{ t: EPOCH(100), key: '5h', m: { resets_at: 1 } }]);
  assert.equal(d.blocked, 1, 'ccboard/s1');
  assert.equal(d.total, d.done + d.needs + d.errored + d.jobs + d.prs + d.limits + d.blocked);
  assert.equal(digest(w, st, EPOCH(300)).blocked, 1);
  assert.equal(digest(w, st, EPOCH(0)).blocked, 1, 'still blocked now, even if nothing else is new');
  for (const [s, since] of [[null, T0], [undefined, T0], [st, 0], [st, null], [st, 'nonsense']]) assert.deepEqual(digest(w, s, since), ZERO, `${s && 'state'} ${since}`);
});

// ---------------------------------------------------------------- mounting Home

/** A Home world that counts mounts of the home page; `at` navigates to the hash and mounts. */
function home(over = {}) {
  const { w } = homeWorld(over);
  w.ctx.__mounts = 0;
  w.run('{ const m = pages.home.mount; pages.home.mount = function (...a) { __mounts++; return m.apply(this, a); }; }');
  return { w, mount: (hash = '#/') => { w.location.hash = hash; return page(w); }, mounts: () => w.get('__mounts') };
}
const q = (w, sel) => page(w).querySelector(sel);
const qa = (w, sel) => page(w).querySelectorAll(sel);
const rowIds = (w, root = page(w)) => root.querySelectorAll('.rrow').map((n) => n.getAttribute('data-tmux'));
const blockIds = (w) => q(w, '.pblocks').children.map((n) => n.getAttribute('data-block'));
const seg = (w, f) => q(w, `.sum-seg[data-f=${f}]`);

test('Home mounts into #page in order: head, summary, inbox, schedules, blocks, usage', () => {
  const { w, mount } = home();
  const root = mount();
  assert.equal(w.document.body.getAttribute('data-page'), 'home');
  assert.match(w.document.title, /^\(\d+\) Home · ccboard$/);
  const kids = root.children.map((n) => n.className.split(' ')[0] + (n.id ? '#' + n.id : ''));
  const at = (cls) => kids.findIndex((k) => k === cls || k.startsWith(cls));
  assert.equal(kids[0], 'page-head');
  assert.deepEqual([at('page-head'), at('summary'), at('away'), at('home-inbox'), at('sched'), at('pblocks'), at('home-usage')].every((x, i, a) => x >= 0 && (i === 0 || x > a[i - 1])), true, kids.join(' '));
  assert.equal(text(q(w, '.page-head h1')), 'Home');
  assert.ok(q(w, '.seg-ctl'), 'the group control sits in the page head');
});

test('Home drops the v0.4 board: no project form, no kanban, no jobs list, no live grid', () => {
  const { w, mount } = home();
  mount();
  for (const id of ['new-project', 'importrow', 'queue', 'tasks', 'jobs', 'live', 'projects']) assert.equal(q(w, '#' + id), null, '#' + id);
  assert.equal(q(w, '.kanban'), null);
});

test('the symbols other pages import stay global (the login modal is gone: its Log in buttons are accountLogin, pages/agents.js)', () => {
  const { w } = home();
  for (const name of ['renderTasks', 'renderJobs', 'logout', 'renderBanner', 'homeInstallHint', 'taskCard', 'openTaskModal', 'awayDigest', 'decide']) {
    assert.equal(w.run(`typeof ${name}`), 'function', name);
  }
  assert.equal(w.run('typeof closeModal'), 'undefined', 'the legacy #modal and its closeModal are gone (v0.5.13): the diff and PR viewer is a sheet');
  for (const name of ['skeleton', 'dropSkeleton', 'items', 'select', 'act', 'paint', 'sync', 'target', 'order', 'openNth', 'reset']) assert.equal(w.run(`typeof Pages.${name}`), 'function', `Pages.${name}`);
});

test('first paint before the first state: skeleton rows, dropped when the state arrives', () => {
  const { w, mount } = home({ withState: false });
  w.run('state = null');
  mount();
  assert.equal(qa(w, '.skel-rows').length, 1);
  assert.equal(qa(w, '.rrow').length, 0);
  setState(w, fixtureState());
  assert.equal(qa(w, '.skel-rows').length, 0);
  assert.ok(qa(w, '.rrow').length > 0);
});

// ---------------------------------------------------------------- the summary line and its filters

test('the summary line counts sessions per state, with the glyphs, and the error segment only when there is one', () => {
  const { w, mount } = home();
  mount();
  const counts = () => ['waiting', 'working', 'idle', 'done', 'errored'].map((f) => text(seg(w, f).querySelector('.sum-n')));
  assert.deepEqual(counts(), ['2', '2', '1', '1', '1'], 'p3 and the checkout wait; cx1 and stock-sync work; s1 idles; p1 is done; p2 failed (the ended one counts nowhere)');
  assert.deepEqual(['waiting', 'working', 'idle', 'done'].map((f) => text(seg(w, f).querySelector('.sum-l'))), ['need you', 'working', 'idle', 'done']);
  assert.ok(seg(w, 'waiting').querySelector('.glyph.waiting') && seg(w, 'working').querySelector('.glyph.working') && seg(w, 'idle').querySelector('.glyph.idle') && seg(w, 'done').querySelector('.glyph.done'));
  assert.equal(seg(w, 'errored').classList.contains('hidden'), false);
  const st = fixtureState();
  st.projects[1].repos[0].sessions = st.projects[1].repos[0].sessions.filter((s) => s.name !== 's2');
  setState(w, st);
  assert.equal(seg(w, 'errored').classList.contains('hidden'), true, 'no failed session: no segment');
  assert.equal(seg(w, 'waiting').classList.contains('zero'), false);
  assert.deepEqual(counts().slice(0, 4), ['2', '2', '1', '1']);
});

test('every segment is a filter link built by buildHash and toggling ?f=; the active one links back to #/', () => {
  const { w, mount } = home();
  mount();
  for (const f of ['waiting', 'working', 'idle', 'done', 'errored']) {
    w.ctx.__f = f;
    assert.equal(seg(w, f).getAttribute('href'), w.run("buildHash('home', {}, { f: __f })"), f);
    assert.equal(seg(w, f).getAttribute('href'), `#/?f=${f}`);
  }
  assert.equal(seg(w, 'waiting').hasAttribute('aria-current'), false);
  assert.equal(seg(w, 'waiting').classList.contains('on'), false);
  mount('#/?f=waiting');
  assert.ok(seg(w, 'waiting').classList.contains('on') && seg(w, 'waiting').getAttribute('aria-current') === 'true');
  assert.equal(seg(w, 'waiting').getAttribute('href'), '#/', 'tapping the active segment clears the filter');
  assert.equal(seg(w, 'working').getAttribute('href'), '#/?f=working');
  assert.equal(seg(w, 'working').classList.contains('on'), false);
});

test('tapping a segment filters in place (no remount): only that state is listed; tapping it again clears', () => {
  const { w, mount, mounts } = home();
  mount();
  const all = rowIds(w).length;
  assert.equal(all, 8, 'the ended session is a row too');
  seg(w, 'waiting').click();
  assert.equal(w.location.hash, '#/?f=waiting');
  assert.equal(mounts(), 1, 'a filter change is onRoute, not a mount');
  assert.deepEqual(rowIds(w).sort(), [CK, P3].sort());
  assert.ok(seg(w, 'waiting').classList.contains('on'));
  seg(w, 'working').click();
  assert.deepEqual(rowIds(w).sort(), [CX, WT].sort(), 'switching filters swaps the rows');
  seg(w, 'working').click();                                              // the active one: clear
  assert.equal(w.location.hash, '#/');
  assert.equal(rowIds(w).length, all);
  assert.equal(mounts(), 1);
});

test('a filter in the URL applies on mount; an unknown one is ignored; blocks without a match leave', () => {
  let h = home(); h.mount('#/?f=working');
  assert.deepEqual(rowIds(h.w).sort(), [CX, WT].sort());
  assert.deepEqual(blockIds(h.w).filter((b) => b !== 'older'), ['p:phasezero', 'p:ccboard'], 'petroit has nothing working: its block is not drawn; the others keep their order');
  h = home(); h.mount('#/?f=bogus');
  assert.equal(rowIds(h.w).length, 8);
  assert.equal(h.w.location.hash, '#/?f=bogus', 'and the address is left alone');
  h = home(); h.mount('#/?f=idle');
  assert.deepEqual(rowIds(h.w), [S1]);
});

test('a filter hides the inbox section (the rows are the list) and a filter with no match says so with a way out', () => {
  const { w, mount } = home();
  mount();
  assert.equal(q(w, '#inbox-home').classList.contains('hidden'), false);
  seg(w, 'waiting').click();
  assert.equal(q(w, '#inbox-home').classList.contains('hidden'), true);
  const st = fixtureState();
  st.projects[2].repos[1].sessions = []; st.projects[1].repos[0].sessions = st.projects[1].repos[0].sessions.filter((s) => s.name !== 's3');
  setState(w, st);                                                          // nobody waits any more
  assert.deepEqual(rowIds(w), []);
  assert.match(text(q(w, '.home-note')), /Nothing is waiting for you right now/);
  const clear = q(w, 'a.btn[href="#/"]');
  assert.ok(clear && text(clear) === 'Clear filter' && !clear.classList.contains('hidden'));
});

// ---------------------------------------------------------------- grouping

test('grouped by project (the default): attention first, then the latest activity; quiet projects wait in one older block at the end', () => {
  const { w, mount } = home();
  mount();
  assert.deepEqual(blockIds(w), ['p:phasezero', 'p:petroit', 'p:ccboard', 'older'], 'two projects need you, then the one that is just working, then older');
  assert.equal(text(q(w, '[data-block="p:petroit"] .pb-count')), '3 sessions');
  assert.equal(q(w, '[data-block="p:phasezero"] .pb-name').getAttribute('href'), '#/p/phasezero', 'the name links to the project page');
  assert.equal(text(q(w, '[data-block="p:phasezero"] .pb-name')), 'phasezero');
  assert.equal(text(q(w, '[data-block="older"] .pb-older-toggle')), '▸ older (2)', 'internalSystem and mailgate have no sessions');
  assert.equal(w.localStorage.getItem('ccboard:home:group'), null);
});

test('inside a repo the rows are attention first, then the latest activity; a block lists its project folder, then its repos', () => {
  const { w, mount } = home();
  mount();
  assert.deepEqual(rowIds(w, q(w, '[data-block="p:petroit"]')), [P3, P2, P1], 'waiting, errored, then done');
  assert.deepEqual(rowIds(w, q(w, '[data-block="p:ccboard"]')), [CX, S1], 'working before idle');
  assert.deepEqual(rowIds(w, q(w, '[data-block="p:phasezero"]')), [ROOT, WT, CK], 'the project folder, then the repos in their own order');
});

test('repo sub-headers show only when a project has several repos or a project folder; a lone repo named like its project has none', () => {
  const { w, mount } = home();
  mount();
  const heads = (block) => q(w, `[data-block="${block}"]`).querySelectorAll('.pgroup-head').filter((h) => !h.classList.contains('hidden')).map((h) => text(h.querySelector('.pg-name')));
  assert.deepEqual(heads('p:phasezero'), ['project folder', 'NestJs-Ecommerce-Backend', 'website']);
  assert.deepEqual(heads('p:petroit'), ['api'], 'a single repo with another name keeps its header');
  assert.deepEqual(heads('p:ccboard'), [], 'the repo is the project');
});

test('the block shows the project cost (today and 7 days) and a + session button that opens the launcher in the sheet', () => {
  const st = fixtureState({ cost: { value: { projects: { phasezero: { today: 38.2, week: 171.4, total: 412.6 }, ccboard: { today: 22.4, week: 58.1, total: 58.1 } } }, at: ISO(5) } });
  const { w, mount } = home({ state: st });
  mount();
  assert.equal(text(q(w, '[data-block="p:phasezero"] .pb-cost')), '$38.20 today · $171 7d', '$100 and over loses its cents, like the row chip');
  assert.equal(text(q(w, '[data-block="p:ccboard"] .pb-cost')), '$22.40 today · $58.10 7d');
  assert.equal(text(q(w, '[data-block="p:petroit"] .pb-cost')), '', 'no ccusage record for this project: nothing shown');
  const add = q(w, '[data-block="p:phasezero"] .pb-add');
  assert.equal(text(add), '+ session');
  assert.equal(w.document.querySelector('#sheet').open, false);
  add.click();
  const sheet = w.document.querySelector('#sheet');
  assert.equal(sheet.open, true, 'the existing launcher opens in the sheet');
  assert.match(text(sheet), /New session/);
  assert.ok(sheet.querySelector('form'), 'with the session form in it');
});

test('grouped by state: one block per state in attention order, each row names its project and repo; the choice is remembered', () => {
  const { w, mount } = home();
  mount();
  const btn = (k) => q(w, `.seg-btn[data-group=${k}]`);
  assert.equal(btn('project').getAttribute('aria-pressed'), 'true');
  btn('state').click();
  assert.equal(w.localStorage.getItem('ccboard:home:group'), 'state');
  assert.equal(btn('state').getAttribute('aria-pressed'), 'true');
  assert.equal(btn('project').getAttribute('aria-pressed'), 'false');
  assert.deepEqual(blockIds(w), ['s:waiting', 's:errored', 's:working', 's:idle', 's:done', 's:ended']);
  assert.deepEqual(rowIds(w, q(w, '[data-block="s:waiting"]')).sort(), [CK, P3].sort());
  assert.equal(text(q(w, '[data-block="s:waiting"] [data-tmux="petroit--api--s3"] .rr-where')), 'petroit/api');
  assert.equal(q(w, '[data-block="s:waiting"] [data-tmux="petroit--api--s3"] .rr-where').classList.contains('hidden'), false);
  // a remount reads it back
  mount('#/agents'); mount('#/');
  assert.equal(btn('state').getAttribute('aria-pressed'), 'true');
  assert.deepEqual(blockIds(w).slice(0, 2), ['s:waiting', 's:errored']);
});

test('grouped by agent: claude first, then codex, shell last', () => {
  const st = fixtureState();
  st.projects[0].repos[0].sessions.push(sess('ccboard', 'ccboard', 'sh', { launcher: 'shell', agent: 'shell', command: 'bash', stats: null }));
  const { w, mount } = home({ state: st });
  w.localStorage.setItem('ccboard:home:group', 'agent');
  mount();
  assert.deepEqual(blockIds(w), ['a:claude', 'a:codex', 'a:shell']);
  assert.deepEqual(rowIds(w, q(w, '[data-block="a:codex"]')), [CX]);
  assert.deepEqual(rowIds(w, q(w, '[data-block="a:shell"]')), ['ccboard--ccboard--sh']);
  assert.ok(q(w, '[data-block="a:claude"] .pb-head .glyph.agent') && q(w, '[data-block="a:codex"] .pb-head .glyph.agent'), 'the agent glyph leads the block header');
});

test('a stored group that is not one of the three falls back to project', () => {
  const { w, mount } = home();
  w.localStorage.setItem('ccboard:home:group', 'banana');
  mount();
  assert.equal(blockIds(w)[0], 'p:phasezero');
  assert.equal(q(w, '.seg-btn[data-group=project]').getAttribute('aria-pressed'), 'true');
});

// ---------------------------------------------------------------- the older block

const DAY = 24 * 60;
function agedFleet(extra = {}) {
  const live = sess('fresh', 'app', 's1', { state: 'idle', state_at: ISO(DAY) });
  const old1 = sess('stale', 'app', 's1', { state: 'idle', state_at: ISO(8 * DAY) });
  const edge = sess('edge', 'app', 's1', { state: 'idle', state_at: ISO(7 * DAY + 5) });
  const nearly = sess('nearly', 'app', 's1', { state: 'idle', state_at: ISO(6 * DAY + 23 * 60) });
  const oldWaiting = sess('oldwait', 'app', 's1', { state: 'waiting', needs_attention: true, state_at: ISO(20 * DAY) });
  return fakeState({ projects: projectsOf({ fresh: { app: [live] }, stale: { app: [old1] }, edge: { app: [edge] }, nearly: { app: [nearly] }, oldwait: { app: [oldWaiting] }, empty: { app: [] } }), ...extra });
}

test('projects with no session activity for 7 days fold into one older block; anything that needs you or is working stays out of it', () => {
  const { w, mount } = home({ state: agedFleet() });
  mount();
  assert.deepEqual(blockIds(w), ['p:oldwait', 'p:fresh', 'p:nearly', 'older'], 'a 20 day old waiting session still leads; 6 days 23 h is still active');
  assert.equal(text(q(w, '.pb-older-toggle')), '▸ older (3)', 'stale, edge (just over 7 days) and empty (no sessions)');
  const st = agedFleet();
  st.projects[0].repos[0].sessions[0].state = 'working';                    // a week-old session is working now: it is the freshest thing there is
  st.projects[0].repos[0].sessions[0].state_at = ISO(1);
  setState(w, st);
  assert.ok(blockIds(w).includes('p:fresh'));
});

test('older is collapsed until expanded; the choice is stored in ccboard:home:collapsed as a JSON array', () => {
  const { w, mount } = home({ state: agedFleet() });
  mount();
  const older = () => q(w, '[data-block="older"]');
  assert.ok(older().classList.contains('collapsed'));
  assert.equal(older().querySelectorAll('.pblock').length, 0, 'no rows are drawn for a collapsed block');
  assert.equal(w.localStorage.getItem('ccboard:home:collapsed'), null, 'nothing is stored until the person chooses');
  q(w, '.pb-older-toggle').click();
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:home:collapsed')), [], 'expanded: older leaves the set');
  assert.equal(older().classList.contains('collapsed'), false);
  assert.deepEqual(older().querySelectorAll('.pblock').map((n) => n.getAttribute('data-project')).sort(), ['edge', 'empty', 'stale']);
  assert.equal(text(q(w, '.pb-older-toggle')), '▾ older (3)');
  assert.equal(text(older().querySelector('[data-project=empty] .pb-count')), '0 sessions');
  assert.equal(older().querySelector('[data-project=empty]').querySelectorAll('.rrow').length, 0);
  q(w, '.pb-older-toggle').click();
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:home:collapsed')), ['older']);
  assert.equal(older().querySelectorAll('.pblock').length, 0);
});

test('a project block collapses to its header; the collapsed set holds project names (and survives a remount)', () => {
  const { w, mount } = home({ state: agedFleet() });
  mount();
  const toggle = () => q(w, '[data-project=fresh] .pb-toggle');
  assert.equal(rowIds(w, q(w, '[data-project=fresh]')).length, 1);
  toggle().click();
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:home:collapsed')), ['older', 'fresh'], 'older stays collapsed beside it');
  assert.deepEqual(rowIds(w, q(w, '[data-project=fresh]')), [], 'a collapsed block draws no rows');
  assert.ok(q(w, '[data-project=fresh]').classList.contains('collapsed'));
  assert.equal(toggle().getAttribute('aria-expanded'), 'false');
  assert.equal(text(q(w, '[data-project=fresh] .pb-count')), '1 session');
  mount('#/agents'); mount('#/');
  assert.deepEqual(rowIds(w, q(w, '[data-project=fresh]')), [], 'still collapsed after coming back');
  toggle().click();
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:home:collapsed')), ['older']);
  assert.equal(rowIds(w, q(w, '[data-project=fresh]')).length, 1);
});

test('a broken stored collapsed set is read as the default (older collapsed)', () => {
  for (const bad of ['{not json', '"older"', '42', 'null']) {
    const { w, mount } = home({ state: agedFleet() });
    w.localStorage.setItem('ccboard:home:collapsed', bad);
    mount();
    assert.equal(q(w, '[data-block="older"]').classList.contains('collapsed'), true, bad);
  }
});

test('collapsing a block with an open tail drops the row and its subscription', () => {
  const { w, mount } = home();
  mount();
  const row = q(w, `[data-block="p:petroit"] [data-tmux="${P3}"]`);
  toggleTail(w, row);
  assert.deepEqual(plain(w.get('__live')).subscribed, [P3]);
  q(w, '[data-block="p:petroit"] .pb-toggle').click();
  assert.deepEqual(plain(w.get('__live')).unsubscribed, [P3], 'the row left the document: its tail let go of the stream');
});

// ---------------------------------------------------------------- rows keep their nodes

test('a poll keeps every row node, reorders them in place and patches only what changed', () => {
  const { w, mount } = home();
  mount();
  const rows = () => Object.fromEntries(qa(w, '.rrow').map((n) => [n.getAttribute('data-tmux'), n]));
  const before = rows();
  const sigs = Object.fromEntries(Object.entries(before).map(([k, n]) => [k, n._sig]));
  assert.ok(Object.values(sigs).every(Boolean), 'every row carries a signature');
  setState(w, fixtureState());                                              // the same fleet again (new object, fresh timestamps of the same minutes)
  const same = rows();
  for (const k of Object.keys(before)) assert.equal(same[k], before[k], `${k}: the same node`);
  // one session changes: only its row repaints
  const st = fixtureState();
  const p1 = st.projects[1].repos[0].sessions.find((s) => s.name === 's1');
  p1.stats.cost_usd = 120; p1.last_message = 'Merged.'; p1.state_at = ISO(0);
  setState(w, st);
  const after = rows();
  for (const k of Object.keys(before)) assert.equal(after[k], before[k], `${k}: still the same node`);
  assert.notEqual(after[P1]._sig, sigs[P1]);
  assert.ok(after[P1].querySelector('.bdg-cost').classList.contains('cost-bad'));
  assert.match(text(after[P1].querySelector('.rr-msg')), /Merged\./);
  assert.equal(after[WT]._sig, sigs[WT], 'an untouched row is not repainted');
});

test('rows reorder when states swap, and the row that holds focus is neither moved nor recreated', () => {
  const { w, mount } = home();
  mount();
  const block = () => q(w, '[data-block="p:ccboard"]');
  assert.deepEqual(rowIds(w, block()), [CX, S1]);
  const cx = block().querySelector(`[data-tmux="${CX}"]`);
  const s1 = block().querySelector(`[data-tmux="${S1}"]`);
  const chip = cx.querySelector('.chip-btn');
  chip.focus();
  const st = fixtureState();
  const [a, b] = st.projects[0].repos[0].sessions;                           // s1, cx1
  Object.assign(a, { state: 'working', state_at: ISO(0) });
  Object.assign(b, { state: 'idle', state_at: ISO(60) });
  setState(w, st);
  setState(w, st);
  assert.deepEqual(rowIds(w, block()), [S1, CX], 'working first now');
  assert.equal(block().querySelector(`[data-tmux="${CX}"]`), cx);
  assert.equal(block().querySelector(`[data-tmux="${S1}"]`), s1);
  assert.ok(cx.contains(w.document.activeElement) && cx.isConnected, 'the focused chip is still in the document');
  assert.equal(chip.parentNode.parentNode.parentNode, cx, 'the chip itself was never recreated');
});

test('a draft in a row\'s send box survives polls', () => {
  const { w, mount } = home();
  mount();
  const row = q(w, `[data-tmux="${P3}"].rrow`);
  const ta = row.querySelector('textarea.composer');
  ta.value = 'half a thought';
  for (let i = 0; i < 3; i++) { const st = fixtureState(); st.projects[1].repos[0].sessions[2].last_message = 'asks again ' + i + '?'; setState(w, st); }
  assert.equal(q(w, `[data-tmux="${P3}"].rrow`).querySelector('textarea.composer'), ta);
  assert.equal(ta.value, 'half a thought');
});

test('a session that ends leaves its block; a new one appears; an emptied block shows no rows', () => {
  const { w, mount } = home();
  mount();
  const st = fixtureState();
  st.projects[0].repos[0].sessions = [sess('ccboard', 'ccboard', 'new', { state: 'working', state_at: ISO(0) })];
  setState(w, st);
  assert.deepEqual(rowIds(w, q(w, '[data-block="p:ccboard"]')), ['ccboard--ccboard--new']);
  assert.equal(rowIds(w).includes(S1), false);
  st.projects[0].repos[0].sessions = [];
  setState(w, st);
  assert.ok(blockIds(w).includes('older'), 'with no sessions ccboard folds into older');
  assert.equal(blockIds(w).includes('p:ccboard'), false);
});

// ---------------------------------------------------------------- inbox, schedules, usage card, empty states

test('the inbox section comes first, only when something needs you: the cards, a count and a way to the whole list', () => {
  const { w, mount } = home();
  mount();
  const sec = q(w, '#inbox-home');
  assert.ok(sec.classList.contains('home-inbox'));
  assert.equal(sec.classList.contains('hidden'), false);
  assert.deepEqual(sec.querySelectorAll('.inbox-card').map((c) => c.getAttribute('data-tmux')), [CK, P3, S1, P2, P1]);
  assert.equal(text(sec.querySelector('.ib-title')), 'Needs you (5)');
  const link = sec.querySelector('a.ib-more');
  assert.ok(link && link.getAttribute('href') === '#/inbox' && !link.classList.contains('hidden'), 'a way to the whole list');
  const kids = page(w).children.map((n) => n.className.split(' ')[0]);
  assert.ok(kids.indexOf('home-inbox') < kids.indexOf('pblocks'), 'before the project blocks');
  const st = fakeState({ projects: projectsOf({ shop: { api: [sess('shop', 'api', 's1')] } }) });
  setState(w, st);
  assert.equal(sec.classList.contains('hidden'), true, 'nothing needs you: no section');
});

test('the inbox section shows at most 8 cards and counts the rest', () => {
  const many = Array.from({ length: 11 }, (_, i) => sess('shop', 'api', 'q' + i, { state: 'waiting', needs_attention: true, last_message: `Question ${i}?`, state_at: ISO(30 - i) }));
  const { w, mount } = home({ state: fakeState({ projects: projectsOf({ shop: { api: many } }) }) });
  mount();
  const sec = q(w, '#inbox-home');
  assert.equal(sec.querySelectorAll('.inbox-card').length, 8);
  assert.equal(text(sec.querySelector('.ib-title')), 'Needs you (11)', 'the count is of all of them');
  const all = sec.querySelector('a.ib-more');
  assert.ok(all && !all.classList.contains('hidden') && text(all) === 'Show all 11' && all.getAttribute('href') === '#/inbox');
});

test('the schedules strip lists the next three enabled runs soonest first with the agent glyph, and hides without schedules', () => {
  const { w, mount } = home();
  mount();
  const strip = q(w, '.sched');
  assert.equal(strip.classList.contains('hidden'), false);
  const items = strip.querySelectorAll('.sched-item');
  assert.deepEqual(items.map((n) => text(n.querySelector('.sched-name'))), ['Prune stale worktrees', 'Nightly dependency audit', 'Weekly changelog digest']);
  assert.ok(items.every((n) => n.querySelector('.glyph.agent')));
  assert.match(text(items[0].querySelector('time.until')), /^\d+[hm]/, 'a countdown');
  assert.ok(Number(items[0].querySelector('time.until').getAttribute('data-epoch')) > Date.now() / 1000);
  assert.equal(text(strip.querySelector('.sched-all')), 'All 4');
  // four enabled: still three; none upcoming: a note; none at all: hidden
  const st = fixtureState();
  st.jobs.push({ id: 9, project: 'a', repo: 'b', name: 'Late job', cron: '* * * * *', enabled: 1, next_run_at: new Date(Date.now() + 90 * 3600e3).toISOString(), agent: 'claude' });
  setState(w, st);
  assert.equal(strip.querySelectorAll('.sched-item').length, 3);
  setState(w, fixtureState({ jobs: [{ id: 1, project: 'a', repo: 'b', name: 'Off', enabled: 0, next_run_at: null }] }));
  assert.equal(strip.classList.contains('hidden'), false);
  assert.match(text(strip), /no upcoming runs \(1 schedule\)/);
  setState(w, fixtureState({ jobs: [] }));
  assert.equal(strip.classList.contains('hidden'), true);
});

test('the schedules strip shows ◇ for a Codex job and ◆ for a Claude one, and repaints when only the agent changes', () => {
  const { w, mount } = home();
  mount();
  const at = (h) => new Date(Date.now() + h * 3600e3).toISOString();
  const jobs = [{ id: 1, project: 'a', repo: 'b', name: 'Claude one', cron: '* * * * *', enabled: 1, next_run_at: at(1), agent: 'claude' },
    { id: 2, project: 'a', repo: 'b', name: 'Codex one', cron: '* * * * *', enabled: 1, next_run_at: at(2), agent: 'codex', opts: { model: 'gpt-5.5' } }];
  setState(w, fixtureState({ jobs }));
  const glyphs = () => q(w, '.sched').querySelectorAll('.sched-item').map((n) => `${text(n.querySelector('.glyph.agent'))} ${text(n.querySelector('.sched-name'))}`);
  assert.deepEqual(glyphs(), ['◆ Claude one', '◇ Codex one']);
  setState(w, fixtureState({ jobs: [{ ...jobs[0], agent: 'codex' }, jobs[1]] }));
  assert.deepEqual(glyphs(), ['◇ Claude one', '◇ Codex one'], 'the agent is part of what the strip repaints on');
});

test('the age and countdown texts tick without a re-render', () => {
  const { w, mount } = home();
  mount();
  const age = q(w, `[data-tmux="${P3}"].rrow time.age`);
  const until = q(w, '.sched time.until');
  age.setAttribute('data-epoch', String(Math.floor(Date.now() / 1000) - 7200));
  until.setAttribute('data-epoch', String(Math.floor(Date.now() / 1000) + 5 * 3600 + 120));
  w.run('agentsTick()');
  assert.equal(text(age), '2h');
  assert.match(text(until), /^5h/);
});

test('with no projects Home offers Add project, which opens the new-project sheet through Shell.openCreate', () => {
  const { w, mount } = home({ state: fakeState({ projects: [] }) });
  mount();
  const empty = qa(w, '.nonideal').find((n) => /No projects yet/.test(text(n)));
  assert.ok(empty && !empty.classList.contains('hidden'));
  const btn = empty.querySelectorAll('button').find((b) => text(b) === 'Add project');
  assert.ok(btn);
  btn.click();
  assert.deepEqual(plain(w.get('__created')), ['project']);
  assert.equal(q(w, '.pblocks').classList.contains('hidden'), true);
  setState(w, fixtureState());
  assert.equal(empty.classList.contains('hidden'), true, 'a project makes it go away');
});

test('projects but no sessions: no empty blocks, just the collapsed older block with the quiet projects', () => {
  const { w, mount } = home({ state: fakeState({ projects: projectsOf({ shop: { api: [] }, blog: { web: [] } }) }) });
  mount();
  assert.deepEqual(blockIds(w), ['older']);
  assert.equal(text(q(w, '.pb-older-toggle')), '▸ older (2)');
  assert.equal(rowIds(w).length, 0);
  assert.equal(q(w, '.home-note').classList.contains('hidden'), true);
  assert.equal(qa(w, '.nonideal').find((n) => /No projects yet/.test(text(n))).classList.contains('hidden'), true, 'there are projects: no onboarding state');
});

test('the usage card is built into Home, stays hidden until a fetch returns data, and is fetched on mount, never by the poll', async () => {
  const { w, mount } = home();
  const series = { since: 'x', until: 'y', step: 1800, t: [1, 1801, 3601], series: { 'rl_5h:claude': [10, 42, 50] }, meta: {} };
  const summary = { daily: Array.from({ length: 7 }, (_, i) => ({ day: `2026-10-0${i + 1}`, total: i === 2 ? 0 : 5 + i, zero: i === 2 })) };
  w.ctx.__answers = { '/api/series': series, '/api/usage/summary': summary };
  mount();
  assert.ok(q(w, '#usage-home .ucard'), 'Widgets.usageCard(host) built into #usage-home');
  assert.equal(q(w, '.ucard').classList.contains('hidden'), true, 'hidden before the first fetch returned');
  await tick(); await tick();
  assert.equal(q(w, '.ucard').classList.contains('hidden'), false);
  assert.ok(q(w, '.ucard svg.spark polyline'), 'the 24 h sparkline');
  assert.equal(qa(w, '.ucard svg.cbars rect.cb').length, 7, 'one bar per day');
  assert.equal(qa(w, '.ucard svg.cbars rect.cb.zero').length, 1, 'the zero day is hatched');
  assert.match(w.get('__calls').map((c) => c.path).find((p) => p.startsWith('/api/series')), /series=rl_5h&key=claude&since=24h/);
  assert.match(w.get('__calls').map((c) => c.path).find((p) => p.startsWith('/api/usage/summary')), /days=7&tz_min=-?\d+/);
  const n = () => plain(w.get('__calls')).filter((c) => c.path.startsWith('/api/series') || c.path.startsWith('/api/usage/summary')).length;
  const base = n();
  for (let i = 0; i < 5; i++) setState(w, fixtureState());
  await tick();
  assert.equal(n(), base, 'five state polls fetched nothing');
  assert.equal(q(w, '.ucard .gauge').classList.contains('hidden'), true, 'no usage record in the state: no gauge');
  setState(w, fixtureState({ usage: { value: { five_hour: { used_percentage: 42, resets_at: EPOCH(-120) }, seven_day: { used_percentage: 71, resets_at: EPOCH(-3000) } }, at: ISO(1) } }));
  const gauges = qa(w, '.ucard .gauge').filter((g) => !g.classList.contains('hidden'));
  assert.deepEqual(gauges.map((g) => text(g.querySelector('.g-label')) + ' ' + text(g.querySelector('.g-val'))), ['5H 42%', '7D 71%']);
});

test('the usage card with nothing to show stays hidden, errors are swallowed, and leaving Home destroys the card', async () => {
  const { w, mount } = home();
  w.ctx.__answers = { '/api/series': () => { throw new Error('boom'); }, '/api/usage/summary': () => { throw new Error('boom'); } };
  mount();
  await tick(); await tick();
  assert.equal(q(w, '.ucard').classList.contains('hidden'), true);
  assert.deepEqual(plain(w.get('__toasts')), [], 'quietly');
  const card = q(w, '.ucard');
  mount('#/agents');
  assert.equal(card.isConnected, false);
});

// ---------------------------------------------------------------- the keyboard selection on Home

test('on Home j and k walk the inbox cards first, then the rows of the open blocks, each session once', () => {
  const { w, mount } = home();
  mount();
  const items = () => plain(w.run('Pages.items().map((s) => s.tmux)'));
  assert.deepEqual(items(), [CK, P3, S1, P2, P1, ROOT, WT, CX], 'five inbox sessions, then the rest in block order (phasezero, petroit, ccboard), nobody twice');
  assert.equal(w.run('Pages.active()'), true);
  w.run('Pages.select(1)');
  assert.equal(w.run('Pages.selected().tmux'), CK);
  const lit = qa(w, '.sel').map((n) => n.getAttribute('data-tmux'));
  assert.equal(lit.length, 2, 'the inbox card and the project row of the same session both light up');
  assert.deepEqual([...new Set(lit)], [CK]);
  w.run('Pages.select(1)');
  assert.equal(w.run('Pages.selected().tmux'), P3);
  w.run('Pages.select(-1); Pages.select(-1)');
  assert.equal(w.run('Pages.selected().tmux'), CK, 'k stops at the first');
  seg(w, 'working').click();
  assert.deepEqual(items(), [WT, CX], 'with a filter on the inbox is not part of the walk');
});

test('the o key (Pages.act("term")) opens the selected session through Shell.openTerm: the same surface as a click on its Open link (the dock from 1024 px, else the page)', async () => {
  const { w, mount } = home();
  mount();
  w.run('Pages.select(1)');
  w.run('globalThis.__opened = []; globalThis.__terms = []; openPage = (u) => __opened.push(u);');
  assert.equal(await w.run('Pages.act("term")'), true);
  assert.deepEqual(plain(w.get('__opened')), [`/term/${CK}`], 'without a Shell (or an older one) it is the terminal page as before');
  w.run('globalThis.Shell = { openTerm: (t) => { __terms.push(t); return true; } }');
  assert.equal(await w.run('Pages.act("term")'), true);
  assert.deepEqual(plain(w.get('__terms')), [CK]);
  assert.deepEqual(plain(w.get('__opened')), [`/term/${CK}`], 'no second tab: the dock took it');
});

test('on Home a collapsed block is skipped by the walk, and a click on a row selects it', () => {
  const { w, mount } = home();
  mount();
  q(w, '[data-block="p:phasezero"] .pb-toggle').click();
  const items = plain(w.run('Pages.items().map((s) => s.tmux)'));
  assert.deepEqual(items, [CK, P3, S1, P2, P1, CX], 'the closed phasezero block drops its two rows that are not in the inbox (the checkout session is there as a card)');
  q(w, `[data-block="p:ccboard"] [data-tmux="${CX}"]`).click();
  assert.equal(w.run('Pages.selected().tmux'), CX);
  assert.ok(q(w, `[data-block="p:ccboard"] [data-tmux="${CX}"]`).classList.contains('sel'));
});

// ---------------------------------------------------------------- the away strip

/** since = seconds ago the board was last seen; returns the strip. `at` is the world's clock (epoch ms) when it is stubbed. */
function awayWorld(sinceAgoS, { state = fixtureState(), answers = null } = {}) {
  const h = home({ state });
  if (sinceAgoS !== null) h.w.localStorage.setItem('ccboard:seen', String(Math.floor(Date.now() / 1000) - sinceAgoS));
  if (answers) h.w.ctx.__answers = answers;
  h.mount();
  return { ...h, strip: () => page(h.w).querySelector('.away') };
}
const awayLinks = (strip) => strip.querySelectorAll('a.away-n').map((a) => [text(a), a.getAttribute('href')]);

test('the away strip says what happened since the board was last seen, each count a link', () => {
  const { strip } = awayWorld(4 * 3600);
  const s = strip();
  assert.equal(s.classList.contains('hidden'), false);
  assert.match(text(s.querySelector('.away-text')), /^while you were away \(since (\w{3} )?\d{2}:\d{2}\)$/);   // 'Sat 20:05' when the since fell on another day (homeSinceLabel)
  assert.deepEqual(awayLinks(s), [
    ['1 done', '#/?f=done'], ['2 need you', '#/?f=waiting'], ['1 error', '#/?f=errored'], ['1 blocked', '#/agents'],
  ]);
  assert.match(text(s), /while you were away/);
});

test('the away strip never shows for a gap under ten minutes, a first visit, or when nothing happened', () => {
  assert.equal(awayWorld(9 * 60 + 30).strip().classList.contains('hidden'), true, '9.5 minutes');
  assert.equal(awayWorld(11 * 60).strip().classList.contains('hidden'), false, '11 minutes');
  assert.equal(awayWorld(null).strip().classList.contains('hidden'), true, 'no ccboard:seen yet: a first visit');
  const quiet = fakeState({ projects: projectsOf({ shop: { api: [sess('shop', 'api', 's1', { state: 'idle', state_at: ISO(60 * 24) }), sess('shop', 'api', 's2', { state: 'working', state_at: ISO(60 * 24) })] } }) });
  assert.equal(awayWorld(4 * 3600, { state: quiet }).strip().classList.contains('hidden'), true, 'nothing happened after since');
});

test('the away strip has a dismiss button and stays dismissed across polls', () => {
  const { w, strip } = awayWorld(4 * 3600);
  const x = strip().querySelector('button.away-x');
  assert.ok(x && x.getAttribute('aria-label') === 'Dismiss');
  x.click();
  assert.equal(strip().classList.contains('hidden'), true);
  setState(w, fixtureState());
  assert.equal(strip().classList.contains('hidden'), true, 'a poll does not bring it back');
});

test('the away strip adds PRs, scheduled runs and limit episodes: one fetch of the episodes at mount, errors swallowed', async () => {
  const sinceAgo = 4 * 3600;
  const ev = (agoS, resets) => ({ t: Math.floor(Date.now() / 1000) - agoS, key: '5h', v: 1, m: { session: P2, resets_at: resets } });
  const st = fixtureState();
  st.tasks.push({ id: 8, tmux: 'x--y--z', title: 'pr task', project: 'shop', repo: 'api', column: 'pr', pr_number: 9, updated: ISO(60) });
  st.jobs[0].last_run_at = ISO(30);
  const before = Date.now() / 1000;       // the last-seen time is stamped while the world is built: bracket that moment, a loaded machine can take seconds afterwards
  const { w, strip } = awayWorld(sinceAgo, { state: st, answers: { '/api/series/events': { events: [ev(7200, 111), ev(7100, 111), ev(60, 222)], truncated: false } } });
  const after = Date.now() / 1000;
  await tick(); await tick();
  const links = awayLinks(strip());
  assert.deepEqual(links.map((l) => l[0]), ['1 done', '2 need you', '1 error', '2 limit hits', '1 PR', '1 scheduled run', '1 blocked']);
  assert.deepEqual(Object.fromEntries(links.map(([t, h]) => [t.replace(/^\d+ /, ''), h])), {
    done: '#/?f=done', 'need you': '#/?f=waiting', error: '#/?f=errored', 'limit hits': '#/usage', PR: '#/tasks', 'scheduled run': '#/tasks', blocked: '#/agents' });
  const fetches = plain(w.get('__calls')).filter((c) => c.path.startsWith('/api/series/events'));
  assert.equal(fetches.length, 1, 'one fetch, at mount');
  const url = new URL('http://x' + fetches[0].path);
  assert.equal(url.searchParams.get('series'), 'lim');
  const since = Date.parse(url.searchParams.get('since')) / 1000;
  assert.ok(since >= before - sinceAgo - 2 && since <= after - sinceAgo + 2, 'since is the last seen time, as ISO');
  for (let i = 0; i < 3; i++) setState(w, st);
  assert.equal(plain(w.get('__calls')).filter((c) => c.path.startsWith('/api/series/events')).length, 1, 'polls never refetch');
  // an error leaves the strip without the episodes
  const bad = awayWorld(sinceAgo, { answers: { '/api/series/events': () => { throw new Error('nope'); } } });
  await tick(); await tick();
  assert.deepEqual(awayLinks(bad.strip()).map((l) => l[0]), ['1 done', '2 need you', '1 error', '1 blocked']);
});

test('no fetch of limit episodes when the strip cannot show (a short gap or a first visit)', async () => {
  for (const ago of [120, null]) {
    const { w } = awayWorld(ago);
    await tick();
    assert.equal(plain(w.get('__calls')).filter((c) => c.path.startsWith('/api/series/events')).length, 0, String(ago));
  }
});

test('ccboard:seen is written on the first render and at most every 30 s while the tab is visible; a hidden tab writes it at once', () => {
  const { w, mount } = home();
  w.ctx.__now = 1_800_000_000_000;
  w.run('Date.now = () => __now');
  const seen = () => w.localStorage.getItem('ccboard:seen');
  assert.equal(seen(), null);
  mount();
  assert.equal(seen(), '1800000000', 'the first render records the visit (epoch seconds)');
  w.ctx.__now += 10_000; setState(w, fixtureState());
  assert.equal(seen(), '1800000000', '10 s later: not rewritten');
  w.ctx.__now += 25_000; setState(w, fixtureState());
  assert.equal(seen(), '1800000035', '35 s after the first write: rewritten');
  w.ctx.__now += 5_000;
  w.document.hidden = true;
  w.document.dispatch('visibilitychange');
  assert.equal(seen(), '1800000040', 'hiding the tab saves the time at once');
  w.ctx.__now += 120_000; setState(w, fixtureState());
  assert.equal(seen(), '1800000040', 'a hidden tab is not "looking": no write on a background poll');
  w.document.hidden = false;
  setState(w, fixtureState());
  assert.equal(seen(), '1800000160');
});

test('since is captured once at mount, before this visit writes ccboard:seen; the strip keeps its since while the page is open', () => {
  const ago = 4 * 3600;
  const { w, mount, strip } = awayWorld(ago);
  const before = text(strip().querySelector('.away-text'));
  for (let i = 0; i < 3; i++) setState(w, fixtureState());
  assert.equal(text(strip().querySelector('.away-text')), before);
  assert.ok(Number(w.localStorage.getItem('ccboard:seen')) > Date.now() / 1000 - 5, 'the new visit was recorded');
  mount('#/agents'); mount('#/');
  assert.equal(strip().classList.contains('hidden'), false, 'returning to Home within the same document keeps the same strip');
});

// ---------------------------------------------------------------- the banner, the install hint, unmount

test('renderBanner shows one message at a time and leaves a callout somebody else keeps in #banner alone', () => {
  const { w, mount } = home();
  mount();
  const banner = () => w.document.querySelector('#banner');
  const foreign = w.document.createElement('div');
  foreign.className = 'callout warn limit-callout';
  banner().append(foreign);
  w.run('ui.error = "boom"; renderBanner()');
  assert.deepEqual(banner().children.map((n) => n.tagName), ['DIV', 'SPAN', 'BUTTON'], 'the foreign callout, the message and its dismiss button');
  assert.equal(banner().children[0], foreign);
  assert.equal(text(banner().querySelector('span')), 'boom');
  w.run('ui.error = null; renderBanner()');
  assert.deepEqual(banner().children, [foreign], 'its own nodes went, the foreign one stayed');
  w.run('state = { ...state, claude: { installed: true, loggedIn: false } }; renderBanner()');
  assert.match(text(banner()), /not logged in/);
  assert.ok(banner().querySelectorAll('button').some((b) => text(b) === 'Log in'));
});

test('renderBanner leaves the rate-limit callout to Widgets.limitBanner once it exists (no second message)', () => {
  const { w, mount } = home();
  mount();
  w.run('renderBanner()');
  assert.doesNotMatch(text(w.document.querySelector('#banner')), /Rate limited/);
  w.run('Widgets.limitBanner(state)');
  assert.match(text(w.document.querySelector('#banner')), /Claude rate limit \(5h\) · resets \d{2}:\d{2} · s2/);
  w.run('renderBanner()');
  assert.match(text(w.document.querySelector('#banner')), /Claude rate limit \(5h\)/, 'a repaint keeps it');
});

test('homeInstallHint: a callout in a browser tab until dismissed, never in the installed app', () => {
  const { w } = home();
  const hint = () => w.run('homeInstallHint()');
  assert.ok(hint(), 'shown in a browser tab');
  assert.ok(hint().classList.contains('install-hint'));
  assert.ok(hint().querySelector('a[href="#/settings?sec=app"]'));
  w.localStorage.setItem('ccboard:hint:install', '1');
  assert.equal(hint(), null, 'dismissed once, gone');
  w.localStorage.removeItem('ccboard:hint:install');
  w.run('window.navigator.standalone = true');
  assert.equal(hint(), null, 'standalone');
});

test('leaving Home stops the ticker, drops tail subscriptions and destroys the usage card; coming back mounts afresh', () => {
  const { w, mount, mounts } = home();
  mount();
  assert.notEqual(w.get('agentsTicker.timer'), null);
  toggleTail(w, q(w, `[data-block="p:petroit"] [data-tmux="${P3}"]`));
  assert.deepEqual(plain(w.get('__live')).subscribed, [P3]);
  mount('#/agents');
  assert.deepEqual(plain(w.get('__live')).unsubscribed, [P3], 'the stream is let go of on unmount');
  assert.equal(w.get('agentsTicker.n'), 1, 'only the roster ticks now');
  mount('#/tasks');
  assert.equal(w.get('agentsTicker.timer'), null, 'no interval leaks past the pages');
  mount('#/');
  assert.equal(mounts(), 2);
});

// ---------------------------------------------------------------- the demo fixture draws every case

/** A Home world over app/static/demo/state.json rebased to now the way api() does in demo mode (core.js demoRebase). */
function demoHome() {
  const h = home({ state: fakeState() });
  h.w.ctx.__demo = fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8');
  h.w.run('state = demoRebase(JSON.parse(__demo)); __st = state');
  return h;
}

test('?demo=1: the fixture shows every inbox kind, every chip and the limit banner on Home', () => {
  const { w, mount } = demoHome();
  mount();
  const kinds = qa(w, '#inbox-home .inbox-card').map((c) => [c.getAttribute('data-tmux').split('--').slice(1).join('/'), text(c.querySelector('.ib-kind'))]);
  assert.deepEqual(kinds, [['website/t-checkout-redesign', 'permission'], ['api/s3', 'question'], ['ccboard/s1', 'job needs you'], ['api/s2', 'limit hit'], ['api/s1', 'done']]);
  const chip = (tmux, cls) => q(w, `.pblocks .rrow[data-tmux="${tmux}"] .${cls}`);
  const on = (n) => !!n && !n.classList.contains('hidden');
  assert.ok(chip(S1, 'bdg-cost').classList.contains('cost-warn') && text(chip(S1, 'bdg-cost')) === '$12.50');
  assert.ok(chip(ROOT, 'bdg-cost').classList.contains('cost-bad') && text(chip(ROOT, 'bdg-cost')) === '$140');
  assert.ok(chip(CK, 'ctx').classList.contains('ctx-hi') && on(chip(CK, 'compact-chip')), 'the checkout session is at 61 %');
  assert.ok(chip(WT, 'ctx').classList.contains('ctx-crit') && on(chip(WT, 'compact-chip')), 'stock-sync is at 88 %');
  assert.ok(on(chip(WT, 'bdg-wt')) && on(chip(CK, 'bdg-wt')), 'two worktree sessions');
  assert.ok(on(chip(S1, 'bdg-sub')) && text(chip(S1, 'bdg-sub')) === '2 subagents');
  assert.ok(on(chip(S1, 'bdg-blocked')));
  assert.ok(on(chip(P2, 'bdg-limit')), 'the limit chip sits on the rate-limited session');
  assert.equal(qa(w, '.pblocks .bdg-limit').filter(on).length, 1);
  assert.equal(qa(w, '.sched-item').length, 3, 'the schedules strip has its three runs');
  // working 1, idle 2: the Codex demo session is idle with 'no hooks (untrusted?)' (#96)
  assert.deepEqual(['waiting', 'working', 'idle', 'done', 'errored'].map((f) => text(seg(w, f).querySelector('.sum-n'))), ['2', '1', '2', '1', '1']);
  assert.equal(qa(w, `[data-tmux="${CX}"] a.bdg-hooks`).length >= 1, true, 'the demo shows the chip on the Codex row');
  assert.equal(text(q(w, '.pb-older-toggle')), '▸ older (6)', 'six quiet projects');
  w.run('Widgets.limitBanner(state)');
  assert.match(text(w.document.querySelector('#banner')), /Claude rate limit \(5h\) · resets \d{2}:\d{2} · s2/);
});

test('?demo=1: the fixture feeds Inbox.kind every kind it has a card for, in the documented order', () => {
  const { w, mount } = demoHome();
  mount();
  const items = plain(w.run('Inbox.items(state).map((s) => [s.tmux, s.kind])'));
  assert.deepEqual(items.map((x) => x[1]), ['permission', 'question', 'needs', 'limit', 'done']);
  assert.deepEqual(items.map((x) => x[0]), [CK, P3, S1, P2, P1]);
  const everyone = plain(w.run('rosterSessions(state).map((s) => [s.tmux, Inbox.kind(s, state)])'));
  assert.equal(everyone.find((x) => x[0] === ROOT)[1], '', 'the ended session is not asking for anything');
  assert.equal(everyone.find((x) => x[0] === CX)[1], '', 'nor the working Codex session');
});

// ---------------------------------------------------------------- v0.5.17b: the account chip on a rich row, chipHue('account', key)

const ACCT_A = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001', ACCT_B = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a002';
const acct = (key, over = {}) => ({ key, email: `${key.slice(-4)}@example.com`, name: null, label: null, plan: 'max', rl_5h: 10, rl_7d: 20, resets_5h: EPOCH(-120), resets_7d: EPOCH(-4000), current: false, ...over });
const TWO = (over = {}) => ({ current: ACCT_A, list: [acct(ACCT_A, { name: 'Demo', current: true }), acct(ACCT_B, { name: 'Work', label: 'Work', plan: 'pro' })], ...over });
const acctState = (accounts, s) => fakeState({ projects: projectsOf({ shop: { api: [s] } }), accounts });
const acctChip = (row) => bdg(row, 'bdg-acct');

test('chipHue(account, key): one stable hue per key from the project pool (never amber or rose), the same hash as a project name', () => {
  const { w } = homeWorld();
  const hue = (kind, key) => w.run(`chipHue(${JSON.stringify(kind)}, ${JSON.stringify(key)})`);
  const POOL = ['hue-blue', 'hue-teal', 'hue-green', 'hue-violet', 'hue-slate'];
  const seen = new Set();
  for (const key of [ACCT_A, ACCT_B, 'a001', 'a002', 'unknown', 'k1', 'k2', 'k3', 'k4', 'k5', 'k6']) {
    const h = hue('account', key);
    assert.ok(POOL.includes(h), `${key} -> ${h}`);
    assert.equal(hue('account', key), h, 'stable');
    assert.equal(h, hue('project', key), 'the same stable hash as a project name');
    seen.add(h);
  }
  assert.ok(seen.size >= 3, 'the keys spread over the pool');
  assert.equal(hue('account', ''), hue('account', null), 'no key is still a hue, not a throw');
});

test('the account chip shows on a Claude row only when the board has more than one account', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { account: ACCT_B });
  const row = rowFor(w, s, acctState(TWO(), s));
  assert.ok(shown(acctChip(row)));
  assert.equal(text(acctChip(row)), 'Work');
  assert.ok(acctChip(row).classList.contains(w.run(`chipHue('account', ${JSON.stringify(ACCT_B)})`)), 'the account\'s hue');
  assert.match(acctChip(row).getAttribute('title'), /^subscription account: Work · .*@example\.com$/);
  // one account: a single-account board stays uncluttered
  repatch(w, s, acctState({ current: ACCT_A, list: [acct(ACCT_B, { label: 'Work', current: true })] }, s));
  assert.equal(shown(acctChip(row)), false);
  // two again
  repatch(w, s, acctState(TWO(), s));
  assert.ok(shown(acctChip(row)));
});

test('no accounts in the state, or a session without an account (shell, Codex, older record): no chip, no error', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { account: ACCT_A });
  const row = rowFor(w, s, fakeState({ projects: projectsOf({ shop: { api: [s] } }) }));          // fakeState has no `accounts` key at all
  assert.equal(shown(acctChip(row)), false);
  for (const accounts of [null, {}, { current: null, list: [] }, { list: 'nope' }]) {
    repatch(w, s, acctState(accounts, s));
    assert.equal(shown(acctChip(row)), false, JSON.stringify(accounts));
  }
  repatch(w, { ...s, account: null }, acctState(TWO(), s));
  assert.equal(shown(acctChip(row)), false, 'a session that carries no account');
  repatch(w, { ...s, account: undefined, agent: 'codex', launcher: 'codex' }, acctState(TWO(), s));
  assert.equal(shown(acctChip(row)), false, 'a Codex session');
});

test('the chip names the account by label, else the name Claude reports, else the email, else the key; never undefined', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { account: ACCT_A });
  const named = (over) => acctState({ current: ACCT_A, list: [acct(ACCT_A, { current: true, name: null, email: null, ...over }), acct(ACCT_B, { label: 'Work' })] }, s);
  const row = rowFor(w, s, named({ label: 'Personal', name: 'Roshan', email: 'r@example.com' }));
  assert.equal(text(acctChip(row)), 'Personal');
  repatch(w, s, named({ name: 'Roshan', email: 'r@example.com' }));
  assert.equal(text(acctChip(row)), 'Roshan');
  repatch(w, s, named({ email: 'r@example.com' }));
  assert.equal(text(acctChip(row)), 'r@example.com');
  repatch(w, s, named({}));
  assert.equal(text(acctChip(row)), ACCT_A.slice(0, 6), 'the first characters of the key');
  repatch(w, { ...s, account: 'ffff0000-unknown' }, named({ label: 'Personal' }));
  assert.equal(text(acctChip(row)), 'ffff00', 'an account the list does not know yet');
  for (const t of [text(acctChip(row)), acctChip(row).getAttribute('title')]) assert.ok(!/undefined|null|NaN/.test(t), t);
});

test('a rename repaints the row at once (the label is part of the row\'s signature), and the hue follows the key, not the label', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { account: ACCT_B });
  const row = rowFor(w, s, acctState(TWO(), s));
  const hue = [...['hue-blue', 'hue-teal', 'hue-green', 'hue-violet', 'hue-slate']].filter((c) => acctChip(row).classList.contains(c));
  assert.equal(hue.length, 1, 'exactly one hue class');
  const renamed = TWO();
  renamed.list[1].label = 'Client work';
  repatch(w, s, acctState(renamed, s));
  assert.equal(text(acctChip(row)), 'Client work');
  assert.ok(acctChip(row).classList.contains(hue[0]) && [...acctChip(row).className.split(' ')].filter((c) => c.startsWith('hue-')).length === 1, 'the same single hue after the rename');
  // an unchanged poll writes nothing (the node keeps its text node)
  const t0 = acctChip(row).textContent;
  repatch(w, s, acctState(renamed, s));
  assert.equal(acctChip(row).textContent, t0);
});

test('the account chip sits in the row\'s badges right after the model chip, and the plain (non-rich) row has none', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 's1', { account: ACCT_A });
  const row = rowFor(w, s, acctState(TWO(), s));
  const order = row.querySelector('.rr-badges').children.map((c) => (c.className.match(/bdg-(model|acct)/) || [])[1]).filter(Boolean);
  assert.deepEqual(order, ['model', 'acct']);
  w.ctx.__s = s; w.ctx.__stx = acctState(TWO(), s);
  w.run('state = __stx; globalThis.__plain = sessionCard({ ...__s, project: "shop", repo: "api" }, { compact: true })');
  assert.equal(w.get('__plain').querySelector('.bdg-acct'), null, 'Agents/Inbox plain rows are unchanged');
});

// ---------------------------------------------------------------- v0.5.13: create from anywhere (openLauncher) and the diff sheet (the #modal is gone)

/** openLauncher as a recorder: nothing opens, every call lands in __launched. */
const stubLauncher = (w) => w.run('globalThis.__launched = []; globalThis.openLauncher = (o) => { __launched.push(o); return true; };');
/** What each call carried, as plain values: the mode, the project and repo names, whether the repo is the project folder, the extra keys. */
const launched = (w) => plain(w.run('__launched.map((o) => ({ mode: o.mode, project: o.project && o.project.name, repo: o.repo && o.repo.name, folder: !!(o.project && o.repo && o.repo === o.project.root), keys: Object.keys(o).sort() }))'));
const sheetOf = (w) => w.document.getElementById('sheet');

test('+ session on a project block calls openLauncher({mode: session, project, repo}) with the repo the project worked in last: no sheet of its own, one tap to the launcher', () => {
  const { w, mount } = home();
  mount();
  stubLauncher(w);
  const add = q(w, '[data-block="p:phasezero"] .pb-add');
  assert.equal(text(add), '+ session');
  add.click();
  assert.deepEqual(launched(w), [{ mode: 'session', project: 'phasezero', repo: 'NestJs-Ecommerce-Backend', folder: false, keys: ['mode', 'project', 'repo'] }], 'the repo of the newest session, as the state\'s own objects');
  assert.equal(w.run('__launched[0].project === state.projects.find((p) => p.name === "phasezero")'), true, 'the project is the state object, not a copy or a name');
  assert.equal(sheetOf(w).open, false, 'the page opens nothing itself: the launcher does');
});

test('+ task sits beside + session on a project block and opens the launcher in task mode, in the repo a task went to last, else where the project works', () => {
  const { w, mount } = home();
  mount();
  stubLauncher(w);
  const block = q(w, '[data-block="p:phasezero"]');
  assert.deepEqual(block.querySelectorAll('.pb-head button').filter((b) => /^\+ /.test(text(b))).map(text), ['+ session', '+ task']);
  const addTask = block.querySelector('.pb-add-task');
  assert.equal(text(addTask), '+ task');
  addTask.click();
  assert.deepEqual(launched(w).map((c) => [c.mode, c.project, c.repo]), [['task', 'phasezero', 'NestJs-Ecommerce-Backend']]);
  w.localStorage.setItem('ccboard:task:last:phasezero', 'website');
  addTask.click();
  assert.deepEqual(launched(w).at(-1), { mode: 'task', project: 'phasezero', repo: 'website', folder: false, keys: ['mode', 'project', 'repo'] }, 'the remembered repo beats the busiest one');
  assert.equal(sheetOf(w).open, false);
});

test('a repo group\'s own + opens the launcher for that repo, the project folder\'s for the folder', () => {
  const { w, mount } = home();
  mount();
  stubLauncher(w);
  const groups = q(w, '[data-block="p:phasezero"]').querySelectorAll('.pgroup');
  const addOf = (label) => groups.find((g) => text(g.querySelector('.pg-name')) === label).querySelector('.pg-add');
  addOf('website').click();
  addOf('project folder').click();
  assert.deepEqual(launched(w).map((c) => [c.mode, c.project, c.repo, c.folder]), [['session', 'phasezero', 'website', false], ['session', 'phasezero', 'root', true]]);
});

test('without openLauncher (a deploy before launcher.js has it) + session still opens the old session form in the sheet; + task opens the task form', () => {
  const { w, mount } = home();
  mount();
  w.run('openLauncher = undefined');                       // whatever launcher.js defines: this is the world before it has one
  q(w, '[data-block="p:phasezero"] .pb-add').click();
  assert.equal(sheetOf(w).open, true);
  assert.match(text(sheetOf(w)), /New session/);
  assert.ok(sheetOf(w).querySelector('form'));
  sheetOf(w).close();
  q(w, '[data-block="p:phasezero"] .pb-add-task').click();
  assert.equal(sheetOf(w).open, true);
  assert.match(text(sheetOf(w)), /New task/);
});

const DIFF = { base: 'main', truncated: false, commits: ['a1 add stock sync', 'b2 tests'], files: ['src/stock.ts'], files_uncommitted: ['README.md', 'x.ts'], committed: 'diff --git a/src/stock.ts b/src/stock.ts\n+x', uncommitted: '' };
const TASK = () => ({ id: 5, title: 'Sync variant stock when an order is cancelled', project: 'phasezero', repo: 'NestJs-Ecommerce-Backend', branch: 'ccb/stock-sync', pr_url: null, pr_number: null });

function diffWorld(over = {}) {
  const h = homeWorld();
  const { w } = h;
  w.ctx.__answers['/api/tasks/5/diff'] = DIFF;
  w.ctx.__answers['/api/tasks/5/pr'] = { url: 'https://example.invalid/pull/9', number: 9, existing: false };
  w.ctx.__answers['/api/tasks/5/merge'] = { ok: true };
  w.ctx.__answers['/api/tasks/5/describe'] = { title: 'Sync stock', body: 'Cancelling restocks.' };
  w.run('globalThis.__drawn = []; loadDiff2Html = () => Promise.resolve(); window.Diff2HtmlUI = class { constructor(box, text, o) { this.box = box; this.text = text; __drawn.push({ text, o }); } draw() { this.box.append(document.createElement("div")); } };');
  w.ctx.__t = { ...TASK(), ...over };
  w.run('render = () => {}');
  return h;
}
const sectionsOf = (w) => sheetOf(w).querySelectorAll('.tm-sec').map((s) => text(s.querySelector('.tm-k')));

test('the diff and PR viewer is a sheet, not the legacy #modal: wide, sections with labels, no #modal node, ui.modal never set', async () => {
  const { w } = diffWorld();
  assert.equal(w.document.getElementById('modal'), null, 'index.html has no #modal');
  w.run('openTaskModal(__t)');
  await tick(); await tick();
  const sh = sheetOf(w);
  assert.equal(sh.open, true);
  assert.ok(sh.classList.contains('wide'), 'room for a diff');
  assert.equal(sh.querySelectorAll('.modal-box').length, 0);
  assert.equal(w.run("'modal' in ui"), false, 'the old ui.modal flag is gone with the #modal');
  assert.equal(text(sh.querySelector('.sheet-title')), 'Sync variant stock when an order is cancelled');
  assert.deepEqual(sectionsOf(w), ['Changes', 'Diff', 'Pull request']);
  assert.match(text(sh.querySelector('.tm-meta')), /phasezero\/NestJs-Ecommerce-Backend · ccb\/stock-sync/);
  assert.match(text(sh), /Commits \(2\): a1 add stock sync · b2 tests/);
  assert.match(text(sh), /Files: src\/stock\.ts {2}· {2}uncommitted: README\.md, x\.ts/);
  assert.deepEqual(calls(w).filter((c) => c.path.startsWith('/api/tasks/5')).map((c) => `${c.method} ${c.path}`), ['GET /api/tasks/5/diff']);
  assert.equal(plain(w.get('__drawn')).length, 1, 'the committed side is drawn');
  sh.close();
  assert.equal(sh.classList.contains('wide'), true, 'a later sheet resets it through openSheet');
  w.run('openSheet({ title: "x", body: "y" })');
  assert.equal(sheetOf(w).classList.contains('wide'), false, 'another sheet is not wide');
});

test('the diff sides are one segmented control (aria-pressed, arrows move), committed first; the other side is drawn on a press, and an empty one says so', async () => {
  const { w } = diffWorld();
  w.run('openTaskModal(__t)');
  await tick(); await tick();
  const seg = sheetOf(w).querySelector('.tm-seg');
  const btns = seg.querySelectorAll('.seg-btn');
  assert.deepEqual(btns.map(text), ['Committed vs main', 'Uncommitted (2)']);
  assert.deepEqual(btns.map((b) => b.getAttribute('aria-pressed')), ['true', 'false']);
  btns[1].click();
  await tick();
  assert.deepEqual(btns.map((b) => b.getAttribute('aria-pressed')), ['false', 'true']);
  assert.match(text(sheetOf(w).querySelector('.diffbox')), /no uncommitted changes/);
  btns[1].dispatchEvent({ type: 'keydown', key: 'ArrowLeft', preventDefault() {} });
  await tick();
  assert.deepEqual(btns.map((b) => b.getAttribute('aria-pressed')), ['true', 'false'], 'an arrow key moves the choice');
  assert.equal(plain(w.get('__drawn')).length, 2, 'drawn again for the committed side');
});

test('the pull request section: one filled primary (Create PR), Describe fills the fields, Create PR posts them; Merge is red-outlined, hidden without a PR and two taps with one', async () => {
  const { w } = diffWorld();
  w.run('openTaskModal(__t)');
  await tick(); await tick();
  const sh = sheetOf(w);
  const btn = (re) => sh.querySelectorAll('button').find((b) => re.test(text(b)));
  assert.equal(sh.querySelectorAll('button').filter((b) => b.classList.contains('bp5-intent-primary')).length, 1, 'one filled primary in the sheet');
  assert.equal(text(btn(/Create PR/)), 'Create PR');
  assert.ok(btn(/Create PR/).classList.contains('bp5-intent-primary'));
  assert.ok(sh.querySelector('.tm-merge').classList.contains('hidden'), 'nothing to merge before a PR exists');
  btn(/Describe with Claude/).click();
  await tick();
  const [title, body] = [sh.querySelector('.tm-form input'), sh.querySelector('.tm-form textarea')];
  assert.equal(title.value, 'Sync stock');
  assert.equal(body.value, 'Cancelling restocks.');
  btn(/Create PR/).click();
  await tick(); await tick();
  assert.deepEqual(calls(w).filter((c) => c.path === '/api/tasks/5/pr'), [{ method: 'POST', path: '/api/tasks/5/pr', body: { title: 'Sync stock', body: 'Cancelling restocks.' } }]);
  assert.match(text(sh.querySelector('.tm-status')), /PR created: https:\/\/example\.invalid\/pull\/9/);
  assert.equal(sh.querySelector('.tm-merge').classList.contains('hidden'), false, 'the PR now exists: Merge appears');
  const merge = btn(/^Merge \(squash\)/);
  assert.ok(merge.classList.contains('bp5-intent-danger') && !merge.classList.contains('confirm'), 'red-outlined at rest');
  merge.click();
  assert.deepEqual(calls(w).filter((c) => c.path.endsWith('/merge')), [], 'the first tap only arms it');
  assert.ok(btn(/^Confirm merge$/) && btn(/^Cancel$/));
  btn(/^Cancel$/).click();
  assert.ok(btn(/^Merge \(squash\)/), 'Cancel disarms it');
  btn(/^Merge \(squash\)/).click();
  btn(/^Confirm merge$/).click();
  await tick(); await tick();
  assert.deepEqual(calls(w).filter((c) => c.path.endsWith('/merge')), [{ method: 'POST', path: '/api/tasks/5/merge', body: { method: 'squash', force: false } }]);
  assert.equal(sheetOf(w).open, false, 'merged: the sheet closes');
});

test('a task that already has a PR shows its link and Merge at once, and Update PR in place of Create PR', async () => {
  const { w } = diffWorld({ pr_url: 'https://example.invalid/pull/3', pr_number: 3 });
  w.run('openTaskModal(__t)');
  await tick(); await tick();
  const sh = sheetOf(w);
  assert.equal(text(sh.querySelector('.tm-meta a')), 'PR #3');
  assert.equal(sh.querySelector('.tm-merge').classList.contains('hidden'), false);
  assert.ok(sh.querySelectorAll('button').find((b) => /^Update PR/.test(text(b))));
});

test('a diff that fails to load says why in the status line and leaves the PR section usable', async () => {
  const { w } = diffWorld();
  w.ctx.__answers['/api/tasks/5/diff'] = () => { throw new Error('worktree is gone'); };
  w.run('openTaskModal(__t)');
  await tick(); await tick();
  assert.match(text(sheetOf(w).querySelector('.tm-status')), /worktree is gone/);
  assert.equal(sheetOf(w).querySelector('.tm-seg'), null);
  assert.ok(sheetOf(w).querySelectorAll('button').find((b) => /Create PR/.test(text(b))));
});

test('tap count (real launcher): a new session from a Home project block is 2 taps with the remembered defaults: + session, then Start & open', async () => {
  const { w, mount } = home();
  mount();
  w.ctx.__answers['/api/projects/'] = { tmux: 'phasezero--NestJs-Ecommerce-Backend--s9', attach_url: '/term/phasezero--NestJs-Ecommerce-Backend--s9', agent: 'claude', cmd: 'claude --model opus' };
  let taps = 0;
  const tap = (b) => { taps++; b.click(); };
  tap(q(w, '[data-block="p:phasezero"] .pb-add'));                                  // 1: the sheet opens with the project and the repo chosen
  const sh = sheetOf(w);
  assert.equal(sh.open, true);
  assert.match(text(sh.querySelector('.sheet-title')), /^New session · phasezero\/NestJs-Ecommerce-Backend$/);
  const primaries = sh.querySelectorAll('button').filter((b) => b.classList.contains('bp5-intent-primary'));
  assert.equal(primaries.length, 1, 'one filled primary');
  assert.match(text(primaries[0]), /Start & open/);
  assert.deepEqual(calls(w).filter((c) => c.method === 'POST'), [], 'opening sent nothing');
  assert.equal(primaries[0].getAttribute('type'), 'submit', 'the primary is the form\'s submit button');
  taps++;                                                                             // 2: Start & open (a click on a submit button is the form's submit event)
  sh.querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
  await tick(); await tick();
  const posts = calls(w).filter((c) => c.method === 'POST' && /\/sessions$/.test(c.path));
  assert.equal(posts.length, 1);
  assert.equal(posts[0].path, '/api/projects/phasezero/repos/NestJs-Ecommerce-Backend/sessions');
  assert.equal(taps, 2, 'two taps, nothing typed');
});
