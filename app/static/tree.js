/* ccboard tree.js (v0.5.6): the lazy directory tree and the read-only file preview. Definitions only: nothing here touches the DOM,
   storage, the network or a timer until a function is called. Classic script, one namespace (Tree). Loaded after launcher.js.

   Tree.mount(host, {project, repo, path: '', hidden, ignored, repos, onOpen(entry, path, info), onSelect(entry, path, info), onLoad(payload),
                     storageKey: 'ccboard:tree:<project>/<repo>'}) -> {refresh(), expand(path), select(path), focus(), destroy(), root, list}
     Hand-written bp5-tree markup (through the semantic classes of core.js) with the WAI-ARIA tree pattern: role=tree on the list, role=treeitem on
     every node, role=group around a directory's children, aria-expanded on directories only, aria-level, aria-selected, one roving tabindex.
     Keys: Right (expand, or move to the first child), Left (collapse, or move to the parent), Up, Down, Home, End, Enter and Space (a file opens,
     a directory toggles), * (expand every sibling directory) and type-ahead by the first letters of a name. A directory loads its children
     (GET .../repos/<repo>/tree?path=) the first time it opens, with one AbortController per expand: a collapse while the request is in flight
     aborts it. The set of open directories persists under storageKey. Every 10 s while the page is visible the open directories revalidate with
     If-None-Match (a 304 changes nothing, a 200 patches the rows in place, keeping focus and the open subtrees).
     Paths are logical: relative to the mounted root, '/'-joined, through a nested repo ('type: repo' entries open that repo's own tree in place).
     info = {project, repo, rel, path}: repo and rel are what the file endpoint takes for that entry.
     expand(path) opens every directory down to path and resolves {entry, path, repo, rel} (null when something on the way does not exist);
     select(path) marks a loaded node selected (and the roving stop) without calling onSelect.
   Tree.previewFile(host, {project, repo, path, line, reveal}) -> {root, reload(), setLine(n), destroy()}: GET .../file, a header (path, size,
     reveal button on a 403, open in code-server link built by codeServerFileUrl) and a line gutter (ol, one li per line, textContent only).
   Tree.children(project, repo, path, {signal, hidden, ignored, repos, refresh, fresh}) -> Promise<payload>: the same listing for the sidebar,
     ETag-revalidated and cached for two seconds. Tree.fetchJson is the one request function (api() cannot abort, set headers or read a 304). */
'use strict';

const Tree = {
  REVALIDATE_MS: 10000,     // open directories revalidate this often while the page is visible
  TYPEAHEAD_MS: 700,        // the type-ahead buffer is forgotten after this pause
  CACHE_MS: 2000,           // Tree.children answers from memory inside this window
  STATUS_TITLE: { M: 'modified', A: 'added', '?': 'untracked', D: 'deleted', U: 'conflict' },
  cache: new Map(),
};

/* ---------- requests ---------- */

Tree.abortError = function () {
  const e = new Error('aborted');
  e.name = 'AbortError';
  return e;
};

/* GET path as JSON: {status, data, etag}. 304 (opt.etag sent as If-None-Match) answers {status: 304, data: null}; an HTTP error throws an Error
   with .status. Demo mode answers from the fixtures through demoApi (no ETags there). */
Tree.fetchJson = async function (path, opt) {
  const o = opt || {};
  if (typeof demoOn === 'function' && demoOn()) {
    const data = await demoApi('GET', path);
    if (o.signal && o.signal.aborted) throw Tree.abortError();
    return { status: 200, data, etag: data && data.etag ? `W/"${data.etag}"` : '' };
  }
  const headers = { 'X-CCBoard': '1' };
  if (o.etag) headers['If-None-Match'] = o.etag;
  const r = await fetch(path, { headers, cache: 'no-cache', signal: o.signal || undefined });
  if (r.status === 304) return { status: 304, data: null, etag: o.etag || '' };
  let data = null;
  try { data = await r.json(); } catch (_) { /* not json */ }
  if (!r.ok) {
    const e = new Error((data && data.error) || `${r.status} ${r.statusText}`);
    e.status = r.status;
    throw e;
  }
  const tag = r.headers && typeof r.headers.get === 'function' ? r.headers.get('ETag') : '';
  return { status: r.status, data, etag: tag || (data && data.etag ? `W/"${data.etag}"` : '') };
};

