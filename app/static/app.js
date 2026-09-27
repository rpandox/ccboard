/* ccboard frontend: one render(state) fed by polling. Dynamic text goes through textContent only. */
'use strict';

const $ = (sel) => document.querySelector(sel);

/* Blueprint (vendored CSS, dark theme): the semantic classes used below are mapped to bp5-* classes here, so the
   renderers stay readable. primary/danger -> intents, state/badge -> tags, card -> card, inputs -> bp5-input. */
const INTENT = { primary: 'bp5-intent-primary', danger: 'bp5-intent-danger', ok: 'bp5-intent-success', bad: 'bp5-intent-danger',
                 warn: 'bp5-intent-warning', working: 'bp5-intent-primary', waiting: 'bp5-intent-warning', done: 'bp5-intent-success',
                 errored: 'bp5-intent-danger' };
function blueprint(n, tag, cls) {
  const list = cls ? cls.split(/\s+/) : [];
  const has = (c) => list.includes(c);
  if (tag === 'button' || (tag === 'a' && has('btn'))) {
    n.classList.add('bp5-button');
    for (const c of list) if (INTENT[c] && (c === 'primary' || c === 'danger')) n.classList.add(INTENT[c]);
    if (has('icon') || has('minimal')) n.classList.add('bp5-minimal');
    if (has('small')) n.classList.add('bp5-small');
  } else if (tag === 'input') {
    const t = n.getAttribute('type') || 'text';
    if (t !== 'checkbox' && t !== 'radio') n.classList.add('bp5-input');
  } else if (tag === 'textarea') {
    n.classList.add('bp5-text-area', 'bp5-fill');
  } else if (has('card')) {
    n.classList.add('bp5-card', 'bp5-elevation-1');
  } else if (has('state') || has('badge')) {
    n.classList.add('bp5-tag', 'bp5-minimal', 'bp5-round');
    for (const c of list) if (INTENT[c] && c !== 'primary' && c !== 'danger') n.classList.add(INTENT[c]);
  }
}
function ic(name) { return el('span', { class: 'bp5-icon bp5-icon-' + name, 'aria-hidden': 'true' }); }

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  blueprint(n, tag, (attrs && attrs.class) || '');
  const kids = children.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false);
  const isButton = n.classList.contains('bp5-button');
  for (const c of kids) {
    // Blueprint spaces a button's element children (icon + text) only when the text is an element too
    if (typeof c === 'string') n.append(isButton && kids.length > 1 ? el('span', { class: 'bp5-button-text', text: c }) : document.createTextNode(c));
    else n.append(c);
  }
  if (tag === 'label' && n.firstElementChild && n.firstElementChild.type === 'checkbox') {
    n.classList.add('bp5-control', 'bp5-checkbox');
    n.firstElementChild.after(el('span', { class: 'bp5-control-indicator' }));
  }
  return n;
}

async function api(method, path, body) {
  const headers = { 'X-CCBoard': '1' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = null;
  try { data = await r.json(); } catch (_) { /* not json */ }
  if (!r.ok) throw new Error((data && data.error) || `${r.status} ${r.statusText}`);
  return data;
}

const ui = { openForm: null, confirm: null, error: null, notice: null, modal: false, lastJson: null, inboxSel: -1, notifyPanel: false, deepLinked: false };
let state = null;
let pollTimer = null;

function setError(msg) { ui.error = msg; renderBanner(); }

function codeServerUrl(path) {
  return `https://${location.hostname}:${state.config.code_https_port}/?folder=${encodeURIComponent(path)}`;
}

function fmtAge(epoch) {
  if (!epoch) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/* ---------- usage & limits strip ---------- */

function fmtIn(epochSeconds) {
  if (!epochSeconds) return '';
  const s = Math.floor(epochSeconds - Date.now() / 1000);
  if (s <= 0) return 'now';
  if (s < 3600) return `${Math.ceil(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d${Math.floor((s % 86400) / 3600)}h`;
}

function pctBar(pct) {
  const cls = pct >= 85 ? ' bad' : pct >= 60 ? ' warn' : '';
  const i = el('i'); i.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  return el('span', { class: 'bar' + cls }, i);
}

function renderUsage() {
  const u = $('#usage');
  if (!u) return;
  u.textContent = '';
  const rl = (state.usage && state.usage.value) || {};
  const blk = (state.block && state.block.value) || null;
  const segs = [];
  const win = (key, label) => {
    const w = rl[key];
    if (!w || typeof w.used_percentage !== 'number') return;
    segs.push(el('span', { class: 'seg', title: `${label} window, from Claude Code's statusline` },
      el('b', { text: label }), pctBar(w.used_percentage), `${Math.round(w.used_percentage)}%`,
      w.resets_at ? el('span', { text: `resets in ${fmtIn(w.resets_at)}` }) : null));
  };
  win('five_hour', '5h'); win('seven_day', 'week'); win('spend_limit', 'spend');
  if (blk && blk.available && blk.active) {
    const bits = [];
    if (typeof blk.burn_cost_per_hour === 'number') bits.push(`burn $${blk.burn_cost_per_hour.toFixed(2)}/h`);
    if (typeof blk.cost_usd === 'number') bits.push(`block $${blk.cost_usd.toFixed(2)}` + (typeof blk.projected_cost === 'number' ? ` → $${blk.projected_cost.toFixed(0)} proj.` : ''));
    if (typeof blk.remaining_minutes === 'number') bits.push(`${Math.floor(blk.remaining_minutes / 60)}h${blk.remaining_minutes % 60}m left`);
    segs.push(el('span', { class: 'seg', title: 'ccusage blocks --active' }, el('b', { text: 'ccusage' }), bits.join(' · ')));
  } else if (blk && !blk.available) {
    segs.push(el('span', { class: 'seg dim', text: 'ccusage not installed (burn rate unavailable)' }));
  }
  const h = state.health;
  if (h && (h.cpu_pct !== null || h.mem || h.disk)) {
    const bits = [];
    if (typeof h.cpu_pct === 'number') bits.push(`cpu ${h.cpu_pct}%`);
    else if (typeof h.load1 === 'number') bits.push(`load ${h.load1.toFixed(2)}`);
    if (h.mem) bits.push(`ram ${h.mem.pct}%`);
    if (h.disk) bits.push(`disk ${h.disk.pct}%`);
    segs.push(el('span', { class: 'seg', title: `${h.host}${h.uptime_s ? ' · up ' + Math.floor(h.uptime_s / 3600) + 'h' : ''}` }, el('b', { text: state.node_name || 'box' }), bits.join(' · ')));
  }
  const bk = state.backup;
  if (bk && bk.at) {
    const failed = bk.status !== 'ok';
    segs.push(el('span', { class: 'seg' + (failed ? ' bad' : ''), title: failed ? (bk.errors || []).join('\n') : 'last nightly backup (restic + git push --all); details in the 🔔 panel' },
      el('b', { text: 'backup' }), `${failed ? 'failed' : 'ok'} ${fmtAge(Date.parse(bk.at) / 1000) + " ago"}`));
  }
  const chips = [];
  for (const p of state.projects) for (const r of repoGroups(p)) for (const s of r.sessions) {
    if (s.stats && (s.stats.model || typeof s.stats.context_pct === 'number') && s.state !== 'ended') {
      chips.push(el('span', { class: 'chip', text: `${p.name}/${r.name}·${s.name} ${s.stats.model || ''}${typeof s.stats.context_pct === 'number' ? ' ctx ' + Math.round(s.stats.context_pct) + '%' : ''}` }));
    }
  }
  if (!segs.length && !chips.length) { u.classList.add('hidden'); return; }
  u.classList.remove('hidden');
  segs.forEach(s => u.append(s));
  chips.forEach(c => u.append(c));
}

/* ---------- PWA: service worker, offline shell, Web Push ---------- */

const LAST_KEY = 'ccboard:last-state';

function rememberState(json) { try { localStorage.setItem(LAST_KEY, JSON.stringify({ at: Date.now(), state: json })); } catch (_) { /* storage may be unavailable */ } }
function recallState() { try { const v = JSON.parse(localStorage.getItem(LAST_KEY) || 'null'); return v && v.state ? v : null; } catch (_) { return null; } }

if ('serviceWorker' in navigator) {
  const hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => { /* no SW: the board still works */ });
  let reloaded = false;
  navigator.serviceWorker.addEventListener('controllerchange', () => {
    // a new worker took over after a deploy: load the new shell once (never on the very first install, and not
    // again right after a version-change reload)
    let justReloaded = false;
    try { justReloaded = sessionStorage.getItem('ccboard:reloaded') === '1'; sessionStorage.removeItem('ccboard:reloaded'); } catch (_) { /* ignore */ }
    if (hadController && !reloaded && !justReloaded) { reloaded = true; location.reload(); }
  });
}

function b64ToBytes(s) {
  const pad = '='.repeat((4 - s.length % 4) % 4);
  const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, ch => ch.charCodeAt(0));
}

async function pushSubscription() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
  const reg = await navigator.serviceWorker.ready;
  return reg.pushManager.getSubscription();
}

async function enablePush() {
  if (!('PushManager' in window)) { setError('Web Push is not available in this browser (on iOS, add the board to the Home Screen first).'); return; }
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') { setError('Notifications were not allowed.'); return; }
  const { key } = await api('GET', '/api/push/vapid');
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
  await api('POST', '/api/push/subscribe', { subscription: sub.toJSON() });
  setError(null); renderNotifyPanel();
}

async function disablePush() {
  const sub = await pushSubscription();
  if (sub) { await api('DELETE', '/api/push/subscribe', { subscription: sub.toJSON() }); await sub.unsubscribe(); }
  renderNotifyPanel();
}

/* ---------- notifications panel ---------- */

