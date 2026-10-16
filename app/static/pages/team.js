/* ccboard Team page (issue #200, Team P4), the lazy bundle 'teamview' (lazy.js, with pages/team-view.css; it needs the 'team' bundle): the whole team across every shared repository in one place.
   Four surfaces draw from the one list the board already holds (GET /api/team/state, `overview` and each repository's `free`; app/team.py overview() and free_of()):
     #/team          the page: rows grouped by person or by repository (a segmented control), filters for person, repository and state, and the free issues of each shared repository;
     the sidebar     one collapsed group "Team" below "Other nodes", with the number of teammates who are live (Shell.patchTree asks TeamView.sbItems);
     Home            one quiet line, "3 teammates active in 2 repos", linking here (home.js asks TeamView.homePaint);
     the palette     "Team" and the Team block of each shared repository (palette.js asks TeamView.paletteGroup).
   Nothing here loads, asks the network or draws until the board says some repository is shared (state.team.shared): core.js teamUse() is the only eager hook. The page is read only: it
   has no action on a teammate's work, and its one filled button is the empty state's link to Settings > Team. People are people, never nodes.
   GitHub text (logins, titles, branch names) is untrusted: cut, stripped of control and bidi characters, set with textContent only, and a link is built only for https://github.com paths
   (team.js teamText, teamLogin, teamSlug, teamLink). "Free" is never guessed: when the board did not read the open issues (unread) or a holder check is unknown, the page says so in words. */
'use strict';

const TV_STATES = {                                           // row state -> [glyph, words, what it means]; state is never colour alone
  working: ['✽', 'Working', 'A board of this person is running a session on it.'],
  waiting: ['✻', 'Waiting', 'A board of this person is waiting for them.'],
  idle: ['∙', 'Idle', 'A board of this person is on, with no session running.'],
  unconfirmed: ['◌', 'Not confirmed yet', 'A live status was seen for the first time and is not confirmed yet.'],
  stale: ['!', 'Stale', 'The last live status or read is old, so this may have changed.'],
  ended: ['○', 'Ended', 'The session on this work ended.'],
  unknown: ['?', 'Unknown', 'GitHub gave no clock for this status.'],
  github: ['◦', 'On GitHub', 'GitHub shows this work. This person publishes no live status.'],
};
const TV_KINDS = { presence: 1, issue: 1, pr: 1, branch: 1, seen: 1 };
const TV_ROWS_MAX = 600;
const TV_SIDEBAR_MAX = 8;
const TV_HELD_MAX = 20;
const TV_KEY_RE = /^(?:id:[0-9]{1,12}|login:[A-Za-z0-9\[\]-]{1,60})$/;

const TeamView = { ready: true, stopWatch: null, last: '', page: null };

/* ---------- the data ---------- */

/* The board's overview, or null when there is none (nothing read yet, or an answer of another shape). */
function tvData() {
  const d = teamStore.data;
  const o = d && typeof d === 'object' ? d.overview : null;
  return o && typeof o === 'object' && o.counts && typeof o.counts === 'object' && Array.isArray(o.rows) && Array.isArray(o.people) ? o : null;
}

function tvCount(o, k) {
  const n = o && o.counts ? o.counts[k] : 0;
  return Number.isInteger(n) && n >= 0 && n < 1e6 ? n : 0;
}

/* A word for a row's own line, aged since the read (a line that outlived the lease is stale whatever the last read said; an old line is never made live). */
function tvWord(r) {
  if (r.live === null || r.live === undefined) return null;
  const w = teamLiveWord(r, { read_at: r.read_at });
  return w === 'gone' ? 'stale' : w;
}

/* The answers below are worked out again only when the store holds another data object or the minute changed (a line ages out by the clock): every shell patch, Home paint and palette
   open asks for them, and the data changes only when the poll brings a new answer. The cache holds one entry per kind and is dropped with the data. */
const tvMemo = { rows: null, live: null, people: null };

function tvMemoGet(slot, o) {
  const m = tvMemo[slot];
  const minute = Math.floor(Date.now() / 60000);
  return m && m.o === o && m.data === teamStore.data && m.minute === minute ? m.value : undefined;
}

function tvMemoSet(slot, o, value) {
  tvMemo[slot] = { o, data: teamStore.data, minute: Math.floor(Date.now() / 60000), value };
  return value;
}

/* The rows as the page draws them: every field checked again, a row of an unusable repository name dropped, the state made stale when its line has aged out. */
function tvRows(o) {
  const hit = tvMemoGet('rows', o);
  if (hit !== undefined) return hit;
  return tvMemoSet('rows', o, tvRowsMake(o));
}

