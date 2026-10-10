/* ccboard agents page (#/agents), the route half of pages/agents.js: the state summary, the roster of project groups, the "started outside the board" Codex section
   and the page's own registerPage. pages/agents.js keeps what Home, the Inbox and the session peek read at the first paint (sessionCard, the account and Codex login
   helpers, agentsGroups, agentsCounts); this file loads with the agents bundle (lazy.js: LAZY_ROUTES.agents, and the settings bundle needs it for the Codex threads
   list that Settings > Agents shows). Classic script, its own globals are prefixed agents*; nothing runs at load but registerPage. */
'use strict';

/* The state summary as the chips Home's summary bar uses (class sum-seg), here a legend rather than filters: a count per state, the error and ended
   segments only while there is one. */
const AGENTS_SEGS = [['waiting', 'need you'], ['working', 'working'], ['idle', 'idle'], ['done', 'done'], ['errored', 'error'], ['ended', 'ended']];

function agentsSummaryNode() {
  const node = el('nav', { class: 'summary sumbar sumbar-static', 'aria-label': 'Sessions by state', tabindex: '0' });
  const segs = {};
  for (const [k, label] of AGENTS_SEGS) {
    const n = el('span', { class: 'sum-n' });
    const seg = el('span', { class: 'sum-seg sum-' + k + (k === 'errored' || k === 'ended' ? ' hidden' : ''), 'data-state': k }, stateGlyph(k), n, el('span', { class: 'sum-l', text: label }));
    segs[k] = { seg, n };
    node.append(seg);
  }
  node.ccSegs = segs;
  return node;
}

function agentsPatchSummary(node, list) {
  const n = agentsCounts(list);
  for (const [k] of AGENTS_SEGS) {
    const { seg, n: num } = node.ccSegs[k];
    const c = n[k] || 0;
    setTextIfChanged(num, String(c));
    seg.classList.toggle('zero', !c);
    if (k === 'errored' || k === 'ended') seg.classList.toggle('hidden', !c);
  }
}

function agentsGroupNode(g) {
  const name = el('span', { class: 'rg-name' });
  const count = el('span', { class: 'mono' });
  const list = el('div', { class: 'rg-list' });
  const node = el('section', { class: 'rgroup' }, el('div', { class: 'rgroup-head' }, name, count), list);
  const rows = makeKeyedList(list, { key: (s) => s.tmux, create: (s) => sessionCard(s, { compact: true, rich: true, autoOpen: true, perm: true }), patch: (n, s) => n.ccPatch(s) });
  node.ccPatch = (grp) => {
    setTextIfChanged(name, grp.name);
    chipHueSet(name, chipHue('project', grp.name));
    setTextIfChanged(count, `${grp.items.length} session${grp.items.length === 1 ? '' : 's'}`);
    rows.update(grp.items);
    list.classList.toggle('cv-auto', grp.items.length > CV_AUTO_ROWS);
  };
  node.ccPatch(g);
  return node;
}

/* ---------- sessions started outside the board (v0.5.12) ----------
   GET /api/external?agent=codex -> {claude: [], codex: [thread...], at}: the Codex threads of the last 14 days that the board did not start (discovery reads ~/.codex read-only;
   Desktop imports and sub-agent threads are already dropped). A thread, as far as this page reads it (every field may be absent): id (or session_id / thread_id), title (or name),
   cwd, project and repo (the server's reading of the folder, set when it is under the projects directory), openable (false: its folder is outside the projects
   directory or gone), model, effort (or reasoning_effort), tokens (or tokens_used), updated_at (epoch seconds or ms, or an ISO string), branch (or git_branch), originator,
   badge (the originator when it is not codex-tui: the Hermes agent on a box, an IDE: the row carries it as a badge rather than being hidden) and tmux (set when the thread is
   already one of the board's own sessions: such a row is not listed). Where the answer has no `codex` list the page falls back to state.external.codex (the demo board's
   fixture). Open runs POST /api/external/codex/<id>/open: a new board session resuming the thread in its folder ({tmux, ...} back, 201), refused for a folder outside the
   projects directory, so the button is disabled there with the reason in its title (a row without `openable` is judged by state.config.projects_dir). */
const agentsExt = { data: null, err: '', timer: null, busy: null, seq: 0 };
const AGENTS_EXT_MS = 30000;
const AGENTS_EXT_NOTE = 'Codex threads of the last 14 days that were not started here. Open resumes one in a board session, in its folder.';

