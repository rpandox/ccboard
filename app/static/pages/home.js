/* ccboard home page (#/): what needs me, what is working, what did it cost.
   Top to bottom: the summary line (a count per state, each a filter: ?f=waiting|working|idle|done|errored), the away strip (what happened
   since the tab was last looked at), the inbox section (Inbox.section, only when something needs you), the schedules strip, the project
   blocks (grouped by project, state or agent; projects idle for a week fold into one 'older' block) and the usage card (Widgets.usageCard).
   The rows are the shared sessionCard from pages/agents.js in its rich form; the keyed lists keep their nodes across polls.
   Also home to the banner (its Log in goes to Settings > Accounts: accountLogin, pages/agents.js), logout, the diff and PR sheet (openTaskModal, task-sheets.js), the keyboard selection (Pages) and the v0.4
   renderers other pages still import (renderTasks, renderJobs, inboxItems).
   The header, usage pills, nav and the render() state consumer are in shell.js; the notify panel and nodes strip in pages/settings.js. */
'use strict';

/* The diff and pull request sheet of a task card (openTaskModal) and its segmented control (diffSideControl) are task-sheets.js, a lazy bundle: a card's Diff / New PR button loads it. */

/* The first-run redirect lives in pages/onboarding.js, a lazy bundle (lazy.js): only a board that was never used and has no project asks for it (once per page load; the
   file itself decides whether the wizard opens). Every other Home visit never loads the wizard. */
let homeOnboardAsked = false;

function homeOnboard(st) {
  if (typeof onboardingMaybeRedirect === 'function') { onboardingMaybeRedirect(st); return; }
  if (homeOnboardAsked || typeof Lazy === 'undefined' || !st || !st.setup || !st.setup.first_run || (Array.isArray(st.projects) && st.projects.length)) return;
  homeOnboardAsked = true;
  Lazy.load('onboarding').then(() => { if (typeof onboardingMaybeRedirect === 'function') onboardingMaybeRedirect(currentState() || st); }, (e) => console.error('ccboard onboarding', e));
}

/* One-time hint to install the board as an app (shown in a browser tab only, until dismissed: ccboard:hint:install). */
const INSTALL_HINT_KEY = 'ccboard:hint:install';
function homeInstallHint() {
  let seen = null, standalone = true;
  try { seen = localStorage.getItem(INSTALL_HINT_KEY); } catch (_) { seen = null; }
  try { standalone = isStandalone(); } catch (_) { standalone = true; }
  if (seen || standalone) return null;
  let box = null;
  const dismiss = () => { try { localStorage.setItem(INSTALL_HINT_KEY, '1'); } catch (_) { /* storage may be unavailable */ } if (box && box.remove) box.remove(); };
  const canPrompt = typeof Shell !== 'undefined' && Shell && Shell.installPrompt && typeof Shell.promptInstall === 'function';
  // a neutral callout (no second accent, no primary: the hint never competes with what needs you); text, How and the x share one row
  box = el('div', { class: 'callout install-hint', role: 'note' },                    // el() drops the null child; a raw append(null) would render "null"
    el('span', { class: 'ih-text', text: 'Install ccboard as an app: its own window, keyboard shortcuts, the title bar on desktop.' }),
    canPrompt ? el('button', { class: 'small', type: 'button', text: 'Install', onclick: () => { Shell.promptInstall(); dismiss(); } }) : null,
    el('a', { class: 'btn small', href: '#/settings?sec=app', text: 'How' }),
    el('button', { class: 'icon minimal small', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: dismiss }, ic('cross')));
  return box;
}

/* ---------- the Home page ---------- */

function homeTitle() {
  const n = state ? inboxItems().length : 0;
  return (n ? `(${n}) ` : '') + 'Home';
}

const HOME_ACTIVE_DAYS = 7;                    // a project with no session activity for this long folds into the 'older' block
const HOME_INBOX_LIMIT = 8;                    // cards of the inbox section on Home; the rest is behind the link to #/inbox
const HOME_GROUP_KEY = 'ccboard:home:group';          // project | state | agent
const HOME_COLLAPSED_KEY = 'ccboard:home:collapsed';  // JSON array of collapsed block keys: project names, 'older', 's:<state>', 'a:<agent>'
const HOME_GROUPS = [['project', 'Project'], ['state', 'State'], ['agent', 'Agent']];
const HOME_FILTERS = [['waiting', 'need you'], ['working', 'working'], ['idle', 'idle'], ['done', 'done'], ['errored', 'error']];
const HOME_STATE_ORDER = ['waiting', 'errored', 'working', 'idle', 'done', 'ended', 'unknown'];
const HOME_AGENT_ORDER = ['claude', 'codex', 'shell'];

const homePage = { refs: null, filter: '', cache: null };

function homeGroupMode() { const v = homeGet(HOME_GROUP_KEY); return HOME_GROUPS.some((g) => g[0] === v) ? v : 'project'; }

