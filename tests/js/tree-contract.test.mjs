// Contract tests for app/static/tree.js (v0.5.6) against the DEMO FIXTURES: Tree.mount, Tree.previewFile and the WAI-ARIA tree pattern, in the vm
// harness with minidom's DOM. (tests/js/tree.test.mjs, written beside tree.js, tests it against hand-made listings; this file runs the same code over
// app/static/demo/tree.json and file.json, so the fixtures the demo mode serves and the UI that draws them are exercised together.)
// The network is a fake of GET /api/projects/<p>/repos/<r>/tree and /file answered from those fixtures (tests/js/treekit.mjs), timers and Date.now run
// on a manual clock and AbortController is a recording stub, so "one request per expand", "abort on collapse" and "revalidate every 10 s" are asserted
// exactly. Every request is logged whether tree.js sent it through api() or fetch().
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld } from './harness.mjs';
import { installDom } from './minidom.mjs';
import {
  FILE_FIXTURE, TREE_FIXTURE, TREE_JS, byPath, clone, dump, focused, isDirty, key, labelOf, makeNetworkWorld, pathOf, pathsState, secondary, settle, treeItems, visibleItems,
} from './treekit.mjs';

const KEY = 'ccboard:tree:phasezero/website';
const names = (host) => visibleItems(host).map(labelOf);
const level = (k) => TREE_FIXTURE[k].entries.map((e) => e.name);

function focusItem(w, item) {
  item.focus();
  item.dispatchEvent({ type: 'focus' });
  item.dispatchEvent({ type: 'focusin' });
  return item;
}

/** A mounted tree on a fresh world. opts override the Tree.mount options; `storage` pre-fills localStorage[KEY] with a JSON value; `held` holds tree requests. */
async function mountTree(opts = {}, { storage, held = [], world = {} } = {}) {
  const env = makeNetworkWorld({ state: pathsState(), ...world });
  const { w, server } = env;
  for (const h of held) server.held.add(h);
  if (storage !== undefined) w.localStorage.setItem(opts.storageKey || KEY, typeof storage === 'string' ? storage : JSON.stringify(storage));
  w.load(TREE_JS);
  const host = w.document.createElement('div');
  w.document.body.append(host);
  const opened = [];
  const selected = [];
  const handle = w.get('Tree').mount(host, {
    project: 'phasezero', repo: 'website', path: '', hidden: false, ignored: false, storageKey: KEY,
    onOpen: (e, p) => opened.push([e.name, p]), onSelect: (e, p) => selected.push([e.name, p]), ...opts,
  });
  await settle();
  return { ...env, host, handle, opened, selected, first: () => focusItem(w, visibleItems(host)[0]) };
}

const asSet = (raw) => {
  const v = JSON.parse(raw);
  return new Set(Array.isArray(v) ? v : Object.keys(v).filter((k) => v[k]));
};

// ---------------------------------------------------------------- definitions only

const trap = (what) => new Proxy(function () {}, {
  get(_t, prop) { if (prop === Symbol.toPrimitive || prop === 'then') return undefined; throw new Error(`load-time access to ${what}.${String(prop)}`); },
  apply() { throw new Error(`load-time call of ${what}()`); },
  construct() { throw new Error(`load-time construction of ${what}`); },
});

test('tree.js defines only: no DOM, storage, network, listener or timer access at load', () => {
  const boom = (what) => () => { throw new Error(`load-time call of ${what}()`); };
  const w = makeWorld({
    document: trap('document'), localStorage: trap('localStorage'), sessionStorage: trap('sessionStorage'), navigator: trap('navigator'),
    location: trap('location'), history: trap('history'), matchMedia: boom('matchMedia'),
    addEventListener: boom('window.addEventListener'), setTimeout: boom('setTimeout'), setInterval: boom('setInterval'),
    requestAnimationFrame: boom('requestAnimationFrame'), fetch: boom('fetch'), AbortController: trap('AbortController'),
    EventSource: function EventSource() { throw new Error('load-time construction of EventSource'); },
  });
  w.load('core.js');
  w.load(TREE_JS);
  assert.equal(w.run("typeof Tree === 'object' && typeof Tree.mount === 'function' && typeof Tree.previewFile === 'function'"), true);
});

// ---------------------------------------------------------------- markup and ARIA

test('mount draws one role=tree with a treeitem per entry, dirs first, and asks for the root level once', async () => {
  const { host, server } = await mountTree();
  assert.equal(host.querySelectorAll('[role=tree]').length, 1, 'role=tree is set once');
  assert.deepEqual(names(host), level('website|'), 'the fixture order: directories first, then files, both case-insensitive');
  assert.deepEqual(server.treePaths(), ['website|'], 'one request, for the top level');
  const r = server.tree()[0];
  assert.equal(r.project, 'phasezero');
  assert.ok(r.hidden === null || r.hidden === '0', 'hidden is off by default');
  assert.ok(r.ignored === null || r.ignored === '0', 'ignored is off by default');
  for (const item of treeItems(host)) assert.equal(item.getAttribute('aria-level'), '1', labelOf(item));
});

test('aria-expanded is on directories only (collapsed = false), never on files', async () => {
  const { host } = await mountTree();
  const dirs = TREE_FIXTURE['website|'].entries.filter((e) => e.type === 'dir').map((e) => e.name);
  for (const item of treeItems(host)) {
    const name = labelOf(item);
    if (dirs.includes(name)) assert.equal(item.getAttribute('aria-expanded'), 'false', `${name} is a collapsed directory`);
    else assert.equal(item.getAttribute('aria-expanded'), null, `${name} is a file: no aria-expanded at all`);
  }
});

