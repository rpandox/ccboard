/* ccboard home page (v0.4 board rehomed): header, strips, banner, inbox, tasks, schedules, projects, login modal.
   render(force) is the state consumer that core.js's poll() calls. */
'use strict';

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
      el('button', { onclick: async () => { try { await api('POST', '/api/usage/rate-limit/clear'); } catch (e) { setError(e.message); } await poll(true); }, text: 'dismiss' }));
  } else if (state && state.tmux_down) {
    b.append(el('span', { text: 'The ccboard tmux server is not running. On the box: sudo systemctl start ccboard-tmux' }));
  } else if (state && state.claude && state.claude.installed && !state.claude.loggedIn) {
    b.className = 'warn';
    b.append(el('span', { text: 'Claude Code is not logged in on this box.' }), el('button', { class: 'primary', onclick: startLogin, text: 'Log in' }));
  }
}

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
          el('button', { onclick: async () => { try { const x = await api('POST', `/api/runs/${r.id}/resume`); openPage(x.attach_url); } catch (e) { setError(e.message); } await poll(true); }, text: 'Resume in terminal' }),
          el('span', { class: 'dim', text: `task card: ${r.branch}` })) : null));
    }
    sec.append(row);
  }
}

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
  else if (e.key === 'Enter' && ui.inboxSel >= 0) { openPage(`/term/${encodeURIComponent(items[ui.inboxSel].tmux)}`); }
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
