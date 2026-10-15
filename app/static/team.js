/* ccboard Team (issue #197, Team P1), the lazy bundle 'team' (lazy.js): who is working on what in the repositories the owner shared with the team, read from GitHub by the box's
   own gh and shown read only. Three surfaces use it: the project page's Team tab (pages/project.js pjTeamView), Settings > Team (pages/settings.js settingsTeam) and the demo
   (demoTeam, called by core.js demoApi for /api/team/...). Nothing here is loaded, fetched or polled until one of them asks.

   The data is GET /api/team/state: {api, enabled, me:{login,id}|null, at, repos[{repo_slug, slug, url, local[{project,repo}], shared, state, message, wait_s, stale, read_at,
   age_s, gql_at, graph_pending, graph_failed, private, people[...], counts, truncated}]}. One block per repository of this board's own projects; only a shared one is ever read. The state is
   a fixed word (off, loading, ok, sso, no_access, not_found, rate_limited, low_budget, no_login, no_gh, error) and each has its own words and its one next step: an access problem,
   a rate limit or an old read is never drawn as "no teammates". The switch is PUT /api/team/repos/<owner>/<repo> {shared} (off by default). The block itself has no control:
   its only actions are links to GitHub, built here from the page's own checks (https://github.com/<owner>/<repo>/(issues|pull)/<n> or /tree/<branch>), never copied blindly.

   GitHub text (titles, branch names, logins, labels) is untrusted: it is cut, stripped of control, bidi and zero-width characters and set with textContent only, never as markup.
   The age of the data is worked out here from read_at (the server leaves age_s out of its ETag). A define-only script: nothing runs until a surface calls in. */
'use strict';

