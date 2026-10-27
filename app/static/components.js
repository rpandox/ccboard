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

function confirmButton(key, label, action, quiet, onChange, message) {
  // Two taps: the first turns the button into "Confirm …" + Cancel. A destructive button rests red-outlined (style.css); only the armed
  // "Confirm …" is filled red. `quiet` renders the resting state as a minimal (borderless-fill) button for rows crowded with everyday actions.
  // Repaint whichever page is mounted (a keyed roster row, the peek, the board): never a home-only renderer, so it is safe off the board.
  const repaint = () => { if (onChange) onChange(); else if (typeof repaintPage === 'function') repaintPage(); else if (typeof render === 'function') render(true); };
  if (ui.confirm === key) {
    return el('span', { class: 'row' },
      message ? el('span', { class: 'dim', text: message }) : null,
      el('button', { class: 'danger confirm', onclick: async () => { ui.confirm = null; try { await action(); setError(null); } catch (e) { setError(e.message); } await poll(true); }, text: `Confirm ${label}` }),
      el('button', { onclick: () => { ui.confirm = null; repaint(); }, text: 'Cancel' }));
  }
  return el('button', { class: 'danger' + (quiet ? ' minimal' : ''), title: `${label} (tap again to confirm)`, onclick: () => { if (onChange) { ui.confirm = key; repaint(); } else confirmArm(key); }, text: label });
}

/* #311: sessions being closed -> 0 while the DELETE is out, then when to stop counting; their rows (agents.js) dim and say "closing…". */
const sessionClosing = new Map();

/* Run by every poll: drop a settled name once the state lacks it; true when one outlived its 15 s (the caller repaints). */
function sessionClosingPrune(st) {
  if (!sessionClosing.size) return false;
  let expired = false;
  const live = new Set(rosterSessions(st).map((x) => x.tmux));            // pages/agents.js
  for (const [tmux, until] of Array.from(sessionClosing)) {
    if (!until) continue;
    if (!live.has(tmux)) sessionClosing.delete(tmux);
    else if (Date.now() > until) { sessionClosing.delete(tmux); expired = true; }
  }
  return expired;
}

async function sessionKill(tmux) {
  sessionClosing.set(tmux, 0);
  repaintPage();
  try {
    await api('DELETE', `/api/sessions/${encodeURIComponent(tmux)}`);
    sessionClosing.set(tmux, Date.now() + 15000);
  } catch (e) {
    sessionClosing.delete(tmux);
    repaintPage();
    toast(`${tmux} was not closed: ${errorText(e)}`, { kind: 'bad' });
  }
}

/* #313: Allow / Deny ('perm:<id>') and Ack ('ack:<tmux>') in flight -> {word, until}; until is 0 while the POST is out, then the 15 s safety expiry. */
const actPending = new Map();

function permMark(pr) { const d = pr && actPending.get('perm:' + pr.id); return d ? d.word : ''; }

/* The Allow / Deny pair: both off while the answer is out, the pressed one reading allowing… / denying…. The pressed button is blurred first: a focused
   button makes the Epic page hold its row's repaint back, and the pair would keep reading Allow / Deny (#318). */
function permButtons(pr, allowCls, denyCls) {
  const w = permMark(pr);
  return [['allow', 'Allow', allowCls, 'allowing…'], ['deny', 'Deny', denyCls, 'denying…']].map(([k, t, c, ing]) => el('button', { class: c, type: 'button', disabled: w ? true : null,
    onclick: (e) => { e.stopPropagation(); if (e.currentTarget && e.currentTarget.blur) e.currentTarget.blur(); decide(pr.id, k); }, text: w === k ? ing : t }));
}

/* needs_attention on one session of the state: the cards and every count read it, so an Ack paints by flipping it. */
function ackFlag(st, tmux, on) {
  for (const p of (st && st.projects) || []) for (const s of [...[p.root, ...(p.repos || [])].flatMap((r) => (r && r.sessions) || []), ...(p.orphan_sessions || [])]) if (s.tmux === tmux) s.needs_attention = on;
  if (typeof Shell !== 'undefined') Shell.cache = null;
}

