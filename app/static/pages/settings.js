/* ccboard settings page (#/settings[?sec=notify|nodes|box|doctor|agents|accounts|app]): the Notify panel (Web Push, ntfy, backup), the Nodes strip, the
   box health, the Doctor (v0.5.19: the checklist, pages/doctor.js), the agents (v0.5.19: a card each for Claude and Codex with version, login, hooks, the models the launcher offers,
   the doctor's checks and the sessions started outside the board; Log in leads to the Accounts add blocks), the subscription Accounts (v0.5.17b: label, email, plan, windows, last seen, Rename, and how to add another)
   and the App panel (installed or browser, Install, Safari steps, build id, Reload app, shortcuts).
   The section is picked with ?sec= and tabs(); each panel is rebuilt only when the
   state it shows changed, so a poll never recreates a button under a finger. renderNotifyPanel() and renderNodes() stay global:
   core.js (enablePush / disablePush) calls the first one. Adding an account (the sign-in link, the code) and switching accounts live in the Accounts panel;
   the Log in buttons of Agents and Home lead there (accountLogin, pages/agents.js). Below the Claude accounts sits the Codex section (v0.5.17e: settingsCxRow, settingsCxAddBlock,
   cxSwitch in pages/agents.js): the same rows and add block on state.codex_accounts, with the device login's one-time code shown to type on the page. */
'use strict';

const SETTINGS_SECTIONS = [
  { id: 'notify', label: 'Notifications' }, { id: 'nodes', label: 'Nodes' }, { id: 'box', label: 'Box' }, { id: 'doctor', label: 'Doctor' }, { id: 'agents', label: 'Agents' }, { id: 'accounts', label: 'Accounts' }, { id: 'app', label: 'App' },
];
const settingsPage = { refs: null, active: 'notify', acct: { at: 0, full: null }, add: null, seenAt: null, cx: null, cxSeenAt: null, focus: null, wantCx: false, vis: null, ext: null, extAsked: false };         // acct: when GET /api/accounts last ran, and its rows by key (for 'seen 3h ago'); add: the add-account block of this mount; seenAt: the login result already announced; cx / cxSeenAt: the same for the Codex add block (v0.5.17e)

/* One setting as the shared .kv row (v0.5.6d): the label in a 120 px column, the value in mono with its helper text under it, the actions at the right
   (under the value on a phone). Buttons, link-buttons and the two-tap pair go to the actions, everything else to the value. row.add() files late
   arrivals (the Web Push state resolves after the panel is built) the same way. */
function settingsIsAction(k) {
  if (!k || k.nodeType !== 1) return false;
  const tag = String(k.tagName || '').toUpperCase();
  return tag === 'BUTTON' || (tag === 'A' && k.classList.contains('btn')) || (tag === 'SPAN' && k.classList.contains('row'));
}

function settingsKv(label, ...kids) {
  const val = el('div', { class: 'kv-main' });
  const act = el('div', { class: 'kv-act' });
  const row = el('div', { class: 'kv' }, el('b', { class: 'k', text: label }), val, act);
  row.add = (...more) => {
    for (const k of more.flat(Infinity)) {
      if (k === null || k === undefined || k === false) continue;
      if (settingsIsAction(k)) act.append(k); else val.append(k);
    }
    act.classList.toggle('hidden', !act.firstChild);
    return row;
  };
  return row.add(...kids);
}

/* A section heading inside a panel: 13 px, upper case, dim. */
function settingsHead(text) { return el('h3', { class: 'set-h', text }); }

/* What to notify (v0.5.18): five switches stored on the box (kv notify_prefs through /api/notify/prefs) and an example of the notice each one
   sends, built by the same code that builds the real ones (GET answers {prefs, samples}). The panel is rebuilt on a minute tick: what was
   fetched stays in settingsNotifyState and paints at once, then refreshes in the background. */
const NOTIFY_KINDS = [
  { key: 'needs', label: 'Needs you', pick: 'Needs you', note: 'A permission to answer, a question for you, or a session waiting on you.' },
  { key: 'done', label: 'Done', pick: 'Done', note: 'A session finished its turn. The middle steps of a chain stay quiet.' },
  { key: 'limit', label: 'Rate limit', pick: 'Limit', note: 'Claude or Codex ran out of allowance. You hear it once until it resets.' },
  { key: 'error', label: 'Error or crash', pick: 'Error', note: 'A turn failed for some other reason.' },
  { key: 'login', label: 'Login problem', pick: 'Login', note: 'A saved login stopped working.' },
];
/* The same five examples GET /api/notify/prefs answers, for ?demo=1 (the demo layer has no fixture file for this route). */
const SETTINGS_NOTIFY_DEMO = { prefs: { needs: true, done: true, limit: true, error: true, login: true }, samples: {
  needs: { title: '◆ shop/api · s1: needs you', body: 'Fix login redirect\n› fix the login redirect\n? Bash: npm test', buttons: ['Allow', 'Deny'] },
  done: { title: '◆ shop/api · s1: done', body: 'Fix login redirect\n› fix the login redirect\n? Fixed the redirect and the tests pass.', buttons: ['Terminal', 'Ack'] },
  limit: { title: 'Claude rate limited', body: '◆ shop/api · s1\nFix login redirect\n› fix the login redirect\n? 5-hour limit reached, resets 14:00', buttons: ['Terminal', 'Ack'] },
  error: { title: '◆ shop/api · s1: error', body: 'Fix login redirect\n› fix the login redirect\n? API Error: 500 Internal server error', buttons: ['Terminal', 'Ack'] },
  login: { title: 'Claude login not valid: work', body: '◆ shop/api · s1\nPlease run /login\nLog in again in Settings > Accounts.', buttons: [] },
} };
const settingsNotifyState = { prefs: { needs: true, done: true, limit: true, error: true, login: true }, samples: {}, kind: 'needs', at: 0, repaint: null };

function settingsNotifyAdopt(r) {
  const st = settingsNotifyState;
  if (r && r.prefs && typeof r.prefs === 'object') st.prefs = Object.assign({}, st.prefs, r.prefs);
  if (r && r.samples && typeof r.samples === 'object') st.samples = r.samples;
}

async function settingsNotifyLoad() {
  const st = settingsNotifyState;
  if (st.at && Date.now() - st.at < 20000) return;
  st.at = Date.now();
  if (typeof demoOn === 'function' && demoOn()) { if (!Object.keys(st.samples).length) { settingsNotifyAdopt(SETTINGS_NOTIFY_DEMO); if (st.repaint) st.repaint(); } return; }
  try { settingsNotifyAdopt(await api('GET', '/api/notify/prefs')); } catch (e) { st.at = 0; setError(e.message); return; }
  if (st.repaint) st.repaint();
}

async function settingsNotifySet(key, on, box) {
  const st = settingsNotifyState;
  const before = st.prefs[key];
  st.prefs[key] = on;
  if (st.repaint) st.repaint();
  try { if (!(typeof demoOn === 'function' && demoOn())) settingsNotifyAdopt(await api('PUT', '/api/notify/prefs', { [key]: on })); setError(null); } catch (e) {
    st.prefs[key] = before;
    if (box) box.checked = before !== false;
    setError(e.message);
  }
  if (st.repaint) st.repaint();
}

function settingsNotifyBlock() {
  const st = settingsNotifyState;
  const boxes = {};
  const list = el('div', { class: 'set-prefs', role: 'group', 'aria-label': 'What to notify' });
  for (const k of NOTIFY_KINDS) {
    const box = el('input', { type: 'checkbox' });
    box.checked = st.prefs[k.key] !== false;
    box.addEventListener('change', () => settingsNotifySet(k.key, box.checked, box));
    boxes[k.key] = box;
    list.append(el('label', { class: 'set-check set-pref' }, box, el('span', { class: 'set-pref-t' }, el('b', { text: k.label }), el('span', { class: 'dim', text: k.note }))));
  }
  const seg = el('div', { class: 'seg-ctl set-kind-seg', role: 'group', 'aria-label': 'Example for' });
  const btns = {};
  const pick = (key, focus) => { st.kind = key; paint(); if (focus) btns[key].focus(); };
  for (const k of NOTIFY_KINDS) {
    btns[k.key] = el('button', { class: 'seg-btn', type: 'button', 'data-kind': k.key, 'aria-pressed': 'false', text: k.pick, onclick: () => pick(k.key), onkeydown: (e) => {
      const i = NOTIFY_KINDS.findIndex((x) => x.key === st.kind);
      const to = e.key === 'ArrowRight' ? NOTIFY_KINDS[(i + 1) % NOTIFY_KINDS.length] : e.key === 'ArrowLeft' ? NOTIFY_KINDS[(i + NOTIFY_KINDS.length - 1) % NOTIFY_KINDS.length] : null;
      if (!to) return;
      e.preventDefault();
      pick(to.key, true);
    } });
    seg.append(btns[k.key]);
  }
  const card = el('div', { class: 'set-notif', role: 'group', 'aria-label': 'Example notification' });
  const cap = el('div', { class: 'dim set-note set-notif-cap' });
  function paint() {
    for (const k of NOTIFY_KINDS) {
      btns[k.key].setAttribute('aria-pressed', k.key === st.kind ? 'true' : 'false');
      if (boxes[k.key].checked !== (st.prefs[k.key] !== false)) boxes[k.key].checked = st.prefs[k.key] !== false;
    }
    card.textContent = '';
    const smp = st.samples[st.kind];
    if (!smp) { card.append(el('div', { class: 'dim', text: 'The example is not available right now.' })); cap.textContent = ''; return; }
    card.append(el('div', { class: 'set-notif-app dim', text: 'ccboard · now' }), el('div', { class: 'set-notif-title', text: smp.title }),
      ...String(smp.body || '').split('\n').map((line) => el('div', { class: 'set-notif-line' + (line.startsWith('?') ? ' mono' : ''), text: line })));
    if (smp.buttons && smp.buttons.length) card.append(el('div', { class: 'set-notif-btns' }, ...smp.buttons.map((b) => el('span', { class: 'set-notif-btn', text: b }))));
    cap.textContent = (st.prefs[st.kind] === false ? 'This kind is switched off: nothing like it is sent. ' : '')
      + (smp.buttons && smp.buttons.length ? 'Android shows the buttons under the text. An iPhone shows the text only; a tap opens the session, where Allow, Deny and Ack are one tap away.' : 'A tap opens the board where this needs attention.');
  }
  st.repaint = paint;
  paint();
  return el('div', { class: 'set-notify-kinds' }, settingsHead('What to notify'), list, settingsHead('What it looks like'), seg, card, cap);
}

