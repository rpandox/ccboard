// Contract tests for app/static/charts.js (v0.5.17): the Charts namespace behind the Usage page. Real core.js and charts.js on minidom's DOM inside the
// vm harness, with a stub uPlot class (records new / setData / setSize / redraw / destroy) and a stub ResizeObserver. Pure geometry (geom.*), the
// formatters, the fetch cache (fake api + the settable Charts.now clock), the lazy loader, one uPlot per host, pause / resume, the canvas overlay, the
// SVG bars / Gantt, the heatmap and the sparkline.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const text = (n) => (n ? n.textContent : '');
const tick = () => new Promise((r) => setImmediate(r));

/** A world with the DOM, core.js and charts.js, a stub uPlot and ResizeObserver, and an api() answering from __answers (path prefix -> value | {__error} | fn). */
function cWorld({ uplot = true, extra = {} } = {}) {
  const w = makeWorld(extra);
  installDom(w);
  w.load('core.js');
  w.load('charts.js');
  w.run(`
    globalThis.__plots = []; globalThis.__ros = []; globalThis.__calls = []; globalThis.__answers = {}; globalThis.__clock = 1000000;
    Charts.now = () => __clock;
    api = async (method, path) => {
      __calls.push(path);
      for (const [prefix, v] of Object.entries(__answers)) {
        if (!path.startsWith(prefix)) continue;
        if (v && v.__error) throw new Error(v.__error);
        return typeof v === 'function' ? v(path) : v;
      }
      return {};
    };
    class FakeRO { constructor(cb) { this.cb = cb; this.targets = []; this.disconnected = false; __ros.push(this); } observe(t) { this.targets.push(t); } disconnect() { this.disconnected = true; } }
    globalThis.ResizeObserver = FakeRO;
  `);
  if (uplot) installUplot(w);
  return w;
}

function installUplot(w) {
  w.run(`
    globalThis.uPlot = class {
      constructor(opts, data, target) { this.opts = opts; this.data = data; this.target = target; this.log = []; this.destroyed = false; this.cursor = { idx: null }; __plots.push(this); }
      setData(d) { this.data = d; this.log.push('setData'); }
      setSize(s) { this.size = s; this.log.push('setSize'); }
      redraw() { this.log.push('redraw'); }
      destroy() { this.destroyed = true; this.log.push('destroy'); }
    };
  `);
}

/** A fresh chart host under #page. */
function host(w, clientWidth = 400) {
  const h = w.document.createElement('div');
  h.clientWidth = clientWidth;
  w.document.querySelector('#page').append(h);
  return h;
}

const plots = (w) => w.get('__plots');
const ros = (w) => w.get('__ros');
const T0 = 1790900000;
const series = (n, last = 77, extra = {}) => ({
  since: new Date((T0 - 100) * 1000).toISOString(), until: new Date((T0 + n * 300) * 1000).toISOString(), step: 300,
  t: Array.from({ length: n }, (_, i) => T0 + i * 300),
  series: { 'rl_5h:claude': Array.from({ length: n }, (_, i) => (i === 2 ? null : i === n - 1 ? last : 10 + i)), 'rl_7d:claude': Array.from({ length: n }, (_, i) => 50 + i), ...extra },
  meta: {},
});

// ---------------------------------------------------------------- definition only

const trap = (what) => new Proxy(function () {}, {
  get(_t, prop) { if (prop === Symbol.toPrimitive || prop === 'then') return undefined; throw new Error(`load-time access to ${what}.${String(prop)}`); },
  apply() { throw new Error(`load-time call of ${what}()`); },
  construct() { throw new Error(`load-time construction of ${what}`); },
});

test('charts.js defines only: no DOM, storage, network, listener or timer access at load, and nothing is drawn', () => {
  assert.ok(fs.existsSync(path.join(STATIC, 'charts.js')), 'charts.js missing');
  const boom = (what) => () => { throw new Error(`load-time call of ${what}()`); };
  const w = makeWorld({
    document: trap('document'), localStorage: trap('localStorage'), sessionStorage: trap('sessionStorage'), navigator: trap('navigator'),
    location: trap('location'), history: trap('history'), matchMedia: boom('matchMedia'), addEventListener: boom('window.addEventListener'),
    setTimeout: boom('setTimeout'), setInterval: boom('setInterval'), requestAnimationFrame: boom('requestAnimationFrame'), fetch: boom('fetch'),
    ResizeObserver: function ResizeObserver() { throw new Error('load-time construction of ResizeObserver'); },
    getComputedStyle: boom('getComputedStyle'),
  });
  w.load('core.js');
  w.load('charts.js');
  assert.equal(w.get('typeof Charts.line'), 'function');
  assert.equal(w.get('window.Charts === Charts'), true, 'window.Charts is the same namespace (Home checks window.Charts)');
});

// ---------------------------------------------------------------- formatters

test('fmtUsd matches Widgets.money, and never prints NaN', () => {
  const w = cWorld();
  const f = (v) => w.run(`Charts.fmtUsd(${JSON.stringify(v)})`);
  assert.equal(f(17.153), '$17.15');
  assert.equal(f(0), '$0.00');
  assert.equal(f(99.994), '$99.99');
  assert.equal(f(100), '$100');
  assert.equal(f(1234.56), '$1235');
  assert.equal(f(-3), '-$3.00');
  assert.equal(f('abc'), '—');
  assert.equal(w.run('Charts.fmtUsd(undefined)'), '—');
  assert.equal(w.run('Charts.fmtUsd(NaN)'), '—');
});

test('fmtTok: 812, 1.2k, 48.7M, no trailing .0, no 1000k', () => {
  const w = cWorld();
  const f = (v) => w.run(`Charts.fmtTok(${v})`);
  assert.equal(f(812), '812');
  assert.equal(f(1200), '1.2k');
  assert.equal(f(1000), '1k');
  assert.equal(f(48700000), '48.7M');
  assert.equal(f(180382285), '180.4M');
  assert.equal(f(999960), '1M');
  assert.equal(f(2.5e9), '2.5B');
  assert.equal(f(0), '0');
  assert.equal(f('NaN'), '0');
  assert.equal(w.run('Charts.fmtTok(undefined)'), '0');
});

test('shortModel drops the vendor prefix and a build date; fmtTz names the zone', () => {
  const w = cWorld();
  assert.equal(w.run("Charts.shortModel('claude-opus-5-5')"), 'opus-5-5');
  assert.equal(w.run("Charts.shortModel('claude-sonnet-5-5')"), 'sonnet-5-5');
  assert.equal(w.run("Charts.shortModel('claude-fable-5-1')"), 'fable-5-1');
  assert.equal(w.run("Charts.shortModel('claude-3-5-sonnet-20241022')"), '3-5-sonnet');
  assert.equal(w.run("Charts.shortModel('gpt-5-codex')"), 'gpt-5-codex');
  assert.equal(w.run('Charts.shortModel(null)'), '');
  assert.equal(w.run('Charts.fmtTz(345)'), 'UTC+5:45');
  assert.equal(w.run('Charts.fmtTz(-300)'), 'UTC-5');
  assert.equal(w.run('Charts.fmtTz(0)'), 'UTC');
});

// ---------------------------------------------------------------- geometry

const DAYS = [
  { day: '2026-10-02', total: 30, zero: false, tokens: 3000, by_project: { A: 10, B: 8, C: 5, D: 4, '(unattributed)': 3 }, by_agent: { claude: 27, codex: 3 } },
  { day: '2026-10-03', total: 0, zero: true, tokens: 0, by_project: {}, by_agent: {} },
  { day: '2026-10-04', total: 12, zero: false, tokens: 900, by_project: { A: 2, E: 1, '(unattributed)': 9 }, by_agent: { claude: 12 } },
];

