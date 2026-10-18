/* ccboard Team (issue #197, Team P1), the lazy bundle 'team' (lazy.js): who is working on what in the repositories the owner shared with the team, read from GitHub by the box's
   own gh and shown read only. Three surfaces use it: the project page's Team tab (pages/project.js pjTeamView), Settings > Team (pages/settings.js settingsTeam) and the demo
   (demoTeam, called by core.js demoApi for /api/team/...). Nothing here is loaded, fetched or polled until one of them asks.

   The data is GET /api/team/state: {api, enabled, me:{login,id}|null, at, repos[{repo_slug, slug, url, local[{project,repo}], shared, state, message, wait_s, stale, read_at,
   age_s, gql_at, graph_pending, graph_failed, private, people[...], counts, truncated}]}. One block per repository of this board's own projects; only a shared one is ever read. The state is
   a fixed word (off, loading, ok, sso, no_access, not_found, rate_limited, low_budget, no_login, no_gh, error) and each has its own words and its one next step: an access problem,
   a rate limit or an old read is never drawn as "no teammates". The switch is PUT /api/team/repos/<owner>/<repo> {shared} (off by default). The block itself has no control:
   its only actions are links to GitHub, built here from the page's own checks (https://github.com/<owner>/<repo>/(issues|pull)/<n> or /tree/<branch>), never copied blindly.

   Issue #198 (Team P2) adds the live line of each teammate (people[].presence.boards, repos[].presence, read from the presence issue) to the block, and, in Settings > Team only,
   GET /api/team/presence with its controls: the publish switch of each repository (PUT /api/team/presence/<owner>/<repo> {publish, private, confirm_public}), Private, the global
   Pause (PUT /api/team/presence {paused}), the second confirm of a public repository (a 409 with needs_confirm) and "What I published", the literal last body. Publishing is off by default.

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
const teamStore = { data: null, at: 0, err: '', seq: 0, subs: new Set(), timer: null, saving: {}, errors: {}, soon: [],
  pres: null, presErr: '', presSeq: 0, presSubs: new Set(), presSaving: {}, presErrors: {}, confirm: {}, open: {} };   // pres: GET /api/team/presence (Settings > Team only); confirm: repo_slug -> the second confirm being asked; open: the "What I published" blocks left open

/* The live line (issue #198). The word comes from the board (GitHub's clock); the glyph and the words are fixed here. A glyph and words, never colour alone. */
const TEAM_LIVE = { live: ['●', 'Live'], idle: ['◐', 'Idle'], unconfirmed: ['◌', 'Not confirmed yet'], stale: ['!', 'Stale'], unknown: ['?', 'Unknown'], gone: ['✕', 'Gone'] };
const TEAM_LIVE_RANK = { live: 0, idle: 1, unconfirmed: 2, stale: 3, unknown: 4, gone: 5 };
const TEAM_SESSION = { running: ['✽', 'Running'], needs_you: ['✻', 'Needs you'], idle: ['∙', 'Idle'], ended: ['○', 'Ended'] };       // the board's own state words and glyphs
const TEAM_AGENT = { claude: '◆', codex: '◇', shell: '▸', other: '·' };
const TEAM_LEASE_S = 1800;                                       // a line older than this is stale, whatever the last read said (the lease of the board that writes it)
const TEAM_GONE_S = 86400;
const TEAM_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const TEAM_BODY_MAX = 3200;
const TEAM_BODY_CTRL_RE = /[\u0000-\u0009\u000b-\u001f\u007f-\u009f\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb]/g;     // the control set above, but a line break stays

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

