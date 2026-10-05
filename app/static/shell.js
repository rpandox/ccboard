/* ccboard shell (v0.5.3): the topbar, the sidebar tree (the 48 px rail on medium widths), the drawer, the bottom nav, the width-driven shell modes, the
   keyboard shortcuts and the state consumers renderHeader() / renderUsage() / renderShell() / render() that core.js's poll() calls.
   Everything is built once by installShell() (main.js calls it) and patched in place on every poll: nothing here is re-created per poll, and the
   project tree is a keyed reconcile, so a focused row survives a state refresh. Classic script; one namespace (Shell) plus the contract functions. */
'use strict';

const Shell = {
  mode: 'compact',          // compact < 600 | medium 600-839 | expanded 840-1199 | large >= 1200 (matchMedia only, no UA sniffing)
  sbOpen: true,             // expanded/large: the 260 px sidebar (true) or the rail (false); persisted as localStorage ccboard:sb = '1' | '0'
  open: new Set(),          // expanded tree nodes, persisted as ccboard:sb:open (JSON array of keys: 'p:<project>', 'older')
  refs: null,               // topbar / sidebar / bnav nodes, set by installShell()
  trees: [],                // the tree roots: one in #sidebar, one in #drawer
  cache: null,              // { st, model }: the last model built from a state object
  badge: -1,                // the last count handed to navigator.setAppBadge
  crumbSig: '',
  dirs: new Map(),          // sidebar directory listings of the current project: 'd:<project>/<repo>/<path>' -> {entries} | {error}
  dirLoading: new Set(),    // the keys whose listing is on its way
  formWatch: null,          // called at the end of render(): lets a launcher form inside the sheet close it once it succeeded
  mq: null,
  installPrompt: null,      // the deferred beforeinstallprompt event (Chromium), kept for Settings > App; null once used, after appinstalled, or where there is none
  sbW: 260,                 // expanded/large: the width of the full sidebar (SB_MIN..SB_MAX), persisted as localStorage ccboard:sb:w
  SB_MIN: 200,
  SB_MAX: 400,
  SB_DEFAULT: 260,
  OLDER_DAYS: 14,
  NAV: [['home', 'Home', 'home', '#/'], ['inbox', 'Needs you', 'notifications', '#/inbox'], ['agents', 'Agents', 'console', '#/agents'],
        ['tasks', 'Tasks', 'git-branch', '#/tasks'], ['usage', 'Usage', 'chart', '#/usage'], ['memory', 'Memory', 'database', '#/memory'],
        ['settings', 'Settings', 'cog', '#/settings']],
  CRUMB_NAMES: { inbox: 'Needs you', agents: 'Agents', tasks: 'Tasks', usage: 'Usage', memory: 'Memory', settings: 'Settings', search: 'Search', quad: 'Quad', onboarding: 'Onboarding' },
};

/* ---------- small helpers ---------- */

Shell.hash = function (id, params, query) {
  try { if (typeof buildHash === 'function') return buildHash(id, params, query); } catch (_) { return '#/'; }
  return id === 'home' ? '#/' : '#/' + id;
};

Shell.go = function (hash) { if (typeof navigate === 'function') navigate(hash); else location.hash = hash; };

Shell.setVar = function (node, name, value) { try { node.style.setProperty(name, value); } catch (_) { /* no CSSOM */ } };

Shell.load = function () {
  try { Shell.sbOpen = localStorage.getItem('ccboard:sb') !== '0'; } catch (_) { Shell.sbOpen = true; }
  try { Shell.sbW = Shell.clampSbW(parseInt(localStorage.getItem('ccboard:sb:w'), 10)); } catch (_) { Shell.sbW = Shell.SB_DEFAULT; }
  try {
    const a = JSON.parse(localStorage.getItem('ccboard:sb:open') || '[]');
    Shell.open = new Set(Array.isArray(a) ? a.filter((x) => typeof x === 'string').slice(0, 300) : []);
  } catch (_) { Shell.open = new Set(); }
};

Shell.saveOpen = function () { try { localStorage.setItem('ccboard:sb:open', JSON.stringify([...Shell.open])); } catch (_) { /* storage may be unavailable */ } };

Shell.wide = function () { return Shell.mode === 'expanded' || Shell.mode === 'large'; };

/* Keyed reconcile: children of `parent` carry data-key; `items` is [{key, ...}] in the wanted order. Existing nodes are patched in place
   and only moved when out of position, so a focused row is never re-created. */
Shell.sync = function (parent, items, make, patch) {
  const have = new Map();
  for (const c of parent.children) have.set(c.getAttribute('data-key'), c);
  let i = 0;
  for (const it of items) {
    let n = have.get(it.key);
    if (n) have.delete(it.key); else n = make(it);
    patch(n, it);
    if (parent.children[i] !== n) parent.insertBefore(n, parent.children[i] || null);
    i += 1;
  }
  for (const n of have.values()) n.remove();
};

/* ---------- the model: one pass over the state, shared by the topbar, the trees and the nav counts ---------- */

const SHELL_STATE_RANK = { waiting: 0, errored: 1, working: 2, idle: 3, done: 4 };

Shell.sessionItem = function (s, repo) {
  const t = Math.max((s.state_at ? Date.parse(s.state_at) / 1000 : 0) || 0, s.created || 0);
  return { key: 's:' + s.tmux, kind: 'sess', tmux: s.tmux, name: s.name, repo, state: s.state || 'unknown', agent: sessionAgent(s), needs: !!s.needs_attention, at: t };
};

Shell.model = function (st) {
  if (Shell.cache && Shell.cache.st === st) return Shell.cache.model;
  const now = Date.now() / 1000;
  let attn = 0;
  let live = 0;
  const projects = [];
  for (const p of (st && st.projects) || []) {
    const sess = [];
    for (const r of repoGroups(p)) for (const s of r.sessions || []) sess.push(Shell.sessionItem(s, r.name));
    for (const s of p.orphan_sessions || []) sess.push(Shell.sessionItem(s, s.repo || '?'));
    sess.sort((a, b) => ((a.needs ? 0 : 1) - (b.needs ? 0 : 1)) || ((SHELL_STATE_RANK[a.state] ?? 5) - (SHELL_STATE_RANK[b.state] ?? 5)) || a.name.localeCompare(b.name));
    const needs = sess.filter((x) => x.needs).length;
    attn += needs;
    live += sess.filter((x) => x.state !== 'ended').length;
    const activity = sess.reduce((m, x) => Math.max(m, x.at), 0);
    const kids = [...sess, ...(p.repos || []).map((r) => ({ key: 'r:' + p.name + '/' + r.name, kind: 'repo', project: p.name, name: r.name, state: r.state }))];
    const folder = !((p.repos || []).length === 1 && p.repos[0].name === p.name);     // the project folder is its own node unless it is the one repo
    projects.push({ key: 'p:' + p.name, kind: 'proj', name: p.name, attn: needs, count: sess.length, activity, kids, folder,
      older: !needs && activity > 0 && now - activity >= Shell.OLDER_DAYS * 86400 });   // no activity at all (a new project) stays in the main list
  }
  projects.sort((a, b) => (b.attn - a.attn) || (b.activity - a.activity) || a.name.localeCompare(b.name));
  const main = projects.filter((p) => !p.older);
  const older = projects.filter((p) => p.older);
  const tasks = ((st && st.tasks) || []).filter((t) => !t.archived_at).length;
  // one definition of 'needs you' everywhere: the inbox list (pending permissions and blocked jobs included), else the attention flags
  const attnAll = (typeof Inbox !== 'undefined' && Inbox && typeof Inbox.items === 'function') ? Inbox.items(st).length : attn;
  const model = { attn: attnAll, live, tasks, main, older, empty: !projects.length };
  Shell.cache = { st, model };
  return model;
};

/* ---------- topbar ---------- */

Shell.pill = function (key, label) {
  const pl = el('b', { class: 'pl', text: label });
  const pv = el('span', { class: 'pv' });
  const pr = el('span', { class: 'pr' });
  const link = key === '5h' || key === '7d' || key === 'codex';              // one tap to the Usage page (v0.5.17); the Codex pill lands on its Codex tab (v0.5.12)
  const n = el(link ? 'a' : 'span', { class: 'pill hidden', 'data-pill': key, href: link ? (key === 'codex' ? '#/usage?agent=codex' : '#/usage') : null, title: link ? 'Usage' : null }, pl, pv, pr);
  n.pl = pl; n.pv = pv; n.pr = pr;
  return n;
};

