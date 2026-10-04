/* ccboard home page (#/): what needs me, what is working, what did it cost.
   Top to bottom: the summary line (a count per state, each a filter: ?f=waiting|working|idle|done|errored), the away strip (what happened
   since the tab was last looked at), the inbox section (Inbox.section, only when something needs you), the schedules strip, the project
   blocks (grouped by project, state or agent; projects idle for a week fold into one 'older' block) and the usage card (Widgets.usageCard).
   The rows are the shared sessionCard from pages/agents.js in its rich form; the keyed lists keep their nodes across polls.
   Also home to the banner and the login modal (startLogin / logout / openModal / updateModal), the keyboard selection (Pages) and the v0.4
   renderers other pages still import (renderTasks, renderJobs, openTaskModal, inboxItems).
   The header, usage pills, nav and the render() state consumer are in shell.js; the notify panel and nodes strip in pages/settings.js. */
'use strict';

/* The banner: one message at a time (a notice, offline, an error, a recovery, a rate limit, tmux down, not logged in). renderBanner only
   removes what it put there itself and stray text, so a callout another file keeps in #banner (Widgets.limitBanner) survives a repaint. */
const bannerOwn = [];

function renderBanner() {
  const b = $('#banner');
  if (!b) return;
  for (const n of Array.from(b.childNodes || b.children || [])) {
    if (n.nodeType === 1 && !bannerOwn.includes(n)) continue;           // somebody else's element: leave it
    if (typeof b.removeChild === 'function') b.removeChild(n);
  }
  bannerOwn.length = 0;
  b.classList.remove('warn', 'info');                                        // only the classes this function owns: Widgets.limitBanner keeps its own
  const own = (...nodes) => { for (const n of nodes) { bannerOwn.push(n); b.append(n); } };
  const limitWidget = typeof Widgets !== 'undefined' && Widgets && typeof Widgets.limitBanner === 'function';
  const rl = limitWidget ? null : rateLimitOf(state);                      // the limit callout is Widgets' job once it exists
  if (ui.notice) {
    b.classList.add('warn');
    own(el('span', { text: ui.notice }), el('button', { class: 'minimal small', type: 'button', onclick: () => { ui.notice = null; renderBanner(); }, text: 'ok' }));
  } else if (ui.offline) {
    b.classList.add('warn');
    own(el('span', { text: `Offline: showing the last known state from ${new Date(ui.offline).toLocaleTimeString()}. Retrying…` }));
  } else if (ui.error) {
    own(el('span', { text: ui.error }), el('button', { class: 'minimal small', type: 'button', onclick: () => setError(null), text: 'dismiss' }));
  } else if (state && state.deploy && state.deploy.pending) {
    const d = state.deploy;
    b.classList.add('info');                                                 // an update is news, not an error: the red bar is for ui.error and tmux_down
    if (d.hold) {
      const why = (d.reasons || []).length ? ` (${d.reasons.join(', ')})` : '';
      own(el('span', { text: `An update is ready. It installs when the terminals are closed${why}, or in ${d.minutes_left} min at the latest.` }),
        el('button', { class: 'primary small', onclick: async () => { try { await api('POST', '/api/deploy/now'); } catch (e) { setError(e.message); } await poll(true); }, text: d.forced ? 'installing at the next check' : 'Install now' }));
    } else {
      own(el('span', { text: 'Update installing: the board restarts and is back in about a minute.' }));
    }
  } else if (state && state.last_recovery && state.last_recovery.value && (state.last_recovery.value.recovered || []).length) {
    b.classList.add('warn');
    const v = state.last_recovery.value;
    const cont = v.continue || [];
    own(el('span', { text: `After a restart, ${v.recovered.length} Claude session${v.recovered.length === 1 ? '' : 's'} relaunched with --resume: ${v.recovered.join(', ')}${cont.length ? ' · was working, continue typed once back: ' + cont.join(', ') : ''}${v.closed.length ? ' · closed: ' + v.closed.join(', ') : ''}` }),
      el('button', { class: 'minimal small', type: 'button', onclick: async () => { try { await api('POST', '/api/recovery/dismiss'); } catch (e) { setError(e.message); } await poll(true); }, text: 'dismiss' }));
  } else if (rl) {
    own(el('span', { text: `Rate limited: ${rl.message || ''}${rl.session ? ' (' + rl.session + ')' : ''}` }),
      el('button', { class: 'minimal small', type: 'button', onclick: async () => { try { await api('POST', '/api/usage/rate-limit/clear'); } catch (e) { setError(e.message); } await poll(true); }, text: 'dismiss' }));
  } else if (state && state.tmux_down) {
    own(el('span', { text: 'The ccboard tmux server is not running. On the box: sudo systemctl start ccboard-tmux' }));
  } else if (state && state.claude && state.claude.installed && !state.claude.loggedIn) {
    b.classList.add('warn');
    own(el('span', { text: 'Claude Code is not logged in on this box.' }), el('button', { class: 'primary small', type: 'button', onclick: startLogin, text: 'Log in' }));
  }
  if (bannerOwn.length) b.classList.remove('limit-only');                    // the limit callout is no longer alone in the banner
  if (state) homeAwayTouch(state);                                           // every render counts as "the tab was looked at", on any page
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
  const list = boardTasks(state);                                  // the poll's rows with the optimistic Start / add / edit / delete laid over them
  if (!list.length) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  // the page's own head (no card around the board: the task cards are the only bordered boxes), then one column per column that has a card
  sec.append(el('div', { class: 'page-head tk-head' },
    el('h1', { text: `Tasks (${list.length})` }),
    el('span', { class: 'summary', text: 'one worktree + branch per task; columns follow the session state' }),
    el('div', { class: 'actions' }, el('button', { class: 'small primary', type: 'button', text: '+ task', title: 'new task: now, later (backlog) or scheduled', onclick: () => homeCreate('task') }))));
  const grid = el('div', { class: 'kanban' });
  for (const [key, label] of BOARD_COLUMNS) {
    const items = list.filter(t => t.column === key);
    if (!items.length && key !== 'backlog') continue;                // Backlog stays (it is where a task starts); an empty In progress / PR / Merged column is only a heading
    const col = el('div', { class: 'col', 'data-col': key }, el('h3', { text: `${label} (${items.length})` }));
    if (!items.length && key === 'backlog') col.append(el('div', { class: 'dim', text: 'nothing queued: + task, then Later' }));
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
        el('button', { type: 'button', onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/run`); } catch (e) { setError(e.message); } await poll(true); } }, ic('play'), 'Run now'),
        el('button', { type: 'button', class: 'minimal', onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/toggle`); } catch (e) { setError(e.message); } await poll(true); }, text: j.enabled ? 'Disable' : 'Enable' }),
        confirmButton('job:' + j.id, 'Delete', () => api('DELETE', `/api/jobs/${j.id}`), true)));
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
  if (typeof Inbox !== 'undefined' && Inbox && typeof Inbox.items === 'function') return state ? Inbox.items(state) : [];   // pages/inbox.js: the nine kinds, in their order
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
   One selection for every list of sessions: Home (the inbox section, then the open project blocks), the inbox page and the Agents roster. The index
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
  if (id === 'home') return homeVisibleItems();
  if (id === 'inbox') return inboxItems();
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
  try { return Array.from(document.querySelectorAll('#page .rrow, #page .inbox-item, #page .inbox-card')); } catch (_) { return []; }
};

