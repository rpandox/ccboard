// The state poll's cadence (#31) and the offline snapshot's write rate and cap (#46): app/static/core.js poll(), pollDelay(),
// refreshNow() and rememberState() in the vm harness with fake timers, a fake clock and a fake fetch. Nothing renders: render(),
// renderHeader(), renderUsage() and renderBanner() are counters.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld } from './harness.mjs';

const settle = () => new Promise((r) => setImmediate(r));

/** core.js with fake timers (`timers`: the armed ones, by id), a fake clock (`clock.now`), and a fetch that answers `answer()` for /api/state. */
function pollWorld({ answer = () => ({ version: 'v1', n: 1 }), fail = () => false } = {}) {
  const timers = new Map();
  let nextId = 1;
  const clock = { now: 1_000_000 };
  const fetched = [];
  const w = makeWorld({
    setTimeout: (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    fetch: async (url) => {
      fetched.push(String(url));
      if (fail()) throw new Error('offline');
      const body = answer();
      return { ok: true, status: 200, statusText: 'OK', json: async () => body };
    },
  });
  w.load('core.js');
  w.run(`globalThis.__renders = 0; globalThis.render = () => { __renders += 1; }; globalThis.renderHeader = () => {};
         globalThis.renderUsage = () => {}; globalThis.renderBanner = () => {};`);
  w.ctx.__clock = clock;
  w.run('Date.now = () => __clock.now;');
  /** The one armed poll timer's delay (the test fails when there is not exactly one). */
  const armed = () => {
    const t = [...timers.values()];
    assert.equal(t.length, 1, `exactly one poll timer is armed, saw ${t.length}`);
    return t[0].ms;
  };
  /** Fire the armed timer as if its delay had passed. */
  const fire = async () => {
    const [[id, t]] = [...timers.entries()];
    timers.delete(id);
    clock.now += t.ms;
    t.fn();
    await settle();
  };
  return { w, timers, clock, fetched, armed, fire };
}

test('pollDelay() is 3000 while the tab is visible and 15000 while it is hidden', () => {
  const { w } = pollWorld();
  assert.equal(w.run('pollDelay()'), 3000);
  w.document.hidden = true;
  assert.equal(w.run('pollDelay()'), 15000);
  w.document.hidden = false;
  assert.equal(w.run('pollDelay()'), 3000);
});

test('the poll re-arms at 3 s while visible and at 15 s while hidden; becoming visible polls at once and returns to 3 s', async () => {
  const p = pollWorld();
  await p.w.run('poll(true)');
  assert.equal(p.armed(), 3000);
  await p.fire();
  assert.equal(p.fetched.length, 2);
  assert.equal(p.armed(), 3000);
  p.w.document.hidden = true;                                  // the tab goes to the background
  await p.fire();
  assert.equal(p.armed(), 15000, 'a hidden tab polls every 15 s');
  await p.fire();
  assert.equal(p.armed(), 15000);
  const before = p.fetched.length;
  p.clock.now += 5000;                                         // 5 s into the 15 s wait the tab comes back
  p.w.document.hidden = false;
  p.w.document.visibilityState = 'visible';
  p.w.run('installLifecycleListeners()');
  p.w.document.dispatch('visibilitychange');
  await settle();
  assert.equal(p.fetched.length, before + 1, 'becoming visible polls at once');
  assert.equal(p.armed(), 3000, 'and the next poll is 3 s away again');
});

test('two lifecycle events inside 500 ms poll once; a hidden-cadence timer armed just before is shortened to the visible one', async () => {
  const p = pollWorld();
  p.w.document.hidden = true;
  await p.w.run('poll(true)');
  assert.equal(p.armed(), 15000);
  p.w.document.hidden = false;
  p.clock.now += 100;                                          // inside the 500 ms guard: no second request
  p.w.run('refreshNow(); refreshNow();');
  await settle();
  assert.equal(p.fetched.length, 1, 'the guard holds');
  assert.equal(p.armed(), 3000, 'but the visible tab is not left waiting 15 s');
  p.clock.now += 600;
  p.w.run('refreshNow()');
  p.clock.now += 100;
  p.w.run('refreshNow()');
  await settle();
  assert.equal(p.fetched.length, 2, 'focus + pageshow + visibilitychange together are one poll');
  assert.equal(p.armed(), 3000);
});

test('a stale answer never paints over a newer one, and only the newest poll arms the next timer', async () => {
  const resolvers = [];
  const timers = new Map();
  let nextId = 1;
  const w = makeWorld({
    setTimeout: (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
    fetch: () => new Promise((resolve) => resolvers.push(resolve)),
  });
  w.load('core.js');
  w.run('globalThis.render = () => {}; globalThis.renderHeader = () => {}; globalThis.renderUsage = () => {}; globalThis.renderBanner = () => {};');
  const first = w.run('poll(true)');
  const second = w.run('poll(true)');
  const reply = (body) => ({ ok: true, status: 200, statusText: 'OK', json: async () => body });
  resolvers[1](reply({ which: 'new' }));
  await second;
  resolvers[0](reply({ which: 'old' }));
  await first;
  assert.equal(w.run('state.which'), 'new');
  assert.equal(timers.size, 1);
});

test('rememberState writes at most once per 30 s, only after a change, and saves a change at the next allowed time', async () => {
  let n = 0;
  const p = pollWorld({ answer: () => ({ version: 'v1', n }) });
  const saved = () => JSON.parse(p.w.localStorage.getItem('ccboard:last-state') || 'null');
  await p.w.run('poll(true)');
  assert.equal(saved().state.n, 0, 'the first answer is saved at once (the offline shell has something)');
  assert.equal(saved().at, p.clock.now);
  const firstAt = saved().at;
  n = 1;                                                       // a change 3 s later: not yet
  await p.fire();
  assert.equal(saved().at, firstAt);
  for (let i = 0; i < 8; i++) await p.fire();                 // 27 s more, unchanged: still the old copy (24 s since the write)
  assert.equal(saved().state.n, 0);
  await p.fire();
  await p.fire();                                              // past 30 s: the change made 27 s ago is written now, unchanged since
  assert.equal(saved().state.n, 1);
  const secondAt = saved().at;
  assert.ok(secondAt - firstAt >= 30000);
  for (let i = 0; i < 12; i++) await p.fire();                // 36 s with no change: nothing is written
  assert.equal(saved().at, secondAt);
});

test('rememberState never writes a state over 200 KB (one console warning), and a full storage never breaks the poll', async () => {
  const p = pollWorld();
  const warnings = [];
  p.w.run('console.warn = (m) => __warn.push(m);'.replace('__warn', 'globalThis.__warn'));
  p.w.ctx.__warn = warnings;
  const big = '"' + 'x'.repeat(200 * 1024) + '"';
  assert.equal(p.w.run(`rememberState(${JSON.stringify(big)}, true)`), false);
  p.clock.now += 31000;
  assert.equal(p.w.run(`rememberState(${JSON.stringify(big)}, true)`), false);
  assert.equal(p.w.localStorage.getItem('ccboard:last-state'), null);
  assert.equal(warnings.length, 1, 'warned once');
  p.w.localStorage.setItem = () => { throw new Error('QuotaExceededError'); };
  await p.w.run('poll(true)');
  assert.equal(p.w.run('state.n'), 1, 'the state still arrived');
  assert.equal(p.armed(), 3000, 'and the poll goes on');
});