/* "just now", "12 min ago", "3 h ago", "2 d ago" from a number of seconds, or from an ISO time (or ''). */
function teamSpan(s) {
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function teamAgo(iso) {
  const t = typeof iso === 'string' ? Date.parse(iso) : NaN;
  if (Number.isNaN(t)) return '';
  return teamSpan(Math.max(0, Math.floor((Date.now() - t) / 1000)));
}

/* "14:10" for today, "Oct 2 14:10" for another day, from an ISO time (or ''). Local time of this device: it only names a moment, it decides nothing. */
function teamClock(iso) {
  const t = typeof iso === 'string' ? Date.parse(iso) : NaN;
  if (Number.isNaN(t)) return '';
  const d = new Date(t);
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return d.toDateString() === new Date().toDateString() ? hm : `${TEAM_MONTHS[d.getMonth()]} ${d.getDate()} ${hm}`;
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
  return JSON.stringify([teamStore.data && teamStore.data.me, teamBlocks().map((b) => [b.repo_slug, b.shared, b.state, b.message, b.stale, b.read_at, b.gql_at, b.counts, b.people, b.truncated, b.graph_pending, b.graph_failed, b.presence, b.warnings]),
    teamStore.err, teamStore.errors, teamStore.saving, Math.floor(Date.now() / 60000),
    teamStore.pres, teamStore.presErr, teamStore.presErrors, teamStore.presSaving, teamStore.confirm]);
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
  if (teamStore.presSubs.size) teamPresenceLoad();                 // Settings > Team is open: its presence settings come with the team view
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

/* GET /api/team/presence (issue #198): this board's publishing settings. Asked only by Settings > Team; the answer asks GitHub for nothing. */
async function teamPresenceLoad() {
  const mine = ++teamStore.presSeq;
  let body = null;
  try { body = await api('GET', '/api/team/presence'); } catch (e) {
    if (mine !== teamStore.presSeq) return false;
    teamStore.presErr = (e && e.message) || 'the board did not answer';
    teamNotify();
    return false;
  }
  if (mine !== teamStore.presSeq) return false;
  if (!body || typeof body !== 'object' || !Array.isArray(body.repos)) { teamStore.presErr = 'the board answered without the presence settings'; teamNotify(); return false; }
  teamStore.pres = body;
  teamStore.presErr = '';
  teamNotify();
  return true;
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

/* Watch the data while a surface is on screen: fn runs after every change; the board is asked now (when the last answer is old) and then every TEAM_POLL_MS. Returns the stop function.
   {presence: true} (Settings > Team) also reads the publishing settings. */
function teamWatch(fn, opts) {
  teamStore.subs.add(fn);
  const wantPres = !!(opts && opts.presence);
  if (wantPres) teamStore.presSubs.add(fn);
  teamSchedule();
  if (!teamStore.data || Date.now() - teamStore.at > 5000) teamLoad();
  else if (wantPres) teamPresenceLoad();
  let live = true;
  return () => {
    if (!live) return;
    live = false;
    teamStore.subs.delete(fn);
    teamStore.presSubs.delete(fn);
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
    stale: false, read_at: null, age_s: null, gql_at: null, presence: null, people: [], counts: { people: 0, teammates: 0, issues: 0, branches: 0, prs: 0, unattributed_branches: 0, live: 0 } });
  teamNotify();
  let ok = false;
  try {
    const nb = await api('PUT', `/api/team/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`, { shared: on });
    if (nb && typeof nb === 'object' && nb.repo_slug === key) { swap(nb); ok = true; }
    else { swap(before); teamStore.errors[key] = 'Not saved: the board answered without the repository.'; }
  } catch (e) {
    swap(before);
    teamStore.errors[key] = `Not saved: ${errorText(e)}`;
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
    if (typeof pageToast === 'function') pageToast(`Could not ask for a new read: ${errorText(e)}`, 'bad');
    return false;
  }
  teamSoon([2000, 6000]);
  if (typeof pageToast === 'function') pageToast(r && r.queued === false ? 'Nothing is shared, so nothing is read.' : 'Asked the board to read GitHub again.', 'info');
  return !!(r && r.queued !== false);
}

/* ---------- publishing (issue #198): Settings > Team only ---------- */

function teamPresBlocks() {
  const v = teamStore.pres;
  return v && Array.isArray(v.repos) ? v.repos.filter((b) => b && typeof b === 'object' && typeof b.repo_slug === 'string' && teamSlug(b.slug)) : [];
}

/* One PUT of a repository's publishing switches. Optimistic like teamShare: the row shows its new state at once, the board's block replaces it, a failed PUT puts the old one back and says
   why beside the switch. A 409 with needs_confirm (a public repository, or one GitHub has not said is private) is not a failure: it opens the second confirm of that row. */
async function teamPresPut(block, payload, optimistic) {
  const key = block && block.repo_slug;
  const slug = teamSlug(block && block.slug);
  if (!key || !slug || !teamStore.pres || teamStore.presSaving[key]) return false;
  const [owner, repo] = slug.split('/');
  const before = teamPresBlocks().find((x) => x.repo_slug === key) || block;
  const swap = (nb) => { teamStore.pres = { ...teamStore.pres, repos: teamStore.pres.repos.map((b) => (b && b.repo_slug === key ? nb : b)) }; };
  teamStore.presSaving[key] = true;
  delete teamStore.presErrors[key];
  delete teamStore.confirm[key];
  teamStore.presSeq += 1;                                          // a read that was on its way would bring the old switch back
  swap(optimistic(before));
  teamNotify();
  let ok = false;
  try {
    const nb = await api('PUT', `/api/team/presence/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}`, payload);
    if (nb && typeof nb === 'object' && nb.repo_slug === key) { swap(nb); ok = true; }
    else { swap(before); teamStore.presErrors[key] = 'Not saved: the board answered without the repository.'; }
  } catch (e) {
    swap(before);
    const b = e && e.body && typeof e.body === 'object' ? e.body : null;
    if (e && e.status === 409 && b && b.needs_confirm === true && payload.confirm_public !== true) {
      teamStore.confirm[key] = { text: teamText(b.error, 300), lists: (Array.isArray(b.public_lists) ? b.public_lists : []).map((x) => teamText(x, 200)).filter(Boolean).slice(0, 8), body: teamBody(b.next_body) };
    } else teamStore.presErrors[key] = `Not saved: ${errorText(e)}`;
  }
  delete teamStore.presSaving[key];
  teamNotify();
  if (ok) teamSoon([2000, 6000]);
  return ok;
}

/* The switch "Publish my status". confirmed is true only from the second confirm of a public repository. */
function teamPublish(block, on, confirmed) {
  if (typeof on !== 'boolean') return Promise.resolve(false);
  const payload = on ? (confirmed === true ? { publish: true, confirm_public: true } : { publish: true }) : { publish: false };
  return teamPresPut(block, payload, (b) => (on ? { ...b, publish: true, state: 'publishing', message: '' } : { ...b, publish: false, private: false, state: 'off', message: '', published: false, last_body: null, last_at: null }));
}

function teamPrivate(block, on) {
  if (typeof on !== 'boolean') return Promise.resolve(false);
  return teamPresPut(block, { private: on }, (b) => ({ ...b, private: on, state: on ? 'private' : 'publishing', message: '' }));
}

/* The global "Pause team presence": the board answers its whole presence view. */
async function teamPause(on) {
  if (typeof on !== 'boolean' || !teamStore.pres || teamStore.presSaving['*']) return false;
  const before = teamStore.pres;
  teamStore.presSaving['*'] = true;
  delete teamStore.presErrors['*'];
  teamStore.presSeq += 1;
  teamStore.pres = { ...before, paused: on };
  teamNotify();
  let ok = false;
  try {
    const nv = await api('PUT', '/api/team/presence', { paused: on });
    if (nv && typeof nv === 'object' && Array.isArray(nv.repos)) { teamStore.pres = nv; ok = true; }
    else { teamStore.pres = before; teamStore.presErrors['*'] = 'Not saved: the board answered without the presence settings.'; }
  } catch (e) {
    teamStore.pres = before;
    teamStore.presErrors['*'] = `Not saved: ${errorText(e)}`;
  }
  delete teamStore.presSaving['*'];
  teamNotify();
  if (ok) teamSoon([2000, 6000]);
  return ok;
}

function teamConfirmCancel(key) {
  delete teamStore.confirm[key];
  teamNotify();
}

/* Text with line breaks (a published body): cut first, control and bidi characters dropped, the line breaks kept, shown through textContent in a pre. */
function teamBody(v) {
  if (typeof v !== 'string') return '';
  return v.slice(0, TEAM_BODY_MAX).replace(/\r\n?/g, '\n').replace(TEAM_BODY_CTRL_RE, ' ').trim();
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
    return { kind: 'dim', title: 'No teammates found here', text: 'Nobody else has an issue assigned, a branch linked to an issue or pushed in the last 14 days, an open pull request or a published live status in this repository.',
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
function teamItem(kind, word, glyph, ref, title, facts, when, url, aria, collides) {
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
    when ? el('span', { class: 'tm-when dim', text: when }) : null,
    teamCollideNode(collides));
}

/* ---------- the live line (issue #198) ---------- */

/* Seconds since a board's line was last seen on GitHub's clock: the board worked it out at the last read (last_seen_s), and the time since that read is added (an elapsed time, not a clock
   reading). null when GitHub gave no clock. */
function teamSeenS(b, block) {
  if (!Number.isInteger(b.last_seen_s) || b.last_seen_s < 0 || b.last_seen_s > 1e9) return null;
  const at = block && typeof block.read_at === 'string' ? Date.parse(block.read_at) : NaN;
  return b.last_seen_s + (Number.isNaN(at) ? 0 : Math.max(0, Math.floor((Date.now() - at) / 1000)));
}

/* The word shown for a line: the board's word, made stale when the line has outlived the lease since that read (never the other way: an unknown or stale line is never made live), and gone
   (not shown) after 24 hours. */
function teamLiveWord(b, block) {
  let w = ownKey(TEAM_LIVE, b.live) ? b.live : 'unknown';
  const age = teamSeenS(b, block);
  if (age !== null && age >= TEAM_GONE_S) return 'gone';
  if (age !== null && age >= TEAM_LEASE_S && (w === 'live' || w === 'idle' || w === 'unconfirmed')) w = 'stale';
  return w;
}

/* One board's line: liveness, agent, the board's own state words, the issue and branch as links, since when, last active, last seen. Every value is checked here again. */
function teamBoardNode(b, block, many) {
  const w = teamLiveWord(b, block);
  if (w === 'gone') return null;
  const agent = ownKey(TEAM_AGENT, b.agent) ? b.agent : null;
  const sess = ownKey(TEAM_SESSION, b.state) ? b.state : null;
  const n = teamNum(b.issue);
  const issue = n ? teamLink(b.issue_url, `#${n}`, `Open issue ${n} on GitHub`) : null;
  if (issue) issue.classList.add('tm-tl', 'mono');
  const name = teamText(b.branch, 100);
  const branch = name ? teamLink(b.branch_url, name, `Open branch ${name} on GitHub`) : null;
  if (branch) branch.classList.add('tm-tl');
  const phase = ownKey({ backlog: 1, queued: 1, running: 1, done: 1, failed: 1, cancelled: 1 }, b.phase) ? b.phase : '';
  const since = teamClock(b.since);
  const active = teamAgo(b.last);
  const age = teamSeenS(b, block);
  const node = many ? teamText(b.node, 40) : '';
  const facts = [];
  if (phase) facts.push(`task ${phase}`);
  if (since) facts.push(`since ${since}`);
  if (active) facts.push(`last active ${active}`);
  facts.push(age === null ? 'last seen unknown' : `last seen ${teamSpan(age)}`);
  const [glyph, words] = TEAM_LIVE[w];
  return el('li', { class: 'tm-pl', 'data-live': w, 'data-state': sess || null }, teamSpaced([
    el('span', { class: 'tm-lv', 'data-live': w }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: glyph }), ` ${words}`),
    agent ? el('span', { class: 'tm-ag', 'data-agent': agent }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: TEAM_AGENT[agent] }), ` ${agent}`) : null,
    sess && !(w === 'idle' && sess === 'idle') ? el('span', { class: 'tm-ps', 'data-state': sess }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: TEAM_SESSION[sess][0] }), ` ${TEAM_SESSION[sess][1]}`) : null,
    issue, branch,
    node ? el('span', { class: 'tm-nd dim', text: `on ${node}` }) : null,
    el('span', { class: 'tm-facts dim', text: facts.join(' · ') })]));
}

/* The parts of a line with a space between them (the rows are flex boxes, but a reader of the text, or a screen reader, takes them as one line). */
function teamSpaced(kids) {
  const out = [];
  for (const k of kids) { if (k === null || k === undefined || k === false) continue; if (out.length) out.push(' '); out.push(k); }
  return out;
}

/* The live lines of a person (one per board that publishes), or null. */
function teamLiveNode(p, block) {
  const pres = p && p.presence && typeof p.presence === 'object' ? p.presence : null;
  const boards = (Array.isArray(pres && pres.boards) ? pres.boards : []).filter((x) => x && typeof x === 'object').slice(0, 8);
  boards.sort((a, c) => (TEAM_LIVE_RANK[teamLiveWord(a, block)] || 0) - (TEAM_LIVE_RANK[teamLiveWord(c, block)] || 0));
  const rows = boards.map((x) => teamBoardNode(x, block, boards.length > 1)).filter(Boolean);
  return rows.length ? el('ul', { class: 'tm-live', 'aria-label': 'Live status' }, rows) : null;
}

