// Contract tests for the Usage page's Refresh (v0.5.17f, part 2) in app/static/pages/usage.js: a quiet bordered button beside the freshness caption, its
// states (Refresh / refreshing… / start a session to refresh), the wait for a newer reading (20 s, a state poll every 1.5 s), the 409 toast, and the once-per-open
// automatic ask when the numbers are stale. Real core.js, components.js, keymap.js, router.js and usage.js on minidom inside the vm harness; api() answers from
// __answers and records every call, setTimeout is a recorder the tests fire by hand (nothing waits), and nothing here starts a clock of its own.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const NOW = Math.floor(Date.now() / 1000);
const ISO = (secAgo) => new Date((NOW - secAgo) * 1000).toISOString();
const tick = async () => { for (let i = 0; i < 4; i++) await new Promise((r) => setImmediate(r)); };
const text = (n) => (n ? n.textContent : '');
const TMUX1 = 'shop--api--s1';

const SUMMARY = { generated_at: ISO(0), tz_min: 345, source: 'samples', windows: { today: { total: 0, by_agent: {}, by_project: [] }, '7d': { total: 0, by_agent: {}, by_project: [] }, '30d': { total: 0, by_agent: {}, by_project: [] } },
  daily: [{ day: '2026-09-20', total: 1, zero: false, by_agent: { claude: 1 }, by_project: { shop: 1 }, tokens: 1000, hours: 1 }, { day: '2026-09-21', total: 2, zero: false, by_agent: { claude: 2 }, by_project: { shop: 2 }, tokens: 1000, hours: 1 }],
  hourly_profile: Array.from({ length: 24 }, () => 0), heatmap: Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => 0)), top_sessions: [], active_hours: {},
  rate_limits: {}, episodes: [], unpriced: [] };
const SERIES = { since: ISO(7 * 86400), until: ISO(0), step: 50000, t: [NOW - 100000, NOW], series: { 'rl_5h:claude': [10, 20], 'rl_7d:claude': [5, 8] }, meta: {} };

const session = (over = {}) => ({ tmux: TMUX1, name: 's1', agent: 'claude', state: 'idle', command: '2.1.288', viewers: { full: 0, grid: 0, ro: 0 }, flags: {}, ...over });
const STATE = (readingAgo = 30, sessions = [session()], extra = {}) => ({
  usage: { value: { five_hour: { used_percentage: 42, resets_at: NOW + 3600 }, seven_day: { used_percentage: 59, resets_at: NOW + 3 * 86400 } }, at: ISO(readingAgo) },
  projects: [{ name: 'shop', path: '/p/shop', root: null, orphan_sessions: [], repos: [{ name: 'api', state: 'ok', sessions }] }],
  pending_permissions: [], ...extra,
});

const CHARTS_STUB = `
globalThis.Charts = {
  ready() { return Promise.resolve(); },
  fetch(p, o) { return api('GET', p); },
  line(host) { host._uplot = true; }, stackedBars(host) { host.append(document.createElement('svg')); },
  gantt(host) { host.append(document.createElement('svg')); }, heatmap(host) { host.append(document.createElement('div')); },
  destroy() {}, destroyAll() {},
  fmtUsd(v) { return '$' + Number(v).toFixed(2); }, fmtTok(n) { return String(n); }, shortModel(m) { return String(m); },
};`;