/* The collapsed block keys. Never saved: 'older' is collapsed. Saved: exactly what the person left. */
function homeCollapsedSet() {
  const raw = homeGet(HOME_COLLAPSED_KEY);
  if (raw === null) return new Set(['older']);
  try { const a = JSON.parse(raw); return new Set(Array.isArray(a) ? a.filter((x) => typeof x === 'string') : ['older']); } catch (_) { return new Set(['older']); }
}

function homeFilterOf(route) {
  const f = route && route.query ? route.query.f : '';
  return HOME_FILTERS.some((x) => x[0] === f) ? f : '';
}

function homeProjectHash(name) { try { return buildHash('project', { project: name }); } catch (_) { return '#/'; } }

function homeFilterHash(f) { try { return buildHash('home', {}, f ? { f } : {}); } catch (_) { return '#/'; } }

/* Seconds from epoch seconds, epoch milliseconds or an ISO string; 0 when it is none of them. */
function homeFmtUsd(x) { return '$' + (x >= 100 ? x.toFixed(0) : x.toFixed(2)); }

function homeCostText(p) {
  const c = state && state.cost && state.cost.value && state.cost.value.projects && state.cost.value.projects[p.name];
  if (!c || (typeof c.today !== 'number' && typeof c.week !== 'number')) return '';
  return `${homeFmtUsd(c.today || 0)} today · ${homeFmtUsd(c.week || 0)} 7d`;
}

/* ---------- the model: every session, grouped and filtered, in the order the page shows it ---------- */

/* Every session of a state payload with its project, repo and whether it sits in the project folder, and the projects with their repo groups. */

/* opts {group: project|state|agent, filter: waiting|...|'', collapsed: Set}. Returns {all, counts, blocks, mode, filter}: blocks are
   {key, kind: project|state|agent|older, name, count, total, groups: [{key, label, repo, folder, items, showHead}], collapsed, ...}; the 'older'
   block holds projects: [project blocks]. Rows inside a group are sorted attention first, then by last activity. */
function homeBuild(st, opts) {
  const o = Object.assign({ group: 'project', filter: '', collapsed: new Set() }, opts || {});
  const scan = homeScan(st);
  const now = Date.now() / 1000;
  const want = (s) => !o.filter || sessionStateKey(s) === o.filter;
  const sorted = (items) => items.filter(want).sort(sessionCompare);
  const blocks = [];
  if (o.group === 'state') {
    for (const k of HOME_STATE_ORDER) {
      const items = sorted(scan.all.filter((s) => sessionStateKey(s) === k));
      if (items.length) blocks.push({ key: 's:' + k, kind: 'state', stateKey: k, name: GLYPH_LABEL[k] || k, count: items.length, total: items.length, groups: [{ key: 'all', label: '', items, showHead: false }], collapsed: o.collapsed.has('s:' + k) });
    }
  } else if (o.group === 'agent') {
    const agents = Array.from(new Set(scan.all.map(sessionAgent)));
    agents.sort((a, b) => (HOME_AGENT_ORDER.indexOf(a) + 1 || 99) - (HOME_AGENT_ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b));
    for (const a of agents) {
      const items = sorted(scan.all.filter((s) => sessionAgent(s) === a));
      if (items.length) blocks.push({ key: 'a:' + a, kind: 'agent', agent: a, name: a, count: items.length, total: items.length, groups: [{ key: 'all', label: '', items, showHead: false }], collapsed: o.collapsed.has('a:' + a) });
    }
  } else {
    const list = [];
    for (const { p, groups } of scan.projects) {
      const every = groups.flatMap((g) => g.items);
      const activity = every.reduce((m, s) => Math.max(m, sessionActivity(s)), 0);
      const attn = every.some((s) => s.needs_attention || sessionStateKey(s) === 'waiting' || sessionStateKey(s) === 'errored');
      const working = every.some((s) => sessionStateKey(s) === 'working');
      const shown = groups.map((g) => ({ ...g, items: sorted(g.items) })).filter((g) => g.items.length);
      const single = shown.length === 1 && !shown[0].folder && shown[0].repo && shown[0].repo.name === p.name;   // the repo is the project: no sub-header
      for (const g of shown) g.showHead = !single;
      const git = single ? shown[0].repo : null;
      list.push({ key: 'p:' + p.name, kind: 'project', name: p.name, project: p, groups: shown, git, total: every.length, attn, activity,
        count: shown.reduce((n, g) => n + g.items.length, 0), collapsed: o.collapsed.has(p.name),
        // no session activity for a week and nothing that needs you or is working: it folds away (a project with no sessions at all does
        // too: Home never shows empty blocks; the sidebar lists it, and creating one from the + menu opens its project page)
        older: !attn && !working && (!activity || now - activity >= HOME_ACTIVE_DAYS * 86400) });
    }
    const order = (a, b) => (b.attn - a.attn) || (b.activity - a.activity) || a.name.localeCompare(b.name);
    const live = list.filter((b) => !b.older && (!o.filter || b.count)).sort(order);
    const old = list.filter((b) => b.older && (!o.filter || b.count)).sort(order);
    blocks.push(...live);
    if (old.length) blocks.push({ key: 'older', kind: 'older', name: 'older', count: old.length, projects: old, groups: [], collapsed: o.collapsed.has('older') });
  }
  return { all: scan.all, counts: agentsCounts(scan.all), blocks, mode: o.group, filter: o.filter };
}

