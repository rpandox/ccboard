/* ccboard usage page (v0.5.17, #/usage?range=24h|7d|30d): where the limits, the spend and the activity of the box are read. Six sections in the
   order the usage analysis asked for (phone-heavy evenings, the live pain is the limit): Limits (two gauges from the 3 s state, one line chart of
   rl_5h / rl_7d over the range with the limit episodes marked), Cost per day (stacked bars by project or agent), Sessions (the priciest ones, an
   Open button on a live session, a per-session ctx and cost chart under a row), Projects ($, active hours and $/h side by side), Activity (the
   24x7 heatmap and the hour-of-day profile) and Timeline (a session Gantt of the last 24 h, 48 h on desktop).

   Definition only at load, apart from the registerPage call. The drawing is charts.js (window.Charts: line, stackedBars, gantt, heatmap and the
   formatters); every call here is guarded, so a charts.js that failed to load shows an inline error with Retry instead of a blank page.

   Data (all GETs, never from the 3 s state poll): /api/usage/summary?days=7|30 (cost, windows, sessions, heatmap, episodes, unpriced),
   /api/series?series=rl_5h,rl_7d&key=claude&since=<range>, /api/series/events?series=state&since=24h|48h, and per open live session
   /api/series?series=ctx,scost&key=<tmux>&since=24h. They are fetched in parallel on mount and on a range change, then every 60 s while the tab is
   visible. A response for a range or a page that is no longer current is cached and never painted (the route token). A range already seen paints
   at once from its cache and refreshes behind it; a new one paints a skeleton. uPlot is loaded lazily through Charts.ready(), only on this page.

   Usage.cur is the mounted page record (null when not mounted); its .loading is the promise of the latest load, for the tests. */
'use strict';

const Usage = {
  RANGES: ['24h', '7d', '30d'],
  RANGE_KEY: 'ccboard:charts:range',               // localStorage: the last range (a ?range= in the address wins and is written back)
  STACK_KEY: 'ccboard:charts:stack',               // localStorage: the cost bars' stack (project | agent)
  DEFAULT_RANGE: '7d',
  REFRESH_MS: 60000,
  STALE_MS: 20000,                                 // a cached range older than this is refetched behind the paint when it is shown again
  WINDOW: { '24h': 'today', '7d': '7d', '30d': '30d' },
  WINDOW_NAME: { '24h': 'Today', '7d': 'Last 7 days', '30d': 'Last 30 days' },
  // the cost caption: the 24 h range still draws a week of bars, so it says both (the bars' window, then the number's)
  CAPTION_NAME: { '24h': 'Last 7 days · today', '7d': 'Last 7 days', '30d': 'Last 30 days' },
  CAPTION_TIP: { '24h': 'The bars show the last 7 days; the numbers count today only (since local midnight)', '7d': 'The last 7 days, bars and numbers', '30d': 'The last 30 days, bars and numbers' },
  GANTT_ROWS: 30,
  PROJECT_ROWS: 12,
  UNATTRIBUTED: '(unattributed)',
  UNATTRIBUTED_TIP: 'sessions the board did not start',
  SECTIONS: [
    ['limits', 'Limits', 'statusline (official)'],
    ['cost', 'Cost per day', 'API-equivalent (ccusage list price)'],
    ['sessions', 'Sessions', 'API-equivalent · top 10 by cost'],
    ['projects', 'Projects', 'API-equivalent · active hours from state events'],
    ['activity', 'Activity', 'hook events, local time'],
    ['timeline', 'Timeline', 'session state events'],
  ],
  cur: null,
};

/* ---------- small helpers ---------- */

Usage.num = function (v) { return typeof v === 'number' && Number.isFinite(v) ? v : 0; };

Usage.isRange = function (r) { return Usage.RANGES.includes(r); };
Usage.days = function (range) { return range === '30d' ? 30 : 7; };

Usage.storedRange = function () {
  try { const r = localStorage.getItem(Usage.RANGE_KEY); if (Usage.isRange(r)) return r; } catch (_) { /* storage may be unavailable */ }
  return Usage.DEFAULT_RANGE;
};
Usage.saveRange = function (r) { try { localStorage.setItem(Usage.RANGE_KEY, r); } catch (_) { /* storage may be unavailable */ } };
Usage.routeRange = function (route) { const r = route && route.query ? route.query.range : null; return Usage.isRange(r) ? r : null; };
Usage.storedStack = function () {
  try { const s = localStorage.getItem(Usage.STACK_KEY); if (s === 'project' || s === 'agent') return s; } catch (_) { /* storage may be unavailable */ }
  return 'project';
};

/* The viewer's zone in minutes east of UTC (-720..840); the summary buckets its days and hours in it. */
Usage.tzMin = function () {
  try { const m = -new Date().getTimezoneOffset(); if (Number.isFinite(m)) return Math.max(-720, Math.min(840, m)); } catch (_) { /* no Date zone */ }
  return 345;
};

Usage.tzLabel = function (min) {
  const f = Usage.charts('fmtTz');
  if (f) return String(f(Usage.num(min)));
  const m = Math.round(Usage.num(min));
  if (!m) return 'UTC';
  const a = Math.abs(m);
  return `UTC${m < 0 ? '-' : '+'}${Math.floor(a / 60)}${a % 60 ? ':' + String(a % 60).padStart(2, '0') : ''}`;
};

/* Epoch seconds from an epoch (s or ms) or an ISO string; 0 when it is neither. */
Usage.epoch = function (v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v > 1e11 ? v / 1000 : v;
  if (typeof v === 'string' && v) { const t = Date.parse(v); return Number.isNaN(t) ? 0 : t / 1000; }
  return 0;
};

Usage.clock = function (epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (Math.abs(epoch - Date.now() / 1000) < 20 * 3600) return hm;
  return `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()]} ${hm}`;
};

Usage.hidden = function () { try { return !!(typeof document !== 'undefined' && document.hidden); } catch (_) { return false; } };

Usage.wide = function () { try { return typeof matchMedia === 'function' && !!matchMedia('(min-width: 840px)').matches; } catch (_) { return false; } };

/* A touch screen (or the QA's html.force-coarse): the timeline rows get taller so a finger can pick one. */
Usage.coarse = function () {
  try { return !!(typeof document !== 'undefined' && document.documentElement && document.documentElement.classList.contains('force-coarse')) || (typeof matchMedia === 'function' && !!matchMedia('(pointer:coarse)').matches); } catch (_) { return false; }
};

/* A Charts function, bound, or null while charts.js is missing. */
Usage.charts = function (name) {
  return typeof Charts !== 'undefined' && Charts && typeof Charts[name] === 'function' ? Charts[name].bind(Charts) : null;
};

Usage.need = function (name) {
  const f = Usage.charts(name);
  if (!f) throw new Error('charts.js did not load (Charts.' + name + ' is missing)');
  return f;
};