test('geom.stack: top cut into other, (unattributed) its own segment on top, zero days flagged, cumulative y0/y1', () => {
  const w = cWorld();
  w.ctx.__days = DAYS;
  const rows = plain(w.run('Charts.geom.stack(__days, "project", 2)'));
  assert.deepEqual(rows[0], { day: '2026-10-02', total: 30, zero: false, segs: [
    { key: 'A', v: 10, y0: 0, y1: 10 }, { key: 'B', v: 8, y0: 10, y1: 18 }, { key: 'other', v: 9, y0: 18, y1: 27 }, { key: '(unattributed)', v: 3, y0: 27, y1: 30 }] });
  assert.deepEqual(rows[1], { day: '2026-10-03', total: 0, zero: true, segs: [] });
  assert.deepEqual(rows[2].segs.map((s) => s.key), ['A', 'other', '(unattributed)'], 'B has no spend that day, so no segment');
  assert.deepEqual(rows[2].segs.map((s) => [s.y0, s.y1]), [[0, 2], [2, 3], [3, 12]]);
  // the same key order on every day, so a colour never moves
  const keys = plain(w.run('Charts.geom.rank(__days, "project", 2)'));
  assert.deepEqual(keys.map((k) => [k.key, k.cls]), [['A', 'seg-0'], ['B', 'seg-1'], ['other', 'seg-other'], ['(unattributed)', 'seg-unattributed']]);
  assert.deepEqual(keys[2].members, ['C', 'D', 'E']);
  assert.equal(keys[3].total, 12);
});

test('geom.stack: (unattributed) is never ranked against projects or folded into other, even with top 0', () => {
  const w = cWorld();
  w.ctx.__days = DAYS;
  const keys = plain(w.run('Charts.geom.rank(__days, "project", 0)'));
  assert.deepEqual(keys.map((k) => k.key), ['other', '(unattributed)']);
  const rows = plain(w.run('Charts.geom.stack(__days, "project", 0)'));
  assert.deepEqual(rows[0].segs.map((s) => [s.key, s.v]), [['other', 27], ['(unattributed)', 3]]);
  const all = plain(w.run('Charts.geom.rank(__days, "project", 99)'));
  assert.deepEqual(all.map((k) => k.key), ['A', 'B', 'C', 'D', 'E', '(unattributed)'], 'no other when nothing is cut');
});

test('geom.stack by agent: the agent colours, and spend the keys do not explain lands in other', () => {
  const w = cWorld();
  w.ctx.__days = [...DAYS, { day: '2026-10-05', total: 10, zero: false, by_agent: { claude: 4 } }];
  const keys = plain(w.run('Charts.geom.rank(__days, "agent", 6)'));
  assert.deepEqual(keys.map((k) => [k.key, k.cls]), [['claude', 'agent-claude'], ['codex', 'agent-codex'], ['other', 'seg-other']]);
  const rows = plain(w.run('Charts.geom.stack(__days, "agent", 6)'));
  assert.deepEqual(rows[3].segs.map((s) => [s.key, s.v]), [['claude', 4], ['other', 6]]);
  const noKeys = plain(w.run('Charts.geom.stack([{day:"2026-10-06", total: 5}], "project", 6)'));
  assert.deepEqual(noKeys[0].segs.map((s) => [s.key, s.v]), [['other', 5]], 'a day with a total but no breakdown is one other segment');
});

test('geom.stack reads window-style {total} values and ignores junk', () => {
  const w = cWorld();
  w.ctx.__days = [{ day: '2026-10-02', total: 5, by_agent: { claude: { total: 5, tokens: 10 }, bad: 'x', neg: -2 } }];
  const rows = plain(w.run('Charts.geom.stack(__days, "agent", 6)'));
  assert.deepEqual(rows[0].segs.map((s) => [s.key, s.v]), [['claude', 5]]);
});

test('geom.scale: a 1/2/3/4/5/6/8 x 10^n ceiling with three gridlines', () => {
  const w = cWorld();
  const s = (m, h = 100) => plain(w.run(`(() => { const r = Charts.geom.scale(${JSON.stringify(m)}, ${h}); return { max: r.max, ticks: r.ticks, k: r.k, y0: r.y(0), yMax: r.y(r.max) }; })()`));
  assert.deepEqual(s(44.56), { max: 50, ticks: [0, 25, 50], k: 2, y0: 100, yMax: 0 });
  assert.equal(s(68.96).max, 80);
  assert.equal(s(17.15).max, 20);
  assert.equal(s(3).max, 3);
  assert.equal(s(0.37).max, 0.4);
  assert.deepEqual(s(0).ticks, [0, 0.5, 1]);
  assert.equal(s(0).max, 1);
  assert.equal(s('x').max, 1);
  assert.equal(s(1234).max, 2000);
});

test('geom.level: 0 only for no events, then the quartile of max', () => {
  const w = cWorld();
  const lv = (v, m) => w.run(`Charts.geom.level(${v}, ${m})`);
  assert.equal(lv(0, 10), 0);
  assert.equal(lv(1, 1000), 1, 'a tiny value is still visible');
  assert.equal(lv(25, 100), 1);
  assert.equal(lv(26, 100), 2);
  assert.equal(lv(50, 100), 2);
  assert.equal(lv(51, 100), 3);
  assert.equal(lv(75, 100), 3);
  assert.equal(lv(76, 100), 4);
  assert.equal(lv(100, 100), 4);
  assert.equal(lv(5, 0), 0);
  assert.equal(lv('NaN', 10), 0);
  assert.equal(lv(-3, 10), 0);
});

const ev = (t, key, v, m) => ({ t, key, v, m });
const META = { p: 'proj', r: 'repo', s: 's1', a: 'claude' };

test('geom.spans: a span runs to the next event, the last to until; ended closes the row; clipped to the window', () => {
  const w = cWorld();
  w.ctx.__ev = [
    ev(100, 'a', 1, META), ev(200, 'a', 2, META), ev(300, 'a', 5, META), ev(400, 'a', 1, META),
    ev(50, 'b', 1, { p: 'solo', r: 'root', s: 'x' }), ev(60, 'b', 1),
    ev(500, 'c', 5, META),
    ev(100, 'd', 0), ev(900, 'd', 5),
  ];
  const r = plain(w.run('Charts.geom.spans(__ev, 0, 1000)'));
  assert.deepEqual(r.rows.map((x) => x.key), ['d', 'a', 'b'], 'most recently active first; c (ended, nothing before it) has no span and is dropped');
  const by = Object.fromEntries(r.rows.map((x) => [x.key, x]));
  assert.deepEqual(by.a.spans, [{ t0: 100, t1: 200, v: 1 }, { t0: 200, t1: 300, v: 2 }, { t0: 400, t1: 1000, v: 1 }], 'the ended event draws nothing; the gap stays a gap');
  assert.equal(by.a.ended, false, 'a later event reopened the row');
  assert.deepEqual(by.b.spans, [{ t0: 50, t1: 1000, v: 1 }], 'adjacent spans of one state merge');
  assert.deepEqual(by.d.spans, [{ t0: 100, t1: 900, v: 0 }]);
  assert.equal(by.d.ended, true);
  assert.equal(by.a.label, 'proj/repo · s1');
  assert.equal(by.b.label, 'solo · x', 'the repo root is not part of the label');
  assert.equal(by.d.label, 'd', 'no meta: the key');
  assert.equal(r.truncated, 0);
  assert.equal(r.total, 3);
  const clipped = plain(w.run('Charts.geom.spans(__ev, 150, 350)'));
  assert.deepEqual(clipped.rows.find((x) => x.key === 'a').spans, [{ t0: 150, t1: 200, v: 1 }, { t0: 200, t1: 300, v: 2 }]);
});

