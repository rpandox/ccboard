// Contract tests for app/static/pages/widgets.js (v0.5.5): the usage card (gauges from the state, a sparkline and cost bars from two fetches that
// never ride the state poll, hidden until data came back, errors swallowed, destroy) and the rate-limit callout in #banner (idempotent, comes back
// after renderBanner() emptied the banner, dismissed per episode, gone when the window resets). Real core.js, components.js, router.js and widgets.js
// on minidom's DOM inside the vm harness.
// v0.5.17 (the way into the Usage page): the card's header link and both gauges point at #/usage; the sparkline is drawn by Charts.spark when charts.js
// is loaded and by the card's own sparkline() when it is not, throws or draws nothing; widgets.js never reads Charts at load.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const NOW = Date.now();
const EPOCH = (minsAgo) => Math.floor(NOW / 1000) - minsAgo * 60;
const ISO = (minsAgo) => new Date(NOW - minsAgo * 60000).toISOString();
const tick = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');

/** A world with the DOM, the scripts the widgets need and an api() that records and answers from __answers (prefix -> value | Error | fn). */
function wWorld(extra = {}, more = []) {
  const w = makeWorld(extra);
  installDom(w);
  for (const f of ['core.js', 'components.js', 'router.js', ...more, 'pages/widgets.js']) w.load(f);
  w.ctx.__calls = [];
  w.ctx.__answers = {};
  w.run(`api = async (method, path) => {
    __calls.push({ method, path });
    for (const [prefix, v] of Object.entries(__answers)) {
      if (!path.startsWith(prefix)) continue;
      if (v && v.__error) throw new Error(v.__error);
      return typeof v === 'function' ? v(path) : v;
    }
    return {};
  };`);
  return w;
}

const calls = (w) => plain(w.get('__calls'));
const SERIES = { since: ISO(1440), until: ISO(0), step: 1800, t: Array.from({ length: 8 }, (_, i) => EPOCH(1440) + i * 1800),
  series: { 'rl_5h:claude': [10, 20, null, null, 60, 70, 65, 42] }, meta: {} };
const day = (d, total, zero) => ({ day: `2026-10-0${d}`, total, zero: !!zero, by_agent: { claude: total }, tokens: 1000, hours: 1 });
const SUMMARY = { generated_at: ISO(0), tz_min: 345, daily: [day(1, 10), day(2, 0, true), day(3, 30), day(4, 5), day(5, 0, true), day(6, 20), day(7, 40)] };
const STATE = () => ({
  usage: { value: { five_hour: { used_percentage: 42, resets_at: EPOCH(-150) }, seven_day: { used_percentage: 91, resets_at: EPOCH(-4000) } }, at: ISO(0) },
  block: { value: { available: true, active: true, burn_cost_per_hour: 11.9, cost_usd: 38.2, remaining_minutes: 160, projected_cost: 61.4 }, at: ISO(0) },
  backup: { at: ISO(720), status: 'ok' },
});

/** The card in a fresh host under #page; answers both fetches unless told otherwise. */
function card(w, { answers = { '/api/series': SERIES, '/api/usage/summary': SUMMARY }, st = STATE() } = {}) {
  Object.assign(w.ctx.__answers, answers);
  w.run('globalThis.__host = document.createElement("section"); document.querySelector("#page").append(__host)');
  w.ctx.__st = st;
  w.run('globalThis.__card = Widgets.usageCard(__host); __card.update(__st)');
  return { host: w.get('__host'), root: w.get('__card').root, card: w.get('__card') };
}

// ---------------------------------------------------------------- the usage card