Shell.buildTopbar = function (bar) {
  const R = Shell.refs;
  R.bar = bar;                                                                 // patchAccount marks it .has-acct (shell.css gives the chip its room in the compact shell)
  R.navBtn = el('button', { class: 'icon minimal nav-toggle', type: 'button', 'aria-label': 'Open menu', title: 'Menu', onclick: () => Shell.navToggle() }, ic('menu'));
  R.crumbs = el('div', { id: 'crumbs', class: 'crumbs' });
  R.p5 = Shell.pill('5h', '5H');
  R.p7 = Shell.pill('7d', '7D');
  R.pCodex = Shell.pill('codex', 'CX');
  R.pSpend = Shell.pill('spend', 'SPEND');
  R.acctT = el('b', { class: 'pl' });
  R.acct = el('a', { class: 'pill acct hidden', 'data-pill': 'acct', href: '#/usage' }, R.acctT);       // v0.5.17b: which subscription account the pills below belong to (only with more than one)
  R.pills = el('div', { id: 'pills', class: 'pills' }, R.acct, R.p5, R.p7, R.pCodex, R.pSpend);
  const g = stateGlyph('waiting');
  g.setAttribute('aria-hidden', 'true'); g.removeAttribute('role'); g.removeAttribute('title');
  R.inboxN = el('span', { class: 'pv' });
  R.inbox = el('a', { id: 'inbox-pill', class: 'pill link', href: '#/inbox', title: 'Needs you', 'aria-label': 'Needs you: 0' }, g, R.inboxN);
  R.adClaude = el('a', { class: 'ad claude', href: '#/settings?sec=agents', text: AGENT_GLYPH.claude });
  R.adCodex = el('a', { class: 'ad codex hidden', href: '#/settings?sec=agents', text: AGENT_GLYPH.codex });
  R.dots = el('span', { id: 'agent-dots', class: 'agent-dots' }, R.adClaude, R.adCodex);
  R.search = el('input', { id: 'hdr-search', type: 'search', placeholder: 'search transcripts…', 'aria-label': 'Search transcripts', autocomplete: 'off', enterkeyhint: 'search' });
  R.search.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const q = R.search.value.trim();
      if (q) Shell.go('#/search?q=' + encodeURIComponent(q));
      R.search.blur(); bar.classList.remove('search-open');
    } else if (e.key === 'Escape') { R.search.value = ''; R.search.blur(); bar.classList.remove('search-open'); }
  });
  R.searchBtn = el('button', { class: 'icon minimal search-btn', type: 'button', 'aria-label': 'Search', title: 'Search transcripts  /', onclick: () => Shell.focusSearch() }, ic('search'));
  R.plus = el('button', { class: 'icon minimal create', type: 'button', 'aria-label': 'Create', title: 'Create' }, ic('plus'));
  menu(R.plus, () => Shell.createItems());
  R.bell = el('button', { class: 'icon minimal bell', type: 'button', 'aria-label': 'Notifications', title: 'Notifications', onclick: () => Shell.go('#/settings?sec=notify') }, ic('notifications'));
  R.refresh = el('button', { class: 'icon minimal refresh', type: 'button', 'aria-label': 'Refresh now', title: 'Refresh now', onclick: () => refreshNow() }, ic('refresh'));
  bar.append(R.navBtn, el('a', { class: 'brand', href: '#/', text: 'ccboard' }), R.crumbs, el('span', { class: 'spacer' }),
    R.pills, R.inbox, R.dots, R.search, R.searchBtn, R.plus, R.bell, R.refresh);
};

Shell.focusSearch = function () {
  const R = Shell.refs;
  if (!R) return;
  R.search.parentNode.classList.add('search-open');
  R.search.focus();
  R.search.select();
};

/* Codex rate limits: state.usage_codex = {value: {limit_id, plan_type, primary, secondary, credits, reached, observed_at, account}, at}, a window being
   {used_percent, window_minutes, resets_at}. Also accepts a bare window, a list of them or an object of them. The pill shows ONE window, the longest one the plan
   reports (weekly: 10080 minutes; a plan with a 5-hour window as well still shows the weekly one), labelled from its window_minutes. */
Shell.codexWindow = function (u) {
  if (!u) return null;
  const v = u.value !== undefined ? u.value : u;
  const cand = [];
  const pct = (w) => (typeof w.used_percentage === 'number' ? w.used_percentage : w.used_percent);
  const add = (w) => { if (w && typeof w === 'object' && typeof pct(w) === 'number' && Number.isFinite(pct(w))) cand.push(w); };
  if (Array.isArray(v)) v.forEach(add);
  else { add(v); if (v && typeof v === 'object') Object.values(v).forEach(add); }
  if (!cand.length) return null;
  cand.sort((a, b) => (b.window_minutes || 0) - (a.window_minutes || 0));
  const w = cand[0];
  const rec = v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  return { used_percentage: pct(w), resets_at: w.resets_at, minutes: w.window_minutes, plan: typeof rec.plan_type === 'string' ? rec.plan_type : '',
    reached: rec.reached === true, account: typeof rec.account === 'string' ? rec.account : '' };
};

Shell.windowLabel = function (m) {
  if (!m) return '';
  if (m === 10080) return '7D';
  if (m === 300) return '5H';
  if (m % 1440 === 0) return `${m / 1440}D`;
  if (m % 60 === 0) return `${m / 60}H`;
  return `${m}m`;
};

/* 'weekly' for 10080 minutes, '5-hour' for 300, '3-day' / '2-hour' for the rest, 'window' when the plan names none: the word of the pill's title. */
Shell.windowName = function (m) {
  if (!m) return 'window';
  if (m === 10080) return 'weekly';
  if (m % 1440 === 0) return `${m / 1440}-day`;
  if (m % 60 === 0) return `${m / 60}-hour`;
  return `${m}-minute`;
};

Shell.patchPill = function (node, w, what) {
  if (!w || typeof w.used_percentage !== 'number') { node.classList.add('hidden'); return; }
  const pct = Math.max(0, Math.min(100, w.used_percentage));
  node.classList.remove('hidden');
  node.classList.toggle('ok', pct < 60);
  node.classList.toggle('warn', pct >= 60 && pct < 85);
  node.classList.toggle('bad', pct >= 85);
  setText(node.pv, `${Math.round(pct)}%`);
  setText(node.pr, w.resets_at ? fmtIn(w.resets_at) : '');
  node.setAttribute('title', `${what}: ${Math.round(pct)}% used` + (w.resets_at ? ` · resets in ${fmtIn(w.resets_at)}` : ''));
  Shell.setVar(node, '--pct', String(Math.round(pct)));
};

/* The account chip of the topbar (v0.5.17b), or null with fewer than two accounts or no current one. {key, name, text, hue, title, amber}:
   text = the first two letters of the label (else the name, else the email), hue = chipHue('account', key) (one subscription, one hue everywhere).
   The chip is the amber tint, and its title names the better account and the window, when the current account is at 85 % or more of a window and another
   account has strictly more of THAT window left: the same rule as the Usage page's 'most room' line (Usage.room): the 7-day window is checked first, then the
   5-hour one, and the first that trips decides: '<label> has 80 % of the 5-hour window left'. A window that has reset since its last reading counts as
   empty (0 % used), an account with no reading of the window is not offered. The current account's own numbers are the pills' (state.usage), the others'
   come from state.accounts. */
Shell.accountChip = function (st, now) {
  if (typeof agentsAccounts !== 'function' || typeof agentsAcctUsedNow !== 'function') return null;
  const list = agentsAccounts(st);
  if (list.length < 2) return null;
  const t = typeof now === 'number' ? now : Date.now() / 1000;
  const curKey = st.accounts && st.accounts.current;
  const cur = list.find((a) => a.current) || list.find((a) => a.key === curKey) || null;
  if (!cur) return null;
  const name = agentsAcctName(cur);
  let move = null;                                                   // {who, left, win}: where to go instead
  for (const [win, nm] of [['7d', '7-day'], ['5h', '5-hour']]) {
    const used = agentsAcctUsedNow(st, cur, win, t);
    if (used === null || used < 85) continue;
    let best = null;
    for (const a of list) {
      if (a === cur) continue;
      const u = agentsAcctUsed(a, win, t);
      if (u !== null && (!best || 100 - u > best.left)) best = { a, left: 100 - u };
    }
    if (best && best.left > 100 - used) { move = { who: agentsAcctName(best.a), left: best.left, win: nm }; break; }
  }
  const letters = Array.from(name.replace(/\s+/g, '')).slice(0, 2).join('');
  return {
    key: cur.key, name, amber: !!move,
    text: letters.charAt(0).toUpperCase() + letters.slice(1),
    hue: move ? 'hue-amber' : chipHue('account', cur.key),
    title: move ? `${move.who} has ${Math.round(move.left)} % of the ${move.win} window left` : `Account: ${name}${cur.plan ? ' (' + cur.plan + ')' : ''} · usage per account`,
  };
};

