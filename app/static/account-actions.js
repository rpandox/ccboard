/* Account mutations; loaded on first action. */
'use strict';
async function acctProblemBack(p) {
  const to = p && p.back ? agentsAccounts(state).find((x) => x.key === p.back.key) : null;
  return to ? accountSwitch(to) : false;
}

async function acctProblemDismiss() {
  try {
    await api('DELETE', '/api/accounts/problem');
  } catch (e) {
    if (typeof setError === 'function') setError(acctReason(e));
    return false;
  }
  if (state && state.accounts) state.accounts.problem = null;
  if (typeof demoOn === 'function' && demoOn()) acctDemo().problemOff = true;
  accountsRepaint();
  if (typeof renderBanner === 'function') renderBanner();
  if (typeof poll === 'function') poll(true);
  return true;
}

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

function accountLogin(email) {
  acctFlow.want = { email: typeof email === 'string' && email ? email : null };
  if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('settings', {}, { sec: 'accounts' }));
  if (typeof settingsPage !== 'undefined' && settingsPage.refs && typeof settingsShow === 'function') settingsShow('accounts');   // already on Settings: the route may not change
}

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