const TEAM_POLL_MS = 15000;                                      // the page asks the board (a cheap, ETag-checked read of what the board already holds); the board reads GitHub on its own slower schedule
const TEAM_TEXT_MAX = 200;
const TEAM_STALE_S = 900;                                        // the board's own mark for an old read (app/team.py STALE_S)
const TEAM_STATES = {                                            // state -> [glyph, words]; state is never colour alone
  off: ['○', 'Not shared'], loading: ['∙', 'Reading GitHub'], ok: ['✓', 'Read'], sso: ['!', 'Authorise the token'], no_access: ['✕', 'No access'], not_found: ['✕', 'Not found'],
  rate_limited: ['!', 'Waiting for GitHub'], low_budget: ['!', 'Few requests left'], no_login: ['✕', 'gh is not logged in'], no_gh: ['✕', 'gh is missing'], error: ['!', 'Could not read'],
};
const TEAM_NEXT = {                                              // the one next step of each state that is not fine
  loading: 'The first read takes a few seconds. Nothing for you to do.',
  sso: 'On GitHub open Settings, Developer settings, your token, choose Configure SSO and authorise the organization. The board tries again by itself every 10 minutes.',
  no_access: 'Ask the owner of the repository for access, or log gh in on the box as a login that can read it. The board tries again by itself every 10 minutes.',
  not_found: 'Check that the repository still has this name, and that this login can see it (a private repository needs a token with access to it). The board tries again by itself every 10 minutes.',
  rate_limited: 'Nothing for you to do: reads start again by themselves when GitHub allows.',
  low_budget: 'Nothing for you to do: team reads wait until the request limit of this GitHub login resets.',
  no_login: 'Log gh in on the box (gh auth login). Settings > Team shows when it works.',
  no_gh: 'Install the GitHub CLI on the box and log it in. Settings > Team shows when it works.',
  error: 'Nothing for you to do: the board tries again by itself.',
};
const TEAM_SLUG_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/(?![-.])[A-Za-z0-9_.-]{1,100}$/;
const TEAM_LOGIN_RE = /^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$/;
const TEAM_URL_RE = /^https:\/\/github\.com\/[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\/(?![-.])[A-Za-z0-9_.-]{1,100}(?:\/(?:issues|pull)\/[1-9][0-9]{0,8}|\/tree\/[A-Za-z0-9._\/%-]{1,300})?$/;
const TEAM_CTRL_RE = /[\u0000-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]/g;
const teamStore = { data: null, at: 0, err: '', seq: 0, subs: new Set(), timer: null, saving: {}, errors: {}, soon: [] };

/* ---------- untrusted text and links ---------- */

/* Plain text from GitHub: cut before any pattern runs, control, bidi and zero-width characters dropped, white space collapsed, at most n characters. */
function teamText(v, n) {
  const max = Number.isInteger(n) && n > 0 ? n : TEAM_TEXT_MAX;
  if (typeof v !== 'string' && typeof v !== 'number') return '';
  const t = String(v).slice(0, max * 4).replace(TEAM_CTRL_RE, ' ').replace(/\s+/g, ' ').trim();
  return t.length > max ? t.slice(0, max - 1) + '…' : t;
}

function teamNum(v) { return Number.isInteger(v) && v > 0 && v < 1e9 ? v : null; }
function teamLogin(v) { const t = teamText(v, 60); return TEAM_LOGIN_RE.test(t) ? t : ''; }
function teamSlug(v) { const t = typeof v === 'string' ? v : ''; return TEAM_SLUG_RE.test(t) ? t : ''; }

/* A link to GitHub or nothing. Only https://github.com/<owner>/<repo>[/issues/<n> | /pull/<n> | /tree/<branch>] passes; anything else (a javascript: address, another host, a
   path with ..) is not an anchor at all. The label says where it goes; the target opens in a new tab without the opener. */
function teamLink(url, label, aria) {
  const u = typeof url === 'string' && url.length < 600 && TEAM_URL_RE.test(url) && !url.includes('..') ? url : null;
  if (!u) return null;
  return el('a', { class: 'tm-open', href: u, target: '_blank', rel: 'noopener noreferrer', 'aria-label': aria || label, title: aria || label, text: label });
}

function teamPlural(n, one, many) { return `${n} ${n === 1 ? one : (many || one + 's')}`; }

/* "just now", "12 min ago", "3 h ago", "2 d ago" from an ISO time (or ''). */
function teamAgo(iso) {
  const t = typeof iso === 'string' ? Date.parse(iso) : NaN;
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function teamState(b) { return b && typeof b.state === 'string' && ownKey(TEAM_STATES, b.state) ? b.state : 'error'; }

function teamSettingsHref() {
  try { return buildHash('settings', {}, { sec: 'team' }); } catch (_) { return '#/settings?sec=team'; }
}

/* ---------- the data: one read of the board, kept here and shared by the surfaces that are open ---------- */

function teamBlocks() {
  const d = teamStore.data;
  return d && Array.isArray(d.repos) ? d.repos.filter((b) => b && typeof b === 'object' && typeof b.repo_slug === 'string' && teamSlug(b.slug)) : [];
}

function teamMe() {
  const m = teamStore.data && teamStore.data.me;
  const login = m && typeof m === 'object' ? teamLogin(m.login) : '';
  return login ? { login, id: teamNum(m.id) } : null;
}

/* What a surface that repaints on a change compares: the blocks, the errors and the saves under way, and the minute (ages move on). */
function teamSig() {
  return JSON.stringify([teamStore.data && teamStore.data.me, teamBlocks().map((b) => [b.repo_slug, b.shared, b.state, b.message, b.stale, b.read_at, b.gql_at, b.counts, b.people, b.truncated, b.graph_pending, b.graph_failed]),
    teamStore.err, teamStore.errors, teamStore.saving, Math.floor(Date.now() / 60000)]);
}

function teamNotify() { for (const fn of [...teamStore.subs]) { try { fn(); } catch (e) { console.error('ccboard team', e); } } }

function teamAdopt(body) {
  if (!body || typeof body !== 'object' || !Array.isArray(body.repos)) { teamStore.err = 'the board answered without a team view'; return false; }
  teamStore.data = body;
  teamStore.at = Date.now();
  teamStore.err = '';
  return true;
}

/* GET /api/team/state. A newer ask makes an older answer stale (a switch counts as one); a failed ask keeps the last good data on screen and says so. */
async function teamLoad() {
  const mine = ++teamStore.seq;
  let body = null;
  try { body = await api('GET', '/api/team/state'); } catch (e) {
    if (mine !== teamStore.seq) return false;
    teamStore.err = (e && e.message) || 'the board did not answer';
    teamNotify();
    return false;
  }
  if (mine !== teamStore.seq) return false;
  const ok = teamAdopt(body);
  teamNotify();
  return ok;
}

function teamSoon(delays) {
  for (const ms of delays) {
    const t = setTimeout(() => { if (teamStore.subs.size) teamLoad(); }, ms);
    if (t && typeof t.unref === 'function') t.unref();
    teamStore.soon.push(t);
  }
}

function teamSchedule() {
  if (teamStore.timer || !teamStore.subs.size) return;
  const t = setTimeout(async () => {
    teamStore.timer = null;
    if (!teamStore.subs.size) return;
    if (!(typeof document !== 'undefined' && document.hidden)) await teamLoad();
    teamSchedule();
  }, TEAM_POLL_MS);
  if (t && typeof t.unref === 'function') t.unref();
  teamStore.timer = t;
}

/* Watch the data while a surface is on screen: fn runs after every change; the board is asked now (when the last answer is old) and then every TEAM_POLL_MS. Returns the stop function. */
function teamWatch(fn) {
  teamStore.subs.add(fn);
  teamSchedule();
  if (!teamStore.data || Date.now() - teamStore.at > 5000) teamLoad();
  let live = true;
  return () => {
    if (!live) return;
    live = false;
    teamStore.subs.delete(fn);
    if (!teamStore.subs.size) {
      clearTimeout(teamStore.timer);
      teamStore.timer = null;
      for (const t of teamStore.soon.splice(0)) clearTimeout(t);
    }
  };
}

/* The switch. Optimistic: the block shows its new state at once; the board's answer replaces it, a failed PUT puts it back and says why beside the switch. */
async function teamShare(block, on) {
  const key = block && block.repo_slug;
  const slug = teamSlug(block && block.slug);
  if (!key || !slug || typeof on !== 'boolean' || teamStore.saving[key]) return false;
  const [owner, repo] = slug.split('/');
  const before = teamBlocks().find((x) => x.repo_slug === key) || block;
  const swap = (nb) => {
    const d = teamStore.data;
    const repos = d.repos.map((b) => (b.repo_slug === key ? nb : b));
    teamStore.data = { ...d, repos, enabled: repos.some((b) => b.shared === true) };
  };
  teamStore.saving[key] = true;
  delete teamStore.errors[key];
  teamStore.seq += 1;                                              // a read that was on its way would bring the old switch back
  swap({ ...before, shared: on, state: on ? 'loading' : 'off', message: on ? 'Reading GitHub for the first time.' : 'Not shared with your team. Nothing is read from GitHub for this repository.',
    stale: false, read_at: null, age_s: null, gql_at: null, people: [], counts: { people: 0, teammates: 0, issues: 0, branches: 0, prs: 0, unattributed_branches: 0 } });
  teamNotify();
  let ok = false;
  try {
    const nb = await api('PUT', `/api/team/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`, { shared: on });
    if (nb && typeof nb === 'object' && nb.repo_slug === key) { swap(nb); ok = true; }
    else { swap(before); teamStore.errors[key] = 'Not saved: the board answered without the repository.'; }
  } catch (e) {
    swap(before);
    teamStore.errors[key] = `Not saved: ${(e && e.message) || 'the board did not answer'}`;
  }
  delete teamStore.saving[key];
  teamNotify();
  if (ok && on) teamSoon([2000, 6000]);
  return ok;
}

/* POST /api/team/refresh: ask the board to read the shared repositories again as soon as GitHub allows (Settings > Team only; the Team block has no controls). */
async function teamRefresh() {
  let r = null;
  try { r = await api('POST', '/api/team/refresh'); } catch (e) {
    if (typeof pageToast === 'function') pageToast(`Could not ask for a new read: ${(e && e.message) || 'the board did not answer'}`, 'bad');
    return false;
  }
  teamSoon([2000, 6000]);
  if (typeof pageToast === 'function') pageToast(r && r.queued === false ? 'Nothing is shared, so nothing is read.' : 'Asked the board to read GitHub again.', 'info');
  return !!(r && r.queued !== false);
}

/* ---------- the pieces ---------- */

function teamStateChip(b) {
  const st = teamState(b);
  return el('span', { class: 'tm-state', 'data-state': st }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: TEAM_STATES[st][0] }), ` ${TEAM_STATES[st][1]}`);
}

