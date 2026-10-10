/* ccboard drag-and-drop dispatch (v0.5.15, namespace Dnd): drag a Backlog task card onto an agent lane (a new session) or onto a running
   session (hand it the prompt), on a fine pointer. Definitions only: nothing here touches the DOM or starts a timer at load. The delegated
   listeners go up on first use (Dnd.bind, Dnd.bindCard and Dnd.source call Dnd.install); main.js starts nothing for this file.

   The contract the other files code against:
     source   a .task[data-task=<id>] card whose task is in phase 'backlog' becomes draggable under a fine pointer on its first pointerover or
              pointerdown (nothing in the card's own code is needed); the grip is a CSS ::before in shell.css. Dnd.bindCard(card, task) / Dnd.source
              mark one explicitly (a grip node carrying data-task and draggable=true inside the card works too: the whole card is the drag image).
     targets  any node with [data-drop]; the attributes say what it is, never the class:
                data-drop="lane"    data-agent="claude|codex"  [data-project=<name>]   drop = a new session of that agent
                data-drop="session" data-tmux=<tmux name> [data-node=<handle>]         drop = hand the prompt to that session (data-node only on a session of
                                                                                       another node: a target is its Ref.key, so one name on two nodes is two
                                                                                       targets; the drop refuses a remote one until the relay phase)
              Session rows (agents.js sessionCard, the sidebar's s-row), the dispatch bar's lanes and chips (Dnd.laneBar, mounted by home.js renderTasks and
              by pages/project.js makeTaskBoard), and later quad tiles and the dock header carry them. Dnd.bind(node, spec) sets them and installs the listeners;
              Dnd.bind(root) only installs.
     state    Dnd.dragging = {id, task} while a card is held; body.dnd-task (iframes stop catching the pointer, see shell.css); every target gets
              .drop-ok or .drop-no, the one under the pointer also .drop-over and data-drop-note (the reason, or 'will be queued after the current turn'),
              shown next to the pointer by one floating .dnd-hint.
     after    a drop on a valid target opens the confirm popover (Dnd.confirm), or dispatches at once when localStorage ccboard:dnd:instant is '1';
              Dnd.dispatch paints the card into In progress through store.tasksOverride (components.js) before POST /api/tasks/<id>/dispatch answers,
              and stays on the board (a Start from the card opens the peek; a drop does not).
     synthetic a drop with no dragstart before it (a DragEvent built by hand) works when its dataTransfer carries the MIME and the card's id.
     repaint  a page that rebuilds the board asks Dnd.afterDrag(fn) first: while a card is held it queues fn and answers true (a node that leaves the
              DOM mid-drag never gets its dragend). Dnd.end() runs the queue. */
'use strict';

const Dnd = {
  MIME: 'application/x-ccboard-task',
  INSTANT_KEY: 'ccboard:dnd:instant',
  MAX_CHIPS: 8,          // a lane lists at most this many running sessions as chips
  dragging: null,        // { id, task } while a task card is dragged
  over: null,            // the [data-drop] node under the pointer
  ready: false,          // the delegated listeners are installed
  held: [],              // repaints queued by afterDrag() until the drag ends
  tick: 0,               // Date.now() of the last dragstart / dragover: a mousemove long after it means the drag is over
  pop: null,             // the open confirm popover: { node, close }
  hintNode: null,        // the floating reason / queue note next to the pointer while a card is over a target
};

/* ---------- facts about the world ---------- */

/* (pointer: fine) and not the html.force-coarse QA switch: the one place that says "this is a mouse". */
Dnd.fine = function () {
  try { if (document.documentElement && document.documentElement.classList && document.documentElement.classList.contains('force-coarse')) return false; } catch (_) { /* no document element */ }
  try { return !!(window.matchMedia && window.matchMedia('(pointer: fine)').matches); } catch (_) { return false; }
};

Dnd.state = function () { return typeof state !== 'undefined' ? state : null; };

/* The board's rows: the poll's tasks with the optimistic overrides laid over them (components.js boardTasks). */
Dnd.tasks = function (st) {
  const cur = st || Dnd.state();
  if (typeof boardTasks === 'function') return boardTasks(cur);
  return cur && Array.isArray(cur.tasks) ? cur.tasks : [];
};

Dnd.taskById = function (id, st) {
  const want = String(id);
  return Dnd.tasks(st).find((t) => String(t.id) === want) || null;
};

