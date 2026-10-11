// Contract tests for issue #142 in the browser: steering a session on another node (the composer and its quick replies, Ack, the Keys panel, Kill session) on the remote session peek
// (app/static/pages/node.js), and answering a node's permission requests (the cards of the inbox group, the node page and the peek; the palette rows; the keys) with the model and
// the calls in app/static/nodes-hub.js. Real scripts on minidom's DOM (tests/js/hubworld.mjs); the board's relay is a scripted `api` that records every call. Nothing opens a
// connection. The secret-shaped strings are built by concatenation.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { boardState, hubWorld, iso, page, plain, poll, rec, setState, text, tick } from './hubworld.mjs';

const HOSTILE = '<img src=x onerror=alert(1)>';
const all = (n, sel) => (n.document || n).querySelectorAll(sel);
const err = (status, body) => Object.assign(new Error((body && body.error) || String(status)), { status, body });
const KEYS = ['Enter', 'Escape', 'Up', 'Down', 'Tab', 'y', 'n', '1', '2', '3', '4', '5', '6', '7', '8', '9'];
const PERMS = () => [
  { id: 41, tmux: 'shop--api--s1', tool: 'Bash', summary: 'git push origin task/orders', since: iso(300) },
  { id: 42, tmux: 'shop--api--s2', tool: 'Edit', summary: 'Edit infra/values.yaml', since: iso(120) },
];

/* A world with a scripted relay. o.scopes: the scopes of build-box; o.perms: the requests GET .../permissions answers (the reading says that many wait); o.board: this board's state.
   h.reply(method, path, body) may return a body, a promise, or throw err(status, body); undefined falls to the default answer of each route. h.log is every api call, in order. */
async function steerWorld(o = {}) {
  const build = rec('build-box', { scopes: o.scopes || ['read', 'tasks', 'sessions', 'permissions'], ...(o.rec || {}) });
  const perms = o.perms || [];
  build.state.needs_you.permissions = perms.length;
  const nodes = o.nodes || (o.alone ? [build] : [build, rec('alice-mac', { scopes: ['read'] })]);
  const h = hubWorld({ state: o.board || boardState(), nodes, search: o.search || '', realApi: !!o.demo });
  const { w } = h;
  h.log = [];
  h.perms = perms;
  h.reply = null;
  if (!o.demo) {
    w.ctx.__r = async (method, path, body) => {
      h.log.push({ method, path, body: body === undefined ? undefined : JSON.parse(JSON.stringify(body)) });
      if (h.reply) { const r = await h.reply(method, path, body); if (r !== undefined) return r; }
      if (method === 'GET' && /\/permissions$/.test(path)) return { node: 'build-box', age: 0, data: { permissions: h.perms } };
      if (/\/prompt$/.test(path)) return { node: 'build-box', age: 0, data: { ok: true, pasted: true, queued: !!(body && body.queue) } };
      if (/\/keys$/.test(path)) return { node: 'build-box', age: 0, data: { ok: true, key: body.key } };
      if (/\/ack$/.test(path)) return { node: 'build-box', age: 0, data: { acked: decodeURIComponent(path.split('/').slice(-2)[0]) } };
      if (method === 'DELETE') return { node: 'build-box', age: 0, data: { killed: decodeURIComponent(path.split('/').pop()) } };
      const m = /\/permissions\/(\d+)\/(allow|deny)$/.exec(path);
      if (m) return { node: 'build-box', age: 0, data: { ok: true, id: Number(m[1]), decision: m[2] } };
      return { ok: true };
    };
    w.run('api = (m, p, b) => __r(m, p, b)');
  }
  h.calls = (re) => h.log.filter((c) => !re || re.test(c.path));
  h.writes = () => h.log.filter((c) => c.method !== 'GET');
  h.toasts = () => plain(w.get('__toasts'));
  await poll(w);
  await tick();
  return h;
}
const go = async (w, hash) => { w.location.hash = hash; await tick(); await tick(); };
const S1 = '#/n/build-box/s/shop--api--s1';
const API = '/api/nodes/build-box/sessions/shop--api--s1';
const box = (w) => page(w).querySelector('.nd-send textarea');
const type = (ta, v) => { ta.value = v; ta.dispatchEvent({ type: 'input' }); };
const enter = (ta, over = {}) => ta.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() {}, ...over });
const st = (w) => text(page(w).querySelector('.nd-steer-st'));
const btn = (root, label) => all(root, 'button').find((b) => text(b) === label);
const flush = async (n = 6) => { for (let i = 0; i < n; i++) await tick(); };
const deferred = () => { let resolve; let reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const shapes = (w) => plain(w.get('__toasts')).map((t) => t.text);

// ---------------------------------------------------------------- the composer

test('Enter sends the typed line through the relay once: POST .../prompt with the text, an optimistic "Sending to" line first, the box emptied and "Sent to" only after the node confirmed', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const ta = box(w);
  assert.ok(ta, 'the peek of a node with the sessions scope has a send box');
  type(ta, 'run the tests');
  const d = deferred();
  h.reply = (m, p) => (/\/prompt$/.test(p) ? d.promise : undefined);
  enter(ta);
  await flush();
  assert.deepEqual(h.writes().map((c) => [c.method, c.path, c.body]), [['POST', `${API}/prompt`, { text: 'run the tests', queue: false }]]);
  assert.equal(st(w), 'Sending to build-box…', 'painted before the node answered');
  assert.equal(text(page(w).querySelector('.nd-steer-echo')), '› run the tests');
  assert.equal(ta.value, 'run the tests', 'the draft stays until the node confirmed');
  assert.equal(ta.disabled, true, 'locked meanwhile: a second Enter cannot send twice');
  enter(ta);
  assert.equal(h.writes().length, 1);
  assert.deepEqual(shapes(w).filter((t) => /Sent/.test(t)), [], 'no "Sent" before the answer');
  d.resolve({ node: 'build-box', age: 0, data: { ok: true, pasted: true, queued: false } });
  await flush();
  assert.equal(ta.value, '');
  assert.equal(ta.disabled, false);
  assert.equal(st(w), 'Sent to build-box.');
  assert.deepEqual(shapes(w), ['Sent to s1 on build-box']);
  assert.equal(page(w).querySelector('.nd-steer-st').classList.contains('ok'), true);
});

test('Shift+Enter adds a line and sends nothing; a multi-line text goes as one prompt with LF newlines', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const ta = box(w);
  type(ta, 'first');
  enter(ta, { shiftKey: true });
  assert.equal(ta.value, 'first\n');
  assert.equal(h.writes().length, 0);
  ta.value += 'second\r\nthird';
  enter(ta);
  await flush();
  assert.equal(h.writes()[0].body.text, 'first\nsecond\nthird');
});

test('the Send button sends the same way (the form submits), and it is the page\'s primary while Open on <node> is plain', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  type(box(w), 'go');
  page(w).querySelector('.nd-send').dispatchEvent({ type: 'submit', preventDefault() {} });
  await flush();
  assert.equal(h.writes().length, 1);
  const send = btn(page(w), 'Send');
  assert.ok(send.classList.contains('primary'));
  const open = all(page(w), 'a.btn').find((a) => text(a) === 'Open on build-box');
  assert.ok(!open.classList.contains('primary'), 'one filled primary: Send (tinted until there is text)');
});

test('a refusal says why in a plain sentence in the panel and in a toast, and keeps the draft: busy with the time to try again, scope, offline with the age, wider permissions, unconfirmed, too many prompts, too large, invalid, gone', async () => {
  const wide = 'that session runs with wider permissions than another node may use, or its mode cannot be read; start it from its own board';
  const cases = [
    [err(409, { error: 'the session is working', reason: 'busy', code: 'working', retry: 8, state: 'working', wait_kind: null, node: 'build-box' }), 'The session is working. Try again in 8 seconds.'],
    [err(409, { error: 'a permission request is waiting for an answer', reason: 'busy', code: 'permission_pending', retry: null }), 'A permission request is waiting for an answer. Waiting will not help.'],
    [err(409, { error: 'needs the sessions scope on build-box', reason: 'scope' }), 'needs the sessions scope on build-box'],
    [err(503, { error: 'build-box is offline', reason: 'offline', age: 190 }), 'build-box is offline, last seen 3 minutes ago'],
    [err(409, { error: wide, reason: 'refused' }), 'That session runs with wider permissions than another node may use, or its mode cannot be read; start it from its own board.'],
    [err(504, { error: 'build-box did not answer in 8 s', reason: 'unconfirmed' }), 'Could not confirm whether build-box got this. Check the terminal before answering again.'],
    [err(502, { error: 'the connection broke', reason: 'unconfirmed' }), 'Could not confirm whether build-box got this. Check the terminal before answering again.'],
    [Object.assign(new Error('Failed to fetch'), {}), 'Could not confirm whether build-box got this. Check the terminal before answering again.'],
    [err(429, { error: 'too many', reason: 'rate_limited' }), 'Too many prompts in a minute for this session. Try again in a moment.'],
    [err(413, { error: 'the body is over 8 KB', reason: 'too_large' }), 'The text is over 8 KB. Shorten it and send again.'],
    [err(422, { error: 'text: no control characters', reason: 'invalid' }), 'That text has a character a terminal cannot take (a control, format or line-separator character). Tab and newline are fine. Remove it and send again.'],
    [err(404, { error: 'session shop--api--s1 not found', reason: 'not_found' }), 'That session is gone from build-box.'],
    [err(422, { error: 'a permission bypass is not allowed from another node', reason: 'invalid' }), 'A permission bypass is not allowed from another node.'],
    [err(409, { error: 'build-box no longer takes this board\'s token', reason: 'needs_repair' }), 'build-box no longer takes this board\'s token: re-pair it in Settings, Nodes'],
    [err(409, { error: 'x', reason: 'not_read_yet' }), 'build-box has not been read yet: wait a few seconds and try again'],
  ];
  for (const [e, want] of cases) {
    const h = await steerWorld();
    const { w } = h;
    await go(w, S1);
    const ta = box(w);
    type(ta, 'keep me');
    h.reply = (m, p) => { if (/\/prompt$/.test(p)) throw e; return undefined; };
    enter(ta);
    await flush();
    assert.equal(st(w), want, want);
    assert.equal(ta.value, 'keep me', `the draft survives: ${want}`);
    assert.equal(ta.disabled, false);
    assert.equal(h.writes().length, 1, 'nothing is retried');
    assert.deepEqual(shapes(w), [want], 'a toast says the same thing');
    assert.equal(text(page(w).querySelector('.nd-steer-echo')), '', 'the optimistic echo is taken back');
  }
});

