/* ccboard launcher: the session / task / schedule / batch / import forms and the launch option controls. */
'use strict';

const LAUNCH_KEY = (p, r) => `ccboard:launch:${p.name}/${r.name}`;
const TASK_KEY = (p, r) => `ccboard:task:${p.name}/${r.name}`;
const MODELS = [['', 'default (settings)'], ['fable', 'fable'], ['opus', 'opus'], ['sonnet', 'sonnet'], ['haiku', 'haiku'], ['custom', 'custom id…']];
const EFFORTS = [['', 'default'], ['low', 'low'], ['medium', 'medium'], ['high', 'high'], ['xhigh', 'xhigh'], ['max', 'max']];
const PERMS = [['', 'ask (default)'], ['acceptEdits', 'accept edits'], ['plan', 'plan'], ['auto', 'auto'], ['dontAsk', "don't ask: deny prompts"]];
const PERMS_HOST = [...PERMS, ['bypassPermissions', 'bypass: never ask (dangerous)']];
const BYPASS_WARNING = 'Claude runs every command and edit without asking, as your user, on this box. Claude Code asks you to confirm once in the terminal.';

/* Model / effort / permission controls shared by the session and task forms. Returns the grid plus a reader. */
function launchControls(saved, permOptions) {
  const model = selectEl(MODELS, saved.model_sel || '');
  const modelId = el('input', { type: 'text', placeholder: 'full model id, e.g. claude-fable-5-1', class: saved.model_sel === 'custom' ? '' : 'hidden', value: saved.model_id || '' });
  model.addEventListener('change', () => { modelId.classList.toggle('hidden', model.value !== 'custom'); if (model.value === 'custom') modelId.focus(); });
  const effort = selectEl(EFFORTS, saved.effort || '');
  const perm = selectEl(permOptions, saved.permission_mode || '');
  if (!perm.value) perm.value = '';                                       // a remembered choice that no longer exists
  const warn = el('div', { class: 'bad hidden', text: BYPASS_WARNING });
  const syncWarn = () => warn.classList.toggle('hidden', perm.value !== 'bypassPermissions');
  perm.addEventListener('change', syncWarn); syncWarn();
  const grid = el('div', {},
    el('div', { class: 'grid' },
      field('model', el('div', { class: 'field' }, model, modelId)),
      field('effort', effort),
      field('permissions', perm)),
    warn);
  return { grid, model, modelId, effort, perm,
    read: () => ({ model: model.value === 'custom' ? modelId.value.trim() : model.value, effort: effort.value, permission_mode: perm.value }),
    prefs: () => ({ model_sel: model.value, model_id: modelId.value.trim(), effort: effort.value, permission_mode: perm.value }) };
}

