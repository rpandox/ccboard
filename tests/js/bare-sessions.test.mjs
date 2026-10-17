import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import { hubWorld, boardState, plain, text, tick } from './hubworld.mjs';

function world() {
  const device = { ts_id: 'device-1', name: '<device>', check: { ok: true, repos: ['repo one'], rows: [{ key: 'claude', ok: true }, { key: 'codex', ok: true }] }, sessions: [] };
  const { w } = hubWorld({ state: boardState({ nodes_enabled: false, bare_devices: [device] }), nodes: [] });
  w.load('bare.js');
  w.run(`Shell.open.add('bare:device-1')`);
  w.run(`globalThis.__timers = new Map(); globalThis.__timerId = 0;
    setTimeout = (fn, ms) => { const id = ++__timerId; __timers.set(id, {fn, ms}); return id; };
    clearTimeout = (id) => __timers.delete(id);`);
  return w;
}

test('bare picker works without paired boards and the forms carry explicit modes', () => {
  const w = world();
  w.run(`globalThis.kit = lxnKit({}, null, 'session')`);
  assert.match(text(w.get('kit.pick')), /<device> \(no board\)/);
  assert.equal(w.get('kit.current()'), 'bare:device-1');
  const form = w.get('kit.ctl.form');
  const selects = form.querySelectorAll('select');
  // MiniDOM does not implement the browser's automatic first-option selection.
  selects[0].value = 'repo one';
  for (const [agent, mode] of [['claude', 'default'], ['codex', 'workspace-write'], ['shell', undefined]]) {
    selects[1].value = agent; selects[1].dispatchEvent({ type: 'change' });
    const body = plain(w.get('kit.ctl.body()'));
    assert.equal(body.agent, agent); assert.equal(body.mode, mode);
    assert.deepEqual(Object.keys(body).sort(), agent === 'shell' ? ['agent', 'repo'] : ['agent', 'effort', 'mode', 'model', 'repo']);
  }
  assert.equal(w.get(`lxnKit({}, null, 'task')`), null);
});

test('a lost start answer stays unconfirmed and is never retried', async () => {
  const w = world();
  w.run(`api = async (...args) => { __calls.push(args); throw new Error('lost'); }; globalThis.f = lxbForm(state.bare_devices[0], {});`);
  const selects = w.get('f.form').querySelectorAll('select');
  selects[0].value = 'repo one'; selects[1].value = 'claude'; selects[1].dispatchEvent({ type: 'change' });
  await w.run('f.submit()'); await w.run('f.submit()');
  assert.equal(w.get('__calls.length'), 1);
  assert.match(text(w.get('f.status')), /Unconfirmed/);
  assert.equal(w.get('f.go.disabled'), true);
});

test('sidebar read is on demand and stop needs two taps, even after state refresh', async () => {
  const w = world();
  w.run(`state.bare_devices[0].sessions = [{tmux_name:'bare-abc', agent:'shell', repo:'repo one', state:'alive'}];
    globalThis.n = el('div'); Shell.bareDevice(n, state.bare_devices[0]);
    api = async (method, path, body) => { __calls.push({method, path, body}); return {sessions:[], status:'stopping'}; };
    state.bare_devices = JSON.parse(JSON.stringify(state.bare_devices));
    Shell.bareDevice(n, state.bare_devices[0]);`);
  assert.equal(w.get('__calls.length'), 0);
  assert.match(text(w.get('n')), /no board/);
  assert.equal(w.get('n').querySelector('img'), null);
  const stop = w.get('n').querySelectorAll('button').find((b) => (b.getAttribute('aria-label') || '').startsWith('Stop '));
  stop.click(); assert.equal(w.get('__calls.length'), 0);
  assert.match(text(stop), /Confirm/);
  stop.click(); await tick();
  assert.deepEqual(plain(w.get('__calls[0].body')), { confirm: true });
  assert.equal(w.get('state.bare_devices[0].sessions[0].state'), 'stopping');
  const read = w.get('n').querySelectorAll('button').find((b) => (b.getAttribute('aria-label') || '').startsWith('Refresh sessions on '));
  assert.equal(read.getAttribute('aria-label'), 'Refresh sessions on <device>');
  assert.equal(read.getAttribute('title'), read.getAttribute('aria-label'));
  read.click(); await tick();
  assert.equal(w.get('__calls.length'), 2);
  assert.equal(w.get('state.bare_devices[0].sessions.length'), 0);
});

test('device sessions expand and collapse through the sidebar tree without a remote read', () => {
  const w = world();
  w.run(`state.bare_devices[0].sessions = [{tmux_name:'bare-abc', agent:'claude', repo:'repo one', state:'alive'}];
    globalThis.tree = el('div'); tree._empty = el('div'); Shell.trees = [tree];
    Shell.bindTree(tree); Shell.patchTrees();`);
  const tree = w.get('tree');
  assert.equal(text(tree.querySelector('.sb-h')), 'Devices (no board)');
  assert.equal(tree.querySelector('.bare-tree .tn-row').getAttribute('aria-expanded'), 'true');
  assert.equal(tree.querySelector('.bare-tree .s-row').getAttribute('aria-level'), '2');
  tree.querySelector('.bare-tree .tn-row').click();
  assert.equal(tree.querySelector('.bare-tree .tn-row').getAttribute('aria-expanded'), 'false');
  assert.equal(tree.querySelector('.bare-tree .s-row'), null);
  tree.querySelector('.bare-tree .tn-row').click();
  assert.equal(tree.querySelector('.bare-tree .tn-row').getAttribute('aria-expanded'), 'true');
  assert.ok(tree.querySelector('.bare-tree .s-row'));
  assert.equal(w.get('__calls.length'), 0);
});


