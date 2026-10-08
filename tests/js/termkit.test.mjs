// Contract tests for app/static/termkit.js (namespace TermKit): ttyUrl, touchScroller, bind (and its font spike), fitSoon, viewportFit, keyBar and its
// hold-to-repeat. The vm harness has no layout and no real events, so the tests drive the kit through stub nodes, fake timers (the
// kit reads setTimeout / setInterval / clearTimeout / clearInterval as globals, so the stubs go in as harness extra globals) and a
// fake Date.now. Microtasks are real: `settle()` lets the repeater's in-flight promise chain run between timer steps.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { installDom } from './minidom.mjs';
import path from 'node:path';
import { ROOT, makeWorld, plain } from './harness.mjs';

// ---------------------------------------------------------------- helpers

/** A fake clock: timers fire in order of due time as `advance(ms)` moves time forward (intervals re-arm). */
function fakeClock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  const clock = {
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
  return clock;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));       // flush the microtask queue (and the vm realm's promises)

/** core.js + termkit.js in a world with fake timers and a fake Date.now; `dom: true` installs the minidom tree (keyBar needs it). */
function kitWorld({ dom = false, extra = {} } = {}) {
  const clock = fakeClock();
  const RealDate = Date;
  class FakeDate extends RealDate { static now() { return clock.now; } }
  const w = makeWorld({
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    Date: FakeDate, ...extra,
  });
  const d = dom ? installDom(w) : null;
  w.load('core.js');
  w.load(process.env.TERMKIT_JS || 'termkit.js');        // TERMKIT_JS=/abs/path/to/candidate.js runs these tests against a modified copy
  return { w, clock, dom: d, kit: w.get('TermKit') };
}

// ---------------------------------------------------------------- the namespace

test('TermKit exposes the contract surface and nothing needs the DOM to read it', () => {
  const { kit } = kitWorld();
  for (const name of ['ttyUrl', 'bind', 'touchScroller', 'fitSoon', 'viewportFit', 'keyBar']) {
    assert.equal(typeof kit[name], 'function', `TermKit.${name}`);
  }
});

// ---------------------------------------------------------------- ttyUrl

const GRID = '/tty/?arg=ccboard--ccboard--s1&arg=grid&fontSize=11&rendererType=canvas&disableResizeOverlay=true&disableReconnect=true';

test('ttyUrl: the full URL is /tty/?arg=<name>, with an optional font size and nothing else', () => {
  const { kit } = kitWorld();
  assert.equal(kit.ttyUrl('ccboard--ccboard--s1', { mode: 'full' }), '/tty/?arg=ccboard--ccboard--s1');
  assert.equal(kit.ttyUrl('ccboard--ccboard--s1'), '/tty/?arg=ccboard--ccboard--s1', 'full is the default');
  assert.equal(kit.ttyUrl('ccboard--ccboard--s1', { fontSize: 15 }), '/tty/?arg=ccboard--ccboard--s1&fontSize=15');
});

test('ttyUrl: the grid URL is exactly the contract string (no theme parameter)', () => {
  const { kit } = kitWorld();
  assert.equal(kit.ttyUrl('ccboard--ccboard--s1', { mode: 'grid' }), GRID);
});

test('ttyUrl: ro adds the second wrapper argument and nothing from grid', () => {
  const { kit } = kitWorld();
  const url = kit.ttyUrl('ccboard--ccboard--s1', { mode: 'ro' });
  const q = new URLSearchParams(url.slice('/tty/?'.length));
  assert.deepEqual(q.getAll('arg'), ['ccboard--ccboard--s1', 'ro']);
  assert.equal(q.has('rendererType'), false);
  assert.ok(url.startsWith('/tty/?arg=ccboard--ccboard--s1&arg=ro'));
});

test('ttyUrl: the session name is one encoded arg, whatever it contains', () => {
  const { kit } = kitWorld();
  for (const name of ['a b', 'x;rm -rf /', 'a&arg=grid', 'a#b', 'a%20b', 'ü/é', '$(x)', 'p--r--s\n1']) {
    const url = kit.ttyUrl(name, { mode: 'full' });
    const q = new URLSearchParams(url.slice('/tty/?'.length));
    assert.deepEqual(q.getAll('arg'), [name], `${JSON.stringify(name)} must round-trip as the only arg: ${url}`);
    const value = url.slice('/tty/?arg='.length).split('&')[0];
    assert.doesNotMatch(value, /[&#;\s\/]/, `raw separator in the encoded name of ${JSON.stringify(name)}`);
  }
});

test('ttyUrl: an empty or non-string name is refused', () => {
  const { kit } = kitWorld();
  for (const bad of ['', null, undefined, 7, {}]) assert.throws(() => kit.ttyUrl(bad), `${String(bad)} is not a session name`);
});

test('ttyUrl: only full, grid and ro ever reach the wrapper (a bad mode throws or is dropped, never passed on)', () => {
  const { kit } = kitWorld();
  for (const mode of ['', 'GRID', 'Grid', '-f', 'full;ls', 'grid&arg=evil', ' grid', 'rw', 'ignore-size', 'grid\n', '../x', 0, false, {}]) {
    let url = null;
    try { url = kit.ttyUrl('ccboard--ccboard--s1', { mode }); } catch (_) { continue; }
    const args = new URLSearchParams(url.slice('/tty/?'.length)).getAll('arg');
    assert.ok(args.length >= 1 && args.length <= 2 && args.slice(1).every((a) => a === 'grid' || a === 'ro'), `mode ${JSON.stringify(mode)} leaked into ${url}`);
    assert.doesNotMatch(url, /evil|ignore-size|-f/);
  }
});

test('ttyUrl: renderer is whitelisted, the font size is clamped to whole px, quiet adds the two disable flags', () => {
  const { kit } = kitWorld();
  const q = (url) => new URLSearchParams(url.slice('/tty/?'.length));
  assert.equal(q(kit.ttyUrl('n', { renderer: 'canvas' })).get('rendererType'), 'canvas');
  for (const renderer of ['x&arg=ls', 'canvas;ls', 'CANVAS']) {
    let url = null;
    try { url = kit.ttyUrl('n', { renderer }); } catch (_) { continue; }
    assert.deepEqual(q(url).getAll('arg'), ['n'], `renderer ${renderer} leaked`);
    assert.ok(!q(url).has('rendererType') || ['canvas', 'dom', 'webgl'].includes(q(url).get('rendererType')));
  }
  assert.equal(q(kit.ttyUrl('n', { fontSize: 99 })).get('fontSize'), '28');
  assert.equal(q(kit.ttyUrl('n', { fontSize: 2 })).get('fontSize'), '8');
  assert.equal(q(kit.ttyUrl('n', { fontSize: 11.6 })).get('fontSize'), '12');
  for (const junk of ['abc', '15abc', NaN, '', null]) assert.equal(q(kit.ttyUrl('n', { fontSize: junk })).has('fontSize'), false, `fontSize ${String(junk)}`);
  const quiet = q(kit.ttyUrl('n', { quiet: true }));
  assert.equal(quiet.get('disableResizeOverlay'), 'true');
  assert.equal(quiet.get('disableReconnect'), 'true');
  assert.equal(q(kit.ttyUrl('n', { mode: 'grid', quiet: false })).has('disableReconnect'), false);
  const gridBig = q(kit.ttyUrl('n', { mode: 'grid', fontSize: 9 }));
  assert.deepEqual(gridBig.getAll('fontSize'), ['9'], 'an explicit font size replaces the grid default, it is not added next to it');
});

// ---------------------------------------------------------------- pure helpers

test('compactKeys: the soft keyboard rule is visualViewport.height < 0.7 * innerHeight, and the stored choice wins', () => {
  const { kit } = kitWorld();
  assert.equal(kit.compactKeys(null, 844, 844), false);
  assert.equal(kit.compactKeys(null, 1000, 1000), false);
  assert.equal(kit.compactKeys(null, 701, 1000), false);
  assert.equal(kit.compactKeys(null, 700, 1000), false, 'exactly 0.7 is not compact');
  assert.equal(kit.compactKeys(null, 699, 1000), true, 'under 0.7 is');
  assert.equal(kit.compactKeys(null, 420, 844), true, 'a phone keyboard takes about half of an 844 px window');
  assert.equal(kit.compactKeys(null, 0, 844), false, 'no measurement, no compact');
  assert.equal(kit.compactKeys(undefined, 300, 0), false);
  assert.equal(kit.compactKeys('compact', 844, 844), true, 'the persisted choice wins over the heuristic');
  assert.equal(kit.compactKeys('full', 300, 844), false);
});

test('clampFont and backTarget', () => {
  const { kit } = kitWorld();
  assert.equal(kit.clampFont(13), 13);
  assert.equal(kit.clampFont('14'), 14);
  assert.equal(kit.clampFont(1), 8);
  assert.equal(kit.clampFont(1000), 28);
  assert.equal(kit.clampFont('x'), null);
  assert.equal(kit.clampFont(null), null);
  assert.equal(kit.backTarget('https://box.ts.net/#/agents', 'https://box.ts.net'), 'back', 'a same-origin referrer: history.back()');
  assert.equal(kit.backTarget('https://box.ts.net', 'https://box.ts.net'), 'back');
  assert.equal(kit.backTarget('', 'https://box.ts.net'), 'board', 'cold start, a notification tap or a shared link: the Agents page');
  assert.equal(kit.backTarget('https://box.ts.net.evil.example/x', 'https://box.ts.net'), 'board', 'a prefix match is not the same origin');
  assert.equal(kit.backTarget('https://other.example/', 'https://box.ts.net'), 'board');
});

test('contextParts: task, last prompt, and the question (pending permission, else the message)', () => {
  const { kit } = kitWorld();
  const a = plain(kit.contextParts({ task: { id: 7, title: 'Fix login', phase: 'running' }, last_prompt: '  do it  ', state: 'waiting',
    last_message: 'Which file?', pending: [{ tool_name: 'Bash', summary: 'Bash: rm -rf build' }] }));
  assert.equal(a.task, 'Fix login'); assert.equal(a.taskId, 7); assert.equal(a.prompt, 'do it');
  assert.equal(a.ask, 'Bash: rm -rf build'); assert.equal(a.askKind, 'permission'); assert.equal(a.approve, true);
  const b = plain(kit.contextParts({ state: 'waiting', last_message: 'Which file?', pending: [] }));
  assert.equal(b.ask, 'Which file?'); assert.equal(b.askKind, 'waiting'); assert.equal(b.approve, false);
  const c = plain(kit.contextParts({ state: 'done', last_message: 'All green' }));
  assert.equal(c.askKind, 'message');
  const empty = plain(kit.contextParts(null));
  assert.deepEqual([empty.task, empty.prompt, empty.ask, empty.taskId], ['', '', '', null]);
});

// ---------------------------------------------------------------- touchScroller

/** A scroller whose wheel output is recorded: wheels = [{ dy, target }]. */
function scroller(extra = {}, world = kitWorld()) {
  const wheels = [];
  const sc = world.kit.touchScroller({ step: 18, threshold: 8, edge: 24, momentum: 0, onWheel: (dy, target) => wheels.push({ dy, target }), ...extra });
  return { sc, wheels, ...world };
}

test('touchScroller: a tap and anything under 8 px of travel produce no wheel event', () => {
  const { sc, wheels } = scroller();
  sc.start(100, 100);
  sc.move(100, 103);
  sc.move(100, 107);
  assert.equal(wheels.length, 0);
  sc.end();
  sc.start(100, 100);
  sc.end();
  assert.equal(wheels.length, 0);
  sc.start(100, 100);
  sc.move(100, 93);                                      // 7 px up: still a tap
  assert.equal(wheels.length, 0);
});

test('touchScroller: one wheel event per 18 px of travel, signed like a trackpad (finger down = negative deltaY)', () => {
  const { sc, wheels } = scroller();
  sc.start(100, 100);
  assert.equal(sc.move(100, 112), true, 'past the threshold the gesture is engaged (the page cancels the touchmove)');
  assert.equal(wheels.length, 0, '12 px of travel is not yet a line');
  sc.move(100, 118);
  assert.equal(wheels.length, 1, '18 px of travel: the first event');
  assert.ok(wheels[0].dy < 0, 'finger moving down: deltaY is negative');
  assert.equal(Math.abs(wheels[0].dy), 18);
  sc.move(100, 140);                                     // 40 px of travel in all: 2 events so far, 4 px carried
  assert.equal(wheels.length, 2);
  sc.move(100, 154);                                     // 54 px: the carried remainder completes a third
  assert.equal(wheels.length, 3);
  assert.ok(wheels.every((e) => e.dy === wheels[0].dy));
});

test('touchScroller: a finger moving up gives positive deltaY, and a reversal walks back', () => {
  const { sc, wheels } = scroller();
  sc.start(100, 400);
  sc.move(100, 340);                                     // 60 px up: 3 events
  assert.equal(wheels.length, 3);
  assert.ok(wheels.every((e) => e.dy > 0 && Math.abs(e.dy) === 18));
  sc.move(100, 340 + 60);                                // 60 px back down (6 px of the first run were carried): 3 events the other way
  assert.equal(wheels.length, 6);
  assert.ok(wheels.slice(3).every((e) => e.dy < 0));
});

test('touchScroller: a big jump emits every step it passed', () => {
  const { sc, wheels } = scroller();
  sc.start(100, 600);
  sc.move(100, 600 - 180);
  assert.equal(wheels.length, 10);
});

test('touchScroller: a swipe that starts within 24 px of the left edge is the iOS back gesture and is ignored', () => {
  const { sc, wheels } = scroller();
  sc.start(10, 100);
  sc.move(10, 200);
  sc.move(60, 400);
  sc.end();
  assert.equal(wheels.length, 0);
  sc.start(0, 100); sc.move(0, 300);
  assert.equal(wheels.length, 0);
  sc.start(23, 100); sc.move(23, 300);
  assert.equal(wheels.length, 0, '23 px is still inside the edge');
  sc.start(24, 100); sc.move(24, 160);
  assert.equal(wheels.length, 3, '24 px is the first column that scrolls');
});

test('touchScroller: the wheel event carries the touch target given to start()', () => {
  const { sc, wheels } = scroller();
  const target = { id: 'screen' };
  sc.start(100, 100, target);
  sc.move(100, 140);
  assert.equal(wheels.length, 2);
  assert.ok(wheels.every((e) => e.target === target));
});

test('touchScroller: a new start() forgets the previous gesture, a move without start does nothing', () => {
  const { sc, wheels } = scroller();
  assert.equal(sc.move(100, 500), false);
  sc.start(100, 100);
  sc.move(100, 112);                                     // engaged, 12 px carried
  sc.start(100, 300);
  sc.move(100, 306);
  assert.equal(wheels.length, 0, 'the 12 px of the first gesture do not leak into the second');
  sc.move(100, 330);                                     // 30 px of travel from the new start: one line (42 px with a leak would be two)
  assert.equal(wheels.length, 1);
});

test('touchScroller: end() with momentum off produces nothing more, even after time passes', () => {
  const { sc, wheels, clock } = scroller({ momentum: 0 });
  sc.start(100, 500);
  clock.advance(10); sc.move(100, 480);
  clock.advance(10); sc.move(100, 460);
  clock.advance(10); sc.move(100, 440);
  const seen = wheels.length;
  assert.ok(seen >= 3);
  sc.end();
  clock.advance(2000);
  assert.equal(wheels.length, seen, 'no timer, no tail');
  assert.equal(clock.pending, 0);
});

test('touchScroller: with momentum a flick keeps scrolling for about 300 ms in the same direction, then stops; touching again stops it at once', () => {
  const { sc, wheels, clock } = scroller({ momentum: 300 });
  sc.start(100, 500);
  clock.advance(10); sc.move(100, 480);
  clock.advance(10); sc.move(100, 460);
  clock.advance(10); sc.move(100, 440);                  // a fast flick: 2 px per ms
  const live = wheels.length;
  sc.end();
  clock.advance(400);
  const tail = wheels.length - live;
  assert.ok(tail >= 1, 'the flick continues');
  assert.ok(wheels.slice(live).every((e) => e.dy > 0), 'same direction as the flick (finger moving up)');
  const settled = wheels.length;
  clock.advance(5000);
  assert.equal(wheels.length, settled, 'momentum ends');
  assert.equal(clock.pending, 0, 'and leaves no timer behind');
  // a slow drag that rests before lifting has no momentum
  const slow = scroller({ momentum: 300 });
  slow.sc.start(100, 500);
  slow.clock.advance(100); slow.sc.move(100, 470);
  slow.clock.advance(500);
  const before = slow.wheels.length;
  slow.sc.end();
  slow.clock.advance(1000);
  assert.equal(slow.wheels.length, before);
  // a new touch during momentum stops it
  const tw = scroller({ momentum: 300 });
  tw.sc.start(100, 500);
  tw.clock.advance(10); tw.sc.move(100, 470);
  tw.clock.advance(10); tw.sc.move(100, 440);
  tw.sc.end();
  tw.sc.start(100, 200);
  const frozen = tw.wheels.length;
  tw.clock.advance(1000);
  assert.equal(tw.wheels.length, frozen);
});

// ---------------------------------------------------------------- viewportFit

function viewportWorld({ vv = true } = {}) {
  const props = [];
  const listeners = {};
  const visualViewport = vv ? {
    height: 700, offsetTop: 0,
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) { listeners[type] = (listeners[type] || []).filter((f) => f !== fn); },
  } : undefined;
  const documentElement = { style: { setProperty(k, v) { props.push([k, v]); } } };
  const world = kitWorld({ extra: { visualViewport, innerHeight: 800 } });
  world.w.document.documentElement = documentElement;
  const last = (name) => { const hit = props.filter(([k]) => k === name).pop(); return hit ? hit[1] : undefined; };
  return { ...world, props, last, listeners, visualViewport, fire: (type) => (listeners[type] || []).slice().forEach((fn) => fn({ type })) };
}

test('viewportFit: sets --vvh from visualViewport.height through style.setProperty and follows resize and scroll', () => {
  const v = viewportWorld();
  v.kit.viewportFit();
  assert.equal(v.last('--vvh'), '700px', 'set at once, not only after the first resize');
  v.visualViewport.height = 420.8;                       // the soft keyboard opens
  v.fire('resize');
  assert.equal(v.last('--vvh'), '420px', 'whole pixels (floor)');
  v.visualViewport.height = 700;
  v.visualViewport.offsetTop = 36;
  v.fire('scroll');
  assert.equal(v.last('--vvh'), '700px');
  assert.equal(v.last('--vvt'), '36px', 'iOS moves the visual viewport when it scrolls a focused field into view');
});

test('viewportFit: never writes a style attribute (CSP) and stop() unhooks every listener', () => {
  const v = viewportWorld();
  const handle = v.kit.viewportFit();
  assert.equal(v.w.document.documentElement.getAttribute, undefined);
  handle.stop();
  assert.equal((v.listeners.resize || []).length, 0);
  assert.equal((v.listeners.scroll || []).length, 0);
  const n = v.props.length;
  v.visualViewport.height = 100;
  v.fire('resize');
  assert.equal(v.props.length, n, 'stopped: no more writes');
});

test('viewportFit: without visualViewport it falls back to innerHeight', () => {
  const v = viewportWorld({ vv: false });
  v.kit.viewportFit();
  assert.equal(v.last('--vvh'), '800px');
});

// ---------------------------------------------------------------- fitSoon

function frameStub(term) {
  const resized = [];
  class Event { constructor(type) { this.type = type; } }
  const contentWindow = { Event, dispatchEvent(e) { resized.push(e.type); return true; } };
  if (term) contentWindow.term = term;
  return { contentWindow, resized };
}

test('fitSoon: bursts of calls collapse into one term.fit() after 250 ms of quiet', () => {
  const { kit, clock } = kitWorld();
  let fits = 0;
  const frame = frameStub({ fit() { fits += 1; } });
  kit.fitSoon(frame);
  clock.advance(200);
  kit.fitSoon(frame);
  clock.advance(200);
  kit.fitSoon(frame);
  assert.equal(fits, 0, 'each call restarts the 250 ms');
  clock.advance(249);
  assert.equal(fits, 0);
  clock.advance(1);
  assert.equal(fits, 1);
  clock.advance(5000);
  assert.equal(fits, 1);
});

test('fitSoon: when term.fit is missing the iframe window gets a resize event; a missing iframe is tolerated', () => {
  const { kit, clock } = kitWorld();
  const frame = frameStub(null);
  kit.fitSoon(frame);
  clock.advance(250);
  assert.deepEqual(frame.resized, ['resize']);
  kit.fitSoon(null);
  kit.fitSoon({ contentWindow: null });
  clock.advance(500);
});

// ---------------------------------------------------------------- bind

function styleNode() { return { style: {}, listeners: {}, events: [], addEventListener(t, fn, o) { (this.listeners[t] ||= []).push({ fn, o }); }, dispatchEvent(e) { this.events.push(e); return true; } }; }

/** An iframe stub around a ttyd-like window: window.term, a document with .xterm / .xterm-screen, a WheelEvent constructor. */
function ttydFrame({ withTerm = true, withScreen = true, termAfterMs = null, clock } = {}) {
  const doc = { listeners: {}, addEventListener(t, fn, o) { (this.listeners[t] ||= []).push({ fn, o }); }, documentElement: { style: {} }, body: { style: {} } };
  const xterm = styleNode();
  const screen = styleNode();
  const viewport = styleNode();
  doc.querySelector = (sel) => ({ '.xterm': xterm, '.xterm-screen': withScreen ? screen : null, '.xterm-viewport': viewport }[sel] || null);
  class WheelEvent { constructor(type, init) { this.type = type; Object.assign(this, init); } }
  const term = { options: { fontSize: 13 }, fits: 0, fit() { this.fits += 1; } };
  const win = { document: doc, WheelEvent };
  if (withTerm) win.term = term;
  if (termAfterMs !== null) clock.setTimeout(() => { win.term = term; }, termAfterMs);
  const frame = { contentWindow: win, listeners: {}, addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }, removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((f) => f !== fn); },
    load() { for (const fn of [...(this.listeners.load || [])]) fn({ type: 'load' }); } };
  return { frame, win, doc, term, xterm, screen, viewport };
}

const touchEvent = (type, x, y, extra = {}) => {
  const t = { clientX: x, clientY: y };
  const calls = { prevented: 0 };
  return { type, touches: type === 'touchend' ? [] : [t], changedTouches: [t], target: extra.target, cancelable: true, preventDefault() { calls.prevented += 1; }, calls, ...extra };
};
const fireAll = (node, type, ev) => { for (const l of node.listeners[type] || []) l.fn(ev); };

test('bind: on load it reports activity, applies the font size, hardens overscroll and touch-action through the CSSOM', () => {
  const { kit, clock } = kitWorld();
  const f = ttydFrame({ clock });
  const seen = [];
  const handle = kit.bind(f.frame, { onActive: (what) => seen.push(what), touchScroll: true, fontSize: 15 });
  assert.equal(typeof handle.setFontSize, 'function');
  assert.equal(f.term.fits, 0, 'nothing happens before the iframe loads');
  f.frame.load();
  for (const type of ['focusin', 'mousedown', 'touchstart', 'keydown']) {
    assert.equal((f.doc.listeners[type] || []).length, 1, `capture listener for ${type}`);
    assert.equal(f.doc.listeners[type][0].o.capture, true, `${type} is a capture-phase listener`);
    f.doc.listeners[type][0].fn({ type });
  }
  assert.deepEqual(seen, ['focusin', 'mousedown', 'touchstart', 'keydown']);
  assert.equal(f.term.options.fontSize, 15);
  assert.equal(f.term.fits, 1, 'the font size is applied and the terminal re-fitted');
  assert.equal(f.xterm.style.touchAction, 'none');
  assert.equal(f.doc.documentElement.style.overscrollBehavior, 'none');
  assert.equal(f.doc.body.style.overscrollBehavior, 'none');
  assert.equal(handle.setFontSize(17), 17);
  assert.equal(f.term.options.fontSize, 17);
  assert.equal(f.term.fits, 2);
  assert.equal(handle.setFontSize('x'), null);
  assert.equal(f.term.options.fontSize, 17);
});