test('geom.spans: truncation counts the rows cut, junk events and an empty body are harmless', () => {
  const w = cWorld();
  w.ctx.__ev = Array.from({ length: 5 }, (_, i) => ev(100 + i, 'k' + i, 1, { p: 'p' + i }));
  const r = plain(w.run('Charts.geom.spans(__ev, 0, 1000, 2)'));
  assert.equal(r.rows.length, 2);
  assert.equal(r.truncated, 3);
  assert.equal(r.total, 5);
  assert.deepEqual(r.rows.map((x) => x.key), ['k4', 'k3'], 'the most recent rows survive the cut');
  assert.deepEqual(plain(w.run('Charts.geom.spans({}, 0, 10)')), { rows: [], truncated: 0, total: 0 });
  assert.deepEqual(plain(w.run('Charts.geom.spans(undefined)')), { rows: [], truncated: 0, total: 0 });
  w.ctx.__bad = [ev('x', 'a', 1), ev(10, 'a', 9), ev(10, 'a', 1.5), ev(10, null, 1), null, ev(20, 'a', 2)];
  const bad = plain(w.run('Charts.geom.spans(__bad, 0, 100)'));
  assert.deepEqual(bad.rows.map((x) => x.spans), [[{ t0: 20, t1: 100, v: 2 }]]);
  const iso = plain(w.run('Charts.geom.spans([{t: 1790900000, key: "a", v: 1}], "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")'));
  assert.equal(iso.rows[0].spans[0].t1, Date.parse('2026-10-03T00:00:00Z') / 1000, 'ISO bounds are read');
});

test('geom.hourTicks: local hours on a multiple of the step, ascending, inside the window', () => {
  const w = cWorld();
  const since = T0 + 1234;
  const ticks = plain(w.run(`Charts.geom.hourTicks(${since}, ${since + 86400}, 3)`));
  assert.ok(ticks.length >= 8 && ticks.length <= 9, `${ticks.length} ticks over 24 h at 3 h`);
  for (const t of ticks) { assert.ok(t.t >= since && t.t <= since + 86400); assert.match(t.label, /^\d\d:00$/); assert.equal(new Date(t.t * 1000).getHours() % 3, 0); }
  assert.ok(ticks.every((t, i) => i === 0 || t.t - ticks[i - 1].t === 10800));
  assert.deepEqual(plain(w.run('Charts.geom.hourTicks(10, 5, 3)')), []);
});

test('geom.dayLabel reads a calendar date without a zone shift', () => {
  const w = cWorld();
  assert.deepEqual(plain(w.run('Charts.geom.dayLabel("2026-10-03")')), { wd: 'Sat', d: 3, mon: 'Oct' });
  assert.equal(w.run('Charts.geom.dayLabel("nope")'), null);
});

// ---------------------------------------------------------------- fetch cache

test('fetch: dedupes in-flight calls, keeps a since=24h answer 30 s and a longer one 5 min, never caches an error', async () => {
  const w = cWorld();
  w.run('__answers["/api/series"] = (p) => ({ p, n: __calls.length })');
  const p1 = w.run('Charts.fetch("/api/series?since=24h")');
  const p2 = w.run('Charts.fetch("/api/series?since=24h")');
  const [a, b] = await Promise.all([p1, p2]);
  assert.equal(w.get('__calls.length'), 1, 'two simultaneous calls share one request');
  assert.equal(a, b);
  const get = (p) => w.run(`Charts.fetch(${JSON.stringify(p)})`);
  w.ctx.__clock += 29000;
  await get('/api/series?since=24h');
  assert.equal(w.get('__calls.length'), 1, 'still fresh at 29 s');
  w.ctx.__clock += 2000;
  await get('/api/series?since=24h');
  assert.equal(w.get('__calls.length'), 2, 'refetched after 30 s');
  await get('/api/series?since=7d');
  w.ctx.__clock += 299000;
  await get('/api/series?since=7d');
  assert.equal(w.get('__calls.length'), 3, 'a 7 d answer is kept for 5 min');
  w.ctx.__clock += 2000;
  await get('/api/series?since=7d');
  assert.equal(w.get('__calls.length'), 4);
  // 1h and 6h are short, 30d and a missing since are long
  assert.equal(w.run('Charts.ttl("/api/series?since=1h&points=200")'), 30000);
  assert.equal(w.run('Charts.ttl("/api/series?series=x&since=6h")'), 30000);
  assert.equal(w.run('Charts.ttl("/api/series?since=30d")'), 300000);
  assert.equal(w.run('Charts.ttl("/api/usage/summary?days=7")'), 300000);
  assert.equal(w.run('Charts.ttl("/api/series?since=24hx")'), 300000);
  // the cache key is the whole path
  await get('/api/series?since=24h&key=a');
  assert.equal(w.get('__calls.length'), 5);
  await get('/api/series?since=24h&key=a');
  assert.equal(w.get('__calls.length'), 5);
});

test('fetch: an error is not cached and a retry goes out; force bypasses the TTL; cached() is the synchronous read', async () => {
  const w = cWorld();
  w.run('__answers["/api/usage"] = { __error: "boom" }');
  await assert.rejects(w.run('Charts.fetch("/api/usage/summary?days=7")'), /boom/);
  assert.equal(w.run('Charts.cached("/api/usage/summary?days=7")'), null);
  w.run('__answers["/api/usage"] = { ok: 1 }');
  const body = plain(await w.run('Charts.fetch("/api/usage/summary?days=7")'));
  assert.deepEqual(body, { ok: 1 });
  assert.equal(w.get('__calls.length'), 2, 'the failed call did not poison the cache');
  assert.deepEqual(plain(w.run('Charts.cached("/api/usage/summary?days=7")')), { ok: 1 });
  w.ctx.__clock += 400000;
  assert.equal(w.run('Charts.cached("/api/usage/summary?days=7")'), null, 'expired');
  assert.deepEqual(plain(w.run('Charts.cached("/api/usage/summary?days=7", true)')), { ok: 1 }, 'stale=true still answers (paint at once, refresh behind)');
  await w.run('Charts.fetch("/api/usage/summary?days=7", { force: true })');
  assert.equal(w.get('__calls.length'), 3);
  await w.run('Charts.fetch("/api/usage/summary?days=7")');
  assert.equal(w.get('__calls.length'), 3, 'the forced answer is cached');
  w.run('Charts.clear()');
  assert.equal(w.run('Charts.cached("/api/usage/summary?days=7", true)'), null);
  w.run('__answers["/api/null"] = null');
  assert.deepEqual(plain(await w.run('Charts.fetch("/api/null")')), {}, 'a null body reads as {}');
});

// ---------------------------------------------------------------- lazy uPlot

test('ready: resolves at once when window.uPlot exists, without touching loadAsset', async () => {
  const w = cWorld();
  w.run('loadAsset = () => { throw new Error("must not load"); }');
  await w.run('Charts.ready()');
});