Usage.usd = function (v) { const f = Usage.charts('fmtUsd'); const n = Usage.num(v); return f ? String(f(n)) : '$' + (n >= 100 ? String(Math.round(n)) : n.toFixed(2)); };
Usage.tok = function (v) {
  const f = Usage.charts('fmtTok');
  const n = Usage.num(v);
  if (f) return String(f(n));
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(Math.round(n));
};
Usage.hours = function (v) { return `${Usage.num(v).toFixed(1)} h`; };
Usage.model = function (m) { const f = Usage.charts('shortModel'); const s = String(m === null || m === undefined ? '' : m); return f ? String(f(s)) : s.replace(/^claude-/, ''); };

/* The muted hue class of a model or project chip (pages/agents.js chipHue), '' when that file is not there. */
Usage.hue = function (kind, key) { return typeof chipHue === 'function' ? chipHue(kind, key) : ''; };

/* The caption of the cost chart: '<window>: $ · tokens · hours' ('Last 7 days · today: ...' for 24h, which still draws a week of bars). */
Usage.caption = function (sum, range) { return `${Usage.CAPTION_NAME[range]}${range === '24h' ? ' ' : ': '}${Usage.totalsText(sum, range)}`; };

Usage.sig = function (v) { try { return JSON.stringify(v); } catch (_) { return String(Math.random()); } };

Usage.go = function (hash) { if (typeof navigate === 'function') navigate(hash); else if (typeof location !== 'undefined') location.hash = hash; };

/* #/p/<project>, or null when the name is not a route param ('(unattributed)', a dot or a space in it: buildHash throws). */
Usage.projectHash = function (project) {
  if (!project || project === Usage.UNATTRIBUTED) return null;
  try { return buildHash('project', { project }); } catch (_) { return null; }
};
Usage.sessionHash = function (tmux) {
  if (!tmux) return null;
  try { return buildHash('session', { tmux }); } catch (_) { return null; }
};

/* GET through Charts.fetch (cached by path, deduped; {force} skips the cache for the 60 s refresh), or api() when charts.js is not there. */
Usage.get = function (path, fresh) {
  const f = Usage.charts('fetch');
  if (f) return Promise.resolve().then(() => f(path, fresh ? { force: true } : undefined));
  return Promise.resolve().then(() => api('GET', path));
};

/* The width-derived point count of the series request: about a point per 3 px, in steps of 20 so the cache key does not wobble. */
Usage.points = function (P) {
  const w = P && P.refs && P.refs.limHost ? Usage.num(P.refs.limHost.clientWidth) : 0;
  return Math.max(120, Math.min(400, Math.round(w / 3 / 20) * 20));
};

Usage.errText = function (e) { return String((e && e.message) || e || 'request failed'); };

/* ---------- live sessions (for the Open button) ---------- */

/* Map of lowercase claude_session_id -> {tmux, project, repo} over every session of the state. */
Usage.liveMap = function (st) {
  const out = new Map();
  for (const p of ((st && st.projects) || [])) {
    const lists = [];
    if (p.root) lists.push([p.root.name || 'root', p.root.sessions]);
    for (const r of (p.repos || [])) lists.push([r.name, r.sessions]);
    lists.push([null, p.orphan_sessions]);
    for (const [repo, sessions] of lists) {
      for (const s of (sessions || [])) {
        const id = s && s.claude_session_id ? String(s.claude_session_id).toLowerCase() : '';
        if (id && s.tmux) out.set(id, { tmux: s.tmux, project: p.name, repo: repo || s.repo || '' });
      }
    }
  }
  return out;
};

Usage.uuidOf = function (key) { const m = /^claude:(.+)$/i.exec(String(key || '')); return m ? m[1].toLowerCase() : ''; };

Usage.liveSig = function (st) { return Usage.sig([...Usage.liveMap(st)].map(([id, v]) => [id, v.tmux]).sort()); };

/* ---------- the gauge (the Home card's classes) ---------- */

Usage.tone = function (pct) { return pct >= 85 ? 'bad' : pct >= 60 ? 'warn' : 'ok'; };

Usage.gauge = function (label) {
  const fill = el('i');
  const val = el('span', { class: 'g-val mono' });
  const reset = el('span', { class: 'g-reset dim' });
  const bar = el('span', { class: 'g-bar', role: 'img' }, fill);
  const root = el('div', { class: 'gauge hidden', 'data-gauge': label }, el('b', { class: 'g-label', text: label }), bar, val, reset);
  root.set = (w) => {
    const ok = !!w && typeof w.pct === 'number' && Number.isFinite(w.pct);
    root.classList.toggle('hidden', !ok);
    if (!ok) return;
    const pct = Math.max(0, Math.min(100, w.pct));
    for (const t of ['ok', 'warn', 'bad']) root.classList.toggle(t, t === Usage.tone(pct));
    fill.style.width = `${pct}%`;
    setText(val, `${Math.round(pct)}%`);
    const eta = w.resets_at ? fmtIn(w.resets_at) : '';
    setText(reset, eta ? `resets in ${eta} · ${Usage.clock(w.resets_at)}` : '');
    bar.setAttribute('aria-label', `${label} ${Math.round(pct)}% used`);
    root.setAttribute('title', `${label} window: ${Math.round(pct)}% used` + (w.resets_at ? ` · resets ${Usage.clock(w.resets_at)}` : ''));
  };
  return root;
};

/* {pct, resets_at} of a state usage window (used_percentage, or utilization), or null. */
Usage.reading = function (w) {
  if (!w || typeof w !== 'object') return null;
  const pct = typeof w.used_percentage === 'number' ? w.used_percentage : (typeof w.utilization === 'number' ? w.utilization : null);
  if (pct === null || !Number.isFinite(pct)) return null;
  return { pct, resets_at: Usage.epoch(w.resets_at) };
};

/* ---------- page state ---------- */

Usage.alive = function (P) { return !!P && !P.dead && Usage.cur === P; };
Usage.entry = function (P) { return P.cache[P.range] || null; };
Usage.summaryOf = function (P) { const c = Usage.entry(P); return c && c.summary && Array.isArray(c.summary.daily) ? c.summary : null; };

Usage.skeleton = function () { return el('div', { class: 'usk skeleton', 'aria-hidden': 'true', text: 'Loading usage' }); };

Usage.setBody = function (P, id, ...kids) {
  const body = P.refs.body[id];
  body.textContent = '';
  for (const k of kids) if (k) body.append(k);
};

Usage.newSession = function () {
  if (typeof Shell === 'undefined' || !Shell || typeof Shell.openCreate !== 'function') return null;
  return el('button', { class: 'small', type: 'button', text: '+ session', onclick: () => Shell.openCreate('session') });     // the page has no primary: an empty section's next step is a plain button
};

/* An empty state that names its next step (text, and the + session button when asked for). */
Usage.empty = function (title, hint, withButton) {
  const node = pageEmpty('timeline-line-chart', title, hint);
  if (withButton) { const b = Usage.newSession(); if (b) node.append(b); }
  return node;
};

Usage.errorBlock = function (P, text) {
  return el('div', { class: 'callout bad uerr', role: 'alert' }, el('span', { class: 'uerr-text', text: String(text) }),
    el('button', { class: 'small', type: 'button', text: 'Retry', onclick: () => Usage.retry(P) }));
};

