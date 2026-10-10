/* ccboard shell (v0.5.3): the topbar, the sidebar tree (the 48 px rail on medium widths), the drawer, the bottom nav, the width-driven shell modes, the
   keyboard shortcuts and the state consumers renderHeader() / renderUsage() / renderShell() / render() that core.js's poll() calls.
   Everything is built once by installShell() (main.js calls it) and patched in place on every poll: nothing here is re-created per poll, and the
   project tree is a keyed reconcile, so a focused row survives a state refresh. Classic script; one namespace (Shell) plus the contract functions.
   v0.5.9: the terminal dock (Shell.openDock / closeDock / toggleDock, section 'the terminal dock'): the live terminal of one session in #dock from
   1024 px up, built from TermKit.termPane (termkit.js), with a drag handle, the sidebar folding to the rail below 1440 px, and a row's Open link opening it.
   v0.5.13: create from anywhere. The + menu, the c-chords, the repo picker, the sidebar's + on a project row and the dock's + all end in launch() (components.js), which
   opens the launcher sheet (launcher.js openLauncher) with the project and the repo filled in: Shell.openCreate / launchAt / sideNew / dockNew. */
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
        ['tasks', 'Tasks', 'git-branch', '#/tasks'], ['quad', 'Quad', 'layout-grid', '#/quad'], ['usage', 'Usage', 'chart', '#/usage'], ['memory', 'Memory', 'database', '#/memory'],
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
  try { Shell.dock.w = Shell.clampDockW(parseInt(localStorage.getItem('ccboard:dock:w'), 10)); } catch (_) { Shell.dock.w = 0; }
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
  return { key: 's:' + Ref.key(s), kind: 'sess', tmux: s.tmux, node: Ref.nodeOf(s), name: s.name, repo, state: s.state || 'unknown', agent: sessionAgent(s), needs: !!s.needs_attention, at: t };
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
  R.pCodex.gl = el('span', { class: 'pg hidden', 'aria-hidden': 'true' }, ic('warning-sign'));          // shown only while the Codex limit is reached
  R.pCodex.insertBefore(R.pCodex.gl, R.pCodex.pl);
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
   {used_percent, window_minutes, resets_at}. Also accepts a bare window, a list of them or an object of them. The pill shows ONE window, labelled from its
   window_minutes. Which one (#32 e): when the record says `reached`, the REACHED window (a window at 100 %; the longest of several, else the fullest), so a reached
   5-hour limit is never shown as the weekly percentage; otherwise, with two windows, the fullest by percentage (a window whose reset has passed counts as 0 %; a tie
   goes to the longer); with one window, that one. */
Shell.codexWindow = function (u, now) {
  if (!u) return null;
  const v = u.value !== undefined ? u.value : u;
  const cand = [];
  const pct = (w) => (typeof w.used_percentage === 'number' ? w.used_percentage : w.used_percent);
  const add = (w) => { if (w && typeof w === 'object' && typeof pct(w) === 'number' && Number.isFinite(pct(w))) cand.push(w); };
  if (Array.isArray(v)) v.forEach(add);
  else { add(v); if (v && typeof v === 'object') Object.values(v).forEach(add); }
  if (!cand.length) return null;
  const rec = v && typeof v === 'object' && !Array.isArray(v) ? v : {};
  const t = typeof now === 'number' ? now : Date.now() / 1000;
  const rolled = (w) => typeof w.resets_at === 'number' && w.resets_at > 0 && w.resets_at <= t;
  const eff = (w) => (rolled(w) ? 0 : pct(w));
  const longer = (a, b) => (b.window_minutes || 0) - (a.window_minutes || 0);
  const fullest = (a, b) => eff(b) - eff(a) || longer(a, b);
  const reached = rec.reached === true;
  let pool = cand;
  if (reached) { const full = cand.filter((w) => eff(w) >= 100); if (full.length) pool = full; }
  const w = pool.slice().sort(reached && pool !== cand ? longer : (cand.length > 1 ? fullest : longer))[0];
  return { used_percentage: pct(w), resets_at: w.resets_at, minutes: w.window_minutes, plan: typeof rec.plan_type === 'string' ? rec.plan_type : '',
    reached, account: typeof rec.account === 'string' ? rec.account : '' };
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

/* One pill: w = {used_percentage, resets_at}, `what` the window's name for the title, `note` what the title adds after the reset (the freshness caption). */
Shell.patchPill = function (node, w, what, note) {
  if (!w || typeof w.used_percentage !== 'number') { node.classList.add('hidden'); return; }
  const pct = Math.max(0, Math.min(100, w.used_percentage));
  node.classList.remove('hidden');
  node.classList.toggle('ok', pct < 60);
  node.classList.toggle('warn', pct >= 60 && pct < 85);
  node.classList.toggle('bad', pct >= 85);
  setText(node.pv, `${Math.round(pct)}%`);
  setText(node.pr, w.resets_at ? fmtIn(w.resets_at) : '');
  node.setAttribute('title', `${what}: ${Math.round(pct)}% used` + (w.resets_at ? ` · resets in ${fmtIn(w.resets_at)}` : '') + (note ? ` · ${note}` : ''));
  Shell.setVar(node, '--pct', String(Math.round(pct)));
};

/* The pill of one Claude window of state.usage ('five_hour' | 'seven_day') at `now`: {w: {used_percentage, resets_at}, note} for Shell.patchPill, null when the
   record has no such window. Time-aware (core.js limitUsageWindow): a window whose reset has passed with no newer reading shows 0 % and counts down to the next
   reset instead of vanishing, and the title carries 'no usage recorded since the window reset' and when and where the reading came from. */
Shell.claudeWindow = function (st, key, now) {
  const x = limitUsageWindow(st, key === 'five_hour' ? '5h' : '7d', now);
  return x ? { w: { used_percentage: x.pct, resets_at: x.resets_at }, note: limitCaption(x) } : null;
};

/* The account chip of the topbar (v0.5.17b), or null with fewer than two accounts or no current one. {key, name, text, hue, title, amber}:
   text = the first two letters of the label (else the name, else the email), hue = chipHue('account', key) (one subscription, one hue everywhere).
   A login problem on the account in use (state.accounts.problem, pages/agents.js) wins: the chip is amber, its title says "Claude's login (<label>) is not valid any more · Log in again"
   and a tap goes to that account's row in Settings. Otherwise the chip is the amber tint, and its title names the better account and the window, when the current account is at 85 % or more of a window and another
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
  const problem = typeof acctProblemHit === 'function' && acctProblemHit(st, cur, 'claude') ? acctProblem(st) : null;      // the box was told this login does not work: that outranks "more room elsewhere"
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
    key: cur.key, name, amber: !!move || !!problem,
    text: letters.charAt(0).toUpperCase() + letters.slice(1),
    hue: move || problem ? 'hue-amber' : chipHue('account', cur.key),
    title: problem ? `${acctProblemText(problem)} · Log in again`
      : move ? `${move.who} has ${Math.round(move.left)} % of the ${move.win} window left` : `Account: ${name}${cur.plan ? ' (' + cur.plan + ')' : ''} · usage per account`,
    href: problem ? acctProblemHash(problem) : '#/usage',            // a tap on the amber chip goes to the account's row, where Log in again is the lead action
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
  R.acct.setAttribute('href', c.href || '#/usage');
  R.acct.setAttribute('aria-label', c.amber ? `${c.name}: ${c.title}` : c.title);      // the amber title alone would not say whose numbers the pills show
  return c;
};

Shell.patchUsage = function (st) {
  const R = Shell.refs;
  if (!R || !st) return;
  const rl = (st.usage && st.usage.value) || {};
  const who = Shell.patchAccount(st);
  const pre = who ? who.name + ' · ' : '';                       // with several accounts the pill title names the one whose numbers it shows
  const t = Date.now() / 1000;
  const five = Shell.claudeWindow(st, 'five_hour', t);
  const seven = Shell.claudeWindow(st, 'seven_day', t);
  Shell.patchPill(R.p5, five && five.w, pre + '5-hour window', five && five.note);
  Shell.patchPill(R.p7, seven && seven.w, pre + 'weekly window', seven && seven.note);
  Shell.patchPill(R.pSpend, rl.spend_limit, 'spend limit');
  Shell.patchCodex(st);
};

/* The CX pill (v0.5.12): ONE window of state.usage_codex, labelled from its window_minutes ('CX 7D' for the weekly window of 10080 minutes, 'CX 5H' for 300). A reading
   whose reset instant has passed is a window that rolled over (nothing counted in the new one yet): it shows 0 %, like the Usage page's account rows. A reached limit
   makes it red whatever the percentage says, with several Codex accounts the title names the one the reading belongs to, and the pill is the way to the Codex tab. */
Shell.patchCodex = function (st) {
  const R = Shell.refs;
  const cx = Shell.codexWindow(st.usage_codex);
  if (!cx) { R.pCodex.classList.remove('reached'); Shell.patchPill(R.pCodex, null, ''); return; }
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
  R.pCodex.classList.toggle('reached', cx.reached);                          // shell.css keeps a reached limit visible in the compact shell (#32 e)
  if (R.pCodex.gl) R.pCodex.gl.classList.toggle('hidden', !cx.reached);       // a glyph, so a reached limit is not told by red alone
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
  const add = el('button', { class: 'icon minimal small tn-add', type: 'button', tabindex: '-1', title: 'New session in this project', 'aria-label': 'New session in this project' }, ic('plus'));
  add.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); Shell.sideNew(add.getAttribute('data-project')); });
  const g = Shell.groupRow('proj-row', 1, '', [dot]);
  g.row.append(add, go);
  const kids = el('div', { class: 'tn-kids hidden', role: 'group' });
  const node = el('div', { class: 'tn proj' }, g.row, kids);
  node._r = { ...g, dot, go, add, kids };
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
  r.add.setAttribute('data-project', p.name);
  r.kids.classList.toggle('hidden', !open);
  if (open) Shell.sync(r.kids, Shell.projKids(p, level), Shell.kidNode, Shell.patchKid);
  else if (r.kids.firstChild) r.kids.textContent = '';
};

