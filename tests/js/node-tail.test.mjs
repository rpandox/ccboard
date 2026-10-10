// Contract tests for the node page's additions of issues #141 and #143 (app/static/pages/node.js, nodes-hub.js): the tail on a remote session's peek (Live.subscribe with a remote Ref,
// kept across the peek's repaints, lines as text, a gone that keeps the lines), Start on <node> for a backlog task, the optimistic rows (Nodes.pend), and the demo fixture's relay
// answers. Real scripts on minidom's DOM (tests/js/hubworld.mjs); Live.subscribe is a recorder that hands the test the callback. Nothing opens a connection.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { boardState, hubWorld, iso, page, plain, poll, rec, text, tick } from './hubworld.mjs';

const HOSTILE = '<img src=x onerror=alert(1)>';
const nodes = (over = {}) => [rec('build-box', { scopes: ['read', 'tasks', 'sessions'], ...over }), rec('read-only', { scopes: ['read'] })];
const all = (n, sel) => (n.document || n).querySelectorAll(sel);

async function tWorld(list = nodes(), extra = {}) {
  const h = hubWorld({ state: boardState(), nodes: list, ...extra });
  const { w } = h;
  w.ctx.__subs = [];
  w.run(`Live.subscribe = (ref, fn) => { const s = { ref: Ref.text(ref), fn, off: false }; __subs.push(s); return () => { s.off = true; }; };`);
  w.run('launch = (o) => { __launched.push(o); return true; }');
  w.ctx.__launched = [];
  await poll(w);
  return h;
}
const go = async (w, hash) => { w.location.hash = hash; await tick(); };
const subs = (w) => plain(w.get('__subs.map((s) => ({ ref: s.ref, off: s.off }))'));
const feed = (w, i, lines, meta) => { w.ctx.__l = lines; w.ctx.__m = meta; w.run(`__subs[${i}].fn(__l, __m)`); };
const tail = (w) => page(w).querySelector('.nd-tail');

test('the session peek of a node with the sessions scope shows the Tail: it subscribes with the remote Ref, waits for the first lines, then shows them as text', async () => {
  const { w } = await tWorld();
  await go(w, '#/n/build-box/s/shop--api--s1');
  const t = tail(w);
  assert.ok(t, 'a Tail section');
  assert.equal(text(t.querySelector('h2')), 'Tail');
  assert.match(text(t), /The last captured lines of the session, not a terminal\./);
  assert.deepEqual(subs(w), [{ ref: 'build-box/shop--api--s1', off: false }]);
  assert.equal(text(t.querySelector('.nd-tail-st')), 'Waiting for the first lines…');
  feed(w, 0, ['first line', '  second line']);
  assert.equal(text(t.querySelector('.nd-tail-pre')), 'first line\n  second line');
  assert.equal(text(t.querySelector('.nd-tail-st')), 'Live: updates as the session prints.');
  feed(w, 0, ['third']);
  assert.equal(text(t.querySelector('.nd-tail-pre')), 'third');
  assert.equal(t.querySelector('.nd-tail-pre').children.length, 0, 'text only');
});

test('a reading every 6 s repaints the peek but not the tail: the same stream, the same element, the same lines', async () => {
  const { w } = await tWorld();
  await go(w, '#/n/build-box/s/shop--api--s1');
  feed(w, 0, ['kept across readings']);
  const el1 = tail(w);
  for (let i = 0; i < 3; i++) { w.run('Nodes.M.recvAt = Date.now() - 70000'); await poll(w, { force: true }); }
  w.ctx.__ans = JSON.stringify({ status: 200, body: { nodes: nodes({ age_s: 30 }), at: iso(0) }, etag: 'W/"2"' });
  await poll(w, { force: true });
  assert.equal(subs(w).length, 1, 'one subscription however many readings');
  assert.equal(subs(w)[0].off, false);
  assert.equal(tail(w), el1, 'the same element');
  assert.equal(text(tail(w).querySelector('.nd-tail-pre')), 'kept across readings');
});

