// Contract tests for the v0.5.9 quad page (app/static/pages/quad.js, route #/quad?s=a,b,c,d&l=1|2|4&p=<project>): the pure helpers (layout capping,
// the query, slot state, auto-fill, the task line, size chip, auto-fit rule, shortcuts), then the page itself on minidom's DOM with the real scripts in
// index.html order: tiles per slot with the exact grid ttyUrl, iframes that are never re-created or reordered, the teardown order (about:blank before
// remove), persistence and URL precedence, zoom as an overlay, the mode pill and the tail, the one-up chip switcher under 840 px, the active tile and the
// host key bar, Allow / Deny / In terminal, auto-fit, the hidden-tab reload and the shortcuts. Timers, Date and ResizeObserver are fakes; api(), toast and
// Live.subscribe are recorders; every iframe gets a fake contentWindow before its load event.
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

function quadWorld({ wide = true, state = fixtureState(), storage = {}, hash = null, withKeymap = true, inbox = false, shell = false } = {}) {
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
  });
  installDom(w);
  for (const [k, v] of Object.entries(storage)) w.localStorage.setItem(k, typeof v === 'string' ? v : JSON.stringify(v));
  for (const f of ['core.js', 'components.js', ...(withKeymap ? ['keymap.js'] : []), 'live.js', 'termkit.js', 'router.js', 'pages/agents.js', ...(inbox ? ['pages/inbox.js'] : []), 'pages/quad.js', 'pages/placeholders.js']) w.load(f);
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

test('layoutFor: under 840 px (the phone and 600-839) is the one-up chip switcher, from 840 the wanted layout 1, 2 or 4, default 2', () => {
  const { Q } = quadWorld();
  const f = (...a) => plain(Q.layoutFor(...a));
  for (const width of [320, 390, 599, 600, 768, 839]) assert.deepEqual(f(width, 4), { n: 1, cols: 1, rows: 1, oneUp: true, forced: true, capped: true }, `${width}: one-up whatever was asked for`);
  assert.deepEqual(f(840, 2), { n: 2, cols: 2, rows: 1, oneUp: false, forced: false, capped: false });
  assert.deepEqual(f(1280, 4), { n: 4, cols: 2, rows: 2, oneUp: false, forced: false, capped: false });
  assert.deepEqual(f(1280, 1), { n: 1, cols: 1, rows: 1, oneUp: true, forced: false, capped: false }, 'a chosen 1 is one tile with the chip switcher, but not forced');
  assert.equal(f(1280, undefined).n, 2, 'the default is 2-up');
  assert.equal(f(1280, 3).n, 2, 'only 1, 2 and 4 exist');
  assert.equal(f(1280, 6).n, 2);
  assert.equal(f(Number.NaN, 4).n, 1, 'an unknown width is one-up');
  assert.equal(f(1024, 2, 740).n, 2, 'the 260 px sidebar open at 1024: still two tiles');
  assert.deepEqual([f(1280, 2, 525).n, f(1280, 4, 525).oneUp, f(1280, 4, 525).forced], [1, true, true], 'the dock open leaves a column under 600 px: one-up');
  assert.equal(f(1280, 1, 525).forced, false, 'one tile needs no columns');
});

// ---------------------------------------------------------------- the query, the saved state

