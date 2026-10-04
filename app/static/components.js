/* ccboard components: small renderers shared by the pages (badges, rows, cards, form fields, two-tap buttons).
   Definitions only. Blueprint classes come from el() in core.js; no Blueprint class literals here. */
'use strict';

function pctBar(pct) {
  const cls = pct >= 85 ? ' bad' : pct >= 60 ? ' warn' : '';
  const i = el('i'); i.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  return el('span', { class: 'bar' + cls }, i);
}

/* Pointer helpers for the forms (components.js is loaded by index.html and term.html, shell.js only by the board).
   coarsePointer(): a touch device, or the html.force-coarse QA switch. focusFine(node): focus a field only on a fine pointer, so a phone never
   opens its soft keyboard (with a cyan ring over the first field) the moment a sheet opens; a tap on a field focuses it as before.
   narrowViewport(): the phone layout (under 600 px), for the controls that fold into a menu there. Each answers false where it cannot tell. */
function coarsePointer() {
  try { if (document.documentElement && document.documentElement.classList && document.documentElement.classList.contains('force-coarse')) return true; } catch (_) { /* no document element */ }
  try { return !!(window.matchMedia && window.matchMedia('(pointer:coarse)').matches); } catch (_) { return false; }
}
function focusFine(node) {
  if (!node || typeof node.focus !== 'function' || coarsePointer()) return false;
  node.focus();
  return true;
}
function narrowViewport() {
  try { return !!(window.matchMedia && window.matchMedia('(max-width: 599px)').matches); } catch (_) { return false; }
}

/* Arm a two-tap button: the page repaints (whichever is mounted) with its Confirm + Cancel. */
function confirmArm(key) {
  ui.confirm = key;
  if (typeof repaintPage === 'function') repaintPage(); else if (typeof render === 'function') render(true);
}

