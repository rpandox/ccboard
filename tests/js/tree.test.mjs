// Contract tests for app/static/tree.js (v0.5.6): the lazy directory tree (WAI-ARIA tree pattern, keyboard, abortable expands, persisted open set,
// ETag revalidation, truncation, nested repos), the file preview (gutter, 403 / 415 / 404, reveal, code-server link) and Tree.children. The real
// scripts run inside the vm harness on minidom; fetch is a fake backend that records every request, can hold answers back and honours If-None-Match.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { fakeState } from './world.mjs';

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const text = (n) => (n ? n.textContent : '');

const entry = (name, type = 'file', over = {}) => ({ name, type, status: null, dirty: false, has_children: type === 'dir' || type === 'repo', ignored: false,
  size: type === 'file' ? 100 : null, ...over });
const listing = (repo, path, entries, over = {}) => ({ project: 'shop', repo, path, git: true, branch: 'main', ahead: 0, behind: 0, entries, truncated: false, total: entries.length,
  hidden: false, ignored: false, status_stale: false, etag: `e-${repo}-${path || 'top'}-${entries.length}`, ...over });

/** A backend: levels is {'<repo>|<path>': payload}, files {'<repo>|<path>': {status, body | error}}. hold('<repo>|<path>') makes that request wait for release(). */
function backend(levels, files = {}) {
  const b = { levels, files, requests: [], held: new Map(), aborted: [] };
  b.hold = (key) => { b.held.set(key, []); };
  b.release = (key) => { for (const f of b.held.get(key) || []) f(); b.held.delete(key); };
  b.fetch = async (url, init = {}) => {
    const u = new URL(url, 'https://box');
    const m = /^\/api\/projects\/([^/]+)\/repos\/([^/]+)\/(tree|file)$/.exec(u.pathname);
    const repo = decodeURIComponent(m[2]);
    const key = `${repo}|${u.searchParams.get('path') || ''}`;
    const headers = init.headers || {};
    const rec = { url, kind: m[3], key, repo, path: u.searchParams.get('path') || '', inm: headers['If-None-Match'] || null, refresh: u.searchParams.get('refresh'),
      hidden: u.searchParams.get('hidden'), ignored: u.searchParams.get('ignored'), repos: u.searchParams.get('repos'), reveal: u.searchParams.get('reveal') };
    b.requests.push(rec);
    if (b.held.has(key)) {
      await new Promise((resolve, reject) => {
        b.held.get(key).push(resolve);
        if (init.signal) init.signal.addEventListener('abort', () => { b.aborted.push(key); const e = new Error('aborted'); e.name = 'AbortError'; reject(e); });
      });
    }
    const reply = (status, data, tag) => ({ status, ok: status >= 200 && status < 300, statusText: String(status), json: async () => data, headers: { get: (k) => (k.toLowerCase() === 'etag' ? tag || null : null) } });
    if (m[3] === 'file') {
      const f = b.files[key];
      if (!f) return reply(404, { error: 'not found' });
      const r = f.status === 403 && rec.reveal === '1' && f.reveal ? f.reveal : f;
      return r.status === 200 ? reply(200, r.body) : reply(r.status, { error: r.error });
    }
    const t = b.levels[key];
    if (!t) return reply(404, { error: 'not found' });
    const tag = `W/"${t.etag}"`;
    if (rec.inm && rec.inm === tag) return reply(304, null, tag);
    return reply(200, t, tag);
  };
  return b;
}

function treeWorld(b, { state } = {}) {
  const w = makeWorld({ fetch: b.fetch, AbortController });
  const dom = installDom(w);
  for (const f of ['core.js', 'components.js', 'tree.js']) w.load(f);
  if (state) { w.ctx.__st = state; w.run('state = __st'); }
  const host = w.document.querySelector('#page');
  return { w, dom, host };
}

const TOP = () => listing('api', '', [entry('docs', 'dir'), entry('src', 'dir', { dirty: true }), entry('empty', 'dir', { has_children: false }), entry('.env.example'), entry('README.md', 'file', { status: 'M', dirty: true }), entry('zeta.txt')]);
const SRC = () => listing('api', 'src', [entry('lib', 'dir'), entry('app.js'), entry('b.js', 'file', { status: '?', dirty: true })]);
const LIB = () => listing('api', 'src/lib', [entry('util.js')]);
const DOCS = () => listing('api', 'docs', [entry('guide.md')]);
const levels = () => ({ 'api|': TOP(), 'api|src': SRC(), 'api|src/lib': LIB(), 'api|docs': DOCS() });

