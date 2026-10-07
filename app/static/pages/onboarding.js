/* ccboard onboarding (v0.5.19): the new-project wizard at #/onboarding/project[?name=<project>], four short steps shown as a vertical stepper that fits a 390 px phone:
     1 Name       a project is a folder under the projects directory. The same rule the box enforces (tmux.valid_name: letters, digits, - and _, start and end with a letter or digit,
                  at most 64, no --) plus 'root' (reserved); POST /api/projects {name}.
     2 Repo       Blank (an empty git repo), Clone (the URL is probed first: POST /api/preflight/clone gives reachable, default branch, needs credentials and the name a clone would
                  get; then POST /api/projects/<p>/repos {url, name}) or Import (the GitHub list behind the box's gh login; POST .../repos/bulk). Progress is read from the state:
                  the repo's own `state` (cloning, ok, clone-failed) and state.clone_queue for a bulk import. Skip leaves the project without a repo (sessions start in its folder).
     3 Defaults   Claude, Codex or Shell, and the model and effort (Codex: model and reasoning) from the launcher's schema, saved as ccboard:defaults:<project> the way the launcher reads it
                  ({agent, claude: {model, effort}, codex: {model, reasoning}}).
     4 First prompt  optional. Start opens the launcher sheet in the new repo with the prompt filled in; Skip goes to the project page.
   The wizard's own state lives in sessionStorage ccboard:wiz, so a reload keeps the step; #/onboarding/project?name=<p> carries on for a project that has no repo yet (step 2).
   The first-run redirect (onboardingMaybeRedirect, called by Home): only when state.setup.first_run, no project exists and ccboard:onboarded is not set, once per tab; leaving the wizard
   any way sets ccboard:onboarded. Nothing else opens it by itself: the entries are the + menu, the sidebar's + project and Home's empty state. The bare #/onboarding is the box checklist
   of the plan, which lives in Settings > Doctor now. One filled primary at a time: the active step's own button. */
'use strict';

const WIZ_KEY = 'ccboard:wiz';                       // sessionStorage: the wizard's state
const WIZ_ONBOARDED = 'ccboard:onboarded';           // localStorage '1': the wizard was used or left; the first-run redirect never fires again
const WIZ_REDIRECTED = 'ccboard:wiz-redirected';     // sessionStorage '1': the first-run redirect fired in this tab already
const WIZ_NAME_RE = /^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?$/;                          // = app/tmux.py NAME_RE (a test keeps the two equal)
const WIZ_URL_RE = /^(https?:\/\/|ssh:\/\/|git:\/\/|git@[A-Za-z0-9._-]+:)[^\s]{1,512}$/;            // = app/projects.py URL_RE
const WIZ_STEPS = [['name', 'Name'], ['repo', 'Repo'], ['defaults', 'Defaults'], ['prompt', 'First prompt']];
const WIZ_PREFLIGHT_MS = 500;                        // the URL is probed this long after the last key
const wizPage = { refs: null, w: null, seq: 0, timer: null, gh: null };

function wizStore(kind) { try { return kind === 'local' ? localStorage : sessionStorage; } catch (_) { return null; } }
function wizGet(kind, key) { const s = wizStore(kind); try { return s ? s.getItem(key) : null; } catch (_) { return null; } }
function wizPut(kind, key, value) { const s = wizStore(kind); try { if (s) { if (value === null) s.removeItem(key); else s.setItem(key, value); } } catch (_) { /* storage may be unavailable */ } }

function wizFresh() {
  return { step: 0, name: '', created: false, mode: 'blank', repoName: '', url: '', repo: '', repos: 0, agent: 'claude', claude: { model: 'opus', effort: 'high' }, codex: { model: '', reasoning: '' }, prompt: '' };
}

function wizLoad() {
  try {
    const v = JSON.parse(wizGet('session', WIZ_KEY) || 'null');
    return v && typeof v === 'object' && !Array.isArray(v) ? { ...wizFresh(), ...v } : null;
  } catch (_) { return null; }
}
function wizSave(w) { wizPut('session', WIZ_KEY, JSON.stringify(w)); }

