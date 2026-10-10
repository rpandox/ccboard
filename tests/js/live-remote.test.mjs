// Contract tests for Live.subscribe(ref, fn) with a remote Ref (issue #143; app/static/live.js): ONE EventSource per node at /api/nodes/<handle>/stream?names=<sorted>&lines=12 on this
// board's own origin, shared by every session of that node; local refs and bare names keep today's single source; a `gone` keeps the lines, tells the subscribers and closes the source;
// the reconnect waits out a backoff of at least 5 s per node and stops for good on a final gone; the last unsubscribe closes the source; a hidden tab keeps none; demo mode reads the
// fixture once. A recording EventSource and manual timers; nothing opens a connection.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';

function rWorld({ demo = false } = {}) {
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
  w.ctx.__now = 1_000_000;
  w.run('Date.now = () => __now');
  if (demo) w.run('demoFlag = true');
  w.run('Live.DEBOUNCE_MS = 0');
  const fire = (es, name, data) => { for (const fn of es.on[name] || []) fn({ data: typeof data === 'string' ? data : JSON.stringify(data) }); };
  const err = (es) => { if (es.onerror) es.onerror({}); };
  const advance = (ms) => { w.ctx.__now += ms; for (const [id, t] of [...timers]) { if (t.ms <= ms || t.ms === 0) { timers.delete(id); t.fn(); } else t.ms -= ms; } };
  const sub = (handle, tmux) => {
    const got = [];
    w.ctx.__fn = (lines, meta) => got.push({ lines, meta });
    w.ctx.__h = handle; w.ctx.__t = tmux;
    const off = w.run('Live.subscribe(Ref.session(__h, __t), __fn)');
    return { got, off };
  };
  return { w, opened, timers, fire, err, advance, sub };
}
const A = 'shop--api--s1', B = 'shop--api--s2', C = 'blog--web--s3';

test('a remote Ref opens ONE EventSource for its node at the hub stream route, names sorted, lines=12; sessions of the same node share it, another node gets its own', () => {
  const { opened, advance, sub } = rWorld();
  sub('build-box', B);
  assert.equal(opened.length, 1);
  assert.equal(opened[0].url, `/api/nodes/build-box/stream?names=${B}&lines=12`);
  sub('build-box', A);
  assert.equal(opened.length, 1, 'a change within 5 s of the last try waits: one try per 5 s per node, and the open stream is kept meanwhile');
  assert.equal(opened[0].closed, false);
  advance(5000);
  assert.equal(opened.length, 2);
  assert.equal(opened[0].closed, true, 'the source of the node is replaced, not added to');
  assert.equal(opened[1].url, `/api/nodes/build-box/stream?names=${A},${B}&lines=12`);
  sub('desk-pc', C);
  assert.equal(opened.length, 3);
  assert.equal(opened[2].url, `/api/nodes/desk-pc/stream?names=${C}&lines=12`);
  assert.equal(opened.filter((e) => !e.closed).length, 2, 'one source per node');
  assert.ok(opened.every((e) => e.url.startsWith('/api/nodes/')), 'only this board\'s own origin');
});

test('a bare name and a local Ref keep the one local source at /api/stream; a remote one never touches it', () => {
  const { w, opened, sub } = rWorld();
  w.ctx.__f = () => {};
  w.run("Live.subscribe('ccboard--ccboard--s1', __f)");
  w.run("Live.subscribe(Ref.session(null, 'ccboard--ccboard--s2'), __f)");
  sub('build-box', A);
  const urls = opened.filter((e) => !e.closed).map((e) => e.url);
  assert.deepEqual(urls.filter((u) => u.startsWith('/api/stream')), ['/api/stream?names=ccboard--ccboard--s1,ccboard--ccboard--s2&lines=12']);
  assert.equal(urls.filter((u) => u.startsWith('/api/nodes/')).length, 1);
  assert.deepEqual(plain(w.get('Array.from(Live.subs.keys())')), ['ccboard--ccboard--s1', 'ccboard--ccboard--s2']);
  assert.deepEqual(plain(w.get('Array.from(Live.rem.keys())')), ['build-box']);
});

test('lines go only to the subscriber of that name; a name nobody asked for is dropped; a late subscriber gets the last tail at once', () => {
  const { opened, fire, sub } = rWorld();
  const a = sub('build-box', A);
  const b = sub('build-box', B);
  const es = opened[opened.length - 1];
  fire(es, 'lines', { name: A, lines: ['one', 'two'] });
  fire(es, 'lines', { name: C, lines: ['not asked'] });
  fire(es, 'lines', { name: B, lines: ['b'] });
  assert.deepEqual(plain(a.got), [{ lines: ['one', 'two'] }]);
  assert.deepEqual(plain(b.got), [{ lines: ['b'] }]);
  const late = sub('build-box', A);
  assert.deepEqual(plain(late.got), [{ lines: ['one', 'two'] }], 'replayed to a late subscriber');
});