function tvRowsMake(o) {
  const out = [];
  for (const r of (o && Array.isArray(o.rows) ? o.rows : []).slice(0, TV_ROWS_MAX)) {
    if (!r || typeof r !== 'object') continue;
    const slug = teamSlug(r.slug);
    const key = typeof r.person === 'string' && TV_KEY_RE.test(r.person) ? r.person : '';
    if (!slug || !key) continue;
    const word = tvWord(r);
    let state = ownKey(TV_STATES, r.state) ? r.state : 'unknown';
    if (word === 'stale' && (state === 'working' || state === 'waiting' || state === 'idle' || state === 'unconfirmed')) state = 'stale';
    const claim = r.claim && typeof r.claim === 'object' && typeof r.claim.status === 'string' ? { status: teamText(r.claim.status, 20), live: r.claim.live === true } : null;
    out.push({ key, login: teamLogin(r.login), you: r.you === true, slug, repoUrl: r.repo_url, readAt: typeof r.read_at === 'string' ? r.read_at : '', repoStale: r.stale === true,
      kind: ownKey(TV_KINDS, r.kind) ? r.kind : 'seen', issue: teamNum(r.issue), issueUrl: r.issue_url, title: teamText(r.title, 200), branch: teamText(r.branch, 120), branchUrl: r.branch_url,
      pr: teamNum(r.pr), prUrl: r.pr_url, draft: r.draft === true, agent: ownKey(TEAM_AGENT, r.agent) ? r.agent : null, state, word, since: typeof r.since === 'string' ? r.since : '',
      last: typeof r.last === 'string' ? r.last : '', seen: word ? teamSeenS(r, { read_at: r.read_at }) : null, claim, collides: r.collides === true });
  }
  return out;
}

/* Who is live: the teammates (never the owner) with a line that is live or idle in a repository that was read lately, and the repositories they are active in. The same rule as the
   board's counts (app/team.py overview), made again here so an answer that has aged is not counted. */
function tvLive(o) {
  const hit = tvMemoGet('live', o);
  if (hit !== undefined) return hit;
  const people = new Set();
  const repos = new Set();
  for (const v of tvRows(o)) {
    if (v.you || v.repoStale || (v.word !== 'live' && v.word !== 'idle')) continue;
    people.add(v.key);
    repos.add(v.slug);
  }
  return tvMemoSet('live', o, { people: people.size, repos: repos.size });
}

function tvBlock(slug) { return teamBlocks().find((b) => b.slug === slug) || null; }

function tvSharedBlocks() { return teamBlocks().filter((b) => b.shared === true); }

/* ---------- the sidebar: a collapsed group "Team" with the number of live teammates ---------- */

function tvGroupNode() {
  const g = Shell.groupRow('proj-row tm-grp', 1, '', []);
  const go = el('a', { class: 'tn-go', tabindex: '-1', title: 'Open Team', 'aria-label': 'Open Team', href: '#/team' }, ic('chevron-right'));
  g.row.append(go);
  const kids = el('div', { class: 'tn-kids hidden', role: 'group' });
  const node = el('div', { class: 'tn proj tm-group' }, g.row, kids);
  node._r = { ...g, go, kids };
  return node;
}

function tvPersonKid() {
  const g = el('span', { class: 'tn-g tm-pg', 'aria-hidden': 'true' });
  const name = el('span', { class: 'tn-name' });
  const sub = el('span', { class: 'tn-sub mono' });
  const node = el('a', { class: 'tn-row r-row tm-prow', role: 'treeitem', tabindex: '-1', 'aria-level': '2' }, g, name, sub);
  node._k = { g, name, sub };
  return node;
}

function tvGroupPatch(node, it) {
  const r = node._r;
  const open = Shell.open.has('team');
  node.setAttribute('data-key', 'team');
  node.classList.toggle('open', open);
  r.row.setAttribute('aria-expanded', open ? 'true' : 'false');
  setText(r.name, 'Teammates');                                               // the section label above already says Team
  setText(r.cnt, it.live ? String(it.live) : '');
  r.cnt.setAttribute('title', it.live ? `${teamPlural(it.live, 'teammate')} live` : '');
  r.row.setAttribute('title', it.live ? `Team: ${teamPlural(it.live, 'teammate')} live` : 'Team: nobody live');
  r.go.setAttribute('href', '#/team');
  r.kids.classList.toggle('hidden', !open);
  if (!open) { if (r.kids.firstChild) r.kids.textContent = ''; return; }
  const kids = it.people.slice(0, TV_SIDEBAR_MAX).map((p) => ({ ...p, pkey: p.key, key: `tp:${p.key}`, kind: 'person' }));
  if (it.people.length > TV_SIDEBAR_MAX) kids.push({ key: 'tp:more', kind: 'more', text: `more (${it.people.length - TV_SIDEBAR_MAX})` });
  else if (!kids.length) kids.push({ key: 'tp:none', kind: 'note', text: it.ready ? 'nobody live' : 'reading GitHub' });
  Shell.sync(r.kids, kids, (k) => (k.kind === 'person' ? tvPersonKid() : k.kind === 'more' ? el('a', { class: 'tn-row r-row nd-more', role: 'treeitem', tabindex: '-1', 'aria-level': '2' }) : Shell.kidNode(k)),
    (n, k) => {
      if (k.kind === 'more') { n.setAttribute('data-key', k.key); n.setAttribute('href', '#/team'); setText(n, k.text); return; }
      if (k.kind !== 'person') { Shell.patchKid(n, k); return; }
      const p = n._k;
      n.setAttribute('data-key', k.key);
      n.setAttribute('href', tvHash({ person: k.pkey }));
      setText(p.g, TEAM_LIVE[k.live][0]);
      setText(p.name, k.label);
      setText(p.sub, k.repos);
      n.setAttribute('title', `${k.label}: ${TEAM_LIVE[k.live][1]}`);
    });
}

