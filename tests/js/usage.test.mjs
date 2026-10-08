// Contract tests for app/static/pages/usage.js (v0.5.17): the Usage page. Real core.js, components.js, keymap.js, router.js and usage.js on minidom's DOM
// inside the vm harness; window.Charts is a recording fake (so the page is tested without charts.js), api() answers from __answers.
// The last tests run the page against the real charts.js (with a stub uPlot) and are skipped while that file does not exist.
// v0.5.17b: the Accounts section (rows, total, 'most room' line, rename sheet) and the Limits account chips are tested with two demo-like accounts.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const NOW = Math.floor(Date.now() / 1000);
const ISO = (secAgo) => new Date((NOW - secAgo) * 1000).toISOString();
const tick = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');
const UUID1 = '5b1c9d3e-0001-4a7e-8c2f-000000000001';          // the live session (the state spells it upper case)
const UUID2 = '5b1c9d3e-0002-4a7e-8c2f-000000000002';
const UUID3 = '5b1c9d3e-0003-4a7e-8c2f-000000000003';
const TMUX1 = 'ccboard--ccboard--s1';

// ---------------------------------------------------------------- fixtures

const day = (i, total, zero, byProject) => ({ day: `2026-09-${String(20 + i).padStart(2, '0')}`, total, zero: !!zero, by_agent: { claude: total }, by_project: byProject || { ccboard: total },
  tokens: Math.round(total * 1e5), hours: zero ? 0 : 1.5 });
const DAILY7 = [day(0, 10), day(1, 0, true), day(2, 30), day(3, 5), day(4, 20), day(5, 40, false, { ccboard: 10, '(unattributed)': 30 }), day(6, 44.56, false, { '(unattributed)': 24.81, useRAIDfun: 17.15, Phasezero: 1.8, cts_nepal: 0.8 })];
const DAILY30 = Array.from({ length: 30 }, (_, i) => day(i % 28, i % 5 === 0 ? 0 : 5 + i, i % 5 === 0));
const WINDOW = (total, extra = []) => ({ total, by_agent: { claude: { total, tokens: 40000000 }, codex: { total: 0, tokens: 8700000 } },
  by_project: [{ project: '(unattributed)', total: 24.81, hours: 0.0 }, { project: 'useRAIDfun', total: 17.15, hours: 0.12 }, { project: 'Phasezero', total: 1.8, hours: 0.23 }, { project: 'cts_nepal', total: 0.81, hours: 0.05 }, ...extra] });
const HEAT = Array.from({ length: 7 }, (_, d) => Array.from({ length: 24 }, (_, h) => (h >= 9 && h <= 18 ? d + h : 0)));
const SUMMARY = (daily = DAILY7, over = {}) => ({
  generated_at: ISO(0), tz_min: 345, source: 'samples',
  windows: { today: WINDOW(44.56), '7d': WINDOW(44.56), '30d': WINDOW(300) },
  daily, hourly_profile: Array.from({ length: 24 }, (_, h) => (h >= 9 && h <= 18 ? 5 : 0)), heatmap: HEAT,
  top_sessions: [
    { key: `claude:${UUID1}`, project: 'ccboard', repo: 'ccboard', agent: 'claude', total: 68.96, hours: 1.2, tokens: 180382285, models: ['claude-sonnet-5-5', 'claude-opus-5-5'] },
    { key: `claude:${UUID2}`, project: 'SD-Law-website', repo: 'root', agent: 'claude', total: 40, hours: 0, tokens: 90000000, models: ['claude-opus-5-5'] },
    { key: `claude:${UUID3}`, project: 'my.proj', repo: 'api', agent: 'claude', total: 12.5, hours: 0.4, tokens: 5200000, models: [] },
  ],
  active_hours: { ccboard: 3, useRAIDfun: 0.12 },
  rate_limits: { claude: { rl_5h: { at: ISO(120), value: 77, meta: { resets_at: NOW + 2700 } }, rl_7d: { at: ISO(120), value: 59, meta: { resets_at: NOW + 4 * 86400 } } } },
  episodes: [{ kind: '5h', at: ISO(30 * 3600), resets_at: NOW - 25 * 3600, session: 's1' }, { kind: '7d', at: ISO(100 * 3600), resets_at: NOW - 90 * 3600, session: 's2' }],
  unpriced: [{ key: 'claude:5b1c9d3e-0014-4a7e-8c2f-000000000014', agent: 'claude', project: 'Phasezero', repo: 'website', tokens: 26100000, models: ['fable-5-1'] }],
  ...over,
});
const SERIES = (since = '7d') => ({ since: ISO(7 * 86400), until: ISO(0), step: 50000, t: Array.from({ length: 12 }, (_, i) => NOW - 7 * 86400 + i * 50000),
  series: { 'rl_5h:claude': [10, 20, null, null, 60, 70, 65, 42, 50, 77, 77, 77], 'rl_7d:claude': [5, 8, 10, 12, 20, 30, 35, 40, 45, 50, 55, 59] },
  meta: { 'rl_5h:claude': { resets_at: NOW - 2 * 3600 }, 'rl_7d:claude': { resets_at: NOW + 3 * 86400 } } });
const DETAIL = { since: ISO(86400), until: ISO(0), step: 600, t: Array.from({ length: 8 }, (_, i) => NOW - 86400 + i * 3000),
  series: { [`ctx:${TMUX1}`]: [10, 20, 30, 35, null, 50, 60, 66], [`scost:${TMUX1}`]: [1, 2, 3, 4, 5, 6, 7, 8] }, meta: { [`ctx:${TMUX1}`]: { model: 'claude-opus-5', window: 200000 } } };
const EVENTS = { events: [
  { t: NOW - 3000, key: 'ccboard--ccboard--s1', v: 1, m: { p: 'ccboard', r: 'ccboard', s: 's1', a: 'claude' } },
  { t: NOW - 2000, key: 'ccboard--ccboard--s1', v: 2, m: { p: 'ccboard', r: 'ccboard', s: 's1', a: 'claude' } },
  { t: NOW - 2500, key: 'phasezero--web--s2', v: 1, m: { p: 'phasezero', r: 'web', s: 's2', a: 'claude' } },
  { t: NOW - 1000, key: 'phasezero--web--s2', v: 5, m: { p: 'phasezero', r: 'web', s: 's2', a: 'claude' } },
], truncated: false };
const STATE = (extra = {}) => ({
  usage: { value: { five_hour: { used_percentage: 77, resets_at: NOW + 2700 }, seven_day: { used_percentage: 59, resets_at: NOW + 3 * 86400 } }, at: ISO(1) },
  projects: [{ name: 'ccboard', path: '/p/ccboard', root: null, orphan_sessions: [],
    repos: [{ name: 'ccboard', state: 'ok', sessions: [{ tmux: TMUX1, name: 's1', claude_session_id: UUID1.toUpperCase(), state: 'working' }] }] }],
  ...extra,
});
const answers = (over = {}) => ({
  '/api/usage/summary?days=7': SUMMARY(), '/api/usage/summary?days=30': SUMMARY(DAILY30),
  '/api/series?series=rl_5h,rl_7d': SERIES(), '/api/series?series=ctx,scost': DETAIL, '/api/series/events': EVENTS, ...over,
});

// ---------------------------------------------------------------- the world

const CHARTS_STUB = `
globalThis.__charts = { calls: [], fetches: [], destroyAll: 0, destroyed: [], readyCalls: 0 };
globalThis.Charts = {
  ready() { __charts.readyCalls++; return Promise.resolve(); },
  fetch(p, o) { __charts.fetches.push({ p, force: !!(o && o.force) }); return api('GET', p); },
  line(host, data, opts) { __charts.calls.push({ fn: 'line', host, data, opts }); host._uplot = true; },
  stackedBars(host, days, opts) { __charts.calls.push({ fn: 'stackedBars', host, days, opts }); host.append(document.createElement('svg')); },
  gantt(host, events, opts) { __charts.calls.push({ fn: 'gantt', host, events, opts }); host.append(document.createElement('svg')); },
  heatmap(host, grid, hourly, opts) { __charts.calls.push({ fn: 'heatmap', host, grid, hourly, opts }); host.append(document.createElement('div')); },
  destroy(host) { __charts.destroyed.push(host); },
  destroyAll() { __charts.destroyAll++; },
  fmtUsd(v) { return '$' + Number(v).toFixed(2); },
  fmtTok(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n); },
  shortModel(m) { return String(m).replace(/^claude-/, ''); },
};`;

/** A world with the real scripts and the router mounted on #page. opts: wide, charts (false = no Charts at all), timers, st, noState, shared (also load
 *  pages/agents.js and pages/settings.js: the account rows' pencil opens settingsRenameAccount, which needs them; off by default because the chip hue
 *  tests want a world without agents.js). */
function usageWorld({ wide = false, charts = true, st = STATE(), over = {}, extra = {}, shared = false } = {}) {
  const timers = [];
  const cleared = [];
  const w = makeWorld({
    matchMedia: (q) => ({ matches: wide && /840/.test(q), addEventListener() {}, removeEventListener() {} }),
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearInterval: (id) => cleared.push(id), ...extra,
  });
  installDom(w);
  for (const f of ['core.js', 'components.js', 'keymap.js', 'router.js']) w.load(f);
  w.run('globalThis.__polls = 0; poll = async () => { __polls++; };');                // the shared rename sheet asks for a fresh state after a save; the tests own their state
  w.ctx.__calls = [];
  w.ctx.__answers = answers(over);
  w.run(`api = async (method, path, body) => {
    __calls.push({ method, path, body });
    for (const [prefix, v] of Object.entries(__answers)) {
      if (!path.startsWith(prefix)) continue;
      if (v && v.__error) throw new Error(v.__error);
      return typeof v === 'function' ? v(path) : v;
    }
    return {};
  };`);
  if (charts) w.run(CHARTS_STUB);
  if (shared) {
    w.load('pages/agents.js');                                                        // agentsAccounts / agentsAcctName: the rename sheet's helpers
    w.load('pages/settings.js');                                                      // settingsRenameAccount: the one rename sheet the pencil opens
  }
  w.load('pages/usage.js');
  if (st) { w.ctx.__st = st; w.run('state = __st'); }
  return { w, timers, cleared };
}