function confirmButton(key, label, action, quiet) {
  // Two taps: the first turns the button into "Confirm …" + Cancel. A destructive button rests red-outlined (style.css); only the armed
  // "Confirm …" is filled red. `quiet` renders the resting state as a minimal (borderless-fill) button for rows crowded with everyday actions.
  // Repaint whichever page is mounted (a keyed roster row, the peek, the board): never a home-only renderer, so it is safe off the board.
  const repaint = () => { if (typeof repaintPage === 'function') repaintPage(); else if (typeof render === 'function') render(true); };
  if (ui.confirm === key) {
    return el('span', { class: 'row' },
      el('button', { class: 'danger confirm', onclick: async () => { ui.confirm = null; try { await action(); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: `Confirm ${label}` }),
      el('button', { onclick: () => { ui.confirm = null; repaint(); }, text: 'Cancel' }));
  }
  return el('button', { class: 'danger' + (quiet ? ' minimal' : ''), title: `${label} (tap again to confirm)`, onclick: () => confirmArm(key), text: label });
}

function repoGroups(p) { return p.root ? [p.root, ...p.repos] : p.repos; }   // the project folder row first, then the repos

function allRepos() {
  const out = [];
  for (const p of state.projects) for (const r of p.repos) if (r.state === 'ok' || r.state === 'unknown') out.push({ id: `${p.name}/${r.name}`, project: p.name, repo: r.name });
  return out;
}

/* A labelled control: the label above, the control, an (initially empty) inline error, then helper text in --dim.
   The label is tied to the control: <label for> when the control is an input, select or textarea (or names one in control._labelFor), and
   aria-labelledby on a wrapper (checks, a segmented control) so a click on the label never toggles its first checkbox. aria-describedby names
   the hint and the error slot. The node carries .errNode and .target; fieldError(node, text, focus) fills the slot. */
let fieldSeq = 0;
function fieldLabelable(n) { return !!n && /^(INPUT|SELECT|TEXTAREA)$/.test(String(n.tagName || '').toUpperCase()); }

function field(label, control, hint, err) {
  const n = ++fieldSeq;
  const target = control && control._labelFor ? control._labelFor : (fieldLabelable(control) ? control : null);
  let id = target ? target.getAttribute('id') : null;
  if (target && !id) { id = `fld${n}`; target.setAttribute('id', id); }
  const labelId = `fld${n}-l`;
  const lab = el('label', { class: 'field-label', id: labelId, for: id, text: label });
  const errNode = el('span', { class: 'field-err bad', role: 'alert', id: `fld${n}-e`, text: err || '' });
  const hintNode = hint ? el('span', { class: 'dim field-hint', id: `fld${n}-h`, text: hint }) : null;
  const owner = target || control;
  if (owner && typeof owner.setAttribute === 'function') {
    owner.setAttribute('aria-describedby', hintNode ? `fld${n}-h fld${n}-e` : `fld${n}-e`);
    if (!target) {
      owner.setAttribute('aria-labelledby', labelId);
      if (!owner.getAttribute('role')) owner.setAttribute('role', 'group');
    }
  }
  const node = el('div', { class: 'field' }, lab, control, errNode, hintNode);
  node.errNode = errNode;
  node.target = owner;
  return node;
}

/* Say what is wrong next to the field (and flag the control for assistive tech); an empty text clears it. focus: move to the control (a
   submit that failed is an action of the person's: the keyboard may open then). */
function fieldError(f, text, focus) {
  if (!f || !f.errNode) return;
  f.errNode.textContent = text || '';
  const t = f.target;
  if (t && typeof t.setAttribute === 'function') { if (text) t.setAttribute('aria-invalid', 'true'); else t.removeAttribute('aria-invalid'); }
  if (text && focus && t && typeof t.focus === 'function') t.focus();
}
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
  const title = el('input', { type: 'text', maxlength: 120, value: t.title || '', autocomplete: 'off' });
  const prompt = el('textarea', { class: 'composer task-prompt', rows: '4', autocomplete: 'off', spellcheck: 'false' });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const titleField = field('Title', title);
  const promptField = field('Prompt', prompt, 'Cmd/Ctrl+Enter saves.');
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
    fieldError(titleField, '');
    if (!nt) { fieldError(titleField, 'A title is required.', true); return; }                  // the sheet stays: closing it would look like a save
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
    titleField, promptField, status,
    el('div', { class: 'submit' }, el('button', { class: 'primary', type: 'submit', text: 'Save' }),
      el('button', { type: 'button', onclick: () => closeSheet(), text: 'Cancel' })));
  openSheet({ title: `Edit task · ${taskWhere(t)}`, body: form });
  if (typeof composerGrow === 'function') composerGrow(prompt, 12);
  focusFine(title);
}

/* Delete a backlog card: gone at once, back with an error toast if the server refused. Called through confirmButton (two taps). */
async function taskDelete(t) {
  taskOverrideSet(t, ['phase'], { _gone: true });
  taskRepaint();
  try { await api('DELETE', `/api/tasks/${t.id}`); }
  catch (e) { taskOverrideDrop(t.id); taskRepaint(); throw e; }
}

/* A task before any session (phase backlog | queued): title, the head of its prompt, where, how old; Start (a tinted primary: the cards repeat it),
   Send to session, Edit, Delete. Under 600 px only Start (and the one-tap "→ s1" target) stay on the card: the other three fold into a ... menu,
   and Delete there is still two taps (the menu arms it, the card then shows Confirm Delete + Cancel). */