/* '' when `raw` is a name the box accepts, else the reason in plain words. kind: 'project' | 'repo'. Mirrors tmux.valid_name (+ the reserved 'root'). */
function wizNameProblem(raw, kind) {
  const s = raw === null || raw === undefined ? '' : String(raw);
  if (!s) return `Name the ${kind}.`;
  if (s.indexOf('--') >= 0) return 'Two dashes in a row (--) are reserved: they separate project, repo and session in a session name.';
  if (!WIZ_NAME_RE.test(s)) return 'Use letters, digits, - and _, start and end with a letter or digit, at most 64 characters.';
  if (s === 'root') return "'root' is reserved: it names the project folder itself.";
  return '';
}

function wizUrlProblem(raw) {
  const s = String(raw === null || raw === undefined ? '' : raw).trim();
  if (!s) return 'Paste the clone URL.';
  if (s.charAt(0) === '-' || !WIZ_URL_RE.test(s)) return 'The URL must start with https://, http://, ssh://, git:// or git@host:';
  return '';
}

/* The repo name a clone of `url` gets (app/projects.py derive_repo_name), or ''. */
function wizDeriveName(url) {
  let tail = String(url || '').trim().replace(/\/+$/, '').split('/').pop() || '';
  if (tail.indexOf(':') >= 0) tail = tail.split(':').pop();
  tail = tail.replace(/\.git$/, '');
  const s = tail.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/-{2,}/g, '-').replace(/^[-_]+|[-_]+$/g, '').slice(0, 64).replace(/[-_]+$/, '');
  return s && WIZ_NAME_RE.test(s) && s.indexOf('--') < 0 ? s : '';
}

function wizOnboarded() { return wizGet('local', WIZ_ONBOARDED) === '1'; }

/* The one rule for opening the wizard by itself: a board that was never used (state.setup.first_run), with no project, whose owner has not been through the wizard. */
function wizRedirectWanted(st) {
  if (!st || !st.setup || !st.setup.first_run) return false;
  if (Array.isArray(st.projects) && st.projects.length) return false;
  return !wizOnboarded();
}

/* Home calls this on every update: from Home only, once per tab, replacing Home in the history so Back does not return to an empty page. */
function onboardingMaybeRedirect(st) {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  if (!r || r.id !== 'home' || !wizRedirectWanted(st) || wizGet('session', WIZ_REDIRECTED)) return false;
  wizPut('session', WIZ_REDIRECTED, '1');
  if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('onboarding', { step: 'project' }), { replace: true });
  return true;
}

function wizProject(w, st) { return st && Array.isArray(st.projects) ? st.projects.find((p) => p.name === w.name) || null : null; }

function wizInstalled(agent) { return typeof lxInstalled === 'function' ? lxInstalled(agent) : agent !== 'codex'; }

/* ---------- what each step says once it is done ---------- */

function wizDefaultsText(w) {
  if (w.agent === 'shell') return 'Shell';
  if (w.agent === 'codex') return ['Codex', w.codex.model || 'default model', w.codex.reasoning || ''].filter(Boolean).join(' · ');
  return ['Claude', w.claude.model, w.claude.effort].filter(Boolean).join(' · ');
}

/* The repo step's line, live: {text, tone: '' | 'warn' | 'bad', link?: {text, hash}} from the wizard's choice and the state (the repo's clone state, the bulk queue). */
function wizRepoSummary(w, st) {
  if (w.mode === 'none' || (!w.repo && !w.repos)) return { text: 'No repo yet: sessions start in the project folder.', tone: '' };
  const p = wizProject(w, st);
  const repoOf = (n) => (p && Array.isArray(p.repos) ? p.repos.find((r) => r.name === n) : null);
  if (w.mode === 'import') {
    const cq = st && st.clone_queue ? st.clone_queue : { queued: [], done: [] };
    const mine = (cq.done || []).filter((d) => d.project === w.name);
    const failed = mine.filter((d) => d.status === 'failed');
    const queued = (cq.queued || []).filter((q) => q.project === w.name).length;
    const cloning = p && Array.isArray(p.repos) ? p.repos.filter((r) => r.state === 'cloning').length : 0;
    if (failed.length) return { text: `${failed.length} of ${w.repos} did not start: ${failed.map((f) => f.repo).join(', ')}`.slice(0, 200), tone: 'bad' };
    if (queued || cloning) return { text: `Importing ${w.repos} repo${w.repos === 1 ? '' : 's'}: ${queued ? `${queued} waiting, ` : ''}${cloning} cloning`, tone: '' };
    return { text: `${w.repos} repo${w.repos === 1 ? '' : 's'} imported`, tone: '' };
  }
  if (w.mode === 'clone') {
    const r = repoOf(w.repo);
    if (r && r.state === 'clone-failed') return { text: `Cloning ${w.repo} failed: the clone's terminal shows why.`, tone: 'bad', link: { text: 'Open it', hash: `#/s/${w.name}--${w.repo}--clone` } };
    if (r && r.state === 'cloning') return { text: `Cloning ${w.repo}…`, tone: '' };
    if (r) return { text: `${w.repo} cloned${r.branch ? ` (${r.branch})` : ''}`, tone: '' };
    return { text: `Cloning ${w.repo}…`, tone: '' };
  }
  return { text: `${w.repo}: an empty git repo`, tone: '' };
}

