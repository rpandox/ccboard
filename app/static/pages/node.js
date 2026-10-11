/* ccboard node pages (issue #139): #/n/<handle> (one paired node, read only), #/n/<handle>/s/<tmux> (a session of that node) and #/n/<handle>/t/<id> (a task of that node). Part of the lazy
   'nodeshub' bundle with nodes-hub.js, which holds the model (Nodes.get, Nodes.M), the row builders (nhSessionRow, nhTaskRow, nhList) and the words (Nodes.view).

   The node page: a header (the node's name, its status as glyph and word with the age of the reading, OS, version, Open board in a new tab), a banner when the reading is old, refused or
   skewed, then the sections Needs you, Sessions, Tasks, Repos and Account windows. Every row has the node chip and opens its peek. A peek shows the row's fields from the last reading, an
   "Open on <node>" link to the node's own page (a new tab; no iframe) and the controls that need a relay action, present but disabled with their reason (Nodes.off). Nothing here asks
   another node: the reading is what this board's hub polled (GET /api/nodes/state). A handle this board does not know, or any #/n/ address while no node is paired, gets the plain "This
   node is not paired" page. Every string of a node is peer data: textContent only.

   Issues #141 and #143: New task here and New session here open the launcher (launcher.js, with launcher-node.js) with this node chosen when the pair holds the scope and the node can be
   called (Nodes.can), else they stay off with the reason; a backlog task's peek has Start (the launcher in dispatch mode, on that node); and the session peek shows the tail of
   the session (Live.subscribe with a remote Ref: one stream per node through this board's own origin) when the pair holds the `sessions` scope. */
'use strict';

const NODE_ERR = {
  timeout: 'did not answer in time', refused: 'refused the connection', tls: 'did not give a valid certificate', http_5xx: 'answered with an error',
  bad_body: 'sent an answer this board could not read', too_large: 'sent more than a reading may hold', identity_changed: 'is a different board than the one that was paired',
};

/* The plain page for an address no paired node answers to (also what #/n/ shows on a board with no node). */
function nodeNotPaired(host, handle) {
  host.textContent = '';
  const empty = pageEmpty('info-sign', 'This node is not paired', `Nothing is paired as "${handle || ''}" on this board. Pair it in Settings, then Nodes.`);
  empty.append(el('a', { class: 'btn', href: '#/settings?sec=nodes', text: 'Open Settings, Nodes' }));   // inside the empty state, centred like its text
  host.append(empty);
}

function nodeAge(rec) {
  const age = Nodes.ageOf(rec);
  return age === null ? 'never' : `${nhSpan(age)} ago`;
}

/* One line of text under a header about how old the reading is and why it may be wrong. */
function nodeNotes(rec) {
  const M = Nodes.M;
  const v = Nodes.view(rec);
  const out = [];
  const reauth = v.status === 'unauthorized' || v.status === 'unpaired';
  if (v.status === 'unauthorized') out.push({ cls: 'bad', text: `${nhName(rec)} no longer accepts the token this board saved (it was revoked or replaced there). Pair it again to see it here.`, repair: true });
  else if (v.status === 'unpaired') out.push({ cls: 'bad', text: `${nhName(rec)} does not answer as the board that was paired (${rec.error_kind === 'identity_changed' ? 'it is a different board now' : 'it has no node routes'}). Remove it and pair it again.`, repair: true });
  if (!reauth && v.status !== 'online') {
    out.push({ cls: 'warn', text: rec.state ? `Showing the last reading, from ${nodeAge(rec)}. ${rec.error_kind ? nhName(rec) + ' ' + (NODE_ERR[rec.error_kind] || 'did not answer') + '.' : ''}`.trim()
      : `Nothing has been read from ${nhName(rec)} yet${rec.error_kind ? `: it ${NODE_ERR[rec.error_kind] || 'did not answer'}` : ''}. It may be asleep or off the tailnet.` });
  } else if (!reauth && rec.error_kind) out.push({ cls: 'warn', text: `The last try failed: ${nhName(rec)} ${NODE_ERR[rec.error_kind] || 'did not answer'}. Showing the reading before it.` });
  if (rec.skew_warn) out.push({ cls: 'warn', text: `Its clock differs from this board's by ${Math.round(Math.abs(rec.skew_ms || 0) / 1000)} s, so ages from ${nhName(rec)} may be off.` });
  if (rec.state && rec.state.truncated) out.push({ cls: 'dim', text: 'The reading is cut to the newest sessions and tasks.' });
  if (M.err) out.push({ cls: 'warn', text: `This page could not refresh (${M.err}). Showing the answer from ${nhSpan((Date.now() - M.recvAt) / 1000)} ago.` });
  return out;
}