/* The people who are live or idle, for the group's rows: [{key, label, live, repos}] in the board's order (the best line first). */
function tvLivePeople(o) {
  const hit = tvMemoGet('people', o);
  if (hit !== undefined) return hit;
  const out = [];
  for (const p of o && Array.isArray(o.people) ? o.people : []) {
    if (!p || typeof p !== 'object' || p.you === true || typeof p.key !== 'string' || !TV_KEY_RE.test(p.key)) continue;
    if (p.live !== 'live' && p.live !== 'idle') continue;
    const slugs = (Array.isArray(p.repos) ? p.repos : []).map(teamSlug).filter(Boolean);
    out.push({ key: p.key, label: teamWho({ login: p.login }), live: p.live, repos: slugs.length > 1 ? `${slugs.length} repos` : (slugs[0] || '') });
  }
  return tvMemoSet('people', o, out);
}

/* Called by Shell.patchTree with the sidebar's items (after the node groups): the group, behind a label of its own, so it is never read as a project or as a node. */
TeamView.sbItems = function (items) {
  if (!teamShared()) return items;
  const o = tvData();
  const live = o ? tvLive(o).people : 0;
  const people = o ? tvLivePeople(o) : [];
  const out = items.slice();
  out.push({ key: 'team-sbh', make: () => el('div', { class: 'sb-h tm-sbh', role: 'presentation' }), patch: (n) => { n.setAttribute('data-key', 'team-sbh'); setText(n, 'Team'); } });
  out.push({ key: 'team', live, people, ready: !!o, make: tvGroupNode, patch: tvGroupPatch });
  return out;
};

/* ---------- Home: one quiet line ---------- */

/* {glyph, text, kind} for Home, or null: teammates active; else a repository that cannot be read or is read for the first time (one next step in the link); else nothing. */
function tvHomeMessage() {
  const o = tvData();
  if (!o) return null;
  const l = tvLive(o);
  if (l.people > 0) return { glyph: '●', kind: 'ok', text: `${teamPlural(l.people, 'teammate')} active in ${teamPlural(l.repos, 'repo')}` };
  const problem = tvCount(o, 'problem');
  if (problem > 0) return { glyph: '!', kind: 'warn', text: `Team: ${teamPlural(problem, 'repo')} could not be read. See why` };
  if (tvCount(o, 'loading') > 0) return { glyph: '∙', kind: 'dim', text: 'Team: reading GitHub for the first time' };
  return null;
}

/* Paints the line into Home (refs is homePage.refs: the node is made after the away line when there is something to say and taken away when there is not, so a board that shares nothing has
   no node here at all). */
TeamView.homePaint = function (refs) {
  if (!refs || !refs.away || !refs.away.parentNode) return;
  const m = teamShared() ? tvHomeMessage() : null;
  if (!m) { if (refs.team) { refs.team.remove(); refs.team = null; } return; }
  if (!refs.team) {
    refs.team = el('p', { class: 'tm-homeline' });
    refs.away.parentNode.insertBefore(refs.team, refs.away.nextSibling);
  }
  const sig = `${m.glyph}|${m.text}`;
  if (refs.team.getAttribute('data-sig') === sig) return;
  refs.team.setAttribute('data-sig', sig);
  refs.team.setAttribute('data-kind', m.kind);
  refs.team.textContent = '';
  refs.team.append(el('a', { class: 'tm-homeline-a', href: '#/team', title: 'Open Team' }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: m.glyph }), ' ', m.text));
};

/* ---------- the palette ---------- */

TeamView.paletteGroup = function (query, close) {
  if (!teamShared()) return null;
  const o = tvData();
  const live = o ? tvLive(o).people : 0;
  const go = (h) => { close(); Palette.go(h); };
  const items = [{ id: 'tm:open', label: 'Team', hint: live ? `${teamPlural(live, 'teammate')} active` : 'who is working on what', keywords: 'team teammates people shared repos free issues', run: () => go('#/team') }];
  for (const b of tvSharedBlocks()) {
    const l = Array.isArray(b.local) ? b.local.find((x) => x && typeof x.project === 'string' && typeof x.repo === 'string') : null;
    if (!l) continue;
    let h = null;
    try { h = buildHash('project', { project: l.project, repo: l.repo }, { tab: 'team' }); } catch (_) { h = null; }
    if (h) items.push({ id: `tm:${b.repo_slug}`, label: `Team: ${b.slug}`, hint: TEAM_STATES[teamState(b)][1], keywords: `team block ${b.slug} ${l.project} ${l.repo}`, run: () => go(h) });
  }
  return { id: 'team', title: 'Team', items, limit: query ? Palette.PER_GROUP : 1 };
};