function backlogCard(t) {
  const queued = taskPhase(t) === 'queued';
  const when = t.created_at ? Date.parse(t.created_at) / 1000 : 0;
  const added = !when ? '' : (Date.now() / 1000 - when < 20 ? 'added just now' : `added ${fmtAge(when)} ago`);
  const quick = taskSessionTargets(t).filter((x) => x.ok && x.same);
  const head = String(t.prompt || '');
  const more = head.startsWith(t.title) ? head.slice(t.title.length).replace(/^[\s.:;,-]+/, '') : head;       // a title cut from the prompt's first line is not said twice
  const delKey = 'tdel:' + t.id;
  const del = confirmButton(delKey, 'Delete', () => taskDelete(t), true);
  let tail;
  if (narrowViewport()) {
    const dots = el('button', { class: 'icon minimal tk-more', type: 'button', 'aria-label': 'More actions', title: 'More actions' }, ic('more'));
    menu(dots, [...(queued ? [] : [{ label: 'Send to session', icon: 'send-message', onClick: () => taskSendSheet(t) }]),
      { label: 'Edit', icon: 'edit', onClick: () => taskEditSheet(t) },
      { label: 'Delete', icon: 'trash', onClick: () => confirmArm(delKey) }]);
    tail = [ui.confirm === delKey ? del : dots];
  } else {
    tail = [queued ? null : el('button', { class: 'tk-send', type: 'button', title: 'Hand the prompt to a running session', onclick: () => taskSendSheet(t), text: 'Send to session' }),
      el('button', { class: 'tk-edit', type: 'button', onclick: () => taskEditSheet(t), text: 'Edit' }), del];
  }
  const card = el('div', { class: 'task backlog' + (queued ? ' queued' : ''), 'data-task': t.id, 'data-phase': taskPhase(t) },
    el('div', { class: 'row' }, el('span', { class: 'title', text: t.title }), queued ? el('span', { class: 'state ended', text: 'waiting for a step' }) : null),
    more ? el('div', { class: 'tk-prompt', text: more }) : null,
    el('div', { class: 'meta' }, taskWhere(t), added ? ` · ${added}` : ''),
    el('div', { class: 'actions' },
      queued ? null : el('button', { class: 'primary tinted tk-start', type: 'button', title: 'Start in a new session, in its own worktree and branch', onclick: () => taskStart(t) }, ic('play'), 'Start'),
      queued || quick.length !== 1 ? null : el('button', { class: 'tk-quick', type: 'button', title: `Send the prompt to ${quick[0].s.name} now`, onclick: () => taskSend(t, quick[0]), text: `→ ${quick[0].s.name}` }),
      ...tail));
  return card;
}

/* Preview: expose the task's dev server on its own tailnet HTTPS port. When the server cannot find the listening port on its own it asks, in a small
   sheet (a labelled number field, Expose + Cancel, the reason in --dim), never a browser prompt(). */
async function taskPreview(t, port) {
  const r = await api('POST', `/api/tasks/${t.id}/preview`, port ? { port } : {});
  toast(`preview at ${r.url} → 127.0.0.1:${r.port}`, { kind: 'ok', ttl: 8000 });
  return r;
}

function taskPortSheet(t, why) {
  const port = el('input', { type: 'number', min: '1', max: '65535', step: '1', inputmode: 'numeric', autocomplete: 'off', placeholder: 'e.g. 3000' });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const portField = field('Dev server port', port, why || 'The port the dev server in this session listens on.');
  const go = el('button', { class: 'primary', type: 'submit', text: 'Expose preview' });
  const form = el('form', { class: 'form', novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    const n = parseInt(port.value, 10);
    fieldError(portField, '');
    if (!(n >= 1 && n <= 65535)) { fieldError(portField, 'Enter a port between 1 and 65535.', true); return; }
    go.disabled = true;
    taskStatus(status, 'exposing…');
    try { await taskPreview(t, n); closeSheet(); if (typeof poll === 'function') await poll(true); }
    catch (err) { taskStatus(status, err.message, true); }
    go.disabled = false;
  } }, portField, status,
  el('div', { class: 'submit' }, go, el('button', { type: 'button', onclick: () => closeSheet(), text: 'Cancel' })));
  openSheet({ title: `Preview · ${String(t.title).slice(0, 60)}`, body: form });
  focusFine(port);
}

