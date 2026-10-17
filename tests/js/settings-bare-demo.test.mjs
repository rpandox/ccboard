import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { tick, text } from './world.mjs';

async function demoWorld() {
  const requests = [];
  const w = makeWorld({ fetch: async (url) => {
    requests.push(String(url));
    assert.match(String(url), /^\/static\/demo\/[\w-]+\.json$/, 'demo must never fetch a real API route');
    return { ok: true, json: async () => JSON.parse(fs.readFileSync(path.join(STATIC, String(url).slice('/static/'.length)), 'utf8')) };
  } });
  installDom(w);
  w.location.search = '?demo=1';
  for (const file of ['core.js', 'components.js', 'router.js', 'bare-play.js', 'pages/settings.js']) w.load(file);
  w.run('document.body.append(settingsBareFrame())');
  await w.run('settingsBareLoad()');
  return { w, requests };
}
const section = (w) => w.document.body.querySelector('.bare-section');
const button = (w, word) => section(w).querySelectorAll('button').find((b) => text(b) === word);
const settle = async () => { for (let i = 0; i < 8; i++) await tick(); };

test('demo renders one candidate and the saved device with every check, Codex fix and two repos', async () => {
  const { w } = await demoWorld();
  const options = section(w).querySelectorAll('option');
  assert.equal(options.length, 1);
  assert.equal(text(options[0]), 'studio-mini · macOS · online');
  const data = plain(await w.run("api('GET', '/api/bare')"));
  assert.equal(data.candidates[0].same_user, true);
  assert.equal(data.devices[0].online, true);
  assert.equal(data.devices[0].os, 'Linux');
  const device = section(w).querySelector('.bare-device');
  assert.match(text(device.querySelector('h3')), /lab-pi · ccagent/);
  const rows = device.querySelectorAll('.bare-check');
  assert.equal(rows.length, 8);
  assert.deepEqual(data.devices[0].check.rows.map((r) => r.key), ['ssh', 'user', 'groups', 'system', 'tmux', 'claude', 'codex', 'repos']);
  for (const [i, label] of ['SSH works', 'non-root user', 'Not in sudo, wheel or admin', 'Linux', 'tmux 3.4', 'claude is on', 'codex is not on', 'Found 2 repositories'].entries()) {
    assert.ok(text(rows[i]).includes(label));
    assert.equal(text(rows[i].querySelector('span')), i === 6 ? '✕' : '✓');
  }
  assert.match(text(rows[6]), /Install codex for ccagent, then Check again\./);
  assert.deepEqual(device.querySelectorAll('.bare-repos li').map(text), ['example-api', 'example-web']);
});

test('demo check, forget host key, remove and add stay local and retain their results on refresh', async () => {
  const { w, requests } = await demoWorld();
  button(w, 'Check again').click();
  await settle();
  assert.equal(section(w).querySelectorAll('.bare-check').length, 8);
  // Make the existing recovery control visible, then exercise its real click handler.
  w.run(`settingsBare.result = null;
    settingsBare.data.devices[0].check.rows[0] = { key: 'ssh', ok: false, action: 'forget_host_key', message: 'Host key changed.', fix: 'Verify the key.' };
    settingsBarePaint();`);
  button(w, 'Forget host key').click();
  await settle();
  assert.match(text(section(w)), /Host key forgotten\. Check again to pin/);
  button(w, 'Check again').click();
  await settle();
  assert.equal(section(w).querySelectorAll('.bare-check').length, 8);
  // Open sessions block removal. Stop and explicitly read them before removing.
  await assert.rejects(w.run("api('DELETE', '/api/bare/demo-lab-pi')"), (e) => e.status === 409);
  w.run(`globalThis.rows = settingsBare.data.devices[0].sessions`);
  for (const row of plain(w.get('rows'))) {
    w.ctx.__row = row;
    await w.run("api('POST', '/api/bare/demo-lab-pi/sessions/' + __row.tmux_name + '/stop', {confirm:true})");
  }
  await w.run("api('POST', '/api/bare/demo-lab-pi/sessions/read', {})");
  button(w, 'Remove device').click();
  await settle();
  button(w, 'Refresh devices').click();
  await settle();
  assert.equal(section(w).querySelectorAll('.bare-device').length, 0);
  section(w).querySelector('select').value = 'demo-lab-pi';
  button(w, 'Add and check').click();
  await settle();
  assert.match(text(section(w).querySelector('h3')), /lab-pi · ccagent/);
  assert.match(text(section(w)), /lab-pi runs Linux\./);
  assert.equal(section(w).querySelectorAll('.bare-check').length, 8);
  assert.ok(requests.includes('/static/demo/bare.json'));
  assert.ok(requests.every((url) => url.startsWith('/static/demo/')));
});


test('demo launcher and sidebar have two live device sessions; start/read/stop persist across state reads', async () => {
  const { w, requests } = await demoWorld();
  for (const file of ['nodes.js', 'shell.js', 'launcher.js', 'bare.js']) w.load(file);
  w.run('Shell.patchTrees = () => {};');
  await w.run("api('GET', '/api/state').then((s) => { state = s; })");
  assert.equal(w.get('state.bare_devices.length'), 1);
  assert.deepEqual(plain(w.get('state.bare_devices[0].sessions.map((s) => [s.agent, s.state])')), [['claude', 'dialog'], ['shell', 'waiting']]);
  w.run("Shell.open.add('bare:demo-lab-pi'); globalThis.kit = lxnKit({}, null, 'session'); globalThis.sidebar = el('div'); Shell.bareDevice(sidebar, state.bare_devices[0]);");
  assert.match(text(w.get('kit.pick')), /lab-pi \(no board\)/);
  assert.equal(w.get('sidebar').querySelectorAll('.s-row').length, 2);
  const selects = w.get('kit.ctl.form').querySelectorAll('select');
  selects[0].value = w.get('state.bare_devices[0].check.repos[0]');
  selects[1].value = 'claude'; selects[1].dispatchEvent({type:'change'});
  await w.run('kit.ctl.submit()');
  await w.run("api('GET', '/api/state').then((s) => { state = s; })");
  assert.equal(w.get('state.bare_devices[0].sessions.length'), 3);
  w.run("globalThis.started = state.bare_devices[0].sessions.at(-1).tmux_name");
  await w.run("api('POST', '/api/bare/demo-lab-pi/sessions/' + started + '/stop', {confirm:true})");
  await w.run("api('POST', '/api/bare/demo-lab-pi/sessions/read', {})");
  await w.run("api('GET', '/api/state').then((s) => { state = s; })");
  assert.equal(w.get('state.bare_devices[0].sessions.length'), 2);
  assert.ok(requests.every((url) => url.startsWith('/static/demo/')));
});