Pages.nodeFor = function (tmux) { return tmux ? (Pages.rowNodes().find((n) => n.getAttribute('data-tmux') === tmux) || null) : null; };

Pages.paint = function () {
  const tmux = Pages.selected() ? Pages.selTmux : null;
  for (const n of Pages.rowNodes()) n.classList.toggle('sel', !!tmux && n.getAttribute('data-tmux') === tmux);
  Pages.markLead();
};

/* The screen's one filled primary (v0.5.6d): of the cards and rows that can carry one (an inbox card always can; a row only while a permission waits on it),
   the selected one leads, else the first in screen order. node.ccLead(true) paints that card's action filled and opens its chips and send box; every other
   one is told false and shows the same action tinted. On Home the inbox section comes first, so its first card leads and the rows below keep Allow tinted;
   on the Agents page and a project's Sessions tab the first row with a pending permission leads. */
Pages.markLead = function () {
  const nodes = Pages.rowNodes().filter((n) => typeof n.ccLead === 'function');
  const can = (n) => (typeof n.ccCanLead === 'function' ? !!n.ccCanLead() : true);
  const lead = nodes.find((n) => n.classList.contains('sel') && can(n)) || nodes.find(can) || null;
  for (const n of nodes) n.ccLead(n === lead);
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

/* The v0.4 board's section renderers are gone; these two names stay because confirmButton() (components.js) and the launcher forms call them
   after a local change, and the keyboard layer's tests call renderInbox(). Both just repaint whatever page is mounted. */
function renderInbox() { repaintPage(); }

function renderProjects() { repaintPage(); }


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
  // a neutral callout (no second accent, no primary: the hint never competes with what needs you); text, How and the x share one row
  box = el('div', { class: 'callout install-hint', role: 'note' },                    // el() drops the null child; a raw append(null) would render "null"
    el('span', { class: 'ih-text', text: 'Install ccboard as an app: its own window, keyboard shortcuts, the title bar on desktop.' }),
    canPrompt ? el('button', { class: 'small', type: 'button', text: 'Install', onclick: () => { Shell.promptInstall(); dismiss(); } }) : null,
    el('a', { class: 'btn small', href: '#/settings?sec=app', text: 'How' }),
    el('button', { class: 'icon minimal small', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: dismiss }, ic('cross')));
  return box;
}

/* ---------- the Home page ---------- */

function homeTitle() {
  const n = state ? inboxItems().length : 0;
  return (n ? `(${n}) ` : '') + 'Home';
}

const HOME_ACTIVE_DAYS = 7;                    // a project with no session activity for this long folds into the 'older' block
const HOME_INBOX_LIMIT = 8;                    // cards of the inbox section on Home; the rest is behind the link to #/inbox
const HOME_GROUP_KEY = 'ccboard:home:group';          // project | state | agent
const HOME_COLLAPSED_KEY = 'ccboard:home:collapsed';  // JSON array of collapsed block keys: project names, 'older', 's:<state>', 'a:<agent>'
const HOME_SEEN_KEY = 'ccboard:seen';                 // epoch seconds of the last render seen while the tab was visible
const HOME_SEEN_TASKS_KEY = 'ccboard:seen:tasks';     // {task id: column} at that moment: a PR opened while away shows as a column change
const HOME_GROUPS = [['project', 'Project'], ['state', 'State'], ['agent', 'Agent']];
const HOME_FILTERS = [['waiting', 'need you'], ['working', 'working'], ['idle', 'idle'], ['done', 'done'], ['errored', 'error']];
const HOME_STATE_ORDER = ['waiting', 'errored', 'working', 'idle', 'done', 'ended', 'unknown'];
const HOME_AGENT_ORDER = ['claude', 'codex', 'shell'];
const AWAY_MIN_S = 600;                        // the strip never shows for a gap shorter than 10 minutes
const AWAY_WRITE_S = 30;                       // ccboard:seen is written at most this often

const homePage = { refs: null, filter: '', cache: null };
const homeAway = { since: undefined, active: false, dismissed: false, lastWrite: 0, wired: false, lim: null, limFor: 0, prevTasks: null };

function homeGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function homeSet(key, v) { try { localStorage.setItem(key, v); } catch (_) { /* storage may be unavailable */ } }