function renderNodes() {
  const n = $('#nodes');
  if (!n) return;
  n.textContent = '';
  const list = (state.nodes && state.nodes.value) || [];
  if (!list.length) { n.classList.add('hidden'); return; }
  n.classList.remove('hidden');
  n.append(el('b', { text: 'Nodes' }));
  for (const x of list) {
    const h = x.health || {};
    const txt = x.online
      ? `${x.sessions} sess · ${x.attention} need you${typeof h.cpu_pct === 'number' ? ' · cpu ' + h.cpu_pct + '%' : ''}${h.mem ? ' · ram ' + h.mem.pct + '%' : ''}${h.disk ? ' · disk ' + h.disk.pct + '%' : ''}${x.usage && x.usage.five_hour ? ' · 5h ' + Math.round(x.usage.five_hour.used_percentage) + '%' : ''}`
      : 'offline' + (x.error ? ' · ' + x.error.slice(0, 60) : '');
    const cls = 'chip' + (x.online ? (x.attention ? ' attn' : '') : ' bad');
    const safe = /^https:\/\/[A-Za-z0-9.-]+(:\d+)?$/.test(x.url || '');
    n.append(safe ? el('a', { class: cls, href: x.url + '/', target: '_blank', rel: 'noopener', title: x.url, text: `${x.name}: ${txt}` })
                  : el('span', { class: cls, text: `${x.name}: ${txt}` }));
  }
}

function renderNotifyPanel() {
  const p = $('#notify');
  if (!p) return;
  p.textContent = '';
  if (!ui.notifyPanel) { p.classList.add('hidden'); return; }
  p.classList.remove('hidden');
  const n = (state.config && state.config.ntfy) || {};
  const pushRow = el('div', { class: 'row' }, el('b', { text: 'This device (Web Push):' }));
  p.append(pushRow);
  pushSubscription().then(sub => {
    if (sub) pushRow.append(el('span', { class: 'dim', text: 'enabled' }),
      el('button', { onclick: () => disablePush().catch(e => setError(e.message)), text: 'Disable' }),
      el('button', { onclick: async () => { try { const r = await api('POST', '/api/push/test'); setError(r.sent ? null : 'no push sent (' + r.subscriptions + ' subscriptions)'); } catch (e) { setError(e.message); } }, text: 'Test push' }));
    else pushRow.append(el('button', { class: 'primary', onclick: () => enablePush().catch(e => setError(e.message)), text: 'Enable push on this device' }),
      el('span', { class: 'dim', text: 'Works in Chrome/Android and in an installed (Home Screen) PWA on iOS 16.4+.' }));
  }).catch(() => pushRow.append(el('span', { class: 'dim', text: 'Web Push not available here.' })));
  const bkRow = el('div', { class: 'row' }, el('b', { text: 'Backup:' }));
  p.append(bkRow);
  const bc = (state.config && state.config.backup) || {};
  const bk = state.backup;
  if (bk && bk.at) {
    const r = bk.restic || {};
    const pushed = (bk.push || []).reduce((n, x) => n + (x.pushed || []).length, 0);
    bkRow.append(el('span', { class: bk.status === 'ok' ? '' : 'bad', text: `${bk.status} ${fmtAge(Date.parse(bk.at) / 1000) + " ago"}` }),
      el('span', { class: 'dim', text: r.snapshot_id ? `snapshot ${String(r.snapshot_id).slice(0, 8)} → ${r.repo || ''}` : (r.skipped ? 'restic off' : 'no snapshot') + ` · ${pushed} branch(es) pushed across ${(bk.push || []).length} repo(s)` }));
    if (bk.status !== 'ok') bkRow.append(el('span', { class: 'bad', text: (bk.errors || []).join(' · ').slice(0, 300) }));
  } else {
    bkRow.append(el('span', { class: 'dim', text: 'no backup has run yet (nightly via ccboard-backup.timer)' + (bc.restic && !bc.restic_installed ? ' · restic is not installed' : '') }));
  }
  bkRow.append(el('button', { onclick: async () => { try { await api('POST', '/api/backup/run'); setError(null); setTimeout(() => poll(true), 3000); } catch (e) { setError(e.message); } }, text: 'Back up now' }),
    el('span', { class: 'dim', text: (bc.restic ? `restic → ${bc.repo}` : 'restic off') + (bc.push ? ' · git push --all origin for every repo' : ' · no git push') }));
  const ntfyRow = el('div', { class: 'row' }, el('b', { text: 'ntfy app:' }));
  p.append(ntfyRow);
  if (!n.enabled) {
    ntfyRow.append(el('span', { class: 'dim', text: 'not configured on the box (NTFY_URL in /etc/ccboard/env, or rerun install.sh).' }));
    return;
  }
  const base = (n.subscribe_url || '').replace(/\/[^/]*$/, '');
  ntfyRow.append(el('code', { text: n.subscribe_url || '' }),
    el('button', { onclick: async () => { try { await navigator.clipboard.writeText(n.subscribe_url || ''); } catch (_) { /* ignore */ } }, text: 'Copy' }),
    el('button', { class: 'primary', onclick: async () => { try { const r = await api('POST', '/api/notify/test'); setError(r.ok ? null : 'ntfy publish failed (is ntfy running?)'); ui.notice = r.ok ? 'Test sent to ntfy. If the phone stays silent, check the steps below.' : null; renderBanner(); } catch (e) { setError(e.message); } }, text: 'Send test' }));
  p.append(el('details', { class: 'dim' }, el('summary', { text: 'Phone setup (ntfy app)' }),
    el('ol', {},
      el('li', {}, 'Install the ntfy app (App Store / Play Store) and keep the Tailscale VPN on: the server is only reachable on the tailnet.'),
      el('li', {}, 'Add a subscription → "Use another server" → server ', el('code', { text: base }), ', topic ', el('code', { text: n.topic || '' }), '.'),
      el('li', {}, 'iPhone: this server relays wake-ups through ntfy.sh (no message content); allow notifications for the app. Android: allow the app to run in the background for instant delivery.'),
      el('li', {}, 'Tap "Send test" above. Pushes go out on needs-you, done, error and rate limit; “Terminal” opens the session, “Ack” clears it.'))));
}

/* ---------- header / banner ---------- */

function renderHeader() {
  const badge = $('#claude-badge');
  badge.textContent = '';
  badge.className = 'badge';
  const c = state.claude || {};
  if (!c.installed) { badge.classList.add('bad'); badge.textContent = 'claude not installed'; }
  else if (c.loggedIn) { badge.classList.add('ok'); badge.textContent = `Claude: ${c.email || 'logged in'}${c.subscriptionType ? ' (' + c.subscriptionType + ')' : ''}`; }
  else { badge.classList.add('warn'); badge.textContent = 'Claude: not logged in'; }
  const actions = $('#hdr-actions');
  actions.textContent = '';
  actions.append(el('span', { class: 'dim user', text: state.user || '' }));
  if (c.installed && !c.loggedIn) actions.append(el('button', { class: 'primary', onclick: startLogin, text: 'Log in' }));
  if (c.installed && c.loggedIn) actions.append(el('button', { class: 'desk', onclick: logout, text: 'Log out' }));
  const n = state.config && state.config.ntfy;
  actions.append(el('button', { class: 'icon' + (ui.notifyPanel ? ' on' : ''), onclick: () => { ui.notifyPanel = !ui.notifyPanel; renderNotifyPanel(); }, title: 'notifications' }, ic(n && n.enabled ? 'notifications' : 'notifications-snooze')));
  actions.append(el('button', { class: live.on ? 'primary' : '', onclick: toggleLive, title: 'live last lines of every session' }, ic('pulse'), el('span', { class: 'desk', text: 'Live' })));
  actions.append(el('button', { class: 'icon search-toggle', onclick: () => { document.body.classList.toggle('search-open'); const b = $('#hdr-search'); if (document.body.classList.contains('search-open') && b) b.focus(); }, title: 'search transcripts' }, ic('search')));
  actions.append(el('button', { class: 'icon', onclick: refreshNow, title: 'refresh now' }, ic('refresh')));
  if (!$('#hdr-search')) {
    const box = el('input', { id: 'hdr-search', type: 'search', placeholder: 'search transcripts…', title: 'full-text search over Claude transcripts (display only)' });
    box.addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(box.value); if (e.key === 'Escape') { box.value = ''; $('#search').classList.add('hidden'); } });
    actions.append(box);
  } else { actions.append($('#hdr-search')); }
  if (state.login && state.login.running && !ui.modal) actions.append(el('button', { onclick: () => openModal(), text: 'Login in progress…' }));
}

function renderBanner() {
  const b = $('#banner');
  b.textContent = '';
  b.className = '';
  const rlim = state && state.rate_limited && state.rate_limited.value;
  const recent = rlim && state.rate_limited.at && (Date.now() - Date.parse(state.rate_limited.at)) < 5 * 3600 * 1000;
  if (ui.notice) {
    b.className = 'warn';
    b.append(el('span', { text: ui.notice }), el('button', { onclick: () => { ui.notice = null; renderBanner(); }, text: 'ok' }));
  } else if (ui.offline) {
    b.className = 'warn';
    b.append(el('span', { text: `Offline: showing the last known state from ${new Date(ui.offline).toLocaleTimeString()}. Retrying…` }));
  } else if (ui.error) {
    b.append(el('span', { text: ui.error }), el('button', { onclick: () => setError(null), text: 'dismiss' }));
  } else if (state && state.last_recovery && state.last_recovery.value && (state.last_recovery.value.recovered || []).length) {
    b.className = 'warn';
    const v = state.last_recovery.value;
    b.append(el('span', { text: `After a restart, ${v.recovered.length} Claude session${v.recovered.length === 1 ? '' : 's'} relaunched with --resume: ${v.recovered.join(', ')}${v.closed.length ? ' · closed: ' + v.closed.join(', ') : ''}` }),
      el('button', { onclick: async () => { try { await api('POST', '/api/recovery/dismiss'); } catch (e) { setError(e.message); } await poll(true); }, text: 'dismiss' }));
  } else if (recent) {
    b.append(el('span', { text: `Rate limited: ${rlim.message || ''}${rlim.session ? ' (' + rlim.session + ')' : ''}` }),
      el('button', { onclick: async () => { try { await api('POST', '/api/usage/rate-limit/clear'); } catch (e) { setError(e.message); } await poll(true);
try { if (localStorage.getItem('ccboard:live') === '1') { live.on = true; liveStart(); } } catch (_) { /* ignore */ } }, text: 'dismiss' }));
  } else if (state && state.tmux_down) {
    b.append(el('span', { text: 'The ccboard tmux server is not running. On the box: sudo systemctl start ccboard-tmux' }));
  } else if (state && state.claude && state.claude.installed && !state.claude.loggedIn) {
    b.className = 'warn';
    b.append(el('span', { text: 'Claude Code is not logged in on this box.' }), el('button', { class: 'primary', onclick: startLogin, text: 'Log in' }));
  }
}

