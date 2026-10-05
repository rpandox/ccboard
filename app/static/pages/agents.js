/* ccboard agents page (#/agents) and the session card every session list shares.
   sessionCard(s, opts) builds one session as a keyed, patch-in-place node: the roster here, the inbox page and the session peek
   all use it (row layout by default, opts.peek for the peek's block layout), so the glyphs, age, model and context, last
   prompt and message, Open / Ack / Kill and the nudge chips look and behave the same everywhere. The node keeps references to
   its parts and node.ccPatch(session) updates only what changed, so a poll never recreates a row that holds focus.
   The roster groups the live sessions by project, blocked sessions first. Classic script: globals are prefixed to stay unique.
   opts.rich (Home) adds the dense row: a model chip, a context meter with a one-tap compact chip, worktree / PR / subagents / cost / limit /
   blocked chips and a tail expander (the last 12 pane lines through Live.subscribe); the plain row (Agents, Inbox) is unchanged. */
'use strict';

const SESSION_NUDGES = ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'];   // the replies typed most often, in order
const SESSION_RANK = { waiting: 0, errored: 1, working: 2, idle: 3, done: 3, ended: 4, unknown: 5 };
const SESSION_NUDGE_STATES = ['waiting', 'idle', 'done', 'working', 'errored'];            // an ended pane is a shell prompt: never type into it
const agentsTicker = { n: 0, timer: null };
const CTX_HI = 60;                                                                          // context meter turns amber here and offers the one-tap compact chip
const CTX_CRIT = 85;                                                                        // and red here
const COST_WARN = 10;                                                                       // session cost chip: amber at $10 (about the p99 of a session), red at $100
const COST_BAD = 100;
const TAIL_LINES = 12;
const WORKTREE_PATH_RE = /(?:^|\/)\.claude\/worktrees\//;
const agentsTails = new Set();                                                              // row nodes that hold a Live subscription (swept when their row leaves the DOM)
const CV_AUTO_ROWS = 30;                                                                    // a list longer than this gets the cv-auto class (content-visibility, see pages.css)

/* Muted category hues (tokens.css: .hue-blue ... .hue-slate set --hue / --hue-bg / --hue-bd, pages.css paints a chip with them). The hue only helps scanning:
   the label always says what the chip is. chipHue(kind, key) -> the class: agent (claude violet, codex teal, shell slate), model (opus blue, fable violet,
   sonnet green, haiku slate, gpt / codex teal, anything else slate), project (a stable hash of the name into blue / teal / green / violet / slate: amber and
   rose mean attention, so they are never handed out), account (v0.5.17b: the same stable hash of the account key, so one subscription wears one hue on the
   Usage page, the topbar chip, the session rows and Settings), and slate for everything else (branch, PR, worktree, folder). */
const HUE_POOL = ['blue', 'teal', 'green', 'violet', 'slate'];
function chipHue(kind, key) {
  const k = String(key === null || key === undefined ? '' : key).toLowerCase();
  if (kind === 'agent') return 'hue-' + (k === 'claude' ? 'violet' : k === 'codex' ? 'teal' : 'slate');
  if (kind === 'model') {
    if (/opus/.test(k)) return 'hue-blue';
    if (/fable/.test(k)) return 'hue-violet';
    if (/sonnet/.test(k)) return 'hue-green';
    if (/haiku/.test(k)) return 'hue-slate';
    if (/gpt|codex|\bo\d/.test(k)) return 'hue-teal';
    return 'hue-slate';
  }
  if (kind === 'project' || kind === 'account') {
    let h = 5381;
    for (let i = 0; i < k.length; i++) h = ((h * 33) ^ k.charCodeAt(i)) >>> 0;
    return 'hue-' + HUE_POOL[h % HUE_POOL.length];
  }
  return 'hue-slate';
}

/* Put a hue class on a node that is patched in place, taking the previous one off. */
function chipHueSet(node, cls) {
  if (!node || node._hue === cls) return;
  if (node._hue) node.classList.remove(node._hue);
  node.classList.add(cls);
  node._hue = cls;
}

/* Subscription accounts (v0.5.17b). state.accounts = {current, list: [{key, email, name, label, plan, rl_5h, rl_7d, resets_5h, resets_7d, current}]} is always there on
   a live board and absent in older fixtures: every read goes through agentsAccounts(), which answers [] for anything else. The name of an account is its label,
   else the name Claude reports, else the email, else the first characters of the key: never 'undefined'. */
function agentsAccounts(st) {
  const a = st && st.accounts;
  return a && Array.isArray(a.list) ? a.list.filter((x) => x && typeof x === 'object' && x.key) : [];
}

function agentsAcctName(a) {
  if (!a) return 'account';
  const n = [a.label, a.name, a.email].find((x) => typeof x === 'string' && x.trim());
  return n ? n.trim() : (a.key ? String(a.key).slice(0, 6) : 'account');
}

/* The used percentage of one of an account's windows ('5h' | '7d') at `now` (epoch s): its last reading, 0 once that window has reset since (the reading is then
   from the window before, and a quiet account has not been read again), null without a reading. */
function agentsAcctUsed(a, win, now) {
  const v = a ? a['rl_' + win] : null;
  if (typeof v !== 'number' || !Number.isFinite(v)) return null;
  const r = a['resets_' + win];
  if (typeof r === 'number' && r > 0 && r <= (typeof now === 'number' ? now : Date.now() / 1000)) return 0;
  return Math.max(0, Math.min(100, v));
}

/* Same, but the account in use answers with the numbers the topbar pills show (state.usage, the freshest statusline) so no screen shows two figures for it. */
function agentsAcctUsedNow(st, a, win, now) {
  if (a && (a.current || (st && st.accounts && st.accounts.current === a.key))) {
    const rl = (st && st.usage && st.usage.value) || {};
    const w = win === '5h' ? rl.five_hour : rl.seven_day;
    if (w && typeof w.used_percentage === 'number' && Number.isFinite(w.used_percentage)) return Math.max(0, Math.min(100, w.used_percentage));
  }
  return agentsAcctUsed(a, win, now);
}

/* ---------- saved logins and the one-tap switch (v0.5.17c UI) ----------
   acctFlow is the page-side state of the account flows, shared by Settings > Accounts, the Usage callout and the Log in buttons:
     busy     the key being switched to while POST /api/accounts/<key>/switch runs (every row's Switch is disabled meanwhile)
     hold     {key}: the switch is painted as done; the poll's answer is laid over so a poll that lands mid-request does not flip the rows back.
              It ends with the request (the server's accounts replace the local ones); only the demo board keeps it, its fixtures never change
     want     {email}: set by accountLogin(), taken by Settings > Accounts, which then starts the add flow
     err      the reason of the last refused switch / forget, shown in the Accounts panel's error line
     demo     the demo board's make-believe login ({login, accounts}), laid over the poll's answer like hold */
const acctFlow = { busy: null, hold: null, want: null, err: '', demo: null, cont: null };
const ACCT_CONTINUE_KEY = 'ccboard:acct:continue';

/* 'After a switch, type continue in sessions parked on a limit': on unless the person turned it off. Kept in localStorage; where writing there fails the choice lasts in
   memory (acctFlow.cont) until reload. */
function acctContinuePref() {
  if (typeof acctFlow.cont === 'boolean') return acctFlow.cont;
  try { return localStorage.getItem(ACCT_CONTINUE_KEY) !== '0'; } catch (_) { return true; }
}
function acctContinueSet(on) {
  try { localStorage.setItem(ACCT_CONTINUE_KEY, on ? '1' : '0'); acctFlow.cont = null; } catch (_) { acctFlow.cont = !!on; }
}

/* Whether this box keeps saved logins (Linux with Claude's file credentials) and, if not, why: a state without `store` (older fixtures) counts as no. */
function acctStore(st) {
  const s = st && st.accounts && st.accounts.store;
  return { supported: !!(s && s.supported), reason: (s && typeof s.reason === 'string' && s.reason) || '' };
}

/* The demo board's make-believe account actions (no box behind it): a login in flight, accounts it "added", logins it "forgot". Created on first use. */
function acctDemo() {
  if (!acctFlow.demo) acctFlow.demo = { login: null, accounts: [], forgot: [] };
  return acctFlow.demo;
}