const mount = (w, host, opts = {}) => {
  w.ctx.__host = host; w.ctx.__opts = opts;
  w.run('globalThis.__events = []; globalThis.__tree = Tree.mount(__host, { project: "shop", repo: "api", onOpen: (e, p, i) => __events.push(["open", e.name, p, i.repo, i.rel]), onSelect: (e, p) => __events.push(["select", e.name, p]), ...__opts })');
  return w.get('__tree');
};
const events = (w) => plain(w.get('__events'));
const items = (host) => host.querySelectorAll('li').filter((n) => n.getAttribute('role') === 'treeitem');
const names = (host) => items(host).filter((n) => !isHidden(n)).map((n) => n.getAttribute('data-path'));
const isHidden = (n) => { for (let x = n.parentNode; x && x.getAttribute; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const item = (host, p) => items(host).find((n) => n.getAttribute('data-path') === p);
const key = (w, node, k, extra = {}) => {
  const e = { type: 'keydown', key: k, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, prevented: false, ...extra };
  e.preventDefault = () => { e.prevented = true; };
  node.dispatchEvent(e);
  return e;
};
const focused = (w) => w.document.activeElement;
const paths = (b) => b.requests.filter((r) => r.kind === 'tree').map((r) => r.key);

// ---------------------------------------------------------------- ARIA structure

test('the tree is one role=tree with a treeitem per row; aria-expanded only on directories; one tab stop; levels and selection', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  const lists = host.querySelectorAll('ul');
  assert.equal(lists.filter((n) => n.getAttribute('role') === 'tree').length, 1, 'role=tree exactly once');
  assert.equal(items(host).length, 6);
  for (const li of items(host)) {
    const dir = li.getAttribute('data-type') === 'dir';
    assert.equal(li.getAttribute('aria-expanded') !== null, dir, `${li.getAttribute('data-path')}: aria-expanded only on directories`);
    assert.equal(li.getAttribute('aria-level'), '1');
    assert.equal(li.getAttribute('aria-selected'), 'false');
  }
  assert.deepEqual(items(host).map((n) => n.getAttribute('data-path')), ['docs', 'src', 'empty', '.env.example', 'README.md', 'zeta.txt'], 'the server order is kept');
  assert.equal(items(host).filter((n) => n.getAttribute('tabindex') === '0').length, 1, 'roving tabindex: exactly one tab stop');
  assert.equal(item(host, 'docs').getAttribute('tabindex'), '0', 'the first row');
  assert.ok(host.querySelectorAll('ul').filter((n) => n.getAttribute('role') === 'group').length >= 3, 'each directory has a group');
  assert.ok(item(host, 'src').classList.contains('dirty'), 'a directory with changes below is marked dirty');
  assert.equal(text(item(host, 'README.md').querySelector('.tstatus')), 'M', 'a file carries its status letter');
  assert.equal(text(item(host, 'b.js') || { textContent: '' }), '', 'src is not loaded until opened');
  tree.destroy();
});

test('the semantic classes of core.js give the bp5-tree markup; tree.js itself names no Blueprint class', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  const li = item(host, 'README.md');
  assert.ok(li.classList.contains('bp5-tree-node'));
  assert.ok(li.querySelector('.treecontent').classList.contains('bp5-tree-node-content'));
  assert.ok(li.querySelector('.treelabel').classList.contains('bp5-tree-node-label'));
  assert.ok(li.querySelector('.treesecondary').classList.contains('bp5-tree-node-secondary-label'));
  assert.ok(item(host, 'src').querySelector('.treecaret').classList.contains('bp5-tree-node-caret'));
  assert.ok(host.querySelector('.tree').classList.contains('bp5-tree'));
  tree.destroy();
});

// ---------------------------------------------------------------- keyboard

test('Right expands and loads, Down moves, Enter on a file opens it, Left collapses then moves to the parent', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  const src = item(host, 'src');
  src.focus();
  key(w, src, 'ArrowDown');                                     // moves off docs? focus was on src itself: next is empty
  assert.equal(focused(w), item(host, 'empty'));
  key(w, focused(w), 'ArrowUp');
  assert.equal(focused(w), src);
  const e = key(w, src, 'ArrowRight');
  assert.ok(e.prevented, 'a handled key is prevented');
  assert.equal(src.getAttribute('aria-expanded'), 'true');
  assert.equal(item(host, 'src').getAttribute('tabindex'), '0', 'the focused row is the tab stop');
  await tick();
  assert.deepEqual(paths(b), ['api|', 'api|src'], 'the children came from one request on first expand');
  assert.deepEqual(names(host), ['docs', 'src', 'src/lib', 'src/app.js', 'src/b.js', 'empty', '.env.example', 'README.md', 'zeta.txt']);
  assert.equal(item(host, 'src/app.js').getAttribute('aria-level'), '2');
  key(w, src, 'ArrowRight');                                    // already open: move in
  assert.equal(focused(w), item(host, 'src/lib'));
  key(w, focused(w), 'ArrowDown');
  assert.equal(focused(w), item(host, 'src/app.js'));
  key(w, focused(w), 'Enter');
  assert.deepEqual(events(w).filter((x) => x[0] === 'open'), [['open', 'app.js', 'src/app.js', 'api', 'src/app.js']]);
  assert.equal(item(host, 'src/app.js').getAttribute('aria-selected'), 'true');
  key(w, focused(w), 'ArrowLeft');                              // a file: to the parent
  assert.equal(focused(w), src);
  key(w, src, 'ArrowLeft');                                     // an open directory: collapse
  assert.equal(src.getAttribute('aria-expanded'), 'false');
  assert.deepEqual(names(host), ['docs', 'src', 'empty', '.env.example', 'README.md', 'zeta.txt']);
  key(w, src, 'ArrowRight');
  assert.deepEqual(paths(b), ['api|', 'api|src'], 'a directory that was loaded is not requested again on re-expand');
  tree.destroy();
});