test('aria-level grows with depth, aria-expanded flips on expand, and files in a loaded level never get aria-expanded', async () => {
  const { host, first, w } = await mountTree();
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight');
  await settle();
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'true');
  assert.equal(byPath(host, 'src/middleware.ts').getAttribute('aria-level'), '2');
  assert.equal(byPath(host, 'src/middleware.ts').getAttribute('aria-expanded'), null);
  assert.equal(byPath(host, 'src/app').getAttribute('aria-expanded'), 'false');
  assert.equal(byPath(host, 'src/app').getAttribute('aria-level'), '2');
});

test('roving tabindex: exactly one node is tabbable and it follows the focus', async () => {
  const { host, first, w } = await mountTree();
  const tabbable = () => treeItems(host).filter((i) => i.getAttribute('tabindex') === '0');
  assert.equal(tabbable().length, 1, 'one tab stop in the tree');
  assert.equal(treeItems(host).filter((i) => i.getAttribute('tabindex') === '-1').length, treeItems(host).length - 1);
  first();
  key(w, 'ArrowDown');
  key(w, 'ArrowDown');
  assert.equal(labelOf(focused(w)), 'src');
  assert.deepEqual(tabbable().map(labelOf), ['src'], dump(host));
  assert.equal(focused(w), tabbable()[0], 'the tabbable node is the focused one');
});

test('the tabbable node is not a node that has been collapsed away', async () => {
  const { host, first, w } = await mountTree();
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  key(w, 'ArrowRight');                                              // into src/app
  assert.equal(pathOf(focused(w)), 'src/app');
  key(w, 'ArrowLeft'); key(w, 'ArrowLeft');                          // back to src, then collapse it
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'false');
  const tabbable = treeItems(host).filter((i) => i.getAttribute('tabindex') === '0');
  assert.equal(tabbable.length, 1);
  assert.ok(visibleItems(host).includes(tabbable[0]), 'a collapsed-away node must not keep the tab stop');
});

test('a dirty directory carries the dirty class, a clean one does not; files show their status letter', async () => {
  const { host, first, w } = await mountTree();
  first();
  for (const [name, dirty] of [['config', false], ['public', false], ['src', true], ['tests', true]]) assert.equal(isDirty(byPath(host, name)), dirty, name);
  for (const [name, letter] of [['next.config.js', 'M'], ['notes.txt', '?'], ['package.json', ''], ['README.md', '']]) assert.equal(secondary(byPath(host, name)), letter, name);
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  assert.equal(isDirty(byPath(host, 'src/app')), true, 'dirty propagates down to the directory that holds the change');
  assert.equal(isDirty(byPath(host, 'src/lib')), false);
  assert.equal(secondary(byPath(host, 'src/middleware.ts')), 'M');
  focusItem(w, byPath(host, 'src/components'));
  key(w, 'ArrowRight'); await settle();
  for (const [name, letter] of [['ProductCard.tsx', 'A'], ['CartDrawer.tsx', '?'], ['Header.tsx', 'M'], ['Footer.tsx', '']]) assert.equal(secondary(byPath(host, `src/components/${name}`)), letter, name);
  focusItem(w, byPath(host, 'tests'));
  key(w, 'ArrowRight'); await settle();
  assert.equal(secondary(byPath(host, 'tests/legacy-cart.test.ts')), 'D');
});

test('a truncated level says how many of the total it shows', async () => {
  const { host, w } = await mountTree();
  focusItem(w, byPath(host, 'public'));
  key(w, 'ArrowRight'); await settle();
  focusItem(w, byPath(host, 'public/icons'));
  key(w, 'ArrowRight'); await settle();
  assert.ok(byPath(host, 'public/icons/icon-000.svg'), 'the entries that came are drawn');
  assert.match(host.textContent, /first\s+\d+\s+of\s+1840/i, 'a "first N of 1840" row');
  assert.equal(treeItems(host).filter((i) => pathOf(i).startsWith('public/icons/')).length, 60);
});

// ---------------------------------------------------------------- keyboard

test('Down / Up / Home / End move through the visible nodes and stop at the ends', async () => {
  const { host, first, w } = await mountTree();
  first();
  const all = level('website|');
  assert.equal(key(w, 'ArrowDown'), true, 'a handled key is prevented (no page scroll)');
  assert.equal(labelOf(focused(w)), all[1]);
  key(w, 'End');
  assert.equal(labelOf(focused(w)), all[all.length - 1]);
  key(w, 'ArrowDown');
  assert.equal(labelOf(focused(w)), all[all.length - 1], 'Down at the last node stays');
  key(w, 'ArrowUp');
  assert.equal(labelOf(focused(w)), all[all.length - 2]);
  key(w, 'Home');
  assert.equal(labelOf(focused(w)), all[0]);
  key(w, 'ArrowUp');
  assert.equal(labelOf(focused(w)), all[0], 'Up at the first node stays');
  assert.equal(key(w, 'Tab'), false, 'Tab is never swallowed: it leaves the tree');
  void host;
});