/* The server's reason for a refusal ({detail, error} body), else the error's message. */
function acctReason(e, fallback) {
  const b = e && e.body && typeof e.body === 'object' ? e.body : null;
  return (b && (b.detail || b.error)) || (e && e.message) || fallback || 'the box refused it';
}

/* Lay the unfinished account actions over a fresh /api/state answer (core.js poll calls this right after `state = s`). */
function accountOverlay(s) {
  if (!s || typeof s !== 'object') return s;
  const a = s.accounts;
  if (a && Array.isArray(a.list)) {
    const d = typeof demoOn === 'function' && demoOn() ? acctFlow.demo : null;
    if (d) {
      for (const x of d.accounts) if (!a.list.some((y) => y && y.key === x.key)) a.list.push({ ...x });
      for (const x of a.list) if (x && d.forgot.includes(x.key)) x.saved = false;
    }
    const h = acctFlow.hold;
    if (h && a.list.some((x) => x && x.key === h.key)) {
      for (const x of a.list) if (x) x.current = x.key === h.key;
      a.current = h.key;
    }
  }
  if (typeof demoOn === 'function' && demoOn() && acctFlow.demo && acctFlow.demo.login) s.login = { ...(s.login || {}), ...acctFlow.demo.login };
  if (typeof cxOverlay === 'function') cxOverlay(s);
  return s;
}

/* Every surface that shows the account in use, repainted from `state`. */
function accountsRepaint() {
  if (typeof settingsPage !== 'undefined' && settingsPage.refs && typeof settingsFill === 'function') settingsFill('accounts', true);
  if (typeof Shell !== 'undefined' && Shell.patchUsage) Shell.patchUsage(state);
  if (typeof updateCurrentPage === 'function') updateCurrentPage(state);
}

/* THE account switch (Settings rows and the Usage callout call it): the tapped row becomes the one in use at once, POST /api/accounts/<key>/switch follows with
   {continue_parked} from the persisted choice, success takes the server's accounts and says so in a toast, a refusal puts every row back and says why (the
   Accounts panel's error line and a toast). One switch at a time. Resolves true when the account is now in use. */
async function accountSwitch(a) {
  if (!a || !a.key || acctFlow.busy) return false;
  const st = state && state.accounts && Array.isArray(state.accounts.list) ? state.accounts : null;
  if (!st) return false;
  const key = a.key;
  const target = st.list.find((x) => x && x.key === key);
  if (!target || target.current) return false;
  const name = agentsAcctName(target);
  const was = new Map(st.list.filter(Boolean).map((x) => [x.key, !!x.current]));       // every row's flag and the account in use, to put back
  const wasCurrent = st.current;
  const paint = (to) => {                                                                // on the accounts `state` holds NOW: a poll may have replaced the object since
    const now = state && state.accounts;
    if (!now || !Array.isArray(now.list)) return;
    for (const x of now.list) if (x) x.current = to === null ? !!was.get(x.key) : x.key === to;
    now.current = to === null ? wasCurrent : to;
  };
  acctFlow.busy = key;
  acctFlow.hold = { key };
  acctFlow.err = '';
  paint(key);
  accountsRepaint();
  let r = null;
  try {
    r = await api('POST', `/api/accounts/${encodeURIComponent(key)}/switch`, { continue_parked: acctContinuePref() });
  } catch (e) {
    const why = acctReason(e);
    acctFlow.busy = null;
    acctFlow.hold = null;
    paint(null);
    acctFlow.err = `Switch failed: ${why}`;
    accountsRepaint();
    if (typeof pageToast === 'function') pageToast(`Switch failed: ${why}`, 'bad');
    return false;
  }
  acctFlow.busy = null;
  if (!(typeof demoOn === 'function' && demoOn())) acctFlow.hold = null;
  if (r && r.accounts && Array.isArray(r.accounts.list)) state.accounts = r.accounts;        // the server's view (demo answers {ok: true}: the local move stays)
  accountsRepaint();
  const done = r && Array.isArray(r.continued) ? r.continued : [];
  const text = r && r.already ? `${name} is already in use`
    : `Switched to ${name} · running sessions follow within seconds${done.length ? ` · continue typed in ${done.length} session${done.length === 1 ? '' : 's'}` : ''}`;
  if (typeof pageToast === 'function') pageToast(text, 'ok');
  if (typeof poll === 'function') poll(true);
  return true;
}

/* The Log in buttons (Settings > Agents, the Home banner): the add flow lives in Settings > Accounts, so go there and start it. email: the account to sign in again. */
function accountLogin(email) {
  acctFlow.want = { email: typeof email === 'string' && email ? email : null };
  if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('settings', {}, { sec: 'accounts' }));
  if (typeof settingsPage !== 'undefined' && settingsPage.refs && typeof settingsShow === 'function') settingsShow('accounts');   // already on Settings: the route may not change
}

/* ---------- saved Codex logins (v0.5.17e): the twin of the Claude flow above, on state.codex_accounts ----------
   state.codex_accounts = {current, list: [{key, label, account_id, plan, saved, current, added_at, last_seen}], store: {supported, add, reason, count}, login: {running, adding, label,
   started_at, url, code, result}} (older boxes and fixtures have none: the Codex section of Settings > Accounts is then not shown). An account is named by its label alone: Codex
   gives no email at login. cxFlow is its page-side state, separate from acctFlow so a Claude switch and a Codex switch never block each other:
     busy   the key being switched to while POST /api/codex-accounts/<key>/switch runs
     hold   {key}: the switch is painted as done and laid over the poll until the request ends (the demo board keeps it)
     err    the reason of the last refused switch / forget; note: the warnings of the last switch (other Codex processes keep the previous login)
     demo   the demo board's make-believe login, accounts it "added" and logins it "forgot" */
const cxFlow = { busy: null, hold: null, err: '', note: '', demo: null };

function cxState(st) {
  const c = st && st.codex_accounts;
  return c && typeof c === 'object' && Array.isArray(c.list) ? c : null;
}

function cxAccounts(st) {
  const c = cxState(st);
  return c ? c.list.filter((x) => x && typeof x === 'object' && x.key) : [];
}

function cxStore(st) {
  const s = st && st.codex_accounts && st.codex_accounts.store;
  return { supported: !!(s && s.supported), add: !!(s && s.add), reason: (s && typeof s.reason === 'string' && s.reason) || '' };
}

function cxName(a) {
  const n = a && typeof a.label === 'string' ? a.label.trim() : '';
  return n || (a && a.key ? String(a.key).slice(0, 6) : 'Codex account');
}

function cxDemo() {
  if (!cxFlow.demo) cxFlow.demo = { login: null, accounts: [], forgot: [], current: null };
  return cxFlow.demo;
}

/* Lay the unfinished Codex actions over a fresh /api/state answer (accountOverlay calls this): the demo's accounts, logins and login, and a switch still running. */
function cxOverlay(s) {
  const c = s && s.codex_accounts;
  if (!c || !Array.isArray(c.list)) return s;
  const d = typeof demoOn === 'function' && demoOn() ? cxFlow.demo : null;
  if (d) {
    for (const x of d.accounts) if (!c.list.some((y) => y && y.key === x.key)) c.list.push({ ...x });
    for (const x of c.list) if (x && d.forgot.includes(x.key)) x.saved = false;
    if (d.login) c.login = { ...(c.login || {}), ...d.login };
  }
  const h = cxFlow.hold;
  if (h && c.list.some((x) => x && x.key === h.key)) {
    for (const x of c.list) if (x) x.current = x.key === h.key;
    c.current = h.key;
  }
  return s;
}

/* Every surface that shows the Codex accounts, repainted from `state` (the Accounts panel is the only one). */
function cxRepaint() {
  if (typeof settingsPage !== 'undefined' && settingsPage.refs && typeof settingsFill === 'function') settingsFill('accounts', true);
}

/* THE Codex switch (the Settings rows call it): the tapped row becomes the one in use at once, POST /api/codex-accounts/<key>/switch follows, success takes the server's
   accounts and says so in a toast (a warning toast and the panel's note line when other Codex processes on the box keep the previous login), a refusal puts every row back
   and says why (the panel's error line and a toast). One switch at a time. Resolves true when the account is now in use. */