/* Paint the chip; returns what accountChip answered (patchUsage titles the pills with its name). */
Shell.patchAccount = function (st) {
  const R = Shell.refs;
  if (!R || !R.acct) return null;
  let c = null;
  try { c = Shell.accountChip(st); } catch (e) { console.error('ccboard account chip', e); }
  R.acct.classList.toggle('hidden', !c);
  if (R.bar) R.bar.classList.toggle('has-acct', !!c);                  // the compact topbar drops the brand text for the chip (shell.css)
  if (!c) return null;
  setText(R.acctT, c.text);
  if (typeof chipHueSet === 'function') chipHueSet(R.acct, c.hue);
  R.acct.setAttribute('title', c.title);
  R.acct.setAttribute('aria-label', c.amber ? `${c.name}: ${c.title}` : c.title);      // the amber title alone would not say whose numbers the pills show
  return c;
};

Shell.patchUsage = function (st) {
  const R = Shell.refs;
  if (!R || !st) return;
  const rl = (st.usage && st.usage.value) || {};
  const who = Shell.patchAccount(st);
  const pre = who ? who.name + ' · ' : '';                       // with several accounts the pill title names the one whose numbers it shows
  Shell.patchPill(R.p5, rl.five_hour, pre + '5-hour window');
  Shell.patchPill(R.p7, rl.seven_day, pre + 'weekly window');
  Shell.patchPill(R.pSpend, rl.spend_limit, 'spend limit');
  Shell.patchCodex(st);
};

/* The CX pill (v0.5.12): ONE window of state.usage_codex, labelled from its window_minutes ('CX 7D' for the weekly window of 10080 minutes, 'CX 5H' for 300). A reading
   whose reset instant has passed is a window that rolled over (nothing counted in the new one yet): it shows 0 %, like the Usage page's account rows. A reached limit
   makes it red whatever the percentage says, with several Codex accounts the title names the one the reading belongs to, and the pill is the way to the Codex tab. */
Shell.patchCodex = function (st) {
  const R = Shell.refs;
  const cx = Shell.codexWindow(st.usage_codex);
  if (!cx) { Shell.patchPill(R.pCodex, null, ''); return; }
  const rolled = typeof cx.resets_at === 'number' && cx.resets_at > 0 && cx.resets_at <= Date.now() / 1000;
  let who = '';
  try {
    if (typeof cxAccounts === 'function' && typeof cxState === 'function' && cxAccounts(st).length > 1) {
      const key = cx.account || (cxState(st) || {}).current;
      const a = cxAccounts(st).find((x) => x.key === key);
      if (a) who = cxName(a) + ' · ';
    }
  } catch (e) { console.error('ccboard codex pill', e); }
  Shell.patchPill(R.pCodex, rolled ? { ...cx, used_percentage: 0, resets_at: 0 } : cx, `${who}Codex ${Shell.windowName(cx.minutes)} window`);
  setText(R.pCodex.pl, `CX ${Shell.windowLabel(cx.minutes)}`.trim());
  const extra = [cx.plan ? `${cx.plan} plan` : '', rolled ? 'the window rolled over since the last reading' : '', cx.reached ? 'limit reached' : ''].filter(Boolean);
  if (cx.reached) { R.pCodex.classList.remove('ok', 'warn'); R.pCodex.classList.add('bad'); }
  if (extra.length) R.pCodex.setAttribute('title', `${R.pCodex.getAttribute('title')} · ${extra.join(' · ')}`);
};

Shell.agentDot = function (node, tone, title) {
  node.classList.toggle('ok', tone === 'ok');
  node.classList.toggle('warn', tone === 'warn');
  node.classList.toggle('bad', tone === 'bad');
  node.setAttribute('title', title);
  node.setAttribute('aria-label', title);
};

Shell.patchHeader = function (st) {
  const R = Shell.refs;
  if (!R || !st) return;
  const m = Shell.model(st);
  setText(R.inboxN, String(m.attn));
  R.inbox.classList.toggle('attn', m.attn > 0);
  R.inbox.setAttribute('aria-label', `Needs you: ${m.attn}`);
  if (m.attn !== Shell.badge) {
    Shell.badge = m.attn;
    try {
      if (m.attn > 0 && navigator.setAppBadge) navigator.setAppBadge(m.attn).catch(() => { /* badge refused */ });
      else if (m.attn === 0 && navigator.clearAppBadge) navigator.clearAppBadge().catch(() => { /* ignore */ });
    } catch (_) { /* no Badging API here */ }
  }
  const c = st.claude || {};
  if (!c.installed) Shell.agentDot(R.adClaude, 'bad', 'Claude: not installed');
  else if (c.loggedIn) Shell.agentDot(R.adClaude, 'ok', `Claude: ${c.email || 'logged in'}${c.subscriptionType ? ' (' + c.subscriptionType + ')' : ''}`);
  else Shell.agentDot(R.adClaude, 'warn', 'Claude: not logged in');
  const cx = st.agents && st.agents.codex;
  R.adCodex.classList.toggle('hidden', !cx);
  if (cx) {
    if (cx.installed === false) Shell.agentDot(R.adCodex, 'bad', 'Codex: not installed');
    else if (cx.loggedIn === false) Shell.agentDot(R.adCodex, 'warn', 'Codex: not logged in');
    else Shell.agentDot(R.adCodex, 'ok', `Codex: ${cx.email || cx.version || 'ready'}`);
  }
};

function renderHeader() { Shell.patchHeader(typeof state === 'undefined' ? null : state); }
function renderUsage() { Shell.patchUsage(typeof state === 'undefined' ? null : state); }

/* ---------- sidebar tree ---------- */

Shell.rowKey = function (row) { return row.parentNode.getAttribute('data-key'); };

Shell.toggle = function (key) {
  if (Shell.open.has(key)) Shell.open.delete(key); else Shell.open.add(key);
  Shell.saveOpen();
  if (key.startsWith('d:') && Shell.open.has(key)) Shell.loadDir({ key, ...Shell.parseDirKey(key) }, true);      // opening a folder re-reads it (the cached rows show meanwhile)
  Shell.patchTrees();
};

Shell.toggleDir = function (key) { Shell.toggle(key); };

Shell.groupRow = function (cls, level, name, extra) {
  const n = { name: el('span', { class: 'tn-name' }), cnt: el('span', { class: 'tn-cnt' }) };
  n.row = el('div', { class: 'tn-row ' + cls, role: 'treeitem', tabindex: '-1', 'aria-level': String(level), 'aria-expanded': 'false' },
    el('span', { class: 'tw' }, ic('chevron-right')), n.name, ...(extra || []), n.cnt);
  return n;
};

Shell.projNode = function () {
  const dot = el('span', { class: 'tn-dot hidden', role: 'img', 'aria-label': 'needs you', title: 'needs you' });
  const go = el('a', { class: 'tn-go', tabindex: '-1', title: 'Open project', 'aria-label': 'Open project' }, ic('chevron-right'));
  const g = Shell.groupRow('proj-row', 1, '', [dot]);
  g.row.append(go);
  const kids = el('div', { class: 'tn-kids hidden', role: 'group' });
  const node = el('div', { class: 'tn proj' }, g.row, kids);
  node._r = { ...g, dot, go, kids };
  return node;
};

Shell.patchProj = function (node, p, level) {
  const r = node._r;
  const open = Shell.open.has(p.key);
  node.setAttribute('data-key', p.key);
  node.classList.toggle('open', open);
  node.classList.toggle('attn', p.attn > 0);
  r.row.setAttribute('aria-expanded', open ? 'true' : 'false');
  r.row.setAttribute('aria-level', String(level));
  setText(r.name, p.name);
  setText(r.cnt, p.count ? String(p.count) : '');
  r.cnt.setAttribute('title', `${p.count} session${p.count === 1 ? '' : 's'}`);
  r.dot.classList.toggle('hidden', !p.attn);
  r.go.setAttribute('href', Shell.hash('project', { project: p.name }));
  r.kids.classList.toggle('hidden', !open);
  if (open) Shell.sync(r.kids, Shell.projKids(p, level), Shell.kidNode, Shell.patchKid);
  else if (r.kids.firstChild) r.kids.textContent = '';
};

/* ---------- directories under the current project: the repos and the project folder open into lazily loaded folders ---------- */

/* The project the page is about: the project route's, or the project of a session peek. */
Shell.currentProject = function () {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  if (!r) return '';
  if (r.id === 'project') return (r.params && r.params.project) || '';
  if (r.id === 'session') {
    const parts = String((r.params && r.params.tmux) || '').split('--');
    return parts.length === 3 ? parts[0] : '';
  }
  return '';
};

/* 'd:<project>/<repo>/<path>' -> {project, repo, path} (names never hold a slash, so the first two segments are the project and the repo). */
Shell.parseDirKey = function (key) {
  const rest = String(key).slice(2);
  const a = rest.indexOf('/');
  const b = rest.indexOf('/', a + 1);
  return { project: rest.slice(0, a), repo: rest.slice(a + 1, b), path: rest.slice(b + 1) };
};

Shell.dirKey = function (project, repo, path) { return `d:${project}/${repo}/${path || ''}`; };