const root = (w) => w.document.querySelector('#page');
const q = (w, sel) => root(w).querySelector(sel);
const qa = (w, sel) => root(w).querySelectorAll(sel);
const calls = (w) => plain(w.get('__calls'));
const paths = (w, prefix) => calls(w).map((c) => c.path).filter((p) => p.startsWith(prefix));
const chartCalls = (w, fn) => w.get('__charts.calls').filter((c) => c.fn === fn);
const loading = async (w) => { await w.get('Usage.cur').loading; await tick(); };
const go = async (w, hash = '#/usage') => { w.location.hash = hash; await loading(w); };
const clean = (w, what = '') => {
  const t = text(root(w));
  assert.doesNotMatch(t, /undefined|NaN|\[object|null/, `${what}: no stray undefined/NaN/null in the text`);
};
const keyR = (w) => w.get('Keymap').handle({ key: 'r', target: w.document.body, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, repeat: false, preventDefault() {} });

// ---------------------------------------------------------------- structure and first paint

test('the seven sections in the asked order (Accounts first), each with an h2 and a provenance label; a skeleton paints before any answer', async () => {
  const { w } = usageWorld();
  w.location.hash = '#/usage';
  const secs = qa(w, 'section.usec');
  assert.deepEqual(secs.map((s) => s.getAttribute('data-sec')), ['accounts', 'limits', 'cost', 'sessions', 'projects', 'activity', 'timeline']);
  assert.deepEqual(secs.map((s) => text(s.querySelector('h2'))), ['Accounts', 'Limits', 'Cost per day', 'Sessions', 'Projects', 'Activity', 'Timeline']);
  assert.ok(secs.every((s) => s.querySelector('.prov')), 'a provenance span in every section');
  assert.equal(text(secs[0].querySelector('.prov')), 'subscription windows (statusline) · API-equivalent $ last');
  assert.equal(text(secs[1].querySelector('.prov')), 'statusline (official)');
  assert.equal(text(secs[2].querySelector('.prov')), 'API-equivalent (ccusage list price)');
  assert.equal(text(q(w, '[data-sec="activity"] .prov')), 'hook events, local time');
  for (const id of ['accounts', 'cost', 'sessions', 'projects', 'activity', 'timeline']) assert.ok(q(w, `[data-sec="${id}"] .usk`), `${id}: skeleton before the first answer`);
  assert.ok(q(w, '.lim-chart').classList.contains('loading'), 'the limits chart host is a skeleton too');
  assert.equal(text(q(w, 'h1')), 'Usage');
  assert.equal(w.document.title, 'Usage · ccboard');
  assert.equal(chartCalls(w, 'line').length + chartCalls(w, 'stackedBars').length, 0, 'nothing drawn yet');
  await loading(w);
  assert.equal(qa(w, '.usk').length, 0, 'every skeleton gave way');
  clean(w, 'loaded');
});

test('fetches: the three reads in parallel on mount, in the viewer\'s zone, uPlot asked for through Charts.ready; update() never fetches', async () => {
  const { w } = usageWorld();
  await go(w);
  const c = paths(w, '/api/');
  assert.equal(c.length, 3, c.join('\n'));
  assert.match(c.find((p) => p.startsWith('/api/usage/summary')), /^\/api\/usage\/summary\?days=7&tz_min=-?\d+$/);
  assert.match(c.find((p) => p.startsWith('/api/series?')), /^\/api\/series\?series=rl_5h,rl_7d&key=claude&since=7d&points=\d+$/);
  assert.equal(c.find((p) => p.startsWith('/api/series/events')), '/api/series/events?series=state&since=24h');
  assert.equal(w.get('__charts.readyCalls'), 1);
  for (let i = 0; i < 6; i++) w.get('pages').usage.update(STATE());                  // the 3 s state poll
  await tick();
  assert.equal(paths(w, '/api/').length, 3, 'the poll never fetches');
});

test('on a wide screen the timeline asks for 48 h', async () => {
  const { w } = usageWorld({ wide: true });
  await go(w);
  assert.equal(paths(w, '/api/series/events')[0], '/api/series/events?series=state&since=48h');
  const g = chartCalls(w, 'gantt')[0];
  assert.equal(Math.round(g.opts.until - g.opts.since), 48 * 3600);
  assert.match(text(q(w, '[data-sec="timeline"] .prov')), /48 h/);
});

// ---------------------------------------------------------------- limits

test('limits: the gauges follow the state (warn at 60, bad at 85, reset countdown), one line chart over the range with guides, episode marks and resets', async () => {
  const { w } = usageWorld();
  await go(w);
  const g5 = q(w, '.gauge[data-gauge="5H"]');
  const g7 = q(w, '.gauge[data-gauge="7D"]');
  assert.equal(text(g5.querySelector('.g-val')), '77%');
  assert.equal(text(g7.querySelector('.g-val')), '59%');
  assert.ok(g5.classList.contains('warn') && g7.classList.contains('ok') && !g5.classList.contains('hidden'));
  assert.equal(g5.querySelector('.g-bar i').style.width, '77%');
  assert.match(text(g5.querySelector('.g-reset')), /^resets in 4\dm · \d\d:\d\d$/);
  assert.match(text(g7.querySelector('.g-reset')), /^resets in 2d/);
  w.get('pages').usage.update(STATE({ usage: { value: { five_hour: { used_percentage: 91, resets_at: NOW + 600 }, seven_day: { used_percentage: 10 } } } }));
  assert.equal(text(g5.querySelector('.g-val')), '91%');
  assert.ok(g5.classList.contains('bad'), 'bad from 85');
  assert.equal(text(g7.querySelector('.g-reset')), '', 'a window without a reset time shows none');
  const line = chartCalls(w, 'line').filter((c) => c.opts.names.some((n) => n.startsWith('rl_')));
  assert.equal(line.length, 1, 'one chart for both windows');
  const o = line[0].opts;
  assert.deepEqual(plain(o.names), ['rl_5h:claude', 'rl_7d:claude']);
  assert.deepEqual(plain(o.labels), { 'rl_5h:claude': '5H', 'rl_7d:claude': '7D' });
  assert.deepEqual(plain(o.colors), { 'rl_5h:claude': '--sig', 'rl_7d:claude': '--fg-2' });
  assert.equal(o.yMax, 100);
  assert.deepEqual(plain(o.thresholds), [60, 85]);
  assert.deepEqual(plain(o.marks).map((m) => [m.label, m.cls]), [['5h limit', 'mark-5h'], ['7d limit', 'mark-7d']]);
  assert.ok(plain(o.marks).every((m) => m.t > NOW - 7 * 86400 && m.t < NOW), 'marks sit at the episodes\' times');
  assert.ok(plain(o.resets).length >= 1 && plain(o.resets).every((r) => /^resets \d\d:\d\d|^resets \w{3} \d\d:\d\d/.test(r.label)), JSON.stringify(plain(o.resets)));
  assert.equal(line[0].data.series['rl_5h:claude'].length, 12, 'the series body goes to Charts as it came');
  assert.equal(q(w, '.lim-chart').classList.contains('loading'), false);
  assert.equal(q(w, '.lim-chart').classList.contains('hidden'), false);
  clean(w);
});

test('limits: without a live reading the gauges fall back to the last statusline sample and say so; with neither, the next step is named', async () => {
  const { w } = usageWorld({ st: { projects: [] } });
  await go(w);
  assert.equal(text(q(w, '.gauge[data-gauge="5H"] .g-val')), '77%');
  assert.match(text(q(w, '[data-sec="limits"] .unote')), /last statusline sample, 2m ago/);
  const { w: w2 } = usageWorld({ st: { projects: [] }, over: { '/api/usage/summary?days=7': SUMMARY(DAILY7, { rate_limits: {} }) } });
  await go(w2);
  assert.ok(q(w2, '.gauge[data-gauge="5H"]').classList.contains('hidden'));
  assert.match(text(q(w2, '[data-sec="limits"] .unote')), /No 5H \/ 7D reading yet.*\+ session/);
});

test('limits: a series with no samples is an empty state with its next step, never a blank chart box', async () => {
  const { w } = usageWorld({ over: { '/api/series?series=rl_5h,rl_7d': { since: ISO(86400), until: ISO(0), step: 600, t: [NOW - 600, NOW], series: { 'rl_5h:claude': [null, null], 'rl_7d:claude': [null, null] }, meta: {} } } });
  await go(w);
  assert.equal(chartCalls(w, 'line').length, 0);
  assert.ok(q(w, '.lim-chart').classList.contains('hidden'));
  assert.match(text(q(w, '[data-sec="limits"] .uslot')), /No samples yet.*first hook event/);
  clean(w);
});

// ---------------------------------------------------------------- the range

test('the range: default 7d, a ?range= wins and is written back, the toggle writes ccboard:charts:range and re-requests with since=30d', async () => {
  const { w } = usageWorld();
  await go(w);
  assert.equal(q(w, 'button[data-range="7d"]').getAttribute('aria-pressed'), 'true');
  assert.equal(q(w, 'button[data-range="30d"]').getAttribute('aria-pressed'), 'false');
  q(w, 'button[data-range="30d"]').click();
  assert.equal(w.localStorage.getItem('ccboard:charts:range'), '30d');
  assert.equal(q(w, 'button[data-range="30d"]').getAttribute('aria-pressed'), 'true');
  assert.ok(q(w, '[data-sec="cost"] .usk'), 'no cache yet for 30d: a skeleton until its answer arrives');
  await loading(w);
  assert.ok(paths(w, '/api/series?').some((p) => /since=30d/.test(p)), 'the series was re-requested with since=30d');
  assert.ok(paths(w, '/api/usage/summary').some((p) => /days=30&/.test(p)), 'and the summary with days=30');
  assert.equal(w.location.hash, '#/usage?range=30d', 'the address follows (replace)');
  assert.equal(w.history.calls.at(-1).method, 'replaceState');
  assert.equal(chartCalls(w, 'stackedBars').pop().days.length, 30);
  // a fresh page of the same world: the stored range, then a query that overrides it
  const { w: w2 } = usageWorld();
  w2.localStorage.setItem('ccboard:charts:range', '24h');
  await go(w2);
  assert.equal(q(w2, 'button[data-range="24h"]').getAttribute('aria-pressed'), 'true');
  assert.match(paths(w2, '/api/series?')[0], /since=24h/);
  const { w: w3 } = usageWorld();
  w3.localStorage.setItem('ccboard:charts:range', '24h');
  await go(w3, '#/usage?range=30d');
  assert.equal(w3.localStorage.getItem('ccboard:charts:range'), '30d', 'the query was written back');
  assert.match(paths(w3, '/api/series?')[0], /since=30d/);
  const { w: w4 } = usageWorld();
  await go(w4, '#/usage?range=bogus');
  assert.match(paths(w4, '/api/series?')[0], /since=7d/, 'an unknown range is ignored');
});

test('a range seen before re-renders at once from its cache, with no new request; a new one paints a skeleton first', async () => {
  const { w } = usageWorld();
  await go(w);
  q(w, 'button[data-range="30d"]').click();
  assert.ok(q(w, '[data-sec="cost"] .usk'), 'a new range: skeleton while the answer is on its way');
  await loading(w);
  const before = calls(w).length;
  const bars = chartCalls(w, 'stackedBars').length;
  q(w, 'button[data-range="7d"]').click();                                           // no await: the paint must be synchronous
  assert.equal(chartCalls(w, 'stackedBars').length, bars + 1, 'repainted in the same tick');
  assert.equal(chartCalls(w, 'stackedBars').at(-1).days.length, 7);
  assert.equal(q(w, '[data-sec="cost"] .usk'), null, 'no skeleton for a cached range');
  await loading(w);
  assert.equal(calls(w).length, before, 'a cached range younger than the stale limit asks the network for nothing');
});

test('a stale cached range repaints at once and refreshes behind it', async () => {
  const { w } = usageWorld();
  await go(w);
  q(w, 'button[data-range="30d"]').click();
  await loading(w);
  w.run('Usage.cur.cache["7d"].at = Date.now() - 60000');
  const before = calls(w).length;
  q(w, 'button[data-range="7d"]').click();
  assert.equal(q(w, '[data-sec="cost"] .usk'), null);
  await loading(w);
  assert.equal(calls(w).length, before + 3, 'old enough: summary, series and events were asked again');
});

test('r cycles the range (24h, 7d, 30d) when the keymap is loaded, and only on this page', async () => {
  const { w } = usageWorld();
  await go(w);
  assert.ok(keyR(w), 'the r key is handled');
  assert.equal(q(w, 'button[data-range="30d"]').getAttribute('aria-pressed'), 'true');
  await loading(w);
  keyR(w);
  assert.equal(q(w, 'button[data-range="24h"]').getAttribute('aria-pressed'), 'true', '30d wraps to 24h');
  await loading(w);
  keyR(w);
  assert.equal(q(w, 'button[data-range="7d"]').getAttribute('aria-pressed'), 'true');
  assert.equal(w.localStorage.getItem('ccboard:charts:range'), '7d');
  w.run('pages.usage.unmount()');
  assert.equal(w.get('Keymap').list.length, 0, 'unmount unbinds r');
});

// ---------------------------------------------------------------- cost

test('cost: stacked bars over the daily series, the window totals line, the unattributed segment kept whole with its tooltip, the unpriced note', async () => {
  const { w } = usageWorld();
  await go(w);
  const call = chartCalls(w, 'stackedBars')[0];
  assert.equal(call.days.length, 7);
  assert.equal(call.opts.by, 'project');
  assert.equal(call.opts.top, 6);
  assert.equal(call.opts.hatchZero, true);
  assert.equal(typeof call.opts.hueOf, 'function', 'the chart colours a project by the hue it has everywhere else (chipHue)');
  assert.equal(call.opts.hueOf('Phasezero'), w.run("Usage.hue('project', 'Phasezero')"));
  assert.equal(call.days[1].zero, true, 'the zero day is passed on as it is (Charts hatches it)');
  assert.equal(call.days[6].by_project['(unattributed)'], 24.81, 'unattributed stays its own key, never folded into a project');
  assert.equal(call.opts.unpriced.has('Phasezero'), true, 'the project with an unpriced session is marked');
  assert.equal(text(q(w, '.utotals')), 'Last 7 days: $44.56 · 48.7M tokens · 0.4 h');
  const note = q(w, '[data-note="unattributed"]');
  assert.ok(note, 'a visible line, because a phone has no hover');
  assert.equal(note.getAttribute('title'), 'sessions the board did not start, with no folder on record');
  assert.match(text(note), /\(unattributed\) \$24\.81 in this window: sessions the board did not start/);
  assert.match(text(q(w, '[data-note="unpriced"]')), /^1 session has tokens but no price \(hatched\)/);
  assert.equal(q(w, '[data-note="unpriced"]').getAttribute('title'), 'projects: Phasezero');
  clean(w);
});

test('cost: the stack toggle re-renders by agent at once (no request), remembers itself, and marks the agent of the unpriced session', async () => {
  const { w } = usageWorld();
  await go(w);
  const before = calls(w).length;
  q(w, 'button[data-stack="agent"]').click();
  const c = chartCalls(w, 'stackedBars').at(-1);
  assert.equal(c.opts.by, 'agent');
  assert.equal(c.opts.unpriced.has('claude'), true);
  assert.equal(w.localStorage.getItem('ccboard:charts:stack'), 'agent');
  assert.equal(q(w, '[data-note="unattributed"]'), null, 'agents have no unattributed segment');
  assert.equal(q(w, 'button[data-stack="agent"]').getAttribute('aria-pressed'), 'true');
  assert.equal(calls(w).length, before);
});

test('cost: a window with no spend is an empty state naming the next step; no unpriced note without unpriced sessions', async () => {
  const zero = Array.from({ length: 7 }, (_, i) => day(i, 0, true));
  const { w } = usageWorld({ over: { '/api/usage/summary?days=7': SUMMARY(zero, { unpriced: [], top_sessions: [], windows: { today: { total: 0, by_agent: {}, by_project: [] }, '7d': { total: 0, by_agent: {}, by_project: [] }, '30d': { total: 0, by_agent: {}, by_project: [] } }, heatmap: [], hourly_profile: [] }) } });
  await go(w);
  assert.equal(chartCalls(w, 'stackedBars').length, 0);
  assert.match(text(q(w, '[data-sec="cost"]')), /No spend in the last 7 days.*Start one/);
  assert.match(text(q(w, '[data-sec="sessions"]')), /No priced sessions yet.*\+ session/);
  assert.match(text(q(w, '[data-sec="projects"]')), /No project spend in this window.*longer range/);
  assert.match(text(q(w, '[data-sec="activity"]')), /No hook events in this window.*first session/);
  clean(w, 'empty');
});

// ---------------------------------------------------------------- sessions

test('sessions: compact table with short model chips, $, tokens and hours; Open only on the key a live session owns, navigating to #/s/<tmux>', async () => {
  const { w } = usageWorld();
  await go(w);
  const rows = qa(w, 'tr.srow');
  assert.deepEqual(rows.map((r) => r.getAttribute('data-key')), [`claude:${UUID1}`, `claude:${UUID2}`, `claude:${UUID3}`]);
  assert.ok(q(w, 'table').classList.contains('bp5-html-table') && q(w, 'table').classList.contains('bp5-compact'), 'bp5 html-table compact through el()');
  const first = rows[0];
  assert.equal(text(first.querySelector('.srow-link')), 'ccboard', 'project/repo collapses when they are the same');
  assert.deepEqual(first.querySelectorAll('.bdg-model').map(text), ['sonnet-5-5', 'opus-5-5']);
  assert.deepEqual(first.querySelectorAll('td').map(text).slice(2, 5), ['$68.96', '180.4M', '1.2 h']);
  assert.equal(text(rows[1].querySelector('.srow-link')), 'SD-Law-website', 'repo root is not repeated');
  assert.equal(text(rows[2].querySelector('.srow-link')), 'my.proj/api');
  assert.equal(rows[2].querySelector('.srow-link').querySelectorAll('wbr').length, 1, 'the name may wrap after the slash (not inside a word)');
  assert.equal(rows[1].querySelector('.srow-link').querySelectorAll('wbr').length, 0, 'a name without a slash has no break point added');
  const opens = qa(w, '.srow-open');
  assert.equal(opens.length, 1, 'Open appears for the live session only');
  assert.equal(opens[0].closest('tr'), first);
  assert.equal(opens[0].getAttribute('href'), `#/s/${TMUX1}`);
  assert.equal(text(opens[0]), 'Open');
  assert.equal(opens[0].classList.contains('bp5-intent-primary'), false, 'the page has no primary: Open is a plain small button');
  // one tap to the terminal: the href is a route the router knows
  assert.equal(plain(w.run(`parseHash('#/s/${TMUX1}')`)).id, 'session');
  // the NAME is not a link (a tap on it toggles the detail); the project has its own small icon link in the last cell
  for (const r of rows) assert.equal(r.querySelector('a.srow-link'), null, 'the name is plain text: it toggles the row, it never navigates');
  assert.equal(first.querySelector('a.srow-proj').getAttribute('href'), '#/p/ccboard');
  assert.equal(rows[1].querySelector('a.srow-proj').getAttribute('href'), '#/p/SD-Law-website');
  assert.equal(first.querySelector('a.srow-proj').getAttribute('title'), 'Open the project');
  assert.ok(first.querySelector('a.srow-proj').getAttribute('aria-label'), 'an icon link needs a name');
  assert.equal(rows[2].querySelector('a.srow-proj'), null, 'a project name with a dot is not a route param: no broken link');
  clean(w);
});

test('sessions: project and model chips wear the muted hue chipHue gives them (the page asks pages/agents.js; without it they stay plain)', async () => {
  const { w } = usageWorld({ extra: {} });
  w.run("globalThis.chipHue = (kind, key) => 'hue-' + kind + '-' + String(key).toLowerCase().replace(/[^a-z0-9]+/g, '')");
  await go(w);
  const first = qa(w, 'tr.srow')[0];
  assert.ok(first.querySelector('.srow-link').classList.contains('hue-project-ccboard'), 'the project name is hued by its project');
  assert.deepEqual(first.querySelectorAll('.bdg-model').map((n) => n.className.split(/\s+/).find((c) => c.startsWith('hue-'))), ['hue-model-claudesonnet55', 'hue-model-claudeopus55'], 'each model chip by its full model id');
  const plainWorld = usageWorld();
  await go(plainWorld.w);
  assert.ok(qa(plainWorld.w, 'tr.srow')[0].querySelectorAll('.bdg-model').every((n) => !/hue-/.test(n.className)), 'no chipHue, no hue class');
});

test('Usage.caption: 24 h says both windows (the bars are a week, the numbers are today), 7 d and 30 d keep their name', () => {
  const { w } = usageWorld();
  const sum = { windows: { today: { total: 12.5 }, '7d': { total: 80 }, '30d': { total: 300 } }, daily: [] };
  w.ctx.__sum = sum;
  const cap = (range) => w.run(`Usage.caption(__sum, ${JSON.stringify(range)})`);
  assert.match(cap('24h'), /^Last 7 days · today \$12\.50/);
  assert.match(cap('7d'), /^Last 7 days: \$80\.00/);
  assert.match(cap('30d'), /^Last 30 days: \$300/);
  assert.match(w.run("Usage.CAPTION_TIP['24h']"), /today only/);
});

test('sessions: the Open buttons follow the state (a session that starts or ends) with no fetch', async () => {
  const { w } = usageWorld({ st: { projects: [] } });
  await go(w);
  assert.equal(qa(w, '.srow-open').length, 0);
  const n = calls(w).length;
  w.get('pages').usage.update(STATE());
  assert.equal(qa(w, '.srow-open').length, 1);
  assert.equal(calls(w).length, n);
  w.get('pages').usage.update(STATE({ projects: [{ name: 'SD-Law-website', root: { name: 'root', sessions: [{ tmux: 'sd--root--s1', claude_session_id: UUID2 }] }, repos: [], orphan_sessions: [{ tmux: 'x--y--s9', claude_session_id: UUID3 }] }] }));
  assert.deepEqual(qa(w, '.srow-open').map((a) => a.getAttribute('href')), ['#/s/sd--root--s1', '#/s/x--y--s9'], 'root and orphan sessions count too');
  assert.equal(calls(w).length, n);
});

test('sessions: a row toggles its detail (a button, aria-expanded); a live session shows a ctx and a cost chart over 24 h, a gone one says so', async () => {
  const { w } = usageWorld();
  await go(w);
  const rows = qa(w, 'tr.srow');
  const toggle = (r) => r.querySelector('button.srow-toggle');
  assert.equal(toggle(rows[0]).tagName, 'BUTTON', 'focusable: Enter and Space click it');
  assert.equal(toggle(rows[0]).getAttribute('aria-expanded'), 'false');
  assert.ok(qa(w, 'tr.sdetail').every((d) => d.classList.contains('hidden')));
  const n = paths(w, '/api/series?series=ctx').length;
  toggle(rows[0]).click();
  assert.equal(toggle(rows[0]).getAttribute('aria-expanded'), 'true');
  assert.equal(text(toggle(rows[0])), '▾');
  const d1 = q(w, `tr.sdetail[data-detail="claude:${UUID1}"]`);
  assert.equal(d1.classList.contains('hidden'), false);
  await tick(); await tick();
  const req = paths(w, '/api/series?series=ctx');
  assert.equal(req.length, n + 1);
  assert.match(req[0], new RegExp(`^/api/series\\?series=ctx,scost&key=${TMUX1}&since=24h&points=\\d+$`));
  const lines = chartCalls(w, 'line').filter((c) => c.opts.names[0].startsWith('ctx:') || c.opts.names[0].startsWith('scost:'));
  assert.deepEqual(plain(lines.map((c) => c.opts.names[0])), [`ctx:${TMUX1}`, `scost:${TMUX1}`], 'two small charts: the scales differ');
  assert.equal(lines[0].opts.yMax, 100);
  assert.deepEqual(plain(lines[0].opts.thresholds), [60, 85]);
  assert.equal(lines[1].opts.yMax, undefined);
  assert.match(text(d1), /claude-sonnet-5-5, claude-opus-5-5 · 180\.4M tokens · 1\.2 h · claude:/);
  assert.equal(d1.querySelector('a').getAttribute('href'), '#/p/ccboard', 'the detail names the way on to the project (the row\'s icon link is hidden on a phone)');
  // a row whose id is not live: the message and the way on, no request
  toggle(rows[1]).click();
  const d2 = q(w, `tr.sdetail[data-detail="claude:${UUID2}"]`);
  assert.equal(d2.classList.contains('hidden'), false);
  assert.match(text(d2), /no live session for this id/);
  assert.equal(d2.querySelector('a').getAttribute('href'), '#/p/SD-Law-website');
  assert.equal(paths(w, '/api/series?series=ctx').length, n + 1, 'nothing fetched for a session that is gone');
  // a click on the row (not only the button) toggles; a click on a link does not
  rows[1].click();
  assert.equal(toggle(rows[1]).getAttribute('aria-expanded'), 'false');
  assert.equal(d2.classList.contains('hidden'), true);
  const link = rows[1].querySelector('a.srow-proj');
  link.dispatchEvent({ type: 'click', preventDefault() {} });
  assert.equal(toggle(rows[1]).getAttribute('aria-expanded'), 'false', 'a link click navigates, it does not toggle');
  rows[1].querySelector('.srow-link').click();                                 // the session NAME toggles the detail (it used to jump to the project)
  assert.equal(toggle(rows[1]).getAttribute('aria-expanded'), 'true', 'a tap on the name opens the row');
  assert.equal(w.location.hash, '#/usage', 'and stays on the page');
  rows[1].querySelector('.srow-link').click();
  assert.equal(toggle(rows[1]).getAttribute('aria-expanded'), 'false');
  // closing keeps the chart alive: opening again does not refetch
  toggle(rows[0]).click();
  toggle(rows[0]).click();
  await tick();
  assert.equal(paths(w, '/api/series?series=ctx').length, n + 1);
  clean(w, 'details');
});

test('sessions: a detail whose series has no samples says so instead of drawing a blank chart', async () => {
  const { w } = usageWorld({ over: { '/api/series?series=ctx,scost': { since: ISO(86400), until: ISO(0), step: 600, t: [NOW - 600, NOW], series: { [`ctx:${TMUX1}`]: [null, null], [`scost:${TMUX1}`]: [null, null] }, meta: {} } } });
  await go(w);
  qa(w, 'button.srow-toggle')[0].click();
  await tick(); await tick();
  assert.match(text(q(w, `tr.sdetail[data-detail="claude:${UUID1}"]`)), /no context samples for this session in the last 24 h/);
  assert.match(text(q(w, `tr.sdetail[data-detail="claude:${UUID1}"]`)), /no cost samples for this session in the last 24 h/);
  assert.equal(chartCalls(w, 'line').filter((c) => c.opts.names[0].startsWith('ctx:')).length, 0);
});

// ---------------------------------------------------------------- projects

test('projects: cost, active hours and $/h side by side; (unattributed) is its own last row with the tooltip and no link; the others link to their project', async () => {
  const { w } = usageWorld();
  await go(w);
  const rows = qa(w, 'tr.prow');
  assert.deepEqual(rows.map((r) => r.getAttribute('data-project')), ['useRAIDfun', 'Phasezero', 'cts_nepal', '(unattributed)'], 'by cost, unattributed last whatever it spent');
  const un = rows[3];
  assert.equal(un.getAttribute('title'), 'sessions the board did not start, with no folder on record');
  assert.equal(un.querySelector('[data-unattributed]').getAttribute('title'), 'sessions the board did not start, with no folder on record');
  assert.equal(un.querySelector('a'), null, 'no project to link to');
  assert.equal(text(un.querySelector('.p-name')), '(unattributed)', 'its own row, not merged into a project');
  assert.deepEqual(un.querySelectorAll('td').map(text).slice(1), ['$24.81', '0.0 h', '–'], 'no hours, so no rate');
  assert.deepEqual(rows[0].querySelectorAll('td').map(text), ['useRAIDfun', '$17.15', '0.1 h', '$142.92/h']);
  assert.equal(rows[0].querySelector('a').getAttribute('href'), '#/p/useRAIDfun');
  assert.equal(rows[1].querySelector('a').getAttribute('href'), '#/p/Phasezero');
  q(w, 'button[data-range="30d"]').click();
  await loading(w);
  assert.equal(qa(w, 'tr.prow').length, 4, 'the 30 day window has its own rows');
  clean(w);
});

test('projects: more than twelve named rows are cut with a Show N more button', async () => {
  const many = Array.from({ length: 14 }, (_, i) => ({ project: `p${i}`, total: 100 - i, hours: 1 + i }));
  const { w } = usageWorld({ over: { '/api/usage/summary?days=7': SUMMARY(DAILY7, { windows: { today: WINDOW(1), '7d': { total: 5, by_agent: {}, by_project: [...many, { project: '(unattributed)', total: 3, hours: 0 }] }, '30d': WINDOW(1) } }) } });
  await go(w);
  assert.equal(qa(w, 'tr.prow').length, 13, '12 named plus the unattributed row');
  const more = qa(w, '[data-body="projects"] button').find((b) => /Show 2 more/.test(text(b)));
  assert.ok(more);
  more.click();
  assert.equal(qa(w, 'tr.prow').length, 15);
  assert.equal(qa(w, 'tr.prow').at(-1).getAttribute('data-project'), '(unattributed)');
  assert.ok(qa(w, '[data-body="projects"] button').some((b) => /Show fewer/.test(text(b))));
});

// ---------------------------------------------------------------- activity and timeline

test('activity: the heatmap and the hourly profile go to Charts with the zone label', async () => {
  const { w } = usageWorld();
  await go(w);
  const h = chartCalls(w, 'heatmap');
  assert.equal(h.length, 1);
  assert.equal(h[0].grid.length, 7);
  assert.equal(h[0].hourly.length, 24);
  assert.equal(h[0].opts.tzLabel, 'UTC+5:45', 'the summary\'s own zone');
  assert.equal(text(q(w, '[data-sec="activity"] .prov')), 'hook events, local time');
});

test('timeline: the events go to the Gantt with a 24 h window and the 30 row cap; a truncated list is handed on', async () => {
  const { w } = usageWorld();
  await go(w);
  const g = chartCalls(w, 'gantt');
  assert.equal(g.length, 1);
  assert.equal(g[0].events.length, 4);
  assert.equal(Math.round(g[0].opts.until - g[0].opts.since), 24 * 3600);
  assert.equal(g[0].opts.maxRows, 30, 'the cap is Charts\' to enforce and to say ("N more")');
  assert.equal(g[0].opts.truncated, false);
  const { w: w2 } = usageWorld({ over: { '/api/series/events': { events: EVENTS.events, truncated: true } } });
  await go(w2);
  assert.equal(chartCalls(w2, 'gantt')[0].opts.truncated, true, 'a server-side cut is handed on for Charts to say');
});

test('timeline: a body without an events array (no fixture, a fresh box) is a sane empty Gantt with its next step', async () => {
  const { w } = usageWorld({ over: { '/api/series/events': {} } });
  await go(w);
  assert.equal(chartCalls(w, 'gantt').length, 0, 'nothing to draw');
  assert.match(text(q(w, '[data-sec="timeline"]')), /No state changes in the last 24 h/);
  assert.match(text(q(w, '[data-sec="timeline"]')), /Start a session and its timeline appears here/);
  assert.equal(qa(w, '.usk').length, 0);
  clean(w, 'empty gantt');
});

// ---------------------------------------------------------------- errors and the missing library

test('an error shows an inline callout with Retry in the sections that lost their data; Retry asks again and recovers; the others keep their paint', async () => {
  const { w } = usageWorld({ over: { '/api/usage/summary?days=7': { __error: '502 Bad gateway' } } });
  await go(w);
  for (const id of ['cost', 'sessions', 'projects', 'activity']) {
    const e = q(w, `[data-sec="${id}"] .uerr`);
    assert.ok(e, `${id}: an error block, not a blank section`);
    assert.match(text(e), /Could not load the usage summary: 502 Bad gateway/);
    assert.equal(text(e.querySelector('button')), 'Retry');
  }
  assert.equal(chartCalls(w, 'gantt').length, 1, 'the timeline did not depend on the summary');
  assert.equal(q(w, '[data-sec="limits"] .uerr'), null, 'the limits section does not need the summary');
  assert.equal(chartCalls(w, 'line').length, 1, 'the limit chart still draws without the episodes');
  assert.deepEqual(plain(chartCalls(w, 'line')[0].opts.marks), [], 'no summary, no marks');
  clean(w, 'error');
  w.ctx.__answers['/api/usage/summary?days=7'] = SUMMARY();
  const n = paths(w, '/api/usage/summary').length;
  q(w, '[data-sec="cost"] .uerr button').click();
  await loading(w);
  assert.equal(paths(w, '/api/usage/summary').length, n + 1, 'Retry asked the network again');
  assert.equal(qa(w, '.uerr').length, 0);
  assert.equal(qa(w, 'tr.srow').length, 3);
  assert.equal(chartCalls(w, 'stackedBars').length, 1);
  clean(w, 'recovered');
});

test('a refresh that fails keeps the last data and says so in one line with Retry', async () => {
  const { w, timers } = usageWorld();
  await go(w);
  w.ctx.__answers['/api/series/events'] = { __error: 'boom' };
  w.ctx.__answers['/api/series?series=rl_5h,rl_7d'] = { __error: 'gone' };
  timers[0].fn();
  await loading(w);
  const a = q(w, '.ualert');
  assert.equal(a.classList.contains('hidden'), false);
  assert.match(text(a), /Could not refresh the limit series, the timeline: gone\. Showing the last data\./);
  assert.equal(qa(w, 'tr.srow').length, 3, 'the cached sections stay');
  w.ctx.__answers['/api/series/events'] = EVENTS;
  w.ctx.__answers['/api/series?series=rl_5h,rl_7d'] = SERIES();
  a.querySelector('button').click();
  await loading(w);
  assert.equal(q(w, '.ualert').classList.contains('hidden'), true);
});

test('charts.js missing: every chart section degrades to an inline error with Retry, the page and the tables stay alive', async () => {
  const { w } = usageWorld({ charts: false, extra: { console: { ...console, error() {} } } });
  await go(w);
  assert.match(text(q(w, '[data-sec="cost"] .uerr')), /charts\.js did not load/);
  assert.match(text(q(w, '[data-sec="limits"] .uslot')), /chart library did not load/);
  assert.match(text(q(w, '[data-sec="activity"] .uerr')), /charts\.js did not load/);
  assert.match(text(q(w, '[data-sec="timeline"] .uerr')), /charts\.js did not load/);
  assert.equal(qa(w, 'tr.srow').length, 3, 'the tables need no library');
  assert.equal(qa(w, 'tr.prow').length, 4);
  assert.equal(text(q(w, '.gauge[data-gauge="5H"] .g-val')), '77%');
  clean(w, 'no charts.js');
});

// ---------------------------------------------------------------- lifecycle

test('every minute while the tab is visible the page refreshes (fresh, past Charts\' cache); a hidden tab waits and refreshes when shown', async () => {
  const { w, timers } = usageWorld();
  await go(w);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 60000);
  const n = calls(w).length;
  w.document.hidden = true;
  timers[0].fn();
  assert.equal(calls(w).length, n, 'a hidden tab fetches nothing');
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  await loading(w);
  assert.equal(calls(w).length, n + 3, 'shown again: one refresh');
  timers[0].fn();
  await loading(w);
  assert.equal(calls(w).length, n + 6, 'and the minute tick refreshes');
  const f = plain(w.get('__charts.fetches'));
  assert.deepEqual(f.slice(0, 3).map((x) => x.force), [false, false, false], 'the mount reads through Charts\' cache');
  assert.deepEqual(f.slice(-3).map((x) => x.force), [true, true, true], 'the minute refresh forces past it');
  // an open live detail refreshes with it
  qa(w, 'button.srow-toggle')[0].click();
  await tick(); await tick();
  const m = paths(w, '/api/series?series=ctx').length;
  timers[0].fn();
  await loading(w); await tick();
  assert.equal(paths(w, '/api/series?series=ctx').length, m + 1);
});