/* The + on a project row: the launcher sheet (launch(), components.js) for this project, in the repo (or folder) it worked in last, so a new session is one tap to Start & open.
   The drawer (a phone's sidebar) closes first. A project with no place yet gets the repo picker narrowed to it. */
Shell.sideNew = function (name) {
  Shell.closeDrawer();
  const st = typeof state === 'undefined' ? null : state;
  const p = st ? (st.projects || []).find((x) => x.name === name) : null;
  if (!p) return false;
  const hit = launchPlace(p, 'session');
  if (hit) return launch({ mode: 'session', project: hit.project, repo: hit.repo });
  return Shell.openCreate('session', { project: name });
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
  if (typeof Tree === 'undefined' && typeof Lazy !== 'undefined' && Lazy.bundles && Lazy.bundles.tree) {      // tree.js is a lazy bundle (lazy.js): the first folder to open loads it
    if (!Shell.dirLoading.has(k.key)) Lazy.run('tree', () => Shell.loadDir(k, fresh), 'the file tree');
    return;
  }
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
  else if (typeof Lazy !== 'undefined') Lazy.later('dnd', () => { if (typeof Dnd !== 'undefined') Dnd.bind(node); });     // dnd.js is lazy: the row is bound when a board that needs it has loaded it
  return node;
};

