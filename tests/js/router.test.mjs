// Contract tests for app/static/router.js (classic script): parseHash, buildHash, rewriteLegacyHash, page registry, route().
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';

const ROUTER = path.join(STATIC, 'router.js');

/** A fresh world with router.js loaded; fails with a clear message while router.js does not exist yet. */
function routerWorld(extra) {
  assert.ok(fs.existsSync(ROUTER), `router.js missing: expected ${ROUTER}`);
  const w = makeWorld(extra);
  w.load('router.js');
  return w;
}

test('router.js exists and defines the public surface', () => {
  const w = routerWorld();
  for (const name of ['ROUTES', 'parseHash', 'buildHash', 'registerPage', 'navigate', 'rewriteLegacyHash', 'currentRoute', 'route', 'pages']) {
    assert.notEqual(w.run(`typeof ${name}`), 'undefined', `${name} is not defined by router.js`);
  }
});

test('load-time side effects: only the hashchange listener', () => {
  const w = routerWorld();
  assert.deepEqual(Object.keys(w.window.listeners), ['hashchange']);
  assert.equal(w.window.listeners.hashchange.length, 1);
  assert.equal(Object.keys(w.document.listeners).length, 0);
  assert.equal(w.history.calls.length, 0);
});

test('parseHash: plain routes', () => {
  const { get } = routerWorld();
  const parse = (h) => plain(get('parseHash')(h));
  assert.deepEqual(parse('#/'), { id: 'home', params: {}, query: {} });
  assert.deepEqual(parse(''), { id: 'home', params: {}, query: {} });
  assert.deepEqual(parse('#'), { id: 'home', params: {}, query: {} });
  assert.deepEqual(parse('#/inbox'), { id: 'inbox', params: {}, query: {} });
  assert.deepEqual(parse('#/tasks'), { id: 'tasks', params: {}, query: {} });
  assert.deepEqual(parse('#/usage'), { id: 'usage', params: {}, query: {} });
  assert.deepEqual(parse('#/settings?sec=notify'), { id: 'settings', params: {}, query: { sec: 'notify' } });
  assert.deepEqual(parse('#/search?q=a%20b'), { id: 'search', params: {}, query: { q: 'a b' } });
  assert.deepEqual(parse('#/memory'), { id: 'memory', params: {}, query: {} });
  assert.deepEqual(parse('#/memory/shop'), { id: 'memory', params: { project: 'shop' }, query: {} });
  assert.deepEqual(parse('#/onboarding'), { id: 'onboarding', params: {}, query: {} });
  assert.deepEqual(parse('#/onboarding/project'), { id: 'onboarding', params: { step: 'project' }, query: {} });
});

test('parseHash: project, quad and session routes with queries', () => {
  const { get } = routerWorld();
  const parse = (h) => plain(get('parseHash')(h));
  assert.deepEqual(parse('#/p/shop'), { id: 'project', params: { project: 'shop' }, query: {} });
  assert.deepEqual(parse('#/p/shop/api?tab=files&path=src'),
    { id: 'project', params: { project: 'shop', repo: 'api' }, query: { tab: 'files', path: 'src' } });
  assert.deepEqual(parse('#/quad?s=a,b&l=4'), { id: 'quad', params: {}, query: { s: 'a,b', l: '4' } });
  assert.deepEqual(parse('#/s/shop--api--s1'), { id: 'session', params: { tmux: 'shop--api--s1' }, query: {} });
});

test('parseHash: legacy #s=<tmux> is a session route flagged legacy', () => {
  const { get } = routerWorld();
  const r = plain(get('parseHash')('#s=shop--api--s1'));
  assert.deepEqual(r, { id: 'session', params: { tmux: 'shop--api--s1' }, query: {}, legacy: true });
  assert.equal(plain(get('parseHash')('#s=bad/name')).unknown, true);          // not a tmux name: falls through to unknown
});