/* The notice above the people, with the one next step: null when there is nothing to say (a fresh read with teammates). */
function teamNotice(b) {
  const st = teamState(b);
  const ago = teamAgo(b.read_at);
  const msg = teamText(b.message, 300);
  if (st === 'off') return null;
  if (st === 'loading') return { kind: 'dim', title: 'Reading GitHub for the first time', text: msg, next: TEAM_NEXT.loading };
  if (st !== 'ok') {
    const kept = ago && Array.isArray(b.people) && b.people.length ? `Showing the last good read, ${ago}.` : '';
    const title = st === 'sso' ? 'Authorise this token for the organization' : st === 'no_access' ? 'This login cannot read this repository' : st === 'not_found' ? 'GitHub does not show this repository'
      : st === 'rate_limited' ? 'GitHub asked the board to wait' : st === 'low_budget' ? 'Waiting for the request limit' : st === 'no_login' ? 'gh is not logged in' : st === 'no_gh' ? 'gh is not installed' : 'GitHub could not be read';
    return { kind: st === 'no_access' || st === 'not_found' || st === 'no_login' || st === 'no_gh' ? 'bad' : 'warn', title, text: msg, next: TEAM_NEXT[st] || TEAM_NEXT.error, kept };
  }
  if (b.stale === true) return { kind: 'warn', title: 'This may be out of date', text: `The last read was ${ago || 'a while ago'}; GitHub has not been read since, so these rows may have changed.`, next: 'The board reads again by itself. If it does not, Settings > Team says why.' };
  const teammates = (Array.isArray(b.people) ? b.people : []).filter((x) => x && typeof x === 'object' && x.you !== true).length;
  if (teammates === 0) {
    return { kind: 'dim', title: 'No teammates found here', text: 'Nobody else has an issue assigned, a branch linked to an issue or pushed in the last 14 days, or an open pull request in this repository.',
      next: 'Someone who works here without any of those on GitHub is not visible. Assign the issue to them on GitHub and they show up.' };
  }
  return null;
}