/* Run one section's painter: an exception becomes that section's error block, the others keep their paint. */
Usage.safe = function (P, id, fn) {
  try { fn(); } catch (e) {
    console.error('ccboard usage', id, e);
    P.sigs[id] = '';
    if (id === 'limits') { Usage.limitsShow(P, 'slot', Usage.errorBlock(P, Usage.errText(e))); return; }
    Usage.setBody(P, id, Usage.errorBlock(P, Usage.errText(e)));
  }
};

/* ---------- build ---------- */

Usage.build = function (root, route) {
  const fromRoute = Usage.routeRange(route);
  const range = fromRoute || Usage.storedRange();
  if (fromRoute) Usage.saveRange(fromRoute);
  const P = { range, token: 0, dead: false, route, st: null, cache: {}, open: new Set(), details: new Map(), stack: Usage.storedStack(), sigs: {},
    refs: { body: {}, prov: {}, toggle: {}, stackBtn: {} }, timer: null, onVisible: null, unbind: null, stale: false, liveSig: '', showAll: false,
    events: null, evErr: '', evDone: false, loading: Promise.resolve(), ready: null, rows: new Map(), limDrawn: false };
  const R = P.refs;

  const seg = el('div', { class: 'useg pj-switch', role: 'group', 'aria-label': 'Time range' });
  for (const r of Usage.RANGES) {
    R.toggle[r] = el('button', { class: 'small useg-btn pj-repo-btn', type: 'button', 'data-range': r, 'aria-pressed': r === range ? 'true' : 'false',
      title: `Show the last ${r === '24h' ? '24 hours' : r === '7d' ? '7 days' : '30 days'} (r cycles)`, text: r, onclick: () => Usage.setRange(P, r) });
    seg.append(R.toggle[r]);
  }
  R.alert = el('div', { class: 'callout warn ualert hidden', role: 'alert' });
  const head = el('div', { class: 'page-head' }, el('h1', { text: 'Usage' }), el('div', { class: 'actions' }, seg));

  // Limits: the gauges and their note, the slot for an empty or error block, the chart host Charts.line owns (kept, never rebuilt)
  R.g5 = Usage.gauge('5H');
  R.g7 = Usage.gauge('7D');
  R.gnote = el('p', { class: 'dim unote hidden' });
  R.limSlot = el('div', { class: 'uslot hidden' });
  R.limHost = el('div', { class: 'chart lim-chart loading', 'aria-busy': 'true' });
  R.limCap = el('p', { class: 'dim unote hidden', text: 'dashed lines: 60% warn, 85% critical · ticks mark limit hits and window resets' });
  // Cost: the segmented stack toggle lives in the section head
  const stack = el('div', { class: 'useg pj-switch', role: 'group', 'aria-label': 'Stack the bars by' });
  for (const by of ['project', 'agent']) {
    R.stackBtn[by] = el('button', { class: 'small useg-btn pj-repo-btn', type: 'button', 'data-stack': by, 'aria-pressed': by === P.stack ? 'true' : 'false',
      title: `Stack each day by ${by}`, text: by, onclick: () => Usage.setStack(P, by) });
    stack.append(R.stackBtn[by]);
  }

  const sections = [];
  for (const [id, title, prov] of Usage.SECTIONS) {
    R.prov[id] = el('span', { class: 'prov dim', text: prov });
    const kids = [el('h2', { text: title }), R.prov[id]];
    if (id === 'cost') kids.push(stack);
    const body = el('div', { class: 'ubody', 'data-body': id });
    R.body[id] = body;
    if (id === 'limits') body.append(el('div', { class: 'uc-gauges' }, R.g5, R.g7), R.gnote, R.limSlot, R.limHost, R.limCap);
    else body.append(Usage.skeleton());
    sections.push(el('section', { class: 'usec', 'data-sec': id }, el('div', { class: 'usec-head' }, ...kids), body));
  }
  root.append(el('div', { class: 'usage-page', 'data-usage': '' }, head, R.alert, el('div', { class: 'ugrid' }, ...sections)));   // not 'usage': style.css owns that legacy strip rule
  return P;
};

/* ---------- range and stack ---------- */

Usage.syncToggle = function (P) {
  for (const r of Usage.RANGES) P.refs.toggle[r].setAttribute('aria-pressed', r === P.range ? 'true' : 'false');
  for (const by of ['project', 'agent']) P.refs.stackBtn[by].setAttribute('aria-pressed', by === P.stack ? 'true' : 'false');
};

/* The range changes at once: paint from the cache of that range when there is one (and refresh behind it when it is old), else a skeleton. */
Usage.applyRange = function (P, range) {
  if (!Usage.isRange(range)) return;
  P.range = range;
  P.token += 1;
  P.sigs = {};
  Usage.syncToggle(P);
  const c = Usage.entry(P);
  if (c && (c.summary || c.series)) {
    Usage.paintAll(P);
    if (Date.now() - c.at > Usage.STALE_MS) Usage.load(P);                      // old enough: refresh behind the paint
  } else {
    Usage.paintSkeleton(P);
    Usage.load(P);
  }
};

/* A tap on a range button or the r key: remember it, repaint, then put it in the address (replace: no history entry per tap). */
Usage.setRange = function (P, range) {
  if (!Usage.alive(P) || !Usage.isRange(range) || range === P.range) return;
  Usage.saveRange(range);
  Usage.applyRange(P, range);
  try {
    if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('usage', {}, { range }), { replace: true });
  } catch (e) { console.error('ccboard usage range', e); }
};

Usage.cycle = function (P) {
  const i = Usage.RANGES.indexOf(P.range);
  Usage.setRange(P, Usage.RANGES[(i + 1) % Usage.RANGES.length]);
  return true;
};

Usage.setStack = function (P, by) {
  if (!Usage.alive(P) || (by !== 'project' && by !== 'agent') || by === P.stack) return;
  P.stack = by;
  try { localStorage.setItem(Usage.STACK_KEY, by); } catch (_) { /* storage may be unavailable */ }
  P.sigs.cost = '';
  Usage.syncToggle(P);
  Usage.safe(P, 'cost', () => Usage.paintCost(P));
};

/* ---------- loading ---------- */

Usage.ready = function () {
  const f = Usage.charts('ready');
  if (!f) return Promise.resolve(false);
  return Promise.resolve().then(() => f()).then(() => true, (e) => { console.error('ccboard usage: uPlot did not load', e); return false; });
};

/* Fetch the summary, the limit series and the state events in parallel and paint each section when its data arrives. fresh: skip Charts.fetch's
   cache (the 60 s refresh). Returns the promise that settles when all three did; P.loading holds it. */