test('unmount calls Charts.destroyAll, stops the timer, removes the listener and the key; a late answer paints nothing and throws nothing', async () => {
  const { w, timers, cleared } = usageWorld();
  let release;
  w.ctx.__hold = new Promise((r) => { release = r; });
  w.ctx.__answers['/api/usage/summary?days=7'] = () => w.get('__hold').then(() => SUMMARY());
  w.location.hash = '#/usage';
  const P = w.get('Usage.cur');
  assert.ok(P);
  const secs = qa(w, 'section.usec').length;
  w.run('pages.usage.unmount()');
  assert.equal(w.get('__charts.destroyAll'), 1);
  assert.deepEqual(cleared, [1], 'the interval was cleared');
  assert.equal(w.get('Usage.cur'), null);
  assert.equal(w.get('Keymap').list.length, 0);
  assert.equal((w.document.listeners.visibilitychange || []).length, 0, 'the visibility listener is gone');
  release();
  await tick(); await tick();
  assert.equal(chartCalls(w, 'stackedBars').length, 0, 'the late summary was dropped');
  assert.equal(chartCalls(w, 'heatmap').length, 0);
  assert.equal(qa(w, 'section.usec').length, secs);
  assert.doesNotThrow(() => timers[0].fn(), 'a tick after unmount does nothing');
  assert.equal(chartCalls(w, 'line').length, 0);
});

test('a range change drops a late answer of the range left behind (but caches it)', async () => {
  const { w } = usageWorld();
  let release;
  w.ctx.__hold = new Promise((r) => { release = r; });
  w.ctx.__answers['/api/usage/summary?days=7'] = () => w.get('__hold').then(() => SUMMARY());
  w.location.hash = '#/usage';
  q(w, 'button[data-range="30d"]').click();
  await loading(w);
  const bars = chartCalls(w, 'stackedBars').length;
  assert.equal(chartCalls(w, 'stackedBars').at(-1).days.length, 30);
  release();
  await tick(); await tick();
  assert.equal(chartCalls(w, 'stackedBars').length, bars, 'the 7 day answer arrived late and did not repaint the 30 day view');
  assert.equal(q(w, 'button[data-range="30d"]').getAttribute('aria-pressed'), 'true');
  const sum = w.get('Usage.cur.cache["7d"].summary');
  assert.ok(sum && sum.daily.length === 7, 'but it is cached for the day the person goes back');
  q(w, 'button[data-range="7d"]').click();
  assert.equal(chartCalls(w, 'stackedBars').at(-1).days.length, 7, 'and the way back is instant');
});