test('the card is hidden until a fetch returned data, then shows the gauges, the burn, the backup chip, the sparkline and the bars', async () => {
  const w = wWorld();
  const { root, card: c } = card(w);
  assert.equal(root.classList.contains('hidden'), true, 'nothing fetched yet');
  await c.refresh();
  assert.equal(root.classList.contains('hidden'), false);
  const gauges = root.querySelectorAll('.gauge');
  assert.deepEqual(gauges.map((g) => text(g.querySelector('.g-label'))), ['5H', '7D']);
  assert.deepEqual(gauges.map((g) => text(g.querySelector('.g-val'))), ['42%', '91%']);
  assert.match(text(gauges[0].querySelector('.g-reset')), /^resets in 2h\d+m$/);
  assert.match(text(gauges[1].querySelector('.g-reset')), /^resets in 2d/);
  assert.ok(gauges[0].classList.contains('ok') && gauges[1].classList.contains('bad'), 'tones follow 60 / 85 like the topbar pills');
  assert.equal(gauges[0].querySelector('.g-bar i').style.width, '42%');
  const burn = root.querySelector('.uc-burn');
  assert.equal(burn.classList.contains('hidden'), false);
  assert.match(text(burn), /\$11\.90\/h burn · \$38\.20 this block · 160 min left · ~\$61\.40 projected/);
  const backup = root.querySelector('.uc-backup');
  assert.equal(backup.classList.contains('hidden'), false);
  assert.match(text(backup), /^backup ok 12h ago$/);
  assert.equal(backup.getAttribute('href'), '#/settings?sec=box');
  // the sparkline: two runs of known points (the nulls are a gap, not a zero line)
  assert.equal(root.querySelector('.uc-spark').classList.contains('hidden'), false);
  assert.equal(root.querySelectorAll('polyline.sp-line').length, 2);
  assert.equal(text(root.querySelector('.uc-spark .uc-cap-v')), 'now 42%');
  // the bars: one rect per day, a day without spend hatched through the 'zero' class
  const bars = root.querySelectorAll('rect.cb');
  assert.equal(bars.length, 7);
  assert.equal(root.querySelectorAll('rect.cb.zero').length, 2);
  assert.ok(bars[0].querySelector('title') && /API-equivalent/.test(text(bars[0].querySelector('title'))), 'every dollar is labelled');
  assert.equal(text(root.querySelector('.uc-bars .uc-cap-v')), '$105', 'a hundred dollars or more shows no cents');
  assert.deepEqual(root.querySelectorAll('.uc-days span').map(text), ['4', '5', '6', '7', '8', '9', '10'].map((_, i) => 'SMTWTFS'.charAt(new Date(Date.UTC(2026, 9, i + 1)).getUTCDay())));
  c.destroy();
});

test('fetches: once on mount, the two paths and the 7-day window in the viewer\'s zone; update() never fetches', async () => {
  const w = wWorld();
  const { card: c } = card(w);
  await c.refresh();
  const paths = calls(w).map((x) => x.path);
  assert.equal(paths.length, 2, 'the mount fetched once and refresh() joined the one in flight');
  assert.equal(paths[0], '/api/series?series=rl_5h&key=claude&since=24h');
  assert.match(paths[1], /^\/api\/usage\/summary\?days=7&tz_min=-?\d+$/);
  const tz = Number(paths[1].split('tz_min=')[1]);
  assert.ok(tz >= -720 && tz <= 840);
  for (let i = 0; i < 5; i++) w.run('__card.update(__st)');                    // the 3 s state poll
  assert.equal(calls(w).length, 2, 'the poll never fetches');
  await c.refresh();
  assert.equal(calls(w).length, 4, 'an explicit refresh does');
  c.destroy();
});

test('an error is swallowed: the card keeps what it had, or stays hidden', async () => {
  const w = wWorld();
  const { root, card: c } = card(w, { answers: { '/api/series': { __error: '502 Bad gateway' }, '/api/usage/summary': { __error: 'boom' } } });
  await assert.doesNotReject(c.refresh());
  assert.equal(root.classList.contains('hidden'), true, 'no data ever came back');
  assert.equal(root.querySelector('.gauge').classList.contains('hidden'), false, 'the gauges still follow the state');
  w.ctx.__answers['/api/series'] = SERIES;
  w.ctx.__answers['/api/usage/summary'] = SUMMARY;
  await c.refresh();
  assert.equal(root.classList.contains('hidden'), false);
  w.ctx.__answers['/api/series'] = { __error: 'gone' };
  w.ctx.__answers['/api/usage/summary'] = { daily: [] };                        // an empty answer is not data either
  await c.refresh();
  assert.equal(root.classList.contains('hidden'), false, 'the earlier data stays');
  assert.equal(root.querySelectorAll('rect.cb').length, 7);
  c.destroy();
});

