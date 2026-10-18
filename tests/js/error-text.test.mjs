// #309: errorText() (core.js, beside api()) turns an error from api() or fetch into words for a panel, and the tmux-down banner on Home names this platform's
// start command and opens Doctor. Real scripts on minidom through world.mjs's homeWorld; errors are built inside the page's context, as api() builds them.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { fakeState, homeWorld, text, tick } from './world.mjs';

const { w } = homeWorld();
const say = (src) => w.run(`errorText(${src})`);
const httpError = (status, message, body) => `Object.assign(new Error(${JSON.stringify(message)}), { status: ${status}, body: ${JSON.stringify(body)} })`;

test('no response at all (Failed to fetch, Load failed, NetworkError) is "the board did not answer"', () => {
  assert.equal(say("new TypeError('Failed to fetch')"), 'the board did not answer');
  assert.equal(say("new TypeError('Load failed')"), 'the board did not answer');
  assert.equal(say("new Error('NetworkError when attempting to fetch resource.')"), 'the board did not answer');
  assert.equal(say('null'), 'the board did not answer');
});

test('an HTTP 5xx with no sentence from the board names the status, not "503 Service Unavailable"', () => {
  assert.equal(say(httpError(503, '503 Service Unavailable', null)), 'the board answered with an error (HTTP 503)');
  assert.equal(say(httpError(502, '502 ', { html: true })), 'the board answered with an error (HTTP 502)');
});

test('a sentence the board wrote (body error or detail) is kept as is, whatever the status', () => {
  assert.equal(say(httpError(503, 'ccboard-tmux is not running (sudo systemctl start ccboard-tmux)', { error: 'ccboard-tmux is not running (sudo systemctl start ccboard-tmux)' })),
    'ccboard-tmux is not running (sudo systemctl start ccboard-tmux)');
  assert.equal(say(httpError(404, '404 Not Found', { detail: 'no such project' })), 'no such project');
  assert.equal(say(httpError(422, '422 Unprocessable', { detail: [{ loc: ['body'] }] })), 'the board answered with an error (HTTP 422)');
  assert.equal(say("new Error('the board answered without a checklist')"), 'the board answered without a checklist');
});

// ---------------------------------------------------------------- the tmux-down banner

function banner(over) {
  const { w: b } = homeWorld({ state: fakeState({ tmux_down: true, ...over }) });
  b.run('renderBanner()');
  const node = b.document.querySelector('#banner');
  return { b, line: text(node.querySelector('span')), code: node.querySelector('code'), open: node.querySelectorAll('button').find((x) => text(x) === 'Open Doctor') };
}

test('tmux down: the banner shows the start command the state carries and an Open Doctor button', () => {
  for (const cmd of ['sudo systemctl start ccboard-tmux', 'launchctl kickstart gui/$(id -u)/dev.ccboard.tmux']) {
    const r = banner({ tmux_fix: cmd });
    assert.equal(r.line, `The ccboard tmux server is not running. Start it on the box: ${cmd}`);
    assert.equal(text(r.code), cmd);
    assert.ok(r.open && r.open.classList.contains('primary') && r.open.classList.contains('small'));
  }
});

test('the command wraps only at its spaces: one code, copyable text equal to tmux_fix, every word an unbreakable piece', () => {
  const cmd = 'launchctl kickstart gui/$(id -u)/dev.ccboard.tmux';
  const r = banner({ tmux_fix: cmd });
  assert.equal(text(r.code), cmd);
  const toks = r.code.querySelectorAll('span.bn-tok');
  assert.deepEqual(toks.map(text), cmd.split(' '));
  const css = fs.readFileSync(path.join(STATIC, 'pages.css'), 'utf8');
  const rule = css.match(/#banner \.bn-tok \{([^}]*)\}/);
  assert.ok(rule && /white-space:\s*nowrap/.test(rule[1]) && /overflow-wrap:\s*normal/.test(rule[1]), 'the words are nowrap, so the span\'s anywhere-wrapping cannot split one');
});

test('tmux down on a board that sends no command: the plain sentence, still with the button', () => {
  const r = banner({});
  assert.equal(r.line, 'The ccboard tmux server is not running.');
  assert.equal(r.code, null);
  assert.ok(r.open);
});

test('Open Doctor goes to Settings > Doctor', async () => {
  const r = banner({ tmux_fix: 'sudo systemctl start ccboard-tmux' });
  r.open.click();
  await tick();
  assert.equal(r.b.location.hash, '#/settings?sec=doctor');
});

test('tmux up: no banner', () => {
  const { w: b } = homeWorld({ state: fakeState({ tmux_down: false }) });
  b.run('renderBanner()');
  assert.equal(b.document.querySelector('#banner').querySelectorAll('button').length, 0);
});
