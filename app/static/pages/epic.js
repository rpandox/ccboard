/* ccboard epic view (issue #147), a lazy bundle (lazy.js 'epic', with pages/epic.css): the page #/epic/<owner>/<repo>/<n>, the Epics list of the Tasks page, the epic rows of the
   command palette and the demo's answers for /api/epics. Classic script, definition-only at load: nothing here touches the DOM, the network or a timer until a page, the list
   or the demo asks (registerPage is the one call made at load).

   The page READS GitHub through the board (GET /api/epics, GET /api/epics/<owner>/<repo>/<n>) and never writes to it.
   Routing (issues #150 and #152): Run (POST .../run) asks the board to send every ready issue to the node that can take it, one at a time, as lanes and rate-limit windows allow; the board
   chooses, the page only shows the choice and its reason ("chose node-b: 2 free lanes, 5-hour window 31 percent") or "waiting: <reason> until <time>" per issue (the answer's `route`, and
   `route` on each child). Pause and Resume (POST .../pause, .../resume) stop and continue new assignments. A row's "Run on..." menu sends that one issue to a node or to Auto (POST
   .../issues/<n>/run {node}). Run is the one filled primary and is off, with its reason, when GitHub writes are off, no node is paired or the epic is paused. The settings also hold the
   window limit, one hand-made group of nodes on one subscription and "Let MCP run this epic" (mcp_run, off by default, set only here by a person).
   It writes three things: this board's own settings, PUT /api/epics/<owner>/<repo>/<n>/settings {pins, epic_pin, report, auto_pr, close, max_window_pct, group, mcp_run}, and, only when the board says
   GitHub writes are on (CCBOARD_EPIC_WRITES), a person's ask to a paired node to claim one issue (POST /api/nodes/<handle>/epics/claim {repo, issue, epic}, issue #148) or to
   work one: claim it, start it as a task on the node and report on the issue (POST .../issues/<n>/work {node, repo, force?}, issues #149 and #150: the board passes the node through the
   same hard filters as Run first and says why in the same words, and each open child carries `work_refused`, the nodes that fail one, so the action is disabled with the reason). The node does the
   claiming and the work; this page paints the ask at once and shows the node's answer in plain words (taken, lost, claim not known, writes off, closed, attempts used up,
   offline, scope, unconfirmed, and for work also: not cloned there, author not allowed, bypass refused, lane cap). It also shows this board's own claims (held, renewing, lost)
   from the answer's `own_claims`, and says once, with a toast, that one was lost. The settings (what a node posts: phases only, summary or none; pull request; Closes) start off
   and are shown, with one paragraph in words, only when GitHub writes are on and a node is paired. An issue by an author who is not on the allowed list is marked, and its Work
   action is disabled and names the author (the node checks again; this page only says so first).

   Everything GitHub sent (titles, labels, claim aliases) is untrusted text written by other people: it goes into the page through textContent only, and a link is drawn only
   for an address that is exactly https://github.com/<owner>/<repo>/(issues|pull)/<n> (the board builds them from validated parts; this file checks again).
   Times from GitHub are shown relative to GitHub's own clock (the answer's server_now); the browser's clock decides nothing here. */
'use strict';

const EP_POLL_MS = 30000;          // the page asks again this often while it is visible (the board answers from its own 60 s / 30 s caches and ETags, so most asks cost GitHub nothing)
const EP_LIST_POLL_MS = 60000;
const EP_LIST_PARTIAL_MS = 11000;  // a list that stopped at its call budget is asked again after this (the board reuses it for 10 s, then carries on)
const EP_WAIT_MAX_MS = 65000;
const EP_LINK_RE = /^https:\/\/github\.com\/[A-Za-z0-9-]{1,39}\/[A-Za-z0-9_.-]{1,100}\/(?:issues|pull)\/[1-9][0-9]{0,8}$/;
const EP_STATE = { done: ['✓', 'done'], running: ['✽', 'running'], queued: ['∙', 'queued'], failed: ['✕', 'failed'], open: ['○', 'open'], closed: ['✓', 'closed'], unknown: ['?', 'claim not known'] };
const EP_PHASE = { backlog: 'in the backlog', queued: 'queued', running: 'running', done: 'done', failed: 'failed', cancelled: 'cancelled', unknown: 'in an unknown phase' };
const EP_CLAIM = { claimed: 'claimed', running: 'running', done: 'done', failed: 'failed', released: 'released', cancelled: 'cancelled' };
const EP_NEXT = {                  // what to do next, by the reason the board gives (the board's own sentence comes first)
  no_gh: 'Install the GitHub CLI (gh) on this board, then run gh auth login.',
  no_login: 'Run gh auth login on this board, then reload this page.',
  no_access: 'Check that the gh login on this board can read this repository (gh auth status).',
  not_found: 'Check the address, or that the gh login on this board can see this issue.',
  rate_limited: 'This page asks again by itself once the time is up. Nothing is lost.',
  timeout: 'GitHub is slow to answer. This page asks again by itself.',
  unavailable: 'GitHub is not answering right now. This page asks again by itself.',
  bad_answer: 'GitHub sent an answer this board could not read. This page asks again by itself.',
  too_large: 'GitHub sent more than this board reads at once.',
};
const EP_TRUNCATED = 'This epic has more issues than shown: only the first 100 are read, so the numbers here are a floor.';
const EP_AUTHORS_NEXT = 'Set CCBOARD_EPIC_AUTHORS, or log gh in on this board. Until then no claim is read and no issue is shown as free.';
const EP_UNKNOWN_HINT = 'Not every comment of these issues could be read, so a claim may exist. Only "Unclaimed" means free.';
const EP_EMPTY = 'This epic has no issues yet. Add sub-issues on GitHub.';
const EP_RUN_IDLE = 'Run sends each ready issue to the node that has a free lane and room in the agent\'s rate-limit window, one at a time. A node that cannot take it is skipped and the reason is shown. Headroom is an estimate: an agent can still reach its limit.';
const EP_RUN_PAUSED = 'Run is off while this epic is paused. Resume first.';
const EP_RUN_NONE = 'Run needs a board that can route issues. Update this board.';
const EP_RUN_ENDED = { finished: 'finished: nothing is left to send', limit: 'stopped: the number of issues the agent was allowed ran out', closed: 'stopped: the epic is closed', stopped: 'stopped: the epic or its repository is not available to this board' };
const EP_ROUTE_POLL_MS = 10000;     // while a run is going the page asks this often, so the choices and waits show soon
const EP_RUNON_TITLE = 'Send this one issue to a node you choose, or to Auto. The board checks the same things as Run: online, the repository, the agent, the window and a free lane.';
const EP_MCP_HINT = 'An agent with the tasks scope may start this epic\'s run with the epic_run tool: up to 10 issues a call and 6 calls an hour, each recorded with the token\'s name. It cannot change settings. Off by default.';
const EP_WINDOW_HINT = 'Auto routing skips a node whose rate-limit window for the issue\'s agent is at or above this percent. A window that is not known counts as 50. Headroom is an estimate.';
const EP_GROUP_HINT = 'Two nodes logged in to one subscription share its rate-limit window. Tick the nodes that do, so the board counts that window once.';
const EP_PAUSE_WHY = 'Pause stops this board routing the epic. Nothing changes on GitHub.';
const EP_KEY = 'ccboard:epics';
const EP_LOST_KEY = 'ccboard:epic-lost';
const EP_WRITES_OFF = 'GitHub writes are off on this board (CCBOARD_EPIC_WRITES)';
const EP_WRITES_OFF_NEXT = 'Claiming comments on the issue, assigns the node and labels it. Set CCBOARD_EPIC_WRITES=1 on the box to allow it; until then this board only reads GitHub.';
const EP_CLAIM_TITLE = 'The node posts a claim comment on the issue, assigns itself and labels it ccboard:in-progress. Nothing else changes on GitHub.';
const EP_LOST_WHY = {              // why this board's own claim was lost (the node's word -> plain words)
  not_first: 'another node was first', expired: 'its lease ran out', deleted: 'the claim comment was deleted', not_ours: 'the claim comment is no longer this board\'s', unparseable: 'the claim comment was edited',
  edited: 'the claim comment was edited', not_counted: 'the claim comment no longer counts', status_changed: 'its status was changed on GitHub',
};
const EP_GH = 'https://github.com/';    // the demo and the blank page build the epic's own address from the route's checked parts; every other link is the board's

/* Work (issue #149): the phases a node's work on one issue shows, the three per-epic settings (all off) and the words for them. */
const EP_WORK_PHASE = {            // phase -> [glyph, words]; state is never colour alone
  running: ['✽', 'running'], waiting: ['✻', 'waiting for a person'], pr: ['○', 'pull request opened'], done: ['✓', 'done'], failed: ['✕', 'failed'], cancelled: ['∙', 'cancelled'], lost: ['⚠', 'claim lost'],
  queued: ['∙', 'queued'],
};
const EP_REPORTS = [['phase', 'Phases only'], ['summary', 'Summary'], ['none', 'None']];
const EP_PLAN = {                  // the same sentences the board gives as `plan` (app/nodes_epics.py work_plan); this page paints them at once, before the board answers
  report: {
    phase: 'Posts short phase comments on each issue: waiting for a person, pull request opened, done or failed. No result text leaves the node.',
    summary: 'Posts the phase comments and the first 1500 characters of each result, after home folders and anything shaped like a token are removed.',
    none: 'Posts no phase comment and no result. The claim comment and a refusal still appear.',
  },
  auto_pr: { on: 'Pushes the task\'s own branch and opens a pull request when a task is done.', off: 'Pushes nothing and opens no pull request.' },
  close: { on: 'The pull request says Closes #N.', needs_pr: 'Close is on, but it only applies with pull requests on.', off: 'Nothing says Closes #N.' },
};
const EP_WORK_TITLE = 'The node claims the issue, starts it as a task in its own worktree with the issue\'s own model line, and keeps the issue informed. Nothing is pushed unless pull requests are on.';
const EP_NOT_ALLOWED = 'who is not on the allowed list';

const epicPage = { cur: null, ids: 0 };
const Epics = { list: null, known: [], at: 0, pending: null, host: null };

/* ---------- small pure helpers ---------- */

function epLink(url, text, cls, title) {
  if (typeof url === 'string' && EP_LINK_RE.test(url)) return el('a', { class: cls || null, href: url, target: '_blank', rel: 'noopener noreferrer', title: title || null, text });
  return el('span', { class: cls || null, text });
}

