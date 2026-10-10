// The single-board contract of issue #139: with state.nodes_enabled false (or absent) Home, Tasks, Agents, the inbox, the sidebar and the palette render byte for byte as they did
// before the hub view, and no request goes to /api/nodes*. The stored dumps (tests/js/snapshots/single-board.json) were taken from the v0.5.36 sources with tests/js/singleboard.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { renderBoard } from './singleboard.mjs';

const SNAP = JSON.parse(fs.readFileSync(path.join(path.dirname(new URL(import.meta.url).pathname), 'snapshots', 'single-board.json'), 'utf8'));
const KEYS = ['home', 'tasks', 'agents', 'inbox', 'sidebar', 'palette', 'paletteQuery'];

function same(got, key) {
  if (got[key] === SNAP[key]) return;
  const a = got[key].split('\n');
  const b = SNAP[key].split('\n');
  const i = a.findIndex((l, j) => l !== b[j]);
  assert.fail(`${key} differs from the single-board snapshot at line ${i + 1}:\n  now:    ${a[i]}\n  before: ${b[i]}`);
}

for (const [label, flag] of [['absent', undefined], ['false', false]]) {
  test(`state.nodes_enabled ${label}: Home, Tasks, Agents, the inbox, the sidebar and the palette are the stored render, byte for byte`, async () => {
    const got = await renderBoard({ nodesEnabled: flag });
    for (const k of KEYS) same(got, k);
  });

  test(`state.nodes_enabled ${label}: nothing asks /api/nodes*, no hub poller runs, no chip is drawn`, async () => {
    const got = await renderBoard({ nodesEnabled: flag });
    assert.deepEqual(got.requests, []);
    const w = got.w;
    assert.equal(w.run('Nodes.ready'), false, 'the hub bundle is not loaded (nothing asked for it)');
    assert.equal(w.document.querySelectorAll('.nchip').length, 0);
    assert.equal(w.document.querySelectorAll('.nd-strip, .nd-inbox, .nd-tfilter, .nd-tasks').length, 0);
    assert.equal(w.run('Nodes.chip(null)'), null);
    assert.equal(w.run("typeof Nodes.sbItems"), 'undefined');
  });
}

test('with state.nodes_enabled true the same board draws the node chip on its own rows (the snapshot test above would catch a leak the other way)', async () => {
  const got = await renderBoard({ nodesEnabled: true });
  assert.notEqual(got.agents, SNAP.agents);
  assert.match(got.agents, /<span\.local\.nchip /);
  assert.match(got.agents, /"BO"\n[\s\S]*"box"/, 'the dim chip: monogram and name of this board');
});

test('the snapshot is real: it holds the pages it names', () => {
  for (const k of KEYS) assert.ok(SNAP[k].length > 500, k);
  assert.match(SNAP.home, /Home/);
  assert.match(SNAP.sidebar, /ccboard/);
  assert.match(SNAP.palette, /Sessions/);
  assert.ok(STATIC);
});