test('a gone keeps the lines and says why in plain words; the time is the last time lines arrived', async () => {
  const { w } = await tWorld();
  await go(w, '#/n/build-box/s/shop--api--s1');
  feed(w, 0, ['still here']);
  const at = new Date(2026, 9, 11, 9, 14).getTime();
  const gone = (reason, final) => feed(w, 0, ['still here'], { gone: { reason, message: '', final: !!final, seen: at } });
  gone('offline');
  assert.equal(text(tail().querySelector('.nd-tail-st')), 'offline, last seen 09:14');
  assert.equal(text(tail().querySelector('.nd-tail-pre')), 'still here', 'the lines stay');
  assert.ok(tail().classList.contains('gone'));
  const cases = { repair: 'build-box: needs a new pairing: its token was refused', revoked: 'build-box: the pair was removed', unpaired: 'build-box: answers as another node now', not_read_yet: 'build-box: has not been read yet',
    upstream_error: 'build-box: could not be reached, last seen 09:14', idle: 'build-box: the stream went quiet, last seen 09:14', closed: 'build-box: the stream ended, last seen 09:14', too_large: 'build-box: sent a line that was too long',
    shutdown: 'build-box: this board is stopping', 'something new': 'build-box: the stream ended, last seen 09:14' };
  for (const [reason, want] of Object.entries(cases)) { gone(reason, reason === 'repair'); assert.equal(text(tail().querySelector('.nd-tail-st')), want, reason); }
  feed(w, 0, ['alive again']);
  assert.ok(!tail().classList.contains('gone'));
  assert.equal(text(tail().querySelector('.nd-tail-st')), 'Live: updates as the session prints.');
  function tail() { return page(w).querySelector('.nd-tail'); }
});

test('no lines seen yet: a gone says so instead of a time', async () => {
  const { w } = await tWorld();
  await go(w, '#/n/build-box/s/shop--api--s1');
  feed(w, 0, [], { gone: { reason: 'offline', message: '', final: false, seen: 0 } });
  assert.equal(text(tail(w).querySelector('.nd-tail-st')), 'offline, no lines seen yet');
});

test('leaving the peek (the node page, another node, another page) ends the subscription; coming back subscribes again', async () => {
  const { w } = await tWorld();
  await go(w, '#/n/build-box/s/shop--api--s1');
  await go(w, '#/n/build-box');
  assert.deepEqual(subs(w), [{ ref: 'build-box/shop--api--s1', off: true }]);
  assert.equal(tail(w), null);
  await go(w, '#/n/build-box/s/shop--api--s1');
  assert.deepEqual(subs(w).map((s) => s.off), [true, false]);
  await go(w, '#/');
  assert.deepEqual(subs(w).map((s) => s.off), [true, true]);
});

test('without the sessions scope there is no Tail and no subscription; the controls that need it keep their reason', async () => {
  const { w } = await tWorld();
  await go(w, '#/n/read-only/s/shop--api--s1');
  assert.equal(tail(w), null);
  assert.deepEqual(subs(w), []);
  assert.deepEqual(all(page(w), '.nd-acts button.nd-off').map(text), ['Send a prompt', 'Answer', 'Close session']);
  assert.match(text(page(w).querySelector('.nd-whys')), /needs the sessions scope on read-only/);
});

test('a session that is not in the last reading has no tail; the page that says so is unchanged', async () => {
  const { w } = await tWorld();
  await go(w, '#/n/build-box/s/shop--api--s9');
  assert.equal(tail(w), null);
  assert.deepEqual(subs(w), []);
  assert.match(text(page(w)), /This session is not in the last reading of the node/);
});

test('hostile lines and a hostile node name are text: no element is made, the pre has no children', async () => {
  const { w } = await tWorld([rec('build-box', { scopes: ['read', 'sessions'], name: HOSTILE })]);
  await go(w, '#/n/build-box/s/shop--api--s1');
  feed(w, 0, [HOSTILE, '<script>alert(1)</script>', '\u001b[31mred\u001b[0m']);
  const pre = tail(w).querySelector('.nd-tail-pre');
  assert.equal(pre.children.length, 0);
  assert.ok(text(pre).includes(HOSTILE));
  feed(w, 0, [HOSTILE], { gone: { reason: 'repair', message: HOSTILE, final: true, seen: 0 } });
  assert.equal(text(tail(w).querySelector('.nd-tail-st')), `${HOSTILE}: needs a new pairing: its token was refused`);
  assert.equal(all(w, 'img, script, iframe').length, 0);
});