function epAgo(s) {
  if (typeof s !== 'number' || !Number.isFinite(s)) return '';
  s = Math.max(0, s);
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

/* 14:10, the clock time of an ISO time on this device; '' when it is none. */
function epClock(iso) {
  const t = typeof iso === 'string' ? Date.parse(iso) : NaN;
  if (Number.isNaN(t)) return '';
  const d = new Date(t);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/* How long before GitHub's own "now" an ISO time was; '' when either is missing. */
function epSince(iso, nowIso) {
  const a = Date.parse(iso);
  const b = Date.parse(nowIso);
  return Number.isNaN(a) || Number.isNaN(b) ? '' : epAgo((b - a) / 1000);
}

function epNum(v) { return typeof v === 'number' && Number.isFinite(v) ? v : 0; }

/* The whole epic address as text: owner/repo#n. */
function epRef(owner, repo, n) { return `${owner}/${repo}#${n}`; }

Epics.hash = function (owner, repo, n) {
  try { return buildHash('epic', { owner, repo, n }); } catch (_) { return null; }
};

function epRemember() { try { localStorage.setItem(EP_KEY, '1'); } catch (_) { /* storage may be unavailable */ } }

/* ---------- the node chip: the registry's chip when the hub view is on, else the same markup from the name ---------- */

function epChip(handle, name, title, glyph) {
  const hub = typeof Nodes !== 'undefined' && typeof Nodes.enabled === 'function' && Nodes.enabled() && typeof Nodes.chip === 'function';
  if (hub && handle) {
    const c = Nodes.chip(handle === 'local' ? null : handle);
    if (c) return c;
  }
  const n = String(name || handle || '?');
  return el('span', { class: handle === 'local' ? 'nchip local' : 'nchip', 'data-node': handle || null, title: title || n },
    el('span', { class: 'nchip-m', 'aria-hidden': 'true', text: (n.replace(/[^A-Za-z0-9]/g, '').slice(0, 2) || '?').toUpperCase() }), el('span', { class: 'nchip-n', text: n }),
    glyph ? el('span', { class: 'nchip-g', 'aria-hidden': 'true', text: glyph }) : null);
}

/* ---------- this board's own claims and the ask to claim (issue #148) ---------- */

function epOwn(data, c) {
  const slug = String(c.repo || '').toLowerCase();
  return (data.own_claims || []).find((o) => o && String(o.repo || '').toLowerCase() === slug && o.issue === c.number) || null;
}

function epOwnActive(o) { return !!o && (o.state === 'held' || o.state === 'claiming'); }

/* One sentence for this board's own claim record. `writes` is the board's switch: a held claim is not renewed while it is off. */
function epOwnText(o, writes) {
  if (!o) return '';
  if (o.state === 'lost') {
    const why = ownKey(EP_LOST_WHY, o.lost_reason) ? EP_LOST_WHY[o.lost_reason] : 'it changed on GitHub';
    return `This board lost its claim: ${why}. It writes nothing more for it.${o.task_id ? ` Its task ${o.task_id} is marked lost-claim; its worktree is kept.` : ''}`;
  }
  if (o.state === 'claiming') return 'This board is claiming this issue';
  if (writes === false) return 'This board holds this claim, but GitHub writes are off, so it is not renewed';
  const n = epNum(o.renewals);
  const word = o.status === 'running' ? 'This board is running this issue and renews its claim' : 'This board holds this claim and renews it';
  return n > 0 ? `${word} (renewed ${n} ${n === 1 ? 'time' : 'times'})` : word;
}

/* The node a claim on this row would go to: the issue's pin, else the epic's pin; never this board (a claim is asked of a paired node), never a stale pin. */
function epClaimNode(data, c) {
  const pin = epPinOf(data, c);
  const ep = data.epic_pin && !data.epic_pin.stale ? data.epic_pin : null;
  const handle = pin && !pin.stale ? pin.handle : (ep ? ep.handle : null);
  if (!handle || handle === 'local') return null;
  return (data.nodes || []).find((x) => x.handle === handle) || null;
}

/* Would a claim be offered on this row if nothing stood in the way: the issue is open, its claim is known to be none, and this board holds no claim of it. */
function epClaimable(data, c) {
  return c.state === 'open' && c.claim_state === 'none' && !c.claim && !epOwnActive(epOwn(data, c));
}

/* The plain sentence for a refused or failed claim. `e` is what api() threw: e.status and e.body {reason, why, winner, error}. */
function epClaimSentence(e, name, n, work) {
  const b = e && e.body && typeof e.body === 'object' ? e.body : {};
  const who = typeof b.winner === 'string' && b.winner ? b.winner : 'Another node';
  const issue = `#${n}`;
  const byWhy = {
    taken: `${who} already holds ${issue}. Nothing was claimed.`,
    lost: `${who} was first on ${issue}, so ${name} withdrew its claim.`,
    claim_unknown: `${name} could not tell who holds ${issue} (claim not known), so it claimed nothing.`,
    writes_off: `GitHub writes are off on ${name} (CCBOARD_EPIC_WRITES). Nothing was claimed.`,
    closed: `${issue} is closed. Nothing was claimed.`,
    done: `${issue} is already done. Nothing was claimed.`,
    released: `${name} no longer holds ${issue}. Look at the issue on GitHub.`,
    attempts_exhausted: `Three attempts on ${issue} ended without a result. ${name} asked for a person and stopped.`,
    not_epic_child: `${name} does not take ${issue} as an issue of this epic. Nothing was claimed.`,
    other_repo: `${name} does not have this repository as one of its projects. Nothing was claimed.`,
    authors_unknown: `${name} cannot tell whose claims count, so it claimed nothing.`,
    login_not_allowed: `The gh login of ${name} is not one whose claims count. Nothing was claimed.`,
    no_login: `gh is not logged in on ${name}. Nothing was claimed.`,
    no_access: `The gh login of ${name} cannot push to that repository, so a claim from it would not count. Nothing was claimed.`,
    busy: `${name} is busy with other claims right now.${Number.isInteger(b.claims_ahead) ? ` ${b.claims_ahead} claims ahead.` : ''} Try ${issue} again${Number.isInteger(b.wait_s) ? ` in about ${b.wait_s} s` : ' in a moment'}. Nothing was claimed.`,
    too_many_claims: `${name} already holds as many claims as it keeps.`,
    rate_limited: `GitHub asked ${name} to wait${typeof b.wait_s === 'number' ? ` ${b.wait_s} s` : ''}. Try ${issue} again then.`,
    outcome_unknown: `GitHub did not say whether the claim on ${issue} was written. ${name} looks before it writes again; check the issue.`,
    withdraw_failed: `${who} was first on ${issue}, and ${name} could not remove its own comment yet. It will try again.`,
    error: `${name} could not claim ${issue}. Nothing was claimed.`,
    bad_request: `${name} could not claim ${issue}. Nothing was claimed.`,
  };
  const byReason = {
    offline: `${name} is offline. Nothing was claimed.`,
    scope: `${name} is paired without the tasks scope. Nothing was claimed.`,
    unconfirmed: work ? `Outcome unknown, check ${name}: it did not answer in time, so it is not known whether it started ${issue}. Look at its tasks and the issue on GitHub. Nothing was retried.`
      : `Outcome unknown, check ${name}: it did not answer in time, so it is not known whether ${issue} was claimed. Look at the issue on GitHub. Nothing was retried.`,
    needs_repair: `${name} no longer takes this board's token: pair it again in Settings. Nothing was claimed.`,
    unknown_node: `${name} is no longer paired. Nothing was claimed.`,
    unreachable: `${name} could not be reached. Nothing was claimed.`,
    human_only: 'Only a signed-in person can claim an issue.',
    forbidden: 'Only a signed-in person can claim an issue.',
    rate_limited: 'Too many requests to the nodes from this board. Try again in a minute.',
  };
  if (b.reason === 'claim_refused' && ownKey(byWhy, b.why)) return byWhy[b.why];
  if (ownKey(byReason, b.reason)) return byReason[b.reason];
  if (e && e.status === 503) return byReason.offline;
  return `${name} did not ${work ? 'start' : 'claim'} ${issue}${e && e.message ? `: ${String(e.message).slice(0, 160)}` : ''}. Nothing was ${work ? 'started' : 'claimed'}.`;
}

/* The plain sentence for a refused Work (issue #149). `e` is what api() threw: a 404 repo_not_here, or a 409 work_refused with `why` (a word of the node's closed list) and a few facts
   (the winner's alias, the author's login). The words the claim shares are the claim's. */
function epWorkSentence(e, name, n) {
  const b = e && e.body && typeof e.body === 'object' ? e.body : {};
  const issue = `#${n}`;
  const author = typeof b.author === 'string' && b.author ? b.author : 'someone';
  const editor = typeof b.editor === 'string' && /^(?=.{1,39}$)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$/.test(b.editor) ? b.editor : 'someone';
  const own = {
    repo_not_here: `${name} does not have this repository cloned, so ${issue} was not claimed.`,
    author_not_allowed: `${issue} was written by ${author}, ${EP_NOT_ALLOWED}. Nothing was claimed.`,
    epic_author_not_allowed: `The epic was written by ${author}, ${EP_NOT_ALLOWED}. Nothing in it will run. Nothing was claimed.`,
    edit_not_allowed: `${issue} was edited by ${editor}, ${EP_NOT_ALLOWED}. Nothing was claimed. Add that login to CCBOARD_EPIC_AUTHORS, or have an allowed author copy the text into a new issue.`,
    epic_edit_not_allowed: `The epic was edited by ${editor}, ${EP_NOT_ALLOWED}. Nothing in it will run. Add that login to CCBOARD_EPIC_AUTHORS, or have an allowed author copy the text into a new issue.`,
    edit_unknown: `GitHub did not say who edited ${issue}, so nothing was claimed.`,
    epic_edit_unknown: 'GitHub did not say who edited the epic text, so nothing was claimed.',
    unreadable: `${name} could not read ${issue} or its epic from GitHub just now. Try again in a moment. Nothing was claimed.`,
    bypass_refused: `${issue} asks for bypass permissions. ${name} refused to run it, said so on the issue and released the claim.`,
    posture_refused: `${issue} asks for a permission setting that ${name} does not use for work from an issue. ${name} refused to run it and released the claim.`,
    lanes_full: `${name} already runs as many tasks as its lane cap advises. Nothing was claimed. You can start ${issue} anyway.`,
    launch_failed: `${name} could not start the task for ${issue} and released the claim.`,
  };
  if (b.reason === 'node_filtered' && typeof b.error === 'string' && b.error) return b.filter === 'lane' ? `${b.error}. You can start ${issue} anyway.` : `${b.error}. Nothing was claimed.`;       // the board's own routing filter, in its words
  if (b.reason === 'repo_not_here' || b.why === 'repo_not_here') return own.repo_not_here;
  if (b.reason === 'work_refused' && ownKey(own, b.why)) return own[b.why];
  return epClaimSentence({ status: e && e.status, message: e && e.message, body: b.reason === 'work_refused' ? { ...b, reason: 'claim_refused' } : b }, name, n, true);
}

/* The outcome of a write to a node is unknown (a timeout or a broken connection after the call went out, issue #153): the sentence says so, nothing is sent again, and this link is the way to
   look: the node's page on this board. Not an unknown outcome: ''. */
function epUnknown(e, node) {
  const b = e && e.body && typeof e.body === 'object' ? e.body : {};
  return b.outcome === 'unknown' || b.reason === 'unconfirmed' ? node.handle : '';
}
function epCheckLink(handle, name) {
  const h = Ref.hash(Ref.node(handle));
  return h ? el('a', { class: 'ep-check', href: h, 'aria-label': `Check ${name}: its tasks and sessions`, text: 'See its tasks' }) : null;
}

/* The bits of the claim UI of one row, added under the pin: the action when it is offered, else why not (writes off, offline, no scope, no pin), then the last refusal. */
function epClaimBits(P, c) {
  const d = P.data;
  const out = [];
  const key = `${c.repo}#${c.number}`;
  const note = P.notes.get(key);
  if (epClaimable(d, c)) {
    const node = epClaimNode(d, c);
    const busy = P.claiming.get(key) || (P.claimChecks.get(key) || {}).pending;
    if (d.writes === false && node) out.push(el('span', { class: 'ep-claimhint warn', 'data-claim-off': 'writes', text: EP_WRITES_OFF }));
    else if (d.writes === true && !node && (d.nodes || []).some((x) => x.handle !== 'local')) out.push(el('span', { class: 'ep-claimhint dim', 'data-claim-off': 'pin', text: 'Pin this issue to a paired node to claim it there.' }));
    else if (d.writes === true && node && node.status !== 'online') out.push(el('span', { class: 'ep-claimhint dim', 'data-claim-off': 'offline', text: `${node.name} is ${node.status}: it cannot claim.` }));
    else if (d.writes === true && node && node.tasks !== true) out.push(el('span', { class: 'ep-claimhint dim', 'data-claim-off': 'scope', text: `${node.name} is paired without the tasks scope.` }));
    else if (d.writes === true && node) {
      const wk = epWorkOffer(P, c);                                              // the row has one tinted action: Work when it can work, else the claim
      out.push(el('button', { class: wk && wk.ok ? 'small ep-claim-btn' : 'small primary tinted ep-claim-btn', type: 'button', 'data-claim': node.handle, title: EP_CLAIM_TITLE, 'aria-label': `Claim issue #${c.number} on ${node.name}`,
        disabled: !!busy || null, onclick: () => epClaim(P, c, node), text: busy ? `Claiming on ${node.name}…` : `Claim on ${node.name}` }));
    }
  }
  if (note) out.push(el('span', { class: 'ep-claimhint warn ep-claimnote', role: 'status', 'data-claim-note': note.why || 'error' }, note.text, note.check ? [' ', epCheckLink(note.check, note.name || note.check)] : null));
  return out;
}

/* Ask the paired node to claim the issue. The row shows "Claiming on <node>…" at once; the node's answer (or its refusal, in plain words) replaces it, and a refused claim puts
   the row back as it was. The next poll only confirms. */
function epClaim(P, c, node) {
  const key = `${c.repo}#${c.number}`;
  if (P.claiming.has(key) || (P.claimChecks.get(key) || {}).pending || epicPage.cur !== P) return null;
  const startedAt = Date.now();
  P.claiming.set(key, node.handle);
  P.claimChecks.delete(key);
  P.notes.delete(key);
  epPaint(P);
  const body = { repo: c.repo, issue: c.number, epic: epRef(P.owner, P.repo, P.n) };
  return api('POST', `/api/nodes/${encodeURIComponent(node.handle)}/epics/claim`, body).then((r) => {
    P.claiming.delete(key);
    if (epicPage.cur !== P) return;
    if (r && r.data && r.data.claimed === true) {
      const alias = (r.data.winner && String(r.data.winner)) || node.name;
      const kid = (P.data.children || []).find((x) => x.repo === c.repo && x.number === c.number);
      if (kid) {                                                                  // painted at once; the next poll reads GitHub and confirms
        kid.claim = { alias, node: node.handle, status: 'claimed', since: P.data.server_now, renewed_at: P.data.server_now, live: true, attempt: r.data.attempt || 1, seq: 1, task_ref: null, branch: null };
        kid.claim_state = 'claimed';
        kid.bucket = { node: alias, state: 'queued' };
      }
      toast(r.data.reason === 'already_held' ? `${node.name} already held #${c.number}` : `${node.name} claimed #${c.number}`);
    } else {
      P.notes.set(key, { why: 'error', text: epClaimSentence({ status: 502, body: {} }, node.name, c.number) });
    }
    epPaint(P);
    epFetch(P);
  }, (e) => {
    P.claiming.delete(key);
    if (epicPage.cur !== P) return;
    const text = epClaimSentence(e, node.name, c.number);
    P.notes.set(key, { why: (e && e.body && (e.body.why || e.body.reason)) || 'error', text, check: epUnknown(e, node), name: node.name });
    if (epUnknown(e, node)) P.claimChecks.set(key, { repo: c.repo, issue: c.number, node, pending: true, claimId: e.body.claim_id, startedAt });
    toast(text, { kind: 'bad' });
    epPaint(P);
    const b = e && e.body;
    if (b && b.reason === 'claim_refused' && (b.why === 'taken' || b.why === 'lost' || b.why === 'done' || b.why === 'closed')) epFetch(P);       // the thread changed: read it again
  });
}

function epClaimUnconfirmed(P, key, check) {
  P.claimChecks.delete(key);
  P.notes.set(key, { why: 'unknown', text: `The claim on #${check.issue} could not be confirmed. Look at the issue on GitHub before trying again.` });
}

async function epCheckClaims(P) {
  await Promise.all([...P.claimChecks].map(async ([key, check]) => {
    if (Date.now() - check.startedAt > 120000) { epClaimUnconfirmed(P, key, check); return; }
    if (check.reading) return;
    check.reading = true;
    try {
      const r = await api('GET', `/api/nodes/${encodeURIComponent(check.node.handle)}/epics/claim/status?repo=${encodeURIComponent(check.repo)}&issue=${check.issue}`);
      if (epicPage.cur !== P || P.claimChecks.get(key) !== check || !r || !r.data) return;
      const d = r.data;
      if (d.claim_id !== check.claimId) return;
      if (d.reason === 'unknown') { epClaimUnconfirmed(P, key, check); return; }
      if (d.reason === 'pending') return;
      if (d.claimed === true) {
        const c = (P.data.children || []).find((x) => x.repo === check.repo && x.number === check.issue);
        P.claimChecks.delete(key);
        P.notes.delete(key);
        if (c && c.state === 'open' && !c.claim) {
          const alias = d.winner || check.node.name;
          c.claim = { alias, node: check.node.handle, status: 'claimed', live: true, attempt: d.attempt || 1 };
          c.claim_state = 'claimed'; c.bucket = { node: alias, state: 'queued' };
        }
      } else {
        P.claimChecks.delete(key);
        P.notes.set(key, { why: d.reason, text: epClaimSentence({ body: { ...d, reason: 'claim_refused', why: d.reason } }, check.node.name, check.issue) });
      }
    } catch (_) { /* keep reading until expiry; a failed read never retries the write */ }
    finally { check.reading = false; }
  }));
}

/* ---------- Work: claim an issue, run it as a task on the node and report on the issue (issue #149) ---------- */

const EP_RETRY = ['failed', 'cancelled', 'released'];          // a claim that ended without a result leaves the issue open for another attempt (three at most; the node says when they are used up)

/* The node that would work this row: the one a claim would go to, else (an issue claimed by a paired node that has not started it, or one whose last attempt ended) the node that held
   the claim. Never this board: the hub refuses `local`, and a node is the one that clones the repository. */
function epWorkNode(d, c) {
  if (epClaimable(d, c)) return epClaimNode(d, c);
  const k = c.claim;
  if (c.state !== 'open' || !k || epOwnActive(epOwn(d, c))) return null;
  const held = k.status === 'claimed' && k.live !== false && !c.task;
  if (!held && !EP_RETRY.includes(k.status)) return null;
  return held ? (d.nodes || []).find((x) => x.handle === k.node && k.node !== 'local') || null : (epClaimNode(d, c) || (d.nodes || []).find((x) => x.handle === k.node && k.node !== 'local') || null);
}

/* What the row's Work action is: null (not offered), {ok, node} (a button that works), or {off, text, node} (a disabled button that says why). The node checks all of this again. */
function epEditText(c) {
  return c.edit_state === 'blocked' ? `Edited by ${c.edited_by || 'someone'}, ${EP_NOT_ALLOWED}. Work is blocked.` : 'GitHub did not say who edited the text. Work is blocked.';
}

/* The routing filter this node fails for this issue (issue #150): {code, text} from the board's own `work_refused`, else null. The free-lane count is not one that disables: Start anyway
   (force) is the person's override of the node's advice, and the board still refuses an unforced Work on a full node with the same words. A pin never lifts a filter. */
function epFilterOf(c, node) {
  const m = c.work_refused;
  if (!m || typeof m !== 'object' || !ownKey(m, node.handle)) return null;
  const f = m[node.handle];
  return f && typeof f.text === 'string' && f.text && f.code !== 'lane' ? { code: String(f.code || 'filter'), text: f.text.slice(0, 300) } : null;
}

function epWorkOffer(P, c) {
  const d = P.data;
  if (!d || c.state !== 'open' || typeof d.writes !== 'boolean') return null;
  if ((P.claimChecks.get(`${c.repo}#${c.number}`) || {}).pending) return null;
  const node = epWorkNode(d, c);
  if (!node) return null;
  const a = d.authors && typeof d.authors === 'object' ? d.authors : null;
  const who = typeof c.author === 'string' && c.author ? `written by ${c.author}, ${EP_NOT_ALLOWED}` : 'not written by an author on the allowed list';
  let off = null;
  let text = '';
  if (d.writes === false) { off = 'writes'; text = EP_WRITES_OFF; }
  else if (node.status !== 'online') { off = 'offline'; text = `${node.name} is ${node.status}: it cannot work.`; }
  else if (node.tasks !== true) { off = 'scope'; text = `${node.name} is paired without the tasks scope.`; }
  else if (epFilterOf(c, node)) { off = 'filter'; text = epFilterOf(c, node).text; }
  else if (a && a.epic_ok === false) { off = 'epic_author'; text = `The epic was written by ${a.epic || 'someone'}, ${EP_NOT_ALLOWED}. Nothing in it will run.`; }
  else if (d.epic && ['blocked', 'unknown'].includes(d.epic.edit_state)) { off = 'epic_edit'; text = `The epic: ${epEditText(d.epic)} Nothing in it will run.`; }
  else if (['blocked', 'unknown'].includes(c.edit_state)) { off = 'edit'; text = epEditText(c); }
  else if (c.author_ok === false) { off = 'author'; text = `#${c.number} was ${who}.`; }
  else if (a && Array.isArray(a.allowed) && !a.allowed.length) { off = 'authors_unknown'; text = 'This board cannot tell whose issues may run. Set CCBOARD_EPIC_AUTHORS, or log gh in on this board.'; }
  const retry = !!c.claim && EP_RETRY.includes(c.claim.status);
  return off ? { off, text, node, retry } : { ok: true, node, retry };
}

/* The bits of the Work UI of one row: the action (the row's one tinted action), or a disabled one that says why, then the last refusal and, for a full lane cap, the way to start anyway. */
function epWorkBits(P, c) {
  const out = [];
  const key = `${c.repo}#${c.number}`;
  const offer = epWorkOffer(P, c);
  const busy = P.working.get(key);
  const note = P.wnotes.get(key);
  if (busy) {                                                                    // the ask is out: the row is painted as held by the node, and the action says so (it cannot be tapped twice)
    out.push(el('button', { class: 'small primary tinted ep-work-btn', type: 'button', 'data-work': busy.node.handle, title: EP_WORK_TITLE, 'aria-label': `Starting issue #${c.number} on ${busy.node.name}`,
      disabled: true, text: `Starting on ${busy.node.name}…` }));
  } else if (offer && offer.ok) {
    out.push(el('button', { class: 'small primary tinted ep-work-btn', type: 'button', 'data-work': offer.node.handle, title: EP_WORK_TITLE, 'aria-label': `Work on issue #${c.number} on ${offer.node.name}`,
      onclick: () => epWork(P, c, offer.node, false), text: `${offer.retry ? 'Try again on' : 'Work on'} ${offer.node.name}` }));
  } else if (offer) {
    out.push(el('button', { class: 'small ep-work-btn', type: 'button', disabled: true, 'data-work-off': offer.off, title: offer.text, 'aria-label': `Work on issue #${c.number}: ${offer.text}`, text: 'Work' }));
    if (!epClaimable(P.data, c) || offer.off === 'filter' || offer.off === 'author' || offer.off === 'epic_author' || offer.off === 'authors_unknown' || offer.off === 'edit' || offer.off === 'epic_edit') out.push(el('span', { class: 'ep-claimhint warn', 'data-work-hint': offer.off, text: offer.text }));
  }
  if (note) {
    out.push(el('span', { class: 'ep-claimhint warn ep-claimnote', role: 'status', 'data-work-note': note.why || 'error' }, note.text, note.check ? [' ', epCheckLink(note.check, note.name || note.check)] : null));
    if (note.why === 'lanes_full' && offer && offer.ok && !busy) {
      out.push(el('button', { class: 'small ep-work-force', type: 'button', 'data-work-force': offer.node.handle, title: 'Start it although the node runs as many tasks as its lane cap advises',
        'aria-label': `Start issue #${c.number} on ${offer.node.name} anyway`, onclick: () => epWork(P, c, offer.node, true), text: 'Start anyway' }));
    }
  }
  return out;
}

/* The fields of a child that a Work paints over, so a refusal can put them back. */
function epWorkSnap(kid) {
  return { claim: kid.claim, claim_state: kid.claim_state, bucket: kid.bucket, task: kid.task, phase: kid.phase };
}

function epWorkPaint(P, kid, node) {
  const now = P.data.server_now;
  kid.claim = { alias: node.name, node: node.handle, status: 'claimed', since: now, renewed_at: now, live: true, attempt: (kid.claim && kid.claim.attempt) || 1, seq: 1, task_ref: null, branch: null };
  kid.claim_state = 'claimed';
  kid.bucket = { node: node.name, state: 'queued' };
  kid.task = { ref: null, phase: 'queued', node: node.handle, node_name: node.name, status: 'online', age_s: 0, updated_at: now };
  kid.phase = 'queued';
}

function epWorkKid(P, c) { return ((P.data && P.data.children) || []).find((x) => x.repo === c.repo && x.number === c.number) || null; }

const EP_WORKED_MS = 90000;           // how long the page keeps painting a started issue over a read that does not show the claim yet (the board's read of GitHub can be a minute old)

function epWorkRunning(P, kid, w) {
  const ref = w.ref;
  kid.claim = { ...(kid.claim || {}), status: 'running', task_ref: ref, branch: w.branch };
  kid.task = { ...(kid.task || {}), ref, phase: 'running' };
  kid.bucket = { node: w.node.name, state: 'running' };
  kid.phase = 'running';
}

/* A poll that lands while a Work is out (or just after it was answered, before the board's read of GitHub shows the claim) replaces the children: paint the ask over the new ones too,
   so the row does not flicker back to Unclaimed. A read that shows a claim ends it. */
function epWorkReapply(P) {
  const kids = (P.data && P.data.children) || [];
  const find = (key) => kids.find((x) => `${x.repo}#${x.number}` === key);
  for (const [key, w] of Array.from(P.working)) {
    const kid = find(key);
    if (!kid) continue;
    w.snap = epWorkSnap(kid);
    epWorkPaint(P, kid, w.node);
  }
  for (const [key, w] of Array.from(P.worked)) {
    const kid = find(key);
    if (!kid || kid.claim || kid.claim_state !== 'none' || Date.now() - w.at > EP_WORKED_MS) { P.worked.delete(key); continue; }
    epWorkPaint(P, kid, w.node);
    epWorkRunning(P, kid, w);
    if (w.route && !kid.route) kid.route = w.route;
  }
  for (const key of Array.from(P.rnotes.keys())) { const kid = find(key); if (!kid || kid.claim || kid.state === 'closed') P.rnotes.delete(key); }
}

/* Ask the paired node to work the issue. The row shows the node holding it at once ("Starting on <node>…", queued); the node's answer replaces it, a refusal (in plain words) puts the
   row back as it was. A full lane cap offers Start anyway, which sends force. The next poll only confirms. */
function epWork(P, c, node, force) {
  const key = `${c.repo}#${c.number}`;
  if (P.working.has(key) || epicPage.cur !== P) return null;
  const kid = epWorkKid(P, c);
  const w = { node, snap: kid ? epWorkSnap(kid) : null };
  P.working.set(key, w);
  P.wnotes.delete(key);
  P.notes.delete(key);
  if (kid) epWorkPaint(P, kid, node);
  epPaint(P);
  const body = { node: node.handle, repo: c.repo };
  if (force === true) body.force = true;
  const undo = () => { const k = epWorkKid(P, c); if (k && w.snap) Object.assign(k, w.snap); };
  // the board passes the node through the same hard filters as Run before it asks it (issue #150); the answer is the relay's {node, age, data}
  return api('POST', epPath(P, `/issues/${encodeURIComponent(c.number)}/work`), body).then((r) => {
    P.working.delete(key);
    if (epicPage.cur !== P) return;
    const d = r && r.data;
    if (d && d.claimed === true) {
      const k = epWorkKid(P, c);
      const done = { node, ref: typeof d.task_ref === 'string' ? d.task_ref : null, branch: typeof d.branch === 'string' ? d.branch : null, at: Date.now() };
      if (k) epWorkRunning(P, k, done);                                           // painted at once; the next poll reads GitHub and confirms
      P.worked.set(key, done);
      toast(d.reason === 'already_held' ? `${node.name} already held #${c.number} and started it` : `${node.name} started #${c.number}`);
    } else {
      undo();
      P.wnotes.set(key, { why: 'error', text: epWorkSentence({ status: 502, body: {} }, node.name, c.number) });
    }
    epPaint(P);
    epFetch(P);
  }, (e) => {
    P.working.delete(key);
    if (epicPage.cur !== P) return;
    undo();
    const text = epWorkSentence(e, node.name, c.number);
    const bb = e && e.body && typeof e.body === 'object' ? e.body : {};
    P.wnotes.set(key, { why: bb.reason === 'node_filtered' && bb.filter === 'lane' ? 'lanes_full' : (bb.why || bb.reason) || 'error', text, check: epUnknown(e, node), name: node.name });
    toast(text, { kind: 'bad' });
    epPaint(P);
    const b = e && e.body;
    if (b && b.reason === 'work_refused' && (b.why === 'taken' || b.why === 'lost' || b.why === 'done' || b.why === 'closed')) epFetch(P);       // the thread changed: read it again
  });
}

/* Where the task of this issue opens: its own page on the node it runs on (#/n/<node>/t/<id>), or this board's Tasks page for a task here. Null when the task is not known yet. */
function epTaskHash(c) {
  const t = c.task;
  const w = c.work;
  let id = null;
  if (t && typeof t.ref === 'string') { const m = /^(?:[a-z0-9-]+:)?([0-9]{1,12})$/.exec(t.ref); if (m) id = m[1]; }
  else if (w && Number.isInteger(w.task_id) && w.task_id > 0) id = String(w.task_id);
  const handle = t && t.node ? t.node : (w ? 'local' : null);
  if (!handle || !id) return null;
  if (handle === 'local') return '#/tasks';
  try { return buildHash('node-task', { node: handle, id }); } catch (_) { return null; }
}

/* Match the issue to its task on the same node, then use that session's permission entry. */
function epPermission(c) {
  const t = c.task;
  const w = c.work;
  const match = t && typeof t.ref === 'string' ? /^(?:[a-z0-9-]+:)?([0-9]{1,12})$/.exec(t.ref) : null;
  const id = match ? Number(match[1]) : w && w.task_id;
  const node = t && t.node ? t.node : w ? 'local' : null;
  if (!id || !node) return null;
  if (node === 'local') {
    const st = typeof state !== 'undefined' && state;
    const task = st && (st.tasks || []).find((x) => x.id === id);
    return task ? (st.pending_permissions || []).find((p) => p.tmux_name === task.tmux_name) || null : null;
  }
  if (typeof Nodes === 'undefined' || !Nodes.ready) return null;
  const task = Nodes.tasks(node).find((x) => Number(x.id) === id);
  return task ? Nodes.permFor(node, task.tmux) : null;
}

function epPermissionNode(p) {
  if (!p) return null;
  if (p.node) return nsPermNode(p);
  return el('div', { class: 'ep-permission' }, el('p', { text: p.summary }),
    el('p', { class: 'dim', text: (p.choices || []).map((c) => `${c.number}. ${c.label}`).join(' · ') }),
    el('button', { class: 'primary small', text: 'Allow', onclick: () => decide(p.id, 'allow') }),
    el('button', { class: 'small', text: 'Deny', onclick: () => decide(p.id, 'deny') }));
}

/* The phase of the work on one issue, from this board's own work record or, for work on a paired node, the phase that node reports for the task (task.epic_phase, which an older node
   does not send: then the task's own phase shows as "Task running on <node>"): a glyph and words, never colour alone. Null when neither exists. */
function epWorkPhaseNode(c) {
  let w = c.work;
  const t = c.task;
  if ((!w || typeof w !== 'object') && t && typeof t.epic_phase === 'string') w = { phase: t.epic_phase, pr_number: t.epic_pr };          // work on a paired node: the node's own phase (issue #149)
  if ((!w || typeof w !== 'object') && !epPermission(c)) return null;
  const p = epPermission(c) ? 'waiting' : ownKey(EP_WORK_PHASE, w.phase) ? w.phase : 'running';
  const [g, words] = EP_WORK_PHASE[p];
  const attn = p === 'waiting' || p === 'lost' || p === 'failed';
  const more = [];
  if (w && w.attempt > 1) more.push(`attempt ${w.attempt}`);
  if (w && typeof w.branch === 'string' && w.branch) more.push(`branch ${w.branch}`);
  if (p === 'pr' && w && w.pr_number) more.push(`PR #${w.pr_number}`);
  return el('span', { class: 'ep-phase' + (attn ? ' warn' : ''), 'data-phase': p, title: p === 'lost' ? 'The claim was lost. The node that lost it writes nothing more for it. The task is parked and its worktree is kept.' : null },
    el('span', { class: 'ep-wg', 'aria-hidden': 'true', text: g + ' ' }), `Work ${words}${more.length ? ' (' + more.join(', ') + ')' : ''}`);
}

/* ---------- the per-epic settings: what a node posts and pushes (all start off) ---------- */

function epReportWord(v) {
  if (v === true) return 'summary';
  return typeof v === 'string' && EP_REPORTS.some(([k]) => k === v) ? v : 'phase';
}

/* The paragraph under the settings: in words, what a node will post and push. */
function epPlanText(d) {
  const pr = d.auto_pr === true;
  return `${EP_PLAN.report[epReportWord(d.report)]} ${pr ? EP_PLAN.auto_pr.on : EP_PLAN.auto_pr.off} ${d.close === true ? (pr ? EP_PLAN.close.on : EP_PLAN.close.needs_pr) : EP_PLAN.close.off}`;
}

/* Shown only when the board may write to GitHub and a node is paired to do the work: with writes off, or no node, nothing of this appears. */
function epSettingsShown(d) {
  return d.writes === true && (d.nodes || []).some((n) => n.handle !== 'local');
}

function epSetReport(P, v) {
  if (!P.data || epReportWord(P.data.report) === v) return null;
  return epWrite(P, { report: v }, () => { P.data.report = v; });
}

/* The window limit: a whole number from 10 to 100, or empty for the board's own setting. Anything else is said in words and nothing is written. */
function epSetWindow(P, raw) {
  const d = P.data;
  if (!d) return null;
  const v = String(raw || '').trim();
  P.winErr = '';
  if (v === '') {
    if (d.max_window_pct_set !== true) { epPaint(P); return null; }
    return epWrite(P, { max_window_pct: null }, () => { d.max_window_pct_set = false; });
  }
  const n = Number(v);
  if (!/^[0-9]{1,3}$/.test(v) || !Number.isInteger(n) || n < 10 || n > 100) { P.winErr = 'Use a whole number from 10 to 100, or leave it empty for the board\'s setting.'; epPaint(P); return null; }
  if (d.max_window_pct_set === true && d.max_window_pct === n) { epPaint(P); return null; }
  return epWrite(P, { max_window_pct: n }, () => { d.max_window_pct = n; d.max_window_pct_set = true; });
}

/* One hand-made group of nodes on one subscription: tick two or more. One ticked node is not a group: nothing is written and the page says so. */
function epGroupNodes(d) { return (d.nodes || []).filter((n) => n.handle !== 'local'); }

function epSetGroup(P) {
  const d = P.data;
  if (!d) return null;
  const picked = epGroupNodes(d).filter((n) => P.groupBoxes.get(n.handle) && P.groupBoxes.get(n.handle).checked).map((n) => n.handle);
  P.groupDraft = new Set(picked);
  P.groupErr = '';
  if (picked.length === 1) { P.groupErr = 'A group needs two or more nodes. Tick another, or untick this one.'; epPaint(P); return null; }
  P.groupDraft = null;
  const next = picked.length ? [picked] : [];
  if (JSON.stringify(next) === JSON.stringify(d.group || [])) { epPaint(P); return null; }
  return epWrite(P, { group: next }, () => { d.group = next; });
}

function epSetFlag(P, key, on) {
  if (!P.data || (P.data[key] === true) === on) return null;
  return epWrite(P, { [key]: on }, () => { P.data[key] = on; });
}

function epCfgBuild(P) {
  const seg = el('div', { class: 'seg-ctl ep-report', role: 'group', 'aria-labelledby': 'ep-cfg-report', 'aria-describedby': 'ep-plan' },
    ...EP_REPORTS.map(([v, t]) => el('button', { class: 'seg-btn', type: 'button', 'data-report': v, 'aria-pressed': 'false', text: t, onclick: () => epSetReport(P, v),
      onkeydown: (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        const i = EP_REPORTS.findIndex(([k]) => k === v);
        const nx = EP_REPORTS[(i + (e.key === 'ArrowRight' ? 1 : EP_REPORTS.length - 1)) % EP_REPORTS.length][0];
        epSetReport(P, nx);
        const b = seg.querySelector(`[data-report="${nx}"]`);
        if (b) b.focus();
      } })));
  const flag = (key, label, hint) => {
    const box = el('input', { type: 'checkbox', 'data-flag': key, onchange: () => epSetFlag(P, key, box.checked) });
    return { box, row: el('label', { class: 'ep-check' }, box, el('span', { class: 'ep-check-t' }, el('b', { text: label }), el('span', { class: 'dim', text: hint }))) };
  };
  const pr = flag('auto_pr', 'Open a pull request when a task is done', 'Pushes only the task\'s own branch. Never main.');
  const cl = flag('close', 'Say Closes #N in the pull request', 'Needs pull requests on. Off by default.');
  P.cfgSeg = seg;
  P.cfgPr = pr.box;
  P.cfgClose = cl.box;
  P.cfgPlan = el('p', { class: 'ep-plan', id: 'ep-plan', 'aria-live': 'polite' });
  // routing (issues #150 and #152): the window limit, one group of nodes on one subscription, and the switch that lets an agent run this epic
  P.winIn = el('input', { type: 'text', inputmode: 'numeric', autocomplete: 'off', maxlength: '3', class: 'ep-win', 'data-window': '', 'aria-labelledby': 'ep-cfg-win', 'aria-describedby': 'ep-win-hint ep-win-err',
    onchange: () => epSetWindow(P, P.winIn.value), onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); epSetWindow(P, P.winIn.value); } } });
  P.winHint = el('p', { class: 'dim ep-cfg-hint', id: 'ep-win-hint' });
  P.winErrEl = el('p', { class: 'warn ep-cfg-hint', id: 'ep-win-err', role: 'alert' });
  P.groupHost = el('div', { class: 'ep-groupbox', role: 'group', 'aria-labelledby': 'ep-cfg-group', 'aria-describedby': 'ep-group-hint ep-group-err' });
  P.groupHint = el('p', { class: 'dim ep-cfg-hint', id: 'ep-group-hint', text: EP_GROUP_HINT });
  P.groupErrEl = el('p', { class: 'warn ep-cfg-hint', id: 'ep-group-err', role: 'alert' });
  const mcp = flag('mcp_run', 'Let MCP run this epic', EP_MCP_HINT);
  P.cfgMcp = mcp.box;
  P.groupBoxes = new Map();
  P.groupRow = el('div', { class: 'ep-cfg-col', 'data-cfg-group': '' }, el('span', { class: 'ep-cfg-l', id: 'ep-cfg-group', text: 'Nodes on one subscription' }), P.groupHost, P.groupHint, P.groupErrEl);
  return el('section', { class: 'ep-sec ep-cfg hidden', 'aria-label': 'When a node works an issue', 'data-cfg': '' },
    el('h2', { class: 'ep-h', text: 'When a node works an issue' }),
    el('div', { class: 'ep-cfg-row' }, el('span', { class: 'ep-cfg-l', id: 'ep-cfg-report', text: 'What it posts on the issue' }), seg),
    pr.row, cl.row, P.cfgPlan,
    el('h3', { class: 'ep-h ep-subh', text: 'Choosing a node' }),
    el('div', { class: 'ep-cfg-row' }, el('span', { class: 'ep-cfg-l', id: 'ep-cfg-win', text: 'Window limit, percent' }), P.winIn),
    P.winHint, P.winErrEl, P.groupRow,
    el('h3', { class: 'ep-h ep-subh', text: 'Agents' }),
    mcp.row);
}