function teamNoticeNode(n) {
  return el('div', { class: `tm-note ${n.kind}`, role: n.kind === 'bad' || n.kind === 'warn' ? 'status' : null },
    el('b', { class: 'tm-note-t', text: n.title }),
    n.text ? el('span', { class: 'tm-note-x', text: n.text }) : null,
    n.kept ? el('span', { class: 'tm-note-x', text: n.kept }) : null,
    el('span', { class: 'tm-note-n dim' }, 'Next: ', n.next, ' ', el('a', { class: 'tm-set', href: teamSettingsHref(), text: 'Settings > Team' })));
}

/* One row of a person: a glyph and the kind in words, the number, the title as text, small facts, the age and the link to GitHub. */
function teamItem(kind, word, glyph, ref, title, facts, when, url, aria) {
  // the title (a branch: its name) is the link to GitHub, so a row carries no extra button; a URL that fails the gate leaves it plain text
  const name = title || ref;
  const link = name ? teamLink(url, name, aria) : null;
  if (link) link.classList.add('tm-tl', title ? 'tm-title' : 'mono');
  return el('li', { class: 'tm-item', 'data-kind': kind },
    el('span', { class: 'tm-g', 'aria-hidden': 'true', text: glyph }),
    el('span', { class: 'tm-kind', text: word }),
    title && ref ? el('span', { class: 'tm-ref mono', text: ref }) : null,
    link || (name ? el('span', { class: title ? 'tm-title' : 'tm-ref mono', text: name }) : null),
    facts.length ? el('span', { class: 'tm-facts dim', text: facts.join(' · ') }) : null,
    when ? el('span', { class: 'tm-when dim', text: when }) : null);
}