function settingsNotify(p) {
  p.textContent = '';
  const n = (state.config && state.config.ntfy) || {};
  p.append(settingsHead('Web Push'));
  const pushRow = settingsKv('This device');
  p.append(pushRow);
  // one primary per section: Enable push while it is off; once it is on that button is gone and the test is an ordinary secondary
  pushSubscription().then((sub) => {
    if (sub) pushRow.add(el('span', { class: 'v', text: 'enabled on this device' }),
      el('button', { type: 'button', onclick: async () => { try { const r = await api('POST', '/api/push/test'); if (r.sent) pageToast('Test push sent', 'ok'); else setError('no push sent (' + r.subscriptions + ' subscriptions)'); } catch (e) { setError(e.message); } }, text: 'Send test push' }),
      el('button', { type: 'button', onclick: () => disablePush().catch((e) => setError(e.message)), text: 'Disable' }));
    else pushRow.add(el('span', { class: 'dim', text: 'Works in Chrome/Android and in an installed (Home Screen) PWA on iOS 16.4+.' }),
      el('button', { class: 'primary', type: 'button', onclick: () => enablePush().catch((e) => setError(e.message)), text: 'Enable push on this device' }),
      el('button', { type: 'button', disabled: true, title: 'Enable push first', text: 'Send test push' }));
  }).catch(() => pushRow.add(el('span', { class: 'dim', text: 'Web Push not available here.' })));

  p.append(settingsNotifyBlock());
  settingsNotifyLoad();

  const bc = (state.config && state.config.backup) || {};
  const bk = state.backup;
  p.append(settingsHead('Backup'));
  const bkRow = settingsKv('Last run');
  p.append(bkRow);
  if (bk && bk.at) {
    const r = bk.restic || {};
    const pushed = (bk.push || []).reduce((sum, x) => sum + (x.pushed || []).length, 0);
    bkRow.add(el('span', { class: bk.status === 'ok' ? 'v' : bk.status === 'partial' ? 'v warn' : 'v bad', text: `${bk.status} ${fmtAge(Date.parse(bk.at) / 1000)} ago` }),
      el('span', { class: 'dim', text: (r.snapshot_id ? `snapshot ${String(r.snapshot_id).slice(0, 8)} → ${r.repo || ''}` : (r.skipped ? 'restic off' : 'no snapshot')) + backupPushText(bk) }));
    if (bk.status === 'failed') bkRow.add(el('span', { class: 'bad', text: (bk.errors || []).join(' · ').slice(0, 300) }));
    else if (bk.status === 'partial') bkRow.add(el('span', { class: 'warn', text: ('snapshot ok · ' + (bk.warnings || []).join(' · ')).slice(0, 300) }));   // a backup branch the remote refused, or a fetch that failed
  } else {
    bkRow.add(el('span', { class: 'dim', text: 'no backup has run yet (nightly via ccboard-backup.timer)' + (bc.restic && !bc.restic_installed ? ' · restic is not installed' : '') }));
  }
  bkRow.add(el('span', { class: 'dim', text: (bc.restic ? `restic → ${bc.repo}` : 'restic off') + (bc.push ? ` · unpushed work → ${bc.ns || 'ccboard-backup/<node>'}/<branch> on origin, never to main` : ' · no git push') }),
    el('button', { type: 'button', onclick: async () => { try { await api('POST', '/api/backup/run'); setError(null); pageToast('Backup started', 'ok'); setTimeout(() => poll(true), 3000); } catch (e) { setError(e.message); } }, text: 'Back up now' }));

  p.append(settingsHead('ntfy'));
  const ntfyRow = settingsKv('Subscribe');
  p.append(ntfyRow);
  if (!n.enabled) {
    ntfyRow.add(el('span', { class: 'dim', text: 'not configured on the box (NTFY_URL in /etc/ccboard/env, or rerun install.sh).' }));
    return;
  }
  const base = (n.subscribe_url || '').replace(/\/[^/]*$/, '');
  ntfyRow.add(el('code', { text: n.subscribe_url || '' }),
    el('button', { class: 'small', type: 'button', onclick: async () => { try { await navigator.clipboard.writeText(n.subscribe_url || ''); pageToast('Copied', 'ok'); } catch (_) { pageToast('Copy failed', 'warn'); } }, text: 'Copy' }),
    el('button', { type: 'button', onclick: async () => {
      try { const r = await api('POST', '/api/notify/test'); if (r.ok) { setError(null); pageToast('Test sent to ntfy. If the phone stays silent, check the steps below.', 'ok'); } else setError('ntfy publish failed (is ntfy running?)'); }
      catch (e) { setError(e.message); }
    }, text: 'Send test' }));
  p.append(el('details', { class: 'dim' }, el('summary', { text: 'Phone setup (ntfy app)' }),
    el('ol', {},
      el('li', {}, 'Install the ntfy app (App Store / Play Store) and keep the Tailscale VPN on: the server is only reachable on the tailnet.'),
      el('li', {}, 'Add a subscription → "Use another server" → server ', el('code', { text: base }), ', topic ', el('code', { text: n.topic || '' }), '.'),
      el('li', {}, 'iPhone: this server relays wake-ups through ntfy.sh (no message content); allow notifications for the app. Android: allow the app to run in the background for instant delivery.'),
      el('li', {}, 'Tap "Send test" above. Pushes follow the switches under What to notify. Allow and Deny answer a permission from the notification, “Terminal” opens the session, “Ack” marks it seen.'))));
}

function settingsNodes(p) {
  p.textContent = '';
  const list = (state.nodes && state.nodes.value) || [];
  if (!list.length) { p.append(el('div', { class: 'dim', text: 'No other nodes are configured.' })); return; }
  for (const x of list) {
    const h = x.health || {};
    const txt = x.online
      ? `${x.sessions} sess · ${x.attention} need you${typeof h.cpu_pct === 'number' ? ' · cpu ' + h.cpu_pct + '%' : ''}${h.mem ? ' · ram ' + h.mem.pct + '%' : ''}${h.disk ? ' · disk ' + h.disk.pct + '%' : ''}${x.usage && x.usage.five_hour ? ' · 5h ' + Math.round(x.usage.five_hour.used_percentage) + '%' : ''}`
      : 'offline' + (x.error ? ' · ' + x.error.slice(0, 60) : '');
    const safe = /^https:\/\/[A-Za-z0-9.-]+(:\d+)?$/.test(x.url || '');
    p.append(settingsKv(x.name, el('span', { class: x.online ? (x.attention ? 'v warn' : 'v') : 'v bad', text: txt }),
      safe ? el('a', { class: 'btn small', href: x.url + '/', target: '_blank', rel: 'noopener', title: x.url, text: 'Open' }) : null));
  }
}

function settingsBox(p) {
  p.textContent = '';
  const h = state.health;
  p.append(settingsHead('Host'));
  p.append(settingsKv('Box', el('span', { class: 'v', text: `${state.node_name || (h && h.host) || 'this box'}${state.user ? ' · ' + state.user : ''}` })));
  if (h && (h.cpu_pct !== null || h.mem || h.disk)) {
    if (h.uptime_s) p.append(settingsKv('Uptime', el('span', { class: 'v', text: h.uptime_s >= 86400 ? `${Math.floor(h.uptime_s / 86400)}d ${Math.floor((h.uptime_s % 86400) / 3600)}h` : `${Math.floor(h.uptime_s / 3600)}h` })));
    const gauge = (label, pct, text) => p.append(settingsKv(label, typeof pct === 'number' ? pctBar(pct) : null, el('span', { class: 'v', text })));
    if (typeof h.cpu_pct === 'number') gauge('CPU', h.cpu_pct, `${h.cpu_pct}%`);
    else if (typeof h.load1 === 'number') p.append(settingsKv('Load', el('span', { class: 'v', text: h.load1.toFixed(2) })));
    if (h.mem) gauge('RAM', h.mem.pct, `${h.mem.pct}%`);
    if (h.disk) gauge('Disk', h.disk.pct, `${h.disk.pct}%`);
  } else p.append(el('div', { class: 'dim', text: 'No health data yet.' }));
  const bk = state.backup;
  p.append(settingsHead('Backup'));
  if (bk && bk.at) {
    const failed = bk.status === 'failed';
    const partial = bk.status === 'partial';
    p.append(settingsKv('Last run', el('span', { class: failed ? 'v bad' : partial ? 'v warn' : 'v', title: failed ? (bk.errors || []).join('\n') : partial ? (bk.warnings || []).join('\n') : 'last nightly backup',
      text: `${failed ? 'failed' : partial ? 'ok, backup branch refused' : 'ok'} ${fmtAge(Date.parse(bk.at) / 1000)} ago` })));
  } else p.append(settingsKv('Last run', el('span', { class: 'dim', text: 'no backup has run yet' })));
}

/* What the git step of the last backup did: ' · 2 branches copied to ccboard-backup/ubu2/ in 1 of 9 repos', or that every repo was
   already on the remote. Runs older than the backup branches (no push_ns) keep their old wording. */
function backupPushText(bk) {
  const list = bk.push || [];
  if (bk.push_skipped || !list.length) return '';
  const n = list.reduce((sum, x) => sum + (x.pushed || []).length, 0);
  if (!bk.push_ns) return ` · ${n} branch(es) pushed across ${list.length} repo(s)`;
  const repos = list.filter((x) => (x.pushed || []).length).length;
  if (!n) return ` · ${list.length} repo(s) checked, nothing unpushed`;
  return ` · ${n} branch${n === 1 ? '' : 'es'} copied to ${bk.push_ns}/ in ${repos} of ${list.length} repo(s)`;
}

/* ---------- Agents (v0.5.19): a section each for Claude and Codex ----------
   Claude: who is logged in (the chip, Log in / the two-tap Log out), the version, the hooks, how many models the launcher offers. Codex: the same, plus the account in use (state.codex_accounts)
   and a Log in that leads to the Codex add block of Settings > Accounts (it asks for a name first) and a two-tap Log out (POST /api/codex-accounts/logout: the saved copy stays). Under each, the doctor's checks for that agent
   (pages/doctor.js: the same rows as the Doctor tab, minus the login button, which the card already has). Last, the Codex threads started outside the board, each with Open
   (pages/agents.js: agentsExtRows / agentsExtNode / agentsExtOpen). Nothing here is filled but Claude's Log in while Claude is logged out. */
const SETTINGS_EXT_NOTE = 'Codex threads of the last 14 days that were not started here. Open resumes one in a board session, in its folder.';

function settingsModelCount(agent) {
  if (typeof launcherSchema !== 'function') return 0;
  const m = launcherSchema(agent).models;
  return Array.isArray(m) ? m.length : 0;
}

/* The version / hooks / models rows of one agent. info: {version, hooks: {installed, trust?}}. */
function settingsAgentFacts(p, agent, info) {
  const ver = typeof info.version === 'string' && info.version.trim() ? info.version.trim() : '';
  p.append(settingsKv('Version', el('span', { class: ver ? 'v' : 'v dim', text: ver || 'unknown' })));
  const hooks = info.hooks && typeof info.hooks === 'object' ? info.hooks : null;
  if (hooks) {
    const trust = typeof hooks.trust === 'string' && hooks.trust ? ` · trust: ${hooks.trust}` : '';
    p.append(settingsKv('Hooks', el('span', { class: hooks.installed ? 'v' : 'v warn', text: (hooks.installed ? 'installed' : 'not installed') + trust }),
      el('span', { class: 'dim', text: hooks.installed ? 'the board hears about state changes from the agent itself' : 'without them the board only guesses a session\'s state from its screen' })));
  }
  const n = settingsModelCount(agent);
  if (n) p.append(settingsKv('Models', el('span', { class: 'v', text: `${n} in the catalogue` }), el('span', { class: 'dim', text: 'what the launcher offers for this agent on this box' })));
}

/* The doctor's checks for one agent, in place under its card. No data yet: one dim line (the first answer repaints the panel). */
function settingsAgentChecks(p, agent) {
  const list = doctorAgentChecks(agent);
  if (!list.length) {
    if (doctorStore.err) p.append(el('div', { class: 'dim set-note', text: `Checks could not run: ${doctorStore.err}` }));
    else if (doctorStore.busy || !doctorStore.data) p.append(el('div', { class: 'dim set-note', role: 'status', text: 'Checking…' }));
    return;
  }
  p.append(el('div', { class: 'doc-list doc-inline' }, list.map((c) => doctorRow(c, { noLogin: true }))));
}