test('bind: the touch shim turns a swipe on .xterm-screen into wheel events and cancels the touchmove once it scrolls', () => {
  const { kit, clock } = kitWorld();
  const f = ttydFrame({ clock });
  kit.bind(f.frame, { touchScroll: true });
  f.frame.load();
  assert.ok((f.screen.listeners.touchstart || []).length && (f.screen.listeners.touchmove || []).length && (f.screen.listeners.touchend || []).length);
  const move = f.screen.listeners.touchmove[0];
  assert.equal(move.o && move.o.passive, false, 'touchmove must be non-passive to be cancelable');
  fireAll(f.screen, 'touchstart', touchEvent('touchstart', 200, 500, { target: f.screen }));
  const tiny = touchEvent('touchmove', 200, 497);
  fireAll(f.screen, 'touchmove', tiny);
  assert.equal(f.screen.events.length, 0);
  assert.equal(tiny.calls.prevented, 0, 'a tap stays a tap: its touchmove is not cancelled');
  const swipe = touchEvent('touchmove', 200, 440);
  fireAll(f.screen, 'touchmove', swipe);
  assert.equal(f.screen.events.length, 3, '60 px up = 3 wheel steps');
  assert.ok(f.screen.events.every((e) => e.type === 'wheel' && e.deltaY > 0 && e.bubbles === true && e.cancelable === true));
  assert.ok(swipe.calls.prevented >= 1, 'an engaged swipe cancels the touchmove (no page scroll, no emulated mouse events)');
  fireAll(f.screen, 'touchend', touchEvent('touchend', 200, 440));
  assert.equal(f.screen.events.length, 3, 'lifting the finger adds nothing when momentum has no velocity to carry');
});

test('bind: a fast flick keeps scrolling for about 300 ms after the finger lifts, a slow drag does not', () => {
  const { kit, clock } = kitWorld();
  const f = ttydFrame({ clock });
  kit.bind(f.frame, { touchScroll: true });
  f.frame.load();
  fireAll(f.screen, 'touchstart', touchEvent('touchstart', 200, 600, { target: f.screen }));
  for (const y of [580, 560, 540]) { clock.advance(10); fireAll(f.screen, 'touchmove', touchEvent('touchmove', 200, y)); }
  const live = f.screen.events.length;
  assert.ok(live >= 3);
  fireAll(f.screen, 'touchend', touchEvent('touchend', 200, 540));
  clock.advance(400);
  const tail = f.screen.events.length - live;
  assert.ok(tail >= 1, 'the flick carries on');
  assert.ok(f.screen.events.slice(live).every((e) => e.deltaY > 0), 'in the direction of the flick');
  const settled = f.screen.events.length;
  clock.advance(5000);
  assert.equal(f.screen.events.length, settled, 'and ends: 300 ms, not forever');
  assert.equal(clock.pending, 0);
  fireAll(f.screen, 'touchstart', touchEvent('touchstart', 200, 600, { target: f.screen }));
  clock.advance(100); fireAll(f.screen, 'touchmove', touchEvent('touchmove', 200, 560));
  clock.advance(600);                                          // the finger rests before it lifts
  const rested = f.screen.events.length;
  fireAll(f.screen, 'touchend', touchEvent('touchend', 200, 560));
  clock.advance(1000);
  assert.equal(f.screen.events.length, rested);
});

test('bind: a swipe from the left edge and a two-finger pinch do not scroll', () => {
  const { kit, clock } = kitWorld();
  const f = ttydFrame({ clock });
  kit.bind(f.frame, { touchScroll: true });
  f.frame.load();
  fireAll(f.screen, 'touchstart', touchEvent('touchstart', 8, 500, { target: f.screen }));
  fireAll(f.screen, 'touchmove', touchEvent('touchmove', 8, 300));
  assert.equal(f.screen.events.length, 0, 'the iOS back swipe');
  const two = { clientX: 100, clientY: 400 };
  fireAll(f.screen, 'touchstart', { type: 'touchstart', touches: [two, two], changedTouches: [two], target: f.screen, cancelable: true, preventDefault() {} });
  fireAll(f.screen, 'touchmove', { type: 'touchmove', touches: [{ clientX: 100, clientY: 300 }, two], changedTouches: [two], cancelable: true, preventDefault() {} });
  assert.equal(f.screen.events.length, 0, 'a pinch is not a scroll');
});

test('bind: touchScroll:false installs no touch listeners but still hardens the page', () => {
  const { kit, clock } = kitWorld();
  const f = ttydFrame({ clock });
  kit.bind(f.frame, { touchScroll: false });
  f.frame.load();
  assert.equal((f.screen.listeners.touchstart || []).length, 0);
  assert.equal(f.xterm.style.touchAction, 'none');
  assert.equal(f.doc.documentElement.style.overscrollBehavior, 'none');
});

test('bind: it polls for window.term and .xterm-screen (ttyd builds them after load) and gives up quietly after 10 s', () => {
  const { kit, clock } = kitWorld();
  const late = ttydFrame({ withTerm: false, termAfterMs: 2500, clock });
  kit.bind(late.frame, { touchScroll: true, fontSize: 12 });
  late.frame.load();
  clock.advance(2000);
  assert.equal((late.screen.listeners.touchstart || []).length, 0, 'term is not there yet');
  clock.advance(1000);
  assert.equal((late.screen.listeners.touchstart || []).length, 1, 'bound once term appeared');
  assert.equal(late.term.options.fontSize, 12);
  clock.advance(60000);
  assert.equal(clock.pending, 0, 'the poll stops once bound');

  const never = ttydFrame({ withTerm: false, clock });
  kit.bind(never.frame, { touchScroll: true, onActive() { throw new Error('must not be called'); } });
  never.frame.load();
  clock.advance(10500);
  clock.advance(10000);
  assert.equal(clock.pending, 0, 'gave up: no timer left running');
  assert.equal((never.screen.listeners.touchstart || []).length, 0);
});

test('bind: a cross-origin frame (contentWindow access throws) is tolerated', () => {
  const { kit, clock } = kitWorld();
  const frame = { listeners: {}, addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }, removeEventListener() {}, get contentWindow() { throw new Error('SecurityError'); } };
  const handle = kit.bind(frame, { touchScroll: true, fontSize: 14 });
  frame.listeners.load[0]({ type: 'load' });
  clock.advance(11000);
  assert.equal(handle.setFontSize(14), null);
  assert.equal(handle.term(), null);
  assert.equal(clock.pending, 0);
});

test('bind: every load of the iframe binds again, and destroy() stops listening', () => {
  const { kit, clock } = kitWorld();
  const f = ttydFrame({ clock });
  const handle = kit.bind(f.frame, { touchScroll: false, fontSize: 11 });
  f.frame.load();
  assert.equal(f.term.fits, 1);
  f.frame.load();
  assert.equal(f.term.fits, 2, 'a reconnect or reload (ttyd rebuilds its document) is bound again');
  handle.destroy();
  assert.equal((f.frame.listeners.load || []).length, 0);
  f.frame.load();
  assert.equal(f.term.fits, 2);
});

// ---------------------------------------------------------------- keyBar

function barWorld(opts = {}) {
  const world = kitWorld({ dom: true });
  const calls = { send: [], text: [], scroll: [] };              // scroll stays empty: the bar has no scroll keys any more (the rail owns them), and it ignores a scroll option
  const hold = { send: null };                                   // set to a promise to keep a request "in flight"
  const host = world.w.document.createElement('div');
  const bar = world.kit.keyBar(host, {
    send: (keys) => { calls.send.push(Array.isArray(keys) ? Array.from(keys) : Array.from(keys.keys)); return hold.send; },
    sendText: (text, enter) => { calls.text.push([text, enter]); },
    scroll: (dir) => { calls.scroll.push(dir); },
    ...opts,
  });
  const buttons = () => [...host.querySelectorAll('button')];
  const byKey = (key) => buttons().find((b) => b.getAttribute('data-key') === key);
  return { ...world, calls, hold, host, bar, buttons, byKey };
}

const ev = (type, extra = {}) => { const calls = { prevented: 0 }; return { type, pointerType: 'touch', pointerId: 1, button: 0, cancelable: true, preventDefault() { calls.prevented += 1; }, calls, ...extra }; };
const press = (b) => { const e = ev('pointerdown'); b.dispatchEvent(e); return e; };
const release = (b) => b.dispatchEvent(ev('pointerup'));

test('keyBar: two rows with the contract keys in the contract order, mounted in the host (the scroll keys are the rail\'s)', () => {
  const b = barWorld();
  assert.ok(b.host.contains(b.bar.root), 'the bar is appended to the host');
  assert.equal(b.bar.root.getAttribute('role'), 'toolbar');
  const rows = [...b.bar.root.querySelectorAll('.kb-row')].filter((r) => r.querySelector('button[data-key]') && !r.classList.contains('kb-ask-row'));
  assert.equal(rows.length, 2, 'one 50 px row less than before: PgUp, PgDn, Top and Bottom duplicated the scroll rail');
  const keysOf = (row) => [...row.querySelectorAll('button')].map((x) => x.getAttribute('data-key')).filter((k) => k);
  assert.deepEqual(keysOf(rows[0]), ['Escape', 'Tab', 'BTab', 'C-c', 'Enter']);
  assert.deepEqual(keysOf(rows[1]), ['Up', 'Down', 'Left', 'Right', 'BSpace', 'C-o'], 'Ctrl+O joined the arrows (six columns)');
  const labels = (row) => [...row.querySelectorAll('button')].map((x) => x.textContent);
  assert.deepEqual(labels(rows[1]), ['↑', '↓', '←', '→', '⌫', 'Ctrl+O']);
  assert.ok(labels(rows[0]).includes('Esc') && labels(rows[0]).includes('Tab') && labels(rows[0]).includes('Enter'));
  const btab = b.byKey('BTab');
  assert.equal(btab.textContent, '⇧Tab', 'one 13 px label at every width (no 10.5 px override)');
  assert.equal(btab.getAttribute('title'), 'Shift+Tab', 'the title spells it out');
  assert.equal(btab.getAttribute('aria-label'), 'Shift+Tab');
});

test('keyBar: there are no scroll keys on the bar: PgUp, PgDn, Top and Bottom are the rail\'s, and a scroll option is never called', () => {
  const b = barWorld();
  for (const dir of ['up', 'down', 'top', 'bottom']) assert.equal(b.byKey(dir), undefined, `no ${dir} key`);
  assert.equal(b.buttons().filter((x) => /^(PgUp|PgDn|Top|Bottom)$/.test(x.textContent)).length, 0);
  for (const x of b.buttons()) { press(x); release(x); }
  assert.deepEqual(b.calls.scroll, [], 'pressing every key never scrolls');
});

test('keyBar: every key is a type=button with tabindex -1 and an accessible name, so the soft keyboard and the tab order are left alone', () => {
  const b = barWorld();
  assert.ok(b.buttons().length >= 11, 'five keys, six keys and the More toggle');
  for (const x of b.buttons()) {
    assert.equal(x.getAttribute('type'), 'button');
    assert.equal(x.getAttribute('tabindex'), '-1', x.textContent);
    assert.ok(x.getAttribute('aria-label') || x.textContent, 'a name');
  }
});

test('keyBar: pressing acts on pointerdown and cancels pointerdown, touchstart and mousedown (focus stays in the composer)', () => {
  const b = barWorld();
  const tab = b.byKey('Tab');
  const down = press(tab);
  assert.equal(down.calls.prevented, 1, 'pointerdown is cancelled: the button never takes the focus');
  const ts = ev('touchstart'); tab.dispatchEvent(ts);
  const md = ev('mousedown'); tab.dispatchEvent(md);
  assert.equal(ts.calls.prevented, 1, 'touchstart is cancelled');
  assert.equal(md.calls.prevented, 1, 'mousedown is cancelled');
  assert.deepEqual(b.calls.send, [['Tab']], 'the key went out on pointerdown, once');
  release(tab);
  tab.dispatchEvent(ev('click', { detail: 1 }));
  assert.deepEqual(b.calls.send, [['Tab']], 'the click that follows a pointer press does not send a second time');
  tab.dispatchEvent(ev('click', { detail: 0 }));
  assert.deepEqual(b.calls.send, [['Tab'], ['Tab']], 'a click with detail 0 (keyboard, screen reader) still works');
});

test('keyBar: each plain key sends its tmux key name, a text key sends text', () => {
  const b = barWorld();
  for (const key of ['Escape', 'Tab', 'BTab', 'C-c', 'Enter', 'C-o']) { press(b.byKey(key)); release(b.byKey(key)); }
  assert.deepEqual(b.calls.send, [['Escape'], ['Tab'], ['BTab'], ['C-c'], ['Enter'], ['C-o']]);
  assert.deepEqual(b.calls.scroll, []);
});

test('keyBar: hold an arrow, 400 ms later it repeats every 90 ms, repeats travel in coalesced batches, and it stops on pointerup', async () => {
  const b = barWorld();
  const up = b.byKey('Up');
  press(up);
  await settle();
  assert.deepEqual(b.calls.send, [['Up']], 'one key at once');
  b.clock.advance(399);
  await settle();
  assert.equal(b.calls.send.length, 1, 'nothing before 400 ms: a tap is a tap');
  b.clock.advance(1);                                    // the hold timer fires, the repeat interval starts
  b.clock.advance(90 * 3);                               // three repeat ticks: 90, 180 and 270 ms into the hold
  await settle();
  assert.equal(b.calls.send.length, 1, 'repeats wait for the coalescing window (250 ms after the first tick)');
  b.clock.advance(70);                                   // 340 ms into the hold: the window closes
  await settle();
  assert.deepEqual(b.calls.send[1], ['Up', 'Up', 'Up'], 'the three repeats travel as one request');
  b.clock.advance(90 * 2);                               // two more ticks queue behind a new window
  await settle();
  assert.equal(b.calls.send.length, 2, 'still inside the next window');
  release(up);
  await settle();
  assert.deepEqual(b.calls.send[2], ['Up', 'Up'], 'pointerup flushes what is queued');
  b.clock.advance(5000);
  await settle();
  assert.equal(b.calls.send.length, 3, 'released: no more keys, no timer left');
  assert.equal(b.clock.pending, 0);
});

test('keyBar: repeats that pile up while a request is in flight go out together, at most 20 keys per send', async () => {
  const b = barWorld();
  let release1;
  b.hold.send = new Promise((resolve) => { release1 = resolve; });              // the first /keys request is slow
  const down = b.byKey('Down');
  press(down);
  await settle();
  assert.deepEqual(b.calls.send, [['Down']]);
  b.clock.advance(400 + 90 * 45);                                               // 45 repeat ticks while the request is pending
  await settle();
  assert.equal(b.calls.send.length, 1, 'ticks wait for the request in flight, they do not each open a new one');
  b.hold.send = null;                                                           // later requests are fast
  release1();
  await settle();
  const sizes = b.calls.send.map((k) => k.length);
  assert.deepEqual(sizes, [1, 20, 20, 5], `45 queued ticks go out as 20 + 20 + 5, got ${sizes}`);
  assert.ok(b.calls.send.every((k) => k.length >= 1 && k.length <= 20 && k.every((x) => x === 'Down')));
  release(down);
  await settle();
  assert.equal(b.calls.send.map((k) => k.length).reduce((a, c) => a + c, 0), 46);
});

test('keyBar: backspace repeats like the arrows, Enter and Esc do not repeat when held', async () => {
  const b = barWorld();
  press(b.byKey('BSpace'));
  b.clock.advance(400 + 90 * 3);
  await settle();
  release(b.byKey('BSpace'));
  const typed = b.calls.send.flat();
  assert.equal(typed.length, 4, 'one at once and one per 90 ms tick after the 400 ms delay (batched when a request is still in flight)');
  assert.ok(typed.every((x) => x === 'BSpace'));
  const before = b.calls.send.length;
  press(b.byKey('Enter'));
  b.clock.advance(2000);
  await settle();
  release(b.byKey('Enter'));
  assert.equal(b.calls.send.length, before + 1, 'Enter is sent once however long it is held');
  assert.deepEqual(b.calls.send[before], ['Enter']);
});

test('pressable: a rail button held down scrolls repeatedly but never queues scrolls behind a slow request (repeat, backlog: false)', async () => {
  const world = kitWorld({ dom: true });
  const btn = world.w.document.createElement('button');
  const fired = [];
  let done;
  let hold = new Promise((resolve) => { done = resolve; });
  world.kit.pressable(btn, (n) => { fired.push(n); return hold; }, { repeat: true, every: 150, backlog: false });
  press(btn);
  await settle();
  world.clock.advance(400 + 150 * 20);
  await settle();
  assert.equal(fired.length, 1, 'while the first /scroll is in flight the ticks are dropped, not queued');
  hold = null;
  done();
  await settle();
  assert.equal(fired.length, 1, 'and nothing is replayed afterwards: the finger has no business scrolling later');
  world.clock.advance(300);
  await settle();
  assert.ok(fired.length >= 2, 'it repeats again once the request is back');
  release(btn);
  await settle();
  const n = fired.length;
  world.clock.advance(3000);
  await settle();
  assert.equal(fired.length, n, 'released: it stops');
});

test('keyBar: releasing in any way (cancel, leave, lost capture) ends the hold', async () => {
  for (const type of ['pointercancel', 'pointerleave', 'lostpointercapture']) {
    const b = barWorld();
    press(b.byKey('Left'));
    b.byKey('Left').dispatchEvent(ev(type));
    b.clock.advance(3000);
    await settle();
    assert.equal(b.calls.send.length, 1, `${type} stops the repeat after the first key`);
  }
});

test('keyBar: compact mode swaps Ctrl-C for ^C, marks the bar and hides the rest behind a More toggle that expands and collapses', () => {
  const b = barWorld({ compact: true });
  const root = b.bar.root;
  assert.ok(root.classList.contains('compact'));
  const ctrlC = b.byKey('C-c');
  assert.equal(ctrlC.textContent, '^C');
  const more = b.buttons().find((x) => /more/i.test(x.textContent) || /more/i.test(x.getAttribute('aria-label') || ''));
  assert.ok(more, 'a More toggle');
  assert.equal(more.getAttribute('aria-expanded'), 'false');
  assert.equal(root.classList.contains('more'), false);
  press(more); release(more);
  assert.equal(root.classList.contains('more'), true, 'More shows the other rows');
  assert.equal(more.getAttribute('aria-expanded'), 'true');
  assert.deepEqual(b.calls.send, [], 'the toggle sends nothing to the terminal');
  press(more); release(more);
  assert.equal(root.classList.contains('more'), false);
  assert.equal(more.getAttribute('aria-expanded'), 'false');
  b.bar.setCompact(false);
  assert.equal(root.classList.contains('compact'), false);
  assert.equal(ctrlC.textContent, 'Ctrl-C');
  b.bar.setCompact(true);
  assert.equal(ctrlC.textContent, '^C');
  assert.equal(root.classList.contains('more'), false, 'entering compact mode collapses the extra rows');
});

test('keyBar: the five compact keys are the first row (Esc, ^C, Tab, Shift+Tab, Enter) and the other rows are the ones that collapse', () => {
  const b = barWorld({ compact: true });
  const rows = [...b.bar.root.querySelectorAll('.kb-row')].filter((r) => !r.classList.contains('kb-ask-row'));
  assert.equal(rows.length, 2);
  const first = [...rows[0].querySelectorAll('button')].map((x) => x.getAttribute('data-key')).filter((k) => k);
  assert.deepEqual(first, ['Escape', 'Tab', 'BTab', 'C-c', 'Enter'], 'what compact mode keeps on screen');
  assert.ok(rows[0].querySelector('.kb-more'), 'the More toggle lives in that row');
  assert.ok(!rows[1].querySelector('.kb-more'));
});

test('keyBar: setApproval toggles the y/n row, whose keys type the answer with Enter', () => {
  const b = barWorld();
  assert.equal(b.bar.root.classList.contains('approval'), false);
  b.bar.setApproval(true);
  assert.equal(b.bar.root.classList.contains('approval'), true);
  const ask = b.bar.root.querySelector('.kb-ask-row');
  assert.ok(ask, 'a y/n row exists');
  const [y, n] = [...ask.querySelectorAll('button')];
  press(y); release(y); press(n); release(n);
  assert.deepEqual(b.calls.text, [['y', true], ['n', true]]);
  b.bar.setApproval(false);
  assert.equal(b.bar.root.classList.contains('approval'), false);
});

test('keyBar: callbacks are optional and a thrown error in one does not break the bar', () => {
  const world = kitWorld({ dom: true });
  const host = world.w.document.createElement('div');
  const bar = world.kit.keyBar(host, {});
  const tab = [...host.querySelectorAll('button')].find((x) => x.getAttribute('data-key') === 'Tab');
  press(tab); release(tab);
  const boom = world.kit.keyBar(world.w.document.createElement('div'), { send() { throw new Error('offline'); } });
  assert.ok(bar.root && boom.root);
});

// ---------------------------------------------------------------- the font spike (bind, flag only)
// OFF by default: ccboard:term:font=1 (or bind's `font: true`) loads the vendored JetBrains Mono into the iframe, THEN sets
// term.options.fontFamily and fits. TermKit.fontState is 'off' | 'loading' | 'on' | 'failed', TermKit.fontReady a promise that never rejects.

const FONT_SRC = 'url(/static/vendor/fonts/jetbrains-mono-latin-wght-normal.woff2)';
const TTYD_FAMILY = 'Consolas,Liberation Mono,Menlo,Courier,monospace';

/** A ttyd frame whose window can build FontFace objects the test settles by hand: faces[i].resolve() / .reject(). `mode: 'missing'` has none. */
function fontFrame({ mode = 'hand', clock } = {}) {
  const f = ttydFrame({ clock });
  const faces = [];
  const fonts = { added: [], add(face) { this.added.push(face); return this; } };
  const order = [];                                                    // what happened, in order: made, added, family:<v>, fit
  f.term.options.fontFamily = TTYD_FAMILY;
  let family = TTYD_FAMILY;
  Object.defineProperty(f.term.options, 'fontFamily', { get: () => family, set: (v) => { family = v; order.push('family'); }, enumerable: true });
  const baseFit = f.term.fit;
  f.term.fit = function fit() { order.push('fit'); return baseFit.call(this); };
  if (mode !== 'missing') {
    f.win.FontFace = class FontFace {
      constructor(fam, source, descriptors) { this.family = fam; this.source = source; this.descriptors = descriptors; faces.push(this); order.push('made'); }
      load() { return new Promise((resolve, reject) => { this.resolve = () => resolve(this); this.reject = (e) => reject(e || new Error('network')); }); }
    };
    f.doc.fonts = { add(face) { fonts.added.push(face); order.push('added'); return this; } };
  }
  return { ...f, faces, fonts, order };
}

test('font spike: off by default, and a fresh namespace says so', async () => {
  const { kit, clock } = kitWorld();
  assert.equal(kit.fontState, 'off');
  assert.equal(await kit.fontReady, 'off');
  const f = fontFrame({ clock });
  kit.bind(f.frame, { touchScroll: false, fontSize: 13 });
  f.frame.load();
  assert.equal(kit.fontState, 'off');
  assert.equal(f.faces.length, 0, 'no FontFace is built unless asked');
  assert.equal(f.term.options.fontFamily, TTYD_FAMILY);
  assert.equal(await kit.fontReady, 'off');
  assert.equal(clock.pending, 0, 'and no timer is left running');
});