/* Only a Backlog card can be dispatched: a queued one waits for its step (409), a started one is already somewhere. */
Dnd.draggable = function (t) {
  return !!t && t.id !== undefined && t.id !== null && (t.phase || 'running') === 'backlog';
};

Dnd.agentOf = function (s) {
  if (typeof sessionAgent === 'function') return sessionAgent(s);
  return s.agent || (s.launcher === 'shell' || s.launcher === 'clone' ? 'shell' : 'claude');
};

/* Every session in a state payload with its project and repo ('root' = the project folder). */
Dnd.sessions = function (st) {
  if (typeof rosterSessions === 'function') return rosterSessions(st);
  const out = [];
  for (const p of ((st && st.projects) || [])) {
    for (const r of [p.root, ...(p.repos || [])]) {
      if (!r) continue;
      for (const s of (r.sessions || [])) out.push({ ...s, project: p.name, repo: r.name });
    }
    for (const s of (p.orphan_sessions || [])) out.push({ ...s, project: p.name, repo: s.repo || '?' });
  }
  return out;
};

Dnd.index = function (st) {
  const m = new Map();
  for (const s of Dnd.sessions(st || Dnd.state())) if (s && s.tmux) m.set(Ref.key(s), s);      // by Ref.key: the same name on two nodes is two entries
  return m;
};

Dnd.agentLabel = function (agent) { return agent === 'codex' ? 'Codex' : agent === 'claude' ? 'Claude' : String(agent || ''); };

/* What a [data-drop] node is. */
Dnd.targetOf = function (node) {
  const kind = node.getAttribute('data-drop');
  if (kind === 'lane') return { kind, agent: node.getAttribute('data-agent') || 'claude', project: node.getAttribute('data-project') || '' };
  const tmux = node.getAttribute('data-tmux') || '';
  const from = Ref.nodeOf(node.getAttribute('data-node'));                    // data-node is absent on this board's own rows: their target is what it always was
  return from === null ? { kind: 'session', tmux } : { kind: 'session', tmux, node: from };
};

/* ---------- the accept rules ---------- */

/* Can this task land on this target? {ok, why, note, kind, agent, tmux?, session?, queue?, force?, autoClose}. `why` says what is wrong when not ok;
   `note` what to know when ok. The rules mirror the Move sheet's session list (components.js taskSessionTargets) and the server's 409 matrix:
   an ended or unknown session, a shell, another agent or project, a permission decision or a dialog waiting, a session that has just started or
   stopped with an error all refuse. A working Claude session accepts: its own input queue holds the prompt until the turn ends. A session in
   another repo of the same project accepts with `force` (the prompt then starts with "Work in <the task's repo>."). */
Dnd.verdict = function (task, target, st, idx) {
  const no = (why) => ({ ok: false, why, note: why });
  if (!task) return no('no task');
  if (!Dnd.draggable(task)) return no('already started');
  const cur = st || Dnd.state();
  const agent = task.agent || 'claude';
  if (target.kind === 'lane') {
    if (target.agent !== 'claude' && target.agent !== 'codex') return no('unknown lane');
    if (target.project && task.project && target.project !== task.project) return no('another project');
    if (target.agent === 'codex') {
      const cx = cur && cur.agents && cur.agents.codex;
      if (!(cx && cx.installed)) return no('Codex is not installed');
    }
    return { ok: true, kind: 'lane', agent: target.agent, note: '', autoClose: true };
  }
  const s = (idx || Dnd.index(cur)).get(Ref.key(target));
  if (!s) return no('unknown session');
  if (Ref.nodeOf(s) !== null) return no('on another node');                 // the relay (a later phase) hands work over; a drop must never reach this board's session of the same name
  const stt = s.state || 'unknown';
  if (stt === 'ended') return no('session ended');
  const sAgent = Dnd.agentOf(s);
  if (sAgent === 'shell' || s.name === 'clone') return no('not an agent session');
  if (sAgent !== agent) return no(`a ${Dnd.agentLabel(agent)} task cannot go to a ${Dnd.agentLabel(sAgent)} session`);
  if (task.project && s.project && s.project !== task.project) return no('another project');
  if (((cur && cur.pending_permissions) || []).some((p) => p.tmux_name === s.tmux)) return no('waiting for a permission decision');
  if (stt === 'unknown') return no('starting…');
  if (stt === 'errored') return no('stopped with an error');
  if (stt === 'waiting' && s.wait_kind !== 'idle_prompt') return no('waiting on a prompt: answer it first');
  const out = { ok: true, kind: 'session', agent: sAgent, tmux: s.tmux, session: s, autoClose: false, note: '' };
  if (stt === 'working') {
    if (sAgent !== 'claude') return no('working: wait for its turn to end');
    if (s.flags && s.flags.compacting) return no('compacting: wait for it to finish');       // the server refuses a queued prompt during /compact (409)
    out.queue = true;
    out.note = 'will be queued after the current turn';
  }
  if (task.repo && s.repo && s.repo !== task.repo) {
    out.force = true;
    out.note = (out.note ? out.note + ' · ' : '') + `works in ${s.repo === 'root' ? 'the project folder' : s.repo}: the prompt goes in with "Work in ${task.repo === 'root' ? 'the project folder' : task.repo}."`;
  }
  return out;
};

