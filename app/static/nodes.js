/* ccboard node refs: how a thing on another node is named in addresses, DOM keys and stored keys. Classic script, definition only (loaded in index.html right after
   core.js, needs nothing from it): no DOM, storage, network, listener or timer at load. One namespace, Ref.

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
