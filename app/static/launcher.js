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
   "where" select that calls opts.onTarget(entry, carry), opts.onDone(res, when) runs once the call worked (the sheet closes), opts.onCancel.
   v0.5.15: 'Then…' adds steps that run one after another (a chain: POST /api/projects/{p}/repos/{r}/chains), and Options holds the switch that closes the
   session when the task finishes (kept per repo; only an OFF switch is sent, as auto_close false: on is the server's default for a new session). */

const TASK_WHEN = [['now', 'Now'], ['later', 'Later'], ['schedule', 'Schedule']];
const TASK_SUBMIT = { now: 'Start task', later: 'Add to backlog', schedule: 'Schedule' };
const TASK_BUSY = { now: 'Starting…', later: 'Adding…', schedule: 'Scheduling…' };
const TASK_LEDE = {
  now: 'Starts Claude in its own worktree and branch, then opens it.',
  later: 'Parks it in the Backlog: Start it, or send it to a running session, when you are ready.',
  schedule: 'A headless run (claude -p) in a fresh worktree, on a cron or once now; the result becomes a task card.',
};
const TASK_LEDE_CODEX_SCHEDULE = 'A headless run (codex exec) in its own worktree, on a cron or once now; the result becomes a task card.';
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

const TASK_AGENTS = [['claude', '◆ Claude'], ['codex', '◇ Codex']];

/* The 'Then…' builder of the task form (v0.5.15): steps that start one after another, each in a session of its own once the one before has finished. A step is
   a prompt (and an optional title) and the agent that runs it. chainBuilder() -> {node, read(), problem(), count()}: node is a <details> that adds its first
   step when it opens; read() the filled steps [{title, prompt, agent}] (a blank step is not a step); problem() says so next to a step that has a title and no
   prompt and answers true. The first step of a chain is the form's own prompt. */
function chainBuilder() {
  const list = el('div', { class: 'tf-steps' });
  const count = el('span', { class: 'dim tf-optnote' });
  const rows = [];
  const sync = () => {
    rows.forEach((r, i) => { r.head.textContent = `Step ${i + 2}`; });
    count.textContent = rows.length ? ` · ${rows.length + 1} steps` : '';
  };
  const addRow = () => {
    const title = el('input', { type: 'text', maxlength: 120, placeholder: 'e.g. Review the change', autocomplete: 'off' });
    const prompt = el('textarea', { class: 'composer task-prompt', rows: '3', autocomplete: 'off', spellcheck: 'true', 'aria-label': 'What this step does',
      placeholder: 'What should this step do?' });
    const agent = segControl(TASK_AGENTS, 'claude', (v) => { if (v === 'codex' && !taskAgentInstalled('codex')) agent.set('claude'); }, 'Agent for this step');   // an arrow key must not land on the disabled Codex
    const cx = agent.node.querySelector('[data-when=codex]');
    if (cx && !taskAgentInstalled('codex')) { cx.setAttribute('disabled', ''); cx.setAttribute('title', 'Codex is not installed on this box'); }
    const head = el('span', { class: 'tf-step-k' });
    const promptField = field('Prompt', prompt);
    prompt.addEventListener('input', () => { composerGrow(prompt); fieldError(promptField, ''); });
    const row = { head, title, prompt, agent, promptField, node: null };
    row.node = el('div', { class: 'tf-step' },
      el('div', { class: 'tf-step-head' }, head,
        el('button', { class: 'icon minimal tf-step-rm', type: 'button', 'aria-label': 'Remove this step', title: 'Remove this step', onclick: () => { rows.splice(rows.indexOf(row), 1); row.node.remove(); sync(); } }, ic('cross'))),
      promptField, field('Title', title, 'Optional: the first line of the prompt is used.'), field('Agent', agent.node));
    rows.push(row);
    list.append(row.node);
    sync();
    return row;
  };
  const addBtn = el('button', { class: 'minimal small tf-step-add', type: 'button', text: '+ Add a step', onclick: () => { const r = addRow(); focusFine(r.prompt); } });
  const node = el('details', { class: 'tf-then' }, el('summary', {}, 'Then…', count),
    el('p', { class: 'dim tf-lede', text: 'Each step starts in a new session once the one before has finished. Write {{result}} in a prompt where the previous result should go; without it the result is added under the prompt.' }),
    list, addBtn);
  node.addEventListener('toggle', () => { if (node.open && !rows.length) { const r = addRow(); focusFine(r.prompt); } });
  return {
    node,
    read: () => rows.map((r) => ({ title: r.title.value.trim(), prompt: r.prompt.value.trim(), agent: r.agent.value })).filter((s) => s.title || s.prompt),
    problem: () => {
      const r = rows.find((x) => x.title.value.trim() && !x.prompt.value.trim());
      if (r) fieldError(r.promptField, 'Write what this step should do.', true);
      return !!r;
    },
    count: () => rows.length,
  };
}

/* ---------- from a GitHub issue (v0.5.20) ----------
   An issue of this repository carries a "## Who should do it" block; GET .../issues/{n} parses it (app/issues.py: bypass spellings are already dropped there, an author who is not the owner of
   `origin` gets an empty block). launcherIssueChoices(who, {agents, installed}) turns that block into what the launcher may preselect, checked against the installed agents' schemas
   (launcherSchema): {agent, claude: {model, effort, permission_mode} | null, codex: {model, reasoning, cx_mode, sandbox, approval} | null, parts: [words for the note], skipped: [what is not
   offered here]}. Nothing unknown, nothing dangerous and nothing for an agent that is not installed gets through; a value the schema does not list is skipped and the person's own default stays. */
function lxKnownModel(agent, m) {
  if (agent === 'claude') return LX_CLAUDE_CHIPS.includes(m) || LX_CLAUDE_MORE.includes(m) || launcherSchema('claude').models.includes(m);
  return launcherSchema('codex').models.includes(m);
}

function launcherIssueChoices(who, o) {
  const opt = o || {};
  const out = { agent: '', claude: null, codex: null, parts: {}, skipped: [] };
  if (!who || typeof who !== 'object') return out;
  const usable = (a) => (!Array.isArray(opt.agents) || opt.agents.includes(a)) && (typeof opt.installed !== 'function' || opt.installed(a));
  const c = who.claude && typeof who.claude === 'object' ? who.claude : null;
  if (c && usable('claude')) {
    const sch = launcherSchema('claude');
    const pick = {};
    const parts = [];
    if (c.model) { if (lxKnownModel('claude', c.model)) { pick.model = c.model; parts.push(c.model); } else out.skipped.push(`model ${c.model}`); }
    if (c.effort) { if (sch.efforts.includes(c.effort) && c.effort !== 'ultracode') { pick.effort = c.effort; parts.push(c.effort); } else out.skipped.push(`effort ${c.effort}`); }
    if (c.permission_mode) {
      const m = c.permission_mode;
      if (m === 'default' || m === 'manual') { pick.permission_mode = ''; parts.push('ask'); }
      else if (m !== 'bypassPermissions' && sch.permission_modes.includes(m)) { pick.permission_mode = m; parts.push(m); }
      else out.skipped.push(`permissions ${m}`);
    }
    if (Object.keys(pick).length) { out.claude = pick; out.parts.claude = parts; }
  }
  const x = who.codex && typeof who.codex === 'object' ? who.codex : null;
  if (x && usable('codex')) {
    const sch = launcherSchema('codex');
    const pick = {};
    const parts = [];
    if (x.model) { if (lxKnownModel('codex', x.model)) { pick.model = x.model; parts.push(x.model); } else out.skipped.push(`model ${x.model}`); }
    if (x.reasoning) {
      if (launcherReasoning(sch, pick.model || '').allowed.includes(x.reasoning)) { pick.reasoning = x.reasoning; parts.push(x.reasoning); } else out.skipped.push(`reasoning ${x.reasoning}`);
    }
    if (x.sandbox || x.approval) {
      const sb = x.sandbox || 'workspace-write';
      const ap = x.approval || 'on-request';
      const apv = sch.options.find((q) => q.key === 'approval');
      const approvals = (apv && apv.choices) || LX_APPROVALS;
      if (sb === 'workspace-write' && ap === 'on-request') { pick.cx_mode = 'default'; parts.push('default mode'); }
      else if (sb === 'read-only' && ap === 'on-request') { pick.cx_mode = 'read-only'; parts.push('read-only'); }
      else if (LX_SANDBOXES.includes(sb) && sb !== 'danger-full-access' && approvals.includes(ap)) { Object.assign(pick, { cx_mode: 'custom', sandbox: sb, approval: ap }); parts.push(`${sb}, ${ap}`); }
      else out.skipped.push(`sandbox ${sb} / approval ${ap}`);
    }
    if (Object.keys(pick).length) { out.codex = pick; out.parts.codex = parts; }
  }
  const want = who.default_agent === 'codex' ? ['codex', 'claude'] : ['claude', 'codex'];
  out.agent = want.find((a) => out[a]) || '';
  return out;
}

/* The picker both task forms share: the select (the open issues of the repo, loaded on focus), the quiet "#N by login · open on GitHub" line, the note "from the issue: opus, high,
   acceptEdits" with Reset to my defaults, and the callout + tick for an issue by someone who is not the owner of origin. Picking never starts anything and never grabs the focus.
   hooks: fill(issue) puts the title and the prompt in; apply(choices) preselects (and remembers what it replaced); restore() puts that back; changed() re-checks the Start button;
   scope {agents, installed} limits launcherIssueChoices. -> {node, select, link(), blocked(), applied(), clear()}. */
function launcherIssuePicker(p, r, hooks) {
  const base = `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/issues`;
  const sel = selectEl([['', 'from a GitHub issue…']]);
  const hint = (msg) => (/\bgh\b|auth|login|not found|command/i.test(msg) ? `${msg} (is gh installed and logged in? run gh auth login on the box)` : msg);
  let issues = [];
  let cur = null;
  let applied = false;
  let seq = 0;
  const hide = (n, on) => n.classList.toggle('hidden', !!on);
  const meta = el('p', { class: 'dim lx-hint lx-issue-meta hidden' });
  const noteText = el('span', { class: 'lx-issue-note-t' });
  const resetBtn = el('button', { class: 'minimal small lx-issue-reset hidden', type: 'button', text: 'Reset to my defaults', onclick: () => reset() });
  const note = el('p', { class: 'dim lx-hint lx-issue-note hidden' }, noteText, ' ', resetBtn);
  const ack = el('input', { type: 'checkbox' });
  const callText = el('span', { class: 'lx-danger-t' });
  const callout = el('div', { class: 'lx-danger lx-issue-warn hidden', role: 'alert' }, ic('warning-sign'),
    el('div', { class: 'lx-danger-b' }, callText, el('div', { class: 'checks' }, el('label', {}, ack, 'I have read it'))));
  ack.addEventListener('change', () => hooks.changed());
  const node = el('div', { class: 'lx-issue' }, sel, meta, note, callout);

  const setNote = (text, withReset) => {
    noteText.textContent = text || '';
    hide(resetBtn, !withReset);
    hide(note, !text);
  };
  const paintMeta = () => {
    meta.textContent = '';
    hide(meta, !cur);
    hide(callout, !cur || cur.trusted);
    if (!cur) return;
    meta.append(`#${cur.number}${cur.author ? ' by ' + cur.author : ''}`);
    if (/^https:\/\//.test(String(cur.url || ''))) meta.append(' · ', el('a', { href: cur.url, target: '_blank', rel: 'noopener noreferrer', text: 'open on GitHub' }));
    callText.textContent = `Issue by ${cur.author || 'an unknown author'}: its text becomes the agent's prompt`;
  };
  const unapply = () => { if (applied) { applied = false; hooks.restore(); } };
  function reset() {
    unapply();
    setNote('Back to your defaults.', false);
    hooks.changed();
  }
  const clear = () => {
    seq++;
    unapply();
    cur = null;
    ack.checked = false;
    setNote('', false);
    paintMeta();
    hooks.changed();
  };

  sel.addEventListener('focus', async () => {
    if (issues.length) return;
    try {
      const res = await api('GET', base);
      issues = (res && res.issues) || [];
      for (const i of issues) sel.append(el('option', { value: String(i.number), text: `#${i.number} ${i.title}`.slice(0, 90) }));
      if (!issues.length) sel.append(el('option', { value: '', text: '(no open issues)' }));
    } catch (e) { sel.append(el('option', { value: '', text: hint(String((e && e.message) || e)).slice(0, 120) })); }
  }, { once: true });

  sel.addEventListener('change', async () => {
    const i = issues.find((x) => String(x.number) === sel.value);
    if (!i) { clear(); return; }
    const my = ++seq;
    unapply();
    ack.checked = false;
    cur = { number: i.number, url: i.url, author: i.author || '', trusted: i.trusted === true };
    setNote('', false);
    paintMeta();
    hooks.fill(i);
    hooks.changed();
    if (!cur.trusted) { setNote("Model and permissions are not taken from someone else's issue.", false); return; }
    try {
      const d = await api('GET', `${base}/${encodeURIComponent(i.number)}`);
      if (my !== seq) return;
      if (!d || d.trusted !== true) { cur.trusted = false; paintMeta(); hooks.changed(); setNote("Model and permissions are not taken from someone else's issue.", false); return; }
      const ch = launcherIssueChoices(d.who, hooks.scope);
      const warns = (d.who && Array.isArray(d.who.warnings) ? d.who.warnings : []).join('; ');
      const tail = (ch.skipped.length ? `; not offered here: ${ch.skipped.join(', ')}, your default stays` : '') + (warns ? `; ${warns}` : '');
      if (!ch.agent) { setNote(ch.skipped.length || warns ? `The issue's model line was not used${tail}.` : 'This issue has no model line; your defaults stay.', false); return; }
      hooks.apply(ch);
      applied = true;
      setNote(`from the issue: ${ch.parts[ch.agent].join(', ')}${tail}`, true);
      hooks.changed();
    } catch (e) {
      if (my !== seq) return;
      setNote(`Could not read the issue's model line: ${hint(String((e && e.message) || e))}. Your defaults stay.`, false);
    }
  });

  return { node, select: sel, link: () => (cur ? { issue_number: cur.number, issue_url: cur.url } : {}),
    blocked: () => !!cur && !cur.trusted && !ack.checked, applied: () => applied, clear };
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

  let oldSnap = null;                                                                 // what the issue's model line replaced, for Reset to my defaults
  const setSel = (sel, v) => { sel.value = v; try { sel.dispatchEvent(new Event('change')); } catch (_) { /* no Event constructor */ } };
  const issueKit = launcherIssuePicker(p, r, {
    scope: { agents: ['claude'], installed: lxInstalled },
    fill: (i) => {
      title.value = `#${i.number} ${i.title}`.slice(0, 120);
      promptEl.value = `${i.title}\n\n${i.body || ''}\n\nGitHub issue: ${i.url}\nWhen done, commit with a message that includes "Closes #${i.number}".`;
      composerGrow(promptEl);
    },
    apply: (ch) => {
      oldSnap = lc.prefs();
      const c = ch.claude || {};
      if (c.model) {
        if (MODELS.some(([v]) => v === c.model && v !== 'custom')) setSel(lc.model, c.model);
        else { setSel(lc.model, 'custom'); lc.modelId.value = c.model; lc.modelId.classList.remove('hidden'); }
      }
      if (c.effort) setSel(lc.effort, c.effort);
      if ('permission_mode' in c) setSel(lc.perm, c.permission_mode);
      optNoteSync();
    },
    restore: () => {
      if (!oldSnap) return;
      lc.model.value = oldSnap.model_sel; lc.modelId.value = oldSnap.model_id; lc.modelId.classList.toggle('hidden', lc.model.value !== 'custom');
      lc.effort.value = oldSnap.effort; lc.perm.value = oldSnap.permission_mode;
      oldSnap = null;
      optNoteSync();
    },
    changed: () => { go.disabled = busy || issueKit.blocked(); },
  });
  const issueSel = issueKit.select;

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
  const autoClose = el('input', { type: 'checkbox' });
  autoClose.checked = prefs.auto_close !== false;                                // on unless the person turned it off for this repo
  const autoField = field('When it finishes', el('div', { class: 'checks' }, el('label', {}, autoClose, 'Close the session')),
    'The session closes itself about a minute after the task stops, unless it asked you something. Keep it open from the card.');
  const chain = chainBuilder();
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

  issueKit.node._labelFor = issueSel;
  const issueField = field('From a GitHub issue', issueKit.node);
  const lcBox = el('div', {}, lc.grid);
  const sibField = siblings.length ? field('Also give access to', checks, '--add-dir') : null;
  const jobOpts = el('div', { class: 'grid' }, field('Permission mode', jobMode), field('Max turns', turns), field('Max $', budget, 'Optional.'));
  const nameField = field('Name', nameEl, 'Shown on the task card.');
  const schedBox = el('div', { class: 'tf-schedule' }, nameField, field('Cron', cron), presets, cronNote);
  upperOnly.push(titleField, issueField, lcBox, autoField, chain.node);
  if (sibField) upperOnly.push(sibField);
  lowerOnly.push(schedBox, jobOpts);
  const options = el('details', { class: 'tf-options' }, el('summary', {}, 'Options', optNote), issueField, lcBox, jobOpts, argsField, autoField, sibField);

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
    go.disabled = on || issueKit.blocked();
    goText.textContent = on ? TASK_BUSY[seg.value] : TASK_SUBMIT[seg.value];
  };

  const remember = (when) => {
    const mine = issueKit.applied() && oldSnap;                                      // a model line read from an issue is never remembered as the person's default
    const v = mine ? { model: oldSnap.model_sel === 'custom' ? oldSnap.model_id : oldSnap.model_sel, effort: oldSnap.effort, permission_mode: oldSnap.permission_mode } : lc.read();
    savePrefs(TASK_KEY(p, r), { ...(mine ? oldSnap : lc.prefs()), model: v.model, effort: v.effort, permission_mode: v.permission_mode, args: args.value.trim(),
      when: when === 'schedule' ? saved.when : when,                                  // a schedule run never becomes the next task's mode
      auto_close: autoClose.checked,
      cron: cron.value.trim(), job_mode: jobMode.value, max_turns: parseInt(turns.value, 10) || 30 });
    taskSaveLastRepo(p.name, r.name);
  };

  const finish = (res, when) => {
    setError(null);
    if (typeof o.onDone === 'function') o.onDone(res, when); else ui.openForm = null;
    if (typeof poll === 'function') poll(true);
  };

  const submitTask = async (when, prompt, titleText) => {
    const body = { project: p.name, repo: r.name, title: titleText, prompt, when, agent: 'claude', add_dirs: boxes.filter((b) => b.checked).map((b) => b.value), ...lc.read(), ...issueKit.link() };
    if (args.value.trim()) body.args = args.value.trim();
    if (!autoClose.checked) body.auto_close = false;
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

  /* A chain: this form's prompt is step 1 (its launch options travel with it), the 'Then…' steps follow. dispatch true starts step 1 now; later steps wait
     queued behind their parent. The answer {chain_id, ids[]} paints the cards at once (step 1 running or in the Backlog, the others queued). */
  const submitChain = async (when, prompt, titleText, more) => {
    const lcv = lc.read();
    const first = {};
    for (const [k, v] of Object.entries({ ...lcv, args: args.value.trim(), add_dirs: boxes.filter((b) => b.checked).map((b) => b.value) })) if (v && !(Array.isArray(v) && !v.length)) first[k] = v;
    const steps = [{ title: titleText, prompt, agent: 'claude', ...first },                     // flat, as POST /chains reads them (model, effort, permission_mode, args, add_dirs)
      ...more.map((s) => ({ title: s.title || taskTitleFrom(s.prompt), prompt: s.prompt, agent: s.agent }))];
    const body = { steps, dispatch: when === 'now' };
    if (!autoClose.checked) body.auto_close = false;
    remember(when);
    const res = (await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/chains`, body)) || {};
    const demo = typeof demoOn === 'function' && demoOn();
    const chainId = res.chain_id !== undefined && res.chain_id !== null ? res.chain_id : (demo ? `demo-${Date.now()}` : null);
    const ids = Array.isArray(res.ids) && res.ids.length === steps.length ? res.ids : (demo ? steps.map((_, i) => -(Date.now() + i)) : null);
    let opened = '';
    const started = res.started && typeof res.started === 'object' ? res.started : res;          // POST /chains answers {chain_id, ids, tasks, started: step 1's dispatch}
    const given = Array.isArray(res.tasks) && res.tasks.length === steps.length ? res.tasks : [];     // the server's own rows when it sends them
    if (ids) {
      steps.forEach((s, i) => {
        const base = { id: ids[i], project: p.name, repo: r.name, slug: '', title: s.title, branch: '', base: '', worktree: '', tmux: '', created_at: new Date().toISOString(), column: 'backlog',
          phase: i ? 'queued' : 'backlog', mode: 'worktree', agent: s.agent, auto_close: autoClose.checked, parent_id: i ? ids[i - 1] : null, chain_id: chainId, session_row: null, session: null, result: null,
          prompt: s.prompt.slice(0, 600), prompt_len: s.prompt.length, claude_session_id: null, pr_url: null, pr_number: null, pr_state: null, cost_usd: null, overlap: [], ci: null, pr: null,
          ...(given[i] && typeof given[i] === 'object' ? given[i] : {}) };
        if (i === 0 && when === 'now') {
          const row = taskRowFromResponse(base, { tmux: started.tmux, session_row: started.session_row, slug: started.slug, branch: started.branch, task: started.task }, { mode: 'worktree' });
          if (!row.tmux && demo) row.tmux = `${p.name}--${r.name}--t-chain-${Math.abs(ids[0])}`;
          opened = row.tmux || '';
          taskOverrideSet(row, null, { _new: true });
        } else taskOverrideSet(base, null, { _new: true });
      });
    }
    toast(when === 'now' ? `started a chain of ${steps.length} steps` : `added a chain of ${steps.length} steps to the backlog`, { kind: 'ok' });
    taskWarn(res);                                                                                  // a hand start inside the limit window goes ahead and says so
    taskRepaint();
    finish(res, when);
    if (opened) taskOpenPeek(opened);
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
    if (issueKit.blocked()) { options.setAttribute('open', ''); fieldError(issueField, 'Tick I have read it to start from this issue.', true); return; }
    fieldError(issueField, '');
    const titleText = title.value.trim() || taskTitleFrom(prompt);
    if (/bypassPermissions|dangerously-skip-permissions/i.test(args.value)) {                // the server refuses it as well: say so before the round trip
      options.setAttribute('open', '');                                                       // the field sits under Options: show it before pointing at it
      fieldError(argsField, 'bypassPermissions is not allowed for tasks or schedules; start a session and choose bypass there if you really want it.', true);
      return;
    }
    const steps = when === 'schedule' ? [] : chain.read();
    if (when !== 'schedule' && chain.problem()) { chain.node.setAttribute('open', ''); return; }              // a step with a title and no prompt: said next to it
    setBusy(true);
    formStatus(status, '');
    try {
      if (when === 'schedule') await submitJob(prompt, titleText);
      else if (steps.length) await submitChain(when, prompt, titleText, steps);
      else await submitTask(when, prompt, titleText);
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
    chain.node,
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

/* ---------- the launcher in dispatch mode (v0.5.15): where a Backlog card goes, and how ----------
   Opened by Options… in the Move sheet. Where: a new session (which agent, its launch options) or a running one that is ready; the switch that closes the
   session when the task finishes (on for a new session, off for a running one, until it is touched); one POST /api/tasks/{id}/dispatch through taskStart or
   taskSend, so the card moves at once and the usual toasts, conflicts and limit warnings apply. preset {agent, session, auto_close} fills it in. */
function taskDispatchSheet(t, preset) {
  const pre = preset || {};
  const saved = loadPrefs(TASK_KEY({ name: t.project }, { name: t.repo }));
  const ready = taskSessionTargets(t).filter((x) => x.ok);
  const own = t.agent || 'claude';
  const agent0 = pre.agent === 'claude' || (pre.agent === 'codex' && taskAgentInstalled('codex')) ? pre.agent : own;
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const lc = launchControls({ ...saved }, PERMS);
  const effort = selectEl([['', 'default'], ['low', 'low'], ['medium', 'medium'], ['high', 'high']], '');
  const sess = selectEl(ready.length ? ready.map((x) => [x.s.tmux, `${x.s.name} · ${x.repo === 'root' ? 'project folder' : x.repo}${x.same ? '' : ' (other repo)'}`]) : [['', '(no session is ready)']], pre.session || '');
  const auto = el('input', { type: 'checkbox' });
  let touched = false;
  auto.addEventListener('change', () => { touched = true; });
  const go = el('button', { class: 'primary', type: 'submit' });
  const claudeBox = el('div', {}, lc.grid);
  const codexBox = el('div', {}, field('Reasoning effort', effort));
  const laneBox = el('div', {});
  const sessBox = el('div', {}, field('Session', sess, ready.length ? 'The prompt is pasted into it; a session in another repo asks first.' : 'Every session of this project is busy or waiting on a prompt.'));
  const where = segControl([['lane', 'New session'], ['session', 'Running session']], pre.session ? 'session' : 'lane', () => sync(), 'Where');
  const agent = segControl(TASK_AGENTS, agent0, () => sync(), 'Agent');
  const cx = agent.node.querySelector('[data-when=codex]');
  if (cx && !taskAgentInstalled('codex')) { cx.setAttribute('disabled', ''); cx.setAttribute('title', 'Codex is not installed on this box'); }
  laneBox.append(field('Agent', agent.node), claudeBox, codexBox);
  function sync() {
    if (agent.value === 'codex' && !taskAgentInstalled('codex')) { agent.set('claude'); return; }             // an arrow key must not land on the disabled Codex
    const lane = where.value === 'lane';
    laneBox.classList.toggle('hidden', !lane);
    sessBox.classList.toggle('hidden', lane);
    claudeBox.classList.toggle('hidden', agent.value !== 'claude');
    codexBox.classList.toggle('hidden', agent.value !== 'codex');
    if (!touched) auto.checked = typeof pre.auto_close === 'boolean' ? pre.auto_close : lane;
    go.disabled = !lane && !ready.length;
    go.textContent = lane ? `Start in ${AGENT_NAME[agent.value] || 'a new session'}` : 'Send to the session';
  }
  const submit = () => {
    if (where.value === 'session') {
      const x = ready.find((r) => r.s.tmux === sess.value);
      if (!x) { formStatus(status, 'Pick a session that is ready.', true); return; }
      closeSheet();
      taskSend(t, x, false, auto.checked ? { auto_close: true } : undefined);
      return;
    }
    const extra = {};
    if (agent.value === 'claude') { const v = lc.read(); for (const k of ['model', 'effort', 'permission_mode']) if (v[k]) extra[k] = v[k]; }
    else if (effort.value) extra.reasoning_effort = effort.value;
    closeSheet();
    taskStart(t, { agent: agent.value, auto_close: auto.checked ? undefined : false, extra });
  };
  const form = el('form', { class: 'form task-form tk-dispatch', onsubmit: (e) => { e.preventDefault(); submit(); } },
    field('Where', where.node), laneBox, sessBox,
    field('When it finishes', el('div', { class: 'checks' }, el('label', {}, auto, 'Close the session')), 'The session closes itself about a minute after the task stops, unless it asked you something.'),
    status,
    el('div', { class: 'submit' }, go, el('button', { type: 'button', onclick: () => closeSheet(), text: 'Cancel' })));
  sync();
  openSheet({ title: `Start “${String(t.title).slice(0, 60)}”`, body: form });
}

/* The schedule form's cron presets (chips that fill the cron field; a blank cron runs once now): [label, cron]. */
const CRON_PRESETS = [['nightly 02:30', '30 2 * * *'], ['weekdays 09:00', '0 9 * * 1-5'], ['hourly', '0 * * * *'], ['one-off', '']];

const JOB_KEY = (p, r) => `ccboard:job:${p.name}/${r.name}`;               // the schedule form's own memory per repo: cron, mode, turns, budget
const BATCH_KEY = 'ccboard:batch';                                        // the batch form's: mode, turns, budget (it spans repos, so one key)

/* The line under a cron field: what it will do, or what is off about it. */
function cronNoteText(v) {
  return !v ? 'Runs once, right now.' : v.split(/\s+/).length === 5 ? `Runs on cron ${v}.` : 'A cron has 5 fields, e.g. 30 2 * * *.';
}

/* The agent control of the schedule and batch forms (v0.5.16): a segmented Claude | Codex (Codex off, with the reason said as text, when it is not installed on this box) and, for
   Codex, its model and reasoning: the same choices and the same prefs the launcher sheet keeps per repo (ccboard:task:<project>/<repo>:codex). A Claude job keeps its model in the
   extra args, as it always did. `want` is the agent the form was last used with. Returns {agent, field, box, modelField, reasoningField, set(a), body(), remember()}. */
function jobAgentPick(p, r, want, onChange, seed) {
  const key = p && r ? LX_TASK_KEY(p.name, r.name, 'codex') : null;
  const kept = (key && lxJson(lxGet(key))) || (seed && typeof seed === 'object' ? seed : {});
  const S = { agent: want === 'codex' && lxInstalled('codex') ? 'codex' : 'claude', model: String(kept.model || ''), reasoning: String(kept.reasoning_effort || kept.reasoning || '') };
  const seg = lxSeg([['claude', `${AGENT_GLYPH.claude} Claude`], ['codex', `${AGENT_GLYPH.codex} Codex`]], (a) => set(a), 'Agent', 'lx-agent');
  const why = el('p', { class: 'dim lx-hint lx-why' });
  const modelSel = selectEl([['', 'default (Codex)']], '');
  const reasoningHost = el('div', { class: 'lx-effhost' });
  const modelField = field('Model', modelSel, 'Optional: blank uses the model Codex is set to.');
  const reasoningField = field('Reasoning', reasoningHost);
  const box = el('div', { class: 'lx-agentbox hidden', 'data-agent': 'codex' }, modelField, reasoningField);
  const paintReasoning = () => {
    const rs = launcherReasoning(launcherSchema('codex'), S.model);
    if (S.reasoning && !rs.allowed.includes(S.reasoning)) S.reasoning = '';
    const sg = lxSeg([['', 'default'], ...rs.all.map((l) => [l, l])], (l) => { S.reasoning = l; sg.set(l); }, 'Reasoning', 'lx-effort');
    for (const l of rs.all) sg.disable(l, !rs.allowed.includes(l), `${S.model || 'this model'} has no ${l} reasoning level`);
    sg.set(S.reasoning || '');
    reasoningHost.textContent = '';
    reasoningHost.append(sg.node);
  };
  const paintModels = () => {
    const models = launcherSchema('codex').models;
    modelSel.textContent = '';
    for (const [v, t] of [['', 'default (Codex)'], ...(S.model && !models.includes(S.model) ? [[S.model, S.model]] : []), ...models.map((m) => [m, m])]) modelSel.append(el('option', { value: v, text: t }));
    modelSel.value = S.model;
  };
  modelSel.addEventListener('change', () => { S.model = modelSel.value; paintReasoning(); });
  const paint = () => {
    const ok = lxInstalled('codex');
    seg.disable('codex', !ok, ok ? '' : 'Codex is not installed on this box');
    why.textContent = ok ? '' : 'Codex is not installed on this box';
    seg.set(S.agent);
    box.classList.toggle('hidden', S.agent !== 'codex');
    if (S.agent === 'codex') { paintModels(); paintReasoning(); }
  };
  function set(a) {
    if (a === S.agent || (a === 'codex' && !lxInstalled('codex'))) return;
    S.agent = a;
    paint();
    if (typeof onChange === 'function') onChange(a);
  }
  paint();
  return {
    get agent() { return S.agent; },
    get model() { return S.model; },
    get reasoning() { return S.reasoning; },
    field: field('Agent', el('div', { class: 'lx-agentctl' }, seg.node, why)),
    box, modelField, reasoningField, set, repaint: paint,
    body() { return S.agent === 'codex' ? { agent: 'codex', ...(S.model ? { model: S.model } : {}), ...(S.reasoning ? { reasoning_effort: S.reasoning } : {}) } : {}; },
    remember() {
      if (S.agent !== 'codex' || !key) return;
      const next = { ...kept };
      for (const [k, v] of [['model', S.model], ['reasoning_effort', S.reasoning]]) { if (v) next[k] = v; else delete next[k]; }
      delete next.reasoning;
      lxPut(key, JSON.stringify(next));
    },
  };
}

function jobForm(p, r) {
  const saved = loadPrefs(JOB_KEY(p, r));
  const ag = jobAgentPick(p, r, saved.agent, () => paintAgent());
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
  const lede = el('p', { class: 'dim tf-lede' });
  const nameField = field('Name', name, 'Shown on the task card and in Schedules.');
  const promptField = field('Prompt', prompt);
  const cronField = field('Cron', cron);
  const argsField = field('Extra args', args);
  const turnsField = field('Max turns', turns);
  const budgetField = field('Max $', budget, 'Optional.');
  const paintAgent = () => {
    const codex = ag.agent === 'codex';
    lede.textContent = codex ? 'A headless run (codex exec) in its own worktree; the result becomes a task card. Codex has no turn or budget limit: a run ends when the task does.'
      : 'A headless run (claude -p) in a fresh worktree; the result becomes a task card.';
    prompt.setAttribute('placeholder', codex ? 'prompt for the headless run (codex exec in its own worktree)…' : 'prompt for the headless run (claude -p in a fresh worktree)…');
    args.setAttribute('placeholder', codex ? 'extra codex args (optional)' : 'extra claude args (optional)');
    turnsField.classList.toggle('hidden', codex);
    budgetField.classList.toggle('hidden', codex);
  };
  name.addEventListener('input', () => fieldError(nameField, ''));
  prompt.addEventListener('input', () => fieldError(promptField, ''));
  const form = el('form', { class: 'form task-form job-form', novalidate: true, onsubmit: async (e) => {
    e.preventDefault();
    for (const f of [nameField, promptField, cronField, argsField, ag.modelField, ag.reasoningField]) fieldError(f, '');
    formStatus(status, '');
    if (!name.value.trim()) { fieldError(nameField, 'Give the schedule a name.', true); return; }
    if (!prompt.value.trim()) { fieldError(promptField, 'Write the prompt for the run.', true); return; }
    const codex = ag.agent === 'codex';
    const body = { name: name.value.trim(), prompt: prompt.value.trim(), permission_mode: mode.value, run_now: !cron.value.trim(), ...ag.body() };
    if (!codex) body.max_turns = parseInt(turns.value, 10) || 30;
    if (cron.value.trim()) body.cron = cron.value.trim();
    if (!codex && budget.value) body.max_budget_usd = parseFloat(budget.value);
    if (args.value.trim()) body.args = args.value.trim();
    savePrefs(JOB_KEY(p, r), { cron: cron.value.trim(), mode: mode.value, turns: parseInt(turns.value, 10) || 30, budget: budget.value, ...(codex ? { agent: 'codex' } : {}) });
    ag.remember();
    try { await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/jobs`, body); ui.openForm = null; setError(null); await poll(true); }
    catch (err) { formFail(status, [[/cron/i, cronField], [/name/i, nameField], [/extra args|args|argument/i, argsField], [/reasoning/i, ag.reasoningField], [/model/i, ag.modelField]], err.message); }
  } },
    lede,
    ag.field,
    ag.box,
    nameField,
    promptField,
    cronField,
    presets,
    cronNote,
    el('details', { class: 'tf-options' }, el('summary', { text: 'Advanced' }),
      el('div', { class: 'grid' }, field('Permission mode', mode), turnsField, budgetField),
      argsField),
    status,
    el('div', { class: 'submit' },
      go,
      el('button', { type: 'button', onclick: () => { ui.openForm = null; renderProjects(); }, text: 'Cancel' })));
  form.focusFirst = () => focusFine(name);
  paintAgent();
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