function homeBuildNow(st) {
  return homeBuild(st, { group: homeGroupMode(), filter: homePage.filter, collapsed: homeCollapsedSet() });
}

/* The sessions j / k walk on Home, in screen order: the inbox section first (not while a filter is on), then the rows of every block that is open.
   A session shown twice (inbox card and project row) counts once. */
function homeInboxItems(st) {
  const items = typeof Inbox !== 'undefined' && Inbox && typeof Inbox.items === 'function' ? Inbox.items(st) : inboxItems();
  return items.slice(0, HOME_INBOX_LIMIT);
}

function homeCacheKey() { return homePage.filter + '|' + homeGroupMode() + '|' + homeGet(HOME_COLLAPSED_KEY); }

function homeItemsOf(st, model) {
  const seen = new Set();
  const out = [];
  const push = (s) => { if (!seen.has(s.tmux)) { seen.add(s.tmux); out.push(s); } };
  if (!model.filter) homeInboxItems(st).forEach(push);
  const walk = (b) => { if (!b.collapsed) for (const g of b.groups) g.items.forEach(push); };
  for (const b of model.blocks) { if (b.kind === 'older') { if (!b.collapsed) b.projects.forEach(walk); } else walk(b); }
  return out;
}

function homeVisibleItems() {
  const st = typeof state !== 'undefined' ? state : null;
  if (!st) return [];
  const key = homeCacheKey();
  if (!homePage.cache || homePage.cache.st !== st || homePage.cache.key !== key) homePage.cache = { st, key, items: homeItemsOf(st, homeBuildNow(st)) };
  return homePage.cache.items;
}

/* ---------- the away strip ---------- */

/* What happened after sinceEpoch (epoch seconds; milliseconds and ISO text work too): sessions whose state changed to done / needs you / error,
   jobs whose last run finished, tasks that reached the PR or merged column, limit episodes (limEvents: the /api/series/events?series=lim list or its
   {events} body) and registry jobs that are blocked right now. prevTasks ({task id: column} from the last visit) is optional: the task payload carries no
   change time, so a task counts when its column became pr or merged since. -> {done, needs, errored, jobs, prs, limits, blocked, total}. */
function awayDigest(st, sinceEpoch, limEvents, prevTasks) {
  const out = { done: 0, needs: 0, errored: 0, jobs: 0, prs: 0, limits: 0, blocked: 0, total: 0 };
  const t0 = homeEpoch(sinceEpoch);
  if (!st || !t0) return out;
  for (const s of rosterSessions(st)) {
    const at = homeEpoch(s.state_at);
    const k = sessionStateKey(s);
    if (at > t0) { if (k === 'done') out.done += 1; else if (k === 'waiting') out.needs += 1; else if (k === 'errored') out.errored += 1; }
    const job = sessionRegistryJob(s);
    if (job && job.state === 'blocked') out.blocked += 1;
  }
  for (const j of (st.jobs || [])) if (homeEpoch(j.last_run_at) > t0) out.jobs += 1;
  for (const t of (st.tasks || [])) {
    if (t.column !== 'pr' && t.column !== 'merged') continue;
    const pr = t.pr && typeof t.pr === 'object' ? t.pr : {};
    const at = [t.updated, t.updated_at, t.pr_at, pr.merged_at, pr.mergedAt, pr.updated_at, pr.updatedAt].map(homeEpoch).find((x) => x > 0) || 0;
    if (at ? at > t0 : (prevTasks && typeof prevTasks === 'object' && ownKey(prevTasks, String(t.id)) && prevTasks[String(t.id)] !== t.column)) out.prs += 1;
  }
  const list = Array.isArray(limEvents) ? limEvents : (limEvents && Array.isArray(limEvents.events) ? limEvents.events : []);
  const episodes = new Set();
  for (const e of list) {
    const t = homeEpoch(e && e.t);
    if (!t || t <= t0) continue;
    const m = (e && e.m) || {};
    episodes.add(`${e.key || 'other'}|${m.resets_at || Math.floor(t / 3600)}`);          // one episode: the same window reset time (or the same hour)
  }
  out.limits = episodes.size;
  out.total = out.done + out.needs + out.errored + out.jobs + out.prs + out.limits + out.blocked;
  return out;
}