test('Stop names the target accessibly and disarms on Cancel or after five seconds', () => {
  const w = world();
  w.run(`state.bare_devices[0].sessions = [{tmux_name:'bare-abc', agent:'claude', repo:'repo one', state:'alive'}];
    globalThis.n = el('div'); Shell.bareDevice(n, state.bare_devices[0]);`);
  const buttons = () => w.get('n').querySelectorAll('button');
  const stop = buttons().find((b) => (b.getAttribute('aria-label') || '').startsWith('Stop '));
  assert.equal(stop.getAttribute('aria-label'), 'Stop claude repo one on <device>');
  assert.equal(stop.getAttribute('title'), stop.getAttribute('aria-label'));
  const cancel = buttons().find((b) => text(b) === 'Cancel');
  assert.equal(cancel.hasAttribute('hidden'), true);
  stop.click();
  assert.equal(stop.getAttribute('aria-label'), 'Confirm stop claude repo one on <device>');
  assert.equal(text(w.get('n').querySelector('[role="status"]')), 'Tap Confirm to stop, or Cancel.');
  assert.equal(cancel.hasAttribute('hidden'), false);
  assert.equal(cancel.classList.contains('hidden'), false);
  assert.equal(w.get('__timers.size'), 1);
  cancel.click();
  assert.equal(stop.getAttribute('aria-label'), 'Stop claude repo one on <device>');
  assert.equal(text(w.get('n').querySelector('[role="status"]')), '');
  assert.ok(stop.querySelector('.bp5-icon-stop'));
  assert.equal(w.get('__timers.size'), 0);
  stop.click();
  assert.equal(w.get('[...__timers.values()][0].ms'), 5000);
  w.run('[...__timers.values()][0].fn()');
  assert.equal(stop.getAttribute('aria-label'), 'Stop claude repo one on <device>');
  assert.equal(text(w.get('n').querySelector('[role="status"]')), '');
  assert.ok(stop.querySelector('.bp5-icon-stop'));
  assert.equal(cancel.classList.contains('hidden'), true);
  stop.click();
  assert.equal(text(stop), 'Confirm');
  assert.equal(w.get('__calls.length'), 0);
});


test('sidebar rows use compact controls, truncate names, and hide unarmed Cancel', () => {
  const css = fs.readFileSync(new URL('../../app/static/shell.css', import.meta.url), 'utf8');
  assert.match(css, /@media \(max-width:600px\) \{ \.bare-tree \.s-row\s*\{[^}]*flex-wrap:wrap/);
  assert.match(css, /\.tn-name\s*\{[^}]*text-overflow:ellipsis[^}]*white-space:nowrap/);
  assert.match(css, /\.bare-controls\s*\{[^}]*flex:none[^}]*align-items:center/);
  assert.match(css, /\.bare-controls \.hidden, \.bare-controls \[hidden\]\s*\{[^}]*display:none !important/);
  assert.match(css, /\.bare-badge\s*\{[^}]*flex:none[^}]*border-radius:999px[^}]*background:var\(--panel\)[^}]*color:var\(--dim\)/);
});


test('reading a pending stop keeps Stop disabled', async () => {
  const w = world();
  w.run(`state.bare_devices[0].sessions = [{tmux_name:'bare-abc', agent:'shell', repo:'repo one', state:'stopping'}];
    globalThis.tree = el('div'); tree._empty = el('div'); Shell.trees = [tree]; Shell.patchTrees();
    api = async () => ({sessions: state.bare_devices[0].sessions});`);
  const tree = w.get('tree');
  const stop = () => tree.querySelectorAll('button').find((b) => (b.getAttribute('aria-label') || '').startsWith('Stop '));
  assert.equal(stop().disabled, true);
  tree.querySelectorAll('button').find((b) => (b.getAttribute('aria-label') || '').startsWith('Refresh ')).click();
  await tick();
  assert.equal(stop().disabled, true);
});

for (const outcome of ['missing', 'reject', 'loaded']) {
  test(`bare lazy load handles ${outcome} without a repatch loop`, async () => {
    const w = world();
    w.ctx.__outcome = outcome;
    w.run(`delete Shell.bareDevice; globalThis.loads = 0; globalThis.patches = 0;
      globalThis.Lazy = {}; Lazy.load = () => { loads++; return __outcome === 'reject' ? Promise.reject(new Error('unavailable')) : Promise.resolve().then(() => {
        if (__outcome === 'loaded') Shell.bareDevice = () => {};
      }); };
      globalThis.tree = el('div'); tree._empty = el('div'); Shell.trees = [tree];
      const patch = Shell.patchTrees; Shell.patchTrees = () => { patches++; patch(); };
      Shell.patchTrees(); Shell.patchTrees();`);
    await tick(); await tick();
    assert.equal(w.get('loads'), 1);
    assert.equal(w.get('patches'), outcome === 'loaded' ? 3 : 2);
    if (outcome === 'missing') {
      w.run('Shell.patchTrees()'); await tick();
      assert.equal(w.get('loads'), 1);
    }
    if (outcome === 'reject') assert.equal(w.get('Shell._bareLoad'), null);
  });
}