async function cxSwitch(a) {
  if (!a || !a.key || cxFlow.busy) return false;
  const st = cxState(state);
  if (!st) return false;
  const key = a.key;
  const target = st.list.find((x) => x && x.key === key);
  if (!target || target.current) return false;
  const name = cxName(target);
  const was = new Map(st.list.filter(Boolean).map((x) => [x.key, !!x.current]));
  const wasCurrent = st.current;
  const paint = (to) => {
    const now = cxState(state);
    if (!now) return;
    for (const x of now.list) if (x) x.current = to === null ? !!was.get(x.key) : x.key === to;
    now.current = to === null ? wasCurrent : to;
  };
  cxFlow.busy = key;
  cxFlow.hold = { key };
  cxFlow.err = '';
  cxFlow.note = '';
  paint(key);
  cxRepaint();
  let r = null;
  try {
    r = await api('POST', `/api/codex-accounts/${encodeURIComponent(key)}/switch`);
  } catch (e) {
    const why = acctReason(e);
    cxFlow.busy = null;
    cxFlow.hold = null;
    paint(null);
    cxFlow.err = `Switch failed: ${why}`;
    cxRepaint();
    if (typeof pageToast === 'function') pageToast(`Switch failed: ${why}`, 'bad');
    return false;
  }
  cxFlow.busy = null;
  if (!(typeof demoOn === 'function' && demoOn())) cxFlow.hold = null;
  if (r && r.accounts && Array.isArray(r.accounts.list)) state.codex_accounts = r.accounts;
  const warns = r && Array.isArray(r.warnings) ? r.warnings.filter((w) => typeof w === 'string' && w) : [];
  cxFlow.note = warns.join(' · ');
  cxRepaint();
  const text = r && r.already ? `${name} is already in use` : `Switched to ${name} · Codex sessions started from now on use it${warns.length ? ` · ${warns.join(' · ')}` : ''}`;
  if (typeof pageToast === 'function') pageToast(text, warns.length ? 'warn' : 'ok');
  if (typeof poll === 'function') poll(true);
  return true;
}

/* The reply box's placeholder: a phone gets the bare verb ('Reply…', 'Send…'): the one-row box is about 330 px wide and a long session name wrapped onto a
   second line that was cut off at the bottom, and there is no Shift+Enter on a touch keyboard. The name and the hint live in the box's title and aria-label
   (the callers set both). A fine pointer gets the full text. */
function sessionPlaceholder(verb, name) {
  let coarse = false;
  try { coarse = document.documentElement.classList.contains('force-coarse') || (typeof matchMedia === 'function' && !!matchMedia('(pointer:coarse)').matches); } catch (_) { coarse = false; }
  const who = name ? ' ' + name : '';
  const bare = String(verb).split(/\s+/)[0];
  return coarse ? `${bare.charAt(0).toUpperCase()}${bare.slice(1)}…` : `${verb}${who} · ⇧Enter new line`;
}

/* A send box was emptied from code (no input event fired): let its listeners (the auto-grow, the row's has-text class) see it. */
function rowCleared(ta) {
  if (typeof composerGrow === 'function') composerGrow(ta);
  try { if (typeof ta.dispatchEvent === 'function' && typeof Event === 'function') ta.dispatchEvent(new Event('input')); } catch (_) { /* no events here */ }
}

/* Every live session in a state payload, each with its project, repo and whether it sits in the project folder. */
function rosterSessions(st) {
  const out = [];
  for (const p of ((st && st.projects) || [])) {
    for (const r of [p.root, ...(p.repos || [])]) {
      if (!r) continue;
      for (const s of (r.sessions || [])) out.push({ ...s, project: p.name, repo: r.name, folder: r.name === 'root' || !!r.root });
    }
    for (const s of (p.orphan_sessions || [])) out.push({ ...s, project: p.name, repo: s.repo || '?', folder: s.repo === 'root' });
  }
  return out;
}

/* Last activity as epoch seconds: state_at is an ISO string, created is already epoch seconds. */
function sessionActivity(s) {
  const t = s.state_at ? Date.parse(s.state_at) / 1000 : NaN;
  return Number.isFinite(t) ? t : (s.created || 0);
}

function sessionStateKey(s) { return ownKey(STATE_GLYPH, s.state) ? s.state : 'unknown'; }

function sessionCompare(a, b) {
  return SESSION_RANK[sessionStateKey(a)] - SESSION_RANK[sessionStateKey(b)]
    || sessionActivity(b) - sessionActivity(a) || String(a.tmux).localeCompare(String(b.tmux));
}

function sessionHash(tmux) { try { return buildHash('session', { tmux }); } catch (_) { return '#/'; } }

function sessionWhere(s, withProject) {
  const folder = s.folder || s.repo === 'root';
  if (!withProject) return folder ? 'project folder' : (s.repo || '?');
  return folder ? `${s.project} (project folder)` : `${s.project}/${s.repo || '?'}`;
}

function sessionMetaText(s, st) {
  const t = s.stats || {};
  const parts = [GLYPH_LABEL[st]];
  if (t.model) parts.push(t.model);
  if (typeof t.context_pct === 'number') parts.push(`ctx ${Math.round(t.context_pct)}%`);
  return parts.join(' · ');
}

function sessionNudgeable(s) { return sessionAgent(s) !== 'shell' && SESSION_NUDGE_STATES.includes(s.state); }

/* The active rate-limit episode of a state payload, or null. state.rate_limited is the kv record, {value: {session, message, kind, resets_at}, at}
   as the poll serves it, or the bare {session, message, kind, resets_at}: both shapes read the same. An episode is over once resets_at has passed
   (resets_at is epoch seconds); without one it lasts 5 hours from `at`. */
function rateLimitOf(st) {
  const raw = st && st.rate_limited;
  if (!raw || typeof raw !== 'object') return null;
  const v = raw.value && typeof raw.value === 'object' ? raw.value : raw;
  if (!v || typeof v !== 'object' || (!v.session && !v.message && !v.kind)) return null;
  const now = Date.now() / 1000;
  let reset = v.resets_at;
  if (typeof reset === 'string') reset = Date.parse(reset) / 1000;
  if (typeof reset === 'number' && Number.isFinite(reset) && reset > 0) return reset > now ? v : null;
  const at = raw.at ? Date.parse(raw.at) / 1000 : 0;
  return at && now - at > 5 * 3600 ? null : v;
}

/* The full task (state.tasks) behind a session's task chip: by the chip's id, else by the tmux name (legacy tasks bind by name). */
function sessionTask(s, st) {
  const list = (st && st.tasks) || [];
  if (s.task && s.task.id !== undefined && s.task.id !== null) { const t = list.find((x) => x.id === s.task.id); if (t) return t; }
  return list.find((x) => x.tmux === s.tmux && !x.archived_at) || null;
}

function sessionWorktree(s, st) {
  if (WORKTREE_PATH_RE.test(String(s.path || ''))) return true;
  const t = sessionTask(s, st);
  return !!(t && t.mode === 'worktree' && s.task);
}

function sessionRegistryJob(s) {
  const reg = s.flags && s.flags.registry;
  return reg && reg.job && typeof reg.job === 'object' ? reg.job : null;
}

/* Subscriptions held by tail expanders: a row that left the DOM (its block collapsed, the group changed, the session ended) drops its own. */
function sessionTailSweep() { for (const n of Array.from(agentsTails)) if (n.isConnected === false && typeof n.ccDestroy === 'function') n.ccDestroy(); }
function sessionTailStopAll() { for (const n of Array.from(agentsTails)) if (typeof n.ccDestroy === 'function') n.ccDestroy(); }
function sessionTailAvailable() { return typeof Live !== 'undefined' && !!Live && typeof Live.subscribe === 'function' && typeof Live.unsubscribe === 'function'; }

function sessionPerm(tmux) {
  if (!state) return null;
  for (const pr of (state.pending_permissions || [])) if (pr.tmux_name === tmux) return pr;
  return null;
}

