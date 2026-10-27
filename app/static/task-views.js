/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

let homeDndAsked = false;
let homeLaneBar = null;                                            // the dispatch bar of #/tasks (dnd.js): one node kept across repaints

function renderTasks() {
  const sec = $('#tasks');
  if (!sec) return;
  if (typeof Dnd !== 'undefined' && Dnd.afterDrag(renderTasks)) return;      // a card is being dragged: the board repaints once it is dropped, never under the pointer
  sec.textContent = '';
  const list = boardTasks(state);                                  // the poll's rows with the optimistic Start / add / edit / delete laid over them
  if (!list.length) { sec.classList.add('hidden'); return; }
  sec.classList.remove('hidden');
  // the page's own head (no card around the board: the task cards are the only bordered boxes), then one column per column that has a card
  sec.append(el('div', { class: 'page-head tk-head' },
    el('h1', { text: `Tasks (${list.length})` }),
    el('span', { class: 'summary', text: 'one worktree + branch per task; columns follow the session state' }),
    el('div', { class: 'actions' }, el('button', { class: 'small primary', type: 'button', text: '+ task', title: 'new task: now, later (backlog) or scheduled', onclick: () => Shell.openCreate('task') }))));
  if (typeof Dnd !== 'undefined' && typeof Dnd.laneBar === 'function') {       // lanes above the board (dnd.js): drop a Backlog card on ◆ Claude / ◇ Codex, or on a running session's chip
    if (!homeLaneBar) homeLaneBar = Dnd.laneBar({});
    homeLaneBar.ccPatch(state);
    sec.append(homeLaneBar);
  } else if (typeof Lazy !== 'undefined' && Lazy.wants('dnd')) {               // dnd.js is a lazy bundle (lazy.js): the bar's room is kept while it loads, then the board is drawn again
    sec.append(el('div', { class: 'dnd-bar-slot' }));
    if (!homeDndAsked) { homeDndAsked = true; Lazy.load('dnd').then(() => renderTasks(), () => { homeDndAsked = false; }); }
  }
  const grid = el('div', { class: 'kanban' });
  for (const [key, label] of BOARD_COLUMNS) {
    const items = list.filter(t => t.column === key);
    if (!items.length && key !== 'backlog') continue;                // Backlog stays (it is where a task starts); an empty In progress / PR / Merged column is only a heading
    const col = el('div', { class: 'col', 'data-col': key, role: 'group', 'aria-label': label }, el('h2', { text: `${label} (${items.length})` }));
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
  const c = state.claude || {};
  const cx = (state.agents && state.agents.codex) || {};
  const logins = [c.installed && !c.loggedIn ? 'Claude is not logged in on this box: runs are deferred until you Log in (headless runs use the same subscription login)' : '',
    jobs.some((j) => j.agent === 'codex') && cx.installed && !cx.loggedIn ? 'Codex is not logged in on this box: its runs are deferred until you log in (Settings)' : ''].filter(Boolean);
  sec.append(el('div', { class: 'row head' }, el('h2', { text: `Schedules (${jobs.length})` }),
    el('span', { class: 'hint', text: 'headless runs · max 2 at once · each agent pauses above 85% of its window · ' + schedWindowNotes(state, jobs).map((x) => x.text).join(' · ') }),
    ...logins.map((t) => el('span', { class: 'dim', text: t }))));
  const batches = {};
  for (const j of jobs) if (j.batch_id) (batches[j.batch_id] = batches[j.batch_id] || []).push(j);
  for (const [bid, js] of Object.entries(batches)) {
    const done = js.filter(j => j.last_status && !j.enabled).length;
    const ok = js.filter(j => j.last_status === 'ok').length;
    sec.append(el('div', { class: 'dim', text: `batch ${bid}: ${js.length} repos · ${done} finished · ${ok} ok · ${js.filter(j => j.enabled).length} waiting` }));
  }
  for (const j of jobs) {
    const jr = runs.filter(r => r.job_id === j.id).slice(0, 3);
    const row = el('div', { class: 'sess' + (j.enabled ? '' : ' muted'), 'data-job': j.id, 'data-agent': j.agent || 'claude' },
      el('div', { class: 'main' },
        agentGlyph(j.agent || 'claude'),
        el('span', { class: 'name', text: j.name }),
        el('span', { class: j.enabled && j.next_run_at ? 'state' : 'state ended', text: j.enabled && j.next_run_at ? 'next ' + fmtTs(j.next_run_at) : j.enabled ? 'parked: Run now' : 'disabled' }),
        el('span', { class: 'meta', text: `${j.project}/${j.repo} · ${j.cron ? 'cron ' + j.cron : 'one-off'} · ${j.permission_mode}${jobLimitText(j)}${j.last_status ? ' · last: ' + j.last_status : ''}` })),
      el('div', { class: 'actions' },
        jobFableButton(j),
        el('button', { type: 'button', onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/run`); } catch (e) { setError(e.message); } await poll(true); } }, ic('play'), 'Run now'),
        el('button', { type: 'button', class: 'minimal', onclick: async () => { try { await api('POST', `/api/jobs/${j.id}/toggle`); } catch (e) { setError(e.message); } await poll(true); }, text: j.enabled ? 'Disable' : 'Enable' }),
        confirmButton('job:' + j.id, 'Delete', () => api('DELETE', `/api/jobs/${j.id}`), true)));
    for (const r of jr) {
      row.append(el('div', { class: 'last', 'data-run': r.id, 'data-agent': r.agent || j.agent || 'claude' },
        el('span', { class: 'dim' }, agentGlyph(r.agent || j.agent || 'claude'), ' ', runLineText(r, j.agent)),
        r.result ? el('span', { text: r.result.slice(0, 300) }) : null,
        r.task_id ? el('span', { class: 'row' },
          el('button', { onclick: async () => { try { const x = await api('POST', `/api/runs/${r.id}/resume`); openPage(x.attach_url); } catch (e) { setError(e.message); } await poll(true); }, text: 'Resume in terminal' }),
          el('span', { class: 'dim', text: `task card: ${r.branch}` })) : null));
    }
    sec.append(row);
  }
}

