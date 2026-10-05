// Contract tests for the shared window arithmetic (v0.5.17f): core.js limitWindowNow / limitFreshness / limitCaption / limitUsageWindow and pages/agents.js
// agentsAcctWindow / agentsAcctUsedNow. tests/fixtures/window_now.json is the one table both this file and tests/test_usage_cache.py (accounts.window_now, the
// server's twin behind headroom) run, so the browser and the server cannot drift apart.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { ROOT, makeWorld, plain } from './harness.mjs';

const TABLE = JSON.parse(fs.readFileSync(path.join(ROOT, 'tests', 'fixtures', 'window_now.json'), 'utf8'));
const NOW = Math.floor(Date.now() / 1000);

function world() {
  const w = makeWorld();
  for (const f of ['core.js', 'components.js', 'router.js', 'pages/agents.js']) w.load(f);
  return w;
}
const call = (w, fn, ...args) => { w.ctx.__a = args; return plain(w.run(`${fn}(...__a)`)); };

for (const c of TABLE.cases) {
  test(`limitWindowNow, the shared table: ${c.name}`, () => {
    const w = world();
    const got = call(w, 'limitWindowNow', { pct: c.used, resets_at: c.resets_at, at: 0 }, TABLE.periods[c.win], c.now);
    if (c.expect === null) { assert.equal(got, null); return; }
    assert.equal(got.pct, c.expect.pct);
    assert.equal(got.resets_at, c.expect.next === null ? 0 : c.expect.next, 'the reset to count down to (0 when the window\'s end is not known)');
    assert.equal(got.rolled, c.expect.rolled);
  });
}

test('the arithmetic never lands on "now": a reset exactly at now is a window later, and the answer is always ahead of now', () => {
  const w = world();
  for (const [win, period] of [['5h', 18000], ['7d', 604800]]) {
    for (const back of [0, 1, 59, period - 1, period, period + 1, 3 * period, 17 * period + 5]) {
      const got = call(w, 'limitWindowNow', { pct: 50, resets_at: NOW - back, at: 0 }, period, NOW);
      assert.ok(got.rolled && got.pct === 0, `${win} ${back}`);
      assert.ok(got.resets_at > NOW && got.resets_at <= NOW + period, `${win} ${back}: the next reset is within one window ahead`);
      assert.equal((got.resets_at - (NOW - back)) % period, 0, 'a whole number of windows after the one that ended');
    }
  }
  assert.equal(call(w, 'limitWindowNow', { pct: 50, resets_at: NOW - 3 * 18000 - 5 }, 18000, NOW).missed, 3, 'whole windows skipped');
  assert.equal(call(w, 'limitWindowNow', { pct: 50, resets_at: NOW - 5 }, 18000, NOW).missed, 0);
});

test('limitWindowNow: the reading\'s own number is kept as `last`, a source other than the cache is the statusline, the time is read from seconds, milliseconds or ISO text', () => {
  const w = world();
  const rolled = call(w, 'limitWindowNow', { pct: 91.4, resets_at: NOW - 60, at: NOW - 600, source: 'cache' }, 18000, NOW);
  assert.equal(rolled.last, 91.4);
  assert.equal(rolled.pct, 0);
  assert.equal(rolled.source, 'cache');
  assert.equal(rolled.at, NOW - 600);
  assert.equal(call(w, 'limitWindowNow', { pct: 5, resets_at: NOW + 60, at: (NOW - 90) * 1000, source: 'weird' }, 18000, NOW).source, 'statusline');
  assert.equal(call(w, 'limitWindowNow', { pct: 5, resets_at: NOW + 60, at: (NOW - 90) * 1000 }, 18000, NOW).at, NOW - 90, 'milliseconds are told from seconds');
  assert.equal(call(w, 'limitWindowNow', { pct: 5, resets_at: NOW + 60, at: new Date((NOW - 90) * 1000).toISOString() }, 18000, NOW).at, NOW - 90);
  for (const bad of [null, undefined, {}, { pct: 'x' }, { pct: NaN }]) assert.equal(call(w, 'limitWindowNow', bad, 18000, NOW), null);
  const unknownPeriod = call(w, 'limitWindowNow', { pct: 50, resets_at: NOW - 60 }, 0, NOW);
  assert.deepEqual([unknownPeriod.pct, unknownPeriod.resets_at], [0, 0], 'a window with no known length is rolled over but has nothing to count down to');
});

test('limitFreshness: the three caption states in plain words', () => {
  const w = world();
  assert.equal(call(w, 'limitFreshness', NOW - 300, 'statusline'), 'updated 5m ago · from the last session');
  assert.equal(call(w, 'limitFreshness', NOW - 300), 'updated 5m ago · from the last session', 'no source: a session\'s statusline');
  assert.equal(call(w, 'limitFreshness', new Date((NOW - 7200) * 1000).toISOString(), 'cache'), "updated 2h ago · from Claude Code's cache");
  assert.equal(call(w, 'limitFreshness', 0, 'cache'), 'no reading yet');
  assert.equal(call(w, 'limitFreshness', null), 'no reading yet');
  assert.equal(call(w, 'limitFreshness', 'not a time'), 'no reading yet');
});