/** A world with the real scripts and the router mounted on #page. `post` answers POST /api/usage/refresh: a value, or { __error: 'text' }. */
function refreshWorld({ st = STATE(), post = { ok: true, session: TMUX1, started_at: ISO(0) } } = {}) {
  const timers = [];
  const cleared = [];
  const w = makeWorld({
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    setInterval: () => 1, clearInterval: () => {},
    setTimeout: (fn, ms) => { timers.push({ fn, ms, id: timers.length + 1, live: true }); return timers.length; },
    clearTimeout: (id) => { cleared.push(id); const t = timers[id - 1]; if (t) t.live = false; },
  });
  installDom(w);
  for (const f of ['core.js', 'components.js', 'keymap.js', 'router.js']) w.load(f);
  w.ctx.__calls = [];
  w.ctx.__post = post;
  w.ctx.__toasts = [];
  w.ctx.__opened = [];
  w.ctx.__polls = 0;
  w.run(`poll = async (force) => { __polls++; };
    toast = (t, o) => { __toasts.push({ t, o }); return null; };
    globalThis.Shell = { openCreate(kind) { __opened.push(kind); return true; } };
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      if (method === 'POST' && path === '/api/usage/refresh') { if (__post && __post.__error) { const e = new Error(__post.__error); e.status = 409; throw e; } return __post; }
      if (path.startsWith('/api/usage/summary')) return __summary;
      if (path.startsWith('/api/series/events')) return { events: [], truncated: false };
      if (path.startsWith('/api/series')) return __series;
      return {};
    };`);
  w.ctx.__summary = SUMMARY;
  w.ctx.__series = SERIES;
  w.run(CHARTS_STUB);
  w.load('pages/usage.js');
  if (st) { w.ctx.__st = st; w.run('state = __st'); }
  return { w, timers, cleared };
}

const root = (w) => w.document.querySelector('#page');
const q = (w, sel) => root(w).querySelector(sel);
const calls = (w) => plain(w.get('__calls'));
const posts = (w) => calls(w).filter((c) => c.method === 'POST');
const toasts = (w) => plain(w.get('__toasts'));
const btn = (w) => q(w, '[data-sec="limits"] [data-refresh]');
const note = (w) => q(w, '[data-sec="limits"] [data-refresh-note]');
const fresh = (w) => q(w, '[data-sec="limits"] [data-fresh]');
const loading = async (w) => { await w.get('Usage.cur').loading; await tick(); };
const open = async (w, hash = '#/usage') => { w.location.hash = hash; await loading(w); };
const live = (timers, ms) => timers.filter((t) => t.live && t.ms === ms);
const fire = (t) => { t.live = false; t.fn(); };                         // a timer fires once
const push = (w, st) => { w.ctx.__st = st; w.run('state = __st'); w.get('pages').usage.update(st); };

// ---------------------------------------------------------------- the button and its states

test('the Refresh button sits in the caption row under the Limits title: quiet, bordered, small, never a filled primary, no inline style', async () => {
  const { w } = refreshWorld();
  await open(w);
  const b = btn(w);
  assert.ok(b, 'a button with data-refresh in the Limits section');
  assert.equal(b.tagName, 'BUTTON');
  assert.equal(b.getAttribute('type'), 'button');
  const row = b.parentNode;
  assert.ok(row.classList.contains('ufresh-row'));
  assert.deepEqual([...row.children].map((c) => c.className.split(/\s+/).find((x) => /^ufresh/.test(x))), ['ufresh', 'ufresh-btn', 'ufresh-note'], 'caption, button, note');
  assert.equal(row.children[0], fresh(w), 'the caption comes first and stays the section\'s first thing');
  assert.equal(q(w, '[data-sec="limits"] .ubody').children[0], row);
  const cls = b.className.split(/\s+/);
  assert.ok(cls.includes('small') && cls.includes('bp5-button') && cls.includes('bp5-small'), b.className);
  assert.ok(!cls.some((c) => /primary|danger|success|warning/.test(c)), 'no intent colour: a filled primary is not new here: ' + b.className);
  assert.equal(b.getAttribute('style'), null, 'CSP: no style attribute');
  assert.equal(text(b), 'Refresh');
  assert.ok(!b.disabled);
  assert.ok(note(w).classList.contains('hidden'));
  assert.match(b.getAttribute('title'), /Claude Code fetches the official numbers with its own login/);
});

test('with a Claude session on the box the button says Refresh; with none it says "start a session to refresh" and a tap opens the launcher, no request', async () => {
  const { w } = refreshWorld({ st: STATE(30, []) });
  await open(w);
  assert.equal(text(btn(w)), 'start a session to refresh');
  assert.ok(!btn(w).disabled, 'it is a way in, not a dead button');
  btn(w).click();
  await tick();
  assert.deepEqual(plain(w.get('__opened')), ['session'], 'the + session sheet of Shell.openCreate');
  assert.equal(posts(w).length, 0);
  push(w, STATE(30, [session()]));
  assert.equal(text(btn(w)), 'Refresh', 'a session came: the button follows the state');
});