test('font spike: bind({font:true}) loads first, adds to document.fonts, THEN sets the family and fits', async () => {
  const { kit, clock } = kitWorld();
  const f = fontFrame({ clock });
  kit.bind(f.frame, { touchScroll: false, fontSize: 15, font: true });
  f.frame.load();
  assert.equal(kit.fontState, 'loading');
  assert.equal(f.faces.length, 1);
  assert.equal(f.faces[0].family, 'JetBrains Mono');
  assert.equal(f.faces[0].source, FONT_SRC);
  assert.deepEqual(plain(f.faces[0].descriptors), { weight: '100 800' });
  assert.equal(f.fonts.added.length, 0, 'not added before the download finished');
  assert.equal(f.term.options.fontFamily, TTYD_FAMILY, 'the family is untouched while loading');
  const ready = kit.fontReady;
  f.faces[0].resolve();
  await settle();
  assert.equal(kit.fontState, 'on');
  assert.equal(await ready, 'on');
  assert.equal(f.fonts.added.length, 1);
  assert.equal(f.term.options.fontFamily, "'JetBrains Mono', " + TTYD_FAMILY, 'prepended: ttyd\'s own fonts stay as the fallback');
  assert.deepEqual(f.order, ['fit', 'made', 'added', 'family', 'fit'],
    'fit (the font size step), made, added, family, fit: the face is in document.fonts before xterm measures with the new family');
  assert.equal(f.term.options.fontSize, 15, 'the size step still ran');
  assert.equal(clock.pending, 0, 'the load timer is gone once settled');
});

test('font spike: a rejected load ends as failed and leaves ttyd\'s fonts alone', async () => {
  const { kit, clock } = kitWorld();
  const f = fontFrame({ clock });
  kit.bind(f.frame, { touchScroll: true, fontSize: 13, font: true });
  f.frame.load();
  assert.equal(kit.fontState, 'loading');
  f.faces[0].reject();
  await settle();
  assert.equal(kit.fontState, 'failed');
  assert.equal(await kit.fontReady, 'failed', 'fontReady resolves, it never rejects');
  assert.equal(f.fonts.added.length, 0);
  assert.equal(f.term.options.fontFamily, TTYD_FAMILY);
  assert.equal(f.xterm.style.touchAction, 'none', 'and the rest of bind() was not affected');
  assert.equal((f.screen.listeners.touchstart || []).length, 1);
  assert.equal(clock.pending, 0);
});

test('font spike: bind() never throws when FontFace or document.fonts is missing, the state is failed, the terminal still binds', async () => {
  for (const variant of ['no FontFace', 'no document.fonts']) {
    const { kit, clock } = kitWorld();
    const f = fontFrame({ mode: variant === 'no FontFace' ? 'missing' : 'hand', clock });
    if (variant === 'no document.fonts') delete f.doc.fonts;
    kit.bind(f.frame, { touchScroll: true, fontSize: 12, font: true });
    assert.doesNotThrow(() => f.frame.load(), variant);
    assert.equal(kit.fontState, 'failed', variant);
    assert.equal(await kit.fontReady, 'failed', variant);
    assert.equal(f.term.options.fontFamily, TTYD_FAMILY, variant);
    assert.equal(f.term.options.fontSize, 12, variant);
    assert.equal((f.screen.listeners.touchstart || []).length, 1, `${variant}: touch shim installed`);
    assert.equal(clock.pending, 0, variant);
  }
});

test('font spike: a FontFace constructor that throws, a load() that throws, and a fonts.add() that throws all end as failed', async () => {
  for (const what of ['constructor', 'load', 'add']) {
    const { kit, clock } = kitWorld();
    const f = fontFrame({ clock });
    if (what === 'constructor') f.win.FontFace = function Boom() { throw new Error('bad descriptor'); };
    if (what === 'load') f.win.FontFace = class { load() { throw new Error('sync boom'); } };
    if (what === 'add') f.doc.fonts.add = () => { throw new Error('read-only set'); };
    kit.bind(f.frame, { touchScroll: false, font: true });
    assert.doesNotThrow(() => f.frame.load(), what);
    if (what === 'add') { f.faces[0].resolve(); }
    await settle();
    assert.equal(kit.fontState, 'failed', what);
    assert.equal(f.term.options.fontFamily, TTYD_FAMILY, `${what}: family untouched`);
    assert.equal(clock.pending, 0, what);
  }
});

test('font spike: a download that stalls ends as failed after 8 s, and arriving later changes nothing', async () => {
  const { kit, clock } = kitWorld();
  const f = fontFrame({ clock });
  kit.bind(f.frame, { touchScroll: false, font: true });
  f.frame.load();
  clock.advance(7999);
  await settle();
  assert.equal(kit.fontState, 'loading');
  clock.advance(1);
  await settle();
  assert.equal(kit.fontState, 'failed');
  assert.equal(await kit.fontReady, 'failed');
  f.faces[0].resolve();
  await settle();
  assert.equal(kit.fontState, 'failed', 'late arrival after the verdict');
  assert.equal(f.fonts.added.length, 0);
  assert.equal(f.term.options.fontFamily, TTYD_FAMILY);
});

test('font spike: the flag ccboard:term:font=1 turns it on when bind() is not told, and an explicit font:false beats the flag', async () => {
  const { kit, clock, w } = kitWorld();
  w.localStorage.setItem('ccboard:term:font', '1');
  const on = fontFrame({ clock });
  kit.bind(on.frame, { touchScroll: false });
  on.frame.load();
  assert.equal(kit.fontState, 'loading');
  on.faces[0].resolve();
  await settle();
  assert.equal(kit.fontState, 'on');

  const kit2 = kitWorld();
  kit2.w.localStorage.setItem('ccboard:term:font', '1');
  const off = fontFrame({ clock: kit2.clock });
  kit2.kit.bind(off.frame, { touchScroll: false, font: false });
  off.frame.load();
  assert.equal(kit2.kit.fontState, 'off');
  assert.equal(off.faces.length, 0);

  const kit3 = kitWorld();
  kit3.w.localStorage.setItem('ccboard:term:font', '0');
  const zero = fontFrame({ clock: kit3.clock });
  kit3.kit.bind(zero.frame, { touchScroll: false });
  zero.frame.load();
  assert.equal(kit3.kit.fontState, 'off', 'only the exact value 1 turns it on');
});

test('font spike: ?font=1 on the page URL turns it on and keeps it on, ?font=0 turns it off and forgets it (a phone has no console)', async () => {
  const on = kitWorld();
  on.w.location.search = '?font=1&x=2';
  const a = fontFrame({ clock: on.clock });
  on.kit.bind(a.frame, { touchScroll: false });
  a.frame.load();
  assert.equal(on.kit.fontState, 'loading');
  assert.equal(on.w.localStorage.getItem('ccboard:term:font'), '1', 'persisted for the next visit');
  a.faces[0].resolve();
  await settle();
  assert.equal(on.kit.fontState, 'on');

  const off = kitWorld();
  off.w.localStorage.setItem('ccboard:term:font', '1');
  off.w.location.search = '?font=0';
  const b = fontFrame({ clock: off.clock });
  off.kit.bind(b.frame, { touchScroll: false });
  b.frame.load();
  assert.equal(off.kit.fontState, 'off');
  assert.equal(b.faces.length, 0);
  assert.equal(off.w.localStorage.getItem('ccboard:term:font'), null, 'forgotten');

  const other = kitWorld();
  other.w.location.search = '?font=yes';
  const c = fontFrame({ clock: other.clock });
  other.kit.bind(c.frame, { touchScroll: false });
  c.frame.load();
  assert.equal(other.kit.fontState, 'off', 'only 1 and 0 mean anything');
  assert.equal(other.w.localStorage.getItem('ccboard:term:font'), null);

  const stuck = kitWorld({ extra: { localStorage: { getItem() { throw new Error('SecurityError'); }, setItem() { throw new Error('SecurityError'); }, removeItem() { throw new Error('SecurityError'); } } } });
  stuck.w.location.search = '?font=1';
  const d = fontFrame({ clock: stuck.clock });
  stuck.kit.bind(d.frame, { touchScroll: false });
  assert.doesNotThrow(() => d.frame.load());
  assert.equal(stuck.kit.fontState, 'loading', 'blocked storage: the URL still decides this load');
});

test('font spike: unreadable localStorage means off, never an exception', () => {
  const { kit, clock } = kitWorld({ extra: { localStorage: { getItem() { throw new Error('SecurityError'); } } } });
  const f = fontFrame({ clock });
  kit.bind(f.frame, { touchScroll: false });
  assert.doesNotThrow(() => f.frame.load());
  assert.equal(kit.fontState, 'off');
});

test('font spike: a window.term that never appears ends as failed with the poll, not as loading for ever', async () => {
  const { kit, clock } = kitWorld();
  const f = fontFrame({ clock });
  delete f.win.term;
  kit.bind(f.frame, { touchScroll: false, font: true });
  f.frame.load();
  assert.equal(kit.fontState, 'loading', 'asked for, waiting for ttyd to build the terminal');
  clock.advance(10500);
  await settle();
  assert.equal(kit.fontState, 'failed');
  assert.equal(f.faces.length, 0);
  assert.equal(clock.pending, 0);
});

test('font spike: a cross-origin frame ends as failed and does not throw', async () => {
  const { kit, clock } = kitWorld();
  const frame = { listeners: {}, addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }, removeEventListener() {}, get contentWindow() { throw new Error('SecurityError'); } };
  kit.bind(frame, { touchScroll: false, font: true });
  assert.doesNotThrow(() => frame.listeners.load[0]({ type: 'load' }));
  clock.advance(11000);
  await settle();
  assert.equal(kit.fontState, 'failed');
  assert.equal(clock.pending, 0);
});

test('font spike: a reload of the iframe starts a new attempt, and the old download cannot touch the new document', async () => {
  const { kit, clock } = kitWorld();
  const f = fontFrame({ clock });
  kit.bind(f.frame, { touchScroll: false, font: true });
  f.frame.load();
  const first = f.faces[0];
  const firstReady = kit.fontReady;
  f.frame.load();                                                      // ttyd reconnected and rebuilt its document before the font arrived
  assert.equal(kit.fontState, 'loading');
  assert.equal(f.faces.length, 2, 'a second FontFace for the second load');
  assert.equal(await firstReady, 'off', 'the first attempt is over, and says so');
  first.resolve();
  await settle();
  assert.equal(f.fonts.added.length, 0, 'the superseded attempt added nothing');
  assert.equal(kit.fontState, 'loading', 'and did not decide the state of the new one');
  f.faces[1].resolve();
  await settle();
  assert.equal(kit.fontState, 'on');
  assert.equal(f.fonts.added.length, 1);
  assert.equal(f.fonts.added[0], f.faces[1]);
});

test('font spike: destroy() while loading settles as off and the late download changes nothing', async () => {
  const { kit, clock } = kitWorld();
  const f = fontFrame({ clock });
  const handle = kit.bind(f.frame, { touchScroll: false, font: true });
  f.frame.load();
  const ready = kit.fontReady;
  handle.destroy();
  assert.equal(await ready, 'off');
  assert.equal(kit.fontState, 'off');
  f.faces[0].resolve();
  await settle();
  assert.equal(kit.fontState, 'off');
  assert.equal(f.fonts.added.length, 0);
  assert.equal(f.term.options.fontFamily, TTYD_FAMILY);
  assert.equal(clock.pending, 0);
});

test('font spike: several terminals on one page each get the font, TermKit.fontState follows the newest, and one terminal without it leaves the others alone', async () => {
  const { kit, clock } = kitWorld();
  const a = fontFrame({ clock });
  const b = fontFrame({ clock });
  const c = fontFrame({ clock });
  kit.bind(a.frame, { touchScroll: false, font: true });
  kit.bind(b.frame, { touchScroll: false, font: true });
  kit.bind(c.frame, { touchScroll: false, font: false });
  a.frame.load();
  b.frame.load();
  c.frame.load();
  assert.equal(kit.fontState, 'loading');
  a.faces[0].resolve();
  await settle();
  assert.equal(a.term.options.fontFamily, "'JetBrains Mono', " + TTYD_FAMILY, 'the older attempt still lands in its own terminal');
  assert.equal(kit.fontState, 'loading', 'the state is the newest attempt\'s');
  b.faces[0].resolve();
  await settle();
  assert.equal(kit.fontState, 'on');
  assert.equal(c.term.options.fontFamily, TTYD_FAMILY);
});

test('font spike: the family is prepended once (a terminal that already names JetBrains Mono is left as it is)', async () => {
  const { kit, clock } = kitWorld();
  const f = fontFrame({ clock });
  f.term.options.fontFamily = "'JetBrains Mono', monospace";
  kit.bind(f.frame, { touchScroll: false, font: true });
  f.frame.load();
  f.faces[0].resolve();
  await settle();
  assert.equal(kit.fontState, 'on');
  assert.equal(f.term.options.fontFamily, "'JetBrains Mono', monospace");
});

test('font spike: with the font on, setFontSize and fit keep working and the font step does not delay the touch shim', async () => {
  const { kit, clock } = kitWorld();
  const f = fontFrame({ clock });
  const handle = kit.bind(f.frame, { touchScroll: true, fontSize: 11, font: true });
  f.frame.load();
  assert.equal((f.screen.listeners.touchstart || []).length, 1, 'bound while the font is still downloading');
  assert.equal(handle.setFontSize(13), 13);
  f.faces[0].resolve();
  await settle();
  assert.equal(handle.setFontSize(14), 14);
  assert.equal(f.term.options.fontSize, 14);
});

// The fake tty (scripts/dev/fake_tty/fake_tty.js) is what scripts/qa_terminal.sh puts in the iframe: run it for real in a vm window and
// bind TermKit to it, so the node path and the browser path exercise the same code.

function fakeTty(clock, mode) {
  const w = makeWorld({ setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, setInterval: clock.setInterval, clearInterval: clock.clearInterval });
  installDom(w);
  if (mode) w.localStorage.setItem('ccboard:fake:font', mode);
  w.load(path.join(ROOT, 'scripts', 'dev', 'fake_tty', 'fake_tty.js'));
  const win = w.run('globalThis');
  const frame = { contentWindow: win, listeners: {}, addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }, removeEventListener(t, fn) { this.listeners[t] = (this.listeners[t] || []).filter((x) => x !== fn); },
    load() { for (const fn of [...(this.listeners.load || [])]) fn({ type: 'load' }); } };
  return { w, win, frame };
}

test('fake tty: it defines the surface the spike reads (FontFace, document.fonts, term.options.fontFamily) and counts what happens', () => {
  const { win } = fakeTty(fakeClock());
  assert.equal(typeof win.FontFace, 'function');
  assert.equal(typeof win.document.fonts.add, 'function');
  assert.equal(win.term.options.fontFamily, TTYD_FAMILY);
  assert.equal(win.term.options.fontSize, 13);
  assert.deepEqual(plain(win.__font), { mode: '', made: 0, loaded: 0, failed: 0, added: 0, families: [] });
  assert.equal(win.__fitsAtFamily, -1);
});

test('fake tty: a bound spike loads the stand-in, adds it, sets the family and fits after it (cols measured and stable)', async () => {
  const k = kitWorld();
  const t = fakeTty(k.clock, 'stub');
  k.kit.bind(t.frame, { touchScroll: false, fontSize: 13, font: true });
  t.frame.load();
  assert.equal(k.kit.fontState, 'loading');
  assert.equal(t.win.__font.made, 1);
  k.clock.advance(20);
  await settle();
  assert.equal(k.kit.fontState, 'on');
  assert.deepEqual(plain(t.win.__font.families), ['JetBrains Mono']);
  assert.equal(t.win.__font.added, 1);
  assert.match(t.win.term.options.fontFamily, /^'JetBrains Mono', Consolas/);
  assert.ok(t.win.__fits > t.win.__fitsAtFamily, 'a fit() ran after the family was assigned');
  const cols = t.win.term.cols;
  assert.ok(cols > 0 && cols < 400, `cols ${cols}`);
  t.win.term.fit();
  t.win.term.fit();
  assert.equal(t.win.term.cols, cols, 'cols are stable across further fits');
});

test('fake tty: fail, hang and missing modes give failed, failed (after 8 s) and failed, and never break the page', async () => {
  const k = kitWorld();
  const fail = fakeTty(k.clock, 'fail');
  k.kit.bind(fail.frame, { touchScroll: false, font: true });
  fail.frame.load();
  k.clock.advance(20);
  await settle();
  assert.equal(k.kit.fontState, 'failed');
  assert.equal(fail.win.__font.failed, 1);
  assert.equal(fail.win.term.options.fontFamily, TTYD_FAMILY);

  const k2 = kitWorld();
  const hang = fakeTty(k2.clock, 'hang');
  k2.kit.bind(hang.frame, { touchScroll: false, font: true });
  hang.frame.load();
  k2.clock.advance(7000);
  await settle();
  assert.equal(k2.kit.fontState, 'loading');
  k2.clock.advance(1000);
  await settle();
  assert.equal(k2.kit.fontState, 'failed');

  const k3 = kitWorld();
  const missing = fakeTty(k3.clock, 'missing');
  assert.equal(missing.win.FontFace, undefined);
  k3.kit.bind(missing.frame, { touchScroll: false, font: true });
  assert.doesNotThrow(() => missing.frame.load());
  assert.equal(k3.kit.fontState, 'failed');
  assert.equal(missing.win.term.options.fontFamily, TTYD_FAMILY);
});

test('fake tty: without the flag nothing is built, and the family stays ttyd\'s', async () => {
  const k = kitWorld();
  const t = fakeTty(k.clock);
  k.kit.bind(t.frame, { touchScroll: false, fontSize: 13 });
  t.frame.load();
  k.clock.advance(100);
  await settle();
  assert.equal(k.kit.fontState, 'off');
  assert.equal(t.win.__font.made, 0);
  assert.equal(t.win.term.options.fontFamily, TTYD_FAMILY);
});

// ---- the soft-keyboard simulation of the fake tty (?vv=<height> on the page URL, or localStorage ccboard:fake:vv): qa_terminal.sh KBD relies on it

/** the fake tty loaded in a vm window whose parent is a page with a real-looking visualViewport (accessors on the PROTOTYPE, like the browser's) */
function fakeTtyUnder({ search = '', key = null, vv = true, parentLocation = null } = {}) {
  class VisualViewport {
    constructor() { this.events = []; }
    get height() { return 844; }
    get offsetTop() { return 0; }
    dispatchEvent(e) { this.events.push(e.type); return true; }
  }
  const viewport = new VisualViewport();
  const parent = { location: parentLocation || { search }, Event: class { constructor(type) { this.type = type; } } };
  if (vv) parent.visualViewport = viewport;
  const w = makeWorld({ parent });
  installDom(w);
  if (key !== null) w.localStorage.setItem('ccboard:fake:vv', key);
  w.load(path.join(ROOT, 'scripts', 'dev', 'fake_tty', 'fake_tty.js'));
  return { w, viewport, win: w.run('globalThis') };
}

test('fake tty soft keyboard: ?vv=520 on the page URL pins the visual viewport (height 520, offsetTop 0) and fires resize on it', () => {
  const t = fakeTtyUnder({ search: '?qa=1&vv=520' });
  assert.equal(t.viewport.height, 520);
  assert.equal(t.viewport.offsetTop, 0);
  assert.deepEqual(t.viewport.events, ['resize'], 'the page\'s listeners (term.js applyLayout, TermKit.viewportFit) hear it');
  assert.deepEqual(plain(t.win.__vv), { height: 520, offsetTop: 0 });
});

test('fake tty soft keyboard: localStorage ccboard:fake:vv does the same, and the page URL wins over it', () => {
  const k = fakeTtyUnder({ key: '480' });
  assert.equal(k.viewport.height, 480);
  assert.deepEqual(k.viewport.events, ['resize']);
  const both = fakeTtyUnder({ search: '?vv=300', key: '480' });
  assert.equal(both.viewport.height, 300);
});

test('fake tty soft keyboard: off by default, and a bad value, a page without a visualViewport or an unreadable parent change nothing and throw nothing', () => {
  const off = fakeTtyUnder();
  assert.equal(off.viewport.height, 844);
  assert.deepEqual(off.viewport.events, []);
  assert.equal(off.win.__vv, undefined);
  for (const bad of [{ search: '?vv=abc' }, { search: '?vv=0' }, { key: 'x' }, { key: '0' }]) {
    const t = fakeTtyUnder(bad);
    assert.equal(t.viewport.height, 844, JSON.stringify(bad));
    assert.deepEqual(t.viewport.events, [], JSON.stringify(bad));
  }
  const none = fakeTtyUnder({ search: '?vv=520', vv: false });
  assert.equal(none.win.__vv, undefined, 'a parent without a visualViewport: nothing to pin');
  const cross = { get search() { throw new Error('cross-origin'); } };
  const blocked = fakeTtyUnder({ parentLocation: cross, key: '500' });                    // the page URL is unreadable: the stored value still works
  assert.equal(blocked.viewport.height, 500);
  assert.doesNotThrow(() => { const w = makeWorld(); installDom(w); w.load(path.join(ROOT, 'scripts', 'dev', 'fake_tty', 'fake_tty.js')); });   // no parent at all (the fake opened on its own)
});

// ---------------------------------------------------------------- termPane (v0.5.9: the dock and the quad tiles)

/** core.js + termkit.js on minidom, fake timers, recorders for api(), toast and Live; `Dnd` is a recorder when `dnd` is true. */
function paneWorld({ dnd = false, live = true, extra = {} } = {}) {
  const k = kitWorld({ dom: true, extra });
  k.w.ctx.__calls = [];
  k.w.ctx.__toasts = [];
  k.w.ctx.__live = { subscribed: [], unsubscribed: [], fns: {} };
  k.w.ctx.__dnd = [];
  k.w.run(`
    api = async (method, path, body) => { __calls.push({ method, path, body }); return { ok: true }; };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    globalThis.poll = () => { __calls.push({ method: 'poll' }); };
    ${live ? `globalThis.Live = { subscribe(tmux, fn) { __live.subscribed.push(tmux); __live.fns[tmux] = fn; return () => { __live.unsubscribed.push(tmux); delete __live.fns[tmux]; }; } };` : ''}
    ${dnd ? 'globalThis.Dnd = { bind(node) { __dnd.push(node); return node; } };' : ''}
  `);
  return k;
}

const SESS = 'shop--api--s1';
const PANE_GRID = '/tty/?arg=shop--api--s1&arg=grid&fontSize=11&rendererType=canvas&disableResizeOverlay=true&disableReconnect=true';
const srcOf = (p) => p.iframe.src;
const paneBtn = (p, act) => p.root.querySelector(`[data-act=${act}]`);
const paneRow = (over = {}) => ({ tmux: SESS, name: 's1', state: 'working', agent: 'claude', stats: { context_pct: 42 }, last_prompt: 'fix the login bug', last_message: 'done', task: null, flags: {}, ...over });

test('termPane: the namespace carries the pane and its pure helpers', () => {
  const { kit } = kitWorld();
  for (const name of ['termPane', 'paneLabel', 'paneParts', 'paneLine', 'panePending', 'sizeChip', 'typingTarget', 'ctxInfo']) assert.equal(typeof kit[name], 'function', `TermKit.${name}`);
});

test('paneLabel: project/repo · name, the project folder spelled out, anything else as it is', () => {
  const { kit } = kitWorld();
  assert.equal(kit.paneLabel('shop--api--s1'), 'shop/api · s1');
  assert.equal(kit.paneLabel('shop--root--s1'), 'shop/project folder · s1');
  assert.equal(kit.paneLabel('weird'), 'weird');
  assert.equal(kit.paneLabel(null), '');
});

test('paneParts: the two halves of paneLabel (project/repo · , the session name), so a narrow header can cut the first and keep the second', () => {
  const { kit } = kitWorld();
  assert.deepEqual(plain(kit.paneParts('shop--api--s1')), ['shop/api · ', 's1']);
  assert.deepEqual(plain(kit.paneParts('shop--root--s1')), ['shop/project folder · ', 's1']);
  assert.deepEqual(plain(kit.paneParts('weird')), ['', 'weird']);
  assert.deepEqual(plain(kit.paneParts(null)), ['', '']);
  for (const t of ['shop--api--s1', 'phasezero--website--t-checkout-redesign', 'a--b', 'x']) assert.equal(kit.paneParts(t).join(''), kit.paneLabel(t), t);
});