/* One fetch of the limit episodes per 'since', at mount; an error leaves the strip without them. */
function homeAwayFetchLim() {
  const since = homeAway.since;
  if (!homeAway.active || !since || homeAway.limFor === since) return;
  homeAway.limFor = since;
  let p = null;
  try { p = api('GET', `/api/series/events?series=lim&since=${encodeURIComponent(new Date(since * 1000).toISOString())}`); } catch (_) { return; }
  Promise.resolve(p).then((r) => {
    if (homeAway.limFor !== since) return;
    homeAway.lim = Array.isArray(r) ? r : (r && Array.isArray(r.events) ? r.events : []);
    if (homePage.refs && typeof state !== 'undefined' && state) homePatchAway(state);
  }).catch(() => { /* limit episodes are an extra */ });
}

function homeSinceLabel(sec) {
  const d = new Date(sec * 1000);
  const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  return d.toDateString() === new Date().toDateString() ? hm : `${d.toLocaleDateString([], { weekday: 'short' })} ${hm}`;
}

function homeAwayNode() {
  const text = el('span', { class: 'away-text' });
  const list = el('span', { class: 'away-list' });
  const x = el('button', { class: 'minimal small away-x', type: 'button', 'aria-label': 'Dismiss', title: 'Dismiss', onclick: () => { homeAway.dismissed = true; if (state) homePatchAway(state); } }, ic('cross'));
  const node = el('div', { class: 'away callout hidden', role: 'note' }, text, list, x);
  node.ccText = text;
  node.ccList = list;
  return node;
}

const AWAY_ITEMS = [
  ['done', (n) => `${n} done`, '#/?f=done'], ['needs', (n) => `${n} need${n === 1 ? 's' : ''} you`, '#/?f=waiting'],
  ['errored', (n) => `${n} error${n === 1 ? '' : 's'}`, '#/?f=errored'], ['limits', (n) => `${n} limit hit${n === 1 ? '' : 's'}`, '#/usage'],
  ['prs', (n) => `${n} PR${n === 1 ? '' : 's'}`, '#/tasks'], ['jobs', (n) => `${n} scheduled run${n === 1 ? '' : 's'}`, '#/tasks'],
  ['blocked', (n) => `${n} blocked`, '#/agents'],
];

function homePatchAway(st) {
  const r = homePage.refs;
  if (!r) return;
  const show = homeAway.active && !homeAway.dismissed && !!homeAway.since;
  const d = show ? awayDigest(st, homeAway.since, homeAway.lim, homeAway.prevTasks) : null;
  const sig = d && d.total ? JSON.stringify([homeAway.since, d]) : '';
  if (r.awaySig === sig) return;
  r.awaySig = sig;
  r.away.classList.toggle('hidden', !sig);
  if (!sig) return;
  setTextIfChanged(r.away.ccText, `while you were away (since ${homeSinceLabel(homeAway.since)})`);
  r.away.ccList.textContent = '';
  for (const [k, label, href] of AWAY_ITEMS) {
    if (!d[k]) continue;
    r.away.ccList.append(el('a', { class: 'away-n', href, text: label(d[k]) }));
  }
}

/* ---------- summary line and group control ---------- */

function homeSummaryNode() {
  const node = el('nav', { class: 'summary sumbar', 'aria-label': 'Filter sessions by state' });
  const segs = {};
  for (const [f, label] of HOME_FILTERS) {
    const n = el('span', { class: 'sum-n' });
    const a = el('a', { class: 'sum-seg sum-' + f, href: homeFilterHash(f), 'data-f': f }, stateGlyph(f), n, el('span', { class: 'sum-l', text: label }));
    a.addEventListener('click', (e) => { e.preventDefault(); navigate(a.getAttribute('href')); });
    segs[f] = { a, n };
    node.append(a);
  }
  node.ccSegs = segs;
  return node;
}

function homePatchSummary(counts, filter) {
  const segs = homePage.refs.summary.ccSegs;
  for (const [f] of HOME_FILTERS) {
    const { a, n } = segs[f];
    const c = counts[f] || 0;
    setTextIfChanged(n, String(c));
    a.classList.toggle('hidden', f === 'errored' && !c && filter !== 'errored');       // the error segment appears only when there is one
    a.classList.toggle('zero', !c);
    a.classList.toggle('on', filter === f);
    if (filter === f) a.setAttribute('aria-current', 'true'); else a.removeAttribute('aria-current');
    a.setAttribute('href', homeFilterHash(filter === f ? '' : f));                      // tapping the active one clears the filter
  }
}

function homeGroupControl() {
  const btns = {};
  const node = el('div', { class: 'seg-ctl', role: 'group', 'aria-label': 'Group by' }, el('span', { class: 'seg-k dim', text: 'group' }));
  for (const [k, label] of HOME_GROUPS) {
    btns[k] = el('button', { class: 'seg-btn', type: 'button', 'data-group': k, text: label, onclick: () => homeSetGroup(k) });
    node.append(btns[k]);
  }
  node.ccBtns = btns;
  return node;
}

