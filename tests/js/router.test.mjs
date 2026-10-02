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
