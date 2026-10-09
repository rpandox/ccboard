// Contract tests for app/static/pages/memory.js (v0.5.20): the Memory page (Palace, Timeline, Summaries, Search), the health tile and the gotchas strip.
// Real core.js, components.js, keymap.js, router.js and memory.js on minidom's DOM inside the vm harness; api() answers from __answers (a body, a function,
// or {__error: {status, body, message}}), the demo fixtures under app/static/demo/ are the data, and setTimeout is a recorder so the 300 ms debounce is exact.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const tick = () => new Promise((r) => setImmediate(r));
const settle = async () => { for (let i = 0; i < 6; i += 1) await tick(); };
const text = (n) => (n ? n.textContent : '');
const fx = (name) => JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', `${name}.json`), 'utf8'));
const clone = (v) => JSON.parse(JSON.stringify(v));

const PAL = fx('memory_palace');
const OBS = fx('memory_observations');
const SUMS = fx('memory_summaries');
const SEARCH = fx('memory_search');
const HEALTH = fx('memory_health');
const STATES = fx('memory_states');
const err = (variant) => ({ __error: { status: STATES[variant].status, body: STATES[variant].body, message: STATES[variant].body.error } });

const STATE = (extra = {}) => ({
  projects: [
    { name: 'shop', path: '/p/shop', root: null, repos: [
      { name: 'api', path: '/p/shop/api', state: 'ok', sessions: [{ tmux: 'shop--api--s1', name: 's1', state: 'idle', agent: 'claude' }, { tmux: 'shop--api--s2', name: 's2', state: 'working', agent: 'claude' },
        { tmux: 'shop--api--sh', name: 'sh', state: 'idle', launcher: 'shell', command: 'bash' }] },
      { name: 'website', path: '/p/shop/website', state: 'ok', sessions: [] }] },
    { name: 'blog', path: '/p/blog', root: null, repos: [{ name: 'web', path: '/p/blog/web', state: 'ok', sessions: [] }] },
  ],
  memory: { ...HEALTH }, config: { code_https_port: 10000, mem_viewer_url: null }, ...extra,
});

function memWorld({ st = STATE(), answers = {}, coarse = false } = {}) {
  const timers = [];
  const w = makeWorld({
    setTimeout: (fn, ms) => { timers.push({ fn, ms, cancelled: false, ran: false }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].cancelled = true; },
  });
  installDom(w);
  if (coarse) w.document.documentElement.classList.add('force-coarse');
  for (const f of ['core.js', 'components.js', 'keymap.js', 'router.js']) w.load(f);
  w.ctx.__calls = [];
  w.ctx.__answers = answers;
  w.run(`api = async (method, path, body) => {
    __calls.push({ method, path, body });
    for (const [prefix, v] of Object.entries(__answers)) {
      if (!path.startsWith(prefix)) continue;
      if (typeof v === 'function') return v(path, method, body);
      if (v && v.__error) throw Object.assign(new Error(v.__error.message || 'failed'), { status: v.__error.status, body: v.__error.body });
      return v;
    }
    return {};
  };`);
  w.run('globalThis.__toasts = []; toast = (t, o) => { __toasts.push({ text: t, kind: o && o.kind }); };');
  w.load('pages/memory.js');
  w.ctx.__st = st;
  w.run('state = __st');
  return { w, timers, answers };
}

const page = (w) => w.document.querySelector('#page');
const q = (w, sel) => page(w).querySelector(sel);
const qa = (w, sel) => page(w).querySelectorAll(sel);
const calls = (w) => plain(w.get('__calls'));
const getCalls = (w, prefix) => calls(w).filter((c) => c.method === 'GET' && c.path.startsWith(prefix));
const go = async (m, hash = '#/memory/shop') => { m.w.location.hash = hash; await settle(); };
const runTimers = async (m, ms) => { for (const t of m.timers) if (!t.ran && !t.cancelled && (ms === undefined || t.ms === ms)) { t.ran = true; t.fn(); } await settle(); };
const call = (w, expr, arg) => { w.ctx.__a = arg; return plain(w.run(expr)); };
const withAnswers = (over = {}) => ({
  '/api/memory/health': HEALTH,
  '/api/memory/shop/palace': PAL, '/api/memory/shop/observations': OBS, '/api/memory/shop/summaries': SUMS, '/api/memory/shop/search': SEARCH, ...over,
});
const tabIds = (w) => qa(w, '.tab').map((n) => n.getAttribute('data-tab'));
const pickTab = async (m, id) => { qa(m.w, '.tab').find((n) => n.getAttribute('data-tab') === id).click(); await settle(); };

// ---------------------------------------------------------------- pure helpers

test('filters: junk is ignored, the address wins key by key, a stored value fills the rest', () => {
  const { w } = memWorld();
  assert.deepEqual(call(w, 'memFilterClean(__a)', { type: 'bugfix,decision', repo: 'api', agent: 'claude', session: 'abcd1234-ef', range: '7d' }),
    { type: 'bugfix,decision', repo: 'api', agent: 'claude', session: 'abcd1234-ef', range: '7d' });
  assert.deepEqual(call(w, 'memFilterClean(__a)', { type: '<script>', repo: '../../x', agent: 'root', session: 'a b', range: 'forever' }), { type: '', repo: '', agent: '', session: '', range: '' });
  assert.deepEqual(call(w, 'memFilterClean(__a)', 'junk'), { type: '', repo: '', agent: '', session: '', range: '' });
  assert.deepEqual(call(w, 'memFilterClean(__a)', { agent: ['claude'], repo: 5 }), { type: '', repo: '', agent: '', session: '', range: '' });
  w.ctx.__q = { agent: 'codex', range: 'bogus', type: 'bugfix' };
  w.ctx.__s = { agent: 'claude', range: '30d', repo: 'api' };
  assert.deepEqual(plain(w.run('memFiltersFrom(__q, __s)')), { type: 'bugfix', repo: 'api', agent: 'codex', session: '', range: '30d' });
});

test('filter query: only set filters reach the proxy, the range becomes since in epoch ms, the hash carries tab, q and every filter', () => {
  const { w } = memWorld();
  w.ctx.__f = { type: 'bugfix', repo: 'api', agent: 'claude', session: 'abcd1234', range: '24h' };
  assert.deepEqual(plain(w.run('memFilterParams(__f, 1000000000)')), { type: 'bugfix', repo: 'api', agent: 'claude', session: 'abcd1234', since: String(1000000000 - 86400000) });
  assert.deepEqual(plain(w.run("memFilterParams({ type: '', repo: '', agent: '', session: '', range: 'all' }, 5)")), {});
  assert.deepEqual(plain(w.run("memHashQuery('timeline', 'sticky', __f)")), { tab: 'timeline', q: 'sticky', type: 'bugfix', repo: 'api', agent: 'claude', session: 'abcd1234', range: '24h' });
  assert.deepEqual(plain(w.run("memHashQuery('palace', '', { type: '', repo: '', agent: '', session: '', range: '' })")), {});
  assert.equal(w.run("memUrl('my proj', 'search', { q: 'a&b', agent: '' })"), '/api/memory/my%20proj/search?q=a%26b');
});

