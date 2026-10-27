/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

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
    if (o.pending && base.some((t) => t.project === o.project && t.repo === o.repo && t.title === o.title && !o._seen.includes(t.id))) continue;
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

/* ---- the Move sheet: where does this Backlog card go (touch: a long press, the card's menu or the m key; a desktop card is dragged) ---- */

/* What the repo's saved launch choices say, for the subtitle of a lane button ('opus · high · acceptEdits'); '' when nothing is saved. */
function taskDefaultsLine(t, agent) {
  if (typeof TASK_KEY !== 'function') return '';
  const key = TASK_KEY({ name: t.project }, { name: t.repo }) + (agent === 'claude' ? '' : `:${agent}`);
  const v = loadPrefs(key);
  const bits = agent === 'claude' ? [v.model, v.effort, v.permission_mode === 'manual' ? 'ask' : v.permission_mode] : [v.model, v.reasoning_effort || v.effort, v.sandbox];
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
  return taskCardShell(t, ' backlog' + (queued ? ' queued' : '') + (t.pending ? ' pending' : ''), !queued && !t.pending,       // #311
    el('div', { class: 'row tk-title-row' }, el('span', { class: 'title', text: t.title }), queued ? el('span', { class: 'state ended', text: 'waiting for a step' }) : null),
    more ? el('div', { class: 'tk-prompt', text: more }) : null,
    el('div', { class: 'meta' }, taskWhere(t), t.pending ? ' · adding…' : (added ? ` · ${added}` : '')),
    chips.length ? el('div', { class: 'tk-chips' }, ...chips) : null,
    t.pending ? null : acts);
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

/* Work an epic started on this board (issue #149): t.epic is the board's own record of it ({phase, attempt, claim_url, ...}; no other task has the key). The card links the claim comment
   (https only), says the phase in words with a glyph, and shows a lost claim as a state of its own. */
const TASK_EPIC = { running: ['✽', 'running'], waiting: ['✻', 'waiting for a person'], pr: ['○', 'pull request opened'], done: ['✓', 'done'], failed: ['✕', 'failed'], cancelled: ['∙', 'cancelled'], lost: ['⚠', 'claim lost'] };

function taskClaimLink(t) {
  const u = String((t && t.epic && t.epic.claim_url) || '');
  return /^https:\/\/github\.com\/[A-Za-z0-9._\/-]+#issuecomment-[0-9]+$/.test(u) ? el('a', { class: 'tk-issue-link', href: u, target: '_blank', rel: 'noopener noreferrer', title: 'open the claim comment on the issue', text: 'claim' }) : null;
}

function taskEpicNode(t) {
  const e = t && t.epic;
  if (!e || typeof e !== 'object') return null;
  const ph = ownKey(TASK_EPIC, e.phase) ? e.phase : 'running';
  return el('div', { class: 'tk-epic' + (ph === 'lost' ? ' lost' : ''), 'data-epic-phase': ph },
    el('span', { class: 'tk-epic-g', 'aria-hidden': 'true', text: TASK_EPIC[ph][0] }), ` Epic work: ${TASK_EPIC[ph][1]}${e.attempt > 1 ? ` (attempt ${e.attempt})` : ''}`,
    ph === 'lost' ? el('span', { class: 'dim', text: ' The issue may go to another node. This task is parked and its worktree is kept.' }) : null);
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
  if (t.epic && t.epic.phase === 'lost') chips.push(el('span', { class: 'state waiting tk-lostclaim', title: 'this board lost its claim on the issue; it writes nothing more for it', text: '⚠ lost claim' }));
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
  const termAct = live ? { id: 'terminal', label: s.pane_dialog ? 'Open' : 'Terminal', href: `/term/${enc}`, newTab: true, title: 'open the terminal in a new tab' } : null;
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
    el('div', { class: 'meta' }, starting ? `${taskWhere(t)} · starting…` : (t.branch ? `${t.project}/${t.repo} · ${t.branch}` : taskWhere(t) + (t.mode === 'attached' ? ' · in place' : '')), t.pr_url ? ' · PR #' + t.pr_number : null, t.issue_number ? [' · ', taskIssueLink(t)] : null, taskClaimLink(t) ? [' · ', taskClaimLink(t)] : null,
      typeof t.cost_usd === 'number' ? [' · ', el('span', { class: 'mono', text: '$' + t.cost_usd.toFixed(2) })] : null),
    chips.length ? el('div', { class: 'tk-chips' }, ...chips) : null,
    taskEpicNode(t),
    pvOff ? taskPreviewOff(t, pvOff) : null,
    pending && live ? taskAutoCloseStrip(t, ac) : null,
    s && s.pane_dialog ? el('div', { class: 'last' }, ...s.pane_dialog.split('\n').map((line) => el('div', { text: line })))
      : s && s.last_message && !(finished && t.result) ? el('div', { class: 'last', text: s.last_message.slice(0, 160) }) : null,
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