function homeGroupMode() { const v = homeGet(HOME_GROUP_KEY); return HOME_GROUPS.some((g) => g[0] === v) ? v : 'project'; }

/* The collapsed block keys. Never saved: 'older' is collapsed. Saved: exactly what the person left. */
function homeCollapsedSet() {
  const raw = homeGet(HOME_COLLAPSED_KEY);
  if (raw === null) return new Set(['older']);
  try { const a = JSON.parse(raw); return new Set(Array.isArray(a) ? a.filter((x) => typeof x === 'string') : ['older']); } catch (_) { return new Set(['older']); }
}

function homeFilterOf(route) {
  const f = route && route.query ? route.query.f : '';
  return HOME_FILTERS.some((x) => x[0] === f) ? f : '';
}

function homeProjectHash(name) { try { return buildHash('project', { project: name }); } catch (_) { return '#/'; } }

function homeFilterHash(f) { try { return buildHash('home', {}, f ? { f } : {}); } catch (_) { return '#/'; } }

/* Seconds from epoch seconds, epoch milliseconds or an ISO string; 0 when it is none of them. */
function homeEpoch(v) {
  if (v === null || v === undefined || v === '') return 0;
  const n = typeof v === 'number' ? v : (/^\d+(?:\.\d+)?$/.test(String(v)) ? parseFloat(v) : Date.parse(v) / 1000);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return n > 1e12 ? n / 1000 : n;
}

function homeFmtUsd(x) { return '$' + (x >= 100 ? x.toFixed(0) : x.toFixed(2)); }

function homeCostText(p) {
  const c = state && state.cost && state.cost.value && state.cost.value.projects && state.cost.value.projects[p.name];
  if (!c || (typeof c.today !== 'number' && typeof c.week !== 'number')) return '';
  return `${homeFmtUsd(c.today || 0)} today · ${homeFmtUsd(c.week || 0)} 7d`;
}

/* ---------- the model: every session, grouped and filtered, in the order the page shows it ---------- */

/* Every session of a state payload with its project, repo and whether it sits in the project folder, and the projects with their repo groups. */
function homeScan(st) {
  const all = [];
  const projects = [];
  for (const p of ((st && st.projects) || [])) {
    const groups = [];
    const add = (key, label, repo, folder, sessions) => {
      const items = (sessions || []).map((s) => ({ ...s, project: p.name, repo: repo ? repo.name : (s.repo || '?'), folder }));
      for (const it of items) all.push(it);
      groups.push({ key, label, repo, folder, items });
    };
    if (p.root) add('root', 'project folder', p.root, true, p.root.sessions);
    for (const r of (p.repos || [])) add(r.name, r.name, r, r.name === 'root' || !!r.root, r.sessions);
    const orphans = new Map();
    for (const s of (p.orphan_sessions || [])) { const k = s.repo || '?'; if (!orphans.has(k)) orphans.set(k, []); orphans.get(k).push(s); }
    for (const [k, list] of orphans) add('x:' + k, `removed repo ${k}`, null, k === 'root', list);
    projects.push({ p, groups });
  }
  return { all, projects };
}

/* opts {group: project|state|agent, filter: waiting|...|'', collapsed: Set}. Returns {all, counts, blocks, mode, filter}: blocks are
   {key, kind: project|state|agent|older, name, count, total, groups: [{key, label, repo, folder, items, showHead}], collapsed, ...}; the 'older'
   block holds projects: [project blocks]. Rows inside a group are sorted attention first, then by last activity. */
function homeBuild(st, opts) {
  const o = Object.assign({ group: 'project', filter: '', collapsed: new Set() }, opts || {});
  const scan = homeScan(st);
  const now = Date.now() / 1000;
  const want = (s) => !o.filter || sessionStateKey(s) === o.filter;
  const sorted = (items) => items.filter(want).sort(sessionCompare);
  const blocks = [];
  if (o.group === 'state') {
    for (const k of HOME_STATE_ORDER) {
      const items = sorted(scan.all.filter((s) => sessionStateKey(s) === k));
      if (items.length) blocks.push({ key: 's:' + k, kind: 'state', stateKey: k, name: GLYPH_LABEL[k] || k, count: items.length, total: items.length, groups: [{ key: 'all', label: '', items, showHead: false }], collapsed: o.collapsed.has('s:' + k) });
    }
  } else if (o.group === 'agent') {
    const agents = Array.from(new Set(scan.all.map(sessionAgent)));
    agents.sort((a, b) => (HOME_AGENT_ORDER.indexOf(a) + 1 || 99) - (HOME_AGENT_ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b));
    for (const a of agents) {
      const items = sorted(scan.all.filter((s) => sessionAgent(s) === a));
      if (items.length) blocks.push({ key: 'a:' + a, kind: 'agent', agent: a, name: a, count: items.length, total: items.length, groups: [{ key: 'all', label: '', items, showHead: false }], collapsed: o.collapsed.has('a:' + a) });
    }
  } else {
    const list = [];
    for (const { p, groups } of scan.projects) {
      const every = groups.flatMap((g) => g.items);
      const activity = every.reduce((m, s) => Math.max(m, sessionActivity(s)), 0);
      const attn = every.some((s) => s.needs_attention || sessionStateKey(s) === 'waiting' || sessionStateKey(s) === 'errored');
      const working = every.some((s) => sessionStateKey(s) === 'working');
      const shown = groups.map((g) => ({ ...g, items: sorted(g.items) })).filter((g) => g.items.length);
      const single = shown.length === 1 && !shown[0].folder && shown[0].repo && shown[0].repo.name === p.name;   // the repo is the project: no sub-header
      for (const g of shown) g.showHead = !single;
      const git = single ? shown[0].repo : null;
      list.push({ key: 'p:' + p.name, kind: 'project', name: p.name, project: p, groups: shown, git, total: every.length, attn, activity,
        count: shown.reduce((n, g) => n + g.items.length, 0), collapsed: o.collapsed.has(p.name),
        // no session activity for a week and nothing that needs you or is working: it folds away (a project with no sessions at all does
        // too: Home never shows empty blocks; the sidebar lists it, and creating one from the + menu opens its project page)
        older: !attn && !working && (!activity || now - activity >= HOME_ACTIVE_DAYS * 86400) });
    }
    const order = (a, b) => (b.attn - a.attn) || (b.activity - a.activity) || a.name.localeCompare(b.name);
    const live = list.filter((b) => !b.older && (!o.filter || b.count)).sort(order);
    const old = list.filter((b) => b.older && (!o.filter || b.count)).sort(order);
    blocks.push(...live);
    if (old.length) blocks.push({ key: 'older', kind: 'older', name: 'older', count: old.length, projects: old, groups: [], collapsed: o.collapsed.has('older') });
  }
  return { all: scan.all, counts: agentsCounts(scan.all), blocks, mode: o.group, filter: o.filter };
}

