// Contract tests for the per-route lazy loading of issue #103 (app/static/lazy.js, router.js): index.html loads only the shell set; the first visit to a lazy route loads its
// script (and sheet) once, in order, before the page mounts; a second visit loads nothing; a failed load shows an error state with a Retry instead of a blank page; the launcher,
// palette and dnd load on first use. The "network" is the world's document.head: a <script src> appended to it runs the real file in the vm (or fails), a <link> loads.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { fixtureState } from './world.mjs';

const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
const INDEX_SCRIPTS = [...html.matchAll(/<script (?:defer )?src="([^"]+)"/g)].map((m) => m[1]);

const settle = async (until, max = 200) => {
  for (let i = 0; i < max; i++) {
    if (until && until()) return true;
    await new Promise((r) => setImmediate(r));
  }
  return until ? !!until() : true;
};

/** The first-paint set of index.html (shell.js included, main.js left out) in a world whose head fetches lazy files from app/static. */
function lazyWorld({ fine = true, fail = [], state = fixtureState(), hash = null, slow = 0, realCreate = false, cold = false } = {}) {
  const w = makeWorld({ companions: false, setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); t.unref(); return t; }, setInterval: (fn, ms) => { const t = setInterval(fn, ms); t.unref(); return t; }, matchMedia: (q) => ({ matches: fine && /pointer:\s*fine/.test(q), addEventListener() {}, removeEventListener() {} }) });
  const dom = installDom(w);
  const doc = w.document;
  const head = new dom.El('head');
  doc.documentElement.insertBefore(head, doc.body);
  doc.head = head;
  const charts = new dom.El('link');
  charts.setAttribute('rel', 'stylesheet');
  charts.setAttribute('href', '/static/charts.css');
  // Styles are ranked by the loader now; there is no eager charts anchor.
  const find = doc.querySelector;
  doc.querySelector = (s) => find(s) || head.querySelector(s);
  const net = { scripts: [], sheets: [], preloads: [], fail: new Set(fail) };
  const onInsert = (n) => {
    if (n.tagName === 'SCRIPT') {
      const src = n.src.split('?')[0];
      net.scripts.push(src);
      (slow ? (f) => setTimeout(f, slow) : setImmediate)(() => {
        if (net.fail.has(src)) { n.onerror(); return; }
        try { w.load(src.slice('/static/'.length).split('?')[0]); } catch (e) { (net.errors ||= []).push(e); }
        n.onload();
      });
    } else if (n.tagName === 'LINK' && n.getAttribute('rel') === 'preload') {
      net.preloads.push(n.getAttribute('href'));
      setImmediate(() => n.onload()); // Downloads bytes; never executes the script.
    } else if (n.tagName === 'LINK' && n.getAttribute('data-lazy')) {
      const href = n.getAttribute('href');
      net.sheets.push(href);
      setImmediate(() => { if (net.fail.has(href)) n.onerror(); else n.onload(); });
    }
  };
  const append = head.appendChild.bind(head);
  const insert = head.insertBefore.bind(head);
  head.appendChild = (n) => { const r = append(n); onInsert(n); return r; };
  head.insertBefore = (n, ref) => { const r = insert(n, ref); onInsert(n); return r; };
  w.load('lazy.js');
  for (const src of plain(w.get('LAZY_BUNDLES.app.js'))) try { w.load(src.slice('/static/'.length).split('?')[0]); } catch (e) { (net.errors ||= []).push(e); }
  if (!cold) {
    // Historical component fixtures: explicit dependencies, without page-specific bundles.
    // The cold-route tests below exercise the actual first-view graph without these helpers.
    for (const file of ['settings-restart.js', 'tabs.js', 'pages/agents.js', 'page-selection.js', 'page-keys.js', 'session-groups.js', 'inbox-cards.js', 'pages/inbox.js', 'pages/widgets.js', 'pages/home.js', 'components-tasks.js', 'task-views.js', 'pages/tasks.js', 'pages/search.js', 'pages/session.js', 'keymap.js', 'shell-dock.js']) w.load(file);
  }
  w.ctx.__calls = [];
  w.run(`api = async (method, path, body) => { __calls.push({ method, path, body }); return { ok: true }; }; poll = async () => {};`);
  w.ctx.__st = state;
  w.run('state = __st');
  if (!realCreate) w.run('Shell.openCreate = () => true');
  if (hash !== null) w.setHash(hash, { silent: true });
  const mounted = () => w.get('mountedId');
  const hrefs = () => head.children.filter((n) => n.tagName === 'LINK').map((n) => n.getAttribute('href'));
  const go = async (h) => { w.location.hash = h; };
  return { w, dom, net, head, mounted, hrefs, go };
}

const LAZY_GLOBALS = ['agentsPage', 'Tree', 'Dnd', 'Palette', 'TermKit', 'Charts', 'Memory', 'Usage', 'Quad', 'projectPage', 'settingsPage', 'doctorStore', 'wizPage', 'launcherSchema', 'epicPage', 'Epics'];

test('index.html loads only the shell set: no lazy script is in it, and with just those files nothing lazy is defined', () => {
  const lazy = JSON.parse(JSON.stringify([]));
  const src = fs.readFileSync(path.join(STATIC, 'lazy.js'), 'utf8');
  for (const file of src.matchAll(/'(\/static\/[^']+\.js)'/g)) {
    assert.ok(!INDEX_SCRIPTS.includes(file[1]), `${file[1]} is a lazy bundle but index.html loads it`);
    lazy.push(file[1]);
  }
  assert.ok(lazy.length >= 12, 'the manifest was found');
  const L = lazyWorld({ cold: true });
  for (const name of LAZY_GLOBALS) assert.equal(L.w.run(`typeof ${name}`), 'undefined', `${name} is not defined by the first-paint set`);
  assert.equal(L.w.run('typeof openLauncher'), 'function', 'the launcher entry points are stubs');
  assert.equal(L.w.run('typeof Lazy'), 'object');
  assert.deepEqual(L.net.scripts, [], 'loading the shell set fetched nothing');
});

test('pools load only when enabled, wait for styles, paint current state and disappear when disabled', async () => {
  const L = lazyWorld();
  const { w } = L;
  w.document.body.append(w.get('el')('div', { id: 'banner' }));
  w.run('state.pools = null; Shell.limit(state)');
  assert.deepEqual(L.net.scripts, []);
  assert.deepEqual(L.net.sheets, []);
  w.run(`state.pools = { groups: [{ id: 1, provider: 'claude', accounts: ['work'], state: 'ok', window_5h_pct: 10 }] }; Shell.limit(state); Shell.limit(state)`);
  assert.equal(w.document.querySelector('.pool-strip'), null, 'no unstyled strip during the load');
  w.run('state = { ...state, pools: { groups: [{ ...state.pools.groups[0], window_5h_pct: 90 }] } }');
  assert.ok(await settle(() => w.document.querySelector('.pool-group')));
  assert.match(w.document.querySelector('.pool-group').textContent, /90%/);
  assert.deepEqual(L.net.scripts, ['/static/pool-usage.js']);
  assert.deepEqual(L.net.sheets, ['/static/pool-usage.css']);
  w.run('Shell.limit(state)');
  assert.equal(L.net.scripts.length, 1, 'subsequent paints reuse the bundle');
  w.run('state.pools = null; Shell.limit(state)');
  assert.equal(w.document.getElementById('pool-strip-host'), null);
});