test('fitName: a project/repo cut to a sliver is dropped (.off), a whole one or one with room is kept, and the class comes off first so the room measured is the real one', () => {
  const { kit, w } = kitWorld();
  const mk = (box, scroll) => { const n = w.document.createElement('span'); n.getBoundingClientRect = () => ({ width: box.width }); Object.defineProperty(n, 'scrollWidth', { get: () => scroll }); return n; };
  const box = { width: 9 };
  const where = mk(box, 119);
  assert.equal(kit.fitName(where), true, 'cut to 9 of 119 px: a sliver');
  assert.ok(where.classList.contains('off'));
  box.width = 44;
  assert.equal(kit.fitName(where), false, 'room again: back on');
  assert.equal(where.classList.contains('off'), false);
  box.width = 20;
  assert.equal(kit.fitName(where, 16), false, 'the minimum is the caller\'s');
  assert.equal(kit.fitName(mk({ width: 12 }, 12)), false, 'a whole project/repo is never dropped, however short');
  assert.equal(kit.fitName(mk({ width: 0 }, 119)), false, 'a hidden node (no layout) says nothing');
  assert.equal(kit.fitName(null), false);
  assert.equal(kit.fitName({}), false);
});

test('ctxInfo: one reading of the context chip for the dock and the quad: "ctx N%", tinted from 80 and again from 90, null without a number', () => {
  const { kit } = kitWorld();
  assert.deepEqual(plain(kit.ctxInfo(42.4)), { pct: 42, text: 'ctx 42%', level: '', title: 'context window used: 42%' });
  assert.deepEqual([79, 79.4, 79.5, 80, 89, 89.4, 89.5, 90, 100].map((n) => kit.ctxInfo(n).level), ['', '', 'hi', 'hi', 'hi', 'hi', 'crit', 'crit', 'crit'], 'rounded first, then compared');
  for (const bad of [null, undefined, '42', NaN, Infinity, {}]) assert.equal(kit.ctxInfo(bad), null, String(bad));
});

test('paneLine: the question while waiting, else the task title, else the last prompt; nothing when there is none', () => {
  const { kit } = kitWorld();
  const line = (r) => plain(kit.paneLine(r));
  assert.deepEqual(line({ state: 'waiting', last_message: 'Which one?', task: { title: 'T', phase: 'running' } }), { text: 'Which one?', kind: 'ask', phase: 'running' });
  assert.deepEqual(line({ state: 'working', task: { title: 'Fix the cart', phase: 'in_progress' }, last_prompt: 'p' }), { text: 'Fix the cart', kind: 'task', phase: 'in_progress' });
  assert.deepEqual(line({ state: 'idle', last_prompt: '  fix\n the   login  ' }), { text: 'fix the login', kind: 'prompt', phase: '' });
  assert.deepEqual(line({ state: 'idle' }), { text: '', kind: '', phase: '' });
  assert.deepEqual(line(null), { text: '', kind: '', phase: '' });
  assert.ok(line({ state: 'idle', last_prompt: 'x'.repeat(500) }).text.length <= 240, 'a long prompt is cut');
  assert.equal(line({ state: 'waiting', last_message: '', task: { title: 'T' } }).kind, 'task', 'waiting without a message falls back to the task');
});

test('panePending: the row\'s own pending list, else state.pending_permissions of this session', () => {
  const { kit } = kitWorld();
  const st = { pending_permissions: [{ id: 3, tmux_name: 'other--x--y', summary: 'Bash: rm' }, { id: 12, tmux_name: SESS, summary: 'Bash: npm test', tool_name: 'Bash' }] };
  assert.deepEqual(plain(kit.panePending(st, SESS, paneRow())), { id: 12, summary: 'Bash: npm test' });
  assert.deepEqual(plain(kit.panePending(null, SESS, paneRow({ pending: [{ id: 7, summary: 'Edit: a.py' }] }))), { id: 7, summary: 'Edit: a.py' });
  assert.equal(kit.panePending(st, 'nobody--x--y', paneRow()), null);
  assert.equal(kit.panePending(null, SESS, null), null);
  assert.equal(plain(kit.panePending({ pending_permissions: [{ id: 1, tmux_name: SESS, tool_name: 'Bash' }] }, SESS, null)).summary, 'Bash', 'no summary: the tool name');
});

test('sizeChip: NxM, or cropped when the window is more than two columns or rows bigger than the pane', () => {
  const { kit } = kitWorld();
  assert.deepEqual(plain(kit.sizeChip(45, 30, [46, 31])).text, '45x30');
  assert.equal(plain(kit.sizeChip(45, 30, [47, 30])).text, '45x30', 'two columns more is still the same view');
  const c = plain(kit.sizeChip(45, 30, [120, 40]));
  assert.equal(c.text, 'cropped');
  assert.equal(c.cropped, true);
  assert.match(c.title, /45x30 of the 120x40 window/);
  assert.equal(plain(kit.sizeChip(45, 30, [80, 33])).cropped, true, 'rows count too');
  assert.equal(plain(kit.sizeChip(45, 30, null)).text, '45x30');
  assert.equal(kit.sizeChip(0, 30, [80, 24]), null);
  assert.equal(kit.sizeChip(undefined, undefined, null), null);
});

test('typingTarget: the focused input, textarea, select or contenteditable; nothing for the body, a link or a button', () => {
  const { w, kit } = kitWorld({ dom: true });
  const d = w.document;
  assert.equal(kit.typingTarget(d), null);
  const make = (tag) => { const n = d.createElement(tag); d.body.append(n); n.focus(); return n; };
  for (const tag of ['input', 'textarea', 'select']) { const n = make(tag); assert.equal(kit.typingTarget(d), n, tag); n.blur(); }
  const ce = d.createElement('div'); ce.isContentEditable = true; d.body.append(ce); ce.focus();
  assert.equal(kit.typingTarget(d), ce);
  ce.blur();
  for (const tag of ['a', 'button', 'div']) { make(tag); assert.equal(kit.typingTarget(d), null, tag); }
});

test('termPane: article.tpane[data-tmux][data-mode] with a header, a body and one iframe whose src is exactly ttyUrl', () => {
  const { w, kit } = paneWorld();
  const p = kit.termPane(SESS, { mode: 'full' });
  assert.equal(p.root.tagName, 'ARTICLE');
  assert.ok(p.root.classList.contains('tpane'));
  assert.equal(p.root.getAttribute('data-tmux'), SESS);
  assert.equal(p.root.getAttribute('data-mode'), 'full');
  assert.equal(p.name, SESS);
  assert.ok(p.head.classList.contains('tp-head') && p.body.classList.contains('tp-body'));
  assert.equal(p.root.querySelectorAll('iframe').length, 1);
  assert.equal(p.iframe.parentNode, p.body);
  assert.equal(srcOf(p), `/tty/?arg=${SESS}`);
  assert.equal(p.root.querySelector('.tp-name').textContent, 'shop/api · s1');
  assert.equal(p.root.querySelector('.tp-name').getAttribute('title'), SESS);
  assert.equal(p.root.querySelector('.tp-where').textContent, 'shop/api · ', 'two spans: the project/repo gives way first in a narrow dock ...');
  assert.equal(p.root.querySelector('.tp-sess').textContent, 's1', '... and the session name stays whole');
  assert.equal(p.iframe.getAttribute('allow'), 'clipboard-write');
  assert.equal(p.mode, 'full');
  assert.equal(w.document.querySelectorAll('.tpane').length, 0, 'the pane is built detached: the owner mounts it');
});

test('termPane: grid is the exact grid contract string; ro adds the second argument; renderer, font and quiet pass through', () => {
  const { kit } = paneWorld();
  assert.equal(srcOf(kit.termPane(SESS, { mode: 'grid' })), PANE_GRID);
  const ro = new URLSearchParams(srcOf(kit.termPane(SESS, { mode: 'ro' })).slice('/tty/?'.length));
  assert.deepEqual(ro.getAll('arg'), [SESS, 'ro']);
  assert.equal(srcOf(kit.termPane(SESS, { fontSize: 15 })), `/tty/?arg=${SESS}&fontSize=15`);
  assert.match(srcOf(kit.termPane(SESS, { mode: 'full', renderer: 'dom', quiet: true })), /rendererType=dom&disableResizeOverlay=true&disableReconnect=true$/);
});

test('termPane: a full pane starts at the terminal page\'s text size (ccboard:term:fs); a grid pane keeps its own 11', () => {
  const { w, kit } = paneWorld();
  w.localStorage.setItem('ccboard:term:fs', '16');
  assert.equal(srcOf(kit.termPane(SESS, { mode: 'full' })), `/tty/?arg=${SESS}&fontSize=16`);
  assert.equal(srcOf(kit.termPane(SESS, { mode: 'full', fontSize: 12 })), `/tty/?arg=${SESS}&fontSize=12`, 'an explicit size wins');
  assert.equal(srcOf(kit.termPane(SESS, { mode: 'grid' })), PANE_GRID);
  w.localStorage.setItem('ccboard:term:fs', 'huge');
  assert.equal(srcOf(kit.termPane(SESS, { mode: 'full' })), `/tty/?arg=${SESS}`, 'a bad stored size is ignored');
});

test('termPane: a bad session name or mode throws, and nothing is built', () => {
  const { kit } = paneWorld();
  for (const bad of ['', null, undefined, 7]) assert.throws(() => kit.termPane(bad), /bad session name/);
  for (const mode of ['', 'GRID', 'rw', '-f', 'full;ls']) assert.throws(() => kit.termPane(SESS, { mode }), /bad pane mode/, mode);
});

test('termPane: header:false is the body alone (the quad draws its own header, task and permission lines)', () => {
  const { kit } = paneWorld();
  const p = kit.termPane(SESS, { mode: 'grid', header: false });
  assert.equal(p.head, null);
  assert.equal(p.root.querySelector('.tp-head'), null);
  assert.equal(p.root.querySelector('.tp-task'), null);
  assert.equal(p.root.querySelector('.tp-perm'), null);
  assert.equal(p.root.children.length, 1);
  assert.doesNotThrow(() => p.update(paneRow(), {}));
  assert.equal(srcOf(p), PANE_GRID);
});

test('termPane: a button exists only for a callback; each one calls it with the session name', () => {
  const { kit } = paneWorld();
  const none = kit.termPane(SESS);
  assert.deepEqual(none.root.querySelectorAll('.tp-btn').map((b) => b.getAttribute('data-act')), ['reconnect', 'popout'], 'reconnect and the link to /term/ are always there');
  const calls = [];
  const p = kit.termPane(SESS, { onClose: (t) => calls.push(['close', t]), onAddToQuad: (t) => calls.push(['quad', t]), onPopOut: (t) => calls.push(['pop', t]) });
  assert.deepEqual(p.root.querySelectorAll('.tp-btn').map((b) => b.getAttribute('data-act')), ['reconnect', 'quad', 'popout', 'close']);
  paneBtn(p, 'close').click();
  paneBtn(p, 'quad').click();
  assert.deepEqual(calls, [['close', SESS], ['quad', SESS]]);
  for (const b of p.root.querySelectorAll('.tp-btn')) assert.ok(b.getAttribute('aria-label') && b.getAttribute('title'), 'every button is named');
  for (const b of p.root.querySelectorAll('button')) assert.equal(b.getAttribute('type'), 'button');
});

test('termPane: pop out is a link to /term/<name> in a new tab; a plain click calls onPopOut and keeps the page, a modified click is the browser\'s', () => {
  const { kit } = paneWorld();
  const calls = [];
  const p = kit.termPane(SESS, { onPopOut: (t) => calls.push(t) });
  const a = paneBtn(p, 'popout');
  assert.equal(a.tagName, 'A');
  assert.equal(a.getAttribute('href'), `/term/${SESS}`);
  assert.equal(a.getAttribute('target'), '_blank');
  assert.equal(a.getAttribute('rel'), 'noopener');
  const click = (extra = {}) => { const e = { type: 'click', button: 0, prevented: 0, preventDefault() { this.prevented += 1; }, ...extra }; a.dispatchEvent(e); return e; };
  assert.equal(click().prevented, 1);
  assert.deepEqual(calls, [SESS]);
  for (const mod of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) assert.equal(click(mod).prevented, 0, JSON.stringify(mod));
  assert.equal(calls.length, 1);
  const plainLink = kit.termPane(SESS);                                         // no callback: the link is just a link
  const e = { type: 'click', button: 0, prevented: 0, preventDefault() { this.prevented += 1; } };
  paneBtn(plainLink, 'popout').dispatchEvent(e);
  assert.equal(e.prevented, 0);
});

test('termPane: the header carries data-drop=session and data-tmux and is bound for dnd.js; drop:false leaves it out', () => {
  const { kit, w } = paneWorld({ dnd: true });
  const p = kit.termPane(SESS);
  assert.equal(p.head.getAttribute('data-drop'), 'session');
  assert.equal(p.head.getAttribute('data-tmux'), SESS);
  assert.deepEqual(plain(w.get('__dnd.length')), 1);
  assert.equal(w.get('__dnd[0]'), p.head);
  const q = kit.termPane(SESS, { drop: false });
  assert.equal(q.head.getAttribute('data-drop'), null);
  assert.equal(q.head.getAttribute('data-tmux'), null);
  assert.doesNotThrow(() => paneWorld().kit.termPane(SESS), 'without Dnd (a page that does not load dnd.js) the attributes are still set');
});

test('termPane.update: state and agent glyphs, context %, task line, and nothing is rewritten when nothing changed', () => {
  const { kit } = paneWorld();
  const p = kit.termPane(SESS);
  p.update(paneRow({ state: 'working', agent: 'codex', stats: { context_pct: 42.4 } }), {});
  const g = p.root.querySelector('.tp-g');
  assert.deepEqual(g.querySelectorAll('.glyph').map((n) => n.getAttribute('aria-label')), ['working', 'codex']);
  const ctx = p.root.querySelector('.tp-ctx');
  assert.equal(ctx.textContent, 'ctx 42%');
  assert.equal(ctx.classList.contains('hidden'), false);
  const first = g.firstChild;
  const task = p.root.querySelector('.tp-task');
  assert.equal(task.classList.contains('hidden'), false);
  assert.equal(task.querySelector('.tp-task-text').textContent, '› fix the login bug');
  assert.equal(task.getAttribute('data-kind'), 'prompt');
  p.update(paneRow({ state: 'working', agent: 'codex', stats: { context_pct: 42.1 } }), {});
  assert.equal(g.firstChild, first, 'the same glyph nodes: a repaint with the same state touches nothing');
  p.update(paneRow({ state: 'waiting', stats: { context_pct: 85 }, task: { title: 'Fix the cart', phase: 'needs_you' }, last_message: 'Which cart?' }), {});
  assert.equal(ctx.classList.contains('warn'), true);
  assert.equal(task.getAttribute('data-kind'), 'ask');
  assert.equal(task.querySelector('.tp-task-text').textContent, 'Which cart?');
  assert.equal(task.querySelector('.tp-phase').textContent, 'needs you');
  p.update(paneRow({ state: 'idle', stats: { context_pct: 95 }, task: { title: 'Fix the cart', phase: 'running' } }), {});
  assert.equal(ctx.classList.contains('bad'), true);
  assert.equal(ctx.classList.contains('warn'), false);
  assert.equal(task.getAttribute('data-kind'), 'task');
  p.update(paneRow({ stats: {}, last_prompt: '', task: null }), {});
  assert.equal(ctx.classList.contains('hidden'), true);
  assert.equal(task.classList.contains('hidden'), true, 'no task, no prompt: no line');
});

test('termPane.update(null): a session that is gone shows as ended with a line that says so', () => {
  const { kit } = paneWorld();
  const p = kit.termPane(SESS);
  p.update(paneRow(), {});
  p.update(null, {});
  assert.equal(p.root.classList.contains('gone'), true);
  assert.deepEqual(p.root.querySelector('.tp-g').querySelectorAll('.glyph').map((n) => n.getAttribute('aria-label')), ['ended', 'shell']);
  assert.match(p.root.querySelector('.tp-task-text').textContent, /not running any more/);
  p.update(paneRow(), {});
  assert.equal(p.root.classList.contains('gone'), false);
});

test('termPane permission line: Allow (tinted primary), Deny (danger) and In terminal post the three decisions', async () => {
  const { w, kit } = paneWorld();
  const p = kit.termPane(SESS);
  const st = { pending_permissions: [{ id: 12, tmux_name: SESS, summary: 'Bash: npm test' }] };
  const line = p.root.querySelector('.tp-perm');
  assert.equal(line.classList.contains('hidden'), true, 'nothing pending: hidden');
  p.update(paneRow({ state: 'waiting' }), st);
  assert.equal(line.classList.contains('hidden'), false);
  assert.equal(line.querySelector('.tp-perm-text').textContent, 'Bash: npm test');
  const [allow, deny, tui] = line.querySelectorAll('button');
  assert.deepEqual([allow.textContent, deny.textContent, tui.textContent], ['Allow', 'Deny', 'In terminal']);
  assert.ok(allow.classList.contains('bp5-intent-primary') && allow.classList.contains('tinted'), 'Allow: primary, tinted');
  assert.ok(deny.classList.contains('bp5-intent-danger'), 'Deny: danger (red outline)');
  assert.ok(!tui.classList.contains('bp5-intent-primary') && !tui.classList.contains('bp5-intent-danger'), 'In terminal: quiet');
  for (const b of [allow, deny, tui]) { b.click(); await settle(); }
  const posts = plain(w.get('__calls')).filter((c) => c.method === 'POST');
  assert.deepEqual(posts.map((c) => c.path), ['/api/permission/12/allow', '/api/permission/12/deny', '/api/permission/12/tui']);
  assert.equal(plain(w.get('__calls')).filter((c) => c.method === 'poll').length, 3, 'the board repolls after a decision');
  p.update(paneRow({ state: 'working' }), { pending_permissions: [] });
  assert.equal(line.classList.contains('hidden'), true, 'answered: the line goes');
});

test('termPane permission line: the buttons are off while a decision is in flight, a refusal toasts, and a custom decide() replaces the POST', async () => {
  const { w, kit } = paneWorld();
  w.run(`api = (method, path) => { __calls.push({ method, path }); return new Promise((res, rej) => { globalThis.__settle = { res, rej }; }); };`);
  const p = kit.termPane(SESS);
  p.update(paneRow(), { pending_permissions: [{ id: 5, tmux_name: SESS, summary: 'Bash: ls' }] });
  const [allow, deny] = p.root.querySelectorAll('.tp-perm button');
  allow.click();
  await settle();
  assert.equal(allow.disabled && deny.disabled, true, 'busy: both are off');
  deny.click();
  await settle();
  assert.equal(plain(w.get('__calls')).filter((c) => c.method === 'POST').length, 1, 'a second tap while one is in flight sends nothing');
  w.run('__settle.rej(new Error("already decided: allow"))');
  await settle();
  assert.deepEqual(plain(w.get('__toasts')), [{ text: 'already decided: allow', kind: 'bad' }]);
  assert.equal(allow.disabled, false, 'free again');
  const seen = [];
  const q = kit.termPane(SESS, { decide: (id, d) => { seen.push([id, d]); return Promise.resolve(); } });
  q.update(paneRow(), { pending_permissions: [{ id: 9, tmux_name: SESS, summary: 'x' }] });
  q.root.querySelector('.tp-allow').click();
  await settle();
  assert.deepEqual(seen, [[9, 'allow']]);
  assert.equal(plain(w.get('__calls')).filter((c) => c.method === 'POST').length, 1, 'decide() replaced the POST');
});

test('termPane: the size chip of a grid pane reads the pane\'s own cols x rows against the window; full and tail panes show none', () => {
  const { kit, clock } = paneWorld();
  const p = kit.termPane(SESS, { mode: 'grid' });
  const f = ttydFrame({ clock });
  f.term.cols = 45; f.term.rows = 30;
  p.iframe.contentWindow = f.win;
  p.update(paneRow({ win: [46, 31] }), {});
  const chip = p.root.querySelector('.tp-size');
  assert.equal(chip.classList.contains('hidden'), false);
  assert.equal(chip.textContent, '45x30');
  p.update(paneRow({ win: [120, 40] }), {});
  assert.equal(chip.textContent, 'cropped');
  assert.equal(chip.classList.contains('cropped'), true);
  p.setMode('full');
  assert.equal(chip.classList.contains('hidden'), true, 'a full pane sizes the session: nothing to report');
  p.update(paneRow({ win: [120, 40] }), {});
  assert.equal(chip.classList.contains('hidden'), true);
});

test('termPane.setMode: full, grid and ro swap the src of the same iframe node (never moved); tail drops it and back makes a new one', () => {
  const { w, kit } = paneWorld();
  const calls = [];
  const p = kit.termPane(SESS, { mode: 'full', modes: ['grid', 'full', 'ro', 'tail'], onMode: (m) => calls.push(m) });
  const body = p.body;
  const frame = p.iframe;
  assert.equal(p.setMode('grid'), true);
  assert.equal(p.iframe, frame, 'the node is the same');
  assert.equal(srcOf(p), PANE_GRID);
  assert.equal(p.root.getAttribute('data-mode'), 'grid');
  assert.equal(p.mode, 'grid');
  p.setMode('ro');
  assert.equal(p.iframe, frame);
  assert.match(srcOf(p), /&arg=ro/);
  assert.equal(p.setMode('ro'), true, 'the same mode again changes nothing');
  assert.equal(p.setMode('nope'), false);
  assert.equal(p.mode, 'ro');
  assert.deepEqual(p.root.querySelectorAll('.tp-mode').map((b) => b.getAttribute('aria-pressed')), ['false', 'false', 'true', 'false']);
  const seenSrc = [];
  const realRemove = frame.remove.bind(frame);
  frame.remove = () => { seenSrc.push(frame.src); realRemove(); };
  p.setMode('tail');
  assert.equal(p.iframe, null);
  assert.deepEqual(seenSrc, ['about:blank'], 'leaving the iframe: about:blank, then it goes');
  assert.equal(body.querySelectorAll('iframe').length, 0);
  assert.ok(body.querySelector('pre.tail'));
  p.setMode('full');
  assert.ok(p.iframe && p.iframe !== frame, 'a new iframe');
  assert.equal(srcOf(p), `/tty/?arg=${SESS}`);
  assert.equal(body.querySelector('pre.tail'), null);
  assert.deepEqual(calls, ['grid', 'ro', 'tail', 'full']);
  assert.equal(w.document.querySelectorAll('iframe').length, 0, 'still detached');
});

test('termPane.reload: the same address again (a grid pane never reconnects by itself); in tail mode it subscribes again', () => {
  const { w, kit } = paneWorld();
  const p = kit.termPane(SESS, { mode: 'grid' });
  const sets = [];
  let cur = p.iframe.src;
  Object.defineProperty(p.iframe, 'src', { get: () => cur, set: (v) => { sets.push(v); cur = v; } });
  assert.equal(p.reload(), true);
  assert.deepEqual(sets, [PANE_GRID]);
  paneBtn(p, 'reconnect').click();
  assert.deepEqual(sets, [PANE_GRID, PANE_GRID], 'the header button does the same');
  const t = kit.termPane(SESS, { mode: 'tail' });
  assert.deepEqual(plain(w.get('__live.subscribed')), [SESS]);
  assert.equal(t.reload(), true);
  assert.deepEqual(plain(w.get('__live.subscribed')), [SESS, SESS]);
  assert.deepEqual(plain(w.get('__live.unsubscribed')), [SESS], 'the old subscription went first');
});

test('termPane tail mode: Live.subscribe(name, fn) for this one session only; the lines fill pre.tail; destroy unsubscribes', () => {
  const { w, kit } = paneWorld();
  const p = kit.termPane(SESS, { mode: 'tail' });
  assert.equal(p.iframe, null);
  assert.equal(p.root.querySelectorAll('iframe').length, 0);
  assert.deepEqual(plain(w.get('__live.subscribed')), [SESS], 'one name, never the whole board');
  const pre = p.root.querySelector('pre.tail');
  assert.equal(pre.textContent, 'waiting for output…');
  w.run(`__live.fns[${JSON.stringify(SESS)}](['a', 'b', 'c'])`);
  assert.equal(pre.textContent, 'a\nb\nc');
  w.run(`__live.fns[${JSON.stringify(SESS)}](Array.from({length: 30}, (_, i) => 'l' + i))`);
  assert.equal(pre.textContent.split('\n').length, 14, 'the last 14 lines');
  assert.ok(pre.textContent.endsWith('l29'));
  w.run(`__live.fns[${JSON.stringify(SESS)}]([])`);
  assert.equal(pre.textContent, '(no output yet)');
  p.destroy();
  assert.deepEqual(plain(w.get('__live.unsubscribed')), [SESS]);
  assert.equal(pre.parentNode, null);
});