function homeSetGroup(k) {
  homeSet(HOME_GROUP_KEY, k);
  homeRender();
}

function homeToggleCollapsed(key) {
  const set = homeCollapsedSet();
  if (set.has(key)) set.delete(key); else set.add(key);
  homeSet(HOME_COLLAPSED_KEY, JSON.stringify([...set]));
  homeRender();
}

/* ---------- blocks, groups and rows ---------- */

/* Where the block's + session starts one (components.js launchPlace): the repo (or project folder) of the project's most recent session, else its only repo, else the
   project folder, else its first repo. null when the project has nowhere to run one. */
function homeLaunchTarget(p) {
  const hit = launchPlace(p, 'session');
  return hit ? { r: hit.repo, label: hit.repo === p.root ? `${p.name} · project folder` : `${p.name}/${hit.repo.name}` } : null;
}

/* A new session in a place: the launcher sheet (launch(), components.js) with the project and the repo filled in, so + session is one tap to Start & open. */

/* + task on a project block: the same sheet in task mode, in the repo a task went to last, else where the project works most. */
function homeNewTask(p) {
  const hit = p ? launchPlace(p, 'task') : null;
  return hit ? launch({ mode: 'task', project: p, repo: hit.repo }) : false;
}

function homeCreate(kind) {
  if (typeof Shell !== 'undefined' && Shell && typeof Shell.openCreate === 'function') Shell.openCreate(kind);
  else if (kind === 'project' && typeof Shell !== 'undefined' && Shell && typeof Shell.newProject === 'function') Shell.newProject();
}



function homeBlockNode(b) {
  const toggle = el('button', { class: 'minimal small pb-toggle', type: 'button', 'aria-expanded': 'true' });
  const name = b.kind === 'project' ? el('a', { class: 'pb-name ' + chipHue('project', b.name), href: homeProjectHash(b.name) }) : el('span', { class: 'pb-name' });
  const lead = b.kind === 'state' ? stateGlyph(b.stateKey) : (b.kind === 'agent' ? agentGlyph(b.agent) : null);
  const count = el('span', { class: 'pb-count dim' });
  const git = el('span', { class: 'pb-git dim mono' });
  const dot = el('span', { class: 'dot hidden' });
  const cost = el('span', { class: 'pb-cost dim mono' });
  const add = b.kind === 'project' ? el('button', { class: 'small pb-add', type: 'button', title: 'start a session in this project', text: '+ session' }) : null;
  const addTask = b.kind === 'project' ? el('button', { class: 'small pb-add-task', type: 'button', title: 'new task in this project: now, later or scheduled', text: '+ task' }) : null;
  const body = el('div', { class: 'pb-body' });
  const node = el('section', { class: 'pblock pb-' + b.kind, 'data-block': b.key, 'data-project': b.kind === 'project' ? b.name : null },
    el('header', { class: 'pb-head' }, toggle, lead, name, count, git, dot, cost, add, addTask), body);
  const cur = { b };
  const key = b.kind === 'project' ? b.name : b.key;
  toggle.addEventListener('click', () => homeToggleCollapsed(key));
  const project = () => cur.b.project || null;                                  // the newest payload's project object, not the one the node was built with
  if (add) add.addEventListener('click', () => { const p = project(); const t = p && homeLaunchTarget(p); if (t) homeNewSession(p, t.r); });
  if (addTask) addTask.addEventListener('click', () => homeNewTask(project()));
  const groups = makeKeyedList(body, { key: (g) => 'g:' + g.key, create: (g) => homeGroupNode(g, b.kind, project), patch: (n, g) => n.ccPatch(g) });
  node.ccPatch = (b2) => {
    cur.b = b2;
    const p = b2.project || null;
    node.classList.toggle('collapsed', !!b2.collapsed);
    node.classList.toggle('has-attn', !!b2.attn);
    setTextIfChanged(toggle, b2.collapsed ? '▸' : '▾');
    toggle.setAttribute('aria-expanded', b2.collapsed ? 'false' : 'true');
    toggle.setAttribute('aria-label', `${b2.collapsed ? 'expand' : 'collapse'} ${b2.name}`);
    patchLinkedName(name, b2.name, b2.project);
    setTextIfChanged(count, b2.count === b2.total || b2.kind !== 'project' ? `${b2.count} session${b2.count === 1 ? '' : 's'}` : `${b2.count} of ${b2.total} sessions`);
    const gr = b2.git;
    setTextIfChanged(git, gr && gr.state === 'ok' && gr.branch ? gr.branch : '');
    dot.classList.toggle('hidden', !(gr && gr.state === 'ok'));
    dot.classList.toggle('dirty', !!(gr && gr.dirty));
    setTextIfChanged(cost, p ? homeCostText(p) : '');
    if (add) add.classList.toggle('hidden', !(p && homeLaunchTarget(p)));
    if (addTask) addTask.classList.toggle('hidden', !(p && launchPlace(p, 'task')));
    body.classList.toggle('hidden', !!b2.collapsed);
    groups.update(b2.collapsed ? [] : b2.groups);                              // a collapsed block drops its rows (and their tail subscriptions)
  };
  node.ccPatch(b);
  return node;
}