/* ---------- new project ---------- */

function renderNewProject() {
  const sec = $('#new-project');
  if (sec.childElementCount) return; // static form, built once
  const name = el('input', { type: 'text', placeholder: 'project name (e.g. shop)', required: true, maxlength: 64 });
  const url = el('input', { type: 'text', placeholder: 'optional: clone URL of the first repo' });
  const form = el('form', { class: 'inline', onsubmit: async (e) => {
    e.preventDefault();
    const body = { name: name.value.trim() };
    if (url.value.trim()) body.url = url.value.trim();
    try { await api('POST', '/api/projects', body); name.value = ''; url.value = ''; setError(null); await poll(true); }
    catch (err) { setError(err.message); }
  } }, name, url, el('button', { class: 'primary', type: 'submit', text: 'New project' }));
  sec.append(el('h2', { text: 'Projects' }), el('div', { class: 'dim', text: `Folder per project under ${state.config.projects_dir}; each repo is a subfolder; sessions run inside a repo.` }), form,
    el('div', { class: 'row', id: 'importrow' }, el('button', { onclick: openImport, text: 'Import from GitHub…' }), el('button', { onclick: openBatch, text: 'Batch prompt…' }), el('span', { id: 'queue', class: 'dim' })));
}

/* ---------- batch: one prompt across N repos ---------- */

function openBatch() {
  ui.modal = true;
  const m = $('#modal');
  m.textContent = '';
  const name = el('input', { type: 'text', placeholder: 'batch name (optional)', maxlength: 60 });
  const prompt = el('textarea', { placeholder: 'prompt to run headlessly in every selected repo (claude -p, fresh worktree each)…' });
  const mode = el('select', {}, ...['acceptEdits', 'default', 'plan', 'auto', 'dontAsk'].map(x => el('option', { value: x, text: x })));
  const turns = el('input', { type: 'number', value: '30', min: '1', max: '500' });
  const budget = el('input', { type: 'number', placeholder: 'max $ per repo (optional)', step: '0.5', min: '0' });
  const status = el('div', { class: 'dim' });
  const boxes = [];
  const list = el('div', { class: 'form' });
  for (const x of allRepos()) { const cb = el('input', { type: 'checkbox', value: x.id, checked: false }); boxes.push(cb); list.append(el('label', { class: 'row' }, cb, x.id)); }
  const go = el('button', { class: 'primary', onclick: async () => {
    const repos = boxes.filter(b => b.checked).map(b => b.value);
    if (!repos.length || !prompt.value.trim()) { status.textContent = 'pick repos and write a prompt'; return; }
    try {
      const r = await api('POST', '/api/batch', { prompt: prompt.value.trim(), repos, name: name.value.trim() || undefined, permission_mode: mode.value, max_turns: parseInt(turns.value, 10) || 30, max_budget_usd: budget.value ? parseFloat(budget.value) : undefined });
      status.textContent = `queued ${r.jobs.length} runs (batch ${r.batch_id}); ${r.started.length} started, the rest wait for a free slot`;
      closeModal(); await poll(true);
    } catch (e) { status.textContent = e.message; }
  }, text: 'Run on selected repos' });
  m.append(el('div', { class: 'modal-box' },
    el('h2', { text: 'Batch prompt across repos' }),
    el('div', { class: 'dim', text: 'Runs are headless (claude -p) in a fresh worktree per repo, at most 2 at once, paused while the 5-hour window is above 85%. Each result becomes a task card.' }),
    el('div', { class: 'row' }, name, el('label', { text: 'mode' }), mode, el('label', { text: 'max turns' }), turns, budget),
    prompt,
    list,
    el('div', { class: 'row' }, go, el('button', { onclick: () => { for (const b of boxes) b.checked = !b.checked; }, text: 'Invert' }), el('button', { onclick: closeModal, text: 'Close' }))));
  m.classList.remove('hidden');
}

function renderQueue() {
  const q = $('#queue');
  if (!q || !state.clone_queue) return;
  const cq = state.clone_queue;
  const failed = cq.done.filter(d => d.status === 'failed');
  q.textContent = '';
  if (cq.queued.length) q.append(el('span', { text: `${cq.queued.length} clone${cq.queued.length === 1 ? '' : 's'} queued (max ${cq.cap} at once) ` }));
  if (failed.length) q.append(el('span', { class: 'bad', text: `${failed.length} failed: ${failed.map(f => f.repo + ' (' + (f.error || '') + ')').join('; ').slice(0, 300)} ` }),
    el('button', { onclick: async () => { await api('POST', '/api/clone-queue/clear'); await poll(true); }, text: 'clear' }));
}

/* ---------- import from GitHub ---------- */

function openImport() {
  ui.modal = true;
  const m = $('#modal');
  m.textContent = '';
  const owner = el('input', { type: 'text', placeholder: 'owner (blank = your repos)', maxlength: 39 });
  const target = el('input', { type: 'text', placeholder: 'target project name', maxlength: 64 });
  const filter = el('input', { type: 'text', placeholder: 'filter…' });
  const list = el('div', { class: 'form' });
  const status = el('div', { class: 'dim' });
  let repos = [];
  const boxes = [];
  const renderList = () => {
    list.textContent = ''; boxes.length = 0;
    const f = filter.value.trim().toLowerCase();
    for (const r of repos) {
      if (f && !(r.name.toLowerCase().includes(f) || (r.description || '').toLowerCase().includes(f))) continue;
      const cb = el('input', { type: 'checkbox', value: r.url, 'data-name': r.name, checked: true });
      boxes.push(cb);
      list.append(el('label', { class: 'row' }, cb, el('b', { text: r.name }), el('span', { class: 'dim', text: `${r.private ? 'private' : 'public'}${r.fork ? ' · fork' : ''} · ${r.description || ''}`.slice(0, 120) })));
    }
    if (!list.childElementCount) list.append(el('span', { class: 'dim', text: repos.length ? 'no match' : 'nothing loaded yet' }));
  };
  filter.addEventListener('input', renderList);
  const load = async () => {
    status.textContent = 'loading…';
    try {
      const r = await api('GET', `/api/github/repos${owner.value.trim() ? '?owner=' + encodeURIComponent(owner.value.trim()) : ''}`);
      repos = r.repos; status.textContent = `${repos.length} repos (${r.protocol})`;
      if (!target.value.trim()) target.value = (owner.value.trim() || (state.user || 'github').split('@')[0]).toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
      renderList();
    } catch (e) { status.textContent = e.message; }
  };
  const importBtn = el('button', { class: 'primary', onclick: async () => {
    const chosen = boxes.filter(b => b.checked).map(b => ({ name: b.dataset.name, url: b.value }));
    if (!chosen.length) { status.textContent = 'nothing selected'; return; }
    try {
      const r = await api('POST', `/api/projects/${encodeURIComponent(target.value.trim())}/repos/bulk`, { repos: chosen });
      status.textContent = `queued ${chosen.length} into ${r.project}`; closeModal(); await poll(true);
    } catch (e) { status.textContent = e.message; }
  }, text: 'Import selected' });
  m.append(el('div', { class: 'modal-box' },
    el('h2', { text: 'Import repos from GitHub' }),
    el('div', { class: 'row' }, owner, el('button', { onclick: load, text: 'Load' })),
    el('div', { class: 'row' }, el('label', { text: 'into project' }), target, filter),
    status, list,
    el('div', { class: 'row' }, importBtn, el('button', { onclick: () => { for (const b of boxes) b.checked = !b.checked; }, text: 'Invert' }), el('button', { onclick: closeModal, text: 'Close' }))));
  m.classList.remove('hidden');
  owner.focus();
}

/* ---------- projects ---------- */