test('the box refuses what no node would take, before any call: empty, a control, format or line-separator character, a slash command, more than 8192 characters', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const ta = box(w);
  const bad = {
    '   ': 'Nothing to send.',
    ['a' + String.fromCharCode(27) + '[2J']: 'That text has a character a terminal cannot take',
    ['x' + String.fromCharCode(0x202e) + 'y']: 'That text has a character a terminal cannot take',
    ['x' + String.fromCharCode(0x2028) + 'y']: 'That text has a character a terminal cannot take',
    ['x' + String.fromCharCode(0x200b) + 'y']: 'That text has a character a terminal cannot take',
    ['x' + String.fromCharCode(0) + 'y']: 'That text has a character a terminal cannot take',
    '/compact': 'Slash commands are not sent to another node.',
    '  /status': 'Slash commands are not sent to another node.',
    ['a'.repeat(8193)]: 'The text is 8193 characters; a prompt can be up to 8 KB',
  };
  for (const [t, why] of Object.entries(bad)) {
    type(ta, t);
    enter(ta);
    await flush();
    assert.ok(st(w).startsWith(why), `${JSON.stringify(t.slice(0, 12))} -> ${st(w)}`);
    assert.equal(ta.value, t, 'kept');
  }
  assert.equal(h.writes().length, 0, 'no call for any of them');
  type(ta, 'tab\tand newline\nare text ' + 'é'.repeat(8100));
  enter(ta);
  await flush();
  assert.equal(h.writes().length, 1, 'tab and newline are text; 8192 characters or fewer pass');
  type(ta, 'a'.repeat(8192));
  enter(ta);
  await flush();
  assert.equal(h.writes().length, 2);
});

test('a busy session offers "Queue it": the same text again with queue true, and the toast says Queued, not Sent', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const ta = box(w);
  type(ta, 'after this');
  h.reply = (m, p, b) => { if (/\/prompt$/.test(p) && !b.queue) throw err(409, { error: 'the session is working', reason: 'busy', code: 'working', retry: 8 }); return undefined; };
  enter(ta);
  await flush();
  const q = btn(page(w), 'Queue it');
  assert.ok(q, 'offered while the session is working');
  q.click();
  await flush();
  assert.deepEqual(h.writes().map((c) => c.body), [{ text: 'after this', queue: false }, { text: 'after this', queue: true }]);
  assert.equal(st(w), 'Queued on build-box. It is typed when the session is ready.');
  assert.deepEqual(shapes(w).slice(-1), ['Queued for s1 on build-box']);
  assert.equal(ta.value, '');
  assert.equal(btn(page(w), 'Queue it'), undefined);
});

test('a node that answers 2xx without ok:true is not "Sent": it is reported as unconfirmed and the draft stays', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const ta = box(w);
  type(ta, 'hello');
  h.reply = (m, p) => (/\/prompt$/.test(p) ? { node: 'build-box', age: 0, data: {} } : undefined);
  enter(ta);
  await flush();
  assert.match(st(w), /^Could not confirm whether build-box got this/);
  assert.equal(ta.value, 'hello');
});

// ---------------------------------------------------------------- quick replies

test('the quick replies are the agent\'s own list (Claude\'s here), kept per node and session; a tap sends that text and leaves the draft in the box alone; a slash command is never a chip', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const chips = all(page(w), '.nd-chips .chip-btn');
  assert.deepEqual(chips.map(text), ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it']);
  type(box(w), 'my draft');
  chips[0].click();
  await flush();
  assert.deepEqual(h.writes().map((c) => c.body), [{ text: 'continue', queue: false }]);
  assert.equal(box(w).value, 'my draft', 'the draft is not touched');
  assert.deepEqual(shapes(w), ['Sent to s1 on build-box']);
  await go(w, '#/n/build-box/s/shop--api--s2');
  assert.deepEqual(all(page(w), '.nd-chips .chip-btn').map(text), ['continue'], 'Codex: its defaults minus the slash commands (/status, /compact, /new stay in the editor\'s list)');
  w.run("quickSave({ tmux: 'shop--api--s1', node: 'build-box' }, ['ship it', '/compact'], 'claude')");
  await go(w, S1);
  assert.deepEqual(all(page(w), '.nd-chips .chip-btn').map(text), ['ship it']);
  assert.equal(w.run("localStorage.getItem('ccboard:quick:build-box/shop--api--s1')") !== null, true, 'the list is stored under the node and the session');
  assert.equal(w.run("localStorage.getItem('ccboard:quick:shop--api--s1')"), null, 'this board\'s list of the same name is a different list');
});

test('a quick reply that the node refuses says why and nothing else is sent; two taps while it is out send once', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const d = deferred();
  h.reply = (m, p) => (/\/prompt$/.test(p) ? d.promise : undefined);
  const [chip] = all(page(w), '.nd-chips .chip-btn');
  chip.click();
  chip.click();
  await flush();
  assert.equal(h.writes().length, 1);
  d.reject(err(409, { error: 'the session is compacting', reason: 'busy', code: 'compacting', retry: 30 }));
  await flush();
  assert.equal(st(w), 'The session is compacting. Try again in 30 seconds.');
});

// ---------------------------------------------------------------- Ack, Keys, Kill session

test('Ack: offered while the session needs you, paints at once (the button goes, the node page stops counting it) and calls .../ack; a refusal puts it back with the sentence', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const ack = btn(page(w), 'Ack');
  assert.ok(ack, 'shop--api--s1 needs you');
  assert.match(ack.getAttribute('title'), /answers no request/);
  const d = deferred();
  h.reply = (m, p) => (/\/ack$/.test(p) ? d.promise : undefined);
  ack.click();
  await flush();
  assert.deepEqual(h.writes().map((c) => [c.method, c.path, c.body]), [['POST', `${API}/ack`, undefined]]);
  assert.equal(btn(page(w), 'Ack'), undefined, 'gone before the node answered');
  assert.equal(w.run("Nodes.sessions('build-box').find((s) => s.tmux === 'shop--api--s1').needs_you"), false);
  d.resolve({ node: 'build-box', age: 0, data: { acked: 'shop--api--s1' } });
  await flush();
  assert.equal(st(w), 'Acknowledged on build-box.');
  assert.deepEqual(shapes(w), ['Acknowledged s1 on build-box']);
  await go(w, '#/n/build-box');
  assert.equal(all(page(w), '[data-sec=needs] .nd-item').length, 0, 'the node page does not list it as needing you');
  // a newer reading is the truth again
  await poll(w, { force: true });
  assert.equal(all(page(w), '[data-sec=needs] .nd-item').length, 1);
  // a refusal
  await go(w, S1);
  h.reply = (m, p) => { if (/\/ack$/.test(p)) throw err(404, { error: 'not found', reason: 'not_found' }); return undefined; };
  btn(page(w), 'Ack').click();
  await flush();
  assert.ok(btn(page(w), 'Ack'), 'put back');
  assert.equal(st(w), 'That session is gone from build-box.');
  assert.equal(w.run("Nodes.sessions('build-box').find((s) => s.tmux === 'shop--api--s1').needs_you"), true);
});