function homeBuildNow(st) {
  return homeBuild(st, { group: homeGroupMode(), filter: homePage.filter, collapsed: homeCollapsedSet() });
}

/* The sessions j / k walk on Home, in screen order: the inbox section first (not while a filter is on), then the rows of every block that is open.
   A session shown twice (inbox card and project row) counts once. */
function homeInboxItems(st) {
  const items = typeof Inbox !== 'undefined' && Inbox && typeof Inbox.items === 'function' ? Inbox.items(st) : inboxItems();
  return items.slice(0, HOME_INBOX_LIMIT);
}

function homeCacheKey() { return homePage.filter + '|' + homeGroupMode() + '|' + homeGet(HOME_COLLAPSED_KEY); }

function homeItemsOf(st, model) {
  const seen = new Set();
  const out = [];
  const push = (s) => { if (!seen.has(s.tmux)) { seen.add(s.tmux); out.push(s); } };
  if (!model.filter) homeInboxItems(st).forEach(push);
  const walk = (b) => { if (!b.collapsed) for (const g of b.groups) g.items.forEach(push); };
  for (const b of model.blocks) { if (b.kind === 'older') { if (!b.collapsed) b.projects.forEach(walk); } else walk(b); }
  return out;
}

function homeVisibleItems() {
  const st = typeof state !== 'undefined' ? state : null;
  if (!st) return [];
  const key = homeCacheKey();
  if (!homePage.cache || homePage.cache.st !== st || homePage.cache.key !== key) homePage.cache = { st, key, items: homeItemsOf(st, homeBuildNow(st)) };
  return homePage.cache.items;
}

/* ---------- the away strip ---------- */

/* What happened after sinceEpoch (epoch seconds; milliseconds and ISO text work too): sessions whose state changed to done / needs you / error,
   jobs whose last run finished, tasks that reached the PR or merged column, limit episodes (limEvents: the /api/series/events?series=lim list or its
   {events} body) and registry jobs that are blocked right now. prevTasks ({task id: column} from the last visit) is optional: the task payload carries no
   change time, so a task counts when its column became pr or merged since. -> {done, needs, errored, jobs, prs, limits, blocked, total}. */
function awayDigest(st, sinceEpoch, limEvents, prevTasks) {
  const out = { done: 0, needs: 0, errored: 0, jobs: 0, prs: 0, limits: 0, blocked: 0, total: 0 };
  const t0 = homeEpoch(sinceEpoch);
  if (!st || !t0) return out;
  for (const s of rosterSessions(st)) {
    const at = homeEpoch(s.state_at);
    const k = sessionStateKey(s);
    if (at > t0) { if (k === 'done') out.done += 1; else if (k === 'waiting') out.needs += 1; else if (k === 'errored') out.errored += 1; }
    const job = sessionRegistryJob(s);
    if (job && job.state === 'blocked') out.blocked += 1;
  }
  for (const j of (st.jobs || [])) if (homeEpoch(j.last_run_at) > t0) out.jobs += 1;
  for (const t of (st.tasks || [])) {
    if (t.column !== 'pr' && t.column !== 'merged') continue;
    const pr = t.pr && typeof t.pr === 'object' ? t.pr : {};
    const at = [t.updated, t.updated_at, t.pr_at, pr.merged_at, pr.mergedAt, pr.updated_at, pr.updatedAt].map(homeEpoch).find((x) => x > 0) || 0;
    if (at ? at > t0 : (prevTasks && typeof prevTasks === 'object' && ownKey(prevTasks, String(t.id)) && prevTasks[String(t.id)] !== t.column)) out.prs += 1;
  }
  const list = Array.isArray(limEvents) ? limEvents : (limEvents && Array.isArray(limEvents.events) ? limEvents.events : []);
  const episodes = new Set();
  for (const e of list) {
    const t = homeEpoch(e && e.t);
    if (!t || t <= t0) continue;
    const m = (e && e.m) || {};
    episodes.add(`${e.key || 'other'}|${m.resets_at || Math.floor(t / 3600)}`);          // one episode: the same window reset time (or the same hour)
  }
  out.limits = episodes.size;
  out.total = out.done + out.needs + out.errored + out.jobs + out.prs + out.limits + out.blocked;
  return out;
}

function homeSeenTasks() { try { const v = JSON.parse(homeGet(HOME_SEEN_TASKS_KEY) || 'null'); return v && typeof v === 'object' ? v : null; } catch (_) { return null; } }

/* The previous visit is read once per document, before anything writes ccboard:seen; the strip only exists for a gap of 10 minutes or more. */
function homeAwayInit(nowSec) {
  if (homeAway.since !== undefined) return;
  const seen = homeEpoch(homeGet(HOME_SEEN_KEY));
  homeAway.since = seen || null;
  homeAway.active = !!seen && nowSec - seen >= AWAY_MIN_S;
  homeAway.prevTasks = homeSeenTasks();
}

