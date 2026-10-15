/* ccboard launcher, node half (issues #141 and #143): start work on ANOTHER node from the launcher sheet. Loaded with launcher.js (the 'launcher' bundle, lazy.js); definition only: no
   DOM, storage, network, listener or timer exists until a sheet is opened. openLauncher (launcher.js) asks lxnKit() for the node picker; it answers null, and the sheet is exactly what
   it always was, unless the hub view is on and at least one paired node is online (Nodes.enabled, Nodes.ready, nodes-hub.js).

   The picker is a segmented control "This node | Auto | <node> ..." (a native select above four) above the form. Auto (issue #150, tasks only) asks the board which node can take the task
   now (GET /api/route/auto), shows the node and the reason in the command preview, and then IS that node's form; when no node can, it says why and offers Check again. "This node" keeps the local form untouched. A node swaps in the REMOTE form
   (lxnForm), a smaller form on purpose: a request from another node is checked by the hub and again by the peer and carries only project, repo, title, prompt, when, agent, model,
   effort, reasoning effort, auto close and an issue reference (a task), a session name, a permission mode of default / acceptEdits / plan or Codex read-only (a session), or a lane /
   session dispatch. No bypass, no extra arguments, tools, directories or system prompt, no first prompt for a session, no chain, no schedule: the form offers none of them and says so
   ("Bypass is only available on the node itself.") and the body builders below send nothing else (tests/js/launcher-node.test.mjs pins the key sets).
   Everything the form lists comes from the chosen node: its projects and repos from the hub read model (Nodes.get(h).state, GET /api/nodes/state), its agents, models, efforts and
   reasoning levels from the relay's agents row (GET /api/nodes/<h>/agents, cached 10 minutes per node). What is remembered (model, effort, mode, agent, the last repo) is stored per
   node and per repo with Ref.scoped / Ref.storeKey, so the local defaults are never touched.
   A start paints its row at once (Nodes.pend: the Tasks page for a task, the node page for a session; "Starting on <node>"), becomes started only when the answer carries a ref, and is
   taken away again when the node refuses. A timeout after the request was sent is "unconfirmed" and is never retried. Every string of a node (names, errors, repo lists) is peer
   data: textContent only. */
'use strict';

const LXN_SCHEMA_MS = 10 * 60 * 1000;              // an agents schema is kept 10 minutes per node
const LXN_RETRY_MS = 15000;                        // a failed schema read is not repeated for 15 s
const LXN_PERMS = [['', 'ask (default)'], ['acceptEdits', 'accept edits'], ['plan', 'plan']];          // the permission modes a request from another node may carry (Claude)
const LXN_CX_MODES = [['default', 'default'], ['read-only', 'read-only']];                              // Codex: default, or the read-only sandbox
const LXN_ISSUE = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}#[0-9]{1,9}$/;
const LXN_NOBYPASS = 'Bypass is only available on the node itself.';
const LXN_NAME = /^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,61}[A-Za-z0-9])?$/;
const lxnSchemas = new Map();                       // handle -> {agents: Map(name -> schema) | null, at, failed, busy, promise}
let lxnSeq = 0;

/* ---------- the demo (?demo=1): GET /api/route/auto is answered here; ?auto=wait plays "no node can take it" ---------- */

function lxnDemoAuto(path) {
  const q = new URLSearchParams(String(path).split('?')[1] || '');
  const agent = q.get('agent') === 'codex' ? 'codex' : 'claude';
  const slug = q.get('project') === 'shop' && q.get('repo') === 'web' ? 'example/shop-web' : 'example/shop-api';
  let flag = '';
  try { const m = /[?&]auto=([a-z]+)/.exec(location.search); flag = m ? m[1] : ''; } catch (_) { flag = ''; }
  const word = agent === 'codex' ? 'weekly window' : '5-hour window';
  if (flag === 'wait') {
    const text = `waiting: build-box ${word} is at 90 percent until 14:10; old-laptop is offline`;
    return Promise.resolve({ handle: null, name: null, why: text, waiting: { code: 'none_ready', reason: text.slice(9), until: 1791900600, until_iso: '2026-10-11T14:10:00Z', text, pinned: null },
      pin: null, refused: [{ handle: 'build-box', name: 'build-box', code: 'window', text: `${word} is at 90 percent until 14:10`, until: 1791900600 }, { handle: 'old-laptop', name: 'old-laptop', code: 'offline', text: 'is offline', until: null }],
      ranked: [], agent, slug });
  }
  return Promise.resolve({ handle: 'build-box', name: 'build-box', why: `chose build-box: 2 free lanes, ${word} 31 percent`, waiting: null, pin: null,
    refused: [{ handle: 'old-laptop', name: 'old-laptop', code: 'offline', text: 'is offline', until: null }], ranked: [{ handle: 'build-box', name: 'build-box', free: 2, pct: 31, window_unknown: false, headroom: 69, shared_with: [] }], agent, slug });
}

/* ---------- the node's lists ---------- */

/* GET /api/nodes/<h>/agents, at most once per 10 minutes per node (a failed read is tried again after 15 s). Resolves to the Map of the node's agents, or null. */
function lxnSchemaLoad(handle) {
  const now = Date.now();
  const e = lxnSchemas.get(handle);
  if (e && (e.busy || (e.agents && now - e.at < LXN_SCHEMA_MS) || (!e.agents && e.failed && now - e.failed < LXN_RETRY_MS))) return e.promise;
  const n = e || { agents: null, at: 0, failed: 0, busy: false, promise: null };
  lxnSchemas.set(handle, n);
  n.busy = true;
  n.promise = (async () => {
    try {
      const r = await api('GET', `/api/nodes/${encodeURIComponent(handle)}/agents`);
      const list = r && r.data && Array.isArray(r.data.agents) ? r.data.agents : null;
      if (list) { n.agents = new Map(list.filter((a) => a && typeof a === 'object' && (a.name === 'claude' || a.name === 'codex')).map((a) => [a.name, a])); n.at = Date.now(); n.failed = 0; }
      else n.failed = Date.now();
    } catch (_) { n.failed = Date.now(); }
    n.busy = false;
    return n.agents;
  })();
  return n.promise;
}

function lxnStr(v) { return Array.isArray(v) ? v.filter((x) => typeof x === 'string' && x) : []; }

