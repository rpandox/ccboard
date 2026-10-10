/* ccboard node refs: how a thing on another node is named in addresses, DOM keys and stored keys. Classic script, definition only (loaded in index.html right after
   core.js, needs nothing from it at load): no DOM, storage, network, listener or timer at load. Two namespaces: Ref (below) and NodeView (the end of the file:
   the chip and row of a node found on the tailnet, which call el() from core.js only when a row is built).

   The grammar (app/nodes.py parse_ref is the same rules in Python):
     handle     ^[a-z0-9][a-z0-9-]{0,30}$   a short readable name for a paired node; 'local' is the board in front of you and is never written anywhere
     tmux part  a ccboard session name <project>--<repo>--<session> (Ref.isTmux, the rule of app/tmux.py); no slash, no dot, so no '..'
     task id    digits only, 1 to 12
     text       session  <handle>/<tmux>      task  <handle>:<id>
     address    node  #/n/<handle>   session  #/n/<handle>/s/<tmux>   task  #/n/<handle>/t/<id>
                local items keep today's #/s/<tmux> (and #/t/<id>); nothing local is ever qualified
   A ref is a frozen {kind: 'session' | 'task' | 'node', node: <handle> | null, tmux | id}. node null means this board. Addresses use the handle, not the display
   name, so a peer renaming itself breaks no bookmark; tmux names are never changed, only wrapped.

   Keys (the one place they are built):
     Ref.key(x)             the DOM and map key: the bare tmux name for a local session (byte for byte what it was), '<handle>/<tmux>' for a remote one;
                            a task is its bare id (local) or '<handle>:<id>' (remote)
     Ref.storeKey(prefix,x) prefix + Ref.key(x): 'ccboard:quick:' + 'shop--api--s1' stays what it was, a remote session gets 'ccboard:quick:box/shop--api--s1'
     Ref.slotValue(key)     the quad store entry: the bare name (local, unchanged) or {node, tmux} (remote); Ref.slotKey(value) is its inverse
   A row of the roster carries its node as `node` (absent or 'local' for this board). */
'use strict';

const Ref = {
  LOCAL: 'local',
  HANDLE_RE: /^[a-z0-9][a-z0-9-]{0,30}$/,
  TMUX_RE: /^[A-Za-z0-9_-]{1,200}$/,
  PART_RE: /^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?$/,
  ID_RE: /^[0-9]{1,12}$/,
};

/* Is this a handle a remote node can have? ('local' matches the pattern but is reserved.) */
Ref.isHandle = function (h) { return typeof h === 'string' && h !== Ref.LOCAL && Ref.HANDLE_RE.test(h); };

/* A ccboard session name, as app/tmux.py valid_name / split_name see it: three parts joined by '--', each [A-Za-z0-9] at both ends and [A-Za-z0-9_-] between (so no
   part holds '--'), 1 to 64 characters. Not a path: no slash, no dot. */
Ref.isTmux = function (t) {
  if (typeof t !== 'string' || t.length > 200 || !Ref.TMUX_RE.test(t)) return false;
  const parts = t.split('--');
  return parts.length === 3 && parts.every((p) => Ref.PART_RE.test(p));
};

Ref.isId = function (i) { return (typeof i === 'string' || typeof i === 'number') && Ref.ID_RE.test(String(i)); };

/* The handle an item or ref names, or null for this board. Not validated: a key must never fold a strange `node` into the local keys. */
Ref.nodeOf = function (x) {
  const n = x && typeof x === 'object' ? x.node : x;
  if (n === undefined || n === null || n === '' || n === Ref.LOCAL) return null;
  return String(n);
};

/* A ref is what Ref.session / task / node return. */
Ref.isRef = function (x) {
  if (!x || typeof x !== 'object') return false;
  if (x.kind === 'session') return typeof x.tmux === 'string';
  if (x.kind === 'task') return x.id !== undefined && x.id !== null;
  return x.kind === 'node' && typeof x.node === 'string';
};

/* Ref.session(handle, tmux): the session `tmux` on `handle` (null, '' or 'local' for this board). null when either part is outside the grammar. */
Ref.session = function (handle, tmux) {
  const node = Ref.nodeOf(handle);
  if (node !== null && !Ref.isHandle(node)) return null;
  if (!Ref.isTmux(tmux)) return null;
  return Object.freeze({ kind: 'session', node, tmux });
};