function nodeHeader(rec) {
  const v = Nodes.view(rec);
  const card = rec.card && typeof rec.card === 'object' ? rec.card : {};
  const os = NodeView.osName(card.os && (card.os.tailscale_os || card.os.system));
  const ver = (rec.state && rec.state.node && rec.state.node.version) || card.version;
  const facts = [os, ver ? `v${String(ver).replace(/^v/, '')}` : '', card.runtime || '', `handle ${rec.handle}`].filter(Boolean);
  const notes = nodeNotes(rec);
  const plat = NodeView.platform(card);
  const repair = notes.some((n) => n.repair);
  const open = nhOpenLink('Open board', rec, '', repair ? 'btn' : 'btn primary') || Nodes.off('Open board', 'the address saved for this node is not an https tailnet address');
  const acts = Nodes.acts(repair ? el('a', { class: 'btn primary', href: '#/settings?sec=nodes', text: 'Re-pair' }) : null, open, nodeNew(rec, 'task'), nodeNew(rec, 'session'));
  return el('header', { class: 'nd-head' },
    el('div', { class: 'page-head' }, el('h1', { class: 'nd-title', text: nhName(rec) }), nhStatusLine(v)),
    el('p', { class: 'dim nd-facts', text: facts.join(' · ') }),
    plat.name ? el('div', { class: 'nd-plat' }, NodeView.chip(plat.name, '', '', 'Platform'), el('span', { class: 'dim nd-load', text: `Load: ${plat.load}` }),
      ...Nodes.platNotes(card, plat.name).map((t) => el('p', { class: 'dim nd-note', text: t }))) : null,
    ...notes.map((n) => el('p', { class: `nd-banner ${n.cls}`, role: 'status', text: n.text })), acts,
    el('p', { class: 'dim nd-ro', text: 'Read only here, except starting a task or a session, the tail of a session and, with the sessions and permissions scopes, replying to a session, pressing keys, acknowledging, closing it and answering its permission requests.' }));
}

/* New task here / New session here: a button that opens the launcher on this node when the pair holds the scope and the node can be called, else the disabled control with its reason. */
function nodeNew(rec, kind) {
  const task = kind === 'task';
  const c = Nodes.can(rec, task ? 'tasks' : 'sessions');
  if (!c.ok) return Nodes.off(task ? 'New task here' : 'New session here', c.why, task ? 'New task' : 'New session');
  return el('button', { class: 'small', type: 'button', text: task ? 'New task here' : 'New session here', onclick: () => nodeLaunch(rec, kind) });
}

/* Open the launcher with `rec` chosen. The local place (the repo this board last used) comes along, so "This node" stays a choice in the sheet; none is fine (the picker then keeps
   "This node" off). */
function nodeLaunch(rec, kind, extra) {
  const pl = kind !== 'dispatch' && typeof launchPlace === 'function' ? launchPlace(null, kind) : null;
  if (typeof launch === 'function') launch({ mode: kind, node: rec.handle, ...(pl ? { project: pl.project, repo: pl.repo } : {}), ...(extra || {}) });
}

function nodeHeaderSig(rec) {
  const c = rec.card || {};
  return JSON.stringify([rec.status, rec.name, rec.url, rec.error_kind, rec.skew_warn, rec.scopes, rec.last_ok_at, c.version, c.os, c.runtime, c.load, c.accounts && c.accounts.supported, rec.state && rec.state.truncated,
    Nodes.M.err, Math.floor(Date.now() / 60000), Math.floor((Nodes.ageOf(rec) || 0) / 60)]);
}

function nodeRepoRows(rec) {
  const out = [];
  for (const p of (rec.state && Array.isArray(rec.state.projects) ? rec.state.projects : [])) {
    for (const r of (Array.isArray(p.repos) ? p.repos : [])) out.push({ key: `${p.name}/${r.name}`, project: p.name, name: r.name, slug: r.slug, branch: r.branch, dirty: !!r.dirty });
  }
  return out;
}

function nodeRepoRow(r) {
  return el('div', { class: 'nd-item nd-repo' }, el('span', { class: 'nd-g' }, ic('git-repo')), el('span', { class: 'nd-name', text: `${r.project}/${r.name === 'root' ? 'project folder' : r.name}` }),
    el('span', { class: 'dim nd-where', text: r.slug || 'no GitHub remote' }), el('span', { class: 'dim nd-meta', text: [r.branch, r.dirty ? 'uncommitted changes' : ''].filter(Boolean).join(' · ') }));
}

/* The windows of the node's accounts: the Claude and Codex windows of its state, then the accounts of its card (a label, never an address). */
function nodeWindows(rec) {
  const rows = [];
  const usage = rec.state && rec.state.usage && typeof rec.state.usage === 'object' ? rec.state.usage : {};
  for (const agent of ['claude', 'codex']) {
    const w = usage[agent];
    if (w && typeof w === 'object') rows.push({ key: `u:${agent}`, agent, label: agent === 'claude' ? 'Claude' : 'Codex', w, kind: 'window' });
  }
  const items = rec.card && rec.card.accounts && Array.isArray(rec.card.accounts.items) ? rec.card.accounts.items : [];
  items.forEach((a, i) => { if (a && typeof a === 'object') rows.push({ key: `a:${i}:${a.agent}:${a.label}`, agent: a.agent, label: a.label || 'account', w: a.window, current: !!a.current, limited: !!a.limited, kind: 'account' }); });
  return rows;
}

function nodeWindowRow(r) {
  const w = r.w && typeof r.w === 'object' ? r.w : null;
  const known = !!(w && w.known && typeof w.pct === 'number');
  const reset = known && typeof w.resets_at === 'number' && w.resets_at > 0 ? ` · resets in ${fmtIn(w.resets_at)}` : '';
  const lim = r.limited || (w && w.limited);
  return el('div', { class: 'nd-item nd-win' + (lim ? ' attn' : '') }, el('span', { class: 'nd-g' }, agentGlyph(nhAgent(r.agent))),
    el('span', { class: 'nd-name', text: r.kind === 'window' ? `${r.label} window` : r.label }),
    el('span', { class: 'dim nd-where', text: r.kind === 'account' ? (r.current ? 'in use' : 'saved') : '5-hour window' }),
    el('span', { class: 'nd-meta' }, el('span', { class: lim ? 'warn' : 'dim', text: known ? `${Math.round(w.pct)}%${lim ? ' · limit reached' : ''}${reset}` : 'no reading yet' })));
}

