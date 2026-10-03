/* ccboard widgets (v0.5.5): the usage card Home ends with and the rate-limit banner. Definition only at load: nothing here touches the DOM,
   storage, the network or a timer until usageCard(host) or limitBanner(st) is called. Classic script, one namespace (Widgets), loaded after
   pages/inbox.js.

   Widgets.usageCard(host) -> {update(st), refresh(), destroy()}
     Builds the card into host and keeps it. update(st) paints what the state already holds (the 5H and 7D gauges and their reset countdowns from
     st.usage, the same record as the topbar pills; the ccusage block's burn; the backup chip); it is cheap and runs on every state change.
     refresh() fetches the two series the state does not carry: the last 24 h of rl_5h for claude (GET /api/series, drawn as a sparkline) and the
     last 7 days of cost (GET /api/usage/summary, one bar per day, a day without spend hatched). It runs once when the card is built and then every
     60 s while the tab is visible, never from the 3 s state poll, and returns the promise of the fetches. An error is swallowed: the card keeps what
     it had. The whole card stays hidden until a fetch has returned data. destroy() stops the timer and removes the card.
   Widgets.limitBanner(st)
     Puts a callout in #banner while st.rate_limited names a limit whose reset time is still ahead ('Claude rate limit (5h) · resets 22:05 · s1'),
     takes it away when the limit is over, and remembers a dismissal (the reset time, in sessionStorage) so the same episode stays quiet.
     renderBanner() empties #banner on every render: the shell calls this right after it, and it is safe to call any number of times.
   state.rate_limited is the kv wrapper {value: {session, message, kind, resets_at (epoch s)}, at}; a flat record is read too. */
'use strict';

const Widgets = {
  REFRESH_MS: 60000,                      // how often the usage card refetches while the tab is visible
  DISMISS_KEY: 'ccboard:limit:dismissed', // sessionStorage: the resets_at of the limit the person dismissed
  SPARK_W: 240, SPARK_H: 44,              // the sparkline's own coordinate space (the svg stretches to the card)
  BARS_W: 280, BARS_H: 56,
  limitNode: null,
};

/* ---------- small helpers ---------- */

Widgets.epoch = function (v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v > 1e11 ? v / 1000 : v;
  if (typeof v === 'string' && v) { const n = Number(v); if (Number.isFinite(n)) return Widgets.epoch(n); const t = Date.parse(v); return Number.isNaN(t) ? 0 : t / 1000; }
  return 0;
};

/* The rate-limit record of a state payload as {session, message, kind, resets_at, at}, or null. */
Widgets.limit = function (st) {
  const r = st && st.rate_limited;
  if (!r || typeof r !== 'object') return null;
  const v = r.value && typeof r.value === 'object' ? r.value : r;
  if (!v.session && !v.message && !v.resets_at) return null;
  return { session: String(v.session || ''), message: String(v.message || ''), kind: String(v.kind || ''), resets_at: Widgets.epoch(v.resets_at), at: r.at || v.at || '' };
};

/* Local clock time of an epoch ('22:05'), with the weekday when it is more than a day away (a 5 h window that resets after midnight is still just a time). */
Widgets.clock = function (epoch) {
  if (!epoch) return '';
  const d = new Date(epoch * 1000);
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (Math.abs(epoch - Date.now() / 1000) < 20 * 3600) return hm;
  return `${['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()]} ${hm}`;
};

Widgets.money = function (v) {
  const n = Number(v) || 0;
  return '$' + (n >= 100 ? String(Math.round(n)) : n.toFixed(2));
};

/* The viewer's zone in minutes east of UTC, for /api/usage/summary (the server takes -720..840; 345 = Asia/Kathmandu when the browser will not say). */
Widgets.tzMin = function () {
  try { const m = -new Date().getTimezoneOffset(); if (Number.isFinite(m)) return Math.max(-720, Math.min(840, m)); } catch (_) { /* no Date zone */ }
  return 345;
};

Widgets.tone = function (pct) { return pct >= 85 ? 'bad' : pct >= 60 ? 'warn' : 'ok'; };

Widgets.hiddenTab = function () { try { return !!(typeof document !== 'undefined' && document.hidden); } catch (_) { return false; } };

/* ---------- the usage card ---------- */