/* ---------- the address: #/team?by=repo&person=<key>&repo=<owner/name>&state=<state> ---------- */

function tvHash(q) {
  const query = {};
  if (q.by === 'repo') query.by = 'repo';
  if (q.person) query.person = q.person;
  if (q.repo) query.repo = q.repo;
  if (q.state) query.state = q.state;
  try { return buildHash('team', {}, query); } catch (_) { return '#/team'; }
}

function tvQuery(route) {
  const q = (route && route.query) || {};
  return { by: q.by === 'repo' ? 'repo' : 'person', person: typeof q.person === 'string' && TV_KEY_RE.test(q.person) ? q.person : '', repo: teamSlug(q.repo), state: ownKey(TV_STATES, q.state) ? q.state : '' };
}

/* ---------- the pieces of a row ---------- */

/* A link to GitHub as plain text with an underline (team.js teamLink builds it after its checks; its bordered button class is replaced), or null. */
function tvLink(url, label, aria, cls) {
  const a = teamLink(url, label, aria);
  if (a) a.setAttribute('class', cls || 'tv-a');
  return a;
}

/* Element.append turns a null into the text "null": every optional child goes through here. */
function tvAdd(node, ...kids) { node.append(...kids.filter((k) => k !== null && k !== undefined && k !== false)); return node; }

function tvCell(key, label, ...kids) {
  const k = kids.filter((x) => x !== null && x !== undefined && x !== false && x !== '');
  if (!k.length) return null;
  return el('span', { class: 'tv-c', 'data-c': key }, el('span', { class: 'tv-cl dim', text: label }), ' ', ...k);
}

function tvStateChip(state) {
  const [g, w, help] = TV_STATES[state];
  return el('span', { class: 'tv-state', 'data-state': state, title: help }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: g }), ` ${w}`);
}

/* One piece of work: the state in a glyph and words, what it is (issue, pull request or branch) with its title, then the small facts, each with its label. */
function tvRowNode(v, by) {
  const issue = v.issue ? tvLink(v.issueUrl, `#${v.issue}`, `Open issue ${v.issue} on GitHub`, 'tv-a tv-ref mono') : null;
  const what = [];
  if (v.kind === 'branch') {
    what.push(v.branch ? (tvLink(v.branchUrl, v.branch, `Open branch ${v.branch} on GitHub`, 'tv-a tv-title') || el('span', { class: 'tv-title', text: v.branch })) : el('span', { class: 'tv-title', text: 'A branch' }));
  } else if (v.kind === 'pr') {
    const pr = v.pr ? tvLink(v.prUrl, `PR #${v.pr}`, `Open pull request ${v.pr} on GitHub`, 'tv-a tv-ref mono') : null;
    what.push(pr || el('span', { class: 'tv-ref mono', text: 'A pull request' }), v.title ? el('span', { class: 'tv-title', text: v.title }) : null);
  } else if (v.kind === 'seen') {
    what.push(el('span', { class: 'tv-title dim', text: 'Seen here, with no issue, branch or pull request' }));
  } else if (v.issue) {
    what.push(issue || el('span', { class: 'tv-ref mono', text: `#${v.issue}` }), v.title ? el('span', { class: 'tv-title', text: v.title }) : null);
  } else {
    what.push(el('span', { class: 'tv-title dim', text: 'A live status with no issue named' }));
  }
  const branch = v.kind !== 'branch' && v.branch ? (tvLink(v.branchUrl, v.branch, `Open branch ${v.branch} on GitHub`, 'tv-a') || v.branch) : null;
  const pr = v.kind !== 'pr' && v.pr ? (tvLink(v.prUrl, `#${v.pr}`, `Open pull request ${v.pr} on GitHub`, 'tv-a mono') || `#${v.pr}`) : null;
  const where = by === 'person' ? tvCell('repo', 'Repo', tvLink(`https://github.com/${v.slug}`, v.slug, `Open ${v.slug} on GitHub`, 'tv-a') || v.slug)
    : tvCell('person', 'Person', v.you ? `${teamWho({ login: v.login })} (you)` : teamWho({ login: v.login }));
  const since = v.since ? teamClock(v.since) : '';
  const lastAgo = v.last ? teamAgo(v.last) : '';
  const seen = v.seen !== null && v.seen !== undefined ? teamSpan(v.seen) : '';
  const read = v.readAt ? teamAgo(v.readAt) : '';
  const cells = [
    where,
    tvCell('branch', 'Branch', branch),
    tvCell('pr', 'Pull request', pr, pr && v.draft ? ' draft' : ''),
    tvCell('agent', 'Agent', v.agent ? el('span', { class: 'tv-ag', 'data-agent': v.agent }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: TEAM_AGENT[v.agent] }), ` ${v.agent}`) : null),
    tvCell('since', 'Since', since),
    v.word ? tvCell('seen', 'Last seen', seen || 'unknown') : null,
    v.word && lastAgo ? tvCell('active', 'Last active', lastAgo) : (!v.word && lastAgo ? tvCell('active', 'Last change', lastAgo) : null),
    tvCell('claim', 'Claim', v.claim ? (v.claim.live ? 'live claim' : 'claimed') : null),
    tvCell('read', 'Read', read ? (v.repoStale ? `${read}, old` : read) : 'not yet'),
  ];
  return el('li', { class: 'tv-row', 'data-state': v.state, 'data-kind': v.kind, 'data-person': v.key, 'data-repo': v.slug },
    el('div', { class: 'tv-main' }, ...teamSpaced([tvStateChip(v.state), ...what])),
    el('div', { class: 'tv-meta' }, ...teamSpaced([...cells, v.collides ? el('span', { class: 'tv-c tm-collide', 'data-c': 'collides' }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: '!' }), ' Collides with one of your tasks') : null])));
}