// ---------------------------------------------------------------- Start on <node> for a backlog task

const withBacklog = (over = {}) => {
  const n = rec('build-box', { scopes: ['read', 'tasks', 'sessions'], ...over });
  n.state.tasks = [...n.state.tasks, { id: 9, title: 'Write docs', phase: 'backlog', agent: 'claude', project: 'shop', repo: 'api', branch: '', tmux: null, issue_ref: null, updated_at: iso(60) }];
  return [n];
};

test('a backlog task of a node has Start on <node>, which opens the launcher in dispatch mode on that node with the task; other phases have none', async () => {
  const { w } = await tWorld(withBacklog());
  await go(w, '#/n/build-box/t/9');
  const b = all(page(w), '.nd-acts button').find((x) => text(x) === 'Start on build-box');
  assert.ok(b && !b.classList.contains('nd-off'));
  b.click();
  const o = plain(w.get('__launched'))[0];
  assert.equal(o.mode, 'dispatch');
  assert.equal(o.node, 'build-box');
  assert.equal(o.remoteTask.id, 9);
  assert.equal(o.remoteTask.node, 'build-box');
  await go(w, '#/n/build-box/t/7');
  assert.equal(all(page(w), '.nd-acts button').filter((x) => /^Start on/.test(text(x))).length, 0, 'a running task is not started again');
});

test('Start on <node> is off with the reason when the pair lacks the tasks scope or the node is offline', async () => {
  const { w } = await tWorld(withBacklog({ scopes: ['read'] }));
  await go(w, '#/n/build-box/t/9');
  const b = all(page(w), '.nd-acts button.nd-off').find((x) => text(x) === 'Start on build-box');
  assert.ok(b);
  assert.match(text(page(w).querySelector('.nd-whys')), /Start: needs the tasks scope on build-box\./);
  const off = await tWorld(withBacklog({ status: 'offline', age_s: 600, last_ok_at: iso(600) }));
  await go(off.w, '#/n/build-box/t/9');
  assert.match(text(page(off.w).querySelector('.nd-whys')), /Start: build-box is offline, last seen 10 minutes ago\./);
});

// ---------------------------------------------------------------- the optimistic rows

test('an optimistic task shows on the node page as "Starting on <node>" at once, turns started with the answer, and goes by itself when the reading carries the same id', async () => {
  const { w } = await tWorld();
  await go(w, '#/n/build-box');
  const rowsOf = () => all(page(w), '[data-sec=tasks] .nd-item').map((r) => text(r));
  assert.equal(rowsOf().length, 1);
  w.run("globalThis.__p = Nodes.pend({ kind: 'task', node: 'build-box', id: 'p1', title: 'New thing', phase: 'queued', agent: 'claude', project: 'shop', repo: 'api' })");
  assert.equal(rowsOf().length, 2);
  assert.match(rowsOf()[1], /New thing.*shop\/api.*Starting on build-box/);
  w.run("Nodes.pended(__p, { id: 44, phase: 'running', tmux: 'shop--api--t-new-thing' })");
  assert.match(rowsOf()[1], /running/);
  assert.doesNotMatch(rowsOf()[1], /Starting on/);
  assert.equal(all(page(w), '[data-sec=tasks] a.nd-name').length, 2, 'it links to its peek now that it has an id');
  // the reading carries the task: the optimistic row is gone, not shown twice
  const n = rec('build-box', { scopes: ['read', 'tasks'] });
  n.state.tasks = [...n.state.tasks, { id: 44, title: 'New thing', phase: 'running', agent: 'claude', project: 'shop', repo: 'api', branch: 'task/new-thing', tmux: 'shop--api--t-new-thing', issue_ref: null, updated_at: iso(1) }];
  w.ctx.__ans = JSON.stringify({ status: 200, body: { nodes: [n], at: iso(0) }, etag: 'W/"9"' });
  await poll(w, { force: true });
  assert.equal(rowsOf().length, 2, 'the real row, once');
  assert.equal(plain(w.get('Nodes.pending.length')), 0);
});

