// Contract tests for the terminal dock (v0.5.9, shell.js: Shell.openDock / closeDock / toggleDock / dockOn / setDockWidth / buildDockResizer / onTermLink / quadAdd, the
// sidebar rule below 1440 px, ccboard:dock, ccboard:dock:w, ccboard:dock:off) on the real core, components, live, termkit, dnd, router, agents, shell and session scripts on
// minidom's DOM. matchMedia is a controllable table (setWidth() flips every min-width query and fires 'change'); timers and Date are fake, so focus handling and the long
// press can be driven. The pane itself (termPane) is covered in termkit.test.mjs; here it is the thing the dock mounts.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { ISO, projectsOf, sess, fakeState } from './world.mjs';

// ---------------------------------------------------------------- the world

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

const SCRIPTS = ['core.js', 'components.js', 'live.js', 'termkit.js', 'launcher.js', 'dnd.js', 'router.js', 'pages/widgets.js', 'pages/agents.js', 'shell.js', 'pages/session.js'];
const S1 = 'shop--api--s1';
const S2 = 'shop--api--s2';
const S3 = 'shop--web--s3';
const ENDED = 'shop--api--old';

function stateOf(rows = {}) {
  const s1 = sess('shop', 'api', 's1', { state: 'idle', ...(rows.s1 || {}) });
  const s2 = sess('shop', 'api', 's2', { state: 'waiting', needs_attention: true, ...(rows.s2 || {}) });
  const s3 = sess('shop', 'web', 's3', { state: 'working', ...(rows.s3 || {}) });
  const old = sess('shop', 'api', 'old', { state: 'ended' });
  return fakeState({ projects: projectsOf({ shop: { api: [s1, s2, old], web: [s3] } }), ...(rows.over || {}) });
}

/** installShell() on a skeleton with #app; `termkit: false` leaves termkit.js out (a partial deploy); `store` preloads localStorage. */
function dWorld({ width = 1280, termkit = true, state = null, store = {}, standalone = false, install = true } = {}) {
  const clock = fakeClock();
  const BASE = Date.now();
  class FakeDate extends Date { static now() { return BASE + clock.now; } }
  const lists = new Map();
  let now = width;
  const matchMedia = (q) => {
    let mq = lists.get(q);
    if (!mq) {
      const m = /\(min-width:\s*(\d+)px\)/.exec(q);
      const min = m ? Number(m[1]) : null;
      mq = { media: q, listeners: [], get matches() { return min !== null && now >= min; }, addEventListener(t, fn) { if (t === 'change') this.listeners.push(fn); }, removeEventListener() {} };
      lists.set(q, mq);
    }
    return mq;
  };
  const w = makeWorld({ matchMedia, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    Date: FakeDate, innerWidth: width, navigator: { userAgent: 'node-test', onLine: true, standalone } });
  const dom = installDom(w);
  const app = w.document.createElement('div');
  app.setAttribute('id', 'app');
  app.props = {};
  app.style = { setProperty(k, v) { app.props[k] = v; }, removeProperty(k) { delete app.props[k]; } };
  dom.body.append(app);
  for (const [k, v] of Object.entries(store)) w.localStorage.setItem(k, v);
  for (const f of SCRIPTS) { if (f === 'termkit.js' && !termkit) continue; w.load(f); }
  w.ctx.__calls = []; w.ctx.__toasts = []; w.ctx.__opened = [];
  w.run(`
    api = async (method, path, body) => { __calls.push({ method, path, body }); return { ok: true }; };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    openPage = (url) => { __opened.push(url); };
    poll = async () => {};
    Live.subscribe = () => () => {};
  `);
  if (state) { w.ctx.__st = state; w.run('state = __st'); }
  const setWidth = (v) => {
    now = v;
    w.ctx.innerWidth = v;
    for (const mq of lists.values()) for (const fn of [...mq.listeners]) fn({ matches: mq.matches });
  };
  if (install) w.run('installShell()');
  const Shell = w.get('Shell');
  const q = (sel) => w.document.querySelector(sel);
  return { w, dom, clock, Shell, app, setWidth, q, dock: () => q('#dock'), body: () => w.document.body };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));
const click = (n, extra = {}) => { const e = { type: 'click', button: 0, prevented: 0, stopped: 0, preventDefault() { this.prevented += 1; }, stopPropagation() { this.stopped += 1; }, ...extra }; n.dispatchEvent(e); return e; };
const tmuxOf = (d) => { const p = d.dock().querySelector('.tpane'); return p ? p.getAttribute('data-tmux') : null; };
const hidden = (n) => n.classList.contains('hidden');
const toasts = (w) => plain(w.get('__toasts')).map((t) => t.text);
const stored = (w, k) => w.localStorage.getItem(k);

/** a /term/ link inside #page, the way a session row draws Open */
function openLink(d, href = `/term/${S1}`) {
  const a = d.w.document.createElement('a');
  a.setAttribute('href', href);
  a.setAttribute('target', '_blank');
  d.q('#page').append(a);
  return a;
}

// ---------------------------------------------------------------- availability

test('the dock exists from 1024 px up, in a browser where it is not switched off and where termkit.js is loaded', () => {
  const d = dWorld({ width: 1280 });
  assert.equal(d.Shell.dockOn(), true);
  d.setWidth(1023);
  assert.equal(d.Shell.dockOn(), false);
  d.setWidth(1024);
  assert.equal(d.Shell.dockOn(), true);
  d.w.localStorage.setItem('ccboard:dock:off', '1');
  assert.equal(d.Shell.dockOn(), false);
  d.w.localStorage.removeItem('ccboard:dock:off');
  assert.equal(d.Shell.dockOn(), true);
  assert.equal(dWorld({ termkit: false }).Shell.dockOn(), false, 'without TermKit there is no dock');
});

test('openDock mounts a full-mode pane for the session in #dock: unhidden, one iframe at the full URL, remembered in ccboard:dock', () => {
  const d = dWorld();
  assert.equal(hidden(d.dock()), true, 'hidden until opened');
  assert.equal(d.Shell.openDock(S1), true);
  const dock = d.dock();
  assert.equal(hidden(dock), false);
  assert.ok(dock.classList.contains('has-term'));
  assert.equal(dock.querySelectorAll('.tpane').length, 1);
  assert.equal(dock.querySelector('.tpane').getAttribute('data-mode'), 'full');
  assert.equal(dock.querySelectorAll('iframe').length, 1);
  assert.equal(dock.querySelector('iframe').src, `/tty/?arg=${S1}`);
  assert.equal(stored(d.w, 'ccboard:dock'), S1);
  assert.equal(d.body().getAttribute('data-dock'), 'open');
  assert.equal(d.Shell.dockOpen(), true);
  assert.equal(d.Shell.dock.tmux, S1);
  assert.ok(dock.querySelector('.dock-resize'), 'the drag handle is in the dock');
});

test('opening another session replaces the pane (the old iframe is blanked first); the same session again keeps the pane', () => {
  const d = dWorld();
  d.Shell.openDock(S1);
  const first = d.dock().querySelector('iframe');
  const pane = d.Shell.dock.pane;
  assert.equal(d.Shell.openDock(S1), true);
  assert.equal(d.Shell.dock.pane, pane, 'no remount for the same session');
  assert.equal(d.dock().querySelector('iframe'), first);
  d.Shell.openDock(S2);
  assert.equal(first.src, 'about:blank', 'the old terminal was blanked before it left');
  assert.equal(first.parentNode, null);
  assert.equal(d.dock().querySelectorAll('iframe').length, 1);
  assert.equal(tmuxOf(d), S2);
  assert.equal(stored(d.w, 'ccboard:dock'), S2);
});

test('below 1024 px, with ccboard:dock:off, or without TermKit the dock does not open (and says why once); a bad name is refused', () => {
  const d = dWorld({ width: 900 });
  assert.equal(d.Shell.openDock(S1), false);
  assert.match(toasts(d.w).join('|'), /1024 px/);
  assert.equal(d.dock().querySelectorAll('iframe').length, 0);
  assert.equal(stored(d.w, 'ccboard:dock'), null);
  const off = dWorld({ store: { 'ccboard:dock:off': '1' } });
  assert.equal(off.Shell.openDock(S1), false);
  assert.match(toasts(off.w).join('|'), /ccboard:dock:off/);
  assert.equal(off.Shell.openDock(S1, { quiet: true }), false);
  assert.equal(toasts(off.w).length, 1, 'quiet: no second toast');
  const bare = dWorld({ termkit: false });
  assert.equal(bare.Shell.openDock(S1), false);
  assert.match(toasts(bare.w).join('|'), /terminal kit is not loaded/);
  const ok = dWorld();
  for (const bad of ['', null, undefined, 5]) assert.equal(ok.Shell.openDock(bad), false);
  assert.equal(ok.Shell.openDock('x'.repeat(10), { quiet: true }), true, 'any non-empty name is a session name here: the server decides');
});