function teamPersonNode(p) {
  const login = teamLogin(p && p.login);
  const you = !!(p && p.you === true);
  const issues = Array.isArray(p && p.issues) ? p.issues : [];
  const prs = Array.isArray(p && p.prs) ? p.prs : [];
  const branches = Array.isArray(p && p.branches) ? p.branches : [];
  const list = el('ul', { class: 'tm-items' });
  for (const i of issues) {
    const n = teamNum(i && i.number);
    if (n === null) continue;
    const facts = [];
    const labels = (Array.isArray(i.labels) ? i.labels : []).map((x) => teamText(x, 40)).filter(Boolean).slice(0, 5);
    if (labels.length) facts.push(labels.join(', '));
    const c = i.claim && typeof i.claim === 'object' ? i.claim : null;
    if (c) facts.push(`claimed${c.live === true ? ', live' : c.live === false ? ', not live' : ''}${teamText(c.branch, 100) ? ' on ' + teamText(c.branch, 100) : ''}`);
    list.append(teamItem('issue', 'Assigned', '●', `#${n}`, teamText(i.title), facts, i.updated_at ? `updated ${teamAgo(i.updated_at)}` : '', i.url, `Open issue ${n} on GitHub`));
  }
  for (const r of prs) {
    const n = teamNum(r && r.number);
    if (n === null) continue;
    const draft = r.draft === true;
    const facts = [];
    if (teamText(r.head, 100)) facts.push(`branch ${teamText(r.head, 100)}`);
    const from = typeof r.from_fork === 'string' && teamSlug(r.from_fork) ? r.from_fork : '';
    if (from) facts.push(`from the fork ${from}`);
    const closes = (Array.isArray(r.closes) ? r.closes : []).map(teamNum).filter(Boolean).slice(0, 5);
    if (closes.length) facts.push(`closes ${closes.map((x) => '#' + x).join(', ')}`);
    list.append(teamItem(draft ? 'draft' : 'pr', draft ? 'Draft PR' : 'Open PR', draft ? '◌' : '◉', `#${n}`, teamText(r.title), facts, r.updated_at ? `updated ${teamAgo(r.updated_at)}` : '',
      r.url, `Open ${draft ? 'draft ' : ''}pull request ${n} on GitHub`));
  }
  for (const b of branches) {
    const name = teamText(b && b.name, 100);
    if (!name) continue;
    const linked = b.source === 'linked';
    const n = teamNum(b.issue);
    list.append(teamItem(linked ? 'linked' : 'pushed', linked ? 'Linked branch' : 'Pushed branch', linked ? '↳' : '↑', name, '', n ? [`linked to #${n}`] : [], b.at ? `${linked ? 'last commit' : 'pushed'} ${teamAgo(b.at)}` : '',
      b.url, `Open branch ${name} on GitHub`));
  }
  const parts = [];
  if (issues.length) parts.push(teamPlural(issues.length, 'issue'));
  if (branches.length) parts.push(teamPlural(branches.length, 'branch', 'branches'));
  if (prs.length) parts.push(teamPlural(prs.length, 'pull request'));
  const active = teamAgo(p && p.last_active);
  return el('li', { class: 'tm-person', 'data-login': login || null, 'data-you': you ? 'yes' : null },
    el('div', { class: 'tm-who' },
      el('b', { class: 'tm-login', text: login || 'unknown user' }),
      you ? el('span', { class: 'badge tm-you', title: 'This board acts as this GitHub login', text: 'you' }) : null,
      el('span', { class: 'tm-sum dim', text: parts.join(', ') || 'nothing open here' }),
      active ? el('span', { class: 'tm-act dim', text: `active ${active}` }) : null),
    list.firstChild ? list : null);
}

/* The read-only block of one shared repository: its state and the age of the data, a notice with the next step when there is something to say, the people, and what was left out.
   No button: the only actions are the links to GitHub. */