test('limitCaption: the freshness, led by "no usage recorded since the window reset" once rolled over; nothing for a window without a time', () => {
  const w = world();
  const rolled = call(w, 'limitWindowNow', { pct: 80, resets_at: NOW - 60, at: NOW - 7200 }, 18000, NOW);
  assert.equal(call(w, 'limitCaption', rolled), 'no usage recorded since the window reset · updated 2h ago · from the last session');
  const live = call(w, 'limitWindowNow', { pct: 80, resets_at: NOW + 600, at: NOW - 60, source: 'cache' }, 18000, NOW);
  assert.equal(call(w, 'limitCaption', live), "updated 1m ago · from Claude Code's cache");
  assert.equal(call(w, 'limitCaption', call(w, 'limitWindowNow', { pct: 80, resets_at: NOW + 600 }, 18000, NOW)), '');
  assert.equal(call(w, 'limitCaption', call(w, 'limitWindowNow', { pct: 80, resets_at: NOW - 600 }, 18000, NOW)), 'no usage recorded since the window reset');
  assert.equal(call(w, 'limitCaption', null), '');
});

test('limitUsageWindow reads state.usage: a window\'s own time and source win over the record\'s; no window, no answer', () => {
  const w = world();
  const st = { usage: { value: { five_hour: { used_percentage: 40, resets_at: NOW + 600 }, seven_day: { used_percentage: 60, resets_at: NOW - 5, at: new Date((NOW - 3600) * 1000).toISOString(), source: 'cache' }, source: 'statusline' },
    at: new Date((NOW - 60) * 1000).toISOString() } };
  const five = call(w, 'limitUsageWindow', st, '5h');
  assert.deepEqual([five.pct, five.at, five.source, five.rolled], [40, NOW - 60, 'statusline', false]);
  const seven = call(w, 'limitUsageWindow', st, '7d');
  assert.deepEqual([seven.pct, seven.at, seven.source, seven.rolled], [0, NOW - 3600, 'cache', true]);
  assert.equal(call(w, 'limitUsageWindow', { usage: { value: { five_hour: { used_percentage: 40 } } } }, '7d'), null);
  for (const st2 of [null, {}, { usage: null }, { usage: { value: null } }, { usage: { value: { five_hour: { used_percentage: 'x' } } } }]) assert.equal(call(w, 'limitUsageWindow', st2, '5h'), null);
});

const row = (over = {}) => ({ key: 'k1', current: false, rl_5h: 60, rl_7d: 80, resets_5h: NOW + 3600, resets_7d: NOW + 200000, rl_5h_at: new Date((NOW - 600) * 1000).toISOString(), rl_7d_at: new Date((NOW - 600) * 1000).toISOString(), source: 'cache', ...over });

test('agentsAcctUsedNow: a row\'s own reading, 0 once its reset has passed (also after several missed windows), null without a reading; the number is unchanged for callers', () => {
  const w = world();
  assert.equal(call(w, 'agentsAcctUsedNow', null, row(), '5h', NOW), 60);
  assert.equal(call(w, 'agentsAcctUsedNow', null, row({ resets_5h: NOW - 1 }), '5h', NOW), 0);
  assert.equal(call(w, 'agentsAcctUsedNow', null, row({ resets_7d: NOW - 3 * 604800 - 3600 }), '7d', NOW), 0);
  assert.equal(call(w, 'agentsAcctUsedNow', null, row({ rl_5h: null }), '5h', NOW), null);
  assert.equal(call(w, 'agentsAcctUsedNow', null, null, '5h', NOW), null);
  assert.equal(call(w, 'agentsAcctUsed', row({ resets_5h: NOW + 1 }), '5h', NOW), 60, 'a reset one second ahead keeps the reading');
});

test('agentsAcctWindow: the account in use answers with the topbar pills\' record (state.usage), another account from its row, both with when and where they were read', () => {
  const w = world();
  const st = { accounts: { current: 'k1' }, usage: { value: { five_hour: { used_percentage: 33, resets_at: NOW + 1000 }, seven_day: { used_percentage: 44, resets_at: NOW - 100 } }, at: new Date((NOW - 30) * 1000).toISOString() } };
  const cur = call(w, 'agentsAcctWindow', st, row({ current: true }), '5h', NOW);
  assert.deepEqual([cur.pct, cur.source, cur.at, cur.rolled], [33, 'statusline', NOW - 30, false], 'one figure for the account in use on every screen');
  assert.deepEqual([call(w, 'agentsAcctWindow', st, row({ key: 'k1' }), '7d', NOW).pct, call(w, 'agentsAcctUsedNow', st, row({ current: true }), '7d', NOW)], [0, 0], 'its rolled window too');
  const other = call(w, 'agentsAcctWindow', st, row({ key: 'k2' }), '5h', NOW);
  assert.deepEqual([other.pct, other.source, other.at, other.resets_at], [60, 'cache', NOW - 600, NOW + 3600]);
  assert.equal(call(w, 'agentsAcctWindow', { accounts: { current: 'k1' } }, row({ current: true }), '5h', NOW).pct, 60, 'no live record: the row\'s own reading');
  const old = call(w, 'agentsAcctWindow', null, { key: 'k3', rl_5h: 10, resets_5h: NOW + 60 }, '5h', NOW);
  assert.deepEqual([old.pct, old.at, old.source], [10, 0, 'statusline'], 'an older row with no time or source');
});