test('parseHash: unknown paths land on home and say so', () => {
  const { get } = routerWorld();
  const parse = (h) => plain(get('parseHash')(h));
  assert.deepEqual(parse('#/nope'), { id: 'home', params: {}, query: {}, unknown: true });
  for (const bad of ['#/p/a/b/c', '#/p/bad name', '#/s/a/b', '#/onboarding/other', '#/inbox/extra', '#/quad/1']) {
    const r = parse(bad);
    assert.equal(r.id, 'home', bad);
    assert.equal(r.unknown, true, bad);
  }
});

test('buildHash: shapes', () => {
  const { get } = routerWorld();
  const build = get('buildHash');
  assert.equal(build('home'), '#/');
  assert.equal(build('inbox'), '#/inbox');
  assert.equal(build('tasks'), '#/tasks');
  assert.equal(build('project', { project: 'shop' }), '#/p/shop');
  assert.equal(build('project', { project: 'shop', repo: 'api' }), '#/p/shop/api');
  assert.equal(build('project', { project: 'shop', repo: 'api' }, { tab: 'files', path: 'src' }), '#/p/shop/api?tab=files&path=src');
  assert.equal(build('session', { tmux: 'shop--api--s1' }), '#/s/shop--api--s1');
  assert.equal(build('memory'), '#/memory');
  assert.equal(build('memory', { project: 'shop' }), '#/memory/shop');
  assert.equal(build('onboarding'), '#/onboarding');
  assert.equal(build('onboarding', { step: 'project' }), '#/onboarding/project');
  assert.equal(build('quad', {}, { s: 'a,b', l: 4 }), '#/quad?s=a%2Cb&l=4');
  assert.equal(build('inbox', {}, {}), '#/inbox');
});

test('buildHash then parseHash round-trips', () => {
  const { get } = routerWorld();
  const build = get('buildHash');
  const parse = (h) => plain(get('parseHash')(h));
  const cases = [
    ['home', {}, {}], ['inbox', {}, {}], ['tasks', {}, {}], ['usage', {}, {}], ['settings', {}, { sec: 'push' }],
    ['project', { project: 'shop' }, {}], ['project', { project: 'shop', repo: 'api' }, { tab: 'files', path: 'src/a b.js' }],
    ['session', { tmux: 'shop--api--s1' }, {}], ['memory', {}, {}], ['memory', { project: 'shop' }, {}],
    ['onboarding', {}, {}], ['onboarding', { step: 'project' }, {}], ['quad', {}, { s: 'a,b,c,d', l: '4', p: 'shop' }],
    ['search', {}, { q: 'a&b=c d' }],
  ];
  for (const [id, params, query] of cases) {
    const h = build(id, params, query);
    assert.ok(h.startsWith('#/'), h);
    assert.deepEqual(parse(h), { id, params, query }, `${id} -> ${h}`);
  }
});

test('buildHash rejects params outside [A-Za-z0-9_-]', () => {
  const { get } = routerWorld();
  const build = get('buildHash');
  assert.throws(() => build('project', { project: 'bad/name' }), /bad route param/);
  assert.throws(() => build('project', { project: 'shop', repo: 'a b' }), /bad route param/);
  assert.throws(() => build('session', { tmux: '../etc' }), /bad route param/);
  assert.throws(() => build('memory', { project: 'x?y' }), /bad route param/);
  assert.throws(() => build('project', { project: 'shop#x' }), /bad route param/);
});

test('rewriteLegacyHash replaces #s=<tmux> through history.replaceState, in place', () => {
  const w = routerWorld();
  w.setHash('#s=shop--api--s1', { silent: true });
  assert.equal(w.get('rewriteLegacyHash')(), true);
  assert.equal(w.history.calls.length, 1);
  assert.equal(w.history.calls[0].method, 'replaceState');
  assert.equal(w.history.calls[0].args[2], '#/s/shop--api--s1');
  assert.equal(w.location.hash, '#/s/shop--api--s1');
  assert.equal(w.get('rewriteLegacyHash')(), false);                    // already rewritten: nothing to do
  assert.equal(w.history.calls.length, 1);
});

test('rewriteLegacyHash leaves every other hash alone', () => {
  const w = routerWorld();
  for (const h of ['', '#', '#/', '#/inbox', '#/s/x', '#s=bad/name', '#s=', '#x=1']) {
    w.setHash(h, { silent: true });
    assert.equal(w.get('rewriteLegacyHash')(), false, JSON.stringify(h));
    assert.equal(w.location.hash, h);
  }
  assert.equal(w.history.calls.length, 0);
});