test('a Claude session that is working still reads Refresh (the server decides who is at the prompt); a Codex or shell row or a pane back at its shell is not a live Claude session', async () => {
  const cases = [
    [[session({ state: 'working' })], 'Refresh'],
    [[session({ agent: 'codex', command: 'codex' })], 'start a session to refresh'],
    [[session({ agent: 'shell', command: 'zsh' })], 'start a session to refresh'],
    [[session({ command: '-zsh' })], 'start a session to refresh'],
    [[session({ agent: null, command: 'claude' })], 'start a session to refresh'],
    [[session({ agent: 'codex', command: 'codex' }), session({ tmux: 'shop--api--s2' })], 'Refresh'],
  ];
  for (const [sessions, want] of cases) {
    const { w } = refreshWorld({ st: STATE(30, sessions) });
    await open(w);
    assert.equal(text(btn(w)), want, JSON.stringify(sessions.map((s) => [s.agent, s.state, s.command])));
  }
});

test('refreshTargets reads the state the way the server does: live / ready (at the prompt) / free (nobody attached)', async () => {
  const { w } = refreshWorld();
  const t = (sessions, extra) => plain(w.get('Usage').refreshTargets(STATE(30, sessions, extra)));
  assert.deepEqual(t([]), { live: 0, ready: 0, free: 0 });
  assert.deepEqual(t([session()]), { live: 1, ready: 1, free: 1 });
  for (const state of ['idle', 'done', 'errored']) assert.equal(t([session({ state })]).ready, 1, state);
  assert.equal(t([session({ state: 'waiting', flags: { wait_kind: 'idle' } })]).ready, 1, 'waiting on the idle prompt');
  for (const flags of [{ wait_kind: 'permission' }, { wait_kind: 'elicitation' }, {}]) assert.equal(t([session({ state: 'waiting', flags })]).ready, 0, 'waiting on ' + JSON.stringify(flags));
  assert.equal(t([session({ state: 'working' })]).ready, 0);
  assert.equal(t([session({ state: 'ended' })]).ready, 0);
  assert.equal(t([session({ state: undefined })]).ready, 0, 'no state yet');
  assert.equal(t([session({ flags: { compacting: true } })]).ready, 0);
  assert.equal(t([session()], { pending_permissions: [{ tmux_name: TMUX1 }] }).ready, 0, 'a permission waits for its answer');
  assert.deepEqual(t([session({ viewers: { full: 1, grid: 0, ro: 0 } })]), { live: 1, ready: 1, free: 0 });
  assert.equal(t([session({ viewers: { full: 0, grid: 4, ro: 2 } })]).free, 1, 'grid tiles and read-only views are nobody at the terminal');
  assert.deepEqual(t([session({ viewers: { full: 2 } }), session({ tmux: 'shop--api--s2' }), session({ tmux: 'shop--api--s3', state: 'working' })]), { live: 3, ready: 2, free: 1 });
});

test('the poll repaints the button without writing an attribute that did not change (a browser drops an open tooltip on any write)', async () => {
  const { w } = refreshWorld();
  await open(w);
  const b = btn(w);
  const writes = [];
  const set = b.setAttribute;
  b.setAttribute = (k, v) => { writes.push(k); return set.call(b, k, v); };
  for (let i = 0; i < 5; i++) push(w, STATE(30));
  assert.deepEqual(writes, [], 'five polls, no attribute written');
  b.click();
  await tick();
  assert.deepEqual([...new Set(writes)].sort(), ['aria-busy', 'title'], 'a state change writes only what changed');
});

test('while an account chip other than the one in use is picked the button hides (its caption is that account\'s reading; a refresh asks the account in use), and comes back with the current account', async () => {
  const { w } = refreshWorld();
  await open(w);
  const P = w.get('Usage.cur');
  assert.ok(!btn(w).classList.contains('hidden'));
  P.limAcct = 'work-account';
  w.get('Usage').refreshPaint(P);
  assert.ok(btn(w).classList.contains('hidden'));
  assert.ok(note(w).classList.contains('hidden'));
  P.limAcct = null;
  w.get('Usage').refreshPaint(P);
  assert.ok(!btn(w).classList.contains('hidden'));
});

