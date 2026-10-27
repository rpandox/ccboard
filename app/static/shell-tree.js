/* Split for first paint (#333); loaded by lazy.js on use. */
'use strict';

/* ---------- directories under the current project: the repos and the project folder open into lazily loaded folders ---------- */

/* The project the page is about: the project route's, or the project of a session peek. */
Shell.currentProject = function () {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  if (!r) return '';
  if (r.id === 'project') return (r.params && r.params.project) || '';
  if (r.id === 'session') {
    const parts = String((r.params && r.params.tmux) || '').split('--');
    return parts.length === 3 ? parts[0] : '';
  }
  return '';
};

/* 'd:<project>/<repo>/<path>' -> {project, repo, path} (names never hold a slash, so the first two segments are the project and the repo). */
Shell.parseDirKey = function (key) {
  const rest = String(key).slice(2);
  const a = rest.indexOf('/');
  const b = rest.indexOf('/', a + 1);
  return { project: rest.slice(0, a), repo: rest.slice(a + 1, b), path: rest.slice(b + 1) };
};

Shell.dirKey = function (project, repo, path) { return `d:${project}/${repo}/${path || ''}`; };

/* The kids of a project node: its sessions as before; for the current project the repos (and the project folder) become directory nodes. */
Shell.projKids = function (p, level) {
  if (Shell.currentProject() !== p.name) return p.kids;
  const out = [];
  for (const k of p.kids) {
    if (k.kind !== 'repo') { out.push(k); continue; }
    if (k.state === 'cloning' || k.state === 'clone-failed') { out.push(k); continue; }       // nothing to browse yet: the plain link
    out.push({ key: Shell.dirKey(p.name, k.name, ''), kind: 'dir', project: p.name, repo: k.name, path: '', name: k.name, linked: k.linked, target: k.target, glyph: 'repo', level: level + 1, hasKids: true, dirty: false });
  }
  if (p.folder) out.push({ key: Shell.dirKey(p.name, 'root', ''), kind: 'dir', project: p.name, repo: 'root', path: '', name: 'project folder', glyph: 'folder', level: level + 1, hasKids: true, dirty: false });
  return out;
};

Shell.loadDir = function (k, fresh) {
  if (typeof Tree === 'undefined' && typeof Lazy !== 'undefined' && Lazy.bundles && Lazy.bundles.tree) {      // tree.js is a lazy bundle (lazy.js): the first folder to open loads it
    if (!Shell.dirLoading.has(k.key)) Lazy.run('tree', () => Shell.loadDir(k, fresh), 'the file tree');
    return;
  }
  if (typeof Tree === 'undefined' || !Tree || typeof Tree.children !== 'function' || Shell.dirLoading.has(k.key)) return;
  Shell.dirLoading.add(k.key);
  Tree.children(k.project, k.repo, k.path, { repos: true, fresh: !!fresh }).then(
    (data) => {
      const entries = (data && data.entries) || [];
      Shell.dirs.set(k.key, { entries });
      let pruned = false;                                                              // an open key that turned out to be a file (a ?path= to a file) goes
      for (const e of entries) if (e.type === 'file' && Shell.open.delete(Shell.dirKey(k.project, k.repo, k.path ? `${k.path}/${e.name}` : e.name))) pruned = true;
      if (pruned) Shell.saveOpen();
    },
    (e) => {
      if (!Shell.dirs.has(k.key) || !Shell.dirs.get(k.key).entries) Shell.dirs.set(k.key, { error: (e && e.message) || 'could not load' });
      const st = e && (e.status || (/\b(400|403|404)\b|not found|not a ccboard|unsafe|bad path/i.test(String(e.message || '')) ? 404 : 0));
      if ((st === 400 || st === 403 || st === 404) && Shell.open.delete(k.key)) Shell.saveOpen();   // a folder that cannot exist is forgotten, not retried every load
    }).then(() => {
    Shell.dirLoading.delete(k.key);
    Shell.patchTrees();
  });
};

/* The rows inside an open directory node: its sub-directories from the cached listing (asked for when it is not there yet). */
Shell.dirItems = function (k) {
  const hit = Shell.dirs.get(k.key);
  if (!hit) { Shell.loadDir(k, false); return [{ key: 'note:' + k.key, kind: 'note', text: 'loading…' }]; }
  if (hit.error) return [{ key: 'note:' + k.key, kind: 'note', text: 'could not load: ' + hit.error }];
  const out = [];
  for (const e of hit.entries) {
    const topRepo = e.type === 'repo' && k.repo === 'root' && !k.path;                 // the project folder's own repos are top-level nodes already
    if (e.type !== 'dir' && !(e.type === 'repo' && !topRepo)) continue;
    const path = k.path ? `${k.path}/${e.name}` : e.name;
    const repo = k.repo;                                                                 // a nested repo opens in place through the same repo name
    out.push({ key: Shell.dirKey(k.project, repo, path), kind: 'dir', project: k.project, repo, path, name: e.name,
      glyph: e.type === 'repo' ? 'repo' : 'dir', level: k.level + 1, hasKids: e.has_children !== false, dirty: !!e.dirty });
  }
  if (!out.length) out.push({ key: 'note:' + k.key, kind: 'note', text: 'no subfolders' });
  return out;
};

