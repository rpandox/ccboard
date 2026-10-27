/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

/* Last-visit tracking runs on every route, before Home is ever opened. */
const HOME_SEEN_KEY = 'ccboard:seen';                 // epoch seconds of the last render seen while the tab was visible
const HOME_SEEN_TASKS_KEY = 'ccboard:seen:tasks';     // {task id: column} at that moment: a PR opened while away shows as a column change
const AWAY_MIN_S = 600;                        // the strip never shows for a gap shorter than 10 minutes
const AWAY_WRITE_S = 30;                       // ccboard:seen is written at most this often
const homeAway = { since: undefined, active: false, dismissed: false, lastWrite: 0, wired: false, lim: null, limFor: 0, prevTasks: null };
function homeGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function homeSet(key, v) { try { localStorage.setItem(key, v); } catch (_) { /* storage may be unavailable */ } }

function homeEpoch(v) {
  if (v === null || v === undefined || v === '') return 0;
  const n = typeof v === 'number' ? v : (/^\d+(?:\.\d+)?$/.test(String(v)) ? parseFloat(v) : Date.parse(v) / 1000);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return n > 1e12 ? n / 1000 : n;
}

function homeSeenTasks() { try { const v = JSON.parse(homeGet(HOME_SEEN_TASKS_KEY) || 'null'); return v && typeof v === 'object' ? v : null; } catch (_) { return null; } }

/* The previous visit is read once per document, before anything writes ccboard:seen; the strip only exists for a gap of 10 minutes or more. */
function homeAwayInit(nowSec) {
  if (homeAway.since !== undefined) return;
  const seen = homeEpoch(homeGet(HOME_SEEN_KEY));
  homeAway.since = seen || null;
  homeAway.active = !!seen && nowSec - seen >= AWAY_MIN_S;
  homeAway.prevTasks = homeSeenTasks();
}

function homeSeenWrite(nowSec, st) {
  homeSet(HOME_SEEN_KEY, String(Math.floor(nowSec)));
  if (st && Array.isArray(st.tasks)) { const m = {}; for (const t of st.tasks) m[t.id] = t.column; homeSet(HOME_SEEN_TASKS_KEY, JSON.stringify(m)); }
  homeAway.lastWrite = nowSec;
}

/* Wired on the first render: the moment the tab is hidden the last-seen time is saved, so the gap is counted from there. */
function homeAwayWire() {
  if (homeAway.wired || typeof document === 'undefined' || typeof document.addEventListener !== 'function') return;
  homeAway.wired = true;
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && typeof state !== 'undefined' && state && !ui.offline) homeSeenWrite(Date.now() / 1000, state);
  });
}

/* Called by every render (Home's update and renderBanner, so on any page): captures 'since' once, notices a long gap inside this document (a laptop
   that slept, a tab hidden for hours) and records that the board is being looked at, at most every 30 s while visible. */
function homeAwayTouch(st) {
  const now = Date.now() / 1000;
  homeAwayInit(now);
  homeAwayWire();
  if (homeAway.lastWrite && now - homeAway.lastWrite >= AWAY_MIN_S && homeAway.since !== homeAway.lastWrite) {
    homeAway.since = homeAway.lastWrite; homeAway.active = true; homeAway.dismissed = false; homeAway.lim = null; homeAway.limFor = 0; homeAway.prevTasks = homeSeenTasks();
  }
  if ((typeof document !== 'undefined' && document.hidden) || !st || ui.offline) return;
  if (now - homeAway.lastWrite >= AWAY_WRITE_S) homeSeenWrite(now, st);
}


/* The banner: one message at a time (a notice, offline, an error, a login that does not work, a recovery, a rate limit, tmux down, not logged in). renderBanner only
   removes what it put there itself and stray text, so a callout another file keeps in #banner (Widgets.limitBanner) survives a repaint. */
const bannerOwn = [];

/* "2 Claude and 1 Codex sessions": last_recovery.agents maps each recovered session to its agent; a record from before that field says "3 sessions". */
function recoveryWho(v) {
  const n = v.recovered.length, by = {};
  for (const name of v.recovered) { const a = (v.agents || {})[name]; if (a) by[a] = (by[a] || 0) + 1; }
  const parts = Object.keys(by).map((a) => `${by[a]} ${a.charAt(0).toUpperCase()}${a.slice(1)}`);
  return `${parts.length ? parts.join(' and ') : n} session${n === 1 ? '' : 's'}`;
}