function confirmButton(key, label, action, quiet) {
  // Two taps: the first turns the button into "Confirm …" + Cancel. `quiet` renders the first state low-key
  // (minimal, no red fill) for destructive actions that sit next to everyday ones on a phone.
  if (ui.confirm === key) {
    return el('span', { class: 'row' },
      el('button', { class: 'danger confirm', onclick: async () => { ui.confirm = null; try { await action(); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: `Confirm ${label}` }),
      el('button', { onclick: () => { ui.confirm = null; renderProjects(); }, text: 'Cancel' }));
  }
  return el('button', { class: 'danger' + (quiet ? ' minimal' : ''), onclick: () => { ui.confirm = key; renderProjects(); }, text: label });
}

function repoGroups(p) { return p.root ? [p.root, ...p.repos] : p.repos; }   // the project folder row first, then the repos

function allRepos() {
  const out = [];
  for (const p of state.projects) for (const r of p.repos) if (r.state === 'ok' || r.state === 'unknown') out.push({ id: `${p.name}/${r.name}`, project: p.name, repo: r.name });
  return out;
}

const LAUNCH_KEY = (p, r) => `ccboard:launch:${p.name}/${r.name}`;
const TASK_KEY = (p, r) => `ccboard:task:${p.name}/${r.name}`;
const MODELS = [['', 'default (settings)'], ['fable', 'fable'], ['opus', 'opus'], ['sonnet', 'sonnet'], ['haiku', 'haiku'], ['custom', 'custom id…']];
const EFFORTS = [['', 'default'], ['low', 'low'], ['medium', 'medium'], ['high', 'high'], ['xhigh', 'xhigh'], ['max', 'max']];
const PERMS = [['', 'ask (default)'], ['acceptEdits', 'accept edits'], ['plan', 'plan'], ['auto', 'auto'], ['dontAsk', "don't ask: deny prompts"]];
const PERMS_HOST = [...PERMS, ['bypassPermissions', 'bypass: never ask (dangerous)']];
const BYPASS_WARNING = 'Claude runs every command and edit without asking, as your user, on this box. Claude Code asks you to confirm once in the terminal.';

function loadPrefs(key) { try { return JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (_) { return {}; } }
function savePrefs(key, v) { try { localStorage.setItem(key, JSON.stringify(v)); } catch (_) { /* storage may be unavailable */ } }
function field(label, control, hint) { return el('div', { class: 'field' }, el('span', { text: label }), control, hint ? el('span', { class: 'dim', text: hint }) : null); }
function selectEl(options, value) {
  const sel = el('select');
  for (const [v, t] of options) sel.append(el('option', { value: v, text: t }));
  if (value !== undefined && value !== null) sel.value = value;
  return sel;
}

/* Model / effort / permission controls shared by the session and task forms. Returns the grid plus a reader. */
function launchControls(saved, permOptions) {
  const model = selectEl(MODELS, saved.model_sel || '');
  const modelId = el('input', { type: 'text', placeholder: 'full model id, e.g. claude-fable-5-1', class: saved.model_sel === 'custom' ? '' : 'hidden', value: saved.model_id || '' });
  model.addEventListener('change', () => { modelId.classList.toggle('hidden', model.value !== 'custom'); if (model.value === 'custom') modelId.focus(); });
  const effort = selectEl(EFFORTS, saved.effort || '');
  const perm = selectEl(permOptions, saved.permission_mode || '');
  if (!perm.value) perm.value = '';                                       // a remembered choice that no longer exists
  const warn = el('div', { class: 'bad hidden', text: BYPASS_WARNING });
  const syncWarn = () => warn.classList.toggle('hidden', perm.value !== 'bypassPermissions');
  perm.addEventListener('change', syncWarn); syncWarn();
  const grid = el('div', {},
    el('div', { class: 'grid' },
      field('model', el('div', { class: 'field' }, model, modelId)),
      field('effort', effort),
      field('permissions', perm)),
    warn);
  return { grid, model, modelId, effort, perm,
    read: () => ({ model: model.value === 'custom' ? modelId.value.trim() : model.value, effort: effort.value, permission_mode: perm.value }),
    prefs: () => ({ model_sel: model.value, model_id: modelId.value.trim(), effort: effort.value, permission_mode: perm.value }) };
}

function sessionForm(p, r) {
  const saved = loadPrefs(LAUNCH_KEY(p, r));
  const launcher = selectEl([['claude', 'claude: new session'], ['resume', 'claude --resume'], ['continue', 'claude --continue'], ['shell', 'shell']], saved.launcher || 'claude');
  const name = el('input', { type: 'text', placeholder: 'auto: s1, s2…', maxlength: 64 });
  const lc = launchControls(saved, PERMS_HOST);
  const resumeId = el('input', { type: 'text', placeholder: 'session id to resume (blank = picker)', class: 'hidden' });
  const allowed = el('input', { type: 'text', placeholder: 'e.g. Bash(npm test), Read', value: saved.allowed_tools || '' });
  const disallowed = el('input', { type: 'text', placeholder: 'e.g. WebFetch', value: saved.disallowed_tools || '' });
  const sysPrompt = el('textarea', { placeholder: 'text appended to the system prompt (optional)' });
  sysPrompt.value = saved.append_system_prompt || '';
  const args = el('input', { type: 'text', placeholder: 'anything else, e.g. --verbose --fallback-model sonnet', value: saved.args || '' });
  const siblings = r.root ? [] : allRepos().filter(x => x.project === p.name && x.repo !== r.name);   // the project folder already contains them
  const others = allRepos().filter(x => x.project !== p.name);
  const boxes = [];
  const mk = (x, checked) => { const cb = el('input', { type: 'checkbox', value: x.id, checked }); boxes.push(cb); return el('label', {}, cb, x.id); };
  const checks = el('div', { class: 'checks' }, siblings.map(x => mk(x, true)));
  const otherBox = el('div', { class: 'checks' }, others.map(x => mk(x, false)));
  const devc = el('input', { type: 'checkbox' });
  const devRow = r.devcontainer ? el('div', { class: 'checks' },
    el('label', { title: 'devcontainer up + devcontainer exec (needs docker and the devcontainer CLI on the box; log in to Claude inside once)' }, devc, 'run in devcontainer')) : null;
  const claudeOnly = el('div', {}, lc.grid,
    el('details', {}, el('summary', { text: 'More options: tools, system prompt, extra args, other repos' }),
      el('div', { class: 'grid' },
        field('allowed tools', allowed, 'comma-separated; --allowedTools'),
        field('disallowed tools', disallowed, '--disallowedTools')),
      field('append to system prompt', sysPrompt),
      field('extra args', args),
      siblings.length ? field('also give access to (--add-dir)', checks) : null,
      others.length ? field('repos of other projects (--add-dir)', otherBox) : null));
  const sync = () => { claudeOnly.classList.toggle('hidden', launcher.value === 'shell'); resumeId.classList.toggle('hidden', launcher.value !== 'resume'); };
  launcher.addEventListener('change', sync); sync();
  const form = el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = { launcher: launcher.value, devcontainer: devc.checked };
    if (name.value.trim()) body.name = name.value.trim();
    if (launcher.value === 'resume' && resumeId.value.trim()) body.resume_id = resumeId.value.trim();
    if (launcher.value !== 'shell') {
      Object.assign(body, lc.read());
      if (allowed.value.trim()) body.allowed_tools = allowed.value.trim();
      if (disallowed.value.trim()) body.disallowed_tools = disallowed.value.trim();
      if (sysPrompt.value.trim()) body.append_system_prompt = sysPrompt.value.trim();
      if (args.value.trim()) body.args = args.value.trim();
      body.add_dirs = boxes.filter(b => b.checked).map(b => b.value);
    }
    for (const k of Object.keys(body)) if (body[k] === '' || body[k] === null) delete body[k];
    savePrefs(LAUNCH_KEY(p, r), { launcher: launcher.value, ...lc.prefs(), allowed_tools: allowed.value.trim(), disallowed_tools: disallowed.value.trim(),
      append_system_prompt: sysPrompt.value.trim(), args: args.value.trim() });
    const tab = window.open('', '_blank');
    try {
      const res = await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/sessions`, body);
      if (tab) tab.location = `/term/${encodeURIComponent(res.tmux)}`;
      ui.openForm = null; setError(null); await poll(true);
    } catch (err) { if (tab) tab.close(); setError(err.message); }
  } },
    el('div', { class: 'grid' }, field('launch', launcher), field('session name', name)),
    resumeId,
    claudeOnly,
    devRow,
    el('div', { class: 'submit' },
      el('button', { class: 'primary', type: 'submit' }, ic('play'), 'Start & open terminal'),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
  return form;
}

function addRepoForm(p) {
  const name = el('input', { type: 'text', placeholder: 'repo name (blank git init)', maxlength: 64 });
  const url = el('input', { type: 'text', placeholder: 'or clone URL' });
  return el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    if (name.value.trim()) body.name = name.value.trim();
    if (url.value.trim()) body.url = url.value.trim();
    try { await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos`, body); ui.openForm = null; setError(null); await poll(true); }
    catch (err) { setError(err.message); }
  } },
    el('div', { class: 'row' }, name, url),
    el('div', { class: 'row' },
      el('button', { class: 'primary', type: 'submit', text: 'Add repo' }),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
}

const STATE_LABEL = { idle: 'idle', working: 'working', waiting: 'needs you', done: 'done', errored: 'error', ended: 'ended', unknown: '' };

function stateBadge(s) {
  const st = s.state || 'unknown';
  if (!STATE_LABEL[st]) return null;
  const age = s.state_at ? fmtAge(Date.parse(s.state_at) / 1000) : '';
  return el('span', { class: `state ${st}` + (s.needs_attention ? ' attn' : ''), title: s.last_event || '' },
    STATE_LABEL[st] + (age ? ` ${age}` : ''));
}

function statsText(s) {
  const t = s.stats;
  if (!t) return '';
  const parts = [];
  if (t.model) parts.push(t.model);
  if (typeof t.context_pct === 'number') parts.push(`ctx ${Math.round(t.context_pct)}%`);
  if (typeof t.cost_usd === 'number') parts.push(`$${t.cost_usd.toFixed(2)}`);
  return parts.join(' · ');
}

function sessionRow(s) {
  const row = el('div', { class: 'sess' + (s.needs_attention ? ' attn' : ''), 'data-tmux': s.tmux },
    el('div', { class: 'main' },
      el('span', { class: 'name', text: s.name }),
      stateBadge(s),
      el('span', { class: 'meta', text: [s.launcher, statsText(s), `${fmtAge(s.created)} · ${s.attached} attached`].filter(Boolean).join(' · ') }),
      el('code', { text: s.command || '' })),
    el('div', { class: 'actions' },
      el('a', { class: 'btn primary', href: `/term/${encodeURIComponent(s.tmux)}`, target: '_blank', rel: 'noopener', text: 'Open terminal' }),
      s.needs_attention ? el('button', { onclick: async () => { try { await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/ack`); } catch (e) { setError(e.message); } await poll(true); }, text: 'Ack' }) : null,
      confirmButton('kill:' + s.tmux, 'Kill session', () => api('DELETE', `/api/sessions/${encodeURIComponent(s.tmux)}`), true)));
  if (s.last_message || s.last_prompt) {
    row.append(el('div', { class: 'last' },
      s.last_prompt ? el('span', { class: 'dim', text: '› ' + s.last_prompt.slice(0, 120) }) : null,
      s.last_message ? el('span', { text: s.last_message.slice(0, 160) }) : null));
  }
  return row;
}

function rootBlock(p, r) {
  const key = `${p.name}/${r.name}`;
  const title = el('div', { class: 'row head' },
    el('div', { class: 'row' },
      el('span', { class: 'title', text: '📁 project folder' }),
      el('span', { class: 'hint', text: 'a session here sees every repo below (no --add-dir needed)' }),
      repoCost(p, r) ? el('span', { class: 'dim', text: repoCost(p, r) }) : null,
      r.devcontainer ? el('span', { class: 'badge', title: '.devcontainer found: sessions can run inside it', text: 'devcontainer' }) : null),
    el('div', { class: 'actions' },
      el('button', { class: 'primary', onclick: () => { ui.openForm = 'session:' + key; renderProjects(); }, text: 'New session' }),
      el('a', { class: 'btn', href: codeServerUrl(r.path), target: '_blank', rel: 'noopener', text: 'code-server' })));
  const block = el('div', { class: 'repo root' }, title);
  if (ui.openForm === 'session:' + key) block.append(sessionForm(p, r));
  for (const s of r.sessions) block.append(sessionRow(s));
  return block;
}

function repoBlock(p, r) {
  const key = `${p.name}/${r.name}`;
  const title = el('div', { class: 'row head' },
    el('div', { class: 'row' },
      el('span', { class: 'title', text: r.name }),
      r.state === 'ok' ? [el('span', { class: 'dim', text: r.branch || '' }), el('span', { class: 'dot' + (r.dirty ? ' dirty' : ''), title: r.dirty ? 'uncommitted changes' : 'clean' })] : null,
      repoCost(p, r) ? el('span', { class: 'dim', text: repoCost(p, r) }) : null,
      r.state === 'cloning' ? el('span', { class: 'badge warn', text: 'cloning…' }) : null,
      r.state === 'clone-failed' ? el('span', { class: 'badge bad', text: 'clone failed (attach to see the error)' }) : null,
      r.state === 'nogit' ? el('span', { class: 'badge bad', text: 'no git' }) : null,
      r.state === 'unknown' ? el('span', { class: 'dot unknown', title: 'git status unknown' }) : null,
      r.devcontainer ? el('span', { class: 'badge', title: '.devcontainer found: sessions can run inside it', text: 'devcontainer' }) : null),
    el('div', { class: 'actions' },
      el('button', { class: 'primary', onclick: () => { ui.openForm = 'session:' + key; renderProjects(); }, text: 'New session' }),
      r.state === 'ok' ? el('button', { onclick: () => { ui.openForm = 'task:' + key; renderProjects(); }, text: 'New task' }) : null,
      r.state === 'ok' ? el('button', { onclick: () => { ui.openForm = 'job:' + key; renderProjects(); }, text: 'Schedule…' }) : null,
      el('a', { class: 'btn', href: codeServerUrl(r.path), target: '_blank', rel: 'noopener', text: 'code-server' }),
      confirmButton('rm:' + key, 'Remove', () => api('DELETE', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}`))));
  const block = el('div', { class: 'repo' }, title);
  if (ui.openForm === 'session:' + key) block.append(sessionForm(p, r));
  if (ui.openForm === 'task:' + key) block.append(taskForm(p, r));
  if (ui.openForm === 'job:' + key) block.append(jobForm(p, r));
  for (const s of r.sessions) block.append(sessionRow(s));
  return block;
}

function costText(p) {
  const c = state.cost && state.cost.value && state.cost.value.projects && state.cost.value.projects[p.name];
  if (!c) return '';
  return `$${c.today.toFixed(2)} today · $${c.week.toFixed(2)} 7d · $${c.total.toFixed(2)} total`;
}

function repoCost(p, r) {
  const c = state.cost && state.cost.value && state.cost.value.projects && state.cost.value.projects[p.name];
  const v = c && c.repos && c.repos[r.name];
  return typeof v === 'number' ? `$${v.toFixed(2)}` : '';
}

function projectCard(p) {
  const nSess = repoGroups(p).reduce((n, r) => n + r.sessions.length, 0) + p.orphan_sessions.length;
  const card = el('div', { class: 'card' },
    el('div', { class: 'row head' },
      el('div', { class: 'row' }, el('h2', { text: p.name }), el('span', { class: 'dim', text: `${p.repos.length} repo${p.repos.length === 1 ? '' : 's'} · ${nSess} session${nSess === 1 ? '' : 's'}` }), costText(p) ? el('span', { class: 'dim', title: 'from ccusage, sessions started by ccboard', text: costText(p) }) : null),
      el('div', { class: 'actions' },
        el('button', { onclick: () => { ui.openForm = 'repo:' + p.name; renderProjects(); }, text: 'Add repo' }),
        el('a', { class: 'btn', href: codeServerUrl(p.path), target: '_blank', rel: 'noopener', text: 'code-server' }),
        confirmButton('del:' + p.name, 'Delete', () => api('DELETE', `/api/projects/${encodeURIComponent(p.name)}`)))));
  if (ui.openForm === 'repo:' + p.name) card.append(addRepoForm(p));
  if (p.root) card.append(rootBlock(p, p.root));
  if (!p.repos.length) card.append(el('div', { class: 'dim', text: 'No repos yet. Add one (blank or clone URL).' }));
  for (const r of p.repos) card.append(repoBlock(p, r));
  for (const s of p.orphan_sessions) card.append(el('div', { class: 'repo' }, el('div', { class: 'dim', text: `sessions in removed repo ${s.repo}` }), sessionRow(s)));
  return card;
}

/* ---------- live last-lines grid (SSE) ---------- */

const live = { on: false, es: null, tiles: {} };

function liveTile(name) {
  let t = live.tiles[name];
  if (t) return t;
  const pre = el('pre');
  const parts = name.split('--');
  t = el('div', { class: 'tile', 'data-tmux': name },
    el('div', { class: 'row' }, el('span', { class: 'name', text: `${parts[0]}/${parts[1]} · ${parts[2] || ''}` }),
      el('a', { class: 'btn', href: `/term/${encodeURIComponent(name)}`, target: '_blank', rel: 'noopener', text: 'Attach' })),
    pre);
  t.pre = pre;
  live.tiles[name] = t;
  $('#live .live-grid').append(t);
  return t;
}

function liveStart() {
  if (live.es) return;
  const sec = $('#live');
  sec.textContent = '';
  sec.append(el('div', { class: 'row head' }, el('h2', { text: 'Live' }), el('span', { class: 'dim', text: 'last 20 lines of every session, every 2 s' })),
    el('div', { class: 'live-grid' }));
  sec.classList.remove('hidden');
  live.tiles = {};
  const es = new EventSource('/api/stream');
  live.es = es;
  es.addEventListener('lines', (e) => {
    try { const d = JSON.parse(e.data); liveTile(d.name).pre.textContent = d.lines.join('\n'); } catch (_) { /* ignore */ }
  });
  es.addEventListener('tick', (e) => {
    try {
      const d = JSON.parse(e.data);
      for (const n of Object.keys(live.tiles)) if (!d.sessions.includes(n)) { live.tiles[n].remove(); delete live.tiles[n]; }
      const attn = new Set(inboxItems().map(s => s.tmux));
      for (const [n, t] of Object.entries(live.tiles)) t.classList.toggle('attn', attn.has(n));
      if (!d.sessions.length) $('#live .live-grid').textContent = '';
    } catch (_) { /* ignore */ }
  });
  es.onerror = () => { /* EventSource reconnects on its own */ };
}

function liveStop() {
  if (live.es) { live.es.close(); live.es = null; }
  $('#live').classList.add('hidden');
  live.tiles = {};
}

function toggleLive() {
  live.on = !live.on;
  try { localStorage.setItem('ccboard:live', live.on ? '1' : '0'); } catch (_) { /* ignore */ }
  if (live.on) liveStart(); else liveStop();
  renderHeader();
}

/* ---------- tasks (worktree per task) ---------- */

const COLUMNS = [['in_progress', 'In progress'], ['needs_you', 'Needs you'], ['done', 'Done'], ['pr', 'PR open'], ['merged', 'Merged']];

function ciBadge(t) {
  if (!t.pr_url) return null;
  const b = (t.ci && t.ci.bucket) || 'none';
  const cls = b === 'pass' ? 'ok' : b === 'fail' ? 'bad' : b === 'pending' ? 'warn' : '';
  const review = t.pr && t.pr.review ? ` · ${t.pr.review.toLowerCase().replace('_', ' ')}` : '';
  const txt = `${(t.pr_state || 'PR').toLowerCase()} · CI ${b}${review}`;
  return el('span', { class: 'badge ' + cls, title: (t.ci && t.ci.checks || []).map(c => `${c.name}: ${c.bucket}`).join('\n'), text: txt });
}

function taskCard(t) {
  const s = t.session;
  const ciFail = t.ci && t.ci.bucket === 'fail';
  const card = el('div', { class: 'task' + (s && s.needs_attention ? ' attn' : ''), 'data-task': t.id },
    el('div', { class: 'row' }, el('span', { class: 'title', text: t.title }), s ? stateBadge(s) : el('span', { class: 'state ended', text: 'no session' }), ciBadge(t)),
    el('div', { class: 'meta', text: `${t.project}/${t.repo} · ${t.branch}${t.pr_url ? ' · PR #' + t.pr_number : ''}${typeof t.cost_usd === 'number' ? ' · $' + t.cost_usd.toFixed(2) : ''}` }),
    s && s.last_message ? el('div', { class: 'last', text: s.last_message.slice(0, 160) }) : null,
    (t.overlap && t.overlap.length) ? el('div', { class: 'last bad', title: t.overlap.map(o => `${o.title}: ${o.files.join(', ')}`).join('\n'),
      text: '⚠ overlaps ' + t.overlap.map(o => `"${o.title}" (${o.files.length} file${o.files.length === 1 ? '' : 's'}: ${o.files.slice(0, 3).join(', ')}${o.files.length > 3 ? '…' : ''})`).join('; ') }) : null,
    el('div', { class: 'actions' },
      el('a', { class: 'btn primary', href: `/term/${encodeURIComponent(t.tmux)}`, target: '_blank', rel: 'noopener' }, ic('console'), 'Terminal'),
      el('button', { onclick: () => openTaskModal(t), text: t.pr_url ? 'Diff / PR' : 'Diff / PR…' }),
      t.pr_url ? el('a', { class: 'btn', href: t.pr_url, target: '_blank', rel: 'noopener', text: 'PR' }) : null,
      t.preview_url ? el('a', { class: 'btn', href: t.preview_url, target: '_blank', rel: 'noopener', text: `Preview :${t.preview_port}` }) : null,
      t.preview_url ? el('button', { onclick: async () => { try { await api('DELETE', `/api/tasks/${t.id}/preview`); } catch (e) { setError(e.message); } await poll(true); }, title: 'stop exposing the preview', text: '⏏' }) :
        el('button', { onclick: async () => {
          try { const r = await api('POST', `/api/tasks/${t.id}/preview`, {}); ui.notice = `preview at ${r.url} → 127.0.0.1:${r.port}`; }
          catch (e) { if (/no listening port/.test(e.message)) { const p = window.prompt(e.message + '\n\nDev server port (leave blank to cancel):'); if (p) { try { await api('POST', `/api/tasks/${t.id}/preview`, { port: parseInt(p, 10) }); } catch (e2) { setError(e2.message); } } } else setError(e.message); }
          await poll(true);
        }, title: 'expose a dev server running in this session on its own tailnet HTTPS port', text: 'Preview' }),
      ciFail ? el('button', { class: 'danger', onclick: async () => { try { const r = await api('POST', `/api/tasks/${t.id}/fix-ci`); setError(null); ui.notice = `CI logs (${r.chars} chars) sent to ${t.title}${r.relaunched ? ' (session relaunched)' : ''}`; } catch (e) { setError(e.message); } await poll(true); }, text: 'Fix CI' }) : null,
      t.pr_url ? el('button', { onclick: async () => { try { await api('POST', `/api/tasks/${t.id}/refresh`); } catch (e) { setError(e.message); } await poll(true); }, title: 'refresh PR / CI status', text: '↻' }) : null,
      confirmButton('arch:' + t.id, 'Archive', async () => {
        try { await api('POST', `/api/tasks/${t.id}/archive`, { force: false }); }
        catch (e) {
          if (/force/.test(e.message) && window.confirm(e.message + '\n\nDiscard the worktree anyway?')) await api('POST', `/api/tasks/${t.id}/archive`, { force: true });
          else throw e;
        }
      })));
  return card;
}

let diff2htmlLoading = null;
function loadDiff2Html() {
  if (window.Diff2HtmlUI) return Promise.resolve();
  if (diff2htmlLoading) return diff2htmlLoading;
  diff2htmlLoading = new Promise((resolve, reject) => {
    const css = el('link', { rel: 'stylesheet', href: '/static/vendor/diff2html.min.css' });
    document.head.append(css);
    const s = el('script', { src: '/static/vendor/diff2html-ui-base.min.js' });
    s.onload = () => resolve(); s.onerror = () => reject(new Error('could not load diff2html'));
    document.head.append(s);
  });
  return diff2htmlLoading;
}

function openTaskModal(t) {
  ui.modal = true;
  const m = $('#modal');
  m.textContent = '';
  const status = el('div', { class: 'dim' });
  const diffBox = el('div', { class: 'diffbox' });
  const commits = el('div', { class: 'dim' });
  const files = el('div', { class: 'dim' });
  const tabs = el('div', { class: 'row' });
  const title = el('input', { type: 'text', placeholder: 'PR title', maxlength: 250, value: t.title });
  const body = el('textarea', { placeholder: 'PR body (Markdown)' });
  let diff = null;
  const show = (which) => {
    if (!diff) return;
    const text = which === 'uncommitted' ? diff.uncommitted : diff.committed;
    diffBox.textContent = '';
    if (!text) { diffBox.append(el('span', { class: 'dim', text: which === 'uncommitted' ? 'no uncommitted changes' : 'nothing committed on this branch yet' })); return; }
    loadDiff2Html().then(() => {
      new window.Diff2HtmlUI(diffBox, text, { drawFileList: false, matching: 'lines', outputFormat: 'line-by-line', highlight: false }).draw();
    }).catch(e => { diffBox.append(el('pre', { class: 'tail', text: text.slice(0, 20000) })); status.textContent = e.message; });
  };
  const load = async () => {
    status.textContent = 'loading diff…';
    try {
      diff = await api('GET', `/api/tasks/${t.id}/diff`);
      status.textContent = diff.truncated ? 'diff truncated for display' : '';
      commits.textContent = diff.commits.length ? `Commits (${diff.commits.length}): ` + diff.commits.slice(0, 20).join(' · ') : 'No commits on the branch yet.';
      files.textContent = (diff.files.length ? `Files: ${diff.files.join(', ')}` : '') + (diff.files_uncommitted.length ? `  ·  uncommitted: ${diff.files_uncommitted.join(', ')}` : '');
      tabs.textContent = '';
      tabs.append(el('button', { onclick: () => show('committed'), text: `Committed vs ${diff.base}` }),
        el('button', { onclick: () => show('uncommitted'), text: `Uncommitted (${diff.files_uncommitted.length})` }));
      show('committed');
    } catch (e) { status.textContent = e.message; }
  };
  const describeBtn = el('button', { onclick: async () => {
    status.textContent = 'asking Claude for a title and description (claude -p, one turn)…'; describeBtn.disabled = true;
    try { const r = await api('POST', `/api/tasks/${t.id}/describe`); title.value = r.title; body.value = r.body; status.textContent = 'description ready; edit and create the PR'; }
    catch (e) { status.textContent = e.message; } finally { describeBtn.disabled = false; }
  }, text: 'Describe with Claude' });
  const prBtn = el('button', { class: 'primary', onclick: async () => {
    status.textContent = 'pushing and creating the PR…'; prBtn.disabled = true;
    try { const r = await api('POST', `/api/tasks/${t.id}/pr`, { title: title.value.trim(), body: body.value }); status.textContent = (r.existing ? 'PR already existed: ' : 'PR created: ') + r.url; t.pr_url = r.url; t.pr_number = r.number; await poll(true); render(true); }
    catch (e) { status.textContent = e.message; } finally { prBtn.disabled = false; }
  }, text: t.pr_url ? 'Update PR (recreate)' : 'Create PR' });
  const mergeBtn = el('button', { class: 'danger', onclick: async () => {
    status.textContent = 'merging…';
    const run = async (force) => api('POST', `/api/tasks/${t.id}/merge`, { method: 'squash', force });
    try { await run(false); status.textContent = 'merged and archived'; closeModal(); await poll(true); }
    catch (e) {
      if (/uncommitted/.test(e.message) && window.confirm(e.message + '\n\nDiscard them and merge?')) { try { await run(true); closeModal(); await poll(true); } catch (e2) { status.textContent = e2.message; } }
      else status.textContent = e.message;
    }
  }, text: 'Merge (squash) & archive' });
  m.append(el('div', { class: 'modal-box wide' },
    el('div', { class: 'row head' }, el('h2', { text: t.title }), el('span', { class: 'dim', text: `${t.project}/${t.repo} · ${t.branch}` }),
      t.pr_url ? el('a', { class: 'btn', href: t.pr_url, target: '_blank', rel: 'noopener', text: `PR #${t.pr_number}` }) : null),
    commits, files, tabs, diffBox,
    el('div', { class: 'form' }, el('label', { text: 'Pull request' }), title, body,
      el('div', { class: 'row' }, describeBtn, prBtn, t.pr_number ? mergeBtn : null)),
    status,
    el('div', { class: 'row' }, el('button', { onclick: closeModal, text: 'Close' }))));
  m.classList.remove('hidden');
  load();
}

function renderTasks() {
  const sec = $('#tasks');
  if (!sec) return;
  sec.textContent = '';
  const list = (state.tasks || []);
  if (!list.length) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  sec.append(el('div', { class: 'row head' }, el('h2', { text: `Tasks (${list.length})` }), el('span', { class: 'dim', text: 'one worktree + branch per task; columns follow the session state' })));
  const grid = el('div', { class: 'kanban' });
  for (const [key, label] of COLUMNS) {
    const items = list.filter(t => t.column === key);
    if (!items.length && key !== 'in_progress') continue;
    const col = el('div', { class: 'col' }, el('h3', { text: `${label} (${items.length})` }));
    for (const t of items) col.append(taskCard(t));
    grid.append(col);
  }
  sec.append(grid);
}

function taskForm(p, r) {
  const saved = loadPrefs(TASK_KEY(p, r));
  const title = el('input', { type: 'text', placeholder: 'task title (becomes the branch name)', maxlength: 120, required: true });
  const prompt = el('textarea', { placeholder: 'what Claude should do in the new worktree…', required: true });
  const issueSel = selectEl([['', 'from a GitHub issue…']]);
  let issues = [];
  issueSel.addEventListener('focus', async () => {
    if (issues.length) return;
    try {
      const res = await api('GET', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/issues`);
      issues = res.issues;
      for (const i of issues) issueSel.append(el('option', { value: String(i.number), text: `#${i.number} ${i.title}`.slice(0, 90) }));
      if (!issues.length) issueSel.append(el('option', { value: '', text: '(no open issues)' }));
    } catch (e) { issueSel.append(el('option', { value: '', text: e.message.slice(0, 80) })); }
  }, { once: true });
  issueSel.addEventListener('change', () => {
    const i = issues.find(x => String(x.number) === issueSel.value);
    if (!i) return;
    title.value = `#${i.number} ${i.title}`.slice(0, 120);
    prompt.value = `${i.title}\n\n${i.body || ''}\n\nGitHub issue: ${i.url}\nWhen done, commit with a message that includes "Closes #${i.number}".`;
  });
  const lc = launchControls(saved, PERMS);
  const args = el('input', { type: 'text', placeholder: 'extra claude args (optional)', value: saved.args || '' });
  const siblings = allRepos().filter(x => x.project === p.name && x.repo !== r.name);
  const boxes = [];
  const checks = el('div', { class: 'checks' });
  for (const x of siblings) { const cb = el('input', { type: 'checkbox', value: x.id, checked: false }); boxes.push(cb); checks.append(el('label', {}, cb, x.id)); }
  return el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = { title: title.value.trim(), prompt: prompt.value.trim(), add_dirs: boxes.filter(b => b.checked).map(b => b.value), ...lc.read() };
    if (args.value.trim()) body.args = args.value.trim();
    for (const k of Object.keys(body)) if (body[k] === '' || body[k] === null) delete body[k];
    savePrefs(TASK_KEY(p, r), { ...lc.prefs(), args: args.value.trim() });
    const tab = window.open('', '_blank');
    try {
      const res = await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/tasks`, body);
      if (tab) tab.location = res.attach_url;
      ui.openForm = null; setError(null); await poll(true);
    } catch (err) { if (tab) tab.close(); setError(err.message); }
  } },
    el('label', { text: 'New task: Claude works on a branch in its own worktree (claude --worktree)' }),
    issueSel, title, prompt,
    lc.grid,
    el('details', {}, el('summary', { text: 'More options: extra args, other repos' }),
      field('extra args', args),
      siblings.length ? field('also give access to (--add-dir)', checks) : null),
    el('div', { class: 'submit' },
      el('button', { class: 'primary', type: 'submit' }, ic('git-branch'), 'Start task & open terminal'),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
}

/* ---------- transcript search ---------- */

async function runSearch(q) {
  const sec = $('#search');
  if (!sec) return;
  q = (q || '').trim();
  if (!q) { sec.classList.add('hidden'); return; }
  sec.textContent = '';
  sec.classList.remove('hidden');
  sec.append(el('div', { class: 'row head' }, el('h2', { text: `Search: ${q}` }), el('button', { onclick: () => sec.classList.add('hidden'), text: 'Close' })));
  try {
    const r = await api('GET', `/api/search?q=${encodeURIComponent(q)}`);
    if (!r.results.length) { sec.append(el('div', { class: 'dim', text: 'no matches' })); return; }
    for (const hit of r.results) {
      const where = hit.project ? `${hit.project}/${hit.repo}` : (hit.cwd || '').split('/').slice(-2).join('/');
      sec.append(el('div', { class: 'sess' },
        el('span', { class: 'state ' + (hit.kind === 'assistant' ? 'done' : ''), text: hit.kind }),
        el('span', { class: 'name', text: where }),
        el('span', { class: 'dim', text: (hit.ts || '').replace('T', ' ').slice(0, 16) }),
        hit.tmux ? el('a', { class: 'btn', href: `/term/${encodeURIComponent(hit.tmux)}`, target: '_blank', rel: 'noopener', text: 'Attach' }) : el('code', { text: hit.session_id.slice(0, 8) }),
        el('div', { class: 'last', text: hit.snippet })));
    }
  } catch (e) { sec.append(el('div', { class: 'bad', text: e.message })); }
}

/* ---------- schedules (headless claude -p runs) ---------- */

function jobForm(p, r) {
  const name = el('input', { type: 'text', placeholder: 'name (e.g. nightly-tests)', maxlength: 80, required: true });
  const prompt = el('textarea', { placeholder: 'prompt for the headless run (claude -p in a fresh worktree)…', required: true });
  const cron = el('input', { type: 'text', placeholder: 'cron, e.g. 30 2 * * * (blank = run once now)' });
  const mode = el('select', {}, ...['acceptEdits', 'default', 'plan', 'auto', 'dontAsk'].map(m => el('option', { value: m, text: m })));
  const turns = el('input', { type: 'number', value: '30', min: '1', max: '500', title: 'max turns' });
  const budget = el('input', { type: 'number', placeholder: 'max $ (optional)', step: '0.5', min: '0' });
  const args = el('input', { type: 'text', placeholder: 'extra claude args (optional)' });
  return el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = { name: name.value.trim(), prompt: prompt.value.trim(), permission_mode: mode.value, max_turns: parseInt(turns.value, 10) || 30, run_now: !cron.value.trim() };
    if (cron.value.trim()) body.cron = cron.value.trim();
    if (budget.value) body.max_budget_usd = parseFloat(budget.value);
    if (args.value.trim()) body.args = args.value.trim();
    try { await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/jobs`, body); ui.openForm = null; setError(null); await poll(true); }
    catch (err) { setError(err.message); }
  } },
    el('label', { text: 'Schedule a headless run: claude -p in a fresh worktree; the result becomes a task card' }),
    el('div', { class: 'row' }, name, cron),
    prompt,
    el('div', { class: 'row' }, el('label', { text: 'permission mode' }), mode, el('label', { text: 'max turns' }), turns, budget),
    args,
    el('div', { class: 'row' },
      el('button', { class: 'primary', type: 'submit', text: cron.value ? 'Schedule' : 'Schedule / run' }),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
}

function fmtTs(s) { return s ? s.replace('T', ' ').slice(0, 16) : ''; }

function renderJobs() {
  const sec = $('#jobs');
  if (!sec) return;
  sec.textContent = '';
  const jobs = state.jobs || [];
  const runs = state.runs || [];
  if (!jobs.length && !runs.length) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  const q = state.scheduler || {};
  const quota = q.backoff_until ? ` · backing off until ${fmtTs(q.backoff_until)} after a rate-limited run`
    : q.known ? ` · 5h window at ${Math.round(q.pct)}%` : ' · quota unknown until an interactive session reports the 5-hour window (runs are not deferred)';
  const c = state.claude || {};
  const login = c.installed && !c.loggedIn ? ' · Claude is not logged in on this box: runs are deferred until you Log in (headless runs use the same subscription login)' : '';
  sec.append(el('div', { class: 'row head' }, el('h2', { text: `Schedules (${jobs.length})` }),
    el('span', { class: 'hint', text: 'headless claude -p runs · max 2 at once · paused above 85% of the 5-hour window' + quota }),
    login ? el('span', { class: 'dim', text: login.slice(3) }) : null));
  const batches = {};
  for (const j of jobs) if (j.batch_id) (batches[j.batch_id] = batches[j.batch_id] || []).push(j);
  for (const [bid, js] of Object.entries(batches)) {
    const done = js.filter(j => j.last_status && !j.enabled).length;
    const ok = js.filter(j => j.last_status === 'ok').length;
    sec.append(el('div', { class: 'dim', text: `batch ${bid}: ${js.length} repos · ${done} finished · ${ok} ok · ${js.filter(j => j.enabled).length} waiting` }));
  }
  for (const j of jobs) {
    const jr = runs.filter(r => r.job_id === j.id).slice(0, 3);
    const row = el('div', { class: 'sess' + (j.enabled ? '' : ' muted') },
      el('div', { class: 'main' },
        el('span', { class: 'name', text: j.name }),
        el('span', { class: j.enabled && j.next_run_at ? 'state' : 'state ended', text: j.enabled && j.next_run_at ? 'next ' + fmtTs(j.next_run_at) : 'disabled' }),
        el('span', { class: 'meta', text: `${j.project}/${j.repo} · ${j.cron ? 'cron ' + j.cron : 'one-off'} · ${j.permission_mode} · ≤${j.max_turns} turns${j.max_budget_usd ? ' · ≤$' + j.max_budget_usd : ''}${j.last_status ? ' · last: ' + j.last_status : ''}` })),
      el('div', { class: 'actions' },
        el('button', { onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/run`); } catch (e) { setError(e.message); } await poll(true); } }, ic('play'), 'Run now'),
        el('button', { onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/toggle`); } catch (e) { setError(e.message); } await poll(true); }, text: j.enabled ? 'Disable' : 'Enable' }),
        confirmButton('job:' + j.id, 'Delete', () => api('DELETE', `/api/jobs/${j.id}`))));
    for (const r of jr) {
      row.append(el('div', { class: 'last' },
        el('span', { class: 'dim', text: `run #${r.id} ${fmtTs(r.started_at)} · ${r.status}${typeof r.cost_usd === 'number' ? ' · $' + r.cost_usd.toFixed(2) : ''}${r.num_turns ? ' · ' + r.num_turns + ' turns' : ''}${r.error ? ' · ' + r.error : ''}` }),
        r.result ? el('span', { text: r.result.slice(0, 300) }) : null,
        r.task_id ? el('span', { class: 'row' },
          el('button', { onclick: async () => { try { const x = await api('POST', `/api/runs/${r.id}/resume`); window.open(x.attach_url, '_blank', 'noopener'); } catch (e) { setError(e.message); } await poll(true); }, text: 'Resume in terminal' }),
          el('span', { class: 'dim', text: `task card: ${r.branch}` })) : null));
    }
    sec.append(row);
  }
}