/* ---------- stepper ---------- */

function wizPaintSummary(refs, w, st) {
  const s = wizRepoSummary(w, st);
  if (!refs.repoSum) return;
  refs.repoSum.textContent = '';
  refs.repoSum.className = `wiz-sum${s.tone ? ' ' + s.tone : ''}`;
  refs.repoSum.append(el('span', { text: s.text }));
  if (s.link) refs.repoSum.append(' ', el('a', { href: s.link.hash, text: s.link.text }));
}

function wizStepNode(i, w, st, refs) {
  const state = i < w.step ? 'done' : i === w.step ? 'active' : 'todo';
  const [key, title] = WIZ_STEPS[i];
  const num = el('span', { class: 'wiz-n', 'aria-hidden': 'true', text: state === 'done' ? '✓' : String(i + 1) });
  const body = el('div', { class: 'wiz-body' }, el('h2', { class: 'wiz-title', id: `wiz-h-${key}`, text: title, 'aria-current': state === 'active' ? 'step' : null }));
  if (state === 'done') {
    if (key === 'name') body.append(el('div', { class: 'wiz-sum' }, el('code', { text: w.name }), el('span', { class: 'dim', text: ' created' })));
    else if (key === 'repo') { refs.repoSum = el('div', { class: 'wiz-sum' }); body.append(refs.repoSum); wizPaintSummary(refs, w, st); }
    else if (key === 'defaults') body.append(el('div', { class: 'wiz-sum', text: wizDefaultsText(w) }));
  } else if (state === 'active') {
    const panel = key === 'name' ? wizPanelName(w) : key === 'repo' ? wizPanelRepo(w, refs) : key === 'defaults' ? wizPanelDefaults(w) : wizPanelPrompt(w);
    body.append(panel);
    refs.focus = panel;
  }
  return el('li', { class: 'wiz-step', 'data-step': key, 'data-state': state }, el('div', { class: 'wiz-rail' }, num), body);
}

function wizRender() {
  const refs = wizPage.refs;
  const w = wizPage.w;
  if (!refs || !w) return;
  refs.repoSum = null;
  refs.focus = null;
  refs.list.textContent = '';
  for (let i = 0; i < WIZ_STEPS.length; i++) refs.list.append(wizStepNode(i, w, typeof state !== 'undefined' ? state : null, refs));
  const first = refs.focus && typeof refs.focus.querySelector === 'function' ? refs.focus.querySelector('input[type=text], textarea') : null;
  if (first && typeof focusFine === 'function') focusFine(first);
}

function wizGo(step) {
  const w = wizPage.w;
  w.step = step;
  wizSave(w);
  wizRender();
}

/* Leaving: remember that the wizard was seen (the first-run redirect is over), then go to the project page (or Home before a project exists). */
function wizLeave(toProject) {
  wizPut('local', WIZ_ONBOARDED, '1');
  const w = wizPage.w;
  if (toProject && w && w.created && typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('project', { project: w.name }));
  else if (typeof navigate === 'function') navigate('#/');
}

/* ---------- step 1: name ---------- */

