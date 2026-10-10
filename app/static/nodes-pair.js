/* ccboard node pairing (issue #135, Settings > Nodes): the words and checks of the pairing flow (NodeView.peer, peerRow, auditWord, the scope table, the code and address
   readers) and the demo's playing of the writes (demoNodesWrite). Classic script, definition only: no DOM, storage, network, listener or timer at load. It is not in index.html:
   lazy.js loads it with the settings bundle, right before pages/settings.js, the one page that uses it (the first-paint pages never build a pair row). It extends NodeView
   (nodes.js) and, for the demo, reads demoNodesMade from core.js; both are there before any bundle loads. */

/* ---------------------------------------------------------------- pairing (issue #135; Settings > Nodes: the sheets, the paired list, who can control this node, Activity)

   The words and the checks of the pairing flow. Pure functions (no DOM, no network, no clock unless handed `nowMs`) except the builders, which call el() when they run.
   Nothing here holds a pairing code or a token: the sheets in pages/settings.js do, for as long as they are open, and empty themselves when they close.

   The board's answers are read loosely on purpose (a list may be a bare array or sit under one of a few keys; a name may be `name` or `peer_name`), and every word in them
   is shown through textContent. Times are the box's observed times (`last_seen`, `last_used_at`), never the time a pair was saved: a pair that was never reached says so. */
NodeView.SCOPES = [
  { id: 'read', label: 'Read', on: true, what: 'See this board\'s name, system and a summary of its sessions.' },
  { id: 'tasks', label: 'Tasks', on: true, what: 'Send tasks to this board and see how they go.' },
  { id: 'sessions', label: 'Sessions', on: false, what: 'Read what a session shows and type into it. Off unless you turn it on.' },
  { id: 'permissions', label: 'Permissions', on: false, what: 'See permission requests. A request is still answered only by a signed-in person on the calling board, never by the token alone. Off unless you turn it on.' },
];
NodeView.CODE_MINUTES = [1, 5, 10, 20, 30];
NodeView.CODE_DEFAULT_MINUTES = 10;
NodeView.ROTATE_GRACE_S = 60;

NodeView.str = function (v, max) { return typeof v === 'string' ? v.slice(0, max || 200) : ''; };

/* The known scopes of a list, in the table's order; a scope a newer board invented is left out rather than shown as a word we cannot explain. */
NodeView.scopeList = function (scopes) {
  const have = new Set(Array.isArray(scopes) ? scopes.filter((s) => typeof s === 'string') : []);
  return NodeView.SCOPES.filter((s) => have.has(s.id)).map((s) => s.id);
};

/* What a person types or pastes, as the code the board expects: 'K7Q2M-4XD9R'. Case, spaces and dashes do not matter; O reads as 0 and I or L as 1 (Crockford base32); U is
   no character of the code. { code, err }: err is a plain sentence for the field when the input cannot be a code. */
NodeView.code = function (input) {
  const s = (typeof input === 'string' ? input : '').toUpperCase().replace(/[\s-]+/g, '').replace(/O/g, '0').replace(/[IL]/g, '1');
  if (!/^[0-9A-HJKMNP-TV-Z]{10}$/.test(s)) return { code: '', err: 'A pairing code is 10 letters and numbers, like K7Q2M-4XD9R.' };
  return { code: `${s.slice(0, 5)}-${s.slice(5)}`, err: '' };
};

/* A code as it is shown: 'XXXXX-XXXXX' (upper case, a dash in the middle); anything that is not 10 letters or numbers is shown as it came. */
NodeView.formatCode = function (code) {
  const s = (typeof code === 'string' ? code : '').toUpperCase();
  const bare = s.replace(/[^0-9A-Z]/g, '');
  return bare.length === 10 ? `${bare.slice(0, 5)}-${bare.slice(5)}` : s;
};

/* The address of the other board, as the board is asked for it: 'https://host[:port]' and nothing more. A bare host gets https. { url, err }. The board checks the address again
   (the tailnet rule); this only catches what a person mistypes. */