Ref.task = function (handle, id) {
  const node = Ref.nodeOf(handle);
  if (node !== null && !Ref.isHandle(node)) return null;
  if (!Ref.isId(id)) return null;
  return Object.freeze({ kind: 'task', node, id: String(id) });
};

/* The node page of a paired node (there is no local one). */
Ref.node = function (handle) {
  return Ref.isHandle(handle) ? Object.freeze({ kind: 'node', node: handle }) : null;
};

/* A ref from a ref, a roster row ({tmux, node?}) or a bare tmux name (this board's). Not validated: local rows keep whatever name they have; Ref.hash is what refuses. */
Ref.of = function (x) {
  if (typeof x === 'string') return x ? { kind: 'session', node: null, tmux: x } : null;
  if (!x || typeof x !== 'object') return null;
  if (Ref.isRef(x)) return x;
  if (x.tmux === undefined || x.tmux === null || x.tmux === '') return null;
  return { kind: 'session', node: Ref.nodeOf(x), tmux: String(x.tmux) };
};

Ref.isLocal = function (x) {
  const r = Ref.of(x);
  return !!r && Ref.nodeOf(r) === null;
};

/* Parse an address or a text ref: '#/n/box', '#/n/box/s/shop--api--s1', '#/n/box/t/42', '#/s/shop--api--s1', '#/t/42' (with or without the '#'), or 'box/shop--api--s1',
   'box:42'. A bare tmux name is not a ref (null): say Ref.session(null, name). Anything outside the grammar is null, never a throw. */
Ref.parse = function (text) {
  if (typeof text !== 'string' || !text || text.length > 300) return null;
  const t = text.charAt(0) === '#' ? text.slice(1) : text;
  let m;
  if (t.charAt(0) === '/') {
    if ((m = /^\/n\/([^/]+)$/.exec(t))) return Ref.node(m[1]);
    if ((m = /^\/n\/([^/]+)\/s\/([^/]+)$/.exec(t))) return Ref.isHandle(m[1]) ? Ref.session(m[1], m[2]) : null;
    if ((m = /^\/n\/([^/]+)\/t\/([^/]+)$/.exec(t))) return Ref.isHandle(m[1]) ? Ref.task(m[1], m[2]) : null;
    if ((m = /^\/s\/([^/]+)$/.exec(t))) return Ref.session(null, m[1]);
    if ((m = /^\/t\/([^/]+)$/.exec(t))) return Ref.task(null, m[1]);
    return null;
  }
  if (text.charAt(0) === '#') return null;
  if ((m = /^([^/:]+)\/([^/:]+)$/.exec(t))) return Ref.isHandle(m[1]) ? Ref.session(m[1], m[2]) : null;
  if ((m = /^([^/:]+):([^/:]+)$/.exec(t))) return Ref.isHandle(m[1]) ? Ref.task(m[1], m[2]) : null;
  return null;
};

/* The address of a ref (or a roster row): '#/s/<tmux>' for a local session, '#/n/<handle>/s/<tmux>' for a remote one; null when the ref is outside the grammar. */
Ref.hash = function (x) {
  const r = Ref.of(x);
  if (!r) return null;
  const node = Ref.nodeOf(r);
  if (node !== null && !Ref.isHandle(node)) return null;
  if (r.kind === 'node') return node === null ? null : '#/n/' + node;
  if (r.kind === 'task') {
    if (!Ref.isId(r.id)) return null;
    return node === null ? '#/t/' + r.id : '#/n/' + node + '/t/' + r.id;
  }
  if (!Ref.isTmux(r.tmux)) return null;
  return node === null ? '#/s/' + r.tmux : '#/n/' + node + '/s/' + r.tmux;
};

/* The text form: '<handle>/<tmux>' or '<handle>:<id>'; a local item is its bare name or id, a node its handle. null outside the grammar. */
Ref.text = function (x) {
  const r = Ref.of(x);
  if (!r || Ref.hash(r) === null) return null;
  const node = Ref.nodeOf(r);
  if (r.kind === 'node') return node;
  if (r.kind === 'task') return node === null ? r.id : node + ':' + r.id;
  return node === null ? r.tmux : node + '/' + r.tmux;
};

