/* ccboard frontend: one render(state) fed by polling. Dynamic text goes through textContent only. */
'use strict';

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    n.append(typeof c === 'string' ? document.createTextNode(c) : c);
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

const ui = { openForm: null, confirm: null, error: null, modal: false, lastJson: null, inboxSel: -1, notifyPanel: false, deepLinked: false };
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
  const chips = [];
  for (const p of state.projects) for (const r of p.repos) for (const s of r.sessions) {
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
  navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => { /* no SW: the board still works */ });
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
  const ntfyRow = el('div', { class: 'row' }, el('b', { text: 'ntfy app:' }));
  p.append(ntfyRow);
  if (!n.enabled) {
    ntfyRow.append(el('span', { class: 'dim', text: 'not configured on the box (NTFY_URL in /etc/ccboard/env, or rerun install.sh).' }));
    return;
  }
  ntfyRow.append(el('span', { text: 'subscribe to ' }), el('code', { text: n.subscribe_url || '' }),
    el('button', { onclick: async () => { try { await navigator.clipboard.writeText(n.subscribe_url || ''); } catch (_) { /* ignore */ } }, text: 'Copy' }),
    el('button', { onclick: async () => { try { const r = await api('POST', '/api/notify/test'); setError(r.ok ? null : 'ntfy publish failed (is ntfy running?)'); } catch (e) { setError(e.message); } }, text: 'Send test' }),
    el('span', { class: 'dim', text: 'Pushes on needs-you, done, error and rate limit; “Terminal” opens the session, “Ack” clears it.' }));
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
  actions.append(el('span', { class: 'dim', text: state.user || '' }));
  if (c.installed && !c.loggedIn) actions.append(el('button', { class: 'primary', onclick: startLogin, text: 'Log in' }));
  if (c.installed && c.loggedIn) actions.append(el('button', { onclick: logout, text: 'Log out' }));
  const n = state.config && state.config.ntfy;
  actions.append(el('button', { onclick: () => { ui.notifyPanel = !ui.notifyPanel; renderNotifyPanel(); }, title: 'push notifications', text: n && n.enabled ? '🔔' : '🔕' }));
  actions.append(el('button', { class: live.on ? 'primary' : '', onclick: toggleLive, title: 'live last lines of every session', text: 'Live' }));
  if (state.login && state.login.running && !ui.modal) actions.append(el('button', { onclick: () => openModal(), text: 'Login in progress…' }));
}

