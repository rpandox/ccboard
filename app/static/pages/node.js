/* ccboard node pages (issue #139): #/n/<handle> (one paired node, read only), #/n/<handle>/s/<tmux> (a session of that node) and #/n/<handle>/t/<id> (a task of that node). Part of the lazy
   'nodeshub' bundle with nodes-hub.js, which holds the model (Nodes.get, Nodes.M), the row builders (nhSessionRow, nhTaskRow, nhList) and the words (Nodes.view).

   The node page: a header (the node's name, its status as glyph and word with the age of the reading, OS, version, Open board in a new tab), a banner when the reading is old, refused or
   skewed, then the sections Needs you, Sessions, Tasks, Repos and Account windows. Every row has the node chip and opens its peek. A peek shows the row's fields from the last reading, an
   "Open on <node>" link to the node's own page (a new tab; no iframe) and the controls that need a relay action, present but disabled with their reason (Nodes.off). Nothing here asks
   another node: the reading is what this board's hub polled (GET /api/nodes/state). A handle this board does not know, or any #/n/ address while no node is paired, gets the plain "This
   node is not paired" page. Every string of a node is peer data: textContent only. */
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
  const acts = Nodes.acts(repair ? el('a', { class: 'btn primary', href: '#/settings?sec=nodes', text: 'Re-pair' }) : null, open,
    Nodes.off('New task here', Nodes.reason(rec, 'tasks'), 'New task'), Nodes.off('New session here', Nodes.reason(rec, 'sessions'), 'New session'));
  return el('header', { class: 'nd-head' },
    el('div', { class: 'page-head' }, el('h1', { class: 'nd-title', text: nhName(rec) }), nhStatusLine(v)),
    el('p', { class: 'dim nd-facts', text: facts.join(' · ') }),
    plat.name ? el('div', { class: 'nd-plat' }, NodeView.chip(plat.name, '', '', 'Platform'), el('span', { class: 'dim nd-load', text: `Load: ${plat.load}` }),
      ...Nodes.platNotes(card, plat.name).map((t) => el('p', { class: 'dim nd-note', text: t }))) : null,
    ...notes.map((n) => el('p', { class: `nd-banner ${n.cls}`, role: 'status', text: n.text })), acts,
    el('p', { class: 'dim nd-ro', text: 'Read only here. Starting or answering things on another node arrives with the relay.' }));
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
  const needs = mk('needs', 'Needs you', (r) => Nodes.sessions(r.handle).filter((s) => s.needs_you), sKey, nhSessionRow, nhSessionSig, (r) => `Nothing needs you on ${nhName(r)}.`);
  const sess = mk('sessions', 'Sessions', (r) => Nodes.sessions(r.handle), sKey, nhSessionRow, nhSessionSig, (r) => `No live session in the last reading of ${nhName(r)}. Start one on its own board (Open board).`);
  const tasks = mk('tasks', 'Tasks', (r) => Nodes.tasks(r.handle), tKey, nhTaskRow, nhTaskSig, (r) => `No task in the last reading of ${nhName(r)}. Open its board to add one.`);
  const repos = mk('repos', 'Repos', nodeRepoRows, (r) => r.key, nodeRepoRow, (r) => JSON.stringify(r), (r) => `No repo in the last reading of ${nhName(r)}.`);
  const wins = mk('windows', 'Account windows', nodeWindows, (r) => r.key, nodeWindowRow, (r) => JSON.stringify([r, Math.floor(Date.now() / 60000)]), (r) => `No account reading from ${nhName(r)} yet. Log in to Claude or Codex on its board.`);
  const permNote = el('p', { class: 'dim nd-perm hidden' });
  needs.insertBefore(permNote, needs.querySelector('.nd-list'));
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
    setTextIfChanged(permNote, perm ? `${perm} permission request${perm === 1 ? '' : 's'} waiting on ${nhName(r)}. Answer on its board (Open board); answering from here arrives with the relay.` : '');
    permNote.classList.toggle('hidden', !perm);
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

function nodeSessionPeek(rec, tmux) {
  const s = Nodes.sessions(rec.handle).find((x) => x.tmux === tmux);
  if (!s) return nodePeekGone(rec, 'session', 'This session is not in the last reading of the node. It may have ended.');
  const v = Nodes.view(rec);
  const st = ownKey(STATE_GLYPH, s.state) ? s.state : 'unknown';
  const open = nhOpenLink(`Open on ${nhName(rec)}`, rec, Ref.isTmux(s.tmux) ? `#/s/${s.tmux}` : '', 'btn primary') || Nodes.off(`Open on ${nhName(rec)}`, 'the address saved for this node is not an https tailnet address');
  return el('div', { class: 'nd-peek' },
    el('div', { class: 'page-head' }, el('h1', { class: 'nd-title', text: String(s.session || s.tmux) }), Nodes.chip(rec.handle)),
    el('p', { class: 'dim nd-ro', text: `Read only: the last reading of ${nhName(rec)}, ${nodeAge(rec)}${v.status === 'online' ? '' : ` (${v.word})`}.` }),
    nodeFacts([['State', el('span', {}, stateGlyph(st), ' ', GLYPH_LABEL[st] + (s.needs_you && st !== 'waiting' ? ', needs you' : ''))], ['Agent', nhAgent(s.agent)], ['Model', s.model], ['Where', nhWhere(s)],
      ['Started as', s.kind], ['Since', s.since ? `${nhAgeText(s.since)} ago` : ''], ['Session', s.tmux]]),
    Nodes.acts(open, nodePeekBack(rec), Nodes.off('Send a prompt', Nodes.reason(rec, 'sessions')), s.needs_you ? Nodes.off('Answer', Nodes.reason(rec, 'sessions')) : null,
      Nodes.off('Close session', Nodes.reason(rec, 'sessions'))));
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
    Nodes.acts(open, nodePeekBack(rec), Nodes.off('Run again', Nodes.reason(rec, 'tasks')), Nodes.off('Cancel task', Nodes.reason(rec, 'tasks'))));
}

function nodePeekGone(rec, what, text) {
  const e = pageEmpty('info-sign', `That ${what} is not here`, text);
  e.append(nodePeekBack(rec));
  return el('div', { class: 'nd-peek nd-gone', role: 'status' }, e);
}

/* ---------- the three pages ---------- */

function nodePage(kind) {
  let host = null;
  let route = null;
  let mode = '';
  let paintBody = null;
  let peekSig = '';
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
      host.textContent = '';
      if (want === 'none') nodeNotPaired(host, h);
      else if (want === 'loading') host.append(el('p', { class: 'dim', role: 'status', text: M.err ? `Could not read the nodes (${M.err}). Trying again.` : 'Reading the nodes…' }));
      else if (want === 'node') paintBody = nodePageBuild(host, rec);
    }
    if (want === 'node' && paintBody) paintBody(rec);
    else if (want === 'peek') {
      const s = JSON.stringify([rec, Math.floor(Date.now() / 60000), M.err]);
      if (s !== peekSig) { peekSig = s; host.textContent = ''; host.append(kind === 'node-session' ? nodeSessionPeek(rec, route.params.tmux) : nodeTaskPeek(rec, route.params.id)); }
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
    unmount() { Nodes.M.subs.delete(paint); host = null; mode = ''; paintBody = null; },
  };
}

registerPage('node', nodePage('node'));
registerPage('node-session', nodePage('node-session'));
registerPage('node-task', nodePage('node-task'));