Shell.patchKid = function (node, k) {
  if (k.kind === 'dir') { Shell.patchDir(node, k); return; }
  if (k.kind === 'note') { node.setAttribute('data-key', k.key); setText(node, k.text); return; }
  const r = node._k;
  node.setAttribute('data-key', k.key);
  setText(r.name, k.name);
  if (k.kind === 'repo') { node.setAttribute('href', Shell.hash('project', { project: k.project, repo: k.name })); return; }
  node.setAttribute('href', k.node ? (Ref.hash(k) || '#/') : Shell.hash('session', { tmux: k.tmux }));      // a session of another node: #/n/<handle>/s/<tmux> (nodes.js); this board's keep #/s/<tmux>
  node.setAttribute('data-tmux', k.tmux);                                          // the drop target's session (dnd.js)
  if (k.node) node.setAttribute('data-node', k.node); else node.removeAttribute('data-node');     // with data-tmux it is the row's Ref.key: the same name on two nodes is two targets
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
  let items = model.main.slice();
  if (model.older.length) items.push({ key: 'older', kind: 'older', projects: model.older });
  if (typeof Nodes.sbItems === 'function') items = Nodes.sbItems(items);                // the hub view (nodes-hub.js): a "This node" label before, a collapsed group per paired node after; items it adds carry their own make / patch
  Shell.sync(tree, items, (it) => (it.make ? it.make(it) : it.kind === 'older' ? Shell.olderNode() : Shell.projNode()),
    (n, it) => (it.patch ? it.patch(n, it) : it.kind === 'older' ? Shell.patchOlder(n, it) : Shell.patchProj(n, it, 1)));
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
  if (e.target.closest && e.target.closest('.tn-add')) return;           // the + is a button: Enter and Space are its own
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
    if (e.target.closest('.tn-go') || e.target.closest('.tn-add')) return;                    // the arrow opens the project page, the + the launcher
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
  const head = el('div', { class: 'sb-h' }, el('span', { text: 'Projects' }),
    el('a', { class: 'sb-add', href: Shell.hash('onboarding', { step: 'project' }), title: 'New project', 'aria-label': 'New project', text: '+ project' }));        // v0.5.19: the wizard
  const inner = el('div', { class: 'sb-inner' }, nav, head, tree, tree._empty);
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

/* Where a Quad link goes: #/quad?p=<project> for the project the quad was last used on (ccboard:quad:scope, written by pages/quad.js), else #/quad (every project; also
   when that project is gone from the board: a project the state does not list is not offered). A bare #/quad that is not one of these links is always all projects. */
Shell.quadHref = function () {
  let p = '';
  try { p = localStorage.getItem('ccboard:quad:scope') || ''; } catch (_) { p = ''; }
  if (p === 'all' || !/^[A-Za-z0-9_-]+$/.test(p)) return '#/quad';
  const st = typeof state === 'undefined' ? null : state;
  if (st && Array.isArray(st.projects) && !st.projects.some((x) => x && x.name === p)) return '#/quad';
  return Shell.hash('quad', {}, { p });
};

/* The sidebar's and the drawer's Quad entries open the scope last used. */
Shell.syncQuadLinks = function () {
  const href = Shell.quadHref();
  for (const root of [$('#sidebar'), $('#drawer'), $('#bnav')]) {
    if (!root) continue;
    for (const a of root.querySelectorAll('[data-nav=quad]')) if (a.getAttribute('href') !== href) a.setAttribute('href', href);
  }
};

Shell.syncNav = function () {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  const id = r ? r.id : 'home';
  Shell.syncQuadLinks();
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
  if (r.id === 'node' || r.id === 'node-session' || r.id === 'node-task') return [{ text: p.node }].concat(p.tmux ? [{ text: p.tmux }] : (p.id ? [{ text: 'task ' + p.id }] : []));
  if (r.id === 'memory' && p.project) return [{ text: 'Memory', href: '#/memory' }, { text: p.project }];
  if (r.id === 'quad' && r.query && typeof r.query.p === 'string' && /^[A-Za-z0-9_-]+$/.test(r.query.p)) return [{ text: 'Quad · ' + r.query.p }];
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
  Shell.dockSync();                                               // a window that shrank below 1024 px takes the dock's terminal down; one that grew puts it back
  const sb = mode === 'compact' ? 'none' : (mode === 'medium' || !Shell.sbOpen || Shell.dockForcesRail()) ? 'rail' : 'full';    // an open dock folds the sidebar below 1440 px
  document.body.setAttribute('data-shell', mode);
  document.body.setAttribute('data-sb', sb);
  document.body.setAttribute('data-dock', Shell.dock.pane ? 'open' : 'closed');
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
  Shell.mq = { l: q('(min-width: 1200px)'), e: q('(min-width: 840px)'), m: q('(min-width: 600px)'), d: q('(min-width: 1024px)'), x: q('(min-width: 1440px)') };   // d: the dock exists, x: sidebar and dock fit together
  for (const m of Object.values(Shell.mq)) {
    if (m.addEventListener) m.addEventListener('change', () => Shell.applyMode());
    else if (m.addListener) m.addListener(() => Shell.applyMode());
  }
  Shell.applyMode();
};

Shell.setSidebar = function (open) {
  Shell.sbOpen = !!open;
  try { localStorage.setItem('ccboard:sb', open ? '1' : '0'); } catch (_) { /* storage may be unavailable */ }
  Shell.dock.keep = !!open && !!Shell.dock.pane && !Shell.dockRoomy();       // the full sidebar asked for beside an open dock below 1440 px stays until the dock closes
  Shell.applyMode();
};

Shell.navToggle = function () { if (Shell.wide()) Shell.setSidebar(true); else Shell.openDrawer(); };

Shell.toggleSidebar = function () {
  if (Shell.wide()) { Shell.setSidebar(document.body.getAttribute('data-sb') !== 'full'); return; }      // what is on screen decides (an open dock may be holding the rail)
  const dlg = $('#drawer');
  if (dlg && dlg.open) dlg.close(); else Shell.openDrawer();
};

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
  // the strip sits in a labelled region of its own (display: contents, so the grid placement below is unchanged): content outside every landmark is an axe 'region' finding
  app.append(el('div', { class: 'sb-resize-wrap', role: 'region', 'aria-label': 'Sidebar width' }, h));
  return h;
};