Tree.base = function (project, repo) {
  return `/api/projects/${encodeURIComponent(project)}/repos/${encodeURIComponent(repo)}`;
};

Tree.url = function (project, repo, path, o) {
  const f = o || {};
  const q = new URLSearchParams();
  q.set('path', path || '');
  q.set('hidden', f.hidden ? '1' : '0');
  q.set('ignored', f.ignored ? '1' : '0');
  q.set('repos', f.repos === false ? '0' : '1');
  if (f.refresh) q.set('refresh', '1');
  return `${Tree.base(project, repo)}/tree?${q.toString()}`;
};

Tree.fileUrl = function (project, repo, path, reveal) {
  const q = new URLSearchParams();
  q.set('path', path || '');
  if (reveal) q.set('reveal', '1');
  return `${Tree.base(project, repo)}/file?${q.toString()}`;
};

Tree.children = async function (project, repo, path, opt) {
  const o = opt || {};
  const key = `${project}/${repo}|${path || ''}|${o.hidden ? 1 : 0}${o.ignored ? 1 : 0}${o.repos === false ? 0 : 1}`;
  const hit = Tree.cache.get(key);
  if (hit && !o.fresh && !o.refresh && Date.now() - hit.at < Tree.CACHE_MS) return hit.data;
  const res = await Tree.fetchJson(Tree.url(project, repo, path, o), { signal: o.signal, etag: hit ? hit.etag : '' });
  if (res.status === 304 && hit) { hit.at = Date.now(); return hit.data; }
  Tree.cache.set(key, { data: res.data, etag: res.etag, at: Date.now() });
  if (Tree.cache.size > 400) Tree.cache.delete(Tree.cache.keys().next().value);
  return res.data;
};

/* ---------- helpers ---------- */

/* The absolute path of a repo on the box ('root' is the project folder), from the state; '' while it is unknown. */
Tree.repoAbs = function (project, repo) {
  const st = typeof state !== 'undefined' ? state : null;
  const p = st && (st.projects || []).find((x) => x.name === project);
  if (!p) return '';
  if (repo === 'root') return p.path || '';
  const r = (p.repos || []).find((x) => x.name === repo);
  return r ? r.path : '';
};

/* The code-server link for a file (rel inside repo) at a line, or '' where the port or the path is unknown. */
Tree.codeUrl = function (project, repo, rel, line) {
  const st = typeof state !== 'undefined' ? state : null;
  if (!st || !st.config || !st.config.code_https_port || typeof codeServerFileUrl !== 'function') return '';
  const base = Tree.repoAbs(project, repo);
  if (!base) return '';
  return codeServerFileUrl(`${base}/${rel}`, line, base);
};

Tree.fmtSize = function (n) {
  if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
};

Tree.setVar = function (node, name, value) { try { node.style.setProperty(name, value); } catch (_) { /* no CSSOM */ } };

Tree.load = function (key) {
  try { const v = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(v) ? v.filter((x) => typeof x === 'string').slice(0, 400) : []; } catch (_) { return []; }
};

Tree.save = function (key, list) {
  try { localStorage.setItem(key, JSON.stringify(list.slice(0, 400))); } catch (_) { /* storage may be unavailable */ }
};

/* ---------- the tree ---------- */

