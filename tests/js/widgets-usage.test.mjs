// Contract tests for app/static/pages/widgets.js (v0.5.5): the usage card (gauges from the state, a sparkline and cost bars from two fetches that
// never ride the state poll, hidden until data came back, errors swallowed, destroy) and the rate-limit callout in #banner (idempotent, comes back
// after renderBanner() emptied the banner, dismissed per episode, gone when the window resets). Real core.js, components.js, router.js and widgets.js
// on minidom's DOM inside the vm harness.
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
function wWorld(extra = {}) {
  const w = makeWorld(extra);
  installDom(w);
  for (const f of ['core.js', 'components.js', 'router.js', 'pages/widgets.js']) w.load(f);
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