// ---------------------------------------------------------------- a tap: the ask, the wait, the new reading

test('a tap disables the button ("refreshing…"), posts once with no auto flag, and asks the state every 1.5 s until the reading is newer; then it is a plain button again and the limits refetch', async () => {
  const { w, timers } = refreshWorld();
  await open(w);
  const before = calls(w).length;
  btn(w).click();
  assert.equal(text(btn(w)), 'refreshing…');
  assert.ok(btn(w).disabled);
  assert.equal(btn(w).getAttribute('aria-busy'), 'true');
  await tick();
  assert.deepEqual(posts(w).map((c) => [c.path, c.body]), [['/api/usage/refresh', {}]], 'one POST, an empty body: a tap never says auto');
  assert.equal(live(timers, 20000).length, 1, 'the give-up');
  assert.equal(live(timers, 1500).length, 1, 'the first state poll');
  fire(live(timers, 1500)[0]);
  assert.equal(w.get('__polls'), 1, 'the state is asked for through core.js poll(true)');
  assert.equal(live(timers, 1500).length, 1, 'and again 1.5 s later');
  btn(w).click();
  await tick();
  assert.equal(posts(w).length, 1, 'a second tap while it waits does nothing');
  push(w, STATE(30));                                                                   // the same old reading: not newer
  assert.equal(text(btn(w)), 'refreshing…');
  push(w, STATE(1, [session()], { usage: { value: { five_hour: { used_percentage: 51, resets_at: NOW + 3000, at: ISO(1), source: 'cache' }, seven_day: { used_percentage: 60, resets_at: NOW + 3 * 86400 }, source: 'cache' }, at: ISO(1) } }));
  assert.equal(text(btn(w)), 'Refresh');
  assert.ok(!btn(w).disabled);
  assert.ok(note(w).classList.contains('hidden'), 'no note: it worked');
  assert.equal(live(timers, 20000).length + live(timers, 1500).length, 0, 'every timer of the wait is gone');
  await tick();
  assert.ok(calls(w).slice(before).some((c) => c.path.startsWith('/api/usage/summary')), 'the summary refetches behind the repaint');
  assert.ok(calls(w).slice(before).some((c) => c.path.startsWith('/api/series?')), 'and so does the limit series');
  assert.match(text(q(w, '.uc-gauges .gauge[data-gauge="5H"] .g-val')), /51%/, 'the gauge shows the new number');
});

test('the wait ends on a newer reading whatever its source (a statusline that spoke meanwhile counts), never on an equal one', async () => {
  const { w } = refreshWorld({ st: STATE(100) });
  await open(w);
  const same = STATE(100);
  btn(w).click();
  await tick();
  push(w, same);
  assert.equal(text(btn(w)), 'refreshing…', 'the same time is not newer');
  push(w, STATE(99));
  assert.equal(text(btn(w)), 'Refresh', 'a second later is');
});

test('20 s without a newer reading: the button is back, the note says "no new reading yet", polling stops; the next tap clears the note', async () => {
  const { w, timers } = refreshWorld();
  await open(w);
  btn(w).click();
  await tick();
  fire(live(timers, 20000)[0]);
  assert.equal(text(btn(w)), 'Refresh');
  assert.ok(!btn(w).disabled);
  assert.equal(text(note(w)), 'no new reading yet');
  assert.ok(!note(w).classList.contains('hidden'));
  assert.equal(live(timers, 1500).length, 0, 'no more polls');
  const n = w.get('__polls');
  assert.equal(n, 0);
  btn(w).click();
  await tick();
  assert.equal(posts(w).length, 2);
  assert.ok(note(w).classList.contains('hidden'), 'asking again clears it');
  assert.equal(text(btn(w)), 'refreshing…');
});

test('the answer of the box may carry no session (demo mode answers {ok: true}); the wait is the same', async () => {
  const { w } = refreshWorld({ post: { ok: true } });
  await open(w);
  btn(w).click();
  await tick();
  assert.equal(text(btn(w)), 'refreshing…');
  push(w, STATE(0));
  assert.equal(text(btn(w)), 'Refresh');
});