function homeOlderNode(b) {
  const toggle = el('button', { class: 'pb-older-toggle', type: 'button', 'aria-expanded': 'false' });
  const body = el('div', { class: 'pb-body' });
  const node = el('section', { class: 'pblock pb-older older', 'data-block': 'older' }, el('header', { class: 'pb-head' }, toggle), body);
  toggle.addEventListener('click', () => homeToggleCollapsed('older'));
  const projects = makeKeyedList(body, { key: (x) => x.key, create: homeBlockNode, patch: (n, x) => n.ccPatch(x) });
  node.ccPatch = (b2) => {
    node.classList.toggle('collapsed', !!b2.collapsed);
    setTextIfChanged(toggle, `${b2.collapsed ? '▸' : '▾'} older (${b2.count})`);
    toggle.setAttribute('aria-expanded', b2.collapsed ? 'false' : 'true');
    toggle.setAttribute('title', `projects with no session activity for ${HOME_ACTIVE_DAYS} days`);
    body.classList.toggle('hidden', !!b2.collapsed);
    projects.update(b2.collapsed ? [] : b2.projects);
  };
  node.ccPatch(b);
  return node;
}

/* ---------- inbox section, schedules strip, usage card ---------- */

/* Without pages/inbox.js (a partial deploy, a test world): the needs-attention sessions as plain session cards. */
function homeInboxFallback(r, st) {
  if (!r.inboxList) r.inboxList = makeKeyedList(r.inboxHost, { key: (s) => s.tmux, create: (s) => sessionCard(s, { showProject: true, perm: true, cls: 'inbox-item' }), patch: (n, s) => n.ccPatch(s) });
  const items = inboxItems();
  r.inboxList.update(items.slice(0, HOME_INBOX_LIMIT));
  return Math.min(items.length, HOME_INBOX_LIMIT);
}

/* The inbox section: Inbox.section fills #inbox-home (and shows or hides it); a filter shows rows only, so the section would list the same sessions twice. */
function homePatchInbox(st) {
  const r = homePage.refs;
  if (homePage.filter) { r.inboxHost.classList.add('hidden'); return; }
  if (typeof Inbox !== 'undefined' && Inbox && typeof Inbox.section === 'function') { Inbox.section(r.inboxHost, st, { limit: HOME_INBOX_LIMIT, link: '#/inbox' }); return; }
  r.inboxHost.classList.toggle('hidden', !homeInboxFallback(r, st));
}

function homeOpenSchedules() {
  if (typeof renderJobs !== 'function' && typeof Lazy !== 'undefined') return Lazy.run('taskui', homeOpenSchedules, 'schedules');
  const sec = el('section', { id: 'jobs', class: 'jobs-sheet' });
  openSheet({ title: 'Schedules', body: sec });
  renderJobs();
}

function homeSchedNode() {
  const list = el('span', { class: 'sched-list' });
  const all = el('button', { class: 'minimal small sched-all', type: 'button', onclick: homeOpenSchedules, text: 'All' });
  const node = el('div', { class: 'sched hidden' }, el('span', { class: 'sched-k dim', text: 'Next runs' }), list, all);
  node.ccList = list;
  node.ccAll = all;
  return node;
}

function homePatchSched(st) {
  const r = homePage.refs;
  const jobs = (st.jobs || []).filter((j) => j.enabled && j.next_run_at);
  const next = jobs.map((j) => ({ j, at: homeEpoch(j.next_run_at) })).filter((x) => x.at).sort((a, b) => a.at - b.at).slice(0, 3);
  const total = (st.jobs || []).length;
  const sig = JSON.stringify([next.map((x) => [x.j.id, x.j.name, x.at, x.j.agent]), total]);
  r.sched.classList.toggle('hidden', !total);
  if (r.schedSig === sig) return;
  r.schedSig = sig;
  const list = r.sched.ccList;
  list.textContent = '';
  if (!next.length) list.append(el('span', { class: 'dim', text: `no upcoming runs (${total} schedule${total === 1 ? '' : 's'})` }));
  for (const { j, at } of next) {
    const when = el('time', { class: 'until mono', 'data-epoch': String(Math.floor(at)), text: fmtIn(at) });
    list.append(el('button', { class: 'sched-item', type: 'button', title: `${j.name} · ${j.project}/${j.repo}`, onclick: homeOpenSchedules },
      agentGlyph(j.agent || 'claude'), el('span', { class: 'sched-name', text: j.name }), el('span', { class: 'dim' }, 'in ', when)));
  }
  setTextIfChanged(r.sched.ccAll, `All ${total}`);
}

