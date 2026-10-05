// Contract tests for the v0.5.12 Codex half of app/static/pages/usage.js: the Claude | Codex switch of the Limits section (shown only with a Codex on the box), the Codex tab's
// gauges (state.usage_codex told apart by window_minutes) and chart (/api/series key=codex or key=cacct:<key>), the series filter that keeps a Codex line out of the Claude chart
// (and the other way round: the demo's series file holds both), and the Codex block of the Accounts section (one row per Codex account, fed by ONE series request).
// Real core.js, components.js, keymap.js, router.js, pages/agents.js, pages/settings.js and usage.js on minidom's DOM inside the vm harness; window.Charts is a recording fake.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const NOW = Math.floor(Date.now() / 1000);
const ISO = (secAgo) => new Date((NOW - secAgo) * 1000).toISOString();
const tick = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');
const K1 = '3b9f1c2a7d4e5f6a8b0c1d2e', K2 = '7c8d9e0f1a2b3c4d5e6f7a8b', K3 = '1a2b3c4d5e6f7a8b9c0d1e2f';
const AK1 = '7d3e1c52-9a41-4b6f-8a0e-5c2f19d8a001';

// ---------------------------------------------------------------- fixtures

const win = (pct, minutes, resets = NOW + 4 * 86400) => ({ used_percent: pct, window_minutes: minutes, resets_at: resets });
const cxUsage = (value = {}) => ({ value: { limit_id: 'codex', plan_type: 'plus', primary: win(17, 10080), secondary: null, credits: null, reached: false, observed_at: ISO(60), account: K1, ...value }, at: ISO(60) });
const cxAccounts = (list = [{ key: K1, label: 'Codex main', plan: 'plus', current: true }, { key: K2, label: 'Codex work', plan: 'pro', current: false }]) =>
  ({ current: (list.find((a) => a.current) || {}).key || null, list: list.map((a) => ({ account_id: null, saved: true, added_at: ISO(86400), last_seen: ISO(60), ...a })), store: { supported: true, add: true, reason: null, count: list.length }, login: { running: false } });
const STATE = (extra = {}) => ({
  usage: { value: { five_hour: { used_percentage: 77, resets_at: NOW + 2700 }, seven_day: { used_percentage: 59, resets_at: NOW + 3 * 86400 } }, at: ISO(1) },
  usage_codex: cxUsage(), codex_accounts: cxAccounts(), agents: { claude: { installed: true }, codex: { installed: true } }, projects: [], ...extra,
});
const NO_CODEX = () => ({ usage: STATE().usage, projects: [] });

const day = (i, total) => ({ day: `2026-09-${String(20 + i).padStart(2, '0')}`, total, zero: false, by_agent: { claude: total - 1, codex: 1 }, by_project: { ccboard: total }, tokens: Math.round(total * 1e5), hours: 1.5 });
const DAILY7 = Array.from({ length: 7 }, (_, i) => day(i, 10 + i));
const WINDOW = (total) => ({ total, by_agent: { claude: { total: total - 10.5, tokens: 40000000 }, codex: { total: 10.5, tokens: 12712580 } }, by_project: [{ project: 'ccboard', total, hours: 3 }] });
const STAT = (total, tokens) => ({ total, tokens, hours: 2, sessions: 3 });
const CLAUDE_ACCT = { key: AK1, email: 'demo@example.com', name: 'Demo', label: 'Demo', plan: 'max', current: true, rl_5h: { value: 42, resets_at: NOW + 7200, at: ISO(120) }, rl_7d: { value: 71, resets_at: NOW + 3 * 86400, at: ISO(120) },
  windows: { today: STAT(10, 1e6), '7d': STAT(60, 5e6), '30d': STAT(200, 2e7) }, episodes: 0, last_seen: ISO(120) };