test('Down walks into an expanded directory and out of it again', async () => {
  const { host, first, w } = await mountTree();
  first();
  focusItem(w, byPath(host, 'public'));
  key(w, 'ArrowRight'); await settle();
  key(w, 'ArrowDown');
  assert.equal(pathOf(focused(w)), 'public/icons', 'the first child follows its parent');
  key(w, 'ArrowDown');
  assert.equal(pathOf(focused(w)), 'public/favicon.ico');
  key(w, 'ArrowDown');
  assert.equal(pathOf(focused(w)), 'public/robots.txt');
  key(w, 'ArrowDown');
  assert.equal(pathOf(focused(w)), 'src', 'the next sibling of the directory');
  key(w, 'ArrowUp');
  assert.equal(pathOf(focused(w)), 'public/robots.txt', 'and back up into the open level');
});

test('Right expands a closed directory, moves into an open one, and does nothing on a file', async () => {
  const { host, first, w, server } = await mountTree();
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'true');
  assert.equal(pathOf(focused(w)), 'src', 'expanding keeps the focus on the directory');
  key(w, 'ArrowRight');
  assert.equal(pathOf(focused(w)), 'src/app', 'on an open directory Right moves to its first child');
  focusItem(w, byPath(host, 'package.json'));
  const before = server.log.length;
  key(w, 'ArrowRight');
  assert.equal(pathOf(focused(w)), 'package.json');
  assert.equal(server.log.length, before, 'a file has nothing to load');
});

test('Left collapses an open directory, else moves to the parent, and does nothing at the top level', async () => {
  const { host, first, w } = await mountTree();
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  focusItem(w, byPath(host, 'src/lib'));
  key(w, 'ArrowLeft');
  assert.equal(pathOf(focused(w)), 'src', 'a closed directory: Left goes to the parent');
  focusItem(w, byPath(host, 'src/middleware.ts'));
  key(w, 'ArrowLeft');
  assert.equal(pathOf(focused(w)), 'src', 'a file: Left goes to the parent');
  key(w, 'ArrowLeft');
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'false', 'an open directory: Left collapses it');
  assert.equal(pathOf(focused(w)), 'src');
  key(w, 'ArrowLeft');
  assert.equal(pathOf(focused(w)), 'src', 'nothing above the top level');
  assert.deepEqual(names(host), level('website|'), 'the children are out of the tab order');
});

test('Enter on a file opens it (onOpen gets the entry and its repo-relative path); on a directory it toggles', async () => {
  const { host, first, w, opened } = await mountTree();
  first();
  focusItem(w, byPath(host, 'package.json'));
  key(w, 'Enter');
  assert.deepEqual(opened, [['package.json', 'package.json']]);
  focusItem(w, byPath(host, 'src'));
  key(w, 'Enter'); await settle();
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'true', 'Enter on a directory expands it');
  assert.equal(opened.length, 1, 'and does not open anything');
  key(w, 'Enter');
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'false', 'a second Enter collapses it');
  focusItem(w, byPath(host, 'src'));
  key(w, 'Enter'); await settle();
  focusItem(w, byPath(host, 'src/lib'));
  key(w, 'Enter'); await settle();
  focusItem(w, byPath(host, 'src/lib/api.ts'));
  key(w, 'Enter');
  assert.deepEqual(opened[1], ['api.ts', 'src/lib/api.ts'], 'a nested file is reported with its path from the repo root');
});

test('asterisk expands every closed sibling directory at the level and leaves files alone', async () => {
  const { host, first, w, server } = await mountTree();
  first();
  server.clear();
  key(w, '*'); await settle();
  for (const d of ['config', 'public', 'src', 'tests']) assert.equal(byPath(host, d).getAttribute('aria-expanded'), 'true', d);
  assert.deepEqual([...server.treePaths()].sort(), ['website|config', 'website|public', 'website|src', 'website|tests'], 'one request per directory');
  assert.equal(byPath(host, 'package.json').getAttribute('aria-expanded'), null);
  assert.ok(byPath(host, 'src/lib') && byPath(host, 'tests/cart.test.ts') && byPath(host, 'config/site.json'));
});

test('type-ahead moves to the next node whose label starts with the typed letters (case-insensitive), the buffer clears after a pause', async () => {
  const { host, first, w, clock } = await mountTree();
  first();                                                           // config
  key(w, 'n'); await settle();
  assert.equal(labelOf(focused(w)), 'next.config.js');
  await clock.advance(2000);
  key(w, 'p');
  assert.equal(labelOf(focused(w)), 'package.json', 'a new word starts a new buffer');
  await clock.advance(2000);
  key(w, 'T');
  assert.equal(labelOf(focused(w)), 'tsconfig.json', 'case-insensitive; the search runs forward from package.json');
  await clock.advance(2000);
  key(w, 'c'); key(w, 'o');
  assert.equal(labelOf(focused(w)), 'config', 'two quick letters search for the prefix "co" and wrap around');
  await clock.advance(2000);
  key(w, 'r');
  assert.equal(labelOf(focused(w)), 'README.md');
  void host;
});

test('type-ahead does not trigger on shortcuts (Ctrl/Meta combinations are left alone)', async () => {
  const { first, w } = await mountTree();
  first();
  assert.equal(key(w, 'p', { ctrlKey: true }), false);
  assert.equal(key(w, 'p', { metaKey: true }), false);
  assert.equal(labelOf(focused(w)), 'config');
});

// ---------------------------------------------------------------- lazy loading, abort, errors

