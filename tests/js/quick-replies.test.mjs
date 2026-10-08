// #69: quick replies per agent. components.js QUICK_DEFAULTS_BY_AGENT is the one source of the words; quickDefaults / quickLoad / quickSave take
// the session's agent, and the Agents row, the inbox card and the palette read the same list (pages/agents.js SESSION_NUDGES and Palette.NUDGES
// are gone). Real scripts on minidom.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { fakeState, homeWorld, projectsOf, sess, text } from './world.mjs';

const CLAUDE = ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'];
const CODEX = ['continue', '/status', '/compact', '/new'];
const KEY = 'ccboard:quick:shop--api--cx1';

function domWorld() {
  const w = makeWorld();
  installDom(w);
  w.load('core.js');
  w.load('components.js');
  return w;
}

test('quickDefaults per agent: Claude six, the Codex set, nothing for a shell; an unknown agent keeps Claude\'s', () => {
  const w = domWorld();
  assert.deepEqual(plain(w.run('quickDefaults("claude")')), CLAUDE);
  assert.deepEqual(plain(w.run('quickDefaults("codex")')), CODEX);
  assert.deepEqual(plain(w.run('quickDefaults("shell")')), []);
  assert.deepEqual(plain(w.run('quickDefaults(null)')), CLAUDE);
  assert.deepEqual(plain(w.run('QUICK_DEFAULTS')), CLAUDE, 'the old name is the Claude list, not a copy of the words');
  assert.equal(w.run('QUICK_DEFAULTS === QUICK_DEFAULTS_BY_AGENT.claude'), true);
  w.run('quickDefaults("codex").push("x")');
  assert.deepEqual(plain(w.run('quickDefaults("codex")')), CODEX, 'a copy each time');
});

test('the Codex set never types a model inline, never opens a picker and never pretends to approve', () => {
  const w = domWorld();
  const codex = plain(w.run('QUICK_DEFAULTS_BY_AGENT.codex'));
  for (const t of codex) {
    assert.doesNotMatch(t, /^\/(model|permissions|approvals|reasoning)\b/, t);
    assert.doesNotMatch(t, /^\/\S+\s+\S/, `${t}: no inline argument`);
    assert.ok(!['y', 'yes', 'n', 'no', 'allow', 'deny'].includes(t.toLowerCase()), t);
  }
});

test('a saved list equal to ITS agent\'s defaults removes the key; a Claude list is not the Codex default', () => {
  const w = domWorld();
  w.run(`quickSave('shop--api--cx1', ${JSON.stringify(CODEX)}, 'codex')`);
  assert.equal(w.localStorage.getItem(KEY), null);
  w.run(`quickSave('shop--api--cx1', ${JSON.stringify(CLAUDE)}, 'codex')`);
  assert.deepEqual(JSON.parse(w.localStorage.getItem(KEY)), CLAUDE, 'kept: for a Codex session the Claude six are an edited list');
  assert.deepEqual(plain(w.run(`quickLoad('shop--api--cx1', 'codex')`)), CLAUDE);
  w.run(`quickSave('shop--api--cx1', null, 'codex')`);
  assert.equal(w.localStorage.getItem(KEY), null);
  assert.deepEqual(plain(w.run(`quickLoad('shop--api--cx1', 'codex')`)), CODEX);
  w.localStorage.setItem('ccboard:quick:shop--api--s1', JSON.stringify(['my', 'list']));
  assert.deepEqual(plain(w.run(`quickLoad('shop--api--s1', 'claude')`)), ['my', 'list'], 'an existing saved Claude list loads unchanged');
});

test('the editor\'s Reset restores the agent\'s list', () => {
  const w = domWorld();
  w.localStorage.setItem(KEY, JSON.stringify(['x']));
  w.run(`quickReplyEditor({ items: quickLoad('shop--api--cx1', 'codex'), defaults: quickDefaults('codex'), onSave: (v) => quickSave('shop--api--cx1', v, 'codex') })`);
  const ed = w.document.querySelector('dialog.qr-editor');
  ed.querySelector('.qr-reset').click();
  assert.deepEqual(ed.querySelectorAll('input').map((i) => i.value), CODEX);
  ed.querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
  assert.equal(w.localStorage.getItem(KEY), null, 'reset + save = the Codex defaults again');
});

function card(w, s, opts) {
  w.ctx.__s = { ...s, project: 'shop', repo: 'api' };
  w.ctx.__stx = fakeState({ projects: projectsOf({ shop: { api: [s] } }) });
  w.ctx.__o = opts;
  w.run('state = __stx; globalThis.__row = sessionCard(__s, __o)');
  return w.get('__row');
}
const chipTexts = (node) => node.querySelectorAll('.chips .chip-btn').map((b) => text(b));

test('the Agents row shows the session\'s agent\'s chips: Claude six for Claude, the Codex set for Codex, each titled as a prompt', () => {
  const { w } = homeWorld();
  assert.deepEqual(chipTexts(card(w, sess('shop', 'api', 's1'), { rich: true })), CLAUDE);
  const cx = card(w, sess('shop', 'api', 'cx1', { agent: 'codex', launcher: 'codex', command: 'codex' }), { rich: true });
  assert.deepEqual(chipTexts(cx), CODEX);
  assert.match(cx.querySelector('.chips .chip-btn').getAttribute('title'), /as a prompt/);
  w.localStorage.setItem(KEY, JSON.stringify(['ship it']));
  assert.deepEqual(chipTexts(card(w, sess('shop', 'api', 'cx1', { agent: 'codex' }), { rich: true })), ['ship it'], 'an edited list is the same everywhere');
});

test('the inbox card reads the same per-agent list', () => {
  const { w } = homeWorld();
  const s = sess('shop', 'api', 'cx1', { agent: 'codex', launcher: 'codex', state: 'waiting', needs_attention: true });
  w.ctx.__s = { ...s, project: 'shop', repo: 'api' };
  w.ctx.__stx = fakeState({ projects: projectsOf({ shop: { api: [s] } }) });
  w.run('state = __stx; globalThis.__ib = inboxCard(__s, __stx)');
  assert.deepEqual(w.get('__ib').querySelectorAll('.ib-chips .chip-btn').filter((b) => !b.classList.contains('chip-more')).map((b) => text(b)), CODEX);
});
