// Contract tests for the quad page (app/static/pages/quad.js, v0.5.9 and v3 in v0.5.9c, route #/quad?s=a,b,c,...&l=1|2|4|6|8|10&p=<project>): the pure helpers (layout
// caps by window width and the 240 px minimum, the query, slot state, auto-fill, the task line, size chip, auto-fit rule, shortcuts), then the page itself on minidom's DOM
// with the real scripts in index.html order: tiles per slot with the exact grid ttyUrl, up to ten of them, iframes that are never re-created or reordered, the teardown
// order (about:blank before remove), persistence and URL precedence, zoom as an overlay, fullscreen, the mode picker and the tail, the one-up chip switcher under 840 px,
// the active tile and the host key bar, Allow / Deny / In terminal, the tile menu's ctx (what TermKit.tileMenu gets) and what its actions do, the docked composer,
// auto-fit, the hidden-tab reload and the shortcuts. Timers, Date and ResizeObserver are fakes; api(), toast and Live.subscribe are recorders; every iframe gets a fake
// contentWindow before its load event. TermKit's tileMenu / composer / tune are either removed (kit: 'none', the default: the old title menu) or recorders (kit: 'stub');
// slice B's real ones have their own tests (termkit.test.mjs).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { ISO, fixtureState, sess, projectsOf, fakeState } from './world.mjs';

const CK = 'phasezero--website--t-checkout-redesign';      // waiting on a permission (pending_permissions id 12)
const P3 = 'petroit--api--s3';                              // waiting, a question
const P2 = 'petroit--api--s2';                              // errored, needs an acknowledgement (older)
const P1 = 'petroit--api--s1';                              // done, needs an acknowledgement
const CX = 'ccboard--ccboard--cx1';                         // codex, working
const WT = 'phasezero--NestJs-Ecommerce-Backend--t-stock-sync';   // claude, working, has a task
const S1 = 'ccboard--ccboard--s1';                          // idle
const ENDED = 'phasezero--root--s1';                        // ended: never offered
const BASE = 1_800_000_000_000;

const GRID = (name) => `/tty/?arg=${name}&arg=grid&fontSize=11&rendererType=canvas&disableResizeOverlay=true&disableReconnect=true`;

/** The quad's part of pages/quad.css (split out of pages.css for issue #103): from its section header to the next section header, or the end of the file (the fullscreen rules follow it there). */
function quadBlock(css) {
  const at = css.indexOf('/* ---------- quad (v0.5.9');
  const next = css.indexOf('/* ---------- ', at + 20);
  return css.slice(at, next < 0 ? undefined : next);
}

// ---------------------------------------------------------------- a world

function fakeClock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  return {
    setTimeout(fn, ms = 0) { const id = ++seq; timers.set(id, { at: now + ms, fn, every: 0 }); return id; },
    setInterval(fn, ms = 0) { const id = ++seq; timers.set(id, { at: now + ms, fn, every: Math.max(1, ms) }); return id; },
    clearTimeout(id) { timers.delete(id); },
    clearInterval(id) { timers.delete(id); },
    get now() { return now; },
    get pending() { return timers.size; },
    advance(ms) {
      const end = now + ms;
      for (;;) {
        let next = null;
        for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next.t.at || (t.at === next.t.at && id < next.id))) next = { id, t };
        if (!next) break;
        now = next.t.at;
        if (next.t.every) next.t.at += next.t.every; else timers.delete(next.id);
        next.t.fn();
      }
      now = end;
    },
  };
}

/** A ttyd window as TermKit.bind and the quad see it: window.term (cols, rows, fit, focus), the document with recorded listeners, location.reload. */
function fakeWin(cols = 80, rows = 24) {
  const listeners = {};
  const win = {
    fits: 0, reloads: 0, focused: 0, listeners,
    document: { addEventListener(type, fn, opts) { (listeners[type] ||= []).push({ fn, opts }); }, querySelector: () => ({ addEventListener() {}, style: {} }), documentElement: { style: {} }, body: { style: {} } },
    term: { cols, rows, fit() { win.fits += 1; }, focus() { win.focused += 1; } },
    location: { reload() { win.reloads += 1; } },
    Event: class {}, dispatchEvent() {}, focus() {},
  };
  return win;
}

/** The recorders TermKit.tileMenu / composer / tune are replaced with in kit: 'stub'. Each call is kept in __kit; every controller has the isOpen getter the real ones have. */
const KIT_STUB = `
  globalThis.__kit = { menus: [], composers: [], tunes: [] };
  const ctl = (rec, extra) => Object.assign({ close() { rec.closed += 1; rec.open = false; }, update(p) { rec.updates.push(p); Object.assign(rec.ctx, p); }, get isOpen() { return rec.open; } }, extra);
  TermKit.tileMenu = (ctx) => {
    const rec = { ctx, opened: [], closed: 0, open: false, updates: [] };
    __kit.menus.push(rec);
    return ctl(rec, { open(anchor, byKeyboard) { if (rec.open) { rec.closed += 1; rec.open = false; return; } rec.opened.push({ anchor, byKeyboard }); rec.open = true; } });
  };
  TermKit.composer = (ctx) => {
    const rec = { ctx, opened: [], closed: 0, destroyed: 0, open: false, updates: [], el: document.createElement('div') };
    rec.el.className = 'tk-composer tk-docked';
    __kit.composers.push(rec);
    return ctl(rec, { el: rec.el, open(anchor) { rec.opened.push(anchor); rec.open = true; }, destroy() { rec.destroyed += 1; rec.open = false; rec.el.remove(); } });
  };
  TermKit.tune = (ctx) => {
    const rec = { ctx, opened: [], closed: 0, destroyed: 0, open: false, updates: [], runs: [], renames: [] };
    __kit.tunes.push(rec);
    return ctl(rec, { open(anchor, byKeyboard) { rec.opened.push({ anchor, byKeyboard }); rec.open = true; }, mount() { return null; }, destroy() { rec.destroyed += 1; rec.open = false; },
      run(cmd, arg, o) { rec.runs.push({ cmd, arg, anchor: o && o.anchor }); return Promise.resolve(true); }, rename(anchor) { rec.renames.push(anchor); rec.open = true; } });
  };
`;

/** `n` slots: the names, then empty strings (the saved and the URL state carry ten). */
const pad = (...names) => [...names, ...Array(10 - names.length).fill('')];

function quadWorld({ wide = true, width = null, state = fixtureState(), storage = {}, hash = null, withKeymap = true, inbox = false, shell = false, kit = 'none', init = '', extra = {} } = {}) {
  const clock = fakeClock();
  class FakeDate extends Date { static now() { return BASE + clock.now; } }
  const ros = [];
  class FakeRO {
    constructor(cb) { this.cb = cb; this.nodes = []; this.gone = false; ros.push(this); }
    observe(n) { this.nodes.push(n); }
    disconnect() { this.gone = true; this.nodes = []; }
  }
  const w = makeWorld({
    matchMedia: (q) => ({ matches: wide && /840/.test(q), addEventListener() {}, removeEventListener() {} }),
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, setInterval: clock.setInterval, clearInterval: clock.clearInterval, Date: FakeDate, ResizeObserver: FakeRO,
    ...(width ? { innerWidth: width } : {}),
    ...extra,
  });
  installDom(w);
  for (const [k, v] of Object.entries(storage)) w.localStorage.setItem(k, typeof v === 'string' ? v : JSON.stringify(v));
  for (const f of ['core.js', 'components.js', ...(withKeymap ? ['keymap.js'] : []), 'live.js', 'termkit.js', 'router.js', 'pages/agents.js', ...(inbox ? ['pages/inbox.js'] : []), 'pages/quad.js', 'pages/placeholders.js']) w.load(f);
  w.run("registerPage('memory', { mount() {}, update() {}, unmount() {} })");                       // some other page to leave to (the memory route's real page is tested in memory.test.mjs)
  w.ctx.__calls = []; w.ctx.__toasts = []; w.ctx.__fail = {}; w.ctx.__live = { sub: [], unsub: [], fns: {} };
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      const f = Object.entries(__fail).find(([p]) => path.includes(p));
      if (f) { const e = new Error(f[1].message || 'failed'); e.status = f[1].status; throw e; }
      return { ok: true };
    };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    poll = async () => {};
    Live.subscribe = (tmux, fn) => { __live.sub.push(tmux); __live.fns[tmux] = fn; return () => { __live.unsub.push(tmux); delete __live.fns[tmux]; }; };
    Live.isDemo = () => false;
  `);
  w.run(kit === 'stub' ? KIT_STUB : (kit === 'none' ? 'TermKit.tileMenu = undefined; TermKit.composer = undefined; TermKit.tune = undefined;' : ''));
  if (init) w.run(init);
  w.ctx.__st = state;
  w.run('state = __st');
  if (shell) w.run('globalThis.__created = []; globalThis.Shell = { openCreate(kind, ctx) { __created.push({ kind, ctx }); return true; }, syncCrumbs() { __crumbs = (globalThis.__crumbs || 0) + 1; } }; var __crumbs = 0;');
  const env = { w, clock, ros, Q: w.get('Quad') };
  if (hash !== null) w.location.hash = hash;
  return env;
}

const page = (w) => w.document.querySelector('#page');
const tiles = (w) => page(w).querySelectorAll('.qtile[data-tmux]');
const tileOf = (w, tmux) => page(w).querySelector(`.qtile[data-tmux=${tmux}]`);
const names = (w) => tiles(w).map((t) => t.getAttribute('data-tmux'));
const slotsOf = (w) => Object.fromEntries(tiles(w).map((t) => [t.getAttribute('data-tmux'), t.getAttribute('data-slot')]));
const frameOf = (w, tmux) => tileOf(w, tmux).querySelector('iframe');
const calls = (w) => plain(w.get('__calls'));
const posts = (w, prefix) => calls(w).filter((c) => c.method === 'POST' && c.path.startsWith(prefix));
const resizes = (w, tmux) => calls(w).filter((c) => c.method === 'POST' && (tmux ? c.path === `/api/sessions/${tmux}/resize` : /\/resize$/.test(c.path)));
const toasts = (w) => plain(w.get('__toasts'));
const live = (w) => plain(w.get('__live'));
const text = (n) => (n ? n.textContent : '');
const button = (root, re) => root.querySelectorAll('button').find((b) => re.test(text(b)));
const savedOf = (w, scope = 'all') => JSON.parse(w.localStorage.getItem(`ccboard:quad:${scope}`) || 'null');
const arm = (w, tmux, cols = 80, rows = 24) => { const f = frameOf(w, tmux); f.contentWindow = fakeWin(cols, rows); return f; };
const load = (w, tmux, cols, rows) => { const f = arm(w, tmux, cols, rows); f.dispatchEvent({ type: 'load' }); return f; };
const setState = (env, st) => { env.w.ctx.__st = st; env.w.run('state = __st; updateCurrentPage(state)'); };
/** Record the order of src assignments and remove() on an iframe. */
function spy(frame, log) {
  let src = frame.src;
  Object.defineProperty(frame, 'src', { get: () => src, set: (v) => { src = v; log.push(`src:${v}`); }, configurable: true });
  const rm = frame.remove.bind(frame);
  frame.remove = () => { log.push('remove'); rm(); };
}
/** A keydown with Ctrl+Alt; `rec` is shared with the copy minidom's dispatch hands the listeners, so the test can see preventDefault. */
const key = (over, rec = {}) => ({ type: 'keydown', ctrlKey: true, altKey: true, metaKey: false, shiftKey: false, preventDefault() { rec.prevented = true; }, stopPropagation() { rec.stopped = true; }, rec, ...over });

// ---------------------------------------------------------------- layout

test('layoutFor: under 840 px one tile with the chips; 840-1199 up to 4, 1200-1599 up to 8, 1600 up 10; the wanted layout stays the wish, default 2', () => {
  const { Q } = quadWorld();
  const f = (...a) => plain(Q.layoutFor(...a));
  for (const width of [320, 390, 599, 600, 768, 839]) {
    const r = f(width, 4);
    assert.deepEqual([r.n, r.cols, r.rows, r.oneUp, r.forced, r.capped, r.cap], [1, 1, 1, true, true, true, 1], `${width}: one-up whatever was asked for`);
  }
  assert.deepEqual(f(840, 2), { n: 2, cols: 2, rows: 1, oneUp: false, forced: false, capped: false, want: 2, cap: 4, why: '', reason: '' });
  assert.deepEqual([f(1280, 4).n, f(1280, 4).cols, f(1280, 4).rows], [4, 2, 2]);
  assert.deepEqual(plain(f(1280, 1)), { n: 1, cols: 1, rows: 1, oneUp: true, forced: false, capped: false, want: 1, cap: 8, why: '', reason: '' }, 'a chosen 1 is one tile with the chip switcher, but not forced');
  assert.equal(f(1280, undefined).n, 2, 'the default is 2-up');
  assert.equal(f(1280, 3).n, 2, 'a layout that does not exist is the default');
  assert.equal(f(Number.NaN, 4).n, 1, 'an unknown width is one-up');
  // the column map: 1 -> 1, 2 -> 2, 4 -> 2 x 2, 6 -> 3 x 2, 8 -> 4 x 2, 10 -> 5 x 2
  const map = [1, 2, 4, 6, 8, 10].map((n) => { const r = f(1920, n); return [r.n, r.cols, r.rows]; });
  assert.deepEqual(map, [[1, 1, 1], [2, 2, 1], [4, 2, 2], [6, 3, 2], [8, 4, 2], [10, 5, 2]]);
  // the caps by window width
  assert.deepEqual([839, 840, 1199, 1200, 1599, 1600, 3840].map((w) => Q.capFor(w)), [1, 4, 4, 8, 8, 10, 10]);
  assert.deepEqual([840, 1024, 1199].map((w) => f(w, 10).n), [4, 4, 4], '840-1199: up to 4');
  assert.deepEqual([1200, 1440, 1599].map((w) => f(w, 10).n), [8, 8, 8], '1200-1599: up to 8');
  assert.deepEqual([1600, 1920].map((w) => f(w, 10).n), [10, 10], '1600 and up: 10');
  assert.deepEqual([1024, 1280].map((w) => f(w, 6).n), [4, 6], 'six needs 1200');
  const capped = f(1280, 10);
  assert.deepEqual([capped.n, capped.capped, capped.want, capped.cap, capped.why], [8, true, 10, 8, 'width'], 'a saved layout above the cap shows the cap');
  assert.equal(capped.reason, '10 tiles need a window 1600 px wide or more: showing 8');
  assert.equal(f(1920, 10).reason, '', 'nothing to say when it fits');
});

test('layoutFor: a tile is never narrower than 240 px; a layout that cannot fit gives way to the next one that does, and says why in plain words', () => {
  const { Q } = quadWorld();
  const f = (...a) => plain(Q.layoutFor(...a));
  assert.deepEqual([Q.maxCols(487), Q.maxCols(488), Q.maxCols(736), Q.maxCols(984), Q.maxCols(1232), Q.maxCols(100), Q.maxCols(0)], [1, 2, 3, 4, 5, 1, Infinity], '240 px tiles and 8 px gaps: 488, 736, 984, 1232');
  assert.deepEqual([1232, 1231].map((a) => f(1920, 10, a).n), [10, 8], 'five columns need 1232 px of column');
  assert.deepEqual([984, 983].map((a) => f(1920, 8, a).n), [8, 6], 'four columns need 984');
  assert.deepEqual([736, 735].map((a) => f(1920, 6, a).n), [6, 4], 'three columns need 736; the next layout that fits is the 2 x 2 (two columns)');
  assert.deepEqual([488, 487].map((a) => f(1920, 4, a).n), [4, 1], 'two columns need 488; under that one tile');
  const squeezed = f(1920, 10, 900);
  assert.deepEqual([squeezed.n, squeezed.cols, squeezed.why, squeezed.cap], [6, 3, 'room', 6]);
  assert.equal(squeezed.reason, '10 tiles would be narrower than 240 px here: showing 6');
  const one = f(1280, 2, 400);
  assert.deepEqual([one.n, one.oneUp, one.forced, one.capped], [1, true, true, true], 'a column that holds one 240 px tile is the chip switcher');
  assert.equal(f(1280, 1, 400).forced, false, 'one tile needs no columns');
  assert.equal(f(1280, 2, 525).n, 2, 'the 260 px sidebar and a dock open at 1024: 525 px still holds two 240 px tiles');
  assert.equal(f(1280, 10, 0).n, 8, 'an unknown column skips the rule');
  assert.equal(Q.fitLayout(10, 3), 6);
  assert.equal(Q.fitLayout(8, 1), 1);
  assert.equal(Q.fitLayout(2, 2), 2);
});

// ---------------------------------------------------------------- the query, the saved state

test('parseQuery keeps only what is valid: names, 1|2|4|6|8|10, a project word; ten slots at most, no repeats', () => {
  const { Q } = quadWorld();
  const p = (q) => plain(Q.parseQuery(q));
  assert.deepEqual(p({ s: `${CK},${P3},,${P2}`, l: '4', p: 'petroit' }), { slots: pad(CK, P3, '', P2), layout: 4, project: 'petroit', any: true });
  assert.deepEqual(p({}), { slots: null, layout: null, project: null, any: false });
  assert.deepEqual(p({ s: `bad name,${CK},${CK},a--b--c,x--y--z,${P1}`, l: '3', p: 'no/pe' }), { slots: pad('', CK, '', 'a--b--c', 'x--y--z', P1), layout: null, project: null, any: true }, 'garbage and repeats become empty slots');
  const twelve = Array.from({ length: 12 }, (_, i) => `a--b--s${i}`);
  assert.deepEqual(p({ s: twelve.join(',') }).slots, twelve.slice(0, 10), 'only ten are read');
  for (const l of ['6', '8', '10']) assert.equal(p({ l }).layout, Number(l), `l=${l} is a layout`);
  assert.equal(p({ l: '12' }).layout, null);
  assert.equal(p({ l: '5' }).layout, null);
  assert.deepEqual(p({ s: 'nonsense', l: 'x' }), { slots: null, layout: null, project: null, any: false }, 'a list with no valid name is no list');
  assert.equal(p({ l: '1' }).layout, 1);
  assert.equal(p({ l: '2.0' }).layout, 2, 'Number() reads 2.0 as 2');
});

test('buildQuery: s up to the last filled slot, l always, p only in a project; the hash is the router\'s', () => {
  const { w, Q } = quadWorld();
  assert.deepEqual(plain(Q.buildQuery({ slots: [CK, '', P3, ''], layout: 4, project: '' })), { s: `${CK},,${P3}`, l: '4' });
  assert.deepEqual(plain(Q.buildQuery({ slots: ['', '', '', ''], layout: 2, project: 'shop' })), { l: '2', p: 'shop' });
  assert.deepEqual(plain(Q.buildQuery({ slots: [], layout: 9 })), { l: '2' }, 'a layout that does not exist is the default');
  assert.equal(Q.hashFor({ slots: ['a--b--c', 'd--e--f'], layout: 2, project: 'shop' }), '#/quad?s=a--b--c%2Cd--e--f&l=2&p=shop');
  assert.deepEqual(plain(w.get('parseHash')(Q.hashFor({ slots: [CK, '', P3], layout: 4, project: 'petroit' }))).query, { s: `${CK},,${P3}`, l: '4', p: 'petroit' }, 'parseHash reads it back');
});

test('load and save: a round trip per scope, garbage reads as nothing, modes and zoom are checked', () => {
  const { w, Q } = quadWorld();
  assert.equal(Q.load(''), null, 'nothing saved');
  Q.save('', { layout: 4, slots: [CK, 'junk', P3], modes: { [CK]: 'full', [P3]: 'bogus', 'x': 'ro' }, zoom: 1 });
  assert.equal(w.localStorage.getItem('ccboard:quad:all') !== null, true);
  assert.deepEqual(plain(Q.load('')), { layout: 4, slots: pad(CK, '', P3), modes: { [CK]: 'full' }, zoom: 1, composers: {} });
  assert.equal(Q.load('petroit'), null, 'another scope has its own key');
  Q.save('petroit', { layout: 1, slots: [P3], modes: {}, zoom: null });
  assert.ok(w.localStorage.getItem('ccboard:quad:petroit'));
  w.localStorage.setItem('ccboard:quad:all', '{not json');
  assert.equal(Q.load(''), null);
  w.localStorage.setItem('ccboard:quad:all', JSON.stringify({ layout: 3, slots: 'x', modes: [], zoom: 10 }));
  assert.deepEqual(plain(Q.load('')), { layout: 2, slots: pad(), modes: {}, zoom: null, composers: {} }, 'ten slots: zoom 0..9');
  w.localStorage.setItem('ccboard:quad:all', JSON.stringify({ layout: 10, slots: [], modes: {}, zoom: 9 }));
  assert.deepEqual([Q.load('').layout, Q.load('').zoom], [10, 9], 'layout 10 and a zoom on the tenth tile are valid');
  const many = {};
  for (let i = 0; i < 40; i++) many[`a--b--s${i}`] = 'ro';
  Q.save('', { layout: 2, slots: [], modes: many, zoom: null });
  assert.equal(Object.keys(Q.load('').modes).length, 24, 'the remembered modes are capped, the newest stay');
  assert.ok(Q.load('').modes['a--b--s39']);
  // the per-tile docked-composer flag rides with the modes: only true values of valid names, capped the same way
  Q.save('', { layout: 2, slots: [], modes: {}, zoom: null, composers: { [CK]: true, [P3]: false, 'bad name': true, [P2]: 'yes' } });
  assert.deepEqual(plain(Q.load('').composers), { [CK]: true });
  const flags = {};
  for (let i = 0; i < 40; i++) flags[`a--b--s${i}`] = true;
  Q.save('', { layout: 2, slots: [], modes: {}, zoom: null, composers: flags });
  assert.equal(Object.keys(Q.load('').composers).length, 24, 'the flags are capped like the modes, the newest stay');
  assert.ok(Q.load('').composers['a--b--s39']);
  Q.save('', { layout: 2, slots: [] });
  assert.deepEqual(plain(Q.load('').composers), {}, 'a state saved without the field has none');
});

// ---------------------------------------------------------------- who goes where

test('autoFill: needs-you first (a permission, then waiting, then needs an ack, the longest waiting first), then working, then the most recent; ended never', () => {
  const { w, Q } = quadWorld();
  const st = w.get('state');
  assert.deepEqual(plain(Q.autoFill(st, 8)), [CK, P3, P2, P1, CX, WT, S1], 'ended sessions are not offered');
  assert.deepEqual(plain(Q.autoFill(st, 2)), [CK, P3]);
  assert.deepEqual(plain(Q.autoFill(st, 4, { exclude: [CK, P2] })), [P3, P1, CX, WT]);
  assert.deepEqual(plain(Q.autoFill(st, 4, { project: 'petroit' })), [P3, P2, P1]);
  assert.deepEqual(plain(Q.autoFill(st, 0)), []);
  assert.deepEqual(plain(Q.autoFill(null, 4)), []);
  const none = fakeState({ projects: projectsOf({ a: { b: [sess('a', 'b', 's1', { state: 'idle', state_at: ISO(500) }), sess('a', 'b', 's2', { state: 'idle', state_at: ISO(5) }),
    sess('a', 'b', 'sh', { state: 'idle', agent: 'shell', launcher: 'shell', state_at: ISO(1) })] } }) });
  assert.deepEqual(plain(Q.autoFill(none, 3)), ['a--b--s2', 'a--b--s1', 'a--b--sh'], 'idle: the most recent first, a plain shell last');
});

test('slotsState: the URL over the saved state over auto-fill; a vanished saved session is dropped, a URL slot is kept; empty visible slots are filled', () => {
  const { w, Q } = quadWorld();
  const st = w.get('state');
  const run = (o) => plain(Q.slotsState({ project: '', ...o }));
  let r = run({ query: {}, saved: null, state: st });
  assert.deepEqual([r.layout, r.slots], [2, pad(CK, P3)], 'nothing saved, no URL: the default 2-up, filled by needs-you');
  r = run({ query: {}, saved: { layout: 4, slots: [WT, '', '', ''], modes: { [WT]: 'ro' }, zoom: 0, composers: { [WT]: true } }, state: st });
  assert.deepEqual([r.layout, r.slots, r.modes, r.zoom, r.composers], [4, pad(WT, CK, P3, P2), { [WT]: 'ro' }, 0, { [WT]: true }], 'saved: its layout, modes, zoom and composer flags; the empty slots fill around the kept one');
  r = run({ query: {}, saved: { layout: 10, slots: [], modes: {}, zoom: null }, state: st });
  assert.deepEqual(r.slots, [CK, P3, P2, P1, CX, WT, S1, '', '', ''], 'ten visible slots: every one is filled while there are sessions (the ended one never)');
  r = run({ query: { s: `${P1},${S1}`, l: '2' }, saved: { layout: 4, slots: [WT, CK, P3, P2], modes: {}, zoom: null }, state: st });
  assert.deepEqual([r.layout, r.slots], [2, pad(P1, S1)], 'the URL wins; slots 3 and 4 are not visible, so not filled');
  assert.deepEqual([r.fromUrl.slots, r.fromUrl.layout], [true, true]);
  r = run({ query: { l: '1' }, saved: { layout: 4, slots: [WT, CK, P3, P2], modes: {}, zoom: 2 }, state: st });
  assert.deepEqual([r.layout, r.slots[0], r.zoom], [1, WT, null], 'only the layout came from the URL; a zoom on a hidden slot is dropped');
  r = run({ query: {}, saved: { layout: 2, slots: ['gone--x--y', S1, '', ''], modes: {}, zoom: null }, state: st });
  assert.deepEqual(r.slots, pad(CK, S1), 'a session that is no longer there leaves its slot to auto-fill');
  r = run({ query: { s: 'gone--x--y' }, saved: null, state: st });
  assert.deepEqual(r.slots.slice(0, 2), ['gone--x--y', CK], 'a URL slot is kept even when its session is not running (the tile says so)');
  r = run({ query: {}, saved: null, state: st, project: 'petroit' });
  assert.deepEqual(r.slots.slice(0, 2), [P3, P2], 'inside a project only its sessions fill the slots');
  r = run({ query: { s: CK }, saved: null, state: null });
  assert.deepEqual(r.slots, pad(CK), 'before the first poll nothing is filled or dropped');
});

test('taskLine: the task title, else the last prompt; a waiting session shows what it asks (the tail of its last message)', () => {
  const { Q } = quadWorld();
  const t = (s) => plain(Q.taskLine(s));
  assert.deepEqual(t({ state: 'working', task: { title: 'Sync stock' }, last_prompt: 'p', last_message: 'm' }), { text: 'Sync stock', kind: 'task' });
  assert.deepEqual(t({ state: 'idle', last_prompt: '  fix\n the   login ', last_message: 'm' }), { text: 'fix the login', kind: 'prompt' });
  assert.deepEqual(t({ state: 'waiting', task: { title: 'Sync stock' }, last_message: 'Which one should it be?' }), { text: 'Which one should it be?', kind: 'ask' });
  assert.deepEqual(t({ state: 'waiting', last_prompt: 'p', last_message: '' }), { text: 'p', kind: 'prompt' }, 'waiting with nothing said falls back to the prompt');
  assert.deepEqual(t({ state: 'idle' }), { text: '', kind: '' });
  const long = t({ state: 'waiting', last_message: `${'x'.repeat(300)} the question?` }).text;
  assert.ok(long.startsWith('…') && long.endsWith('the question?') && long.length <= 241, 'a long ask keeps its end');
  assert.ok(t({ state: 'idle', last_prompt: 'y'.repeat(500) }).text.length <= 200);
});

test('sizeInfo and needsFit: the window or "cropped"; resize only for nobody-full, more than 2 columns off, inside the server\'s bounds', () => {
  const { Q } = quadWorld();
  const si = (...a) => plain(Q.sizeInfo(...a));
  assert.equal(si([45, 30], { cols: 45, rows: 30 }).text, '45x30');
  assert.equal(si([100, 30], { cols: 80, rows: 24 }).text, 'cropped');
  assert.equal(si([100, 30], { cols: 80, rows: 24 }).cropped, true);
  assert.equal(si([100, 30], { cols: 99, rows: 24 }).text, '100x30', 'within 2 columns is not cropped');
  assert.equal(si([100, 30], { cols: 120, rows: 24 }).text, '100x30', 'a tile wider than the window is not cropped');
  assert.equal(si(null, { cols: 80, rows: 24 }).text, '80x24', 'no window known: the tile\'s own size');
  assert.equal(si(null, null).text, '');
  const nf = (...a) => plain(Q.needsFit(...a));
  assert.deepEqual(nf({ full: 0, grid: 1 }, [100, 30], { cols: 80, rows: 24 }), { cols: 80, rows: 24 });
  assert.equal(nf({ full: 1 }, [100, 30], { cols: 80, rows: 24 }), null, 'a full client sets the size (409)');
  assert.equal(nf({ full: 0 }, [82, 30], { cols: 80, rows: 24 }), null, 'two columns off or less: leave it');
  assert.deepEqual(nf({ full: 0 }, [83, 30], { cols: 80, rows: 24 }), { cols: 80, rows: 24 });
  assert.equal(nf({ full: 0 }, [100, 30], { cols: 39, rows: 24 }), null, 'the server takes 40 to 400 columns');
  assert.equal(nf({ full: 0 }, [100, 30], { cols: 401, rows: 24 }), null);
  assert.equal(nf({ full: 0 }, [100, 30], { cols: 80, rows: 9 }), null, 'and 10 to 200 rows');
  assert.equal(nf({ full: 0 }, [100, 30], { cols: 80, rows: 201 }), null);
  assert.equal(nf({ full: 0 }, null, { cols: 80, rows: 24 }), null, 'no window known');
  assert.equal(nf({ full: 0 }, [100, 30], null), null, 'no terminal yet');
});

test('shortcutOf: Ctrl+Alt+1..9 and 0 (the tenth), Z, F, K and R by event.code (Option+digit types another character on a Mac); nothing else', () => {
  const { Q } = quadWorld();
  const s = (e) => plain(Q.shortcutOf({ ctrlKey: true, altKey: true, metaKey: false, shiftKey: false, ...e }));
  assert.deepEqual(s({ code: 'Digit3', key: '£' }), { act: 'focus', n: 3 });
  assert.deepEqual(s({ key: '2' }), { act: 'focus', n: 2 });
  assert.deepEqual(s({ code: 'KeyZ', key: 'Ω' }), { act: 'zoom' });
  assert.deepEqual(s({ code: 'KeyK', key: 'z' }), { act: 'attention' }, 'the code decides, not the character');
  assert.deepEqual(s({ key: 'R' }), { act: 'reload' });
  assert.deepEqual(s({ code: 'Digit5' }), { act: 'focus', n: 5 });
  assert.deepEqual(s({ code: 'Digit9', key: 'ª' }), { act: 'focus', n: 9 });
  assert.deepEqual(s({ code: 'Digit0', key: 'º' }), { act: 'focus', n: 10 }, '0 is the tenth tile');
  assert.deepEqual(s({ key: '0' }), { act: 'focus', n: 10 });
  assert.deepEqual(s({ code: 'Numpad7' }), { act: 'focus', n: 7 });
  assert.deepEqual(s({ code: 'KeyF', key: 'ƒ' }), { act: 'fullscreen' }, 'Ctrl+Alt+F works inside a terminal, where a plain f is typed');
  assert.deepEqual(s({ key: 'F' }), { act: 'fullscreen' });
  assert.equal(s({ code: 'KeyZ', ctrlKey: false }), null);
  assert.equal(s({ code: 'KeyZ', altKey: false }), null);
  assert.equal(s({ code: 'KeyZ', metaKey: true }), null);
  assert.equal(s({ code: 'KeyZ', shiftKey: true }), null);
  assert.equal(Q.shortcutOf(null), null);
  assert.equal(s({ code: 'Digit2', getModifierState: (m) => m === 'AltGraph' }), null, 'AltGr types ~ { # on some layouts: not a shortcut');
  assert.deepEqual(s({ code: 'Digit2', getModifierState: () => false }), { act: 'focus', n: 2 });
  assert.equal(s({ code: 'KeyZ', isComposing: true }), null, 'not in the middle of an IME composition');
});

test('attentionNext: the next session that needs you, after the one given, wrapping round; "" when nothing does', () => {
  const { w, Q } = quadWorld();
  const st = w.get('state');
  assert.equal(Q.attentionNext(st, '', ''), CK);
  assert.equal(Q.attentionNext(st, '', CK), P3);
  assert.equal(Q.attentionNext(st, '', P3), P2);
  assert.equal(Q.attentionNext(st, '', P1), CK, 'it wraps');
  assert.equal(Q.attentionNext(st, '', S1), CK, 'a session that does not need you starts from the top');
  assert.equal(Q.attentionNext(st, 'petroit', ''), P3, 'inside a project only its sessions count');
  assert.equal(Q.attentionNext(fakeState({ projects: projectsOf({ a: { b: [sess('a', 'b', 's1')] } }) }), '', ''), '');
});

// ---------------------------------------------------------------- the page: tiles

test('#/quad mounts the real page: two tiles by default, needs-you first, each a keyed article with the contract attributes and the exact grid ttyUrl', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  assert.equal(w.document.body.getAttribute('data-page') ?? 'quad', 'quad');
  assert.deepEqual(names(w), [CK, P3]);
  assert.deepEqual(slotsOf(w), { [CK]: '0', [P3]: '1' });
  const t = tileOf(w, CK);
  assert.equal(t.tagName, 'ARTICLE');
  assert.equal(t.getAttribute('data-mode'), 'grid');
  assert.equal(t.getAttribute('data-drop'), 'session', 'dnd.js can drop a backlog task on it');
  assert.equal(t.getAttribute('data-state'), 'waiting');
  assert.equal(t.getAttribute('data-agent'), 'claude');
  assert.equal(frameOf(w, CK).src, GRID(CK), 'the exact grid URL: canvas renderer, font 11, no resize overlay, no reconnect');
  assert.equal(frameOf(w, P3).src, GRID(P3));
  assert.equal(page(w).querySelectorAll('iframe').length, 2, 'one iframe per tile');
  assert.equal(page(w).querySelector('.quad').getAttribute('data-layout'), '2');
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-layout'), '2');
  assert.equal(page(w).querySelector('.quad').getAttribute('data-oneup'), null);
  assert.match(w.document.title, /^Quad · ccboard$/);
});

test('a tile header: state and agent glyph, project/repo · session, the four modes with the current one pressed, ctx %, zoom, open, reconnect, close', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  const t = tileOf(w, CK);
  assert.equal(text(t.querySelector('.qt-name')), 'phasezero/website · t-checkout-redesign');
  assert.equal(text(t.querySelector('.qt-where')), 'phasezero/website · ', 'the project and repo shrink first');
  assert.equal(text(t.querySelector('.qt-sess')), 't-checkout-redesign', 'the session name stays');
  assert.equal(text(tileOf(w, P3).querySelector('.qt-where')), 'petroit/api · ');
  assert.equal(t.querySelector('.qt-title').getAttribute('title'), CK + ': swap, reconnect, close');
  assert.ok(t.querySelector('.qt-glyphs .glyph.waiting'));
  assert.equal(t.querySelector('.qt-glyphs .glyph.agent').getAttribute('aria-label'), 'claude');
  assert.ok(t.querySelector('.qt-glyphs .glyph.agent').classList.contains('hue-violet'), 'the agent glyph wears its muted hue (chipHue)');
  assert.deepEqual(t.querySelectorAll('.qt-mode').map((b) => [b.getAttribute('data-mode'), b.getAttribute('aria-pressed')]),
    [['grid', 'true'], ['full', 'false'], ['ro', 'false'], ['tail', 'false']]);
  assert.equal(t.querySelector('.qt-pill'), null, 'no cycling pill any more: a segmented control, or a menu button when narrow');
  assert.equal(text(t.querySelector('.qt-modemenu .qt-modelabel')), 'grid', 'the narrow tile\'s menu button names the current mode');
  assert.equal(text(t.querySelector('.qt-ctx')), 'ctx 61%', 'the same reading as the dock pane (TermKit.ctxInfo)');
  assert.equal(t.querySelector('.qt-open').getAttribute('href'), `/term/${CK}`);
  assert.equal(t.querySelector('.qt-open').getAttribute('target'), '_blank');
  assert.equal(t.querySelector('.qt-open').getAttribute('data-dock'), 'skip', 'the tile\'s open link pops the terminal out: the dock never takes it');
  for (const sel of ['.qt-zoom', '.qt-reconnect', '.qt-close']) {
    const b = t.querySelector(sel);
    assert.ok(b.getAttribute('aria-label') && b.getAttribute('title'), `${sel} has a label and a title (icon only)`);
  }
  const cx = tileOf(w, P3);
  assert.ok(cx.classList.contains('needs'));
  const idle = quadWorld({ hash: `#/quad?s=${CX}` });
  assert.equal(tileOf(idle.w, CX).querySelector('.qt-glyphs .glyph.agent').getAttribute('aria-label'), 'codex');
  assert.ok(tileOf(idle.w, CX).querySelector('.qt-glyphs .glyph.agent').classList.contains('hue-teal'));
  assert.equal(tileOf(idle.w, CX).classList.contains('needs'), false);
});