test('ready: loads the vendored css and js once, memoised', async () => {
  const w = cWorld({ uplot: false });
  w.run(`globalThis.__loaded = []; loadAsset = (p) => { __loaded.push(p); if (p.endsWith('.js')) { globalThis.uPlot = function () {}; } return Promise.resolve(); };`);
  await Promise.all([w.run('Charts.ready()'), w.run('Charts.ready()')]);
  assert.deepEqual(plain(w.get('__loaded')).sort(), ['/static/vendor/uplot/uPlot.iife.min.js', '/static/vendor/uplot/uPlot.min.css']);
  await w.run('Charts.ready()');
  assert.equal(w.get('__loaded.length'), 2);
});

test('ready: a failed load rejects, is not memoised, and a retry loads again', async () => {
  const w = cWorld({ uplot: false });
  w.run(`globalThis.__n = 0; loadAsset = (p) => { __n++; return p.endsWith('.js') && __n < 3 ? Promise.reject(new Error('offline')) : Promise.resolve(); };`);
  await assert.rejects(w.run('Charts.ready()'), /offline/);
  assert.equal(w.get('Charts._ready'), null);
  w.run("loadAsset = (p) => { if (p.endsWith('.js')) globalThis.uPlot = function () {}; return Promise.resolve(); }");
  await w.run('Charts.ready()');
  assert.equal(w.get('typeof window.uPlot'), 'function');
});

test('ready: core.js memoises a failed asset for good; the loader forgets it so Retry can work', async () => {
  const w = cWorld({ uplot: false });
  await assert.rejects(w.run('Charts.ready()'));          // minidom has no document.head, so the real loadAsset rejects
  assert.equal(w.get('Object.keys(assetLoading).length'), 0);
});

// ---------------------------------------------------------------- line charts

test('line: one uPlot per host, built from the series body with the colours, labels, axes and size it was asked for, then updated in place', () => {
  const w = cWorld({ extra: { getComputedStyle: () => ({ getPropertyValue: (n) => (n === '--sig' ? ' #123456 ' : n === '--warn' ? '#ec9a3c' : '') }) } });
  const h = host(w, 400);
  w.ctx.__h = h;
  w.ctx.__d = series(8);
  w.run(`Charts.line(__h, __d, { names: ['rl_5h:claude', 'rl_7d:claude'], labels: { 'rl_5h:claude': '5H', 'rl_7d:claude': '7D' }, colors: { 'rl_5h:claude': '--sig', 'rl_7d:claude': '--warn' }, yMax: 100, thresholds: [60, 85], height: 200 })`);
  assert.equal(plots(w).length, 1);
  const u = plots(w)[0];
  assert.equal(h._uplot, u, 'host._uplot is the instance');
  assert.equal(u.opts.width, 400);
  assert.equal(u.opts.height, 200);
  assert.equal(u.opts.legend.show, false, 'the chips below replace uPlot\'s legend');
  assert.equal(u.opts.cursor.drag.setScale, false, 'a phone swipe scrolls, it does not zoom');
  assert.deepEqual(plain(u.opts.series.slice(1).map((s) => [s.label, s.stroke, s.spanGaps])), [['5H', '#123456', false], ['7D', '#ec9a3c', false]]);
  assert.equal(u.data.length, 3);
  assert.equal(u.data[0].length, 8);
  assert.equal(u.data[1][2], null, 'a hole stays null (a gap, not a zero)');
  assert.deepEqual(plain(u.opts.scales.y.range()), [0, 100]);
  const [lo, hi] = plain(u.opts.scales.x.range());
  assert.equal(lo, T0 - 100, 'the x range is the whole requested window (since), not just the samples');
  assert.ok(hi >= T0 + 7 * 300);
  assert.ok(h.classList.contains('chart'));
  assert.equal(h.querySelectorAll('.legend .lg').length, 2);
  assert.equal(text(h.querySelectorAll('.legend .lg-v')[0]), '77%', 'the chips start at the latest known value');
  assert.deepEqual(h.querySelectorAll('.legend .lg-l').map(text), ['5H', '7D']);
  // the same data again: nothing happens
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude", "rl_7d:claude"], yMax: 100, thresholds: [60, 85], height: 200 })');
  assert.equal(plots(w).length, 1, 'still one instance');
  assert.deepEqual(plain(u.log), []);
  // the same last t and length but another value: still no setData (the rule is last t or length)
  w.ctx.__d = series(8, 12);
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude", "rl_7d:claude"], yMax: 100, thresholds: [60, 85], height: 200 })');
  assert.deepEqual(plain(u.log), []);
  // a new last t: one setData
  w.ctx.__d = series(9, 40);
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude", "rl_7d:claude"], yMax: 100, thresholds: [60, 85], height: 200 })');
  assert.deepEqual(plain(u.log), ['setData']);
  assert.equal(u.data[0].length, 9);
  assert.equal(text(h.querySelectorAll('.legend .lg-v')[0]), '40%');
  // the marks changed, the data did not: a redraw only
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude", "rl_7d:claude"], yMax: 100, thresholds: [60, 85], height: 200, marks: [{ t: 1790900300, label: "5h limit", cls: "bad" }] })');
  assert.deepEqual(plain(u.log), ['setData', 'redraw']);
  assert.equal(plots(w).length, 1);
});

test('line: another series set (or a shorter list) rebuilds; destroy and destroyAll clear the host and the observers', () => {
  const w = cWorld();
  const h = host(w);
  const h2 = host(w);
  w.ctx.__h = h; w.ctx.__h2 = h2;
  w.ctx.__d = series(6);
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude", "rl_7d:claude"] }); Charts.line(__h2, __d, { names: ["rl_5h:claude"] })');
  assert.equal(plots(w).length, 2, 'one instance per host');
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude"] })');
  assert.equal(plots(w).length, 3, 'the series set changed: a new instance');
  assert.equal(plots(w)[0].destroyed, true, 'the old one was destroyed');
  assert.equal(h.querySelectorAll('.chart-plot').length, 1, 'the host holds one plot, not two');
  const live = ros(w).filter((r) => !r.disconnected).length;
  assert.equal(live, 2, 'the replaced instance\'s observer was disconnected');
  w.run('Charts.destroy(__h2)');
  assert.equal(h2._uplot, undefined);
  assert.equal(text(h2), '');
  assert.equal(plots(w)[1].destroyed, true);
  w.run('Charts.destroyAll()');
  assert.equal(h._uplot, undefined);
  assert.ok(plots(w).every((p) => p.destroyed));
  assert.ok(ros(w).every((r) => r.disconnected));
  assert.equal(w.get('Charts._hosts.size'), 0);
  assert.equal(w.get('Charts._vis'), null, 'the visibility listener goes with the last chart');
  w.run('Charts.destroyAll()');   // twice is fine
});

test('line: nothing to draw shows an empty-state text with a next step, never a blank box; the chart comes back with data', () => {
  const w = cWorld();
  const h = host(w);
  w.ctx.__h = h;
  for (const d of [{}, null, { t: [], series: {} }, { t: [1, 2, 3], series: { 'rl_5h:claude': [null, null, null] } }, { t: [1, 2], series: {} }]) {
    w.ctx.__d = d;
    assert.equal(w.run('Charts.line(__h, __d, { names: ["rl_5h:claude"] })'), null);
    assert.match(text(h), /No samples in this window yet: the board records usage from its first hook event\./);
    assert.equal(h.querySelectorAll('canvas').length, 0);
  }
  w.run('Charts.line(__h, { t: [], series: {} }, { names: ["x"], empty: "Nothing here: pick 7d." })');
  assert.equal(text(h), 'Nothing here: pick 7d.');
  assert.equal(plots(w).length, 0);
  w.ctx.__d = series(5);
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude"] })');
  assert.equal(plots(w).length, 1);
  assert.equal(h.querySelectorAll('.chart-empty').length, 0, 'the empty text is gone');
  w.run('Charts.line(__h, {}, { names: ["rl_5h:claude"] })');
  assert.equal(plots(w)[0].destroyed, true, 'data that went away destroys the instance');
  assert.equal(h._uplot, undefined);
});