/* The header of a list in a form (repos to pick): a title, an optional count, and the list's own tools (Load, Invert) as quiet small buttons,
   so the footer carries the primary and Cancel and nothing else. */
function listHead(title, ...tools) {
  return el('div', { class: 'row list-head' }, el('span', { class: 'dim list-title', text: title }), ...tools);
}

/* Run one headless prompt in every picked repo: POST /api/batch, with Claude or Codex (v0.5.16: the agent picker; every job of the batch runs with it). */
function batchForm(opts) {
  const o = opts || {};
  const saved = loadPrefs(BATCH_KEY);
  const ag = jobAgentPick(null, null, saved.agent, () => paintAgent(), { model: saved.cx_model, reasoning_effort: saved.cx_reasoning });
  const name = el('input', { type: 'text', placeholder: 'optional', maxlength: 60, autocomplete: 'off', autocapitalize: 'off' });
  const prompt = el('textarea', { placeholder: 'prompt to run headlessly in every selected repo (claude -p, fresh worktree each)…' });
  const mode = selectEl(JOB_MODES.map((x) => [x, x]), JOB_MODES.includes(saved.mode) ? saved.mode : 'acceptEdits');
  const turns = el('input', { type: 'number', value: String(saved.turns || 30), min: '1', max: '500', inputmode: 'numeric' });
  const budget = el('input', { type: 'number', placeholder: 'optional', step: '0.5', min: '0', inputmode: 'decimal', value: saved.budget || '' });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const lede = el('div', { class: 'dim' });
  const turnsField = field('Max turns', turns);
  const budgetField = field('Max $ per repo', budget, 'Optional.');
  const paintAgent = () => {
    const codex = ag.agent === 'codex';
    lede.textContent = codex ? 'Runs are headless (codex exec) in its own worktree per repo, at most 2 at once, paused while the Codex usage window is above 85%. Each result becomes a task card.'
      : 'Runs are headless (claude -p) in a fresh worktree per repo, at most 2 at once, paused while the 5-hour window is above 85%. Each result becomes a task card.';
    prompt.setAttribute('placeholder', codex ? 'prompt to run headlessly in every selected repo (codex exec, its own worktree each)…' : 'prompt to run headlessly in every selected repo (claude -p, fresh worktree each)…');
    turnsField.classList.toggle('hidden', codex);
    budgetField.classList.toggle('hidden', codex);
  };
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
    fieldError(promptField, ''); fieldError(reposField, ''); fieldError(ag.modelField, ''); fieldError(ag.reasoningField, ''); formStatus(status, '');
    if (!prompt.value.trim()) { fieldError(promptField, 'Write the prompt to run.', true); if (!repos.length) fieldError(reposField, 'Pick at least one repo.'); return; }
    if (!repos.length) { fieldError(reposField, 'Pick at least one repo.', true); return; }
    const codex = ag.agent === 'codex';
    savePrefs(BATCH_KEY, { mode: mode.value, turns: parseInt(turns.value, 10) || 30, budget: budget.value, ...(codex ? { agent: 'codex', cx_model: ag.model, cx_reasoning: ag.reasoning } : {}) });
    try {
      const body = { prompt: prompt.value.trim(), repos, name: name.value.trim() || undefined, permission_mode: mode.value, ...ag.body() };
      if (!codex) { body.max_turns = parseInt(turns.value, 10) || 30; body.max_budget_usd = budget.value ? parseFloat(budget.value) : undefined; }
      const r = await api('POST', '/api/batch', body);
      formStatus(status, `queued ${r.jobs.length} runs (batch ${r.batch_id}); ${r.started.length} started, the rest wait for a free slot`);
      if (typeof o.onDone === 'function') o.onDone(r);
      await poll(true);
    } catch (e) { formStatus(status, e.message, true); }
  }, text: 'Run on selected repos' });
  const form = el('form', { class: 'form batch-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); go.click(); } },
    lede,
    ag.field,
    ag.box,
    promptField,
    reposField,
    el('details', { class: 'tf-options' }, el('summary', { text: 'Options' }),
      el('div', { class: 'grid' }, field('Name', name, 'Optional label for the batch.'), field('Permission mode', mode), turnsField, budgetField)),
    status,
    el('div', { class: 'submit' }, go,
      el('button', { type: 'button', onclick: () => { if (typeof o.onCancel === 'function') o.onCancel(); }, text: 'Cancel' })));
  form.focusFirst = () => focusFine(prompt);
  paintAgent();
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

/* ---------- launcher v2 (v0.5.13): openLauncher, ONE sheet for starting a session, a task or a dispatch ----------
   openLauncher({project, repo, agent?, task?, mode: 'session' | 'task' | 'dispatch', ...}) opens it in the sheet (components.js openSheet: a right panel from 840 px up, a
   bottom sheet below). project / repo are the state objects or their names ('root' is the project folder). Options (all optional): cwd_rel (a folder under the repo to start in,
   sent as is), carry {title, prompt, when, name, cron} (refills a task form after a repo switch), targets [{p, r, label}] + onTarget(entry, carry) (a 'Repo' select, like taskForm),
   onDone(res, when), onCancel(), back {label, onClick}, when ('now' | 'later' | 'schedule'), preset {agent, session, auto_close} (dispatch). It answers the controller {form, V, preview(),
   body(), submit(), sheet}. launcherForm(opts) is the same without the sheet (Shell.showForm wraps a form in its own).
   The fields of each agent come from AGENT_SCHEMAS, the embedded fallback that GET /api/agents overrides ONCE per page load (launcherSchemaLoad: never on the poll; an answer without
   agents keeps the fallback, a failed request is tried again on the next open). The agent's option_schema() decides which advanced fields exist; the model chips, the reasoning levels
   (disabled per model from reasoning_by_model), the permission modes and the capabilities come from it too.
   Remembered per repo and agent: ccboard:launch:<project>/<repo>:<agent> (the pre-v0.5.13 ccboard:launch:<project>/<repo> is read once as Claude, converted and removed),
   ccboard:agent:<project>/<repo> (the agent last used), ccboard:defaults:<project> (what a repo with no memory of its own starts from), ccboard:task:<project>/<repo>[:codex] (task mode;
   Claude keeps the bare key: components.js taskDefaultsLine and dnd.js read it), ccboard:presets:<project>/<repo> (saved presets). A bypass-class choice (bypassPermissions, Codex bypass,
   danger-full-access) is never remembered; its 'I understand' is, once per repo (ccboard:bypass-ack:<project>/<repo>), and it exists in a session only: task and dispatch modes never offer it. */

const LX_EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max'];
const LX_CLAUDE_CHIPS = ['opus', 'fable', 'sonnet', 'haiku'];                  // the model chips, in this order (the brief's F7: opus first)
const LX_CLAUDE_MORE = ['opusplan', 'best', 'opus[1m]', 'sonnet[1m]'];         // under 'More models' with default and a custom id
const LX_MODEL_HUE = { opus: 'hue-blue', fable: 'hue-violet', sonnet: 'hue-green', haiku: 'hue-slate' };   // tokens.css .hue-*, the same four as the terminal's tuning strip
const LX_CX_MODES = [['default', 'default'], ['auto', 'auto'], ['read-only', 'read-only'], ['bypass', 'bypass'], ['custom', 'custom']];
const LX_CX_MODE_PERM = { default: 'default', auto: 'auto', 'read-only': 'plan' };   // the adapter's permission_mode behind each picker entry (custom sends sandbox + approval instead)
const LX_SANDBOXES = ['read-only', 'workspace-write', 'danger-full-access'];
const LX_APPROVALS = ['untrusted', 'on-failure', 'on-request', 'never'];
const LX_CONFIG_RE = /^[A-Za-z0-9_.]+=.+$/;                                  // one -c line; the server's blocklist still wins
const LX_PERM_LABEL = { manual: 'ask (default)', acceptEdits: 'accept edits', plan: 'plan', auto: 'auto', dontAsk: "don't ask: deny prompts", bypassPermissions: 'bypass: never ask (dangerous)' };
const BYPASS_WARNING_CODEX = 'Codex runs every command and edit without asking and without its sandbox, as your user, on this box.';
const LX_BYPASS_ACK = 'I understand';

function lxOpt(key, label, kind, choices, def, help, group, danger, when) {
  return { key, label, kind, choices: choices || null, default: def === undefined ? null : def, help: help || '', group: group || 'basic', danger: !!danger, when: when || null };
}

/* The embedded fallback: the shape GET /api/agents answers per agent (app/agents/base.py Agent.describe): options [{key, label, kind, choices, default, help, group, danger, when}],
   permission_modes, efforts, models, reasoning_by_model, capabilities (Codex). Codex's models are the box's visible slugs at codex 0.145. */
const AGENT_SCHEMAS = {
  claude: {
    name: 'claude', label: 'Claude', glyph: '◆', installed: true,
    options: [
      lxOpt('launcher', 'Start', 'select', ['new', 'resume', 'continue', 'from_pr'], 'new'),
      lxOpt('resume_id', 'Session to resume', 'text', null, null, '', 'basic', false, { launcher: ['resume'] }),
      lxOpt('from_pr', 'Pull request', 'text', null, null, '', 'basic', false, { launcher: ['from_pr'] }),
      lxOpt('name', 'Session name', 'text', null, null),
      lxOpt('model', 'Model', 'combo', ['opus', 'fable', 'sonnet', 'haiku', 'opusplan', 'best', 'opus[1m]', 'sonnet[1m]'], 'opus'),
      lxOpt('effort', 'Effort', 'select', LX_EFFORTS, 'high'),
      lxOpt('permission_mode', 'Permission mode', 'select', ['manual', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions'], null),
      lxOpt('prompt', 'First prompt', 'textarea', null, null, '', 'basic', false, { launcher: ['new'] }),
      lxOpt('fast', 'Fast mode', 'bool', null, false),
      lxOpt('bypass', 'Skip all permission prompts', 'bool', null, false, '', 'advanced', true),
      lxOpt('allowed_tools', 'Allowed tools', 'textarea', null, null, '', 'advanced'),
      lxOpt('disallowed_tools', 'Disallowed tools', 'textarea', null, null, '', 'advanced'),
      lxOpt('tools', 'Available tools', 'textarea', null, null, '', 'advanced'),
      lxOpt('append_system_prompt', 'Append to system prompt', 'textarea', null, null, '', 'advanced'),
      lxOpt('agent_name', 'Agent', 'text', null, null, '', 'advanced'),
      lxOpt('fallback_model', 'Fallback model', 'text', null, null, '', 'advanced'),
      lxOpt('autocompact', 'Auto-compact', 'combo', ['auto'], null, '', 'advanced'),
      lxOpt('worktree', 'Start in a new git worktree', 'bool', null, false, '', 'advanced', false, { launcher: ['new'] }),
      lxOpt('worktree_name', 'Worktree name', 'text', null, null, '', 'advanced', false, { worktree: true }),
      lxOpt('fork_session', 'Fork instead of continuing', 'bool', null, false, '', 'advanced', false, { launcher: ['resume', 'continue'] }),
      lxOpt('add_dirs', 'Extra directories', 'dirs', null, null, '', 'advanced'),
      lxOpt('devcontainer', 'Run in the devcontainer', 'bool', null, false, '', 'advanced', false, { 'repo.devcontainer': true }),
      lxOpt('mcp_config', 'MCP config file', 'text', null, null, '', 'advanced'),
      lxOpt('extra', 'Extra arguments', 'args', null, null, '', 'advanced'),
    ],
    permission_modes: ['manual', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions'], efforts: LX_EFFORTS,
    models: ['opus', 'fable', 'sonnet', 'haiku', 'opusplan', 'best', 'opus[1m]', 'sonnet[1m]'], reasoning_by_model: {}, capabilities: { ultracode_flag: false },
  },
  codex: {
    name: 'codex', label: 'Codex', glyph: '◇', installed: false,
    options: [
      lxOpt('launcher', 'Start', 'select', ['new', 'resume', 'continue', 'fork'], 'new'),
      lxOpt('resume_id', 'Session to resume or fork', 'text', null, null, '', 'basic', false, { launcher: ['resume', 'fork'] }),
      lxOpt('name', 'Session name', 'text', null, null),
      lxOpt('model', 'Model', 'combo', ['gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-5.5', 'codex-auto-review'], null),
      lxOpt('reasoning_effort', 'Reasoning', 'select', LX_EFFORTS, null),
      lxOpt('mode', 'Mode', 'select', ['default', 'auto', 'read-only', 'bypass', 'custom'], 'default'),
      lxOpt('sandbox', 'Sandbox', 'select', LX_SANDBOXES, null, '', 'basic', false, { mode: ['custom'] }),
      lxOpt('approval', 'Approval policy', 'select', ['untrusted', 'on-request', 'never'], null, '', 'basic', false, { mode: ['custom'] }),
      lxOpt('prompt', 'First prompt', 'textarea', null, null, '', 'basic', false, { launcher: ['new'] }),
      lxOpt('search', 'Live web search', 'bool', null, false, '', 'advanced'),
      lxOpt('bypass', 'Skip approvals and the sandbox', 'bool', null, false, '', 'advanced', true),
      lxOpt('permission_mode', 'Permission mode (Claude\'s words)', 'select', ['default', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions'], null, '', 'advanced'),
      lxOpt('add_dirs', 'Extra directories', 'dirs', null, null, '', 'advanced'),
      lxOpt('worktree', 'Start in a new git worktree', 'bool', null, false, '', 'advanced', false, { launcher: ['new'] }),
      lxOpt('worktree_name', 'Worktree name', 'text', null, null, '', 'advanced', false, { worktree: true }),
      lxOpt('config', 'Config overrides', 'textarea', null, null, '', 'advanced'),
      lxOpt('extra', 'Extra arguments', 'args', null, null, '', 'advanced'),
    ],
    permission_modes: ['default', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions'], efforts: LX_EFFORTS,
    models: ['gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-5.5', 'codex-auto-review'],
    reasoning_by_model: { 'gpt-5.6-terra': LX_EFFORTS, 'gpt-5.6-sol': LX_EFFORTS, 'gpt-5.6-luna': LX_EFFORTS, 'gpt-5.5': LX_EFFORTS, 'codex-auto-review': LX_EFFORTS },
    capabilities: { fork: true, approve_for_me: false, bypass_approvals: true, yolo: false, search: true, add_dir: true, no_alt_screen: true, approval_on_failure: false, approval_untrusted: true },
  },
};
const LX_SHELL = { name: 'shell', label: 'Shell', glyph: '▸', installed: true, options: [], permission_modes: [], efforts: [], models: [], reasoning_by_model: {}, capabilities: {} };

/* GET /api/agents, once per page load (never on the poll). The sheet opens at once on the fallback and repaints when the answer lands. */
const LX_LOAD = { agents: null, promise: null };

function launcherSchemaLoad() {
  if (LX_LOAD.promise) return LX_LOAD.promise;
  LX_LOAD.promise = (async () => {
    try {
      const r = await api('GET', '/api/agents');
      const m = r && r.agents && typeof r.agents === 'object' ? r.agents : null;
      if (m && (m.claude || m.codex)) LX_LOAD.agents = m;               // an answer without agents (the demo board before its fixture exists) keeps the fallback
    } catch (_) { LX_LOAD.promise = null; }                              // a failed request is tried again on the next open, never on the poll
    return LX_LOAD.agents;
  })();
  return LX_LOAD.promise;
}

/* The schema of one agent: the embedded one with whatever GET /api/agents said laid over it (a list that came back empty keeps the embedded one). */
function launcherSchema(agent) {
  const base = agent === 'shell' ? LX_SHELL : (AGENT_SCHEMAS[agent] || LX_SHELL);
  const got = LX_LOAD.agents && LX_LOAD.agents[agent] && typeof LX_LOAD.agents[agent] === 'object' ? LX_LOAD.agents[agent] : null;
  if (!got) return base;
  const list = (k) => (Array.isArray(got[k]) && got[k].length ? got[k] : base[k]);
  const map = got.reasoning_by_model && typeof got.reasoning_by_model === 'object' && Object.keys(got.reasoning_by_model).length ? got.reasoning_by_model : base.reasoning_by_model;
  return { ...base, ...got, options: list('options'), permission_modes: list('permission_modes'), efforts: list('efforts'), models: list('models'), reasoning_by_model: map,
    capabilities: { ...(base.capabilities || {}), ...(got.capabilities && typeof got.capabilities === 'object' ? got.capabilities : {}) } };
}

function lxHas(agent, key) { return launcherSchema(agent).options.some((o) => o.key === key); }

function lxInstalled(agent) {
  if (agent === 'shell') return true;
  const st = typeof state !== 'undefined' ? state : null;
  const a = st && st.agents && st.agents[agent];
  if (agent === 'claude') return !(a && a.installed === false);
  return !!(a && a.installed);
}

/* Reasoning levels of a Codex model: {all: every level any model offers, allowed: the levels this model accepts (all of them for a model the catalogue does not list)}. */
function launcherReasoning(schema, model) {
  const by = schema.reasoning_by_model || {};
  const all = [];
  for (const e of [...(schema.efforts || []), ...Object.values(by).flat()]) if (typeof e === 'string' && e && !all.includes(e)) all.push(e);
  all.sort((a, b) => { const i = LX_EFFORTS.indexOf(a); const j = LX_EFFORTS.indexOf(b); return (i < 0 ? 99 : i) - (j < 0 ? 99 : j); });
  const own = model && Array.isArray(by[model]) && by[model].length ? by[model] : null;
  return { all, allowed: own || all };
}

/* ---------- what is remembered ---------- */

function lxGet(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
function lxPut(key, value) { try { if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (_) { /* storage may be unavailable */ } }
function lxJson(raw) { try { const v = JSON.parse(raw || 'null'); return v && typeof v === 'object' && !Array.isArray(v) ? v : null; } catch (_) { return null; } }

const LX_KEY = (p, r, agent) => `ccboard:launch:${p}/${r}:${agent}`;
const LX_LEGACY_KEY = (p, r) => `ccboard:launch:${p}/${r}`;
const LX_AGENT_KEY = (p, r) => `ccboard:agent:${p}/${r}`;
const LX_DEFAULTS_KEY = (p) => `ccboard:defaults:${p}`;
const LX_ACK_KEY = (p, r) => `ccboard:bypass-ack:${p}/${r}`;
const LX_PRESETS_KEY = (p, r) => `ccboard:presets:${p}/${r}`;
const LX_TASK_KEY = (p, r, agent) => `ccboard:task:${p}/${r}` + (agent === 'claude' ? '' : `:${agent}`);   // Claude keeps the bare key: components.js taskDefaultsLine and dnd.js read it

/* The pre-v0.5.13 session form's memory ({launcher, model_sel, model_id, effort, permission_mode, allowed_tools, ...}) in this sheet's terms. The launch kind is not carried over:
   a new session is what a + opens. A remembered bypass is dropped, like every bypass. */
function launcherLegacyPrefs(old) {
  const o = old && typeof old === 'object' ? old : {};
  const v = {};
  if (o.model_sel === 'custom' && o.model_id) { v.model = 'custom'; v.model_custom = String(o.model_id); }
  else if (typeof o.model_sel === 'string') v.model = o.model_sel;
  if (typeof o.effort === 'string') v.effort = o.effort;
  if (typeof o.permission_mode === 'string' && o.permission_mode !== 'bypassPermissions') v.permission_mode = o.permission_mode;
  for (const k of ['allowed_tools', 'disallowed_tools', 'append_system_prompt', 'args']) if (typeof o[k] === 'string' && o[k]) v[k] = o[k];
  return v;
}

/* What a launch of `agent` in p/r starts from: its own memory, else (Claude) the old key, converted and removed here (read once), else the project's defaults, else {}. */
function launcherPrefs(p, r, agent) {
  const own = lxJson(lxGet(LX_KEY(p, r, agent)));
  if (own) return own;
  if (agent === 'claude') {
    const old = lxJson(lxGet(LX_LEGACY_KEY(p, r)));
    if (old) {
      const conv = launcherLegacyPrefs(old);
      lxPut(LX_KEY(p, r, 'claude'), JSON.stringify(conv));
      lxPut(LX_LEGACY_KEY(p, r), null);
      return conv;
    }
  }
  const d = lxJson(lxGet(LX_DEFAULTS_KEY(p)));
  return d && d[agent] && typeof d[agent] === 'object' ? { ...d[agent] } : {};
}

function launcherAgentPref(p, r) {
  const own = lxGet(LX_AGENT_KEY(p, r));
  if (own === 'claude' || own === 'codex' || own === 'shell') return own;
  const d = lxJson(lxGet(LX_DEFAULTS_KEY(p)));
  return d && (d.agent === 'claude' || d.agent === 'codex' || d.agent === 'shell') ? d.agent : '';
}

function launcherAcked(p, r) { return lxGet(LX_ACK_KEY(p, r)) === '1'; }
function launcherAck(p, r) { lxPut(LX_ACK_KEY(p, r), '1'); }

const LX_BUILTIN_PRESETS = {
  claude: [{ id: 'opus-high-worktree', name: 'opus high worktree', v: { model: 'opus', effort: 'high', worktree: true } },
    { id: 'ultracode', name: 'ultracode', v: { ultracode: true } }],
  codex: [],
};
function launcherPresets(p, r, agent) {
  const saved = lxJson(lxGet(LX_PRESETS_KEY(p, r)));
  const mine = saved && Array.isArray(saved.list) ? saved.list.filter((x) => x && x.agent === agent && typeof x.name === 'string' && x.v && typeof x.v === 'object') : [];
  return { builtin: LX_BUILTIN_PRESETS[agent] || [], saved: mine };
}
function launcherPresetSave(p, r, agent, name, v) {
  const saved = lxJson(lxGet(LX_PRESETS_KEY(p, r)));
  const list = saved && Array.isArray(saved.list) ? saved.list.filter((x) => x && !(x.agent === agent && x.name === name)) : [];
  list.push({ agent, name, v });
  lxPut(LX_PRESETS_KEY(p, r), JSON.stringify({ list: list.slice(-24) }));
}
function launcherPresetDrop(p, r, agent, name) {
  const saved = lxJson(lxGet(LX_PRESETS_KEY(p, r)));
  const list = saved && Array.isArray(saved.list) ? saved.list.filter((x) => x && !(x.agent === agent && x.name === name)) : [];
  lxPut(LX_PRESETS_KEY(p, r), JSON.stringify({ list }));
}

/* ---------- the values of the form, and what they mean ---------- */

/* The values a form starts from, per agent. Claude: model opus, effort high (the brief's F7); everything else empty until remembered. */
function launcherDefaults(agent) {
  if (agent === 'claude') return { model: 'opus', model_custom: '', effort: 'high', ultracode: false, fast: false, permission_mode: '', tools: '', allowed_tools: '', disallowed_tools: '', append_system_prompt: '',
    agent_name: '', fallback_model: '', autocompact: '', mcp_config: '', devcontainer: false };
  if (agent === 'codex') return { model: '', model_custom: '', reasoning: '', cx_mode: 'default', sandbox: 'workspace-write', approval: 'on-request', search: false, config: '' };
  return {};
}

function lxIsDangerous(v) {
  if (!v) return false;
  if (v.agent === 'claude') return v.permission_mode === 'bypassPermissions';
  if (v.agent === 'codex') return v.cx_mode === 'bypass' || (v.cx_mode === 'custom' && v.sandbox === 'danger-full-access');
  return false;
}

/* The danger gate: does this set of values need the red callout (and, until the repo has acknowledged it, the 'I understand' box)? Never in task or dispatch mode: there the bypass
   choices are not offered, and a remembered one is turned back to the default. */
function launcherDanger(v, mode) { return (mode || 'session') === 'session' && lxIsDangerous(v); }

/* The part of a form's values that is remembered: no bypass-class choice, no first prompt, no name, no resume id, no PR, no worktree (a branch per session is a choice of the moment: the preset chip is one tap). A switch that is off and a box that is empty are not kept (they
   are the defaults), except an empty model or effort: 'default (settings)' is a choice next to opus and high. Codex's sandbox and approval only count in its custom mode. */
function launcherRemember(v) {
  const keep = v.agent === 'claude'
    ? ['model', 'model_custom', 'effort', 'ultracode', 'fast', 'permission_mode', 'tools', 'allowed_tools', 'disallowed_tools', 'append_system_prompt', 'agent_name', 'fallback_model', 'autocompact', 'mcp_config', 'devcontainer', 'args']
    : v.agent === 'codex' ? ['model', 'model_custom', 'reasoning', 'cx_mode', 'sandbox', 'approval', 'search', 'config', 'args'] : [];
  const out = {};
  for (const k of keep) {
    const x = v[k];
    if (x === undefined || x === null || x === false) continue;
    if (x === '' && !(k === 'effort' || (k === 'model' && v.agent === 'claude'))) continue;
    out[k] = x;
  }
  if (v.agent === 'claude' && out.permission_mode === 'bypassPermissions') delete out.permission_mode;
  if (v.agent === 'codex') {
    if (out.cx_mode === 'bypass' || (out.cx_mode === 'custom' && out.sandbox === 'danger-full-access')) { out.cx_mode = 'default'; delete out.sandbox; delete out.approval; }
    else if (out.cx_mode !== 'custom') { delete out.sandbox; delete out.approval; }
  }
  return out;
}

/* A shell-ish splitter for the extra-args box ('--verbose --fallback-model "x y"'): quotes group, a backslash escapes. A line that does not split cleanly is returned as one token. */
function lxSplit(text) {
  const out = [];
  let cur = '';
  let q = '';
  let has = false;
  const s = String(text || '');
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (q) { if (c === q) q = ''; else if (c === '\\' && q === '"' && i + 1 < s.length) cur += s[++i]; else cur += c; continue; }
    if (c === '"' || c === "'") { q = c; has = true; continue; }
    if (c === '\\' && i + 1 < s.length) { cur += s[++i]; has = true; continue; }
    if (/\s/.test(c)) { if (cur || has) { out.push(cur); cur = ''; has = false; } continue; }
    cur += c;
  }
  if (cur || has) out.push(cur);
  return out;
}
function lxQuote(s) { const t = String(s); return /^[A-Za-z0-9_@%+=:,./-]+$/.test(t) ? t : `'${t.replace(/'/g, "'\\''")}'`; }
function lxJoin(args) { return args.map(lxQuote).join(' '); }
/* A command line as nodes for the preview: every token (a quoted string is one) in a .lx-t span that does not break, so a flag is never cut after its `--`; the spaces between them are the
   only places a line wraps. A token longer than 44 characters (a path; the longest flag, --dangerously-bypass-approvals-and-sandbox, is 42) stays plain text and may break anywhere; a note line (`# then /fast`) is plain text. Copy reads the plain string. */
function lxCmdNodes(text) {
  const out = [];
  String(text).split('\n').forEach((line, i) => {
    if (i) out.push('\n');
    if (line.startsWith('#')) { out.push(line); return; }
    const toks = [];
    let cur = '';
    let q = false;
    for (let k = 0; k < line.length; k++) {
      const c = line[k];
      if (!q && c === '\\' && k + 1 < line.length) { cur += c + line[++k]; continue; }
      if (c === "'") q = !q;
      if (!q && /\s/.test(c)) { if (cur) toks.push(cur); cur = ''; continue; }
      cur += c;
    }
    if (cur) toks.push(cur);
    toks.forEach((t, j) => {
      if (j) out.push(' ');
      out.push(t.length <= 44 ? el('span', { class: 'lx-t', text: t }) : t);
    });
  });
  return out;
}
function lxList(text) { return String(text || '').split(/[,\n]+/).map((x) => x.trim()).filter(Boolean); }
function lxShort(text, n) { const t = String(text || '').replace(/\s+/g, ' ').trim(); return t.length > n ? t.slice(0, n - 1) + '…' : t; }
function lxModelOf(v) { return v.model === 'custom' ? String(v.model_custom || '').trim() : String(v.model || ''); }

/* The command a launch starts, as the adapter would build it (app/agents/claude.py and codex.py launch_plan: the same flags in the same order). APPROXIMATE: the session id, a cut
   copy of the prompt and the paths are placeholders, and Codex's hook-trust flags depend on the box; the response's cmd is the truth and replaces this line after Start.
   v: the flat values {agent, launch, name, resume_id, from_pr, prompt, model, ..., args, worktree, worktree_name, add_dirs}; ctx {mode, caps, cwd, nextName, dirs}. */
function commandPreview(v, ctx) {
  const c = ctx || {};
  const mode = c.mode || 'session';
  const lane = mode !== 'session';                                           // a task or a dispatched lane: its own worktree, the prompt is the task's, never a bypass
  const then = [];
  if (v.agent === 'shell') {
    const cmd = c.devcontainer ? lxJoin(['devcontainer', 'up', '--workspace-folder', c.cwd || '.']) + ' && ' + lxJoin(['devcontainer', 'exec', '--workspace-folder', c.cwd || '.', '--', 'bash', '-l']) : '';
    return cmd || `# a plain shell${c.cwd ? ' in ' + c.cwd : ''}`;
  }
  const dirs = (v.add_dirs || []).map((d) => (c.dirs && c.dirs[d]) || d);
  const prompt = (!lane && v.launch !== 'new') ? '' : String(v.prompt || '').trim();
  const flags = [];
  const wt = lane ? true : !!v.worktree;
  const sname = String(v.name || '').trim() || c.nextName || 's1';           // the session's name: --name, and what a blank worktree name is made from
  const wtGiven = String(v.worktree_name || '').trim();
  const wtName = wtGiven || (lane ? (c.slug || '<task>') : sname);          // Codex: the managed worktree folder (the board slugs the name; no suffix)
  const wtClaude = wtGiven || (lane ? (c.slug || '<task>') : `${sname}-xxxxxx`);   // Claude: a blank name becomes <session>-<6 random hex> on the server; the six x stand for them
  let argv;
  if (v.agent === 'claude') {
    const model = lxModelOf(v);
    if (model) flags.push('--model', model);
    if (v.effort || (v.ultracode && c.ultraNative)) flags.push('--effort', v.ultracode && c.ultraNative ? 'ultracode' : v.effort);
    if (v.permission_mode && v.permission_mode !== 'manual') flags.push('--permission-mode', v.permission_mode);
    const al = lxList(v.allowed_tools); if (al.length) flags.push('--allowedTools', ...al);
    const dl = lxList(v.disallowed_tools); if (dl.length) flags.push('--disallowedTools', ...dl);
    if (String(v.append_system_prompt || '').trim()) flags.push('--append-system-prompt', String(v.append_system_prompt).trim());    // the adapter's order: this one BEFORE --tools
    const tl = lxList(v.tools); if (tl.length) flags.push('--tools', tl.join(','));                                                    // one comma-joined token
    if (String(v.agent_name || '').trim()) flags.push('--agent', String(v.agent_name).trim());
    const fm = [...new Set(String(v.fallback_model || '').split(/[,\s]+/).filter(Boolean))]; if (fm.length) flags.push('--fallback-model', fm.join(','));
    if (String(v.autocompact || '').trim()) flags.push('--autocompact', String(v.autocompact).trim());
    if (String(v.mcp_config || '').trim()) flags.push('--mcp-config', String(v.mcp_config).trim());
    flags.push(...lxSplit(v.args));
    const more = dirs.length && !v.devcontainer ? ['--add-dir', ...dirs] : [];
    const fork = v.fork_session && !lane && (v.launch === 'resume' || v.launch === 'continue') ? ['--fork-session'] : [];    // right after --resume <id> / --continue, as the adapter builds it
    const pr = String(v.from_pr || '').trim().replace(/^#+/, '');                                                               // the route strips the # from a PR number
    const kind = lane ? 'new' : (v.launch === 'from_pr' ? 'resume' : v.launch);
    if (kind === 'resume') {
      argv = ['claude', ...(v.launch === 'from_pr' ? ['--from-pr', pr].filter((x) => x !== '') : ['--resume', ...(String(v.resume_id || '').trim() ? [String(v.resume_id).trim()] : []), ...fork]), ...flags, ...more];
    } else if (kind === 'continue') argv = ['claude', '--continue', ...fork, ...flags, ...more];
    else if (wt) argv = ['claude', ...flags, ...more, '--worktree', wtClaude, '--session-id', '<uuid>', ...(lane ? [] : ['--name', sname]), ...(prompt ? ['--', lxShort(prompt, 60)] : [])];   // a task has no session name of its own
    else if (prompt) argv = ['claude', ...flags, ...more, '--session-id', '<uuid>', '--name', sname, '--', lxShort(prompt, 60)];
    else argv = ['claude', '--session-id', '<uuid>', '--name', sname, ...flags, ...more];
    if (v.ultracode && !c.ultraNative) then.push('/effort ultracode on');
    if (v.fast) then.push('/fast');
  } else {
    const caps = { approve_for_me: false, bypass_approvals: true, yolo: false, no_alt_screen: true, search: true, add_dir: true, ...(c.caps || {}) };
    if (caps.no_alt_screen) flags.push('--no-alt-screen');
    const md = v.cx_mode || 'default';
    const danger = md === 'bypass' || (md === 'custom' && v.sandbox === 'danger-full-access');         // the sheet sends bypass: true for both, and the adapter then builds the one bypass flag
    if (danger && !lane) flags.push(caps.bypass_approvals || !caps.yolo ? '--dangerously-bypass-approvals-and-sandbox' : '--yolo');
    else if (md === 'custom') { if (v.sandbox) flags.push('-s', v.sandbox === 'danger-full-access' ? 'workspace-write' : v.sandbox); if (v.approval) flags.push('-a', v.approval); }   // a lane never takes the danger sandbox
    else if (md === 'read-only') flags.push('-s', 'read-only', '-a', 'on-request');
    else if (md === 'auto') { if (caps.approve_for_me) flags.push('--approve-for-me', '-s', 'workspace-write'); else flags.push('-s', 'workspace-write', '-a', 'on-request'); }
    else flags.push('-s', 'workspace-write', '-a', 'on-request');
    const model = lxModelOf(v);
    if (model) flags.push('-m', model);
    if (v.reasoning) flags.push('-c', `model_reasoning_effort="${v.reasoning}"`);
    for (const line of String(v.config || '').split('\n').map((x) => x.trim()).filter(Boolean)) flags.push('-c', line);
    if (v.search && caps.search) flags.push('--search');
    if (caps.add_dir) for (const d of dirs) flags.push('--add-dir', d);
    flags.push(...lxSplit(v.args));
    const kind = lane ? 'new' : v.launch;
    const target = String(v.resume_id || '').trim();
    if (kind === 'resume') argv = ['codex', 'resume', ...flags, ...(target ? [target] : [])];
    else if (kind === 'continue') argv = ['codex', 'resume', ...flags, '--last'];
    else if (kind === 'fork') argv = ['codex', 'fork', ...flags, ...(target ? [target] : [])];           // a blank id opens Codex's picker: the server adds no --last
    else argv = ['codex', ...flags, ...(prompt ? ['--', lxShort(prompt, 60)] : [])];
  }
  let line = lxJoin(argv);
  if (v.devcontainer && v.agent === 'claude') line = lxJoin(['devcontainer', 'up', '--workspace-folder', c.cwd || '.']) + ' && ' + lxJoin(['devcontainer', 'exec', '--workspace-folder', c.cwd || '.', '--']) + ' ' + line;
  const notes = [];
  if (v.agent === 'codex' && wt) notes.push(`# in a new worktree: .ccboard/worktrees/${wtName}`);
  if (then.length) notes.push(`# then ${then.join(' · ')}`);
  return [line, ...notes].join('\n');
}

/* ---------- the account a new session runs on ---------- */

/* {agent, name, text, pct, switchTo, run} or null. Claude: state.accounts' current login and the more used of its two windows (the same reader the topbar pills use, so no screen
   shows two figures); Codex: state.codex_accounts' current login and the window of state.usage_codex when that reading is its. switchTo: from 85 % on, another SAVED login
   (Claude: one with a known, lower reading on that window; Codex: state holds no reading of the others, so any other saved login), and run() makes it the one in use (the shared
   accountSwitch / cxSwitch of pages/agents.js). null for a shell, a box with no login seen yet, or scripts that are not loaded. */
function launcherAccount(agent) {
  const st = typeof state !== 'undefined' ? state : null;
  if (!st) return null;
  const hot = 85;
  if (agent === 'claude') {
    const list = typeof agentsAccounts === 'function' ? agentsAccounts(st) : [];
    const cur = list.find((a) => a.current) || list.find((a) => st.accounts && st.accounts.current === a.key);
    if (!cur) return null;
    const now = Date.now() / 1000;
    const used = (a, w) => (typeof agentsAcctUsedNow === 'function' ? agentsAcctUsedNow(st, a, w, now) : null);
    const wins = ['5h', '7d'].map((w) => ({ w, pct: used(cur, w) })).filter((x) => x.pct !== null).sort((a, b) => b.pct - a.pct);
    const worst = wins[0] || null;
    const name = typeof agentsAcctName === 'function' ? agentsAcctName(cur) : String(cur.email || cur.key).slice(0, 24);
    let switchTo = null;
    if (worst && worst.pct >= hot && typeof acctStore === 'function' && acctStore(st).supported) {
      const better = list.filter((a) => a.saved && !a.current && a.key !== cur.key).map((a) => ({ a, pct: used(a, worst.w) })).filter((x) => x.pct !== null && x.pct < worst.pct).sort((a, b) => a.pct - b.pct);
      if (better.length) switchTo = better[0].a;
    }
    return { agent, name, pct: worst ? worst.pct : null, switchTo, run: switchTo && typeof accountSwitch === 'function' ? () => accountSwitch(switchTo) : null,
      text: `Runs on ${name}${worst ? ` · ${Math.round(100 - worst.pct)}% of the ${worst.w === '5h' ? '5-hour' : '7-day'} window left` : ''}` };
  }
  if (agent === 'codex') {
    const cs = typeof cxState === 'function' ? cxState(st) : null;
    const list = typeof cxAccounts === 'function' ? cxAccounts(st) : [];
    const cur = cs ? (list.find((a) => a.current) || list.find((a) => a.key === cs.current)) : null;
    if (!cur) return null;
    const w = typeof Shell !== 'undefined' && Shell && typeof Shell.codexWindow === 'function' ? Shell.codexWindow(st.usage_codex) : null;
    const mine = w && typeof w.used_percentage === 'number' && (!w.account || w.account === cur.key) ? w : null;
    const name = typeof cxName === 'function' ? cxName(cur) : String(cur.label || cur.key).slice(0, 24);
    const pct = mine ? Math.max(0, Math.min(100, mine.used_percentage)) : null;
    const win = mine && typeof Shell.windowName === 'function' ? Shell.windowName(mine.minutes) : 'usage';
    let switchTo = null;
    if (pct !== null && pct >= hot && typeof cxStore === 'function' && cxStore(st).supported) switchTo = list.find((a) => a.saved !== false && !a.current && a.key !== cur.key) || null;
    return { agent, name, pct, switchTo, run: switchTo && typeof cxSwitch === 'function' ? () => cxSwitch(switchTo) : null,
      text: `Runs on ${name}${pct !== null ? ` · ${Math.round(100 - pct)}% of the ${win} window left` : ''}` };
  }
  return null;
}

/* ---------- the request ---------- */

const LX_PR = /^(#?\d{1,7}|https:\/\/\S{3,300})$/;
const LX_UUID = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/* The permission part of a Codex form in the adapter's terms: {permission_mode?, bypass?, opts: {sandbox?, approval?}}. */
function lxCodexPerm(v) {
  const out = { opts: {} };
  if (v.cx_mode === 'bypass') { out.permission_mode = 'bypassPermissions'; out.bypass = true; }
  else if (v.cx_mode === 'custom') {
    if (v.sandbox) out.opts.sandbox = v.sandbox;
    if (v.approval) out.opts.approval = v.approval;
    if (v.sandbox === 'danger-full-access') out.bypass = true;                  // the adapter refuses that sandbox without the acknowledgement
  } else out.permission_mode = LX_CX_MODE_PERM[v.cx_mode] || 'default';
  return out;
}

/* The body of POST /api/projects/{p}/repos/{r}/sessions for a form (brief v0.5.13; app/main.py SessionIn): the launcher's words (agent, launcher: claude | resume | continue | fork, mode (Codex's
   one picker), reasoning, prompt, worktree, worktree_name, from_pr, fork_session, fallback_model, autocompact, tools, agent_name, mcp_config, config[], fast, search, sandbox, approval,
   cwd_rel) next to the flat compat fields the route has always read (name, model, effort, permission_mode, allowed_tools, disallowed_tools, append_system_prompt, reasoning_effort, args,
   resume_id, add_dirs, devcontainer, bypass). Blank and false values are left out; ctx.has(key) says which options the agent's schema knows (the others are not sent). A Claude
   from-PR launch is a 'claude' launcher with from_pr beside it (the route reads that as --from-pr). ultracode is not part of it unless this box's claude takes --effort ultracode
   (ctx.ultraNative: then it IS the effort); otherwise it is applied after the start (/effort ultracode on). */
function launcherPayload(v, ctx) {
  const c = ctx || {};
  const has = typeof c.has === 'function' ? c.has : () => true;
  const body = {};
  const put = (k, x) => { if (x !== undefined && x !== null && x !== '' && x !== false && !(Array.isArray(x) && !x.length)) body[k] = x; };
  const t = (x) => String(x === undefined || x === null ? '' : x).trim();
  if (v.agent === 'shell') {
    body.launcher = 'shell'; body.agent = 'shell';
    put('name', t(v.name)); put('devcontainer', !!v.devcontainer);
    return body;
  }
  const kind = v.launch || 'new';
  body.launcher = kind === 'resume' ? 'resume' : kind === 'continue' ? 'continue' : (kind === 'fork' && v.agent === 'codex') ? 'fork' : 'claude';
  body.agent = v.agent;
  put('name', t(v.name));
  put('resume_id', (kind === 'resume' || kind === 'fork') ? t(v.resume_id) : '');
  if (kind === 'from_pr') put('from_pr', t(v.from_pr));
  if (kind === 'new') put('prompt', t(v.prompt));
  put('model', lxModelOf(v));
  if (v.agent === 'claude') {
    put('effort', v.ultracode && c.ultraNative ? 'ultracode' : v.effort);
    if (v.permission_mode && v.permission_mode !== 'manual') body.permission_mode = v.permission_mode;
    if (v.permission_mode === 'bypassPermissions') { body.mode = 'bypass'; body.bypass = true; }      // the danger gate's answer, said the way the Codex path says it (the route's `mode: bypass` needs `bypass: true`)
    put('fast', !!v.fast && has('fast'));
    for (const k of ['allowed_tools', 'disallowed_tools', 'append_system_prompt']) put(k, t(v[k]));
    for (const k of ['tools', 'agent_name', 'fallback_model', 'autocompact', 'mcp_config']) if (has(k)) put(k, t(v[k]));
    if (v.fork_session && (kind === 'resume' || kind === 'continue') && has('fork_session')) body.fork_session = true;
    put('devcontainer', !!v.devcontainer);
  } else {
    put('reasoning', v.reasoning); put('reasoning_effort', v.reasoning);
    const perm = lxCodexPerm(v);
    body.mode = v.cx_mode || 'default';                                          // the one picker (C's `mode`) and, beside it, the same choice in the adapter's older words
    put('permission_mode', perm.permission_mode); put('bypass', perm.bypass);
    if (v.cx_mode === 'custom') { put('sandbox', perm.opts.sandbox); put('approval', perm.opts.approval); }
    if (v.search && has('search')) body.search = true;
    const cfg = String(v.config || '').split('\n').map((x) => x.trim()).filter(Boolean);
    if (has('config')) put('config', cfg);
  }
  if (v.worktree && has('worktree') && kind === 'new') { body.worktree = true; put('worktree_name', t(v.worktree_name)); }
  put('args', t(v.args));
  put('add_dirs', v.add_dirs);
  put('cwd_rel', t(c.cwd_rel));
  return body;
}

/* The launch choices a task or a dispatch carries (POST /api/tasks, POST /api/tasks/{id}/dispatch: TaskIn / DispatchIn read the flat LaunchOpts): never a bypass, never a worktree flag
   (a task is always in its own worktree). */
function launcherTaskOpts(v) {
  const out = {};
  const t = (x) => String(x === undefined || x === null ? '' : x).trim();
  const put = (k, x) => { if (x !== undefined && x !== null && x !== '' && x !== false && !(Array.isArray(x) && !x.length)) out[k] = x; };
  put('model', lxModelOf(v));
  if (v.agent === 'claude') {
    put('effort', v.effort);
    if (v.permission_mode && v.permission_mode !== 'manual' && v.permission_mode !== 'bypassPermissions') out.permission_mode = v.permission_mode;
    for (const k of ['allowed_tools', 'disallowed_tools', 'append_system_prompt']) put(k, t(v[k]));
  } else {
    put('reasoning_effort', v.reasoning);
    const perm = lxCodexPerm({ ...v, cx_mode: v.cx_mode === 'bypass' ? 'default' : v.cx_mode, sandbox: v.sandbox === 'danger-full-access' ? 'workspace-write' : v.sandbox });
    put('permission_mode', perm.permission_mode);
    const opts = { ...perm.opts };
    if (v.search) opts.search = true;
    if (Object.keys(opts).length) out.opts = opts;
  }
  return out;
}

/* The project/repo a launcher call names: state objects, or names ('root' or nothing is the project folder). {p, r} or null. */
function lxResolve(project, repo) {
  const st = typeof state !== 'undefined' ? state : null;
  const p = project && typeof project === 'object' ? project : ((st && st.projects) || []).find((x) => x.name === project);
  if (!p) return null;
  let r = repo && typeof repo === 'object' ? repo : null;
  if (!r) r = (repo === undefined || repo === null || repo === 'root') ? (p.root || null) : ((p.repos || []).find((x) => x.name === repo) || null);
  return r ? { p, r } : null;
}

function lxNextName(r) {
  const used = new Set((r.sessions || []).map((s) => s && s.name));
  for (let i = 1; i < 1000; i++) if (!used.has(`s${i}`)) return `s${i}`;
  return 's1';
}

/* A control that must not be used and says why: both the attribute and the property (a browser reflects one into the other; minidom does not). */
function lxDisable(node, on, why) {
  if (!node) return;
  node.disabled = !!on;
  if (on) node.setAttribute('disabled', ''); else node.removeAttribute('disabled');
  if (why !== undefined) { if (on && why) node.setAttribute('title', why); else if (!on) node.removeAttribute('title'); }
}

/* A segmented control: the sheet's .seg-ctl / .seg-btn (pages.css), buttons with aria-pressed (one on, or none), arrow keys move and skip the disabled ones.
   items [[value, label, {title, disabled, cls}]]; onPick(value) runs on a tap; set(value) only paints. */
function lxSeg(items, onPick, label, extra) {
  const node = el('div', { class: 'seg-ctl lx-seg' + (extra ? ' ' + extra : ''), role: 'group', 'aria-label': label || null });
  const btns = new Map();
  let cur = null;
  const set = (v) => { cur = v; for (const [k, b] of btns) b.setAttribute('aria-pressed', k === v ? 'true' : 'false'); };
  const step = (from, dir) => {
    const keys = [...btns.keys()];
    let i = keys.indexOf(from);
    for (let n = 0; n < keys.length; n++) { i = (i + dir + keys.length) % keys.length; if (!btns.get(keys[i]).hasAttribute('disabled')) return keys[i]; }
    return from;
  };
  for (const [v, text, o] of items) {
    const b = el('button', { class: 'seg-btn' + (o && o.cls ? ' ' + o.cls : ''), type: 'button', 'aria-pressed': 'false', 'data-when': v, text, title: (o && o.title) || null,
      onclick: () => { if (b.hasAttribute('disabled')) return; onPick(v); },
      onkeydown: (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        const to = step(cur === null ? v : cur, e.key === 'ArrowRight' ? 1 : -1);
        onPick(to);
        const nb = btns.get(to);
        if (nb && typeof nb.focus === 'function') nb.focus();
      } });
    if (o && o.disabled) lxDisable(b, true, o.title);
    btns.set(v, b);
    node.append(b);
  }
  set(null);
  return { node, set, get value() { return cur; }, btn: (v) => btns.get(v), disable: (v, on, why) => lxDisable(btns.get(v), on, why) };
}

/* ---------- the sheet ---------- */

const LX_LAUNCH = {
  claude: [['new', 'New'], ['resume', 'Resume'], ['continue', 'Continue'], ['from_pr', 'From PR']],
  codex: [['new', 'New'], ['resume', 'Resume'], ['continue', 'Last'], ['fork', 'Fork']],
};
const LX_TASK_ICON = { now: 'play', later: 'add', schedule: 'time' };

function launcherForm(o) {
  const opt = o || {};
  const mode = opt.mode === 'task' || opt.mode === 'dispatch' ? opt.mode : 'session';
  const rp = lxResolve(opt.project, opt.repo);
  if (!rp) return null;
  const { p, r } = rp;
  const session = mode === 'session';
  const task = opt.task && typeof opt.task === 'object' ? opt.task : null;
  if (mode === 'dispatch' && !task) return null;
  const carry = opt.carry && typeof opt.carry === 'object' ? opt.carry : (mode === 'task' && task ? task : {});
  const preset = { agent: opt.agent, session: opt.session, auto_close: opt.auto_close, ...(opt.preset && typeof opt.preset === 'object' ? opt.preset : {}) };      // launch() passes them at the top level
  const agentsHere = session ? ['claude', 'codex', 'shell'] : ['claude', 'codex'];
  const painters = [];
  const sib = r.root ? [] : allRepos().filter((x) => x.project === p.name && x.repo !== r.name);
  const others = allRepos().filter((x) => x.project !== p.name);
  const dirPath = {};
  for (const x of allRepos()) { const pj = (state.projects || []).find((q) => q.name === x.project); const rr = pj && [pj.root, ...(pj.repos || [])].find((q) => q && q.name === x.repo); if (rr && rr.path) dirPath[x.id] = rr.path; }

  /* ---- the values ---- */
  const known = (agent, m) => agent === 'claude' ? (LX_CLAUDE_CHIPS.includes(m) || LX_CLAUDE_MORE.includes(m) || launcherSchema('claude').models.includes(m)) : launcherSchema('codex').models.includes(m);
  const fromPrefs = (agent) => {
    const saved = session ? launcherPrefs(p.name, r.name, agent) : (lxJson(lxGet(LX_TASK_KEY(p.name, r.name, agent))) || {});
    const v = { ...launcherDefaults(agent), ...saved };
    if (agent === 'codex' && !saved.reasoning && saved.reasoning_effort) v.reasoning = saved.reasoning_effort;
    if (typeof v.model === 'string' && v.model && v.model !== 'custom' && !known(agent, v.model)) { v.model_custom = v.model; v.model = 'custom'; }
    if (agent === 'claude') {
      if (v.permission_mode === 'manual') v.permission_mode = '';
      if (!session && v.permission_mode === 'bypassPermissions') v.permission_mode = '';
    } else if (agent === 'codex' && !session) {
      if (v.cx_mode === 'bypass') v.cx_mode = 'default';
      if (v.sandbox === 'danger-full-access') v.sandbox = 'workspace-write';
    }
    if (agent === 'codex' && !LX_CX_MODES.some(([k]) => k === v.cx_mode)) v.cx_mode = 'default';
    return { worktree: false, worktree_name: '', fork_session: false, args: '', ...v };
  };
  const first = [opt.agent, mode === 'dispatch' ? (preset.agent || task.agent) : '', launcherAgentPref(p.name, r.name), 'claude', 'codex', 'shell'].find((a) => agentsHere.includes(a) && lxInstalled(a)) || 'claude';
  const V = { agent: first, claude: fromPrefs('claude'), codex: fromPrefs('codex'), shell: { devcontainer: false },
    common: { launch: 'new', name: carry.name && session ? carry.name : '', resume_id: '', from_pr: '', prompt: String(mode === 'dispatch' ? (task.prompt || '') : (carry.prompt || '')), add_dirs: session && first === 'claude' ? sib.map((x) => x.id) : [] } };         // a session starts with its sibling repos checked (as the old form did); a task's directories are a choice
  const view = () => ({ ...V.common, ...V[V.agent], agent: V.agent });
  const schema = () => launcherSchema(V.agent);
  const ctx = () => ({ mode, caps: launcherSchema('codex').capabilities, cwd: r.path || '', nextName: lxNextName(r), dirs: dirPath, slug: (mode === 'dispatch' && task.slug) || taskTitleFrom(V.common.prompt).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 30) || '<task>',
    devcontainer: !!V.shell.devcontainer, has: (k) => lxHas(V.agent, k), cwd_rel: opt.cwd_rel || '', ultraNative: ultraNative() });
  const ultraNative = () => launcherSchema('claude').efforts.includes('ultracode');

  /* ---- small builders: a control reads its value from V and writes it back; paint() puts V into the controls (a preset, an agent switch, a model change) ---- */
  let update = () => {};
  const edit = () => { truth = ''; visible(); update(); };
  const text = (get, set, attrs) => {
    const n = el('input', { type: 'text', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', ...(attrs || {}) });
    n.value = get();
    const on = () => { set(n.value); edit(); };
    n.addEventListener('input', on); n.addEventListener('change', on);
    painters.push(() => { if (n.value !== get()) n.value = get(); });
    return n;
  };
  const area = (get, set, attrs) => {
    const n = el('textarea', { autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', rows: '3', ...(attrs || {}) });
    n.value = get();
    const on = () => { set(n.value); edit(); };
    n.addEventListener('input', on); n.addEventListener('change', on);
    painters.push(() => { if (n.value !== get()) n.value = get(); });
    return n;
  };
  const check = (label, get, set, title) => {
    const cb = el('input', { type: 'checkbox' });
    cb.checked = !!get();
    cb.addEventListener('change', () => { set(cb.checked); edit(); });
    painters.push(() => { cb.checked = !!get(); });
    return { node: el('label', { title: title || null }, cb, label), input: cb };
  };
  const pick = (options, get, set) => {
    const s = selectEl(options, get());
    s.addEventListener('change', () => { set(s.value); edit(); });
    painters.push(() => { s.value = get(); });
    return s;
  };
  const hide = (n, on) => { if (n) n.classList.toggle('hidden', !!on); };
  const sec = (label) => el('h3', { class: 'lx-k', text: label });
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const checksRow = (...kids) => el('div', { class: 'checks' }, ...kids.filter(Boolean).map((k) => k.node || k));

  /* ---- the agent ---- */
  const agentItems = [['claude', '◆ Claude'], ['codex', '◇ Codex'], ['shell', '▸ Shell']].filter(([a]) => agentsHere.includes(a));
  const agentSeg = lxSeg(agentItems, (a) => switchAgent(a), 'Agent', 'lx-agent');
  const agentWhy = el('p', { class: 'dim lx-hint lx-why' });                            // the reasons a button is off, as text: a title is invisible on touch (shown there by pages.css)
  const paintAgentSeg = () => {
    const reasons = [];
    for (const [a] of agentItems) {
      const why = !lxInstalled(a) ? `${AGENT_NAME[a] || a} is not installed on this box` : '';
      agentSeg.disable(a, !!why, why);
      if (why) reasons.push(why);
    }
    agentWhy.textContent = [...new Set(reasons)].join(' · ');
    agentSeg.set(V.agent);
  };
  const switchAgent = (a) => {
    if (a === V.agent || !agentsHere.includes(a) || !lxInstalled(a)) return;
    V.agent = a;
    const kinds = (LX_LAUNCH[a] || [['new']]).map((x) => x[0]);
    if (!kinds.includes(V.common.launch)) V.common.launch = 'new';
    paintAgent();
  };

  /* ---- presets ---- */
  const presetBox = el('div', { class: 'chips lx-presets', role: 'group', 'aria-label': 'Presets' });
  const presetField = field('Presets', presetBox);
  let presetBtns = [];
  const saveRow = el('div', { class: 'row lx-saverow hidden' });
  const saveName = el('input', { type: 'text', maxlength: 40, placeholder: 'preset name', 'aria-label': 'Preset name', autocomplete: 'off', autocapitalize: 'off' });
  const applyPreset = (pr) => {
    const c = V[V.agent];
    for (const [k, x] of Object.entries(pr.v || {})) if (k in c) c[k] = x;
    if (typeof c.model === 'string' && c.model && c.model !== 'custom' && !known(V.agent, c.model)) { c.model_custom = c.model; c.model = 'custom'; }
    if (!session) {
      if (c.permission_mode === 'bypassPermissions') c.permission_mode = '';
      if (c.cx_mode === 'bypass') c.cx_mode = 'default';
      if (c.sandbox === 'danger-full-access') c.sandbox = 'workspace-write';
    }
    reasoningFix();
    paint();
  };
  const paintPresets = () => {
    presetBox.textContent = '';
    presetBtns = [];
    const ps = launcherPresets(p.name, r.name, V.agent);
    const chips = [];
    for (const [kind, list] of [['builtin', session ? ps.builtin : []], ['saved', ps.saved]]) {      // the built-in ones are a session's: a task is always in a worktree, and ultracode is typed into a live session
      for (const pr of list) {
        const b = el('button', { class: 'chip-btn', type: 'button', text: pr.name, 'aria-pressed': 'false', 'data-preset': pr.id || pr.name, onclick: () => applyPreset(pr) });
        presetBtns.push({ b, pr });
        if (kind === 'saved') {
          chips.push(el('span', { class: 'lx-pchip' }, b, el('button', { class: 'chip-btn lx-pdrop', type: 'button', 'aria-label': `Remove preset ${pr.name}`, title: 'Remove this preset', text: '×',
            onclick: () => { launcherPresetDrop(p.name, r.name, V.agent, pr.name); paintPresets(); update(); } })));
        } else chips.push(b);
      }
    }
    const saveBtn = el('button', { class: 'chip-btn lx-save', type: 'button', text: '+ save these', title: 'Remember the choices above as a preset for this repo', onclick: () => { saveRow.classList.remove('hidden'); focusFine(saveName); } });
    const last = chips.pop();
    presetBox.append(...chips);
    if (last) presetBox.append(el('span', { class: 'lx-ptail' }, last, saveBtn));            // the save chip travels with the chip before it: never alone on a line
    else presetBox.append(saveBtn);
    presetBox.append(saveRow);
  };
  const saveNow = () => {
    const name = saveName.value.trim();
    if (!name) { saveName.focus(); return; }
    const v = launcherRemember(view());
    delete v.args;
    launcherPresetSave(p.name, r.name, V.agent, name, v);
    saveName.value = ''; saveRow.classList.add('hidden');
    paintPresets(); update();
  };
  saveName.addEventListener('keydown', (e) => {                                      // Enter saves the preset, it must not start the session; Escape closes only this row
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); e.stopPropagation(); saveNow(); }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); saveName.value = ''; saveRow.classList.add('hidden'); }
  });
  saveRow.append(saveName, el('button', { class: 'small', type: 'button', text: 'Save', onclick: () => {
    const name = saveName.value.trim();
    if (!name) { saveName.focus(); return; }
    const v = launcherRemember(view());
    delete v.args;
    launcherPresetSave(p.name, r.name, V.agent, name, v);
    saveName.value = ''; saveRow.classList.add('hidden');
    paintPresets(); update();
  } }), el('button', { class: 'small minimal', type: 'button', text: 'Cancel', onclick: () => { saveName.value = ''; saveRow.classList.add('hidden'); } }));
  const presetOn = (pr) => Object.entries(pr.v || {}).every(([k, x]) => (V[V.agent][k] === undefined ? (x === false || x === '') : V[V.agent][k] === x));

  /* ---- launch kind, name, ids ---- */
  const launchHost = el('div', { class: 'lx-launchhost' });
  const paintLaunch = () => {
    const items = LX_LAUNCH[V.agent] || [];
    const lo = launcherSchema(V.agent).options.find((x) => x.key === 'launcher');
    const offered = (k) => !lo || !Array.isArray(lo.choices) || !lo.choices.length || lo.choices.includes(k);          // the schema lists the kinds this box can start (no fork without `codex fork`)
    const why = { fork: 'this Codex has no `codex fork` command', from_pr: 'this Claude Code has no --from-pr', resume: 'resume is not offered here', continue: 'continue is not offered here' };
    const seg = lxSeg(items.map(([k, label]) => [k, label, offered(k) ? null : { disabled: true, title: why[k] }]),
      (k) => { V.common.launch = k; seg.set(k); edit(); }, 'Launch');
    seg.set(V.common.launch);
    launchHost.textContent = '';
    launchHost.append(seg.node);
  };
  const nameInput = text(() => V.common.name, (x) => { V.common.name = x.trim(); }, { maxlength: 64 });
  nameInput.setAttribute('placeholder', `auto: ${lxNextName(r)}`);
  const nameField = field('Session name', nameInput);
  const resumeInput = text(() => V.common.resume_id, (x) => { V.common.resume_id = x.trim(); }, { placeholder: 'session id (blank = the picker)' });
  const resumeField = field('Session to resume', resumeInput, 'Blank opens the picker.');
  const prInput = text(() => V.common.from_pr, (x) => { V.common.from_pr = x.trim(); }, { placeholder: 'number or URL' });
  const prField = field('Pull request', prInput, 'Resumes the session linked to it.');
  const launchField = field('Launch', launchHost);

  /* ---- Claude: model chips, effort, switches, permissions ---- */
  const C = V.claude;
  const X = V.codex;
  const claudeChips = () => { const m = launcherSchema('claude').models; return LX_CLAUDE_CHIPS.filter((c) => !m.length || m.includes(c)); };
  const claudeMore = () => [...new Set([...launcherSchema('claude').models.filter((m) => !LX_CLAUDE_CHIPS.includes(m)), ...LX_CLAUDE_MORE])];
  const modelChips = lxSeg(LX_CLAUDE_CHIPS.map((m) => [m, m, { cls: LX_MODEL_HUE[m] }]), (m) => { C.model = m; edit(); paintModel(); }, 'Model', 'lx-chips');
  const moreSel = selectEl([['__', 'More models…']], '__');
  moreSel.setAttribute('aria-label', 'More models');
  const modelId = text(() => C.model_custom, (x) => { C.model_custom = x.trim(); }, { placeholder: 'full model id, e.g. claude-fable-5-1', 'aria-label': 'Custom model id' });
  moreSel.addEventListener('change', () => { if (moreSel.value !== '__') { C.model = moreSel.value; edit(); if (C.model === 'custom') focusFine(modelId); } paintModel(); });
  const paintModelOptions = () => {
    const chips = claudeChips();
    for (const m of LX_CLAUDE_CHIPS) lxDisable(modelChips.btn(m), !chips.includes(m), chips.includes(m) ? '' : `${m} is not offered by this build of Claude Code`);
    moreSel.textContent = '';
    for (const [v, t] of [['__', 'More models…'], ['', 'default (settings)'], ...claudeMore().map((m) => [m, m]), ['custom', 'custom id…']]) moreSel.append(el('option', { value: v, text: t }));
  };
  const paintModel = () => {
    const chips = claudeChips();
    modelChips.set(chips.includes(C.model) ? C.model : null);
    moreSel.value = chips.includes(C.model) ? '__' : C.model;
    hide(modelId, C.model !== 'custom');
  };
  painters.push(paintModel);
  const modelBox = el('div', { class: 'lx-model' }, modelChips.node, moreSel, modelId);
  const modelField = field('Model', modelBox);
  let effortSeg = null;
  const effortHost = el('div', { class: 'lx-effhost' });
  const paintEffortItems = () => {
    const eff = launcherSchema('claude').efforts.filter((e) => e !== 'ultracode');
    const seg = lxSeg([['', 'default'], ...eff.map((e) => [e, e])], (e) => { C.effort = e; seg.set(e); edit(); }, 'Effort', 'lx-effort');
    seg.set(C.effort || '');
    effortHost.textContent = '';
    effortHost.append(seg.node);
    effortSeg = seg;
  };
  painters.push(() => { if (effortSeg) effortSeg.set(C.effort || ''); });
  const effortField = field('Effort', effortHost);
  const fast = check('Fast mode', () => C.fast, (x) => { C.fast = x; }, 'The board types /fast once the session is up. /fast switches fast mode on or off, so if your settings already turn it on, this turns it off.');
  const ultra = check('Ultracode', () => C.ultracode, (x) => { C.ultracode = x; }, 'The board types /effort ultracode on once the session is up');
  const switchRow = checksRow(fast, ultra);
  const permSel = selectEl([['', 'ask (default)']], '');
  const paintPermOptions = () => {
    permSel.textContent = '';
    const modes = launcherSchema('claude').permission_modes.filter((m) => m !== 'manual' && m !== 'default' && (session || m !== 'bypassPermissions'));
    for (const [v, t] of [['', 'ask (default)'], ...modes.map((m) => [m, LX_PERM_LABEL[m] || m])]) permSel.append(el('option', { value: v, text: t }));
    permSel.value = C.permission_mode;
  };
  permSel.addEventListener('change', () => { C.permission_mode = permSel.value; edit(); });
  painters.push(() => { permSel.value = C.permission_mode; });
  const permField = field('Permissions', permSel);

  /* ---- Codex: model, reasoning per model, ONE mode picker ---- */
  const cxModelSel = selectEl([['', 'default (Codex)']], '');
  const cxModelId = text(() => X.model_custom, (x) => { X.model_custom = x.trim(); }, { placeholder: 'model slug', 'aria-label': 'Custom model id' });
  cxModelSel.addEventListener('change', () => { X.model = cxModelSel.value; reasoningFix(); edit(); hide(cxModelId, X.model !== 'custom'); paintReasoning(); if (X.model === 'custom') focusFine(cxModelId); });
  const paintCxModels = () => {
    cxModelSel.textContent = '';
    for (const [v, t] of [['', 'default (Codex)'], ...launcherSchema('codex').models.map((m) => [m, m]), ['custom', 'custom id…']]) cxModelSel.append(el('option', { value: v, text: t }));
    cxModelSel.value = X.model;
    hide(cxModelId, X.model !== 'custom');
  };
  painters.push(() => { cxModelSel.value = X.model; hide(cxModelId, X.model !== 'custom'); });
  const cxModelField = field('Model', el('div', { class: 'lx-model' }, cxModelSel, cxModelId));
  const reasoningHost = el('div', { class: 'lx-effhost' });
  let reasoningSeg = null;
  const reasoningFix = () => {
    const ok = launcherReasoning(launcherSchema('codex'), X.model === 'custom' ? '' : X.model).allowed;
    if (X.reasoning && !ok.includes(X.reasoning)) X.reasoning = '';
  };
  const paintReasoning = () => {
    const rs = launcherReasoning(launcherSchema('codex'), X.model === 'custom' ? '' : X.model);
    const seg = lxSeg([['', 'default'], ...rs.all.map((l) => [l, l])], (l) => { X.reasoning = l; seg.set(l); edit(); }, 'Reasoning', 'lx-effort');
    const model = X.model === 'custom' ? 'this model' : (X.model || 'this model');
    for (const l of rs.all) seg.disable(l, !rs.allowed.includes(l), `${model} has no ${l} reasoning level`);
    seg.set(X.reasoning || '');
    reasoningHost.textContent = '';
    reasoningHost.append(seg.node);
    reasoningSeg = seg;
  };
  painters.push(() => { reasoningFix(); paintReasoning(); });
  const reasoningField = field('Reasoning', reasoningHost);
  const modeHost = el('div', { class: 'lx-modehost' });
  let modeSeg = null;
  const sandboxSel = selectEl([['workspace-write', 'workspace-write']], 'workspace-write');
  const approvalSel = selectEl([['on-request', 'on-request']], 'on-request');
  const paintModeItems = () => {
    const caps = launcherSchema('codex').capabilities || {};
    const tips = { default: 'sandboxed to the repo, asks before anything else', auto: caps.approve_for_me ? 'auto-review: Codex approves what it judges safe' : 'auto-review: this Codex has no --approve-for-me, so it asks on request',
      'read-only': 'read-only sandbox, asks before anything else', bypass: 'no approvals and no sandbox', custom: 'choose the sandbox and the approval policy yourself' };
    const seg = lxSeg(LX_CX_MODES.filter(([k]) => session || k !== 'bypass').map(([k, label]) => [k, label, { title: tips[k] }]), (k) => { X.cx_mode = k; seg.set(k); edit(); }, 'Codex mode', 'lx-modes');
    seg.set(X.cx_mode);
    modeHost.textContent = '';
    modeHost.append(seg.node);
    modeSeg = seg;
    const sbx = launcherSchema('codex').options.find((x) => x.key === 'sandbox');
    const apv = launcherSchema('codex').options.find((x) => x.key === 'approval');
    sandboxSel.textContent = ''; approvalSel.textContent = '';
    for (const s of ((sbx && sbx.choices) || LX_SANDBOXES).filter((s) => session || s !== 'danger-full-access')) sandboxSel.append(el('option', { value: s, text: s }));
    for (const a of ((apv && apv.choices) || LX_APPROVALS)) approvalSel.append(el('option', { value: a, text: a }));
    sandboxSel.value = X.sandbox; approvalSel.value = X.approval;
  };
  painters.push(() => { if (modeSeg) modeSeg.set(X.cx_mode); sandboxSel.value = X.sandbox; approvalSel.value = X.approval; });
  sandboxSel.addEventListener('change', () => { X.sandbox = sandboxSel.value; edit(); });
  approvalSel.addEventListener('change', () => { X.approval = approvalSel.value; edit(); });
  const customRow = el('div', { class: 'grid lx-custom' }, field('Sandbox', sandboxSel), field('Approval policy', approvalSel));
  const modeField = field('Mode', modeHost, 'default asks on request inside a sandbox; auto asks Codex to review its own approvals.');

  /* ---- the first prompt ---- */
  const promptEl = el('textarea', { class: 'task-prompt lx-prompt', rows: '3', autocomplete: 'off', spellcheck: 'true', 'aria-label': session ? 'First prompt' : 'Prompt',
    placeholder: session ? 'First prompt (optional)' : 'What should it do?' });
  promptEl._maxRows = 10;
  promptEl.value = V.common.prompt;
  painters.push(() => { if (promptEl.value !== V.common.prompt) promptEl.value = V.common.prompt; composerGrow(promptEl); });
  const nlBtn = newlineButton(promptEl);
  const promptField = field(session ? 'First prompt' : 'Prompt', el('div', { class: 'lx-promptbox' }, promptEl, nlBtn),
    session ? 'Optional. Enter starts it · Shift+Enter adds a line.' : 'Enter starts it · Shift+Enter adds a line.');

  /* ---- Advanced: Claude ---- */
  const dirBoxes = [];
  const dirsField = (label, list, hint) => {
    const wrap = el('div', { class: 'checks' });
    for (const x of list) {
      const cb = el('input', { type: 'checkbox', value: x.id });
      cb.checked = V.common.add_dirs.includes(x.id);
      cb.addEventListener('change', () => { const s = new Set(V.common.add_dirs); if (cb.checked) s.add(x.id); else s.delete(x.id); V.common.add_dirs = [...s]; edit(); });
      dirBoxes.push([cb, x.id]);
      wrap.append(el('label', {}, cb, x.id));
    }
    return field(label, wrap, hint);
  };
  painters.push(() => { for (const [cb, id] of dirBoxes) cb.checked = V.common.add_dirs.includes(id); });
  const wtCheck = (c) => {
    const box = check('Start in a new git worktree', () => c.worktree, (x) => { c.worktree = x; }, 'A branch and folder of its own for this session');
    const nm = text(() => c.worktree_name, (x) => { c.worktree_name = x.trim(); }, { placeholder: 'name (blank: the session name)', 'aria-label': 'Worktree name', maxlength: 80 });
    const wrap = el('div', { class: 'lx-wt' }, checksRow(box), nm);
    painters.push(() => hide(nm, !c.worktree));
    hide(nm, !c.worktree);
    return { wrap, box, nm };
  };
  const wtBlocked = !!(r.root && typeof rootIsGit === 'function' && !rootIsGit(r));
  const cWt = wtCheck(C);
  const xWt = wtCheck(X);
  if (wtBlocked) for (const w of [cWt, xWt]) lxDisable(w.box.input, true, 'the project folder is not a git repo');
  const devc = check('Run in the devcontainer', () => C.devcontainer, (x) => { C.devcontainer = x; }, 'devcontainer up + devcontainer exec (needs docker and the devcontainer CLI on the box)');
  const forkChk = check('Fork the session instead of continuing it', () => C.fork_session, (x) => { C.fork_session = x; });
  const forkRow = checksRow(forkChk);
  const textField = (label, key, c, hint, attrs) => field(label, text(() => c[key], (x) => { c[key] = x; }, attrs), hint);
  const allowedF = field('Allowed tools', area(() => C.allowed_tools, (x) => { C.allowed_tools = x; }, { rows: '2', placeholder: 'e.g. Bash(npm test), Read' }), '--allowedTools: comma or one per line.');
  const disallowedF = field('Disallowed tools', area(() => C.disallowed_tools, (x) => { C.disallowed_tools = x; }, { rows: '2', placeholder: 'e.g. WebFetch' }), '--disallowedTools.');
  const toolsF = textField('Available tools', 'tools', C, '--tools: the built-in tools the session has at all, comma separated.', { placeholder: 'e.g. Bash, Edit, Read' });
  const sysF = field('Append to system prompt', area(() => C.append_system_prompt, (x) => { C.append_system_prompt = x; }, { placeholder: 'text appended to the system prompt (optional)' }));
  const agentF = textField('Agent', 'agent_name', C, '--agent: a subagent definition to run as.', { placeholder: 'e.g. reviewer' });
  const fallbackF = textField('Fallback model', 'fallback_model', C, '--fallback-model: used when the first is overloaded.', { placeholder: 'e.g. sonnet' });
  const compactF = textField('Auto-compact', 'autocompact', C, '--autocompact: auto, or the context size to compact at (a number such as 150000).', { placeholder: 'auto or 150000' });
  const mcpF = textField('MCP config file', 'mcp_config', C, '--mcp-config: the absolute path of a JSON file.', { placeholder: '/srv/projects/…/mcp.json' });
  const argsC = textField('Extra args', 'args', C, null, { placeholder: 'anything else, e.g. --verbose' });
  const argsX = textField('Extra args', 'args', X, null, { placeholder: 'anything else' });
  const sibF = sib.length ? dirsField('Also give access to', sib, '--add-dir') : null;
  const sibX = sib.length ? dirsField('Also give write access to', sib, '--add-dir') : null;
  const otherF = others.length ? el('details', { class: 'lx-others' }, el('summary', { text: `Repos of other projects (${others.length})` }), dirsField('Also give access to', others, '--add-dir')) : null;
  const cfgArea = area(() => X.config, (x) => { X.config = x; fieldError(cfgF, ''); }, { rows: '3', placeholder: 'one key=value per line, e.g. model_verbosity=low' });
  const cfgF = field('Config overrides', cfgArea, '-c key=value, one per line. Keys the board manages are refused.');
  const searchChk = check('Live web search', () => X.search, (x) => { X.search = x; }, '--search');
  /* An advanced field exists while the agent's schema lists its key (the answer of GET /api/agents may arrive after the sheet opened: visible() follows it); a task or a dispatch takes the tools,
     the system prompt, the directories and the extra args, the rest (a worktree flag, a fork, a devcontainer, --agent ...) is a session's. gate(node, agent, key, sessionOnly) -> node. */
  const gated = [];
  const gate = (n, agent, key, so) => { gated.push([n, agent, key, !!so]); return n; };
  const devRow = checksRow(devc);
  const claudeAdv = el('details', { class: 'tf-options lx-adv', 'data-agent': 'claude' }, el('summary', { text: 'Advanced' }),
    el('div', { class: 'grid' }, gate(allowedF, 'claude', 'allowed_tools'), gate(disallowedF, 'claude', 'disallowed_tools')),
    gate(toolsF, 'claude', 'tools', true), gate(sysF, 'claude', 'append_system_prompt'),
    gate(el('div', { class: 'grid' }, gate(agentF, 'claude', 'agent_name'), gate(fallbackF, 'claude', 'fallback_model')), 'claude', null, true),      // pairs, so no field is left alone on a row of the 2-column grid
    gate(el('div', { class: 'grid' }, gate(compactF, 'claude', 'autocompact'), gate(mcpF, 'claude', 'mcp_config')), 'claude', null, true),
    gate(cWt.wrap, 'claude', 'worktree', true), gate(forkRow, 'claude', 'fork_session'),
    sibF, otherF, gate(devRow, 'claude', 'devcontainer', true),
    argsC);
  if (!r.devcontainer) gate(devRow, 'claude', '__never', true);
  const xWtBox = el('div', {}, xWt.wrap, el('p', { class: 'dim lx-hint', text: 'Codex has no worktree flag: the board makes a git worktree under .ccboard/worktrees and starts Codex in it.' }));
  const searchRow = checksRow(searchChk);
  const codexAdv = el('details', { class: 'tf-options lx-adv', 'data-agent': 'codex' }, el('summary', { text: 'Advanced' }),
    gate(searchRow, 'codex', 'search'), gate(xWtBox, 'codex', 'worktree', true),
    sibX, gate(cfgF, 'codex', 'config', true), argsX);
  gate(fast.node, 'claude', 'fast');
  const shellDev = check('Run in the devcontainer', () => V.shell.devcontainer, (x) => { V.shell.devcontainer = x; }, 'devcontainer up + devcontainer exec');
  const shellBox = el('div', { class: 'lx-agentbox', 'data-agent': 'shell' }, el('p', { class: 'dim lx-lede', text: `A plain shell in ${r.root ? 'the project folder' : r.name}: no agent, just a terminal.` }), r.devcontainer ? checksRow(shellDev) : null);

  /* ---- the panels ---- */
  const claudeBasic = el('div', { class: 'lx-agentbox', 'data-agent': 'claude' }, modelField, effortField, switchRow, permField);
  const codexBasic = el('div', { class: 'lx-agentbox', 'data-agent': 'codex' }, cxModelField, reasoningField, modeField, customRow);

  /* ---- the danger gate ---- */
  const dangerText = el('span', { class: 'lx-danger-t' });
  const ack = el('input', { type: 'checkbox' });
  ack.addEventListener('change', () => { fieldError(ackRow, ''); update(); });
  const ackErr = el('span', { class: 'field-err bad', role: 'alert' });
  const ackRow = el('div', { class: 'lx-ack' }, el('div', { class: 'checks' }, el('label', {}, ack, LX_BYPASS_ACK)), ackErr);
  ackRow.errNode = ackErr;
  ackRow.target = ack;
  const dangerBox = el('div', { class: 'lx-danger hidden', role: 'alert' }, ic('warning-sign'), el('div', { class: 'lx-danger-b' }, dangerText, ackRow));
  const acked = () => launcherAcked(p.name, r.name);

  /* ---- the command ---- */
  let cmdText = '';
  let truth = '';                                                                // the response's cmd, shown in place of the approximation until a value changes
  const cmdNode = el('pre', { class: 'lx-cmd', 'aria-label': 'Command preview' });
  const paintCmd = () => { cmdNode.textContent = ''; cmdNode.append(...lxCmdNodes(cmdText)); };           // each token in its own span (see lxCmdNodes); cmdText stays the plain line Copy takes
  const copyBtn = el('button', { class: 'minimal small lx-copy', type: 'button', text: 'Copy', onclick: async () => {
    try { if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(cmdText); else throw new Error('no clipboard'); toast('Copied', { kind: 'ok', ttl: 1800 }); }
    catch (_) { toast('Could not copy: select the text instead', { kind: 'warn' }); }
  } });
  const cmdBox = el('div', { class: 'lx-cmdbox' }, el('div', { class: 'lx-cmdhead' }, el('h3', { class: 'lx-k', text: 'Command' }), copyBtn), cmdNode,
    el('p', { class: 'dim lx-hint', text: 'Approximate: what the board runs is shown after it starts.' }));

  /* ---- dispatch mode: a new session or a running one ---- */
  const ready = mode === 'dispatch' ? taskSessionTargets(task).filter((x) => x.ok) : [];
  const D = { where: preset.session ? 'session' : 'lane', session: preset.session || (ready[0] ? ready[0].s.tmux : ''), autoClose: typeof preset.auto_close === 'boolean' ? preset.auto_close : null };
  const whereSeg = lxSeg([['lane', 'New session'], ['session', 'Running session']], (w) => { D.where = w; whereSeg.set(w); edit(); }, 'Where', 'lx-where');
  const sessSel = selectEl(ready.length ? ready.map((x) => [x.s.tmux, `${x.s.name} · ${x.repo === 'root' ? 'project folder' : x.repo}${x.same ? '' : ' (other repo)'}`]) : [['', '(no session is ready)']], D.session);
  sessSel.addEventListener('change', () => { D.session = sessSel.value; });
  const sessField = field('Session', sessSel, ready.length ? 'The prompt is pasted into it; a session in another repo asks first.' : 'Every session of this project is busy or waiting on a prompt.');
  function autoValue() { return mode === 'dispatch' ? (D.autoClose === null ? D.where === 'lane' : D.autoClose) : T.autoClose; }

  /* ---- task mode: run when, title, issue, chain, close when finished ---- */
  const T = { when: ['now', 'later', 'schedule'].includes(carry.when || opt.when) ? (carry.when || opt.when) : (() => { const s = lxJson(lxGet(LX_TASK_KEY(p.name, r.name, 'claude'))); return s && (s.when === 'now' || s.when === 'later') ? s.when : 'now'; })(),
    title: String(carry.title || ''), autoClose: true, cron: carry.cron || '', jobMode: 'acceptEdits', turns: '30', budget: '', schedName: String(carry.name || '') };
  const taskSaved = lxJson(lxGet(LX_TASK_KEY(p.name, r.name, 'claude'))) || {};
  if (taskSaved.auto_close === false) T.autoClose = false;
  if (JOB_MODES.includes(taskSaved.job_mode)) T.jobMode = taskSaved.job_mode;
  if (taskSaved.max_turns) T.turns = String(taskSaved.max_turns);
  let nameTyped = !!carry.name;
  const whenSeg = lxSeg(TASK_WHEN, (w) => { T.when = w; whenSeg.set(w); paintAgentSeg(); edit(); focusFine(promptEl); }, 'Run', 'lx-when');      // a mouse moves on to the prompt (an arrow key's own focus move runs after this and wins); touch raises no keyboard
  const lede = el('p', { class: 'dim tf-lede' });
  const titleIn = text(() => T.title, (x) => { T.title = x; if (T.when === 'schedule' && !nameTyped) autoName(); }, { maxlength: 120, placeholder: 'e.g. Fix the login redirect' });
  const titleField = field('Title', titleIn, 'Optional: the first line of the prompt is used.');
  const ISSUE_KEYS = { claude: ['model', 'model_custom', 'effort', 'permission_mode'], codex: ['model', 'model_custom', 'reasoning', 'cx_mode', 'sandbox', 'approval'] };
  let issueSnap = null;                                                              // what the issue's model line replaced, for Reset to my defaults and for what is remembered
  const issueKit = launcherIssuePicker(p, r, {
    scope: { agents: agentsHere, installed: lxInstalled },
    fill: (i) => {
      T.title = `#${i.number} ${i.title}`.slice(0, 120);
      V.common.prompt = `${i.title}\n\n${i.body || ''}\n\nGitHub issue: ${i.url}\nWhen done, commit with a message that includes "Closes #${i.number}".`;
      titleIn.value = T.title;
      promptEl.value = V.common.prompt;
      composerGrow(promptEl);
      update();
    },
    apply: (ch) => {
      issueSnap = { agent: V.agent, claude: {}, codex: {} };
      for (const ag of ['claude', 'codex']) for (const k of ISSUE_KEYS[ag]) issueSnap[ag][k] = V[ag][k];
      if (ch.claude) { Object.assign(C, ch.claude); if (ch.claude.model) C.model_custom = ''; }
      if (ch.codex) { Object.assign(X, ch.codex); if (ch.codex.model) X.model_custom = ''; }
      V.agent = ch.agent;
      reasoningFix();
      paint();
    },
    restore: () => {
      const snap = issueSnap;
      issueSnap = null;
      if (!snap) return;
      for (const ag of ['claude', 'codex']) for (const k of ISSUE_KEYS[ag]) { if (snap[ag][k] === undefined) delete V[ag][k]; else V[ag][k] = snap[ag][k]; }
      V.agent = snap.agent;
      reasoningFix();
      paint();
    },
    changed: () => update(),
  });
  const issueSel = issueKit.select;
  issueKit.node._labelFor = issueSel;
  const issueField = field('From a GitHub issue', issueKit.node);
  const chain = mode === 'task' ? chainBuilder() : null;
  const autoChk = check('Close the session', () => autoValue(), (x) => { if (mode === 'dispatch') D.autoClose = x; else T.autoClose = x; });
  const autoField = field('When it finishes', checksRow(autoChk),
    'The session closes itself about a minute after the task stops, unless it asked you something. Keep it open from the card.');
  const nameEl = text(() => T.schedName, (x) => { T.schedName = x; nameTyped = true; }, { maxlength: 80, placeholder: 'name (e.g. nightly-tests)' });
  const autoName = () => { if (!nameTyped) { T.schedName = T.title.trim() || (V.common.prompt.trim() ? taskTitleFrom(V.common.prompt) : ''); nameEl.value = T.schedName; } };
  const cronEl = text(() => T.cron, (x) => { T.cron = x; syncCron(); }, { placeholder: 'cron: 30 2 * * *  (blank = once, now)' });
  const cronNote = el('div', { class: 'dim tf-cronnote' });
  const presetCron = CRON_PRESETS.map(([label, value]) => el('button', { class: 'chip-btn', type: 'button', text: label, 'aria-pressed': 'false', 'data-cron': value, title: value ? `cron ${value}` : 'blank cron: run once, now',
    onclick: () => { T.cron = value; cronEl.value = value; syncCron(); } }));
  const syncCron = () => { const v = T.cron.trim(); for (const b of presetCron) b.setAttribute('aria-pressed', b.getAttribute('data-cron') === v ? 'true' : 'false'); cronNote.textContent = cronNoteText(v); };
  const jobModeSel = pick(JOB_MODES.map((m) => [m, m]), () => T.jobMode, (x) => { T.jobMode = x; });
  const turnsIn = text(() => T.turns, (x) => { T.turns = x; }, { type: 'number', min: '1', max: '500', inputmode: 'numeric' });
  const budgetIn = text(() => T.budget, (x) => { T.budget = x; }, { type: 'number', step: '0.5', min: '0', inputmode: 'decimal', placeholder: 'optional' });
  const turnsF = field('Max turns', turnsIn);
  const budgetF = field('Max $', budgetIn, 'Optional.');
  const codexSchedNote = el('p', { class: 'dim lx-hint hidden', text: 'Codex runs in a workspace-write sandbox (plan: read-only) and never stops to ask. It has no turn or budget limit: a run ends when the task does.' });
  const schedBox = el('div', { class: 'tf-schedule' }, field('Name', nameEl, 'Shown on the task card.'), field('Cron', cronEl), el('div', { class: 'chips cron-presets', role: 'group', 'aria-label': 'Cron presets' }, presetCron), cronNote,
    el('div', { class: 'grid' }, field('Permission mode', jobModeSel), turnsF, budgetF), codexSchedNote);
  const when = () => (mode === 'task' ? T.when : 'now');

  /* ---- targets (the repo select of the task form) ---- */
  const targets = Array.isArray(opt.targets) ? opt.targets : (mode === 'task' && typeof taskTargets === 'function' ? taskTargets(p) : []);      // the places a task can start in this project
  const here = Math.max(0, targets.findIndex((x) => x.p === p && x.r === r));
  const whereRepo = targets.length > 1 ? selectEl(targets.map((x, i) => [String(i), x.label]), String(here)) : null;
  if (whereRepo) whereRepo.addEventListener('change', () => {
    const x = targets[parseInt(whereRepo.value, 10)];
    const carryOn = { title: T.title, prompt: V.common.prompt, when: T.when, name: T.schedName, cron: T.cron };
    if (x && typeof opt.onTarget === 'function') opt.onTarget(x, carryOn);
    else if (x) openLauncher({ ...opt, project: x.p, repo: x.r, carry: carryOn, label: x.label, targets: opt.targets });
  });

  /* ---- the account line and the footer ---- */
  const acctText = el('span', { class: 'lx-acct-t dim' });
  const acctBtn = el('button', { class: 'minimal small lx-switch', type: 'button', text: 'Switch first' });
  const acctRow = el('div', { class: 'lx-acct hidden' }, acctText, acctBtn);
  let acct = null;
  const paintAccount = () => {
    acct = V.agent === 'shell' || (mode === 'dispatch' && D.where === 'session') ? null : launcherAccount(V.agent);
    hide(acctRow, !acct);
    if (!acct) return;
    acctText.textContent = acct.text;
    hide(acctBtn, !acct.run);
    if (acct.run) acctBtn.setAttribute('title', `Switch to ${acct.switchTo.label || acct.switchTo.name || acct.switchTo.email || 'the other account'} first, then start`);
  };
  acctBtn.addEventListener('click', async () => {
    if (!acct || !acct.run) return;
    lxDisable(acctBtn, true);
    try { await acct.run(); } finally { lxDisable(acctBtn, false); paintAccount(); }
  });
  const goText = el('span', { class: 'tf-go-text' });
  const goIcon = el('span', { class: 'tf-go-ic' });
  const go = el('button', { class: 'primary', type: 'submit', title: 'Enter in the prompt starts it' }, goIcon, goText);
  const cancel = el('button', { type: 'button', text: 'Cancel', onclick: () => { if (typeof opt.onCancel === 'function') opt.onCancel(); else { ui.openForm = null; closeSheet(); } } });
  const foot = el('div', { class: 'submit lx-foot' }, acctRow, go, cancel);

  /* ---- what shows ---- */
  function visible() {
    const a = V.agent;
    const sch = when() === 'schedule';
    const launch = V.common.launch;
    hide(claudeBasic, a !== 'claude' || sch); hide(codexBasic, a !== 'codex');
    hide(claudeAdv, a !== 'claude' || sch); hide(codexAdv, a !== 'codex' || sch);
    hide(shellBox, a !== 'shell');
    hide(modeField, sch); hide(customRow, sch || X.cx_mode !== 'custom');                  // a schedule's permission mode is the schedule's own (Codex: plan = read-only, else workspace-write)
    hide(turnsF, a === 'codex'); hide(budgetF, a === 'codex'); hide(codexSchedNote, a !== 'codex');
    hide(presetField, a === 'shell' || sch || (mode === 'dispatch' && D.where !== 'lane'));
    hide(launchField, !session || a === 'shell');
    hide(resumeField, !session || a === 'shell' || launch !== 'resume' && !(a === 'codex' && launch === 'fork'));
    hide(prField, !session || a !== 'claude' || launch !== 'from_pr');
    hide(nameField, !session);
    hide(switchRow, !session);
    for (const [n, ag, key, so] of gated) hide(n, (key !== null && !lxHas(ag, key)) || (so && !session));
    /* the launch kind decides these two, after the schema gate above (which would show them again): a fork copies a conversation that resume or continue picks up, a worktree is a new session's */
    if (!(launch === 'resume' || launch === 'continue') || a !== 'claude') hide(forkRow, true);
    if (launch !== 'new') { hide(cWt.wrap, true); hide(xWtBox, true); }
    hide(customRow, sch || X.cx_mode !== 'custom');
    hide(promptField, a === 'shell' || (session && launch !== 'new') || (mode === 'dispatch'));
    hide(titleField, mode !== 'task' || sch); hide(issueField, mode !== 'task' || sch);
    if (chain) hide(chain.node, sch || mode !== 'task');
    hide(autoField, mode === 'session' || sch);
    hide(schedBox, !sch);
    hide(cmdBox, (a === 'shell' && !r.devcontainer) || sch);
    if (mode === 'dispatch') { hide(sessField, D.where !== 'session'); hide(agentField, D.where !== 'lane'); hide(claudeBasic, D.where !== 'lane' || a !== 'claude'); hide(codexBasic, D.where !== 'lane' || a !== 'codex'); hide(claudeAdv, true); hide(codexAdv, true); hide(cmdBox, D.where !== 'lane'); }
    lede.textContent = mode === 'task' ? (T.when === 'schedule' && a === 'codex' ? TASK_LEDE_CODEX_SCHEDULE : TASK_LEDE[T.when]) : '';
    hide(lede, mode !== 'task');
  }

  update = () => {
    const v = view();
    cmdText = truth || commandPreview(v, ctx());
    paintCmd();
    for (const x of presetBtns) x.b.setAttribute('aria-pressed', presetOn(x.pr) ? 'true' : 'false');
    const d = launcherDanger(v, mode);
    hide(dangerBox, !d);
    dangerText.textContent = v.agent === 'codex' ? BYPASS_WARNING_CODEX : BYPASS_WARNING;
    hide(ackRow, !d || acked());
    const issueHeld = mode === 'task' && issueKit.blocked();
    const gated = (d && !acked() && !ack.checked) || issueHeld;
    const w = when();
    if (mode === 'session') { goText.textContent = busy ? 'Starting…' : 'Start & open'; }
    else if (mode === 'task') goText.textContent = busy ? TASK_BUSY[w] : TASK_SUBMIT[w];
    else goText.textContent = busy ? 'Starting…' : (D.where === 'lane' ? `Start in ${AGENT_NAME[v.agent] || 'a new session'}` : 'Send to the session');
    goIcon.textContent = '';
    goIcon.append(ic(mode === 'task' ? LX_TASK_ICON[w] : 'play'));
    lxDisable(go, busy || gated || (mode === 'dispatch' && D.where === 'session' && !ready.length));
    go.setAttribute('title', issueHeld ? 'Tick I have read it, under the issue, to go on' : gated ? 'Tick I understand to go on' : 'Enter in the prompt starts it');
    if (mode !== 'session') autoChk.input.checked = autoValue();
    paintAccount();
  };

  const paintAgent = () => {
    paintAgentSeg();
    paintLaunch();
    paintPresets();
    paintReasoning();
    visible();
    update();
  };
  const paint = () => {
    for (const f of painters) f();
    if (mode === 'task') { whenSeg.set(T.when); syncCron(); }
    if (mode === 'dispatch') whereSeg.set(D.where);
    paintAgentSeg();
    paintLaunch();
    paintPresets();
    visible();
    update();
  };
  const agentField = field('Agent', el('div', { class: 'lx-agentctl' }, agentSeg.node, agentWhy));
  const whenField = field('Run', whenSeg.node);
  const whereField = field('Where', whereSeg.node);

  let busy = false;
  const setBusy = (on) => { busy = on; update(); };
  const wipe = () => { for (const f of [promptField, nameField, resumeField, prField, argsC, cfgF, ackRow, modelField, effortField, issueField]) fieldError(f, ''); formStatus(status, ''); };
  const failAt = (f, msg) => { const d = f && f.closest ? f.closest('details') : null; if (d) d.setAttribute('open', ''); fieldError(f, msg, true); };

  /* ---- submit ---- */
  const remember = (v0, w) => {
    const v = issueSnap && issueKit.applied() && issueSnap[v0.agent] ? { ...v0, ...issueSnap[v0.agent] } : v0;      // a model line read from an issue is never remembered as the person's default
    const keep = launcherRemember(v);
    if (session) {
      launcherSaveAll(p.name, r.name, v.agent, keep);
    } else if (v.agent === 'claude' || v.agent === 'codex') {
      const flat = { ...keep };
      const m = lxModelOf(v);
      if (m) flat.model = m; else delete flat.model;
      delete flat.model_custom;
      if (v.agent === 'claude') { if (v.effort) flat.effort = v.effort; else delete flat.effort; }
      else if (v.reasoning) flat.reasoning_effort = v.reasoning;
      const base = lxJson(lxGet(LX_TASK_KEY(p.name, r.name, v.agent))) || {};
      const next = { ...flat };
      if (mode === 'task') { next.when = w === 'schedule' ? base.when : w; next.auto_close = T.autoClose; next.cron = T.cron.trim(); next.job_mode = T.jobMode; next.max_turns = parseInt(T.turns, 10) || 30; }
      else for (const k of ['when', 'auto_close', 'cron', 'job_mode', 'max_turns']) if (base[k] !== undefined) next[k] = base[k];
      lxPut(LX_TASK_KEY(p.name, r.name, v.agent), JSON.stringify(next));
    }
  };

  const openIt = (tmux, tab) => {
    const url = '/term/' + encodeURIComponent(tmux);
    if (tab) { tab.location = url; return; }
    if (typeof Shell !== 'undefined' && Shell && typeof Shell.openTerm === 'function') Shell.openTerm(tmux); else openPage(url);
  };
  const finish = (res, w) => {
    setError(null);
    if (typeof opt.onDone === 'function') opt.onDone(res, w); else { ui.openForm = null; closeSheet(); }
    if (typeof poll === 'function') poll(true);
  };

  const submitSession = async (v) => {
    const body = launcherPayload(v, ctx());
    remember(v);
    const useDock = typeof Shell !== 'undefined' && Shell && typeof Shell.dockOn === 'function' && Shell.dockOn() && !(typeof Shell.dockHeld === 'function' && Shell.dockHeld());
    const quiet = opt.open === false;                                    // the caller shows the session itself (the quad's empty tile: onDone puts it there)
    const tab = !quiet && !useDock && !isStandalone() && typeof window.open === 'function' ? window.open('', '_blank') : null;     // opened on the tap: a popup blocker lets it through
    let res;
    try { res = await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/sessions`, body); }
    catch (err) {
      if (tab) tab.close();
      formFail(status, [[/resume id|resume/i, resumeField], [/pull request|from.pr/i, prField], [/extra args|settings overrides|argument/i, argsC], [/session .*(exists|name)|name .*(invalid|allowed)|reserved/i, nameField],
        [/model/i, modelField], [/config|-c /i, cfgF]], err.message);
      return;
    }
    if (res && typeof res.cmd === 'string' && res.cmd) { truth = res.cmd; cmdText = truth; paintCmd(); }       // the truth replaces the approximation
    if (res && res.tmux && !quiet) openIt(res.tmux, tab); else if (tab) tab.close();
    if (res && res.tmux && v.agent === 'claude') {                      // what has no flag is typed once the session is at its prompt
      if (v.ultracode && !ultraNative()) launcherAfterStart(res.tmux, 'effort', 'ultracode on');
      if (v.fast && lxHas('claude', 'fast')) launcherAfterStart(res.tmux, 'fast');
    }
    finish(res, 'now');
  };

  const taskBase = (v) => ({ project: p.name, repo: r.name, agent: v.agent, ...launcherTaskOpts(v), add_dirs: v.add_dirs });
  const submitTaskNow = async (v, w, prompt, title) => {
    const body = { ...taskBase(v), title, prompt, when: w, ...issueKit.link() };
    if (!T.autoClose) body.auto_close = false;
    for (const k of Object.keys(body)) if (body[k] === '' || body[k] === null || (Array.isArray(body[k]) && !body[k].length)) delete body[k];
    remember(v, w); taskSaveLastRepo(p.name, r.name);
    const res = (await api('POST', '/api/tasks', body)) || {};
    const demo = typeof demoOn === 'function' && demoOn();
    const base = { id: res.id !== undefined && res.id !== null ? res.id : -Date.now(), project: p.name, repo: r.name, slug: res.slug || '', title, branch: '', base: '', worktree: '', tmux: '', created_at: new Date().toISOString(),
      column: 'backlog', phase: 'backlog', mode: 'worktree', agent: v.agent, auto_close: false, parent_id: null, chain_id: null, session_row: null, session: null, result: null, prompt: prompt.slice(0, 600), prompt_len: prompt.length,
      claude_session_id: null, pr_url: null, pr_number: null, pr_state: null, cost_usd: null, overlap: [], ci: null, pr: null };
    if (w === 'now') {
      const row = taskRowFromResponse(base, res, { mode: 'worktree' });
      if (!row.tmux && demo) row.tmux = `${p.name}--${r.name}--t-${row.slug || 'task'}`;
      taskOverrideSet(row, null, { _new: true });
      toast(`started ${row.slug || title}`, { kind: 'ok' });
      taskRepaint();
      finish(res, w);
      taskOpenPeek(row.tmux);
    } else {
      const row = { ...base, ...(res.task && typeof res.task === 'object' ? res.task : {}), phase: 'backlog', column: 'backlog', tmux: '' };
      taskOverrideSet(row, null, { _new: true });
      toast(`added to backlog: ${title}`.slice(0, 120), { kind: 'ok' });
      taskRepaint();
      finish(res, w);
    }
  };
  const submitChainNow = async (v, w, prompt, title, more) => {
    const first = {};
    for (const [k, x] of Object.entries({ ...launcherTaskOpts(v), add_dirs: v.add_dirs })) if (x && !(Array.isArray(x) && !x.length)) first[k] = x;
    const steps = [{ title, prompt, agent: v.agent, ...first }, ...more.map((s) => ({ title: s.title || taskTitleFrom(s.prompt), prompt: s.prompt, agent: s.agent }))];
    const body = { steps, dispatch: w === 'now' };
    if (!T.autoClose) body.auto_close = false;
    remember(v, w); taskSaveLastRepo(p.name, r.name);
    const res = (await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/chains`, body)) || {};
    const demo = typeof demoOn === 'function' && demoOn();
    const chainId = res.chain_id !== undefined && res.chain_id !== null ? res.chain_id : (demo ? `demo-${Date.now()}` : null);
    const ids = Array.isArray(res.ids) && res.ids.length === steps.length ? res.ids : (demo ? steps.map((_, i) => -(Date.now() + i)) : null);
    let opened = '';
    const started = res.started && typeof res.started === 'object' ? res.started : res;
    const given = Array.isArray(res.tasks) && res.tasks.length === steps.length ? res.tasks : [];
    if (ids) {
      steps.forEach((s, i) => {
        const base = { id: ids[i], project: p.name, repo: r.name, slug: '', title: s.title, branch: '', base: '', worktree: '', tmux: '', created_at: new Date().toISOString(), column: 'backlog',
          phase: i ? 'queued' : 'backlog', mode: 'worktree', agent: s.agent, auto_close: T.autoClose, parent_id: i ? ids[i - 1] : null, chain_id: chainId, session_row: null, session: null, result: null,
          prompt: s.prompt.slice(0, 600), prompt_len: s.prompt.length, claude_session_id: null, pr_url: null, pr_number: null, pr_state: null, cost_usd: null, overlap: [], ci: null, pr: null,
          ...(given[i] && typeof given[i] === 'object' ? given[i] : {}) };
        if (i === 0 && w === 'now') {
          const row = taskRowFromResponse(base, { tmux: started.tmux, session_row: started.session_row, slug: started.slug, branch: started.branch, task: started.task }, { mode: 'worktree' });
          if (!row.tmux && demo) row.tmux = `${p.name}--${r.name}--t-chain-${Math.abs(ids[0])}`;
          opened = row.tmux || '';
          taskOverrideSet(row, null, { _new: true });
        } else taskOverrideSet(base, null, { _new: true });
      });
    }
    toast(w === 'now' ? `started a chain of ${steps.length} steps` : `added a chain of ${steps.length} steps to the backlog`, { kind: 'ok' });
    taskWarn(res);
    taskRepaint();
    finish(res, w);
    if (opened) taskOpenPeek(opened);
  };
  const submitJobNow = async (v, prompt, title) => {
    const name = (T.schedName.trim() || title).slice(0, 80);
    const c = T.cron.trim();
    const codex = v.agent === 'codex';
    const body = { name, prompt, permission_mode: T.jobMode, run_now: !c };
    if (c) body.cron = c;
    if (codex) {                                                       // Codex: its model and reasoning are the job's options; it has no turn or budget limit
      body.agent = 'codex';
      const m = lxModelOf(v);
      if (m) body.model = m;
      if (v.reasoning) body.reasoning_effort = v.reasoning;
    } else {
      body.max_turns = parseInt(T.turns, 10) || 30;
      if (T.budget) body.max_budget_usd = parseFloat(T.budget);
      if (String(C.args || '').trim()) body.args = String(C.args).trim();
    }
    if (codex) remember(v, 'schedule');                                // the model, reasoning, cron and mode go under the Codex task key, as a Codex task's do
    else lxPut(LX_TASK_KEY(p.name, r.name, 'claude'), JSON.stringify({ ...taskSaved, cron: c, job_mode: T.jobMode, max_turns: parseInt(T.turns, 10) || 30 }));
    const res = await api('POST', `/api/projects/${encodeURIComponent(p.name)}/repos/${encodeURIComponent(r.name)}/jobs`, body);
    toast(c ? `scheduled ${name}` : `running ${name} once`, { kind: 'ok' });
    finish(res, 'schedule');
  };
  const submitDispatch = (v) => {
    if (D.where === 'session') {
      const x = ready.find((q) => q.s.tmux === D.session);
      if (!x) { formStatus(status, 'Pick a session that is ready.', true); return; }
      closeSheet();
      taskSend(task, x, false, autoValue() ? { auto_close: true } : undefined);
      return;
    }
    const extra = launcherTaskOpts(v);
    remember(v);
    closeSheet();
    taskStart(task, { agent: v.agent, auto_close: autoValue() ? undefined : false, extra });
  };

  const submit = async () => {
    if (busy) return;
    wipe();
    V.common.prompt = promptEl.value;
    const v = view();
    const w = when();
    const errs = (f, m) => { failAt(f, m); };
    if (mode === 'session' && v.agent !== 'shell') {
      if (v.launch === 'from_pr' && !LX_PR.test(String(v.from_pr).trim())) return errs(prField, 'Give a pull request number (123 or #123) or its URL.');
      if (v.agent === 'claude' && String(v.autocompact || '').trim() && !/^(auto|\d{4,9})$/.test(String(v.autocompact).trim())) return errs(compactF, 'auto, or a context size such as 150000.');
      if (v.agent === 'claude' && v.launch === 'resume' && v.resume_id && !LX_UUID.test(v.resume_id)) return errs(resumeField, 'A session id looks like 8-4-4-4-12 hex digits.');
    }
    if (v.agent === 'codex' && w !== 'schedule') {
      const bad = String(v.config || '').split('\n').map((x) => x.trim()).filter(Boolean).find((x) => !LX_CONFIG_RE.test(x));
      if (bad) return errs(cfgF, `"${bad}" is not key=value (letters, digits, _ and . before the =).`);
    }
    if (launcherDanger(v, mode) && !acked() && !ack.checked) return errs(ackRow, 'Tick I understand to start without approvals.');
    if (mode === 'task' && issueKit.blocked()) return errs(issueField, 'Tick I have read it to start from this issue.');
    if (mode === 'dispatch') { submitDispatch(v); return; }
    if (mode === 'task') {
      const prompt = V.common.prompt.trim();
      if (!prompt) return errs(promptField, 'Write what it should do.');
      const title = T.title.trim() || taskTitleFrom(prompt);
      const steps = w === 'schedule' ? [] : chain.read();
      if (w !== 'schedule' && chain.problem()) { chain.node.setAttribute('open', ''); return; }
      if (w !== 'schedule' && /bypassPermissions|dangerously-skip-permissions/i.test(String(v.args || ''))) return errs(v.agent === 'claude' ? argsC : argsX, 'bypassPermissions is not allowed for tasks or schedules; start a session and choose bypass there if you really want it.');
      setBusy(true);
      try {
        if (w === 'schedule') await submitJobNow(v, prompt, title);
        else if (steps.length) await submitChainNow(v, w, prompt, title, steps);
        else await submitTaskNow(v, w, prompt, title);
      } catch (err) { formStatus(status, err.message, true); setError(err.message); }
      setBusy(false);
      return;
    }
    if (launcherDanger(v, mode)) launcherAck(p.name, r.name);
    setBusy(true);
    try { await submitSession(v); } catch (err) { formStatus(status, err.message, true); setError(err.message); }
    setBusy(false);
  };

  composerBind(promptEl, { maxRows: 10, onSend: () => submit() });                 // Enter starts, Shift+Enter (or the newline button) adds a line, Cmd/Ctrl+Enter starts too
  const syncPrompt = () => { if (V.common.prompt !== promptEl.value) { V.common.prompt = promptEl.value; if (T.when === 'schedule') autoName(); edit(); } };   // a newline typed by script fires no input event
  promptEl.addEventListener('input', () => { fieldError(promptField, ''); syncPrompt(); });
  promptEl.addEventListener('keyup', syncPrompt);
  nlBtn.addEventListener('click', syncPrompt);

  const memHost = mode === 'session' ? el('div', { class: 'lx-mem hidden' }) : null;          // the project's newest claude-mem gotchas (v0.5.20): openLauncher fills it after the sheet has painted
  const form = el('form', { class: 'form lx-form' + (mode === 'session' ? '' : ' task-form'), onsubmit: (e) => { e.preventDefault(); submit(); } },
    whereRepo ? field('Repo', whereRepo) : null,
    memHost,
    mode === 'task' ? [whenField, lede] : null,
    mode === 'dispatch' ? whereField : null,
    mode === 'dispatch' ? sessField : null,
    agentField,
    presetField,
    launchField, resumeField, prField,
    nameField,
    claudeBasic, codexBasic, shellBox,
    dangerBox,
    promptField, titleField, issueField, chain ? chain.node : null, schedBox,
    claudeAdv, codexAdv,
    autoField,
    cmdBox,
    status,
    foot);
  form.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !e.isComposing) { e.preventDefault(); if (typeof e.stopPropagation === 'function') e.stopPropagation(); cancel.click(); } });
  form.focusFirst = () => focusFine(promptEl);

  paintModelOptions(); paintEffortItems(); paintPermOptions(); paintCxModels(); paintModeItems();
  paint();
  if (mode === 'task') syncCron();

  /* the schema may arrive after the sheet opened: the model lists, the efforts, the permission modes and the reasoning levels follow it */
  const refreshSchema = () => {
    paintModelOptions(); paintEffortItems(); paintPermOptions(); paintCxModels(); paintModeItems();
    paintModel(); reasoningFix(); paint();
  };
  const ctl = { form, V, view, schema, refreshSchema, paint, submit, go, status, acct: () => acct, memHost,
    preview: () => cmdText, payload: () => launcherPayload(view(), ctx()), T, D, busy: () => busy };
  form._launcher = ctl;
  return ctl;
}

/* Everything remembered by a session launch: the agent's own memory, the agent last used here, and the project's defaults (what a repo with no memory of its own starts from). */
function launcherSaveAll(pname, rname, agent, keep) {
  if (agent === 'claude' || agent === 'codex') lxPut(LX_KEY(pname, rname, agent), JSON.stringify(keep));
  lxPut(LX_AGENT_KEY(pname, rname), agent);
  const d = lxJson(lxGet(LX_DEFAULTS_KEY(pname))) || {};
  d.agent = agent;
  if (agent === 'claude' || agent === 'codex') {
    const light = {};
    for (const k of agent === 'claude' ? ['model', 'model_custom', 'effort', 'permission_mode'] : ['model', 'model_custom', 'reasoning', 'cx_mode']) if (keep[k] !== undefined) light[k] = keep[k];
    d[agent] = light;
  }
  lxPut(LX_DEFAULTS_KEY(pname), JSON.stringify(d));
}

/* A slash command typed once the new session is at its prompt (POST /api/sessions/{tmux}/command; the board's /command refuses with 409 until then): `/effort ultracode on` and `/fast`
   have no flag to carry them. Tried for about 20 seconds, then said. Two of them for one session go one after the other (the second starts when the first has landed or given up), so
   they never type over each other. cmd is the command's name (effort, fast), arg its argument or nothing (the /fast SlashSpec takes none). */
const LX_AFTER = new Map();
function launcherAfterStart(tmux, cmd, arg) {
  const typed = `/${cmd}${arg ? ' ' + arg : ''}`;
  const body = arg ? { cmd, arg } : { cmd };
  const once = () => new Promise((done) => {
    let tries = 0;
    const later = (fn, ms) => { const t = setTimeout(fn, ms); if (t && typeof t.unref === 'function') t.unref(); };
    const attempt = async () => {
      tries += 1;
      try { await api('POST', `/api/sessions/${encodeURIComponent(tmux)}/command`, body); done(); return; }
      catch (e) {
        if (e && e.status === 409 && tries < 10) { later(attempt, 2000); return; }
        toast(`${typed} was not applied: type it in the terminal`, { kind: 'warn' });
        done();
      }
    };
    later(attempt, 1500);
  });
  const next = (LX_AFTER.get(tmux) || Promise.resolve()).then(once);
  LX_AFTER.set(tmux, next);
  next.then(() => { if (LX_AFTER.get(tmux) === next) LX_AFTER.delete(tmux); });
}

/* Open the launcher in the sheet. See the comment at the top of this section for the options. Returns the controller (launcherForm's), or false when the project or repo is unknown (or a dispatch has no task). */
function openLauncher(o) {
  const opt = o || {};
  const ctl = launcherForm(opt);
  if (!ctl) return false;                                               // launch() (components.js) reads false as 'nothing opened'
  const rp = lxResolve(opt.project, opt.repo);
  const label = typeof opt.label === 'string' && opt.label ? opt.label : (rp.r.root ? `${rp.p.name} · project folder` : `${rp.p.name}/${rp.r.name}`);
  const mode = opt.mode === 'task' || opt.mode === 'dispatch' ? opt.mode : 'session';
  const title = mode === 'task' ? `New task · ${label}` : mode === 'dispatch' ? `Start “${String(opt.task.title).slice(0, 60)}”` : `New session · ${label}`;
  const holder = el('div', { class: 'sheet-form' }, ctl.form);
  ui.openForm = 'sheet';
  ctl.sheet = openSheet({ title, body: holder, back: opt.back && typeof opt.back.onClick === 'function' ? opt.back : null,
    onClose: () => { if (ui.openForm === 'sheet') ui.openForm = null; if (typeof Shell !== 'undefined' && Shell) Shell.formWatch = null; } });
  if (typeof Shell !== 'undefined' && Shell) Shell.formWatch = () => { if (ui.openForm !== 'sheet') { Shell.formWatch = null; closeSheet(); } };
  if (typeof ctl.form.focusFirst === 'function') ctl.form.focusFirst();
  launcherSchemaLoad().then((m) => { if (m) ctl.refreshSchema(); });
  launcherGotchas(ctl, rp.p, mode);
  return ctl;
}

/* The newest claude-mem gotchas of the project under the repo field (v0.5.20): one fetch per sheet open, started after the sheet has painted, only while the board
   has claude-mem. The answer is dropped when the sheet closed or was swapped meanwhile; a stopped or slow worker, or no gotchas, shows nothing and never takes the focus
   or touches what was typed. */
function launcherGotchas(ctl, p, mode) {
  if (mode !== 'session' || !ctl.memHost || typeof memoryGotchasMount !== 'function' || !(state && state.memory)) return;
  const host = ctl.memHost;
  setTimeout(() => {
    memoryGotchasMount(host, p.name, { isCurrent: () => ui.openForm === 'sheet' && host.isConnected !== false && ctl.form._launcher === ctl });
  }, 0);
}