const SUMMARY = (over = {}) => ({
  generated_at: ISO(0), tz_min: 345, source: 'samples', windows: { today: WINDOW(10), '7d': WINDOW(77), '30d': WINDOW(300) }, daily: DAILY7,
  hourly_profile: Array.from({ length: 24 }, () => 0), heatmap: Array.from({ length: 7 }, () => Array.from({ length: 24 }, () => 0)), top_sessions: [], active_hours: {},
  rate_limits: { claude: { rl_5h: { at: ISO(120), value: 42, meta: { resets_at: NOW + 7200 } }, rl_7d: { at: ISO(120), value: 71, meta: { resets_at: NOW + 3 * 86400 } } } },
  episodes: [{ kind: '5h', at: ISO(30 * 3600), resets_at: NOW - 25 * 3600, session: 's1', acct: AK1 }], unpriced: [], accounts: [CLAUDE_ACCT],
  total: { today: STAT(10, 1e6), '7d': STAT(60, 5e6), '30d': STAT(200, 2e7), accounts: 1, headroom_5h: [], headroom_7d: [] }, ...over,
});
const T = Array.from({ length: 12 }, (_, i) => NOW - 7 * 86400 + i * 50000);
const body = (series, meta = {}) => ({ since: ISO(7 * 86400), until: ISO(0), step: 50000, t: T, series, meta });
const RISE = (from, to) => Array.from({ length: 12 }, (_, i) => Math.round((from + ((to - from) * i) / 11) * 100) / 100);
const CLAUDE_SERIES = body({ 'rl_5h:claude': RISE(10, 77), 'rl_7d:claude': RISE(5, 59), 'rl_7d:codex': RISE(9, 17) }, { 'rl_5h:claude': { resets_at: NOW + 2700 }, 'rl_7d:codex': { resets_at: NOW + 4 * 86400 } });
const CODEX_SERIES = body({ 'rl_7d:codex': RISE(9, 17), ['rl_7d:cacct:' + K2]: RISE(40, 52), 'rl_5h:claude': RISE(10, 77) }, { 'rl_7d:codex': { resets_at: NOW - 2 * 86400 } });   // a reset inside the range: a tick
const K2_SERIES = body({ ['rl_7d:cacct:' + K2]: RISE(40, 52), 'rl_7d:codex': RISE(9, 17) }, { ['rl_7d:cacct:' + K2]: { resets_at: NOW + 2 * 86400 } });
// the accounts' own readings: K1 weekly only, K2 both windows
const ACC_SERIES = body({ ['rl_7d:cacct:' + K1]: RISE(9, 16), ['rl_7d:cacct:' + K2]: [...Array(8).fill(null), 49, 50, 51, 52], ['rl_5h:cacct:' + K2]: [...Array(8).fill(null), 20, 25, 30, 30] },
  { ['rl_7d:cacct:' + K1]: { resets_at: NOW + 4 * 86400 }, ['rl_7d:cacct:' + K2]: { resets_at: NOW + 2 * 86400 }, ['rl_5h:cacct:' + K2]: { resets_at: NOW + 3600 } });
const EVENTS = { events: [{ t: NOW - 3000, key: 'ccboard--ccboard--s1', v: 1, m: { p: 'ccboard', r: 'ccboard', s: 's1', a: 'claude' } }], truncated: false };

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

/** The path -> answer router of the page's GETs (an answer may be overridden through over[name]). */
function route(over = {}) {
  return (path) => {
    if (path.startsWith('/api/usage/summary')) return over.summary || SUMMARY();
    if (path.startsWith('/api/series/events')) return EVENTS;
    if (path.startsWith('/api/series?series=rl_')) {
      if (/key=cacct:[^&]*,cacct:|&since=30d/.test(path) && /since=30d/.test(path)) return over.acc || ACC_SERIES;
      if (/key=cacct:/.test(path)) return over.k2 || K2_SERIES;
      if (/key=codex/.test(path)) return over.codex || CODEX_SERIES;
      return over.claude || CLAUDE_SERIES;
    }
    return {};
  };
}

function usageWorld({ st = STATE(), over = {}, answer = null } = {}) {
  const w = makeWorld({ matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }), setInterval: () => 1, clearInterval: () => {} });
  installDom(w);
  for (const f of ['core.js', 'components.js', 'keymap.js', 'router.js']) w.load(f);
  w.run('globalThis.__polls = 0; poll = async () => { __polls++; };');
  w.ctx.__calls = [];
  w.ctx.__route = answer || route(over);
  w.run(`api = async (method, path, body) => { __calls.push({ method, path, body }); const v = __route(path); if (v && v.__error) throw new Error(v.__error); return v; };`);
  w.run(CHARTS_STUB);
  w.load('pages/agents.js');
  w.load('pages/settings.js');
  w.load('pages/usage.js');
  if (st) { w.ctx.__st = st; w.run('state = __st'); }
  return { w };
}

