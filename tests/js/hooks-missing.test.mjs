// #96: the 'no hooks (untrusted?)' chip (components.js hooksMissingChip) on the Agents row and the peek (pages/agents.js sessionCard), keyed
// to the state row's `hooks_missing` ('untrusted' | 'bypass' | null; agents/codex.hooks_missing on the box). Real scripts on minidom.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeState, homeWorld, projectsOf, sess, text } from './world.mjs';

const UNTRUSTED = 'No hook event since your first prompt. Trust the ccboard hooks once: type /hooks in a Codex terminal. See Settings > Doctor.';
const shown = (n) => !!n && !n.classList.contains('hidden');

function build(w, s, opts) {
  w.ctx.__s = { ...s, project: 'shop', repo: 'api' };
  w.ctx.__stx = fakeState({ projects: projectsOf({ shop: { api: [s] } }) });
  w.ctx.__o = opts;
  w.run('state = __stx; globalThis.__row = sessionCard(__s, __o); __row.ccPatch(__s)');
  return w.get('__row');
}
const repatch = (w, s) => { w.ctx.__s = { ...s, project: 'shop', repo: 'api' }; w.run('__row.ccPatch(__s)'); };
const codex = (over = {}) => sess('shop', 'api', 'cx1', { agent: 'codex', launcher: 'codex', command: 'codex', flags: { turn_seen_at: '2026-10-08T10:00:00+00:00' }, ...over });

test('the chip: text, glyph, muted amber link to the Doctor, the explanation as its title; bypass says the flag is set', () => {
  const { w } = homeWorld();
  const a = w.run('hooksMissingChip("untrusted")');
  assert.equal(a.tagName.toLowerCase(), 'a');
  assert.ok(a.classList.contains('bdg-hooks'));
  assert.equal(text(a).trim(), '◇ no hooks (untrusted?)');
  assert.equal(a.getAttribute('href'), '/#/settings?sec=doctor');
  assert.equal(a.getAttribute('title'), UNTRUSTED);
  const b = w.run('hooksMissingChip("bypass")');
  assert.match(b.getAttribute('title'), /CCBOARD_CODEX_HOOK_TRUST=bypass is set and hooks should be running/);
  assert.doesNotMatch(b.getAttribute('title'), /type \/hooks/);
});

test('the rich Agents row shows the chip only while hooks_missing, and clears on the next poll', () => {
  const { w } = homeWorld();
  const row = build(w, codex({ hooks_missing: 'untrusted' }), { rich: true });
  const host = row.querySelector('.rr-hooks');
  assert.ok(shown(host) && host.querySelector('a.bdg-hooks'));
  repatch(w, codex({ hooks_missing: null, flags: { hook_seen: true } }));
  assert.equal(shown(row.querySelector('.rr-hooks')), false, 'a hook arrived: gone without a reload');
  const fresh = build(w, codex({ hooks_missing: null, flags: {} }), { rich: true });
  assert.equal(shown(fresh.querySelector('.rr-hooks')), false, 'a fresh launch with no prompt has no chip');
  const claude = build(w, sess('shop', 'api', 's1'), { rich: true });
  assert.equal(shown(claude.querySelector('.rr-hooks')), false);
});

test('the peek says it in words, not only in a title (touch and keyboard), and keeps the state as it was', () => {
  const { w } = homeWorld();
  const peek = build(w, codex({ hooks_missing: 'untrusted', state: 'idle' }), { peek: true, perm: true, showProject: true, link: false });
  const note = peek.querySelector('.peek-hooks');
  assert.ok(shown(note));
  assert.equal(text(note.querySelector('.peek-hooks-why')), UNTRUSTED);
  assert.ok(note.querySelector('a.bdg-hooks[href="/#/settings?sec=doctor"]'));
  repatch(w, codex({ hooks_missing: 'bypass' }));
  assert.match(text(peek.querySelector('.peek-hooks-why')), /bypass is set/);
});