test('the route: the project of the address, else the last one opened, else the first; ?q= opens Search; a bad tab is Palace', () => {
  const { w } = memWorld();
  const st = STATE();
  w.ctx.__st2 = st;
  assert.equal(plain(w.run("memParseRoute({ params: {}, query: {} }, __st2)")).project, 'shop');
  w.localStorage.setItem('ccboard:memory-last', 'blog');
  assert.equal(plain(w.run("memParseRoute({ params: {}, query: {} }, __st2)")).project, 'blog');
  w.localStorage.setItem('ccboard:memory-last', 'gone');
  assert.equal(plain(w.run("memParseRoute({ params: {}, query: {} }, __st2)")).project, 'shop', 'a project that no longer exists is not remembered');
  const r = plain(w.run("memParseRoute({ params: { project: 'shop' }, query: { q: 'sticky' } }, __st2)"));
  assert.deepEqual([r.tab, r.q], ['search', 'sticky']);
  assert.equal(plain(w.run("memParseRoute({ params: { project: 'shop' }, query: { tab: 'nope' } }, __st2)")).tab, 'palace');
  assert.equal(plain(w.run("memParseRoute({ params: { project: 'shop' }, query: { tab: 'summaries', q: 'x' } }, __st2)")).tab, 'summaries');
});

test('the persisted filters are read back per project, and junk in storage is ignored', () => {
  const { w } = memWorld();
  w.localStorage.setItem('ccboard:memory:shop', JSON.stringify({ type: 'bugfix', agent: 'codex', range: '7d', repo: '../x' }));
  assert.deepEqual(plain(w.run("memStoredFilters('shop')")), { type: 'bugfix', repo: '', agent: 'codex', session: '', range: '7d' });
  w.localStorage.setItem('ccboard:memory:shop', '{not json');
  assert.deepEqual(plain(w.run("memStoredFilters('shop')")), { type: '', repo: '', agent: '', session: '', range: '' });
  assert.deepEqual(plain(w.run("memStoredFilters('blog')")), { type: '', repo: '', agent: '', session: '', range: '' }, 'another project has its own');
});

test('storage that throws never breaks the helpers', () => {
  const { w } = memWorld();
  w.run(`Object.defineProperty(globalThis, 'localStorage', { get() { throw new Error('blocked'); }, configurable: true });`);
  assert.deepEqual(plain(w.run("memStoredFilters('shop')")), { type: '', repo: '', agent: '', session: '', range: '' });
  assert.doesNotThrow(() => w.run("memSaveFilters('shop', { type: 'bugfix' })"));
});

test('the worker pill: version and count only from the health record, an unknown count reads unknown, stale and every failing state say why', () => {
  const { w } = memWorld();
  const pill = (h, env) => { w.ctx.__h = h; w.ctx.__e = env; return plain(w.run('memPillInfo(__h, __e)')); };
  assert.equal(pill({ state: 'up', version: '13.31.0', observations: 11836 }).text, 'up v13.31.0 · 11,836 obs');
  assert.equal(pill({ state: 'up', version: '13.31.0', observations: null }).text, 'up v13.31.0 · unknown obs');
  assert.match(pill({ state: 'degraded', reason: 'the worker is slow: no answer within 2.0 s' }).text, /^degraded · the worker is slow/);
  assert.match(pill({ state: 'down', reason: 'connection refused' }).text, /^down · connection refused/);
  assert.equal(pill({ state: 'up', version: '1' }, { stale: true, stale_at: '2026-10-03T08:52:10+00:00' }).cls, 'stale');
  assert.match(pill({ state: 'up', version: '1' }, { stale: true, stale_at: '2026-10-03T08:52:10+00:00' }).text, /^stale \d\d:\d\d$/);
  assert.equal(pill(null).text, 'checking');
  assert.equal(pill({ state: 'off' }).text, 'off');
});

test('env notes: each state has its own glyph and words', () => {
  const { w } = memWorld();
  const notes = (env) => { w.ctx.__e = env; return plain(w.run('memEnvNotes(__e)')); };
  assert.deepEqual(notes(PAL), [], 'a clean answer says nothing');
  const stale = notes({ stale: true, stale_at: '2026-10-03T08:52:10+00:00', reason: 'showing the last good answer: the worker is slow' });
  assert.equal(stale[0].id, 'stale');
  assert.match(stale[0].text, /^Showing data from \d\d:\d\d\. showing the last good answer/);
  assert.equal(stale[0].retry, true);
  assert.match(notes({ partial: ['website/sticky-summary', 'api'] })[0].text, /did not answer: website\/sticky-summary, api/);
  assert.match(notes({ ambiguous: [{ key: 'website', shared_with: ['x/website'] }] })[0].text, /Another project shares the folder name website.*best effort/);
  assert.match(notes(STATES.incompatible.body)[0].text, /may be wrong until the board is updated; report it/);
  assert.match(notes({ compat: 'untested', worker_version: '13.34.2', tested_worker: '13.31.0' })[0].text, /13\.34\.2 is newer than the one this board was tested with \(13\.31\.0\)/);
  for (const n of [...notes({ stale: true, partial: ['a'], compat: 'untested' })]) assert.ok(n.glyph && n.text && !/undefined|null|NaN/.test(n.text));
});

// ---------------------------------------------------------------- the page

test('the header: project select, worker pill from state.memory, the search box, four tabs with Palace selected, no filled primary', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m);
  assert.deepEqual(qa(m.w, '.mem-project option').map((o) => text(o)), ['shop', 'blog']);
  assert.equal(text(q(m.w, '.mem-pill-t')), 'up v13.31.0 · 11,836 obs');
  assert.ok(q(m.w, '.mem-search'));
  assert.deepEqual(tabIds(m.w), ['palace', 'timeline', 'summaries', 'search']);
  assert.equal(q(m.w, '.tab[aria-selected=true]').getAttribute('data-tab'), 'palace');
  assert.equal(qa(m.w, 'button.primary, a.primary').length, 0, 'a healthy page has no filled primary at all');
  assert.equal(m.w.document.title, 'Memory shop · ccboard');
  assert.doesNotMatch(text(page(m.w)), /undefined|\[object|NaN/);
});

test('Palace: a card per wing with room chips and counts, the stacked type bar with its counts as text, drawers collapsed until tapped', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m);
  const wings = qa(m.w, '.mem-wing');
  assert.equal(wings.length, PAL.wings.length);
  const first = wings[0];
  assert.equal(text(first.querySelector('.mem-wing-name')), PAL.wings[0].name);
  assert.equal(first.querySelectorAll('.mem-room').length, PAL.wings[0].rooms.length);
  const room = first.querySelector('.mem-room');
  assert.match(text(room), new RegExp(`${PAL.wings[0].rooms[0].name}\\s*${PAL.wings[0].rooms[0].count}`));
  const bar = first.querySelector('.mem-bar');
  assert.match(bar.getAttribute('aria-label'), /^Observations by type: /);
  assert.equal(bar.querySelectorAll('.mem-seg').length, Object.keys(PAL.wings[0].types).length);
  assert.ok(bar.querySelectorAll('.mem-seg')[0].classList.contains('mem-w' + 20) || /mem-w\d+|mem-seg-rest/.test(bar.querySelectorAll('.mem-seg')[0].className));
  assert.ok(bar.querySelectorAll('.mem-seg').pop().classList.contains('mem-seg-rest'), 'the last segment fills what the ladder leaves');
  for (const [t, n] of Object.entries(PAL.wings[0].types)) assert.match(text(first.querySelector('.mem-legend')), new RegExp(`${t} ${n}`), 'the counts are also text');
  const body = first.querySelector('.mem-item .mem-body');
  assert.ok(body.classList.contains('hidden'));
  first.querySelector('.mem-item-head').click();
  assert.equal(first.querySelector('.mem-item-head').getAttribute('aria-expanded'), 'true');
  assert.equal(first.querySelector('.mem-item .mem-body').classList.contains('hidden'), false);
  first.querySelector('.mem-room').click();
  assert.equal(first.querySelector('.mem-room').getAttribute('aria-pressed'), 'true', 'a room chip filters the drawers');
});