function settingsExtPaint() {
  const x = settingsPage.ext;
  if (!x || !x.host || !settingsPage.refs) return;
  const rows = agentsExtRows(state);
  x.host.textContent = '';
  for (const r of rows) x.host.append(agentsExtNode(r));
  const err = rows.length ? '' : agentsExt.err;
  if (!rows.length) x.host.append(el('div', { class: 'dim set-note', text: err ? `Could not read the outside threads: ${err}` : 'No outside Codex threads found.' }));
  setText(x.count, `${rows.length} thread${rows.length === 1 ? '' : 's'}`);
}

/* agents.js calls this after the outside threads were (re)loaded or an Open started: repaint just the list. */
function settingsExtRepaint() { if (settingsPage.refs && settingsPage.active === 'agents') settingsExtPaint(); }

function settingsAgents(p) {
  p.textContent = '';
  settingsPage.ext = null;
  const c = state.claude || {};
  const ac = (state.agents && state.agents.claude) || {};
  p.append(settingsHead('Claude'));
  const badge = el('span', { class: 'badge' });
  // who is logged in is an identity, not a verdict: the agent's own hue (violet for Claude, teal for Codex), never the green that means ok
  if (!c.installed) { badge.classList.add('bad'); badge.textContent = 'claude not installed'; }
  else if (c.loggedIn) { badge.classList.add('hue-violet'); badge.textContent = `Claude: ${c.email || 'logged in'}${c.subscriptionType ? ' (' + c.subscriptionType + ')' : ''}`; }
  else { badge.classList.add('warn'); badge.textContent = 'Claude: not logged in'; }
  const row = settingsKv('Claude', badge);
  if (c.installed && !c.loggedIn) row.add(el('button', { class: 'primary', type: 'button', title: 'Sign in from Settings > Accounts', onclick: () => accountLogin(), text: 'Log in' }));
  if (c.installed && c.loggedIn) row.add(confirmButton('logout', 'Log out', logout, true));          // red-outlined, two taps: the login is not one tap to lose
  p.append(row);
  if (c.installed) settingsAgentFacts(p, 'claude', { version: ac.version || c.version, hooks: ac.hooks });
  settingsAgentChecks(p, 'claude');

  const codex = state.agents && state.agents.codex;
  if (!codex) return;
  p.append(settingsHead('Codex'));
  const cbadge = codex.installed ? el('span', { class: 'badge' }) : el('span', { class: 'v dim', text: 'not installed' });
  const cur = cxAccounts(state).find((a) => a.current);
  if (!codex.installed) { /* the dim words above */ }
  else if (codex.loggedIn) { cbadge.classList.add('hue-teal'); cbadge.textContent = `Codex: ${cur ? cxName(cur) + (cur.plan ? ' (' + cur.plan + ')' : '') : (codex.email || 'logged in')}`; }
  else { cbadge.classList.add('warn'); cbadge.textContent = 'Codex: not logged in'; }
  const crow = settingsKv('Codex', cbadge);
  if (codex.installed) crow.add(el('button', { type: 'button', title: 'Sign in from Settings > Accounts', onclick: () => settingsCodexLogin(), text: codex.loggedIn ? 'Add account' : 'Log in' }));
  if (codex.installed && codex.loggedIn && cxStore(state).supported) {   // red-outlined, two taps; the saved copy stays (Forget login in Accounts is the one that removes a copy)
    crow.add(confirmButton('cx-logout', 'Log out', () => cxLogout(), true));
    crow.add(el('span', { class: 'dim', text: 'Log out clears the current Codex login on this box and keeps its saved copy, so you can switch back. Forget login (in Accounts) removes a copy.' }));
  }
  p.append(crow);
  if (cxFlow.err && cxFlow.err.startsWith('Log out failed')) p.append(el('div', { class: 'warn set-note', role: 'status', text: cxFlow.err }));
  if (codex.installed) settingsAgentFacts(p, 'codex', codex);
  settingsAgentChecks(p, 'codex');

  if (!agentsExtWanted(state)) return;
  p.append(settingsHead('Import external sessions'));
  const count = el('span', { class: 'v' });
  const host = el('div', { class: 'xlist set-ext' });
  settingsPage.ext = { host, count };
  p.append(settingsKv('Outside threads', count, el('span', { class: 'dim', text: SETTINGS_EXT_NOTE }),
    el('button', { type: 'button', onclick: () => agentsExtLoad(), text: 'Look again' })), host);
  settingsExtPaint();
  if (!settingsPage.extAsked && !agentsExt.data) { settingsPage.extAsked = true; agentsExtLoad(); }     // once per visit: the page's own 30 s timer belongs to the Agents page
}

/* Codex's Log in (Settings > Agents, the Doctor): the add block for a Codex account lives in Settings > Accounts and asks for a name first, so go there and put the cursor in it. */
function settingsCodexLogin() {
  settingsPage.wantCx = true;
  if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('settings', {}, { sec: 'accounts' }));
  if (settingsPage.refs && typeof settingsShow === 'function') settingsShow('accounts');           // already on Settings: the route may not change
}

/* ---------- Accounts (v0.5.17b rows, v0.5.17c saved logins): the subscription accounts the board has seen ----------
   state.accounts.list gives the rows (label, email, plan, the two window readings, `saved`: a login is saved on the box); GET /api/accounts adds last_seen, asked once when
   the panel opens and then at most every 5 minutes (the panel rebuilds each minute so the ages move: that is no polling of its own). Rename opens a small sheet: one field,
   Save is the one primary; the label is painted at once and PATCH /api/accounts/<key> follows (a refusal puts the old label back).
   Where the box keeps saved logins (state.accounts.store.supported) a row also has Switch (accountSwitch, pages/agents.js: one tap), Log in again (v0.5.17g: on every row, the
   account in use included: signing the same account in again replaces its saved login) and Forget login, a quiet "login saved 3d ago" line, and the panel carries the
   add-account block: the sign-in link to copy, the code to paste, both without leaving Settings. When the box was told a login does not work (state.accounts.problem, a
   session stopped on an authentication error) that account's row wears an amber 'login not valid any more' chip and its Log in again is the row's `primary tinted`
   action; #/settings?sec=accounts&acct=<key> (the Home banner, the topbar chip, the notification) scrolls to the row.
   The panel is built once (settingsAcctSkeleton): a poll rebuilds only the list of rows, never the add block, so a code being typed keeps its node and its focus. */
const SETTINGS_RECOVERY = 'If anything looks wrong, run /login in any terminal; the board records it.';
const SETTINGS_LOGIN_HOWTO = 'Sign in with another subscription from here: open the link, sign in, paste the code. Running /login in any terminal works too; the board notices within a minute and starts a new row for it.';
const SETTINGS_LOGIN_TERMINAL = 'To use another subscription, run /login in any terminal; the board notices within a minute and starts a new row for it.';
const SETTINGS_CODE_RE = /^[A-Za-z0-9._~-]{1,256}#[A-Za-z0-9._~-]{1,256}$/;           // the server's rule (claude_auth.CODE_RE)
const SETTINGS_CODE_HINT = "paste the whole code shown by the browser, including the part after '#'";
const SETTINGS_DEMO_LINK = 'https://claude.ai/oauth/authorize?code=true&client_id=demo&response_type=code&state=demo';       // what the demo board shows as the sign-in link (nothing signs in)
const SETTINGS_WATCH_SECONDS = 60;                                                      // after a code is sent the page asks for the result every second, this long

const SETTINGS_CX_HOWTO = 'Sign in with another ChatGPT account from here: name it, open the link, sign in and type the one-time code on the page that opens. Nothing is pasted back. The account in use does not change until you switch.';
const SETTINGS_CX_NOTE = 'A Codex that is already running keeps the login it started with. Switch while no Codex session of the board is open; other Codex programs on this box keep the previous login until they restart.';
const SETTINGS_CX_DEMO_LINK = 'https://auth.openai.com/codex/device';                      // what the demo board shows as the sign-in link (nothing signs in)
const SETTINGS_CX_WATCH_SECONDS = 900;                                                     // the page asks for the result every second this long: a device code lives 15 minutes

function settingsAcctSeen(a) {
  const full = settingsPage.acct.full && settingsPage.acct.full[a.key];
  const t = full && full.last_seen ? Date.parse(full.last_seen) / 1000 : 0;
  return t > 0 ? `seen ${fmtAge(t)} ago` : '';
}

function settingsAcctLoad() {
  const m = settingsPage.acct;
  if (typeof api !== 'function' || Date.now() - m.at < 300000) return;
  m.at = Date.now();
  Promise.resolve().then(() => api('GET', '/api/accounts')).then((r) => {
    const rows = r && Array.isArray(r.list) ? r.list : null;
    if (!rows) return;
    m.full = {};
    for (const x of rows) if (x && x.key) m.full[x.key] = x;
    if (settingsPage.refs && settingsPage.active === 'accounts') settingsFill('accounts', true);
  }).catch(() => { /* the rows still show; only 'seen' is missing */ });
}

/* 'login saved 3d ago': when the box last wrote this account's saved login (a.saved_at; for Claude the age of the credentials file at the moment it was copied), '' without one. */
function settingsSavedLine(a) {
  const t = a && a.saved && a.saved_at ? Date.parse(a.saved_at) / 1000 : 0;
  return t > 0 ? `login saved ${fmtAge(t)} ago` : '';
}

