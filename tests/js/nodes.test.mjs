// Contract tests for app/static/nodes.js (the Ref helpers of issue #137): the grammar of a handle, a session ref and a task ref, the round trips, the one place keys
// are built (Ref.key / Ref.storeKey) and the collision contract: the same tmux name on this board and on a node 'box' is two different sidebar, DOM, quad slot,
// quick-reply and launcher-default keys, two drop targets and two palette rows, while every key of a local item is byte for byte what it was.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { fakeState, fixtureState, homeWorld, tick } from './world.mjs';

const NAME = 'shop--api--t-fix';

/** A world with nodes.js (makeWorld runs it, as index.html does right after core.js). */
function refWorld() {
  const w = makeWorld();
  return { w, Ref: w.get('Ref') };
}

const same = (a, b) => assert.deepEqual(plain(a), b);

// ---------------------------------------------------------------- the file itself

test('nodes.js is a classic script in index.html right after core.js, ahead of everything that builds a key', () => {
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  const srcs = [...html.matchAll(/<script defer src="([^"]+)"/g)].map((m) => m[1]);
  const at = srcs.indexOf('/static/nodes.js');
  assert.equal(at, srcs.indexOf('/static/core.js') + 1, 'nodes.js directly after core.js');
  for (const later of ['components.js', 'live.js', 'shell.js', 'router.js', 'pages/quad.js']) {
    const i = srcs.indexOf('/static/' + later);
    assert.ok(i < 0 || i > at, `${later} loads after nodes.js`);
  }
});