function sessionForm(p, r) {
  const saved = loadPrefs(LAUNCH_KEY(p, r));
  const launcher = selectEl([['claude', 'claude: new session'], ['resume', 'claude --resume'], ['continue', 'claude --continue'], ['shell', 'shell']], saved.launcher || 'claude');
  const name = el('input', { type: 'text', placeholder: 'auto: s1, s2…', maxlength: 64 });
  const lc = launchControls(saved, PERMS_HOST);
  const resumeId = el('input', { type: 'text', placeholder: 'session id to resume (blank = picker)', class: 'hidden' });
  const allowed = el('input', { type: 'text', placeholder: 'e.g. Bash(npm test), Read', value: saved.allowed_tools || '' });
  const disallowed = el('input', { type: 'text', placeholder: 'e.g. WebFetch', value: saved.disallowed_tools || '' });
  const sysPrompt = el('textarea', { placeholder: 'text appended to the system prompt (optional)' });
  sysPrompt.value = saved.append_system_prompt || '';
  const args = el('input', { type: 'text', placeholder: 'anything else, e.g. --verbose --fallback-model sonnet', value: saved.args || '' });
  const siblings = r.root ? [] : allRepos().filter(x => x.project === p.name && x.repo !== r.name);   // the project folder already contains them
  const others = allRepos().filter(x => x.project !== p.name);
  const boxes = [];
  const mk = (x, checked) => { const cb = el('input', { type: 'checkbox', value: x.id, checked }); boxes.push(cb); return el('label', {}, cb, x.id); };
  const checks = el('div', { class: 'checks' }, siblings.map(x => mk(x, true)));
  const otherBox = el('div', { class: 'checks' }, others.map(x => mk(x, false)));
  const devc = el('input', { type: 'checkbox' });
  const devRow = r.devcontainer ? el('div', { class: 'checks' },
    el('label', { title: 'devcontainer up + devcontainer exec (needs docker and the devcontainer CLI on the box; log in to Claude inside once)' }, devc, 'run in devcontainer')) : null;
  const claudeOnly = el('div', {}, lc.grid,
    el('details', {}, el('summary', { text: 'More options: tools, system prompt, extra args, other repos' }),
      el('div', { class: 'grid' },
        field('allowed tools', allowed, 'comma-separated; --allowedTools'),
        field('disallowed tools', disallowed, '--disallowedTools')),
      field('append to system prompt', sysPrompt),
      field('extra args', args),
      siblings.length ? field('also give access to (--add-dir)', checks) : null,
      others.length ? field('repos of other projects (--add-dir)', otherBox) : null));
  const sync = () => { claudeOnly.classList.toggle('hidden', launcher.value === 'shell'); resumeId.classList.toggle('hidden', launcher.value !== 'resume'); };
  launcher.addEventListener('change', sync); sync();
  const form = el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = { launcher: launcher.value, devcontainer: devc.checked };
    if (name.value.trim()) body.name = name.value.trim();
    if (launcher.value === 'resume' && resumeId.value.trim()) body.resume_id = resumeId.value.trim();
    if (launcher.value !== 'shell') {
      Object.assign(body, lc.read());
      if (allowed.value.trim()) body.allowed_tools = allowed.value.trim();
      if (disallowed.value.trim()) body.disallowed_tools = disallowed.value.trim();
      if (sysPrompt.value.trim()) body.append_system_prompt = sysPrompt.value.trim();
      if (args.value.trim()) body.args = args.value.trim();
      body.add_dirs = boxes.filter(b => b.checked).map(b => b.value);
    }
    for (const k of Object.keys(body)) if (body[k] === '' || body[k] === null) delete body[k];
    savePrefs(LAUNCH_KEY(p, r), { launcher: launcher.value, ...lc.prefs(), allowed_tools: allowed.value.trim(), disallowed_tools: disallowed.value.trim(),
      append_system_prompt: sysPrompt.value.trim(), args: args.value.trim() });
    const tab = isStandalone() ? null : window.open('', '_blank');
    try {
      const res = await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/sessions`, body);
      if (tab) tab.location = `/term/${encodeURIComponent(res.tmux)}`; else openPage(`/term/${encodeURIComponent(res.tmux)}`);
      ui.openForm = null; setError(null); await poll(true);
    } catch (err) { if (tab) tab.close(); setError(err.message); }
  } },
    el('div', { class: 'grid' }, field('launch', launcher), field('session name', name)),
    resumeId,
    claudeOnly,
    devRow,
    el('div', { class: 'submit' },
      el('button', { class: 'primary', type: 'submit' }, ic('play'), 'Start & open terminal'),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
  return form;
}

function addRepoForm(p) {
  const name = el('input', { type: 'text', placeholder: 'repo name (blank git init)', maxlength: 64 });
  const url = el('input', { type: 'text', placeholder: 'or clone URL' });
  return el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    if (name.value.trim()) body.name = name.value.trim();
    if (url.value.trim()) body.url = url.value.trim();
    try { await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos`, body); ui.openForm = null; setError(null); await poll(true); }
    catch (err) { setError(err.message); }
  } },
    el('div', { class: 'row' }, name, url),
    el('div', { class: 'row' },
      el('button', { class: 'primary', type: 'submit', text: 'Add repo' }),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
}