function taskCard(t) {
  if (taskIsBacklog(t)) return backlogCard(t);
  const s = t.session;
  const ciFail = t.ci && t.ci.bucket === 'fail';
  const starting = !t.tmux;
  const handed = t.mode === 'session';
  const lead = !!(s && s.needs_attention);                                 // the card that waits for you carries the tinted primary; the others are quiet
  const chip = handed && t.tmux ? el('a', { class: 'chip-btn tk-chip', href: taskPeekHash(t.tmux), title: `this task runs in ${sessionNameOf(t.tmux)}`, text: `in ${sessionNameOf(t.tmux)}` }) : null;
  const card = el('div', { class: 'task' + (s && s.needs_attention ? ' attn' : '') + (handed ? ' handed' : ''), 'data-task': t.id },
    el('div', { class: 'row' }, el('span', { class: 'title', text: t.title }), s ? stateBadge(s) : el('span', { class: 'state ended', text: 'no session' }), ciBadge(t)),
    el('div', { class: 'meta' }, starting ? `${taskWhere(t)} · starting…` : (t.branch ? `${t.project}/${t.repo} · ${t.branch}` : taskWhere(t) + (t.mode === 'attached' ? ' · in place' : '')), t.pr_url ? ' · PR #' + t.pr_number : null,
      typeof t.cost_usd === 'number' ? [' · ', el('span', { class: 'mono', text: '$' + t.cost_usd.toFixed(2) })] : null, chip ? ' ' : null, chip),
    s && s.last_message ? el('div', { class: 'last', text: s.last_message.slice(0, 160) }) : null,
    (t.overlap && t.overlap.length) ? el('div', { class: 'last bad', title: t.overlap.map(o => `${o.title}: ${o.files.join(', ')}`).join('\n'),
      text: '⚠ overlaps ' + t.overlap.map(o => `"${o.title}" (${o.files.length} file${o.files.length === 1 ? '' : 's'}: ${o.files.slice(0, 3).join(', ')}${o.files.length > 3 ? '…' : ''})`).join('; ') }) : null,
    el('div', { class: 'actions' },
      starting ? null : el('a', { class: 'btn' + (lead ? ' primary tinted' : ''), href: `/term/${encodeURIComponent(t.tmux)}`, target: '_blank', rel: 'noopener' }, ic('console'), 'Terminal'),
      t.branch ? el('button', { onclick: () => openTaskModal(t), text: t.pr_url ? 'Diff / PR' : 'Diff / PR…' }) : null,
      t.pr_url ? el('a', { class: 'btn', href: t.pr_url, target: '_blank', rel: 'noopener', text: 'PR' }) : null,
      t.preview_url ? el('a', { class: 'btn', href: t.preview_url, target: '_blank', rel: 'noopener', text: `Preview :${t.preview_port}` }) : null,
      starting ? null : (t.preview_url ? el('button', { onclick: async () => { try { await api('DELETE', `/api/tasks/${t.id}/preview`); } catch (e) { setError(e.message); } await poll(true); }, title: 'stop exposing the preview', text: '⏏' }) :
        el('button', { onclick: async () => {
          try { await taskPreview(t); }
          catch (e) { if (/no listening port/.test(e.message)) taskPortSheet(t, e.message); else setError(e.message); }
          await poll(true);
        }, title: 'expose a dev server running in this session on its own tailnet HTTPS port', text: 'Preview' })),
      ciFail ? el('button', { class: 'tk-fixci', title: 'send the failing CI logs to the session', onclick: async () => { try { const r = await api('POST', `/api/tasks/${t.id}/fix-ci`); setError(null); toast(`CI logs (${r.chars} chars) sent to ${t.title}${r.relaunched ? ' (session relaunched)' : ''}`, { kind: 'ok', ttl: 8000 }); } catch (e) { setError(e.message); } await poll(true); }, text: 'Fix CI' }) : null,
      t.pr_url ? el('button', { onclick: async () => { try { await api('POST', `/api/tasks/${t.id}/refresh`); } catch (e) { setError(e.message); } await poll(true); }, title: 'refresh PR / CI status', text: '↻' }) : null,
      starting ? null : confirmButton('arch:' + t.id, 'Archive', async () => {
        try { await api('POST', `/api/tasks/${t.id}/archive`, { force: false }); }
        catch (e) {
          if (/force/.test(e.message) && window.confirm(e.message + '\n\nDiscard the worktree anyway?')) await api('POST', `/api/tasks/${t.id}/archive`, { force: true });
          else throw e;
        }
      }, true)));
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

/* openSheet({title, body, actions?, placement?, onClose?, back?}) -> { dialog, body, close }. dialog#sheet through showModal(): a right panel from 840 px up,
   a bottom sheet below (placement 'right' | 'bottom' forces one). Calling it again while open swaps the content in place. body and actions
   take a node, an array of nodes or a string. Esc, the backdrop and the close button end it; closeSheet() does the same from code.
   back {label, onClick} puts a chevron button before the title (the way up one level: the repo picker behind a form). The dialog itself takes
   the focus on open, so the browser does not ring the close button; a form's focusFirst (focusFine) then moves it on to the first field, on a
   fine pointer only. */
function sheetKids(x) { return x === null || x === undefined || x === false ? [] : (Array.isArray(x) ? x : [x]).flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false); }