/* What the form needs of one agent on one node: the node's own schema once it is read, until then the card's agent list (installed, logged in). */
function lxnInfo(handle, agent) {
  const rec = Nodes.get(handle);
  const e = lxnSchemas.get(handle);
  const got = e && e.agents ? e.agents.get(agent) || null : null;
  const card = rec && rec.card && Array.isArray(rec.card.agents) ? rec.card.agents.find((a) => a && a.id === agent) || null : null;
  const by = got && got.reasoning_by_model && typeof got.reasoning_by_model === 'object' ? got.reasoning_by_model : {};
  const opt = (k) => (got && Array.isArray(got.options) ? got.options.find((o) => o && o.key === k) || null : null);
  return { loaded: !!got, known: !!(got || card), installed: got ? !!got.installed : !!(card && card.installed), loggedIn: got ? !!got.logged_in : !!(card && card.logged_in),
    models: got ? lxnStr(got.models) : [], efforts: got ? lxnStr(got.efforts).filter((x) => x !== 'ultracode') : [], reasoning_by_model: by, opt };
}

function lxnAgentWhy(handle, agent) {
  const i = lxnInfo(handle, agent);
  if (!i.known) return '';
  const name = Nodes.nameOf(handle);
  if (!i.installed) return `${AGENT_NAME[agent] || agent} is not installed on ${name}`;
  if (!i.loggedIn) return `${AGENT_NAME[agent] || agent} is not logged in on ${name}`;
  return '';
}

/* [{project, repo, slug, branch}] from the hub's last reading of the node: the places a request may name (names the peer would refuse are left out). */
function lxnRepos(handle) {
  const rec = Nodes.get(handle);
  const ps = rec && rec.state && Array.isArray(rec.state.projects) ? rec.state.projects : [];
  const ok = (n) => typeof n === 'string' && Ref.PART_RE.test(n) && n.indexOf('--') < 0;
  const out = [];
  for (const p of ps) {
    if (!p || !ok(p.name)) continue;
    for (const r of Array.isArray(p.repos) ? p.repos : []) if (r && ok(r.name)) out.push({ project: p.name, repo: r.name, slug: typeof r.slug === 'string' ? r.slug : '', branch: typeof r.branch === 'string' ? r.branch : '' });
  }
  return out;
}

/* ---------- what is remembered, per node and per repo (the local keys are never written) ---------- */

const lxnPrefKey = (kind, handle, p, r, agent) => (kind === 'session' ? `ccboard:launch:${Ref.scoped(handle, `${p}/${r}`)}:${agent}` : `ccboard:task:${Ref.scoped(handle, `${p}/${r}`)}` + (agent === 'claude' ? '' : `:${agent}`));
const lxnAgentKey = (handle, p, r) => `ccboard:agent:${Ref.scoped(handle, `${p}/${r}`)}`;
const lxnLastKey = (handle) => Ref.storeKey('ccboard:nlast:', Ref.node(handle));

/* Remembered values for one agent, checked against what the node offers now. A choice the node no longer offers is replaced by the default and said (notes). */
function lxnClean(handle, agent, saved, kind, notes) {
  const s = saved && typeof saved === 'object' ? saved : {};
  const i = lxnInfo(handle, agent);
  const name = Nodes.nameOf(handle);
  const v = agent === 'claude' ? { model: '', model_custom: '', effort: '', permission_mode: '' } : { model: '', model_custom: '', reasoning: '', cx_mode: 'default' };
  const has = (k) => typeof s[k] === 'string';                                   // an empty model or effort is a choice too ("default (settings)"), kept as one
  const m = has('model') ? s.model : '';
  if (m === 'custom') { v.model = 'custom'; v.model_custom = String(s.model_custom || '').slice(0, 80); }
  else if (m) { if (!i.loaded || i.models.includes(m)) v.model = m; else notes.push(`Your saved model (${m.slice(0, 40)}) is not offered by ${name}. Using the default.`); }
  const dm = i.opt('model');
  if (!v.model && !(has('model') && !m) && dm && typeof dm.default === 'string' && i.models.includes(dm.default)) v.model = dm.default;
  if (agent === 'claude') {
    const e = has('effort') ? s.effort : '';
    if (e) { if (!i.loaded || i.efforts.includes(e)) v.effort = e; else notes.push(`Your saved effort (${e.slice(0, 20)}) is not offered by ${name}. Using the default.`); }
    const de = i.opt('effort');
    if (!v.effort && !(has('effort') && !e) && de && typeof de.default === 'string' && i.efforts.includes(de.default)) v.effort = de.default;
    if (kind === 'session' && (s.permission_mode === 'acceptEdits' || s.permission_mode === 'plan')) v.permission_mode = s.permission_mode;
  } else {
    const r = typeof s.reasoning === 'string' && s.reasoning ? s.reasoning : (typeof s.reasoning_effort === 'string' ? s.reasoning_effort : '');
    if (r) {
      const ok = launcherReasoning({ efforts: i.efforts, reasoning_by_model: i.reasoning_by_model }, v.model === 'custom' ? '' : v.model).allowed;
      if (!i.loaded || ok.includes(r)) v.reasoning = r; else notes.push(`Your saved reasoning level (${r.slice(0, 20)}) is not offered by ${name}. Using the default.`);
    }
    if (kind === 'session' && s.cx_mode === 'read-only') v.cx_mode = 'read-only';
  }
  return v;
}

/* ---------- the bodies: ONE place that decides what leaves this board for another node (the hub's strict models refuse anything else) ---------- */

function lxnModel(c) { return String(c.model === 'custom' ? c.model_custom : c.model || '').trim().slice(0, 80); }

/* POST /api/nodes/<h>/sessions: project, repo, agent, name?, model?, effort? | reasoning_effort?, permission_mode? (acceptEdits, plan) | mode? (read-only). No prompt, no launcher. */
function lxnSessionBody(R) {
  const c = R[R.agent];
  const body = { project: R.project, repo: R.repo, agent: R.agent };
  if (String(R.name || '').trim()) body.name = String(R.name).trim();
  const m = lxnModel(c);
  if (m) body.model = m;
  if (R.agent === 'claude') {
    if (c.effort) body.effort = String(c.effort).slice(0, 40);
    if (c.permission_mode === 'acceptEdits' || c.permission_mode === 'plan') body.permission_mode = c.permission_mode;
  } else {
    if (c.reasoning) body.reasoning_effort = String(c.reasoning).slice(0, 40);
    if (c.cx_mode === 'read-only') body.mode = 'read-only';
  }
  return body;
}

/* POST /api/nodes/<h>/tasks: project, repo, title, prompt, when (now | later), agent, model?, effort? | reasoning_effort?, auto_close? (only false is sent), issue_ref?. */
function lxnTaskBody(R) {
  const c = R[R.agent];
  const prompt = String(R.prompt || '').trim();
  const body = { project: R.project, repo: R.repo, title: (String(R.title || '').trim() || taskTitleFrom(prompt)).slice(0, 300), prompt, when: R.when === 'later' ? 'later' : 'now', agent: R.agent };
  const m = lxnModel(c);
  if (m) body.model = m;
  if (R.agent === 'claude') { if (c.effort) body.effort = String(c.effort).slice(0, 40); } else if (c.reasoning) body.reasoning_effort = String(c.reasoning).slice(0, 40);
  if (R.autoClose === false) body.auto_close = false;
  if (String(R.issue || '').trim()) body.issue_ref = String(R.issue).trim();
  return body;
}