function wizPanelName(w) {
  const input = el('input', { type: 'text', maxlength: 64, autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', placeholder: 'e.g. shop', 'aria-label': 'Project name' });
  input.value = w.name || '';
  const f = field('Project name', input, 'A folder under the projects directory. Letters, digits, - and _; start and end with a letter or digit.');
  input.addEventListener('input', () => fieldError(f, ''));
  const go = el('button', { class: 'primary', type: 'submit', text: 'Create project' });
  const submit = async () => {
    const name = input.value.trim();
    w.name = name;
    const bad = wizNameProblem(name, 'project');
    if (bad) { fieldError(f, bad, true); return; }
    const have = wizProject(w, typeof state !== 'undefined' ? state : null);
    if (have) {
      if ((have.repos || []).length) { fieldError(f, `A project called ${name} already exists.`, true); return; }
      w.created = true;                                                  // an empty project of that name: carry on with it
      wizGo(1);
      return;
    }
    go.disabled = true;
    try { await api('POST', '/api/projects', { name }); } catch (e) { go.disabled = false; fieldError(f, (e && e.message) || 'the board refused it', true); return; }
    w.created = true;
    wizSave(w);
    if (typeof poll === 'function') { try { await poll(true); } catch (_) { /* the next poll shows it */ } }
    wizGo(1);
  };
  return el('form', { class: 'form wiz-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); submit(); } }, f, el('div', { class: 'submit' }, go));
}

/* ---------- step 2: repo ---------- */

const WIZ_MODES = [['blank', 'Blank'], ['clone', 'Clone'], ['import', 'Import']];
const WIZ_GO = { blank: 'Create repo', clone: 'Clone repo', import: 'Import selected', };

/* The chips under the clone URL, from POST /api/preflight/clone: reachable, default branch, credentials, the name. A plain sentence for what to do when the box cannot read it. */
function wizChips(host, res, busy, problem) {
  host.textContent = '';
  if (problem) { host.append(el('span', { class: 'dim', text: problem })); return; }
  if (busy) { host.append(el('span', { class: 'badge', text: 'checking…' })); return; }
  if (!res) return;
  host.append(el('span', { class: `badge ${res.reachable ? 'hue-green' : 'warn'}`, text: res.reachable ? 'reachable' : 'not reachable' }));
  if (res.default_branch) host.append(el('span', { class: 'badge hue-slate', text: `default branch ${res.default_branch}` }));
  if (res.needs_auth) host.append(el('span', { class: 'badge warn', text: 'needs credentials' }));
  if (res.name) host.append(el('span', { class: 'badge', text: `repo name ${res.name}` }));
  if (res.needs_auth) {
    host.append(el('div', { class: 'wiz-hint dim' }, 'The box has no credentials for this address. Use an ssh URL (git@host:owner/repo.git) with a key on the box, or log the GitHub CLI in and use Import: ',
      el('code', { class: 'doc-cmd', text: 'gh auth login' }), copyButton('gh auth login', 'the command')));
  } else if (!res.reachable && res.error) host.append(el('div', { class: 'wiz-hint dim', text: res.error }));
}

function wizPanelRepo(w, refs) {
  let mode = w.mode === 'clone' || w.mode === 'import' ? w.mode : 'blank';
  const blankName = el('input', { type: 'text', maxlength: 64, autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', placeholder: 'e.g. api', 'aria-label': 'Repo name' });
  blankName.value = w.repoName || w.name || '';
  const blankField = field('Repo name', blankName, 'An empty git repo (branch main) inside the project.');
  blankName.addEventListener('input', () => fieldError(blankField, ''));

  const url = el('input', { type: 'text', inputmode: 'url', autocomplete: 'off', autocapitalize: 'off', autocorrect: 'off', spellcheck: 'false', placeholder: 'https://github.com/you/repo.git', 'aria-label': 'Clone URL' });
  url.value = w.url || '';
  const urlField = field('Clone URL', url, 'https, ssh or git. The box asks the remote first, so you know before it clones.');
  const chips = el('div', { class: 'wiz-chips', role: 'status', 'aria-live': 'polite' });
  const cloneName = el('input', { type: 'text', maxlength: 64, autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false', 'aria-label': 'Repo name' });
  cloneName.value = w.repoName || wizDeriveName(w.url) || '';
  const cloneField = field('Repo name', cloneName, 'The folder the clone goes into, inside the project.');
  let nameTouched = !!w.repoName;
  cloneName.addEventListener('input', () => { nameTouched = true; fieldError(cloneField, ''); });
  url.addEventListener('input', () => { fieldError(urlField, ''); wizProbeSoon(url, chips, cloneName, () => nameTouched, (n) => { cloneName.value = n; }); });
  url.addEventListener('blur', () => wizProbeNow(url, chips, cloneName, () => nameTouched, (n) => { cloneName.value = n; }));

  const filter = el('input', { type: 'text', placeholder: 'filter by name…', 'aria-label': 'Filter repos', autocomplete: 'off', autocapitalize: 'off' });
  const list = el('div', { class: 'checks import-repos wiz-list' });
  const gh = el('div', { class: 'wiz-chips', role: 'status', 'aria-live': 'polite' });
  const boxes = [];
  let repos = [];
  const draw = () => {
    list.textContent = '';
    boxes.length = 0;
    const q = filter.value.trim().toLowerCase();
    for (const r of repos) {
      if (q && !(String(r.name).toLowerCase().includes(q) || String(r.description || '').toLowerCase().includes(q))) continue;
      const cb = el('input', { type: 'checkbox', value: r.url, 'data-name': r.name });
      boxes.push(cb);
      list.append(el('label', {}, cb, el('b', { text: r.name }), el('span', { class: 'dim', text: `${r.private ? 'private' : 'public'}${r.fork ? ' · fork' : ''}${r.description ? ' · ' + r.description : ''}`.slice(0, 100) })));
    }
    if (!boxes.length) list.append(el('span', { class: 'dim', text: repos.length ? 'No match.' : 'Nothing loaded yet: press Load my repos.' }));
  };
  filter.addEventListener('input', draw);
  filter.addEventListener('keydown', (e) => { if (e.key === 'Enter') e.preventDefault(); });
  const load = el('button', { type: 'button', text: 'Load my repos', onclick: async () => {
    gh.textContent = '';
    gh.append(el('span', { class: 'badge', text: 'asking GitHub…' }));
    try {
      const r = await api('GET', '/api/github/repos');
      repos = Array.isArray(r && r.repos) ? r.repos : [];
      gh.textContent = '';
      gh.append(el('span', { class: 'badge hue-green', text: 'GitHub connected' }), el('span', { class: 'dim', text: `${repos.length} repo${repos.length === 1 ? '' : 's'}` }));
      draw();
    } catch (e) {
      gh.textContent = '';
      gh.append(el('span', { class: 'badge warn', text: 'GitHub not connected' }), el('span', { class: 'dim', text: (e && e.message ? String(e.message) : 'the box could not list your repos').slice(0, 160) }),
        el('div', { class: 'wiz-hint dim' }, 'Log the GitHub CLI in on the box: ', el('code', { class: 'doc-cmd', text: 'gh auth login' }), copyButton('gh auth login', 'the command')));
    }
  } });
  const importErr = el('div', { class: 'field-err bad', role: 'alert' });

  const blankBox = el('div', { class: 'wiz-mode', 'data-mode': 'blank' }, blankField);
  const cloneBox = el('div', { class: 'wiz-mode', 'data-mode': 'clone' }, urlField, chips, cloneField);
  const importBox = el('div', { class: 'wiz-mode', 'data-mode': 'import' }, el('div', { class: 'wiz-row' }, load, gh), filter, list, importErr);
  const go = el('button', { class: 'primary', type: 'submit', text: WIZ_GO[mode] });
  const skip = el('button', { type: 'button', text: 'Skip, no repo yet', onclick: () => { w.mode = 'none'; w.repo = ''; w.repos = 0; wizGo(2); } });
  const seg = lxSeg(WIZ_MODES, (v) => paint(v), 'Where the code comes from', 'wiz-seg');
  const paint = (v) => {
    mode = v;
    seg.set(v);
    for (const b of [blankBox, cloneBox, importBox]) b.classList.toggle('hidden', b.getAttribute('data-mode') !== v);
    go.textContent = WIZ_GO[v];
  };

  const submit = async () => {
    const p = encodeURIComponent(w.name);
    go.disabled = true;
    try {
      if (mode === 'blank') {
        const name = blankName.value.trim();
        const bad = wizNameProblem(name, 'repo');
        if (bad) { fieldError(blankField, bad, true); return; }
        await api('POST', `/api/projects/${p}/repos`, { name });
        Object.assign(w, { mode: 'blank', repo: name, repoName: name, repos: 1 });
      } else if (mode === 'clone') {
        const u = url.value.trim();
        const ub = wizUrlProblem(u);
        if (ub) { fieldError(urlField, ub, true); return; }
        const name = cloneName.value.trim() || wizDeriveName(u);
        const nb = wizNameProblem(name, 'repo');
        if (nb) { fieldError(cloneField, nb, true); return; }
        await api('POST', `/api/projects/${p}/repos`, { url: u, name });
        Object.assign(w, { mode: 'clone', repo: name, repoName: name, url: u, repos: 1 });
      } else {
        const chosen = boxes.filter((b) => b.checked).map((b) => ({ name: b.getAttribute('data-name'), url: b.getAttribute('value') }));
        importErr.textContent = '';
        if (!chosen.length) { importErr.textContent = 'Tick at least one repo, or press Load my repos first.'; return; }
        await api('POST', `/api/projects/${p}/repos/bulk`, { repos: chosen });
        Object.assign(w, { mode: 'import', repo: chosen[0].name, repos: chosen.length });
      }
    } catch (e) {
      const msg = (e && e.message) || 'the board refused it';
      if (mode === 'blank') fieldError(blankField, msg, true); else if (mode === 'clone') fieldError(urlField, msg, true); else importErr.textContent = msg;
      return;
    } finally { go.disabled = false; }
    if (typeof poll === 'function') { try { await poll(true); } catch (_) { /* the next poll shows it */ } }
    wizGo(2);
  };
  paint(mode);
  if (mode === 'clone' && url.value.trim()) wizProbeNow(url, chips, cloneName, () => nameTouched, (n) => { cloneName.value = n; });
  return el('form', { class: 'form wiz-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); submit(); } },
    seg.node, blankBox, cloneBox, importBox, el('div', { class: 'submit' }, go, skip));
}

/* The clone URL's probe: after a pause, or on leaving the field. A newer URL makes an older answer stale. The derived name fills the name field until the user typed their own. */
function wizProbeSoon(url, chips, nameInput, touched, setName) {
  clearTimeout(wizPage.timer);
  wizPage.timer = setTimeout(() => wizProbeNow(url, chips, nameInput, touched, setName), WIZ_PREFLIGHT_MS);
  if (wizPage.timer && typeof wizPage.timer.unref === 'function') wizPage.timer.unref();
}

async function wizProbeNow(url, chips, nameInput, touched, setName) {
  clearTimeout(wizPage.timer);
  const u = url.value.trim();
  const mine = ++wizPage.seq;
  if (!u) { wizChips(chips, null); return; }
  const bad = wizUrlProblem(u);
  const guess = wizDeriveName(u);
  if (guess && !touched()) setName(guess);
  if (bad) { wizChips(chips, null, false, bad); return; }
  wizChips(chips, null, true);
  let res = null;
  try { res = await api('POST', '/api/preflight/clone', { url: u }); } catch (e) {
    if (mine !== wizPage.seq) return;
    wizChips(chips, null, false, (e && e.message) || 'the box could not check this address');
    return;
  }
  if (mine !== wizPage.seq) return;
  if (!res || typeof res.reachable !== 'boolean') res = { reachable: true, default_branch: 'main', needs_auth: false, heads: ['main'], name: guess || null, error: null };      // the demo board answers {ok: true}: it has no remote to ask
  if (res.name && !touched()) setName(res.name);
  wizChips(chips, res);
}

/* ---------- step 3: defaults ---------- */

function wizPanelDefaults(w) {
  const agentSeg = lxSeg([['claude', '◆ Claude'], ['codex', '◇ Codex'], ['shell', '▸ Shell']], (v) => pickAgent(v), 'Agent', 'wiz-seg');
  if (!wizInstalled('codex')) agentSeg.disable('codex', true, 'Codex is not installed on this box');
  if (!wizInstalled('claude')) agentSeg.disable('claude', true, 'Claude is not installed on this box');

  const cs = launcherSchema('claude');
  const modelSeg = lxSeg(LX_CLAUDE_CHIPS.map((m) => [m, m]), (v) => { w.claude.model = v; modelSeg.set(v); }, 'Model', 'wiz-seg');
  const effortSeg = lxSeg((cs.efforts && cs.efforts.length ? cs.efforts : LX_EFFORTS).map((e) => [e, e]), (v) => { w.claude.effort = v; effortSeg.set(v); }, 'Effort', 'wiz-seg');
  modelSeg.set(w.claude.model);
  effortSeg.set(w.claude.effort);
  const claudeBox = el('div', { class: 'wiz-mode', 'data-agent': 'claude' }, field('Model', modelSeg.node), field('Effort', effortSeg.node));

  const xs = launcherSchema('codex');
  const cxModels = Array.isArray(xs.models) ? xs.models.map((m) => (typeof m === 'string' ? m : m && m.slug)).filter(Boolean) : [];
  const cxModel = selectEl([['', 'Default model'], ...cxModels.map((m) => [m, m])], w.codex.model || '');
  cxModel.setAttribute('aria-label', 'Codex model');
  const reasoning = { seg: null };
  const drawReasoning = () => {
    const lv = launcherReasoning(xs, w.codex.model);
    const seg = lxSeg([['', 'default'], ...lv.all.map((e) => [e, e, lv.allowed.includes(e) ? undefined : { disabled: true, title: 'This model does not offer it' }])], (v) => { w.codex.reasoning = v; seg.set(v); }, 'Reasoning', 'wiz-seg');
    if (w.codex.reasoning && !lv.allowed.includes(w.codex.reasoning)) w.codex.reasoning = '';
    seg.set(w.codex.reasoning || '');
    if (reasoning.seg && reasoning.seg.node.parentNode) reasoning.seg.node.parentNode.replaceChild(seg.node, reasoning.seg.node);
    reasoning.seg = seg;
  };
  drawReasoning();
  cxModel.addEventListener('change', () => { w.codex.model = cxModel.value; drawReasoning(); });
  const codexBox = el('div', { class: 'wiz-mode', 'data-agent': 'codex' }, field('Model', cxModel), field('Reasoning', reasoning.seg.node));
  const shellBox = el('div', { class: 'wiz-mode dim', 'data-agent': 'shell', text: 'A plain terminal in the repo: there is no model to choose.' });

  const paint = () => {
    agentSeg.set(w.agent);
    for (const b of [claudeBox, codexBox, shellBox]) b.classList.toggle('hidden', b.getAttribute('data-agent') !== w.agent);
  };
  function pickAgent(v) { w.agent = v; paint(); }
  paint();
  const next = el('button', { class: 'primary', type: 'submit', text: 'Next' });
  return el('form', { class: 'form wiz-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); wizSaveDefaults(w); wizGo(3); } },
    el('p', { class: 'dim wiz-note', text: 'What a new session in this project starts with. You can change it for any one session in the launcher.' }),
    field('Agent', agentSeg.node), claudeBox, codexBox, shellBox, el('div', { class: 'submit' }, next));
}

/* ccboard:defaults:<project>, in the shape launcherPrefs / launcherAgentPref read: {agent, claude: {model, effort}, codex: {model, reasoning}}. What was there for the other agent stays. */
function wizSaveDefaults(w) {
  const old = lxJson(lxGet(LX_DEFAULTS_KEY(w.name))) || {};
  const d = { ...old, agent: w.agent };
  if (w.agent === 'claude') d.claude = { ...(old.claude && typeof old.claude === 'object' ? old.claude : {}), model: w.claude.model, effort: w.claude.effort };
  if (w.agent === 'codex') {
    const c = { ...(old.codex && typeof old.codex === 'object' ? old.codex : {}) };
    if (w.codex.model) c.model = w.codex.model; else delete c.model;
    if (w.codex.reasoning) c.reasoning = w.codex.reasoning; else delete c.reasoning;
    d.codex = c;
  }
  lxPut(LX_DEFAULTS_KEY(w.name), JSON.stringify(d));
}

/* ---------- step 4: first prompt ---------- */

function wizPanelPrompt(w) {
  const prompt = el('textarea', { class: 'wiz-prompt', rows: 5, autocomplete: 'off', spellcheck: 'true', placeholder: 'e.g. Read the repo and tell me what it does', 'aria-label': 'First prompt' });
  prompt.value = w.prompt || '';
  prompt.addEventListener('input', () => { w.prompt = prompt.value; wizSave(w); });
  const start = el('button', { class: 'primary', type: 'submit', text: 'Start' });
  return el('form', { class: 'form wiz-form', novalidate: true, onsubmit: (e) => { e.preventDefault(); wizFinish(true); } },
    field('First prompt (optional)', prompt, 'Start opens the new-session sheet with this filled in; you pick the options there. Skip goes to the project.'),
    el('div', { class: 'submit' }, start, el('button', { type: 'button', text: 'Skip', onclick: () => wizFinish(false) })));
}

/* Done: the wizard's state goes, the first-run redirect is over; Start opens the launcher in the new repo (the project folder when there is none) with the prompt in. */
async function wizFinish(start) {
  const w = wizPage.w;
  if (!w) return;
  const prompt = String(w.prompt || '').trim();
  wizPut('local', WIZ_ONBOARDED, '1');
  wizPut('session', WIZ_KEY, null);
  if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('project', { project: w.name }));
  if (!start) return;
  if (typeof poll === 'function') { try { await poll(true); } catch (_) { /* the state may be a poll behind */ } }
  const st = typeof state !== 'undefined' ? state : null;
  const p = wizProject(w, st);
  const r = p && w.repo ? (p.repos || []).find((x) => x.name === w.repo) : null;
  if (w.repo && (!r || r.state === 'cloning' || r.state === 'clone-failed')) {
    if (typeof pageToast === 'function') pageToast(`${w.repo} is still cloning: start a session from the project page once it is done.`, 'warn');
    return;
  }
  const opened = typeof openLauncher === 'function' ? openLauncher({ project: w.name, repo: w.repo || 'root', mode: 'session', agent: w.agent, carry: prompt ? { prompt } : undefined }) : false;
  if (!opened && typeof pageToast === 'function') pageToast('The project is not on the board yet: start a session from the project page.', 'warn');
}

/* ---------- the page ---------- */

function wizResume(route, st) {
  let saved = wizLoad();
  if (saved && saved.created && st && Array.isArray(st.projects) && !st.projects.some((p) => p.name === saved.name)) saved = null;      // its project is gone: start over
  const q = route && route.query && route.query.name ? String(route.query.name) : '';
  if (q && !wizNameProblem(q, 'project')) {
    const p = st && Array.isArray(st.projects) ? st.projects.find((x) => x.name === q) : null;
    if (p && !(p.repos || []).length) return { ...wizFresh(), ...(saved && saved.name === q ? saved : {}), name: q, created: true, step: Math.max(1, saved && saved.name === q ? saved.step : 1) };
    if (!p) return { ...wizFresh(), name: q };
  }
  return saved || wizFresh();
}

registerPage('onboarding', {
  title: (route) => (route && route.params && route.params.step ? 'New project' : 'Onboarding'),
  mount(root, route) {
    if (!(route && route.params && route.params.step)) {                  // the bare #/onboarding was the plan's box checklist: that is Settings > Doctor now
      if (typeof navigate === 'function' && typeof buildHash === 'function') navigate(buildHash('settings', {}, { sec: 'doctor' }), { replace: true });
      return;
    }
    const list = el('ol', { class: 'wiz-steps' });
    const cancel = el('button', { class: 'minimal small wiz-cancel', type: 'button', onclick: () => wizLeave(true), text: 'Cancel' });
    const host = el('div', { class: 'page-narrow wiz', 'data-page': 'onboarding' },
      el('div', { class: 'page-head' }, el('h1', { text: 'New project' }), el('div', { class: 'actions' }, cancel)),
      el('p', { class: 'dim wiz-lede', text: 'A project is a folder for your repos; sessions run inside a repo. Four short steps.' }), list);
    root.append(host);
    wizPage.refs = { host, list, repoSum: null, focus: null };
    wizPage.w = wizResume(route, typeof state !== 'undefined' ? state : null);
    wizRender();
    document.title = 'New project · ccboard';
  },
  update(st) { if (wizPage.refs && wizPage.w) wizPaintSummary(wizPage.refs, wizPage.w, st); },
  onRoute() {},
  unmount() { clearTimeout(wizPage.timer); wizPage.seq += 1; wizPage.refs = null; wizPage.w = null; },
});