Tree.mount = function (host, opts) {
  const o = Object.assign({ project: '', repo: '', path: '', hidden: false, ignored: false, repos: true, onOpen: null, onSelect: null, onLoad: null, storageKey: null }, opts || {});
  const project = o.project;
  const storageKey = o.storageKey || `ccboard:tree:${project}/${o.repo}`;
  const inst = { destroyed: false, timer: null, ticking: false, lastTick: Date.now(), typed: '', typedAt: 0, focusRec: null, selected: null, recs: new Map(),
    expanded: new Set(Tree.load(storageKey)) };
  const list = el('ul', { class: 'treelist ftree-list', role: 'tree', 'aria-label': `${project}/${o.repo} files` });
  const wrap = el('div', { class: 'tree ftree' }, list);
  host.append(wrap);
  const root = { isRoot: true, isDir: true, entry: null, name: '', path: '', level: 0, repo: o.repo, rel: o.path || '', parent: null, kids: [], kidMap: new Map(),
    open: true, loaded: false, loading: false, busy: false, seq: 0, ctrl: null, loadP: null, etag: '', ul: list, more: null, moreSig: '', error: null, dead: false,
    truncated: false, total: 0 };
  inst.recs.set('', root);

  const info = (rec) => ({ project, repo: rec.repo, rel: rec.rel, path: rec.path });
  const openState = (rec) => !!rec.isDir && rec.open;
  const persist = (path, on) => {
    if (on) inst.expanded.add(path); else inst.expanded.delete(path);
    Tree.save(storageKey, [...inst.expanded]);
  };

  /* the row that owns a node: walk up to the first node that carries a record (the loading / error rows carry none) */
  const recOf = (node) => {
    for (let n = node; n && n !== wrap; n = n.parentNode) {
      if (n._more) return null;
      if (n._rec) return n._rec;
    }
    return null;
  };
  const within = (node, ancestor) => { for (let n = node; n; n = n.parentNode) if (n === ancestor) return true; return false; };
  const inside = (rec, ancestor) => { for (let n = rec; n; n = n.parent) if (n === ancestor) return true; return false; };
  const shown = (rec) => { for (let n = rec.parent; n; n = n.parent) if (!n.isRoot && !n.open) return false; return true; };

  const visible = () => {
    const out = [];
    const walk = (r) => { for (const k of r.kids) { out.push(k); if (openState(k)) walk(k); } };
    walk(root);
    return out;
  };

  const openLoaded = () => {
    const out = [root];
    const walk = (r) => { for (const k of r.kids) if (openState(k) && k.loaded) { out.push(k); walk(k); } };
    walk(root);
    return out;
  };

  /* ---- painting ---- */

  const statusText = (e) => {
    if (e.type === 'dir' || e.type === 'repo') return e.dirty ? 'changes inside' : '';
    return e.status ? (Tree.STATUS_TITLE[e.status] || e.status) : '';
  };

  const paintSecondary = (rec) => {
    const e = rec.entry;
    const isDir = rec.isDir;
    const sig = `${isDir ? (e.dirty ? 'd' : '') : (e.status || '')}|${isDir ? '' : (e.size === null || e.size === undefined ? '' : e.size)}`;
    if (rec.secSig === sig) return;
    rec.secSig = sig;
    rec.sec.textContent = '';
    if (isDir) {
      if (e.dirty) rec.sec.append(el('span', { class: 'tdot', title: 'uncommitted changes inside', 'aria-hidden': 'true' }));
      return;
    }
    if (typeof e.size === 'number') rec.sec.append(el('span', { class: 'tsize dim mono', text: Tree.fmtSize(e.size) }));
    if (e.status) rec.sec.append(el('span', { class: `tstatus st-${e.status === '?' ? 'new' : e.status}`, title: Tree.STATUS_TITLE[e.status] || e.status, text: e.status }));
  };

  const paintRec = (rec) => {
    if (rec.isRoot) return;                              // the root is the list itself: no row
    const li = rec.li;
    const e = rec.entry;
    const sel = inst.selected === rec;
    li.classList.toggle('open', openState(rec));
    li.classList.toggle('selected', sel);
    li.classList.toggle('dirty', !!e.dirty);
    li.classList.toggle('ignored', !!e.ignored);
    li.classList.toggle('loading', !!rec.loading);
    li.setAttribute('aria-selected', sel ? 'true' : 'false');
    const extra = [statusText(e), e.ignored ? 'ignored' : ''].filter(Boolean).join(', ');
    li.setAttribute('aria-label', extra ? `${rec.name}, ${extra}` : rec.name);
    if (rec.isDir) {
      li.setAttribute('aria-expanded', rec.open ? 'true' : 'false');
      if (rec.caret.classList) rec.caret.classList.toggle('open', rec.open);
      rec.ul.classList.toggle('hidden', !rec.open);
      li.setAttribute('aria-busy', rec.loading ? 'true' : 'false');
    }
    paintSecondary(rec);
  };

  /* the dim row at the end of a directory's list: loading, the error with a retry button, an empty folder, or 'first 1500 of N' */
  const paintMore = (rec) => {
    let msg = '';
    let bad = false;
    if (rec.error) { msg = `could not load: ${rec.error}`; bad = true; }
    else if (rec.loading && !rec.loaded) msg = 'loading…';
    else if (rec.loaded && !rec.kids.length) msg = rec.isRoot ? 'empty' : 'empty folder';
    else if (rec.loaded && rec.truncated) msg = `first ${rec.kids.length} of ${rec.total}`;
    const sig = `${msg}|${bad ? 1 : 0}`;
    if (!msg) { if (rec.more) { rec.more.remove(); rec.more = null; rec.moreSig = ''; } return; }
    if (!rec.more) { rec.more = el('li', { class: 'treemore', role: 'none' }); rec.more._more = true; rec.moreSig = ''; Tree.setVar(rec.more, '--lvl', String(rec.level)); }
    if (rec.moreSig !== sig) {
      rec.moreSig = sig;
      rec.more.textContent = '';
      rec.more.classList.toggle('err', bad);
      rec.more.append(el('span', { class: 'tmore-text', text: msg }));
      if (bad) rec.more.append(el('button', { class: 'minimal small', type: 'button', text: 'retry', onclick: () => { rec.error = null; load(rec); } }));
    }
    if (rec.more.parentNode !== rec.ul || rec.ul.lastChild !== rec.more) rec.ul.append(rec.more);
  };

  const rovingTo = (rec, focus) => {
    if (inst.focusRec && inst.focusRec !== rec && !inst.focusRec.dead) inst.focusRec.li.setAttribute('tabindex', '-1');
    inst.focusRec = rec;
    rec.li.setAttribute('tabindex', '0');
    if (focus) rec.li.focus();
  };

  /* exactly one node is a tab stop: the last focused one while it is still shown, else the first row */
  const ensureTab = () => {
    const f = inst.focusRec;
    if (f && !f.dead && shown(f)) { f.li.setAttribute('tabindex', '0'); return; }
    inst.focusRec = null;
    if (root.kids.length) rovingTo(root.kids[0], false);
  };

  /* ---- records ---- */

  const makeRec = (parent, entry) => {
    const isRepo = entry.type === 'repo';
    const isDir = entry.type === 'dir' || isRepo;
    const path = parent.path ? `${parent.path}/${entry.name}` : entry.name;
    // a board repo at the top of the project folder is addressed by its own name; a repo nested anywhere else keeps the parent's
    // repo and extends the path (the server lists it through the nested repo's own git)
    const ownRepo = isRepo && parent.isRoot && (parent.repo === 'root' || !parent.repo) && !parent.rel;
    const rec = { isRoot: false, isDir, entry, name: entry.name, path, level: parent.level + 1, repo: ownRepo ? entry.name : parent.repo,
      rel: ownRepo ? '' : (parent.rel ? `${parent.rel}/${entry.name}` : entry.name), parent, kids: [], kidMap: new Map(), open: false,
      loaded: !isDir || entry.has_children === false, loading: false, busy: false, seq: 0, ctrl: null, loadP: null, etag: '', ul: null, more: null, moreSig: '',
      error: null, dead: false, truncated: false, total: 0, secSig: null };
    const glyph = el('span', { class: 'tglyph' });
    if (isRepo) glyph.append(ic('git-repo'));
    else if (isDir) glyph.append(el('span', { class: 'g-closed' }, ic('folder-close')), el('span', { class: 'g-open' }, ic('folder-open')));
    else glyph.append(ic(entry.type === 'symlink' ? 'link' : 'document'));
    rec.caret = isDir && entry.has_children !== false ? el('span', { class: 'treecaret', 'aria-hidden': 'true' }) : el('span', { class: 'tcaret-none', 'aria-hidden': 'true' });
    rec.label = el('span', { class: 'treelabel', text: entry.name, title: entry.name });
    rec.sec = el('span', { class: 'treesecondary' });
    rec.content = el('div', { class: 'treecontent' }, rec.caret, glyph, rec.label, rec.sec);
    Tree.setVar(rec.content, '--lvl', String(rec.level - 1));
    rec.li = el('li', { class: `treenode t-${entry.type}`, role: 'treeitem', tabindex: '-1', 'aria-level': String(rec.level), 'aria-selected': 'false',
      'data-path': path, 'data-type': entry.type }, rec.content);
    rec.li._rec = rec;
    if (isDir) {
      rec.ul = el('ul', { class: 'treelist hidden', role: 'group' });
      rec.li.append(rec.ul);
    }
    inst.recs.set(path, rec);
    paintRec(rec);
    return rec;
  };

  const removeRec = (rec) => {
    rec.dead = true;
    rec.seq += 1;
    if (rec.ctrl) { try { rec.ctrl.abort(); } catch (_) { /* already settled */ } rec.ctrl = null; }
    for (const k of rec.kids) removeRec(k);
    if (inst.recs.get(rec.path) === rec) inst.recs.delete(rec.path);
    if (inst.selected === rec) inst.selected = null;
    rec.li.remove();
  };

  /* Patch a directory's rows from a listing: rows keyed by name, patched in place, new ones created (a persisted open directory opens again),
     gone ones removed, DOM order following the payload; a row that holds focus is never moved. */
  const applyListing = (rec, data) => {
    const entries = (data && Array.isArray(data.entries)) ? data.entries : [];
    if (rec.isRoot) inst.meta = data;
    rec.truncated = !!(data && data.truncated);
    rec.total = data && typeof data.total === 'number' ? data.total : entries.length;
    const old = new Map(rec.kids.map((k) => [k.name, k]));
    const next = [];
    const fresh = [];
    for (const entry of entries) {
      if (!entry || typeof entry.name !== 'string') continue;
      let k = old.get(entry.name);
      if (k && k.entry.type !== entry.type) { removeRec(k); old.delete(entry.name); k = null; }
      if (k) { old.delete(entry.name); k.entry = entry; k.label.title = entry.name; paintRec(k); }
      else { k = makeRec(rec, entry); fresh.push(k); }
      next.push(k);
    }
    for (const k of old.values()) removeRec(k);
    rec.kids = next;
    rec.kidMap = new Map(next.map((k) => [k.name, k]));
    const active = typeof document !== 'undefined' ? document.activeElement : null;
    let ref = rec.ul.firstElementChild;
    for (const k of next) {
      if (k.li === ref) { ref = ref.nextElementSibling; continue; }
      if (k.li.parentNode === rec.ul && active && k.li.contains(active)) continue;
      rec.ul.insertBefore(k.li, ref);
    }
    for (const k of fresh) if (k.isDir && inst.expanded.has(k.path)) expand(k, true);
  };

  /* ---- loading ---- */

  const listing = (rec, extra) => Tree.url(project, rec.repo, rec.rel, Object.assign({ hidden: o.hidden, ignored: o.ignored, repos: o.repos }, extra || {}));

  const notify = (rec, data) => {
    if (rec.isRoot && typeof o.onLoad === 'function') { try { o.onLoad(data, rec); } catch (e) { console.error('ccboard tree onLoad', e); } }
  };

  function load(rec) {
    if (rec.loading) return rec.loadP;
    const token = rec.seq + 1;
    rec.seq = token;
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    rec.loading = true;
    rec.error = null;
    rec.ctrl = ctrl;
    paintRec(rec);
    paintMore(rec);
    const settle = (err) => {
      rec.loading = false; rec.ctrl = null; rec.loadP = null;
      if (err) rec.error = (err && err.message) || 'request failed';
    };
    const p = (async () => {
      let res = null;
      try { res = await Tree.fetchJson(listing(rec), { signal: ctrl ? ctrl.signal : null }); } catch (err) {
        if (inst.destroyed || token !== rec.seq) return;          // a collapse (or destroy) cancelled it: nothing to show
        settle(err || new Error('request failed'));
        paintRec(rec);
        paintMore(rec);
        return;
      }
      if (inst.destroyed || token !== rec.seq) return;
      settle(null);
      try {
        rec.etag = res.etag;
        applyListing(rec, res.data);
        rec.loaded = true;
      } catch (e) { console.error('ccboard tree', e); settle(e); }
      paintRec(rec);
      paintMore(rec);
      ensureTab();
      if (!rec.error) notify(rec, res.data);
    })();
    rec.loadP = p;
    return p;
  }

  /* A background check of a loaded directory: If-None-Match unless forced; a failure leaves the rows as they are. */
  async function revalidate(rec, ro) {
    if (rec.busy || rec.loading || !rec.loaded || rec.dead || inst.destroyed) return;
    const f = ro || {};
    rec.busy = true;
    try {
      const res = await Tree.fetchJson(listing(rec, { refresh: f.refresh }), { etag: f.force ? '' : rec.etag });
      if (inst.destroyed || rec.dead || res.status === 304) return;
      rec.etag = res.etag;
      applyListing(rec, res.data);
      paintRec(rec);
      paintMore(rec);
      ensureTab();
      notify(rec, res.data);
    } catch (_) { /* keep what is shown */ } finally { rec.busy = false; }
  }

  const ready = (rec) => (rec.loaded ? Promise.resolve() : (rec.loading ? rec.loadP : load(rec)));

  /* ---- expand, collapse, select ---- */

  function expand(rec, restoring) {
    if (!rec.isDir || rec.open) return;
    rec.open = true;
    if (!restoring) persist(rec.path, true);
    paintRec(rec);
    if (!rec.loaded) load(rec);
  }

  function collapse(rec) {
    if (!rec.isDir || !rec.open) return;
    rec.open = false;
    persist(rec.path, false);
    if (rec.loading) {                                   // the expand is cancelled: its request is aborted
      rec.seq += 1; rec.loading = false; rec.loadP = null;
      if (rec.ctrl) { try { rec.ctrl.abort(); } catch (_) { /* already settled */ } rec.ctrl = null; }
      paintMore(rec);
    }
    if (inst.focusRec && inst.focusRec !== rec && inside(inst.focusRec, rec)) rovingTo(rec, true);
    paintRec(rec);
  }

  const toggle = (rec) => { if (rec.open) collapse(rec); else expand(rec, false); };

  const selectRec = (rec, silent) => {
    if (inst.selected === rec) return;
    const prev = inst.selected;
    inst.selected = rec;
    if (prev && !prev.dead) paintRec(prev);
    paintRec(rec);
    if (!silent && typeof o.onSelect === 'function') o.onSelect(rec.entry, rec.path, info(rec));
  };

  const activate = (rec) => {
    selectRec(rec, false);
    if (rec.isDir) { toggle(rec); return; }
    if (rec.entry.type === 'file' && typeof o.onOpen === 'function') o.onOpen(rec.entry, rec.path, info(rec));
  };

  /* ---- events ---- */

  const typeAhead = (key, rec) => {
    const now = Date.now();
    if (now - inst.typedAt > Tree.TYPEAHEAD_MS) inst.typed = '';
    inst.typedAt = now;
    inst.typed += key.toLowerCase();
    const vis = visible();
    if (!vis.length) return;
    const same = inst.typed.split('').every((c) => c === inst.typed[0]);
    const needle = same ? inst.typed[0] : inst.typed;
    const at = vis.indexOf(rec);
    const from = same || inst.typed.length === 1 ? at + 1 : at;       // a longer prefix refines the current match; a repeated letter cycles
    for (let n = 0; n < vis.length; n++) {
      const cand = vis[(from + n + vis.length) % vis.length];
      if (cand.name.toLowerCase().startsWith(needle)) { rovingTo(cand, true); return; }
    }
  };

  list.addEventListener('click', (e) => {
    const rec = recOf(e.target);
    if (!rec) return;
    rovingTo(rec, false);
    if (rec.isDir && within(e.target, rec.caret)) { toggle(rec); return; }   // the caret only toggles
    activate(rec);
  });

  list.addEventListener('focusin', (e) => {
    const rec = recOf(e.target);
    if (rec && e.target === rec.li) rovingTo(rec, false);
  });

  list.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const rec = recOf(e.target);
    if (!rec || e.target !== rec.li) return;
    if (inst.focusRec !== rec) rovingTo(rec, false);        // a focus that did not announce itself still makes this row the tab stop
    const vis = visible();
    const i = vis.indexOf(rec);
    const k = e.key;
    const go = (n) => { if (n) rovingTo(n, true); };
    let handled = true;
    if (k === 'ArrowDown') go(vis[i + 1]);
    else if (k === 'ArrowUp') go(vis[i - 1]);
    else if (k === 'Home') go(vis[0]);
    else if (k === 'End') go(vis[vis.length - 1]);
    else if (k === 'ArrowRight') {
      if (rec.isDir && !rec.open) expand(rec, false);
      else if (rec.isDir && rec.kids.length) go(rec.kids[0]);
    } else if (k === 'ArrowLeft') {
      if (rec.isDir && rec.open) collapse(rec);
      else if (rec.parent && !rec.parent.isRoot) go(rec.parent);
    } else if (k === 'Enter' || k === ' ') activate(rec);
    else if (k === '*') { for (const s of rec.parent.kids) if (s.isDir) expand(s, false); }
    else if (k.length === 1 && k.trim()) typeAhead(k, rec);
    else handled = false;
    if (handled) e.preventDefault();
  });

  /* ---- revalidation ---- */

  const tick = async () => {
    if (inst.destroyed || inst.ticking) return;
    if (typeof document !== 'undefined' && document.hidden) return;
    inst.ticking = true;
    inst.lastTick = Date.now();
    try { for (const rec of openLoaded()) { if (inst.destroyed) break; await revalidate(rec); } } finally { inst.ticking = false; }
  };

  const onVisible = () => { if (typeof document !== 'undefined' && !document.hidden && Date.now() - inst.lastTick >= Tree.REVALIDATE_MS) tick(); };

  if (!(typeof demoOn === 'function' && demoOn()) && typeof setInterval === 'function') {
    inst.timer = setInterval(tick, Tree.REVALIDATE_MS);
    if (inst.timer && typeof inst.timer.unref === 'function') inst.timer.unref();
  }
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') document.addEventListener('visibilitychange', onVisible);

  /* ---- the handle ---- */

  const handle = {
    root: wrap,
    list,
    get selected() { return inst.selected ? inst.selected.path : null; },
    get loaded() { return root.loaded; },

    /* reload every open directory now, the root first with refresh=1 (the server drops its git caches for it) */
    async refresh() {
      if (!root.loaded) { await ready(root); return; }
      await revalidate(root, { force: true, refresh: true });
      for (const rec of openLoaded()) if (!rec.isRoot) await revalidate(rec, { force: true });
    },

    /* open every directory down to path (and path itself when it is one): {entry, path, repo, rel}, or null when something on the way is missing */
    async expand(path) {
      const segs = String(path || '').split('/').filter(Boolean);
      await ready(root);
      let rec = root;
      for (const seg of segs) {
        if (inst.destroyed || !rec.isDir) return null;
        if (!rec.isRoot && !rec.open) expand(rec, false);
        await ready(rec);
        const k = rec.kidMap.get(seg);
        if (!k) return null;
        rec = k;
      }
      if (inst.destroyed) return null;
      if (rec.isDir && !rec.isRoot) expand(rec, false);
      return { entry: rec.entry || { name: '', type: 'dir' }, path: rec.path, repo: rec.repo, rel: rec.rel };
    },

    select(path) {
      const rec = inst.recs.get(String(path || ''));
      if (!rec || rec.isRoot || rec.dead) return false;
      selectRec(rec, true);
      rovingTo(rec, false);
      if (rec.content && typeof rec.content.scrollIntoView === 'function') { try { rec.content.scrollIntoView({ block: 'nearest' }); } catch (_) { /* no layout */ } }
      return true;
    },

    focus() {
      ensureTab();
      if (inst.focusRec) inst.focusRec.li.focus();
    },

    destroy() {
      if (inst.destroyed) return;
      inst.destroyed = true;
      if (inst.timer) { clearInterval(inst.timer); inst.timer = null; }
      if (typeof document !== 'undefined' && typeof document.removeEventListener === 'function') document.removeEventListener('visibilitychange', onVisible);
      for (const rec of inst.recs.values()) if (rec.ctrl) { try { rec.ctrl.abort(); } catch (_) { /* already settled */ } rec.ctrl = null; }
      wrap.remove();
    },
  };

  load(root);
  return handle;
};