/* The DOM and map key (see the header). Never throws; a row without a name has the key ''. */
Ref.key = function (x) {
  const r = Ref.of(x);
  if (!r) return '';
  const node = Ref.nodeOf(r);
  if (r.kind === 'node') return node === null ? '' : node;
  if (r.kind === 'task') return node === null ? String(r.id) : node + ':' + r.id;
  return node === null ? String(r.tmux) : node + '/' + r.tmux;
};

/* A localStorage key for an item: the prefix as it is (it carries its own ':') plus the item's key. */
Ref.storeKey = function (prefix, x) { return String(prefix) + Ref.key(x); };

/* '<name>' for this board, '<handle>/<name>' for a remote one: the scope of a per-project key (the launcher's ccboard:launch:<p>/<r>) on another node. A project
   or repo name never holds a slash, so the qualified form cannot read as a local one. */
Ref.scoped = function (handle, name) {
  const node = Ref.nodeOf(handle);
  return node === null ? String(name) : node + '/' + name;
};

/* A key apart again: {node, tmux} (node null for a bare name) or null when it is not a key (a slash in the wrong place, a bad handle). The tmux part is not
   checked here: the caller knows its own name rule. */
Ref.splitKey = function (key) {
  if (typeof key !== 'string' || !key) return null;
  const i = key.indexOf('/');
  if (i < 0) return { node: null, tmux: key };
  const node = key.slice(0, i);
  const tmux = key.slice(i + 1);
  if (!Ref.isHandle(node) || !tmux || tmux.indexOf('/') >= 0) return null;
  return { node, tmux };
};

/* The quad's stored form of a slot key: the bare tmux name for a local session (what every saved layout holds today), {node, tmux} for a remote one. '' for no key. */
Ref.slotValue = function (key) {
  const p = Ref.splitKey(key);
  if (!p) return '';
  return p.node === null ? p.tmux : { node: p.node, tmux: p.tmux };
};

/* A stored slot (a string, or {node, tmux}) back to a key; '' when it is neither. */
Ref.slotKey = function (v) {
  if (typeof v === 'string') return Ref.splitKey(v) ? v : '';
  if (v && typeof v === 'object' && typeof v.node === 'string' && typeof v.tmux === 'string' && Ref.isHandle(v.node) && v.tmux && v.tmux.indexOf('/') < 0) return v.node + '/' + v.tmux;
  return '';
};

/* ---------------------------------------------------------------- how a node looks (issue #134; the node page of #139 reuses these)

   NodeView holds the words, the chips and the row of a node found on the tailnet. The functions that only choose words or compare are pure (no DOM, no clock unless
   handed `nowMs`); the builders call el() from core.js when they run, never at load. Every dynamic text goes through el()'s `text`, so a node that calls itself
   "<img onerror=...>" is shown as that text and nothing more. Nothing here asks the network: a row is built from an answer the caller already has.

   A row of GET /api/nodes/discover (app/nodes_discovery.py _out_row) is {ts_id, name, dns_name, os, online, last_seen, owner: 'user' | 'tag', tags, state, url, node_id,
   at, age, stale}: `state` is one of NodeView.PROBE, `url` the address that answered and `at` the time of the probe (ISO), both only for a probed row. A row with a
   state outside the table reads as 'unchecked' (never as found). Times are compared on the box's clock (NodeView.boxNow), not the browser's. */
const NodeView = {
  STALE_S: 30,
  /* state -> the plain words, the tone of the chip (ok | warn | bad | '' for quiet) and a glyph, so a state is never colour only */
  PROBE: {
    found: { word: 'ccboard answers', tone: 'ok', glyph: '✓', hint: 'ccboard answered on this address.' },
    refuses: { word: 'ccboard refuses us', tone: 'warn', glyph: '!', hint: 'Something answers but will not talk to this board: a grant or an identity is missing on that node.' },
    no_ccboard: { word: 'nothing listening', tone: '', glyph: '○', hint: 'The device is reachable, but ccboard or its HTTPS port is not running there.' },
    no_tls: { word: 'no HTTPS', tone: 'warn', glyph: '!', hint: 'The device did not give a valid certificate: HTTPS may be off for the tailnet, or the port is wrong.' },
    unreachable: { word: 'unreachable', tone: 'bad', glyph: '✕', hint: 'No answer in time: the device is asleep, or the tailnet policy blocks this board.' },
    offline: { word: 'offline', tone: '', glyph: '○', hint: 'The device is not connected to the tailnet, so it was not checked.' },
    invalid: { word: 'invalid name', tone: 'bad', glyph: '✕', hint: 'The name Tailscale gave is not a tailnet name, so it was not checked.' },
    unchecked: { word: 'not checked yet', tone: '', glyph: '·', hint: 'Press Refresh to check this device.' },
  },
  OS: { linux: 'Linux', macos: 'macOS', windows: 'Windows', ios: 'iOS', android: 'Android', freebsd: 'FreeBSD', tvos: 'tvOS' },
};

