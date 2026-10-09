/* ccboard usage page (v0.5.17, #/usage?range=24h|7d|30d): where the limits, the spend and the activity of the box are read. Seven sections in the
   order the usage analysis asked for (phone-heavy evenings, the live pain is the limit): Accounts (v0.5.17b: one row per Claude subscription account
   with its 5H and 7D gauges, then tokens, active hours, sessions and limit hits for the range, the API-equivalent dollars last and dim, a TOTAL row
   and the 'most room' line; a pencil renames an account), Limits (two gauges from the 3 s state, one line chart of rl_5h / rl_7d over the range
   with the limit episodes marked; with two or more accounts a chip per account picks whose windows the gauges and the chart show), Cost per day
   (stacked bars by project or agent), Sessions (the priciest ones, an Open button on a live session, a per-session ctx and cost chart under a row),
   Projects ($, active hours and $/h side by side), Activity (the 24x7 heatmap and the hour-of-day profile) and Timeline (a session Gantt of the
   last 24 h, 48 h on desktop).

   Definition only at load, apart from the registerPage call. The drawing is charts.js (window.Charts: line, stackedBars, gantt, heatmap and the
   formatters); every call here is guarded, so a charts.js that failed to load shows an inline error with Retry instead of a blank page.

   Data (all GETs, never from the 3 s state poll): /api/usage/summary?days=7|30 (cost, windows, sessions, heatmap, episodes, unpriced),
   /api/series?series=rl_5h,rl_7d&key=claude&since=<range> (key=acct:<key> once an account chip other than the current one is picked),
   /api/series/events?series=state&since=24h|48h, and per open live session
   /api/series?series=ctx,scost&key=<tmux>&since=24h. They are fetched in parallel on mount and on a range change, then every 60 s while the tab is
   visible. A response for a range or a page that is no longer current is cached and never painted (the route token). A range already seen paints
   at once from its cache and refreshes behind it; a new one paints a skeleton. uPlot is loaded lazily through Charts.ready(), only on this page.

   The accounts come from the same summary (accounts[], total, episodes[].acct, rate_limits.by_account): no extra polling; their labels are overlaid
   from state.accounts.list (the 3 s poll). The one write, PATCH /api/accounts/{key} {label}, belongs to the shared rename sheet of settings.js
   (settingsRenameAccount: the pencil of a row opens it).

   Codex (v0.5.12): with a Codex on the box the Limits section head carries a Claude | Codex switch (P.agent, ?agent=codex in the address, the last pick in localStorage).
   The Codex tab draws the gauges from state.usage_codex (primary / secondary, told apart by window_minutes: up to 360 is the 5H slot, from 7200 the 7D one; a plan that
   reports only the weekly window shows one gauge) and the chart from /api/series?series=rl_5h,rl_7d&key=codex (key=cacct:<key> once a Codex account chip other than the
   one in use is picked; its gauges then read the last point of that series). The Accounts section ends with a Codex block when state.codex_accounts exists: one row per
   Codex account (label in the teal of its agent, plan, current, 5H / 7D gauges) read from ONE series request of the page (rl_5h,rl_7d for key=cacct:<key>,...: P.cx), the
   account in use answering with the live state.usage_codex; tokens and dollars are the box's (the summary's windows.by_agent.codex), not an account's.

   Refresh (v0.5.17f, part 2): a quiet button beside the freshness caption under the Limits title. A tap asks the box for /usage (POST /api/usage/refresh: it types
   /usage into one idle Claude session, Claude Code fetches the official numbers with its own login, the board reads them from Claude Code's state file), then the
   page asks the state every 1.5 s until the newest reading is newer than the one it held at the tap (P.rf.base: the box's own clock on both sides, so a phone's
   clock does not matter) and repaints; 20 s without one reads 'no new reading yet'. A 409 shows its reason as a toast; with no live Claude session the button
   says 'start a session to refresh' and opens the launcher. The page also asks once on opening, by itself, when the newest reading is older than 5 minutes and a
   session nobody is attached to is at its prompt ({auto: true}: the server never picks an attached pane for it). Nothing here runs on a timer apart from the
   wait of a tap; the answer of an automatic ask is silent.

   Usage.cur is the mounted page record (null when not mounted); its .loading is the promise of the latest load, for the tests. */
'use strict';