test('a narrative that contains markup is shown as text, with pre-wrap, and never becomes an element', async () => {
  const bad = clone(PAL);
  const nasty = '<img src=x onerror=alert(1)> <b>bold</b>\nline two & <script>boom()</script>';
  bad.wings[0].drawers[0].narrative = nasty;
  bad.wings[0].drawers[0].title = '<i>title</i>';
  bad.wings[0].drawers[0].facts = ['<u>fact</u>'];
  bad.wings[0].drawers[0].concepts = ['<tag>'];
  bad.wings[0].drawers[0].files = ['<svg onload=1>.ts'];
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/palace': bad }) });
  await go(m);
  const item = q(m.w, '.mem-wing .mem-item');
  assert.equal(text(item.querySelector('.mem-narr')), nasty);
  assert.equal(text(item.querySelector('.mem-title')), '<i>title</i>');
  assert.equal(text(item.querySelector('.mem-fact')), '<u>fact</u>');
  assert.equal(text(item.querySelector('.mem-tag')), '<tag>');
  assert.equal(text(item.querySelector('.mem-file')), '<svg onload=1>.ts');
  for (const tag of ['img', 'i', 'u', 'script', 'svg']) assert.equal(qa(m.w, tag).length, 0, `<${tag}> from data must not exist`);
  assert.equal(qa(m.w, '.mem-wing b').length, 0, '<b> from data must not exist');
  assert.match(fs.readFileSync(path.join(STATIC, 'pages', 'memory.css'), 'utf8'), /\.mem-narr \{[^}]*white-space:pre-wrap/);
});

test('file chips link to code-server only when the path resolves inside the wing\'s repo', async () => {
  const d = clone(PAL);
  d.wings[0] = { ...d.wings[0], name: 'api', drawers: [{ ...d.wings[0].drawers[0], files: ['src/orders.ts', '/p/shop/api/src/abs.ts', '/etc/passwd', '../secret.ts', 'a/../../b.ts'] }] };
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/palace': d }) });
  await go(m);
  q(m.w, '.mem-wing .mem-item-head').click();
  const chips = qa(m.w, '.mem-wing[data-wing=api] .mem-file');
  assert.equal(chips.length, 5);
  const links = chips.filter((c) => c.tagName === 'A');
  assert.deepEqual(links.map((a) => text(a)), ['src/orders.ts', '/p/shop/api/src/abs.ts']);
  for (const a of links) { assert.equal(a.getAttribute('rel'), 'noopener'); assert.equal(a.getAttribute('target'), '_blank'); assert.match(a.getAttribute('href'), /^https:\/\/box:10000\/\?folder=/); }
  assert.deepEqual(chips.filter((c) => c.tagName !== 'A').map((c) => text(c)), ['/etc/passwd', '../secret.ts', 'a/../../b.ts']);
});

test('Palace with no memory for the project says what to do next; an incompatible answer shows the report line and no cards', async () => {
  const none = { ...clone(PAL), wings: [], rooms: [], gotchas: [], keys: [], keys_checked: true, total: 0 };
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/palace': none }) });
  await go(m);
  assert.match(text(q(m.w, '.mem-palace')), /No memory for shop yet/);
  assert.match(text(q(m.w, '.mem-palace')), /run a session/);
  const m2 = memWorld({ answers: withAnswers({ '/api/memory/shop/palace': { ...PAL, ...STATES.incompatible.body } }) });
  await go(m2);
  assert.equal(qa(m2.w, '.mem-wing').length, 0);
  assert.match(text(q(m2.w, '.mem-note')), /may be wrong until the board is updated; report it/);
});

// ---------------------------------------------------------------- timeline

test('Timeline: day headers, type tag, repo, agent glyph, session chip and tokens; null tokens never print', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m, '#/memory/shop?tab=timeline');
  assert.deepEqual(getCalls(m.w, '/api/memory/shop/observations').map((c) => c.path), [`/api/memory/shop/observations?limit=30`]);
  const rows = qa(m.w, '.mem-list .mem-item');
  assert.equal(rows.length, OBS.items.length);
  assert.ok(qa(m.w, '.mem-day').length >= 1);
  const r0 = rows[0];
  assert.ok(r0.querySelector('.mem-type'));
  assert.ok(r0.querySelector('.mem-repo'));
  assert.ok(r0.querySelector('.glyph.agent'));
  assert.ok(r0.querySelector('.mem-sess'));
  const withTok = OBS.items.findIndex((o) => typeof o.discovery_tokens === 'number');
  if (withTok >= 0) assert.match(text(rows[withTok].querySelector('.mem-tok')), /^\d[\d,]* tok$/);
  assert.doesNotMatch(text(page(m.w)), /undefined|null|NaN/);
  assert.equal(q(m.w, '.tab[aria-selected=true]').getAttribute('data-tab'), 'timeline');
});

test('pagination: Load more asks for before=next_before, appends without duplicates, and the button goes when nothing older is left', async () => {
  const page1 = { ...OBS, items: OBS.items.slice(0, 3), has_more: true, next_before: 1791000000000 };
  const page2 = { ...OBS, items: [...OBS.items.slice(2, 5)], has_more: false, next_before: null };
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/observations': (p) => (p.includes('before=') ? page2 : page1) }) });
  await go(m, '#/memory/shop?tab=timeline');
  assert.equal(qa(m.w, '.mem-list .mem-item').length, 3);
  const btn = q(m.w, '.mem-loadmore');
  assert.ok(btn);
  btn.click();
  await settle();
  const reqs = getCalls(m.w, '/api/memory/shop/observations').map((c) => c.path);
  assert.equal(reqs.length, 2);
  assert.match(reqs[1], /before=1791000000000/);
  const ids = qa(m.w, '.mem-list .mem-item').map((n) => n.getAttribute('data-id'));
  assert.equal(ids.length, new Set(ids).size, 'no duplicate rows');
  assert.equal(ids.length, 5);
  assert.equal(q(m.w, '.mem-loadmore'), null, 'has_more false: no button');
  assert.match(text(q(m.w, '.mem-more')), /everything claude-mem kept/);
});