/* The page of one node. Built once per node; its lists are keyed and repaint with every reading (6 s), the header only when something it shows changed. */
function nodePageBuild(host, rec) {
  const head = el('div', { class: 'nd-headhost' });
  const empties = {};
  const mk = (id, title, rowsOf, key, build, sig, why) => {
    const list = el('div', { class: 'roster nd-list' });
    const empty = el('p', { class: 'dim nd-empty hidden' });
    empties[id] = { list: nhList(list, key, build, sig), empty, count: null, rowsOf, why, title };
    const countNode = el('span', { class: 'nd-count dim' });
    empties[id].count = countNode;
    return el('section', { class: 'nd-sec', 'data-sec': id }, el('h2', { class: 'nd-h' }, title, countNode), list, empty);
  };
  const sKey = (s) => Ref.key({ tmux: s.tmux, node: s.node });
  const tKey = (t) => Ref.key({ kind: 'task', node: t.node, id: t.id });
  const needs = mk('needs', 'Needs you', (r) => { const owned = new Set(Nodes.permCards().filter((c) => !c.stub && c.node === r.handle).map((c) => c.tmux)); return Nodes.sessions(r.handle).filter((s) => s.needs_you && !owned.has(s.tmux)); },
    sKey, nhSessionRow, nhSessionSig, (r) => `Nothing needs you on ${nhName(r)}.`);
  const sess = mk('sessions', 'Sessions', (r) => Nodes.sessions(r.handle), sKey, nhSessionRow, nhSessionSig, (r) => `No live session in the last reading of ${nhName(r)}. Start one on its own board (Open board).`);
  const tasks = mk('tasks', 'Tasks', (r) => Nodes.tasks(r.handle), tKey, nhTaskRow, nhTaskSig, (r) => `No task in the last reading of ${nhName(r)}. Open its board to add one.`);
  const repos = mk('repos', 'Repos', nodeRepoRows, (r) => r.key, nodeRepoRow, (r) => JSON.stringify(r), (r) => `No repo in the last reading of ${nhName(r)}.`);
  const wins = mk('windows', 'Account windows', nodeWindows, (r) => r.key, nodeWindowRow, (r) => JSON.stringify([r, Math.floor(Date.now() / 60000)]), (r) => `No account reading from ${nhName(r)} yet. Log in to Claude or Codex on its board.`);
  const permNote = el('p', { class: 'dim nd-perm hidden' });
  const permWrap = el('div', { class: 'roster nd-list nd-perms' });
  const permList = nhList(permWrap, (c) => c.key, nsPermNode, nsPermSig);
  const sessList = needs.querySelector('.nd-list');
  needs.insertBefore(permNote, sessList);
  needs.insertBefore(permWrap, sessList);
  host.append(head, needs, sess, tasks, repos, wins);
  let sig = '';
  return function paint(r) {
    const s = nodeHeaderSig(r);
    if (s !== sig) { sig = s; head.textContent = ''; head.append(nodeHeader(r)); }
    for (const e of Object.values(empties)) {
      const rows = e.rowsOf(r);
      e.list.update(rows);
      setTextIfChanged(e.count, ` (${rows.length})`);
      const perm = e === empties.needs ? Number(r.state && r.state.needs_you && r.state.needs_you.permissions) || 0 : 0;
      const none = !rows.length && !perm;
      setTextIfChanged(e.empty, none ? e.why(r) : '');
      e.empty.classList.toggle('hidden', !none);
    }
    const perm = Number(r.state && r.state.needs_you && r.state.needs_you.permissions) || 0;
    const cards = Nodes.permCards().filter((c) => !c.stub && c.node === r.handle);
    permList.update(cards);
    const can = Nodes.can(r, 'permissions');
    const word = `${perm} permission request${perm === 1 ? '' : 's'}`;
    setTextIfChanged(permNote, perm && !cards.length ? (can.ok ? `${word} waiting on ${nhName(r)}. Reading ${perm === 1 ? 'it' : 'them'} from the node…` : `${word} waiting on ${nhName(r)}. Allow and Deny: ${can.why}. You can answer on its board (Open board).`) : '');
    permNote.classList.toggle('hidden', !(perm && !cards.length));
  };
}

/* ---------- the read only peeks ---------- */

function nodeFacts(rows) {
  const dl = el('dl', { class: 'nd-facts-dl' });
  for (const [k, v] of rows) {
    if (v === null || v === undefined || v === '') continue;
    dl.append(el('dt', { class: 'k', text: k }), el('dd', { class: 'nd-v' }, typeof v === 'string' ? v : v));
  }
  return dl;
}

function nodePeekBack(rec, tab) {
  return el('a', { class: 'btn small', href: Ref.hash(Ref.node(rec.handle)) || '#/', text: `Back to ${nhName(rec)}` });
}