test('termPane tail mode without Live (a page that does not load live.js): a note, no throw', () => {
  const { kit } = paneWorld({ live: false });
  const p = kit.termPane(SESS, { mode: 'tail' });
  assert.match(p.root.querySelector('pre.tail').textContent, /not available/);
  assert.doesNotThrow(() => p.destroy());
});

test('termPane.destroy: the bound handle goes first, the iframe is blanked and THEN removed, then the pane; observers, listeners and timers are released', () => {
  class FakeRO { constructor(fn) { this.fn = fn; this.observed = []; this.disconnected = 0; FakeRO.all.push(this); } observe(n) { this.observed.push(n); } disconnect() { this.disconnected += 1; } }
  FakeRO.all = [];
  const { w, kit, clock, dom } = paneWorld({ extra: { ResizeObserver: FakeRO } });
  const host = w.document.createElement('div');
  dom.body.append(host);
  const p = kit.termPane(SESS, { mode: 'grid', restoreFocus: null, holdFocus: true });
  host.append(p.root);
  const frame = p.iframe;
  assert.equal(FakeRO.all.length, 1);
  assert.deepEqual(FakeRO.all[0].observed, [p.body]);
  assert.equal((frame._on.load || []).length >= 2, true, 'bind() and the focus guard listen for load');
  frame.dispatchEvent({ type: 'load' });                                        // arms the three focus timers
  assert.ok(clock.pending > 0);
  const order = [];
  const realRemove = frame.remove.bind(frame);
  frame.remove = () => { order.push('iframe.remove src=' + frame.src); realRemove(); };
  const realRoot = p.root.remove.bind(p.root);
  p.root.remove = () => { order.push('root.remove iframe-gone=' + (frame.parentNode === null)); realRoot(); };
  const listeners = () => [...(w.window.listeners.resize || []), ...(w.window.listeners.orientationchange || []), ...(w.document.listeners.visibilitychange || [])].length;
  assert.equal(listeners(), 3);
  p.destroy();
  assert.deepEqual(order, ['iframe.remove src=about:blank', 'root.remove iframe-gone=true']);
  assert.equal((frame._on.load || []).length, 0, 'no load listener is left: the blank page starts no poll');
  assert.equal(FakeRO.all[0].disconnected, 1);
  assert.equal(listeners(), 0);
  assert.equal(clock.pending, 0, 'no timer survives');
  assert.equal(p.iframe, null);
  assert.equal(host.children.length, 0);
  assert.doesNotThrow(() => { p.destroy(); p.update(paneRow(), {}); p.reload(); p.fit(); p.focus(); p.setMode('grid'); });
  assert.equal(p.setMode('grid'), false);
});

test('termPane.fit: a burst of resizes (ResizeObserver, window) is one term.fit() after 250 ms of quiet; nothing after destroy', () => {
  class FakeRO { constructor(fn) { this.fn = fn; FakeRO.last = this; } observe() {} disconnect() {} }
  const { w, kit, clock } = paneWorld({ extra: { ResizeObserver: FakeRO } });
  const p = kit.termPane(SESS, { mode: 'full' });
  const f = ttydFrame({ clock });
  p.iframe.contentWindow = f.win;
  for (let i = 0; i < 6; i += 1) { FakeRO.last.fn(); clock.advance(50); }
  w.fire('resize');
  assert.equal(f.term.fits, 0);
  clock.advance(249);
  assert.equal(f.term.fits, 0);
  clock.advance(2);
  assert.equal(f.term.fits, 1, 'one fit for the whole burst');
  FakeRO.last.fn();
  p.destroy();
  clock.advance(500);
  assert.equal(f.term.fits, 1, 'a destroyed pane never fits');
});

test('termPane focus: a composer that had the keyboard gets it back when ttyd focuses its terminal on load, until the terminal is used', () => {
  const { w, kit, clock, dom } = paneWorld();
  const d = w.document;
  const box = d.createElement('textarea');
  dom.body.append(box);
  box.focus();
  const p = kit.termPane(SESS, { restoreFocus: box });
  dom.body.append(p.root);
  const f = ttydFrame({ clock });
  p.iframe.contentWindow = f.win;
  p.iframe.focus();                                                              // what ttyd does on load: its terminal takes the focus
  assert.equal(d.activeElement, p.iframe);
  p.iframe.dispatchEvent({ type: 'load' });
  clock.advance(1);
  assert.equal(d.activeElement, box, 'back in the composer');
  p.iframe.focus();                                                              // a later focus (ttyd opening its socket)
  clock.advance(300);
  assert.equal(d.activeElement, box);
  box.blur();
  const q = kit.termPane(SESS, { restoreFocus: box });
  dom.body.append(q.root);
  const g = ttydFrame({ clock });
  q.iframe.contentWindow = g.win;
  q.iframe.dispatchEvent({ type: 'load' });
  clock.advance(5);                                                              // bind() has found window.term: it listens for the person
  for (const l of g.doc.listeners.mousedown || []) l.fn({ type: 'mousedown' });  // the person clicked the terminal
  q.iframe.focus();
  clock.advance(2000);
  assert.equal(d.activeElement, q.iframe, 'once the terminal is used, focus is its own');
});

test('termPane focus: holdFocus without a node releases a focus the terminal was not asked for; without either, nothing is touched', () => {
  const { w, kit, clock, dom } = paneWorld();
  const d = w.document;
  const held = kit.termPane(SESS, { holdFocus: true });
  dom.body.append(held.root);
  held.iframe.focus();
  held.iframe.dispatchEvent({ type: 'load' });
  clock.advance(1);
  assert.notEqual(d.activeElement, held.iframe, 'blurred');
  const free = kit.termPane(SESS);
  dom.body.append(free.root);
  free.iframe.focus();
  free.iframe.dispatchEvent({ type: 'load' });
  clock.advance(2000);
  assert.equal(d.activeElement, free.iframe, 'a pane that was asked to take focus keeps it');
});

test('termPane.focus: the xterm instance when there is one, else the iframe element', () => {
  const { w, kit, clock } = paneWorld();
  const p = kit.termPane(SESS);
  assert.equal(p.focus(), true);
  assert.equal(w.document.activeElement, p.iframe);
  const f = ttydFrame({ clock });
  let focused = 0;
  f.term.focus = () => { focused += 1; };
  p.iframe.contentWindow = f.win;
  assert.equal(p.focus(), true);
  assert.equal(focused, 1);
});

// ---------------------------------------------------------------- v0.5.9c quad v3: tileMenu, composer, tune
//
// core.js + components.js + termkit.js on minidom with fake timers. api() is a recorder (`__route(method, path, body)` answers or throws), toast() and poll() record.
// The tile menu is a popover on a mouse and components.js openSheet's bottom sheet on touch, so the same ctx is opened both ways.
import fs from 'node:fs';

const KIT = path.join(ROOT, 'app', 'static');

function uiWorld({ extra = {}, storage = {} } = {}) {
  const k = kitWorld({ dom: true, extra: { innerWidth: 1280, innerHeight: 800, ...extra } });
  const { w } = k;
  w.load('components.js');
  w.ctx.__calls = [];
  w.ctx.__toasts = [];
  w.run(`
    globalThis.__route = null;
    globalThis.__httpError = (status, message, body) => { const e = new Error(message); e.status = status; e.body = body === undefined ? null : body; return e; };
    api = async (method, path, body) => { __calls.push({ method, path, body }); return __route ? __route(method, path, body) : { ok: true }; };
    toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };
    globalThis.poll = async () => { __calls.push({ method: 'poll' }); };
    globalThis.renderBanner = () => {};
  `);
  for (const [key, value] of Object.entries(storage)) w.localStorage.setItem(key, value);
  return k;
}

const calls = (w) => plain(w.get('__calls')).filter((c) => c.method !== 'poll');
const toasts = (w) => plain(w.get('__toasts'));
const keyOn = (node, key, extra = {}) => node.dispatchEvent({ type: 'keydown', key, preventDefault() {}, stopPropagation() {}, ...extra });
const names = (root) => root.querySelectorAll('.tk-item').map((n) => n.querySelector('.tk-name').textContent);
const group = (root, key) => root.querySelector(`[data-group=${key}]`);
const modeCells = (root) => root.querySelectorAll('.tk-mode').map((n) => n.querySelector('.tk-name').textContent);
const labels = (root) => root.querySelectorAll('.tk-gl').map((n) => n.textContent);
const item = (root, id) => root.querySelector(`[data-id="${id}"]`);
const anchorAt = (w, rect = {}) => {
  const a = w.document.createElement('button');
  const r = { left: 100, top: 100, right: 130, bottom: 128, width: 30, height: 28, ...rect };
  a.getBoundingClientRect = () => r;
  w.document.body.append(a);
  return a;
};

const ALL_ACTIONS = ['setMode', 'zoom', 'fullscreenTile', 'popout', 'openTerm', 'dock', 'reload', 'keysHere', 'allow', 'deny', 'tui', 'composer', 'tune', 'compact', 'context', 'usage', 'rename', 'close', 'kill'];
/** a tile menu ctx whose actions record their name (and argument) in `log`; `without` leaves actions out (null). */
function menuCtx(over = {}, without = []) {
  const log = [];
  const actions = {};
  for (const name of ALL_ACTIONS) actions[name] = without.includes(name) ? null : (...a) => { log.push(a.length ? [name, ...a] : name); };
  return { log, ctx: { tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude', mode: 'grid', modes: ['grid', 'full', 'ro', 'tail'], touch: false, actions,
    perm: { pending: false, summary: '' }, atPrompt: true, why: 'available when the session is at its prompt', ...over } };
}

test('v0.5.9c: the namespace carries the three components and their pure helpers', () => {
  const { kit } = uiWorld();
  for (const name of ['tileMenu', 'composer', 'tune', 'tuneGate', 'tunePlan', 'tuneCurrent', 'tuneRegistry']) assert.equal(typeof kit[name], 'function', `TermKit.${name}`);
  assert.equal(kit.composer.name, 'makeComposer', 'the inner factory does not shadow components.js composer()');
});

// ---- tileMenu: the groups

test('tileMenu on a mouse: a popover with the labelled groups VIEW / INPUT / TUNE / SESSION, in that order, every action as a row', () => {
  const { w, kit } = uiWorld();
  const { ctx } = menuCtx();
  const m = kit.tileMenu(ctx);
  const a = anchorAt(w);
  m.open(a, false);
  const pop = m.root;
  assert.ok(pop.classList.contains('tk-pop') && pop.classList.contains('tk-pop-menu'), 'a popover, not a sheet');
  assert.equal(pop.parentNode, w.document.body);
  assert.equal(w.document.querySelector('#sheet').open, false, 'no sheet on a mouse');
  assert.deepEqual(labels(pop), ['VIEW', 'INPUT', 'TUNE', 'SESSION']);
  assert.deepEqual(modeCells(pop), ['Grid', 'Full', 'Read only', 'Tail'], 'the four modes are one segmented row');
  assert.deepEqual(names(group(pop, 'view')), ['Zoom', 'Fullscreen this tile', 'Pop out', 'Open in terminal', 'Add to dock', 'Reload']);
  assert.deepEqual(names(group(pop, 'input')), ['Send a prompt…', 'Keys here'], 'no permission pending: no Allow / Deny');
  assert.deepEqual(names(group(pop, 'tune')), ['Tune…', '/compact', '/context'], '/usage and Rename… live inside Tune…');
  assert.deepEqual(names(group(pop, 'session')), ['Close tile']);
  const kill = group(pop, 'session').querySelector('.tk-kill');
  assert.ok(kill, 'Kill session closes the SESSION group');
  assert.equal(group(pop, 'session').children[group(pop, 'session').children.length - 1], kill, 'Kill is the last row of the menu');
  assert.ok(kill.querySelector('button').classList.contains('bp5-intent-danger'), 'red-outlined (danger)');
  assert.equal(pop.querySelectorAll('[data-group]').length, 4);
  for (const g of pop.querySelectorAll('[data-group]')) assert.ok(g.getAttribute('aria-labelledby'), 'a labelled group');
});

