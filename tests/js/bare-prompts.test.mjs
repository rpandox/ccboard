import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { makeWorld, STATIC } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { homeWorld, fakeState, plain, text, tick } from './world.mjs';

const name = 'bare-test';
const device = { ts_id: 'device-1', name: 'Device', sessions: [{ tmux_name: name, agent: 'claude', repo: 'repo', state: 'dialog' }] };
const pr = { id: 276000000001, kind: 'terminal_prompt', tmux_name: name, bare_device: device.ts_id,
  summary: 'Bash command: npm test', tool_name: 'Bash', choices: [{number: '1', label: 'Yes'}, {number: '2', label: 'No'}] };

function world() {
  const { w } = homeWorld({ state: fakeState({ bare_devices: [structuredClone(device)], pending_permissions: [structuredClone(pr)] }) });
  w.location.assign = (url) => { w.ctx.destination = url; };
  w.run(`globalThis.item = Inbox.items(state).find((s) => s.bare_device);
    globalThis.card = inboxCard(item, state); document.body.append(card);
    globalThis.recorded = []; api = async (...args) => { recorded.push(args); return {attach_url:'/term/bare-ticket-test'}; };
    poll = async () => {};`);
  return w;
}

test('device dialog uses the ordinary Needs you card and routes Allow and Deny to its device', async () => {
  const w = world();
  assert.equal(w.get('item.kind'), 'terminal_prompt');
  assert.match(text(w.get('card')), /Bash command: npm test/);
  assert.match(text(w.get('card')), /claude on Device/);
  for (const decision of ['Allow', 'Deny']) {
    w.get('card').querySelectorAll('button').find((b) => text(b) === decision).click(); await tick();
    w.run('actPending.clear()');                                 // the poll that confirms an answer drops it (#318: a second answer waits for that)
  }
  assert.deepEqual(plain(w.get('recorded')), [
    ['POST', `/api/bare/device-1/sessions/${name}/allow`, {pid: pr.id}],
    ['POST', `/api/bare/device-1/sessions/${name}/deny`, {pid: pr.id}],
  ]);
  assert.ok(!w.get('card').querySelector('.ib-replybtn'), 'a dialog is answered with Allow or Deny, never a typed reply');
  assert.ok(!w.get('card').querySelectorAll('button').some((b) => text(b) === 'Ack'));
});

test('Needs you reply sends to the device and Terminal requests a ticket', async () => {
  const w = world();
  w.get('card').querySelector('textarea').value = 'Review this change';
  await w.run(`sessionSend(item, card.querySelector('textarea'))`);
  w.get('card').querySelectorAll('button').find((b) => text(b) === 'Terminal').click(); await tick();
  assert.deepEqual(plain(w.get('recorded')), [
    ['POST', `/api/bare/device-1/sessions/${name}/reply`, {text:'Review this change'}],
    ['POST', `/api/bare/device-1/sessions/${name}/attach`, {}],
  ]);
  assert.equal(w.ctx.destination, '/term/bare-ticket-test');
});

test('sidebar shows needs you, preserves reply drafts across state updates, and uses the reply route', async () => {
  const w = world();
  w.run('Shell.open = new Set(); Shell.patchTrees = () => {};');
  w.load('bare.js');
  w.run(`Shell.open.add('bare:device-1'); globalThis.tree = el('div'); Shell.bareDevice(tree, state.bare_devices[0]);`);
  assert.match(text(w.get('tree')), /needs you/);
  assert.ok(!w.get('tree').querySelector('textarea'), 'a dialog row has no reply box');
  w.run(`state.bare_devices[0].sessions[0].state = 'waiting'; Shell.bareDevice(tree, state.bare_devices[0]);`);
  let input = w.get('tree').querySelector('textarea');
  input.value = 'A draft'; input.dispatchEvent({type:'input'});
  w.run(`Shell.bareDevice(tree, state.bare_devices[0]);`);
  input = w.get('tree').querySelector('textarea');
  assert.equal(input.value, 'A draft');
  w.get('tree').querySelector('form').dispatchEvent({type:'submit', preventDefault(){}}); await tick();
  assert.deepEqual(plain(w.get('recorded[0]')), ['POST', `/api/bare/device-1/sessions/${name}/reply`, {text:'A draft'}]);
});

test('only a waiting device session offers Reply and quick replies', () => {
  const w = world();
  for (const [state, want] of [['waiting', true], ['dialog', false], ['working', false], ['unknown', false], ['unconfirmed', false]]) {
    assert.equal(w.run(`sessionNudgeable({bare_device:'device-1', state:'${state}', agent:'claude'})`), want, state);
  }
});

function sidebar(state = 'dialog') {
  const w = world();
  w.run('Shell.open = new Set(); Shell.patchTrees = () => {};');
  w.load('bare.js');
  w.run(`state.bare_devices[0].sessions[0].state = '${state}'; Shell.open.add('bare:device-1'); globalThis.tree = el('div'); Shell.bareDevice(tree, state.bare_devices[0]);`);
  return w;
}