/* The kids of a project node: its sessions as before; for the current project the repos (and the project folder) become directory nodes. */
Shell.projKids = function (p, level) {
  if (Shell.currentProject() !== p.name) return p.kids;
  const out = [];
  for (const k of p.kids) {
    if (k.kind !== 'repo') { out.push(k); continue; }
    if (k.state === 'cloning' || k.state === 'clone-failed') { out.push(k); continue; }       // nothing to browse yet: the plain link
    out.push({ key: Shell.dirKey(p.name, k.name, ''), kind: 'dir', project: p.name, repo: k.name, path: '', name: k.name, glyph: 'repo', level: level + 1, hasKids: true, dirty: false });
  }
  if (p.folder) out.push({ key: Shell.dirKey(p.name, 'root', ''), kind: 'dir', project: p.name, repo: 'root', path: '', name: 'project folder', glyph: 'folder', level: level + 1, hasKids: true, dirty: false });
  return out;
};

Shell.loadDir = function (k, fresh) {
  if (typeof Tree === 'undefined' || !Tree || typeof Tree.children !== 'function' || Shell.dirLoading.has(k.key)) return;
  Shell.dirLoading.add(k.key);
  Tree.children(k.project, k.repo, k.path, { repos: true, fresh: !!fresh }).then(
    (data) => {
      const entries = (data && data.entries) || [];
      Shell.dirs.set(k.key, { entries });
      let pruned = false;                                                              // an open key that turned out to be a file (a ?path= to a file) goes
      for (const e of entries) if (e.type === 'file' && Shell.open.delete(Shell.dirKey(k.project, k.repo, k.path ? `${k.path}/${e.name}` : e.name))) pruned = true;
      if (pruned) Shell.saveOpen();
    },
    (e) => {
      if (!Shell.dirs.has(k.key) || !Shell.dirs.get(k.key).entries) Shell.dirs.set(k.key, { error: (e && e.message) || 'could not load' });
      const st = e && (e.status || (/\b(400|403|404)\b|not found|not a ccboard|unsafe|bad path/i.test(String(e.message || '')) ? 404 : 0));
      if ((st === 400 || st === 403 || st === 404) && Shell.open.delete(k.key)) Shell.saveOpen();   // a folder that cannot exist is forgotten, not retried every load
    }).then(() => {
    Shell.dirLoading.delete(k.key);
    Shell.patchTrees();
  });
};

/* The rows inside an open directory node: its sub-directories from the cached listing (asked for when it is not there yet). */
Shell.dirItems = function (k) {
  const hit = Shell.dirs.get(k.key);
  if (!hit) { Shell.loadDir(k, false); return [{ key: 'note:' + k.key, kind: 'note', text: 'loading…' }]; }
  if (hit.error) return [{ key: 'note:' + k.key, kind: 'note', text: 'could not load: ' + hit.error }];
  const out = [];
  for (const e of hit.entries) {
    const topRepo = e.type === 'repo' && k.repo === 'root' && !k.path;                 // the project folder's own repos are top-level nodes already
    if (e.type !== 'dir' && !(e.type === 'repo' && !topRepo)) continue;
    const path = k.path ? `${k.path}/${e.name}` : e.name;
    const repo = k.repo;                                                                 // a nested repo opens in place through the same repo name
    out.push({ key: Shell.dirKey(k.project, repo, path), kind: 'dir', project: k.project, repo, path, name: e.name,
      glyph: e.type === 'repo' ? 'repo' : 'dir', level: k.level + 1, hasKids: e.has_children !== false, dirty: !!e.dirty });
  }
  if (!out.length) out.push({ key: 'note:' + k.key, kind: 'note', text: 'no subfolders' });
  return out;
};

Shell.dirNode = function () {
  const tw = el('span', { class: 'tw', 'aria-hidden': 'true' }, ic('chevron-right'));
  const glyph = el('span', { class: 'tn-g' });
  const name = el('span', { class: 'tn-name' });
  const dot = el('span', { class: 'tn-dot dirty hidden', title: 'uncommitted changes inside', 'aria-hidden': 'true' });
  const row = el('a', { class: 'tn-row d-row', role: 'treeitem', tabindex: '-1', 'aria-level': '2', 'data-route': 'tree-dir' }, tw, glyph, name, dot);
  const kids = el('div', { class: 'tn-kids hidden', role: 'group' });
  const node = el('div', { class: 'tn dirn' }, row, kids);
  node._d = { row, glyph, name, dot, kids, g: null };
  return node;
};

/* Is this directory the one the Files tab shows (its repo, ?tab=files and ?path= equal to it)? */
Shell.dirCurrent = function (k) {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  if (!r || r.id !== 'project' || !r.params || r.params.project !== k.project || r.params.repo !== k.repo) return false;
  const q = r.query || {};
  return q.tab === 'files' && (q.path || '') === k.path;
};

Shell.patchDir = function (node, k) {
  const d = node._d;
  const open = Shell.open.has(k.key);
  node.setAttribute('data-key', k.key);
  node.classList.toggle('open', open && k.hasKids);
  d.row.setAttribute('href', Shell.hash('project', { project: k.project, repo: k.repo }, k.path ? { tab: 'files', path: k.path } : { tab: 'files' }));
  d.row.setAttribute('aria-level', String(k.level));
  Shell.setVar(d.row, '--lvl', String(k.level));
  d.row.classList.toggle('leaf', !k.hasKids);
  if (k.hasKids) d.row.setAttribute('aria-expanded', open ? 'true' : 'false'); else d.row.removeAttribute('aria-expanded');
  d.row.classList.toggle('cur', Shell.dirCurrent(k));
  if (Shell.dirCurrent(k)) d.row.setAttribute('aria-selected', 'true'); else d.row.removeAttribute('aria-selected');
  if (d.g !== k.glyph) { d.g = k.glyph; d.glyph.textContent = ''; d.glyph.append(ic(k.glyph === 'repo' ? 'git-repo' : 'folder-close')); }
  setText(d.name, k.name);
  d.row.setAttribute('title', k.path || (k.repo === 'root' ? 'the project folder' : k.repo));
  d.dot.classList.toggle('hidden', !k.dirty);
  const showKids = open && k.hasKids;
  d.kids.classList.toggle('hidden', !showKids);
  if (showKids) Shell.sync(d.kids, Shell.dirItems(k), Shell.kidNode, Shell.patchKid);
  else if (d.kids.firstChild) d.kids.textContent = '';
};

/* The current project's node opens (and so do the repo and the folders down to the ?path= of the Files tab) when the page is entered; open
   directories refresh their listing then. Called on every route change and once when the shell is installed. */
Shell.openCurrent = function () {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  if (r && r.id === 'project' && r.params && r.params.project) {
    const proj = r.params.project;
    let changed = false;
    const add = (key) => { if (!Shell.open.has(key)) { Shell.open.add(key); changed = true; } };
    add('p:' + proj);
    if (r.params.repo) {
      add(Shell.dirKey(proj, r.params.repo, ''));
      let acc = '';
      const segs = String((r.query && r.query.path) || '').split('/').filter(Boolean);
      if (!segs.some((s) => s === '..' || s === '.' || s === '.git')) {                   // a bad deep link never becomes a persisted key
        for (const seg of segs) { acc = acc ? `${acc}/${seg}` : seg; add(Shell.dirKey(proj, r.params.repo, acc)); }   // a file's key is dropped again once its folder is listed (loadDir)
      }
    }
    if (changed) Shell.saveOpen();
    // open folders re-read their listing; the ?path= itself is left to its own node (it may be a file, which has none)
    const here = r.params.repo ? Shell.dirKey(proj, r.params.repo, String((r.query && r.query.path) || '')) : '';
    for (const key of Shell.open) if (key.startsWith(`d:${proj}/`) && key !== here) Shell.loadDir({ key, ...Shell.parseDirKey(key) }, true);
  }
  Shell.patchTrees();
};

Shell.kidNode = function (k) {
  if (k.kind === 'dir') return Shell.dirNode();
  if (k.kind === 'note') return el('div', { class: 'tn-note dim' });
  if (k.kind === 'repo') {
    const name = el('span', { class: 'tn-name' });
    const node = el('a', { class: 'tn-row r-row', role: 'treeitem', tabindex: '-1', 'aria-level': '2' }, el('span', { class: 'tn-g' }, ic('git-repo')), name);
    node._k = { name };
    return node;
  }
  const slot = el('span', { class: 'tn-g' });
  const name = el('span', { class: 'tn-name' });
  const sub = el('span', { class: 'tn-sub mono' });
  const node = el('a', { class: 'tn-row s-row', role: 'treeitem', tabindex: '-1', 'aria-level': '2', 'data-drop': 'session' }, slot, name, sub);
  node._k = { slot, name, sub, state: null, agent: null };
  if (typeof Dnd !== 'undefined') Dnd.bind(node);                                  // a backlog card dropped on the row is handed to this session (dnd.js)
  return node;
};