/* ---------- groups ---------- */

function tvPersonHead(g, info) {
  const word = g.word;
  const live = word && ownKey(TEAM_LIVE, word) ? TEAM_LIVE[word] : null;
  const repos = info && Array.isArray(info.repos) ? info.repos.length : new Set(g.rows.map((r) => r.slug)).size;
  return el('div', { class: 'tv-gh' }, ...teamSpaced([
    el('h3', { class: 'tv-gname', text: teamWho({ login: g.login }) }),
    g.you ? el('span', { class: 'tv-you dim', text: 'you' }) : null,
    live ? el('span', { class: 'tm-lv tv-live', 'data-live': word }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: live[0] }), ` ${live[1]}`) : el('span', { class: 'tv-live dim', text: 'no live status' }),
    el('span', { class: 'tv-gsum dim', text: `${teamPlural(repos, 'repo')} · ${teamPlural(g.rows.length, 'row')}` })]));
}

function tvRepoHead(b, slug, n) {
  const link = b ? teamLink(b.url, 'GitHub', `Open ${slug} on GitHub`) : null;
  const age = b && b.read_at ? `read ${teamAgo(b.read_at) || 'just now'}` : 'not read yet';
  return el('div', { class: 'tv-gh' }, ...teamSpaced([
    el('h3', { class: 'tv-gname mono', text: slug }),
    b ? teamStateChip(b) : null,
    el('span', { class: 'tv-gsum dim', text: `${age} · ${teamPlural(n, 'row')}` }),
    link]));
}

const TV_RANK = { live: 0, idle: 1, unconfirmed: 2, stale: 3, unknown: 4 };

/* The groups of the filtered rows: [{head, rows}] by person (in the board's order of people) or by repository (in the order of the shared repositories). */
function tvGroups(rows, by, o, filtered) {
  if (by === 'repo') {
    const out = [];
    for (const b of tvSharedBlocks()) {
      const mine = rows.filter((r) => r.slug === b.slug);
      if (mine.length || !filtered) out.push({ head: tvRepoHead(b, b.slug, mine.length), rows: mine, empty: b });
    }
    return out;
  }
  const info = new Map((o.people || []).filter((p) => p && typeof p.key === 'string').map((p) => [p.key, p]));
  const order = (o.people || []).map((p) => p && p.key);
  const map = new Map();
  for (const r of rows) {
    if (!map.has(r.key)) map.set(r.key, { key: r.key, login: r.login, you: r.you, word: null, rows: [] });
    const g = map.get(r.key);
    g.rows.push(r);
    if (r.word && (g.word === null || (TV_RANK[r.word] ?? 9) < (TV_RANK[g.word] ?? 9))) g.word = r.word;
  }
  return [...map.values()].sort((a, c) => order.indexOf(a.key) - order.indexOf(c.key)).map((g) => ({ head: tvPersonHead(g, info.get(g.key)), rows: g.rows }));
}

/* ---------- free issues ---------- */