/* One gauge: label, a bar filled to the used percentage, the number and the reset countdown. set(window | null). */
Widgets.gauge = function (label) {
  const fill = el('i');
  const val = el('span', { class: 'g-val mono' });
  const reset = el('span', { class: 'g-reset dim' });
  const bar = el('span', { class: 'g-bar', role: 'img' }, fill);
  const root = el('div', { class: 'gauge hidden' }, el('b', { class: 'g-label', text: label }), bar, val, reset);
  root.set = (w) => {
    const ok = !!w && typeof w.used_percentage === 'number';
    root.classList.toggle('hidden', !ok);
    if (!ok) return;
    const pct = Math.max(0, Math.min(100, w.used_percentage));
    for (const t of ['ok', 'warn', 'bad']) root.classList.toggle(t, t === Widgets.tone(pct));
    fill.style.width = `${pct}%`;
    setTextIfChanged(val, `${Math.round(pct)}%`);
    const eta = w.resets_at ? fmtIn(w.resets_at) : '';
    setTextIfChanged(reset, eta ? `resets in ${eta}` : '');
    bar.setAttribute('aria-label', `${label} ${Math.round(pct)}% used`);
    root.setAttribute('title', `${label} window: ${Math.round(pct)}% used` + (w.resets_at ? ` · resets ${Widgets.clock(w.resets_at)}` : ''));
  };
  return root;
};

/* The sparkline: {t:[epoch s], values:[0..100|null]} -> an svg with one polyline per run of known points (a gap is a break, not a zero). */
Widgets.sparkline = function (t, values) {
  const W = Widgets.SPARK_W;
  const H = Widgets.SPARK_H;
  const pad = 3;
  const t0 = t[0];
  const span = Math.max(1, t[t.length - 1] - t0);
  const px = (i) => ((t[i] - t0) / span) * W;
  const py = (v) => H - pad - (Math.max(0, Math.min(100, v)) / 100) * (H - 2 * pad);
  const kids = [
    svg('line', { class: 'sp-guide', x1: 0, x2: W, y1: py(85), y2: py(85), 'vector-effect': 'non-scaling-stroke' }),
    svg('line', { class: 'sp-base', x1: 0, x2: W, y1: H - pad, y2: H - pad, 'vector-effect': 'non-scaling-stroke' }),
  ];
  let run = [];
  const flush = () => {
    if (run.length > 1) kids.push(svg('polyline', { class: 'sp-line', points: run.join(' '), fill: 'none', 'vector-effect': 'non-scaling-stroke' }));
    else if (run.length === 1) { const [x, y] = run[0].split(',').map(Number); kids.push(svg('line', { class: 'sp-line', x1: x - 1, x2: x + 1, y1: y, y2: y, 'vector-effect': 'non-scaling-stroke' })); }
    run = [];
  };
  values.forEach((v, i) => { if (typeof v === 'number' && Number.isFinite(v)) run.push(`${px(i).toFixed(1)},${py(v).toFixed(1)}`); else flush(); });
  flush();
  const known = values.filter((v) => typeof v === 'number');
  const now = known.length ? Math.round(known[known.length - 1]) : null;
  const peak = known.length ? Math.round(Math.max(...known)) : null;
  const desc = known.length ? `5-hour window, last 24 hours: now ${now}%, peak ${peak}%` : '5-hour window, last 24 hours: no samples';
  return svg('svg', { class: 'spark', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': desc }, svg('title', { text: desc }), ...kids);
};

/* Seven bars: daily = [{day:'YYYY-MM-DD', total, zero}] -> an svg with one rect per day (a day without spend is a short hatched stub). */
Widgets.costBars = function (daily) {
  const W = Widgets.BARS_W;
  const H = Widgets.BARS_H;
  const n = daily.length;
  const slot = W / n;
  const bw = Math.max(4, slot * 0.58);
  const max = Math.max(1e-9, ...daily.map((d) => Number(d.total) || 0));
  const stub = 7;
  const hatch = svg('defs', {}, svg('pattern', { id: 'uc-hatch', width: 4, height: 4, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
    svg('line', { class: 'cb-hatch-line', x1: 0, y1: 0, x2: 0, y2: 4 })));
  const rects = daily.map((d, i) => {
    const total = Number(d.total) || 0;
    const zero = !!d.zero || total <= 0;
    const h = zero ? stub : Math.max(2, (total / max) * (H - 2));
    return svg('rect', { class: 'cb' + (zero ? ' zero' : ''), x: (i * slot + (slot - bw) / 2).toFixed(1), y: (H - h).toFixed(1), width: bw.toFixed(1), height: h.toFixed(1), rx: 2, 'data-day': d.day },
      svg('title', { text: `${d.day}: ${zero ? 'no spend' : Widgets.money(total) + ' API-equivalent'}` }));
  });
  const sum = daily.reduce((a, d) => a + (Number(d.total) || 0), 0);
  const desc = `Cost over the last ${n} days: ${Widgets.money(sum)} API-equivalent`;
  return svg('svg', { class: 'cbars', viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: 'none', role: 'img', 'aria-label': desc }, svg('title', { text: desc }), hatch,
    svg('line', { class: 'cb-base', x1: 0, x2: W, y1: H, y2: H, 'vector-effect': 'non-scaling-stroke' }), ...rects);
};

/* Weekday initial of a 'YYYY-MM-DD' day, read as a calendar date (no zone shift). */
Widgets.dayInitial = function (day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day));
  if (!m) return '';
  return 'SMTWTFS'.charAt(new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])).getUTCDay());
};