test('the task line and the permission line: the task title, else the prompt, the ask while waiting; Allow, Deny and In terminal for a pending permission', () => {
  const { w } = quadWorld({ hash: `#/quad?s=${WT},${CK},${P3},${S1}&l=4` });
  assert.equal(text(tileOf(w, WT).querySelector('.qt-task')), 'Sync variant stock when an order is cancelled', 'the task title');
  assert.equal(text(tileOf(w, S1).querySelector('.qt-task')), 'fix the login bug', 'no task: the last prompt');
  assert.equal(text(tileOf(w, P3).querySelector('.qt-task')), 'Both paginations work. Which one should GET /devices expose?', 'waiting: what it asks');
  assert.equal(tileOf(w, P3).querySelector('.qt-task').getAttribute('data-kind'), 'ask');
  assert.ok(tileOf(w, WT).querySelector('.qt-perm').classList.contains('hidden'), 'no permission pending: no line');
  const perm = tileOf(w, CK).querySelector('.qt-perm');
  assert.equal(perm.classList.contains('hidden'), false);
  assert.equal(text(perm.querySelector('.qt-perm-text')), 'Bash: npm test');
  assert.deepEqual(perm.querySelectorAll('button').map(text), ['Allow', 'Deny', 'In terminal']);
  assert.ok(perm.querySelector('.qt-allow').classList.contains('primary') || perm.querySelector('.qt-allow').className.includes('primary'), 'Allow is primary (tinted)');
  assert.ok(perm.querySelector('.qt-allow').className.includes('tinted'));
  assert.ok(perm.querySelector('.qt-deny').className.includes('danger'));
  assert.ok(perm.querySelector('.qt-tui').className.includes('minimal'));
});

test('Allow, Deny and In terminal post the decision, the line goes at once and a failure brings it back with a toast', async () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const perm = () => tileOf(w, CK).querySelector('.qt-perm');
  perm().querySelector('.qt-allow').click();
  assert.equal(perm().classList.contains('hidden'), true, 'optimistic: the line is gone before the answer');
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(posts(w, '/api/permission')[0], { method: 'POST', path: '/api/permission/12/allow' });
  setState(env, w.get('state'));
  assert.equal(perm().classList.contains('hidden'), true, 'the next poll still lists it: it stays hidden until it is really gone');
  const env2 = quadWorld({ hash: '#/quad' });
  tileOf(env2.w, CK).querySelector('.qt-deny').click();
  tileOf(env2.w, CK).querySelector('.qt-tui');
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(posts(env2.w, '/api/permission')[0], { method: 'POST', path: '/api/permission/12/deny' });
  const env3 = quadWorld({ hash: '#/quad' });
  tileOf(env3.w, CK).querySelector('.qt-tui').click();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(posts(env3.w, '/api/permission')[0], { method: 'POST', path: '/api/permission/12/tui' }, 'In terminal is the tui decision');
  const env4 = quadWorld({ hash: '#/quad' });
  env4.w.ctx.__fail = { '/api/permission': { message: 'already decided: allow', status: 409 } };
  tileOf(env4.w, CK).querySelector('.qt-allow').click();
  await new Promise((r) => setImmediate(r));
  assert.equal(tileOf(env4.w, CK).querySelector('.qt-perm').classList.contains('hidden'), false, 'a refused decision shows the line again');
  assert.equal(toasts(env4.w).at(-1).text, 'already decided: allow');
});

test('a pending permission always has Allow, Deny and In terminal; a full client of someone else only adds the note (the tile\'s own full client does not count)', async () => {
  const st = fixtureState();
  const ck = st.projects.find((p) => p.name === 'phasezero').repos.find((r) => r.name === 'website').sessions[0];
  ck.viewers = { full: 1, grid: 0, ro: 0 };
  const { w } = quadWorld({ state: st, hash: '#/quad' });
  const perm = tileOf(w, CK).querySelector('.qt-perm');
  assert.deepEqual(perm.querySelectorAll('button').map(text), ['Allow', 'Deny', 'In terminal'], 'a request still in pending_permissions can be allowed from the tile');
  assert.match(text(perm), /a terminal is attached/, 'a grid tile has no client of its own: the one full viewer is someone else');
  assert.equal(text(perm.querySelector('.qt-perm-sum')), 'Bash: npm test');
  assert.match(perm.querySelector('.qt-perm-text').getAttribute('title'), /^Bash: npm test/, 'the whole summary is the tooltip');
  perm.querySelector('.qt-allow').click();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(posts(w, '/api/permission')[0], { method: 'POST', path: '/api/permission/12/allow' });
  // a full-mode tile (one-up on a phone) is itself the full client: 1 viewer is its own, so no note and the same three buttons
  const own = quadWorld({ state: st, hash: `#/quad?s=${CK}`, wide: false });
  const mine = tileOf(own.w, CK);
  assert.equal(mine.getAttribute('data-mode'), 'full');
  assert.deepEqual(mine.querySelector('.qt-perm').querySelectorAll('button').map(text), ['Allow', 'Deny', 'In terminal']);
  assert.doesNotMatch(text(mine.querySelector('.qt-perm')), /a terminal is attached/);
  ck.viewers = { full: 2, grid: 0, ro: 0 };
  setState(own, st);
  assert.match(text(tileOf(own.w, CK).querySelector('.qt-perm')), /a terminal is attached/, 'a second full client is someone else\'s');
  assert.deepEqual(tileOf(own.w, CK).querySelector('.qt-perm').querySelectorAll('button').map(text), ['Allow', 'Deny', 'In terminal']);
});

// ---------------------------------------------------------------- v0.5.9 review fixes: modes, the board's needs-you list, the key bar, the ctx chip

test('defaultMode / modeOf depend on "forced one-up" only: grid, except where the window leaves one tile (a phone, a narrow column)', () => {
  const { Q } = quadWorld();
  assert.equal(Q.defaultMode(false), 'grid');
  assert.equal(Q.defaultMode(true), 'full');
  assert.equal(Q.modeOf({}, CK, false), 'grid');
  assert.equal(Q.modeOf({}, CK, true), 'full');
  assert.equal(Q.modeOf({ [CK]: 'ro' }, CK, true), 'ro', 'a chosen mode wins in every window');
  assert.equal(Q.modeOf({ [CK]: 'bogus' }, CK, false), 'grid');
});

test('picking 1, 2 or 4 never changes a tile\'s mode or touches its iframe: the same node, the same src, no src assignment at all', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  const Q = () => w.get('Quad').current;
  const f = frameOf(w, CK);
  const log = [];
  spy(f, log);
  for (const n of [1, 4, 2, 1, 2, 4, 1]) {
    Q().setLayout(n);
    assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'grid', `layout ${n}: still the grid`);
    assert.equal(frameOf(w, CK), f, `layout ${n}: the same iframe node`);
    assert.equal(f.src, GRID(CK), `layout ${n}: the same address`);
  }
  assert.deepEqual(log, [], 'not one src assignment, not one remove(): nothing reloaded');
  assert.equal(savedOf(w).modes[CK], undefined, 'and nothing was saved as a choice');
  Q().setMode(CK, 'full');
  const logFull = [];
  spy(f, logFull);
  Q().setLayout(2);
  Q().setLayout(1);
  assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'full', 'a chosen mode survives every layout');
  assert.deepEqual(logFull, []);
});

test('a tile created in a chosen 1-up layout on a wide window is a grid like any other (full is only for the window that forces one tile); on a phone the same layout is a full terminal', () => {
  const wide = quadWorld({ hash: '#/quad?l=1' });
  assert.equal(page(wide.w).querySelector('.quad').getAttribute('data-layout'), '1');
  assert.equal(page(wide.w).querySelector('.quad').getAttribute('data-forced'), null);
  assert.equal(tileOf(wide.w, CK).getAttribute('data-mode'), 'grid');
  assert.equal(frameOf(wide.w, CK).src, GRID(CK), 'the grid address: no writable client that sizes the session');
  const phone = quadWorld({ wide: false, hash: '#/quad?l=1' });
  assert.equal(page(phone.w).querySelector('.quad').getAttribute('data-forced'), 'true');
  assert.equal(tileOf(phone.w, CK).getAttribute('data-mode'), 'full');
  const grown = quadWorld({ hash: '#/quad' });
  grown.w.get('Quad').current.setLayout(1);
  grown.w.get('Quad').current.setLayout(4);
  assert.deepEqual(tiles(grown.w).map((t) => t.getAttribute('data-mode')), ['grid', 'grid', 'grid', 'grid'], 'tiles that joined later are grids too');
});

test('only the window going to or from "forced one-up" re-derives a default mode; a chosen mode is left alone in both directions', () => {
  let isWide = true;
  const env = quadWorld({ hash: '#/quad', storage: { 'ccboard:quad:all': { layout: 2, slots: [CK, P3, '', ''], modes: { [P3]: 'ro' }, zoom: null } } });
  const { w } = env;
  w.run("globalThis.matchMedia = (q) => ({ matches: __wide.value && /840/.test(q), addEventListener() {}, removeEventListener() {} });");
  w.ctx.__wide = { get value() { return isWide; } };
  assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'grid');
  assert.equal(tileOf(w, P3).getAttribute('data-mode'), 'ro');
  isWide = false;
  w.fire('resize');
  assert.deepEqual(names(w), [CK], 'one-up: the first tile');
  assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'full', 'the phone one-up default is a full terminal');
  assert.equal(frameOf(w, CK).src, '/tty/?arg=' + CK);
  isWide = true;
  w.fire('resize');
  assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'grid', 'back to the default grid');
  assert.equal(tileOf(w, P3).getAttribute('data-mode'), 'ro', 'the chosen mode came back as it was');
});

test('needs you is the board\'s own list (Inbox.items) when it is loaded: its members first in its order, cached per state object, the same set the sidebar counts', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const Q = w.get('Quad');
  w.run(`globalThis.__inboxCalls = 0; globalThis.__inbox = ['${S1}', '${CK}'];
    globalThis.Inbox = { items: (st) => { __inboxCalls += 1; return __inbox.map((tmux) => ({ tmux, kind: 'needs' })); } };`);
  const st = w.get('state');
  const n0 = w.get('__inboxCalls');
  assert.deepEqual(plain(Q.autoFill(st, 3)).slice(0, 2), [S1, CK], 'the inbox order, not the permission-first fallback');
  assert.equal(Q.needsYou({ tmux: S1 }, st), true, 'a blocked-job session the old rule missed');
  assert.equal(Q.needsYou({ tmux: P3, state: 'waiting' }, st), false, 'a waiting session the inbox does not list (acknowledged) is not needs-you here either');
  assert.equal(Q.attentionNext(st, '', ''), S1);
  assert.equal(Q.attentionNext(st, '', S1), CK);
  assert.equal(Q.attentionNext(st, '', CK), S1, 'it wraps');
  assert.equal(w.get('__inboxCalls') - n0, 1, 'one Inbox pass for this state object, however often the quad asks');
  const next = JSON.parse(JSON.stringify(st));
  assert.deepEqual(plain(Q.autoFill(next, 1)), [S1]);
  assert.equal(w.get('__inboxCalls') - n0, 2, 'a new state object is a new pass');
  w.run('Inbox.items = () => { throw new Error("boom"); }');
  assert.deepEqual(plain(Q.autoFill(JSON.parse(JSON.stringify(st)), 2)), [CK, P3], 'an inbox that throws leaves the quad on its own rule');
});

test('with the real inbox.js loaded the quad\'s needs-you is exactly Inbox.items: a session held by a blocked job counts, an acknowledged waiting one does not', () => {
  const st = fixtureState();                                   // ccboard/s1 is idle, but a blocked background job waits on you: the board lists it, the old quad rule did not
  const p3 = st.projects.find((p) => p.name === 'petroit').repos[0].sessions.find((x) => x.tmux === P3);
  p3.needs_attention = false;                                  // waiting, but acknowledged: the inbox drops it
  const { w } = quadWorld({ state: st, inbox: true, hash: `#/quad?s=${S1},${P3}` });
  const Q = w.get('Quad');
  const sw = w.get('state');
  const items = plain(w.run('Inbox.items(state).map((i) => i.tmux)'));
  assert.ok(items.includes(S1) && !items.includes(P3), 'the premise: the inbox lists the job-blocked session and not the acknowledged one');
  const mine = plain(Q.candidates(sw, '').filter((x) => Q.needsYou(x, sw)).map((x) => x.tmux));
  assert.deepEqual(mine, items, 'the same members in the same order, nobody missing, nobody added');
  assert.deepEqual(plain(Q.autoFill(sw, items.length)), items, 'auto-fill leads with them');
  assert.equal(Q.attentionNext(sw, '', ''), items[0]);
  assert.equal(tileOf(w, S1).classList.contains('needs'), true, 'the tile of the job-blocked session is marked');
  assert.equal(tileOf(w, P3).classList.contains('needs'), false, 'the acknowledged one is not');
});

test('the host key bar in 4-up stays hidden until a tile has really been used (a press, a key in a terminal, a shortcut); ttyd focusing itself on load is not use', () => {
  const env = quadWorld({ hash: `#/quad?s=${S1},${P3},${CX},${WT}&l=4` });
  const { w } = env;
  const keys = () => page(w).querySelector('.q-keys');
  assert.equal(keys().classList.contains('hidden'), true, 'four tiles: the bar waits');
  assert.equal(w.get('Quad').current.active !== '', true, 'the default tile is active, which is not use');
  const f = load(w, P3);
  f.contentWindow.listeners.focusin[0].fn({});
  assert.equal(keys().classList.contains('hidden'), true, 'ttyd\'s own focus() on load is a focusin: not the person');
  tileOf(w, CX).dispatchEvent({ type: 'pointerdown' });
  assert.equal(keys().classList.contains('hidden'), false, 'a press on a tile: the bar shows');
  const again = quadWorld({ hash: `#/quad?s=${S1},${P3},${CX},${WT}&l=4` });
  const f2 = load(again.w, WT);
  f2.contentWindow.listeners.keydown[0].fn({});
  assert.equal(page(again.w).querySelector('.q-keys').classList.contains('hidden'), false, 'a key typed into a terminal counts');
  const sc = quadWorld({ hash: `#/quad?s=${S1},${P3},${CX},${WT}&l=4` });
  sc.w.get('Quad').current.focusSlot(1);
  assert.equal(page(sc.w).querySelector('.q-keys').classList.contains('hidden'), false, 'so does Ctrl+Alt+2');
  const two = quadWorld({ hash: '#/quad' });
  assert.equal(page(two.w).querySelector('.q-keys').classList.contains('hidden'), true, '2-up with a mouse: hidden as before');
  const pref = quadWorld({ hash: `#/quad?s=${S1},${P3},${CX},${WT}&l=4`, storage: { 'ccboard:quad:keys': '1' } });
  assert.equal(page(pref.w).querySelector('.q-keys').classList.contains('hidden'), false, 'the person\'s own choice wins over the 4-up rule');
});

test('the touch tiers of the header hang on data-touch, set when the pointer is coarse (or html.force-coarse) and gone when it is not', () => {
  const mouse = quadWorld({ hash: '#/quad' });
  assert.equal(page(mouse.w).querySelector('.quad').getAttribute('data-touch'), null);
  const touch = quadWorld({ hash: '#/quad' });
  touch.w.document.documentElement.classList.add('force-coarse');
  touch.w.get('Quad').current.sync();
  assert.equal(page(touch.w).querySelector('.quad').getAttribute('data-touch'), 'true');
  touch.w.document.documentElement.classList.remove('force-coarse');
  touch.w.get('Quad').current.sync();
  assert.equal(page(touch.w).querySelector('.quad').getAttribute('data-touch'), null);
});

test('a tile that is resized (or whose pointer type changed) drops a sliver of its project/repo: TermKit.fitName is asked about its .qt-where, never about a collapsed tile', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w, ros } = env;
  w.run('globalThis.__fit = []; TermKit.fitName = (n) => { __fit.push(n.getAttribute("class")); return false; };');
  const ro = ros.find((r) => r.nodes.some((n) => n.classList && n.classList.contains('qt-body')));
  ro.cb([{ contentRect: { width: 300, height: 200 } }]);
  assert.deepEqual(plain(w.get('__fit')), ['qt-where pend']);
  ro.cb([{ contentRect: { width: 0, height: 0 } }]);
  assert.equal(w.get('__fit').length, 1, 'a hidden or collapsed tile says nothing');
  w.document.documentElement.classList.add('force-coarse');
  w.get('Quad').current.sync();
  assert.equal(w.get('__fit').length, 3, 'the pointer type changed the header\'s pieces: both tiles look again');
});

test('the context chip reads like the dock\'s: "ctx 61%", no tint under 80, .hi from 80, .crit from 90 (one TermKit.ctxInfo for both)', () => {
  const T = quadWorld().w.get('TermKit');
  assert.deepEqual(plain(T.ctxInfo(61)), { pct: 61, text: 'ctx 61%', level: '', title: 'context window used: 61%' });
  assert.equal(T.ctxInfo(79.6).level, 'hi', '79.6 rounds to 80');
  assert.equal(T.ctxInfo(80).level, 'hi');
  assert.equal(T.ctxInfo(89).level, 'hi');
  assert.equal(T.ctxInfo(90).level, 'crit');
  assert.equal(T.ctxInfo(null), null);
  assert.equal(T.ctxInfo(undefined), null);
  assert.equal(T.ctxInfo('61'), null);
  assert.equal(T.ctxInfo(NaN), null);
  const st = fixtureState();
  const ck = st.projects.find((p) => p.name === 'phasezero').repos.find((r) => r.name === 'website').sessions[0];
  const env = quadWorld({ state: st, hash: '#/quad' });
  const chip = () => tileOf(env.w, CK).querySelector('.qt-ctx');
  const cls = () => ['hi', 'crit', 'hidden'].filter((c) => chip().classList.contains(c));
  assert.deepEqual([text(chip()), cls()], ['ctx 61%', []]);
  for (const [pct, want] of [[80, ['hi']], [89, ['hi']], [90, ['crit']], [97, ['crit']], [70, []]]) {
    ck.stats = { ...ck.stats, context_pct: pct };
    setState(env, JSON.parse(JSON.stringify(st)));
    assert.deepEqual([text(chip()), cls()], [`ctx ${pct}%`, want], `${pct}%`);
  }
  assert.equal(chip().getAttribute('title'), 'context window used: 70%');
  delete ck.stats.context_pct;
  setState(env, JSON.parse(JSON.stringify(st)));
  assert.deepEqual(cls(), ['hidden']);
});

// ---------------------------------------------------------------- frames are never re-created or reordered