test('a session that does not need you has no Ack; a shell or an ended session has no send box and no Keys', async () => {
  const b = rec('build-box', { scopes: ['read', 'tasks', 'sessions', 'permissions'] });
  b.state.sessions = [
    { tmux: 'shop--api--s2', project: 'shop', repo: 'api', session: 's2', agent: 'codex', state: 'working', needs_you: false, kind: 'task', since: iso(60), model: 'gpt-5.5' },
    { tmux: 'shop--api--sh', project: 'shop', repo: 'api', session: 'sh', agent: 'shell', state: 'idle', needs_you: false, kind: 'user', since: iso(60), model: null },
    { tmux: 'shop--api--s9', project: 'shop', repo: 'api', session: 's9', agent: 'claude', state: 'ended', needs_you: false, kind: 'user', since: iso(60), model: null },
  ];
  const h = await steerWorld({ nodes: [b] });
  const { w } = h;
  await go(w, '#/n/build-box/s/shop--api--s2');
  assert.equal(btn(page(w), 'Ack'), undefined);
  assert.ok(box(w) && btn(page(w), 'Keys'));
  await go(w, '#/n/build-box/s/shop--api--sh');
  assert.equal(btn(page(w), 'Keys'), undefined, 'a shell takes no keys from another node');
  assert.ok(page(w).querySelector('.nd-reply').classList.contains('hidden'));
  assert.match(text(page(w).querySelector('.nd-steer')), /cannot take a prompt: it is a shell or it has ended/);
  assert.ok(btn(page(w), 'Kill session on build-box'));
  await go(w, '#/n/build-box/s/shop--api--s9');
  assert.equal(btn(page(w), 'Keys'), undefined);
});

test('the Keys panel lists exactly the allow list (Enter, Esc, Up, Down, Tab, y, n, 1 to 9) and C-c; a key posts its name and nothing else; nothing is a key outside the list', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const keys = btn(page(w), 'Keys');
  assert.equal(keys.getAttribute('aria-expanded'), 'false');
  assert.ok(page(w).querySelector('.nd-keys').classList.contains('hidden'));
  keys.click();
  assert.equal(btn(page(w), 'Keys').getAttribute('aria-expanded'), 'true');
  const cells = all(page(w), '.nd-keys button');
  assert.deepEqual(cells.slice(0, -1).map((b) => b.getAttribute('data-key')), KEYS, 'the closed list, in order');
  assert.deepEqual(cells.slice(0, -1).map(text), ['Enter', 'Esc', 'Up', 'Down', 'Tab', 'y', 'n', '1', '2', '3', '4', '5', '6', '7', '8', '9']);
  assert.equal(text(cells[cells.length - 1]), 'C-c');
  assert.ok(cells[cells.length - 1].classList.contains('danger'), 'C-c rests red-outlined');
  for (const b of cells.slice(0, -1)) { b.click(); await flush(); }
  assert.deepEqual(h.writes().map((c) => [c.path, c.body]), KEYS.map((k) => [`${API}/keys`, { key: k }]), 'every cell posts {key}; none posts confirm');
  assert.equal(st(w), 'Sent 9 to build-box.');
  assert.deepEqual(shapes(w).slice(0, 2), ['Sent Enter to s1 on build-box', 'Sent Esc to s1 on build-box']);
  // the model refuses a key that is not on the list, before any call
  const r = plain(await w.run("Nodes.sendKey('build-box', 'shop--api--s1', 'Left', false)"));
  assert.deepEqual([r.ok, r.local], [false, true]);
  for (const bad of ['Left', 'C-u', 'Space', 'BSpace', 'Escape ', '$(x)', 'c-c', '']) {
    const x = plain(await w.run(`Nodes.sendKey('build-box', 'shop--api--s1', ${JSON.stringify(bad)}, true)`));
    assert.equal(x.ok, false, bad);
  }
  assert.equal(h.writes().length, 16, 'no call for a key outside the list');
});

test('C-c needs two taps: the first arms "Confirm C-c" with a Cancel and sends nothing; Cancel sends nothing; the second sends {key: C-c, confirm: true}', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  btn(page(w), 'Keys').click();
  const cc = () => btn(page(w).querySelector('.nd-keys'), 'C-c');
  cc().click();
  await flush();
  assert.equal(h.writes().length, 0, 'the first tap sends nothing');
  const labels = all(page(w).querySelector('.nd-ckey'), 'button').map(text);
  assert.deepEqual(labels, ['Confirm C-c', 'Cancel']);
  btn(page(w).querySelector('.nd-ckey'), 'Cancel').click();
  await flush();
  assert.equal(h.writes().length, 0, 'Cancel sends nothing');
  assert.ok(cc());
  cc().click();
  btn(page(w).querySelector('.nd-ckey'), 'Confirm C-c').click();
  await flush(10);
  assert.deepEqual(h.writes().map((c) => [c.path, c.body]), [[`${API}/keys`, { key: 'C-c', confirm: true }]]);
  assert.equal(st(w), 'Sent C-c to build-box.');
  const direct = plain(await w.run("Nodes.sendKey('build-box', 'shop--api--s1', 'C-c', false)"));
  assert.deepEqual([direct.ok, direct.local], [false, true], 'the model itself refuses C-c without the confirmation');
  assert.equal(h.writes().length, 1);
});

test('a key the node refuses says why (wider permissions); Esc inside the Keys panel closes it', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  btn(page(w), 'Keys').click();
  h.reply = (m, p) => { if (/\/keys$/.test(p)) throw err(409, { error: 'that session runs with wider permissions than another node may use', reason: 'refused' }); return undefined; };
  all(page(w), '.nd-keys button')[0].click();
  await flush();
  assert.equal(st(w), 'That session runs with wider permissions than another node may use.');
  page(w).querySelector('.nd-keys').dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {}, stopPropagation() {} });
  await flush();
  assert.ok(page(w).querySelector('.nd-keys').classList.contains('hidden'));
  assert.equal(btn(page(w), 'Keys').getAttribute('aria-expanded'), 'false');
});

test('Kill session takes two taps: the first arms "Confirm Kill session on build-box" and calls nothing, Cancel calls nothing, the second sends DELETE, names the node in a toast and goes to the node page', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const kill = btn(page(w), 'Kill session on build-box');
  assert.ok(kill.classList.contains('danger') && !kill.classList.contains('confirm'), 'rests red-outlined, not filled');
  kill.click();
  await flush();
  assert.equal(h.writes().length, 0);
  assert.deepEqual(all(page(w).querySelector('.nd-kill'), 'button').map(text), ['Confirm Kill session on build-box', 'Cancel']);
  btn(page(w), 'Cancel').click();
  await flush();
  assert.equal(h.writes().length, 0);
  assert.ok(btn(page(w), 'Kill session on build-box'));
  btn(page(w), 'Kill session on build-box').click();
  const d = deferred();
  h.reply = (m) => (m === 'DELETE' ? d.promise : undefined);
  btn(page(w), 'Confirm Kill session on build-box').click();
  await flush();
  assert.deepEqual(h.writes().map((c) => [c.method, c.path]), [['DELETE', API]]);
  assert.equal(st(w), 'Closing s1 on build-box…', 'pending at once');
  assert.deepEqual(shapes(w), [], 'the toast waits for the node');
  d.resolve({ node: 'build-box', age: 0, data: { killed: 'shop--api--s1' } });
  await flush();
  assert.deepEqual(shapes(w), ['Killed s1 on build-box']);
  assert.equal(w.location.hash, '#/n/build-box', 'the peek is gone with the session');
  assert.equal(w.run("Nodes.sessions('build-box').some((s) => s.tmux === 'shop--api--s1')"), false, 'left out of the lists until a newer reading');
});

test('a Kill the node refuses leaves the session and the peek alone and says why', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  h.reply = (m) => { if (m === 'DELETE') throw err(404, { error: 'session not found', reason: 'not_found' }); return undefined; };
  btn(page(w), 'Kill session on build-box').click();
  btn(page(w), 'Confirm Kill session on build-box').click();
  await flush();
  assert.equal(w.location.hash, S1);
  assert.equal(st(w), 'That session is gone from build-box.');
  assert.ok(btn(page(w), 'Kill session on build-box'), 'ready for another try');
  assert.equal(w.run("Nodes.sessions('build-box').some((s) => s.tmux === 'shop--api--s1')"), true);
});

// ---------------------------------------------------------------- scopes, offline, and a peek that keeps its state

test('without the sessions scope the peek keeps the disabled Send a prompt / Answer / Close session with their reason and has no send box, no Keys, no Ack', async () => {
  const h = await steerWorld({ scopes: ['read'] });
  const { w } = h;
  await go(w, S1);
  assert.equal(box(w), null);
  assert.equal(page(w).querySelector('.nd-steer'), null);
  assert.deepEqual(all(page(w), '.nd-acts button.nd-off').map(text), ['Send a prompt', 'Answer', 'Close session']);
  assert.match(text(page(w).querySelector('.nd-whys')), /Send a prompt: needs the sessions scope on build-box\./);
  assert.equal(h.log.length, 0, 'nothing was asked of the node');
});

test('with the scope but a node that is offline, the panel is there and every control is disabled with the reason and the age; a press sends nothing and says it', async () => {
  const h = await steerWorld({ rec: { status: 'offline', age_s: 21600, last_ok_at: iso(21600) } });
  const { w } = h;
  await go(w, S1);
  const why = 'build-box is offline, last seen 6 hours ago';
  assert.ok(box(w), 'the draft box is there');
  const offs = all(page(w).querySelector('.nd-steer'), 'button.nd-off');
  assert.deepEqual(offs.map(text), ['Send', 'Ack', 'Keys', 'Kill session']);
  for (const b of offs) { assert.equal(b.getAttribute('aria-disabled'), 'true'); assert.equal(b.disabled, false, 'focusable, so the reason can be read'); }
  assert.match(text(page(w).querySelector('.nd-steer .nd-swhys')), new RegExp(`Reply, Keys, Ack and Kill session: ${why}\\.`));
  type(box(w), 'hello');
  enter(box(w));
  await flush();
  assert.equal(st(w), why);
  assert.equal(box(w).value, 'hello');
  btn(page(w), 'Kill session').click();
  await flush();
  assert.equal(h.writes().length, 0);
  assert.equal(h.calls().filter((c) => /sessions/.test(c.path)).length, 0);
});