function agentsExtEpoch(v) {
  if (typeof v === 'number' && Number.isFinite(v) && v > 0) return v > 1e11 ? v / 1000 : v;
  if (typeof v === 'string' && v) { const t = Date.parse(v); return Number.isNaN(t) ? 0 : t / 1000; }
  return 0;
}

function agentsTok(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '';
  if (v < 999.5) return String(Math.round(v));
  const units = ['k', 'M', 'B'];
  let x = v / 1000;
  let i = 0;
  while (x >= 999.5 && i < units.length - 1) { x /= 1000; i += 1; }
  return x.toFixed(1).replace(/\.0$/, '') + units[i];
}

/* One thread of /api/external as the roster draws it, or null when it cannot be resumed (no id) or is already a board session. `base` = the projects directory ('' unknown):
   inside is true / false once it is known whether the folder is under it, null while it is not. */
function agentsExtRow(raw, base) {
  const o = raw && typeof raw === 'object' ? raw : null;
  if (!o) return null;
  const str = (...keys) => { for (const k of keys) { const v = o[k]; if (typeof v === 'string' && v.trim()) return v.trim(); } return ''; };
  const id = str('id', 'session_id', 'thread_id');
  if (!id || o.tmux) return null;
  const cwd = str('cwd');
  let project = str('project');
  let repo = str('repo');
  let inside = typeof o.openable === 'boolean' ? o.openable : null;      // the server's verdict first
  if (!project && base && cwd) {
    const under = cwd === base || cwd.indexOf(base + '/') === 0;
    if (inside === null) inside = under;
    if (under) { const rel = cwd.slice(base.length + 1).split('/').filter(Boolean); project = rel[0] || ''; repo = rel[1] || ''; }
  }
  const originator = str('originator');
  const tokens = [o.tokens_used, o.tokens].find((x) => typeof x === 'number' && Number.isFinite(x) && x > 0) || 0;
  return { agent: 'codex', id, title: str('title', 'name', 'first_user_message', 'preview'), cwd, project, repo, inside, model: str('model'), effort: str('effort', 'reasoning_effort'),
    tokens, at: agentsExtEpoch(o.updated_at !== undefined ? o.updated_at : o.at), branch: str('branch', 'git_branch'),
    badge: str('badge') || (originator && originator !== 'codex-tui' ? originator : '') };
}

/* The rows to draw: what GET /api/external answered, else state.external, newest first. */
function agentsExtRows(st) {
  const base = st && st.config && typeof st.config.projects_dir === 'string' ? st.config.projects_dir.replace(/\/+$/, '') : '';
  const own = agentsExt.data && Array.isArray(agentsExt.data.codex) ? agentsExt.data.codex : null;
  const fb = st && st.external && typeof st.external === 'object' && Array.isArray(st.external.codex) ? st.external.codex : null;
  return (own || fb || []).map((e) => agentsExtRow(e, base)).filter(Boolean).sort((a, b) => b.at - a.at || a.id.localeCompare(b.id));
}

/* A folder the way a row says it: project/repo inside the projects directory, else the last two segments of the path (the whole path is the title). */
function agentsExtWhere(x) {
  if (x.project) return x.repo && x.repo !== 'root' ? `${x.project}/${x.repo}` : x.project;
  const parts = x.cwd.split('/').filter(Boolean);
  return parts.length ? (parts.length > 2 ? '…/' : '/') + parts.slice(-2).join('/') : '';
}

const XR_OUTSIDE_WHY = 'Open is off: its folder is outside the projects directory (or gone).';