Shell.patchKid = function (node, k) {
  if (k.kind === 'dir') { Shell.patchDir(node, k); return; }
  if (k.kind === 'note') { node.setAttribute('data-key', k.key); setText(node, k.text); return; }
  const r = node._k;
  node.setAttribute('data-key', k.key);
  setText(r.name, k.name);
  if (k.kind === 'repo') { node.setAttribute('href', Shell.hash('project', { project: k.project, repo: k.name })); return; }
  node.setAttribute('href', Shell.hash('session', { tmux: k.tmux }));
  node.setAttribute('data-tmux', k.tmux);                                          // the drop target's session (dnd.js)
  node.classList.toggle('attn', k.needs);
  if (r.state !== k.state || r.agent !== k.agent) {
    r.state = k.state; r.agent = k.agent;
    r.slot.textContent = '';
    r.slot.append(stateGlyph(k.state), agentGlyph(k.agent));
  }
  setText(r.sub, k.repo === 'root' ? 'folder' : k.repo);
};

Shell.olderNode = function () {
  const g = Shell.groupRow('older-row', 1, '', []);
  const kids = el('div', { class: 'tn-kids hidden', role: 'group' });
  const node = el('div', { class: 'tn older', 'data-key': 'older' }, g.row, kids);
  node._r = { ...g, kids };
  return node;
};

Shell.patchOlder = function (node, it) {
  const r = node._r;
  const open = Shell.open.has('older');
  node.classList.toggle('open', open);
  r.row.setAttribute('aria-expanded', open ? 'true' : 'false');
  setText(r.name, `older (${it.projects.length})`);
  setText(r.cnt, '');
  r.kids.classList.toggle('hidden', !open);
  if (open) Shell.sync(r.kids, it.projects, Shell.projNode, (n, p) => Shell.patchProj(n, p, 2));
  else if (r.kids.firstChild) r.kids.textContent = '';
};

Shell.patchTree = function (tree, model) {
  const items = model.main.slice();
  if (model.older.length) items.push({ key: 'older', kind: 'older', projects: model.older });
  Shell.sync(tree, items, (it) => (it.kind === 'older' ? Shell.olderNode() : Shell.projNode()),
    (n, it) => (it.kind === 'older' ? Shell.patchOlder(n, it) : Shell.patchProj(n, it, 1)));
  tree._empty.classList.toggle('hidden', !model.empty);
  const rows = tree.querySelectorAll('.tn-row');
  if (rows.length && !tree.querySelector('.tn-row[tabindex="0"]')) rows[0].setAttribute('tabindex', '0');
};

Shell.patchTrees = function () {
  if (typeof state === 'undefined' || !state) return;
  const model = Shell.model(state);
  for (const t of Shell.trees) Shell.patchTree(t, model);
};

Shell.treeKey = function (e) {
  const tree = e.currentTarget;
  const row = e.target.closest && e.target.closest('.tn-row');
  if (!row) return;
  const rows = [...tree.querySelectorAll('.tn-row')];
  const i = rows.indexOf(row);
  const group = row.hasAttribute('aria-expanded');
  const open = row.getAttribute('aria-expanded') === 'true';
  const focus = (n) => { if (n) n.focus(); };
  const k = e.key;
  if (k === 'ArrowDown') { e.preventDefault(); focus(rows[i + 1]); }
  else if (k === 'ArrowUp') { e.preventDefault(); focus(rows[i - 1]); }
  else if (k === 'Home') { e.preventDefault(); focus(rows[0]); }
  else if (k === 'End') { e.preventDefault(); focus(rows[rows.length - 1]); }
  else if (k === 'ArrowRight' && group) { e.preventDefault(); if (!open) Shell.toggle(Shell.rowKey(row)); else focus(rows[i + 1]); }
  else if (k === 'ArrowLeft') {
    e.preventDefault();
    if (group && open) Shell.toggle(Shell.rowKey(row));
    else { const up = row.closest('.tn-kids'); if (up) focus(up.parentNode.querySelector('.tn-row')); }
  } else if (k === ' ' && group) { e.preventDefault(); Shell.toggle(Shell.rowKey(row)); }
  else if (k === 'Enter' && row.classList.contains('d-row')) { e.preventDefault(); row.click(); }       // a directory node is a link to its Files tab
  else if (k === 'Enter' && group) {
    e.preventDefault();
    const go = row.querySelector('.tn-go');
    if (go) go.click(); else Shell.toggle(Shell.rowKey(row));
  } else if (k === ' ' && row.tagName === 'A') { e.preventDefault(); row.click(); }
};

Shell.bindTree = function (tree) {
  tree.addEventListener('click', (e) => {
    if (e.target.closest('.tn-go')) return;                    // the arrow opens the project page
    const row = e.target.closest('.tn-row');
    if (row && row.classList.contains('d-row')) {              // a directory node: the chevron toggles, the rest is a link to its Files tab
      if (e.target.closest('.tw')) {
        e.preventDefault();
        e.stopPropagation();                                   // not a navigation: the drawer must stay open
        if (row.hasAttribute('aria-expanded')) Shell.toggleDir(Shell.rowKey(row));
      }
      return;
    }
    if (row && row.hasAttribute('aria-expanded')) Shell.toggle(Shell.rowKey(row));
  });
  tree.addEventListener('keydown', Shell.treeKey);
  tree.addEventListener('focusin', (e) => {                     // roving tabindex: one row is a tab stop
    const row = e.target.closest && e.target.closest('.tn-row');
    if (!row) return;
    for (const r of tree.querySelectorAll('.tn-row[tabindex="0"]')) if (r !== row) r.setAttribute('tabindex', '-1');
    row.setAttribute('tabindex', '0');
  });
};

/* The nav list + the tree: built twice (sidebar and drawer) by this one builder, each with its own root. */
Shell.buildSide = function (container, withFoot) {
  const nav = el('nav', { class: 'sb-nav', 'aria-label': 'Sections' });
  for (const [id, label, icon, href] of Shell.NAV) {
    nav.append(el('a', { class: 'nav-item', href, 'data-nav': id, title: label }, ic(icon), el('span', { class: 'lbl', text: label }),
      id === 'inbox' || id === 'agents' || id === 'tasks' ? el('span', { class: 'cnt', 'data-cnt': id }) : null));
  }
  const tree = el('div', { class: 'tree', role: 'tree', 'aria-label': 'Projects' });
  tree._empty = el('div', { class: 'sb-empty dim hidden', text: 'No projects yet' });   // hidden until the first state says so (never a false empty state while loading)
  const inner = el('div', { class: 'sb-inner' }, nav, el('div', { class: 'sb-h', text: 'Projects' }), tree, tree._empty);
  if (withFoot) {
    inner.append(el('div', { class: 'sb-foot' },
      el('button', { class: 'minimal small collapse', type: 'button', title: 'Collapse sidebar  [', 'aria-label': 'Collapse sidebar', onclick: () => Shell.setSidebar(false) }, ic('double-chevron-left'), 'Collapse')));
  }
  container.append(inner);
  Shell.bindTree(tree);
  Shell.trees.push(tree);
  return tree;
};

Shell.buildDrawer = function (dlg) {
  const panel = el('div', { class: 'drawer-panel' },
    el('div', { class: 'drawer-head' }, el('a', { class: 'brand', href: '#/', text: 'ccboard' }),
      el('button', { class: 'icon minimal', type: 'button', 'aria-label': 'Close menu', title: 'Close', onclick: () => dlg.close() }, ic('cross'))));
  Shell.buildSide(panel, false);
  dlg.append(panel);
  dlg.addEventListener('click', (e) => {
    if (e.target === dlg) dlg.close();                          // backdrop
    else if (e.target.closest && e.target.closest('a[href^="#/"]')) dlg.close();   // a link to the current hash fires no hashchange
  });
};

Shell.openDrawer = function () {
  const dlg = $('#drawer');
  if (!dlg || dlg.open) return;
  try { dlg.showModal(); } catch (_) { dlg.setAttribute('open', ''); }
};

Shell.closeDrawer = function () { const dlg = $('#drawer'); if (dlg && dlg.open) dlg.close(); };

/* ---------- bottom nav (compact) ---------- */

Shell.buildBnav = function (nav) {
  for (const [id, label, icon, href] of Shell.NAV.slice(0, 4)) {
    nav.append(el('a', { class: 'bn', href, 'data-nav': id },
      el('span', { class: 'bn-ic' }, ic(icon), id === 'inbox' || id === 'agents' || id === 'tasks' ? el('span', { class: 'cnt', 'data-cnt': id }) : null),
      el('span', { class: 'lbl', text: label })));
  }
  nav.append(el('button', { class: 'minimal bn', type: 'button', 'aria-label': 'Menu', onclick: () => Shell.openDrawer() },
    el('span', { class: 'bn-ic' }, ic('menu')), el('span', { class: 'lbl', text: 'Menu' })));
};