test('route() with no registered pages is a no-op (the legacy single page stays)', () => {
  const w = routerWorld();
  w.setHash('#/inbox', { silent: true });
  assert.doesNotThrow(() => w.get('route')());
  assert.equal(w.get('currentRoute')().id, 'inbox');
  assert.equal(w.history.calls.length, 0);
  // a page registered for another id does not hijack this route either
  const mounted = [];
  w.get('registerPage')('tasks', { mount: (root, r) => mounted.push(r.id), update() {} });
  w.get('route')();
  assert.deepEqual(mounted, []);
  // and a hashchange with nothing to mount does not throw
  w.document.nodes['#page'] = null;
  assert.doesNotThrow(() => w.fire('hashchange'));
});

test('registerPage + route: mounts into #page, hands over state, unmounts the previous page', () => {
  const w = routerWorld();
  const log = [];
  const root = w.document.createElement('div');
  root.textContent = 'old content';
  w.document.nodes['#page'] = root;
  w.run('var state = { marker: 42 };');
  const register = w.get('registerPage');
  register('inbox', {
    mount(r, route) { log.push(['mount inbox', r === root, route.id, root.textContent]); },
    update(st, route) { log.push(['update inbox', st.marker, route.id]); },
    unmount() { log.push(['unmount inbox']); },
  });
  register('tasks', { mount(_r, route) { log.push(['mount tasks', route.id]); }, update() { log.push(['update tasks']); } });
  w.location.hash = '#/inbox';                                 // fires hashchange -> route()
  assert.deepEqual(log, [['mount inbox', true, 'inbox', ''], ['update inbox', 42, 'inbox']]);   // #page was cleared before mount
  log.length = 0;
  w.location.hash = '#/tasks';
  assert.deepEqual(log, [['unmount inbox'], ['mount tasks', 'tasks'], ['update tasks']]);
  assert.equal(w.get('currentRoute')().id, 'tasks');
});

test('navigate: replace goes through history.replaceState and routes; plain sets location.hash', () => {
  const w = routerWorld();
  w.get('navigate')('#/inbox', { replace: true });
  assert.equal(w.history.calls.length, 1);
  assert.deepEqual([w.history.calls[0].method, w.history.calls[0].args[2]], ['replaceState', '#/inbox']);
  assert.equal(w.get('currentRoute')().id, 'inbox');
  w.get('navigate')('#/quad?l=2');
  assert.equal(w.location.hash, '#/quad?l=2');
  assert.equal(w.get('currentRoute')().id, 'quad');            // the simulated hashchange ran route()
  assert.equal(w.history.calls.length, 1);
});

// ---------- v0.5.3 contract: the agents route, the twelve ids, route() lifecycle with stub pages, the service worker nav message ----------
// Written against the v0.5.3 router contract. A failure that says "contract" means router.js has not caught up with it yet.

const ROUTE_IDS = ['agents', 'home', 'inbox', 'memory', 'onboarding', 'project', 'quad', 'search', 'session', 'settings', 'tasks', 'usage'];

/** routerWorld() plus the DOM the contract route() touches: body.dataset, #main (focus), #drawer (close), a #page root, scrollTo. */
function shellWorld(extra) {
  const w = routerWorld(extra);
  const node = () => Object.assign(w.document.createElement('div'), { focus() {}, close() {}, showModal() {}, scrollTo() {}, scrollTop: 0 });
  w.document.body.dataset = {};
  for (const sel of ['#main', '#drawer', '#sheet', '#banner']) w.document.nodes[sel] = node();
  const page = node();
  page.textContent = 'stale content';
  w.document.nodes['#page'] = page;
  w.run('globalThis.scrollTo = () => {};');
  return { w, page };
}

const bodyPage = (w) => w.document.body.dataset.page ?? w.document.body.attrs['data-page'];

