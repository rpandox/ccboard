// #79: the '+2 queued' chip (components.js queuedPromptsChip / queuedPromptsSet) on the Agents row, keyed to the state row's `queued_prompts`
// (the prompts the BOARD itself holds for a busy session; never a count of text typed into Claude's own terminal). Real scripts on minidom.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fakeState, homeWorld, projectsOf, sess, text } from './world.mjs';

const TITLE = 'prompts the board will send when the session is free';
const shown = (n) => !!n && !n.classList.contains('hidden');

function build(w, s, opts) {
  w.ctx.__s = { ...s, project: 'shop', repo: 'api' };
  w.ctx.__stx = fakeState({ projects: projectsOf({ shop: { api: [s] } }) });
  w.ctx.__o = opts;
  w.run('state = __stx; globalThis.__row = sessionCard(__s, __o); __row.ccPatch(__s)');
  return w.get('__row');
}
const repatch = (w, s) => { w.ctx.__s = { ...s, project: 'shop', repo: 'api' }; w.run('__row.ccPatch(__s)'); };

test('the chip says "+N queued" with the board-only title, and shows nothing for zero or junk', () => {
  const { w } = homeWorld();
  w.run('globalThis.__q = queuedPromptsChip()');
  const q = w.get('__q');
  assert.ok(!shown(q), 'built hidden');
  w.ctx.__n = 2;
  w.run('queuedPromptsSet(__q, __n)');
  assert.ok(shown(q));
  assert.equal(text(q), '+2 queued');
  assert.equal(q.getAttribute('title'), TITLE);
  assert.match(q.getAttribute('aria-label'), /^\+2 queued: prompts the board will send when the session is free$/);
  for (const bad of [0, -1, null, undefined, 'x', NaN]) {
    w.ctx.__n = bad;
    w.run('queuedPromptsSet(__q, __n)');
    assert.ok(!shown(q), `${String(bad)} shows nothing`);
  }
  w.ctx.__n = 1;
  w.run('queuedPromptsSet(__q, __n)');
  assert.equal(text(q), '+1 queued');
});

test('the rich Agents row shows the chip while queued_prompts > 0 and clears on the next poll', () => {
  const { w } = homeWorld();
  const row = build(w, sess('shop', 'api', 's1', { queued_prompts: 2 }), { rich: true });
  const chip = row.querySelector('.bdg-queued');
  assert.ok(shown(chip));
  assert.equal(text(chip), '+2 queued');
  assert.equal(chip.getAttribute('title'), TITLE);
  repatch(w, sess('shop', 'api', 's1', { queued_prompts: 3 }));
  assert.equal(text(row.querySelector('.bdg-queued')), '+3 queued');
  repatch(w, sess('shop', 'api', 's1', { queued_prompts: 0 }));
  assert.ok(!shown(row.querySelector('.bdg-queued')), 'the queue ran dry: gone without a reload');
  const none = build(w, sess('shop', 'api', 's2'), { rich: true });
  assert.ok(!shown(none.querySelector('.bdg-queued')), 'no queued_prompts at all: no chip');
});

test('the demo state has one session that shows "+1 queued" and no other', () => {
  const d = JSON.parse(readFileSync(new URL('../../app/static/demo/state.json', import.meta.url), 'utf8'));
  const all = [];
  for (const p of d.projects) for (const r of [...(p.repos || []), p.root].filter(Boolean)) all.push(...(r.sessions || []));
  all.push(...d.projects.flatMap((p) => p.orphan_sessions || []));
  const q = all.filter((s) => s.queued_prompts > 0);
  assert.equal(q.length, 1);
  assert.equal(q[0].queued_prompts, 1);
  assert.ok(d.tasks.some((t) => t.tmux === q[0].tmux && t.phase === 'running' && t.turns_ahead > 0), 'a board-queued task on that session backs it');
});

test('the quad tile header and the terminal header wear the same chip, fed by queued_prompts', () => {
  for (const f of ['pages/quad.js', 'term.js']) {
    const src = readFileSync(new URL(`../../app/static/${f}`, import.meta.url), 'utf8');
    assert.match(src, /queuedPromptsChip\(\)/, `${f} builds the chip`);
    assert.match(src, /queuedPromptsSet\([^)]*queued_prompts\)/, `${f} sets it from queued_prompts`);
  }
});