/* ---------- counts, aria-current, crumbs ---------- */

Shell.patchCounts = function (model) {
  const counts = { inbox: model.attn, agents: model.live, tasks: model.tasks };
  for (const root of [$('#sidebar'), $('#drawer'), $('#bnav')]) {
    if (!root) continue;
    for (const c of root.querySelectorAll('[data-cnt]')) {
      const id = c.getAttribute('data-cnt');
      const n = counts[id] || 0;
      setText(c, n ? String(n) : '');
      c.classList.toggle('attn', id === 'inbox' && n > 0);
      const a = c.closest('[data-nav]');
      if (a) a.classList.toggle('attn', id === 'inbox' && n > 0);
    }
  }
};

Shell.syncNav = function () {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  const id = r ? r.id : 'home';
  for (const root of [$('#sidebar'), $('#drawer'), $('#bnav')]) {
    if (!root) continue;
    for (const a of root.querySelectorAll('[data-nav]')) {
      if (a.getAttribute('data-nav') === id) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
  }
};

Shell.crumbList = function (r) {
  if (!r || r.id === 'home') return [];
  const p = r.params || {};
  if (r.id === 'project') return [{ text: p.project, href: Shell.hash('project', { project: p.project }) }].concat(p.repo ? [{ text: p.repo }] : []);
  if (r.id === 'session') {
    const parts = String(p.tmux || '').split('--');
    return parts.length === 3 ? [{ text: parts[0], href: Shell.hash('project', { project: parts[0] }) }, { text: parts[1] }, { text: parts[2] }] : [{ text: p.tmux }];
  }
  if (r.id === 'memory' && p.project) return [{ text: 'Memory', href: '#/memory' }, { text: p.project }];
  return [{ text: Shell.CRUMB_NAMES[r.id] || r.id }];
};

Shell.syncCrumbs = function () {
  const R = Shell.refs;
  if (!R) return;
  const list = Shell.crumbList(typeof currentRoute === 'function' ? currentRoute() : null);
  const sig = JSON.stringify(list);
  if (sig === Shell.crumbSig) return;
  Shell.crumbSig = sig;
  R.crumbs.textContent = '';
  list.forEach((c, i) => {
    if (i) R.crumbs.append(el('span', { class: 'sep', 'aria-hidden': 'true', text: '/' }));
    R.crumbs.append(c.href ? el('a', { class: 'crumb', href: c.href, text: c.text }) : el('span', { class: 'crumb', text: c.text }));
  });
};

Shell.syncSearchBox = function () {
  const R = Shell.refs;
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  if (R && r && r.id === 'search' && document.activeElement !== R.search) R.search.value = (r.query && r.query.q) || '';
};

/* ---------- shell modes ---------- */

Shell.applyMode = function () {
  const mq = Shell.mq;
  const mode = mq.l.matches ? 'large' : mq.e.matches ? 'expanded' : mq.m.matches ? 'medium' : 'compact';
  Shell.mode = mode;
  const wide = Shell.wide();
  const sb = mode === 'compact' ? 'none' : (mode === 'medium' || !Shell.sbOpen) ? 'rail' : 'full';
  document.body.setAttribute('data-shell', mode);
  document.body.setAttribute('data-sb', sb);
  const side = $('#sidebar');
  if (side) side.classList.toggle('rail', sb === 'rail');
  const R = Shell.refs;
  if (R) {
    R.navBtn.classList.toggle('hidden', wide && sb === 'full');
    R.navBtn.setAttribute('aria-label', wide ? 'Expand sidebar' : 'Open menu');
    R.navBtn.setAttribute('title', wide ? 'Expand sidebar  [' : 'Menu  [');
  }
  if (wide) Shell.closeDrawer();                                  // the persistent sidebar replaces the drawer from 840 px up
};

Shell.watchMode = function () {
  const q = (s) => (window.matchMedia ? window.matchMedia(s) : { matches: false });
  Shell.mq = { l: q('(min-width: 1200px)'), e: q('(min-width: 840px)'), m: q('(min-width: 600px)') };
  for (const m of Object.values(Shell.mq)) {
    if (m.addEventListener) m.addEventListener('change', () => Shell.applyMode());
    else if (m.addListener) m.addListener(() => Shell.applyMode());
  }
  Shell.applyMode();
};

Shell.setSidebar = function (open) {
  Shell.sbOpen = !!open;
  try { localStorage.setItem('ccboard:sb', open ? '1' : '0'); } catch (_) { /* storage may be unavailable */ }
  Shell.applyMode();
};

Shell.navToggle = function () { if (Shell.wide()) Shell.setSidebar(true); else Shell.openDrawer(); };

Shell.toggleSidebar = function () {
  if (Shell.wide()) { Shell.setSidebar(!Shell.sbOpen); return; }
  const dlg = $('#drawer');
  if (dlg && dlg.open) dlg.close(); else Shell.openDrawer();
};

/* The terminal dock arrives with the quad view (v0.5.9); the keyboard layer already binds mod+j to this, so the binding needs no change then. */
Shell.toggleDock = function () { /* no dock yet */ };

/* ---------- resizable sidebar: a drag handle over the right edge of the expanded sidebar ---------- */

Shell.clampSbW = function (n) {
  return Number.isFinite(n) ? Math.round(Math.min(Shell.SB_MAX, Math.max(Shell.SB_MIN, n))) : Shell.SB_DEFAULT;
};

/* --sb-full lives on #app (shell.css maps --sb-w to it there), set through the CSSOM: never a style attribute. */
Shell.setSbWidth = function (w, persist) {
  Shell.sbW = Shell.clampSbW(w);
  const app = $('#app');
  if (app) Shell.setVar(app, '--sb-full', Shell.sbW + 'px');
  const R = Shell.refs;
  if (R && R.resizer) R.resizer.setAttribute('aria-valuenow', String(Shell.sbW));
  if (persist) { try { localStorage.setItem('ccboard:sb:w', String(Shell.sbW)); } catch (_) { /* storage may be unavailable */ } }
};

/* Back to the default width; the persisted value is removed (not overwritten) so a later default change applies. */
Shell.resetSbWidth = function () {
  Shell.setSbWidth(Shell.SB_DEFAULT, false);
  try { localStorage.removeItem('ccboard:sb:w'); } catch (_) { /* storage may be unavailable */ }
};

Shell.buildResizer = function (app) {
  const h = el('div', { class: 'sb-resize', role: 'separator', 'aria-orientation': 'vertical', 'aria-label': 'Resize sidebar', tabindex: '0',
    'aria-valuemin': String(Shell.SB_MIN), 'aria-valuemax': String(Shell.SB_MAX), 'aria-valuenow': String(Shell.sbW),
    title: 'Drag to resize the sidebar (double-click resets)' });
  let drag = null;
  const end = () => {
    if (!drag) return;
    drag = null;
    app.classList.remove('sb-dragging');
    Shell.setSbWidth(Shell.sbW, true);
  };
  h.addEventListener('pointerdown', (e) => {
    if (e.button) return;
    e.preventDefault();
    const side = $('#sidebar');
    drag = { grab: e.clientX - (side ? side.getBoundingClientRect().right : e.clientX) };          // where on the 6 px strip it was taken
    try { h.setPointerCapture(e.pointerId); } catch (_) { /* no pointer capture: the move events still arrive over the strip */ }
    app.classList.add('sb-dragging');
  });
  h.addEventListener('pointermove', (e) => {
    if (drag) Shell.setSbWidth(e.clientX - drag.grab - app.getBoundingClientRect().left, false);
  });
  for (const t of ['pointerup', 'pointercancel', 'lostpointercapture']) h.addEventListener(t, end);
  h.addEventListener('dblclick', () => Shell.resetSbWidth());
  h.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 48 : 16;
    let w = null;
    if (e.key === 'ArrowLeft') w = Shell.sbW - step;
    else if (e.key === 'ArrowRight') w = Shell.sbW + step;
    else if (e.key === 'Home') w = Shell.SB_MIN;
    else if (e.key === 'End') w = Shell.SB_MAX;
    else if (e.key === 'Enter') { e.preventDefault(); Shell.resetSbWidth(); return; }
    if (w === null) return;
    e.preventDefault();
    Shell.setSbWidth(w, true);
  });
  app.append(h);
  return h;
};

/* ---------- create menu: repo picker sheet, then the existing launcher forms ---------- */

Shell.createItems = function () {
  return [
    { label: 'New session', icon: 'console', onClick: () => Shell.openCreate('session') },
    { label: 'New task', icon: 'git-branch', onClick: () => Shell.openCreate('task') },
    { label: 'Schedule', icon: 'time', onClick: () => Shell.openCreate('schedule') },
    { label: 'New project', icon: 'folder-close', onClick: () => Shell.openCreate('project') },
    { label: 'Import from GitHub', icon: 'download', onClick: () => Shell.openCreate('import') },
    { label: 'Batch prompt', icon: 'layers', onClick: () => Shell.openCreate('batch') },
  ];
};