test('a page cut short by the scan cap says so and still offers Load more', async () => {
  const cut = { ...OBS, items: OBS.items.slice(0, 2), has_more: true, scan_capped: true, next_before: 5 };
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/observations': cut }) });
  await go(m, '#/memory/shop?tab=timeline');
  assert.match(text(q(m.w, '.mem-more')), /cut short/);
  assert.ok(q(m.w, '.mem-loadmore'));
});

test('filters: a select change asks the proxy with it, writes the hash and the per-project storage; a remount reads them back', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m, '#/memory/shop?tab=timeline');
  const sel = qa(m.w, '.mem-filters select')[0];
  sel.value = 'bugfix';
  sel.dispatchEvent({ type: 'change', preventDefault() {} });
  await settle();
  assert.match(getCalls(m.w, '/api/memory/shop/observations').pop().path, /[?&]type=bugfix/);
  assert.match(m.w.location.hash, /type=bugfix/);
  assert.match(m.w.location.hash, /tab=timeline/);
  assert.equal(JSON.parse(m.w.localStorage.getItem('ccboard:memory:shop')).type, 'bugfix');
  const seg = qa(m.w, '.mem-filters .mem-seg')[1];
  seg.querySelectorAll('.seg-btn').find((b) => text(b) === '7 d').click();
  await settle();
  assert.match(getCalls(m.w, '/api/memory/shop/observations').pop().path, /since=\d{13}/);
  assert.match(m.w.location.hash, /range=7d/);
  const m2 = memWorld({ answers: withAnswers() });
  m2.w.localStorage.setItem('ccboard:memory:shop', JSON.stringify({ type: 'decision', agent: 'codex', range: '30d', repo: 'evil/../x' }));
  await go(m2, '#/memory/shop?tab=timeline');
  const last = getCalls(m2.w, '/api/memory/shop/observations').pop().path;
  assert.match(last, /type=decision/);
  assert.match(last, /agent=codex/);
  assert.doesNotMatch(last, /repo=/, 'junk in storage is dropped');
});

test('an address with filters reproduces the view: the link wins over storage and Clear filters empties both', async () => {
  const m = memWorld({ answers: withAnswers() });
  m.w.localStorage.setItem('ccboard:memory:shop', JSON.stringify({ agent: 'codex' }));
  await go(m, '#/memory/shop?tab=summaries&agent=claude&range=24h');
  const path1 = getCalls(m.w, '/api/memory/shop/summaries').pop().path;
  assert.match(path1, /agent=claude/);
  assert.doesNotMatch(path1, /agent=codex/);
  q(m.w, '.mem-clear').click();
  await settle();
  const path2 = getCalls(m.w, '/api/memory/shop/summaries').pop().path;
  assert.doesNotMatch(path2, /agent=|since=/);
  assert.deepEqual(JSON.parse(m.w.localStorage.getItem('ccboard:memory:shop')), { type: '', repo: '', agent: '', session: '', range: '' });
});

test('a session chip filters to that session and a chip in the bar removes the filter', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m, '#/memory/shop?tab=timeline');
  const sid = OBS.items[0].content_session_id;
  q(m.w, '.mem-list .mem-sess').click();
  await settle();
  assert.match(getCalls(m.w, '/api/memory/shop/observations').pop().path, new RegExp(`session=${sid}`));
  assert.match(text(q(m.w, '.mem-sess-chip')), /✕/);
  q(m.w, '.mem-sess-chip').click();
  await settle();
  assert.doesNotMatch(getCalls(m.w, '/api/memory/shop/observations').pop().path, /session=/);
});

// ---------------------------------------------------------------- summaries and search

test('Summaries: a card per summary with the labelled sections that have text, and an empty state that names the next step', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m, '#/memory/shop?tab=summaries');
  const cards = qa(m.w, '.mem-sum');
  assert.equal(cards.length, SUMS.items.length);
  assert.deepEqual(cards[0].querySelectorAll('.mem-k').map((n) => text(n)), ['Request', 'Investigated', 'Learned', 'Completed', 'Next steps']);
  const none = memWorld({ answers: withAnswers({ '/api/memory/shop/summaries': { ...SUMS, items: [], has_more: false } }) });
  await go(none, '#/memory/shop?tab=summaries');
  assert.match(text(q(none.w, '.empty')), /No summaries yet/);
  assert.match(text(q(none.w, '.empty')), /run a session here/);
});

test('Search: the box is debounced at 300 ms (only the last keystroke asks), switches to Search, keeps ?q= in the address', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m);
  const box = q(m.w, '.mem-search');
  for (const v of ['s', 'st', 'sticky']) { box.value = v; box.dispatchEvent({ type: 'input' }); }
  await settle();
  assert.equal(getCalls(m.w, '/api/memory/shop/search').length, 0, 'nothing asked before the timer');
  const live = m.timers.filter((t) => !t.cancelled && !t.ran && t.ms === 300);
  assert.equal(live.length, 1, 'each keystroke cancels the one before');
  await runTimers(m, 300);
  const reqs = getCalls(m.w, '/api/memory/shop/search');
  assert.deepEqual(reqs.map((c) => c.path), ['/api/memory/shop/search?q=sticky&limit=20']);
  assert.equal(q(m.w, '.tab[aria-selected=true]').getAttribute('data-tab'), 'search');
  assert.match(m.w.location.hash, /q=sticky/);
  assert.match(m.w.location.hash, /tab=search/);
  assert.ok(qa(m.w, '.mem-sec').length >= 1);
  assert.match(text(q(m.w, '.mem-counts')), /for “sticky”/);
});

test('a link with ?q= opens Search with the box filled; an empty box says what to type; no match names the next step', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m, '#/memory/shop?q=sticky');
  assert.equal(q(m.w, '.mem-search').value, 'sticky');
  assert.equal(getCalls(m.w, '/api/memory/shop/search').length, 1);
  const empty = memWorld({ answers: withAnswers() });
  await go(empty, '#/memory/shop?tab=search');
  assert.equal(getCalls(empty.w, '/api/memory/shop/search').length, 0, 'no query: nothing is asked');
  assert.match(text(q(empty.w, '.empty')), /Type in the search box/);
  const nothing = memWorld({ answers: withAnswers({ '/api/memory/shop/search': { ...SEARCH, observations: [], sessions: [], prompts: [], total: 0 } }) });
  await go(nothing, '#/memory/shop?q=zzz');
  assert.match(text(q(nothing.w, '.empty')), /Nothing matched “zzz”/);
  assert.match(text(q(nothing.w, '.empty')), /Try fewer or different words/);
});

test('a search prompt is shown as the text it is', async () => {
  const s = { ...SEARCH, prompts: [{ id: 9, prompt_text: '<b>please</b> fix\nit', prompt_number: 2, created_at_epoch: 1791000000000 }] };
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/search': s }) });
  await go(m, '#/memory/shop?q=fix');
  const p = q(m.w, '.mem-prompt .mem-narr');
  assert.equal(text(p), '<b>please</b> fix\nit');
  assert.equal(qa(m.w, '.mem-prompt b').length, 0);
});

// ---------------------------------------------------------------- states