function teamPersonNode(p, block) {
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
    list.append(teamItem('issue', 'Assigned', '●', `#${n}`, teamText(i.title), facts, i.updated_at ? `updated ${teamAgo(i.updated_at)}` : '', i.url, `Open issue ${n} on GitHub`, i.collides));
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
      r.url, `Open ${draft ? 'draft ' : ''}pull request ${n} on GitHub`, r.collides));
  }
  for (const b of branches) {
    const name = teamText(b && b.name, 100);
    if (!name) continue;
    const linked = b.source === 'linked';
    const n = teamNum(b.issue);
    list.append(teamItem(linked ? 'linked' : 'pushed', linked ? 'Linked branch' : 'Pushed branch', linked ? '↳' : '↑', name, '', n ? [`linked to #${n}`] : [], b.at ? `${linked ? 'last commit' : 'pushed'} ${teamAgo(b.at)}` : '',
      b.url, `Open branch ${name} on GitHub`, b.collides));
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
      el('span', { class: 'tm-sum dim', text: parts.join(', ') || (p && p.presence ? 'nothing on GitHub yet' : 'nothing open here') }),
      active ? el('span', { class: 'tm-act dim', text: `active ${active}` }) : null),
    teamLiveNode(p, block),
    list.firstChild ? list : null);
}

/* What the board found of the presence issue of a repository (repos[].presence), in words under the people: where the live lines come from, or why there are none. null when there is nothing to say. */
function teamPresenceNote(b) {
  const pr = b.presence && typeof b.presence === 'object' ? b.presence : null;
  if (!pr) return null;
  const n = teamNum(pr.issue);
  const link = n ? teamLink(pr.issue_url, `presence issue #${n}`, `Open presence issue ${n} on GitHub`) : null;
  if (link) link.classList.add('tm-tl');                                          // inside a sentence it is a plain link, not a button
  const say = (kind, text, tail) => el('p', { class: `tm-pn ${kind}`, role: kind === 'warn' ? 'status' : null },
    kind === 'warn' ? el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: '!' }) : null, kind === 'warn' ? ` ${text}` : text, tail || null);
  const rows = [];
  if (pr.state === 'ok') rows.push(say('dim', 'Live status is read from the ', [link || 'presence issue', '.']));
  else if (pr.state === 'none') rows.push(say('dim', 'Nobody publishes a live status in this repository yet. A teammate can switch on Publish my status in Settings > Team.'));
  else if (pr.state === 'closed') rows.push(say('warn', 'The presence issue is closed, so no live status is shown here.', link ? [' ', link, '.'] : null));
  else if (pr.state === 'error') rows.push(say('warn', 'The live status could not be read just now. The board tries again by itself.'));
  if (pr.state === 'ok' && pr.editor_check === 'failed') rows.push(say('warn', 'Some live lines are left out: GitHub would not say who last edited them.'));
  if (pr.truncated === true) rows.push(say('dim', 'Only the first part of the live status comments was read, so some lines may be missing.'));
  return rows.length ? el('div', { class: 'tm-pnote' }, rows) : null;
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
    st === 'ok' && Number.isInteger(counts.live) && counts.live > 0 ? el('span', { class: 'tm-livecount', text: `${counts.live} live now` }) : null,
    b.private === true ? el('span', { class: 'tm-priv dim', text: 'private repository' }) : null);
  const ul = el('ul', { class: 'tm-people' + (st !== 'ok' || b.stale === true ? ' stale' : '') });
  for (const p of people) ul.append(teamPersonNode(p, b));
  const foot = [];
  if (b.graph_failed === true && st !== 'loading' && st !== 'off') foot.push('Branches linked to issues and the issues a pull request closes could not be read just now; the board will try again soon. The issues and pull requests above are from the latest read.');
  else if (st === 'ok' && b.graph_pending === true) foot.push('Branches linked to issues and the issues a pull request closes are read slowly (every 10 minutes); they are not here yet.');
  if (Number.isInteger(counts.unattributed_branches) && counts.unattributed_branches > 0) foot.push(`${teamPlural(counts.unattributed_branches, 'recent branch', 'recent branches')} with no GitHub author ${counts.unattributed_branches === 1 ? 'is' : 'are'} not listed.`);
  if (tr.issues === true || tr.pulls === true || tr.tips === true) foot.push(`The board read only the first part of the ${[tr.issues ? 'issues' : '', tr.pulls ? 'pull requests' : '', tr.tips ? 'branches' : ''].filter(Boolean).join(', ')} of this repository, so some rows may be missing.`);
  const pnote = st === 'ok' ? teamPresenceNote(b) : null;
  return el('div', { class: 'tm-block', 'data-state': st, 'data-repo': b.repo_slug },
    status, note ? teamNoticeNode(note) : null, ul.firstChild ? ul : null,
    pnote, foot.length ? el('div', { class: 'tm-foot dim' }, foot.map((t) => el('p', { text: t }))) : null);
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
        el('span', { class: 'dim', text: b.shared === true ? `On. This board reads the issues, branches and pull requests of ${slug} from GitHub${me ? ' as ' + me.login : ''}. Reading writes nothing to GitHub.` : `Off. Turn on to see who else works in ${slug}: the board then reads its issues, branches and pull requests from GitHub, read only. Reading writes nothing to GitHub.` }))),
    err ? el('p', { class: 'tm-err bad', role: 'alert', text: err }) : null);
}

/* ---------- conflict warnings (issue #199, Team P3) ----------
   Advice, never a lock: a warning says who seems to hold an issue or a branch, or which pull request changes the same files, and the launcher's Start is the same button with or without
   it. The data is repos[].warnings of GET /api/team/state (app/team.py `conflicts`): {api, identity_known, issues{"<n>": [warning]}, branches{"<name>": [warning]}, tasks{"<id>": {task,
   title, issue[], branch[], files[]}}, files{checked, pending, capped}}, or null (not shared, not read yet). Nothing here asks GitHub for anything: the launcher reads the view the board
   already holds. Every name, branch and file path comes from GitHub or from a task and is cut, stripped of control characters and set with textContent only. */

const TEAM_WARN_MAX = 4;                                         // lines of one kind in the launcher; the rest is "and N more"
const TEAM_WARN_FILES = 6;                                       // file names on one line
const TEAM_WARN_WORDS = { taken: ['!', 'Taken'], recent: ['◌', 'Seen lately'], stale: ['◌', 'Stale'] };
const TEAM_SIGNAL_STATE = { running: 'running', needs_you: 'needs them', idle: 'idle', ended: 'ended' };

/* "@login", or "a teammate" when GitHub's text failed the login rule (the id is still known). Never "@null". */
function teamWho(person) {
  const l = person && typeof person === 'object' ? teamLogin(person.login) : '';
  return l ? `@${l}` : 'a teammate';
}

/* The name git gives the branch of a task started with this title (app/tasks.py slugify, then worktree-<slug>): where a branch warning is looked up before the task exists. */
function teamBranchFor(title) {
  const s = String(typeof title === 'string' ? title : '').slice(0, 400).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').replace(/-{2,}/g, '-').slice(0, 40).replace(/^-+|-+$/g, '');
  return `worktree-${s || 'task'}`;
}

function teamWarnList(v) { return Array.isArray(v) ? v.filter((w) => w && typeof w === 'object' && w.advice === true).slice(0, 10) : []; }

/* The warnings of a repository in this project, or null: the block of a shared repository, its warnings (null when none were worked out) and the quiet note that says why they may be
   missing or old. null when no repository of this project is shared (the launcher then shows nothing at all). */
function teamLaunchView(project, repo) {
  const blocks = teamBlocksFor(project, repo).filter((b) => b.shared === true);
  if (!blocks.length) return null;
  const b = blocks.find((x) => x.warnings && typeof x.warnings === 'object') || blocks[0];
  const w = b.warnings && typeof b.warnings === 'object' && b.warnings.api === 1 ? b.warnings : null;
  return { block: b, warnings: w, note: teamWarnNote(b, w) };
}

/* The quiet line for the cases where "no warning" does not mean "nobody": reading for the first time, a read that failed, an old read, an unknown login. Never an error, never empty-as-zero. */
function teamWarnNote(b, w) {
  const st = teamState(b);
  const ago = teamAgo(b.read_at);
  if (st === 'off') return '';
  if (st === 'loading') return 'Team: GitHub is being read for the first time. Nothing is checked yet.';
  if (st !== 'ok') return `Team: ${TEAM_STATES[st][1].toLowerCase()}.${w ? ` Warnings are from the last read${ago ? ', ' + ago : ''}.` : ' Nothing is checked yet.'}`;
  if (b.stale === true) return `Team: last read ${ago || 'a while ago'}, so warnings may be out of date.`;
  if (!w) return '';
  if (w.identity_known === false) return 'Team: who you are on GitHub is not known yet, so nobody is checked.';
  return '';
}

/* Seconds since a person's line was last seen: the board worked it out at the read (age_s, GitHub's clock) and the time since the read is added. null when GitHub gave no clock. */
function teamWarnAge(w, block) { return teamSeenS({ last_seen_s: w && w.age_s }, block); }