/* Shell.openCreate(kind, ctx): the one entry for everything the + menu, the c-chords (keymap.js) and the page buttons create. kind is
   session | task | schedule (a repo picker, then the launcher form), project (the new-project form and the clone queue), import (GitHub
   repos into a project) or batch (one headless prompt over many repos). ctx = {project, repo?} (the project page's buttons) skips the picker
   when the place is clear: the form opens for that repo ('root' is the project folder: a session always, a task or schedule when it is itself
   a git repo), or for the project's only place; a project with several places and no repo in ctx gets the picker narrowed to it. Without a ctx
   the place comes from the route (Shell.routeCtx: the project page, or the session open in the peek; for a task off a project page, the place
   the last task went to), so c t on a project page is the form for that project at once. Opens the sheet and returns true; false for an unknown
   kind, before the first state has arrived (every form lists the box's repos; the tap says 'still loading the board…') or without the sheet
   dialog, so a key that asked can be left alone. */
Shell.openCreate = function (kind, ctx) {
  if (typeof state === 'undefined' || !state) {                    // a tap before the first /api/state: say so (the buttons are drawn already)
    if (['session', 'task', 'schedule', 'job', 'project', 'import', 'batch'].includes(kind) && typeof toast === 'function') toast('still loading the board…');
    return false;
  }
  if (!document.getElementById('sheet')) return false;
  let pre = ctx && typeof ctx === 'object' && ctx.project ? ctx : null;
  if (!pre && (kind === 'session' || kind === 'task' || kind === 'schedule' || kind === 'job')) pre = Shell.routeCtx(kind);
  if (kind === 'session' || kind === 'task') { if (!(pre && Shell.createFor(kind, pre))) Shell.pickRepo(kind, pre); }
  else if (kind === 'schedule' || kind === 'job') { if (!(pre && Shell.createFor('job', pre))) Shell.pickRepo('job', pre); }
  else if (kind === 'project') Shell.projectSheet();
  else if (kind === 'import') Shell.formSheet('Import repos from GitHub', importForm, 'Import queued');
  else if (kind === 'batch') Shell.formSheet('Batch prompt across repos', batchForm, 'Batch queued');
  else return false;
  return true;
};

/* The project (and repo) the current route is about: the project page, or the session open in the peek (its tmux name is project--repo--name).
   For a task or schedule without a repo in the route the repo is Shell.defaultRepo's (the one used last, else the first). Off a project page a TASK
   goes where the last task went (launcher.js taskLastAny: ccboard:task:last), so + task on #/tasks, the + menu and c t from Home open the form at
   once, with its "in" select to switch; null when nothing was saved, or the place is gone or no longer takes a task (then the picker asks). */
Shell.routeCtx = function (kind) {
  let r = null;
  try { r = typeof currentRoute === 'function' ? currentRoute() : null; } catch (_) { r = null; }
  let project = '';
  let repo = '';
  if (r && r.params) {
    if (r.id === 'project') { project = r.params.project || ''; repo = r.params.repo || (r.query && r.query.repo) || ''; }
    else if (r.id === 'session' && r.params.tmux) { const parts = String(r.params.tmux).split('--'); if (parts.length >= 3) { project = parts[0]; repo = parts[1]; } }
  }
  if (!project) {
    const last = kind === 'task' && typeof taskLastAny === 'function' ? taskLastAny() : null;
    const lp = last && (state.projects || []).find((x) => x.name === last.project);
    const lr = lp && (last.repo === 'root' ? lp.root : (lp.repos || []).find((x) => x.name === last.repo));
    return lr && taskTarget(lp, lr) ? { project: last.project, repo: last.repo } : null;
  }
  const p = (state.projects || []).find((x) => x.name === project);
  if (!p) return null;
  if (!repo && (kind === 'task' || kind === 'schedule' || kind === 'job')) repo = Shell.defaultRepo(p, 'task') || '';
  return repo ? { project, repo } : { project };
};

/* Where a task or schedule starts in a project when nobody said: the repo a task was last started in (remembered per project), else its first
   repo, else its project folder when that is a git repo. undefined when it has none. Sessions have no default: they ask. */
Shell.defaultRepo = function (p, kind) {
  if (!p || (kind !== 'task' && kind !== 'schedule' && kind !== 'job')) return undefined;
  const list = taskTargets(p, kind).map((x) => x.r);
  if (!list.length) return undefined;
  const last = typeof taskLastRepo === 'function' ? taskLastRepo(p.name) : '';
  const hit = last ? list.find((x) => x.name === last) : null;
  return (hit || list[0]).name;
};

Shell.targetLabel = function (p, r) { return r === p.root || r.root ? `${p.name} · project folder` : `${p.name}/${r.name}`; };

/* The launcher form for a project (and repo) that is already known: true when it opened, false when the picker has to ask. */
Shell.createFor = function (kind, ctx) {
  const p = (state.projects || []).find((x) => x.name === ctx.project);
  if (!p) return false;
  const git = kind === 'task' || kind === 'job';                                          // tasks and schedules need a git repo
  const ok = (r) => r.state === 'ok' || r.state === 'unknown';
  let r = null;
  if (ctx.repo) r = ctx.repo === 'root' ? p.root : (p.repos || []).find((x) => x.name === ctx.repo && ok(x));
  else {
    const where = git ? taskTargets(p, kind).map((x) => x.r) : [...(p.root ? [p.root] : []), ...(p.repos || []).filter(ok)];
    if (where.length === 1) r = where[0];
  }
  if (!r || (git && !(kind === 'task' ? taskTarget : gitTarget)(p, r))) return false;      // a task may run in place in a non-git project folder
  Shell.showForm(kind, { p, r, label: Shell.targetLabel(p, r) });
  return true;
};

Shell.PICK_TITLES = { session: 'New session', task: 'New task', job: 'Schedule a run' };

Shell.pickRepo = function (kind, ctx) {
  if (typeof state === 'undefined' || !state) return;
  const only = ctx && ctx.project ? ctx.project : null;                                  // narrowed to one project (the project page's buttons)
  const git = kind === 'task' || kind === 'job';
  const entries = [];
  const notGit = [];                                                                     // projects whose folder is no git repo: a hint under the list for tasks
  if (kind === 'session') {
    for (const p of state.projects || []) {
      if (!p.root || (only && p.name !== only)) continue;
      entries.push({ p, r: p.root, label: `${p.name}/`, sub: gitTarget(p, p.root) ? 'project folder' : 'project folder · not a git repo' });
    }
  }
  for (const p of state.projects || []) {
    if (only && p.name !== only) continue;
    const here = [];
    for (const x of allRepos()) {
      if (x.project !== p.name) continue;
      const r = p.repos.find((q) => q.name === x.repo);
      if (r) here.push({ p, r, label: x.id, sub: r.path && r.path === p.path ? 'project folder' : (r.branch || '') });
    }
    if (git) {
      const rootOk = p.root && (kind === 'task' ? taskTarget(p, p.root) : gitTarget(p, p.root));
      if (rootOk) here.push({ p, r: p.root, label: Shell.targetLabel(p, p.root), sub: rootIsGit(p.root) ? (p.root.branch || '') : 'in place (not a git repo)' });
      else if (p.root && !(p.repos || []).some((q) => q.path === p.root.path)) notGit.push(p.name);      // schedules: the folder needs git
      const last = typeof taskLastRepo === 'function' ? taskLastRepo(p.name) : '';
      const i = last ? here.findIndex((e) => e.r.name === last) : -1;
      if (i > 0) here.unshift(...here.splice(i, 1));                                     // the repo used last comes first
    }
    entries.push(...here);
  }
  const list = el('div', { class: 'pick-list' });
  const fill = (q) => {
    list.textContent = '';
    const f = q.trim().toLowerCase();
    for (const e of entries) {
      if (f && !e.label.toLowerCase().includes(f)) continue;
      list.append(el('button', { class: 'minimal pick-row', type: 'button', onclick: () => Shell.showForm(kind, e) },
        el('span', { class: 'pr-name mono', text: e.label }), e.sub ? el('span', { class: 'dim', text: e.sub }) : null));
    }
    if (!list.childElementCount) list.append(el('div', { class: 'dim', text: 'no match' }));
  };
  const filter = entries.length > 8 ? el('input', { type: 'search', placeholder: 'filter repos…', 'aria-label': 'Filter repos', oninput: (ev) => fill(ev.target.value) }) : null;
  fill('');
  const hint = git && notGit.length ? el('p', { class: 'dim pick-hint', text: notGit.length === 1 ? `The folder of ${notGit[0]} is not a git repo: tasks need git (git init there to run tasks in it).`
    : `The folders of ${notGit.slice(0, 4).join(', ')}${notGit.length > 4 ? '…' : ''} are not git repos: tasks need git (git init there to run tasks in them).` }) : null;
  openSheet({ title: Shell.PICK_TITLES[kind], body: entries.length ? [filter, list, hint] : emptyState('folder-close', 'No repos yet', 'Create a project and add a repo first.'),
    onClose: () => { if (ui.openForm === 'sheet') ui.openForm = null; Shell.formWatch = null; } });
  focusFine(filter);                                                                    // a phone keeps its keyboard down until a field is tapped
};