test('malformed events change nothing: bad JSON, a non-string name, lines that are not an array, non-string lines are dropped', () => {
  const { opened, fire, sub } = rWorld();
  const a = sub('build-box', A);
  const es = opened[0];
  fire(es, 'lines', 'not json');
  fire(es, 'lines', { name: 5, lines: [] });
  fire(es, 'lines', { name: A, lines: 'x' });
  fire(es, 'lines', null);
  assert.deepEqual(plain(a.got), []);
  fire(es, 'lines', { name: A, lines: ['ok', 7, null, { x: 1 }] });
  assert.deepEqual(plain(a.got), [{ lines: ['ok'] }], 'only strings are kept');
});

test('gone keeps the lines, tells every subscriber with a reason and the time it last saw lines, and closes the source (the browser does not retry by itself)', () => {
  const { w, opened, fire, sub } = rWorld();
  const a = sub('build-box', A);
  const es = opened[0];
  w.ctx.__now += 5000;
  fire(es, 'lines', { name: A, lines: ['kept'] });
  const seen = w.ctx.__now;
  w.ctx.__now += 60000;
  fire(es, 'gone', { reason: 'offline', message: 'build-box is offline', final: false, dropped: 0 });
  assert.equal(es.closed, true);
  const last = plain(a.got[a.got.length - 1]);
  assert.deepEqual(last.lines, ['kept'], 'the last lines stay');
  assert.deepEqual(last.meta.gone, { reason: 'offline', message: 'build-box is offline', final: false, seen });
  const late = sub('build-box', A);
  assert.deepEqual(plain(late.got[0]).meta.gone.reason, 'offline', 'a subscriber that comes later is told too');
});

test('after a gone the stream opens again on a backoff of 5 s, then 10, 20 and 30 at most: never two tries within 5 s of each other', () => {
  const { w, opened, timers, fire, advance, sub } = rWorld();
  sub('build-box', A);
  assert.equal(opened.length, 1);
  fire(opened[0], 'gone', { reason: 'offline', message: '', final: false });
  assert.equal(opened.length, 1, 'not at once');
  assert.ok([...timers.values()].some((t) => t.ms === 5000), 'a 5 s wait');
  advance(4999);
  assert.equal(opened.length, 1);
  advance(1);
  assert.equal(opened.length, 2, 'after 5 s');
  fire(opened[1], 'gone', { reason: 'offline', message: '', final: false });
  advance(9999);
  assert.equal(opened.length, 2);
  advance(1);
  assert.equal(opened.length, 3, 'the second wait is 10 s');
  fire(opened[2], 'gone', { reason: 'upstream_error', message: '', final: false });
  advance(20000);
  assert.equal(opened.length, 4, 'then 20 s');
  fire(opened[3], 'gone', { reason: 'offline', message: '', final: false });
  advance(30000);
  assert.equal(opened.length, 5, 'then 30 s');
  fire(opened[4], 'gone', { reason: 'offline', message: '', final: false });
  advance(29999);
  assert.equal(opened.length, 5, 'the wait stops growing at 30 s');
  advance(1);
  assert.equal(opened.length, 6);
  const tries = w.get('Live.rem.get("build-box").fails');
  assert.ok(tries >= 1);
});

test('an event that arrives resets the backoff, and the tiles leave "offline" at once', () => {
  const { opened, fire, advance, sub } = rWorld();
  const a = sub('build-box', A);
  fire(opened[0], 'gone', { reason: 'offline', message: '', final: false });
  advance(5000);
  assert.equal(opened.length, 2);
  fire(opened[1], 'tick', { sessions: [A] });
  const calm = plain(a.got[a.got.length - 1]);
  assert.equal(calm.meta, undefined, 'alive again: a plain call with no gone');
  fire(opened[1], 'gone', { reason: 'closed', message: '', final: false });
  advance(5000);
  assert.equal(opened.length, 3, 'back to 5 s after a good event');
});

test('an error with no gone is treated as an end: the source is closed and opened again after the backoff', () => {
  const { opened, err, advance, sub } = rWorld();
  const a = sub('build-box', A);
  err(opened[0]);
  assert.equal(opened[0].closed, true);
  assert.equal(plain(a.got[a.got.length - 1]).meta.gone.reason, 'upstream_error');
  advance(5000);
  assert.equal(opened.length, 2);
});