test('an answer without data (a fresh install) keeps the card hidden', async () => {
  const w = wWorld();
  const { root, card: c } = card(w, { answers: { '/api/series': { t: [], series: {} }, '/api/usage/summary': { daily: [] } } });
  await c.refresh();
  assert.equal(root.classList.contains('hidden'), true);
  c.destroy();
});

test('the gauges hide for a window the state does not carry; the burn and the backup chip follow the state', async () => {
  const w = wWorld();
  const { root, card: c } = card(w, { st: { usage: { value: { five_hour: { used_percentage: 61 } } }, block: { value: { available: false } }, backup: { at: ISO(30), status: 'failed' } } });
  await c.refresh();
  const g = root.querySelectorAll('.gauge');
  assert.equal(g[0].classList.contains('hidden'), false);
  assert.ok(g[0].classList.contains('warn'));
  assert.equal(text(g[0].querySelector('.g-reset')), '', 'no reset time, no countdown');
  assert.equal(g[1].classList.contains('hidden'), true, 'no 7D window in the state');
  assert.equal(root.querySelector('.uc-burn').classList.contains('hidden'), true);
  assert.match(text(root.querySelector('.uc-backup')), /^backup failed 30m ago$/);
  assert.ok(root.querySelector('.uc-backup').classList.contains('bad'));
  w.ctx.__st = { usage: null, block: null, backup: null };
  w.run('__card.update(__st)');
  assert.equal(root.querySelector('.gauge').classList.contains('hidden'), true);
  assert.equal(root.querySelector('.uc-backup').classList.contains('hidden'), true);
  c.destroy();
});

test('the minute timer refetches while the tab is visible and waits while it is hidden; destroy() stops it and removes the card', async () => {
  const timers = [];
  const cleared = [];
  const w = wWorld({ setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearInterval: (id) => cleared.push(id) });
  const { host, root, card: c } = card(w);
  await c.refresh();
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, 60000, 'every 60 s');
  const base = calls(w).length;
  w.document.hidden = true;
  timers[0].fn();
  await tick();
  assert.equal(calls(w).length, base, 'a hidden tab does not fetch');
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');                                     // shown again after a missed tick: refetch at once
  await tick();
  assert.equal(calls(w).length, base + 2, 'the missed refresh happens when the tab is shown');
  timers[0].fn();
  await tick();
  assert.equal(calls(w).length, base + 4, 'a visible tick refetches');
  c.destroy();
  assert.deepEqual(cleared, [1], 'the timer is cleared');
  assert.equal(host.children.length, 0, 'the card left its host');
  assert.equal(root.parentNode, null);
  const n = calls(w).length;
  await c.refresh();
  w.document.dispatch('visibilitychange');
  assert.equal(calls(w).length, n, 'a destroyed card fetches nothing');
});

test('two refreshes at once share one fetch', async () => {
  const w = wWorld();
  const { card: c } = card(w);
  const a = c.refresh();
  const b = c.refresh();
  assert.equal(a, b);
  await a;
  assert.equal(calls(w).length, 2);
  c.destroy();
});

