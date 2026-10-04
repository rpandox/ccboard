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
  const modelId = el('input', { type: 'text', placeholder: 'full model id, e.g. claude-fable-5-1', 'aria-label': 'Custom model id', autocomplete: 'off', autocapitalize: 'off',
    class: saved.model_sel === 'custom' ? '' : 'hidden', value: saved.model_id || '' });
  model.addEventListener('change', () => { modelId.classList.toggle('hidden', model.value !== 'custom'); if (model.value === 'custom') modelId.focus(); });
  const effort = selectEl(EFFORTS, saved.effort || '');
  const perm = selectEl(permOptions, saved.permission_mode || '');
  if (!perm.value) perm.value = '';                                       // a remembered choice that no longer exists
  const warn = el('div', { class: 'bad hidden', role: 'alert', text: BYPASS_WARNING });
  const syncWarn = () => warn.classList.toggle('hidden', perm.value !== 'bypassPermissions');
  perm.addEventListener('change', syncWarn); syncWarn();
  const modelBox = el('div', { class: 'field' }, model, modelId);
  modelBox._labelFor = model;                                             // the label names the select, not the wrapper
  const grid = el('div', {},
    el('div', { class: 'grid' },
      field('Model', modelBox),
      field('Effort', effort),
      field('Permissions', perm)),
    warn);
  return { grid, model, modelId, effort, perm,
    read: () => ({ model: model.value === 'custom' ? modelId.value.trim() : model.value, effort: effort.value, permission_mode: perm.value }),
    prefs: () => ({ model_sel: model.value, model_id: modelId.value.trim(), effort: effort.value, permission_mode: perm.value }) };
}

/* A launch that failed: the message goes next to the field it is about when it names one (rules [[regex, field]]), else under the form; setError
   keeps the banner honest (and the shell toasts it while a sheet covers the banner). The sheet stays open with what was typed. */
function formFail(status, rules, msg) {
  const hit = (rules || []).find(([re]) => re.test(msg));
  formStatus(status, hit ? '' : msg, !hit);
  if (hit) fieldError(hit[1], msg, true);
  setError(msg);
}

function sessionForm(p, r) {
  const saved = loadPrefs(LAUNCH_KEY(p, r));
  const launcher = selectEl([['claude', 'claude: new session'], ['resume', 'claude --resume'], ['continue', 'claude --continue'], ['shell', 'shell']], saved.launcher || 'claude');
  const name = el('input', { type: 'text', placeholder: 'auto: s1, s2…', maxlength: 64, autocomplete: 'off', autocapitalize: 'off' });
  const lc = launchControls(saved, PERMS_HOST);
  const resumeId = el('input', { type: 'text', placeholder: 'session id to resume (blank = picker)', autocomplete: 'off', autocapitalize: 'off' });
  const allowed = el('input', { type: 'text', placeholder: 'e.g. Bash(npm test), Read', value: saved.allowed_tools || '', autocomplete: 'off', autocapitalize: 'off' });
  const disallowed = el('input', { type: 'text', placeholder: 'e.g. WebFetch', value: saved.disallowed_tools || '', autocomplete: 'off', autocapitalize: 'off' });
  const sysPrompt = el('textarea', { placeholder: 'text appended to the system prompt (optional)' });
  sysPrompt.value = saved.append_system_prompt || '';
  const args = el('input', { type: 'text', placeholder: 'anything else, e.g. --verbose --fallback-model sonnet', value: saved.args || '', autocomplete: 'off', autocapitalize: 'off' });
  const siblings = r.root ? [] : allRepos().filter(x => x.project === p.name && x.repo !== r.name);   // the project folder already contains them
  const others = allRepos().filter(x => x.project !== p.name);
  const boxes = [];
  const mk = (x, checked) => { const cb = el('input', { type: 'checkbox', value: x.id, checked }); boxes.push(cb); return el('label', {}, cb, x.id); };
  const checks = el('div', { class: 'checks' }, siblings.map(x => mk(x, true)));
  const otherBox = el('div', { class: 'checks' }, others.map(x => mk(x, false)));
  const devc = el('input', { type: 'checkbox' });
  const devRow = r.devcontainer ? el('div', { class: 'checks' },
    el('label', { title: 'devcontainer up + devcontainer exec (needs docker and the devcontainer CLI on the box; log in to Claude inside once)' }, devc, 'run in devcontainer')) : null;
  const nameField = field('Session name', name, 'Blank takes the next free one (s1, s2…).');
  const resumeField = field('Session id to resume', resumeId, 'Blank opens the picker.');
  const argsField = field('Extra args', args);
  const claudeOnly = el('div', {}, lc.grid,
    el('details', {}, el('summary', { text: 'More options: tools, system prompt, extra args, other repos' }),
      el('div', { class: 'grid' },
        field('Allowed tools', allowed, 'Comma-separated; --allowedTools.'),
        field('Disallowed tools', disallowed, '--disallowedTools.')),
      field('Append to system prompt', sysPrompt),
      argsField,
      siblings.length ? field('Also give access to', checks, '--add-dir') : null,
      others.length ? field('Repos of other projects', otherBox, '--add-dir') : null));
  const sync = () => { claudeOnly.classList.toggle('hidden', launcher.value === 'shell'); resumeField.classList.toggle('hidden', launcher.value !== 'resume'); };
  launcher.addEventListener('change', sync); sync();
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const form = el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    for (const f of [nameField, resumeField, argsField]) fieldError(f, '');
    formStatus(status, '');
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
    } catch (err) {
      if (tab) tab.close();
      formFail(status, [[/resume id/i, resumeField], [/extra args|settings overrides/i, argsField], [/session .*(exists|name)|name .*(invalid|allowed)|reserved/i, nameField]], err.message);
    }
  } },
    el('div', { class: 'grid' }, field('Launch', launcher), nameField),
    resumeField,
    claudeOnly,
    devRow,
    status,
    el('div', { class: 'submit' },
      el('button', { class: 'primary', type: 'submit' }, ic('play'), 'Start & open terminal'),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
  form.focusFirst = () => focusFine(name);
  return form;
}