test('line: rows that are not increasing are dropped, a series shorter than t is padded, a missing name is skipped, loading is cleared', () => {
  const w = cWorld();
  const h = host(w);
  h.classList.add('loading');
  w.ctx.__h = h;
  w.ctx.__d = { t: [10, 20, 20, 15, 30, 'x', 40], series: { a: [1, 2, 3, 4, 5, 6], b: [9, 9, 9, 9, 9, 9, 9] } };
  w.run('Charts.line(__h, __d, { names: ["a", "ghost", "b"] })');
  const u = plots(w)[0];
  assert.deepEqual(plain(u.data[0]), [10, 20, 30, 40]);
  assert.deepEqual(plain(u.data[1]), [1, 2, 5, null], 'padded with null');
  assert.equal(u.data.length, 3, 'ghost is not a series');
  assert.equal(h.classList.contains('loading'), false);
});

test('line: a second, right-hand axis for series of another unit (ctx % and scost $)', () => {
  const w = cWorld();
  const h = host(w);
  w.ctx.__h = h;
  w.ctx.__d = series(6, 5, { 'ctx:s1': [10, 20, 30, 40, 50, 60], 'scost:s1': [0.5, 1, 1.5, 2, 2.5, 3] });
  w.run('Charts.line(__h, __d, { names: ["ctx:s1", "scost:s1"], right: ["scost:s1"], labels: { "ctx:s1": "ctx", "scost:s1": "cost" }, yMax: 100, legend: false })');
  const u = plots(w)[0];
  assert.deepEqual(plain(u.opts.series.slice(1).map((s) => s.scale)), ['y', 'y2']);
  assert.equal(u.opts.axes.length, 3);
  assert.equal(u.opts.axes[2].scale, 'y2');
  assert.equal(u.opts.axes[1].values(null, [0, 50, 100]).join(), '0%,50%,100%');
  assert.equal(u.opts.axes[2].values(null, [0, 1.5, 3]).join(), '$0,$1.5,$3');
  assert.deepEqual(plain(u.opts.scales.y2.range(null, 0, 3)), [0, 3.3000000000000003]);
  assert.equal(h.querySelectorAll('.legend').length, 0, 'legend: false');
});

test('line: the resize observer sets the size; the cursor drives the chips', () => {
  const w = cWorld();
  const h = host(w, 400);
  w.ctx.__h = h;
  w.ctx.__d = series(6, 70);
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude", "rl_7d:claude"], height: 150 })');
  const u = plots(w)[0];
  const ro = ros(w)[0];
  assert.equal(ro.targets.length, 1);
  assert.equal(ro.targets[0], h, 'the host is what is observed');
  ro.cb();                                   // same width: nothing
  assert.deepEqual(plain(u.log), []);
  h.clientWidth = 300;
  ro.cb();
  assert.deepEqual(plain(u.size), { width: 300, height: 150 });
  u.cursor.idx = 1;
  u.opts.hooks.setCursor[0](u);
  assert.equal(text(h.querySelectorAll('.legend .lg-v')[0]), '11%');
  assert.equal(text(h.querySelectorAll('.legend .lg-v')[1]), '51%');
  assert.match(text(h.querySelector('.lg-t')), /^\d\d:\d\d$/);
  u.cursor.idx = 2;
  u.opts.hooks.setCursor[0](u);
  assert.equal(text(h.querySelectorAll('.legend .lg-v')[0]), '–', 'a hole reads as a dash, not NaN or 0');
  u.cursor.idx = null;
  u.opts.hooks.setCursor[0](u);
  assert.equal(text(h.querySelectorAll('.legend .lg-v')[0]), '70%');
  assert.equal(text(h.querySelector('.lg-t')), '');
});

test('line: pause skips redraws and resume applies the latest data and size once; visibilitychange drives it', () => {
  const w = cWorld();
  const h = host(w, 400);
  w.ctx.__h = h;
  w.ctx.__d = series(6);
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude"] })');
  const u = plots(w)[0];
  assert.equal(w.get('Charts._vis !== null'), true, 'wired when the first chart was drawn');
  w.document.hidden = true;
  w.document.dispatch('visibilitychange');
  assert.equal(w.get('Charts.paused'), true);
  w.ctx.__d = series(7, 33);
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude"] })');
  w.ctx.__d = series(8, 44);
  w.run('Charts.line(__h, __d, { names: ["rl_5h:claude"] })');
  h.clientWidth = 250;
  ros(w)[0].cb();
  assert.deepEqual(plain(u.log), [], 'nothing was drawn while hidden');
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  assert.equal(w.get('Charts.paused'), false);
  assert.deepEqual(plain(u.log), ['setSize', 'setData'], 'one setData with the newest data, one setSize');
  assert.equal(u.data[0].length, 8);
  assert.equal(u.size.width, 250);
  w.run('Charts.resume()');
  assert.equal(u.log.length, 2, 'resume with nothing pending does nothing');
});

test('line: without uPlot it throws a clear error (the page awaits Charts.ready() first)', () => {
  const w = cWorld({ uplot: false });
  const h = host(w);
  w.ctx.__h = h; w.ctx.__d = series(4);
  assert.throws(() => w.run('Charts.line(__h, __d, {})'), /Charts\.ready/);
});

/** A canvas context that records every call. */
function fakeCtx() {
  const log = [];
  const props = {};
  const ctx = new Proxy(props, {
    get(t, k) {
      if (k === 'log') return log;
      if (k === 'measureText') return () => ({ width: 30 });
      if (k in t) return t[k];
      return (...a) => { log.push([String(k), ...a]); };
    },
    set(t, k, v) { t[k] = v; return true; },
  });
  return { ctx, log };
}

test('line overlay: thresholds, in-window marks with non-overlapping labels, and reset ticks go on the canvas', () => {
  const w = cWorld({ extra: { console: { log() {}, error() {} } } });
  const h = host(w, 400);
  w.ctx.__h = h; w.ctx.__d = series(6);
  const marks = [{ t: T0 + 300, label: '5h limit', cls: 'bad' }, { t: T0 + 330, label: '7d limit', cls: 'warn' }, { t: T0 - 99999, label: 'outside' }];
  w.ctx.__marks = marks;
  w.run(`Charts.line(__h, __d, { names: ["rl_5h:claude"], yMax: 100, thresholds: [60, 85, 250], marks: __marks, resets: [{ t: ${T0 + 600}, label: "reset 22:05" }, { t: ${T0 + 99999} }] })`);
  const u = plots(w)[0];
  const { ctx, log } = fakeCtx();
  const fakeU = { ctx, bbox: { left: 10, top: 5, width: 300, height: 100 }, pxRatio: 1, scales: { y: { min: 0, max: 100 } }, valToPos: (v, s) => (s === 'y' ? 105 - v : 10 + (v - T0) * 0.5), cursor: {} };
  u.opts.hooks.draw[0](fakeU);
  const calls = (name) => log.filter((c) => c[0] === name);
  assert.equal(calls('save').length, 1);
  assert.equal(calls('restore').length, 1);
  assert.equal(calls('clip').length, 1, 'clipped to the plot area');
  const lines = calls('lineTo');
  // 2 thresholds (250 is off the scale) + 2 marks (the outside one is skipped) + 1 reset tick (the far one is skipped)
  assert.equal(lines.length, 5);
  const texts = calls('fillText').map((c) => c[1]);
  assert.ok(texts.includes('60%') && texts.includes('85%'));
  assert.ok(texts.includes('5h limit'));
  assert.ok(!texts.includes('7d limit'), 'a label that would overlap the previous one is skipped (its line is still drawn)');
  assert.ok(!texts.includes('outside'));
  assert.ok(texts.includes('reset 22:05'));
  // a hostile entry never blanks the chart
  const { ctx: c2 } = fakeCtx();
  assert.doesNotThrow(() => u.opts.hooks.draw[0]({ ...fakeU, ctx: c2, valToPos: () => { throw new Error('bad'); } }));
});