test('growing 2 -> 4 mounts two more tiles and leaves the first two untouched; shrinking tears the last two down (about:blank, then remove)', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  const f0 = frameOf(w, CK);
  const f1 = frameOf(w, P3);
  const order = () => page(w).querySelector('.qgrid').children.map((n) => n.getAttribute('data-tmux') || 'empty');
  assert.deepEqual(order(), [CK, P3]);
  button(page(w).querySelector('.q-layout'), /^4$/).click();
  assert.deepEqual(names(w), [CK, P3, P2, P1]);
  assert.deepEqual(order(), [CK, P3, P2, P1], 'new tiles are appended; nothing is inserted before an iframe');
  assert.equal(frameOf(w, CK), f0, 'the same iframe node: never re-created');
  assert.equal(frameOf(w, P3), f1);
  assert.equal(f0.src, GRID(CK), 'and never navigated again');
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-layout'), '4');
  assert.deepEqual(slotsOf(w), { [CK]: '0', [P3]: '1', [P2]: '2', [P1]: '3' });
  const logs = { [P2]: [], [P1]: [] };
  for (const t of [P2, P1]) spy(frameOf(w, t), logs[t]);
  button(page(w).querySelector('.q-layout'), /^2$/).click();
  assert.deepEqual(names(w), [CK, P3]);
  assert.deepEqual(logs[P2], ['src:about:blank', 'remove'], 'the blank page first (the websocket closes), then the node');
  assert.deepEqual(logs[P1], ['src:about:blank', 'remove']);
  assert.equal(frameOf(w, CK), f0, 'the first two never moved');
  button(page(w).querySelector('.q-layout'), /^4$/).click();
  assert.deepEqual(names(w), [CK, P3, P2, P1], 'and remounted on growth');
  assert.notEqual(frameOf(w, P2), null);
});

test('swapping two slots moves no iframe: only data-slot changes; assigning a session to a slot replaces just that tile', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  const f0 = frameOf(w, CK);
  const f1 = frameOf(w, P3);
  const order = () => page(w).querySelector('.qgrid').children.map((n) => n.getAttribute('data-tmux'));
  const logs = [];
  spy(f0, logs);
  spy(f1, logs);
  w.get('Quad').current.assign(0, P3);
  assert.deepEqual(slotsOf(w), { [CK]: '1', [P3]: '0' }, 'a swap');
  assert.deepEqual(order(), [CK, P3], 'the DOM order is the creation order, whatever the slots say');
  assert.equal(frameOf(w, CK), f0);
  assert.equal(frameOf(w, P3), f1);
  assert.deepEqual(logs, [], 'no iframe was touched');
  w.get('Quad').current.assign(1, S1);
  assert.deepEqual(names(w), [P3, S1], 'CK left slot 1, S1 took it');
  assert.deepEqual(logs, ['src:about:blank', 'remove'], 'only the replaced tile was torn down');
  assert.equal(frameOf(w, P3), f1);
});

test('unmounting (a route change) tears every tile down: iframe.src = about:blank before remove, ResizeObserver off, the keys and Live unbound', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2}&l=4` });
  const { w, ros } = env;
  const logs = {};
  for (const t of [CK, P3, P2]) { logs[t] = []; spy(frameOf(w, t), logs[t]); }
  env.w.get('Quad').current.setMode(P2, 'tail');
  const before = w.run('Keymap.list.filter((b) => /^Quad/.test(b.help)).length');
  assert.equal(before, 5, 'five help-only entries while the page is up');
  assert.ok(live(w).sub.includes(P2), 'the tail subscribed');
  w.location.hash = '#/memory';
  for (const t of [CK, P3]) assert.deepEqual(logs[t], ['src:about:blank', 'remove'], `${t}: blank first, then remove`);
  assert.deepEqual(logs[P2], ['src:about:blank', 'remove'], 'the tile that went to tail had its iframe torn down on the switch');
  assert.ok(live(w).unsub.includes(P2), 'Live unsubscribed');
  assert.equal(page(w).querySelectorAll('iframe').length, 0);
  assert.equal(page(w).querySelectorAll('.qtile').length, 0);
  assert.ok(ros.length >= 4 && ros.every((r) => r.gone), 'every ResizeObserver disconnected (the page\'s and one per tile)');
  assert.equal(w.get('Quad').current, null);
  assert.equal(w.run('Keymap.list.filter((b) => /^Quad/.test(b.help)).length'), 0, 'the help entries left with the page');
  assert.equal(w.document.listeners.keydown ? w.document.listeners.keydown.length : 0, 0, 'no document keydown listener is left behind');
  const before2 = calls(w).length;
  env.clock.advance(5000);
  assert.equal(calls(w).length, before2, 'a timer that was still pending (TermKit\'s fit debounce) finds the iframes gone and does nothing');
  assert.equal(page(w).querySelectorAll('iframe').length, 0);
});

// ---------------------------------------------------------------- persistence and the URL

test('the first visit saves the resolved state and writes it into the address once the page has settled (no remount)', async () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w, clock } = env;
  assert.equal(w.location.hash, '#/quad', 'mount never writes the address');
  clock.advance(5);
  assert.equal(w.location.hash, `#/quad?s=${CK}%2C${P3}&l=2`, 'then the state is in the address: s up to the last slot, l always');
  const rep = plain(w.history.calls).filter((c) => c.method === 'replaceState');
  assert.equal(rep.length, 1, 'one replaceState, no pushState');
  assert.deepEqual(savedOf(w), { layout: 2, slots: pad(CK, P3), modes: {}, zoom: null, composers: {} });
  assert.equal(tileOf(w, CK).querySelector('iframe') !== null, true);
  assert.equal(w.get('Quad').current !== null, true);
  const mountsBefore = frameOf(w, CK);
  clock.advance(100);
  assert.equal(frameOf(w, CK), mountsBefore, 'the write-back did not remount the page');
});

test('the saved state is used when the URL says nothing; the URL wins over it and is written back to the saved state', () => {
  const saved = { layout: 4, slots: [WT, CX, S1, P1], modes: { [WT]: 'ro' }, zoom: null };
  const a = quadWorld({ hash: '#/quad', storage: { 'ccboard:quad:all': saved } });
  assert.deepEqual(names(a.w), [WT, CX, S1, P1], 'saved layout and slots');
  assert.equal(frameOf(a.w, WT).src, '/tty/?arg=' + WT + '&arg=ro', 'a saved explicit mode');
  assert.equal(frameOf(a.w, CX).src, GRID(CX), 'the rest default to grid');
  const b = quadWorld({ hash: `#/quad?s=${P3},${P2}&l=2`, storage: { 'ccboard:quad:all': saved } });
  assert.deepEqual(names(b.w), [P3, P2], 'the URL wins over the saved slots and layout');
  assert.deepEqual(savedOf(b.w).slots, pad(P3, P2), 'and the saved state follows');
  assert.equal(savedOf(b.w).layout, 2);
  assert.deepEqual(savedOf(b.w).modes, { [WT]: 'ro' }, 'the modes of the saved state are kept');
  const c = quadWorld({ hash: '#/quad?l=1', storage: { 'ccboard:quad:all': saved } });
  assert.deepEqual(names(c.w), [WT], 'only the layout came from the URL: the first saved slot shows');
});

test('a stale saved session is dropped, its slot refilled; a URL slot whose session is gone says so instead of mounting an iframe', () => {
  const a = quadWorld({ hash: '#/quad', storage: { 'ccboard:quad:all': { layout: 2, slots: ['old--x--y', S1, '', ''], modes: {}, zoom: null } } });
  assert.deepEqual(names(a.w), [CK, S1]);
  const b = quadWorld({ hash: `#/quad?s=old--x--y,${S1}` });
  assert.deepEqual(names(b.w), ['old--x--y', S1]);
  const t = tileOf(b.w, 'old--x--y');
  assert.ok(t.classList.contains('gone'));
  assert.equal(t.querySelector('iframe'), null, 'no dead iframe');
  assert.match(text(t.querySelector('.qt-note')), /not running any more/);
});

test('?p=<project> keeps its own saved scope and only its sessions fill the slots', () => {
  const env = quadWorld({ hash: '#/quad?p=petroit' });
  assert.deepEqual(names(env.w), [P3, P2]);
  env.clock.advance(5);
  assert.equal(env.w.location.hash, `#/quad?s=${P3}%2C${P2}&l=2&p=petroit`);
  assert.ok(savedOf(env.w, 'petroit'));
  assert.equal(savedOf(env.w, 'all'), null, 'the all-projects scope is untouched');
  assert.match(text(page(env.w).querySelector('.q-scope')), /petroit/);
  assert.equal(page(env.w).querySelector('.q-scope a').getAttribute('href'), '#/quad', 'a way back to all projects');
  assert.equal(env.w.document.title, 'Quad · petroit · ccboard', 'v0.5.9b: the title reads "Quad · <project>"');
});

test('a new address under the mounted page (a pasted link, another layout) is applied in place: kept tiles keep their iframes', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3}&l=2` });
  const { w } = env;
  env.clock.advance(5);
  const f = frameOf(w, CK);
  w.location.hash = `#/quad?s=${CK},${P3},${P2},${P1}&l=4`;
  assert.deepEqual(names(w), [CK, P3, P2, P1]);
  assert.equal(frameOf(w, CK), f, 'same iframe: the page took onRoute, not a remount');
  w.location.hash = `#/quad?s=${P1},${CK}&l=2`;
  assert.deepEqual(names(w), [CK, P1], 'the DOM keeps its order (CK was there first); the slots say where each one shows');
  assert.equal(frameOf(w, CK), f, 'CK only changed slot');
  assert.deepEqual(slotsOf(w), { [P1]: '0', [CK]: '1' });
  env.clock.advance(5);
  assert.equal(w.location.hash, `#/quad?s=${P1}%2C${CK}&l=2`, 'and the address is the same one, canonical');
  w.location.hash = '#/quad?p=petroit';
  assert.deepEqual(names(w), [P3, P2], 'another scope: its own tiles');
  assert.equal(page(w).querySelectorAll('iframe').length, 2);
  w.location.hash = '#/quad';
  assert.deepEqual(names(w).length, 2, 'back to the all-projects scope');
  assert.equal(page(w).querySelector('.q-scope-sel').value, '', 'v0.5.9b: the scope select is always there and says all projects');
  assert.equal(page(w).querySelector('.q-scope .q-all').classList.contains('hidden'), true, 'the way back to all projects is hidden while it is all projects');
});

// ---------------------------------------------------------------- zoom, modes, closing, picking

test('zoom is an overlay: the zoomed tile gets .zoomed, the others stay mounted and inert at the same size, no iframe is touched, nothing is refitted', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2},${P1}&l=4` });
  const { w } = env;
  const logs = [];
  const wins = {};
  for (const t of [CK, P3, P2, P1]) { wins[t] = load(w, t).contentWindow; spy(frameOf(w, t), logs); }
  const Q = w.get('Quad').current;
  tileOf(w, P3).querySelector('.qt-zoom').click();
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-zoom'), '1');
  assert.ok(tileOf(w, P3).classList.contains('zoomed'));
  assert.equal(tileOf(w, P3).querySelector('.qt-zoom').getAttribute('aria-pressed'), 'true');
  assert.ok(tileOf(w, P3).querySelector('.qt-zoom').children[0].classList.contains('bp5-icon-minimize'), 'the button now says minimize');
  assert.equal(tileOf(w, P3).querySelector('.qt-zoom').getAttribute('aria-label'), 'Back to the grid');
  assert.ok(tileOf(w, CK).querySelector('.qt-zoom').children[0].classList.contains('bp5-icon-maximize'));
  for (const t of [CK, P2, P1]) {
    assert.equal(tileOf(w, t).getAttribute('inert'), '', `${t} is inert under the overlay`);
    assert.equal(tileOf(w, t).classList.contains('zoomed'), false);
    assert.ok(frameOf(w, t), `${t} stays mounted`);
  }
  assert.equal(tileOf(w, P3).getAttribute('inert'), null);
  assert.deepEqual(logs, [], 'zoom assigned no src and removed nothing');
  env.clock.advance(1000);
  assert.deepEqual([CK, P2, P1].map((t) => wins[t].fits), [0, 0, 0], 'the other tiles were not refitted');
  assert.equal(page(w).querySelectorAll('.qtile[data-tmux]').length, 4);
  assert.deepEqual(savedOf(w).zoom, 1, 'the zoom is saved');
  tileOf(w, P3).querySelector('.qt-zoom').click();
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-zoom'), null);
  assert.equal(tileOf(w, CK).getAttribute('inert'), null);
  assert.deepEqual(logs, []);
  assert.equal(Q.zoomSlot(0), true);
  assert.equal(Q.zoomSlot(3), true, 'zooming another slot moves the overlay');
  assert.ok(tileOf(w, P1).classList.contains('zoomed'));
});

test('Ctrl+Alt+<n> while zoomed on another tile moves the overlay to that tile (a covered tile cannot take focus)', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2},${P1}&l=4` });
  const { w } = env;
  for (const t of [CK, P3, P2, P1]) load(w, t);
  const Q = w.get('Quad').current;
  Q.zoomSlot(0);
  w.document.dispatch('keydown', key({ code: 'Digit3' }));
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-zoom'), '2');
  assert.ok(tileOf(w, P2).classList.contains('zoomed'));
  assert.equal(tileOf(w, P2).getAttribute('inert'), null);
  assert.equal(Q.active, P2);
  assert.equal(frameOf(w, P2).contentWindow.focused, 1);
});

test('no zoom in one-up (nothing to zoom), the zoom button is hidden; a zoom on a slot that is closed is dropped', () => {
  const one = quadWorld({ wide: false, hash: '#/quad' });
  assert.equal(tileOf(one.w, CK).querySelector('.qt-zoom').classList.contains('hidden'), true);
  assert.equal(one.w.get('Quad').current.zoomSlot(0), false);
  const env = quadWorld({ hash: '#/quad' });
  const Q = env.w.get('Quad').current;
  Q.zoomSlot(1);
  tileOf(env.w, P3).querySelector('.qt-close').click();
  assert.equal(page(env.w).querySelector('.qgrid').getAttribute('data-zoom'), null);
});

test('the mode is a 4-way segmented control (and a menu button for narrow tiles); tail swaps the iframe for pre.tail fed by Live; the choice is saved and is the default of nobody else', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const seg = (m) => tileOf(w, CK).querySelector(`.qt-modes .qt-mode[data-mode=${m}]`);
  const f = frameOf(w, CK);
  assert.deepEqual(tileOf(w, CK).querySelectorAll('.qt-modes .seg-btn').map(text), ['grid', 'full', 'ro', 'tail'], 'one tap to any mode, all four visible');
  seg('full').click();
  assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'full');
  assert.equal(frameOf(w, CK), f, 'the same iframe, a new address');
  assert.equal(f.src, '/tty/?arg=' + CK, 'full: no grid arg, no quiet flags');
  assert.equal(text(tileOf(w, CK).querySelector('.qt-modelabel')), 'full');
  seg('ro').click();
  assert.equal(f.src, '/tty/?arg=' + CK + '&arg=ro');
  assert.equal(seg('ro').getAttribute('aria-pressed'), 'true');
  const log = [];
  spy(f, log);
  seg('tail').click();
  assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'tail');
  assert.deepEqual(log, ['src:about:blank', 'remove'], 'the iframe goes the safe way');
  assert.equal(tileOf(w, CK).querySelector('iframe'), null);
  const pre = tileOf(w, CK).querySelector('pre.tail');
  assert.ok(pre, 'a pre.tail');
  assert.ok(live(w).sub.includes(CK));
  w.get('__live').fns[CK](['one', 'two', 'three']);
  assert.equal(pre.textContent, 'one\ntwo\nthree');
  assert.deepEqual(savedOf(w).modes, { [CK]: 'tail' }, 'saved, for this session only');
  assert.equal(frameOf(w, P3).src, GRID(P3), 'the other tile is still a grid');
  seg('grid').click();
  assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'grid');
  assert.ok(live(w).unsub.includes(CK), 'leaving tail unsubscribes');
  assert.equal(tileOf(w, CK).querySelector('pre.tail'), null);
  assert.equal(frameOf(w, CK).src, GRID(CK), 'a new iframe on the grid address');
});

test('under 366 px the mode picker is a menu button: it lists all four modes with the current one ticked, and picking one is the same setMode as the segmented control', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  const btn = () => tileOf(w, CK).querySelector('.qt-modemenu');
  btn().click();
  const rows = w.document.querySelectorAll('.menuitem');
  assert.equal(rows.length, 4);
  assert.deepEqual(rows.map((r) => r.textContent.trim().split(':')[0]), ['Grid', 'Full', 'Read only', 'Tail']);
  assert.ok(rows[0].querySelector('.bp5-icon-tick') && !rows[1].querySelector('.bp5-icon-tick'), 'the current mode carries the tick');
  rows[1].click();
  assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'full');
  assert.equal(text(btn().querySelector('.qt-modelabel')), 'full');
  assert.equal(btn().getAttribute('aria-label'), 'View mode: full');
  assert.deepEqual(savedOf(w).modes, { [CK]: 'full' });
  assert.equal(tileOf(w, CK).querySelector('.qt-modes .qt-mode[data-mode=full]').getAttribute('aria-pressed'), 'true');
});

test('close empties the slot (an empty tile with a pick list); picking a session fills it; Auto-fill fills the empty ones; the iframe of a closed tile is torn down', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const log = [];
  spy(frameOf(w, P3), log);
  tileOf(w, P3).querySelector('.qt-close').click();
  assert.deepEqual(log, ['src:about:blank', 'remove']);
  assert.deepEqual(names(w), [CK]);
  const empty = page(w).querySelector('.qempty');
  assert.equal(empty.getAttribute('data-slot'), '1');
  const picks = empty.querySelectorAll('.qe-pick').map((b) => b.getAttribute('data-tmux'));
  assert.deepEqual(picks, [P3, P2, P1, CX, WT, S1], 'the sessions not on screen, in auto-fill order');
  assert.deepEqual(savedOf(w).slots, pad(CK));
  empty.querySelectorAll('.qe-pick').find((b) => b.getAttribute('data-tmux') === P1).click();
  assert.deepEqual(slotsOf(w), { [CK]: '0', [P1]: '1' });
  assert.equal(page(w).querySelector('.qempty'), null);
  tileOf(w, P1).querySelector('.qt-close').click();
  tileOf(w, CK).querySelector('.qt-close').click();
  assert.equal(page(w).querySelectorAll('.qempty').length, 2);
  button(page(w), /^Auto-fill$/).click();
  assert.deepEqual(names(w), [CK, P3]);
});

test('the tile menu: open the terminal page, reconnect, close, and swap in a session that is not on screen', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const opened = [];
  w.ctx.__open = opened;
  w.run('openPage = (u) => { __open.push(u); }');
  tileOf(w, CK).querySelector('.qt-title').click();
  const items = w.document.querySelectorAll('.menuitem').map((i) => i.textContent.trim());
  assert.deepEqual(items.slice(0, 4), ['Open terminal page', 'Reconnect', 'Zoom this tile', 'Close tile'], 'everything the narrow header sheds is in this menu');
  assert.ok(items.some((i) => i === `Swap in ${'petroit/api · s2'}`), 'swap offers the sessions that are not on screen');
  assert.ok(!items.some((i) => /t-checkout-redesign|petroit\/api · s3/.test(i)), 'not the ones that are');
  w.document.querySelectorAll('.menuitem').find((i) => /Swap in petroit\/api · s2/.test(i.textContent)).click();
  assert.deepEqual(slotsOf(w), { [P2]: '0', [P3]: '1' });
  tileOf(w, P2).querySelector('.qt-title').click();
  w.document.querySelectorAll('.menuitem').find((i) => /Open terminal page/.test(i.textContent)).click();
  assert.deepEqual(plain(opened), [`/term/${P2}`]);
});

// ---------------------------------------------------------------- the active tile, the host key bar, the shortcuts

test('the active tile is the last one whose iframe document saw focusin, mousedown, touchstart or keydown (capture); .active moves and the key bar follows', async () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const f0 = load(w, CK);
  const f1 = load(w, P3);
  assert.ok(tileOf(w, CK).classList.contains('active'), 'the first tile starts active');
  for (const ev of ['focusin', 'mousedown', 'touchstart', 'keydown']) {
    const l = f1.contentWindow.listeners[ev] || [];
    assert.ok(l.some((x) => x.opts && x.opts.capture === true), `${ev}: a capture listener on the iframe document`);
  }
  f1.contentWindow.listeners.mousedown[0].fn({});
  assert.deepEqual(tiles(w).map((t) => t.classList.contains('active')), [false, true]);
  assert.match(text(page(w).querySelector('.q-keys-to')), /keys to petroit\/api · s3/);
  f0.contentWindow.listeners.keydown.find((x) => x.opts && x.opts.capture).fn({});
  assert.deepEqual(tiles(w).map((t) => t.classList.contains('active')), [true, false]);
  tileOf(w, P3).dispatchEvent({ type: 'pointerdown' });
  assert.ok(tileOf(w, P3).classList.contains('active'), 'a press on the header or the empty body counts too');
  assert.equal(w.get('Quad').current.active, P3);
});

test('the host key bar sends named keys and scroll to the ACTIVE tile through /keys and /scroll; it shows on touch and in one-up, a toggle forces it', async () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const keys = page(w).querySelector('.q-keys');
  assert.equal(keys.classList.contains('hidden'), true, 'hidden with a mouse in 2-up');
  button(page(w).querySelector('.q-actions'), /^Keys$/).click();
  assert.equal(keys.classList.contains('hidden'), false, 'the toggle shows it');
  assert.equal(w.localStorage.getItem('ccboard:quad:keys'), '1');
  assert.ok(page(w).querySelector('.kb.compact'), 'the compact preset');
  w.get('Quad').current.setActive(P3);
  page(w).querySelector('.kb-key[data-key=Escape]').click();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(posts(w, '/api/sessions').at(-1), { method: 'POST', path: `/api/sessions/${P3}/keys`, body: { keys: ['Escape'] } });
  page(w).querySelector('.kb-key[data-key=Enter]').click();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(posts(w, '/api/sessions').at(-1).body, { keys: ['Enter'] });
  const scrollBtn = (label) => page(w).querySelectorAll('.q-scroll-btn').find((b) => b.getAttribute('aria-label').startsWith(label));
  scrollBtn('Page up').click();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(posts(w, '/api/sessions').at(-1), { method: 'POST', path: `/api/sessions/${P3}/scroll`, body: { dir: 'up', n: 1 } });
  scrollBtn('Scroll to the bottom').click();
  await new Promise((r) => setImmediate(r));
  assert.equal(posts(w, '/api/sessions').at(-1).body.dir, 'bottom');
  w.get('Quad').current.setActive(CK);
  page(w).querySelector('.kb-key[data-key=Tab]').click();
  await new Promise((r) => setImmediate(r));
  assert.equal(posts(w, '/api/sessions').at(-1).path, `/api/sessions/${CK}/keys`, 'the active tile changed: so did the target');
  button(page(w).querySelector('.q-actions'), /^Keys$/).click();
  assert.equal(keys.classList.contains('hidden'), true);
  assert.equal(w.localStorage.getItem('ccboard:quad:keys'), '0');
  const one = quadWorld({ wide: false, hash: '#/quad' });
  assert.equal(page(one.w).querySelector('.q-keys').classList.contains('hidden'), false, 'one-up: shown by default');
});

test('Ctrl+Alt+1..4 focus a tile, Ctrl+Alt+Z zooms the active one, Ctrl+Alt+R reloads every live tile, Ctrl+Alt+K lands on what needs you', () => {
  const env = quadWorld({ hash: `#/quad?s=${S1},${P3},${CX},${WT}&l=4` });
  const { w } = env;
  const wins = {};
  for (const t of [S1, P3, CX, WT]) wins[t] = load(w, t).contentWindow;
  const doc = w.document;
  const press = (over, target = doc) => { const rec = {}; target.dispatch('keydown', key(over, rec)); return rec; };
  let e = press({ code: 'Digit2', key: '™' });
  assert.equal(e.prevented, true, 'the page takes the key');
  assert.equal(w.get('Quad').current.active, P3, 'tile 2 is active');
  assert.equal(wins[P3].focused, 1, 'and the terminal inside it has the focus');
  press({ code: 'Digit4' });
  assert.equal(w.get('Quad').current.active, WT);
  press({ code: 'Digit9' });
  assert.equal(w.get('Quad').current.active, WT, 'there is no tile 9');
  press({ code: 'KeyZ' });
  assert.ok(tileOf(w, WT).classList.contains('zoomed'));
  press({ code: 'KeyZ' });
  assert.equal(tiles(w).some((t) => t.classList.contains('zoomed')), false);
  press({ code: 'KeyR' });
  assert.deepEqual(Object.values(wins).map((x) => x.reloads), [1, 1, 1, 1], 'reload: every live tile');
  const plainKey = press({ code: 'KeyZ', ctrlKey: false });
  assert.equal(plainKey.prevented, undefined, 'a plain z is nobody\'s');
  // Ctrl+Alt+K: the next session that needs you; one that is not on screen takes a calm tile's slot
  w.get('Quad').current.setActive(S1);
  press({ code: 'KeyK' });
  assert.equal(w.get('Quad').current.active, CK, 'CK (a permission) is the first thing that needs you; it was not on screen');
  assert.deepEqual(names(w).sort(), [CK, P3, CX, WT].sort(), 'it took the calm active tile\'s slot (S1 idle)');
  assert.equal(slotsOf(w)[CK], '0');
  press({ code: 'KeyK' });
  assert.equal(w.get('Quad').current.active, P3, 'then the next one, already on screen: just focus');
  assert.equal(wins[P3].focused >= 2, true);
  // inside an iframe document, where the host never sees the key
  const frameKey = wins[CX].listeners.keydown.filter((x) => x.opts === true || (x.opts && x.opts.capture));
  assert.ok(frameKey.length >= 1, 'a keydown listener on the iframe document');
  const rec2 = {};
  frameKey.at(-1).fn(key({ code: 'Digit1' }, rec2));
  assert.equal(rec2.prevented, true);
  assert.equal(w.get('Quad').current.active, CK);
});

test('Ctrl+Alt+K with nothing waiting says so', () => {
  const st = fakeState({ projects: projectsOf({ a: { b: [sess('a', 'b', 's1', { state: 'idle' }), sess('a', 'b', 's2', { state: 'working' })] } }) });
  const { w } = quadWorld({ state: st, hash: '#/quad' });
  w.document.dispatch('keydown', key({ code: 'KeyK' }));
  assert.equal(toasts(w).at(-1).text, 'Nothing needs you right now');
});

test('the help dialog lists the quad shortcuts while the page is up', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  const help = plain(w.run('Keymap.help()')).filter((h) => h.group === 'Quad');
  assert.deepEqual(help.map((h) => h.keys[0]), ['Ctrl+Alt+1…0', 'Ctrl+Alt+Z', 'Ctrl+Alt+K', 'Ctrl+Alt+R', 'Ctrl+Alt+F']);
});

// ---------------------------------------------------------------- fit

test('a resize burst on a tile is ONE fit: ResizeObserver callbacks 250 ms apart collapse; a hidden (0 x 0) tile is never fitted', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w, clock, ros } = env;
  const win = load(w, CK).contentWindow;
  const tileRO = ros.find((r) => r.nodes.some((n) => n === tileOf(w, CK).querySelector('.qt-body')));
  assert.ok(tileRO, 'a ResizeObserver on the tile body');
  const size = { contentRect: { width: 500, height: 400 } };
  for (let i = 0; i < 6; i++) { tileRO.cb([size]); clock.advance(40); }
  assert.equal(win.fits, 0, 'still inside the 250 ms window');
  clock.advance(300);
  assert.equal(win.fits, 1, 'one fit for the whole burst');
  tileRO.cb([{ contentRect: { width: 0, height: 0 } }]);
  clock.advance(1000);
  assert.equal(win.fits, 1, 'a collapsed tile is not fitted to nothing');
  tileRO.cb([size]);
  clock.advance(300);
  assert.equal(win.fits, 2, 'the next burst is the next fit');
});