test('tileMenu height at 1280x800: the item count and the window cap are pinned (13 rows + one mode row + Kill is about 546 px with a mouse, under 560; the popover never exceeds the window)', () => {
  const { w, kit } = uiWorld();
  const { ctx } = menuCtx({}, []);
  ctx.actions.dockComposer = () => {};
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  const rows = m.root.querySelectorAll('.tk-item');
  assert.equal(rows.length, 13, 'every row of the fullest quiet menu: 6 VIEW + 3 INPUT + 3 TUNE + Close tile');
  assert.equal(m.root.querySelectorAll('.tk-mode').length, 4, 'and the four modes in ONE row, not four rows');
  assert.equal(m.root.querySelectorAll('.tk-kill button').length, 1);
  // 15 stops of 28 px (13 rows, the mode row, Kill) + 4 labels of 18 + group and menu padding and rules = 546: under 560 with room on an 800 px window
  const stops = rows.length + 1 + 1;
  const css = fs.readFileSync(path.join(KIT, 'termkit.css'), 'utf8');
  assert.match(css, /\.tk-menu \{ --tk-h:28px;/, 'a row is 28 px with a mouse');
  assert.ok(stops * 28 + 4 * 18 + 8 + 4 * 6 + 3 + 13 < 560, 'about 546 px');
  assert.match(css, /\.tk-pop \{[^}]*max-height:calc\(100vh - 16px\)/, 'the popover is capped to the window and scrolls inside itself');
  assert.match(css, /\.tk-pop \{[^}]*overflow-y:auto/);
  assert.doesNotMatch(css, /^\.tk-cell[ :{.]/m, '.tk-cell is the tune panel\'s command button: the menu\'s mode cells are .tk-mode and share no unscoped rule with it');
  assert.match(css, /^\.tk-mode \{/m);
});

test('tileMenu: the mode is one segmented row with the current one tinted (aria-checked, .on), and a pick calls setMode(mode) and closes', () => {
  const { w, kit } = uiWorld();
  const { ctx, log } = menuCtx({ mode: 'ro' });
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  const row = m.root.querySelector('.tk-modes');
  assert.equal(row.getAttribute('role'), 'group');
  assert.ok(row.getAttribute('aria-label'));
  const radios = m.root.querySelectorAll('[role=menuitemradio]');
  assert.equal(radios.length, 4);
  assert.deepEqual(radios.map((n) => n.getAttribute('aria-checked')), ['false', 'false', 'true', 'false']);
  assert.deepEqual(radios.map((n) => n.classList.contains('on')), [false, false, true, false], 'the tint sits on the current mode only');
  assert.equal(m.root.querySelectorAll('.tk-modes .bp5-icon-tick').length, 0, 'no tick icon: the segment says it');
  assert.ok(radios.every((n) => n.parentNode === row), 'all four in the one row');
  radios[1].click();
  assert.deepEqual(log, [['setMode', 'full']]);
  assert.equal(m.isOpen, false);
  assert.equal(w.document.querySelectorAll('.tk-pop').length, 0);
});

test('tileMenu: an item whose action is null is left out (Add to dock under 1024 px), and so is an empty group', () => {
  const { w, kit } = uiWorld();
  const { ctx } = menuCtx({}, ['dock', 'popout', 'allow', 'deny', 'tui']);
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  assert.deepEqual(names(group(m.root, 'view')), ['Zoom', 'Fullscreen this tile', 'Open in terminal', 'Reload']);
  assert.equal(m.root.querySelectorAll('.tk-mode').length, 4);
  m.close();
  const bare = menuCtx({}, ALL_ACTIONS.filter((n) => n !== 'zoom' && n !== 'close'));
  const m2 = kit.tileMenu(bare.ctx);
  m2.open(anchorAt(w), false);
  assert.deepEqual(labels(m2.root), ['VIEW', 'SESSION'], 'no input, no tune, no kill: those groups are not drawn');
  assert.equal(m2.root.querySelectorAll('.tk-kill').length, 0);
  m2.close();
  const noMode = kit.tileMenu(menuCtx({}, ['setMode']).ctx);
  noMode.open(anchorAt(w), false);
  assert.equal(noMode.root.querySelectorAll('.tk-mode').length, 0, 'no setMode: no mode row');
});

test('tileMenu with a permission pending: Allow, Deny and In terminal join the INPUT group under a note naming the request', () => {
  const { w, kit } = uiWorld();
  const { ctx, log } = menuCtx({ perm: { pending: true, summary: 'Bash: npm test' }, session: paneRow({ state: 'waiting' }), atPrompt: false });
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  const input = group(m.root, 'input');
  assert.deepEqual(names(input), ['Send a prompt…', 'Keys here', 'Allow', 'Deny', 'In terminal']);
  assert.equal(input.querySelector('.tk-perm').textContent, 'Permission: Bash: npm test');
  item(m.root, 'deny').click();
  assert.deepEqual(log, ['deny'], 'the permission rows stay live while the session is not at its prompt');
});

test('tileMenu: the TUNE items are off with ctx.why while the session is not at its prompt; a tap says why and does nothing', () => {
  const { w, kit } = uiWorld();
  const { ctx, log } = menuCtx({ atPrompt: false, why: 'it is working: wait for the prompt', session: paneRow({ state: 'working' }) });
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  const tune = group(m.root, 'tune');
  const rows = tune.querySelectorAll('.tk-item');
  assert.equal(rows.length, 3);
  for (const r of rows) {
    assert.equal(r.getAttribute('aria-disabled'), 'true');
    assert.equal(r.getAttribute('title'), 'it is working: wait for the prompt');
  }
  assert.equal(tune.querySelector('.tk-note').textContent, 'it is working: wait for the prompt', 'the reason is said once, under the label');
  assert.equal(group(m.root, 'view').querySelectorAll('[aria-disabled=true]').length, 0, 'only the TUNE group is off');
  rows[1].click();
  assert.deepEqual(log, [], 'a disabled row does nothing');
  assert.equal(m.isOpen, true, 'and the menu stays');
  assert.deepEqual(toasts(w), [{ text: 'it is working: wait for the prompt', kind: 'info' }]);
});

test('tileMenu at the prompt: the TUNE rows are live and each calls its action (Tune…, /compact, /context; /usage and Rename… are not rows)', () => {
  const { w, kit } = uiWorld();
  const { ctx, log } = menuCtx();
  const m = kit.tileMenu(ctx);
  for (const id of ['tune', 'compact', 'context']) {
    m.open(anchorAt(w), false);
    assert.equal(item(m.root, id).getAttribute('aria-disabled'), null, `${id} is live`);
    assert.equal(item(m.root, 'usage'), null, '/usage is inside Tune…, not a row');
    assert.equal(item(m.root, 'rename'), null, 'and so is Rename…');
    item(m.root, id).click();
  }
  assert.deepEqual(log, ['tune', 'compact', 'context']);
});

test('tileMenu: the atPrompt flag is derived from the session row when ctx does not give one', () => {
  const { w, kit } = uiWorld();
  const { ctx } = menuCtx({ atPrompt: undefined, session: paneRow({ state: 'working' }) });
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  assert.equal(item(m.root, 'compact').getAttribute('aria-disabled'), 'true', 'working: off');
  m.close();
  ctx.session = paneRow({ state: 'idle' });
  m.open(anchorAt(w), false);
  assert.equal(item(m.root, 'compact').getAttribute('aria-disabled'), null, 'idle: on');
});

test('tileMenu per agent: a Claude session has Tune…, /compact and /context, a Codex session only Tune…, a shell no TUNE group at all', () => {
  const { w, kit } = uiWorld();
  const claude = menuCtx();
  const m1 = kit.tileMenu(claude.ctx);
  m1.open(anchorAt(w), false);
  assert.deepEqual(names(group(m1.root, 'tune')), ['Tune…', '/compact', '/context']);
  m1.close();
  const m2 = kit.tileMenu(menuCtx({ agent: 'codex', session: paneRow({ agent: 'codex', state: 'idle' }) }).ctx);
  m2.open(anchorAt(w), false);
  assert.deepEqual(names(group(m2.root, 'tune')), ['Tune…'], 'Codex has /model and /reasoning only: the Claude shortcuts are not offered');
  m2.close();
  const m3 = kit.tileMenu(menuCtx({ agent: 'shell', session: paneRow({ agent: 'shell', state: 'idle' }) }).ctx);
  m3.open(anchorAt(w), false);
  assert.deepEqual(labels(m3.root), ['VIEW', 'INPUT', 'SESSION']);
  m3.close();
  const schema = menuCtx({ agent: 'codex', schema: { slash: { model: { cmd: '/model' }, compact: { cmd: '/compact' } } } }).ctx;
  const m4 = kit.tileMenu(schema);
  m4.open(anchorAt(w), false);
  assert.deepEqual(names(group(m4.root, 'tune')), ['Tune…', '/compact'], 'an agent whose registry lists /compact gets the row');
});

test('tileMenu: the optional rows (Show composer and Keys here as ticks, Back to the grid when zoomed)', () => {
  const { w, kit } = uiWorld();
  const { ctx, log } = menuCtx({ zoomed: true, composerDocked: true, keysTarget: true });
  ctx.actions.dockComposer = () => log.push('dockComposer');
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  assert.equal(names(group(m.root, 'view'))[0], 'Back to the grid');
  const docked = item(m.root, 'dockcomposer');
  assert.equal(docked.getAttribute('role'), 'menuitemcheckbox');
  assert.equal(docked.getAttribute('aria-checked'), 'true');
  assert.equal(docked.querySelectorAll('.bp5-icon-tick').length, 1);
  assert.equal(item(m.root, 'keys').getAttribute('aria-checked'), 'true');
  docked.click();
  assert.deepEqual(log, ['dockComposer']);
});

test('tileMenu Kill session: red-outlined and last; the first tap arms it (Confirm Kill session + Cancel), the second kills once, Cancel disarms', async () => {
  const { w, kit } = uiWorld();
  const { ctx, log } = menuCtx();
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  let kill = m.root.querySelector('.tk-kill');
  assert.deepEqual(kill.querySelectorAll('button').map((b) => b.textContent), ['Kill session']);
  kill.querySelector('button').click();
  assert.deepEqual(log, [], 'one tap kills nothing');
  assert.equal(m.isOpen, true);
  kill = m.root.querySelector('.tk-kill');
  assert.deepEqual(kill.querySelectorAll('button').map((b) => b.textContent), ['Confirm Kill session', 'Cancel'], 'the popover itself shows the arm: confirmButton repaints the page, not this');
  kill.querySelectorAll('button')[1].click();
  assert.deepEqual(m.root.querySelector('.tk-kill').querySelectorAll('button').map((b) => b.textContent), ['Kill session'], 'Cancel: back to rest');
  assert.equal(w.get('ui.confirm'), null);
  m.root.querySelector('.tk-kill button').click();
  m.root.querySelector('.tk-kill button').click();           // Confirm Kill session
  await settle();
  assert.deepEqual(log, ['kill'], 'the second tap kills once');
  assert.equal(m.isOpen, false, 'and the menu goes');
});

test('tileMenu: closing the menu while Kill is armed disarms it (the next open starts at rest)', () => {
  const { w, kit } = uiWorld();
  const { ctx } = menuCtx();
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  m.root.querySelector('.tk-kill button').click();
  assert.equal(w.get('ui.confirm'), 'tile-kill:' + SESS);
  m.close();
  assert.equal(w.get('ui.confirm'), null);
  m.open(anchorAt(w), false);
  assert.deepEqual(m.root.querySelector('.tk-kill').querySelectorAll('button').map((b) => b.textContent), ['Kill session']);
});

// ---- tileMenu: mouse popover vs touch sheet, focus, keys, placement

test('tileMenu on touch: a bottom sheet (openSheet) with the same groups as labelled sections and 44 px rows, no popover', () => {
  const { w, kit } = uiWorld();
  const { ctx, log } = menuCtx({ touch: true, perm: { pending: true, summary: 'Bash: ls' } });
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  const dlg = w.document.querySelector('#sheet');
  assert.equal(dlg.open, true);
  assert.ok(dlg.classList.contains('bottom') && !dlg.classList.contains('right'), 'a bottom sheet, also on a wide screen');
  assert.equal(dlg.querySelector('.sheet-title').textContent, 'Tile menu · shop/api · s1');
  const menu = dlg.querySelector('.tk-menu');
  assert.ok(menu.classList.contains('tk-touch'), 'tk-touch: 44 px rows (termkit.css)');
  assert.deepEqual(labels(menu), ['VIEW', 'INPUT', 'TUNE', 'SESSION']);
  assert.equal(menu.querySelectorAll('[data-group]').length, 4);
  assert.equal(w.document.querySelectorAll('.tk-pop').length, 0);
  assert.equal(menu.querySelectorAll('.tk-mode').length, 4, 'the same segmented mode row');
  assert.equal(menu.querySelector('.tk-modenote').textContent, 'Grid: a small tile that does not size the session', 'touch has no tooltips: the current mode is described under the row');
  item(menu, 'reload').click();
  assert.deepEqual(log, ['reload']);
  assert.equal(dlg.open, false, 'a pick closes the sheet');
  assert.equal(m.isOpen, false);
});

test('tileMenu on touch: an action that opens another sheet swaps it in place; the menu\'s sheet is not closed over it', () => {
  const { w, kit } = uiWorld();
  const { ctx } = menuCtx({ touch: true });
  let sheet = null;
  ctx.actions.composer = () => { sheet = w.get('openSheet')({ title: 'Next', body: 'x', placement: 'bottom' }); };
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  item(m.root || w.document.querySelector('#sheet'), 'composer').click();
  const dlg = w.document.querySelector('#sheet');
  assert.equal(dlg.open, true, 'still open: it shows the next sheet');
  assert.equal(dlg.querySelector('.sheet-title').textContent, 'Next');
  assert.equal(m.isOpen, false, 'the menu let go');
  assert.ok(sheet);
});

test('tileMenu: opened by a pointer nothing is highlighted (the popover has the focus), by the keyboard the first item is focused', () => {
  const { w, kit } = uiWorld();
  const { ctx } = menuCtx();
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), false);
  assert.equal(w.document.activeElement, m.root, 'pointer: the popover, no row');
  m.close();
  m.open(anchorAt(w), true);
  assert.equal(w.document.activeElement, m.root.querySelector('.tk-mode.on'), 'keyboard: the first row of the menu, the current mode');
  m.close();
  const t = kit.tileMenu(menuCtx({ touch: true }).ctx);
  t.open(anchorAt(w), true);
  assert.equal(w.document.activeElement, w.document.querySelector('#sheet .tk-mode.on'), 'keyboard on a sheet: the first row too');
  t.close();
  t.open(anchorAt(w), false);
  assert.notEqual(w.document.activeElement.className.includes('tk-item'), true, 'a pointer on a sheet: no row focused');
});

test('tileMenu keys: arrows, Home and End move between rows (the mode row is one stop; Left and Right move inside it), Enter and Space pick, Esc closes and gives the focus back, Tab closes', () => {
  const { w, kit } = uiWorld();
  const { ctx, log } = menuCtx();
  const m = kit.tileMenu(ctx);
  const a = anchorAt(w);
  m.open(a, true);
  const cells = m.root.querySelectorAll('.tk-mode');
  const rows = m.root.querySelectorAll('.tk-item, .tk-kill button');
  const doc = w.document;
  const key = (k) => doc.dispatch('keydown', { key: k, preventDefault() {}, stopPropagation() {} });
  assert.equal(doc.activeElement, cells[0], 'the keyboard starts on the current mode (Grid)');
  key('ArrowRight');
  assert.equal(doc.activeElement, cells[1], 'Right: the next mode');
  key('ArrowLeft');
  key('ArrowLeft');
  assert.equal(doc.activeElement, cells[3], 'Left wraps inside the row');
  key('ArrowDown');
  assert.equal(doc.activeElement, rows[0], 'Down leaves the mode row for the next row (Zoom)');
  key('ArrowUp');
  assert.equal(doc.activeElement, cells[0], 'Up comes back to the current mode');
  key('End');
  assert.equal(doc.activeElement, rows[rows.length - 1], 'End: the Kill button');
  key('ArrowDown');
  assert.equal(doc.activeElement, cells[0], 'wraps to the mode row');
  key('ArrowUp');
  assert.equal(doc.activeElement, rows[rows.length - 1]);
  key('Home');
  assert.equal(doc.activeElement, cells[0]);
  keyOn(doc.activeElement, ' ');
  assert.deepEqual(log, [['setMode', 'grid']], 'Space on the mode picks it');
  m.open(a, true);
  key('Escape');
  assert.equal(m.isOpen, false);
  assert.equal(doc.activeElement, a, 'Esc: focus goes back to the ▾');
  m.open(a, true);
  key('Tab');
  assert.equal(m.isOpen, false, 'Tab closes it like components.menu');
});

test('tileMenu: a pointerdown outside closes it, one on the anchor does not (the anchor\'s own click toggles), a route change closes it, a resize keeps it under its anchor', () => {
  const { w, kit } = uiWorld();
  const m = kit.tileMenu(menuCtx().ctx);
  const a = anchorAt(w);
  m.open(a, false);
  w.document.dispatch('pointerdown', { target: m.root.querySelector('.tk-item') });
  assert.equal(m.isOpen, true, 'inside: stays');
  w.document.dispatch('pointerdown', { target: a });
  assert.equal(m.isOpen, true, 'the anchor: left to its click');
  m.open(a, false);
  assert.equal(m.isOpen, false, 'open() on an open menu is the second tap on the ▾: it closes');
  m.open(a, false);
  w.document.dispatch('pointerdown', { target: w.document.body });
  assert.equal(m.isOpen, false, 'outside: closes');
  m.open(a, false);
  w.fire('hashchange');
  assert.equal(m.isOpen, false);
  m.open(a, false);
  a.getBoundingClientRect = () => ({ left: 300, top: 40, right: 330, bottom: 68, width: 30, height: 28 });
  w.fire('resize');
  assert.equal(m.isOpen, true, 'a resize does not close it (a screenshot, a keyboard, a devtools pane fire them)');
  assert.equal(m.root.style.left, '300px', 'it is put under its anchor again');
  a.remove();
  w.fire('resize');
  assert.equal(m.isOpen, false, 'with the anchor gone it goes');
  const a2 = anchorAt(w);
  m.open(a2, false);
  m.close();
  assert.equal(w.document.querySelectorAll('.tk-pop').length, 0);
  assert.deepEqual(w.window.listeners.hashchange || [], [], 'every listener is released');
  assert.deepEqual(w.document.listeners.pointerdown || [], []);
  assert.deepEqual(w.document.listeners.keydown || [], []);
});

test('tileMenu popover placement: under the anchor, kept inside the window, above it when it does not fit below, scrolling in its own max-height', () => {
  const { w, kit, dom } = uiWorld();
  Object.defineProperty(dom.El.prototype, 'offsetHeight', { configurable: true, get() { return this.classList && this.classList.contains('tk-pop') ? 600 : 0; } });
  const m = kit.tileMenu(menuCtx().ctx);
  m.open(anchorAt(w, { left: 100, top: 40, bottom: 68, right: 130 }), false);
  assert.equal(m.root.style.top, '72px', 'under the anchor');
  assert.equal(m.root.style.left, '100px');
  assert.equal(m.root.style.maxHeight, '720px', 'the room under it');
  m.close();
  m.open(anchorAt(w, { left: 1250, top: 700, bottom: 728, right: 1280 }), false);
  assert.equal(m.root.style.left, '1012px', 'pulled back inside the window (1280 - 260 - 8)');
  assert.equal(m.root.style.top, '96px', 'flipped above the anchor');
  assert.equal(m.root.style.maxHeight, '688px');
  m.close();
  const noRect = kit.tileMenu(menuCtx().ctx);
  noRect.open(null, false);
  assert.ok(noRect.root, 'no anchor: still opens (the CSS default place)');
  noRect.close();
  delete dom.El.prototype.offsetHeight;
});

test('tileMenu in fullscreen: the popover goes into the fullscreen element (a fixed node outside it would be invisible)', () => {
  const { w, kit } = uiWorld();
  const root = w.document.createElement('div');
  w.document.body.append(root);
  const a = w.document.createElement('button');
  root.append(a);
  a.getBoundingClientRect = () => ({ left: 0, top: 0, right: 30, bottom: 28, width: 30, height: 28 });
  w.document.fullscreenElement = root;
  const m = kit.tileMenu(menuCtx().ctx);
  m.open(a, false);
  assert.equal(m.root.parentNode, root);
  m.close();
  w.document.fullscreenElement = null;
  m.open(a, false);
  assert.equal(m.root.parentNode, w.document.body);
  w.document.dispatch('fullscreenchange', {});
  assert.equal(m.isOpen, false, 'a fullscreen change closes it (the node would be in the wrong tree)');
});

test('popovers in page fullscreen: with <html> fullscreen (the quad does that) the menu, the composer and the tune panel go into <body>, where the theme is; only another fullscreen element takes them in', () => {
  const { w, kit } = uiWorld();
  const html = w.document.documentElement;
  const a = anchorAt(w);                                                       // in <body>, inside <html>
  const host = (open) => { const c = open(); const r = c.root; const parent = r && r.parentNode; c.close(); return parent; };
  const surfaces = {
    menu: () => { const m = kit.tileMenu(menuCtx().ctx); m.open(a, false); return m; },
    composer: () => { const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude', touch: false }); c.open(a); return c; },
    tune: () => { const t = kit.tune(tuneCtx()); t.open(a, false); return t; },
  };
  for (const [name, open] of Object.entries(surfaces)) {
    w.document.fullscreenElement = null;
    assert.equal(host(open), w.document.body, `${name}: no fullscreen: the body`);
    w.document.fullscreenElement = html;
    assert.equal(host(open), w.document.body, `${name}: <html> is the fullscreen element: still the body (outside it nothing is themed: Times, a filled white Send, a filled red Kill)`);
    w.document.webkitFullscreenElement = html;
    w.document.fullscreenElement = null;
    assert.equal(host(open), w.document.body, `${name}: the webkit prefix too`);
    w.document.webkitFullscreenElement = null;
  }
  const tile = w.document.createElement('div');
  const inner = w.document.createElement('button');
  inner.getBoundingClientRect = () => ({ left: 0, top: 0, right: 30, bottom: 28, width: 30, height: 28 });
  tile.append(inner);
  w.document.body.append(tile);
  w.document.fullscreenElement = tile;
  assert.equal(host(() => { const m = kit.tileMenu(menuCtx().ctx); m.open(inner, false); return m; }), tile, 'a fullscreen element of its own that holds the anchor takes the popover in');
  assert.equal(host(() => { const m = kit.tileMenu(menuCtx().ctx); m.open(a, false); return m; }), w.document.body, 'an anchor outside that element: the body');
  w.document.fullscreenElement = null;
});

test('tileMenu placement: below the anchor, above it when only there it fits whole, and slid up over the anchor (whole, no scroll) when neither side has the room', () => {
  const { w, kit, dom } = uiWorld();
  Object.defineProperty(dom.El.prototype, 'offsetHeight', { configurable: true, get() { return this.classList && this.classList.contains('tk-pop') ? 540 : 0; } });
  const m = kit.tileMenu(menuCtx().ctx);
  const vh = 800;                                                              // the harness window (the placement test above reads 720 px of room under a 72 px top)
  const at = (top) => anchorAt(w, { top, bottom: top + 28 });
  m.open(at(100), false);
  assert.equal(m.root.style.top, '132px', 'room below: under the anchor');
  m.close();
  m.open(at(vh - 200), false);
  assert.equal(m.root.style.top, String(vh - 200 - 4 - 540) + 'px', 'no room below, room above: flipped above');
  m.close();
  m.open(at(vh / 2 + 20), false);                                              // 540 px fits neither under (about 340) nor above (about 400): slide up
  assert.equal(m.root.style.top, String(vh - 540 - 8) + 'px', 'slid up so its bottom edge is 8 px above the window\'s');
  assert.equal(m.root.style.maxHeight, String(vh - 16) + 'px', 'the most the window can give');
  m.close();
  delete dom.El.prototype.offsetHeight;
});

test('tileMenu Kill session at rest is red-OUTLINED (class danger, the outlined Blueprint intent), never the filled one; only the armed Confirm carries `confirm` (the filled red)', () => {
  const { w, kit } = uiWorld();
  const m = kit.tileMenu(menuCtx().ctx);
  m.open(anchorAt(w), false);
  let btns = m.root.querySelectorAll('.tk-kill button');
  assert.equal(btns.length, 1);
  assert.ok(btns[0].classList.contains('bp5-intent-danger'), 'danger: the outline on a faint red tint (style.css)');
  assert.equal(btns[0].classList.contains('confirm'), false, 'not the filled variant at rest');
  assert.equal(btns[0].classList.contains('bp5-minimal'), false);
  btns[0].click();
  btns = m.root.querySelectorAll('.tk-kill button');
  assert.deepEqual(btns.map((b) => [b.textContent, b.classList.contains('confirm')]), [['Confirm Kill session', true], ['Cancel', false]], 'only the armed second tap is filled');
  const css = fs.readFileSync(path.join(KIT, 'termkit.css'), 'utf8');
  const killRules = css.split('\n').filter((l) => /\.tk-kill/.test(l)).join('\n');
  assert.doesNotMatch(killRules, /background|\bborder:|bad-solid|#fff/, 'termkit.css adds no fill or border to Kill: the outline is style.css danger');
});

test('tileMenu.update(patch): an open menu repaints in place with the new state (a permission arrives, the mode changes)', () => {
  const { w, kit } = uiWorld();
  const { ctx } = menuCtx();
  const m = kit.tileMenu(ctx);
  m.open(anchorAt(w), true);
  const pop = m.root;
  assert.equal(item(pop, 'allow'), null);
  m.update({ perm: { pending: true, summary: 'Bash: ls' }, mode: 'tail' });
  assert.equal(m.root, pop, 'the same popover');
  assert.ok(item(pop, 'allow'));
  assert.equal(pop.querySelector('[role=menuitemradio][aria-checked=true] .tk-name').textContent, 'Tail');
  assert.equal(pop.querySelector('.tk-mode.on').textContent, 'Tail');
  m.close();
  assert.doesNotThrow(() => m.update({ mode: 'full' }), 'a closed menu just takes the state');
});

test('tileMenu: ctx.touch decides, not the pointer; without a #sheet on the page a touch menu falls back to the popover', () => {
  const { w, kit } = uiWorld();
  w.document.querySelector('#sheet').remove();
  const m = kit.tileMenu(menuCtx({ touch: true }).ctx);
  m.open(anchorAt(w), false);
  assert.ok(m.root && m.root.classList.contains('tk-pop'), 'term.html has no #sheet: the popover');
  assert.ok(m.root.querySelector('.tk-menu').classList.contains('tk-touch'), 'with the touch sizes');
});

// ---- composer

test('composer on a mouse: a popover under the anchor with a two-row box, the quick replies, a bordered Send and the key hint; the box has the focus', () => {
  const { w, kit } = uiWorld();
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude', touch: false });
  const a = anchorAt(w);
  c.open(a);
  const pop = c.root;
  assert.ok(pop.classList.contains('tk-pop') && pop.classList.contains('tk-pop-composer'));
  const ta = pop.querySelector('textarea');
  assert.equal(ta.getAttribute('rows'), '2');
  assert.equal(w.document.activeElement, ta, 'a mouse: the box has the focus');
  assert.deepEqual(pop.querySelectorAll('.tk-chip').map((b) => b.textContent), ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'], 'the agent defaults when nothing was saved');
  const send = pop.querySelector('.tk-send');
  assert.equal(send.textContent, 'Send');
  assert.ok(!send.classList.contains('bp5-intent-primary') && !send.classList.contains('primary'), 'bordered, never a filled primary');
  assert.equal(pop.querySelector('.tk-hint').textContent, 'Enter sends · Shift+Enter adds a line');
  assert.equal(pop.querySelector('.tk-gl').textContent, 'SEND A PROMPT');
});

test('composer: the chips are the person\'s own list (ccboard:quick:<tmux>) when there is one', () => {
  const { w, kit } = uiWorld({ storage: { ['ccboard:quick:' + SESS]: JSON.stringify(['ship it', 'rebase']) } });
  const c = kit.composer({ tmux: SESS, session: paneRow(), agent: 'claude' });
  c.open(anchorAt(w));
  assert.deepEqual(c.root.querySelectorAll('.tk-chip').map((b) => b.textContent), ['ship it', 'rebase']);
});

test('composer: Enter sends POST /prompt {text, enter, queue:false} at the prompt, toasts "sent", reports onSent, clears the box and closes', async () => {
  const { w, kit } = uiWorld();
  const seen = [];
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude', onSent: (k) => seen.push(k) });
  c.open(anchorAt(w));
  const ta = c.root.querySelector('textarea');
  ta.value = 'fix the login bug';
  keyOn(ta, 'Enter');
  await settle();
  assert.deepEqual(calls(w), [{ method: 'POST', path: `/api/sessions/${SESS}/prompt`, body: { text: 'fix the login bug', enter: true, queue: false } }]);
  assert.deepEqual(toasts(w), [{ text: 'sent', kind: 'ok' }]);
  assert.deepEqual(seen, ['sent']);
  assert.equal(ta.value, '');
  assert.equal(c.isOpen, false, 'the composer closes after a send');
  assert.equal(plain(w.get('__calls')).filter((x) => x.method === 'poll').length, 1, 'the board repolls');
});

test('composer: a working Claude session queues (queue:true, toast "queued"); a working Codex session and a shell never do', async () => {
  const { w, kit } = uiWorld();
  w.run('__route = (m, p) => (p.endsWith("/prompt") ? { ok: true, pasted: true, queued: true } : { ok: true })');
  const seen = [];
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'working' }), agent: 'claude', onSent: (k) => seen.push(k) });
  c.open(anchorAt(w));
  assert.equal(c.root.querySelector('.tk-hint').textContent, 'Working now: your prompt goes in the queue');
  const ta = c.root.querySelector('textarea');
  ta.value = 'and then add tests';
  c.root.querySelector('.tk-send').click();
  await settle();
  assert.deepEqual(calls(w).map((x) => x.body), [{ text: 'and then add tests', enter: true, queue: true }]);
  assert.deepEqual(toasts(w), [{ text: 'queued', kind: 'ok' }]);
  assert.deepEqual(seen, ['queued']);

  w.run('__calls.length = 0; __toasts.length = 0; __route = (m, p) => { throw __httpError(409, "the session is working", { error: "working", message: "the session is working", retry: 5 }); }');
  const cx = kit.composer({ tmux: SESS, session: paneRow({ state: 'working', agent: 'codex' }), agent: 'codex' });
  cx.open(anchorAt(w));
  const tx = cx.root.querySelector('textarea');
  tx.value = 'next';
  cx.root.querySelector('.tk-send').click();
  await settle();
  assert.deepEqual(calls(w).map((x) => x.body), [{ text: 'next', enter: true, queue: false }], 'Codex never queues, and the refusal is not retried as a queue');
  assert.deepEqual(toasts(w), [{ text: 'the session is working · try again in 5 s', kind: 'warn' }]);
  assert.equal(cx.isOpen, true, 'a refusal keeps the composer open');
  assert.equal(tx.value, 'next', 'and the text');
  assert.equal(cx.root.querySelector('.tk-send').disabled, false, 'Send is free again');
});

test('composer: a shell has no agent row to paste into, so its text goes to /keys {text, enter}', async () => {
  const { w, kit } = uiWorld();
  const c = kit.composer({ tmux: SESS, session: paneRow({ agent: 'shell', state: 'idle' }), agent: 'shell' });
  c.open(anchorAt(w));
  const ta = c.root.querySelector('textarea');
  ta.value = 'ls -la';
  c.root.querySelector('.tk-send').click();
  await settle();
  assert.deepEqual(calls(w), [{ method: 'POST', path: `/api/sessions/${SESS}/keys`, body: { text: 'ls -la', enter: true } }]);
  assert.deepEqual(toasts(w), [{ text: 'sent', kind: 'ok' }]);
});

test('composer: a Claude session that started a turn since the last poll answers 409 "working" once: the text is queued instead, and an old server\'s 404 falls back to /keys', async () => {
  const { w, kit } = uiWorld();
  let n = 0;
  w.ctx.__n = () => ++n;
  w.run('__route = (m, p, b) => { if (b && b.queue === false && p.endsWith("/prompt")) throw __httpError(409, "working", { error: "working", message: "the session is working", retry: 5 }); return { ok: true, queued: true }; }');
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude' });
  c.open(anchorAt(w));
  c.root.querySelector('textarea').value = 'go on';
  c.root.querySelector('.tk-send').click();
  await settle();
  assert.deepEqual(calls(w).map((x) => x.body.queue), [false, true], 'asked at the prompt, then queued');
  assert.deepEqual(toasts(w), [{ text: 'queued', kind: 'ok' }]);

  w.run('__calls.length = 0; __toasts.length = 0; __route = (m, p) => { if (p.endsWith("/prompt")) throw __httpError(404, "Not Found"); return { ok: true }; }');
  const old = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude' });
  old.open(anchorAt(w));
  old.root.querySelector('textarea').value = 'hello';
  old.root.querySelector('.tk-send').click();
  await settle();
  assert.deepEqual(calls(w).map((x) => x.path.split('/').pop()), ['prompt', 'keys']);
  assert.deepEqual(toasts(w), [{ text: 'sent', kind: 'ok' }]);
});

test('composer: Shift+Enter adds a line and sends nothing, an empty box sends nothing, Esc closes and gives the focus back, a chip sends at once', async () => {
  const { w, kit } = uiWorld();
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude' });
  const a = anchorAt(w);
  c.open(a);
  const ta = c.root.querySelector('textarea');
  ta.value = 'line one';
  keyOn(ta, 'Enter', { shiftKey: true });
  assert.equal(ta.value, 'line one\n', 'Shift+Enter inserts a newline');
  assert.deepEqual(calls(w), []);
  ta.value = '   ';
  keyOn(ta, 'Enter');
  await settle();
  assert.deepEqual(calls(w), [], 'a blank prompt is not sent');
  w.document.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
  assert.equal(c.isOpen, false);
  assert.equal(w.document.activeElement, a);
  assert.equal(ta.value, '   ', 'the draft survives a close');
  c.open(a);
  c.root.querySelectorAll('.tk-chip')[1].click();                              // merge
  await settle();
  assert.deepEqual(calls(w).map((x) => x.body.text), ['merge']);
  assert.equal(c.isOpen, false, 'a chip send closes it too');
});

test('composer: a second tap while a send is in flight sends nothing, and the buttons are off meanwhile', async () => {
  const { w, kit } = uiWorld();
  w.run('__route = () => new Promise((res) => { globalThis.__done = () => res({ ok: true }); })');
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude' });
  c.open(anchorAt(w));
  c.root.querySelector('textarea').value = 'once';
  const send = c.root.querySelector('.tk-send');
  send.click();
  await settle();
  assert.equal(send.disabled, true);
  assert.equal(c.root.querySelectorAll('.tk-chip').filter((b) => !b.disabled).length, 0, 'chips are off too');
  send.click();
  keyOn(c.root.querySelector('textarea'), 'Enter');
  await settle();
  assert.equal(calls(w).length, 1);
  w.run('__done()');
  await settle();
  assert.equal(c.isOpen, false);
});