test('sparkline and bars are plain svg: no style attribute, thin 2 px line, bars anchored on the baseline', () => {
  const w = wWorld();
  w.run('globalThis.__sp = Widgets.sparkline([0, 100, 200, 300], [10, null, 50, 100]); globalThis.__cb = Widgets.costBars([{ day: "2026-10-01", total: 4 }, { day: "2026-10-02", total: 8 }, { day: "2026-10-03", total: 0, zero: true }])');
  const sp = w.get('__sp');
  assert.equal(sp.tagName, 'SVG');
  assert.equal(sp.getAttribute('viewBox'), '0 0 240 44');
  assert.equal(sp.getAttribute('role'), 'img');
  assert.match(sp.getAttribute('aria-label'), /now 100%, peak 100%/);
  assert.equal(sp.querySelectorAll('polyline').length, 1, 'the run after the gap is one line');
  assert.equal(sp.querySelectorAll('line.sp-line').length, 1, 'a lone point before the gap is a short tick, not nothing');
  const cb = w.get('__cb');
  const rects = cb.querySelectorAll('rect.cb');
  assert.equal(rects.length, 3);
  const bottoms = rects.map((r) => Number(r.getAttribute('y')) + Number(r.getAttribute('height')));
  assert.deepEqual([...new Set(bottoms)], [56], 'every bar ends on the baseline');
  assert.ok(Number(rects[1].getAttribute('height')) > Number(rects[0].getAttribute('height')), 'taller for more spend');
  assert.ok(cb.querySelector('pattern#uc-hatch'), 'the hatch the zero class paints');
  for (const n of [sp, cb]) assert.equal(n.getAttribute('style'), null);
});

// ---------------------------------------------------------------- the rate-limit callout

const LIMIT = (over = {}) => ({ rate_limited: { value: { session: 'ccboard--ccboard--s1', message: '5-hour limit reached · resets 6:25am', kind: '5h', resets_at: EPOCH(-150), ...over }, at: ISO(5) } });
const banner = (w) => w.document.querySelector('#banner');
const callout = (w) => banner(w).querySelector('.limit-callout');
const show = (w, st) => { w.ctx.__stx = st; return w.run('Widgets.limitBanner(__stx)'); };

test('limitBanner puts a callout in #banner for a limit whose reset is ahead: kind, reset time and the session', () => {
  const w = wWorld();
  show(w, LIMIT());
  const n = callout(w);
  assert.ok(n, 'the callout is there');
  assert.ok(n.classList.contains('callout') && n.classList.contains('warn'));
  assert.match(text(n.querySelector('.lc-text')), /^Claude rate limit \(5h\) · resets \d{2}:\d{2} · s1$/);
  assert.ok(banner(w).classList.contains('limit-only'), 'alone in the banner: no second frame');
  show(w, LIMIT({ kind: '7d', resets_at: EPOCH(-3 * 24 * 60) }));
  assert.match(text(callout(w).querySelector('.lc-text')), /^Claude rate limit \(7d\) · resets (Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d{2}:\d{2} · s1$/, 'a reset more than a day away names the weekday');
  show(w, LIMIT({ kind: 'other', session: '' }));
  assert.match(text(callout(w).querySelector('.lc-text')), /^Claude rate limit · resets \d{2}:\d{2}$/);
});

test('limitBanner is idempotent, comes back after renderBanner() emptied the banner, and leaves a banner message beside it', () => {
  const w = wWorld();
  show(w, LIMIT());
  const first = callout(w);
  show(w, LIMIT());
  assert.equal(callout(w), first, 'the same node: patched in place');
  assert.equal(banner(w).querySelectorAll('.limit-callout').length, 1);
  banner(w).textContent = '';                                                  // renderBanner() does this on every render
  banner(w).className = '';
  assert.equal(callout(w), null);
  show(w, LIMIT());
  assert.ok(callout(w) && callout(w) !== first, 'put back');
  assert.equal(banner(w).querySelectorAll('.limit-callout').length, 1);
  // another message in the banner: the callout joins it and the banner keeps its own frame
  banner(w).textContent = '';
  banner(w).append(w.run('el("span", { text: "Offline: showing the last known state" })'));
  show(w, LIMIT());
  assert.equal(banner(w).children.length, 2);
  assert.equal(banner(w).classList.contains('limit-only'), false);
});

test('limitBanner reads a flat record, ignores a limit with no reset time or one that is over, and removes the callout when it ends', () => {
  const w = wWorld();
  show(w, { rate_limited: { session: 'ccboard--ccboard--s1', message: 'limit', kind: '5h', resets_at: EPOCH(-60) } });
  assert.ok(callout(w), 'a flat record reads like the kv wrapper');
  show(w, LIMIT({ resets_at: EPOCH(5) }));
  assert.equal(callout(w), null, 'the window has reset');
  assert.equal(banner(w).classList.contains('limit-only'), false);
  show(w, LIMIT());
  assert.ok(callout(w));
  show(w, { rate_limited: null });
  assert.equal(callout(w), null, 'no limit, no callout');
  show(w, LIMIT({ resets_at: null }));
  assert.equal(callout(w), null, 'no reset time to count down to');
  assert.equal(w.run('Widgets.limitBanner(null)'), null);
});

test('dismissing remembers the episode in sessionStorage; a new window shows again', () => {
  const w = wWorld();
  const st = LIMIT();
  show(w, st);
  callout(w).querySelector('button').click();
  assert.equal(callout(w), null);
  assert.equal(banner(w).classList.contains('limit-only'), false);
  assert.equal(Number(w.sessionStorage.getItem('ccboard:limit:dismissed')), st.rate_limited.value.resets_at);
  show(w, st);
  assert.equal(callout(w), null, 'the same episode stays quiet through every later render');
  show(w, LIMIT({ resets_at: st.rate_limited.value.resets_at + 4 }));
  assert.equal(callout(w), null, 'a few seconds of jitter in the parsed reset time is the same episode');
  show(w, LIMIT({ resets_at: st.rate_limited.value.resets_at + 5 * 3600 }));
  assert.ok(callout(w), 'the next 5-hour window is a new episode');
});

test('limitBanner survives storage that throws', () => {
  const w = wWorld({ sessionStorage: { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } } });
  show(w, LIMIT());
  assert.ok(callout(w));
  assert.doesNotThrow(() => callout(w).querySelector('button').click());
  assert.equal(callout(w), null, 'dismissed for this render at least');
});