test('the size chip says the window ("100x30") or "cropped" when the tile\'s terminal is narrower; it follows the poll', () => {
  const st = fixtureState();
  const ck = st.projects.find((p) => p.name === 'phasezero').repos.find((r) => r.name === 'website').sessions[0];
  ck.win = [100, 30];
  const env = quadWorld({ state: st, hash: '#/quad' });
  const { w, clock } = env;
  const size = () => tileOf(w, CK).querySelector('.qt-size');
  assert.equal(text(size()), '100x30', 'no terminal measured yet: the window');
  load(w, CK, 80, 24);
  clock.advance(400);
  assert.equal(text(size()), 'cropped');
  assert.ok(size().classList.contains('cropped'));
  assert.match(size().getAttribute('title'), /100x30.*80x24/);
  w.get('Quad').current.measure(CK);
  frameOf(w, CK).contentWindow.term.cols = 100;
  frameOf(w, CK).contentWindow.term.rows = 30;
  clock.advance(0);
  w.get('Quad').current.measure(CK);
  assert.equal(text(size()), '100x30');
  assert.equal(size().classList.contains('cropped'), false);
});

test('auto-fit: with nobody full attached and the window more than 2 columns off, the session is resized to the tile once; 409 stops it; a full client or odd sizes never post', async () => {
  const mk = (over = {}) => {
    const st = fixtureState();
    const ck = st.projects.find((p) => p.name === 'phasezero').repos.find((r) => r.name === 'website').sessions[0];
    Object.assign(ck, { win: [100, 30], viewers: { full: 0, grid: 1, ro: 0 }, ...over });
    return quadWorld({ state: st, hash: '#/quad' });
  };
  const env = mk();
  const { w, clock } = env;
  load(w, CK, 80, 24);
  clock.advance(400);
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(resizes(w, CK), [{ method: 'POST', path: `/api/sessions/${CK}/resize`, body: { cols: 80, rows: 24 } }]);
  for (let i = 0; i < 4; i++) { setState(env, w.get('state')); clock.advance(3000); }
  await new Promise((r) => setImmediate(r));
  assert.equal(resizes(w, CK).length, 1, 'the same target is not asked for again within a minute');
  const full = mk({ viewers: { full: 1, grid: 0, ro: 0 } });
  load(full.w, CK, 80, 24);
  full.clock.advance(400);
  assert.equal(resizes(full.w).length, 0, 'a full client attached: no POST (it would be a 409)');
  const same = mk({ win: [81, 24] });
  load(same.w, CK, 80, 24);
  same.clock.advance(400);
  assert.equal(resizes(same.w).length, 0, 'within 2 columns');
  const tiny = mk();
  load(tiny.w, CK, 30, 24);
  tiny.clock.advance(400);
  assert.equal(resizes(tiny.w).length, 0, 'under the 40 columns the server takes');
  const conflict = mk();
  conflict.w.ctx.__fail = { '/resize': { message: 'a terminal is attached', status: 409 } };
  load(conflict.w, CK, 80, 24);
  conflict.clock.advance(400);
  await new Promise((r) => setImmediate(r));
  assert.equal(resizes(conflict.w).length, 1);
  conflict.clock.advance(120000);
  conflict.w.run('updateCurrentPage(state)');
  conflict.w.get('Quad').current.measure(CK);
  await new Promise((r) => setImmediate(r));
  assert.equal(resizes(conflict.w).length, 1, 'after a 409 it waits for the window to change before asking again');
  const full2 = mk();
  full2.w.get('Quad').current.setMode(CK, 'full');
  load(full2.w, CK, 80, 24);
  full2.clock.advance(400);
  assert.equal(resizes(full2.w).length, 0, 'a full-mode tile is the sized client itself: it never resizes the window');
});

test('a tab hidden for more than 60 s reloads its live tiles when it is visible again; a short absence only refits', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w, clock } = env;
  const wins = [load(w, CK).contentWindow, load(w, P3).contentWindow];
  w.document.hidden = true;
  w.document.dispatch('visibilitychange');
  clock.advance(30000);
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  clock.advance(400);
  assert.deepEqual(wins.map((x) => x.reloads), [0, 0], '30 s: no reload');
  assert.ok(wins.every((x) => x.fits >= 1), 'but a refit');
  w.document.hidden = true;
  w.document.dispatch('visibilitychange');
  clock.advance(61000);
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  assert.deepEqual(wins.map((x) => x.reloads), [1, 1], '61 s: every live tile reloads');
});

test('Reconnect reloads one tile (the terminal inside its iframe); Reload all reloads every one', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const wins = [load(w, CK).contentWindow, load(w, P3).contentWindow];
  tileOf(w, CK).querySelector('.qt-reconnect').click();
  assert.deepEqual(wins.map((x) => x.reloads), [1, 0]);
  button(page(w), /Reload all/).click();
  assert.deepEqual(wins.map((x) => x.reloads), [2, 1]);
  const f = frameOf(w, P3);
  f.contentWindow = null;
  tileOf(w, P3).querySelector('.qt-reconnect').click();
  assert.equal(f.src, GRID(P3), 'a frame that cannot be reloaded from inside gets its address again');
});

// ---------------------------------------------------------------- a session that goes away

test('a tile whose session is missing for two polls says so and drops its iframe; one empty poll or tmux down never does; it comes back when the session does', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const st = w.get('state');
  const without = (tmux) => {
    const c = JSON.parse(JSON.stringify(st));
    for (const p of c.projects) for (const r of [p.root, ...p.repos].filter(Boolean)) r.sessions = r.sessions.filter((s) => s.tmux !== tmux);
    return c;
  };
  setState(env, { ...st, tmux_down: true, projects: [] });
  assert.ok(frameOf(w, CK) && frameOf(w, P3), 'tmux down: nothing flips');
  setState(env, without(P3));
  assert.ok(frameOf(w, P3), 'missing once: still mounted');
  setState(env, without(P3));
  assert.ok(tileOf(w, P3).classList.contains('gone'));
  assert.equal(frameOf(w, P3), null);
  assert.match(text(tileOf(w, P3).querySelector('.qt-note')), /not running any more/);
  assert.ok(frameOf(w, CK), 'the other tile is unaffected');
  const nobody = { ...st, tmux_down: false, projects: [] };
  setState(env, nobody);
  setState(env, nobody);
  assert.ok(tileOf(w, CK).classList.contains('gone'), 'every session ended (an empty roster with tmux up) is not a blip');
  assert.equal(frameOf(w, CK), null);
  setState(env, st);
  assert.equal(tileOf(w, P3).classList.contains('gone'), false);
  assert.ok(frameOf(w, P3), 'back: a fresh iframe');
  assert.equal(frameOf(w, P3).src, GRID(P3));
});

// ---------------------------------------------------------------- one-up under 840 px

test('under 840 px: one-up, a chip per session (needs-you first) above ONE mounted tile in full mode; only one iframe, so one websocket', () => {
  const { w } = quadWorld({ wide: false, hash: '#/quad' });
  const quad = page(w).querySelector('.quad');
  assert.equal(quad.getAttribute('data-oneup'), 'true');
  assert.equal(quad.getAttribute('data-forced'), 'true', 'the layout picker is out of the way');
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-layout'), '1');
  const chips = page(w).querySelectorAll('.q-chip');
  assert.deepEqual(chips.map((c) => c.getAttribute('data-tmux')), [CK, P3, P2, P1, CX, WT, S1], 'the ended session is not a chip');
  assert.equal(page(w).querySelector('.q-chips').classList.contains('hidden'), false);
  assert.equal(page(w).querySelector('.q-chips').getAttribute('role'), 'tablist');
  assert.deepEqual(chips.map((c) => c.getAttribute('aria-selected')), ['true', 'false', 'false', 'false', 'false', 'false', 'false']);
  assert.deepEqual(names(w), [CK], 'one tile');
  assert.equal(page(w).querySelectorAll('iframe').length, 1);
  assert.equal(tileOf(w, CK).getAttribute('data-mode'), 'full', 'one-up is a full terminal');
  assert.equal(frameOf(w, CK).src, '/tty/?arg=' + CK);
  const first = chips[0];
  assert.ok(first.querySelector('.glyph.waiting') && first.querySelector('.glyph.agent'), 'state and agent glyph');
  assert.equal(text(first.querySelector('.q-chip-name')), 't-checkout-redesign');
  assert.ok(first.classList.contains('needs'));
  assert.ok(chips.find((c) => c.getAttribute('data-tmux') === CX).querySelector('.glyph.agent').getAttribute('aria-label') === 'codex');
});

test('tapping a chip switches the one tile: the old iframe is torn down (about:blank, remove) before the new one mounts; the chip tails come from Live', () => {
  const env = quadWorld({ wide: false, hash: '#/quad' });
  const { w } = env;
  const log = [];
  spy(frameOf(w, CK), log);
  const chip = (t) => page(w).querySelectorAll('.q-chip').find((c) => c.getAttribute('data-tmux') === t);
  chip(CX).click();
  assert.deepEqual(log, ['src:about:blank', 'remove']);
  assert.deepEqual(names(w), [CX]);
  assert.equal(page(w).querySelectorAll('iframe').length, 1, 'still exactly one iframe');
  assert.equal(frameOf(w, CX).src, '/tty/?arg=' + CX);
  assert.equal(chip(CX).getAttribute('aria-selected'), 'true');
  assert.equal(chip(CK).getAttribute('aria-selected'), 'false');
  assert.deepEqual(chips(w), [CK, P3, P2, P1, CX, WT, S1], 'chips keep their order when the selection moves');
  assert.deepEqual(savedOf(w).slots.slice(0, 1), [CX]);
  assert.deepEqual(live(w).sub.slice().sort(), [CK, P3, P2, P1, CX, WT, S1].sort(), 'one Live subscription per chip (one EventSource for all)');
  w.get('__live').fns[CX](['', 'running tests… 41 passed', '', '']);
  assert.equal(text(chip(CX).querySelector('.q-chip-tail')), 'running tests… 41 passed', 'the last non-empty line');
  function chips(world) { return page(world).querySelectorAll('.q-chip').map((c) => c.getAttribute('data-tmux')); }
});

test('the chips rejoin in order: a new session joins at its rank, a vanished one leaves, and Live unsubscribes it; growing the window brings the grid back', () => {
  const env = quadWorld({ wide: false, hash: '#/quad' });
  const { w } = env;
  const order = () => page(w).querySelectorAll('.q-chip').map((c) => c.getAttribute('data-tmux'));
  const st = w.get('state');
  const next = JSON.parse(JSON.stringify(st));
  next.projects.find((p) => p.name === 'ccboard').repos[0].sessions.push(sess('ccboard', 'ccboard', 'new', { state: 'working', state_at: ISO(0.1) }));
  setState(env, next);
  assert.equal(order().at(-1), 'ccboard--ccboard--new', 'a new chip is appended, nothing jumps under the finger');
  assert.equal(order().slice(0, 7).join(), [CK, P3, P2, P1, CX, WT, S1].join());
  const gone = JSON.parse(JSON.stringify(next));
  gone.projects.find((p) => p.name === 'petroit').repos[0].sessions = gone.projects.find((p) => p.name === 'petroit').repos[0].sessions.filter((s) => s.tmux !== P1);
  setState(env, gone);
  assert.ok(!order().includes(P1));
  assert.ok(live(w).unsub.includes(P1));
  const wide = quadWorld({ hash: '#/quad' });
  assert.equal(page(wide.w).querySelector('.q-chips').classList.contains('hidden'), true, 'on a wide window the chips are hidden');
  assert.equal(live(wide.w).sub.length, 0, 'and cost no subscription');
});

test('a window that grows past 840 px turns the chips into the 2-up grid with the first tile kept; one that shrinks goes back to one tile', () => {
  let isWide = false;
  const env = quadWorld({ wide: false, hash: '#/quad' });
  const { w } = env;
  const f = frameOf(w, CK);
  w.run("globalThis.matchMedia = (q) => ({ matches: __wide.value && /840/.test(q), addEventListener() {}, removeEventListener() {} });");
  w.ctx.__wide = { get value() { return isWide; } };
  isWide = true;
  w.fire('resize');
  assert.deepEqual(names(w), [CK, P3], 'the second tile joins');
  assert.equal(page(w).querySelector('.quad').getAttribute('data-oneup'), null);
  assert.equal(frameOf(w, CK).src, GRID(CK), 'the default mode of a tile follows the layout: grid in 2-up');
  assert.equal(page(w).querySelector('.q-chips').classList.contains('hidden'), true);
  isWide = false;
  w.fire('resize');
  assert.deepEqual(names(w), [CK]);
  assert.equal(frameOf(w, CK).src, '/tty/?arg=' + CK, 'and full again in one-up');
  void f;
});

test('a quad column that cannot hold two 240 px tiles (under 488 px) is one-up even in a wide window; one that can keeps two', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w, ros } = env;
  assert.deepEqual(names(w), [CK, P3]);
  const host = page(w).querySelector('.quad');
  const rootRO = ros.find((r) => r.nodes.includes(host));
  host.clientWidth = 525;
  rootRO.cb([{ contentRect: { width: 525, height: 600 } }]);
  assert.deepEqual(names(w), [CK, P3], '525 px holds two tiles of 258 px: the old 600 px rule is gone');
  host.clientWidth = 480;
  rootRO.cb([{ contentRect: { width: 480, height: 600 } }]);
  assert.equal(host.getAttribute('data-oneup'), 'true');
  assert.deepEqual(names(w), [CK]);
  assert.equal(host.getAttribute('data-forced'), 'true');
  host.clientWidth = 900;
  rootRO.cb([{ contentRect: { width: 900, height: 600 } }]);
  assert.deepEqual(names(w), [CK, P3], 'and back to two tiles when the column is wide again');
});

// ---------------------------------------------------------------- outside the page

test('Quad.addToQuad puts a session into the saved slots (growing the layout rather than evicting) and goes to #/quad', () => {
  const { w, Q } = quadWorld();
  w.ctx.__nav = [];
  w.run('navigate = (h) => { __nav.push(h); }');             // the page is not mounted here: only the saved state and the address are looked at
  assert.equal(Q.addToQuad(CK, ''), true);
  assert.deepEqual(plain(Q.load('')), { layout: 2, slots: pad(CK), modes: {}, zoom: null, composers: {} });
  assert.deepEqual(plain(w.get('__nav')), ['#/quad']);
  Q.addToQuad(P3, '');
  assert.deepEqual(plain(Q.load('')).slots, pad(CK, P3));
  Q.addToQuad(P2, '');
  assert.deepEqual(plain(Q.load('')), { layout: 4, slots: pad(CK, P3, P2), modes: {}, zoom: null, composers: {} }, 'two full: 4-up');
  Q.addToQuad(P2, '');
  assert.deepEqual(plain(Q.load('')).slots, pad(CK, P3, P2), 'already there: unchanged');
  Q.addToQuad(P1, 'petroit');
  assert.deepEqual(plain(Q.load('petroit')).slots, pad(P1), 'a project has its own slots');
  assert.equal(plain(w.get('__nav')).at(-1), '#/quad?p=petroit');
  assert.equal(Q.addToQuad('not a name', ''), false);
  Q.addToQuad(S1, '');
  assert.deepEqual(plain(Q.load('')).slots, pad(CK, P3, P2, S1), 'the last free slot');
  Q.addToQuad(WT, '');
  assert.deepEqual(plain(Q.load('')).slots, pad(CK, P3, P2, WT), 'every slot taken in a window of unknown size (the 4-up it always grew to): the last one is replaced');
});

test('Quad.addToQuad grows the layout 4 -> 6 -> 8 -> 10 as far as the window takes tiles, and replaces the last visible tile only beyond that', () => {
  const grow = (width, count) => {
    const { w, Q } = quadWorld({ width });
    w.run('navigate = () => {}');
    const sessions = [CK, P3, P2, P1, CX, WT, S1, 'a--b--s8', 'a--b--s9', 'a--b--s10', 'a--b--s11'];
    for (const t of sessions.slice(0, count)) Q.addToQuad(t, '');
    return plain(Q.load(''));
  };
  assert.deepEqual([grow(1280, 5).layout, grow(1280, 5).slots.filter(Boolean).length], [6, 5], 'a fifth tile in a 1280 window: 6-up');
  assert.deepEqual([grow(1280, 7).layout, grow(1280, 7).slots.filter(Boolean).length], [8, 7]);
  const full = grow(1280, 9);
  assert.equal(full.layout, 8, '1280 takes 8: the ninth replaces the last visible slot');
  assert.equal(full.slots[7], 'a--b--s9');
  assert.equal(full.slots[8], '');
  assert.deepEqual([grow(1920, 9).layout, grow(1920, 9).slots.filter(Boolean).length], [10, 9]);
  assert.equal(grow(1920, 11).slots[9], 'a--b--s11', 'ten visible: the eleventh replaces the tenth');
  assert.equal(grow(1100, 6).layout, 4, '840-1199 takes 4');
});

test('Quad.addToQuad while the quad is up shows the session at once (a free slot, else the active tile\'s); one already shown is just made active', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w, Q } = env;
  const f1 = frameOf(w, P3);
  assert.equal(Q.addToQuad(P3, ''), true);
  assert.equal(w.get('Quad').current.active, P3, 'already on screen: focused, nothing moves');
  assert.equal(frameOf(w, P3), f1);
  assert.equal(Q.addToQuad(P2, ''), true);
  assert.deepEqual(slotsOf(w), { [P2]: '1', [CK]: '0' }, 'both visible slots were taken: the active tile (P3, slot 1) made room');
  w.get('Quad').current.setLayout(4);
  const third = Object.entries(slotsOf(w)).find(([, slot]) => slot === '2')[0];
  tileOf(w, third).querySelector('.qt-close').click();
  assert.equal(Q.addToQuad(S1, ''), true);
  assert.equal(slotsOf(w)[S1], '2', 'a free visible slot takes it first');
});

// ---------------------------------------------------------------- pages.css: the header tiers, the title floor, the sizes

test('pages.css (quad block): the title keeps 40 % of the tile, the pieces shed in the measured order, the mode picker is a menu button under 354 px, touch sheds earlier', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'quad.css'), 'utf8');
  const quad = quadBlock(css);
  assert.match(quad, /\.qt-title \{[^}]*min-width:40cqi/, 'the title floor is 40 % of the size container (the tile)');
  const hide = (sel, scope = '') => { const m = new RegExp(`@container \\(max-width: (\\d+)px\\) \\{ #page \\.quad${scope} ${sel.replace('.', '\\.')} \\{ display:none; \\}`).exec(quad); return m ? Number(m[1]) : null; };
  const mouse = { size: hide('.qt-size'), reconnect: hide('.qt-reconnect'), ctx: hide('.qt-ctx'), zoom: hide('.qt-zoom'), open: hide('.qt-open') };
  assert.ok(mouse.size > mouse.reconnect && mouse.reconnect > mouse.ctx && mouse.ctx > mouse.zoom && mouse.zoom > mouse.open, `the size chip goes first, the open link last: ${JSON.stringify(mouse)}`);
  assert.ok(mouse.open < 400 && mouse.open >= 380, 'the open link survives down to a ~393 px tile (a 494 px 2-up / 4-up tile has it; Reconnect goes first)');
  const touch = { size: hide('.qt-size', '\\[data-touch\\]'), reconnect: hide('.qt-reconnect', '\\[data-touch\\]'), ctx: hide('.qt-ctx', '\\[data-touch\\]'), zoom: hide('.qt-zoom', '\\[data-touch\\]'), close: hide('.qt-close', '\\[data-touch\\]'), open: hide('.qt-open', '\\[data-touch\\]') };
  for (const k of ['size', 'reconnect', 'ctx', 'zoom', 'open']) assert.ok(touch[k] > mouse[k], `touch sheds the ${k} earlier than a mouse (44 px controls)`);
  assert.ok(touch.close > 0, 'on touch Close also moves into the title menu when narrow');
  assert.match(quad, /@container \(max-width: 351px\) \{[^\n]*\n\s*#page \.quad \.qt-modes \{ display:none; \}\n\s*#page \.quad \.qt-modemenu \{ display:inline-flex; \}/, 'under 354 px: the menu button replaces the segmented control (a 20-character session name clipped at 340-352 with it)');
  assert.match(quad, /@container \(max-width: 355px\) \{[^\n]*\n\s*#page \.quad\[data-touch\] \.qt-modes \{ display:none; \}\n\s*#page \.quad\[data-touch\] \.qt-modemenu \{ display:inline-flex; \}/, 'touch: four 44 px options and a readable title need 358 px, so under that it is the menu button too (measured: a 120 px session name clipped at 340-357)');
  assert.match(quad, /\.qt-modemenu \{ display:none;/, 'hidden by default');
  assert.match(quad, /@container \(max-width: 379px\) \{[\s\S]*?\[data-touch\] \.qt-title \{ min-width:0;/, 'the 176 px touch mode control comes before the title floor');
  assert.doesNotMatch(quad, /qt-pill/, 'no cycling pill');
  assert.match(quad, /\.qt-where\.off \{ display:none; \}/, 'TermKit.fitName drops a sliver of project/repo');
});

test('pages.css (quad block): one row for the permission line from 358 px, 28 px keys and chips with a mouse (--row-btn, never --tap), the session name never shrinks', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'quad.css'), 'utf8');
  const quad = quadBlock(css);
  assert.match(quad, /\.qt-perm \{[^}]*display:flex; align-items:center;/);
  assert.doesNotMatch(quad.match(/\.qt-perm \{[^}]*\}/)[0], /flex-wrap:wrap/, 'nowrap by default: the text truncates (its tooltip has it all)');
  assert.match(quad, /@container \(max-width: 357px\) \{ #page \.quad \.qt-perm \{ flex-wrap:wrap; \}/, 'wraps only when too narrow for the text and three buttons');
  assert.match(quad, /\.qt-perm-text \{[^}]*flex:1 1 0; min-width:0;/);
  assert.match(quad, /\.kb-key \{[^}]*min-height:var\(--row-btn\)/);
  assert.match(quad, /\.q-scroll-btn \{[^}]*min-height:var\(--row-btn\)/);
  assert.match(quad, /\.q-chip \{[^}]*min-height:var\(--row-btn\)/);
  assert.doesNotMatch(quad, /var\(--tap\)/, 'the quad block sizes with --row-btn (28 px with a mouse, 44 px on touch)');
  assert.match(quad, /\.qt-name > \.qt-sess \{ flex:0 0 auto; max-width:100%;/, 'the session name does not shrink: the project/repo gives way first');
  assert.match(quad, /\.qt-name > \.qt-where \{ flex:0 1 auto; min-width:0;/);
});

// ---------------------------------------------------------------- the quad for one project (v0.5.9b)

const scopeSel = (w) => page(w).querySelector('.q-scope-sel');
const optionsOf = (w) => scopeSel(w).querySelectorAll('option').map((o) => [o.getAttribute('value'), text(o)]);
const pickScope = (w, value) => { const sel = scopeSel(w); sel.value = value; sel.dispatchEvent({ type: 'change' }); };
const scopeNow = (w) => w.localStorage.getItem('ccboard:quad:scope');
/** The demo-like state with every session of `names` ended (the project has nothing live), and, for `drop`, the named sessions ended too. */
function quiet(names, drop = []) {
  const st = fixtureState();
  for (const p of st.projects) for (const r of [p.root, ...p.repos]) for (const x of (r && r.sessions) || []) if (names.includes(p.name) || drop.includes(x.tmux)) x.state = 'ended';
  return st;
}

test('scopeOptions: All projects first, then every project with a live session by name, a count in each label; ended sessions are not counted; the current scope is always listed', () => {
  const { Q } = quadWorld();
  const o = (st, cur) => plain(Q.scopeOptions(st, cur)).map((x) => [x.project, x.label]);
  assert.deepEqual(o(fixtureState()), [['', 'All projects (7)'], ['ccboard', 'ccboard (2)'], ['petroit', 'petroit (3)'], ['phasezero', 'phasezero (2)']], 'phasezero has an ended session: 2, not 3');
  assert.deepEqual(o(quiet(['phasezero'])), [['', 'All projects (5)'], ['ccboard', 'ccboard (2)'], ['petroit', 'petroit (3)']], 'a project with nothing live is not offered');
  assert.deepEqual(o(quiet(['phasezero']), 'phasezero').map((x) => x[1]), ['All projects (5)', 'ccboard (2)', 'petroit (3)', 'phasezero (0)'], 'but the scope the page is in always is, so the select never lies');
  assert.deepEqual(o(null), [['', 'All projects (0)']]);
  assert.deepEqual(o(null, 'mailgate'), [['', 'All projects (0)'], ['mailgate', 'mailgate (0)']]);
  assert.deepEqual(o(fixtureState(), 'bad name!').map((x) => x[0]), ['', 'ccboard', 'petroit', 'phasezero'], 'a scope that is not a project word is not listed');
});

test('slotsState in a project scope drops the slots of other projects, from the saved state and from the address, and auto-fills from that project only', () => {
  const { Q } = quadWorld();
  const st = fixtureState();
  const saved = { layout: 2, slots: [CK, P3, '', ''], modes: {}, zoom: null };
  assert.deepEqual(plain(Q.slotsState({ saved, state: st, project: 'petroit', n: 2 })).slots, pad(P2, P3), 'CK is phasezero: gone; the empty slot is auto-filled with petroit\'s first');
  assert.deepEqual(plain(Q.slotsState({ saved, state: st, project: '', n: 2 })).slots, pad(CK, P3), 'all projects: nothing is dropped');
  assert.deepEqual(plain(Q.slotsState({ query: { s: `${CK},${P3}` }, state: st, project: 'petroit', n: 2 })).slots, pad(P2, P3), 'the address is held to the scope too');
  assert.deepEqual(plain(Q.slotsState({ query: { s: `${CK},${P3}` }, state: null, project: 'petroit', n: 2 })).slots, pad('', P3), 'before the first poll the name says which project a slot is');
  assert.equal(Q.inProject(st, CK, 'phasezero'), true);
  assert.equal(Q.inProject(st, CK, 'petroit'), false);
  assert.equal(Q.inProject(st, 'phasezero--website--gone', 'phasezero'), true, 'a session that has gone is told by its name');
  assert.equal(Q.inProject(st, CK, ''), true);
});

test('storage: a project called all, scope or keys saves under ccboard:quad:p:<name>, so it cannot clobber the all-projects state, the last-scope pref or the key-bar pref', () => {
  const { w, Q } = quadWorld();
  assert.equal(Q.storageKey(''), 'ccboard:quad:all');
  assert.equal(Q.storageKey('petroit'), 'ccboard:quad:petroit');
  for (const name of ['all', 'scope', 'keys']) assert.equal(Q.storageKey(name), `ccboard:quad:p:${name}`, name);
  w.localStorage.setItem('ccboard:quad:scope', 'petroit');
  w.localStorage.setItem('ccboard:quad:keys', '1');
  Q.save('scope', { layout: 4, slots: [P1, '', '', ''], modes: {}, zoom: null });
  Q.save('keys', { layout: 4, slots: [P2, '', '', ''], modes: {}, zoom: null });
  assert.equal(w.localStorage.getItem('ccboard:quad:scope'), 'petroit', 'the last-scope pref is untouched');
  assert.equal(w.localStorage.getItem('ccboard:quad:keys'), '1');
  assert.deepEqual(plain(Q.load('scope')).slots, pad(P1));
});

test('a project scope fills the tiles, the needs-you order, what an empty tile offers and the tile menu\'s swaps from that project\'s live sessions only', () => {
  const env = quadWorld({ hash: '#/quad?p=petroit&l=4' });
  const { w } = env;
  assert.deepEqual(names(w), [P3, P2, P1], 'petroit\'s three, the one that waits first; phasezero\'s and ccboard\'s are not offered');
  assert.deepEqual(plain(w.get('Quad').autoFill(fixtureState(), 4, { project: 'petroit' })), [P3, P2, P1]);
  assert.match(text(page(w).querySelector('.qempty .qe-title')), /No other session in petroit/, 'the empty fourth tile says whose sessions it looked through');
  tileOf(w, P2).querySelector('.qt-close').click();
  const offered = new Set(page(w).querySelectorAll('.qempty .qe-pick').map((b) => b.getAttribute('data-tmux')));
  assert.deepEqual([...offered], [P2], 'the empty tiles offer petroit\'s one free session, nobody else\'s (all projects would add four more)');
  assert.deepEqual(plain(w.get('Quad').candidates(fixtureState(), 'petroit').map((s) => s.tmux)), [P3, P2, P1]);
  assert.equal(plain(w.get('Quad').candidates(fixtureState(), '').length), 7);
});

test('the phone chips of a project scope are that project\'s sessions only', () => {
  const env = quadWorld({ wide: false, hash: '#/quad?p=petroit' });
  const chips = page(env.w).querySelectorAll('.q-chip').map((c) => c.getAttribute('data-tmux'));
  assert.deepEqual(chips, [P3, P2, P1]);
  assert.equal(page(env.w).querySelector('.q-chips').classList.contains('hidden'), false);
  assert.deepEqual(names(env.w), [P3], 'one tile: the first');
});

test('the scope select: a native select on every width, All projects first, the counts in the labels, the value is the scope', () => {
  for (const wide of [true, false]) {
    const env = quadWorld({ wide, hash: '#/quad?p=petroit' });
    const sel = scopeSel(env.w);
    assert.equal(sel.tagName.toLowerCase(), 'select', 'native: more than four options is no segmented control');
    assert.deepEqual(optionsOf(env.w), [['', 'All projects (7)'], ['ccboard', 'ccboard (2)'], ['petroit', 'petroit (3)'], ['phasezero', 'phasezero (2)']]);
    assert.equal(sel.value, 'petroit');
    assert.equal(sel.getAttribute('aria-label'), 'Project');
    assert.equal(page(env.w).querySelector('.q-scope .q-all').classList.contains('hidden'), false, 'one tap back to all projects while one is chosen');
  }
  const all = quadWorld({ hash: '#/quad' });
  assert.equal(scopeSel(all.w).value, '');
});

test('the select\'s counts follow the poll in place (the option nodes stay); a new project is added once the picker is not in use; the scope stays listed at 0', () => {
  const env = quadWorld({ hash: '#/quad?p=ccboard' });
  const { w } = env;
  const before = scopeSel(w).querySelectorAll('option');
  const st = fixtureState();
  st.projects.find((p) => p.name === 'petroit').repos[0].sessions.pop();
  setState(env, st);
  assert.deepEqual(optionsOf(w).map((o) => o[1]), ['All projects (6)', 'ccboard (2)', 'petroit (2)', 'phasezero (2)']);
  assert.deepEqual(scopeSel(w).querySelectorAll('option'), before, 'the same option nodes: only their text changed');
  setState(env, quiet(['ccboard']));
  assert.deepEqual(optionsOf(w).map((o) => o[1]), ['All projects (5)', 'ccboard (0)', 'petroit (3)', 'phasezero (2)'], 'the scope the page is in stays in the list');
  assert.equal(scopeSel(w).value, 'ccboard');
});

test('choosing a project writes ?p= (a new history entry, so Back returns to the scope before); choosing All projects drops it; the choice is remembered as ccboard:quad:scope', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3}&l=2` });
  const { w } = env;
  env.clock.advance(5);
  assert.equal(scopeNow(w), 'all');
  const before = w.history.calls.length;
  pickScope(w, 'petroit');
  assert.match(w.location.hash, /^#\/quad\?/);
  assert.equal(new URLSearchParams(w.location.hash.split('?')[1]).get('p'), 'petroit');
  assert.equal(w.history.calls.slice(before).filter((c) => c.method === 'pushState').length, 0, 'the push is the location.hash assignment itself (a hashchange), not a replaceState of it');
  assert.equal(scopeNow(w), 'petroit');
  env.clock.advance(5);
  assert.equal(new URLSearchParams(w.location.hash.split('?')[1]).get('p'), 'petroit', 'and it stays');
  assert.equal(scopeSel(w).value, 'petroit');
  assert.equal(w.document.title, 'Quad · petroit · ccboard');
  pickScope(w, '');
  assert.equal(new URLSearchParams(w.location.hash.split('?')[1]).get('p'), null, 'All projects: no ?p=');
  assert.equal(scopeNow(w), 'all');
  assert.equal(w.document.title, 'Quad · ccboard');
  assert.equal(page(w).querySelector('.q-scope .q-all').classList.contains('hidden'), true);
  pickScope(w, 'petroit');
  const hist = w.history.calls.filter((c) => c.method === 'replaceState').length;
  pickScope(w, 'petroit');
  assert.equal(w.history.calls.filter((c) => c.method === 'replaceState').length, hist, 'choosing the scope the page is in does nothing');
});

test('a scope change keeps the tiles of the chosen project (not one iframe is touched), drops the others and auto-fills the gaps; the DOM order never changes', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2},${WT}&l=4` });
  const { w } = env;
  env.clock.advance(5);
  env.w.get('Quad').current.setMode(P3, 'tail');
  const f2 = frameOf(w, P2);
  const log = [];
  spy(f2, log);
  const ckFrame = frameOf(w, CK);
  const dropped = [];
  spy(ckFrame, dropped);
  pickScope(w, 'petroit');
  assert.deepEqual(names(w), [P3, P2, P1], 'CK and WT are gone; P1 is new and was appended, nothing was reordered');
  assert.deepEqual(slotsOf(w), { [P1]: '0', [P3]: '1', [P2]: '2' }, 'the kept ones stay where they were, the gap at 0 is auto-filled, slot 3 has nobody left to show');
  assert.equal(frameOf(w, P2), f2, 'the same iframe node');
  assert.deepEqual(log, [], 'no src assignment and no remove on a tile that stays');
  assert.deepEqual(dropped, ['src:about:blank', 'remove'], 'a tile that goes: the blank page first, then the node');
  assert.equal(tileOf(w, P3).getAttribute('data-mode'), 'tail', 'a mode chosen on a tile that stays stays');
  assert.ok(page(w).querySelector('.qempty[data-slot="3"]'), 'the fourth slot is an empty tile');
  env.clock.advance(5);
  assert.deepEqual(plain(savedOf(w, 'petroit').slots), pad(P1, P3, P2), 'the project\'s own saved state');
  assert.equal(savedOf(w, 'petroit').modes[P3], 'tail');
  pickScope(w, '');
  assert.deepEqual(names(w), [P3, P2, P1, CK], 'all projects keeps all three and fills the free slot with whatever needs you most');
  assert.equal(frameOf(w, P2), f2, 'still the same iframe');
  assert.deepEqual(log, []);
  assert.deepEqual(slotsOf(w), { [P1]: '0', [P3]: '1', [P2]: '2', [CK]: '3' });
});

test('a zoomed tile that stays is still zoomed after a scope change; one that goes drops the zoom', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2},${WT}&l=4` });
  const { w } = env;
  env.clock.advance(5);
  env.w.get('Quad').current.zoomSlot(1);
  assert.ok(tileOf(w, P3).classList.contains('zoomed'));
  pickScope(w, 'petroit');
  assert.ok(tileOf(w, P3).classList.contains('zoomed'), 'P3 stayed, in the same slot');
  pickScope(w, 'phasezero');
  assert.equal(page(w).querySelectorAll('.qtile.zoomed').length, 0, 'P3 left with petroit');
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-zoom'), null);
});

test('a paste of another scope\'s link (no slots in it) opens that scope\'s own saved tiles; one with ?s= wins; Back to an earlier scope restores it', () => {
  const env = quadWorld({ hash: '#/quad?p=petroit&l=2', storage: { 'ccboard:quad:phasezero': { layout: 2, slots: [WT, CK, '', ''], modes: {}, zoom: null } } });
  const { w } = env;
  env.clock.advance(5);
  assert.deepEqual(names(w), [P3, P2]);
  w.location.hash = '#/quad?p=phasezero';
  assert.deepEqual(slotsOf(w), { [WT]: '0', [CK]: '1' }, 'phasezero\'s saved arrangement');
  w.location.hash = '#/quad?p=petroit';
  assert.deepEqual(slotsOf(w), { [P3]: '0', [P2]: '1' }, 'back: petroit\'s');
  w.location.hash = `#/quad?s=${CK}&l=2&p=phasezero`;
  assert.deepEqual(slotsOf(w), { [CK]: '0', [WT]: '1' }, 'the address wins over what phasezero saved; the gap is auto-filled');
});