function teamBlockNode(b) {
  const st = teamState(b);
  const ago = teamAgo(b.read_at);
  const graph = teamAgo(b.gql_at);
  const people = st === 'loading' || st === 'off' ? [] : (Array.isArray(b.people) ? b.people : []);
  const note = teamNotice(b);
  const counts = b.counts && typeof b.counts === 'object' ? b.counts : {};
  const tr = b.truncated && typeof b.truncated === 'object' ? b.truncated : {};
  const status = el('div', { class: 'tm-status' }, teamStateChip(b),
    ago ? el('span', { class: 'tm-age dim', text: `Read ${ago}` }) : null,
    graph && st === 'ok' ? el('span', { class: 'tm-age dim', text: `Branch links read ${graph}` }) : null,
    b.private === true ? el('span', { class: 'tm-priv dim', text: 'private repository' }) : null);
  const ul = el('ul', { class: 'tm-people' + (st !== 'ok' || b.stale === true ? ' stale' : '') });
  for (const p of people) ul.append(teamPersonNode(p));
  const foot = [];
  if (b.graph_failed === true && st !== 'loading' && st !== 'off') foot.push('Branches linked to issues and the issues a pull request closes could not be read just now; the board will try again soon. The issues and pull requests above are from the latest read.');
  else if (st === 'ok' && b.graph_pending === true) foot.push('Branches linked to issues and the issues a pull request closes are read slowly (every 10 minutes); they are not here yet.');
  if (Number.isInteger(counts.unattributed_branches) && counts.unattributed_branches > 0) foot.push(`${teamPlural(counts.unattributed_branches, 'recent branch', 'recent branches')} with no GitHub author ${counts.unattributed_branches === 1 ? 'is' : 'are'} not listed.`);
  if (tr.issues === true || tr.pulls === true || tr.tips === true) foot.push(`The board read only the first part of the ${[tr.issues ? 'issues' : '', tr.pulls ? 'pull requests' : '', tr.tips ? 'branches' : ''].filter(Boolean).join(', ')} of this repository, so some rows may be missing.`);
  return el('div', { class: 'tm-block', 'data-state': st, 'data-repo': b.repo_slug },
    status, note ? teamNoticeNode(note) : null, ul.firstChild ? ul : null,
    foot.length ? el('div', { class: 'tm-foot dim' }, foot.map((t) => el('p', { text: t }))) : null);
}

/* The repo row: the switch, off by default, and the one sentence that says what turning it on does. Reads only; nothing is written to GitHub. */
function teamShareRow(b) {
  const key = b.repo_slug;
  const slug = teamSlug(b.slug);
  const me = teamMe();
  const box = el('input', { type: 'checkbox', role: 'switch', 'data-share': key, 'aria-label': `Shared with my team: ${slug}` });
  box.checked = b.shared === true;
  box.addEventListener('change', () => { teamShare(b, box.checked); });
  const err = teamStore.errors[key];
  return el('div', { class: 'tm-row', 'data-repo': key },
    el('label', { class: 'tm-switch' }, box,
      el('span', { class: 'tm-switch-t' }, el('b', { text: 'Shared with my team' }),
        el('span', { class: 'dim', text: b.shared === true ? `On. This board reads the issues, branches and pull requests of ${slug} from GitHub${me ? ' as ' + me.login : ''}. It never writes to GitHub.` : `Off. Turn on to see who else works in ${slug}: the board then reads its issues, branches and pull requests from GitHub, read only. Nothing is written to GitHub.` }))),
    err ? el('p', { class: 'tm-err bad', role: 'alert', text: err }) : null);
}

/* ---------- the project page's Team tab (pages/project.js pjTeamView) ---------- */

function teamBlocksFor(project, repo) {
  return teamBlocks().filter((b) => Array.isArray(b.local) && b.local.some((l) => l && l.project === project && (!repo || l.repo === repo)));
}

function teamLocalNames(b, project) {
  const names = (Array.isArray(b.local) ? b.local : []).filter((l) => l && l.project === project).map((l) => teamText(l.repo, 100)).filter(Boolean);
  return names.length ? `folder ${names.join(', ')}` : '';
}