const Usage = {
  RANGES: ['24h', '7d', '30d'],
  RANGE_KEY: 'ccboard:charts:range',               // localStorage: the last range (a ?range= in the address wins and is written back)
  STACK_KEY: 'ccboard:charts:stack',               // localStorage: the cost bars' stack (project | agent)
  BASIS_KEY: 'ccboard:usage:basis',                // localStorage: the dollar basis (reported | est), issue #95
  BASES: ['reported', 'est'],
  BASIS_NAME: { reported: 'Reported', est: 'Estimated' },
  AGENT_KEY: 'ccboard:usage:agent',                // localStorage: the provider tab of the Limits section (claude | codex)
  AGENTS: ['claude', 'codex'],
  CX_WIN: { five: 360, seven: 7200 },              // a Codex window of at most 360 minutes is the 5H slot, one of at least 7200 the 7D slot, anything between none
  CX_ACCT_MAX: 8,                                  // the series endpoint takes at most 8 series x key combinations per request
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
  UNATTRIBUTED_TIP: 'sessions the board did not start, with no folder on record',
  OUTSIDE: '(outside projects)',
  OUTSIDE_TIP: 'sessions that ran in a folder outside the projects folder',
  JOINED_TIP: 'matched to this project by the folder the session ran in (Claude Code\'s own session files); the board did not start these',
  UNKNOWN: 'unknown',                              // the summary's row for history from before the board tracked accounts
  HOT_PCT: 85,                                     // an account window at or above this is 'in trouble' (the bad tone, and the cue to use another account)
  REFRESH_WAIT_MS: 20000,                          // a tap on Refresh waits this long for a newer reading before it says 'no new reading yet'
  REFRESH_POLL_MS: 1500,                           // ... and asks the state this often meanwhile
  REFRESH_STALE_S: 300,                            // opening the page asks for a refresh by itself only when the newest reading is older than this
  REFRESH_SHELLS: ['sh', 'bash', 'zsh', 'fish', 'dash', 'ksh', 'tcsh', 'csh'],   // a pane back at its login shell is no Claude (claude_auth.SHELLS)
  SECTIONS: [
    ['accounts', 'Accounts', 'subscription windows (statusline) · API-equivalent $ last'],
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
Usage.storedBasis = function () {
  try { const b = localStorage.getItem(Usage.BASIS_KEY); if (b === 'reported' || b === 'est') return b; } catch (_) { /* storage may be unavailable */ }
  return 'reported';
};
Usage.saveBasis = function (b) { try { localStorage.setItem(Usage.BASIS_KEY, b); } catch (_) { /* storage may be unavailable */ } };
Usage.storedStack = function () {
  try { const s = localStorage.getItem(Usage.STACK_KEY); if (s === 'project' || s === 'agent') return s; } catch (_) { /* storage may be unavailable */ }
  return 'project';
};

Usage.storedAgent = function () {
  try { const a = localStorage.getItem(Usage.AGENT_KEY); if (Usage.AGENTS.includes(a)) return a; } catch (_) { /* storage may be unavailable */ }
  return 'claude';
};
Usage.saveAgent = function (a) { try { localStorage.setItem(Usage.AGENT_KEY, a); } catch (_) { /* storage may be unavailable */ } };
Usage.routeAgent = function (route) { const a = route && route.query ? route.query.agent : null; return Usage.AGENTS.includes(a) ? a : null; };

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

Usage.approx = false;     // true while an Estimated-basis section is painted (Usage.safe sets it): every dollar then carries a `~`, because it includes list-price estimates
Usage.usd = function (v) {
  const f = Usage.charts('fmtUsd');
  const n = Usage.num(v);
  return (Usage.approx ? '~' : '') + (f ? String(f(n)) : '$' + (n >= 100 ? String(Math.round(n)) : n.toFixed(2)));
};
Usage.tok = function (v) {
  const f = Usage.charts('fmtTok');
  const n = Usage.num(v);
  if (f) return String(f(n));
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(Math.round(n));
};
Usage.hours = function (v) { return `${Usage.num(v).toFixed(1)} h`; };
Usage.model = function (m) { const f = Usage.charts('shortModel'); const s = String(m === null || m === undefined ? '' : m); return f ? String(f(s)) : s.replace(/^claude-/, ''); };

/* The muted hue class of a model, project or account chip (pages/agents.js chipHue), '' when that file is not there. */
Usage.hue = function (kind, key) { return typeof chipHue === 'function' ? chipHue(kind, key) : ''; };

/* The caption of the cost chart: '<window>: $ · tokens · hours' ('Last 7 days · today: ...' for 24h, which still draws a week of bars). */
Usage.caption = function (sum, range) { return `${Usage.CAPTION_NAME[range]}${range === '24h' ? ' ' : ': '}${Usage.totalsText(sum, range)}`; };

Usage.sig = function (v) { try { return JSON.stringify(v); } catch (_) { return String(Math.random()); } };

Usage.go = function (hash) { if (typeof navigate === 'function') navigate(hash); else if (typeof location !== 'undefined') location.hash = hash; };

/* The two labels that are not projects: no folder on record, or a folder outside the projects folder. Each has its one-line meaning. */
Usage.isUnassigned = function (project) { return project === Usage.UNATTRIBUTED || project === Usage.OUTSIDE; };
Usage.unassignedTip = function (project) { return project === Usage.OUTSIDE ? Usage.OUTSIDE_TIP : project === Usage.UNATTRIBUTED ? Usage.UNATTRIBUTED_TIP : ''; };

/* #/p/<project>, or null when the name is not a route param ('(unattributed)', a dot or a space in it: buildHash throws). */
Usage.projectHash = function (project) {
  if (!project || project === Usage.UNATTRIBUTED || project === Usage.OUTSIDE) return null;
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
    setText(reset, w.reset_text || (eta ? `resets in ${eta} · ${Usage.clock(w.resets_at)}` : ''));
    bar.setAttribute('aria-label', `${label} ${Math.round(pct)}% used`);
    root.setAttribute('title', w.title || (`${label} window: ${Math.round(pct)}% used` + (w.resets_at ? ` · resets ${Usage.clock(w.resets_at)}` : '')));
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

/* {pct, resets_at, at, source, rolled, last, title} of one window ('5h' | '7d') for Usage.gauge.set, null without a numeric reading. Time-aware like every other surface
   (core.js limitWindowNow): a window whose reset has passed with no newer reading reads 0 % and counts down to the next reset, and its title says that no usage
   was recorded since the reset; every title ends with when the reading was taken and where it came from. `who` leads the title ('5H window', 'Work 5H'). */
Usage.windowReading = function (win, pct, resets, at, source, who) {
  const x = limitWindowNow({ pct, resets_at: resets, at, source }, LIMIT_PERIOD[win], Date.now() / 1000);
  if (!x) return null;
  const caption = limitCaption(x);
  return { pct: x.pct, resets_at: x.resets_at, at: x.at, source: x.source, rolled: x.rolled, last: x.last,
    title: `${who}: ${Math.round(x.pct)}% used${x.resets_at ? ' · resets ' + Usage.clock(x.resets_at) : ''}${caption ? ' · ' + caption : ''}` };
};

/* ---------- refresh (v0.5.17f, part 2) ---------- */

/* Every session of a state as one list (project roots, repos and orphans). */
Usage.sessionList = function (st) {
  const out = [];
  for (const p of ((st && st.projects) || [])) {
    if (p && p.root) out.push(...(p.root.sessions || []));
    for (const r of ((p && p.repos) || [])) out.push(...((r && r.sessions) || []));
    out.push(...((p && p.orphan_sessions) || []));
  }
  return out.filter((s) => s && typeof s === 'object');
};

/* What a refresh could ask, read from the state the way the server reads it (app/usage_refresh.py, the command route's guards): {live: Claude panes that
   run something other than a login shell, ready: of those the ones at their prompt (idle, done, errored or idle-waiting; no permission pending; not compacting),
   free: of those the ones nobody is attached to}. The server decides; this only words the button and gates the automatic ask. */
Usage.refreshTargets = function (st) {
  const perm = new Set(((st && st.pending_permissions) || []).map((p) => p && p.tmux_name));
  let live = 0;
  let ready = 0;
  let free = 0;
  for (const s of Usage.sessionList(st)) {
    if (s.agent !== 'claude') continue;
    const cmd = String(s.command || '').replace(/^-/, '');
    if (!cmd || Usage.REFRESH_SHELLS.includes(cmd)) continue;
    live += 1;
    const flags = s.flags || {};
    const atPrompt = ['idle', 'done', 'errored'].includes(s.state) || (s.state === 'waiting' && flags.wait_kind === 'idle');
    if (!atPrompt || flags.compacting || perm.has(s.tmux)) continue;
    ready += 1;
    if (!(s.viewers && s.viewers.full > 0)) free += 1;
  }
  return { live, ready, free };
};

/* Epoch seconds of the newest Claude reading in a state (the record's time and each window's own), 0 when there is none. */
Usage.newestReading = function (st) {
  const u = st && st.usage;
  if (!u || typeof u !== 'object') return 0;
  const rl = u.value || {};
  const times = [Usage.epoch(u.at)];
  for (const w of [rl.five_hour, rl.seven_day]) if (w && typeof w === 'object') times.push(Usage.epoch(w.at));
  return Math.max(...times);
};

/* The empty refresh record of a page: on = a tap (or the opening ask) is waiting for its reading, base = the newest reading's epoch when it was asked,
   timer / poll = the give-up and the state polls, note = the line next to the button ('no new reading yet'). */
Usage.refreshFresh = function () { return { on: false, base: 0, timer: null, poll: null, note: '' }; };

Usage.refreshBusy = function (P) { return !!(P.rf.on || (P.st && P.st.usage_refresh && P.st.usage_refresh.running)); };

/* The button and its note follow the state: 'Refresh', 'refreshing…' (disabled; a refresh another device asked for counts too) or 'start a session to refresh'. */
Usage.refreshPaint = function (P) {
  const R = P.refs;
  if (!R.refresh) return;
  const busy = Usage.refreshBusy(P);
  const none = !busy && !!P.st && Usage.refreshTargets(P.st).live === 0;
  const title = busy ? 'Asking a Claude session for /usage…'
    : none ? 'No Claude session is running: start one, then refresh'
      : "Ask an idle Claude session for /usage: Claude Code fetches the official numbers with its own login, and the board reads them from Claude Code's state file";
  setText(R.refresh, busy ? 'refreshing…' : none ? 'start a session to refresh' : 'Refresh');
  if (R.refresh.disabled !== busy) R.refresh.disabled = busy;                         // the poll repaints every 3 s: touch an attribute only when it changed (a browser drops an open tooltip on any write)
  if (R.refresh.getAttribute('aria-busy') !== String(busy)) R.refresh.setAttribute('aria-busy', String(busy));
  if (R.refresh.getAttribute('title') !== title) R.refresh.setAttribute('title', title);
  R.refresh.classList.toggle('hidden', !!P.limAcct);                                  // a chip picked another account: the caption is about that account's reading, and a refresh asks the account in use
  setText(R.refreshNote, P.rf.note);
  R.refreshNote.classList.toggle('hidden', !P.rf.note || busy || !!P.limAcct);
};

/* A wait is over: its timers go, the note says why when it gave up, and a reading that arrived repaints the limits (the chart and the summary refetch behind). */
Usage.refreshEnd = function (P, note, arrived) {
  if (P.rf.timer !== null) clearTimeout(P.rf.timer);
  if (P.rf.poll !== null) clearTimeout(P.rf.poll);
  P.rf.timer = null;
  P.rf.poll = null;
  P.rf.on = false;
  P.rf.note = note || '';
  if (!Usage.alive(P)) return;
  if (arrived) { Usage.paintGauges(P); Usage.load(P, { fresh: true }); } else Usage.refreshPaint(P);
};

/* update(): did the reading a tap waits for arrive? Newer than the one held at the tap, from either source (a statusline that spoke meanwhile is as good). */
Usage.refreshCheck = function (P) {
  if (P.rf.on && Usage.newestReading(P.st) > P.rf.base) Usage.refreshEnd(P, '', true);
};

/* The state asked for every REFRESH_POLL_MS while a wait is on (poll(true) is core.js's own fetch; update() sees the answer). */
Usage.refreshWatch = function (P) {
  const step = () => {
    P.rf.poll = null;
    if (!Usage.alive(P) || !P.rf.on) return;
    if (typeof poll === 'function') Promise.resolve(poll(true)).catch(() => { /* the next step asks again */ });
    P.rf.poll = setTimeout(step, Usage.REFRESH_POLL_MS);
  };
  P.rf.poll = setTimeout(step, Usage.REFRESH_POLL_MS);
};

Usage.startSession = function () {
  if (typeof Shell === 'undefined' || !Shell || typeof Shell.openCreate !== 'function') return false;
  return Shell.openCreate('session') !== false;
};

/* One refresh. A tap (auto false) with no live Claude session opens the launcher instead; a 409 toasts its reason. auto (the page opening on stale
   numbers) is silent when it is refused. Resolves true when the box took the ask. */
Usage.refresh = function (P, auto) {
  if (!Usage.alive(P) || Usage.refreshBusy(P)) return Promise.resolve(false);
  if (!auto && P.st && Usage.refreshTargets(P.st).live === 0) { Usage.startSession(); return Promise.resolve(false); }
  P.rf.on = true;
  P.rf.base = Usage.newestReading(P.st);
  P.rf.note = '';
  P.rf.timer = setTimeout(() => { if (Usage.alive(P) && P.rf.on) Usage.refreshEnd(P, 'no new reading yet', false); }, Usage.REFRESH_WAIT_MS);
  Usage.refreshPaint(P);
  return Promise.resolve().then(() => api('POST', '/api/usage/refresh', auto ? { auto: true } : {})).then(() => {
    if (Usage.alive(P) && P.rf.on) Usage.refreshWatch(P);
    return true;
  }, (e) => {
    Usage.refreshEnd(P, '', false);
    if (!auto && typeof toast === 'function') toast(Usage.errText(e), { kind: 'warn' });
    return false;
  });
};

/* Once per page open, from the first state: ask by itself when the newest reading is older than REFRESH_STALE_S and a session nobody is attached to is at its prompt. */
Usage.refreshAuto = function (P) {
  if (P.autoDone || !P.st) return;
  P.autoDone = true;
  const newest = Usage.newestReading(P.st);
  const stale = !newest || Date.now() / 1000 - newest > Usage.REFRESH_STALE_S;
  if (stale && Usage.refreshTargets(P.st).free > 0) Usage.refresh(P, true);
};

/* ---------- page state ---------- */

Usage.alive = function (P) { return !!P && !P.dead && Usage.cur === P; };
Usage.entry = function (P) { return P.cache[P.range] || null; };
/* The summary of the page's basis: c.summary (reported) or c.summaryEst (estimated, ?basis=est); each is fetched when first wanted. */
Usage.rawSummary = function (P) { const c = Usage.entry(P); return c ? (P.basis === 'est' ? c.summaryEst : c.summary) : null; };
Usage.summaryOf = function (P) { const s = Usage.rawSummary(P); return s && Array.isArray(s.daily) ? s : null; };
Usage.errKey = function (P) { return P.basis === 'est' ? 'summaryEst' : 'summary'; };
Usage.sumDone = function (P) { const c = Usage.entry(P); return !!c && (P.basis === 'est' ? c.sumEstDone : c.sumDone); };

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
  Usage.approx = P.basis === 'est';
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
  const P = { range, token: 0, dead: false, route, st: null, cache: {}, open: new Set(), details: new Map(), stack: Usage.storedStack(), sigs: {}, basis: Usage.storedBasis(),
    refs: { body: {}, prov: {}, toggle: {}, stackBtn: {}, basisBtn: {} }, timer: null, onVisible: null, unbind: null, stale: false, liveSig: '', showAll: false,
    events: null, evErr: '', evDone: false, loading: Promise.resolve(), ready: null, rows: new Map(), limDrawn: false,
    limAcct: null, accSeq: 0,                                 // limAcct: the account the Limits section shows (null = the current one, via key=claude)
    rf: Usage.refreshFresh(), autoDone: false,                // v0.5.17f: the Refresh button's wait, and the once-per-open automatic ask
    agent: 'claude', agentNow: 'claude', cxAcct: null,        // v0.5.12: the provider tab, the one it last painted, and the Codex account its chip picked (null = the one in use, key=codex)
    cx: { acc: {}, seq: 0, done: false, err: '', keys: '' } };  // the Codex accounts' own readings (one series request for all of them)
  const R = P.refs;
  const fromAgent = Usage.routeAgent(route);
  P.agent = fromAgent || Usage.storedAgent();
  if (fromAgent) Usage.saveAgent(fromAgent);
  P.agentNow = Usage.agent(P);

  const seg = el('div', { class: 'useg pj-switch', role: 'group', 'aria-label': 'Time range' });
  for (const r of Usage.RANGES) {
    R.toggle[r] = el('button', { class: 'small useg-btn pj-repo-btn', type: 'button', 'data-range': r, 'aria-pressed': r === range ? 'true' : 'false',
      title: `Show the last ${r === '24h' ? '24 hours' : r === '7d' ? '7 days' : '30 days'} (r cycles)`, text: r, onclick: () => Usage.setRange(P, r) });
    seg.append(R.toggle[r]);
  }
  R.alert = el('div', { class: 'callout warn ualert hidden', role: 'alert' });
  // the provider tab of the Limits section: only with a Codex on the box (Usage.paintAgentSeg shows it)
  R.agentSeg = el('div', { class: 'useg pj-switch hidden', role: 'group', 'aria-label': 'Provider of the limits', 'data-seg': 'agent' });
  R.agentBtn = {};
  for (const a of Usage.AGENTS) {
    R.agentBtn[a] = el('button', { class: 'small useg-btn pj-repo-btn', type: 'button', 'data-agent': a, 'aria-pressed': a === Usage.agent(P) ? 'true' : 'false',
      title: `Show the ${a === 'codex' ? 'Codex' : 'Claude'} limits`, text: a === 'codex' ? 'Codex' : 'Claude', onclick: () => Usage.setAgent(P, a) });
    R.agentSeg.append(R.agentBtn[a]);
  }
  // the dollar basis (issue #95): Reported (ccusage's own dollars) or Estimated (the same plus list-price estimates for models it prices at zero). A segmented control
  // with the other filters; arrow keys move it, and it never remounts the page: the range, the provider tab and the scroll stay where they are.
  const bseg = el('div', { class: 'useg pj-switch', role: 'group', 'aria-label': 'Dollar basis', 'data-seg': 'basis' });
  for (const b of Usage.BASES) {
    R.basisBtn[b] = el('button', { class: 'small useg-btn pj-repo-btn', type: 'button', 'data-basis': b, 'aria-pressed': b === P.basis ? 'true' : 'false',
      title: b === 'est' ? 'Reported dollars plus list-price estimates for models ccusage prices at zero' : 'The dollars ccusage reports', text: Usage.BASIS_NAME[b], onclick: () => Usage.setBasis(P, b) });
    bseg.append(R.basisBtn[b]);
  }
  bseg.addEventListener('keydown', (e) => {
    if (!e || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    if (typeof e.preventDefault === 'function') e.preventDefault();
    const next = e.key === 'ArrowRight' ? 'est' : 'reported';
    Usage.setBasis(P, next);
    if (R.basisBtn[next] && typeof R.basisBtn[next].focus === 'function') R.basisBtn[next].focus();
  });
  const head = el('div', { class: 'page-head' }, el('h1', { text: 'Usage' }), el('div', { class: 'actions' }, bseg, seg));

  // Limits: the gauges and their note, the slot for an empty or error block, the chart host Charts.line owns (kept, never rebuilt)
  R.g5 = Usage.gauge('5H');
  R.g7 = Usage.gauge('7D');
  R.gnote = el('p', { class: 'dim unote hidden' });
  R.gfresh = el('p', { class: 'dim ufresh hidden', 'data-fresh': '' });        // v0.5.17f: when the gauges were last read and where from, right under the Limits title
  R.refresh = el('button', { class: 'small ufresh-btn', type: 'button', 'data-refresh': '', text: 'Refresh', onclick: () => Usage.refresh(P, false) });   // beside it: a quiet bordered button, never a primary
  R.refreshNote = el('p', { class: 'dim ufresh-note hidden', 'data-refresh-note': '', role: 'status' });
  R.freshRow = el('div', { class: 'ufresh-row' }, R.gfresh, R.refresh, R.refreshNote);
  R.limAcct = el('div', { class: 'ua-picks pj-switch hidden', role: 'group', 'aria-label': 'Account shown in the limits' });
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
    if (id === 'limits') kids.push(R.agentSeg);
    const body = el('div', { class: 'ubody', 'data-body': id });
    R.body[id] = body;
    if (id === 'limits') body.append(R.freshRow, R.limAcct, el('div', { class: 'uc-gauges' }, R.g5, R.g7), R.gnote, R.limSlot, R.limHost, R.limCap);
    else body.append(Usage.skeleton());
    sections.push(el('section', { class: 'usec', 'data-sec': id }, el('div', { class: 'usec-head' }, ...kids), body));
  }
  R.foot = el('p', { class: 'dim unote ufoot', 'data-foot': '' });              // API-equivalent, not an invoice, and the price table's date (issue #95)
  R.mem = el('div', { class: 'mem-usage hidden' });                              // the claude-mem health tile (pages/memory.js), after the work sections; hidden without state.memory
  // ubooting (charts.css): until the first summary is in, only the Accounts section shows; the sections under it would be on screen as skeletons and then jump down as it grows (#103)
  R.page = el('div', { class: 'usage-page ubooting', 'data-usage': '' }, head, R.alert, el('div', { class: 'ugrid' }, ...sections), R.foot, R.mem);
  root.append(R.page);   // not 'usage': style.css owns that legacy strip rule
  return P;
};

/* The claude-mem health tile (v0.5.20, pages/memory.js memoryHealthTile, the compact one): state.memory from the 3 s poll, nothing fetched here. Hidden when claude-mem is off
   or not sampled yet; rebuilt only when what it shows changed. */
Usage.paintMem = function (P) {
  const host = P.refs && P.refs.mem;
  if (!host) return;
  const st = P.st || (typeof state !== 'undefined' ? state : null);
  const m = st && st.memory;
  const sig = m ? JSON.stringify([m.state, m.observations, m.queue_depth, m.processing, m.last_error, m.rates, m.reason, m.version]) : '';
  if (sig === P.memSig) return;
  P.memSig = sig;
  host.textContent = '';
  const tile = m && typeof memoryHealthTile === 'function' ? memoryHealthTile(m, { compact: true, link: true }) : null;
  if (tile) host.append(tile);
  host.classList.toggle('hidden', !tile);
};

/* ---------- range and stack ---------- */

Usage.syncBasis = function (P) {
  for (const b of Usage.BASES) if (P.refs.basisBtn[b]) P.refs.basisBtn[b].setAttribute('aria-pressed', b === P.basis ? 'true' : 'false');
};

/* The dollar basis changes at once: remember it, repaint the summary sections from that basis's summary (or a skeleton while it loads). Nothing else moves. */
Usage.setBasis = function (P, basis) {
  if (!Usage.alive(P) || (basis !== 'reported' && basis !== 'est') || basis === P.basis) return;
  P.basis = basis;
  Usage.saveBasis(basis);
  Usage.syncBasis(P);
  for (const id of ['accounts', 'cost', 'sessions', 'projects']) P.sigs[id] = '';
  const c = Usage.entry(P);
  if (!c) return;                                                               // the first load is still on its way and paints the basis the page holds by then
  if (Usage.rawSummary(P)) {
    Usage.paintSummary(P);
    if (Date.now() - (basis === 'est' ? c.atEst || 0 : c.at) > Usage.STALE_MS) Usage.fetchBasis(P);
  } else {
    for (const id of ['accounts', 'cost', 'sessions', 'projects']) Usage.setBody(P, id, Usage.skeleton());
    Usage.fetchBasis(P);
  }
};

/* Only the summary of the current basis (a switch to a basis not fetched yet, or one gone stale): it paints when it arrives if the page still holds that basis. */
Usage.fetchBasis = function (P) {
  const c = Usage.entry(P);
  if (!c) return Promise.resolve();
  const basis = P.basis;
  const p = Usage.fetchSummary(P, c, basis, false).then(() => { if (Usage.alive(P) && P.basis === basis) Usage.paintSummary(P); });
  P.loading = p;
  return p;
};

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
    const other = c.serAcct !== Usage.scope(P);                                  // its limit series is another account's (or provider's): fetch this one's (series only)
    if (other) { c.series = null; c.serDone = false; delete c.err.series; }
    Usage.paintAll(P);
    if (Date.now() - c.at > Usage.STALE_MS) Usage.load(P);                      // old enough: refresh behind the paint
    else if (other) Usage.reloadSeries(P);
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
    if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('usage', {}, { range, agent: Usage.agent(P) === 'codex' ? 'codex' : null }), { replace: true });
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
  const c = P.cache[range] || (P.cache[range] = { summary: null, series: null, at: 0, err: {}, sumDone: false, serDone: false, serAcct: Usage.scope(P) });
  if (c.serAcct !== Usage.scope(P)) { c.series = null; c.serDone = false; delete c.err.series; }
  const current = () => Usage.alive(P) && P.token === token;
  const hours = Usage.wide() ? 48 : 24;
  c.hours = hours;
  const get = (path) => Usage.get(path, o.fresh);
  const evs = get(`/api/series/events?series=state&since=${hours}h`);

  const sSum = Usage.fetchSummary(P, c, P.basis, o.fresh).then(() => { if (current()) Usage.paintSummary(P); });
  const sSer = Usage.fetchSeries(P, c, o.fresh);
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
  const sCx = Usage.fetchCx(P, o.fresh);                                         // the Codex accounts' readings (nothing without a Codex account)
  const done = Promise.all([sLim, sEv, sCx]).then(() => {
    if (current()) { Usage.paintAlert(P); Usage.refreshDetails(P, o.fresh); }
  });
  P.loading = done;
  return done;
};