test('a directory loads on its first expand with exactly one request and one AbortController', async () => {
  const { host, first, w, server, aborts } = await mountTree();
  first();
  const requests = server.tree().length;
  const controllers = aborts.length;
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  assert.equal(server.tree().length, requests + 1, 'one call per expand');
  assert.equal(aborts.length, controllers + 1, 'one AbortController per expand');
  const r = server.tree().at(-1);
  assert.deepEqual([r.repo, r.path], ['website', 'src']);
  assert.equal(aborts.at(-1).signal.aborted, false, 'a finished expand is not aborted');
  focusItem(w, byPath(host, 'src/components'));
  key(w, 'ArrowRight'); await settle();
  assert.equal(server.tree().at(-1).path, 'src/components', 'the path is the full path from the repo root');
  assert.equal(new URL(server.tree().at(-1).url, 'https://box').searchParams.get('path'), 'src/components', 'sent as one query parameter');
  assert.ok(byPath(host, 'src/components/Header.tsx'));
});

test('hidden and ignored options travel as query flags', async () => {
  const { server } = await mountTree({ hidden: true, ignored: true });
  const r = server.tree()[0];
  assert.equal(r.hidden, '1');
  assert.equal(r.ignored, '1');
});

test('collapsing a directory while its request is in flight aborts that request and discards a late answer', async () => {
  const { host, first, w, server, aborts } = await mountTree({}, { held: ['website|src'] });
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  const ctl = aborts.at(-1);
  assert.equal(server.pending.length, 1, 'the request is waiting');
  assert.equal(ctl.signal.aborted, false);
  key(w, 'ArrowLeft'); await settle();                               // collapse while it loads
  assert.equal(ctl.signal.aborted, true, 'the cancelled expand aborted its request');
  server.release('website|src');                                     // a late answer, whether or not the signal reached the transport
  await settle();
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'false');
  assert.equal(byPath(host, 'src/app'), null, 'the stale children are not drawn');
  assert.equal(host.textContent.includes('aborted'), false, 'an abort is not an error to show');
  // expanding again is a fresh expand with a fresh controller
  server.held.delete('website|src');
  const before = aborts.length;
  key(w, 'ArrowRight'); await settle();
  assert.equal(aborts.length, before + 1);
  assert.equal(aborts.at(-1).signal.aborted, false);
  assert.ok(byPath(host, 'src/app'), 'and this time the children arrive');
});

test('an answer that arrives after the collapse anyway (a transport that ignores the abort) is discarded', async () => {
  const { host, first, w, server } = await mountTree({}, { held: ['website|src'] });
  server.ignoreAbort = true;
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  key(w, 'ArrowLeft'); await settle();
  server.release('website|src');
  await settle();
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'false');
  assert.equal(byPath(host, 'src/app'), null, 'nothing from the stale answer is drawn');
  assert.deepEqual(names(host), level('website|'));
});

test('a failed load shows the error and leaves the tree usable', async () => {
  const { host, first, w, server } = await mountTree();
  first();
  server.fail.set('website|src', { status: 500, error: 'git exploded' });
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  assert.match(host.textContent, /git exploded/, 'the message is shown');
  assert.equal(key(w, 'ArrowDown'), true, 'the keyboard still works');
  server.fail.delete('website|src');
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowLeft'); await settle();
  key(w, 'ArrowRight'); await settle();
  assert.ok(byPath(host, 'src/app'), 'a later expand retries');
  assert.equal(host.textContent.includes('git exploded'), false);
});

// ---------------------------------------------------------------- persistence

test('the expanded set is saved under storageKey and a collapse removes the entry', async () => {
  const { host, first, w } = await mountTree();
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  focusItem(w, byPath(host, 'src/lib'));
  key(w, 'ArrowRight'); await settle();
  assert.deepEqual([...asSet(w.localStorage.getItem(KEY))].sort(), ['src', 'src/lib']);
  focusItem(w, byPath(host, 'src/lib'));
  key(w, 'ArrowLeft');
  assert.deepEqual([...asSet(w.localStorage.getItem(KEY))].sort(), ['src']);
  assert.equal(w.localStorage.getItem('ccboard:tree:phasezero/other'), null, 'only its own key');
});

test('a remount restores the expanded directories, loading parents before children', async () => {
  const { host, server, w } = await mountTree({}, { storage: ['src', 'src/lib'] });
  assert.deepEqual(server.treePaths(), ['website|', 'website|src', 'website|src/lib']);
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'true');
  assert.equal(byPath(host, 'src/lib').getAttribute('aria-expanded'), 'true');
  assert.ok(byPath(host, 'src/lib/api.ts'));
  assert.equal(byPath(host, 'tests').getAttribute('aria-expanded'), 'false');
  assert.equal(treeItems(host).filter((i) => i.getAttribute('tabindex') === '0').length, 1);
  void w;
});

test('stored state that is garbage, or names directories that are gone, is ignored', async () => {
  const bad = await mountTree({}, { storage: '{not json' });
  assert.deepEqual(names(bad.host), level('website|'));
  const gone = await mountTree({}, { storage: ['no/such/dir', 'src'] });
  assert.equal(byPath(gone.host, 'src').getAttribute('aria-expanded'), 'true');
  assert.deepEqual(names(gone.host).slice(0, 4), ['config', 'public', 'src', 'app'], 'the real directory opens, the missing one is skipped quietly');
});

