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
   toggleLive) is gone: the tail lives in the session rows now.

   A Ref (nodes.js) may stand for the session (issue #143): a local one is the bare name above, a remote one (Ref.session('build-box', name)) joins ONE EventSource per node at
   /api/nodes/<handle>/stream?names=<sorted>&lines=12 (this board's own origin: the page never talks to another node; the hub validates and re-emits the peer's stream). Its
   events are 'lines' {name, lines}, 'tick' {sessions} and, always last, 'gone' {reason, message, final}. fn(lines, meta): meta is undefined for a live tail and {gone: {reason,
   message, final, seen}} once the stream ended; the last lines are kept (seen: ms of the last event). A stream that ended (gone, or an error) is closed here and, unless
   gone.final (a re-pair or a removed pair), opened again after the backoff 5 s, 10 s, 20 s, 30 s: never more than one try per 5 s per node, and nothing is retried while the tab
   is hidden. In demo mode a remote tail is read once from the fixture, through api(). */
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
  rem: new Map(),           // node handle -> {subs: Map(tmux -> Set(fn)), tails: Map(tmux -> lines), es, url, timer, last, fails, seen, gone, paused}
  GAP_MS: 5000,             // the least time between two tries to open one node's stream
  GAP_MAX_MS: 30000,
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

/* Add fn to the session's subscribers. `tmux` is a bare name, a local Ref or a remote Ref. Returns a function that undoes it. */
Live.subscribe = function (tmux, fn) {
  const r = tmux && typeof tmux === 'object' && typeof Ref !== 'undefined' ? Ref.of(tmux) : null;
  if (r) {
    const node = Ref.nodeOf(r);
    if (node !== null) return Live.subRemote(node, r.tmux, fn);
    tmux = r.tmux;
  }
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
  const gone = Live.hidden();
  for (const [h, n] of Live.rem) {
    if (gone) { if (n.es || n.timer) { n.paused = true; Live.stopRemote(n); } }
    else if (n.paused) { n.paused = false; Live.syncRemote(h); }
  }
  if (gone) {
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
  for (const n of Live.rem.values()) Live.stopRemote(n);
  Live.rem.clear();
  Live.subs.clear();
  Live.tails.clear();
  Live.close();
  Live.paused = false;
  if (Live.watching && typeof document !== 'undefined') document.removeEventListener('visibilitychange', Live.onVisible);
  Live.watching = false;
};


/* ---------- remote refs (issue #143): one stream per node ---------- */

Live.call = function (fn, lines, meta) { try { fn(lines, meta); } catch (e) { console.error('ccboard live', e); } };

Live.subRemote = function (h, tmux, fn) {
  const off = () => Live.unsubRemote(h, tmux, fn);
  if (!Ref.isHandle(h) || !Ref.isTmux(tmux) || typeof fn !== 'function') return off;
  if (Live.isDemo()) {                                                    // the fixture's tail, once (demo/nodes.json tails), through the relay's pane route
    let live = true;
    api('GET', `/api/nodes/${h}/sessions/${tmux}/pane`).then((r) => { if (live) Live.call(fn, r && r.data && Array.isArray(r.data.lines) ? r.data.lines : [Live.DEMO_NOTE]); }, () => { if (live) Live.call(fn, [Live.DEMO_NOTE]); });
    return () => { live = false; };
  }
  let n = Live.rem.get(h);
  if (!n) { n = { subs: new Map(), tails: new Map(), es: null, url: '', timer: null, last: 0, fails: 0, seen: 0, gone: null, paused: false }; Live.rem.set(h, n); }
  let set = n.subs.get(tmux);
  const added = !set;
  if (!set) { set = new Set(); n.subs.set(tmux, set); }
  set.add(fn);
  Live.watch();
  if (n.tails.has(tmux) || n.gone) Live.call(fn, n.tails.get(tmux) || [], n.gone ? { gone: n.gone } : undefined);
  if (added) Live.schedRemote(h);
  return off;
};

Live.unsubRemote = function (h, tmux, fn) {
  const n = Live.rem.get(h);
  const set = n && n.subs.get(tmux);
  if (!set || !set.delete(fn) || set.size) return;
  n.subs.delete(tmux);
  n.tails.delete(tmux);
  if (n.subs.size) { Live.schedRemote(h); return; }
  Live.stopRemote(n);                                                     // the last ref of a node closes its source
  Live.rem.delete(h);
};

Live.stopRemote = function (n) {
  if (n.timer && typeof clearTimeout === 'function') clearTimeout(n.timer);
  n.timer = null;
  if (n.es) { try { n.es.close(); } catch (_) { /* already closed */ } }
  n.es = null;
  n.url = '';
};

Live.schedRemote = function (h, ms) {
  const n = Live.rem.get(h);
  if (!n) return;
  if (n.timer && typeof clearTimeout === 'function') clearTimeout(n.timer);
  n.timer = null;
  const wait = ms === undefined ? Live.DEBOUNCE_MS : ms;
  if (wait <= 0 || typeof setTimeout !== 'function') { Live.syncRemote(h); return; }
  n.timer = setTimeout(() => Live.syncRemote(h), wait);
};

/* Make the node's connection match its subscriber set. A final gone (re-pair, pair removed, board stopping) stays closed until the set changes to nothing; any other end waits
   out the backoff (the tries of one node are at least GAP_MS apart). */
Live.syncRemote = function (h) {
  const n = Live.rem.get(h);
  if (!n) return;
  n.timer = null;
  const names = Array.from(n.subs.keys()).sort().slice(0, Live.MAX_NAMES);
  if (!names.length) { Live.stopRemote(n); return; }
  if (Live.hidden()) { n.paused = true; Live.stopRemote(n); return; }
  const url = `/api/nodes/${h}/stream?names=${names.map(encodeURIComponent).join(',')}&lines=${Live.LINES}`;
  if (n.es && n.url === url) return;
  if (n.gone && n.gone.final) return;
  const wait = n.last + Math.min(Live.GAP_MAX_MS, Live.GAP_MS * Math.pow(2, Math.max(0, n.fails - 1))) - Date.now();
  if (n.last && wait > 0) { Live.schedRemote(h, wait); return; }               // an open stream stays until the new one may open
  Live.stopRemote(n);
  let es = null;
  try { es = new EventSource(url); } catch (e) { console.error('ccboard live', e); return; }
  n.es = es; n.url = url; n.last = Date.now();
  es.addEventListener('lines', (e) => Live.deliverRemote(h, es, 'lines', e));
  es.addEventListener('tick', (e) => Live.deliverRemote(h, es, 'tick', e));
  es.addEventListener('gone', (e) => Live.deliverRemote(h, es, 'gone', e));
  es.onerror = () => { if (Live.rem.get(h) === n && n.es === es) Live.ended(h, n, { reason: 'upstream_error', message: '', final: false }); };
};

Live.deliverRemote = function (h, es, type, e) {
  const n = Live.rem.get(h);
  if (!n || n.es !== es) return;
  let d = null;
  try { d = JSON.parse(e.data); } catch (_) { return; }
  if (!d || typeof d !== 'object') return;
  if (type === 'gone') { Live.ended(h, n, { reason: String(d.reason || 'closed').slice(0, 40), message: String(d.message || '').slice(0, 200), final: d.final === true }); return; }
  n.fails = 0; n.seen = Date.now();
  if (n.gone) { n.gone = null; for (const [t, set] of n.subs) for (const fn of Array.from(set)) Live.call(fn, n.tails.get(t) || []); }     // alive again: the tiles leave "offline"
  if (type !== 'lines' || typeof d.name !== 'string' || !Array.isArray(d.lines)) return;
  const set = n.subs.get(d.name);
  if (!set) return;                                                        // not asked for
  const lines = d.lines.filter((x) => typeof x === 'string');
  n.tails.set(d.name, lines);
  for (const fn of Array.from(set)) Live.call(fn, lines);
};

/* The stream of a node ended: keep the lines, tell every subscriber, close the source (so the browser does not retry by itself) and, unless it was final, try again later. */
Live.ended = function (h, n, gone) {
  Live.stopRemote(n);
  n.fails += 1;
  n.gone = { ...gone, seen: n.seen };
  for (const [t, set] of n.subs) for (const fn of Array.from(set)) Live.call(fn, n.tails.get(t) || [], { gone: n.gone });
  if (!gone.final) Live.schedRemote(h, 0);
};