/* 10x pass: the row echoes a sent text at once ('› continue'); the next poll's last_prompt is the same text. `from` is any node inside the row. */
function sessionEcho(from, text) {
  const row = from && typeof from.closest === 'function' ? from.closest('.rrow, .peek, .inbox-card') : null;
  const line = row && row.querySelector('.rr-last .dim, .peek-block .dim');
  if (line) setTextIfChanged(line, '› ' + String(text).split('\n')[0].slice(0, 160));
}

async function sessionNudge(s, text, btn) {
  if (!s || (btn && btn.disabled)) return;
  if (btn) btn.disabled = true;
  try {
    await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/keys`, { text, enter: true });
    pageToast(`sent "${text}" to ${s.name || s.tmux}`, 'ok');
    sessionEcho(btn, text);
  } catch (e) { pageToast(e.message, 'bad'); }
  finally { if (btn) btn.disabled = false; }
}

/* The row's own send box (components.js composer): Enter sends, Shift+Enter adds a line; the draft survives polls because the
   row node is kept and patched. The text goes through /keys like the chips (multi-line arrives as one bracketed paste). */
async function sessionSend(s, ta) {
  const text = String(ta.value || '').replace(/\r\n?/g, '\n');
  if (!s || !text.trim()) return;
  ta.disabled = true;
  try {
    await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/keys`, { text, enter: true });
    ta.value = '';
    rowCleared(ta);
    pageToast(`sent to ${s.name || s.tmux}`, 'ok');
    sessionEcho(ta, text);
  } catch (e) { pageToast(e.message, 'bad'); }
  finally { ta.disabled = false; if (typeof ta.focus === 'function') ta.focus(); }
}