test('composer on touch: a bottom sheet with a newline key, no keyboard hint, and the box is not focused (the soft keyboard stays down)', () => {
  const { w, kit } = uiWorld();
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude', touch: true });
  c.open(anchorAt(w));
  const dlg = w.document.querySelector('#sheet');
  assert.equal(dlg.open, true);
  assert.equal(dlg.querySelector('.sheet-title').textContent, 'Send a prompt · shop/api · s1');
  const box = dlg.querySelector('.tk-composer');
  assert.ok(box.classList.contains('tk-touch'));
  assert.equal(box.querySelector('.tk-hint').textContent, '');
  assert.ok(box.querySelector('.nl'), 'touch keyboards have no Shift+Enter: a newline button');
  assert.notEqual(w.document.activeElement, box.querySelector('textarea'));
  assert.equal(w.document.querySelectorAll('.tk-pop').length, 0);
  c.close();
  assert.equal(dlg.open, false);
});

test('composer.el: the docked one-line box stays after a send, and it is the same send (queue rule, toast, onSent)', async () => {
  const { w, kit } = uiWorld();
  w.run('__route = () => ({ ok: true, queued: true })');
  const seen = [];
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'working' }), agent: 'claude', onSent: (k) => seen.push(k) });
  const docked = c.el;
  assert.ok(docked.classList.contains('tk-docked'));
  const ta = docked.querySelector('textarea');
  assert.equal(ta.getAttribute('rows'), '1');
  assert.ok(docked.querySelector('.tk-send') && !docked.querySelector('.tk-chip'), 'a box and a Send, nothing else');
  w.document.body.append(docked);
  ta.value = 'queue me';
  keyOn(ta, 'Enter');
  await settle();
  assert.deepEqual(calls(w).map((x) => x.body), [{ text: 'queue me', enter: true, queue: true }]);
  assert.deepEqual(seen, ['queued']);
  assert.equal(ta.value, '');
  assert.equal(docked.parentNode, w.document.body, 'it stays where it is mounted');
  c.update({ session: paneRow({ state: 'idle' }) });
  ta.value = 'now';
  keyOn(ta, 'Enter');
  await settle();
  assert.equal(calls(w)[1].body.queue, false, 'update(): the next send sees the new state');
});