test('a bare #/quad is always all projects, whatever scope was last used (the last scope rides in the links\' ?p=); the page records the scope it is in', () => {
  const at = (pref, hash = '#/quad') => { const e = quadWorld({ hash, storage: pref === null ? {} : { 'ccboard:quad:scope': pref } }); e.clock.advance(5); return e; };
  const e1 = at('petroit');
  assert.equal(scopeSel(e1.w).value, '', 'a bare address is all projects: the dock\'s add-to-quad, the manifest shortcut and the All projects links depend on it');
  assert.equal(new URLSearchParams(e1.w.location.hash.split('?')[1]).get('p'), null);
  assert.equal(scopeNow(e1.w), 'all', 'and that is now the scope last used');
  const e2 = at('petroit', '#/quad?p=phasezero');
  assert.equal(scopeSel(e2.w).value, 'phasezero');
  assert.equal(scopeNow(e2.w), 'phasezero');
  for (const pref of [null, 'all', 'bad name!', '']) assert.equal(scopeSel(at(pref).w).value, '', `pref ${JSON.stringify(pref)}: all projects`);
});

test('the all-projects link beside the select is the way back, and a link to a scope while another is mounted is a scope change', () => {
  const env = quadWorld({ hash: '#/quad?p=petroit' });
  const { w } = env;
  assert.equal(page(w).querySelector('.q-scope .q-all').getAttribute('href'), '#/quad');
  w.location.hash = '#/quad';
  assert.equal(scopeSel(w).value, '', 'a bare address while a scope is mounted is all projects (the last-scope links carry ?p=)');
  assert.equal(scopeNow(w), 'all');
});

test('a project with no live session shows one plain line, a bordered New session and a quiet All projects link instead of the grid; it fills as soon as a session starts', () => {
  const env = quadWorld({ state: quiet(['phasezero']), shell: true, hash: '#/quad?p=phasezero' });
  const { w } = env;
  env.clock.advance(5);
  const none = page(w).querySelector('.q-none');
  assert.equal(none.classList.contains('hidden'), false);
  assert.equal(text(none.querySelector('.q-none-line')), 'no live session in phasezero');
  assert.equal(page(w).querySelector('.qgrid').classList.contains('hidden'), true, 'no grid');
  assert.equal(page(w).querySelectorAll('.qtile').length, 0, 'no tile, no empty tile, no iframe');
  assert.equal(page(w).querySelectorAll('iframe').length, 0);
  assert.equal(page(w).querySelector('.q-chips').classList.contains('hidden'), true);
  assert.equal(page(w).querySelector('.q-actions').classList.contains('hidden'), true, 'no layout, auto-fill, reload or key buttons for nothing');
  const make = none.querySelector('.q-none-new');
  assert.equal(make.classList.contains('hidden'), false);
  assert.equal(text(make), 'New session');
  assert.equal(make.classList.contains('bp5-intent-primary'), false, 'bordered, not the filled primary: the quad has none');
  make.click();
  assert.deepEqual(plain(w.get('__created')), [{ kind: 'session', ctx: { project: 'phasezero' } }], 'the launcher opens with the project chosen');
  const link = none.querySelector('.q-none-all');
  assert.equal(link.getAttribute('href'), '#/quad');
  assert.equal(text(link), 'All projects');
  assert.ok(link.classList.contains('bp5-minimal') && !link.classList.contains('bp5-intent-primary'), 'quiet');
  assert.equal(scopeSel(w).value, 'phasezero', 'the header still says where you are');
  assert.equal(page(w).querySelector('.q-scope .q-all').classList.contains('hidden'), true, 'and does not repeat the All projects link the block has');
  assert.equal(page(w).querySelector('.q-keys').classList.contains('hidden'), true);
  setState(env, fixtureState());
  assert.equal(none.classList.contains('hidden'), true);
  assert.equal(page(w).querySelector('.q-scope .q-all').classList.contains('hidden'), false, 'the header link is back with the tiles');
  assert.deepEqual(names(w), [CK, WT]);
  assert.equal(page(w).querySelector('.qgrid').classList.contains('hidden'), false);
  assert.equal(page(w).querySelector('.q-actions').classList.contains('hidden'), false);
});

test('the empty-project line is only said when the board knows: a project the board does not have gets no New session; tmux down says nothing', () => {
  const unknown = quadWorld({ shell: true, hash: '#/quad?p=nothere' });
  assert.equal(text(page(unknown.w).querySelector('.q-none-line')), 'no live session in nothere');
  assert.equal(page(unknown.w).querySelector('.q-none-new').classList.contains('hidden'), true, 'there is no such project to start a session in');
  assert.equal(page(unknown.w).querySelector('.q-none-all').getAttribute('href'), '#/quad');
  const down = quiet(['phasezero']);
  down.tmux_down = true;
  const e = quadWorld({ state: down, shell: true, hash: '#/quad?p=phasezero' });
  assert.equal(page(e.w).querySelector('.q-none').classList.contains('hidden'), true, 'tmux is down: the board does not know that nothing runs');
  const noShell = quadWorld({ state: quiet(['phasezero']), hash: '#/quad?p=phasezero' });
  assert.equal(page(noShell.w).querySelector('.q-none-new').classList.contains('hidden'), true, 'without the shell there is no launcher to open');
});

test('the crumb is asked to follow a write-back of the address (a replace fires no hashchange)', () => {
  const env = quadWorld({ shell: true, hash: '#/quad' });
  env.clock.advance(5);
  assert.ok(Number(env.w.get('__crumbs')) >= 1, 'Shell.syncCrumbs after the page wrote ?s=&l= into the address');
});

test('pages.css (quad block, v0.5.9b): the scope select is 28 px with a mouse and 44 px on touch (--row-btn), 16 px there, cut by its own max-width; the empty-project block is centred', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'quad.css'), 'utf8');
  const quad = css.slice(css.indexOf('/* ---------- quad (v0.5.9'));
  const sel = quad.match(/#page \.quad \.q-scope-sel \{[^}]*\}/)[0];
  assert.match(sel, /height:var\(--row-btn\)/);
  assert.match(sel, /min-height:var\(--row-btn\)/);
  assert.match(sel, /max-width:min\(220px, 48vw\)/, 'a long project name cannot push the header past 390 px');
  assert.doesNotMatch(sel, /var\(--tap\)/);
  assert.match(quad, /@media \(pointer:coarse\) \{ #page \.quad \.q-scope-sel \{ font-size:16px; \} \}/, '16 px on touch: iOS does not zoom on focus');
  assert.match(quad, /html\.force-coarse #page \.quad \.q-scope-sel \{ font-size:16px; \}/, 'and for the QA switch');
  assert.match(quad, /\.q-scope \.q-all \{[^}]*min-height:var\(--row-btn\)/, 'the link back is a 44 px target on touch');
  assert.match(quad, /\.q-none \{[^}]*align-items:center; justify-content:center/);
});

test('the scoped page and the empty-project page build no inline style either', () => {
  for (const [state, hash] of [[fixtureState(), '#/quad?p=petroit&l=4'], [quiet(['phasezero']), '#/quad?p=phasezero'], [fixtureState(), '#/quad?p=mailgate']]) {
    const { w } = quadWorld({ state, shell: true, hash });
    const withStyle = [];
    const walk = (n) => { if (n.nodeType !== 1) return; if (n.getAttribute('style') !== null) withStyle.push(n.className); for (const c of n.children) walk(c); };
    walk(page(w));
    assert.deepEqual(withStyle, [], hash);
  }
});

// ---------------------------------------------------------------- v0.5.9c: up to ten tiles

/** n idle Claude sessions in one repo (big--api--s01 ..): they tie on state and age, so the quad's order is their name order. */
function bigState(n = 12, over = {}) {
  const list = Array.from({ length: n }, (_, i) => sess('big', 'api', `s${String(i + 1).padStart(2, '0')}`, { state_at: ISO(30), ...(over[i] || {}) }));
  return fakeState({ projects: projectsOf({ big: { api: list } }) });
}
const BIG = (i) => `big--api--s${String(i).padStart(2, '0')}`;
const bigNames = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => BIG(from + i));
const cells = (w) => page(w).querySelectorAll('.q-layout .seg-btn');
const cell = (w, n) => cells(w).find((b) => text(b) === String(n));
const kitOf = (w) => w.get('__kit');
const order = (w) => page(w).querySelector('.qgrid').children.map((n) => n.getAttribute('data-tmux') || 'empty');

test('the layout control is one number per cell: 1 2 4 6 8 10, 28 px with a mouse (--row-btn), each with a title; the layout that shows is pressed', () => {
  const { w } = quadWorld({ width: 1920, state: bigState(), hash: '#/quad' });
  assert.deepEqual(cells(w).map(text), ['1', '2', '4', '6', '8', '10']);
  assert.deepEqual(cells(w).map((b) => b.getAttribute('aria-pressed')), ['false', 'true', 'false', 'false', 'false', 'false'], 'the default 2 is pressed');
  assert.deepEqual(cells(w).map((b) => b.disabled), [false, false, false, false, false, false], 'a 1920 window takes every layout');
  assert.equal(cell(w, 6).getAttribute('title'), '6 terminals, 3 by 2');
  assert.equal(cell(w, 10).getAttribute('title'), '10 terminals, 5 by 2');
  assert.equal(cell(w, 2).getAttribute('title'), 'Two side by side');
  assert.equal(page(w).querySelector('.q-layout').getAttribute('role'), 'group');
  assert.match(page(w).querySelector('.q-layout').getAttribute('aria-label'), /Tiles on screen/);
  assert.equal(page(w).querySelector('.q-cap').classList.contains('hidden'), true, 'nothing to say while the layout fits');
  cell(w, 10).click();
  assert.deepEqual(cells(w).map((b) => b.getAttribute('aria-pressed')), ['false', 'false', 'false', 'false', 'false', 'true']);
  assert.equal(savedOf(w).layout, 10);
});

test('10-up: ten tiles in five columns, one iframe each, placed by data-slot and never reordered; 10 -> 6 -> 10 keeps the surviving iframes and tears the rest down in order', () => {
  const { w } = quadWorld({ width: 1920, state: bigState(), hash: '#/quad?l=10' });
  assert.deepEqual(names(w), bigNames(1, 10));
  assert.deepEqual(slotsOf(w), Object.fromEntries(bigNames(1, 10).map((t, i) => [t, String(i)])));
  assert.equal(page(w).querySelectorAll('iframe').length, 10, 'one iframe per tile');
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-layout'), '10');
  assert.equal(page(w).querySelector('.quad').getAttribute('data-layout'), '10');
  assert.deepEqual(order(w), bigNames(1, 10));
  const frames = Object.fromEntries(bigNames(1, 10).map((t) => [t, frameOf(w, t)]));
  const logs = Object.fromEntries(bigNames(1, 10).map((t) => [t, []]));
  for (const t of bigNames(1, 10)) spy(frames[t], logs[t]);
  cell(w, 6).click();
  assert.deepEqual(names(w), bigNames(1, 6));
  for (const t of bigNames(7, 10)) assert.deepEqual(logs[t], ['src:about:blank', 'remove'], `${t}: the blank page first, then the node`);
  for (const t of bigNames(1, 6)) { assert.equal(frameOf(w, t), frames[t], `${t}: the same iframe`); assert.deepEqual(logs[t], [], `${t}: not touched`); }
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-layout'), '6');
  cell(w, 10).click();
  assert.deepEqual(names(w), bigNames(1, 10), 'growth remounts the same sessions in the same slots');
  assert.deepEqual(order(w), [...bigNames(1, 6), ...bigNames(7, 10)], 'the first six never moved; the new ones were appended');
  for (const t of bigNames(1, 6)) assert.equal(frameOf(w, t), frames[t]);
  assert.equal(page(w).querySelectorAll('iframe').length, 10);
});

test('the caps by window width: 1280 takes 8, 1100 takes 4, 1920 takes 10; a saved layout above the cap shows the cap with a quiet caption, the cells above it are off with the reason in their title', () => {
  const env = quadWorld({ width: 1280, state: bigState(), hash: '#/quad?l=10' });
  const { w } = env;
  assert.deepEqual(names(w), bigNames(1, 8), 'a saved 10 in a 1280 window shows 8');
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-layout'), '8');
  assert.deepEqual(cells(w).map((b) => b.getAttribute('aria-pressed')), ['false', 'false', 'false', 'false', 'true', 'false'], 'the 8 that shows is pressed, not the 10 that was asked for');
  assert.deepEqual(cells(w).map((b) => b.disabled), [false, false, false, false, false, true], 'only the 10 is off');
  assert.equal(cell(w, 10).getAttribute('title'), '10 terminals need a window 1600 px wide or more');
  const cap = page(w).querySelector('.q-cap');
  assert.equal(text(cap), '10 tiles need a window 1600 px wide or more: showing 8');
  assert.equal(cap.classList.contains('hidden'), false);
  assert.equal(page(w).querySelector('.q-layout').getAttribute('title'), '10 tiles need a window 1600 px wide or more: showing 8', 'and in the control\'s title');
  assert.equal(savedOf(w).layout, 10, 'the wish is what is saved');
  // the window shrinks: 8 -> 4, the tiles beyond unmount
  const logs = Object.fromEntries(bigNames(1, 8).map((t) => [t, []]));
  for (const t of bigNames(1, 8)) spy(frameOf(w, t), logs[t]);
  w.run('innerWidth = 1100');
  w.fire('resize');
  assert.deepEqual(names(w), bigNames(1, 4));
  for (const t of bigNames(5, 8)) assert.deepEqual(logs[t], ['src:about:blank', 'remove']);
  for (const t of bigNames(1, 4)) assert.deepEqual(logs[t], [], 'the survivors are not touched');
  assert.deepEqual(cells(w).map((b) => b.disabled), [false, false, false, true, true, true], '840-1199: up to 4');
  assert.deepEqual(cells(w).map((b) => b.getAttribute('aria-pressed')), ['false', 'false', 'true', 'false', 'false', 'false']);
  assert.equal(text(page(w).querySelector('.q-cap')), '10 tiles need a window 1600 px wide or more: showing 4');
  assert.equal(cell(w, 6).getAttribute('title'), '6 terminals need a window 1200 px wide or more');
  // and the window grows: the saved 10 comes back
  w.run('innerWidth = 1920');
  w.fire('resize');
  assert.deepEqual(names(w), bigNames(1, 10));
  assert.equal(page(w).querySelector('.q-cap').classList.contains('hidden'), true);
  assert.deepEqual(cells(w).map((b) => b.disabled), [false, false, false, false, false, false]);
});

test('the control follows the window even when the tile count does not change (a cap that moves but not under the wish)', () => {
  const { w } = quadWorld({ width: 1280, state: bigState(), hash: '#/quad?l=4' });
  assert.deepEqual(cells(w).map((b) => b.disabled), [false, false, false, false, false, true], '1280: up to 8');
  w.run('innerWidth = 1700');
  w.fire('resize');
  assert.deepEqual(names(w), bigNames(1, 4), 'the same four tiles');
  assert.deepEqual(cells(w).map((b) => b.disabled), [false, false, false, false, false, false], 'but 10 is on now');
});

test('a tile is never narrower than 240 px: a column that holds five tiles keeps 10, one of 1231 px gives way to 8 and says so; the quiet caption names the room', () => {
  const env = quadWorld({ width: 1920, state: bigState(), hash: '#/quad?l=10' });
  const { w, ros } = env;
  const host = page(w).querySelector('.quad');
  const rootRO = ros.find((r) => r.nodes.includes(host));
  host.clientWidth = 1232;
  rootRO.cb([{ contentRect: { width: 1232, height: 800 } }]);
  assert.deepEqual(names(w), bigNames(1, 10), 'five columns of 240 px and four gaps of 8');
  host.clientWidth = 1231;
  rootRO.cb([{ contentRect: { width: 1231, height: 800 } }]);
  assert.deepEqual(names(w), bigNames(1, 8), 'one px short: 8 tiles in four columns');
  assert.equal(cell(w, 10).disabled, true);
  assert.equal(cell(w, 10).getAttribute('title'), '10 terminals would be narrower than 240 px here');
  assert.equal(text(page(w).querySelector('.q-cap')), '10 tiles would be narrower than 240 px here: showing 8');
  assert.equal(cell(w, 8).getAttribute('aria-pressed'), 'true');
  host.clientWidth = 900;
  rootRO.cb([{ contentRect: { width: 900, height: 800 } }]);
  assert.deepEqual(names(w), bigNames(1, 6), '900 px holds three columns');
  host.clientWidth = 600;
  rootRO.cb([{ contentRect: { width: 600, height: 800 } }]);
  assert.deepEqual(names(w), bigNames(1, 4), 'two columns');
  host.clientWidth = 1400;
  rootRO.cb([{ contentRect: { width: 1400, height: 800 } }]);
  assert.deepEqual(names(w), bigNames(1, 10), 'the room came back, so did the wish');
});

test('Auto-fill fills every slot of the layout from the scope, needs-you first, then working, then the most recent; with fewer sessions than tiles the rest are empty tiles', () => {
  const { w } = quadWorld({ width: 1920, hash: '#/quad?l=10' });
  assert.deepEqual(names(w), [CK, P3, P2, P1, CX, WT, S1], 'the seven live sessions of the fixture, in the board\'s order (the ended one never)');
  assert.equal(page(w).querySelectorAll('.qempty').length, 3, 'three slots have nobody to show');
  assert.deepEqual(page(w).querySelectorAll('.qempty').map((e) => e.getAttribute('data-slot')), ['7', '8', '9']);
  const big = quadWorld({ width: 1920, state: bigState(), hash: '#/quad?l=10' });
  for (const t of [BIG(2), BIG(5), BIG(7), BIG(10)]) tileOf(big.w, t).querySelector('.qt-close').click();
  assert.equal(page(big.w).querySelectorAll('.qempty').length, 4);
  button(page(big.w), /^Auto-fill$/).click();
  assert.equal(page(big.w).querySelectorAll('.qempty').length, 0, 'every slot is a tile again');
  assert.equal(new Set(names(big.w)).size, 10, 'ten different sessions');
  assert.deepEqual(names(big.w).slice(0, 1), [BIG(1)]);
  assert.equal(savedOf(big.w).slots.filter(Boolean).length, 10, 'and saved');
  // needs you first: a session that waits on a question is the first one the page fills
  const st = bigState(12, { 11: { state: 'waiting', needs_attention: true, last_message: 'which one?' } });
  const need = quadWorld({ width: 1920, state: st, hash: '#/quad?l=10' });
  assert.equal(names(need.w)[0], BIG(12), 'the session that needs you is the first tile');
});

test('the one-up chip switcher lists up to ten sessions, only one iframe is live', () => {
  const { w } = quadWorld({ wide: false, state: bigState(), hash: '#/quad' });
  const chips = page(w).querySelectorAll('.q-chip');
  assert.equal(chips.length, 10, 'twelve sessions, ten chips');
  assert.equal(page(w).querySelectorAll('iframe').length, 1);
  assert.equal(page(w).querySelector('.quad').getAttribute('data-forced'), 'true');
});

test('a saved layout above what a phone shows is kept as the wish: 10 is saved, one tile shows, the caption stays hidden (the chips are the switcher)', () => {
  const { w } = quadWorld({ wide: false, state: bigState(), hash: '#/quad?l=10' });
  assert.deepEqual(names(w), [BIG(1)]);
  assert.equal(page(w).querySelector('.q-cap').classList.contains('hidden'), true);
  assert.equal(savedOf(w).layout, 10);
});

test('Ctrl+Alt+1..9 and Ctrl+Alt+0 focus tiles 1 to 10; a tile that is not there takes nothing; the key also works inside a terminal document', () => {
  const env = quadWorld({ width: 1920, state: bigState(), hash: '#/quad?l=10' });
  const { w } = env;
  const wins = {};
  for (const t of bigNames(1, 10)) wins[t] = load(w, t).contentWindow;
  const Q = w.get('Quad').current;
  for (const [code, n] of [['Digit1', 1], ['Digit5', 5], ['Digit9', 9], ['Digit0', 10]]) {
    const rec = {};
    w.document.dispatch('keydown', key({ code }, rec));
    assert.equal(rec.prevented, true, `${code}: the page takes the key`);
    assert.equal(Q.active, BIG(n), `${code} is tile ${n}`);
    assert.equal(wins[BIG(n)].focused >= 1, true, 'the terminal inside has the focus');
  }
  const frameKey = wins[BIG(3)].listeners.keydown.filter((x) => x.opts === true || (x.opts && x.opts.capture));
  const rec = {};
  frameKey.at(-1).fn(key({ code: 'Digit5' }, rec));
  assert.equal(rec.prevented, true);
  assert.equal(Q.active, BIG(5), 'also from inside a terminal');
  cell(w, 4).click();
  assert.equal(Q.active, BIG(1), 'the active tile went with its slot: the first one is active again');
  w.document.dispatch('keydown', key({ code: 'Digit7' }));
  assert.equal(Q.active, BIG(1), 'there is no tile 7 in a 4-up: nothing moves');
  assert.deepEqual(names(w), bigNames(1, 4));
});

test('zoom works with ten tiles: the overlay covers the grid, the others stay mounted and inert, no iframe is touched, nothing is refitted', () => {
  const env = quadWorld({ width: 1920, state: bigState(), hash: '#/quad?l=10' });
  const { w } = env;
  const logs = [];
  const wins = {};
  for (const t of bigNames(1, 10)) { wins[t] = load(w, t).contentWindow; spy(frameOf(w, t), logs); }
  const Q = w.get('Quad').current;
  assert.equal(Q.zoomSlot(9), true, 'the tenth tile can be zoomed');
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-zoom'), '9');
  assert.ok(tileOf(w, BIG(10)).classList.contains('zoomed'));
  assert.equal(tileOf(w, BIG(1)).getAttribute('inert'), '');
  assert.deepEqual(logs, []);
  env.clock.advance(1000);
  assert.deepEqual(bigNames(1, 9).map((t) => wins[t].fits), Array(9).fill(0), 'the covered tiles were not refitted');
  assert.equal(savedOf(w).zoom, 9);
  assert.equal(Q.zoomSlot(9), true);
  assert.equal(page(w).querySelector('.qgrid').getAttribute('data-zoom'), null);
});

test('pages.css (quad block, v3): the grid has the layouts\' columns, tiles are placed by `order` per slot 0 to 9 (no grid-area per slot), the layout cells are 28 px / 44 px, a tile of 300 px or less keeps glyphs, name and the menu only', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'quad.css'), 'utf8');
  const quad = css.slice(css.indexOf('/* ---------- quad (v0.5.9'));
  const cols = { 2: 2, 4: 2, 6: 3, 8: 4, 10: 5 };
  for (const [n, c] of Object.entries(cols)) assert.match(quad, new RegExp(`\\.qgrid\\[data-layout="${n}"\\] \\{ grid-template-columns:repeat\\(${c}, minmax\\(0, 1fr\\)\\);`), `${n}-up has ${c} columns`);
  for (const n of [4, 6, 8, 10]) assert.match(quad, new RegExp(`\\.qgrid\\[data-layout="${n}"\\] \\{[^}]*grid-template-rows:repeat\\(2, minmax\\(0, 1fr\\)\\)`), `${n}-up has two rows`);
  for (let i = 0; i < 10; i++) assert.match(quad, new RegExp(`\\.qgrid \\.qtile\\[data-slot="${i}"\\] \\{ order:${i}; \\}`), `slot ${i} is placed by order`);
  assert.doesNotMatch(quad, /\[data-slot="\d"\] \{ grid-area/, 'no grid-area per slot any more');
  assert.match(quad, /\.qgrid \.qtile\.zoomed \{ position:absolute; inset:0; grid-area:1 \/ 1 \/ -1 \/ -1;/, 'the zoomed tile still covers the whole grid');
  assert.match(quad, /\.q-layout \.seg-btn \{[^}]*min-width:var\(--row-btn\); min-height:var\(--row-btn\)/, 'a cell is 28 px with a mouse and 44 px on touch');
  assert.doesNotMatch(quad, /\.q-layout[^{]*\{[^}]*var\(--tap\)/);
  assert.match(quad, /\.q-layout \.seg-btn:disabled \{ opacity:/);
  const small = quad.match(/@container \(max-width: 300px\) \{([\s\S]*?)\n\}/)[1];
  for (const sel of ['.qt-modes', '.qt-modemenu', '.qt-ctx', '.qt-size', '.qt-zoom', '.qt-open', '.qt-reconnect', '.qt-close']) assert.ok(small.includes(`#page .quad ${sel}`), `${sel} goes at 300 px`);
  assert.ok(small.includes('[data-touch] .qt-modemenu') && small.includes('[data-touch] .qt-modes'), 'the touch rules that show the menu button give way too');
  assert.doesNotMatch(small, /\.qt-title \{ display|\.qt-glyphs/, 'the glyphs and the name stay');
  assert.match(quad, /\.q-fs\.on/, 'the fullscreen button when on');
});

// ---------------------------------------------------------------- v0.5.9c: fullscreen

const htmlOf = (w) => w.document.documentElement;
const inFs = (w) => htmlOf(w).classList.contains('quad-fs');
const fsBtn = (w) => page(w).querySelector('.q-fs');
/** The browser's Fullscreen API on the document element, as a recorder: request puts the element in fullscreenElement, exit takes it out. */
function fsApi(w, { refuse = false } = {}) {
  const rec = { requests: [], exits: 0 };
  const root = htmlOf(w);
  root.requestFullscreen = (opts) => {
    rec.requests.push(opts);
    if (refuse) return Promise.reject(new Error('not allowed'));
    w.document.fullscreenElement = root;
    return Promise.resolve();
  };
  w.document.exitFullscreen = () => { rec.exits += 1; w.document.fullscreenElement = null; return Promise.resolve(); };
  return rec;
}
/** A plain key (no modifiers) as Keymap.handle sees it; `rec.prevented` says whether a binding took it. */
const plainKey = (k, over = {}, rec = {}) => ({ type: 'keydown', key: k, ctrlKey: false, altKey: false, metaKey: false, shiftKey: false, target: null, defaultPrevented: false, preventDefault() { rec.prevented = true; }, stopPropagation() {}, ...over });
const esc = (w, over = {}) => { const rec = {}; w.document.dispatch('keydown', plainKey('Escape', over, rec)); return rec; };
const nextTicks = async () => { for (let i = 0; i < 4; i++) await Promise.resolve(); };

test('Fullscreen: the header button puts html.quad-fs on the page and asks the browser for full screen on the document element; the same button says Exit fullscreen and leaves', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  const api = fsApi(w);
  assert.equal(inFs(w), false, 'never entered by itself');
  assert.equal(text(fsBtn(w)), 'Fullscreen');
  assert.equal(fsBtn(w).classList.contains('on'), false);
  assert.equal(fsBtn(w).getAttribute('aria-pressed'), null, 'the label says which way it goes: no pressed state on top of it');
  assert.match(fsBtn(w).getAttribute('title'), /\(F\)/);
  assert.ok(fsBtn(w).querySelector('.bp5-icon-fullscreen'), 'the icon says fullscreen');
  fsBtn(w).click();
  assert.equal(inFs(w), true);
  assert.equal(api.requests.length, 1, 'one request to the Fullscreen API');
  assert.equal(w.document.fullscreenElement, htmlOf(w), 'on the document element, so a popover mounted outside the quad (the topbar\'s) still paints');
  assert.equal(text(fsBtn(w)), 'Exit fullscreen');
  assert.equal(fsBtn(w).classList.contains('on'), true);
  assert.ok(fsBtn(w).querySelector('.bp5-icon-minimize'));
  assert.equal(w.get('Quad').current.fs, true);
  fsBtn(w).click();
  assert.equal(inFs(w), false);
  assert.equal(api.exits, 1, 'and out through the API');
  assert.equal(text(fsBtn(w)), 'Fullscreen');
  assert.equal(fsBtn(w).classList.contains('on'), false);
});

test('Fullscreen is never remembered: nothing about it is saved, a fresh page starts without it', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  fsApi(w);
  w.get('Quad').current.toggleFullscreen();
  env.clock.advance(10);
  assert.equal(inFs(w), true);
  const keys = [];
  for (let i = 0; i < w.localStorage.length; i++) keys.push(w.localStorage.key(i));
  for (const k of keys) assert.doesNotMatch(String(w.localStorage.getItem(k)), /fullscreen|quad-fs|"fs"/i, `${k} says nothing about fullscreen`);
  const saved = Object.fromEntries(keys.map((k) => [k, w.localStorage.getItem(k)]));
  const again = quadWorld({ hash: '#/quad', storage: saved });
  assert.equal(inFs(again.w), false, 'a new page with the same storage is not in fullscreen');
  assert.equal(again.w.get('Quad').current.fs, false);
});

test('Fullscreen: when the browser leaves it (Esc, a gesture) the class goes with it; no second exit call', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  const api = fsApi(w);
  w.get('Quad').current.toggleFullscreen();
  assert.equal(inFs(w), true);
  w.document.fullscreenElement = null;
  w.document.dispatch('fullscreenchange');
  assert.equal(inFs(w), false);
  assert.equal(api.exits, 0, 'the browser had already left');
  assert.equal(text(fsBtn(w)), 'Fullscreen');
  assert.equal(w.get('Quad').current.fs, false);
  w.document.dispatch('fullscreenchange');
  assert.equal(inFs(w), false, 'a change event with nothing to leave is nothing');
  w.get('Quad').current.toggleFullscreen();
  assert.equal(inFs(w), true, 'and it can be entered again');
});