/* ---------- the terminal dock (v0.5.9): the live terminal of one session beside any page, from 1024 px up ----------
   One TermKit.termPane (full mode: it sizes the session like /term does) in #dock, with a drag handle on its left edge. The pane is created when the dock opens and destroyed
   when it closes or stops being allowed (a window under 1024 px, ccboard:dock:off): a mounted full-mode client sizes the tmux session even when it is hidden, so a hidden dock
   must not exist. localStorage: ccboard:dock = the session shown (restored on load), ccboard:dock:w = the width in px (absent = the CSS default min(44vw, 640px)),
   ccboard:dock:off = '1' turns the dock off in this browser (a row's Open then opens /term/<name> in its own tab as before). Below 1440 px an open dock folds the sidebar to
   the rail (body[data-sb=rail]) without writing ccboard:sb; asking for the full sidebar while the dock is open keeps it until the dock closes. The session peek (session.js)
   goes to the sheet while the dock is on, so the two never share #dock.
   The quad page (#/quad) and the dock are exclusive: the quad owns the window (its tiles are the terminals, and a tile's width must not change under it), so the dock is
   suspended while the quad is the page (Shell.dockSuspend, called by Quad.mount; Shell.dockResume by its destroy; mountedId === 'quad' is the same test the router makes):
   the pane goes (D.wanted keeps the session, ccboard:dock stays), no Open link opens it (they open a tab), and it comes back when the person leaves the quad. */