/* An epoch in seconds from a number (seconds, or milliseconds when it is that big), a numeric string or an ISO string; null when it is none of those. */
NodeView.epoch = function (v) {
  let n = null;
  if (typeof v === 'number') n = v;
  else if (typeof v === 'string' && v) n = /^\d+(\.\d+)?$/.test(v) ? Number(v) : Date.parse(v) / 1000;
  if (n === null || !Number.isFinite(n) || n <= 0) return null;
  return n > 1e12 ? n / 1000 : n;
};

/* '12s', '5m', '3h', '2d' for the time between an epoch and nowMs; '' when the epoch is unreadable. */
NodeView.span = function (epoch, nowMs) {
  const e = NodeView.epoch(epoch);
  if (e === null) return '';
  const s = Math.max(0, Math.floor((nowMs - e * 1000) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
};

/* The probe state of a row: a name of NodeView.PROBE, else 'unchecked'. Only own keys count, so 'constructor' is not a state. */
NodeView.state = function (row) {
  const s = row && typeof row === 'object' ? row.state : null;
  return typeof s === 'string' && Object.prototype.hasOwnProperty.call(NodeView.PROBE, s) ? s : 'unchecked';
};

/* The chip's facts for a row: {state, word, tone, glyph, hint}. */
NodeView.probeInfo = function (row) {
  const state = NodeView.state(row);
  return { state, ...NodeView.PROBE[state] };
};

/* 'Linux', 'macOS' ...; an OS this table does not know is shown as Tailscale spelled it (cut to 20 characters); '' when there is none. */
NodeView.osName = function (os) {
  const o = typeof os === 'string' ? os.trim() : '';
  if (!o) return '';
  const k = o.toLowerCase();
  return Object.prototype.hasOwnProperty.call(NodeView.OS, k) ? NodeView.OS[k] : o.slice(0, 20);
};

/* The ownership word of a row (its `owner`: 'user' is the same Tailscale user, 'tag' a device tagged for ccboard): 'your device', 'tag', or '' when the answer did not say.
   A device shared in from another user is never listed by the board, so there is no word for it. */
NodeView.owner = function (row) {
  const o = row && typeof row === 'object' ? row.owner : null;
  return o === 'user' ? 'your device' : o === 'tag' ? 'tag' : '';
};

/* The box's clock, in ms, as best the page can tell without trusting its own: the time the answer was made (`answerAt`) plus what has passed in the browser since the
   answer arrived (`receivedMs`). The browser's clock alone would call a fresh row stale when the two clocks differ. Falls back to the browser's clock without both. */
NodeView.boxNow = function (answerAt, receivedMs, nowMs) {
  const a = NodeView.epoch(answerAt);
  return a !== null && Number.isFinite(receivedMs) ? a * 1000 + Math.max(0, nowMs - receivedMs) : nowMs;
};

/* 'on the tailnet' or 'offline, last seen 3h ago' / 'offline': online here only means connected to the tailnet, not that ccboard answers. */
NodeView.onlineWord = function (row, nowMs) {
  if (row && row.online === true) return 'on the tailnet';
  const ago = NodeView.span(row && row.last_seen, nowMs);
  return ago ? `offline, last seen ${ago} ago` : 'offline';
};

/* A node id for a small place: 'ts:nDEMO1CNTRL' stays as it is, a long one is cut in the middle ('ccb:3f9c...b25e'). */
NodeView.shortId = function (id) {
  const s = typeof id === 'string' ? id : '';
  return s.length > 16 ? `${s.slice(0, 8)}...${s.slice(-4)}` : s;
};

/* host[:port] of a url for display, '' when it is not a readable https/http address (an address with user info is none). Display only: nothing is ever opened from it. */
NodeView.hostOf = function (url) {
  const m = typeof url === 'string' ? /^https?:\/\/([^/?#@\s]+)(?:[/?#]|$)/i.exec(url) : null;
  return m ? m[1].slice(0, 80) : '';
};

/* Is this found row already one of `paired` ([{node_id?, url?}], the entries of state.nodes.value today)? A match by the host of its address (the row's answering
   url, else its tailnet name). A node id alone decides nothing: it is only the word of the board at that address, and a paired board that claimed a found
   device's id must not hide that device from the list. A found row is listed once: as paired, or as found, never both. */
NodeView.isPaired = function (row, paired) {
  if (!row || !Array.isArray(paired) || !paired.length) return false;
  const bare = (u) => NodeView.hostOf(u).toLowerCase().replace(/:\d+$/, '');
  const hosts = new Set([bare(row.url), bare(typeof row.dns_name === 'string' ? `https://${row.dns_name.replace(/\.$/, '')}` : '')].filter(Boolean));
  return paired.some((x) => {
    if (!x || typeof x !== 'object') return false;
    const h = bare(x.url);
    return !!h && hosts.has(h);
  });
};

/* What a row shows, as a string: the list repaints a row only when this changed. `bucket` moves on with the clock (the caller's age rounding), so 'checked 5m ago' ages.
   The answer's own `age` is left out on purpose: it grows with every answer while the row stays the same. */
NodeView.sig = function (row, bucket) {
  return JSON.stringify([row.ts_id, row.name, row.dns_name, row.os, row.online, row.last_seen, row.owner, row.tags, row.state, row.url, row.node_id, row.at, bucket]);
};

/* A chip: <span class="badge [tone]"> with an optional glyph (aria-hidden: the words carry the meaning). */
NodeView.chip = function (text, tone, glyph, title) {
  const c = el('span', { class: tone ? `badge ${tone}` : 'badge', title: title || null });
  if (glyph) c.append(el('span', { class: 'nd-glyph', 'aria-hidden': 'true', text: glyph }), ' ');
  c.append(String(text));
  return c;
};

NodeView.probeChip = function (row) {
  const i = NodeView.probeInfo(row);
  return NodeView.chip(i.word, i.tone, i.glyph, i.hint);
};

/* The online dot with its word. The dot is a decoration; the word is the state. */
NodeView.onlineDot = function (row, nowMs) {
  const on = !!(row && row.online === true);
  return el('span', { class: 'nd-online' }, el('span', { class: on ? 'dot' : 'dot unknown', 'aria-hidden': 'true' }), el('span', { text: NodeView.onlineWord(row, nowMs) }));
};

/* One found node as a row: name, the online dot with its word, the chips (OS, whose it is, the probe result), a line saying what the probe found (and how old it is,
   'stale' past STALE_S), and `action` (an element: the Pair button) at the right. The root carries data-key = the Tailscale id, the key of a keyed repaint. */
NodeView.row = function (row, { nowMs = Date.now(), action = null } = {}) {
  const info = NodeView.probeInfo(row);
  const owner = NodeView.owner(row);
  const os = NodeView.osName(row.os);
  const checked = info.state !== 'offline' && info.state !== 'invalid' && info.state !== 'unchecked';          // offline and invalid are decided from the status file: no probe ran
  const at = checked ? NodeView.epoch(row.at) : null;
  const stale = at !== null && nowMs / 1000 - at > NodeView.STALE_S;
  const answers = info.state === 'found' ? NodeView.hostOf(row.url) : '';
  const notes = [answers ? `Answers at ${answers}.` : info.hint];
  if (at !== null) notes.push(stale ? `Checked ${NodeView.span(at, nowMs)} ago, stale: press Refresh.` : `Checked ${NodeView.span(at, nowMs) || '0s'} ago.`);
  return el('div', { class: stale ? 'nd-row stale' : 'nd-row', 'data-key': String(row.ts_id), 'data-state': info.state },
    el('div', { class: 'nd-main' },
      el('div', { class: 'nd-head' }, el('b', { class: 'nd-name', text: String(row.name || row.dns_name || 'unnamed device') }), NodeView.onlineDot(row, nowMs)),
      el('div', { class: 'nd-chips' }, os ? NodeView.chip(os, '', '', 'Operating system') : null,
        owner ? NodeView.chip(owner, '', '', owner === 'tag' ? 'Tagged for ccboard in the tailnet' : 'Signed in as you') : null, NodeView.probeChip(row)),
      el('div', { class: 'dim nd-note', text: notes.join(' ') })),
    action ? el('div', { class: 'nd-act' }, action) : null);
};
