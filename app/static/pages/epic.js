/* ccboard epic view (issue #147), a lazy bundle (lazy.js 'epic', with pages/epic.css): the page #/epic/<owner>/<repo>/<n>, the Epics list of the Tasks page, the epic rows of the
   command palette and the demo's answers for /api/epics. Classic script, definition-only at load: nothing here touches the DOM, the network or a timer until a page, the list
   or the demo asks (registerPage is the one call made at load).

   The page only READS GitHub (through the board: GET /api/epics, GET /api/epics/<owner>/<repo>/<n>). It claims nothing, runs nothing and comments on nothing; Run is shown
   disabled until the routing phase wires it. The two things it writes are this board's own: PUT /api/epics/<owner>/<repo>/<n>/settings {pins, epic_pin, paused}.

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
const EP_GH = 'https://github.com/';    // the demo and the blank page build the epic's own address from the route's checked parts; every other link is the board's

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
  return JSON.stringify([c, epPinOf(P.data, c), P.data.server_now, P.data.nodes]);
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
  return el('div', { class: 'ep-pinbox' }, btn, text ? el('span', { class: 'ep-pintext' + (pin.stale ? ' warn' : ' dim'), text }) : null);
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
  const readAgo = c.state === 'open' && c.claims_read_at ? epSince(c.claims_read_at, data.server_now) : '';
  if (readAgo) meta.append(el('span', { class: 'dim ep-read', title: 'When the board last read this issue\'s comments, by GitHub\'s clock. "Unclaimed" is only true for that moment.', text: `claims read ${readAgo}` }));
  if (task) meta.append(el('span', { class: 'ep-task', text: task }));
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
  P.data.pins = snap.pins; P.data.epic_pin = snap.epic_pin; P.data.paused = snap.paused;
}

/* One write to this board's settings, painted first: the new value shows at once and a refusal puts the old one back (with the board's words in a toast). */
function epWrite(P, patch, apply) {
  const snap = { pins: { ...(P.data.pins || {}) }, epic_pin: P.data.epic_pin, paused: P.data.paused };
  apply();
  epPaint(P);
  const id = (P.writes = (P.writes || 0) + 1);
  return api('PUT', epPath(P, '/settings'), patch).then((r) => {
    if (epicPage.cur !== P) return;
    if (r && typeof r === 'object' && r.pins && id === P.writes) { P.data.pins = r.pins; P.data.epic_pin = r.epic_pin || null; P.data.paused = !!r.paused; if (Array.isArray(r.nodes)) P.data.nodes = r.nodes.map((n) => ({ ...(P.data.nodes || []).find((x) => x.handle === n.handle), ...n })); }
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
    if (d && typeof d === 'object' && d.epic) { P.data = d; P.loaded = true; epRemember(); }
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
    pins: {}, epic_pin: null, paused: false, nodes: [], rate: null, server_now: null, error: null, stale_age_s: null, empty: false };
}

function epMount(root, route) {
  const q = route.params || {};
  const P = { id: ++epicPage.ids, owner: q.owner, repo: q.repo, n: q.n, data: null, failed: null, busy: null, timer: null, sleeping: false, loaded: false, where: new Map(), lists: {}, secs: {}, counts2: {}, writes: 0 };
  P.title = el('h1', { class: 'ep-h1', text: `Epic #${P.n}` });
  P.name = el('span', { class: 'ep-name mono' });
  P.counts = el('span', { class: 'ep-counts' });
  P.rate = el('span', { class: 'dim ep-rate' });
  P.run = el('button', { class: 'primary ep-run', type: 'button', disabled: true, 'aria-describedby': 'ep-run-why', title: EP_RUN_WHY, text: 'Run' });
  P.pause = el('button', { class: 'ep-pause', type: 'button', 'aria-pressed': 'false', text: 'Pause', onclick: () => { if (P.data) epSetPaused(P, !P.data.paused); } });
  P.pauseWhy = el('p', { class: 'dim ep-why', text: EP_PAUSE_WHY });
  P.epicPin = el('span', { class: 'dim ep-epicpin', text: 'Epic: no pin' });
  P.epicPinBtn = el('button', { class: 'small ep-pin-btn', type: 'button', title: 'Pin the whole epic to a node (kept on this board only; nothing changes on GitHub)', 'aria-label': 'Pin the whole epic to a node' }, ic('pin'), 'Pin epic');
  menu(P.epicPinBtn, () => epPinItems(P, P.data && P.data.epic_pin, (h) => epSetEpicPin(P, h)));
  P.head = el('header', { class: 'ep-head' },
    el('div', { class: 'ep-crumb' }, el('a', { class: 'ep-back', href: '#/tasks?tab=epics', text: 'Epics' }), el('span', { class: 'dim', 'aria-hidden': 'true', text: ' / ' }), P.name),
    el('div', { class: 'page-head ep-titlerow' }, P.title, el('div', { class: 'actions ep-actions' }, P.pause, P.run)),
    el('div', { class: 'ep-sub' }, P.counts, P.rate),
    el('p', { class: 'dim ep-why', id: 'ep-run-why', text: `Run: ${EP_RUN_WHY}.` }),
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
  root.append(el('div', { class: 'ep ep-page' }, P.head, P.banner, P.skel, P.empty, P.stripSec, sec('nodes', 'On nodes'), sec('free', 'Unclaimed'), sec('unknown', 'Claim not known')));
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
  const m = /^\/api\/epics\/([^/]+)\/([^/]+)\/([^/]+?)(\/settings)?$/.exec(bare);
  if (!m) return {};
  const key = `${decodeURIComponent(m[1])}/${decodeURIComponent(m[2])}#${m[3]}`.toLowerCase();
  const base = fx.details && ownKey(fx.details, key) ? fx.details[key] : null;
  const shell = (d) => (d.epic && d.epic.number ? d : { ...d, epic: { ...d.epic, repo: `${m[1]}/${m[2]}`, owner: m[1], name: m[2], number: Number(m[3]), url: `${EP_GH}${m[1]}/${m[2]}/issues/${m[3]}` } });
  if (!base && method === 'GET') return shell({ ...fx.blank, error: (((fx.variants || {}).notfound || {}).detail || {}).error || null });
  const made = demoEpicMade[key] || (demoEpicMade[key] = { pins: { ...(base ? base.pins : {}) }, epic_pin: base ? base.epic_pin : null, paused: base ? base.paused : false });
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
    return { pins: made.pins, epic_pin: made.epic_pin, paused: made.paused, report: false, auto_pr: false, nodes };
  }
  return shell({ ...base, ...((v && v.detail) || {}), pins: made.pins, epic_pin: made.epic_pin, paused: made.paused });
}