// ---------------------------------------------------------------- stacked bars

const bday = (i, by) => ({ day: `2026-09-${String(i + 1).padStart(2, '0')}`, total: Object.values(by).reduce((a, b) => a + b, 0), zero: false, tokens: 1000000 * (i + 1), hours: 1.5, by_project: by, by_agent: { claude: 1 } });

test('stackedBars: rects with titles, (unattributed) its own segment with its tooltip, a hatched zero day, a chip per segment', () => {
  const w = cWorld();
  const h = host(w, 400);
  w.ctx.__h = h; w.ctx.__days = DAYS;
  const got = w.run('Charts.stackedBars(__h, __days, { by: "project", top: 2 })');
  const svgEl = h.querySelector('svg');
  assert.ok(svgEl);
  assert.equal(svgEl.getAttribute('viewBox'), '0 0 400 170');
  assert.equal(got.svg, svgEl);
  const rects = h.querySelectorAll('rect');
  assert.ok(rects.length <= 240);
  const unattr = rects.filter((r) => r.classList.contains('seg-unattributed'));
  assert.equal(unattr.length, 2, 'one per day that has it');
  assert.ok(unattr.every((r) => /sessions the board did not start/.test(text(r.querySelector('title')))));
  assert.match(text(unattr[0].querySelector('title')), /^2026-10-02 · \(unattributed\) \$3\.00 \(sessions the board did not start\) · day \$30\.00 · 3k tok$/);
  const a = rects.find((r) => r.classList.contains('seg-0'));
  assert.match(text(a.querySelector('title')), /^2026-10-02 · A \$10\.00 · day \$30\.00 · 3k tok$/);
  const zero = rects.filter((r) => r.classList.contains('bar-zero'));
  assert.equal(zero.length, 1);
  assert.equal(zero[0].getAttribute('fill'), 'url(#ch-hz)');
  assert.match(text(zero[0].querySelector('title')), /^2026-10-03 · no spend$/);
  assert.equal(h.querySelectorAll('pattern').filter((p) => p.getAttribute('id') === 'ch-hz').length, 1);
  const chips = h.querySelectorAll('.legend .lg');
  assert.deepEqual(chips.map((c) => text(c.querySelector('.lg-l'))), ['A', 'B', 'other', '(unattributed)']);
  assert.deepEqual(chips.map((c) => text(c.querySelector('.lg-v'))), ['$12.00', '$8.00', '$10.00', '$12.00']);
  assert.ok(chips[3].querySelector('.sw').classList.contains('seg-unattributed'), 'the swatch carries the segment class');
  assert.equal(chips[3].getAttribute('title'), 'sessions the board did not start');
  assert.match(chips[2].getAttribute('title'), /^3 more: C, D, E$/);
  assert.match(svgEl.getAttribute('aria-label'), /Cost per day over the last 3 days, by project: \$42\.00 API-equivalent/);
  assert.ok(h.classList.contains('chart'));
  assert.equal(got.rows.length, 3);
  assert.equal(got.keys.length, 4);
});

test('stackedBars: hover and tap write the readout and call onHover; the geometry follows the scale', () => {
  const w = cWorld({ extra: { console: { log() {}, error() {} } } });
  const h = host(w, 400);
  w.ctx.__h = h; w.ctx.__days = DAYS;
  w.ctx.__seen = [];
  w.run('Charts.stackedBars(__h, __days, { by: "project", top: 2, onHover: (i) => __seen.push(i) })');
  const read = h.querySelector('.chart-read');
  assert.match(text(read), /Hover or tap a bar/);
  const rects = h.querySelectorAll('rect');
  const a = rects.find((r) => r.classList.contains('seg-0'));
  a.click();
  assert.match(text(read), /^2026-10-02 · A \$10\.00/);
  const seen = plain(w.get('__seen'));
  assert.deepEqual(seen[0], { day: '2026-10-02', key: 'A', v: 10, total: 30, zero: false, tokens: 3000, hours: 0, text: seen[0].text });
  assert.match(seen[0].text, /^2026-10-02 · A \$10\.00/);
  rects.find((r) => r.classList.contains('bar-zero')).click();
  assert.match(text(read), /^2026-10-03 · no spend/);
  assert.equal(plain(w.get('__seen'))[1].zero, true);
  // max 30 -> scale 30 over innerH 142: the A segment (10) is a third of the plot height
  const inner = 170 - 8 - 20;
  assert.ok(Math.abs(Number(a.getAttribute('height')) - (10 * inner) / 30) < 0.2);
  assert.equal(Number(a.getAttribute('y')) + Number(a.getAttribute('height')), 8 + inner, 'the first segment sits on the baseline');
  // a handler that throws never breaks the chart
  w.run('Charts.stackedBars(__h, __days, { onHover: () => { throw new Error("x"); } })');
  assert.doesNotThrow(() => h.querySelectorAll('rect').find((r) => r.classList.contains('seg-0')).click());
});

test('stackedBars: unpriced segments get the hatch pattern and a note in their title; by agent uses the agent classes', () => {
  const w = cWorld();
  const h = host(w, 400);
  w.ctx.__h = h; w.ctx.__days = DAYS;
  w.run('Charts.stackedBars(__h, __days, { by: "project", top: 2, unpriced: new Set(["A"]) })');
  const hx = h.querySelectorAll('rect').filter((r) => r.classList.contains('seg-hx'));
  assert.equal(hx.length, 2, 'A on two days');
  assert.ok(hx.every((r) => r.getAttribute('fill') === 'url(#ch-hx-seg-0)'));
  assert.ok(hx[0].classList.contains('seg-0'));
  assert.match(text(hx[0].querySelector('title')), /some sessions have tokens but no price$/);
  assert.equal(h.querySelectorAll('pattern').filter((p) => p.getAttribute('id') === 'ch-hx-seg-0').length, 1);
  assert.ok(h.querySelectorAll('.legend .lg')[0].classList.contains('hatch'));
  assert.equal(h.querySelectorAll('rect').filter((r) => r.classList.contains('seg-1')).every((r) => r.getAttribute('fill') === null), true);
  // an unpriced day
  w.run('Charts.stackedBars(__h, __days, { by: "agent", unpriced: ["2026-10-04"] })');
  const agents = h.querySelectorAll('rect').filter((r) => r.classList.contains('agent-claude') || r.classList.contains('agent-codex'));
  assert.ok(agents.length >= 3);
  assert.equal(h.querySelectorAll('rect').filter((r) => r.classList.contains('seg-hx')).length, 1, 'the 4th of October only');
});

