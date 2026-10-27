/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

/* ---------- saved logins and the one-tap switch (v0.5.17c UI) ----------
   acctFlow is the page-side state of the account flows, shared by Settings > Accounts, the Usage callout and the Log in buttons:
     busy     the key being switched to while POST /api/accounts/<key>/switch runs (every row's Switch is disabled meanwhile)
     hold     {key}: the switch is painted as done; the poll's answer is laid over so a poll that lands mid-request does not flip the rows back.
              It ends with the request (the server's accounts replace the local ones); only the demo board keeps it, its fixtures never change
     want     {email}: set by accountLogin(), taken by Settings > Accounts, which then starts the add flow
     err      the reason of the last refused switch / forget, shown in the Accounts panel's error line
     demo     the demo board's make-believe login ({login, accounts, forgot, renewed, problemOff}), laid over the poll's answer like hold */
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

/* Whether this box keeps saved logins (Linux with Claude's file credentials) and, if not, why: `reason` is the server's own per-system text (macOS: the
   Keychain; Windows: use WSL2; a config folder on a mount that ignores permissions), shown as given. A state without `store` (older fixtures) counts as no. */
function acctStore(st) {
  const s = st && st.accounts && st.accounts.store;
  return { supported: !!(s && s.supported), reason: (s && typeof s.reason === 'string' && s.reason) || '' };
}

/* The demo board's make-believe account actions (no box behind it): a login in flight, accounts it "added", logins it "forgot", logins it "renewed" ({key: ISO time}, a
   log-in-again) and whether the made-up login problem (?problem=1) was dismissed or fixed. Created on first use. */
function acctDemo() {
  if (!acctFlow.demo) acctFlow.demo = { login: null, accounts: [], forgot: [], renewed: {}, problemOff: false };
  return acctFlow.demo;
}

/* ---------- a login the box was told is no good (v0.5.17g) ----------
   state.accounts.problem = {at, agent, account, label, session, message, back} or null: a session reported an authentication failure (the box never reads a credentials
   file; it learns this from the failure). `account` is the account key (null when the box could not tell), `label` what it is called, `back` {key, label} of the account to
   switch back to when the failure came within ten minutes of a switch to this one. It ends by itself when that account shows a sign of life, when a login for it finishes,
   or with dismiss (DELETE /api/accounts/problem). The Home banner, the topbar chip, the flagged Settings row and the notification all read it. */
function acctProblem(st) {
  const p = st && st.accounts && st.accounts.problem;
  return p && typeof p === 'object' && p.at ? p : null;
}

/* Is this row (a: a Claude row, or a Codex row with agent 'codex') the account the problem is about? A problem without an account is the account in use. */
function acctProblemHit(st, a, agent) {
  const p = acctProblem(st);
  if (!p || !a || (p.agent || 'claude') !== (agent || 'claude')) return false;
  return p.account ? p.account === a.key : !!a.current;
}

function acctProblemText(p) {
  const who = p && p.agent === 'codex' ? 'Codex' : 'Claude';
  const label = p && typeof p.label === 'string' ? p.label.trim() : '';
  return `${who}'s login${label ? ` (${label})` : ''} is not valid any more`;
}

function acctProblemHash(p) {
  try { return buildHash('settings', {}, p && p.account ? { sec: 'accounts', acct: p.account } : { sec: 'accounts' }); } catch (_) { return '#/settings?sec=accounts'; }
}

function acctProblemOpen(p) { if (typeof navigate === 'function') navigate(acctProblemHash(p)); }

/* 'Switch back to <label>': the one-tap switch to the account that was in use before the failing switch. */

/* 'dismiss': the box forgets the notice (a false alarm, or the login was fixed in a terminal). */

/* The demo board's login problem (?problem=1): the person "switched" to the second saved account and its login turned out dead, so the board says so and offers the way
   back. Laid over the poll like the rest of the demo's account state; a switch made on the demo board moves it along, dismiss or a log-in-again of the account ends it. */
function acctDemoProblem(a) {
  let on = false;
  try { on = typeof demoOn === 'function' && demoOn() && /[?&]problem=1/.test(location.search); } catch (_) { on = false; }
  const d = acctFlow.demo;
  if (!on || (d && d.problemOff)) return;
  const saved = a.list.filter((x) => x && x.saved);
  if (saved.length < 2) return;
  const [first, flagged] = saved;
  if (!acctFlow.hold) { for (const x of a.list) if (x) x.current = x.key === flagged.key; a.current = flagged.key; }
  const live = a.current === flagged.key;
  a.problem = { at: new Date(Date.now() - 3 * 60000).toISOString(), agent: 'claude', account: flagged.key, label: agentsAcctName(flagged), session: 'shop--api--s1',
    message: 'Invalid API key · Please run /login', back: live ? { key: first.key, label: agentsAcctName(first) } : null };
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
      for (const x of a.list) if (x && d.forgot.includes(x.key)) { x.saved = false; x.saved_at = null; }
      for (const x of a.list) if (x && x.saved && d.renewed[x.key]) x.saved_at = d.renewed[x.key];
    }
    const h = acctFlow.hold;
    if (h && a.list.some((x) => x && x.key === h.key)) {
      for (const x of a.list) if (x) x.current = x.key === h.key;
      a.current = h.key;
    }
    acctDemoProblem(a);
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