function addRepoForm(p) {
  const name = el('input', { type: 'text', placeholder: 'repo name', maxlength: 64, autocomplete: 'off', autocapitalize: 'off' });
  const url = el('input', { type: 'text', placeholder: 'https://github.com/you/repo.git', autocomplete: 'off', autocapitalize: 'off' });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const nameField = field('New repo', name, 'A blank repo: git init in a new folder.');
  const urlField = field('Clone URL', url, 'Or clone an existing repo instead; the name defaults to the one in the URL.');
  const form = el('form', { class: 'form', onsubmit: async (e) => {
    e.preventDefault();
    const body = {};
    if (name.value.trim()) body.name = name.value.trim();
    if (url.value.trim()) body.url = url.value.trim();
    fieldError(nameField, ''); fieldError(urlField, ''); formStatus(status, '');
    if (!body.name && !body.url) { fieldError(nameField, 'Name a new repo, or give a URL to clone.', true); return; }
    try { await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos`, body); ui.openForm = null; setError(null); await poll(true); }
    catch (err) { formFail(status, [[/url|clone|git@|https?:/i, urlField], [/name|exists|reserved/i, nameField]], err.message); }
  } },
    nameField, urlField, status,
    el('div', { class: 'submit' },
      el('button', { class: 'primary', type: 'submit', text: 'Add repo' }),
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
  form.focusFirst = () => focusFine(name);
  return form;
}

/* ---------- the task form (v0.5.14a): one form for Now / Later / Schedule ----------
   Type a prompt and press Cmd/Ctrl+Enter: the title is the first line of the prompt unless you typed one. Run: Now starts a session in its own
   worktree and opens it, Later parks a card in the Backlog, Schedule hands the same prompt to a headless job (cron presets, or once now).
   The run mode (Now or Later only: a schedule is never remembered, and no cron is prefilled in Now or Later) and the Claude options are remembered
   per repo (ccboard:task:<project>/<repo>), the last repo per project (ccboard:task:last:<project>) and the last {project, repo} anywhere
   (ccboard:task:last: where + task opens when the page names no project, Shell.routeCtx). taskForm(p, r, opts):
   opts.when forces a mode, opts.carry {title, prompt, when, name, cron} refills the form after a repo switch, opts.targets [{p, r, label}] adds a
   "where" select that calls opts.onTarget(entry, carry), opts.onDone(res, when) runs once the call worked (the sheet closes), opts.onCancel. */

const TASK_WHEN = [['now', 'Now'], ['later', 'Later'], ['schedule', 'Schedule']];
const TASK_SUBMIT = { now: 'Start task', later: 'Add to backlog', schedule: 'Schedule' };
const TASK_BUSY = { now: 'Starting…', later: 'Adding…', schedule: 'Scheduling…' };
const TASK_LEDE = {
  now: 'Starts Claude in its own worktree and branch, then opens it.',
  later: 'Parks it in the Backlog: Start it, or send it to a running session, when you are ready.',
  schedule: 'A headless run (claude -p) in a fresh worktree, on a cron or once now; the result becomes a task card.',
};
const TASK_ICON = { now: 'play', later: 'add', schedule: 'time' };
const JOB_MODES = ['acceptEdits', 'default', 'plan', 'auto', 'dontAsk'];
const TASK_LAST_KEY = (project) => `ccboard:task:last:${project}`;       // the repo a task was last started in, per project
const TASK_LAST_ANY_KEY = 'ccboard:task:last';                          // and the {project, repo} of the last task anywhere: where '+ task' opens off a project page

/* The title a task gets when none was typed: the first line of the prompt, whitespace collapsed, cut at a word near 80 characters. */
function taskTitleFrom(prompt) {
  const line = String(prompt || '').split('\n').map((l) => l.trim()).find(Boolean) || '';
  let t = line.replace(/\s+/g, ' ');
  if (t.length > 80) { t = t.slice(0, 80); const i = t.lastIndexOf(' '); if (i > 40) t = t.slice(0, i); }
  return t.replace(/[\s.:;,]+$/, '') || 'task';
}

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

/* A segmented control: buttons with aria-pressed (one on), arrow keys move. items [[value, label]]; onChange(value) runs on a change, not at build. */
function segControl(items, initial, onChange, label) {
  const node = el('div', { class: 'seg-ctl tf-when', role: 'group', 'aria-label': label || null });
  const btns = new Map();
  let cur = items.some(([v]) => v === initial) ? initial : items[0][0];
  const paint = () => { for (const [v, b] of btns) b.setAttribute('aria-pressed', v === cur ? 'true' : 'false'); };
  const set = (v, focus) => {
    if (!btns.has(v)) return;
    const changed = v !== cur;
    cur = v;
    paint();
    if (focus) btns.get(v).focus();
    if (changed && typeof onChange === 'function') onChange(v);
  };
  for (const [v, text] of items) {
    const b = el('button', { class: 'seg-btn', type: 'button', 'aria-pressed': 'false', 'data-when': v, text, onclick: () => set(v), onkeydown: (e) => {
      const i = items.findIndex(([x]) => x === cur);
      const to = e.key === 'ArrowRight' ? items[(i + 1) % items.length][0] : e.key === 'ArrowLeft' ? items[(i + items.length - 1) % items.length][0] : null;
      if (to === null) return;
      e.preventDefault();
      set(to, true);
    } });
    btns.set(v, b);
    node.append(b);
  }
  paint();
  return { node, get value() { return cur; }, set };
}

function taskForm(p, r, opts) {
  const o = opts || {};
  const carry = o.carry || {};
  const saved = loadPrefs(TASK_KEY(p, r));
  const prefs = { ...saved };
  if (!prefs.model_sel && typeof prefs.model === 'string' && prefs.model) {        // the contract's {model} next to launchControls' own model_sel / model_id
    if (MODELS.some(([v]) => v && v !== 'custom' && v === prefs.model)) prefs.model_sel = prefs.model;
    else { prefs.model_sel = 'custom'; prefs.model_id = prefs.model; }
  }
  // What was remembered is only now | later: a schedule is a recurring job, and opening the next task in Schedule with its cron filled in
  // would make Cmd+Enter create one by accident. Schedule is a choice made on the form (carry/opts: the "in" switch, or a Schedule entry).
  const savedWhen = saved.when === 'now' || saved.when === 'later' ? saved.when : '';
  const whenWant = carry.when || o.when || savedWhen;
  const when0 = TASK_WHEN.some(([v]) => v === whenWant) ? whenWant : 'now';

  const promptEl = el('textarea', { class: 'composer task-prompt', rows: '3', autocomplete: 'off', spellcheck: 'true',
    placeholder: 'What should Claude do?' });
  promptEl._maxRows = 12;
  promptEl.value = carry.prompt || '';
  const title = el('input', { type: 'text', maxlength: 120, placeholder: 'e.g. Fix the login redirect', autocomplete: 'off', value: carry.title || '' });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });

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
    const i = issues.find((x) => String(x.number) === issueSel.value);
    if (!i) return;
    title.value = `#${i.number} ${i.title}`.slice(0, 120);
    promptEl.value = `${i.title}\n\n${i.body || ''}\n\nGitHub issue: ${i.url}\nWhen done, commit with a message that includes "Closes #${i.number}".`;
    composerGrow(promptEl);
  });

  const lc = launchControls(prefs, PERMS);
  const args = el('input', { type: 'text', placeholder: 'extra claude args (optional)', autocomplete: 'off', autocapitalize: 'off', value: prefs.args || '' });
  const siblings = allRepos().filter((x) => x.project === p.name && x.repo !== r.name);
  const boxes = [];
  const checks = el('div', { class: 'checks' });
  for (const x of siblings) { const cb = el('input', { type: 'checkbox', value: x.id, checked: false }); boxes.push(cb); checks.append(el('label', {}, cb, x.id)); }
  const jobMode = selectEl(JOB_MODES.map((m) => [m, m]), JOB_MODES.includes(saved.job_mode) ? saved.job_mode : 'acceptEdits');
  const turns = el('input', { type: 'number', value: String(saved.max_turns || 30), min: '1', max: '500', inputmode: 'numeric' });
  const budget = el('input', { type: 'number', placeholder: 'optional', step: '0.5', min: '0', inputmode: 'decimal' });

  /* the Schedule half: a name (from the title), a cron with presets (blank = run once now) */
  const nameEl = el('input', { type: 'text', placeholder: 'name (e.g. nightly-tests)', maxlength: 80, autocomplete: 'off', autocapitalize: 'off', value: carry.name || '' });
  let nameTyped = !!carry.name;
  nameEl.addEventListener('input', () => { nameTyped = true; });
  const cron = el('input', { type: 'text', placeholder: 'cron: 30 2 * * *  (blank = once, now)', autocomplete: 'off', autocapitalize: 'off', value: carry.cron !== undefined ? carry.cron : (when0 === 'schedule' ? (saved.cron || '') : '') });
  const cronNote = el('div', { class: 'dim tf-cronnote' });
  const presetBtns = CRON_PRESETS.map(([label, value]) => el('button', { class: 'chip-btn', type: 'button', text: label, 'aria-pressed': 'false', 'data-cron': value,
    title: value ? `cron ${value}` : 'blank cron: run once, now', onclick: () => { cron.value = value; syncCron(); if (!promptEl.value.trim()) focusFine(promptEl); } }));
  const presets = el('div', { class: 'chips cron-presets', role: 'group', 'aria-label': 'Cron presets' }, presetBtns);
  const syncCron = () => {
    const v = cron.value.trim();
    for (const b of presetBtns) b.setAttribute('aria-pressed', b.getAttribute('data-cron') === v ? 'true' : 'false');
    cronNote.textContent = cronNoteText(v);
  };
  cron.addEventListener('input', syncCron);

  let seg = null;
  const upperOnly = [];                                                       // shown for Now and Later
  const lowerOnly = [];                                                       // shown for Schedule
  const lede = el('p', { class: 'dim tf-lede' });
  const goText = el('span', { class: 'tf-go-text' });
  const goIcon = el('span', { class: 'tf-go-ic' });
  const go = el('button', { class: 'primary', type: 'submit', title: 'Cmd/Ctrl+Enter' }, goIcon, goText);
  const optNote = el('span', { class: 'dim tf-optnote' });
  const titleField = field('Title', title, 'Optional: the first line of the prompt is used.');
  const promptField = field('Prompt', promptEl, coarsePointer() ? null : 'Enter adds a line · Cmd/Ctrl+Enter submits.');
  const argsField = field('Extra args', args);
  const optNoteSync = () => {
    const v = lc.read();
    const bits = [v.model, v.effort, v.permission_mode].filter(Boolean);
    optNote.textContent = bits.length ? ' · ' + bits.join(' · ') : '';
  };
  for (const c of [lc.model, lc.effort, lc.perm]) c.addEventListener('change', optNoteSync);
  optNoteSync();

  /* the schedule's name follows the title, else the first line of the prompt, until it is typed by hand */
  const autoName = () => {
    if (nameTyped) return;
    nameEl.value = title.value.trim() || (promptEl.value.trim() ? taskTitleFrom(promptEl.value) : '');
  };

  const syncMode = () => {
    const w = seg ? seg.value : when0;
    for (const n of upperOnly) n.classList.toggle('hidden', w === 'schedule');
    for (const n of lowerOnly) n.classList.toggle('hidden', w !== 'schedule');
    lede.textContent = TASK_LEDE[w];
    goText.textContent = TASK_SUBMIT[w];
    goIcon.textContent = '';
    goIcon.append(ic(TASK_ICON[w]));
    if (w === 'schedule') { autoName(); syncCron(); }
  };
  seg = segControl(TASK_WHEN, when0, syncMode, 'When');

  const issueField = field('From a GitHub issue', issueSel);
  const lcBox = el('div', {}, lc.grid);
  const sibField = siblings.length ? field('Also give access to', checks, '--add-dir') : null;
  const jobOpts = el('div', { class: 'grid' }, field('Permission mode', jobMode), field('Max turns', turns), field('Max $', budget, 'Optional.'));
  const nameField = field('Name', nameEl, 'Shown on the task card.');
  const schedBox = el('div', { class: 'tf-schedule' }, nameField, field('Cron', cron), presets, cronNote);
  upperOnly.push(titleField, issueField, lcBox);
  if (sibField) upperOnly.push(sibField);
  lowerOnly.push(schedBox, jobOpts);
  const options = el('details', { class: 'tf-options' }, el('summary', {}, 'Options', optNote), issueField, lcBox, jobOpts, argsField, sibField);

  const targets = Array.isArray(o.targets) ? o.targets : [];
  const here = Math.max(0, targets.findIndex((x) => x.p === p && x.r === r));
  const whereSel = targets.length > 1 ? selectEl(targets.map((x, i) => [String(i), x.label]), String(here)) : null;
  if (whereSel) whereSel.addEventListener('change', () => {
    const x = targets[parseInt(whereSel.value, 10)];
    if (x && typeof o.onTarget === 'function') o.onTarget(x, { title: title.value, prompt: promptEl.value, when: seg.value, name: nameEl.value, cron: cron.value });
  });

  let busy = false;
  const setBusy = (on) => {
    busy = on;
    go.disabled = on;
    goText.textContent = on ? TASK_BUSY[seg.value] : TASK_SUBMIT[seg.value];
  };

  const remember = (when) => {
    const v = lc.read();
    savePrefs(TASK_KEY(p, r), { ...lc.prefs(), model: v.model, effort: v.effort, permission_mode: v.permission_mode, args: args.value.trim(),
      when: when === 'schedule' ? saved.when : when,                                  // a schedule run never becomes the next task's mode
      cron: cron.value.trim(), job_mode: jobMode.value, max_turns: parseInt(turns.value, 10) || 30 });
    taskSaveLastRepo(p.name, r.name);
  };

  const finish = (res, when) => {
    setError(null);
    if (typeof o.onDone === 'function') o.onDone(res, when); else ui.openForm = null;
    if (typeof poll === 'function') poll(true);
  };

  const submitTask = async (when, prompt, titleText) => {
    const body = { project: p.name, repo: r.name, title: titleText, prompt, when, agent: 'claude', add_dirs: boxes.filter((b) => b.checked).map((b) => b.value), ...lc.read() };
    if (args.value.trim()) body.args = args.value.trim();
    for (const k of Object.keys(body)) if (body[k] === '' || body[k] === null) delete body[k];
    remember(when);
    const res = (await api('POST', '/api/tasks', body)) || {};
    const demo = typeof demoOn === 'function' && demoOn();
    const base = { id: res.id !== undefined && res.id !== null ? res.id : -Date.now(), project: p.name, repo: r.name, slug: res.slug || '', title: titleText, branch: '', base: '', worktree: '',
      tmux: '', created_at: new Date().toISOString(), column: 'backlog', phase: 'backlog', mode: 'worktree', agent: 'claude', auto_close: false, parent_id: null, chain_id: null,
      session_row: null, session: null, result: null, prompt: prompt.slice(0, 600), prompt_len: prompt.length, claude_session_id: null, pr_url: null, pr_number: null, pr_state: null,
      cost_usd: null, overlap: [], ci: null, pr: null };
    if (when === 'now') {
      const row = taskRowFromResponse(base, res, { mode: 'worktree' });
      if (!row.tmux && demo) row.tmux = `${p.name}--${r.name}--t-${row.slug || 'task'}`;
      taskOverrideSet(row, null, { _new: true });
      toast(`started ${row.slug || titleText}`, { kind: 'ok' });
      taskRepaint();
      finish(res, when);
      taskOpenPeek(row.tmux);
    } else {
      const row = { ...base, ...(res.task && typeof res.task === 'object' ? res.task : {}), phase: 'backlog', column: 'backlog', tmux: '' };
      taskOverrideSet(row, null, { _new: true });
      toast(`added to backlog: ${titleText}`.slice(0, 120), { kind: 'ok' });
      taskRepaint();
      finish(res, when);
    }
  };

  const submitJob = async (prompt, titleText) => {
    const name = (nameEl.value.trim() || titleText).slice(0, 80);
    const c = cron.value.trim();
    const body = { name, prompt, permission_mode: jobMode.value, max_turns: parseInt(turns.value, 10) || 30, run_now: !c };
    if (c) body.cron = c;
    if (budget.value) body.max_budget_usd = parseFloat(budget.value);
    if (args.value.trim()) body.args = args.value.trim();
    remember('schedule');
    const res = await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/jobs`, body);
    toast(c ? `scheduled ${name}` : `running ${name} once`, { kind: 'ok' });
    finish(res, 'schedule');
  };

  const submit = async () => {
    if (busy) return;
    const when = seg.value;
    const prompt = promptEl.value.trim();
    fieldError(promptField, ''); fieldError(argsField, '');
    if (!prompt) { fieldError(promptField, 'Write what Claude should do.', true); return; }
    const titleText = title.value.trim() || taskTitleFrom(prompt);
    if (/bypassPermissions|dangerously-skip-permissions/i.test(args.value)) {                // the server refuses it as well: say so before the round trip
      options.setAttribute('open', '');                                                       // the field sits under Options: show it before pointing at it
      fieldError(argsField, 'bypassPermissions is not allowed for tasks or schedules; start a session and choose bypass there if you really want it.', true);
      return;
    }
    setBusy(true);
    formStatus(status, '');
    try {
      if (when === 'schedule') await submitJob(prompt, titleText); else await submitTask(when, prompt, titleText);
    } catch (err) { formStatus(status, err.message, true); setError(err.message); }
    setBusy(false);
  };

  const keys = (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.isComposing) { e.preventDefault(); submit(); } };
  promptEl.addEventListener('input', () => { composerGrow(promptEl); fieldError(promptField, ''); if (seg.value === 'schedule') autoName(); });
  promptEl.addEventListener('keydown', keys);

  const form = el('form', { class: 'form task-form', onsubmit: (e) => { e.preventDefault(); submit(); } },
    whereSel ? field('Repo', whereSel) : null,
    field('When', seg.node),
    lede,
    promptField,
    titleField,
    schedBox,
    options,
    status,
    el('div', { class: 'submit' }, go,
      el('button', { type: 'button', onclick: () => { if (typeof o.onCancel === 'function') o.onCancel(); else { ui.openForm = null; if (typeof renderProjects === 'function') renderProjects(); } }, text: 'Cancel' })));
  form.addEventListener('keydown', keys);
  seg.node.addEventListener('click', (e) => { if (e.detail !== 0) focusFine(promptEl); });         // a click on a mode goes on to typing on a desktop (a phone keeps its keyboard down until the box is tapped); the keyboard keeps its focus on the control
  form.focusFirst = () => focusFine(promptEl);
  syncMode();
  if (carry.prompt) composerGrow(promptEl);
  return form;
}

/* The schedule form's cron presets (chips that fill the cron field; a blank cron runs once now): [label, cron]. */
const CRON_PRESETS = [['nightly 02:30', '30 2 * * *'], ['weekdays 09:00', '0 9 * * 1-5'], ['hourly', '0 * * * *'], ['one-off', '']];

const JOB_KEY = (p, r) => `ccboard:job:${p.name}/${r.name}`;               // the schedule form's own memory per repo: cron, mode, turns, budget
const BATCH_KEY = 'ccboard:batch';                                        // the batch form's: mode, turns, budget (it spans repos, so one key)

/* The line under a cron field: what it will do, or what is off about it. */
function cronNoteText(v) {
  return !v ? 'Runs once, right now.' : v.split(/\s+/).length === 5 ? `Runs on cron ${v}.` : 'A cron has 5 fields, e.g. 30 2 * * *.';
}

function jobForm(p, r) {
  const saved = loadPrefs(JOB_KEY(p, r));
  const name = el('input', { type: 'text', placeholder: 'e.g. nightly-tests', maxlength: 80, required: true, autocomplete: 'off', autocapitalize: 'off' });
  const prompt = el('textarea', { placeholder: 'prompt for the headless run (claude -p in a fresh worktree)…', required: true });
  const cron = el('input', { type: 'text', placeholder: 'cron: 30 2 * * *  (blank = run once now)', autocomplete: 'off', autocapitalize: 'off', value: typeof saved.cron === 'string' ? saved.cron : '' });
  cron.value = typeof saved.cron === 'string' ? saved.cron : '';          // the property too: syncGo reads it before the attribute is reflected anywhere
  const cronNote = el('div', { class: 'dim tf-cronnote' });
  const go = el('button', { class: 'primary', type: 'submit', text: 'Schedule / run' });
  const presetBtns = CRON_PRESETS.map(([label, value]) => el('button', { class: 'chip-btn', type: 'button', text: label, 'aria-pressed': 'false', 'data-cron': value,
    title: value ? `cron ${value}` : 'blank cron: run once now', onclick: () => { cron.value = value; syncGo(); focusFine(cron); } }));
  const presets = el('div', { class: 'chips cron-presets', role: 'group', 'aria-label': 'Cron presets' }, presetBtns);
  const syncGo = () => {
    const v = cron.value.trim();
    go.textContent = v ? 'Schedule' : 'Schedule / run';
    cronNote.textContent = cronNoteText(v);
    for (const b of presetBtns) b.setAttribute('aria-pressed', b.getAttribute('data-cron') === v ? 'true' : 'false');
  };
  cron.addEventListener('input', syncGo);
  syncGo();
  const mode = selectEl(JOB_MODES.map((m) => [m, m]), JOB_MODES.includes(saved.mode) ? saved.mode : 'acceptEdits');
  const turns = el('input', { type: 'number', value: String(saved.turns || 30), min: '1', max: '500', inputmode: 'numeric' });
  const budget = el('input', { type: 'number', placeholder: 'optional', step: '0.5', min: '0', inputmode: 'decimal', value: saved.budget || '' });
  const args = el('input', { type: 'text', placeholder: 'extra claude args (optional)', autocomplete: 'off', autocapitalize: 'off' });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const nameField = field('Name', name, 'Shown on the task card and in Schedules.');
  const promptField = field('Prompt', prompt);
  const cronField = field('Cron', cron);
  const argsField = field('Extra args', args);
  name.addEventListener('input', () => fieldError(nameField, ''));
  prompt.addEventListener('input', () => fieldError(promptField, ''));
  const form = el('form', { class: 'form task-form job-form', novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    for (const f of [nameField, promptField, cronField, argsField]) fieldError(f, '');
    formStatus(status, '');
    if (!name.value.trim()) { fieldError(nameField, 'Give the schedule a name.', true); return; }
    if (!prompt.value.trim()) { fieldError(promptField, 'Write the prompt for the run.', true); return; }
    const body = { name: name.value.trim(), prompt: prompt.value.trim(), permission_mode: mode.value, max_turns: parseInt(turns.value, 10) || 30, run_now: !cron.value.trim() };
    if (cron.value.trim()) body.cron = cron.value.trim();
    if (budget.value) body.max_budget_usd = parseFloat(budget.value);
    if (args.value.trim()) body.args = args.value.trim();
    savePrefs(JOB_KEY(p, r), { cron: cron.value.trim(), mode: mode.value, turns: parseInt(turns.value, 10) || 30, budget: budget.value });
    try { await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/jobs`, body); ui.openForm = null; setError(null); await poll(true); }
    catch (err) { formFail(status, [[/cron/i, cronField], [/name/i, nameField], [/extra args|args/i, argsField]], err.message); }
  } },
    el('p', { class: 'dim tf-lede', text: 'A headless run (claude -p) in a fresh worktree; the result becomes a task card.' }),
    nameField,
    promptField,
    cronField,
    presets,
    cronNote,
    el('details', { class: 'tf-options' }, el('summary', { text: 'Advanced' }),
      el('div', { class: 'grid' }, field('Permission mode', mode), field('Max turns', turns), field('Max $', budget, 'Optional.')),
      argsField),
    status,
    el('div', { class: 'submit' },
      go,
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
  form.focusFirst = () => focusFine(name);
  return form;
}

