// Contract tests for app/static/sw.js (the service worker template): the push handler maps a payload to showNotification options,
// the notificationclick handler answers a permission or an ack without opening a window, a plain tap focuses the board window.
// The file is a template (/sw.js fills the build id and the shell list): the same two substitutions are made here, then it runs in a vm
// with a fake `self`, so no browser and no network are involved.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { test } from 'node:test';
import { STATIC, plain } from './harness.mjs';

const ORIGIN = 'https://box.example.ts.net:8443';
const SRC = fs.readFileSync(path.join(STATIC, 'sw.js'), 'utf8').replace('__ASSET_VERSION__', 'test').replace('__SHELL_JSON__', '[]');

/** Load the worker. `opts.badge` false removes setAppBadge; `opts.fetch` answers the worker's own fetch calls. */
function worker(opts = {}) {
  const calls = { shown: [], fetched: [], opened: [], badge: [], closed: [], navigated: [], posted: [], focused: [] };
  const handlers = {};
  const shownList = [];
  const clients = (opts.clients || []).map((c) => ({
    url: c.url,
    postMessage: (m) => { if (c.postFails) throw new Error('detached'); calls.posted.push([c.url, m]); },
    focus: async () => { if (c.focusFails) throw new Error('no focus'); calls.focused.push(c.url); },
    navigate: async (u) => { calls.navigated.push(u); },
  }));
  const self = {
    location: { origin: ORIGIN },
    addEventListener: (type, fn) => { handlers[type] = fn; },
    skipWaiting: async () => {},
    clients: { matchAll: async () => clients, openWindow: async (u) => { calls.opened.push(u); }, claim: async () => {} },
    registration: {
      showNotification: async (title, o) => { calls.shown.push({ title, o }); shownList.push({ title, ...o, close() { calls.closed.push(o.body); } }); },
      getNotifications: async () => shownList,
    },
    navigator: opts.badge === false ? {} : { setAppBadge: async (n) => { calls.badge.push(n); }, clearAppBadge: async () => { calls.badge.push(0); } },
    fetch: async (url, init) => { calls.fetched.push([url, init]); return opts.fetch ? opts.fetch(url, init) : { ok: true, status: 200 }; },
  };
  const ctx = vm.createContext({ self, URL, Request: class { constructor(u) { this.url = u; } }, JSON, Promise, setTimeout: (f) => { f(); return 0; },
                                 caches: { open: async () => ({ addAll: async () => {} }), keys: async () => [] } });
  vm.runInContext(SRC, ctx, { filename: 'sw.js' });
  const done = [];
  const fire = async (type, ev) => { await handlers[type]({ ...ev, waitUntil: (p) => done.push(p) }); await Promise.all(done.splice(0)); };
  const push = (payload) => fire('push', { data: { json: () => payload, text: () => JSON.stringify(payload) } });
  const click = (action, d, extra = {}) => fire('notificationclick', { action, notification: { title: 'T', tag: 'shop--api--s1', body: 'B', data: d, close() { calls.closed.push('n'); }, ...extra } });
  return { calls, handlers, ctx, push, click, fire };
}

test('the worker still has the push and notificationclick handlers', () => {
  const w = worker();
  assert.equal(typeof w.handlers.push, 'function');
  assert.equal(typeof w.handlers.notificationclick, 'function');
});

test('an old payload (title, body, url, tag) still renders', async () => {
  const w = worker();
  await w.push({ title: 'ccboard test', body: 'Web Push works.', url: '/', tag: 'test' });
  assert.equal(w.calls.shown.length, 1);
  const { title, o } = w.calls.shown[0];
  assert.equal(title, 'ccboard test');
  assert.equal(o.body, 'Web Push works.');
  assert.equal(o.tag, 'test');
  assert.equal(o.data.url, '/');
  assert.equal(o.renotify, undefined);
  assert.equal(o.requireInteraction, undefined);
  assert.equal(o.actions, undefined);
  assert.equal(o.icon, '/static/icon-192.png');
  assert.deepEqual(w.calls.badge, []);
});

