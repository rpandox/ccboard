// Settings > Nodes > Device check (issue #136): the optional layer that binds a node token to the paired device (whois) and asks the tailnet policy for the operation (whois+cap).
// The section shows the mode in words, a banner when the mode is stronger than the host can do, what the host reads, the grant and the tailscale serve command (shown, never applied),
// and the pair rows show the bound device. Real core.js, nodes.js, nodes-pair.js and pages/settings.js on minidom's DOM; the board's answers are the demo fixture app/static/demo/nodes-auth.json.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld } from './harness.mjs';
import { calls, fakeState, homeWorld, page, plain, tick, text } from './world.mjs';

const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'nodes-auth.json'), 'utf8'));
const answerOf = (name) => ({ ...FX.states[name], snippet: { ...FX.snippet, ...(FX.states[name].snippet || {}) } });
const STATES = Object.keys(FX.states);
const settle = async () => { for (let i = 0; i < 8; i++) await tick(); };
const CARD = { app: 'ccboard', api: 1, node_id: 'ts:nDEMO1CNTRL', name: 'box', os: { system: 'Linux', release: '6.8.0-demo', tailscale_os: 'linux' }, lanes: { cap: 3, running: 1, free: 2 }, handle: 'local' };

function pair(over) {
  return { peer_id: 'p-x', node_id: 'ts:nX', name: 'desk-pc', url: 'https://desk-pc.example.ts.net', scopes: ['read'], created_at: '2026-10-03T02:00:00+00:00', last_seen: null, direction: 'in', ...over };
}

// A Settings > Nodes page whose /api/nodes/auth answer is `auth` (an object, a function or an Error to throw); `pairs` are the nodes that call this board.
function authWorld({ auth = answerOf('token'), pairs = [] } = {}) {
  const { w } = homeWorld({ state: fakeState({ node: CARD, node_name: 'box' }) });
  w.ctx.__answers['/api/nodes/auth'] = typeof auth === 'function' ? auth : () => { if (auth instanceof Error) throw auth; return auth; };
  w.ctx.__answers['/api/nodes/discover'] = { at: new Date().toISOString(), tailscale: { ok: true, reason: null, variant: 'cli' }, rows: [] };
  w.ctx.__answers['/api/nodes'] = { nodes: [], pairs, at: new Date().toISOString() };
  w.ctx.__copied = [];
  w.ctx.navigator.clipboard = { writeText: async (t) => { w.ctx.__copied.push(t); } };
  w.location.hash = '#/settings?sec=nodes';
  return w;
}
const panel = (w) => page(w).querySelector('.settings-panel[data-sec=nodes]');
const section = (w) => panel(w).querySelector('.nd-auth');
const auth = (w, key) => section(w).querySelector(`[data-auth=${key}]`);
const authCalls = (w) => calls(w).filter((c) => c.path.startsWith('/api/nodes/auth')).map((c) => c.path);
const button = (root, word) => root.querySelectorAll('button').find((b) => text(b).trim() === word);

test('opening the tab reads the Device check once (a plain GET, nothing for Tailscale to answer), a poll repaint does not read it again, and Check asks with ?check=1', async () => {
  const w = authWorld();
  await settle();
  assert.deepEqual(authCalls(w), ['/api/nodes/auth']);
  for (let i = 0; i < 3; i++) w.run('updateCurrentPage(state)');
  await settle();
  assert.equal(authCalls(w).length, 1, 'no poll, no timer');
  button(panel(w), 'Check').click();
  await settle();
  assert.deepEqual(authCalls(w), ['/api/nodes/auth', '/api/nodes/auth?check=1']);
});

test('the default is token: the mode in words, no banner, nothing about Tailscale until Check, and no control that switches it', async () => {
  const w = authWorld();
  await settle();
  assert.equal(text(auth(w, 'mode')), 'token');
  assert.match(text(section(w)), /Pair token only\. The default device setting\. Node signatures still apply/);
  assert.equal(auth(w, 'banner'), null);
  assert.equal(auth(w, 'version'), null, 'the host is asked nothing in token mode');
  assert.match(text(section(w)), /The board asks Tailscale nothing in this mode\. Press Check/);
  assert.equal(section(w).querySelectorAll('input, select, textarea').length, 0, 'nothing here switches the mode');
  assert.match(text(section(w)), /Nothing on this page switches it/);
  assert.match(text(section(w)), /CCBOARD_NODE_AUTH/);
});