test('closeDock: the iframe is blanked and removed, #dock hides, the saved session is forgotten, the next mod+j brings it back', () => {
  const d = dWorld({ state: stateOf() });
  d.Shell.openDock(S1);
  const frame = d.dock().querySelector('iframe');
  assert.equal(d.Shell.closeDock(), true);
  assert.equal(frame.src, 'about:blank');
  assert.equal(frame.parentNode, null);
  assert.equal(hidden(d.dock()), true);
  assert.equal(d.dock().children.length, 0);
  assert.equal(d.dock().classList.contains('has-term'), false);
  assert.equal(stored(d.w, 'ccboard:dock'), null);
  assert.equal(d.body().getAttribute('data-dock'), 'closed');
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(d.Shell.toggleDock(), true);
  assert.equal(tmuxOf(d), S1, 'the one that was shown');
});

test('the close button of the pane closes the dock', () => {
  const d = dWorld();
  d.Shell.openDock(S1);
  click(d.dock().querySelector('[data-act=close]'));
  assert.equal(hidden(d.dock()), true);
  assert.equal(d.Shell.dockOpen(), false);
});

test('setDockOff(true) closes an open dock and keeps it closed; setDockOff(false) lets it open again', () => {
  const d = dWorld();
  d.Shell.openDock(S1);
  d.Shell.setDockOff(true);
  assert.equal(stored(d.w, 'ccboard:dock:off'), '1');
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(d.Shell.openDock(S1, { quiet: true }), false);
  d.Shell.setDockOff(false);
  assert.equal(stored(d.w, 'ccboard:dock:off'), null);
  assert.equal(d.Shell.openDock(S1), true);
});

test('openTerm: the dock when it is on; its own page (openPage) when it is not', () => {
  const d = dWorld();
  assert.equal(d.Shell.openTerm(S1), true);
  assert.equal(tmuxOf(d), S1);
  assert.deepEqual(plain(d.w.get('__opened')), []);
  const n = dWorld({ width: 800 });
  assert.equal(n.Shell.openTerm(S2), false);
  assert.deepEqual(plain(n.w.get('__opened')), [`/term/${S2}`]);
  assert.equal(n.Shell.dockOpen(), false);
  assert.deepEqual(toasts(n.w), [], 'no toast: the page opened, nothing failed');
  const off = dWorld({ store: { 'ccboard:dock:off': '1' } });
  assert.equal(off.Shell.openTerm(S3), false);
  assert.deepEqual(plain(off.w.get('__opened')), [`/term/${S3}`]);
});

test('a session the pane cannot show leaves the dock as it was (the new pane is built before the old one goes)', () => {
  const d = dWorld();
  d.Shell.openDock(S1);
  const f = d.dock().querySelector('iframe');
  d.w.run('TermKit.termPane = () => { throw new Error("bad session name"); }');
  assert.equal(d.Shell.openDock(S2, { quiet: true }), false);
  assert.equal(tmuxOf(d), S1);
  assert.equal(d.dock().querySelector('iframe'), f);
  assert.equal(hidden(d.dock()), false);
  assert.deepEqual(toasts(d.w), ['Not a terminal session name']);
});

// ---------------------------------------------------------------- mod+j and the pick

test('toggleDock: closes an open dock; with nothing remembered it opens the session that needs you most', () => {
  const d = dWorld({ state: stateOf() });
  assert.equal(d.Shell.toggleDock(), true);
  assert.equal(tmuxOf(d), S2, 'the waiting session that needs attention comes before the idle and the working one');
  assert.equal(d.Shell.toggleDock(), true);
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(d.Shell.toggleDock(), true);
  assert.equal(tmuxOf(d), S2, 'reopens the last one shown');
});

test('toggleDock with no live session says so and opens nothing; below 1024 px it explains instead', () => {
  const d = dWorld({ state: fakeState({ projects: projectsOf({ shop: { api: [sess('shop', 'api', 'old', { state: 'ended' })] } }) }) });
  assert.equal(d.Shell.toggleDock(), false);
  assert.deepEqual(toasts(d.w), ['No live session to show']);
  const n = dWorld({ width: 800, state: stateOf() });
  assert.equal(n.Shell.toggleDock(), false);
  assert.match(toasts(n.w).join('|'), /1024 px/);
});

test('dockPick: needs you first, then errored, working, idle; ended sessions never; the newest first inside a rank; empty without state', () => {
  const d = dWorld();
  assert.equal(d.Shell.dockPick(null), '');
  assert.equal(d.Shell.dockPick(stateOf()), S2);
  assert.equal(d.Shell.dockPick(stateOf({ s2: { state: 'idle', needs_attention: false } })), S3, 'working before idle');
  assert.equal(d.Shell.dockPick(stateOf({ s2: { state: 'idle', needs_attention: false }, s3: { state: 'idle' } })), S1, 'ties: the newest, then the name');
  assert.equal(d.Shell.dockPick(fakeState({ projects: projectsOf({ shop: { api: [sess('shop', 'api', 'old', { state: 'ended' })] } }) })), '');
});