/* ---------- needs-attention inbox ---------- */

function inboxItems() {
  const items = [];
  const pend = {};
  for (const pr of (state.pending_permissions || [])) pend[pr.tmux_name] = pr;
  for (const p of state.projects) {
    for (const r of repoGroups(p)) for (const s of r.sessions) if (s.needs_attention) items.push({ ...s, project: p.name, repo: r.name, perm: pend[s.tmux] || null });
    for (const s of p.orphan_sessions) if (s.needs_attention) items.push({ ...s, project: p.name, perm: pend[s.tmux] || null });
  }
  return items.sort((a, b) => (a.state_at || '').localeCompare(b.state_at || ''));  // oldest first
}

async function decide(pid, decision) {
  try { await api('POST', `/api/permission/${pid}/${decision}`); setError(null); } catch (e) { setError(e.message); }
  await poll(true);
}

function renderInbox() {
  const sec = $('#inbox');
  if (!sec) return;
  sec.textContent = '';
  const items = inboxItems();
  document.title = (items.length ? `(${items.length}) ` : '') + 'ccboard';
  if (!items.length) { sec.classList.add('hidden'); ui.inboxSel = -1; return; }
  sec.classList.remove('hidden');
  if (ui.inboxSel >= items.length) ui.inboxSel = items.length - 1;
  sec.append(el('div', { class: 'row head' },
    el('h2', { text: `Needs attention (${items.length})` }),
    el('span', { class: 'hint', text: 'j / k move · Enter attach · a ack · y / n allow / deny' })));
  items.forEach((s, i) => {
    const row = el('div', { class: 'inbox-item' + (i === ui.inboxSel ? ' sel' : ''), onclick: () => { ui.inboxSel = i; renderInbox(); } },
      el('div', { class: 'main' },
        el('span', { class: 'name', text: `${s.project}/${s.repo === 'root' ? '📁' : (s.repo || '?')} · ${s.name}` }),
        stateBadge(s)),
      el('div', { class: 'msg', text: (s.perm ? s.perm.summary : (s.last_message || s.last_prompt || '')).slice(0, 200) }),
      el('div', { class: 'actions' },
        s.perm ? el('button', { class: 'primary', onclick: (e) => { e.stopPropagation(); decide(s.perm.id, 'allow'); }, text: 'Allow' }) : null,
        s.perm ? el('button', { class: 'danger', onclick: (e) => { e.stopPropagation(); decide(s.perm.id, 'deny'); }, text: 'Deny' }) : null,
        el('a', { class: 'btn' + (s.perm ? '' : ' primary'), href: `/term/${encodeURIComponent(s.tmux)}`, target: '_blank', rel: 'noopener', text: 'Open terminal' }),
        el('button', { onclick: async (e) => { e.stopPropagation(); try { await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/ack`); } catch (err) { setError(err.message); } await poll(true); }, text: 'Ack' })));
    sec.append(row);
  });
}

async function inboxKey(e) {
  const ae = document.activeElement;
  if (ae && ae.matches('input, select, textarea')) return;
  if (ui.modal) return;
  const items = inboxItems();
  if (!items.length) return;
  if (e.key === 'j' || e.key === 'n') { ui.inboxSel = Math.min(items.length - 1, (ui.inboxSel < 0 ? -1 : ui.inboxSel) + 1); renderInbox(); }
  else if (e.key === 'k' || e.key === 'p') { ui.inboxSel = Math.max(0, ui.inboxSel - 1); renderInbox(); }
  else if (e.key === 'Enter' && ui.inboxSel >= 0) { window.open(`/term/${encodeURIComponent(items[ui.inboxSel].tmux)}`, '_blank', 'noopener'); }
  else if (e.key === 'a' && ui.inboxSel >= 0) {
    e.preventDefault();
    try { await api('POST', `/api/sessions/${encodeURIComponent(items[ui.inboxSel].tmux)}/ack`); } catch (err) { setError(err.message); }
    await poll(true);
  } else if ((e.key === 'y' || e.key === 'n') && ui.inboxSel >= 0 && items[ui.inboxSel].perm) {
    e.preventDefault();
    await decide(items[ui.inboxSel].perm.id, e.key === 'y' ? 'allow' : 'deny');
  } else if (e.key === 'Escape') { ui.inboxSel = -1; renderInbox(); }
  else return;
  const sel = document.querySelector('.inbox-item.sel');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}
document.addEventListener('keydown', inboxKey);

function renderProjects() {
  const sec = $('#projects');
  sec.textContent = '';
  if (!state.projects.length) sec.append(el('div', { class: 'card dim', text: 'No projects yet.' }));
  for (const p of state.projects) sec.append(projectCard(p));
}

/* ---------- login modal ---------- */

async function startLogin() {
  try { await api('POST', '/api/claude/login'); setError(null); openModal(); await poll(true); }
  catch (e) { setError(e.message); }
}

async function logout() {
  try { const r = await api('POST', '/api/claude/logout'); if (!r.ok) setError('logout: ' + (r.output || 'failed')); else setError(null); }
  catch (e) { setError(e.message); }
  await poll(true);
}

const modalParts = {};

function openModal() {
  ui.modal = true;
  const m = $('#modal');
  m.textContent = '';
  const link = el('a', { class: 'btn primary', href: '#', target: '_blank', rel: 'noopener', text: 'Open sign-in page' });
  const urlText = el('div', { class: 'url' });
  const copy = el('button', { onclick: async () => { try { await navigator.clipboard.writeText(modalParts.url || ''); copy.textContent = 'copied'; } catch (_) { copy.textContent = 'copy failed'; } }, text: 'Copy link' });
  const code = el('input', { type: 'text', placeholder: 'paste the whole code from the browser (code#state)', autocomplete: 'off' });
  const send = el('button', { class: 'primary', type: 'submit', text: 'Send code' });
  const form = el('form', { class: 'inline', onsubmit: async (e) => {
    e.preventDefault();
    try { await api('POST', '/api/claude/login/code', { code: code.value.trim() }); code.value = ''; setError(null); status.textContent = 'Code sent. Waiting for Claude to confirm…'; }
    catch (err) { status.textContent = err.message; }
    await poll(true);
  } }, code, send);
  const status = el('div', { class: 'dim' });
  const tail = el('pre', { class: 'tail' });
  Object.assign(modalParts, { link, urlText, code, status, tail, url: null });
  m.append(el('div', { class: 'modal-box' },
    el('h2', { text: 'Log in to Claude Code' }),
    el('ol', {},
      el('li', {}, 'Open the sign-in page and log in with your Anthropic account.'),
      el('li', {}, 'The page shows a code. Copy the whole thing and paste it below.')),
    el('div', { class: 'row' }, link, copy, el('a', { class: 'btn', href: '/tty/?arg=_ccboard-login', target: '_blank', rel: 'noopener', text: 'Open in terminal' })),
    urlText,
    form, status,
    el('details', {}, el('summary', { class: 'dim', text: 'terminal output' }), tail),
    el('div', { class: 'row' }, el('button', { onclick: closeModal, text: 'Close' }))));
  m.classList.remove('hidden');
  updateModal();
}

function updateModal() {
  if (!ui.modal || !modalParts.link || !modalParts.link.isConnected) return;
  const l = state.login || {};
  const c = state.claude || {};
  modalParts.url = l.url;
  if (l.url) { modalParts.link.href = l.url; modalParts.link.classList.remove('muted'); modalParts.urlText.textContent = l.url; }
  else { modalParts.link.href = '#'; modalParts.link.classList.add('muted'); modalParts.urlText.textContent = l.running ? 'waiting for the sign-in link…' : 'login is not running'; }
  modalParts.tail.textContent = (l.tail || []).join('\n');
  if (c.loggedIn) { modalParts.status.textContent = `Logged in as ${c.email || ''}.`; modalParts.code.disabled = true; }
}

function updateModalSafe() { if (modalParts.link && modalParts.link.isConnected) updateModal(); }

function closeModal() { ui.modal = false; $('#modal').classList.add('hidden'); }

/* ---------- polling ---------- */

function applyDeepLink() {
  // ntfy click target: /#s=<tmux name> selects that session in the inbox (or scrolls to its row).
  if (ui.deepLinked) return;
  const m = /^#s=([A-Za-z0-9_-]+)$/.exec(location.hash || '');
  if (!m || !state) return;
  ui.deepLinked = true;
  const items = inboxItems();
  const i = items.findIndex(s => s.tmux === m[1]);
  if (i >= 0) { ui.inboxSel = i; renderInbox(); }
  const row = [...document.querySelectorAll('.sess')].find(r => r.dataset.tmux === m[1]);
  if (row) row.scrollIntoView({ block: 'center' });
}

function renderNav() {
  const nav = $('#bnav');
  if (!nav) return;
  nav.textContent = '';
  nav.classList.remove('hidden');
  const inbox = inboxItems().length;
  const tasks = (state.tasks || []).filter(t => !t.archived_at).length;
  const jobs = (state.jobs || []).length;
  const projects = (state.projects || []).length;
  const items = [
    ['inbox', 'notifications', 'Needs you', inbox, inbox > 0],
    ['tasks', 'git-branch', 'Tasks', tasks, false],
    ['jobs', 'time', 'Schedules', jobs, false],
    ['projects', 'folder-close', 'Projects', projects, false],
  ];
  for (const [id, icon, label, cnt, attn] of items) {
    nav.append(el('button', { class: 'minimal' + (attn ? ' attn' : ''), onclick: () => { const t = $('#' + id); if (t) { t.classList.remove('hidden'); t.scrollIntoView({ behavior: 'smooth', block: 'start' }); } } },
      ic(icon), el('span', { class: 'lbl' }, el('span', { class: 'cnt', text: cnt ? String(cnt) + ' ' : '' }), label)));
  }
}

function refreshNow() {
  if (Date.now() - (ui.lastPollAt || 0) < 500) return;   // several lifecycle events fire together
  clearTimeout(pollTimer);
  if ('serviceWorker' in navigator) navigator.serviceWorker.getRegistration().then(r => r && r.update()).catch(() => { /* ignore */ });
  poll(true);
}
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshNow(); });
window.addEventListener('pageshow', (e) => { if (e.persisted) refreshNow(); });
window.addEventListener('focus', () => refreshNow());
window.addEventListener('online', () => refreshNow());

function render(force) {
  renderHeader();
  renderNav();
  renderUsage();
  renderNodes();
  renderBanner();
  renderInbox();
  renderTasks();
  renderJobs();
  renderNotifyPanel();
  renderNewProject();
  renderQueue();
  const ae = document.activeElement;
  const typing = !!(ae && ae.closest('#projects') && ae.matches('input, select, textarea'));
  // A forced poll follows a user action: always redraw (a focused button must not block it).
  // A background poll leaves an open form or a field being typed in alone.
  if (force ? !typing : (!ui.openForm && !typing)) renderProjects();
  updateModal();
  applyDeepLink();
}

async function poll(force) {
  ui.lastPollAt = Date.now();
  try {
    const s = await api('GET', '/api/state');
    if (s.version && ui.version && s.version !== ui.version) {
      // the box was updated while this page stayed open (an installed PWA restored from memory never navigates)
      try { sessionStorage.setItem('ccboard:reloaded', '1'); } catch (_) { /* ignore */ }
      location.reload();
      return;
    }
    if (s.version) ui.version = s.version;
    const j = JSON.stringify(s);
    const changed = j !== ui.lastJson;
    ui.lastJson = j;
    state = s;
    ui.offline = false;
    if (changed || force) render(force);
    else { renderHeader(); renderUsage(); updateModal(); }
    rememberState(s);
  } catch (e) {
    if (!state) {
      const last = recallState();
      if (last) { state = last.state; ui.offline = last.at; render(true); }
      else { $('#banner').textContent = 'Cannot reach ccboard: ' + e.message; }
    } else { ui.offline = ui.offline || Date.now(); renderBanner(); }
  }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => poll(false), ui.modal ? 2000 : 3000);
}

poll(true);