test('storage that throws does not stop the tree', async () => {
  const env = makeNetworkWorld({ state: pathsState() });
  env.w.load(TREE_JS);
  env.w.run("Object.defineProperty(globalThis, 'localStorage', { get() { throw new Error('storage blocked'); }, configurable: true });");
  const host = env.w.document.createElement('div');
  env.w.document.body.append(host);
  env.w.get('Tree').mount(host, { project: 'phasezero', repo: 'website', path: '', storageKey: KEY });
  await settle();
  assert.deepEqual(names(host), level('website|'));
  focusItem(env.w, byPath(host, 'src'));
  key(env.w, 'ArrowRight'); await settle();
  assert.ok(byPath(host, 'src/app'));
});

// ---------------------------------------------------------------- revalidation

test('open nodes revalidate every 10 s with If-None-Match; a 304 changes nothing', async () => {
  const { host, first, w, server, clock } = await mountTree();
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  const src = byPath(host, 'src');
  const lib = byPath(host, 'src/lib');
  const before = dump(host);
  server.clear();
  await clock.advance(9000);
  assert.equal(server.tree().length, 0, 'not before 10 s');
  await clock.advance(1500);
  const got = server.tree();
  assert.deepEqual([...new Set(got.map((r) => `${r.repo}|${r.path}`))].sort(), ['website|', 'website|src'], 'the root and the open directory, not the closed ones');
  for (const r of got) {
    assert.ok(r.ifNoneMatch, `${r.path || '(root)'} is a conditional request`);
    assert.equal(r.ifNoneMatch.replace(/^W\//, '').replace(/"/g, ''), TREE_FIXTURE[`website|${r.path}`].etag);
    assert.equal(r.status, 304);
  }
  assert.equal(dump(host), before, 'nothing changed on screen');
  assert.equal(byPath(host, 'src'), src, 'the same nodes');
  assert.equal(byPath(host, 'src/lib'), lib);
  assert.equal(pathOf(focused(w)), 'src', 'the focus stayed');
});

test('a changed level is redrawn on the next revalidation without losing the focus or the open directories', async () => {
  const { host, first, w, server, clock } = await mountTree();
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  focusItem(w, byPath(host, 'src/lib'));
  key(w, 'ArrowRight'); await settle();
  focusItem(w, byPath(host, 'src/lib/api.ts'));
  const changed = clone(TREE_FIXTURE['website|src']);
  changed.etag = 'feedfacefeedface';
  changed.entries.push({ name: 'zz-new.ts', type: 'file', status: '?', dirty: true, has_children: false, ignored: false, size: 12 });
  changed.total += 1;
  server.overrides.set('website|src', changed);
  await clock.advance(10500);
  assert.ok(byPath(host, 'src/zz-new.ts'), 'the new file appears');
  assert.equal(secondary(byPath(host, 'src/zz-new.ts')), '?');
  assert.equal(byPath(host, 'src/lib').getAttribute('aria-expanded'), 'true', 'an open child stays open');
  assert.ok(byPath(host, 'src/lib/api.ts'));
  assert.equal(pathOf(focused(w)), 'src/lib/api.ts', 'the focused node is still the focused node');
  assert.equal(treeItems(host).filter((i) => i.getAttribute('tabindex') === '0').length, 1);
});

test('revalidation pauses while the page is hidden and resumes when it is visible again', async () => {
  const { w, server, clock } = await mountTree();
  w.document.hidden = true;
  w.document.visibilityState = 'hidden';
  w.document.dispatch('visibilitychange');
  server.clear();
  await clock.advance(35000);
  assert.equal(server.tree().length, 0, 'a hidden page asks for nothing');
  w.document.hidden = false;
  w.document.visibilityState = 'visible';
  w.document.dispatch('visibilitychange');
  await clock.advance(10500);
  assert.ok(server.tree().length >= 1, 'visible again: revalidating');
});

// ---------------------------------------------------------------- repos, programmatic API, mouse

test('the project folder lists plain directories and nested repos; a repo opens its own tree in place', async () => {
  const { host, first, w, server } = await mountTree({ repo: 'root', storageKey: 'ccboard:tree:phasezero/root' });
  first();
  assert.deepEqual(names(host), level('root|'));
  assert.equal(byPath(host, 'NestJs-Ecommerce-Backend').getAttribute('aria-expanded'), 'false', 'a repo is expandable');
  assert.equal(byPath(host, 'README.md').getAttribute('aria-expanded'), null);
  focusItem(w, byPath(host, 'NestJs-Ecommerce-Backend'));
  server.clear();
  key(w, 'ArrowRight'); await settle();
  assert.deepEqual(server.treePaths(), ['NestJs-Ecommerce-Backend|'], 'the nested repo is read through its own repo name');
  assert.deepEqual(['prisma', 'src', 'test'], visibleItems(host).filter((i) => i.getAttribute('aria-level') === '2').map(labelOf).slice(0, 3));
  focusItem(w, byPath(host, 'NestJs-Ecommerce-Backend/src'));
  key(w, 'ArrowRight'); await settle();
  assert.deepEqual(server.treePaths().at(-1), 'NestJs-Ecommerce-Backend|src', 'directories below it are read inside that repo');
  focusItem(w, byPath(host, 'assets'));
  key(w, 'ArrowRight'); await settle();
  assert.deepEqual(server.treePaths().at(-1), 'root|assets', 'a plain directory of the project folder');
  assert.ok(byPath(host, 'assets/brand-sheet.pdf'));
});

test('handle.expand(path) opens the directory and its parents; select(path) marks one node selected', async () => {
  const { host, handle, server } = await mountTree();
  await handle.expand('src/components');
  await settle();
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'true');
  assert.equal(byPath(host, 'src/components').getAttribute('aria-expanded'), 'true');
  assert.ok(byPath(host, 'src/components/Header.tsx'));
  assert.deepEqual(server.treePaths(), ['website|', 'website|src', 'website|src/components'], 'parents first, one request each');
  handle.select('src/components/Header.tsx');
  await settle();
  const sel = treeItems(host).filter((i) => i.getAttribute('aria-selected') === 'true');
  assert.deepEqual(sel.map(pathOf), ['src/components/Header.tsx'], 'exactly one selected node');
  handle.select('package.json');
  assert.deepEqual(treeItems(host).filter((i) => i.getAttribute('aria-selected') === 'true').map(pathOf), ['package.json']);
});

test('handle.refresh() re-reads the root and every open directory', async () => {
  const { host, first, w, handle, server } = await mountTree();
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  server.clear();
  await handle.refresh();
  await settle();
  assert.deepEqual([...new Set(server.treePaths())].sort(), ['website|', 'website|src']);
});

test('clicking a directory toggles it; clicking a file selects it and reports onSelect', async () => {
  const { host, w, selected } = await mountTree();
  byPath(host, 'src').querySelector('.treelabel').click(); await settle();
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'true', 'click opens a directory');
  byPath(host, 'src').querySelector('.treelabel').click(); await settle();
  assert.equal(byPath(host, 'src').getAttribute('aria-expanded'), 'false', 'and closes it again');
  byPath(host, 'package.json').querySelector('.treelabel').click(); await settle();
  assert.equal(byPath(host, 'package.json').getAttribute('aria-selected'), 'true');
  assert.deepEqual(selected.at(-1), ['package.json', 'package.json']);
  void w;
});