test('Enter on a directory toggles it; Home and End; * opens every sibling directory; the caret only toggles', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  const docs = item(host, 'docs');
  docs.focus();
  key(w, docs, 'Enter');
  assert.equal(docs.getAttribute('aria-expanded'), 'true');
  assert.deepEqual(events(w).filter((x) => x[0] === 'open'), [], 'a directory is not an open');
  key(w, docs, 'End');
  assert.equal(focused(w), item(host, 'zeta.txt'));
  key(w, focused(w), 'Home');
  assert.equal(focused(w), docs);
  key(w, docs, '*');
  await tick();
  assert.equal(item(host, 'src').getAttribute('aria-expanded'), 'true');
  assert.equal(item(host, 'empty').getAttribute('aria-expanded'), 'true', 'an empty directory opens too (nothing to load)');
  assert.deepEqual(paths(b).sort(), ['api|', 'api|docs', 'api|src']);
  // a click on a file row opens it; on the caret it only toggles (no select event for the toggle row's file open)
  const guide = item(host, 'docs/guide.md');
  guide.querySelector('.treecontent').dispatchEvent({ type: 'click', preventDefault() {} });
  assert.deepEqual(events(w).filter((x) => x[0] === 'open').pop(), ['open', 'guide.md', 'docs/guide.md', 'api', 'docs/guide.md']);
  item(host, 'docs').querySelector('.treecaret').dispatchEvent({ type: 'click', preventDefault() {} });
  assert.equal(item(host, 'docs').getAttribute('aria-expanded'), 'false');
  tree.destroy();
});

test('type-ahead moves to the next row starting with the typed letters; the buffer clears after a pause; modifier keys are left alone', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  w.run('Tree.TYPEAHEAD_MS = 40');
  const tree = mount(w, host);
  await tick();
  const docs = item(host, 'docs');
  docs.focus();
  key(w, docs, 'r');
  assert.equal(focused(w), item(host, 'README.md'));
  key(w, focused(w), 'z');                                       // 'rz' matches nothing: focus stays
  assert.equal(focused(w), item(host, 'README.md'));
  await tick(60);
  key(w, focused(w), 'z');
  assert.equal(focused(w), item(host, 'zeta.txt'));
  await tick(60);
  key(w, focused(w), 'd');
  assert.equal(focused(w), docs);
  const e = key(w, docs, 'r', { ctrlKey: true });
  assert.equal(e.prevented, false);
  assert.equal(focused(w), docs);
  tree.destroy();
});

// ---------------------------------------------------------------- abort, persistence, revalidation

test('a collapse while the children are loading aborts the request; a later expand asks again', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  b.hold('api|src');
  const src = item(host, 'src');
  src.focus();
  key(w, src, 'ArrowRight');
  await tick();
  assert.equal(src.getAttribute('aria-busy'), 'true');
  assert.equal(text(src.querySelector('.treemore')), 'loading…');
  key(w, src, 'ArrowLeft');                                      // collapse mid-flight
  await tick();
  assert.deepEqual(b.aborted, ['api|src'], 'the AbortController of that expand fired');
  assert.equal(src.getAttribute('aria-busy'), 'false');
  assert.equal(src.querySelector('.treemore'), null, 'no stray row left behind');
  b.release('api|src');
  key(w, src, 'ArrowRight');
  await tick();
  assert.deepEqual(paths(b), ['api|', 'api|src', 'api|src'], 'the cancelled expand loaded nothing, so the next one requests again');
  assert.ok(item(host, 'src/app.js'));
  tree.destroy();
});

test('a failed load shows the error with a retry button, and the retry loads', async () => {
  const lv = levels();
  delete lv['api|src'];
  const b = backend(lv);
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  const src = item(host, 'src');
  src.focus();
  key(w, src, 'ArrowRight');
  await tick();
  const more = src.querySelector('.treemore');
  assert.match(text(more), /could not load: not found/);
  b.levels['api|src'] = SRC();
  more.querySelector('button').click();
  await tick();
  assert.ok(item(host, 'src/app.js'), 'the retry loaded the level');
  assert.equal(src.querySelector('.treemore'), null);
  tree.destroy();
});