function tvFreeNode(b) {
  const f = b.free && typeof b.free === 'object' ? b.free : null;
  if (!f) return null;
  const age = b.read_at ? `read ${teamAgo(b.read_at) || 'just now'}` : 'not read yet';
  const sec = el('section', { class: 'tv-free', 'data-repo': b.slug, 'data-state': f.state, 'aria-label': `Free issues in ${b.slug}` },
    el('div', { class: 'tv-gh' }, el('h3', { class: 'tv-gname mono', text: b.slug }), el('span', { class: 'tv-gsum dim', text: age })));
  const held = (Array.isArray(f.held) ? f.held : []).filter((n) => teamNum(n)).slice(0, 200);
  const heldNode = () => {
    if (!held.length) return null;
    const shown = held.slice(0, TV_HELD_MAX);
    const kids = [];
    shown.forEach((n, i) => { if (i) kids.push(', '); kids.push(teamLink(`${b.url}/issues/${n}`, `#${n}`, `Open issue ${n} on GitHub`) || `#${n}`); });
    for (const k of kids) if (k && typeof k === 'object' && k.setAttribute) k.setAttribute('class', 'tv-a mono');
    return el('p', { class: 'tv-held dim' }, `${f.state === 'unknown' ? 'Known to be held' : 'Held by somebody'}: `, ...kids, held.length > shown.length ? ` and ${held.length - shown.length} more` : '');
  };
  if (f.state === 'ok') {
    const rows = (Array.isArray(f.issues) ? f.issues : []).filter((i) => i && teamNum(i.number)).slice(0, 50);
    if (!rows.length) {
      tvAdd(sec, el('p', { class: 'tv-fnote', text: 'No free issues: every open issue is held by somebody or has an assignee.' }), heldNode());
    } else {
      const count = Number.isInteger(f.count) && f.count >= rows.length ? f.count : rows.length;
      const note = count > rows.length ? `${rows.length} of ${count} free issues, newest first.`
        : f.truncated === true ? `${teamPlural(rows.length, 'free issue')}, newest first. There may be more: the board reads only the first 100 open issues that have no assignee.`
          : `${teamPlural(rows.length, 'free issue')}, newest first.`;
      tvAdd(sec, el('p', { class: 'tv-fnote', text: note }),
        el('ul', { class: 'tv-issues' }, rows.map((i) => {
          const n = teamNum(i.number);
          const link = tvLink(`${b.url}/issues/${n}`, `#${n}`, `Open issue ${n} on GitHub`, 'tv-a tv-ref mono');
          const labels = (Array.isArray(i.labels) ? i.labels : []).map((x) => teamText(x, 30)).filter(Boolean).slice(0, 5);
          const ago = typeof i.updated_at === 'string' ? teamAgo(i.updated_at) : '';
          return el('li', { class: 'tv-issue', 'data-n': String(n) }, ...teamSpaced([link || el('span', { class: 'tv-ref mono', text: `#${n}` }), el('span', { class: 'tv-title', text: teamText(i.title, 200) || '(no title)' }),
            labels.length ? el('span', { class: 'tv-labels dim', text: labels.join(', ') }) : null, ago ? el('span', { class: 'tv-when dim', text: `updated ${ago}` }) : null]));
        })),
        heldNode());
    }
  } else {
    const reason = typeof f.reason === 'string' ? f.reason : '';
    const next = f.state === 'unread' ? 'Open the issue list in the launcher to pick one: a taken issue is marked there.' : (ownKey(TEAM_NEXT, reason) ? TEAM_NEXT[reason] : 'Nothing for you to do: the board reads again by itself.');
    tvAdd(sec, el('p', { class: 'tv-fnote', text: teamText(f.message, 300) || 'Free issues are not known for this repository.' }), heldNode(), el('p', { class: 'tv-next dim', text: `Next: ${next}` }));
  }
  return sec;
}

/* ---------- the page ---------- */

function tvSelect(label, onchange) {
  const sel = el('select', { 'aria-label': label, onchange: () => onchange(sel.value) });
  return { sel, node: el('label', { class: 'tv-f' }, el('span', { class: 'tv-fl dim', text: label }), sel) };
}

function tvOptions(sel, items, value) {
  const sig = JSON.stringify(items);
  if (sel.getAttribute('data-sig') !== sig) {
    sel.setAttribute('data-sig', sig);
    sel.textContent = '';
    for (const [v, t] of items) sel.append(el('option', { value: v, text: t }));
  }
  sel.value = items.some(([v]) => v === value) ? value : '';
}

function tvSegment(P) {
  const items = [['person', 'Person'], ['repo', 'Repo']];
  const node = el('div', { class: 'seg-ctl tv-by', role: 'group', 'aria-label': 'Group by' }, el('span', { class: 'seg-k dim', text: 'group by' }));
  P.byBtns = new Map();
  for (const [v, t] of items) {
    const b = el('button', { class: 'seg-btn', type: 'button', 'data-by': v, 'aria-pressed': v === P.q.by ? 'true' : 'false', text: t, onclick: () => tvSet(P, { by: v }), onkeydown: (e) => {
      const i = items.findIndex(([x]) => x === P.q.by);
      const to = e.key === 'ArrowRight' ? items[(i + 1) % items.length][0] : e.key === 'ArrowLeft' ? items[(i + items.length - 1) % items.length][0] : null;
      if (to === null) return;
      e.preventDefault();
      tvSet(P, { by: to });
      P.byBtns.get(to).focus();
    } });
    P.byBtns.set(v, b);
    node.append(b);
  }
  return node;
}

/* A filter or the grouping changed: the address carries it (so a link or a reload shows the same screen), and the page repaints from it. */
function tvSet(P, patch) {
  const q = { ...P.q, ...patch };
  const h = tvHash(q);
  if (typeof navigate === 'function') navigate(h, { replace: true });
  else { P.q = tvQuery({ query: Object.fromEntries(new URLSearchParams(h.split('?')[1] || '')) }); tvPaint(P); }
}

function tvEmptyNode(title, hint, action) {
  const n = pageEmpty('people', title, hint);
  if (action) n.append(action);
  return n;
}