test('destroy() stops the timers and the requests, and later keys do nothing', async () => {
  const { host, first, w, handle, server, clock } = await mountTree();
  first();
  focusItem(w, byPath(host, 'src'));
  key(w, 'ArrowRight'); await settle();
  const target = focused(w);
  handle.destroy();
  server.clear();
  await clock.advance(60000);
  assert.equal(server.tree().length, 0, 'no revalidation after destroy');
  assert.equal(clock.pending, 0, 'no timer left behind');
  assert.doesNotThrow(() => target.dispatchEvent({ type: 'keydown', key: 'ArrowDown', preventDefault() {}, stopPropagation() {} }));
  assert.equal(server.tree().length, 0);
});

// ---------------------------------------------------------------- file preview

const previewHost = (w) => { const h = w.document.createElement('div'); w.document.body.append(h); return h; };
const lineNodes = (host) => host.querySelectorAll('ol li');
const linesOf = (name) => FILE_FIXTURE[name].body.text.split('\n').filter((l, i, a) => i < a.length - 1 || l !== '');

test('previewFile: a line gutter with one li per line, the path in the header, text only', async () => {
  const { w, server } = await mountTree();
  const host = previewHost(w);
  await w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'website', path: 'package.json' });
  await settle();
  const req = server.log.filter((r) => r.kind === 'file').at(-1);
  assert.deepEqual([req.repo, req.path], ['website', 'package.json']);
  assert.ok(req.reveal === null || req.reveal === '0');
  const lines = linesOf('website|package.json');
  assert.equal(lineNodes(host).length, FILE_FIXTURE['website|package.json'].body.lines);
  for (const [i, line] of lines.slice(0, 5).entries()) assert.ok(lineNodes(host)[i].textContent.includes(line.trim() || ''), `line ${i + 1}`);
  assert.match(host.textContent, /package\.json/);
  assert.match(host.textContent, /phasezero-website/, 'the content is there');
});

test('previewFile builds its DOM from text only: markup in a file stays text', async () => {
  const { w, server } = await mountTree();
  server.files = { ...server.files, 'website|evil.html': { status: 200, body: { path: 'evil.html', size: 60, mtime: 1, truncated: false, lines: 2,
    text: '<img src=x onerror="alert(1)">\n<script>boom()</script>\n' } } };
  const host = previewHost(w);
  await w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'website', path: 'evil.html' });
  await settle();
  assert.equal(host.querySelectorAll('script').length + host.querySelectorAll('img').length, 0, 'no element was created from the file');
  assert.ok(host.textContent.includes('<script>boom()</script>'), 'the characters are shown');
  assert.equal(lineNodes(host).length, 2);
});

test('previewFile: a truncated file says so', async () => {
  const { w, server } = await mountTree();
  server.files = { ...server.files, 'website|big.log': { status: 200, body: { path: 'big.log', size: 900000, mtime: 1, truncated: true, lines: 3, text: 'a\nb\nc\n' } } };
  const host = previewHost(w);
  await w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'website', path: 'big.log' });
  await settle();
  assert.match(host.textContent, /truncat|first 200|200 ?KB/i);
  assert.equal(lineNodes(host).length, 3);
});

test('previewFile: a binary file (415) shows its message and no gutter', async () => {
  const { w } = await mountTree();
  const host = previewHost(w);
  await w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'website', path: 'public/favicon.ico' });
  await settle();
  assert.match(host.textContent, /binary file/i);
  assert.equal(lineNodes(host).length, 0);
});

