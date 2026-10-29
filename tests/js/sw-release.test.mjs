import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { test } from 'node:test';
import { STATIC, makeWorld } from './harness.mjs';
import { installDom } from './minidom.mjs';

const origin = 'https://board.test';
const key = r => new URL(typeof r === 'string' ? r : r.url, origin).href;
function network() {
  const stores = new Map();
  const env = { online: true, missing: null, release: 'old', skipped: [], claimed: [] };
  env.fetch = async req => {
    const u = new URL(key(req));
    if (!env.online) throw new Error('offline');
    if (u.pathname === env.missing) return new Response('missing', { status: 404 });
    if (u.searchParams.get('v') && u.searchParams.get('v') !== env.release) return new Response('release gone', { status: 409 });
    return new Response(u.pathname === '/' ? `<html data-release="${env.release}">` : env.release + ':' + u.pathname);
  };
  env.caches = {
    open: async name => {
      if (!stores.has(name)) stores.set(name, new Map());
      const store = stores.get(name);
      return {
        match: async req => store.get(key(req))?.clone(),
        put: async (req, response) => store.set(key(req), response.clone()),
        addAll: async requests => {
          const responses = await Promise.all(requests.map(env.fetch));
          if (responses.some(r => !r.ok)) throw new Error('incomplete install');
          requests.forEach((r, i) => store.set(key(r), responses[i]));
        },
      };
    }, keys: async () => [...stores.keys()], delete: async name => stores.delete(name),
  };
  return env;
}
function worker(env, release, legacy = false) {
  const handlers = {};
  const shell = ['/', '/static/boot.js', '/static/lazy.js', '/static/pages/home.js'].map(u => legacy ? u : u + '?v=' + release);
  const self = { location: { origin }, addEventListener: (t, f) => handlers[t] = f,
    skipWaiting: async () => env.skipped.push(release), clients: { claim: async () => env.claimed.push(release) } };
  class RelativeRequest extends Request { constructor(url, init) { super(key(url), init); } }
  const ctx = vm.createContext({ self, caches: env.caches, fetch: env.fetch, URL, Response, Request: RelativeRequest });
  const source = legacy ? fs.readFileSync(new URL('./fixtures/sw-before-release-urls.js', import.meta.url), 'utf8') : fs.readFileSync(path.join(STATIC, 'sw.js'), 'utf8');
  vm.runInContext(source.replaceAll('__ASSET_VERSION__', release).replace('__SHELL_JSON__', JSON.stringify(shell)), ctx);
  return {
    fire: type => { let promise; handlers[type]({ waitUntil: p => promise = p }); return promise; },
    get: (url, navigate = false) => {
      const req = new RelativeRequest(url); if (navigate) Object.defineProperty(req, 'mode', { value: 'navigate' });
      let answer; handlers.fetch({ request: req, respondWith: p => answer = p }); return answer || env.fetch(req);
    },
  };
}

test('review: new index and boot under previous worker cannot get the old lazy loader offline; Retry recovers', async () => {
  const env = network(), old = worker(env, 'old');
  await old.fire('install'); await old.fire('activate');
  env.release = 'new';
  assert.match(await (await old.get('/', true)).text(), /new/);
  assert.equal(await (await old.get('/static/boot.js?v=new')).text(), 'new:/static/boot.js');
  env.online = false;
  assert.equal((await old.get('/static/lazy.js?v=new')).status, 503);
  // A subsequent offline navigation still has the complete OLD index; network
  // responses above never overwrite its release cache.
  assert.match(await (await old.get('/', true)).text(), /old/);
  assert.equal(await (await old.get('/static/lazy.js?v=old')).text(), 'old:/static/lazy.js');
  const w = makeWorld({ companions: false, primitives: false, requestAnimationFrame: f => f() });
  installDom(w);
  const scripts = [];
  w.document.head = { appendChild: s => scripts.push(s) };
  w.run(fs.readFileSync(path.join(STATIC, 'boot.js'), 'utf8').replaceAll('__ASSET_VERSION__', 'new'));
  assert.equal(scripts[0].src, '/static/lazy.js?v=new');
  scripts[0].onerror();
  const retry = w.document.querySelector('#page button');
  assert.equal(retry.textContent, 'Retry');
  assert.match(w.document.querySelector('#page').textContent, /could not be loaded/);
  env.online = true; retry.click();
  assert.equal((await old.get(scripts[1].src)).status, 200);
});

