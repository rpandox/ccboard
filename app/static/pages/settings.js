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
function settingsHead(text) { return el('h2', { class: 'set-h', text }); }

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

  // #84: the board-wide state, read-only; the per-session switch is in each session's menu and Tune panel
  const acOff = boardAutoContinueOff(state);
  p.append(settingsHead('Auto-continue'));
  p.append(settingsKv('Board-wide').add(
    el('span', { class: 'v', text: acOff ? 'off' : 'on' }),
    el('code', { class: 'mono', text: 'CCBOARD_AUTO_CONTINUE' }),
    el('span', { class: 'dim', text: acOff
      ? 'Set it to 1 in the board settings file and restart the board to turn it on. A session switch cannot change it.'
      : 'Types continue once after a limit reset, and after a reboot into a session that was working. Set it to 0 and restart the board to turn it off everywhere. One session opts out from its own menu or Tune panel.' })));

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

/* Settings > Nodes > pairing (issue #135): the paired nodes and the activity (GET /api/nodes, /api/nodes/pairs, /api/nodes/audit), the two sheets and the three actions on a pair.

   Create pairing code (POST /api/nodes/pair-code {scopes, minutes}) shows the code once, large, with Copy and a countdown, and says that closing the sheet does not cancel the
   code (Cancel code does, DELETE /api/nodes/pair-code). Add node (POST /api/nodes {url, code, handle?, both_ways}) takes the address of the other board and the code typed or
   pasted from it. Rotate token (POST /api/nodes/<peer>/rotate) keeps the old token for 60 s on the other side and says so; Remove and Revoke (DELETE /api/nodes/<peer>) work even
   when the other node is offline and say when it was not told. Rotate token and Revoke are two-tap confirmButtons. Remove opens a sheet that first asks the board
   what else would stay able to call it (POST /api/nodes/<peer>/remove-preview): the pairs that use the same node id from another address, or were never
   confirmed, are listed with an unticked box each, and Remove sends the ones the person ticks as DELETE {also_revoke: [peer_id, ...]}. A pair the node made again from another address is listed under
   Who can control this node as "says it is this node too", still working, with its usual Revoke: a node id is not proof, so nothing points at one pair.

   A code is in this file only as the text of the open sheet (settingsNdSheet.code and the output node): it is never put in storage, a URL, a toast or a log, and closing the sheet
   empties every node and the variable. A token is never in this file at all: the board's answers to rotate and add carry none that this page reads. The one timer here is the
   countdown of an open sheet; it is cleared when the sheet closes, the code runs out, or the page goes away. */
const settingsNdPeers = { nodes: null, pairs: null, audit: null, err: '', at: 0, now: null, busy: false, seq: 0, act: {}, rotated: {} };
const settingsNdSheet = { shell: null, timer: null, tick: null, deadline: 0, code: '', opener: null, scopes: null, minutes: NodeView.CODE_DEFAULT_MINUTES };

/* This board's clock for ages in the pair lists: the answer's own `now` or `at` plus what has passed here since, else the browser's. */
function settingsNdNow() { return NodeView.boxNow(settingsNdPeers.now, settingsNdPeers.at, Date.now()); }

/* The pairs this board calls (direction 'out', and the legacy rows the registry imported) and the ones that call this board. A revoked pair is not listed. */
function settingsNdOut() {
  return NodeView.list(settingsNdPeers.nodes, 'nodes', 'peers', 'rows').filter((r) => r.direction !== 'in' && !r.revoked_at);
}
function settingsNdIn() {
  const P = settingsNdPeers;
  const seen = new Set();
  const out = [];
  for (const r of [...NodeView.list(P.pairs, 'pairs', 'rows', 'nodes'), ...NodeView.list(P.nodes, 'pairs'), ...NodeView.list(P.nodes, 'nodes', 'peers', 'rows').filter((x) => x.direction === 'in')]) {
    const k = NodeView.peer(r).key;
    if (r.revoked_at || !k || seen.has(k)) continue;
    seen.add(k);
    out.push(r);
  }
  return out;
}

/* The two-tap confirm armed on this pair, if any ('node-rot:<id>', 'node-rm:<id>', 'node-rv:<id>'): part of the row's signature, so arming repaints the row. */
function settingsNdArmed(id) {
  const c = String(ui.confirm || '');
  return c.startsWith('node-') && c.endsWith(`:${id}`) ? c : '';
}

function settingsNdToast(msg, kind) {
  if (typeof pageToast === 'function') pageToast(msg, kind || 'ok');
  else if (typeof toast === 'function') toast(msg, { kind: kind || 'ok' });
}

/* The three lists in one go. A failed list keeps the last good one on screen and the page says so; a newer ask makes an older answer stale. */
async function settingsNdLoadPeers() {
  const P = settingsNdPeers;
  const mine = ++P.seq;
  P.busy = true;
  const ask = async (path) => { try { return { v: await api('GET', path) }; } catch (e) { return { err: (e && e.message) || 'the board did not answer' }; } };
  const [n, p, a] = await Promise.all([ask('/api/nodes'), ask('/api/nodes/pairs'), ask('/api/nodes/audit?limit=50')]);
  if (mine !== P.seq) return false;
  P.busy = false;
  P.err = n.err || p.err || a.err || '';
  if (n.err === undefined) { P.nodes = n.v === undefined || n.v === null ? {} : n.v; P.at = Date.now(); P.now = n.v && typeof n.v === 'object' ? (n.v.now || n.v.at || null) : null; }
  if (p.err === undefined) P.pairs = p.v === undefined || p.v === null ? {} : p.v;
  if (a.err === undefined) P.audit = a.v === undefined || a.v === null ? {} : a.v;
  settingsNd.rev++;
  settingsFill('nodes', true);
  return true;
}

function settingsNdOpenPeers() {
  const P = settingsNdPeers;
  if (P.busy) return;
  if (P.nodes !== null && Date.now() - P.at < NodeView.STALE_S * 1000) return;
  settingsNdLoadPeers();
}

function settingsNdBusy(id, word) {
  const P = settingsNdPeers;
  if (word) P.act[id] = word; else delete P.act[id];
  settingsNd.rev++;
  settingsFill('nodes', true);
}

/* Rotate: the other node keeps the old token for 60 s and this board saves the new one; nothing about either token comes back to this page. */
async function settingsNdRotate(p) {
  if (settingsNdPeers.act[p.id]) return;
  settingsNdBusy(p.id, 'rotate');
  try {
    const r = await api('POST', `/api/nodes/${encodeURIComponent(p.id)}/rotate`);
    const grace = r && Number.isFinite(r.grace_s) && r.grace_s > 0 && r.grace_s <= 3600 ? Math.round(r.grace_s) : NodeView.ROTATE_GRACE_S;
    settingsNdPeers.rotated[p.id] = Date.now();
    settingsNdToast(`Token for ${p.name} rotated. The old one still works for ${grace} seconds.`);
  } catch (e) { settingsNdToast(`Token for ${p.name} not rotated: ${e.message}`, 'warn'); }
  settingsNdBusy(p.id, '');
  await settingsNdLoadPeers();
}

/* Revoke (a pair that calls this board) is DELETE /api/nodes/<peer>. The board never fails it because the other node is offline. */
async function settingsNdRemove(p, revoke) {
  if (settingsNdPeers.act[p.id]) return;
  settingsNdBusy(p.id, revoke ? 'revoke' : 'remove');
  try {
    await api('DELETE', `/api/nodes/${encodeURIComponent(p.id)}`);
    settingsNdToast(`${p.name} can no longer call this board.`);
  } catch (e) { settingsNdToast(`${p.name} not ${revoke ? 'revoked' : 'removed'}: ${e.message}`, 'warn'); }
  settingsNdBusy(p.id, '');
  await settingsNdLoadPeers();
}

/* The pairs of the preview as the sheet's rows: an unticked box each (a node id is only a claim, so revoking another pair is the person's choice, never the default), the name, the address and, for a pair
   nobody confirmed, the tag "not confirmed". */
function settingsNdRemoveRow(o, checks) {
  const c = el('input', { type: 'checkbox', 'data-pair': NodeView.str(o.peer_id, 80) });
  c.checked = false;
  checks.push(c);
  const name = NodeView.str(o.name, 80) || 'unnamed node';
  const host = NodeView.hostOf(NodeView.str(o.url, 300));
  return el('label', { class: 'set-check set-pref nd-rm-row' }, c, el('span', { class: 'set-pref-t' },
    el('b', { text: name }),
    el('span', { class: 'dim', text: host || 'no address' }),
    o.verified === false ? NodeView.chip('not confirmed', 'warn', '!', 'This pair never confirmed who it is. It may only be using this node\'s id.') : null));
}

/* Remove a node this board calls: the sheet asks the board which other pairs name the same node id (they are not revoked by the id alone), lists them unticked, and Remove sends
   the ticked ones. A failed ask does not block the removal: the sheet says so and Remove then revokes nothing else. */