/* The usage summary of one basis into its slot of the range's cache entry (c.summary / c.summaryEst), with its own done flag and error. Never paints. */
Usage.summaryPath = function (P, basis) { return `/api/usage/summary?days=${Usage.days(P.range)}&tz_min=${Usage.tzMin()}${basis === 'est' ? '&basis=est' : ''}`; };
Usage.fetchSummary = function (P, c, basis, fresh) {
  const est = basis === 'est';
  const slot = est ? 'summaryEst' : 'summary';
  return Usage.get(Usage.summaryPath(P, basis), fresh).then((v) => {
    if (v && typeof v === 'object') { c[slot] = v; if (est) c.atEst = Date.now(); else c.at = Date.now(); delete c.err[slot]; } else c.err[slot] = 'empty answer';
  }, (e) => { c.err[slot] = Usage.errText(e); }).then(() => { if (est) c.sumEstDone = true; else c.sumDone = true; });
};

/* The limit series of the page's account for c's range (key=claude follows the current account; acct:<key> is one account's own windows). It
   fills c.series / c.serDone, never paints, and drops its answer when the account was changed while it was on its way. */
Usage.seriesPath = function (P) {
  const codex = Usage.agent(P) === 'codex';                                      // the Codex tab: key=codex, or one Codex account's own windows (cacct:<key>)
  const key = codex ? (P.cxAcct ? 'cacct:' + encodeURIComponent(P.cxAcct) : 'codex') : (P.limAcct ? 'acct:' + encodeURIComponent(P.limAcct) : 'claude');
  return `/api/series?series=rl_5h,rl_7d&key=${key}&since=${P.range}&points=${Usage.points(P)}`;
};

Usage.fetchSeries = function (P, c, fresh) {
  const acct = Usage.scope(P);
  c.serAcct = acct;
  return Usage.get(Usage.seriesPath(P), fresh).then((v) => {
    if (c.serAcct !== acct) return;
    if (v && typeof v === 'object') { c.series = v; c.at = Date.now(); delete c.err.series; } else c.err.series = 'empty answer';
  }, (e) => { if (c.serAcct === acct) c.err.series = Usage.errText(e); }).then(() => { if (c.serAcct === acct) c.serDone = true; });
};

/* Only the series (the account changed, or a cached range holds another account's): fetch it and repaint the limits. P.loading follows it. */
Usage.reloadSeries = function (P, fresh) {
  const c = Usage.entry(P);
  if (!c) return Promise.resolve();
  const token = P.token;
  const seq = P.accSeq;
  if (c.serAcct !== Usage.scope(P)) { c.series = null; c.serDone = false; delete c.err.series; }
  const done = Usage.fetchSeries(P, c, fresh).then(() => {
    if (Usage.alive(P) && P.token === token && P.accSeq === seq) Usage.safe(P, 'limits', () => Usage.paintLimits(P));
  });
  P.loading = done;
  return done;
};