Shell.DOCK_MIN = 320;
Shell.DOCK_MAX = 960;
Shell.DOCK_AT = 1024;                    // the dock exists from this window width
Shell.DOCK_COMPACT = 1180;               // a touch window under this width keeps the page to itself: an Open link opens the terminal page (v0.5.21, #56)
Shell.DOCK_ROOM = 1440;                  // from this width the sidebar and the dock fit side by side
Shell.LONG_PRESS = 500;
Shell.TERM_HREF = /^\/term\/([^/?#]+)\/?$/;
Shell.dock = { tmux: '', pane: null, last: '', wanted: '', w: 0, keep: false, restored: false, resizer: null, press: null, held: false };

Shell.mqNow = function (query) {
  try { return !!(window.matchMedia && window.matchMedia(query).matches); } catch (_) { return false; }
};

Shell.dockStore = {
  get(k) { try { return localStorage.getItem(k); } catch (_) { return null; } },
  set(k, v) { try { if (v === null || v === undefined) localStorage.removeItem(k); else localStorage.setItem(k, String(v)); } catch (_) { /* storage may be unavailable */ } },
};

Shell.dockOff = function () { return Shell.dockStore.get('ccboard:dock:off') === '1'; };

/* Wide enough for a dock (1024 px up); the media query the shell already watches, else asked now. */
Shell.dockWide = function () { return Shell.mq && Shell.mq.d ? !!Shell.mq.d.matches : Shell.mqNow('(min-width: 1024px)'); };

/* Does this window have room for the sidebar and the dock together (1440 px up)? */
Shell.dockRoomy = function () { return Shell.mq && Shell.mq.x ? !!Shell.mq.x.matches : Shell.mqNow('(min-width: 1440px)'); };

/* The dock exists in this window and this browser: wide enough, not switched off, and the terminal kit is loaded. session.js asks this to send the peek elsewhere. */
Shell.dockOn = function () { return !Shell.dockOff() && Shell.dockWide() && (Shell.kitReady() || Shell.kitLazy()); };

/* termkit.js is a lazy bundle (lazy.js): the dock opens it on first use. kitReady: it is here; kitLazy: it is not, but can be had. */
Shell.kitReady = function () { return typeof TermKit !== 'undefined' && !!TermKit && typeof TermKit.termPane === 'function'; };
Shell.kitLazy = function () { return typeof TermKit === 'undefined' && typeof Lazy !== 'undefined' && !!Lazy.bundles && !!Lazy.bundles.termkit; };

Shell.dockOpen = function () { return !!Shell.dock.pane; };

/* Should an Open link or button open the dock? Not on a coarse primary pointer in a window under 1180 px (an iPad in portrait, a small tablet): the dock would leave the
   page about 525 px at 1024, and the pages are laid out for 840 and more. There an Open link opens the terminal page as before; mod+j and "Add to dock" still open the dock
   on purpose (and a dock opened that way comes back on the next load), with its width capped at 38 vw by default (shell.css) and a 44 px grabber to change it. */
Shell.dockAuto = function () {
  let coarse = false;
  try { coarse = typeof coarsePointer === 'function' ? !!coarsePointer() : false; } catch (_) { coarse = false; }
  return !(coarse && (Number(window.innerWidth) || 0) < Shell.DOCK_COMPACT);
};

/* Does the quad page own the window? Then the dock is suspended. D.held is the quad's own say (Shell.dockSuspend); mountedId === 'quad' is the router's (the session peek is an
   overlay: it never changes mountedId, so a peek over the quad is still the quad). During the quad's unmount mountedId is still 'quad': Shell.dockResume reconciles after it. */
Shell.dockHeld = function () { return !!Shell.dock.held || (typeof mountedId !== 'undefined' && mountedId === 'quad'); };

/* Quad.mount, before it measures its column: the dock lets go of the window (its pane is taken down at once, the sidebar goes back to its own rule). */
Shell.dockSuspend = function () {
  Shell.dock.held = true;
  if (Shell.mq) Shell.applyMode(); else Shell.dockSync();
  return true;
};

/* The quad is gone: the dock comes back (the saved session, when there is room). The router moves mountedId after the old page's unmount, so the mode pass is deferred one tick. */
Shell.dockResume = function () {
  Shell.dock.held = false;
  setTimeout(() => { if (!Shell.dockHeld() && Shell.mq) Shell.applyMode(); }, 0);
  return true;
};

/* Why the dock will not open, as a sentence (a toast on mod+j; the Open links simply navigate). */
Shell.dockWhy = function () {
  const say = (t) => { if (typeof toast === 'function') toast(t, { kind: 'info' }); };
  if (!Shell.kitReady() && !Shell.kitLazy()) say('The terminal kit is not loaded: reload the board');
  else if (Shell.dockOff()) say('The terminal dock is off in this browser (localStorage ccboard:dock:off)');
  else if (!Shell.dockWide()) say('The terminal dock needs a window 1024 px wide or more');
  else if (Shell.dockHeld()) say('The Quad page shows the terminals itself: the dock is back when you leave it');
};

Shell.clampDockW = function (n) {
  const v = Number(n);
  if (n === null || n === undefined || n === '' || !Number.isFinite(v) || v <= 0) return 0;
  const room = Math.floor((Number(window.innerWidth) || 1600) * 0.6);
  const max = Math.max(Shell.DOCK_MIN, Math.min(Shell.DOCK_MAX, room));
  return Math.round(Math.min(max, Math.max(Shell.DOCK_MIN, v)));
};

/* --dock-w lives on #app (shell.css reads it on #dock), set through the CSSOM: never a style attribute. 0 = back to the CSS default. */
Shell.setDockWidth = function (w, persist) {
  const app = $('#app');
  const px = Shell.clampDockW(w);
  Shell.dock.w = px;
  if (app) {
    if (px) Shell.setVar(app, '--dock-w', px + 'px');
    else { try { app.style.removeProperty('--dock-w'); } catch (_) { /* no CSSOM */ } }
  }
  const r = Shell.dock.resizer;
  if (r) {
    if (px) r.setAttribute('aria-valuenow', String(px)); else r.removeAttribute('aria-valuenow');
  }
  if (persist) Shell.dockStore.set('ccboard:dock:w', px ? px : null);
  return px;
};

/* Back to the default width: the saved value is removed (not overwritten), so the viewport-relative default applies again. */
Shell.resetDockWidth = function () { return Shell.setDockWidth(0, true); };

Shell.buildDockResizer = function () {
  const app = $('#app');
  const h = el('div', { class: 'dock-resize', role: 'separator', 'aria-orientation': 'vertical', 'aria-label': 'Resize the terminal dock', tabindex: '0',
    'aria-valuemin': String(Shell.DOCK_MIN), 'aria-valuemax': String(Shell.DOCK_MAX), title: 'Drag to resize the dock (double-click or double-tap resets)' });
  let drag = null;
  const end = () => {
    if (!drag) return;
    drag = null;
    if (app) app.classList.remove('dock-dragging');
    if (Shell.dock.w) Shell.setDockWidth(Shell.dock.w, true);
  };
  h.addEventListener('pointerdown', (e) => {
    if (e.button) return;
    e.preventDefault();
    const dock = $('#dock');
    drag = { grab: (dock ? dock.getBoundingClientRect().left : e.clientX) - e.clientX, moved: false };     // where on the strip it was taken
    try { h.setPointerCapture(e.pointerId); } catch (_) { /* no pointer capture: the move events still arrive over the strip */ }
    if (app) app.classList.add('dock-dragging');
  });
  h.addEventListener('pointermove', (e) => {
    if (!drag || !app) return;
    drag.moved = true;
    Shell.setDockWidth(app.getBoundingClientRect().right - (e.clientX + drag.grab), false);
  });
  let lastTap = 0;
  h.addEventListener('pointerup', (e) => {                              // a double tap on the grabber resets the width (a touch screen sends no dblclick here)
    if (drag && !drag.moved && e && (e.pointerType === 'touch' || e.pointerType === 'pen')) {
      const now = Date.now();
      if (now - lastTap < 350) { lastTap = 0; Shell.resetDockWidth(); } else lastTap = now;
    }
  });
  for (const t of ['pointerup', 'pointercancel', 'lostpointercapture']) h.addEventListener(t, end);
  h.addEventListener('dblclick', () => Shell.resetDockWidth());
  h.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 48 : 16;
    const cur = Shell.dock.w || (() => { const d = $('#dock'); const r = d ? d.getBoundingClientRect() : null; return r && r.width > 0 ? Math.round(r.width) : 450; })();
    let w = null;
    if (e.key === 'ArrowLeft') w = cur + step;                       // the dock is on the right: left grows it
    else if (e.key === 'ArrowRight') w = cur - step;
    else if (e.key === 'Home') w = Shell.DOCK_MIN;
    else if (e.key === 'End') w = Shell.DOCK_MAX;
    else if (e.key === 'Enter') { e.preventDefault(); Shell.resetDockWidth(); return; }
    if (w === null) return;
    e.preventDefault();
    Shell.setDockWidth(w, true);
  });
  return h;
};

/* The session the dock offers when nothing was asked for (mod+j with nothing open and nothing remembered): the one that needs you, else an errored, working, idle one,
   the latest first (the sidebar's order); '' when there is no live session. */
Shell.dockPick = function (st) {
  if (!st) return '';
  const m = Shell.model(st);
  const all = [];
  for (const p of [...m.main, ...m.older]) for (const k of p.kids) if (k.kind === 'sess' && k.state !== 'ended' && !k.node) all.push(k);      // the dock shows this board's terminals only
  all.sort((a, b) => ((a.needs ? 0 : 1) - (b.needs ? 0 : 1)) || ((SHELL_STATE_RANK[a.state] ?? 5) - (SHELL_STATE_RANK[b.state] ?? 5)) || (b.at - a.at) || a.tmux.localeCompare(b.tmux));
  return all.length ? all[0].tmux : '';
};