test('onRoute: a hash change inside the page (#/usage -> #/usage?range=24h) moves the range without a remount', async () => {
  const { w } = usageWorld();
  await go(w);
  const ids = w.get('Usage.cur.id');
  const nodes = qa(w, 'section.usec');
  w.location.hash = '#/usage?range=24h';
  await loading(w);
  assert.equal(q(w, 'button[data-range="24h"]').getAttribute('aria-pressed'), 'true');
  assert.equal(qa(w, 'section.usec')[0], nodes[0], 'the same nodes: no remount');
  assert.equal(w.get('Usage.cur.id'), ids);
  assert.equal(w.localStorage.getItem('ccboard:charts:range'), '24h');
  assert.match(paths(w, '/api/series?').at(-1), /since=24h/);
  assert.match(text(q(w, '.utotals')), /^Last 7 days · today \$/, 'the 24 h range draws a week of bars and says so, then names the number\'s window');
  assert.match(q(w, '.utotals').getAttribute('title'), /bars show the last 7 days.*today only/i, 'the title names the exact windows');
});

test('usage.js is definition-only apart from registerPage: loading it touches no DOM, storage, network or timer', () => {
  const boom = (what) => () => { throw new Error(`load-time call of ${what}()`); };
  const w = makeWorld({ setTimeout: boom('setTimeout'), setInterval: boom('setInterval'), fetch: boom('fetch'), matchMedia: boom('matchMedia'), requestAnimationFrame: boom('requestAnimationFrame') });
  w.load('core.js'); w.load('components.js'); w.load('router.js');
  assert.doesNotThrow(() => w.load('pages/usage.js'));
  assert.ok(w.get("typeof pages.usage.mount === 'function'"));
  assert.equal(w.get('Usage.cur'), null);
});

// ---------------------------------------------------------------- accounts (v0.5.17b)

const AK1 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001';          // the account in use: no label, name Demo, plan max
const AK2 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a002';          // the other one: label Work, plan pro, a 5-hour window that has rolled over since its reading
const win = (total, tokens, hours, sessions) => ({ total, tokens, hours, sessions });
const ACCOUNTS = (over1 = {}, over2 = {}) => [
  { key: AK1, email: 'demo@example.com', name: 'Demo', label: null, plan: 'max', current: true,
    rl_5h: { value: 42, resets_at: NOW + 3 * 3600, at: ISO(60) }, rl_7d: { value: 71, resets_at: NOW + 3 * 86400, at: ISO(60) },
    windows: { today: win(12.5, 3000000, 1.5, 2), '7d': win(60, 20000000, 9.5, 4), '30d': win(200, 90000000, 30, 9) }, episodes: 2, last_seen: ISO(30), ...over1 },
  { key: AK2, email: 'work@example.com', name: 'Work', label: 'Work', plan: 'pro', current: false,
    rl_5h: { value: 100, resets_at: NOW - 3600, at: ISO(7200) }, rl_7d: { value: 83, resets_at: NOW + 2 * 86400, at: ISO(7200) },
    windows: { today: win(0, 0, 0, 0), '7d': win(30, 10000000, 4.5, 2), '30d': win(80, 40000000, 10, 3) }, episodes: 1, last_seen: ISO(7200), ...over2 },
  { key: 'unknown', email: null, name: '(before account tracking)', label: null, plan: null, current: false, rl_5h: null, rl_7d: null,
    windows: { today: win(0, 0, 0, 0), '7d': win(0, 0, 0, 0), '30d': win(20, 5000000, 2, 4) }, episodes: 1, last_seen: null },
];
const TOTALS = (over = {}) => ({ today: win(12.5, 3000000, 1.5, 2), '7d': win(90, 30000000, 14, 5), '30d': win(300, 135000000, 42, 14), accounts: 2,
  headroom_5h: [{ key: AK2, left_pct: 100 }, { key: AK1, left_pct: 58 }], headroom_7d: [{ key: AK1, left_pct: 29 }, { key: AK2, left_pct: 17 }], ...over });
const EPISODES = [
  { kind: '5h', at: ISO(30 * 3600), resets_at: NOW - 25 * 3600, session: 's1', acct: AK1 },
  { kind: '7d', at: ISO(100 * 3600), resets_at: NOW - 90 * 3600, session: 's2', acct: AK2 },
  { kind: '5h', at: ISO(200 * 3600), resets_at: NOW - 190 * 3600, session: 's3', acct: null },              // before account tracking: 30 days only
];
const ACC_SUMMARY = (over = {}) => SUMMARY(DAILY7, { accounts: ACCOUNTS(), total: TOTALS(), episodes: EPISODES, ...over });
const ACC_SUMMARY30 = (over = {}) => SUMMARY(DAILY30, { accounts: ACCOUNTS(), total: TOTALS(), episodes: EPISODES, ...over });
// one account's own limit series (key=acct:<key>): the same body as SERIES with the account in the names
const SERIES_OF = (key, five = 33, seven = 44) => ({ ...SERIES(), series: { [`rl_5h:acct:${key}`]: [1, 2, null, null, 5, 6, 7, 8, 9, 10, 11, five], [`rl_7d:acct:${key}`]: [2, 3, 4, 5, 6, 7, 8, 9, 10, 20, 30, seven] },
  meta: { [`rl_5h:acct:${key}`]: { resets_at: NOW + 3600 }, [`rl_7d:acct:${key}`]: { resets_at: NOW + 86400 } } });
const SERIES_ANSWER = (p) => (/key=acct:([0-9a-f-]+)/.exec(p) ? SERIES_OF(/key=acct:([0-9a-f-]+)/.exec(p)[1]) : SERIES());
const HUES = `globalThis.chipHue = (kind, key) => 'hue-' + (kind === 'account' ? (String(key).endsWith('1') ? 'blue' : 'violet') : 'slate');`;
const accWorld = (opts = {}) => {
  const r = usageWorld({ shared: true, ...opts, over: { '/api/usage/summary?days=7': ACC_SUMMARY(), '/api/usage/summary?days=30': ACC_SUMMARY30(), '/api/series?series=rl_5h,rl_7d': SERIES_ANSWER, ...(opts.over || {}) } });
  r.w.run(HUES);
  return r;
};
const row = (w, key) => q(w, `.ua-row[data-account="${key}"]`);
const cell = (r, col) => text(r.querySelector(`[data-col="${col}"] .ua-v`));
const submit = (form) => form.dispatchEvent({ type: 'submit', preventDefault() {} });

test('accounts: the first section, one row per account (current first; the pre-tracking history last and dim once the range reaches it) and a closing TOTAL row', async () => {
  const { w } = accWorld();
  await go(w);
  const sec = q(w, '[data-sec="accounts"]');
  assert.equal(text(sec.querySelector('h2')), 'Accounts');
  assert.equal(qa(w, '[data-sec="accounts"] .usk').length, 0);
  assert.deepEqual(qa(w, '[data-sec="accounts"] .ua-row').map((r) => r.getAttribute('data-account') || 'total'), [AK1, AK2, 'total'], '7 days: the history before tracking has nothing in it, so no row for it');
  const r1 = row(w, AK1);
  const r2 = row(w, AK2);
  assert.equal(text(r1.querySelector('.ua-chip')), 'Demo', 'no label: the name');
  assert.equal(text(r2.querySelector('.ua-chip')), 'Work', 'the label wins over the name');
  assert.ok(r1.querySelector('.ua-chip').classList.contains('hue-blue') && r2.querySelector('.ua-chip').classList.contains('hue-violet'), 'the chip wears chipHue(account, key)');
  assert.equal(text(r1.querySelector('.ua-plan')), 'max');
  assert.equal(text(r2.querySelector('.ua-plan')), 'pro');
  assert.equal(r1.getAttribute('data-current'), '');
  assert.equal(r2.getAttribute('data-current'), null);
  assert.equal(qa(w, '.ua-current').length, 1, 'one account is marked current');
  assert.ok(r1.querySelector('.ua-current'));
  assert.equal(text(r1.querySelector('.ua-email')), 'demo@example.com');
  assert.equal(text(r2.querySelector('.ua-email')), 'work@example.com');
  // the selected range (7d): tokens, active hours, sessions, limit hits, then the API-equivalent dollars last
  assert.deepEqual(['tokens', 'hours', 'sessions', 'hits', 'usd'].map((c) => cell(r1, c)), ['20.0M', '9.5 h', '4', '1', '$60.00']);
  assert.deepEqual(['tokens', 'hours', 'sessions', 'hits', 'usd'].map((c) => cell(r2, c)), ['10.0M', '4.5 h', '2', '1', '$30.00']);
  assert.deepEqual(qa(w, '.ua-row[data-account="' + AK1 + '"] .ua-stat').map((n) => n.getAttribute('data-col')), ['tokens', 'hours', 'sessions', 'hits', 'usd'], 'dollars last');
  assert.ok(r1.querySelector('[data-col="usd"]').classList.contains('ua-dim'), 'and dim');
  assert.match(r1.querySelector('[data-col="usd"]').getAttribute('title'), /API list price/);
  const tot = q(w, '.ua-row.total');
  assert.equal(text(tot.querySelector('.ua-total-l')), 'Total');
  assert.equal(text(tot.querySelector('.ua-count')), '2 accounts');
  assert.deepEqual(['tokens', 'hours', 'sessions', 'hits', 'usd'].map((c) => cell(tot, c)), ['30.0M', '14.0 h', '5', '2', '$90.00']);
  assert.match(tot.querySelector('[data-col="sessions"]').getAttribute('title'), /ran on two accounts counts once/, 'the rows add up to 6 sessions, the total says 5: the cell says why');
  assert.equal(text(q(w, '[data-note="sessions"]')), 'Sessions: 5 in total, 6 in the rows above: a session that ran on two accounts counts once in the total and once in each row.', 'and so does a visible line (a phone has no hover)');
  assert.equal(qa(w, '.ua-row.total [role="cell"]').length, 8, 'a table row keeps its eight cells');
  assert.equal(tot.querySelector('.gauge'), null);
  assert.equal(q(w, '.uacc').getAttribute('role'), 'table');
  assert.equal(qa(w, '.uacc-head [role="columnheader"]').length, 8);
  // 30 days reaches the history before tracking: its row comes last and dim, with no gauges, no pencil and no hue chip
  q(w, 'button[data-range="30d"]').click();
  await loading(w);
  assert.deepEqual(qa(w, '[data-sec="accounts"] .ua-row').map((r) => r.getAttribute('data-account') || 'total'), [AK1, AK2, 'unknown', 'total']);
  const ru = row(w, 'unknown');
  assert.ok(ru.classList.contains('unknown'));
  assert.equal(text(ru.querySelector('.ua-name')), '(before account tracking)');
  assert.equal(ru.querySelector('.gauge'), null, 'no gauges for the history before tracking');
  assert.equal(ru.querySelector('.ua-edit'), null, 'and nothing to rename');
  assert.equal(ru.querySelector('.ua-chip'), null, 'and no hue chip');
  assert.deepEqual(['tokens', 'hours', 'sessions', 'hits', 'usd'].map((c) => cell(ru, c)), ['5.0M', '2.0 h', '4', '1', '$20.00']);
  assert.equal(qa(w, '.ua-row[data-account="unknown"] [role="cell"]').length, 8);
  clean(w, 'accounts');
});

test('accounts: the "(before account tracking)" row is shown only when the range has something in it: tokens, hours, sessions or a limit hit; all zero hides it, and so does an empty section', async () => {
  const unknown = (win30, win7) => ({ ...ACCOUNTS()[2], windows: { today: win(0, 0, 0, 0), '7d': win7 || win(0, 0, 0, 0), '30d': win30 } });
  const rowsOf = async (accounts, episodes, range) => {
    const { w } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts, episodes }), '/api/usage/summary?days=30': ACC_SUMMARY30({ accounts, episodes }) } });
    await go(w, range ? `#/usage?range=${range}` : '#/usage');
    return qa(w, '[data-sec="accounts"] .ua-row').map((r) => r.getAttribute('data-account') || 'total');
  };
  const real = ACCOUNTS().slice(0, 2);
  const live = [{ kind: '5h', at: ISO(3600), resets_at: NOW + 100, session: 's9', acct: AK1 }];           // an episode of the current account, 1 h ago: in every range
  assert.deepEqual(await rowsOf([...real, unknown(win(0, 0, 0, 0))], live), [AK1, AK2, 'total'], 'all zero in 7d: hidden');
  assert.deepEqual(await rowsOf([...real, unknown(win(0, 0, 0, 0))], live, '30d'), [AK1, AK2, 'total'], 'all zero in 30d: hidden');
  for (const [label, w30] of [['tokens', win(0, 1000, 0, 0)], ['hours', win(0, 0, 0.5, 0)], ['sessions', win(0, 0, 0, 1)]]) {
    assert.deepEqual(await rowsOf([...real, unknown(w30)], live, '30d'), [AK1, AK2, 'unknown', 'total'], `${label} alone keep the row`);
  }
  assert.deepEqual(await rowsOf([...real, unknown(win(0, 0, 0, 0))], [...live, { kind: '7d', at: ISO(3600), resets_at: NOW + 100, session: 's8', acct: null }]), [AK1, AK2, 'unknown', 'total'],
    'a limit hit alone keeps it (it belongs to no account: the row is where it is counted)');
  // the history and nothing else: zero in this range -> the empty state, not a table of one total row
  const { w } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: [unknown(win(20, 5000000, 2, 4))], episodes: [], total: TOTALS({ accounts: 0, headroom_5h: [], headroom_7d: [] }) }) } });
  w.run('globalThis.Shell = { openCreate() {} }');
  await go(w);
  assert.equal(row(w, 'unknown'), null);
  assert.match(text(q(w, '[data-sec="accounts"] .ubody')), /No account seen yet/);
  clean(w, 'hidden history');
});

test('accounts: the gauges of a row are the Limits classes (warn from 60, bad from 85); a window whose reset has passed reads 0 % rolled over; no reading says so', async () => {
  const hot = ACCOUNTS({ rl_5h: { value: 91, resets_at: NOW + 600, at: ISO(30) } }, { rl_5h: null });
  const { w } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: hot }) } });
  await go(w);
  const g = (key, label) => row(w, key).querySelector(`.gauge[data-gauge="${label}"]`);
  assert.equal(text(g(AK1, '5H').querySelector('.g-val')), '91%');
  assert.ok(g(AK1, '5H').classList.contains('bad'));
  assert.match(text(g(AK1, '5H').querySelector('.g-reset')), /^resets in \d+m · \d\d:\d\d$/);
  assert.equal(g(AK1, '5H').querySelector('.g-bar i').style.width, '91%');
  assert.equal(text(g(AK1, '7D').querySelector('.g-val')), '71%');
  assert.ok(g(AK1, '7D').classList.contains('warn'));
  assert.equal(g(AK2, '5H'), null, 'a null reading draws no gauge');
  assert.equal(text(row(w, AK2).querySelector('[data-win="5H"] .ua-none')), 'no reading yet');
  assert.equal(text(g(AK2, '7D').querySelector('.g-val')), '83%');
  assert.ok(g(AK2, '7D').classList.contains('warn') && !g(AK2, '7D').classList.contains('bad'));
  const { w: w2 } = accWorld();
  await go(w2);
  const rolled = row(w2, AK2).querySelector('.gauge[data-gauge="5H"]');
  assert.equal(text(rolled.querySelector('.g-val')), '0%', 'reset since the reading: the new window has nothing counted');
  assert.match(text(rolled.querySelector('.g-reset')), /^resets in 3h5\dm · \d\d:\d\d$/, 'the NEXT reset (the old one + a whole window), never the words "window rolled over"');
  assert.ok(rolled.classList.contains('ok'));
  assert.match(rolled.getAttribute('title'), /^Work 5H: 0% used · resets .* · no usage recorded since the window reset · updated 2h ago · from the last session$/);
  assert.doesNotMatch(text(q(w2, '[data-sec="accounts"]')), /resets in now|rolled over/);
});