Dnd.verdictFor = function (node, task) {
  const t = task || (Dnd.dragging && Dnd.dragging.task);
  return Dnd.verdict(t, Dnd.targetOf(node), Dnd.state());
};

/* ---------- marking: the drop-ok ring and the dimming ---------- */

Dnd.paint = function () {
  const t = Dnd.dragging && Dnd.dragging.task;
  if (!t || typeof document === 'undefined') return;
  const st = Dnd.state();
  const idx = Dnd.index(st);
  for (const n of document.querySelectorAll('[data-drop]')) {
    const v = Dnd.verdict(t, Dnd.targetOf(n), st, idx);
    n.classList.toggle('drop-ok', !!v.ok);
    n.classList.toggle('drop-no', !v.ok);
  }
};

/* The note next to the pointer: why a target refuses, or what will happen ('will be queued after the current turn'). One floating node, never
   inside a row (rows clip and re-grid their children). Gone when `text` is empty. */
Dnd.hint = function (text, bad, at) {
  if (!text) { if (Dnd.hintNode) { Dnd.hintNode.remove(); Dnd.hintNode = null; } return; }
  if (!Dnd.hintNode) { Dnd.hintNode = el('div', { class: 'dnd-hint', role: 'status' }); document.body.append(Dnd.hintNode); }
  const n = Dnd.hintNode;
  setText(n, text);
  n.classList.toggle('no', !!bad);
  if (at && typeof at.x === 'number') {
    const w = typeof window !== 'undefined' && window.innerWidth ? window.innerWidth : 1024;
    n.style.left = `${Math.max(8, Math.min(Math.round(at.x + 14), w - 336))}px`;
    n.style.top = `${Math.round(at.y + 18)}px`;
  }
};

Dnd.hover = function (node, v, at) {
  if (Dnd.over && Dnd.over !== node) { Dnd.over.classList.remove('drop-over'); Dnd.over.removeAttribute('data-drop-note'); }
  Dnd.over = node || null;
  if (!node) { Dnd.hint(''); return; }
  node.classList.add('drop-over');
  const note = v ? (v.ok ? v.note : v.why) : '';
  if (note) node.setAttribute('data-drop-note', note); else node.removeAttribute('data-drop-note');
  Dnd.hint(note, !!v && !v.ok, at);
};

Dnd.clear = function () {
  if (typeof document === 'undefined') return;
  try {
    for (const n of document.querySelectorAll('.drop-ok, .drop-no, .drop-over, .dnd-src')) n.classList.remove('drop-ok', 'drop-no', 'drop-over', 'dnd-src');
    for (const n of document.querySelectorAll('[data-drop-note]')) n.removeAttribute('data-drop-note');
    if (document.body) document.body.classList.remove('dnd-task');
    Dnd.hint('');
  } catch (e) { console.error('ccboard dnd', e); }
};

/* The drag is over (dragend, a drop, a window blur, an Escape, or the first mouse move after a drag whose dragend never came): clear every mark,
   then run the repaints that waited for it. */
Dnd.end = function () {
  const was = Dnd.dragging;
  Dnd.dragging = null;
  Dnd.over = null;
  Dnd.clear();
  const held = Dnd.held;
  Dnd.held = [];
  for (const fn of held) { try { fn(); } catch (e) { console.error('ccboard dnd', e); } }
  return was;
};

