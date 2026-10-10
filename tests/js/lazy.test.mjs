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
function lazyWorld({ fine = true, fail = [], state = fixtureState(), hash = null, slow = 0, realCreate = false } = {}) {
  const w = makeWorld({ companions: false, matchMedia: (q) => ({ matches: fine && /pointer:\s*fine/.test(q), addEventListener() {}, removeEventListener() {} }) });
  const dom = installDom(w);
  const doc = w.document;
  const head = new dom.El('head');
  doc.documentElement.insertBefore(head, doc.body);
  doc.head = head;
  const charts = new dom.El('link');
  charts.setAttribute('rel', 'stylesheet');
  charts.setAttribute('href', '/static/charts.css');
  head.append(charts);
  const find = doc.querySelector;
  doc.querySelector = (s) => find(s) || head.querySelector(s);
  const net = { scripts: [], sheets: [], fail: new Set(fail) };
  const onInsert = (n) => {
    if (n.tagName === 'SCRIPT') {
      const src = n.src;
      net.scripts.push(src);
      (slow ? (f) => setTimeout(f, slow) : setImmediate)(() => {
        if (net.fail.has(src)) { n.onerror(); return; }
        w.load(src.slice('/static/'.length));
        n.onload();
      });
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
  for (const src of INDEX_SCRIPTS) if (src !== '/static/main.js') w.load(src.slice('/static/'.length));
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

const LAZY_GLOBALS = ['agentsPage', 'Tree', 'Dnd', 'Palette', 'TermKit', 'Charts', 'Memory', 'Usage', 'Quad', 'projectPage', 'settingsPage', 'doctorStore', 'wizPage', 'launcherSchema'];

test('index.html loads only the shell set: no lazy script is in it, and with just those files nothing lazy is defined', () => {
  const lazy = JSON.parse(JSON.stringify([]));
  const src = fs.readFileSync(path.join(STATIC, 'lazy.js'), 'utf8');
  for (const file of src.matchAll(/'(\/static\/[^']+\.js)'/g)) {
    assert.ok(!INDEX_SCRIPTS.includes(file[1]), `${file[1]} is a lazy bundle but index.html loads it`);
    lazy.push(file[1]);
  }
  assert.ok(lazy.length >= 12, 'the manifest was found');
  const L = lazyWorld();
  for (const name of LAZY_GLOBALS) assert.equal(L.w.run(`typeof ${name}`), 'undefined', `${name} is not defined by the first-paint set`);
  assert.equal(L.w.run('typeof openLauncher'), 'function', 'the launcher entry points are stubs');
  assert.equal(L.w.run('typeof Lazy'), 'object');
  assert.deepEqual(L.net.scripts, [], 'loading the shell set fetched nothing');
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
  { hash: '#/usage', id: 'usage', scripts: ['/static/charts.js', '/static/pages/usage.js'], sheets: [] },
  { hash: '#/settings', id: 'settings', scripts: ['/static/pages/agents-page.js', '/static/nodes-pair.js', '/static/pages/doctor.js', '/static/pages/settings.js'], sheets: ['/static/pages/settings.css'] },
  { hash: '#/memory', id: 'memory', scripts: ['/static/pages/memory.js'], sheets: ['/static/pages/memory.css'] },
  { hash: '#/quad', id: 'quad', scripts: ['/static/termkit.js', '/static/pages/quad.js'], sheets: ['/static/pages/quad.css'] },
  { hash: '#/p/ccboard', id: 'project', scripts: ['/static/tree.js', '/static/pages/project.js'], sheets: [] },
  { hash: '#/onboarding/project', id: 'onboarding', scripts: ['/static/launcher.js', '/static/launcher-node.js', '/static/pages/onboarding.js'], sheets: ['/static/pages/onboarding.css'] },
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

test('page sheets go in before charts.css, in the order pages.css had their rules, whichever page was opened first', async () => {
  const L = lazyWorld();
  for (const h of ['#/memory', '#/quad', '#/settings']) {
    L.w.location.hash = h;
    assert.ok(await settle(() => L.mounted() === h.slice(2)), `${h} mounted ${L.mounted()} ${JSON.stringify(L.net.scripts)}`);
  }
  assert.deepEqual(L.hrefs(), ['/static/pages/settings.css', '/static/pages/quad.css', '/static/pages/memory.css', '/static/charts.css']);
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
  assert.deepEqual(L.hrefs(), ['/static/charts.css'], 'the failed sheet is not left in the head');
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
  assert.ok(await settle(() => L.mounted() === 'settings', 100000));
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
  assert.equal(L.w.run('openLauncher({ mode: "session" })'), true);
  assert.equal(L.w.run('taskDispatchSheet({ id: 1 })'), true);
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
  assert.deepEqual(L.net.sheets, ['/static/pages/onboarding.css']);
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
  assert.deepEqual(L.net.scripts, ['/static/tree.js', '/static/pages/project.js', '/static/pages/memory.js']);
  assert.deepEqual(L.net.sheets, ['/static/pages/memory.css']);
  const bare = lazyWorld({ state: fixtureState() });
  bare.w.location.hash = '#/p/ccboard';
  assert.ok(await settle(() => bare.mounted() === 'project'));
  await settle(null, 30);
  assert.deepEqual(bare.net.scripts, ['/static/tree.js', '/static/pages/project.js']);
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
  assert.equal(S.net.scripts[0], '/static/pages/agents-page.js', 'settings needs the agents bundle first');
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