/* opts: {project: () => name, repo: () => name or ''}. Returns {update(), destroy()}; it draws into host and repaints when the data or the route changes. */
function teamProjectSection(host, opts) {
  let sig = null;
  let dead = false;
  const paint = (force) => {
    if (dead) return;
    const project = opts.project();
    const repo = opts.repo();
    const blocks = teamBlocksFor(project, repo);
    const next = JSON.stringify([project, repo, teamSig(), blocks.length]);
    if (!force && next === sig) return;
    sig = next;
    const active = typeof document !== 'undefined' && document.activeElement && typeof document.activeElement.getAttribute === 'function' ? document.activeElement.getAttribute('data-share') : null;
    host.textContent = '';
    if (!teamStore.data) {
      host.append(teamStore.err ? el('p', { class: 'bad tm-load', role: 'alert', text: `The team view could not be read: ${teamStore.err}` }) : el('p', { class: 'dim tm-load', role: 'status', text: 'Reading the team settings…' }));
      return;
    }
    host.append(el('p', { class: 'tm-lede dim', text: 'Who is working on what in these repositories, read from GitHub. Read only: nothing is written to GitHub, and a teammate does not need ccboard. It is off until you switch a repository on.' }));
    if (teamStore.err) host.append(el('p', { class: 'bad tm-load', role: 'alert', text: `Could not refresh: ${teamStore.err}. Showing the last read.` }));
    if (!blocks.length) {
      host.append(pageEmpty('git-repo', repo ? 'No GitHub repository here' : 'No GitHub repository in this project', 'The team view reads GitHub. It needs a repo of this project with a github.com remote.'));
      return;
    }
    for (const b of blocks) {
      const slug = teamSlug(b.slug);
      const local = teamLocalNames(b, project);
      host.append(el('section', { class: 'tm-repo', 'aria-label': `Team: ${slug}`, 'data-repo': b.repo_slug },
        el('div', { class: 'tm-repohead' }, el('h2', { class: 'tm-h mono', text: slug }), local ? el('span', { class: 'dim tm-local', text: local }) : null,
          teamLink(b.url, 'Open on GitHub', `Open ${slug} on GitHub`)),
        teamShareRow(b), b.shared === true ? teamBlockNode(b) : null));
    }
    if (active) { const again = host.querySelector ? host.querySelector(`[data-share="${active}"]`) : null; if (again && typeof again.focus === 'function') again.focus(); }
  };
  const stop = teamWatch(() => paint(false));
  paint(true);
  return { update() { paint(false); }, destroy() { dead = true; stop(); host.textContent = ''; } };
}

/* ---------- Settings > Team (pages/settings.js settingsTeam) ---------- */

function teamKv(label, ...kids) {
  return el('div', { class: 'kv' }, el('b', { class: 'k', text: label }), el('div', { class: 'kv-main' }, kids), el('div', { class: 'kv-act hidden' }));
}

function teamHead(text) { return el('h2', { class: 'set-h', text }); }

function teamSettingsPanel(p) {
  p.textContent = '';
  const me = teamMe();
  const known = !!teamStore.data;
  p.append(teamHead('This board acts as'));
  p.append(teamKv('GitHub login', me ? el('span', { class: 'v mono', text: me.login }) : el('span', { class: 'dim', text: known ? 'not known yet' : 'reading…' }),
    me && me.id ? el('span', { class: 'dim', text: `GitHub user id ${me.id}` }) : null,
    el('span', { class: 'dim', text: 'The login gh uses on the box. Teammates see your work under it. The board never reads or stores the gh token. "Not known yet" means gh has not answered, not that nobody is logged in.' })));
  p.append(teamHead('Team GitHub access'));
  const checks = typeof doctorChecks === 'function' ? doctorChecks().filter((c) => c.id === 'gh-login' || c.id === 'team-gh') : [];
  const list = el('div', { class: 'doc-list tm-doc' });
  if (checks.length && typeof doctorRow === 'function') for (const c of checks) list.append(doctorRow(c));
  else list.append(el('p', { class: 'dim set-note', text: 'The Doctor has not answered yet. Press Check again.' }));
  p.append(list);
  p.append(el('p', { class: 'dim set-note', text: 'Least permission to read a shared repository: a classic token needs the repo scope. A fine-grained token needs read access to Issues, Pull requests and Contents on the repository, with the organization opted in. An organization with single sign-on also needs the token authorised for it: that is a different message from "no access".' }));
  const again = el('button', { class: 'small tm-recheck', type: 'button', text: 'Check again', onclick: async () => {
    await teamRefresh();
    if (typeof doctorLoad === 'function') doctorLoad(true);
    teamLoad();
  } });
  p.append(el('div', { class: 'tm-actions' }, again, el('span', { class: 'dim', text: 'Reads the shared repositories again as soon as GitHub allows, and asks the Doctor.' })));
  p.append(teamHead('Shared repositories'));
  if (teamStore.err) p.append(el('p', { class: 'bad set-err', role: 'alert', text: `Could not read the team view: ${teamStore.err}` }));
  const blocks = teamBlocks();
  if (!blocks.length) p.append(el('p', { class: 'dim set-note', text: known ? 'No project of this board has a github.com remote, so there is nothing to share.' : 'Reading the team settings…' }));
  for (const b of blocks) {
    const slug = teamSlug(b.slug);
    const ago = teamAgo(b.read_at);
    p.append(el('section', { class: 'tm-srow', 'aria-label': `Team: ${slug}`, 'data-repo': b.repo_slug },
      el('div', { class: 'tm-repohead' }, el('b', { class: 'tm-h mono', text: slug }), b.shared === true ? teamStateChip(b) : null, ago && b.shared === true ? el('span', { class: 'dim tm-age', text: `read ${ago}` }) : null),
      teamShareRow(b),
      b.shared === true && teamState(b) !== 'ok' && b.message ? el('p', { class: 'dim tm-smsg', text: teamText(b.message, 300) }) : null));
  }
  p.append(teamHead('What it costs'));
  p.append(el('p', { class: 'dim set-note', text: 'Team reads are last in line for the 5,000 hourly GitHub requests of this login: at most 600 an hour in all. Issues and pull requests are read about every 2 minutes per repository (slower with many shared repositories, which share the 600), branch links every 10 minutes, and less often when few requests are left. With nothing shared the board asks GitHub for nothing.' }));
}