/* A page that rebuilds the board calls this first: true means "a card is held, your repaint is queued, do nothing now". */
Dnd.afterDrag = function (fn) {
  if (!Dnd.dragging) return false;
  if (typeof fn === 'function' && !Dnd.held.includes(fn)) Dnd.held.push(fn);
  return true;
};

/* ---------- the source: a Backlog card ---------- */

Dnd.source = function (card, t) {
  Dnd.install();
  if (!card || typeof card.setAttribute !== 'function') return card;
  const on = Dnd.draggable(t) && Dnd.fine();
  if (on) { card.setAttribute('draggable', 'true'); card.setAttribute('data-dnd', '1'); }
  else if (card.getAttribute('data-dnd') === '1') { card.removeAttribute('draggable'); card.removeAttribute('data-dnd'); }
  return card;
};

/* The card a node belongs to: .task[data-task] only (the chain strip's step buttons carry data-task too, and are not cards). */
Dnd.cardOf = function (target) { return target && typeof target.closest === 'function' ? target.closest('.task[data-task]') : null; };

/* A Backlog card is a drag source: marked on the first pointerover or pointerdown under a fine pointer (cards are rebuilt on every repaint, so this
   runs per card, not per page). components.js may also call Dnd.bindCard when it builds one. */
Dnd.bindCard = function (card, t) { return Dnd.source(card, t); };

Dnd.onPointer = function (e) {
  if (e.pointerType === 'touch' || !Dnd.fine()) return;
  const card = Dnd.cardOf(e.target);
  if (!card || card.getAttribute('draggable') === 'true') return;         // already marked: dragstart looks at the task again anyway
  const phase = card.getAttribute('data-phase');
  if (phase && phase !== 'backlog') return;                                // a started card never is a source: no lookup for every move over it
  Dnd.source(card, Dnd.taskById(card.getAttribute('data-task')));
};

Dnd.onDragStart = function (e) {
  const whole = Dnd.cardOf(e.target);
  if (!whole || !e.dataTransfer) return;
  const t = Dnd.taskById(whole.getAttribute('data-task'));
  if (!Dnd.fine() || !Dnd.draggable(t)) { e.preventDefault(); return; }
  Dnd.dragging = { id: t.id, task: t };
  Dnd.tick = Date.now();
  try {
    e.dataTransfer.setData(Dnd.MIME, String(t.id));
    e.dataTransfer.effectAllowed = 'move';
    if (e.target !== whole && typeof e.dataTransfer.setDragImage === 'function') e.dataTransfer.setDragImage(whole, 14, 14);      // started on a grip inside the card: the whole card is what moves
  } catch (_) { /* a synthetic event */ }
  document.body.classList.add('dnd-task');
  Dnd.paint();
  setTimeout(() => { if (Dnd.dragging && Dnd.dragging.id === t.id) whole.classList.add('dnd-src'); }, 0);    // after the drag image is taken: it must not be the faded card
};

Dnd.onDragOver = function (e) {
  if (!Dnd.dragging) return;
  Dnd.tick = Date.now();
  const node = e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-drop]') : null;
  if (!node) { Dnd.hover(null); return; }
  const v = Dnd.verdictFor(node);
  Dnd.hover(node, v, { x: e.clientX, y: e.clientY });
  try { e.dataTransfer.dropEffect = v.ok ? 'move' : 'none'; } catch (_) { /* a synthetic event */ }
  if (v.ok) e.preventDefault();                                           // only a valid target is a drop target: the others show the not-allowed cursor
};

/* Crossing from a target to its child fires dragleave too, and the next dragover (every ~50 ms) says where the pointer is, so only a pointer that
   leaves the window clears the ring here (clientX / clientY come as 0 or past the edge then). */
Dnd.onDragLeave = function (e) {
  if (!Dnd.dragging || !Dnd.over || e.relatedTarget) return;
  const w = typeof window !== 'undefined' ? window.innerWidth : 0;
  const h = typeof window !== 'undefined' ? window.innerHeight : 0;
  if (!(e.clientX > 0) || !(e.clientY > 0) || (w && e.clientX >= w) || (h && e.clientY >= h)) Dnd.hover(null);
};

