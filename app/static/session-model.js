/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

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


/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

/* ---------- the windows of an account at a given moment (v0.5.17f) ----------
   The arithmetic behind every 5-hour / 7-day figure (limitWindowNow, limitFreshness, limitCaption, limitUsageWindow) lives in core.js, so the topbar pills, the Usage
   gauges and rows, Settings > Accounts and the Home card all say the same thing; what follows reads an account's state row (or the live record of the account in use). */

/* One of an account's windows ('5h' | '7d') from its state row (rl_5h, resets_5h, rl_5h_at, source) at `now`: a limitWindowNow answer, null without a reading. */
function agentsRowWindow(a, win, now) {
  const v = a ? a['rl_' + win] : null;
  if (typeof v !== 'number' || !Number.isFinite(v)) return null;
  return limitWindowNow({ pct: v, resets_at: a['resets_' + win], at: a['rl_' + win + '_at'], source: a.source }, LIMIT_PERIOD[win], now);
}

/* The used percentage of one of an account's windows at `now`: its last reading, 0 once that window has reset since (the reading is then from the window before,
   and a quiet account has not been read again), null without a reading. */
function agentsAcctUsed(a, win, now) {
  const w = agentsRowWindow(a, win, now);
  return w ? w.pct : null;
}

/* One of an account's windows, as the account in use answers it: with the numbers the topbar pills show (state.usage, the freshest reading: a session's statusline or
   Claude Code's cache), so no screen shows two figures for it; any other account from its row. A limitWindowNow answer, null without a reading. */
function agentsAcctWindow(st, a, win, now) {
  if (a && (a.current || (st && st.accounts && st.accounts.current === a.key))) {
    const live = limitUsageWindow(st, win, now);
    if (live) return live;
  }
  return agentsRowWindow(a, win, now);
}

/* Same, but only the percentage. */
function agentsAcctUsedNow(st, a, win, now) {
  const w = agentsAcctWindow(st, a, win, now);
  return w ? w.pct : null;
}


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