function taskForm(p, r) {
  const saved = loadPrefs(TASK_KEY(p, r));
  const title = el('input', { type: 'text', placeholder: 'task title (becomes the branch name)', maxlength: 120, required: true });
  const prompt = el('textarea', { placeholder: 'what Claude should do in the new worktree…', required: true });
  const issueSel = selectEl([['', 'from a GitHub issue…']]);
  let issues = [];
  issueSel.addEventListener('focus', async () => {
    if (issues.length) return;
    try {
      const res = await api('GET', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/issues`);
      issues = res.issues;
      for (const i of issues) issueSel.append(el('option', { value: String(i.number), text: `#${i.number} ${i.title}`.slice(0, 90) }));
      if (!issues.length) issueSel.append(el('option', { value: '', text: '(no open issues)' }));
    } catch (e) { issueSel.append(el('option', { value: '', text: e.message.slice(0, 80) })); }
  }, { once: true });
  issueSel.addEventListener('change', () => {
    const i = issues.find(x => String(x.number) === issueSel.value);
    if (!i) return;
    title.value = `#${i.number} ${i.title}`.slice(0, 120);
    prompt.value = `${i.title}\n\n${i.body || ''}\n\nGitHub issue: ${i.url}\nWhen done, commit with a message that includes "Closes #${i.number}".`;
  });
  const lc = launchControls(saved, PERMS);
  const args = el('input', { type: 'text', placeholder: 'extra claude args (optional)', value: saved.args || '' });
  const siblings = allRepos().filter(x => x.project === p.name && x.repo !== r.name);
  const boxes = [];
  const checks = el('div', { class: 'checks' });
  for (const x of siblings) { const cb = el('input', { type: 'checkbox', value: x.id, checked: false }); boxes.push(cb); checks.append(el('label', {}, cb, x.id)); }
  return el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = { title: title.value.trim(), prompt: prompt.value.trim(), add_dirs: boxes.filter(b => b.checked).map(b => b.value), ...lc.read() };
    if (args.value.trim()) body.args = args.value.trim();
    for (const k of Object.keys(body)) if (body[k] === '' || body[k] === null) delete body[k];
    savePrefs(TASK_KEY(p, r), { ...lc.prefs(), args: args.value.trim() });
    const tab = isStandalone() ? null : window.open('', '_blank');
    try {
      const res = await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/tasks`, body);
      if (tab) tab.location = res.attach_url; else openPage(res.attach_url);
      ui.openForm = null; setError(null); await poll(true);
    } catch (err) { if (tab) tab.close(); setError(err.message); }
  } },
    el('label', { text: 'New task: Claude works on a branch in its own worktree (claude --worktree)' }),
    issueSel, title, prompt,
    lc.grid,
    el('details', {}, el('summary', { text: 'More options: extra args, other repos' }),
      field('extra args', args),
      siblings.length ? field('also give access to (--add-dir)', checks) : null),
    el('div', { class: 'submit' },
      el('button', { class: 'primary', type: 'submit' }, ic('git-branch'), 'Start task & open terminal'),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
}

/* The schedule form's cron presets (chips that fill the cron field; a blank cron runs once now): [label, cron]. */
const CRON_PRESETS = [['nightly 02:30', '30 2 * * *'], ['weekdays 09:00', '0 9 * * 1-5'], ['hourly', '0 * * * *'], ['one-off', '']];

function jobForm(p, r) {
  const name = el('input', { type: 'text', placeholder: 'name (e.g. nightly-tests)', maxlength: 80, required: true });
  const prompt = el('textarea', { placeholder: 'prompt for the headless run (claude -p in a fresh worktree)…', required: true });
  const cron = el('input', { type: 'text', placeholder: 'cron, e.g. 30 2 * * * (blank = run once now)' });
  const go = el('button', { class: 'primary', type: 'submit', text: 'Schedule / run' });
  const syncGo = () => { go.textContent = cron.value.trim() ? 'Schedule' : 'Schedule / run'; };
  cron.addEventListener('input', syncGo);
  const presets = el('div', { class: 'chips cron-presets', role: 'group', 'aria-label': 'Cron presets' },
    CRON_PRESETS.map(([label, value]) => el('button', { class: 'chip-btn', type: 'button', text: label, title: value ? `cron ${value}` : 'blank cron: run once now',
      onclick: () => { cron.value = value; syncGo(); cron.focus(); } })));
  const mode = el('select', {}, ...['acceptEdits', 'default', 'plan', 'auto', 'dontAsk'].map(m => el('option', { value: m, text: m })));
  const turns = el('input', { type: 'number', value: '30', min: '1', max: '500', title: 'max turns' });
  const budget = el('input', { type: 'number', placeholder: 'max $ (optional)', step: '0.5', min: '0' });
  const args = el('input', { type: 'text', placeholder: 'extra claude args (optional)' });
  return el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = { name: name.value.trim(), prompt: prompt.value.trim(), permission_mode: mode.value, max_turns: parseInt(turns.value, 10) || 30, run_now: !cron.value.trim() };
    if (cron.value.trim()) body.cron = cron.value.trim();
    if (budget.value) body.max_budget_usd = parseFloat(budget.value);
    if (args.value.trim()) body.args = args.value.trim();
    try { await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/jobs`, body); ui.openForm = null; setError(null); await poll(true); }
    catch (err) { setError(err.message); }
  } },
    el('label', { text: 'Schedule a headless run: claude -p in a fresh worktree; the result becomes a task card' }),
    el('div', { class: 'row' }, name, cron),
    presets,
    prompt,
    el('div', { class: 'row' }, el('label', { text: 'permission mode' }), mode, el('label', { text: 'max turns' }), turns, budget),
    args,
    el('div', { class: 'row' },
      go,
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
}