test('the open set persists under storageKey and a new tree opens those directories again (nested ones too)', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  const src = item(host, 'src');
  src.focus();
  key(w, src, 'ArrowRight');
  await tick();
  key(w, item(host, 'src/lib'), 'ArrowRight');
  await tick();
  assert.deepEqual(plain(JSON.parse(w.localStorage.getItem('ccboard:tree:shop/api'))).sort(), ['src', 'src/lib']);
  key(w, item(host, 'src/lib'), 'ArrowLeft');
  assert.deepEqual(plain(JSON.parse(w.localStorage.getItem('ccboard:tree:shop/api'))), ['src'], 'a collapse removes it');
  key(w, item(host, 'src/lib'), 'ArrowRight');
  tree.destroy();
  assert.equal(host.querySelectorAll('li').length, 0, 'destroy removes the markup');
  await tick();
  mount(w, host);
  await tick(10);
  assert.deepEqual(names(host), ['docs', 'src', 'src/lib', 'src/lib/util.js', 'src/app.js', 'src/b.js', 'empty', '.env.example', 'README.md', 'zeta.txt']);
  w.get('__tree').destroy();
});

test('open directories revalidate with If-None-Match: a 304 changes nothing, a 200 patches the rows in place and keeps focus', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  w.run('Tree.REVALIDATE_MS = 25');
  const tree = mount(w, host);
  await tick();
  const src = item(host, 'src');
  src.focus();
  key(w, src, 'ArrowRight');
  await tick();
  const before = new Map(items(host).map((n) => [n.getAttribute('data-path'), n]));
  await tick(120);
  const inm = b.requests.filter((r) => r.inm);
  assert.ok(inm.length >= 4, 'the root and src revalidated more than once');
  assert.ok(inm.every((r) => /^W\/"/.test(r.inm)), 'weak ETag from the first answer');
  assert.deepEqual(items(host).map((n) => n.getAttribute('data-path')), [...before.keys()]);
  for (const [p, n] of before) assert.equal(item(host, p), n, `${p}: the same node after a 304`);
  // the server's answer changes: a new file, one gone, a status flipped
  b.levels['api|src'] = listing('api', 'src', [entry('lib', 'dir'), entry('app.js', 'file', { status: 'M', dirty: true }), entry('new.js')], { etag: 'src-2' });
  await tick(120);
  assert.deepEqual(names(host).filter((p) => p.startsWith('src/')), ['src/lib', 'src/app.js', 'src/new.js']);
  assert.equal(item(host, 'src/app.js'), before.get('src/app.js'), 'a patched row is the same node');
  assert.equal(text(item(host, 'src/app.js').querySelector('.tstatus')), 'M');
  assert.equal(item(host, 'src/b.js'), undefined, 'a row that is gone is removed');
  assert.equal(focused(w), src, 'focus did not move');
  tree.destroy();
  const n = b.requests.length;
  await tick(80);
  assert.equal(b.requests.length, n, 'destroy stopped the timer');
});

test('revalidation waits while the page is hidden and resumes when it is visible', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  w.run('Tree.REVALIDATE_MS = 20');
  const tree = mount(w, host);
  await tick();
  w.document.hidden = true;
  const n = b.requests.length;
  await tick(100);
  assert.equal(b.requests.length, n, 'no request while hidden');
  w.document.hidden = false;
  await tick(60);
  assert.ok(b.requests.length > n);
  tree.destroy();
});

test('refresh() reloads every open directory, the root first with refresh=1 and without If-None-Match', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  key(w, item(host, 'src'), 'ArrowRight');
  await tick();
  const n = b.requests.length;
  b.levels['api|'] = listing('api', '', [entry('src', 'dir'), entry('fresh.txt')], { etag: 'top-2' });
  await tree.refresh();
  const sent = b.requests.slice(n).map((r) => [r.key, r.refresh, r.inm]);
  assert.deepEqual(sent, [['api|', '1', null], ['api|src', null, null]]);
  assert.deepEqual(names(host).slice(0, 2), ['src', 'src/lib']);
  assert.ok(item(host, 'fresh.txt'));
  assert.equal(item(host, 'docs'), undefined);
  tree.destroy();
});

// ---------------------------------------------------------------- truncation, nested repos, flags