/** Registers a logging stub for each id; `withOnRoute` ids also get onRoute. */
function stubPages(w, log, ids, { withOnRoute = [], titles = {} } = {}) {
  const register = w.get('registerPage');
  for (const id of ids) {
    const page = {
      title: titles[id] ?? id,
      mount(root, route) { log.push(['mount', id, root.textContent, route.id]); },
      update(st, route) { log.push(['update', id, st && st.marker, route.id]); },
      unmount() { log.push(['unmount', id]); },
    };
    if (withOnRoute.includes(id)) page.onRoute = (route) => log.push(['onRoute', id, JSON.stringify(route.params), JSON.stringify(route.query)]);
    register(id, page);
  }
}

test('parseHash and buildHash know the agents roster route', () => {
  const { get } = routerWorld();
  const parse = (h) => plain(get('parseHash')(h));
  assert.deepEqual(parse('#/agents'), { id: 'agents', params: {}, query: {} });
  assert.deepEqual(parse('#/agents?x=1'), { id: 'agents', params: {}, query: { x: '1' } });
  assert.equal(get('buildHash')('agents'), '#/agents');
  assert.deepEqual(parse(get('buildHash')('agents')), { id: 'agents', params: {}, query: {} });
  for (const bad of ['#/agents/', '#/agents/x', '#/agent']) {
    assert.equal(parse(bad).unknown, true, bad);
    assert.equal(parse(bad).id, 'home', bad);
  }
});

test('ROUTES holds exactly the twelve route ids, each once', () => {
  const w = routerWorld();
  const ids = plain(w.run('ROUTES.map((r) => r.id)'));
  assert.equal(new Set(ids).size, ids.length, `duplicate ids in ${ids}`);
  assert.deepEqual([...ids].sort(), ROUTE_IDS);
});

test('route(): mount into a cleared #page, then update with state; the next page is preceded by unmount of the previous one', () => {
  const { w, page } = shellWorld();
  w.run('var state = { marker: 7 };');
  const log = [];
  stubPages(w, log, ['inbox', 'settings', 'home']);
  w.location.hash = '#/inbox';
  assert.deepEqual(log, [['mount', 'inbox', '', 'inbox'], ['update', 'inbox', 7, 'inbox']], 'contract: #page is cleared before mount; update(state, route) follows mount');
  assert.equal(w.get('currentRoute')().id, 'inbox');
  log.length = 0;
  page.textContent = 'left behind by the inbox page';
  w.location.hash = '#/settings?sec=notify';
  assert.deepEqual(log, [['unmount', 'inbox'], ['mount', 'settings', '', 'settings'], ['update', 'settings', 7, 'settings']]);
  log.length = 0;
  w.location.hash = '#/';
  assert.deepEqual(log, [['unmount', 'settings'], ['mount', 'home', '', 'home'], ['update', 'home', 7, 'home']]);
});

test('route(): document.title becomes "<page title> · ccboard" and body[data-page] the route id', () => {
  const { w } = shellWorld();
  w.run('var state = { marker: 1 };');
  const log = [];
  stubPages(w, log, ['inbox', 'settings'], { titles: { inbox: 'Needs you', settings: 'Settings' } });
  w.location.hash = '#/inbox';
  assert.equal(w.document.title, 'Needs you · ccboard');
  assert.equal(bodyPage(w), 'inbox', 'contract: route() sets body[data-page]');
  w.location.hash = '#/settings?sec=box';
  assert.equal(w.document.title, 'Settings · ccboard');
  assert.equal(bodyPage(w), 'settings');
});

test('route(): the agents roster page mounts at #/agents', () => {
  const { w } = shellWorld();
  w.run('var state = { marker: 2 };');
  const log = [];
  stubPages(w, log, ['agents', 'home'], { titles: { agents: 'Agents' } });
  w.location.hash = '#/agents';
  assert.deepEqual(log, [['mount', 'agents', '', 'agents'], ['update', 'agents', 2, 'agents']], 'contract: ROUTES has the agents id (re /^\\/agents$/)');
  assert.equal(w.document.title, 'Agents · ccboard');
});