/* POST /api/nodes/<h>/tasks/<id>/dispatch: mode lane (agent when it is not the task's own, model?, effort? | reasoning_effort?, auto_close) or mode session (session, auto_close). */
function lxnDispatchBody(R, task) {
  const auto = R.autoClose !== false;
  if (R.where === 'session') return { mode: 'session', session: R.sess, auto_close: auto };
  const c = R[R.agent];
  const body = { mode: 'lane', auto_close: auto };
  if (R.agent !== (task && task.agent === 'codex' ? 'codex' : 'claude')) body.agent = R.agent;
  const m = lxnModel(c);
  if (m) body.model = m;
  if (R.agent === 'claude') { if (c.effort) body.effort = String(c.effort).slice(0, 40); } else if (c.reasoning) body.reasoning_effort = String(c.reasoning).slice(0, 40);
  return body;
}

/* ---------- the words of a refusal ---------- */

/* A failed call as one plain sentence. err.body is the hub's {error, reason, node, age}: the reason picks the sentence, the hub's own text (already cleaned) fills the cases that
   carry something only it knows (a missing repo, a peer's conflict). Capped; shown through textContent. */
function lxnErrorText(err, handle, scope) {
  const b = err && err.body && typeof err.body === 'object' ? err.body : {};
  const name = Nodes.nameOf(handle);
  const own = typeof b.error === 'string' && b.error ? b.error : (err && err.message ? String(err.message) : 'the request failed');
  const cut = (t) => String(t).slice(0, 300);
  if (Nodes.unknown(err)) return Nodes.unknownText(handle);                              // issue #153: the call may have reached the node; the board never sends it again
  switch (b.reason) {
    case 'offline': return typeof b.age === 'number' ? `${name} is offline, last seen ${nhAgo(b.age)} ago` : `${name} has not answered yet`;
    case 'scope': return /^needs the /.test(own) ? cut(own) : `needs the ${scope || 'tasks'} scope on ${name}`;
    case 'needs_repair': return `${name} no longer takes this board's token: re-pair it in Settings, Nodes`;
    case 'unpaired': return `${name} answers as another node or no longer knows this board: remove it and pair it again`;
    case 'not_read_yet': return `${name} has not been read yet: wait a few seconds and try again`;
    case 'identity_changed': return `This address now belongs to another device, not ${name}: remove it or pair again`;
    case 'incompatible': return Nodes.apiWords({ name, handle, peer_api: b.peer_api, api: b.api });
    case 'rate_limited': return 'Too many requests in a minute. Try again in a moment.';
    default: return cut(own);
  }
}

/* ---------- the toast with a link to the node's own page ---------- */

function lxnToast(handle, text, route, kind) {
  const t = toast(text, { kind: kind || 'ok', ttl: 9000 });
  const rec = Nodes.get(handle);
  const a = t && rec && typeof nhOpenLink === 'function' ? nhOpenLink(`Open on ${Nodes.nameOf(handle)}`, rec, route || '', 'toast-link') : null;
  if (a) t.append(a);
  return t;
}

function lxnGo(hash) { if (typeof navigate === 'function') navigate(hash); else if (typeof location !== 'undefined') location.hash = hash; }

/* ---------- the remote form ---------- */