/* ---------- project, GitHub import and batch prompt: the + menu's sheets (v0.5.5) ----------
   Each returns a <form> for openSheet() (Shell.openCreate builds the sheet around it); onDone(...) runs after the call succeeded and the
   caller closes the sheet. They moved here from pages/home.js (the v0.4 board's project form) and from the #modal versions of this file;
   the endpoints and fields are the same. A failed call keeps the form open and says why, inline and (the sheet covers the banner) as a toast. */

function formStatus(node, text, bad) {
  node.textContent = text || '';
  node.classList.toggle('bad', !!bad);
}

/* The clone queue line: queued clones and failed ones with a Clear button, rebuilt only when what it says changes (st.clone_queue). */
function cloneQueueView() {
  const node = el('div', { class: 'clone-queue dim', 'aria-live': 'polite' });
  let sig = null;
  const update = (st) => {
    const cq = st && st.clone_queue;
    if (!cq) { if (sig !== '') { sig = ''; node.textContent = ''; } return; }
    const queued = (cq.queued || []).length;
    const failed = (cq.done || []).filter((d) => d.status === 'failed');
    const next = JSON.stringify([queued, cq.cap, failed.map((f) => [f.repo, f.error])]);
    if (next === sig) return;
    sig = next;
    node.textContent = '';
    if (queued) node.append(el('span', { text: `${queued} clone${queued === 1 ? '' : 's'} queued (max ${cq.cap} at once) ` }));
    if (failed.length) {
      node.append(el('span', { class: 'bad', text: `${failed.length} failed: ${failed.map((f) => f.repo + ' (' + (f.error || '') + ')').join('; ').slice(0, 300)} ` }),
        el('button', { type: 'button', class: 'small', onclick: async () => { try { await api('POST', '/api/clone-queue/clear'); } catch (e) { setError(e.message); } await poll(true); }, text: 'Clear' }));
    }
  };
  update(typeof state !== 'undefined' ? state : null);
  return { node, update };
}