for (const failed of ['/static/pool-usage.js', '/static/pool-usage.css']) {
  test(`pools retry a failed ${failed} through the shared loader without painting a partial bundle`, async () => {
    const L = lazyWorld({ fail: [failed] });
    const { w } = L;
    w.document.body.append(w.get('el')('div', { id: 'banner' }));
    w.run("globalThis.poolErrors = 0; toast = () => { poolErrors++; }; state.pools = { groups: [] }; Shell.limit(state)");
    assert.ok(await settle(() => w.get('poolErrors') === 1));
    assert.equal(w.document.querySelector('.pool-strip'), null);
    assert.equal(w.run("Lazy.done('pools')"), false);
    L.net.fail.clear();
    w.run('Shell.limit(state)');
    assert.ok(await settle(() => w.document.querySelector('.pool-strip')));
    assert.equal(L.net.scripts.length, failed.endsWith('.js') ? 2 : 1);
    assert.equal(L.net.sheets.length, failed.endsWith('.css') ? 2 : 1);
    assert.equal(w.run("Lazy.done('pools')"), true);
  });
}

test('pools disabled during loading do not paint stale data', async () => {
  const L = lazyWorld();
  const { w } = L;
  w.document.body.append(w.get('el')('div', { id: 'banner' }));
  w.run('state.pools = { groups: [] }; Shell.limit(state); state.pools = null; Shell.limit(state)');
  assert.ok(await settle(() => w.run("Lazy.done('pools')")));
  assert.equal(w.document.getElementById('pool-strip-host'), null);
});

test('the pages of the first-paint set mount without a fetch, with a mouse or without', async () => {
  for (const fine of [false, true]) {
    const L = lazyWorld({ fine });
    for (const [hash, id] of [['#/', 'home'], ['#/inbox', 'inbox'], ['#/search', 'search']]) {
      L.w.location.hash = hash;
      assert.equal(L.mounted(), id, `${id} mounts synchronously`);
    }
    await settle(null, 20);
    assert.deepEqual(L.net.scripts, [], `fine=${fine}`);
  }
});

test('the Tasks page with a mouse keeps the dispatch bar\'s room, loads dnd.js once and draws the bar; on touch it never loads it', async () => {
  const mouse = lazyWorld({ fine: true });
  const host = new mouse.dom.El('section');
  host.setAttribute('id', 'tasks');
  mouse.w.document.body.append(host);
  mouse.w.location.hash = '#/tasks';
  assert.equal(mouse.mounted(), 'tasks');
  assert.ok(await settle(() => mouse.w.run('typeof Dnd') !== 'undefined'));
  assert.deepEqual(mouse.net.scripts, ['/static/dnd.js']);
  assert.ok(await settle(() => mouse.w.document.querySelector('.dnd-bar')), 'the bar is drawn once dnd.js is here');
  assert.equal(mouse.w.document.querySelector('.dnd-bar-slot'), null, 'and the placeholder is gone');
  mouse.w.location.hash = '#/inbox';
  mouse.w.location.hash = '#/tasks';
  assert.deepEqual(mouse.net.scripts, ['/static/dnd.js'], 'the second visit loads nothing');

  const touch = lazyWorld({ fine: false });
  touch.w.location.hash = '#/tasks';
  await settle(null, 30);
  assert.deepEqual(touch.net.scripts, []);
  assert.equal(touch.w.document.querySelector('.dnd-bar-slot'), null);
});

const ROUTES = [
  { hash: '#/agents', id: 'agents', scripts: ['/static/pages/agents-page.js'], sheets: [] },
  { hash: '#/usage', id: 'usage', scripts: ['/static/charts.js', '/static/pages/usage.js'], sheets: ['/static/charts.css'] },
  { hash: '#/settings', id: 'settings', scripts: ['/static/nodes.js', '/static/pages/agents-page.js', '/static/nodes-pair.js', '/static/pages/doctor.js', '/static/pages/settings.js'], sheets: ['/static/pages/node-base.css', '/static/pages/settings.css'] },
  { hash: '#/memory', id: 'memory', scripts: ['/static/pages/memory.js'], sheets: ['/static/pages/memory.css'] },
  { hash: '#/quad', id: 'quad', scripts: ['/static/termkit.js', '/static/pages/quad.js'], sheets: ['/static/termkit.css', '/static/shell-dock.css', '/static/pages/quad.css'] },
  { hash: '#/p/ccboard', id: 'project', scripts: ['/static/tree.js', '/static/shell-tree.js', '/static/pages/project.js'], sheets: ['/static/pages/project.css', '/static/pages/home.css'] },
  { hash: '#/onboarding/project', id: 'onboarding', scripts: ['/static/launcher.js', '/static/launcher-node.js', '/static/pages/onboarding.js'], sheets: ['/static/pages/create.css', '/static/launcher.css', '/static/pages/onboarding.css'] },
];

for (const r of ROUTES) {
  test(`${r.hash}: the first visit loads ${r.scripts.length} script(s) and ${r.sheets.length} sheet(s) in order, once, before the page mounts; a second visit loads nothing`, async () => {
    const L = lazyWorld();
    L.w.location.hash = '#/inbox';
    assert.equal(L.mounted(), 'inbox');
    L.w.location.hash = r.hash;
    assert.equal(L.mounted(), null, 'the old page is taken down when the load starts (a page that rewrites its own address would otherwise undo the click)');
    assert.equal(L.w.document.body.getAttribute('data-page-loading'), r.id, 'the loading state is on the body (no visual)');
    assert.ok(await settle(() => L.mounted() === r.id), `${r.id} mounted`);
    assert.deepEqual(L.net.scripts, r.scripts);
    assert.deepEqual(L.net.sheets, r.sheets);
    assert.equal(L.w.document.body.getAttribute('data-page-loading'), null);
    assert.equal(L.w.document.body.getAttribute('data-page'), r.id);
    for (const src of r.scripts) assert.equal(L.net.scripts.filter((x) => x === src).length, 1);
    L.w.location.hash = '#/inbox';
    assert.equal(L.mounted(), 'inbox');
    const before = [L.net.scripts.length, L.net.sheets.length];
    L.w.location.hash = r.hash;
    assert.equal(L.mounted(), r.id, 'mounts at once the second time');
    assert.deepEqual([L.net.scripts.length, L.net.sheets.length], before, `nothing is fetched again: ${JSON.stringify(L.net.scripts)} ${JSON.stringify(L.net.sheets)}`);
  });
}

test('routeCount is not bumped by the late mount: history.back() decisions see one navigation per click', async () => {
  const L = lazyWorld();
  L.w.location.hash = '#/inbox';
  const n = L.w.get('routeCount');
  L.w.location.hash = '#/usage';
  assert.ok(await settle(() => L.mounted() === 'usage'));
  assert.equal(L.w.get('routeCount'), n + 1);
});