function epCfgPaint(P) {
  const d = P.data;
  const on = epSettingsShown(d);
  P.cfg.classList.toggle('hidden', !on);
  if (!on) return;
  const r = epReportWord(d.report);
  for (const b of P.cfgSeg.querySelectorAll('.seg-btn')) b.setAttribute('aria-pressed', b.getAttribute('data-report') === r ? 'true' : 'false');
  P.cfgPr.checked = d.auto_pr === true;
  P.cfgClose.checked = d.close === true;
  P.cfgMcp.checked = d.mcp_run === true;
  setText(P.cfgPlan, epPlanText(d));
  epRoutingCfgPaint(P);
}

/* The routing rows of the settings: the limit (empty = the board's own setting, shown), the group boxes (only with two paired nodes or more) and Let MCP run this epic. */
function epRoutingCfgPaint(P) {
  const d = P.data;
  const set = d.max_window_pct_set === true;
  const board = Number.isInteger(d.max_window_pct) ? d.max_window_pct : 85;
  if (typeof document === 'undefined' || document.activeElement !== P.winIn) P.winIn.value = set ? String(board) : '';
  P.winIn.placeholder = String(board);
  P.winIn.setAttribute('aria-invalid', P.winErr ? 'true' : 'false');
  setText(P.winHint, `${set ? `This epic skips a node at ${board} percent or more.` : `Using the board's setting: ${board} percent.`} ${EP_WINDOW_HINT}`);
  setText(P.winErrEl, P.winErr || '');
  P.winErrEl.classList.toggle('hidden', !P.winErr);
  const nodes = epGroupNodes(d);
  const lists = Array.isArray(d.group) ? d.group : [];
  P.groupRow.classList.toggle('hidden', nodes.length < 2);
  if (nodes.length >= 2) {
    const sig = nodes.map((n) => n.handle + ':' + n.name).join('|');
    if (P.groupSig !== sig) {                                          // the boxes are rebuilt only when the node list changes, so a poll never takes the focus away
      P.groupSig = sig;
      P.groupHost.textContent = '';
      P.groupBoxes = new Map();
      for (const n of nodes) {
        const box = el('input', { type: 'checkbox', 'data-group-node': n.handle, onchange: () => epSetGroup(P) });
        P.groupBoxes.set(n.handle, box);
        P.groupHost.append(el('label', { class: 'ep-check ep-check-node' }, box, el('span', { class: 'ep-check-t' }, el('b', { text: n.name }))));
      }
    }
    const many = lists.length > 1;                                      // more than one group came from elsewhere: shown, not edited here
    const on = P.groupDraft || new Set(lists[0] || []);
    for (const [h, box] of P.groupBoxes) { box.checked = many ? lists.some((g) => g.includes(h)) : on.has(h); box.disabled = many; }
    setText(P.groupErrEl, many ? 'Several groups are set for this epic. Change them on the board that set them.' : (P.groupErr || ''));
    P.groupErrEl.classList.toggle('hidden', !(many || P.groupErr));
  }
}

/* Run names what it would skip: the epic's author, or each issue by an author who is not on the allowed list. Run itself is the button above it. */
function epRunAuthors(d) {
  const a = d.authors && typeof d.authors === 'object' ? d.authors : null;
  if (!a || d.writes !== true) return '';
  if (a.epic_ok === false) return `Run would skip everything: the epic was written by ${a.epic || 'someone'}, ${EP_NOT_ALLOWED}.`;
  const b = Array.isArray(a.blocked) ? a.blocked.filter((x) => x && Number.isInteger(x.number)) : [];
  if (!b.length) return typeof a.text === 'string' ? a.text : '';
  return `Run would skip ${b.map((x) => `#${x.number}${x.author ? ' (' + x.author + ')' : ''}`).join(', ')}: ${b.length === 1 ? 'its author is' : 'their authors are'} not on the allowed list.`;
}

/* A lost claim is said once per browser with a toast (a record is kept until it is dropped, so the page would otherwise say it at every visit). */
function epLostSeen() {
  try { return new Set(JSON.parse(localStorage.getItem(EP_LOST_KEY) || '[]')); } catch (_) { return new Set(); }
}

function epToastLost(P) {
  const lost = (P.data.own_claims || []).filter((o) => o && o.state === 'lost');
  if (!lost.length) return;
  const seen = epLostSeen();
  let fresh = false;
  for (const o of lost) {
    const k = `${o.repo}#${o.issue}:${o.attempt}:${o.lost_reason || ''}`;
    if (seen.has(k) || P.toasted.has(k)) continue;
    P.toasted.add(k);
    seen.add(k);
    fresh = true;
    toast(`Lost the claim on #${o.issue}. ${epOwnText(o, P.data.writes)}`, { kind: 'bad', ttl: 9000 });
  }
  if (fresh) { try { localStorage.setItem(EP_LOST_KEY, JSON.stringify(Array.from(seen).slice(-40))); } catch (_) { /* storage may be unavailable */ } }
}

/* ---------- pins: the page pin wins over a label pin; a pin to a removed node is stale and ignored ---------- */

function epNodeName(data, handle) {
  const n = (data.nodes || []).find((x) => x.handle === handle);
  return n ? n.name : null;
}

function epPinOf(data, child) {
  const page = data.pins && ownKey(data.pins, String(child.number)) ? data.pins[String(child.number)] : null;
  const lp = child.label_pin || null;
  if (page && !page.stale) return { handle: page.handle, node_name: epNodeName(data, page.handle) || page.node_name || page.handle, source: 'page', stale: false };
  if (lp && !lp.stale) return { handle: lp.handle, node_name: epNodeName(data, lp.handle) || lp.alias, source: 'label', stale: false };
  if (page) return { handle: page.handle, node_name: page.node_name || page.handle, source: 'page', stale: true };
  if (lp) return { handle: null, node_name: lp.alias, source: 'label', stale: true };
  return null;
}

/* ---------- routing: Run, Pause, Run on... and the choice shown per issue (issues #150 and #152) ---------- */

function epRoute(d) { return d && d.route && typeof d.route === 'object' && !Array.isArray(d.route) ? d.route : null; }

/* Who started the run, in words: a person's login, or an agent's token name. Both are text other people can write: textContent only. */
function epRunBy(r) {
  const by = typeof r.by === 'string' && r.by ? r.by : 'you';
  return r.via === 'mcp' ? `an agent (${by})` : by;
}

/* What Run is now: {ok, label, text, title, warn}. Run is the one filled primary; it is off, with its reason in words, when nothing can be sent. */
function epRunState(P) {
  const d = P.data;
  const r = epRoute(d);
  if (!d || !P.loaded) {
    if (P.failed) return { ok: false, label: 'Run', text: 'Could not reach this board, so Run is off. This page asks again soon.', title: 'Could not reach this board', warn: true };
    return { ok: false, label: 'Run', text: '', title: 'Loading', warn: false };
  }
  if (!r) return { ok: false, label: 'Run', text: EP_RUN_NONE, title: EP_RUN_NONE, warn: false };
  if (P.runBusy) return { ok: false, label: 'Starting…', text: 'Starting the run: asking the board to send the ready issues.', title: 'Starting the run', warn: false };
  if (!r.can_run) {                                                       // checked before "running": a run that is recorded but cannot go on is not Running
    const t0 = r.blocked && typeof r.blocked.text === 'string' && r.blocked.text ? r.blocked.text : 'Run is not available on this board.';
    const t = r.running ? `${t0} A run is recorded and goes on when this clears.` : t0;
    return { ok: false, label: 'Run', text: t, title: t0, warn: true };
  }
  if (r.running && d.paused) return { ok: false, label: 'Paused', text: 'Paused: no new assignments until Resume. Issues already sent go on.', title: 'Paused. Resume continues the run.', warn: false };
  if (r.running) {
    const since = r.started_at ? epSince(r.started_at, d.server_now || d.at) : '';
    const left = typeof r.left === 'number' ? `, ${r.left} more ${r.left === 1 ? 'issue' : 'issues'} allowed` : '';
    const gh = r.error_code === 'github_unavailable' ? ` ${typeof r.error === 'string' && r.error ? r.error.slice(0, 160) : 'GitHub is not available.'} The run tries again ${epClock(r.retry_at) ? 'around ' + epClock(r.retry_at) : 'soon'}.` : '';       // issue #153: what is wrong and when it asks again
    return { ok: false, label: 'Running', text: `Running${since ? ', started ' + since : ''} by ${epRunBy(r)}${left}.${gh} Pause stops new assignments; issues already sent go on.`, title: 'The run is going. Pause stops new assignments.', warn: !!gh };
  }
  if (d.paused) return { ok: false, label: 'Run', text: EP_RUN_PAUSED, title: EP_RUN_PAUSED, warn: false };
  if (d.error && !(d.children || []).length) return { ok: false, label: 'Run', text: 'GitHub could not be read, so there is nothing to send yet.', title: 'GitHub could not be read', warn: true };
  const last = r.ended && ownKey(EP_RUN_ENDED, r.ended) ? ` The last run ${EP_RUN_ENDED[r.ended]}.` : '';
  return { ok: true, label: 'Run', text: EP_RUN_IDLE + last, title: 'Send each ready issue to the node that can take it', warn: false };
}

/* The plain sentence for a refused Run or Run on... `e` is what api() threw: e.body {error, reason, waiting} is the board's own sentence. */
function epRunSentence(e, what) {
  const b = e && e.body && typeof e.body === 'object' ? e.body : {};
  const own = typeof b.error === 'string' && b.error ? b.error : (e && e.message ? String(e.message) : 'the board refused');
  if (b.reason === 'waiting' && b.waiting && typeof b.waiting.text === 'string' && b.waiting.text) return b.waiting.text;
  if (e && e.status === 403) return 'Only a signed-in person can do this.';
  if (e && e.status === 429) return 'Too many requests. Try again in a minute.';
  return `${what} did not start: ${own.slice(0, 240)}`;
}

/* Run. The page says Running at once; the board's answer replaces it and a refusal puts the old state back (with the board's words in a toast and under the button). */
function epRun(P) {
  if (epicPage.cur !== P || !P.data || P.runBusy || !epRunState(P).ok) return null;
  const before = P.data.route;
  P.runBusy = true;
  P.runNote = null;
  P.runOpt = { ...before, running: true, ended: null, ended_at: null, via: 'page', by: null, started_at: P.data.server_now || null };
  P.data.route = P.runOpt;
  epPaint(P);
  return api('POST', epPath(P, '/run')).then((r) => {
    P.runBusy = false;
    P.runOpt = null;
    if (epicPage.cur !== P) return;
    const route = r && r.route && typeof r.route === 'object' ? r.route : null;
    if (route) { P.data.route = route; if (typeof route.paused === 'boolean') P.data.paused = route.paused; } else P.data.route = { ...before, running: true };
    toast('Run started: the ready issues go to the nodes that can take them');
    epPaint(P);
    epFetch(P);
  }, (e) => {
    P.runBusy = false;
    P.runOpt = null;
    if (epicPage.cur !== P) return;
    P.data.route = before;
    P.runNote = epRunSentence(e, 'Run');
    toast(P.runNote, { kind: 'bad' });
    epPaint(P);
  });
}

/* Pause and Resume: POST .../pause and .../resume. Painted at once; a refusal puts the old state back. Resume also wakes the loop, which a bare settings write does not. */
function epPause(P, on) {
  if (epicPage.cur !== P || !P.data || (P.pausing !== null && P.pausing !== undefined)) return null;
  const before = { paused: P.data.paused, route: P.data.route };
  P.pausing = on;
  P.data.paused = on;
  if (before.route) P.data.route = { ...before.route, paused: on };
  epPaint(P);
  return api('POST', epPath(P, on ? '/pause' : '/resume')).then((r) => {
    P.pausing = null;
    if (epicPage.cur !== P) return;
    if (r && typeof r === 'object') {
      if (typeof r.paused === 'boolean') P.data.paused = r.paused;
      if (r.route && typeof r.route === 'object') P.data.route = r.route;
    }
    epPaint(P);
  }, (e) => {
    P.pausing = null;
    if (epicPage.cur !== P) return;
    P.data.paused = before.paused;
    P.data.route = before.route;
    epPaint(P);
    toast(`${(e && e.message) || 'The board refused the change'}. Nothing was saved.`, { kind: 'bad' });
  });
}

/* The choice the board made for one issue (or its wait), as the row shows it: a glyph and the board's own words, never a bare spinner and never colour alone. */
const EP_ROUTE_GLYPH = { assigned: '→', waiting: '✻', held: '⚠', sending: '∙' };

function epRouteBit(c) {
  const r = c.route;
  if (!r || typeof r !== 'object' || !ownKey(EP_ROUTE_GLYPH, r.state) || typeof r.text !== 'string' || !r.text) return null;
  const attn = r.state === 'waiting' || r.state === 'held';
  const text = r.state === 'held' ? `held: ${r.text}` : r.text;
  return el('span', { class: 'ep-route' + (attn ? ' warn' : ''), 'data-route': r.state, 'data-route-code': r.code || null, role: r.state === 'sending' ? 'status' : null,
    title: r.state === 'waiting' ? 'The board asks again by itself and sends the issue as soon as a node can take it.' : r.state === 'held' ? 'The board does not send this issue until this clears.' : null },
  el('span', { class: 'ep-wg', 'aria-hidden': 'true', text: EP_ROUTE_GLYPH[r.state] + ' ' }), text);
}

/* The "Run on..." menu of one open, unclaimed issue: Auto first, then every paired node (never this board: routing sends work to nodes). */
function epRunOnItems(P, c) {
  const pin = epPinOf(P.data, c);
  const items = [{ label: 'Auto', sub: pin && !pin.stale ? `follows the pin: ${pin.node_name || pin.handle}` : 'the node with room', onClick: () => epRunIssue(P, c, 'auto') }];
  for (const n of P.data.nodes || []) {
    if (n.handle === 'local') continue;
    items.push({ label: n.name, sub: n.status === 'online' ? 'paired node' : `paired node, ${n.status}`, onClick: () => epRunIssue(P, c, n.handle) });
  }
  return items;
}

function epRunOnBits(P, c) {
  const r = epRoute(P.data);
  if (!r || c.state !== 'open' || !epClaimable(P.data, c) || !(P.data.nodes || []).some((n) => n.handle !== 'local')) return [];
  const key = `${c.repo}#${c.number}`;
  const busy = P.runon.get(key);
  const why = !r.can_run && r.blocked && r.blocked.text ? r.blocked.text : null;
  const btn = el('button', { class: 'small ep-runon-btn', type: 'button', 'data-runon': '', title: why || EP_RUNON_TITLE, 'aria-label': `Run issue #${c.number} on a node`, disabled: !!busy || !r.can_run || null },
    ic('play'), busy ? 'Sending…' : 'Run on…');
  if (!busy && r.can_run) menu(btn, () => epRunOnItems(P, c));
  const out = [btn];
  const note = P.rnotes.get(key);
  if (note) out.push(el('span', { class: 'ep-claimhint warn ep-claimnote', role: 'status', 'data-run-note': note.code || 'error', text: note.text }));
  return out;
}

/* Send one issue to a node, or to Auto. The row says "Sending to <node>..." (or "Choosing a node...") at once; the board's answer replaces it with the choice and its reason, a wait or a
   refusal puts the row back as it was and says why in words. The same checks as Run apply to a node you name. */
function epRunIssue(P, c, which) {
  const key = `${c.repo}#${c.number}`;
  if (P.runon.has(key) || epicPage.cur !== P) return null;
  const node = which === 'auto' ? null : ((P.data.nodes || []).find((x) => x.handle === which) || { handle: which, name: which });
  const kid = epWorkKid(P, c);
  const w = { node, snap: kid ? { ...epWorkSnap(kid), route: kid.route } : null };
  P.runon.set(key, w);
  P.rnotes.delete(key);
  P.wnotes.delete(key);
  P.notes.delete(key);
  if (kid) kid.route = { state: 'sending', text: node ? `Sending to ${node.name}…` : 'Choosing a node…', node: node ? node.name : null, handle: node ? node.handle : null, until: null, code: null };
  epPaint(P);
  const undo = () => { const k = epWorkKid(P, c); if (k && w.snap) Object.assign(k, w.snap); };
  return api('POST', epPath(P, `/issues/${encodeURIComponent(c.number)}/run`), { node: which }).then((r) => {
    P.runon.delete(key);
    if (epicPage.cur !== P) return;
    if (r && r.ok === true && typeof r.handle === 'string') {
      const target = (P.data.nodes || []).find((x) => x.handle === r.handle) || { handle: r.handle, name: typeof r.name === 'string' && r.name ? r.name : r.handle };
      const dd = r.data && typeof r.data === 'object' ? r.data : {};
      const done = { node: target, ref: typeof dd.task_ref === 'string' ? dd.task_ref : null, branch: typeof dd.branch === 'string' ? dd.branch : null, at: Date.now(),
        route: { state: 'assigned', text: String(r.why || `chose ${target.name}`).slice(0, 300), node: target.name, handle: target.handle, until: null, code: null, at: P.data.server_now } };
      const k = epWorkKid(P, c);
      if (k) { epWorkPaint(P, k, target); epWorkRunning(P, k, done); k.route = done.route; }
      P.worked.set(key, done);
      toast(`${target.name} started #${c.number}`);
    } else {
      undo();
      P.rnotes.set(key, { code: 'error', text: epRunSentence({ status: 502, body: {} }, `#${c.number}`) });
    }
    epPaint(P);
    epFetch(P);
  }, (e) => {
    P.runon.delete(key);
    if (epicPage.cur !== P) return;
    undo();
    const b = e && e.body && typeof e.body === 'object' ? e.body : {};
    const text = epRunSentence(e, `#${c.number}`);
    P.rnotes.set(key, { code: b.reason === 'waiting' && b.waiting ? 'waiting' : (b.reason || 'error'), text });
    toast(text, { kind: b.reason === 'waiting' ? 'ok' : 'bad' });
    epPaint(P);
  });
}

/* ---------- the progress strip ---------- */

const EP_SEGS = [['done', 'done'], ['running', 'running'], ['queued', 'queued'], ['failed', 'failed']];

function epWords(p, keys) {
  const out = [];
  for (const [k, word] of keys) if (epNum(p[k]) > 0) out.push(`${p[k]} ${word}`);
  return out.join(', ') || 'nothing yet';
}

/* One row of the strip: who, a bar of segments (width by count, a different fill and edge for each state) and the numbers in words, each with its glyph. */
function epStripRow(label, handle, keys, counts, name, title, glyph) {
  const total = keys.reduce((a, [k]) => a + epNum(counts[k]), 0);
  const words = epWords(counts, keys);
  const bar = el('div', { class: 'ep-bar', role: 'img', 'aria-label': `${label}: ${words}` });
  for (const [k] of keys) {
    const n = epNum(counts[k]);
    if (n <= 0) continue;
    const seg = el('span', { class: `ep-seg ${k}`, 'data-seg': k });
    seg.style.flexGrow = String(n);
    bar.append(seg);
  }
  if (!total) bar.append(el('span', { class: 'ep-seg none' }));
  const list = el('ul', { class: 'ep-words' });
  for (const [k, word] of keys) {
    const n = epNum(counts[k]);
    if (n <= 0) continue;
    list.append(el('li', { 'data-seg': k }, el('span', { class: `ep-wg ${k}`, 'aria-hidden': 'true', text: EP_STATE[k][0] }), ` ${n} ${word}`));
  }
  if (!total) list.append(el('li', { class: 'dim', text: 'nothing yet' }));
  return el('li', { class: 'ep-prow', 'data-node': handle || label },
    el('div', { class: 'ep-pwho' }, handle !== undefined ? epChip(handle, name || label, title, glyph) : el('span', { class: 'ep-plabel', text: label }), el('span', { class: 'dim ep-ptot', text: `${total} ${total === 1 ? 'issue' : 'issues'}` })), bar, list);
}

function epStrip(data) {
  const host = el('ul', { class: 'ep-prows' });
  const by = (data.progress && data.progress.by_node) || {};
  const aliases = Object.keys(by).sort((a, b) => ((by[a].handle === 'local' ? 0 : 1) - (by[b].handle === 'local' ? 0 : 1)) || a.localeCompare(b));
  for (const alias of aliases) {
    const p = by[alias];
    const known = !!p.handle;
    host.append(epStripRow(alias, p.handle || null, EP_SEGS, p, alias, known ? null : `${alias} is not a node paired with this board`, known ? null : '?'));
  }
  const un = (data.progress && data.progress.unclaimed) || { open: 0, closed: 0 };
  if (epNum(un.open) + epNum(un.closed) > 0) host.append(epStripRow('Unclaimed', undefined, [['open', 'open'], ['closed', 'closed']], un));
  const unk = epNum(data.progress && data.progress.unknown);
  if (unk > 0) host.append(epStripRow('Claim not known', undefined, [['unknown', 'claim not known']], { unknown: unk }));
  return host;
}

/* ---------- the rows ---------- */

function epBucket(c) {
  if (c.bucket && typeof c.bucket === 'object') return { node: c.bucket.node || null, state: ownKey(EP_STATE, c.bucket.state) ? c.bucket.state : 'open' };
  return { node: null, state: c.state === 'closed' ? 'closed' : 'open' };
}

function epTaskText(t) {
  if (!t) return '';
  const word = ownKey(EP_PHASE, t.phase) ? EP_PHASE[t.phase] : EP_PHASE.unknown;
  const name = t.node_name || t.node || 'a node';
  if (!t.status || t.status === 'online') return `Task ${word} on ${name}`;
  const age = typeof t.age_s === 'number' ? ` ${epAgo(t.age_s)}` : '';
  return `${name} is ${t.status}: last seen ${word}${age}`;
}

function epClaimParts(c, nowIso) {
  const k = c.claim;
  if (!k) return { text: '', title: '', warn: false };
  const word = ownKey(EP_CLAIM, k.status) ? EP_CLAIM[k.status] : 'claimed';
  const open = k.status === 'claimed' || k.status === 'running';
  if (open && k.live === false) {
    const when = epSince(k.renewed_at, nowIso);
    return { text: `${word[0].toUpperCase()}${word.slice(1)} by ${k.alias}, not live${when ? ': last renewed ' + when : ''}`, title: 'This claim was not renewed within its lease, so another node may take the issue.', warn: true };
  }
  const when = open && k.live === true ? epSince(k.renewed_at, nowIso) : '';
  return { text: `${word[0].toUpperCase()}${word.slice(1)} by ${k.alias}${when ? ', renewed ' + when : ''}`, title: '', warn: false };
}

/* What a row shows, as a string: the row repaints only when this changes (a poll never rebuilds a row that looks the same). */
function epRowSig(P, c) {
  const key = `${c.repo}#${c.number}`;
  const rt = epRoute(P.data);
  return JSON.stringify([epPermission(c), c, epPinOf(P.data, c), P.data.server_now, P.data.nodes, P.data.writes, epOwn(P.data, c), P.claiming.get(key) || null, P.notes.get(key) || null, P.data.epic_pin, P.working.has(key), P.wnotes.get(key) || null, P.data.authors || null,
    rt ? [rt.can_run, rt.blocked && rt.blocked.code] : null, P.runon.has(key), P.rnotes.get(key) || null]);
}

function epPinItems(P, current, apply) {
  const items = [{ label: current ? 'No pin' : '✓ No pin', sub: 'let the board choose', onClick: () => apply(null) }];
  for (const n of P.data.nodes || []) {
    const on = current && !current.stale && current.handle === n.handle;
    items.push({ label: `${on ? '✓ ' : ''}${n.name}`, sub: n.handle === 'local' ? 'this board' : n.status === 'online' ? 'paired node' : `paired node, ${n.status}`, onClick: () => apply(n.handle) });
  }
  return items;
}

function epPinBox(P, c) {
  const pin = epPinOf(P.data, c);
  const btn = el('button', { class: 'small ep-pin-btn', type: 'button', title: 'Pin this issue to a node (kept on this board only; nothing changes on GitHub)', 'aria-label': `Pin issue ${c.number} to a node` }, ic('pin'), 'Pin');
  menu(btn, () => epPinItems(P, pin, (h) => epSetPin(P, c.number, h)));
  let text = '';
  if (pin) {
    if (pin.stale) text = pin.source === 'page' ? `pinned to ${pin.node_name || pin.handle}, which is no longer paired: ignored` : `label names ${pin.node_name}, which is not paired: ignored`;
    else text = `${pin.source === 'label' ? 'label pin' : 'pinned'}: ${pin.node_name || pin.handle}`;
  }
  return el('div', { class: 'ep-pinbox' }, btn, text ? el('span', { class: 'ep-pintext' + (pin.stale ? ' warn' : ' dim'), text }) : null, ...epRunOnBits(P, c), ...epWorkBits(P, c), ...epClaimBits(P, c));
}

function epFillRow(P, li, c) {
  const data = P.data;
  const b = epBucket(c);
  const [g, gword] = EP_STATE[b.state];
  li.className = `ep-row ${b.state}`;
  li.setAttribute('data-n', String(c.number));
  li.setAttribute('data-state', b.state);
  li.textContent = '';
  const claim = epClaimParts(c, data.server_now);
  const task = epTaskText(c.task);
  const main = el('div', { class: 'ep-main' },
    el('span', { class: `ep-g ${b.state}`, role: 'img', 'aria-label': gword, title: gword, text: g }),
    epLink(c.url, `#${c.number}`, 'ep-num', 'Open the issue on GitHub'),
    el('span', { class: 'ep-title', text: c.title || `Issue ${c.number}` }));
  if (c.repo && data.epic && c.repo.toLowerCase() !== String(data.epic.repo || '').toLowerCase()) main.append(el('span', { class: 'badge ep-other', title: 'This issue is in another repository', text: c.repo }));
  const meta = el('div', { class: 'ep-meta2' });
  if (b.node) {
    const k = c.claim && c.claim.alias === b.node ? c.claim : null;
    const handle = k ? k.node : (c.task && c.task.node_name === b.node ? c.task.node : null);
    meta.append(epChip(handle, b.node, handle ? null : `${b.node} is not a node paired with this board`, handle ? null : '?'));
  }
  if (claim.text) meta.append(el('span', { class: 'ep-claim' + (claim.warn ? ' warn' : ''), title: claim.title || null }, claim.warn ? el('span', { class: 'ep-wg', 'aria-hidden': 'true', text: '⚠ ' }) : null, claim.text));
  else if (c.claim_state === 'unknown' && c.state === 'open') {
    const why = c.claim_unknown && typeof c.claim_unknown.text === 'string' && c.claim_unknown.text ? c.claim_unknown.text : 'not every comment was read';
    meta.append(el('span', { class: 'ep-claim warn', 'data-claim': 'unknown', title: 'The board could not read all the comments of this issue, so it cannot tell whether someone has claimed it. It is not shown as free.' },
      el('span', { class: 'ep-wg', 'aria-hidden': 'true', text: '? ' }), `Claim not known: ${why}`));
  } else if (c.claim_rejected > 0) meta.append(el('span', { class: 'ep-claim warn', title: 'Someone who is not on the allowed list wrote a claim comment. It is ignored.' }, el('span', { class: 'ep-wg', 'aria-hidden': 'true', text: '⚠ ' }), c.claim_note || 'not from an allowed author'));
  const own = epOwn(data, c);
  if (own) meta.append(el('span', { class: 'ep-own' + (own.state === 'lost' || (own.state === 'held' && data.writes === false) ? ' warn' : ''), 'data-own': own.state }, el('span', { class: 'ep-wg', 'aria-hidden': 'true', text: own.state === 'lost' ? '⚠ ' : own.state === 'claiming' ? '∙ ' : '✽ ' }), epOwnText(own, data.writes)));
  const readAgo = c.state === 'open' && c.claims_read_at ? epSince(c.claims_read_at, data.server_now) : '';
  if (readAgo) meta.append(el('span', { class: 'dim ep-read', title: 'When the board last read this issue\'s comments, by GitHub\'s clock. "Unclaimed" is only true for that moment.', text: `claims read ${readAgo}` }));
  if (c.state === 'open' && ['blocked', 'unknown'].includes(c.edit_state)) meta.append(el('span', { class: 'ep-author warn', 'data-edit': c.edit_state, title: epEditText(c) },
    el('span', { class: 'ep-wg', 'aria-hidden': 'true', text: '⚠ ' }), epEditText(c)));
  if (c.state === 'open' && c.author_ok === false) meta.append(el('span', { class: 'ep-author warn', 'data-author': 'blocked', title: 'The node runs only issues by an allowed author, because an issue body becomes the prompt of an agent that edits files.' },
    el('span', { class: 'ep-wg', 'aria-hidden': 'true', text: '⚠ ' }), c.author ? `by ${c.author}, ${EP_NOT_ALLOWED}` : 'author not on the allowed list'));
  const chosen = epRouteBit(c);
  if (chosen) meta.append(chosen);
  const phase = epWorkPhaseNode(c);
  if (phase) meta.append(phase);
  if (task) meta.append(el('span', { class: 'ep-task', text: task }));
  const open = (c.task || c.work) ? epTaskHash(c) : null;
  if (open) meta.append(el('a', { class: 'ep-open-task', href: open, 'data-open-task': '', title: 'Open the task on the node it runs on', text: `Open on ${(c.task && c.task.node_name) || (c.claim && c.claim.alias) || 'this board'}` }));
  if (c.pr) meta.append(epLink(c.pr.url, `PR #${c.pr.number} ${c.pr.draft ? 'draft' : c.pr.state}`, 'ep-pr', 'Open the pull request on GitHub'));
  if ((c.blocked_by || []).length) meta.append(el('span', { class: 'ep-dep', text: `waits for ${c.blocked_by.join(', ')}` }));
  if (c.dependency_cycle) meta.append(el('span', { class: 'ep-claim warn', text: '⚠ dependency cycle' }));
  const when = epSince(c.updated_at, data.server_now);
  if (when) meta.append(el('span', { class: 'dim ep-age', title: 'GitHub time of the last change', text: `updated ${when}` }));
  li.append(main, meta, epPinBox(P, c));
  const permission = epPermissionNode(epPermission(c));
  if (permission) li.append(permission);
}

/* ---------- the page ---------- */

function epPath(P, tail) { return `/api/epics/${encodeURIComponent(P.owner)}/${encodeURIComponent(P.repo)}/${encodeURIComponent(P.n)}${tail || ''}`; }

function epSettingsUndo(P, snap) {
  P.data.pins = snap.pins; P.data.epic_pin = snap.epic_pin; P.data.paused = snap.paused; P.data.report = snap.report; P.data.auto_pr = snap.auto_pr; P.data.close = snap.close;
  P.data.max_window_pct = snap.max_window_pct; P.data.max_window_pct_set = snap.max_window_pct_set; P.data.group = snap.group; P.data.mcp_run = snap.mcp_run;
}

/* One write to this board's settings, painted first: the new value shows at once and a refusal puts the old one back (with the board's words in a toast). */
function epWrite(P, patch, apply) {
  const snap = { pins: { ...(P.data.pins || {}) }, epic_pin: P.data.epic_pin, paused: P.data.paused, report: P.data.report, auto_pr: P.data.auto_pr, close: P.data.close,
    max_window_pct: P.data.max_window_pct, max_window_pct_set: P.data.max_window_pct_set, group: P.data.group, mcp_run: P.data.mcp_run };
  apply();
  epPaint(P);
  const id = (P.writes = (P.writes || 0) + 1);
  return api('PUT', epPath(P, '/settings'), patch).then((r) => {
    if (epicPage.cur !== P) return;
    if (r && typeof r === 'object' && r.pins && id === P.writes) { P.data.pins = r.pins; P.data.epic_pin = r.epic_pin || null; P.data.paused = !!r.paused; if (ownKey(r, 'report')) P.data.report = epReportWord(r.report); if (ownKey(r, 'auto_pr')) P.data.auto_pr = r.auto_pr === true; if (ownKey(r, 'close')) P.data.close = r.close === true;
      if (Number.isInteger(r.max_window_pct)) { P.data.max_window_pct = r.max_window_pct; P.data.max_window_pct_set = r.max_window_pct_set === true; } if (Array.isArray(r.group)) P.data.group = r.group; if (ownKey(r, 'mcp_run')) P.data.mcp_run = r.mcp_run === true;
      if (Array.isArray(r.nodes)) P.data.nodes = r.nodes.map((n) => ({ ...(P.data.nodes || []).find((x) => x.handle === n.handle), ...n })); }
    epPaint(P);
  }, (e) => {
    if (epicPage.cur !== P) return;
    epSettingsUndo(P, snap);
    epPaint(P);
    toast(`${(e && e.message) || 'The board refused the change'}. Nothing was saved.`, { kind: 'bad' });
  });
}

function epSetPin(P, issue, handle) {
  const k = String(issue);
  return epWrite(P, { pins: { [k]: handle } }, () => {
    const pins = { ...(P.data.pins || {}) };
    if (handle) pins[k] = { handle, node_name: epNodeName(P.data, handle), source: 'page', stale: false }; else delete pins[k];
    P.data.pins = pins;
  });
}

function epSetEpicPin(P, handle) {
  return epWrite(P, { epic_pin: handle }, () => {
    P.data.epic_pin = handle ? { handle, node_name: epNodeName(P.data, handle), source: 'page', stale: false } : null;
  });
}

function epRateText(rate) {
  if (!rate || typeof rate !== 'object') return '';
  if (rate.wait_s) return '';
  if (typeof rate.remaining === 'number' && typeof rate.limit === 'number') return `GitHub requests left: ${rate.remaining} of ${rate.limit}`;
  return '';
}

function epEpicPinText(P) {
  const p = P.data.epic_pin;
  if (!p) return 'Epic: no pin';
  if (p.stale) return `Epic pinned to ${p.node_name || p.handle}, which is no longer paired: ignored`;
  return `Epic pinned to ${p.node_name || p.handle}`;
}

function epBanner(P) {
  const d = P.data;
  const out = [];
  const err = d.error;
  if (P.failed) out.push(['bad', `Could not reach this board${P.failed.message ? ': ' + P.failed.message : ''}.`, d.at ? 'Showing the last read. Asking again soon.' : 'Asking again soon.']);
  if (err) {
    const next = ownKey(EP_NEXT, err.reason) ? EP_NEXT[err.reason] : '';
    out.push([err.reason === 'rate_limited' ? 'warn' : 'bad', err.message || 'GitHub could not be read.', next]);
    if (typeof d.stale_age_s === 'number') out.push(['dim', `Showing the last read, from ${epAgo(d.stale_age_s).replace(' ago', '')} ago.`, '']);
  }
  if (d.note) out.push(['dim', d.note, '']);
  if (d.claims_note) out.push(['warn', d.claims_note, EP_AUTHORS_NEXT]);
  if (d.truncated) out.push(['warn', EP_TRUNCATED, '']);
  if (d.writes === false && (d.own_claims || []).some((o) => o && o.state === 'held')) out.push(['warn', EP_WRITES_OFF, 'A claim this board holds is not renewed while writes are off, so it lapses after its lease.']);
  else if (d.writes === false && (d.children || []).some((c) => epClaimable(d, c) && epClaimNode(d, c))) out.push(['dim', EP_WRITES_OFF, EP_WRITES_OFF_NEXT]);
  if (d.partial && d.comments_pending > 0) out.push(['dim', `${d.comments_pending} ${d.comments_pending === 1 ? 'issue' : 'issues'} still to be checked for claims; they fill in on the next refresh.`, '']);
  return out;
}

function epPaint(P) {
  if (epicPage.cur !== P || !P.data) return;
  const d = P.data;
  const e = d.epic || {};
  const title = e.title || `Epic #${P.n}`;
  setText(P.title, title);
  P.name.textContent = '';
  P.name.append(epLink(e.url, epRef(P.owner, P.repo, P.n), 'ep-ref', 'Open the epic on GitHub'));
  const prog = d.progress || {};
  const open = epNum(prog.open);
  const closed = epNum(prog.closed);
  setText(P.counts, d.error && !(d.children || []).length ? '' : `${open} open, ${closed} closed`);
  setText(P.rate, epRateText(d.rate));
  const rs = epRunState(P);
  P.run.textContent = rs.label;
  P.run.disabled = !rs.ok;
  if (rs.ok) P.run.removeAttribute('disabled'); else P.run.setAttribute('disabled', '');
  P.run.setAttribute('title', rs.title);
  P.run.setAttribute('aria-busy', P.runBusy ? 'true' : 'false');
  setText(P.runWhy, `${rs.text}${P.runNote ? ' ' + P.runNote : ''}`.trim());
  P.runWhy.classList.toggle('warn', rs.warn || !!P.runNote);
  P.runWhy.classList.toggle('dim', !(rs.warn || P.runNote));
  P.pause.textContent = d.paused ? 'Resume' : 'Pause';
  P.pause.setAttribute('aria-pressed', d.paused ? 'true' : 'false');
  P.pause.title = d.paused ? 'Resume routing this epic on this board' : 'Pause routing this epic on this board';
  const rr = epRoute(d);
  setText(P.pauseWhy, rr && rr.running ? '' : (d.paused ? 'Paused on this board: it does not route this epic. Nothing changes on GitHub.' : EP_PAUSE_WHY));   // a recorded run says what Pause does in its own line
  P.pauseWhy.classList.toggle('hidden', !!(rr && rr.running));
  P.head.classList.toggle('paused', !!d.paused);
  setText(P.epicPin, epEpicPinText(P));
  const runAuthors = epRunAuthors(d);
  setText(P.runAuthors, runAuthors);
  P.runAuthors.classList.toggle('hidden', !runAuthors);
  epCfgPaint(P);
  P.epicPinBtn.disabled = !(d.nodes || []).length;

  P.banner.textContent = '';
  for (const [kind, text, next] of epBanner(P)) {
    P.banner.append(el('div', { class: `ep-note ${kind}` }, el('span', { class: 'ep-note-t', text }), next ? el('span', { class: 'ep-note-n dim', text: next }) : null));
  }

  const kids = d.children || [];
  const nothing = !kids.length;
  P.strip.textContent = '';
  P.stripSec.classList.toggle('hidden', nothing);
  if (!nothing) P.strip.append(epStrip(d));
  P.empty.textContent = '';
  const showEmpty = nothing && d.empty === true && !d.error;
  P.empty.classList.toggle('hidden', !showEmpty);
  if (showEmpty) P.empty.append(pageEmpty('git-branch', EP_EMPTY, ''), epLink(e.url, 'Open the epic on GitHub', 'btn small ep-open'));
  P.skel.classList.toggle('hidden', true);
  epToastLost(P);

  // three groups in the queue order GitHub keeps: the issues a node holds or finished, the Unclaimed (every comment read, no claim) and the ones whose claim is not known (never mixed
  // with the Unclaimed). A row that holds focus stays in its group until it lets go, so a poll never moves it.
  const groups = { nodes: [], free: [], unknown: [] };
  for (const c of kids) {
    const key = c.repo + '#' + c.number;
    const b = epBucket(c);
    const want = b.node ? 'nodes' : (b.state === 'unknown' ? 'unknown' : 'free');
    const was = P.where.get(key);
    const row = P.lists[was] && P.lists[was].nodes.get(key);
    const g = row && was !== want && row.contains(document.activeElement) ? was : want;
    P.where.set(key, g);
    groups[g].push(c);
  }
  for (const [key] of Array.from(P.where)) if (!kids.some((c) => c.repo + '#' + c.number === key)) P.where.delete(key);
  for (const g of ['nodes', 'free', 'unknown']) {
    P.lists[g].update(groups[g]);
    P.secs[g].classList.toggle('hidden', !groups[g].length);
    setText(P.counts2[g], `(${groups[g].length})`);
  }
}

function epRowList(P, g, host) {
  return makeKeyedList(host, {
    key: (c) => c.repo + '#' + c.number,
    create: (c) => {
      const li = el('li', { class: 'ep-row' });
      li.ccSig = null;
      li.addEventListener('focusout', () => { if (li.ccPending) { const p = li.ccPending; li.ccPending = null; setTimeout(() => { if (epicPage.cur === P) epPatchRow(P, li, p); }, 0); } });
      epPatchRow(P, li, c);
      return li;
    },
    patch: (li, c) => epPatchRow(P, li, c),
  });
}

function epPatchRow(P, li, c) {
  const sig = epRowSig(P, c);
  if (li.ccSig === sig) return;
  if (li.ccSig !== null && (li.contains(document.activeElement) || li.querySelector('[aria-expanded="true"]'))) { li.ccPending = c; return; }       // a pin menu is open or a button has focus: repaint when it lets go
  li.ccSig = sig;
  epFillRow(P, li, c);
}

function epDelay(P) {
  const err = P.data && P.data.error;
  if (err && err.reason === 'rate_limited' && typeof err.wait_s === 'number') return Math.min(EP_WAIT_MAX_MS, Math.max(5000, (err.wait_s + 1) * 1000));
  if (P.failed) return 15000;
  const rt = epRoute(P.data);
  if (rt && rt.running) return EP_ROUTE_POLL_MS;                                  // a run is going: its choices and waits show soon
  return EP_POLL_MS;
}

function epSchedule(P) {
  if (P.timer) { clearTimeout(P.timer); P.timer = null; }
  if (epicPage.cur !== P) return;
  P.timer = setTimeout(() => { P.timer = null; if (typeof document !== 'undefined' && document.hidden) { P.sleeping = true; return; } epFetch(P); }, epDelay(P));
}

function epFetch(P) {
  if (P.busy || epicPage.cur !== P) return P.busy;
  P.busy = api('GET', epPath(P)).then((d) => {
    if (epicPage.cur !== P) return;
    P.failed = null;
    if (d && typeof d === 'object' && d.epic) {
      P.data = d; P.loaded = true; epRemember(); epWorkReapply(P);
      if (P.runBusy && P.runOpt) P.data.route = P.runOpt;                         // a poll that was out before the Run landed must not take Running back
      if (P.pausing !== null && P.pausing !== undefined) { P.data.paused = P.pausing; if (P.data.route) P.data.route = { ...P.data.route, paused: P.pausing }; }
    }
    epPaint(P);
    epCheckClaims(P).then(() => { if (epicPage.cur === P) epPaint(P); });
  }, (e) => {
    if (epicPage.cur !== P) return;
    P.failed = { message: e && e.status === 400 ? 'that is not a GitHub epic address' : (e && e.message) || '' };
    if (!P.data) P.data = epBlank(P);
    epPaint(P);
  }).then(() => { P.busy = null; epSchedule(P); });
  return P.busy;
}

function epBlank(P) {
  return { epic: { repo: `${P.owner}/${P.repo}`, owner: P.owner, name: P.repo, number: Number(P.n), title: null, url: `${EP_GH}${P.owner}/${P.repo}/issues/${P.n}`, state: null }, children: [], progress: { by_node: {}, unclaimed: { open: 0, closed: 0 }, unknown: 0, open: 0, closed: 0, total: 0 },
    pins: {}, epic_pin: null, paused: false, nodes: [], rate: null, server_now: null, error: null, stale_age_s: null, empty: false, writes: null, own_claims: [],
    route: null, max_window_pct: 85, max_window_pct_set: false, group: [], mcp_run: false };
}

function epMount(root, route) {
  const q = route.params || {};
  const P = { id: ++epicPage.ids, owner: q.owner, repo: q.repo, n: q.n, data: null, failed: null, busy: null, timer: null, sleeping: false, loaded: false, where: new Map(), lists: {}, secs: {}, counts2: {}, writes: 0, claiming: new Map(), claimChecks: new Map(), notes: new Map(), working: new Map(), worked: new Map(), wnotes: new Map(), toasted: new Set(),
    runBusy: false, runOpt: null, runNote: null, pausing: null, runon: new Map(), rnotes: new Map(), winErr: '', groupErr: '', groupDraft: null, groupSig: null, groupBoxes: new Map() };
  P.title = el('h1', { class: 'ep-h1', text: `Epic #${P.n}` });
  P.name = el('span', { class: 'ep-name mono' });
  P.counts = el('span', { class: 'ep-counts' });
  P.rate = el('span', { class: 'dim ep-rate' });
  P.run = el('button', { class: 'primary ep-run', type: 'button', disabled: true, 'aria-describedby': 'ep-run-why', title: 'Loading', text: 'Run', onclick: () => epRun(P) });
  P.pause = el('button', { class: 'ep-pause', type: 'button', 'aria-pressed': 'false', text: 'Pause', onclick: () => { if (P.data) epPause(P, !P.data.paused); } });
  P.pauseWhy = el('p', { class: 'dim ep-why', 'data-pause-why': '', text: EP_PAUSE_WHY });
  P.runWhy = el('p', { class: 'dim ep-why', id: 'ep-run-why', 'data-run-why': '' });
  P.runAuthors = el('p', { class: 'ep-why warn hidden', 'data-run-authors': '' });
  P.epicPin = el('span', { class: 'dim ep-epicpin', text: 'Epic: no pin' });
  P.epicPinBtn = el('button', { class: 'small ep-pin-btn', type: 'button', title: 'Pin the whole epic to a node (kept on this board only; nothing changes on GitHub)', 'aria-label': 'Pin the whole epic to a node' }, ic('pin'), 'Pin epic');
  menu(P.epicPinBtn, () => epPinItems(P, P.data && P.data.epic_pin, (h) => epSetEpicPin(P, h)));
  P.head = el('header', { class: 'ep-head' },
    el('div', { class: 'ep-crumb' }, el('a', { class: 'ep-back', href: '#/tasks?tab=epics', text: 'Epics' }), el('span', { class: 'dim', 'aria-hidden': 'true', text: ' / ' }), P.name),
    el('div', { class: 'page-head ep-titlerow' }, P.title, el('div', { class: 'actions ep-actions' }, P.pause, P.run)),
    el('div', { class: 'ep-sub' }, P.counts, P.rate),
    P.runWhy,
    P.runAuthors,
    P.pauseWhy,
    el('div', { class: 'ep-sub' }, P.epicPinBtn, P.epicPin));
  P.banner = el('div', { class: 'ep-banner', role: 'status', 'aria-live': 'polite' });
  P.strip = el('div', { class: 'ep-strip' });
  P.stripSec = el('section', { class: 'ep-sec', 'aria-label': 'Progress by node' }, el('h2', { class: 'ep-h', text: 'Progress by node' }), P.strip);
  P.empty = el('div', { class: 'ep-emptybox hidden' });
  P.skel = typeof Pages !== 'undefined' && Pages && typeof Pages.skeleton === 'function' ? Pages.skeleton(3) : el('div', { class: 'dim', text: 'Loading…' });
  const sec = (g, label) => {
    const host = el('ul', { class: 'ep-rows' });
    P.counts2[g] = el('span', { class: 'ep-hn dim' });
    P.secs[g] = el('section', { class: 'ep-sec hidden', 'data-group': g, 'aria-label': label }, el('h2', { class: 'ep-h' }, label + ' ', P.counts2[g]),
      g === 'unknown' ? el('p', { class: 'dim ep-why', text: EP_UNKNOWN_HINT }) : null, host);
    P.lists[g] = epRowList(P, g, host);
    return P.secs[g];
  };
  P.cfg = epCfgBuild(P);
  root.append(el('div', { class: 'ep ep-page' }, P.head, P.banner, P.cfg, P.skel, P.empty, P.stripSec, sec('nodes', 'On nodes'), sec('free', 'Unclaimed'), sec('unknown', 'Claim not known')));
  P.stripSec.classList.add('hidden');
  P.onVis = () => {
    if (document.hidden || epicPage.cur !== P) return;
    if (P.sleeping || !P.timer) { P.sleeping = false; epFetch(P); }
  };
  document.addEventListener('visibilitychange', P.onVis);
  epicPage.cur = P;
  P.onNodes = () => epPaint(P);
  if (typeof Nodes !== 'undefined') Nodes.use(() => { if (epicPage.cur === P) { Nodes.M.subs.add(P.onNodes); epPaint(P); } });
  epFetch(P);
}

function epUnmount() {
  const P = epicPage.cur;
  if (!P) return;
  epicPage.cur = null;
  if (P.timer) clearTimeout(P.timer);
  P.timer = null;
  document.removeEventListener('visibilitychange', P.onVis);
  if (typeof Nodes !== 'undefined' && Nodes.M) Nodes.M.subs.delete(P.onNodes);
}

registerPage('epic', {
  title(route) { const q = (route && route.params) || {}; return q.owner ? `${epRef(q.owner, q.repo, q.n)} · epic` : 'Epic'; },
  mount: epMount,
  update() { if (epicPage.cur) epPaint(epicPage.cur); },
  unmount: epUnmount,
});

/* ---------- the Epics list (the Tasks page's Epics tab) ---------- */

/* The list answer, once: a second ask while one is out gets the same promise; a recent answer (30 s) is reused, unless it stopped at the board's call budget (`partial`): that one is asked again. A refusal leaves the last answer in place. */
Epics.fetchList = function (force) {
  if (Epics.pending) return Epics.pending;
  if (!force && Epics.list && Epics.list.partial !== true && Date.now() - Epics.at < 30000) return Promise.resolve(Epics.list);
  Epics.pending = api('GET', '/api/epics').then((d) => {
    if (d && typeof d === 'object' && Array.isArray(d.epics)) {
      Epics.list = d; Epics.at = Date.now();
      Epics.known = d.epics.filter((e) => e && Epics.hash(e.owner, e.name, e.number));
      if (Epics.known.length) epRemember();
    }
    return Epics.list;
  }, () => Epics.list).then((r) => { Epics.pending = null; return r; });
  return Epics.pending;
};

function epListRow(e, nowIso) {
  const h = Epics.hash(e.owner, e.name, e.number);
  const c = e.children;
  const counts = c ? `${c.open} open, ${c.closed} closed${c.more ? ', more issues than shown' : ''}` : e.children_state === 'budget' ? 'issues not read this time' : 'issues not counted yet';          // never 0: an epic whose issues were not read says so (issue #147)
  const when = epSince(e.updated_at, nowIso);
  return el('li', { class: 'ep-lrow' + (e.paused ? ' paused' : ''), 'data-epic': epRef(e.owner, e.name, e.number) },
    el('a', { class: 'ep-ltitle', href: h || '#/tasks?tab=epics', text: e.title || `Epic #${e.number}` }),
    el('div', { class: 'ep-lmeta' }, el('span', { class: 'mono', text: epRef(e.owner, e.name, e.number) }), el('span', { text: counts }), e.paused ? el('span', { class: 'badge warn', title: 'This board does not route this epic', text: 'paused' }) : null,
      when ? el('span', { class: 'dim', text: `updated ${when}` }) : null, epLink(e.url, 'GitHub', 'ep-lgh', 'Open the epic on GitHub')));
}

function epListPaint(L) {
  const d = Epics.list;
  L.body.textContent = '';
  if (!d) { L.body.append(L.failed ? el('div', { class: 'ep-note bad' }, el('span', { class: 'ep-note-t', text: 'Could not reach this board.' }), el('span', { class: 'ep-note-n dim', text: 'Asking again soon.' })) : (typeof Pages !== 'undefined' && Pages && typeof Pages.skeleton === 'function' ? Pages.skeleton(2) : el('div', { class: 'dim', text: 'Loading…' }))); return; }
  if (d.error) {
    const next = ownKey(EP_NEXT, d.error.reason) ? EP_NEXT[d.error.reason] : '';
    L.body.append(el('div', { class: `ep-note ${d.error.reason === 'rate_limited' ? 'warn' : 'bad'}`, role: 'status' }, el('span', { class: 'ep-note-t', text: d.error.message || 'GitHub could not be read.' }), next ? el('span', { class: 'ep-note-n dim', text: next }) : null));
    if (typeof d.stale_age_s === 'number') L.body.append(el('p', { class: 'dim ep-lnote', text: `Showing the last read, from ${epAgo(d.stale_age_s).replace(' ago', '')} ago.` }));
  }
  for (const er of d.errors || []) L.body.append(el('div', { class: 'ep-note warn' }, el('span', { class: 'ep-note-t', text: `${er.repo}: ${er.message || 'could not be read'}` }), ownKey(EP_NEXT, er.reason) ? el('span', { class: 'ep-note-n dim', text: EP_NEXT[er.reason] }) : null));
  if (d.epics && d.epics.length) {
    const ul = el('ul', { class: 'ep-lrows' });
    for (const e of d.epics) ul.append(epListRow(e, d.server_now));
    L.body.append(ul);
  } else if (!d.error) {
    L.body.append(pageEmpty('git-branch', d.note ? 'No GitHub remote here' : 'No epics yet', d.note || 'An epic is an issue labelled ccboard:epic that has sub-issues. Label one on GitHub and it shows up here.'));
  }
  if (d.truncated) L.body.append(el('div', { class: 'ep-note warn', role: 'status' }, el('span', { class: 'ep-note-t', text: 'There are more epics than shown.' })));
  if (d.partial === true) {                                    // this refresh stopped at its call budget: what it did not read is said, and the next refresh carries on
    const repos = Array.isArray(d.deferred_repos) ? d.deferred_repos.filter((r) => typeof r === 'string').slice(0, 4) : [];
    const more = Array.isArray(d.deferred_repos) ? d.deferred_repos.length - repos.length : 0;
    const parts = [];
    if (repos.length) parts.push(`not looked in this time: ${repos.join(', ')}${more > 0 ? ` and ${more} more` : ''}`);
    if (Number.isInteger(d.deferred_counts) && d.deferred_counts > 0) parts.push(`${d.deferred_counts} ${d.deferred_counts === 1 ? 'epic' : 'epics'} with their issues not read this time`);
    if (!parts.length) parts.push('part of it was not read this time');
    L.body.append(el('div', { class: 'ep-note warn', role: 'status', 'data-partial': '' }, el('span', { class: 'ep-note-t', text: 'This list stopped at its GitHub call limit.' }),
      el('span', { class: 'ep-note-n dim', text: `${parts.join('; ')}. The next refresh carries on from there.` })));
  }
  if (d.repos && d.repos.length) L.body.append(el('p', { class: 'dim ep-lnote', text: `Looked in ${d.repos.length} ${d.repos.length === 1 ? 'repository' : 'repositories'}: ${d.repos.slice(0, 4).join(', ')}${d.repos.length > 4 ? ' and more' : ''}.${d.repos_truncated ? ' This board has more repositories than it looks in.' : ''}` }));
  const rate = epRateText(d.rate);
  if (rate) L.body.append(el('p', { class: 'dim ep-lnote', text: rate }));
}

function epListTick(L) {
  if (Epics.host !== L) return;
  if (L.timer) { clearTimeout(L.timer); L.timer = null; }
  const err = Epics.list && Epics.list.error;
  const wait = err && err.reason === 'rate_limited' && typeof err.wait_s === 'number' ? Math.min(EP_WAIT_MAX_MS, Math.max(5000, (err.wait_s + 1) * 1000)) : Epics.list && Epics.list.partial === true ? EP_LIST_PARTIAL_MS : EP_LIST_POLL_MS;
  L.timer = setTimeout(() => { L.timer = null; if (document.hidden) { L.sleeping = true; return; } epListLoad(L, true); }, wait);
}

function epListLoad(L, force) {
  Epics.fetchList(force).then(() => { L.failed = !Epics.list; if (Epics.host === L) { epListPaint(L); epListTick(L); } });
}

/* Draw the list into `host` (the Tasks page's Epics panel) and keep it fresh while it is visible. One list at a time; Epics.stopList() takes it away. */
Epics.paintList = function (host) {
  if (Epics.host && Epics.host.node === host) return;
  Epics.stopList();
  const L = { node: host, body: el('div', { class: 'ep-lbody' }), timer: null, sleeping: false, failed: false };
  L.onVis = () => { if (!document.hidden && Epics.host === L && (L.sleeping || !L.timer)) { L.sleeping = false; epListLoad(L, false); } };
  document.addEventListener('visibilitychange', L.onVis);
  host.textContent = '';
  host.append(el('div', { class: 'page-head tk-head ep-lhead' }, el('h1', { text: 'Epics' }), el('span', { class: 'summary', text: 'issues with sub-issues on GitHub, shared out across your nodes' }),
    el('div', { class: 'actions' }, el('button', { class: 'small', type: 'button', text: 'Refresh', onclick: () => epListLoad(L, true) }),
      el('button', { class: 'small primary ep-new', type: 'button', onclick: () => epicNewOpen({}), title: 'Write a new epic and its issues to GitHub: you see a preview first' }, ic('plus'), 'New epic'))), L.body);
  Epics.host = L;
  epListPaint(L);
  epListLoad(L, false);
  try {                                                         // #/tasks?tab=epics&new=1 opens the New epic sheet (a link to share, and for a screenshot); the address is then the plain one
    const r = typeof currentRoute === 'function' ? currentRoute() : null;
    if (r && r.query && r.query.new === '1' && typeof epicNewOpen === 'function' && typeof navigate === 'function') {
      epicNewOpen({});
      setTimeout(() => navigate('#/tasks?tab=epics', { replace: true }), 0);          // out of this route change, then the plain address (router.js owns the history)
    }
  } catch (_) { /* no route yet */ }
};

Epics.stopList = function () {
  const L = Epics.host;
  if (!L) return;
  Epics.host = null;
  if (L.timer) clearTimeout(L.timer);
  document.removeEventListener('visibilitychange', L.onVis);
};

/* ---------- the demo (?demo=1): list, detail and pins from demo/epic.json; a PUT is played here, nothing leaves the page ---------- */

const demoEpicMade = {};                                    // "owner/repo#n" -> the settings the visitor made

/* The claim ask in the demo: ?claim=<word> plays a refusal (taken lost unknown writesoff closed done attempts offline scope unconfirmed), else the claim is made and the issue shows as
   claimed by that node from then on. Nothing leaves the page. */
const DEMO_CLAIM = {
  taken: [409, { reason: 'claim_refused', why: 'taken', winner: 'willow-3f' }], lost: [409, { reason: 'claim_refused', why: 'lost', winner: 'willow-3f' }],
  unknown: [409, { reason: 'claim_refused', why: 'claim_unknown', unknown_reason: 'too_long' }], writesoff: [409, { reason: 'claim_refused', why: 'writes_off' }],
  closed: [409, { reason: 'claim_refused', why: 'closed' }], done: [409, { reason: 'claim_refused', why: 'done' }], attempts: [409, { reason: 'claim_refused', why: 'attempts_exhausted' }],
  offline: [503, { reason: 'offline' }], scope: [409, { reason: 'scope' }], unconfirmed: [504, { reason: 'unconfirmed' }],
};
const demoEpicClaimed = {};

/* The work ask in the demo (issue #149): ?work=<word> plays a refusal (the claim's words and nothere author epicauthor unreadable bypass posture lanes launch), else the node starts the issue
   and the row shows its task running from then on. "lanes" is refused until the visitor taps Start anyway (force). */
const DEMO_WORK = {
  edited: [409, { reason: 'work_refused', why: 'edit_not_allowed', editor: 'dana-w' }],
  editunknown: [409, { reason: 'work_refused', why: 'edit_unknown' }],
  nothere: [404, { reason: 'repo_not_here', why: 'repo_not_here' }],
  author: [409, { reason: 'work_refused', why: 'author_not_allowed', author: 'dana-w' }], epicauthor: [409, { reason: 'work_refused', why: 'epic_author_not_allowed', author: 'dana-w' }],
  unreadable: [409, { reason: 'work_refused', why: 'unreadable' }], bypass: [409, { reason: 'work_refused', why: 'bypass_refused' }], posture: [409, { reason: 'work_refused', why: 'posture_refused' }],
  lanes: [409, { reason: 'work_refused', why: 'lanes_full' }], launch: [409, { reason: 'work_refused', why: 'launch_failed' }],
  taken: [409, { reason: 'work_refused', why: 'taken', winner: 'willow-3f' }], lost: [409, { reason: 'work_refused', why: 'lost', winner: 'willow-3f' }],
  unknown: [409, { reason: 'work_refused', why: 'claim_unknown', unknown_reason: 'too_long' }], writesoff: [409, { reason: 'work_refused', why: 'writes_off' }],
  closed: [409, { reason: 'work_refused', why: 'closed' }], done: [409, { reason: 'work_refused', why: 'done' }], attempts: [409, { reason: 'work_refused', why: 'attempts_exhausted' }],
  offline: [503, { reason: 'offline' }], scope: [409, { reason: 'scope' }], unconfirmed: [504, { reason: 'unconfirmed' }],
};
const demoEpicWorked = {};
function demoWorkFlag() { try { const m = /[?&]work=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }
function demoEpicWork(handle, body, fx) {
  const flag = demoWorkFlag();
  if (flag && ownKey(DEMO_WORK, flag) && !(flag === 'lanes' && body.force === true)) {
    const [status, b] = DEMO_WORK[flag];
    const e = demoError(status, 'played');
    e.body = { ...b, error: 'played', node: handle };
    throw e;
  }
  demoEpicWorked[`${body.repo}#${body.issue}`] = handle;
  const node = (((fx.details || {})['example/website#147'] || {}).nodes || []).find((n) => n.handle === handle);
  const id = 50 + (Number(body.issue) % 50);
  return { node: handle, age: 0, data: { claimed: true, reason: 'won', task_ref: `${handle}:${id}`, id, branch: `worktree-issue-${Number(body.issue)}`, tmux: `demo--board--t-issue-${Number(body.issue)}`, repo: body.repo, issue: body.issue, name: node ? node.name : handle } };
}
function demoClaimFlag() { try { const m = /[?&]claim=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }
function demoEpicClaim(handle, body, fx) {
  const flag = demoClaimFlag();
  if (flag && ownKey(DEMO_CLAIM, flag)) {
    const [status, b] = DEMO_CLAIM[flag];
    const e = demoError(status, 'played');
    e.body = { ...b, error: 'played', node: handle };
    throw e;
  }
  demoEpicClaimed[`${body.repo}#${body.issue}`] = handle;
  const node = (((fx.details || {})['example/website#147'] || {}).nodes || []).find((n) => n.handle === handle);
  return { node: handle, age: 0, data: { claimed: true, reason: 'won', repo: body.repo, issue: body.issue, winner: node ? node.name : handle, attempt: 1 } };
}

function demoRunFlag() { try { const m = /[?&]run=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }
function demoEpicFlag() { try { const m = /[?&]epic=([a-z]+)/.exec(location.search); return m ? m[1] : ''; } catch (_) { return ''; } }

async function demoEpics(method, path, body) {
  const bare = path.split('?')[0];
  await new Promise((resolve) => setTimeout(resolve, 60));
  const r = await fetch('/static/demo/epic.json');
  if (!r.ok) throw new Error(`demo fixture epic.json: ${r.status} ${r.statusText}`);
  const fx = await r.json();
  const flag = demoEpicFlag();
  const v = flag && ownKey(fx.variants || {}, flag) ? fx.variants[flag] : null;
  if (v && v.status) throw demoError(v.status, v.error);
  if (bare === '/api/epics') { const l = { ...fx.list, ...((v && v.list) || {}) }; return typeof demoNewListed === 'function' ? demoNewListed(l) : l; }          // epics the visitor made with New epic come first
  const cm = /^\/api\/nodes\/([^/]+)\/epics\/claim$/.exec(bare);
  if (cm) return demoEpicClaim(decodeURIComponent(cm[1]), body || {}, fx);
  const m = /^\/api\/epics\/([^/]+)\/([^/]+)\/([^/]+?)(\/settings|\/run|\/pause|\/resume|\/route|\/issues\/[0-9]+\/(?:run|work))?$/.exec(bare);
  if (!m) return {};
  const key = `${decodeURIComponent(m[1])}/${decodeURIComponent(m[2])}#${m[3]}`.toLowerCase();
  const base = (fx.details && ownKey(fx.details, key) ? fx.details[key] : null) || (typeof demoNewBase === 'function' ? demoNewBase(fx, key) : null);          // or an epic the visitor made with New epic (epic-new.js)
  const shell = (d) => (d.epic && d.epic.number ? d : { ...d, epic: { ...d.epic, repo: `${m[1]}/${m[2]}`, owner: m[1], name: m[2], number: Number(m[3]), url: `${EP_GH}${m[1]}/${m[2]}/issues/${m[3]}` } });
  if (!base && method === 'GET') return shell({ ...fx.blank, error: (((fx.variants || {}).notfound || {}).detail || {}).error || null });
  const eff = { ...(base || {}), ...((v && v.detail) || {}) };                     // the detail as the variant lays it over the base: what the visitor starts from
  const made = demoEpicMade[key] || (demoEpicMade[key] = { pins: { ...(eff.pins || {}) }, epic_pin: eff.epic_pin || null, paused: !!eff.paused,
    report: epReportWord(eff.report), auto_pr: !!eff.auto_pr, close: !!eff.close, max_window_pct: Number.isInteger(eff.max_window_pct) ? eff.max_window_pct : 85,
    max_window_pct_set: eff.max_window_pct_set === true, group: Array.isArray(eff.group) ? eff.group : [], mcp_run: eff.mcp_run === true, run: false, why: {} });
  const nodes = eff.nodes || [];
  const tail = m[4] || '';
  const route0 = () => ({ ...(eff.route || {}), paused: made.paused, max_window_pct: made.max_window_pct, mcp_run: made.mcp_run });
  if (method === 'PUT' && tail === '/settings') {
    const b = body || {};
    const okHandle = (h) => h === 'local' || nodes.some((n) => n.handle === h);
    for (const [k, h] of Object.entries(b.pins || {})) {
      if (h && !okHandle(h)) throw demoError(400, 'a pin names a node that is not paired with this board (or not a node handle)');
      if (h) made.pins[k] = { handle: h, node_name: (nodes.find((n) => n.handle === h) || {}).name || null, source: 'page', stale: false }; else delete made.pins[k];
    }
    if (ownKey(b, 'epic_pin')) {
      if (b.epic_pin && !okHandle(b.epic_pin)) throw demoError(400, 'epic_pin names a node that is not paired with this board (or not a node handle)');
      made.epic_pin = b.epic_pin ? { handle: b.epic_pin, node_name: (nodes.find((n) => n.handle === b.epic_pin) || {}).name || null, source: 'page', stale: false } : null;
    }
    if (typeof b.paused === 'boolean') made.paused = b.paused;
    if (typeof b.report === 'string' && EP_REPORTS.some(([k]) => k === b.report)) made.report = b.report;
    if (typeof b.auto_pr === 'boolean') made.auto_pr = b.auto_pr;
    if (typeof b.close === 'boolean') made.close = b.close;
    if (typeof b.mcp_run === 'boolean') made.mcp_run = b.mcp_run;
    if (ownKey(b, 'max_window_pct')) {
      if (b.max_window_pct === null) { made.max_window_pct = 85; made.max_window_pct_set = false; }
      else if (Number.isInteger(b.max_window_pct) && b.max_window_pct >= 10 && b.max_window_pct <= 100) { made.max_window_pct = b.max_window_pct; made.max_window_pct_set = true; }
      else throw demoError(400, 'max_window_pct is a whole number from 10 to 100, or null for the board\'s setting');
    }
    if (ownKey(b, 'group')) {
      const g = b.group === null ? [] : b.group;
      const seen = new Set();
      let bad = !Array.isArray(g);
      if (!bad) {
        for (const x of g) {
          if (!Array.isArray(x) || x.length < 2 || new Set(x).size !== x.length || x.some((h) => h === 'local' || !nodes.some((n) => n.handle === h) || seen.has(h))) { bad = true; break; }
          x.forEach((h) => seen.add(h));
        }
      }
      if (bad) throw demoError(400, 'a group names two or more different paired nodes, and a node is in one group only');
      made.group = g;
    }
    return { pins: made.pins, epic_pin: made.epic_pin, paused: made.paused, report: made.report, auto_pr: made.auto_pr, close: made.close, plan: null, nodes,
      max_window_pct: made.max_window_pct, max_window_pct_set: made.max_window_pct_set, group: made.group, mcp_run: made.mcp_run };
  }
  const elig = (c) => c.state === 'open' && c.claim_state === 'none' && !c.claim && c.author_ok !== false;
  const hereNode = () => nodes.find((n) => n.handle !== 'local' && n.status === 'online' && n.tasks !== false) || null;
  if (method === 'POST' && (tail === '/pause' || tail === '/resume')) {
    made.paused = tail === '/pause';
    return { paused: made.paused, route: { ...route0(), running: made.run || (eff.route || {}).running === true } };
  }
  if (method === 'POST' && tail === '/run') {
    const r0 = eff.route || {};
    if (r0.can_run === false) { const e = demoError(409, 'played'); e.body = { error: (r0.blocked && r0.blocked.text) || 'Run is not available.', reason: (r0.blocked && r0.blocked.code) || 'writes_off' }; throw e; }
    made.paused = false;                                              // Run lifts a pause, as the board does
    made.run = true;
    return { route: { ...route0(), running: true, by: 'you', via: 'page', started_at: eff.server_now || null, ended: null } };
  }
  if (method === 'GET' && tail === '/route') return { route: { ...route0(), running: made.run || (eff.route || {}).running === true }, preview: null };
  const wk = /^\/issues\/([0-9]+)\/work$/.exec(tail);                  // Work on <node> (issue #150): the board checks the routing filters, then the node works the issue
  if (method === 'POST' && wk) {
    const b = body || {};
    return demoEpicWork(typeof b.node === 'string' ? b.node : '', { repo: typeof b.repo === 'string' ? b.repo : `${m[1]}/${m[2]}`, issue: Number(wk[1]), force: b.force === true }, fx);
  }
  const im = /^\/issues\/([0-9]+)\/run$/.exec(tail);
  if (method === 'POST' && im) {
    const n = Number(im[1]);
    const c = (eff.children || []).find((x) => x.number === n);
    const refuse = (status, error, reason, extra) => { const e = demoError(status, 'played'); e.body = { ok: false, error, reason, ...(extra || {}) }; throw e; };
    const pick = demoRunFlag();
    if (!c) refuse(404, 'that issue is not one of the epic\'s issues this board has read', 'not_found');
    if (pick === 'notfound') refuse(404, 'that issue is not one of the epic\'s issues this board has read', 'not_found');
    if (pick === 'claimed') refuse(409, `That issue cannot be sent now: held by ${(c.bucket && c.bucket.node) || 'a node'}.`, 'running');
    if (pick === 'busy') refuse(409, 'The run is busy sending other issues. Try again in a moment.', 'busy');
    const want = (body && body.node) || 'auto';
    const pinned = made.pins[String(n)] && !made.pins[String(n)].stale ? made.pins[String(n)].handle : null;
    const node = want === 'auto' ? (pinned ? nodes.find((x) => x.handle === pinned) : hereNode()) : nodes.find((x) => x.handle === want);
    const waiting = (text, code, until) => refuse(409, text, 'waiting', { waiting: { code, reason: text.replace(/^waiting: /, ''), until: null, until_iso: until || null, text, pinned: want === 'auto' ? null : want }, refused: [] });
    if (pick === 'waiting') waiting('waiting: node-b 5-hour window is at 90 percent until 14:10', 'window', '2026-10-11T14:10:00Z');
    if (!node || node.handle === 'local') waiting(want === 'auto' && !pinned ? 'waiting: no node can take it' : `waiting: ${want} is not a node paired with this board`, 'no_nodes', null);
    if (node.status !== 'online') waiting(`waiting: ${node.name}${want === 'auto' ? '' : ' (named by you)'} is ${node.status}`, node.status, null);
    if (node.tasks === false) waiting(`waiting: ${node.name}${want === 'auto' ? '' : ' (named by you)'} has no tasks scope for this board`, 'scope', null);
    demoEpicWorked[`${c.repo}#${c.number}`] = node.handle;
    made.why[`${c.repo}#${c.number}`] = `chose ${node.name}: 2 free lanes, 5-hour window 31 percent${want === 'auto' && !pinned ? '' : `, pinned by ${want === 'auto' ? 'the issue\'s pin' : 'you'}`}`;
    const id = 50 + (n % 50);
    return { ok: true, handle: node.handle, name: node.name, why: made.why[`${c.repo}#${c.number}`], data: { claimed: true, reason: 'won', task_ref: `${node.handle}:${id}`, id, branch: `worktree-issue-${n}`, tmux: `demo--board--t-issue-${n}`, repo: c.repo, issue: n } };
  }
  const route = { ...route0() };
  let sent = null;
  if (made.run) {                                                     // a Run in the demo: every ready issue goes to the first online node that holds the tasks scope
    const nd = hereNode();
    route.running = true; route.by = 'you'; route.via = 'page'; route.started_at = eff.server_now || null; route.ended = null;
    if (nd) {
      sent = {};
      for (const c of eff.children || []) if (elig(c) && !ownKey(demoEpicWorked, `${c.repo}#${c.number}`)) sent[c.number] = nd;
      route.assigned = { ...(route.assigned || {}) };
      for (const [n, h] of Object.entries(sent)) route.assigned[n] = { handle: h.handle, node: h.name, why: `chose ${h.name}: 2 free lanes, 5-hour window 31 percent`, at: eff.server_now || null, task_ref: null, reason: 'won' };
    }
  }
  const out = shell({ ...eff, pins: made.pins, epic_pin: made.epic_pin, paused: made.paused, report: made.report, auto_pr: made.auto_pr, close: made.close, route,
    max_window_pct: made.max_window_pct, max_window_pct_set: made.max_window_pct_set, group: made.group, mcp_run: made.mcp_run });
  const kids = out.children || [];
  if (!kids.length) return out;
  const patch = (v && v.child) || {};                                   // a variant that changes single children (by issue number) without a copy of all of them
  return { ...out, children: kids.map((c0) => {
    const c = ownKey(patch, String(c0.number)) ? { ...c0, ...patch[String(c0.number)] } : c0;
    const k = `${c.repo}#${c.number}`;
    const wh = ownKey(demoEpicWorked, k) ? demoEpicWorked[k] : null;
    const run = !wh && sent && ownKey(sent, String(c.number)) ? sent[String(c.number)] : null;
    const h = wh || (run && run.handle) || (ownKey(demoEpicClaimed, k) ? demoEpicClaimed[k] : null);
    const n = h ? (out.nodes || []).find((x) => x.handle === h) : null;
    if (!n) return c;
    const claim = { alias: n.name, node: h, status: wh ? 'running' : 'claimed', since: out.server_now, renewed_at: out.server_now, live: true, attempt: 1, seq: 1, task_ref: wh ? `${h}:${50 + (c.number % 50)}` : null, branch: wh ? `worktree-issue-${c.number}` : null };
    const why = made.why[k] || `chose ${n.name}: 2 free lanes, 5-hour window 31 percent`;
    const chosen = wh || run ? { route: { state: 'assigned', text: why, node: n.name, handle: h, until: null, code: null, at: out.server_now } } : {};
    return { ...c, ...chosen, claim_state: 'claimed', claim, bucket: { node: n.name, state: wh ? 'running' : 'queued' }, phase: wh ? 'running' : 'queued',
      task: wh ? { ref: claim.task_ref, phase: 'running', node: h, node_name: n.name, status: 'online', age_s: 0, updated_at: out.server_now } : c.task };
  }) };
}