test('parseQuery keeps only what is valid: names, 1|2|4, a project word; four slots at most, no repeats', () => {
  const { Q } = quadWorld();
  const p = (q) => plain(Q.parseQuery(q));
  assert.deepEqual(p({ s: `${CK},${P3},,${P2}`, l: '4', p: 'petroit' }), { slots: [CK, P3, '', P2], layout: 4, project: 'petroit', any: true });
  assert.deepEqual(p({}), { slots: null, layout: null, project: null, any: false });
  assert.deepEqual(p({ s: `bad name,${CK},${CK},a--b--c,x--y--z,${P1}`, l: '3', p: 'no/pe' }), { slots: ['', CK, '', 'a--b--c'], layout: null, project: null, any: true }, 'garbage and repeats become empty slots, only four are read');
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
  assert.deepEqual(plain(Q.load('')), { layout: 4, slots: [CK, '', P3, ''], modes: { [CK]: 'full' }, zoom: 1 });
  assert.equal(Q.load('petroit'), null, 'another scope has its own key');
  Q.save('petroit', { layout: 1, slots: [P3], modes: {}, zoom: null });
  assert.ok(w.localStorage.getItem('ccboard:quad:petroit'));
  w.localStorage.setItem('ccboard:quad:all', '{not json');
  assert.equal(Q.load(''), null);
  w.localStorage.setItem('ccboard:quad:all', JSON.stringify({ layout: 3, slots: 'x', modes: [], zoom: 9 }));
  assert.deepEqual(plain(Q.load('')), { layout: 2, slots: ['', '', '', ''], modes: {}, zoom: null });
  const many = {};
  for (let i = 0; i < 40; i++) many[`a--b--s${i}`] = 'ro';
  Q.save('', { layout: 2, slots: [], modes: many, zoom: null });
  assert.equal(Object.keys(Q.load('').modes).length, 24, 'the remembered modes are capped, the newest stay');
  assert.ok(Q.load('').modes['a--b--s39']);
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
  assert.deepEqual([r.layout, r.slots], [2, [CK, P3, '', '']], 'nothing saved, no URL: the default 2-up, filled by needs-you');
  r = run({ query: {}, saved: { layout: 4, slots: [WT, '', '', ''], modes: { [WT]: 'ro' }, zoom: 0 }, state: st });
  assert.deepEqual([r.layout, r.slots, r.modes, r.zoom], [4, [WT, CK, P3, P2], { [WT]: 'ro' }, 0], 'saved: its layout, modes and zoom; the empty slots fill around the kept one');
  r = run({ query: { s: `${P1},${S1}`, l: '2' }, saved: { layout: 4, slots: [WT, CK, P3, P2], modes: {}, zoom: null }, state: st });
  assert.deepEqual([r.layout, r.slots], [2, [P1, S1, '', '']], 'the URL wins; slots 3 and 4 are not visible, so not filled');
  assert.deepEqual([r.fromUrl.slots, r.fromUrl.layout], [true, true]);
  r = run({ query: { l: '1' }, saved: { layout: 4, slots: [WT, CK, P3, P2], modes: {}, zoom: 2 }, state: st });
  assert.deepEqual([r.layout, r.slots[0], r.zoom], [1, WT, null], 'only the layout came from the URL; a zoom on a hidden slot is dropped');
  r = run({ query: {}, saved: { layout: 2, slots: ['gone--x--y', S1, '', ''], modes: {}, zoom: null }, state: st });
  assert.deepEqual(r.slots, [CK, S1, '', ''], 'a session that is no longer there leaves its slot to auto-fill');
  r = run({ query: { s: 'gone--x--y' }, saved: null, state: st });
  assert.deepEqual(r.slots.slice(0, 2), ['gone--x--y', CK], 'a URL slot is kept even when its session is not running (the tile says so)');
  r = run({ query: {}, saved: null, state: st, project: 'petroit' });
  assert.deepEqual(r.slots.slice(0, 2), [P3, P2], 'inside a project only its sessions fill the slots');
  r = run({ query: { s: CK }, saved: null, state: null });
  assert.deepEqual(r.slots, [CK, '', '', ''], 'before the first poll nothing is filled or dropped');
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

test('shortcutOf: Ctrl+Alt+1..4, Z, K and R by event.code (Option+digit types another character on a Mac); nothing else', () => {
  const { Q } = quadWorld();
  const s = (e) => plain(Q.shortcutOf({ ctrlKey: true, altKey: true, metaKey: false, shiftKey: false, ...e }));
  assert.deepEqual(s({ code: 'Digit3', key: '£' }), { act: 'focus', n: 3 });
  assert.deepEqual(s({ key: '2' }), { act: 'focus', n: 2 });
  assert.deepEqual(s({ code: 'KeyZ', key: 'Ω' }), { act: 'zoom' });
  assert.deepEqual(s({ code: 'KeyK', key: 'z' }), { act: 'attention' }, 'the code decides, not the character');
  assert.deepEqual(s({ key: 'R' }), { act: 'reload' });
  assert.equal(s({ code: 'Digit5' }), null);
  assert.equal(s({ code: 'Digit0' }), null);
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
  assert.deepEqual(plain(w.get('__fit')), ['qt-where']);
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
  assert.equal(before, 4, 'four help-only entries while the page is up');
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
  assert.deepEqual(savedOf(w), { layout: 2, slots: [CK, P3, '', ''], modes: {}, zoom: null });
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
  assert.deepEqual(savedOf(b.w).slots, [P3, P2, '', ''], 'and the saved state follows');
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
  assert.deepEqual(savedOf(w).slots, [CK, '', '', '']);
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
  assert.deepEqual(help.map((h) => h.keys[0]), ['Ctrl+Alt+1…4', 'Ctrl+Alt+Z', 'Ctrl+Alt+K', 'Ctrl+Alt+R']);
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

test('a quad column under 600 px (the dock open) is one-up even in a wide window', () => {
  const env = quadWorld({ hash: '#/quad' });
  const { w, ros } = env;
  assert.deepEqual(names(w), [CK, P3]);
  const host = page(w).querySelector('.quad');
  host.clientWidth = 525;
  const rootRO = ros.find((r) => r.nodes.includes(host));
  rootRO.cb([{ contentRect: { width: 525, height: 600 } }]);
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
  assert.deepEqual(plain(Q.load('')), { layout: 2, slots: [CK, '', '', ''], modes: {}, zoom: null });
  assert.deepEqual(plain(w.get('__nav')), ['#/quad']);
  Q.addToQuad(P3, '');
  assert.deepEqual(plain(Q.load('')).slots, [CK, P3, '', '']);
  Q.addToQuad(P2, '');
  assert.deepEqual(plain(Q.load('')), { layout: 4, slots: [CK, P3, P2, ''], modes: {}, zoom: null }, 'two full: 4-up');
  Q.addToQuad(P2, '');
  assert.deepEqual(plain(Q.load('')).slots, [CK, P3, P2, ''], 'already there: unchanged');
  Q.addToQuad(P1, 'petroit');
  assert.deepEqual(plain(Q.load('petroit')).slots, [P1, '', '', ''], 'a project has its own slots');
  assert.equal(plain(w.get('__nav')).at(-1), '#/quad?p=petroit');
  assert.equal(Q.addToQuad('not a name', ''), false);
  Q.addToQuad(S1, '');
  assert.deepEqual(plain(Q.load('')).slots, [CK, P3, P2, S1], 'the last free slot');
  Q.addToQuad(WT, '');
  assert.deepEqual(plain(Q.load('')).slots, [CK, P3, P2, WT], 'every slot taken: the last one is replaced');
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
  const css = fs.readFileSync(path.join(STATIC, 'pages.css'), 'utf8');
  const quad = css.slice(css.indexOf('/* ---------- quad (v0.5.9'));
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
  const css = fs.readFileSync(path.join(STATIC, 'pages.css'), 'utf8');
  const quad = css.slice(css.indexOf('/* ---------- quad (v0.5.9'));
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
  assert.deepEqual(plain(Q.slotsState({ saved, state: st, project: 'petroit', n: 2 })).slots, [P2, P3, '', ''], 'CK is phasezero: gone; the empty slot is auto-filled with petroit\'s first');
  assert.deepEqual(plain(Q.slotsState({ saved, state: st, project: '', n: 2 })).slots, [CK, P3, '', ''], 'all projects: nothing is dropped');
  assert.deepEqual(plain(Q.slotsState({ query: { s: `${CK},${P3}` }, state: st, project: 'petroit', n: 2 })).slots, [P2, P3, '', ''], 'the address is held to the scope too');
  assert.deepEqual(plain(Q.slotsState({ query: { s: `${CK},${P3}` }, state: null, project: 'petroit', n: 2 })).slots, ['', P3, '', ''], 'before the first poll the name says which project a slot is');
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
  assert.deepEqual(plain(Q.load('scope')).slots, [P1, '', '', '']);
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
  assert.deepEqual(plain(savedOf(w, 'petroit').slots), [P1, P3, P2, ''], 'the project\'s own saved state');
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
  const css = fs.readFileSync(path.join(STATIC, 'pages.css'), 'utf8');
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