test('#/epic/<owner>/<repo>/<n> is a lazy route: epic.js and epic.css load once, in order, before the page mounts; the Tasks page and the palette never load them on their own', async () => {
  const L = lazyWorld();
  L.w.location.hash = '#/inbox';
  L.w.location.hash = '#/epic/example/website/147';
  assert.equal(L.mounted(), null);
  assert.ok(await settle(() => L.mounted() === 'epic'), 'epic mounted');
  assert.deepEqual(L.net.scripts, ['/static/pages/epic.js', '/static/pages/epic-new.js']);          // the New epic sheet (issue #158) comes with the epic bundle
  assert.deepEqual(L.net.sheets, ['/static/pages/epic.css']);
  assert.deepEqual(L.hrefs(), ['/static/pages/epic.css'], 'the sheet goes in before charts.css');
  assert.equal(L.w.document.body.getAttribute('data-page'), 'epic');
  L.w.location.hash = '#/inbox';                                        // leaves the page: its timer and listener go
  const T = lazyWorld({ fine: false });
  T.w.location.hash = '#/tasks';
  await settle(null, 30);
  assert.deepEqual(T.net.scripts, [], 'Tasks without a sign of epics loads nothing for them');
  assert.equal(T.w.document.querySelector('.tk-tabs'), null);
});

test('page sheets go in before charts.css, in the order pages.css had their rules, whichever page was opened first', async () => {
  const L = lazyWorld();
  for (const h of ['#/memory', '#/quad', '#/settings']) {
    L.w.location.hash = h;
    assert.ok(await settle(() => L.mounted() === h.slice(2)), `${h} mounted ${L.mounted()} ${JSON.stringify(L.net.scripts)}`);
  }
  assert.deepEqual(L.hrefs(), ['/static/shell-dock.css', '/static/pages/node-base.css', '/static/pages/settings.css', '/static/pages/quad.css', '/static/pages/memory.css', '/static/termkit.css']);
});

test('a script that cannot be fetched leaves an error state with a Retry, not a blank page; Retry loads again and mounts', async () => {
  const L = lazyWorld({ fail: ['/static/pages/usage.js'] });
  L.w.location.hash = '#/inbox';
  L.w.location.hash = '#/usage';
  assert.ok(await settle(() => L.w.document.querySelector('#page .load-error')));
  const page = L.w.document.querySelector('#page');
  const err = page.querySelector('.load-error');
  assert.equal(err.getAttribute('role'), 'alert');
  assert.match(err.textContent, /could not be loaded/);
  assert.equal(L.mounted(), null, 'the old page is taken down');
  assert.equal(L.w.document.body.getAttribute('data-page-loading'), null);
  const retry = err.querySelector('button');
  assert.equal(retry.textContent, 'Retry');
  L.net.fail.clear();
  retry.click();
  assert.ok(await settle(() => L.mounted() === 'usage'));
  assert.equal(page.querySelector('.load-error'), null);
  assert.equal(L.net.scripts.filter((s) => s === '/static/pages/usage.js').length, 2, 'asked again after the failure');
  assert.equal(L.net.scripts.filter((s) => s === '/static/charts.js').length, 1, 'the part that loaded stays loaded');
});

test('a sheet that cannot be fetched is an error state too: the page never mounts unstyled, not even when its script ran, and the next visit asks for the sheet again', async () => {
  const L = lazyWorld({ fail: ['/static/pages/memory.css'] });
  L.w.location.hash = '#/memory';
  assert.ok(await settle(() => L.w.document.querySelector('#page .load-error')));
  assert.equal(L.mounted(), null);
  assert.notEqual(L.w.run('typeof Memory'), 'undefined', 'the script itself did run');
  assert.deepEqual(L.hrefs(), [], 'the failed sheet is not left in the head');
  L.net.fail.clear();
  L.w.location.hash = '#/inbox';
  L.w.location.hash = '#/memory';
  assert.equal(L.mounted(), null, 'a global that exists is not proof the bundle is whole: still loading');
  assert.ok(await settle(() => L.mounted() === 'memory'));
  assert.deepEqual(L.net.sheets, ['/static/pages/memory.css', '/static/pages/memory.css']);
  assert.deepEqual(L.net.scripts, ['/static/pages/memory.js'], 'the script that did load is not fetched twice');
});

test('a load that finishes after the person went elsewhere only registers its page', async () => {
  const L = lazyWorld();
  L.w.location.hash = '#/inbox';
  L.w.location.hash = '#/usage';
  L.w.location.hash = '#/inbox';
  assert.equal(L.mounted(), 'inbox');
  assert.ok(await settle(() => L.w.run('typeof Usage') !== 'undefined'));
  await settle(null, 20);
  assert.equal(L.mounted(), 'inbox', 'not dragged back to Usage');
  L.w.location.hash = '#/usage';
  assert.equal(L.mounted(), 'usage', 'registered: the next visit is synchronous');
});

test('a cold deep link shows Loading… after a beat when the load is slow and nothing is mounted, and the page replaces it', async () => {
  const L = lazyWorld({ hash: '#/settings', slow: 400 });
  L.w.run('route()');
  assert.equal(L.mounted(), null);
  const page = L.w.document.querySelector('#page');
  assert.ok(!/Loading…/.test(page.textContent), 'not at once: a fast load never flashes it');
  assert.ok(await settle(() => /Loading…/.test(page.textContent), 1000000), 'the hint shows after about 250 ms');
  assert.equal(L.mounted(), null);
  assert.ok(await settle(() => L.mounted() === 'settings', 1000000));
  assert.ok(!/Loading…/.test(page.textContent), 'the hint is gone once the page is up');
});

test('an unknown address and the pages of the first-paint set never ask the loader for anything', () => {
  const L = lazyWorld({ fine: false });
  for (const id of ['home', 'inbox', 'tasks', 'search']) assert.deepEqual(plain(L.w.run(`Lazy.pending('${id}')`)), [], id);
  assert.deepEqual(plain(L.w.run("Lazy.pending('usage')")), ['usage']);
  assert.deepEqual(plain(L.w.run("Lazy.pending('project')")), ['project']);
  assert.equal(L.w.run("Lazy.wants('dnd')"), false, 'dnd is for a mouse: not wanted on touch');
});

test('the launcher entry points are stubs that load launcher.js once and then call the real function', async () => {
  const L = lazyWorld();
  assert.equal(typeof L.w.run('openLauncher({ mode: "session" })').then, 'function');
  assert.equal(typeof L.w.run('taskDispatchSheet({ id: 1 })').then, 'function');
  assert.ok(await settle(() => L.w.run('typeof launcherSchema') === 'function'));
  assert.deepEqual(L.net.scripts, ['/static/launcher.js', '/static/launcher-node.js'], 'one fetch for both');
  assert.equal(L.w.run('openLauncher.toString().includes("lazyLoad")'), false, 'the real openLauncher replaced the stub');
  assert.equal(L.w.run('window.openLauncher === openLauncher'), true);
});