test('Check in token mode shows what the host reads: the version against 1.92, whether Tailscale answers, and Serve', async () => {
  const w = authWorld({ auth: (r) => answerOf(/check=1/.test(r.path) ? 'token-checked' : 'token') });
  await settle();
  button(panel(w), 'Check').click();
  await settle();
  assert.match(text(auth(w, 'version')), /1\.104\.1, 1\.92 or newer/);
  assert.match(text(auth(w, 'whois')), /Tailscale answers/);
  assert.match(text(auth(w, 'serve')), /no capability name is set/);
  assert.equal(text(auth(w, 'mode')), 'token');
});

const BANNERS = { bind: /listens beyond loopback/, old: /Tailscale 1\.88\.2 is older than 1\.92/, noserve: /no --accept-app-caps/, down: /does not answer who is calling/, nocap: /needs CCBOARD_NODE_CAP/, badmode: /not token, whois or whois\+cap/, unknown: /Could not confirm whois/ };

test('every demo state paints its mode, and a banner exactly where the mode is stronger than the host', async () => {
  for (const name of STATES) {
    const a = answerOf(name);
    const w = authWorld({ auth: a });
    await settle();
    assert.ok(section(w), name);
    assert.equal(text(auth(w, 'mode')), a.mode === 'who is' ? 'who is' : a.mode, name);
    const banner = auth(w, 'banner');
    if (BANNERS[name]) {
      assert.ok(banner, `${name} has a banner`);
      assert.match(text(banner), BANNERS[name], name);
      assert.equal(text(banner), a.banner, 'the board\'s own sentence');
      assert.equal(banner.parentNode.getAttribute('role'), 'status');
    } else assert.equal(banner, null, `${name} has no banner`);
    assert.doesNotMatch(text(section(w)), /—|–/, `${name}: no dashes in the words`);
  }
});

test('state is never colour only: every mode carries a word (default, on, cannot run) and the readings carry a glyph', async () => {
  for (const [name, want] of [['token', 'default'], ['whois', 'on'], ['whois-cap', 'on'], ['bind', 'cannot run'], ['nocap', 'cannot run'], ['badmode', 'cannot run']]) {
    const w = authWorld({ auth: answerOf(name) });
    await settle();
    assert.equal(text(section(w).querySelector('.kv .badge')).replace(/^[✓✕·!]\s*/, '').trim(), want, name);
  }
  const w = authWorld({ auth: answerOf('whois') });
  await settle();
  assert.match(text(auth(w, 'bind')), /^✓ loopback only/);
  assert.match(text(auth(w, 'version')), /^✓ /);
  const w2 = authWorld({ auth: answerOf('old') });
  await settle();
  assert.match(text(auth(w2, 'version')), /^✕ 1\.88\.2, older than 1\.92/);
  assert.match(text(auth(w2, 'serve')), /^✕ no, the capability flag is missing/);
});

test('a reading the board could not take is "to verify", never shown as fine', async () => {
  const w = authWorld({ auth: answerOf('unknown') });
  await settle();
  for (const key of ['bind', 'version', 'whois', 'serve']) {
    assert.match(text(auth(w, key)), /\(to verify\)|^! /, key);
    assert.doesNotMatch(text(auth(w, key)), /✓/, key);
  }
  assert.match(text(auth(w, 'bind')), /unknown \(to verify\)/);
});

test('an unknown CCBOARD_NODE_AUTH value is shown as typed, cannot run, and is never read as token', async () => {
  const w = authWorld({ auth: answerOf('badmode') });
  await settle();
  assert.equal(text(auth(w, 'mode')), 'who is');
  assert.match(text(section(w)), /cannot run/);
  assert.match(text(section(w)), /Unknown value/);
  assert.doesNotMatch(text(section(w)), /Pair token only/);
});