/* ---------- the file preview ---------- */

Tree.previewFile = function (host, opts) {
  const o = Object.assign({ project: '', repo: '', path: '', line: 0, reveal: false }, opts || {});
  const st = { destroyed: false, seq: 0, ctrl: null, line: o.line > 0 ? Math.floor(o.line) : 0, reveal: !!o.reveal, cur: null };
  const pathNode = el('span', { class: 'tp-path mono', text: o.path, title: o.path });
  const metaNode = el('span', { class: 'tp-meta dim mono' });
  const revealBtn = el('button', { class: 'small hidden tp-reveal', type: 'button', text: 'reveal', title: 'show this file although its name looks like a secret', onclick: () => load(true) });
  const codeLink = el('a', { class: 'btn small tp-code hidden', target: '_blank', rel: 'noopener', title: 'open this file in code-server' }, ic('code'), 'open in code-server');
  const head = el('div', { class: 'tp-head' }, pathNode, metaNode, revealBtn, codeLink);
  const body = el('div', { class: 'tp-body', role: 'region', 'aria-label': `contents of ${o.path}`, tabindex: '0' });
  const root = el('div', { class: 'tpreview' }, head, body);
  host.append(root);

  const paintCode = () => {
    const url = Tree.codeUrl(o.project, o.repo, o.path, st.line);
    codeLink.classList.toggle('hidden', !url);
    if (url) codeLink.setAttribute('href', url);
  };

  const message = (title, text, retry) => {
    body.textContent = '';
    const box = el('div', { class: 'tp-msg' }, el('b', { text: title }));
    if (text) box.append(el('span', { class: 'dim', text }));
    if (retry) box.append(el('button', { class: 'small', type: 'button', text: 'retry', onclick: () => load(false) }));
    body.append(box);
  };

  const mark = (li) => {
    if (st.cur) st.cur.classList.remove('cur');
    st.cur = li || null;
    if (li) { li.classList.add('cur'); st.line = li._n; } else st.line = 0;
    paintCode();
  };

  const render = (data) => {
    const text = String((data && data.text) || '');
    const lines = text.split('\n');
    if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
    setText(metaNode, [Tree.fmtSize(data.size), `${lines.length} line${lines.length === 1 ? '' : 's'}`, data.truncated ? 'first 200 KB' : ''].filter(Boolean).join(' · '));
    revealBtn.classList.add('hidden');
    body.textContent = '';
    if (!text) { body.append(el('div', { class: 'tp-msg dim', text: 'empty file' })); paintCode(); return; }
    const ol = el('ol', { class: 'tp-lines' });
    let target = null;
    lines.forEach((line, i) => {
      const li = el('li');
      li.textContent = line;
      li._n = i + 1;
      if (st.line === i + 1) target = li;
      ol.append(li);
    });
    ol.addEventListener('click', (e) => {
      for (let n = e.target; n && n !== ol; n = n.parentNode) if (n._n) { mark(n === st.cur ? null : n); return; }
    });
    body.append(ol);
    if (data.truncated) body.append(el('div', { class: 'tp-foot dim', text: `Only the first 200 KB of ${Tree.fmtSize(data.size)} is shown: open the file in code-server for the rest.` }));
    st.cur = null;
    mark(target);
    if (target && typeof target.scrollIntoView === 'function') { try { target.scrollIntoView({ block: 'center' }); } catch (_) { /* no layout */ } }
  };

  function load(reveal) {
    const token = st.seq + 1;
    st.seq = token;
    if (st.ctrl) { try { st.ctrl.abort(); } catch (_) { /* already settled */ } }
    st.ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    if (reveal) st.reveal = true;
    revealBtn.classList.add('hidden');
    message('loading…');
    paintCode();
    return Tree.fetchJson(Tree.fileUrl(o.project, o.repo, o.path, st.reveal), { signal: st.ctrl ? st.ctrl.signal : null }).then((res) => {
      if (st.destroyed || token !== st.seq) return;
      render(res.data);
    }, (err) => {
      if (st.destroyed || token !== st.seq || (err && err.name === 'AbortError')) return;
      const status = err && err.status;
      const msg = (err && err.message) || '';
      setText(metaNode, '');
      if (status === 403 || /reveal/i.test(msg)) { message('This looks like a secret file', msg || 'Its name matches a secret pattern: reveal it to read it.'); revealBtn.classList.remove('hidden'); }
      else if (status === 415 || /binary/i.test(msg)) message('Binary file', 'No preview for this one: open it in code-server.');
      else if (status === 404 || /not found/i.test(msg)) message('File not found', 'It may have been deleted or moved since the tree was loaded.');
      else message('Could not load this file', msg, true);
    });
  }

  load(false);
  return {
    root,
    reload: () => load(false),
    setLine(n) {
      const lis = body.querySelectorAll('ol.tp-lines li');
      mark(lis[n - 1] || null);
    },
    destroy() {
      if (st.destroyed) return;
      st.destroyed = true;
      if (st.ctrl) { try { st.ctrl.abort(); } catch (_) { /* already settled */ } }
      root.remove();
    },
  };
};