function renderBanner() {
  const b = $('#banner');
  b.textContent = '';
  b.className = '';
  const rlim = state && state.rate_limited && state.rate_limited.value;
  const recent = rlim && state.rate_limited.at && (Date.now() - Date.parse(state.rate_limited.at)) < 5 * 3600 * 1000;
  if (ui.offline) {
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
    el('div', { class: 'row', id: 'importrow' }, el('button', { onclick: openImport, text: 'Import from GitHub…' }), el('span', { id: 'queue', class: 'dim' })));
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

function confirmButton(key, label, action) {
  if (ui.confirm === key) {
    return el('span', { class: 'row' },
      el('button', { class: 'danger confirm', onclick: async () => { ui.confirm = null; try { await action(); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: `Confirm ${label}` }),
      el('button', { onclick: () => { ui.confirm = null; renderProjects(); }, text: 'Cancel' }));
  }
  return el('button', { class: 'danger', onclick: () => { ui.confirm = key; renderProjects(); }, text: label });
}

function allRepos() {
  const out = [];
  for (const p of state.projects) for (const r of p.repos) if (r.state === 'ok' || r.state === 'unknown') out.push({ id: `${p.name}/${r.name}`, project: p.name, repo: r.name });
  return out;
}

function sessionForm(p, r) {
  const launcher = el('select', {},
    el('option', { value: 'claude', text: 'claude (new session)' }),
    el('option', { value: 'resume', text: 'claude --resume' }),
    el('option', { value: 'continue', text: 'claude --continue' }),
    el('option', { value: 'shell', text: 'shell' }));
  const name = el('input', { type: 'text', placeholder: 'session name (auto: s1, s2…)', maxlength: 64 });
  const args = el('input', { type: 'text', placeholder: 'extra args, e.g. --permission-mode acceptEdits --model opus' });
  const resumeId = el('input', { type: 'text', placeholder: 'session id to resume (blank = picker)', class: 'hidden' });
  launcher.addEventListener('change', () => resumeId.classList.toggle('hidden', launcher.value !== 'resume'));
  const siblings = allRepos().filter(x => x.project === p.name && x.repo !== r.name);
  const others = allRepos().filter(x => x.project !== p.name);
  const checks = el('div', { class: 'checks' });
  const boxes = [];
  const mk = (x, checked) => { const cb = el('input', { type: 'checkbox', value: x.id, checked }); boxes.push(cb); return el('label', { class: 'row' }, cb, x.id); };
  for (const x of siblings) checks.append(mk(x, true));
  const otherBox = el('div', { class: 'checks' });
  for (const x of others) otherBox.append(mk(x, false));
  const form = el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = { launcher: launcher.value };
    if (name.value.trim()) body.name = name.value.trim();
    if (args.value.trim()) body.args = args.value.trim();
    if (launcher.value === 'resume' && resumeId.value.trim()) body.resume_id = resumeId.value.trim();
    body.add_dirs = boxes.filter(b => b.checked).map(b => b.value);
    const tab = window.open('', '_blank');
    try {
      const res = await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/sessions`, body);
      if (tab) tab.location = `/term/${encodeURIComponent(res.tmux)}`;
      ui.openForm = null; setError(null); await poll(true);
    } catch (err) { if (tab) tab.close(); setError(err.message); }
  } },
    el('div', { class: 'row' }, launcher, name),
    el('label', { text: 'extra args' }), args,
    resumeId,
    siblings.length ? el('label', { text: 'also give access to (--add-dir)' }) : null, checks,
    others.length ? el('details', {}, el('summary', { class: 'dim', text: 'repos of other projects' }), otherBox) : null,
    el('div', { class: 'row' },
      el('button', { class: 'primary', type: 'submit', text: 'Start & attach' }),
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
    el('span', { class: 'name', text: s.name }),
    stateBadge(s),
    el('span', { class: 'dim', text: s.launcher }),
    el('code', { text: s.command || '' }),
    el('span', { class: 'dim', text: statsText(s) }),
    el('span', { class: 'dim', text: `${fmtAge(s.created)} · ${s.attached} attached` }),
    el('a', { class: 'btn', href: `/term/${encodeURIComponent(s.tmux)}`, target: '_blank', rel: 'noopener', text: 'Attach' }),
    s.needs_attention ? el('button', { onclick: async () => { try { await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/ack`); } catch (e) { setError(e.message); } await poll(true); }, text: 'Ack' }) : null,
    el('button', { class: 'danger', onclick: async () => { try { await api('DELETE', `/api/sessions/${encodeURIComponent(s.tmux)}`); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: 'Kill' }));
  if (s.last_message || s.last_prompt) {
    row.append(el('div', { class: 'last' },
      s.last_prompt ? el('span', { class: 'dim', text: '› ' + s.last_prompt.slice(0, 120) }) : null,
      s.last_message ? el('span', { text: s.last_message.slice(0, 160) }) : null));
  }
  return row;
}

