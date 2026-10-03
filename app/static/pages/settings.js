/* ccboard settings page (#/settings[?sec=notify|nodes|box|agents]): the Notify panel (Web Push, ntfy, backup), the Nodes strip, the
   box health and the agents (Claude login). The section is picked with ?sec= and tabs(); each panel is rebuilt only when the
   state it shows changed, so a poll never recreates a button under a finger. renderNotifyPanel() and renderNodes() stay global:
   core.js (enablePush / disablePush) calls the first one. The login modal itself lives in pages/home.js (startLogin / openModal). */
'use strict';

const SETTINGS_SECTIONS = [
  { id: 'notify', label: 'Notifications' }, { id: 'nodes', label: 'Nodes' }, { id: 'box', label: 'Box' }, { id: 'agents', label: 'Agents' },
];
const settingsPage = { refs: null, active: 'notify' };

function settingsKv(label, ...kids) { return el('div', { class: 'kv' }, el('b', { text: label }), ...kids); }

function settingsNotify(p) {
  p.textContent = '';
  const n = (state.config && state.config.ntfy) || {};
  const pushRow = settingsKv('Web Push');
  p.append(pushRow);
  pushSubscription().then((sub) => {
    if (sub) pushRow.append(el('span', { class: 'v', text: 'enabled on this device' }),
      el('button', { type: 'button', onclick: () => disablePush().catch((e) => setError(e.message)), text: 'Disable' }),
      el('button', { type: 'button', onclick: async () => { try { const r = await api('POST', '/api/push/test'); if (r.sent) pageToast('Test push sent', 'ok'); else setError('no push sent (' + r.subscriptions + ' subscriptions)'); } catch (e) { setError(e.message); } }, text: 'Test push' }));
    else pushRow.append(el('button', { class: 'primary', type: 'button', onclick: () => enablePush().catch((e) => setError(e.message)), text: 'Enable push on this device' }),
      el('span', { class: 'dim', text: 'Works in Chrome/Android and in an installed (Home Screen) PWA on iOS 16.4+.' }));
  }).catch(() => pushRow.append(el('span', { class: 'dim', text: 'Web Push not available here.' })));

  const bc = (state.config && state.config.backup) || {};
  const bk = state.backup;
  const bkRow = settingsKv('Backup');
  p.append(bkRow);
  if (bk && bk.at) {
    const r = bk.restic || {};
    const pushed = (bk.push || []).reduce((sum, x) => sum + (x.pushed || []).length, 0);
    bkRow.append(el('span', { class: bk.status === 'ok' ? 'v' : 'v bad', text: `${bk.status} ${fmtAge(Date.parse(bk.at) / 1000)} ago` }),
      el('span', { class: 'dim', text: r.snapshot_id ? `snapshot ${String(r.snapshot_id).slice(0, 8)} → ${r.repo || ''}` : (r.skipped ? 'restic off' : 'no snapshot') + ` · ${pushed} branch(es) pushed across ${(bk.push || []).length} repo(s)` }));
    if (bk.status !== 'ok') bkRow.append(el('span', { class: 'bad', text: (bk.errors || []).join(' · ').slice(0, 300) }));
  } else {
    bkRow.append(el('span', { class: 'dim', text: 'no backup has run yet (nightly via ccboard-backup.timer)' + (bc.restic && !bc.restic_installed ? ' · restic is not installed' : '') }));
  }
  bkRow.append(el('button', { type: 'button', onclick: async () => { try { await api('POST', '/api/backup/run'); setError(null); pageToast('Backup started', 'ok'); setTimeout(() => poll(true), 3000); } catch (e) { setError(e.message); } }, text: 'Back up now' }),
    el('span', { class: 'dim', text: (bc.restic ? `restic → ${bc.repo}` : 'restic off') + (bc.push ? ' · git push --all origin for every repo' : ' · no git push') }));

  const ntfyRow = settingsKv('ntfy app');
  p.append(ntfyRow);
  if (!n.enabled) {
    ntfyRow.append(el('span', { class: 'dim', text: 'not configured on the box (NTFY_URL in /etc/ccboard/env, or rerun install.sh).' }));
    return;
  }
  const base = (n.subscribe_url || '').replace(/\/[^/]*$/, '');
  ntfyRow.append(el('code', { text: n.subscribe_url || '' }),
    el('button', { type: 'button', onclick: async () => { try { await navigator.clipboard.writeText(n.subscribe_url || ''); pageToast('Copied', 'ok'); } catch (_) { pageToast('Copy failed', 'warn'); } }, text: 'Copy' }),
    el('button', { class: 'primary', type: 'button', onclick: async () => {
      try { const r = await api('POST', '/api/notify/test'); if (r.ok) { setError(null); pageToast('Test sent to ntfy. If the phone stays silent, check the steps below.', 'ok'); } else setError('ntfy publish failed (is ntfy running?)'); }
      catch (e) { setError(e.message); }
    }, text: 'Send test' }));
  p.append(el('details', { class: 'dim' }, el('summary', { text: 'Phone setup (ntfy app)' }),
    el('ol', {},
      el('li', {}, 'Install the ntfy app (App Store / Play Store) and keep the Tailscale VPN on: the server is only reachable on the tailnet.'),
      el('li', {}, 'Add a subscription → "Use another server" → server ', el('code', { text: base }), ', topic ', el('code', { text: n.topic || '' }), '.'),
      el('li', {}, 'iPhone: this server relays wake-ups through ntfy.sh (no message content); allow notifications for the app. Android: allow the app to run in the background for instant delivery.'),
      el('li', {}, 'Tap "Send test" above. Pushes go out on needs-you, done, error and rate limit; “Terminal” opens the session, “Ack” clears it.'))));
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
  if (bk && bk.at) {
    const failed = bk.status !== 'ok';
    p.append(settingsKv('Backup', el('span', { class: failed ? 'v bad' : 'v', title: failed ? (bk.errors || []).join('\n') : 'last nightly backup', text: `${failed ? 'failed' : 'ok'} ${fmtAge(Date.parse(bk.at) / 1000)} ago` })));
  } else p.append(settingsKv('Backup', el('span', { class: 'dim', text: 'no backup has run yet' })));
}