test('the backup chip tells a partial run apart: snapshot ok, some mirror pushes rejected (warn, never bad)', async () => {
  const w = wWorld();
  const { root, card: c } = card(w, { st: { usage: null, block: null, backup: { at: ISO(12 * 60), status: 'partial', warnings: ['push a/b: main: [rejected] (fetch first)', 'push c/d: main: [rejected] (non-fast-forward)'] } } });
  await c.refresh();
  const chip = root.querySelector('.uc-backup');
  assert.equal(chip.classList.contains('hidden'), false);
  assert.match(text(chip), /^backup ok 12h ago · 2 pushes rejected$/);
  assert.ok(chip.classList.contains('warn') && !chip.classList.contains('bad'));
  w.ctx.__st = { usage: null, block: null, backup: { at: ISO(1), status: 'partial', warnings: ['push a/b: main: [rejected]'] } };
  w.run('__card.update(__st)');
  assert.match(text(chip), /1 push rejected$/);
  c.destroy();
});


// ---------------------------------------------------------------- v0.5.17: the way into the Usage page

/** A Charts stand-in that draws like charts.js's spark: records the call, puts one <svg class="spark"> with a polyline into the host. */
const CHARTS_STUB = `globalThis.Charts = {
  calls: [],
  spark(host, t, vals, opts) {
    this.calls.push({ n: t.length, vals: vals.slice(), opts });
    host.append(svg('svg', { class: 'spark', viewBox: '0 0 ' + opts.w + ' ' + opts.h }, svg('polyline', { class: 'chart-spark', points: '0,0 1,1' })));
  },
};`;