test('a reading every 6 s repaints the facts but not the panel: the same box with its draft and focus, an armed Kill, an open Keys panel', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  const ta = box(w);
  type(ta, 'half a thought');
  ta.focus();
  btn(page(w), 'Keys').click();
  btn(page(w), 'Kill session on build-box').click();
  const panel = page(w).querySelector('.nd-steer');
  const facts = page(w).querySelector('.nd-facts-dl');
  for (let i = 0; i < 3; i++) { w.run('Nodes.M.recvAt = Date.now() - 70000'); await poll(w, { force: true }); await tick(); }
  w.ctx.__ans = JSON.stringify({ status: 200, body: { nodes: [rec('build-box', { scopes: ['read', 'tasks', 'sessions', 'permissions'], age_s: 30 })], at: iso(0) }, etag: 'W/"9"' });
  await poll(w, { force: true });
  await tick();
  assert.equal(page(w).querySelector('.nd-steer'), panel, 'the same panel');
  assert.equal(box(w), ta, 'the same textarea');
  assert.equal(ta.value, 'half a thought');
  assert.equal(w.document.activeElement, ta, 'and it keeps the focus');
  assert.equal(page(w).querySelector('.nd-keys').classList.contains('hidden'), false, 'the Keys panel stays open');
  assert.ok(btn(page(w), 'Confirm Kill session on build-box'), 'the armed Kill stays armed');
  assert.notEqual(page(w).querySelector('.nd-facts-dl'), facts, 'the facts were repainted');
  assert.equal(all(page(w), '.nd-tail').length, 1);
});

test('leaving the peek stops it: the keys go quiet and nothing is left registered', async () => {
  const h = await steerWorld();
  const { w } = h;
  await go(w, S1);
  assert.equal(w.run('!!Nodes.peek && Nodes.peek.tmux'), 'shop--api--s1');
  await go(w, '#/n/build-box');
  assert.equal(w.run('Nodes.peek'), null);
});

// ---------------------------------------------------------------- permission requests: the cards

const noLocal = () => boardState({ projects: [] });
const sec = (w) => w.document.querySelector('.nd-inbox');
const cards = (w) => all(sec(w), '.nd-pcard');
const cardOf = (w, id) => cards(w).find((c) => c.getAttribute('data-key') === `build-box/${id}`);