function homeSeenWrite(nowSec, st) {
  homeSet(HOME_SEEN_KEY, String(Math.floor(nowSec)));
  if (st && Array.isArray(st.tasks)) { const m = {}; for (const t of st.tasks) m[t.id] = t.column; homeSet(HOME_SEEN_TASKS_KEY, JSON.stringify(m)); }
  homeAway.lastWrite = nowSec;
}

/* Wired on the first render: the moment the tab is hidden the last-seen time is saved, so the gap is counted from there. */
function homeAwayWire() {
  if (homeAway.wired || typeof document === 'undefined' || typeof document.addEventListener !== 'function') return;
  homeAway.wired = true;
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && typeof state !== 'undefined' && state && !ui.offline) homeSeenWrite(Date.now() / 1000, state);
  });
}

/* Called by every render (Home's update and renderBanner, so on any page): captures 'since' once, notices a long gap inside this document (a laptop
   that slept, a tab hidden for hours) and records that the board is being looked at, at most every 30 s while visible. */
function homeAwayTouch(st) {
  const now = Date.now() / 1000;
  homeAwayInit(now);
  homeAwayWire();
  if (homeAway.lastWrite && now - homeAway.lastWrite >= AWAY_MIN_S && homeAway.since !== homeAway.lastWrite) {
    homeAway.since = homeAway.lastWrite; homeAway.active = true; homeAway.dismissed = false; homeAway.lim = null; homeAway.limFor = 0; homeAway.prevTasks = homeSeenTasks();
  }
  if ((typeof document !== 'undefined' && document.hidden) || !st || ui.offline) return;
  if (now - homeAway.lastWrite >= AWAY_WRITE_S) homeSeenWrite(now, st);
}

/* One fetch of the limit episodes per 'since', at mount; an error leaves the strip without them. */
function homeAwayFetchLim() {
  const since = homeAway.since;
  if (!homeAway.active || !since || homeAway.limFor === since) return;
  homeAway.limFor = since;
  let p = null;
  try { p = api('GET', `/api/series/events?series=lim&since=${encodeURIComponent(new Date(since * 1000).toISOString())}`); } catch (_) { return; }
  Promise.resolve(p).then((r) => {
    if (homeAway.limFor !== since) return;
    homeAway.lim = Array.isArray(r) ? r : (r && Array.isArray(r.events) ? r.events : []);
    if (homePage.refs && typeof state !== 'undefined' && state) homePatchAway(state);
  }).catch(() => { /* limit episodes are an extra */ });
}

function homeSinceLabel(sec) {
  const d = new Date(sec * 1000);
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  return d.toDateString() === new Date().toDateString() ? hm : `${d.toLocaleDateString([], { weekday: 'short' })} ${hm}`;
}

function homeAwayNode() {
  const text = el('span', { class: 'away-text' });
  const list = el('span', { class: 'away-list' });
  const x = el('button', { class: 'minimal small away-x', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: () => { homeAway.dismissed = true; if (state) homePatchAway(state); } }, ic('cross'));
  const node = el('div', { class: 'away callout hidden', role: 'note' }, text, list, x);
  node.ccText = text;
  node.ccList = list;
  return node;
}

const AWAY_ITEMS = [
  ['done', (n) => `${n} done`, '#/?f=done'], ['needs', (n) => `${n} need${n === 1 ? 's' : ''} you`, '#/?f=waiting'],
  ['errored', (n) => `${n} error${n === 1 ? '' : 's'}`, '#/?f=errored'], ['limits', (n) => `${n} limit hit${n === 1 ? '' : 's'}`, '#/usage'],
  ['prs', (n) => `${n} PR${n === 1 ? '' : 's'}`, '#/tasks'], ['jobs', (n) => `${n} scheduled run${n === 1 ? '' : 's'}`, '#/tasks'],
  ['blocked', (n) => `${n} blocked`, '#/agents'],
];

function homePatchAway(st) {
  const r = homePage.refs;
  if (!r) return;
  const show = homeAway.active && !homeAway.dismissed && !!homeAway.since;
  const d = show ? awayDigest(st, homeAway.since, homeAway.lim, homeAway.prevTasks) : null;
  const sig = d && d.total ? JSON.stringify([homeAway.since, d]) : '';
  if (r.awaySig === sig) return;
  r.awaySig = sig;
  r.away.classList.toggle('hidden', !sig);
  if (!sig) return;
  setTextIfChanged(r.away.ccText, `while you were away (since ${homeSinceLabel(homeAway.since)})`);
  r.away.ccList.textContent = '';
  for (const [k, label, href] of AWAY_ITEMS) {
    if (!d[k]) continue;
    r.away.ccList.append(el('a', { class: 'away-n', href, text: label(d[k]) }));
  }
}

/* ---------- summary line and group control ---------- */

function homeSummaryNode() {
  const node = el('nav', { class: 'summary sumbar', 'aria-label': 'Filter sessions by state' });
  const segs = {};
  for (const [f, label] of HOME_FILTERS) {
    const n = el('span', { class: 'sum-n' });
    const a = el('a', { class: 'sum-seg sum-' + f, href: homeFilterHash(f), 'data-f': f }, stateGlyph(f), n, el('span', { class: 'sum-l', text: label }));
    a.addEventListener('click', (e) => { e.preventDefault(); navigate(a.getAttribute('href')); });
    segs[f] = { a, n };
    node.append(a);
  }
  node.ccSegs = segs;
  return node;
}

function homePatchSummary(counts, filter) {
  const segs = homePage.refs.summary.ccSegs;
  for (const [f] of HOME_FILTERS) {
    const { a, n } = segs[f];
    const c = counts[f] || 0;
    setTextIfChanged(n, String(c));
    a.classList.toggle('hidden', f === 'errored' && !c && filter !== 'errored');       // the error segment appears only when there is one
    a.classList.toggle('zero', !c);
    a.classList.toggle('on', filter === f);
    if (filter === f) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current');
    a.setAttribute('href', homeFilterHash(filter === f ? '' : f));                      // tapping the active one clears the filter
  }
}