NodeView.address = function (input) {
  let s = typeof input === 'string' ? input.trim() : '';
  if (!s) return { url: '', err: 'Type the address of the other board.' };
  if (!/^[A-Za-z][A-Za-z0-9+.-]*:\/\//.test(s)) s = `https://${s}`;
  if (/^http:\/\//i.test(s)) return { url: '', err: 'Use an https address: boards talk to each other over the tailnet with HTTPS.' };
  const m = /^https:\/\/([^/?#@\s]+)\/?$/i.exec(s);
  if (!m || m[1].length > 260 || !/^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])(?::\d{1,5})?$/.test(m[1])) {
    return { url: '', err: 'Give the host name and the port if there is one, like https://box.example.ts.net:8443, with no path.' };
  }
  const clean = 'https://' + m[1].toLowerCase();
  return { url: clean, err: '' };
};

/* The optional handle: '' (the board picks one from the node's name) or a valid handle; 'self' and 'local' are taken. { handle, err }. */
NodeView.handle = function (input) {
  const h = (typeof input === 'string' ? input : '').trim().toLowerCase();
  if (!h) return { handle: '', err: '' };
  if (h === 'self' || h === Ref.LOCAL) return { handle: '', err: `"${h}" is taken: pick another short name.` };
  if (!Ref.HANDLE_RE.test(h)) return { handle: '', err: 'Use 1 to 31 lower case letters, numbers or dashes, starting with a letter or number.' };
  return { handle: h, err: '' };
};

/* '9:41' for the time left (ms), '0:00' once it has run out. */
NodeView.countdown = function (ms) {
  const s = Math.max(0, Math.ceil((Number.isFinite(ms) ? ms : 0) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

/* Which field of the Add node sheet an error belongs to: 'handle', 'address', 'code', or '' (it belongs to no field). The board's one-word `reason` (app/nodes.py PairError) is
   read first, then the status (a 429 counts the tries of a code), then the board's own words. */
NodeView.REASON_FIELD = {
  none: 'code', wrong: 'code', expired: 'code', burned: 'code', rate_limited: 'code', bad_url: 'address', callback_mismatch: 'address', unreachable: 'address',
  bad_request: '', refused: '', store: '',
};
NodeView.errField = function (status, message, reason) {
  if (typeof reason === 'string' && Object.prototype.hasOwnProperty.call(NodeView.REASON_FIELD, reason)) return NodeView.REASON_FIELD[reason];
  const m = typeof message === 'string' ? message : '';
  if (status === 429) return 'code';
  if (/handle/i.test(m)) return 'handle';
  if (/\b(url|address|host|https?|dns|resolve[sd]?|reach(ed|able)?|unreachable|connect(ion)?|timed? out|tailnet)\b/i.test(m)) return 'address';
  if (/\b(code|expired|used|burn(ed|t)?|wrong|attempts?|tries)\b/i.test(m)) return 'code';
  return '';
};

/* A list out of an answer: the answer itself when it is an array, else the first of `keys` that holds one, else []. A plain {ok: true} is an empty list. */
NodeView.list = function (answer, ...keys) {
  if (Array.isArray(answer)) return answer.filter((x) => x && typeof x === 'object');
  if (answer && typeof answer === 'object') for (const k of keys) if (Array.isArray(answer[k])) return answer[k].filter((x) => x && typeof x === 'object');
  return [];
};

/* One paired node (a row of GET /api/nodes or GET /api/nodes/pairs) in the words the list shows:
   { id, key, name, handle, host, scopes, seen, unverified, repair, legacy, revoked, error, direction, superseded, supersededHost }.
   `superseded` is the peer_id of the newer pair that replaced this one from another address ('' when none), `supersededHost` that pair's address.
   id is what the board's routes take (peer_id); name falls back to the handle and then the id; `seen` is the last time the board really heard from it (epoch s) or null. */
NodeView.peer = function (rec) {
  const r = rec && typeof rec === 'object' ? rec : {};
  const s = NodeView.str;
  const id = s(r.peer_id || r.id || r.handle, 80);
  const handle = s(r.handle, 31);
  const seen = [r.last_seen, r.last_ok_at, r.last_used_at].map((v) => NodeView.epoch(v)).find((v) => v !== null) || null;
  return {
    id, key: id || s(r.node_id, 80), name: s(r.name || r.peer_name || handle || id, 80) || 'unnamed node', handle, host: NodeView.hostOf(s(r.url || r.peer_url, 300)),
    scopes: NodeView.scopeList(r.scopes), seen, unverified: r.callback_unverified === true, repair: r.needs_repair === true, legacy: r.legacy === true,
    revoked: !!r.revoked_at, error: s(r.last_error, 120), direction: r.direction === 'in' ? 'in' : 'out',
    superseded: s(r.superseded_by, 80), supersededHost: NodeView.hostOf(s(r.superseded_by_url, 300)),
  };
};

/* The state chip of a pair: re-pair (amber), read only (a CCBOARD_NODES row: it is polled, it is not paired, whatever time it was last read), paired and heard from, or waiting for the first contact. 'Paired' is said only once the board has actually heard from it. For a
   node that calls this board (`incoming`) the words are about use. */
NodeView.peerState = function (p, incoming) {
  if (p.repair) return { word: 're-pair needed', tone: 'warn', glyph: '!', hint: 'The saved token stopped working. Remove this node and pair it again.' };
  if (p.legacy && !incoming) return { word: 'read only', tone: '', glyph: '·', hint: 'Set in CCBOARD_NODES and read with the hub token: pair it to enable actions.' };
  if (p.seen !== null) return { word: incoming ? 'in use' : 'paired', tone: 'ok', glyph: '✓', hint: incoming ? 'This node has called this board.' : 'This board has reached the node.' };
  return { word: incoming ? 'never used' : 'waiting for first contact', tone: '', glyph: '·', hint: incoming ? 'This node has not called this board yet.' : 'Paired, but this board has not reached the node yet.' };
};

/* The line under a pair's chips: where it is, when it was last heard, the last error, and for 60 s after a rotation that the old token still works. */
NodeView.peerNote = function (p, { nowMs = Date.now(), incoming = false, rotatedMs = 0 } = {}) {
  const parts = [];
  if (p.host) parts.push(p.host);
  const ago = p.seen !== null ? NodeView.span(p.seen, nowMs) : '';
  parts.push(ago ? `${incoming ? 'last used' : 'last reached'} ${ago} ago` : incoming ? 'never used' : 'not reached yet');
  if (p.error && !incoming) parts.push(`last error: ${p.error}`);
  if (p.legacy) parts.push('read only, pair to enable actions');
  if (rotatedMs && nowMs - rotatedMs < NodeView.ROTATE_GRACE_S * 1000) parts.push(`token rotated, the old one still works for ${NodeView.ROTATE_GRACE_S} s`);
  return parts.join(' · ');
};

/* A pair as a row, in the shape of the found rows: name (and its handle), chips (state, scopes, flags), a note, and `actions` (elements) at the right. */
NodeView.REPLACED = 'Another pair';

NodeView.peerRow = function (rec, { nowMs = Date.now(), actions = null, incoming = false, rotatedMs = 0 } = {}) {
  const p = NodeView.peer(rec);
  const st = NodeView.peerState(p, incoming);
  const scopes = NodeView.SCOPES.filter((s) => p.scopes.includes(s.id));
  const replaced = incoming && p.superseded;
  return el('div', { class: `nd-row${p.repair ? ' repair' : ''}${replaced ? ' superseded' : ''}`, 'data-key': p.key, 'data-kind': incoming ? 'in' : 'out' },
    el('div', { class: 'nd-main' },
      el('div', { class: 'nd-head' }, el('b', { class: 'nd-name', text: p.name }), p.handle ? el('span', { class: 'dim nd-handle', text: p.handle }) : null),
      el('div', { class: 'nd-chips' }, NodeView.chip(st.word, st.tone, st.glyph, st.hint),
        scopes.map((s) => NodeView.chip(s.label.toLowerCase(), '', '', s.what)),
        p.unverified ? NodeView.chip('callback not verified', 'warn', '!', 'That node did not confirm who it is when it was paired. The pair works; its name is not confirmed.') : null,
        replaced ? NodeView.chip('same node id', 'warn', '!', 'A newer pair from another address gave this node\'s id. A node id is only the word of the board at its address, so neither pair was cut.') : null),
      replaced ? el('div', { class: 'nd-replaced', text: `${NodeView.REPLACED}${p.supersededHost ? ` from ${p.supersededHost}` : ''} says it is this node too. A node id is not proof, so both still work: revoke the one you do not recognise.` }) : null,
      el('div', { class: 'dim nd-note', text: NodeView.peerNote(p, { nowMs, incoming, rotatedMs }) })),
    actions ? el('div', { class: 'nd-act' }, actions) : null);
};

/* The words of an audit action (GET /api/nodes/audit; the actions are app/nodes.py audit(): code_created, code_cancelled, code_burned, code_used, callback_unverified, paired,
   paired_back, pair_refused, pair_failed, rotated, rotate_failed, revoked, superseded, unpair_kept_outgoing, removed, unpair, and, from the relay (issue #140), read_card, read_state,
   read_task, read_pane, read_agents, scope_refused, route_refused, method_refused): the first rule that matches the action's name gives [words when it worked, words when
   it did not]. An action this table does not know is its name with the underscores and dots turned into spaces, and 'failed' after it when it did not work. */
NodeView.AUDIT_WORDS = [
  [/^(scope|route|method)_refused/, 'Request refused', 'Request refused'],
  [/^read_card/, 'Card read', 'Card read failed'],
  [/^read_state/, 'State read', 'State read failed'],
  [/^read_task/, 'Task read', 'Task read failed'],
  [/^read_pane/, 'Screen tail read', 'Screen tail read failed'],
  [/^read_agents/, 'Agents read', 'Agents read failed'],
  [/burn/, 'Pairing code burned', 'Pairing code burned'],
  [/code.*(cancel|delete|revoke)|(cancel|delete).*code/, 'Pairing code cancelled', 'Pairing code not cancelled'],
  [/code.*(creat|mint|issue|new)|(creat|mint|issue).*code/, 'Pairing code created', 'Pairing code not created'],
  [/code.*(us|redeem)|redeem/, 'Pairing code used', 'Pairing code refused'],
  [/callback/, 'Callback checked', 'Callback not verified'],
  [/paired?_back|reverse/, 'Paired both ways', 'Pairing both ways failed'],
  [/rotat/, 'Token rotated', 'Token rotation failed'],
  [/supersed/, 'Same node id from another address', 'Same node id from another address'],
  [/kept/, 'Pair kept', 'Pair kept'],
  [/revok/, 'Pair revoked', 'Pair not revoked'],
  [/unpair|remov|delet/, 'Node removed', 'Node removed, the other node was not told'],
  [/refus/, 'Pairing refused', 'Pairing refused'],
  [/pair/, 'Paired', 'Pairing failed'],
  [/hello|card|summary|read|get/, 'Read', 'Read failed'],
];

NodeView.auditWord = function (action, ok) {
  const a = NodeView.str(action, 80).toLowerCase();
  const worked = ok !== false;
  for (const [re, yes, no] of NodeView.AUDIT_WORDS) if (re.test(a)) return worked ? yes : no;
  const plain = a.replace(/[_.:-]+/g, ' ').trim();
  const word = plain ? plain.charAt(0).toUpperCase() + plain.slice(1, 40) : 'Event';
  return worked ? word : `${word} failed`;
};

/* One audit row in the words the list shows: { key, at, word, dir: 'to' | 'from' | '', who, ok, detail }. `ok` is the row's ok, else true for a status below 400. */
NodeView.audit = function (row, index) {
  const r = row && typeof row === 'object' ? row : {};
  const s = NodeView.str;
  let ok = true;
  if (typeof r.ok === 'boolean') ok = r.ok;
  else if (typeof r.status === 'number') ok = r.status < 400;
  else if (typeof r.status === 'string' && r.status) ok = /^(ok|[23]\d\d)$/i.test(r.status);
  return {
    key: String(r.id !== undefined && r.id !== null ? r.id : `i${index}`), at: NodeView.epoch(r.at), word: NodeView.auditWord(r.action, ok), ok,
    dir: r.direction === 'out' ? 'to' : r.direction === 'in' ? 'from' : '', who: s(r.node_name || r.name || r.peer_name || r.handle, 60),
    detail: s(r.detail, 140), target: s(r.target, 80), user: s(r.user, 70),
  };
};

/* The Activity filters (issue #140): GET /api/nodes/audit takes direction (in | out), node (a node name or a peer id), action and failures=1. The page asks the board with
   them and, since the same rows may come from a board that ignores them (or from the demo), also keeps only the rows that match. A filter is { direction, node, action, failures }. */
NodeView.AUDIT_LIMIT = 50;
NodeView.auditFilter = function () { return { direction: '', node: '', action: '', failures: false }; };
NodeView.auditActive = function (f) { return !!(f && (f.direction || f.node || f.action || f.failures)); };
NodeView.auditQuery = function (f, limit) {
  const q = [`limit=${limit || NodeView.AUDIT_LIMIT}`];
  if (f && (f.direction === 'in' || f.direction === 'out')) q.push(`direction=${f.direction}`);
  if (f && f.node) q.push(`node=${encodeURIComponent(NodeView.str(f.node, 64))}`);
  if (f && f.action) q.push(`action=${encodeURIComponent(NodeView.str(f.action, 40))}`);
  if (f && f.failures) q.push('failures=1');
  return `?${q.join('&')}`;
};
NodeView.auditFailed = function (r) {
  if (!r || typeof r !== 'object') return false;
  if (typeof r.ok === 'boolean') return !r.ok;
  if (typeof r.status === 'number') return r.status >= 400;
  return typeof r.status === 'string' && r.status !== '' && !/^(ok|[23]\d\d)$/i.test(r.status);
};
NodeView.auditMatch = function (r, f) {
  if (!r || typeof r !== 'object') return false;
  if (!f) return true;
  if (f.direction && r.direction !== f.direction) return false;
  if (f.node && r.node_name !== f.node && r.peer !== f.node) return false;
  if (f.action && r.action !== f.action) return false;
  if (f.failures && !NodeView.auditFailed(r)) return false;
  return true;
};
/* The choices of the node and action selects: every distinct value of the rows seen so far, the current choice always among them. [[value, label]] after 'all'. */
NodeView.auditChoices = function (seen, current, label) {
  const vals = [...new Set([...(seen || []), ...(current ? [current] : [])])].filter((v) => typeof v === 'string' && v).sort((a, b) => label(a).localeCompare(label(b)));
  return vals.map((v) => [v, label(v)]);
};

NodeView.auditRow = function (info, nowMs) {
  const ago = info.at !== null ? NodeView.span(info.at, nowMs) : '';
  const by = info.user ? (info.user.startsWith('for ') ? info.user : `by ${info.user}`) : '';       // the acting user: on the peer a claim (`for <login>`), never a proof
  const where = [info.dir && info.who ? `${info.dir} ${info.who}` : info.who, info.target, by, ago ? `${ago} ago` : ''].filter(Boolean).join(' · ');
  return el('div', { class: info.ok ? 'nd-audit' : 'nd-audit bad', 'data-key': info.key },
    el('span', { class: 'nd-glyph', 'aria-hidden': 'true', text: info.ok ? '✓' : '✕' }),
    el('div', { class: 'nd-audit-main' },
      el('b', { class: 'nd-audit-word', text: info.word }),
      where ? el('span', { class: 'dim nd-audit-where', text: where }) : null,
      info.detail ? el('span', { class: 'dim nd-audit-detail', text: info.detail }) : null));
};

/* ---------------------------------------------------------------- demo mode (?demo=1): the writes of Settings > Nodes, played in the page
   core.js sends POST and DELETE /api/nodes... here (demoApi) and answers the three GETs from demoNodesMade + demo/nodes.json (demoNodes). Nothing of this is a credential. */
function demoNodesNote(action, who, ok, detail, direction) {
  demoNodesMade.audit.unshift({ id: `demo-w${++demoNodesMade.seq}`, at: new Date().toISOString(), direction: direction || 'out', peer: '', node_name: who || '', action, status: ok === false ? 'failed' : 'ok', detail: detail || '' });
}
/* What removing a node would also touch, as the board works it out: the confirmed pairs at the node's own address go by themselves (`auto`), the other pairs that name its node id
   (another address, or never confirmed) stay unless the request lists them (`others`). */
function demoRemoval(data, id) {
  const host = (u) => String(u || '').replace(/^https?:\/\//i, '').replace(/\/+$/, '').toLowerCase();
  const out = [...(data.nodes || []), ...demoNodesMade.added].find((r) => r.peer_id === id);
  const res = { auto: [], others: [] };
  if (!out || !out.node_id) return res;
  for (const r of (data.pairs || [])) {
    if (r.node_id !== out.node_id || demoNodesMade.gone.has(r.peer_id)) continue;
    const v = { peer_id: r.peer_id, name: r.name, url: r.url, verified: r.callback_unverified !== true };
    (v.verified && host(r.url) === host(out.url) ? res.auto : res.others).push(v);
  }
  return res;
}
function demoNodesWrite(method, path, body, data) {
  const b = body && typeof body === 'object' ? body : {};
  let m = null;
  if (method === 'POST' && path === '/api/nodes/pair-code') {
    const mins = Number.isFinite(b.minutes) ? Math.min(30, Math.max(1, b.minutes)) : 10;
    demoNodesNote('code_created', '', true, `scopes ${(Array.isArray(b.scopes) ? b.scopes : []).join(', ')}`, 'in');
    return { code: 'K7Q2M-4XD9R', expires_at: new Date(Date.now() + mins * 60000).toISOString(), scopes: Array.isArray(b.scopes) ? b.scopes : ['read', 'tasks'] };
  }
  if (method === 'DELETE' && path === '/api/nodes/pair-code') { demoNodesNote('code_cancelled', '', true, '', 'in'); return { cancelled: true }; }
  if (method === 'POST' && path === '/api/nodes') {
    const host = String(b.url || '').replace(/^https?:\/\//, '').split(/[.:/]/)[0] || 'node';
    const handle = String(b.handle || host).toLowerCase().slice(0, 31);
    const row = { peer_id: `demo-${handle}`, handle, node_id: `ts:nDEMO-${handle}`, name: handle, url: String(b.url || ''), scopes: ['read', 'tasks'], created_at: new Date().toISOString(), last_seen: null, direction: 'out' };
    demoNodesMade.added.push(row);
    demoNodesNote('paired', handle, true, b.both_ways ? 'both ways' : '');
    return row;
  }
  if (method === 'POST' && (m = /^\/api\/nodes\/([^/]+)\/rotate$/.exec(path))) { demoNodesNote('rotated', decodeURIComponent(m[1]), true, 'the old token works 60 s more there'); return { rotated: true, grace_s: 60 }; }
  if (method === 'POST' && (m = /^\/api\/nodes\/([^/]+)\/remove-preview$/.exec(path))) {
    const rm = demoRemoval(data, decodeURIComponent(m[1]));
    return { auto: rm.auto, others: rm.others, at: new Date().toISOString() };
  }
  if (method === 'DELETE' && (m = /^\/api\/nodes\/([^/]+)$/.exec(path))) {
    const id = decodeURIComponent(m[1]);
    const rm = demoRemoval(data, id);
    const asked = Array.isArray(b.also_revoke) ? b.also_revoke : [];
    const cut = rm.others.filter((x) => asked.includes(x.peer_id));
    demoNodesMade.gone.add(id);
    for (const x of [...rm.auto, ...cut]) demoNodesMade.gone.add(x.peer_id);
    demoNodesNote('unpair', id, true, '');
    return { removed: true, peer_notified: id !== 'p-old-laptop', also_revoked: cut.map((x) => x.peer_id), other_pairs: rm.others.filter((x) => !asked.includes(x.peer_id)) };
  }
  return null;
}