Usage.load = function (P, opts) {
  const o = opts || {};
  const range = P.range;
  const token = P.token;
  const c = P.cache[range] || (P.cache[range] = { summary: null, series: null, at: 0, err: {}, sumDone: false, serDone: false });
  const current = () => Usage.alive(P) && P.token === token;
  const hours = Usage.wide() ? 48 : 24;
  c.hours = hours;
  const get = (path) => Usage.get(path, o.fresh);
  const sum = get(`/api/usage/summary?days=${Usage.days(range)}&tz_min=${Usage.tzMin()}`);
  const ser = get(`/api/series?series=rl_5h,rl_7d&key=claude&since=${range}&points=${Usage.points(P)}`);
  const evs = get(`/api/series/events?series=state&since=${hours}h`);

  const sSum = sum.then((v) => {
    if (v && typeof v === 'object') { c.summary = v; c.at = Date.now(); delete c.err.summary; } else c.err.summary = 'empty answer';
  }, (e) => { c.err.summary = Usage.errText(e); }).then(() => {
    c.sumDone = true;
    if (current()) Usage.paintSummary(P);
  });
  const sSer = ser.then((v) => {
    if (v && typeof v === 'object') { c.series = v; c.at = Date.now(); delete c.err.series; } else c.err.series = 'empty answer';
  }, (e) => { c.err.series = Usage.errText(e); }).then(() => { c.serDone = true; });
  const sEv = evs.then((v) => {
    P.events = v && typeof v === 'object' ? v : { events: [] };
    P.evErr = '';
  }, (e) => { P.evErr = Usage.errText(e); }).then(() => {
    P.evDone = true;
    P.evHours = hours;
    if (current()) Usage.safe(P, 'timeline', () => Usage.paintTimeline(P));
  });
  // the limit chart wants the series and the summary (episodes, resets) at once: Charts.line only redraws marks when the data moved
  const sLim = Promise.all([sSum, sSer]).then(() => { if (current()) Usage.safe(P, 'limits', () => Usage.paintLimits(P)); });
  const done = Promise.all([sLim, sEv]).then(() => {
    if (current()) { Usage.paintAlert(P); Usage.refreshDetails(P, o.fresh); }
  });
  P.loading = done;
  return done;
};

Usage.retry = function (P) {
  if (!Usage.alive(P)) return;
  P.ready = Usage.ready();
  const c = Usage.entry(P);
  if (c) { c.err = {}; c.sumDone = !!c.summary; c.serDone = !!c.series; }
  P.evErr = '';
  P.sigs = {};
  Usage.paintSkeleton(P);
  Usage.load(P, { fresh: true });
};

/* ---------- paint ---------- */

Usage.paintSkeleton = function (P) {
  for (const id of ['cost', 'sessions', 'projects', 'activity']) Usage.setBody(P, id, Usage.skeleton());
  if (!P.evDone) Usage.setBody(P, 'timeline', Usage.skeleton());
  Usage.limitsShow(P, 'loading');
  P.refs.alert.classList.add('hidden');
};

Usage.paintAll = function (P) {
  Usage.paintGauges(P);
  Usage.paintSummary(P);
  Usage.safe(P, 'limits', () => Usage.paintLimits(P));
  if (P.evDone) Usage.safe(P, 'timeline', () => Usage.paintTimeline(P));
  Usage.paintAlert(P);
};

Usage.paintSummary = function (P) {
  Usage.paintGauges(P);
  Usage.safe(P, 'cost', () => Usage.paintCost(P));
  Usage.safe(P, 'sessions', () => Usage.paintSessions(P));
  Usage.safe(P, 'projects', () => Usage.paintProjects(P));
  Usage.safe(P, 'activity', () => Usage.paintActivity(P));
};

/* A summary-based section without its data: a skeleton while the fetch runs, the error block when it failed, else the empty state (title, hint). */
Usage.noSummary = function (P, id, title, hint) {
  const c = Usage.entry(P);
  P.sigs[id] = '';
  if (!c || !c.sumDone) { Usage.setBody(P, id, Usage.skeleton()); return; }
  if (c.err.summary) { Usage.setBody(P, id, Usage.errorBlock(P, `Could not load the usage summary: ${c.err.summary}`)); return; }
  Usage.setBody(P, id, Usage.empty(title, hint, id === 'cost'));
};

Usage.paintAlert = function (P) {
  const c = Usage.entry(P);
  const parts = [];
  const msgs = [];
  if (c && c.err.summary && c.summary) { parts.push('the usage summary'); msgs.push(c.err.summary); }
  if (c && c.err.series && c.series) { parts.push('the limit series'); msgs.push(c.err.series); }
  if (P.evErr && P.events) { parts.push('the timeline'); msgs.push(P.evErr); }
  const a = P.refs.alert;
  a.classList.toggle('hidden', !parts.length);
  a.textContent = '';
  if (!parts.length) return;
  a.append(el('span', { text: `Could not refresh ${parts.join(', ')}: ${msgs[0]}. Showing the last data.` }),
    el('button', { class: 'small', type: 'button', text: 'Retry', onclick: () => Usage.retry(P) }));
};

/* The two gauges follow the state on every update(); with no live reading they fall back to the summary's last statusline sample. */
Usage.paintGauges = function (P) {
  const R = P.refs;
  const rl = (P.st && P.st.usage && P.st.usage.value) || {};
  let five = Usage.reading(rl.five_hour);
  let seven = Usage.reading(rl.seven_day);
  let note = '';
  const sum = Usage.summaryOf(P);
  const lim = sum && sum.rate_limits && sum.rate_limits.claude;
  if ((!five || !seven) && lim) {
    const from = (r) => (r && typeof r.value === 'number' ? { pct: r.value, resets_at: Usage.epoch(r.meta && r.meta.resets_at), at: r.at } : null);
    const f5 = from(lim.rl_5h);
    const f7 = from(lim.rl_7d);
    const used = [];
    if (!five && f5) { five = f5; used.push(f5.at); }
    if (!seven && f7) { seven = f7; used.push(f7.at); }
    if (used.length) {
      const at = Math.max(...used.map((x) => Usage.epoch(x)));
      note = `${used.length === 2 || (!rl.five_hour && !rl.seven_day) ? 'Gauges show' : 'A gauge shows'} the last statusline sample${at ? ', ' + fmtAge(at) + ' ago' : ''}: no session is reporting live.`;
    }
  }
  R.g5.set(five);
  R.g7.set(seven);
  const none = !five && !seven;
  const text = none ? 'No 5H / 7D reading yet: the statusline reports them from the first Claude session. Start one with + session.' : note;
  R.gnote.classList.toggle('hidden', !text);
  setText(R.gnote, text);
};

/* Which of the limits section's three blocks shows: 'chart' (host and caption), 'slot' (an empty or error block, node given) or 'loading' (the host's skeleton). */
Usage.limitsShow = function (P, what, node) {
  const R = P.refs;
  R.limSlot.textContent = '';
  if (what === 'slot' && node) R.limSlot.append(node);
  R.limSlot.classList.toggle('hidden', what !== 'slot');
  R.limHost.classList.toggle('hidden', what === 'slot');
  R.limHost.classList.toggle('loading', what === 'loading');
  R.limHost.setAttribute('aria-busy', what === 'loading' ? 'true' : 'false');
  R.limCap.classList.toggle('hidden', what !== 'chart');
};