test('the card points at the Usage page: a header link and both gauges are anchors to #/usage', async () => {
  const w = wWorld();
  const { root, card: c } = card(w);
  await c.refresh();
  const more = root.querySelector('.uc-head .uc-more');
  assert.ok(more, 'the header carries the link');
  assert.equal(more.tagName, 'A');
  assert.equal(more.getAttribute('href'), '#/usage');
  assert.equal(text(more), 'Usage →');
  assert.ok(more.classList.contains('small'));
  assert.equal(more.classList.contains('bp5-button'), false, 'a plain link: the semantic map only makes a button of a.btn');
  assert.ok(root.querySelector('.uc-head .uc-backup'), 'the backup chip stays in the header beside it');
  assert.equal(root.querySelector('.uc-head .uc-backup').getAttribute('href'), '#/settings?sec=box');
  const gauges = root.querySelectorAll('.gauge');
  assert.equal(gauges.length, 2);
  for (const g of gauges) {
    assert.equal(g.tagName, 'A', 'a gauge is a link');
    assert.equal(g.getAttribute('href'), '#/usage');
    assert.ok(g.getAttribute('title'), 'and keeps its tooltip');
  }
  assert.equal(root.querySelectorAll('a[href="#/usage"]').length, 3, 'one link in the header, one per gauge');
  c.destroy();
});

test('the Usage link shows even before any fetch answered: it is part of the card, which is hidden as a whole until data came back', () => {
  const w = wWorld();
  const { root, card: c } = card(w, { answers: { '/api/series': { __error: 'down' }, '/api/usage/summary': { __error: 'down' } } });
  assert.equal(root.classList.contains('hidden'), true);
  assert.ok(root.querySelector('.uc-more'), 'built with the card, so the first paint after the data has the way in');
  c.destroy();
});

test('widgets.js reads Charts only when the card paints: loading it with no Charts is fine, and the card then draws its own sparkline', async () => {
  const w = wWorld();
  assert.equal(w.run('typeof Charts'), 'undefined');
  const { root, card: c } = card(w);
  await c.refresh();
  assert.equal(root.querySelectorAll('polyline.sp-line').length, 2, 'the card\'s own sparkline');
  assert.equal(root.querySelector('svg.spark').getAttribute('viewBox'), '0 0 240 44');
  c.destroy();
});

test('with charts.js loaded the sparkline is Charts.spark: the host, the series arrays and the 240 x 44 box on a 0-100 scale; the caption and the aria label stay', async () => {
  const w = wWorld();
  w.run(CHARTS_STUB);
  const { root, card: c } = card(w);
  await c.refresh();
  const calls_ = plain(w.get('Charts.calls'));
  assert.equal(calls_.length, 1, 'one draw for the first data');
  assert.equal(calls_[0].n, 8);
  assert.deepEqual(calls_[0].vals, [10, 20, null, null, 60, 70, 65, 42], 'nulls go through as they are: the gap is Charts\' to draw');
  assert.deepEqual(calls_[0].opts, { w: 240, h: 44, min: 0, max: 100, guide: 85, label: '5-hour window, last 24 hours' }, 'the same box, 0-100 scale and 85 % guide as the card\'s own sparkline, and the label its aria text starts with');
  assert.equal(root.querySelectorAll('polyline.chart-spark').length, 1, 'the stub\'s polyline is in the card');
  assert.equal(root.querySelectorAll('polyline.sp-line').length, 0, 'and the card\'s own is not drawn on top');
  assert.equal(root.querySelectorAll('.uc-plot svg').filter((n) => n.classList.contains('spark')).length, 1, 'exactly one spark svg in the box');
  assert.equal(text(root.querySelector('.uc-spark .uc-cap-v')), 'now 42%');
  const svgNode = root.querySelector('.uc-spark svg');
  assert.equal(svgNode.getAttribute('role'), 'img', 'an svg without its own label gets one');
  assert.match(svgNode.getAttribute('aria-label'), /5-hour window, last 24 hours: now 42%, peak 70%/);
  c.destroy();
});

test('a second refresh redraws into the same box (no second svg), and the bars stay the card\'s own either way', async () => {
  const w = wWorld();
  w.run(CHARTS_STUB);
  const { root, card: c } = card(w);
  await c.refresh();
  await c.refresh();
  assert.equal(root.querySelectorAll('.uc-spark svg').length, 1);
  assert.equal(plain(w.get('Charts.calls')).length, 2);
  assert.equal(root.querySelectorAll('rect.cb').length, 7);
  c.destroy();
});