function openSheet(opts) {
  const dlg = document.getElementById('sheet');
  if (!dlg) return null;
  const o = opts || {};
  if (!dlg.dataset.wired) {
    dlg.dataset.wired = '1';
    try { dlg.style.outline = 'none'; } catch (_) { /* no CSSOM */ }                // the dialog takes the focus on open (below): no ring around the whole sheet
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
    el('div', { class: 'sheet-head' }, el('h2', { class: 'sheet-title' }, o.back && typeof o.back.onClick === 'function'
      ? el('button', { class: 'icon minimal sheet-back', type: 'button', 'aria-label': o.back.label || 'Back', title: o.back.label || 'Back', onclick: o.back.onClick }, ic('chevron-left')) : null, o.title || ''),
      el('button', { class: 'icon minimal', type: 'button', 'aria-label': 'Close', title: 'Close', onclick: () => closeSheet() }, ic('cross'))),
    body);
  if (actions.length) dlg.append(el('div', { class: 'sheet-actions' }, ...actions));   // Element.append(null) would add the text "null"
  dlg._onClose = typeof o.onClose === 'function' ? o.onClose : null;
  if (!dlg.open) { try { dlg.showModal(); } catch (_) { dlg.setAttribute('open', ''); } }
  try { dlg.setAttribute('tabindex', '-1'); dlg.focus(); } catch (_) { /* no focus API */ }
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

/* A send row with a draft says so (class has-text on the textarea's parent, set by composerBind): the Send button is a tinted primary while the
   box is empty and a filled one once there is text, so the box you are about to send stands out. composerGrow is the one choke point: every
   path that changes the text (typing, the newline key, a send that clears the box, an import that fills it) calls it. */
function composerMark(ta) {
  const row = ta._rowMark ? ta.parentNode : null;
  if (row && row.classList) row.classList.toggle('has-text', !!String(ta.value || '').trim());
}

function composerGrow(ta, maxRows) {
  if (!ta || !ta.style) return;
  composerMark(ta);
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
  ta._rowMark = true;
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

/* The keyboard hint a caller appends to a placeholder ("reply to s1 · ⇧Enter new line"): meaningless on a touch keyboard, and on a phone it wraps
   to a second line the one-row box cuts off. */
const COMPOSER_HINT = /\s·\s⇧Enter new line$/;

/* Build a composer textarea: opts {placeholder, label, id, onSend, maxRows}. On a coarse pointer a placeholder that carries the hint suffix is cut down to its
   bare verb ("Reply…": one short line in the one-row box, never wrapped and clipped, whatever the session is called) and the full text moves to the title
   (the label stays the accessible name). style.css also keeps any placeholder on one line with an ellipsis. */
function composer(opts) {
  const o = opts || {};
  const full = o.placeholder || 'send';
  const bare = String(full).replace(COMPOSER_HINT, '').trim().split(/\s+/)[0];
  const short = coarsePointer() && COMPOSER_HINT.test(full) && bare ? bare.charAt(0).toUpperCase() + bare.slice(1) + '…' : full;
  const ta = el('textarea', { class: 'composer', rows: '1', placeholder: short, 'aria-label': o.label || full, title: short === full ? null : full,
    autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', enterkeyhint: 'send', id: o.id || null });
  return composerBind(ta, o);
}

/* A ↵ button that inserts a newline into `ta`; pointerdown is cancelled so focus (and the soft keyboard) stays on the box. */
function newlineButton(ta) {
  return el('button', { class: 'minimal small nl', type: 'button', title: 'newline (Shift+Enter)', 'aria-label': 'insert newline',
    onpointerdown: (e) => e.preventDefault(), onclick: () => composerInsertNewline(ta) }, '↵');
}

/* ---- modal <dialog>s without window.prompt: a small shell and the quick-reply editor -----------------------------
   modalShell(cls, label, onClose) -> { dlg, close, show }: a <dialog class=cls> that is appended to <body> on show(), takes Esc and a
   backdrop click as "cancel" (handled here, so the same code runs where a browser would not fire `cancel`) and removes itself when it
   closes. The dialog has no padding of its own: a click that lands on the dialog itself is the backdrop. Definitions only.
   On a phone the soft keyboard shrinks the VISUAL viewport but not the layout viewport a centred dialog is placed in, which left Save under the
   keyboard. So the dialog is anchored to the top of the visual viewport (style.css dialog.qr-editor: position fixed, top = --vvt + 12 px,
   max-height from --vvh), and show() keeps those two custom properties on the dialog itself, from visualViewport, for as long as it is open (the
   terminal page's TermKit.viewportFit keeps them on <html> only, and the other pages have none). Under a coarse pointer show() also puts the focus on
   the dialog, so the browser does not hand it to the first input and pop the keyboard up before anything was tapped. */
function modalFit(dlg) {
  const vv = window.visualViewport || null;
  const put = () => {
    if (!dlg.style || typeof dlg.style.setProperty !== 'function') return;
    const h = Math.floor(vv ? vv.height : window.innerHeight);
    if (h > 0) dlg.style.setProperty('--vvh', h + 'px');
    dlg.style.setProperty('--vvt', Math.max(0, Math.floor(vv ? vv.offsetTop : 0)) + 'px');
  };
  put();
  if (!vv || typeof vv.addEventListener !== 'function') return () => {};
  vv.addEventListener('resize', put);                                // the keyboard came up (or went away) after the dialog opened
  vv.addEventListener('scroll', put);                                // iOS pans the visual viewport inside the layout one
  return () => { vv.removeEventListener('resize', put); vv.removeEventListener('scroll', put); };
}

function modalShell(cls, label, onClose) {
  const dlg = el('dialog', { class: cls, 'aria-label': label });
  let done = false;
  let unfit = null;
  const gone = () => { if (done) return; done = true; if (unfit) { unfit(); unfit = null; } dlg.remove(); if (typeof onClose === 'function') onClose(); };   // once, whichever of close() and the close event comes first
  const close = () => {
    if (dlg.open) { try { dlg.close(); } catch (_) { dlg.removeAttribute('open'); } }
    gone();                                                          // a browser fires `close` a task later; callers (a second long press) must not see the old dialog
  };
  dlg.addEventListener('close', gone);
  dlg.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); close(); } });
  dlg.addEventListener('click', (e) => { if (e.target === dlg) close(); });
  const show = () => {
    document.body.append(dlg);
    const touch = coarsePointer();
    if (touch) { dlg.setAttribute('tabindex', '-1'); dlg.setAttribute('autofocus', ''); }   // autofocus: where showModal honours it, the dialog itself is the target
    unfit = modalFit(dlg);
    try { dlg.showModal(); } catch (_) { dlg.setAttribute('open', ''); }
    if (touch) { try { dlg.focus(); } catch (_) { /* nothing to focus */ } }   // not the first input: no soft keyboard until the person taps one
  };
  return { dlg, close, show };
}