function homeGroupControl() {
  const btns = {};
  const node = el('div', { class: 'seg-ctl', role: 'group', 'aria-label': 'Group by' }, el('span', { class: 'seg-k dim', text: 'group' }));
  for (const [k, label] of HOME_GROUPS) {
    btns[k] = el('button', { class: 'seg-btn', type: 'button', 'data-group': k, text: label, onclick: () => homeSetGroup(k) });
    node.append(btns[k]);
  }
  node.ccBtns = btns;
  return node;
}

function homeSetGroup(k) {
  homeSet(HOME_GROUP_KEY, k);
  homeRender();
}

function homeToggleCollapsed(key) {
  const set = homeCollapsedSet();
  if (set.has(key)) set.delete(key); else set.add(key);
  homeSet(HOME_COLLAPSED_KEY, JSON.stringify([...set]));
  homeRender();
}

/* ---------- blocks, groups and rows ---------- */

/* Where the block's + session starts one: the repo (or project folder) of the project's most recent session, else its only repo, else the project
   folder, else its first repo. null when the project has nowhere to run one. */
function homeLaunchTarget(p) {
  const ok = (p.repos || []).filter((x) => x.state === 'ok' || x.state === 'unknown');
  const make = (r) => ({ r, label: r === p.root ? `${p.name} · project folder` : `${p.name}/${r.name}` });
  let best = null;
  let at = -1;
  for (const r of [p.root, ...ok]) for (const s of ((r && r.sessions) || [])) { const t = sessionActivity(s); if (t > at) { at = t; best = r; } }
  if (best) return make(best);
  if (ok.length === 1) return make(ok[0]);
  if (p.root) return make(p.root);
  return ok[0] ? make(ok[0]) : null;
}

/* The existing launcher form in the sheet (Shell.showForm closes the sheet on its Cancel and once the launch worked). */
function homeNewSession(p, r, label) {
  if (typeof Shell !== 'undefined' && Shell && typeof Shell.showForm === 'function') { Shell.showForm('session', { p, r, label }); return; }
  ui.openForm = 'sheet';
  openSheet({ title: `New session · ${label}`, body: sessionForm(p, r), onClose: () => { if (ui.openForm === 'sheet') ui.openForm = null; } });
}

function homeCreate(kind) {
  if (typeof Shell !== 'undefined' && Shell && typeof Shell.openCreate === 'function') Shell.openCreate(kind);
  else if (kind === 'project' && typeof Shell !== 'undefined' && Shell && typeof Shell.newProject === 'function') Shell.newProject();
}

function homeRowOpts(kind) {
  return { rich: true, perm: true, showProject: kind !== 'project', noWhere: kind === 'project' };
}

function homeGroupNode(g, kind, getProject) {
  const name = el('span', { class: 'pg-name mono' });
  const branch = el('span', { class: 'pg-branch dim mono' });
  const dot = el('span', { class: 'dot hidden' });
  const cost = el('span', { class: 'pg-cost dim mono' });
  const add = el('button', { class: 'minimal small pg-add', type: 'button', 'aria-label': 'New session in this repo', title: 'new session here' }, ic('plus'));
  const head = el('div', { class: 'pgroup-head' }, name, branch, dot, cost, add);
  const list = el('div', { class: 'pg-list' });
  const node = el('div', { class: 'pgroup' }, head, list);
  const cur = { g };
  add.addEventListener('click', () => {
    const r = cur.g.repo;
    const p = getProject();
    if (r && p) homeNewSession(p, r, cur.g.folder ? `${p.name} · project folder` : `${p.name}/${r.name}`);
  });
  const rows = makeKeyedList(list, { key: (s) => s.tmux, create: (s) => sessionCard(s, homeRowOpts(kind)), patch: (n, s) => n.ccPatch(s) });
  node.ccPatch = (g2) => {
    cur.g = g2;
    head.classList.toggle('hidden', !g2.showHead);
    setTextIfChanged(name, g2.label);
    const r = g2.repo;
    setTextIfChanged(branch, r && r.state === 'ok' && r.branch ? r.branch : '');
    dot.classList.toggle('hidden', !(r && r.state === 'ok'));
    dot.classList.toggle('dirty', !!(r && r.dirty));
    dot.setAttribute('title', r && r.dirty ? 'uncommitted changes' : 'clean');
    const p = getProject();
    setTextIfChanged(cost, r && p ? (repoCost(p, r) || '') : '');
    add.classList.toggle('hidden', !(r && (r.state === 'ok' || r.state === 'unknown' || g2.folder)));
    rows.update(g2.items);
  };
  node.ccPatch(g);
  return node;
}