function agentsExtNode(x0) {
  const cur = { x: x0, sig: '' };
  const title = el('span', { class: 'xr-title' });
  const meta = el('span', { class: 'xr-meta' });
  const age = el('time', { class: 'age xr-age', title: 'last activity' });
  const open = el('button', { class: 'small xr-open', type: 'button', onclick: () => agentsExtOpen(cur.x) });
  const why = el('span', { class: 'xr-why dim hidden', text: XR_OUTSIDE_WHY });           // one line under the title: why Open is disabled, readable without a hover (#32 d)
  const node = el('div', { class: 'xrow', 'data-ext': x0.id, 'data-agent': x0.agent },
    el('div', { class: 'xr-main' }, title, meta, why), el('div', { class: 'xr-act' }, age, open));
  node.ccPatch = (x) => {
    cur.x = x;
    setTextIfChanged(title, x.title || `thread ${x.id.slice(0, 8)}`);
    title.setAttribute('title', x.title ? `${x.title} · ${x.id}` : x.id);
    agentsAgeNode(age, x.at);
    const busy = agentsExt.busy === x.id;
    const outside = x.inside === false;
    setTextIfChanged(open, busy ? 'Opening…' : 'Open');
    open.disabled = busy || !!agentsExt.busy || outside;
    open.setAttribute('title', outside ? 'Its folder is outside the projects directory, or gone: the board only opens threads that live in a project' : 'Resume this thread in a board session');
    why.classList.toggle('hidden', !outside);
    open.setAttribute('aria-label', `Open ${x.title || 'thread ' + x.id.slice(0, 8)} in a board session`);
    const sig = JSON.stringify([x.project, x.repo, x.cwd, x.model, x.effort, x.tokens, x.branch, x.badge, outside]);
    if (sig === cur.sig) return;
    cur.sig = sig;
    meta.textContent = '';
    meta.append(el('span', { class: ['bdg', chipHue('agent', x.agent)].join(' '), title: 'Codex thread', text: `${AGENT_GLYPH.codex} Codex` }));
    if (outside) meta.append(el('span', { class: 'bdg dim xr-outside', title: XR_OUTSIDE_WHY, text: 'outside projects' }));
    if (x.badge) meta.append(el('span', { class: 'bdg mono xr-origin', title: `Started by ${x.badge}, not from a Codex terminal`, text: x.badge }));
    const where = agentsExtWhere(x);
    if (where) meta.append(el('span', { class: ['xr-where', x.project ? chipHue('project', x.project) : ''].filter(Boolean).join(' '), title: x.cwd, text: where }));
    if (x.branch) meta.append(el('span', { class: 'bdg bdg-wt mono', title: 'Git branch', text: x.branch }));
    if (x.model) meta.append(el('span', { class: ['bdg bdg-model mono', chipHue('model', x.model)].join(' '), title: 'Model', text: x.model }));
    if (x.effort) meta.append(el('span', { class: 'bdg mono', title: 'Reasoning effort', text: x.effort }));
    if (x.tokens) meta.append(el('span', { class: 'xr-tok', title: 'Tokens this thread used (forks count what they inherited)', text: `${agentsTok(x.tokens)} tokens` }));
  };
  node.ccPatch(x0);
  return node;
}

function agentsExtSection() {
  const count = el('span', { class: 'mono' });
  const list = el('div', { class: 'xlist' });
  const note = el('p', { class: 'dim unote', text: AGENTS_EXT_NOTE });
  const err = el('p', { class: 'warn unote hidden', role: 'alert' });
  const node = el('section', { class: 'xsec hidden', 'data-sec': 'external' }, el('div', { class: 'xsec-head' }, el('span', { class: 'xsec-name', text: 'Started outside the board' }), count), list, note, err);
  return { node, count, err, list: makeKeyedList(list, { key: (x) => x.id, create: agentsExtNode, patch: (n, x) => n.ccPatch(x) }) };
}

function agentsPatchExt(r, st) {
  const rows = agentsExtRows(st);
  r.ext.list.update(rows);
  setTextIfChanged(r.ext.count, `${rows.length} thread${rows.length === 1 ? '' : 's'}`);
  const err = rows.length ? '' : agentsExt.err;                          // a failed read with rows on screen keeps them; with none the section says so
  r.ext.err.classList.toggle('hidden', !err);
  setTextIfChanged(r.ext.err, err ? `Could not read the outside threads: ${err}` : '');
  r.ext.node.classList.toggle('hidden', !rows.length && !err);
}

/* The threads are asked for only on a box that has a Codex (state.agents.codex, installed): every 30 s while the Agents page is open and visible, the first time the state says so. */
function agentsExtWanted(st) {
  const c = st && st.agents && st.agents.codex;
  return !!(c && typeof c === 'object' && c.installed !== false);
}

function agentsExtEnsure(st) {
  const want = agentsExtWanted(st);
  if (want && !agentsExt.timer && typeof setInterval === 'function') {
    agentsExtLoad();
    agentsExt.timer = setInterval(() => { if (!document.hidden) agentsExtLoad(); }, AGENTS_EXT_MS);
    if (agentsExt.timer && typeof agentsExt.timer.unref === 'function') agentsExt.timer.unref();
  } else if (!want && agentsExt.timer) {
    clearInterval(agentsExt.timer);
    agentsExt.timer = null;
  }
}

