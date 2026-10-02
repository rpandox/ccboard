// core.js, components.js and termkit.js (when it exists) are definition-only: loading them must not touch the DOM,
// storage, the network, listeners or timers. main.js (and term.js) are the only files that start anything.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld } from './harness.mjs';

const trap = (what) => new Proxy(function () {}, {
  get(_t, prop) { if (prop === Symbol.toPrimitive || prop === 'then') return undefined; throw new Error(`load-time access to ${what}.${String(prop)}`); },
  apply() { throw new Error(`load-time call of ${what}()`); },
  construct() { throw new Error(`load-time construction of ${what}`); },
});

function hostileWorld() {
  const boom = (what) => () => { throw new Error(`load-time call of ${what}()`); };
  return makeWorld({
    document: trap('document'), localStorage: trap('localStorage'), sessionStorage: trap('sessionStorage'), navigator: trap('navigator'),
    location: trap('location'), history: trap('history'), matchMedia: boom('matchMedia'),
    addEventListener: boom('window.addEventListener'), setTimeout: boom('setTimeout'), setInterval: boom('setInterval'),
    requestAnimationFrame: boom('requestAnimationFrame'), fetch: boom('fetch'),
    EventSource: function EventSource() { throw new Error('load-time construction of EventSource'); },
  });
}

for (const file of ['core.js', 'components.js', 'termkit.js']) {
  test(`${file} defines only: no DOM, storage, network, listener or timer access at load`, (t) => {
    const abs = path.join(STATIC, file);
    if (!fs.existsSync(abs)) {
      if (file === 'termkit.js') return t.skip('termkit.js does not exist yet');      // arrives with the terminal page phase
      assert.fail(`${file} missing: expected ${abs}`);
    }
    const w = hostileWorld();
    w.load('core.js');                                       // components.js and termkit.js build on core.js
    if (file !== 'core.js') w.load(file);
  });
}
