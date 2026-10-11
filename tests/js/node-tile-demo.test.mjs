// Contract tests for issue #144 in ?demo=1 (demo/nodes.json): the quad with a remote tile and its composer, a read only node, an offline node that keeps its last lines, and the pick list
// grouped by node. Real scripts on minidom's DOM through tests/js/termworld.mjs; the board's api() is the demo's own (core.js demoApi), nothing leaves the page.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { FakeRO, all, box, btn, enter, flush, go, line, page, plain, poll, qrOf, text, tile, type } from './termworld.mjs';
import { hubWorld, setState } from './hubworld.mjs';

async function demoQuad(hash) {
  const h = hubWorld({ search: '?demo=1', realApi: true, realLive: true, extra: { innerWidth: 1280, ResizeObserver: FakeRO, matchMedia: (q) => ({ matches: /840|1024/.test(q), addEventListener() {}, removeEventListener() {} }) } });
  const { w } = h;
  w.load('termkit.js');
  w.load('pages/quad.js');
  w.run('TermKit.tileMenu = undefined; TermKit.composer = undefined; TermKit.tune = undefined;');
  setState(w, plain(await w.run("api('GET', '/api/state')")));
  await poll(w);
  await flush(12);
  await go(w, hash);
  await flush(12);
  return h;
}
const DEMO = '#/quad?l=6&s=build-box/shop--api--s1,build-box/shop--web--t-cart-page,alice-mac/notes--site--s1,old-laptop/scratch--play--s1';
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

test('demo: the quad shows a remote tile with its tail and its composer, a read only node that cannot be typed into, and an offline node that keeps its last lines', async () => {
  const { w } = await demoQuad(DEMO);
  const ok = tile(w, 'build-box/shop--api--s1');
  assert.ok(ok, 'a remote tile');
  assert.match(text(qrOf(w, 'build-box/shop--api--s1').querySelector('pre')), /Claude needs your permission to run: git push origin task\/orders-pagination/, 'the fixture\'s tail, as text');
  assert.equal(box(ok).disabled, false, 'the composer');
  assert.equal(ok.querySelector('iframe'), null);
  const ro = tile(w, 'alice-mac/notes--site--s1');
  assert.equal(box(ro).disabled, true);
  assert.equal(line(ro), 'needs the sessions scope on alice-mac');
  const off = tile(w, 'old-laptop/scratch--play--s1');
  assert.match(line(off), /^offline since /);
  assert.match(text(qrOf(w, 'old-laptop/scratch--play--s1').querySelector('pre')), /Idle\. Waiting for your next prompt\./, 'the last lines are kept');
  assert.equal(box(off).disabled, true);
  assert.ok(off.querySelector('[data-act=terminal]'));
});

test('demo: a prompt typed into the remote tile goes through the demo relay: sent, busy with Queue it, and the draft stays on a refusal', async () => {
  const { w } = await demoQuad(DEMO);
  const send = async (key, t) => { type(box(tile(w, key)), t); enter(box(tile(w, key))); await wait(260); await flush(8); return line(tile(w, key)); };
  assert.equal(await send('build-box/shop--api--s1', 'go on'), 'Sent through the relay to build-box.');
  assert.equal(box(tile(w, 'build-box/shop--api--s1')).value, '');
  assert.equal(await send('build-box/shop--web--t-cart-page', 'after the tests'), 'The session is working. Try again in 8 seconds.');
  assert.equal(box(tile(w, 'build-box/shop--web--t-cart-page')).value, 'after the tests', 'the draft stays');
  btn(tile(w, 'build-box/shop--web--t-cart-page'), 'Queue it').click();
  await wait(260);
  await flush(8);
  assert.equal(line(tile(w, 'build-box/shop--web--t-cart-page')), 'Queued on build-box. It is typed when the session is ready.');
});

test('demo: GET /api/mcp/tokens answers the remote endpoint off, no device and the nodes switch off, so Settings, Agents shows its read only line', async () => {
  const { w } = await demoQuad('#/quad?l=2');
  const r = plain(await w.run("api('GET', '/api/mcp/tokens')"));
  assert.deepEqual(r.nodes, { enabled: false, env: 'CCBOARD_MCP_NODES' });
  assert.equal(r.enabled, false);
  assert.deepEqual(r.tokens, []);
});

test('demo: closing a tile shows the pick list grouped by node, with the offline node\'s word', async () => {
  const { w } = await demoQuad('#/quad?l=2&s=build-box/shop--api--s1,build-box/shop--web--t-cart-page');
  tile(w, 'build-box/shop--web--t-cart-page').querySelector('.qt-close').click();
  await flush(4);
  const groups = all(all(page(w), '.qempty')[0], '.qe-group');
  assert.ok(groups.some((g) => g.getAttribute('aria-label') === 'On build-box'));
  const old = groups.find((g) => g.getAttribute('aria-label') === 'On old-laptop');
  assert.ok(old);
  assert.match(text(old.querySelector('.qe-gs')), /^offline since /);
});