function agentsExtRepaint() {
  if (agentsPage.refs && agentsPage.refs.ext) agentsPatchExt(agentsPage.refs, currentState());
  if (typeof settingsExtRepaint === 'function') settingsExtRepaint();      // Settings > Agents lists the same outside threads (v0.5.19)
}

async function agentsExtLoad() {
  const mine = ++agentsExt.seq;
  let d = null;
  try { d = await api('GET', '/api/external?agent=codex'); } catch (e) {
    if (mine !== agentsExt.seq) return;
    agentsExt.err = acctReason(e, 'the board did not answer');
    agentsExtRepaint();
    return;
  }
  if (mine !== agentsExt.seq) return;                                    // a newer load, or the page is gone
  agentsExt.data = d && typeof d === 'object' ? d : null;
  agentsExt.err = '';
  agentsExtRepaint();
}

/* Open: a new board session resuming the thread. One at a time. Success goes to the new session; a refusal is a toast with the board's reason. */
async function agentsExtOpen(x) {
  if (!x || !x.id || agentsExt.busy) return false;
  agentsExt.busy = x.id;
  agentsExtRepaint();
  let r = null;
  try { r = await api('POST', `/api/external/${encodeURIComponent(x.agent)}/${encodeURIComponent(x.id)}/open`); } catch (e) {
    agentsExt.busy = null;
    agentsExtRepaint();
    pageToast(`Could not open it: ${acctReason(e)}`, 'bad');
    return false;
  }
  agentsExt.busy = null;
  const tmux = r && typeof r.tmux === 'string' ? r.tmux : '';
  pageToast(tmux ? `Opened in the board as ${tmux}` : 'Opened in a board session', 'ok');
  agentsExtRepaint();
  if (typeof poll === 'function') { try { await poll(true); } catch (_) { /* the next poll will show it */ } }
  agentsExtLoad();
  if (tmux && typeof navigate === 'function') navigate(sessionHash(tmux));
  return true;
}

const agentsPage = { refs: null };

registerPage('agents', {
  title: 'Agents',
  mount(root) {
    const summary = agentsSummaryNode();
    const roster = el('div', { class: 'roster' });
    const none = pageEmpty('console', 'No live sessions', 'Start one from the + menu: every session on this box shows up here.');
    const ext = agentsExtSection();                                     // v0.5.12: the threads started outside the board, with an Open each
    const loading = !currentState();                                    // first paint before /api/state: skeleton rows, and no empty states yet
    if (loading) none.classList.add('hidden');
    Pages.reset();
    root.append(el('div', { class: 'agents' },
      el('div', { class: 'page-head' }, el('h1', { text: 'Agents' }), summary), ...(loading ? [Pages.skeleton(3)] : []), roster, none, ext.node));
    // a click on a row selects it, so the mouse and j / k share one selection
    roster.addEventListener('click', (e) => {
      const row = e.target && typeof e.target.closest === 'function' ? e.target.closest('.rrow') : null;
      const i = row ? Pages.items().findIndex((x) => x.tmux === row.getAttribute('data-tmux')) : -1;
      if (i >= 0) Pages.setIndex(i);
    });
    agentsPage.refs = { summary, none, ext, groups: makeKeyedList(roster, { key: (g) => 'g:' + g.key, create: agentsGroupNode, patch: (n, g) => n.ccPatch(g) }) };
    startAgeTicker();
  },
  update(st) {
    const r = agentsPage.refs;
    if (!r) return;
    Pages.dropSkeleton();
    const list = rosterSessions(st);
    agentsPatchSummary(r.summary, list);
    r.groups.update(agentsGroups(list));
    r.none.classList.toggle('hidden', list.length > 0);
    agentsExtEnsure(st);
    agentsPatchExt(r, st);
    Pages.sync();                                             // the roster may have reordered or lost the selected session
    Pages.paint();
  },
  unmount() {
    agentsPage.refs = null;
    agentsExt.seq += 1;                                                 // a load still on its way is dropped
    if (agentsExt.timer) { clearInterval(agentsExt.timer); agentsExt.timer = null; }
    stopAgeTicker();
  },
});
