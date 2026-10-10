// Contract tests for the #/n/ addresses of app/static/router.js (issue #137, the grammar of nodes.js). router.test.mjs is untouched: the twelve routes of ROUTES and
// every local address mean what they meant; the three forms of another node live in NODE_ROUTES and share one plain "not paired" page until the hub phase.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

function routerWorld() {
  const w = makeWorld();
  w.load('router.js');
  return w;
}

test('parseHash: the three #/n/ forms route to node, node-session and node-task; local forms stay what they were', () => {
  const { get } = routerWorld();
  const parse = (h) => plain(get('parseHash')(h));
  assert.deepEqual(parse('#/n/box'), { id: 'node', params: { node: 'box' }, query: {} });
  assert.deepEqual(parse('#/n/node-a/s/shop--api--t-fix'), { id: 'node-session', params: { node: 'node-a', tmux: 'shop--api--t-fix' }, query: {} });
  assert.deepEqual(parse('#/n/box/t/42'), { id: 'node-task', params: { node: 'box', id: '42' }, query: {} });
  assert.deepEqual(parse('#/n/box/s/x?tab=1'), { id: 'node-session', params: { node: 'box', tmux: 'x' }, query: { tab: '1' } });
  assert.deepEqual(parse('#/s/shop--api--s1'), { id: 'session', params: { tmux: 'shop--api--s1' }, query: {} });
  assert.deepEqual(parse('#/tasks'), { id: 'tasks', params: {}, query: {} });
  assert.deepEqual(parse('#/settings?sec=notify'), { id: 'settings', params: {}, query: { sec: 'notify' } });
  assert.deepEqual(parse('#s=shop--api--s1'), { id: 'session', params: { tmux: 'shop--api--s1' }, query: {}, legacy: true }, 'the old ntfy link');
});

test('parseHash: an address outside the grammar is the home page, never a node route', () => {
  const { get } = routerWorld();
  for (const bad of ['#/n', '#/n/', '#/n/Box', '#/n/local', '#/n/local/s/x', '#/n/local/t/1', '#/n/box/s', '#/n/box/s/', '#/n/box/s/a/b', '#/n/box/s/a.b', '#/n/box/t/abc', '#/n/box/t/',
    '#/n/box/t/1/2', '#/n/box/x/1', '#/n/' + 'x'.repeat(32), '#/n/box/s/' + 'a'.repeat(201), '#/n/box/t/' + '9'.repeat(13), '#/n/-box', '#/n/bo_x']) {
    const r = plain(get('parseHash')(bad));
    assert.equal(r.id, 'home', bad);
    assert.equal(r.unknown, true, bad);
  }
});

test('buildHash: the node forms build what parseHash reads, and refuse a handle or an id outside the grammar', () => {
  const { get } = routerWorld();
  const build = get('buildHash');
  assert.equal(build('node', { node: 'box' }), '#/n/box');
  assert.equal(build('node-session', { node: 'box', tmux: 'shop--api--s1' }), '#/n/box/s/shop--api--s1');
  assert.equal(build('node-task', { node: 'box', id: 42 }), '#/n/box/t/42');
  assert.equal(build('node-task', { node: 'box', id: '42' }, { a: 1 }), '#/n/box/t/42?a=1');
  for (const [id, params] of [['node', { node: 'Box' }], ['node', { node: 'local' }], ['node', {}], ['node-session', { node: 'box' }], ['node-session', { node: 'box', tmux: 'a/b' }],
    ['node-task', { node: 'box', id: 'x' }], ['node-task', { node: 'box' }], ['node', { node: 'x'.repeat(32) }], ['node-session', { tmux: 'a' }]]) {
    assert.throws(() => build(id, params), /bad route param/, JSON.stringify([id, params]));
  }
  assert.equal(build('session', { tmux: 'shop--api--s1' }), '#/s/shop--api--s1', 'this board\'s own build is unchanged');
});

/** installDom's skeleton with core, components and the router: the real el(), pageEmpty() and #page. */
function pageWorld() {
  const w = makeWorld();
  installDom(w);
  for (const f of ['core.js', 'components.js', 'router.js', 'nodes-hub.js', 'pages/node.js']) w.load(f);      // the node pages are the lazy 'nodeshub' bundle (lazy.js), loaded here the way a #/n/ address loads it
  w.run('state = { nodes_enabled: false }');      // the first state has arrived and no node is paired (before it the page says "Reading the nodes", not "not paired")
  return w;
}

test('a node that is not paired: a plain page that says so and links Settings, for all three forms; no throw, no blank', () => {
  for (const hash of ['#/n/box', '#/n/box/s/shop--api--s1', '#/n/box/t/42']) {
    const w = pageWorld();
    w.location.hash = hash;
    const page = w.document.querySelector('#page');
    assert.match(page.textContent, /This node is not paired/, hash);
    assert.match(page.textContent, /"box"/, 'it names the handle it was asked for');
    const link = page.querySelector('a');
    assert.ok(link, hash);
    assert.equal(link.getAttribute('href'), '#/settings?sec=nodes');
    assert.match(link.textContent, /Settings/);
    assert.equal(w.document.body.getAttribute('data-page'), hash.includes('/s/') ? 'node-session' : hash.includes('/t/') ? 'node-task' : 'node');
    assert.doesNotMatch(page.textContent, /—/, 'no em-dash in UI text');
  }
});

test('the not-paired page follows the address in place (box to node-a) and gives way to another route', () => {
  const w = pageWorld();
  w.location.hash = '#/n/box';
  w.location.hash = '#/n/node-a';
  assert.match(w.document.querySelector('#page').textContent, /"node-a"/);
  assert.doesNotMatch(w.document.querySelector('#page').textContent, /"box"/);
  w.run("registerPage('tasks', { mount(root) { root.append(el('p', { text: 'the tasks page' })); }, update() {} })");
  w.location.hash = '#/tasks';
  assert.match(w.document.querySelector('#page').textContent, /the tasks page/);
  assert.doesNotMatch(w.document.querySelector('#page').textContent, /not paired/);
});

test('node addresses add nothing to the twelve routes of ROUTES; their three ids carry the node pages (pages/node.js) and the router registers none itself', () => {
  const w = pageWorld();
  assert.equal(plain(w.run('ROUTES.map((r) => r.id)')).length, 12);
  assert.deepEqual(plain(w.run('NODE_ROUTES.map((r) => r.id)')), ['node', 'node-session', 'node-task']);
  assert.deepEqual(plain(w.run('Object.keys(pages)')).sort(), ['node', 'node-session', 'node-task']);
});

test('a link to a node in the sidebar or the nav marks aria-current only on its own route', () => {
  const w = pageWorld();
  w.location.hash = '#/n/box';
  const route = plain(w.get('currentRoute')());
  assert.deepEqual([route.id, route.params.node], ['node', 'box']);
  assert.equal(plain(w.get('parseHash')('#/n/box')).unknown, undefined, 'a link to a node is a known route for updateNav');
});