function homeBlockNode(b) {
  const toggle = el('button', { class: 'minimal small pb-toggle', type: 'button', 'aria-expanded': 'true' });
  const name = b.kind === 'project' ? el('a', { class: 'pb-name ' + chipHue('project', b.name), href: homeProjectHash(b.name) }) : el('span', { class: 'pb-name' });
  const lead = b.kind === 'state' ? stateGlyph(b.stateKey) : (b.kind === 'agent' ? agentGlyph(b.agent) : null);
  const count = el('span', { class: 'pb-count dim' });
  const git = el('span', { class: 'pb-git dim mono' });
  const dot = el('span', { class: 'dot hidden' });
  const cost = el('span', { class: 'pb-cost dim mono' });
  const add = b.kind === 'project' ? el('button', { class: 'small pb-add', type: 'button', title: 'start a session in this project', text: '+ session' }) : null;
  const body = el('div', { class: 'pb-body' });
  const node = el('section', { class: 'pblock pb-' + b.kind, 'data-block': b.key, 'data-project': b.kind === 'project' ? b.name : null },
    el('header', { class: 'pb-head' }, toggle, lead, name, count, git, dot, cost, add), body);
  const cur = { b };
  const key = b.kind === 'project' ? b.name : b.key;
  toggle.addEventListener('click', () => homeToggleCollapsed(key));
  const project = () => cur.b.project || null;                                  // the newest payload's project object, not the one the node was built with
  if (add) add.addEventListener('click', () => { const p = project(); const t = p && homeLaunchTarget(p); if (t) homeNewSession(p, t.r, t.label); });
  const groups = makeKeyedList(body, { key: (g) => 'g:' + g.key, create: (g) => homeGroupNode(g, b.kind, project), patch: (n, g) => n.ccPatch(g) });
  node.ccPatch = (b2) => {
    cur.b = b2;
    const p = b2.project || null;
    node.classList.toggle('collapsed', !!b2.collapsed);
    node.classList.toggle('has-attn', !!b2.attn);
    setTextIfChanged(toggle, b2.collapsed ? '▸' : '▾');
    toggle.setAttribute('aria-expanded', b2.collapsed ? 'false' : 'true');
    toggle.setAttribute('aria-label', `${b2.collapsed ? 'expand' : 'collapse'} ${b2.name}`);
    setTextIfChanged(name, b2.name);
    setTextIfChanged(count, b2.count === b2.total || b2.kind !== 'project' ? `${b2.count} session${b2.count === 1 ? '' : 's'}` : `${b2.count} of ${b2.total} sessions`);
    const gr = b2.git;
    setTextIfChanged(git, gr && gr.state === 'ok' && gr.branch ? gr.branch : '');
    dot.classList.toggle('hidden', !(gr && gr.state === 'ok'));
    dot.classList.toggle('dirty', !!(gr && gr.dirty));
    setTextIfChanged(cost, p ? homeCostText(p) : '');
    if (add) add.classList.toggle('hidden', !(p && homeLaunchTarget(p)));
    body.classList.toggle('hidden', !!b2.collapsed);
    groups.update(b2.collapsed ? [] : b2.groups);                              // a collapsed block drops its rows (and their tail subscriptions)
  };
  node.ccPatch(b);
  return node;
}

function homeOlderNode(b) {
  const toggle = el('button', { class: 'pb-older-toggle', type: 'button', 'aria-expanded': 'false' });
  const body = el('div', { class: 'pb-body' });
  const node = el('section', { class: 'pblock pb-older older', 'data-block': 'older' }, el('header', { class: 'pb-head' }, toggle), body);
  toggle.addEventListener('click', () => homeToggleCollapsed('older'));
  const projects = makeKeyedList(body, { key: (x) => x.key, create: homeBlockNode, patch: (n, x) => n.ccPatch(x) });
  node.ccPatch = (b2) => {
    node.classList.toggle('collapsed', !!b2.collapsed);
    setTextIfChanged(toggle, `${b2.collapsed ? '▸' : '▾'} older (${b2.count})`);
    toggle.setAttribute('aria-expanded', b2.collapsed ? 'false' : 'true');
    toggle.setAttribute('title', `projects with no session activity for ${HOME_ACTIVE_DAYS} days`);
    body.classList.toggle('hidden', !!b2.collapsed);
    projects.update(b2.collapsed ? [] : b2.projects);
  };
  node.ccPatch(b);
  return node;
}

/* ---------- inbox section, schedules strip, usage card ---------- */

/* Without pages/inbox.js (a partial deploy, a test world): the needs-attention sessions as plain session cards. */
function homeInboxFallback(r, st) {
  if (!r.inboxList) r.inboxList = makeKeyedList(r.inboxHost, { key: (s) => s.tmux, create: (s) => sessionCard(s, { showProject: true, perm: true, cls: 'inbox-item' }), patch: (n, s) => n.ccPatch(s) });
  const items = inboxItems();
  r.inboxList.update(items.slice(0, HOME_INBOX_LIMIT));
  return Math.min(items.length, HOME_INBOX_LIMIT);
}

/* The inbox section: Inbox.section fills #inbox-home (and shows or hides it); a filter shows rows only, so the section would list the same sessions twice. */
function homePatchInbox(st) {
  const r = homePage.refs;
  if (homePage.filter) { r.inboxHost.classList.add('hidden'); return; }
  if (typeof Inbox !== 'undefined' && Inbox && typeof Inbox.section === 'function') { Inbox.section(r.inboxHost, st, { limit: HOME_INBOX_LIMIT, link: '#/inbox' }); return; }
  r.inboxHost.classList.toggle('hidden', !homeInboxFallback(r, st));
}

function homeOpenSchedules() {
  const sec = el('section', { id: 'jobs', class: 'jobs-sheet' });
  openSheet({ title: 'Schedules', body: sec });
  renderJobs();
}

function homeSchedNode() {
  const list = el('span', { class: 'sched-list' });
  const all = el('button', { class: 'minimal small sched-all', type: 'button', onclick: homeOpenSchedules, text: 'All' });
  const node = el('div', { class: 'sched hidden' }, el('span', { class: 'sched-k dim', text: 'Next runs' }), list, all);
  node.ccList = list;
  node.ccAll = all;
  return node;
}

function homePatchSched(st) {
  const r = homePage.refs;
  const jobs = (st.jobs || []).filter((j) => j.enabled && j.next_run_at);
  const next = jobs.map((j) => ({ j, at: homeEpoch(j.next_run_at) })).filter((x) => x.at).sort((a, b) => a.at - b.at).slice(0, 3);
  const total = (st.jobs || []).length;
  const sig = JSON.stringify([next.map((x) => [x.j.id, x.j.name, x.at, x.j.agent]), total]);
  r.sched.classList.toggle('hidden', !total);
  if (r.schedSig === sig) return;
  r.schedSig = sig;
  const list = r.sched.ccList;
  list.textContent = '';
  if (!next.length) list.append(el('span', { class: 'dim', text: `no upcoming runs (${total} schedule${total === 1 ? '' : 's'})` }));
  for (const { j, at } of next) {
    const when = el('time', { class: 'until mono', 'data-epoch': String(Math.floor(at)), text: fmtIn(at) });
    list.append(el('button', { class: 'sched-item', type: 'button', title: `${j.name} · ${j.project}/${j.repo}`, onclick: homeOpenSchedules },
      agentGlyph(j.agent || 'claude'), el('span', { class: 'sched-name', text: j.name }), el('span', { class: 'dim' }, 'in ', when)));
  }
  setTextIfChanged(r.sched.ccAll, `All ${total}`);
}