function settingsAcctRow(a) {
  const name = agentsAcctName(a);
  const now = Date.now() / 1000;
  const store = acctStore(state);
  const busy = !!acctFlow.busy;                                       // a switch is running: no second action on any row
  const flagged = acctProblemHit(state, a, 'claude');                 // the box was told this account's login does not work
  const chips = el('span', { class: 'set-chips' });
  if (flagged) chips.append(el('span', { class: 'badge warn', title: 'A session stopped on an authentication error: this login does not work any more. Log in again to fix it.', text: 'login not valid any more' }));
  if (a.plan) chips.append(el('span', { class: 'badge ' + chipHue('account', a.key), text: String(a.plan) }));
  if (a.current) chips.append(el('span', { class: 'badge cur', title: 'the account signed in on this box right now', text: 'current' }));
  if (store.supported) {
    chips.append(a.saved ? el('span', { class: 'badge hue-slate', title: 'a login for this account is saved on the box: it can be switched to', text: 'saved login' })
      : el('span', { class: 'dim', text: 'no saved login' }));
  }
  const w5 = agentsAcctWindow(state, a, '5h', now);                 // the account in use shows the pills' numbers
  const w7 = agentsAcctWindow(state, a, '7d', now);
  // a window that reset with nothing recorded since reads 0 % and counts down to the next reset (core.js limitWindowNow, the rule every surface shares)
  const part = (label, w) => (w ? `${label} ${Math.round(w.pct)}%${w.rolled && w.resets_at ? ` (resets in ${fmtIn(w.resets_at)})` : ''}` : `${label} no reading`);
  const seen = settingsAcctSeen(a);
  const usage = (!w5 && !w7 ? 'no usage reading yet' : `${part('5H', w5)} · ${part('7D', w7)}`) + (seen ? ` · ${seen}` : '');
  const newest = [w5, w7].filter((w) => w && w.at).sort((x, y) => y.at - x.at)[0];
  const usageTitle = !w5 && !w7 ? 'no reading yet' : [w5 && w5.rolled ? '5H: no usage recorded since the window reset' : '', w7 && w7.rolled ? '7D: no usage recorded since the window reset' : '',
    newest ? limitFreshness(newest.at, newest.source) : ''].filter(Boolean).join(' · ');
  const kept = settingsSavedLine(a);
  const acts = [];
  if (store.supported && !a.current && a.saved) {
    acts.push(el('button', { class: flagged ? '' : 'primary tinted', type: 'button', disabled: busy, 'aria-label': `Switch to ${name}`, title: 'Make this the account signed in on this box', onclick: () => accountSwitch(a), text: 'Switch' }));   // the flagged row's one lead action is Log in again (el() maps the classes at creation: not classList.add)
  }
  if (store.supported) {                                              // every row: a dead login is a saved one, and the account in use is the one that needs it most
    acts.push(el('button', { class: flagged ? 'primary tinted' : '', type: 'button', disabled: busy, 'aria-label': `Log in again as ${name}`,
      title: flagged ? 'Sign in with this account again: its saved login does not work any more' : 'Sign in with this account again to renew its saved login', onclick: () => settingsAddStart(a.email, name), text: 'Log in again' }));
  }
  acts.push(el('button', { type: 'button', 'aria-label': `Rename ${name}`, title: 'Rename this account', onclick: () => settingsRenameAccount(a), text: 'Rename' }));
  if (store.supported && !a.current && a.saved) {
    acts.push(busy ? el('button', { class: 'danger', type: 'button', disabled: true, text: 'Forget login' })
      : confirmButton(`acct-forget:${a.key}`, 'Forget login', () => settingsForgetLogin(a), false));
  }
  const row = settingsKv(name, chips.firstChild ? chips : null, a.email && a.email !== name ? el('span', { class: 'v', text: a.email }) : null,
    el('span', { class: 'dim', text: usage, title: usageTitle || null }), kept ? el('span', { class: 'dim set-saved', text: kept }) : null, ...acts);
  row.classList.add('set-acct');
  if (acts.length >= 4) row.classList.add('acts-4');                   // four actions on a phone: two rows of two, the destructive one last
  row.setAttribute('data-account', a.key);
  if (flagged) row.classList.add('is-flagged');
  const k = row.querySelector('.k');
  if (k) k.classList.add(chipHue('account', a.key));
  return row;
}

/* Forget login: DELETE /api/accounts/<key>/saved (the second tap of the red button); the row then shows 'no saved login'. The box answers with its accounts. */
async function settingsForgetLogin(a) {
  const name = agentsAcctName(a);
  acctFlow.err = '';
  try {
    const r = await api('DELETE', `/api/accounts/${encodeURIComponent(a.key)}/saved`);
    if (r && r.accounts && Array.isArray(r.accounts.list)) state.accounts = r.accounts;
    else {                                                              // an answer without accounts (the demo's {ok: true}): the local row says it
      const rec = agentsAccounts(state).find((x) => x.key === a.key);
      if (rec) rec.saved = false;
      if (typeof demoOn === 'function' && demoOn()) acctDemo().forgot.push(a.key);
    }
    accountsRepaint();
    pageToast(`Forgot the saved login of ${name}`, 'ok');
  } catch (e) {
    const why = acctReason(e);
    acctFlow.err = `Could not forget the login of ${name}: ${why}`;
    accountsRepaint();
    pageToast(acctFlow.err, 'bad');
  }
}

/* The add-account block, built once per mount (settingsPage.add) and only patched afterwards: idle (Add account, or the one primary Log in while nobody is signed in),
   in flight (step 1: the link to copy or open; step 2: the code to paste, Add account the one primary; Cancel; the terminal output), checking (the code is sent: the page
   asks for the result every second for a minute), a failed login (the reason, Try again) and, where saved logins are not supported, the reason and the /login how-to.
   The two inputs are never rebuilt. A login result is announced once per `result.at` (settingsPage.seenAt). */