/* The launcher form in the sheet. The task form takes a "where" select over the project's places (a switch re-opens it for the other repo with the
   typed text carried over) and closes the sheet itself once its call worked; the session and schedule forms close it through the next render. */
Shell.showForm = function (kind, e, carry) {
  const form = kind === 'session' ? sessionForm(e.p, e.r)
    : kind === 'task' ? taskForm(e.p, e.r, { carry, targets: taskTargets(e.p), onTarget: (x, c) => Shell.showForm('task', { p: x.p, r: x.r, label: x.label }, c),
      onDone: () => { closeSheet(); }, onCancel: () => closeSheet() })
      : jobForm(e.p, e.r);
  const holder = el('div', { class: 'sheet-form' }, form);
  // The launcher forms end with a Cancel that re-renders the home board: inside the sheet it only closes the sheet.
  holder.addEventListener('click', (ev) => {
    const b = ev.target.closest && ev.target.closest('button');
    if (b && b.type === 'button' && b.textContent.trim() === 'Cancel') { ev.stopPropagation(); ev.preventDefault(); closeSheet(); }
  }, true);
  // The forms set ui.openForm = null and poll(true) once the launch worked: the next forced render closes the sheet.
  ui.openForm = 'sheet';
  Shell.formWatch = () => { if (ui.openForm !== 'sheet') { Shell.formWatch = null; closeSheet(); } };
  openSheet({ title: `${Shell.PICK_TITLES[kind]} · ${e.label}`, body: holder, back: { label: 'Back to the repo list', onClick: () => Shell.pickRepo(kind, e) },
    onClose: () => { if (ui.openForm === 'sheet') ui.openForm = null; Shell.formWatch = null; } });
  if (typeof form.focusFirst === 'function') form.focusFirst();          // focusFine inside: the first field on a fine pointer, nothing on touch
};

Shell.sheetClosed = function () { if (ui.openForm === 'sheet') ui.openForm = null; Shell.formWatch = null; };

/* A launcher form (import, batch) in the sheet: it calls onDone after its call succeeded, which closes the sheet with a toast. */
Shell.formSheet = function (title, build, done) {
  Shell.formWatch = null;
  const form = build({ onDone: () => { closeSheet(); toast(done, { kind: 'ok' }); }, onCancel: () => closeSheet() });
  openSheet({ title, body: form, onClose: Shell.sheetClosed });
  if (typeof form.focusFirst === 'function') form.focusFirst();
};

/* The new-project sheet: the two bulk entries the v0.4 board had beside the form, the clone queue line, then the form (its footer is the last row) (st.clone_queue, patched on every
   render through Shell.formWatch). A project created with a clone URL keeps the sheet open so the queue shows the clone; a blank one closes it. */
Shell.projectSheet = function () {
  const queue = cloneQueueView();
  const form = projectForm({
    onDone: (body) => { toast(`Project ${body.name} created`, { kind: 'ok' }); if (!body.url) closeSheet(); },
    onCancel: () => closeSheet(),
  });
  const bulk = el('div', { class: 'row sheet-links' },
    el('button', { class: 'small', type: 'button', onclick: () => Shell.openCreate('import') }, ic('download'), 'Import from GitHub…'),
    el('button', { class: 'small', type: 'button', onclick: () => Shell.openCreate('batch') }, ic('layers'), 'Batch prompt…'));
  Shell.formWatch = () => queue.update(typeof state === 'undefined' ? null : state);
  openSheet({ title: 'New project', body: [bulk, queue.node, form], onClose: Shell.sheetClosed });       // the two bulk entries and the clone queue above the form: its sticky footer (Create project / Cancel) is the last row
  form.focusFirst();
};

Shell.newProject = function () { return Shell.openCreate('project'); };

/* ---------- install ---------- */

/* Install (Chromium): the browser's own mini-infobar is suppressed and the event kept; Settings > App has the button. Safari has no such event
   (File > Add to Dock on macOS, Share > Add to Home Screen on iPadOS: the same panel lists the steps). */
Shell.installChanged = function () { if (typeof renderAppPanel === 'function') renderAppPanel(); };

Shell.promptInstall = async function () {
  const ev = Shell.installPrompt;
  if (!ev || typeof ev.prompt !== 'function') return 'unavailable';
  Shell.installPrompt = null;                                      // an install event can be used once; the browser fires a new one when it is ready again
  let outcome = 'dismissed';
  try { await ev.prompt(); const c = await ev.userChoice; if (c && c.outcome) outcome = c.outcome; } catch (_) { /* refused or already used */ }
  Shell.installChanged();
  return outcome;
};

Shell.listen = function () {
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    Shell.installPrompt = e;
    Shell.installChanged();
  });
  window.addEventListener('appinstalled', () => {
    Shell.installPrompt = null;
    Shell.installChanged();
    if (typeof toast === 'function') toast('ccboard is installed', { kind: 'ok' });
  });
  window.addEventListener('hashchange', () => { Shell.syncNav(); Shell.syncCrumbs(); Shell.syncSearchBox(); Shell.closeDrawer(); Shell.openCurrent(); });
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (document.querySelector('dialog[open]') || (typeof ui !== 'undefined' && ui.modal)) return;
    if (e.key === '/') { e.preventDefault(); Shell.focusSearch(); }
    else if (e.key === '[') { e.preventDefault(); Shell.toggleSidebar(); }
  });
  // An error raised while the sheet covers the board would be invisible: show it as a toast as well.
  if (typeof setError === 'function' && !Shell.errWrapped) {
    const base = setError;
    Shell.errWrapped = true;
    setError = function (msg) {
      base(msg);
      Shell.limit(typeof state === 'undefined' ? null : state);      // base() repainted #banner without the rate-limit callout
      const sh = document.getElementById('sheet');
      if (msg && sh && sh.open) toast(msg, { kind: 'bad' });
    };
  }
};

function installShell() {
  if (Shell.refs) return;
  const bar = $('#topbar');
  const side = $('#sidebar');
  const drawer = $('#drawer');
  const bnav = $('#bnav');
  if (!bar || !side || !drawer || !bnav) return;
  Shell.load();
  Shell.refs = {};
  Shell.buildTopbar(bar);
  Shell.buildSide(side, true);
  Shell.buildDrawer(drawer);
  Shell.buildBnav(bnav);
  const app = $('#app');
  if (app) { Shell.refs.resizer = Shell.buildResizer(app); Shell.setSbWidth(Shell.sbW, false); }
  Shell.watchMode();
  Shell.listen();
  if (typeof state !== 'undefined' && state) renderShell(state);
  else { Shell.syncNav(); Shell.syncCrumbs(); }
  Shell.openCurrent();                                            // a deep link to a project page opens its node (and the folders of ?path=)
}

/* The rate-limit callout (pages/widgets.js) lives in #banner, which renderBanner() empties on every render: it is put back right after. */
Shell.limit = function (st) {
  try { if (typeof Widgets !== 'undefined' && Widgets && typeof Widgets.limitBanner === 'function') Widgets.limitBanner(st); }
  catch (e) { console.error('ccboard limit banner', e); }
};

/* renderShell(st): everything the chrome shows, patched in place. render(force): renderShell + the current page's update(state, route). */
function renderShell(st) {
  if (!Shell.refs || !st) return;
  const model = Shell.model(st);
  Shell.patchHeader(st);
  Shell.patchUsage(st);
  Shell.patchCounts(model);
  Shell.patchTrees();
  Shell.syncNav();
  Shell.syncCrumbs();
  Shell.syncSearchBox();
}

function render(force) {
  if (typeof state === 'undefined' || !state) return;
  renderShell(state);
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  const page = r && typeof pages !== 'undefined' ? pages[r.id] : null;
  // a page that is not mounted yet gets its update() from route() right after its mount(); the router updates the base page and the overlay
  if (typeof updateCurrentPage === 'function') updateCurrentPage(state);
  else if (page && typeof page.update === 'function' && (typeof mountedId === 'undefined' || mountedId === r.id)) {
    try { page.update(state, r); } catch (e) { console.error('ccboard update', r.id, e); }
  }
  if (typeof renderBanner === 'function') renderBanner();
  Shell.limit(state);
  if (Shell.formWatch) Shell.formWatch();
}