test('review: one missing file rejects worker installation; activation waits for a complete cache', async () => {
  const env = network(), old = worker(env, 'old');
  await old.fire('install'); await old.fire('activate');
  env.release = 'new'; env.missing = '/static/lazy.js';
  const next = worker(env, 'new');
  await assert.rejects(next.fire('install'), /incomplete install/);
  assert.deepEqual(env.skipped, ['old']);
  env.missing = null;
  await next.fire('install'); await next.fire('activate');
  assert.deepEqual(env.skipped, ['old', 'new']);
  env.online = false;
  assert.equal(await (await next.get('/static/lazy.js?v=new')).text(), 'new:/static/lazy.js');
  assert.equal(await (await next.get('/static/lazy.js?v=old')).text(), 'old:/static/lazy.js', 'an existing old tab stays on its release');
  assert.match(await (await next.get('/', true)).text(), /new/);
});

test('review: an app prerequisite missing offline shows Retry without evaluating the loader twice', async () => {
  let offline = true;
  const loads = [], scripts = [];
  const w = makeWorld({ companions: false, primitives: false, requestAnimationFrame: f => f(), Lazy: {
    boot: async () => { loads.push('boot'); if (offline) throw new Error('core.js unavailable offline'); },
  } });
  installDom(w);
  w.document.head = { appendChild: script => scripts.push(script) };
  w.load('boot.js');
  await new Promise(r => setImmediate(r));
  const retry = w.document.querySelector('#page button');
  assert.equal(retry?.textContent, 'Retry');
  offline = false; retry.click();
  await new Promise(r => setImmediate(r));
  assert.deepEqual(loads, ['boot', 'boot']);
  assert.equal(scripts.length, 0, 'retry reuses Lazy and cannot redeclare its lexical globals');
});

// The exact previous network-first worker, not just a previous release of the
// fixed worker: it overwrites its navigation/boot entries during the update.
test('review: actual legacy worker cannot substitute its loader without an app bundle for a new boot', async () => {
  const env = network(), legacy = worker(env, 'old', true);
  await legacy.fire('install'); await legacy.fire('activate');
  const oldCache = await env.caches.open('ccboard-shell-old');
  await oldCache.put('/static/lazy.js', new Response('const Lazy = {bundles:{home:{}}};'));
  env.release = 'new';
  await legacy.get('/', true);
  await legacy.get('/static/boot.js?v=new');
  const scripts = [];
  const w = makeWorld({ companions: false, primitives: false, requestAnimationFrame: f => f() });
  w.document.head = { appendChild: s => scripts.push(s) };
  w.run(fs.readFileSync(path.join(STATIC, 'boot.js'), 'utf8').replaceAll('__ASSET_VERSION__', 'new'));
  const loaderURL = scripts[0].src;
  env.online = false;
  assert.match(await (await legacy.get('/', true)).text(), /new/, 'legacy worker already replaced its cached index');
  assert.equal(await (await legacy.get('/static/boot.js?v=new')).text(), 'new:/static/boot.js');
  assert.equal((await legacy.get(loaderURL)).status, 503, 'boot must show Retry, never execute the cached incompatible loader');
  assert.match(await (await legacy.get('/static/lazy.js')).text(), /bundles:\{home/, 'the incompatible legacy loader really is cached');
  env.online = true;
  assert.equal(await (await legacy.get(loaderURL)).text(), 'new:/static/lazy.js', 'Retry obtains the matching release after connectivity returns');
});