/* ---- quick replies: one list per session in localStorage `ccboard:quick:<tmux>`, shared by the terminal page and the peek ---------
   Stored as a JSON array of lines; no key (or a list equal to the defaults) means the agent defaults. quickClean trims, drops empty and
   duplicate lines and keeps at most QUICK_MAX. */
const QUICK_DEFAULTS = ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'];   // the agent nudges (SESSION_NUDGES in pages/agents.js)
const QUICK_MAX = 12;
const QUICK_LINE_MAX = 200;
const QUICK_HOLD_MS = 500;
let quickEditing = null;                                             // the editor that is open, if any (a second long-press must not stack another)

function quickKey(tmux) { return 'ccboard:quick:' + tmux; }

function quickClean(list) {
  const out = [];
  for (const x of Array.isArray(list) ? list : []) {
    const t = String(x === null || x === undefined ? '' : x).trim();
    if (t && !out.includes(t)) out.push(t);
    if (out.length >= QUICK_MAX) break;
  }
  return out;
}

function quickLoad(tmux) {
  try {
    const raw = localStorage.getItem(quickKey(tmux));
    if (raw) { const v = JSON.parse(raw); if (Array.isArray(v)) return quickClean(v); }
  } catch (_) { /* no storage, or not JSON: the defaults */ }
  return QUICK_DEFAULTS.slice();
}