test('an optimistic row that the node refused is taken away, and one nobody confirmed goes after 3 minutes', async () => {
  const { w } = await tWorld();
  w.run("globalThis.__p = Nodes.pend({ kind: 'session', node: 'build-box', tmux: 'starting-1', session: 'x', project: 'shop', repo: 'api', agent: 'claude', state: 'unknown' })");
  assert.equal(plain(w.run("Nodes.sessions('build-box').length")), 3);
  assert.equal(plain(w.run("Nodes.sessions('build-box').filter((s) => s.pending === 'starting').length")), 1);
  w.run('Nodes.unpend(__p)');
  assert.equal(plain(w.run("Nodes.sessions('build-box').length")), 2);
  w.run("Nodes.pend({ kind: 'task', node: 'build-box', id: 'p2', title: 't', phase: 'queued' })");
  assert.equal(plain(w.run("Nodes.tasks().length")), 3, 'two real rows and the optimistic one');
  const later = Date.now() + 181000;
  w.run(`Date.now = () => ${later}`);
  assert.equal(plain(w.run("Nodes.tasks().length")), 2);
});

test('a task being dispatched stands in for its backlog row until the reading shows another phase', async () => {
  const { w } = await tWorld(withBacklog());
  w.run("globalThis.__p = Nodes.pend({ ...Nodes.tasks('build-box').find((t) => t.id === 9), kind: 'task', phase: 'queued', replace: true, was: 'backlog' })");
  const rows = plain(w.run("Nodes.tasks('build-box').filter((t) => t.id === 9)"));
  assert.equal(rows.length, 1, 'one row, not the backlog one and a second');
  assert.equal(rows[0].pending, 'starting');
  const n = withBacklog();
  n[0].state.tasks = n[0].state.tasks.map((t) => (t.id === 9 ? { ...t, phase: 'running' } : t));
  w.ctx.__ans = JSON.stringify({ status: 200, body: { nodes: n, at: iso(0) }, etag: 'W/"8"' });
  await poll(w, { force: true });
  const after = plain(w.run("Nodes.tasks('build-box').filter((t) => t.id === 9)"));
  assert.deepEqual([after.length, after[0].phase, after[0].pending], [1, 'running', undefined]);
});

// ---------------------------------------------------------------- Nodes.can, the words of a disabled start

test('Nodes.can names the first thing that stops an action, in the order the hub refuses', async () => {
  const { w } = await tWorld();
  const can = (over, scope) => { w.ctx.__r = JSON.stringify(rec('x', over)); return plain(w.run(`Nodes.can(JSON.parse(__r), ${JSON.stringify(scope)})`)); };
  assert.deepEqual(can({ scopes: ['read', 'tasks'] }, 'tasks'), { ok: true, why: '' });
  assert.deepEqual(can({ scopes: ['read', 'tasks'], status: 'stale', age_s: 190 }, 'tasks'), { ok: true, why: '' }, 'a stale reading is still called');
  assert.equal(can({ scopes: ['read'] }, 'tasks').why, 'needs the tasks scope on x');
  assert.equal(can({ scopes: ['read', 'tasks'], status: 'unauthorized' }, 'tasks').why, "x no longer takes this board's token: re-pair it in Settings, Nodes");
  assert.equal(can({ scopes: ['read', 'tasks'], status: 'unpaired' }, 'tasks').why, 'x answers as another node: remove it and pair it again');
  assert.equal(can({ scopes: ['read', 'tasks'], legacy: true }, 'tasks').why, 'x was added without a token: pair it to act on it');
  assert.match(can({ scopes: ['read', 'tasks'], status: 'offline', age_s: 3 * 3600 }, 'tasks').why, /^x is offline, last seen 3 hours ago$/);
  assert.match(can({ scopes: ['read', 'tasks'], status: 'offline', age_s: 1 }, 'tasks').why, /^x is offline, last seen 1 second ago$/);
  assert.equal(can({ scopes: ['read', 'tasks'], status: 'offline', age_s: null }, 'tasks').why, 'x has not answered yet');
  assert.equal(plain(w.run('Nodes.can(null, "tasks")')).ok, false);
});

// ---------------------------------------------------------------- the demo