Widgets.usageCard = function (host) {
  const g5 = Widgets.gauge('5H');
  const g7 = Widgets.gauge('7D');
  const burn = el('div', { class: 'uc-burn dim hidden' });
  const backup = el('a', { class: 'uc-backup hidden', href: '#/settings?sec=box' });
  const sparkTitle = el('span', { class: 'uc-cap-t', text: '5H window · last 24 h' });
  const sparkNow = el('span', { class: 'uc-cap-v mono' });
  const sparkBox = el('div', { class: 'uc-plot' });
  const barsTitle = el('span', { class: 'uc-cap-t', text: 'Cost · last 7 days · API-equivalent' });
  const barsSum = el('span', { class: 'uc-cap-v mono' });
  const barsBox = el('div', { class: 'uc-plot' });
  const days = el('div', { class: 'uc-days', 'aria-hidden': 'true' });
  const spark = el('figure', { class: 'uc-fig uc-spark hidden' }, el('figcaption', { class: 'uc-cap' }, sparkTitle, sparkNow), sparkBox);
  const bars = el('figure', { class: 'uc-fig uc-bars hidden' }, el('figcaption', { class: 'uc-cap' }, barsTitle, barsSum), barsBox, days);
  const root = el('section', { class: 'ucard hidden', 'aria-label': 'Usage' },
    el('div', { class: 'uc-head' }, el('h2', { text: 'Usage' }), backup),
    el('div', { class: 'uc-gauges' }, g5, g7),
    burn,
    el('div', { class: 'uc-charts' }, spark, bars));
  host.append(root);

  const self = { destroyed: false, st: null, series: null, summary: null, inflight: null, timer: null, stale: false, sig: '', onVisible: null };

  const paintData = () => {
    const s = self.series;
    const t = s && Array.isArray(s.t) ? s.t : [];
    const key = s && s.series ? Object.keys(s.series).find((k) => k.indexOf('rl_5h') === 0) : '';
    const values = key ? s.series[key] : [];
    const known = Array.isArray(values) ? values.filter((v) => typeof v === 'number') : [];
    const hasSpark = t.length > 1 && known.length > 0;
    spark.classList.toggle('hidden', !hasSpark);
    if (hasSpark) {
      sparkBox.textContent = '';
      sparkBox.append(Widgets.sparkline(t, values));
      setTextIfChanged(sparkNow, `now ${Math.round(known[known.length - 1])}%`);
    }
    const d = self.summary && Array.isArray(self.summary.daily) ? self.summary.daily.slice(-7) : [];
    bars.classList.toggle('hidden', !d.length);
    if (d.length) {
      barsBox.textContent = '';
      barsBox.append(Widgets.costBars(d));
      days.textContent = '';
      for (const x of d) days.append(el('span', { text: Widgets.dayInitial(x.day) }));
      setTextIfChanged(barsSum, Widgets.money(d.reduce((a, x) => a + (Number(x.total) || 0), 0)));
    }
    root.classList.toggle('hidden', !(hasSpark || d.length));
  };

  const paintState = (st) => {
    if (!st) return;
    const rl = (st.usage && st.usage.value) || {};
    g5.set(rl.five_hour);
    g7.set(rl.seven_day);
    const b = st.block && st.block.value;
    const live = !!(b && b.available && b.active);
    burn.classList.toggle('hidden', !live);
    if (live) {
      const parts = [];
      if (typeof b.burn_cost_per_hour === 'number') parts.push(`${Widgets.money(b.burn_cost_per_hour)}/h burn`);
      if (typeof b.cost_usd === 'number') parts.push(`${Widgets.money(b.cost_usd)} this block`);
      if (typeof b.remaining_minutes === 'number') parts.push(`${Math.round(b.remaining_minutes)} min left`);
      if (typeof b.projected_cost === 'number') parts.push(`~${Widgets.money(b.projected_cost)} projected`);
      setTextIfChanged(burn, 'ccusage · ' + parts.join(' · '));
    }
    const bk = st.backup;
    backup.classList.toggle('hidden', !bk || !bk.at);
    if (bk && bk.at) {
      const failed = bk.status && bk.status !== 'ok';
      setTextIfChanged(backup, `backup ${failed ? 'failed' : 'ok'} ${fmtAge(Date.parse(bk.at) / 1000)} ago`);
      backup.classList.toggle('bad', !!failed);
    }
  };

  const valid = {
    series: (s) => !!s && Array.isArray(s.t) && s.t.length > 1 && !!s.series && typeof s.series === 'object'
      && Object.values(s.series).some((col) => Array.isArray(col) && col.some((v) => typeof v === 'number')),   // at least one real point
    summary: (s) => !!s && Array.isArray(s.daily) && s.daily.some((d) => d && Number(d.total) > 0),              // a fresh install shows nothing
  };

  self.refresh = function () {
    if (self.destroyed) return Promise.resolve();
    if (self.inflight) return self.inflight;
    self.stale = false;
    const quiet = () => null;                                   // an error leaves what the card already shows
    const p = Promise.all([
      Promise.resolve().then(() => api('GET', '/api/series?series=rl_5h&key=claude&since=24h')).catch(quiet),
      Promise.resolve().then(() => api('GET', `/api/usage/summary?days=7&tz_min=${Widgets.tzMin()}`)).catch(quiet),
    ]).then(([series, summary]) => {
      if (self.destroyed) return;
      if (valid.series(series)) self.series = series;
      if (valid.summary(summary)) self.summary = summary;
      paintData();
      paintState(self.st);
    }).catch(quiet).then(() => { self.inflight = null; });
    self.inflight = p;
    return p;
  };

  self.update = function (st) {
    if (self.destroyed) return;
    self.st = st || null;
    paintState(self.st);
  };

  self.destroy = function () {
    self.destroyed = true;
    if (self.timer && typeof clearInterval === 'function') clearInterval(self.timer);
    self.timer = null;
    if (self.onVisible && typeof document !== 'undefined') document.removeEventListener('visibilitychange', self.onVisible);
    self.onVisible = null;
    root.remove();
  };

  // every minute: refetch while the tab is visible (a hidden tab refetches when it is shown again) and let the countdowns move
  if (typeof setInterval === 'function') {
    self.timer = setInterval(() => {
      if (Widgets.hiddenTab()) { self.stale = true; return; }
      paintState(self.st);
      self.refresh();
    }, Widgets.REFRESH_MS);
    if (self.timer && typeof self.timer.unref === 'function') self.timer.unref();
  }
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    self.onVisible = () => { if (!Widgets.hiddenTab() && self.stale) { paintState(self.st); self.refresh(); } };
    document.addEventListener('visibilitychange', self.onVisible);
  }
  self.refresh();
  return { update: self.update, refresh: self.refresh, destroy: self.destroy, root, state: self };
};