/* Save the cleaned list; null (or a list equal to the defaults) removes the key. Returns the list that is in effect. */
function quickSave(tmux, items) {
  const list = items === null ? QUICK_DEFAULTS.slice() : quickClean(items);
  const same = list.length === QUICK_DEFAULTS.length && list.every((x, i) => x === QUICK_DEFAULTS[i]);
  try {
    if (same) localStorage.removeItem(quickKey(tmux)); else localStorage.setItem(quickKey(tmux), JSON.stringify(list));
  } catch (_) { /* storage may be unavailable: the list lasts until the page closes */ }
  return list;
}

/* quickReplyEditor({items, defaults?, onSave(items)}) -> { dialog, close, save, values } | the editor already open.
   A <dialog class="qr-editor">: one row per reply (input + quiet remove), "+ add" and Reset (quiet text buttons; Reset puts the defaults in the rows, saved only with
   Save), Cancel (the default bordered button) and Save (the one filled primary). Enter in a row saves, Esc and the backdrop cancel (onSave is not called).
   Limits: at most QUICK_MAX (12) replies ("+ add" stops at twelve) and QUICK_LINE_MAX (200) characters each; a longer line is not cut silently: Save refuses and
   says so in the inline error under the list (--bad-fg, role alert), with the row marked aria-invalid and focused. Blank and duplicate lines are dropped on Save.
   16 px inputs on coarse pointers come from style.css. */