test('stackedBars: never more than 240 rects, whatever the days and keys; labels and ticks stay finite', () => {
  const w = cWorld();
  const h = host(w, 360);
  const days = Array.from({ length: 30 }, (_, i) => bday(i, Object.fromEntries(Array.from({ length: 14 }, (__, k) => ['proj' + k, 1 + ((i + k) % 5)]).concat([['(unattributed)', 4]]))));
  w.ctx.__h = h; w.ctx.__days = days;
  w.run('Charts.stackedBars(__h, __days, { by: "project", top: 6 })');
  const rects = h.querySelectorAll('rect');
  assert.ok(rects.length <= 240, `${rects.length} rects`);
  assert.equal(h.querySelectorAll('.legend .lg').length, 8, 'six projects, other and (unattributed)');
  w.run('Charts.stackedBars(__h, __days, { by: "project", top: 99 })');
  assert.ok(h.querySelectorAll('rect').length <= 240, 'top is capped so the rect budget holds');
  const body = h.querySelector('svg').textContent;
  assert.doesNotMatch(body, /NaN|undefined|Infinity/);
  const attrs = h.querySelectorAll('rect').flatMap((r) => ['x', 'y', 'width', 'height'].map((k) => r.getAttribute(k)));
  assert.ok(attrs.every((v) => /^-?\d+(\.\d+)?$/.test(v)), 'every coordinate is a finite number');
  const labels = h.querySelectorAll('text.ch-ax').map(text);
  assert.ok(labels.includes('Sep 1'), 'the first day names its month');
});

test('stackedBars: no days shows the empty text; a width change re-renders; paused it waits for resume', () => {
  const w = cWorld();
  const h = host(w, 400);
  w.ctx.__h = h;
  w.run('Charts.stackedBars(__h, [], {})');
  assert.match(text(h), /No daily cost yet: it appears once a session has spent something\./);
  w.ctx.__days = DAYS;
  w.run('Charts.stackedBars(__h, __days, {})');
  assert.equal(h.querySelectorAll('.chart-empty').length, 0);
  assert.equal(h.querySelector('svg').getAttribute('viewBox'), '0 0 400 170');
  h.clientWidth = 320;
  ros(w)[0].cb();
  assert.equal(h.querySelector('svg').getAttribute('viewBox'), '0 0 320 170', 'redrawn at the new width so 1 unit is 1 px');
  w.run('Charts.pause()');
  h.clientWidth = 500;
  ros(w)[0].cb();
  assert.equal(h.querySelector('svg').getAttribute('viewBox'), '0 0 320 170', 'paused: untouched');
  w.run('Charts.resume()');
  assert.equal(h.querySelector('svg').getAttribute('viewBox'), '0 0 500 170');
  assert.equal(h.querySelectorAll('svg').length, 1);
  w.run('Charts.destroyAll()');
  assert.equal(text(h), '');
  assert.ok(ros(w).every((r) => r.disconnected));
});

// ---------------------------------------------------------------- Gantt

const GEV = [
  ev(T0 + 100, 'c1', 1, { p: 'alpha', r: 'root', s: 's1', a: 'claude' }), ev(T0 + 1000, 'c1', 2, { p: 'alpha', r: 'root', s: 's1', a: 'claude' }), ev(T0 + 2000, 'c1', 3, { p: 'alpha', s: 's1' }),
  ev(T0 + 500, 'c2', 1, { p: 'beta', r: 'web', s: 's2', a: 'claude' }), ev(T0 + 3000, 'c2', 5, { p: 'beta', r: 'web', s: 's2' }),
];

test('gantt: one row per session, spans by state class, ended rows fade, legend chips for the states present', () => {
  const w = cWorld();
  const h = host(w, 400);
  w.ctx.__h = h; w.ctx.__ev = GEV;
  const got = w.run(`Charts.gantt(__h, __ev, { since: ${T0}, until: ${T0 + 4000} })`);
  const rows = h.querySelectorAll('g.g-row');
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((r) => r.getAttribute('data-key')), ['c2', 'c1'], 'most recently active first');
  const c2 = rows[0];
  assert.ok(c2.classList.contains('ended'));
  assert.equal(rows[1].classList.contains('ended'), false);
  assert.deepEqual(c2.querySelectorAll('rect').map((r) => r.getAttribute('class')), ['sp st-1']);
  assert.deepEqual(rows[1].querySelectorAll('rect').map((r) => r.getAttribute('class')), ['sp st-1', 'sp st-2', 'sp st-3']);
  assert.equal(c2.querySelector('text').childNodes[0].textContent, 'beta/web · s2');
  assert.match(text(c2.querySelector('text title')), /^beta\/web · s2 · ended$/, 'the tooltip says it ended');
  assert.equal(rows[1].querySelector('text').childNodes[0].textContent, 'alpha · s1');
  assert.match(text(rows[1].querySelectorAll('rect')[1].querySelector('title')), /^alpha · s1 · waiting · \d\d:\d\d to \d\d:\d\d \(\d+m\)$/);
  const chips = h.querySelectorAll('.legend .lg');
  assert.deepEqual(chips.map((c) => c.querySelector('.sw').getAttribute('class')), ['sw st-1', 'sw st-2', 'sw st-3', 'sw st-5']);
  assert.match(text(chips[0]), /working/);
  assert.match(text(chips[3]), /ended/);
  assert.equal(got.rows.length, 2);
  assert.equal(got.truncated, 0);
  assert.equal(h.querySelectorAll('.chart-note').length, 0);
  assert.match(h.querySelector('svg').getAttribute('aria-label'), /^Session timeline: 2 sessions over /);
  for (const r of h.querySelectorAll('rect')) assert.ok(Number(r.getAttribute('width')) >= 1.5 && Number.isFinite(Number(r.getAttribute('x'))));
});

test('gantt: caps the rows, says how many more, accepts the whole body, and notes a server-side cut', () => {
  const w = cWorld();
  const h = host(w, 400);
  const evs = Array.from({ length: 36 }, (_, i) => ev(T0 + i * 10, 'k' + i, 1, { p: 'p' + i, s: 's' }));
  w.ctx.__h = h; w.ctx.__body = { events: evs, truncated: true };
  w.run(`Charts.gantt(__h, __body, { since: ${T0}, until: ${T0 + 1000} })`);
  assert.equal(h.querySelectorAll('g.g-row').length, 30);
  const notes = h.querySelectorAll('.chart-note').map(text);
  assert.equal(notes[0], '6 more sessions not shown');
  assert.match(notes[1], /server cut the event list/);
  w.run(`Charts.gantt(__h, __body, { since: ${T0}, until: ${T0 + 1000}, maxRows: 35 })`);
  assert.equal(h.querySelectorAll('.chart-note').map(text)[0], '1 more session not shown');
  assert.equal(h.querySelectorAll('svg').length, 1, 'redrawn in place');
});

test('gantt: an empty body, an empty list or no row inside the window says what to expect', () => {
  const w = cWorld();
  const h = host(w, 400);
  w.ctx.__h = h;
  for (const body of ['{}', '[]', 'undefined', 'null', '{ events: "no" }', '[{ t: 5, key: "a", v: 5 }]']) {
    w.run(`Charts.gantt(__h, ${body}, { since: ${T0}, until: ${T0 + 1000} })`);
    assert.match(text(h), /No session activity in this window: rows appear as sessions start and change state\./, body);
    assert.equal(h.querySelectorAll('svg').length, 0);
  }
  w.run('Charts.gantt(__h, {}, { empty: "Quiet: start a session." })');
  assert.equal(text(h), 'Quiet: start a session.');
});

// ---------------------------------------------------------------- heatmap