Usage.hasData = function (data, names) {
  if (!data || !Array.isArray(data.t) || data.t.length < 2 || !data.series || typeof data.series !== 'object') return false;
  return names.some((n) => Array.isArray(data.series[n]) && data.series[n].some((v) => typeof v === 'number' && Number.isFinite(v)));
};

Usage.paintLimits = function (P) {
  const c = Usage.entry(P);
  if (!c || !c.serDone || !c.sumDone) { Usage.limitsShow(P, 'loading'); return; }
  const empty = () => {
    P.sigs.limits = '';
    if (c.err.series && !c.series) { Usage.limitsShow(P, 'slot', Usage.errorBlock(P, `Could not load the limit series: ${c.err.series}`)); return; }
    Usage.limitsShow(P, 'slot', Usage.empty('No samples yet', 'The board records usage from its first hook event: start a session and the 5H and 7D history appears here.', true));
  };
  const data = c.series;
  const names = data && data.series ? Object.keys(data.series).filter((k) => /^rl_(5h|7d):/.test(k)) : [];
  if (!Usage.hasData(data, names)) { empty(); return; }
  const sum = Usage.summaryOf(P);
  const since = Usage.epoch(data.since) || data.t[0];
  const until = Usage.epoch(data.until) || data.t[data.t.length - 1];
  const marks = [];
  const resets = [];
  const seen = new Set();
  const reset = (t) => { const k = Math.round(t); if (t >= since && t <= until && !seen.has(k)) { seen.add(k); resets.push({ t, label: `resets ${Usage.clock(t)}` }); } };
  if (sum && Array.isArray(sum.episodes)) {
    for (const ep of sum.episodes) {
      const t = Usage.epoch(ep && ep.at);
      if (t >= since && t <= until) marks.push({ t, label: `${String(ep.kind || '').toLowerCase()} limit`, cls: ep.kind === '7d' ? 'mark-7d' : 'mark-5h' });
      reset(Usage.epoch(ep && ep.resets_at));
    }
  }
  for (const n of names) reset(Usage.epoch(data.meta && data.meta[n] && data.meta[n].resets_at));
  const labels = {};
  const colors = {};
  for (const n of names) { labels[n] = n.indexOf('rl_5h') === 0 ? '5H' : '7D'; colors[n] = n.indexOf('rl_5h') === 0 ? '--sig' : '--fg-2'; }
  if (!P.limDrawn) Usage.limitsShow(P, 'loading');                              // the skeleton until the first draw, then the chart stays put while it refreshes
  Promise.resolve(P.ready || (P.ready = Usage.ready())).then((ok) => {
    if (!Usage.alive(P) || Usage.entry(P) !== c) return;
    if (!ok) { P.limDrawn = false; Usage.limitsShow(P, 'slot', Usage.errorBlock(P, 'The chart library did not load.')); return; }
    try {
      Usage.limitsShow(P, P.limDrawn ? 'chart' : 'loading');                  // visible while Charts.line measures it
      Usage.need('line')(P.refs.limHost, data, { names, labels, colors, yMax: 100, thresholds: [60, 85], marks, resets, height: Usage.wide() ? 220 : 180 });
      P.limDrawn = true;
      Usage.limitsShow(P, 'chart');
    } catch (e) { console.error('ccboard usage chart', e); P.limDrawn = false; Usage.limitsShow(P, 'slot', Usage.errorBlock(P, Usage.errText(e))); }
  });
};

/* The window numbers of the range: total $, tokens and active hours (from the summary's window, else the sum of the daily bars). */
Usage.totals = function (sum, range) {
  const w = sum.windows && sum.windows[Usage.WINDOW[range]];
  const n = range === '24h' ? 1 : Usage.days(range);
  const tail = sum.daily.slice(-n);
  const add = (arr, f) => arr.reduce((a, x) => a + Usage.num(f(x)), 0);
  if (w && typeof w === 'object') {
    const agents = w.by_agent && typeof w.by_agent === 'object' ? Object.values(w.by_agent) : [];
    return { total: Usage.num(w.total), tokens: agents.length ? add(agents, (a) => a && a.tokens) : add(tail, (d) => d.tokens),
      hours: Array.isArray(w.by_project) && w.by_project.length ? add(w.by_project, (r) => r && r.hours) : add(tail, (d) => d.hours) };
  }
  return { total: add(tail, (d) => d.total), tokens: add(tail, (d) => d.tokens), hours: add(tail, (d) => d.hours) };
};

Usage.totalsText = function (sum, range) {
  const t = Usage.totals(sum, range);
  return `${Usage.usd(t.total)} · ${Usage.tok(t.tokens)} tokens · ${Usage.hours(t.hours)}`;
};

Usage.windowRows = function (sum, range) {
  const w = sum.windows && sum.windows[Usage.WINDOW[range]];
  return w && Array.isArray(w.by_project) ? w.by_project.filter((r) => r && typeof r === 'object') : [];
};

Usage.paintCost = function (P) {
  const sum = Usage.summaryOf(P);
  if (!sum) { Usage.noSummary(P, 'cost', 'No spend recorded yet', 'The board records usage from its first hook event: start a session and the bars appear here.'); return; }
  const daily = (sum.daily || []).slice(-(P.range === '30d' ? 30 : 7));          // demo mode ignores ?days=; live mode already returns exactly that many
  if (!daily.some((d) => Usage.num(d && d.total) > 0)) {
    P.sigs.cost = '';
    Usage.setBody(P, 'cost', Usage.empty(`No spend in the last ${daily.length} days`, 'Cost samples arrive with the first session the board sees. Start one, or pick a longer range.', true));
    return;
  }
  const sig = Usage.sig([P.range, P.stack, daily, sum.unpriced, sum.windows]);
  if (P.sigs.cost === sig) return;
  P.sigs.cost = sig;
  const totals = el('p', { class: 'utotals mono', 'data-totals': '', title: Usage.CAPTION_TIP[P.range], text: Usage.caption(sum, P.range) });
  const host = el('div', { class: 'chart cost-chart' });
  const unpriced = new Set();
  for (const u of (Array.isArray(sum.unpriced) ? sum.unpriced : [])) { const k = P.stack === 'agent' ? u && u.agent : u && u.project; if (k) unpriced.add(k); }
  const kids = [totals, host, el('p', { class: 'dim unote', text: `${daily.length} days, stacked by ${P.stack}` })];
  const un = Usage.windowRows(sum, P.range).find((r) => r.project === Usage.UNATTRIBUTED);
  if (P.stack === 'project' && un && Usage.num(un.total) > 0) {
    kids.push(el('p', { class: 'dim unote', 'data-note': 'unattributed', title: Usage.UNATTRIBUTED_TIP,
      text: `${Usage.UNATTRIBUTED} ${Usage.usd(un.total)} in this window: ${Usage.UNATTRIBUTED_TIP} (started by hand, workflow subagent runs, history from before the board).` }));
  }
  const n = Array.isArray(sum.unpriced) ? sum.unpriced.length : 0;
  if (n) {
    const names = [...new Set(sum.unpriced.map((u) => u && u.project).filter(Boolean))].join(', ');
    kids.push(el('p', { class: 'warn unote', 'data-note': 'unpriced', title: names ? `projects: ${names}` : null,
      text: `${n} session${n === 1 ? ' has' : 's have'} tokens but no price (hatched): the cost shown is a floor.` }));
  }
  Usage.setBody(P, 'cost', ...kids);
  Usage.need('stackedBars')(host, daily, { by: P.stack, top: 6, unpriced, hatchZero: true, hueOf: (name) => Usage.hue('project', name) });   // a project keeps the hue it has everywhere else (chipHue)   // Charts adds the legend and the tap-a-bar readout
};