test('previewFile: a secret-looking name (403) offers Reveal, and revealing re-reads the file with reveal=1', async () => {
  const { w, server } = await mountTree();
  const host = previewHost(w);
  await w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'website', path: 'config/credentials.example.json' });
  await settle();
  assert.match(host.textContent, /may hold secrets/);
  assert.equal(lineNodes(host).length, 0, 'nothing of the file is shown yet');
  const reveal = host.querySelectorAll('button').find((b) => /reveal/i.test(b.textContent));
  assert.ok(reveal, 'a reveal button');
  reveal.click();
  await settle();
  const last = server.log.filter((r) => r.kind === 'file').at(-1);
  assert.equal(last.reveal, '1');
  assert.equal(last.path, 'config/credentials.example.json');
  assert.match(host.textContent, /esewa/, 'the content is shown after the reveal');
  assert.ok(lineNodes(host).length > 0);
});

test('previewFile: a missing file shows the error', async () => {
  const { w } = await mountTree();
  const host = previewHost(w);
  await w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'website', path: 'tests/legacy-cart.test.ts' });
  await settle();
  assert.match(host.textContent, /not found/i);
  assert.equal(lineNodes(host).length, 0);
});

test('previewFile: open in code-server is a link built by codeServerFileUrl for the absolute path', async () => {
  const { w } = await mountTree();
  const host = previewHost(w);
  await w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'website', path: 'src/lib/api.ts' });
  await settle();
  const link = host.querySelectorAll('a').find((a) => /code-server/i.test(a.textContent + (a.getAttribute('title') || '') + (a.getAttribute('aria-label') || '')));
  assert.ok(link, 'an "open in code-server" link');
  const href = link.getAttribute('href');
  assert.equal(href, w.get('codeServerFileUrl')('/srv/projects/phasezero/website/src/lib/api.ts', 0, '/srv/projects/phasezero/website'), 'the link comes from the one builder');
  const u = new URL(href);
  assert.equal(u.origin, 'https://box:10000');
  assert.equal(u.searchParams.get('folder'), '/srv/projects/phasezero/website', 'the folder is the repo');
  assert.deepEqual(JSON.parse(u.searchParams.get('payload')), [['openFile', 'vscode-remote:///srv/projects/phasezero/website/src/lib/api.ts']], 'no line picked: the file alone');
  assert.equal(link.getAttribute('target'), '_blank');
  assert.match(link.getAttribute('rel') || '', /noopener/);
});

test('previewFile: a file of the project folder resolves against the project path', async () => {
  const { w } = await mountTree();
  const host = previewHost(w);
  await w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'root', path: 'docs/plan.md' });
  await settle();
  const link = host.querySelectorAll('a').find((a) => /code-server/i.test(a.textContent + (a.getAttribute('title') || '') + (a.getAttribute('aria-label') || '')));
  assert.ok(link);
  const u = new URL(link.getAttribute('href'));
  assert.equal(u.searchParams.get('folder'), '/srv/projects/phasezero');
  assert.equal(JSON.parse(u.searchParams.get('payload'))[0][1], 'vscode-remote:///srv/projects/phasezero/docs/plan.md');
});

test('previewFile: a line given up front, a click on a line and setLine() put the line number into the code-server link', async () => {
  const { w } = await mountTree();
  const host = previewHost(w);
  const link = () => host.querySelectorAll('a').find((a) => /code-server/i.test(a.textContent));
  const payload = () => JSON.parse(new URL(link().getAttribute('href')).searchParams.get('payload'))[0][1];
  const pv = w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'website', path: 'src/lib/api.ts', line: 3 });
  await settle();
  assert.equal(payload(), 'vscode-remote:///srv/projects/phasezero/website/src/lib/api.ts:3', 'the line of the request');
  lineNodes(host)[4].click();
  assert.equal(payload().endsWith('api.ts:5'), true, 'a click on a line moves it');
  lineNodes(host)[4].click();
  assert.equal(payload().endsWith('api.ts'), true, 'a second click on the same line clears it');
  pv.setLine(2);
  assert.equal(payload().endsWith('api.ts:2'), true);
});

test('previewFile: destroy() removes the preview and a late answer is not drawn', async () => {
  const { w } = await mountTree();
  const host = previewHost(w);
  const pv = w.get('Tree').previewFile(host, { project: 'phasezero', repo: 'website', path: 'package.json' });
  pv.destroy();
  await settle();
  assert.equal(host.querySelectorAll('ol').length, 0);
  assert.equal(host.children.length, 0, 'nothing left in the host');
});

// ---------------------------------------------------------------- demo mode: the same screens from the fixtures, no server

/** A world with ?demo=1 whose fetch serves /static/demo/*.json from disk (what the board does in demo mode); any other request is an error. */
function demoWorld() {
  const served = [];
  const hostFetch = async (url) => {
    served.push(String(url));
    const m = /^\/static\/demo\/([\w]+)\.json$/.exec(String(url));
    if (!m) throw new Error(`demo mode asked for ${url}`);
    const text = fs.readFileSync(path.join(STATIC, 'demo', `${m[1]}.json`), 'utf8');
    return { ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(text) };
  };
  const w = makeWorld({ fetch: hostFetch });
  installDom(w);
  w.location.search = '?demo=1';
  w.load('core.js'); w.load('components.js'); w.load(TREE_JS);
  w.ctx.__st = pathsState();
  w.run('state = __st');
  return { w, served };
}