/* POST an answer with its entry in actPending and paint(on) at the click; a refusal drops the entry, paints back and toasts. */
async function actPost(key, word, path, body, paint, failed) {
  actPending.set(key, { word, until: 0 });
  paint(true);
  try { await api('POST', path, body); } catch (e) {
    actPending.delete(key);
    paint(false);
    toast(`${failed}: ${errorText(e)}`, { kind: 'bad' });
    return false;
  }
  actPending.set(key, { word, until: Date.now() + 15000 });
  return true;
}

/* Run by every poll on the fresh state: an answered entry goes once the poll agrees (permission gone, session no longer needs you) or after its 15 s (true: repaint);
   the Acks still pending are laid over the state. */
function pendingApply(st) {
  let expired = false;
  const live = new Set([...(st.pending_permissions || []).map((p) => 'perm:' + p.id), ...rosterSessions(st).filter((x) => x.needs_attention).map((x) => 'ack:' + x.tmux)]);
  for (const [k, v] of Array.from(actPending)) {
    if (v.until && !live.has(k)) actPending.delete(k);
    else if (v.until && Date.now() > v.until) { actPending.delete(k); expired = true; }
  }
  for (const k of actPending.keys()) if (k.startsWith('ack:')) ackFlag(st, k.slice(4), false);
  return expired;
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

/* #79: "+2 queued", the prompts the BOARD itself has queued for a busy session (s.queued_prompts from the state builder: running tasks that wait for an
   earlier turn, whether the text is in Claude's queue or still held here). Only the board's own count: text typed straight into Claude's terminal is not
   known to the board, so nothing here claims it. Zero shows nothing. One chip on every surface (Home row, quad tile, terminal header). */
const QUEUED_PROMPTS_TITLE = 'prompts the board will send when the session is free';
function queuedPromptsChip() { return el('span', { class: 'bdg bdg-queued hidden' }); }
function queuedPromptsSet(node, n) {
  const k = Math.floor(Number(n));
  const on = Number.isFinite(k) && k > 0;
  if (on) {
    const text = `+${k} queued`;
    if (node.textContent !== text) node.textContent = text;
    node.setAttribute('title', QUEUED_PROMPTS_TITLE);
    node.setAttribute('aria-label', `${text}: ${QUEUED_PROMPTS_TITLE}`);
  }
  node.classList.toggle('hidden', !on);
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
      s.needs_attention ? el('button', { onclick: () => sessionAck(s), text: 'Ack' }) : null,
      confirmButton('kill:' + s.tmux, 'Kill session', () => sessionKill(s.tmux), true)));
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

/* ---------- shell components (v0.5.3): tabs, sheet, toast, menu, empty state. Definitions only: the DOM is touched when they are called. ---------- */

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
      pop.append(el('div', { class: 'menuitem' + (it.current ? ' is-current' : ''), role: 'menuitem', tabindex: '-1', title: it.title || null, 'aria-current': it.current ? 'true' : null, onclick: pick,
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

/* emptyState(icon, title, hint, actions): the centred "nothing here" block (icon = a Blueprint icon name). actions = nodes (buttons, links) for the next step the text names,
   centred under the hint (rule 3: an empty state never names an action without the control for it). */
function emptyState(icon, title, hint, actions) {
  return el('div', { class: 'nonideal empty' },
    el('div', { class: 'empty-visual' }, ic(icon || 'info-sign')),
    el('h2', { class: 'empty-title', text: title || '' }),
    hint ? el('div', { class: 'empty-hint dim', text: hint }) : null,
    Array.isArray(actions) && actions.length ? el('div', { class: 'empty-actions' }, ...actions) : null);
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

/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

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