test('palette.js loads when a key needs it, once, and the key then runs', async () => {
  const L = lazyWorld();
  L.w.run('globalThis.__ran = 0; Keymap.withPalette(() => { __ran += 1; }); Keymap.withPalette(() => { __ran += 10; });');
  assert.equal(L.w.run('__ran'), 0);
  assert.ok(await settle(() => L.w.run('__ran') > 0));
  assert.equal(L.w.run('__ran'), 1, 'a key pressed again while it loads is not queued');
  assert.deepEqual(L.net.scripts, ['/static/palette.js']);
  L.w.run('Keymap.withPalette(() => { __ran += 10; })');
  assert.equal(L.w.run('__ran'), 11, 'loaded: synchronous');
  assert.deepEqual(L.net.scripts, ['/static/palette.js']);
});

test('Lazy.later runs after a bundle loads and never asks for it; Lazy.run loads it', async () => {
  const L = lazyWorld({ fine: false });
  L.w.run('globalThis.__a = 0; globalThis.__b = 0; Lazy.later("tree", () => { __a += 1; });');
  await settle(null, 10);
  assert.equal(L.w.run('__a'), 0);
  assert.deepEqual(L.net.scripts, [], 'later() did not load anything');
  L.w.run('Lazy.run("tree", () => { __b += 1; })');
  assert.ok(await settle(() => L.w.run('__b') === 1));
  assert.equal(L.w.run('__a'), 1, 'the queued work ran once the bundle was there');
  assert.deepEqual(L.net.scripts, ['/static/tree.js']);
});

test('a pointer over a link to a lazy page starts loading it (warm); the click then mounts at once', async () => {
  const L = lazyWorld();
  L.w.run('Lazy.watchLinks()');
  const a = new L.dom.El('a');
  a.setAttribute('href', '#/memory');
  L.w.document.body.append(a);
  L.w.document.dispatch('pointerover', { target: a });
  assert.ok(await settle(() => L.w.run('typeof Memory') !== 'undefined'));
  assert.deepEqual(L.net.scripts, ['/static/pages/memory.js']);
  L.w.location.hash = '#/memory';
  assert.equal(L.mounted(), 'memory');
});

test('the dock asks for the terminal kit on first use and opens when it is here; the quad page loads it as a need', async () => {
  const L = lazyWorld({ fine: false });
  L.w.run('Shell.dockWide = () => true; Shell.dockOff = () => false');
  assert.equal(L.w.run('Shell.dockOn()'), true, 'the kit is not here but can be had: the dock is on');
  assert.equal(L.w.run('Shell.kitReady()'), false);
  L.w.run('Shell.dock = Shell.dock || {}');
  assert.equal(L.w.run('Shell.openDock("shop--api--s1", { quiet: true })'), true, 'on its way');
  assert.ok(await settle(() => L.w.run('typeof TermKit') !== 'undefined'));
  assert.deepEqual(L.net.scripts, ['/static/termkit.js']);
});