function teamSignalWords(s) {
  const kind = s && typeof s === 'object' ? s.kind : '';
  if (kind === 'assigned') return 'assigned on GitHub';
  if (kind === 'claim') return `holds a claim${teamText(s.status, 20) ? ' (' + teamText(s.status, 20) + ')' : ''}`;
  if (kind === 'presence') return `live status: ${ownKey(TEAM_SIGNAL_STATE, s.state) ? TEAM_SIGNAL_STATE[s.state] : 'seen'}`;
  if (kind === 'branch') return `branch ${teamText(s.name, 100)}`;
  if (kind === 'pr') { const n = teamNum(s.number); return n ? `${s.draft === true ? 'draft ' : ''}PR #${n}` : ''; }
  return '';
}

/* One person's warning about one issue: {status, glyph, head, detail}. taken: "#212 is being worked on by @bob since 14:10" (since only from a live status line; a stale one gives "last seen 3 hours ago" instead); recent and stale say how
   long ago the person was seen. */
function teamIssueLine(w, block) {
  const n = teamNum(w.issue);
  const st = ownKey(TEAM_WARN_WORDS, w.status) ? w.status : 'stale';
  const who = teamWho(w.person);
  const ref = n ? `#${n}` : 'This issue';
  const age = teamWarnAge(w, block);
  const since = w.since_from === 'presence' ? teamClock(w.since) : '';
  let head;
  const stale = st === 'taken' && !since ? teamSeenS({ last_seen_s: w.stale_age_s }, block) : null;
  if (st === 'taken') head = `${ref} is being worked on by ${who}${since ? ' since ' + since : ''}${stale !== null ? ', last seen ' + teamSpan(stale) : ''}`;
  else if (st === 'recent') head = `${ref}: ${who} may be working on it, seen ${age === null ? 'lately' : teamSpan(age)}`;
  else head = `${ref}: ${who} worked on it, ${age === null ? 'not seen lately' : 'last seen ' + teamSpan(age)}`;
  const detail = (Array.isArray(w.signals) ? w.signals : []).slice(0, 6).map(teamSignalWords).filter(Boolean).join(' · ');
  return { kind: 'issue', status: st, glyph: TEAM_WARN_WORDS[st][0], head: [head], detail };
}

/* The chip on a row of the issue picker: "! taken by @bob", "◌ @bob, last seen 3 h ago"; '' when nobody holds the issue. A second person adds ", +1". */
function teamIssueChip(view, n) {
  if (!view || !view.warnings || !teamNum(n)) return '';
  const list = teamWarnList(view.warnings.issues && view.warnings.issues[String(n)]);
  if (!list.length) return '';
  const w = list[0];
  const st = ownKey(TEAM_WARN_WORDS, w.status) ? w.status : 'stale';
  const who = teamWho(w.person);
  const age = teamWarnAge(w, view.block);
  const more = list.length > 1 ? `, +${list.length - 1}` : '';
  if (st === 'taken') return `! taken by ${who}${more}`;
  if (st === 'recent') return `◌ ${who}, seen ${age === null ? 'lately' : teamSpan(age)}${more}`;
  return `◌ ${who}, ${age === null ? 'not seen lately' : 'last seen ' + teamSpan(age)}${more}`;
}

function teamBranchLine(w, name) {
  const who = teamWho(w.person);
  const src = Array.isArray(w.sources) ? w.sources : [];
  const bits = [];
  if (src.includes('linked') && teamNum(w.issue)) bits.push(`linked to #${teamNum(w.issue)}`);
  if (src.includes('pr') && teamNum(w.pr)) bits.push(`PR #${teamNum(w.pr)}`);
  if (src.includes('pushed') || !bits.length) bits.push('pushed');
  if (w.at) bits.push(`${teamAgo(w.at)}`);
  if (w.old === true) bits.push('older than 14 days');
  return { kind: 'branch', status: 'taken', glyph: '!', head: [`Branch ${teamText(name, 100)} is already on GitHub under ${who}`], detail: bits.join(' · ') };
}

function teamFilesLine(w) {
  const pr = w.pr && typeof w.pr === 'object' ? w.pr : {};
  const n = teamNum(pr.number);
  const link = n ? teamLink(pr.url, `PR #${n}`, `Open pull request ${n} on GitHub`) : null;
  if (link) link.classList.add('tm-tl');
  const files = (Array.isArray(w.files) ? w.files : []).map((f) => teamText(f, 300)).filter(Boolean);
  const count = Number.isInteger(w.count) && w.count >= files.length ? w.count : files.length;
  const shown = files.slice(0, TEAM_WARN_FILES);
  const rest = count - shown.length;
  return { kind: 'files', status: 'taken', glyph: '!', files: shown,
    head: [link || 'A pull request', ` by ${teamWho(w.person)}${pr.draft === true ? ' (draft)' : ''} also changes ${count}${w.more === true ? ' or more' : ''} of these files`],
    detail: rest > 0 ? `and ${rest} more` : (w.more === true ? 'the list of changed files is cut' : '') };
}

/* Every line the launcher shows for this moment. ctx: {issue: number | null (the picked issue), title: the typed title (its branch is looked up), task: number | null (a task being
   started: its own issue, branch and files)}. Lines come from the board's warnings only; no line when there is nothing, and a quiet note when "nothing" could be "not known". */
function teamLaunchLines(view, ctx) {
  const out = [];
  if (!view) return out;
  const w = view.warnings;
  if (w) {
    const c = ctx || {};
    const seen = new Set();
    const take = (kind, list, make) => {
      const items = list.slice(0, TEAM_WARN_MAX);
      for (const x of items) out.push(make(x));
      if (list.length > items.length) out.push({ kind, status: 'taken', glyph: '!', head: [`and ${list.length - items.length} more`], detail: '' });
    };
    const issue = teamNum(c.issue);
    const tk = teamNum(c.task) && w.tasks && ownKey(w.tasks, String(c.task)) && w.tasks[String(c.task)] && typeof w.tasks[String(c.task)] === 'object' ? w.tasks[String(c.task)] : null;
    if (issue) { seen.add(issue); take('issue', teamWarnList(w.issues && w.issues[String(issue)]), (x) => teamIssueLine(x, view.block)); }
    if (tk) take('issue', teamWarnList(tk.issue).filter((x) => !seen.has(teamNum(x.issue))), (x) => teamIssueLine(x, view.block));
    const bl = [];
    if (typeof c.title === 'string' && c.title.trim()) {
      const nm = teamBranchFor(c.title);
      for (const x of teamWarnList(w.branches && ownKey(w.branches, nm) ? w.branches[nm] : [])) bl.push([x, nm]);
    }
    if (tk) for (const x of teamWarnList(tk.branch)) bl.push([x, x.name]);
    const sb = new Set();
    take('branch', bl.filter(([x, nm]) => { const k = `${nm}|${x.person && x.person.id}`; if (sb.has(k)) return false; sb.add(k); return true; }), ([x, nm]) => teamBranchLine(x, nm));
    if (tk) take('files', teamWarnList(tk.files), (x) => teamFilesLine(x));
  }
  return out;
}

/* One line as a node: the glyph, the words (one sentence), then what lies behind it, small. */
function teamLineNode(l) {
  return el('p', { class: 'lx-team-l', 'data-kind': l.kind, 'data-status': l.status }, teamSpaced([
    el('span', { class: 'tm-sg lx-team-g', 'aria-hidden': 'true', text: l.glyph }),
    el('span', { class: 'lx-team-t' }, l.head),
    l.files && l.files.length ? el('span', { class: 'lx-team-f mono' }, l.files.map((f, i) => el('span', { class: 'lx-team-file', text: i ? `, ${f}` : f }))) : null,
    l.detail ? el('span', { class: 'dim lx-team-d', text: l.detail }) : null]));
}

/* When a task is being started and some teammate pull requests have no file list yet, "no file warning" is not "no overlap": one dim line says so. '' otherwise. */
function teamPendingNote(view, ctx) {
  const w = view && view.warnings;
  const c = ctx || {};
  const n = w && w.files && typeof w.files === 'object' ? w.files.pending : null;
  if (!teamNum(c.task) || !Number.isInteger(n) || n <= 0 || n > 1e6) return '';
  return `Team: the changed files of ${teamPlural(n, 'pull request')} are not read yet, so file overlap is not fully checked.`;
}

/* Paint the launcher's team block into host (an element the form keeps above Start): the lines, then the quiet note. Returns how many lines. The host is hidden when it has nothing to say.
   It never touches a button: Start is the same with or without a warning. */