const root = (w) => w.document.querySelector('#page');
const q = (w, sel) => root(w).querySelector(sel);
const qa = (w, sel) => root(w).querySelectorAll(sel);
const calls = (w) => plain(w.get('__calls'));
const paths = (w, prefix) => calls(w).map((c) => c.path).filter((p) => p.startsWith(prefix));
const lines = (w) => w.get('__charts.calls').filter((c) => c.fn === 'line');
const loading = async (w) => { await w.get('Usage.cur').loading; await tick(); };
const go = async (w, hash = '#/usage') => { w.location.hash = hash; await loading(w); };
const seg = (w) => q(w, '[data-sec="limits"] [data-seg="agent"]');
const segBtn = (w, a) => seg(w).querySelector(`button[data-agent="${a}"]`);
const pressed = (w) => ['claude', 'codex'].filter((a) => segBtn(w, a).getAttribute('aria-pressed') === 'true');
const gauge = (w, label) => q(w, `.uc-gauges .gauge[data-gauge="${label}"]`);
const shown = (n) => !!n && !n.classList.contains('hidden');
const val = (g) => text(g.querySelector('.g-val'));
const lineNames = (w) => plain(lines(w).filter((c) => c.opts.names.some((n) => n.startsWith('rl_'))).pop().opts.names);
const pick = async (w, a) => { segBtn(w, a).click(); await loading(w); };
const clean = (w, what = '') => assert.doesNotMatch(text(root(w)), /undefined|NaN|\[object|null/, `${what}: no stray undefined/NaN/null in the text`);

// ---------------------------------------------------------------- the switch

test('no Codex on the box: no switch, no Codex request, and a Codex line in the series answer stays out of the Claude chart', async () => {
  const { w } = usageWorld({ st: NO_CODEX() });
  await go(w);
  assert.equal(shown(seg(w)), false, 'the Claude | Codex switch stays hidden');
  assert.equal(paths(w, '/api/series?').length, 1);
  assert.match(paths(w, '/api/series?')[0], /key=claude/);
  assert.deepEqual(lineNames(w), ['rl_5h:claude', 'rl_7d:claude'], 'the answer also held rl_7d:codex: it is not drawn');
  assert.equal(text(q(w, '[data-sec="limits"] .prov')), 'statusline (official)');
  assert.equal(q(w, '[data-block="codex"]'), null);
});

test('a Codex on the box shows the switch with Claude pressed; Codex asks for key=codex, draws the weekly line and puts agent=codex in the address', async () => {
  const { w } = usageWorld();
  await go(w);
  assert.ok(shown(seg(w)));
  assert.deepEqual(plain(Array.from(seg(w).querySelectorAll('button')).map((b) => text(b))), ['Claude', 'Codex']);
  assert.deepEqual(pressed(w), ['claude']);
  assert.match(paths(w, '/api/series?series=rl_5h,rl_7d&key=claude')[0], /since=7d/);
  await pick(w, 'codex');
  assert.deepEqual(pressed(w), ['codex']);
  assert.match(paths(w, '/api/series?').find((p) => /key=codex/.test(p)), /^\/api\/series\?series=rl_5h,rl_7d&key=codex&since=7d&points=\d+$/);
  assert.deepEqual(lineNames(w), ['rl_7d:codex'], 'only the Codex line: the stray rl_5h:claude of the answer is not drawn');
  const o = lines(w).pop().opts;
  assert.deepEqual(plain(o.labels), { 'rl_7d:codex': '7D' });
  assert.deepEqual(plain(o.marks), [], 'limit hits are Claude\'s: a Codex window has only its resets');
  assert.ok(plain(o.resets).length >= 1);
  assert.equal(text(q(w, '[data-sec="limits"] .prov')), 'rollout rate limits (Codex)');
  assert.match(text(qa(w, '[data-sec="limits"] .unote').find((n) => /dashed lines/.test(text(n)))), /ticks mark window resets$/);
  assert.equal(w.localStorage.getItem('ccboard:usage:agent'), 'codex');
  assert.equal(w.location.hash, '#/usage?range=7d&agent=codex');
  assert.equal(w.history.calls.at(-1).method, 'replaceState');
  await pick(w, 'claude');
  assert.deepEqual(pressed(w), ['claude']);
  assert.deepEqual(lineNames(w), ['rl_5h:claude', 'rl_7d:claude'], 'back on Claude: the Codex line is out again');
  assert.equal(w.location.hash, '#/usage?range=7d', 'no agent in the address for Claude');
  assert.equal(w.localStorage.getItem('ccboard:usage:agent'), 'claude');
  clean(w);
});

test('?agent=codex opens on the Codex tab (the CX pill\'s link); the last pick is remembered; a range change keeps agent=codex in the address', async () => {
  const { w } = usageWorld();
  await go(w, '#/usage?agent=codex');
  assert.deepEqual(pressed(w), ['codex']);
  assert.match(paths(w, '/api/series?')[0], /key=codex/);
  assert.equal(w.localStorage.getItem('ccboard:usage:agent'), 'codex');
  q(w, 'button[data-range="30d"]').click();
  await loading(w);
  assert.equal(w.location.hash, '#/usage?range=30d&agent=codex');
  const { w: w2 } = usageWorld();
  w2.localStorage.setItem('ccboard:usage:agent', 'codex');
  await go(w2);
  assert.deepEqual(pressed(w2), ['codex'], 'a stored pick without a query');
  const { w: w3 } = usageWorld();
  w3.localStorage.setItem('ccboard:usage:agent', 'codex');
  await go(w3, '#/usage?agent=claude');
  assert.deepEqual(pressed(w3), ['claude'], 'the query wins');
});

test('a stored Codex pick on a box without Codex falls back to Claude, with no Codex request', async () => {
  const { w } = usageWorld({ st: NO_CODEX() });
  w.localStorage.setItem('ccboard:usage:agent', 'codex');
  await go(w, '#/usage?agent=codex');
  assert.equal(paths(w, '/api/series?').length, 1);
  assert.match(paths(w, '/api/series?')[0], /key=claude/);
  assert.equal(shown(seg(w)), false);
  assert.ok(shown(gauge(w, '5H')), 'the Claude gauges');
});

test('the state arriving after the page: a stored Codex pick waits for the box to say it has one, then fetches the Codex series', async () => {
  const { w } = usageWorld({ st: null });
  w.localStorage.setItem('ccboard:usage:agent', 'codex');
  await go(w);
  assert.match(paths(w, '/api/series?')[0], /key=claude/, 'no state yet: Claude');
  w.get('pages').usage.update(STATE());
  await loading(w);
  assert.ok(shown(seg(w)));
  assert.deepEqual(pressed(w), ['codex']);
  assert.ok(paths(w, '/api/series?').some((p) => /key=codex/.test(p)));
  assert.deepEqual(lineNames(w), ['rl_7d:codex']);
});

// ---------------------------------------------------------------- the gauges

test('the Codex tab: a weekly-only plan shows ONE gauge (7D), the plan, the age of the reading and why there is no 5H', async () => {
  const { w } = usageWorld();
  await go(w);
  await pick(w, 'codex');
  assert.equal(shown(gauge(w, '5H')), false);
  assert.ok(shown(gauge(w, '7D')));
  assert.equal(val(gauge(w, '7D')), '17%');
  assert.ok(gauge(w, '7D').classList.contains('ok'));
  assert.match(text(gauge(w, '7D').querySelector('.g-reset')), /^resets in (4d|3d23h)/);
  assert.match(text(q(w, '[data-sec="limits"] .unote')), /^plus plan · last reading 1m ago · the weekly window is the only one this plan reports/);
});

test('the slot comes from window_minutes, not from primary / secondary: 300 is the 5H gauge, 10080 the 7D one, in either order; a window in between has none', async () => {
  for (const [primary, secondary] of [[win(80, 300, NOW + 3600), win(23, 10080)], [win(23, 10080), win(80, 300, NOW + 3600)]]) {
    const { w } = usageWorld({ st: STATE({ usage_codex: cxUsage({ primary, secondary }) }) });
    await go(w, '#/usage?agent=codex');
    assert.equal(val(gauge(w, '5H')), '80%');
    assert.ok(gauge(w, '5H').classList.contains('warn'));
    assert.equal(val(gauge(w, '7D')), '23%');
    assert.doesNotMatch(text(q(w, '[data-sec="limits"] .unote')), /only one this plan reports/, 'both windows: nothing to explain');
  }
  const { w } = usageWorld({ st: STATE({ usage_codex: cxUsage({ primary: win(50, 1440), secondary: null }), codex_accounts: undefined }) });
  await go(w, '#/usage?agent=codex');
  assert.equal(shown(gauge(w, '5H')) || shown(gauge(w, '7D')), false, 'a one-day window fits neither slot');
  assert.match(text(q(w, '[data-sec="limits"] .unote')), /No Codex reading yet/);
});

test('a window that rolled over reads 0 % and says so; a reached limit is named in the note; no reading names the next step', async () => {
  const { w } = usageWorld({ st: STATE({ usage_codex: cxUsage({ primary: win(64, 10080, NOW - 3600), reached: true }) }) });
  await go(w, '#/usage?agent=codex');
  assert.equal(val(gauge(w, '7D')), '0%');
  assert.equal(text(gauge(w, '7D').querySelector('.g-reset')), 'window rolled over');
  assert.match(text(q(w, '[data-sec="limits"] .unote')), /limit reached/);
  const { w: w2 } = usageWorld({ st: STATE({ usage_codex: null, codex_accounts: undefined }) });
  await go(w2, '#/usage?agent=codex');
  assert.ok(shown(seg(w2)), 'an installed Codex is enough for the tab');
  assert.equal(shown(gauge(w2, '7D')), false);
  assert.match(text(q(w2, '[data-sec="limits"] .unote')), /No Codex reading yet.*\+ session/);
  clean(w2);
});

test('an empty Codex series is an empty state with its next step, never a blank chart box', async () => {
  const { w } = usageWorld({ over: { codex: body({ 'rl_7d:codex': Array(12).fill(null) }) } });
  await go(w, '#/usage?agent=codex');
  assert.equal(lines(w).length, 0);
  assert.ok(q(w, '.lim-chart').classList.contains('hidden'));
  assert.match(text(q(w, '[data-sec="limits"] .uslot')), /No Codex samples yet.*rollout/);
  clean(w);
});

// ---------------------------------------------------------------- the Codex account chips

test('with two Codex accounts the Codex tab gets one chip each; a chip other than the one in use asks for key=cacct:<key>, reads that account\'s own series and gauges', async () => {
  const { w } = usageWorld();
  await go(w, '#/usage?agent=codex');
  const chips = () => qa(w, '.ua-picks button[data-account]');
  assert.deepEqual(chips().map((b) => b.getAttribute('data-account')), [K1, K2]);
  assert.deepEqual(chips().map((b) => b.getAttribute('aria-pressed')), ['true', 'false'], 'the account in use is pressed');
  assert.ok(chips()[0].querySelector('.hue-teal'), 'a Codex account\'s dot is teal');
  chips()[1].click();
  await loading(w);
  assert.ok(paths(w, '/api/series?').some((p) => /^\/api\/series\?series=rl_5h,rl_7d&key=cacct:7c8d9e0f1a2b3c4d5e6f7a8b&since=7d/.test(p)), paths(w, '/api/series?').join('\n'));
  assert.deepEqual(lineNames(w), ['rl_7d:cacct:' + K2], 'that account only (the answer also held the account in use\'s own line)');
  assert.equal(val(gauge(w, '7D')), '52%', 'its last point; the account in use\'s live 17% is not its');
  assert.equal(shown(gauge(w, '5H')), true, 'its 5-hour window from the accounts\' series');
  assert.equal(val(gauge(w, '5H')), '30%');
  assert.match(text(q(w, '[data-sec="limits"] .unote')), /pro plan.*not the account in use/);
  qa(w, '.ua-picks button[data-account]')[0].click();
  await loading(w);
  assert.match(paths(w, '/api/series?').at(-1), /key=codex&/, 'the account in use is key=codex again');
  assert.equal(val(gauge(w, '7D')), '17%');
  clean(w);
});

test('one Codex account: no chips; the chips of the Claude tab are the Claude accounts\' and never the Codex ones', async () => {
  const { w } = usageWorld({ st: STATE({ codex_accounts: cxAccounts([{ key: K1, label: 'Codex main', plan: 'plus', current: true }]) }) });
  await go(w, '#/usage?agent=codex');
  assert.ok(q(w, '.ua-picks').classList.contains('hidden'));
  const { w: w2 } = usageWorld();
  await go(w2);
  assert.ok(q(w2, '.ua-picks').classList.contains('hidden'), 'one Claude account, two Codex ones: the Claude tab shows no chip');
});

test('a picked Codex account that is forgotten goes back to the account in use', async () => {
  const { w } = usageWorld();
  await go(w, '#/usage?agent=codex');
  qa(w, '.ua-picks button[data-account]')[1].click();
  await loading(w);
  const before = paths(w, '/api/series?').length;
  w.get('pages').usage.update(STATE({ codex_accounts: cxAccounts([{ key: K1, label: 'Codex main', plan: 'plus', current: true }]) }));
  await loading(w);
  assert.ok(paths(w, '/api/series?').slice(before).some((p) => /key=codex&since=7d/.test(p)), 'the series of the account in use again');
  assert.equal(val(gauge(w, '7D')), '17%');
});

// ---------------------------------------------------------------- the Codex block of the Accounts section

const row = (w, key) => q(w, `[data-block="codex"] .ucx-row[data-codex-account="${key}"]`);
const rowGauge = (w, key, label) => row(w, key).querySelector(`.gauge[data-gauge="${label}"]`);

test('the Codex block: one row per Codex account in the teal of its agent with plan and current, the weekly gauge of the account in use from the live reading, the other\'s from its series', async () => {
  const { w } = usageWorld();
  await go(w);
  const block = q(w, '[data-sec="accounts"] [data-block="codex"]');
  assert.ok(block, 'under the Claude accounts');
  assert.equal(text(block.querySelector('.ucx-h')), 'Codexrollout rate limits');
  assert.deepEqual(qa(w, '.ucx-row').map((r) => r.getAttribute('data-codex-account')), [K1, K2]);
  const chip = (k) => row(w, k).querySelector('.ua-chip');
  assert.equal(text(chip(K1)), 'Codex main');
  assert.ok(chip(K1).classList.contains('hue-teal'));
  assert.equal(text(row(w, K1).querySelector('.ua-plan')), 'plus');
  assert.equal(text(row(w, K2).querySelector('.ua-plan')), 'pro');
  assert.ok(row(w, K1).querySelector('.ua-current') && !row(w, K2).querySelector('.ua-current'));
  assert.notEqual(row(w, K1).getAttribute('data-current'), null);
  assert.equal(row(w, K2).getAttribute('data-current'), null);
  assert.equal(val(rowGauge(w, K1, '7D')), '17%', 'the live reading');
  assert.equal(rowGauge(w, K1, '5H'), null, 'a weekly-only account has no 5H cell content');
  assert.equal(val(rowGauge(w, K2, '7D')), '52%', 'its last series point');
  assert.equal(val(rowGauge(w, K2, '5H')), '30%');
  assert.match(text(rowGauge(w, K2, '7D').querySelector('.g-reset')), /^resets in 1d/);
  const note = text(block.querySelector('[data-note="codex"]'));
  assert.match(note, /^Last 7 days: 12\.7M tokens · \$10\.50 API-equivalent, for the whole box: ccusage counts Codex sessions, not accounts\./);
  assert.ok(block.querySelector('.ua-edit'), 'the rename pencil of the Codex sheet');
  clean(w);
});

test('the Codex accounts\' readings are ONE series request (both windows, key=cacct:<key> for each, 30 days) that the 60 s refresh repeats and the 3 s poll never does', async () => {
  const { w } = usageWorld();
  await go(w);
  const acc = paths(w, '/api/series?').filter((p) => /since=30d/.test(p));
  assert.deepEqual(acc, [`/api/series?series=rl_5h,rl_7d&key=cacct:${K1},cacct:${K2}&since=30d&points=60`]);
  for (let i = 0; i < 5; i++) w.get('pages').usage.update(STATE());
  await tick();
  assert.equal(paths(w, '/api/series?').filter((p) => /since=30d/.test(p)).length, 1, 'the poll never fetches');
  w.get('pages').usage.update(STATE({ codex_accounts: cxAccounts([{ key: K1, label: 'Codex main', plan: 'plus', current: true }, { key: K2, label: 'Codex work', plan: 'pro' }, { key: K3, label: 'Third', plan: 'free' }]) }));
  await loading(w);
  assert.equal(paths(w, '/api/series?').filter((p) => /since=30d/.test(p)).length, 2, 'a new account is a new request');
  assert.match(paths(w, '/api/series?').filter((p) => /since=30d/.test(p)).pop(), new RegExp(`key=cacct:${K1},cacct:${K2},cacct:${K3}&`));
});

test('past 4 Codex accounts the request asks for the weekly window alone (the endpoint takes 8 series x key combinations), at most 8 accounts', async () => {
  const many = Array.from({ length: 10 }, (_, i) => ({ key: `k${String(i).padStart(2, '0')}0f1c2a7d4e5f6a8b0c1d`, label: `Acct ${i}`, plan: 'plus', current: i === 0 }));
  const { w } = usageWorld({ st: STATE({ codex_accounts: cxAccounts(many) }) });
  await go(w);
  const p = paths(w, '/api/series?').find((x) => /since=30d/.test(x));
  assert.match(p, /^\/api\/series\?series=rl_7d&key=/);
  assert.equal(p.split('key=')[1].split('&')[0].split(',').length, 8);
  assert.equal(qa(w, '.ucx-row').length, 10, 'every account still has its row');
  const five = Array.from({ length: 4 }, (_, i) => ({ key: `f${i}9f1c2a7d4e5f6a8b0c1d2e`, label: `F${i}`, current: i === 0 }));
  const { w: w2 } = usageWorld({ st: STATE({ codex_accounts: cxAccounts(five) }) });
  await go(w2);
  assert.match(paths(w2, '/api/series?').find((x) => /since=30d/.test(x)), /^\/api\/series\?series=rl_5h,rl_7d&key=/, '4 accounts still fit both windows');
});

test('no state.codex_accounts, no block and no accounts request; an account without a reading says so', async () => {
  const { w } = usageWorld({ st: STATE({ codex_accounts: undefined }) });
  await go(w);
  assert.equal(q(w, '[data-block="codex"]'), null);
  assert.equal(paths(w, '/api/series?').filter((p) => /since=30d/.test(p)).length, 0);
  const { w: w2 } = usageWorld({ over: { acc: body({}) } });
  await go(w2);
  assert.match(text(row(w2, K2)), /no reading yet/);
  assert.equal(val(rowGauge(w2, K1, '7D')), '17%', 'the account in use still has the live reading');
});

test('a box that runs Codex only (no Claude account in the summary) still shows its Codex accounts under the Claude empty state', async () => {
  const { w } = usageWorld({ over: { summary: SUMMARY({ accounts: [], total: { today: STAT(0, 0), '7d': STAT(0, 0), '30d': STAT(0, 0), accounts: 0, headroom_5h: [], headroom_7d: [] } }) } });
  await go(w);
  assert.match(text(q(w, '[data-sec="accounts"]')), /No account seen yet/);
  assert.equal(qa(w, '.ucx-row').length, 2);
});

test('a Codex state reading that names another account is that account\'s, and the account in use falls back to its series', async () => {
  const { w } = usageWorld({ st: STATE({ usage_codex: cxUsage({ account: K2, primary: win(61, 10080, NOW + 86400) }) }) });
  await go(w);
  assert.equal(val(rowGauge(w, K2, '7D')), '61%', 'the live reading belongs to the account it names');
  assert.equal(val(rowGauge(w, K1, '7D')), '16%', 'the account in use shows its own last series point');
});

test('the block follows a rename and a switch from the 3 s poll without a fetch', async () => {
  const { w } = usageWorld();
  await go(w);
  w.get('pages').usage.update(STATE({ codex_accounts: cxAccounts([{ key: K1, label: 'Renamed', plan: 'plus', current: false }, { key: K2, label: 'Codex work', plan: 'pro', current: true }]) }));
  assert.equal(text(row(w, K1).querySelector('.ua-chip')), 'Renamed');
  assert.ok(row(w, K2).querySelector('.ua-current') && !row(w, K1).querySelector('.ua-current'));
});