/* The session's row in a state payload, or null. */
Shell.dockRow = function (st, tmux) {
  if (!st || typeof rosterSessions !== 'function') return null;
  return rosterSessions(st).find((s) => s.tmux === tmux && Ref.nodeOf(s) === null) || null;      // the same name on another node is not this terminal
};

/* Paint the open pane from the state (called by renderShell on every render). */
Shell.patchDock = function (st) {
  const D = Shell.dock;
  if (!D.pane || !st) return;
  try { D.pane.update(Shell.dockRow(st, D.tmux), st); } catch (e) { console.error('ccboard dock', e); }
};

/* {project, repo} (state objects) of the place a session name project--repo--name is in, or null when the board does not know it (yet). */
Shell.placeOf = function (tmux) {
  const st = typeof state === 'undefined' ? null : state;
  const parts = String(tmux || '').split('--');
  if (!st || parts.length < 3) return null;
  const p = (st.projects || []).find((x) => x.name === parts[0]);
  const r = p ? (parts[1] === 'root' ? p.root : (p.repos || []).find((x) => x.name === parts[1])) : null;
  return p && r ? { project: p, repo: r } : null;
};

/* The dock's +: a new session in the place of the one the dock shows (launch(), components.js: the launcher sheet with that project and repo filled in, one tap to Start & open).
   When the board no longer knows the place, the repo picker. */
Shell.dockNew = function (tmux) {
  const place = Shell.placeOf(tmux || Shell.dock.tmux);
  if (place) return launch({ mode: 'session', project: place.project, repo: place.repo });
  return Shell.openCreate('session');
};

/* The + among the pane's own header buttons (termkit.js builds those; it has no callback for this one), first, before reconnect. */
Shell.dockAddButton = function (pane, tmux) {
  const btns = pane && pane.head && typeof pane.head.querySelector === 'function' ? pane.head.querySelector('.tp-btns') : null;
  if (!btns) return null;
  const add = el('button', { class: 'icon minimal tp-btn', type: 'button', 'data-act': 'new', 'aria-label': 'New session in this repo', title: 'New session in this repo', onclick: () => Shell.dockNew(tmux) }, ic('plus'));
  btns.insertBefore(add, btns.firstChild);
  return add;
};

/* DOM only (no mode pass): put a pane for `tmux` into #dock, replacing the one there. */
Shell.dockMount = function (tmux, o) {
  const D = Shell.dock;
  const host = $('#dock');
  if (!host) return false;
  const typing = TermKit.typingTarget();                              // a composer that has the keyboard keeps it, however the dock was opened
  let pane = null;
  try {                                                               // built detached first: a name that cannot be shown leaves the dock as it was
    pane = TermKit.termPane(tmux, {
      mode: 'full',
      restoreFocus: typing,
      holdFocus: !o.focus || !!typing,                                // opened by a restore, a route or a key: nothing may land in the terminal unasked
      onClose: () => Shell.closeDock(),
      onPopOut: (t) => Shell.popOutDock(t),
      onAddToQuad: (t) => Shell.quadAdd(t),
    });
  } catch (e) {
    if (typeof toast === 'function') toast('Not a terminal session name', { kind: 'bad' });
    return false;
  }
  Shell.dockAddButton(pane, tmux);
  Shell.dockUnmount(true);
  if (!D.resizer) D.resizer = Shell.buildDockResizer();
  host.textContent = '';
  host.append(D.resizer, pane.root);
  host.classList.add('has-term');
  host.classList.remove('hidden');
  D.pane = pane;
  D.tmux = tmux;
  D.last = tmux;
  if (D.w) D.resizer.setAttribute('aria-valuenow', String(D.w));
  if (typeof state !== 'undefined' && state) Shell.patchDock(state);
  return true;
};

/* DOM only: take the pane down (the iframe goes to about:blank first, see termPane.destroy) and hide #dock. */
Shell.dockUnmount = function (keepHost) {
  const D = Shell.dock;
  if (D.pane) { try { D.pane.destroy(); } catch (e) { console.error('ccboard dock', e); } }
  D.pane = null;
  D.tmux = '';
  const host = $('#dock');
  if (!host) return;
  host.textContent = '';
  host.classList.remove('has-term');
  if (!keepHost) host.classList.add('hidden');
};

/* Show `tmux` in the dock. opts.focus: the person asked (a click on Open, mod+j): the terminal takes the keyboard unless a text field has it; without it (a restore) the
   terminal never takes focus by itself. opts.quiet: no toast when the dock is not available. true when the dock shows the session. */
Shell.openDock = function (tmux, opts) {
  const o = opts || {};
  const D = Shell.dock;
  if (typeof tmux !== 'string' || !tmux) return false;
  if (!Shell.dockOn() || Shell.dockHeld()) { if (!o.quiet) Shell.dockWhy(); return false; }          // the quad owns the window: no dock over it
  if (!$('#dock')) return false;
  if (!Shell.kitReady()) {                                                                            // the terminal kit loads now; the dock opens when it is here (true: it is on its way)
    Lazy.load('termkit').then(() => { if (Shell.kitReady() && !Shell.dockHeld() && !Shell.dockOff()) Shell.openDock(tmux, o); }, (e) => { if (!o.quiet) Lazy.fail('the terminal', e); });
    return true;
  }
  if (D.pane && D.tmux === tmux) {
    if (o.focus && !TermKit.typingTarget()) D.pane.focus();
    return true;
  }
  if (!Shell.dockMount(tmux, o)) return false;
  Shell.dockStore.set('ccboard:dock', tmux);
  if (Shell.mq) Shell.applyMode();
  if (o.focus && !TermKit.typingTarget()) D.pane.focus();
  return true;
};

/* The one call for "open this session's terminal": the dock when it is on (true), else its own page through openPage (false). For the keys and the palette. */
Shell.openTerm = function (tmux) {
  if (Shell.dockAuto() && Shell.openDock(tmux, { focus: true, quiet: true })) return true;
  if (typeof tmux === 'string' && tmux) { const url = '/term/' + encodeURIComponent(tmux); if (typeof openPage === 'function') openPage(url); else window.open(url, '_blank', 'noopener'); }
  return false;
};

