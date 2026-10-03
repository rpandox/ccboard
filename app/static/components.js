/* ccboard components: small renderers shared by the pages (badges, rows, cards, form fields, two-tap buttons).
   Definitions only. Blueprint classes come from el() in core.js; no Blueprint class literals here. */
'use strict';

function pctBar(pct) {
  const cls = pct >= 85 ? ' bad' : pct >= 60 ? ' warn' : '';
  const i = el('i'); i.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  return el('span', { class: 'bar' + cls }, i);
}

function confirmButton(key, label, action, quiet) {
  // Two taps: the first turns the button into "Confirm …" + Cancel. `quiet` renders the first state low-key
  // (minimal, no red fill) for destructive actions that sit next to everyday ones on a phone.
  // Repaint whichever page is mounted (a keyed roster row, the peek, the board): never a home-only renderer, so it is safe off the board.
  const repaint = () => { if (typeof repaintPage === 'function') repaintPage(); else if (typeof render === 'function') render(true); };
  if (ui.confirm === key) {
    return el('span', { class: 'row' },
      el('button', { class: 'danger confirm', onclick: async () => { ui.confirm = null; try { await action(); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: `Confirm ${label}` }),
      el('button', { onclick: () => { ui.confirm = null; repaint(); }, text: 'Cancel' }));
  }
  return el('button', { class: 'danger' + (quiet ? ' minimal' : ''), onclick: () => { ui.confirm = key; repaint(); }, text: label });
}

function repoGroups(p) { return p.root ? [p.root, ...p.repos] : p.repos; }   // the project folder row first, then the repos

function allRepos() {
  const out = [];
  for (const p of state.projects) for (const r of p.repos) if (r.state === 'ok' || r.state === 'unknown') out.push({ id: `${p.name}/${r.name}`, project: p.name, repo: r.name });
  return out;
}

function field(label, control, hint) { return el('div', { class: 'field' }, el('span', { text: label }), control, hint ? el('span', { class: 'dim', text: hint }) : null); }
function selectEl(options, value) {
  const sel = el('select');
  for (const [v, t] of options) sel.append(el('option', { value: v, text: t }));
  if (value !== undefined && value !== null) sel.value = value;
  return sel;
}

const STATE_LABEL = { idle: 'idle', working: 'working', waiting: 'needs you', done: 'done', errored: 'error', ended: 'ended', unknown: '' };

function stateBadge(s) {
  const st = s.state || 'unknown';
  if (!STATE_LABEL[st]) return null;
  const age = s.state_at ? fmtAge(Date.parse(s.state_at) / 1000) : '';
  // beside its text label the glyph is decoration: hide it from screen readers (no double announcement) and let the
  // badge's own title (the last hook event) show on hover
  const glyph = stateGlyph(st);
  glyph.setAttribute('aria-hidden', 'true');
  glyph.removeAttribute('title');
  return el('span', { class: `state ${st}` + (s.needs_attention ? ' attn' : ''), title: s.last_event || '' },
    glyph, ' ', STATE_LABEL[st] + (age ? ` ${age}` : ''));
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

// Sessions carry no agent field until the adapter phase: shell and clone launchers run a plain shell, the rest run Claude.
function sessionAgent(s) { return s.agent || (s.launcher === 'shell' || s.launcher === 'clone' ? 'shell' : 'claude'); }

function sessionMeta(s) {
  const stats = statsText(s);
  const parts = [s.launcher || null, stats ? el('span', { class: 'mono', text: stats }) : null, `${fmtAge(s.created)} · ${s.attached} attached`].filter(Boolean);
  const kids = [];
  parts.forEach((p, i) => { if (i) kids.push(' · '); kids.push(p); });
  return el('span', { class: 'meta' }, ...kids);
}

function sessionRow(s) {
  const row = el('div', { class: 'sess' + (s.needs_attention ? ' attn' : ''), 'data-tmux': s.tmux },
    el('div', { class: 'main' },
      agentGlyph(sessionAgent(s)),
      el('span', { class: 'name', text: s.name }),
      stateBadge(s),
      sessionMeta(s),
      el('code', { class: 'mono', text: s.command || '' })),
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

const COLUMNS = [['in_progress', 'In progress'], ['needs_you', 'Needs you'], ['done', 'Done'], ['pr', 'PR open'], ['merged', 'Merged']];
/* Every kanban: Backlog (a task before any session: phase backlog | queued) first, then COLUMNS. */
const BOARD_COLUMNS = [['backlog', 'Backlog'], ...COLUMNS];

function ciBadge(t) {
  if (!t.pr_url) return null;
  const b = (t.ci && t.ci.bucket) || 'none';
  const cls = b === 'pass' ? 'ok' : b === 'fail' ? 'bad' : b === 'pending' ? 'warn' : '';
  const review = t.pr && t.pr.review ? ` · ${t.pr.review.toLowerCase().replace('_', ' ')}` : '';
  const txt = `${(t.pr_state || 'PR').toLowerCase()} · CI ${b}${review}`;
  return el('span', { class: 'badge ' + cls, title: (t.ci && t.ci.checks || []).map(c => `${c.name}: ${c.bucket}`).join('\n'), text: txt });
}

/* ---------- tasks v2 (v0.5.14a): git targets, optimistic rows, the backlog card, Start / Send to session / Edit / Delete ---------- */

const TASK_OVERRIDE_TTL = 20000;         // ms an optimistic row outlives a poll that never agrees (a write that failed unseen); demo mode keeps them

function taskPhase(t) { return (t && t.phase) || 'running'; }
function taskIsBacklog(t) { const ph = taskPhase(t); return ph === 'backlog' || ph === 'queued'; }
function taskWhere(t) { return t.repo === 'root' ? `${t.project} · project folder` : `${t.project}/${t.repo}`; }
function sessionNameOf(tmux) { const s = String(tmux || ''); const i = s.lastIndexOf('--'); return i >= 0 ? s.slice(i + 2) : s; }
function taskPeekHash(tmux) { try { return buildHash('session', { tmux }); } catch (_) { return ''; } }
function taskDemo() { return typeof demoOn === 'function' && demoOn(); }

/* Can a task or a schedule run here? A repo that is ok (or not yet known to be broken), or the project folder when it is a git repo itself
   (state.projects[].root carries a branch or state 'ok' then) and is not already listed as the project's one repo. */
function gitTarget(p, r) {
  if (!p || !r) return false;
  if (r.root || r.name === 'root') {
    if ((p.repos || []).some((x) => x.path && x.path === r.path)) return false;      // the folder is already listed as a repo of the same name
    return !!(r.git === true || r.branch || r.state === 'ok');
  }
  return r.state === 'ok' || r.state === 'unknown';
}

function rootIsGit(r) { return !!(r && (r.git === true || r.branch || r.state === 'ok')); }

/* Can a TASK run here? Like gitTarget, except that the project folder is always a target: a git repo gets a worktree, any other folder runs
   the task in place (mode 'attached', v0.5.14a: "give the whole project a task"). Schedules keep gitTarget (a run needs a worktree). */
function taskTarget(p, r) {
  if (!p || !r) return false;
  if (r.root || r.name === 'root') return !(p.repos || []).some((x) => x.path && x.path === r.path);
  return r.state === 'ok' || r.state === 'unknown';
}

/* The places a task (kind 'task', default) or a schedule (kind 'job'/'schedule') can start in a project: its repos, then its project folder.
   [{p, r, label, sub}]; sub says 'in place (not a git repo)' for a folder a task runs in without a worktree. */
function taskTargets(p, kind) {
  const forTask = !kind || kind === 'task';
  const can = forTask ? taskTarget : gitTarget;
  const out = [];
  for (const r of (p.repos || [])) if (can(p, r)) out.push({ p, r, label: `${p.name}/${r.name}`, sub: r.branch || '' });
  if (p.root && can(p, p.root)) out.push({ p, r: p.root, label: `${p.name} · project folder`, sub: rootIsGit(p.root) ? (p.root.branch || 'project folder') : 'in place (not a git repo)' });
  return out;
}

function taskOverrideSet(row, keys, extra) {
  if (!row || row.id === undefined || row.id === null) return null;
  const o = { ...row, ...(extra || {}), _at: Date.now() };
  if (keys) o._keys = keys;
  store.tasksOverride[row.id] = o;
  return o;
}

function taskOverrideDrop(id) { delete store.tasksOverride[id]; }
function taskRepaint() { if (typeof repaintPage === 'function') repaintPage(); }
function taskStartingSession() { return { state: 'working', state_at: new Date().toISOString(), last_message: '', needs_attention: false, command: 'claude' }; }

/* Has the poll caught up with an optimistic row? Same phase, session and text. */
function taskConfirmed(o, t) {
  if (taskPhase(t) !== taskPhase(o) || (t.tmux || '') !== (o.tmux || '') || t.title !== o.title) return false;
  return o.prompt === undefined || o.prompt === null || t.prompt === undefined || t.prompt === null || t.prompt === o.prompt;
}

function taskApply(t, o) {
  const row = t ? { ...t } : {};
  for (const k of (o._keys || Object.keys(o).filter((x) => x.charAt(0) !== '_'))) row[k] = o[k];
  return row;
}

/* state.tasks with store.tasksOverride painted over it: an optimistic Start, a card added or edited, a delete that has not reached the poll yet.
   An override is dropped when the poll agrees (taskConfirmed), when its task is gone for a delete, or after TASK_OVERRIDE_TTL. */
function boardTasks(st) {
  const base = st && Array.isArray(st.tasks) ? st.tasks : [];
  const ov = store.tasksOverride;
  if (!Object.keys(ov).length) return base;
  const keep = taskDemo();
  const now = Date.now();
  const out = [];
  const used = new Set();
  for (const t of base) {
    const o = ov[t.id];
    if (!o) { out.push(t); continue; }
    used.add(String(t.id));
    if (!keep && (now - o._at > TASK_OVERRIDE_TTL || (!o._gone && taskConfirmed(o, t)))) { delete ov[t.id]; out.push(t); continue; }
    if (!o._gone) out.push(taskApply(t, o));
  }
  const fresh = [];
  for (const id of Object.keys(ov)) {
    const o = ov[id];
    if (used.has(id)) continue;
    if (o._gone || (!keep && now - o._at > TASK_OVERRIDE_TTL)) { if (!keep) delete ov[id]; continue; }    // a delete the poll has caught up with
    if (o._new) fresh.push(taskApply(null, o));
  }
  return fresh.concat(out);
}

/* The row an optimistic update paints: the server's own state.tasks-shaped row when the response carries it (res.task), else `base` with the
   response's tmux / session_row / slug / branch laid over it. */
function taskRowFromResponse(base, res, patch) {
  const row = { ...base, ...(res && res.task && typeof res.task === 'object' ? res.task : {}), ...(patch || {}) };
  if (res && res.tmux) row.tmux = res.tmux;
  if (res && res.session_row !== undefined && res.session_row !== null) row.session_row = res.session_row;
  if (res && res.slug) row.slug = res.slug;
  if (res && res.branch) row.branch = res.branch;
  row.phase = 'running';
  if (!row.column || row.column === 'backlog') row.column = 'in_progress';
  if (!row.session) row.session = taskStartingSession();
  return row;
}

/* Open the peek of a session just started (the sheet in front of it closes first); false in demo mode, where there is no such session. */
function taskOpenPeek(tmux) {
  if (!tmux || taskDemo() || typeof navigate !== 'function') return false;
  const h = taskPeekHash(tmux);
  if (!h) return false;
  if (typeof closeSheet === 'function') closeSheet();
  navigate(h);
  return true;
}

function taskStatus(node, text, bad) { node.textContent = text || ''; node.classList.toggle('bad', !!bad); }

function taskFail(e) { toast(e && e.message ? e.message : String(e), { kind: 'bad' }); }

/* Start: a new session in the task's own worktree. The card moves to In progress at once; the response (or an error that puts it back) settles it. */
async function taskStart(t) {
  const cur = store.tasksOverride[t.id];
  if (cur && cur._busy) return;                                           // a double tap
  const pending = { ...t, phase: 'running', column: 'in_progress', tmux: '', session_row: null, mode: 'worktree', session: taskStartingSession() };
  taskOverrideSet(pending, ['phase', 'column', 'tmux', 'session_row', 'mode', 'session'], { _busy: true });
  taskRepaint();
  try {
    const res = await api('POST', `/api/tasks/${t.id}/dispatch`, { mode: 'lane' });
    const row = taskRowFromResponse(pending, res, { mode: 'worktree' });
    if (!row.tmux && taskDemo()) row.tmux = `${t.project}--${t.repo}--t-${t.slug}`;
    taskOverrideSet(row);
    toast(`started ${row.slug || t.slug}`, { kind: 'ok' });
    taskRepaint();
    taskOpenPeek(row.tmux);
    if (typeof poll === 'function') poll(true);
  } catch (e) {
    taskOverrideDrop(t.id);
    taskRepaint();
    taskFail(e);
  }
}

/* Sessions of the task's project that could take its prompt: Claude sessions that are not ended, ready ones (idle, done, waiting at its idle
   prompt: wait_kind 'idle_prompt') first and those in the task's own repo before the others. A waiting session in a permission prompt or a dialog
   is listed but off ('waiting on a prompt': typing into it would answer it), and so is one that has just started (state unknown: 'starting…').
   [{s, repo, same, ok, why}] */
function taskSessionTargets(t, st) {
  const out = [];
  const cur = st || (typeof state !== 'undefined' ? state : null);
  const p = cur && (cur.projects || []).find((x) => x.name === t.project);
  if (!p) return out;
  const pend = new Set(((cur && cur.pending_permissions) || []).map((x) => x.tmux_name));
  const seen = new Set();
  const take = (s, repo) => {
    if (!s || !s.tmux || seen.has(s.tmux)) return;
    seen.add(s.tmux);
    const stt = s.state || 'unknown';
    if (sessionAgent(s) !== 'claude' || stt === 'ended' || s.name === 'clone') return;
    let why = '';
    if (stt === 'working') why = 'working: wait for its turn to end';
    else if (stt === 'waiting' && pend.has(s.tmux)) why = 'waiting for a permission decision';
    else if (stt === 'waiting' && s.wait_kind !== 'idle_prompt') why = 'waiting on a prompt';
    else if (stt === 'unknown') why = 'starting…';
    else if (stt !== 'idle' && stt !== 'done' && stt !== 'waiting') why = stt === 'errored' ? 'stopped with an error' : stt;
    out.push({ s, repo, same: repo === t.repo, ok: !why, why });
  };
  for (const r of repoGroups(p)) for (const s of (r.sessions || [])) take(s, r.name);
  for (const s of (p.orphan_sessions || [])) take(s, s.repo || '?');
  const rank = { idle: 0, done: 1, waiting: 2 };
  out.sort((a, b) => (b.ok - a.ok) || (b.same - a.same) || ((rank[a.s.state] ?? 9) - (rank[b.s.state] ?? 9)) || String(b.s.state_at || '').localeCompare(String(a.s.state_at || '')));
  return out;
}

/* The repo-mismatch 409 ({error, mismatch: {task, session}}): api() keeps the body on the error (err.body, err.status), so that is what is
   tested first; the message is only the fallback for an error that was built without a body. */
function taskIsMismatch(e) { return !!(e && ((e.body && e.body.mismatch) || e.mismatch || /another repo|mismatch/i.test(e.message || ''))); }

/* Hand the task's prompt to a running session. A session of another repo answers 409 (mismatch): ask, then retry with force. */
async function taskSend(t, target, force) {
  const tmux = target.s.tmux;
  const cur = store.tasksOverride[t.id];
  if (cur && cur._busy) return;
  const pending = { ...t, phase: 'running', column: 'in_progress', tmux, session_row: target.s.row_id === undefined ? null : target.s.row_id, mode: 'session', session: taskStartingSession() };
  taskOverrideSet(pending, ['phase', 'column', 'tmux', 'session_row', 'mode', 'session'], { _busy: true });
  taskRepaint();
  try {
    const res = await api('POST', `/api/tasks/${t.id}/dispatch`, force ? { session: tmux, force: true } : { session: tmux });
    taskOverrideSet(taskRowFromResponse(pending, res, { mode: 'session', tmux }));
    toast(`sent to ${target.s.name}`, { kind: 'ok' });
    taskRepaint();
    if (typeof poll === 'function') poll(true);
  } catch (e) {
    taskOverrideDrop(t.id);
    taskRepaint();
    if (!force && taskIsMismatch(e)) {
      const ask = typeof window !== 'undefined' && typeof window.confirm === 'function'
        && window.confirm(`${e.message}\n\n${target.s.name} works in ${target.repo}, this task is in ${t.repo}. Work there anyway? The prompt goes in with "Work in <this task's repo>." in front.`);
      if (ask) await taskSend(t, target, true);
      return;
    }
    taskFail(e);
  }
}

/* The sheet behind "Send to session": the project's live sessions, each one tap. */
function taskSendSheet(t) {
  const list = taskSessionTargets(t);
  const rows = list.map((x) => el('button', { class: 'minimal pick-row tk-pick' + (x.ok ? '' : ' off'), type: 'button', disabled: !x.ok, 'data-tmux': x.s.tmux,
    title: x.why || `send the prompt to ${x.s.name}`, onclick: () => { closeSheet(); taskSend(t, x); } },
  stateGlyph(x.s.state),
  el('span', { class: 'pr-name mono', text: x.s.name }),
  el('span', { class: 'dim tk-repo', text: (x.repo === 'root' ? 'project folder' : x.repo) + (x.same ? '' : ' · other repo') }),
  x.why ? el('span', { class: 'dim', text: x.why }) : null,
  x.s.last_prompt ? el('span', { class: 'dim tk-last', text: '› ' + String(x.s.last_prompt).slice(0, 100) }) : null));
  const start = el('div', { class: 'submit' }, el('button', { class: 'primary', type: 'button', onclick: () => { closeSheet(); taskStart(t); } }, ic('play'), 'Start in a new session'));
  const anyOk = list.some((x) => x.ok);
  const body = !list.length ? [emptyState('console', 'No session to send to', 'No Claude session of this project is running.'), start]
    : [el('p', { class: 'dim tk-note', text: anyOk ? 'Ready sessions first. A session in another repo asks before it works there.' : 'None of these can take it right now: wait for one, or start a new session.' }),
      el('div', { class: 'pick-list' }, ...rows), ...(anyOk ? [] : [start])];                    // never a dead end: with no ready session the way out is the new one
  openSheet({ title: `Send “${String(t.title).slice(0, 60)}”`, body });
}

/* Edit a backlog card: a title and a prompt in a small sheet (PATCH sends only what changed). The full prompt is fetched when the row carries only its head. */
function taskEditSheet(t) {
  const title = el('input', { type: 'text', maxlength: 120, value: t.title || '', 'aria-label': 'title' });
  const prompt = el('textarea', { class: 'composer task-prompt', rows: '4', 'aria-label': 'prompt', autocomplete: 'off', spellcheck: 'false' });
  const status = el('div', { class: 'dim form-status' });
  const truncated = typeof t.prompt_len === 'number' && typeof t.prompt === 'string' && t.prompt.length < t.prompt_len;
  let original = typeof t.prompt === 'string' ? t.prompt : '';
  prompt.value = original;
  if (truncated) {
    prompt.disabled = true;
    prompt.placeholder = 'loading the full prompt…';
    Promise.resolve().then(() => api('GET', `/api/tasks/${t.id}`)).then((full) => {
      original = String((full && full.prompt) || t.prompt || '');
      prompt.value = original;
      prompt.disabled = false;
      if (typeof composerGrow === 'function') composerGrow(prompt, 12);
    }).catch((e) => { prompt.placeholder = 'could not load the full prompt: leave empty to keep it'; prompt.disabled = false; taskStatus(status, e.message, true); });
  }
  const save = async () => {
    const body = {};
    const nt = title.value.trim();
    if (!nt) { taskStatus(status, 'a title is required', true); title.focus(); return; }       // the sheet stays: closing it would look like a save
    if (nt !== t.title) body.title = nt;
    const np = prompt.value.trim();
    if (np && np !== original.trim()) body.prompt = np;
    if (!Object.keys(body).length) { closeSheet(); return; }
    taskStatus(status, 'saving…');
    try {
      const res = await api('PATCH', `/api/tasks/${t.id}`, body);
      const head = typeof res.prompt === 'string' ? res.prompt.slice(0, 600) : (body.prompt ? body.prompt.slice(0, 600) : t.prompt);
      const row = res && res.task && typeof res.task === 'object' ? { ...t, ...res.task } : { ...t, title: body.title || t.title, prompt: head, prompt_len: body.prompt ? body.prompt.length : t.prompt_len };
      taskOverrideSet(row, ['title', 'prompt', 'prompt_len']);
      closeSheet();
      toast('saved', { kind: 'ok' });
      taskRepaint();
      if (typeof poll === 'function') poll(true);
    } catch (e) { taskStatus(status, e.message, true); }
  };
  prompt.addEventListener('input', () => { if (typeof composerGrow === 'function') composerGrow(prompt, 12); });
  prompt.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.isComposing) { e.preventDefault(); save(); } });
  const form = el('form', { class: 'form', onsubmit: (e) => { e.preventDefault(); save(); } },
    field('title', title), field('prompt', prompt), status,
    el('div', { class: 'submit' }, el('button', { class: 'primary', type: 'submit', text: 'Save' }),
      el('button', { type: 'button', onclick: () => closeSheet(), text: 'Cancel' })));
  openSheet({ title: `Edit task · ${taskWhere(t)}`, body: form });
  if (typeof composerGrow === 'function') composerGrow(prompt, 12);
  title.focus();
}

/* Delete a backlog card: gone at once, back with an error toast if the server refused. Called through confirmButton (two taps). */
async function taskDelete(t) {
  taskOverrideSet(t, ['phase'], { _gone: true });
  taskRepaint();
  try { await api('DELETE', `/api/tasks/${t.id}`); }
  catch (e) { taskOverrideDrop(t.id); taskRepaint(); throw e; }
}

/* A task before any session (phase backlog | queued): title, the head of its prompt, where, how old; Start, Send to session, Edit, Delete. */
function backlogCard(t) {
  const queued = taskPhase(t) === 'queued';
  const when = t.created_at ? Date.parse(t.created_at) / 1000 : 0;
  const added = !when ? '' : (Date.now() / 1000 - when < 20 ? 'added just now' : `added ${fmtAge(when)} ago`);
  const quick = taskSessionTargets(t).filter((x) => x.ok && x.same);
  const head = String(t.prompt || '');
  const more = head.startsWith(t.title) ? head.slice(t.title.length).replace(/^[\s.:;,-]+/, '') : head;       // a title cut from the prompt's first line is not said twice
  const card = el('div', { class: 'task backlog' + (queued ? ' queued' : ''), 'data-task': t.id, 'data-phase': taskPhase(t) },
    el('div', { class: 'row' }, el('span', { class: 'title', text: t.title }), queued ? el('span', { class: 'state ended', text: 'waiting for a step' }) : null),
    more ? el('div', { class: 'tk-prompt', text: more }) : null,
    el('div', { class: 'meta' }, taskWhere(t), added ? ` · ${added}` : ''),
    el('div', { class: 'actions' },
      queued ? null : el('button', { class: 'primary tk-start', type: 'button', title: 'Start in a new session, in its own worktree and branch', onclick: () => taskStart(t) }, ic('play'), 'Start'),
      queued ? null : el('button', { class: 'tk-send', type: 'button', title: 'Hand the prompt to a running session', onclick: () => taskSendSheet(t), text: 'Send to session' }),
      queued || quick.length !== 1 ? null : el('button', { class: 'tk-quick', type: 'button', title: `Send the prompt to ${quick[0].s.name} now`, onclick: () => taskSend(t, quick[0]), text: `→ ${quick[0].s.name}` }),
      el('button', { class: 'tk-edit', type: 'button', onclick: () => taskEditSheet(t), text: 'Edit' }),
      confirmButton('tdel:' + t.id, 'Delete', () => taskDelete(t), true)));
  return card;
}

function taskCard(t) {
  if (taskIsBacklog(t)) return backlogCard(t);
  const s = t.session;
  const ciFail = t.ci && t.ci.bucket === 'fail';
  const starting = !t.tmux;
  const handed = t.mode === 'session';
  const chip = handed && t.tmux ? el('a', { class: 'chip-btn tk-chip', href: taskPeekHash(t.tmux), title: `this task runs in ${sessionNameOf(t.tmux)}`, text: `in ${sessionNameOf(t.tmux)}` }) : null;
  const card = el('div', { class: 'task' + (s && s.needs_attention ? ' attn' : '') + (handed ? ' handed' : ''), 'data-task': t.id },
    el('div', { class: 'row' }, el('span', { class: 'title', text: t.title }), s ? stateBadge(s) : el('span', { class: 'state ended', text: 'no session' }), ciBadge(t)),
    el('div', { class: 'meta' }, starting ? `${taskWhere(t)} · starting…` : (t.branch ? `${t.project}/${t.repo} · ${t.branch}` : taskWhere(t) + (t.mode === 'attached' ? ' · in place' : '')), t.pr_url ? ' · PR #' + t.pr_number : null,
      typeof t.cost_usd === 'number' ? [' · ', el('span', { class: 'mono', text: '$' + t.cost_usd.toFixed(2) })] : null, chip ? ' ' : null, chip),
    s && s.last_message ? el('div', { class: 'last', text: s.last_message.slice(0, 160) }) : null,
    (t.overlap && t.overlap.length) ? el('div', { class: 'last bad', title: t.overlap.map(o => `${o.title}: ${o.files.join(', ')}`).join('\n'),
      text: '⚠ overlaps ' + t.overlap.map(o => `"${o.title}" (${o.files.length} file${o.files.length === 1 ? '' : 's'}: ${o.files.slice(0, 3).join(', ')}${o.files.length > 3 ? '…' : ''})`).join('; ') }) : null,
    el('div', { class: 'actions' },
      starting ? null : el('a', { class: 'btn primary', href: `/term/${encodeURIComponent(t.tmux)}`, target: '_blank', rel: 'noopener' }, ic('console'), 'Terminal'),
      t.branch ? el('button', { onclick: () => openTaskModal(t), text: t.pr_url ? 'Diff / PR' : 'Diff / PR…' }) : null,
      t.pr_url ? el('a', { class: 'btn', href: t.pr_url, target: '_blank', rel: 'noopener', text: 'PR' }) : null,
      t.preview_url ? el('a', { class: 'btn', href: t.preview_url, target: '_blank', rel: 'noopener', text: `Preview :${t.preview_port}` }) : null,
      starting ? null : (t.preview_url ? el('button', { onclick: async () => { try { await api('DELETE', `/api/tasks/${t.id}/preview`); } catch (e) { setError(e.message); } await poll(true); }, title: 'stop exposing the preview', text: '⏏' }) :
        el('button', { onclick: async () => {
          try { const r = await api('POST', `/api/tasks/${t.id}/preview`, {}); toast(`preview at ${r.url} → 127.0.0.1:${r.port}`, { kind: 'ok', ttl: 8000 }); }
          catch (e) { if (/no listening port/.test(e.message)) { const p = window.prompt(e.message + '\n\nDev server port (leave blank to cancel):'); if (p) { try { await api('POST', `/api/tasks/${t.id}/preview`, { port: parseInt(p, 10) }); } catch (e2) { setError(e2.message); } } } else setError(e.message); }
          await poll(true);
        }, title: 'expose a dev server running in this session on its own tailnet HTTPS port', text: 'Preview' })),
      ciFail ? el('button', { class: 'danger', onclick: async () => { try { const r = await api('POST', `/api/tasks/${t.id}/fix-ci`); setError(null); toast(`CI logs (${r.chars} chars) sent to ${t.title}${r.relaunched ? ' (session relaunched)' : ''}`, { kind: 'ok', ttl: 8000 }); } catch (e) { setError(e.message); } await poll(true); }, text: 'Fix CI' }) : null,
      t.pr_url ? el('button', { onclick: async () => { try { await api('POST', `/api/tasks/${t.id}/refresh`); } catch (e) { setError(e.message); } await poll(true); }, title: 'refresh PR / CI status', text: '↻' }) : null,
      starting ? null : confirmButton('arch:' + t.id, 'Archive', async () => {
        try { await api('POST', `/api/tasks/${t.id}/archive`, { force: false }); }
        catch (e) {
          if (/force/.test(e.message) && window.confirm(e.message + '\n\nDiscard the worktree anyway?')) await api('POST', `/api/tasks/${t.id}/archive`, { force: true });
          else throw e;
        }
      })));
  return card;
}

/* ---------- shell components (v0.5.3): tabs, sheet, toast, menu, empty state. Definitions only: the DOM is touched when they are called. ---------- */

/* tabs(items:[{id,label,count?}], activeId, onChange) -> { root, set(id), setCount(id, n), value }. Only the tab list: the page renders the panel.
   set() repaints without calling onChange (so a route change cannot loop); a click or an arrow key calls onChange(id). */
function tabs(items, activeId, onChange) {
  const list = el('div', { class: 'tablist', role: 'tablist' });
  const root = el('div', { class: 'tabs' }, list);
  const nodes = new Map();
  const counts = new Map();
  const ids = items.map((it) => it.id);
  let current = ids.includes(activeId) ? activeId : ids[0];
  const paint = (id) => {
    current = id;
    for (const [tid, n] of nodes) {
      n.setAttribute('aria-selected', tid === id ? 'true' : 'false');
      n.setAttribute('tabindex', tid === id ? '0' : '-1');
    }
  };
  const pick = (id, focus) => {
    if (id !== current) { paint(id); if (typeof onChange === 'function') onChange(id); }
    if (focus) nodes.get(id).focus();
  };
  for (const it of items) {
    const count = it.count === undefined || it.count === null ? null : el('span', { class: 'tab-count', text: String(it.count) });
    if (count) counts.set(it.id, count);
    const n = el('div', { class: 'tab', role: 'tab', 'data-tab': it.id, onclick: () => pick(it.id, false), onkeydown: (e) => {
      const i = ids.indexOf(it.id);
      let to = null;
      if (e.key === 'ArrowRight') to = ids[(i + 1) % ids.length];
      else if (e.key === 'ArrowLeft') to = ids[(i + ids.length - 1) % ids.length];
      else if (e.key === 'Home') to = ids[0];
      else if (e.key === 'End') to = ids[ids.length - 1];
      else if (e.key === 'Enter' || e.key === ' ') to = it.id;
      if (to === null) return;
      e.preventDefault();
      pick(to, true);
    } }, it.label, count);
    nodes.set(it.id, n);
    list.append(n);
  }
  paint(current);
  return {
    root,
    set(id) { if (nodes.has(id)) paint(id); },
    setCount(id, n) { const c = counts.get(id); if (c) setText(c, n); },
    get value() { return current; },
  };
}

/* openSheet({title, body, actions?, placement?, onClose?}) -> { dialog, body, close }. dialog#sheet through showModal(): a right panel from 840 px up,
   a bottom sheet below (placement 'right' | 'bottom' forces one). Calling it again while open swaps the content in place. body and actions
   take a node, an array of nodes or a string. Esc, the backdrop and the close button end it; closeSheet() does the same from code. */
function sheetKids(x) { return x === null || x === undefined || x === false ? [] : (Array.isArray(x) ? x : [x]).flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false); }

function openSheet(opts) {
  const dlg = document.getElementById('sheet');
  if (!dlg) return null;
  const o = opts || {};
  if (!dlg.dataset.wired) {
    dlg.dataset.wired = '1';
    dlg.addEventListener('close', () => {
      const cb = dlg._onClose;
      dlg._onClose = null;
      dlg.textContent = '';
      if (typeof cb === 'function') { try { cb(); } catch (e) { console.error('ccboard sheet onClose', e); } }
    });
    dlg.addEventListener('click', (e) => { if (e.target === dlg) closeSheet(); });   // the dialog has no padding: a click on itself is a backdrop click
  }
  const wide = !!(window.matchMedia && window.matchMedia('(min-width: 840px)').matches);
  const place = o.placement === 'bottom' || o.placement === 'right' ? o.placement : (wide ? 'right' : 'bottom');
  dlg.classList.toggle('bottom', place === 'bottom');
  dlg.classList.toggle('right', place === 'right');
  const body = el('div', { class: 'sheet-body' }, ...sheetKids(typeof o.body === 'string' ? document.createTextNode(o.body) : o.body));
  const actions = sheetKids(o.actions);
  dlg.textContent = '';
  dlg.append(
    el('div', { class: 'sheet-head' }, el('h2', { class: 'sheet-title', text: o.title || '' }),
      el('button', { class: 'icon minimal', type: 'button', 'aria-label': 'Close', title: 'Close', onclick: () => closeSheet() }, ic('cross'))),
    body);
  if (actions.length) dlg.append(el('div', { class: 'sheet-actions' }, ...actions));   // Element.append(null) would add the text "null"
  dlg._onClose = typeof o.onClose === 'function' ? o.onClose : null;
  if (!dlg.open) { try { dlg.showModal(); } catch (_) { dlg.setAttribute('open', ''); } }
  return { dialog: dlg, body, close: closeSheet };
}

function closeSheet() {
  const dlg = document.getElementById('sheet');
  if (!dlg) return;
  if (dlg.open) { try { dlg.close(); } catch (_) { dlg.removeAttribute('open'); } }
  else if (dlg._onClose) { const cb = dlg._onClose; dlg._onClose = null; dlg.textContent = ''; try { cb(); } catch (e) { console.error('ccboard sheet onClose', e); } }
}

/* toast(text, {kind, ttl}): appended to #toasts, removed after ttl ms (default 4 s, 7 s for 'bad'), click to dismiss. kind: info | ok | warn | bad.
   #toasts is a manual popover where the browser has them, so it is re-promoted above any modal dialog that is open (a sheet, the drawer). */
function toastHost(on) {
  const host = document.getElementById('toasts');
  if (!host || typeof host.showPopover !== 'function') return host;
  try {
    if (!host.hasAttribute('popover')) host.setAttribute('popover', 'manual');
    if (host.matches(':popover-open')) host.hidePopover();
    if (on) host.showPopover();
  } catch (_) { /* no popover support: the fixed container still shows */ }
  return host;
}

function toast(text, opts) {
  const host = toastHost(true);
  if (!host) return null;
  const o = opts || {};
  const kind = ['info', 'ok', 'warn', 'bad'].includes(o.kind) ? o.kind : 'info';
  const ttl = typeof o.ttl === 'number' ? o.ttl : (kind === 'bad' ? 7000 : 4000);
  const node = el('div', { class: 'toast ' + kind, role: kind === 'bad' ? 'alert' : null }, el('span', { class: 'toast-text', text: String(text) }));
  const gone = () => { node.remove(); if (!host.childElementCount) toastHost(false); };
  node.addEventListener('click', gone);
  host.append(node);
  while (host.childElementCount > 4) host.firstElementChild.remove();
  if (ttl > 0) setTimeout(gone, ttl);
  return node;
}

/* menu(button, items:[{label, icon?, onClick}] | () => items) -> { open(), close(), toggle(), root }. A small popover under the button, in the
   button's dialog when it sits in one, else inside #topbar. Click outside, Esc, Tab or a route change close it; arrows move, Enter picks. */
function menu(button, items) {
  let pop = null;
  const ctl = { get root() { return pop; } };
  const onDoc = (e) => { if (pop && !pop.contains(e.target) && !button.contains(e.target)) ctl.close(false); };
  const onKey = (e) => {
    if (!pop) return;
    const rows = [...pop.querySelectorAll('.menuitem')];
    const i = rows.indexOf(document.activeElement);
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); ctl.close(true); }
    else if (e.key === 'Tab') ctl.close(false);
    else if (e.key === 'ArrowDown') { e.preventDefault(); rows[(i + 1) % rows.length].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); rows[(i + rows.length - 1) % rows.length].focus(); }
    else if (e.key === 'Home') { e.preventDefault(); rows[0].focus(); }
    else if (e.key === 'End') { e.preventDefault(); rows[rows.length - 1].focus(); }
  };
  const onHash = () => ctl.close(false);
  ctl.close = (refocus) => {
    if (!pop) return;
    pop.remove();
    pop = null;
    button.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', onDoc, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('hashchange', onHash);
    if (refocus) button.focus();
  };
  ctl.open = () => {
    if (pop) return;
    const list = typeof items === 'function' ? items() : items;
    pop = el('div', { class: 'menu menu-pop', role: 'menu' });
    for (const it of list) {
      const pick = () => { ctl.close(false); if (typeof it.onClick === 'function') it.onClick(); };
      pop.append(el('div', { class: 'menuitem', role: 'menuitem', tabindex: '-1', onclick: pick,
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } } },
      it.icon ? ic(it.icon) : null, el('span', { class: 'mi-text', text: it.label })));
    }
    (button.closest('dialog') || document.getElementById('topbar') || document.body).append(pop);
    const r = button.getBoundingClientRect();
    pop.style.top = `${Math.round(r.bottom + 4)}px`;
    if (r.left + r.width / 2 > window.innerWidth / 2) pop.style.right = `${Math.max(8, Math.round(window.innerWidth - r.right))}px`;
    else pop.style.left = `${Math.max(8, Math.round(r.left))}px`;
    button.setAttribute('aria-expanded', 'true');
    document.addEventListener('pointerdown', onDoc, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('hashchange', onHash);
    const first = pop.querySelector('.menuitem');
    if (first) first.focus();
  };
  ctl.toggle = () => (pop ? ctl.close(true) : ctl.open());
  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'false');
  button.addEventListener('click', () => ctl.toggle());
  return ctl;
}