test('a payload with no JSON body shows its text', async () => {
  const w = worker();
  await w.fire('push', { data: { json: () => { throw new Error('not json'); }, text: () => 'plain words' } });
  assert.equal(w.calls.shown[0].title, 'ccboard');
  assert.equal(w.calls.shown[0].o.body, 'plain words');
});

test('a new payload maps onto the notification options', async () => {
  const w = worker();
  const actions = [{ action: 'allow', title: 'Allow' }, { action: 'deny', title: 'Deny' }, { action: 'terminal', title: 'Terminal' }];
  await w.push({ title: '◆ shop/api · s1: needs you', body: 'Fix login\n? Bash: npm test', url: '/#/s/shop--api--s1', tag: 'shop--api--s1', renotify: true,
                 agent: 'claude', state: 'waiting', tmux: 'shop--api--s1', perm_id: 5, actions, badge: 3, ts: 1760000000000 });
  const { o } = w.calls.shown[0];
  assert.equal(o.renotify, true);
  assert.equal(o.requireInteraction, true);
  assert.equal(o.timestamp, 1760000000000);
  assert.deepEqual(plain(o.actions), [{ action: 'allow', title: 'Allow' }, { action: 'deny', title: 'Deny' }]);       // two buttons at most
  assert.deepEqual(plain(o.data), { url: '/#/s/shop--api--s1', tmux: 'shop--api--s1', perm_id: 5, state: 'waiting', agent: 'claude' });
  assert.deepEqual(w.calls.badge, [3]);
});

test('renotify without a tag is never passed on (the browser throws on it)', async () => {
  const w = worker();
  await w.push({ title: 'x', body: 'y', renotify: true });
  assert.equal(w.calls.shown[0].o.renotify, undefined);
  assert.equal(w.calls.shown[0].o.tag, undefined);
});

test('only a waiting state or a permission stays on screen until dealt with', async () => {
  const w = worker();
  await w.push({ title: 'a', body: '', tag: 't', state: 'done' });
  await w.push({ title: 'b', body: '', tag: 't', state: 'waiting' });
  await w.push({ title: 'c', body: '', tag: 't', state: 'done', perm_id: 9 });
  assert.deepEqual(w.calls.shown.map((s) => s.o.requireInteraction), [undefined, true, true]);
});

test('a badge of 0 clears the icon badge, a missing one leaves it alone, and a browser without the API is fine', async () => {
  const w = worker();
  await w.push({ title: 'a', body: '', badge: 0 });
  assert.deepEqual(w.calls.badge, [0]);
  await w.push({ title: 'b', body: '' });
  assert.deepEqual(w.calls.badge, [0]);
  const none = worker({ badge: false });
  await none.push({ title: 'c', body: '', badge: 4 });
  assert.equal(none.calls.shown.length, 1);
});

test('Allow answers the permission with a same-origin POST and a short confirmation, and opens no window', async () => {
  const w = worker();
  await w.click('allow', { url: '/#/s/shop--api--s1', tmux: 'shop--api--s1', perm_id: 5 });
  assert.equal(w.calls.fetched.length, 1);
  const [url, init] = w.calls.fetched[0];
  assert.equal(url, '/api/permission/5/allow');
  assert.equal(init.method, 'POST');
  assert.equal(init.headers['X-CCBoard'], '1');
  assert.equal(init.credentials, 'same-origin');
  assert.deepEqual(w.calls.opened, []);
  assert.equal(w.calls.shown.length, 1);
  assert.equal(w.calls.shown[0].o.body, 'Allowed.');
  assert.equal(w.calls.shown[0].o.silent, true);
  assert.equal(w.calls.shown[0].o.tag, 'shop--api--s1');
  assert.ok(w.calls.closed.includes('Allowed.'), 'the confirmation closes itself');
});

