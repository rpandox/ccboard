/* ccboard task sheets (the other half of components.js and pages/home.js): the sheets a task card opens on a tap, not at the first paint. Move (taskMoveSheet: a new Claude or
   Codex session, or a running one), Edit (taskEditSheet), Preview port (taskPortSheet), and the diff and pull request sheet (openTaskModal, diffSideControl). The cards (components.js
   backlogCard / startedCard, Home's renderTasks) call these by name from a click handler; lazy.js stubs each name until the tasksheets bundle has loaded, then calls the real one.
   Classic script, definition-only: nothing here runs at load. */
'use strict';

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

/* The diff and pull request of a task card, in the sheet (v0.5.13: it was the legacy #modal): the commits and files, the diff one side at a time (a segmented control),
   then the pull request (Describe with Claude, Create PR, and for a card that has one, Merge). The diff wants room, so the sheet is wider than the forms. */
function openTaskModal(t) {
  const status = el('div', { class: 'dim form-status tm-status', role: 'status', 'aria-live': 'polite' });
  const diffBox = el('div', { class: 'diffbox' });
  const commits = el('div', { class: 'dim tm-line' });
  const files = el('div', { class: 'dim tm-line' });
  const tabs = el('div', { class: 'tm-tabs' });
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
      const dCommits = Array.isArray(diff.commits) ? diff.commits : [], dFiles = Array.isArray(diff.files) ? diff.files : [], dUnc = Array.isArray(diff.files_uncommitted) ? diff.files_uncommitted : [];   // a malformed answer reads as empty
      commits.textContent = dCommits.length ? `Commits (${dCommits.length}): ` + dCommits.slice(0, 20).join(' · ') : 'No commits on the branch yet.';
      files.textContent = (dFiles.length ? `Files: ${dFiles.join(', ')}` : '') + (dUnc.length ? `  ·  uncommitted: ${dUnc.join(', ')}` : '');
      tabs.textContent = '';
      tabs.append(diffSideControl([['committed', `Committed vs ${diff.base || 'base'}`], ['uncommitted', `Uncommitted (${dUnc.length})`]], 'committed', show));
      show('committed');
    } catch (e) { status.textContent = e.message; }
  };
  const describeBtn = el('button', { type: 'button', onclick: async () => {
    status.textContent = 'asking Claude for a title and description (claude -p, one turn)…'; describeBtn.disabled = true;
    try { const r = await api('POST', `/api/tasks/${t.id}/describe`); title.value = r.title; body.value = r.body; status.textContent = 'description ready; edit and create the PR'; }
    catch (e) { status.textContent = e.message; } finally { describeBtn.disabled = false; }
  }, text: 'Describe with Claude' });
  const prBtn = el('button', { class: 'primary', type: 'button', onclick: async () => {
    status.textContent = 'pushing and creating the PR…'; prBtn.disabled = true;
    try {
      const r = await api('POST', `/api/tasks/${t.id}/pr`, { title: title.value.trim(), body: body.value });
      status.textContent = (r.existing ? 'PR already existed: ' : 'PR created: ') + r.url; t.pr_url = r.url; t.pr_number = r.number;
      mergeRow.classList.remove('hidden');
      await poll(true); render(true);
    } catch (e) { status.textContent = e.message; } finally { prBtn.disabled = false; }
  }, text: t.pr_url ? 'Update PR (recreate)' : 'Create PR' });
  // Merge is destructive: red-outlined, two taps (confirmButton repaints the page, not a sheet, so the arming is local)
  const mergeRow = el('span', { class: 'tm-merge' + (t.pr_number ? '' : ' hidden') });
  const doMerge = async () => {
    status.textContent = 'merging…';
    const run = async (force) => api('POST', `/api/tasks/${t.id}/merge`, { method: 'squash', force });
    try { await run(false); status.textContent = 'merged and archived'; closeSheet(); await poll(true); }
    catch (e) {
      if (/uncommitted/.test(e.message) && window.confirm(e.message + '\n\nDiscard them and merge?')) { try { await run(true); closeSheet(); await poll(true); } catch (e2) { status.textContent = e2.message; } }
      else status.textContent = e.message;
    }
  };
  const paintMerge = (armed) => {
    mergeRow.textContent = '';
    if (!armed) mergeRow.append(el('button', { class: 'danger', type: 'button', title: 'Merge (squash) & archive (tap again to confirm)', onclick: () => paintMerge(true), text: 'Merge (squash) & archive' }));
    else mergeRow.append(el('button', { class: 'danger confirm', type: 'button', onclick: doMerge, text: 'Confirm merge' }), el('button', { type: 'button', onclick: () => paintMerge(false), text: 'Cancel' }));
  };
  paintMerge(false);
  const sec = (label, ...kids) => el('section', { class: 'tm-sec' }, el('h3', { class: 'tm-k', text: label }), ...kids);
  openSheet({ title: t.title, wide: true, body: [
    el('div', { class: 'tm-meta' }, el('span', { class: 'dim mono', text: `${t.project}/${t.repo} · ${t.branch}` }),
      t.pr_url ? el('a', { class: 'btn small', href: t.pr_url, target: '_blank', rel: 'noopener', text: `PR #${t.pr_number}` }) : null),
    sec('Changes', commits, files),
    sec('Diff', tabs, diffBox),
    sec('Pull request', el('div', { class: 'form tm-form' }, field('Title', title), field('Description', body),
      el('div', { class: 'tm-actions' }, describeBtn, prBtn, mergeRow)), status)] });
  load();
}

/* Two or three exclusive sides of one thing as a segmented control (one track, aria-pressed, arrow keys): the diff's committed / uncommitted switch. */
function diffSideControl(items, initial, onPick) {
  const node = el('div', { class: 'seg-ctl tm-seg', role: 'group', 'aria-label': 'Diff' });
  const btns = new Map();
  let cur = initial;
  const set = (v, focus) => {
    cur = v;
    for (const [k, b] of btns) b.setAttribute('aria-pressed', k === cur ? 'true' : 'false');
    if (focus) btns.get(v).focus();
    onPick(v);
  };
  for (const [v, text] of items) {
    btns.set(v, el('button', { class: 'seg-btn', type: 'button', 'data-side': v, 'aria-pressed': v === cur ? 'true' : 'false', text, onclick: () => set(v), onkeydown: (e) => {
      const i = items.findIndex(([x]) => x === cur);
      const to = e.key === 'ArrowRight' ? items[(i + 1) % items.length][0] : e.key === 'ArrowLeft' ? items[(i + items.length - 1) % items.length][0] : null;
      if (to === null) return;
      e.preventDefault();
      set(to, true);
    } }));
    node.append(btns.get(v));
  }
  return node;
}
