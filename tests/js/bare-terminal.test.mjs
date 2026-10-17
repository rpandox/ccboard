import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { makeWorld, STATIC } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { hubWorld, boardState, tick, plain } from './hubworld.mjs';

const ticket = 'bare-ticket-' + '0'.repeat(64);

test('sidebar issues a ticket only on Open terminal and navigates without a host', async () => {
  const device = { ts_id: 'device-1', name: 'Device', sessions: [{ tmux_name: 'bare-test', agent: 'shell', repo: 'repo', state: 'alive' }] };
  const { w } = hubWorld({ state: boardState({ bare_devices: [device] }), nodes: [] });
  w.location.assign = (url) => { w.ctx.__destination = url; };
  w.load('bare.js');
  w.run(`Shell.open.add('bare:device-1'); globalThis.row = el('div'); Shell.bareDevice(row, state.bare_devices[0]);
    api = async (...args) => { __calls.push(args); return {attach_url: '/term/' + '${ticket}'}; };`);
  assert.equal(w.get('__calls.length'), 0);
  const button = w.get('row').querySelectorAll('button').find((b) => (b.getAttribute('aria-label') || '').startsWith('Open terminal for '));
  assert.equal(button.getAttribute('title'), button.getAttribute('aria-label'));
  button.click(); await tick();
  assert.deepEqual(plain(w.get('__calls')), [['POST', '/api/bare/device-1/sessions/bare-test/attach', {}]]);
  assert.equal(w.ctx.__destination, '/term/' + ticket);
});

function termWorld(name, demo) {
  const w = makeWorld({ fetch: async () => { assert.fail('terminal fetched a network route'); } });
  installDom(w);
  w.location.pathname = '/term/' + name;
  w.location.search = demo ? '?demo=1' : '';
  for (const id of ['headid', 'back', 'ctxstrip', 'rail', 'histchip', 'keyhost', 'quickrow', 'sendform', 'tty']) {
    const node = w.document.createElement(id === 'tty' ? 'iframe' : 'div');
    node.setAttribute('id', id);
    w.document.body.append(node);
  }
  w.load('core.js');
  w.load('termkit.js');
  w.run('TermKit.bind = () => {}; TermKit.viewportFit = () => {};');
  w.load('term.js');
  return w;
}

test('device terminal mounts the ordinary ttyd path with only the ticket argument', () => {
  const w = termWorld(ticket, false);
  assert.equal(w.document.querySelector('#tty').src, '/tty/?arg=' + ticket);
  for (const id of ['rail', 'keyhost', 'quickrow', 'sendform']) assert.ok(w.document.querySelector('#' + id).classList.contains('hidden'));
  assert.equal(w.get('document.title'), 'Device terminal · ccboard');
  assert.ok(w.document.body.classList.contains('terminal-only'));
});

test('a demo device terminal shows a local placeholder and never loads ttyd', () => {
  const w = termWorld(ticket, true), frame = w.document.querySelector('#tty'), pre = w.document.querySelector('.demo-term');
  assert.ok(!frame.src && frame.hidden);
  assert.match(pre.textContent, /Demo terminal: no device connection/);
});

test('a normal demo terminal keeps the full page (the QA harness screens it)', () => {
  const src = fs.readFileSync(path.join(STATIC, 'term.js'), 'utf8');
  assert.match(src, /\n  if \(BARE\) \{\n/);                 // only a device ticket takes the terminal-only branch
  assert.doesNotMatch(src, /BARE \|\| demoOn\(\)/);
});

test('lab-pi demo row Open terminal uses only static fixtures', async () => {
  const fetches = [];
  const w = makeWorld({ fetch: async (url) => {
    fetches.push(String(url));
    assert.match(String(url), /^\/static\/demo\/[\w-]+\.json$/);
    return { ok: true, json: async () => JSON.parse(fs.readFileSync(path.join(STATIC, String(url).slice('/static/'.length)), 'utf8')) };
  } });
  installDom(w);
  w.location.search = '?demo=1';
  w.location.assign = (url) => { w.ctx.__destination = url; };
  for (const file of ['core.js', 'components.js', 'bare-play.js', 'nodes.js', 'shell.js', 'bare.js']) w.load(file);
  await w.run(`api('GET', '/api/bare').then((data) => { globalThis.device = data.devices[0]; })`);
  assert.equal(w.get('device.name'), 'lab-pi');
  w.run(`Shell.open.add('bare:' + device.ts_id); globalThis.row = el('div'); Shell.bareDevice(row, device);`);
  w.get('row').querySelectorAll('button').find((b) => (b.getAttribute('aria-label') || '').startsWith('Open terminal for ')).click();
  await tick();
  assert.equal(w.ctx.__destination, '/term/' + ticket + '?demo=1');
  assert.ok(fetches.every((url) => url.startsWith('/static/demo/')));
});

test('normal terminal URL arguments remain byte-for-byte unchanged', () => {
  const w = makeWorld();
  w.load('termkit.js');
  const url = w.get('TermKit.ttyUrl');
  assert.equal(url('shop--api--s1'), '/tty/?arg=shop--api--s1');
  assert.equal(url('shop--api--s1', {mode: 'grid'}), '/tty/?arg=shop--api--s1&arg=grid&fontSize=11&rendererType=canvas&disableResizeOverlay=true&disableReconnect=true');
  assert.equal(url('shop--api--s1', {mode: 'ro'}), '/tty/?arg=shop--api--s1&arg=ro');
});