test('the inbox group "On other nodes" shows a permission card per request: the kind, the tool and the summary as text, the session, the node chip, how long it waits, Allow and Deny, and the caption that says what the node trusts', async () => {
  const h = await steerWorld({ perms: PERMS(), board: noLocal(), alone: true });
  const { w } = h;
  await go(w, '#/inbox');
  assert.deepEqual(h.calls().map((c) => [c.method, c.path]), [['GET', '/api/nodes/build-box/permissions']], 'one read of the list');
  assert.match(text(sec(w).querySelector('h2')), /^On other nodes \(2\)$/);
  assert.deepEqual(cards(w).map((c) => c.getAttribute('data-key')), ['build-box/41', 'build-box/42'], 'oldest first');
  const c = cardOf(w, 41);
  assert.equal(text(c.querySelector('.ib-kind')), 'permission');
  assert.equal(text(c.querySelector('.ib-ctx')), 'git push origin task/orders');
  assert.ok(c.querySelector('.ib-ctx').classList.contains('mono'));
  assert.equal(text(c.querySelector('.ib-note')), 'Bash');
  assert.equal(c.querySelector('a.ib-name').getAttribute('href'), '#/n/build-box/s/shop--api--s1');
  assert.equal(text(c.querySelector('a.ib-name')), 's1');
  assert.equal(text(c.querySelector('.nchip-n')), 'build-box');
  assert.match(text(c), /waiting 5m/);
  assert.deepEqual(all(c, 'button').map(text), ['Allow', 'Deny']);
  assert.match(text(sec(w)), /Allow and Deny go to the node through this board\. The node trusts this board to pass on a person's choice and records the name this board reports; it cannot check it\./);
  assert.equal(all(sec(w), '.nd-item').length, 0, 'the two sessions that wait on these requests are the cards, not a second row');
  assert.ok(!/Read only here/.test(text(sec(w))));
});

test('one filled primary: Allow is filled only on the lead card, which is the first card when no local card leads; every other Allow is tinted and Deny is only bordered', async () => {
  const h = await steerWorld({ perms: PERMS(), board: noLocal(), alone: true });
  const { w } = h;
  await go(w, '#/inbox');
  const allow = (id) => btn(cardOf(w, id), 'Allow');
  assert.deepEqual([allow(41).classList.contains('primary'), allow(41).classList.contains('tinted')], [true, false]);
  assert.deepEqual([allow(42).classList.contains('primary'), allow(42).classList.contains('tinted')], [true, true]);
  for (const id of [41, 42]) { const d = btn(cardOf(w, id), 'Deny'); assert.ok(!d.classList.contains('primary') && !d.classList.contains('danger')); }
  const filled = () => all(sec(w), 'button').filter((b) => b.classList.contains('primary') && !b.classList.contains('tinted'));
  assert.equal(filled().length, 1, 'one filled primary in the group');
  // with a local card on the page, this board's card leads and none of these is filled
  setState(w, boardState());
  w.run('updateCurrentPage(state)');
  await tick();
  assert.equal(filled().length, 0);
  assert.equal(all(sec(w), 'button').filter((b) => b.classList.contains('primary') && b.classList.contains('tinted')).length, 2);
});

test('Home shows the same cards under its own inbox, and Allow is one tap from there: it posts allow for that request id, with no body', async () => {
  const h = await steerWorld({ perms: PERMS(), alone: true });
  const { w } = h;
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  await tick();
  assert.ok(sec(w), 'the group is on Home');
  assert.equal(cards(w).length, 2);
  btn(cardOf(w, 42), 'Allow').click();
  await flush();
  assert.deepEqual(h.writes().map((c) => [c.method, c.path, c.body]), [['POST', '/api/nodes/build-box/permissions/42/allow', undefined]]);
});

test('Allow paints a pending line at once ("Allowing on build-box…", no buttons, so no second answer), says "Allowed on build-box" only when the node confirmed, and the card does not come back from a reading made before the answer', async () => {
  const h = await steerWorld({ perms: PERMS(), board: noLocal(), alone: true });
  const { w } = h;
  await go(w, '#/inbox');
  const d = deferred();
  h.reply = (m, p) => (/permissions\/41\/allow$/.test(p) ? d.promise : undefined);
  btn(cardOf(w, 41), 'Allow').click();
  await flush();
  const pend = cardOf(w, 41);
  assert.match(text(pend.querySelector('.ib-ctx')), /^Allowing on build-box…$/);
  assert.equal(all(pend, 'button').length, 0, 'no buttons while it is out');
  assert.ok(pend.classList.contains('pending'));
  assert.deepEqual(shapes(w), [], 'nothing is said before the node answers');
  assert.equal(h.writes().length, 1);
  d.resolve({ node: 'build-box', age: 0, data: { ok: true, id: 41, decision: 'allow' } });
  await flush();
  assert.deepEqual(shapes(w), ['Allowed on build-box']);
  assert.equal(plain(w.get('__toasts'))[0].kind, 'ok');
  assert.equal(cardOf(w, 41), undefined, 'gone');
  assert.deepEqual(cards(w).map((c) => c.getAttribute('data-key')), ['build-box/42']);
  await poll(w, { force: true });
  await flush();
  assert.equal(cardOf(w, 41), undefined, 'the list still names it (a reading made before the answer): it stays gone');
  assert.equal(h.writes().length, 1);
});

test('Deny posts deny and says "Denied on build-box"', async () => {
  const h = await steerWorld({ perms: PERMS(), board: noLocal(), alone: true });
  const { w } = h;
  await go(w, '#/inbox');
  btn(cardOf(w, 42), 'Deny').click();
  await flush();
  assert.deepEqual(h.writes().map((c) => c.path), ['/api/nodes/build-box/permissions/42/deny']);
  assert.deepEqual(plain(w.get('__toasts')).map((t) => [t.text, t.kind]), [['Denied on build-box', 'warn']]);
  assert.equal(cardOf(w, 42), undefined);
});

test('a refusal puts the card back with the sentence and its buttons: offline, scope, rate limit, unconfirmed (never retried); a second try is possible', async () => {
  const cases = [
    [err(503, { error: 'build-box is offline', reason: 'offline', age: 65 }), 'build-box is offline, last seen 1 minute ago'],
    [err(409, { error: 'needs the permissions scope on build-box', reason: 'scope' }), 'needs the permissions scope on build-box'],
    [err(429, { error: 'too many', reason: 'rate_limited' }), 'Too many requests in a minute. Try again in a moment.'],
    [err(504, { error: 'build-box did not answer', reason: 'unconfirmed' }), 'Could not confirm whether build-box got this. Check the terminal before answering again.'],
    [Object.assign(new Error('Failed to fetch'), {}), 'Could not confirm whether build-box got this. Check the terminal before answering again.'],
  ];
  for (const [e, want] of cases) {
    const h = await steerWorld({ perms: PERMS(), board: noLocal(), alone: true });
    const { w } = h;
    await go(w, '#/inbox');
    h.reply = (m, p) => { if (/permissions\/41\/allow$/.test(p)) throw e; return undefined; };
    btn(cardOf(w, 41), 'Allow').click();
    await flush();
    const c = cardOf(w, 41);
    assert.ok(c, `back: ${want}`);
    assert.equal(text(c.querySelector('.nd-perr')), want);
    assert.deepEqual(all(c, 'button').map(text), ['Allow', 'Deny'], 'ready for another try');
    assert.equal(h.writes().length, 1, 'nothing was retried');
    assert.deepEqual(shapes(w), [want]);
    assert.equal(plain(w.get('__toasts'))[0].kind, 'bad');
    h.reply = null;
    btn(c, 'Deny').click();
    await flush();
    assert.equal(h.writes().length, 2);
  }
});

test('an answered, expired or gone request stops being actionable and says so: no buttons, the sentence, and it leaves by itself', async () => {
  const cases = [
    [err(409, { error: 'x', reason: 'answered' }), 'This request was already answered. Open terminal to see the current prompt.'],
    [err(409, { error: 'x', reason: 'expired' }), 'This request has expired. Open terminal to see the current prompt.'],
    [err(404, { error: 'x', reason: 'gone' }), 'This request is gone. Open terminal to see the current prompt.'],
  ];
  for (const [e, want] of cases) {
    const h = await steerWorld({ perms: PERMS(), board: noLocal(), alone: true });
    const { w } = h;
    await go(w, '#/inbox');
    h.reply = (m, p) => { if (/permissions\/41\/deny$/.test(p)) throw e; return undefined; };
    btn(cardOf(w, 41), 'Deny').click();
    await flush();
    const c = cardOf(w, 41);
    assert.equal(text(c.querySelector('.nd-perr')), want);
    assert.equal(all(c, 'button').length, 0, 'nothing to press');
    assert.ok(c.classList.contains('closed'));
    assert.ok(all(c, 'a.btn').some((a) => text(a) === 'Open on build-box'), 'a way to the node\'s own board');
    assert.deepEqual(shapes(w), [want]);
    w.run('for (const v of Nodes.P.st.values()) v.at -= 11000; Nodes.changed(false)');
    await flush();
    assert.equal(cardOf(w, 41), undefined, 'gone after a few seconds');
  }
});

test('Allow and Deny are never sent for a made-up decision or id: the model refuses always, tui, a rule, a case variant, a bad id; it never calls', async () => {
  const h = await steerWorld({ perms: PERMS() });
  const { w } = h;
  for (const dec of ['always', 'tui', 'Allow', 'ALLOW', 'allow ', 'allow/../x', '', 'deny;', 'rule']) {
    w.ctx.__d = dec;
    const r = plain(await w.run("Nodes.permAnswer({ node: 'build-box', id: 41, tmux: 'shop--api--s1', key: 'build-box/41' }, __d)"));
    assert.equal(r.ok, false, JSON.stringify(dec));
  }
  for (const id of [0, -1, 1.5, NaN, '41; x', '41', null, undefined, 2 ** 60]) {
    w.ctx.__i = id;
    const r = plain(await w.run("Nodes.permAnswer({ node: 'build-box', id: __i, tmux: 'shop--api--s1' }, 'allow')"));
    assert.equal(r.ok, false, String(id));
  }
  const bad = plain(await w.run("Nodes.permAnswer({ node: '../x', id: 41 }, 'allow')"));
  assert.equal(bad.ok, false);
  assert.equal(await w.run("Nodes.permAnswer({ stub: true, node: 'build-box', id: 41 }, 'allow').then((r) => r.ok)"), false);
  assert.equal(h.writes().length, 0, 'not one call');
});

test('a node that answers 2xx with another decision than the one asked is not "Allowed": it is shown as unconfirmed', async () => {
  const h = await steerWorld({ perms: PERMS(), board: noLocal(), alone: true });
  const { w } = h;
  await go(w, '#/inbox');
  h.reply = (m, p) => (/permissions\/41\/allow$/.test(p) ? { node: 'build-box', age: 0, data: { ok: true, id: 41, decision: 'deny' } } : undefined);
  btn(cardOf(w, 41), 'Allow').click();
  await flush();
  assert.match(text(cardOf(w, 41).querySelector('.nd-perr')), /^Could not confirm whether build-box got this/);
  assert.deepEqual(shapes(w).filter((t) => /^Allowed/.test(t)), []);
});

test('peer text is only text: a tool, a summary and a session name that look like markup are shown as they are, and no element is made from them', async () => {
  const perms = [{ id: 41, tmux: 'shop--api--s1', tool: HOSTILE, summary: HOSTILE + ' rm -rf /', since: iso(10) }];
  const b = rec('build-box', { name: HOSTILE, scopes: ['read', 'tasks', 'sessions', 'permissions'] });
  b.state.sessions[0].session = HOSTILE;
  b.state.needs_you.permissions = 1;
  const h = await steerWorld({ perms, nodes: [b], board: noLocal() });
  const { w } = h;
  await go(w, '#/inbox');
  assert.equal(text(cardOf(w, 41).querySelector('.ib-ctx')), HOSTILE + ' rm -rf /');
  assert.equal(text(cardOf(w, 41).querySelector('a.ib-name')), HOSTILE);
  assert.equal(all(w, 'img, iframe, script').length, 0);
  await go(w, S1);
  assert.equal(all(w, 'img, iframe, script').length, 0);
  btn(page(w), 'Allow').click();
  await flush();
  assert.deepEqual(shapes(w), [`Allowed on ${HOSTILE}`], 'a toast is text too');
});

// ---------------------------------------------------------------- permission requests: who is asked, and when

test('the list is read only from a node whose pair holds the permissions scope AND whose reading says a request waits; once per node at a time; never while the tab is hidden', async () => {
  const waiting = rec('build-box', { scopes: ['read', 'tasks', 'sessions', 'permissions'] });
  waiting.state.needs_you.permissions = 1;
  const quiet = rec('quiet', { scopes: ['read', 'permissions'] });
  quiet.state.needs_you.permissions = 0;
  const noscope = rec('no-scope', { scopes: ['read', 'sessions'] });
  noscope.state.needs_you.permissions = 3;
  const h = await steerWorld({ nodes: [waiting, quiet, noscope], perms: [] });
  const { w } = h;
  h.perms = [PERMS()[0]];
  h.log.length = 0;
  const age = () => w.run('for (const v of Nodes.P.by.values()) v.tried -= 20000');
  age();
  await poll(w, { force: true });
  await flush();
  assert.deepEqual(h.calls().map((c) => c.path), ['/api/nodes/build-box/permissions'], 'not quiet (nothing waits), not no-scope (no scope)');
  h.log.length = 0;
  age();
  w.document.hidden = true;
  await poll(w, { force: true });
  await flush();
  assert.equal(h.log.length, 0, 'a hidden tab asks nothing, not even when the poll is forced');
  w.document.hidden = false;
  const d = deferred();
  h.reply = (m, p) => (/permissions$/.test(p) ? d.promise : undefined);
  await poll(w, { force: true });
  age();
  await poll(w, { force: true });
  await flush();
  assert.equal(h.calls().length, 1, 'a second beat while the list is out does not ask again');
  d.resolve({ node: 'build-box', age: 0, data: { permissions: [] } });
  await flush();
});

test('a board with no node paired, or with the hub view off, reads no list and shows no card: nothing new runs', async () => {
  const off = await steerWorld({ board: boardState({ nodes_enabled: false }), perms: PERMS() });
  setState(off.w, boardState({ nodes_enabled: false }));
  off.log.length = 0;
  await poll(off.w, { force: true });
  off.w.run('Nodes.permsSync()');
  await flush();
  assert.equal(off.log.length, 0);
  assert.equal(plain(off.w.run('Nodes.permCards()')).length, 0);
  const none = await steerWorld({ nodes: [], perms: [] });
  none.w.run('Nodes.permsSync()');
  await flush();
  assert.equal(none.log.length, 0);
  assert.equal(plain(none.w.run('Nodes.permCards()')).length, 0);
});

test('a node whose pair lacks the permissions scope, or that is offline, shows one notice with disabled Allow and Deny and the reason; no list is read', async () => {
  const mac = rec('alice-mac', { scopes: ['read'] });
  mac.state.needs_you.permissions = 2;
  const off = rec('old-laptop', { scopes: ['read', 'permissions'], status: 'offline', age_s: 3600, last_ok_at: iso(3600) });
  off.state.needs_you.permissions = 1;
  const h = await steerWorld({ nodes: [mac, off], perms: [], board: noLocal() });
  const { w } = h;
  await go(w, '#/inbox');
  assert.equal(h.log.length, 0, 'no list read for either');
  const stubs = all(sec(w), '.nd-pstub');
  assert.equal(stubs.length, 2);
  assert.match(text(stubs[0]), /2 permission requests are waiting on alice-mac\./);
  assert.match(text(stubs[1]), /1 permission request is waiting on old-laptop\./);
  assert.deepEqual(all(stubs[0], 'button').map(text), ['Allow', 'Deny']);
  for (const b of all(sec(w), '.nd-pstub button')) { assert.equal(b.getAttribute('aria-disabled'), 'true'); }
  assert.match(text(stubs[0].querySelector('.nd-whys')), /Allow: needs the permissions scope on alice-mac\. Deny: needs the permissions scope on alice-mac\./);
  assert.match(text(stubs[1].querySelector('.nd-whys')), /old-laptop is offline, last seen 1 hour ago/);
  all(stubs[0], 'button')[0].click();
  assert.equal(h.log.length, 0, 'pressing a disabled control sends nothing');
  assert.match(shapes(w)[0], /Allow: needs the permissions scope on alice-mac/);
  assert.ok(all(stubs[0], 'button.primary').length === 0);
});

test('a list the node cannot give (an error) leaves the notice with the sentence, not an empty group; a list with a malformed row keeps the good ones', async () => {
  const h = await steerWorld({ perms: PERMS(), board: noLocal(), alone: true });
  const { w } = h;
  h.reply = (m, p) => { if (m === 'GET' && /permissions$/.test(p)) throw err(409, { error: 'x', reason: 'scope' }); return undefined; };
  w.run('for (const v of Nodes.P.by.values()) v.tried -= 20000');
  await poll(w, { force: true });
  await flush();
  await go(w, '#/inbox');
  assert.equal(cards(w).filter((c) => !c.classList.contains('nd-pstub')).length, 0);
  assert.match(text(all(sec(w), '.nd-pstub')[0]), /needs the permissions scope on build-box/);
  h.reply = (m, p) => (m === 'GET' && /permissions$/.test(p) ? { node: 'build-box', age: 0, data: { permissions: [
    { id: 41, tmux: 'shop--api--s1', tool: 'Bash', summary: 'ok', since: iso(5) }, { id: 'x', tmux: 'shop--api--s1' }, { id: 5, tmux: '../etc' }, null, { id: 0, tmux: 'shop--api--s2' }, { id: 7, tmux: 'shop--api--s2', tool: 5, summary: 'x'.repeat(900), since: 'junk' }] } } : undefined);
  w.run('for (const v of Nodes.P.by.values()) v.tried -= 20000');
  await poll(w, { force: true });
  await flush();
  assert.deepEqual(cards(w).filter((c) => !c.classList.contains('nd-pstub')).map((c) => c.getAttribute('data-key')), ['build-box/41', 'build-box/7']);
  assert.equal(text(cardOf(w, 7).querySelector('.ib-ctx')).length, 300, 'the summary is cut to 300 characters');
});

test('Nodes.stop forgets the lists, the answers in flight and the overlays', async () => {
  const h = await steerWorld({ perms: PERMS() });
  const { w } = h;
  w.run("Nodes.acked.set('build-box/shop--api--s1', 1); Nodes.closed.set('build-box/shop--api--s1', Date.now())");
  assert.ok(w.run('Nodes.P.by.size') > 0);
  w.run('Nodes.stop()');
  assert.deepEqual(plain(w.run('[Nodes.P.by.size, Nodes.P.st.size, Nodes.P.done.size, Nodes.acked.size, Nodes.closed.size]')), [0, 0, 0, 0, 0]);
});

// ---------------------------------------------------------------- the node page and the peek

test('the node page lists the cards of that node in Needs you instead of a second row; a node without the scope says so and why', async () => {
  const h = await steerWorld({ perms: PERMS() });
  const { w } = h;
  await go(w, '#/n/build-box');
  const needs = page(w).querySelector('[data-sec=needs]');
  assert.deepEqual(all(needs, '.nd-pcard').map((c) => c.getAttribute('data-key')), ['build-box/41', 'build-box/42']);
  assert.equal(all(needs, '.nd-item').length, 0);
  assert.ok(page(w).querySelector('.nd-perm').classList.contains('hidden'));
  assert.equal(all(needs, 'button').filter((b) => b.classList.contains('primary') && !b.classList.contains('tinted')).length, 0, 'Open board is the page\'s primary');
  btn(cardOf2(w, 42), 'Deny').click();
  await flush();
  assert.deepEqual(h.writes().map((c) => c.path), ['/api/nodes/build-box/permissions/42/deny']);
  const mac = rec('alice-mac', { scopes: ['read'] });
  mac.state.needs_you.permissions = 1;
  const g = await steerWorld({ nodes: [mac] });
  await go(g.w, '#/n/alice-mac');
  assert.match(text(page(g.w).querySelector('.nd-perm')), /^1 permission request waiting on alice-mac\. Allow and Deny: needs the permissions scope on alice-mac\. You can answer on its board \(Open board\)\.$/);
  assert.ok(!/arrives with the relay/.test(text(page(g.w))));
});
function cardOf2(w, id) { return all(page(w), '.nd-pcard').find((c) => c.getAttribute('data-key') === `build-box/${id}`); }

test('the peek of a session with a waiting request shows its card first, Allow filled (the peek is the lead), and answering from there works', async () => {
  const h = await steerWorld({ perms: PERMS() });
  const { w } = h;
  await go(w, S1);
  const c = page(w).querySelector('.nd-steer .nd-pcard');
  assert.equal(c.getAttribute('data-key'), 'build-box/41', 'only the request of this session');
  assert.equal(text(page(w).querySelector('.nd-steer h2')), 'Permission request');
  assert.ok(btn(c, 'Allow').classList.contains('primary') && !btn(c, 'Allow').classList.contains('tinted'));
  btn(c, 'Allow').click();
  await flush();
  assert.deepEqual(h.writes().map((x) => x.path), ['/api/nodes/build-box/permissions/41/allow']);
  assert.equal(page(w).querySelector('.nd-steer .nd-pcard'), null);
  assert.ok(page(w).querySelector('.nd-steer h2').classList.contains('hidden'));
});

test('a peek whose pair has the sessions scope but not the permissions scope keeps a disabled Answer with its reason while the session waits on a request', async () => {
  const b = rec('build-box', { scopes: ['read', 'sessions'] });
  b.state.needs_you.permissions = 1;
  const h = await steerWorld({ nodes: [b] });
  const { w } = h;
  await go(w, S1);
  const off = all(page(w), '.nd-acts button.nd-off');
  assert.deepEqual(off.map(text), ['Answer']);
  assert.match(text(page(w).querySelector('.nd-actbox .nd-whys')), /Answer: needs the permissions scope on build-box\./);
  assert.equal(h.calls(/permissions/).length, 0);
});

// ---------------------------------------------------------------- the keys of the peek and the palette

const ev = (key, over = {}) => ({ key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, target: null, defaultPrevented: false, preventDefault() {}, ...over });

test('the keys of the local peek work on a remote peek: r focuses the send box, a acknowledges, y and d allow and deny the request waiting on that session, o opens it on its own board; they are quiet in a field and off a peek', async () => {
  const h = await steerWorld({ perms: PERMS() });
  const { w } = h;
  w.run('Keymap.install()');
  const K = w.get('Keymap');
  assert.equal(K.handle(ev('y')), null, 'no remote peek open: not handled');
  await go(w, S1);
  assert.ok(K.handle(ev('r')));
  assert.equal(w.document.activeElement, box(w));
  assert.equal(K.handle(ev('a', { target: { tagName: 'TEXTAREA' } })), null, 'not inside a field');
  assert.equal(K.handle(ev('y', { target: { tagName: 'TEXTAREA' } })), null);
  assert.ok(K.handle(ev('a')));
  await flush();
  assert.deepEqual(h.writes().map((c) => c.path), [`${API}/ack`]);
  assert.ok(K.handle(ev('o')));
  assert.deepEqual(plain(w.get('__opened')), [['https://build-box.example.ts.net/#/s/shop--api--s1', '_blank', 'noopener,noreferrer']]);
  h.log.length = 0;
  assert.ok(K.handle(ev('d')));
  await flush();
  assert.deepEqual(h.writes().map((c) => c.path), ['/api/nodes/build-box/permissions/41/deny']);
  assert.equal(K.handle(ev('y')), null, 'the request is answered: no y left to handle');
  await go(w, '#/n/build-box/s/shop--api--s2');
  h.log.length = 0;
  assert.ok(K.handle(ev('y')), 'the request of s2 (42) is answered from its own peek');
  await flush();
  assert.deepEqual(h.writes().map((c) => c.path), ['/api/nodes/build-box/permissions/42/allow']);
  await go(w, '#/n/build-box');
  assert.equal(K.handle(ev('r')), null, 'not on a peek');
});

test('the palette leads with Allow / Deny for the lead remote request (the oldest) and has "Reply to <session> on <node>" for a remote session that needs you', async () => {
  const h = await steerWorld({ perms: PERMS() });
  const { w } = h;
  w.run('Palette.open()');
  const rows = all(w.document.querySelector('#helpdlg'), '.pal-group, .pal-item').map((n) => (n.classList.contains('pal-group') ? `## ${text(n)}` : text(n)));
  assert.equal(rows[0], '## Permission on build-box');
  assert.match(rows[1], /^Allow s1 on build-boxBash: git push origin task\/orders/);
  assert.match(rows[2], /^Deny s1 on build-boxBash: git push origin task\/orders/);
  assert.ok(rows.includes('## Reply on other nodes'));
  assert.ok(rows.some((r) => r.startsWith('Reply to s1 on build-box')));
  assert.ok(!rows.some((r) => r.startsWith('Reply to s2 on build-box')), 's2 does not need you: only a search finds it');
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id === "rp:allow"); Palette.runSelected({})');
  await flush();
  assert.deepEqual(h.writes().map((c) => c.path), ['/api/nodes/build-box/permissions/41/allow']);
  assert.equal(w.run('Palette.isOpen()'), false);
});