test('Fullscreen without the API (iOS Safari): the class alone does it inside the page; Esc or the button leaves; a refused request keeps the class too', async () => {
  const a = quadWorld({ hash: '#/quad' });
  assert.equal(typeof htmlOf(a.w).requestFullscreen, 'undefined', 'minidom has no Fullscreen API');
  fsBtn(a.w).click();
  assert.equal(inFs(a.w), true, 'the class alone');
  assert.equal(text(fsBtn(a.w)), 'Exit fullscreen');
  const rec = esc(a.w);
  assert.equal(inFs(a.w), false, 'Esc leaves');
  assert.equal(rec.prevented, true);
  fsBtn(a.w).click();
  assert.equal(inFs(a.w), true);
  fsBtn(a.w).click();
  assert.equal(inFs(a.w), false, 'and the button');
  // the browser refuses (no gesture, a policy): the class stays and a later change event does not take it away
  const b = quadWorld({ hash: '#/quad' });
  const api = fsApi(b.w, { refuse: true });
  b.w.get('Quad').current.toggleFullscreen();
  await nextTicks();
  assert.equal(api.requests.length, 1);
  assert.equal(inFs(b.w), true, 'refused: the class alone stands');
  b.w.document.dispatch('fullscreenchange');
  assert.equal(inFs(b.w), true, 'no fullscreen element was ever there to leave');
  esc(b.w);
  assert.equal(inFs(b.w), false);
});

test('Esc leaves fullscreen only when nobody else needs it: not with a dialog, a menu or a tile popover open, not in a field, not when something already took it', () => {
  const { w } = quadWorld({ hash: '#/quad', kit: 'stub' });
  const Q = w.get('Quad').current;
  Q.toggleFullscreen();
  titleBtn(w, CK).click();
  assert.equal(lastMenu(w).open, true);
  esc(w);
  assert.equal(inFs(w), true, 'a tile menu (TermKit.tileMenu) is open: its Esc closes it, the page stays in fullscreen');
  Q.openComposer(CK);
  esc(w);
  assert.equal(inFs(w), true, 'a composer popover too');
  Q.openTune(CK);
  esc(w);
  assert.equal(inFs(w), true, 'and the tune panel');
  Q.closePop();
  w.document.querySelector('#sheet').showModal();
  esc(w);
  assert.equal(inFs(w), true, 'a sheet is open: its Esc closes it');
  w.document.querySelector('#sheet').close();
  esc(w, { target: { tagName: 'INPUT' } });
  esc(w, { target: { tagName: 'TEXTAREA' } });
  assert.equal(inFs(w), true, 'a field keeps its Esc');
  esc(w, { defaultPrevented: true });
  assert.equal(inFs(w), true, 'an Esc somebody already handled');
  const pop = w.document.createElement('div');
  pop.className = 'menu-pop';
  w.document.querySelector('#topbar').append(pop);
  esc(w);
  assert.equal(inFs(w), true, 'a menu is open');
  pop.remove();
  esc(w);
  assert.equal(inFs(w), false);
  esc(w);
  assert.equal(inFs(w), false, 'out of fullscreen an Esc is nobody\'s');
});

test('leaving the page leaves fullscreen: the class goes, the browser is told, no listener is left', () => {
  const { w } = quadWorld({ hash: '#/quad' });
  const api = fsApi(w);
  w.get('Quad').current.toggleFullscreen();
  assert.equal(inFs(w), true);
  w.location.hash = '#/memory';
  assert.equal(inFs(w), false);
  assert.equal(api.exits, 1);
  assert.equal(w.document.listeners.fullscreenchange ? w.document.listeners.fullscreenchange.length : 0, 0, 'no fullscreenchange listener is left behind');
  assert.equal(w.document.listeners.keydown ? w.document.listeners.keydown.length : 0, 0);
});

test('F is full screen and Z zooms the active tile (the keymap), Ctrl+Alt+F works from inside a terminal; all inert in a field', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2},${P1}&l=4` });
  const { w } = env;
  w.run('Keymap.bindDefaults(); Keymap.listen();');
  for (const t of [CK, P3, P2, P1]) load(w, t);
  const press = (k, over = {}) => { const rec = {}; w.document.dispatch('keydown', plainKey(k, over, rec)); return rec; };
  assert.equal(press('f').prevented, true);
  assert.equal(inFs(w), true, 'F goes full screen');
  assert.equal(press('f').prevented, true);
  assert.equal(inFs(w), false, 'and back');
  w.get('Quad').current.setActive(P3);
  assert.equal(press('z').prevented, true);
  assert.ok(tileOf(w, P3).classList.contains('zoomed'), 'Z zooms the active tile');
  assert.equal(press('z').prevented, true);
  assert.equal(tiles(w).some((t) => t.classList.contains('zoomed')), false);
  assert.equal(press('f', { target: { tagName: 'INPUT' } }).prevented, undefined, 'a field keeps its f');
  assert.equal(inFs(w), false);
  w.document.querySelector('#sheet').showModal();
  assert.equal(press('f').prevented, undefined, 'a dialog is modal');
  w.document.querySelector('#sheet').close();
  const rec = {};
  w.document.dispatch('keydown', key({ code: 'KeyF' }, rec));
  assert.equal(rec.prevented, true, 'Ctrl+Alt+F is the page\'s own capture listener');
  assert.equal(inFs(w), true);
  const frameKey = frameOf(w, CK).contentWindow.listeners.keydown.filter((x) => x.opts === true || (x.opts && x.opts.capture));
  const rec2 = {};
  frameKey.at(-1).fn(key({ code: 'KeyF' }, rec2));
  assert.equal(rec2.prevented, true, 'also from inside a terminal document, where a plain f is typed');
  assert.equal(inFs(w), false);
  const one = quadWorld({ wide: false, hash: '#/quad' });
  one.w.run('Keymap.bindDefaults(); Keymap.listen();');
  const r1 = {};
  one.w.document.dispatch('keydown', plainKey('z', {}, r1));
  assert.equal(r1.prevented, undefined, 'one tile: nothing to zoom, the key is left alone');
});

test('Zoom works inside fullscreen: one terminal can take the whole screen; the grid comes back with Back to the grid', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2},${P1}&l=4` });
  const { w } = env;
  const logs = [];
  for (const t of [CK, P3, P2, P1]) { load(w, t); spy(frameOf(w, t), logs); }
  const Q = w.get('Quad').current;
  Q.toggleFullscreen();
  assert.equal(Q.zoomSlot(2), true);
  assert.ok(tileOf(w, P2).classList.contains('zoomed'));
  assert.equal(inFs(w), true, 'still in fullscreen');
  assert.equal(tileOf(w, P2).querySelector('.qt-zoom').getAttribute('aria-pressed'), 'true');
  tileOf(w, P2).querySelector('.qt-zoom').click();
  assert.equal(tiles(w).some((t) => t.classList.contains('zoomed')), false);
  assert.deepEqual(logs, [], 'not one iframe was touched');
});

test('Fullscreen this tile = zoom + page fullscreen; leaving it puts the grid back when this call zoomed it, and keeps a zoom the person made first', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2},${P1}&l=4` });
  const { w } = env;
  const Q = w.get('Quad').current;
  const api = fsApi(w);
  Q.menuCtx(P3).actions.fullscreenTile();
  assert.ok(tileOf(w, P3).classList.contains('zoomed'));
  assert.equal(inFs(w), true);
  assert.equal(api.requests.length, 1);
  assert.equal(Q.active, P3);
  Q.leaveFullscreen();
  assert.equal(inFs(w), false);
  assert.equal(tiles(w).some((t) => t.classList.contains('zoomed')), false, 'the grid is back');
  assert.equal(savedOf(w).zoom, null, 'and nothing of it is saved');
  Q.zoomSlot(0);
  Q.menuCtx(CK).actions.fullscreenTile();
  Q.leaveFullscreen();
  assert.ok(tileOf(w, CK).classList.contains('zoomed'), 'a zoom made before stays');
  Q.zoomSlot(0);
  const one = quadWorld({ wide: false, hash: '#/quad' });
  fsApi(one.w);
  one.w.get('Quad').current.menuCtx(CK).actions.fullscreenTile();
  assert.equal(inFs(one.w), true, 'one tile: just the page fullscreen');
  assert.equal(page(one.w).querySelector('.qgrid').getAttribute('data-zoom'), null);
});

test('leaving the page in "Fullscreen this tile" does not save the zoom it made', () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${P3}&l=2` });
  const { w } = env;
  w.get('Quad').current.menuCtx(CK).actions.fullscreenTile();
  assert.equal(savedOf(w).zoom, 0, 'while it is up the zoom is the page\'s state');
  w.location.hash = '#/memory';
  assert.equal(savedOf(w).zoom, null, 'gone with the page');
});

test('a project that has nothing left to show cannot enter fullscreen, and leaves it when the last session goes (its header, with the Exit button, is hidden)', () => {
  const solo = (list) => fakeState({ projects: projectsOf({ solo: { r: list } }) });
  const env = quadWorld({ hash: '#/quad?p=solo', state: solo([sess('solo', 'r', 's1')]) });
  const { w } = env;
  const Q = w.get('Quad').current;
  assert.equal(Q.toggleFullscreen(), true);
  assert.equal(inFs(w), true);
  tileOf(w, 'solo--r--s1').querySelector('.qt-close').click();
  assert.equal(inFs(w), true, 'the session is still there to pick');
  setState(env, solo([]));
  assert.equal(inFs(w), false, 'nothing left: the page is out of fullscreen');
  assert.equal(page(w).querySelector('.q-none').classList.contains('hidden'), false);
  assert.equal(Q.toggleFullscreen(), false, 'and it will not go in again: the key is left alone');
  assert.equal(inFs(w), false);
});