test('accounts: the range switches the numbers (today / 7 days / 30 days), the limit hits count the range\'s episodes, and the totals add up', async () => {
  const { w } = accWorld();
  await go(w);
  q(w, 'button[data-range="30d"]').click();
  await loading(w);
  const r1 = row(w, AK1);
  assert.deepEqual(['tokens', 'hours', 'sessions', 'usd'].map((c) => cell(r1, c)), ['90.0M', '30.0 h', '9', '$200.00']);
  assert.equal(cell(row(w, 'unknown'), 'tokens'), '5.0M', 'the history before tracking shows once the range reaches it');
  assert.deepEqual([AK1, AK2, 'unknown'].map((k) => cell(row(w, k), 'hits')), ['1', '1', '1'], '30 days reaches the episode of 8 days ago, which belongs to no account');
  assert.equal(cell(q(w, '.ua-row.total'), 'hits'), '3');
  // the sum of the rows is the total (tokens, hours, dollars)
  const sum = (c, f) => [AK1, AK2, 'unknown'].reduce((a, k) => a + f(cell(row(w, k), c)), 0);
  assert.equal(sum('tokens', (t) => parseFloat(t) * 1e6).toFixed(0), String(parseFloat(cell(q(w, '.ua-row.total'), 'tokens')) * 1e6));
  assert.equal(sum('usd', (t) => parseFloat(t.slice(1))).toFixed(2), '300.00');
  q(w, 'button[data-range="24h"]').click();
  await loading(w);
  assert.deepEqual(['tokens', 'hours', 'sessions', 'hits', 'usd'].map((c) => cell(row(w, AK1), c)), ['3.0M', '1.5 h', '2', '0', '$12.50'], 'today');
  assert.deepEqual(['tokens', 'sessions', 'usd'].map((c) => cell(row(w, AK2), c)), ['0', '0', '$0.00']);
  assert.match(q(w, '.uacc').getAttribute('aria-label'), /today/);
  clean(w, 'ranges');
});

test('accounts: the headroom line names the account with the most room in each window (info, dim, no button)', async () => {
  const { w } = accWorld();
  await go(w);
  const line = q(w, '.ua-room');
  assert.equal(text(line), 'most room: Work, 100 % of the 5-hour window left · Demo, 29 % of the 7-day window left');
  assert.equal(line.getAttribute('data-room'), 'info');
  assert.ok(line.classList.contains('dim') && !line.classList.contains('attn'));
  assert.equal(line.querySelector('button'), null);
});

test('accounts: the account in use at 85 % or more with room on another account turns the line into an amber callout, no button; no room elsewhere keeps it plain', async () => {
  const hot = ACCOUNTS({ rl_5h: { value: 91, resets_at: NOW + 600, at: ISO(30) } });
  const { w } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: hot, total: TOTALS({ headroom_5h: [{ key: AK2, left_pct: 100 }, { key: AK1, left_pct: 9 }] }) }) } });
  await go(w);
  const line = q(w, '.ua-room');
  assert.equal(line.getAttribute('data-room'), 'attention');
  assert.ok(line.classList.contains('attn'));
  assert.equal(text(line), 'Demo is at 91 % of the 5-hour window. most room: Work, 100 % of the 5-hour window left');
  assert.equal(line.querySelector('button'), null, 'a callout with no button');
  assert.equal(line.querySelector('a'), null);
  // 7 days wins when both are hot; the pick is the best other account for that window
  const both = ACCOUNTS({ rl_5h: { value: 91, resets_at: NOW + 600, at: ISO(30) }, rl_7d: { value: 88, resets_at: NOW + 86400, at: ISO(30) } });
  const { w: w2 } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: both, total: TOTALS({ headroom_7d: [{ key: AK2, left_pct: 40 }, { key: AK1, left_pct: 12 }] }) }) } });
  await go(w2);
  assert.equal(text(q(w2, '.ua-room')), 'Demo is at 88 % of the 7-day window. most room: Work, 40 % of the 7-day window left');
  // the other account has no more room than the one in use: nothing to switch to, the plain line
  const busy = ACCOUNTS({ rl_5h: { value: 91, resets_at: NOW + 600, at: ISO(30) } }, { rl_5h: { value: 96, resets_at: NOW + 3600, at: ISO(60) } });      // its window has NOT reset
  const { w: w3 } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: busy, total: TOTALS({ headroom_5h: [{ key: AK1, left_pct: 9 }, { key: AK2, left_pct: 4 }] }) }) } });
  await go(w3);
  assert.equal(q(w3, '.ua-room').getAttribute('data-room'), 'info');
  assert.match(text(q(w3, '.ua-room')), /^most room: Demo, 9 % of the 5-hour window left/);
  // an account below 85 % never raises it, however much room another one has
  const warm = ACCOUNTS({ rl_5h: { value: 84, resets_at: NOW + 600, at: ISO(30) } });
  const { w: w4 } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: warm, total: TOTALS({ headroom_5h: [{ key: AK2, left_pct: 100 }, { key: AK1, left_pct: 16 }] }) }) } });
  await go(w4);
  assert.equal(q(w4, '.ua-room').getAttribute('data-room'), 'info');
});

test('accounts: the amber callout carries "Switch to <name>" (primary tinted) only when that account has a saved login on a board that keeps them; it is the same switch as Settings', async () => {
  const hot = ACCOUNTS({ rl_5h: { value: 91, resets_at: NOW + 600, at: ISO(30) } });
  const over = { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: hot, total: TOTALS({ headroom_5h: [{ key: AK2, left_pct: 100 }, { key: AK1, left_pct: 9 }] }) }), '/api/accounts/': { ok: true, already: false, continued: [] } };
  const accts = (saved, supported = true) => ({ current: AK1, list: [{ key: AK1, name: 'Demo', email: 'demo@example.com', label: null, current: true, saved: true }, { key: AK2, name: 'Work', email: 'work@example.com', label: 'Work', current: false, saved }], store: { supported, reason: supported ? null : 'x', count: 1 } });
  const { w } = accWorld({ st: STATE({ accounts: accts(true) }), over });
  await go(w);
  const line = q(w, '.ua-room');
  assert.equal(line.getAttribute('data-room'), 'attention');
  assert.ok(line.classList.contains('attn'));
  assert.equal(text(line.querySelector('.ua-room-t')), 'Demo is at 91 % of the 5-hour window. most room: Work, 100 % of the 5-hour window left', 'the callout text is unchanged');
  const b = line.querySelector('button');
  assert.equal(text(b), 'Switch to Work');
  assert.ok(b.classList.contains('primary') && b.classList.contains('tinted'), 'the repeated-row primary, not a second filled one');
  assert.equal(qa(w, '.ua-room button').length, 1);
  b.click();
  await tick(); await tick();
  assert.deepEqual(calls(w).filter((c) => c.method === 'POST'), [{ method: 'POST', path: `/api/accounts/${AK2}/switch`, body: { continue_parked: true } }], 'accountSwitch, the one shared function');
  assert.equal(w.get('state.accounts.current'), AK2, 'the account in use moved at once');
  assert.match(text(w.document.getElementById('toasts')), /Switched to Work · running sessions follow within seconds/);
  // no saved login on the account with the room, or a board that keeps none: the callout stays as it is
  for (const [saved, supported] of [[false, true], [true, false]]) {
    const r = accWorld({ st: STATE({ accounts: accts(saved, supported) }), over });
    await go(r.w);
    assert.equal(r.w.document.querySelector('#page .ua-room').getAttribute('data-room'), 'attention');
    assert.equal(r.w.document.querySelector('#page .ua-room button'), null, `saved ${saved}, supported ${supported}: no button`);
    assert.equal(text(r.w.document.querySelector('#page .ua-room')), 'Demo is at 91 % of the 5-hour window. most room: Work, 100 % of the 5-hour window left');
  }
  // the button follows the saved logins without a reload of the page
  const r2 = accWorld({ st: STATE({ accounts: accts(false) }), over });
  await go(r2.w);
  assert.equal(r2.w.document.querySelector('#page .ua-room button'), null);
  r2.w.ctx.__st = STATE({ accounts: accts(true) });
  r2.w.run('state = __st; updateCurrentPage(state)');
  assert.equal(text(r2.w.document.querySelector('#page .ua-room button')), 'Switch to Work');
});

test('accounts: one account still shows one row and the total, in subscription terms; no headroom line, no chips on the limits', async () => {
  const one = [ACCOUNTS()[0]];
  const { w } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: one, total: TOTALS({ accounts: 1, headroom_5h: [{ key: AK1, left_pct: 58 }], headroom_7d: [{ key: AK1, left_pct: 29 }], '7d': win(60, 20000000, 9.5, 4) }) }) } });
  await go(w);
  assert.deepEqual(qa(w, '[data-sec="accounts"] .ua-row').map((r) => r.getAttribute('data-account') || 'total'), [AK1, 'total']);
  assert.equal(text(q(w, '.ua-count')), '1 account');
  assert.equal(cell(q(w, '.ua-row.total'), 'tokens'), '20.0M');
  assert.equal(q(w, '.ua-room'), null, 'one account has no "most room"');
  assert.equal(q(w, '[data-note="sessions"]'), null, 'and nothing to reconcile');
  assert.ok(q(w, '.ua-picks').classList.contains('hidden'), 'no chips with one account');
  assert.equal(qa(w, '.ua-pick').length, 0);
  assert.equal(chartCalls(w, 'line')[0].opts.names[0], 'rl_5h:claude');
  assert.equal(chartCalls(w, 'line')[0].opts.marks.length, 2, 'every episode stays on the chart: there is nobody to tell them apart from');
  clean(w, 'one account');
});

test('accounts: a server without accounts (or none seen yet) gets an empty state with its next step, never a blank or a skeleton', async () => {
  const { w } = usageWorld();
  w.run('globalThis.Shell = { openCreate() {} }');                                // the + session button needs the shell's create flow
  await go(w);
  const body = q(w, '[data-sec="accounts"] .ubody');
  assert.equal(qa(w, '[data-sec="accounts"] .usk').length, 0);
  assert.match(text(body), /No account seen yet.*within a minute.*\+ session.*\/login/);
  assert.ok(body.querySelector('button'), 'the + session button');
  const { w: w2 } = usageWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: [], total: { accounts: 0 } }) } });
  await go(w2);
  assert.match(text(q(w2, '[data-sec="accounts"] .ubody')), /No account seen yet/);
  const { w: w3 } = usageWorld({ over: { '/api/usage/summary?days=7': { __error: 'boom' } } });
  await go(w3);
  assert.match(text(q(w3, '[data-sec="accounts"] .uerr')), /Could not load the usage summary: boom/);
  clean(w, 'empty');
});

test('accounts: only the history before tracking (no real account yet) shows its row, the total and the way to get one', async () => {
  const history = { ...ACCOUNTS()[2], windows: { today: win(0, 0, 0, 0), '7d': win(20, 5000000, 2, 4), '30d': win(20, 5000000, 2, 4) } };            // something in the last 7 days
  const { w } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts: [history], total: TOTALS({ accounts: 0, headroom_5h: [], headroom_7d: [] }) }) } });
  await go(w);
  assert.deepEqual(qa(w, '[data-sec="accounts"] .ua-row').map((r) => r.getAttribute('data-account') || 'total'), ['unknown', 'total']);
  assert.equal(text(q(w, '.ua-count')), '0 accounts');
  assert.match(text(q(w, '[data-sec="accounts"]')), /No Claude account has been seen yet/);
  assert.equal(q(w, '.ua-room'), null);
  clean(w, 'unknown only');
});

const RENAME_HINT = 'Shown on the Usage page, the topbar chip and the session rows. Leave it empty to go back to the name Claude reports.';
const STATE_ACCTS = (over = {}) => STATE({ accounts: { current: AK1, list: [{ key: AK1, name: 'Demo', email: 'demo@example.com', label: null, current: true }, { key: AK2, name: 'Work', email: 'work@example.com', label: 'Work' }] }, ...over });

test('accounts: the pencil opens THE rename sheet of settings.js (one function, no second copy in usage.js): same title, hint, 60 characters, Save the one primary, PATCH {label}', async () => {
  const { w } = accWorld({ st: STATE_ACCTS() });
  await go(w);
  w.run('globalThis.__opened = []; { const real = settingsRenameAccount; settingsRenameAccount = (a) => { __opened.push(a.key); return real(a); }; }');
  const sheet = w.document.getElementById('sheet');
  assert.notEqual(sheet.open, true);
  const pencil = row(w, AK1).querySelector('.ua-edit');
  assert.equal(pencil.getAttribute('aria-label'), 'Rename Demo');
  pencil.click();
  assert.deepEqual(plain(w.get('__opened')), [AK1], 'the pencil calls settingsRenameAccount');
  assert.equal(sheet.open, true, 'one tap opens the sheet');
  assert.equal(text(sheet.querySelector('.sheet-title')), 'Rename account · Demo', 'the same title the Settings row shows');
  assert.equal(text(sheet.querySelector('.field-hint')), RENAME_HINT, 'and the same hint');
  const input = sheet.querySelector('input');
  assert.equal(input.value, '', 'no label yet');
  assert.equal(input.getAttribute('placeholder'), 'Demo');
  assert.equal(input.getAttribute('maxlength'), '60');
  const primaries = sheet.querySelectorAll('button.primary');
  assert.equal(primaries.length, 1, 'Save is the one primary');
  assert.equal(text(primaries[0]), 'Save');
  assert.deepEqual(sheet.querySelectorAll('button').map(text).filter(Boolean), ['Save', 'Cancel']);
  // no second implementation behind the pencil
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'usage.js'), 'utf8');
  assert.ok(!/Usage\.rename\b|Usage\.applyLabel\b|\bLABEL_MAX\b|P\.labels\b/.test(src), 'no rename sheet, label overlay or 60-character constant left in usage.js');
  assert.ok(!/api\(\s*'PATCH'/.test(src), 'usage.js sends no PATCH of its own');
  assert.equal(w.get('typeof Usage.rename'), 'undefined');
});

test('accounts: Save paints the label on every surface at once (the row, the Limits chips, the headroom line, the state list) while the sheet stays open and PATCH /api/accounts/<key> {label} runs; success closes it', async () => {
  const { w } = accWorld({ st: STATE_ACCTS() });
  await go(w);
  let release;
  w.ctx.__answers['/api/accounts/'] = () => new Promise((r) => { release = r; });
  const sheet = w.document.getElementById('sheet');
  row(w, AK1).querySelector('.ua-edit').click();
  const input = sheet.querySelector('input');
  input.value = '  Personal   max ';
  submit(sheet.querySelector('form'));
  await tick();
  assert.equal(sheet.open, true, 'open while the box answers');
  assert.equal(sheet.querySelector('button.primary').disabled, true);
  assert.deepEqual(calls(w).filter((c) => c.method === 'PATCH'), [{ method: 'PATCH', path: `/api/accounts/${AK1}`, body: { label: 'Personal max' } }], 'whitespace is tidied before it is sent');
  assert.equal(text(row(w, AK1).querySelector('.ua-chip')), 'Personal max', 'the row repaints before the answer, without a refetch');
  assert.equal(text(qa(w, '.ua-pick .ua-pick-l')[0]), 'Personal max', 'so do the Limits chips');
  assert.match(text(q(w, '.ua-room')), /Personal max, 29 % of the 7-day window left/, 'and the headroom line names the new label');
  assert.equal(w.get('state.accounts.list[0].label'), 'Personal max', 'the state list (the topbar chip, Settings, the session rows) is the one source');
  release({ ok: true });
  await tick(); await tick();
  assert.notEqual(sheet.open, true, 'the sheet closes once the box has it');
  assert.match(text(w.document.getElementById('toasts')), /Renamed to Personal max/);
  assert.equal(w.get('__polls'), 1, 'one fresh state to agree with the server');
  assert.equal(paths(w, '/api/usage/summary').length, 1, 'no summary refetch for a rename');
  // the 60 s refresh brings the server's summary (which has the name by then, or not yet): the page keeps the state's label
  w.run('Usage.cur.cache["7d"].summary = ' + JSON.stringify(ACC_SUMMARY()) + '; Usage.cur.sigs = {}; Usage.paintSummary(Usage.cur)');
  assert.equal(text(row(w, AK1).querySelector('.ua-chip')), 'Personal max');
});