test('the palette: Deny runs deny; Reply to <session> on <node> opens the peek with the send box focused; typing "reply" finds the other remote sessions too', async () => {
  const h = await steerWorld({ perms: PERMS() });
  const { w } = h;
  w.run('Palette.open()');
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id === "rp:deny"); Palette.runSelected({})');
  await flush();
  assert.deepEqual(h.writes().map((c) => c.path), ['/api/nodes/build-box/permissions/41/deny']);
  w.run('Palette.open()');
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id === "rr:build-box/shop--api--s1"); Palette.runSelected({})');
  await new Promise((r) => setTimeout(r, 150));
  await tick();
  assert.equal(w.location.hash, S1);
  assert.equal(w.document.activeElement, box(w), 'the send box has the focus');
  w.run('Palette.open()');
  w.ctx.__q = 'reply s2';
  w.run('Palette.ui.input.value = __q; Palette.render()');
  assert.ok(all(w.document.querySelector('#helpdlg'), '.pal-item').some((n) => text(n).startsWith('Reply to s2 on build-box')));
});

test('the palette marks a reply to a node that cannot be steered off with the reason, and offers no Allow / Deny when nothing waits', async () => {
  const mac = rec('alice-mac', { scopes: ['read'] });
  const h = await steerWorld({ nodes: [mac] });
  const { w } = h;
  w.run('Palette.open()');
  const items = all(w.document.querySelector('#helpdlg'), '.pal-item');
  const reply = items.find((n) => text(n).startsWith('Reply to s1 on alice-mac'));
  assert.ok(reply.classList.contains('off'));
  assert.match(text(reply), /needs the sessions scope on alice-mac/);
  assert.ok(!items.some((n) => /^(Allow|Deny) /.test(text(n))));
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id === "rr:alice-mac/shop--api--s1"); Palette.runSelected({})');
  assert.deepEqual(h.calls(/^\/api\/nodes/), [], 'nothing was asked of any node');
  assert.match(shapes(w)[0], /needs the sessions scope on alice-mac/);
});