async function sessionAck(s) {
  try { await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/ack`); } catch (e) { setError(e.message); }
  await poll(true);
}

function agentsAgeNode(node, epoch) {
  const e = epoch ? String(Math.floor(epoch)) : '';
  if (node.getAttribute('data-epoch') !== e) node.setAttribute('data-epoch', e);
  setTextIfChanged(node, epoch ? fmtAge(epoch) : '');
}

/* One timer for every <time class="age"> on screen; pages start it on mount and stop it on unmount. */
function agentsTick() {
  if (document.hidden) return;
  for (const n of Array.from(document.querySelectorAll('time.age[data-epoch], time.until[data-epoch]'))) {
    const e = parseInt(n.getAttribute('data-epoch'), 10);
    if (e) setTextIfChanged(n, n.classList.contains('until') ? fmtIn(e) : fmtAge(e));
  }
}

function startAgeTicker() {
  agentsTicker.n += 1;
  if (agentsTicker.timer || typeof setInterval !== 'function') return;
  agentsTicker.timer = setInterval(agentsTick, 1000);
  if (agentsTicker.timer && typeof agentsTicker.timer.unref === 'function') agentsTicker.timer.unref();
}

function stopAgeTicker() {
  agentsTicker.n = Math.max(0, agentsTicker.n - 1);
  if (agentsTicker.n || !agentsTicker.timer) return;
  clearInterval(agentsTicker.timer);
  agentsTicker.timer = null;
}

/* The context meter: a 40 px bar and the percentage (class ctx-hi from CTX_HI, ctx-crit from CTX_CRIT). The bar width is set through the CSSOM. */
function ctxMeter() {
  const fill = el('i');
  const pctText = el('span', { class: 'ctx-pct mono' });
  const node = el('span', { class: 'ctx', title: 'context window used' }, el('span', { class: 'ctx-bar' }, fill), pctText);
  node.ccSet = (pct) => {
    const p = Math.max(0, Math.min(100, Math.round(pct)));
    if (node._p === p) return;
    node._p = p;
    fill.style.width = p + '%';
    setTextIfChanged(pctText, p + '%');
    node.classList.toggle('ctx-hi', p >= CTX_HI && p < CTX_CRIT);
    node.classList.toggle('ctx-crit', p >= CTX_CRIT);
  };
  return node;
}

function sessionCostClass(usd) { return usd >= COST_BAD ? ' cost-bad' : usd >= COST_WARN ? ' cost-warn' : ''; }

/* opts: compact (shorter texts), peek (block layout for the dock / sheet), perm (show a pending permission with Allow / Deny),
   showProject (project/repo instead of just the repo), link (the name opens the peek), cls (extra class on the node),
   rich (the dense row: model, context meter, compact, worktree / PR / subagents / cost / limit chips; the nudge chips and the send box stay behind
   the row's own `...` menu > Reply), tail (the tail expander in that menu; defaults to rich), noWhere (hide the repo text: the block above already
   names it), autoOpen (Agents: a waiting row keeps its chips and send box open until the person toggles them).
   v0.5.6d: a row's actions are [Allow / Deny while a permission waits] [Open] [...]: Ack, Reply, Tail and Kill live in the menu and Kill's second tap
   shows inline. node.ccLead(on) says whether this row carries the screen's one filled primary (Allow); the others show it tinted. Only the peek keeps
   Open terminal / Ack / Kill in view. */
function sessionCard(s, opts) {
  const o = Object.assign({ compact: false, peek: false, perm: false, showProject: false, link: true, cls: '', rich: false, tail: null, noWhere: false, autoOpen: false }, opts || {});
  const rich = !!o.rich && !o.peek;
  const wantTail = !o.peek && (o.tail === null || o.tail === undefined ? rich : !!o.tail) && sessionTailAvailable();
  const cur = { s, lead: undefined, pr: null, manual: false };
  const tmux = s.tmux;
  const killKey = 'kill:' + tmux;
  const glyphs = el('span', { class: o.peek ? 'peek-glyphs' : 'rr-g' });
  const nameNode = o.link && !o.peek ? el('a', { class: 'rr-name', href: sessionHash(tmux) }) : el('span', { class: 'rr-name' });
  const where = el('span', { class: 'rr-where' + (o.noWhere ? ' hidden' : '') });
  const age = el('time', { class: 'age rr-age', title: 'last activity' });
  const meta = el('span', { class: 'rr-meta' });
  const promptNode = el('span', { class: 'dim' });
  const msgNode = el('span', { class: 'rr-msg' });
  const permNote = el('span', { class: 'perm-note' });
  const permBtns = el('span', { class: 'actions perm-btns' });
  const ackSlot = el('span', { class: 'slot-ack' });
  const killSlot = el('span', { class: 'slot-kill' });
  const openSlot = el('span', { class: 'slot-open' });     // the peek's Open terminal: the primary only while no permission waits
  const openHref = `/term/${encodeURIComponent(tmux)}`;
  const openNode = (primary) => el('a', { class: 'btn' + (primary ? ' primary' : ''), href: openHref, target: '_blank', rel: 'noopener', text: 'Open terminal' });
  const openLink = o.peek ? null : el('a', { class: 'btn small', href: openHref, target: '_blank', rel: 'noopener', text: 'Open' });
  if (o.peek) { openSlot.append(openNode(true)); cur.openP = true; }
  const chips = el('div', { class: 'chips' + (o.peek ? '' : ' rr-chips'), role: 'group', 'aria-label': 'Quick replies' });
  for (const text of SESSION_NUDGES) {
    const b = el('button', { class: 'chip-btn', type: 'button', text });
    b.addEventListener('click', (e) => { e.stopPropagation(); sessionNudge(cur.s, text, b); });
    chips.append(b);
  }
  let sendRow = null;
  if (!o.peek) {                                             // the peek has its own composer (session.js)
    const ta = composer({ placeholder: sessionPlaceholder('send to', s.name || tmux), label: `send to ${s.name || tmux}`, onSend: () => sessionSend(cur.s, ta) });
    ta.setAttribute('title', `send to ${s.name || tmux}: Enter sends, Shift+Enter adds a line`);
    ta.addEventListener('click', (e) => e.stopPropagation());
    sendRow = el('form', { class: 'rr-send', onsubmit: (e) => { e.preventDefault(); e.stopPropagation(); sessionSend(cur.s, ta); } },
      ta, el('button', { class: 'small primary', type: 'submit', text: 'Send', onclick: (e) => e.stopPropagation() }));
  }

  // rich row: badges (built once, shown and patched in place), the `...` menu with Reply and Tail
  let badges = null;
  let moreBtn = null;
  let tailHost = null;
  let tailPre = null;
  const b = {};
  if (rich) {
    b.model = el('span', { class: 'bdg bdg-model mono hidden' });
    b.acct = el('span', { class: 'bdg bdg-acct hidden' });                    // which subscription account the session runs on: only when the board has more than one
    b.ctx = ctxMeter();
    b.ctx.classList.add('hidden');
    b.compact = el('button', { class: 'chip-btn compact-chip hue-slate hidden', type: 'button', title: 'send /compact to this session', text: 'compact' });
    b.compact.addEventListener('click', (e) => { e.stopPropagation(); sessionNudge(cur.s, '/compact', b.compact); });
    b.worktree = el('span', { class: 'bdg bdg-wt hue-slate hidden', text: 'worktree' });
    b.pr = el('a', { class: 'bdg bdg-pr hue-slate hidden', target: '_blank', rel: 'noopener' });
    b.sub = el('span', { class: 'bdg bdg-sub hue-slate hidden' });
    b.cost = el('span', { class: 'bdg bdg-cost mono hidden', title: 'session cost (API-equivalent)' });
    b.limit = el('span', { class: 'bdg bdg-limit hidden', text: 'limit' });
    b.blocked = el('span', { class: 'bdg bdg-blocked hidden', text: 'blocked' });
    badges = el('span', { class: 'rr-badges' }, b.model, b.acct, b.ctx, b.compact, b.worktree, b.pr, b.sub, b.cost, b.limit, b.blocked);
  }
  if (!o.peek) {
    moreBtn = el('button', { class: 'icon minimal rr-more', type: 'button', 'aria-label': 'More actions', title: 'More: acknowledge, reply, tail, add to quad, kill' }, ic('more'));
    moreBtn.addEventListener('click', (e) => e.stopPropagation());
    if (typeof menu === 'function') menu(moreBtn, () => moreItems());
  }
  if (wantTail) {
    tailPre = el('pre', { class: 'tail' });
    tailHost = el('div', { class: 'rr-tail hidden' }, tailPre);
  }

  /* The `...` menu, read when it opens: only what applies to the session right now. Kill is the first tap of the two-tap button: it asks the row to
     show 'Confirm Kill' / 'Cancel' inline, where the second tap (or Esc / Cancel) decides. */
  function moreItems() {
    const s2 = cur.s;
    const items = [];
    if (s2.needs_attention) items.push({ label: 'Acknowledge', icon: 'tick', onClick: () => sessionAck(cur.s) });
    if (rich && sessionNudgeable(s2)) items.push({ label: node.classList.contains('open') ? 'Hide reply box' : 'Reply', icon: 'comment', onClick: toggleReply });
    if (wantTail) items.push({ label: cur.tailFn ? 'Hide tail' : 'Tail', icon: 'console', onClick: () => { if (cur.tailFn) tailOff(); else tailOn(); } });
    // the Quad view (v0.5.9) needs 840 px: the row offers it where tiles fit (Shell.quadAdd -> Quad.addToQuad: the session joins the saved slots and #/quad opens)
    if (s2.state !== 'ended' && typeof Shell !== 'undefined' && Shell && typeof Shell.wide === 'function' && Shell.wide() && typeof Shell.quadAdd === 'function') {
      items.push({ label: 'Add to quad', icon: 'layout-grid', onClick: () => Shell.quadAdd(tmux) });
    }
    items.push({ label: 'Kill', icon: 'trash', onClick: () => { ui.confirm = killKey; if (typeof repaintPage === 'function') repaintPage(); } });
    return items;
  }

  function toggleReply() { cur.manual = true; node.classList.toggle('open'); syncMore(); }

  /* the `...` button wears the accent while the row's reply box or tail is open */
  function syncMore() { if (moreBtn) moreBtn.classList.toggle('on', (rich && node.classList.contains('open')) || !!cur.tailFn); }

  function tailOn() {
    if (cur.tailFn || !sessionTailAvailable()) return;
    cur.tailFn = (lines) => {
      const text = (Array.isArray(lines) ? lines : []).slice(-TAIL_LINES).join('\n');
      setTextIfChanged(tailPre, text || '(no output yet)');
      tailPre.scrollTop = tailPre.scrollHeight;
    };
    setTextIfChanged(tailPre, 'waiting for output…');
    tailHost.classList.remove('hidden');
    agentsTails.add(node);
    syncMore();
    Live.subscribe(tmux, cur.tailFn);
  }

  function tailOff() {
    const fn = cur.tailFn;
    if (!fn) return;
    cur.tailFn = null;
    agentsTails.delete(node);
    try { if (sessionTailAvailable()) Live.unsubscribe(tmux, fn); } catch (e) { console.error('ccboard tail', e); }
    tailHost.classList.add('hidden');
    syncMore();
  }

  let node;
  let promptHost = promptNode;
  let msgHost = msgNode;
  let permHost = null;                                       // what to hide when no permission is pending (peek: its own block)
  if (o.peek) {
    const block = (label, body, extra) => el('div', { class: 'peek-block' + (extra || '') }, el('span', { class: 'k', text: label }), body);
    promptHost = block('Last prompt', promptNode);
    msgHost = block('Last message', msgNode);
    permHost = o.perm ? block('Needs permission', [permNote, permBtns], ' peek-perm') : null;
    node = el('div', { class: 'peek-card', 'data-tmux': tmux },
      el('div', { class: 'peek-sub' }, glyphs, where, age, meta),
      promptHost, msgHost, permHost,
      el('div', { class: 'peek-actions' }, openSlot, ackSlot, killSlot), chips);
  } else {
    node = el('div', { class: 'rrow' + (o.compact ? ' compact' : '') + (rich ? ' rich' : '') + (o.cls ? ' ' + o.cls : ''), 'data-tmux': tmux },
      glyphs, el('div', { class: 'rr-main' }, nameNode, where, age), rich ? el('div', { class: 'rr-metaline' }, meta, badges) : meta,
      el('div', { class: 'rr-last' }, o.perm ? permNote : null, promptNode, msgNode),
      el('div', { class: 'rr-actions' }, o.perm ? permBtns : null, openLink, killSlot, moreBtn), chips, sendRow, tailHost);
    if (typeof Dnd !== 'undefined') Dnd.bind(node, { drop: 'session', tmux });      // a backlog card dropped on the row is handed to this session (dnd.js)
  }

  function patchPerm(pr) {
    const lead = o.peek || cur.lead !== false;                // the peek is its own surface: its Allow is always the filled one
    const sig = pr ? `${pr.id}:${pr.summary || ''}:${lead ? 1 : 0}` : '';
    cur.pr = pr || null;
    if (cur.perm === sig) return;
    cur.perm = sig;
    permBtns.textContent = '';
    if (pr) {
      permBtns.append(
        el('button', { class: (lead ? 'primary' : 'primary tinted') + ' small', type: 'button', onclick: (e) => { e.stopPropagation(); decide(pr.id, 'allow'); }, text: 'Allow' }),
        el('button', { class: 'danger small', type: 'button', onclick: (e) => { e.stopPropagation(); decide(pr.id, 'deny'); }, text: 'Deny' }));
    }
    permNote.textContent = pr ? String(pr.summary || 'permission request').slice(0, o.peek ? 800 : 200) : '';
    for (const n of (permHost ? [permHost] : [permNote, permBtns])) n.classList.toggle('hidden', !pr);
  }

  const show = (n, on) => n.classList.toggle('hidden', !on);

  function patchBadges(s2, st) {
    const t = s2.stats || {};
    const nudge = sessionNudgeable(s2);
    show(b.model, !!t.model);
    if (t.model) { setTextIfChanged(b.model, String(t.model)); chipHueSet(b.model, chipHue('model', t.model)); }
    const accts = agentsAccounts(st);
    const acct = s2.account && accts.length > 1 ? (accts.find((x) => x.key === s2.account) || { key: s2.account }) : null;
    show(b.acct, !!acct);
    if (acct) {
      setTextIfChanged(b.acct, agentsAcctName(acct));
      chipHueSet(b.acct, chipHue('account', acct.key));
      b.acct.setAttribute('title', `subscription account: ${agentsAcctName(acct)}${acct.email && acct.email !== agentsAcctName(acct) ? ' · ' + acct.email : ''}`);
    }
    const hasCtx = typeof t.context_pct === 'number';
    show(b.ctx, hasCtx);
    if (hasCtx) b.ctx.ccSet(t.context_pct);
    show(b.compact, hasCtx && t.context_pct >= CTX_HI && sessionAgent(s2) === 'claude' && nudge);
    show(b.worktree, sessionWorktree(s2, st));
    const task = sessionTask(s2, st);
    const prNum = task && task.pr_number;
    show(b.pr, !!prNum);
    if (prNum) {
      const bucket = task.ci && task.ci.bucket;
      setTextIfChanged(b.pr, `PR #${prNum}`);
      b.pr.className = 'bdg bdg-pr hue-slate' + (bucket === 'fail' ? ' bad' : bucket === 'pass' ? ' ok' : bucket === 'pending' ? ' warn' : '');
      if (task.pr_url) b.pr.setAttribute('href', task.pr_url); else b.pr.removeAttribute('href');
      b.pr.setAttribute('title', `${task.pr_state || 'PR'}${bucket && bucket !== 'none' ? ' · CI ' + bucket : ''}`);
    }
    const nSub = s2.flags && typeof s2.flags.subagents === 'number' ? s2.flags.subagents : 0;
    show(b.sub, nSub > 0);
    if (nSub > 0) setTextIfChanged(b.sub, `${nSub} subagent${nSub === 1 ? '' : 's'}`);
    const usd = typeof t.cost_usd === 'number' ? t.cost_usd : 0;
    show(b.cost, usd > 0);
    if (usd > 0) { setTextIfChanged(b.cost, '$' + (usd >= 100 ? usd.toFixed(0) : usd.toFixed(2))); b.cost.className = 'bdg bdg-cost mono' + sessionCostClass(usd); }
    const rl = rateLimitOf(st);
    const limited = !!(rl && rl.session === s2.tmux);
    show(b.limit, limited);
    if (limited) b.limit.setAttribute('title', `${rl.message || 'rate limited'}${rl.resets_at ? ' · resets ' + new Date(rl.resets_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''}`);
    const job = sessionRegistryJob(s2);
    const blocked = !!(job && job.state === 'blocked');
    show(b.blocked, blocked);
    if (blocked) b.blocked.setAttribute('title', job.needs ? String(job.needs) : 'blocked job');
  }

  /* What the node shows, as one string: when it did not change since the last patch there is nothing to write. The pieces that come from outside
     the session object (a pending permission, the two-tap Kill, the linked task, the rate-limit episode) are part of it. */
  function sigOf(s2, st) {
    const t = rich ? sessionTask(s2, st) : null;
    const rl = rich ? rateLimitOf(st) : null;
    const pr = o.perm ? sessionPerm(s2.tmux) : null;
    const accts = rich && s2.account ? agentsAccounts(st) : [];
    const ac = accts.length > 1 ? (accts.find((x) => x.key === s2.account) || null) : null;
    return JSON.stringify([s2.state, s2.state_at, s2.name, s2.last_prompt, s2.last_message, s2.needs_attention, s2.agent, s2.launcher, s2.created,
      s2.project, s2.repo, s2.folder, s2.stats, s2.path, s2.flags, s2.task,
      pr ? pr.id + ':' + pr.summary : '', ui.confirm === killKey,
      t ? [t.id, t.pr_number, t.pr_url, t.pr_state, t.mode, t.ci && t.ci.bucket] : null, rl && rl.session === s2.tmux ? [rl.message, rl.resets_at] : null,
      rich ? [s2.account || '', accts.length > 1, ac ? [ac.label, ac.name, ac.email] : null] : null]);
  }

  function patch(s2) {
    cur.s = s2;
    const st = typeof state !== 'undefined' ? state : null;
    const sig = sigOf(s2, st);
    if (node._sig === sig) return;
    node._sig = sig;
    const sk = sessionStateKey(s2);
    const agent = sessionAgent(s2);
    const gk = sk + '|' + agent;
    if (cur.g !== gk) {
      const ag = agentGlyph(agent);
      ag.classList.add(chipHue('agent', agent));             // claude violet, codex teal, shell slate
      glyphs.textContent = '';
      glyphs.append(stateGlyph(sk), ag);
      cur.g = gk;
    }
    setTextIfChanged(nameNode, s2.name || tmux);
    setTextIfChanged(where, sessionWhere(s2, o.showProject));
    if (o.showProject && s2.project) chipHueSet(where, chipHue('project', s2.project));
    agentsAgeNode(age, sessionActivity(s2));
    setTextIfChanged(meta, rich ? GLYPH_LABEL[sk] : sessionMetaText(s2, sk));
    const lim = o.peek ? [800, 2000] : (o.compact || rich ? [120, 160] : [200, 320]);
    const pt = s2.last_prompt ? '› ' + String(s2.last_prompt).slice(0, lim[0]) : '';
    const mt = s2.last_message ? String(s2.last_message).slice(0, lim[1]) : '';
    setTextIfChanged(promptNode, pt);
    setTextIfChanged(msgNode, mt);
    promptHost.classList.toggle('hidden', !pt);
    msgHost.classList.toggle('hidden', !mt);
    node.classList.toggle('attn', !!s2.needs_attention);
    if (o.perm) patchPerm(sessionPerm(s2.tmux));
    if (o.peek) {
      const wantPrimary = !cur.pr;                            // Open terminal leads only while nothing waits on an Allow
      if (cur.openP !== wantPrimary) { cur.openP = wantPrimary; openSlot.textContent = ''; openSlot.append(openNode(wantPrimary)); }
      const ack = !!s2.needs_attention;
      if (cur.ack !== ack) {
        cur.ack = ack;
        ackSlot.textContent = '';
        if (ack) ackSlot.append(el('button', { class: 'small minimal', type: 'button', onclick: (e) => { e.stopPropagation(); sessionAck(cur.s); }, text: 'Ack' }));
      }
    }
    const confirming = ui.confirm === killKey;                // rebuilt only when the two-tap state flips (confirmButton repaints through repaintPage)
    if (cur.kill !== confirming) {
      cur.kill = confirming;
      killSlot.textContent = '';
      if (confirming || o.peek) killSlot.append(confirmButton(killKey, 'Kill', () => api('DELETE', `/api/sessions/${encodeURIComponent(cur.s.tmux)}`), true));
    }
    const nudge = sessionNudgeable(s2);
    chips.classList.toggle('hidden', !nudge);
    if (sendRow) sendRow.classList.toggle('hidden', !nudge);
    if (rich) {
      patchBadges(s2, st);
      if (o.autoOpen && !cur.manual) node.classList.toggle('open', sk === 'waiting' && nudge);   // a waiting row keeps its reply box open until the person toggles it
      syncMore();
    }
  }

  node.ccPatch = patch;
  node.ccDestroy = () => { tailOff(); };
  node.ccCanLead = () => !!cur.pr;                           // only a row with an Allow button can carry the screen's filled primary
  node.ccLead = (on) => {
    if (cur.lead === on) return;
    cur.lead = on;
    if (o.perm) patchPerm(sessionPerm(cur.s.tmux));
  };
  patch(s);
  return node;
}