test('demo mode: the relay\'s reads and writes are answered from demo/nodes.json, in the shapes the hub answers, and nothing leaves the page', async () => {
  const { w } = hubWorld({ search: '?demo=1', realApi: true });
  w.run("globalThis.__h = null");
  const ask = async (m, p, b) => { w.ctx.__a = [m, p, b]; return plain(await w.run('api(__a[0], __a[1], __a[2])')); };
  const agents = await ask('GET', '/api/nodes/build-box/agents');
  assert.equal(agents.node, 'build-box');
  assert.deepEqual(agents.data.agents.map((a) => a.name), ['claude', 'codex']);
  assert.deepEqual(agents.data.agents[0].models, ['opus', 'sonnet', 'haiku']);
  const tailRes = await ask('GET', '/api/nodes/build-box/sessions/shop--api--s1/pane');
  assert.ok(tailRes.data.lines.length > 3 && tailRes.data.lines.every((l) => typeof l === 'string'));
  const other = await ask('GET', '/api/nodes/build-box/sessions/shop--api--s3/pane');
  assert.deepEqual(other.data.lines, ['Done. The change is on branch task/orders-pagination.', 'What next?']);
  const made = await ask('POST', '/api/nodes/build-box/tasks', { project: 'shop', repo: 'api', title: 'Add a test', prompt: 'p', when: 'now', agent: 'claude' });
  assert.match(made.data.ref, /^build-box:\d+$/);
  assert.equal(made.data.phase, 'running');
  assert.equal(made.data.tmux, 'shop--api--t-add-a-test');
  assert.equal(made.data.limit_warning, undefined);
  const hot = await ask('POST', '/api/nodes/build-box/tasks', { project: 'shop', repo: 'api', title: 'Hot', prompt: 'p', when: 'now', agent: 'claude', effort: 'max' });
  assert.equal(hot.data.limit_warning.kind, '5h');
  const later = await ask('POST', '/api/nodes/build-box/tasks', { project: 'shop', repo: 'api', title: 'Later', prompt: 'p', when: 'later' });
  assert.deepEqual([later.data.phase, later.data.tmux], ['backlog', null]);
  const sess = await ask('POST', '/api/nodes/build-box/sessions', { project: 'shop', repo: 'web', agent: 'codex', name: 'try' });
  assert.deepEqual(sess.data, { ref: 'build-box/shop--web--try', tmux: 'shop--web--try', agent: 'codex', project: 'shop', repo: 'web' });
  for (const [m, p, b, status, reason] of [
    ['POST', '/api/nodes/build-box/tasks', { project: 'shop', repo: 'nope', title: 't', prompt: 'p' }, 404, 'repo_missing'],
    ['POST', '/api/nodes/alice-mac/tasks', { project: 'shop', repo: 'api', title: 't', prompt: 'p' }, 409, 'scope'],
    ['POST', '/api/nodes/old-laptop/tasks', { project: 'shop', repo: 'api', title: 't', prompt: 'p' }, 503, 'offline'],
    ['POST', '/api/nodes/build-box/tasks/999/dispatch', { mode: 'lane' }, 404, 'not_found'],
  ]) {
    w.ctx.__a = [m, p, b];
    const err = await w.run('api(__a[0], __a[1], __a[2]).then(() => null, (e) => e)');
    assert.ok(err, `${p} is refused`);
    assert.deepEqual([err.status, err.body.reason], [status, reason]);
  }
});

test('the demo fixture offers the picker a remote node to choose: online with tasks and sessions, one read only, one offline; its tails belong to its sessions', () => {
  const d = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'nodes.json'), 'utf8'));
  const by = Object.fromEntries(d.hub.map((r) => [r.handle, r]));
  assert.deepEqual(by['build-box'].scopes, ['read', 'tasks', 'sessions']);
  assert.deepEqual(by['alice-mac'].scopes, ['read']);
  assert.equal(by['old-laptop'].status, 'offline');
  assert.deepEqual(Object.keys(d.agents), ['build-box']);
  for (const k of Object.keys(d.tails)) if (k !== 'default') assert.ok(by[k.split('/')[0]].state.sessions.some((s) => s.tmux === k.split('/')[1]), `${k} is a session of the fixture`);
  assert.ok(!JSON.stringify([d.agents, d.tails]).match(/@|\/Users\/|\/home\/|—/), 'no e-mail address, personal path or em-dash');
});