test('the palette\'s send mode takes the sessions of the nodes whose pair holds the sessions scope: it sends through the relay, refuses a slash command, and says why for a node it cannot reach', async () => {
  const h = await steerWorld();
  const { w } = h;
  w.run("Palette.open({ mode: 'send', text: 'look at this' })");
  const items = all(w.document.querySelector('#helpdlg'), '.pal-item');
  const mine = items.find((n) => text(n).includes('s1') && n.querySelector('.nchip') && text(n).includes('build-box'));
  assert.ok(mine, 'a remote session with its node chip');
  const offIds = plain(w.run('Palette.ui.shown.filter((i) => i.off).map((i) => i.id)'));
  assert.ok(offIds.includes('send:alice-mac/shop--api--s1'), 'a read-only node is off');
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id === "send:build-box/shop--api--s1"); Palette.runSelected({})');
  await flush();
  assert.deepEqual(h.writes().map((c) => [c.path, c.body]), [[`${API}/prompt`, { text: 'look at this', queue: false }]]);
  assert.equal(shapes(w).slice(-1)[0], 'Sent to s1 on build-box');
  w.run("Palette.open({ mode: 'send', text: '/compact' })");
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id === "send:build-box/shop--api--s1"); Palette.runSelected({})');
  await flush();
  assert.equal(h.writes().length, 1);
  assert.match(shapes(w).slice(-1)[0], /^Slash commands are not sent to another node\./);
  w.run("Palette.open({ mode: 'send', text: 'hello' })");
  w.run('Palette.ui.sel = Palette.ui.shown.findIndex((i) => i.id === "send:alice-mac/shop--api--s1"); Palette.runSelected({})');
  assert.equal(h.writes().length, 1);
  assert.match(shapes(w).slice(-1)[0], /needs the sessions scope on alice-mac/);
});

test('with the hub view off the palette has no remote steering rows and send mode lists only this board\'s sessions', async () => {
  const h = await steerWorld({ board: boardState({ nodes_enabled: false }) });
  const { w } = h;
  setState(w, boardState({ nodes_enabled: false }));
  w.run('Palette.open()');
  const items = all(w.document.querySelector('#helpdlg'), '.pal-item').map(text);
  assert.ok(!items.some((t) => /^(Reply to|Allow|Deny) /.test(t)));
  w.run("Palette.open({ mode: 'send', text: 'hi' })");
  assert.equal(all(w.document.querySelector('#helpdlg'), '.pal-item .nchip').length, 0);
});

// ---------------------------------------------------------------- nothing but a person's tap answers a permission