/* ---------- mount / update / route / unmount ---------- */

function homeRender(st) {
  const r = homePage.refs;
  const s = st || (typeof state !== 'undefined' ? state : null);
  if (!r || !s) return;
  Pages.dropSkeleton();
  if (r.usageHost) { r.usageHost.classList.remove('hidden'); homeUsageWatch(r); }
  const pending = s.settings_pending || {};
  if (pending.count || r.settingsPending) {
    const paint = () => { if (homePage.refs === r) settingsHomePending(r, state.settings_pending || {}); };
    if (typeof settingsHomePending === 'function') paint(); else if (typeof lazyRun === 'function') lazyRun('restartnotice', paint);
  }
  homeAwayTouch(s);
  homeAwayFetchLim();
  const model = homeBuildNow(s);
  homePage.cache = { st: s, key: homeCacheKey(), items: homeItemsOf(s, model) };      // Pages.sync() below reads it
  homePatchSummary(model.counts, model.filter);
  for (const [k, btn] of Object.entries(r.group.ccBtns)) btn.setAttribute('aria-pressed', model.mode === k ? 'true' : 'false');
  homePatchAway(s);
  if (!teamUse(() => TeamView.homePaint(r)) && r.team) { r.team.remove(); r.team = null; }                          // Team (issue #200): one quiet line while a repository is shared; with none shared there is no node at all
  Nodes.use(() => Nodes.slot('strip', r.away));                                     // paired nodes: the Nodes strip after the away line, before the inbox (nodes-hub.js)
  homePatchInbox(s);
  Nodes.use(() => Nodes.slot('inbox', r.inboxHost, { limit: HOME_INBOX_LIMIT }));   // and the inbox items of other nodes under the local ones
  homePatchSched(s);
  r.blocks.update(model.blocks);
  const noProjects = !(s.projects || []).length;
  r.empty.classList.toggle('hidden', !noProjects);
  r.blocksHost.classList.toggle('hidden', noProjects);
  Nodes.use(() => Nodes.slot('sessions', r.empty, { filter: model.filter, collapsed: homeCollapsedSet().has('x:nodes'), toggle: () => homeToggleCollapsed('x:nodes') }));   // #82
  if (r.scanSlow) r.scanSlow.classList.toggle('hidden', !(s.scan_slow === true && !noProjects && !ui.offline));   // offline cached values have their own banner
  // Nothing live and no filter: say so and offer the next step. Idle projects fold into 'older', which is still a block, so this
  // cannot hang off "no blocks" (that left Home with only zero chips and a closed 'older' line).
  const live = HOME_FILTERS.reduce((a, [k]) => a + (model.counts[k] || 0), 0);
  const idleEmpty = !noProjects && !model.filter && live === 0;
  if (idleEmpty) {
    const n = (s.projects || []).length;
    setTextIfChanged(r.idleHint, `${n} project${n === 1 ? '' : 's'}, none with a session running. Start a session in a repo, or add a task to run now or later.`);
  }
  r.idle.classList.toggle('hidden', !idleEmpty);
  let note = '';
  if (!noProjects && !model.blocks.length && model.filter) {
    const f = HOME_FILTERS.find((x) => x[0] === model.filter);
    const EMPTY_WORD = { waiting: 'waiting for you', working: 'working', idle: 'idle', done: 'finished', errored: 'failing' };
    note = `Nothing is ${(f && EMPTY_WORD[f[0]]) || (f && f[1]) || model.filter} right now.`;
  }
  setTextIfChanged(r.note, note);
  r.note.classList.toggle('hidden', !note);
  r.clear.classList.toggle('hidden', !(note && model.filter));
  if (r.usage) { try { r.usage.update(s); } catch (e) { console.error('ccboard usage card', e); } }
  if ($('#jobs')) renderJobs();                                                     // the schedules sheet: its Run / Disable / Delete buttons repaint with the poll
  sessionTailSweep();
  Pages.sync();
  Pages.paint();
  refreshTitle();
}