test('heatmap: 7 x 24 cells with levels, weekday and 3-hourly labels, the hourly profile, titles in the readout', () => {
  const w = cWorld();
  const h = host(w, 400);
  const grid = Array.from({ length: 7 }, (_, d) => Array.from({ length: 24 }, (__, hr) => (d === 1 && hr === 21 ? 14 : d === 0 && hr === 9 ? 3 : 0)));
  const hourly = Array.from({ length: 24 }, (_, hr) => (hr === 21 ? 14 : hr === 9 ? 3 : 0));
  w.ctx.__h = h; w.ctx.__g = grid; w.ctx.__hr = hourly;
  const node = w.run('Charts.heatmap(__h, __g, __hr, { tzLabel: "UTC+5:45" })');
  assert.equal(node, h.querySelector('.heat-grid'));
  const cells = h.querySelectorAll('.cell');
  assert.equal(cells.length, 168);
  assert.equal(cells.filter((c) => c.classList.contains('lv0')).length, 166);
  const tue21 = cells[1 * 24 + 21];
  assert.ok(tue21.classList.contains('lv4'));
  assert.equal(tue21.getAttribute('title'), 'Tue 21:00 · 14 events');
  assert.ok(cells[9].classList.contains('lv1'), '3 of 14 is the lowest level, never 0');
  assert.equal(cells[9].getAttribute('title'), 'Mon 09:00 · 3 events');
  assert.deepEqual(h.querySelectorAll('.heat-day').slice(0, 7).map(text), ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']);
  assert.deepEqual(h.querySelectorAll('.heat-hr').map(text).filter(Boolean), ['00', '03', '06', '09', '12', '15', '18', '21']);
  assert.equal(h.querySelectorAll('.heat-hr').length, 24);
  const prof = h.querySelectorAll('.hb');
  assert.equal(prof.length, 24);
  assert.equal(prof[21].querySelector('i').style.height, '100%');
  assert.equal(prof[0].querySelector('i').style.height, '0');
  assert.equal(prof[21].getAttribute('title'), '21:00 · 14 events');
  assert.match(node.getAttribute('aria-label'), /busiest hour 21:00, 14 events/);
  assert.equal(text(h.querySelector('.heat-tz')), 'UTC+5:45');
  tue21.click();
  assert.equal(text(h.querySelector('.chart-read')), 'Tue 21:00 · 14 events');
  assert.ok(tue21.classList.contains('sel'));
  cells[9].click();
  assert.equal(tue21.classList.contains('sel'), false, 'one selection at a time');
  assert.equal(text(h.querySelector('.chart-read')), 'Mon 09:00 · 3 events');
  h.querySelectorAll('.hb')[21].click();
  assert.equal(text(h.querySelector('.chart-read')), '21:00 · 14 events');
});

test('heatmap: the profile defaults to the column sums, junk reads as 0, nothing recorded says what to do', () => {
  const w = cWorld();
  const h = host(w, 400);
  w.ctx.__h = h;
  w.ctx.__g = [[...Array(24).fill(1)], [...Array(24).fill('x')], null];
  w.run('Charts.heatmap(__h, __g, undefined, {})');
  assert.equal(h.querySelectorAll('.cell').length, 168);
  assert.equal(h.querySelectorAll('.hb')[5].getAttribute('title'), '05:00 · 1 event');
  assert.doesNotMatch(text(h), /NaN|undefined/);
  for (const g of ['null', '[]', 'undefined', 'Array.from({length: 7}, () => Array(24).fill(0))']) {
    assert.equal(w.run(`Charts.heatmap(__h, ${g}, undefined, {})`), null);
    assert.match(text(h), /No hook events recorded yet: start a session and this fills in\./);
  }
});

// ---------------------------------------------------------------- sparkline

test('spark: polylines per run of known points (a null is a gap), the Home class names, appended to an emptied host', () => {
  const w = cWorld();
  const h = host(w);
  h.append('old');
  w.ctx.__h = h;
  const node = w.run('Charts.spark(__h, [0, 60, 120, 180, 240, 300], [10, 20, null, null, 60, 70], { w: 240, h: 44 })');
  assert.equal(node.getAttribute('class'), 'spark');
  assert.equal(node.getAttribute('viewBox'), '0 0 240 44');
  assert.equal(h.querySelectorAll('svg').length, 1);
  assert.equal(text(h).replace(/\s+/g, ''), text(node).replace(/\s+/g, ''), 'the old content is gone');
  const lines = node.querySelectorAll('polyline.sp-line');
  assert.equal(lines.length, 2);
  assert.equal(lines[0].getAttribute('points').split(' ').length, 2);
  assert.equal(node.querySelectorAll('line.sp-guide').length, 1, 'the 85 % guide, like Home');
  assert.equal(node.querySelectorAll('line.sp-base').length, 1);
  assert.match(node.getAttribute('aria-label'), /^Trend: now 70%, peak 70%$/);
  const [x2, y2] = lines[1].getAttribute('points').split(' ')[1].split(',').map(Number);
  assert.equal(x2, 240);
  assert.equal(y2, +(44 - 3 - 0.7 * 38).toFixed(1));
  // a lone point is a short stub, not a vanished line
  const lone = w.run('Charts.spark(null, [0, 1, 2], [null, 50, null], {})');
  assert.equal(lone.querySelectorAll('line.sp-line').length, 1);
  assert.equal(lone.querySelectorAll('polyline').length, 0);
  // auto range and no guide for a non-percentage series
  const auto = w.run('Charts.spark(null, [0, 1, 2], [1, 2, 4], { max: null, label: "cost" })');
  assert.equal(auto.querySelectorAll('line.sp-guide').length, 0);
  assert.match(auto.getAttribute('aria-label'), /^cost: now 4, peak 4$/);
  assert.equal(w.run('Charts.spark(__h, [0], [5], {})'), null);
  assert.equal(w.run('Charts.spark(__h, [0, 1], [null, null], {})'), null);
  assert.equal(h.querySelectorAll('svg').length, 0, 'nothing to draw empties the host');
  assert.equal(w.run('Charts.spark(__h, undefined, undefined)'), null);
});

// ---------------------------------------------------------------- hygiene

test('charts.js and charts.css carry nothing the CSP or the static scan forbids', () => {
  const js = fs.readFileSync(path.join(STATIC, 'charts.js'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  const css = fs.readFileSync(path.join(STATIC, 'charts.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  for (const [name, re] of [['innerHTML', /innerHTML|insertAdjacentHTML|outerHTML/], ['cssText', /cssText/], ['style attr', /setAttribute\(\s*['"]style/], ['style key', /\bstyle\s*:/], ['bp5', /bp5-/], ['eval', /\beval\s*\(/]]) {
    assert.doesNotMatch(js, re, `charts.js: ${name}`);
  }
  assert.doesNotMatch(css, /@import|data:|https?:\/\//, 'charts.css: no imports, data: URIs or remote hosts');
  assert.doesNotMatch(js, /https?:\/\//, 'charts.js: no absolute URLs');
  for (const token of ['--st-idle', '--st-working', '--st-waiting', '--st-done', '--st-errored', '--st-ended', '--agent-claude', '--agent-codex', '--agent-shell', '--c-unattributed']) {
    assert.match(css, new RegExp(token + ':'), `charts.css defines ${token}`);
  }
  for (const cls of ['.chart', '.chart.loading', '.legend', '.lv0', '.lv1', '.lv2', '.lv3', '.lv4', '.seg-unattributed', '.st-0', '.st-5', '.agent-claude']) {
    assert.ok(css.includes(cls), `charts.css styles ${cls}`);
  }
  assert.match(css, /\.chart \{[^}]*touch-action:pan-y/);
  assert.match(css, /@media \(max-width:599px\)/);
});