test('composer.el is ONE row: .tk-docked is a flex row (no column), a one-row box that does not auto-grow, no chips, a small … button that opens the roomy box, a bordered Send', async () => {
  const { w, kit } = uiWorld();
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude', touch: false });
  const docked = c.el;
  assert.ok(docked.classList.contains('tk-composer') && docked.classList.contains('tk-docked'));
  assert.deepEqual(docked.children.map((n) => (n.tagName || '').toLowerCase()), ['textarea', 'button', 'button'], 'the box, the … button, Send: one row of three');
  assert.equal(docked.querySelectorAll('.tk-chip, .tk-chips, .tk-gl, .tk-hint').length, 0, 'no chips, no label, no hint on the line');
  const ta = docked.querySelector('textarea');
  assert.equal(ta.getAttribute('rows'), '1');
  assert.ok(ta.classList.contains('composer'), 'style.css\'s box');
  assert.equal(ta._maxRows, undefined, 'composerBind is not used: nothing grows it (composerGrow would)');
  ta.value = 'a\nb\nc';
  ta.dispatchEvent({ type: 'input' });
  assert.equal(ta.style.height || '', '', 'typing sets no height: the row stays one row');
  const more = docked.querySelector('.tk-more');
  const send = docked.querySelector('.tk-send');
  assert.equal(more.textContent, '…');
  assert.ok(more.getAttribute('aria-label'));
  assert.equal(send.textContent, 'Send');
  w.document.body.append(docked);
  assert.equal(c.isOpen, false);
  more.click();
  assert.equal(c.isOpen, true, 'the … button opens the popover variant (two rows, the quick replies)');
  assert.ok(c.root.classList.contains('tk-pop-composer'));
  assert.ok(c.root.querySelector('.tk-chips'), 'with the chips');
  c.close();
  const css = fs.readFileSync(path.join(KIT, 'termkit.css'), 'utf8');
  const rule = css.match(/\.tk-docked \{[^}]*\}/)[0];
  assert.match(rule, /flex-direction:\s*row/, 'a row: .tk-composer is a column and this must override it');
  assert.match(rule, /flex-wrap:\s*nowrap/);
  assert.match(rule, /align-items:\s*center/);
  const box = css.match(/\.tk-docked textarea\.composer\.tk-ta \{[^}]*\}/)[0];
  assert.match(box, /height:\s*var\(--tk-h\)/);
  assert.match(box, /max-height:\s*var\(--tk-h\)/, 'one row, never taller');
  assert.match(box, /resize:\s*none/);
  assert.ok(css.indexOf('.tk-docked {') > css.indexOf('.tk-composer {'), 'declared after .tk-composer, so its row wins');
  assert.match(css, /\.tk-docked \{ --tk-h:28px;/, '28 px with a mouse');
  assert.match(css, /@media \(pointer:coarse\) \{ \.tk-docked \{ --tk-h:44px;/, '44 px on touch');
  assert.match(css, /html\.force-coarse \.tk-docked \{ --tk-h:44px; \}/);
  assert.match(css, /\.bp5-dark \.bp5-button\.tk-send:not\(\[class\*=bp5-intent-\]\) \{[^}]*min-height:var\(--tk-h\)/, 'Send is a bordered button as tall as the row');
  assert.match(css, /\.bp5-dark \.bp5-button\.tk-more:not\(\[class\*=bp5-intent-\]\) \{[^}]*min-height:var\(--tk-h\)/);
});

// ---- tune

const TUNE_REG = {
  claude: { compact: { cmd: '/compact', read: false, arg: false }, context: { cmd: '/context', read: true, arg: false }, usage: { cmd: '/usage', read: true, arg: false }, cost: { cmd: '/cost', read: true, arg: false },
    status: { cmd: '/status', read: true, arg: false }, rename: { cmd: '/rename', read: false, arg: true }, model: { cmd: '/model', read: false, arg: true }, effort: { cmd: '/effort', read: false, arg: true }, fast: { cmd: '/fast', read: false, arg: false } },
  codex: { model: { cmd: '/model', read: false, arg: true }, reasoning: { cmd: '/reasoning', read: false, arg: true } },   // an older server: Codex's /model typed inline
};
// v0.5.21 (box checks V8 / V8-Codex): the registry rows of claude.py / codex.py today
const TUNE_REG_NOW = {
  claude: { ...TUNE_REG.claude, effort: { cmd: '/effort', read: false, arg: true, tune: 'effort', saves_default: true, verified: true },
    model: { cmd: '/model', read: false, arg: true, saves_default: true, verified: true }, fast: { cmd: '/fast', read: false, arg: true, choices: ['on', 'off'] } },
  codex: { model: { cmd: '/model', arg: false, read: false, drive: 'picker', tune: 'model', verified: true },
    reasoning: { cmd: '/model', arg: false, read: false, drive: 'picker', tune: 'reasoning', verified: false },
    permissions: { cmd: '/permissions', arg: false, read: false, drive: 'picker', tune: 'permissions', verified: false },
    status: { cmd: '/status', arg: false, read: true, dialog: false, verified: true } },
};
const claudeStats = (over = {}) => ({ model: 'Opus 5', model_id: 'claude-opus-5', effort: 'high', fast: false, context_pct: 42, session_name: 'login work', ...over });
const tuneCtx = (over = {}) => ({ tmux: SESS, session: paneRow({ state: 'idle', stats: claudeStats() }), agent: 'claude', stats: claudeStats(), touch: false, schema: null, ...over });
const segTexts = (root, kind) => root.querySelector(`.tk-seg[data-kind=${kind}]`).querySelectorAll('button').map((b) => b.textContent);
const pressed = (root, kind) => root.querySelector(`.tk-seg[data-kind=${kind}]`).querySelectorAll('button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.textContent);

test('tune for Claude: MODEL (opus fable sonnet haiku), EFFORT (low medium high xhigh max), OPTIONS (Fast, Ultracode) and an equal-cell COMMANDS grid, with the current values read from stats', () => {
  const { w, kit } = uiWorld();
  const t = kit.tune(tuneCtx());
  t.open(anchorAt(w), false);
  const pop = t.root;
  assert.ok(pop.classList.contains('tk-pop-tune'), 'a popover (320 px in termkit.css)');
  assert.deepEqual(pop.querySelectorAll('.tk-sec').map((s) => s.querySelector('.tk-gl').textContent), ['MODEL', 'EFFORT', 'OPTIONS', 'COMMANDS']);
  assert.deepEqual(segTexts(pop, 'model'), ['opus', 'fable', 'sonnet', 'haiku']);
  assert.deepEqual(segTexts(pop, 'effort'), ['low', 'medium', 'high', 'xhigh', 'max']);
  assert.deepEqual(pressed(pop, 'model'), ['opus'], '"Opus 5" is the opus option');
  assert.deepEqual(pressed(pop, 'effort'), ['high']);
  assert.deepEqual(pop.querySelectorAll('.tk-tog').map((b) => b.querySelector('.tk-tn').textContent), ['Fast', 'Ultracode']);
  assert.deepEqual(pop.querySelectorAll('.tk-tog').map((b) => b.querySelector('.tk-state').textContent), ['off', 'unknown'],
    'the state is said in words; Ultracode has no reading yet (no statusline field carries it), so neither on nor off');
  const notes = pop.querySelectorAll('.tk-note').map((n) => n.textContent);
  assert.ok(notes.includes('Also saves your default for new sessions'), 'Model is typed inline (/model <name>), which saves the default: said under MODEL');
  assert.ok(notes.includes('Ultracode sets xhigh and turns workflows on'));
  assert.deepEqual(pop.querySelectorAll('.tk-unverified').map((n) => n.parentNode.querySelector('.tk-gl').textContent), ['EFFORT'],
    'the /effort slider with `s` did not run end to end on the box: said, not hidden; Model, Fast and Ultracode did');
  assert.deepEqual(pop.querySelectorAll('.tk-cell').map((b) => b.textContent), ['/compact', '/context', '/usage', '/cost', '/status', '/rename…']);
  assert.ok(pop.querySelector('.tk-seg[data-kind=model] .tk-opt').classList.contains('hue-blue'), 'the model options wear the hues of the board');
  assert.ok(!pop.querySelector('.tk-gate') || pop.querySelector('.tk-gate').classList.contains('hidden'), 'at the prompt: no gate note');
  for (const b of pop.querySelectorAll('button')) assert.equal(b.disabled, false);
});

test('tune: the current values follow stats (a model name that contains the option, effort, Fast, Ultracode) and update(patch) repaints', () => {
  const { w, kit } = uiWorld();
  const t = kit.tune(tuneCtx({ stats: claudeStats({ model: 'Sonnet 4.7', model_id: 'claude-sonnet-4-7', effort: 'xhigh', fast: true }) }));
  t.open(anchorAt(w), false);
  assert.deepEqual(pressed(t.root, 'model'), ['sonnet']);
  assert.deepEqual(pressed(t.root, 'effort'), ['xhigh']);
  assert.equal(t.root.querySelector('.tk-tog[data-kind=fast]').getAttribute('aria-pressed'), 'true');
  assert.equal(t.root.querySelector('.tk-tog[data-kind=fast] .tk-state').textContent, 'on');
  t.update({ stats: claudeStats({ effort: 'xhigh', model: 'Haiku 4', model_id: '' }), session: paneRow({ state: 'idle', flags: { tuned: { ultracode: { value: 'on', at: 'x' } } } }) });
  assert.deepEqual(pressed(t.root, 'effort'), ['xhigh'], 'ultracode is the switch, not a segment: the level stays');
  assert.equal(t.root.querySelector('.tk-tog[data-kind=ultra]').getAttribute('aria-pressed'), 'true', 'what the board last read back from the pane');
  assert.equal(t.root.querySelector('.tk-tog[data-kind=ultra] .tk-state').textContent, 'on');
  assert.deepEqual(pressed(t.root, 'model'), ['haiku']);
  t.update({ stats: claudeStats({ fast: null }) });
  assert.equal(t.root.querySelector('.tk-tog[data-kind=fast] .tk-state').textContent, 'unknown', 'no fast reading: no guessed Off');
  assert.equal(t.root.querySelector('.tk-tog[data-kind=fast]').getAttribute('aria-pressed'), 'false');
});

test('tune: every control posts its own body: Effort and Ultracode through POST /tune (session only), Model, Fast and the cells through /command', async () => {
  const { w, kit } = uiWorld();
  const t = kit.tune(tuneCtx());
  t.open(anchorAt(w), false);
  const click = async (node) => { node.click(); await settle(); };
  const seg = (kind, text) => t.root.querySelector(`.tk-seg[data-kind=${kind}]`).querySelectorAll('button').find((b) => b.textContent === text);
  await click(seg('model', 'sonnet'));
  await click(seg('effort', 'max'));
  await click(t.root.querySelector('.tk-tog[data-kind=fast]'));
  await click(t.root.querySelector('.tk-tog[data-kind=ultra]'));
  const cell = (key) => t.root.querySelector(`.tk-cell[data-cmd=${key}]`);
  await click(cell('compact'));
  assert.deepEqual(calls(w).map((c) => [c.path.split('/').pop(), c.body]), [['command', { cmd: 'model', arg: 'sonnet' }], ['tune', { setting: 'effort', value: 'max' }],
    ['command', { cmd: 'fast', arg: 'on' }], ['tune', { setting: 'ultracode', value: 'on' }], ['command', { cmd: 'compact' }]],
    'effort never typed inline (that saves the default); bare /fast never (it opens a dialog that swallows keys)');
  assert.ok(calls(w).every((c) => c.method === 'POST' && c.path.startsWith(`/api/sessions/${SESS}/`)));
  t.update({ session: paneRow({ state: 'idle', flags: { tuned: { ultracode: { value: 'on' } } } }), stats: claudeStats({ fast: true }) });
  await click(t.root.querySelector('.tk-tog[data-kind=ultra]'));
  assert.deepEqual(calls(w).pop().body, { setting: 'ultracode', value: 'off' }, 'Ultracode off is /effort ultracode off, never /effort high');
  await click(t.root.querySelector('.tk-tog[data-kind=fast]'));
  assert.deepEqual(calls(w).pop().body, { cmd: 'fast', arg: 'off' });
  // an older server (its registry has no `tune`): the inline commands, with the documented ultracode spellings
  const old = kit.tune(tuneCtx({ schema: { slash: TUNE_REG.claude } }));
  old.open(anchorAt(w), false);
  old.root.querySelector('.tk-seg[data-kind=effort]').querySelectorAll('button').find((b) => b.textContent === 'low').click();
  await settle();
  assert.deepEqual(calls(w).pop().body, { cmd: 'effort', arg: 'low' });
  old.root.querySelector('.tk-tog[data-kind=ultra]').click();
  await settle();
  assert.deepEqual(calls(w).pop().body, { cmd: 'effort', arg: 'ultracode on' });
});

test('tune: a /tune answer settles the row at once: confirmed flashes ok, a contradiction toasts the server\'s words and keeps the old value', async () => {
  const { w, kit } = uiWorld();
  const answers = [];
  w.ctx.__answers = answers;
  w.run('__route = async (m, p, b) => (p.endsWith("/tune") ? __answers.shift() : { ok: true });');
  const schema = { models: ['gpt-6.1-sol', 'gpt-6-luna'], reasoning_by_model: { 'gpt-6.1-sol': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], 'gpt-6-luna': ['low', 'medium', 'high', 'xhigh', 'max'] }, slash: TUNE_REG_NOW.codex };
  const st = { model: 'gpt-6.1-sol', effort: 'low' };
  const t = kit.tune(tuneCtx({ agent: 'codex', schema, stats: st, session: paneRow({ agent: 'codex', state: 'idle', stats: st }) }));
  t.open(anchorAt(w), false);
  const luna = t.root.querySelector('.tk-seg[data-kind=model]').querySelectorAll('button').find((b) => b.textContent === 'gpt-6-luna');
  answers.push({ ok: true, confirmed: true, observed: 'gpt-6-luna medium', verified: true });
  luna.click();
  await settle();
  assert.deepEqual(pressed(t.root, 'model'), ['gpt-6-luna'], 'read back from the pane: shown before the rollout reports it');
  assert.ok(luna.classList.contains('ok') && !luna.classList.contains('pending'));
  t.update({ stats: { model: 'gpt-6-luna', effort: 'medium' } });
  assert.deepEqual(pressed(t.root, 'model'), ['gpt-6-luna']);
  const ask = t.root.querySelector('.tk-seg[data-kind=perms]').querySelectorAll('button')[0];
  answers.push({ ok: true, confirmed: false, observed: 'Approve for me', message: 'permissions not applied: the session shows Approve for me' });
  ask.click();
  await settle();
  assert.deepEqual(toasts(w).at(-1), { text: 'permissions not applied: the session shows Approve for me', kind: 'warn' });
  assert.equal(ask.getAttribute('aria-pressed'), 'false', 'not applied: not selected');
});

test('tune for Codex: MODEL, REASONING and PERMISSIONS through POST /tune (the TUI pickers, never `/model <slug>` typed inline), /status as a cell, no switches', async () => {
  const { w, kit } = uiWorld();
  const schema = { models: ['gpt-6.1-sol', 'gpt-6-luna', 'gpt-6-luna-mini'], efforts: ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
    reasoning_by_model: { 'gpt-6.1-sol': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], 'gpt-6-luna': ['low', 'medium', 'high', 'xhigh', 'max'], 'gpt-6-luna-mini': ['low', 'medium'] },
    slash: TUNE_REG_NOW.codex };
  const st = { model: 'gpt-6-luna-mini', effort: 'medium' };
  const t = kit.tune(tuneCtx({ agent: 'codex', schema, stats: st, session: paneRow({ agent: 'codex', state: 'idle', stats: st }) }));
  t.open(anchorAt(w), false);
  assert.deepEqual(labels(t.root), ['MODEL', 'REASONING', 'PERMISSIONS', 'COMMANDS']);
  assert.deepEqual(segTexts(t.root, 'model'), ['gpt-6.1-sol', 'gpt-6-luna', 'gpt-6-luna-mini']);
  assert.deepEqual(pressed(t.root, 'model'), ['gpt-6-luna-mini'], 'the longest name wins: gpt-6-luna is inside gpt-6-luna-mini');
  assert.deepEqual(segTexts(t.root, 'effort'), ['low', 'medium'], 'the levels of the current model');
  assert.deepEqual(pressed(t.root, 'effort'), ['medium']);
  assert.deepEqual(segTexts(t.root, 'perms'), ['Ask for approval', 'Approve for me'], 'no Full Access, no untrusted, no on-failure');
  assert.deepEqual(pressed(t.root, 'perms'), [], 'unknown until the board reads it back: nothing selected on a guess');
  assert.equal(t.root.querySelectorAll('.tk-tog').length, 0, 'no Fast (bare /fast toggles and writes config.toml)');
  assert.deepEqual(t.root.querySelectorAll('.tk-cell').map((b) => b.textContent), ['/status']);
  assert.equal(t.root.querySelectorAll('.tk-unverified').length, 2, 'reasoning and permissions: the key path was not run end to end on the box');
  t.root.querySelector('.tk-seg[data-kind=effort] button').click();
  await settle();
  t.root.querySelector('.tk-seg[data-kind=model] button').click();
  await settle();
  t.root.querySelector('.tk-seg[data-kind=perms] button').click();
  await settle();
  assert.deepEqual(calls(w).map((c) => [c.path.split('/').pop(), c.body]), [['tune', { setting: 'reasoning', value: 'low' }], ['tune', { setting: 'model', value: 'gpt-6.1-sol' }],
    ['tune', { setting: 'permissions', value: 'ask' }]]);
  const sol = kit.tune(tuneCtx({ agent: 'codex', schema, stats: { model: 'gpt-6.1-sol' } }));
  sol.open(anchorAt(w), false);
  assert.deepEqual(segTexts(sol.root, 'effort'), ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'], 'ultra where the model lists it');
  const luna = kit.tune(tuneCtx({ agent: 'codex', schema, stats: { model: 'gpt-6-luna' } }));
  luna.open(anchorAt(w), false);
  assert.ok(!segTexts(luna.root, 'effort').includes('ultra'), 'never where it does not');
  const bare = kit.tune(tuneCtx({ agent: 'codex', schema: null, stats: {} }));
  bare.open(anchorAt(w), false);
  assert.ok(!bare.root.textContent.includes('There is nothing to tune'), 'no schema: a Codex tile still has something to tune');
  assert.deepEqual(labels(bare.root), ['MODEL', 'REASONING', 'PERMISSIONS', 'COMMANDS']);
  assert.deepEqual(segTexts(bare.root, 'model'), ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna'], 'the built-in list (codex.py FALLBACK_MODELS)');
  assert.deepEqual(segTexts(bare.root, 'effort'), ['low', 'medium', 'high', 'xhigh', 'max']);
  // an older server's registry types /model <slug> inline, which Codex sends to the model as a prompt: no model or reasoning row then
  const old = kit.tunePlan('codex', { models: ['gpt-6-sol'], slash: TUNE_REG.codex }, {});
  assert.deepEqual([old.model, old.effort], [null, null]);
});

test('the built-in Codex model list is codex.py FALLBACK_MODELS (the copies must not drift): same slugs, same order; ultra only on gpt-6.1-sol', () => {
  const { kit } = uiWorld();
  const py = fs.readFileSync(path.join(ROOT, 'app', 'agents', 'codex.py'), 'utf8');
  const block = py.slice(py.indexOf('FALLBACK_MODELS = ['), py.indexOf(']\n\n', py.indexOf('FALLBACK_MODELS = [')));
  const slugs = [...block.matchAll(/"slug": "([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual(slugs, ['gpt-6.1-sol', 'gpt-6-astra', 'gpt-6-sol', 'gpt-6-luna']);
  assert.deepEqual(plain(kit.tunePlan('codex', null, {}).model.options.map((o) => o.value)), slugs);
  assert.deepEqual(plain(kit.tunePlan('codex', null, { model: 'gpt-6.1-sol' }).effort.options.map((o) => o.value)), ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']);
  assert.deepEqual(plain(kit.tunePlan('codex', null, { model: 'gpt-6-luna' }).effort.options.map((o) => o.value)), ['low', 'medium', 'high', 'xhigh', 'max']);
});

test('tune for Codex with a state row\'s agent entry (status_summary: installed, version, loggedIn, hooks; no models, no registry): the built-in lists stand in, never "nothing to tune"', () => {
  const { w, kit } = uiWorld();
  const statusSummary = { installed: true, version: '0.160.1', loggedIn: true, authMethod: 'chatgpt', glyph: 'codex', hooks: { trust: 'review' } };
  const t = kit.tune(tuneCtx({ agent: 'codex', schema: statusSummary, stats: { model: 'gpt-6-sol', effort: 'high' }, session: paneRow({ agent: 'codex', state: 'idle', stats: { model: 'gpt-6-sol', effort: 'high' } }) }));
  t.open(anchorAt(w), false);
  assert.ok(!t.root.textContent.includes('There is nothing to tune'));
  assert.deepEqual(labels(t.root), ['MODEL', 'REASONING', 'PERMISSIONS', 'COMMANDS']);
  assert.equal(segTexts(t.root, 'model').length, 4);
  assert.deepEqual(pressed(t.root, 'model'), ['gpt-6-sol']);
  assert.deepEqual(pressed(t.root, 'effort'), ['high']);
  assert.equal(t.root.querySelectorAll('.tk-tog').length, 0, 'no Claude switches');
  const plan = kit.tunePlan('codex', statusSummary, {});
  assert.equal(plan.model.cmd, 'model');
  assert.equal(plan.effort.cmd, 'reasoning');
  assert.deepEqual([plan.model.via, plan.effort.via, plan.perms.via], ['tune', 'tune', 'tune']);
  const fromSchema = kit.tunePlan('codex', { models: ['gpt-6-sol'], efforts: ['low'], slash: TUNE_REG_NOW.codex }, {});
  assert.deepEqual([fromSchema.model.options.map((o) => o.value), fromSchema.effort.options.map((o) => o.value)], [['gpt-6-sol'], ['low']], 'a schema that lists them wins over the built-in lists');
});

test('tune: a 409 shows the server\'s words as a toast and the rows come back; other failures show their message', async () => {
  const { w, kit } = uiWorld();
  w.run('__route = () => { throw __httpError(409, "refused", { error: "permission_pending", message: "a permission request is waiting for an answer", retry: 10 }); }');
  const t = kit.tune(tuneCtx());
  t.open(anchorAt(w), false);
  const opus = t.root.querySelector('.tk-seg[data-kind=model] button');
  opus.click();
  await settle();
  assert.deepEqual(toasts(w), [{ text: 'a permission request is waiting for an answer · try again in 10 s', kind: 'warn' }]);
  assert.equal(opus.disabled, false, 'free again');
  w.run('__calls.length = 0; __toasts.length = 0; __route = () => { throw __httpError(500, "tmux is down"); }');
  t.root.querySelector('.tk-cell[data-cmd=compact]').click();
  await settle();
  assert.deepEqual(toasts(w), [{ text: 'tmux is down', kind: 'bad' }]);
});

test('tune while the session is not at its prompt: every row is off, a note says why, and a tap posts nothing', async () => {
  const { w, kit } = uiWorld();
  const t = kit.tune(tuneCtx({ session: paneRow({ state: 'working', stats: claudeStats() }) }));
  t.open(anchorAt(w), false);
  const gate = t.root.querySelector('.tk-gate');
  assert.equal(gate.classList.contains('hidden'), false);
  assert.equal(gate.textContent, 'Available when the session is at its prompt.');
  const all = t.root.querySelectorAll('.tk-opt, .tk-tog, .tk-cell');
  assert.ok(all.length >= 15);
  for (const b of all) { assert.equal(b.disabled, true); assert.equal(b.getAttribute('title'), 'available when the session is at its prompt'); }
  assert.equal(await t.run('compact', ''), false, 'run() says no too');
  assert.deepEqual(calls(w), []);
  t.update({ session: paneRow({ state: 'idle', stats: claudeStats() }) });
  assert.equal(gate.classList.contains('hidden'), true, 'at the prompt: the note goes');
  for (const b of all) assert.equal(b.disabled, false);
  t.update({ atPrompt: false });
  assert.equal(all[0].disabled, true, 'an explicit atPrompt wins over the row');
  const waiting = kit.tuneGate(paneRow({ state: 'waiting', flags: { wait_kind: 'idle' } }));
  assert.equal(waiting.enabled, true, 'waiting on the idle prompt counts as at the prompt');
  assert.equal(kit.tuneGate(paneRow({ state: 'idle', flags: { compacting: true } })).enabled, false);
  assert.equal(kit.tuneGate(paneRow({ state: 'idle', pending: [{ id: 1 }] })).enabled, false);
  assert.equal(kit.tuneGate(paneRow({ agent: 'shell' })).show, false);
});

test('tune: a read command shows what it printed, Close sends the agent one Escape, and the next command waits for it', async () => {
  const { w, kit, clock } = uiWorld();
  w.run('__route = (m, p, b) => (b && b.cmd === "usage" ? { ok: true, screen: "Plan usage\\n  5h: 41%\\n  week: 12%\\n\\n" } : b && b.cmd === "context" ? { ok: true, screen: "ctx 42%" } : { ok: true })');
  const t = kit.tune(tuneCtx());
  t.open(anchorAt(w), false);
  t.root.querySelector('.tk-cell[data-cmd=usage]').click();
  await settle();
  const readout = t.root.querySelector('.tk-readout');
  assert.equal(readout.classList.contains('hidden'), false);
  assert.equal(readout.querySelector('.tk-gl').textContent, '/USAGE');
  assert.equal(readout.querySelector('.tk-pre').textContent, 'Plan usage\n  5h: 41%\n  week: 12%');
  assert.deepEqual(calls(w).map((c) => c.path.split('/').pop()), ['command']);
  readout.querySelector('.tk-close').click();
  await settle();
  assert.deepEqual(calls(w).slice(1).map((c) => [c.path.split('/').pop(), c.body]), [['keys', { keys: ['Escape'] }]], 'closing the readout dismisses the dialog in the pane');
  assert.equal(readout.classList.contains('hidden'), true);
  // a second read command, then another command straight away: the Escape goes first, 150 ms before it
  t.root.querySelector('.tk-cell[data-cmd=context]').click();
  await settle();
  w.run('__calls.length = 0');
  t.root.querySelector('.tk-cell[data-cmd=compact]').click();
  await settle();
  assert.deepEqual(calls(w).map((c) => c.path.split('/').pop()), ['keys'], 'Escape first, the command waits the gap');
  clock.advance(200);
  await settle();
  assert.deepEqual(calls(w).map((c) => c.path.split('/').pop()), ['keys', 'command']);
});

test('tune: closing the panel with a read command\'s output still open sends the Escape; Esc inside it closes the readout first', async () => {
  const { w, kit } = uiWorld();
  w.run('__route = (m, p, b) => (b && b.cmd === "cost" ? { ok: true, screen: "$1.20" } : { ok: true })');
  const t = kit.tune(tuneCtx());
  t.open(anchorAt(w), false);
  t.root.querySelector('.tk-cell[data-cmd=cost]').click();
  await settle();
  w.document.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
  await settle();
  assert.equal(t.isOpen, true, 'the first Esc closes the readout');
  assert.equal(calls(w).filter((c) => c.path.endsWith('/keys')).length, 1);
  t.root.querySelector('.tk-cell[data-cmd=cost]').click();
  await settle();
  t.close();
  await settle();
  assert.equal(calls(w).filter((c) => c.path.endsWith('/keys')).length, 2, 'closing the panel dismisses the dialog too');
});

test('tune.run(): a command from the tile menu works without a panel; a read command opens the panel at the anchor to show its output', async () => {
  const { w, kit } = uiWorld();
  w.run('__route = (m, p, b) => (b && b.cmd === "context" ? { ok: true, screen: "ctx 42%" } : { ok: true })');
  const t = kit.tune(tuneCtx());
  assert.equal(await t.run('compact', ''), true);
  assert.equal(t.isOpen, false, 'a set command needs no panel');
  const a = anchorAt(w);
  assert.equal(await t.run('context', '', { anchor: a }), true);
  assert.equal(t.isOpen, true);
  assert.equal(t.root.querySelector('.tk-pre').textContent, 'ctx 42%');
});

test('tune Rename…: an inline form (prefilled with the session name), blank is refused in place, Enter posts /rename, Esc backs out of the form first', async () => {
  const { w, kit } = uiWorld();
  const t = kit.tune(tuneCtx());
  t.open(anchorAt(w), false);
  const form = t.root.querySelector('.tk-rename');
  assert.equal(form.classList.contains('hidden'), true);
  t.root.querySelector('.tk-cell[data-cmd=rename]').click();
  assert.equal(form.classList.contains('hidden'), false);
  const input = form.querySelector('input');
  assert.equal(input.value, 'login work');
  assert.equal(w.document.activeElement, input);
  input.value = '   ';
  form.dispatchEvent({ type: 'submit', preventDefault() {} });
  assert.equal(form.querySelector('.tk-err').textContent, 'Type a name first.');
  assert.deepEqual(calls(w), []);
  w.document.dispatch('keydown', { key: 'Escape', preventDefault() {}, stopPropagation() {} });
  assert.equal(form.classList.contains('hidden'), true, 'Esc closes the form');
  assert.equal(t.isOpen, true, 'not the panel');
  t.root.querySelector('.tk-cell[data-cmd=rename]').click();
  input.value = 'api cleanup';
  form.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.deepEqual(calls(w).map((c) => c.body), [{ cmd: 'rename', arg: 'api cleanup' }]);
  assert.equal(form.classList.contains('hidden'), true);
  t.close();
  t.rename(anchorAt(w));
  assert.equal(t.isOpen, true, 'rename(anchor) opens the panel with the form open');
  assert.equal(t.root.querySelector('.tk-rename').classList.contains('hidden'), false);
});

test('tune: a typed setting is pending until the statusline agrees (then it flashes ok); one that never lands says so after 20 s', async () => {
  const { w, kit, clock } = uiWorld();
  const t = kit.tune(tuneCtx());
  t.open(anchorAt(w), false);
  const sonnet = t.root.querySelector('.tk-seg[data-kind=model]').querySelectorAll('button')[2];
  sonnet.click();
  await settle();
  assert.equal(sonnet.classList.contains('pending'), true);
  t.update({ stats: claudeStats({ model: 'Sonnet 4.7', model_id: 'claude-sonnet-4-7' }) });
  assert.equal(sonnet.classList.contains('pending'), false);
  assert.equal(sonnet.classList.contains('ok'), true, 'landed');
  clock.advance(1600);
  assert.equal(sonnet.classList.contains('ok'), false);
  const fable = t.root.querySelector('.tk-seg[data-kind=model]').querySelectorAll('button')[1];
  fable.click();
  await settle();
  assert.equal(fable.classList.contains('pending'), true);
  clock.advance(20100);
  assert.equal(fable.classList.contains('pending'), false);
  assert.deepEqual(toasts(w), [{ text: '/model fable not confirmed by the session', kind: 'warn' }]);
});

test('tune.mount(targetEl): the same panel inside a page, no popover chrome; update() and destroy() work on it (the terminal page reuses this)', async () => {
  const { w, kit } = uiWorld();
  const host = w.document.createElement('div');
  w.document.body.append(host);
  const t = kit.tune(tuneCtx());
  const m = t.mount(host);
  assert.equal(m.root.parentNode, host);
  assert.ok(m.root.classList.contains('tk-tune'));
  assert.equal(w.document.querySelectorAll('.tk-pop').length, 0);
  assert.equal(t.isOpen, false);
  assert.deepEqual(pressed(m.root, 'effort'), ['high']);
  m.update({ stats: claudeStats({ effort: 'max' }) });
  assert.deepEqual(pressed(m.root, 'effort'), ['max']);
  m.root.querySelector('.tk-cell[data-cmd=compact]').click();
  await settle();
  assert.deepEqual(calls(w).map((c) => c.body), [{ cmd: 'compact' }]);
  m.destroy();
  assert.equal(host.children.length, 0);
  assert.equal(t.mount(null), null);
});

test('tune on touch: a bottom sheet with 44 px controls (tk-touch), titled with the session; closing it ends the sheet', () => {
  const { w, kit } = uiWorld();
  const t = kit.tune(tuneCtx({ touch: true }));
  t.open(anchorAt(w), false);
  const dlg = w.document.querySelector('#sheet');
  assert.equal(dlg.open, true);
  assert.equal(dlg.querySelector('.sheet-title').textContent, 'Tune · shop/api · s1');
  assert.ok(dlg.querySelector('.tk-tune').classList.contains('tk-touch'));
  assert.equal(w.document.querySelectorAll('.tk-pop').length, 0);
  t.open(anchorAt(w), false);
  assert.equal(t.isOpen, false, 'open() on an open panel closes it');
  assert.equal(dlg.open, false);
});

test('tune and the tile menu share one gate: TermKit.tuneGate decides both, and tunePlan(agent) is what the panel shows', () => {
  const { kit } = uiWorld();
  const p = plain(kit.tunePlan('claude', null, {}));
  assert.deepEqual(p.model.options.map((o) => o.value), ['opus', 'fable', 'sonnet', 'haiku']);
  assert.deepEqual(p.effort.options.map((o) => o.value), ['low', 'medium', 'high', 'xhigh', 'max']);
  assert.deepEqual([p.fast, p.ultra, p.cells.map((c) => c.key)], [true, true, ['compact', 'context', 'usage', 'cost', 'status', 'rename']]);
  assert.deepEqual(p.cells.filter((c) => c.read).map((c) => c.key), ['context', 'usage', 'cost', 'status']);
  const reg = plain(kit.tuneRegistry('claude', { slash: { effort: { cmd: '/effort' } } }));
  assert.deepEqual(Object.keys(reg), ['effort'], 'the agent\'s own registry wins');
  const only = plain(kit.tunePlan('claude', { slash: { effort: { cmd: '/effort' } } }, {}));
  assert.equal(only.model, null);
  assert.equal(only.fast, false);
  assert.deepEqual(plain(kit.tunePlan('shell', null, {})).cells, []);
});

test('#42: term.js keeps no lists of its own: its strip is drawn from TermKit.tunePlan and its requests come from TermKit.tuneRequest', () => {
  const src = fs.readFileSync(path.join(KIT, 'term.js'), 'utf8');
  for (const gone of ['const EFFORTS', 'const MODELS', 'EFFORT_ARG', 'FALLBACK_SLASH', 'slashRow']) assert.ok(!src.includes(gone), `term.js no longer defines ${gone}`);
  for (const used of ['TermKit.tunePlan', 'TermKit.tuneCurrent', 'TermKit.tuneRequest', 'TermKit.tuneRegistry', 'TermKit.tuneGate']) assert.ok(src.includes(used), `term.js uses ${used}`);
  const hues = /const MODEL_HUE = (\{[^}]*\})/.exec(src)[1];
  for (const [k, v] of Object.entries({ opus: 'hue-blue', fable: 'hue-violet', sonnet: 'hue-green', haiku: 'hue-slate' })) assert.ok(hues.includes(`${k}: '${v}'`), `${k} keeps ${v}`);
  const { kit } = uiWorld();
  assert.deepEqual(plain(kit.tuneRequest({ via: 'tune', setting: 'effort', cmd: 'effort' }, 'high')), { path: '/tune', body: { setting: 'effort', value: 'high' } });
  assert.deepEqual(plain(kit.tuneRequest({ via: 'command', cmd: 'model' }, 'opus')), { path: '/command', body: { cmd: 'model', arg: 'opus' } });
});

// ---- destroy(): the tile goes

test('destroy() on each component: the surface closes, nothing fires afterwards (no pending toast about a dead tile, no send), the docked box leaves the page', async () => {
  const { w, kit, clock } = uiWorld();
  const m = kit.tileMenu(menuCtx().ctx);
  m.open(anchorAt(w), false);
  m.destroy();
  assert.equal(m.isOpen, false);
  assert.equal(w.document.querySelectorAll('.tk-pop').length, 0);

  const t = kit.tune(tuneCtx());
  t.open(anchorAt(w), false);
  t.root.querySelector('.tk-seg[data-kind=model]').querySelectorAll('button')[1].click();     // /model fable: pending for 20 s
  await settle();
  const host = w.document.createElement('div');
  w.document.body.append(host);
  t.mount(host);
  t.destroy();
  assert.equal(t.isOpen, false);
  assert.equal(host.children.length, 0, 'a mounted panel leaves too');
  clock.advance(25000);
  assert.deepEqual(toasts(w), [], 'the pending setting does not toast about a dead tile');
  await assert.doesNotReject(() => t.run('compact', ''), 'a late run() does not throw');

  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude' });
  w.document.body.append(c.el);
  c.open(anchorAt(w));
  c.root.querySelector('textarea').value = 'late';
  c.destroy();
  assert.equal(c.isOpen, false);
  assert.equal(c.el.parentNode, null, 'the docked box is taken out of the page');
  w.run('__calls.length = 0');
  c.el.querySelector('.tk-send').click();
  await settle();
  assert.deepEqual(calls(w), [], 'a destroyed composer sends nothing');
});

test('tune.destroy() with a read command\'s output on screen sends the agent its one Escape (the tile closes, the session keeps running)', async () => {
  const { w, kit } = uiWorld();
  w.run('__route = (m, p, b) => (b && b.cmd === "usage" ? { ok: true, screen: "x" } : { ok: true })');
  const t = kit.tune(tuneCtx());
  t.open(anchorAt(w), false);
  t.root.querySelector('.tk-cell[data-cmd=usage]').click();
  await settle();
  w.run('__calls.length = 0');
  t.destroy();
  await settle();
  assert.deepEqual(calls(w).map((c) => [c.path.split('/').pop(), c.body]), [['keys', { keys: ['Escape'] }]]);
});

test('tileMenu: while it is open the page\'s single-letter shortcuts (F, Z) do not reach the page; Space and Enter still pick, modified keys pass', () => {
  const { w, kit } = uiWorld();
  const m = kit.tileMenu(menuCtx().ctx);
  m.open(anchorAt(w), true);
  const ev = (key, extra = {}) => { const e = { type: 'keydown', key, preventDefault() {}, stopPropagation() { e.stopped = true; }, ...extra }; w.document.dispatch('keydown', e); return e; };
  assert.equal(ev('f').stopped, true);
  assert.equal(ev('z').stopped, true);
  assert.equal(ev(' ').stopped, undefined, 'Space is the row\'s own key');
  assert.equal(ev('f', { ctrlKey: true, altKey: true }).stopped, undefined, 'Ctrl+Alt+F stays the page\'s');
  assert.equal(ev('ArrowDown').stopped, undefined);
});

// ---- termkit.css and its links

const termkitCss = fs.readFileSync(path.join(KIT, 'termkit.css'), 'utf8');
const cssRule = (sel) => { const m = new RegExp('(?:^|\\})\\s*' + sel.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}', 'm').exec(termkitCss); return m ? m[1] : null; };

test('termkit.css: the menu is at least 240 px wide, the tune popover 320, touch rows and controls 44 px, mouse 28; nothing is wider than the window', () => {
  assert.match(cssRule('.tk-pop-menu'), /min-width:\s*(2[4-9]\d|[3-9]\d\d)px/);
  assert.match(cssRule('.tk-pop-tune'), /width:\s*min\(320px,\s*calc\(100vw - 16px\)\)/);
  assert.match(cssRule('.tk-pop-composer'), /width:\s*min\(420px,\s*calc\(100vw - 16px\)\)/);
  assert.match(cssRule('.tk-pop'), /max-width:\s*calc\(100vw - 16px\)/);
  assert.match(cssRule('.tk-pop'), /overflow-y:\s*auto/);
  assert.match(cssRule('.tk-touch'), /--tk-h:\s*44px/);
  assert.match(cssRule('.tk-menu'), /--tk-h:\s*28px/);
  assert.match(cssRule('.tk-tune'), /--tk-h:\s*28px/);
  assert.match(cssRule('.tk-composer'), /--tk-h:\s*28px/);
  assert.match(cssRule('.tk-item'), /min-height:\s*var\(--tk-h\)/);
  assert.ok(termkitCss.indexOf('.tk-touch { --tk-h:44px; }') > termkitCss.indexOf('.tk-menu { --tk-h:28px'), '.tk-touch comes after .tk-menu: on a touch menu (both classes) the 44 px wins');
  assert.match(termkitCss, /\.tk-composer\.tk-touch textarea\.composer\.tk-ta \{[^}]*font-size:\s*16px/, 'a 16 px box on touch (iOS does not zoom)');
  assert.match(termkitCss, /\.tk-docked textarea\.composer\.tk-ta \{[^}]*height:\s*var\(--tk-h\)/);
  assert.match(termkitCss, /\.tk-gl \{[^}]*text-transform:\s*uppercase/, 'small-caps group labels');
  for (const sel of ['.tk-opt', '.tk-tog', '.tk-cell', '.tk-send', '.tk-chip']) assert.ok(termkitCss.includes(sel), `${sel} is styled`);
  assert.doesNotMatch(termkitCss.replace(/\/\*[\s\S]*?\*\//g, ''), /!important|@import|url\(|primary/, 'no filled primary, no import, no urls');
});

test('termkit.css is linked from both pages right after the stylesheets they already had, and nothing in termkit.js writes a style attribute', () => {
  const links = (file) => [...fs.readFileSync(path.join(KIT, file), 'utf8').matchAll(/<link rel="stylesheet" href="([^"]+)">/g)].map((m) => m[1]);
  assert.equal(links('index.html').at(-1), '/static/termkit.css');
  assert.equal(links('index.html').at(-2), '/static/charts.css');
  assert.equal(links('term.html').at(-1), '/static/termkit.css');
  assert.equal(links('term.html').at(-2), '/static/term.css');
  const js = fs.readFileSync(path.join(KIT, 'termkit.js'), 'utf8');
  assert.doesNotMatch(js.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, ''), /innerHTML|cssText|setAttribute\(\s*['"]style|insertAdjacentHTML/);
});

test('v0.5.9c: loading termkit.js still defines only (the three factories are not run at load)', () => {
  const k = kitWorld();
  assert.equal(k.w.document.body.children.length, 0);
});

// ---------------------------------------------------------------- v0.5.21 quad leftovers (#34)

test('tileMenu: the TUNE caption says where Usage and Rename went, only for an agent whose Tune… really has them, and the not-at-prompt reason still wins', () => {
  const { w, kit } = uiWorld();
  const claude = kit.tileMenu(menuCtx().ctx);
  claude.open(anchorAt(w), false);
  assert.equal(group(claude.root, 'tune').querySelector('.tk-note').textContent, 'Usage and rename are inside Tune…');
  assert.deepEqual(names(group(claude.root, 'tune')), ['Tune…', '/compact', '/context'], 'and they stay out of the rows');
  claude.close();
  const busy = kit.tileMenu(menuCtx({ atPrompt: false, why: 'it is working: wait for the prompt' }).ctx);
  busy.open(anchorAt(w), false);
  assert.equal(group(busy.root, 'tune').querySelector('.tk-note').textContent, 'it is working: wait for the prompt');
  busy.close();
  const codex = kit.tileMenu(menuCtx({ agent: 'codex' }).ctx);
  codex.open(anchorAt(w), false);
  const plan = kit.tunePlan('codex', undefined);
  const hasMoved = plan.cells.some((c) => c.key === 'usage' || c.key === 'rename');
  const note = group(codex.root, 'tune').querySelector('.tk-note');
  assert.equal(!!note, hasMoved, 'Codex claims the rows only when its Tune… has them');
});

test('composer.el: Shift+Enter in the docked one-line box neither sends nor inserts a hidden newline; Enter sends; Enter during IME composition is left alone; the title says so', async () => {
  const { w, kit } = uiWorld();
  w.run('__route = () => ({ ok: true })');
  const c = kit.composer({ tmux: SESS, session: paneRow({ state: 'idle' }), agent: 'claude' });
  const ta = c.el.querySelector('textarea');
  assert.match(ta.getAttribute('title'), /Enter sends.*no new line/);
  w.document.body.append(c.el);
  ta.value = 'hello';
  const rec = (extra) => { const r = { prevented: 0 }; keyOn(ta, 'Enter', { preventDefault() { r.prevented += 1; }, ...extra }); return r; };
  const shift = rec({ shiftKey: true });
  await settle();
  assert.equal(shift.prevented, 1, 'the newline is cancelled');
  assert.equal(calls(w).length, 0, 'and nothing is sent');
  assert.equal(ta.value, 'hello', 'the draft is kept');
  const ime = rec({ isComposing: true });
  await settle();
  assert.equal(ime.prevented, 0, 'the Enter that confirms an IME candidate is not touched');
  assert.equal(calls(w).length, 0);
  const plain1 = rec({});
  await settle();
  assert.equal(plain1.prevented, 1);
  assert.deepEqual(calls(w).map((x) => x.body), [{ text: 'hello', enter: true, queue: false }]);
});

test('fitName: the first measurement lifts .pend (a tile header keeps project/repo hidden until then) whether it keeps the text or drops it', () => {
  const { w, kit } = uiWorld();
  const mk = (width, scroll) => {
    const n = w.document.createElement('span');
    n.setAttribute('class', 'qt-where pend');
    n.getBoundingClientRect = () => ({ width });
    Object.defineProperty(n, 'scrollWidth', { get: () => scroll, configurable: true });
    return n;
  };
  const keep = mk(120, 120);
  assert.equal(kit.fitName(keep), false);
  assert.equal(keep.classList.contains('pend'), false);
  assert.equal(keep.classList.contains('off'), false);
  const sliver = mk(10, 120);
  assert.equal(kit.fitName(sliver), true);
  assert.equal(sliver.classList.contains('pend'), false);
  assert.equal(sliver.classList.contains('off'), true, 'a sliver goes, and never shows first');
});