Usage.retry = function (P) {
  if (!Usage.alive(P)) return;
  P.ready = Usage.ready();
  const c = Usage.entry(P);
  if (c) { c.err = {}; c.sumDone = !!c.summary; c.sumEstDone = !!c.summaryEst; c.serDone = !!c.series; }
  P.evErr = '';
  P.sigs = {};
  Usage.paintSkeleton(P);
  Usage.load(P, { fresh: true });
};

/* ---------- paint ---------- */

Usage.paintSkeleton = function (P) {
  for (const id of ['accounts', 'cost', 'sessions', 'projects', 'activity']) Usage.setBody(P, id, Usage.skeleton());
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
  if (P.refs.page && (Usage.summaryOf(P) || Usage.sumDone(P))) P.refs.page.classList.remove('ubooting');          // the first answer (or its error) is in: the rest of the page takes its place at once
  Usage.checkAccount(P);
  Usage.paintFoot(P);
  Usage.paintGauges(P);
  Usage.safe(P, 'accounts', () => Usage.paintAccounts(P));
  Usage.safe(P, 'cost', () => Usage.paintCost(P));
  Usage.safe(P, 'sessions', () => Usage.paintSessions(P));
  Usage.safe(P, 'projects', () => Usage.paintProjects(P));
  Usage.safe(P, 'activity', () => Usage.paintActivity(P));
};

/* A summary-based section without its data: a skeleton while the fetch runs, the error block when it failed, else the empty state (title, hint). */
Usage.noSummary = function (P, id, title, hint) {
  const c = Usage.entry(P);
  P.sigs[id] = '';
  if (!c || !Usage.sumDone(P)) { Usage.setBody(P, id, Usage.skeleton()); return; }
  if (c.err[Usage.errKey(P)]) { Usage.setBody(P, id, Usage.errorBlock(P, `Could not load the usage summary: ${c.err[Usage.errKey(P)]}`)); return; }
  Usage.setBody(P, id, Usage.empty(title, hint, id === 'cost' || id === 'accounts'));
};

Usage.paintAlert = function (P) {
  const c = Usage.entry(P);
  const parts = [];
  const msgs = [];
  if (c && c.err[Usage.errKey(P)] && Usage.rawSummary(P)) { parts.push('the usage summary'); msgs.push(c.err[Usage.errKey(P)]); }
  if (c && c.err.series && c.series) { parts.push('the limit series'); msgs.push(c.err.series); }
  if (P.evErr && P.events) { parts.push('the timeline'); msgs.push(P.evErr); }
  const a = P.refs.alert;
  a.classList.toggle('hidden', !parts.length);
  a.textContent = '';
  if (!parts.length) return;
  a.append(el('span', { text: `Could not refresh ${parts.join(', ')}: ${msgs[0]}. Showing the last data.` }),
    el('button', { class: 'small', type: 'button', text: 'Retry', onclick: () => Usage.retry(P) }));
};

/* The freshness caption under the Limits title: when the gauges were last read and where from ('updated 5m ago · from the last session', '... · from Claude Code's
   cache'), or 'no reading yet'. The newest of the two readings speaks; a reading that carries no time says nothing. null (the Codex tab, whose note gives the age) hides it. */
Usage.paintFresh = function (P, readings) {
  const node = P.refs.gfresh;
  if (!node) return;
  const rs = readings ? readings.filter(Boolean) : null;
  let text = '';
  if (rs && !rs.length) text = limitFreshness(0);
  else if (rs) { const best = rs.reduce((a, b) => ((b.at || 0) > (a.at || 0) ? b : a)); text = best.at ? limitFreshness(best.at, best.source) : ''; }
  node.classList.toggle('hidden', !text);
  setText(node, text);
  P.refs.freshRow.classList.toggle('hidden', !rs);                       // the Codex tab (null readings) has neither the caption nor the Claude refresh
  Usage.refreshPaint(P);
};