Dnd.onDrop = function (e) {
  let drag = Dnd.dragging;
  if (!drag) {                                       // a drop that never saw its dragstart (a synthetic DragEvent, a drag begun in another window): the MIME data names the card
    let id = '';
    try { id = e.dataTransfer ? e.dataTransfer.getData(Dnd.MIME) : ''; } catch (_) { /* no data */ }
    const t = id ? Dnd.taskById(id) : null;
    if (!t) return;
    drag = { id: t.id, task: t };
  }
  const node = e.target && typeof e.target.closest === 'function' ? e.target.closest('[data-drop]') : null;
  const v = node ? Dnd.verdictFor(node, drag.task) : null;
  const at = { x: e.clientX, y: e.clientY };
  if (!node || !v || !v.ok) { Dnd.end(); return; }
  e.preventDefault();
  const plan = Dnd.plan(drag.task, v);
  Dnd.end();
  Dnd.drop(plan, at);
};

/* A mouse move cannot happen during a native drag: one that comes long after the last drag event is the end of a drag that never reported it. */
Dnd.onMove = function () {
  if (Dnd.dragging && Date.now() - Dnd.tick > 700) Dnd.end();
};

Dnd.onKey = function (e) {
  if (e.key === 'Escape' && Dnd.dragging) Dnd.end();
};

Dnd.install = function () {
  if (Dnd.ready) return false;
  if (typeof document === 'undefined' || typeof document.addEventListener !== 'function') return false;
  Dnd.ready = true;
  document.addEventListener('pointerover', Dnd.onPointer, true);
  document.addEventListener('pointerdown', Dnd.onPointer, true);
  document.addEventListener('dragstart', Dnd.onDragStart, true);
  document.addEventListener('dragover', Dnd.onDragOver, true);
  document.addEventListener('dragenter', Dnd.onDragOver, true);
  document.addEventListener('dragleave', Dnd.onDragLeave, true);
  document.addEventListener('drop', Dnd.onDrop, true);
  document.addEventListener('dragend', () => { Dnd.end(); }, true);
  document.addEventListener('mousemove', Dnd.onMove, true);
  document.addEventListener('keydown', Dnd.onKey, true);
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('dragend', () => { Dnd.end(); }, true);
    window.addEventListener('drop', () => { if (Dnd.dragging) Dnd.end(); });
    window.addEventListener('blur', () => { if (Dnd.dragging) Dnd.end(); });
  }
  return true;
};

/* Dnd.bind(node, {drop, tmux, agent, project}): make a node a drop target (the attributes the delegated listeners read) and make sure they are up.
   A row that already carries data-drop and data-tmux calls it with no spec. */
Dnd.bind = function (node, spec) {
  if (node && spec && typeof node.setAttribute === 'function') {
    if (spec.drop) node.setAttribute('data-drop', spec.drop);
    if (spec.tmux) node.setAttribute('data-tmux', spec.tmux);
    if (spec.node) node.setAttribute('data-node', spec.node);
    if (spec.agent) node.setAttribute('data-agent', spec.agent);
    if (spec.project) node.setAttribute('data-project', spec.project);
  }
  Dnd.install();
  return node;
};

/* ---------- a drop: plan, confirm, dispatch ---------- */

Dnd.plan = function (task, v) {
  return { task, kind: v.kind, agent: v.agent, tmux: v.tmux || '', session: v.session || null, queue: !!v.queue, force: !!v.force, autoClose: !!v.autoClose, note: v.note || '' };
};

Dnd.instant = function () {
  try { return localStorage.getItem(Dnd.INSTANT_KEY) === '1'; } catch (_) { return false; }
};

Dnd.drop = function (plan, at) {
  if (Dnd.instant()) return Dnd.dispatch(plan, {});
  return Dnd.confirm(plan, at);
};

/* The POST body: a lane starts a new session of the agent (model / effort / permissions only when the person changed them in the popover; the
   card's own remembered options apply otherwise), a session gets the prompt (queue for a working Claude, force across repos). */
Dnd.body = function (plan, o) {
  const opt = o || {};
  const auto = typeof opt.autoClose === 'boolean' ? opt.autoClose : plan.autoClose;
  if (plan.kind === 'lane') {
    const b = { mode: 'lane', agent: plan.agent, auto_close: auto };
    for (const k of ['model', 'effort', 'permission_mode']) if (opt.opts && opt.opts[k]) b[k] = opt.opts[k];
    return b;
  }
  const b = { session: plan.tmux, auto_close: auto };
  if (plan.force) b.force = true;
  if (plan.queue) b.queue = true;
  return b;
};