/* ---------- the demo (?demo=1): core.js demoApi calls this for /api/team/... ---------- */

const demoTeamMade = {};                                           // repo_slug -> the switch as the visitor left it
function demoTeamFlag() { try { const m = /[?&]team=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }

function demoTeamBlock(r, on) {
  const zero = { people: 0, teammates: 0, issues: 0, branches: 0, prs: 0, unattributed_branches: 0 };
  if (on === r.shared) return r;
  if (!on) return { ...r, shared: false, state: 'off', message: 'Not shared with your team. Nothing is read from GitHub for this repository.', stale: false, read_at: null, age_s: null, gql_at: null, graph_pending: false, private: null, people: [], counts: zero };
  return { ...r, shared: true, state: 'ok', message: '', wait_s: null, stale: false, read_at: new Date().toISOString(), age_s: 0, gql_at: new Date().toISOString(), graph_pending: false, people: [], counts: zero };
}

async function demoTeam(method, path, body) {
  const bare = path.split('?')[0];
  await new Promise((resolve) => setTimeout(resolve, 60));
  const r = await fetch('/static/demo/team.json');
  if (!r.ok) throw new Error(`demo fixture team.json: ${r.status} ${r.statusText}`);
  const fx = demoRebase(await r.json());
  const flag = demoTeamFlag();
  const v = flag && ownKey(fx.variants || {}, flag) ? fx.variants[flag] : null;
  const build = () => {
    const now = Date.now();
    const repos = fx.base.repos.map((b) => (v && v.repo && b.repo_slug === 'github.com/example/website' ? { ...b, ...v.repo } : b))
      .map((b) => (ownKey(demoTeamMade, b.repo_slug) ? demoTeamBlock(b, demoTeamMade[b.repo_slug]) : b))
      .map((b) => (b.read_at ? { ...b, age_s: Math.max(0, Math.floor((now - Date.parse(b.read_at)) / 1000)) } : b));
    return { ...fx.base, ...((v && v.body) || {}), repos, enabled: repos.some((b) => b.shared === true), at: new Date(now).toISOString() };
  };
  if (method === 'GET' && bare === '/api/team/state') return build();
  if (method === 'POST' && bare === '/api/team/refresh') return { queued: build().enabled };
  const m = /^\/api\/team\/repos\/([^/]+)\/([^/]+)$/.exec(bare);
  if (method === 'PUT' && m) {
    const slug = `${decodeURIComponent(m[1])}/${decodeURIComponent(m[2])}`.toLowerCase();
    const hit = build().repos.find((b) => String(b.slug).toLowerCase() === slug);
    if (!hit) throw demoError(404, `this board has no project for ${slug}`);
    if (!body || typeof body.shared !== 'boolean') throw demoError(400, 'shared must be true or false');
    demoTeamMade[hit.repo_slug] = body.shared;
    return build().repos.find((b) => b.repo_slug === hit.repo_slug);
  }
  return {};
}

const Team = { store: teamStore, load: teamLoad, watch: teamWatch, share: teamShare, refresh: teamRefresh, sig: teamSig, blocks: teamBlocks, blocksFor: teamBlocksFor, blockNode: teamBlockNode,
  personNode: teamPersonNode, shareRow: teamShareRow, projectSection: teamProjectSection, settingsPanel: teamSettingsPanel, text: teamText, link: teamLink, ago: teamAgo };