test('mod+j is bound to Shell.toggleDock (keymap.js)', () => {
  const src = fs.readFileSync(path.join(STATIC, 'keymap.js'), 'utf8');
  assert.match(src, /B\('mod\+j'[^\n]*Shell\.toggleDock\(\)/);
});

// ---------------------------------------------------------------- restore on load

test('the session the dock showed last time comes back on the first state, without taking the keyboard', () => {
  const d = dWorld({ store: { 'ccboard:dock': S3 }, install: false });
  const box = d.w.document.createElement('textarea');
  d.q('#page').append(box);
  box.focus();
  d.w.ctx.__st = stateOf();
  d.w.run('state = __st; installShell()');
  assert.equal(tmuxOf(d), S3);
  assert.equal(hidden(d.dock()), false);
  const f = d.dock().querySelector('iframe');
  assert.equal(f.src, `/tty/?arg=${S3}`);
  f.focus();                                                                 // ttyd focuses its terminal on load
  f.dispatchEvent({ type: 'load' });
  d.clock.advance(1);
  assert.equal(d.w.document.activeElement, box, 'the composer kept the keyboard');
});

test('a restore without a composer lets the terminal NOT take focus by itself (the board\'s shortcuts keep working)', () => {
  const d = dWorld({ store: { 'ccboard:dock': S1 }, state: stateOf() });
  const f = d.dock().querySelector('iframe');
  f.focus();
  f.dispatchEvent({ type: 'load' });
  d.clock.advance(1);
  assert.notEqual(d.w.document.activeElement, f);
});

test('a saved session that is gone or ended is forgotten, not opened', () => {
  for (const t of ['shop--api--nope', ENDED]) {
    const d = dWorld({ store: { 'ccboard:dock': t }, state: stateOf() });
    assert.equal(d.Shell.dockOpen(), false, t);
    assert.equal(stored(d.w, 'ccboard:dock'), null, t);
  }
});

test('restore happens once; a later state does not reopen what the person closed', () => {
  const d = dWorld({ store: { 'ccboard:dock': S1 }, state: stateOf() });
  assert.equal(d.Shell.dockOpen(), true);
  d.Shell.closeDock();
  d.w.run('renderShell(state)');
  assert.equal(d.Shell.dockOpen(), false);
});

test('a narrow window keeps the saved session for the width that fits: it opens when the window grows to 1024', () => {
  const d = dWorld({ width: 800, store: { 'ccboard:dock': S1 }, state: stateOf() });
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(stored(d.w, 'ccboard:dock'), S1, 'still saved');
  d.setWidth(1100);
  assert.equal(tmuxOf(d), S1);
});

test('with ccboard:dock:off the saved session is not opened and not kept for later', () => {
  const d = dWorld({ store: { 'ccboard:dock': S1, 'ccboard:dock:off': '1' }, state: stateOf() });
  assert.equal(d.Shell.dockOpen(), false);
  d.setWidth(1500);
  assert.equal(d.Shell.dockOpen(), false);
});

// ---------------------------------------------------------------- the width

test('the width is the CSS default until a drag: no --dock-w, no stored width', () => {
  const d = dWorld();
  d.Shell.openDock(S1);
  assert.equal(d.app.props['--dock-w'], undefined);
  assert.equal(stored(d.w, 'ccboard:dock:w'), null);
  const css = fs.readFileSync(path.join(STATIC, 'shell.css'), 'utf8');
  assert.match(css, /--dock-w:min\(44vw, 640px\)/, 'the default is min(44vw, 640px)');
  assert.match(css, /#dock:not\(\.hidden\) \{[^}]*width:var\(--dock-w\)/);
});

test('setDockWidth sets --dock-w on #app through the CSSOM, clamps to 320 px and 60 % of the window (960 at most), and persists only when asked', () => {
  const d = dWorld({ width: 1280 });
  assert.equal(d.Shell.setDockWidth(500, true), 500);
  assert.equal(d.app.props['--dock-w'], '500px');
  assert.equal(stored(d.w, 'ccboard:dock:w'), '500');
  assert.equal(d.Shell.setDockWidth(100, false), 320);
  assert.equal(d.app.props['--dock-w'], '320px');
  assert.equal(stored(d.w, 'ccboard:dock:w'), '500', 'not persisted without the flag');
  assert.equal(d.Shell.setDockWidth(5000, false), 768, '60 % of 1280');
  d.w.ctx.innerWidth = 2400;
  assert.equal(d.Shell.setDockWidth(5000, false), 960, 'the absolute cap');
  assert.equal(d.Shell.clampDockW('x'), 0);
  assert.equal(d.Shell.clampDockW(null), 0);
  assert.equal(d.Shell.clampDockW(-5), 0);
  assert.equal(d.Shell.clampDockW(400.4), 400);
  assert.equal(d.app.getAttribute('style'), null, 'never a style attribute');
});

test('resetDockWidth removes the variable and the saved value (not overwritten), so the viewport-relative default applies again', () => {
  const d = dWorld();
  d.Shell.setDockWidth(600, true);
  assert.equal(d.Shell.resetDockWidth(), 0);
  assert.equal(d.app.props['--dock-w'], undefined);
  assert.equal(stored(d.w, 'ccboard:dock:w'), null);
  assert.equal(d.Shell.dock.w, 0);
});

test('a saved width is applied when the shell is installed, clamped', () => {
  const d = dWorld({ store: { 'ccboard:dock:w': '610' } });
  assert.equal(d.app.props['--dock-w'], '610px');
  const bad = dWorld({ store: { 'ccboard:dock:w': 'wide' } });
  assert.equal(bad.app.props['--dock-w'], undefined);
  const tiny = dWorld({ store: { 'ccboard:dock:w': '40' } });
  assert.equal(tiny.app.props['--dock-w'], '320px');
});

/** the resizer with a believable geometry: #app spans 0..1280, #dock starts at 830 */
function resizer(d) {
  d.Shell.openDock(S1);
  d.app.getBoundingClientRect = () => ({ left: 0, right: 1280, top: 0, bottom: 800, width: 1280, height: 800 });
  d.dock().getBoundingClientRect = () => ({ left: 830, right: 1280, top: 0, bottom: 800, width: 450, height: 800 });
  return d.dock().querySelector('.dock-resize');
}
const ptr = (type, x, extra = {}) => ({ type, button: 0, clientX: x, pointerId: 1, preventDefault() {}, ...extra });

test('dragging the handle sizes the dock from the right edge: the width follows the pointer, the app is marked while dragging, the end persists it', () => {
  const d = dWorld();
  const h = resizer(d);
  assert.equal(h.getAttribute('role'), 'separator');
  assert.equal(h.getAttribute('aria-orientation'), 'vertical');
  assert.equal(h.getAttribute('tabindex'), '0');
  h.dispatchEvent(ptr('pointerdown', 832));                                  // taken 2 px into the strip
  assert.equal(d.app.classList.contains('dock-dragging'), true);
  h.dispatchEvent(ptr('pointermove', 700));
  assert.equal(d.app.props['--dock-w'], '582px');
  assert.equal(stored(d.w, 'ccboard:dock:w'), null, 'not persisted mid-drag');
  h.dispatchEvent(ptr('pointermove', 100));
  assert.equal(d.app.props['--dock-w'], '768px', 'clamped to 60 % of the window');
  h.dispatchEvent(ptr('pointermove', 1270));
  assert.equal(d.app.props['--dock-w'], '320px', 'and to the minimum');
  h.dispatchEvent(ptr('pointermove', 700));
  h.dispatchEvent(ptr('pointerup', 700));
  assert.equal(d.app.classList.contains('dock-dragging'), false);
  assert.equal(stored(d.w, 'ccboard:dock:w'), '582');
  assert.equal(h.getAttribute('aria-valuenow'), '582');
  h.dispatchEvent(ptr('pointermove', 600));
  assert.equal(d.app.props['--dock-w'], '582px', 'after the drag the handle is idle');
});

test('a click on the handle that never moves persists nothing; a pointercancel ends the drag like a release', () => {
  const d = dWorld();
  const h = resizer(d);
  h.dispatchEvent(ptr('pointerdown', 830));
  h.dispatchEvent(ptr('pointerup', 830));
  assert.equal(stored(d.w, 'ccboard:dock:w'), null);
  h.dispatchEvent(ptr('pointerdown', 830));
  h.dispatchEvent(ptr('pointermove', 780));
  h.dispatchEvent(ptr('pointercancel', 780));
  assert.equal(d.app.classList.contains('dock-dragging'), false);
  assert.equal(stored(d.w, 'ccboard:dock:w'), '500');
  h.dispatchEvent(ptr('pointerdown', 830, { button: 2 }));
  assert.equal(d.app.classList.contains('dock-dragging'), false, 'only the primary button drags');
});

test('the handle by keyboard: left grows, right shrinks (16 px, 48 with Shift), Home and End go to the ends, Enter and a double click reset', () => {
  const d = dWorld();
  const h = resizer(d);
  const key = (k, extra = {}) => { const e = { type: 'keydown', key: k, prevented: 0, preventDefault() { this.prevented += 1; }, ...extra }; h.dispatchEvent(e); return e; };
  assert.equal(key('ArrowLeft').prevented, 1);
  assert.equal(d.app.props['--dock-w'], '466px', 'from the dock\'s measured 450 px');
  key('ArrowLeft', { shiftKey: true });
  assert.equal(d.app.props['--dock-w'], '514px');
  key('ArrowRight');
  assert.equal(d.app.props['--dock-w'], '498px');
  assert.equal(stored(d.w, 'ccboard:dock:w'), '498', 'keys persist at once');
  key('Home');
  assert.equal(d.app.props['--dock-w'], '320px');
  key('End');
  assert.equal(d.app.props['--dock-w'], '768px');
  key('Enter');
  assert.equal(d.app.props['--dock-w'], undefined);
  assert.equal(stored(d.w, 'ccboard:dock:w'), null);
  d.Shell.setDockWidth(600, true);
  h.dispatchEvent({ type: 'dblclick' });
  assert.equal(d.app.props['--dock-w'], undefined);
  assert.equal(key('a').prevented, 0, 'other keys are left alone');
});

// ---------------------------------------------------------------- the sidebar

test('below 1440 px an open dock folds the sidebar to the rail without touching ccboard:sb; closing it brings the sidebar back', () => {
  const d = dWorld({ width: 1300 });
  assert.equal(d.body().getAttribute('data-sb'), 'full');
  d.Shell.openDock(S1);
  assert.equal(d.body().getAttribute('data-sb'), 'rail');
  assert.equal(d.q('#sidebar').classList.contains('rail'), true);
  assert.equal(stored(d.w, 'ccboard:sb'), null, 'the person\'s choice is untouched');
  assert.equal(d.Shell.sbOpen, true);
  d.Shell.closeDock();
  assert.equal(d.body().getAttribute('data-sb'), 'full');
  assert.equal(d.q('#sidebar').classList.contains('rail'), false);
});

test('from 1440 px up the sidebar and the dock sit side by side; crossing 1440 while the dock is open follows live', () => {
  const d = dWorld({ width: 1500 });
  d.Shell.openDock(S1);
  assert.equal(d.body().getAttribute('data-sb'), 'full');
  d.setWidth(1400);
  assert.equal(d.body().getAttribute('data-sb'), 'rail');
  d.setWidth(1440);
  assert.equal(d.body().getAttribute('data-sb'), 'full');
});

test('a sidebar the person collapsed stays collapsed with the dock open and after it closes', () => {
  const d = dWorld({ width: 1500, store: { 'ccboard:sb': '0' } });
  assert.equal(d.body().getAttribute('data-sb'), 'rail');
  d.Shell.openDock(S1);
  d.Shell.closeDock();
  assert.equal(d.body().getAttribute('data-sb'), 'rail');
});

test('asking for the sidebar while the dock holds the rail shows it until the dock closes; the [ key follows what is on screen', () => {
  const d = dWorld({ width: 1300 });
  d.Shell.openDock(S1);
  assert.equal(d.body().getAttribute('data-sb'), 'rail');
  d.Shell.toggleSidebar();                                                   // [ : what is on screen is the rail, so this expands
  assert.equal(d.body().getAttribute('data-sb'), 'full');
  assert.equal(d.Shell.dock.keep, true);
  assert.equal(stored(d.w, 'ccboard:sb'), '1', 'the person asked for it: that is saved');
  d.Shell.toggleSidebar();                                                   // now it collapses, for real
  assert.equal(d.body().getAttribute('data-sb'), 'rail');
  assert.equal(stored(d.w, 'ccboard:sb'), '0');
  assert.equal(d.Shell.dock.keep, false);
  d.Shell.setSidebar(true);
  d.Shell.closeDock();
  assert.equal(d.Shell.dock.keep, false, 'closing the dock ends the exception');
  d.Shell.openDock(S1);
  assert.equal(d.body().getAttribute('data-sb'), 'rail', 'the rule applies again with the next dock');
});

test('the rail rule only concerns the wide shells: a medium window has its rail anyway, a compact one no sidebar', () => {
  const m = dWorld({ width: 1100 });                                          // large? no: 1100 is expanded (840..1199)
  m.Shell.openDock(S1);
  assert.equal(m.body().getAttribute('data-sb'), 'rail');
  m.Shell.closeDock();
  assert.equal(m.body().getAttribute('data-sb'), 'full');
  const c = dWorld({ width: 500 });
  assert.equal(c.body().getAttribute('data-sb'), 'none');
});

// ---------------------------------------------------------------- crossing 1024

test('a window that shrinks below 1024 px takes the terminal down (no hidden full-mode client), keeps the session saved, and brings it back with the width', () => {
  const d = dWorld({ width: 1280, state: stateOf() });
  d.Shell.openDock(S1);
  const frame = d.dock().querySelector('iframe');
  d.setWidth(900);
  assert.equal(frame.src, 'about:blank');
  assert.equal(frame.parentNode, null);
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(hidden(d.dock()), true);
  assert.equal(stored(d.w, 'ccboard:dock'), S1, 'still saved');
  assert.equal(d.body().getAttribute('data-dock'), 'closed');
  d.setWidth(1280);
  assert.equal(tmuxOf(d), S1);
  assert.equal(d.dock().querySelectorAll('iframe').length, 1);
  assert.notEqual(d.dock().querySelector('iframe'), frame, 'a fresh terminal');
});

test('crossing 1024 with the dock closed does nothing; ccboard:dock:off set by hand takes an open dock down on the next resize', () => {
  const d = dWorld({ width: 900 });
  d.setWidth(1300);
  assert.equal(d.Shell.dockOpen(), false);
  d.Shell.openDock(S1);
  d.w.localStorage.setItem('ccboard:dock:off', '1');
  d.setWidth(1310);
  assert.equal(d.Shell.dockOpen(), false);
  d.w.localStorage.removeItem('ccboard:dock:off');
  d.setWidth(1320);
  assert.equal(tmuxOf(d), S1, 'switched on again: it comes back');
});

// ---------------------------------------------------------------- the header, the state

test('the pane header is a drop target for a Backlog card: data-drop=session and data-tmux, bound by dnd.js', () => {
  const d = dWorld();
  d.Shell.openDock(S1);
  const head = d.dock().querySelector('.tp-head');
  assert.equal(head.getAttribute('data-drop'), 'session');
  assert.equal(head.getAttribute('data-tmux'), S1);
  assert.deepEqual(plain(d.w.get('Dnd').targetOf(head)), { kind: 'session', tmux: S1 });
  d.Shell.openDock(S2);
  assert.equal(d.dock().querySelector('.tp-head').getAttribute('data-tmux'), S2, 'the new pane\'s header names the new session');
});

test('every render paints the open pane from the state: glyphs, context, task line, the pending permission; a session that left shows as gone', async () => {
  const d = dWorld({ state: stateOf() });
  d.Shell.openDock(S2);
  const root = d.dock().querySelector('.tpane');
  assert.deepEqual(root.querySelector('.tp-g').querySelectorAll('.glyph').map((n) => n.getAttribute('aria-label')), ['needs you', 'claude']);
  assert.equal(root.querySelector('.tp-ctx').textContent, 'ctx 42%');
  assert.equal(root.querySelector('.tp-perm').classList.contains('hidden'), true);
  const st = stateOf({ s2: { state: 'waiting' }, over: { pending_permissions: [{ id: 41, tmux_name: S2, tool_name: 'Bash', summary: 'Bash: npm test' }] } });
  d.w.ctx.__st = st;
  d.w.run('state = __st; renderShell(state)');
  assert.equal(root.querySelector('.tp-perm').classList.contains('hidden'), false);
  assert.equal(root.querySelector('.tp-perm-text').textContent, 'Bash: npm test');
  click(root.querySelector('.tp-allow'));
  await settle();
  assert.deepEqual(plain(d.w.get('__calls')).filter((c) => c.method === 'POST').map((c) => c.path), ['/api/permission/41/allow']);
  d.w.ctx.__st = fakeState({ projects: projectsOf({ shop: { web: [sess('shop', 'web', 's3')] } }) });
  d.w.run('state = __st; renderShell(state)');
  assert.equal(root.classList.contains('gone'), true);
  assert.equal(d.Shell.dockOpen(), true, 'the dock itself stays: the person closes it');
});

// ---------------------------------------------------------------- the pane's buttons

test('pop out opens /term/<name> in its own window and lets the dock go (two full clients on one session would fight over its size)', () => {
  const d = dWorld();
  d.Shell.openDock(S1);
  const pop = d.dock().querySelector('[data-act=popout]');
  assert.equal(pop.getAttribute('href'), `/term/${S1}`);
  const e = click(pop);
  assert.equal(e.prevented, 1);
  assert.deepEqual(plain(d.w.get('__opened')), [`/term/${S1}`]);
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(stored(d.w, 'ccboard:dock'), null);
});

test('the dock\'s own pop-out link is never taken for a row\'s Open link', () => {
  const d = dWorld();
  d.Shell.openDock(S1);
  const pop = d.dock().querySelector('[data-act=popout]');
  assert.equal(d.Shell.onTermLink({ target: pop, button: 0, preventDefault() { throw new Error('must not intercept'); } }), false);
});

test('add to quad hands the session to #/quad as ?s= with the saved slots in front; l=4 once there are more than two; a full quad gives up its last slot', () => {
  const d = dWorld();
  const slotsOf = () => new URLSearchParams(d.w.location.hash.split('?')[1]);
  d.Shell.openDock(S1);
  click(d.dock().querySelector('[data-act=quad]'));
  assert.match(d.w.location.hash, /^#\/quad\?/);
  assert.equal(slotsOf().get('s'), S1);
  assert.equal(slotsOf().get('l'), null);
  assert.equal(d.Shell.dockOpen(), false, 'the quad shows it: the dock lets go');
  d.w.localStorage.setItem('ccboard:quad:all', JSON.stringify({ layout: 2, slots: ['a--b--c', S2], modes: {}, zoom: null }));
  d.Shell.quadAdd(S3);
  assert.equal(slotsOf().get('s'), `a--b--c,${S2},${S3}`);
  assert.equal(slotsOf().get('l'), '4');
  d.w.localStorage.setItem('ccboard:quad:all', JSON.stringify({ layout: 4, slots: ['a--b--c', 'd--e--f', 'g--h--i', 'j--k--l'] }));
  d.Shell.quadAdd(S1);
  assert.equal(slotsOf().get('s'), `a--b--c,d--e--f,g--h--i,${S1}`);
  d.Shell.quadAdd('d--e--f');
  assert.equal(slotsOf().get('s'), 'a--b--c,d--e--f,g--h--i,j--k--l', 'already in the quad: unchanged');
  d.w.localStorage.setItem('ccboard:quad:all', '{not json');
  d.Shell.quadAdd(S2);
  assert.equal(slotsOf().get('s'), S2, 'an unreadable entry is no slots');
  d.w.localStorage.setItem('ccboard:quad:all', JSON.stringify({ slots: [null, 5, '', S3] }));
  assert.deepEqual(plain(d.Shell.quadSlots()), [S3]);
  assert.equal(d.Shell.quadAdd(''), false);
});

test('with the quad page loaded, add to quad is Quad.addToQuad(tmux, project): the page owns its saved slots; the dock still lets go', () => {
  const d = dWorld();
  d.w.run('globalThis.__quad = []; globalThis.Quad = { current: null, addToQuad(t, p) { __quad.push([t, p]); return true; } }');
  d.Shell.openDock(S1);
  click(d.dock().querySelector('[data-act=quad]'));
  assert.deepEqual(plain(d.w.get('__quad')), [[S1, '']]);
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(d.w.location.hash, '', 'the quad page navigates, not the shell');
  d.w.run('Quad.current = { project: "shop" }');
  d.Shell.openDock(S2);
  d.Shell.quadAdd(S2);
  assert.deepEqual(plain(d.w.get('__quad')).pop(), [S2, 'shop'], 'the quad that is up keeps its project scope');
  d.w.run('Quad.addToQuad = () => false');
  assert.equal(d.Shell.quadAdd(S3), false);
});

// ---------------------------------------------------------------- the quad page owns the window (defect 2 of the v0.5.9 review)

test('dockSuspend takes the open dock down at once (iframe blanked, #dock hidden, sidebar back on its own rule) and keeps the session for later; ccboard:dock stays', () => {
  const d = dWorld({ width: 1100 });
  d.Shell.openDock(S1);
  assert.equal(d.body().getAttribute('data-sb'), 'rail', 'the open dock folds the sidebar below 1440');
  const frame = d.dock().querySelector('iframe');
  assert.equal(d.Shell.dockSuspend(), true);
  assert.equal(d.Shell.dockHeld(), true);
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(frame.src, 'about:blank');
  assert.equal(frame.parentNode, null);
  assert.equal(hidden(d.dock()), true);
  assert.equal(d.body().getAttribute('data-dock'), 'closed');
  assert.equal(d.body().getAttribute('data-sb'), 'full', 'no dock, no forced rail: the quad gets the sidebar the person has');
  assert.equal(stored(d.w, 'ccboard:dock'), S1, 'kept: it comes back');
  assert.equal(d.Shell.dock.wanted, S1);
});

test('nothing opens the dock while the quad owns the window: openDock, openTerm (opens its tab), toggleDock (says why), a row\'s Open link (falls through)', () => {
  const d = dWorld({ state: stateOf() });
  d.Shell.dockSuspend();
  assert.equal(d.Shell.openDock(S1), false);
  assert.match(toasts(d.w).join('|'), /Quad page/);
  assert.equal(d.Shell.openDock(S1, { quiet: true }), false);
  assert.equal(toasts(d.w).length, 1, 'quiet: no second toast');
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(d.Shell.openTerm(S2), false);
  assert.deepEqual(plain(d.w.get('__opened')), [`/term/${S2}`], 'the key opens the terminal page in a tab instead');
  assert.equal(d.Shell.toggleDock(), false);
  assert.equal(toasts(d.w).length, 2);
  assert.equal(d.Shell.dockOpen(), false);
  const e = click(openLink(d));
  assert.equal(e.prevented, 0, 'the Open link keeps its own behaviour: a new tab');
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(stored(d.w, 'ccboard:dock'), null, 'a refused open remembers nothing');
});

test('mountedId === "quad" is the same test (the peek is an overlay: it never changes mountedId, so a peek over the quad is still the quad)', () => {
  const d = dWorld();
  d.w.run("mountedId = 'quad'");
  assert.equal(d.Shell.dockHeld(), true);
  assert.equal(d.Shell.openDock(S1, { quiet: true }), false);
  d.w.run("mountedId = 'agents'");
  assert.equal(d.Shell.dockHeld(), false);
  assert.equal(d.Shell.openDock(S1, { quiet: true }), true);
});

test('a link marked data-dock="skip" is never taken by the dock, even with the dock on (the quad tile\'s open link pops out)', () => {
  const d = dWorld();
  const a = openLink(d);
  a.setAttribute('data-dock', 'skip');
  assert.equal(click(a).prevented, 0);
  assert.equal(d.Shell.dockOpen(), false);
  a.removeAttribute('data-dock');
  assert.equal(click(a).prevented, 1);
});

test('dockResume brings the saved session back one tick later (the router moves mountedId after the quad\'s unmount), and not while the quad is still the page', () => {
  const d = dWorld({ width: 1100 });
  d.Shell.openDock(S1);
  d.Shell.dockSuspend();
  d.w.run("mountedId = 'quad'");
  d.Shell.dockResume();                                              // the quad's unmount: the router has not moved on yet
  assert.equal(d.Shell.dockOpen(), false, 'nothing before the tick');
  d.clock.advance(0);
  assert.equal(d.Shell.dockOpen(), false, 'mountedId is still quad (a remount of the quad itself): no dock');
  d.w.run("mountedId = 'agents'");
  d.Shell.dockResume();
  d.clock.advance(0);
  assert.equal(tmuxOf(d), S1, 'left the quad: the dock is back with its session');
  assert.equal(d.dock().querySelectorAll('iframe').length, 1);
  assert.equal(d.body().getAttribute('data-sb'), 'rail');
  assert.equal(stored(d.w, 'ccboard:dock'), S1);
});

test('a load straight onto the quad with a saved dock keeps the session for later (restoreDock under the hold), and the dock comes back when the quad is left', () => {
  const d = dWorld({ store: { 'ccboard:dock': S1 }, state: stateOf(), install: false });
  d.Shell.dockSuspend();                                              // the quad mounted first
  d.w.run('installShell()');
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(stored(d.w, 'ccboard:dock'), S1, 'not dropped');
  assert.equal(d.Shell.dock.wanted, S1);
  d.Shell.dockResume();
  d.clock.advance(0);
  assert.equal(tmuxOf(d), S1);
  const off = dWorld({ store: { 'ccboard:dock': S1, 'ccboard:dock:off': '1' }, state: stateOf(), install: false });
  off.Shell.dockSuspend();
  off.w.run('installShell()');
  assert.equal(off.Shell.dock.wanted, '', 'switched off: nothing is kept for later');
});

test('a window that crosses 1024 while the quad owns it brings no dock back (dockSync under the hold); narrow keeps the wish, wide with the quad gone restores it', () => {
  const d = dWorld({ width: 1100 });
  d.Shell.openDock(S1);
  d.Shell.dockSuspend();
  d.setWidth(900);
  d.setWidth(1300);
  assert.equal(d.Shell.dockOpen(), false, 'wide again, but the quad is the page');
  d.Shell.dockResume();
  d.w.run("mountedId = 'home'");
  d.clock.advance(0);
  assert.equal(tmuxOf(d), S1);
});

test('the real quad page suspends the dock before it measures its column and resumes it when it goes: add to quad from the dock, then back', () => {
  const d = dWorld({ width: 1100, state: stateOf() });
  d.w.load('pages/quad.js');
  d.Shell.openDock(S1);
  const frame = d.dock().querySelector('iframe');
  const Quad = d.w.get('Quad');
  d.w.run("registerPage('tasks', { mount() {}, unmount() {} })");       // a page to leave the quad for (the world loads no other page scripts)
  d.w.location.hash = '#/quad';
  d.w.run('route()');
  assert.equal(d.w.get('mountedId'), 'quad');
  assert.ok(Quad.current, 'the quad is up');
  assert.equal(d.Shell.dockOpen(), false, 'no dock next to the quad');
  assert.equal(frame.src, 'about:blank');
  assert.equal(hidden(d.dock()), true);
  assert.equal(stored(d.w, 'ccboard:dock'), S1);
  d.w.location.hash = '#/tasks';
  d.w.run('route()');
  d.clock.advance(0);
  assert.equal(Quad.current, null);
  assert.equal(tmuxOf(d), S1, 'back on another page: the dock shows its session again');
});

test('the Quad has an entry on touch (the palette test pins its own list): the sidebar and the drawer list it after Tasks, the bottom nav keeps its four plus Menu', () => {
  const d = dWorld({ width: 390 });
  const ids = (sel) => d.w.document.querySelectorAll(`${sel} [data-nav]`).map((a) => `${a.getAttribute('data-nav')}=${a.getAttribute('href')}`);
  const want = ['home=#/', 'inbox=#/inbox', 'agents=#/agents', 'tasks=#/tasks', 'quad=#/quad', 'usage=#/usage', 'memory=#/memory', 'settings=#/settings'];
  assert.deepEqual(ids('#sidebar'), want);
  assert.deepEqual(ids('#drawer'), want, 'the Menu sheet of the phone');
  assert.deepEqual(ids('#bnav'), want.slice(0, 4), 'the bottom nav is Home, Needs you, Agents, Tasks and the Menu button: Quad is one tap into Menu');
  assert.equal(d.w.document.querySelectorAll('#bnav .bn').length, 5);
  assert.equal(plain(d.w.run("Shell.NAV.map((n) => n[0])")).indexOf('quad'), 4);
});

// ---------------------------------------------------------------- the entry point: a row's Open link

test('a plain click on a row\'s Open link (/term/<session>) opens the dock instead of a new tab, and takes the keyboard', () => {
  const d = dWorld();
  const a = openLink(d);
  const e = click(a);
  assert.equal(e.prevented, 1);
  assert.equal(tmuxOf(d), S1);
  assert.equal(d.w.document.activeElement, d.dock().querySelector('iframe'), 'asked for: the terminal has the keyboard');
  const again = click(a);
  assert.equal(again.prevented, 1);
  assert.equal(d.dock().querySelectorAll('iframe').length, 1);
});

test('modifier clicks, other buttons, other links, a dock that is off, a narrow window and a link inside the dock all keep their own behaviour', () => {
  const d = dWorld();
  const a = openLink(d);
  for (const mod of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }, { button: 2 }]) {
    assert.equal(click(a, mod).prevented, 0, JSON.stringify(mod));
  }
  assert.equal(d.Shell.dockOpen(), false);
  const other = openLink(d, '/static/x.js');
  assert.equal(click(other).prevented, 0);
  const nested = openLink(d, `/term/${S1}/extra`);
  assert.equal(click(nested).prevented, 0, 'only /term/<name>');
  assert.equal(click(openLink(d, `/term/${S1}?font=1`)).prevented, 0, 'a link with a query is not a plain terminal link');
  d.w.localStorage.setItem('ccboard:dock:off', '1');
  assert.equal(click(a).prevented, 0, 'off: the link opens its tab as it always did');
  d.w.localStorage.removeItem('ccboard:dock:off');
  d.setWidth(900);
  assert.equal(click(a).prevented, 0, 'narrow: the same');
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(click(a, { defaultPrevented: true }).prevented, 0, 'someone already handled it');
  const bare = dWorld({ termkit: false });
  assert.equal(click(openLink(bare)).prevented, 0, 'no TermKit: the link works as before');
});

test('a press held for 500 ms or more navigates, as a long press always did; a quick one opens the dock', () => {
  const d = dWorld();
  const a = openLink(d);
  a.dispatchEvent({ type: 'pointerdown', button: 0 });
  d.clock.advance(600);
  assert.equal(click(a).prevented, 0);
  assert.equal(d.Shell.dockOpen(), false);
  a.dispatchEvent({ type: 'pointerdown', button: 0 });
  d.clock.advance(120);
  assert.equal(click(a).prevented, 1);
  assert.equal(d.Shell.dockOpen(), true);
  d.Shell.closeDock();
  a.dispatchEvent({ type: 'pointerdown', button: 0 });
  d.clock.advance(900);
  const other = openLink(d, `/term/${S2}`);
  assert.equal(click(other).prevented, 1, 'a press on another link says nothing about this one');
});

test('in an installed app core.js would navigate this window to /term/: the click is stopped there so the dock wins', () => {
  const d = dWorld({ standalone: true });
  const e = click(openLink(d));
  assert.equal(e.prevented, 1);
  assert.equal(e.stopped, 1);
  const web = dWorld();
  assert.equal(click(openLink(web)).stopped, 0, 'in a browser tab nothing else listens: no need');
});

test('the listener is on <body> (capture), so it runs before core.js\'s handler on document', () => {
  const src = fs.readFileSync(path.join(STATIC, 'shell.js'), 'utf8');
  assert.match(src, /document\.body\.addEventListener\('click', Shell\.onTermLink, true\)/);
  assert.match(src, /document\.body\.addEventListener\('pointerdown', Shell\.onTermPress, true\)/);
});

test('an Open link inside the peek sheet closes the sheet (it would cover the dock) and opens the dock', () => {
  const d = dWorld();
  const sheet = d.q('#sheet');
  const a = d.w.document.createElement('a');
  a.setAttribute('href', `/term/${S2}`);
  sheet.append(a);
  d.w.run('globalThis.__closed = 0; closeSheet = () => { __closed += 1; }');
  assert.equal(click(a).prevented, 1);
  assert.equal(d.w.get('__closed'), 1);
  assert.equal(tmuxOf(d), S2);
});

// ---------------------------------------------------------------- focus

test('opening the dock never takes the keyboard from a composer: ttyd\'s focus on load goes back to it', () => {
  const d = dWorld();
  const box = d.w.document.createElement('textarea');
  d.q('#page').append(box);
  box.focus();
  assert.equal(d.Shell.openDock(S1, { focus: true }), true);
  assert.equal(d.w.document.activeElement, box, 'not at once');
  const f = d.dock().querySelector('iframe');
  f.focus();
  f.dispatchEvent({ type: 'load' });
  d.clock.advance(1);
  assert.equal(d.w.document.activeElement, box);
  f.focus();
  d.clock.advance(400);
  assert.equal(d.w.document.activeElement, box, 'again when ttyd opens its socket');
});

test('mod+j with nothing typed into gives the terminal the keyboard; the same key does nothing to a composer that is being typed in', () => {
  const d = dWorld({ state: stateOf() });
  d.Shell.toggleDock();
  assert.equal(d.w.document.activeElement, d.dock().querySelector('iframe'));
  d.Shell.toggleDock();
  const box = d.w.document.createElement('input');
  d.q('#page').append(box);
  box.focus();
  d.Shell.toggleDock();
  assert.equal(d.w.document.activeElement, box);
});

// ---------------------------------------------------------------- the peek and the dock share #dock

test('the session peek goes to the sheet while the terminal dock is on, and keeps the dock surface when it is off, narrow, or termkit.js is missing', () => {
  const surface = (d) => d.w.get('peekSurface')();
  assert.equal(surface(dWorld({ width: 1280 })), 'sheet');
  assert.equal(surface(dWorld({ width: 1280, store: { 'ccboard:dock:off': '1' } })), 'dock');
  assert.equal(surface(dWorld({ width: 1280, termkit: false })), 'dock');
  assert.equal(surface(dWorld({ width: 900 })), 'sheet', 'below 1024 it was always the sheet');
  const d = dWorld({ width: 1280 });
  d.Shell.openDock(S1);
  assert.equal(surface(d), 'sheet');
  d.setWidth(1100);
  assert.equal(surface(d), 'sheet');
});

test('session.js keeps working without a Shell (the page tests load it that way)', () => {
  const w = makeWorld({ matchMedia: (q) => ({ matches: /1024/.test(q), addEventListener() {}, removeEventListener() {} }) });
  installDom(w);
  for (const f of ['core.js', 'components.js', 'router.js', 'pages/session.js']) w.load(f);
  assert.equal(w.run('typeof Shell'), 'undefined');
  assert.equal(w.get('peekSurface')(), 'dock');
});

// ---------------------------------------------------------------- CSS and markup contracts

test('the dock pane\'s context chip and a quad tile\'s are one reading: the same text and the same 80 / 90 thresholds, painted .warn/.bad in the dock', async () => {
  const d = dWorld({ state: stateOf({ s1: { stats: { context_pct: 79 } } }) });
  d.Shell.openDock(S1);
  const chip = () => d.dock().querySelector('.tp-ctx');
  const seen = [];
  for (const pct of [61, 79, 80, 89, 90, 100]) {
    const st = stateOf({ s1: { stats: { context_pct: pct } } });
    d.w.ctx.__st = st;
    d.w.run('state = __st; renderShell(state)');
    seen.push([chip().textContent, chip().classList.contains('warn') ? 'warn' : chip().classList.contains('bad') ? 'bad' : '']);
  }
  assert.deepEqual(seen, [['ctx 61%', ''], ['ctx 79%', ''], ['ctx 80%', 'warn'], ['ctx 89%', 'warn'], ['ctx 90%', 'bad'], ['ctx 100%', 'bad']]);
  for (const pct of [61, 79, 80, 89, 90, 100]) {
    const i = plain(d.w.run(`TermKit.ctxInfo(${pct})`));
    assert.equal(i.text, seen[[61, 79, 80, 89, 90, 100].indexOf(pct)][0], 'the helper both surfaces read');
  }
});

test('shell.css: the dock default, the flex column of the terminal dock, the handle, and the 1023 px hide are all there; no inline styles anywhere in the new code', () => {
  const css = fs.readFileSync(path.join(STATIC, 'shell.css'), 'utf8');
  assert.match(css, /#dock\.has-term:not\(\.hidden\) \{[^}]*display:flex; flex-direction:column; overflow:hidden/);
  assert.match(css, /@media \(max-width:1023px\) \{ #dock \{ display:none !important; \} \}/);
  assert.match(css, /\.dock-resize \{[^}]*cursor:col-resize/);
  assert.match(css, /#app\.dock-dragging iframe \{ pointer-events:none; \}/);
  assert.match(css, /body\.dnd-task iframe \{ pointer-events:none; \}/, 'the class hook for the task drag (v0.5.15)');
  assert.match(css, /\.tp-head \{[^}]*min-height:var\(--row-btn\)/, 'one header row: 28 px with a mouse, 44 px on touch (--row-btn)');
  assert.match(css, /\.tp-frame \{[^}]*position:absolute; inset:0/);
  for (const file of ['shell.js', 'termkit.js']) {
    const src = fs.readFileSync(path.join(STATIC, file), 'utf8');
    assert.doesNotMatch(src, /innerHTML|insertAdjacentHTML|cssText|setAttribute\(\s*['"]style['"]/, `${file}: CSP`);
  }
});

test('termkit.js is a lazy bundle (lazy.js, issue #103): index.html no longer loads it, the dock opens it on first use and the quad bundle needs it; #dock is a hidden aside', () => {
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  const order = [...html.matchAll(/<script (?:defer )?src="([^"]+)"/g)].map((m) => m[1]);
  assert.ok(order.length > 5, 'the script tags were found');
  assert.ok(!order.includes('/static/termkit.js'), 'not in the first-paint script set');
  const lazy = fs.readFileSync(path.join(STATIC, 'lazy.js'), 'utf8');
  assert.match(lazy, /termkit: \{ js: \['\/static\/termkit\.js'\]/);
  assert.match(lazy, /quad: \{ needs: \['termkit'\]/);
  assert.match(html, /<aside id="dock" class="hidden" aria-label="Terminal dock"><\/aside>/);
});

// ---------------------------------------------------------------- v0.5.13: create from anywhere: the dock's +, the sidebar's +, the topbar menu (openLauncher)

/** openLauncher as a recorder; read back as plain values (the project and the repo are the state's own objects). */
const stubLauncher = (w) => w.run('globalThis.__launched = []; globalThis.openLauncher = (o) => { __launched.push(o); return true; };');
const launched = (w) => plain(w.run('__launched.map((o) => ({ mode: o.mode, project: o.project && o.project.name, repo: o.repo && o.repo.name, folder: !!(o.project && o.repo && o.repo === o.project.root) }))'));
const sheetOpen = (d) => d.q('#sheet').open === true;

test('the dock\'s header has a + first among its buttons: a new session in the repo of the session shown, through openLauncher (one tap to the filled sheet)', () => {
  const d = dWorld({ state: stateOf() });
  stubLauncher(d.w);
  assert.equal(d.Shell.openDock(S3), true);
  const btns = d.dock().querySelector('.tp-btns');
  assert.deepEqual(btns.children.map((b) => b.getAttribute('data-act')).slice(0, 2), ['new', 'reconnect'], 'first, before the pane\'s own buttons');
  const add = btns.querySelector('[data-act=new]');
  assert.equal(add.getAttribute('aria-label'), 'New session in this repo');
  assert.ok(add.classList.contains('tp-btn') && add.classList.contains('icon'), 'the same icon button as its neighbours');
  click(add);
  assert.deepEqual(launched(d.w), [{ mode: 'session', project: 'shop', repo: 'web', folder: false }], 'the repo of the session in the dock: web, not api');
  assert.equal(d.w.run('__launched[0].project === state.projects[0] && __launched[0].repo === state.projects[0].repos[1]'), true, 'the state objects');
  assert.equal(sheetOpen(d), false, 'the dock opens no sheet itself');
  d.Shell.openDock(S1);
  click(d.dock().querySelector('[data-act=new]'));
  assert.equal(launched(d.w).at(-1).repo, 'api', 'a repainted dock keeps the button and follows the session it shows');
});

test('the dock\'s + for a session the board no longer knows opens the repo picker instead of nothing', () => {
  const d = dWorld({ state: stateOf() });
  stubLauncher(d.w);
  assert.equal(d.Shell.openDock('gone--repo--s9'), true);
  click(d.dock().querySelector('[data-act=new]'));
  assert.deepEqual(launched(d.w), []);
  assert.equal(sheetOpen(d), true);
  assert.equal(d.q('#sheet').querySelector('.sheet-title').textContent, 'New session');
});

test('every project row of the sidebar has a +: openLauncher for that project, in the repo it worked in last; the row does not open or close and the page does not navigate', () => {
  const d = dWorld({ state: stateOf({ s3: { state_at: ISO(1) } }) });
  stubLauncher(d.w);
  d.w.run('renderShell(state)');
  const row = d.q('#sidebar .proj-row');
  const add = row.querySelector('.tn-add');
  assert.ok(add, 'the + is on the project row');
  assert.equal(add.getAttribute('aria-label'), 'New session in this project');
  assert.equal(add.getAttribute('data-project'), 'shop');
  assert.ok(add.classList.contains('bp5-button'), 'a real button, not a link inside the row\'s link');
  const open0 = row.getAttribute('aria-expanded');
  const e = click(add);
  assert.equal(e.prevented > 0 || e.stopped > 0, true, 'the click is its own');
  assert.deepEqual(launched(d.w), [{ mode: 'session', project: 'shop', repo: 'web', folder: false }], 'web has the newest session');
  assert.equal(row.getAttribute('aria-expanded'), open0, 'the tree row did not toggle');
  assert.equal(d.w.location.hash, '', 'and nothing navigated');
});

test('the sidebar + closes the phone drawer first, and a project with no place at all gets the repo picker narrowed to it', () => {
  const st = stateOf();
  st.projects.push({ name: 'empty', path: '/srv/projects/empty', root: null, orphan_sessions: [], repos: [] });
  const d = dWorld({ state: st });
  stubLauncher(d.w);
  d.w.run('renderShell(state)');
  d.w.run('Shell.openDrawer()');
  assert.equal(d.q('#drawer').open, true);
  const drawerRow = d.q('#drawer .proj-row[data-key], #drawer .tn.proj');
  assert.ok(drawerRow, 'the drawer has the same tree');
  click(d.q('#drawer .tn-add'));
  assert.equal(d.q('#drawer').open, false, 'the drawer closes so the sheet is not under it');
  assert.equal(launched(d.w).length, 1);
  d.w.run('Shell.sideNew("empty")');
  assert.equal(launched(d.w).length, 1, 'no place in that project: no launcher call');
  assert.equal(sheetOpen(d), true, 'the picker (narrowed to it) says so');
  assert.equal(d.w.run('Shell.sideNew("nope")'), false, 'an unknown project does nothing');
});

test('the topbar + menu: New session and New task open the launcher with the project and repo of the route; off a project page they ask the repo first, then open it', () => {
  const d = dWorld({ state: stateOf() });
  stubLauncher(d.w);
  const item = (label) => d.w.get('Shell').createItems().find((i) => i.label === label);
  d.w.location.hash = '#/p/shop/web';
  item('New session').onClick();
  item('New task').onClick();
  assert.deepEqual(launched(d.w), [{ mode: 'session', project: 'shop', repo: 'web', folder: false }, { mode: 'task', project: 'shop', repo: 'web', folder: false }]);
  assert.equal(sheetOpen(d), false, 'no picker in between');
  d.w.location.hash = '#/';
  item('New session').onClick();
  assert.equal(sheetOpen(d), true, 'no project in the route: the repo picker');
  const rows = d.q('#sheet').querySelectorAll('.pick-row');
  assert.ok(rows.length >= 2);
  const second = rows.find((r) => /shop\/web/.test(r.textContent));
  second.click();
  assert.deepEqual(launched(d.w).at(-1), { mode: 'session', project: 'shop', repo: 'web', folder: false }, 'a picker row opens the launcher for that repo');
  assert.equal(launched(d.w).length, 3);
  const labels = plain(d.w.run('Shell.createItems().map((i) => i.label)'));
  assert.deepEqual(labels, ['New session', 'New task', 'Schedule', 'New project', 'Import from GitHub', 'Batch prompt'], 'schedule, project, import and batch keep their own sheets');
});

test('the topbar + menu on a project page that names no repo: New session opens the launcher where the project worked last (no picker), the chevron goes back to the list', () => {
  const d = dWorld({ state: stateOf({ s3: { state_at: new Date(Date.now() - 5000).toISOString() }, s1: { state_at: new Date(Date.now() - 600000).toISOString() }, s2: { state_at: new Date(Date.now() - 700000).toISOString() } }) });
  stubLauncher(d.w);
  d.w.location.hash = '#/p/shop';
  d.w.get('Shell').createItems()[0].onClick();
  assert.deepEqual(launched(d.w), [{ mode: 'session', project: 'shop', repo: 'web', folder: false }], 'web holds the project\'s newest session');
  assert.equal(sheetOpen(d), false, 'no picker in between');
  assert.equal(typeof d.w.run('__launched[0].back.onClick'), 'function', 'the chevron has somewhere to go: the repo list');
  assert.equal(d.w.run('__launched[0].back.label'), 'Back to the repo list');
});

test('without openLauncher the + menu falls back to the old session form in the sheet (a deploy before launcher.js has it)', () => {
  const d = dWorld({ state: stateOf() });
  d.w.run('openLauncher = undefined');
  d.w.location.hash = '#/p/shop/api';
  d.w.get('Shell').createItems()[0].onClick();
  assert.equal(sheetOpen(d), true);
  assert.equal(d.q('#sheet').querySelector('.sheet-title').textContent, 'New session · shop/api');
  assert.ok(d.q('#sheet').querySelector('form.form'));
});

test('index.html has no #modal (v0.5.13: the diff viewer is a sheet) and shell.css styles the wide sheet and the sidebar +', () => {
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  assert.doesNotMatch(html, /id="modal"/);
  for (const file of ['style.css', 'shell.css', 'pages.css']) assert.doesNotMatch(fs.readFileSync(path.join(STATIC, file), 'utf8'), /#modal\s*[{,]|\.modal-box/, `${file} has no #modal rules`);
  const css = fs.readFileSync(path.join(STATIC, 'shell.css'), 'utf8');
  assert.match(css, /dialog#sheet\.right\.wide \{ width:min\(1100px, 100vw\); \}/);
  assert.match(css, /\.tn-row \.tn-add \{/);
});

test('tap count (real launcher): a new session from the dock is 2 taps with the remembered defaults: the dock\'s +, then Start & open', async () => {
  const d = dWorld({ state: stateOf() });
  d.w.run('renderBanner = () => {}; api = async (method, path, body) => { __calls.push({ method, path, body }); return path.endsWith("/sessions") ? { tmux: "shop--web--s9", attach_url: "/term/shop--web--s9", agent: "claude", cmd: "claude" } : { ok: true }; }');
  assert.equal(d.Shell.openDock(S3), true);
  let taps = 0;
  taps++;                                                                                  // 1: the dock's +
  click(d.dock().querySelector('[data-act=new]'));
  const sh = d.q('#sheet');
  assert.equal(sh.open, true);
  assert.equal(sh.querySelector('.sheet-title').textContent, 'New session · shop/web', 'the repo of the session in the dock, already chosen');
  const primaries = sh.querySelectorAll('button').filter((b) => b.classList.contains('bp5-intent-primary'));
  assert.equal(primaries.length, 1);
  assert.match(primaries[0].textContent, /Start & open/);
  assert.deepEqual(plain(d.w.get('__calls')).filter((c) => c.method === 'POST'), [], 'opening sent nothing');
  taps++;                                                                                  // 2: Start & open
  sh.querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle(); await settle();
  const posts = plain(d.w.get('__calls')).filter((c) => c.method === 'POST' && /\/sessions$/.test(c.path));
  assert.deepEqual(posts.map((c) => c.path), ['/api/projects/shop/repos/web/sessions']);
  assert.equal(taps, 2);
});

// ---------------------------------------------------------------- touch windows under 1180 px (v0.5.21, #56)

const coarsen = (d, on = true) => d.w.document.documentElement.classList[on ? 'add' : 'remove']('force-coarse');

test('touch under 1180 px: an Open link and openTerm open the terminal page, not the dock; the dock still opens on purpose (mod+j, Add to dock)', () => {
  const d = dWorld({ width: 1024 });
  coarsen(d);
  assert.equal(d.Shell.dockAuto(), false);
  const e = click(openLink(d));
  assert.equal(e.prevented, 0, 'the link keeps its own behaviour');
  assert.equal(d.Shell.dockOpen(), false);
  assert.equal(d.Shell.openTerm(S1), false);
  assert.deepEqual(plain(d.w.get('__opened')), ['/term/' + S1], 'openTerm went to the terminal page');
  assert.equal(d.Shell.openDock(S1, { focus: true }), true, 'on purpose it still opens');
  assert.equal(tmuxOf(d), S1);
  assert.equal(stored(d.w, 'ccboard:dock'), S1, 'and comes back on the next load');
});

test('touch at 1180 px and up, and a mouse at any width, keep the dock as before', () => {
  const wide = dWorld({ width: 1180 });
  coarsen(wide);
  assert.equal(wide.Shell.dockAuto(), true);
  assert.equal(click(openLink(wide)).prevented, 1);
  assert.equal(tmuxOf(wide), S1);
  const mouse = dWorld({ width: 1024 });
  assert.equal(mouse.Shell.dockAuto(), true);
  assert.equal(click(openLink(mouse)).prevented, 1);
  const big = dWorld({ width: 1440 });
  coarsen(big);
  assert.equal(click(openLink(big)).prevented, 1, 'a touch screen at 1440 behaves as before');
});

test('the touch grabber: a double tap resets the width, a drag does not count as a tap', () => {
  const d = dWorld();
  const h = resizer(d);
  const tap = (x) => { h.dispatchEvent(ptr('pointerdown', x, { pointerType: 'touch' })); h.dispatchEvent(ptr('pointerup', x, { pointerType: 'touch' })); };
  d.Shell.setDockWidth(600, true);
  tap(830);
  assert.equal(d.app.props['--dock-w'], '600px', 'one tap changes nothing');
  d.clock.advance(100);
  tap(830);
  assert.equal(d.app.props['--dock-w'], undefined, 'the second tap within 350 ms resets');
  assert.equal(stored(d.w, 'ccboard:dock:w'), null);
  d.Shell.setDockWidth(600, true);
  tap(830);
  d.clock.advance(100);
  h.dispatchEvent(ptr('pointerdown', 830, { pointerType: 'touch' }));
  h.dispatchEvent(ptr('pointermove', 780, { pointerType: 'touch' }));
  h.dispatchEvent(ptr('pointerup', 780, { pointerType: 'touch' }));
  assert.equal(stored(d.w, 'ccboard:dock:w'), '500', 'a drag sizes it and is no tap');
});

test('touch geometry in the CSS: a 44 px grabber on a coarse pointer and under html.force-coarse, a 38 vw default, and the header controls on the touch tokens', () => {
  const css = fs.readFileSync(path.join(STATIC, 'shell.css'), 'utf8');
  const tokens = fs.readFileSync(path.join(STATIC, 'tokens.css'), 'utf8');
  assert.match(css, /@media \(pointer:coarse\) \{\s*#dock\.has-term \.dock-resize \{ display:block;[^}]*width:var\(--tap\); height:var\(--tap\);/, 'touch grabber, 44 px by the token');
  assert.match(css, /html\.force-coarse #dock\.has-term \.dock-resize \{ display:block;[^}]*width:var\(--tap\); height:var\(--tap\);/, 'and for the QA switch');
  assert.match(css, /@media \(pointer:coarse\) \{ :root \{[^}]*--dock-w:min\(38vw, 560px\);/, 'default width on touch');
  assert.match(css, /html\.force-coarse \{[^}]*--dock-w:min\(38vw, 560px\);/);
  assert.match(css, /\.tp-head \.bp5-button\.tp-btn:not\(\[class\*=bp5-intent-\]\) \{ min-width:var\(--row-btn\); width:var\(--row-btn\); min-height:var\(--row-btn\); height:var\(--row-btn\);/, 'the header buttons follow --row-btn');
  assert.match(css, /\.tp-modes \.bp5-button\.tp-mode \{ min-height:var\(--row-btn\);/);
  assert.match(css, /\.tp-perm \.bp5-button \{ min-height:var\(--row-btn\); \}/);
  assert.match(tokens, /html\.force-coarse \{ --tap:44px; --row-h:44px; --row-btn:44px; \}/, '--row-btn is 44 px under force-coarse');
  assert.match(tokens, /@media \(pointer:coarse\) \{ :root \{ --tap:44px; --row-h:44px; --row-btn:44px; \} \}/);
});