/* The two gauges follow the state on every update(); with no live reading they fall back to the summary's last statusline sample. */
Usage.paintGauges = function (P) {
  const R = P.refs;
  Usage.paintAgentSeg(P);
  Usage.paintPicks(P);
  if (Usage.agent(P) === 'codex') { Usage.paintCodexGauges(P); return; }
  const chosen = P.limAcct ? Usage.accounts(P).find((a) => a.key === P.limAcct) : null;
  if (chosen) { Usage.paintAccountGauges(P, chosen); return; }
  const u = P.st && P.st.usage;
  const rl = (u && u.value) || {};
  const live = (raw, win) => {                                                   // the pills' own record: a window whose reset has passed rolls over instead of going missing
    const r = Usage.reading(raw);
    return r ? Usage.windowReading(win, r.pct, r.resets_at, Usage.epoch(raw.at) || Usage.epoch(u && u.at), raw.source || rl.source, `${win === '5h' ? '5H' : '7D'} window`) : null;
  };
  let five = live(rl.five_hour, '5h');
  let seven = live(rl.seven_day, '7d');
  let note = '';
  const sum = Usage.summaryOf(P);
  const lim = sum && sum.rate_limits && sum.rate_limits.claude;
  if ((!five || !seven) && lim) {
    const from = (r, win) => (r && typeof r.value === 'number' ? Usage.windowReading(win, r.value, Usage.epoch(r.meta && r.meta.resets_at), Usage.epoch(r.at), r.meta && r.meta.source, `${win === '5h' ? '5H' : '7D'} window`) : null);
    const f5 = from(lim.rl_5h, '5h');
    const f7 = from(lim.rl_7d, '7d');
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
  Usage.paintFresh(P, [five, seven]);
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
  if (!c || !c.serDone || !c.sumDone || c.serAcct !== Usage.scope(P)) { Usage.limitsShow(P, 'loading'); return; }
  const codex = Usage.agent(P) === 'codex';
  const empty = () => {
    P.sigs.limits = '';
    if (c.err.series && !c.series) { Usage.limitsShow(P, 'slot', Usage.errorBlock(P, `Could not load the limit series: ${c.err.series}`)); return; }
    if (codex) {
      const cxWho = P.cxAcct ? Usage.cxAccts(Usage.stOf(P)).find((a) => a.key === P.cxAcct) : null;
      if (cxWho) { Usage.limitsShow(P, 'slot', Usage.empty(`No samples for ${cxName(cxWho)} in this range`, 'Readings arrive while a Codex session runs on that account. Pick a longer range or another account.', false)); return; }
      Usage.limitsShow(P, 'slot', Usage.empty('No Codex samples yet', 'The board reads the limits from the rollout of a Codex session: start one with + session and the history appears here.', true));
      return;
    }
    const who = P.limAcct ? Usage.accounts(P).find((a) => a.key === P.limAcct) : null;
    if (who) { Usage.limitsShow(P, 'slot', Usage.empty(`No samples for ${Usage.accName(who)} in this range`, 'Readings arrive while a session runs on that account. Pick a longer range or another account.', false)); return; }
    Usage.limitsShow(P, 'slot', Usage.empty('No samples yet', 'The board records usage from its first hook event: start a session and the 5H and 7D history appears here.', true));
  };
  const data = c.series;
  const names = data && data.series ? Object.keys(data.series).filter((k) => Usage.limName(k, P)) : [];
  if (!Usage.hasData(data, names)) { empty(); return; }
  const sum = Usage.summaryOf(P);
  const since = Usage.epoch(data.since) || data.t[0];
  const until = Usage.epoch(data.until) || data.t[data.t.length - 1];
  const marks = [];
  const resets = [];
  const seen = new Set();
  const reset = (t) => { const k = Math.round(t); if (t >= since && t <= until && !seen.has(k)) { seen.add(k); resets.push({ t, label: `resets ${Usage.clock(t)}` }); } };
  const only = Usage.limitAccount(P);                                           // with two accounts or more the chart shows one account's hits only
  if (!codex && sum && Array.isArray(sum.episodes)) {                           // limit hits are Claude's (the statusline / hook episodes); a Codex window only has its resets
    for (const ep of sum.episodes) {
      if (only && Usage.str(ep && ep.acct) !== only) continue;
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
  setText(P.refs.limCap, `dashed lines: 60% warn, 85% critical · ticks mark ${codex ? 'window resets' : 'limit hits and window resets'}`);
  const acct = Usage.scope(P);
  Promise.resolve(P.ready || (P.ready = Usage.ready())).then((ok) => {
    if (!Usage.alive(P) || Usage.entry(P) !== c || Usage.scope(P) !== acct) return;      // another range, account or provider was picked while uPlot loaded
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
  const sig = Usage.sig([P.range, P.stack, daily, sum.unpriced, sum.windows, P.basis, sum.estimate]);
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
  const out = Usage.windowRows(sum, P.range).find((r) => r.project === Usage.OUTSIDE);
  if (P.stack === 'project' && out && Usage.num(out.total) > 0) {
    kids.push(el('p', { class: 'dim unote', 'data-note': 'outside', title: Usage.OUTSIDE_TIP,
      text: `${Usage.OUTSIDE} ${Usage.usd(out.total)} in this window: ${Usage.OUTSIDE_TIP}.` }));
  }
  const n = Array.isArray(sum.unpriced) ? sum.unpriced.length : 0;
  if (n) {
    const names = [...new Set(sum.unpriced.map((u) => u && u.project).filter(Boolean))].join(', ');
    kids.push(el('p', { class: 'warn unote', 'data-note': 'unpriced', title: names ? `projects: ${names}` : null,
      text: P.basis === 'est' ? `${n} session${n === 1 ? ' has' : 's have'} tokens but no price, even as an estimate (hatched): the cost shown is a floor.`
        : `${n} session${n === 1 ? ' has' : 's have'} tokens but no price (hatched): the cost shown is a floor.` }));
  }
  const est = Usage.estimateInfo(sum);
  if (P.basis === 'est') kids.push(Usage.estimateDetails(est));
  else if (est.sessions > 0) {
    kids.push(el('p', { class: 'dim unote', 'data-note': 'est-available', title: `Estimated from the list prices of ${est.date}`,
      text: `${Usage.plural(est.sessions, 'session')} on models ccusage prices at zero ${est.sessions === 1 ? 'is' : 'are'} not in these dollars. Estimated adds about ${Usage.usdOf(est.usd, true)}.` }));
  }
  Usage.setBody(P, 'cost', ...kids);
  Usage.need('stackedBars')(host, daily, { by: P.stack, top: 6, unpriced, hatchZero: true, approx: P.basis === 'est', hueOf: (name) => Usage.hue('project', name) });   // a project keeps the hue it has everywhere else (chipHue)   // Charts adds the legend and the tap-a-bar readout
};

/* ---------- the Estimated basis (issue #95) ---------- */

/* The summary's estimate block with its defaults: {date, source, sessions, usd, bases, cache_write_assumed, cache_write_x}. */
Usage.estimateInfo = function (sum) {
  const e = sum && sum.estimate && typeof sum.estimate === 'object' ? sum.estimate : {};
  return { date: Usage.str(e.date), source: Usage.str(e.source), sessions: Math.round(Usage.num(e.sessions)), usd: Usage.num(e.usd),
    bases: e.bases && typeof e.bases === 'object' ? e.bases : {}, cwa: !!e.cache_write_assumed, cwx: Usage.num(e.cache_write_x) || 1.25 };
};

/* A dollar figure with or without the `~`, whatever the page's basis (Usage.usd follows the basis). */
Usage.usdOf = function (v, approx) { const was = Usage.approx; Usage.approx = !!approx; try { return Usage.usd(v); } finally { Usage.approx = was; } };

Usage.BASIS_WORDS = { list: 'priced from the list price of the model itself', sibling: 'priced at the rate of a sibling model (the newest of its family in the table)', rough: 'priced roughly: several models and no per-model split of the tokens' };

/* The disclosure under the Estimated cost chart: what an estimate is, which prices, which basis each session used. A <details>, so it opens by touch and by keyboard. */
Usage.estimateDetails = function (est) {
  const lines = [
    el('p', { text: `${Usage.plural(est.sessions, 'session')} on models ccusage prices at zero ${est.sessions === 1 ? 'adds' : 'add'} ${Usage.usdOf(est.usd, true)} to the reported dollars. Each of those numbers is an estimate from the token counts at the list prices of ${est.date || 'the price table'}${est.source ? ` (${est.source})` : ''}.` }),
  ];
  const bases = Object.keys(Usage.BASIS_WORDS).filter((k) => Usage.num(est.bases[k]) > 0);
  if (bases.length) lines.push(el('ul', { class: 'uest-bases' }, ...bases.map((k) => el('li', { 'data-basis': k, text: `${Usage.plural(Usage.num(est.bases[k]), 'session')} ${Usage.BASIS_WORDS[k]}` }))));
  if (est.cwa) lines.push(el('p', { text: `The cache-write price of these models is not known: cache writes are priced at ${est.cwx} times the input price, an assumption.` }));
  lines.push(el('p', { text: 'A session whose model has no price in the table is not guessed: it stays hatched. These are API-equivalent dollars, what the same tokens would cost at API list prices, not a subscription invoice.' }));
  return el('details', { class: 'uest', 'data-note': 'estimate' }, el('summary', { text: 'How these estimates are made' }), ...lines);
};

Usage.paintFoot = function (P) {
  const foot = P.refs.foot;
  if (!foot) return;
  const c = Usage.entry(P);
  const sum = Usage.rawSummary(P) || (c && (c.summary || c.summaryEst));
  const est = Usage.estimateInfo(sum);
  setText(foot, `Dollars are API-equivalent: what the tokens would cost at API list prices, not a subscription invoice.${est.date ? ` Estimates use the list prices of ${est.date}.` : ''}`);
};

/* ---------- accounts (v0.5.17b): the subscription is the unit, so the windows lead and the dollars come last and dim ---------- */

Usage.str = function (v) { return typeof v === 'string' && v.trim() ? v.trim() : ''; };

Usage.plural = function (n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; };

/* What an account is called: the person's label, else the display name, else the email. */
Usage.accName = function (a) { return Usage.str(a && a.label) || Usage.str(a && a.name) || Usage.str(a && a.email) || 'account'; };

/* The summary's accounts for the page's range, with the labels of state.accounts.list laid over them: the state is polled every 3 s, the summary every 60 s,
   so a rename made anywhere (the shared sheet of settings.js, a Settings row, another tab) shows here at once, not at the next summary. Real accounts first
   (the server sorts the current one first), the '(before account tracking)' row last. [] while there is no summary or the server predates accounts. */
Usage.accounts = function (P) {
  const sum = Usage.summaryOf(P);
  const raw = sum && Array.isArray(sum.accounts) ? sum.accounts.filter((a) => a && typeof a === 'object' && typeof a.key === 'string' && a.key) : [];
  const fresh = new Map();
  try {
    const list = typeof state !== 'undefined' && state && state.accounts && Array.isArray(state.accounts.list) ? state.accounts.list : [];
    for (const x of list) if (x && typeof x === 'object' && typeof x.key === 'string') fresh.set(x.key, Usage.str(x.label) || null);
  } catch (_) { /* no state yet */ }
  const out = raw.map((a) => (fresh.has(a.key) && fresh.get(a.key) !== (Usage.str(a.label) || null) ? { ...a, label: fresh.get(a.key) } : a));
  return [...out.filter((a) => a.key !== Usage.UNKNOWN), ...out.filter((a) => a.key === Usage.UNKNOWN)];
};

Usage.realAccounts = function (list) { return list.filter((a) => a.key !== Usage.UNKNOWN); };

/* {pct, resets_at, at, source, rolled, title} of an account reading {value, resets_at, at, source}, or null without one. With `win` ('5h' | '7d': a Claude account) it is
   time-aware like every surface (Usage.windowReading): a reading whose reset has passed with no newer one reads 0 % and counts down to the next reset. Without it (the
   Codex block, whose windows come from a rollout) a passed reset reads 0 % 'window rolled over' with no countdown, as before. */
Usage.accReading = function (r, who, win) {
  if (!r || typeof r !== 'object' || typeof r.value !== 'number' || !Number.isFinite(r.value)) return null;
  if (win) return Usage.windowReading(win, r.value, Usage.epoch(r.resets_at), Usage.epoch(r.at), r.source, who);
  const resets = Usage.epoch(r.resets_at);
  const at = Usage.epoch(r.at);
  const rolled = resets > 0 && resets <= Date.now() / 1000;
  const age = at ? `, read ${fmtAge(at)} ago` : '';
  const out = { pct: rolled ? 0 : Math.max(0, Math.min(100, r.value)), resets_at: rolled ? 0 : resets, at };
  if (rolled) { out.reset_text = 'window rolled over'; out.title = `${who}: the window reset ${Usage.clock(resets)}; the last reading was ${Math.round(r.value)}%${age}`; }
  else out.title = `${who}: ${Math.round(r.value)}% used${resets ? ' · resets ' + Usage.clock(resets) : ''}${age}`;
  return out;
};

/* The 5H / 7D gauges of the Limits section follow the account a chip picked: its last statusline readings from the summary. */
Usage.paintAccountGauges = function (P, a) {
  const R = P.refs;
  const name = Usage.accName(a);
  const five = Usage.accReading(a.rl_5h, `${name} 5H`, '5h');
  const seven = Usage.accReading(a.rl_7d, `${name} 7D`, '7d');
  R.g5.set(five);
  R.g7.set(seven);
  Usage.paintFresh(P, [five, seven]);
  const at = Math.max(five ? five.at : 0, seven ? seven.at : 0);
  const text = !five && !seven ? `No 5H / 7D reading for ${name} yet: it appears once a session runs on that account.`
    : `Gauges show ${name}'s last statusline readings${at ? ', ' + fmtAge(at) + ' ago' : ''}: it is not the account in use.`;
  R.gnote.classList.remove('hidden');
  setText(R.gnote, text);
};

/* The account whose episodes the chart marks: null (all of them) with one account, else the picked one, else the current one. */
Usage.limitAccount = function (P) {
  const real = Usage.realAccounts(Usage.accounts(P));
  if (real.length < 2) return null;
  const cur = real.find((a) => a.current) || real[0];
  return P.limAcct || cur.key;
};

/* A picked account the summary no longer lists goes back to the current one (and its series with it). */
Usage.checkAccount = function (P) {
  if (!P.limAcct || !Usage.summaryOf(P)) return;
  if (!Usage.realAccounts(Usage.accounts(P)).some((a) => a.key === P.limAcct)) Usage.setAccount(P, null);
};

/* Pick whose windows the Limits section shows: null = the current account (the series key stays 'claude'), else an account key. */
Usage.setAccount = function (P, key) {
  if (!Usage.alive(P)) return;
  const next = key || null;
  if (next === P.limAcct) return;
  P.limAcct = next;
  P.accSeq += 1;
  Usage.paintGauges(P);                                                          // also the chips
  Usage.safe(P, 'limits', () => Usage.paintLimits(P));                           // the skeleton until this account's series arrives
  Usage.reloadSeries(P);
};

/* One chip per real account above the Limits gauges (nothing with a single account); on the Codex tab one per Codex account (state.codex_accounts). */
Usage.paintPicks = function (P) {
  const host = P.refs.limAcct;
  if (!host) return;
  const cx = Usage.agent(P) === 'codex';
  const list = cx ? Usage.cxAccts(Usage.stOf(P)) : Usage.realAccounts(Usage.accounts(P));
  if (list.length < 2) { P.sigs.picks = ''; host.textContent = ''; host.classList.add('hidden'); return; }
  const cur = list.find((a) => a.current) || list[0];
  const sel = (cx ? P.cxAcct : P.limAcct) || cur.key;
  host.classList.remove('hidden');
  const nameOf = (a) => (cx ? cxName(a) : Usage.accName(a));
  const sig = Usage.sig([cx, list.map((a) => [a.key, nameOf(a), !!a.current]), sel]);
  if (P.sigs.picks === sig) return;
  P.sigs.picks = sig;
  host.textContent = '';
  host.append(el('span', { class: 'dim ua-picks-l', text: 'Account' }));
  for (const a of list) {
    const name = nameOf(a);
    const pick = () => (cx ? Usage.setCxAccount(P, a.key === cur.key ? null : a.key) : Usage.setAccount(P, a.key === cur.key ? null : a.key));
    host.append(el('button', { class: 'small ua-pick pj-repo-btn', type: 'button', 'data-account': a.key, 'aria-pressed': a.key === sel ? 'true' : 'false',
      title: `Show the windows of ${name}${a.current ? ' (the account in use)' : ''}`, onclick: pick },
      el('span', { class: ['ua-dot', cx ? 'hue-teal' : Usage.hue('account', a.key)].filter(Boolean).join(' '), 'aria-hidden': 'true' }),
      el('span', { class: 'ua-pick-l', text: name }), a.current ? el('span', { class: 'dim ua-pick-now', text: 'now' }) : null));
  }
};

/* ---------- the Codex tab (v0.5.12): the provider switch of the Limits section, its gauges and the Codex block of the Accounts section ---------- */

Usage.stOf = function (P) { return (P && P.st) || (typeof state !== 'undefined' ? state : null); };

/* The saved Codex accounts of a state ([] where pages/agents.js is not loaded or the box predates them). */
Usage.cxAccts = function (st) { return typeof cxAccounts === 'function' && st ? cxAccounts(st) : []; };

/* Is there a Codex on this box worth a tab: a rate-limit reading, saved Codex accounts, or an installed Codex agent. */
Usage.hasCodex = function (st) {
  if (!st || typeof st !== 'object') return false;
  if (st.usage_codex && typeof st.usage_codex === 'object') return true;
  if (typeof cxState === 'function' && cxState(st)) return true;
  const a = st.agents && st.agents.codex;
  return !!(a && typeof a === 'object' && a.installed !== false);
};

/* The provider the Limits section shows: the picked one, Claude while the box has no Codex. */
Usage.agent = function (P) { return P && P.agent === 'codex' && Usage.hasCodex(Usage.stOf(P)) ? 'codex' : 'claude'; };

/* What the cached limit series of a range was fetched for: null or an account key for Claude (as before v0.5.12), 'codex:<picked Codex account>' for Codex. */
Usage.scope = function (P) { return Usage.agent(P) === 'codex' ? 'codex:' + (P.cxAcct || '') : P.limAcct; };

/* Does a series name belong to the limit chart of the page's provider? 'rl_7d:claude' and 'rl_5h:acct:<key>' are Claude's, 'rl_7d:codex' and 'rl_7d:cacct:<key>' Codex's
   (the demo's series file holds all of them; a live answer only has the key that was asked for). On the Codex tab one account's windows at a time. */
Usage.limName = function (name, P) {
  const m = /^rl_(?:5h|7d):(.+)$/.exec(String(name));
  if (!m) return false;
  const isCx = m[1] === 'codex' || m[1].indexOf('cacct:') === 0;
  if (Usage.agent(P) !== 'codex') return !isCx;
  return m[1] === (P.cxAcct ? 'cacct:' + P.cxAcct : 'codex');
};

/* The provider switch: shown with a Codex on the box, the pressed button follows the provider painted, the section's provenance label says where its numbers come from. */
Usage.paintAgentSeg = function (P) {
  const R = P.refs;
  if (!R.agentSeg) return;
  const ag = Usage.agent(P);
  R.agentSeg.classList.toggle('hidden', !Usage.hasCodex(Usage.stOf(P)));
  for (const a of Usage.AGENTS) R.agentBtn[a].setAttribute('aria-pressed', a === ag ? 'true' : 'false');
  setText(R.prov.limits, ag === 'codex' ? 'rollout rate limits (Codex)' : 'statusline (official)');
};

/* The provider changed (a tap, the address, or the state arriving after the page: a stored Codex pick waits for the box to say it has one): repaint the gauges and the chips
   and fetch the other provider's series. False when nothing changed. */
Usage.syncAgent = function (P) {
  const ag = Usage.agent(P);
  if (ag === P.agentNow) return false;
  P.agentNow = ag;
  P.accSeq += 1;
  P.sigs.picks = '';
  Usage.paintGauges(P);
  Usage.safe(P, 'limits', () => Usage.paintLimits(P));
  Usage.reloadSeries(P);
  return true;
};

Usage.setAgent = function (P, agent) {
  if (!Usage.alive(P) || !Usage.AGENTS.includes(agent) || agent === P.agent) return;
  P.agent = agent;
  Usage.saveAgent(agent);
  if (!Usage.syncAgent(P)) Usage.paintGauges(P);
  try {
    if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('usage', {}, { range: P.range, agent: Usage.agent(P) === 'codex' ? 'codex' : null }), { replace: true });
  } catch (e) { console.error('ccboard usage agent', e); }
};

/* Pick whose windows the Codex tab shows: null = the account in use (key=codex), else a Codex account key (key=cacct:<key>). */
Usage.setCxAccount = function (P, key) {
  if (!Usage.alive(P)) return;
  const next = key || null;
  if (next === P.cxAcct) return;
  P.cxAcct = next;
  P.accSeq += 1;
  Usage.paintGauges(P);
  Usage.safe(P, 'limits', () => Usage.paintLimits(P));
  Usage.reloadSeries(P);
};

/* A Codex rate-limit record ({primary, secondary}: {used_percent, window_minutes, resets_at}) by slot: {five, seven}, each {value, resets_at, at} (the shape of an
   account reading, Usage.accReading) or null. The slot comes from window_minutes: up to 360 is the 5H one, from 7200 the 7D one, a window in between has none. */
Usage.cxSlots = function (rec) {
  const out = { five: null, seven: null };
  if (!rec || typeof rec !== 'object') return out;
  for (const w of [rec.primary, rec.secondary]) {
    if (!w || typeof w !== 'object') continue;
    const pct = typeof w.used_percent === 'number' ? w.used_percent : w.used_percentage;
    const m = Usage.num(w.window_minutes);
    if (typeof pct !== 'number' || !Number.isFinite(pct) || !m) continue;
    const slot = m <= Usage.CX_WIN.five ? 'five' : m >= Usage.CX_WIN.seven ? 'seven' : null;
    if (slot && !out[slot]) out[slot] = { value: pct, resets_at: w.resets_at, at: rec.observed_at };
  }
  return out;
};

/* What is known of one Codex account's windows: {five, seven, plan, reached, live}. The account in use (or the one state.usage_codex names) answers with the live reading
   the topbar pill shows, and falls back to the page's series; any other account has its last series point (P.cx.acc). A null key is 'the reading', whoever it belongs to. */
Usage.cxReading = function (P, key) {
  const st = Usage.stOf(P);
  const v = st && st.usage_codex && typeof st.usage_codex === 'object' && st.usage_codex.value && typeof st.usage_codex.value === 'object' ? st.usage_codex.value : null;
  const cs = typeof cxState === 'function' ? cxState(st) : null;
  const liveKey = v && typeof v.account === 'string' && v.account ? v.account : (cs ? cs.current : null);      // a reading that names no account is the one in use's
  const live = Usage.cxSlots(v);
  const own = (key && P.cx && P.cx.acc && P.cx.acc[key]) || { five: null, seven: null };
  if (!key || key === liveKey) return { five: live.five || own.five, seven: live.seven || own.seven, plan: v && typeof v.plan_type === 'string' ? v.plan_type : '', reached: !!(v && v.reached === true), live: !!v };
  return { five: own.five, seven: own.seven, plan: '', reached: false, live: false };
};

/* The two gauges and the note of the Codex tab. */
Usage.paintCodexGauges = function (P) {
  const R = P.refs;
  const st = Usage.stOf(P);
  const list = Usage.cxAccts(st);
  if (P.cxAcct && !list.some((a) => a.key === P.cxAcct)) { Usage.setCxAccount(P, null); return; }     // a picked account that is gone: back to the one in use
  const chosen = P.cxAcct ? list.find((a) => a.key === P.cxAcct) : null;
  const cs = typeof cxState === 'function' ? cxState(st) : null;
  const key = chosen ? chosen.key : (cs ? cs.current : null);
  const name = chosen ? cxName(chosen) : 'Codex';
  const rd = Usage.cxReading(P, key);
  const five = Usage.accReading(rd.five, `${name} 5H`);
  const seven = Usage.accReading(rd.seven, `${name} 7D`);
  R.g5.set(five);
  R.g7.set(seven);
  Usage.paintFresh(P, null);                                                     // Claude's wording ('from the last session'): the Codex note below gives the age
  const at = Math.max(five ? five.at : 0, seven ? seven.at : 0);
  let text = '';
  if (!five && !seven) {
    text = chosen ? `No 5H / 7D reading for ${name} yet: it appears once a Codex session runs on that account.`
      : 'No Codex reading yet: the board reads the limits from the rollout of a Codex session. Start one with + session.';
  } else {
    const plan = rd.plan || (chosen && Usage.str(chosen.plan)) || '';
    const parts = [`${plan ? plan + ' plan' : 'Codex'}`, at ? `last reading ${fmtAge(at)} ago` : '',
      !five || !seven ? `the ${seven ? 'weekly' : '5-hour'} window is the only one this plan reports` : '', rd.reached ? 'limit reached' : '',
      chosen && !chosen.current ? 'not the account in use' : ''];
    text = parts.filter(Boolean).join(' · ');
  }
  R.gnote.classList.toggle('hidden', !text);
  setText(R.gnote, text);
};

/* The Codex accounts' own windows: ONE series request for all of them (rl_5h and rl_7d with key=cacct:<key>, the last 30 days; the weekly one alone past 4 accounts because the
   endpoint takes 8 series x key combinations), reduced to each account's last point. P.cx.acc[key] = {five, seven} in the shape of Usage.cxSlots. Nothing is asked without a Codex account. */
Usage.cxKeys = function (st) {
  if (!Usage.hasCodex(st)) return [];
  const keys = Usage.cxAccts(st).map((a) => a.key);
  return keys.slice(0, keys.length * 2 <= Usage.CX_ACCT_MAX ? keys.length : Usage.CX_ACCT_MAX);
};

Usage.cxParse = function (data, keys) {
  const acc = {};
  const ok = data && typeof data === 'object' && Array.isArray(data.t) && data.series && typeof data.series === 'object';
  for (const k of keys) {
    const slot = { five: null, seven: null };
    for (const [field, name] of [['five', 'rl_5h'], ['seven', 'rl_7d']]) {
      const full = `${name}:cacct:${k}`;
      const col = ok ? data.series[full] : null;
      if (!Array.isArray(col)) continue;
      let i = col.length - 1;
      while (i >= 0 && !(typeof col[i] === 'number' && Number.isFinite(col[i]))) i -= 1;
      if (i < 0) continue;
      const meta = data.meta && data.meta[full];
      slot[field] = { value: col[i], resets_at: meta && meta.resets_at, at: data.t[i] };
    }
    acc[k] = slot;
  }
  return acc;
};

Usage.fetchCx = function (P, fresh) {
  const keys = Usage.cxKeys(Usage.stOf(P));
  const cx = P.cx;
  cx.keys = keys.join(',');
  if (!keys.length) { cx.seq += 1; cx.acc = {}; cx.err = ''; cx.done = true; return Promise.resolve(); }
  const names = keys.length * 2 <= Usage.CX_ACCT_MAX ? 'rl_5h,rl_7d' : 'rl_7d';
  const path = `/api/series?series=${names}&key=${keys.map((k) => 'cacct:' + encodeURIComponent(k)).join(',')}&since=30d&points=60`;
  const mine = ++cx.seq;
  return Usage.get(path, fresh).then((data) => { if (cx.seq === mine) { cx.acc = Usage.cxParse(data, keys); cx.err = ''; } },
    (e) => { if (cx.seq === mine) cx.err = Usage.errText(e); }).then(() => {
    if (cx.seq !== mine) return;
    cx.done = true;
    if (Usage.alive(P) && Usage.summaryOf(P)) Usage.safe(P, 'accounts', () => Usage.paintAccounts(P));      // the block's gauges arrive
  });
};

/* Local midnight of the first day of the range (24h is today only, like the summary's 'today' window), in epoch seconds. */
Usage.rangeStart = function (range) {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - ((range === '24h' ? 1 : Usage.days(range)) - 1));
  return d.getTime() / 1000;
};

/* The limit episodes of the range ('lim' rows: {kind, at, resets_at, session, acct}). */
Usage.episodesIn = function (sum, range) {
  const from = Usage.rangeStart(range);
  return (Array.isArray(sum.episodes) ? sum.episodes : []).filter((e) => e && typeof e === 'object' && Usage.epoch(e.at) >= from);
};

/* {total, tokens, hours, sessions} of an account (or of the summary's total) for a window name, zeros for what is missing. */
Usage.accStat = function (rec, w) {
  const s = rec && rec.windows && rec.windows[w] && typeof rec.windows[w] === 'object' ? rec.windows[w] : {};
  return { total: Usage.num(s.total), tokens: Usage.num(s.tokens), hours: Usage.num(s.hours), sessions: Math.max(0, Math.round(Usage.num(s.sessions))) };
};

Usage.statCell = function (col, label, value, dim, title) {
  return el('div', { class: `ua-stat${dim ? ' ua-dim' : ''}`, role: 'cell', 'data-col': col, title: title || null },
    el('span', { class: 'ua-k dim', text: label }), el('span', { class: 'ua-v mono', text: value }));
};

Usage.accGauge = function (label, reading, who, win) {
  const r = Usage.accReading(reading, `${who} ${label}`, win);
  if (!r) return el('div', { class: 'ua-win', role: 'cell', 'data-win': label }, el('span', { class: 'ua-wl mono', text: label }), el('span', { class: 'dim ua-none', text: 'no reading yet' }));
  const g = Usage.gauge(label);
  g.set(r);
  return el('div', { class: 'ua-win', role: 'cell', 'data-win': label }, g);
};

Usage.accStats = function (st, hits, hitsTitle, sessionsTitle, usdTitle) {
  return [Usage.statCell('tokens', 'tokens', Usage.tok(st.tokens)), Usage.statCell('hours', 'active hours', Usage.hours(st.hours)),
    Usage.statCell('sessions', 'sessions', String(st.sessions), false, sessionsTitle), Usage.statCell('hits', 'limit hits', String(hits), false, hitsTitle),
    Usage.statCell('usd', 'API-equivalent', Usage.usd(st.total), true, usdTitle || 'What the same tokens would cost at API list price: not the subscription fee')];
};

Usage.accRow = function (a, w, hits) {
  const unknown = a.key === Usage.UNKNOWN;
  const name = Usage.accName(a);
  const email = Usage.str(a.email);
  const plan = Usage.str(a.plan);
  const hue = unknown ? '' : Usage.hue('account', a.key);
  const who = [unknown ? el('span', { class: 'ua-name', text: name }) : el('span', { class: ['bdg ua-chip', hue].filter(Boolean).join(' '), title: email || name, text: name })];
  if (plan) who.push(el('span', { class: 'bdg mono ua-plan', title: 'Subscription plan', text: plan }));
  if (a.current) who.push(el('span', { class: 'bdg ua-current', title: 'The account this board is logged in with', text: 'current' }));
  if (email && email !== name) who.push(el('span', { class: 'dim ua-email', text: email }));
  // the name, plan, current marker and email wrap among themselves (.ua-who); the pencil is its own flex item at the right of the card header, so a long
  // email never pushes it onto a line of its own
  const id = [el('span', { class: 'ua-who' }, ...who)];
  if (!unknown) id.push(el('button', { class: 'icon minimal small ua-edit', type: 'button', title: 'Rename this account', 'aria-label': `Rename ${name}`, onclick: () => settingsRenameAccount(a) }, ic('edit')));   // the one rename sheet, settings.js
  const wins = unknown ? [el('div', { class: 'ua-win ua-blank', role: 'cell' }), el('div', { class: 'ua-win ua-blank', role: 'cell' })]
    : [Usage.accGauge('5H', a.rl_5h, name, '5h'), Usage.accGauge('7D', a.rl_7d, name, '7d')];
  return el('div', { class: 'ua-row' + (unknown ? ' unknown' : ''), role: 'row', 'data-account': a.key, 'data-current': a.current ? true : null },
    el('div', { class: 'ua-id', role: 'cell' }, ...id), ...wins, ...Usage.accStats(Usage.accStat(a, w), hits, 'Limit hits in this range'));
};

/* The closing row: the summary's total for the window (it counts a session that ran on two accounts once; the rows count it once each). */
Usage.accTotal = function (sum, list, real, w, hits) {
  const tot = sum.total && typeof sum.total === 'object' ? sum.total : {};
  const rows = list.map((a) => Usage.accStat(a, w));
  const add = (f) => rows.reduce((x, r) => x + f(r), 0);
  const st = tot[w] && typeof tot[w] === 'object' ? Usage.accStat({ windows: { [w]: tot[w] } }, w)
    : { total: add((r) => r.total), tokens: add((r) => r.tokens), hours: add((r) => r.hours), sessions: add((r) => r.sessions) };
  const n = typeof tot.accounts === 'number' && Number.isFinite(tot.accounts) ? Math.round(tot.accounts) : real.length;
  const split = st.sessions !== add((r) => r.sessions) ? 'A session that ran on two accounts counts once here and once in each account row' : null;
  return el('div', { class: 'ua-row total', role: 'row', 'data-total': '' },
    el('div', { class: 'ua-id', role: 'cell' }, el('b', { class: 'ua-total-l', text: 'Total' }), el('span', { class: 'dim ua-count', text: Usage.plural(n, 'account') })),
    el('div', { class: 'ua-win ua-blank', role: 'cell' }), el('div', { class: 'ua-win ua-blank', role: 'cell' }),
    ...Usage.accStats(st, hits, 'Limit hits in this range, every account', split, 'API-equivalent dollars, rounded: each row is rounded on its own, so the rows can differ from this total by $1'));
};

/* 'most room: Work, 62 % of the 5-hour window left': the account with the most of a window left. When the account in use is at 85 % or more of a
   window and another account has more of it left, it is the attention line (amber): {text, attn, to: the key of the account with the room}; paintAccounts adds a Switch
   button when that account has a saved login. null with fewer than two accounts. */
Usage.room = function (sum, real) {
  const tot = sum.total && typeof sum.total === 'object' ? sum.total : {};
  if (real.length < 2) return null;
  const by = new Map(real.map((a) => [a.key, a]));
  // the server's ranking was made when the summary was fetched: a window whose reset has passed since then is wide open now (the same rule as the rows' gauges)
  const ranked = (list, field) => {
    const out = (Array.isArray(list) ? list.filter((x) => x && by.has(x.key) && typeof x.left_pct === 'number' && Number.isFinite(x.left_pct)) : [])
      .map((x) => { const r = Usage.accReading(by.get(x.key)[field], '', field === 'rl_5h' ? '5h' : '7d'); return r && r.rolled ? { ...x, left_pct: 100 } : x; });
    return out.sort((a, b) => (b.left_pct - a.left_pct) || ((by.get(b.key).current ? 1 : 0) - (by.get(a.key).current ? 1 : 0)));
  };
  const wins = [['7-day', ranked(tot.headroom_7d, 'rl_7d'), 'rl_7d'], ['5-hour', ranked(tot.headroom_5h, 'rl_5h'), 'rl_5h']];
  const clause = (e, nm) => `${Usage.accName(by.get(e.key))}, ${Math.round(e.left_pct)} % of the ${nm} window left`;
  const cur = real.find((a) => a.current);
  if (cur) {
    for (const [nm, list, field] of wins) {
      const r = Usage.accReading(cur[field], nm, field === 'rl_5h' ? '5h' : '7d');
      if (!r || r.pct < Usage.HOT_PCT) continue;
      const other = list.find((x) => x.key !== cur.key);
      if (other && other.left_pct > 100 - r.pct) return { attn: true, to: other.key, text: `${Usage.accName(cur)} is at ${Math.round(r.pct)} % of the ${nm} window. most room: ${clause(other, nm)}` };
    }
  }
  const parts = [];
  for (const [nm, list] of [wins[1], wins[0]]) if (list.length) parts.push(clause(list[0], nm));
  return parts.length ? { attn: false, text: `most room: ${parts.join(' · ')}` } : null;
};

/* The saved logins the board knows (state.accounts.list, pages/agents.js; none where that script is not loaded), whether a switch is running, and the account the amber
   callout points at when it can be switched to: its login is saved, it is not the one in use, and this box keeps saved logins. */
Usage.logins = function () { return typeof agentsAccounts === 'function' && typeof state !== 'undefined' ? agentsAccounts(state) : []; };
Usage.busy = function () { return typeof acctFlow !== 'undefined' && !!acctFlow.busy; };
Usage.swapTarget = function (room) {
  if (!room || !room.attn || !room.to || typeof accountSwitch !== 'function' || typeof acctStore !== 'function') return null;
  const to = Usage.logins().find((a) => a.key === room.to);
  return to && to.saved && !to.current && acctStore(state).supported ? to : null;
};

Usage.paintAccounts = function (P) {
  const sum = Usage.summaryOf(P);
  const w = Usage.WINDOW[P.range];
  const eps = sum ? Usage.episodesIn(sum, P.range) : [];
  const hits = (key) => eps.filter((e) => (Usage.str(e.acct) || Usage.UNKNOWN) === key).length;
  // the '(before account tracking)' row is history: with nothing in it for this range (no tokens, hours, sessions or limit hits) it is only noise
  const list = sum ? Usage.accounts(P).filter((a) => {
    if (a.key !== Usage.UNKNOWN) return true;
    const st = Usage.accStat(a, w);
    return st.tokens > 0 || st.hours > 0 || st.sessions > 0 || hits(a.key) > 0;
  }) : [];
  if (!sum || !list.length) {
    Usage.noSummary(P, 'accounts', 'No account seen yet', 'The board reads the logged-in Claude account within a minute of the first session. Start one with + session, or run /login in a terminal.');
    const only = sum ? Usage.cxBlock(P, sum, w) : null;                         // a box that runs Codex only still shows its Codex accounts
    if (only) { P.sigs.accounts = ''; P.refs.body.accounts.append(only); }
    return;
  }
  const minute = Math.floor(Date.now() / 60000);                                // the countdowns move: repaint at least once a minute
  const sig = Usage.sig([P.range, minute, list, sum.total, eps.map((e) => [e.acct, e.at]), Usage.logins().map((a) => [a.key, !!a.saved, !!a.current]), Usage.busy(), Usage.cxSig(P, sum, w)]);       // the callout's Switch button follows the saved logins, the account in use and a switch under way; the Codex block follows its accounts and readings
  if (P.sigs.accounts === sig) return;
  P.sigs.accounts = sig;
  const real = Usage.realAccounts(list);
  const head = el('div', { class: 'uacc-head', role: 'row' },
    ...[['Account', ''], ['5H window', ''], ['7D window', ''], ['Tokens', ' ua-r'], ['Active hours', ' ua-r'], ['Sessions', ' ua-r'], ['Limit hits', ' ua-r'], ['API-equiv. $', ' ua-r ua-dim']]
      .map(([t, c]) => el('div', { class: 'ua-h' + c, role: 'columnheader', text: t })));
  const table = el('div', { class: 'uacc', role: 'table', 'aria-label': `Usage per account, ${Usage.WINDOW_NAME[P.range].toLowerCase()}` }, head,
    ...list.map((a) => Usage.accRow(a, w, hits(a.key))), Usage.accTotal(sum, list, real, w, eps.length));
  const room = Usage.room(sum, real);
  const kids = [table];
  if (room) {
    // the amber callout: with a saved login on the account that has the room, one button makes it the account in use (the same accountSwitch as Settings)
    const swap = Usage.swapTarget(room);
    if (swap) {
      kids.push(el('div', { class: 'ua-room attn has-act', 'data-room': 'attention' }, el('span', { class: 'ua-room-t', text: room.text }),
        el('button', { class: 'primary tinted', type: 'button', disabled: Usage.busy(), onclick: () => accountSwitch(swap), text: `Switch to ${Usage.accName(swap)}` })));
    } else kids.push(el('p', { class: 'ua-room' + (room.attn ? ' attn' : ' dim'), 'data-room': room.attn ? 'attention' : 'info', text: room.text }));
  }
  // the sessions of the rows can add up to more than the total (one that ran on two accounts is counted in each row, once in the total): say so in
  // the open, because the cell's title does not show on a phone
  const rowSessions = list.reduce((x, a) => x + Usage.accStat(a, w).sessions, 0);
  const totSessions = sum.total && sum.total[w] && typeof sum.total[w] === 'object' ? Usage.accStat({ windows: { [w]: sum.total[w] } }, w).sessions : rowSessions;
  if (totSessions !== rowSessions) {
    kids.push(el('p', { class: 'dim unote', 'data-note': 'sessions', text: `Sessions: ${totSessions} in total, ${rowSessions} in the rows above: a session that ran on two accounts counts once in the total and once in each row.` }));
  }
  if (!real.length) kids.push(el('p', { class: 'dim unote', text: 'No Claude account has been seen yet: the board reads the logged-in one within a minute of the first session.' }));
  kids.push(el('p', { class: 'dim unote', text: `${Usage.WINDOW_NAME[P.range]}. The 5H and 7D bars are the subscription's own windows, tokens and hours are what each account used in the range; dollars are API-equivalent (list price), not what a plan costs. To use another subscription, run /login in any terminal.` }));
  kids.push(Usage.cxBlock(P, sum, w));
  Usage.setBody(P, 'accounts', ...kids);
};

/* ---------- the Codex block of the Accounts section (v0.5.12) ---------- */

/* What the block repaints on: the accounts, their readings and the box's Codex totals of the window. */
Usage.cxSig = function (P, sum, w) {
  const list = Usage.cxAccts(Usage.stOf(P));
  if (!list.length) return null;
  return [list.map((a) => [a.key, a.label, a.plan, !!a.current, Usage.cxReading(P, a.key)]), Usage.cxTotals(sum, w)];
};

/* {total, tokens} the summary counts for Codex in a window (ccusage counts sessions, not accounts), null without it. */
Usage.cxTotals = function (sum, w) {
  const by = sum && sum.windows && sum.windows[w] && sum.windows[w].by_agent && typeof sum.windows[w].by_agent === 'object' ? sum.windows[w].by_agent.codex : null;
  return by && typeof by === 'object' ? { total: Usage.num(by.total), tokens: Usage.num(by.tokens) } : null;
};

/* One Codex account: its name in the teal of its agent, the plan once the box has learned it, 'current', the rename pencil, and its 5H and 7D gauges (a plan that only has
   a weekly window leaves the 5H cell empty). */
Usage.cxRow = function (P, a) {
  const name = cxName(a);
  const rd = Usage.cxReading(P, a.key);
  const plan = Usage.str(a.plan) || rd.plan;
  const who = [el('span', { class: 'bdg ua-chip hue-teal', title: `${name}: a Codex account`, text: name })];
  if (plan) who.push(el('span', { class: 'bdg mono ua-plan', title: 'Codex plan', text: plan }));
  if (a.current) who.push(el('span', { class: 'bdg ua-current', title: 'The Codex login in use on this box', text: 'current' }));
  const id = [el('span', { class: 'ua-who' }, ...who)];
  if (typeof settingsRenameAccount === 'function') {
    id.push(el('button', { class: 'icon minimal small ua-edit', type: 'button', title: 'Rename this account', 'aria-label': `Rename ${name}`, onclick: () => settingsRenameAccount(a, { codex: true }) }, ic('edit')));
  }
  const blank = () => el('div', { class: 'ua-win ua-blank', role: 'cell' });
  const win5 = rd.five ? Usage.accGauge('5H', rd.five, name) : blank();
  const win7 = rd.seven || !rd.five ? Usage.accGauge('7D', rd.seven, name) : blank();
  return el('div', { class: 'ua-row ucx-row', role: 'row', 'data-codex-account': a.key, 'data-current': a.current ? true : null },
    el('div', { class: 'ua-id', role: 'cell' }, ...id), win5, win7, blank(), blank(), blank(), blank(), blank());
};

/* The block: a head, one row per Codex account, and the box's Codex tokens and dollars for the window under them. null without state.codex_accounts or without an account. */
Usage.cxBlock = function (P, sum, w) {
  const list = Usage.cxAccts(Usage.stOf(P));
  if (!list.length) return null;
  const head = el('div', { class: 'uacc-head', role: 'row' },
    ...[['Codex account', ''], ['5H window', ''], ['7D window', '']].map(([t, c]) => el('div', { class: 'ua-h' + c, role: 'columnheader', text: t })));
  const table = el('div', { class: 'uacc ucx-table', role: 'table', 'aria-label': 'Codex accounts' }, head, ...list.map((a) => Usage.cxRow(P, a)));
  const t = Usage.cxTotals(sum, w);
  const totals = t ? `${Usage.WINDOW_NAME[P.range]}: ${Usage.tok(t.tokens)} tokens · ${Usage.usd(t.total)} API-equivalent, for the whole box: ccusage counts Codex sessions, not accounts. ` : '';
  return el('div', { class: 'ucx', 'data-block': 'codex' },
    el('h3', { class: 'ucx-h' }, el('span', { text: 'Codex' }), el('span', { class: 'prov dim', text: 'rollout rate limits' })),
    table,
    el('p', { class: 'dim unote', 'data-note': 'codex', text: `${totals}The bars are the Codex plan's own windows, read from the rollout of a session that ran on the account.` }));
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
  const klass = [cls, Usage.isUnassigned(project) ? '' : Usage.hue('project', project)].filter(Boolean).join(' ') || null;
  if (hash) return el('a', { class: klass, href: hash, title: `Open the ${project} project`, text: text || project });
  return el('span', { class: klass, title: Usage.unassignedTip(project) || null, text: text || project });
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
    if (s.est_basis) name.append(el('span', { class: 'dim srow-via', 'data-est': s.est_basis, title: Usage.BASIS_WORDS[s.est_basis] || 'estimated from list prices',
      text: `estimated · ${s.est_basis === 'list' ? 'list price' : s.est_basis === 'sibling' ? 'sibling model rate' : 'rough'}` }));
    if (s.via === 'folder') name.append(el('span', { class: 'dim srow-via', 'data-via': 'folder', title: Usage.JOINED_TIP, text: 'joined by folder' }));
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
  const head = el('tr', {}, el('th', { scope: 'col' }, el('span', { class: 'sr-only', text: 'Details' })), el('th', { scope: 'col', text: 'Session' }), el('th', { scope: 'col', class: 'c-num', text: '$' }),
    el('th', { scope: 'col', class: 'c-num c-tok', text: 'Tokens' }), el('th', { scope: 'col', class: 'c-num c-hrs', text: 'Hours' }), el('th', { scope: 'col' }, el('span', { class: 'sr-only', text: 'Open' })));
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
  const outside = rows.find((r) => r.project === Usage.OUTSIDE);
  const named = rows.filter((r) => !Usage.isUnassigned(r.project)).sort((a, b) => Usage.num(b.total) - Usage.num(a.total));
  const shown = P.showAll ? named : named.slice(0, Usage.PROJECT_ROWS);
  const tbody = el('tbody');
  const hrsOf = (r) => (typeof r.hours === 'number' ? r.hours : Usage.num(active[r.project]));
  const row = (r, isUn) => {
    const hrs = hrsOf(r);
    const rate = hrs >= 0.05 ? Usage.usd(Usage.num(r.total) / hrs) + '/h' : '–';
    const tip = Usage.unassignedTip(r.project);
    const label = isUn ? el('span', Object.assign({ class: 'p-name', title: tip, text: r.project }, r.project === Usage.OUTSIDE ? { 'data-outside': '' } : { 'data-unattributed': '' }))
      : Usage.projectLink(r.project, null, 'p-name');
    const joined = !isUn && Usage.num(r.joined) > 0 ? el('span', { class: 'dim p-tip p-joined', 'data-joined': '', title: Usage.JOINED_TIP, text: `${Usage.usd(r.joined)} joined by folder` }) : null;
    return el('tr', { class: 'prow' + (isUn ? ' unattributed' : ''), 'data-project': r.project, title: isUn ? tip : null },
      el('td', { class: 'c-name' }, label, isUn ? el('span', { class: 'dim p-tip', text: tip }) : joined),
      el('td', { class: 'c-num mono', text: Usage.usd(r.total) }),
      el('td', { class: 'c-num c-hrs mono', text: Usage.hours(hrs) }),
      el('td', { class: 'c-num c-rate mono', text: rate }));
  };
  for (const r of shown) tbody.append(row(r, false));
  if (outside) tbody.append(row(outside, true));
  if (un) tbody.append(row(un, true));
  const anyJoined = named.some((r) => Usage.num(r.joined) > 0);
  const head = el('tr', {}, el('th', { scope: 'col', text: 'Project' }), el('th', { scope: 'col', class: 'c-num', text: '$' }),
    el('th', { scope: 'col', class: 'c-num c-hrs', text: 'Active' }), el('th', { scope: 'col', class: 'c-num c-rate', text: '$/h' }));
  const more = named.length - Usage.PROJECT_ROWS;
  Usage.setBody(P, 'projects', el('div', { class: 'utable' }, el('table', { class: 'table uprojects' }, el('thead', {}, head), tbody)),
    more > 0 ? el('button', { class: 'small', type: 'button', text: P.showAll ? 'Show fewer' : `Show ${more} more`, onclick: () => { P.showAll = !P.showAll; P.sigs.projects = ''; Usage.safe(P, 'projects', () => Usage.paintProjects(P)); } }) : null,
    el('p', { class: 'dim unote', text: `${Usage.WINDOW_NAME[P.range]}. Active hours count agent time from state events, not wall clock: $/h differs a lot between projects.` }),
    anyJoined ? el('p', { class: 'dim unote', 'data-note': 'joined', text: 'Rows that say "joined by folder" include sessions started outside the board. They are matched to a project by the folder they ran in, from Claude Code\'s own session files, and are never counted in a task\'s cost.' }) : null);
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
  if (P.rf) { if (P.rf.timer !== null) clearTimeout(P.rf.timer); if (P.rf.poll !== null) clearTimeout(P.rf.poll); P.rf.timer = null; P.rf.poll = null; P.rf.on = false; }
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
    Usage.paintMem(P);
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
    Usage.paintMem(P);
    if (!Usage.syncAgent(P)) Usage.paintGauges(P);
    Usage.refreshCheck(P);                                                              // a tap waits for a reading newer than the one it found
    Usage.refreshAuto(P);                                                               // the first state of this page open: ask by itself when the numbers are stale
    if (Usage.cxKeys(st).join(',') !== P.cx.keys) Usage.fetchCx(P);                    // a Codex account came or went (or the state arrived after the page): ask for its readings
    if (Usage.summaryOf(P)) Usage.safe(P, 'accounts', () => Usage.paintAccounts(P));      // a rename (or a /login switch) shows now; the signature keeps an unchanged poll from rebuilding the rows
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
    const a = Usage.routeAgent(route);                       // the CX pill lands on #/usage?agent=codex
    if (a && a !== P.agent) { P.agent = a; Usage.saveAgent(a); if (!Usage.syncAgent(P)) Usage.paintGauges(P); }
  },
  unmount() {
    const P = Usage.cur;
    if (P) Usage.teardown(P);
  },
});
