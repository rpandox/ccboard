/* ccboard epic view (issue #147), a lazy bundle (lazy.js 'epic', with pages/epic.css): the page #/epic/<owner>/<repo>/<n>, the Epics list of the Tasks page, the epic rows of the
   command palette and the demo's answers for /api/epics. Classic script, definition-only at load: nothing here touches the DOM, the network or a timer until a page, the list
   or the demo asks (registerPage is the one call made at load).

   The page READS GitHub through the board (GET /api/epics, GET /api/epics/<owner>/<repo>/<n>) and never writes to it. Run is shown disabled until the routing phase wires it.
   It writes three things: this board's own settings, PUT /api/epics/<owner>/<repo>/<n>/settings {pins, epic_pin, paused, report, auto_pr, close}, and, only when the board says
   GitHub writes are on (CCBOARD_EPIC_WRITES), a person's ask to a paired node to claim one issue (POST /api/nodes/<handle>/epics/claim {repo, issue, epic}, issue #148) or to
   work one: claim it, start it as a task on the node and report on the issue (POST /api/nodes/<handle>/epics/work {repo, issue, epic, force?}, issue #149). The node does the
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
const EP_RUN_WHY = 'available when routing is installed';
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
    attempts_exhausted: `Three attempts on ${issue} ended without a result. ${name} asked for a person and stopped.`,
    not_epic_child: `${name} does not take ${issue} as an issue of this epic. Nothing was claimed.`,
    other_repo: `${name} does not have this repository as one of its projects. Nothing was claimed.`,
    authors_unknown: `${name} cannot tell whose claims count, so it claimed nothing.`,
    login_not_allowed: `The gh login of ${name} is not one whose claims count. Nothing was claimed.`,
    no_login: `gh is not logged in on ${name}. Nothing was claimed.`,
    no_access: `The gh login of ${name} cannot push to that repository, so a claim from it would not count. Nothing was claimed.`,
    busy: `${name} is busy with other claims right now. Try ${issue} again in a moment. Nothing was claimed.`,
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
    unconfirmed: work ? `${name} did not answer in time, so it is not known whether it started ${issue}. Check its tasks and the issue on GitHub. Nothing was retried.`
      : `${name} did not answer in time, so it is not known whether ${issue} was claimed. Check the issue on GitHub. Nothing was retried.`,
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
  const own = {
    repo_not_here: `${name} does not have this repository cloned, so ${issue} was not claimed.`,
    author_not_allowed: `${issue} was written by ${author}, ${EP_NOT_ALLOWED}. Nothing was claimed.`,
    epic_author_not_allowed: `The epic was written by ${author}, ${EP_NOT_ALLOWED}. Nothing in it will run. Nothing was claimed.`,
    unreadable: `${name} could not read ${issue} or its epic from GitHub just now. Try again in a moment. Nothing was claimed.`,
    bypass_refused: `${issue} asks for bypass permissions. ${name} refused to run it, said so on the issue and released the claim.`,
    posture_refused: `${issue} asks for a permission setting that ${name} does not use for work from an issue. ${name} refused to run it and released the claim.`,
    lanes_full: `${name} already runs as many tasks as its lane cap advises. Nothing was claimed. You can start ${issue} anyway.`,
    launch_failed: `${name} could not start the task for ${issue} and released the claim.`,
  };
  if (b.reason === 'repo_not_here' || b.why === 'repo_not_here') return own.repo_not_here;
  if (b.reason === 'work_refused' && ownKey(own, b.why)) return own[b.why];
  return epClaimSentence({ status: e && e.status, message: e && e.message, body: b.reason === 'work_refused' ? { ...b, reason: 'claim_refused' } : b }, name, n, true);
}

/* The bits of the claim UI of one row, added under the pin: the action when it is offered, else why not (writes off, offline, no scope, no pin), then the last refusal. */
function epClaimBits(P, c) {
  const d = P.data;
  const out = [];
  const key = `${c.repo}#${c.number}`;
  const note = P.notes.get(key);
  if (epClaimable(d, c)) {
    const node = epClaimNode(d, c);
    const busy = P.claiming.get(key);
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
  if (note) out.push(el('span', { class: 'ep-claimhint warn ep-claimnote', role: 'status', 'data-claim-note': note.why || 'error', text: note.text }));
  return out;
}

/* Ask the paired node to claim the issue. The row shows "Claiming on <node>…" at once; the node's answer (or its refusal, in plain words) replaces it, and a refused claim puts
   the row back as it was. The next poll only confirms. */
function epClaim(P, c, node) {
  const key = `${c.repo}#${c.number}`;
  if (P.claiming.has(key) || epicPage.cur !== P) return null;
  P.claiming.set(key, node.handle);
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
    P.notes.set(key, { why: (e && e.body && (e.body.why || e.body.reason)) || 'error', text });
    toast(text, { kind: 'bad' });
    epPaint(P);
    const b = e && e.body;
    if (b && b.reason === 'claim_refused' && (b.why === 'taken' || b.why === 'lost' || b.why === 'done' || b.why === 'closed')) epFetch(P);       // the thread changed: read it again
  });
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
function epWorkOffer(P, c) {
  const d = P.data;
  if (!d || c.state !== 'open' || typeof d.writes !== 'boolean') return null;
  const node = epWorkNode(d, c);
  if (!node) return null;
  const a = d.authors && typeof d.authors === 'object' ? d.authors : null;
  const who = typeof c.author === 'string' && c.author ? `written by ${c.author}, ${EP_NOT_ALLOWED}` : 'not written by an author on the allowed list';
  let off = null;
  let text = '';
  if (d.writes === false) { off = 'writes'; text = EP_WRITES_OFF; }
  else if (node.status !== 'online') { off = 'offline'; text = `${node.name} is ${node.status}: it cannot work.`; }
  else if (node.tasks !== true) { off = 'scope'; text = `${node.name} is paired without the tasks scope.`; }
  else if (a && a.epic_ok === false) { off = 'epic_author'; text = `The epic was written by ${a.epic || 'someone'}, ${EP_NOT_ALLOWED}. Nothing in it will run.`; }
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
    if (!epClaimable(P.data, c) || offer.off === 'author' || offer.off === 'epic_author' || offer.off === 'authors_unknown') out.push(el('span', { class: 'ep-claimhint warn', 'data-work-hint': offer.off, text: offer.text }));
  }
  if (note) {
    out.push(el('span', { class: 'ep-claimhint warn ep-claimnote', role: 'status', 'data-work-note': note.why || 'error', text: note.text }));
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
  }
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
  const body = { repo: c.repo, issue: c.number, epic: epRef(P.owner, P.repo, P.n) };
  if (force === true) body.force = true;
  const undo = () => { const k = epWorkKid(P, c); if (k && w.snap) Object.assign(k, w.snap); };
  return api('POST', `/api/nodes/${encodeURIComponent(node.handle)}/epics/work`, body).then((r) => {
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
    P.wnotes.set(key, { why: (e && e.body && (e.body.why || e.body.reason)) || 'error', text });
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

/* The phase of the work on one issue, from this board's own work record: a glyph and words, never colour alone. Null when this board holds no record (a remote node's task shows
   as "Task running on <node>"). */
function epWorkPhaseNode(c) {
  const w = c.work;
  if (!w || typeof w !== 'object') return null;
  const p = ownKey(EP_WORK_PHASE, w.phase) ? w.phase : 'running';
  const [g, words] = EP_WORK_PHASE[p];
  const attn = p === 'waiting' || p === 'lost' || p === 'failed';
  const more = [];
  if (w.attempt > 1) more.push(`attempt ${w.attempt}`);
  if (typeof w.branch === 'string' && w.branch) more.push(`branch ${w.branch}`);
  if (p === 'pr' && w.pr_number) more.push(`PR #${w.pr_number}`);
  return el('span', { class: 'ep-phase' + (attn ? ' warn' : ''), 'data-phase': p, title: p === 'lost' ? 'This board lost the claim. It writes nothing more for it. The task is parked and its worktree is kept.' : null },
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
  return el('section', { class: 'ep-sec ep-cfg hidden', 'aria-label': 'When a node works an issue', 'data-cfg': '' },
    el('h2', { class: 'ep-h', text: 'When a node works an issue' }),
    el('div', { class: 'ep-cfg-row' }, el('span', { class: 'ep-cfg-l', id: 'ep-cfg-report', text: 'What it posts on the issue' }), seg),
    pr.row, cl.row, P.cfgPlan);
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
  setText(P.cfgPlan, epPlanText(d));
}

/* Run names what it would skip: the epic's author, or each issue by an author who is not on the allowed list. Run itself stays disabled until routing is installed. */
function epRunAuthors(d) {
  const a = d.authors && typeof d.authors === 'object' ? d.authors : null;
  if (!a || d.writes !== true) return '';
  if (a.epic_ok === false) return `Run would skip everything: the epic was written by ${a.epic || 'someone'}, ${EP_NOT_ALLOWED}.`;
  const b = Array.isArray(a.blocked) ? a.blocked.filter((x) => x && Number.isInteger(x.number)) : [];
  if (!b.length) return '';
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
  return JSON.stringify([c, epPinOf(P.data, c), P.data.server_now, P.data.nodes, P.data.writes, epOwn(P.data, c), P.claiming.get(key) || null, P.notes.get(key) || null, P.data.epic_pin, P.working.has(key), P.wnotes.get(key) || null, P.data.authors || null]);
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
  return el('div', { class: 'ep-pinbox' }, btn, text ? el('span', { class: 'ep-pintext' + (pin.stale ? ' warn' : ' dim'), text }) : null, ...epWorkBits(P, c), ...epClaimBits(P, c));
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
  if (c.state === 'open' && c.author_ok === false) meta.append(el('span', { class: 'ep-author warn', 'data-author': 'blocked', title: 'The node runs only issues by an allowed author, because an issue body becomes the prompt of an agent that edits files.' },
    el('span', { class: 'ep-wg', 'aria-hidden': 'true', text: '⚠ ' }), c.author ? `by ${c.author}, ${EP_NOT_ALLOWED}` : 'author not on the allowed list'));
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
}

/* ---------- the page ---------- */

function epPath(P, tail) { return `/api/epics/${encodeURIComponent(P.owner)}/${encodeURIComponent(P.repo)}/${encodeURIComponent(P.n)}${tail || ''}`; }

function epSettingsUndo(P, snap) {
  P.data.pins = snap.pins; P.data.epic_pin = snap.epic_pin; P.data.paused = snap.paused; P.data.report = snap.report; P.data.auto_pr = snap.auto_pr; P.data.close = snap.close;
}

/* One write to this board's settings, painted first: the new value shows at once and a refusal puts the old one back (with the board's words in a toast). */
function epWrite(P, patch, apply) {
  const snap = { pins: { ...(P.data.pins || {}) }, epic_pin: P.data.epic_pin, paused: P.data.paused, report: P.data.report, auto_pr: P.data.auto_pr, close: P.data.close };
  apply();
  epPaint(P);
  const id = (P.writes = (P.writes || 0) + 1);
  return api('PUT', epPath(P, '/settings'), patch).then((r) => {
    if (epicPage.cur !== P) return;
    if (r && typeof r === 'object' && r.pins && id === P.writes) { P.data.pins = r.pins; P.data.epic_pin = r.epic_pin || null; P.data.paused = !!r.paused; if (ownKey(r, 'report')) P.data.report = epReportWord(r.report); if (ownKey(r, 'auto_pr')) P.data.auto_pr = r.auto_pr === true; if (ownKey(r, 'close')) P.data.close = r.close === true; if (Array.isArray(r.nodes)) P.data.nodes = r.nodes.map((n) => ({ ...(P.data.nodes || []).find((x) => x.handle === n.handle), ...n })); }
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

function epSetPaused(P, on) {
  return epWrite(P, { paused: on }, () => { P.data.paused = on; });
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
  P.pause.textContent = d.paused ? 'Resume' : 'Pause';
  P.pause.setAttribute('aria-pressed', d.paused ? 'true' : 'false');
  P.pause.title = d.paused ? 'Resume routing this epic on this board' : 'Pause routing this epic on this board';
  setText(P.pauseWhy, d.paused ? 'Paused on this board: it does not route this epic. Nothing changes on GitHub.' : EP_PAUSE_WHY);
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
    if (d && typeof d === 'object' && d.epic) { P.data = d; P.loaded = true; epRemember(); epWorkReapply(P); }
    epPaint(P);
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
    pins: {}, epic_pin: null, paused: false, nodes: [], rate: null, server_now: null, error: null, stale_age_s: null, empty: false, writes: null, own_claims: [] };
}

function epMount(root, route) {
  const q = route.params || {};
  const P = { id: ++epicPage.ids, owner: q.owner, repo: q.repo, n: q.n, data: null, failed: null, busy: null, timer: null, sleeping: false, loaded: false, where: new Map(), lists: {}, secs: {}, counts2: {}, writes: 0, claiming: new Map(), notes: new Map(), working: new Map(), worked: new Map(), wnotes: new Map(), toasted: new Set() };
  P.title = el('h1', { class: 'ep-h1', text: `Epic #${P.n}` });
  P.name = el('span', { class: 'ep-name mono' });
  P.counts = el('span', { class: 'ep-counts' });
  P.rate = el('span', { class: 'dim ep-rate' });
  P.run = el('button', { class: 'primary ep-run', type: 'button', disabled: true, 'aria-describedby': 'ep-run-why', title: EP_RUN_WHY, text: 'Run' });
  P.pause = el('button', { class: 'ep-pause', type: 'button', 'aria-pressed': 'false', text: 'Pause', onclick: () => { if (P.data) epSetPaused(P, !P.data.paused); } });
  P.pauseWhy = el('p', { class: 'dim ep-why', text: EP_PAUSE_WHY });
  P.runAuthors = el('p', { class: 'ep-why warn hidden', 'data-run-authors': '' });
  P.epicPin = el('span', { class: 'dim ep-epicpin', text: 'Epic: no pin' });
  P.epicPinBtn = el('button', { class: 'small ep-pin-btn', type: 'button', title: 'Pin the whole epic to a node (kept on this board only; nothing changes on GitHub)', 'aria-label': 'Pin the whole epic to a node' }, ic('pin'), 'Pin epic');
  menu(P.epicPinBtn, () => epPinItems(P, P.data && P.data.epic_pin, (h) => epSetEpicPin(P, h)));
  P.head = el('header', { class: 'ep-head' },
    el('div', { class: 'ep-crumb' }, el('a', { class: 'ep-back', href: '#/tasks?tab=epics', text: 'Epics' }), el('span', { class: 'dim', 'aria-hidden': 'true', text: ' / ' }), P.name),
    el('div', { class: 'page-head ep-titlerow' }, P.title, el('div', { class: 'actions ep-actions' }, P.pause, P.run)),
    el('div', { class: 'ep-sub' }, P.counts, P.rate),
    el('p', { class: 'dim ep-why', id: 'ep-run-why', text: `Run: ${EP_RUN_WHY}.` }),
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
  epFetch(P);
}

function epUnmount() {
  const P = epicPage.cur;
  if (!P) return;
  epicPage.cur = null;
  if (P.timer) clearTimeout(P.timer);
  P.timer = null;
  document.removeEventListener('visibilitychange', P.onVis);
}

registerPage('epic', {
  title(route) { const q = (route && route.params) || {}; return q.owner ? `${epRef(q.owner, q.repo, q.n)} · epic` : 'Epic'; },
  mount: epMount,
  update() {},
  unmount: epUnmount,
});

/* ---------- the Epics list (the Tasks page's Epics tab) ---------- */

/* The list answer, once: a second ask while one is out gets the same promise; a recent answer (30 s) is reused. A refusal leaves the last answer in place. */
Epics.fetchList = function (force) {
  if (Epics.pending) return Epics.pending;
  if (!force && Epics.list && Date.now() - Epics.at < 30000) return Promise.resolve(Epics.list);
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
  const counts = c ? `${c.open} open, ${c.closed} closed${c.more ? ', more issues than shown' : ''}` : 'issues not counted yet';
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
  if (d.repos && d.repos.length) L.body.append(el('p', { class: 'dim ep-lnote', text: `Looked in ${d.repos.length} ${d.repos.length === 1 ? 'repository' : 'repositories'}: ${d.repos.slice(0, 4).join(', ')}${d.repos.length > 4 ? ' and more' : ''}.${d.repos_truncated ? ' This board has more repositories than it looks in.' : ''}` }));
  const rate = epRateText(d.rate);
  if (rate) L.body.append(el('p', { class: 'dim ep-lnote', text: rate }));
}

function epListTick(L) {
  if (Epics.host !== L) return;
  if (L.timer) { clearTimeout(L.timer); L.timer = null; }
  const err = Epics.list && Epics.list.error;
  const wait = err && err.reason === 'rate_limited' && typeof err.wait_s === 'number' ? Math.min(EP_WAIT_MAX_MS, Math.max(5000, (err.wait_s + 1) * 1000)) : EP_LIST_POLL_MS;
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
    el('div', { class: 'actions' }, el('button', { class: 'small', type: 'button', text: 'Refresh', onclick: () => epListLoad(L, true) }))), L.body);
  Epics.host = L;
  epListPaint(L);
  epListLoad(L, false);
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
  if (bare === '/api/epics') return { ...fx.list, ...((v && v.list) || {}) };
  const cm = /^\/api\/nodes\/([^/]+)\/epics\/claim$/.exec(bare);
  if (cm) return demoEpicClaim(decodeURIComponent(cm[1]), body || {}, fx);
  const wm = /^\/api\/nodes\/([^/]+)\/epics\/work$/.exec(bare);
  if (wm) return demoEpicWork(decodeURIComponent(wm[1]), body || {}, fx);
  const m = /^\/api\/epics\/([^/]+)\/([^/]+)\/([^/]+?)(\/settings)?$/.exec(bare);
  if (!m) return {};
  const key = `${decodeURIComponent(m[1])}/${decodeURIComponent(m[2])}#${m[3]}`.toLowerCase();
  const base = fx.details && ownKey(fx.details, key) ? fx.details[key] : null;
  const shell = (d) => (d.epic && d.epic.number ? d : { ...d, epic: { ...d.epic, repo: `${m[1]}/${m[2]}`, owner: m[1], name: m[2], number: Number(m[3]), url: `${EP_GH}${m[1]}/${m[2]}/issues/${m[3]}` } });
  if (!base && method === 'GET') return shell({ ...fx.blank, error: (((fx.variants || {}).notfound || {}).detail || {}).error || null });
  const made = demoEpicMade[key] || (demoEpicMade[key] = { pins: { ...(base ? base.pins : {}) }, epic_pin: base ? base.epic_pin : null, paused: base ? base.paused : false,
    report: epReportWord(base ? base.report : null), auto_pr: !!(base && base.auto_pr), close: !!(base && base.close) });
  const nodes = (base && base.nodes) || [];
  if (method === 'PUT') {
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
    return { pins: made.pins, epic_pin: made.epic_pin, paused: made.paused, report: made.report, auto_pr: made.auto_pr, close: made.close, plan: null, nodes };
  }
  const out = shell({ ...base, ...((v && v.detail) || {}), pins: made.pins, epic_pin: made.epic_pin, paused: made.paused, report: made.report, auto_pr: made.auto_pr, close: made.close });
  const kids = out.children || [];
  if (!kids.length) return out;
  const patch = (v && v.child) || {};                                   // a variant that changes single children (by issue number) without a copy of all of them
  return { ...out, children: kids.map((c0) => {
    const c = ownKey(patch, String(c0.number)) ? { ...c0, ...patch[String(c0.number)] } : c0;
    const k = `${c.repo}#${c.number}`;
    const wh = ownKey(demoEpicWorked, k) ? demoEpicWorked[k] : null;
    const h = wh || (ownKey(demoEpicClaimed, k) ? demoEpicClaimed[k] : null);
    const n = h ? (out.nodes || []).find((x) => x.handle === h) : null;
    if (!n) return c;
    const claim = { alias: n.name, node: h, status: wh ? 'running' : 'claimed', since: out.server_now, renewed_at: out.server_now, live: true, attempt: 1, seq: 1, task_ref: wh ? `${h}:${50 + (c.number % 50)}` : null, branch: wh ? `worktree-issue-${c.number}` : null };
    return { ...c, claim_state: 'claimed', claim, bucket: { node: n.name, state: wh ? 'running' : 'queued' }, phase: wh ? 'running' : 'queued',
      task: wh ? { ref: claim.task_ref, phase: 'running', node: h, node_name: n.name, status: 'online', age_s: 0, updated_at: out.server_now } : c.task };
  }) };
}