test('demo mode: the tree reads tree.json by <repo>|<path>, never the network, and starts no revalidation timer', async () => {
  const { w, served } = demoWorld();
  const host = w.document.createElement('div');
  w.document.body.append(host);
  const timers = [];
  w.ctx.__setInterval = (...a) => timers.push(a);
  w.run('globalThis.setInterval = (...a) => { __setInterval(...a); return 1; }');
  const handle = w.get('Tree').mount(host, { project: 'phasezero', repo: 'website', path: '', storageKey: KEY });
  await settle();
  assert.deepEqual(names(host), level('website|'));
  const hit = await handle.expand('src/components');
  assert.equal(hit.entry.name, 'components');
  await settle();                                                    // the last directory's own listing is still on its way when expand() resolves
  assert.ok(byPath(host, 'src/components/Header.tsx'));
  assert.deepEqual([...new Set(served)], ['/static/demo/tree.json'], 'only the fixture was fetched');
  assert.equal(timers.length, 0, 'no polling of a fixture');
  handle.destroy();
});

test('demo mode: the project folder and a nested repo, a path the fixture lacks answers 404 as an error row', async () => {
  const { w } = demoWorld();
  const host = w.document.createElement('div');
  w.document.body.append(host);
  const handle = w.get('Tree').mount(host, { project: 'phasezero', repo: 'root', path: '', storageKey: 'ccboard:tree:phasezero/root' });
  await settle();
  assert.deepEqual(names(host), level('root|'));
  const hit = await handle.expand('NestJs-Ecommerce-Backend/src/modules/stock');
  assert.equal(hit.repo, 'NestJs-Ecommerce-Backend');
  assert.equal(hit.rel, 'src/modules/stock');
  await settle();
  assert.ok(byPath(host, 'NestJs-Ecommerce-Backend/src/modules/stock/stock.service.ts'));
  const miss = w.document.createElement('div');
  w.document.body.append(miss);
  w.get('Tree').mount(miss, { project: 'phasezero', repo: 'no-such-repo', path: '', storageKey: 'x' });
  await settle();
  assert.match(miss.textContent, /not found/);
});

test('demo mode: the file previews of file.json, with 403 and reveal, 415 and 404', async () => {
  const { w } = demoWorld();
  const lines = (host) => host.querySelectorAll('ol li').length;
  const show = async (repo, p) => {
    const host = w.document.createElement('div');
    w.document.body.append(host);
    w.get('Tree').previewFile(host, { project: 'phasezero', repo, path: p });
    await settle();
    return host;
  };
  const ok = await show('website', 'src/components/Header.tsx');
  assert.equal(lines(ok), FILE_FIXTURE['website|src/components/Header.tsx'].body.lines);
  assert.match(ok.textContent, /CartButton/);
  const secret = await show('website', 'config/credentials.example.json');
  assert.match(secret.textContent, /may hold secrets/);
  assert.equal(lines(secret), 0);
  secret.querySelectorAll('button').find((b) => /reveal/i.test(b.textContent)).click();
  await settle();
  assert.match(secret.textContent, /esewa/);
  assert.match((await show('website', 'public/favicon.ico')).textContent, /binary/i);
  assert.match((await show('root', 'assets/brand-sheet.pdf')).textContent, /binary/i);
  assert.match((await show('website', 'tests/legacy-cart.test.ts')).textContent, /not found/i);
  assert.equal(lines(await show('root', 'docs/plan.md')), 5);
});

test('demo mode: Tree.children (the sidebar\'s reader) answers from the same fixture', async () => {
  const { w } = demoWorld();
  const data = await w.run("Tree.children('phasezero', 'website', 'src', { repos: true })");
  assert.deepEqual(data.entries.map((e) => e.name), level('website|src'));
});

// ---------------------------------------------------------------- core.js: the one deep-link builder

test('codeServerFileUrl(abs, line) builds the folder + openFile payload link in one place', () => {
  const w = makeWorld();
  installDom(w);
  w.load('core.js');
  w.ctx.__st = pathsState();
  w.run('state = __st');
  const url = w.get('codeServerFileUrl')('/srv/projects/phasezero/website/src/lib/api.ts', 42);
  const u = new URL(url);
  assert.equal(u.protocol, 'https:');
  assert.equal(u.host, 'box:10000');
  assert.ok('/srv/projects/phasezero/website/src/lib/api.ts'.startsWith(u.searchParams.get('folder')), 'folder is a parent of the file');
  assert.deepEqual(JSON.parse(u.searchParams.get('payload')), [['openFile', 'vscode-remote:///srv/projects/phasezero/website/src/lib/api.ts:42']]);
  assert.match(url, /payload=%5B%5B/, 'the payload is percent-encoded JSON');
  assert.deepEqual(JSON.parse(new URL(w.get('codeServerFileUrl')('/srv/projects/phasezero/website/src/lib/api.ts')).searchParams.get('payload')),
    [['openFile', 'vscode-remote:///srv/projects/phasezero/website/src/lib/api.ts']], 'no line: the bare file');
  assert.equal(new URL(w.get('codeServerFileUrl')('/srv/a/b.ts', 7, '/srv/a')).searchParams.get('folder'), '/srv/a', 'an explicit folder wins');
  assert.match(w.get('codeServerFileUrl')('/srv/a/b.ts', 7, '/srv/a', 3), /b\.ts%3A7%3A3/, 'and a column follows the line');
  assert.equal(w.get('codeServerUrl')('/srv/projects/phasezero'), 'https://box:10000/?folder=%2Fsrv%2Fprojects%2Fphasezero', 'the folder link is unchanged');
});