/* Dispatch: the card moves to In progress at once (store.tasksOverride, the same optimism as Start and Send to session), the response settles it,
   an error puts the card back. The override goes when the poll returns the task with the new tmux (components.js boardTasks / taskConfirmed). */
Dnd.dispatch = async function (plan, o) {
  const t = plan.task;
  const cur = store.tasksOverride[t.id];
  if (cur && cur._busy) return null;                                      // a second drop on a card that is already on its way
  const lane = plan.kind === 'lane';
  const body = Dnd.body(plan, o);
  const sname = plan.session ? plan.session.name : (typeof sessionNameOf === 'function' ? sessionNameOf(plan.tmux) : plan.tmux);
  const pending = { ...t, phase: 'running', column: 'in_progress', tmux: lane ? '' : plan.tmux, mode: lane ? 'worktree' : 'session', agent: plan.agent,
    auto_close: !!body.auto_close, session_row: !lane && plan.session && plan.session.row_id !== undefined ? plan.session.row_id : null, session: taskStartingSession() };
  taskOverrideSet(pending, ['phase', 'column', 'tmux', 'session_row', 'mode', 'agent', 'auto_close', 'session'], { _busy: true });
  taskRepaint();
  try {
    const res = await api('POST', `/api/tasks/${t.id}/dispatch`, body);
    const row = taskRowFromResponse(pending, res, lane ? { mode: 'worktree' } : { mode: 'session', tmux: plan.tmux });
    if (lane && !row.tmux && taskDemo()) row.tmux = `${t.project}--${t.repo}--t-${t.slug}`;
    taskOverrideSet(row);
    toast(lane ? `started ${row.slug || t.slug} in ${Dnd.agentLabel(plan.agent)}` : (plan.queue ? `queued in ${sname}` : `sent to ${sname}`), { kind: 'ok' });
    if (typeof taskWarn === 'function') taskWarn(res);                    // limit_warning: a hand dispatch is never held, only warned
    else if (res && res.limit_warning) toast(typeof res.limit_warning === 'string' ? res.limit_warning : 'Close to the usage limit', { kind: 'warn', ttl: 9000 });
    taskRepaint();
    if (typeof poll === 'function') poll(true);
    return res;
  } catch (e) {
    taskOverrideDrop(t.id);
    taskRepaint();
    taskFail(e);
    return null;
  }
};

/* ---------- the confirm popover ---------- */

Dnd.closePop = function () {
  const p = Dnd.pop;
  if (p) p.close();
};

/* What the repo's saved launch defaults say, as 'opus · acceptEdits'. A row that carries what it remembered (t.spec) wins over the form's memory. */
Dnd.prefsOf = function (t, agent) {
  if (typeof loadPrefs !== 'function' || typeof TASK_KEY !== 'function') return {};
  const base = TASK_KEY({ name: t.project }, { name: t.repo });
  if (agent && agent !== 'claude') return loadPrefs(`${base}:${agent}`);
  return loadPrefs(base);
};

Dnd.summary = function (t, agent) {
  const src = (t && t.spec && typeof t.spec === 'object' ? t.spec : null) || Dnd.prefsOf(t, agent) || {};
  const model = src.model_sel === 'custom' ? src.model_id : (src.model_sel || src.model || '');
  const bits = [model, src.effort || src.reasoning_effort, src.permission_mode].filter(Boolean);
  return bits.length ? bits.join(' · ') : 'repo defaults';
};