Shell.dirNode = function () {
  const tw = el('span', { class: 'tw', 'aria-hidden': 'true' }, ic('chevron-right'));
  const glyph = el('span', { class: 'tn-g' });
  const name = el('span', { class: 'tn-name' });
  const dot = el('span', { class: 'tn-dot dirty hidden', title: 'uncommitted changes inside', 'aria-hidden': 'true' });
  const row = el('a', { class: 'tn-row d-row', role: 'treeitem', tabindex: '-1', 'aria-level': '2', 'data-route': 'tree-dir' }, tw, glyph, name, dot);
  const kids = el('div', { class: 'tn-kids hidden', role: 'group' });
  const node = el('div', { class: 'tn dirn' }, row, kids);
  node._d = { row, glyph, name, dot, kids, g: null };
  return node;
};

/* Is this directory the one the Files tab shows (its repo, ?tab=files and ?path= equal to it)? */
Shell.dirCurrent = function (k) {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  if (!r || r.id !== 'project' || !r.params || r.params.project !== k.project || r.params.repo !== k.repo) return false;
  const q = r.query || {};
  return q.tab === 'files' && (q.path || '') === k.path;
};

Shell.patchDir = function (node, k) {
  const d = node._d;
  const open = Shell.open.has(k.key);
  node.setAttribute('data-key', k.key);
  node.classList.toggle('open', open && k.hasKids);
  d.row.setAttribute('href', Shell.hash('project', { project: k.project, repo: k.repo }, k.path ? { tab: 'files', path: k.path } : { tab: 'files' }));
  d.row.setAttribute('aria-level', String(k.level));
  Shell.setVar(d.row, '--lvl', String(k.level));
  d.row.classList.toggle('leaf', !k.hasKids);
  if (k.hasKids) d.row.setAttribute('aria-expanded', open ? 'true' : 'false'); else d.row.removeAttribute('aria-expanded');
  d.row.classList.toggle('cur', Shell.dirCurrent(k));
  if (Shell.dirCurrent(k)) d.row.setAttribute('aria-selected', 'true'); else d.row.removeAttribute('aria-selected');
  if (d.g !== k.glyph) { d.g = k.glyph; d.glyph.textContent = ''; d.glyph.append(ic(k.glyph === 'repo' ? 'git-repo' : 'folder-close')); }
  patchLinkedName(d.name, k.name, k);
  d.row.setAttribute('title', k.path || (k.repo === 'root' ? 'the project folder' : k.repo));
  d.dot.classList.toggle('hidden', !k.dirty);
  const showKids = open && k.hasKids;
  d.kids.classList.toggle('hidden', !showKids);
  if (showKids) Shell.sync(d.kids, Shell.dirItems(k), Shell.kidNode, Shell.patchKid);
  else if (d.kids.firstChild) d.kids.textContent = '';
};

/* The current project's node opens (and so do the repo and the folders down to the ?path= of the Files tab) when the page is entered; open
   directories refresh their listing then. Called on every route change and once when the shell is installed. */
Shell.openCurrent = function () {
  const r = typeof currentRoute === 'function' ? currentRoute() : null;
  if (r && r.id === 'project' && r.params && r.params.project) {
    const proj = r.params.project;
    let changed = false;
    const add = (key) => { if (!Shell.open.has(key)) { Shell.open.add(key); changed = true; } };
    add('p:' + proj);
    if (r.params.repo) {
      add(Shell.dirKey(proj, r.params.repo, ''));
      let acc = '';
      const segs = String((r.query && r.query.path) || '').split('/').filter(Boolean);
      if (!segs.some((s) => s === '..' || s === '.' || s === '.git')) {                   // a bad deep link never becomes a persisted key
        for (const seg of segs) { acc = acc ? `${acc}/${seg}` : seg; add(Shell.dirKey(proj, r.params.repo, acc)); }   // a file's key is dropped again once its folder is listed (loadDir)
      }
    }
    if (changed) Shell.saveOpen();
    // open folders re-read their listing; the ?path= itself is left to its own node (it may be a file, which has none)
    const here = r.params.repo ? Shell.dirKey(proj, r.params.repo, String((r.query && r.query.path) || '')) : '';
    for (const key of Shell.open) if (key.startsWith(`d:${proj}/`) && key !== here) Shell.loadDir({ key, ...Shell.parseDirKey(key) }, true);
  }
  Shell.patchTrees();
};