test('a 409 shows its reason as a toast and puts the button back; nothing waits, nothing polls', async () => {
  const reason = 'no Claude session is at its prompt; start one to refresh';
  const { w, timers } = refreshWorld({ post: { __error: reason } });
  await open(w);
  btn(w).click();
  await tick();
  assert.deepEqual(toasts(w), [{ t: reason, o: { kind: 'warn' } }]);
  assert.equal(text(btn(w)), 'Refresh');
  assert.ok(!btn(w).disabled);
  assert.ok(note(w).classList.contains('hidden'));
  assert.equal(live(timers, 20000).length + live(timers, 1500).length, 0);
  const { w: w2 } = refreshWorld({ post: { __error: 'a refresh is already running' } });
  await open(w2);
  btn(w2).click();
  await tick();
  assert.equal(toasts(w2)[0].t, 'a refresh is already running');
});

test('a refresh another device asked for (state.usage_refresh.running) greys the button here too', async () => {
  const { w } = refreshWorld({ st: STATE(30, [session()], { usage_refresh: { running: true, last: null } }) });
  await open(w);
  assert.equal(text(btn(w)), 'refreshing…');
  assert.ok(btn(w).disabled);
  push(w, STATE(30, [session()], { usage_refresh: { running: false, last: { at: ISO(2), session: TMUX1, ok: true } } }));
  assert.equal(text(btn(w)), 'Refresh');
  assert.ok(!btn(w).disabled);
});

test('leaving the page ends the wait: its timers are cleared and a late answer paints nothing', async () => {
  const { w, timers, cleared } = refreshWorld();
  await open(w);
  btn(w).click();
  await tick();
  const ids = timers.filter((t) => t.live).map((t) => t.id);
  assert.ok(ids.length >= 2);
  const P = w.get('Usage.cur');
  w.run('pages.usage.unmount()');
  for (const id of ids) assert.ok(cleared.includes(id), `timer ${id} cleared`);
  assert.equal(P.rf.on, false);
  const n = calls(w).length;
  P.st = STATE(0);
  w.get('Usage').refreshCheck(P);
  assert.equal(calls(w).length, n, 'a page that is gone refetches nothing');
});

test('the caption row goes with the Claude tab: on the Codex tab neither the caption nor the button shows', async () => {
  const cx = { value: { primary: { used_percent: 20, window_minutes: 10080, resets_at: NOW + 3600 } }, at: ISO(5) };
  const { w } = refreshWorld({ st: STATE(30, [session()], { usage_codex: cx }) });
  await open(w, '#/usage?agent=codex');
  assert.ok(q(w, '.ufresh-row').classList.contains('hidden'));
  await open(w, '#/usage?agent=claude');
  assert.ok(!q(w, '.ufresh-row').classList.contains('hidden'));
  assert.ok(!btn(w).disabled);
});

test('with no reading yet the caption says so and the button is still there', async () => {
  const { w } = refreshWorld({ st: { projects: [{ name: 'shop', path: '/p/shop', root: null, orphan_sessions: [], repos: [{ name: 'api', state: 'ok', sessions: [session({ viewers: { full: 1 } })] }] }] } });
  await open(w);
  assert.equal(text(fresh(w)), 'no reading yet');
  assert.equal(text(btn(w)), 'Refresh');
  assert.ok(!btn(w).parentNode.classList.contains('hidden'));
});

// ---------------------------------------------------------------- opening the page: one automatic ask when the numbers are stale

const OLD = 600;                                                 // 10 minutes: past the 5 that count as stale

test('opening the page on a reading older than 5 minutes, with an unattached idle session, asks once by itself ({auto: true}); the answer is silent', async () => {
  const { w } = refreshWorld({ st: STATE(OLD) });
  await open(w);
  assert.deepEqual(posts(w).map((c) => c.body), [{ auto: true }]);
  assert.equal(text(btn(w)), 'refreshing…', 'the same wait as a tap');
  for (let i = 0; i < 6; i++) w.get('pages').usage.update(STATE(OLD));            // the 3 s poll goes on
  await tick();
  assert.equal(posts(w).length, 1, 'once per page open');
  assert.deepEqual(toasts(w), []);
});