test('a state change while typing a reply keeps the focus, caret and text', () => {
  const w = sidebar('waiting');
  const input = w.get('tree').querySelector('textarea');
  w.get('tree').querySelectorAll('button').find((b) => /^Reply to /.test(b.getAttribute('aria-label') || '')).click();
  input.value = 'Half a reply'; input.dispatchEvent({type:'input'}); input.focus();
  w.run(`state.bare_devices[0].sessions[0].state = 'working'; Shell.bareDevice(tree, state.bare_devices[0]);`);
  const after = w.get('tree').querySelector('textarea');
  assert.equal(after, input, 'the same textarea element survives');
  assert.equal(w.get('document.activeElement'), input);
  assert.equal(after.value, 'Half a reply');
  assert.match(text(w.get('tree')), /waiting/, 'the stale row is repainted once the focus leaves');
  w.run('globalThis.other = el("button"); document.body.append(other); other.focus()');
  w.run(`Shell.bareDevice(tree, state.bare_devices[0]);`);
  assert.match(text(w.get('tree')), /working/);
  assert.ok(!w.get('tree').querySelector('textarea'), 'a row that no longer waits has no reply box');
  w.run(`state.bare_devices[0].sessions[0].state = 'waiting'; Shell.bareDevice(tree, state.bare_devices[0]);`);
  assert.equal(w.get('tree').querySelector('textarea').value, 'Half a reply', 'the draft still survives the repaint');
});

test('the sidebar offers Reply, as a compact named icon button, on waiting rows only', () => {
  for (const [state, want] of [['waiting', true], ['dialog', false], ['working', false], ['unconfirmed', false], ['stopping', false], ['unknown', false]]) {
    const w = sidebar(state);
    const reply = w.get('tree').querySelectorAll('button').filter((b) => /^Reply to /.test(b.getAttribute('aria-label') || ''));
    assert.equal(reply.length, want ? 1 : 0, state);
    assert.equal(w.get('tree').querySelectorAll('textarea').length, want ? 1 : 0, state + ' reply box');
    assert.ok(!w.get('tree').querySelectorAll('button').some((b) => text(b) === 'Reply'), state + ': no text Reply button');
    if (!want) continue;
    const b = reply[0];
    assert.equal(b.getAttribute('aria-label'), 'Reply to claude repo on Device');
    assert.equal(b.getAttribute('title'), 'Reply to claude repo on Device');
    assert.ok(b.classList.contains('icon') && b.classList.contains('tn-add'), 'same compact class as terminal and stop');
    assert.ok(b.querySelector('.bp5-icon-comment'));
    assert.equal(text(b), '');
    assert.equal(b.getAttribute('aria-expanded'), 'false');
    b.click();
    assert.equal(b.getAttribute('aria-expanded'), 'true');
    assert.ok(!w.get('tree').querySelector('form').classList.contains('hidden'), 'it opens the reply box');
  }
});

test('an armed Stop stays armed across a state refresh and is rebuilt once cancelled', () => {
  const w = sidebar();
  const stop = () => w.get('tree').querySelectorAll('button').find((b) => /^Stop |^Confirm stop /.test(b.getAttribute('aria-label') || ''));
  const armed = stop(); armed.click();
  assert.match(text(armed), /Confirm/);
  w.run(`state.bare_devices[0].sessions[0].state = 'working'; Shell.bareDevice(tree, state.bare_devices[0]);`);
  assert.equal(stop(), armed);
  assert.match(text(stop()), /Confirm/);
  w.get('tree').querySelectorAll('button').find((b) => text(b) === 'Cancel').click();
  w.run(`Shell.bareDevice(tree, state.bare_devices[0]);`);
  assert.doesNotMatch(text(w.get('tree')), /needs you/);
  assert.match(text(w.get('tree')), /working/);
});

function demoWorld() {
  const urls = [];
  const w = makeWorld({ fetch: async (url) => {
    urls.push(String(url));
    assert.match(String(url), /^\/static\/demo\/[\w-]+\.json$/);
    return { ok:true, json:async () => JSON.parse(fs.readFileSync(path.join(STATIC, String(url).slice('/static/'.length)), 'utf8')) };
  } });
  installDom(w); w.location.search = '?demo=1';
  w.load('core.js'); w.load('bare-play.js');
  return {w, urls};
}

for (const decision of ['allow', 'deny']) test(`demo ${decision} consumes the dialog entirely in memory`, async () => {
  const {w, urls} = demoWorld();
  await w.run(`api('GET','/api/state').then((s) => { globalThis.demoState = s; globalThis.pr = s.pending_permissions.find((p) => p.bare_device); })`);
  assert.equal(w.get('pr.kind'), 'terminal_prompt');
  const base = '/api/bare/demo-lab-pi/sessions/bare-demo-claude/';
  await w.run(`api('POST', '${base + decision}', {pid:pr.id})`);
  await assert.rejects(w.run(`api('POST', '${base + decision}', {pid:pr.id})`), /nothing was sent/);
  if (decision === 'deny') await w.run(`api('POST', '${base}reply', {text:'Try another approach'})`);
  const st = await w.run(`api('GET','/api/state')`);
  assert.equal(st.pending_permissions.filter((p) => p.bare_device).length, 0);
  assert.equal(st.bare_devices[0].sessions[0].state, 'working');
  assert.ok(urls.every((s) => s.startsWith('/static/demo/')));
});