test('route(): update is skipped while no state has arrived, and an unknown hash mounts home', () => {
  const { w } = shellWorld();
  const log = [];
  stubPages(w, log, ['home', 'inbox']);
  w.location.hash = '#/nope';
  assert.deepEqual(log, [['mount', 'home', '', 'home']], 'unknown routes land on home; no state yet means no update call');
  log.length = 0;
  w.run('var state = { marker: 1 };');
  w.location.hash = '#/inbox';
  assert.deepEqual(log, [['unmount', 'home'], ['mount', 'inbox', '', 'inbox'], ['update', 'inbox', 1, 'inbox']]);
});

test('route(): same id with only the query changed calls onRoute, not unmount/mount', () => {
  const { w } = shellWorld();
  w.run('var state = { marker: 3 };');
  const log = [];
  stubPages(w, log, ['settings'], { withOnRoute: ['settings'] });
  w.location.hash = '#/settings?sec=notify';
  log.length = 0;
  w.location.hash = '#/settings?sec=nodes';
  assert.deepEqual(log, [['onRoute', 'settings', '{}', '{"sec":"nodes"}']], 'contract: same id + onRoute -> onRoute(route) instead of remounting');
  assert.deepEqual(plain(w.get('currentRoute')()), { id: 'settings', params: {}, query: { sec: 'nodes' } });
});

test('route(): same id with changed params calls onRoute too (the session peek keeps its panel when #/s/<a> becomes #/s/<b>)', () => {
  const { w } = shellWorld();
  w.run('var state = { marker: 3 };');
  const log = [];
  stubPages(w, log, ['session'], { withOnRoute: ['session'] });
  w.location.hash = '#/s/shop--api--s1';
  log.length = 0;
  w.location.hash = '#/s/shop--api--s2';
  assert.deepEqual(log, [['onRoute', 'session', '{"tmux":"shop--api--s2"}', '{}']],
    'contract: "same id with only params/query changed and the page has onRoute -> call onRoute instead of remounting"');
});

test('route(): a page without onRoute is remounted on a same-id navigation (unmount, clear, mount, update)', () => {
  const { w, page } = shellWorld();
  w.run('var state = { marker: 5 };');
  const log = [];
  stubPages(w, log, ['search']);
  w.location.hash = '#/search?q=a';
  log.length = 0;
  page.textContent = 'results of the first query';
  w.location.hash = '#/search?q=b';
  assert.deepEqual(log, [['unmount', 'search'], ['mount', 'search', '', 'search'], ['update', 'search', 5, 'search']]);
});

test('route(): moving from a page with onRoute to another id still unmounts it, and coming back mounts afresh', () => {
  const { w } = shellWorld();
  w.run('var state = { marker: 9 };');
  const log = [];
  stubPages(w, log, ['settings', 'tasks'], { withOnRoute: ['settings'] });
  w.location.hash = '#/settings';
  w.location.hash = '#/tasks';
  w.location.hash = '#/settings?sec=box';
  assert.deepEqual(log.map((e) => e.slice(0, 2)), [
    ['mount', 'settings'], ['update', 'settings'], ['unmount', 'settings'], ['mount', 'tasks'], ['update', 'tasks'],
    ['unmount', 'tasks'], ['mount', 'settings'], ['update', 'settings']]);
});

test('the service worker nav message {type:"nav", url} sets location.hash', (t) => {
  const handlers = [];
  const serviceWorker = { addEventListener(type, fn) { handlers.push([type, fn]); }, removeEventListener() {} };
  const w = routerWorld({ navigator: { userAgent: 'node-test', onLine: true, serviceWorker } });
  const message = handlers.find(([type]) => type === 'message');
  if (!message) {
    t.skip('router.js adds no serviceWorker "message" listener at load time (if it does so from an init function this test cannot reach it)');
    return;
  }
  message[1]({ data: { type: 'nav', url: '/#/inbox' } });
  assert.equal(w.location.hash, '#/inbox');
  message[1]({ data: { type: 'nav', url: '/#/s/shop--api--s1' } });
  assert.equal(w.location.hash, '#/s/shop--api--s1');
  message[1]({ data: { type: 'other', url: '/#/tasks' } });
  assert.equal(w.location.hash, '#/s/shop--api--s1', 'only type nav messages move the hash');
});