/* ---------- the rate-limit banner ---------- */

Widgets.dismissed = function () { try { return sessionStorage.getItem(Widgets.DISMISS_KEY) || ''; } catch (_) { return ''; } };

/* The same episode keeps its reset time to within a few minutes (parsed from a clock time in the message); a new window is hours away. */
Widgets.isDismissed = function (resetsAt) {
  const d = Number(Widgets.dismissed());
  return Number.isFinite(d) && d > 0 && Math.abs(d - resetsAt) < 600;
};

Widgets.dismiss = function (resetsAt) { try { sessionStorage.setItem(Widgets.DISMISS_KEY, String(resetsAt)); } catch (_) { /* storage may be unavailable */ } };

Widgets.limitText = function (lim) {
  const kind = lim.kind === '5h' || lim.kind === '7d' ? ` (${lim.kind})` : '';
  const who = lim.session ? lim.session.split('--').pop() : '';
  return `Claude rate limit${kind} · resets ${Widgets.clock(lim.resets_at)}${who ? ' · ' + who : ''}`;
};

Widgets.limitBanner = function (st) {
  if (typeof document === 'undefined') return null;
  const banner = document.getElementById('banner');
  if (!banner) return null;
  const lim = Widgets.limit(st);
  const active = !!lim && lim.resets_at > 0 && lim.resets_at * 1000 > Date.now() && !Widgets.isDismissed(lim.resets_at);
  let node = Widgets.limitNode;
  if (!active) {
    if (node) node.remove();
    Widgets.limitNode = null;
    banner.classList.remove('limit-only');
    return null;
  }
  const text = Widgets.limitText(lim);
  if (!node || node.parentNode !== banner) {                    // first time, or renderBanner() emptied the banner
    if (node) node.remove();
    const label = el('span', { class: 'lc-text', text });
    node = el('div', { class: 'callout warn limit-callout', role: 'status' }, label,
      el('button', { class: 'minimal small', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: () => {
        Widgets.dismiss(node._resets);
        node.remove();
        Widgets.limitNode = null;
        banner.classList.remove('limit-only');
      } }, ic('cross')));
    node._label = label;
    banner.append(node);
    Widgets.limitNode = node;
  } else setTextIfChanged(node._label, text);
  node._resets = lim.resets_at;
  banner.classList.toggle('limit-only', banner.children.length === 1);   // alone in the banner: no second frame around the callout
  return node;
};
