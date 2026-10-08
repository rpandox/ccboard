// Home with projects but no live session: "No sessions running" with the next step, instead of zero chips and a closed 'older' line.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fixtureState, homeWorld, page, setState, text } from './world.mjs';

const noSessions = (st) => ({
  ...st,
  projects: (st.projects || []).map((p) => ({
    ...p,
    root: p.root ? { ...p.root, sessions: [] } : p.root,
    orphan_sessions: [],
    repos: (p.repos || []).map((r) => ({ ...r, sessions: [] })),
  })),
});
function mounted(st, hash) {
  const { w } = homeWorld({ state: st });
  w.location.hash = hash || '#/';
  return w;
}
const idle = (w) => page(w).querySelector('.home-idle');
const shown = (n) => !!n && !n.classList.contains('hidden');

test('projects but no live session: "No sessions running", the project count and the two ways to start', () => {
  const st = noSessions(fixtureState());
  const w = mounted(st);
  const n = idle(w);
  assert.equal(shown(n), true);
  assert.match(text(n), /No sessions running/);
  assert.match(text(n), new RegExp(`${st.projects.length} projects?, none with a session running`));
  assert.doesNotMatch(text(page(w)), /\bnull\b|\bundefined\b|NaN/, 'no raw null anywhere on Home');
  const buttons = n.querySelectorAll('button').map((b) => [b.textContent, b.classList.contains('primary')]);
  assert.deepEqual(buttons, [['New session', true], ['New task', false]], 'one filled primary, then the quiet second action');
  const kids = page(w).children;
  assert.ok(kids.indexOf(n) < kids.indexOf(page(w).querySelector('.pblocks')), 'above the project list (the folded older block stays under it)');
  assert.equal(shown(page(w).querySelector('.home-note')), false, 'the one-line note is not shown twice');
});

test('it goes away when a session is live, and stays out of the way of a filter and of the no-projects state', () => {
  const w = mounted(noSessions(fixtureState()));
  assert.equal(shown(idle(w)), true);
  setState(w, fixtureState());
  assert.equal(shown(idle(w)), false, 'live sessions: the blocks speak for themselves');
  const f = mounted(noSessions(fixtureState()), '#/?f=working');
  assert.equal(shown(idle(f)), false, 'a filter on: the filter note says "Nothing is working right now." instead');
  assert.match(text(page(f).querySelector('.home-note')), /Nothing is working right now\./);
  const e = mounted(noSessions(fixtureState({ projects: [] })));
  assert.equal(shown(idle(e)), false, 'no project at all: the "No projects yet" state, not this one');
});
