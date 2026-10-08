// #31: Home's quiet caption while the server backs off its project scan on a busy box (state.scan_slow). Real Home on minidom (tests/js/world.mjs).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fixtureState, homeWorld, page, setState, text } from './world.mjs';

function mounted(st) {
  const { w } = homeWorld({ state: st });
  w.location.hash = '#/';
  return w;
}
const caption = (w) => page(w).querySelector('.home-scan-slow');
const shown = (n) => !!n && !n.classList.contains('hidden');

test('the caption "scan slowed (box is busy)" shows only while state.scan_slow is true, above the project list', () => {
  const w = mounted(fixtureState({ scan_slow: false }));
  const c = caption(w);
  assert.ok(c, 'the caption node is there from the mount');
  assert.equal(shown(c), false, 'hidden while the box is not busy');
  setState(w, fixtureState({ scan_slow: true }));
  assert.equal(shown(caption(w)), true);
  assert.equal(text(caption(w)), 'scan slowed (box is busy)');
  assert.doesNotMatch(text(caption(w)), /fail/i, 'it never says failed');
  const kids = page(w).children;
  assert.ok(kids.indexOf(caption(w)) < kids.indexOf(page(w).querySelector('.pblocks')), 'right above the project and session list it is about');
  assert.ok(caption(w).classList.contains('dim') && !caption(w).classList.contains('home-note'), 'a quiet caption, not the empty-list note');
  setState(w, fixtureState({ scan_slow: false }));
  assert.equal(shown(caption(w)), false, 'gone again once the board stops backing off');
  setState(w, fixtureState());
  assert.equal(shown(caption(w)), false, 'a state without the key (an older board) shows nothing');
});

test('the caption stays hidden while the page shows offline cached values (they have their own banner) and on a board with no project', () => {
  const w = mounted(fixtureState({ scan_slow: true }));
  assert.equal(shown(caption(w)), true);
  w.run('ui.offline = Date.now()');
  setState(w, fixtureState({ scan_slow: true }));
  assert.equal(shown(caption(w)), false);
  w.run('ui.offline = false');
  setState(w, fixtureState({ scan_slow: true, projects: [] }));
  assert.equal(shown(caption(w)), false);
});