/* ---------- the roster ---------- */

function agentsCounts(list) {
  const n = { waiting: 0, working: 0, idle: 0, done: 0, errored: 0, ended: 0 };
  for (const s of list) { const k = sessionStateKey(s); if (ownKey(n, k)) n[k] += 1; }
  return n;
}

function agentsSummaryText(list) {
  const n = agentsCounts(list);
  const parts = [`${STATE_GLYPH.waiting} ${n.waiting} need you`, `${STATE_GLYPH.working} ${n.working} working`, `${STATE_GLYPH.idle} ${n.idle} idle`, `${STATE_GLYPH.done} ${n.done} done`];
  if (n.errored) parts.push(`${STATE_GLYPH.errored} ${n.errored} error`);
  if (n.ended) parts.push(`${STATE_GLYPH.ended} ${n.ended} ended`);
  return parts.join(' · ');
}

/* The state summary as the chips Home's summary bar uses (class sum-seg), here a legend rather than filters: a count per state, the error and ended
   segments only while there is one. */
const AGENTS_SEGS = [['waiting', 'need you'], ['working', 'working'], ['idle', 'idle'], ['done', 'done'], ['errored', 'error'], ['ended', 'ended']];

function agentsSummaryNode() {
  const node = el('nav', { class: 'summary sumbar sumbar-static', 'aria-label': 'Sessions by state' });
  const segs = {};
  for (const [k, label] of AGENTS_SEGS) {
    const n = el('span', { class: 'sum-n' });
    const seg = el('span', { class: 'sum-seg sum-' + k + (k === 'errored' || k === 'ended' ? ' hidden' : ''), 'data-state': k }, stateGlyph(k), n, el('span', { class: 'sum-l', text: label }));
    segs[k] = { seg, n };
    node.append(seg);
  }
  node.ccSegs = segs;
  return node;
}