/* The tail of a remote session (issue #143): the last captured lines, kept across the peek's repaints (the peek is rebuilt with every reading; the stream is not). Every line is
   peer data and goes into a <pre> through textContent. A `gone` keeps the lines and says why in plain words. Returns {node, stop()}. */
const NODE_TAIL_GONE = {
  offline: 'offline', upstream_error: 'could not be reached', closed: 'the stream ended', idle: 'the stream went quiet',
  repair: 'needs a new pairing: its token was refused', unpaired: 'answers as another node now', revoked: 'the pair was removed',
  not_read_yet: 'has not been read yet', too_large: 'sent a line that was too long', shutdown: 'this board is stopping',
};
function nodeClock(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
function nodeTailWords(gone, name) {
  if (!gone) return '';
  const at = nodeClock(gone.seen);
  const seen = at ? `last seen ${at}` : 'no lines seen yet';
  if (gone.reason === 'offline') return `offline, ${seen}`;
  const why = ownKey(NODE_TAIL_GONE, gone.reason) ? NODE_TAIL_GONE[gone.reason] : 'the stream ended';
  return !ownKey(NODE_TAIL_GONE, gone.reason) || gone.reason === 'upstream_error' || gone.reason === 'closed' || gone.reason === 'idle' ? `${name}: ${why}, ${seen}` : `${name}: ${why}`;
}

function nodeTailMake(handle, tmux) {
  const ref = Ref.session(handle, tmux);
  if (!ref) return null;
  const pre = el('pre', { class: 'nd-tail-pre', tabindex: '0', role: 'log', 'aria-label': `Tail of ${tmux}` });
  const st = el('p', { class: 'dim nd-tail-st', role: 'status', text: 'Waiting for the first lines…' });
  const node = el('section', { class: 'nd-tail', 'aria-label': 'Tail' },
    el('h2', { class: 'nd-h', text: 'Tail' }), el('p', { class: 'dim nd-ro', text: 'The last captured lines of the session, not a terminal. Secrets on the screen are hidden on a best-effort basis.' }), pre, st);
  let got = false;
  const off = Live.subscribe(ref, (lines, meta) => {
    got = got || (Array.isArray(lines) && lines.length > 0);
    const rec = Nodes.get(handle);
    setTextIfChanged(pre, Array.isArray(lines) ? lines.join('\n') : '');
    const gone = meta && meta.gone ? meta.gone : null;
    node.classList.toggle('gone', !!gone);
    setTextIfChanged(st, gone ? nodeTailWords(gone, rec ? nhName(rec) : handle) : got ? 'Live: updates as the session prints.' : 'Waiting for the first lines…');
  });
  return { node, stop() { off(); } };
}

/* The facts and the actions of a session's peek, from the last reading. `steer`: the steer panel is on the page (the pair holds the sessions scope), so the page's filled primary is its
   Send and Open on <node> is a plain button; without it the controls that need the scope stay, disabled, with their reason. */
function nodeSessionParts(rec, s, steer) {
  const v = Nodes.view(rec);
  const st = ownKey(STATE_GLYPH, s.state) ? s.state : 'unknown';
  const name = nhName(rec);
  const open = nhOpenLink(`Open on ${name}`, rec, Ref.isTmux(s.tmux) ? `#/s/${s.tmux}` : '', steer ? 'btn' : 'btn primary') || Nodes.off(`Open on ${name}`, 'the address saved for this node is not an https tailnet address');
  const top = [
    el('div', { class: 'page-head' }, el('h1', { class: 'nd-title', text: String(s.session || s.tmux) }), Nodes.chip(rec.handle)),
    el('p', { class: 'dim nd-ro', text: steer ? `The facts are from the last reading of ${name}, ${nodeAge(rec)}${v.status === 'online' ? '' : ` (${v.word})`}. The panel below acts on ${name} now.`
      : `Read only: the last reading of ${name}, ${nodeAge(rec)}${v.status === 'online' ? '' : ` (${v.word})`}.` }),
    nodeFacts([['State', el('span', {}, stateGlyph(st), ' ', GLYPH_LABEL[st] + (s.needs_you && st !== 'waiting' ? ', needs you' : ''))], ['Agent', nhAgent(s.agent)], ['Model', s.model], ['Where', nhWhere(s)],
      ['Started as', s.kind], ['Since', s.since ? `${nhAgeText(s.since)} ago` : ''], ['Session', s.tmux]])];
  const pc = Nodes.can(rec, 'permissions');
  const answer = steer ? (s.needs_you && nsWaiting(rec) > 0 && !pc.ok ? Nodes.off('Answer', pc.why) : null) : (s.needs_you ? Nodes.off('Answer', Nodes.reason(rec, 'sessions')) : null);
  const acts = steer ? Nodes.acts(open, nodePeekBack(rec), answer)
    : Nodes.acts(open, nodePeekBack(rec), Nodes.off('Send a prompt', Nodes.reason(rec, 'sessions')), answer, Nodes.off('Close session', Nodes.reason(rec, 'sessions')));
  return { top, acts };
}

/* The plain peek: no steer panel (the pair lacks the sessions scope). */
function nodeSessionPeek(rec, tmux, tail) {
  const s = Nodes.sessions(rec.handle).find((x) => x.tmux === tmux);
  if (!s) return nodePeekGone(rec, 'session', 'This session is not in the last reading of the node. It may have ended.');
  const p = nodeSessionParts(rec, s, false);
  return el('div', { class: 'nd-peek' }, ...p.top, tail ? tail.node : null, p.acts);
}

/* The peek with the steer panel: the facts above and the actions below are repainted with every reading; the tail and the panel in between are the same elements for as long as the
   peek lives, so a draft, a focus, an armed confirm and an open Keys panel survive. */
function nodeShellMake(tail, steer) {
  const top = el('div', { class: 'nd-peek-top' });
  const bot = el('div', { class: 'nd-peek-bot' });
  const root = el('div', { class: 'nd-peek' }, top, tail ? tail.node : null, steer.node, bot);
  return { root, paint(rec, s) { const p = nodeSessionParts(rec, s, true); top.textContent = ''; top.append(...p.top); bot.textContent = ''; bot.append(p.acts); } };
}

function nodeTaskPeek(rec, id) {
  const t = Nodes.tasks(rec.handle).find((x) => String(x.id) === String(id));
  if (!t) return nodePeekGone(rec, 'task', 'This task is not in the last reading of the node. It may be finished and cleared.');
  const v = Nodes.view(rec);
  const open = nhOpenLink(`Open on ${nhName(rec)}`, rec, '#/tasks', 'btn primary') || Nodes.off(`Open on ${nhName(rec)}`, 'the address saved for this node is not an https tailnet address');
  const sHref = t.tmux && Ref.isTmux(t.tmux) ? Ref.hash({ tmux: t.tmux, node: rec.handle }) : null;
  return el('div', { class: 'nd-peek' },
    el('div', { class: 'page-head' }, el('h1', { class: 'nd-title', text: String(t.title || `Task ${t.id}`) }), Nodes.chip(rec.handle)),
    el('p', { class: 'dim nd-ro', text: `Read only: the last reading of ${nhName(rec)}, ${nodeAge(rec)}${v.status === 'online' ? '' : ` (${v.word})`}.` }),
    nodeFacts([['Phase', t.phase], ['Agent', nhAgent(t.agent)], ['Where', nhWhere(t)], ['Branch', t.branch], ['Issue', t.issue_ref], ['Updated', t.updated_at ? `${nhAgeText(t.updated_at)} ago` : ''],
      ['Session', sHref ? el('a', { href: sHref, text: String(t.tmux) }) : t.tmux]]),
    Nodes.acts(open, nodePeekBack(rec), nodeStart(rec, t), Nodes.off('Run again', Nodes.reason(rec, 'tasks')), Nodes.off('Cancel task', Nodes.reason(rec, 'tasks'))));
}

/* Start on <node> for a card in the node's backlog: the launcher in dispatch mode, on the node the task lives on (a task id exists on one node only). */
function nodeStart(rec, t) {
  if (t.phase !== 'backlog') return null;
  const c = Nodes.can(rec, 'tasks');
  if (!c.ok) return Nodes.off(`Start on ${nhName(rec)}`, c.why, 'Start');
  return el('button', { class: 'small', type: 'button', text: `Start on ${nhName(rec)}`, onclick: () => nodeLaunch(rec, 'dispatch', { remoteTask: t }) });
}

function nodePeekGone(rec, what, text) {
  const e = pageEmpty('info-sign', `That ${what} is not here`, text);
  e.append(nodePeekBack(rec));
  return el('div', { class: 'nd-peek nd-gone', role: 'status' }, e);
}

/* ---------- steering a remote session (issue #142) ----------

   The steer panel of a session's peek: the permission request waiting on it (Allow, Deny), a quick-reply row and a one-line send box (Enter sends, Shift+Enter adds a line), Ack, a Keys
   panel (the closed list of Nodes.KEYS; C-c needs a second tap) and Kill session (two taps). It is built once per peek and kept across the readings (the peek around it is repainted
   every 6 s; a draft, an armed confirm and an open Keys panel must survive that). Every action goes through Nodes.sendPrompt / sendKey / ackSession / closeSession / permAnswer
   (nodes-hub.js), which refuse early with a plain sentence and never throw. The sentence stays in the panel beside the toast, so a missed toast loses nothing. A draft is
   cleared only when the node confirmed; a refusal or a timeout keeps it. */

const NODE_WHO = 'Reply, Keys, Ack and Kill session';

function nodeSteerMake(handle, tmux) {
  if (!Ref.session(handle, tmux)) return null;
  const killKey = `kill:${handle}/${tmux}`;
  const ctrlKey = `ckey:${handle}/${tmux}`;
  const whyId = `nd-sw-${handle}-${tmux}`;
  const who = { tmux, node: handle };
  const S = { rec: null, s: null, can: { ok: false, why: '' }, busy: false, keyBusy: false, closing: false, keysOpen: false, last: '', actSig: '', chipSig: '', queue: null };
  const nm = () => (S.rec ? nhName(S.rec) : handle);
  const sn = () => String((S.s && S.s.session) || tmux);
  const agent = () => nhAgent(S.s && S.s.agent);
  const takesText = () => !!S.s && agent() !== 'shell' && ['waiting', 'idle', 'done', 'working', 'errored'].includes(S.s.state);
  const takesKeys = () => !!S.s && agent() !== 'shell' && S.s.state !== 'ended';

  const permHead = el('h2', { class: 'nd-h hidden', text: 'Permission request' });
  const permList = el('div', { class: 'roster nd-list nd-perms' });
  const permNote = el('p', { class: 'dim nd-ro hidden', text: 'Allow and Deny go to the node through this board. The node trusts this board to pass on a person\'s choice and records the name this board reports; it cannot check it.' });
  const permKeys = nhList(permList, (c) => c.key, nsPermNode, nsPermSig);

  const chips = el('div', { class: 'chips nd-chips', role: 'group', 'aria-label': 'Quick replies' });
  const ta = composer({ placeholder: typeof sessionPlaceholder === 'function' ? sessionPlaceholder('send to', '') : 'send', label: 'Send a prompt to this session', onSend: () => sendText(ta.value, true) });
  ta.setAttribute('title', 'Send a prompt: Enter sends, Shift+Enter adds a line');
  const sendBtn = el('button', { class: 'small primary', type: 'submit', text: 'Send' });
  const form = el('form', { class: 'ib-send nd-send', onsubmit: (e) => { e.preventDefault(); sendText(ta.value, true); } }, ta, sendBtn);
  const echo = el('p', { class: 'dim nd-steer-echo hidden' });
  const status = el('p', { class: 'nd-steer-st hidden', role: 'status' });
  const queueSlot = el('span', { class: 'nd-queue' });
  const line = el('div', { class: 'nd-steer-line hidden' }, status, queueSlot);
  const replySec = el('div', { class: 'nd-ssec nd-reply' }, el('h2', { class: 'nd-h', text: 'Reply' }), chips, form, echo, line);
  const noText = el('p', { class: 'dim nd-ro hidden', text: 'This session cannot take a prompt: it is a shell or it has ended.' });
  const acts = el('div', { class: 'nd-steer-acts' });
  const keysBox = el('div', { class: 'nd-keys hidden', role: 'group', 'aria-label': 'Keys' });
  const why = el('p', { class: 'dim nd-swhys hidden' });
  const actSec = el('div', { class: 'nd-ssec nd-actions' }, el('h2', { class: 'nd-h', text: 'Session' }), acts, keysBox, why);
  const node = el('section', { class: 'nd-steer', 'aria-label': 'Steer this session' }, permHead, permList, permNote, replySec, noText, actSec);

  function show(kind, text) {
    status.className = `nd-steer-st ${kind}`;
    setTextIfChanged(status, text);
    status.classList.toggle('hidden', !text);
    line.classList.toggle('hidden', !text);
  }
  function echoLine(text) { setTextIfChanged(echo, text ? `› ${text}` : ''); echo.classList.toggle('hidden', !text); }
  function fail(r) { show(r.reason === 'busy' ? 'warn' : 'bad', r.text); Nodes.say(r.text, r.reason === 'busy' || r.reason === 'rate_limited' ? 'warn' : 'bad'); }

  /* A prompt: from the box (fromBox: the box is locked while the node answers, emptied only when it confirmed) or from a chip (the draft in the box is left alone). */
  async function sendText(text, fromBox, opts) {
    if (S.busy) return;
    const t = String(text === undefined || text === null ? '' : text);
    const first = t.replace(/\r\n?/g, '\n').trim().split('\n')[0].slice(0, 160);
    S.busy = true;
    S.last = t;
    queueSlot.textContent = '';
    if (fromBox) ta.disabled = true;
    sendBtn.disabled = true;
    show('info', `Sending to ${nm()}…`);
    echoLine(first);
    const r = await Nodes.sendPrompt(handle, tmux, t, opts);
    S.busy = false;
    if (fromBox) ta.disabled = false;
    sendBtn.disabled = false;
    if (r.ok) {
      if (fromBox) { ta.value = ''; if (typeof rowCleared === 'function') rowCleared(ta); }
      show('ok', r.queued ? `Queued on ${nm()}. It is typed when the session is ready.` : `Sent to ${nm()}.`);
      Nodes.say(r.queued ? `Queued for ${sn()} on ${nm()}` : `Sent to ${sn()} on ${nm()}`, 'ok');
    } else {
      echoLine('');
      fail(r);
      if (r.busy && r.busy.code === 'working') queueSlot.append(el('button', { class: 'small', type: 'button', title: 'Type it as soon as the session is ready', text: 'Queue it', onclick: () => sendText(S.last, fromBox, { queue: true }) }));
    }
    if (fromBox && typeof ta.focus === 'function') ta.focus();
  }

  async function doKey(key, confirm) {
    if (S.keyBusy) return;
    S.keyBusy = true;
    const word = Nodes.KEY_WORD[key] || key;
    const r = await Nodes.sendKey(handle, tmux, key, confirm === true);
    S.keyBusy = false;
    if (r.ok) { show('ok', `Sent ${word} to ${nm()}.`); Nodes.say(`Sent ${word} to ${sn()} on ${nm()}`, 'ok'); } else fail(r);
    paint();
  }

  async function doAck() {
    const r = await Nodes.ackSession(handle, tmux);
    if (r.ok) { show('ok', `Acknowledged on ${nm()}.`); Nodes.say(`Acknowledged ${sn()} on ${nm()}`, 'ok'); } else fail(r);
  }

  async function doClose() {
    S.closing = true;
    show('info', `Closing ${sn()} on ${nm()}…`);
    paint();
    const r = await Nodes.closeSession(handle, tmux);
    S.closing = false;
    if (r.ok) {
      Nodes.say(`Killed ${sn()} on ${nm()}`, 'ok');
      const back = Ref.hash(Ref.node(handle)) || '#/';
      if (typeof navigate === 'function') navigate(back); else if (typeof location !== 'undefined') location.hash = back;
    } else fail(r);
    paint();
  }

  /* The quick replies of this session: its agent's list until edited (components.js quickLoad, kept per node and session). A slash command is not sent to another node, so a reply that
     starts with one is left out of the row (it stays in the editor's list). */
  function fillChips() {
    if (!S.s || typeof quickLoad !== 'function') return;
    const ag = agent();
    const list = quickLoad(who, ag);
    const sig = JSON.stringify([ag, list, S.can.ok]);
    if (sig === S.chipSig) return;
    S.chipSig = sig;
    const edit = () => quickReplyEditor({ items: quickLoad(who, ag), defaults: quickDefaults(ag), onSave: (items) => { quickSave(who, items, ag); S.chipSig = ''; paint(); } });
    chips.textContent = '';
    for (const text of list) {
      if (/^\s*\//.test(text)) continue;
      chips.append(quickChip(text, { cls: 'chip-btn', onSend: () => sendText(text, false), onEdit: edit }));
    }
    chips.append(el('button', { class: 'icon minimal qr-edit', type: 'button', title: 'Edit quick replies', 'aria-label': 'Edit quick replies', onclick: edit }, ic('edit')));
  }

  function keyCell(k) {
    const word = Nodes.KEY_WORD[k] || k;
    return el('button', { class: 'small nd-key', type: 'button', 'data-key': k, title: `Send ${word} to ${sn()}`, text: word, onclick: () => doKey(k, false) });
  }

  function paintActs() {
    const ok = S.can.ok;
    const needs = !!(S.s && S.s.needs_you);
    const keysOn = ok && takesKeys();
    const open = keysOn && S.keysOpen;
    const sig = JSON.stringify([ok, S.can.why, needs, open, keysOn, ui.confirm === killKey, ui.confirm === ctrlKey, S.closing, nm(), sn()]);
    if (sig === S.actSig) return;
    S.actSig = sig;
    acts.textContent = '';
    const cells = [];
    if (needs) cells.push(ok ? el('button', { class: 'small', type: 'button', 'data-act': 'ack', title: 'Clears the attention mark on this session. It answers no request.', text: 'Ack', onclick: doAck }) : Nodes.off('Ack', S.can.why));
    if (takesKeys()) {
      if (ok) cells.push(el('button', { class: 'small' + (open ? ' on' : ''), type: 'button', 'data-act': 'keys', 'aria-expanded': open ? 'true' : 'false', title: 'Press a key in the session', text: 'Keys',
        onclick: () => { S.keysOpen = !S.keysOpen; S.actSig = ''; paintActs(); } }));
      else cells.push(Nodes.off('Keys', S.can.why));
    }
    acts.append(...cells);
    acts.append(el('div', { class: 'nd-kill' }, !ok ? Nodes.off('Kill session', S.can.why, 'Kill session')
      : S.closing ? el('button', { class: 'danger', type: 'button', disabled: true, text: `Closing on ${nm()}…` }) : confirmButton(killKey, `Kill session on ${nm()}`, doClose, false)));
    keysBox.textContent = '';
    keysBox.classList.toggle('hidden', !open);
    if (open) {
      for (const k of Nodes.KEYS) keysBox.append(keyCell(k));
      keysBox.append(el('div', { class: 'nd-ckey' }, confirmButton(ctrlKey, Nodes.CONFIRM_KEY, () => doKey(Nodes.CONFIRM_KEY, true), false)));
    }
    why.textContent = '';
    why.classList.toggle('hidden', ok);
    if (!ok) {
      why.append(el('span', { id: whyId, class: 'nd-why', text: `${NODE_WHO}: ${S.can.why}.` }));
      for (const b of Array.from(acts.querySelectorAll('.nd-off'))) b.setAttribute('aria-describedby', whyId);
    }
  }

  function paint() {
    if (!S.rec || !S.s) return;
    const ok = S.can.ok;
    const text = takesText();
    replySec.classList.toggle('hidden', !text);
    noText.classList.toggle('hidden', text || !S.s);
    sendBtn.classList.toggle('nd-off', !ok);
    if (ok) sendBtn.removeAttribute('aria-disabled'); else sendBtn.setAttribute('aria-disabled', 'true');
    sendBtn.setAttribute('title', ok ? 'Send this prompt' : S.can.why);
    node.classList.toggle('blocked', !ok);
    if (text) fillChips();
    const cards = Nodes.permCards().filter((c) => !c.stub && c.node === handle && c.tmux === tmux).map((c) => ({ ...c, lead: true }));
    permKeys.update(cards);
    permHead.classList.toggle('hidden', !cards.length);
    permNote.classList.toggle('hidden', !cards.length);
    paintActs();
  }

  const ctl = {
    handle, tmux, node, ta,
    update(rec, s) { S.rec = rec; S.s = s; S.can = Nodes.can(rec, 'sessions'); paint(); },
    focus() { if (!takesText() || typeof ta.focus !== 'function') return false; ta.focus(); return true; },
    ack() { if (!S.s || !S.s.needs_you) { Nodes.say(`${sn()} has nothing to acknowledge`, 'info'); return true; } doAck(); return true; },
    allow() { const c = Nodes.permFor(handle, tmux); if (!c) return false; Nodes.permAnswer(c, 'allow'); return true; },
    deny() { const c = Nodes.permFor(handle, tmux); if (!c) return false; Nodes.permAnswer(c, 'deny'); return true; },
    open() { const url = S.rec ? Nodes.openUrl(S.rec, Ref.isTmux(tmux) ? `#/s/${tmux}` : '') : null; if (!url) return false; window.open(url, '_blank', 'noopener,noreferrer'); return true; },
    stop() { if (Nodes.peek === ctl) Nodes.peek = null; },
  };
  keysBox.addEventListener('keydown', (e) => { if (e.key === 'Escape' && S.keysOpen) { e.preventDefault(); e.stopPropagation(); S.keysOpen = false; S.actSig = ''; paintActs(); const b = acts.querySelector('[data-act=keys]'); if (b) b.focus(); } });
  Nodes.peek = ctl;
  return ctl;
}

/* ---------- the three pages ---------- */

function nodePage(kind) {
  let host = null;
  let route = null;
  let mode = '';
  let paintBody = null;
  let peekSig = '';
  let tail = null;
  let steer = null;
  let shell = null;
  const stopTail = () => { if (tail) { tail.stop(); tail = null; } };
  const stopSteer = () => { if (steer) { steer.stop(); steer = null; } shell = null; };
  const title = () => {
    const h = route && route.params && route.params.node;
    const rec = h && Nodes.enabled() ? Nodes.get(h) : null;
    if (!rec) return 'Node';
    if (kind === 'node-session') return `${route.params.tmux} on ${nhName(rec)}`;
    if (kind === 'node-task') return `Task ${route.params.id} on ${nhName(rec)}`;
    return nhName(rec);
  };
  const paint = () => {
    if (!host || !route) return;
    if (!nhAttached(host)) { Nodes.M.subs.delete(paint); return; }
    const h = route.params.node;
    const M = Nodes.M;
    const rec = Nodes.enabled() ? Nodes.get(h) : null;
    const booting = typeof state === 'undefined' || !state;                  // a direct load of #/n/<handle>: the first /api/state has not arrived, so whether nodes exist is not known yet
    const want = !booting && (!Nodes.enabled() || (M.loaded && !rec)) ? 'none' : !rec ? 'loading' : kind === 'node' ? 'node' : 'peek';
    const key = `${want}|${h}|${route.params.tmux || ''}|${route.params.id || ''}`;
    if (key !== mode) {
      mode = key; paintBody = null; peekSig = '';
      stopTail();
      stopSteer();
      host.textContent = '';
      if (want === 'none') nodeNotPaired(host, h);
      else if (want === 'loading') host.append(el('p', { class: 'dim', role: 'status', text: M.err ? `Could not read the nodes (${M.err}). Trying again.` : 'Reading the nodes…' }));
      else if (want === 'node') paintBody = nodePageBuild(host, rec);
    }
    if (want === 'node' && paintBody) paintBody(rec);
    else if (want === 'peek') {
      let live = null;
      if (kind === 'node-session') {                                           // the tail and the steer panel live as long as the peek does, not as long as one reading
        live = Nodes.sessions(rec.handle).find((x) => x.tmux === route.params.tmux) || null;
        const here = !!live && Array.isArray(rec.scopes) && rec.scopes.includes('sessions');
        if (here && !tail && typeof Live !== 'undefined') tail = nodeTailMake(rec.handle, route.params.tmux);
        else if (!here) stopTail();
        if (here && !shell) {
          steer = nodeSteerMake(rec.handle, route.params.tmux);
          if (steer) { shell = nodeShellMake(tail, steer); host.textContent = ''; host.append(shell.root); peekSig = ''; }
        } else if (!here && shell) { stopSteer(); peekSig = ''; }
      }
      const s = JSON.stringify([rec, Math.floor(Date.now() / 60000), M.err, !!tail, !!shell, live && live.needs_you]);
      if (shell) { steer.update(rec, live); if (s !== peekSig) { peekSig = s; shell.paint(rec, live); } }
      else if (s !== peekSig) { peekSig = s; host.textContent = ''; host.append(kind === 'node-session' ? nodeSessionPeek(rec, route.params.tmux, tail) : nodeTaskPeek(rec, route.params.id)); }
    } else if (want === 'loading') {
      const p = host.firstChild;
      if (p && M.err) setTextIfChanged(p, `Could not read the nodes (${M.err}). Trying again.`);
    }
    if (typeof refreshTitle === 'function') refreshTitle();
  };
  return {
    title,
    mount(root, r) {
      route = r;
      mode = '';
      host = el('div', { class: 'nd-page page-narrow' + (kind === 'node' ? ' nd-wide' : ''), 'data-kind': kind });
      root.append(host);
      Nodes.M.subs.add(paint);
      paint();
      if (Nodes.enabled() && !Nodes.M.loaded) Nodes.poll({ force: true });
    },
    update() { paint(); },
    onRoute(r) { route = r; paint(); },
    unmount() { Nodes.M.subs.delete(paint); stopTail(); stopSteer(); host = null; mode = ''; paintBody = null; },
  };
}

registerPage('node', nodePage('node'));
registerPage('node-session', nodePage('node-session'));
registerPage('node-task', nodePage('node-task'));
