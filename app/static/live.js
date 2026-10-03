/* ccboard live (v0.5.5): the pane-tail stream behind the session rows' tail expanders.
   One EventSource for the whole page: Live.subscribe(tmux, fn) adds a session to it, Live.unsubscribe(tmux, fn) takes it off again. The
   stream is (re)opened when the set of subscribed sessions changes (debounced, so ten rows expanding in a row cost one connection),
   closed the moment the set is empty, and paused while the tab is hidden. It is never touched by the 3 s state poll: only a row
   being expanded or collapsed changes the set.

   GET /api/stream?names=<a,b,...>&lines=12 answers an SSE of 'lines' events {name, lines} (when a session's visible tail changed) and
   'tick' events {sessions}. The server filters on names (at most 20, validated with tmux.split_name); the client filters again, so an
   older server that ignores the query still delivers only what was asked for. fn(lines: string[]) runs on every 'lines' event of its
   session and once, at once, with the last tail the stream already holds (a row that is expanded again does not wait for a change).
   In demo mode (?demo=1) there is no EventSource to fake: subscribe() calls fn once with a one-line note and does nothing else.
   Definition only at load: no connection, listener or timer exists until the first subscribe(). The v0.4 Live grid (liveStart, liveStop,
   toggleLive) is gone: the tail lives in the session rows now. */
'use strict';

const Live = {
  subs: new Map(),          // tmux name -> Set of fn(lines)
  tails: new Map(),         // tmux name -> the last lines delivered (replayed to a late subscriber)
  es: null,                 // the one EventSource, or null
  url: '',                  // what es was opened with
  timer: null,              // the pending debounced sync
  paused: false,            // the tab is hidden: the stream is closed and the subscribers stay
  watching: false,          // the visibilitychange listener is installed
  LINES: 12,                // tail length asked for (server: 1..40)
  MAX_NAMES: 20,            // the server's cap on ?names=
  DEBOUNCE_MS: 300,         // 0 syncs synchronously (the tests do)
  DEMO_NOTE: 'demo: live tail unavailable',
};

Live.isDemo = function () { try { return typeof demoOn === 'function' && demoOn(); } catch (_) { return false; } };

Live.hidden = function () { try { return !!(typeof document !== 'undefined' && document.hidden); } catch (_) { return false; } };

/* The subscribed session names, sorted and cut at the server's cap. */
Live.names = function () { return Array.from(Live.subs.keys()).sort().slice(0, Live.MAX_NAMES); };

Live.streamUrl = function () {
  const names = Live.names();
  return names.length ? `/api/stream?names=${names.map(encodeURIComponent).join(',')}&lines=${Live.LINES}` : '';
};

Live.count = function () { let n = 0; for (const set of Live.subs.values()) n += set.size; return n; };

/* Add fn to the session's subscribers. Returns a function that undoes it. */
Live.subscribe = function (tmux, fn) {
  const off = () => Live.unsubscribe(tmux, fn);
  if (typeof tmux !== 'string' || !tmux || typeof fn !== 'function') return off;
  if (Live.isDemo()) { try { fn([Live.DEMO_NOTE]); } catch (e) { console.error('ccboard live', e); } return off; }
  let set = Live.subs.get(tmux);
  const added = !set;
  if (!set) { set = new Set(); Live.subs.set(tmux, set); }
  set.add(fn);
  Live.watch();
  if (Live.tails.has(tmux)) { try { fn(Live.tails.get(tmux)); } catch (e) { console.error('ccboard live', e); } }
  if (added) Live.schedule();
  return off;
};

Live.unsubscribe = function (tmux, fn) {
  const set = Live.subs.get(tmux);
  if (!set || !set.delete(fn)) return;
  if (set.size) return;
  Live.subs.delete(tmux);
  Live.tails.delete(tmux);
  if (!Live.subs.size) Live.close();          // empty: the stream goes at once
  else Live.schedule();
};

Live.schedule = function () {
  if (Live.timer && typeof clearTimeout === 'function') clearTimeout(Live.timer);
  Live.timer = null;
  if (Live.DEBOUNCE_MS <= 0 || typeof setTimeout !== 'function') { Live.sync(); return; }
  Live.timer = setTimeout(Live.sync, Live.DEBOUNCE_MS);
};

/* Make the connection match the subscriber set: nothing to ask for closes it, an unchanged set keeps it, a changed one replaces it. */
Live.sync = function () {
  Live.timer = null;
  const url = Live.streamUrl();
  if (!url) { Live.close(); return; }
  if (Live.hidden()) { Live.paused = true; Live.close(); return; }
  if (Live.es && Live.url === url) return;
  Live.close();
  Live.open(url);
};

Live.open = function (url) {
  let es = null;
  try { es = new EventSource(url); } catch (e) { console.error('ccboard live', e); return; }
  Live.es = es;
  Live.url = url;
  es.addEventListener('lines', (e) => Live.deliver(e));
  es.onerror = () => { /* EventSource reconnects on its own */ };
};

Live.close = function () {
  if (Live.timer && typeof clearTimeout === 'function') clearTimeout(Live.timer);
  Live.timer = null;
  if (Live.es) { try { Live.es.close(); } catch (_) { /* already closed */ } }
  Live.es = null;
  Live.url = '';
};

Live.deliver = function (e) {
  let d = null;
  try { d = JSON.parse(e.data); } catch (_) { return; }
  if (!d || typeof d.name !== 'string' || !Array.isArray(d.lines)) return;
  const set = Live.subs.get(d.name);
  if (!set) return;                           // not asked for (an older server ignores ?names=)
  Live.tails.set(d.name, d.lines);
  for (const fn of Array.from(set)) { try { fn(d.lines); } catch (err) { console.error('ccboard live', err); } }
};

/* A hidden tab keeps no stream: it closes now and comes back, with a fresh tail for every session, when the tab is visible again. */
Live.onVisible = function () {
  if (Live.hidden()) {
    if (Live.es) { Live.paused = true; Live.close(); }
    return;
  }
  if (Live.paused) { Live.paused = false; Live.schedule(); }
};

Live.watch = function () {
  if (Live.watching || typeof document === 'undefined' || typeof document.addEventListener !== 'function') return;
  Live.watching = true;
  document.addEventListener('visibilitychange', Live.onVisible);
};

/* Test and teardown helper: drop every subscriber and the connection. */
Live.reset = function () {
  Live.subs.clear();
  Live.tails.clear();
  Live.close();
  Live.paused = false;
  if (Live.watching && typeof document !== 'undefined') document.removeEventListener('visibilitychange', Live.onVisible);
  Live.watching = false;
};