test('a final gone (a pair to renew, a pair removed, the board stopping) is not retried, however long we wait', () => {
  const { opened, fire, advance, sub } = rWorld();
  const a = sub('build-box', A);
  fire(opened[0], 'gone', { reason: 'revoked', message: 'the pair was removed', final: true });
  assert.equal(plain(a.got[a.got.length - 1]).meta.gone.final, true);
  advance(600000);
  assert.equal(opened.length, 1, 'no reconnect loop');
});

test('the last unsubscribe of a node closes its source; the other node keeps its own; unsubscribing one of two sessions keeps the source and asks for the rest', () => {
  const { w, opened, advance, sub } = rWorld();
  const a = sub('build-box', A);
  const b = sub('build-box', B);
  const d = sub('desk-pc', C);
  advance(5000);
  const live = () => opened.filter((e) => !e.closed).map((e) => e.url);
  assert.equal(live().length, 2);
  assert.ok(live().includes(`/api/nodes/build-box/stream?names=${A},${B}&lines=12`));
  a.off();
  advance(5000);
  assert.deepEqual(live().filter((u) => u.includes('build-box')), [`/api/nodes/build-box/stream?names=${B}&lines=12`], 'the remaining session is asked for alone');
  b.off();
  assert.deepEqual(live(), [`/api/nodes/desk-pc/stream?names=${C}&lines=12`], 'build-box has no source left');
  assert.equal(w.get('Live.rem.has("build-box")'), false);
  d.off();
  assert.equal(live().length, 0);
  assert.equal(w.get('Live.rem.size'), 0);
});

test('a hidden tab keeps no remote source and the tab coming back opens it again', () => {
  const { w, opened, advance, sub } = rWorld();
  sub('build-box', A);
  w.document.hidden = true;
  w.document.dispatch('visibilitychange');
  assert.equal(opened.filter((e) => !e.closed).length, 0);
  w.document.hidden = false;
  w.document.dispatch('visibilitychange');
  advance(5000);
  assert.equal(opened.filter((e) => !e.closed).length, 1);
  assert.equal(opened[opened.length - 1].url, `/api/nodes/build-box/stream?names=${A}&lines=12`);
});

test('a Ref outside the grammar subscribes to nothing', () => {
  const { w, opened } = rWorld();
  w.ctx.__f = () => {};
  for (const r of ["{ kind: 'session', node: 'Bad Handle', tmux: 'shop--api--s1' }", "{ kind: 'session', node: 'build-box', tmux: '../x' }", "{ kind: 'session', node: 'build-box', tmux: 'a--b' }"]) w.run(`Live.subscribe(${r}, __f)`);
  w.run("Live.subscribe(Ref.session('build-box', 'shop--api--s1'), 5)");
  assert.equal(opened.length, 0);
  assert.equal(w.get('Live.rem.size'), 0);
});

test('hostile text in a line or a reason is kept as plain strings; nothing is evaluated or turned into markup', () => {
  const { opened, fire, sub } = rWorld();
  const a = sub('build-box', A);
  const bad = '<img src=x onerror=alert(1)>';
  fire(opened[0], 'lines', { name: A, lines: [bad, '</script>'] });
  assert.deepEqual(plain(a.got[0].lines), [bad, '</script>']);
  fire(opened[0], 'gone', { reason: bad, message: bad.repeat(100), final: false });
  const g = plain(a.got[a.got.length - 1]).meta.gone;
  assert.ok(g.reason.length <= 40 && g.message.length <= 200, 'capped');
});

test('demo mode: a remote Ref reads its tail once from the fixture through the board\'s api() and opens no EventSource; the local demo note is unchanged', async () => {
  const { w, opened } = rWorld({ demo: true });
  w.run(`api = async (m, p) => { __asked.push(m + ' ' + p); return { node: 'build-box', age: 0, data: { name: 'x', lines: ['demo line'], cap: 40 } }; }`.replace('__asked', '(globalThis.__asked ||= [])'));
  const got = [];
  w.ctx.__fn = (lines) => got.push(plain(lines));
  w.run("Live.subscribe(Ref.session('build-box', 'shop--api--s1'), __fn)");
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(got, [['demo line']]);
  assert.deepEqual(plain(w.get('globalThis.__asked')), ['GET /api/nodes/build-box/sessions/shop--api--s1/pane']);
  w.run("Live.subscribe('ccboard--ccboard--s1', __fn)");
  assert.deepEqual(got[1], ['demo: live tail unavailable']);
  assert.equal(opened.length, 0);
});

test('an unsubscribed demo callback is not called late', async () => {
  const { w } = rWorld({ demo: true });
  w.run(`api = async () => ({ data: { lines: ['x'] } })`);
  const got = [];
  w.ctx.__fn = (lines) => got.push(lines);
  const off = w.run("Live.subscribe(Ref.session('build-box', 'shop--api--s1'), __fn)");
  off();
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(got, []);
});