/* ---------- sessions ---------- */

Usage.sessionLabel = function (s) {
  const repo = s.repo && s.repo !== 'root' && s.repo !== s.project ? `/${s.repo}` : '';
  return `${s.project || '(no project)'}${repo}`;
};

/* A project/repo name that may wrap after each '/' (a <wbr> there) before anything breaks inside a word ('internalSystem/se rver' at 390 px). */
Usage.breakable = function (text) {
  const parts = String(text).split('/');
  const out = [];
  parts.forEach((part, i) => { out.push(i < parts.length - 1 ? part + '/' : part); if (i < parts.length - 1) out.push(el('wbr')); });
  return out;
};

Usage.projectLink = function (project, text, cls) {
  const hash = Usage.projectHash(project);
  const klass = [cls, project === Usage.UNATTRIBUTED ? '' : Usage.hue('project', project)].filter(Boolean).join(' ') || null;
  if (hash) return el('a', { class: klass, href: hash, title: `Open the ${project} project`, text: text || project });
  return el('span', { class: klass, title: project === Usage.UNATTRIBUTED ? Usage.UNATTRIBUTED_TIP : null, text: text || project });
};

Usage.sessionMeta = function (s) {
  const models = (Array.isArray(s.models) ? s.models : []).map((m) => String(m));
  return `${models.length ? models.join(', ') : 'model unknown'} · ${Usage.tok(s.tokens)} tokens · ${Usage.hours(s.hours)} · ${s.key}`;
};

Usage.paintSessions = function (P) {
  const sum = Usage.summaryOf(P);
  const list = sum && Array.isArray(sum.top_sessions) ? sum.top_sessions.filter((s) => s && typeof s === 'object') : [];
  if (!sum || !list.length) { Usage.noSummary(P, 'sessions', 'No priced sessions yet', 'A session shows here once it has spent something. Start one with + session.'); return; }
  const live = Usage.liveMap(P.st);
  const sig = Usage.sig([P.range, list, Usage.liveSig(P.st)]);
  if (P.sigs.sessions === sig) return;
  P.sigs.sessions = sig;
  const tbody = el('tbody');
  const keep = new Set(list.map((s) => s.key));
  for (const [key, rec] of P.details) if (!keep.has(key)) { Usage.dropDetail(P, key, rec); }
  P.rows = new Map();
  for (const s of list) {
    const uuid = Usage.uuidOf(s.key);
    const lv = uuid ? live.get(uuid) : null;
    const openHash = lv ? Usage.sessionHash(lv.tmux) : null;
    const open = P.open.has(s.key);
    const chev = el('button', { class: 'minimal small chev srow-toggle', type: 'button', 'aria-expanded': open ? 'true' : 'false', 'data-key': s.key,
      'aria-label': `Details of ${Usage.sessionLabel(s)}`, title: 'Show the context and cost of this session', text: open ? '▾' : '▸' });
    // the name toggles the detail (the whole row does); the project has its own small link in the last cell
    const name = el('div', { class: 'srow-name' }, el('span', { class: ['srow-link', Usage.hue('project', s.project)].filter(Boolean).join(' ') }, ...Usage.breakable(Usage.sessionLabel(s))),
      el('span', { class: 'srow-models' }, ...(Array.isArray(s.models) ? s.models : []).map((m) => el('span', { class: ['bdg bdg-model mono', Usage.hue('model', m)].filter(Boolean).join(' '), title: String(m), text: Usage.model(m) }))));
    const projHash = Usage.projectHash(s.project);
    const tr = el('tr', { class: 'srow' + (open ? ' open' : ''), 'data-key': s.key, 'data-agent': s.agent || null },
      el('td', { class: 'c-chev' }, chev),
      el('td', { class: 'c-name' }, name),
      el('td', { class: 'c-num mono', text: Usage.usd(s.total) }),
      el('td', { class: 'c-num c-tok mono', text: Usage.tok(s.tokens) }),
      el('td', { class: 'c-num c-hrs mono', text: Usage.hours(s.hours) }),
      el('td', { class: 'c-act' },
        projHash ? el('a', { class: 'btn icon minimal small srow-proj', href: projHash, title: 'Open the project', 'aria-label': `Open the ${s.project} project` }, ic('git-repo')) : null,
        openHash ? el('a', { class: 'btn small srow-open', href: openHash, title: `Open the terminal of ${lv.tmux}`, text: 'Open' }) : null));
    tr.addEventListener('click', (e) => {
      const t = e && e.target;
      if (t && typeof t.closest === 'function' && t.closest('a')) return;           // a link or Open navigates; everything else on the row toggles
      Usage.toggleRow(P, s.key);
    });
    const rec = P.details.get(s.key) || Usage.makeDetail(P, s);
    rec.session = s;
    rec.live = lv || null;
    setText(rec.meta, Usage.sessionMeta(s));
    P.rows.set(s.key, { tr, chev, rec });
    tbody.append(tr, rec.tr);
    rec.tr.classList.toggle('hidden', !open);
    if (open) Usage.fillDetail(P, rec);
  }
  const head = el('tr', {}, el('th', { 'aria-label': 'Details' }), el('th', { scope: 'col', text: 'Session' }), el('th', { scope: 'col', class: 'c-num', text: '$' }),
    el('th', { scope: 'col', class: 'c-num c-tok', text: 'Tokens' }), el('th', { scope: 'col', class: 'c-num c-hrs', text: 'Hours' }), el('th', { 'aria-label': 'Open' }));
  Usage.setBody(P, 'sessions', el('div', { class: 'utable' }, el('table', { class: 'table usessions' }, el('thead', {}, head), tbody)),
    el('p', { class: 'dim unote', text: 'Tap a row for its context and cost over 24 h. Open jumps to the terminal of a session that is still live.' }));
};

Usage.toggleRow = function (P, key) {
  if (!Usage.alive(P)) return;
  if (P.open.has(key)) P.open.delete(key); else P.open.add(key);
  const row = P.rows.get(key);
  if (!row) return;
  const open = P.open.has(key);
  row.chev.setAttribute('aria-expanded', open ? 'true' : 'false');
  setText(row.chev, open ? '▾' : '▸');
  row.tr.classList.toggle('open', open);
  row.rec.tr.classList.toggle('hidden', !open);
  if (open) Usage.fillDetail(P, row.rec);
};