test('accounts: a rename made in Settings (state.accounts.list changed, the summary not refetched) reaches the rows, the chips and the headroom line at the next poll, not at the next 60 s refetch', async () => {
  const { w } = accWorld({ st: STATE_ACCTS() });
  await go(w);
  assert.equal(text(row(w, AK2).querySelector('.ua-chip')), 'Work');
  const n = paths(w, '/api/usage/summary').length;
  const st = STATE_ACCTS();
  st.accounts.list[1].label = 'Client work';                                       // what the Settings sheet (or another tab, then a poll) put in the state
  w.ctx.__st = st;
  w.run('state = __st; updateCurrentPage(state)');                                 // the 3 s poll
  assert.equal(text(row(w, AK2).querySelector('.ua-chip')), 'Client work');
  assert.equal(text(qa(w, '.ua-pick .ua-pick-l')[1]), 'Client work');
  assert.deepEqual(qa(w, '.ua-edit').map((b) => b.getAttribute('aria-label')), ['Rename Demo', 'Rename Client work'], 'the pencil names it too');
  assert.match(text(q(w, '.ua-room')), /Client work, 100 % of the 5-hour window left/);
  assert.equal(paths(w, '/api/usage/summary').length, n, 'nothing was refetched');
  // a label cleared in Settings goes back to the account name here too
  const cleared = STATE_ACCTS();
  cleared.accounts.list[1].label = null;
  w.ctx.__st = cleared;
  w.run('state = __st; updateCurrentPage(state)');
  assert.equal(text(row(w, AK2).querySelector('.ua-chip')), 'Work', 'the name of the account (the summary says "Work" as its label and name)');
  // an unchanged poll leaves the rows alone
  const first = row(w, AK2);
  w.run('updateCurrentPage(state)');
  assert.equal(row(w, AK2), first);
  // an account the state does not list keeps the summary's label
  w.ctx.__st = STATE({ accounts: { current: AK1, list: [{ key: AK1, label: 'Mine' }] } });
  w.run('state = __st; updateCurrentPage(state)');
  assert.equal(text(row(w, AK1).querySelector('.ua-chip')), 'Mine');
  assert.equal(text(row(w, AK2).querySelector('.ua-chip')), 'Work');
  w.ctx.__st = STATE();                                                            // a state without accounts at all: the summary's labels
  w.run('state = __st; updateCurrentPage(state)');
  assert.equal(text(row(w, AK1).querySelector('.ua-chip')), 'Demo');
  clean(w, 'state labels');
});

test('accounts: renaming checks the 60 characters; a refused PATCH puts the old label back on every surface and says why in the sheet (and a toast), which stays open; an empty label goes back to the name', async () => {
  const { w } = accWorld({ st: STATE_ACCTS() });
  await go(w);
  const sheet = w.document.getElementById('sheet');
  row(w, AK2).querySelector('.ua-edit').click();
  const input = sheet.querySelector('input');
  assert.equal(input.value, 'Work', 'the label is prefilled');
  input.value = 'x'.repeat(61);
  submit(sheet.querySelector('form'));
  await tick();
  assert.match(text(sheet.querySelector('.field-err')), /At most 60 characters/);
  assert.equal(calls(w).filter((c) => c.method === 'PATCH').length, 0, 'nothing was sent');
  input.value = 'Team';
  w.ctx.__answers['/api/accounts/'] = { __error: 'account not found' };
  submit(sheet.querySelector('form'));
  await tick(); await tick();
  assert.equal(sheet.open, true, 'the sheet stays');
  assert.equal(text(sheet.querySelector('.field-err')), 'Rename failed: account not found', 'the reason, under the field');
  assert.match(text(w.document.getElementById('toasts')), /Rename failed: account not found/);
  assert.equal(text(row(w, AK2).querySelector('.ua-chip')), 'Work', 'the row went back to what it was');
  assert.equal(text(qa(w, '.ua-pick .ua-pick-l')[1]), 'Work', 'and so did the chip');
  assert.equal(w.get('state.accounts.list[1].label'), 'Work');
  assert.equal(sheet.querySelector('button.primary').disabled, false, 'Save can be tried again');
  assert.equal(input.value, 'Team', 'what was typed is still there');
  delete w.ctx.__answers['/api/accounts/'];
  input.value = '';
  submit(sheet.querySelector('form'));
  await tick(); await tick();
  assert.deepEqual(calls(w).filter((c) => c.method === 'PATCH').at(-1).body, { label: '' });
  assert.equal(text(row(w, AK2).querySelector('.ua-chip')), 'Work', 'an empty label falls back to the account name (also Work here)');
  assert.notEqual(sheet.open, true);
  assert.match(text(w.document.getElementById('toasts')), /Work is back to its own name/);
});

test('limits: with two accounts a chip each (default the current one, key=claude, its episodes only); one account shows none', async () => {
  const { w } = accWorld();
  await go(w);
  const picks = qa(w, '.ua-pick');
  assert.deepEqual(picks.map((b) => b.getAttribute('data-account')), [AK1, AK2], 'real accounts only: the pre-tracking history is no account');
  assert.equal(q(w, '.ua-picks').classList.contains('hidden'), false);
  assert.deepEqual(picks.map((b) => b.getAttribute('aria-pressed')), ['true', 'false'], 'the current account is the default');
  assert.deepEqual(picks.map((b) => text(b.querySelector('.ua-pick-l'))), ['Demo', 'Work']);
  assert.ok(picks[0].querySelector('.ua-dot').classList.contains('hue-blue'));
  assert.match(paths(w, '/api/series?')[0], /key=claude&/, 'the default request still follows the current account');
  const line = chartCalls(w, 'line').filter((c) => c.opts.names.some((n) => n.startsWith('rl_')));
  assert.equal(line.length, 1);
  assert.deepEqual(plain(line[0].opts.marks).map((m) => [m.label, m.cls]), [['5h limit', 'mark-5h']], 'the current account\'s episode only: the other account\'s and the untracked one are left out');
  clean(w, 'chips');
});