/* The Log in buttons (Settings > Agents, the Home banner): the add flow lives in Settings > Accounts, so go there and start it. email: the account to sign in again. */

/* ---------- saved Codex logins (v0.5.17e): the twin of the Claude flow above, on state.codex_accounts ----------
   state.codex_accounts = {current, list: [{key, label, account_id, plan, saved, saved_at, current, added_at, last_seen}], store: {supported, add, reason, count}, login: {running, adding,
   label, replace_key, started_at, url, code, result}} (older boxes and fixtures have none: the Codex section of Settings > Accounts is then not shown). An account is named by its label alone: Codex
   gives no email at login. cxFlow is its page-side state, separate from acctFlow so a Claude switch and a Codex switch never block each other:
     busy   the key being switched to while POST /api/codex-accounts/<key>/switch runs
     hold   {key}: the switch is painted as done and laid over the poll until the request ends (the demo board keeps it)
     err    the reason of the last refused switch / forget; note: the warnings of the last switch (other Codex processes keep the previous login)
     demo   the demo board's make-believe login, accounts it "added" and logins it "forgot" */
const cxFlow = { busy: null, hold: null, err: '', note: '', loggingOut: false, demo: null };

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
  if (!cxFlow.demo) cxFlow.demo = { login: null, accounts: [], forgot: [], renewed: {}, current: null };
  return cxFlow.demo;
}

/* Lay the unfinished Codex actions over a fresh /api/state answer (accountOverlay calls this): the demo's accounts, logins and login, and a switch still running. */
function cxOverlay(s) {
  const c = s && s.codex_accounts;
  if (!c || !Array.isArray(c.list)) return s;
  const d = typeof demoOn === 'function' && demoOn() ? cxFlow.demo : null;
  if (d) {
    for (const x of d.accounts) if (!c.list.some((y) => y && y.key === x.key)) c.list.push({ ...x });
    for (const x of c.list) if (x && d.forgot.includes(x.key)) { x.saved = false; x.saved_at = null; }
    for (const x of c.list) if (x && x.saved && d.renewed[x.key]) x.saved_at = d.renewed[x.key];
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
  if (typeof settingsPage !== 'undefined' && settingsPage.refs && typeof settingsFill === 'function') {
    settingsFill('accounts', true);
    if (settingsPage.active === 'agents') settingsFill('agents', true);                // the Codex card has the Log out too
  }
}

/* THE Codex switch (the Settings rows call it): the tapped row becomes the one in use at once, POST /api/codex-accounts/<key>/switch follows, success takes the server's
   accounts and says so in a toast (a warning toast and the panel's note line when other Codex processes on the box keep the previous login), a refusal puts every row back
   and says why (the panel's error line and a toast). One switch at a time. Resolves true when the account is now in use. */

/* ---------- the account actions outside Settings (v0.5.101): the topbar chip's menu and the command palette ----------
   acctChoices(st) lists every Claude and Codex account of a state payload with what a tap on it can do, so those two surfaces never re-decide it: `switchable` is Settings'
   Switch rule (the box keeps saved logins, the row is not the one in use, and it has a saved login), `run` the same accountSwitch / cxSwitch the Settings rows call, `flagged`
   the box having been told this login does not work, and `relogin` what Log in again does from here: Claude goes through accountLogin (Settings > Accounts, the sign-in
   started), Codex opens its row there, where Log in again is the lead action. [] without accounts. */
function acctChoices(st) {
  const out = [];
  const add = (agent, list, can, nameOf, run, relogin) => {
    for (const a of list) {
      out.push({ agent, key: a.key, account: a, name: nameOf(a), plan: typeof a.plan === 'string' ? a.plan : '', current: !!a.current,
        switchable: can.switch && !a.current && !!a.saved, flagged: acctProblemHit(st, a, agent), canRelogin: can.relogin,
        run: () => run(a), relogin: () => relogin(a) });
    }
  };
  const cs = acctStore(st);
  add('claude', agentsAccounts(st), { switch: cs.supported, relogin: cs.supported }, agentsAcctName, accountSwitch, (a) => accountLogin(a.email));
  const xs = cxStore(st);
  add('codex', cxAccounts(st), { switch: xs.supported, relogin: xs.supported && xs.add }, cxName, cxSwitch,
    (a) => { if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('settings', {}, { sec: 'accounts', acct: a.key })); });
  return out;
}

/* The reply box's placeholder: a phone gets the bare verb ('Reply…', 'Send…'): the one-row box is about 330 px wide and a long session name wrapped onto a
   second line that was cut off at the bottom, and there is no Shift+Enter on a touch keyboard. The name and the hint live in the box's title and aria-label
   (the callers set both). A fine pointer gets the full text. */