/* The detail row of one session: a persistent <tr> (its charts survive a table repaint) with a meta line and, for a live session, two small charts. */
Usage.makeDetail = function (P, s) {
  const meta = el('p', { class: 'dim sd-meta mono' });
  const body = el('div', { class: 'sd-body' });
  // the way on to the project, for a live session and a gone one alike (the row's own icon link is hidden on a phone: the name column needs the room)
  const links = Usage.projectHash(s.project) ? el('p', { class: 'sd-links' }, Usage.projectLink(s.project, `Open the ${s.project} project`, 'btn small')) : null;
  const tr = el('tr', { class: 'sdetail hidden', 'data-detail': s.key }, el('td', { colspan: '6' }, el('div', { class: 'sd' }, meta, body, links)));
  const rec = { key: s.key, tr, meta, body, session: s, live: null, shown: '', seq: 0, hosts: [] };
  P.details.set(s.key, rec);
  return rec;
};

Usage.dropDetail = function (P, key, rec) {
  const destroy = Usage.charts('destroy');
  if (destroy) for (const h of rec.hosts) { try { destroy(h); } catch (_) { /* already gone */ } }
  rec.seq += 1;
  rec.tr.remove();
  P.details.delete(key);
  P.open.delete(key);
};

Usage.detailFigure = function (title, host) {
  return el('figure', { class: 'sd-fig' }, el('figcaption', { class: 'uc-cap' }, el('span', { class: 'uc-cap-t', text: title })), host);
};

/* Fill an opened detail: the charts of a live session (fetched once per open and then with the 60 s refresh), or the way on for one that is gone. */
Usage.fillDetail = function (P, rec, fresh) {
  const lv = rec.live;
  const mode = lv ? 'live:' + lv.tmux : 'gone';
  if (rec.shown === mode && !fresh) return;
  const rebuild = rec.shown !== mode;
  rec.shown = mode;
  const seq = ++rec.seq;
  if (!lv) {
    const destroy = Usage.charts('destroy');
    if (destroy) for (const h of rec.hosts) { try { destroy(h); } catch (_) { /* already gone */ } }
    rec.hosts = [];
    rec.body.textContent = '';
    rec.body.append(el('div', { class: 'sd-gone' }, el('p', { class: 'dim', text: 'no live session for this id' })));
    return;
  }
  const tmux = lv.tmux;
  const names = [`ctx:${tmux}`, `scost:${tmux}`];
  let ctxHost = rec.hosts[0];
  let costHost = rec.hosts[1];
  if (rebuild || !ctxHost) {
    ctxHost = el('div', { class: 'chart sd-chart loading', 'aria-busy': 'true' });
    costHost = el('div', { class: 'chart sd-chart loading', 'aria-busy': 'true' });
    rec.hosts = [ctxHost, costHost];
    rec.body.textContent = '';
    rec.body.append(el('div', { class: 'charts-2 sd-grid' }, Usage.detailFigure('context · % of window', ctxHost), Usage.detailFigure('session cost · $', costHost)));
  }
  const path = `/api/series?series=ctx,scost&key=${encodeURIComponent(tmux)}&since=24h&points=${Math.max(120, Math.min(240, Usage.points(P)))}`;
  const stale = () => !Usage.alive(P) || rec.seq !== seq;
  Promise.all([Usage.get(path, fresh), P.ready || (P.ready = Usage.ready())]).then(([data, ok]) => {
    if (stale()) return;
    const fig = (host, name, opts, none) => {
      host.classList.remove('loading');
      host.setAttribute('aria-busy', 'false');
      if (!Usage.hasData(data, [name])) { host.textContent = ''; host.append(el('p', { class: 'dim', text: none })); return; }
      if (!ok) { host.append(Usage.errorBlock(P, 'The chart library did not load.')); return; }
      Usage.need('line')(host, data, { names: [name], labels: { [name]: opts.label }, colors: { [name]: opts.color }, yMax: opts.yMax, thresholds: opts.thresholds, fmt: opts.fmt || 'pct', height: 120 });
    };
    try {
      fig(ctxHost, names[0], { label: 'ctx', color: '--mute-blue', yMax: 100, thresholds: [60, 85] }, 'no context samples for this session in the last 24 h');
      fig(costHost, names[1], { label: 'cost', color: '--mute-teal', yMax: undefined, thresholds: [], fmt: 'usd' }, 'no cost samples for this session in the last 24 h');
    } catch (e) { console.error('ccboard usage detail', e); rec.body.textContent = ''; rec.body.append(Usage.errorBlock(P, Usage.errText(e))); rec.shown = ''; }
  }, (e) => {
    if (stale()) return;
    rec.shown = '';
    rec.body.textContent = '';
    rec.body.append(Usage.errorBlock(P, `Could not load the session series: ${Usage.errText(e)}`));
  });
};

Usage.refreshDetails = function (P, fresh) {
  for (const key of P.open) { const row = P.rows.get(key); if (row && row.rec.live) Usage.fillDetail(P, row.rec, fresh === true); }
};

/* ---------- projects ---------- */

Usage.paintProjects = function (P) {
  const sum = Usage.summaryOf(P);
  const rows = sum ? Usage.windowRows(sum, P.range) : [];
  if (!sum || !rows.length) { Usage.noSummary(P, 'projects', 'No project spend in this window', 'A project shows here once one of its sessions has spent something. Pick a longer range, or start a session.'); return; }
  const sig = Usage.sig([P.range, rows, sum.active_hours, P.showAll]);
  if (P.sigs.projects === sig) return;
  P.sigs.projects = sig;
  const active = sum.active_hours && typeof sum.active_hours === 'object' ? sum.active_hours : {};
  const un = rows.find((r) => r.project === Usage.UNATTRIBUTED);
  const named = rows.filter((r) => r.project !== Usage.UNATTRIBUTED).sort((a, b) => Usage.num(b.total) - Usage.num(a.total));
  const shown = P.showAll ? named : named.slice(0, Usage.PROJECT_ROWS);
  const tbody = el('tbody');
  const hrsOf = (r) => (typeof r.hours === 'number' ? r.hours : Usage.num(active[r.project]));
  const row = (r, isUn) => {
    const hrs = hrsOf(r);
    const rate = hrs >= 0.05 ? Usage.usd(Usage.num(r.total) / hrs) + '/h' : '–';
    const label = isUn ? el('span', { class: 'p-name', 'data-unattributed': '', title: Usage.UNATTRIBUTED_TIP, text: Usage.UNATTRIBUTED }) : Usage.projectLink(r.project, null, 'p-name');
    return el('tr', { class: 'prow' + (isUn ? ' unattributed' : ''), 'data-project': r.project, title: isUn ? Usage.UNATTRIBUTED_TIP : null },
      el('td', { class: 'c-name' }, label, isUn ? el('span', { class: 'dim p-tip', text: Usage.UNATTRIBUTED_TIP }) : null),
      el('td', { class: 'c-num mono', text: Usage.usd(r.total) }),
      el('td', { class: 'c-num c-hrs mono', text: Usage.hours(hrs) }),
      el('td', { class: 'c-num c-rate mono', text: rate }));
  };
  for (const r of shown) tbody.append(row(r, false));
  if (un) tbody.append(row(un, true));
  const head = el('tr', {}, el('th', { scope: 'col', text: 'Project' }), el('th', { scope: 'col', class: 'c-num', text: '$' }),
    el('th', { scope: 'col', class: 'c-num c-hrs', text: 'Active' }), el('th', { scope: 'col', class: 'c-num c-rate', text: '$/h' }));
  const more = named.length - Usage.PROJECT_ROWS;
  Usage.setBody(P, 'projects', el('div', { class: 'utable' }, el('table', { class: 'table uprojects' }, el('thead', {}, head), tbody)),
    more > 0 ? el('button', { class: 'small', type: 'button', text: P.showAll ? 'Show fewer' : `Show ${more} more`, onclick: () => { P.showAll = !P.showAll; P.sigs.projects = ''; Usage.safe(P, 'projects', () => Usage.paintProjects(P)); } }) : null,
    el('p', { class: 'dim unote', text: `${Usage.WINDOW_NAME[P.range]}. Active hours count agent time from state events, not wall clock: $/h differs a lot between projects.` }));
};

