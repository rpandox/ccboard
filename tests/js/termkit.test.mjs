// Contract tests for app/static/termkit.js (namespace TermKit): ttyUrl, touchScroller, bind, fitSoon, viewportFit, keyBar and its
// hold-to-repeat. The vm harness has no layout and no real events, so the tests drive the kit through stub nodes, fake timers (the
// kit reads setTimeout / setInterval / clearTimeout / clearInterval as globals, so the stubs go in as harness extra globals) and a
// fake Date.now. Microtasks are real: `settle()` lets the repeater's in-flight promise chain run between timer steps.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { installDom } from './minidom.mjs';
import { makeWorld, plain } from './harness.mjs';

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
  const calls = { send: [], text: [], scroll: [] };
  const hold = { send: null, scroll: null };                     // set to a promise to keep a request "in flight"
  const host = world.w.document.createElement('div');
  const bar = world.kit.keyBar(host, {
    send: (keys) => { calls.send.push(Array.isArray(keys) ? Array.from(keys) : Array.from(keys.keys)); return hold.send; },
    sendText: (text, enter) => { calls.text.push([text, enter]); },
    scroll: (dir) => { calls.scroll.push(dir); return hold.scroll; },
    ...opts,
  });
  const buttons = () => [...host.querySelectorAll('button')];
  const byKey = (key) => buttons().find((b) => b.getAttribute('data-key') === key);
  return { ...world, calls, hold, host, bar, buttons, byKey };
}

const ev = (type, extra = {}) => { const calls = { prevented: 0 }; return { type, pointerType: 'touch', pointerId: 1, button: 0, cancelable: true, preventDefault() { calls.prevented += 1; }, calls, ...extra }; };
const press = (b) => { const e = ev('pointerdown'); b.dispatchEvent(e); return e; };
const release = (b) => b.dispatchEvent(ev('pointerup'));

test('keyBar: three rows with the contract keys in the contract order, mounted in the host', () => {
  const b = barWorld();
  assert.ok(b.host.contains(b.bar.root), 'the bar is appended to the host');
  assert.equal(b.bar.root.getAttribute('role'), 'toolbar');
  const rows = [...b.bar.root.querySelectorAll('.kb-row')].filter((r) => r.querySelector('button[data-key]') && !r.classList.contains('kb-ask-row'));
  const keysOf = (row) => [...row.querySelectorAll('button')].map((x) => x.getAttribute('data-key')).filter((k) => k);
  assert.deepEqual(keysOf(rows[0]), ['Escape', 'Tab', 'BTab', 'C-c', 'Enter']);
  assert.deepEqual(keysOf(rows[1]), ['Up', 'Down', 'Left', 'Right', 'BSpace']);
  assert.deepEqual(keysOf(rows[2]), ['up', 'down', 'top', 'bottom', 'C-o'], 'PgUp PgDn Top Bottom Ctrl+O (the first four are scroll buttons, not raw keys)');
  const labels = (row) => [...row.querySelectorAll('button')].map((x) => x.textContent);
  assert.deepEqual(labels(rows[1]), ['↑', '↓', '←', '→', '⌫']);
  assert.deepEqual(labels(rows[2]), ['PgUp', 'PgDn', 'Top', 'Bottom', 'Ctrl+O']);
  assert.ok(labels(rows[0]).slice(0, 3).join(' ').includes('Esc') && labels(rows[0]).some((t) => /Tab/.test(t)) && labels(rows[0]).includes('Enter'));
});

test('keyBar: every key is a type=button with tabindex -1 and an accessible name, so the soft keyboard and the tab order are left alone', () => {
  const b = barWorld();
  assert.ok(b.buttons().length >= 15);
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

test('keyBar: PgUp, PgDn, Top and Bottom call scroll(dir) and never send raw keys', () => {
  const b = barWorld();
  for (const dir of ['up', 'down', 'top', 'bottom']) { press(b.byKey(dir)); release(b.byKey(dir)); }
  assert.deepEqual(b.calls.scroll, ['up', 'down', 'top', 'bottom']);
  assert.deepEqual(b.calls.send, [], 'the server decides between tmux copy-mode and a forwarded PageUp');
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

test('keyBar: holding PgUp scrolls repeatedly but never queues scrolls behind a slow request', async () => {
  const b = barWorld();
  let done;
  b.hold.scroll = new Promise((resolve) => { done = resolve; });
  press(b.byKey('up'));
  await settle();
  b.clock.advance(400 + 150 * 20);
  await settle();
  assert.equal(b.calls.scroll.length, 1, 'while the first /scroll is in flight the ticks are dropped, not queued');
  b.hold.scroll = null;
  done();
  await settle();
  assert.equal(b.calls.scroll.length, 1, 'and nothing is replayed afterwards: the finger has no business scrolling later');
  b.clock.advance(300);
  await settle();
  assert.ok(b.calls.scroll.length >= 2, 'it repeats again once the request is back');
  release(b.byKey('up'));
  await settle();
  const n = b.calls.scroll.length;
  b.clock.advance(3000);
  await settle();
  assert.equal(b.calls.scroll.length, n, 'released: it stops');
  assert.ok(b.calls.scroll.every((d) => d === 'up'));
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
  assert.equal(rows.length, 3);
  const first = [...rows[0].querySelectorAll('button')].map((x) => x.getAttribute('data-key')).filter((k) => k);
  assert.deepEqual(first, ['Escape', 'Tab', 'BTab', 'C-c', 'Enter'], 'what compact mode keeps on screen');
  assert.ok(rows[0].querySelector('.kb-more'), 'the More toggle lives in that row');
  assert.ok(!rows[1].querySelector('.kb-more') && !rows[2].querySelector('.kb-more'));
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