test('limits: choosing an account chip reloads only the series with key=acct:<key>, moves the gauges and the episodes to that account, and the current chip goes back to key=claude', async () => {
  const { w } = accWorld();
  await go(w);
  const before = calls(w).length;
  qa(w, '.ua-pick')[1].click();
  assert.ok(q(w, '.lim-chart').classList.contains('loading'), 'the skeleton until that account\'s series is here');
  await loading(w);
  const fresh = paths(w, '/api/').slice(before);
  assert.deepEqual(fresh, [`/api/series?series=rl_5h,rl_7d&key=acct:${AK2}&since=7d&points=${/points=(\d+)/.exec(paths(w, '/api/series?')[0])[1]}`], 'one request: the series, nothing else');
  assert.deepEqual(qa(w, '.ua-pick').map((b) => b.getAttribute('aria-pressed')), ['false', 'true']);
  const last = chartCalls(w, 'line').filter((c) => c.opts.names.some((n) => n.startsWith('rl_'))).at(-1);
  assert.deepEqual(plain(last.opts.names), [`rl_5h:acct:${AK2}`, `rl_7d:acct:${AK2}`]);
  assert.deepEqual(plain(last.opts.labels), { [`rl_5h:acct:${AK2}`]: '5H', [`rl_7d:acct:${AK2}`]: '7D' });
  assert.deepEqual(plain(last.opts.marks).map((m) => [m.label, m.cls]), [['7d limit', 'mark-7d']], 'only that account\'s episode');
  // the two gauges above the chart are that account's windows now, and the note says so
  const g5 = q(w, '.uc-gauges .gauge[data-gauge="5H"]');
  const g7 = q(w, '.uc-gauges .gauge[data-gauge="7D"]');
  assert.equal(text(g7.querySelector('.g-val')), '83%');
  assert.equal(text(g5.querySelector('.g-val')), '0%', 'its 5-hour window rolled over since the reading');
  assert.match(text(q(w, '[data-sec="limits"] .unote')), /^Gauges show Work's last statusline readings, 2h ago: it is not the account in use\.$/);
  w.get('pages').usage.update(STATE());                                           // the 3 s poll reports the current account: the gauges stay on the picked one
  assert.equal(text(g7.querySelector('.g-val')), '83%');
  // a range change keeps the account
  q(w, 'button[data-range="30d"]').click();
  await loading(w);
  assert.ok(paths(w, '/api/series?').at(-1).includes(`key=acct:${AK2}&since=30d`), paths(w, '/api/series?').at(-1));
  // and the current account's chip is the way back (no acct: key: claude follows whoever is logged in)
  qa(w, '.ua-pick')[0].click();
  await loading(w);
  assert.match(paths(w, '/api/series?').at(-1), /key=claude&since=30d/);
  assert.equal(text(q(w, '.uc-gauges .gauge[data-gauge="7D"] .g-val')), '59%', 'the live reading of the state again');
  assert.deepEqual(qa(w, '.ua-pick').map((b) => b.getAttribute('aria-pressed')), ['true', 'false']);
  assert.deepEqual(plain(chartCalls(w, 'line').at(-1).opts.names), ['rl_5h:claude', 'rl_7d:claude']);
  clean(w, 'account switch');
});

test('limits: a late series of the account left behind is dropped; an account with no samples says so; a refresh keeps the pick', async () => {
  const { w, timers } = accWorld();
  await go(w);
  let release;
  w.ctx.__hold = new Promise((r) => { release = r; });
  w.ctx.__answers['/api/series?series=rl_5h,rl_7d'] = (p) => (p.includes('key=acct:') ? w.get('__hold').then(() => SERIES_OF(AK2)) : SERIES());
  const lines = chartCalls(w, 'line').length;
  qa(w, '.ua-pick')[1].click();
  qa(w, '.ua-pick')[0].click();                                                   // back to the current account before Work's series arrived
  await loading(w);
  release();
  await tick(); await tick();
  assert.equal(plain(chartCalls(w, 'line').at(-1).opts.names)[0], 'rl_5h:claude', 'Work\'s late answer was not drawn');
  assert.ok(chartCalls(w, 'line').length >= lines);
  assert.deepEqual(qa(w, '.ua-pick').map((b) => b.getAttribute('aria-pressed')), ['true', 'false']);
  // no samples for the picked account: the empty state names it
  w.ctx.__answers['/api/series?series=rl_5h,rl_7d'] = (p) => (p.includes('key=acct:') ? { since: ISO(86400), until: ISO(0), step: 600, t: [NOW - 600, NOW], series: {}, meta: {} } : SERIES());
  qa(w, '.ua-pick')[1].click();
  await loading(w);
  assert.match(text(q(w, '[data-sec="limits"] .uslot')), /No samples for Work in this range.*another account/);
  assert.ok(q(w, '.lim-chart').classList.contains('hidden'));
  // the 60 s refresh keeps asking for the picked account
  const n = paths(w, '/api/series?').length;
  timers[0].fn();
  await loading(w);
  assert.ok(paths(w, '/api/series?').slice(n).every((p) => p.includes(`key=acct:${AK2}`)));
  clean(w, 'late');
});

test('accounts: with charts.js missing the Accounts section still paints, renames and picks; only the chart sections show their inline error', async () => {
  const { w } = accWorld({ charts: false });
  await go(w);
  assert.equal(qa(w, '[data-sec="accounts"] .uerr').length, 0);
  assert.equal(qa(w, '[data-sec="accounts"] .ua-row').length, 3, 'two accounts and the total (the history row has nothing in 7 days)');
  assert.equal(cell(row(w, AK1), 'tokens'), '20.0M', 'the fallback formatter');
  assert.equal(cell(row(w, AK1), 'usd'), '$60.00');
  assert.ok(qa(w, '.ua-pick').length === 2);
  qa(w, '.ua-pick')[1].click();
  await loading(w);
  assert.match(text(q(w, '[data-sec="limits"] .uerr')), /charts\.js did not load|chart library did not load/);
  clean(w, 'no charts');
});

test('accounts: the rows are left alone while nothing moved and repainted when a minute has passed (the countdowns); an unmount leaves no timer', async () => {
  const { w, timers } = accWorld();
  w.run(`globalThis.__clock = Date.now(); Date.now = () => __clock;`);            // a frozen clock: no minute boundary can fall inside the test
  await go(w);
  const first = row(w, AK1);
  const countdown = (r) => text(r.querySelector('.gauge[data-gauge="5H"] .g-reset'));
  const before = countdown(first);
  assert.match(before, /^resets in [23]h\d*m? · \d\d:\d\d$/);
  w.run('Usage.paintSummary(Usage.cur)');
  assert.equal(row(w, AK1), first, 'same minute, same numbers: the same nodes');
  w.run('Date.now = () => __clock + 61000');
  timers[0].fn();                                                                  // the 60 s refresh
  await loading(w);
  assert.notEqual(row(w, AK1), first, 'a new minute repaints the rows');
  assert.notEqual(countdown(row(w, AK1)), before, 'with the countdown moved on');
  assert.equal(qa(w, '.ua-row').length, 3);
  assert.equal(text(row(w, AK1).querySelector('.ua-chip')), 'Demo');
  w.run('pages.usage.unmount()');
  assert.equal(w.get('Usage.cur'), null);
});

test('charts.css: the accounts section is full width on a wide grid, a card below 700 px of its own width and a table from there (a 1024 px tablet gets the table), with 44 px targets on touch', () => {
  const css = fs.readFileSync(path.join(STATIC, 'charts.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  assert.match(css, /#page \.ugrid > \.usec\[data-sec="accounts"\][^{]*\{\s*grid-column:\s*1 \/ -1/, 'a full-width row of the two-column grid');
  assert.match(css, /#page \.usec\[data-sec="accounts"\]\s*\{[^}]*container-type:\s*inline-size/, 'the section is the container: the sidebar takes width a viewport query would not see');
  const table = /@container accsec \(min-width:\s*700px\)\s*\{([\s\S]*?)\n\}/.exec(css);
  assert.ok(table, 'the table layout lives in a container query that starts at 700 px: the section of a 1024 x 768 tablet (1024 - 260 sidebar - gutters) is about 730');
  assert.ok(!/min-width:\s*760px/.test(css), 'and no 760 px threshold is left');
  assert.match(table[1], /\.uacc-head, #page \.ua-row \{ display:grid; grid-template-columns:(?:minmax\([^)]*\)\s*){8}/, 'eight columns: account, 5H, 7D, tokens, hours, sessions, hits, dollars');
  // the table must fit the narrowest section it is used in: the sum of the column minimums + the gaps + the row padding is at most 700 px
  const cols = /grid-template-columns:((?:\s*minmax\([^)]*\))+)\s*;\s*gap:0 (\d+)px/.exec(table[1]);
  assert.ok(cols, 'the columns and the column gap');
  const mins = [...cols[1].matchAll(/minmax\((\d+)px/g)].map((m) => Number(m[1]));
  const pad = Number(/#page \.ua-row \{[^}]*padding:\s*\d+px (\d+)px/.exec(table[1])[1]);
  assert.equal(mins.length, 8);
  assert.ok(mins.reduce((a, b) => a + b, 0) + 7 * Number(cols[2]) + 2 * pad <= 700, `the table needs ${mins.reduce((a, b) => a + b, 0) + 7 * Number(cols[2]) + 2 * pad} px at the least: it must fit the 700 px it starts at`);
  assert.doesNotMatch(css.replace(table[0], ''), /#page \.uacc-head \{[^}]*display:\s*(?:grid|flex)/, 'under the table width the header row is gone');
  assert.match(css, /#page \.uacc-head \{ display:none; \}/);
  assert.match(css, /@media \(pointer:coarse\) \{ #page \.ua-edit \{ min-width:var\(--tap\); \} \}/);
  assert.match(css, /html\.force-coarse #page \.ua-edit \{ min-width:var\(--tap\); \}/);
  assert.doesNotMatch(css, /#page \.ua-row[^{]*\{[^}]*(?:min-width:\s*\d{3,}px|white-space:\s*nowrap)/, 'a card never forces a width');
  assert.match(css, /#page \.ua-room\.attn \{[^}]*var\(--mute-amber-bd\)[^}]*var\(--mute-amber-bg\)[^}]*var\(--mute-amber\)/, 'the muted amber tokens, not the loud warn colour');
});

test('accounts: the card header keeps the rename pencil beside the identity for every account: name, plan, current marker and email wrap inside .ua-who, the pencil is its own non-wrapping flex item', async () => {
  const { w } = accWorld();
  await go(w);
  for (const key of [AK1, AK2]) {
    const id = row(w, key).querySelector('.ua-id');
    assert.deepEqual(id.children.map((n) => n.className.split(' ')[0]), ['ua-who', 'icon'], `${key}: the header is who + pencil`);
    assert.ok(id.children[1].classList.contains('ua-edit'), 'the pencil is a direct child of the header, not inside the wrapping part');
    assert.equal(id.querySelector('.ua-who .ua-edit'), null);
  }
  assert.ok(row(w, AK1).querySelector('.ua-who .ua-current') && row(w, AK1).querySelector('.ua-who .ua-email'), 'the current account\'s long line (chip, plan, current, email) is what used to push the pencil down');
  const css = fs.readFileSync(path.join(STATIC, 'charts.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const header = /#page \.ua-id \{([^}]*)\}/.exec(css);
  assert.ok(header && /display:\s*flex/.test(header[1]) && !/flex-wrap:\s*wrap/.test(header[1]), 'the header itself never wraps');
  assert.match(css, /#page \.ua-who \{[^}]*flex:1 1 0;[^}]*display:flex;[^}]*flex-wrap:wrap/, 'the identity wraps inside its own box');
  assert.match(css, /#page \.ua-edit \{[^}]*flex:none/, 'the pencil keeps its size and its place');
});

test('accounts: the Total row\'s dollar cell says the figure is rounded (the rows are rounded one by one: a $1 gap is not an error); the rows keep the API-equivalent hint', async () => {
  const { w } = accWorld();
  await go(w);
  assert.match(q(w, '.ua-row.total [data-col="usd"]').getAttribute('title'), /rounded/i);
  assert.match(row(w, AK1).querySelector('[data-col="usd"]').getAttribute('title'), /API list price/);
  assert.doesNotMatch(row(w, AK1).querySelector('[data-col="usd"]').getAttribute('title'), /rounded/i);
});

// ---------------------------------------------------------------- demo mode (?demo=1): the fixtures through the real api()

/** The Usage page on the demo fixtures: core.js's real api() in demo mode (fetch serves /static/demo/*.json), the state rebased the way poll() gets it. */
function demoUsageWorld() {
  const w = makeWorld({
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    setInterval: () => 1, clearInterval() {},
    fetch: async (url) => {
      const m = /^\/static\/demo\/(\w+)\.json$/.exec(String(url));
      if (!m) throw new Error(`demo mode asked for ${url}`);
      const body = fs.readFileSync(path.join(STATIC, 'demo', `${m[1]}.json`), 'utf8');
      return { ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(body) };
    },
  });
  installDom(w);
  w.location.search = '?demo=1';
  for (const f of ['core.js', 'components.js', 'keymap.js', 'router.js', 'pages/agents.js', 'pages/settings.js']) w.load(f);
  w.run(CHARTS_STUB);
  w.load('pages/usage.js');
  w.ctx.__demo = fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8');
  w.run('globalThis.__clock = Date.now(); Date.now = () => __clock;');            // one instant for every rebase: the state's and the summary's reset times then agree to the second
  w.run('state = demoRebase(JSON.parse(__demo))');
  return w;
}

test('demo mode: the Accounts rows read the same 5H percentage and countdown as the topbar pill and the Limits gauge, and the 7D reset time is the gauge\'s; every other section still paints', async () => {
  const w = demoUsageWorld();
  await go(w);
  assert.equal(row(w, 'unknown'), null, 'the history row has nothing in 7 days');
  const r1 = qa(w, '[data-sec="accounts"] .ua-row[data-current]')[0];
  assert.ok(r1, 'the account in use');
  const g5 = r1.querySelector('.gauge[data-gauge="5H"]');
  const g7 = r1.querySelector('.gauge[data-gauge="7D"]');
  const lim5 = q(w, '.uc-gauges .gauge[data-gauge="5H"]');
  const lim7 = q(w, '.uc-gauges .gauge[data-gauge="7D"]');
  const pct = w.get('state.usage.value.five_hour.used_percentage');
  assert.equal(text(g5.querySelector('.g-val')), `${pct}%`, 'the Accounts row says what the topbar pill says (state.usage), not "0%"');
  assert.equal(text(g5.querySelector('.g-val')), text(lim5.querySelector('.g-val')));
  assert.match(text(g5.querySelector('.g-reset')), /^resets in \d+[hm]/, 'with a countdown, not "window rolled over"');
  assert.equal(text(g5.querySelector('.g-reset')), text(lim5.querySelector('.g-reset')), 'the same countdown as the Limits gauge');
  assert.equal(text(g7.querySelector('.g-val')), text(lim7.querySelector('.g-val')));
  assert.equal(text(g7.querySelector('.g-reset')), text(lim7.querySelector('.g-reset')), 'the 7D reset time of the row is the gauge\'s');
  assert.doesNotMatch(text(q(w, '[data-sec="accounts"]')).replace(/Work[\s\S]*$/, ''), /rolled over/, 'only the other account, whose window did reset, says so');
  // the rebase also shifts the ISO strings of the other sections: they must still draw
  assert.equal(qa(w, 'section.usec .uerr').length, 0, qa(w, 'section.usec .uerr').map(text).join(' | '));
  assert.equal(qa(w, '.usk').length, 0, 'no skeleton left');
  assert.equal(chartCalls(w, 'stackedBars').at(-1).days.length, 7, 'the daily bars');
  assert.equal(chartCalls(w, 'heatmap').length, 1, 'the heatmap');
  assert.ok(qa(w, '.usessions .srow').length >= 2, 'the top sessions');
  assert.match(text(q(w, '[data-sec="projects"] .ubody')), /phasezero/, 'the projects section');
  const hits = (key) => cell(row(w, key), 'hits');
  assert.deepEqual([hits('7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001'), hits('7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a002')], ['2', '1'], 'the episodes keep their age: the fixture\'s limit hits are inside the last 7 days now as then');
  clean(w, 'demo usage');
});

// ---------------------------------------------------------------- the real charts.js

const REAL = path.join(STATIC, 'charts.js');
test('against the real charts.js (stub uPlot): no section is blank, no error block, no stray undefined/NaN', { skip: !fs.existsSync(REAL) && 'charts.js does not exist yet' }, async () => {
  const { w } = usageWorld({ charts: false });
  w.run(`globalThis.__uplots = []; globalThis.uPlot = class { constructor(o, d, h) { __uplots.push({ o, d, h }); this.root = document.createElement('div'); if (h && h.append) h.append(this.root); }
    setData() {} setSize() {} destroy() {} redraw() {} };`);
  w.load('charts.js');
  await go(w);
  assert.equal(qa(w, '.uerr').length, 0, qa(w, '.uerr').map(text).join(' | '));
  for (const id of ['cost', 'activity', 'timeline']) assert.ok(q(w, `[data-sec="${id}"] .chart`).children.length > 0, `${id}: Charts drew into its host`);
  assert.ok(w.get('__uplots').length >= 1, 'uPlot was constructed for the limits chart');
  assert.equal(qa(w, '.usk').length, 0);
  qa(w, 'button.srow-toggle')[0].click();
  await tick(); await tick();
  assert.ok(w.get('__uplots').length >= 3, 'and for the ctx and cost charts of the opened session');
  q(w, 'button[data-range="30d"]').click();
  await loading(w);
  assert.equal(qa(w, '.uerr').length, 0);
  clean(w, 'real charts');
  w.run('pages.usage.unmount()');
});

test('against the real charts.js (stub uPlot): the account chips rebuild the limits chart for another account, no error block, no stray undefined/NaN', { skip: !fs.existsSync(REAL) && 'charts.js does not exist yet' }, async () => {
  const { w } = accWorld({ charts: false });
  w.run(`globalThis.__uplots = []; globalThis.uPlot = class { constructor(o, d, h) { __uplots.push({ o, d, h }); this.root = document.createElement('div'); if (h && h.append) h.append(this.root); }
    setData() {} setSize() {} destroy() {} redraw() {} };`);
  w.load('charts.js');
  await go(w);
  assert.equal(qa(w, '.uerr').length, 0, qa(w, '.uerr').map(text).join(' | '));
  const n = w.get('__uplots').length;
  qa(w, '.ua-pick')[1].click();
  await loading(w);
  assert.equal(qa(w, '.uerr').length, 0, qa(w, '.uerr').map(text).join(' | '));
  assert.ok(w.get('__uplots').length > n, 'another series set builds another chart');
  assert.equal(qa(w, '[data-sec="accounts"] .ua-row').length, 3);
  clean(w, 'real charts, accounts');
  w.run('pages.usage.unmount()');
});

// ---------------------------------------------------------------- time-aware windows and the freshness caption (v0.5.17f)

const fresh = (w) => q(w, '[data-sec="limits"] [data-fresh]');
const gaugeOf = (w, label) => q(w, `.uc-gauges .gauge[data-gauge="${label}"]`);
const usageAt = (five, seven, at, source) => ({ value: { five_hour: five, seven_day: seven, ...(source ? { source } : {}) }, at });

test('limits: the freshness caption sits right under the Limits title and says how old the reading is and where it came from (a session, Claude Code\'s cache, or none yet)', async () => {
  const st = (over) => STATE({ usage: usageAt({ used_percentage: 42, resets_at: NOW + 3600 }, { used_percentage: 59, resets_at: NOW + 3 * 86400 }, ISO(300), over) });
  const { w } = usageWorld({ st: st() });
  await go(w);
  const sec = q(w, '[data-sec="limits"]');
  const row = sec.querySelector('.ubody').children[0];
  assert.ok(row.classList.contains('ufresh-row') && row.children[0] === fresh(w), 'the first thing under the title, above the account chips and the gauges: the caption leads its row (v0.5.17f part 2: the Refresh button follows it)');
  assert.equal(text(fresh(w)), 'updated 5m ago · from the last session');
  assert.ok(!fresh(w).classList.contains('hidden'));
  const { w: w2 } = usageWorld({ st: st('cache') });
  await go(w2);
  assert.equal(text(fresh(w2)), "updated 5m ago · from Claude Code's cache");
  // the newer of the two readings speaks: the 5-hour window was read later than the weekly one
  const { w: w3 } = usageWorld({ st: STATE({ usage: usageAt({ used_percentage: 42, resets_at: NOW + 3600, at: ISO(120), source: 'cache' }, { used_percentage: 59, resets_at: NOW + 3 * 86400 }, ISO(3600)) }) });
  await go(w3);
  assert.equal(text(fresh(w3)), "updated 2m ago · from Claude Code's cache");
  const { w: w4 } = usageWorld({ st: { projects: [] }, over: { '/api/usage/summary?days=7': SUMMARY(DAILY7, { rate_limits: {} }) } });
  await go(w4);
  assert.equal(text(fresh(w4)), 'no reading yet');
  assert.ok(!fresh(w4).classList.contains('hidden'));
});

test('limits: the caption follows the account a chip picked, and the age ticks with the poll (no refetch)', async () => {
  const { w } = accWorld();
  await go(w);
  assert.match(text(fresh(w)), /^updated \d+s ago · from the last session$/, 'the live reading of the state (read a second ago)');
  qa(w, '.ua-pick')[1].click();
  await loading(w);
  assert.equal(text(fresh(w)), 'updated 2h ago · from the last session', 'Work was read two hours ago');
  const before = calls(w).length;
  w.get('pages').usage.update(STATE());
  assert.equal(calls(w).length, before, 'the poll never fetches');
});

test('limits: a window whose reset has passed with no newer reading reads 0 %, counts down to the NEXT reset and says nothing was recorded; a window not yet reset keeps its percentage', async () => {
  const st = STATE({ usage: usageAt({ used_percentage: 91, resets_at: NOW - 3600 }, { used_percentage: 59, resets_at: NOW + 3 * 86400 }, ISO(3 * 3600)) });
  const { w } = usageWorld({ st });
  await go(w);
  const g5 = gaugeOf(w, '5H');
  assert.ok(!g5.classList.contains('hidden'), 'a rolled window is a gauge, not a missing one');
  assert.equal(text(g5.querySelector('.g-val')), '0%');
  assert.ok(g5.classList.contains('ok'));
  assert.equal(g5.querySelector('.g-bar i').style.width, '0%');
  assert.match(text(g5.querySelector('.g-reset')), /^resets in [34]h\d+m · \d\d:\d\d$/, 'one window after the reset that passed: 5 h - 1 h');
  assert.match(g5.getAttribute('title'), /^5H window: 0% used · resets .* · no usage recorded since the window reset · updated 3h ago · from the last session$/);
  assert.equal(text(gaugeOf(w, '7D').querySelector('.g-val')), '59%', 'the weekly window has not reset: its last reading stands');
  assert.match(text(gaugeOf(w, '7D').querySelector('.g-reset')), /^resets in 2d\d+h/);
  assert.doesNotMatch(text(root(w)), /rolled over/);
  clean(w, 'rolled gauge');
});

test('limits: several missed windows are skipped (the next reset is the first one ahead), for the 5-hour and the weekly window', async () => {
  const st = STATE({ usage: usageAt({ used_percentage: 100, resets_at: NOW - 5 * 18000 - 100 }, { used_percentage: 71, resets_at: NOW - 3 * 604800 - 3600 }, ISO(30 * 86400)) });
  const { w } = usageWorld({ st });
  await go(w);
  assert.equal(text(gaugeOf(w, '5H').querySelector('.g-val')), '0%');
  assert.match(text(gaugeOf(w, '5H').querySelector('.g-reset')), /^resets in 4h5\dm/, '5 h x 6 - (5 h x 5 + 100 s) = 4 h 58 m');
  assert.equal(text(gaugeOf(w, '7D').querySelector('.g-val')), '0%');
  assert.match(text(gaugeOf(w, '7D').querySelector('.g-reset')), /^resets in 6d2[23]h/, '4 weeks - (3 weeks + 1 h) = 6 d 23 h (a few ms of the clock may tip it to 22 h)');
  assert.match(text(fresh(w)), /^updated 30d ago · /);
});

test('limits: with the weekly window missing from the record the gauge keeps the reading the server filled in (and rolls it over when its reset has passed)', async () => {
  const st = STATE({ usage: usageAt({ used_percentage: 12, resets_at: NOW + 3600 }, { used_percentage: 64, resets_at: NOW - 600, at: ISO(8 * 3600), source: 'cache' }, ISO(60)) });
  const { w } = usageWorld({ st });
  await go(w);
  assert.equal(text(gaugeOf(w, '7D').querySelector('.g-val')), '0%');
  assert.match(gaugeOf(w, '7D').getAttribute('title'), /no usage recorded since the window reset · updated 8h ago · from Claude Code's cache$/, 'the window\'s own time and source, not the record\'s');
  assert.equal(text(gaugeOf(w, '5H').querySelector('.g-val')), '12%');
  assert.match(gaugeOf(w, '5H').getAttribute('title'), /updated 1m ago · from the last session$/);
});

test('accounts: the gauge titles say when and where each account was read, a rolled window counts down to the next reset, and the room line counts it as open', async () => {
  const cache = (r) => ({ ...r, source: 'cache' });
  const accounts = ACCOUNTS({}, { rl_5h: cache({ value: 100, resets_at: NOW - 3600, at: ISO(7200) }), rl_7d: cache({ value: 83, resets_at: NOW + 2 * 86400, at: ISO(7200) }) });
  const stale = TOTALS({ headroom_5h: [{ key: AK1, left_pct: 58 }, { key: AK2, left_pct: 0 }] });          // the ranking of a summary fetched before the window reset
  const { w } = accWorld({ over: { '/api/usage/summary?days=7': ACC_SUMMARY({ accounts, total: stale }) } });
  await go(w);
  const g = (key, label) => row(w, key).querySelector(`.gauge[data-gauge="${label}"]`);
  assert.match(g(AK2, '5H').getAttribute('title'), /^Work 5H: 0% used · resets .* · no usage recorded since the window reset · updated 2h ago · from Claude Code's cache$/);
  assert.match(g(AK2, '7D').getAttribute('title'), /^Work 7D: 83% used · resets .* · updated 2h ago · from Claude Code's cache$/);
  assert.match(g(AK1, '5H').getAttribute('title'), /^Demo 5H: 42% used · resets .* · updated 1m ago · from the last session$/);
  assert.equal(text(q(w, '.ua-room')), 'most room: Work, 100 % of the 5-hour window left · Demo, 29 % of the 7-day window left');
  assert.doesNotMatch(text(q(w, '[data-sec="accounts"]')), /rolled over/);
});


// ---------------------------------------------------------------- the claude-mem health tile (v0.5.20, issue #8)

test('the Usage page ends with the compact claude-mem tile when state.memory exists, outside the seven sections, and has none without it', async () => {
  const { w } = usageWorld({ st: STATE({ memory: { state: 'up', version: '13.31.0', observations: 11836, queue_depth: 12, processing: true, active_sessions: 2, last_error: null, rates: { obs: { d1: 61.5, d7: 74.2 }, sum: { d1: 4, d7: 5.1 } } } }) });
  w.load('pages/memory.js');
  await go(w);
  const tile = q(w, '.mem-usage .mem-tile');
  assert.ok(tile, 'the tile is on the page');
  assert.equal(q(w, '.mem-usage').classList.contains('hidden'), false);
  assert.match(text(tile), /Observations11,836 61\.5 per day over 24 h/);
  assert.doesNotMatch(text(tile), /Summaries|Active sessions/, 'the small tile');
  assert.equal(tile.querySelector('a').getAttribute('href'), '#/memory');
  assert.deepEqual(qa(w, 'section.usec').map((s) => s.getAttribute('data-sec')), ['accounts', 'limits', 'cost', 'sessions', 'projects', 'activity', 'timeline'], 'the tile is no eighth section');
  const none = usageWorld();
  none.w.load('pages/memory.js');
  await go(none.w);
  assert.equal(q(none.w, '.mem-usage .mem-tile'), null);
  assert.ok(q(none.w, '.mem-usage').classList.contains('hidden'), 'hidden without state.memory');
});


// ---------------------------------------------------------------- sessions started outside the board, matched by folder (issue #57)

const JOINED_SUMMARY = () => SUMMARY(DAILY7, {
  windows: { today: WINDOW(44.56), '30d': WINDOW(300),
    '7d': { total: 70, by_agent: { claude: { total: 70, tokens: 1 } }, by_project: [
      { project: '(unattributed)', total: 5, hours: 0 }, { project: '(outside projects)', total: 7.25, hours: 0 },
      { project: 'ccboard', total: 40, hours: 2, joined: 12.5 }, { project: 'Phasezero', total: 17.75, hours: 1 }] } },
  top_sessions: [
    { key: `claude:${UUID1}`, project: 'ccboard', repo: 'api', agent: 'claude', total: 12.5, hours: 1.2, tokens: 1800, models: ['claude-sonnet-5-5'], via: 'folder' },
    { key: `claude:${UUID2}`, project: 'Phasezero', repo: 'root', agent: 'claude', total: 5, hours: 0, tokens: 900, models: [] },
    { key: `claude:${UUID3}`, project: '(outside projects)', repo: null, agent: 'claude', total: 7.25, hours: 0.4, tokens: 500, models: [], via: 'folder' },
  ] });

test('projects: the two labels without a project are their own last rows with a one-line meaning each, and a project says how much was joined by folder', async () => {
  const { w } = usageWorld({ over: { '/api/usage/summary?days=7': JOINED_SUMMARY() } });
  await go(w);
  const rows = qa(w, 'tr.prow');
  assert.deepEqual(rows.map((r) => r.getAttribute('data-project')), ['ccboard', 'Phasezero', '(outside projects)', '(unattributed)'], 'named by cost, then the two labels, never ranked against projects');
  const out = rows[2];
  assert.equal(out.getAttribute('title'), 'sessions that ran in a folder outside the projects folder');
  assert.equal(out.querySelector('[data-outside]').getAttribute('title'), 'sessions that ran in a folder outside the projects folder');
  assert.equal(text(out.querySelector('.p-tip')), 'sessions that ran in a folder outside the projects folder', 'the meaning is text, not only a tooltip');
  assert.equal(out.querySelector('a'), null, 'no project to link to');
  assert.equal(text(rows[3].querySelector('.p-tip')), 'sessions the board did not start, with no folder on record');
  const joined = rows[0].querySelector('[data-joined]');
  assert.equal(text(joined), '$12.50 joined by folder', 'readable without hovering');
  assert.match(joined.getAttribute('title'), /the folder the session ran in/);
  assert.equal(rows[1].querySelector('[data-joined]'), null, 'a project with nothing joined says nothing');
  assert.equal(rows[0].querySelector('a').getAttribute('href'), '#/p/ccboard');
  assert.match(text(q(w, '[data-note="joined"]')), /never counted in a task/);
  clean(w);
});

test('projects: without any joined cost there is no joined caption or note', async () => {
  const { w } = usageWorld();
  await go(w);
  assert.equal(q(w, '[data-joined]'), null);
  assert.equal(q(w, '[data-note="joined"]'), null);
  clean(w);
});

test('sessions: a session matched by its folder says so in text, the others do not, and (outside projects) has no project link', async () => {
  const { w } = usageWorld({ over: { '/api/usage/summary?days=7': JOINED_SUMMARY() } });
  await go(w);
  const rows = qa(w, 'tr.srow');
  assert.equal(rows.length, 3);
  assert.equal(text(rows[0].querySelector('[data-via="folder"]')), 'joined by folder');
  assert.match(rows[0].querySelector('[data-via="folder"]').getAttribute('title'), /the board did not start these/);
  assert.equal(rows[1].querySelector('[data-via]'), null);
  assert.equal(text(rows[2].querySelector('[data-via="folder"]')), 'joined by folder');
  assert.equal(rows[2].querySelector('.srow-proj'), null, 'the outside label is not a project');
  assert.ok(rows[0].querySelector('.srow-proj'), 'a joined session still links to its project');
  clean(w);
});

test('cost: the outside label gets its own note under the bars with its meaning', async () => {
  const { w } = usageWorld({ over: { '/api/usage/summary?days=7': JOINED_SUMMARY() } });
  await go(w);
  const note = q(w, '[data-note="outside"]');
  assert.ok(note, 'a note for the outside bucket');
  assert.equal(note.getAttribute('title'), 'sessions that ran in a folder outside the projects folder');
  assert.match(text(note), /^\(outside projects\) \$7\.25 in this window: sessions that ran in a folder outside the projects folder\.$/);
  clean(w);
});

test('the unassigned helpers: both labels are recognised, and neither is a route', () => {
  const { w } = usageWorld();
  const U = w.get('Usage');
  assert.equal(U.isUnassigned('(outside projects)'), true);
  assert.equal(U.isUnassigned('(unattributed)'), true);
  assert.equal(U.isUnassigned('ccboard'), false);
  assert.equal(U.projectHash('(outside projects)'), null);
  assert.equal(U.projectHash('(unattributed)'), null);
  assert.equal(U.unassignedTip('ccboard'), '');
});


// ---------------------------------------------------------------- the Reported / Estimated basis (issue #95)

const EST_BLOCK = { date: '2026-10-07', source: 'Anthropic pricing page (list prices per million tokens)', sessions: 3, usd: 51.2, bases: { list: 2, sibling: 1 }, cache_write_assumed: true, cache_write_x: 1.25 };
const EST_SUMMARY = () => SUMMARY(DAILY7, {
  basis: 'est', estimate: EST_BLOCK,
  windows: { today: WINDOW(44.56), '30d': WINDOW(300),
    '7d': { total: 95.76, by_agent: { claude: { total: 95.76, tokens: 1 } }, by_project: [{ project: 'ccboard', total: 80.5, hours: 2 }, { project: 'Phasezero', total: 15.26, hours: 1 }] } },
  top_sessions: [
    { key: `claude:${UUID1}`, project: 'ccboard', repo: 'ccboard', agent: 'claude', total: 120.3, hours: 1.2, tokens: 180382285, models: ['claude-sonnet-5-5'], est_basis: 'list' },
    { key: `claude:${UUID2}`, project: 'SD-Law-website', repo: 'root', agent: 'claude', total: 40, hours: 0, tokens: 90000000, models: [] },
  ],
  unpriced: [],
});
const REPORTED_WITH_EST = () => SUMMARY(DAILY7, { basis: 'reported', estimate: EST_BLOCK });
// answers are matched by path prefix, first match wins: one function answers both bases
const estOver = (failEst = false) => ({ '/api/usage/summary?days=7': (path) => {
  if (!path.includes('basis=est')) return REPORTED_WITH_EST();
  if (failEst) throw new Error('boom');
  return EST_SUMMARY();
} });
const pressed = (w, sel) => q(w, sel).getAttribute('aria-pressed');

test('basis: a segmented Reported | Estimated control sits in the toolbar, defaults to Reported, and the first load asks for no basis', async () => {
  const { w } = usageWorld({ over: estOver() });
  await go(w);
  const seg = q(w, '[data-seg="basis"]');
  assert.ok(seg, 'the control');
  assert.equal(seg.getAttribute('role'), 'group');
  assert.deepEqual(seg.querySelectorAll('button').map(text), ['Reported', 'Estimated']);
  assert.equal(pressed(w, '[data-basis="reported"]'), 'true');
  assert.equal(pressed(w, '[data-basis="est"]'), 'false');
  const sums = plain(w.get('__calls')).filter((c) => c.path.startsWith('/api/usage/summary'));
  assert.equal(sums.length, 1);
  assert.equal(sums[0].path, '/api/usage/summary?days=7&tz_min=345', 'the reported path is the one it always was');
  assert.ok(!qa(w, 'td.c-num').some((n) => text(n).startsWith('~')), 'no ~ on the reported basis');
  clean(w);
});

test('basis: switching to Estimated fetches ?basis=est once, repaints the figures with a ~, remembers the choice, and keeps the range and the scroll', async () => {
  const { w } = usageWorld({ over: estOver() });
  await go(w);
  q(w, 'button[data-range="30d"]').click();
  await loading(w);
  q(w, 'button[data-range="7d"]').click();
  await loading(w);
  const before = plain(w.get('__calls')).length;
  q(w, '[data-basis="est"]').click();
  await loading(w);
  const calls = plain(w.get('__calls')).slice(before).filter((c) => c.path.startsWith('/api/usage/summary'));
  assert.deepEqual(calls.map((c) => c.path), ['/api/usage/summary?days=7&tz_min=345&basis=est'], 'only the summary of the other basis, once');
  assert.equal(pressed(w, '[data-basis="est"]'), 'true');
  assert.equal(pressed(w, '[data-basis="reported"]'), 'false');
  assert.equal(w.localStorage.getItem('ccboard:usage:basis'), 'est');
  assert.equal(pressed(w, 'button[data-range="7d"]'), 'true', 'the range stays');
  const prow = qa(w, 'tr.prow');
  assert.deepEqual(prow.map((r) => r.getAttribute('data-project')), ['ccboard', 'Phasezero']);
  assert.equal(text(prow[0].querySelectorAll('td')[1]), '~$80.50', 'every dollar of the Estimated basis carries a ~');
  assert.match(text(q(w, '.utotals')), /~\$95\.76/);
  const srow = qa(w, 'tr.srow');
  assert.equal(text(srow[0].querySelectorAll('td')[2]), '~$120.30');
  assert.equal(text(srow[0].querySelector('[data-est]')), 'estimated · list price', 'a session with an estimate says which basis it used');
  assert.equal(srow[1].querySelector('[data-est]'), null);
  q(w, '[data-basis="reported"]').click();
  await loading(w);
  assert.equal(text(qa(w, 'tr.prow')[0].querySelectorAll('td')[1]).startsWith('~'), false, 'and back: no ~');
  const again = plain(w.get('__calls')).filter((c) => c.path.startsWith('/api/usage/summary'));
  assert.equal(again.filter((c) => c.path.includes('basis=est')).length, 1, 'the Estimated summary is cached for the next switch');
  clean(w);
});

test('basis: the choice is remembered in ccboard:usage:basis and a new page opens on it', async () => {
  const first = usageWorld({ over: estOver() });
  first.w.localStorage.setItem('ccboard:usage:basis', 'est');
  await go(first.w);
  assert.equal(pressed(first.w, '[data-basis="est"]'), 'true');
  const sums = plain(first.w.get('__calls')).filter((c) => c.path.startsWith('/api/usage/summary')).map((c) => c.path);
  assert.deepEqual(sums, ['/api/usage/summary?days=7&tz_min=345&basis=est'], 'it asked for the Estimated summary straight away');
  assert.match(text(q(first.w, '.utotals')), /^Last 7 days: ~\$95\.76/);
  clean(first.w);
  const junk = usageWorld({ over: estOver() });
  junk.w.localStorage.setItem('ccboard:usage:basis', 'nonsense');
  await go(junk.w);
  assert.equal(pressed(junk.w, '[data-basis="reported"]'), 'true', 'an unknown stored word is Reported');
  clean(junk.w);
});

test('basis: arrow keys move the selection and keep focus on the selected button', async () => {
  const { w } = usageWorld({ over: estOver() });
  await go(w);
  const seg = q(w, '[data-seg="basis"]');
  let prevented = 0;
  seg.dispatchEvent({ type: 'keydown', key: 'ArrowRight', preventDefault() { prevented++; } });
  await loading(w);
  assert.equal(pressed(w, '[data-basis="est"]'), 'true');
  assert.equal(prevented, 1);
  seg.dispatchEvent({ type: 'keydown', key: 'ArrowLeft', preventDefault() {} });
  await loading(w);
  assert.equal(pressed(w, '[data-basis="reported"]'), 'true');
  seg.dispatchEvent({ type: 'keydown', key: 'Tab', preventDefault() { prevented++; } });
  assert.equal(prevented, 1, 'other keys are left alone');
  clean(w);
});

test('basis: Estimated explains itself in a disclosure a keyboard can open: the price date, the sources of each basis, the assumed cache-write price, not an invoice', async () => {
  const { w } = usageWorld({ over: estOver() });
  w.localStorage.setItem('ccboard:usage:basis', 'est');
  await go(w);
  const d = q(w, 'details[data-note="estimate"]');
  assert.ok(d, 'a <details>, not a hover tooltip');
  assert.equal(text(d.querySelector('summary')), 'How these estimates are made');
  const body = text(d);
  assert.match(body, /3 sessions on models ccusage prices at zero add ~\$51\.20/);
  assert.match(body, /list prices of 2026-10-07 \(Anthropic pricing page/);
  assert.match(body, /2 sessions priced from the list price of the model itself/);
  assert.match(body, /1 session priced at the rate of a sibling model/);
  assert.match(body, /cache-write price of these models is not known: cache writes are priced at 1\.25 times the input price/);
  assert.match(body, /not a subscription invoice/);
  clean(w);
});

test('basis: on Reported a quiet caption says how many sessions are left out and what Estimated would add; with none estimated there is none', async () => {
  const { w } = usageWorld({ over: estOver() });
  await go(w);
  const note = q(w, '[data-note="est-available"]');
  assert.ok(note);
  assert.match(text(note), /^3 sessions on models ccusage prices at zero are not in these dollars\. Estimated adds about ~\$51\.20\.$/);
  assert.equal(q(w, 'details[data-note="estimate"]'), null, 'the disclosure is for the Estimated basis');
  clean(w);
  const none = usageWorld({ over: { '/api/usage/summary?days=7': SUMMARY(DAILY7, { estimate: { ...EST_BLOCK, sessions: 0, usd: 0, bases: {} } }) } });
  await go(none.w);
  assert.equal(q(none.w, '[data-note="est-available"]'), null);
  clean(none.w);
});

test('basis: the page footer says the dollars are API-equivalent, not an invoice, and shows the price date', async () => {
  const { w } = usageWorld({ over: estOver() });
  await go(w);
  const foot = q(w, '[data-foot]');
  assert.match(text(foot), /API-equivalent: what the tokens would cost at API list prices, not a subscription invoice\./);
  assert.match(text(foot), /Estimates use the list prices of 2026-10-07\./);
  clean(w);
});

test('basis: the Estimated hatch stays for sessions with no estimate and a failed Estimated fetch shows its own error while Reported stays', async () => {
  const { w } = usageWorld({ over: estOver(true) });
  await go(w);
  const reportedRows = qa(w, 'tr.prow').length;
  q(w, '[data-basis="est"]').click();
  await loading(w);
  assert.match(text(q(w, '[data-body="projects"]')), /Could not load the usage summary: boom/);
  q(w, '[data-basis="reported"]').click();
  await loading(w);
  assert.equal(qa(w, 'tr.prow').length, reportedRows, 'Reported is back, untouched');
  clean(w);
});

test('basis: the stacked bars get approx on the Estimated basis and not on Reported', async () => {
  const { w } = usageWorld({ over: estOver() });
  await go(w);
  const approxOf = () => { const l = w.get('__charts').calls.filter((c) => c.fn === 'stackedBars'); return l[l.length - 1].opts.approx; };
  assert.equal(approxOf(), false);
  q(w, '[data-basis="est"]').click();
  await loading(w);
  assert.equal(approxOf(), true);
  clean(w);
});