test('a board never used, with no project: Home loads the wizard (and the launcher it needs) once and sends the person there; any other Home never asks', async () => {
  const L = lazyWorld({ state: fixtureState({ setup: { first_run: true, ok: true }, projects: [] }) });
  L.w.location.hash = '#/';
  assert.equal(L.mounted(), 'home');
  assert.ok(await settle(() => L.mounted() === 'onboarding'));
  assert.deepEqual(L.net.scripts, ['/static/launcher.js', '/static/launcher-node.js', '/static/pages/onboarding.js']);
  assert.deepEqual(L.net.sheets, ['/static/pages/create.css', '/static/launcher.css', '/static/pages/onboarding.css']);
  assert.match(L.w.location.hash, /^#\/onboarding/);
  const calm = lazyWorld({ state: fixtureState() });
  calm.w.location.hash = '#/';
  await settle(null, 30);
  assert.deepEqual(calm.net.scripts, [], 'a board with projects never loads the wizard');
});

test('a box with claude-mem: the project page loads the memory bundle (script and sheet) once for its strip and tab; a box without it never does', async () => {
  const mem = { state: 'ok', observations: 3, queue_depth: 0, processing: false, version: '1', rates: {}, reason: '' };
  const L = lazyWorld({ state: fixtureState({ memory: mem }) });
  L.w.location.hash = '#/p/ccboard';
  assert.ok(await settle(() => L.mounted() === 'project'));
  assert.ok(await settle(() => L.w.run('typeof memoryGotchasMount') === 'function'));
  assert.deepEqual(L.net.scripts, ['/static/tree.js', '/static/shell-tree.js', '/static/pages/project.js', '/static/pages/memory.js']);
  assert.deepEqual(L.net.sheets, ['/static/pages/project.css', '/static/pages/home.css', '/static/pages/memory.css']);
  const bare = lazyWorld({ state: fixtureState() });
  bare.w.location.hash = '#/p/ccboard';
  assert.ok(await settle(() => bare.mounted() === 'project'));
  await settle(null, 30);
  assert.deepEqual(bare.net.scripts, ['/static/tree.js', '/static/shell-tree.js', '/static/pages/project.js']);
});

test('the Usage page\'s rename pencil loads Settings on first use and then opens its one rename sheet', async () => {
  const L = lazyWorld();
  L.w.location.hash = '#/usage';
  assert.ok(await settle(() => L.mounted() === 'usage'));
  assert.equal(L.w.run('typeof settingsRenameAccount'), 'undefined');
  L.w.run('Usage.renameAccount({ key: "acct-1", name: "Work", email: "a@example.com" })');
  assert.ok(await settle(() => L.w.run('typeof settingsRenameAccount') === 'function'));
  assert.ok(L.net.scripts.includes('/static/pages/settings.js'));
  assert.ok(await settle(() => L.w.document.querySelector('#sheet').open === true), 'the rename sheet is open');
});

// ---------------------------------------------------------------- the lazy halves of the fixes after #103: the Agents page, the create flow, the task card's sheets

const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');

test('the Agents page is a lazy route: pages/agents.js keeps the session card and the roster helpers Home reads, pages/agents-page.js registers the page', async () => {
  const L = lazyWorld();
  for (const name of ['sessionCard', 'agentsGroups', 'agentsCounts', 'accountLogin']) assert.equal(L.w.run(`typeof ${name}`), 'function', `${name} stays in the first-paint set`);
  for (const name of ['agentsExtRows', 'agentsExtNode', 'agentsGroupNode', 'agentsSummaryNode', 'agentsExt']) assert.equal(L.w.run(`typeof ${name}`), 'undefined', `${name} is in the agents bundle`);
  assert.deepEqual(plain(L.w.run("Lazy.pending('agents')")), ['agents']);
  L.w.location.hash = '#/inbox';
  assert.deepEqual(L.net.scripts, [], 'Home and the Inbox never ask for it');
  L.w.location.hash = '#/agents';
  assert.equal(L.mounted(), null, 'taken down while the bundle loads');
  assert.ok(await settle(() => L.mounted() === 'agents'));
  assert.deepEqual(L.net.scripts, ['/static/pages/agents-page.js']);
  assert.equal(L.w.run('typeof agentsExtRows'), 'function');
});

test('a pointer over the Agents link warms the page, and Settings brings the Codex threads list of the agents bundle with it', async () => {
  const L = lazyWorld();
  L.w.run("Lazy.warm('agents')");
  assert.ok(await settle(() => L.w.run('typeof agentsPage') !== 'undefined'));
  assert.deepEqual(L.net.scripts, ['/static/pages/agents-page.js']);
  const S = lazyWorld();
  S.w.location.hash = '#/settings';
  assert.ok(await settle(() => S.mounted() === 'settings'));
  assert.ok(S.net.scripts.indexOf('/static/pages/agents-page.js') < S.net.scripts.indexOf('/static/pages/settings.js'), 'settings waits for agents; independent prerequisites may load together');
  assert.equal(S.w.run('typeof agentsExtLoad'), 'function');
});

test('Shell.openCreate answers at once and the create flow (shell-create.js) loads on the first tap, once', async () => {
  const L = lazyWorld({ realCreate: true });
  const sheet = () => L.w.document.getElementById('sheet');
  for (const name of ['openCreate', 'createItems', 'defaultRepo', 'withCreate', 'withForms', 'targetLabel']) assert.equal(L.w.run(`typeof Shell.${name}`), 'function', `Shell.${name} stays in shell.js`);
  for (const name of ['pickRepo', 'showForm', 'createFor', 'launchAt', 'routeCtx', 'createPlace']) assert.equal(L.w.run(`typeof Shell.${name}`), 'undefined', `Shell.${name} is in the shellcreate bundle`);
  assert.equal(L.w.run("Shell.openCreate('nonsense')"), false, 'an unknown kind is answered without loading anything');
  assert.deepEqual(L.net.scripts, [], 'an unknown kind loads nothing');
  assert.equal(L.w.run("Shell.openCreate('session')"), true, 'the answer does not wait for the file');
  assert.equal(sheet().open, false);
  assert.ok(await settle(() => sheet().open === true));
  assert.deepEqual(L.net.scripts, ['/static/shell-create.js']);
  assert.equal(sheet().querySelector('.sheet-title').textContent, 'New session');
  sheet().close();
  assert.equal(L.w.run("Shell.openCreate('task')"), true);
  assert.equal(sheet().open, true, 'loaded: opens in the same tick');
  assert.deepEqual(L.net.scripts, ['/static/shell-create.js'], 'nothing is fetched twice');
});

test('a create flow that cannot be fetched says so in a toast and the next tap tries again', async () => {
  const L = lazyWorld({ realCreate: true, fail: ['/static/shell-create.js'] });
  L.w.ctx.__toasts = [];
  L.w.run('toast = (t) => { __toasts.push(t); }');
  assert.equal(L.w.run("Shell.openCreate('session')"), true);
  assert.ok(await settle(() => L.w.ctx.__toasts.length > 0));
  assert.match(L.w.ctx.__toasts[0], /Could not load the create sheet/);
  L.net.fail.clear();
  L.w.run("Shell.openCreate('session')");
  assert.ok(await settle(() => L.w.document.getElementById('sheet').open === true));
  assert.equal(L.net.scripts.filter((x) => x === '/static/shell-create.js').length, 2);
});

const BACKLOG = { id: 31, slug: 'tidy', title: 'Tidy the settings page', project: 'ccboard', repo: 'ccboard', column: 'backlog', phase: 'backlog', agent: 'claude', prompt: 'Tidy it', prompt_len: 7 };

test("the task card's sheets are stubs until the first tap: Move and Edit load task-sheets.js once and open", async () => {
  const L = lazyWorld();
  const sheet = () => L.w.document.getElementById('sheet');
  for (const name of ['taskMoveSheet', 'taskEditSheet', 'taskPortSheet', 'openTaskModal']) assert.equal(L.w.run(`typeof ${name}`), 'function', `${name} answers before the file is here`);
  assert.equal(L.w.run('typeof diffSideControl'), 'undefined');
  assert.equal(L.w.run('typeof makeTaskBoard'), 'undefined', "the project board is project.js's");
  L.w.ctx.__t = BACKLOG;
  L.w.run('taskMoveSheet(__t)');
  L.w.run('taskEditSheet(__t)');
  assert.ok(await settle(() => sheet().open === true));
  assert.deepEqual(L.net.scripts, ['/static/task-sheets.js'], 'one fetch for both taps');
  assert.equal(L.w.run('taskMoveSheet.toString().includes("lazyLoad")'), false, 'the real sheet replaced the stub');
  assert.equal(L.w.run('window.taskEditSheet === taskEditSheet'), true);
  assert.match(sheet().querySelector('.sheet-title').textContent, /^(Move|Edit task)/);
  sheet().close();
  L.w.run('taskEditSheet(__t)');
  assert.equal(sheet().open, true, 'loaded: opens in the same tick');
  assert.match(sheet().querySelector('.sheet-title').textContent, /^Edit task/);
  assert.deepEqual(L.net.scripts, ['/static/task-sheets.js']);
});

test('a task sheet that cannot be fetched toasts, and the sheet opens on the next tap', async () => {
  const L = lazyWorld({ fail: ['/static/task-sheets.js'] });
  L.w.ctx.__toasts = [];
  L.w.ctx.__t = BACKLOG;
  L.w.run('toast = (t) => { __toasts.push(t); }');
  L.w.run('taskEditSheet(__t)');
  assert.ok(await settle(() => L.w.ctx.__toasts.length > 0));
  assert.match(L.w.ctx.__toasts[0], /Could not load the task sheet/);
  assert.equal(L.w.document.getElementById('sheet').open, false);
  L.net.fail.clear();
  L.w.run('taskEditSheet(__t)');
  assert.ok(await settle(() => L.w.document.getElementById('sheet').open === true));
});

test('the diff and pull request sheet comes with the same bundle: openTaskModal and its segmented control', async () => {
  const L = lazyWorld();
  L.w.ctx.__t = { ...BACKLOG, id: 32, column: 'review', phase: 'review', branch: 'task/tidy' };
  L.w.run('openTaskModal(__t)');
  assert.ok(await settle(() => L.w.run('typeof diffSideControl') === 'function'));
  assert.deepEqual(L.net.scripts, ['/static/task-sheets.js']);
  assert.ok(await settle(() => L.w.document.getElementById('sheet').open === true));
});

test('the project page brings its own task board: makeTaskBoard is in project.js, not in components.js', async () => {
  const L = lazyWorld();
  L.w.location.hash = '#/p/ccboard';
  assert.ok(await settle(() => L.mounted() === 'project'));
  assert.equal(L.w.run('typeof makeTaskBoard'), 'function');
  assert.ok(!read('components.js').includes('function makeTaskBoard'));
  assert.ok(read('pages/project.js').includes('function makeTaskBoard'));
});


// Cold route contracts: no historical companions and no page preloaded.
for (const [hash, id] of [['#/', 'home'], ['#/inbox', 'inbox'], ['#/tasks', 'tasks'], ['#/agents', 'agents'], ['#/search?q=hello', 'search'], ['#/usage', 'usage'], ['#/p/ccboard', 'project'], ['#/settings', 'settings'], ['#/quad', 'quad'], ['#/memory', 'memory'], ['#/onboarding/project', 'onboarding'], ['#/s/ccboard--ccboard--s1', 'session'], ['#/n/build-box', 'node'], ['#/epic/example/website/147', 'epic'], ['#/team', 'team']]) {
  test(`cold ${hash}: only its dependency graph loads, styles settle, and mount/update have no missing globals`, async () => {
    const L = lazyWorld({ cold: true, hash, fine: false });
    const errors = [];
    L.w.ctx.console = { ...console, error: (...args) => errors.push(args.map(String).join(' ')) };
    assert.equal(L.w.run(`typeof pages['${id}']`), 'undefined');
    L.w.run('route()');
    assert.equal(L.mounted(), null);
    assert.ok(await settle(() => L.w.document.body.getAttribute('data-page') === id), `${id}: ${errors.join('; ')}`);
    L.w.run('render(true)');
    assert.deepEqual(errors, [], 'mount, first update and shared shell render must not hide missing dependencies');
    assert.ok(L.w.document.querySelector('#page').children.length);
    assert.equal(L.w.document.body.getAttribute('data-page-loading'), null);
    assert.equal(new Set(L.net.scripts).size, L.net.scripts.length, 'one request per script');
    if (id !== 'session' && id !== 'home') assert.ok(!L.net.scripts.includes('/static/pages/home.js'), 'shared helpers do not pull in Home');
    if (id === 'home') {
      for (const file of ['pages/inbox.js', 'pages/tasks.js', 'pages/agents-page.js', 'pages/search.js', 'pages/session.js', 'nodes.js', 'keymap.js', 'components-tasks.js', 'shell-dock.js']) assert.ok(!L.net.scripts.includes('/static/' + file), file);
    }
    L.w.run('takeDownPage()');
  });
}

test('cold CSS failure cannot be hidden by a successful probe; retry mounts after the CSS arrives', async () => {
  const L = lazyWorld({ cold: true, hash: '#/agents', fail: ['/static/pages/session-row.css'] });
  L.w.run('route()');
  assert.ok(await settle(() => L.w.document.querySelector('.load-error')));
  assert.equal(L.mounted(), null);
  L.net.fail.clear();
  L.w.document.querySelector('.load-error button').click();
  assert.ok(await settle(() => L.mounted() === 'agents'));
  assert.equal(L.net.scripts.filter((p) => p === '/static/pages/agents.js').length, 1);
  assert.equal(L.net.sheets.filter((p) => p === '/static/pages/session-row.css').length, 2);
  L.w.run('takeDownPage()');
});

test('cold keyboard: the first chord is queued through the loader and field typing stays alone', async () => {
  const L = lazyWorld({ cold: true, hash: '#/' });
  L.w.run('Lazy.watchKeys()');
  const gate = L.w.document.listeners.keydown.at(-1);
  const event = (key, target = L.w.document.body) => ({ key, target, preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() {} });
  const typing = event('g', { tagName: 'INPUT' });
  gate(typing);
  assert.equal(typing.defaultPrevented, undefined);
  assert.deepEqual(L.net.scripts, []);
  gate(event('g')); gate(event('a'));
  assert.ok(await settle(() => L.w.location.hash === '#/agents'));
  assert.ok(await settle(() => L.mounted() === 'agents'));
  assert.equal(L.net.scripts.filter((p) => p === '/static/keymap.js').length, 1);
  L.w.run('takeDownPage()');
});

test('bootstrap gives the skeleton two animation frames before requesting the entry bundle', async () => {
  const frames = [], scripts = [], loads = [];
  const loader = { load: name => {
    loads.push(name);
    return Promise.resolve();
  } };
  const w = makeWorld({ companions: false, requestAnimationFrame: fn => frames.push(fn) });
  w.document.head = { appendChild: s => scripts.push(s) };
  w.run(read('boot.js').replaceAll('__ASSET_VERSION__', 'release-b'));
  assert.equal(scripts.length, 0);
  frames.shift()();
  assert.equal(scripts.length, 0, 'no app request before the first rendering opportunity');
  frames.shift()();
  assert.equal(scripts[0].src, '/static/lazy.js?v=release-b');
  assert.equal(scripts[0].defer, true);
  assert.equal(scripts[0].async, false);
  w.ctx.Lazy = loader;
  scripts[0].onload();
  assert.deepEqual(loads, ['main']);
});

test('the entry script preloads with app but cannot execute until all base CSS has loaded', async () => {
  const L = lazyWorld({ cold: true });
  // This fixture already defines app exports; mark it started so CSS is required.
  L.w.run('lazyStarted.app = true');
  const pending = [];
  L.head.appendChild = n => { pending.push(n); return n; };
  const load = L.w.run("Lazy.load('main')");
  const preload = pending.find(n => n.getAttribute('rel') === 'preload' && n.getAttribute('href') === '/static/main.js');
  assert.ok(preload);
  assert.equal(preload.getAttribute('fetchpriority'), 'low', 'dependent bytes must not crowd out prerequisites');
  await settle(null, 2);
  assert.equal(pending.some(n => n.tagName === 'SCRIPT'), false);
  const sheets = pending.filter(n => n.getAttribute('rel') === 'stylesheet');
  assert.equal(sheets.length, 6);
  for (const sheet of sheets.slice(1)) sheet.onload();
  await settle(null, 2);
  assert.equal(pending.some(n => n.tagName === 'SCRIPT'), false, 'even one outstanding base sheet blocks execution');
  sheets[0].onload();
  assert.ok(await settle(() => pending.some(n => n.tagName === 'SCRIPT')));
  L.w.run('const firstStateReady = true');
  pending.find(n => n.tagName === 'SCRIPT').onload();
  await load;
});

test('cold Home renders pending settings without Settings and loads restart actions only on a tap', async () => {
  const st = fixtureState();
  st.settings_pending = { count: 1, labels: ['Task lanes'] };
  st.restart = { method: 'docker', hint: 'docker compose restart ccboard', blockers: [] };
  const L = lazyWorld({ cold: true, hash: '#/', state: st, fine: false });
  L.w.run('route()');
  assert.ok(await settle(() => L.mounted() === 'home'));
  assert.ok(L.w.document.querySelector('.home-settings-pending'));
  assert.ok(!L.net.scripts.includes('/static/pages/settings.js'));
  assert.ok(!L.net.scripts.includes('/static/settings-restart-actions.js'));
  L.w.document.querySelector('.set-restart-control button').click();
  assert.ok(await settle(() => L.net.scripts.includes('/static/settings-restart-actions.js')));
  assert.ok(await settle(() => L.w.run('typeof settingsRestartPoll') === 'function'));
  assert.ok(!L.net.scripts.includes('/static/pages/settings.js'));
  L.w.run('takeDownPage()');
});

test('the cold keyboard gate preserves native Enter, Space and Escape on controls', () => {
  const L = lazyWorld({ cold: true });
  L.w.run('Lazy.watchKeys()');
  const gate = L.w.document.listeners.keydown.at(-1);
  const target = L.w.document.createElement('button');
  L.w.document.body.append(target);
  for (const key of ['Enter', ' ', 'Escape']) {
    const e = { key, target, preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() {} };
    gate(e);
    assert.equal(e.defaultPrevented, undefined, `${key} must retain its native action`);
  }
});

test('cold dock: its first Open waits for the terminal sheet and implementation, and Close cancels an in-flight Open', async () => {
  const L = lazyWorld({ cold: true, fine: false });
  L.w.run('Shell.dockWide = () => true; Shell.dockOff = () => false');
  assert.equal(L.w.run('typeof TermKit'), 'undefined');
  assert.equal(L.w.run('typeof Shell.dockMount'), 'undefined');
  assert.equal(L.w.run('Shell.openDock("shop--api--s1", { quiet: true })'), true);
  L.w.run('Shell.closeDock()');
  assert.ok(await settle(() => L.w.run('typeof Shell.dockMount') === 'function'));
  assert.deepEqual(L.net.scripts, ['/static/termkit.js', '/static/shell-dock.js']);
  assert.deepEqual(L.net.sheets, ['/static/termkit.css', '/static/shell-dock.css']);
  assert.equal(L.w.run('Shell.dock.wanted'), '');
  assert.equal(L.w.document.querySelector('#dock').children.length, 0);
});


test('the cold keyboard gate leaves browser modifier shortcuts and a pending chord Tab alone', () => {
  const L = lazyWorld({ cold: true });
  L.w.run('Lazy.watchKeys()');
  const gate = L.w.document.listeners.keydown.at(-1);
  for (const props of [{ key: 'J', ctrlKey: true, shiftKey: true }, { key: 'k', metaKey: true }]) {
    const e = { ...props, target: L.w.document.body, preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() {} };
    gate(e); assert.equal(e.defaultPrevented, undefined);
  }
  const e = { key: 'g', target: L.w.document.body, preventDefault() { this.defaultPrevented = true; }, stopImmediatePropagation() {} };
  gate(e);
  const tab = { ...e, key: 'Tab', defaultPrevented: false };
  gate(tab); assert.equal(tab.defaultPrevented, false);
});

test('cold Settings App keeps the shortcut button and loads its dialog on demand', async () => {
  const L = lazyWorld({ cold: true });
  await L.w.run("Lazy.load('settings')");
  assert.equal(L.w.run('typeof Keymap'), 'undefined');
  assert.equal(L.w.run('typeof Palette'), 'undefined');
  assert.equal(L.w.run('settingsHelpAvailable()'), true);
  L.w.run('settingsOpenShortcuts()');
  assert.ok(await settle(() => L.w.run("typeof Palette !== 'undefined'")));
  assert.ok(L.net.scripts.includes('/static/palette.js'));
  assert.ok(await settle(() => L.w.document.querySelector('#helpdlg')?.open));
});

// Review regressions: use the browser's load event even when execution throws.
test('review: failed nodes prerequisite retries all Settings members in order with complete exports', async () => {
  const L = lazyWorld({ cold: true, hash: '#/settings', fail: ['/static/nodes.js'] });
  L.w.run('route()');
  assert.ok(await settle(() => L.w.document.querySelector('.load-error')));
  assert.equal(L.w.run("Lazy.done('settings')"), false);
  assert.ok(L.net.preloads.includes('/static/nodes-pair.js'), 'dependent bytes are fetched while prerequisites load');
  assert.ok(!L.net.scripts.includes('/static/nodes-pair.js'), 'a preload cannot execute against a failed prerequisite');
  L.net.fail.clear();
  L.w.document.querySelector('.load-error button').click();
  assert.ok(await settle(() => L.mounted() === 'settings'));
  assert.equal(L.w.run("Lazy.done('settings')"), true);
  assert.equal(L.w.run('typeof NodeView.peer'), 'function');
  assert.ok(L.w.run('NodeView.SCOPES.length') > 0);
  assert.deepEqual(L.net.errors || [], [], 'dependents never execute against a failed prerequisite');
  assert.ok(L.net.scripts.lastIndexOf('/static/nodes.js') < L.net.scripts.lastIndexOf('/static/nodes-pair.js'));
  L.w.run('delete NodeView.peer');
  assert.equal(L.w.run("Lazy.done('settings')"), false, 'a secondary export is required even after a successful load');
  L.w.run('takeDownPage()');
});

test('review: mouse-opened Settings shortcuts install the keyboard exactly once', async () => {
  const L = lazyWorld({ cold: true });
  await L.w.run("Lazy.load('settings')");
  L.w.run('settingsOpenShortcuts()');
  assert.ok(await settle(() => L.w.document.querySelector('#helpdlg')?.open));
  assert.equal(L.w.run('Keymap.installed'), true);
  const help = L.w.run('Keymap.help().length');
  assert.ok(help > 20);
  L.w.run('Keymap.install(); settingsOpenShortcuts(); Keymap.install()');
  assert.equal(L.w.run('Keymap.help().length'), help);
  assert.equal(L.w.document.listeners.keydown.filter(f => f === L.w.get('Keymap.listener')).length, 1);
});

test('review: Usage mount queues r and an unmount cancels it before keyboard arrival', async () => {
  for (const leave of [false, true]) {
    const L = lazyWorld({ cold: true, hash: '#/usage' });
    L.w.run('route()');
    assert.ok(await settle(() => L.mounted() === 'usage'));
    assert.equal(L.w.run('typeof Keymap'), 'undefined');
    if (leave) L.w.run('takeDownPage()');
    await L.w.run("Lazy.load('keyboard')");
    assert.equal(L.w.run("Keymap.list.filter(b => b.group === 'Usage' && b.spec === 'r').length"), leave ? 0 : 1);
    if (!leave) {
      L.w.run("globalThis.cycles = 0; Usage.cycle = () => cycles++; Keymap.handle({key:'r',target:document.body,preventDefault(){}})");
      assert.equal(L.w.get('cycles'), 1);
      L.w.run('takeDownPage()');
      assert.equal(L.w.run("Keymap.list.filter(b => b.group === 'Usage').length"), 0);
    }
  }
});

test('review: nodes loaded before keyboard retain g n and every remote-session action', async () => {
  const L = lazyWorld({ cold: true });
  await L.w.run("Lazy.load('nodeshub')");
  assert.equal(L.w.run('typeof Keymap'), 'undefined');
  await L.w.run("Lazy.load('keyboard')");
  assert.equal(L.w.run("Keymap.list.filter(b => b.spec === 'g n').length"), 1);
  L.w.run(`globalThis.remoteActions = []; Nodes.peek = Object.fromEntries(['focus','ack','allow','deny','open'].map(n => [n, () => {remoteActions.push(n);return true}]))`);
  for (const key of ['r', 'a', 'y', 'd', 'o']) L.w.run(`Keymap.handle({key:'${key}',target:document.body,preventDefault(){}})`);
  assert.deepEqual(plain(L.w.get('remoteActions')), ['focus', 'ack', 'allow', 'deny', 'open']);
});

for (const [file, action, args] of [
  ['account-actions.js', 'accountSwitch', "{name:'Work'}"],
  ['account-actions.js', 'acctProblemDismiss', ''],
  ['quick-editor.js', 'quickReplyEditor', '{items:[]}'],
]) test(`review: 404 ${file} from ${action} is caught and displays a working Retry`, async () => {
  const L = lazyWorld({ cold: true, fail: ['/static/' + file] });
  const result = await L.w.run(`${action}(${args})`);
  assert.equal(result, false);
  const alert = L.w.document.querySelector('.toast.bad');
  assert.ok(alert);
  assert.match(alert.textContent, /Could not load/);
  assert.equal(alert.querySelector('button').textContent, 'Retry');
  L.net.fail.clear();
  alert.querySelector('button').click();
  assert.ok(await settle(() => L.w.run(`!${action}.lazyStub`)));
  assert.equal(L.net.scripts.filter(p => p === '/static/' + file).length, 2);
  await settle(null, 20);
});

test('review: all scripts declare an export probe and a load event without exports is rejected', async () => {
  const L = lazyWorld({ cold: true });
  assert.deepEqual(plain(L.w.run('Object.values(LAZY_BUNDLES).flatMap(b => b.js || []).filter(src => !LAZY_EXPORTS[src])')), []);
  L.w.load = () => {}; // Browser successfully fetches an empty/corrupted script.
  await assert.rejects(L.w.run("Lazy.load('quickeditor')"), /missing export/);
  assert.equal(L.w.run("Lazy.done('quickeditor')"), false);
});

test('review: removing a dependent export invalidates its fulfilled load promise and Retry restores it', async () => {
  const L = lazyWorld({ cold: true });
  await L.w.run("Lazy.load('settings')");
  L.w.run('delete NodeView.peer; delete NodeView.SCOPES');
  assert.equal(L.w.run("Lazy.done('settings')"), false);
  await L.w.run("Lazy.load('settings')");
  assert.equal(L.w.run('typeof NodeView.peer'), 'function');
  assert.ok(L.w.run('NodeView.SCOPES.length') > 0);
  assert.equal(L.net.scripts.filter(p => p === '/static/nodes-pair.js').length, 2);
});

test('review: Lazy and core.loadAsset stamp every script and stylesheet with the boot release', async () => {
  const L = lazyWorld({ cold: true });
  L.w.document.documentElement.setAttribute('data-release', 'release-b');
  const urls = [];
  const append = L.head.appendChild.bind(L.head);
  L.head.appendChild = n => { urls.push(n.src || n.getAttribute('href')); return append(n); };
  await L.w.run("Lazy.load('widgets')");
  assert.ok(urls.includes('/static/pages/widgets.js?v=release-b'));
  assert.ok(urls.includes('/static/pages/widgets.css?v=release-b'));
  await L.w.run("Lazy.load('agents')");
  assert.ok(L.net.preloads.includes('/static/pages/agents-page.js?v=release-b'), 'preloads share the release-scoped script URL');
  // loadAsset uses append, not appendChild; intercept that independent path.
  L.head.append = n => { urls.push(n.src || n.getAttribute('src') || n.getAttribute('href')); n.onload(); };
  await L.w.run("loadAsset('/static/vendor/diff2html.min.css'); loadAsset('/static/vendor/diff2html-ui-base.min.js')");
  assert.ok(urls.slice(-2).every(u => u.endsWith('?v=release-b')));
});

test('LCP: every bundle script is requested before any load event, with ordered execution and a CSS mount barrier', async () => {
  const L = lazyWorld({ cold: true });
  const pending = [];
  L.head.appendChild = n => { pending.push(n); return n; };
  let done = false;
  const load = L.w.run("Lazy.load('sessionrows')").then(() => { done = true; });
  await settle(() => pending.some(n => n.tagName === 'SCRIPT'));
  const scripts = pending.filter(n => n.tagName === 'SCRIPT');
  assert.deepEqual(scripts.map(n => n.src), ['/static/pages/agents.js', '/static/page-selection.js', '/static/session-groups.js']);
  assert.ok(scripts.every(n => n.async === false), 'browser downloads together but executes in insertion order');
  for (const script of scripts) { L.w.load(script.src.slice('/static/'.length)); script.onload(); }
  await settle(null, 2);
  assert.equal(done, false, 'exports alone cannot skip the pending stylesheet');
  pending.find(n => n.tagName === 'LINK').onload();
  await load;
  assert.equal(L.w.run("Lazy.done('sessionrows')"), true);
});

test('Home keeps the Team line above Nodes even when Team arrives first', async () => {
  const L = lazyWorld({ cold: true, hash: '#/' });
  L.w.run(`
    teamUse = () => {
      const r = homePage.refs;
      if (!r.team) {
        r.team = el('p', {class: 'tm-homeline', text: 'Team'});
        r.away.parentNode.insertBefore(r.team, r.away.nextSibling);
      }
      return true;
    };
    Nodes.use = fn => { if (typeof Nodes.slot === 'function') fn(); };
    route();
  `);
  assert.ok(await settle(() => L.mounted() === 'home'));
  await L.w.run("Lazy.load('nodeshub')");
  L.w.run('render(true); render(true)');
  const root = L.w.document.querySelector('#page');
  const team = root.querySelector('.tm-homeline'), strip = root.querySelector('.nd-strip');
  assert.ok(team && strip);
  assert.ok(root.children.indexOf(team) < root.children.indexOf(strip));
  assert.equal(root.querySelectorAll('.nd-strip').length, 1);
  L.w.run('takeDownPage()');
});

test('LCP: a parallel failure waits for outstanding members before Retry and reuses their successful exports', async () => {
  const L = lazyWorld({ cold: true });
  const pending = [];
  L.head.appendChild = n => { pending.push(n); return n; };
  let failed = false;
  const load = L.w.run("Lazy.load('sessionrows')").catch(() => { failed = true; });
  await settle(() => pending.some(n => n.tagName === 'SCRIPT'));
  pending.find(n => n.tagName === 'LINK').onerror();
  await settle(null, 2);
  assert.equal(failed, false, 'Retry must not overlap scripts from this attempt');
  const scripts = pending.filter(n => n.tagName === 'SCRIPT');
  for (const script of scripts) { L.w.load(script.src.slice('/static/'.length)); script.onload(); }
  await load;
  assert.equal(failed, true);
  assert.equal(L.w.run("Lazy.done('sessionrows')"), false);
  pending.length = 0;
  const retry = L.w.run("Lazy.load('sessionrows')");
  await settle(() => pending.length);
  assert.deepEqual(pending.map(n => n.tagName), ['LINK'], 'successful scripts are not executed twice');
  pending[0].onload();
  await retry;
  assert.equal(L.w.run("Lazy.done('sessionrows')"), true);
});