/* opt: the options openLauncher got (mode, project, repo, remoteTask); handle: the node; hooks {carry, close}. Returns {form, R, submit, go, status, gate, body, preview, refresh}. */
function lxnForm(opt, handle, hooks) {
  const mode = opt.mode === 'task' || opt.mode === 'dispatch' ? opt.mode : 'session';
  const session = mode === 'session';
  const dispatch = mode === 'dispatch';
  const kind = session ? 'session' : 'task';
  const rt = dispatch && opt.remoteTask && typeof opt.remoteTask === 'object' ? opt.remoteTask : null;
  const carry = (hooks && hooks.carry) || {};
  const autoAgent = hooks && (hooks.autoAgent === 'claude' || hooks.autoAgent === 'codex') ? hooks.autoAgent : '';          // Auto asked the board for this agent: the form does not change it
  const name = () => Nodes.nameOf(handle);
  const rec = () => Nodes.get(handle);
  let repos = lxnRepos(handle);
  const R = { project: '', repo: '', agent: autoAgent || 'claude', name: String(carry.name || ''), title: String(carry.title || ''), prompt: String(carry.prompt || ''), issue: '', when: 'now', autoClose: true,
    where: 'lane', sess: '', claude: null, codex: null, notes: [], dirty: false };

  /* ---- where: the project and repo (a dispatch is for the repo its task lives in) ---- */
  const named = (x) => (x && typeof x === 'object' ? x.name : typeof x === 'string' ? x : '');
  if (rt) { R.project = String(rt.project || ''); R.repo = String(rt.repo || ''); R.agent = rt.agent === 'codex' ? 'codex' : 'claude'; R.autoClose = true; }
  else {
    const want = repos.find((x) => x.project === named(opt.project) && x.repo === named(opt.repo));
    const last = String(lxGet(lxnLastKey(handle)) || '').split('/');
    const was = repos.find((x) => x.project === last[0] && x.repo === last[1]);
    const pick = want || was || repos[0] || { project: '', repo: '' };
    R.project = pick.project; R.repo = pick.repo;
  }
  const loadPrefs = () => {
    R.notes = [];
    if (!rt && !autoAgent) { const ag = lxGet(lxnAgentKey(handle, R.project, R.repo)); if (ag === 'claude' || ag === 'codex') R.agent = ag; }
    for (const a of ['claude', 'codex']) R[a] = lxnClean(handle, a, lxJson(lxGet(lxnPrefKey(kind, handle, R.project, R.repo, a))), kind, R.notes);
    const saved = lxJson(lxGet(lxnPrefKey(kind, handle, R.project, R.repo, 'claude'))) || {};
    if (!session && !rt && saved.when === 'later') R.when = 'later';
    if (!session && saved.auto_close === false) R.autoClose = false;
  };
  loadPrefs();

  /* ---- the controls ---- */
  let update = () => {};
  const edit = () => { R.dirty = true; update(); };
  const hide = (n, on) => { if (n) n.classList.toggle('hidden', !!on); };
  const status = el('div', { class: 'dim form-status', role: 'status', 'aria-live': 'polite' });
  const why = el('p', { class: 'dim lx-hint lx-nodewhy', role: 'status' });
  const fill = (sel, options, value) => { sel.textContent = ''; for (const [v, t] of options) sel.append(el('option', { value: v, text: t })); sel.value = value; };

  const projSel = el('select', { 'aria-label': 'Project' });
  const repoSel = el('select', { 'aria-label': 'Repo' });
  const projects = () => [...new Set(repos.map((x) => x.project))];
  const paintPlace = () => {
    fill(projSel, projects().length ? projects().map((p) => [p, p]) : [['', '(no repo in the last reading)']], R.project);
    const inP = repos.filter((x) => x.project === R.project);
    fill(repoSel, inP.length ? inP.map((x) => [x.repo, x.repo]) : [['', '(none)']], R.repo);
  };
  const placed = () => { loadPrefs(); R.dirty = false; paint(); };
  projSel.addEventListener('change', () => { R.project = projSel.value; const f = repos.find((x) => x.project === R.project); R.repo = f ? f.repo : ''; placed(); });
  repoSel.addEventListener('change', () => { R.repo = repoSel.value; placed(); });
  const placeField = rt
    ? field('Task', el('div', { class: 'lx-nodetask' }, el('b', { text: String(rt.title || `Task ${rt.id}`) }), el('span', { class: 'dim', text: ` ${R.project}/${R.repo} on ${name()}` })))
    : el('div', { class: 'grid' }, field('Project', projSel), field('Repo', repoSel));

  const agentSeg = lxSeg([['claude', '◆ Claude'], ['codex', '◇ Codex']], (a) => switchAgent(a), 'Agent', 'lx-agent');
  const agentWhy = el('p', { class: 'dim lx-hint lx-why' });
  const switchAgent = (a) => { if (a === R.agent || lxnAgentWhy(handle, a)) return; R.agent = a; edit(); paint(); };
  const agentField = field('Agent', el('div', { class: 'lx-agentctl' }, agentSeg.node, agentWhy));

  const modelSel = el('select', { 'aria-label': 'Model' });
  const modelId = el('input', { type: 'text', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', maxlength: '80', placeholder: 'model id', 'aria-label': 'Custom model id' });
  modelSel.addEventListener('change', () => { R[R.agent].model = modelSel.value; edit(); paint(); if (modelSel.value === 'custom') focusFine(modelId); });
  const onId = () => { R[R.agent].model_custom = modelId.value.trim(); edit(); };
  modelId.addEventListener('input', onId); modelId.addEventListener('change', onId);
  const modelNote = el('p', { class: 'dim lx-hint' });
  const modelField = field('Model', el('div', { class: 'lx-model' }, modelSel, modelId), null);
  const effortSel = el('select', { 'aria-label': 'Effort' });
  effortSel.addEventListener('change', () => { R.claude.effort = effortSel.value; edit(); });
  const effortField = field('Effort', effortSel);
  const reasonSel = el('select', { 'aria-label': 'Reasoning' });
  reasonSel.addEventListener('change', () => { R.codex.reasoning = reasonSel.value; edit(); });
  const reasonField = field('Reasoning', reasonSel);

  const permSeg = lxSeg(LXN_PERMS, (v) => { R.claude.permission_mode = v; edit(); paint(); }, 'Permissions', 'lx-modes');
  const cxSeg = lxSeg(LXN_CX_MODES, (v) => { R.codex.cx_mode = v; edit(); paint(); }, 'Codex mode', 'lx-modes');
  const permField = field('Permissions', permSeg.node);
  const cxField = field('Mode', cxSeg.node, 'default: sandboxed to the repo, asks on request. read-only: it can read, not write.');
  const noBypass = el('p', { class: 'dim lx-hint lx-nobypass', text: LXN_NOBYPASS });

  const nameIn = el('input', { type: 'text', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', maxlength: '63', placeholder: 'auto: the node picks a free name' });
  nameIn.value = R.name;
  const onName = () => { R.name = nameIn.value.trim(); edit(); };
  nameIn.addEventListener('input', onName); nameIn.addEventListener('change', onName);
  const nameField = field('Session name', nameIn);
  const noPrompt = el('p', { class: 'dim lx-hint', text: 'A session opened from another node starts empty. Type to it on its own page: the toast has Open on that node.' });

  const whenSeg = lxSeg([['now', 'Now'], ['later', 'Later']], (w) => { R.when = w; edit(); paint(); }, 'Run', 'lx-when');
  const whenField = field('Run', whenSeg.node, 'Now starts it on the node. Later parks it in the node\'s Backlog.');
  const titleIn = el('input', { type: 'text', autocomplete: 'off', autocapitalize: 'off', maxlength: '120', placeholder: 'e.g. Fix the login redirect' });
  titleIn.value = R.title;
  const onTitle = () => { R.title = titleIn.value; edit(); };
  titleIn.addEventListener('input', onTitle); titleIn.addEventListener('change', onTitle);
  const titleField = field('Title', titleIn, 'Optional: the first line of the prompt is used.');
  const promptEl = el('textarea', { class: 'task-prompt lx-prompt', rows: '3', autocomplete: 'off', spellcheck: 'true', 'aria-label': 'Prompt', placeholder: 'What should it do?' });
  promptEl._maxRows = 10;
  promptEl.value = R.prompt;
  const nlBtn = newlineButton(promptEl);
  const promptField = field('Prompt', el('div', { class: 'lx-promptbox' }, promptEl, nlBtn), 'Enter starts it · Shift+Enter adds a line.');
  const syncPrompt = () => { if (R.prompt !== promptEl.value) { R.prompt = promptEl.value; edit(); } };
  promptEl.addEventListener('input', () => { fieldError(promptField, ''); syncPrompt(); });
  promptEl.addEventListener('keyup', syncPrompt);
  nlBtn.addEventListener('click', syncPrompt);
  const issueIn = el('input', { type: 'text', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', maxlength: '250', placeholder: 'owner/name#123' });
  const onIssue = () => { R.issue = issueIn.value.trim(); fieldError(issueField, ''); edit(); };
  issueIn.addEventListener('input', onIssue); issueIn.addEventListener('change', onIssue);
  const issueField = field('Issue', issueIn, 'Optional: a GitHub issue this task is for.');
  const autoChk = el('input', { type: 'checkbox' });
  autoChk.checked = R.autoClose;
  autoChk.addEventListener('change', () => { R.autoClose = autoChk.checked; edit(); });
  const autoField = field('When it finishes', el('div', { class: 'checks' }, el('label', {}, autoChk, 'Close the session')),
    'The session closes itself about a minute after the task stops, unless it asked you something.');

  const whereSeg = lxSeg([['lane', 'New session'], ['session', 'Running session']], (w) => { if (w === 'session' && Nodes.can(rec(), 'sessions').ok === false) return; R.where = w; edit(); paint(); }, 'Where', 'lx-where');
  const ready = () => (rt ? Nodes.sessions(handle).filter((s) => s.state === 'idle' && s.project === rt.project && s.repo === rt.repo && Ref.isTmux(s.tmux)) : []);
  const sessSel = el('select', { 'aria-label': 'Session' });
  sessSel.addEventListener('change', () => { R.sess = sessSel.value; edit(); });
  const sessField = field('Session', sessSel, 'The prompt is pasted into it. It must be idle in the same repo.');
  const whereField = field('Where', whereSeg.node);

  /* ---- the command preview, labelled with the node ---- */
  let cmdText = '';
  const cmdNode = el('pre', { class: 'lx-cmd', 'aria-label': 'Command preview' });
  const cmdHead = el('h3', { class: 'lx-k' });
  const cmdHint = el('p', { class: 'dim lx-hint' });
  const cmdBox = el('div', { class: 'lx-cmdbox' }, el('div', { class: 'lx-cmdhead' }, cmdHead), cmdNode, cmdHint);
  const previewText = () => {
    const a = R.agent, c = R[a];
    const v = { agent: a, launch: 'new', name: R.name, prompt: '', model: c.model, model_custom: c.model_custom, effort: c.effort, reasoning: c.reasoning, permission_mode: c.permission_mode,
      cx_mode: c.cx_mode, worktree: false, add_dirs: [], args: '' };
    return commandPreview(v, { mode: session ? 'session' : 'task', caps: {}, cwd: '', nextName: 'auto', dirs: {}, slug: taskTitleFrom(R.title || R.prompt).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 30) || '<task>' });
  };

  /* ---- the foot: one filled primary, named for the node ---- */
  const goText = el('span', { class: 'tf-go-text' });
  const goIcon = el('span', { class: 'tf-go-ic' });
  const go = el('button', { class: 'primary', type: 'submit' }, goIcon, goText);
  const cancel = el('button', { type: 'button', text: 'Cancel', onclick: () => { if (hooks && typeof hooks.cancel === 'function') hooks.cancel(); else { ui.openForm = null; closeSheet(); } } });
  const foot = el('div', { class: 'submit lx-foot' }, go, cancel);

  /* ---- what is offered, and why Start is off ---- */
  const need = () => (session ? ['sessions'] : dispatch && R.where === 'session' ? ['tasks', 'sessions'] : ['tasks']);
  const gate = () => {
    const r = rec();
    for (const sc of need()) { const c = Nodes.can(r, sc); if (!c.ok) return c.why; }
    if (dispatch && R.where === 'session') return ready().length ? '' : `No session on ${name()} is idle in ${R.project}/${R.repo}.`;
    if (!rt && !repos.length) return r && r.state ? `${name()} lists no repo in its last reading` : `${name()} has not been read yet`;
    if (!rt && !R.repo) return `Pick a repo on ${name()}.`;
    return lxnAgentWhy(handle, R.agent);
  };

  const paintLists = () => {
    const i = lxnInfo(handle, R.agent);
    const c = R[R.agent];
    const models = [['', 'default (settings)'], ...i.models.map((m) => [m, m]), ['custom', 'custom id…']];
    if (c.model && c.model !== 'custom' && !i.models.includes(c.model)) models.splice(1, 0, [c.model, c.model]);                  // a value kept before the node's list arrived
    fill(modelSel, models, c.model);
    modelId.value = c.model_custom || '';
    hide(modelId, c.model !== 'custom');
    if (R.agent === 'claude') {
      const list = [['', 'default'], ...i.efforts.map((e) => [e, e])];
      if (c.effort && !i.efforts.includes(c.effort)) list.splice(1, 0, [c.effort, c.effort]);
      fill(effortSel, list, c.effort);
    } else {
      const lv = launcherReasoning({ efforts: i.efforts, reasoning_by_model: i.reasoning_by_model }, c.model === 'custom' ? '' : c.model).allowed;
      const list = [['', 'default'], ...lv.map((e) => [e, e])];
      if (c.reasoning && !lv.includes(c.reasoning)) c.reasoning = '';
      fill(reasonSel, list, c.reasoning);
    }
  };
  const paint = () => {
    const claude = R.agent === 'claude';
    for (const a of ['claude', 'codex']) { const w = lxnAgentWhy(handle, a); agentSeg.disable(a, !!w, w); }
    agentSeg.set(R.agent);
    agentWhy.textContent = [...new Set(['claude', 'codex'].map((a) => lxnAgentWhy(handle, a)).filter(Boolean))].join(' · ');
    if (!rt) paintPlace();
    paintLists();
    permSeg.set(R.claude.permission_mode || ''); cxSeg.set(R.codex.cx_mode || 'default');
    nameIn.value = R.name; titleIn.value = R.title; issueIn.value = R.issue; autoChk.checked = R.autoClose;
    whenSeg.set(R.when);
    whereSeg.set(R.where);
    const noSess = Nodes.can(rec(), 'sessions');
    whereSeg.disable('session', !noSess.ok, noSess.ok ? '' : noSess.why);
    const rd = ready();
    if (dispatch) { if (!rd.some((s) => s.tmux === R.sess)) R.sess = rd[0] ? rd[0].tmux : ''; fill(sessSel, rd.length ? rd.map((s) => [s.tmux, String(s.session || s.tmux)]) : [['', '(no session is idle here)']], R.sess); }
    hide(effortField, !claude); hide(reasonField, claude);
    hide(permField, !session || !claude); hide(cxField, !session || claude);
    hide(nameField, !session); hide(noPrompt, !session);
    hide(whenField, mode !== 'task'); hide(titleField, mode !== 'task'); hide(promptField, mode !== 'task'); hide(issueField, mode !== 'task');
    hide(autoField, session);
    hide(whereField, !dispatch); hide(sessField, !dispatch || R.where !== 'session');
    const laneHide = dispatch && R.where === 'session';
    hide(agentField, laneHide || !!autoAgent); hide(modelField, laneHide); hide(effortField, laneHide || !claude); hide(reasonField, laneHide || claude); hide(cmdBox, laneHide);
    modelNote.textContent = R.notes.join(' ');
    hide(modelNote, !R.notes.length);
    update();
  };
  let busy = false;
  update = () => {
    const w = gate();
    cmdText = previewText();
    cmdNode.textContent = '';
    cmdNode.append(...lxCmdNodes(cmdText));
    const autoWhy = hooks && typeof hooks.autoWhy === 'string' ? hooks.autoWhy : '';
    cmdHead.textContent = autoWhy ? `Auto: command on ${name()}` : `Command on ${name()}`;
    cmdHint.textContent = autoWhy ? `Auto ${autoWhy}. Approximate: ${name()} builds the real command.` : `Approximate: ${name()} builds the real command.`;
    const label = session ? `Start on ${name()}` : mode === 'task' ? (R.when === 'later' ? `Add to ${name()}'s backlog` : `Start on ${name()}`) : `Start on ${name()}`;
    goText.textContent = busy ? 'Starting…' : label;
    goIcon.textContent = '';
    goIcon.append(ic(mode === 'task' && R.when === 'later' ? 'add' : 'play'));
    lxDisable(go, busy || !!w, w || '');
    why.textContent = w;
    hide(why, !w);
  };

  /* ---- submit ---- */
  const wipe = () => { for (const f of [nameField, promptField, issueField]) fieldError(f, ''); formStatus(status, ''); };
  const fail = (err) => {
    formStatus(status, lxnErrorText(err, handle, session ? 'sessions' : 'tasks'), true);
    if (Nodes.unknown(err)) { const link = Nodes.checkLink(handle); if (link) status.append(' ', link); }          // the outcome is unknown: a link to the node's page, never a second send
  };
  const finish = () => { if (hooks && typeof hooks.close === 'function') hooks.close(); else { ui.openForm = null; closeSheet(); } };
  const remember = () => {
    const c = R[R.agent];
    const keep = { model: c.model, model_custom: c.model_custom };
    if (R.agent === 'claude') { keep.effort = c.effort; if (session) keep.permission_mode = c.permission_mode; } else { keep.reasoning = c.reasoning; if (session) keep.cx_mode = c.cx_mode; }
    if (!session) { keep.auto_close = R.autoClose; if (!rt) keep.when = R.when; }
    for (const k of Object.keys(keep)) if (keep[k] === undefined || (keep[k] === '' && k !== 'model' && k !== 'effort')) delete keep[k];
    lxPut(lxnPrefKey(kind, handle, R.project, R.repo, R.agent), JSON.stringify(keep));
    if (!rt) { lxPut(lxnAgentKey(handle, R.project, R.repo), R.agent); lxPut(lxnLastKey(handle), `${R.project}/${R.repo}`); }
  };

  const base = `/api/nodes/${encodeURIComponent(handle)}`;
  const sendSession = async () => {
    const body = lxnSessionBody(R);
    const row = Nodes.pend({ kind: 'session', node: handle, tmux: `starting-${++lxnSeq}`, session: R.name || 'new session', project: R.project, repo: R.repo, agent: R.agent, state: 'unknown', needs_you: false });
    let res = null;
    try { res = await api('POST', `${base}/sessions`, body); } catch (err) { Nodes.unpend(row); fail(err); return false; }
    const d = res && res.data && typeof res.data === 'object' ? res.data : null;
    if (!d || typeof d.ref !== 'string' || !Ref.isTmux(d.tmux)) { Nodes.unpend(row); fail({ status: 502, body: { reason: 'unconfirmed' } }); return false; }
    Nodes.pended(row, { tmux: d.tmux, session: d.tmux.split('--')[2] || d.tmux, agent: d.agent || R.agent, project: d.project || R.project, repo: d.repo || R.repo });
    remember();
    lxnToast(handle, `Started on ${name()}`, `#/s/${d.tmux}`);
    finish();
    lxnGo(Ref.hash(Ref.node(handle)) || '#/');
    return true;
  };
  const taskRow = (d, from) => ({ id: d.id, title: (d.task && d.task.title) || from.title, phase: d.phase || (d.task && d.task.phase) || from.phase, agent: (d.task && d.task.agent) || R.agent, project: R.project, repo: R.repo,
    branch: d.branch || '', tmux: d.tmux || '', issue_ref: (d.task && d.task.issue_ref) || null, updated_at: (d.task && d.task.updated_at) || new Date().toISOString() });
  const landed = (d, route) => {
    remember();
    lxnToast(handle, R.when === 'later' && !dispatch ? `Added to the backlog on ${name()}` : `Started on ${name()}`, route);
    if (typeof taskWarn === 'function') taskWarn(d);
  };
  const sendTask = async () => {
    const body = lxnTaskBody(R);
    const row = Nodes.pend({ kind: 'task', node: handle, id: `p${++lxnSeq}`, title: body.title, phase: body.when === 'later' ? 'backlog' : 'queued', agent: R.agent, project: R.project, repo: R.repo, issue_ref: null, updated_at: new Date().toISOString() });
    let res = null;
    try { res = await api('POST', `${base}/tasks`, body); } catch (err) { Nodes.unpend(row); fail(err); return false; }
    const d = res && res.data && typeof res.data === 'object' ? res.data : null;
    if (!d || typeof d.ref !== 'string' || !Ref.isId(d.id)) { Nodes.unpend(row); fail({ status: 502, body: { reason: 'unconfirmed' } }); return false; }
    Nodes.pended(row, taskRow(d, row));
    landed(res.data, Ref.isTmux(d.tmux) ? `#/s/${d.tmux}` : '#/tasks');
    finish();
    Nodes.M.tfilter = handle;
    lxPut('ccboard:nodes:tfilter', handle);
    lxnGo('#/tasks');
    return true;
  };
  const sendDispatch = async () => {
    const body = lxnDispatchBody(R, rt);
    const from = Nodes.tasks(handle).find((t) => String(t.id) === String(rt.id)) || rt;
    const row = Nodes.pend({ ...from, kind: 'task', node: handle, id: rt.id, phase: 'queued', replace: true, was: from.phase });
    let res = null;
    try { res = await api('POST', `${base}/tasks/${encodeURIComponent(String(rt.id))}/dispatch`, body); } catch (err) { Nodes.unpend(row); fail(err); return false; }
    const d = res && res.data && typeof res.data === 'object' ? res.data : null;
    if (!d || typeof d.ref !== 'string' || !Ref.isId(d.id)) { Nodes.unpend(row); fail({ status: 502, body: { reason: 'unconfirmed' } }); return false; }
    Nodes.pended(row, taskRow(d, row));
    landed(res.data, Ref.isTmux(d.tmux) ? `#/s/${d.tmux}` : '#/tasks');
    finish();
    lxnGo(Ref.hash(Ref.task(handle, d.id)) || '#/tasks');
    return true;
  };

  const submit = async () => {
    if (busy) return;
    wipe();
    syncPrompt();
    const w = gate();
    if (w) { formStatus(status, w, true); return; }
    if (session && R.name && (!LXN_NAME.test(R.name) || R.name.indexOf('--') >= 0)) { fieldError(nameField, "use letters, digits, '-' or '_' (no '--')", true); return; }
    if (mode === 'task') {
      if (!R.prompt.trim()) { fieldError(promptField, 'Write what it should do.', true); return; }
      if (R.issue && !LXN_ISSUE.test(R.issue)) { fieldError(issueField, 'An issue reference looks like owner/name#123.', true); return; }
    }
    busy = true; update();
    try { await (session ? sendSession() : dispatch ? sendDispatch() : sendTask()); } finally { busy = false; update(); }
  };

  composerBind(promptEl, { maxRows: 10, onSend: () => submit() });
  const form = el('form', { class: 'form lx-form lx-remote' + (session ? '' : ' task-form'), 'data-node': handle, onsubmit: (e) => { e.preventDefault(); submit(); } },
    placeField, dispatch ? [whereField, sessField] : null, agentField, modelField, modelNote, effortField, reasonField, permField, cxField, noBypass, nameField, noPrompt,
    mode === 'task' ? [whenField, titleField, promptField, issueField] : null, autoField, cmdBox, why, status, foot);
  form.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !e.isComposing) { e.preventDefault(); if (typeof e.stopPropagation === 'function') e.stopPropagation(); cancel.click(); } });
  form.focusFirst = () => focusFine(mode === 'task' ? promptEl : (session ? nameIn : null));
  paint();

  /* the node's schema arrives after the sheet opened (cached 10 minutes): the lists follow it, and a saved choice it no longer offers is replaced */
  const refresh = () => { repos = lxnRepos(handle); if (!R.project || !repos.some((x) => x.project === R.project && x.repo === R.repo)) { if (!rt && repos[0]) { R.project = repos[0].project; R.repo = repos[0].repo; } } if (!R.dirty) loadPrefs(); paint(); };
  lxnSchemaLoad(handle).then(() => { if (form.isConnected !== false) refresh(); });
  return { form, R, submit, go, status, gate, refresh, preview: () => cmdText, body: () => (session ? lxnSessionBody(R) : dispatch ? lxnDispatchBody(R, rt) : lxnTaskBody(R)), busy: () => busy, paint };
}

/* ---------- Auto: the board picks the node (issue #150) ---------- */

const LXN_AUTO = '~auto';             // the picker's value for Auto; never a node handle (handles are letters, digits and dashes)

/* The Auto cell of the picker (tasks only). It asks GET /api/route/auto for the repo the sheet was opened for and the agent chosen here, and shows the answer: the node and why (in the command
   preview of that node's form) or "waiting: <reason>" with the nodes that were left out and why. When a node comes back the cell IS that node's remote form (lxnForm), so the body, the scope
   checks and the remembered values are the node form's own; the repo is the node's own name for the slug the board gave, and when the node does not list it the cell says so and builds no form,
   so nothing starts in a repo nobody chose. Nothing is started by the choice: Start is the node form's, named for the node. opt: the options openLauncher got; ctl: the local controller. */
function lxnAuto(opt, ctl, hooks) {
  const rp = typeof lxResolve === 'function' ? lxResolve(opt.project, opt.repo) : null;
  const project = rp ? rp.p.name : '';
  const repo = rp ? rp.r.name : '';
  const A = { agent: ctl && ctl.V && ctl.V.agent === 'codex' ? 'codex' : 'claude', res: null, loading: false, seq: 0, inner: null, handle: '', innerAgent: '', carry: { ...((hooks && hooks.carry) || {}) } };
  const agentSeg = lxSeg([['claude', '◆ Claude'], ['codex', '◇ Codex']], (a) => { if (a === A.agent) return; A.agent = a; agentSeg.set(a); ask(); }, 'Agent', 'lx-agent');
  agentSeg.set(A.agent);
  const note = el('p', { class: 'dim lx-hint lx-autonote', text: 'Auto picks a node that has this repository, the agent ready, a free lane and room in its rate-limit window. Headroom is an estimate: an agent can still reach its limit.' });
  const state = el('p', { class: 'lx-autostate', role: 'status', 'aria-live': 'polite', 'data-auto-state': '' });
  const left = el('ul', { class: 'lx-autoleft' });
  const host = el('div', { class: 'lx-autoinner' });
  const again = el('button', { type: 'button', 'data-auto-again': '', text: 'Check again', onclick: () => ask() });
  const goWait = el('button', { class: 'primary', type: 'button', text: 'Start', 'data-auto-start': '' });
  const foot = el('div', { class: 'submit lx-foot' }, goWait, again);
  const place = el('div', { class: 'lx-autorepo' }, el('b', { text: `${project}/${repo}` }), el('span', { class: 'dim', text: ' on this board' }));
  const form = el('div', { class: 'form lx-form lx-auto', 'data-auto': '' }, field('Repo', place), field('Agent', agentSeg.node), note, state, left, host, foot);
  const hide = (n, on) => { if (n) n.classList.toggle('hidden', !!on); };

  const dropInner = () => {
    if (A.inner) { const r = A.inner.R; A.carry = { title: r.title, prompt: r.prompt, name: r.name }; }
    host.textContent = '';
    A.inner = null;
    A.handle = '';
  };
  const waitView = (text, refused) => {
    dropInner();
    state.textContent = text;
    state.classList.add('warn');
    left.textContent = '';
    for (const x of (Array.isArray(refused) ? refused : []).slice(0, 8)) {
      if (!x || typeof x.text !== 'string') continue;
      left.append(el('li', { class: 'dim' }, el('span', { 'aria-hidden': 'true', text: '∙ ' }), `${String(x.name || x.handle || 'a node').slice(0, 60)} ${x.text.slice(0, 160)}`));
    }
    hide(state, false); hide(left, !left.childNodes.length); hide(foot, false);
    lxDisable(goWait, true, text);
    lxDisable(again, false);
  };
  const apply = () => {
    const r = A.res || {};
    if (A.loading) {
      dropInner();
      state.textContent = 'Choosing a node…';
      state.classList.remove('warn');
      left.textContent = '';
      hide(state, false); hide(left, true); hide(foot, false);
      lxDisable(goWait, true, 'Choosing a node'); lxDisable(again, true);
      return;
    }
    if (r.error) { waitView(`Auto could not ask the board: ${String(r.error).slice(0, 200)}. Check again, or pick a node above.`, []); return; }
    const h = typeof r.handle === 'string' && Ref.isHandle(r.handle) && Nodes.get(r.handle) ? r.handle : '';
    if (!h) {
      const w = r.waiting && typeof r.waiting.text === 'string' && r.waiting.text ? r.waiting.text : 'waiting: no node can take it now';
      waitView(`${w.slice(0, 400)}. Pick a node above to start it there, or check again.`, r.refused);
      return;
    }
    const rr = lxnRepos(h).find((x) => typeof r.slug === 'string' && r.slug && x.slug === r.slug);
    if (!rr) { waitView(`Auto chose ${Nodes.nameOf(h)}, but its last reading does not list ${String(r.slug || 'this repository').slice(0, 100)}. Check again in a moment, or pick a node above.`, []); return; }
    const why = String(r.why || `chose ${Nodes.nameOf(h)}`).slice(0, 300);
    hide(state, true); hide(left, true); hide(foot, true);
    if (A.inner && A.handle === h && A.innerAgent === A.agent) { A.inner.hooks.autoWhy = why; A.inner.paint(); return; }
    dropInner();
    const hk = { ...(hooks || {}), carry: A.carry, autoAgent: A.agent, autoWhy: why };
    A.inner = lxnForm({ ...opt, mode: 'task', project: rr.project, repo: rr.repo }, h, hk);
    A.inner.hooks = hk;
    A.handle = h;
    A.innerAgent = A.agent;
    host.append(A.inner.form);
  };
  function ask() {
    const my = ++A.seq;
    dropInner();
    A.res = null;
    A.loading = true;
    apply();
    api('GET', `/api/route/auto?agent=${encodeURIComponent(A.agent)}&project=${encodeURIComponent(project)}&repo=${encodeURIComponent(repo)}`).then((r) => {
      if (my !== A.seq) return;
      A.loading = false;
      A.res = r && typeof r === 'object' ? r : {};
      apply();
    }, (e) => {
      if (my !== A.seq) return;
      A.loading = false;
      A.res = { error: (e && e.body && e.body.error) || (e && e.message) || 'the request failed' };
      apply();
    });
  }
  form.focusFirst = () => { if (A.inner && A.inner.form.focusFirst) A.inner.form.focusFirst(); else if (typeof again.focus === 'function') again.focus(); };
  ask();
  return { form, get R() { return A.inner ? A.inner.R : null; }, auto: A, go: goWait, status: state, submit: () => (A.inner ? A.inner.submit() : Promise.resolve()), refresh: () => ask(),
    gate: () => (A.inner ? A.inner.gate() : (state.textContent || 'Auto has not chosen a node yet')), preview: () => (A.inner ? A.inner.preview() : ''), body: () => (A.inner ? A.inner.body() : null),
    busy: () => !!(A.inner && A.inner.busy()), paint: () => { if (A.inner) A.inner.paint(); } };
}

/* ---------- the picker in the sheet ---------- */

/* Called by openLauncher (launcher.js) with the options, the local controller (null when there is no local place) and the mode. null: no picker (the hub view is off, no node is
   online, or a dispatch of a task that lives on this node). Otherwise {pick, host, choose(handle), current(), title(), ctl: the remote controller, setTitle}. */
function lxnKit(opt, ctl, mode) {
  if (typeof Nodes === 'undefined' || !Nodes.enabled() || !Nodes.ready) return null;
  const remoteTask = mode === 'dispatch' && opt.remoteTask && typeof opt.remoteTask === 'object' ? opt.remoteTask : null;
  if (mode === 'dispatch' && !remoteTask) return null;
  const recs = Nodes.list().filter((r) => r && !r.legacy && Ref.isHandle(r.handle));
  if (!recs.some((r) => r.status === 'online' || r.status === 'stale')) return null;
  const start = remoteTask ? remoteTask.node : (typeof opt.node === 'string' && recs.some((r) => r.handle === opt.node) ? opt.node : '');
  if (remoteTask && !recs.some((r) => r.handle === remoteTask.node)) return null;
  if (!ctl && !start) return null;                                      // nothing local to go back to and no node chosen: the sheet has nothing to show
  const forms = new Map();
  const host = el('div', { class: 'lx-nodehost' });
  let cur = null;
  let seg = null;
  let sel = null;
  const kit = { host, setTitle: null, ctl: null };
  const label = (r) => { const v = Nodes.view(r); return r.status === 'online' ? Nodes.nameOf(r.handle) : `${Nodes.nameOf(r.handle)} ${v.glyph}`; };
  const rpAuto = mode === 'task' && ctl && !remoteTask && typeof lxResolve === 'function' ? lxResolve(opt.project, opt.repo) : null;          // Auto is for a new task in a repo of this board
  const autoItem = rpAuto && rpAuto.r && !rpAuto.r.root ? [[LXN_AUTO, 'Auto', { title: 'The board picks the node that has this repository, the agent ready, a free lane and room in its rate-limit window' }]] : [];
  const items = [['', 'This node', !ctl ? { disabled: true, title: 'There is no repo on this node to start in' } : remoteTask ? { disabled: true, title: `This task lives on ${Nodes.nameOf(remoteTask.node)}` } : null],
    ...autoItem,
    ...recs.map((r) => [r.handle, label(r), remoteTask && r.handle !== remoteTask.node ? { disabled: true, title: `This task lives on ${Nodes.nameOf(remoteTask.node)}` } : { title: Nodes.view(r).word }])];
  const titleOf = (h) => {
    const n = h === LXN_AUTO ? 'Auto' : Nodes.nameOf(h);
    return mode === 'task' ? `New task · ${n}` : mode === 'dispatch' ? `Start “${String(remoteTask.title || `Task ${remoteTask.id}`).slice(0, 60)}” on ${n}` : `New session · ${n}`;
  };
  const choose = (h) => {
    if (h === cur) { sync(); return; }
    if ((h === '' && !ctl) || (remoteTask && h !== remoteTask.node)) { sync(); return; }
    const from = cur;
    cur = h;
    if (ctl) ctl.form.classList.toggle('hidden', !!h);
    host.textContent = '';
    kit.ctl = null;
    if (h) {
      let r = forms.get(h);
      if (!r) {
        const carry = ctl && from === '' ? { title: ctl.T ? ctl.T.title : '', prompt: ctl.V.common.prompt, name: ctl.V.common.name } : {};
        const hk = { carry, close: () => { ui.openForm = null; closeSheet(); }, cancel: typeof opt.onCancel === 'function' ? opt.onCancel : null };
        r = h === LXN_AUTO ? lxnAuto({ ...opt, mode }, ctl, hk) : lxnForm({ ...opt, mode }, h, hk);
        forms.set(h, r);
      } else r.refresh();
      host.append(r.form);
      kit.ctl = r;
    }
    sync();
    if (typeof kit.setTitle === 'function' && cur !== null && from !== null) kit.setTitle(h ? titleOf(h) : kit.localTitle);
  };
  const sync = () => { if (seg) seg.set(cur); if (sel) sel.value = cur; };
  let control;
  if (items.length <= 4) {
    seg = lxSeg(items, (v) => choose(v), 'Node', 'lx-nodeseg');
    control = seg.node;
  } else {
    sel = selectEl(items.map(([v, t]) => [v, t]), start);
    sel.setAttribute('aria-label', 'Node');
    sel.addEventListener('change', () => choose(sel.value));
    control = sel;
  }
  kit.pick = el('div', { class: 'form lx-form lx-nodepick' }, field('Node', control));
  kit.current = () => cur;
  kit.choose = choose;
  kit.title = () => (cur ? titleOf(cur) : kit.localTitle);
  kit.focus = () => { if (cur && kit.ctl && kit.ctl.form.focusFirst) kit.ctl.form.focusFirst(); else if (ctl && ctl.form.focusFirst) ctl.form.focusFirst(); };
  choose(start);
  return kit;
}
