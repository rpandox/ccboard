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

/* A small "Copy" button for a command or a link (v0.5.19, the Doctor's fixes and the wizard's commands): copies `text` to the clipboard, says so in a toast and
   reads "Copied" for a moment. Without a clipboard (an insecure origin) it says to select the text instead. `what` names the thing for the screen reader and the toast. */
function copyButton(text, what) {
  const name = what ? String(what) : 'text';
  const btn = el('button', { class: 'small copy-btn', type: 'button', 'aria-label': `Copy ${name}`, title: `Copy ${name}`, text: 'Copy' });
  let timer = null;
  btn.addEventListener('click', async () => {
    let ok = false;
    try { await navigator.clipboard.writeText(String(text)); ok = true; } catch (_) { ok = false; }
    const msg = ok ? `${name.charAt(0).toUpperCase()}${name.slice(1)} copied` : `Copy failed: select the ${name} and copy it`;
    if (typeof pageToast === 'function') pageToast(msg, ok ? 'ok' : 'warn');
    else if (typeof toast === 'function') toast(msg, { kind: ok ? 'ok' : 'warn' });
    if (!ok) return;
    btn.textContent = 'Copied';
    clearTimeout(timer);
    timer = setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
    if (timer && typeof timer.unref === 'function') timer.unref();
  });
  return btn;
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

/* #96: a Codex session that took a prompt 30 s ago and sent the board no hook (s.hooks_missing from the state builder: 'untrusted' | 'bypass' | null; one
   rule, agents/codex.hooks_missing, so every surface agrees). The chip is a link to Settings > Doctor (the Codex checks), muted amber, with text and a
   glyph; its explanation is the title here and visible text where there is room (the peek). Never a state: the row's state stays hook-driven. */
const HOOKS_MISSING_LABEL = 'no hooks (untrusted?)';
const HOOKS_MISSING_HREF = '/#/settings?sec=doctor';
function hooksMissingWhy(kind) {
  return kind === 'bypass'
    ? 'No hook event since your first prompt, although CCBOARD_CODEX_HOOK_TRUST=bypass is set and hooks should be running. Check the Codex rows in Settings > Doctor (hooks installed, hooks feature on).'
    : 'No hook event since your first prompt. Trust the ccboard hooks once: type /hooks in a Codex terminal. See Settings > Doctor.';
}
function hooksMissingChip(kind, cls) {
  const why = hooksMissingWhy(kind);
  return el('a', { class: 'bdg bdg-hooks' + (cls ? ' ' + cls : ''), href: HOOKS_MISSING_HREF, title: why, 'aria-label': `${HOOKS_MISSING_LABEL}. ${why}`,
    onclick: (e) => e.stopPropagation() }, el('span', { class: 'bdg-hooks-g', 'aria-hidden': 'true', text: '◇' }), ' ' + HOOKS_MISSING_LABEL);
}

function stateBadge(s) {
  const parked = limitParkedText(s);                          // #71: parked on a rate limit: amber, glyph and words, not a bare "error"
  const st = parked ? 'waiting' : s.state || 'unknown';
  if (!STATE_LABEL[st]) return null;
  const age = s.state_at ? fmtAge(Date.parse(s.state_at) / 1000) : '';
  // beside its text label the glyph is decoration: hide it from screen readers (no double announcement) and let the
  // badge's own title (the last hook event) show on hover
  const glyph = stateGlyph(st);
  glyph.setAttribute('aria-hidden', 'true');
  glyph.removeAttribute('title');
  return el('span', { class: `state ${st}` + (s.needs_attention ? ' attn' : ''), title: s.last_event || '' },
    glyph, ' ', parked || STATE_LABEL[st] + (age ? ` ${age}` : ''));
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

/* ---- the launcher entry (v0.5.13): ONE call for every create entry point ----
   launch({mode: 'session' | 'task' | 'dispatch', project, repo, agent?, task?, ...}) opens the launcher sheet (launcher.js openLauncher). project and repo are the OBJECTS of
   state.projects (a project and one of its repos, or its root, the pair sessionForm and taskForm take), not names; a dispatch carries the task. The topbar +, the Home block,
   the project page, the sidebar, the quad's empty tiles, the dock and the Move sheet's Options… all call this, so the sheet is built in one place. Without openLauncher
   (a partial deploy, a test world) the older forms open instead: Shell.showForm for a session or a task when the shell is there, else the form in the sheet; the dispatch
   sheet for a dispatch. true when something opened (else false: no place, no task). */
function launch(o) {
  const opts = o || {};
  if (typeof openLauncher === 'function') return openLauncher(opts) !== false;
  if (opts.mode === 'dispatch') {
    if (!opts.task || typeof taskDispatchSheet !== 'function') return false;
    taskDispatchSheet(opts.task, { agent: opts.agent, session: opts.session, auto_close: opts.auto_close });
    return true;
  }
  const kind = opts.mode === 'task' ? 'task' : 'session';
  const p = opts.project;
  const r = opts.repo;
  if (!p || !r) return false;
  const label = opts.label || (r === p.root || r.root ? `${p.name} · project folder` : `${p.name}/${r.name}`);
  if (typeof Shell !== 'undefined' && Shell && typeof Shell.showForm === 'function') { Shell.showForm(kind, { p, r, label }); return true; }
  ui.openForm = 'sheet';
  const done = () => { if (typeof closeSheet === 'function') closeSheet(); };
  openSheet({ title: `${kind === 'task' ? 'New task' : 'New session'} · ${label}`,
    body: kind === 'task' ? taskForm(p, r, { onDone: done, onCancel: done }) : sessionForm(p, r), onClose: () => { if (ui.openForm === 'sheet') ui.openForm = null; } });
  return true;
}

/* Where a new session (kind 'session') or task (kind 'task') starts when nobody picked a place, so an entry point can skip the repo picker: {project, repo} (state objects)
   or null. In a project: the repo (or the project folder) of its most recent session, else its only repo, else the project folder, else its first repo. A task needs a
   place that takes one and prefers the repo a task was last started in. project null looks at every project: the one with the most recent session, else the first that
   has any place. */
function launchPlace(project, kind) {
  const st = typeof state !== 'undefined' ? state : null;
  const projects = project ? [project] : ((st && st.projects) || []);
  const forTask = kind === 'task';
  const okRepo = (r) => r.state === 'ok' || r.state === 'unknown';
  const fits = (p, r) => !!r && (forTask ? taskTarget(p, r) : (r === p.root || okRepo(r)));
  const when = (s) => { const t = s.state_at ? Date.parse(s.state_at) / 1000 : NaN; return Number.isFinite(t) ? t : (s.created || 0); };
  let best = null;
  let at = -1;
  for (const p of projects) {
    for (const r of [p.root, ...(p.repos || [])]) {
      if (!fits(p, r)) continue;
      for (const s of (r.sessions || [])) { const t = when(s); if (t > at) { at = t; best = { project: p, repo: r }; } }
    }
  }
  const only = (p) => {                                                    // no session to go by: a task's last repo, the only repo, the folder, the first repo
    const last = forTask && typeof taskLastRepo === 'function' ? taskLastRepo(p.name) : '';
    const hit = last ? (last === 'root' ? p.root : (p.repos || []).find((x) => x.name === last)) : null;
    if (hit && fits(p, hit)) return hit;
    const ok = (p.repos || []).filter((x) => fits(p, x));
    if (ok.length === 1) return ok[0];
    if (p.root && fits(p, p.root)) return p.root;
    return ok[0] || null;
  };
  if (forTask && project) {                                                // the repo a task went to last beats the busiest one
    const last = typeof taskLastRepo === 'function' ? taskLastRepo(project.name) : '';
    const hit = last ? (last === 'root' ? project.root : (project.repos || []).find((x) => x.name === last)) : null;
    if (hit && fits(project, hit)) return { project, repo: hit };
  }
  if (best) return best;
  for (const p of projects) { const r = only(p); if (r) return { project: p, repo: r }; }
  return null;
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
  if (o._keys && o._keys.includes('autoclose') && JSON.stringify(t.autoclose || null) !== JSON.stringify(o.autoclose || null)) return false;      // Keep open: until the poll has no countdown
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

/* What a limit gate object ({kind, resets_at, pct}) says: 'the 5h usage window is at 91% (resets 16:15)'. */
function taskLimitText(g) {
  if (!g || typeof g !== 'object') return typeof g === 'string' ? g : '';
  const at = taskMs(g.resets_at);
  const when = at ? ` (resets ${taskClock(at)})` : '';
  const pct = typeof g.pct === 'number' ? ` at ${Math.round(g.pct)}%` : '';
  if (g.kind === '5h' || g.kind === '7d') return `the ${g.kind} usage window is${pct || ' nearly used'}${when}`;
  if (g.kind === 'backoff') return `the scheduler is backing off after a limit${when}`;
  return `a usage limit is active${when}`;
}

/* A response that carries limit_warning (the Claude 5h or 7d window is nearly used): a hand dispatch is never blocked, only warned. */
function taskWarn(res) {
  const w = res && res.limit_warning;
  if (!w) return;
  toast(`Started anyway: ${taskLimitText(w) || 'close to the usage limit'}`, { kind: 'warn', ttl: 9000 });
}

/* Start: a new session in the task's own worktree. The card moves to In progress at once; the response (or an error that puts it back) settles it.
   opts {agent, auto_close, extra}: agent (when it is not the task's own) and auto_close go in the body, extra is merged over it (launch options). */
async function taskStart(t, opts) {
  const o = opts || {};
  const cur = store.tasksOverride[t.id];
  if (cur && cur._busy) return;                                           // a double tap
  const own = t.agent || 'claude';
  const agent = o.agent || own;
  const body = { mode: 'lane' };
  if (agent !== own) body.agent = agent;
  if (o.auto_close !== undefined && o.auto_close !== null) body.auto_close = !!o.auto_close;
  if (o.extra && typeof o.extra === 'object') Object.assign(body, o.extra);
  const pending = { ...t, agent, phase: 'running', column: 'in_progress', tmux: '', session_row: null, mode: 'worktree', session: taskStartingSession(), closed_at: null, autoclose: null };
  taskOverrideSet(pending, ['agent', 'phase', 'column', 'tmux', 'session_row', 'mode', 'session'], { _busy: true });
  taskRepaint();
  try {
    const res = await api('POST', `/api/tasks/${t.id}/dispatch`, body);
    const row = taskRowFromResponse(pending, res, { mode: 'worktree' });
    if (!row.tmux && taskDemo()) row.tmux = `${t.project}--${t.repo}--t-${t.slug}`;
    taskOverrideSet(row);
    toast(`started ${row.slug || t.slug}`, { kind: 'ok' });
    taskWarn(res);
    taskRepaint();
    taskOpenPeek(row.tmux);
    if (typeof poll === 'function') poll(true);
  } catch (e) {
    taskOverrideDrop(t.id);
    taskRepaint();
    taskFail(e);
  }
}

/* Sessions of the task's project that could take its prompt: Claude and Codex sessions that are not ended, ready ones (idle, done, waiting at its idle
   prompt: wait_kind 'idle_prompt') first and those in the task's own repo before the others. A waiting session in a permission prompt or a dialog
   is listed but off ('waiting on a prompt': typing into it would answer it), and so is one that has just started (state unknown: 'starting…') and one
   of the other agent ('a Codex session: this task is for Claude': the server answers 409 for it).
   [{s, repo, same, ok, why, agent}] */
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
    const agent = sessionAgent(s);
    if ((agent !== 'claude' && agent !== 'codex') || stt === 'ended' || s.name === 'clone') return;
    const want = t.agent || 'claude';
    let why = '';
    let queue = false;                                   // a working Claude session takes the prompt into its queue (the server pastes it with queue:true)
    if (agent !== want) why = `a ${AGENT_NAME[agent]} session: this task is for ${AGENT_NAME[want] || want}`;
    else if (stt === 'working' && agent === 'claude') queue = true;
    else if (stt === 'working') why = 'working: wait for its turn to end';
    else if (stt === 'waiting' && pend.has(s.tmux)) why = 'waiting for a permission decision';
    else if (stt === 'waiting' && s.wait_kind !== 'idle_prompt') why = 'waiting on a prompt';
    else if (stt === 'unknown') why = 'starting…';
    else if (stt !== 'idle' && stt !== 'done' && stt !== 'waiting') why = stt === 'errored' ? 'stopped with an error' : stt;
    out.push({ s, repo, same: repo === t.repo, ok: !why, why, agent, queue, note: queue ? 'will be queued after the current turn' : '' });
  };
  for (const r of repoGroups(p)) for (const s of (r.sessions || [])) take(s, r.name);
  for (const s of (p.orphan_sessions || [])) take(s, s.repo || '?');
  const rank = { idle: 0, done: 1, waiting: 2, working: 3 };
  out.sort((a, b) => (b.ok - a.ok) || (b.same - a.same) || ((rank[a.s.state] ?? 9) - (rank[b.s.state] ?? 9)) || String(b.s.state_at || '').localeCompare(String(a.s.state_at || '')));
  return out;
}

/* The repo-mismatch 409 ({error, mismatch: {task, session}}): api() keeps the body on the error (err.body, err.status), so that is what is
   tested first; the message is only the fallback for an error that was built without a body. */
function taskIsMismatch(e) { return !!(e && ((e.body && e.body.mismatch) || e.mismatch || /another repo|mismatch/i.test(e.message || ''))); }

/* Hand the task's prompt to a running session. A session of another repo answers 409 (mismatch): ask, then retry with force.
   extra {auto_close, ...} is merged into the body (the Options form, the drop confirm). */
async function taskSend(t, target, force, extra) {
  const tmux = target.s.tmux;
  const cur = store.tasksOverride[t.id];
  if (cur && cur._busy) return;
  const pending = { ...t, phase: 'running', column: 'in_progress', tmux, session_row: target.s.row_id === undefined ? null : target.s.row_id, mode: 'session', session: taskStartingSession() };
  taskOverrideSet(pending, ['phase', 'column', 'tmux', 'session_row', 'mode', 'session'], { _busy: true });
  taskRepaint();
  try {
    const body = force ? { session: tmux, force: true } : { session: tmux };
    if (target.queue) body.queue = true;                 // a working Claude session: the server pastes the prompt into Claude's queue
    if (extra && typeof extra === 'object') Object.assign(body, extra);
    const res = await api('POST', `/api/tasks/${t.id}/dispatch`, body);
    taskOverrideSet(taskRowFromResponse(pending, res, { mode: 'session', tmux }));
    toast(`sent to ${target.s.name}`, { kind: 'ok' });
    taskWarn(res);
    taskRepaint();
    if (typeof poll === 'function') poll(true);
  } catch (e) {
    taskOverrideDrop(t.id);
    taskRepaint();
    if (!force && taskIsMismatch(e)) {
      const ask = typeof window !== 'undefined' && typeof window.confirm === 'function'
        && window.confirm(`${e.message}\n\n${target.s.name} works in ${target.repo}, this task is in ${t.repo}. Work there anyway? The prompt goes in with "Work in <this task's repo>." in front.`);
      if (ask) await taskSend(t, target, true, extra);
      return;
    }
    taskFail(e);
  }
}

/* ---------- tasks v2, second half (v0.5.14b / v0.5.15): the Move sheet, a long press, owner / auto-close / result / chain chips ----------
   The fields the board reads from state.tasks[] beyond v0.5.14a (app/main.py _tasks_view, app/taskflow.py):
     autoclose  {task, due} | {task, held: 'question'} | {task, closing: true} | {task, due, waiting: why} | null
                the session's flags.autoclose while it is this task's: due is an ISO time (a countdown), held 'question' means the last message asked something, so the
                session stays and the card says 'needs you' (the server also puts the card in the needs_you column), waiting says why a guard postponed the close
     closed_at  ISO | null       the session was closed after the stop (sessions.ended_reason 'auto_close')
     result     the first 300 characters of the result (GET /api/tasks/{id} has all of it), result_at, done_at
     chain      {i, n} | null    step i of n (taskflow.chain_positions); chain_id and parent_id link the steps, taskChainInfo works the position out from them when chain is absent
     limit_hold {kind: '5h'|'7d'|'limit'|'backoff', resets_at (epoch seconds), pct} | null     a queued step the dispatch gate holds back
     turns_ahead  integer       a prompt handed to a working Claude session: the Stops that come before its own turn; above 0 the card says 'queued' instead of the session's state
   and from a dispatch answer: limit_warning, the same object, when a hand start went ahead inside the limit window. */

const TASK_HOLD_MS = 450;               // a touch held this long on a card opens the Move sheet
const TASK_HOLD_SLOP = 8;               // px of travel that makes it a scroll or a swipe instead
const AGENT_NAME = { claude: 'Claude', codex: 'Codex' };

/* ---- schedules per agent (v0.5.16): the words a job and a run share on the Schedules tab and the Home schedules sheet ---- */

/* ' · ≤30 turns · ≤$2' for a Claude job; ' · gpt-5.5 · high reasoning' for a Codex one (Codex has no turn or budget limit, its model and reasoning are the job's own options). '' when there is nothing to say. */
function jobLimitText(j) {
  if (!j) return '';
  if (j.agent === 'codex') {
    const o = j.opts && typeof j.opts === 'object' ? j.opts : {};
    return ` · ${o.model || 'default model'}${o.reasoning_effort ? ' · ' + o.reasoning_effort + ' reasoning' : ''}`;
  }
  return ` · ≤${j.max_turns} turns${j.max_budget_usd ? ' · ≤$' + j.max_budget_usd : ''}${jobFableText(j)}`;
}

/* A Claude job whose model resolves to Fable (state.jobs[].fable, issue #108): ' · Fable acknowledged' with its cap, or ' · held: needs a Fable acknowledgement and a Max $' (it does not run until the owner gives both). */
function jobFableText(j) {
  const f = j && j.fable;
  if (!f) return '';
  return f.state === 'held' ? ' · held: waiting for a Fable acknowledgement and a Max $' : ` · Fable acknowledged${f.cap ? ' (≤$' + f.cap + ')' : ''}`;
}

/* The button of a held Fable job: opens the acknowledgement sheet (launcher.js jobFableSheet). null for every other job. */
function jobFableButton(j) {
  if (!j || !j.fable || j.fable.state !== 'held' || typeof jobFableSheet !== 'function') return null;
  return el('button', { type: 'button', class: 'primary tinted', onclick: () => jobFableSheet(j), text: 'Acknowledge Fable' });
}

/* One run as a line: 'run #12 12:30 · ok · $0.42 · 7 turns'. A Codex run reports no dollar cost (tokens only) and its turn count says nothing, so it shows neither. */
function runLineText(r, agent) {
  const codex = (r && r.agent || agent) === 'codex';
  return `run #${r.id} ${fmtTs(r.started_at)} · ${r.status}${typeof r.cost_usd === 'number' ? ' · $' + r.cost_usd.toFixed(2) : ''}${!codex && r.num_turns ? ' · ' + r.num_turns + ' turns' : ''}${r.error ? ' · ' + r.error : ''}`;
}

/* The window each agent's schedules wait on, in plain words: [{agent, text}] for the agents among `jobs` (all when none). Claude: the 5-hour window; Codex: its usage window. */
function schedWindowNotes(st, jobs) {
  const q = (st && st.scheduler) || {};
  const agents = new Set((jobs || []).map((j) => j.agent || 'claude'));
  if (!agents.size) { agents.add('claude'); agents.add('codex'); }
  const one = (name, w, label) => w.backoff_until ? `${name} is backing off until ${fmtTs(w.backoff_until)} after a rate-limited run` : w.known ? `${name} ${label} at ${Math.round(w.pct)}%` : `${name} ${label} unknown`;
  const out = [];
  if (agents.has('claude')) out.push({ agent: 'claude', text: one('Claude', q, '5-hour window') });
  if (agents.has('codex')) out.push({ agent: 'codex', text: one('Codex', q.codex || {}, 'usage window') });
  return out;
}
const taskUi = { open: {}, full: {}, timer: null, wired: false };   // open {task id: true}: result excerpts the person expanded; full {task id: text}: whole results fetched

function taskAgentInstalled(agent) {
  if (agent === 'claude') return true;
  const st = typeof state !== 'undefined' ? state : null;
  return !!(st && st.agents && st.agents[agent] && st.agents[agent].installed);
}

/* An epoch (seconds, or milliseconds) or an ISO string -> milliseconds since the epoch; 0 for anything else. */
function taskMs(v) {
  if (v === null || v === undefined || v === '' || typeof v === 'boolean') return 0;
  if (typeof v === 'number') return !isFinite(v) || v <= 0 ? 0 : (v < 1e11 ? v * 1000 : v);
  const t = Date.parse(String(v));
  return Number.isNaN(t) ? 0 : t;
}

function taskClock(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* The pending close of the task's session: {due (ms, 0 = none yet), held ('question' | ''), reason}, or null. */
function taskAutoclose(t) {
  const s = t && t.session;
  const a = t && (t.autoclose || (s && (s.autoclose || (s.flags && s.flags.autoclose))) || (t.flags && t.flags.autoclose));
  if (!a || typeof a !== 'object') return null;
  return { due: taskMs(a.due), held: a.held ? String(a.held) : '', closing: !!a.closing, waiting: a.waiting ? String(a.waiting) : '' };
}

/* {until (ms, 0 = unknown), kind, text} while the dispatch gate holds this card, else null. */
function taskLimitHold(t) {
  const h = t && t.limit_hold;
  if (!h) return null;
  const until = taskMs(typeof h === 'object' ? (h.resets_at || h.until || h.reset_at || h.at) : h);
  return { until, kind: typeof h === 'object' ? h.kind || '' : '', text: until ? `waiting for the limit window (resets ${taskClock(until)})` : 'waiting for the limit window' };
}

function taskServerChain(t) {
  const c = t && t.chain && typeof t.chain === 'object' ? t.chain : null;
  const step = Number(c ? (c.i ?? c.step ?? c.pos) : (t && (t.chain_step ?? t.chain_pos)));
  const of = Number(c ? (c.n ?? c.of ?? c.total ?? c.len) : (t && (t.chain_len ?? t.chain_total ?? t.chain_n)));
  return step >= 1 && of >= 1 ? { step, of } : null;
}

/* Position of every chained task in its chain, from the rows themselves (parent_id links): Map task id -> {step, of, chain}. A parent that is not
   in the list (archived) still counts: its child is at least step 2. A server-given position wins over the computed one. */
function taskChainInfo(list) {
  const groups = new Map();
  for (const t of list || []) {
    if (t.chain_id === null || t.chain_id === undefined || t.chain_id === '') continue;
    const k = String(t.chain_id);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(t);
  }
  const info = new Map();
  for (const [chain, steps] of groups) {
    const byId = new Map(steps.map((t) => [String(t.id), t]));
    const depth = (t, seen) => {
      if (t.parent_id === null || t.parent_id === undefined || t.parent_id === '') return 1;
      const p = byId.get(String(t.parent_id));
      if (!p) return 2;
      if (seen.has(p)) return 1;
      seen.add(p);
      return 1 + depth(p, seen);
    };
    const rows = steps.map((t) => ({ t, step: depth(t, new Set([t])) }));
    const of = rows.reduce((m, r) => Math.max(m, r.step), 1);
    for (const r of rows) info.set(r.t.id, { ...(taskServerChain(r.t) || { step: r.step, of }), chain });
  }
  return info;
}

/* The chain position of one card: from the board's context when it has one, else worked out from every row of the board (a card drawn by home.js renderTasks). */
function taskChainOf(t, ctx) {
  if (t.chain_id === null || t.chain_id === undefined || t.chain_id === '') return taskServerChain(t);
  const info = ctx && ctx.chain ? ctx.chain : taskChainInfo(boardTasks(typeof state !== 'undefined' ? state : null));
  return info.get(t.id) || taskServerChain(t);
}

/* ---- the auto-close countdown: text only, ticked once a second while a .tk-count is on the page ---- */

function taskCountdownText(ms) {
  const s = Math.max(0, Math.ceil(ms / 1000));
  return s <= 0 ? 'closing…' : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function taskTick() {
  let n = 0;
  for (const node of document.querySelectorAll('.tk-count')) {
    const due = Number(node.getAttribute('data-due')) || 0;
    if (!due) continue;
    n += 1;
    setText(node, taskCountdownText(due - Date.now()));
  }
  if (!n && taskUi.timer !== null) { clearInterval(taskUi.timer); taskUi.timer = null; }
}

function taskTickEnsure() {
  if (taskUi.timer !== null || typeof setInterval !== 'function') return;
  taskUi.timer = setInterval(taskTick, 1000);
}

/* ---- long press (touch): longPress(node, onFire, {ms, slop, skip}) -> {cancel}
   Fires onFire(event) after `ms` (450) of a held touch or pen (or any pointer while the screen is a coarse one). Cancelled by pointerup, pointercancel (the browser
   took the touch for a scroll), pointerleave, a scroll anywhere, or a move of more than `slop` (8) px. A press that starts on a button, link or field is
   left alone (skip(event) says so; the default does). The click that ends a fired press is swallowed, and a long press never opens the browser's own
   context menu. A mouse on a fine pointer does nothing: the card has its Move button and the m key, and desktop has drag and drop. */
function longPress(node, onFire, opts) {
  const o = opts || {};
  const ms = o.ms || TASK_HOLD_MS;
  const slop = o.slop || TASK_HOLD_SLOP;
  const skip = o.skip || ((e) => !!(e.target && typeof e.target.closest === 'function' && e.target.closest('a, button, input, select, textarea, summary')));
  let timer = null;
  let x0 = 0;
  let y0 = 0;
  let fired = false;
  const onMove = (e) => { if (Math.hypot((e.clientX || 0) - x0, (e.clientY || 0) - y0) > slop) ctl.cancel(); };
  const onScroll = () => ctl.cancel();
  const ctl = {
    cancel() {
      if (timer !== null) { clearTimeout(timer); timer = null; }
      node.removeEventListener('pointermove', onMove);
      window.removeEventListener('scroll', onScroll, true);
    },
  };
  node.addEventListener('pointerdown', (e) => {
    ctl.cancel();
    fired = false;
    const touchy = e.pointerType === 'touch' || e.pointerType === 'pen' || coarsePointer();
    if (!touchy || (e.pointerType === 'mouse' && e.button) || skip(e)) return;
    x0 = e.clientX || 0;
    y0 = e.clientY || 0;
    node.addEventListener('pointermove', onMove);
    window.addEventListener('scroll', onScroll, true);
    timer = setTimeout(() => {
      timer = null;
      ctl.cancel();
      fired = true;
      try { if (typeof navigator !== 'undefined' && typeof navigator.vibrate === 'function') navigator.vibrate(12); } catch (_) { /* no haptics */ }
      onFire(e);
    }, ms);
  });
  for (const type of ['pointerup', 'pointercancel', 'pointerleave']) node.addEventListener(type, () => ctl.cancel());
  node.addEventListener('contextmenu', (e) => { if (coarsePointer() || fired) e.preventDefault(); });
  node.addEventListener('click', (e) => { if (fired) { fired = false; e.preventDefault(); if (typeof e.stopPropagation === 'function') e.stopPropagation(); } }, true);
  return ctl;
}

/* ---- what the launcher remembers, readable without it (launcher.js is a lazy bundle, lazy.js): the saved choices of a repo, the repo a task last went to ---- */

const TASK_KEY = (p, r) => `ccboard:task:${p.name}/${r.name}`;
const TASK_LAST_KEY = (project) => `ccboard:task:last:${project}`;       // the repo a task was last started in, per project
const TASK_LAST_ANY_KEY = 'ccboard:task:last';                          // and the {project, repo} of the last task anywhere: where '+ task' opens off a project page

function taskLastRepo(project) { try { return localStorage.getItem(TASK_LAST_KEY(project)) || ''; } catch (_) { return ''; } }
function taskSaveLastRepo(project, repo) {
  try {
    localStorage.setItem(TASK_LAST_KEY(project), repo);
    localStorage.setItem(TASK_LAST_ANY_KEY, JSON.stringify({ project, repo }));
  } catch (_) { /* storage may be unavailable */ }
}
/* The {project, repo} a task was last created in, on any page, or null (nothing saved, or not shaped like that). The caller checks it still exists. */
function taskLastAny() {
  try {
    const v = JSON.parse(localStorage.getItem(TASK_LAST_ANY_KEY) || 'null');
    return v && typeof v.project === 'string' && typeof v.repo === 'string' && v.project && v.repo ? { project: v.project, repo: v.repo } : null;
  } catch (_) { return null; }
}

/* ---- the Move sheet: where does this Backlog card go (touch: a long press, the card's menu or the m key; a desktop card is dragged) ---- */

/* What the repo's saved launch choices say, for the subtitle of a lane button ('opus · high · acceptEdits'); '' when nothing is saved. */
function taskDefaultsLine(t, agent) {
  if (typeof TASK_KEY !== 'function') return '';
  const key = TASK_KEY({ name: t.project }, { name: t.repo }) + (agent === 'claude' ? '' : `:${agent}`);
  const v = loadPrefs(key);
  const bits = agent === 'claude' ? [v.model, v.effort, v.permission_mode] : [v.model, v.reasoning_effort || v.effort, v.sandbox];
  return bits.filter((x) => typeof x === 'string' && x).join(' · ');
}

/* The opts of the launcher in dispatch mode (launch) for a Backlog card: its project and repo as state objects (a bare {name} when the poll no longer has the place: the sheet
   still opens for the task), the task, and an optional preset {agent, session, auto_close} (dnd.js: Edit options… after a drop). */
function taskLaunchOpts(t, preset) {
  const st = typeof state !== 'undefined' ? state : null;
  const p = ((st && st.projects) || []).find((x) => x.name === t.project) || { name: t.project };
  const r = t.repo === 'root' ? (p.root || { name: 'root', root: true }) : ((p.repos || []).find((x) => x.name === t.repo) || { name: t.repo });
  return { mode: 'dispatch', project: p, repo: r, task: t, ...(preset || {}) };
}

function taskMoveable(t) { return taskPhase(t) === 'backlog'; }

function taskMoveSheet(t) {
  const want = t.agent || 'claude';
  const lane = (agent) => {
    const ok = taskAgentInstalled(agent);
    return el('button', { class: 'tk-lane-btn' + (agent === want ? ' primary tinted' : ''), type: 'button', 'data-agent': agent, disabled: !ok,
      title: ok ? `Start in a new ${AGENT_NAME[agent]} session, in its own worktree` : `${AGENT_NAME[agent]} is not installed on this box`,
      onclick: () => { closeSheet(); taskStart(t, { agent }); } },
    el('span', { class: 'tk-lane-top' }, agentGlyph(agent), el('span', { class: 'tk-lane-name', text: AGENT_NAME[agent] })),
    el('span', { class: 'tk-lane-sub', text: ok ? (taskDefaultsLine(t, agent) || 'repo defaults') : 'not installed on this box' }));
  };
  const list = taskSessionTargets(t);
  const bound = new Map();
  for (const x of boardTasks(typeof state !== 'undefined' ? state : null)) if (x.tmux && taskPhase(x) === 'running' && x.id !== t.id) bound.set(x.tmux, x);
  const rows = list.map((x) => {
    const mine = bound.get(x.s.tmux);
    return el('button', { class: 'minimal pick-row tk-pick' + (x.ok ? '' : ' off'), type: 'button', disabled: !x.ok, 'data-tmux': x.s.tmux,
      title: x.why || x.note || `send the prompt to ${x.s.name}`, onclick: () => { closeSheet(); taskSend(t, x); } },
    stateGlyph(x.s.state),
    el('span', { class: 'pr-name mono', text: x.s.name }),
    x.ok ? el('span', { class: 'dim tk-st', text: STATE_LABEL[x.s.state] || '' }) : null,
    el('span', { class: 'dim tk-repo', text: (x.repo === 'root' ? 'project folder' : x.repo) + (x.same ? '' : ' · other repo') }),
    x.why || x.note ? el('span', { class: 'dim', text: x.why || x.note }) : null,
    mine ? el('span', { class: 'dim tk-last', text: `task: ${mine.title}` }) : (x.s.last_prompt ? el('span', { class: 'dim tk-last', text: '› ' + String(x.s.last_prompt).slice(0, 100) }) : null));
  });
  const anyOk = list.some((x) => x.ok);
  const foot = el('div', { class: 'tk-foot' },
    typeof openLauncher === 'function' || typeof taskDispatchSheet === 'function' ? el('button', { type: 'button', class: 'tk-opts', onclick: () => launch(taskLaunchOpts(t)), text: 'Options…' }) : null,         // the launcher in dispatch mode; no closeSheet() first: a closed dialog fires its close event a task later and would wipe the form that replaced it
    el('button', { type: 'button', class: 'tk-cancel', onclick: () => closeSheet(), text: 'Cancel' }));
  const body = el('div', { class: 'tk-move' },
    el('h3', { class: 'tk-sec', text: 'Start in a new session' }),
    el('div', { class: 'tk-lane-btns' }, lane('claude'), lane('codex')),
    el('h3', { class: 'tk-sec', text: 'Hand to a running session' }),
    list.length ? el('p', { class: 'dim tk-note', text: anyOk ? 'Ready sessions first. A session in another repo asks before it works there.' : 'None of these can take it right now: wait for one, or start a new session above.' })
      : el('p', { class: 'dim tk-note', text: 'No session of this project is running.' }),
    list.length ? el('div', { class: 'pick-list' }, ...rows) : null,
    foot);
  openSheet({ title: `Move “${String(t.title).slice(0, 60)}”`, body, placement: 'bottom' });
}

function taskSendSheet(t) { return taskMoveSheet(t); }          // the name v0.5.14a gave the sheet

/* ---- the card's action rows (two rows on one 3-column grid; a phone keeps two and folds the rest into a ... menu) ----
   acts {r1: [d], r2: [d], more: [d], danger: {key, label, run}} where d is {id, label, icon?, kind: 'primary' | 'warn' | undefined, href?, newTab?, title?, onClick?}.
   Row 1 shows its first two (the primary and the owner / quick target), row 2 up to three quiet ones with the destructive one last; whatever does not fit goes
   to the menu, which also takes the whole of row 2 under 600 px. An armed destructive action replaces row 2 with Confirm + Cancel on equal cells. */

function taskActCell(d, row) {
  const cls = [d.kind === 'primary' ? 'primary tinted' : '', d.kind === 'warn' ? 'warn' : '', row === 2 ? 'minimal' : '', d.cls || ''].filter(Boolean).join(' ');
  const kids = [d.icon ? ic(d.icon) : null, d.label];
  if (d.href) return el('a', { class: 'btn ' + cls, href: d.href, target: d.newTab ? '_blank' : null, rel: d.newTab ? 'noopener' : null, title: d.title || null, 'data-act': d.id }, ...kids);
  return el('button', { class: cls, type: 'button', title: d.title || null, 'data-act': d.id, onclick: d.onClick }, ...kids);
}

function taskMenuItem(d) {
  return { label: d.label, icon: d.icon || null, onClick: d.onClick || (() => { if (d.newTab && typeof window !== 'undefined' && typeof window.open === 'function') window.open(d.href, '_blank', 'noopener'); else if (typeof location !== 'undefined') location.hash = d.href; }) };
}

function taskActions(acts) {
  const narrow = narrowViewport();
  const danger = acts.danger || null;
  const armed = !!(danger && ui.confirm === danger.key);
  const r1all = (acts.r1 || []).filter(Boolean);
  const r2all = (acts.r2 || []).filter(Boolean);
  const extra = (acts.more || []).filter(Boolean);
  let r1 = r1all.slice(0, 2);
  let r2 = [];
  let more;
  if (narrow && !r1.length) { r1 = r2all.slice(0, 1); more = [...r2all.slice(1), ...extra]; }                       // nothing on row 1 (a queued card): its first quiet action stays visible, not a lone ... cell
  else if (narrow) more = [...r1all.slice(2), ...r2all, ...extra];
  else { const cap = danger ? 2 : 3; r2 = r2all.slice(0, cap); more = [...r1all.slice(2), ...r2all.slice(cap), ...extra]; }
  const items = more.map(taskMenuItem);
  if (narrow && danger) items.push({ label: danger.label, icon: 'trash', onClick: () => confirmArm(danger.key) });
  const cells = r1.map((d) => taskActCell(d, 1));
  if (items.length && !armed) {
    const dots = el('button', { class: 'icon minimal tk-more', type: 'button', 'aria-label': 'More actions', title: 'More actions' }, ic('more'));
    menu(dots, items);
    cells.push(dots);
  }
  const rows = [];
  if (cells.length) rows.push(el('div', { class: 'tk-r tk-r1' }, ...cells));
  if (armed) rows.push(el('div', { class: 'tk-r tk-confirm' }, confirmButton(danger.key, danger.label, danger.run, true)));
  else if (!narrow && (r2.length || danger)) rows.push(el('div', { class: 'tk-r tk-r2' }, ...r2.map((d) => taskActCell(d, 2)), danger ? confirmButton(danger.key, danger.label, danger.run, true) : null));
  return el('div', { class: 'tk-acts' }, ...rows);
}

/* ---- the card itself: wired for the long press and the m key (the Move sheet); dnd.js makes a Backlog card draggable on a fine pointer, by the [data-task] it carries ---- */

function taskCardShell(t, cls, move, ...kids) {
  const card = el('div', { class: 'task' + cls, 'data-task': t.id, 'data-phase': taskPhase(t), tabindex: '0', role: 'group', 'aria-label': `Task: ${String(t.title || '').slice(0, 120)}` }, ...kids);
  const chip = Nodes.chip(null);                                                        // paired nodes: this board's dim chip on every card (nodes.js); null otherwise
  if (chip) { const meta = card.querySelector('.meta'); if (meta) meta.append(' ', chip); else card.append(chip); }                  // in the meta line, beside where the task lives and its age
  if (move) {
    card.setAttribute('data-movable', '1');
    card.setAttribute('aria-keyshortcuts', 'm');
    longPress(card, () => taskMoveSheet(t));
    card.addEventListener('keydown', (e) => {
      if (e.key !== 'm' || e.ctrlKey || e.metaKey || e.altKey || e.shiftKey || e.isComposing) return;
      const tg = e.target;
      if (tg && (tg.isContentEditable || /^(?:INPUT|TEXTAREA|SELECT)$/.test(String(tg.tagName || '')))) return;
      if (typeof Keymap !== 'undefined' && Keymap && Keymap.pending) return;          // 'g m' goes to Memory
      e.preventDefault();
      if (typeof e.stopPropagation === 'function') e.stopPropagation();
      taskMoveSheet(t);
    });
  }
  return card;
}

/* ---- owner, auto-close, result and chain chips ---- */

function taskOwnerChip(t) {
  if (!t.tmux) return null;
  const s = t.session;
  const name = sessionNameOf(t.tmux);
  const age = s && s.state_at ? fmtAge(Date.parse(s.state_at) / 1000) : '';
  const state = s && STATE_LABEL[s.state] ? (s.parked ? 'limit reached' : STATE_LABEL[s.state]) + (age ? ` · ${age}` : '') : '';
  return el('a', { class: 'chip-btn tk-owner', href: taskPeekHash(t.tmux), title: `this task runs in ${name}: open it` },
    agentGlyph(t.agent || 'claude'), ' ', el('span', { class: 'tk-owner-name', text: name }),
    s && s.state ? [' ', stateGlyph(s.state), ' ', el('span', { class: 'tk-owner-state', text: state })] : null);
}

function taskKeepOpen(t) { return taskSessionCall(t, 'keep-open', 'kept open: the session stays', { autoclose: null }); }
function taskCloseSession(t) { return taskSessionCall(t, 'close-session', 'closing the session…', null); }

/* POST /api/tasks/{id}/<what> (keep-open, close-session): toast the outcome, paint `patch` at once, let the poll settle it. */
async function taskSessionCall(t, what, said, patch) {
  if (patch) { taskOverrideSet({ ...t, ...patch }, Object.keys(patch)); taskRepaint(); }
  try {
    const res = await api('POST', `/api/tasks/${t.id}/${what}`);
    if (patch && res && res.task && typeof res.task === 'object') taskOverrideSet({ ...t, ...res.task, ...patch }, Object.keys(patch));
    toast(said, { kind: 'ok' });
    if (typeof poll === 'function') poll(true);
  } catch (e) {
    if (patch) { taskOverrideDrop(t.id); taskRepaint(); }
    taskFail(e);
  }
}

/* Reopen: a done, failed or cancelled task starts again in its worktree (or a fresh one); the card is In progress at once. */
async function taskReopen(t) {
  const cur = store.tasksOverride[t.id];
  if (cur && cur._busy) return;
  const pending = { ...t, phase: 'running', column: 'in_progress', session: taskStartingSession(), closed_at: null, autoclose: null };
  taskOverrideSet(pending, ['phase', 'column', 'session', 'closed_at', 'autoclose'], { _busy: true });
  taskRepaint();
  try {
    const res = await api('POST', `/api/tasks/${t.id}/reopen`);
    const row = taskRowFromResponse(pending, res, { closed_at: null, autoclose: null });
    if (!row.tmux && taskDemo()) row.tmux = `${t.project}--${t.repo}--t-${t.slug}`;
    taskOverrideSet(row);
    toast(`reopened ${row.slug || t.slug}`, { kind: 'ok' });
    taskWarn(res);
    taskRepaint();
    taskOpenPeek(row.tmux);
    if (typeof poll === 'function') poll(true);
  } catch (e) {
    taskOverrideDrop(t.id);
    taskRepaint();
    taskFail(e);
  }
}

/* The strip under a card that is about to lose its session: '⏻ auto-close 0:32' and Keep open. */
function taskAutoCloseStrip(t, ac) {
  if (ac.closing) return el('div', { class: 'tk-ac' }, el('span', { class: 'badge tk-ac-tag', title: 'the session is being closed' }, ic('power'), 'closing…'));
  taskTickEnsure();
  return el('div', { class: 'tk-ac' },
    el('span', { class: 'badge tk-ac-tag', title: ac.waiting ? `the close is postponed: ${ac.waiting}` : 'the session closes itself shortly after this task stops, unless you keep it open' }, ic('power'), 'auto-close',
      el('span', { class: 'tk-count mono', 'data-due': String(ac.due), text: taskCountdownText(ac.due - Date.now()) }), ac.waiting ? el('span', { class: 'dim tk-ac-why', text: `· ${ac.waiting}` }) : null),
    el('button', { class: 'small tk-keep', type: 'button', title: 'cancel the pending close: the session stays open', onclick: () => taskKeepOpen(t), text: 'Keep open' }));
}

/* The result of a finished task: its first line under a Result summary; opened, the excerpt (the poll carries 300 characters, then 'Show the whole result'). */
function taskResultNode(t) {
  const head = String(t.result || '').trim();
  if (!head) return null;
  const full = taskUi.full[t.id];
  const shown = typeof full === 'string' ? full.trim() : head;
  const more = typeof full !== 'string' && head.length >= 300;
  const first = shown.split('\n').map((l) => l.trim()).find(Boolean) || '';
  const kids = [el('div', { class: 'tk-result-body', text: shown })];
  if (more) {
    kids.push(el('button', { class: 'minimal small tk-result-more', type: 'button', text: 'Show the whole result', onclick: async () => {
      try { const r = await api('GET', `/api/tasks/${t.id}`); taskUi.full[t.id] = String((r && r.result) || head); taskUi.open[t.id] = true; taskRepaint(); }
      catch (e) { taskFail(e); }
    } }));
  }
  const d = el('details', { class: 'tk-result', open: taskUi.open[t.id] ? true : null },
    el('summary', {}, el('span', { class: 'tk-result-k', text: 'Result' }), el('span', { class: 'tk-result-first', text: first })), ...kids);
  d.addEventListener('toggle', () => { if (d.open) taskUi.open[t.id] = true; else delete taskUi.open[t.id]; });
  return d;
}

/* ---- the chain strip: connected steps with a status each (the Tasks page and the project's Tasks tab) ---- */

function taskStepStatus(t) {
  const ph = taskPhase(t);
  const s = t.session;
  if (ph === 'queued') return taskLimitHold(t) ? { glyph: 'idle', label: 'held' } : { glyph: 'idle', label: 'waiting' };
  if (ph === 'backlog') return { glyph: 'idle', label: 'ready' };
  if (ph === 'done') return { glyph: 'done', label: 'done' };
  if (ph === 'failed') return { glyph: 'errored', label: 'failed' };
  if (ph === 'cancelled') return { glyph: 'ended', label: 'cancelled' };
  const st = s && s.state ? s.state : 'working';
  return { glyph: st, label: STATE_LABEL[st] || 'running' };
}

function taskScrollTo(id) {
  const n = document.querySelector(`.task[data-task="${id}"]`);
  if (!n) return;
  if (typeof n.scrollIntoView === 'function') { try { n.scrollIntoView({ block: 'nearest', behavior: scrollBehavior() }); } catch (_) { /* old browsers */ } }
  if (typeof n.focus === 'function') n.focus();
}

/* [chain node] for every chain of two or more steps among `list`; info is taskChainInfo(list). */
function taskChainStrips(list, info) {
  const groups = new Map();
  for (const t of list) {
    const i = info.get(t.id);
    if (!i) continue;
    if (!groups.has(i.chain)) groups.set(i.chain, []);
    groups.get(i.chain).push({ t, i });
  }
  const out = [];
  for (const [key, steps] of groups) {
    if (steps.length < 2) continue;
    steps.sort((a, b) => a.i.step - b.i.step || Number(a.t.id) - Number(b.t.id));
    const row = el('div', { class: 'tk-chain', 'data-chain': key, role: 'group', 'aria-label': `A chain of ${steps.length} steps` });
    steps.forEach(({ t, i }, n) => {
      if (n) row.append(el('span', { class: 'tk-link', 'aria-hidden': 'true', text: i.step === steps[n - 1].i.step ? '·' : '→' }));
      const st = taskStepStatus(t);
      row.append(el('button', { class: 'tk-step', type: 'button', 'data-step': t.id, 'data-phase': taskPhase(t), title: `${t.title}: ${st.label}`, onclick: () => taskScrollTo(t.id) },
        el('span', { class: 'tk-step-n', text: String(i.step) }),
        el('span', { class: 'tk-step-title', text: t.title }),
        el('span', { class: 'tk-step-st' }, stateGlyph(st.glyph), ' ', st.label)));
    });
    out.push(row);
  }
  return out;
}

/* ---- the board: the dispatch bar (dnd.js: Dnd.laneBar, one node kept across repaints), the chains, then the columns. One node per page; update()
   rebuilds only when something it shows changed, and never while a card is being dragged (a node that leaves the page mid-drag never gets its dragend:
   Dnd.afterDrag queues the repaint for the end of the drag). ---- */

function taskSig(v) { try { return JSON.stringify(v); } catch (_) { return String(Math.random()); } }

/* makeTaskBoard({project?: name | () => name, lanes?, cls?, empty?}) -> {node, update(tasks, st)}; tasks are boardTasks(st) rows (already filtered to the page's scope).
   The Tasks page draws its own board (home.js renderTasks) and adds the chains (pages/tasks.js); the project's Tasks tab draws this one. */
function makeTaskBoard(opts) {
  const o = opts || {};
  const lanesHost = el('div', { class: 'tk-lanes-host' });
  const chainsHost = el('div', { class: 'tk-chains hidden' });
  const grid = el('div', { class: 'kanban' + (o.cls ? ' ' + o.cls : '') });
  const node = el('div', { class: 'tk-board' }, lanesHost, chainsHost, grid);
  let sig = null;
  let bar = null;
  let barFor = null;
  let dndAsked = false;
  return {
    node,
    update(tasks, st) {
      if (typeof Dnd !== 'undefined' && Dnd && typeof Dnd.afterDrag === 'function' && Dnd.afterDrag(taskRepaint)) return;
      const project = (typeof o.project === 'function' ? o.project() : o.project) || '';
      if (o.lanes !== false && typeof Dnd !== 'undefined' && Dnd && typeof Dnd.laneBar === 'function') {
        if (!bar || barFor !== project) { bar = Dnd.laneBar({ project }); barFor = project; lanesHost.textContent = ''; lanesHost.append(bar); }
        bar.ccPatch(st);
      } else if (o.lanes !== false && typeof Lazy !== 'undefined' && Lazy.wants('dnd')) {      // dnd.js is a lazy bundle (lazy.js): the bar's room is kept while it loads, then the board is drawn again
        if (!lanesHost.firstChild) lanesHost.append(el('div', { class: 'dnd-bar-slot' }));
        if (!dndAsked) { dndAsked = true; Lazy.load('dnd').then(() => { sig = null; taskRepaint(); }, () => { dndAsked = false; lanesHost.textContent = ''; }); }
      }
      const info = taskChainInfo(tasks);
      const ready = tasks.filter(taskIsBacklog).map((t) => taskSessionTargets(t, st).filter((x) => x.ok && x.same).map((x) => x.s.tmux));      // a backlog card's quick send follows the sessions
      const next = taskSig([tasks, ui.confirm, ready, narrowViewport(), coarsePointer(), taskUi.open, taskUi.full, [...info], Math.floor(Date.now() / 60000)]);   // the minute: a card's ages stay current
      if (next === sig) return;
      sig = next;
      const strips = taskChainStrips(tasks, info);
      chainsHost.textContent = '';
      if (strips.length) chainsHost.append(el('h2', { class: 'tk-sec', text: 'Chains' }), ...strips);
      chainsHost.classList.toggle('hidden', !strips.length);
      grid.textContent = '';
      const ctx = { chain: info };
      for (const [key, label] of BOARD_COLUMNS) {
        const items = tasks.filter((t) => t.column === key);
        if (!items.length && key !== 'backlog') continue;            // a column is drawn when it has a card; Backlog stays, it is where a task starts
        const col = el('div', { class: 'col', 'data-col': key, role: 'group', 'aria-label': label }, el('h2', { text: `${label} (${items.length})` }));
        if (!items.length && key === 'backlog') col.append(el('div', { class: 'dim', text: o.empty || 'nothing queued: + task, then Later' }));
        for (const t of items) col.append(taskCard(t, ctx));
        grid.append(col);
      }
    },
  };
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

/* A task before any session (phase backlog | queued): title, the head of its prompt, where, how old, then the two action rows. Row 1: Start (the tinted
   primary: the cards repeat it) and, when exactly one ready session of the same repo could take it, a one-tap '→ s1'. Row 2: Move… (the Move sheet: a new
   Claude or Codex session, or a running one), Edit, and Delete last. Under 600 px only row 1 stays and a ... menu takes Move…, Edit and Delete (Delete is
   still two taps: the menu arms it, the card then shows Confirm Delete + Cancel). A queued card (a chain step waiting for its parent) has no Start or Move:
   the server would refuse both. The long press, the m key and the grip open or start the Move. */
function backlogCard(t, ctx) {
  const queued = taskPhase(t) === 'queued';
  const when = t.created_at ? Date.parse(t.created_at) / 1000 : 0;
  const added = !when ? '' : (Date.now() / 1000 - when < 20 ? 'added just now' : `added ${fmtAge(when)} ago`);
  const quick = queued ? [] : taskSessionTargets(t).filter((x) => x.ok && x.same && !x.queue);   // the one-tap target is a session at its prompt, never a queue
  const head = String(t.prompt || '');
  const more = head.startsWith(t.title) ? head.slice(t.title.length).replace(/^[\s.:;,-]+/, '') : head;       // a title cut from the prompt's first line is not said twice
  const chain = taskChainOf(t, ctx);
  const hold = taskLimitHold(t);
  const chips = [];
  if (chain && chain.of > 1) chips.push(el('span', { class: 'badge tk-chain-badge', title: 'a chain: each step starts when the one before it finishes', text: `step ${chain.step} of ${chain.of}` }));
  if (hold) chips.push(el('span', { class: 'badge warn tk-hold', title: 'the Claude usage window is nearly used: this starts when it resets (Start still works)' }, ic('time'), hold.text));
  const acts = taskActions({
    r1: queued ? [] : [
      { id: 'start', label: 'Start', icon: 'play', kind: 'primary', title: 'Start in a new session, in its own worktree and branch', onClick: () => taskStart(t) },
      quick.length === 1 ? { id: 'quick', label: `→ ${quick[0].s.name}`, title: `Send the prompt to ${quick[0].s.name} now`, onClick: () => taskSend(t, quick[0]) } : null],
    r2: [queued ? null : { id: 'move', label: 'Move…', title: 'Start it in a new Claude or Codex session, or hand it to a running one (m)', onClick: () => taskMoveSheet(t) },
      { id: 'edit', label: 'Edit', onClick: () => taskEditSheet(t) }],
    danger: { key: 'tdel:' + t.id, label: 'Delete', run: () => taskDelete(t) },
  });
  return taskCardShell(t, ' backlog' + (queued ? ' queued' : ''), !queued,
    el('div', { class: 'row tk-title-row' }, el('span', { class: 'title', text: t.title }), queued ? el('span', { class: 'state ended', text: 'waiting for a step' }) : null),
    more ? el('div', { class: 'tk-prompt', text: more }) : null,
    el('div', { class: 'meta' }, taskWhere(t), added ? ` · ${added}` : ''),
    chips.length ? el('div', { class: 'tk-chips' }, ...chips) : null,
    acts);
}

/* Preview: expose the task's dev server on its own tailnet HTTPS port. When the server cannot find the listening port on its own it asks, in a small
   sheet (a labelled number field, Expose + Cancel, the reason in --dim), never a browser prompt(). */
async function taskPreview(t, port) {
  const r = await api('POST', `/api/tasks/${t.id}/preview`, port ? { port } : {});
  toast(`preview at ${r.url} → 127.0.0.1:${r.port}`, { kind: 'ok', ttl: 8000 });
  return r;
}

/* #126: the board cannot make a preview here (state.preview from app/previews.py capability()): a disabled Preview and the reason in words, before any click. */
function taskPreviewOff(t, pv) {
  const id = `tk-pv-${t.id}`;
  return el('div', { class: 'tk-pv' }, el('button', { class: 'minimal small', type: 'button', disabled: true, 'aria-describedby': id, 'data-act': 'preview', text: 'Preview' }),
    el('span', { id, class: 'dim tk-pv-why', text: `unavailable: ${pv.reason || 'Tailscale is not ready'}` }));
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

/* Open the diff / PR sheet of the card (home.js openTaskModal: diff, Describe, Create PR, Merge) */
function taskModal(t) { if (typeof openTaskModal === 'function') openTaskModal(t); }

/* Archive (two taps through confirmButton): a worktree with unsaved work asks once more. */
async function taskArchive(t) {
  try { await api('POST', `/api/tasks/${t.id}/archive`, { force: false }); }
  catch (e) {
    if (/force/.test(e.message) && window.confirm(e.message + '\n\nDiscard the worktree anyway?')) await api('POST', `/api/tasks/${t.id}/archive`, { force: true });
    else throw e;
  }
}

/* The GitHub issue a task came from (v0.5.20). taskIssueLink(t) -> "#N" linking to the issue (https only), or plain text when the URL is missing.
   taskIssueNode(t) -> the card's "Comment on the issue" row once the task is done with a result: a two-tap confirmButton, the body to be posted shown while it is armed
   (GET /api/tasks/{id}/issue-comment), POST once; "Posted on #N" afterwards. A failure shows its error and keeps the button; nothing posts without the second tap. */
const TASK_ISSUE = { body: {}, posted: {} };

function taskIssueLink(t) {
  if (!t || !t.issue_number) return null;
  const label = `#${t.issue_number}`;
  const url = String(t.issue_url || '');
  if (/^https:\/\//.test(url)) return el('a', { class: 'tk-issue-link', href: url, target: '_blank', rel: 'noopener noreferrer', title: 'open the GitHub issue', text: label });
  return el('span', { class: 'tk-issue-link', text: label });
}

function taskIssueNode(t) {
  if (!t || !t.issue_number || taskPhase(t) !== 'done' || !String(t.result || '').trim()) return null;
  const n = t.issue_number;
  if (t.issue_commented_at || TASK_ISSUE.posted[t.id]) return el('div', { class: 'tk-issue dim', text: `Posted on #${n}` });
  const key = 'issue:' + t.id;
  const btn = confirmButton(key, 'Comment on the issue', async () => {
    const r = await api('POST', `/api/tasks/${t.id}/issue-comment`);
    TASK_ISSUE.posted[t.id] = true;
    delete TASK_ISSUE.body[t.id];
    toast(`posted on #${n}`, { kind: 'ok' });
    return r;
  }, true);
  if (ui.confirm !== key) {
    btn.addEventListener('click', async () => {                                    // the first tap arms it; the body to be posted follows as soon as the board has built it
      try { const r = await api('GET', `/api/tasks/${t.id}/issue-comment`); TASK_ISSUE.body[t.id] = String((r && r.body) || ''); }
      catch (e) { TASK_ISSUE.body[t.id] = `Could not build the comment: ${e.message}`; }
      if (ui.confirm === key && typeof repaintPage === 'function') repaintPage();
    });
  }
  return el('div', { class: 'tk-issue' },
    el('div', { class: 'row' }, btn),
    ui.confirm === key ? el('pre', { class: 'tk-issue-body', 'aria-label': 'The comment to be posted', text: TASK_ISSUE.body[t.id] || 'Building the comment…' }) : null);
}

/* A started task's card. Chips under the meta line: the owner (◆ s2 ✽ working · 3m, opens the session), the chain step, a limit hold, 'closed after stop', 'needs you'.
   A pending close shows its countdown and Keep open; a finished task shows its result (expandable) and the PR actions before Reopen.
   Row 1: the one primary, then the next most useful (Terminal and Fix CI while it runs; New PR…, Merge… and Reopen once it is done). Row 2: the quiet ones, Archive last. */
function startedCard(t, ctx) {
  const phase = taskPhase(t);
  const s = t.session;
  const starting = !t.tmux;
  const live = !!(t.tmux && s && s.state !== 'ended' && !t.closed_at);          // a session is open (a closed or ended one leaves the card with Reopen)
  const finished = phase === 'done' || phase === 'failed' || phase === 'cancelled';
  const ac = taskAutoclose(t);
  const asked = !!(ac && ac.held === 'question');
  const pending = !!(ac && (ac.due || ac.closing) && !ac.held);
  const lead = !!(s && s.needs_attention) || asked;                             // the card that waits for you carries the tinted primary; the others are quiet
  const handed = t.mode === 'session';
  const ciFail = t.ci && t.ci.bucket === 'fail';
  const chain = taskChainOf(t, ctx);
  const hold = taskLimitHold(t);
  const doneAt = taskMs(t.done_at || t.result_at);
  const enc = encodeURIComponent(t.tmux || '');
  const queuedTurn = phase === 'running' && Number(t.turns_ahead) > 0;           // a prompt queued behind a turn in flight: its own turn has not begun
  const badge = queuedTurn ? el('span', { class: 'state idle', title: 'the session is finishing an earlier turn; this task runs next', text: 'queued' })
    : live && s ? stateBadge(s)
    : phase === 'failed' ? el('span', { class: 'state errored', text: 'failed' })
      : phase === 'cancelled' ? el('span', { class: 'state ended', text: 'cancelled' })
        : phase === 'done' ? el('span', { class: 'state done', text: 'done' + (doneAt ? ` ${fmtAge(doneAt / 1000)}` : '') })
          : el('span', { class: 'state ended', text: 'no session' });
  const chips = [];
  const owner = live ? taskOwnerChip(t) : null;
  if (owner) chips.push(owner);
  if (asked) chips.push(el('span', { class: 'state waiting tk-asked', title: 'its last message ends with a question: the session stays open for your answer', text: 'needs you · it asked a question' }));
  if (!live && finished && t.closed_at) chips.push(el('span', { class: 'badge tk-closed', title: 'the session closed itself after the task stopped' }, ic('power'), `closed after stop · ${fmtAge(taskMs(t.closed_at) / 1000)}`));
  if (chain && chain.of > 1) chips.push(el('span', { class: 'badge tk-chain-badge', title: 'a chain: each step starts when the one before it finishes', text: `step ${chain.step} of ${chain.of}` }));
  if (hold) chips.push(el('span', { class: 'badge warn tk-hold', title: 'the Claude usage window is nearly used: the next step starts when it resets' }, ic('time'), hold.text));
  if (live && !pending && !asked && t.auto_close && !finished) chips.push(el('span', { class: 'badge tk-ac-tag', title: 'the session closes itself shortly after this task stops' }, ic('power'), 'auto-close'));

  const modalAct = t.branch ? { id: 'diff', label: t.pr_url ? 'Diff' : 'Diff…', title: 'the diff, the description and the pull request', onClick: () => taskModal(t) } : null;
  const prAct = t.branch ? (t.pr_url ? { id: 'pr', label: `PR #${t.pr_number}`, href: t.pr_url, newTab: true, title: 'open the pull request' }
    : { id: 'pr', label: 'New PR…', title: 'push the branch and open a pull request', onClick: () => taskModal(t) }) : null;
  const mergeAct = t.pr_url && t.pr_number && t.pr_state !== 'MERGED' && t.pr_state !== 'CLOSED' ? { id: 'merge', label: 'Merge…', title: 'squash-merge the pull request and archive the task', onClick: () => taskModal(t) } : null;
  const reopenAct = finished && !live && t.pr_state !== 'MERGED' ? { id: 'reopen', label: 'Reopen', title: 'start it again in its worktree', onClick: () => taskReopen(t) } : null;
  const termAct = live ? { id: 'terminal', label: 'Terminal', href: `/term/${enc}`, newTab: true, title: 'open the terminal in a new tab' } : null;
  const fixAct = ciFail ? { id: 'fixci', label: 'Fix CI', kind: 'warn', cls: 'tk-fixci', title: 'send the failing CI logs to the session', onClick: async () => {
    try { const r = await api('POST', `/api/tasks/${t.id}/fix-ci`); setError(null); toast(`CI logs (${r.chars} chars) sent to ${t.title}${r.relaunched ? ' (session relaunched)' : ''}`, { kind: 'ok', ttl: 8000 }); } catch (e) { setError(e.message); }
    await poll(true);
  } } : null;
  const pvOff = !starting && live && !t.preview_url && typeof state !== 'undefined' && state && state.preview && state.preview.available === false ? state.preview : null;
  const previewAct = starting || !live || pvOff ? null : (t.preview_url
    ? { id: 'preview', label: `Preview :${t.preview_port}`, href: t.preview_url, newTab: true }
    : { id: 'preview', label: 'Preview', title: 'expose a dev server running in this session on its own tailnet HTTPS port', onClick: async () => {
      try { await taskPreview(t); }
      catch (e) { if (/no listening port/.test(e.message)) taskPortSheet(t, e.message); else setError(e.message); }
      await poll(true);
    } });
  const extra = [];
  if (t.preview_url) extra.push({ id: 'unpreview', label: 'Stop preview', icon: 'cross', onClick: async () => { try { await api('DELETE', `/api/tasks/${t.id}/preview`); } catch (e) { setError(e.message); } await poll(true); } });
  if (t.pr_url) extra.push({ id: 'refresh', label: 'Refresh PR status', icon: 'refresh', onClick: async () => { try { await api('POST', `/api/tasks/${t.id}/refresh`); } catch (e) { setError(e.message); } await poll(true); } });
  if (live && finished) extra.push({ id: 'close', label: 'Close the session now', icon: 'power', onClick: () => taskCloseSession(t) });

  let order;
  let tint = false;                                                              // the first action leads (a tinted primary) on a card that has a call to action
  if (starting) order = [];
  else if (finished && !live) { order = phase === 'failed' ? [reopenAct, prAct, mergeAct, t.pr_url ? modalAct : null] : [prAct, mergeAct, reopenAct, t.pr_url ? modalAct : null]; tint = true; }
  else if (phase === 'done') { order = [t.pr_url ? null : prAct, termAct, mergeAct, fixAct, previewAct, t.pr_url ? prAct : null]; tint = true; }
  else { order = [termAct, fixAct, t.pr_url ? prAct : modalAct, previewAct, t.pr_url ? modalAct : null]; tint = lead; }
  order = order.filter(Boolean);
  if (tint && order[0] && !order[0].kind) order[0] = { ...order[0], kind: 'primary' };
  const acts = taskActions({
    r1: order.slice(0, 2),
    r2: order.slice(2),
    more: extra,
    danger: starting ? null : { key: 'arch:' + t.id, label: 'Archive', run: () => taskArchive(t) },
  });
  return taskCardShell(t, (s && s.needs_attention || asked ? ' attn' : '') + (handed ? ' handed' : '') + (finished && !live ? ' finished' : ''), false,
    el('div', { class: 'row tk-title-row' }, el('span', { class: 'title', text: t.title }), badge, ciBadge(t)),
    el('div', { class: 'meta' }, starting ? `${taskWhere(t)} · starting…` : (t.branch ? `${t.project}/${t.repo} · ${t.branch}` : taskWhere(t) + (t.mode === 'attached' ? ' · in place' : '')), t.pr_url ? ' · PR #' + t.pr_number : null, t.issue_number ? [' · ', taskIssueLink(t)] : null,
      typeof t.cost_usd === 'number' ? [' · ', el('span', { class: 'mono', text: '$' + t.cost_usd.toFixed(2) })] : null),
    chips.length ? el('div', { class: 'tk-chips' }, ...chips) : null,
    pvOff ? taskPreviewOff(t, pvOff) : null,
    pending && live ? taskAutoCloseStrip(t, ac) : null,
    s && s.last_message && !(finished && t.result) ? el('div', { class: 'last', text: s.last_message.slice(0, 160) }) : null,
    finished ? taskResultNode(t) : null,
    taskIssueNode(t),
    (t.overlap && t.overlap.length) ? el('div', { class: 'last bad', title: t.overlap.map(o => `${o.title}: ${o.files.join(', ')}`).join('\n'),
      text: '⚠ overlaps ' + t.overlap.map(o => `"${o.title}" (${o.files.length} file${o.files.length === 1 ? '' : 's'}: ${o.files.slice(0, 3).join(', ')}${o.files.length > 3 ? '…' : ''})`).join('; ') }) : null,
    acts);
}

/* A card: the Backlog's (before any session) or a started task's. ctx {chain: taskChainInfo(list)} places a chained card in its chain. */
function taskCard(t, ctx) {
  return taskIsBacklog(t) ? backlogCard(t, ctx) : startedCard(t, ctx);
}

/* ---------- shell components (v0.5.3): tabs, sheet, toast, menu, empty state. Definitions only: the DOM is touched when they are called. ---------- */

/* tabs(items:[{id,label,count?}], activeId, onChange) -> { root, set(id), setCount(id, n), value }. Only the tab list: the page renders the panel.
   set() repaints without calling onChange (so a route change cannot loop); a click or an arrow key calls onChange(id). */
let tabsSeq = 0;
function tabs(items, activeId, onChange) {
  const seq = ++tabsSeq;
  let shared = null;                                                       // link(node): ONE panel whose content follows the selected tab
  const list = el('div', { class: 'tablist', role: 'tablist' });
  const root = el('div', { class: 'tabs' }, list);
  const nodes = new Map();
  const counts = new Map();
  const ids = items.map((it) => it.id);
  let current = ids.includes(activeId) ? activeId : ids[0];
  // the tab list scrolls sideways when the tabs do not fit (390 px, six tabs): bring the selected one into view once the list is in the page
  const reveal = (n) => {
    if (typeof requestAnimationFrame !== 'function') return;
    requestAnimationFrame(() => {
      try {
        const a = n.getBoundingClientRect();
        const b = list.getBoundingClientRect();
        if (a.left < b.left) list.scrollLeft -= b.left - a.left;
        else if (a.right > b.right) list.scrollLeft += a.right - b.right;
      } catch (_) { /* no layout */ }
    });
  };
  const paint = (id) => {
    current = id;
    for (const [tid, n] of nodes) {
      n.setAttribute('aria-selected', tid === id ? 'true' : 'false');
      n.setAttribute('tabindex', tid === id ? '0' : '-1');
    }
    if (shared && nodes.has(id)) shared.setAttribute('aria-labelledby', nodes.get(id).getAttribute('id'));
    if (nodes.has(id)) reveal(nodes.get(id));
  };
  const pick = (id, focus) => {
    if (id !== current) { paint(id); if (typeof onChange === 'function') onChange(id); }
    if (focus) nodes.get(id).focus();
  };
  for (const it of items) {
    const count = it.count === undefined || it.count === null ? null : el('span', { class: 'tab-count', text: String(it.count) });
    if (count) counts.set(it.id, count);
    const n = el('div', { class: 'tab', role: 'tab', id: `tab${seq}-${it.id}`, 'data-tab': it.id, onclick: () => pick(it.id, false), onkeydown: (e) => {
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
    /* Tie the tabs to their panels for assistive tech: link({tabId: panelNode}) gives every tab aria-controls and every panel an id and aria-labelledby; link(node) does it for ONE panel
       that shows whichever tab is selected (its aria-labelledby follows). Only panels that exist are referenced: an aria-controls to nothing is an error. */
    link(panels) {
      const one = panels && panels.nodeType === 1 ? panels : null;
      for (const [tid, n] of nodes) {
        const panel = one || (panels && panels[tid]);
        if (!panel || typeof panel.setAttribute !== 'function') continue;
        if (!panel.getAttribute('id')) panel.setAttribute('id', one ? `tabpanel${seq}` : `tabpanel${seq}-${tid}`);
        panel.setAttribute('role', 'tabpanel');
        n.setAttribute('aria-controls', panel.getAttribute('id'));
        if (!one) panel.setAttribute('aria-labelledby', n.getAttribute('id'));
      }
      if (one) { shared = one; one.setAttribute('aria-labelledby', nodes.get(current).getAttribute('id')); }
    },
  };
}

/* openSheet({title, body, actions?, placement?, wide?, onClose?, back?}) -> { dialog, body, close }. dialog#sheet through showModal(): a right panel from 840 px up,
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
  dlg.classList.toggle('wide', o.wide === true);                                     // room for a diff (home.js openTaskModal): ~1100 px instead of 460 / 680
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
   button's dialog when it sits in one, else inside #topbar. Click outside, Esc, Tab or a route change close it; arrows move, Enter picks.
   Opened from the keyboard (Enter or Space on the button: a click with detail 0) the first item takes focus, so the arrows work at once; opened
   with a pointer the popover itself holds the focus: a focus ring on the first item would read as "this one is selected" under a thumb. */
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
  ctl.open = (byKeyboard) => {
    if (pop) return;
    const list = typeof items === 'function' ? items() : items;
    pop = el('div', { class: 'menu menu-pop', role: 'menu', tabindex: '-1' });
    for (const it of list) {
      const pick = () => { ctl.close(false); if (typeof it.onClick === 'function') it.onClick(); };
      pop.append(el('div', { class: 'menuitem', role: 'menuitem', tabindex: '-1', title: it.title || null, onclick: pick,
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } } },
      it.icon ? ic(it.icon) : null,
      it.sub ? el('span', { class: 'mi-text' }, el('span', { class: 'mi-name', text: it.label }), el('span', { class: 'mi-sub', text: it.sub })) : el('span', { class: 'mi-text', text: it.label })));
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
    if (byKeyboard && first) first.focus(); else pop.focus();
  };
  ctl.toggle = (byKeyboard) => (pop ? ctl.close(true) : ctl.open(byKeyboard));
  button.setAttribute('aria-haspopup', 'menu');
  button.setAttribute('aria-expanded', 'false');
  button.addEventListener('click', (e) => ctl.toggle(!!(e && e.detail === 0 && e.isTrusted)));
  return ctl;
}

/* emptyState(icon, title, hint): the centred "nothing here" block (icon = a Blueprint icon name). */
function emptyState(icon, title, hint) {
  return el('div', { class: 'nonideal empty' },
    el('div', { class: 'empty-visual' }, ic(icon || 'info-sign')),
    el('h2', { class: 'empty-title', text: title || '' }),
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

/* ---- quick replies: one list per session in localStorage `ccboard:quick:<tmux>`, shared by the terminal page, the peek, the Agents rows, the inbox
   cards, the quad composer and the palette ---------
   Stored as a JSON array of lines; no key (or a list equal to THAT AGENT's defaults) means the agent's defaults. quickClean trims, drops empty and
   duplicate lines and keeps at most QUICK_MAX. QUICK_DEFAULTS_BY_AGENT is the one source of the words (#69):
     claude  the six most typed replies of the usage analysis (continue 128, merge 31, add commit push 19, push 18, pr 13, do it 10)
     codex   `continue` and the argument-free Codex TUI commands a chip can finish by itself (/status prints inline, verified on the box;
             /compact and /new run at once). Never `/model X` (Codex sends an inline argument to the model as a prompt), no picker command
             (/model, /permissions open a dialog a chip cannot answer: the Tune strip drives those), no y/yes/no (a typed yes is a new
             message in Codex, not an approval; permissions stay on the hook-backed Allow / Deny)
     shell   none (the owner may add some) */
const QUICK_DEFAULTS_BY_AGENT = Object.freeze({
  claude: Object.freeze(['continue', 'merge', 'push', 'pr', 'add commit push', 'do it']),
  codex: Object.freeze(['continue', '/status', '/compact', '/new']),
  shell: Object.freeze([]),
});
const QUICK_DEFAULTS = QUICK_DEFAULTS_BY_AGENT.claude;              // the Claude list under its old name (callers that know no agent)
function quickAgent(agent) { return agent === 'codex' || agent === 'shell' ? agent : 'claude'; }     // unknown (an external session): Claude's, as before
function quickDefaults(agent) { return QUICK_DEFAULTS_BY_AGENT[quickAgent(agent)].slice(); }
const QUICK_MAX = 12;
const QUICK_LINE_MAX = 200;
const QUICK_HOLD_MS = 500;
let quickEditing = null;                                             // the editor that is open, if any (a second long-press must not stack another)

/* A tmux name (this board's: the key it always had) or a roster row / ref: a session of another node keeps its own list, 'ccboard:quick:<handle>/<tmux>' (nodes.js Ref.storeKey). */
function quickKey(tmux) { return tmux !== null && typeof tmux === 'object' && typeof Ref !== 'undefined' ? Ref.storeKey('ccboard:quick:', tmux) : 'ccboard:quick:' + tmux; }

function quickClean(list) {
  const out = [];
  for (const x of Array.isArray(list) ? list : []) {
    const t = String(x === null || x === undefined ? '' : x).trim();
    if (t && !out.includes(t)) out.push(t);
    if (out.length >= QUICK_MAX) break;
  }
  return out;
}

function quickLoad(tmux, agent) {
  try {
    const raw = localStorage.getItem(quickKey(tmux));
    if (raw) { const v = JSON.parse(raw); if (Array.isArray(v)) return quickClean(v); }
  } catch (_) { /* no storage, or not JSON: the defaults */ }
  return quickDefaults(agent);
}

/* Save the cleaned list; null (or a list equal to the agent's defaults) removes the key. Returns the list that is in effect. */
function quickSave(tmux, items, agent) {
  const defs = quickDefaults(agent);
  const list = items === null ? defs : quickClean(items);
  const same = list.length === defs.length && list.every((x, i) => x === defs[i]);
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
  const b = el('button', { type: 'button', class: o.cls || '', title: 'types this text as a prompt: tap to send · hold to edit', text });
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