test('pages.css (quad block, fullscreen): chrome hidden through html.quad-fs, the head one row (32 px with a mouse, the 44 px target on touch), the tiles take the viewport', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'quad.css'), 'utf8');
  const quad = css.slice(css.indexOf('/* ---------- quad (v0.5.9'));
  assert.match(quad, /html\.quad-fs #app \{ grid-template-columns:0 minmax\(0, 1fr\) 0; grid-template-rows:0 minmax\(0, 1fr\); \}/);
  assert.match(quad, /html\.quad-fs #topbar \{ min-height:0; height:0; padding:0; border:0; overflow:visible; \}/, 'the topbar keeps a zero-height box: components.menu() mounts its popovers there');
  assert.match(quad, /html\.quad-fs #topbar > :not\(\.menu-pop\) \{ display:none; \}/, 'only its own children go');
  const hidden = quad.match(/html\.quad-fs #sidebar, html\.quad-fs #dock, html\.quad-fs #bnav, html\.quad-fs #banner[^{]*\{ display:none !important; \}/);
  assert.ok(hidden, 'the sidebar, dock, bottom nav and banner are hidden');
  assert.match(quad, /html\.quad-fs body\[data-shell\] #main \{ padding:0; overflow:hidden; \}/);
  assert.match(quad, /html\.quad-fs body\[data-page=quad\] #page \{ min-height:0; padding:0; \}/, 'the page\'s own bottom padding goes too: the tiles end at the screen\'s edge');
  assert.match(quad, /html\.quad-fs #page > \.quad \{ max-width:none; \}/, '`#page > * { max-width:1400px }` must not cap the grid in fullscreen: above 1400 px the tiles take the whole screen');
  const base = fs.readFileSync(path.join(STATIC, 'pages.css'), 'utf8');
  assert.match(base, /#page > \* \{ max-width:1400px;/, 'the page cap that rule beats (1,2,1 over 1,0,0)');
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  assert.ok(html.indexOf('/static/pages.css') > 0 && html.indexOf('/static/charts.css') > html.indexOf('/static/pages.css'), 'pages.css is linked before charts.css');
  assert.match(fs.readFileSync(path.join(STATIC, 'lazy.js'), 'utf8'), /quad: \{[^}]*pages\/quad\.css/, 'and quad.css is inserted before charts.css (lazy.js), so it still comes after the page cap');
  const head = quad.match(/html\.quad-fs #page \.quad \.q-head \{([^}]*)\}/)[1];
  assert.match(head, /flex-wrap:nowrap/, 'one row');
  assert.match(head, /min-height:max\(32px, var\(--row-btn\)\)/, '32 px with a mouse, 44 px (--row-btn) with a finger');
  assert.match(quad, /html\.quad-fs #page \.quad \.q-head h1, html\.quad-fs #page \.quad \.q-reload, html\.quad-fs #page \.quad \.q-cap/, 'the title, Reload all and the caption go');
  assert.match(quad, /html\.quad-fs #page \.quad\[data-forced\] \.q-fill \{ display:none; \}/, 'a phone has nothing to auto-fill');
  assert.match(quad, /html\.quad-fs #page \.quad \.qgrid \{ gap:4px; \}/);
  assert.doesNotMatch(quad, /html\.quad-fs[^{]*\{[^}]*(position:fixed|100vh)/, 'no fixed overlay: the document is the full screen');
});

// ---------------------------------------------------------------- v0.5.9c: the tile menu (TermKit.tileMenu) and what its actions do

const titleBtn = (w, tmux) => tileOf(w, tmux).querySelector('.qt-title');
const lastMenu = (w) => kitOf(w).menus.at(-1);
const openMenu = (w, tmux) => { const n = kitOf(w).menus.length; titleBtn(w, tmux).click(); return kitOf(w).menus.length > n ? lastMenu(w) : null; };
const GATE = 'Available when the session is at its prompt';
const acts = (ctx) => Object.keys(ctx.actions).filter((k) => typeof ctx.actions[k] === 'function');
const SHELL = 'ops--box--sh1';
const withShell = () => fakeState({ projects: projectsOf({ ops: { box: [sess('ops', 'box', 'sh1', { launcher: 'shell', agent: 'shell', command: 'bash', state: 'idle' }), sess('ops', 'box', 'c1', { state: 'idle' })] } }) });

test('the name and its ▾ open TermKit.tileMenu anchored on that button: a pointer opens it highlighting nothing, the keyboard focuses the first item; a second tap closes; one menu at a time', () => {
  const { w } = quadWorld({ hash: '#/quad', kit: 'stub' });
  const t = titleBtn(w, CK);
  assert.equal(t.getAttribute('aria-haspopup'), 'menu');
  assert.equal(t.getAttribute('aria-expanded'), null, 'not components.menu(): the kit menu owns the popover');
  assert.equal(t.getAttribute('title'), `${CK}: view, input and tune options`);
  assert.ok(t.querySelector('.qt-caret'), 'the ▾');
  t.click();
  const first = lastMenu(w);
  assert.deepEqual([first.opened.length, first.opened[0].anchor === t, first.opened[0].byKeyboard], [1, true, false], 'a pointer: nothing highlighted');
  t.click();
  assert.equal(first.open, false, 'a second tap closes it');
  assert.equal(kitOf(w).menus.length, 1, 'and does not build another');
  t.dispatchEvent({ type: 'click', detail: 0, isTrusted: true, preventDefault() {}, stopPropagation() {} });
  assert.equal(kitOf(w).menus.length, 2);
  assert.equal(lastMenu(w).opened[0].byKeyboard, true, 'Enter or Space on the button: the first item takes the focus');
  const open2 = lastMenu(w);
  titleBtn(w, P3).click();
  assert.equal(open2.open, false, 'opening the menu of another tile closes this one');
  assert.equal(lastMenu(w).ctx.tmux, P3);
  assert.equal(tileOf(w, CK).querySelectorAll('.menu-pop').length, 0);
});

test('without TermKit.tileMenu the old title menu stays (open the terminal page, reconnect, zoom, close, swap in)', () => {
  const { w } = quadWorld({ hash: '#/quad', kit: 'none' });
  assert.equal(titleBtn(w, CK).getAttribute('title'), `${CK}: swap, reconnect, close`);
  assert.equal(titleBtn(w, CK).getAttribute('aria-expanded'), 'false', 'components.menu() owns it');
  titleBtn(w, CK).click();
  assert.deepEqual(w.document.querySelectorAll('.menuitem').slice(0, 4).map((i) => i.textContent.trim()), ['Open terminal page', 'Reconnect', 'Zoom this tile', 'Close tile']);
});

test('the menu\'s ctx for a session with a pending permission: Allow, Deny and In terminal act on it; the prompt is not free, with the reason', () => {
  const { w } = quadWorld({ hash: '#/quad', kit: 'stub' });
  const c = openMenu(w, CK).ctx;
  assert.equal(c.tmux, CK);
  assert.equal(c.agent, 'claude');
  assert.equal(c.session.tmux, CK);
  assert.equal(c.mode, 'grid');
  assert.deepEqual(plain(c.modes), ['grid', 'full', 'ro', 'tail']);
  assert.equal(c.touch, false);
  assert.deepEqual(plain(c.perm), { pending: true, summary: 'Bash: npm test' });
  assert.equal(c.atPrompt, false, 'a permission is open: the prompt is not free');
  assert.equal(c.why, GATE);
  assert.deepEqual(acts(c), ['setMode', 'zoom', 'fullscreenTile', 'popout', 'openTerm', 'reload', 'keysHere', 'allow', 'deny', 'tui', 'composer', 'dockComposer', 'tune', 'compact', 'context', 'usage', 'rename', 'autoContinue', 'close', 'kill'],
    'everything the contract names, minus dock (the window has none)');
  assert.equal(c.actions.dock, null);
  assert.equal(c.zoomed, false);
  assert.equal(c.composerDocked, false);
  assert.equal(c.schema, null, 'no agents entry in this state');
});

test('the menu\'s ctx without a pending permission: no Allow / Deny / In terminal, a free prompt says nothing', () => {
  const { w } = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const c = openMenu(w, S1).ctx;
  assert.deepEqual(plain(c.perm), { pending: false, summary: '' });
  assert.equal(c.actions.allow, null);
  assert.equal(c.actions.deny, null);
  assert.equal(c.actions.tui, null);
  assert.equal(c.atPrompt, true);
  assert.equal(c.why, '');
  assert.equal(typeof c.actions.tune, 'function');
});

test('a working session is not at its prompt: atPrompt is false with the reason, and the tune actions stay (the menu shows them off); done and idle are free; waiting is free only on the idle prompt; compaction is not', () => {
  const st = fakeState({ projects: projectsOf({ a: { b: [
    sess('a', 'b', 'work', { state: 'working' }), sess('a', 'b', 'done', { state: 'done' }), sess('a', 'b', 'err', { state: 'errored' }), sess('a', 'b', 'ask', { state: 'waiting' }),
    sess('a', 'b', 'idlew', { state: 'waiting', flags: { wait_kind: 'idle' } }), sess('a', 'b', 'comp', { state: 'idle', flags: { compacting: true } }), sess('a', 'b', 'cx', { state: 'working', agent: 'codex', launcher: 'codex', command: 'codex' }),
  ] } }) });
  const { w } = quadWorld({ state: st, width: 1920, hash: '#/quad?l=8', kit: 'stub' });
  const gate = (name) => { const c = openMenu(w, `a--b--${name}`).ctx; return [c.atPrompt, c.why]; };
  assert.deepEqual(gate('work'), [false, GATE]);
  assert.deepEqual(gate('cx'), [false, GATE], 'a working Codex session too');
  assert.deepEqual(gate('done'), [true, '']);
  assert.deepEqual(gate('err'), [true, '']);
  assert.deepEqual(gate('ask'), [false, GATE], 'a question is not the prompt');
  assert.deepEqual(gate('idlew'), [true, ''], 'waiting on the idle prompt is');
  assert.deepEqual(gate('comp'), [false, GATE]);
  const c = lastMenu(w).ctx;
  for (const k of ['tune', 'compact', 'context', 'usage', 'rename']) assert.equal(typeof c.actions[k], 'function', `${k} is still offered (the menu shows it off)`);
  const Q = w.get('Quad');
  assert.deepEqual(plain(Q.atPrompt(Q.roster(w.get('state')).find((s) => s.tmux === 'a--b--work'), w.get('state'))), { show: true, ok: false, why: GATE }, 'the pure rule the ctx comes from');
  assert.deepEqual(plain(Q.atPrompt(null, null)), { show: false, ok: false, why: '' });
});

test('Claude, Codex and a plain shell: the agent and the schema ride in the ctx; a shell has no tune items at all', () => {
  const st = fakeState({ agents: { codex: { name: 'codex', models: ['gpt-5.5'] } }, projects: projectsOf({ ops: { box: [sess('ops', 'box', 'sh1', { launcher: 'shell', agent: 'shell', command: 'bash' }), sess('ops', 'box', 'c1'), sess('ops', 'box', 'x1', { agent: 'codex', launcher: 'codex', command: 'codex' })] } }) });
  const { w } = quadWorld({ state: st, width: 1920, hash: '#/quad?l=4', kit: 'stub' });
  const claude = openMenu(w, 'ops--box--c1').ctx;
  assert.deepEqual([claude.agent, claude.schema], ['claude', null], 'no Claude entry in state.agents');
  const codex = openMenu(w, 'ops--box--x1').ctx;
  assert.equal(codex.agent, 'codex');
  assert.deepEqual(plain(codex.schema), { name: 'codex', models: ['gpt-5.5'] }, 'state.agents[agent] is the schema');
  assert.equal(typeof codex.actions.tune, 'function');
  const sh = openMenu(w, SHELL).ctx;
  assert.equal(sh.agent, 'shell');
  for (const k of ['tune', 'compact', 'context', 'usage', 'rename']) assert.equal(sh.actions[k], null, `a shell has no ${k}`);
  assert.equal(sh.why, '', 'nothing to explain: there is nothing to tune');
  for (const k of ['setMode', 'keysHere', 'composer', 'close', 'kill', 'reload']) assert.equal(typeof sh.actions[k], 'function', `${k} works on a shell`);
});

test('a touch window says so: ctx.touch follows the coarse pointer (html.force-coarse), so the menu is a sheet there', () => {
  const { w } = quadWorld({ hash: '#/quad', kit: 'stub' });
  assert.equal(openMenu(w, CK).ctx.touch, false);
  htmlOf(w).classList.add('force-coarse');
  w.get('Quad').current.closePop();
  assert.equal(openMenu(w, CK).ctx.touch, true);
});

test('the actions that need room: Zoom only with two tiles or more (and the ctx knows when it is zoomed); Add to dock only where the dock exists (1024 px and up)', () => {
  const one = quadWorld({ wide: false, hash: '#/quad', kit: 'stub' });
  assert.equal(openMenu(one.w, CK).ctx.actions.zoom, null, 'one tile: nothing to zoom');
  const { w } = quadWorld({ hash: '#/quad', kit: 'stub' });
  const Q = w.get('Quad').current;
  assert.equal(typeof Q.menuCtx(CK).actions.zoom, 'function');
  assert.equal(Q.menuCtx(CK).zoomed, false);
  Q.menuCtx(CK).actions.zoom();
  assert.equal(Q.menuCtx(CK).zoomed, true);
  assert.equal(Q.menuCtx(P3).zoomed, false);
  assert.equal(Q.menuCtx(CK).actions.dock, null, 'no Shell');
  w.run(`globalThis.Shell = { dockOn: () => false, dock: { wanted: '' }, dockStore: { set(k, v) { __dockset.push([k, v]); } }, syncCrumbs() {} }; globalThis.__dockset = [];`);
  assert.equal(Q.menuCtx(CK).actions.dock, null, 'a window under 1024 px (the dock is off)');
  w.run('Shell.dockOn = () => true');
  const dock = Q.menuCtx(CK).actions.dock;
  assert.equal(typeof dock, 'function');
  dock();
  assert.equal(w.run('Shell.dock.wanted'), CK, 'the dock opens it when the Quad is left (the two share the window)');
  assert.deepEqual(plain(w.get('__dockset')), [['ccboard:dock', CK]]);
  assert.match(toasts(w).at(-1).text, /opens in the dock when you leave the Quad/);
});

test('the actions: a mode, close, reload, Allow / Deny / In terminal, the keys, kill', async () => {
  const env = quadWorld({ hash: `#/quad?s=${CK},${S1}`, kit: 'stub' });
  const { w } = env;
  const Q = w.get('Quad').current;
  const a = (tmux) => Q.menuCtx(tmux).actions;
  const ck = a(CK);                                                    // taken while the permission is pending: a decided one is gone from the next ctx
  a(S1).setMode('ro');
  assert.equal(tileOf(w, S1).getAttribute('data-mode'), 'ro');
  assert.equal(Q.menuCtx(S1).mode, 'ro', 'the next menu says so');
  assert.deepEqual(savedOf(w).modes, { [S1]: 'ro' });
  const win = load(w, CK).contentWindow;
  a(CK).reload();
  assert.equal(win.reloads, 1, 'Reload is the tile\'s reconnect');
  await ck.allow();
  assert.equal(posts(w, '/api/permission/12/allow').length, 1);
  assert.equal(Q.menuCtx(CK).actions.allow, null, 'decided: the line is gone and so are the items');
  await ck.deny();
  await ck.tui();
  assert.deepEqual(plain(calls(w)).filter((c) => /permission/.test(c.path)).map((c) => c.path), ['/api/permission/12/allow', '/api/permission/12/deny', '/api/permission/12/tui']);
  a(S1).close();
  assert.deepEqual(names(w), [CK], 'Close tile empties the slot (the session keeps running)');
  assert.equal(calls(w).some((c) => c.method === 'DELETE'), false);
});

test('Keys here: the key bar shows and aims at this tile (a tick in the menu); a second pick hides it', () => {
  const { w } = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2},${P1}&l=4`, kit: 'stub' });
  const Q = w.get('Quad').current;
  assert.equal(Q.menuCtx(P3).keysTarget, false, 'the bar is hidden in 4-up until used');
  assert.equal(Q.menuCtx(P3).actions.keysHere(), true);
  assert.equal(Q.active, P3);
  assert.equal(w.localStorage.getItem('ccboard:quad:keys'), '1');
  assert.equal(page(w).querySelector('.q-keys').classList.contains('hidden'), false);
  assert.match(text(page(w).querySelector('.q-keys-to')), new RegExp(`keys to .*s3`));
  assert.equal(Q.menuCtx(P3).keysTarget, true);
  assert.equal(Q.menuCtx(CK).keysTarget, false, 'another tile is not the target');
  assert.equal(Q.menuCtx(CK).actions.keysHere(), true, 'aimed at another tile');
  assert.equal(Q.active, CK);
  assert.equal(page(w).querySelector('.q-keys').classList.contains('hidden'), false);
  assert.equal(Q.menuCtx(CK).actions.keysHere(), false, 'the target again: the bar goes');
  assert.equal(w.localStorage.getItem('ccboard:quad:keys'), '0');
  assert.equal(page(w).querySelector('.q-keys').classList.contains('hidden'), true);
});

test('Kill session: DELETE /api/sessions/<name>, the tile is emptied; a refusal says why and the tile stays', async () => {
  const env = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const { w } = env;
  const Q = w.get('Quad').current;
  w.ctx.__fail['/api/sessions/' + S1] = { status: 500, message: 'tmux said no' };
  assert.equal(await Q.menuCtx(S1).actions.kill(), false);
  assert.equal(toasts(w).at(-1).text, 'tmux said no');
  assert.deepEqual(names(w), [S1, CK], 'the tile stays');
  w.ctx.__fail = {};
  assert.equal(await Q.menuCtx(S1).actions.kill(), true);
  assert.deepEqual(plain(calls(w)).filter((c) => c.method === 'DELETE').map((c) => c.path), ['/api/sessions/' + S1, '/api/sessions/' + S1]);
  assert.deepEqual(names(w), [CK], 'the tile is emptied');
  assert.match(toasts(w).at(-1).text, /ended/);
});

test('Pop out is a window of its own (a tab when the browser blocks it); Open in terminal is the board\'s own way (the dock when it is on, else the page)', () => {
  const { w } = quadWorld({ hash: '#/quad', kit: 'stub' });
  const Q = w.get('Quad').current;
  w.run('globalThis.__open = []; window.open = (u, n, f) => { __open.push({ u, n, f }); return __opensNull ? null : {}; }; globalThis.__opensNull = false;');
  Q.menuCtx(CK).actions.popout();
  const first = plain(w.get('__open'));
  assert.equal(first.length, 1);
  assert.equal(first[0].u, `/term/${CK}`);
  assert.match(first[0].f, /popup=yes/);
  assert.match(first[0].f, /width=\d+,height=\d+/);
  w.run('__opensNull = true');
  Q.menuCtx(CK).actions.popout();
  const again = plain(w.get('__open'));
  assert.equal(again.length, 3, 'blocked: the same page in a tab');
  assert.deepEqual([again[2].u, again[2].n, again[2].f], [`/term/${CK}`, '_blank', 'noopener']);
  // Open in terminal: Shell.openTerm when there is one
  w.run('globalThis.__term = []; globalThis.Shell = { openTerm: (t) => { __term.push(t); return true; }, syncCrumbs() {} };');
  Q.menuCtx(P3).actions.openTerm();
  assert.deepEqual(plain(w.get('__term')), [P3]);
});

test('Send a prompt…: TermKit.composer is built once per tile with its row, opened under the tile header; Tune…: TermKit.tune with the stats and the schema; one popover at a time', () => {
  const st = fixtureState({ agents: { claude: { name: 'claude', models: ['opus'] } } });
  const { w } = quadWorld({ state: st, hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const Q = w.get('Quad').current;
  const head = tileOf(w, S1).querySelector('.qt-head');
  assert.equal(Q.menuCtx(S1).actions.composer(), true);
  const kit = kitOf(w);
  assert.equal(kit.composers.length, 1);
  const c = kit.composers[0];
  assert.deepEqual([c.ctx.tmux, c.ctx.agent, c.ctx.touch, c.ctx.session.tmux], [S1, 'claude', false, S1]);
  assert.equal(c.opened[0], head, 'under the tile header');
  assert.deepEqual([c.updates.at(-1).touch, c.updates.at(-1).session.tmux], [false, S1], 'handed the row and the pointer as of the open');
  assert.equal(typeof c.ctx.onSent, 'function');
  Q.setActive(CK);
  c.ctx.onSent('sent');
  assert.equal(Q.active, S1, 'a prompt sent from a tile makes it the active one (the key bar follows)');
  c.open = false;
  Q.menuCtx(S1).actions.composer();
  assert.equal(kit.composers.length, 1, 'the same controller again: a draft survives a close');
  assert.equal(c.opened.length, 2);
  // tune
  assert.equal(Q.menuCtx(S1).actions.tune(), true);
  assert.equal(kit.tunes.length, 1);
  assert.equal(c.closed >= 1, true, 'the composer popover closed when the tune panel opened');
  const t = kit.tunes[0];
  assert.deepEqual([t.ctx.tmux, t.ctx.agent, t.ctx.touch, t.ctx.atPrompt], [S1, 'claude', false, true]);
  assert.equal(t.ctx.stats.model, 'Opus 5', 'the current values are read from the session\'s stats');
  assert.deepEqual(plain(t.ctx.schema), { name: 'claude', models: ['opus'] });
  assert.equal(t.opened[0].anchor, head);
  Q.menuCtx(S1).actions.tune();
  assert.equal(kit.tunes.length, 1, 'one controller per tile');
  assert.equal(t.updates.length >= 1, true, 'and kept current: its row, stats and prompt gate are handed over at each open');
});

test('a Codex restart from a tile\'s Tune (issue #2): the tile attaches to the new tmux session of the same name after a moment, nothing sooner and nothing for the other tiles', () => {
  const { w, clock } = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const Q = w.get('Quad').current;
  const wins = {};
  for (const t of [S1, CK]) wins[t] = load(w, t).contentWindow;
  assert.equal(Q.menuCtx(S1).actions.tune(), true);
  const ctx = kitOf(w).tunes[0].ctx;
  assert.equal(typeof ctx.onRestart, 'function', 'the tile hands the kit its way to attach again');
  ctx.onRestart({ tmux: S1 });
  assert.equal(wins[S1].reloads, 0, 'the new session needs a moment to exist');
  clock.advance(1600);
  assert.deepEqual([wins[S1].reloads, wins[CK].reloads], [1, 0], 'this tile only');
});

test('an item picked from the open menu leaves the menu to the kit: the page never closes it first (on touch the kit closes its sheet after the action, so a sheet the action opens is swapped in place, not wiped by the close event of the one before)', () => {
  const { w } = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const Q = w.get('Quad').current;
  const m = openMenu(w, S1);
  assert.equal(m.open, true);
  for (const act of ['composer', 'tune', 'compact', 'context', 'usage', 'rename', 'dockComposer', 'keysHere', 'setMode']) {
    m.ctx.actions[act](act === 'setMode' ? 'ro' : undefined);
    assert.equal(m.closed, 0, `${act}: the menu was not closed by the page`);
    assert.equal(m.open, true);
  }
  assert.equal(kitOf(w).composers[0].opened.length, 1, 'the composer opened');
  assert.equal(kitOf(w).tunes[0].opened.length >= 1, true);
  // what the page does close: a menu it opens another menu over, the page's own fullscreen, its own teardown
  m.ctx.actions.fullscreenTile();
  assert.equal(m.closed >= 1, true, 'going fullscreen closes every surface');
  Q.leaveFullscreen();
  const again = openMenu(w, CK);
  Q.closePop();
  assert.equal(again.open, false, 'closePop() closes the menu and the panels');
});

test('/compact, /context and /usage are the tune panel\'s run(); Rename… is its rename row, both under the tile header; a kit without them opens the panel', () => {
  const { w } = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const Q = w.get('Quad').current;
  const head = tileOf(w, S1).querySelector('.qt-head');
  const a = Q.menuCtx(S1).actions;
  a.compact();
  a.context();
  a.usage();
  const t = kitOf(w).tunes[0];
  assert.deepEqual(plain(t.runs.map((r) => [r.cmd, r.arg])), [['compact', ''], ['context', ''], ['usage', '']]);
  assert.ok(t.runs.every((r) => r.anchor === head), 'a read command\'s output opens at the tile header');
  a.rename();
  assert.equal(t.renames.length, 1);
  assert.equal(t.renames[0], head);
  assert.equal(calls(w).some((c) => /\/command$/.test(c.path)), false, 'the page itself posts nothing: the tune panel owns the command route');
  // a kit whose tune has no run() / rename(): the plain panel
  const bare = quadWorld({ hash: `#/quad?s=${S1}`, kit: 'stub', init: `TermKit.tune = (ctx) => { const rec = { ctx, opened: [], updates: [], closed: 0, open: false }; __kit.tunes.push(rec); return { open(a) { rec.opened.push(a); }, close() { rec.closed += 1; }, update(p) { rec.updates.push(p); }, get isOpen() { return false; } }; };` });
  const b = bare.w.get('Quad').current.menuCtx(S1).actions;
  b.compact();
  b.rename();
  assert.equal(kitOf(bare.w).tunes[0].opened.length, 2, 'no run(), no rename(): Tune… opens instead');
});

test('the tune actions are left out where there is nothing to tune or no kit: a shell, a gone session, a kit without TermKit.tune or TermKit.composer', () => {
  const sh = quadWorld({ state: withShell(), hash: `#/quad?s=${SHELL}`, kit: 'stub' });
  assert.equal(sh.w.get('Quad').current.menuCtx(SHELL).actions.tune, null);
  const noTune = quadWorld({ hash: `#/quad?s=${S1}`, kit: 'stub', init: 'TermKit.tune = undefined; TermKit.composer = undefined;' });
  const c = noTune.w.get('Quad').current.menuCtx(S1);
  for (const k of ['tune', 'compact', 'context', 'usage', 'rename', 'composer', 'dockComposer']) assert.equal(c.actions[k], null, `${k} needs the kit`);
  assert.equal(typeof c.actions.setMode, 'function');
  const gone = quadWorld({ hash: '#/quad?s=old--x--y', kit: 'stub' });
  const g = gone.w.get('Quad').current.menuCtx('old--x--y');
  assert.deepEqual(acts(g), ['setMode', 'zoom', 'fullscreenTile', 'popout', 'reload', 'close'], 'a session that is not running: view and close only');
  assert.equal(g.atPrompt, false);
});

test('the menu\'s ctx is built at each open: the session\'s state changing between two opens changes atPrompt; the menu of a tile that goes is closed with it', () => {
  const env = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const { w } = env;
  assert.equal(openMenu(w, S1).ctx.atPrompt, true);
  w.get('Quad').current.closePop();
  const busy = fixtureState();
  for (const p of busy.projects) for (const r of [p.root, ...p.repos]) for (const s of (r && r.sessions) || []) if (s.tmux === S1) s.state = 'working';
  setState(env, busy);
  const second = openMenu(w, S1);
  assert.equal(second.ctx.atPrompt, false, 'a fresh ctx: the session is working now');
  assert.equal(second.ctx.why, GATE);
  assert.equal(second.open, true);
  tileOf(w, S1).querySelector('.qt-close').click();
  assert.equal(second.open, false, 'the tile left: its menu went with it');
});

// ---------------------------------------------------------------- v0.5.9c: the docked composer (Show composer)

const dockOf = (w, tmux) => tileOf(w, tmux).querySelector('.qt-dock');

test('Show composer: TermKit.composer\'s one-line box is docked at the bottom of the tile, per tile, remembered with the modes and back on the next visit; a second pick takes it away', () => {
  const env = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const { w } = env;
  const Q = w.get('Quad').current;
  assert.equal(dockOf(w, S1).classList.contains('hidden'), true, 'off by default');
  assert.equal(dockOf(w, S1).children.length, 0);
  assert.equal(Q.menuCtx(S1).composerDocked, false);
  assert.equal(kitOf(w).composers.length, 0, 'nothing is built until it is wanted');
  assert.equal(Q.menuCtx(S1).actions.dockComposer(), true);
  const comp = kitOf(w).composers[0];
  assert.equal(dockOf(w, S1).children[0], comp.el, 'the composer\'s own el is mounted in the tile');
  assert.equal(dockOf(w, S1).classList.contains('hidden'), false);
  assert.equal(tileOf(w, S1).children.at(-1), dockOf(w, S1), 'at the tile\'s bottom, after the terminal');
  assert.equal(dockOf(w, CK).children.length, 0, 'per tile');
  assert.equal(Q.menuCtx(S1).composerDocked, true, 'the menu\'s tick');
  assert.deepEqual(savedOf(w).composers, { [S1]: true }, 'remembered with the tile\'s mode');
  assert.deepEqual(savedOf(w).modes, {}, 'and the mode is its own');
  Q.menuCtx(S1).actions.composer();
  assert.equal(kitOf(w).composers.length, 1, 'Send a prompt… is the same controller: one draft');
  // a poll hands the new row over (the queue note says working)
  const busy = fixtureState();
  for (const p of busy.projects) for (const r of [p.root, ...p.repos]) for (const s of (r && r.sessions) || []) if (s.tmux === S1) s.state = 'working';
  setState(env, busy);
  assert.equal(comp.updates.at(-1).session.state, 'working');
  assert.equal(comp.ctx.session.state, 'working', 'so it queues, as the inbox composer does');
  // the next visit
  const again = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub', storage: { 'ccboard:quad:all': savedOf(w) } });
  assert.equal(kitOf(again.w).composers.length, 1);
  assert.equal(dockOf(again.w, S1).children[0], kitOf(again.w).composers[0].el, 'restored');
  assert.equal(dockOf(again.w, CK).children.length, 0);
  // off again
  assert.equal(Q.menuCtx(S1).actions.dockComposer(), false);
  assert.equal(comp.el.parentNode, null, 'the box is taken out');
  assert.equal(dockOf(w, S1).classList.contains('hidden'), true);
  assert.deepEqual(savedOf(w).composers, {});
});

test('the docked composer follows the tile: a session that has gone drops it, closing the tile closes its controllers, the flag stays for the session', () => {
  const env = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const { w } = env;
  const Q = w.get('Quad').current;
  Q.menuCtx(S1).actions.dockComposer();
  Q.menuCtx(S1).actions.tune();
  const comp = kitOf(w).composers[0];
  const tune = kitOf(w).tunes[0];
  const without = fixtureState();
  for (const p of without.projects) for (const r of [p.root, ...p.repos]) if (r && r.sessions) r.sessions = r.sessions.filter((s) => s.tmux !== S1);
  setState(env, without);
  setState(env, without);
  assert.equal(tileOf(w, S1).classList.contains('gone'), true);
  assert.equal(dockOf(w, S1).classList.contains('hidden'), true, 'a dead session has nothing to send to');
  assert.equal(comp.el.parentNode, null);
  setState(env, fixtureState());
  assert.equal(dockOf(w, S1).classList.contains('hidden'), false, 'and it comes back with the session');
  tileOf(w, S1).querySelector('.qt-close').click();
  assert.equal(comp.destroyed, 1, 'the composer is destroyed, not just closed: nothing it holds can fire for a tile that is gone');
  assert.equal(tune.destroyed, 1, 'and so is the tune panel (its pending-setting timer would toast about a dead tile)');
  assert.deepEqual(savedOf(w).composers, { [S1]: true }, 'the flag is the session\'s: it is docked again if it comes back to a tile');
});

test('no composer kit, no docked composer: the action is not offered and a saved flag shows nothing', () => {
  const { w } = quadWorld({ hash: `#/quad?s=${S1}`, kit: 'none', storage: { 'ccboard:quad:all': { layout: 2, slots: [S1], modes: {}, zoom: null, composers: { [S1]: true } } } });
  assert.equal(dockOf(w, S1).classList.contains('hidden'), true);
  assert.equal(dockOf(w, S1).children.length, 0);
});

test('pages.css (quad block): the docked composer is there from 520 px (the tile is the size container); its box brings its own padding and hairline', () => {
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'quad.css'), 'utf8');
  const quad = css.slice(css.indexOf('/* ---------- quad (v0.5.9'));
  assert.match(quad, /@container \(max-width: 519px\) \{ #page \.quad \.qt-dock \{ display:none; \} \}/);
  assert.match(quad, /#page \.quad \.qt-dock \{ flex:none; min-width:0; \}/);
  assert.doesNotMatch(quad.match(/#page \.quad \.qt-dock \{[^}]*\}/)[0], /padding|border|background/, 'no second padding or hairline around TermKit.composer\'s own');
});

test('closing a tile destroys its composer and tune panel; a controller without destroy() is closed instead', () => {
  const env = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const { w } = env;
  const Q = w.get('Quad').current;
  Q.menuCtx(S1).actions.composer();
  Q.menuCtx(S1).actions.tune();
  const comp = kitOf(w).composers[0];
  const tune = kitOf(w).tunes[0];
  tileOf(w, S1).querySelector('.qt-close').click();
  assert.equal(tileOf(w, S1), null);
  assert.deepEqual([comp.destroyed, tune.destroyed], [1, 1], 'destroy(), once each (close() alone would leave the pending-setting timers running)');
  const old = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub', init: 'for (const k of ["composer", "tune"]) { const f = TermKit[k]; TermKit[k] = (ctx) => { const c = f(ctx); delete c.destroy; return c; }; }' });
  old.w.get('Quad').current.menuCtx(S1).actions.composer();
  old.w.get('Quad').current.menuCtx(S1).actions.tune();
  tileOf(old.w, S1).querySelector('.qt-close').click();
  assert.equal(kitOf(old.w).tunes[0].closed, 1, 'an older kit without destroy(): close()');
  assert.equal(kitOf(old.w).composers[0].closed >= 2, true, 'the composer: closed when the tune panel took over, and again with the tile');
});

test('a tile that is gone cannot toast: with the real tune panel, a setting typed just before the tile was closed never reports "not confirmed" 20 s later', async () => {
  const env = quadWorld({ hash: `#/quad?s=${S1}`, kit: 'real' });
  const { w, clock } = env;
  const Q = w.get('Quad').current;
  const wait = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
  assert.equal(Q.menuCtx(S1).actions.tune(), true);
  const btn = w.document.querySelector('.tk-pop-tune .tk-seg[data-kind=effort] button[data-arg=max]');
  assert.ok(btn, 'the real panel is on the page');
  btn.click();
  await wait();
  assert.equal(calls(w).some((c) => /\/tune$/.test(c.path) && c.body.setting === 'effort' && c.body.value === 'max'), true, 'effort max was sent (the /effort picker, this session only)');
  tileOf(w, S1).querySelector('.qt-close').click();
  clock.advance(25000);
  await wait();
  assert.deepEqual(w.get('__toasts').filter((t) => /not confirmed/.test(t.text)), [], 'nothing toasts about a tile that was closed');
});

test('Show composer is offered only where the docked box shows: a tile narrower than 520 px has no such row (its tick would show nothing); an unmeasured tile keeps it', () => {
  const { w } = quadWorld({ hash: `#/quad?s=${S1},${CK}`, kit: 'stub' });
  const Q = w.get('Quad').current;
  assert.equal(w.get('Quad').DOCK_MIN, 520, 'the same 520 px as pages.css');
  const widthOf = (tmux, n) => Object.defineProperty(tileOf(w, tmux), 'offsetWidth', { configurable: true, get: () => n });
  assert.equal(typeof Q.menuCtx(S1).actions.dockComposer, 'function', 'unmeasured: kept');
  widthOf(S1, 494);
  assert.equal(Q.menuCtx(S1).actions.dockComposer, null, '494 px (a 2-up or 4-up tile at 1280): no docked composer, so no row');
  widthOf(S1, 519);
  assert.equal(Q.menuCtx(S1).actions.dockComposer, null);
  widthOf(S1, 520);
  assert.equal(typeof Q.menuCtx(S1).actions.dockComposer, 'function', '520 px: it shows');
  widthOf(S1, 696);
  assert.equal(typeof Q.menuCtx(S1).actions.dockComposer, 'function');
  widthOf(CK, 300);
  assert.equal(Q.menuCtx(CK).actions.dockComposer, null, 'per tile');
  assert.equal(typeof Q.menuCtx(CK).actions.composer, 'function', 'Send a prompt… is still there: the popover does not need the room');
  const real = quadWorld({ hash: `#/quad?s=${S1}`, kit: 'real' });
  Object.defineProperty(tileOf(real.w, S1), 'offsetWidth', { configurable: true, get: () => 300 });
  tileOf(real.w, S1).querySelector('.qt-title').click();
  assert.equal(real.w.document.querySelector('.tk-menu [data-id=dockcomposer]'), null, 'the real menu leaves the row out');
  assert.ok(real.w.document.querySelector('.tk-menu [data-id=composer]'));
});

// ---- hidden slots: a smaller layout leaves live sessions in slots no tile shows; empty visible tiles take them first

const TEN = Array.from({ length: 10 }, (_, i) => `ten--box--s${i}`);
const tenState = (alive = TEN) => fakeState({ projects: projectsOf({ ten: { box: alive.map((t) => sess('ten', 'box', t.split('--')[2])) } }) });

test('Quad.fillSlots: an empty visible slot takes the live sessions of the hidden slots first, in slot order, and that hidden slot is cleared; auto-fill only comes after; a dead hidden slot stays', () => {
  const { Q } = quadWorld();
  const fill = (slots, alive, n, o = {}) => plain(Q.fillSlots(slots, tenState(alive), n, o));
  const [s0, s1, s2, s3, s4, s5, s6, s7, s8, s9] = TEN;
  const alive = [s0, s3, s4, s5, s6, s7, s8, s9];
  assert.deepEqual(fill([s0, '', '', s3, s4, s5, s6, s7, s8, s9], alive, 4), [s0, s4, s5, s3, '', '', s6, s7, s8, s9], '10 slots shrunk to 4: slots 1 and 2 take s4 and s5, and their old slots are empty');
  assert.deepEqual(fill([s0, '', '', s3, s4, s5, s6, s7, s8, s9], alive, 4, { only: [2] }), [s0, '', s4, s3, '', s5, s6, s7, s8, s9], 'only the slots asked for');
  assert.deepEqual(fill([s0, '', '', '', '', '', '', '', '', s9], [s0, s9, s1, s2], 4), [s0, s9, s1, s2, '', '', '', '', '', ''], 'the hidden one first (s9), then auto-fill (s1, s2)');
  assert.deepEqual(fill([s0, '', '', s3, 'gone--x--y', s5, '', '', '', ''], [s0, s3, s5], 4), [s0, s5, '', s3, 'gone--x--y', '', '', '', '', ''], 'a hidden slot whose session is gone is not moved');
  assert.deepEqual(fill([s0, s1, s2, s3, s4, s5, '', '', '', ''], TEN, 6), [s0, s1, s2, s3, s4, s5, '', '', '', ''], 'a full layout changes nothing');
  assert.deepEqual(fill([s0, '', '', '', s1, '', '', '', '', ''], [s0, s1], 4), [s0, s1, '', '', '', '', '', '', '', ''], 'nothing else to take: the rest stays empty');
  const two = fakeState({ projects: projectsOf({ ten: { box: [sess('ten', 'box', 's0'), sess('ten', 'box', 's5')] }, other: { box: [sess('other', 'box', 'x')] } }) });
  assert.deepEqual(plain(Q.fillSlots(['', '', '', '', '', 'other--box--x', '', '', '', ''], two, 4, { project: 'ten' })).slice(0, 6), ['ten--box--s0', 'ten--box--s5', '', '', '', 'other--box--x'], 'a project scope never pulls another project\'s session in');
});

test('shrinking 10 slots to 4 on the page: the empty visible tiles take the live sessions that sat in hidden slots, the surviving iframes are the same nodes, no tile says "No other session is running"', () => {
  const [s0, s1, s2, s3, s4, s5, s6, s7, s8, s9] = TEN;
  const alive = [s0, s3, s4, s5, s6, s7, s8, s9];
  const { w } = quadWorld({ width: 1920, hash: '#/quad', state: tenState(alive), storage: { 'ccboard:quad:all': { layout: 10, slots: [s0, '', '', s3, s4, s5, s6, s7, s8, s9], modes: {}, zoom: null } } });
  const Q = w.get('Quad').current;
  assert.deepEqual(plain(Q.slots).slice(0, 4), [s0, '', '', s3], '10-up: slots 1 and 2 are empty, every live session is placed');
  assert.equal(page(w).querySelectorAll('.qempty').length, 2);
  const keep = [tileOf(w, s0), tileOf(w, s3)];
  const frames = keep.map((t) => t.querySelector('iframe'));
  const moved = [tileOf(w, s4), tileOf(w, s5)];                                 // these two change slot (4, 5 -> 1, 2)
  const movedFrames = moved.map((t) => t.querySelector('iframe'));
  Q.setLayout(4);
  assert.deepEqual(plain(Q.slots), [s0, s4, s5, s3, '', '', s6, s7, s8, s9]);
  assert.deepEqual(names(w).sort(), [s0, s3, s4, s5].sort(), 'four tiles, all of them sessions');
  assert.equal(page(w).querySelectorAll('.qempty').length, 0, 'no empty tile while live sessions wait in hidden slots');
  assert.equal(tileOf(w, s0), keep[0]);
  assert.equal(tileOf(w, s3), keep[1]);
  assert.deepEqual([tileOf(w, s0).querySelector('iframe'), tileOf(w, s3).querySelector('iframe')], frames, 'the iframes that stayed are the same nodes: nothing was moved in the DOM');
  const order = tiles(w).map((t) => t.getAttribute('data-tmux'));
  assert.deepEqual(order.filter((t) => t === s0 || t === s3), [s0, s3], 'and in the same DOM order');
  assert.deepEqual([tileOf(w, s4), tileOf(w, s5)], moved, 'the sessions that moved slots keep their tile nodes too: only data-slot changed');
  assert.deepEqual([tileOf(w, s4).querySelector('iframe'), tileOf(w, s5).querySelector('iframe')], movedFrames, 'and their iframes (a moved iframe would reload)');
  assert.deepEqual([tileOf(w, s4).getAttribute('data-slot'), tileOf(w, s5).getAttribute('data-slot')], ['1', '2']);
  assert.deepEqual(plain(savedOf(w).slots), [s0, s4, s5, s3, '', '', s6, s7, s8, s9], 'saved');
  Q.setLayout(10);
  assert.deepEqual(plain(Q.slots), [s0, s4, s5, s3, '', '', s6, s7, s8, s9], 'growing back puts the rest where it was');
});

test('Auto-fill on the page also pulls the live sessions of hidden slots into empty visible ones; an empty tile offers them too', () => {
  const [s0, , , s3, s4, s5, s6, s7, s8, s9] = TEN;
  const alive = [s0, s3, s4, s5, s6, s7, s8, s9];
  const { w } = quadWorld({ width: 1920, hash: '#/quad', state: tenState(alive), storage: { 'ccboard:quad:all': { layout: 10, slots: [s0, '', '', s3, s4, s5, s6, s7, s8, s9], modes: {}, zoom: null } } });
  const Q = w.get('Quad').current;
  Q.assign(1, '');                                                              // the person empties slot 1 on purpose, in the 10-up
  Q.setLayout(4);
  assert.deepEqual(plain(Q.slots).slice(0, 4), [s0, '', s4, s3], 'a slot the person emptied is not filled by the shrink');
  const offered = page(w).querySelectorAll('.qempty .qe-pick').map((b) => b.getAttribute('data-tmux'));
  assert.ok(offered.includes(s5), 'but its empty tile offers a session that sits in a hidden slot');
  page(w).querySelector('.q-fill').click();
  assert.deepEqual(plain(Q.slots).slice(0, 4), [s0, s5, s4, s3], 'Auto-fill fills it from the hidden slots');
});

// ---------------------------------------------------------------- v0.5.9c: the real TermKit.tileMenu with this page's ctx

test('with the real TermKit.tileMenu: the dropdown has VIEW, INPUT, TUNE and SESSION from this page\'s ctx; a pending permission adds Allow / Deny / In terminal; a working session shows the tune items off', () => {
  const { w } = quadWorld({ hash: `#/quad?s=${CK},${WT},${SHELL}&l=4`, state: fakeState({ projects: projectsOf({ phasezero: { website: [sess('phasezero', 'website', 't-checkout-redesign', { state: 'waiting' })], 'NestJs-Ecommerce-Backend': [sess('phasezero', 'NestJs-Ecommerce-Backend', 't-stock-sync', { state: 'working' })] }, ops: { box: [sess('ops', 'box', 'sh1', { launcher: 'shell', agent: 'shell', command: 'bash' })] } }),
    pending_permissions: [{ id: 12, tmux_name: CK, tool_name: 'Bash', summary: 'Bash: npm test', created_at: ISO(3) }] }), kit: 'real' });
  const open = (tmux) => { titleBtn(w, tmux).click(); const root = w.document.querySelector('.tk-menu'); return root; };
  const groups = (root) => root.querySelectorAll('.tk-group').map((g) => [g.getAttribute('data-group'), g.querySelectorAll('.tk-item').map((i) => text(i.querySelector('.tk-name')))]);
  const modes = (root) => root.querySelectorAll('.tk-mode').map((c) => text(c.querySelector('.tk-name')));
  let root = open(CK);
  assert.ok(root, 'the kit\'s menu is on the page');
  const g = Object.fromEntries(groups(root));
  assert.deepEqual(Object.keys(g), ['view', 'input', 'tune', 'session']);
  assert.deepEqual(modes(root), ['Grid', 'Full', 'Read only', 'Tail'], 'the mode as one segmented row');
  assert.ok(g.view.includes('Zoom') && g.view.includes('Fullscreen this tile') && g.view.includes('Pop out') && g.view.includes('Open in terminal') && g.view.includes('Reload'));
  assert.equal(g.view.includes('Add to dock'), false, 'no dock here');
  assert.deepEqual(g.input.filter((n) => ['Send a prompt…', 'Show composer', 'Keys here', 'Allow', 'Deny', 'In terminal'].includes(n)), ['Send a prompt…', 'Show composer', 'Keys here', 'Allow', 'Deny', 'In terminal']);
  assert.deepEqual(g.tune, ['Tune…', '/compact', '/context']);
  assert.equal(root.querySelectorAll('.tk-group').find((x) => x.getAttribute('data-group') === 'tune').querySelectorAll('.tk-off').length, 3, 'a permission is open: every tune item is off');
  assert.ok(root.querySelector('.tk-kill'), 'Kill session, last, behind confirmButton');
  w.get('Quad').current.closePop();
  root = open(WT);
  const g2 = Object.fromEntries(groups(root));
  assert.deepEqual(g2.input.filter((n) => ['Allow', 'Deny', 'In terminal'].includes(n)), [], 'no permission, no Allow');
  assert.equal(root.querySelectorAll('.tk-group').find((x) => x.getAttribute('data-group') === 'tune').querySelectorAll('.tk-off').length, 3, 'working: off, with the reason');
  assert.match(text(root.querySelector('.tk-note')), /at its prompt/i);
  w.get('Quad').current.closePop();
  root = open(SHELL);
  const g3 = Object.fromEntries(groups(root));
  assert.equal(g3.tune, undefined, 'a shell has no TUNE group');
});

// ---------------------------------------------------------------- v0.5.9c: the static scan

test('quad.js builds nothing through innerHTML, insertAdjacentHTML, cssText or a style attribute, and sets text with textContent', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'quad.js'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  assert.doesNotMatch(src, /\.innerHTML\b|\binnerHTML\s*[=+]/, 'no innerHTML');
  assert.doesNotMatch(src, /\binsertAdjacentHTML\b/);
  assert.doesNotMatch(src, /\bcssText\b/);
  assert.doesNotMatch(src, /setAttribute\(\s*['"`]style['"`]/);
  assert.doesNotMatch(src, /\bstyle\s*:/, 'no style key in an el() attribute list');
  assert.doesNotMatch(src, /\.style\./, 'not even through the CSSOM: the page has no inline style at all');
  assert.doesNotMatch(src, /document\.write|\beval\(|new Function/);
});

test('the quad keeps no filled primary: its buttons are bordered, tinted or minimal, the permission row\'s Allow is the tinted one, Kill is the menu\'s red outline', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'quad.js'), 'utf8');
  const classes = [...src.matchAll(/class: '([^']*)'/g)].map((m) => m[1]);
  for (const c of classes) if (/\bprimary\b/.test(c)) assert.match(c, /\btinted\b/, `${c}: a primary in the quad is the outlined tint`);
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'quad.css'), 'utf8');
  const quad = css.slice(css.indexOf('/* ---------- quad (v0.5.9'), css.indexOf('/* ---------- quad (v0.5.9') + 30000);
  assert.doesNotMatch(quad.match(/#page \.quad \.q-layout[^\n]*/g).join('\n'), /background:var\(--sig\)/, 'no filled cyan on the layout control');
  assert.doesNotMatch(quad.match(/#page \.quad \.q-fs[^\n]*/g).join('\n'), /background:var\(--sig\)[^-]/);
});

// ---------------------------------------------------------------- the CSP and the registration

test('the page builds no inline style: no style attribute is ever set on a node it creates', () => {
  const { w } = quadWorld({ hash: `#/quad?s=${CK},${P3},${P2},${P1}&l=4` });
  const withStyle = [];
  const walk = (n) => { if (n.nodeType !== 1) return; if (n.getAttribute('style') !== null) withStyle.push(n.className); for (const c of n.children) walk(c); };
  walk(page(w));
  assert.deepEqual(withStyle, []);
});

test('quad.js is registered once, as the quad route, and does nothing at load', () => {
  const { w } = quadWorld();
  assert.deepEqual(plain(w.run("Object.keys(pages).filter((k) => k === 'quad')")), ['quad']);
  assert.equal(w.get('Quad').current, null);
  assert.equal(w.run('PLACEHOLDER_INFO.quad'), undefined);
  assert.equal(w.document.listeners.keydown ? w.document.listeners.keydown.length : 0, 0, 'no listener before the page is mounted');
});

// ---------------------------------------------------------------- v0.5.13: a new session from an empty tile (openLauncher)

/** openLauncher as a recorder; the opts it got are read back as plain values (the project and repo are the state's objects, onStarted a function). */
const stubLauncher = (w) => w.run('globalThis.__launched = []; globalThis.openLauncher = (o) => { __launched.push(o); return true; };');
const launched = (w) => plain(w.run('__launched.map((o) => ({ mode: o.mode, project: o.project && o.project.name, repo: o.repo && o.repo.name, slot: o.slot, open: o.open, hook: typeof o.onDone }))'));
const emptyTile = (w, slot) => page(w).querySelectorAll('.qempty').find((n) => n.getAttribute('data-slot') === String(slot));

test('an empty tile has + New session: openLauncher in session mode for the project of the quad, in the repo it worked in last, with the slot; the page opens no sheet itself', () => {
  const { w } = quadWorld({ hash: '#/quad?p=petroit' });
  stubLauncher(w);
  tileOf(w, P3).querySelector('.qt-close').click();
  const e = emptyTile(w, 0) || emptyTile(w, 1);
  const btn = button(e, /New session/);
  assert.ok(btn, 'the button is in the empty tile, beside the pick list');
  assert.equal(btn.getAttribute('title'), 'start a new session for this tile');
  btn.click();
  assert.deepEqual(launched(w), [{ mode: 'session', project: 'petroit', repo: 'api', slot: Number(e.getAttribute('data-slot')), open: false, hook: 'function' }]);
  assert.equal(w.run('__launched[0].project === state.projects.find((p) => p.name === "petroit")'), true, 'the state object');
  assert.equal(w.document.getElementById('sheet').open, false);
});

test('on the board-wide quad the new session goes where the board worked last; onDone (the launcher hook for a started session) closes the sheet and puts the new session into that tile', () => {
  const st = fixtureState();
  st.projects.find((p) => p.name === 'phasezero').repos.find((r) => r.name === 'website').sessions[0].state_at = new Date(Date.now() + 60000).toISOString();     // the newest activity anywhere
  const { w } = quadWorld({ hash: '#/quad', state: st });
  stubLauncher(w);
  tileOf(w, P3).querySelector('.qt-close').click();
  const e = page(w).querySelectorAll('.qempty')[0];
  button(e, /New session/).click();
  const [c] = launched(w);
  assert.deepEqual([c.mode, c.project, c.repo], ['session', 'phasezero', 'website']);
  const slot = c.slot;
  w.ctx.__started = { tmux: 'phasezero--website--fresh' };
  w.run("document.getElementById('sheet').showModal && document.getElementById('sheet').showModal()");
  w.run('__launched[0].onDone(__started, "now")');
  assert.equal(slotsOf(w)['phasezero--website--fresh'], String(slot), 'the new session takes the tile it was started for');
  assert.equal(w.document.getElementById('sheet').open, false, 'and the sheet is closed (the launcher leaves that to its caller when it has an onDone)');
  w.run('__launched[0].onDone({})');
  w.run('__launched[0].onDone(null)');
  assert.equal(page(w).querySelectorAll('.qtile[data-tmux]').length >= 1, true, 'a response without a tmux name changes nothing and throws nothing');
});

test('with no project and no repo anywhere + New session falls back to the repo picker (Shell.openCreate)', () => {
  const { w } = quadWorld({ hash: '#/quad', state: fakeState({ projects: [] }) });
  stubLauncher(w);
  w.run('globalThis.__kinds = []; globalThis.Shell = { openCreate: (k, ctx) => { __kinds.push([k, ctx === undefined ? null : ctx]); return true; } };');
  const e = page(w).querySelectorAll('.qempty')[0];
  button(e, /New session/).click();
  assert.deepEqual(launched(w), []);
  assert.deepEqual(plain(w.get('__kinds')), [['session', null]]);
});

test('tap count (real launcher): a new session from an empty quad tile is 2 taps with the remembered defaults: + New session, then Start & open; the new session lands in the tile', async () => {
  const { w } = quadWorld({ hash: '#/quad?p=petroit' });
  w.load('launcher.js');                                                                  // the quad world has no launcher; the real one here
  w.ctx.__opened = []; w.run('renderBanner = () => {}; openPage = (u) => { __opened.push(u); }; api = async (method, path, body) => { __calls.push({ method, path, body }); return path.endsWith("/sessions") ? { tmux: "petroit--api--s9", attach_url: "/term/petroit--api--s9", agent: "claude", cmd: "claude" } : { ok: true }; }');
  tileOf(w, P3).querySelector('.qt-close').click();
  const e = page(w).querySelectorAll('.qempty')[0];
  const slot = e.getAttribute('data-slot');
  let taps = 0;
  taps++;                                                                                  // 1: + New session in the empty tile
  button(e, /New session/).click();
  const sh = w.document.getElementById('sheet');
  assert.equal(sh.open, true);
  assert.equal(text(sh.querySelector('.sheet-title')), 'New session · petroit/api');
  const primaries = sh.querySelectorAll('button').filter((b) => b.classList.contains('bp5-intent-primary'));
  assert.equal(primaries.length, 1);
  assert.match(text(primaries[0]), /Start & open/);
  taps++;                                                                                  // 2: Start & open
  sh.querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
  await new Promise((r) => setImmediate(r)); await new Promise((r) => setImmediate(r));
  assert.deepEqual(posts(w, '/api/projects/').map((c) => c.path), ['/api/projects/petroit/repos/api/sessions']);
  assert.equal(taps, 2);
  assert.equal(slotsOf(w)['petroit--api--s9'], slot, 'the new session takes the tile it was started for (the launcher\'s onDone)');
  assert.equal(sh.open, false, 'and the sheet is closed');
  assert.deepEqual(plain(w.get('__opened')), [], 'open: false: no terminal page or tab is opened elsewhere, the tile shows it');
});

// ---------------------------------------------------------------- v0.5.21: the pointer type flips (#52), and the quad leftovers (#34)

/** A world whose matchMedia and MutationObserver are tables the test drives: flip(on) changes (pointer: coarse) and fires 'change' on the queries the page subscribed to. */
function pointerWorld(opts = {}) {
  const st = { coarse: false, adds: 0, removes: 0, subs: new Set(), mos: [] };
  const mm = (q) => {
    const isPointer = /pointer/.test(q);
    const m = {
      media: q,
      get matches() { return isPointer ? st.coarse : /840/.test(q); },
      addEventListener(t, fn) { if (t === 'change') { st.adds += 1; st.subs.add(fn); } },
      removeEventListener(t, fn) { if (t === 'change') { st.removes += 1; st.subs.delete(fn); } },
    };
    return m;
  };
  class FakeMO {
    constructor(cb) { this.cb = cb; this.gone = false; this.opts = null; this.target = null; st.mos.push(this); }
    observe(n, o) { this.target = n; this.opts = o; }
    disconnect() { this.gone = true; }
  }
  const env = quadWorld({ hash: '#/quad', kit: 'stub', extra: { matchMedia: mm, MutationObserver: FakeMO }, ...opts });
  env.st = st;
  env.flip = (on) => { st.coarse = on; for (const fn of [...st.subs]) fn({ matches: on }); };
  return env;
}

test('a pointer flip sets data-touch and hands every live component update({touch}) before any poll, and flipping back clears it', () => {
  const env = pointerWorld();
  const { w } = env;
  const quad = () => page(w).querySelector('.quad');
  assert.equal(quad().getAttribute('data-touch'), null);
  const cur = w.get('Quad').current;
  const T1 = names(w)[0];
  cur.openComposer(T1);                                                  // builds a composer controller for that tile
  assert.equal(w.get('__kit').composers.length >= 1, true);
  const composer = w.get('__kit').composers[0];
  composer.updates.length = 0;
  env.flip(true);
  assert.equal(quad().getAttribute('data-touch'), 'true', 'no sync pass was needed');
  assert.deepEqual(plain(composer.updates).filter((u) => 'touch' in u), [{ touch: true }]);
  env.flip(false);
  assert.equal(quad().getAttribute('data-touch'), null);
  assert.equal(composer.updates.filter((u) => u.touch === false).length, 1);
  env.flip(false);
  assert.equal(composer.updates.filter((u) => u.touch === false).length, 1, 'a change event that changes nothing does nothing');
});

test('toggling html.force-coarse does the same through a class-only MutationObserver', () => {
  const env = pointerWorld();
  const { w, st } = env;
  const mo = st.mos[0];
  assert.ok(mo, 'an observer was made');
  assert.equal(mo.target, w.document.documentElement);
  assert.deepEqual(plain(mo.opts), { attributes: true, attributeFilter: ['class'] });
  w.document.documentElement.classList.add('force-coarse');
  mo.cb([]);
  assert.equal(page(w).querySelector('.quad').getAttribute('data-touch'), 'true');
  w.document.documentElement.classList.remove('force-coarse');
  mo.cb([]);
  assert.equal(page(w).querySelector('.quad').getAttribute('data-touch'), null);
});

test('a flip closes an open tile menu (it was built for the old tier) and re-sources, reorders and restarts nothing', () => {
  const env = pointerWorld();
  const { w } = env;
  const cur = w.get('Quad').current;
  const before = names(w);
  const T1 = before[0];
  const frame = frameOf(w, T1);
  const log = [];
  spy(frame, log);
  assert.equal(cur.openMenu(T1, false), true);
  const menu = w.get('__kit').menus[0];
  assert.equal(menu.open, true);
  env.flip(true);
  assert.equal(menu.open, false);
  assert.deepEqual(names(w), before);
  assert.equal(frameOf(w, T1), frame, 'the same iframe node');
  assert.deepEqual(log, [], 'never re-sourced or removed');
  assert.equal(resizes(w).length, 0, 'no process or size was touched');
});

test('unmount removes the media listener and disconnects the observer; repeated mounts leave nothing behind', () => {
  const env = pointerWorld();
  const { w, st } = env;
  assert.equal(st.subs.size >= 1, true);
  assert.equal(st.mos.filter((m) => !m.gone).length, 1);
  w.get('Quad').current.destroy();
  assert.equal(st.subs.size, 0, 'no listener left');
  assert.equal(st.mos.every((m) => m.gone), true);
  assert.equal(st.adds, st.removes, 'every add has its remove');
  env.flip(true);                                                        // nothing to call any more
});

test('without matchMedia or MutationObserver the page mounts and behaves as before', () => {
  const env = quadWorld({ hash: '#/quad', extra: { matchMedia: undefined, MutationObserver: undefined } });
  assert.equal(page(env.w).querySelector('.quad') !== null, true);
  env.w.document.documentElement.classList.add('force-coarse');
  env.w.get('Quad').current.sync();
  assert.equal(page(env.w).querySelector('.quad').getAttribute('data-touch'), 'true', 'the sync pass still follows the pointer');
});

test('a tile Open link carries data-standalone="skip" next to data-dock="skip"; the PWA click handler leaves it alone, a /term/ link elsewhere still navigates in place', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const T1 = names(w)[0];
  const open = tileOf(w, T1).querySelector('a.qt-open');
  assert.equal(open.getAttribute('data-standalone'), 'skip');
  assert.equal(open.getAttribute('data-dock'), 'skip');
  assert.equal(open.getAttribute('href'), `/term/${T1}`);
  w.run('globalThis.__assigned = []; globalThis.__docOn = {}; location.assign = (u) => { __assigned.push(u); }; navigator.standalone = true; document.addEventListener = (t, f) => { (__docOn[t] ||= []).push(f); }; installLifecycleListeners();');
  const click = (n) => { const e = { type: 'click', target: n, prevented: 0, preventDefault() { this.prevented += 1; } }; for (const f of w.get('__docOn').click) f(e); return e; };
  const e1 = click(open);
  assert.equal(e1.prevented, 0, 'the click falls through to target=_blank');
  assert.deepEqual(plain(w.get('__assigned')), []);
  const other = w.document.createElement('a');
  other.setAttribute('href', `/term/${T1}`);
  other.setAttribute('target', '_blank');
  page(w).append(other);
  const e2 = click(other);
  assert.equal(e2.prevented, 1);
  assert.deepEqual(plain(w.get('__assigned')), [`/term/${T1}`]);
});

test('the project/repo of a tile header is .pend (hidden by CSS) until TermKit.fitName has measured it, then the class is gone', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w } = env;
  const T1 = names(w)[0];
  const where = tileOf(w, T1).querySelector('.qt-where');
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'quad.css'), 'utf8');
  assert.match(css, /\.qt-where\.pend \{ visibility:hidden; \}/, 'hidden (not removed): no layout jump when it shows');
  const T = w.get('TermKit');
  const fresh = w.document.createElement('span');
  fresh.setAttribute('class', 'qt-where pend');
  fresh.getBoundingClientRect = () => ({ width: 120 });
  assert.equal(fresh.classList.contains('pend'), true);
  T.fitName(fresh);
  assert.equal(fresh.classList.contains('pend'), false, 'the first measurement shows it');
  assert.equal(where.classList.contains('pend') || where.classList.contains('qt-where'), true);
});

// ---------------------------------------------------------------- issue #137: one name on two nodes is two tiles

test('the same session name on this board and on a node: two tiles, the node\'s never opens a terminal or a drop on this board\'s session, and the store keeps both shapes', () => {
  const st = fixtureState();
  const twin = { ...st.projects.flatMap((p) => [p.root, ...p.repos]).filter(Boolean).flatMap((r) => r.sessions).find((s) => s.tmux === S1), node: 'box' };
  st.projects.find((p) => p.name === 'ccboard').repos.find((r) => r.name === 'ccboard').sessions.push(twin);
  const { w } = quadWorld({ hash: '#/quad', state: st, storage: { 'ccboard:quad:all': { layout: 2, slots: pad(S1, { node: 'box', tmux: S1 }), modes: { ['box/' + S1]: 'ro' }, zoom: null } } });
  const all = page(w).querySelectorAll('.qtile');
  assert.equal(all.length, 2, 'both tiles exist');
  const [here, there] = ['', 'box'].map((n) => all.find((t) => (t.getAttribute('data-node') || '') === n));
  assert.ok(here && there);
  assert.equal(here.getAttribute('data-tmux'), S1);
  assert.equal(there.getAttribute('data-tmux'), S1);
  assert.equal(here.getAttribute('data-node'), null, 'this board\'s tile carries no node: a drop on it is what it always was');
  assert.equal(there.getAttribute('data-node'), 'box', 'the other tile is another drop target');
  assert.ok(here.querySelector('iframe'), 'this board\'s tile has its terminal');
  assert.equal(there.querySelector('iframe'), null, 'the node\'s tile never opens a terminal by a name it only shares');
  assert.match(text(there), /runs on another node/);
  assert.equal(frameOf(w, S1).src, GRID(S1));
  // the store: this board's slot is still a string, the node's is {node, tmux}, and the node's mode is kept under its key
  const saved = savedOf(w);
  assert.equal(saved.slots[0], S1);
  assert.deepEqual(saved.slots[1], { node: 'box', tmux: S1 });
  assert.equal(saved.modes['box/' + S1], 'ro');
});