test('the permission answer has one writer: Nodes.permAnswer, called only by a card button, the peek\'s y and d, and the palette row; no other file builds the route', () => {
  const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8');
  const files = [];
  const walk = (dir) => { for (const e of fs.readdirSync(path.join(STATIC, dir), { withFileTypes: true })) { const rel = path.posix.join(dir, e.name); if (e.isDirectory()) { if (!/vendor|demo|screenshots/.test(rel)) walk(rel); } else if (rel.endsWith('.js')) files.push(rel); } };
  walk('.');
  const calls = [];
  for (const f of files) {
    const src = read(f);
    for (const m of src.matchAll(/permAnswer\(/g)) calls.push(f);
    if (f !== 'nodes-hub.js') assert.ok(!/permissions\/\$\{/.test(src) && !/\/permissions\/'/.test(src.replace(/\/\*[\s\S]*?\*\//g, '')), `${f}: no permission route outside nodes-hub.js`);
  }
  assert.deepEqual(calls.sort(), ['nodes-hub.js', 'nodes-hub.js', 'pages/node.js', 'pages/node.js', 'palette.js'], 'the Allow and Deny buttons, the peek keys y and d, the palette row');
  const hub = read('nodes-hub.js');
  assert.ok(!/setInterval\([^)]*permAnswer|auto.?allow/i.test(hub), 'no timer or setting answers a request');
  const pal = read('palette.js');
  assert.equal((pal.match(/permAnswer\(/g) || []).length, 1);
});

// ---------------------------------------------------------------- ?demo=1

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function demoWorld() {
  const h = hubWorld({ search: '?demo=1', realApi: true });
  const { w } = h;
  setState(w, plain(await w.run("api('GET', '/api/state')")));
  await poll(w);
  await flush(12);
  return h;
}
const dsec = (w) => w.document.querySelector('.nd-inbox');
const dcard = (w, id) => all(dsec(w), '.nd-pcard').find((c) => c.getAttribute('data-key') === `build-box/${id}`);

test('demo: Home shows the four remote requests of build-box as cards and one notice for alice-mac (no permissions scope), each answer ends as the real node would end it', async () => {
  const { w } = await demoWorld();
  await go(w, '#/');
  w.run('updateCurrentPage(state)');
  await flush(8);
  assert.deepEqual(all(dsec(w), '.nd-pcard').filter((c) => !c.classList.contains('nd-pstub')).map((c) => c.getAttribute('data-key')), ['build-box/41', 'build-box/42', 'build-box/43', 'build-box/44']);
  const stub = all(dsec(w), '.nd-pstub')[0];
  assert.match(text(stub), /1 permission request is waiting on alice-mac\./);
  assert.match(text(stub.querySelector('.nd-whys')), /needs the permissions scope on alice-mac/);
  assert.equal(all(dsec(w), 'button').filter((b) => b.classList.contains('primary') && !b.classList.contains('tinted')).length, 0, 'this board\'s own card leads on Home');
  const want = {
    41: ['Allowed on build-box', null],
    42: ['This request was already answered. Open terminal to see the current prompt.', 'closed'],
    43: ['This request has expired. Open terminal to see the current prompt.', 'closed'],
    44: ['This request is gone. Open terminal to see the current prompt.', 'closed'],
  };
  for (const id of [42, 43, 44, 41]) {
    btn(dcard(w, id), 'Allow').click();
    await wait(260);
    await flush(8);
    const [sentence, closed] = want[id];
    const t = plain(w.get('__toasts')).map((x) => x.text);
    assert.equal(t[t.length - 1], sentence, String(id));
    if (closed) { assert.ok(dcard(w, id).classList.contains('closed')); assert.equal(text(dcard(w, id).querySelector('.nd-perr')), sentence); assert.equal(all(dcard(w, id), 'button').length, 0); }
    else assert.equal(dcard(w, id), undefined, 'answered: gone');
  }
  await poll(w, { force: true });
  await flush(8);
  assert.equal(dcard(w, 41), undefined, 'the next reading agrees');
});

test('demo: the peek of build-box/s1 steers: a prompt is sent, Ack clears it, a key is sent; s3 is a session with wider permissions, t-cart-page is busy until queued, s2 does not confirm, and Kill session on s2 finds it gone', async () => {
  const { w } = await demoWorld();
  const prompt = async (tmux, t, label) => {
    await go(w, `#/n/build-box/s/${tmux}`);
    type(box(w), t);
    enter(box(w));
    await wait(260);
    await flush(8);
    return st(w);
  };
  assert.equal(await prompt('shop--api--s1', 'go on', 'ok'), 'Sent to build-box.');
  assert.equal(box(w).value, '');
  assert.equal(await prompt('shop--api--s3', 'go on'), 'That session runs with wider permissions than another node may use, or its mode cannot be read; start it from its own board.');
  assert.equal(box(w).value, 'go on', 'the draft stays');
  assert.equal(await prompt('shop--web--t-cart-page', 'after the tests'), 'The session is working. Try again in 8 seconds.');
  btn(page(w), 'Queue it').click();
  await wait(260);
  await flush(8);
  assert.equal(st(w), 'Queued on build-box. It is typed when the session is ready.');
  assert.equal(await prompt('infra--deploy--s2', 'status please'), 'Could not confirm whether build-box got this. Check the terminal before answering again.');
  assert.equal(box(w).value, 'status please');
  // keys: one goes, C-c needs the second tap
  await go(w, '#/n/build-box/s/shop--api--s1');
  btn(page(w), 'Keys').click();
  all(page(w), '.nd-keys button')[0].click();
  await wait(260);
  await flush(8);
  assert.equal(st(w), 'Sent Enter to build-box.');
  btn(page(w).querySelector('.nd-keys'), 'C-c').click();
  btn(page(w).querySelector('.nd-ckey'), 'Confirm C-c').click();
  await wait(260);
  await flush(8);
  assert.equal(st(w), 'Sent C-c to build-box.');
  // Ack
  btn(page(w), 'Ack').click();
  await wait(260);
  await flush(8);
  assert.equal(st(w), 'Acknowledged on build-box.');
  assert.equal(btn(page(w), 'Ack'), undefined);
  await poll(w, { force: true });
  await flush(6);
  assert.equal(btn(page(w), 'Ack'), undefined, 'the demo\'s next reading agrees');
  // Kill: gone on s2
  await go(w, '#/n/build-box/s/infra--deploy--s2');
  btn(page(w), 'Kill session on build-box').click();
  btn(page(w), 'Confirm Kill session on build-box').click();
  await wait(260);
  await flush(8);
  assert.equal(st(w), 'That session is gone from build-box.');
  // Kill s1 works and goes to the node page
  await go(w, '#/n/build-box/s/shop--api--s1');
  btn(page(w), 'Kill session on build-box').click();
  btn(page(w), 'Confirm Kill session on build-box').click();
  await wait(260);
  await flush(8);
  assert.equal(w.location.hash, '#/n/build-box');
  assert.equal(plain(w.get('__toasts')).slice(-1)[0].text, 'Killed s1 on build-box');
});

test('demo: the refusals of the other two nodes: alice-mac holds read only (the peek keeps the disabled controls), old-laptop is offline (the panel is there, disabled, with the age)', async () => {
  const { w } = await demoWorld();
  await go(w, '#/n/alice-mac/s/notes--site--s1');
  assert.equal(box(w), null);
  assert.deepEqual(all(page(w), '.nd-acts button.nd-off').map(text), ['Send a prompt', 'Answer', 'Close session']);
  await go(w, '#/n/old-laptop/s/scratch--play--s1');
  assert.ok(box(w));
  assert.match(text(page(w).querySelector('.nd-swhys')), /^Reply, Keys, Ack and Kill session: old-laptop is offline, last seen \d+ hours? ago\.$/);
  type(box(w), 'hello');
  enter(box(w));
  await flush(6);
  assert.match(st(w), /^old-laptop is offline, last seen/);
  assert.equal(box(w).value, 'hello');
});

test('the demo fixture carries what the demo handlers read: four requests for build-box with their demo_answer, the steer rules, and no secret-shaped or personal string', () => {
  const d = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'nodes.json'), 'utf8'));
  assert.deepEqual(d.permissions['build-box'].map((p) => [p.id, p.demo_answer || 'ok']), [[41, 'ok'], [42, 'answered'], [43, 'expired'], [44, 'gone']]);
  const by = Object.fromEntries(d.hub.map((r) => [r.handle, r]));
  assert.equal(by['build-box'].state.needs_you.permissions, 4);
  assert.deepEqual(by['old-laptop'].scopes, ['read', 'tasks', 'sessions']);
  assert.deepEqual(Object.keys(d.steer), ['build-box/shop--api--s3', 'build-box/infra--deploy--s2']);
  for (const k of Object.keys(d.steer)) assert.ok(by[k.split('/')[0]].state.sessions.some((s) => s.tmux === k.split('/')[1]), `${k} is a session of the fixture`);
  for (const p of d.permissions['build-box']) assert.ok(by['build-box'].state.sessions.some((s) => s.tmux === p.tmux), `${p.tmux} is a session of the fixture`);
  assert.ok(!JSON.stringify([d.permissions, d.steer]).match(/@|\/Users\/|\/home\/|—|sk-|ghp_/), 'no e-mail address, personal path, em-dash or token shape');
});

// ---------------------------------------------------------------- the words and the files

test('no em-dash and no markup in what the steering code says or builds; the files keep to el() and text', () => {
  for (const f of ['nodes-hub.js', 'pages/node.js', 'palette.js']) {
    const src = fs.readFileSync(path.join(STATIC, f), 'utf8');
    const strings = [...src.matchAll(/'([^'\\\n]|\\.)*'|`([^`\\]|\\.)*`/g)].map((m) => m[0]).filter((t) => /Allow|Deny|Kill|Reply|Keys|Queue|permission|Sent|Acknowledged|Closing|Slash|confirm/i.test(t));
    assert.ok(strings.length > 5, f);
    for (const t of strings) assert.ok(!t.includes('—'), `${f}: ${t}`);
  }
});

test('the first-paint files grew by a few bytes only: the steering code is in the lazy bundle', () => {
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  const scripts = [...html.matchAll(/<script[^>]*src="(\/static\/[^"]+)"/g)].map((m) => m[1]);
  const total = scripts.reduce((n, s) => n + fs.statSync(path.join(STATIC, s.slice('/static/'.length))).size, 0);
  assert.ok(total <= 530000, `${total} bytes of script up front`);
  for (const f of ['nodes-hub.js', 'pages/node.js']) assert.ok(!html.includes(f));
  const eager = fs.readFileSync(path.join(STATIC, 'nodes.js'), 'utf8');
  for (const name of ['permAnswer', 'sendPrompt', 'permsSync', 'sendKey']) assert.ok(!eager.includes(name), `${name} is not in the eager nodes.js`);
});

test('the audit-friendly cadence: a list already held is read again only when the count in the reading changed or after 15 s; a failed read waits the same 15 s; an unchanged reading costs nothing', async () => {
  const h = await steerWorld({ perms: PERMS(), alone: true });
  const { w } = h;
  assert.equal(h.calls(/permissions$/).length, 1, 'the first reading asks once');
  for (let i = 0; i < 4; i++) { w.run('Nodes.M.recvAt = Date.now() - 70000'); await poll(w, { force: true }); await flush(3); }
  assert.equal(h.calls(/permissions$/).length, 1, 'four more beats, same count: nothing asked');
  const before = w.run("Nodes.get('build-box').state.needs_you.permissions");
  assert.equal(before, 2);
  const one = rec('build-box', { scopes: ['read', 'tasks', 'sessions', 'permissions'] });
  one.state.needs_you.permissions = 1;
  w.ctx.__ans = JSON.stringify({ status: 200, body: { nodes: [one], at: iso(0) }, etag: 'W/"c1"' });
  await poll(w, { force: true });
  await flush(4);
  assert.equal(h.calls(/permissions$/).length, 2, 'the count moved: asked again');
  w.run('for (const v of Nodes.P.by.values()) v.tried -= 16000');
  await poll(w, { force: true });
  await flush(4);
  assert.equal(h.calls(/permissions$/).length, 3, '15 s later: asked again');
  h.reply = (m, p) => { if (m === 'GET' && /permissions$/.test(p)) throw err(503, { error: 'x', reason: 'offline', age: 5 }); return undefined; };
  w.run('for (const v of Nodes.P.by.values()) v.tried -= 16000');
  await poll(w, { force: true });
  await flush(4);
  assert.equal(h.calls(/permissions$/).length, 4);
  await poll(w, { force: true });
  await flush(4);
  assert.equal(h.calls(/permissions$/).length, 4, 'a failed read is not repeated on the next beat');
});