test('a repo nested inside a repo opens in place through the SAME repo name and an extended path (never its own repo name)', async () => {
  const b = backend({
    'api|': listing('api', '', [entry('vendor', 'dir'), entry('README.md')]),
    'api|vendor': listing('api', 'vendor', [entry('lib', 'repo', { dirty: true })]),
    'api|vendor/lib': listing('api', 'vendor/lib', [entry('index.js'), entry('src', 'dir')], { branch: 'dev' }),
    'api|vendor/lib/src': listing('api', 'vendor/lib/src', [entry('a.js')]),
  });
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  key(w, item(host, 'vendor'), 'ArrowRight'); await tick();
  assert.equal(item(host, 'vendor/lib').getAttribute('data-type'), 'repo');
  key(w, item(host, 'vendor/lib'), 'ArrowRight'); await tick();
  assert.deepEqual(paths(b), ['api|', 'api|vendor', 'api|vendor/lib'], 'the nested repo is read through the parent repo with its path, not as repo "lib"');
  assert.ok(!b.requests.some((r) => r.repo === 'lib'), 'no request ever names the nested repo as a board repo');
  assert.deepEqual(names(host).slice(0, 5), ['vendor', 'vendor/lib', 'vendor/lib/index.js', 'vendor/lib/src', 'README.md'], 'server order, nested repo rows in place');
  key(w, item(host, 'vendor/lib/src'), 'ArrowRight'); await tick();
  assert.equal(paths(b).at(-1), 'api|vendor/lib/src', 'directories below the nested repo keep the same addressing');
  tree.destroy();
});


test('a truncated level ends in a first-N-of-M row', async () => {
  const many = Array.from({ length: 5 }, (_, i) => entry(`f${i}.txt`));
  const b = backend({ 'api|': listing('api', '', many, { truncated: true, total: 1840 }) });
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  assert.equal(text(host.querySelector('.treemore')), 'first 5 of 1840');
  assert.equal(host.querySelector('.treemore').getAttribute('role'), 'none');
  assert.equal(items(host).length, 5, 'the note is not a treeitem');
  host.querySelector('.treemore').dispatchEvent({ type: 'click', preventDefault() {} });
  assert.deepEqual(events(w), [], 'a click on the note does nothing');
  tree.destroy();
});

test('an empty folder says so and an empty root says empty', async () => {
  const b = backend({ 'api|': listing('api', '', [entry('hollow', 'dir')]), 'api|hollow': listing('api', 'hollow', []) });
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  await tick();
  key(w, item(host, 'hollow'), 'ArrowRight');
  await tick();
  assert.equal(text(item(host, 'hollow').querySelector('.treemore')), 'empty folder');
  tree.destroy();
});

test('entries of type repo open that repo\'s own tree in place; callbacks carry the repo and the path inside it', async () => {
  const root = listing('root', '', [entry('docs', 'dir'), entry('website', 'repo', { dirty: true }), entry('notes.txt')]);
  const web = listing('website', '', [entry('src', 'dir'), entry('README.md')]);
  const webSrc = listing('website', 'src', [entry('a.ts')]);
  const b = backend({ 'root|': root, 'website|': web, 'website|src': webSrc });
  const { w, host } = treeWorld(b);
  const tree = mount(w, host, { repo: 'root' });
  await tick();
  const repo = item(host, 'website');
  assert.ok(repo.querySelector('.bp5-icon-git-repo'), 'the repo glyph');
  assert.equal(repo.getAttribute('aria-expanded'), 'false');
  repo.focus();
  key(w, repo, 'ArrowRight');
  await tick();
  assert.deepEqual(paths(b), ['root|', 'website|'], 'the nested repo is listed through its own endpoint');
  key(w, item(host, 'website/src'), 'ArrowRight');
  await tick();
  assert.deepEqual(paths(b), ['root|', 'website|', 'website|src']);
  key(w, item(host, 'website/src/a.ts'), 'Enter');
  assert.deepEqual(events(w).filter((x) => x[0] === 'open'), [['open', 'a.ts', 'website/src/a.ts', 'website', 'src/a.ts']]);
  tree.destroy();
});

test('the hidden / ignored / repos flags go to the endpoint; type symlink is a leaf that never opens', async () => {
  const b = backend({ 'api|': listing('api', '', [entry('link', 'symlink', { has_children: false }), entry('.git-ignored', 'file', { ignored: true })]) });
  const { w, host } = treeWorld(b);
  const tree = mount(w, host, { hidden: true, ignored: true, repos: false });
  await tick();
  assert.deepEqual(plain(b.requests[0]), { ...plain(b.requests[0]), hidden: '1', ignored: '1', repos: '0' });
  const link = item(host, 'link');
  assert.equal(link.getAttribute('aria-expanded'), null);
  assert.ok(link.querySelector('.bp5-icon-link'));
  key(w, link, 'Enter');
  assert.deepEqual(events(w).filter((x) => x[0] === 'open'), [], 'a symlink is never opened');
  assert.ok(item(host, '.git-ignored').classList.contains('ignored'));
  tree.destroy();
});

// ---------------------------------------------------------------- expand(path) and select(path)

