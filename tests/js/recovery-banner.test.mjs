// #93: the Home recovery banner names the agents of the relaunched sessions (last_recovery.value.agents), so a recovered Codex row is not called a Claude
// one, and a record from before that field says only "N sessions". Real core.js .. home.js on minidom through tests/js/world.mjs.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeState, homeWorld, text } from './world.mjs';

function bannerText(value) {
  const { w } = homeWorld({ state: fakeState({ last_recovery: { value } }) });
  w.run('renderBanner()');
  const b = w.document.querySelector('#banner');
  return { line: text(b.querySelector('span')), warn: b.classList.contains('warn'), buttons: b.querySelectorAll('button').map(text) };
}

test('two Claude and one Codex session are counted by agent, not all called Claude', () => {
  const r = bannerText({ recovered: ['a--x--s1', 'a--x--s2', 'a--x--cx1'], closed: [], skipped: [], agents: { 'a--x--s1': 'claude', 'a--x--s2': 'claude', 'a--x--cx1': 'codex' } });
  assert.equal(r.line, 'After a restart, 2 Claude and 1 Codex sessions relaunched: a--x--s1, a--x--s2, a--x--cx1');
  assert.ok(r.warn);
  assert.deepEqual(r.buttons, ['dismiss']);
});

test('a lone Codex session is a Codex session and the old "with --resume" claim is gone', () => {
  const r = bannerText({ recovered: ['a--x--cx1'], closed: ['a--x--sh'], skipped: [], agents: { 'a--x--cx1': 'codex' } });
  assert.equal(r.line, 'After a restart, 1 Codex session relaunched: a--x--cx1 · closed: a--x--sh');
  assert.ok(!/--resume|Claude/.test(r.line));
});

test('a lone Claude session, with the continue note', () => {
  const r = bannerText({ recovered: ['a--x--s1'], closed: [], skipped: [], continue: ['a--x--s1'], agents: { 'a--x--s1': 'claude' } });
  assert.equal(r.line, 'After a restart, 1 Claude session relaunched: a--x--s1 · was working, continue typed once back: a--x--s1');
});

test('a record without agents (written by an older board) names no agent at all', () => {
  const r = bannerText({ recovered: ['a--x--s1', 'a--x--cx1'], closed: [], skipped: [] });
  assert.equal(r.line, 'After a restart, 2 sessions relaunched: a--x--s1, a--x--cx1');
});