function tvPaint(P) {
  if (TeamView.page !== P || !P.root) return;
  const shared = teamShared();
  if (shared && !P.stop) P.stop = Team.watch(() => tvPaint(P));
  if (!shared && P.stop) { P.stop(); P.stop = null; }
  P.root.classList.toggle('tv-none', !shared);
  if (!shared) {
    if (P.sig === 'none') return;
    P.sig = 'none';
    P.body.textContent = '';
    P.body.append(tvEmptyNode('No repository is shared with your team', 'Share a repository in Settings > Team and the board starts to read who is working in it. Until then nothing is read from GitHub for the team.',
      el('a', { class: 'btn primary tv-go', href: teamSettingsHref(), text: 'Open Team settings' })));
    return;
  }
  const o = tvData();
  const sig = JSON.stringify([o, teamStore.err, teamBlocks().map((b) => [b.slug, b.state, b.stale, b.read_at, b.message, b.free]), P.q, Math.floor(Date.now() / 60000)]);
  if (sig === P.sig) return;
  P.sig = sig;
  P.body.textContent = '';
  if (!o) {
    if (teamStore.err) P.body.append(el('div', { class: 'tm-note bad', role: 'status' }, el('b', { class: 'tm-note-t', text: 'The board did not answer' }), el('span', { class: 'tm-note-x', text: teamStore.err }),
      el('span', { class: 'tm-note-n dim', text: 'Next: nothing for you to do, the page asks again every few seconds.' })));
    else P.body.append(typeof Pages !== 'undefined' && Pages && typeof Pages.skeleton === 'function' ? Pages.skeleton(3) : el('p', { class: 'dim', text: 'Loading…' }));
    return;
  }
  const rows = tvRows(o);
  const people = (o.people || []).filter((p) => p && typeof p.key === 'string' && TV_KEY_RE.test(p.key));
  const slugs = tvSharedBlocks().map((b) => b.slug);
  tvOptions(P.person.sel, [['', 'Everyone'], ...people.map((p) => [p.key, `${teamWho({ login: p.login })}${p.you === true ? ' (you)' : ''}`])], P.q.person);
  tvOptions(P.repo.sel, [['', 'All repos'], ...slugs.map((s) => [s, s])], P.q.repo);
  tvOptions(P.state.sel, [['', 'Any state'], ...Object.keys(TV_STATES).map((s) => [s, `${TV_STATES[s][0]} ${TV_STATES[s][1]}`])], P.q.state);
  for (const [v, b] of P.byBtns) b.setAttribute('aria-pressed', v === P.q.by ? 'true' : 'false');
  const keep = (r) => (!P.q.person || r.key === P.q.person) && (!P.q.repo || r.slug === P.q.repo) && (!P.q.state || r.state === P.q.state);
  const shown = rows.filter(keep);
  const filtered = !!(P.q.person || P.q.repo || P.q.state);
  P.clear.classList.toggle('hidden', !filtered);
  setTextIfChanged(P.count, `${teamPlural(shown.length, 'row')}${filtered ? ` of ${rows.length}` : ''} from ${teamPlural(new Set(shown.map((r) => r.key)).size, 'person', 'people')} in ${teamPlural(new Set(shown.map((r) => r.slug)).size, 'repo')}`);

  // what is wrong with a repository comes first, each with its one next step (team.js teamNotice)
  const notes = el('div', { class: 'tv-notes' });
  if (teamStore.err) notes.append(el('div', { class: 'tm-note warn', role: 'status' }, el('b', { class: 'tm-note-t', text: 'The board did not answer just now' }), el('span', { class: 'tm-note-x', text: `${teamStore.err}. Showing the last read.` })));
  for (const b of tvSharedBlocks()) {
    const n = teamNotice(b);
    if (n && (n.kind === 'bad' || n.kind === 'warn' || b.state === 'loading')) notes.append(el('div', { class: 'tv-repo-note' }, el('span', { class: 'tv-rn mono', text: b.slug }), ' ', teamNoticeNode(n)));
  }
  P.body.append(notes);

  const work = el('section', { class: 'tv-work', 'aria-label': 'Who is working on what' }, el('h2', { class: 'tv-h', text: 'Who is working on what' }));
  const groups = tvGroups(shown, P.q.by, o, filtered);
  if (!groups.length) {
    const teammates = tvCount(o, 'teammates');
    const unsure = tvCount(o, 'problem') > 0 || tvCount(o, 'loading') > 0 || tvSharedBlocks().some((b) => b.stale === true);      // an old or failed read is never "nobody"
    work.append(filtered
      ? tvEmptyNode('No rows match these filters', `${teamPlural(rows.length, 'row')} in all.`, el('button', { type: 'button', class: 'tv-go', onclick: () => tvSet(P, { person: '', repo: '', state: '' }), text: 'Clear filters' }))
      : unsure ? tvEmptyNode('Nothing to show until GitHub can be read', 'The notes above say what is wrong and what to do. This page shows rows again when the board has read the repository.')
        : tvEmptyNode(teammates === 0 ? 'Nobody else shows up in your shared repos' : 'Nothing to show yet', teammates === 0
          ? 'Nobody else has an issue assigned, a branch linked or pushed lately, an open pull request or a live status here. Assign the issue to them on GitHub and they show up.' : 'The board has not read any work yet.'));
  }
  for (const g of groups) {
    const sec = el('section', { class: 'tv-group', 'aria-label': g.head.querySelector('.tv-gname') ? g.head.querySelector('.tv-gname').textContent : 'Group' }, g.head);
    if (g.rows.length) sec.append(el('ul', { class: 'tv-rows' }, g.rows.map((r) => tvRowNode(r, P.q.by))));
    else sec.append(el('p', { class: 'tv-fnote dim', text: 'No work found here.' }));
    work.append(sec);
  }
  P.body.append(work);

  const free = el('section', { class: 'tv-freewrap', 'aria-label': 'Free issues' }, el('h2', { class: 'tv-h', text: 'Free issues' }),
    el('p', { class: 'tv-lede dim', text: 'Open issues nobody holds: no assignee, no live status, no linked branch, no claim and no pull request that closes it. When the board cannot tell, it says so instead of listing. One limit: an unassigned issue with a linked branch and no status line can show as free.' }));
  const sections = tvSharedBlocks().filter((b) => !P.q.repo || b.slug === P.q.repo).map(tvFreeNode).filter(Boolean);
  if (sections.length) free.append(...sections); else free.append(el('p', { class: 'tv-fnote dim', text: 'No shared repository to list.' }));
  P.body.append(free);
  P.body.append(el('p', { class: 'tv-foot dim', text: "Read only. Nothing here changes a teammate's work or yours. Every row says how old its read is." }));
}