/* Close the dock and forget it (the saved session goes too, the full sidebar the person asked for goes back to the rule); mod+j brings back the one that was shown. */
Shell.closeDock = function () {
  const D = Shell.dock;
  Shell.dockUnmount(false);
  D.keep = false;
  D.wanted = '';
  Shell.dockStore.set('ccboard:dock', null);
  if (Shell.mq) Shell.applyMode();
  return true;
};

/* mod+j: close the dock when it is open; else open the last one shown, else the one that needs you most. */
Shell.toggleDock = function () {
  const D = Shell.dock;
  if (D.pane) { Shell.closeDock(); return true; }
  if (!Shell.dockOn() || Shell.dockHeld()) { Shell.dockWhy(); return false; }
  const t = D.last || Shell.dockPick(typeof state === 'undefined' ? null : state);
  if (!t) { if (typeof toast === 'function') toast('No live session to show', { kind: 'info' }); return false; }
  return Shell.openDock(t, { focus: true });
};

/* The window crossed 1024 px or the dock was switched off: the pane goes (it must not hold a hidden full-mode client) and comes back with the room. Runs inside applyMode. */
Shell.dockSync = function () {
  const D = Shell.dock;
  const on = Shell.dockOn();
  const held = Shell.dockHeld();
  if (D.pane && (!on || held)) { D.wanted = D.tmux; Shell.dockUnmount(false); }              // below 1024 px, switched off, or the quad page: the pane goes, ccboard:dock stays
  else if (!D.pane && D.wanted && on && !held) {
    if (!Shell.kitReady()) { Lazy.load('termkit').then(() => { if (Shell.kitReady() && Shell.mq) Shell.applyMode(); }, () => { /* the dock stays wanted; the next mode pass asks again */ }); return; }
    const t = D.wanted; D.wanted = ''; Shell.dockMount(t, {});
  }
};

/* ccboard:dock:off from outside (a setting, a test): true turns the dock off and closes it. */
Shell.setDockOff = function (off) {
  Shell.dockStore.set('ccboard:dock:off', off ? '1' : null);
  if (off && Shell.dock.pane) Shell.closeDock();
  else if (Shell.mq) Shell.applyMode();
};

/* Does the open dock hold the sidebar at the rail? (below 1440 px, unless the person asked for the full sidebar while it is open) */
Shell.dockForcesRail = function () { const D = Shell.dock; return !!D.pane && !D.keep && !Shell.dockRoomy(); };

/* First state after load: the session the dock showed last time comes back, when it is still alive. */
Shell.restoreDock = function (st) {
  const D = Shell.dock;
  const t = Shell.dockStore.get('ccboard:dock');
  if (!t) return false;
  const row = Shell.dockRow(st, t);
  if (!row || row.state === 'ended') { Shell.dockStore.set('ccboard:dock', null); return false; }
  if (Shell.dockHeld()) { D.wanted = Shell.dockOff() ? '' : t; return false; }                          // a load straight onto the quad: the dock comes back when it is left
  if (!Shell.dockOn()) { D.wanted = !Shell.dockOff() && !Shell.dockWide() ? t : ''; return false; }       // a narrow window keeps it for the width that fits
  return Shell.openDock(t, { quiet: true });
};

/* Pop out: the session in its own window (/term/), and the dock lets go of it: two full clients on one session would fight over its size. */
Shell.popOutDock = function (tmux) {
  const t = tmux || Shell.dock.tmux;
  if (!t) return false;
  const url = '/term/' + encodeURIComponent(t);
  if (typeof openPage === 'function') openPage(url); else window.open(url, '_blank', 'noopener');
  Shell.closeDock();
  return true;
};

/* The quad's saved slots (ccboard:quad:all = {layout, slots[], ...}), as far as they can be read: [] when there are none. */
Shell.quadSlots = function () {
  try {
    const v = JSON.parse(Shell.dockStore.get('ccboard:quad:all') || 'null');
    if (v && Array.isArray(v.slots)) return v.slots.filter((x) => typeof x === 'string' && x).slice(0, 4);
  } catch (_) { /* an unreadable entry is no slots */ }
  return [];
};

/* Add a session to the quad view and go there; the dock lets go of the session (the quad shows it). The quad page owns its saved slots, so when pages/quad.js is
   loaded this is Quad.addToQuad(tmux, project) (the project scope of the quad that is up, else the board-wide one). Without it (a partial deploy) the session is handed
   over as ?s= instead: the saved slots plus this one (a full quad gives up its last slot), with l=4 once there are more than two; the address wins over what is
   saved and the page writes it back. */
Shell.quadAdd = function (tmux) {
  if (typeof tmux !== 'string' || !tmux) return false;
  if (typeof Quad !== 'undefined' && Quad && typeof Quad.addToQuad === 'function') {
    if (Shell.dock.pane && Shell.dock.tmux === tmux) Shell.closeDock();
    return !!Quad.addToQuad(tmux, Quad.current && Quad.current.project ? Quad.current.project : '');
  }
  const slots = Shell.quadSlots();
  if (!slots.includes(tmux)) { if (slots.length >= 4) slots.pop(); slots.push(tmux); }
  const query = { s: slots.join(',') };
  if (slots.length > 2) query.l = '4';
  if (Shell.dock.pane && Shell.dock.tmux === tmux) Shell.closeDock();
  Shell.go(Shell.hash('quad', {}, query));
  return true;
};

/* A click on a link to /term/<session>, from 1024 px up and with the dock on, opens the dock instead of a new tab. Left button, no modifier (those keep the browser's own
   behaviour), not held for LONG_PRESS ms (a long press navigates, as it always did), not the dock's own pop-out link. Without the dock the click is left alone.
   Registered on <body> in the capture phase, so it also runs before core.js's installed-app handler, which would navigate this window to /term/ instead. */