function settingsAgents(p) {
  p.textContent = '';
  const c = state.claude || {};
  const badge = el('span', { class: 'badge' });
  if (!c.installed) { badge.classList.add('bad'); badge.textContent = 'claude not installed'; }
  else if (c.loggedIn) { badge.classList.add('ok'); badge.textContent = `Claude: ${c.email || 'logged in'}${c.subscriptionType ? ' (' + c.subscriptionType + ')' : ''}`; }
  else { badge.classList.add('warn'); badge.textContent = 'Claude: not logged in'; }
  const row = settingsKv('Claude', badge);
  if (c.installed && !c.loggedIn) row.append(el('button', { class: 'primary', type: 'button', onclick: startLogin, text: 'Log in' }));
  if (c.installed && c.loggedIn) row.append(el('button', { type: 'button', onclick: logout, text: 'Log out' }));
  if (state.login && state.login.running && !ui.modal) row.append(el('button', { type: 'button', onclick: () => openModal(), text: 'Login in progress…' }));
  p.append(row);
  const codex = state.agents && state.agents.codex;
  if (codex) p.append(settingsKv('Codex', el('span', { class: codex.installed ? 'v' : 'v dim', text: codex.installed ? 'installed' : 'not installed' })));
}

const SETTINGS_BUILD = { notify: settingsNotify, nodes: settingsNodes, box: settingsBox, agents: settingsAgents };

/* What a panel shows, as a string: the panel is rebuilt only when it changes. */
function settingsSig(id, st) {
  const minute = Math.floor(Date.now() / 60000);                       // ages ("3h ago") move on, so the minute is part of the key
  if (id === 'notify') return JSON.stringify([st.config && st.config.ntfy, st.config && st.config.backup, st.backup, minute]);
  if (id === 'nodes') return JSON.stringify(st.nodes);
  if (id === 'box') return JSON.stringify([st.health, st.backup, st.node_name, st.user, minute]);
  return JSON.stringify([st.claude, st.agents, st.login && st.login.running, ui.modal]);
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
    const tabCtl = settingsTabs(active, (id) => navigate(buildHash('settings', {}, { sec: id }), { replace: true }));
    const panels = {};
    const wrap = el('div', { class: 'settings-page' }, el('div', { class: 'page-head' }, el('h1', { text: 'Settings' })), tabCtl.root);
    for (const sec of SETTINGS_SECTIONS) {
      panels[sec.id] = el('div', { class: 'section settings-panel hidden', role: 'tabpanel', 'data-sec': sec.id });
      wrap.append(panels[sec.id]);
    }
    root.append(wrap);
    settingsPage.refs = { tabs: tabCtl, panels };
    settingsShow(active);
  },
  update() { settingsFill(settingsPage.active, false); },
  onRoute(route) { settingsShow(settingsSecOf(route)); },
  unmount() { settingsPage.refs = null; },
});