test('expand(path) opens every directory down to it and resolves its entry; a missing path resolves null', async () => {
  const b = backend(levels());
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  const hit = await tree.expand('src/lib/util.js');
  assert.deepEqual(plain({ ...hit, entry: hit.entry.type }), { entry: 'file', path: 'src/lib/util.js', repo: 'api', rel: 'src/lib/util.js' });
  assert.deepEqual(paths(b), ['api|', 'api|src', 'api|src/lib']);
  assert.equal(item(host, 'src').getAttribute('aria-expanded'), 'true');
  assert.equal(item(host, 'src/lib').getAttribute('aria-expanded'), 'true');
  assert.equal(tree.select('src/lib/util.js'), true);
  assert.equal(tree.selected, 'src/lib/util.js');
  assert.equal(item(host, 'src/lib/util.js').getAttribute('aria-selected'), 'true');
  assert.equal(item(host, 'src/lib/util.js').getAttribute('tabindex'), '0', 'select moves the tab stop');
  assert.deepEqual(events(w), [], 'select() from code raises no callback');
  const dir = await tree.expand('docs');
  assert.equal(dir.entry.type, 'dir');
  assert.equal(item(host, 'docs').getAttribute('aria-expanded'), 'true');
  assert.equal(await tree.expand('src/nope/x.js'), null);
  assert.equal(await tree.expand('README.md/x'), null, 'through a file is nothing');
  assert.equal(tree.select('src/nope'), false);
  assert.deepEqual((await tree.expand('')).path, '');
  tree.destroy();
});

test('an unreachable root shows an error row and expand() resolves null instead of hanging', async () => {
  const b = backend({});
  const { w, host } = treeWorld(b);
  const tree = mount(w, host);
  assert.equal(await tree.expand('a/b'), null);
  assert.match(text(host.querySelector('.treemore')), /could not load: not found/);
  tree.destroy();
});

// ---------------------------------------------------------------- the file preview

const FILES = () => ({
  'api|src/app.js': { status: 200, body: { path: 'src/app.js', size: 41, mtime: 1790990000, truncated: false, lines: 3, text: 'const a = 1;\n\n<b>&lt;</b>\n' } },
  'api|.env': { status: 403, error: 'this looks like a secret file: reveal it to read it', reveal: { status: 200, body: { path: '.env', size: 8, mtime: 1, truncated: false, lines: 1, text: 'KEY=abc\n' } } },
  'api|logo.png': { status: 415, error: 'binary file: no preview' },
  'api|big.log': { status: 200, body: { path: 'big.log', size: 300000, mtime: 1, truncated: true, lines: 2, text: 'a\nb\n' } },
});
const STATE = () => fakeState({ projects: [{ name: 'shop', path: '/srv/projects/shop', root: null, orphan_sessions: [], repos: [{ name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, sessions: [] }] }] });

test('previewFile: a gutter of one li per line, set by textContent (markup in a file stays text), header with path, size and the code-server link', async () => {
  const b = backend({}, FILES());
  const { w, host } = treeWorld(b, { state: STATE() });
  w.ctx.__host = host;
  w.run('globalThis.__pv = Tree.previewFile(__host, { project: "shop", repo: "api", path: "src/app.js" })');
  await tick();
  const lines = host.querySelectorAll('ol.tp-lines li');
  assert.equal(lines.length, 3);
  assert.deepEqual(lines.map(text), ['const a = 1;', '', '<b>&lt;</b>']);
  assert.equal(host.querySelectorAll('ol.tp-lines b').length, 0, 'no element was created from file content');
  assert.equal(text(host.querySelector('.tp-path')), 'src/app.js');
  assert.match(text(host.querySelector('.tp-meta')), /41 B · 3 lines/);
  const link = host.querySelector('a.tp-code');
  assert.equal(link.classList.contains('hidden'), false);
  const href = new URL(link.getAttribute('href'));
  assert.equal(href.origin, 'https://box:10000');
  assert.equal(href.searchParams.get('folder'), '/srv/projects/shop/api');
  assert.deepEqual(JSON.parse(href.searchParams.get('payload')), [['openFile', 'vscode-remote:///srv/projects/shop/api/src/app.js']]);
  // a click on a line marks it and puts it in the link
  lines[2].dispatchEvent({ type: 'click' });
  assert.ok(lines[2].classList.contains('cur'));
  assert.deepEqual(JSON.parse(new URL(host.querySelector('a.tp-code').getAttribute('href')).searchParams.get('payload')), [['openFile', 'vscode-remote:///srv/projects/shop/api/src/app.js:3']]);
  lines[2].dispatchEvent({ type: 'click' });
  assert.equal(lines[2].classList.contains('cur'), false, 'a second click clears it');
  w.get('__pv').destroy();
  assert.equal(host.querySelectorAll('.tpreview').length, 0);
});

