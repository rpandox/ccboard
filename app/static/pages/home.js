/* ccboard home page (the v0.4 board rehomed into #page): the needs-attention list, the live grid, the tasks kanban, schedules,
   the new-project form and the project cards, built by mount() with the ids the legacy renderers write into and patched by
   update(). Also home to the banner and the login modal (startLogin / logout / openModal / updateModal), which every page uses.
   The header, usage strip, nav and the render() state consumer moved to shell.js; the notify panel and nodes strip to
   pages/settings.js; transcript search to pages/search.js. */
'use strict';

function renderBanner() {
  const b = $('#banner');
  if (!b) return;
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
  if (!state) return items;
  const pend = {};
  for (const pr of (state.pending_permissions || [])) pend[pr.tmux_name] = pr;
  for (const p of state.projects) {
    for (const r of repoGroups(p)) for (const s of r.sessions) if (s.needs_attention) items.push({ ...s, project: p.name, repo: r.name, perm: pend[s.tmux] || null });
    for (const s of p.orphan_sessions) if (s.needs_attention) items.push({ ...s, project: p.name, perm: pend[s.tmux] || null });
  }
  return items.sort((a, b) => (a.state_at || '').localeCompare(b.state_at || ''));  // oldest first
}

/* ---------- the keyboard selection and the actions on it (keymap.js drives these; palette.js reads them) ----------
   One selection for every list of sessions: the legacy Needs-attention section on Home, the inbox page and the Agents roster. The index
   stays in ui.inboxSel (a click on a card sets it too); Pages remembers the tmux name beside it so a poll that reorders the list keeps
   the same session selected, and the 'sel' class is painted on every row or card of that session. Only the list on screen answers:
   on any other page items() is empty and j / k are not handled. */
const Pages = {
  selTmux: null,            // the selected session's tmux name (the index alone would point at another session after a reorder)
  selHash: '',              // the route the selection was made on: with a peek open, a selection made before it opened does not beat the peek
  retry: null,
};

Pages.hashNow = function () { return (typeof location !== 'undefined' && location.hash) || '#/'; };

Pages.listId = function () {
  const id = typeof mountedId !== 'undefined' && mountedId ? mountedId : (typeof currentRoute === 'function' && currentRoute() ? currentRoute().id : '');
  return id === 'agents' || id === 'inbox' || id === 'home' ? id : '';
};

/* Every live session in the order the Agents page shows them (blocked first, grouped by project): mod+1..9 and the palette count in it. */
Pages.order = function () {
  if (typeof state === 'undefined' || !state) return [];
  return agentsGroups(rosterSessions(state)).flatMap((g) => g.items);
};

/* The sessions the selection can walk on this page, in screen order ([] where the page has no session list). */
Pages.items = function () {
  if (typeof state === 'undefined' || !state) return [];
  const id = Pages.listId();
  if (id === 'agents') return Pages.order();
  if (id === 'inbox' || id === 'home') return inboxItems();
  return [];
};

Pages.active = function () { return Pages.items().length > 0; };

/* Re-find the selection in the current list: by tmux name when it is still there, else by its index (clamped), else none. */
Pages.sync = function () {
  const items = Pages.items();
  if (!items.length) { ui.inboxSel = -1; Pages.selTmux = null; return items; }
  const at = Pages.selTmux ? items.findIndex((x) => x.tmux === Pages.selTmux) : -1;
  if (at >= 0) ui.inboxSel = at;
  else if (ui.inboxSel >= 0) { ui.inboxSel = Math.min(ui.inboxSel, items.length - 1); Pages.selTmux = items[ui.inboxSel].tmux; }
  else Pages.selTmux = null;
  return items;
};

Pages.selected = function () {
  const items = Pages.items();
  return ui.inboxSel >= 0 && ui.inboxSel < items.length ? items[ui.inboxSel] : null;
};

Pages.rowNodes = function () {
  try { return Array.from(document.querySelectorAll('#page .rrow, #page .inbox-item')); } catch (_) { return []; }
};

Pages.nodeFor = function (tmux) { return tmux ? (Pages.rowNodes().find((n) => n.getAttribute('data-tmux') === tmux) || null) : null; };

Pages.paint = function () {
  const tmux = Pages.selected() ? Pages.selTmux : null;
  for (const n of Pages.rowNodes()) n.classList.toggle('sel', !!tmux && n.getAttribute('data-tmux') === tmux);
};

Pages.setIndex = function (i) {
  const items = Pages.items();
  if (i < 0 || i >= items.length) { ui.inboxSel = -1; Pages.selTmux = null; }
  else { ui.inboxSel = i; Pages.selTmux = items[i].tmux; Pages.selHash = Pages.hashNow(); }
  Pages.paint();
};