function renderBanner() {
  const b = $('#banner');
  if (!b) return;
  for (const n of Array.from(b.childNodes || b.children || [])) {
    if (n.nodeType === 1 && !bannerOwn.includes(n)) continue;           // somebody else's element: leave it
    if (typeof b.removeChild === 'function') b.removeChild(n);
  }
  bannerOwn.length = 0;
  b.classList.remove('warn', 'info', 'login');                               // only the classes this function owns: Widgets.limitBanner keeps its own
  const own = (...nodes) => { for (const n of nodes) { bannerOwn.push(n); b.append(n); } };
  const limitWidget = typeof Widgets !== 'undefined' && Widgets && typeof Widgets.limitBanner === 'function';
  const rl = limitWidget ? null : rateLimitOf(state);                      // the limit callout is Widgets' job once it exists
  if (ui.notice) {
    b.classList.add('warn');
    own(el('span', { text: ui.notice }), el('button', { class: 'minimal small', type: 'button', onclick: () => { ui.notice = null; renderBanner(); }, text: 'ok' }));
  } else if (ui.offline) {
    b.classList.add('warn');
    own(el('span', { text: `Offline: showing the last known state from ${new Date(ui.offline).toLocaleTimeString()}. Retrying…` }));
  } else if (ui.error) {
    own(el('span', { text: ui.error }), el('button', { class: 'minimal small', type: 'button', onclick: () => setError(null), text: 'dismiss' }));
  } else if (acctProblem(state)) {
    // a session stopped on an authentication error (state.accounts.problem): the login of that account does not work. Bordered actions only: Log in again goes to that
    // account's row in Settings (where it is the row's lead action), Switch back is offered right after a switch, dismiss is for a false alarm
    const p = acctProblem(state);
    b.classList.add('warn', 'login');
    const acts = el('div', { class: 'banner-acts' }, el('button', { class: 'small', type: 'button', onclick: () => acctProblemOpen(p), text: 'Log in again' }));
    if (p.back && p.back.key) acts.append(el('button', { class: 'small', type: 'button', title: `Make ${p.back.label || 'the other account'} the account in use again`, onclick: () => acctProblemBack(p), text: `Switch back to ${p.back.label || 'the other account'}` }));
    const x = el('button', { class: 'minimal small bn-x', type: 'button', 'aria-label': 'dismiss', title: 'Dismiss this notice: a false alarm, or the login was fixed in a terminal', onclick: () => acctProblemDismiss(), text: '×' });   // a corner ×, like the limit callout's
    own(el('span', { text: acctProblemText(p) }), acts, x);                 // on a phone the × shares the text line and the two real actions keep one line below
  } else if (state && state.deploy && state.deploy.pending) {
    const d = state.deploy;
    b.classList.add('info');                                                 // an update is news, not an error: the red bar is for ui.error and tmux_down
    if (d.hold) {
      const asked = d.forced || ui.installNow > Date.now();                   // #313: asked here (a poll in flight keeps it) or confirmed by the board
      const why = (d.reasons || []).length ? ` (${d.reasons.join(', ')})` : '';
      own(el('span', { text: `An update is ready. It installs when the terminals are closed${why}, or in ${d.minutes_left} min at the latest.` }),
        el('button', { class: 'primary small', onclick: deployNow, disabled: asked ? true : null, text: asked ? 'installing at the next check' : 'Install now' }));
    } else {
      own(el('span', { text: 'Update installing: the board restarts and is back in about a minute.' }));
    }
  } else if (state && state.last_recovery && state.last_recovery.value && (state.last_recovery.value.recovered || []).length) {
    b.classList.add('warn');
    const v = state.last_recovery.value;
    const cont = v.continue || [];
    own(el('span', { text: `After a restart, ${recoveryWho(v)} relaunched: ${v.recovered.join(', ')}${cont.length ? ' · was working, continue typed once back: ' + cont.join(', ') : ''}${v.closed.length ? ' · closed: ' + v.closed.join(', ') : ''}` }),
      el('button', { class: 'minimal small', type: 'button', onclick: async () => { try { await api('POST', '/api/recovery/dismiss'); } catch (e) { setError(e.message); } await poll(true); }, text: 'dismiss' }));
  } else if (rl) {
    own(el('span', { text: `Rate limited: ${rl.message || ''}${rl.session ? ' (' + rl.session + ')' : ''}` }),
      el('button', { class: 'minimal small', type: 'button', onclick: async () => { try { await api('POST', '/api/usage/rate-limit/clear'); } catch (e) { setError(e.message); } await poll(true); }, text: 'dismiss' }));
  } else if (state && state.tmux_down) {
    const fix = typeof state.tmux_fix === 'string' ? state.tmux_fix : '';       // this platform's start command (#309); an older board sends none
    own(el('span', null, 'The ccboard tmux server is not running.', fix ? [' Start it on the box: ', el('code', { class: 'bn-cmd' }, fix.split(' ').flatMap((t, i) => (i ? [' ', el('span', { class: 'bn-tok', text: t })] : [el('span', { class: 'bn-tok', text: t })])))] : false),   // wraps only at the command's spaces
      el('button', { class: 'primary small', type: 'button', onclick: () => { const h = '#/settings?sec=doctor'; if (typeof navigate === 'function') navigate(h); else location.hash = h; }, text: 'Open Doctor' }));
  } else if (state && state.claude && state.claude.installed && !state.claude.loggedIn) {
    b.classList.add('warn');
    own(el('span', { text: 'Claude Code is not logged in on this box.' }), el('button', { class: 'primary small', type: 'button', onclick: () => accountLogin(), text: 'Log in' }));
  }
  if (bannerOwn.length) b.classList.remove('limit-only');                    // the limit callout is no longer alone in the banner
  if (state) homeAwayTouch(state);                                           // every render counts as "the tab was looked at", on any page
}