test('worker down (503 {up:false}): the real reason, Retry that says what it asks, and Retry asks again', async () => {
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/observations': err('down') }) });
  await go(m, '#/memory/shop?tab=timeline');
  const d = q(m.w, '.mem-degraded');
  assert.ok(d);
  assert.equal(d.getAttribute('data-state'), 'down');
  assert.match(text(d), /claude-mem is not running/);
  assert.match(text(d), /connection refused: the claude-mem worker is not running/);
  assert.match(text(d), /Retry asks the worker again for the timeline/);
  assert.equal(qa(m.w, '.mem-list').length, 0);
  assert.equal(qa(m.w, 'button.primary').length, 1, 'Retry is the one filled button while nothing else can be done');
  m.answers['/api/memory/shop/observations'] = OBS;
  const before = getCalls(m.w, '/api/memory/shop/observations').length;
  qa(m.w, '.mem-degraded button').find((b) => text(b) === 'Retry').click();
  await settle();
  assert.equal(getCalls(m.w, '/api/memory/shop/observations').length, before + 1);
  assert.equal(q(m.w, '.mem-degraded'), null);
  assert.equal(qa(m.w, '.mem-list .mem-item').length, OBS.items.length);
});

test('worker slow (503 degraded): says slow, with the proxy\'s own reason', async () => {
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/summaries': err('degraded') }) });
  await go(m, '#/memory/shop?tab=summaries');
  assert.match(text(q(m.w, '.mem-degraded b')), /claude-mem is slow/);
  assert.match(text(q(m.w, '.mem-reason')), /no answer within 2\.0 s/);
});

test('a stale answer (200, stale:true) keeps the data and shows a callout with the time and Retry', async () => {
  const stale = { ...OBS, ...STATES.stale.body };
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/observations': stale }) });
  await go(m, '#/memory/shop?tab=timeline');
  const note = q(m.w, '.mem-note[data-note=stale]');
  assert.ok(note);
  assert.match(text(note), /Showing data from \d\d:\d\d\./);
  assert.match(text(note), /the worker is slow/);
  assert.ok(note.querySelector('.mem-retry'));
  assert.equal(qa(m.w, '.mem-list .mem-item').length, OBS.items.length);
  assert.match(text(q(m.w, '.mem-pill-t')), /^stale \d\d:\d\d$/);
});

test('a failure after a good view keeps the last view, labelled with its time, and Retry', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m, '#/memory/shop?tab=timeline');
  assert.equal(qa(m.w, '.mem-list .mem-item').length, OBS.items.length);
  m.answers['/api/memory/shop/observations'] = err('down');
  await pickTab(m, 'summaries');
  await pickTab(m, 'timeline');
  assert.equal(qa(m.w, '.mem-list .mem-item').length, OBS.items.length, 'the rows stay');
  assert.match(text(q(m.w, '.mem-note[data-note=stale]')), /Showing data from \d\d:\d\d\./);
  assert.match(text(q(m.w, '.mem-note[data-note=stale]')), /connection refused/);
  assert.equal(q(m.w, '.mem-degraded'), null);
});

test('offline (no status) reads as the board being offline, not the worker', async () => {
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/palace': { __error: { message: 'Failed to fetch' } } }) });
  await go(m);
  assert.match(text(q(m.w, '.mem-degraded b')), /cannot be reached, retrying/);
  assert.match(text(q(m.w, '.mem-reason')), /Failed to fetch/);
});

test('partial, ambiguous, incompatible and untested each get their own line', async () => {
  const over = { ...PAL, partial: ['website/sticky-summary'], ambiguous: [{ key: 'website', shared_with: ['x/website'] }], compat: 'untested', worker_version: '13.34.2' };
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/palace': over }) });
  await go(m);
  assert.deepEqual(qa(m.w, '.mem-note').map((n) => n.getAttribute('data-note')), ['untested', 'partial', 'ambiguous']);
  assert.equal(qa(m.w, '.mem-wing').length, PAL.wings.length, 'the data is still shown');
});

test('claude-mem off (503 state off) says so and offers no Retry', async () => {
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/palace': { __error: { status: 503, body: { state: 'off', up: false, reason: 'claude-mem is turned off (CCBOARD_CLAUDE_MEM=0)' } } } }) });
  await go(m);
  assert.match(text(q(m.w, '.mem-degraded')), /turned off on this box/);
  assert.equal(qa(m.w, '.mem-degraded button').length, 0);
});

// ---------------------------------------------------------------- N new

test('"N new - Load": appears only when the worker total grows past the count at load, Load refetches page one and adds no polling', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m, '#/memory/shop?tab=timeline');
  assert.ok(q(m.w, '.mem-new').classList.contains('hidden'));
  const fresh = clone(OBS);
  fresh.items.unshift({ ...clone(OBS.items[0]), id: 99999, title: 'A brand new note', created_at_epoch: OBS.items[0].created_at_epoch + 5000 });
  m.answers['/api/memory/shop/observations'] = fresh;
  m.w.ctx.__st.memory.observations = HEALTH.observations + 4;
  m.w.run('updateCurrentPage(state)');
  assert.equal(q(m.w, '.mem-new').classList.contains('hidden'), false);
  assert.equal(text(q(m.w, '.mem-new-btn')), '4 new · Load');
  assert.equal(getCalls(m.w, '/api/memory/shop/observations').length, 1, 'showing the pill fetched nothing');
  q(m.w, '.mem-new-btn').click();
  await settle();
  assert.equal(getCalls(m.w, '/api/memory/shop/observations').length, 2, 'Load asks page one again');
  assert.ok(qa(m.w, '.mem-list .mem-item').some((n) => n.getAttribute('data-id') === '99999'));
  assert.equal(qa(m.w, '.mem-list .mem-item').length, OBS.items.length + 1, 'the new row is on top of what was there');
  assert.ok(q(m.w, '.mem-new').classList.contains('hidden'));
  assert.deepEqual(m.timers.filter((t) => t.ms > 300).length, 0, 'no polling timer was started');
});

test('the pill never shows for an unknown count, a smaller count or an equal one', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m, '#/memory/shop?tab=timeline');
  for (const v of [null, HEALTH.observations - 3, HEALTH.observations]) {
    m.w.ctx.__st.memory.observations = v;
    m.w.run('updateCurrentPage(state)');
    assert.ok(q(m.w, '.mem-new').classList.contains('hidden'), String(v));
  }
});

test('a project change in the select goes to #/memory/<project> and loads that project, not the old one', async () => {
  const m = memWorld({ answers: withAnswers({ '/api/memory/blog/palace': { ...PAL, project: 'blog', wings: [] } }) });
  await go(m);
  const sel = q(m.w, '.mem-project');
  sel.value = 'blog';
  sel.dispatchEvent({ type: 'change' });
  await settle();
  assert.equal(m.w.location.hash, '#/memory/blog');
  assert.equal(getCalls(m.w, '/api/memory/blog/palace').length, 1);
  assert.equal(m.w.localStorage.getItem('ccboard:memory-last'), 'blog');
});