/* ---------- project, GitHub import and batch prompt: the + menu's sheets (v0.5.5) ----------
   Each returns a <form> for openSheet() (Shell.openCreate builds the sheet around it); onDone(...) runs after the call succeeded and the
   caller closes the sheet. They moved here from pages/home.js (the v0.4 board's project form) and from the #modal versions of this file;
   the endpoints and fields are the same. A failed call keeps the form open and says why, inline and (the sheet covers the banner) as a toast. */

function formStatus(node, text, bad) {
  node.textContent = text || '';
  node.classList.toggle('bad', !!bad);
  node.setAttribute('role', bad ? 'alert' : 'status');
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

/* The header of a list in a form (repos to pick): a title, an optional count, and the list's own tools (Load, Invert) as quiet small buttons,
   so the footer carries the primary and Cancel and nothing else. */
function listHead(title, ...tools) {
  return el('div', { class: 'row list-head' }, el('span', { class: 'dim list-title', text: title }), ...tools);
}

function projectForm(opts) {
  const o = opts || {};
  const name = el('input', { type: 'text', placeholder: 'e.g. shop', required: true, maxlength: 64, autocomplete: 'off', autocapitalize: 'off' });
  const url = el('input', { type: 'text', placeholder: 'https://github.com/you/repo.git', autocomplete: 'off', autocapitalize: 'off' });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const where = typeof state !== 'undefined' && state && state.config ? state.config.projects_dir : '';
  const nameField = field('Name', name, 'A folder in the projects directory: letters, digits, - and _ (start and end with a letter or digit).');
  const urlField = field('Clone URL', url, 'Optional: the first repo, cloned into the project.');
  name.addEventListener('input', () => fieldError(nameField, ''));
  const form = el('form', { class: 'form', novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    fieldError(nameField, '');
    const body = { name: name.value.trim() };
    if (!body.name) { fieldError(nameField, 'Name the project.', true); return; }
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
  nameField, urlField, status,
  el('div', { class: 'submit' }, el('button', { class: 'primary', type: 'submit', text: 'Create project' }),
    el('button', { type: 'button', onclick: () => { if (typeof o.onCancel === 'function') o.onCancel(); }, text: 'Cancel' })));
  form.focusFirst = () => focusFine(name);
  return form;
}

/* Run one headless prompt in every picked repo: POST /api/batch. */
function batchForm(opts) {
  const o = opts || {};
  const saved = loadPrefs(BATCH_KEY);
  const name = el('input', { type: 'text', placeholder: 'optional', maxlength: 60, autocomplete: 'off', autocapitalize: 'off' });
  const prompt = el('textarea', { placeholder: 'prompt to run headlessly in every selected repo (claude -p, fresh worktree each)…' });
  const mode = selectEl(JOB_MODES.map((x) => [x, x]), JOB_MODES.includes(saved.mode) ? saved.mode : 'acceptEdits');
  const turns = el('input', { type: 'number', value: String(saved.turns || 30), min: '1', max: '500', inputmode: 'numeric' });
  const budget = el('input', { type: 'number', placeholder: 'optional', step: '0.5', min: '0', inputmode: 'decimal', value: saved.budget || '' });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const boxes = [];
  const list = el('div', { class: 'checks batch-repos' });
  for (const x of (typeof state !== 'undefined' && state ? allRepos() : [])) {
    const cb = el('input', { type: 'checkbox', value: x.id, checked: false });
    boxes.push(cb);
    list.append(el('label', {}, cb, x.id));
  }
  if (!boxes.length) list.append(el('span', { class: 'dim', text: 'No repos yet: create a project and add a repo first.' }));
  const invert = el('button', { class: 'small', type: 'button', onclick: () => { for (const b of boxes) b.checked = !b.checked; }, text: 'Invert' });
  const promptField = field('Prompt', prompt);
  const reposBox = el('div', {}, listHead('Tick the repos to run in', invert), list);
  const reposField = field('Repos', reposBox);
  prompt.addEventListener('input', () => fieldError(promptField, ''));
  list.addEventListener('change', () => fieldError(reposField, ''));
  const go = el('button', { class: 'primary', type: 'button', onclick: async () => {
    const repos = boxes.filter((b) => b.checked).map((b) => b.getAttribute('value'));
    fieldError(promptField, ''); fieldError(reposField, ''); formStatus(status, '');
    if (!prompt.value.trim()) { fieldError(promptField, 'Write the prompt to run.', true); if (!repos.length) fieldError(reposField, 'Pick at least one repo.'); return; }
    if (!repos.length) { fieldError(reposField, 'Pick at least one repo.', true); return; }
    savePrefs(BATCH_KEY, { mode: mode.value, turns: parseInt(turns.value, 10) || 30, budget: budget.value });
    try {
      const r = await api('POST', '/api/batch', { prompt: prompt.value.trim(), repos, name: name.value.trim() || undefined, permission_mode: mode.value,
        max_turns: parseInt(turns.value, 10) || 30, max_budget_usd: budget.value ? parseFloat(budget.value) : undefined });
      formStatus(status, `queued ${r.jobs.length} runs (batch ${r.batch_id}); ${r.started.length} started, the rest wait for a free slot`);
      if (typeof o.onDone === 'function') o.onDone(r);
      await poll(true);
    } catch (e) { formStatus(status, e.message, true); }
  }, text: 'Run on selected repos' });
  const form = el('form', { class: 'form', novalidate: true, onsubmit: (e) => { e.preventDefault(); go.click(); } },
    el('div', { class: 'dim', text: 'Runs are headless (claude -p) in a fresh worktree per repo, at most 2 at once, paused while the 5-hour window is above 85%. Each result becomes a task card.' }),
    promptField,
    reposField,
    el('details', { class: 'tf-options' }, el('summary', { text: 'Options' }),
      el('div', { class: 'grid' }, field('Name', name, 'Optional label for the batch.'), field('Permission mode', mode), field('Max turns', turns), field('Max $ per repo', budget, 'Optional.'))),
    status,
    el('div', { class: 'submit' }, go,
      el('button', { type: 'button', onclick: () => { if (typeof o.onCancel === 'function') o.onCancel(); }, text: 'Cancel' })));
  form.focusFirst = () => focusFine(prompt);
  return form;
}

/* Import repos from GitHub into a project: GET /api/github/repos[?owner=], then POST /api/projects/<project>/repos/bulk. */
function importForm(opts) {
  const o = opts || {};
  const owner = el('input', { type: 'text', placeholder: 'blank = your repos', maxlength: 39, autocomplete: 'off', autocapitalize: 'off' });
  const target = el('input', { type: 'text', placeholder: 'target project name', maxlength: 64, autocomplete: 'off', autocapitalize: 'off' });
  const filter = el('input', { type: 'text', placeholder: 'filter by name or description…', 'aria-label': 'Filter repos', autocomplete: 'off', autocapitalize: 'off' });
  const list = el('div', { class: 'checks import-repos' });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
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
    if (!list.childElementCount) list.append(el('span', { class: 'dim', text: repos.length ? 'no match' : 'nothing loaded yet: press Load' }));
  };
  filter.addEventListener('input', renderList);
  filter.addEventListener('keydown', (e) => { if (e.key === 'Enter') e.preventDefault(); });          // the filter is live: Enter must not re-fetch the list from GitHub
  const ownerField = field('GitHub owner', owner, 'Blank lists your own repos. Enter loads them.');
  const targetField = field('Into project', target, 'The project the repos are cloned into.');
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
  const loadBtn = el('button', { class: 'small', type: 'button', onclick: load, text: 'Load' });
  const invert = el('button', { class: 'small', type: 'button', onclick: () => { for (const b of boxes) b.checked = !b.checked; }, text: 'Invert' });
  const reposField = field('Repos', el('div', {}, listHead('Tick the repos to import', loadBtn, invert), filter, list));
  target.addEventListener('input', () => fieldError(targetField, ''));
  list.addEventListener('change', () => fieldError(reposField, ''));
  const importBtn = el('button', { class: 'primary', type: 'button', onclick: async () => {
    const chosen = boxes.filter((b) => b.checked).map((b) => ({ name: b.getAttribute('data-name'), url: b.getAttribute('value') }));
    fieldError(targetField, ''); fieldError(reposField, '');
    if (!chosen.length) { fieldError(reposField, 'Nothing selected: press Load, then tick the repos to import.', true); return; }
    if (!target.value.trim()) { fieldError(targetField, 'Name the target project.', true); return; }
    try {
      const r = await api('POST', `/api/projects/${encodeURIComponent(target.value.trim())}/repos/bulk`, { repos: chosen });
      formStatus(status, `queued ${chosen.length} into ${r.project}`);
      if (typeof o.onDone === 'function') o.onDone(r, chosen);
      await poll(true);
    } catch (e) { formStatus(status, e.message, true); }
  }, text: 'Import selected' });
  renderList();
  const form = el('form', { class: 'form', novalidate: true, onsubmit: (e) => { e.preventDefault(); load(); } },
    ownerField,
    targetField,
    reposField,
    status,
    el('div', { class: 'submit' }, importBtn,
      el('button', { type: 'button', onclick: () => { if (typeof o.onCancel === 'function') o.onCancel(); }, text: 'Cancel' })));
  form.focusFirst = () => focusFine(owner);
  return form;
}

/* The v0.4 entry points: the same sheets as the + menu. */
function openBatch() { return typeof Shell !== 'undefined' && Shell.openCreate ? Shell.openCreate('batch') : false; }
function openImport() { return typeof Shell !== 'undefined' && Shell.openCreate ? Shell.openCreate('import') : false; }
