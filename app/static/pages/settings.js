/* ccboard settings page (#/settings[?sec=notify|nodes|box|agents|app]): the Notify panel (Web Push, ntfy, backup), the Nodes strip, the
   box health, the agents (Claude login) and the App panel (installed or browser, Install, Safari steps, build id, Reload app, shortcuts).
   The section is picked with ?sec= and tabs(); each panel is rebuilt only when the
   state it shows changed, so a poll never recreates a button under a finger. renderNotifyPanel() and renderNodes() stay global:
   core.js (enablePush / disablePush) calls the first one. The login modal itself lives in pages/home.js (startLogin / openModal). */
'use strict';

const SETTINGS_SECTIONS = [
  { id: 'notify', label: 'Notifications' }, { id: 'nodes', label: 'Nodes' }, { id: 'box', label: 'Box' }, { id: 'agents', label: 'Agents' }, { id: 'app', label: 'App' },
];
const settingsPage = { refs: null, active: 'notify' };

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

function settingsAgents(p) {
  p.textContent = '';
  const c = state.claude || {};
  const badge = el('span', { class: 'badge' });
  // who is logged in is an identity, not a verdict: the agent's own hue (violet for Claude, teal for Codex), never the green that means ok
  if (!c.installed) { badge.classList.add('bad'); badge.textContent = 'claude not installed'; }
  else if (c.loggedIn) { badge.classList.add('hue-violet'); badge.textContent = `Claude: ${c.email || 'logged in'}${c.subscriptionType ? ' (' + c.subscriptionType + ')' : ''}`; }
  else { badge.classList.add('warn'); badge.textContent = 'Claude: not logged in'; }
  const row = settingsKv('Claude', badge);
  if (c.installed && !c.loggedIn) row.add(el('button', { class: 'primary', type: 'button', onclick: startLogin, text: 'Log in' }));
  if (c.installed && c.loggedIn) row.add(confirmButton('logout', 'Log out', logout, true));          // red-outlined, two taps: the login is not one tap to lose
  if (state.login && state.login.running && !ui.modal) row.add(el('button', { type: 'button', onclick: () => openModal(), text: 'Login in progress…' }));
  p.append(row);
  const codex = state.agents && state.agents.codex;
  if (codex) p.append(settingsKv('Codex', el('span', { class: codex.installed ? 'badge hue-teal' : 'v dim', text: codex.installed ? 'installed' : 'not installed' })));
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

const SETTINGS_BUILD = { notify: settingsNotify, nodes: settingsNodes, box: settingsBox, agents: settingsAgents, app: settingsApp };

/* What a panel shows, as a string: the panel is rebuilt only when it changes. */
function settingsSig(id, st) {
  const minute = Math.floor(Date.now() / 60000);                       // ages ("3h ago") move on, so the minute is part of the key
  if (id === 'notify') return JSON.stringify([st.config && st.config.ntfy, st.config && st.config.backup, st.backup, minute]);
  if (id === 'nodes') return JSON.stringify(st.nodes);
  if (id === 'box') return JSON.stringify([st.health, st.backup, st.node_name, st.user, minute]);
  if (id === 'app') return JSON.stringify([st.version, settingsAppMode().note, !!settingsInstallPrompt(), settingsHelpAvailable()]);
  return JSON.stringify([st.claude, st.agents, st.login && st.login.running, ui.modal, ui.confirm === 'logout']);     // the two-tap Log out repaints the panel
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