function projectForm(opts) {
  const o = opts || {};
  const name = el('input', { type: 'text', placeholder: 'project name (e.g. shop)', required: true, maxlength: 64, 'aria-label': 'project name', autocomplete: 'off', autocapitalize: 'off' });
  const url = el('input', { type: 'text', placeholder: 'optional: clone URL of the first repo', 'aria-label': 'clone URL', autocomplete: 'off', autocapitalize: 'off' });
  const status = el('div', { class: 'dim form-status' });
  const where = typeof state !== 'undefined' && state && state.config ? state.config.projects_dir : '';
  const form = el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = { name: name.value.trim() };
    if (url.value.trim()) body.url = url.value.trim();
    try {
      const res = await api('POST', '/api/projects', body);
      name.value = ''; url.value = '';
      formStatus(status, '');
      setError(null);
      if (typeof o.onDone === 'function') o.onDone(body, res);
      await poll(true);
      if (body.name && typeof navigate === 'function') navigate('#/p/' + encodeURIComponent(body.name));   // a new project has no sessions: Home keeps it under 'older', its page shows it
    } catch (err) { formStatus(status, err.message, true); setError(err.message); }
  } },
  where ? el('div', { class: 'dim', text: `A folder per project under ${where}; each repo is a subfolder and sessions run inside a repo.` }) : null,
  field('name', name), field('clone URL', url), status,
  el('div', { class: 'submit' }, el('button', { class: 'primary', type: 'submit', text: 'Create project' }),
    el('button', { type: 'button', onclick: () => { if (typeof o.onCancel === 'function') o.onCancel(); }, text: 'Cancel' })));
  form.focusFirst = () => name.focus();
  return form;
}

/* Run one headless prompt in every picked repo: POST /api/batch. */
function batchForm(opts) {
  const o = opts || {};
  const name = el('input', { type: 'text', placeholder: 'batch name (optional)', maxlength: 60, 'aria-label': 'batch name' });
  const prompt = el('textarea', { placeholder: 'prompt to run headlessly in every selected repo (claude -p, fresh worktree each)…', 'aria-label': 'prompt' });
  const mode = el('select', { 'aria-label': 'permission mode' }, ...['acceptEdits', 'default', 'plan', 'auto', 'dontAsk'].map((x) => el('option', { value: x, text: x })));
  const turns = el('input', { type: 'number', value: '30', min: '1', max: '500', 'aria-label': 'max turns' });
  const budget = el('input', { type: 'number', placeholder: 'max $ per repo (optional)', step: '0.5', min: '0', 'aria-label': 'max dollars per repo' });
  const status = el('div', { class: 'dim form-status' });
  const boxes = [];
  const list = el('div', { class: 'checks batch-repos' });
  for (const x of (typeof state !== 'undefined' && state ? allRepos() : [])) {
    const cb = el('input', { type: 'checkbox', value: x.id, checked: false });
    boxes.push(cb);
    list.append(el('label', {}, cb, x.id));
  }
  if (!boxes.length) list.append(el('span', { class: 'dim', text: 'No repos yet: create a project and add a repo first.' }));
  const go = el('button', { class: 'primary', type: 'button', onclick: async () => {
    const repos = boxes.filter((b) => b.checked).map((b) => b.getAttribute('value'));
    if (!repos.length || !prompt.value.trim()) { formStatus(status, 'pick repos and write a prompt', true); return; }
    try {
      const r = await api('POST', '/api/batch', { prompt: prompt.value.trim(), repos, name: name.value.trim() || undefined, permission_mode: mode.value,
        max_turns: parseInt(turns.value, 10) || 30, max_budget_usd: budget.value ? parseFloat(budget.value) : undefined });
      formStatus(status, `queued ${r.jobs.length} runs (batch ${r.batch_id}); ${r.started.length} started, the rest wait for a free slot`);
      if (typeof o.onDone === 'function') o.onDone(r);
      await poll(true);
    } catch (e) { formStatus(status, e.message, true); }
  }, text: 'Run on selected repos' });
  const form = el('form', { class: 'form', onsubmit: (e) => { e.preventDefault(); go.click(); } },
    el('div', { class: 'dim', text: 'Runs are headless (claude -p) in a fresh worktree per repo, at most 2 at once, paused while the 5-hour window is above 85%. Each result becomes a task card.' }),
    el('div', { class: 'grid' }, field('name', name), field('permission mode', mode), field('max turns', turns), field('max $ per repo', budget)),
    field('prompt', prompt),
    field('repos', list),
    status,
    el('div', { class: 'submit' }, go,
      el('button', { type: 'button', onclick: () => { for (const b of boxes) b.checked = !b.checked; }, text: 'Invert' }),
      el('button', { type: 'button', onclick: () => { if (typeof o.onCancel === 'function') o.onCancel(); }, text: 'Cancel' })));
  form.focusFirst = () => prompt.focus();
  return form;
}

