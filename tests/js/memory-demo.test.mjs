// Demo mode for the Memory proxy routes (v0.5.20, docs/memory-api.md): core.js demoApi maps each /api/memory/* GET to its own fixture
// under app/static/demo/, and ?mem=<state> lays a degraded variant from demo/memory_states.json over the project routes.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { STATIC, makeWorld, plain } from './harness.mjs';

function demoWorld(search = '?demo=1') {
  const w = makeWorld({
    fetch: async (url) => {
      const m = /^\/static\/demo\/(\w+)\.json$/.exec(String(url));
      if (!m) throw new Error(`demo mode asked for ${url}`);
      const body = fs.readFileSync(path.join(STATIC, 'demo', `${m[1]}.json`), 'utf8');
      return { ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(body) };
    },
  });
  w.location.search = search;
  w.load('core.js');
  return w;
}

const fx = (name) => JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', `${name}.json`), 'utf8'));
// a project route answers its fixture under the asked project's name (every demo project has the same rows)
const fxFor = (name, url) => {
  const d = fx(name);
  const m = /^\/api\/memory\/([^/]+)\/[a-z]+/.exec(url);
  return m && typeof d.project === 'string' ? { ...d, project: m[1] } : d;
};

test('every memory route reads its own fixture, for any project', async () => {
  const w = demoWorld();
  const routes = [['/api/memory/health', 'memory_health'], ['/api/memory/prefs', 'memory_prefs'],
    ['/api/memory/shop/observations?limit=20&before=1', 'memory_observations'], ['/api/memory/ccboard/summaries', 'memory_summaries'],
    ['/api/memory/shop/search?q=sticky', 'memory_search'], ['/api/memory/shop/timeline?anchor=19608', 'memory_timeline'],
    ['/api/memory/phasezero/palace?subagents=1', 'memory_palace']];
  for (const [url, name] of routes) {
    w.ctx.__u = url;
    assert.deepEqual(plain(await w.run('api("GET", __u)')), fxFor(name, url), url);
  }
});

test('?mem=down throws the 503 the way api() does, with the body for the page', async () => {
  const w = demoWorld('?demo=1&mem=down');
  const err = await w.run("api('GET', '/api/memory/shop/observations').then(() => null, (e) => ({ status: e.status, body: e.body, message: e.message }))");
  const e = plain(err);
  assert.equal(e.status, 503);
  assert.equal(e.body.state, 'down');
  assert.equal(e.body.up, false);
  assert.match(e.message, /connection refused/);
  assert.equal(plain(await w.run("api('GET', '/api/memory/health')")).state, 'up', 'the health route is never a variant');
});

test('?mem=stale and ?mem=incompatible are laid over the fixture', async () => {
  let out = plain(await demoWorld('?demo=1&mem=stale').run("api('GET', '/api/memory/shop/palace')"));
  assert.equal(out.stale, true);
  assert.ok(out.stale_at && out.wings.length > 0, 'the stale answer still has its data');
  out = plain(await demoWorld('?demo=1&mem=incompatible').run("api('GET', '/api/memory/shop/observations')"));
  assert.equal(out.state, 'incompatible');
  assert.deepEqual(out.items, []);
  out = plain(await demoWorld('?demo=1&mem=nonsense').run("api('GET', '/api/memory/shop/observations')"));
  assert.deepEqual(out, fxFor('memory_observations', '/api/memory/shop/observations'), 'an unknown flag answers the fixture as it is');
});
