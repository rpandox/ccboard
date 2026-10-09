/* ccboard charts (v0.5.17): the Usage page's drawing kit. uPlot (vendored, loaded lazily) draws the time series; the bars, the Gantt, the heatmap
   and the sparkline are hand-rolled SVG / CSS grid through el() and svg(). Definition only at load: nothing here touches the DOM, storage, the
   network, a listener or a timer until a function is called. Classic script, one namespace (Charts), depends on core.js only (el, svg, api,
   loadAsset), loaded after tree.js and before shell.js. Colours are CSS variables (charts.css); only uPlot's canvas needs resolved strings
   (Charts.css reads them with getComputedStyle). Text is always written with textContent / el() `text:`, never as markup.

   Data
     Charts.ready()              Promise: uPlot's css + js, loaded once, resolved at once when window.uPlot exists. A failed load can be retried.
     Charts.fetch(path, {force}) Promise<body>: GET through api(), cached by the full path (30 s when since=1h|6h|24h, else 300 s), in-flight dedupe,
                                 an error is never cached. Charts.cached(path[, stale]) is the synchronous read of that cache (data | null) so a
                                 range toggle can paint at once. Charts.clear([path]) empties it. Charts.now() is the clock (tests replace it).
   Drawing (one chart per host element; calling again with the same host updates it)
     Charts.line(host, data, opts)         uPlot lines from a /api/series body -> the uPlot instance (host._uplot) | null when there is nothing to draw
     Charts.stackedBars(host, days, opts)  SVG stacked cost bars + legend + a readout line -> {rows, keys, svg, legend}
     Charts.gantt(host, events, opts)      SVG session timeline from /api/series/events -> {rows, truncated, svg, legend}
     Charts.heatmap(host, grid, hourly, o) 7x24 CSS grid + the hour-of-day profile -> the grid node
     Charts.spark(host, t, vals, opts)     one small polyline svg (same class names as Widgets.sparkline) -> the svg | null
     Charts.destroy(host) / destroyAll()   uPlot.destroy(), ResizeObserver.disconnect(), host emptied; destroyAll covers every host, unmount calls it
     Charts.pause() / resume()             a hidden tab skips redraws; resume() applies the latest data and size once (visibilitychange is wired
                                           the first time a chart is drawn, and unwired when the last one is destroyed)
   Pure geometry for tests: Charts.geom.{rank, stack, scale, spans, hourTicks, level, dayLabel}. Formatters: fmtUsd, fmtTok, shortModel, fmtTz. */
'use strict';

const Charts = {
  UPLOT_CSS: '/static/vendor/uplot/uPlot.min.css',
  UPLOT_JS: '/static/vendor/uplot/uPlot.iife.min.js',
  TTL_SHORT: 30000,                 // a since=1h|6h|24h answer is reused for 30 s, anything longer for 5 min
  TTL_LONG: 300000,
  CACHE_MAX: 64,
  MAX_RECTS: 240,                   // the stacked bars never draw more rects than this (30 days x (6 + other + unattributed) = 240)
  LINE_COLORS: ['--sig', '--seg-1', '--seg-2', '--seg-3', '--seg-4', '--seg-5'],   // a line's colour when opts.colors names none
  UNATTRIBUTED: '(unattributed)',
  UNATTRIBUTED_TIP: 'sessions the board did not start, with no folder on record',
  OUTSIDE: '(outside projects)',
  OUTSIDE_TIP: 'sessions that ran in a folder outside the projects folder',
  STATES: ['idle', 'working', 'waiting', 'done', 'errored', 'ended'],     // /api/series/events state v: 0..5
  WEEKDAYS: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],             // the heatmap's rows (weekday Mon=0)
  MONTHS: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
  EMPTY_LINE: 'No samples in this window yet: the board records usage from its first hook event.',
  EMPTY_BARS: 'No daily cost yet: it appears once a session has spent something.',
  EMPTY_GANTT: 'No session activity in this window: rows appear as sessions start and change state.',
  EMPTY_HEAT: 'No hook events recorded yet: start a session and this fills in.',
  paused: false,
  now: () => Date.now(),
  geom: {},
  /* the colour a token resolves to when getComputedStyle is not there (tests, a stylesheet that has not loaded yet) */
  FALLBACK: {
    '--sig': '#4dd0e1', '--ok': '#32a467', '--warn': '#ec9a3c', '--bad': '#e76a6e', '--dim': '#abb3bf', '--dim-2': '#738091', '--fg-2': '#c5cbd3',
    '--line': '#404854', '--line-soft': '#2d333b', '--card': '#252a31',
    // the muted category hues (tokens.css): the project / agent series and the chart lines that are not a state
    '--mute-blue': '#8db4e8', '--mute-teal': '#6cc4b8', '--mute-green': '#84c096', '--mute-violet': '#b9a9ee', '--mute-slate': '#adb7c4',
    '--seg-0': '#8db4e8', '--seg-1': '#6cc4b8', '--seg-2': '#84c096', '--seg-3': '#b9a9ee', '--seg-4': '#adb7c4', '--seg-5': '#a3b0e6',
    '--font-mono': 'ui-monospace, Menlo, monospace',
  },
  _cache: new Map(),
  _flight: new Map(),
  _hosts: new Set(),
  _ready: null,
  _vis: null,
};

/* ---------- small helpers ---------- */

Charts.num = function (v) { return typeof v === 'number' && Number.isFinite(v) ? v : null; };

/* Epoch seconds from a number (ms are folded to s), a numeric string or an ISO string; null when it is none of them. */
Charts.epoch = function (v) {
  if (typeof v === 'number') return Number.isFinite(v) ? (v > 1e11 ? v / 1000 : v) : null;
  if (typeof v === 'string' && v.trim()) { const n = Number(v); if (Number.isFinite(n)) return Charts.epoch(n); const t = Date.parse(v); return Number.isNaN(t) ? null : t / 1000; }
  return null;
};

/* A colour string for uPlot's canvas: '--sig' | 'var(--sig)' read from :root, or a literal colour passed through. */
Charts.css = function (name, fallback) {
  const raw = String(name || '').trim();
  const m = /^var\(\s*(--[\w-]+)/.exec(raw);
  const key = m ? m[1] : raw;
  if (/^--[\w-]+$/.test(key)) {
    let v = '';
    try {
      if (typeof getComputedStyle === 'function' && typeof document !== 'undefined' && document.documentElement) v = String(getComputedStyle(document.documentElement).getPropertyValue(key) || '').trim();
    } catch (_) { /* no computed style */ }
    return v || fallback || (Object.prototype.hasOwnProperty.call(Charts.FALLBACK, key) ? Charts.FALLBACK[key] : '#8a97a8');
  }
  return raw || fallback || '#8a97a8';
};

/* The value a swatch can use without resolving anything: var(--x) for a token, the literal otherwise. */
Charts.cssRef = function (name) {
  const raw = String(name || '').trim();
  if (/^--[\w-]+$/.test(raw)) return `var(${raw})`;
  return raw;
};

Charts.pad2 = function (n) { return String(n).padStart(2, '0'); };

Charts.clock = function (epoch) {
  const d = new Date(epoch * 1000);
  return `${Charts.pad2(d.getHours())}:${Charts.pad2(d.getMinutes())}`;
};

/* Local 'Tue 21:40' (the weekday only when the window is wide, so a 24 h chart says just '21:40'). */
Charts.when = function (epoch, wide) {
  const d = new Date(epoch * 1000);
  const wd = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()];
  return (wide ? wd + ' ' : '') + Charts.clock(epoch);
};