registerPage('home', {
  title: () => homeTitle(),
  mount(root, route) {
    Pages.reset();
    homePage.filter = homeFilterOf(route);
    homePage.cache = null;
    homeAwayInit(Date.now() / 1000);                                                  // the last visit, before this render writes the new one
    const summary = homeSummaryNode();
    const group = homeGroupControl();
    const away = homeAwayNode();
    const inboxHost = el('section', { id: 'inbox-home', class: 'home-inbox hidden' });          // Inbox.section builds its head and cards in here
    const sched = homeSchedNode();
    const blocksHost = el('div', { class: 'pblocks' });
    // #31: a quiet caption above the project list while the server backs off its scan on a busy box (state.scan_slow): branch and
    // dirty may be up to 10 s old. Not a banner, not a toast, and it never says failed.
    const scanSlow = el('p', { class: 'home-scan-slow dim hidden', text: 'scan slowed (box is busy)' });
    const note = el('p', { class: 'home-note dim hidden' });
    const clear = el('a', { class: 'btn small hidden', href: '#/', text: 'Clear filter' });
    const empty = pageEmpty('folder-close', 'No projects yet', 'A project is a folder of repos; sessions run inside a repo.');
    empty.append(el('button', { class: 'primary', type: 'button', onclick: () => homeCreate('project'), text: 'Add project' }));
    empty.classList.add('hidden');
    // projects but nothing live: the same empty-state look, with the two ways to start (one filled primary)
    const idle = pageEmpty('console', 'No sessions running', '');
    const idleHint = idle.querySelector('.empty-hint') || el('div', { class: 'empty-hint dim' });
    if (!idleHint.parentNode) idle.append(idleHint);
    idle.append(el('div', { class: 'home-idle-actions' },
      el('button', { class: 'primary', type: 'button', onclick: () => Shell.openCreate('session'), text: 'New session' }),
      el('button', { type: 'button', onclick: () => Shell.openCreate('task'), text: 'New task' })));
    idle.classList.add('home-idle', 'hidden');
    // The usage card sits under everything else, so it stays out until the first state: with the skeleton above it, it would be on screen and then pushed a screen down
    // by the cards and projects that arrive (#103, layout shift 0.8 at 390 px).
    const usageHost = el('div', { id: 'usage-home', class: currentState() ? 'home-usage' : 'home-usage hidden' });
    root.append(
      el('div', { class: 'page-head' }, el('h1', { text: 'Home' }), el('div', { class: 'actions' }, group)),
      summary, homeInstallHint(),
      ...(currentState() ? [] : [Pages.skeleton(3)]),                                    // first paint: rows until /api/state arrives
      away, inboxHost, sched, scanSlow, idle, blocksHost, note, clear, empty, usageHost);
    // a click on a row or card selects it, so the mouse and j / k share one selection
    root.addEventListener('click', (e) => {
      const row = e.target && typeof e.target.closest === 'function' ? e.target.closest('.rrow, .inbox-card, .inbox-item') : null;
      if (!row || !(blocksHost.contains(row) || inboxHost.contains(row))) return;
      const i = Pages.items().findIndex((x) => x.tmux === row.getAttribute('data-tmux'));
      if (i >= 0) Pages.setIndex(i);
    });
    let usage = null;
    if (typeof Widgets !== 'undefined' && Widgets && typeof Widgets.usageCard === 'function') { try { usage = Widgets.usageCard(usageHost); } catch (e) { console.error('ccboard usage card', e); } }
    homePage.refs = { summary, group, away, team: null, awaySig: null, inboxHost, usageHost, inboxList: null, sched, schedSig: null, scanSlow, idle, idleHint, blocksHost, note, clear, empty, usage,
      blocks: makeKeyedList(blocksHost, { key: (b) => b.key, create: (b) => (b.kind === 'older' ? homeOlderNode(b) : homeBlockNode(b)), patch: (n, b) => n.ccPatch(b) }) };
    startAgeTicker();
  },
  update(st) { homeRender(st); homeOnboard(st); },                                  // a board never used, with no project: the wizard once (pages/onboarding.js)
  onRoute(route) {                                                                    // #/ <-> #/?f=waiting: the same page, a different filter (no remount, no lost subscriptions)
    homePage.filter = homeFilterOf(route);
    homePage.cache = null;
    homeRender();
  },
  unmount() {
    sessionTailStopAll();
    const r = homePage.refs;
    if (r && r.usage && typeof r.usage.destroy === 'function') { try { r.usage.destroy(); } catch (e) { console.error('ccboard usage card', e); } }
    if (r && r.usageObserver) r.usageObserver.disconnect();
    homePage.refs = null;
    homePage.cache = null;
    stopAgeTicker();
  },
});

/* The usage card is below the session list. Fetch its component as it approaches
   the viewport, including empty boards where it is visible immediately. */
function homeUsageWatch(r) {
  if (r.usage || r.usageWatching || typeof Lazy === 'undefined') return;
  r.usageWatching = true;
  const load = () => {
    if (r.usageObserver) { r.usageObserver.disconnect(); r.usageObserver = null; }
    Lazy.load('widgets').then(() => {
      if (homePage.refs !== r || r.usage) return;
      r.usage = Widgets.usageCard(r.usageHost);
      if (typeof state !== 'undefined' && state) r.usage.update(state);
    }, (err) => { r.usageWatching = false; Lazy.fail('the usage card', err); });
  };
  if (typeof IntersectionObserver === 'undefined') { load(); return; }
  r.usageObserver = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) load(); }, { root: $('#main'), rootMargin: '200px' });
  r.usageObserver.observe(r.usageHost);
}