test('previewFile: 403 offers reveal (and reveal=1 shows the file), 415 says binary, 404 says gone, other errors can retry; an initial line is marked', async () => {
  const b = backend({}, FILES());
  const { w, host } = treeWorld(b, { state: STATE() });
  const open = async (path, extra = '') => {
    w.ctx.__host = host;
    w.run(`globalThis.__pv = Tree.previewFile(__host, { project: "shop", repo: "api", path: ${JSON.stringify(path)}${extra} })`);
    await tick();
    return w.get('__pv');
  };
  let pv = await open('.env');
  assert.match(text(host.querySelector('.tp-msg')), /secret/);
  const reveal = host.querySelector('.tp-reveal');
  assert.equal(reveal.classList.contains('hidden'), false, 'the reveal button shows on a 403');
  assert.equal(host.querySelectorAll('ol.tp-lines').length, 0);
  reveal.click();
  await tick();
  assert.equal(b.requests.at(-1).reveal, '1');
  assert.deepEqual(host.querySelectorAll('ol.tp-lines li').map(text), ['KEY=abc']);
  assert.equal(reveal.classList.contains('hidden'), true);
  pv.destroy();
  pv = await open('logo.png');
  assert.match(text(host.querySelector('.tp-msg')), /Binary file/);
  assert.equal(host.querySelector('.tp-reveal').classList.contains('hidden'), true);
  assert.equal(host.querySelector('a.tp-code').classList.contains('hidden'), false, 'binary files still link to code-server');
  pv.destroy();
  pv = await open('gone.txt');
  assert.match(text(host.querySelector('.tp-msg')), /File not found/);
  pv.destroy();
  pv = await open('big.log');
  assert.match(text(host.querySelector('.tp-foot')), /first 200 KB of 293 KB/);
  assert.match(text(host.querySelector('.tp-meta')), /first 200 KB/);
  pv.destroy();
  pv = await open('src/app.js', ', line: 3');
  assert.ok(host.querySelectorAll('ol.tp-lines li')[2].classList.contains('cur'), 'the requested line is marked');
  pv.destroy();
});

test('previewFile aborts the request it replaced: destroy during the load leaves nothing behind', async () => {
  const b = backend({}, FILES());
  const { w, host } = treeWorld(b, { state: STATE() });
  b.hold('api|src/app.js');
  w.ctx.__host = host;
  w.run('globalThis.__pv = Tree.previewFile(__host, { project: "shop", repo: "api", path: "src/app.js" })');
  await tick();
  w.get('__pv').destroy();
  await tick();
  assert.deepEqual(b.aborted, ['api|src/app.js']);
  assert.equal(host.querySelectorAll('li').length, 0);
});

// ---------------------------------------------------------------- Tree.children, urls, the code-server builder