function agentsPatchSummary(node, list) {
  const n = agentsCounts(list);
  for (const [k] of AGENTS_SEGS) {
    const { seg, n: num } = node.ccSegs[k];
    const c = n[k] || 0;
    setTextIfChanged(num, String(c));
    seg.classList.toggle('zero', !c);
    if (k === 'errored' || k === 'ended') seg.classList.toggle('hidden', !c);
  }
}

/* Groups by project (directory): the group with the most urgent session first, then the most recently active. */
function agentsGroups(list) {
  const by = new Map();
  for (const s of list) { if (!by.has(s.project)) by.set(s.project, []); by.get(s.project).push(s); }
  const groups = [];
  for (const [name, items] of by) {
    items.sort(sessionCompare);
    groups.push({ key: name, name, items, rank: SESSION_RANK[sessionStateKey(items[0])], at: Math.max(...items.map(sessionActivity)) });
  }
  return groups.sort((a, b) => a.rank - b.rank || b.at - a.at || a.name.localeCompare(b.name));
}

function agentsGroupNode(g) {
  const name = el('span', { class: 'rg-name' });
  const count = el('span', { class: 'mono' });
  const list = el('div', { class: 'rg-list' });
  const node = el('section', { class: 'rgroup' }, el('div', { class: 'rgroup-head' }, name, count), list);
  const rows = makeKeyedList(list, { key: (s) => s.tmux, create: (s) => sessionCard(s, { compact: true, rich: true, autoOpen: true, perm: true }), patch: (n, s) => n.ccPatch(s) });
  node.ccPatch = (grp) => {
    setTextIfChanged(name, grp.name);
    chipHueSet(name, chipHue('project', grp.name));
    setTextIfChanged(count, `${grp.items.length} session${grp.items.length === 1 ? '' : 's'}`);
    rows.update(grp.items);
    list.classList.toggle('cv-auto', grp.items.length > CV_AUTO_ROWS);
  };
  node.ccPatch(g);
  return node;
}

/* ---------- sessions started outside the board (v0.5.12) ----------
   GET /api/external?agent=codex -> {claude: [], codex: [thread...], at}: the Codex threads of the last 14 days that the board did not start (discovery reads ~/.codex read-only;
   Desktop imports and sub-agent threads are already dropped). A thread, as far as this page reads it (every field may be absent): id (or session_id / thread_id), title (or name),
   cwd, project and repo (the server's reading of the folder, set when it is under the projects directory), openable (false: its folder is outside the projects
   directory or gone), model, effort (or reasoning_effort), tokens (or tokens_used), updated_at (epoch seconds or ms, or an ISO string), branch (or git_branch), originator,
   badge (the originator when it is not codex-tui: the Hermes agent on a box, an IDE: the row carries it as a badge rather than being hidden) and tmux (set when the thread is
   already one of the board's own sessions: such a row is not listed). Where the answer has no `codex` list the page falls back to state.external.codex (the demo board's
   fixture). Open runs POST /api/external/codex/<id>/open: a new board session resuming the thread in its folder ({tmux, ...} back, 201), refused for a folder outside the
   projects directory, so the button is disabled there with the reason in its title (a row without `openable` is judged by state.config.projects_dir). */
const agentsExt = { data: null, err: '', timer: null, busy: null, seq: 0 };
const AGENTS_EXT_MS = 30000;
const AGENTS_EXT_NOTE = 'Codex threads of the last 14 days that were not started here. Open resumes one in a board session, in its folder.';

function agentsExtEpoch(v) {
  if (typeof v === 'number' && Number.isFinite(v) && v > 0) return v > 1e11 ? v / 1000 : v;
  if (typeof v === 'string' && v) { const t = Date.parse(v); return Number.isNaN(t) ? 0 : t / 1000; }
  return 0;
}

function agentsTok(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '';
  if (v < 999.5) return String(Math.round(v));
  const units = ['k', 'M', 'B'];
  let x = v / 1000;
  let i = 0;
  while (x >= 999.5 && i < units.length - 1) { x /= 1000; i += 1; }
  return x.toFixed(1).replace(/\.0$/, '') + units[i];
}

/* One thread of /api/external as the roster draws it, or null when it cannot be resumed (no id) or is already a board session. `base` = the projects directory ('' unknown):
   inside is true / false once it is known whether the folder is under it, null while it is not. */
function agentsExtRow(raw, base) {
  const o = raw && typeof raw === 'object' ? raw : null;
  if (!o) return null;
  const str = (...keys) => { for (const k of keys) { const v = o[k]; if (typeof v === 'string' && v.trim()) return v.trim(); } return ''; };
  const id = str('id', 'session_id', 'thread_id');
  if (!id || o.tmux) return null;
  const cwd = str('cwd');
  let project = str('project');
  let repo = str('repo');
  let inside = typeof o.openable === 'boolean' ? o.openable : null;      // the server's verdict first
  if (!project && base && cwd) {
    const under = cwd === base || cwd.indexOf(base + '/') === 0;
    if (inside === null) inside = under;
    if (under) { const rel = cwd.slice(base.length + 1).split('/').filter(Boolean); project = rel[0] || ''; repo = rel[1] || ''; }
  }
  const originator = str('originator');
  const tokens = [o.tokens_used, o.tokens].find((x) => typeof x === 'number' && Number.isFinite(x) && x > 0) || 0;
  return { agent: 'codex', id, title: str('title', 'name', 'first_user_message', 'preview'), cwd, project, repo, inside, model: str('model'), effort: str('effort', 'reasoning_effort'),
    tokens, at: agentsExtEpoch(o.updated_at !== undefined ? o.updated_at : o.at), branch: str('branch', 'git_branch'),
    badge: str('badge') || (originator && originator !== 'codex-tui' ? originator : '') };
}

/* The rows to draw: what GET /api/external answered, else state.external, newest first. */
function agentsExtRows(st) {
  const base = st && st.config && typeof st.config.projects_dir === 'string' ? st.config.projects_dir.replace(/\/+$/, '') : '';
  const own = agentsExt.data && Array.isArray(agentsExt.data.codex) ? agentsExt.data.codex : null;
  const fb = st && st.external && typeof st.external === 'object' && Array.isArray(st.external.codex) ? st.external.codex : null;
  return (own || fb || []).map((e) => agentsExtRow(e, base)).filter(Boolean).sort((a, b) => b.at - a.at || a.id.localeCompare(b.id));
}

/* A folder the way a row says it: project/repo inside the projects directory, else the last two segments of the path (the whole path is the title). */
function agentsExtWhere(x) {
  if (x.project) return x.repo && x.repo !== 'root' ? `${x.project}/${x.repo}` : x.project;
  const parts = x.cwd.split('/').filter(Boolean);
  return parts.length ? (parts.length > 2 ? '…/' : '/') + parts.slice(-2).join('/') : '';
}