function teamLaunchPaint(host, view, ctx) {
  host.textContent = '';
  const lines = teamLaunchLines(view, ctx);
  for (const l of lines) host.append(teamLineNode(l));
  if (view && view.note) host.append(el('p', { class: 'dim lx-team-n', text: view.note }));
  const pending = teamPendingNote(view, ctx);
  if (pending) host.append(el('p', { class: 'dim lx-team-n', text: pending }));
  host.classList.toggle('quiet', !lines.length);
  host.classList.toggle('hidden', !host.firstChild);
  return lines.length;
}

/* The mark on a row of the project page's Team block that collides with this board's own open task (repos[].people[].issues|branches|prs[].collides). The words say what collides. */
function teamCollideNode(list) {
  const rows = (Array.isArray(list) ? list : []).filter((c) => c && typeof c === 'object' && (c.kind === 'issue' || c.kind === 'branch' || c.kind === 'files')).slice(0, 3);
  if (!rows.length) return null;
  const parts = rows.map((c) => {
    const t = teamText(c.title, 60);
    const task = t ? `your task “${t}”` : 'one of your tasks';
    if (c.kind === 'issue') return `same issue as ${task}`;
    if (c.kind === 'branch') return `same branch as ${task}`;
    const files = (Array.isArray(c.files) ? c.files : []).map((f) => teamText(f, 300)).filter(Boolean).slice(0, 3);
    const count = Number.isInteger(c.count) && c.count >= files.length ? c.count : files.length;
    return `changes ${teamPlural(count, 'file')} that ${task} changes${files.length ? ': ' + files.join(', ') + (count > files.length || c.more === true ? ', …' : '') : ''}`;
  });
  return el('span', { class: 'tm-collide', role: 'note' }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: '!' }), ` Collides: ${parts.join('; ')}`);
}