test('an automatic ask the box refuses is silent: no toast, the button is back, no note', async () => {
  const { w } = refreshWorld({ st: STATE(OLD), post: { __error: 'no Claude session is at its prompt; start one to refresh' } });
  await open(w);
  await tick();
  assert.equal(posts(w).length, 1);
  assert.deepEqual(toasts(w), []);
  assert.equal(text(btn(w)), 'Refresh');
  assert.ok(note(w).classList.contains('hidden'));
});

test('the automatic ask is once per page open: a fresh mount of the page (leaving and coming back) asks again', async () => {
  const { w } = refreshWorld({ st: STATE(OLD) });
  await open(w);
  assert.equal(posts(w).length, 1);
  const pg = w.get('pages').usage;
  pg.unmount();
  root(w).textContent = '';
  pg.mount(root(w), w.get('parseHash')('#/usage'));
  pg.update(STATE(OLD));                                          // what the router does right after a mount
  await tick();
  assert.equal(posts(w).length, 2, 'a new page open is a new once');
  pg.update(STATE(OLD));
  await tick();
  assert.equal(posts(w).length, 2);
});

test('no automatic ask when the reading is fresh, or a little inside 5 minutes (the clock moves a few seconds while the suite runs)', async () => {
  for (const ago of [0, 30, 240]) {
    const { w } = refreshWorld({ st: STATE(ago) });
    await open(w);
    assert.equal(posts(w).length, 0, `a reading ${ago} s old`);
  }
});

test('no automatic ask into a session somebody is attached to, one that is not at its prompt, a permission, a compaction, or a Codex / shell row', async () => {
  const cases = {
    'attached': [session({ viewers: { full: 1, grid: 0, ro: 0 } })],
    'working': [session({ state: 'working' })],
    'permission dialog': [session({ state: 'waiting', flags: { wait_kind: 'permission' } })],
    'compacting': [session({ flags: { compacting: true } })],
    'codex': [session({ agent: 'codex', command: 'codex' })],
    'shell': [session({ agent: 'shell', command: 'zsh' })],
    'no sessions': [],
  };
  for (const [what, sessions] of Object.entries(cases)) {
    const { w } = refreshWorld({ st: STATE(OLD, sessions) });
    await open(w);
    assert.equal(posts(w).length, 0, what);
  }
  const { w } = refreshWorld({ st: STATE(OLD, [session()], { pending_permissions: [{ tmux_name: TMUX1 }] }) });
  await open(w);
  assert.equal(posts(w).length, 0, 'a pending permission request');
  const { w: w2 } = refreshWorld({ st: STATE(OLD, [session({ viewers: { full: 2 } }), session({ tmux: 'shop--api--s2' })]) });
  await open(w2);
  assert.equal(posts(w2).length, 1, 'one unattached idle session is enough');
});

test('no reading at all counts as stale; the ask waits for the first state when the page opens before it', async () => {
  const bare = { projects: [{ name: 'shop', path: '/p/shop', root: null, orphan_sessions: [], repos: [{ name: 'api', state: 'ok', sessions: [session()] }] }] };
  const { w } = refreshWorld({ st: null });
  await open(w);
  assert.equal(posts(w).length, 0, 'no state yet: nothing to decide on');
  push(w, bare);
  await tick();
  assert.deepEqual(posts(w).map((c) => c.body), [{ auto: true }]);
  push(w, bare);
  assert.equal(posts(w).length, 1);
});

test('the automatic ask is decided on the first state only: a reading that goes stale later does not trigger one (nothing runs on a timer)', async () => {
  const { w } = refreshWorld({ st: STATE(10) });
  await open(w);
  assert.equal(posts(w).length, 0);
  push(w, STATE(OLD));
  push(w, STATE(2 * OLD));
  await tick();
  assert.equal(posts(w).length, 0);
});

test('the page start asks nothing on its own besides the three reads and, when stale, the one ask: no setInterval of its own for the refresh', async () => {
  const { w, timers } = refreshWorld({ st: STATE(30) });
  await open(w);
  assert.equal(posts(w).length, 0);
  assert.equal(live(timers, 20000).length + live(timers, 1500).length, 0, 'no wait timers without a tap');
});