Dnd.confirm = function (plan, at) {
  Dnd.closePop();
  const t = plan.task;
  const lane = plan.kind === 'lane';
  const label = Dnd.agentLabel(plan.agent);
  const sname = plan.session ? plan.session.name : plan.tmux;
  const where = typeof taskWhere === 'function' ? taskWhere(t) : `${t.project}/${t.repo}`;
  const row = (k, v) => el('div', { class: 'dnd-pop-row' }, el('span', { class: 'dnd-pop-k', text: k }), el('span', { class: 'dnd-pop-v', text: v }));

  const auto = el('input', { type: 'checkbox', 'aria-label': 'Close the session when it finishes' });
  auto.checked = !!plan.autoClose;
  const sw = el('label', { class: 'dnd-switch', title: 'Ends the session a minute after the task finishes, unless it asks you something' },
    el('span', { class: 'dnd-switch-text', text: 'Close the session when it finishes' }), auto);

  /* Edit options…: the launcher in dispatch mode (launch(): the same sheet as the Move sheet's Options…) when it exists, else the
     launch controls opened in place (model, effort, permissions: Claude only, and only what the person touches is sent). */
  let lc = null;
  let opened = false;
  const optsHost = el('div', { class: 'dnd-opts hidden' });
  const hook = typeof openLauncher === 'function' || typeof taskDispatchSheet === 'function';       // launch() (components.js): the launcher in dispatch mode, else the dispatch sheet
  const inline = !hook && lane && plan.agent === 'claude' && typeof launchControls === 'function';
  const optsBtn = !lane || !(hook || inline) ? null
    : el('button', { class: 'minimal dnd-optsbtn', type: 'button', 'aria-expanded': hook ? null : 'false', text: 'Edit options…', onclick: () => {
      if (hook) { Dnd.closePop(); launch(taskLaunchOpts(t, { agent: plan.agent, auto_close: auto.checked })); return; }
      opened = !opened;
      if (opened && !lc) {
        lc = launchControls(Dnd.prefsOf(t, plan.agent) || {}, typeof PERMS !== 'undefined' ? PERMS : [['', 'ask (default)']]);
        optsHost.append(lc.grid);
      }
      optsHost.classList.toggle('hidden', !opened);
      optsBtn.setAttribute('aria-expanded', opened ? 'true' : 'false');
      Dnd.place(dlg, at);
    } });

  const go = el('button', { class: 'primary tinted dnd-go', type: 'submit', text: lane ? 'Start' : (plan.queue ? 'Queue it' : 'Send') });
  const cancel = el('button', { class: 'dnd-cancel', type: 'button', text: 'Cancel', onclick: () => Dnd.closePop() });
  const form = el('form', { class: 'dnd-pop-form', onsubmit: (e) => {
    e.preventDefault();
    const opts = opened && lc ? lc.read() : null;
    Dnd.closePop();
    Dnd.dispatch(plan, { autoClose: auto.checked, opts });
  } },
  el('div', { class: 'dnd-pop-head' }, agentGlyph(plan.agent), el('strong', { text: lane ? `Start in a new ${label} session` : `Hand to ${sname}` })),
  el('div', { class: 'dnd-pop-facts' },
    row('Task', t.title),
    row('Where', where),
    lane ? row('Launch', Dnd.summary(t, plan.agent)) : row('Session', `${sname}${plan.session && plan.session.state ? ' · ' + plan.session.state : ''}`)),
  plan.note ? el('p', { class: 'dnd-pop-note', role: 'status', text: plan.note }) : null,
  sw,
  optsHost,
  el('div', { class: 'dnd-pop-actions' }, ...(optsBtn ? [optsBtn] : []), cancel, go));

  const dlg = el('dialog', { class: 'dnd-pop', role: 'dialog', 'aria-label': 'Confirm dispatch' }, form);
  const onKey = (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); Dnd.closePop(); } };
  const onDown = (e) => { if (!dlg.contains(e.target)) Dnd.closePop(); };
  const onHash = () => Dnd.closePop();
  const close = () => {
    if (Dnd.pop && Dnd.pop.node === dlg) Dnd.pop = null;
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('pointerdown', onDown, true);
    window.removeEventListener('hashchange', onHash);
    dlg.remove();
  };
  Dnd.pop = { node: dlg, close, plan };
  document.body.append(dlg);
  if (typeof dlg.show === 'function') dlg.show(); else dlg.setAttribute('open', '');
  Dnd.place(dlg, at);
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('pointerdown', onDown, true);
  window.addEventListener('hashchange', onHash);
  go.focus();                                                             // Enter confirms
  return dlg;
};