function quickReplyEditor(opts) {
  if (quickEditing) return quickEditing;
  const o = opts || {};
  const defaults = quickClean(Array.isArray(o.defaults) ? o.defaults : QUICK_DEFAULTS);
  let saved = false;
  const shell = modalShell('qr-editor', 'Edit quick replies', () => { quickEditing = null; });
  const list = el('div', { class: 'qr-list' });
  const count = el('span', { class: 'dim qr-count' });
  const err = el('p', { class: 'qr-err bad', role: 'alert' });
  const addBtn = el('button', { type: 'button', class: 'minimal qr-add', onclick: () => { if (rows().length < QUICK_MAX) { const r = addRow(''); sync(); r.querySelector('input').focus(); } }, text: '+ add' });
  const rows = () => Array.from(list.querySelectorAll('.qr-row'));
  const inputs = () => Array.from(list.querySelectorAll('input'));
  const values = () => inputs().map((i) => i.value);
  function clearError() { err.textContent = ''; for (const i of inputs()) i.removeAttribute('aria-invalid'); }
  function sync() {
    const n = rows().length;
    addBtn.disabled = n >= QUICK_MAX;
    if (n >= QUICK_MAX) addBtn.setAttribute('title', 'At most ' + QUICK_MAX + ' replies'); else addBtn.removeAttribute('title');
    count.textContent = n + ' of ' + QUICK_MAX;
  }
  function addRow(text) {
    const input = el('input', { type: 'text', class: 'qr-input', 'aria-label': 'Quick reply', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', enterkeyhint: 'done' });
    input.value = text;
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); save(); } });
    input.addEventListener('input', clearError);
    const row = el('div', { class: 'qr-row' }, input,
      el('button', { type: 'button', class: 'icon minimal qr-rm', title: 'Remove', 'aria-label': 'Remove this reply', onclick: () => { row.remove(); sync(); clearError(); } }, ic('cross')));
    list.append(row);
    return row;
  }
  function fill(items) { list.textContent = ''; for (const t of items) addRow(t); sync(); clearError(); }
  /* '' when the rows are fine, else the message for the inline error (and the first offending input) */
  function problem() {
    const ins = inputs();
    if (ins.length > QUICK_MAX) return { msg: 'At most ' + QUICK_MAX + ' replies (this has ' + ins.length + ').', input: ins[QUICK_MAX] };
    const long = ins.find((i) => i.value.trim().length > QUICK_LINE_MAX);
    if (long) { const v = long.value.trim(); return { msg: 'A reply is at most ' + QUICK_LINE_MAX + ' characters ("' + v.slice(0, 24) + '…" has ' + v.length + ').', input: long }; }
    return null;
  }
  function save() {
    if (saved) return;
    const bad = problem();
    if (bad) {
      err.textContent = bad.msg;
      bad.input.setAttribute('aria-invalid', 'true');
      try { bad.input.focus(); } catch (_) { /* nothing to focus */ }
      return;
    }
    saved = true;
    const items = quickClean(values());
    try { if (typeof o.onSave === 'function') o.onSave(items); } catch (e) { console.error('ccboard quickReplyEditor onSave', e); }
    shell.close();
  }
  fill(quickClean(Array.isArray(o.items) ? o.items : defaults));
  shell.dlg.append(el('form', { class: 'qr-box', onsubmit: (e) => { e.preventDefault(); save(); } },
    el('h2', { class: 'qr-title', text: 'Quick replies' }),
    el('p', { class: 'dim qr-hint', text: 'One per row, sent to the session with Enter. Up to ' + QUICK_MAX + ', ' + QUICK_LINE_MAX + ' characters each.' }),
    list, err,
    el('div', { class: 'qr-tools' }, addBtn, el('button', { type: 'button', class: 'minimal qr-reset', title: 'Back to the default replies', onclick: () => fill(defaults), text: 'Reset' }), count),
    el('div', { class: 'qr-actions' },
      el('button', { type: 'button', class: 'qr-cancel', onclick: () => shell.close(), text: 'Cancel' }),
      el('button', { type: 'submit', class: 'primary qr-save', text: 'Save' }))));
  const ctl = { dialog: shell.dlg, close: shell.close, save, values };
  quickEditing = ctl;
  shell.show();
  if (!coarsePointer()) { const first = list.querySelector('input'); if (first) first.focus(); }
  return ctl;
}

/* quickChip(text, {cls?, onSend(button), onEdit}) -> a button that sends on a tap and opens the editor on a long press (500 ms; right click
   does too, which is also what a long press on touch becomes). The tap that ends a long press is swallowed. Callers that must not steal
   focus from the composer add their own pointerdown preventDefault (term.js keep()). */
function quickChip(text, opts) {
  const o = opts || {};
  let timer = null;
  let held = false;
  const stop = () => { if (timer !== null) { clearTimeout(timer); timer = null; } };
  const edit = () => { stop(); held = true; if (typeof o.onEdit === 'function') o.onEdit(); };
  const b = el('button', { type: 'button', class: o.cls || '', title: 'tap to send · hold to edit', text });
  b.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse' && e.button) return;
    held = false;
    stop();
    timer = setTimeout(() => { timer = null; edit(); }, QUICK_HOLD_MS);
  });
  for (const t of ['pointerup', 'pointercancel', 'pointerleave']) b.addEventListener(t, stop);
  b.addEventListener('contextmenu', (e) => { e.preventDefault(); edit(); });
  b.addEventListener('click', (e) => {
    if (held && e.detail) { held = false; e.preventDefault(); return; }       // the lift that ends a long press (a keyboard click has detail 0 and always sends)
    held = false;
    if (typeof o.onSend === 'function') o.onSend(b);
  });
  return b;
}