Charts.dur = function (s) {
  const m = Math.round(Math.max(0, s) / 60);
  if (m < 1) return '<1m';
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h}h${m % 60}m` : `${h}h`;
};

/* ---------- formatters ---------- */

/* '$17.15', '$69' from 100 up (the same as Widgets.money, so Home and Usage never disagree); '—' for what is not a number. */
Charts.fmtUsd = function (v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const a = Math.abs(n);
  return (n < 0 ? '-$' : '$') + (a >= 100 ? String(Math.round(a)) : a.toFixed(2));
};

/* 812, 1.2k, 48.7M, 1.2B: one decimal, no trailing '.0', and never '1000k'. */
Charts.fmtTok = function (n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '0';
  if (v < 1000) return String(Math.round(v));
  const units = ['k', 'M', 'B'];
  let x = v / 1000;
  let i = 0;
  let s = x.toFixed(1);
  while (parseFloat(s) >= 1000 && i < units.length - 1) { x /= 1000; i++; s = x.toFixed(1); }
  return s.replace(/\.0$/, '') + units[i];
};

/* 'claude-opus-5-5' -> 'opus-5-5'; a trailing build date goes too ('claude-3-5-sonnet-20241022' -> '3-5-sonnet'). */
Charts.shortModel = function (model) {
  if (typeof model !== 'string' || !model) return '';
  return model.replace(/^claude-/, '').replace(/-\d{8}$/, '');
};

/* Minutes east of UTC -> 'UTC+5:45', 'UTC-5', 'UTC'. */
Charts.fmtTz = function (min) {
  const m = Number(min);
  if (!Number.isFinite(m) || m === 0) return 'UTC';
  const a = Math.abs(Math.round(m));
  const h = Math.floor(a / 60);
  const r = a % 60;
  return `UTC${m < 0 ? '-' : '+'}${h}${r ? ':' + Charts.pad2(r) : ''}`;
};

Charts.fmtAxis = function (v, kind) {
  if (!Number.isFinite(v)) return '';
  if (kind === 'pct') return `${Math.round(v)}%`;
  if (kind === 'usd') {
    if (v === 0) return '$0';
    if (v >= 1000) return '$' + (v / 1000).toFixed(v % 1000 === 0 ? 0 : 1) + 'k';
    if (Number.isInteger(v)) return '$' + v;
    return '$' + v.toFixed(2);                                     // money has two decimals under $100, never one (fmtUsd is the rule; the axis only drops '.00' on whole dollars)
  }
  return String(+v.toFixed(2));
};

/* A line's value in the legend chips: a dollar value is fmtUsd (the same '$3.40' as the tables), everything else reads like its axis. */
Charts._fmtLegend = function (kind) { return kind === 'usd' ? (v) => Charts.fmtUsd(v) : Charts._fmtFor(kind); };

/* ---------- uPlot loader ---------- */

/* loadAsset() memoises a rejected load for good; drop that memo so a Retry button can try again. */
Charts.loadOne = function (path) {
  return loadAsset(path).catch((err) => {
    try { if (typeof assetLoading !== 'undefined') delete assetLoading[path]; } catch (_) { /* nothing to forget */ }
    throw err;
  });
};

Charts.ready = function () {
  if (typeof window !== 'undefined' && window.uPlot) return Promise.resolve();
  if (Charts._ready) return Charts._ready;
  const p = Promise.resolve()
    .then(() => Promise.all([Charts.loadOne(Charts.UPLOT_CSS), Charts.loadOne(Charts.UPLOT_JS)]))
    .then(() => { if (!window.uPlot) throw new Error('uPlot did not load'); });
  Charts._ready = p.catch((err) => { Charts._ready = null; throw err; });
  return Charts._ready;
};

/* ---------- fetch cache ---------- */

Charts.ttl = function (path) {
  return /[?&]since=(?:1h|6h|24h)(?:&|$)/.test(String(path)) ? Charts.TTL_SHORT : Charts.TTL_LONG;
};

/* The cached body of a path: while it is fresh, or at any age with stale=true; null when there is none. */
Charts.cached = function (path, stale) {
  const hit = Charts._cache.get(String(path));
  if (!hit) return null;
  return stale || Charts.now() - hit.at < Charts.ttl(path) ? hit.data : null;
};

Charts.clear = function (path) {
  if (path === undefined) { Charts._cache.clear(); return; }
  Charts._cache.delete(String(path));
};

Charts.fetch = function (path, opts) {
  const key = String(path);
  if (!(opts && opts.force)) {
    const fresh = Charts.cached(key);
    if (fresh !== null) return Promise.resolve(fresh);
  }
  const flying = Charts._flight.get(key);
  if (flying) return flying;
  let call;
  try { call = Promise.resolve(api('GET', key)); } catch (err) { call = Promise.reject(err); }
  const p = call.then((data) => {
    const body = data === null || data === undefined ? {} : data;
    Charts._cache.delete(key);
    Charts._cache.set(key, { at: Charts.now(), data: body });
    while (Charts._cache.size > Charts.CACHE_MAX) Charts._cache.delete(Charts._cache.keys().next().value);
    return body;
  }).finally(() => { if (Charts._flight.get(key) === p) Charts._flight.delete(key); });
  Charts._flight.set(key, p);
  return p;
};

/* ---------- hosts: registry, resize, visibility ---------- */

Charts._watch = function () {
  if (Charts._vis || typeof document === 'undefined' || typeof document.addEventListener !== 'function') return;
  Charts._vis = () => { if (document.hidden) Charts.pause(); else Charts.resume(); };
  document.addEventListener('visibilitychange', Charts._vis);
};

Charts._unwatch = function () {
  if (!Charts._vis || Charts._hosts.size) return;
  try { document.removeEventListener('visibilitychange', Charts._vis); } catch (_) { /* no document */ }
  Charts._vis = null;
};

Charts.pause = function () { Charts.paused = true; };

Charts.resume = function () {
  Charts.paused = false;
  for (const host of [...Charts._hosts]) Charts._flush(host);
};

/* What a paused chart held back (a newer dataset, a new size, a redraw) is applied once. */
Charts._flush = function (host) {
  const st = host._chart;
  if (!st || !st.pending) return;
  const p = st.pending;
  st.pending = null;
  if (st.kind === 'line') {
    const u = host._uplot;
    if (!u) return;
    if (p.size) u.setSize({ width: p.size, height: st.h });
    if (p.data) { u.setData(Charts._lineData(st.model)); Charts._legendPaint(st, null); }
    else if (p.redraw) u.redraw();
  } else if (typeof st.redo === 'function') st.redo();
};

Charts._width = function (host) { return Math.round(Number(host.clientWidth) || 0); };

/* One ResizeObserver per host; onSize(width) runs when the width really changed (the observer also fires once on observe). */
Charts._observe = function (host, onSize) {
  if (typeof ResizeObserver === 'undefined') return null;
  let last = Charts._width(host);
  const ro = new ResizeObserver(() => {
    const w = Charts._width(host);
    if (!w || w === last) return;
    last = w;
    onSize(w);
  });
  ro.observe(host);
  return ro;
};

Charts._take = function (host, kind) {
  let st = host._chart;
  if (!st || st.kind !== kind) {
    Charts.destroy(host);
    st = host._chart = { kind, pending: null, ro: null, redo: null };
  }
  Charts._hosts.add(host);
  Charts._watch();
  host.classList.add('chart', 'chart-' + kind);
  host.classList.remove('loading');
  return st;
};

Charts.destroy = function (host) {
  if (!host) return;
  const st = host._chart;
  if (st && st.ro) { try { st.ro.disconnect(); } catch (_) { /* already gone */ } }
  const u = host._uplot;
  if (u) { try { u.destroy(); } catch (_) { /* already gone */ } }
  if (st) host.classList.remove('chart-' + st.kind);
  if (st || u) host.textContent = '';
  delete host._uplot;
  delete host._chart;
  Charts._hosts.delete(host);
  Charts._unwatch();
};

Charts.destroyAll = function () {
  for (const host of [...Charts._hosts]) Charts.destroy(host);
};

Charts.empty = function (host, text) {
  host.classList.add('chart');
  host.classList.remove('loading');
  host.textContent = '';
  host.append(el('div', { class: 'chart-empty', role: 'status', text }));
};

/* The legend: [{cls, label, v, tip, color}] -> div.legend of chips (swatch span carrying the segment class, label, optional value). */
Charts.legendNode = function (items) {
  const box = el('div', { class: 'legend' });
  for (const it of items) {
    const sw = el('span', { class: 'sw' + (it.cls ? ' ' + it.cls : ''), 'aria-hidden': 'true' });
    if (it.color) sw.style.background = it.color;
    const chip = el('span', { class: 'lg' + (it.hatch ? ' hatch' : ''), title: it.tip || it.label }, sw, el('span', { class: 'lg-l', text: it.label }));
    if (it.v !== undefined && it.v !== null && it.v !== '') chip.append(el('span', { class: 'lg-v mono', text: String(it.v) }));
    box.append(chip);
  }
  return box;
};

/* ---------- line chart (uPlot) ---------- */

/* The model behind one line chart: strictly increasing finite t, the requested series padded / trimmed to it, null for a hole. */
Charts._lineModel = function (data, o) {
  const body = data && typeof data === 'object' ? data : {};
  const raw = Array.isArray(body.t) ? body.t : [];
  const series = body.series && typeof body.series === 'object' ? body.series : {};
  const idx = [];
  const t = [];
  raw.forEach((x, i) => {
    const n = Charts.num(Number(x));
    if (n !== null && (t.length === 0 || n > t[t.length - 1])) { t.push(n); idx.push(i); }
  });
  const want = Array.isArray(o.names) && o.names.length ? o.names : Object.keys(series);
  const names = want.filter((n) => Array.isArray(series[n]));
  const cols = names.map((n) => idx.map((i) => Charts.num(series[n][i])));
  const known = cols.some((c) => c.some((v) => v !== null));
  const since = Charts.epoch(body.since);
  const until = Charts.epoch(body.until);
  return { t, names, cols, empty: !t.length || !names.length || !known, since, until };
};

Charts._lineData = function (model) { return [model.t, ...model.cols]; };

Charts._lineSig = function (model, o) {
  return [model.names.join('|'), (o.right || []).join('|'), o.height || '', o.legend === false ? 'n' : 'y'].join('#');
};

Charts._overlaySig = function (o) {
  return JSON.stringify([o.yMax, o.thresholds || [], o.marks || [], o.resets || []]);
};

/* x range: the whole requested window when the body says it (so a 7 d chart with two days of samples shows the empty five), never less than the samples. */
Charts._xRange = function (model) {
  const first = model.t[0];
  const last = model.t[model.t.length - 1];
  let lo = model.since === null ? first : Math.min(model.since, first);
  let hi = model.until === null ? last : Math.max(model.until, last);
  if (!(hi > lo)) hi = lo + 1800;
  return [lo, hi];
};

Charts._fmtFor = function (kind) { return typeof kind === 'function' ? kind : (v) => Charts.fmtAxis(v, kind || 'num'); };

Charts._legendPaint = function (st, idx) {
  const u = st.legend;
  if (!u) return;
  const m = st.model;
  const wide = m.t.length > 1 && m.t[m.t.length - 1] - m.t[0] > 36 * 3600;
  m.cols.forEach((col, j) => {
    let v = null;
    if (idx !== null && idx !== undefined) v = col[idx];
    else for (let i = col.length - 1; i >= 0; i--) if (col[i] !== null) { v = col[i]; break; }
    const text = v === null || v === undefined ? '–' : st.fmts[j](v);
    if (u.vals[j].textContent !== text) u.vals[j].textContent = text;
  });
  const tt = idx !== null && idx !== undefined && m.t[idx] !== undefined ? Charts.when(m.t[idx], wide) : '';
  if (u.time.textContent !== tt) u.time.textContent = tt;
};

/* thresholds, episode marks and reset ticks, drawn on the canvas after uPlot paints (a hook, not CSS). A bad entry never blanks the chart. */
Charts._overlay = function (u, st) {
  try {
    const o = st.opts;
    const ctx = u.ctx;
    const b = u.bbox;
    if (!ctx || !b) return;
    const px = u.pxRatio || (typeof window !== 'undefined' && window.uPlot && window.uPlot.pxRatio) || (typeof devicePixelRatio === 'number' && devicePixelRatio) || 1;   // uPlot 1.6.32 keeps pxRatio on the class, not the instance
    const x0 = b.left;
    const x1 = b.left + b.width;
    const y1 = b.top + b.height;
    ctx.save();
    ctx.beginPath();
    ctx.rect(b.left, b.top, b.width, b.height);
    ctx.clip();
    ctx.lineWidth = Math.max(1, Math.round(px));
    ctx.font = `${Math.round(10 * px)}px ${st.mono}`;
    const ys = u.scales && u.scales.y;
    for (const th of o.thresholds || []) {
      const spec = typeof th === 'number' ? { v: th } : th;
      const v = Charts.num(spec && spec.v);
      if (v === null || (ys && (v < ys.min || v > ys.max))) continue;
      const y = Math.round(u.valToPos(v, 'y', true));
      const col = Charts.css(spec.cls ? '--' + spec.cls : v >= 85 ? '--bad' : '--warn');
      ctx.setLineDash([4 * px, 4 * px]);
      ctx.strokeStyle = col;
      ctx.fillStyle = col;
      ctx.globalAlpha = 0.6;
      ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x1, y); ctx.stroke();
      ctx.globalAlpha = 0.9;
      ctx.textAlign = 'right'; ctx.textBaseline = 'bottom';
      ctx.fillText(spec.label || `${v}%`, x1 - 3 * px, y - 2 * px);
    }
    ctx.setLineDash([3 * px, 3 * px]);
    let lastRight = -Infinity;
    const marks = (o.marks || []).filter((m) => m && Charts.num(Number(m.t)) !== null).sort((a, c) => a.t - c.t);
    for (const m of marks) {
      const x = Math.round(u.valToPos(Number(m.t), 'x', true));
      if (x < x0 || x > x1) continue;
      const col = Charts.css(m.cls ? '--' + m.cls : '--bad');
      ctx.strokeStyle = col; ctx.fillStyle = col; ctx.globalAlpha = 0.85;
      ctx.beginPath(); ctx.moveTo(x, b.top); ctx.lineTo(x, y1); ctx.stroke();
      if (m.label) {
        const w = ctx.measureText(String(m.label)).width;
        const right = x + 4 * px + w > x1;
        const from = right ? x - 4 * px - w : x + 4 * px;
        if (from >= lastRight + 2 * px) {
          ctx.textAlign = 'left'; ctx.textBaseline = 'top';
          ctx.fillText(String(m.label), from, b.top + 3 * px);
          lastRight = from + w;
        }
      }
    }
    ctx.setLineDash([]);
    const resets = (o.resets || []).filter((r) => r && Charts.num(Number(r.t)) !== null);
    ctx.strokeStyle = Charts.css('--dim'); ctx.fillStyle = Charts.css('--dim'); ctx.globalAlpha = 0.9;
    ctx.lineWidth = Math.max(1, Math.round(1.5 * px));
    for (const r of resets) {
      const x = Math.round(u.valToPos(Number(r.t), 'x', true));
      if (x < x0 || x > x1) continue;
      ctx.beginPath(); ctx.moveTo(x, y1 - 7 * px); ctx.lineTo(x, y1); ctx.stroke();
      if (r.label && resets.length <= 4) {
        ctx.textAlign = x > x1 - 40 * px ? 'right' : 'left'; ctx.textBaseline = 'bottom';
        ctx.fillText(String(r.label), x + (ctx.textAlign === 'right' ? -3 : 3) * px, y1 - 8 * px);
      }
    }
    ctx.restore();
  } catch (err) {
    try { u.ctx.restore(); } catch (_) { /* nothing was saved */ }
    if (typeof console !== 'undefined') console.error('ccboard charts overlay', err);
  }
};

Charts._lineOpts = function (host, model, o, st, width) {
  const mono = Charts.css('--font-mono');
  const font = `11px ${mono}`;
  const dim = Charts.css('--dim');
  const grid = { stroke: Charts.css('--line-soft'), width: 1 };
  const ticks = { stroke: Charts.css('--line'), width: 1, size: 4 };
  const right = new Set(o.right || []);
  const hasRight = model.names.some((n) => right.has(n));
  const fmtL = Charts._fmtFor(o.fmt || 'pct');
  const fmtR = Charts._fmtFor(o.rightFmt || 'usd');
  st.mono = mono;
  const legL = Charts._fmtLegend(o.fmt || 'pct');
  const legR = Charts._fmtLegend(o.rightFmt || 'usd');
  st.fmts = model.names.map((n) => (right.has(n) ? legR : legL));
  const axes = [
    { stroke: dim, font, size: 40, grid, ticks },                                   // uPlot's two-line time labels (time over date) need 40 px
    { scale: 'y', stroke: dim, font, size: 42, grid, ticks, values: (_u, vals) => vals.map((v) => fmtL(v)) },
  ];
  if (hasRight) axes.push({ scale: 'y2', side: 1, stroke: dim, font, size: 50, grid: { show: false }, ticks, values: (_u, vals) => vals.map((v) => fmtR(v)) });
  const auto = () => (_u, _min, max) => [0, max > 0 ? max * 1.1 : 1];
  const scales = {
    x: { time: true, range: () => st.range },
    y: { range: typeof o.yMax === 'number' ? () => [0, st.opts.yMax || o.yMax] : auto() },
  };
  if (hasRight) scales.y2 = { range: auto() };
  const series = [{}].concat(model.names.map((n, i) => {
    const color = Charts.css((o.colors && o.colors[n]) || Charts.LINE_COLORS[i % Charts.LINE_COLORS.length]);
    return { label: (o.labels && o.labels[n]) || n.split(':')[0], scale: right.has(n) ? 'y2' : 'y', stroke: color, width: 2, spanGaps: false, points: { size: 5, fill: color, stroke: color, width: 1 } };
  }));
  return {
    width, height: st.h, padding: [8, 8, 0, 0], legend: { show: false }, select: { show: false },
    cursor: { drag: { x: false, y: false, setScale: false }, points: { size: 7 } },
    scales, axes, series,
    hooks: { draw: [(u) => Charts._overlay(u, st)], setCursor: [(u) => Charts._legendPaint(st, u.cursor && typeof u.cursor.idx === 'number' ? u.cursor.idx : null)] },
  };
};

/* data: a /api/series body. opts: {names:['rl_5h:claude', ...] (default: every series), labels:{name: text}, colors:{name: '--css-var'},
   yMax (left axis 0..yMax; default auto), thresholds:[60, 85] | [{v, label, cls}], marks:[{t, label, cls:'bad'|'warn'|'ok'|'sig'|'dim'}],
   resets:[{t, label}], height (default 180), legend:false (no chips), empty (text when there is nothing to draw),
   right:[names on a second, right-hand axis], fmt / rightFmt ('pct' | 'usd' | 'num' | function; defaults pct and usd)}.
   Returns the uPlot instance (also host._uplot), or null when there is nothing to draw (an empty-state text is shown instead). */
Charts.line = function (host, data, opts) {
  const o = opts || {};
  const model = Charts._lineModel(data, o);
  if (model.empty) {
    Charts.destroy(host);
    Charts.empty(host, o.empty || Charts.EMPTY_LINE);
    return null;
  }
  const prev = host._chart;
  const sig = Charts._lineSig(model, o);
  const stamp = `${model.t.length}:${model.t[model.t.length - 1]}`;
  const osig = Charts._overlaySig(o);
  if (prev && prev.kind === 'line' && host._uplot && prev.sig === sig) {
    const u = host._uplot;
    const moved = prev.stamp !== stamp;
    const marked = prev.osig !== osig;
    prev.opts = o; prev.model = model; prev.range = Charts._xRange(model); prev.stamp = stamp; prev.osig = osig;
    if (moved || marked) {
      if (Charts.paused) prev.pending = Object.assign(prev.pending || {}, moved ? { data: true } : { redraw: true });
      else if (moved) { u.setData(Charts._lineData(model)); Charts._legendPaint(prev, null); }
      else u.redraw();
    }
    return u;
  }
  const U = typeof window !== 'undefined' ? window.uPlot : undefined;
  if (typeof U !== 'function') throw new Error('Charts.line needs uPlot: await Charts.ready() first');
  Charts.destroy(host);                 // a different series set (or a host that held another chart) starts clean
  const st = Charts._take(host, 'line');
  host.textContent = '';                 // an empty-state text or a skeleton goes
  Object.assign(st, { sig, stamp, osig, opts: o, model, h: Math.max(80, Number(o.height) || 180), range: Charts._xRange(model), legend: null });
  const plot = el('div', { class: 'chart-plot' });
  host.append(plot);
  const width = Charts._width(host) || 320;
  const uopts = Charts._lineOpts(host, model, o, st, width);
  if (o.legend !== false) {
    const items = model.names.map((n, i) => ({ label: (o.labels && o.labels[n]) || n.split(':')[0], color: Charts.cssRef((o.colors && o.colors[n]) || Charts.LINE_COLORS[i % Charts.LINE_COLORS.length]), tip: n }));
    const box = Charts.legendNode(items);
    const chips = Array.from(box.querySelectorAll('.lg'));
    const vals = chips.map((chip) => { const v = el('span', { class: 'lg-v mono', text: '–' }); chip.append(v); return v; });
    const time = el('span', { class: 'lg-t mono dim' });
    box.append(time);
    host.append(box);
    st.legend = { vals, time };
  }
  const u = new U(uopts, Charts._lineData(model), plot);
  host._uplot = u;
  Charts._legendPaint(st, null);
  st.ro = Charts._observe(host, (w) => {
    if (Charts.paused) st.pending = Object.assign(st.pending || {}, { size: w });
    else u.setSize({ width: w, height: st.h });
  });
  return u;
};

/* ---------- stacked cost bars ---------- */

Charts.geom.dayLabel = function (day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day));
  if (!m) return null;
  const dt = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  return { wd: Charts.WEEKDAYS[(dt.getUTCDay() + 6) % 7], d: +m[3], mon: Charts.MONTHS[+m[2] - 1] };
};

/* One day's spend by key: {map: Map(key -> usd > 0), resid: usd the keys do not explain}. by_agent / by_project hold plain numbers per day. */
Charts.geom.dayMap = function (d, by) {
  const src = d && (by === 'agent' ? d.by_agent : d.by_project);
  const map = new Map();
  let sum = 0;
  if (src && typeof src === 'object') {
    for (const [k, v] of Object.entries(src)) {
      const n = typeof v === 'number' ? v : (v && typeof v === 'object' ? Number(v.total) : Number(v));
      if (Number.isFinite(n) && n > 0) { map.set(k, n); sum += n; }
    }
  }
  const total = Number(d && d.total);
  const resid = Number.isFinite(total) && total - sum > 0.005 ? total - sum : 0;
  return { map, resid };
};

/* The segments of the whole window in stacking order (bottom up): the `top` biggest keys by total, then 'other' (the rest and anything the
   keys do not explain), then '(unattributed)', which is always its own segment: never ranked against projects, never folded into 'other'.
   -> [{key, total, cls, members?}] with cls seg-0..5 | seg-other | seg-unattributed | agent-claude|codex|shell. */
/* Project colours that agree with the rest of the app: chipHue('project', name) (pages/agents.js) hashes a project into blue / teal / green / violet / slate, and
   --seg-0..4 are those same five muted tokens, so a series wears the segment of its own hue. Two projects can hash to one hue and a stack needs distinct colours:
   the biggest claimant keeps the hue, the others take the free segments (--seg-5 and the unclaimed hues) in rank order. segsFor(names, hueOf) -> [seg-N]. */
Charts.HUE_SEG = { 'hue-blue': 'seg-0', 'hue-teal': 'seg-1', 'hue-green': 'seg-2', 'hue-violet': 'seg-3', 'hue-slate': 'seg-4' };
Charts.geom.segsFor = function (names, hueOf) {
  const out = names.map(() => null);
  const taken = new Set();
  names.forEach((n, i) => {
    let want = null;
    try { want = Charts.HUE_SEG[hueOf(n)] || null; } catch (_) { want = null; }
    if (want && !taken.has(want)) { taken.add(want); out[i] = want; }
  });
  const free = ['seg-0', 'seg-1', 'seg-2', 'seg-3', 'seg-4', 'seg-5'].filter((c) => !taken.has(c));
  let f = 0;
  names.forEach((n, i) => { if (out[i] === null) out[i] = f < free.length ? free[f++] : 'seg-' + (i % 6); });
  return out;
};

Charts.geom.rank = function (days, by, top, hueOf) {
  const tot = new Map();
  let resid = 0;
  for (const d of days) {
    const m = Charts.geom.dayMap(d, by);
    resid += m.resid;
    for (const [k, v] of m.map) tot.set(k, (tot.get(k) || 0) + v);
  }
  const named = [...tot.entries()].filter(([k]) => k !== Charts.UNATTRIBUTED && k !== Charts.OUTSIDE).sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1));
  const cut = top === undefined || top === null ? 6 : Math.max(0, Math.floor(Number(top)) || 0);
  const head = named.slice(0, cut);
  const segs = by !== 'agent' && typeof hueOf === 'function' ? Charts.geom.segsFor(head.map(([k]) => k), hueOf) : null;
  const keys = head.map(([key, total], i) => ({ key, total, cls: by === 'agent' && /^(claude|codex|shell)$/.test(key) ? 'agent-' + key : (segs ? segs[i] : 'seg-' + (i % 6)) }));
  const rest = named.slice(cut);
  const restTotal = rest.reduce((a, [, v]) => a + v, 0) + resid;
  if (rest.length || resid > 0) keys.push({ key: 'other', total: restTotal, cls: 'seg-other', members: rest.map(([k]) => k) });
  if (by !== 'agent' && tot.has(Charts.OUTSIDE)) keys.push({ key: Charts.OUTSIDE, total: tot.get(Charts.OUTSIDE), cls: 'seg-unattributed' });     // two labels with no project of their own: dashed, never ranked
  if (tot.has(Charts.UNATTRIBUTED)) keys.push({ key: Charts.UNATTRIBUTED, total: tot.get(Charts.UNATTRIBUTED), cls: 'seg-unattributed' });
  return keys;
};

/* -> [{day, total, zero, segs:[{key, v, y0, y1}]}], y0/y1 are cumulative dollars (the base and the top of the segment). */
Charts.geom.stack = function (days, by, top, hueOf) {
  const keys = Charts.geom.rank(days, by, top, hueOf);
  const order = keys.map((k) => k.key);
  const named = new Set(order.filter((k) => k !== 'other'));
  return days.map((d) => {
    const m = Charts.geom.dayMap(d, by);
    const per = new Map();
    let other = m.resid;
    for (const [k, v] of m.map) { if (named.has(k)) per.set(k, v); else other += v; }
    if (other > 0) per.set('other', (per.get('other') || 0) + other);
    const segs = [];
    let y = 0;
    for (const key of order) {
      const v = per.get(key);
      if (!(v > 0)) continue;
      segs.push({ key, v, y0: y, y1: y + v });
      y += v;
    }
    return { day: String(d && d.day), total: y, zero: !!(d && d.zero) || y <= 0, segs };
  });
};

/* A 1/2/3/4/5/6/8 x 10^n ceiling over max with three gridlines. -> {max, ticks:[0, mid, max], k: px per dollar, y(v)}. */
Charts.geom.scale = function (max, H) {
  const m = Number(max);
  const h = Number(H) || 1;
  let nice = 1;
  if (m > 0 && Number.isFinite(m)) {
    const pow = Math.pow(10, Math.floor(Math.log10(m)));
    const f = m / pow;
    nice = ([1, 2, 3, 4, 5, 6, 8, 10].find((s) => f <= s + 1e-9) || 10) * pow;
  }
  const r = (x) => +x.toFixed(6);
  const k = h / nice;
  return { max: r(nice), ticks: [0, r(nice / 2), r(nice)], k, y: (v) => h - v * k };
};

/* Keyboard reach for a pointer chart (issue #45). The chart root is the one tab stop (tabindex 0, role application: arrow keys must reach it, and a screen reader in
   browse mode would keep them). The cursor is a value the chart owns (a day, a row, a [weekday, hour]) rather than a focus per item, so the page keeps one stop per
   chart however many days, rows or cells it draws. o: {label (the aria-label), start (the first cursor), next(cur, key) -> the new cursor, or null for a key that is
   not the chart's, show(cur) (the pointer's own readout and mark for that item)}. Focus by keyboard shows the cursor's item; focus by a pointer does not (the click
   picks its own). The readout <p> is the aria-live region and the root's description, so a screen reader hears each step. Escape leaves (blur). data-kbd="chart" tells
   the page's key table (Keymap.inChart) and shell.js to leave plain keys alone while the chart has the focus. -> {at(cur)}: a pointer pick calls it to move the cursor. */
Charts._seq = 0;
Charts._kbd = function (node, read, o) {
  Charts._seq += 1;
  const id = 'chart-read-' + Charts._seq;
  read.setAttribute('id', id);
  node.setAttribute('role', 'application');
  node.setAttribute('aria-roledescription', 'chart');
  node.setAttribute('aria-label', o.label);
  node.setAttribute('aria-describedby', id);
  node.setAttribute('tabindex', '0');
  node.setAttribute('data-kbd', 'chart');
  let cur = o.start;
  let byPointer = false;
  node.addEventListener('pointerdown', () => { byPointer = true; });
  node.addEventListener('blur', () => { byPointer = false; });
  node.addEventListener('focus', () => { if (byPointer) { byPointer = false; return; } o.show(cur); });
  node.addEventListener('keydown', (e) => {
    if (!e || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Escape') { if (typeof e.preventDefault === 'function') e.preventDefault(); if (typeof node.blur === 'function') node.blur(); return; }
    const nx = o.next(cur, e.key);
    if (nx === null || nx === undefined) return;
    if (typeof e.preventDefault === 'function') e.preventDefault();
    cur = nx;
    o.show(cur);
  });
  return { at: (c) => { cur = c; } };
};

/* The readout of a whole day (a tap beside the drawn bars, on a phone where a 30-day bar is 7 px wide): the day, its total and every segment. */
/* `approx` (the Usage page's Estimated basis, issue #95) puts a `~` in front of every dollar of the readout: they include list-price estimates. */
Charts.dayTitle = function (row, day, approx) {
  const u = (v) => (approx ? '~' : '') + Charts.fmtUsd(v);
  if (!row || row.zero || !row.segs.length) return `${row && row.day} · no spend`;
  const tok = day && Number(day.tokens) > 0 ? ` · ${Charts.fmtTok(day.tokens)} tok` : '';
  return `${row.day} · day ${u(row.total)}${tok} · ${row.segs.map((s) => `${s.key} ${u(s.v)}`).join(', ')}`;
};

Charts.barTitle = function (row, seg, day, approx) {
  const u = (v) => (approx ? '~' : '') + Charts.fmtUsd(v);
  if (!seg) return `${row.day} · no spend`;
  const tok = day && Number(day.tokens) > 0 ? ` · ${Charts.fmtTok(day.tokens)} tok` : '';
  const unTip = seg.key === Charts.UNATTRIBUTED ? Charts.UNATTRIBUTED_TIP : seg.key === Charts.OUTSIDE ? Charts.OUTSIDE_TIP : '';
  const who = unTip ? `${seg.key} ${u(seg.v)} (${unTip})` : `${seg.key} ${u(seg.v)}`;
  return `${row.day} · ${who} · day ${u(row.total)}${tok}`;
};

/* Pattern defs: the zero-day hatch and, per unpriced segment class, the segment colour with dark diagonals over it. */
Charts._barDefs = function (hatchClasses) {
  const defs = svg('defs', { class: 'ch-defs' },
    svg('pattern', { id: 'ch-hz', width: 5, height: 5, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
      svg('line', { class: 'ch-hl', x1: 0, y1: 0, x2: 0, y2: 5 })));
  for (const cls of hatchClasses) {
    defs.append(svg('pattern', { id: 'ch-hx-' + cls, width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
      svg('path', { class: cls, d: 'M0 0h6v6h-6z' }),
      svg('line', { class: 'ch-hxl', x1: 0, y1: 0, x2: 0, y2: 6 })));
  }
  return defs;
};

/* days: summary.daily ([{day, total, zero, by_project, by_agent, tokens, hours}]). opts: {by:'project'|'agent', top:6,
   unpriced: Set of segment keys (project names, or agent names when by is 'agent') and / or 'YYYY-MM-DD' days whose spend is a lower bound,
   hatchZero (default true), height (default 170), hint, empty, onHover(info)} with info {day, key, v, total, zero, tokens, hours, text}.
   A bar's text (hover or tap) goes to the readout line under the chart, because a <title> does not show on a phone, and to onHover. */
Charts.stackedBars = function (host, days, opts) {
  const st = Charts._take(host, 'bars');
  st.args = { days: Array.isArray(days) ? days.slice(-120) : [], o: opts || {} };
  st.redo = () => Charts._paintBars(host, st);
  if (!st.ro) st.ro = Charts._observe(host, () => { if (Charts.paused) st.pending = { redo: true }; else st.redo(); });
  return Charts._paintBars(host, st);
};

Charts._paintBars = function (host, st) {
  const { days, o } = st.args;
  const by = o.by === 'agent' ? 'agent' : 'project';
  const n = days.length;
  host.textContent = '';
  if (!n) { Charts.empty(host, o.empty || Charts.EMPTY_BARS); host.classList.add('chart-bars'); return { rows: [], keys: [], svg: null, legend: null }; }
  const maxSegs = Math.max(2, Math.floor(Charts.MAX_RECTS / n));
  const want = o.top === undefined || o.top === null ? 6 : Math.max(0, Math.floor(Number(o.top)) || 0);
  const top = Math.min(want, maxSegs - 2);
  const rows = Charts.geom.stack(days, by, top, o.hueOf);
  const keys = Charts.geom.rank(days, by, top, o.hueOf);
  const cls = new Map(keys.map((k) => [k.key, k.cls]));
  const unpriced = o.unpriced instanceof Set ? o.unpriced : new Set(Array.isArray(o.unpriced) ? o.unpriced : []);
  const W = Math.max(240, Charts._width(host) || 640);
  const H = Math.max(100, Number(o.height) || 170);
  const M = { l: 42, r: 6, t: 8, b: 20 };
  const innerW = W - M.l - M.r;
  const innerH = H - M.t - M.b;
  const sc = Charts.geom.scale(Math.max(0, ...rows.map((r) => r.total)), innerH);
  const slot = innerW / n;
  const bw = Math.max(3, Math.min(36, slot * 0.72));
  const base = M.t + innerH;
  const hatched = new Set();
  const kids = [];
  for (const t of sc.ticks) {
    const y = (base - t * sc.k).toFixed(1);
    kids.push(svg('line', { class: 'ch-grid', x1: M.l, x2: W - M.r, y1: y, y2: y }));
    kids.push(svg('text', { class: 'ch-ax', x: M.l - 5, y: +y + 3, 'text-anchor': 'end', text: Charts.fmtAxis(t, 'usd') }));
  }
  const step = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(innerW / (n <= 8 ? 46 : 30)))));
  rows.forEach((row, i) => {
    const x = M.l + i * slot + (slot - bw) / 2;
    const info = Charts.geom.dayLabel(row.day);
    if (info && i % step === 0) {
      const text = n <= 8 ? `${info.wd} ${info.d}` : (i === 0 || info.d === 1 ? `${info.mon} ${info.d}` : String(info.d));
      kids.push(svg('text', { class: 'ch-ax', x: (x + bw / 2).toFixed(1), y: H - 5, 'text-anchor': 'middle', text }));
    }
    if (row.zero) {
      if (o.hatchZero === false) return;
      kids.push(svg('rect', { class: 'bar-zero', fill: 'url(#ch-hz)', x: x.toFixed(1), y: base - 8, width: bw.toFixed(1), height: 8, 'data-i': i }, svg('title', { text: Charts.barTitle(row, null, days[i], o.approx) })));
      return;
    }
    row.segs.forEach((seg, s) => {
      const c = cls.get(seg.key) || 'seg-other';
      const flagged = unpriced.has(seg.key) || unpriced.has(row.day);
      const y = base - seg.y1 * sc.k;
      const h = Math.max(1, seg.v * sc.k);
      const a = { x: x.toFixed(1), y: y.toFixed(1), width: bw.toFixed(1), height: h.toFixed(1), 'data-i': i, 'data-s': s };
      if (flagged) { hatched.add(c); Object.assign(a, { class: 'seg-hx ' + c, fill: 'url(#ch-hx-' + c + ')' }); } else a.class = 'seg ' + c;
      kids.push(svg('rect', a, svg('title', { text: Charts.barTitle(row, seg, days[i], o.approx) + (flagged ? ' · some sessions have tokens but no price' : '') })));
    });
  });
  const sum = rows.reduce((a, r) => a + r.total, 0);
  const desc = `Cost per day over the last ${n} days, by ${by}: ${o.approx ? '~' : ''}${Charts.fmtUsd(sum)} API-equivalent${o.approx ? ' (estimated)' : ''}`;
  const node = svg('svg', { class: 'bars', viewBox: `0 0 ${W} ${H}` }, svg('title', { text: desc }), Charts._barDefs(hatched), ...kids);
  const legend = Charts.legendNode(keys.map((k) => ({
    cls: k.cls, label: k.key, v: (o.approx ? '~' : '') + Charts.fmtUsd(k.total), hatch: unpriced.has(k.key),
    tip: k.key === Charts.UNATTRIBUTED ? Charts.UNATTRIBUTED_TIP : k.key === Charts.OUTSIDE ? Charts.OUTSIDE_TIP : (k.members && k.members.length ? `${k.members.length} more: ${k.members.slice(0, 8).join(', ')}` : k.key),
  })));
  const read = el('p', { class: 'chart-read dim', 'aria-live': 'polite', text: o.hint || 'Hover or tap a day for its numbers.' });
  /* A tap or a hover that misses every drawn segment still picks the day under it (the whole column answers, a 30-day bar is 7 px wide): the pointer's
     x maps to a slot. The page's touch-action keeps vertical scrolling. */
  const dayAt = (e) => {
    if (!e || typeof e.clientX !== 'number' || typeof node.getBoundingClientRect !== 'function') return null;
    const r = node.getBoundingClientRect();
    if (!(r.width > 0)) return null;
    const i = Math.floor((((e.clientX - r.left) * (W / r.width)) - M.l) / slot);
    return i >= 0 && i < n ? i : null;
  };
  const mark = (i) => { for (const r of node.querySelectorAll('rect')) r.classList.toggle('day-sel', r.getAttribute('data-i') === String(i)); };
  let kb = null;
  const choose = (i, t) => {
    const row = rows[i];
    if (!row) return;
    if (kb) kb.at(i);
    const s = t ? t.getAttribute('data-s') : null;
    const seg = s === null ? null : row.segs[Number(s)];
    const day = days[i] || {};
    const text = t ? Charts.barTitle(row, seg, day, o.approx) : Charts.dayTitle(row, day, o.approx);
    mark(i);
    read.textContent = text;
    if (typeof o.onHover === 'function') {
      try { o.onHover({ day: row.day, key: seg ? seg.key : null, v: seg ? seg.v : 0, total: row.total, zero: row.zero, tokens: Number(day.tokens) || 0, hours: Number(day.hours) || 0, text }); } catch (err) { console.error('ccboard charts onHover', err); }
    }
  };
  const show = (e) => {
    const t = e && e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-i]') : null;
    const i = t ? Number(t.getAttribute('data-i')) : dayAt(e);
    if (i !== null) choose(i, t);
  };
  /* the keyboard walks the days (left and right, Home and End) and reads each one whole, like a tap beside the bars */
  kb = Charts._kbd(node, read, { label: desc + '. Left and right arrows step through the days.', start: 0,
    next: (i, key) => (key === 'ArrowRight' ? Math.min(n - 1, i + 1) : key === 'ArrowLeft' ? Math.max(0, i - 1) : key === 'Home' ? 0 : key === 'End' ? n - 1 : null),
    show: (i) => choose(i, null) });
  node.addEventListener('click', show);
  node.addEventListener('pointerover', show);
  node.addEventListener('pointermove', (e) => { if (e && e.pointerType === 'mouse') show(e); });
  host.append(node, legend, read);
  return { rows, keys, svg: node, legend };
};

/* ---------- session Gantt ---------- */

/* events: /api/series/events?series=state rows [{t, key, v, m:{p, r, s, a}}] (v 0 idle 1 working 2 waiting 3 done 4 errored 5 ended). A span runs from an
   event to the next event of the same key and the last one to `until`; an ended event (v 5) draws nothing and closes the row (ended: true), a later
   event for the same key opens it again. Spans are clipped to [since, until]; a row with no span left is dropped. Rows are the most recently active
   first; past maxRows the rest are cut. -> {rows:[{key, label, project, repo, session, agent, ended, last, spans:[{t0, t1, v}]}], truncated: rows cut, total}
   (label is 'project/repo · session', the repo left out when it is 'root'). */
Charts.geom.spans = function (events, since, until, maxRows) {
  const list = Array.isArray(events) ? events : [];
  const groups = new Map();
  list.forEach((e, i) => {
    const t = Charts.epoch(e && e.t);
    const v = Number(e && e.v);
    if (t === null || !Number.isInteger(v) || v < 0 || v > 5 || !e || e.key === undefined || e.key === null) return;
    const key = String(e.key);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push({ t, v, m: e.m && typeof e.m === 'object' ? e.m : null, i });
  });
  let lo = Charts.epoch(since);
  let hi = Charts.epoch(until);
  const all = [...groups.values()].flat();
  if (hi === null) hi = all.length ? Math.max(...all.map((e) => e.t)) : 0;
  if (lo === null) lo = all.length ? Math.min(...all.map((e) => e.t)) : 0;
  const rows = [];
  for (const [key, evs] of groups) {
    evs.sort((a, b) => a.t - b.t || a.i - b.i);
    const spans = [];
    evs.forEach((e, i) => {
      if (e.v === 5) return;
      const next = evs[i + 1];
      const t0 = Math.max(e.t, lo);
      const t1 = Math.min(next ? next.t : hi, hi);
      if (!(t1 > t0)) return;
      const prev = spans[spans.length - 1];
      if (prev && prev.v === e.v && prev.t1 === t0) prev.t1 = t1; else spans.push({ t0, t1, v: e.v });
    });
    if (!spans.length) continue;
    const meta = [...evs].reverse().find((e) => e.m && (e.m.p || e.m.r || e.m.s));
    const m = meta ? meta.m : {};
    const where = m.p ? (m.r && m.r !== 'root' ? `${m.p}/${m.r}` : String(m.p)) : '';
    const label = where ? (m.s ? `${where} · ${m.s}` : where) : key;
    rows.push({ key, label, project: m.p ? String(m.p) : '', repo: m.r ? String(m.r) : '', session: m.s ? String(m.s) : '', agent: m.a ? String(m.a) : '',
      ended: evs[evs.length - 1].v === 5, last: evs[evs.length - 1].t, spans });
  }
  rows.sort((a, b) => b.last - a.last || (a.key < b.key ? -1 : 1));
  const cap = maxRows === undefined || maxRows === null ? Infinity : Math.max(0, Math.floor(Number(maxRows)));
  return { rows: rows.slice(0, cap), truncated: Math.max(0, rows.length - cap), total: rows.length };
};

/* Local hour boundaries inside [since, until] that fall on a multiple of stepH hours: [{t, label:'06:00'}]. */
Charts.geom.hourTicks = function (since, until, stepH) {
  const step = Math.max(1, Math.floor(Number(stepH)) || 1);
  const out = [];
  if (!(until > since)) return out;
  const d = new Date(since * 1000);
  d.setMinutes(0, 0, 0);
  let t = d.getTime() / 1000;
  for (let guard = 0; guard < 400 && (t < since || new Date(t * 1000).getHours() % step !== 0); guard++) t += 3600;
  for (let guard = 0; t <= until && guard < 400; guard++, t += step * 3600) out.push({ t, label: `${Charts.pad2(new Date(t * 1000).getHours())}:00` });
  return out;
};

/* events: the rows array, or the whole body {events, truncated}. opts: {since, until (epoch s or ISO; default the events' own span), maxRows:30,
   rowHeight:18, empty (text), truncated (the server cut the events too)}. The empty text is Charts.EMPTY_GANTT. */
Charts.gantt = function (host, events, opts) {
  const st = Charts._take(host, 'gantt');
  st.args = { events, o: opts || {} };
  st.redo = () => Charts._paintGantt(host, st);
  if (!st.ro) st.ro = Charts._observe(host, () => { if (Charts.paused) st.pending = { redo: true }; else st.redo(); });
  return Charts._paintGantt(host, st);
};

Charts._paintGantt = function (host, st) {
  const { events, o } = st.args;
  const body = events && !Array.isArray(events) && typeof events === 'object' ? events : null;
  const list = Array.isArray(events) ? events : (body && Array.isArray(body.events) ? body.events : []);
  const maxRows = o.maxRows === undefined ? 30 : o.maxRows;
  const model = Charts.geom.spans(list, o.since, o.until, maxRows);
  host.textContent = '';
  if (!model.rows.length) { Charts.empty(host, o.empty || Charts.EMPTY_GANTT); host.classList.add('chart-gantt'); return { rows: [], truncated: 0, svg: null, legend: null }; }
  const all = model.rows.flatMap((r) => r.spans);
  const lo = Charts.epoch(o.since) !== null ? Charts.epoch(o.since) : Math.min(...all.map((s) => s.t0));
  const hi = Charts.epoch(o.until) !== null ? Charts.epoch(o.until) : Math.max(...all.map((s) => s.t1));
  const span = Math.max(1, hi - lo);
  const W = Math.max(240, Charts._width(host) || 640);
  const rowH = Math.max(14, Number(o.rowHeight) || 18);
  const top = 16;
  const labelW = Math.round(Math.max(88, Math.min(170, W * 0.34)));
  const x0 = labelW;
  const plotW = Math.max(60, W - 6 - x0);
  const H = top + model.rows.length * rowH + 4;
  const X = (t) => x0 + ((Math.min(hi, Math.max(lo, t)) - lo) / span) * plotW;
  const wide = span > 36 * 3600;
  let stepH = 1;
  for (const c of [1, 2, 3, 4, 6, 12, 24]) { stepH = c; if (span / 3600 / c <= plotW / 46) break; }
  const kids = [];
  for (const tk of Charts.geom.hourTicks(lo, hi, stepH)) {
    const x = X(tk.t).toFixed(1);
    kids.push(svg('line', { class: 'ch-grid', x1: x, x2: x, y1: top - 3, y2: H - 2 }));
    kids.push(svg('text', { class: 'ch-ax', x, y: 10, 'text-anchor': 'middle', text: tk.label }));
  }
  const chars = Math.max(6, Math.floor((labelW - 8) / 6.1));
  const seen = new Set();
  model.rows.forEach((row, i) => {
    const y = top + i * rowH;
    const g = svg('g', { class: 'g-row' + (row.ended ? ' ended' : ''), 'data-key': row.key });
    const full = row.ended ? `${row.label} · ended` : row.label;
    g.append(svg('text', { class: 'g-lab', x: 0, y: y + rowH / 2 + 3.5, text: row.label.length > chars ? row.label.slice(0, chars - 1) + '…' : row.label }, svg('title', { text: full })));
    for (const sp of row.spans) {
      seen.add(sp.v);
      const x = X(sp.t0);
      g.append(svg('rect', { class: 'sp st-' + sp.v, x: x.toFixed(1), y: y + 3, width: Math.max(1.5, X(sp.t1) - x).toFixed(1), height: rowH - 6, rx: 1 },
        svg('title', { text: `${row.label} · ${Charts.STATES[sp.v]} · ${Charts.when(sp.t0, wide)} to ${Charts.when(sp.t1, wide)} (${Charts.dur(sp.t1 - sp.t0)})` })));
    }
    kids.push(g);
  });
  const desc = `Session timeline: ${model.rows.length} session${model.rows.length === 1 ? '' : 's'} over ${Charts.dur(span)}`;
  const node = svg('svg', { class: 'gantt', viewBox: `0 0 ${W} ${H}` }, svg('title', { text: desc }), ...kids);
  if (model.rows.some((r) => r.ended)) seen.add(5);
  const glyphs = typeof STATE_GLYPH !== 'undefined' ? STATE_GLYPH : {};
  const legend = Charts.legendNode([...seen].sort().map((v) => ({ cls: 'st-' + v, label: `${glyphs[Charts.STATES[v]] || ''} ${Charts.STATES[v]}`.trim(), tip: v === 5 ? 'ended: the row fades' : Charts.STATES[v] })));
  // a row's label is cut to fit and the times live in tooltips a phone never shows: a tap on a row (its label, a span, or the space beside it) says it all
  const read = el('p', { class: 'chart-read dim', 'aria-live': 'polite', text: o.hint || 'Tap a row for its full name and times.' });
  const rowText = (row) => {
    const spans = row.spans.map((sp) => `${Charts.when(sp.t0, wide)}–${Charts.when(sp.t1, wide)} ${Charts.STATES[sp.v]}`);
    return `${row.label}${row.ended ? ' · ended' : ''} · ${(spans.length > 6 ? ['…', ...spans.slice(-6)] : spans).join(', ')}`;
  };
  let kb = null;
  const choose = (i) => {
    const row = model.rows[i];
    if (!row) return;
    if (kb) kb.at(i);
    for (const gr of node.querySelectorAll('.g-row')) gr.classList.toggle('sel', gr.getAttribute('data-key') === row.key);
    read.textContent = rowText(row);
  };
  /* the keyboard walks the rows (up and down, Home and End) and reads each like a tap on it */
  const last = model.rows.length - 1;
  kb = Charts._kbd(node, read, { label: desc + '. Up and down arrows step through the sessions.', start: 0,
    next: (i, key) => (key === 'ArrowDown' ? Math.min(last, i + 1) : key === 'ArrowUp' ? Math.max(0, i - 1) : key === 'Home' ? 0 : key === 'End' ? last : null),
    show: choose });
  const pick = (e) => {
    const g = e && e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-key]') : null;
    let key = g ? g.getAttribute('data-key') : null;
    if (key === null && e && typeof e.clientY === 'number' && typeof node.getBoundingClientRect === 'function') {
      const r = node.getBoundingClientRect();
      if (r.height > 0) { const i = Math.floor((((e.clientY - r.top) * (H / r.height)) - top) / rowH); if (i >= 0 && i < model.rows.length) key = model.rows[i].key; }
    }
    choose(key === null ? -1 : model.rows.findIndex((x) => x.key === key));
  };
  node.addEventListener('click', pick);
  host.append(node, legend, read);
  if (model.truncated) host.append(el('p', { class: 'chart-note dim', text: `${model.truncated} more session${model.truncated === 1 ? '' : 's'} not shown` }));
  if (o.truncated || (body && body.truncated)) host.append(el('p', { class: 'chart-note dim', text: 'The server cut the event list: older activity in this window may be missing.' }));
  return { rows: model.rows, truncated: model.truncated, svg: node, legend };
};

/* ---------- heatmap ---------- */

/* 0 only for no events; otherwise the quartile of max the value falls in: 1 (up to a quarter), 2, 3, 4 (over three quarters). */
Charts.geom.level = function (v, max) {
  const x = Number(v);
  const m = Number(max);
  if (!(x > 0) || !(m > 0)) return 0;
  return Math.min(4, Math.max(1, Math.ceil((4 * x) / m)));
};

/* grid: 7 rows (Mon=0) of 24 hour counts; hourly: 24 counts (default: the column sums). opts: {tzLabel, hint, empty}. -> the grid node. */
Charts.heatmap = function (host, grid, hourly, opts) {
  const o = opts || {};
  const st = Charts._take(host, 'heat');
  host.textContent = '';
  const cell = (v) => { const n = Number(v); return Number.isFinite(n) && n > 0 ? n : 0; };
  const rows = Charts.WEEKDAYS.map((_, d) => Array.from({ length: 24 }, (__, h) => cell(Array.isArray(grid) && Array.isArray(grid[d]) ? grid[d][h] : 0)));
  const prof = Array.from({ length: 24 }, (_, h) => (Array.isArray(hourly) && hourly.length === 24 ? cell(hourly[h]) : rows.reduce((a, r) => a + r[h], 0)));
  const max = Math.max(0, ...rows.flat());
  const pmax = Math.max(0, ...prof);
  if (max <= 0 && pmax <= 0) { Charts.empty(host, o.empty || Charts.EMPTY_HEAT); host.classList.add('chart-heat'); return null; }
  const plural = (n) => `${n} event${n === 1 ? '' : 's'}`;
  const hh = (h) => `${Charts.pad2(h)}:00`;
  const desc = `Hook events by weekday and hour, local time${o.tzLabel ? ' (' + o.tzLabel + ')' : ''}: busiest hour ${hh(prof.indexOf(pmax))}, ${plural(pmax)}`;
  const node = el('div', { class: 'heat-grid' });
  node.append(el('span', { class: 'heat-corner' }));
  for (let h = 0; h < 24; h++) node.append(el('span', { class: 'heat-hr', text: h % 3 === 0 ? Charts.pad2(h) : '' }));
  const cellAt = [];                                                     // [weekday][hour] -> the cell, and the hour profile's bars after the 7 rows (the tap's nearest-cell lookup)
  rows.forEach((r, d) => {
    node.append(el('span', { class: 'heat-day', text: Charts.WEEKDAYS[d] }));
    cellAt[d] = [];
    r.forEach((v, h) => {
      const tip = `${Charts.WEEKDAYS[d]} ${hh(h)} · ${plural(v)}`;
      const c = el('div', { class: `cell lv${Charts.geom.level(v, max)}`, title: tip, 'data-tip': tip });
      cellAt[d][h] = c;
      node.append(c);
    });
  });
  node.append(el('span', { class: 'heat-day heat-all', text: 'all' }));
  cellAt[7] = [];
  prof.forEach((v, h) => {
    const tip = `${hh(h)} · ${plural(v)}`;
    const bar = el('i');
    bar.style.height = v > 0 ? `${Math.max(6, Math.round((v / pmax) * 100))}%` : '0';
    const b = el('div', { class: 'hb', title: tip, 'data-tip': tip }, bar);
    cellAt[7][h] = b;
    node.append(b);
  });
  const read = el('p', { class: 'chart-read dim', 'aria-live': 'polite', text: o.hint || 'Hover or tap a cell for its count.' });
  let sel = null;
  /* An 11 px cell is no target for a finger: a tap beside a cell (the gap, a label, the corner) picks the nearest one by its position in the grid. */
  const nearest = (e) => {
    if (!e || typeof e.clientX !== 'number' || typeof e.clientY !== 'number' || typeof cellAt[0][0].getBoundingClientRect !== 'function') return null;
    const tl = cellAt[0][0].getBoundingClientRect();
    const tr = cellAt[0][23].getBoundingClientRect();
    const bl = cellAt[6][0].getBoundingClientRect();
    if (!(tr.right > tl.left) || !(bl.bottom > tl.top)) return null;
    const h = Math.max(0, Math.min(23, Math.floor((e.clientX - tl.left) / ((tr.right - tl.left) / 24))));
    const d = e.clientY > bl.bottom ? 7 : Math.max(0, Math.min(6, Math.floor((e.clientY - tl.top) / ((bl.bottom - tl.top) / 7))));
    return cellAt[d][h];
  };
  let kb = null;
  const choose = (t) => {
    if (sel && sel !== t) sel.classList.remove('sel');
    t.classList.add('sel');
    sel = t;
    read.textContent = t.getAttribute('data-tip');
    if (kb) cellAt.forEach((r, d) => { const h = r.indexOf(t); if (h >= 0) kb.at([d, h]); });
  };
  const show = (e) => {
    const t = (e && e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-tip]') : null) || nearest(e);
    if (t) choose(t);
  };
  /* the keyboard moves a cursor over the grid: arrows by weekday and hour, Home and End to the ends of the row; the 8th row is the hour profile under it */
  kb = Charts._kbd(node, read, { label: desc + '. Arrow keys move between the cells; Home and End go to the ends of the row.', start: [0, 0],
    next: ([d, h], key) => (key === 'ArrowRight' ? [d, Math.min(23, h + 1)] : key === 'ArrowLeft' ? [d, Math.max(0, h - 1)] : key === 'ArrowDown' ? [Math.min(7, d + 1), h]
      : key === 'ArrowUp' ? [Math.max(0, d - 1), h] : key === 'Home' ? [d, 0] : key === 'End' ? [d, 23] : null),
    show: ([d, h]) => choose(cellAt[d][h]) });
  node.addEventListener('click', show);
  node.addEventListener('pointerover', show);
  host.append(node);
  if (o.tzLabel) host.append(el('p', { class: 'chart-note dim heat-tz', text: String(o.tzLabel) }));
  host.append(read);
  st.redo = null;
  return node;
};

/* ---------- sparkline ---------- */

/* t: epoch seconds, vals: numbers | null (a null breaks the line). opts: {w:240, h:44, min:0, max:100 (null = the data's max), guide:85 (null = none), label}.
   The classes are Widgets.sparkline's (spark, sp-line, sp-guide, sp-base), so one stylesheet draws both. -> the svg (also appended to host
   after emptying it), or null when there are fewer than two points or no value. */
Charts.spark = function (host, t, vals, opts) {
  const o = opts || {};
  if (host) host.textContent = '';
  const ts = Array.isArray(t) ? t : [];
  const vs = Array.isArray(vals) ? vals : [];
  const known = vs.filter((v) => Charts.num(v) !== null);
  if (ts.length < 2 || !known.length) return null;
  const W = Number(o.w) || 240;
  const H = Number(o.h) || 44;
  const pad = 3;
  const lo = Charts.num(o.min) === null ? 0 : o.min;
  const hi = o.max === null ? Math.max(lo + 1e-9, ...known) : (Charts.num(o.max) === null ? 100 : o.max);
  const t0 = Number(ts[0]);
  const spanT = Math.max(1, Number(ts[ts.length - 1]) - t0);
  const px = (i) => ((Number(ts[i]) - t0) / spanT) * W;
  const py = (v) => H - pad - ((Math.max(lo, Math.min(hi, v)) - lo) / ((hi - lo) || 1)) * (H - 2 * pad);
  const kids = [];
  const guide = o.guide === undefined ? (hi === 100 ? 85 : null) : o.guide;
  if (Charts.num(guide) !== null) kids.push(svg('line', { class: 'sp-guide', x1: 0, x2: W, y1: py(guide), y2: py(guide), 'vector-effect': 'non-scaling-stroke' }));
  kids.push(svg('line', { class: 'sp-base', x1: 0, x2: W, y1: H - pad, y2: H - pad, 'vector-effect': 'non-scaling-stroke' }));
  let run = [];
  const flush = () => {
    if (run.length > 1) kids.push(svg('polyline', { class: 'sp-line', points: run.join(' '), fill: 'none', 'vector-effect': 'non-scaling-stroke' }));
    else if (run.length === 1) { const [x, y] = run[0].split(',').map(Number); kids.push(svg('line', { class: 'sp-line', x1: x - 1, x2: x + 1, y1: y, y2: y, 'vector-effect': 'non-scaling-stroke' })); }
    run = [];
  };
  vs.forEach((v, i) => { if (i < ts.length && Charts.num(v) !== null) run.push(`${px(i).toFixed(1)},${py(v).toFixed(1)}`); else flush(); });
  flush();
  const unit = hi === 100 ? '%' : '';
  const desc = `${o.label || 'Trend'}: now ${Math.round(known[known.length - 1])}${unit}, peak ${Math.round(Math.max(...known))}${unit}`;
  const node = svg('svg', { class: 'spark', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': desc }, svg('title', { text: desc }), ...kids);
  if (host) host.append(node);
  return node;
};

if (typeof window !== 'undefined') window.Charts = Charts;