test('the grant: your devices first (placeholder login and domain), Tagged devices in the same control, the network access line explained, and Copy gives exactly the text shown', async () => {
  const w = authWorld({ auth: answerOf('whois') });
  await settle();
  const pre = () => section(w).querySelector('pre');
  const user = JSON.parse(text(pre()));
  assert.deepEqual(user.grants[0].src, ['<owner login>']);
  assert.deepEqual(user.grants[0].dst, ['autogroup:self']);
  assert.deepEqual(user.grants[0].ip, ['tcp:8443']);
  assert.deepEqual(Object.keys(user.grants[0].app), ['example.com/cap/ccboard']);
  assert.deepEqual(user.grants[0].app['example.com/cap/ccboard'], [{ ops: ['node.read', 'node.tasks', 'node.sessions', 'node.permissions'] }]);
  assert.match(text(section(w)), /example\.com\/cap\/ccboard stands for a domain you control/);
  assert.match(text(section(w)), /The ip line gives the network access: a grant with only app gives none/);
  const seg = section(w).querySelector('.seg-ctl');
  const cells = seg.querySelectorAll('.seg-btn');
  assert.deepEqual(cells.map(text), ['Your devices', 'Tagged devices']);
  assert.deepEqual(cells.map((c) => c.getAttribute('aria-pressed')), ['true', 'false']);
  cells[1].click();
  const tag = JSON.parse(text(pre()));
  assert.deepEqual([tag.grants[0].src, tag.grants[0].dst], [['tag:ccboard'], ['tag:ccboard']]);
  assert.deepEqual(section(w).querySelector('.seg-ctl').querySelectorAll('.seg-btn').map((c) => c.getAttribute('aria-pressed')), ['false', 'true']);
  assert.doesNotMatch(text(section(w)), /owner login> stands for/, 'the login note is for the user variant only');
  section(w).querySelector('.nd-auth-tools').querySelectorAll('button').find((b) => /^Copy/.test(text(b))).click();
  await settle();
  assert.deepEqual(w.ctx.__copied, [text(pre())]);
  assert.ok(!/@|\.ts\.net|\/Users\//.test(text(pre())), 'no real login, host or path in the grant');
});

test('the serve command and the version command each have a Copy; they are text, shown with the sentence that ccboard never runs them', async () => {
  const w = authWorld({ auth: answerOf('whois-cap') });
  await settle();
  const cmds = section(w).querySelectorAll('code.doc-cmd').map(text);
  assert.deepEqual(cmds, [FX.snippet.serve, 'tailscale version']);
  assert.match(cmds[0], /^tailscale serve --bg --https=\d+ --accept-app-caps=example\.com\/cap\/ccboard http:\/\/127\.0\.0\.1:\d+$/);
  for (const line of section(w).querySelectorAll('.doc-cmdline')) line.querySelectorAll('button').find((b) => /^Copy/.test(text(b))).click();
  await settle();
  assert.deepEqual(w.ctx.__copied, [FX.snippet.serve, 'tailscale version']);
  assert.match(text(section(w)), /Both texts are shown, never applied/);
  assert.match(text(section(w)), /whois\+cap needs Tailscale 1\.92 or newer/);
  assert.doesNotMatch(text(section(w)), /example\.com\/cap\/ccboard stands for/, 'a capability name that is set is not called a placeholder');
  assert.ok(!cmds.some((c) => /serve reset|\bup\b|\bdown\b|\bset\b/.test(c)));
});

test('the last refusal is named in words with its reason code, and the allowed tags are listed', async () => {
  const w = authWorld({ auth: answerOf('tagged') });
  await settle();
  assert.match(text(auth(w, 'tags')), /tag:ccboard, tag:build/);
  const w2 = authWorld({ auth: answerOf('whois-cap') });
  await settle();
  assert.match(text(auth(w2, 'last')), /a different device used the token \(device_mismatch\)/);
  assert.match(text(auth(w2, 'cap')), /example\.com\/cap\/ccboard/);
});

test('everything the board says is text: a hostile banner, reason and capability name make no element', async () => {
  const a = answerOf('whois-cap');
  a.banner = '<img src=x onerror=alert(1)> Serve is down';
  a.last_denial = { at: '<b>now</b>', reason: '<script>x</script>', node: 'n' };
  a.cap.name = '<i>evil</i>/cap/x';
  const w = authWorld({ auth: a });
  await settle();
  assert.equal(section(w).querySelectorAll('img, script, i').length, 0);
  assert.equal(auth(w, 'banner').children.length, 0, 'the banner holds one text node');
  assert.match(text(auth(w, 'banner')), /<img src=x onerror=alert\(1\)> Serve is down/);
});

test('a failed read says so, keeps the last answer, and the rest of the page still paints', async () => {
  let n = 0;
  const w = authWorld({ auth: () => { n += 1; if (n > 1) throw new Error('the board did not answer'); return answerOf('whois'); } });
  await settle();
  assert.equal(text(auth(w, 'mode')), 'whois');
  button(panel(w), 'Check').click();
  await settle();
  assert.match(text(section(w)), /Could not read the device check: the board did not answer\. What is shown is the last answer\./);
  assert.equal(text(auth(w, 'mode')), 'whois');
  const w2 = authWorld({ auth: new Error('403 forbidden') });
  await settle();
  assert.match(text(section(w2)), /Could not read the device check: 403 forbidden/);
  assert.ok(panel(w2).querySelector('.nd-panel'), 'the other sections are still there');
  assert.ok(section(w2).querySelector('pre'), 'the grant text is still shown');
});

test('a pair row names the bound device (short StableID) and its tags; with the check on, an unbound pair says it must be paired again', async () => {
  const pairs = [pair({ peer_id: 'p-a', name: 'build-box', device_bound: true, device_id: 'nBOX...TRL1', device_tags: ['tag:ccboard'] }), pair({ peer_id: 'p-b', name: 'old-pair', device_bound: false, device_id: null, device_tags: [] })];
  const w = authWorld({ auth: answerOf('whois'), pairs });
  await settle();
  const rows = panel(w).querySelectorAll('.nd-row[data-kind=in]');
  const chips = (name) => rows.find((r) => text(r.querySelector('.nd-name')) === name).querySelectorAll('.nd-chips .badge').map(text);
  assert.ok(chips('build-box').some((c) => /✓ device nBOX\.\.\.TRL1$/.test(c)), 'the short id, not the whole one');
  assert.ok(chips('build-box').includes('tag:ccboard'));
  assert.ok(chips('old-pair').some((c) => /not bound, pair again/.test(c)));
  assert.ok(!chips('old-pair').some((c) => /device n/.test(c)));
});

test('with the check off (token) a bound device is named as not checked, and an unbound pair gets no warning', async () => {
  const pairs = [pair({ peer_id: 'p-a', name: 'build-box', device_bound: true, device_id: 'nBOX...TRL1', device_tags: [] }), pair({ peer_id: 'p-b', name: 'old-pair', device_bound: false })];
  const w = authWorld({ auth: answerOf('token'), pairs });
  await settle();
  const rows = panel(w).querySelectorAll('.nd-row[data-kind=in]');
  const chips = (name) => rows.find((r) => text(r.querySelector('.nd-name')) === name).querySelectorAll('.nd-chips .badge').map(text);
  assert.ok(chips('build-box').some((c) => /device nBOX\.\.\.TRL1, not checked/.test(c)));
  assert.ok(!chips('old-pair').some((c) => /not bound/.test(c)));
});

test('a board that cannot run the mode (bind) does not call a bound pair checked', async () => {
  const pairs = [pair({ peer_id: 'p-a', name: 'build-box', device_bound: true, device_id: 'nBOX...TRL1', device_tags: [] })];
  const w = authWorld({ auth: answerOf('bind'), pairs });
  await settle();
  const rows = panel(w).querySelectorAll('.nd-row[data-kind=in]');
  assert.ok(rows[0].querySelectorAll('.nd-chips .badge').map(text).some((c) => /not checked/.test(c)));
});

test('NodeView.authView: the mode words, and nothing healthy that was not read', () => {
  const w = makeWorld({});
  w.load('core.js');
  w.load('nodes.js');
  w.load('nodes-pair.js');
  const v = plain(w.run('NodeView.authView(null)'));
  assert.equal(v.known, false);
  assert.equal(v.state.word, 'cannot run');
  assert.equal(v.deviceOn, false);
  const rows = Object.fromEntries(v.rows.map((r) => [r.key, r]));
  assert.equal(rows.bind.tone, 'warn', 'an absent bind reading is unknown, not fine');
  const t = plain(w.run('NodeView.authView({ mode: "token", ready: true, host: { checked: false } })'));
  assert.equal(t.state.word, 'default');
  assert.equal(t.deviceOn, false);
  assert.ok(!t.rows.some((r) => r.key === 'version'));
  assert.equal(plain(w.run('NodeView.deviceChips(NodeView.peer({ peer_id: "a" }), false)')).length, 0);
});

test('demo mode: ?authstate= picks the state, Check on token answers as if Tailscale was read, an unknown name falls back to token, and the texts follow the state', async () => {
  const fetchOf = () => async () => ({ ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(JSON.stringify(FX)) });
  const ask = async (search, p) => {
    const w = makeWorld({ fetch: fetchOf() });
    w.location.search = search;
    w.load('core.js');
    w.load('nodes.js');
    w.load('nodes-pair.js');                                               // the demo's answer is played there (the settings bundle), not in the first-paint core.js
    return plain(await w.run(`demoApi('GET', ${JSON.stringify(p)})`));
  };
  for (const name of STATES.filter((n) => n !== 'token-checked')) assert.deepEqual(await ask(`?demo=1&authstate=${name}`, '/api/nodes/auth'), answerOf(name), name);
  assert.deepEqual(await ask('?demo=1', '/api/nodes/auth'), answerOf('token'));
  assert.deepEqual(await ask('?demo=1&authstate=nope', '/api/nodes/auth'), answerOf('token'));
  assert.deepEqual(await ask('?demo=1', '/api/nodes/auth?check=1'), answerOf('token-checked'));
  assert.deepEqual(await ask('?demo=1&authstate=whois', '/api/nodes/auth?check=1'), answerOf('whois'));
});

test('demo mode: the pairs carry a bound device with a short id and no whole StableID', () => {
  const d = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'nodes.json'), 'utf8'));
  const bound = d.pairs.filter((p) => p.device_bound === true);
  assert.ok(bound.length >= 2 && bound.some((p) => p.device_tags.length) && bound.some((p) => !p.device_tags.length));
  assert.ok(bound.every((p) => /^.{4}\.\.\..{4}$/.test(p.device_id)));
});

