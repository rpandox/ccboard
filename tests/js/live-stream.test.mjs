// Contract tests for app/static/live.js (v0.5.5): Live.subscribe / Live.unsubscribe over ONE EventSource to /api/stream?names=<sorted>&lines=12,
// reopened (debounced 300 ms) when the subscriber set changes, closed the moment it empties, paused while the tab is hidden, filtered on the
// client, a no-op in demo mode, and definition-only at load.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';

/** A world with a recording EventSource and manual timers. fire(es, name, data) delivers an SSE event to an instance. */
function lWorld({ demo = false, debounce } = {}) {
  const opened = [];
  class FakeES {
    constructor(url) { this.url = url; this.closed = false; this.on = {}; opened.push(this); }
    addEventListener(type, fn) { (this.on[type] ||= []).push(fn); }
    close() { this.closed = true; }
  }
  const timers = new Map();
  let nextId = 1;
  const w = makeWorld({
    EventSource: FakeES,
    setTimeout: (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; },
    clearTimeout: (id) => { timers.delete(id); },
  });
  w.load('core.js');
  w.load('live.js');
  if (demo) w.run('demoFlag = true');
  if (debounce !== undefined) w.run(`Live.DEBOUNCE_MS = ${debounce}`);
  const runTimers = () => { for (const [id, t] of [...timers]) { timers.delete(id); t.fn(); } };
  const fire = (es, name, data) => { for (const fn of es.on[name] || []) fn({ data: typeof data === 'string' ? data : JSON.stringify(data) }); };
  const sub = (tmux) => {
    const got = [];
    w.ctx.__fn = (lines) => got.push(lines);
    w.ctx.__tmux = tmux;
    const off = w.run('Live.subscribe(__tmux, __fn)');
    return { got, fn: w.get('__fn'), off };
  };
  return { w, opened, timers, runTimers, fire, sub };
}

const A = 'shop--api--s1', B = 'shop--api--s2', C = 'blog--web--s3';

test('live.js defines only: no stream, listener or timer exists until the first subscribe', () => {
  const { w, opened, timers } = lWorld();
  assert.equal(opened.length, 0);
  assert.equal(timers.size, 0);
  assert.equal(w.get('Live.es'), null);
  assert.equal(w.get('Live.subs.size'), 0);
  assert.deepEqual(plain(w.get('document.listeners')), {}, 'no visibilitychange listener yet');
});

test('subscribe opens ONE EventSource after the debounce, with the names sorted and lines=12', () => {
  const { w, opened, timers, runTimers, sub } = lWorld();
  sub(B);
  sub(A);
  assert.equal(opened.length, 0, 'nothing opens before the debounce');
  assert.equal(timers.size, 1, 'one pending timer however many subscribe');
  assert.equal([...timers.values()][0].ms, 300);
  runTimers();
  assert.equal(opened.length, 1);
  assert.equal(opened[0].url, `/api/stream?names=${A},${B}&lines=12`);
  assert.ok(w.get('Live.es'), 'the connection is kept on Live.es');
  assert.equal(w.get('Live.url'), opened[0].url);
});

test('a changed set replaces the connection, an unchanged one keeps it, a second subscriber to a session changes nothing', () => {
  const { opened, runTimers, sub } = lWorld();
  sub(A);
  runTimers();
  assert.equal(opened.length, 1);
  sub(A);                                                                // a second listener for the same session
  runTimers();
  assert.equal(opened.length, 1, 'the set of names is the same');
  sub(B);
  runTimers();
  assert.equal(opened.length, 2);
  assert.equal(opened[0].closed, true, 'the old connection is closed');
  assert.equal(opened[1].url, `/api/stream?names=${A},${B}&lines=12`);
  sub(C);
  runTimers();
  assert.equal(opened.length, 3);
  assert.match(opened[2].url, new RegExp(`names=${C},${A},${B}&`));
  assert.equal(opened[1].closed, true);
});

test('a burst of changes inside the debounce is one reconnect', () => {
  const { opened, timers, runTimers, sub } = lWorld();
  sub(A); sub(B); sub(C);
  assert.equal(timers.size, 1, 'each change restarts the one timer');
  runTimers();
  assert.equal(opened.length, 1);
  assert.match(opened[0].url, new RegExp(`names=${C},${A},${B}&`));
});

test('the last unsubscribe closes the stream at once; one that leaves others schedules a reconnect with the smaller set', () => {
  const { w, opened, timers, runTimers, sub } = lWorld();
  const a = sub(A);
  const b = sub(B);
  runTimers();
  assert.equal(opened.length, 1);
  w.run('Live.unsubscribe(__tmux, __fn)');                               // __tmux / __fn are B's
  assert.equal(b.fn, w.get('__fn'));
  assert.equal(opened[0].closed, false, 'A is still on the stream until the debounce');
  runTimers();
  assert.equal(opened.length, 2);
  assert.equal(opened[1].url, `/api/stream?names=${A}&lines=12`);
  assert.equal(opened[0].closed, true);
  a.off();                                                               // the returned function unsubscribes too
  assert.equal(opened[1].closed, true, 'closed the moment it empties, not after the debounce');
  assert.equal(timers.size, 0);
  assert.equal(w.get('Live.es'), null);
  assert.equal(w.get('Live.subs.size'), 0);
  assert.equal(w.get('Live.url'), '');
  assert.doesNotThrow(() => a.off(), 'unsubscribing twice is harmless');
});