Shell.onTermPress = function (e) {
  const a = e && e.target && typeof e.target.closest === 'function' ? e.target.closest('a[href]') : null;
  Shell.dock.press = a && Shell.TERM_HREF.test(a.getAttribute('href') || '') ? { a, t: Date.now() } : null;
};

Shell.onTermLink = function (e) {
  if (!e || e.defaultPrevented) return false;
  if (e.button !== undefined && e.button !== 0) return false;
  if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return false;
  const a = e.target && typeof e.target.closest === 'function' ? e.target.closest('a[href]') : null;
  if (!a) return false;
  const m = Shell.TERM_HREF.exec(a.getAttribute('href') || '');
  if (!m) return false;
  const press = Shell.dock.press;
  Shell.dock.press = null;
  if (a.closest('#dock')) return false;
  if (press && press.a === a && Date.now() - press.t >= Shell.LONG_PRESS) return false;
  if (a.getAttribute('data-dock') === 'skip') return false;             // a link that pops out on purpose (the quad tile's open link)
  if (!Shell.dockOn() || Shell.dockHeld() || !Shell.dockAuto()) return false;               // on the quad the link opens its own tab; so it does on a touch window under 1180 px
  let tmux = '';
  try { tmux = decodeURIComponent(m[1]); } catch (_) { return false; }
  if (!Shell.openDock(tmux, { focus: true, quiet: true })) return false;
  if (typeof e.preventDefault === 'function') e.preventDefault();
  try { if (typeof isStandalone === 'function' && isStandalone() && typeof e.stopPropagation === 'function') e.stopPropagation(); } catch (_) { /* no navigator */ }
  const sheet = a.closest('#sheet');
  if (sheet && typeof closeSheet === 'function') closeSheet();         // the peek sheet this link sat in would cover the dock
  return true;
};

/* ---------- create menu: repo picker sheet, then the launcher sheet (launch(), components.js) ---------- */

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
   session | task | schedule (a repo picker, then the launcher sheet), project (no sheet: it goes to the onboarding wizard, #/onboarding/project, where the name rules and the clone check live), import (GitHub
   repos into a project) or batch (one headless prompt over many repos). ctx = {project, repo?} (the project page's buttons) skips the picker
   when the place is clear: the form opens for that repo ('root' is the project folder: a session always, a task or schedule when it is itself
   a git repo), or for the project's only place; a session in a project with several places and no repo in ctx opens where the project worked last (launchPlace), a project with
   no place at all gets the picker narrowed to it. Without a ctx
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
  if (kind === 'session' || kind === 'task' || kind === 'schedule' || kind === 'job') Shell.withCreate(() => Shell.createPlace(kind, ctx));      // shell-create.js: the place, the picker, the form
  else if (kind === 'project') Shell.go(Shell.hash('onboarding', { step: 'project' }));          // v0.5.19: the wizard (pages/onboarding.js) replaced the new-project sheet
  else if (kind === 'import') Shell.withForms(() => Shell.formSheet('Import repos from GitHub', importForm, 'Import queued'));
  else if (kind === 'batch') Shell.withForms(() => Shell.formSheet('Batch prompt across repos', batchForm, 'Batch queued'));
  else return false;
  return true;
};

/* The create flow (shell-create.js: routeCtx, createFor, launchAt, pickRepo, showForm, createPlace) is a lazy bundle (lazy.js): fn runs now when it is here, else once it has loaded. */
Shell.withCreate = function (fn) {
  if (typeof Lazy === 'undefined' || Lazy.done('shellcreate')) return fn();
  Lazy.run('shellcreate', fn, 'the create sheet');
  return undefined;
};

/* The launcher's forms (launcher.js) are a lazy bundle (lazy.js): fn runs now when they are here, else once they have loaded. */
Shell.withForms = function (fn) {
  if (typeof Lazy === 'undefined' || Lazy.done('launcher')) return fn();
  Lazy.run('launcher', fn, 'the launcher');
  return undefined;
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

Shell.sheetClosed = function () { if (ui.openForm === 'sheet') ui.openForm = null; Shell.formWatch = null; };

/* A launcher form (import, batch) in the sheet: it calls onDone after its call succeeded, which closes the sheet with a toast. */
Shell.formSheet = function (title, build, done) {
  Shell.formWatch = null;
  const form = build({ onDone: () => { closeSheet(); toast(done, { kind: 'ok' }); }, onCancel: () => closeSheet() });
  openSheet({ title, body: form, onClose: Shell.sheetClosed });
  if (typeof form.focusFirst === 'function') form.focusFirst();
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
  if (document.body) {                                              // a row's Open link opens the dock from 1024 px up (capture: before core.js's installed-app handler)
    document.body.addEventListener('pointerdown', Shell.onTermPress, true);
    document.body.addEventListener('click', Shell.onTermLink, true);
  }
  window.addEventListener('hashchange', () => { Shell.syncNav(); Shell.syncCrumbs(); Shell.syncSearchBox(); Shell.closeDrawer(); Shell.openCurrent(); });
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (t && typeof t.closest === 'function' && t.closest('[data-kbd=chart]')) return;      // a keyboard chart (charts.js) keeps its keys
    if (document.querySelector('dialog[open]')) return;
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
  if (app) { Shell.refs.resizer = Shell.buildResizer(app); Shell.setSbWidth(Shell.sbW, false); if (Shell.dock.w) Shell.setDockWidth(Shell.dock.w, false); }
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
  Nodes.sync(st);                                                                       // the hub view's poll starts (or stops) with state.nodes_enabled
  Shell.patchTrees();
  Shell.syncNav();
  Shell.syncCrumbs();
  Shell.syncSearchBox();
  if (!Shell.dock.restored) { Shell.dock.restored = true; Shell.restoreDock(st); }       // once: the session the dock showed last time
  Shell.patchDock(st);
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
