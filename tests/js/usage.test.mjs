// Contract tests for app/static/pages/usage.js (v0.5.17): the Usage page. Real core.js, components.js, keymap.js, router.js and usage.js on minidom's DOM
// inside the vm harness; window.Charts is a recording fake (so the page is tested without charts.js), api() answers from __answers.
// The last test runs the page against the real charts.js (with a stub uPlot) and is skipped while that file does not exist.
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

/** A world with the real scripts and the router mounted on #page. opts: wide, charts (false = no Charts at all), timers, st, noState. */
function usageWorld({ wide = false, charts = true, st = STATE(), over = {}, extra = {} } = {}) {
  const timers = [];
  const cleared = [];
  const w = makeWorld({
    matchMedia: (q) => ({ matches: wide && /840/.test(q), addEventListener() {}, removeEventListener() {} }),
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearInterval: (id) => cleared.push(id), ...extra,
  });
  installDom(w);
  for (const f of ['core.js', 'components.js', 'keymap.js', 'router.js']) w.load(f);
  w.ctx.__calls = [];
  w.ctx.__answers = answers(over);
  w.run(`api = async (method, path) => {
    __calls.push({ method, path });
    for (const [prefix, v] of Object.entries(__answers)) {
      if (!path.startsWith(prefix)) continue;
      if (v && v.__error) throw new Error(v.__error);
      return typeof v === 'function' ? v(path) : v;
    }
    return {};
  };`);
  if (charts) w.run(CHARTS_STUB);
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

test('the six sections in the asked order, each with an h2 and a provenance label; a skeleton paints before any answer', async () => {
  const { w } = usageWorld();
  w.location.hash = '#/usage';
  const secs = qa(w, 'section.usec');
  assert.deepEqual(secs.map((s) => s.getAttribute('data-sec')), ['limits', 'cost', 'sessions', 'projects', 'activity', 'timeline']);
  assert.deepEqual(secs.map((s) => text(s.querySelector('h2'))), ['Limits', 'Cost per day', 'Sessions', 'Projects', 'Activity', 'Timeline']);
  assert.ok(secs.every((s) => s.querySelector('.prov')), 'a provenance span in every section');
  assert.equal(text(secs[0].querySelector('.prov')), 'statusline (official)');
  assert.equal(text(secs[1].querySelector('.prov')), 'API-equivalent (ccusage list price)');
  assert.equal(text(q(w, '[data-sec="activity"] .prov')), 'hook events, local time');
  for (const id of ['cost', 'sessions', 'projects', 'activity', 'timeline']) assert.ok(q(w, `[data-sec="${id}"] .usk`), `${id}: skeleton before the first answer`);
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
  assert.equal(note.getAttribute('title'), 'sessions the board did not start');
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
  assert.equal(un.getAttribute('title'), 'sessions the board did not start');
  assert.equal(un.querySelector('[data-unattributed]').getAttribute('title'), 'sessions the board did not start');
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