test('Deny posts deny; an already answered request says so', async () => {
  const w = worker({ fetch: () => ({ ok: false, status: 409 }) });
  await w.click('deny', { tmux: 'shop--api--s1', perm_id: 7 });
  assert.equal(w.calls.fetched[0][0], '/api/permission/7/deny');
  assert.equal(w.calls.shown[0].o.body, 'Already answered.');
});

test('a board that cannot be reached is said so, with a tap that opens it', async () => {
  const w = worker({ fetch: () => { throw new Error('offline'); } });
  await w.click('allow', { url: '/#/s/a--b--c', tmux: 'a--b--c', perm_id: 1 });
  assert.match(w.calls.shown[0].o.body, /Could not reach ccboard/);
  assert.equal(w.calls.shown[0].o.data.url, '/#/s/a--b--c');
});

test('Allow without a permission id falls back to the plain tap', async () => {
  const w = worker({ clients: [{ url: ORIGIN + '/' }] });
  await w.click('allow', { url: '/#/s/a--b--c', tmux: 'a--b--c', perm_id: null });
  assert.deepEqual(w.calls.fetched, []);
  assert.equal(w.calls.posted.length, 1);
});

test('Ack posts to the session ack route', async () => {
  const w = worker();
  await w.click('ack', { tmux: 'shop--api--s1', url: '/#/s/shop--api--s1' });
  assert.equal(w.calls.fetched[0][0], '/api/sessions/shop--api--s1/ack');
  assert.equal(w.calls.fetched[0][1].method, 'POST');
  assert.equal(w.calls.shown[0].o.body, 'Marked as seen.');
  assert.deepEqual(w.calls.opened, []);
});

test('Ack on the settings test push (no session) only closes it', async () => {
  const w = worker();
  await w.click('ack', { tmux: '', url: '/' });
  assert.deepEqual(w.calls.fetched, []);
  assert.deepEqual(w.calls.shown, []);
  assert.deepEqual(w.calls.opened, []);
});

test('Terminal opens the session terminal in a window of its own', async () => {
  const w = worker({ clients: [{ url: ORIGIN + '/' }] });
  await w.click('terminal', { tmux: 'shop--api--s1', url: '/#/s/shop--api--s1' });
  assert.deepEqual(w.calls.opened, [ORIGIN + '/term/shop--api--s1']);
  assert.deepEqual(w.calls.posted, []);
});

test('a plain tap focuses the open board window and asks it to navigate', async () => {
  const w = worker({ clients: [{ url: ORIGIN + '/term/x--y--z' }, { url: ORIGIN + '/#/inbox' }] });
  await w.click('', { url: '/#/s/shop--api--s1', tmux: 'shop--api--s1' });
  assert.deepEqual(plain(w.calls.posted), [[ORIGIN + '/#/inbox', { type: 'nav', url: '/#/s/shop--api--s1' }]]);
  assert.deepEqual(w.calls.focused, [ORIGIN + '/#/inbox']);
  assert.deepEqual(w.calls.opened, []);
});

test('a window that refuses the message is navigated; with no board window a new one opens', async () => {
  const stuck = worker({ clients: [{ url: ORIGIN + '/', postFails: true }] });
  await stuck.click('', { url: '/#/s/a--b--c' });
  assert.deepEqual(stuck.calls.navigated, [ORIGIN + '/#/s/a--b--c']);
  const none = worker({ clients: [{ url: ORIGIN + '/term/a--b--c' }] });
  await none.click('', { url: '/#/s/a--b--c' });
  assert.deepEqual(none.calls.opened, [ORIGIN + '/#/s/a--b--c']);
  assert.deepEqual(none.calls.posted, []);
});

test('a url that leaves the origin is never opened', async () => {
  const w = worker();
  await w.click('', { url: 'https://evil.example/x' });
  assert.deepEqual(w.calls.opened, [ORIGIN + '/']);
});