test('lines events reach only the subscribers of that session, as the array', () => {
  const { opened, runTimers, fire, sub } = lWorld();
  const a = sub(A);
  const a2 = sub(A);
  const b = sub(B);
  runTimers();
  fire(opened[0], 'lines', { name: A, lines: ['one', 'two'] });
  assert.deepEqual(plain(a.got), [['one', 'two']]);
  assert.deepEqual(plain(a2.got), [['one', 'two']], 'every subscriber of the session');
  assert.deepEqual(b.got, []);
  fire(opened[0], 'lines', { name: C, lines: ['stray'] });               // an older server ignores ?names=: the client filters
  fire(opened[0], 'lines', 'not json');
  fire(opened[0], 'lines', { name: A });                                 // no lines
  assert.deepEqual(plain(a.got), [['one', 'two']]);
  a.off();
  fire(opened[0], 'lines', { name: A, lines: ['three'] });
  assert.deepEqual(plain(a2.got), [['one', 'two'], ['three']]);
  assert.deepEqual(plain(a.got), [['one', 'two']], 'an unsubscribed fn hears nothing more');
});

test('a throwing subscriber does not stop the others or the stream', () => {
  const { w, opened, runTimers, fire, sub } = lWorld();
  w.ctx.console = { error() {} };
  w.run('globalThis.__bad = () => { throw new Error("boom"); }; Live.subscribe("' + A + '", __bad)');
  const ok = sub(A);
  runTimers();
  assert.doesNotThrow(() => fire(opened[0], 'lines', { name: A, lines: ['x'] }));
  assert.deepEqual(plain(ok.got), [['x']]);
});

test('a late subscriber to a session already streaming gets its last tail at once', () => {
  const { opened, runTimers, fire, sub } = lWorld();
  sub(A);
  runTimers();
  fire(opened[0], 'lines', { name: A, lines: ['tail', 'lines'] });
  const late = sub(A);
  assert.deepEqual(plain(late.got), [['tail', 'lines']], 'replayed without waiting for the tail to change');
  late.off();
  const b = sub(B);
  assert.deepEqual(b.got, [], 'nothing to replay for a session the stream has not sent yet');
});

test('the stream pauses while the tab is hidden and comes back, with a fresh connection, when it is shown', () => {
  const { w, opened, runTimers, sub } = lWorld();
  sub(A);
  runTimers();
  assert.equal(opened.length, 1);
  assert.equal((w.document.listeners.visibilitychange || []).length, 1, 'the listener arrives with the first subscriber');
  w.document.hidden = true;
  w.document.dispatch('visibilitychange');
  assert.equal(opened[0].closed, true, 'a hidden tab keeps no stream');
  assert.equal(w.get('Live.subs.size'), 1, 'but keeps its subscribers');
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  runTimers();
  assert.equal(opened.length, 2);
  assert.equal(opened[1].url, `/api/stream?names=${A}&lines=12`);
  // a subscriber that arrives while hidden waits for the tab
  w.document.hidden = true;
  w.document.dispatch('visibilitychange');
  sub(B);
  runTimers();
  assert.equal(opened.length, 2, 'nothing opens in a hidden tab');
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  runTimers();
  assert.equal(opened.length, 3);
  assert.match(opened[2].url, new RegExp(`names=${A},${B}&`));
});

test('at most 20 names go to the server (its cap), the rest still subscribe', () => {
  const { w, opened, runTimers, sub } = lWorld();
  for (let i = 0; i < 25; i++) sub(`p--r--s${String(i).padStart(2, '0')}`);
  runTimers();
  const names = decodeURIComponent(opened[0].url.split('names=')[1].split('&')[0]).split(',');
  assert.equal(names.length, 20);
  assert.deepEqual(names, [...names].sort());
  assert.equal(w.get('Live.subs.size'), 25);
});

test('demo mode: subscribe is a no-op that calls fn once with a note', () => {
  const { w, opened, timers, sub } = lWorld({ demo: true });
  const a = sub(A);
  assert.deepEqual(plain(a.got), [['demo: live tail unavailable']]);
  assert.equal(opened.length, 0);
  assert.equal(timers.size, 0);
  assert.equal(w.get('Live.subs.size'), 0, 'nothing is kept');
  assert.doesNotThrow(() => a.off());
});

test('DEBOUNCE_MS 0 syncs synchronously; a constructor that throws does not break subscribing', () => {
  const { w, opened, sub } = lWorld({ debounce: 0 });
  sub(A);
  assert.equal(opened.length, 1, 'no timer at all');
  w.ctx.console = { error() {} };
  w.run('EventSource = function () { throw new Error("no EventSource"); }');
  assert.doesNotThrow(() => sub(B));
  assert.equal(w.get('Live.es'), null);
});

test('reset() drops every subscriber, the connection and the listener', () => {
  const { w, opened, runTimers, sub } = lWorld();
  sub(A); sub(B);
  runTimers();
  w.run('Live.reset()');
  assert.equal(opened[0].closed, true);
  assert.equal(w.get('Live.subs.size'), 0);
  assert.equal((w.document.listeners.visibilitychange || []).length, 0);
});