/* emptyState(icon, title, hint): the centred "nothing here" block (icon = a Blueprint icon name). */
function emptyState(icon, title, hint) {
  return el('div', { class: 'nonideal empty' },
    el('div', { class: 'empty-visual' }, ic(icon || 'info-sign')),
    el('h4', { class: 'empty-title', text: title || '' }),
    hint ? el('div', { class: 'empty-hint dim', text: hint }) : null);
}

/* ---- composer: the multi-line send box ----------------------------------------------------------------------------
   Enter sends; Shift+Enter (or the ↵ button, because touch keyboards have no Shift+Enter) inserts a newline; ⌘/Ctrl+Enter
   also sends. The box grows with its text up to maxRows (default 6) and scrolls beyond that; the height goes through the
   CSSOM (ta.style), never a style attribute. Multi-line text reaches tmux as ONE bracketed paste (tmux.send_text), so the
   newlines stay inside the prompt instead of submitting after the first line. */
const COMPOSER_MAX_ROWS = 6;

function composerGrow(ta, maxRows) {
  if (!ta || !ta.style) return;
  const rows = maxRows || ta._maxRows || COMPOSER_MAX_ROWS;
  let lh = 20, pad = 14;                                             // fallbacks for environments without layout (tests)
  if (typeof getComputedStyle === 'function') {
    const cs = getComputedStyle(ta);
    lh = parseFloat(cs.lineHeight) || (parseFloat(cs.fontSize) || 14) * 1.4;
    pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0) + (parseFloat(cs.borderTopWidth) || 0) + (parseFloat(cs.borderBottomWidth) || 0);
  }
  const max = Math.round(lh * rows + pad);
  if (!String(ta.value || '').length) {                               // empty: back to one row (a wrapped placeholder would otherwise inflate scrollHeight)
    ta.style.height = ''; ta.style.overflowY = 'hidden'; return;
  }
  ta.style.height = 'auto';                                          // let scrollHeight shrink again after lines were deleted
  const want = ta.scrollHeight || 0;
  const h = Math.min(want, max);
  ta.style.height = h ? h + 'px' : '';
  ta.style.overflowY = want > max ? 'auto' : 'hidden';
}