test('nodes.js: no innerHTML, no inline style, no cssText, no storage, no network, no timer', () => {
  const code = fs.readFileSync(path.join(STATIC, 'nodes.js'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  for (const bad of [/innerHTML|insertAdjacentHTML|outerHTML/, /\.style\b|style=|cssText/, /localStorage|sessionStorage|indexedDB/, /\bfetch\(|XMLHttpRequest|EventSource|WebSocket/, /setTimeout|setInterval|requestAnimationFrame/, /\bdocument\b|\bwindow\b/]) {
    assert.doesNotMatch(code, bad);
  }
});

// ---------------------------------------------------------------- the grammar

test('handle: [a-z0-9][a-z0-9-]{0,30}, and local is reserved', () => {
  const { Ref } = refWorld();
  for (const ok of ['box', 'node-a', 'a', '0', '9lives', 'a-b-c', 'x'.repeat(31)]) assert.equal(Ref.isHandle(ok), true, ok);
  for (const bad of ['', 'Box', 'BOX', '-box', 'bo x', 'bo_x', 'box.', 'bo/x', 'x'.repeat(32), 'local', null, undefined, 4, 'é']) assert.equal(Ref.isHandle(bad), false, String(bad));
});

test('Ref.parse accepts the text forms and the addresses', () => {
  const { Ref } = refWorld();
  const p = (t) => plain(Ref.parse(t));
  assert.deepEqual(p('box/' + NAME), { kind: 'session', node: 'box', tmux: NAME });
  assert.deepEqual(p('box:42'), { kind: 'task', node: 'box', id: '42' });
  assert.deepEqual(p('#/n/box'), { kind: 'node', node: 'box' });
  assert.deepEqual(p('#/n/box/s/' + NAME), { kind: 'session', node: 'box', tmux: NAME });
  assert.deepEqual(p('#/n/box/t/42'), { kind: 'task', node: 'box', id: '42' });
  assert.deepEqual(p('/n/box/s/' + NAME), { kind: 'session', node: 'box', tmux: NAME }, 'the # is optional');
  assert.deepEqual(p('#/s/' + NAME), { kind: 'session', node: null, tmux: NAME }, 'a local address is a local ref');
  assert.deepEqual(p('#/t/7'), { kind: 'task', node: null, id: '7' });
});

test('Ref.parse refuses everything outside the grammar, and never throws', () => {
  const { Ref } = refWorld();
  const bad = [
    'box/../x', 'box/a/b', 'Box/x', 'box:abc', 'box:', ':42', '/x', 'box/', '', 'x'.repeat(32) + '/x', 'x'.repeat(32) + ':1',
    'local/' + NAME, 'local:1', '#/n/local', '#/n/local/s/' + NAME, '#/n/local/t/1',
    'box/bad name', 'box/a.b', 'box/%2e%2e', 'box:1:2', 'box:-1', 'box:1.5', 'box:0x10', 'box:' + '9'.repeat(13), 'box/' + 'a'.repeat(201),
    '#/n/Box/s/x', '#/n/box/s/', '#/n/box/s/a/b', '#/n/box/s/..', '#/n/box/t/abc', '#/n/box/t/', '#/n/box/x/1', '#/n/box/s/x?y=1', '#/n/', '#/n', '#/n//s/x',
    'box/x', 'box/shop--api', 'box/a--b--c--d', 'box/--b--c', 'box/a----c', 'box/-a--b--c', 'box/a--b--c-', 'box/' + ['a'.repeat(65), 'b', 'c'].join('--'), '#/n/box/s/x',     // not a ccboard session name (app/tmux.py valid_name / split_name)
    '#/s/a/b', '#/t/abc', NAME, 'shop', '#' + 'x'.repeat(400),
    null, undefined, 5, {}, [], true,
  ];
  for (const t of bad) assert.equal(Ref.parse(t), null, String(t).slice(0, 40));
});

test('the constructors check their parts; a handle of null, "" or "local" is this board', () => {
  const { Ref } = refWorld();
  assert.deepEqual(plain(Ref.session('box', NAME)), { kind: 'session', node: 'box', tmux: NAME });
  for (const none of [null, undefined, '', 'local']) assert.equal(Ref.session(none, NAME).node, null);
  assert.equal(Ref.session('Box', NAME), null);
  assert.equal(Ref.session('box', 'a/b'), null);
  assert.equal(Ref.session('box', '..'), null);
  assert.equal(Ref.session('box', ''), null);
  assert.deepEqual(plain(Ref.task('box', 42)), { kind: 'task', node: 'box', id: '42' });
  assert.equal(Ref.task('box', 'x'), null);
  assert.equal(Ref.task('box', ''), null);
  assert.equal(Ref.node('local'), null, 'there is no node page for this board');
  assert.equal(Ref.node('box').node, 'box');
  assert.equal(Object.isFrozen(Ref.session('box', NAME)), true);
});

// ---------------------------------------------------------------- round trips and the local rule

test('Ref.hash(Ref.parse(x)) is x for every accepted address; Ref.text(Ref.parse(x)) is x for every accepted text form', () => {
  const { Ref } = refWorld();
  for (const h of ['#/n/box', '#/n/box/s/' + NAME, '#/n/box/t/42', '#/n/node-a/s/ccboard--ccboard--s1', '#/s/' + NAME, '#/t/42', '#/n/a/t/0']) assert.equal(Ref.hash(Ref.parse(h)), h, h);
  for (const t of ['box/' + NAME, 'box:42', 'node-a/ccboard--ccboard--s1']) assert.equal(Ref.text(Ref.parse(t)), t, t);
  assert.equal(Ref.text(Ref.parse('#/n/box')), 'box');
});

test('local addresses are byte for byte today\'s: #/s/<tmux>, never #/n/ and never local/', () => {
  const { Ref } = refWorld();
  assert.equal(Ref.hash(Ref.session(null, NAME)), '#/s/' + NAME);
  assert.equal(Ref.hash({ tmux: NAME }), '#/s/' + NAME);
  assert.equal(Ref.hash({ tmux: NAME, node: 'local' }), '#/s/' + NAME);
  assert.equal(Ref.hash(Ref.task(null, 7)), '#/t/7');
  assert.equal(Ref.hash({ tmux: NAME, node: 'box' }), '#/n/box/s/' + NAME);
  assert.equal(Ref.text(Ref.session(null, NAME)), NAME);
  assert.equal(Ref.hash({ tmux: 'a b' }), null, 'a name outside the grammar has no address');
  assert.equal(Ref.hash({ tmux: NAME, node: 'Box' }), null);
  assert.equal(Ref.hash(null), null);
  assert.equal(Ref.hash(Ref.node('box')), '#/n/box');
});

test('Ref.hash agrees with the router: parseHash gives the route of the same thing, buildHash gives the same address', () => {
  const w = makeWorld();
  w.load('router.js');
  const { Ref } = { Ref: w.get('Ref') };
  const parse = (h) => plain(w.get('parseHash')(h));
  assert.deepEqual(parse(Ref.hash(Ref.session(null, NAME))), { id: 'session', params: { tmux: NAME }, query: {} });
  assert.deepEqual(parse(Ref.hash(Ref.session('box', NAME))), { id: 'node-session', params: { node: 'box', tmux: NAME }, query: {} });
  assert.deepEqual(parse(Ref.hash(Ref.task('box', 42))), { id: 'node-task', params: { node: 'box', id: '42' }, query: {} });
  assert.deepEqual(parse(Ref.hash(Ref.node('box'))), { id: 'node', params: { node: 'box' }, query: {} });
  const build = w.get('buildHash');
  assert.equal(build('node-session', { node: 'box', tmux: NAME }), Ref.hash(Ref.session('box', NAME)));
  assert.equal(build('node-task', { node: 'box', id: '42' }), Ref.hash(Ref.task('box', 42)));
  assert.equal(build('node', { node: 'box' }), Ref.hash(Ref.node('box')));
  assert.equal(build('session', { tmux: NAME }), Ref.hash(Ref.session(null, NAME)));
});

// ---------------------------------------------------------------- keys

test('Ref.key: the bare name for this board (whatever the name), <handle>/<name> for a node; tasks <id> and <handle>:<id>', () => {
  const { Ref } = refWorld();
  assert.equal(Ref.key({ tmux: NAME }), NAME);
  assert.equal(Ref.key({ tmux: NAME, node: null }), NAME);
  assert.equal(Ref.key({ tmux: NAME, node: 'local' }), NAME);
  assert.equal(Ref.key(NAME), NAME, 'a bare string is this board\'s name');
  assert.equal(Ref.key({ tmux: 'odd name!' }), 'odd name!', 'a local row is never reshaped, valid name or not');
  assert.equal(Ref.key({ tmux: NAME, node: 'box' }), 'box/' + NAME);
  assert.equal(Ref.key(Ref.session('box', NAME)), 'box/' + NAME);
  assert.equal(Ref.key(Ref.task(null, 7)), '7');
  assert.equal(Ref.key(Ref.task('box', 7)), 'box:7');
  assert.equal(Ref.key(Ref.node('box')), 'box');
  assert.equal(Ref.key(null), '');
  assert.equal(Ref.key({}), '');
  assert.notEqual(Ref.key({ tmux: NAME, node: 'strange name' }), NAME, 'a node that is no handle never folds into the local key');
});

test('Ref.storeKey: the prefix as it is plus the key; a local item keeps the key it always had', () => {
  const { Ref } = refWorld();
  assert.equal(Ref.storeKey('ccboard:quick:', { tmux: NAME }), 'ccboard:quick:' + NAME);
  assert.equal(Ref.storeKey('ccboard:quick:', { tmux: NAME, node: 'box' }), 'ccboard:quick:box/' + NAME);
  assert.equal(Ref.scoped(null, 'shop'), 'shop');
  assert.equal(Ref.scoped('box', 'shop'), 'box/shop');
  assert.equal(Ref.storeKey('ccboard:launch:', { tmux: Ref.scoped('box', 'shop') + '/api' }), 'ccboard:launch:box/shop/api');
});

test('Ref.splitKey / slotValue / slotKey: the quad store form, local unchanged', () => {
  const { Ref } = refWorld();
  same(Ref.splitKey(NAME), { node: null, tmux: NAME });
  same(Ref.splitKey('box/' + NAME), { node: 'box', tmux: NAME });
  for (const bad of ['', 'Box/x', 'box/', 'box/a/b', 'local/x', '/x', null, 4]) assert.equal(Ref.splitKey(bad), null, String(bad));
  assert.equal(Ref.slotValue(NAME), NAME);
  same(Ref.slotValue('box/' + NAME), { node: 'box', tmux: NAME });
  assert.equal(Ref.slotValue('Box/x'), '');
  assert.equal(Ref.slotKey(NAME), NAME);
  assert.equal(Ref.slotKey({ node: 'box', tmux: NAME }), 'box/' + NAME);
  for (const bad of [{ node: 'Box', tmux: NAME }, { node: 'box' }, { node: 'box', tmux: 'a/b' }, { node: 'local', tmux: NAME }, {}, null, 4, 'Box/x', ['box', NAME]]) assert.equal(Ref.slotKey(bad), '', JSON.stringify(bad));
});

// ---------------------------------------------------------------- the collision contract

const REMOTE = { node: 'box' };

/** Two sessions with one tmux name: this board's and the node box's, in the same project and repo (the shape a merged roster would have). */
function twinState() {
  const st = fixtureState();
  const repo = st.projects.find((p) => p.name === 'ccboard').repos.find((r) => r.name === 'ccboard');
  const local = repo.sessions.find((s) => s.tmux === 'ccboard--ccboard--s1');
  const remote = { ...local, ...REMOTE };
  repo.sessions.push(remote);
  return { st, local, remote, tmux: local.tmux };
}

/** The scripts the keys pass through, in index.html order, without a shell skeleton that needs a network. */
function keyWorld() {
  const w = makeWorld();
  installDom(w);
  for (const f of ['core.js', 'components.js', 'live.js', 'launcher.js', 'palette.js', 'tree.js', 'router.js', 'pages/widgets.js', 'shell.js', 'dnd.js', 'pages/agents.js', 'pages/quad.js']) w.load(f);
  return w;
}

test('the same tmux name on this board and on a node: the sidebar, DOM, quad slot, quick-reply and launcher-default keys are all different strings', () => {
  const w = keyWorld();
  const Ref = w.get('Ref');
  const { local, remote, tmux } = twinState();
  const item = (s) => plain(w.get('Shell').sessionItem(s, 'ccboard'));
  const sidebar = [item(local).key, item(remote).key];
  const dom = [Ref.key(local), Ref.key(remote)];
  const slot = [JSON.stringify(Ref.slotValue(Ref.key(local))), JSON.stringify(Ref.slotValue(Ref.key(remote)))];
  const quick = [w.get('quickKey')(local), w.get('quickKey')(remote)];
  const lx = w.get('LX_KEY');
  const launcher = [lx('ccboard', 'ccboard', 'claude'), lx(Ref.scoped('box', 'ccboard'), 'ccboard', 'claude')];
  for (const [label, pair] of Object.entries({ sidebar, dom, slot, quick, launcher })) {
    assert.notEqual(pair[0], pair[1], `${label}: one name on two nodes is two keys`);
    assert.equal(new Set(pair).size, 2, label);
  }
  // and the local ones are what they were before nodes
  assert.equal(sidebar[0], 's:' + tmux);
  assert.equal(dom[0], tmux);
  assert.equal(slot[0], JSON.stringify(tmux));
  assert.equal(quick[0], 'ccboard:quick:' + tmux);
  assert.equal(quick[1], 'ccboard:quick:box/' + tmux);
  assert.equal(launcher[0], 'ccboard:launch:ccboard/ccboard:claude');
  assert.equal(launcher[1], 'ccboard:launch:box/ccboard/ccboard:claude');
});

test('the quick-reply key of a roster row, a ref and a bare name agree for this board; the list of a node\'s session is its own', () => {
  const w = keyWorld();
  const Ref = w.get('Ref');
  const qk = w.get('quickKey');
  assert.equal(qk(NAME), 'ccboard:quick:' + NAME);
  assert.equal(qk({ tmux: NAME }), qk(NAME));
  assert.equal(qk(Ref.session(null, NAME)), qk(NAME));
  assert.equal(qk(Ref.session('box', NAME)), 'ccboard:quick:box/' + NAME);
  w.run(`quickSave({ tmux: ${JSON.stringify(NAME)}, node: 'box' }, ['only on box'], 'claude')`);
  assert.deepEqual(plain(w.run(`quickLoad(${JSON.stringify(NAME)}, 'claude')`)), plain(w.run("quickDefaults('claude')")), 'the local list is untouched');
  assert.deepEqual(plain(w.run(`quickLoad({ tmux: ${JSON.stringify(NAME)}, node: 'box' }, 'claude')`)), ['only on box']);
});

test('the sidebar: two rows for the one name, each with its own address and drop-target attributes; the local row is unchanged', () => {
  const w = keyWorld();
  const { st, tmux } = twinState();
  const Shell = w.get('Shell');
  const items = plain(Shell.model(st).main.concat(Shell.model(st).older).flatMap((p) => p.kids)).filter((k) => k.kind === 'sess' && k.tmux === tmux);
  assert.equal(items.length, 2);
  assert.deepEqual(items.map((k) => k.key).sort(), ['s:box/' + tmux, 's:' + tmux].sort());
  const row = (k) => { const n = Shell.kidNode(k); Shell.patchKid(n, k); return n; };
  const [a, b] = [items.find((k) => !k.node), items.find((k) => k.node)].map(row);
  assert.equal(a.getAttribute('href'), '#/s/' + tmux);
  assert.equal(a.getAttribute('data-tmux'), tmux);
  assert.equal(a.getAttribute('data-node'), null, 'this board\'s row carries no data-node');
  assert.equal(b.getAttribute('href'), '#/n/box/s/' + tmux);
  assert.equal(b.getAttribute('data-tmux'), tmux);
  assert.equal(b.getAttribute('data-node'), 'box');
});

test('drop targets: one name on two nodes is two targets; the node\'s refuses a drop, so a prompt never lands on this board\'s session of that name', () => {
  const w = keyWorld();
  const { st, tmux } = twinState();
  w.run('state = ' + JSON.stringify(st));
  const Dnd = w.get('Dnd');
  const idx = Dnd.index(st);
  const keys = [...idx.keys()].filter((k) => k.endsWith(tmux));
  assert.deepEqual(keys.sort(), [tmux, 'box/' + tmux].sort(), 'both sessions are in the index');
  const mk = (attrs) => { const n = w.document.createElement('div'); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
  const here = plain(Dnd.targetOf(mk({ 'data-drop': 'session', 'data-tmux': tmux })));
  const there = plain(Dnd.targetOf(mk({ 'data-drop': 'session', 'data-tmux': tmux, 'data-node': 'box' })));
  assert.deepEqual(here, { kind: 'session', tmux }, 'a local target is what it always was');
  assert.deepEqual(there, { kind: 'session', tmux, node: 'box' });
  const task = { id: 5, project: 'ccboard', repo: 'ccboard', agent: 'claude', phase: 'backlog', column: 'backlog', title: 't', mode: 'worktree' };
  const stIdle = JSON.parse(JSON.stringify(st));
  for (const p of stIdle.projects) for (const r of [p.root, ...(p.repos || [])]) for (const s of (r && r.sessions) || []) if (s.tmux === tmux) { s.state = 'idle'; s.wait_kind = undefined; }
  const vHere = plain(Dnd.verdict(task, here, stIdle));
  const vThere = plain(Dnd.verdict(task, there, stIdle));
  assert.equal(vHere.ok, true, 'this board\'s session takes the task');
  assert.equal(vHere.session.node, undefined, 'and the verdict is about this board\'s row, not the node\'s');
  assert.equal(vThere.ok, false);
  assert.equal(vThere.why, 'on another node');
  assert.equal(plain(Dnd.verdict(task, { kind: 'session', tmux, node: 'other' }, stIdle)).why, 'unknown session', 'a node with no such session');
});

test('palette: a session of a node has its own row id, its own address and a node word to search; sending to it is refused', () => {
  const w = keyWorld();
  const { local, remote, tmux } = twinState();
  const Palette = w.get('Palette');
  assert.equal(Palette.sessionHash(tmux), '#/s/' + tmux, 'a bare name: today\'s address');
  assert.equal(Palette.sessionHash(local), '#/s/' + tmux);
  assert.equal(Palette.sessionHash(remote), '#/n/box/s/' + tmux);
  const ctx = { sessions: [local, remote], target: null, targetTmux: null, box: null, skills: [], composer: null };
  const group = plain(Palette.catalog(ctx, '').find((g) => g.id === 'sessions').items.map((i) => ({ id: i.id, keywords: i.keywords })));
  assert.deepEqual(group.map((i) => i.id), ['s:' + tmux, 's:box/' + tmux]);
  assert.match(group[1].keywords, /\bbox\b/);
  assert.doesNotMatch(group[0].keywords, /\bbox\b/);
  const send = plain(Palette.sendGroups({ ...ctx, sessions: [{ ...local, state: 'idle' }, { ...remote, state: 'idle' }] }).flatMap((g) => g.items.map((i) => i.id)));
  assert.deepEqual(send, ['send:' + tmux], 'the send list is this board\'s sessions only');
});

// ---------------------------------------------------------------- the peek

test('the peek: this board\'s #/s/<tmux> is unchanged; a session of a node has its own quick replies, says where it runs and sends nothing to this board', async () => {
  const { w } = homeWorld({ wide: false });
  const tmux = 'ccboard--ccboard--s1';
  const sheet = () => w.document.querySelector('#sheet');
  w.location.hash = '#/s/' + tmux;
  const local = sheet().querySelector('.peek[data-tmux=' + tmux + ']');
  assert.ok(local, 'the local peek opens as it did');
  assert.equal(local.getAttribute('data-node'), null);
  w.location.hash = '#/';
  // the node's: the router gives it the not-paired page today, so mount the peek the way the hub phase will route it
  w.run(`pages.session.mount(document.querySelector('#page'), { id: 'node-session', params: { node: 'box', tmux: ${JSON.stringify(tmux)} }, query: {} })`);
  const peek = sheet().querySelector('.peek[data-node=box]');
  assert.ok(peek, 'the peek of a node\'s session');
  assert.equal(peek.getAttribute('data-tmux'), tmux);
  assert.match(w.run('document.title') + sheet().textContent, /on box/, 'the title says where it runs');
  w.run(`quickSave({ tmux: ${JSON.stringify(tmux)}, node: 'box' }, ['hello box'], 'claude')`);
  assert.equal(w.localStorage.getItem('ccboard:quick:box/' + tmux), JSON.stringify(['hello box']));
  assert.equal(w.localStorage.getItem('ccboard:quick:' + tmux), null, 'this board\'s list is untouched');
  w.run('__calls.length = 0');
  const input = peek.querySelector('textarea');
  input.value = 'rm -rf it';
  peek.querySelector('form.peek-send').dispatchEvent({ type: 'submit', preventDefault() {} });
  await tick();
  assert.deepEqual(plain(w.get('__calls')).filter((c) => c.method === 'POST'), [], 'nothing is posted to this board\'s session of that name');
  assert.match(plain(w.get('__toasts')).map((t) => t.text).join('|'), /another node/);
});

// ---------------------------------------------------------------- the quad store

test('the quad store: a layout saved before nodes loads unchanged and saves back byte for byte', () => {
  const w = keyWorld();
  const Quad = w.get('Quad');
  const before = { layout: 4, slots: ['ccboard--ccboard--s1', 'petroit--api--s3', '', '', '', '', '', '', '', ''], modes: { 'ccboard--ccboard--s1': 'full' }, zoom: null, composers: { 'petroit--api--s3': true } };
  w.localStorage.setItem('ccboard:quad:all', JSON.stringify(before));
  const loaded = plain(Quad.load(''));
  assert.deepEqual(loaded.slots, before.slots);
  assert.deepEqual(loaded.modes, before.modes);
  assert.deepEqual(loaded.composers, before.composers);
  Quad.save('', { ...loaded });
  assert.deepEqual(JSON.parse(w.localStorage.getItem('ccboard:quad:all')), before);
});

test('the quad store: a node\'s slot is {node, tmux}, comes back as its key, and the same name on this board stays a plain string beside it', () => {
  const w = keyWorld();
  const Quad = w.get('Quad');
  const slots = ['ccboard--ccboard--s1', 'box/ccboard--ccboard--s1', ...Array(8).fill('')];
  const saved = plain(Quad.save('', { layout: 2, slots, modes: { 'box/ccboard--ccboard--s1': 'ro' }, composers: {}, zoom: null }));
  assert.deepEqual(saved.slots.slice(0, 2), ['ccboard--ccboard--s1', { node: 'box', tmux: 'ccboard--ccboard--s1' }]);
  const raw = JSON.parse(w.localStorage.getItem('ccboard:quad:all'));
  assert.deepEqual(raw.slots[1], { node: 'box', tmux: 'ccboard--ccboard--s1' });
  const back = plain(Quad.load(''));
  assert.deepEqual(back.slots.slice(0, 2), slots.slice(0, 2));
  assert.deepEqual(back.modes, { 'box/ccboard--ccboard--s1': 'ro' });
  // an older build reads the same value with its string-only rule: the node's slot is empty there, and nothing throws
  const oldClean = (list) => { const out = []; const seen = new Set(); for (let i = 0; i < 10; i++) { const t = Array.isArray(list) && typeof list[i] === 'string' ? list[i] : ''; if (t && Quad.NAME_RE.test(t) && !seen.has(t)) { seen.add(t); out.push(t); } else out.push(''); } return out; };
  assert.deepEqual(oldClean(raw.slots).slice(0, 2), ['ccboard--ccboard--s1', '']);
  // and a hostile value is just an empty slot
  w.localStorage.setItem('ccboard:quad:all', JSON.stringify({ layout: 2, slots: [{ node: 'Box', tmux: 'a--b--c' }, { node: 'box' }, { node: 'box', tmux: 'a/b' }, { node: 'local', tmux: 'a--b--c' }, 7, null, ['x'], 'box/', '../x'] }));
  assert.deepEqual(plain(Quad.load('')).slots, Array(10).fill(''));
});

test('the quad address: ?s= carries a node\'s slot as <handle>/<name> and parses it back; a slot key from outside the grammar is dropped', () => {
  const w = keyWorld();
  const Quad = w.get('Quad');
  const slots = ['ccboard--ccboard--s1', 'box/ccboard--ccboard--s1', '', '', '', '', '', '', '', ''];
  const q = plain(Quad.buildQuery({ layout: 2, slots }));
  assert.equal(q.s, 'ccboard--ccboard--s1,box/ccboard--ccboard--s1');
  assert.deepEqual(plain(Quad.parseQuery(q)).slots.slice(0, 2), slots.slice(0, 2));
  assert.deepEqual(plain(Quad.parseQuery({ s: 'Box/a--b--c,box/a--b--c,box/x' })).slots.slice(0, 3), ['', 'box/a--b--c', '']);
  assert.equal(Quad.keyOk('box/a--b--c'), true);
  assert.equal(Quad.keyOk('a--b--c'), true);
  assert.equal(Quad.keyOk('box/a'), false, 'the name part is the quad\'s own three-part name');
  assert.equal(Quad.label('box/ccboard--ccboard--s1'), 'ccboard/ccboard · s1 · on box');
  assert.equal(Quad.label('ccboard--ccboard--s1'), 'ccboard/ccboard · s1');
});

test('the quad keeps two slots for one name on two nodes: slotsState keeps both, auto-fill offers both, a node\'s needs-you does not make this board\'s session wait', () => {
  const w = keyWorld();
  const Quad = w.get('Quad');
  const { st, tmux } = twinState();
  const keys = Quad.candidates(st, '').map((s) => w.get('Ref').key(s));
  assert.ok(keys.includes(tmux) && keys.includes('box/' + tmux), 'both are candidates');
  const r = plain(Quad.slotsState({ query: { s: tmux + ',box/' + tmux }, saved: null, state: st, project: '', n: 2 }));
  assert.deepEqual(r.slots.slice(0, 2), [tmux, 'box/' + tmux]);
  assert.equal(Quad.autoFill(st, 10, { project: '' }).filter((k) => k === tmux || k === 'box/' + tmux).length, 2);
  const withPerm = { ...st, pending_permissions: [{ id: 1, tmux_name: tmux, tool_name: 'Bash' }] };
  assert.ok(Quad.perm(withPerm, tmux), 'this board\'s permission');
  assert.equal(Quad.perm(withPerm, 'box/' + tmux), null, 'is not the node\'s');
});

// ---------------------------------------------------------------- NodeView: the words, chips and row of a node found on the tailnet (issue #134)

const rowFx = (over = {}) => ({ ts_id: 'n1', name: 'node-a', dns_name: 'node-a.example.ts.net.', os: 'linux', online: true, last_seen: null, owner: 'user', tags: [], state: 'found', url: 'https://node-a.example.ts.net:8443', node_id: 'ts:nA', at: 1_000, age: 0, stale: false, ...over });

test('NodeView.PROBE: every state has plain words, a glyph and a hint; no em-dash, no raw state name in the words', () => {
  const w = makeWorld();
  const P = plain(w.get('NodeView.PROBE'));
  assert.deepEqual(Object.keys(P), ['found', 'refuses', 'no_ccboard', 'no_tls', 'unreachable', 'offline', 'invalid', 'unchecked']);
  for (const [k, v] of Object.entries(P)) {
    assert.ok(v.word && v.glyph && v.hint, k);
    assert.ok(['', 'ok', 'warn', 'bad'].includes(v.tone), k);
    assert.doesNotMatch(v.word + v.hint, /—|_/, `${k}: plain words`);
  }
  assert.equal(P.found.tone, 'ok');
  assert.equal(new Set(Object.values(P).map((v) => v.word)).size, 8, 'eight states, eight different words');
});

test('NodeView.state: only a state of the table counts; anything else (a prototype name, a number, a missing state) reads as unchecked, never as found', () => {
  const w = makeWorld();
  w.ctx.__r = [rowFx(), rowFx({ state: 'refuses' }), rowFx({ state: 'constructor' }), rowFx({ state: '__proto__' }), rowFx({ state: 7 }), rowFx({ state: null }), { ts_id: 'x' }, null];
  assert.deepEqual(plain(w.run('__r.map((r) => NodeView.state(r))')), ['found', 'refuses', 'unchecked', 'unchecked', 'unchecked', 'unchecked', 'unchecked', 'unchecked']);
});

test('NodeView.epoch / span: seconds, milliseconds, numeric strings and ISO stamps; unreadable is null / empty', () => {
  const w = makeWorld();
  const run = (c) => plain(w.run(c));
  assert.equal(run('NodeView.epoch(1700000000)'), 1700000000);
  assert.equal(run('NodeView.epoch(1700000000000)'), 1700000000);
  assert.equal(run('NodeView.epoch("1700000000")'), 1700000000);
  assert.equal(run('NodeView.epoch("2023-11-14T22:13:20Z")'), 1700000000);
  for (const bad of ['null', 'undefined', '""', '"nope"', '0', '-5', 'NaN', '{}']) assert.equal(run(`NodeView.epoch(${bad})`), null, bad);
  const now = 1700000000 * 1000;
  assert.deepEqual(run(`[0, 59, 60, 3599, 3600, 86399, 86400, -30].map((s) => NodeView.span(1700000000 - s, ${now}))`), ['0s', '59s', '1m', '59m', '1h', '23h', '1d', '0s']);
  assert.equal(run(`NodeView.span("nope", ${now})`), '');
});

test('NodeView.osName / owner / onlineWord / shortId / hostOf', () => {
  const w = makeWorld();
  const run = (c) => plain(w.run(c));
  assert.deepEqual(run('["linux","macOS","Windows","iOS","android","plan9","",null,"a-very-long-operating-system-name"].map((o) => NodeView.osName(o))'), ['Linux', 'macOS', 'Windows', 'iOS', 'Android', 'plan9', '', '', 'a-very-long-operatin']);
  assert.deepEqual(run('[{owner:"user",tags:["tag:x"]},{owner:"tag",tags:["tag:ccboard"]},{owner:"other"},{tags:["tag:ccboard"]},{},null].map((r) => NodeView.owner(r))'), ['your device', 'tag', '', '', '', '']);
  const now = 1700000000 * 1000;
  assert.equal(run(`NodeView.onlineWord({online:true}, ${now})`), 'on the tailnet');
  assert.equal(run(`NodeView.onlineWord({online:false,last_seen:${1700000000 - 7300}}, ${now})`), 'offline, last seen 2h ago');
  assert.equal(run(`NodeView.onlineWord({online:false}, ${now})`), 'offline');
  assert.equal(run(`NodeView.onlineWord({online:"yes"}, ${now})`), 'offline', 'only a real true is online');
  assert.deepEqual(run('["ts:nDEMO1CNTRL","ccb:3f9c1a7d0b25a81e5c20d7f4","",null].map((i) => NodeView.shortId(i))'), ['ts:nDEMO1CNTRL', 'ccb:3f9c...d7f4', '', '']);
  assert.deepEqual(run('["https://a.example.ts.net:8443/x","http://100.64.0.1","javascript:alert(1)","https://u@evil.test","https://","",null].map((u) => NodeView.hostOf(u))'), ['a.example.ts.net:8443', '100.64.0.1', '', '', '', '', '']);
});

test('NodeView.isPaired: by node id, else by host (port ignored); never by a name or a different host', () => {
  const w = makeWorld();
  w.ctx.__row = rowFx();
  const paired = (list) => w.run(`NodeView.isPaired(__row, ${JSON.stringify(list)})`);
  assert.equal(paired([{ url: 'https://node-a.example.ts.net:443' }]), true, 'same host, another port');
  assert.equal(paired([{ url: 'https://NODE-A.example.ts.net' }]), true, 'case ignored');
  assert.equal(paired([{ node_id: 'ts:nA', url: 'https://other.example.ts.net' }]), true, 'by node id');
  assert.equal(paired([{ name: 'node-a', url: 'https://node-b.example.ts.net' }]), false, 'a name is not an identity');
  assert.equal(paired([{ node_id: 'ts:nB' }]), false);
  assert.equal(paired([]), false);
  assert.equal(paired([null, 'x', {}]), false);
  w.ctx.__row = rowFx({ state: 'unreachable', url: null, node_id: null });
  assert.equal(paired([{ url: 'https://node-a.example.ts.net' }]), true, 'a row never probed still matches by its tailnet name');
  assert.equal(w.run('NodeView.isPaired(null, [{url:"https://a"}])'), false);
});

test('NodeView.sig changes with what the row shows and with the age bucket, and with nothing else', () => {
  const w = makeWorld();
  const sig = (r, b = 1) => { w.ctx.__r = r; return w.run(`NodeView.sig(__r, ${b})`); };
  const base = sig(rowFx());
  assert.equal(sig(rowFx()), base);
  assert.equal(sig({ ...rowFx(), ips: ['100.64.0.9'], extra: 1, age: 77, stale: true }), base, 'fields the row does not show, and the answer\'s own age, do not repaint it');
  for (const r of [rowFx({ name: 'x' }), rowFx({ online: false }), rowFx({ state: 'refuses' }), rowFx({ owner: 'tag' }), rowFx({ os: 'macOS' }), rowFx({ at: 2_000 }), rowFx({ url: 'https://x.example.ts.net' })]) assert.notEqual(sig(r), base);
  assert.notEqual(sig(rowFx(), 2), base);
});

function viewWorld() {
  const { w } = homeWorld({ state: fakeState() });
  return w;
}

test('NodeView.row: name, online word, chips, note and the action; the root is keyed by the Tailscale id and carries the state; a hostile name is text', () => {
  const w = viewWorld();
  const evil = '<img src=x onerror=alert(1)>';
  w.ctx.__r = rowFx({ name: evil });
  const row = w.run('NodeView.row(__r, { nowMs: 1000 * 1000 + 10000, action: el("button", { text: "Pair" }) })');
  assert.equal(row.getAttribute('data-key'), 'n1');
  assert.equal(row.getAttribute('data-state'), 'found');
  assert.equal(row.querySelector('.nd-name').textContent, evil);
  assert.equal(row.querySelectorAll('img').length, 0);
  assert.deepEqual(row.querySelectorAll('.nd-chips .badge').map((b) => b.textContent), ['Linux', 'your device', '✓ ccboard answers']);
  assert.equal(row.querySelector('.nd-act button').textContent, 'Pair');
  assert.equal(row.classList.contains('stale'), false, 'checked 10 s ago');
  assert.match(row.querySelector('.nd-note').textContent, /Checked 10s ago\.$/);
  assert.notEqual(row.querySelector('.nd-chips .badge.ok'), null);
});

test('NodeView.row: no action means no action cell; a row with no name falls back to the DNS name; stale past 30 s, not before', () => {
  const w = viewWorld();
  w.ctx.__r = rowFx({ name: '' });
  const at = (s) => w.run(`NodeView.row(__r, { nowMs: ${(1000 + s) * 1000} })`);
  const fresh = at(30);
  assert.equal(fresh.querySelector('.nd-act'), null);
  assert.equal(fresh.querySelector('.nd-name').textContent, 'node-a.example.ts.net.');
  assert.equal(fresh.classList.contains('stale'), false, 'exactly 30 s is still fresh');
  assert.equal(at(31).classList.contains('stale'), true);
  w.ctx.__r = rowFx({ name: '', dns_name: '' });
  assert.equal(w.run('NodeView.row(__r, {})').querySelector('.nd-name').textContent, 'unnamed device');
});

test('NodeView.boxNow: the box\'s clock is the answer\'s time plus what passed here since it arrived; the browser\'s own clock only without an answer time', () => {
  const w = makeWorld();
  const run = (c) => plain(w.run(c));
  assert.equal(run('NodeView.boxNow("2026-10-03T03:00:00.000+00:00", 5000, 8000)'), Date.parse('2026-10-03T03:00:03.000Z'));
  assert.equal(run('NodeView.boxNow(1700000000, 5000, 4000)'), 1700000000 * 1000, 'a clock that went backwards adds nothing');
  assert.equal(run('NodeView.boxNow(null, 5000, 8000)'), 8000);
  assert.equal(run('NodeView.boxNow("nope", 5000, 8000)'), 8000);
  assert.equal(run('NodeView.boxNow(1700000000, undefined, 8000)'), 8000);
});

test('NodeView.row: an offline, invalid or unchecked row was never probed, so it shows no check time and is never stale', () => {
  const w = viewWorld();
  for (const state of ['offline', 'invalid', 'unchecked']) {
    w.ctx.__r = rowFx({ online: state !== 'offline', state, url: null, node_id: null, at: state === 'unchecked' ? null : 1 });
    const row = w.run('NodeView.row(__r, { nowMs: 9e12 })');
    assert.doesNotMatch(row.querySelector('.nd-note').textContent, /Checked/);
    assert.equal(row.classList.contains('stale'), false);
  }
});

// ---------------------------------------------------------------- pairing (issue #135): the words and checks of the Settings > Nodes pairing flow

const pv = (code, arg) => { const w = viewWorld(); w.ctx.__a = arg; return plain(w.run(code)); };

test('NodeView.SCOPES: read and tasks are on by default, sessions and permissions are off and say so; the permissions line says a person answers, never the token', () => {
  const S = pv('NodeView.SCOPES');
  assert.deepEqual(S.map((s) => s.id), ['read', 'tasks', 'sessions', 'permissions']);
  assert.deepEqual(S.map((s) => s.on), [true, true, false, false]);
  for (const s of S) { assert.ok(s.label && s.what, s.id); assert.doesNotMatch(s.what + s.label, /—/, `${s.id}: no em-dash`); }
  assert.match(S[3].what, /still answered only by a signed-in person on the calling board, never by the token alone/);
  assert.match(S[2].what + S[3].what, /Off unless you turn it on\./);
  assert.deepEqual(pv('NodeView.CODE_MINUTES'), [1, 5, 10, 20, 30]);
  assert.equal(pv('NodeView.CODE_DEFAULT_MINUTES'), 10);
});

test('NodeView.scopeList keeps the scopes this page can explain, in the table\'s order, once each', () => {
  assert.deepEqual(pv('NodeView.scopeList(__a)', ['tasks', 'read', 'tasks', 'root', 'sessions']), ['read', 'tasks', 'sessions']);
  assert.deepEqual(pv('NodeView.scopeList(__a)', 'read'), []);
  assert.deepEqual(pv('NodeView.scopeList(__a)', null), []);
  assert.deepEqual(pv('NodeView.scopeList(__a)', [{ id: 'read' }, 5, null]), []);
});

test('NodeView.code: Crockford base32, case, spaces and dashes do not matter, O is 0 and I or L is 1, U and a wrong length are refused with a plain sentence', () => {
  const good = { code: 'K7Q2M-4XD9R', err: '' };
  for (const v of ['K7Q2M-4XD9R', 'k7q2m-4xd9r', ' k7q2m 4xd9r ', 'K7Q2M4XD9R', 'k7q2m_4xd9r'.replace('_', '-')]) assert.deepEqual(pv('NodeView.code(__a)', v), good, v);
  assert.equal(pv('NodeView.code(__a)', 'OIL00-11000').code, '01100-11000', 'O reads as 0, I and L as 1');
  for (const v of ['', 'K7Q2M-4XD9', 'K7Q2M-4XD9RR', 'K7Q2M-4XD9U', 'K7Q2M 4XD9!', null, 5, {}]) {
    const r = pv('NodeView.code(__a)', v);
    assert.equal(r.code, '', String(v));
    assert.equal(r.err, 'A pairing code is 10 letters and numbers, like K7Q2M-4XD9R.', String(v));
  }
});

test('NodeView.formatCode shows XXXXX-XXXXX, upper case, and leaves anything else as it came', () => {
  assert.equal(pv('NodeView.formatCode(__a)', 'k7q2m4xd9r'), 'K7Q2M-4XD9R');
  assert.equal(pv('NodeView.formatCode(__a)', 'K7Q2M-4XD9R'), 'K7Q2M-4XD9R');
  assert.equal(pv('NodeView.formatCode(__a)', 'abc'), 'ABC');
  assert.equal(pv('NodeView.formatCode(__a)', null), '');
});

test('NodeView.address: https host[:port] only; a bare host gets https; http, a path, user info, a query and junk are refused in plain words', () => {
  const ok = (v) => pv('NodeView.address(__a)', v);
  assert.deepEqual(ok('https://box.example.ts.net'), { url: 'https://box.example.ts.net', err: '' });
  assert.deepEqual(ok('  Box.Example.ts.net:8443/ '), { url: 'https://box.example.ts.net:8443', err: '' });
  assert.deepEqual(ok('https://100.101.102.103:8443'), { url: 'https://100.101.102.103:8443', err: '' });
  assert.deepEqual(ok('https://[fd7a:115c:a1e0::1]:8443'), { url: 'https://[fd7a:115c:a1e0::1]:8443', err: '' });
  assert.match(ok('http://box.example.ts.net').err, /^Use an https address/);
  assert.match(ok('HTTP://box.example.ts.net').err, /^Use an https address/);
  assert.equal(ok('').err, 'Type the address of the other board.');
  assert.equal(ok('   ').err, 'Type the address of the other board.');
  for (const bad of ['https://box.example.ts.net/api', 'https://user:pw@box.example.ts.net', 'https://box.example.ts.net?x=1', 'https://box.example.ts.net#x', 'ftp://box.example.ts.net',
    'https://bo x.example.ts.net', 'https://-box.example.ts.net', 'https://box.example.ts.net:99999999', 'https://', 'javascript:alert(1)', null, 7]) {
    const r = ok(bad);
    assert.equal(r.url, '', String(bad));
    assert.ok(r.err, String(bad));
  }
});

test('NodeView.handle: empty is fine, a valid handle is lower-cased, local and self are taken, anything else is refused', () => {
  const h = (v) => pv('NodeView.handle(__a)', v);
  assert.deepEqual(h(''), { handle: '', err: '' });
  assert.deepEqual(h('  '), { handle: '', err: '' });
  assert.deepEqual(h(' Build-Box '), { handle: 'build-box', err: '' });
  assert.match(h('local').err, /"local" is taken/);
  assert.match(h('SELF').err, /"self" is taken/);
  for (const bad of ['-box', 'box_1', 'a'.repeat(32), 'box.one', 'two words']) assert.match(h(bad).err, /1 to 31 lower case letters, numbers or dashes/, bad);
  assert.equal(h('a'.repeat(31)).handle, 'a'.repeat(31));
});

test('NodeView.countdown: m:ss, rounded up, never negative, never NaN', () => {
  const c = (v) => pv('NodeView.countdown(__a)', v);
  assert.equal(c(600000), '10:00');
  assert.equal(c(599001), '10:00');
  assert.equal(c(599000), '9:59');
  assert.equal(c(61000), '1:01');
  assert.equal(c(1), '0:01');
  assert.equal(c(0), '0:00');
  assert.equal(c(-5000), '0:00');
  assert.equal(c(NaN), '0:00');
  assert.equal(c('x'), '0:00');
});

test('NodeView.errField: the field an error is about, from the status and the board\'s words; a 429 is the code field; the rest belongs to no field', () => {
  const f = (s, m, r) => { const w = viewWorld(); w.ctx.__s = s; w.ctx.__m = m; w.ctx.__r = r; return w.run('NodeView.errField(__s, __m, __r)'); };
  assert.equal(f(429, ''), 'code');
  assert.equal(f(400, 'that code is wrong, expired or already used'), 'code');
  assert.equal(f(400, 'code burned: too many wrong tries'), 'code');
  assert.equal(f(400, 'the address is not on the tailnet'), 'address');
  assert.equal(f(502, 'the other node could not be reached'), 'address');
  assert.equal(f(400, 'the host name does not resolve'), 'address');
  assert.equal(f(409, 'handle build-box is already taken'), 'handle');
  assert.equal(f(500, 'something odd happened'), '');
  assert.equal(f(500, null), '');
  assert.equal(f(undefined, undefined), '');
  for (const [reason, field] of [['none', 'code'], ['wrong', 'code'], ['expired', 'code'], ['burned', 'code'], ['rate_limited', 'code'], ['bad_url', 'address'], ['callback_mismatch', 'address'],
    ['unreachable', 'address'], ['refused', ''], ['bad_request', ''], ['store', '']]) assert.equal(f(500, 'anything', reason), field, reason);
  assert.equal(f(400, 'the address is bad', 'made_up_reason'), 'address', 'a reason this page does not know falls back to the words');
  assert.equal(f(502, 'handle x', 'refused'), '', 'the board\'s reason wins over the words');
});

test('NodeView.list takes a bare array, or the first key that holds one, and drops what is not an object', () => {
  const l = (a, ...keys) => { const w = viewWorld(); w.ctx.__a = a; w.ctx.__k = keys; return plain(w.run('NodeView.list(__a, ...__k)')); };
  assert.deepEqual(l([{ a: 1 }, null, 5, 'x']), [{ a: 1 }]);
  assert.deepEqual(l({ nodes: [{ a: 1 }] }, 'peers', 'nodes'), [{ a: 1 }]);
  assert.deepEqual(l({ peers: [{ b: 2 }], nodes: [{ a: 1 }] }, 'peers', 'nodes'), [{ b: 2 }]);
  assert.deepEqual(l({ ok: true }, 'nodes'), []);
  assert.deepEqual(l(null, 'nodes'), []);
  assert.deepEqual(l({ nodes: 'x' }, 'nodes'), []);
});

test('NodeView.peer: the id the routes take, the name with its fallbacks, the address host, scopes this page knows, and the last time the board really heard from it', () => {
  const p = (r) => pv('NodeView.peer(__a)', r);
  const r = p({ peer_id: 'p1', handle: 'box', node_id: 'ts:n', name: 'Build Box', url: 'https://box.example.ts.net:8443/x', scopes: ['tasks', 'read', 'root'], last_seen: '2026-10-03T03:00:00Z', created_at: '2026-01-01T00:00:00Z', direction: 'out' });
  assert.equal(r.id, 'p1');
  assert.equal(r.key, 'p1');
  assert.equal(r.name, 'Build Box');
  assert.equal(r.handle, 'box');
  assert.equal(r.host, 'box.example.ts.net:8443');
  assert.deepEqual(r.scopes, ['read', 'tasks']);
  assert.equal(r.seen, Date.parse('2026-10-03T03:00:00Z') / 1000);
  assert.equal(p({ peer_id: 'p2', created_at: '2026-01-01T00:00:00Z' }).seen, null, 'the day it was saved is not a time it was reached');
  assert.equal(p({ id: 'x', peer_name: 'Desk', peer_url: 'https://desk.example.ts.net', last_used_at: 1790996400 }).name, 'Desk');
  assert.equal(p({ id: 'x', last_used_at: 1790996400 }).seen, 1790996400);
  assert.equal(p({ handle: 'only-handle' }).name, 'only-handle');
  assert.equal(p({}).name, 'unnamed node');
  assert.equal(p(null).name, 'unnamed node');
  assert.equal(p({ peer_id: 'p', callback_unverified: true, needs_repair: true, legacy: true, revoked_at: '2026-01-01', direction: 'in' }).direction, 'in');
  assert.deepEqual([p({ peer_id: 'p', callback_unverified: true }).unverified, p({ peer_id: 'p', callback_unverified: 'yes' }).unverified], [true, false], 'only a real true counts');
  assert.equal(p({ peer_id: 'p'.repeat(200) }).id.length, 80);
});

test('NodeView.peerState: re-pair wins; paired only once reached; incoming has its own words; every state has a glyph, words and a hint', () => {
  const s = (p, inc) => { const w = viewWorld(); w.ctx.__p = p; w.ctx.__i = inc; return plain(w.run('NodeView.peerState(__p, __i)')); };
  assert.equal(s({ repair: true, seen: 1 }, false).word, 're-pair needed');
  assert.equal(s({ repair: true, seen: 1 }, false).tone, 'warn');
  assert.deepEqual([s({ seen: 1 }, false).word, s({ seen: 1 }, false).tone], ['paired', 'ok']);
  assert.equal(s({ seen: null }, false).word, 'waiting for first contact');
  assert.deepEqual([s({ legacy: true, seen: 1 }, false).word, s({ legacy: true, seen: null }, false).word], ['read only', 'read only'], 'a CCBOARD_NODES row is never called paired, read or not');
  assert.equal(s({ legacy: true, repair: true }, false).word, 're-pair needed');
  assert.equal(s({ seen: 1 }, true).word, 'in use');
  assert.equal(s({ seen: null }, true).word, 'never used');
  for (const x of [s({ repair: true }, false), s({ seen: 1 }, false), s({ seen: null }, false), s({ seen: 1 }, true), s({ seen: null }, true)]) {
    assert.ok(x.glyph && x.word && x.hint);
    assert.doesNotMatch(x.word + x.hint, /—/);
  }
});

test('NodeView.peerNote: address, when it was last reached, the last error, the legacy sentence, and the 60 s rotation window only while it lasts', () => {
  const n = (p, o) => { const w = viewWorld(); w.ctx.__p = p; w.ctx.__o = o; return w.run('NodeView.peerNote(__p, __o)'); };
  const base = { host: 'box.example.ts.net', seen: 1000, error: '', legacy: false };
  assert.equal(n(base, { nowMs: 1000 * 1000 + 120000 }), 'box.example.ts.net · last reached 2m ago');
  assert.equal(n({ ...base, seen: null }, { nowMs: 5 }), 'box.example.ts.net · not reached yet');
  assert.equal(n({ ...base, seen: null, host: '' }, { nowMs: 5, incoming: true }), 'never used');
  assert.equal(n(base, { nowMs: 1000 * 1000 + 3600000, incoming: true }), 'box.example.ts.net · last used 1h ago');
  assert.match(n({ ...base, error: 'refused (401)' }, { nowMs: 1000 * 1000 }), /· last error: refused \(401\)$/);
  assert.doesNotMatch(n({ ...base, error: 'refused (401)' }, { nowMs: 1000 * 1000, incoming: true }), /last error/, 'an error of our calls is not shown for a caller');
  assert.match(n({ ...base, legacy: true }, { nowMs: 1000 * 1000 }), /· read only, pair to enable actions$/);
  assert.match(n(base, { nowMs: 1000 * 1000 + 30000, rotatedMs: 1000 * 1000 }), /token rotated, the old one still works for 60 s$/);
  assert.doesNotMatch(n(base, { nowMs: 1000 * 1000 + 60000, rotatedMs: 1000 * 1000 }), /rotated/, 'the window is over at 60 s');
});

test('NodeView.peerRow: name and short name, state chip, scope chips with their meaning as the hint, flags, a note and the actions; keyed by the id; a hostile name is text', () => {
  const w = viewWorld();
  const evil = '<img src=x onerror=alert(1)>';
  w.ctx.__r = { peer_id: 'p1', handle: 'box', name: evil, url: 'https://box.example.ts.net', scopes: ['read', 'permissions'], last_seen: 1790996400, callback_unverified: true, legacy: true };
  const row = w.run('NodeView.peerRow(__r, { nowMs: 1790996400000 + 5000, actions: [el("button", { text: "Remove" })] })');
  assert.equal(row.getAttribute('data-key'), 'p1');
  assert.equal(row.getAttribute('data-kind'), 'out');
  assert.equal(row.querySelector('.nd-name').textContent, evil);
  assert.equal(row.querySelectorAll('img').length, 0);
  assert.equal(row.querySelector('.nd-handle').textContent, 'box');
  assert.deepEqual(row.querySelectorAll('.nd-chips .badge').map((b) => b.textContent), ['· read only', 'read', 'permissions', '! callback not verified'], 'a legacy row that was read a moment ago is not "paired"');
  assert.match(row.querySelectorAll('.nd-chips .badge')[2].getAttribute('title'), /never by the token alone/);
  assert.equal(row.querySelector('.nd-act button').textContent, 'Remove');
  assert.equal(row.classList.contains('repair'), false);
  const inc = w.run('NodeView.peerRow(__r, { incoming: true })');
  assert.equal(inc.getAttribute('data-kind'), 'in');
  assert.equal(w.run('NodeView.peerRow({ peer_id: "p", needs_repair: true })').classList.contains('repair'), true);
  assert.equal(w.run('NodeView.peerRow({ peer_id: "p" })').querySelector('.nd-act'), null, 'no actions, no action cell');
});

test('NodeView.auditWord: the board\'s own actions in plain words (worked and not), the raw name made readable for the rest, nothing long', () => {
  const a = (v, ok) => { const w = viewWorld(); w.ctx.__a = v; w.ctx.__ok = ok; return w.run('NodeView.auditWord(__a, __ok)'); };
  assert.equal(a('code_created', true), 'Pairing code created');
  assert.equal(a('code_used', true), 'Pairing code used');
  assert.equal(a('code_burned', false), 'Pairing code burned');
  assert.equal(a('code_cancelled', true), 'Pairing code cancelled');
  assert.equal(a('callback_unverified', false), 'Callback not verified');
  assert.equal(a('paired', true), 'Paired');
  assert.equal(a('paired_back', true), 'Paired both ways');
  assert.equal(a('pair_refused', false), 'Pairing refused');
  assert.equal(a('pair_failed', false), 'Pairing failed');
  assert.equal(a('rotated', true), 'Token rotated');
  assert.equal(a('rotate_failed', false), 'Token rotation failed');
  assert.equal(a('revoked', true), 'Pair revoked');
  assert.equal(a('removed', true), 'Node removed');
  assert.equal(a('unpair', true), 'Node removed');
  assert.equal(a('unpair', false), 'Node removed, the other node was not told');
  assert.equal(a('relay.task_start', true), 'Relay task start');
  assert.equal(a('relay.task_start', false), 'Relay task start failed');
  assert.equal(a('', true), 'Event');
  assert.equal(a(null, false), 'Event failed');
  assert.equal(a('rotated'), 'Token rotated', 'no verdict means it worked');
  assert.ok(a('x'.repeat(500), true).length <= 40);
});

test('NodeView.audit: who, which way, whether it worked (the board\'s status word, a boolean ok, or a status below 400), the words for that, and the detail cut short', () => {
  const a = (r) => pv('NodeView.audit(__a, 4)', r);
  const r = a({ id: 9, at: '2026-10-03T03:00:00Z', direction: 'in', peer: 'abc123', node_name: 'desk', action: 'code_used', status: 'ok', detail: 'x'.repeat(500) });
  assert.deepEqual([r.key, r.dir, r.who, r.word, r.ok, r.detail.length], ['9', 'from', 'desk', 'Pairing code used', true, 140]);
  assert.equal(r.at, Date.parse('2026-10-03T03:00:00Z') / 1000);
  assert.equal(a({ direction: 'out', status: 'failed', action: 'rotate_failed' }).ok, false);
  assert.equal(a({ direction: 'out', status: 'failed', action: 'rotate_failed' }).word, 'Token rotation failed');
  assert.equal(a({ direction: 'out', status: 'refused', action: 'pair_refused' }).ok, false);
  assert.equal(a({ direction: 'out', ok: false, action: 'paired' }).word, 'Pairing failed');
  assert.equal(a({ direction: 'out' }).dir, 'to');
  assert.equal(a({ status: 403 }).ok, false);
  assert.equal(a({ status: 200 }).ok, true);
  assert.equal(a({ status: 'ok' }).ok, true);
  assert.equal(a({}).key, 'i4', 'a row with no id is keyed by its place');
  assert.equal(a({ direction: 'sideways' }).dir, '');
  assert.equal(a({ peer: 'abc123' }).who, '', 'an opaque peer id is not a name to show');
  assert.equal(a(null).word, 'Event');
});

test('NodeView.auditRow: a done row has a tick, a failed row a cross and its words (never colour only), the place and age, and the detail as text', () => {
  const w = viewWorld();
  const evil = '<img src=x onerror=alert(1)>';
  w.ctx.__i = { key: '1', at: 1000, word: 'Pairing failed', dir: 'to', who: evil, ok: false, detail: evil };
  const row = w.run('NodeView.auditRow(__i, 1000 * 1000 + 180000)');
  assert.equal(row.querySelector('.nd-glyph').textContent, '✕');
  assert.equal(row.querySelector('.nd-audit-word').textContent, 'Pairing failed');
  assert.equal(row.querySelector('.nd-audit-where').textContent, `to ${evil} · 3m ago`);
  assert.equal(row.querySelector('.nd-audit-detail').textContent, evil);
  assert.equal(row.querySelectorAll('img').length, 0);
  assert.ok(row.classList.contains('bad'));
  assert.equal(row.getAttribute('data-key'), '1');
  w.ctx.__i = { key: '2', at: null, word: 'Token rotated', dir: '', who: '', ok: true, detail: '' };
  const ok = w.run('NodeView.auditRow(__i, 5)');
  assert.equal(ok.querySelector('.nd-glyph').textContent, '✓');
  assert.equal(ok.querySelector('.nd-audit-word').textContent, 'Token rotated');
  assert.equal(ok.querySelector('.nd-audit-where'), null);
  assert.equal(ok.querySelector('.nd-audit-detail'), null);
});

test('nodes.js pairing helpers hold no code or token: no storage, no network, no timer, no innerHTML, and the words carry no em-dash', () => {
  const src = fs.readFileSync(path.join(STATIC, 'nodes.js'), 'utf8');
  const code = src.slice(src.indexOf('NodeView.SCOPES = [')).replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.doesNotMatch(code, /localStorage|sessionStorage|fetch\(|XMLHttpRequest|setInterval|setTimeout|innerHTML|insertAdjacentHTML|cssText|style=|\.style\b|api\(/);
  assert.doesNotMatch(code, /—/);
});