test('Tree.url / fileUrl / children: flags, encoding, a two-second memory and ETag revalidation', async () => {
  const b = backend(levels());
  const { w } = treeWorld(b);
  assert.equal(w.run('Tree.url("shop", "api", "src/a b", {})'), '/api/projects/shop/repos/api/tree?path=src%2Fa+b&hidden=0&ignored=0&repos=1');
  assert.equal(w.run('Tree.url("shop", "api", "", { hidden: true, repos: false, refresh: true })'), '/api/projects/shop/repos/api/tree?path=&hidden=1&ignored=0&repos=0&refresh=1');
  assert.equal(w.run('Tree.fileUrl("shop", "api", "a/b.js", true)'), '/api/projects/shop/repos/api/file?path=a%2Fb.js&reveal=1');
  const first = plain(await w.run('Tree.children("shop", "api", "src", {})'));
  assert.equal(first.entries.length, 3);
  await w.run('Tree.children("shop", "api", "src", {})');
  assert.equal(b.requests.length, 1, 'the second call inside two seconds comes from memory');
  const again = plain(await w.run('Tree.children("shop", "api", "src", { fresh: true })'));
  assert.equal(b.requests.length, 2);
  assert.match(b.requests[1].inm, /^W\//, 'a fresh call revalidates');
  assert.deepEqual(again, first, 'a 304 answers the cached payload');
  await assert.rejects(() => w.run('Tree.children("shop", "api", "nope", {})'), /not found/);
});

test('codeServerFileUrl: folder and openFile payload, line and column optional, the authority in one constant', () => {
  const w = makeWorld();
  w.load('core.js');
  w.run('state = { config: { code_https_port: 10000 } }');
  const url = (code) => new URL(w.run(code));
  const a = url('codeServerFileUrl("/srv/p/api/src/a.js", 12, "/srv/p/api")');
  assert.equal(a.origin, 'https://box:10000');
  assert.equal(a.searchParams.get('folder'), '/srv/p/api');
  assert.deepEqual(JSON.parse(a.searchParams.get('payload')), [['openFile', 'vscode-remote:///srv/p/api/src/a.js:12']]);
  assert.deepEqual(JSON.parse(url('codeServerFileUrl("/srv/p/api/src/a.js", 12, "/srv/p/api", 5)').searchParams.get('payload')), [['openFile', 'vscode-remote:///srv/p/api/src/a.js:12:5']]);
  const bare = url('codeServerFileUrl("/srv/p/api/src/a.js")');
  assert.equal(bare.searchParams.get('folder'), '/srv/p/api/src', 'the folder defaults to the file\'s directory');
  assert.deepEqual(JSON.parse(bare.searchParams.get('payload')), [['openFile', 'vscode-remote:///srv/p/api/src/a.js']], 'no line, no suffix');
  w.run('CODE_SERVER_AUTHORITY = "box:10000"');
  assert.deepEqual(JSON.parse(url('codeServerFileUrl("/x/y.js", 2, "/x")').searchParams.get('payload')), [['openFile', 'vscode-remote://box:10000/x/y.js:2']]);
});

// ---------------------------------------------------------------- the demo fixtures through demoApi

test('demoApi answers the tree and file endpoints from the fixtures by repo and path; errors carry their status; reveal=1 unlocks', async () => {
  const fs = await import('node:fs');
  const path = await import('node:path');
  const { STATIC } = await import('./harness.mjs');
  const read = (n) => fs.readFileSync(path.join(STATIC, 'demo', n), 'utf8');
  const w = makeWorld({ location: { hostname: 'box', host: 'box', protocol: 'https:', pathname: '/', search: '?demo=1', hash: '' },
    fetch: async (url) => { const n = String(url).split('/').pop(); return { ok: true, status: 200, json: async () => JSON.parse(read(n)) }; } });
  w.load('core.js');
  const api = (p) => w.run(`api('GET', ${JSON.stringify(p)})`);
  const top = plain(await api('/api/projects/phasezero/repos/website/tree?path=&hidden=0&ignored=0&repos=1'));
  assert.equal(top.repo, 'website');
  assert.ok(top.entries.some((e) => e.name === 'src' && e.type === 'dir'));
  const src = plain(await api('/api/projects/phasezero/repos/website/tree?path=src'));
  assert.equal(src.path, 'src');
  const root = plain(await api('/api/projects/phasezero/repos/root/tree?path='));
  assert.ok(root.entries.some((e) => e.type === 'repo'), 'the project folder lists its repos');
  await assert.rejects(() => api('/api/projects/phasezero/repos/website/tree?path=nope'), (e) => e.status === 404);
  const readme = plain(await api('/api/projects/phasezero/repos/website/file?path=README.md'));
  assert.equal(readme.path, 'README.md');
  assert.ok(readme.text.length > 0);
  await assert.rejects(() => api('/api/projects/phasezero/repos/website/file?path=public%2Ffavicon.ico'), (e) => e.status === 415 && /binary/.test(e.message));
  await assert.rejects(() => api('/api/projects/phasezero/repos/website/file?path=config%2Fcredentials.example.json'), (e) => e.status === 403 && /reveal/.test(e.message));
  const revealed = plain(await api('/api/projects/phasezero/repos/website/file?path=config%2Fcredentials.example.json&reveal=1'));
  assert.equal(revealed.path, 'config/credentials.example.json');
  await assert.rejects(() => api('/api/projects/phasezero/repos/website/file?path=missing.txt'), (e) => e.status === 404);
});

// ---------------------------------------------------------------- the untracked-cache hint (issue #112)

test('onHint receives the hint from whichever level carried it (not only the top), and a level without one calls nothing', async () => {
  const hint = { kind: 'untracked_cache', cmd: "git -C '/srv/p/api' config core.untrackedCache true" };
  const lv = levels();
  lv['api|src'] = { ...SRC(), hint };
  const b = backend(lv);
  const { w, host } = treeWorld(b);
  const got = [];
  mount(w, host, { onHint: (h, rec) => got.push([h.kind, h.cmd, rec.path]) });
  await tick();
  assert.deepEqual(got, [], 'the top level carried no hint');
  key(w, item(host, 'src'), 'ArrowRight');
  await tick(5);
  assert.deepEqual(got, [['untracked_cache', hint.cmd, 'src']]);
});

test('a throwing onHint never breaks the tree', async () => {
  const lv = levels();
  lv['api|'] = { ...TOP(), hint: { kind: 'untracked_cache', cmd: 'x' } };
  const { w, host } = treeWorld(backend(lv));
  const quiet = console.error; console.error = () => {};
  try {
    mount(w, host, { onHint: () => { throw new Error('boom'); } });
    await tick();
  } finally { console.error = quiet; }
  assert.equal(items(host).length, 6, 'the rows are drawn');
});