test('a Charts.spark that throws, draws nothing or is not a function leaves the card with its own sparkline', async () => {
  for (const [name, stub] of [
    ['throws', 'globalThis.Charts = { spark() { throw new Error("uPlot not ready"); } };'],
    ['draws nothing', 'globalThis.Charts = { spark() {} };'],
    ['no spark', 'globalThis.Charts = { line() {} };'],
    ['null', 'globalThis.Charts = null;'],
  ]) {
    const logged = [];
    const w = wWorld({ console: { log() {}, warn() {}, error: (...a) => logged.push(a) } });
    w.run(stub);
    const { root, card: c } = card(w);
    await assert.doesNotReject(c.refresh(), name);
    assert.equal(logged.length, name === 'throws' ? 1 : 0, `${name}: only a throwing Charts.spark is logged`);
    assert.equal(root.querySelectorAll('polyline.sp-line').length, 2, `${name}: the card's own sparkline`);
    assert.equal(root.querySelectorAll('.uc-spark svg').length, 1, `${name}: one svg, no leftovers`);
    assert.equal(root.classList.contains('hidden'), false, `${name}: the card still shows`);
    c.destroy();
  }
});

test('a Charts.spark that returns its node instead of appending it is placed in the box', async () => {
  const w = wWorld();
  w.run('globalThis.Charts = { spark(host, t, v, o) { return svg("svg", { class: "spark", "aria-label": "mine" }, svg("polyline", { class: "chart-spark", points: "0,0 1,1" })); } };');
  const { root, card: c } = card(w);
  await c.refresh();
  assert.equal(root.querySelectorAll('polyline.chart-spark').length, 1);
  assert.equal(root.querySelector('.uc-spark svg').getAttribute('aria-label'), 'mine', 'an aria label the chart set is kept');
  c.destroy();
});

test('Widgets.sparkDesc says now and peak, or that there are no samples', () => {
  const w = wWorld();
  assert.equal(w.run('Widgets.sparkDesc([10, null, 100, 42.4])'), '5-hour window, last 24 hours: now 42%, peak 100%');
  assert.equal(w.run('Widgets.sparkDesc([null, null])'), '5-hour window, last 24 hours: no samples');
  assert.equal(w.run('Widgets.sparkDesc([])'), '5-hour window, last 24 hours: no samples');
});

test('against the real charts.js: Charts.spark draws the same two runs, the 240 x 44 box and the same label as the card\'s own sparkline', async () => {
  const own = wWorld();
  const a = card(own);
  await a.card.refresh();
  const mine = a.root.querySelector('.uc-spark svg');
  const w = wWorld({}, ['charts.js']);                                       // charts.js loads before widgets.js here; either order works, Charts is read at paint time
  assert.equal(w.run('typeof Charts.spark'), 'function');
  const { root, card: c } = card(w);
  await c.refresh();
  const real = root.querySelector('.uc-spark svg');
  assert.equal(root.querySelectorAll('.uc-spark svg').length, 1);
  assert.equal(root.querySelectorAll('polyline.sp-line').length, 2, 'the null gap is a break: two runs');
  assert.equal(real.getAttribute('viewBox'), mine.getAttribute('viewBox'));
  assert.equal(real.getAttribute('aria-label'), mine.getAttribute('aria-label'), 'one description, whichever drew it');
  assert.equal(real.getAttribute('role'), 'img');
  assert.equal(root.querySelectorAll('line.sp-guide').length, 1, 'the 85 % guide');
  for (const n of [real]) assert.equal(n.getAttribute('style'), null);
  c.destroy();
  a.card.destroy();
});

test('charts.js loaded AFTER widgets.js works the same: the card finds Charts when it paints', async () => {
  const w = wWorld();
  w.load('charts.js');
  const { root, card: c } = card(w);
  await c.refresh();
  assert.equal(root.querySelectorAll('polyline.sp-line').length, 2);
  assert.equal(root.querySelectorAll('.uc-spark svg').length, 1);
  c.destroy();
});

// ---------------------------------------------------------------- v0.5.17b: the accounts line