/* ---------- mount / update / route / unmount ---------- */

function homeRender(st) {
  const r = homePage.refs;
  const s = st || (typeof state !== 'undefined' ? state : null);
  if (!r || !s) return;
  Pages.dropSkeleton();
  homeAwayTouch(s);
  homeAwayFetchLim();
  const model = homeBuildNow(s);
  homePage.cache = { st: s, key: homeCacheKey(), items: homeItemsOf(s, model) };      // Pages.sync() below reads it
  homePatchSummary(model.counts, model.filter);
  for (const [k, btn] of Object.entries(r.group.ccBtns)) btn.setAttribute('aria-pressed', model.mode === k ? 'true' : 'false');
  homePatchAway(s);
  homePatchInbox(s);
  homePatchSched(s);
  r.blocks.update(model.blocks);
  const noProjects = !(s.projects || []).length;
  r.empty.classList.toggle('hidden', !noProjects);
  r.blocksHost.classList.toggle('hidden', noProjects);
  let note = '';
  if (!noProjects && !model.blocks.length) {
    const f = HOME_FILTERS.find((x) => x[0] === model.filter);
    const EMPTY_WORD = { waiting: 'waiting for you', working: 'working', idle: 'idle', done: 'finished', errored: 'failing' };
    note = f ? `Nothing is ${EMPTY_WORD[f[0]] || f[1]} right now.` : 'No live sessions yet. Start one from the + menu.';
  }
  setTextIfChanged(r.note, note);
  r.note.classList.toggle('hidden', !note);
  r.clear.classList.toggle('hidden', !(note && model.filter));
  if (r.usage) { try { r.usage.update(s); } catch (e) { console.error('ccboard usage card', e); } }
  if ($('#jobs')) renderJobs();                                                     // the schedules sheet: its Run / Disable / Delete buttons repaint with the poll
  sessionTailSweep();
  Pages.sync();
  Pages.paint();
  refreshTitle();
}

registerPage('home', {
  title: () => homeTitle(),
  mount(root, route) {
    Pages.reset();
    homePage.filter = homeFilterOf(route);
    homePage.cache = null;
    homeAwayInit(Date.now() / 1000);                                                  // the last visit, before this render writes the new one
    const summary = homeSummaryNode();
    const group = homeGroupControl();
    const away = homeAwayNode();
    const inboxHost = el('section', { id: 'inbox-home', class: 'home-inbox hidden' });          // Inbox.section builds its head and cards in here
    const sched = homeSchedNode();
    const blocksHost = el('div', { class: 'pblocks' });
    const note = el('p', { class: 'home-note dim hidden' });
    const clear = el('a', { class: 'btn small hidden', href: '#/', text: 'Clear filter' });
    const empty = pageEmpty('folder-close', 'No projects yet', 'A project is a folder of repos; sessions run inside a repo.');
    empty.append(el('button', { class: 'primary', type: 'button', onclick: () => homeCreate('project'), text: 'Add project' }));
    empty.classList.add('hidden');
    const usageHost = el('div', { id: 'usage-home', class: 'home-usage' });
    root.append(
      el('div', { class: 'page-head' }, el('h1', { text: 'Home' }), el('div', { class: 'actions' }, group)),
      summary, homeInstallHint(),
      ...(currentState() ? [] : [Pages.skeleton(3)]),                                    // first paint: rows until /api/state arrives
      away, inboxHost, sched, blocksHost, note, clear, empty, usageHost);
    // a click on a row or card selects it, so the mouse and j / k share one selection
    root.addEventListener('click', (e) => {
      const row = e.target && typeof e.target.closest === 'function' ? e.target.closest('.rrow, .inbox-card, .inbox-item') : null;
      if (!row || !(blocksHost.contains(row) || inboxHost.contains(row))) return;
      const i = Pages.items().findIndex((x) => x.tmux === row.getAttribute('data-tmux'));
      if (i >= 0) Pages.setIndex(i);
    });
    let usage = null;
    if (typeof Widgets !== 'undefined' && Widgets && typeof Widgets.usageCard === 'function') { try { usage = Widgets.usageCard(usageHost); } catch (e) { console.error('ccboard usage card', e); } }
    homePage.refs = { summary, group, away, awaySig: null, inboxHost, inboxList: null, sched, schedSig: null, blocksHost, note, clear, empty, usage,
      blocks: makeKeyedList(blocksHost, { key: (b) => b.key, create: (b) => (b.kind === 'older' ? homeOlderNode(b) : homeBlockNode(b)), patch: (n, b) => n.ccPatch(b) }) };
    startAgeTicker();
  },
  update(st) { homeRender(st); },
  onRoute(route) {                                                                    // #/ <-> #/?f=waiting: the same page, a different filter (no remount, no lost subscriptions)
    homePage.filter = homeFilterOf(route);
    homePage.cache = null;
    homeRender();
  },
  unmount() {
    sessionTailStopAll();
    const r = homePage.refs;
    if (r && r.usage && typeof r.usage.destroy === 'function') { try { r.usage.destroy(); } catch (e) { console.error('ccboard usage card', e); } }
    homePage.refs = null;
    homePage.cache = null;
    stopAgeTicker();
  },
});
