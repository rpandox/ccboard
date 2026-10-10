// #126: the Preview control says "unavailable" and why BEFORE the click (state.preview from app/previews.py capability()): a disabled button, the reason as
// visible text and aria-describedby; no toast after a click that could never work. Real scripts on minidom through tests/js/world.mjs.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeState, homeWorld, text } from './world.mjs';

const SESSION = { tmux: 'shop--api--s1', name: 's1', project: 'shop', repo: 'api', agent: 'claude', state: 'working', state_at: new Date().toISOString(), flags: {} };
const TASK = (over = {}) => ({ id: 7, project: 'shop', repo: 'api', slug: 'fix', title: 'Fix it', branch: 'task/fix', base: 'main', worktree: '/w', tmux: SESSION.tmux,
  agent: 'claude', mode: 'worktree', phase: 'running', column: 'in_progress', session: SESSION, overlap: [], created_at: new Date().toISOString(), cost_usd: null,
  pr_url: null, preview_url: null, preview_port: null, ...over });

function card(preview, task = TASK()) {
  const { w } = homeWorld({ state: fakeState({ preview }) });
  w.ctx.__t = task;
  return { w, card: w.run('taskCard(__t, {})') };
}
const previewButtons = (c) => c.querySelectorAll('[data-act="preview"]');

test('unavailable: a disabled Preview, the reason in words next to it, and aria-describedby pointing at that text', () => {
  const reason = 'Tailscale is not logged in on this device';
  const { w, card: c } = card({ available: false, code: 'not_signed_in', reason });
  const [b] = previewButtons(c);
  assert.equal(previewButtons(c).length, 1, 'one Preview control, the disabled one');
  assert.ok(b.disabled === true || b.hasAttribute('disabled'));
  assert.equal(text(b), 'Preview');
  const why = c.querySelector('.tk-pv-why');
  assert.equal(text(why), `unavailable: ${reason}`);
  assert.equal(b.getAttribute('aria-describedby'), why.getAttribute('id'));
  assert.ok(why.getAttribute('id').includes('7'), 'one id per card');
  // the click does nothing: no API call, no toast
  b.click();
  assert.equal(w.ctx.__calls.length, 0);
  assert.equal(w.ctx.__toasts.length, 0);
});

test('every reason the server gives shows as it is, and none of them is a toast', () => {
  for (const reason of ['CCBOARD_PUBLIC_URL is not set (rerun install.sh)', 'tailscale is not installed (https://tailscale.com/download)',
    'tailscale serve failed; on the box run: sudo tailscale set --operator=alice', 'Tailscale runs on the Windows side of this WSL2 distro, which has no route to expose a preview']) {
    const { card: c } = card({ available: false, code: 'x', reason });
    assert.equal(text(c.querySelector('.tk-pv-why')), `unavailable: ${reason}`);
  }
});

test('available (or an older board with no state.preview): the normal Preview action, no reason line', () => {
  for (const preview of [{ available: true, code: 'ok', reason: null }, undefined, null]) {
    const { card: c } = card(preview);
    assert.equal(c.querySelector('.tk-pv'), null);
    const b = previewButtons(c)[0];
    assert.ok(b, 'Preview is offered');
    assert.ok(!(b.disabled === true || b.hasAttribute('disabled')));
  }
});

test('a task that already has a preview keeps its link even when the capability turned off; a card with no live session shows none', () => {
  const { card: c } = card({ available: false, code: 'not_signed_in', reason: 'x' }, TASK({ preview_url: 'https://box.example.ts.net:9100/', preview_port: 5173 }));
  assert.equal(c.querySelector('.tk-pv'), null);
  assert.ok(c.querySelectorAll('a').some((a) => /Preview :5173/.test(text(a))));
  const done = card({ available: false, code: 'x', reason: 'x' }, TASK({ phase: 'done', column: 'done', session: null, tmux: '' })).card;
  assert.equal(done.querySelector('.tk-pv'), null);
});