/* Import repos from GitHub into a project: GET /api/github/repos[?owner=], then POST /api/projects/<project>/repos/bulk. */
function importForm(opts) {
  const o = opts || {};
  const owner = el('input', { type: 'text', placeholder: 'owner (blank = your repos)', maxlength: 39, 'aria-label': 'GitHub owner', autocomplete: 'off', autocapitalize: 'off' });
  const target = el('input', { type: 'text', placeholder: 'target project name', maxlength: 64, 'aria-label': 'target project', autocomplete: 'off', autocapitalize: 'off' });
  const filter = el('input', { type: 'text', placeholder: 'filter…', 'aria-label': 'filter repos' });
  const list = el('div', { class: 'checks import-repos' });
  const status = el('div', { class: 'dim form-status' });
  let repos = [];
  const boxes = [];
  const renderList = () => {
    list.textContent = '';
    boxes.length = 0;
    const f = filter.value.trim().toLowerCase();
    for (const r of repos) {
      if (f && !(r.name.toLowerCase().includes(f) || (r.description || '').toLowerCase().includes(f))) continue;
      const cb = el('input', { type: 'checkbox', value: r.url, 'data-name': r.name, checked: true });
      boxes.push(cb);
      list.append(el('label', {}, cb, el('b', { text: r.name }), el('span', { class: 'dim', text: `${r.private ? 'private' : 'public'}${r.fork ? ' · fork' : ''} · ${r.description || ''}`.slice(0, 120) })));
    }
    if (!list.childElementCount) list.append(el('span', { class: 'dim', text: repos.length ? 'no match' : 'nothing loaded yet' }));
  };
  filter.addEventListener('input', renderList);
  const load = async () => {
    formStatus(status, 'loading…');
    try {
      const r = await api('GET', `/api/github/repos${owner.value.trim() ? '?owner=' + encodeURIComponent(owner.value.trim()) : ''}`);
      repos = r.repos;
      formStatus(status, `${repos.length} repos (${r.protocol})`);
      if (!target.value.trim()) target.value = (owner.value.trim() || ((typeof state !== 'undefined' && state && state.user) || 'github').split('@')[0]).toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 64);
      renderList();
    } catch (e) { formStatus(status, e.message, true); }
  };
  const importBtn = el('button', { class: 'primary', type: 'button', onclick: async () => {
    const chosen = boxes.filter((b) => b.checked).map((b) => ({ name: b.getAttribute('data-name'), url: b.getAttribute('value') }));
    if (!chosen.length) { formStatus(status, 'nothing selected', true); return; }
    if (!target.value.trim()) { formStatus(status, 'name the target project', true); return; }
    try {
      const r = await api('POST', `/api/projects/${encodeURIComponent(target.value.trim())}/repos/bulk`, { repos: chosen });
      formStatus(status, `queued ${chosen.length} into ${r.project}`);
      if (typeof o.onDone === 'function') o.onDone(r, chosen);
      await poll(true);
    } catch (e) { formStatus(status, e.message, true); }
  }, text: 'Import selected' });
  renderList();
  const form = el('form', { class: 'form', onsubmit: (e) => { e.preventDefault(); load(); } },
    el('div', { class: 'row' }, owner, el('button', { type: 'button', onclick: load, text: 'Load' })),
    field('into project', target),
    field('filter', filter),
    status, list,
    el('div', { class: 'submit' }, importBtn,
      el('button', { type: 'button', onclick: () => { for (const b of boxes) b.checked = !b.checked; }, text: 'Invert' }),
      el('button', { type: 'button', onclick: () => { if (typeof o.onCancel === 'function') o.onCancel(); }, text: 'Cancel' })));
  form.focusFirst = () => owner.focus();
  return form;
}

/* The v0.4 entry points: the same sheets as the + menu. */
function openBatch() { return typeof Shell !== 'undefined' && Shell.openCreate ? Shell.openCreate('batch') : false; }
function openImport() { return typeof Shell !== 'undefined' && Shell.openCreate ? Shell.openCreate('import') : false; }