function settingsAddBlock() {
  const m = { starting: false, sending: false, checking: false, slow: false, ticks: 0, timer: null, demoTimer: null, err: '', email: null, sawAdding: false, hide: false, again: null, note: '' };   // again: the name of the account being logged in again; note: what the last login said that is worth keeping on screen
  const root = el('div', { class: 'set-add' });
  const title = el('div', { class: 'add-title hidden', role: 'status' });                       // 'Log in again as <name>' while one is in flight
  const note = el('div', { class: 'warn set-note add-note hidden', role: 'status' });

  const urlInput = el('input', { type: 'text', class: 'add-url', readonly: true, autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', 'aria-label': 'Sign-in link', placeholder: 'the link appears here' });
  urlInput.addEventListener('focus', () => { if (typeof urlInput.select === 'function') urlInput.select(); });
  const copy = el('button', { type: 'button', disabled: true, onclick: () => doCopy(), text: 'Copy link' });
  const open = el('a', { class: 'btn', target: '_blank', rel: 'noopener', 'aria-disabled': 'true', text: 'Open' });
  const wait = el('div', { class: 'dim add-wait', role: 'status', text: 'starting the login…' });
  const step1 = el('div', { class: 'add-step' }, el('b', { class: 'add-k', text: 'Step 1' }),
    el('p', { class: 'add-t', text: 'Open this link and sign in with the account you want to add.' }), urlInput, el('div', { class: 'add-btns' }, copy, open));

  const code = el('input', { type: 'text', class: 'add-code', autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', disabled: true, placeholder: 'code#state' });
  const codeField = field('Paste the code the page shows.', code);
  const addBtn = el('button', { class: 'primary', type: 'submit', disabled: true, text: 'Add account' });
  const cancelBtn = el('button', { type: 'button', onclick: () => doCancel(), text: 'Cancel' });
  const status = el('div', { class: 'dim add-status hidden', role: 'status' });
  const form = el('form', { class: 'add-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); submit(); } },
    codeField, el('div', { class: 'add-btns' }, addBtn, cancelBtn), status);
  const step2 = el('div', { class: 'add-step' }, el('b', { class: 'add-k', text: 'Step 2' }), form);

  const tail = el('pre', { class: 'tail add-tail' });
  const details = el('details', { class: 'dim add-out' }, el('summary', { text: 'terminal output' }), tail,
    el('a', { class: 'add-term', href: '/term/_ccboard-login', target: '_blank', rel: 'noopener', text: 'Open the login terminal' }));
  const flight = el('div', { class: 'add-flight hidden' }, title, wait, step1, step2, details);

  const addIdle = el('button', { type: 'button', onclick: () => start(null), text: 'Add account' });
  const logIn = el('button', { class: 'primary hidden', type: 'button', onclick: () => start(null), text: 'Log in' });
  const idle = el('div', { class: 'add-idle' }, el('div', { class: 'dim set-note', text: SETTINGS_LOGIN_HOWTO }), el('div', { class: 'add-btns' }, addIdle, logIn));

  const errText = el('div', { class: 'bad add-err', role: 'alert' });
  const retry = el('button', { class: 'primary', type: 'button', onclick: () => start(m.email || (state && state.login && state.login.email) || null, true, m.again), text: 'Try again' });
  const dismiss = el('button', { type: 'button', onclick: () => { m.err = ''; patch(); }, text: 'Dismiss' });
  const failed = el('div', { class: 'add-error hidden' }, errText, el('div', { class: 'add-btns' }, retry, dismiss));

  const reason = el('div', { class: 'dim set-note add-reason' });
  const off = el('div', { class: 'add-off hidden' }, reason, el('div', { class: 'dim set-note', text: SETTINGS_LOGIN_TERMINAL }));
  root.append(note, idle, flight, failed, off);

  const show = (node, on) => node.classList.toggle('hidden', !on);
  const stopWatch = () => { if (m.timer) { clearInterval(m.timer); m.timer = null; } };
  const watch = () => {                                                  // one timer at most: the result is asked for every second, for a minute
    if (m.timer) return;
    m.ticks = 0;
    m.timer = setInterval(() => {
      m.ticks++;
      if (typeof poll === 'function') poll(true);
      if (m.ticks >= SETTINGS_WATCH_SECONDS) { stopWatch(); m.slow = true; patch(); }
    }, 1000);
  };

  function finish(res) {
    stopWatch();
    m.starting = m.sending = m.checking = m.sawAdding = m.slow = false;
    code.value = '';
    fieldError(codeField, '');
    if (res.ok) m.again = null;                                           // a failed one keeps whose it is: Try again repeats it
    if (res.ok) {
      m.err = '';
      const who = res.name || 'the account';
      m.note = res.different_account ? `That login was a different account (${res.different_account}); it was saved as its own row.` : '';
      if (m.note) pageToast(m.note, 'warn');                              // asked for one account, signed in as another: nothing was lost, and it says so
      else pageToast(`${res.replaced ? 'Logged in again as' : 'Added'} ${who}${res.live ? ' · it is the account in use now' : ''}`, 'ok');
      if (typeof poll === 'function') poll(true);                        // the new row is in the next state
    } else m.err = res.error || 'the login did not complete';
  }

  function patch() {
    const st = typeof state === 'undefined' ? null : state;
    const store = acctStore(st);
    const l = (st && st.login) || {};
    const res = l.result;
    if (res && res.at && res.at !== settingsPage.seenAt) {
      const mine = m.starting || m.sending || m.checking || m.sawAdding;
      settingsPage.seenAt = res.at;                                      // announced once, here or never (a result nobody here waited for is not news)
      if (mine) finish(res);
    }
    if (m.hide && !l.adding) m.hide = false;
    const adding = !!l.adding && !m.hide;
    if (adding) { m.sawAdding = true; m.starting = false; }
    const url = adding && typeof l.url === 'string' ? l.url : '';
    const list = agentsAccounts(st);
    const needLogin = !list.length || !(st.accounts && st.accounts.current) || !!(st.claude && st.claude.installed && st.claude.loggedIn === false);
    const inFlight = m.starting || m.sending || m.checking || adding;
    const mode = !store.supported ? 'off' : m.err ? 'error' : inFlight ? 'flight' : 'idle';
    const known = adding && l.email ? list.find((x) => x.email === l.email) : null;                // a page opened in the middle of a log-in-again knows whose it is from the state
    const again = m.again || (known ? agentsAcctName(known) : '');
    setText(title, again ? `Log in again as ${again}` : '');
    show(title, mode === 'flight' && !!again);
    setText(note, m.note);
    show(note, !!m.note && mode !== 'flight');
    show(idle, mode === 'idle');
    show(flight, mode === 'flight');
    show(failed, mode === 'error');
    show(off, mode === 'off');
    show(addIdle, !needLogin);
    show(logIn, needLogin);
    if (mode === 'off') setText(reason, store.reason ? `${store.reason.charAt(0).toUpperCase()}${store.reason.slice(1)}: adding and switching accounts is off on this box.` : 'Saved logins are not available on this box: adding and switching accounts is off.');
    if (mode === 'error') setText(errText, m.err);
    if (mode !== 'flight') return;
    show(wait, !url);
    if (urlInput.value !== url) urlInput.value = url;
    copy.disabled = !url;
    const link = /^https:\/\//.test(url) ? url : '';
    if (link) { if (open.getAttribute('href') !== link) open.setAttribute('href', link); open.removeAttribute('aria-disabled'); open.classList.remove('is-off'); }
    else { open.removeAttribute('href'); open.setAttribute('aria-disabled', 'true'); open.classList.add('is-off'); }
    const locked = !url || m.sending || m.checking;
    code.disabled = locked;
    addBtn.disabled = locked;
    show(status, m.sending || m.checking);
    setText(status, m.slow ? 'Still checking the code… this is taking longer than usual. Cancel and try again if it does not finish.' : 'Checking the code…');
    const t = (Array.isArray(l.tail) ? l.tail : []).join('\n');
    if (tail.textContent !== t) tail.textContent = t;
  }

  async function doCopy() {
    const url = urlInput.value;
    if (!url) return;
    try { await navigator.clipboard.writeText(url); pageToast('Link copied', 'ok'); return; } catch (_) { /* no clipboard API here: select the field and copy */ }
    let ok = false;
    try { urlInput.focus(); urlInput.select(); ok = !!document.execCommand('copy'); } catch (_) { ok = false; }
    if (ok) pageToast('Link copied', 'ok'); else pageToast('Copy failed: select the link and copy it', 'warn');
  }

  async function start(email, restart, again) {
    if (m.starting || m.sending || m.checking) return;
    m.err = '';
    m.note = '';
    m.again = again || null;
    m.email = email || null;
    m.starting = true;
    m.hide = false;
    m.slow = false;
    patch();
    if (root.scrollIntoView) { try { root.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (_) { /* no scrolling here */ } }
    const body = {};
    if (m.email) body.email = m.email;
    if (restart) body.restart = true;
    try {
      await api('POST', '/api/accounts/login', body);
    } catch (e) {
      m.starting = false;
      m.err = acctReason(e);
      patch();
      return;
    }
    if (typeof demoOn === 'function' && demoOn()) demoStart();
    if (typeof poll === 'function') poll(true);
  }

  async function submit() {
    if (m.sending || m.checking) return;
    const v = code.value.trim();
    if (!SETTINGS_CODE_RE.test(v)) { fieldError(codeField, SETTINGS_CODE_HINT, true); return; }
    fieldError(codeField, '');
    m.sending = true;
    patch();
    try {
      await api('POST', '/api/accounts/login/code', { code: v });
    } catch (e) {
      m.sending = false;
      patch();
      fieldError(codeField, acctReason(e), true);
      return;
    }
    m.sending = false;
    m.checking = true;
    m.slow = false;
    code.value = '';                                                     // the code is used once: it does not stay in the page
    watch();
    if (typeof demoOn === 'function' && demoOn()) demoFinish();
    patch();
    if (typeof poll === 'function') poll(true);
  }

  async function doCancel() {
    stopWatch();
    clearTimeout(m.demoTimer);
    m.starting = m.sending = m.checking = m.sawAdding = m.slow = false;
    m.err = '';
    m.again = null;
    m.hide = true;                                                       // until the poll says the login is gone: the stale `adding` must not bring the steps back
    code.value = '';
    fieldError(codeField, '');
    if (typeof demoOn === 'function' && demoOn()) acctDemo().login = null;
    patch();
    try { await api('DELETE', '/api/accounts/login'); } catch (e) { m.hide = false; m.err = acctReason(e); }
    patch();
    if (typeof poll === 'function') poll(true);
  }

  /* The demo board has no box behind it: the login it shows is made up here and laid over the poll (acctFlow.demo, agents.js accountOverlay). */
  function demoStart() {
    clearTimeout(m.demoTimer);
    m.demoTimer = setTimeout(() => {
      acctDemo().login = { running: true, adding: true, email: m.email, started_at: new Date().toISOString(), result: null,
        url: SETTINGS_DEMO_LINK, tail: ['Opening browser to sign in…', `If the browser did not open, visit: ${SETTINGS_DEMO_LINK}`, 'Paste code here if prompted >'] };
      if (typeof poll === 'function') poll(true);
    }, 900);
  }
  function demoFinish() {
    clearTimeout(m.demoTimer);
    m.demoTimer = setTimeout(() => {
      const d = acctDemo();
      const old = agentsAccounts(state).find((x) => m.email && x.email === m.email);
      let key, name;
      if (old) { key = old.key; name = agentsAcctName(old); d.forgot = d.forgot.filter((k) => k !== key); d.renewed[key] = new Date().toISOString(); if (acctProblemHit(state, old, 'claude')) d.problemOff = true; }
      else {
        key = `demo-added-${d.accounts.length + 1}`;
        name = 'New account';
        d.accounts.push({ key, email: m.email || 'new@example.com', name, label: null, plan: 'pro', rl_5h: null, rl_7d: null, resets_5h: null, resets_7d: null, current: false, saved: true });
      }
      d.login = { running: false, adding: false, url: null, tail: [], email: null, started_at: null, result: { ok: true, key, name, live: !!(old && old.current), ...(old ? { replaced: true } : {}), at: new Date().toISOString() } };
      if (typeof poll === 'function') poll(true);
    }, 2500);
  }

  return {
    root, patch, start,
    dispose() { stopWatch(); clearTimeout(m.demoTimer); },
    get busy() { return m.starting || m.sending || m.checking; },
    get timers() { return m.timer ? 1 : 0; },
  };
}

/* 'Log in again' on a row, and the Log in buttons of Agents and Home (through acctFlow.want): start the add flow in the block, if there is one. `name` is the row's
   account: the block then says 'Log in again as <name>' and the finished login replaces that account's saved one. */
function settingsAddStart(email, name) {
  const b = settingsPage.add;
  if (b && acctStore(state).supported) b.start(email || null, false, name || null);
}

function settingsAcctWant() {
  const w = acctFlow.want;
  if (!w || !settingsPage.refs || settingsPage.active !== 'accounts' || !settingsPage.add || typeof state === 'undefined' || !state) return;
  acctFlow.want = null;
  if (acctStore(state).supported) settingsPage.add.start(w.email);
}

/* ---------- Codex accounts (v0.5.17e): the same rows and the same add block, on state.codex_accounts (pages/agents.js: cxState, cxSwitch) ----------
   An account is named by its label alone (Codex gives no email), wears the teal of its agent, and shows its plan once the box has learned it from a session. Adding one: a
   required name, then Codex's device login: the link to open and a one-time code to TYPE ON THE PAGE (there is nothing to paste back), the page asking for the result every
   second until the box has seen the login. Not one filled primary in this section: the Claude add block owns the panel's one. */
function settingsCxRow(a) {
  const name = cxName(a);
  const store = cxStore(state);
  const busy = !!cxFlow.busy;
  const flagged = acctProblemHit(state, a, 'codex');                  // (nothing raises a Codex problem yet: a dead Codex login shows in the terminal; kept so a later signal needs no new UI)
  const chips = el('span', { class: 'set-chips' });
  if (flagged) chips.append(el('span', { class: 'badge warn', title: 'This login does not work any more. Log in again to fix it.', text: 'login not valid any more' }));
  if (a.plan) chips.append(el('span', { class: 'badge hue-teal', text: String(a.plan) }));
  if (a.current) chips.append(el('span', { class: 'badge cur', title: 'the Codex login in use on this box right now', text: 'current' }));
  if (store.supported) {
    chips.append(a.saved ? el('span', { class: 'badge hue-slate', title: 'a login for this account is saved on the box: it can be switched to', text: 'saved login' })
      : el('span', { class: 'dim', text: 'no saved login' }));
  }
  const ago = (iso) => { const t = iso ? Date.parse(iso) / 1000 : 0; return t > 0 ? fmtAge(t) : ''; };
  const seen = ago(a.last_seen), added = ago(a.added_at);
  const dim = [added ? `added ${added} ago` : '', seen ? `seen ${seen} ago` : ''].filter(Boolean).join(' · ');
  const kept = settingsSavedLine(a);
  const acts = [];
  if (store.supported && !a.current && a.saved) {
    acts.push(el('button', { class: flagged ? '' : 'primary tinted', type: 'button', disabled: busy, 'aria-label': `Switch to ${name}`, title: 'Make this the Codex account in use on this box', onclick: () => cxSwitch(a), text: 'Switch' }));
  }
  if (store.supported && store.add) {                                 // every row: the live account's login is replaced in place, a saved one's is renewed, a forgotten one's comes back
    acts.push(el('button', { class: flagged ? 'primary tinted' : '', type: 'button', disabled: busy, 'aria-label': `Log in again as ${name}`,
      title: 'Sign in with this account again: its saved login is replaced, the account stays one row', onclick: () => settingsCxAddStart(a), text: 'Log in again' }));
  }
  acts.push(el('button', { type: 'button', 'aria-label': `Rename ${name}`, title: 'Rename this account', onclick: () => settingsRenameAccount(a, { codex: true }), text: 'Rename' }));
  if (store.supported && a.current) {                                 // the live login: Log out keeps its saved copy; Forget login is for the others
    acts.push(busy ? el('button', { class: 'danger', type: 'button', disabled: true, text: 'Log out' })
      : confirmButton('cx-logout', 'Log out', () => cxLogout(), false));
  }
  if (store.supported && !a.current && a.saved) {
    acts.push(busy ? el('button', { class: 'danger', type: 'button', disabled: true, text: 'Forget login' })
      : confirmButton(`cx-forget:${a.key}`, 'Forget login', () => settingsCxForget(a), false));
  }
  const row = settingsKv(name, chips.firstChild ? chips : null, dim ? el('span', { class: 'dim', text: dim }) : null, kept ? el('span', { class: 'dim set-saved', text: kept }) : null, ...acts);
  row.classList.add('set-cx');
  if (acts.length >= 4) row.classList.add('acts-4');
  row.setAttribute('data-cx-account', a.key);
  if (flagged) row.classList.add('is-flagged');
  const k = row.querySelector('.k');
  if (k) k.classList.add('hue-teal');
  return row;
}

/* Forget login: DELETE /api/codex-accounts/<key>/saved (the second tap of the red button); the row then shows 'no saved login'. */
async function settingsCxForget(a) {
  const name = cxName(a);
  cxFlow.err = '';
  try {
    const r = await api('DELETE', `/api/codex-accounts/${encodeURIComponent(a.key)}/saved`);
    if (r && r.accounts && Array.isArray(r.accounts.list)) state.codex_accounts = r.accounts;
    else {                                                              // an answer without accounts (the demo's {ok: true}): the local row says it
      const rec = cxAccounts(state).find((x) => x.key === a.key);
      if (rec) rec.saved = false;
      if (typeof demoOn === 'function' && demoOn()) cxDemo().forgot.push(a.key);
    }
    cxRepaint();
    pageToast(`Forgot the saved login of ${name}`, 'ok');
  } catch (e) {
    cxFlow.err = `Could not forget the login of ${name}: ${acctReason(e)}`;
    cxRepaint();
    pageToast(cxFlow.err, 'bad');
  }
}

function settingsCxAddBlock() {
  const m = { starting: false, timer: null, demoTimer: null, ticks: 0, slow: false, err: '', label: '', sawAdding: false, hide: false, tail: [], replace: null, note: '' };      // tail: the login's terminal output, asked for only while the disclosure is open (/api/state leaves it out); replace: {key, label} of the account being logged in again; note: what the last login said that is worth keeping on screen
  const root = el('div', { class: 'set-add cx-add' });
  const title = el('div', { class: 'add-title hidden', role: 'status' });                       // 'Log in again as <name>' while one is in flight
  const note = el('div', { class: 'warn set-note add-note hidden', role: 'status' });

  const name = el('input', { type: 'text', class: 'cx-name', maxlength: 60, autocomplete: 'off', autocapitalize: 'words', placeholder: 'work, personal, a client' });
  const nameField = field('Account name', name, 'Required. Codex gives no email at sign-in, so this name is how the account is listed.');
  const startBtn = el('button', { type: 'submit', disabled: true, text: 'Add Codex account' });
  const nameForm = el('form', { class: 'add-form cx-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); start(name.value); } },
    nameField, el('div', { class: 'add-btns' }, startBtn));
  name.addEventListener('input', () => { startBtn.disabled = !name.value.trim(); if (name.value.trim()) fieldError(nameField, ''); });
  const idle = el('div', { class: 'add-idle' }, el('div', { class: 'dim set-note', text: SETTINGS_CX_HOWTO }), nameForm);

  const urlInput = el('input', { type: 'text', class: 'add-url', readonly: true, autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', 'aria-label': 'Sign-in link', placeholder: 'the link appears here' });
  urlInput.addEventListener('focus', () => { if (typeof urlInput.select === 'function') urlInput.select(); });
  const copyLink = el('button', { type: 'button', disabled: true, onclick: () => doCopy(urlInput.value, 'Link', urlInput), text: 'Copy link' });
  const open = el('a', { class: 'btn', target: '_blank', rel: 'noopener', 'aria-disabled': 'true', text: 'Open' });
  const wait = el('div', { class: 'dim add-wait', role: 'status', text: 'starting the login…' });
  const step1 = el('div', { class: 'add-step' }, el('b', { class: 'add-k', text: 'Step 1' }),
    el('p', { class: 'add-t', text: 'Open this link and sign in with the account you want to add.' }), urlInput, el('div', { class: 'add-btns' }, copyLink, open));

  const codeBox = el('div', { class: 'cx-code', role: 'status', 'aria-label': 'One-time code', text: '…' });
  const copyCode = el('button', { type: 'button', disabled: true, onclick: () => doCopy(codeBox.textContent, 'Code', null), text: 'Copy code' });
  const step2 = el('div', { class: 'add-step' }, el('b', { class: 'add-k', text: 'Step 2' }),
    el('p', { class: 'add-t', text: 'Type this code on the page. Nothing is pasted back here.' }), codeBox, el('div', { class: 'add-btns' }, copyCode));

  const status = el('div', { class: 'dim add-status', role: 'status', text: 'waiting for the sign-in…' });
  const cancelBtn = el('button', { type: 'button', onclick: () => doCancel(), text: 'Cancel' });
  const tail = el('pre', { class: 'tail add-tail' });
  const details = el('details', { class: 'dim add-out' }, el('summary', { text: 'terminal output' }), tail,
    el('a', { class: 'add-term', href: '/term/_ccboard-login', target: '_blank', rel: 'noopener', text: 'Open the login terminal' }));
  const flight = el('div', { class: 'add-flight hidden' }, title, wait, step1, step2, el('div', { class: 'add-step' }, status, el('div', { class: 'add-btns' }, cancelBtn)), details);

  const errText = el('div', { class: 'bad add-err', role: 'alert' });
  const retry = el('button', { type: 'button', onclick: () => start(m.label || name.value, true, m.replace), text: 'Try again' });
  const dismiss = el('button', { type: 'button', onclick: () => { m.err = ''; patch(); }, text: 'Dismiss' });
  const failed = el('div', { class: 'add-error hidden' }, errText, el('div', { class: 'add-btns' }, retry, dismiss));

  const reason = el('div', { class: 'dim set-note add-reason' });
  const cmd = el('code', { class: 'cx-cmd hidden' });
  const off = el('div', { class: 'add-off hidden' }, reason, cmd);
  root.append(note, idle, flight, failed, off);

  const show = (node, on) => node.classList.toggle('hidden', !on);
  const stopWatch = () => { if (m.timer) { clearInterval(m.timer); m.timer = null; } };
  const pullTail = async () => {                                         // state.codex_accounts.login has no `tail`: GET /api/codex-accounts has it, so ask while the disclosure is open
    if (!details.open || typeof api !== 'function') return;
    try {
      const r = await api('GET', '/api/codex-accounts');
      if (r && r.login && Array.isArray(r.login.tail)) { m.tail = r.login.tail; patch(); }
    } catch (_) { /* the output is a convenience: the link and the code are in the state */ }
  };
  details.addEventListener('toggle', pullTail);
  const watch = () => {                                                  // one timer at most: the result is asked for every second, for a device code's lifetime
    if (m.timer) return;
    m.ticks = 0;
    m.timer = setInterval(() => {
      m.ticks++;
      if (typeof poll === 'function') poll(true);
      pullTail();
      if (m.ticks >= SETTINGS_CX_WATCH_SECONDS) { stopWatch(); m.slow = true; patch(); }
    }, 1000);
  };

  function finish(res) {
    stopWatch();
    m.starting = m.sawAdding = m.slow = false;
    m.tail = [];
    if (res.ok) m.replace = null;                                        // a failed one keeps whose it is: Try again repeats it
    if (res.ok) {
      m.err = '';
      const who = res.label || 'the account';
      const warns = Array.isArray(res.warnings) ? res.warnings.filter((w) => typeof w === 'string' && w) : [];
      if (res.replaced) {
        m.note = res.why ? `Logged in again as ${who}, but the fresh login is not in use yet: ${res.why}. It goes in by itself once none is open.` : '';
        pageToast(res.why ? m.note : `Logged in again as ${who}${res.live ? ' · it is the Codex account in use now' : ''}${warns.length ? ` · ${warns.join(' · ')}` : ''}`, res.why || warns.length ? 'warn' : 'ok');
      } else {
        m.note = '';
        name.value = '';
        startBtn.disabled = true;
        pageToast(`Added ${who}${res.live ? ' · it is the Codex account in use now' : ''}`, 'ok');
      }
      if (typeof poll === 'function') poll(true);                        // the new row is in the next state
    } else m.err = res.error || 'the login did not complete';
  }

  function patch() {
    const st = typeof state === 'undefined' ? null : state;
    const store = cxStore(st);
    const c = cxState(st);
    const l = (c && c.login) || {};
    const res = l.result;
    if (res && res.at && res.at !== settingsPage.cxSeenAt) {
      const mine = m.starting || m.sawAdding || !!m.timer;
      settingsPage.cxSeenAt = res.at;                                    // announced once, here or never (a result nobody here waited for is not news)
      if (mine) finish(res);
    }
    if (m.hide && !l.adding) m.hide = false;
    const adding = !!l.adding && !m.hide;
    if (adding) { m.sawAdding = true; m.starting = false; if (!m.timer && !m.slow) watch(); }   // a page opened in the middle of a login keeps asking too
    const inFlight = m.starting || adding;
    const mode = !store.supported || !store.add ? 'off' : m.err ? 'error' : inFlight ? 'flight' : 'idle';
    const again = m.replace ? m.replace.label : (adding && l.replace_key ? (typeof l.label === 'string' && l.label) || cxName(cxAccounts(st).find((x) => x.key === l.replace_key)) : '');   // a page opened mid-login knows whose it is from the state
    setText(title, again ? `Log in again as ${again}` : '');
    show(title, mode === 'flight' && !!again);
    setText(note, m.note);
    show(note, !!m.note && mode !== 'flight');
    show(idle, mode === 'idle');
    show(flight, mode === 'flight');
    show(failed, mode === 'error');
    show(off, mode === 'off');
    if (mode === 'off') {
      const why = store.reason || 'Codex accounts cannot be added on this box.';
      const i = why.indexOf(': ');
      const head = i > 0 ? why.slice(0, i) : why;
      setText(reason, `${head.charAt(0).toUpperCase()}${head.slice(1)}${i > 0 ? ':' : '.'}`);
      setText(cmd, i > 0 ? why.slice(i + 2) : '');
      show(cmd, i > 0);
    }
    if (mode === 'error') setText(errText, m.err);
    if (mode !== 'flight') return;
    const url = adding && typeof l.url === 'string' ? l.url : '';
    const code = adding && typeof l.code === 'string' ? l.code : '';
    show(wait, !url && !code);
    if (urlInput.value !== url) urlInput.value = url;
    copyLink.disabled = !url;
    const link = /^https:\/\//.test(url) ? url : '';
    if (link) { if (open.getAttribute('href') !== link) open.setAttribute('href', link); open.removeAttribute('aria-disabled'); open.classList.remove('is-off'); }
    else { open.removeAttribute('href'); open.setAttribute('aria-disabled', 'true'); open.classList.add('is-off'); }
    setText(codeBox, code || '…');
    codeBox.classList.toggle('is-empty', !code);
    copyCode.disabled = !code;
    setText(status, m.slow ? 'Still waiting… the code has probably expired. Cancel and start again.' : 'waiting for the sign-in…');
    const t = (Array.isArray(l.tail) && l.tail.length ? l.tail : m.tail).join('\n');
    if (tail.textContent !== t) tail.textContent = t;
  }

  async function doCopy(text, what, input) {
    if (!text) return;
    try { await navigator.clipboard.writeText(text); pageToast(`${what} copied`, 'ok'); return; } catch (_) { /* no clipboard API here: select the field and copy */ }
    let ok = false;
    try { if (input) { input.focus(); input.select(); ok = !!document.execCommand('copy'); } } catch (_) { ok = false; }
    if (ok) pageToast(`${what} copied`, 'ok'); else pageToast(`Copy failed: select the ${what.toLowerCase()} and copy it`, 'warn');
  }

  async function start(raw, restart, replace) {                         // replace: the account being logged in again ({key, label}): no name asked for, its login is replaced
    if (m.starting) return;
    const again = replace && replace.key ? { key: replace.key, label: cxName(replace) } : null;
    const label = again ? again.label : String(raw || '').trim().replace(/\s+/g, ' ');
    if (!again) {
      if (!label) { fieldError(nameField, 'Give the account a name.', true); return; }
      if (label.length > 60) { fieldError(nameField, 'At most 60 characters.', true); return; }
      fieldError(nameField, '');
    }
    m.err = '';
    m.note = '';
    m.replace = again;
    m.label = label;
    m.starting = true;
    m.hide = false;
    m.slow = false;
    patch();
    if (root.scrollIntoView) { try { root.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (_) { /* no scrolling here */ } }
    try {
      await api('POST', '/api/codex-accounts/login', { ...(again ? { replace_key: again.key } : { label }), ...(restart ? { restart: true } : {}) });
    } catch (e) {
      m.starting = false;
      m.err = acctReason(e);
      patch();
      return;
    }
    if (typeof demoOn === 'function' && demoOn()) demoStart();
    watch();
    if (typeof poll === 'function') poll(true);
  }

  async function doCancel() {
    stopWatch();
    clearTimeout(m.demoTimer);
    m.starting = m.sawAdding = m.slow = false;
    m.tail = [];
    m.err = '';
    m.replace = null;
    m.hide = true;                                                       // until the poll says the login is gone: the stale `adding` must not bring the steps back
    if (typeof demoOn === 'function' && demoOn()) cxDemo().login = null;
    patch();
    try { await api('DELETE', '/api/codex-accounts/login'); } catch (e) { m.hide = false; m.err = acctReason(e); }
    patch();
    if (typeof poll === 'function') poll(true);
  }

  /* The demo board has no box behind it: the login it shows (a made-up code) is laid over the poll, and a few seconds later the "sign-in" completes (cxFlow.demo, agents.js cxOverlay). */
  function demoStart() {
    clearTimeout(m.demoTimer);
    m.demoTimer = setTimeout(() => {
      cxDemo().login = { running: true, adding: true, label: m.label, replace_key: m.replace ? m.replace.key : null, started_at: new Date().toISOString(), result: null, url: SETTINGS_CX_DEMO_LINK, code: 'DEMO-4821',
        tail: ['Follow these steps to sign in with ChatGPT using device code authorization:', `1. Open this link in your browser and sign in to your account: ${SETTINGS_CX_DEMO_LINK}`, '2. Enter this one-time code: DEMO-4821'] };
      if (typeof poll === 'function') poll(true);
      m.demoTimer = setTimeout(demoFinish, 6000);
    }, 900);
  }
  function demoFinish() {
    const d = cxDemo();
    if (m.replace) {                                                     // a log-in-again: the same row, its saved login renewed
      const old = cxAccounts(state).find((x) => x.key === m.replace.key);
      d.forgot = d.forgot.filter((k) => k !== m.replace.key);
      d.renewed[m.replace.key] = new Date().toISOString();
      d.login = { running: false, adding: false, label: null, replace_key: null, started_at: null, url: null, code: null, tail: [], result: { ok: true, key: m.replace.key, label: m.replace.label, live: !!(old && old.current), replaced: true, why: null, at: new Date().toISOString() } };
      if (typeof poll === 'function') poll(true);
      return;
    }
    const key = `demo-cx-added-${d.accounts.length + 1}`;
    d.accounts.push({ key, label: m.label || 'New Codex account', account_id: null, plan: null, saved: true, saved_at: new Date().toISOString(), current: false, added_at: new Date().toISOString(), last_seen: null });
    d.login = { running: false, adding: false, label: null, replace_key: null, started_at: null, url: null, code: null, tail: [], result: { ok: true, key, label: m.label || 'New Codex account', live: false, at: new Date().toISOString() } };
    if (typeof poll === 'function') poll(true);
  }

  return {
    root, patch, start,
    dispose() { stopWatch(); clearTimeout(m.demoTimer); },
    get busy() { return m.starting || !!m.timer; },
    get timers() { return m.timer ? 1 : 0; },
  };
}

/* 'Log in again' on a Codex row: start the add flow in the block for that account (a: the row's account): the login that finishes replaces its saved one. */
function settingsCxAddStart(a) {
  const b = settingsPage.cx;
  if (b && cxStore(state).add && a && a.key) b.start(null, false, a);
}

/* The panel's skeleton, built the first time the Accounts section is filled and kept: the error line, the rows' host, the recovery line, the switch choice and the add block. */
function settingsAcctSkeleton(p) {
  p.textContent = '';
  const err = el('div', { class: 'set-err bad hidden', role: 'alert' });
  const list = el('div', { class: 'set-acct-list' });
  const cont = el('input', { type: 'checkbox' });
  cont.checked = acctContinuePref();
  cont.addEventListener('change', () => acctContinueSet(cont.checked));
  const sw = el('div', { class: 'set-switching hidden' }, settingsHead('When you switch'),
    el('label', { class: 'set-check' }, cont, 'After a switch, type continue in sessions parked on a limit'));
  if (settingsPage.add) settingsPage.add.dispose();
  const add = settingsAddBlock();
  settingsPage.add = add;
  if (settingsPage.cx) settingsPage.cx.dispose();
  const cx = settingsCxAddBlock();
  settingsPage.cx = cx;
  const cxErr = el('div', { class: 'set-err bad hidden', role: 'alert' });
  const cxList = el('div', { class: 'set-acct-list cx-list' });
  const cxNote = el('div', { class: 'dim set-note cx-note', text: SETTINGS_CX_NOTE });
  const cxWarn = el('div', { class: 'warn set-note cx-warn hidden', role: 'status' });
  const cxSec = el('div', { class: 'cx-section hidden' }, settingsHead('Codex accounts'), cxErr, cxList, cxNote, cxWarn, settingsHead('Add a Codex account'), cx.root);
  p.append(err, settingsHead('Subscription accounts'), list, el('div', { class: 'dim set-note', text: SETTINGS_RECOVERY }), sw, settingsHead('Add another subscription'), add.root, cxSec);
  return { err, list, sw, cont, add, cxSec, cxErr, cxList, cxNote, cxWarn, cx };
}

/* Everything of the panel that is not the rows, repainted on every update: the error line, the switch choice, the add block, a Log in tapped elsewhere. */
function settingsAcctPatch() {
  const r = settingsPage.refs;
  const s = r && r.panels.accounts.ccAcct;
  if (!s) return;
  const msg = acctFlow.err || '';
  if (s.err.textContent !== msg) s.err.textContent = msg;
  s.err.classList.toggle('hidden', !msg);
  s.sw.classList.toggle('hidden', !acctStore(state).supported);
  const pref = acctContinuePref();
  if (s.cont.checked !== pref) s.cont.checked = pref;
  s.add.patch();
  const cs = cxState(state);                                          // older boxes and fixtures have no state.codex_accounts: the Codex section is then not there at all
  s.cxSec.classList.toggle('hidden', !cs);
  const cmsg = cxFlow.err || '';
  if (s.cxErr.textContent !== cmsg) s.cxErr.textContent = cmsg;
  s.cxErr.classList.toggle('hidden', !cmsg);
  const warn = cxFlow.note ? `Switched, but ${cxFlow.note}.` : '';
  if (s.cxWarn.textContent !== warn) s.cxWarn.textContent = warn;
  s.cxWarn.classList.toggle('hidden', !warn);
  s.cxNote.classList.toggle('hidden', !cxStore(state).supported);
  s.cx.patch();
  settingsAcctWant();
  if (settingsPage.wantCx && cxState(state)) {                         // the Codex Log in of Agents / the Doctor: show the block and focus its name field
    settingsPage.wantCx = false;
    if (s.cx.root.scrollIntoView) { try { s.cx.root.scrollIntoView({ block: 'center' }); } catch (_) { /* no scrolling here */ } }
    const nm = s.cx.root.querySelector('.cx-name');
    if (nm && typeof focusFine === 'function') focusFine(nm);
  }
}

/* #/settings?sec=accounts&acct=<key> (the Home banner, the topbar chip, the notification of a login that does not work): scroll to that account's row, once. */
function settingsAcctFocus(s) {
  const key = settingsPage.focus;
  if (!key) return;
  const rows = [...Array.from(s.list.children || []), ...Array.from(s.cxList.children || [])];
  const row = rows.find((r) => r.getAttribute && (r.getAttribute('data-account') === key || r.getAttribute('data-cx-account') === key));
  if (!row) return;
  settingsPage.focus = null;
  if (typeof row.scrollIntoView === 'function') { try { row.scrollIntoView({ block: 'center' }); } catch (_) { /* no scrolling here */ } }
}

function settingsAccounts(p) {
  const s = p.ccAcct || (p.ccAcct = settingsAcctSkeleton(p));
  const list = agentsAccounts(state);
  s.list.textContent = '';
  if (!list.length) s.list.append(el('div', { class: 'dim set-note', text: 'No Claude account seen yet. The board records the account a session is signed in with once one is running.' }));
  for (const a of list) s.list.append(settingsAcctRow(a));
  s.cxList.textContent = '';
  const cl = cxAccounts(state);
  if (cxState(state) && !cl.length) s.cxList.append(el('div', { class: 'dim set-note', text: 'No Codex account saved yet. Add one below, or run codex login on the box: the board saves the login it finds.' }));
  for (const a of cl) s.cxList.append(settingsCxRow(a));
  settingsAcctPatch();
  settingsAcctLoad();
  settingsAcctFocus(s);
}

/* THE rename sheet of an account: the Settings row and the Usage pencil both open it, so the title, the hint, the 60 characters, Save as the one primary and
   the behaviour are one. a: an account record (state.accounts.list, or the summary's: only key, label, name, email are read). The label is painted at once
   on every surface (this panel, the topbar chip and pills, the page behind: Usage rows, Limits chips, session rows) while the sheet stays open and PATCH
   /api/accounts/<key> {label} runs; success closes it, a refusal puts the old label back everywhere and says why in the sheet (and a toast), which stays open. */
function settingsRenameAccount(a, cfg) {
  if (!a || !a.key) return null;
  const cx = !!(cfg && cfg.codex);                                                         // a Codex account: its own endpoint, no name to fall back to, so the label is required
  const name = cx ? cxName(a) : agentsAcctName(a);
  const input = el('input', { type: 'text', maxlength: 60, autocomplete: 'off', autocapitalize: 'words', placeholder: cx ? 'account name' : (a.name || a.email || 'label') });
  input.value = a.label || '';
  const f = field(cx ? 'Name' : 'Label', input, cx ? 'Required. How this Codex account is listed here: Codex gives no email, so the name is the only way to tell accounts apart.'
    : 'Shown on the Usage page, the topbar chip and the session rows. Leave it empty to go back to the name Claude reports.');
  const live = () => (cx ? cxAccounts(state) : agentsAccounts(state)).find((q) => q.key === a.key) || a;                // the poll may have replaced the record since the sheet opened
  const put = (label) => { live().label = label; };
  const repaint = () => {
    if (settingsPage.refs) settingsFill('accounts', true);
    if (cx) return;
    if (typeof Shell !== 'undefined' && Shell.patchUsage) Shell.patchUsage(state);
    if (typeof updateCurrentPage === 'function') updateCurrentPage(state);
  };
  const saveBtn = el('button', { class: 'primary', type: 'submit', text: 'Save' });
  const save = async () => {
    const label = input.value.trim().replace(/\s+/g, ' ');
    if (label.length > 60) { fieldError(f, 'At most 60 characters.', true); return; }
    if (cx && !label) { fieldError(f, 'Give the account a name.', true); return; }
    fieldError(f, '');
    const before = live().label || null;
    if (label === (before || '')) { closeSheet(); return; }
    saveBtn.disabled = true;
    put(label || null);                                   // painted at once; the server's answer decides whether it stays
    repaint();
    try {
      await api('PATCH', `${cx ? '/api/codex-accounts' : '/api/accounts'}/${encodeURIComponent(a.key)}`, { label });
    } catch (e) {
      const why = cx ? acctReason(e) : (e && e.message ? e.message : 'the box refused it');
      put(before);
      repaint();
      saveBtn.disabled = false;
      fieldError(f, `Rename failed: ${why}`, true);       // the sheet stays open with the reason under the field ...
      pageToast(`Rename failed: ${why}`, 'bad');         // ... and a toast, because the sheet may have been dismissed meanwhile
      return;
    }
    closeSheet();
    pageToast(label ? `Renamed to ${label}` : `${name} is back to its own name`, 'ok');
    if (typeof poll === 'function') poll(true);
  };
  const form = el('form', { class: 'form', novalidate: true, onsubmit: (e) => { e.preventDefault(); save(); } },
    f,
    el('div', { class: 'submit' }, saveBtn, el('button', { type: 'button', onclick: () => closeSheet(), text: 'Cancel' })));
  const sheet = openSheet({ title: `Rename account · ${name}`, body: form });
  if (typeof focusFine === 'function') focusFine(input);
  return sheet;
}

/* How this window runs: an installed app (standalone, with the title-bar overlay on desktop Chrome / Edge) or a browser tab. */
function settingsAppMode() {
  const mq = (q) => !!(window.matchMedia && window.matchMedia(q).matches);
  if (mq('(display-mode: window-controls-overlay)')) return { id: 'standalone', note: 'installed app, the topbar is the title bar' };
  if (typeof isStandalone === 'function' && isStandalone()) return { id: 'standalone', note: 'installed app' };
  return { id: 'browser', note: 'running in a browser tab' };
}

function settingsInstallPrompt() { return typeof Shell !== 'undefined' && Shell.installPrompt ? Shell.installPrompt : null; }

/* The shortcut list is the keyboard layer's help dialog (keymap.js Keymap.openHelp(), drawn by palette.js Palette.openHelp()); the button shows only when one of them exists. */
function settingsHelpAvailable() {
  return (typeof Keymap !== 'undefined' && typeof Keymap.openHelp === 'function') || (typeof Palette !== 'undefined' && typeof Palette.openHelp === 'function');
}

function settingsOpenShortcuts() {
  if (typeof Keymap !== 'undefined' && typeof Keymap.openHelp === 'function' && Keymap.openHelp()) return;
  if (typeof Palette !== 'undefined' && typeof Palette.openHelp === 'function') Palette.openHelp();
}

function settingsApp(p) {
  p.textContent = '';
  const mode = settingsAppMode();
  p.append(settingsHead('Install'));
  p.append(settingsKv('Mode', el('span', { class: 'v', text: mode.id }), el('span', { class: 'dim', text: mode.note })));

  const installRow = settingsKv('Install');
  const prompt = settingsInstallPrompt();
  if (mode.id === 'standalone') installRow.add(el('span', { class: 'v', text: 'installed' }), el('span', { class: 'dim', text: 'open it from the Dock, Launchpad or the Home Screen' }));
  else if (prompt) installRow.add(el('span', { class: 'dim', text: 'its own window, a Dock or taskbar icon and the shortcuts below' }), el('button', { class: 'primary', type: 'button', onclick: async () => {
    const outcome = await Shell.promptInstall();
    if (outcome === 'accepted') pageToast('Installing ccboard…', 'ok');
  }, text: 'Install ccboard' }));
  else installRow.add(el('span', { class: 'dim', text: 'This browser offers no install button right now: use the steps below (Safari never has one; Chrome and Edge show it once the page has been used).' }));
  p.append(installRow);

  // the steps are reference text: closed until asked for, so the App tab is a short list and not a wall
  p.append(el('details', { class: 'dim set-steps' }, el('summary', { text: 'Install steps by browser' }),
    el('ul', {},
      el('li', {}, el('b', { text: 'Safari on a Mac' }), ': File › Add to Dock. The board then opens in its own window.'),
      el('li', {}, el('b', { text: 'Safari on iPad / iPhone' }), ': Share › Add to Home Screen. Web Push on iOS and iPadOS only works from the Home Screen icon (16.4 and later).'),
      el('li', {}, el('b', { text: 'Chrome or Edge on a laptop' }), ': the install icon at the right of the address bar, or the menu › Install ccboard (Edge: … › Apps › Install this site as an app). The topbar then doubles as the title bar.'),
      el('li', {}, el('b', { text: 'Chrome on Android' }), ': menu › Install app.'))));

  const swReady = !!(typeof navigator !== 'undefined' && navigator.serviceWorker && navigator.serviceWorker.controller);
  p.append(settingsHead('This build'));
  p.append(settingsKv('Version', el('span', { class: 'v', text: (state && state.version) || '-' }),
    el('span', { class: 'dim', text: 'build id; the board reloads itself when the box is updated' })));
  p.append(settingsKv('Offline shell', el('span', { class: swReady ? 'v' : 'v dim', text: swReady ? 'cached on this device' : 'not active yet (reload once)' })));
  p.append(settingsHead('Tools'));
  p.append(settingsKv('Reload', el('span', { class: 'dim', text: 'an installed app has no browser reload button' }),
    el('button', { type: 'button', onclick: () => location.reload(), text: 'Reload app' })));
  if (settingsHelpAvailable()) {
    p.append(settingsKv('Shortcuts', el('span', { class: 'dim', text: 'press ? on any page' }),
      el('button', { type: 'button', onclick: settingsOpenShortcuts, text: 'Keyboard shortcuts' })));
  }
}

/* core.js / shell.js call renderAppPanel() when the browser hands over (or withdraws) the install prompt: rebuild the panel if the page is open. */
function renderAppPanel() { settingsFill('app', true); }

const SETTINGS_BUILD = { notify: settingsNotify, nodes: settingsNodes, box: settingsBox, doctor: (p) => settingsDoctor(p), agents: settingsAgents, accounts: settingsAccounts, app: settingsApp };

/* What a panel shows, as a string: the panel is rebuilt only when it changes. */
function settingsSig(id, st) {
  const minute = Math.floor(Date.now() / 60000);                       // ages ("3h ago") move on, so the minute is part of the key
  if (id === 'notify') return JSON.stringify([st.config && st.config.ntfy, st.config && st.config.backup, st.backup, minute]);
  if (id === 'nodes') return JSON.stringify(st.nodes);
  if (id === 'box') return JSON.stringify([st.health, st.backup, st.node_name, st.user, minute]);
  if (id === 'accounts') {                                             // identity and labels, not the readings: those move with every statusline and would rebuild the Rename button under a finger (they refresh with the minute)
    const forget = /^((acct|cx)-forget:|cx-logout)/.test(String(ui.confirm || '')) ? ui.confirm : null;      // the two-tap Forget login repaints the row
    return JSON.stringify([st.accounts && st.accounts.current, agentsAccounts(st).map((a) => [a.key, a.label, a.name, a.email, a.plan, !!a.current, !!a.saved]), acctStore(st), acctFlow.busy, forget, minute,
      cxState(st) ? [st.codex_accounts.current, cxAccounts(st).map((a) => [a.key, a.label, a.plan, !!a.current, !!a.saved]), cxStore(st), cxFlow.busy] : null,
      (acctProblem(st) || {}).account || (acctProblem(st) ? '*' : null)]);                  // the amber chip and the lead action follow the problem at once
  }
  if (id === 'app') return JSON.stringify([st.version, settingsAppMode().note, !!settingsInstallPrompt(), settingsHelpAvailable()]);
  if (id === 'doctor') return JSON.stringify(doctorSig());
  return JSON.stringify([st.claude, st.agents, ui.confirm === 'logout', ui.confirm === 'cx-logout', cxFlow.err, doctorSig(), cxState(st) ? (cxAccounts(st).find((a) => a.current) || {}).key || null : null]);     // the two-tap Log out repaints the panel; the doctor's answer repaints the checks under each card
}

function settingsSecOf(r) {
  const sec = r && r.query && r.query.sec;
  return SETTINGS_SECTIONS.some((x) => x.id === sec) ? sec : 'notify';
}

function settingsFill(id, force) {
  const r = settingsPage.refs;
  if (!r || !state) return;
  const panel = r.panels[id];
  const sig = settingsSig(id, state);
  if (!force && panel.ccSig === sig) return;
  panel.ccSig = sig;
  SETTINGS_BUILD[id](panel);
}

function settingsShow(id) {
  const r = settingsPage.refs;
  if (!r) return;
  settingsPage.active = id;
  for (const sec of SETTINGS_SECTIONS) r.panels[sec.id].classList.toggle('hidden', sec.id !== id);
  r.tabs.set(id);
  settingsFill(id, false);
  if (id === 'accounts') settingsAcctPatch();                         // a Log in tapped on another page starts the add flow here
  if (typeof doctorLinePaint === 'function') doctorLinePaint();       // the 'Doctor: n checks failing' line is not shown on the Doctor tab itself
}

/* core.js calls renderNotifyPanel() after push was enabled or disabled: rebuild that panel if the page is open. */
function renderNotifyPanel() { settingsFill('notify', true); }
function renderNodes() { settingsFill('nodes', true); }

/* tabs() comes from components.js; without it the sections still switch through plain buttons. */
function settingsTabs(active, onChange) {
  if (typeof tabs === 'function') return tabs(SETTINGS_SECTIONS, active, onChange);
  const buttons = new Map();
  const root = el('div', { class: 'tabs', role: 'tablist' });
  const paint = (id) => { for (const [k, b] of buttons) b.setAttribute('aria-selected', k === id ? 'true' : 'false'); };
  for (const sec of SETTINGS_SECTIONS) {
    const b = el('button', { class: 'minimal', type: 'button', role: 'tab', text: sec.label, onclick: () => { paint(sec.id); onChange(sec.id); } });
    buttons.set(sec.id, b);
    root.append(b);
  }
  paint(active);
  return { root, set: paint };
}

registerPage('settings', {
  title: 'Settings',
  mount(root, route) {
    const active = settingsSecOf(route);
    settingsPage.focus = (route && route.query && route.query.acct) || null;                    // ?acct=<key>: the account row to scroll to (the Accounts panel takes it once)
    const tabCtl = settingsTabs(active, (id) => navigate(buildHash('settings', {}, { sec: id }), { replace: true }));
    const panels = {};
    const docLink = el('a', { href: buildHash('settings', {}, { sec: 'doctor' }), text: '' });                  // 'Doctor: 2 checks failing': a quiet line, there only while something fails
    const docLine = el('div', { class: 'settings-doc-line hidden', role: 'status' }, docLink);
    const wrap = el('div', { class: 'settings-page' }, el('div', { class: 'page-head' }, el('h1', { text: 'Settings' })), tabCtl.root, docLine);
    for (const sec of SETTINGS_SECTIONS) {
      panels[sec.id] = el('div', { class: 'section settings-panel hidden', role: 'tabpanel', 'data-sec': sec.id });
      wrap.append(panels[sec.id]);
    }
    root.append(wrap);
    if (settingsPage.add) settingsPage.add.dispose();
    settingsPage.add = null;                                          // the add block is built again with the Accounts panel of this mount
    if (settingsPage.cx) settingsPage.cx.dispose();
    settingsPage.cx = null;
    settingsPage.refs = { tabs: tabCtl, panels, docLine, docLink };
    settingsShow(active);
    if (typeof doctorLoad === 'function') doctorLoad(false);                                        // the checklist is read quietly: it feeds the line under the tabs and Agents
    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function' && typeof doctorOnVisible === 'function') {
      if (settingsPage.vis) document.removeEventListener('visibilitychange', settingsPage.vis);
      settingsPage.vis = () => doctorOnVisible();                                                    // back on the tab: ask again, so a fix made on the box shows
      document.addEventListener('visibilitychange', settingsPage.vis);
    }
  },
  update() { settingsFill(settingsPage.active, false); settingsAcctPatch(); },     // the add block follows the login state on every poll, not only when the rows change
  onRoute(route) {
    settingsPage.focus = (route && route.query && route.query.acct) || null;
    settingsShow(settingsSecOf(route));
    if (settingsPage.focus && settingsPage.active === 'accounts') settingsFill('accounts', true);           // already on the page: the rows are rebuilt so the focus finds its row
  },
  unmount() {
    if (settingsPage.add) settingsPage.add.dispose();                   // its one-second timer must not outlive the page
    settingsPage.add = null;
    if (settingsPage.cx) settingsPage.cx.dispose();
    settingsPage.cx = null;
    if (settingsPage.vis && typeof document !== 'undefined' && typeof document.removeEventListener === 'function') document.removeEventListener('visibilitychange', settingsPage.vis);
    settingsPage.vis = null;
    settingsPage.ext = null;
    settingsPage.extAsked = false;
    settingsPage.refs = null;
  },
});