function agentsExtNode(x0) {
  const cur = { x: x0, sig: '' };
  const title = el('span', { class: 'xr-title' });
  const meta = el('span', { class: 'xr-meta' });
  const age = el('time', { class: 'age xr-age', title: 'last activity' });
  const open = el('button', { class: 'small xr-open', type: 'button', onclick: () => agentsExtOpen(cur.x) });
  const node = el('div', { class: 'xrow', 'data-ext': x0.id, 'data-agent': x0.agent },
    el('div', { class: 'xr-main' }, title, meta), el('div', { class: 'xr-act' }, age, open));
  node.ccPatch = (x) => {
    cur.x = x;
    setTextIfChanged(title, x.title || `thread ${x.id.slice(0, 8)}`);
    title.setAttribute('title', x.title ? `${x.title} · ${x.id}` : x.id);
    agentsAgeNode(age, x.at);
    const busy = agentsExt.busy === x.id;
    const outside = x.inside === false;
    setTextIfChanged(open, busy ? 'Opening…' : 'Open');
    open.disabled = busy || !!agentsExt.busy || outside;
    open.setAttribute('title', outside ? 'Its folder is outside the projects directory, or gone: the board only opens threads that live in a project' : 'Resume this thread in a board session');
    open.setAttribute('aria-label', `Open ${x.title || 'thread ' + x.id.slice(0, 8)} in a board session`);
    const sig = JSON.stringify([x.project, x.repo, x.cwd, x.model, x.effort, x.tokens, x.branch, x.badge]);
    if (sig === cur.sig) return;
    cur.sig = sig;
    meta.textContent = '';
    meta.append(el('span', { class: ['bdg', chipHue('agent', x.agent)].join(' '), title: 'Codex thread', text: `${AGENT_GLYPH.codex} Codex` }));
    if (x.badge) meta.append(el('span', { class: 'bdg mono xr-origin', title: `Started by ${x.badge}, not from a Codex terminal`, text: x.badge }));
    const where = agentsExtWhere(x);
    if (where) meta.append(el('span', { class: ['xr-where', x.project ? chipHue('project', x.project) : ''].filter(Boolean).join(' '), title: x.cwd, text: where }));
    if (x.branch) meta.append(el('span', { class: 'bdg bdg-wt mono', title: 'Git branch', text: x.branch }));
    if (x.model) meta.append(el('span', { class: ['bdg bdg-model mono', chipHue('model', x.model)].join(' '), title: 'Model', text: x.model }));
    if (x.effort) meta.append(el('span', { class: 'bdg mono', title: 'Reasoning effort', text: x.effort }));
    if (x.tokens) meta.append(el('span', { class: 'xr-tok', title: 'Tokens this thread used (forks count what they inherited)', text: `${agentsTok(x.tokens)} tokens` }));
  };
  node.ccPatch(x0);
  return node;
}

function agentsExtSection() {
  const count = el('span', { class: 'mono' });
  const list = el('div', { class: 'xlist' });
  const note = el('p', { class: 'dim unote', text: AGENTS_EXT_NOTE });
  const err = el('p', { class: 'warn unote hidden', role: 'alert' });
  const node = el('section', { class: 'xsec hidden', 'data-sec': 'external' }, el('div', { class: 'xsec-head' }, el('span', { class: 'xsec-name', text: 'Started outside the board' }), count), list, note, err);
  return { node, count, err, list: makeKeyedList(list, { key: (x) => x.id, create: agentsExtNode, patch: (n, x) => n.ccPatch(x) }) };
}

function agentsPatchExt(r, st) {
  const rows = agentsExtRows(st);
  r.ext.list.update(rows);
  setTextIfChanged(r.ext.count, `${rows.length} thread${rows.length === 1 ? '' : 's'}`);
  const err = rows.length ? '' : agentsExt.err;                          // a failed read with rows on screen keeps them; with none the section says so
  r.ext.err.classList.toggle('hidden', !err);
  setTextIfChanged(r.ext.err, err ? `Could not read the outside threads: ${err}` : '');
  r.ext.node.classList.toggle('hidden', !rows.length && !err);
}

/* The threads are asked for only on a box that has a Codex (state.agents.codex, installed): every 30 s while the Agents page is open and visible, the first time the state says so. */
function agentsExtWanted(st) {
  const c = st && st.agents && st.agents.codex;
  return !!(c && typeof c === 'object' && c.installed !== false);
}

function agentsExtEnsure(st) {
  const want = agentsExtWanted(st);
  if (want && !agentsExt.timer && typeof setInterval === 'function') {
    agentsExtLoad();
    agentsExt.timer = setInterval(() => { if (!document.hidden) agentsExtLoad(); }, AGENTS_EXT_MS);
    if (agentsExt.timer && typeof agentsExt.timer.unref === 'function') agentsExt.timer.unref();
  } else if (!want && agentsExt.timer) {
    clearInterval(agentsExt.timer);
    agentsExt.timer = null;
  }
}

function agentsExtRepaint() {
  if (agentsPage.refs && agentsPage.refs.ext) agentsPatchExt(agentsPage.refs, currentState());
}

async function agentsExtLoad() {
  const mine = ++agentsExt.seq;
  let d = null;
  try { d = await api('GET', '/api/external?agent=codex'); } catch (e) {
    if (mine !== agentsExt.seq) return;
    agentsExt.err = acctReason(e, 'the board did not answer');
    agentsExtRepaint();
    return;
  }
  if (mine !== agentsExt.seq) return;                                    // a newer load, or the page is gone
  agentsExt.data = d && typeof d === 'object' ? d : null;
  agentsExt.err = '';
  agentsExtRepaint();
}

/* Open: a new board session resuming the thread. One at a time. Success goes to the new session; a refusal is a toast with the board's reason. */
async function agentsExtOpen(x) {
  if (!x || !x.id || agentsExt.busy) return false;
  agentsExt.busy = x.id;
  agentsExtRepaint();
  let r = null;
  try { r = await api('POST', `/api/external/${encodeURIComponent(x.agent)}/${encodeURIComponent(x.id)}/open`); } catch (e) {
    agentsExt.busy = null;
    agentsExtRepaint();
    pageToast(`Could not open it: ${acctReason(e)}`, 'bad');
    return false;
  }
  agentsExt.busy = null;
  const tmux = r && typeof r.tmux === 'string' ? r.tmux : '';
  pageToast(tmux ? `Opened in the board as ${tmux}` : 'Opened in a board session', 'ok');
  agentsExtRepaint();
  if (typeof poll === 'function') { try { await poll(true); } catch (_) { /* the next poll will show it */ } }
  agentsExtLoad();
  if (tmux && typeof navigate === 'function') navigate(sessionHash(tmux));
  return true;
}

const agentsPage = { refs: null };

registerPage('agents', {
  title: 'Agents',
  mount(root) {
    const summary = agentsSummaryNode();
    const roster = el('div', { class: 'roster' });
    const none = pageEmpty('console', 'No live sessions', 'Start one from the + menu: every session on this box shows up here.');
    const ext = agentsExtSection();                                     // v0.5.12: the threads started outside the board, with an Open each
    const loading = !currentState();                                    // first paint before /api/state: skeleton rows, and no empty states yet
    if (loading) none.classList.add('hidden');
    Pages.reset();
    root.append(el('div', { class: 'agents' },
      el('div', { class: 'page-head' }, el('h1', { text: 'Agents' }), summary), ...(loading ? [Pages.skeleton(3)] : []), roster, none, ext.node));
    // a click on a row selects it, so the mouse and j / k share one selection
    roster.addEventListener('click', (e) => {
      const row = e.target && typeof e.target.closest === 'function' ? e.target.closest('.rrow') : null;
      const i = row ? Pages.items().findIndex((x) => x.tmux === row.getAttribute('data-tmux')) : -1;
      if (i >= 0) Pages.setIndex(i);
    });
    agentsPage.refs = { summary, none, ext, groups: makeKeyedList(roster, { key: (g) => 'g:' + g.key, create: agentsGroupNode, patch: (n, g) => n.ccPatch(g) }) };
    startAgeTicker();
  },
  update(st) {
    const r = agentsPage.refs;
    if (!r) return;
    Pages.dropSkeleton();
    const list = rosterSessions(st);
    agentsPatchSummary(r.summary, list);
    r.groups.update(agentsGroups(list));
    r.none.classList.toggle('hidden', list.length > 0);
    agentsExtEnsure(st);
    agentsPatchExt(r, st);
    Pages.sync();                                             // the roster may have reordered or lost the selected session
    Pages.paint();
  },
  unmount() {
    agentsPage.refs = null;
    agentsExt.seq += 1;                                                 // a load still on its way is dropped
    if (agentsExt.timer) { clearInterval(agentsExt.timer); agentsExt.timer = null; }
    stopAgeTicker();
  },
});