function composerInsertNewline(ta) {
  const v = String(ta.value || '');
  const s = typeof ta.selectionStart === 'number' ? ta.selectionStart : v.length;
  const e = typeof ta.selectionEnd === 'number' ? ta.selectionEnd : s;
  if (typeof ta.setRangeText === 'function') ta.setRangeText('\n', s, e, 'end');
  else ta.value = v.slice(0, s) + '\n' + v.slice(e);
  composerGrow(ta);
  if (typeof ta.focus === 'function') ta.focus();
}

/* Wire an existing textarea: opts {onSend(text, ta), maxRows}. onSend owns the text (it sends and clears the box). */
function composerBind(ta, opts) {
  const o = opts || {};
  ta._maxRows = o.maxRows || COMPOSER_MAX_ROWS;
  ta.classList.add('composer');
  ta.addEventListener('input', () => composerGrow(ta));
  ta.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' || e.isComposing) return;                  // Enter during IME composition confirms the composition
    e.preventDefault();                                              // no stray newline, no form submit
    if (e.shiftKey || e.altKey) { composerInsertNewline(ta); return; }
    if (typeof o.onSend === 'function') o.onSend(ta.value, ta);
  });
  return ta;
}

/* Build a composer textarea: opts {placeholder, label, id, onSend, maxRows}. */
function composer(opts) {
  const o = opts || {};
  const ta = el('textarea', { class: 'composer', rows: '1', placeholder: o.placeholder || 'send', 'aria-label': o.label || o.placeholder || 'send',
    autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', enterkeyhint: 'send', id: o.id || null });
  return composerBind(ta, o);
}

/* A ↵ button that inserts a newline into `ta`; pointerdown is cancelled so focus (and the soft keyboard) stays on the box. */
function newlineButton(ta) {
  return el('button', { class: 'minimal small nl', type: 'button', title: 'newline (Shift+Enter)', 'aria-label': 'insert newline',
    onpointerdown: (e) => e.preventDefault(), onclick: () => composerInsertNewline(ta) }, '↵');
}