function repoBlock(p, r) {
  const key = `${p.name}/${r.name}`;
  const title = el('div', { class: 'row head' },
    el('div', { class: 'row' },
      el('span', { class: 'title', text: r.name }),
      r.state === 'ok' ? [el('span', { class: 'dim', text: r.branch || '' }), el('span', { class: 'dot' + (r.dirty ? ' dirty' : ''), title: r.dirty ? 'uncommitted changes' : 'clean' })] : null,
      r.state === 'cloning' ? el('span', { class: 'badge warn', text: 'cloning…' }) : null,
      r.state === 'clone-failed' ? el('span', { class: 'badge bad', text: 'clone failed (attach to see the error)' }) : null,
      r.state === 'nogit' ? el('span', { class: 'badge bad', text: 'no git' }) : null,
      r.state === 'unknown' ? el('span', { class: 'dot unknown', title: 'git status unknown' }) : null),
    el('div', { class: 'row' },
      el('a', { class: 'btn', href: codeServerUrl(r.path), target: '_blank', rel: 'noopener', text: 'code-server' }),
      el('button', { class: 'primary', onclick: () => { ui.openForm = 'session:' + key; renderProjects(); }, text: 'New session' }),
      confirmButton('rm:' + key, 'Remove', () => api('DELETE', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}`))));
  const block = el('div', { class: 'repo' }, title);
  if (ui.openForm === 'session:' + key) block.append(sessionForm(p, r));
  for (const s of r.sessions) block.append(sessionRow(s));
  return block;
}

function projectCard(p) {
  const nSess = p.repos.reduce((n, r) => n + r.sessions.length, 0) + p.orphan_sessions.length;
  const card = el('div', { class: 'card' },
    el('div', { class: 'row head' },
      el('div', { class: 'row' }, el('h2', { text: p.name }), el('span', { class: 'dim', text: `${p.repos.length} repo${p.repos.length === 1 ? '' : 's'} · ${nSess} session${nSess === 1 ? '' : 's'}` })),
      el('div', { class: 'row' },
        el('a', { class: 'btn', href: codeServerUrl(p.path), target: '_blank', rel: 'noopener', text: 'Open project in code-server' }),
        el('button', { onclick: () => { ui.openForm = 'repo:' + p.name; renderProjects(); }, text: 'Add repo' }),
        confirmButton('del:' + p.name, 'Delete', () => api('DELETE', `/api/projects/${encodeURIComponent(p.name)}`)))));
  if (ui.openForm === 'repo:' + p.name) card.append(addRepoForm(p));
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

/* ---------- needs-attention inbox ---------- */

function inboxItems() {
  const items = [];
  const pend = {};
  for (const pr of (state.pending_permissions || [])) pend[pr.tmux_name] = pr;
  for (const p of state.projects) {
    for (const r of p.repos) for (const s of r.sessions) if (s.needs_attention) items.push({ ...s, project: p.name, repo: r.name, perm: pend[s.tmux] || null });
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
    el('span', { class: 'dim', text: 'j / k move · Enter attach · a ack · y / n allow / deny' })));
  items.forEach((s, i) => {
    const row = el('div', { class: 'inbox-item' + (i === ui.inboxSel ? ' sel' : ''), onclick: () => { ui.inboxSel = i; renderInbox(); } },
      el('span', { class: 'name', text: `${s.project}/${s.repo || '?'} · ${s.name}` }),
      stateBadge(s),
      el('span', { class: 'msg', text: (s.perm ? s.perm.summary : (s.last_message || s.last_prompt || '')).slice(0, 140) }),
      s.perm ? el('button', { class: 'primary', onclick: (e) => { e.stopPropagation(); decide(s.perm.id, 'allow'); }, text: 'Allow (y)' }) : null,
      s.perm ? el('button', { class: 'danger', onclick: (e) => { e.stopPropagation(); decide(s.perm.id, 'deny'); }, text: 'Deny (n)' }) : null,
      el('a', { class: 'btn', href: `/term/${encodeURIComponent(s.tmux)}`, target: '_blank', rel: 'noopener', text: 'Attach' }),
      el('button', { onclick: async (e) => { e.stopPropagation(); try { await api('POST', `/api/sessions/${encodeURIComponent(s.tmux)}/ack`); } catch (err) { setError(err.message); } await poll(true); }, text: 'Ack' }));
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

function render(force) {
  renderHeader();
  renderUsage();
  renderBanner();
  renderInbox();
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
  try {
    const s = await api('GET', '/api/state');
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