function tvMount(root, route) {
  const P = { root: null, q: tvQuery(route), stop: null, sig: '' };
  P.person = tvSelect('Person', (v) => tvSet(P, { person: v }));
  P.repo = tvSelect('Repo', (v) => tvSet(P, { repo: v }));
  P.state = tvSelect('State', (v) => tvSet(P, { state: v }));
  P.clear = el('button', { class: 'tv-clear hidden', type: 'button', text: 'Clear filters', onclick: () => tvSet(P, { person: '', repo: '', state: '' }) });
  P.count = el('span', { class: 'tv-count dim', role: 'status', 'aria-live': 'polite' });
  P.body = el('div', { class: 'tv-body' });
  P.filters = el('section', { class: 'tv-filters', 'aria-label': 'Filters' }, P.person.node, P.repo.node, P.state.node, P.clear, P.count);
  P.root = el('div', { class: 'tv' },
    el('div', { class: 'page-head' }, el('h1', { text: 'Team' }), el('div', { class: 'actions' }, tvSegment(P))),
    el('p', { class: 'tv-lede dim', text: 'Who is working on what in the repos you share with your team, read from GitHub through this board.' }),
    P.filters, P.body);
  root.append(P.root);
  TeamView.page = P;
  tvPaint(P);
}

function tvUnmount() {
  const P = TeamView.page;
  if (!P) return;
  if (P.stop) P.stop();
  TeamView.page = null;
}

registerPage('team', {
  title: () => 'Team',
  mount: tvMount,
  update() { if (TeamView.page) tvPaint(TeamView.page); },
  onRoute(route) { const P = TeamView.page; if (!P) return; P.q = tvQuery(route); P.sig = ''; tvPaint(P); },
  unmount: tvUnmount,
});

/* ---------- the shell's watch: the sidebar group and the Home line follow the board's team view while some repository is shared ---------- */

function tvRepaint() {
  const sig = JSON.stringify([tvData(), teamStore.err, Math.floor(Date.now() / 60000)]);
  if (sig === TeamView.last) return;
  TeamView.last = sig;
  try { if (typeof Shell !== 'undefined' && Shell && typeof Shell.patchTrees === 'function') Shell.patchTrees(); } catch (e) { console.error('ccboard team sidebar', e); }
  try { if (typeof homePage !== 'undefined' && homePage && homePage.refs) TeamView.homePaint(homePage.refs); } catch (e) { console.error('ccboard team home', e); }
  try { if (typeof Palette !== 'undefined' && Palette && Palette.ui && typeof Palette.refresh === 'function') Palette.refresh(); } catch (e) { console.error('ccboard team palette', e); }
}

/* Starts the watch (once) when the board shares a repository; core.js teamSync() calls it with every state. It asks the board for what it already holds, never GitHub. */
TeamView.start = function () {
  if (TeamView.stopWatch) return;
  TeamView.stopWatch = Team.watch(tvRepaint);
};

TeamView.stop = function () {
  if (!TeamView.stopWatch) return;
  TeamView.stopWatch();
  TeamView.stopWatch = null;
  TeamView.last = '';
  try { if (typeof Shell !== 'undefined' && Shell && typeof Shell.patchTrees === 'function') Shell.patchTrees(); } catch (_) { /* no shell */ }
};

TeamView.data = tvData;
TeamView.rows = tvRows;
TeamView.live = tvLive;
TeamView.homeMessage = tvHomeMessage;
TeamView.groups = tvGroups;
TeamView.freeNode = tvFreeNode;