function settingsNdRemoveSheet(opener, rec) {
  settingsNdSheetClose();
  const s = settingsNdSheet;
  s.opener = opener || null;
  const p = NodeView.peer(rec);
  let shell = null;
  shell = modalShell('qr-editor readout nd-sheet', `Remove ${p.name}`, () => settingsNdSheetGone(shell));
  s.shell = shell;
  const checks = [];
  const list = el('div', { class: 'nd-rm-list' });
  const info = el('p', { class: 'qr-hint dim', text: 'Checking which other nodes can still call this board…' });
  const err = el('p', { class: 'qr-err bad', role: 'alert' });
  const go = el('button', { class: 'danger confirm', type: 'button', text: 'Remove', disabled: true, onclick: () => submit() });
  const f = el('form', { class: 'nd-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); submit(); } }, info, list, err,
    el('div', { class: 'qr-actions' }, el('button', { type: 'button', onclick: () => shell.close(), text: 'Cancel' }), go));
  shell.dlg.append(el('div', { class: 'qr-box' }, el('h2', { class: 'qr-title', text: `Remove ${p.name}` }),
    el('p', { class: 'qr-hint dim', text: 'This board stops calling the node and tells it to unpair. That works even when the node is offline.' }), f));
  async function ask() {
    let pv = null;
    try { pv = await api('POST', `/api/nodes/${encodeURIComponent(p.id)}/remove-preview`); } catch (e) {
      if (shell !== s.shell) return;
      info.textContent = `Could not check which other pairs would stay (${e.message}). Removing still works; look at Who can control this node afterwards.`;
      go.disabled = false;
      return;
    }
    if (shell !== s.shell) return;
    const others = NodeView.list(pv, 'others');
    if (others.length) {
      info.textContent = 'These pairs use the same node id from another address, or never confirmed who they are, so they were not cut with it. None is ticked: a node id is not proof of who a board is. Tick any you want revoked too.';
      settingsNdPut(list, ...others.map((o) => settingsNdRemoveRow(o, checks)));
    } else info.textContent = 'No other pair uses this node\'s id.';
    go.disabled = false;
  }
  async function submit() {
    if (go.disabled) return;
    const ticked = checks.filter((c) => c.checked).map((c) => c.getAttribute('data-pair')).filter(Boolean);
    go.disabled = true;
    go.textContent = 'Removing…';
    err.textContent = '';
    settingsNdBusy(p.id, 'remove');
    let r = null;
    let fail = null;
    try { r = ticked.length ? await api('DELETE', `/api/nodes/${encodeURIComponent(p.id)}`, { also_revoke: ticked }) : await api('DELETE', `/api/nodes/${encodeURIComponent(p.id)}`); } catch (e) { fail = e; }
    settingsNdBusy(p.id, '');
    if (fail) {
      go.disabled = false;
      go.textContent = 'Remove';
      err.textContent = `${p.name} not removed: ${fail.message}`;
      settingsNdLoadPeers();
      return;
    }
    const told = r && typeof r === 'object' ? (r.peer_notified !== undefined ? r.peer_notified : r.notified) : undefined;
    const cut = r && Array.isArray(r.also_revoked) ? r.also_revoked.length : 0;
    const left = r && Array.isArray(r.other_pairs) ? r.other_pairs.length : 0;
    const said = [told === false ? `${p.name} removed. It was not told (offline or no answer), so remove this board there too.` : `${p.name} removed.`,
      cut ? `${cut} other ${cut === 1 ? 'pair' : 'pairs'} revoked.` : '',
      left ? `${left} other ${left === 1 ? 'pair' : 'pairs'} with its node id can still call this board.` : ''].filter(Boolean).join(' ');
    shell.close();
    settingsNdToast(said, told === false || left ? 'warn' : 'ok');
    settingsNdLoadPeers();
  }
  shell.show();
  ask();
  return shell;
}

/* The actions of a pair row: Rotate token and Remove (a pair this board calls), Revoke (one that calls it), or just Pair for a legacy row. */
function settingsNdPeerActions(rec, incoming) {
  const p = NodeView.peer(rec);
  const busy = settingsNdPeers.act[p.id];
  const wait = (word) => el('button', { class: 'danger', type: 'button', disabled: true, text: word });
  if (incoming) {
    if (busy) return [wait('Revoking…')];
    const rv = confirmButton(`node-rv:${p.id}`, 'Revoke', () => settingsNdRemove(p, true), false);
    return [rv];
  }
  if (p.legacy) {
    const pair = el('button', { class: 'primary tinted', type: 'button', 'aria-label': `Pair ${p.name}`, text: 'Pair' });
    pair.addEventListener('click', () => settingsNdAddSheet(pair, rec.url));
    return [pair];
  }
  const rm = el('button', { class: 'danger', type: 'button', title: 'Remove (asks what else would stay)', 'aria-label': `Remove ${p.name}`, text: 'Remove' });
  rm.addEventListener('click', () => settingsNdRemoveSheet(rm, rec));
  return [busy === 'rotate' ? wait('Rotating…') : confirmButton(`node-rot:${p.id}`, 'Rotate token', () => settingsNdRotate(p), false),
    busy === 'remove' ? wait('Removing…') : rm];
}

/* Empty everything the sheet holds, stop its timer, and give the focus back to what opened it (or to Add node when that row was repainted away). */
function settingsNdSheetGone(shell) {
  const s = settingsNdSheet;
  if (s.timer) { clearInterval(s.timer); s.timer = null; }
  s.code = '';
  s.tick = null;
  s.deadline = 0;
  shell.dlg.textContent = '';
  if (s.shell === shell) s.shell = null;
  const o = s.opener;
  s.opener = null;
  const back = o && o.isConnected !== false ? o : (settingsNd.ui && settingsNd.ui.addBtn) || null;
  if (back && typeof back.focus === 'function') back.focus();
}

/* append() with the empty slots left out (a bare null would be written into the page as the word "null"). */
function settingsNdPut(host, ...kids) { host.append(...kids.filter((k) => k !== null && k !== undefined && k !== false)); }

function settingsNdSheetClose() { if (settingsNdSheet.shell) settingsNdSheet.shell.close(); }

function settingsNdScopeRows(checks, chosen) {
  return NodeView.SCOPES.map((sc) => {
    const c = el('input', { type: 'checkbox', 'data-scope': sc.id });
    c.checked = chosen ? chosen.includes(sc.id) : sc.on;
    checks[sc.id] = c;
    return el('label', { class: 'set-check set-pref' }, c, el('span', { class: 'set-pref-t' }, el('b', { text: sc.label }), el('span', { class: 'dim', text: sc.what })));
  });
}

/* Create pairing code: the form (what the other node may do, how long the code works), then the code. */
function settingsNdCodeSheet(opener) {
  settingsNdSheetClose();
  const s = settingsNdSheet;
  s.opener = opener || null;
  let shell = null;
  shell = modalShell('qr-editor readout nd-sheet', 'Create pairing code', () => settingsNdSheetGone(shell));
  s.shell = shell;
  const box = el('div', { class: 'qr-box' });
  shell.dlg.append(box);
  const stop = () => { if (s.timer) { clearInterval(s.timer); s.timer = null; } };
  const title = () => el('h2', { class: 'qr-title', text: 'Create pairing code' });

  function form(note) {
    stop();
    s.code = '';
    box.textContent = '';
    const checks = {};
    const scopeField = field('What the other node may do here', el('div', { class: 'nd-scopes' }, settingsNdScopeRows(checks, s.scopes)));
    const mins = selectEl(NodeView.CODE_MINUTES.map((m) => [String(m), m === 1 ? '1 minute' : `${m} minutes`]), String(s.minutes));
    const minField = field('The code works for', mins);
    const err = el('p', { class: 'qr-err bad', role: 'alert' });
    const create = el('button', { class: 'primary', type: 'submit', text: 'Create code' });
    const f = el('form', { class: 'nd-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); go(); } }, scopeField, minField, err,
      el('div', { class: 'qr-actions' }, el('button', { type: 'button', onclick: () => shell.close(), text: 'Cancel' }), create));
    settingsNdPut(box, title(), el('p', { class: 'qr-hint dim', text: 'Make a one-time code, then type it on the other board under Settings, Nodes, Add node. Nothing is allowed until it is used, and you choose what the other node may do.' }),
      note ? el('p', { class: 'qr-hint warn', role: 'status', text: note }) : null, f);
    async function go() {
      if (create.disabled) return;
      const scopes = NodeView.SCOPES.map((x) => x.id).filter((id) => checks[id].checked);
      s.scopes = scopes;
      s.minutes = parseInt(mins.value, 10) || NodeView.CODE_DEFAULT_MINUTES;
      if (!scopes.length) { fieldError(scopeField, 'Pick at least one thing the other node may do.'); return; }
      fieldError(scopeField, '');
      err.textContent = '';
      create.disabled = true;
      create.textContent = 'Creating…';
      const sent = Date.now();
      let r = null;
      try { r = await api('POST', '/api/nodes/pair-code', { scopes, minutes: s.minutes }); } catch (e) { err.textContent = `No code was made: ${e.message}`; }
      const code = r && typeof r === 'object' && typeof r.code === 'string' ? NodeView.formatCode(r.code) : '';
      if (s.shell !== shell) {                                          // closed while the board was making it: nobody will see this code, so do not leave it live
        if (code) { try { await api('DELETE', '/api/nodes/pair-code'); } catch (_) { /* it ends by itself */ } }
        return;
      }
      create.disabled = false;
      create.textContent = 'Create code';
      if (!r) return;
      if (!code) { err.textContent = 'The board answered without a code.'; return; }
      const granted = r && Array.isArray(r.scopes) ? NodeView.scopeList(r.scopes) : scopes;
      show(code, sent + s.minutes * 60000, granted);
      settingsNdLoadPeers();
    }
  }

  function show(code, deadline, granted) {
    stop();
    s.code = code;
    s.deadline = deadline;
    box.textContent = '';
    const out = el('output', { class: 'nd-code', 'aria-label': 'Pairing code', text: code });
    const count = el('div', { class: 'dim nd-count', role: 'timer' });
    const err = el('p', { class: 'qr-err bad', role: 'alert' });
    const cancel = el('button', { type: 'button', text: 'Cancel code', onclick: cancelCode });
    const url = settingsNdSelfUrl();
    settingsNdPut(box, title(),
      el('p', { class: 'qr-hint dim', text: 'On the other board open Settings, Nodes, Add node, and type this code with the address of this board. It works once.' }),
      el('div', { class: 'nd-code-row' }, out, copyButton(code, 'pairing code')),
      el('div', { class: 'dim nd-granted', text: `It lets that node use: ${granted.length ? granted.join(', ') : 'nothing'}.` }),
      url ? el('div', { class: 'nd-addr' }, el('span', { class: 'dim', text: 'This board' }), el('code', { text: url }), copyButton(url, 'address')) : null,
      count, el('p', { class: 'qr-hint dim', text: 'Closing this sheet does not cancel the code. Cancel code does.' }), err,
      el('div', { class: 'qr-actions' }, cancel, el('button', { class: 'primary', type: 'button', onclick: () => shell.close(), text: 'Done' })));
    const tick = () => {
      const left = s.deadline - Date.now();
      if (left <= 0) { expired(); return; }
      count.textContent = `Ends in ${NodeView.countdown(left)}`;
    };
    async function cancelCode() {
      cancel.disabled = true;
      err.textContent = '';
      try { await api('DELETE', '/api/nodes/pair-code'); } catch (e) {
        err.textContent = `The code was not cancelled: ${e.message}. It still works until it ends.`;
        cancel.disabled = false;
        return;
      }
      settingsNdToast('Pairing code cancelled.');
      shell.close();
      settingsNdLoadPeers();
    }
    s.tick = tick;                                                    // the interval calls it; a test calls it to move the clock on
    tick();
    if (s.code) {
      s.timer = setInterval(tick, 1000);
      if (s.timer && typeof s.timer.unref === 'function') s.timer.unref();
    }
  }

  function expired() {
    stop();
    s.code = '';
    box.textContent = '';
    settingsNdPut(box, title(), el('p', { class: 'qr-hint warn', role: 'status', text: 'This code has run out. It can no longer pair anything.' }),
      el('div', { class: 'qr-actions' }, el('button', { type: 'button', onclick: () => shell.close(), text: 'Done' }), el('button', { class: 'primary', type: 'button', onclick: () => form(''), text: 'Create a new code' })));
    settingsNdLoadPeers();
  }

  form('');
  shell.show();
  return shell;
}

/* This board's own address for the other board to type, when the node card has one. */
function settingsNdSelfUrl() {
  const n = typeof state !== 'undefined' && state && state.node && typeof state.node === 'object' ? state.node : null;
  return n && typeof n.url === 'string' && NodeView.address(n.url).url ? NodeView.address(n.url).url : '';
}

/* Add node: the address of the other board and the code it shows. A refusal shows beside the field it belongs to and keeps everything typed. */
function settingsNdAddSheet(opener, prefill) {
  settingsNdSheetClose();
  const s = settingsNdSheet;
  s.opener = opener || null;
  let shell = null;
  shell = modalShell('qr-editor readout nd-sheet', 'Add node', () => settingsNdSheetGone(shell));
  s.shell = shell;
  const url = el('input', { type: 'text', class: 'nd-addr-in', inputmode: 'url', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', placeholder: 'https://box.example.ts.net' });
  const code = el('input', { type: 'text', class: 'nd-code-in', autocomplete: 'off', autocapitalize: 'characters', spellcheck: 'false', maxlength: '24', placeholder: 'XXXXX-XXXXX' });
  const handle = el('input', { type: 'text', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', maxlength: '31', placeholder: 'box' });
  const both = el('input', { type: 'checkbox' });
  if (typeof prefill === 'string' && prefill) url.value = prefill;
  const addrF = field('Address of the other board', url, 'The https address the other board answers on over the tailnet.');
  const codeF = field('Pairing code', code, 'The code the other board shows under Create pairing code. Paste it or type it.');
  const handleF = field('Short name (optional)', handle, 'Used in addresses, like box. Letters, numbers and dashes. Leave it empty to use the node\'s own name.');
  const bothRow = el('label', { class: 'set-check set-pref' }, both, el('span', { class: 'set-pref-t' }, el('b', { text: 'Also let that node control this one' }),
    el('span', { class: 'dim', text: 'It gets Read and Tasks on this board. Leave it off to control the other node from here only.' })));
  const err = el('p', { class: 'qr-err bad', role: 'alert' });
  const pair = el('button', { class: 'primary', type: 'submit', text: 'Pair' });
  code.addEventListener('change', () => { const c = NodeView.code(code.value); if (c.code) code.value = c.code; });
  const fields = { address: addrF, code: codeF, handle: handleF };
  const form = el('form', { class: 'nd-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); go(); } }, addrF, codeF, handleF, bothRow, err,
    el('div', { class: 'qr-actions' }, el('button', { type: 'button', onclick: () => shell.close(), text: 'Cancel' }), pair));
  shell.dlg.append(el('div', { class: 'qr-box' }, el('h2', { class: 'qr-title', text: 'Add node' }),
    el('p', { class: 'qr-hint dim', text: 'Pair this board with another one. On the other board open Settings, Nodes and press Create pairing code, then type its address and code here.' }), form));
  async function go() {
    if (pair.disabled) return;
    const a = NodeView.address(url.value);
    const c = NodeView.code(code.value);
    const h = NodeView.handle(handle.value);
    fieldError(addrF, a.err);
    fieldError(codeF, c.err);
    fieldError(handleF, h.err);
    err.textContent = '';
    if (a.err || c.err || h.err) { const bad = a.err ? addrF : c.err ? codeF : handleF; fieldError(bad, a.err || c.err || h.err, true); return; }
    pair.disabled = true;
    pair.textContent = 'Pairing…';
    let r = null;
    let fail = null;
    try { r = await api('POST', '/api/nodes', { url: a.url, code: c.code, ...(h.handle ? { handle: h.handle } : {}), both_ways: !!both.checked }); } catch (e) { fail = e; }
    pair.disabled = false;
    pair.textContent = 'Pair';
    if (fail) {
      const msg = fail.status === 429 ? 'Too many tries. Wait a minute, then try again.' : (fail.message || 'the board refused');
      const which = NodeView.errField(fail.status, msg, fail.body && fail.body.reason);
      if (which) fieldError(fields[which], msg, true); else err.textContent = `Not paired: ${msg}`;
      settingsNdLoadPeers();
      return;
    }
    const row = r && typeof r === 'object' ? r : {};
    const name = NodeView.str(row.name || row.handle, 60) || NodeView.hostOf(a.url);
    code.value = '';
    shell.close();
    settingsNdToast(row.callback_unverified === true ? `Paired with ${name}, but its own address did not confirm who it is.` : `Paired with ${name}.`, row.callback_unverified === true ? 'warn' : 'ok');
    settingsNdLoadPeers();
  }
  shell.show();
  if (typeof coarsePointer !== 'function' || !coarsePointer()) (prefill ? code : url).focus();
  return shell;
}

/* Settings > Nodes (issue #134): the sections. This node (the card of state.node), Paired nodes (the pairs of #135 and, until they are paired, the CCBOARD_NODES rows), Found on
   your tailnet (GET /api/nodes/discover), Who can control this node and Activity (both #135, above). The frame is built once per visit and the sections are patched in place, so a poll or an answer never recreates the
   Refresh button under a finger: it keeps its focus, and a found row is repainted only when what it shows changed (keyed by the Tailscale id). The tailnet is asked about
   only when a person opens this tab (a plain GET: the board answers from its last look) or presses Refresh (?refresh=1: the board looks again). No timer, no poll: the
   page script never asks while this tab is closed or the page is hidden, and a board with no tailnet answers once with a plain reason. Everything the board's answer
   says about a device is shown through textContent (NodeView in nodes.js) and nothing in it is ever opened or followed. */
const settingsNd = { data: null, at: 0, err: '', busy: false, seq: 0, rev: 0, ui: null };

/* The short lines under a Mac or WSL2 node's platform (issue #154); the same words as Nodes.platNotes in nodes-hub.js, which a single board's Settings page does not load. */
function settingsNdPlatNotes(card, name) {
  const mac = name === 'Mac';
  return [mac && card.accounts && card.accounts.supported === false ? 'Saved logins are not available on a Mac. Normal login still works.' : '',
    mac ? 'A sleeping Mac shows as stale.' : name ? 'Load describes the distro and its VM, not Windows.' : ''].filter(Boolean);
}

/* A device's agents as [{id, name, version, ok}] from the full card (a list) or from this board's state.agents (a map); only installed ones. */
function settingsNdAgents(st) {
  const card = st.node && Array.isArray(st.node.agents) ? st.node.agents.map((a) => ({ id: a.id, version: a.version, ok: !!a.logged_in && !a.login_problem, installed: !!a.installed })) : null;
  const rows = card || Object.entries((st.agents && typeof st.agents === 'object') ? st.agents : {}).map(([id, a]) => ({ id, version: a && a.version, ok: !!(a && a.loggedIn), installed: !!(a && a.installed) }));
  return rows.filter((a) => a.installed && typeof a.id === 'string').map((a) => ({ ...a, name: a.id.charAt(0).toUpperCase() + a.id.slice(1) }));
}

/* This node: name, short node id (the full one to copy), operating system, agents and lanes. All from state.node, which every poll carries. */
function settingsNdPaintThis(host, st) {
  host.textContent = '';
  const n = st.node && typeof st.node === 'object' ? st.node : null;
  if (!n) { host.append(el('div', { class: 'dim', text: 'This board has not sent its node card yet.' })); return; }
  host.append(settingsKv('Name', el('span', { class: 'v', text: String(n.name || st.node_name || 'this node') })));
  if (n.node_id) host.append(settingsKv('Node id', el('span', { class: 'v', title: String(n.node_id), text: NodeView.shortId(String(n.node_id)) }), typeof copyButton === 'function' ? copyButton(String(n.node_id), 'node id') : null));
  const os = n.os && typeof n.os === 'object' ? n.os : {};
  const osText = [NodeView.osName(os.tailscale_os || os.system), os.release].filter(Boolean).join(' ');
  if (osText) host.append(settingsKv('System', el('span', { class: 'v', text: osText })));
  const store = st.accounts && st.accounts.store;                    // state.node holds no accounts; the board's own saved-login answer stands in for it
  const full = (n.accounts || !store) ? n : { ...n, accounts: { supported: !!store.supported } };
  const plat = NodeView.platform(n);
  if (plat.name) host.append(settingsKv('Platform', NodeView.chip(plat.name, '', '', 'Platform'), el('span', { class: 'v', text: `load ${plat.load}` }), ...settingsNdPlatNotes(full, plat.name).map((t) => el('span', { class: 'dim', text: t }))));
  const agents = settingsNdAgents(st);
  host.append(settingsKv('Agents', agents.length
    ? el('span', { class: 'set-chips' }, agents.map((a) => NodeView.chip(`${typeof AGENT_GLYPH !== 'undefined' && ownKey(AGENT_GLYPH, a.id) ? AGENT_GLYPH[a.id] + ' ' : ''}${a.name}${a.version ? ' ' + a.version : ''}${a.ok ? '' : ', not logged in'}`, a.ok ? '' : 'warn')))
    : el('span', { class: 'v dim', text: 'none installed' })));
  const l = n.lanes && typeof n.lanes === 'object' ? n.lanes : null;
  if (l && Number.isFinite(l.cap) && Number.isFinite(l.free)) host.append(settingsKv('Free lanes', el('span', { class: 'v', text: `${l.free} of ${l.cap}` }), el('span', { class: 'dim', text: 'A lane is a task slot: tasks sent here wait when none is free.' })));
}

/* Keep a list of keyed rows up to date without recreating the ones that did not change (a button under a finger keeps its node): `sigOf` says what a row shows. */
function settingsNdPatch(host, items, keyOf, sigOf, build) {
  const old = new Map([...host.children].map((n) => [n.getAttribute('data-key'), n]));
  const seen = new Set();
  let i = 0;
  for (const it of items) {
    const key = keyOf(it);
    if (seen.has(key)) continue;
    seen.add(key);
    const sig = sigOf(it);
    let node = old.get(key);
    if (!node || node.ccSig !== sig) {
      const fresh = build(it);
      fresh.ccSig = sig;
      fresh.setAttribute('data-key', key);
      if (node) { host.insertBefore(fresh, node); node.remove(); }
      node = fresh;
    }
    old.delete(key);
    if (host.children[i] !== node) host.insertBefore(node, host.children[i] || null);
    i++;
  }
  for (const gone of old.values()) gone.remove();
}

/* An empty-list sentence: present only while the list is empty (not just hidden), so it is never read out or found beside rows. */
function settingsNdEmpty(node, show, words) {
  node.classList.toggle('hidden', !show);
  node.textContent = show ? words : '';
}

/* The CCBOARD_NODES rows. The board keeps them as read only `legacy` rows of the registry too, but the state's own row (state.nodes.value) is the one that carries the health
   line and the https-only Open link, so it is the one shown for an address; the registry's legacy row is shown only when the state has none for that address. Once the same address
   is really paired both legacy rows go: the pair replaces them. */
function settingsNdPairedOut() { return settingsNdOut().filter((r) => r.legacy !== true); }
function settingsNdLegacyRows(st) {
  const list = (st.nodes && Array.isArray(st.nodes.value)) ? st.nodes.value : [];
  const paired = settingsNdPairedOut();
  return list.filter((x) => x && typeof x === 'object' && !NodeView.isPaired({ url: x.url, node_id: x.node_id }, paired));
}
function settingsNdRegistryLegacy(st, shown) {
  const paired = settingsNdPairedOut();
  return settingsNdOut().filter((r) => r.legacy === true && !NodeView.isPaired({ url: r.url, node_id: r.node_id }, paired) && !shown.some((x) => NodeView.isPaired({ url: x.url, node_id: x.node_id }, [r])));
}

function settingsNdLegacy(x) {
  const h = x.health || {};
  const txt = x.online
    ? `${x.sessions} sess · ${x.attention} need you${typeof h.cpu_pct === 'number' ? ' · cpu ' + h.cpu_pct + '%' : ''}${h.mem ? ' · ram ' + h.mem.pct + '%' : ''}${h.disk ? ' · disk ' + h.disk.pct + '%' : ''}${x.usage && x.usage.five_hour ? ' · 5h ' + Math.round(x.usage.five_hour.used_percentage) + '%' : ''}`
    : 'offline' + (x.error ? ' · ' + String(x.error).slice(0, 60) : '');
  const safe = /^https:\/\/[A-Za-z0-9.-]+(:\d+)?$/.test(x.url || '');
  const pair = el('button', { class: 'primary tinted', type: 'button', 'aria-label': `Pair ${x.name || 'this node'}`, text: 'Pair' });
  pair.addEventListener('click', () => settingsNdAddSheet(pair, safe ? x.url : ''));
  return settingsKv(x.name, el('span', { class: x.online ? (x.attention ? 'v warn' : 'v') : 'v bad', text: txt }), el('span', { class: 'dim', text: 'read only, pair to enable actions' }),
    safe ? el('a', { class: 'btn small', href: x.url + '/', target: '_blank', rel: 'noopener', title: x.url, text: 'Open' }) : null, pair);
}

function settingsNdPaintPaired(nu, st) {
  const P = settingsNdPeers;
  const now = settingsNdNow();
  const minute = Math.floor(Date.now() / 60000);
  const kv = settingsNdLegacyRows(st);
  const items = [...settingsNdPairedOut().map((rec) => ({ rec })), ...kv.map((x) => ({ legacy: x })), ...settingsNdRegistryLegacy(st, kv).map((rec) => ({ rec }))];
  nu.pairedStatus.textContent = P.err ? `Could not read the paired list: ${P.err}. ${P.nodes !== null ? 'The list below is the last answer.' : ''}` : '';
  nu.pairedStatus.classList.toggle('hidden', !P.err);
  settingsNdPatch(nu.pairedList, items, (it) => (it.legacy ? `legacy:${it.legacy.url || it.legacy.name}` : NodeView.peer(it.rec).key),
    (it) => {
      if (it.legacy) return JSON.stringify([it.legacy, minute]);
      const id = NodeView.peer(it.rec).id;
      const rot = P.rotated[id] && Date.now() - P.rotated[id] < (NodeView.ROTATE_GRACE_S + 15) * 1000 ? Math.floor(Date.now() / 15000) : 0;
      return JSON.stringify([it.rec, minute, P.act[id] || '', settingsNdArmed(id), rot, Nodes.ready ? Nodes.M.rev : 0]);
    },
    (it) => {
      if (it.legacy) return settingsNdLegacy(it.legacy);
      const row = NodeView.peerRow(it.rec, { nowMs: now, actions: settingsNdPeerActions(it.rec, false), rotatedMs: P.rotated[NodeView.peer(it.rec).id] || 0 });
      const line = Nodes.ready && Nodes.enabled() ? Nodes.settingsLine(NodeView.peer(it.rec).handle) : null;      // the hub's reading of this node (status and age, clock skew, agents, accounts); nodes-hub.js
      if (line) row.querySelector('.nd-main').append(line);
      return row;
    });
  settingsNdEmpty(nu.pairedEmpty, !items.length && !(P.nodes === null && !P.err), 'No node is paired. Press Add node and type the address and code the other board shows, or press Pair on a device found below.');
}

function settingsNdPaintIncoming(nu) {
  const P = settingsNdPeers;
  const now = settingsNdNow();
  const minute = Math.floor(Date.now() / 60000);
  const items = settingsNdIn();
  settingsNdPatch(nu.inList, items, (rec) => NodeView.peer(rec).key, (rec) => JSON.stringify([rec, minute, P.act[NodeView.peer(rec).id] || '', settingsNdArmed(NodeView.peer(rec).id)]),
    (rec) => NodeView.peerRow(rec, { nowMs: now, incoming: true, actions: settingsNdPeerActions(rec, true) }));
  settingsNdEmpty(nu.inEmpty, !items.length && !(P.pairs === null && P.nodes === null), 'No node can control this board. Press Create pairing code, then type the code on the other board.');
}

function settingsNdPaintAudit(nu) {
  const P = settingsNdPeers;
  const now = settingsNdNow();
  const minute = Math.floor(Date.now() / 60000);
  const items = NodeView.list(P.audit, 'rows', 'audit', 'events').slice(0, 25).map((r, i) => NodeView.audit(r, i));
  settingsNdPatch(nu.auditList, items, (a) => a.key, (a) => JSON.stringify([a, minute]), (a) => NodeView.auditRow(a, now));
  settingsNdEmpty(nu.auditEmpty, !items.length && P.audit !== null, 'Nothing yet. Making a code, pairing, rotating and removing show here.');
}

/* The line under a Tailscale that cannot be read, chosen from the words of the board's own reason (app/nodes_discovery.py read_tailscale; the reason already names the
   fix, this line adds the next step and, for a command line login, the command to copy). The Mac app's reasons say "open the Tailscale app": no command is offered for
   those. A reason this does not know gets the plain line. */
function settingsNdFix(reason) {
  const line = (t) => el('div', { class: 'dim nd-fix', text: t });
  if (/Windows side/i.test(reason)) return line('Press Add node, then type the other board\'s address and its pairing code. Finding devices here needs Tailscale inside this Linux distro.');
  if (/Tailscale app/i.test(reason)) return line('Do that on this device, then press Refresh.');
  if (/`tailscale up`|log(ged)?[ -]?(in|out)\b|login/i.test(reason)) {
    return el('div', { class: 'nd-fix' }, el('span', { text: 'Run ' }), el('code', { class: 'doc-cmd', text: 'tailscale up' }), typeof copyButton === 'function' ? copyButton('tailscale up', 'the command') : null, el('span', { text: ' on this device, then press Refresh.' }));
  }
  if (/MagicDNS/i.test(reason)) return line('Turn on MagicDNS in the Tailscale admin console (DNS page), then press Refresh.');
  if (/not running|cannot be reached/i.test(reason)) return line('Start Tailscale on this device, then press Refresh.');
  if (/install|not found/i.test(reason)) return line('Install Tailscale on this device and sign in, then press Refresh.');
  return line('Check Tailscale on this device, then press Refresh.');
}

function settingsNdPaintFound(ui, st) {
  const d = settingsNd.data;
  const now = Date.now();
  const boxMs = NodeView.boxNow(d && d.at, settingsNd.at, now);          // ages are measured on the box's clock, so a browser clock that is off does not make a fresh row stale
  const busy = settingsNd.busy;
  ui.refresh.textContent = busy ? 'Refreshing' : 'Refresh';
  ui.refresh.setAttribute('aria-busy', busy ? 'true' : 'false');
  ui.refresh.classList.toggle('busy', busy);
  const at = d ? NodeView.epoch(d.at) : null;
  ui.status.textContent = busy ? 'Looking at the tailnet.' : settingsNd.err ? `Could not ask the board: ${settingsNd.err}. ${d ? 'The list below is its last answer.' : 'Press Refresh to try again.'}` : at !== null ? `Looked ${NodeView.span(at, boxMs)} ago.` : '';
  ui.status.classList.toggle('bad', !!settingsNd.err && !busy);
  const ts = d && d.tailscale && typeof d.tailscale === 'object' ? d.tailscale : null;
  const down = !!ts && ts.ok === false;
  ui.problem.textContent = '';
  ui.problem.classList.toggle('hidden', !down);
  if (down) ui.problem.append(el('div', { class: 'set-err', role: 'alert', text: String(ts.reason || 'Tailscale is not available on this device').slice(0, 300) }), settingsNdFix(String(ts.reason || '')));
  const paired = [...((st.nodes && Array.isArray(st.nodes.value)) ? st.nodes.value : []), ...settingsNdOut()];          // the CCBOARD_NODES rows and the pairs of the registry: a found device is listed once
  const seen = new Set();
  const rows = [];
  for (const r of (d && !down && Array.isArray(d.rows)) ? d.rows : []) {
    if (!r || typeof r !== 'object' || r.ts_id === undefined || r.ts_id === null || seen.has(String(r.ts_id))) continue;
    seen.add(String(r.ts_id));
    if (!NodeView.isPaired(r, paired)) rows.push(r);
  }
  ui.empty.classList.toggle('hidden', !!rows.length || down || !d);
  ui.empty.textContent = d && d.rows && d.rows.length && !rows.length ? 'Every device found here is already paired.'
    : 'No other devices of yours were found on the tailnet. Install ccboard on another device, or tag it for ccboard in the tailnet, then press Refresh.';
  const bucket = Math.floor(now / 15000);
  const old = new Map([...ui.list.children].map((n) => [n.getAttribute('data-key'), n]));
  rows.forEach((r, i) => {
    const key = String(r.ts_id);
    const sig = NodeView.sig(r, bucket);
    let node = old.get(key);
    if (!node || node.ccSig !== sig) {
      let pair = null;
      if (NodeView.state(r) === 'found') {                                // Pair opens the Add node sheet with the address that answered; the code is typed there
        pair = el('button', { class: 'primary tinted', type: 'button', 'aria-label': `Pair ${r.name || 'this device'}`, text: 'Pair' });
        const btn = pair;
        btn.addEventListener('click', () => settingsNdAddSheet(btn, typeof r.url === 'string' ? r.url : ''));
      }
      const fresh = NodeView.row(r, { nowMs: boxMs, action: pair });
      fresh.ccSig = sig;
      if (node) { ui.list.insertBefore(fresh, node); node.remove(); }
      node = fresh;
    }
    old.delete(key);
    if (ui.list.children[i] !== node) ui.list.insertBefore(node, ui.list.children[i] || null);
  });
  for (const gone of old.values()) gone.remove();
}

function settingsNdUi() {
  if (settingsNd.ui) return settingsNd.ui;
  const ui = {
    thisHost: el('div', { class: 'nd-sec' }),
    pairedStatus: el('div', { class: 'dim nd-pstatus bad hidden', role: 'status' }),
    pairedList: el('div', { class: 'nd-list' }),
    pairedEmpty: el('div', { class: 'dim nd-pempty hidden' }),
    status: el('div', { class: 'dim nd-status', role: 'status', 'aria-live': 'polite' }),
    problem: el('div', { class: 'nd-problem hidden' }),
    empty: el('div', { class: 'dim nd-empty hidden' }),
    list: el('div', { class: 'nd-list' }),
    inList: el('div', { class: 'nd-list' }),
    inEmpty: el('div', { class: 'dim nd-pempty hidden' }),
    auditList: el('div', { class: 'nd-list' }),
    auditEmpty: el('div', { class: 'dim nd-pempty hidden' }),
  };
  ui.refresh = el('button', { class: 'minimal', type: 'button', title: 'Look at the tailnet again now', 'aria-busy': 'false', text: 'Refresh', onclick: () => settingsNdLoad(true) });
  ui.addBtn = el('button', { type: 'button', title: 'Pair with another board using its code', text: 'Add node' });
  ui.addBtn.addEventListener('click', () => settingsNdAddSheet(ui.addBtn, ''));
  ui.codeBtn = el('button', { type: 'button', title: 'Make a one-time code to type on another board', text: 'Create pairing code' });
  ui.codeBtn.addEventListener('click', () => settingsNdCodeSheet(ui.codeBtn));
  ui.reload = el('button', { class: 'minimal', type: 'button', title: 'Read the paired nodes and the activity again', text: 'Reload', onclick: () => settingsNdLoadPeers() });
  ui.root = el('div', { class: 'nd-panel' },
    settingsHead('This node'), ui.thisHost,
    el('div', { class: 'nd-sec-head' }, settingsHead('Paired nodes'), ui.addBtn),
    ui.pairedStatus, ui.pairedList, ui.pairedEmpty,
    el('div', { class: 'nd-sec-head' }, settingsHead('Found on your tailnet'), ui.refresh),
    el('div', { class: 'dim set-note', text: 'Finding a device gives it no access to this board: only pairing does. "On the tailnet" means the device is connected to Tailscale, not that ccboard answers there.' }),
    ui.status, ui.problem, ui.empty, ui.list,
    el('div', { class: 'nd-sec-head' }, settingsHead('Who can control this node'), ui.codeBtn),
    el('div', { class: 'dim set-note', text: 'Nodes that hold a token for this board, and what each may do. Revoke cuts one off at once.' }),
    ui.inList, ui.inEmpty,
    el('div', { class: 'nd-sec-head' }, settingsHead('Activity'), ui.reload),
    ui.auditList, ui.auditEmpty);
  settingsNd.ui = ui;
  return ui;
}

function settingsNodes(p) {
  const ui = settingsNdUi();
  if (ui.root.parentNode !== p) { p.textContent = ''; p.append(ui.root); }
  settingsNdPaintThis(ui.thisHost, state);
  settingsNdPaintPaired(ui, state);
  settingsNdPaintFound(ui, state);
  settingsNdPaintIncoming(ui);
  settingsNdPaintAudit(ui);
}

/* GET /api/nodes/discover (refresh: ?refresh=1). A newer ask makes an older answer stale; a failed ask keeps the last good list on screen and says so. A press while one is
   running does nothing (the button stays focusable: aria-busy, not disabled). */
async function settingsNdLoad(refresh) {
  if (settingsNd.busy) return false;
  const mine = ++settingsNd.seq;
  settingsNd.busy = true;
  settingsNd.err = '';
  settingsNd.rev++;
  settingsFill('nodes', true);
  let d = null;
  try { d = await api('GET', '/api/nodes/discover' + (refresh ? '?refresh=1' : '')); } catch (e) {
    if (mine !== settingsNd.seq) return false;
    settingsNd.busy = false;
    settingsNd.err = (e && e.message) || 'the board did not answer';
    settingsNd.rev++;
    settingsFill('nodes', true);
    return false;
  }
  if (mine !== settingsNd.seq) return false;
  settingsNd.busy = false;
  if (d && typeof d === 'object' && Array.isArray(d.rows)) { settingsNd.data = d; settingsNd.at = Date.now(); } else settingsNd.err = 'the board answered without a list';
  settingsNd.rev++;
  settingsFill('nodes', true);
  return true;
}

/* The tab was opened (mount or a tab switch): look once if nothing was fetched yet or the last answer is older than the stale mark. */
function settingsNdOpen() {
  settingsNdOpenPeers();
  if (settingsNd.busy) return;
  if (settingsNd.data && Date.now() - settingsNd.at < NodeView.STALE_S * 1000) return;
  settingsNdLoad(false);
}

/* The page goes away: an answer still on its way is dropped and the frame is built again with the next visit. */
function settingsNdDispose() {
  settingsNdSheetClose();
  const P = settingsNdPeers;
  P.seq++;
  P.busy = false;
  P.nodes = P.pairs = P.audit = null;
  P.err = '';
  P.at = 0;
  P.now = null;
  P.act = {};
  P.rotated = {};
  settingsNd.seq++;
  settingsNd.busy = false;
  settingsNd.ui = null;
  settingsNd.data = null;
  settingsNd.at = 0;
  settingsNd.err = '';
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
  if (h && h.host_note === 'wsl2') p.append(el('div', { class: 'dim', text: 'WSL2: these numbers are for the Linux VM, not for Windows.' }));
  const bk = state.backup;
  p.append(settingsHead('Backup'));
  if (bk && bk.at) {
    const failed = bk.status === 'failed';
    const partial = bk.status === 'partial';
    p.append(settingsKv('Last run', el('span', { class: failed ? 'v bad' : partial ? 'v warn' : 'v', title: failed ? (bk.errors || []).join('\n') : partial ? (bk.warnings || []).join('\n') : 'last nightly backup',
      text: `${failed ? 'failed' : partial ? 'ok, backup branch refused' : 'ok'} ${fmtAge(Date.parse(bk.at) / 1000)} ago` })));
  } else p.append(settingsKv('Last run', el('span', { class: 'dim', text: 'no backup has run yet' })));
  if (bk && bk.at) p.append(settingsKv('Contains', el('span', { class: 'dim', text: backupContainsText(bk) })));
  const cd = state.claude_defaults;
  if (cd) {
    p.append(settingsHead('Claude defaults'));
    p.append(settingsKv('Subagent model', el('span', { class: 'v', text: cd.subagent_model === 'inherit' ? 'inherit: subagents use the main model' : cd.subagent_model }),
      el('span', { class: 'dim', text: 'CCBOARD_SUBAGENT_MODEL: the default model for subagents of the sessions, tasks and scheduled runs the board starts; the launcher can override it per launch.' })));
    p.append(settingsKv('Fable runs', el('span', { class: 'v', text: `Max $ ${cd.fable_cap}` }),
      el('span', { class: 'dim', text: 'CCBOARD_HEADLESS_FABLE_CAP: the most a scheduled or batch run that uses Fable may be capped at. Each such job also needs its own acknowledgement.' })));
  }
  p.append(settingsHead('claude-mem'));
  p.append(settingsMemBlock());
}

/* The claude-mem write-back (v0.5.20, issue #10): one switch, off by default, stored on the box (kv mem_writeback through GET / PUT
   /api/memory/prefs). On, each finished task's result is saved to claude-mem as a note; the switch only says the preference was saved, a note
   shows up in the Memory timeline. Optimistic: a failed PUT puts the switch back and shows the error beside it. Disabled, with the box's
   reason, when claude-mem is off or its plugin is missing or disabled. */
const SETTINGS_MEM_NOTE = "Save each finished task's result to claude-mem. Results can contain anything the agent printed.";
const settingsMemState = { prefs: { writeback: false }, available: null, reason: null, sends: null, at: 0, err: null, repaint: null };

function settingsMemAdopt(r) {
  const st = settingsMemState;
  if (!r || typeof r !== 'object') return;
  if (r.prefs && typeof r.prefs.writeback === 'boolean') st.prefs.writeback = r.prefs.writeback;
  if (typeof r.available === 'boolean') { st.available = r.available; st.reason = typeof r.reason === 'string' ? r.reason : null; }
  if (r.writeback_sends && typeof r.writeback_sends === 'object') st.sends = r.writeback_sends;
}

async function settingsMemLoad() {
  const st = settingsMemState;
  if (st.at && Date.now() - st.at < 20000) return;
  st.at = Date.now();
  try { settingsMemAdopt(await api('GET', '/api/memory/prefs')); } catch (e) { st.at = 0; st.err = e.message; }
  if (st.repaint) st.repaint();
}

async function settingsMemSet(on) {
  const st = settingsMemState;
  const before = st.prefs.writeback;
  st.prefs.writeback = on;
  st.err = null;
  if (st.repaint) st.repaint();
  try { if (!(typeof demoOn === 'function' && demoOn())) settingsMemAdopt(await api('PUT', '/api/memory/prefs', { writeback: on })); } catch (e) {
    st.prefs.writeback = before;
    st.err = `Not saved: ${e.message}`;
  }
  if (st.repaint) st.repaint();
}

function settingsMemBlock() {
  const st = settingsMemState;
  const box = el('input', { type: 'checkbox' });
  box.addEventListener('change', () => settingsMemSet(box.checked));
  const why = el('span', { class: 'dim' });
  const err = el('span', { class: 'v bad', role: 'status' });
  const sends = el('ul', { class: 'set-mem-sends dim' });
  const tile = typeof memoryHealthTile === 'function' && typeof state !== 'undefined' && state && state.memory ? memoryHealthTile(state.memory, { link: true }) : null;     // v0.5.20 health tile (pages/memory.js)
  if (typeof memoryHealthTile !== 'function' && typeof state !== 'undefined' && state && state.memory && typeof Lazy !== 'undefined' && Lazy.bundles && Lazy.bundles.memory) {      // memory.js is lazy (lazy.js): only a box with claude-mem loads it for the tile
    Lazy.load('memory').then(() => { if (settingsPage.refs && typeof memoryHealthTile === 'function') settingsFill('box', true); }, () => { /* no tile */ });
  }
  const hasViewer = typeof state !== 'undefined' && state && state.config && typeof state.config.mem_viewer_url === 'string' && /^https:\/\//.test(state.config.mem_viewer_url);
  const wrap = el('div', { class: 'set-mem' },
    tile,
    tile && !hasViewer ? el('p', { class: 'dim set-mem-viewer-hint', text: 'No claude-mem viewer link yet: set CCBOARD_MEM_HTTPS_PORT on the box and rerun ./install.sh to add one (the README says who can reach it).' }) : null,
    el('label', { class: 'set-check set-pref' }, box,
      el('span', { class: 'set-pref-t' }, el('b', { text: 'Save finished tasks to claude-mem' }), el('span', { class: 'dim', text: SETTINGS_MEM_NOTE }))),
    why, err,
    el('details', { class: 'set-mem-what' }, el('summary', { text: 'What is sent' }), sends));
  function paint() {
    box.checked = st.prefs.writeback === true;
    box.disabled = st.available === false;
    why.textContent = st.available === false ? `Not available: ${st.reason || 'claude-mem is not set up on this box'}.` : '';
    err.textContent = st.err || '';
    const s = st.sends || {};
    sends.textContent = '';
    for (const line of [`The title "${s.title || 'Task: <title>'}".`, `The result: ${s.text || "the task's result, at most 8 KB, cut at a line break and marked"}.`,
      `The project key: ${s.project || "the repo's claude-mem key"}.`, 'The task id and the agent.', 'Never the prompt.',
      'Saved notes appear in the Memory timeline for anyone who can open the board, and in claude-mem\'s own viewer for whoever can open that.']) {
      sends.append(el('li', { text: line }));
    }
  }
  st.repaint = paint;
  paint();
  settingsMemLoad();
  return wrap;
}

/* What the last nightly run put in the restic set (issue #24), by name, read from bk.paths: the board database, the Claude transcripts, the Codex rollouts, the
   claude-mem snapshot, extra paths. A staged file is not a backup until restic recorded a snapshot id, so without one the line says so; it never claims an
   off-box copy (whether the repository shares the disk is the doctor's backup-repo row). Saved logins are never in the set. */
function backupContainsText(bk) {
  const r = bk.restic || {};
  if (r.skipped || !Array.isArray(bk.paths)) return 'restic is off, so nothing is in a snapshot · saved logins are never backed up';
  const name = (path) => {
    const s = String(path);
    if (/\/ccboard\.db$/.test(s)) return 'board database';
    if (/\/claude-mem\.db$/.test(s)) return 'claude-mem snapshot';
    if (/\/projects$/.test(s)) return 'Claude transcripts';
    if (/\/sessions$/.test(s)) return 'Codex rollouts';
    return 'extra path';
  };
  const names = [];
  for (const path of bk.paths) { const n = name(path); if (!names.includes(n)) names.push(n); }
  return `${names.length ? names.join(', ') : 'nothing'}${r.snapshot_id ? '' : ' (staged, no restic snapshot recorded)'} · saved logins are never backed up`;
}

/* What the git step of the last backup did: ' · 2 branches copied to ccboard-backup/<node>/ in 1 of 9 repos', or that every repo was
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
  if (typeof launcherSchema !== 'function') {          // launcher.js is a lazy bundle (lazy.js): its embedded catalogue is what is counted, so the Agents tab loads it once and repaints
    if (typeof Lazy !== 'undefined' && Lazy.bundles && Lazy.bundles.launcher && settingsPage.refs) Lazy.load('launcher').then(() => { if (settingsPage.refs) settingsFill('agents', true); }, () => { /* the row stays out */ });
    return 0;
  }
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
  if (!codex) { settingsMcpMount(p); return; }
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

  settingsMcpMount(p);                                                  // Connect from another device (issue #14), below the agent rows
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

/* ---------- Connect from another device (issue #14): the remote MCP endpoint /mcp, its device tokens and the client commands ----------
   GET /api/mcp/tokens answers {enabled, tokens: [{id, name, scopes, created_at, last_used_at, last_tool, expires_at, expired}], max_tokens}; PUT /api/mcp/remote {enabled}
   switches it; POST /api/mcp/tokens {name, scopes, expires_days} mints one and is the only answer that ever carries a token; DELETE /api/mcp/tokens/<id> revokes.
   The block is built once per visit (settingsMcp.node) and moved into the Agents panel on every rebuild, so a name being typed survives a poll. It shows what the
   server last answered: the switch and a Revoke stay pending until the answer arrives, and a revoked device leaves the list only then.
   The token is never kept: it goes from the mint answer straight into the text nodes of a one-time <dialog> (never into settingsMcp, localStorage, sessionStorage, a URL
   or the state), and closing the dialog empties and removes it. The commands come from MCP_COMMANDS (tests/js/mcp-remote.test.mjs compares them with the README) and
   are offered only with a board URL (state.config.public_url): no guessed address. */
const MCP_COMMANDS = Object.freeze({
  claude: 'claude mcp add --transport http --scope user --header "Authorization: Bearer {token}" ccboard {url}',
  codex: 'export CCBOARD_MCP_TOKEN={token}\ncodex mcp add ccboard --url {url} --bearer-token-env-var CCBOARD_MCP_TOKEN',
});
const MCP_CHECKS = Object.freeze({ claude: 'claude mcp list', codex: 'codex mcp list' });
const MCP_NOTE = "Lets Claude Code or Codex on another device of your tailnet list, create and start board tasks through /mcp, each device with a token of its own. The box's hook token never leaves the box.";
const MCP_BOX_NOTE = 'Not needed on the box itself: the stdio ccboard server is registered there already.';
const MCP_SESSIONS_WARN = 'Sessions lets the device type a task into a session that is already running.';
const MCP_URL_WARN = 'No board address: set CCBOARD_PUBLIC_URL on the box (the https address of the board on your tailnet) and rerun ./install.sh. Until then no command is shown, so none points at a guessed address.';
const MCP_SCOPES = [['read', 'Read', 'list projects and tasks, read results'], ['tasks', 'Tasks', 'create tasks and start them in a new session'], ['sessions', 'Sessions', 'hand a task to a running session']];
const MCP_DAYS = [30, 90, 365];
const MCP_NAME_MAX = 40;
const settingsMcp = { data: null, at: 0, err: '', busy: null, node: null, paint: null, dialog: null, addBtn: null };

function mcpCommand(kind, token, url) { return MCP_COMMANDS[kind].split('{token}').join(String(token)).split('{url}').join(String(url)); }

/* The endpoint's address: the board's public URL + /mcp, or '' when there is none (or it is not an http(s) address). */
function mcpUrl(st) {
  const raw = st && st.config && typeof st.config.public_url === 'string' ? st.config.public_url.trim() : '';
  if (!/^https?:\/\/[^\s/]+(\/\S*)?$/.test(raw)) return '';
  return raw.replace(/\/+$/, '') + '/mcp';
}

function mcpDate(iso) { return typeof iso === 'string' && iso ? iso.slice(0, 10) : ''; }

/* Only the fields the block shows; a token in an answer is never copied in. */
function settingsMcpAdopt(r) {
  if (!r || typeof r !== 'object') return;
  const str = (v) => (typeof v === 'string' && v ? v : null);
  const tokens = [];
  for (const t of Array.isArray(r.tokens) ? r.tokens : []) {
    if (!t || typeof t.id !== 'string') continue;
    tokens.push({ id: t.id, name: String(t.name || ''), scopes: Array.isArray(t.scopes) ? t.scopes.filter((s) => typeof s === 'string') : [],
      created_at: str(t.created_at), last_used_at: str(t.last_used_at), last_tool: str(t.last_tool), expires_at: str(t.expires_at), expired: t.expired === true });
  }
  settingsMcp.data = { enabled: r.enabled === true, max: Number.isInteger(r.max_tokens) ? r.max_tokens : 10, tokens };
}

function settingsMcpRepaint() { if (settingsMcp.paint) settingsMcp.paint(); }

async function settingsMcpLoad(force) {
  if (!force && settingsMcp.at && Date.now() - settingsMcp.at < 15000) return;
  settingsMcp.at = Date.now();
  try { settingsMcpAdopt(await api('GET', '/api/mcp/tokens')); settingsMcp.err = ''; } catch (e) { settingsMcp.at = 0; settingsMcp.err = `Could not read the device tokens: ${e.message}`; }
  settingsMcpRepaint();
}

async function settingsMcpSwitch(on) {
  if (settingsMcp.busy) { settingsMcpRepaint(); return; }
  settingsMcp.busy = 'switch';
  settingsMcp.err = '';
  settingsMcpRepaint();
  try { settingsMcpAdopt(await api('PUT', '/api/mcp/remote', { enabled: !!on })); } catch (e) { settingsMcp.err = `Not switched: ${e.message}`; }
  settingsMcp.busy = null;
  settingsMcpRepaint();
}

async function settingsMcpRevoke(id) {
  if (settingsMcp.busy) return;
  settingsMcp.busy = `revoke:${id}`;
  settingsMcp.err = '';
  settingsMcpRepaint();
  try { settingsMcpAdopt(await api('DELETE', `/api/mcp/tokens/${encodeURIComponent(id)}`)); } catch (e) { settingsMcp.err = `Not revoked: ${e.message}`; }
  settingsMcp.busy = null;
  settingsMcpRepaint();
}

/* The one-time dialog: the token, a Copy button, and the two commands with the token and the URL in them. Closing it (Done, Escape, the backdrop) empties every
   node, removes the dialog and puts the focus back on Add device. */
function settingsMcpShowToken(token, name) {
  if (settingsMcp.dialog) settingsMcp.dialog.close();
  const url = mcpUrl(typeof state !== 'undefined' ? state : null);
  let shell = null;
  shell = modalShell('qr-editor readout mcp-mint', `Token for ${name}`, () => {
    shell.dlg.textContent = '';
    if (settingsMcp.dialog === shell) settingsMcp.dialog = null;
    if (settingsMcp.addBtn && typeof settingsMcp.addBtn.focus === 'function') settingsMcp.addBtn.focus();
  });
  const block = (label, kind) => {
    const cmd = mcpCommand(kind, token, url);
    return el('div', { class: 'mcp-cmd' }, el('div', { class: 'mcp-cmd-head' }, el('b', { text: label }), copyButton(cmd, `${label} command`)),
      el('pre', { class: 'cmd-readout', text: cmd }), el('span', { class: 'dim', text: `Check it with: ${MCP_CHECKS[kind]}` }));
  };
  const cmds = url ? [block('Claude Code', 'claude'), block('Codex', 'codex')] : [el('p', { class: 'warn qr-hint mcp-nourl', role: 'status', text: MCP_URL_WARN })];
  shell.dlg.append(el('div', { class: 'qr-box' },
    el('h2', { class: 'qr-title', text: `Token for ${name}` }),
    el('p', { class: 'qr-hint warn', text: 'Copy it now: it cannot be shown again. Anyone who has it can do what its scopes allow until it expires or you revoke it.' }),
    el('div', { class: 'mcp-cmd' }, el('div', { class: 'mcp-cmd-head' }, el('b', { text: 'Token' }), copyButton(token, 'token')), el('pre', { class: 'cmd-readout mcp-token', text: token })),
    ...cmds,
    el('div', { class: 'qr-actions' }, el('button', { type: 'button', onclick: () => shell.close(), text: 'Done' }))));
  settingsMcp.dialog = shell;
  shell.show();
}

async function settingsMcpMint(body, onError) {
  if (settingsMcp.busy) return;
  settingsMcp.busy = 'mint';
  settingsMcp.err = '';
  settingsMcpRepaint();
  let r = null;
  try { r = await api('POST', '/api/mcp/tokens', body); } catch (e) { onError(e.message); }
  settingsMcp.busy = null;
  if (r && typeof r === 'object') {
    settingsMcpAdopt(r);
    const token = typeof r.token === 'string' ? r.token : '';
    if (token) settingsMcpShowToken(token, (r.record && r.record.name) || body.name);
    else if (!(typeof demoOn === 'function' && demoOn())) onError('the board answered without a token');
  }
  settingsMcpRepaint();
  return !!(r && typeof r.token === 'string');
}

function settingsMcpRow(t) {
  const chips = el('span', { class: 'set-chips' }, t.scopes.map((s) => el('span', { class: 'badge hue-slate', text: s })),
    t.expired ? el('span', { class: 'badge warn', text: 'expired' }) : null);
  const used = t.last_used_at ? `last used ${fmtAge(Date.parse(t.last_used_at) / 1000) || '0s'} ago${t.last_tool ? ` (${t.last_tool})` : ''}` : 'never used';
  const meta = el('span', { class: 'dim', text: [t.created_at ? `added ${mcpDate(t.created_at)}` : '', used, t.expired ? 'expired' : (t.expires_at ? `expires ${mcpDate(t.expires_at)}` : '')].filter(Boolean).join(' · ') });
  const act = settingsMcp.busy === `revoke:${t.id}` ? el('button', { class: 'danger', type: 'button', disabled: true, text: 'Revoking…' })
    : confirmButton(`mcp-revoke:${t.id}`, 'Revoke', () => settingsMcpRevoke(t.id), false);
  const row = settingsKv(t.name, chips, meta, act);
  row.classList.add('set-mcp-row');
  row.setAttribute('data-token', t.id);
  return row;
}

function settingsMcpBlock() {
  const box = el('input', { type: 'checkbox', role: 'switch' });
  box.addEventListener('change', () => settingsMcpSwitch(box.checked));
  const status = el('span', { class: 'dim', role: 'status' });
  const sw = el('label', { class: 'set-check set-pref mcp-switch' }, box,
    el('span', { class: 'set-pref-t' }, el('b', { text: 'Allow other devices' }), el('span', { class: 'dim', text: MCP_NOTE })));
  const err = el('div', { class: 'bad set-note', role: 'alert' });
  const urlWarn = el('div', { class: 'warn set-note mcp-nourl', role: 'status', text: MCP_URL_WARN });
  const count = el('div', { class: 'dim set-note mcp-count' });
  const list = el('div', { class: 'mcp-list' });

  const name = el('input', { type: 'text', class: 'mcp-name', maxlength: String(MCP_NAME_MAX), autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', placeholder: 'laptop' });
  const nameField = field('Device name', name, `1 to ${MCP_NAME_MAX} characters; it names the token in this list`);
  const checks = {};
  const scopeRows = MCP_SCOPES.map(([id, label, what]) => {
    const c = el('input', { type: 'checkbox', 'data-scope': id });
    c.checked = id !== 'sessions';
    checks[id] = c;
    return el('label', { class: 'set-check set-pref' }, c, el('span', { class: 'set-pref-t' }, el('b', { text: label }), el('span', { class: 'dim', text: what })));
  });
  const warnS = el('div', { class: 'warn set-note mcp-sessions-warn', text: MCP_SESSIONS_WARN });
  const scopeBox = el('div', { class: 'mcp-scopes' }, scopeRows, warnS);
  const scopeField = field('Scopes', scopeBox);
  const days = selectEl(MCP_DAYS.map((d) => [String(d), `${d} days`]), '90');
  const daysField = field('Expires after', days);
  const add = el('button', { class: 'primary', type: 'submit', text: 'Add device' });
  settingsMcp.addBtn = add;
  const form = el('form', { class: 'add-form mcp-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); submit(); } },
    nameField, scopeField, daysField, el('div', { class: 'add-btns' }, add));
  const on = el('div', { class: 'mcp-on hidden' }, urlWarn, count, list, el('h4', { class: 'mcp-add-h', text: 'Add device' }), form);
  const node = el('div', { class: 'set-mcp' }, sw, status, el('div', { class: 'dim set-note', text: MCP_BOX_NOTE }), err, on);

  async function submit() {
    if (settingsMcp.busy === 'mint') return;                            // a second tap while minting does nothing
    const n = String(name.value || '').trim().replace(/\s+/g, ' ');
    if (!n || n.length > MCP_NAME_MAX) { fieldError(nameField, `Name the device: 1 to ${MCP_NAME_MAX} characters.`, true); return; }
    const scopes = MCP_SCOPES.map(([id]) => id).filter((id) => checks[id].checked);
    if (!scopes.length) { fieldError(scopeField, 'Pick at least one scope.'); return; }
    fieldError(nameField, '');
    fieldError(scopeField, '');
    const ok = await settingsMcpMint({ name: n, scopes, expires_days: parseInt(days.value, 10) || 90 }, (msg) => fieldError(nameField, `Not added: ${msg}`));
    if (ok) {                                                           // a new device starts from the defaults; a refusal keeps what was entered
      name.value = '';
      for (const [id] of MCP_SCOPES) checks[id].checked = id !== 'sessions';
      days.value = '90';
    }
  }

  function paint() {
    const d = settingsMcp.data;
    const live = !!(d && d.enabled);
    box.checked = live;                                                 // what the server said, never the tap that is still on its way
    box.disabled = settingsMcp.busy === 'switch' || !d;
    box.setAttribute('aria-checked', live ? 'true' : 'false');
    status.textContent = settingsMcp.busy === 'switch' ? 'Saving…' : (!d && !settingsMcp.err ? 'Checking…' : '');
    err.textContent = settingsMcp.err || '';
    on.classList.toggle('hidden', !live);
    urlWarn.classList.toggle('hidden', !!mcpUrl(typeof state !== 'undefined' ? state : null));
    warnS.classList.toggle('hidden', !checks.sessions.checked);
    const toks = d ? d.tokens : [];
    count.textContent = toks.length ? `${toks.length} of ${d.max} devices` : 'No device has a token yet.';
    list.textContent = '';
    for (const t of toks) list.append(settingsMcpRow(t));
    add.disabled = settingsMcp.busy === 'mint';
    add.textContent = settingsMcp.busy === 'mint' ? 'Adding…' : 'Add device';
  }
  checks.sessions.addEventListener('change', paint);
  settingsMcp.paint = paint;
  settingsMcp.node = node;
  paint();
  return node;
}

/* Put the block (built once per visit) at the end of the Agents panel and repaint it from what the server last said. */
function settingsMcpMount(p) {
  p.append(settingsHead('Connect from another device'));
  p.append(settingsMcp.node || settingsMcpBlock());
  settingsMcpRepaint();
  settingsMcpLoad(false);
}

function settingsMcpDispose() {
  if (settingsMcp.dialog) settingsMcp.dialog.close();
  settingsMcp.dialog = null;
  settingsMcp.node = null;
  settingsMcp.paint = null;
  settingsMcp.addBtn = null;
  settingsMcp.at = 0;
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
const SETTINGS_LOGIN_TERMINAL = 'To use another subscription, run /login in any terminal; the board notices within a minute and starts a new row for it. The Usage page keeps its split per account, and saved Codex logins still switch where Codex keeps its login in a file.';
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
    if (mode === 'off') setText(reason, store.reason ? `${store.reason.charAt(0).toUpperCase()}${store.reason.slice(1).replace(/\.$/, '')}. Adding and switching Claude accounts is off on this box.` : 'Saved logins are not available on this box. Adding and switching Claude accounts is off.');
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
    if (root.scrollIntoView) { try { root.scrollIntoView({ block: 'nearest', behavior: scrollBehavior() }); } catch (_) { /* no scrolling here */ } }
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
  const flagged = acctProblemHit(state, a, 'codex');                  // the rollout Tailer raises a Codex problem from an auth-looking error item (#35)
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

/* #36: the one-time notice of a split-off account (state.codex_accounts.notice {key, label, text, at}: a hand `codex login` to another account was saved as a
   new one). Rename opens the shared rename sheet on that account (a rename drops the notice on the box); Dismiss hides it at once and tells the box. */
function settingsCxSplitNotice() {
  const cs = cxState(state);
  const n = cs && cs.notice;
  return n && typeof n === 'object' && n.key && cxAccounts(state).some((a) => a.key === n.key) ? n : null;
}

function settingsCxSplitRename() {
  const n = settingsCxSplitNotice();
  const a = n && cxAccounts(state).find((x) => x.key === n.key);
  if (a) settingsRenameAccount(a, { codex: true });
}

async function settingsCxSplitDismiss() {
  const cs = cxState(state);
  if (cs) cs.notice = null;
  cxRepaint();
  try {
    await api('DELETE', '/api/codex-accounts/notice');
  } catch (e) {
    pageToast(`Could not hide the notice: ${acctReason(e)}`, 'bad');
  }
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
    if (root.scrollIntoView) { try { root.scrollIntoView({ block: 'nearest', behavior: scrollBehavior() }); } catch (_) { /* no scrolling here */ } }
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
  const cxSplitText = el('span', { class: 'cx-split-text' });
  const cxSplit = el('div', { class: 'warn set-note cx-split hidden', role: 'status' }, cxSplitText,
    el('span', { class: 'set-acts' },
      el('button', { class: 'primary tinted', type: 'button', title: 'Name the new Codex account', onclick: () => settingsCxSplitRename(), text: 'Rename' }),
      el('button', { type: 'button', title: 'Hide this notice', onclick: () => settingsCxSplitDismiss(), text: 'Dismiss' })));
  const cxSec = el('div', { class: 'cx-section hidden' }, settingsHead('Codex accounts'), cxErr, cxSplit, cxList, cxNote, cxWarn, settingsHead('Add a Codex account'), cx.root);
  p.append(err, settingsHead('Subscription accounts'), list, el('div', { class: 'dim set-note', text: SETTINGS_RECOVERY }), sw, settingsHead('Add another subscription'), add.root, cxSec);
  return { err, list, sw, cont, add, cxSec, cxErr, cxList, cxNote, cxWarn, cxSplit, cxSplitText, cx };
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
  const split = settingsCxSplitNotice();
  const stext = split ? String(split.text || '') : '';
  if (s.cxSplitText.textContent !== stext) s.cxSplitText.textContent = stext;
  s.cxSplit.classList.toggle('hidden', !split);
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
  if (id === 'notify') return JSON.stringify([st.config && st.config.ntfy, st.config && st.config.backup, st.backup, st.config && st.config.auto_continue, minute]);
  if (id === 'nodes') {                                                // the card without its clock ('now' moves with every poll), the CCBOARD_NODES rows, the tailnet list's own revision and a 15 s age bucket
    const n = st.node && typeof st.node === 'object' ? st.node : null;
    const armed = /^node-/.test(String(ui.confirm || '')) ? ui.confirm : null;                 // the two-tap Rotate token, Remove and Revoke repaint their row
    return JSON.stringify([st.nodes, n && [n.node_id, n.name, n.os, n.lanes, n.agents], st.agents, st.node_name, settingsNd.rev, armed, Math.floor(Date.now() / 15000), Nodes.ready ? Nodes.M.rev : 0]);
  }
  if (id === 'box') return JSON.stringify([st.health, st.backup, st.node_name, st.user, st.claude_defaults, minute]);
  if (id === 'accounts') {                                             // identity and labels, not the readings: those move with every statusline and would rebuild the Rename button under a finger (they refresh with the minute)
    const forget = /^((acct|cx)-forget:|cx-logout)/.test(String(ui.confirm || '')) ? ui.confirm : null;      // the two-tap Forget login repaints the row
    return JSON.stringify([st.accounts && st.accounts.current, agentsAccounts(st).map((a) => [a.key, a.label, a.name, a.email, a.plan, !!a.current, !!a.saved]), acctStore(st), acctFlow.busy, forget, minute,
      cxState(st) ? [st.codex_accounts.current, cxAccounts(st).map((a) => [a.key, a.label, a.plan, !!a.current, !!a.saved]), cxStore(st), cxFlow.busy] : null,
      (acctProblem(st) || {}).account || (acctProblem(st) ? '*' : null)]);                  // the amber chip and the lead action follow the problem at once
  }
  if (id === 'app') return JSON.stringify([st.version, settingsAppMode().note, !!settingsInstallPrompt(), settingsHelpAvailable()]);
  if (id === 'doctor') return JSON.stringify(doctorSig());
  const mcpArmed = /^mcp-revoke:/.test(String(ui.confirm || '')) ? ui.confirm : null;                // the two-tap Revoke of a device token (the block itself repaints in place)
  return JSON.stringify([st.claude, st.agents, ui.confirm === 'logout', ui.confirm === 'cx-logout', mcpArmed, cxFlow.err, doctorSig(), cxState(st) ? (cxAccounts(st).find((a) => a.current) || {}).key || null : null]);     // the two-tap Log out repaints the panel; the doctor's answer repaints the checks under each card
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
  if (id === 'nodes') settingsNdOpen();                              // opening the tab is the ask: one look, never a timer
  if (id === 'accounts') settingsAcctPatch();                       // a Log in tapped on another page starts the add flow here
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
    if (typeof tabCtl.link === 'function') tabCtl.link(panels);                                     // aria-controls / aria-labelledby between each tab and its panel
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
    settingsMcpDispose();                                             // the one-time token dialog never outlives the page
    settingsNdDispose();                                              // an answer from the tailnet list still on its way is dropped
    settingsPage.refs = null;
  },
});