/* Ask the board for its team view unless it was read lately. One GET of what the board already holds; it makes no GitHub call. */
async function teamEnsure(maxAgeMs) {
  if (teamStore.data && Date.now() - teamStore.at <= (Number.isInteger(maxAgeMs) ? maxAgeMs : 20000)) return true;
  return teamLoad();
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
    host.append(el('p', { class: 'tm-lede dim', text: 'Who is working on what in these repositories, read from GitHub. Reading is off until you switch a repository on, and it writes nothing. Sharing your own status is a separate switch in Settings > Team, also off. A teammate does not need ccboard to show up.' }));
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

/* ---------- Settings > Team: Publish my status (issue #198) ---------- */

const TEAM_PUB = {                                               // the state of a repository's publishing -> [glyph, words]; the board's own sentence (message) comes with it
  off: ['○', 'Not published'], paused: ['∙', 'Paused'], private: ['○', 'Private'], needs_confirm: ['!', 'Needs your confirm'], waiting: ['∙', 'Waiting for gh'], publishing: ['✓', 'Publishing'],
  removing: ['∙', 'Taking my line down'], issues_disabled: ['✕', 'Issues are off'], archived: ['✕', 'Archived'], locked: ['✕', 'Issue locked'], closed: ['✕', 'Issue closed'],
  deleted: ['✕', 'Issue deleted'], no_create: ['✕', 'Cannot open issues'], no_label: ['✕', 'Cannot add the label'], forbidden: ['✕', 'GitHub refused'], limited: ['!', 'Interaction limits'],
  rate_limited: ['!', 'Waiting for GitHub'], too_long: ['✕', 'Too many comments'], budget: ['!', 'Hourly budget spent'], error: ['!', 'Could not write'],
};
const TEAM_PUB_NEXT = {                                          // the one next step, for the states whose sentence from the board does not already give it
  needs_confirm: 'Read the box below and confirm, or leave publishing off.',
  issues_disabled: 'Turn Issues on in the repository settings on GitHub, or switch publishing off.',
  archived: 'Nothing can be written to an archived repository. Switch publishing off, or unarchive it on GitHub.',
  locked: 'Unlock the presence issue on GitHub, or switch publishing off.',
  no_create: 'Ask the owner of the repository to let this login open issues, or switch publishing off.',
  no_label: 'Ask the owner to give this login triage rights (they let it add labels), or switch publishing off.',
  forbidden: 'Ask the owner of the repository for write access, or switch publishing off. Some organizations do not let tools write to their issues.',
  limited: 'Nothing for you to do: publishing goes on when GitHub lifts the limit.',
  rate_limited: 'Nothing for you to do: publishing goes on when GitHub allows.',
  too_long: 'Ask the owner of the repository to delete old comments on the presence issue, or switch publishing off. Until then this board writes nothing there.',
  budget: 'Nothing for you to do: the next write waits for the hour to turn.',
  error: 'Nothing for you to do: the board tries again by itself.',
};
const TEAM_AUDIT = { presence_on: 'Publishing switched on', presence_off: 'Publishing switched off', presence_pause: 'Paused', presence_resume: 'Resumed', presence_private: 'Private switched on',
  presence_unprivate: 'Private switched off', presence_issue: 'Presence issue made', presence_label: 'Label made', presence_publish: 'Line written', presence_delete: 'Line taken down', presence_blocked: 'Write refused' };
const TEAM_OPT_IN = 'Publishing writes one comment for you on a ccboard presence issue in this repository. Anyone who can read the repository can read it. GitHub keeps the edit history of a comment, and everyone who can read the repository can open it while the comment exists. Some organizations do not allow tools to write to their issues.';

function teamPubState(r) { return r && typeof r.state === 'string' && ownKey(TEAM_PUB, r.state) ? r.state : 'error'; }

function teamPubChip(r) {
  const st = teamPubState(r);
  return el('span', { class: 'tm-state tm-pstate', 'data-pstate': st }, el('span', { class: 'tm-sg', 'aria-hidden': 'true', text: TEAM_PUB[st][0] }), ` ${TEAM_PUB[st][1]}`);
}

/* A switch in the style of the read switch: Blueprint's box through el(), the title in bold, one sentence under it. */
function teamSwitch(attr, key, label, aria, checked, desc, onchange, disabled) {
  const box = el('input', { type: 'checkbox', role: 'switch', [attr]: key, 'aria-label': aria, disabled: disabled === true });
  box.checked = checked === true;
  box.addEventListener('change', () => { onchange(box.checked); });
  return el('label', { class: 'tm-switch' + (disabled === true ? ' tm-dis' : '') }, box, el('span', { class: 'tm-switch-t' }, el('b', { text: label }), el('span', { class: 'dim', text: desc })));
}

/* The second confirm of a public repository (or one GitHub has not said is private): exactly what becomes readable, and the line as it would be written. Nothing is sent until Publish. */
function teamConfirmNode(r, c) {
  const slug = teamSlug(r.slug);
  const go = el('button', { class: 'primary small tm-go', type: 'button', 'data-confirm': r.repo_slug, text: 'Publish here', onclick: () => { teamPublish(r, true, true); } });
  const no = el('button', { class: 'small', type: 'button', text: 'Cancel', onclick: () => teamConfirmCancel(r.repo_slug) });
  return el('div', { class: 'tm-confirm', role: 'group', 'aria-label': `Confirm publishing in ${slug}`, 'data-repo': r.repo_slug },
    el('b', { class: 'tm-confirm-t', text: `Confirm before publishing in ${slug}` }),
    c.text ? el('p', { class: 'tm-confirm-x', text: c.text }) : null,
    el('p', { class: 'tm-confirm-x', text: 'If anyone can read this repository, this becomes public:' }),
    el('ul', { class: 'tm-confirm-l' }, c.lists.map((t) => el('li', { text: t }))),
    el('p', { class: 'tm-confirm-x', text: 'No branch name is published here. The line would read:' }),
    c.body ? el('pre', { class: 'tm-body mono', text: c.body }) : null,
    el('div', { class: 'tm-confirm-a' }, go, no));
}

/* "What I published": the literal last body, or why there is none. */
function teamPubBody(r) {
  const key = r.repo_slug;
  const body = r.published === true ? teamBody(r.last_body) : '';
  const when = r.published === true ? teamAgo(r.last_at) : '';
  const det = el('details', { class: 'tm-pub', 'data-repo': key }, el('summary', { class: 'tm-pub-s', text: 'What I published' }),
    body ? el('pre', { class: 'tm-body mono', text: body }) : el('p', { class: 'dim tm-pub-x', text: 'Nothing is published in this repository right now.' }),
    body ? el('p', { class: 'dim tm-pub-x', text: `${when ? 'Written ' + when + '. ' : ''}This is the text on GitHub. Its edit history stays readable while the comment exists.` }) : null);
  if (teamStore.open[key] === true) det.setAttribute('open', '');
  det.addEventListener('toggle', () => { teamStore.open[key] = det.hasAttribute('open'); });
  return det;
}

function teamPubRow(r, paused) {
  const key = r.repo_slug;
  const slug = teamSlug(r.slug);
  const st = teamPubState(r);
  const n = teamNum(r.issue);
  const issue = n ? teamLink(r.issue_url, `presence issue #${n}`, `Open presence issue ${n} on GitHub`) : null;
  if (issue) issue.classList.add('tm-tl');
  const vis = r.public === true ? 'public repository' : r.public === false ? 'private repository' : 'not known to be private';
  const msg = st !== 'off' && st !== 'publishing' ? teamText(r.message, 300) : '';
  const next = TEAM_PUB_NEXT[st] || '';
  const err = teamStore.presErrors[key];
  const c = teamStore.confirm[key];
  const boards = Number.isInteger(r.boards) && r.boards > 1 ? `${r.boards} of your boards publish here, each with its own line.` : '';
  const sub = r.publish === true
    ? `On. This board keeps one line on a presence issue in ${slug}.${paused ? ' Paused: nothing is written now.' : ''}`
    : `Off. Nothing is written to ${slug}.`;
  return el('section', { class: 'tm-prow', 'aria-label': `Publish my status: ${slug}`, 'data-repo': key },
    el('div', { class: 'tm-repohead' }, teamSpaced([el('b', { class: 'tm-h mono', text: slug }), teamPubChip(r), el('span', { class: 'dim tm-priv', text: vis }), issue])),
    teamSwitch('data-pub', key, 'Publish my status', `Publish my status: ${slug}`, r.publish === true, sub, (on) => { teamPublish(r, on); }, teamStore.presSaving[key] === true),
    r.publish === true ? teamSwitch('data-priv', key, 'Private', `Private: ${slug}`, r.private === true,
      r.private === true ? 'On. This board has taken its line down and writes nothing here. The switch above stays on.' : 'Off. Turn on to take my line down and stop writing here, without switching publishing off.',
      (on) => { teamPrivate(r, on); }, teamStore.presSaving[key] === true) : null,
    msg || next ? el('p', { class: 'dim tm-smsg' }, msg, msg && next ? ' ' : null, next ? el('b', { class: 'tm-next', text: 'Next: ' }) : null, next) : null,
    st === 'publishing' && r.publish === true && r.published !== true ? el('p', { class: 'dim tm-smsg', text: 'Waiting for the first write. It happens within a minute.' }) : null,
    boards ? el('p', { class: 'dim tm-smsg', text: boards }) : null,
    err ? el('p', { class: 'tm-err bad', role: 'alert', text: err }) : null,
    c ? teamConfirmNode(r, c) : null,
    r.publish === true || r.published === true ? teamPubBody(r) : null);
}

function teamPresenceSection(p) {
  const v = teamStore.pres;
  p.append(teamHead('Publish my status'));
  if (!v) {
    p.append(teamStore.presErr ? el('p', { class: 'bad set-err', role: 'alert', text: `Could not read the publishing settings: ${teamStore.presErr}` }) : el('p', { class: 'dim set-note', role: 'status', text: 'Reading the publishing settings…' }));
    return;
  }
  if (teamStore.presErr) p.append(el('p', { class: 'bad set-err', role: 'alert', text: `Could not refresh the publishing settings: ${teamStore.presErr}. Showing the last answer.` }));
  p.append(el('p', { class: 'set-note tm-optin', text: teamText(v.opt_in_text, 900) || TEAM_OPT_IN }));
  p.append(el('p', { class: 'dim set-note', text: 'Off for every repository until you switch it on here, and separate from Shared with my team. Never published: titles, prompts, replies, transcripts, terminal text, paths, host names, account names, usage or cost.' }));
  const paused = v.paused === true;
  p.append(teamSwitch('data-pause', 'all', 'Pause team presence', 'Pause team presence', paused,
    paused ? 'On. This board writes nothing and takes its lines down within a minute. Your switches stay as they are.' : 'Off. Turn on to stop every heartbeat and take this board\'s lines down in all repositories, without losing your switches.',
    (on) => { teamPause(on); }, teamStore.presSaving['*'] === true));
  if (teamStore.presErrors['*']) p.append(el('p', { class: 'tm-err bad', role: 'alert', text: teamStore.presErrors['*'] }));
  const b = v.budget && typeof v.budget === 'object' ? v.budget : null;
  const hb = Array.isArray(v.heartbeat_s) && v.heartbeat_s.length === 2 && v.heartbeat_s.every(Number.isInteger) ? v.heartbeat_s.map((x) => Math.round(x / 60)) : null;
  if (b && [b.per_hour, b.spent_hour, b.board_share].every(Number.isInteger)) {
    const many = Number.isInteger(b.boards) && b.boards > 1 ? `, and ${b.boards} of your boards share it` : '';
    p.append(el('p', { class: 'dim set-note tm-budget', text: `Writes this hour: ${b.spent_hour} of ${b.per_hour} for this login (this board may use ${b.board_share}${many}). A line is rewritten when your state changes and ${hb ? `every ${hb[0]} to ${hb[1]} minutes` : 'every few minutes'} to show you are still here; it counts as stale after ${Number.isInteger(v.lease_min) ? v.lease_min : 30} minutes.` }));
  }
  const rows = teamPresBlocks();
  if (!rows.length) p.append(el('p', { class: 'dim set-note', text: 'No project of this board has a github.com remote, so there is nothing to publish.' }));
  for (const r of rows) p.append(teamPubRow(r, paused));
  const audit = (Array.isArray(v.audit) ? v.audit : []).filter((a) => a && typeof a === 'object' && ownKey(TEAM_AUDIT, a.action)).slice(0, 8);
  if (audit.length) {
    p.append(el('details', { class: 'tm-audit' }, el('summary', { class: 'tm-pub-s', text: 'Recent publishing events' }),
      el('ul', { class: 'tm-audit-l' }, audit.map((a) => el('li', { class: 'tm-audit-i' },
        el('span', { class: 'dim', text: teamClock(a.at) }), el('span', { text: ` ${TEAM_AUDIT[a.action]}` }), teamText(a.target, 100) ? el('span', { class: 'mono dim', text: ` ${teamText(a.target, 100)}` }) : null,
        a.status && a.status !== 'ok' ? el('span', { class: 'bad', text: ` ${teamText(a.status, 20)}` }) : null)))));
  }
}

/* ---------- Settings > Team (pages/settings.js settingsTeam) ---------- */

function teamKv(label, ...kids) {
  return el('div', { class: 'kv' }, el('b', { class: 'k', text: label }), el('div', { class: 'kv-main' }, kids), el('div', { class: 'kv-act hidden' }));
}

function teamHead(text) { return el('h2', { class: 'set-h', text }); }

function teamSettingsPanel(p) {
  const had = typeof document !== 'undefined' && document.activeElement && typeof document.activeElement.getAttribute === 'function'
    ? ['data-share', 'data-pub', 'data-priv', 'data-pause', 'data-confirm'].map((a) => [a, document.activeElement.getAttribute(a)]).find((x) => x[1]) : null;      // a switch keeps the focus through a repaint
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
  teamPresenceSection(p);
  p.append(teamHead('What it costs'));
  p.append(el('p', { class: 'dim set-note', text: 'Team reads are last in line for the 5,000 hourly GitHub requests of this login: at most 600 an hour in all. Issues and pull requests are read about every 2 minutes per repository (slower with many shared repositories, which share the 600), branch links every 10 minutes, and less often when few requests are left. With nothing shared the board asks GitHub for nothing. Publishing is counted apart: at most 240 writes an hour for the whole login, however many repositories and boards.' }));
  if (had && p.querySelector) { const again = p.querySelector(`[${had[0]}="${String(had[1]).replace(/[^A-Za-z0-9_./-]/g, '')}"]`); if (again && typeof again.focus === 'function') again.focus(); }
}

/* ---------- the demo (?demo=1): core.js demoApi calls this for /api/team/... ---------- */

const demoTeamMade = {};                                           // repo_slug -> the switch as the visitor left it
function demoTeamFlag() { try { const m = /[?&]team=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }

function demoTeamBlock(r, on) {
  const zero = { people: 0, teammates: 0, issues: 0, branches: 0, prs: 0, unattributed_branches: 0, live: 0 };
  if (on === r.shared) return r;
  if (!on) return { ...r, shared: false, state: 'off', message: 'Not shared with your team. Nothing is read from GitHub for this repository.', stale: false, read_at: null, age_s: null, gql_at: null, graph_pending: false, private: null, presence: null, warnings: null, free: null, people: [], counts: zero };
  return { ...r, shared: true, state: 'ok', message: '', wait_s: null, stale: false, read_at: new Date().toISOString(), age_s: 0, gql_at: new Date().toISOString(), graph_pending: false, presence: null, warnings: { api: 1, identity_known: true, issues: {}, branches: {}, tasks: {}, files: { checked: 0, pending: 0, capped: false } },
    free: { state: 'unread', reason: 'unread', message: 'The open issues of this repository have not been read yet, so free issues are not listed.', held: [], issues: [], count: 0, truncated: false }, people: [], counts: zero };
}

const demoPresMade = { repos: {}, paused: null };              // repo_slug -> {publish, private, ok} as the visitor left it; the global pause
function demoPresFlag() { try { const m = /[?&]presence=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }

/* The literal body of a repository that has no work item: the marker and this board's one line, as app/team.py presence_body writes it. */
function demoPresBody(slug) {
  const now = Date.now();
  const at = new Date(now).toISOString().slice(0, 19) + 'Z';
  const since = new Date(now - 7200000).toISOString().slice(0, 16) + 'Z';
  const last = new Date(Math.floor(now / 900000) * 900000).toISOString().slice(0, 16) + 'Z';
  return `<!-- ccboard:presence v1 -->\nccboard presence: node=node-a nid=1a2b3c4d repo=${String(slug).toLowerCase()} seq=1 at=${at} lease=30m state=idle agent=claude since=${since} last=${last}`;
}

/* The times inside a fixture body move with the clock like every other time of the fixture (demoRebase does not look inside text). */
function demoPresShift(text, epoch) {
  if (typeof text !== 'string' || !epoch) return text;
  const dt = Math.floor(Date.now() / 1000 - (typeof epoch === 'number' ? epoch : Date.parse(epoch) / 1000));
  return text.replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z/g, (m) => {
    const short = m.length === 17;
    const iso = new Date(Date.parse(short ? m.slice(0, 16) + ':00Z' : m) + dt * 1000).toISOString();
    return short ? iso.slice(0, 16) + 'Z' : iso.slice(0, 19) + 'Z';
  });
}

/* The block of a repository the visitor switched: what app/team_presence.py block() answers after the same switch. */
function demoPresApply(b, m, pf) {
  const msg = (name) => pf.variants[name].repo.message;
  const none = { published: false, last_body: null, last_at: null };
  if (!m.publish) return { ...b, publish: false, private: false, state: 'off', message: msg('off'), issue: null, issue_url: null, public_confirmed: false, ...none };
  if (m.private) return { ...b, publish: true, private: true, state: 'private', message: msg('private'), public_confirmed: m.ok === true, ...none };
  const n = b.issue || 1;
  return { ...b, publish: true, private: false, state: 'publishing', message: '', public_confirmed: m.ok === true || b.public_confirmed === true, issue: n, issue_url: `https://github.com/${b.slug}/issues/${n}`,
    published: true, last_body: b.last_body || demoPresBody(b.slug), last_at: b.last_at || new Date().toISOString() };
}

function demoPresBuild(fx) {
  const pf = fx.presence;
  const flag = demoPresFlag();
  const v = flag && ownKey(pf.variants || {}, flag) ? pf.variants[flag] : null;
  const epoch = fx.demo && fx.demo.epoch;
  const paused = demoPresMade.paused === null ? !!(v && v.body && v.body.paused === true) : demoPresMade.paused;
  const overlay = !!(v && v.repo) && !(v.body && v.body.paused === true && demoPresMade.paused !== null);
  const repos = pf.base.repos.map((b) => (overlay && b.repo_slug === 'github.com/example/website' ? { ...b, ...v.repo } : b))
    .map((b) => (b.last_body ? { ...b, last_body: demoPresShift(b.last_body, epoch) } : b))
    .map((b) => (ownKey(demoPresMade.repos, b.repo_slug) ? demoPresApply(b, demoPresMade.repos[b.repo_slug], pf) : b))
    .map((b) => (paused && b.publish && b.state !== 'off' ? { ...b, state: 'paused', message: pf.variants.paused.repo.message, published: false, last_body: null, last_at: null } : b));
  return { ...pf.base, ...((v && v.body) || {}), paused, repos };
}

/* The one list of the whole team (issue #200) for the demo: what app/team.py overview() makes of the blocks, so a variant (?team=sso, a repository switched on or off) shows the screens of
   that state. A port of the model, held to it by tests (tests/test_team_overview.py makes the answers from the real model for every variant, tests/js/team-view.test.mjs compares). */
const DEMO_ROW_STATES = ['working', 'waiting', 'idle', 'unconfirmed', 'stale', 'ended', 'unknown', 'github'];
function demoRowState(word, pstate) {
  if (word === null || word === undefined) return 'github';
  if (pstate === 'ended' && ['live', 'idle', 'unconfirmed', 'stale'].includes(word)) return 'ended';
  if (word === 'live') return pstate === 'running' ? 'working' : 'waiting';
  return ['idle', 'unconfirmed', 'stale', 'unknown'].includes(word) ? word : 'stale';
}
function demoRowsOf(b, p, pk) {
  const [so, sn] = b.slug.split('/');
  const stale = !!b.stale;
  const base = { person: pk, login: p.login, you: !!p.you, repo_slug: b.repo_slug, slug: b.slug, repo_url: b.url, read_at: b.read_at, stale };
  const blank = (kind, kw) => ({ ...base, kind, issue: null, issue_url: null, title: null, branch: null, branch_url: null, pr: null, pr_url: null, draft: null, agent: null, state: 'github', live: null, since: null,
    last: null, last_seen_s: null, claim: null, collides: false, ...kw });
  const gh = `https://github.com/${so}/${sn}`;
  const issues = new Map(p.issues.map((i) => [i.number, i]));
  const prsFor = new Map();
  for (const pr of p.prs) for (const c of pr.closes || []) if (!prsFor.has(c)) prsFor.set(c, pr);
  const usedIssue = new Set(); const usedBranch = new Set(); const usedPr = new Set();
  const out = [];
  const attach = (row, n) => {
    const pr = n ? prsFor.get(n) : null;
    if (!pr) return;
    Object.assign(row, { pr: pr.number, pr_url: pr.url, draft: pr.draft });
    row.collides = row.collides || !!pr.collides;
    usedPr.add(pr.number);
    if (pr.head) usedBranch.add(pr.head);
  };
  const claimOf = (it) => (it && it.claim ? { status: it.claim.status, live: it.claim.live } : null);
  const rank = { live: 0, idle: 1, unconfirmed: 2, stale: 3, unknown: 4, gone: 5 };
  const seenBoard = new Set();
  const boards = [...((p.presence && p.presence.boards) || [])].sort((x, y) => (rank[x.live] ?? 9) - (rank[y.live] ?? 9) || (x.last_seen_s ?? 1e9) - (y.last_seen_s ?? 1e9));
  for (const bd of boards) {
    const k = `${bd.issue}|${bd.branch}`;
    if (seenBoard.has(k)) continue;
    seenBoard.add(k);
    const word = stale && ['live', 'idle', 'unconfirmed'].includes(bd.live) ? 'stale' : bd.live;
    const it = bd.issue ? issues.get(bd.issue) : null;
    const row = blank('presence', { issue: bd.issue, issue_url: bd.issue_url, title: it ? it.title : null, branch: bd.branch, branch_url: bd.branch_url, agent: bd.agent, state: demoRowState(word, bd.state), live: word,
      since: bd.since, last: bd.last, last_seen_s: bd.last_seen_s, claim: claimOf(it), collides: !!(it && it.collides && it.collides.length) });
    if (it) { usedIssue.add(it.number); for (const x of it.branches) usedBranch.add(x); }
    if (bd.branch) usedBranch.add(bd.branch);
    attach(row, bd.issue);
    out.push(row);
  }
  for (const it of p.issues) {
    if (usedIssue.has(it.number)) continue;
    const br = it.branches.length ? it.branches[0] : null;
    const row = blank('issue', { issue: it.number, issue_url: it.url, title: it.title, branch: br, branch_url: br ? `${gh}/tree/${br}` : null, last: it.updated_at, claim: claimOf(it), collides: !!(it.collides && it.collides.length) });
    usedIssue.add(it.number);
    for (const x of it.branches) usedBranch.add(x);
    attach(row, it.number);
    out.push(row);
  }
  for (const pr of p.prs) {
    if (usedPr.has(pr.number)) continue;
    const n = pr.closes && pr.closes.length ? pr.closes[0] : null;
    out.push(blank('pr', { issue: n, issue_url: n ? `${gh}/issues/${n}` : null, branch: pr.head, branch_url: pr.head ? `${gh}/tree/${pr.head}` : null, pr: pr.number, pr_url: pr.url, title: pr.title, draft: pr.draft,
      last: pr.updated_at, collides: !!(pr.collides && pr.collides.length) }));
    usedPr.add(pr.number);
    if (pr.head) usedBranch.add(pr.head);
  }
  for (const br of p.branches) {
    if (usedBranch.has(br.name)) continue;
    usedBranch.add(br.name);
    out.push(blank('branch', { issue: br.issue, issue_url: br.issue ? `${gh}/issues/${br.issue}` : null, branch: br.name, branch_url: br.url, last: br.at, collides: !!(br.collides && br.collides.length) }));
  }
  if (!out.length) out.push(blank('seen', { last: p.last_active }));
  return out;
}
function demoOverview(repos) {
  const rank = { live: 0, idle: 1, unconfirmed: 2, stale: 3, unknown: 4, gone: 5 };
  const shared = repos.filter((b) => b.shared);
  const ids = new Map();
  for (const b of shared) for (const p of b.people || []) if (p.id !== null && p.id !== undefined && p.login) ids.set(p.login.toLowerCase(), p.id);
  const rows = []; const people = new Map(); const livePeople = new Set();
  let activeRepos = 0;
  for (const b of shared) {
    const counted = b.state === 'ok' && !b.stale;
    let repoLive = false;
    for (const p of b.people || []) {
      const uid = p.id !== null && p.id !== undefined ? p.id : (p.login ? ids.get(p.login.toLowerCase()) : undefined);
      const pk = uid !== null && uid !== undefined ? `id:${uid}` : `login:${(p.login || '').toLowerCase()}`;
      const mine = demoRowsOf(b, p, pk);
      rows.push(...mine);
      if (!people.has(pk)) people.set(pk, { key: pk, id: uid === undefined ? null : uid, login: p.login, you: false, live: 'none', last_seen_s: null, repos: [], rows: 0 });
      const ent = people.get(pk);
      ent.you = ent.you || !!p.you;
      ent.rows += mine.length;
      if (!ent.repos.includes(b.slug)) ent.repos.push(b.slug);
      let best = null;
      const better = (r, o) => o === null || ((rank[r.live] ?? 9) - (rank[o.live] ?? 9) || (r.last_seen_s ?? 1e9) - (o.last_seen_s ?? 1e9)) < 0;
      for (const r of mine) if (r.live !== null && better(r, best)) best = r;
      if (best && (rank[best.live] ?? 9) < (rank[ent.live] ?? 9)) { ent.live = best.live; ent.last_seen_s = best.last_seen_s; }
      if (counted && !p.you && mine.some((r) => r.live === 'live' || r.live === 'idle')) { livePeople.add(pk); repoLive = true; }
    }
    if (repoLive) activeRepos += 1;
  }
  const secs = (v) => { const t = Date.parse(v || ''); return Number.isNaN(t) ? 0 : t / 1000; };
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
  rows.sort((a, b) => cmp(!a.you, !b.you) || DEMO_ROW_STATES.indexOf(a.state) - DEMO_ROW_STATES.indexOf(b.state) || secs(b.last) - secs(a.last) || cmp((a.login || '').toLowerCase(), (b.login || '').toLowerCase())
    || cmp(a.slug.toLowerCase(), b.slug.toLowerCase()) || (a.issue || 0) - (b.issue || 0) || cmp(a.branch || '', b.branch || '') || (a.pr || 0) - (b.pr || 0));
  const plist = [...people.values()].sort((a, b) => cmp(!a.you, !b.you) || (rank[a.live] ?? 9) - (rank[b.live] ?? 9) || cmp((a.login || '').toLowerCase(), (b.login || '').toLowerCase()) || (a.id || 0) - (b.id || 0));
  const frees = shared.map((b) => b.free).filter((f) => f && typeof f === 'object');
  const count = (s) => frees.filter((f) => f.state === s).length;
  return { api: 1, counts: { shared: shared.length, ok: shared.filter((b) => b.state === 'ok').length, loading: shared.filter((b) => b.state === 'loading').length,
    problem: shared.filter((b) => b.state !== 'ok' && b.state !== 'loading').length, teammates: [...people.values()].filter((e) => !e.you).length, live: livePeople.size, active_repos: activeRepos, rows: rows.length,
    free: { count: frees.filter((f) => f.state === 'ok').reduce((a, f) => a + f.count, 0), ok: count('ok'), unread: count('unread'), unknown: count('unknown') }, truncated: rows.length > 600 || plist.length > 200 },
  people: plist.slice(0, 200), rows: rows.slice(0, 600) };
}

async function demoTeam(method, path, body) {
  const bare = path.split('?')[0];
  await new Promise((resolve) => setTimeout(resolve, 60));
  const r = await fetch('/static/demo/team.json');
  if (!r.ok) throw new Error(`demo fixture team.json: ${r.status} ${r.statusText}`);
  const fx = demoRebase(await r.json());
  const flag = demoTeamFlag();
  const v = flag && ownKey(fx.variants || {}, flag) ? fx.variants[flag] : null;
  const lines = (b) => {                                           // a variant sets the live line of a person by login
    if (!v || !v.lines || !Array.isArray(b.people)) return b;
    const people = b.people.map((x) => (ownKey(v.lines, x.login) ? { ...x, presence: v.lines[x.login] } : x));
    return { ...b, people, counts: { ...b.counts, live: people.filter((x) => !x.you && x.presence && (x.presence.live === 'live' || x.presence.live === 'idle')).length } };
  };
  const build = () => {
    const now = Date.now();
    const repos = fx.base.repos.map((b) => (v && v.repo && b.repo_slug === 'github.com/example/website' ? { ...b, ...v.repo } : b))
      .map((b) => (v && v.others && ownKey(v.others, b.repo_slug) ? { ...b, ...v.others[b.repo_slug] } : b))
      .map((b) => (b.repo_slug === 'github.com/example/website' ? lines(b) : b))
      .map((b) => (ownKey(demoTeamMade, b.repo_slug) ? demoTeamBlock(b, demoTeamMade[b.repo_slug]) : b))
      .map((b) => (b.read_at ? { ...b, age_s: Math.max(0, Math.floor((now - Date.parse(b.read_at)) / 1000)) } : b));
    const overview = !v && !Object.keys(demoTeamMade).length && fx.base.overview ? fx.base.overview : demoOverview(repos);          // the fixture's own list while nothing is laid over it
    return { ...fx.base, ...((v && v.body) || {}), repos, overview, enabled: repos.some((b) => b.shared === true), at: new Date(now).toISOString() };
  };
  if (method === 'GET' && bare === '/api/team/state') return build();
  if (method === 'POST' && bare === '/api/team/refresh') return { queued: build().enabled };
  if (method === 'GET' && bare === '/api/team/presence') return demoPresBuild(fx);
  if (method === 'PUT' && bare === '/api/team/presence') {
    if (!body || typeof body.paused !== 'boolean') throw demoError(400, 'paused must be true or false');
    demoPresMade.paused = body.paused;
    return demoPresBuild(fx);
  }
  const mp = /^\/api\/team\/presence\/([^/]+)\/([^/]+)$/.exec(bare);
  if (method === 'PUT' && mp) {
    const slug = `${decodeURIComponent(mp[1])}/${decodeURIComponent(mp[2])}`.toLowerCase();
    const view = demoPresBuild(fx);
    const hit = view.repos.find((b) => String(b.slug).toLowerCase() === slug);
    if (!hit) throw demoError(404, `this board has no project for ${slug}`);
    if (!body || (typeof body.publish !== 'boolean' && typeof body.private !== 'boolean')) throw demoError(400, 'say publish or private');
    const cur = ownKey(demoPresMade.repos, hit.repo_slug) ? demoPresMade.repos[hit.repo_slug] : { publish: hit.publish === true, private: hit.private === true, ok: hit.public_confirmed === true };
    let next;
    if (body.publish === false) next = { publish: false, private: false, ok: false };
    else if (body.publish === true || (typeof body.private === 'boolean' && cur.publish)) {
      const ok = cur.ok === true || body.confirm_public === true;
      if (body.publish === true && hit.public !== false && !ok) {
        const e = demoError(409, 'This repository is public, or GitHub has not said it is private. Confirm to publish.');
        e.body = { error: e.message, needs_confirm: true, public_lists: view.public_lists, next_body: demoPresBody(hit.slug), opt_in_text: view.opt_in_text };
        throw e;
      }
      next = { publish: true, ok, private: typeof body.private === 'boolean' ? body.private : (cur.publish ? cur.private === true : false) };
    } else throw demoError(400, 'publishing is off for this repository: switch it on first');
    demoPresMade.repos[hit.repo_slug] = next;
    return demoPresBuild(fx).repos.find((b) => b.repo_slug === hit.repo_slug);
  }
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
  personNode: teamPersonNode, shareRow: teamShareRow, projectSection: teamProjectSection, settingsPanel: teamSettingsPanel, text: teamText, link: teamLink, ago: teamAgo,
  presenceLoad: teamPresenceLoad, publish: teamPublish, setPrivate: teamPrivate, pause: teamPause, liveWord: teamLiveWord,
  ensure: teamEnsure, launchView: teamLaunchView, launchLines: teamLaunchLines, launchPaint: teamLaunchPaint, issueChip: teamIssueChip, branchFor: teamBranchFor, who: teamWho, collideNode: teamCollideNode };