/* ---------- activity and timeline ---------- */

Usage.paintActivity = function (P) {
  const sum = Usage.summaryOf(P);
  const grid = sum && Array.isArray(sum.heatmap) ? sum.heatmap : [];
  const hourly = sum && Array.isArray(sum.hourly_profile) ? sum.hourly_profile : [];
  const any = grid.some((row) => Array.isArray(row) && row.some((v) => Usage.num(v) > 0)) || hourly.some((v) => Usage.num(v) > 0);
  const tz = Usage.tzLabel(sum && typeof sum.tz_min === 'number' ? sum.tz_min : Usage.tzMin());
  if (!sum || !any) { Usage.noSummary(P, 'activity', 'No hook events in this window', 'Hook events arrive with the first session: start one with + session and the heatmap fills in.'); return; }
  const sig = Usage.sig([P.range, grid, hourly, tz]);
  if (P.sigs.activity === sig) return;
  P.sigs.activity = sig;
  const host = el('div', { class: 'chart heat-chart' });
  Usage.setBody(P, 'activity', host);
  Usage.need('heatmap')(host, grid, hourly, { tzLabel: tz });
};

Usage.paintTimeline = function (P) {
  const c = Usage.entry(P);
  const hours = P.evHours || (c && c.hours) || 24;
  setText(P.refs.prov.timeline, `session state events, last ${hours} h`);
  if (!P.evDone) { Usage.setBody(P, 'timeline', Usage.skeleton()); return; }
  const events = P.events && Array.isArray(P.events.events) ? P.events.events.filter((e) => e && typeof e === 'object' && e.key) : [];
  if (P.evErr && !P.events) { P.sigs.timeline = ''; Usage.setBody(P, 'timeline', Usage.errorBlock(P, `Could not load the timeline: ${P.evErr}`)); return; }
  if (!events.length) {
    P.sigs.timeline = '';
    Usage.setBody(P, 'timeline', Usage.empty(`No state changes in the last ${hours} h`, 'Start a session and its timeline appears here: one row per session, coloured by state.', true));
    return;
  }
  const until = Math.floor(Date.now() / 1000);
  const since = until - hours * 3600;
  const sig = Usage.sig([hours, P.events.truncated, events]);
  if (P.sigs.timeline === sig && P.timelineUntil && until - P.timelineUntil < 30) return;
  P.sigs.timeline = sig;
  P.timelineUntil = until;
  const host = el('div', { class: 'chart gantt-chart' });
  Usage.setBody(P, 'timeline', host);
  Usage.need('gantt')(host, events, { since, until, maxRows: Usage.GANTT_ROWS, truncated: !!P.events.truncated, rowHeight: Usage.coarse() ? 24 : 18 });   // Charts says "N more" and that the server cut the list; a tap on a row names it in full
};

/* ---------- the page ---------- */

Usage.teardown = function (P) {
  P.dead = true;
  P.token += 1;
  if (P.timer !== null && typeof clearInterval === 'function') clearInterval(P.timer);
  P.timer = null;
  if (P.onVisible && typeof document !== 'undefined') document.removeEventListener('visibilitychange', P.onVisible);
  P.onVisible = null;
  if (typeof P.unbind === 'function') { try { P.unbind(); } catch (_) { /* already unbound */ } }
  P.unbind = null;
  const destroyAll = Usage.charts('destroyAll');
  if (destroyAll) { try { destroyAll(); } catch (e) { console.error('ccboard usage destroyAll', e); } }
  if (Usage.cur === P) Usage.cur = null;
};

registerPage('usage', {
  title: () => 'Usage',
  mount(root, route) {
    if (Usage.cur) Usage.teardown(Usage.cur);
    const P = Usage.build(root, route);
    Usage.cur = P;
    P.ready = Usage.ready();                               // uPlot loads while the fetches run
    Usage.load(P);
    // every minute: refetch while the tab is visible (a hidden tab refetches when it is shown again) and let the countdowns move
    if (typeof setInterval === 'function') {
      P.timer = setInterval(() => {
        if (!Usage.alive(P)) return;
        if (Usage.hidden()) { P.stale = true; return; }
        Usage.paintGauges(P);
        Usage.load(P, { fresh: true });
      }, Usage.REFRESH_MS);
      if (P.timer && typeof P.timer.unref === 'function') P.timer.unref();
    }
    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
      P.onVisible = () => { if (Usage.alive(P) && !Usage.hidden() && P.stale) { P.stale = false; Usage.paintGauges(P); Usage.load(P, { fresh: true }); } };
      document.addEventListener('visibilitychange', P.onVisible);
    }
    if (typeof Keymap !== 'undefined' && Keymap && typeof Keymap.bindKey === 'function') {
      P.unbind = Keymap.bindKey('r', () => Usage.cycle(P), { when: () => Usage.alive(P) && typeof currentRoute === 'function' && !!currentRoute() && currentRoute().id === 'usage', help: 'Cycle the Usage range (24h, 7d, 30d)', group: 'Usage' });
    }
  },
  update(st) {
    const P = Usage.cur;
    if (!P || !st) return;
    P.st = st;
    Usage.paintGauges(P);
    const sig = Usage.liveSig(st);
    if (sig !== P.liveSig) {                               // a session started or ended: the Open buttons follow, without a fetch
      P.liveSig = sig;
      if (Usage.summaryOf(P)) Usage.safe(P, 'sessions', () => Usage.paintSessions(P));
    }
  },
  onRoute(route) {
    const P = Usage.cur;
    if (!P) return;
    P.route = route;
    const r = Usage.routeRange(route);
    if (r) { Usage.saveRange(r); if (r !== P.range) Usage.applyRange(P, r); }
  },
  unmount() {
    const P = Usage.cur;
    if (P) Usage.teardown(P);
  },
});