test('a late answer for a tab that is no longer shown is dropped', async () => {
  let release;
  const slow = new Promise((r) => { release = r; });
  const m = memWorld({ answers: withAnswers({ '/api/memory/shop/summaries': () => slow }) });
  await go(m, '#/memory/shop?tab=summaries');
  await pickTab(m, 'timeline');
  release(SUMS);
  await settle();
  assert.equal(qa(m.w, '.mem-sum').length, 0, 'the timeline is on screen, the old summaries answer was dropped');
  assert.equal(qa(m.w, '.mem-list .mem-item').length, OBS.items.length);
});

// ---------------------------------------------------------------- send to session

test('Send to session: the menu lists only idle agent sessions of this project and nothing is sent when one is picked', async () => {
  const m = memWorld({ answers: withAnswers({ '/api/sessions/': { ok: true, pasted: true } }) });
  await go(m, '#/memory/shop?tab=timeline');
  const btn = q(m.w, '.mem-send');
  btn.click();
  const items = m.w.document.querySelectorAll('.menuitem');
  assert.deepEqual(items.map((i) => text(i).trim()), ['api / s1'], 'a working session and a plain shell are not offered');
  items[0].click();
  assert.equal(calls(m.w).filter((c) => c.method === 'POST').length, 0, 'choosing a session sends nothing');
  const panel = q(m.w, '.mem-send-panel');
  assert.ok(panel);
  assert.match(text(panel), /Paste into api \/ s1\?/);
  assert.match(text(panel.querySelector('.mem-send-text')), /^Context from claude-mem \(observation/);
  const send = panel.querySelectorAll('button').find((b) => text(b) === 'Send');
  send.click();
  await settle();
  const post = calls(m.w).filter((c) => c.method === 'POST');
  assert.equal(post.length, 1);
  assert.equal(post[0].path, '/api/sessions/shop--api--s1/prompt');
  assert.ok(post[0].body.text.length > 20 && post[0].body.text.length <= 4000);
  assert.match(text(panel.querySelector('.mem-send-status')), /Sent to api \/ s1\. The session may not have acted on it yet\./);
});

test('Send to session: a 409 shows the route\'s own message and never says Sent', async () => {
  const m = memWorld({ answers: withAnswers({ '/api/sessions/': { __error: { status: 409, message: 'x', body: { error: 'session is busy: it is working on a turn' } } } }) });
  await go(m, '#/memory/shop?tab=timeline');
  q(m.w, '.mem-send').click();
  m.w.document.querySelectorAll('.menuitem')[0].click();
  const panel = q(m.w, '.mem-send-panel');
  panel.querySelectorAll('button').find((b) => text(b) === 'Send').click();
  await settle();
  assert.match(text(panel.querySelector('.mem-send-status')), /session is busy: it is working on a turn/);
  assert.doesNotMatch(text(panel), /Sent to/);
  assert.ok(panel.querySelector('.mem-send-status').classList.contains('bad'));
});

test('Send to session with no idle session says so and offers nothing to pick', async () => {
  const st = STATE();
  st.projects[0].repos[0].sessions = [];
  const m = memWorld({ st, answers: withAnswers() });
  await go(m, '#/memory/shop?tab=timeline');
  q(m.w, '.mem-send').click();
  const items = m.w.document.querySelectorAll('.menuitem');
  assert.deepEqual(items.map((i) => text(i).trim()), ['No idle session in this project']);
  items[0].click();
  assert.equal(q(m.w, '.mem-send-panel'), null);
});

// ---------------------------------------------------------------- viewer link

test('the viewer link is hidden with an install hint while mem_viewer_url is null; set, it opens in a new tab with the exposure caption', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m);
  assert.equal(qa(m.w, '.mem-viewer-link').length, 0);
  assert.equal(qa(m.w, '.mem-viewer-cap').length, 0, 'the how-to lives in Settings > Box now');
  const st = STATE();
  st.config.mem_viewer_url = 'https://box.example.ts.net:10443/';
  const m2 = memWorld({ st, answers: withAnswers() });
  await go(m2);
  const a = q(m2.w, '.mem-viewer-link');
  assert.equal(a.getAttribute('href'), 'https://box.example.ts.net:10443/');
  assert.equal(a.getAttribute('target'), '_blank');
  assert.equal(a.getAttribute('rel'), 'noopener');
  assert.match(text(q(m2.w, '.mem-viewer')), /outside the board's sign-in: every device on your tailnet can open and change the claude-mem worker/);
  const bad = STATE();
  bad.config.mem_viewer_url = 'javascript:alert(1)';
  const m3 = memWorld({ st: bad, answers: withAnswers() });
  await go(m3);
  assert.equal(qa(m3.w, '.mem-viewer-link').length, 0, 'only an https address is ever linked');
});

// ---------------------------------------------------------------- health tile

const tileOf = (w, h, o) => { w.ctx.__h = h; w.ctx.__o = o; return w.run('memoryHealthTile(__h, __o)'); };