/* Move the selection by delta (j = 1, k = -1): none starts at the first row. False when the page has no list, so the key is left alone. */
Pages.select = function (delta) {
  const items = Pages.sync();
  if (!items.length) return false;
  Pages.setIndex(Math.max(0, Math.min(items.length - 1, (ui.inboxSel < 0 ? -1 : ui.inboxSel) + delta)));
  const node = Pages.nodeFor(Pages.selTmux);
  if (node && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' });
  return true;
};

Pages.clear = function () {
  if (ui.inboxSel < 0 && !Pages.selTmux) return;
  ui.inboxSel = -1;
  Pages.selTmux = null;
  Pages.paint();
};

/* A page mounts with nothing selected. */
Pages.reset = function () { ui.inboxSel = -1; Pages.selTmux = null; Pages.selHash = ''; };

Pages.peekTmux = function () {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  return r && r.id === 'session' && r.params ? r.params.tmux : null;
};

/* The session a key or a palette nudge acts on: the selected row, unless a peek was opened after that selection (then the peek), else the peek. */
Pages.target = function () {
  const sel = Pages.selected();
  const peek = Pages.peekTmux();
  if (sel && (!peek || Pages.selHash === Pages.hashNow())) return sel.tmux;
  return peek || null;
};

Pages.session = function (tmux) { return tmux ? (Pages.order().find((x) => x.tmux === tmux) || null) : null; };

Pages.targetPerm = function () {
  const t = Pages.target();
  return t && typeof sessionPerm === 'function' ? sessionPerm(t) : null;
};

/* mod+1..9: open the nth session of Pages.order() as the peek. False (key left alone) when there is no nth session. */
Pages.openNth = function (n) {
  const s = Pages.order()[n - 1];
  if (!s) return false;
  navigate(sessionHash(s.tmux));
  return true;
};

/* The peek's send box (session.js builds it, a composer textarea): the node, or null while no peek is open. */
Pages.sendBox = function () {
  if (typeof sessionPeek !== 'undefined' && sessionPeek.parts && sessionPeek.parts.input && sessionPeek.parts.input.isConnected !== false) return sessionPeek.parts.input;
  try { return document.querySelector('.peek-send textarea, .peek-send input'); } catch (_) { return null; }
};

/* Run fn(box) as soon as the peek's send box exists: now, or within about half a second (a peek opened by the same key press mounts on hashchange). */
Pages.withSendBox = function (fn, tries) {
  const box = Pages.sendBox();
  if (box) { fn(box); return true; }
  const left = typeof tries === 'number' ? tries : 8;
  if (left <= 0 || typeof setTimeout !== 'function') return false;
  Pages.retry = setTimeout(() => Pages.withSendBox(fn, left - 1), 60);
  return true;
};

/* what: peek | term | ack | allow | deny | reply. Returns true when something was done (or started). */
Pages.act = async function (what) {
  if (what === 'peek') {
    const s = Pages.selected();
    if (!s) return false;
    const h = sessionHash(s.tmux);
    Pages.selHash = h;
    navigate(h);
    return true;
  }
  const tmux = Pages.target();
  if (!tmux) return false;
  const s = Pages.session(tmux);
  const name = (s && s.name) || tmux;
  if (what === 'term') { openPage(`/term/${encodeURIComponent(tmux)}`); return true; }
  if (what === 'reply') {
    if (!Pages.peekTmux() || Pages.peekTmux() !== tmux) { const h = sessionHash(tmux); Pages.selHash = h; navigate(h); }
    return Pages.withSendBox((box) => box.focus());
  }
  if (what === 'ack') {
    if (s && !s.needs_attention) { pageToast(`${name} has nothing to acknowledge`, 'info'); return false; }
    await sessionAck(s || { tmux });
    pageToast(`acknowledged ${name}`, 'ok');
    return true;
  }
  if (what === 'allow' || what === 'deny') {
    const pr = typeof sessionPerm === 'function' ? sessionPerm(tmux) : null;
    if (!pr) { pageToast(`no permission is waiting on ${name}`, 'warn'); return false; }
    await decide(pr.id, what);
    pageToast(`${what === 'allow' ? 'allowed' : 'denied'} ${name}`, what === 'allow' ? 'ok' : 'warn');
    return true;
  }
  return false;
};

/* First-paint placeholder: n grey rows (core.js maps 'skeleton' to bp5-skeleton) until the first /api/state arrives; the page's update() drops it. */
Pages.skeleton = function (n) {
  const wrap = el('div', { class: 'skel-rows', 'aria-hidden': 'true' });
  for (let i = 0; i < (n || 3); i++) {
    wrap.append(el('div', { class: 'skel-row' },
      el('span', { class: 'skeleton skel-g', text: '✽ ◆' }),
      el('span', { class: 'skeleton skel-main', text: 'project/repo · session name' }),
      el('span', { class: 'skeleton skel-meta', text: 'working · model · ctx 42%' })));
  }
  return wrap;
};

Pages.dropSkeleton = function () {
  let nodes = [];
  try { nodes = Array.from(document.querySelectorAll('#page .skel-rows')); } catch (_) { return; }
  for (const n of nodes) n.remove();
};

async function decide(pid, decision) {
  try { await api('POST', `/api/permission/${pid}/${decision}`); setError(null); } catch (e) { setError(e.message); }
  await poll(true);
}

const INBOX_HINT = 'j / k move · Enter open · o terminal · a ack · y allow · d deny · r reply · ? all keys';

function renderInbox() {
  const sec = $('#inbox');
  if (!sec) { repaintPage(); return; }            // the board is not mounted: the inbox page owns the list
  sec.textContent = '';
  const items = Pages.sync();
  if (!items.length) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  sec.append(el('div', { class: 'row head' },
    el('h2', { text: `Needs attention (${items.length})` }),
    el('span', { class: 'hint', text: INBOX_HINT })));
  items.forEach((s, i) => {
    const row = el('div', { class: 'inbox-item' + (i === ui.inboxSel ? ' sel' : ''), 'data-tmux': s.tmux, onclick: () => { Pages.setIndex(i); } },
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

const homeConfirm = { painted: null };

function renderProjects() {
  const sec = $('#projects');
  // confirmButton() calls this after its first tap and on Cancel: off the board there is no #projects, repaint the page instead
  if (!sec) { repaintPage(); return; }
  // the tasks and schedules sections hold confirm buttons too (Archive, Delete): repaint them when the two-tap state flipped
  if (homeConfirm.painted !== ui.confirm) { homeConfirm.painted = ui.confirm; renderTasks(); renderJobs(); }
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


/* ---------- the Home page ---------- */

function homeTitle() {
  const n = state ? inboxItems().length : 0;
  return (n ? `(${n}) ` : '') + 'Home';
}

function homeLiveButton() {
  return el('button', { type: 'button', class: live.on ? 'primary' : '', title: 'live last lines of every session', onclick: homeToggleLive }, ic('pulse'), 'Live');
}

function homeToggleLive() {
  try { toggleLive(); } catch (e) { console.error('ccboard live', e); }   // toggleLive() also asks the shell to repaint its header
  const slot = $('#home-live');
  if (!slot) return;
  slot.textContent = '';
  const b = homeLiveButton();
  slot.append(b);
  b.focus();
}

/* The sections carry the ids the legacy renderers write into: #inbox #live #tasks #jobs #new-project #projects, and
   renderNewProject() adds #importrow and #queue inside #new-project. */
/* One-time hint to install the board as an app (shown in a browser tab only, until dismissed: ccboard:hint:install). */
const INSTALL_HINT_KEY = 'ccboard:hint:install';
function homeInstallHint() {
  let seen = null, standalone = true;
  try { seen = localStorage.getItem(INSTALL_HINT_KEY); } catch (_) { seen = null; }
  try { standalone = isStandalone(); } catch (_) { standalone = true; }
  if (seen || standalone) return null;
  let box = null;
  const dismiss = () => { try { localStorage.setItem(INSTALL_HINT_KEY, '1'); } catch (_) { /* storage may be unavailable */ } if (box && box.remove) box.remove(); };
  const canPrompt = typeof Shell !== 'undefined' && Shell && Shell.installPrompt && typeof Shell.promptInstall === 'function';
  box = el('div', { class: 'callout primary install-hint', role: 'note' },           // el() drops the null child; a raw append(null) would render "null"
    el('span', { class: 'ih-text', text: 'Install ccboard as an app: its own window, keyboard shortcuts, the title bar on desktop.' }),
    canPrompt ? el('button', { class: 'small primary', type: 'button', text: 'Install', onclick: () => { Shell.promptInstall(); dismiss(); } }) : null,
    el('a', { class: 'btn small', href: '#/settings?sec=app', text: 'How' }),
    el('button', { class: 'minimal small', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: dismiss }, ic('cross')));
  return box;
}

registerPage('home', {
  title: () => homeTitle(),
  mount(root) {
    const section = (id, cls) => el('section', { id, class: cls });
    Pages.reset();
    root.append(
      el('div', { class: 'page-head' }, el('h1', { text: 'Home' }), el('div', { class: 'actions', id: 'home-live' }, homeLiveButton())),
      homeInstallHint(),
      ...(currentState() ? [] : [Pages.skeleton(3)]),                                       // first paint: rows until /api/state arrives
      section('inbox', 'card inbox hidden'), section('live', 'card hidden'), section('tasks', 'card hidden'), section('jobs', 'card hidden'),
      section('new-project', 'card'), section('projects', ''));
    if (live.on) { try { liveStart(); } catch (e) { console.error('ccboard live', e); } }   // main.js only restores the flag
  },
  update() {
    Pages.dropSkeleton();
    renderInbox();
    renderTasks();
    renderJobs();
    renderNewProject();
    renderQueue();
    const sec = $('#projects');
    if (!sec) return;
    const ae = document.activeElement;
    const typing = !!(ae && ae.closest && ae.closest('#projects') && ae.matches('input, select, textarea'));
    // a background poll leaves an open form and a field being typed in alone; the first paint always happens
    if (!sec.childElementCount || (!ui.openForm && !typing)) renderProjects();
    refreshTitle();
  },
  unmount() {
    // the stream stops with the board; live.on stays, so coming back restarts it
    if ($('#live')) { try { liveStop(); } catch (e) { console.error('ccboard live', e); } }
    else if (live.es) { live.es.close(); live.es = null; live.tiles = {}; }
  },
});