test('no timer, no innerHTML, no inline style in the Device check code', () => {
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'settings.js'), 'utf8');
  const start = src.indexOf('const SETTINGS_AUTH_SWITCH');
  const end = src.indexOf('function settingsNdUi()');
  assert.ok(start > 0 && end > start);
  const block = src.slice(start, end).replace(/\/\*[\s\S]*?\*\//g, '');
  assert.doesNotMatch(block, /setInterval|setTimeout|requestAnimationFrame|innerHTML|insertAdjacentHTML|cssText|\.style\b|'style'/);
  assert.equal((block.match(/api\('GET'/g) || []).length, 1, 'one place asks the board');
});

test('the last refusal says how long ago, like every other time on the board, never the raw ISO string', () => {
  const w = authWorld();
  const iso = new Date(Date.now() - 180000).toISOString();
  const when = w.run(`NodeView.authWhen(${JSON.stringify(iso)})`);
  assert.doesNotMatch(when, /T\d\d:\d\d/, 'not the raw timestamp');
  assert.match(when, /ago|min|m\b/);
});

test('a widened proxy trust is said in words, not as a reason word', () => {
  const w = authWorld();
  assert.match(w.run('NodeView.AUTH_REASONS.proxy_trust'), /forwarded addresses from beyond loopback/);
});