/* Put the popover next to where the card was dropped, inside the window. Plain left / top (CSSOM, like menu()), never a style attribute. */
Dnd.place = function (dlg, at) {
  try {
    const w = window.innerWidth || 0;
    const h = window.innerHeight || 0;
    const r = dlg.getBoundingClientRect();
    const x0 = at && typeof at.x === 'number' ? at.x : w / 2;
    const y0 = at && typeof at.y === 'number' ? at.y : h / 3;
    const left = Math.max(8, Math.min(x0 + 12, w - r.width - 8));
    const top = Math.max(8, Math.min(y0 + 12, h - r.height - 8));
    dlg.style.left = `${Math.round(left)}px`;
    dlg.style.top = `${Math.round(top)}px`;
  } catch (_) { /* no layout */ }
};

/* ---------- the dispatch bar: lanes always above the board ---------- */

/* Dnd.laneBar({project?}) -> a node; node.ccPatch(state) keeps it current (call it from every render of the page that mounts it). Two lanes,
   ◆ Claude and ◇ Codex (Codex shows once state.agents.codex.installed): the lane body is a drop target for "a new session", and each running
   session of that agent (of `project` when given, else of every project) is a chip that is a drop target for "hand it to that one".
   On a touch screen the bar is hidden by CSS: the Move sheet is the touch path. */
Dnd.laneBar = function (opts) {
  const o = opts || {};
  const project = o.project || '';
  Dnd.install();
  const bar = el('div', { class: 'dnd-bar', role: 'group', 'aria-label': 'Drop a backlog card on an agent to start it, or on a session to hand it over' });
  const lanes = {};
  for (const agent of ['claude', 'codex']) {
    const chips = el('span', { class: 'dnd-chips' });
    const node = el('div', { class: 'dnd-lane', 'data-drop': 'lane', 'data-agent': agent, 'data-project': project || null, 'data-lane': agent },
      el('span', { class: 'dnd-lane-head' }, agentGlyph(agent), el('strong', { text: Dnd.agentLabel(agent) }), el('span', { class: 'dim dnd-lane-hint', text: 'new session' })),
      chips);
    const list = makeKeyedList(chips, { key: (s) => Ref.key(s), create: (s) => Dnd.chip(s), patch: (n, s) => n.ccPatch(s) });
    lanes[agent] = { node, chips, list };
    bar.append(node);
  }
  bar.ccPatch = function (st) {
    const cur = st || Dnd.state();
    const cx = cur && cur.agents && cur.agents.codex;
    lanes.codex.node.classList.toggle('hidden', !(cx && cx.installed));
    const all = Dnd.sessions(cur).filter((s) => s.state !== 'ended' && s.name !== 'clone' && (!project || s.project === project));
    for (const agent of ['claude', 'codex']) {
      const shown = agent === 'claude' || !!(cx && cx.installed);
      const mine = shown ? all.filter((s) => Dnd.agentOf(s) === agent).sort((a, b) => String(a.name).localeCompare(String(b.name), undefined, { numeric: true })) : [];
      lanes[agent].list.update(mine.slice(0, Dnd.MAX_CHIPS));
      lanes[agent].node.setAttribute('data-count', String(mine.length));
      lanes[agent].node.setAttribute('title', mine.length ? `Drop a task here to start it in a new ${Dnd.agentLabel(agent)} session, or on a chip to hand it to that session` : `Drop a task here to start it in a new ${Dnd.agentLabel(agent)} session`);
    }
    if (Dnd.dragging) Dnd.paint();
  };
  bar.ccPatch(Dnd.state());
  return bar;
};

/* One running session in a lane: a link to its peek that is also a drop target. */
Dnd.chip = function (s) {
  const slot = el('span', { class: 'dnd-chip-g' });
  const name = el('span', { class: 'dnd-chip-name mono' });
  const remote = Ref.nodeOf(s) !== null;
  const node = el('a', { class: 'dnd-chip', 'data-drop': 'session', 'data-tmux': s.tmux, 'data-node': remote ? Ref.nodeOf(s) : null, draggable: 'false',
    href: remote ? (Ref.hash(s) || '#') : (typeof taskPeekHash === 'function' ? taskPeekHash(s.tmux) : '#') }, slot, name);
  node.ccPatch = function (s2) {
    const key = s2.state || 'unknown';
    if (node._k !== key) { node._k = key; slot.textContent = ''; slot.append(stateGlyph(key)); }
    setText(name, s2.name || s2.tmux);
    node.setAttribute('title', `${s2.project}/${s2.repo === 'root' ? 'project folder' : s2.repo} · ${s2.name} · ${key}`);
  };
  node.ccPatch(s);
  return node;
};