test('health tile: up shows rates, queue with the processing marker, active sessions with the stale estimate, and the recovered error', () => {
  const { w } = memWorld();
  const t = tileOf(w, HEALTH, {});
  const s = text(t);
  assert.match(s, /claude-mem health/);
  assert.match(s, /Observations11,836 61\.5 per day over 24 h, 74\.2 per day over 7 days/);
  assert.match(s, /Summaries187 4 per day over 24 h, 5\.1 per day over 7 days/);
  assert.match(s, /Observer queue12 waiting for the observer · ✽ processing now/);
  assert.match(s, /Active sessions2 · about 1 possibly stale \(an estimate: the worker's count minus the board's live Claude sessions\)/);
  assert.match(s, /recovered/);
  assert.equal(t.querySelectorAll('.mem-err.failing').length, 0, 'amber only while the observer is failing');
  assert.match(s, /Plugin 13\.34\.2 is installed; the running worker is 13\.31\.0 until it restarts/);
  assert.match(s, /samples, taken every 5 minutes/);
  assert.doesNotMatch(s, /undefined|NaN|\[object|null/);
});

test('health tile: a rate with too few samples reads collecting, never 0 per day', () => {
  const { w } = memWorld();
  const s = text(tileOf(w, { ...HEALTH, rates: { obs: { d1: null, d7: null }, sum: { d1: null, d7: null } } }, {}));
  assert.match(s, /Observations11,836 collecting over 24 h, collecting over 7 days/);
  assert.match(s, /Summaries187 collecting over 24 h, collecting over 7 days/);
  assert.doesNotMatch(s, /\b0 per day/);
});

test('health tile: down shows its reason and unknown, never zeros', () => {
  const { w } = memWorld();
  const t = tileOf(w, { state: 'down', reason: 'connection refused: the claude-mem worker is not running', observations: null, summaries: null, queue_depth: null, active_sessions: null, last_error: null }, {});
  const s = text(t);
  assert.match(s, /down · connection refused/);
  assert.match(s, /Observationsunknown unknown/);
  assert.match(s, /Observer queueunknown/);
  assert.doesNotMatch(s, /\b0\b/);
  const stale = text(tileOf(w, { state: 'down', reason: 'x', observations: 500, queue_depth: 7, active_sessions: 2 }, {}));
  assert.doesNotMatch(stale, /500|waiting for the observer/, 'a down worker\'s old counts are not shown as if they were current');
});

test('health tile: a failing observer is amber with a glyph and words; a long message is cut to one line and set as text', () => {
  const { w } = memWorld();
  const long = '<b>Provider reported the inference allowance exhausted</b> '.repeat(12);
  const t = tileOf(w, { ...HEALTH, state: 'degraded', reason: 'the observer is failing', last_error: { at: '2026-10-03T07:40:00+00:00', message: long, provider: 'claude', failures: 3, last_success_at: '2026-10-03T06:00:00+00:00' } }, {});
  const err = t.querySelector('.mem-err');
  assert.ok(err.classList.contains('failing'));
  assert.match(text(err), /^⚠observer failing · /);
  assert.ok(text(t.querySelector('.mem-err-msg')).length <= 140);
  assert.match(text(t.querySelector('.mem-err-msg')), /^<b>Provider reported/, 'markup stays text');
  assert.equal(t.querySelectorAll('b').filter((n) => n.tagName === 'B' && n.parentNode.className !== 'mem-kv').length, 0);
  assert.equal(t.getAttribute('data-state'), 'degraded');
  assert.match(text(t), /degraded · the observer is failing/);
});

test('health tile: "recovered" needs a later success and no failures', () => {
  const { w } = memWorld();
  const row = (e) => text(tileOf(w, { ...HEALTH, last_error: e }, {}).querySelector('.mem-err'));
  assert.match(row({ at: '2026-10-03T07:00:00+00:00', message: 'x', failures: 0, last_success_at: '2026-10-03T08:00:00+00:00' }), /recovered/);
  assert.match(row({ at: '2026-10-03T07:00:00+00:00', message: 'x', failures: 2, last_success_at: '2026-10-03T08:00:00+00:00' }), /observer failing/);
  assert.match(row({ at: '2026-10-03T09:00:00+00:00', message: 'x', failures: 0, last_success_at: '2026-10-03T08:00:00+00:00' }), /observer failing/);
  assert.match(row({ at: '2026-10-03T09:00:00+00:00', message: 'x', failures: 0, last_success_at: null }), /observer failing/);
  assert.match(text(tileOf(w, { ...HEALTH, last_error: null }, {})), /Last provider errornone/);
});

test('health tile: hidden entirely when claude-mem is off or not sampled; compact drops the long rows; the link goes to #/memory', () => {
  const { w } = memWorld();
  assert.equal(tileOf(w, null, {}), null);
  assert.equal(tileOf(w, { state: 'off' }, {}), null);
  const c = tileOf(w, HEALTH, { compact: true, link: true });
  assert.doesNotMatch(text(c), /Summaries|Active sessions/);
  assert.equal(c.querySelector('a').getAttribute('href'), '#/memory');
});

test('the tile sits under the Memory header and follows the 3 s poll', async () => {
  const m = memWorld({ answers: withAnswers() });
  await go(m);
  assert.ok(q(m.w, '.mem-tile-host .mem-tile'));
  m.w.ctx.__st.memory = { ...HEALTH, state: 'down', reason: 'connection refused', observations: null };
  m.w.run('updateCurrentPage(state)');
  assert.match(text(q(m.w, '.mem-tile-host')), /down · connection refused/);
  assert.match(text(q(m.w, '.mem-pill-t')), /^down · connection refused/);
  m.w.ctx.__st.memory = null;
  m.w.run('updateCurrentPage(state)');
  assert.ok(q(m.w, '.mem-tile-host').classList.contains('hidden'), 'no state.memory: no tile');
});

// ---------------------------------------------------------------- gotchas strip

const G = (n, extra = {}) => Array.from({ length: n }, (_, i) => ({ id: 100 + i, title: `Gotcha ${i}`, subtitle: `line ${i}`, type: 'bugfix', concepts: ['gotcha'], files: ['src/a.ts'], created_at_epoch: Date.now() - (i + 1) * 3600000, key: 'api', narrative: `narrative ${i}`, facts: ['f'], ...extra }));
const strip = (w, d, o) => { w.ctx.__d = d; w.ctx.__o = o; return w.run('memoryGotchasStrip(__d, __o)'); };

test('gotchas strip: zero shows nothing, one and three show title, one line and age; a gotcha without a subtitle shows the title alone', () => {
  const { w } = memWorld();
  assert.equal(strip(w, { ...PAL, gotchas: [] }, {}), null);
  assert.equal(strip(w, { ...PAL, gotchas: undefined }, {}), null);
  const one = strip(w, { ...PAL, gotchas: G(1) }, { project: 'shop' });
  assert.equal(one.querySelectorAll('.mem-gotcha').length, 1);
  assert.match(text(one), /Newest gotchas in this project, from claude-mem/);
  assert.match(text(one.querySelector('.mem-gotcha')), /Gotcha 0line 01h ago/);
  assert.equal(one.querySelector('.mem-gotcha-more'), null);
  const three = strip(w, { ...PAL, gotchas: G(5) }, { project: 'shop' });
  assert.equal(three.querySelectorAll('.mem-gotcha').length, 3, 'at most three');
  const bare = strip(w, { ...PAL, gotchas: G(1, { subtitle: null }) }, {});
  assert.equal(bare.querySelector('.mem-gotcha-s'), null);
});

test('gotchas strip: a stale, partial-state or not-ok answer shows nothing (never a stale gotcha as fresh)', () => {
  const { w } = memWorld();
  assert.equal(strip(w, { ...PAL, gotchas: G(2), stale: true }, {}), null);
  assert.equal(strip(w, { ...PAL, gotchas: G(2), state: 'incompatible' }, {}), null);
});

test('gotchas strip: markup in a title is text; a click opens the drawer in place on desktop and again closes it', () => {
  const { w } = memWorld();
  const s = strip(w, { ...PAL, gotchas: G(2, { title: '<img src=x onerror=1>', narrative: '<script>x</script>' }) }, { project: 'shop' });
  assert.equal(text(s.querySelector('.mem-gotcha-t')), '<img src=x onerror=1>');
  assert.equal(s.querySelectorAll('img').length, 0);
  const panel = s.querySelector('.mem-gotcha-panel');
  assert.ok(panel.classList.contains('hidden'));
  s.querySelector('.mem-gotcha').click();
  assert.equal(panel.classList.contains('hidden'), false);
  assert.equal(text(panel.querySelector('.mem-narr')), '<script>x</script>');
  s.querySelector('.mem-gotcha').click();
  assert.ok(panel.classList.contains('hidden'));
});

test('gotchas strip: on a narrow screen it is one line with the rest behind a tap', () => {
  const m = memWorld();
  m.w.ctx.matchMedia = (qq) => ({ matches: /max-width: 599px/.test(qq), addEventListener() {}, removeEventListener() {} });
  m.w.run('window.matchMedia = matchMedia');
  const s = strip(m.w, { ...PAL, gotchas: G(3) }, {});
  assert.equal(s.getAttribute('data-open'), 'false');
  const more = s.querySelector('.mem-gotcha-more');
  assert.equal(text(more), '2 more');
  more.click();
  assert.equal(s.getAttribute('data-open'), 'true');
  assert.equal(text(more), 'fewer');
  assert.match(fs.readFileSync(path.join(STATIC, 'pages', 'memory.css'), 'utf8'), /\.mem-gotchas\[data-open=false\] \.mem-gotcha:nth-child\(n\+2\) \{ display:none/);
});

test('gotchas strip on a touch screen opens the drawer as a sheet and gives the focus back to the gotcha', () => {
  const m = memWorld({ coarse: true });
  const s = strip(m.w, { ...PAL, gotchas: G(1) }, { project: 'shop' });
  m.w.document.body.append(s);
  const btn = s.querySelector('.mem-gotcha');
  btn.click();
  const sheet = m.w.document.getElementById('sheet');
  assert.ok(sheet.open, 'the sheet is open');
  assert.match(text(sheet), /Gotcha 0/);
  assert.match(text(sheet), /narrative 0/);
  sheet.close();
  assert.equal(m.w.document.activeElement, btn, 'focus returns to the gotcha that opened it');
});

test('gotchas mount: fills the host once, and a stopped worker, a slow one or a dropped answer leave it hidden with no toast', async () => {
  const mk = (answer) => memWorld({ answers: { '/api/memory/shop/palace': answer } });
  const m = mk({ ...PAL, gotchas: G(2) });
  const host = m.w.document.createElement('div');
  host.classList.add('hidden');
  m.w.ctx.__host = host;
  const node = await m.w.run("memoryGotchasMount(__host, 'shop', { isCurrent: () => true })");
  assert.ok(node);
  assert.equal(host.classList.contains('hidden'), false);
  const down = mk(err('down'));
  const h2 = down.w.document.createElement('div');
  h2.classList.add('hidden');
  down.w.ctx.__host = h2;
  assert.equal(await down.w.run("memoryGotchasMount(__host, 'shop', {})"), null);
  assert.ok(h2.classList.contains('hidden'));
  assert.equal(plain(down.w.get('__toasts')).length, 0, 'no toast');
  const late = mk({ ...PAL, gotchas: G(2) });
  const h3 = late.w.document.createElement('div');
  late.w.ctx.__host = h3;
  assert.equal(await late.w.run("memoryGotchasMount(__host, 'shop', { isCurrent: () => false })"), null, 'an answer for a closed sheet is dropped');
  assert.equal(h3.children.length, 0);
});

// ---------------------------------------------------------------- static scans

test('memory.js writes no style, no innerHTML, names no worker address and uses no em dash in its text', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'memory.js'), 'utf8');
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.doesNotMatch(code, /\.innerHTML|innerHTML\s*=|outerHTML|insertAdjacentHTML|cssText|setAttribute\(\s*['"`]style|\.style\b|\bstyle\s*:/);
  assert.doesNotMatch(src, /(?<![0-9])377[0-9]{2}(?![0-9])|127\.0\.0\.1|localhost/, 'the browser never names the worker');
  assert.doesNotMatch(code, /—/, 'no em dashes in UI text');
  assert.doesNotMatch(code, /\bfetch\(/, 'every call goes through api()');
  assert.doesNotMatch(code, /['"`]\/api\/memory\/(?!health|\$\{)/, 'only the proxy routes');
});

test('the type bar is a ladder of classes: every width class the code can produce exists in pages.css', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'memory.css'), 'utf8');
  for (let n = 5; n <= 100; n += 5) assert.match(css, new RegExp(`\\.mem-bar \\.mem-w${n} \\{ flex:0 0 ${n}%; \\}`), `mem-w${n}`);
  const { w } = memWorld();
  for (const f of [0, 0.01, 0.049, 0.5, 0.97, 1]) {
    const step = plain(w.run(`memStep(${f})`));
    assert.ok(step >= 5 && step <= 100 && step % 5 === 0, String(f));
  }
});

test('memory.js is define-only: loading it runs nothing but registerPage', () => {
  const w = makeWorld();
  installDom(w);
  for (const f of ['core.js', 'components.js', 'router.js']) w.load(f);
  w.run('globalThis.__api = 0; const realApi = api; api = async () => { __api++; return {}; };');
  w.load('pages/memory.js');
  assert.equal(w.get('__api'), 0);
  assert.ok(w.get("typeof pages.memory === 'object'"));
  assert.equal(w.get("typeof memoryPalaceView + typeof memoryHealthTile + typeof memoryGotchasStrip + typeof memoryGotchasMount"), 'functionfunctionfunctionfunction');
});


// ---------------------------------------------------------------- demo mode (?demo=1): every tab and state from the fixtures, with the real api()

function demoMemWorld(search = '?demo=1') {
  const w = makeWorld({
    fetch: async (url) => {
      const m = /^\/static\/demo\/(\w+)\.json$/.exec(String(url));
      if (!m) throw new Error(`demo mode asked for ${url}`);
      const body = fs.readFileSync(path.join(STATIC, 'demo', `${m[1]}.json`), 'utf8');
      return { ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(body) };
    },
  });
  w.location.search = search;
  installDom(w);
  for (const f of ['core.js', 'components.js', 'keymap.js', 'router.js', 'pages/memory.js']) w.load(f);
  w.ctx.__st = fx('state');
  w.run('state = __st');
  return w;
}

test('?demo=1: Palace, Timeline, Summaries and Search all render from the fixtures, and the header shows the demo worker', async () => {
  const w = demoMemWorld();
  w.location.hash = '#/memory/ccboard';
  await settle();
  assert.equal(page(w).querySelectorAll('.mem-wing').length, PAL.wings.length);
  assert.match(text(page(w).querySelector('.mem-pill-t')), /^up v13\.31\.0 · 11,836 obs$/);
  assert.ok(page(w).querySelector('.mem-tile'), 'the health tile too');
  for (const [tab, sel, n] of [['timeline', '.mem-list .mem-item', OBS.items.length], ['summaries', '.mem-sum', SUMS.items.length]]) {
    w.location.hash = `#/memory/ccboard?tab=${tab}`;
    await settle();
    assert.equal(page(w).querySelectorAll(sel).length, n, tab);
  }
  w.location.hash = '#/memory/ccboard?q=sticky';
  await settle();
  assert.ok(page(w).querySelectorAll('.mem-sec').length >= 1, 'search');
  assert.doesNotMatch(text(page(w)), /undefined|\[object|NaN/);
});

test('?demo=1&mem=down and mem=stale: the degraded panel and the stale callout, both with the real reason', async () => {
  const down = demoMemWorld('?demo=1&mem=down');
  down.location.hash = '#/memory/ccboard';
  await settle();
  assert.match(text(page(down).querySelector('.mem-degraded')), /connection refused: the claude-mem worker is not running/);
  const stale = demoMemWorld('?demo=1&mem=stale');
  stale.location.hash = '#/memory/ccboard?tab=timeline';
  await settle();
  assert.match(text(page(stale).querySelector('.mem-note[data-note=stale]')), /Showing data from \d\d:\d\d\./);
  assert.ok(page(stale).querySelectorAll('.mem-item').length > 0);
  const inc = demoMemWorld('?demo=1&mem=incompatible');
  inc.location.hash = '#/memory/ccboard?tab=summaries';
  await settle();
  assert.match(text(page(inc).querySelector('.mem-note[data-note=incompatible]')), /report it/);
  assert.equal(page(inc).querySelectorAll('.mem-sum').length, 0);
});
