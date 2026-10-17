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

test('demo lab-pi shows the state log installed; Remove and Install need a second tap and stay in memory', async () => {
  const { w, requests } = await demoWorld();
  const device = () => section(w).querySelector('.bare-device');
  assert.match(text(device().querySelector('.bare-statelog')), /State log installed/);
  assert.match(text(device().querySelector('.bare-statelog')), /no prompt text/);
  assert.equal(button(w, 'Install state log'), undefined);
  button(w, 'Remove state log').click();                               // first tap only arms
  const log = () => device().querySelector('.bare-statelog');
  const inLog = (word) => log().querySelectorAll('button').find((b) => text(b) === word);
  assert.ok(inLog('Confirm') && inLog('Cancel'));
  assert.equal(plain(await w.run("api('GET', '/api/bare')")).devices[0].state_log.installed, true);
  inLog('Cancel').click();
  assert.ok(button(w, 'Remove state log'));
  button(w, 'Remove state log').click();
  button(w, 'Confirm').click();
  await settle();
  assert.match(text(device().querySelector('.bare-statelog')), /State log not installed/);
  assert.equal(button(w, 'Remove state log'), undefined);
  assert.equal(plain(await w.run("api('GET', '/api/bare')")).devices[0].state_log, undefined);
  button(w, 'Check again').click();                                    // a check keeps the (absent) log state
  await settle();
  button(w, 'Install state log').click();
  button(w, 'Confirm').click();
  await settle();
  assert.match(text(device().querySelector('.bare-statelog')), /State log installed/);
  await assert.rejects(w.run("api('POST', '/api/bare/demo-lab-pi/state-log/install', {})"), (e) => e.status === 400);
  assert.ok(requests.every((url) => url.startsWith('/static/demo/')));
});

test('state log control survives a failing check, Install needs a passing one, and the Codex review sentence shows only with Codex', async () => {
  const { w } = await demoWorld();
  const log = () => section(w).querySelector('.bare-device .bare-statelog');
  const sentence = /Codex asks you to review new hooks once: run \/hooks in a Codex session on the device\./;
  // installed for Claude only: no Codex sentence
  assert.doesNotMatch(text(log()), sentence);
  // a failing check keeps Remove, enabled, and its text
  w.run(`settingsBare.result = null; settingsBare.data.devices[0].check.ok = false;
    settingsBare.data.devices[0].check.rows.find((r) => r.key === 'ssh').ok = false; settingsBarePaint();`);
  assert.ok(log(), 'the control stays while the log is installed');
  assert.ok(button(w, 'Remove state log') && !button(w, 'Remove state log').disabled);
  assert.equal(button(w, 'Install state log'), undefined);
  // installed with Codex: the sentence is in the installed text
  w.run("settingsBare.data.devices[0].state_log.agents = ['claude', 'codex']; settingsBarePaint();");
  assert.match(text(log()), sentence);
  // not installed and a failing check: nothing to install, so no control
  w.run("delete settingsBare.data.devices[0].state_log; settingsBarePaint();");
  assert.equal(log(), null);
  // not installed, passing check: Install is offered; the confirm text carries the sentence only when Codex was found
  w.run("settingsBare.data.devices[0].check.ok = true; settingsBare.data.devices[0].check.rows.find((r) => r.key === 'ssh').ok = true; settingsBarePaint();");
  assert.ok(button(w, 'Install state log') && !button(w, 'Install state log').disabled);
  button(w, 'Install state log').click();
  assert.doesNotMatch(text(log()), sentence);                           // the demo device has no Codex
  button(w, 'Cancel').click();
  w.run("settingsBare.data.devices[0].check.rows.find((r) => r.key === 'codex').ok = true; settingsBarePaint();");
  button(w, 'Install state log').click();
  assert.match(text(log()), sentence);
  assert.match(text(log()), /Tap Confirm, or Cancel\./);
});