const TOTAL = (over = {}) => ({ today: { total: 76.3, tokens: 170595051, hours: 7.85, sessions: 5 }, '7d': { total: 296.2, tokens: 640692274, hours: 37.3, sessions: 15 },
  '30d': { total: 1726.8, tokens: 3707620720, hours: 80.32, sessions: 41 }, accounts: 2, headroom_5h: [], headroom_7d: [], ...over });
const withTotal = (over) => ({ ...SUMMARY, total: TOTAL(over) });
const acctLine = (root) => root.querySelector('.uc-acct');

test('Widgets.tokens: 812, 1.2k, 48.7M, 1.2B; no trailing .0, never 1000k, never NaN', () => {
  const w = wWorld();
  const f = (v) => w.run(`Widgets.tokens(${JSON.stringify(v)})`);
  assert.deepEqual([0, 7, 812, 999.6].map(f), ['0', '7', '812', '1k']);
  assert.deepEqual([1000, 1234, 48700000, 170595051, 999950, 1.2e9].map(f), ['1k', '1.2k', '48.7M', '170.6M', '1M', '1.2B']);
  for (const bad of [null, undefined, 'x', -5, NaN, Infinity]) assert.equal(f(bad), '0', String(bad));
});

test('with more than one account the card gets one dim line: "N accounts · <tokens today> tokens today"; it is plain text, not another link', async () => {
  const w = wWorld();
  const { root, card: c } = card(w, { answers: { '/api/series': SERIES, '/api/usage/summary': withTotal() } });
  assert.ok(acctLine(root), 'built with the card');
  assert.equal(acctLine(root).classList.contains('hidden'), true, 'hidden until the summary came back');
  await c.refresh();
  const line = acctLine(root);
  assert.equal(line.classList.contains('hidden'), false);
  assert.equal(text(line), '2 accounts · 170.6M tokens today');
  assert.ok(line.classList.contains('dim'));
  assert.equal(line.tagName, 'DIV');
  assert.equal(root.querySelectorAll('a[href="#/usage"]').length, 3, 'still one link in the header and one per gauge');
  // it sits under the gauges and the burn line, above the charts
  const order = root.children.map((n) => (n.className.match(/uc-(head|gauges|burn|acct|charts)/) || [])[1]);
  assert.deepEqual(order, ['head', 'gauges', 'burn', 'acct', 'charts']);
  c.destroy();
});

test('one account, no `total` (an older box), or nothing sensible: no line, no NaN, and the rest of the card is unchanged', async () => {
  for (const total of [TOTAL({ accounts: 1 }), TOTAL({ accounts: 0 }), TOTAL({ accounts: null }), TOTAL({ accounts: 'x' })]) {
    const w = wWorld();
    const { root, card: c } = card(w, { answers: { '/api/series': SERIES, '/api/usage/summary': { ...SUMMARY, total } } });
    await c.refresh();
    assert.equal(acctLine(root).classList.contains('hidden'), true, JSON.stringify(total.accounts));
    assert.equal(root.classList.contains('hidden'), false, 'the card itself shows');
    c.destroy();
  }
  const w = wWorld();
  const { root, card: c } = card(w);                                                  // the existing SUMMARY has no `total` at all
  await c.refresh();
  assert.equal(acctLine(root).classList.contains('hidden'), true);
  assert.equal(text(acctLine(root)), '');
  assert.ok(!/NaN|undefined/.test(root.textContent));
  c.destroy();
});

test('a total without today still says how many accounts there are (0 tokens), and the line follows the 60 s refresh both ways', async () => {
  const w = wWorld();
  let n = 2;
  const { root, card: c } = card(w, { answers: { '/api/series': SERIES, '/api/usage/summary': () => withTotal({ accounts: n, today: undefined }) } });
  await c.refresh();
  assert.equal(text(acctLine(root)), '2 accounts · 0 tokens today');
  n = 3;
  await c.refresh();
  assert.equal(text(acctLine(root)), '3 accounts · 0 tokens today');
  n = 1;                                                                              // the second subscription went away
  await c.refresh();
  assert.equal(acctLine(root).classList.contains('hidden'), true);
  c.destroy();
});
