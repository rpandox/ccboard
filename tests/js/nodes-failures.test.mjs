// Contract tests for issue #153 in the browser (failure modes of the fleet): the status chip and the plain message of each state on the node page, in Settings, in the disabled
// reasons and in the refusals; Re-pair, Remove and Update address; the skew chip; the outcome-unknown link (never a retry); the GitHub trouble line of the epic page; and the demo
// variants (?demo=1&nodestate=<name>) that show each state without a server. Real scripts on minidom's DOM (tests/js/hubworld.mjs); nothing opens a connection.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { boardState, hubWorld, iso, page, plain, poll, rec, setState, text, tick } from './hubworld.mjs';

const all = (n, sel) => (n.document || n).querySelectorAll(sel);
const go = async (w, hash) => { w.location.hash = hash; await tick(); await tick(); };
const err = (status, body) => Object.assign(new Error((body && body.error) || String(status)), { status, body });
const WRITE = ['tasks', 'sessions'];
const ready = async (nodes, over = {}) => { const h = hubWorld({ state: boardState(), nodes, ...over }); await poll(h.w); return h; };

// the states of issue #153, as the hub's record carries them
const STATES = {
  unauthorized: { status: 'unauthorized', error_kind: null },
  identity: { status: 'unpaired', error_kind: 'identity_changed' },
  unpaired: { status: 'unpaired', error_kind: null },
  repair: { status: 'online', needs_repair: true },
  incompatible: { status: 'incompatible', error_kind: 'api_version', peer_api: 2, api: 1 },
  offline: { status: 'offline', age_s: 4000, last_ok_at: iso(4000), error_kind: 'timeout' },
  stale: { status: 'stale', age_s: 100, error_kind: 'timeout' },
};
const recOf = (key) => rec('build-box', { scopes: ['read', 'tasks', 'sessions'], ...STATES[key] });

// ---------------------------------------------------------------- the chip and the words

test('every state is a glyph and words, never colour only: online, stale, offline, re-pair, another device, not paired any more, incompatible', async () => {
  const { w } = await ready([rec('build-box')]);
  const view = (r) => plain(w.run(`Nodes.view(${JSON.stringify(r)})`));
  const cases = [
    [{ ...recOf('unauthorized') }, '!', 're-pair', 'bad'],
    [{ ...recOf('identity') }, '?', 'another device', 'bad'],
    [{ ...recOf('unpaired') }, '?', 'not paired any more', 'bad'],
    [{ ...recOf('repair') }, '!', 're-pair', 'bad'],
    [{ ...recOf('incompatible') }, '!', 'incompatible, API 2', 'warn'],
    [{ ...recOf('stale') }, '◐', 'stale 1 min', 'warn'],
  ];
  for (const [r, glyph, word, tone] of cases) {
    const v = view(r);
    assert.deepEqual([v.glyph, v.word, v.tone], [glyph, word, tone], JSON.stringify([r.status, r.error_kind]));
  }
  assert.match(view(recOf('offline')).word, /^offline since \d\d:\d\d$/);
  assert.equal(view(recOf('repair')).status, 'online', 'the hub status is kept; only the chip asks for Re-pair');
  assert.equal(view({ ...recOf('incompatible'), peer_api: null }).word, 'incompatible, API unknown', 'unknown is not a number');
  assert.equal(view({ ...recOf('incompatible'), peer_api: '2<b>' }).word, 'incompatible, API unknown', 'peer text is never put in the chip');
});

test('Nodes.can: each state refuses with its own sentence, in the relay\'s order, and an offline node says how long it has been away', async () => {
  const { w } = await ready([rec('build-box')]);
  const can = (r) => plain(w.run(`Nodes.can(${JSON.stringify(r)}, 'tasks')`));
  assert.equal(can(recOf('unauthorized')).why, "build-box no longer takes this board's token: re-pair it in Settings, Nodes");
  assert.equal(can(recOf('repair')).why, "build-box no longer takes this board's token: re-pair it in Settings, Nodes");
  assert.equal(can(recOf('identity')).why, 'This address now belongs to another device, not build-box: remove it or pair again');
  assert.equal(can(recOf('unpaired')).why, 'build-box answers as another node: remove it and pair it again');
  assert.equal(can(recOf('incompatible')).why, 'build-box speaks node API 2 and this board speaks 1: update the older board', 'both versions are named');
  assert.match(can(recOf('offline')).why, /^build-box is offline, last seen 1 hour ago$/);
  for (const k of ['unauthorized', 'repair', 'identity', 'unpaired', 'incompatible', 'offline']) assert.equal(can(recOf(k)).ok, false, k);
  assert.equal(can({ ...recOf('stale') }).ok, true, 'a stale node is still called');
});

// ---------------------------------------------------------------- the node page

test('the node page says each state in plain words: removed the pairing, another device, needs a new pairing, incompatible with both versions, offline with the last seen age', async () => {
  const want = {
    unauthorized: /^build-box removed this pairing, pair again\./,
    identity: /^This address now belongs to another device, not build-box: remove it or pair again\./,
    repair: /^build-box needs to be paired again: this board could not confirm that it saved the new token/,
    incompatible: /^build-box speaks node API 2 and this board speaks 1: update the older board\. Its card is still shown; nothing is sent to it\./,
    offline: /^build-box is offline, last seen 1 hour ago\. Showing the last reading/,
  };
  for (const [key, re] of Object.entries(want)) {
    const { w } = await ready([recOf(key)]);
    await go(w, '#/n/build-box');
    const banner = page(w).querySelector(`.nd-banner[data-notice="${key === 'identity' ? 'identity_changed' : key === 'repair' ? 'needs_repair' : key}"]`);
    assert.ok(banner, key);
    assert.match(text(banner), re, key);
    assert.equal(banner.getAttribute('role'), 'status');
    assert.ok(page(w).querySelector('.nd-status .nd-sg').textContent.length > 0, `${key}: the glyph is there beside the words`);
  }
});

test('Re-pair is the page\'s one filled primary for a refused token, another device or a lost rotation; incompatible and offline keep Open board primary; another device also offers Remove', async () => {
  for (const key of ['unauthorized', 'identity', 'repair']) {
    const { w } = await ready([recOf(key)]);
    await go(w, '#/n/build-box');
    const acts = all(page(w), 'a.btn');
    const repair = acts.find((a) => text(a) === 'Re-pair');
    assert.ok(repair, key);
    assert.equal(repair.getAttribute('href'), '#/settings?sec=nodes');
    assert.equal(all(page(w), '.primary').length, 1, `${key}: one filled primary`);
    assert.ok(repair.classList.contains('primary'));
    assert.equal(!!acts.find((a) => text(a) === 'Remove in Settings'), key === 'identity', `${key}: Remove is offered where the device is not the one paired`);
  }
  for (const key of ['incompatible', 'offline']) {
    const { w } = await ready([recOf(key)]);
    await go(w, '#/n/build-box');
    assert.equal(all(page(w), 'a.btn').find((a) => text(a) === 'Re-pair'), undefined, key);
    assert.ok(all(page(w), 'a.btn').find((a) => text(a) === 'Open board').classList.contains('primary'), `${key}: Open board stays the primary`);
  }
});

test('every disabled action carries its reason: aria-disabled, described by a sentence printed once under the grid, and a toast on a tap', async () => {
  for (const key of ['unauthorized', 'identity', 'repair', 'incompatible', 'offline']) {
    const { w } = await ready([recOf(key)]);
    await go(w, '#/n/build-box');
    const offs = all(page(w), 'button.nd-off');
    assert.deepEqual(offs.map(text), ['New task here', 'New session here'], key);
    const why = text(page(w).querySelector('.nd-whys'));
    for (const b of offs) {
      assert.equal(b.getAttribute('aria-disabled'), 'true');
      assert.ok(all(page(w), '.nd-why').some((x) => x.getAttribute('id') === b.getAttribute('aria-describedby')), 'described by its own reason');
    }
    const reason = plain(w.run(`Nodes.can(${JSON.stringify(recOf(key))}, 'tasks')`)).why;
    assert.ok(why.includes(reason), `${key}: ${why}`);
    offs[0].click();
    assert.ok(plain(w.get('__toasts')).some((t) => t.text.includes(reason)), `${key}: the tap says why`);
  }
});

test('skew above 5 s: a warning chip with a glyph beside the status, and the sentence in a banner; an honest clock has neither', async () => {
  const { w } = await ready([rec('build-box', { skew_warn: true, skew_ms: -600000 }), rec('alice-mac')]);
  await go(w, '#/n/build-box');
  const chip = page(w).querySelector('.page-head .badge.warn');
  assert.equal(text(chip), '! clock off by 600 s');
  assert.ok(chip.getAttribute('title').includes('more than 5 s'));
  assert.match(text(page(w).querySelector('.nd-banner[data-notice=skew]')), /Its clock differs from this board's by 600 s/);
  await go(w, '#/n/alice-mac');
  assert.equal(page(w).querySelector('.page-head .badge'), null);
  assert.equal(page(w).querySelector('.nd-banner[data-notice=skew]'), null);
});

test('Settings line under a pair: the chip of the state, the skew chip with a glyph and a plain message for a refused token, another device, a lost rotation and another node API', async () => {
  const { w } = await ready([recOf('unauthorized'), rec('a', { ...STATES.identity }), rec('b', { ...STATES.repair }), rec('c', { ...STATES.incompatible }), rec('d', { skew_warn: true, skew_ms: 12000 })]);
  const line = (h) => w.run(`Nodes.settingsLine(${JSON.stringify(h)})`);
  assert.match(text(line('build-box')), /^!re-pair.*build-box removed this pairing, pair again\./);
  assert.match(text(line('a')), /^\?another device.*This address now belongs to another device, not a: remove it or pair again\./);
  assert.match(text(line('b')), /^!re-pairb needs to be paired again/);
  assert.match(text(line('c')), /^!incompatible, API 2.*c speaks node API 2 and this board speaks 1: update the older board\./);
  assert.equal(text(line('d').querySelector('.badge')), '! clock differs by 12 s');
  for (const h of ['build-box', 'a', 'b', 'c']) assert.ok(line(h).querySelector('[data-notice]'), h);
});

// ---------------------------------------------------------------- outcome unknown: a link, never a retry

test('Nodes.unknown: a timeout or a broken connection after a write went out is unknown; a refusal with a reason is not; a read is never unknown', async () => {
  const { w } = await ready([rec('build-box')]);
  const u = (e, what) => w.run(`Nodes.unknown(${JSON.stringify(e)}, ${JSON.stringify(what || 'prompt')})`);
  assert.equal(u({ status: 504, body: { reason: 'unconfirmed' } }), true);
  assert.equal(u({ status: 502, body: { reason: 'unconfirmed', outcome: 'unknown', check_url: '#/n/build-box' } }), true);
  assert.equal(u({ status: 502, body: {} }), true, 'a proxy answer with no reason: the write may have gone through');
  assert.equal(u({}), true, 'no answer at all');
  assert.equal(u({ status: 503, body: { reason: 'offline' } }), false);
  assert.equal(u({ status: 409, body: { reason: 'scope' } }), false);
  assert.equal(u({ status: 409, body: { reason: 'incompatible' } }), false);
  assert.equal(u({ status: 504, body: {} }, 'perms'), false, 'reading the permission list does not write anything');
  assert.equal(w.run("Nodes.checkLink('../x')"), null, 'a handle outside the grammar gives no link');
  const a = w.run("Nodes.checkLink('build-box')");
  assert.equal(a.getAttribute('href'), '#/n/build-box');
  assert.equal(text(a), 'See its tasks');
  assert.match(a.getAttribute('aria-label'), /^Check build-box: its tasks and sessions$/);
});

test('a prompt whose answer was lost says "Outcome unknown, check <node>" with the link to its page, sends once, keeps the draft and offers no Retry button', async () => {
  const { w } = await ready([rec('build-box', { scopes: ['read', 'tasks', 'sessions'] })]);
  const calls = [];
  w.ctx.__r = async (method, p, b) => { calls.push([method, p]); throw err(504, { error: 'build-box did not answer in 8 s', reason: 'unconfirmed', outcome: 'unknown', check_url: '#/n/build-box' }); };
  w.run('api = (m, p, b) => __r(m, p, b)');
  await go(w, '#/n/build-box/s/shop--api--s1');
  const ta = page(w).querySelector('.nd-send textarea');
  ta.value = 'keep me';
  ta.dispatchEvent({ type: 'input' });
  ta.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() {} });
  await tick(); await tick(); await tick();
  const st = page(w).querySelector('.nd-steer-st');
  assert.match(text(st), /^Outcome unknown, check build-box: it may or may not have got this\. It was not retried\. See its tasks$/);
  assert.equal(st.querySelector('a').getAttribute('href'), '#/n/build-box');
  assert.equal(calls.filter(([m]) => m === 'POST').length, 1, 'one call, no second attempt');
  assert.equal(ta.value, 'keep me');
  assert.equal(all(page(w), 'button').find((b) => /retry|try again/i.test(text(b))), undefined, 'no Retry or Try again control');
});

test('a permission answer whose outcome is unknown keeps the card with the sentence and the link to the node', async () => {
  const build = rec('build-box', { scopes: ['read', 'tasks', 'sessions', 'permissions'] });
  build.state.needs_you.permissions = 1;
  const { w } = hubWorld({ state: boardState({ projects: [] }), nodes: [build] });
  w.ctx.__r = async (method, p) => {
    if (method === 'GET') return { node: 'build-box', age: 0, data: { permissions: [{ id: 41, tmux: 'shop--api--s1', tool: 'Bash', summary: 'git push', since: iso(30) }] } };
    throw err(504, { error: 'x', reason: 'unconfirmed' });
  };
  w.run('api = (m, p, b) => __r(m, p, b)');
  await poll(w);
  await tick();
  await go(w, '#/inbox');
  await tick(); await tick();
  const card = all(page(w), '.nd-pcard').find((c) => c.getAttribute('data-key') === 'build-box/41');
  assert.ok(card);
  all(card, 'button').find((b) => text(b) === 'Allow').click();
  await tick(); await tick(); await tick();
  const c2 = all(page(w), '.nd-pcard').find((c) => c.getAttribute('data-key') === 'build-box/41');
  const msg = c2.querySelector('.nd-perr');
  assert.match(text(msg), /^Outcome unknown, check build-box/);
  assert.equal(msg.querySelector('a').getAttribute('href'), '#/n/build-box');
});

// ---------------------------------------------------------------- the demo variants

const read = (f) => JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', f), 'utf8'));

test('the demo has a variant for each state of issue #153 and each patch names a node the demo has', () => {
  const d = read('nodes.json');
  const v = d.variants;
  for (const name of ['unauthorized', 'identity', 'repair', 'incompatible', 'skew', 'offline', 'unknown']) assert.ok(name in v, name);
  const handles = new Set(d.hub.map((r) => r.handle));
  const ids = new Set(d.nodes.map((r) => r.peer_id));
  for (const [name, x] of Object.entries(v)) {
    if (name === '_note') continue;
    for (const h of Object.keys(x.hub || {})) assert.ok(handles.has(h), `${name}: ${h}`);
    for (const p of Object.keys(x.nodes || {})) assert.ok(ids.has(p), `${name}: ${p}`);
  }
  assert.ok(read('nodes-discover.json').variants.moved.moved[0].to_url.startsWith('https://'));
  assert.ok(JSON.stringify(d.variants).indexOf('@') < 0, 'no address in a fixture');
});

for (const [flag, want] of [
  ['unauthorized', { status: 'unauthorized', word: 're-pair' }],
  ['identity', { status: 'unpaired', word: 'another device' }],
  ['repair', { status: 'online', word: 're-pair' }],
  ['incompatible', { status: 'incompatible', word: 'incompatible, API 2' }],
  ['skew', { status: 'online', word: 'online' }],
  ['offline', { status: 'offline' }],
]) {
  test(`demo ?demo=1&nodestate=${flag} lays the state over build-box and reads through core.js alone`, async () => {
    const { w } = hubWorld({ search: `?demo=1&nodestate=${flag}`, realApi: true });
    setState(w, plain(await w.run("api('GET', '/api/state')")));
    await poll(w);
    const r = plain(w.run("Nodes.get('build-box')"));
    assert.equal(r.status, want.status);
    if (want.word) assert.equal(w.run("Nodes.view(Nodes.get('build-box')).word"), want.word);
    if (flag === 'skew') assert.equal(r.skew_warn, true);
    await go(w, '#/n/build-box');
    assert.ok(page(w).querySelector('h1'), 'the page renders');
    const urls = plain(w.get('__fetches')).map((f) => f.url);
    assert.ok(urls.every((u) => u.startsWith('/static/demo/')), `only fixtures: ${urls}`);
    const regs = plain(await w.run("api('GET', '/api/nodes')")).nodes;
    const row = regs.find((x) => x.handle === 'build-box');
    assert.equal(row.needs_repair === true, ['unauthorized', 'repair'].includes(flag), 'the registry row agrees where it should');
  });
}

test('demo nodestate=moved: the discovery answer carries the offer, without the variants table; without the flag it carries none', async () => {
  const on = hubWorld({ search: '?demo=1&nodestate=moved', realApi: true });
  const a = plain(await on.w.run("api('GET', '/api/nodes/discover')"));
  assert.equal(a.moved.length, 1);
  assert.equal(a.moved[0].peer_id, 'p-build-box');
  assert.ok(!('variants' in a));
  const off = hubWorld({ search: '?demo=1', realApi: true });
  const b = plain(await off.w.run("api('GET', '/api/nodes/discover')"));
  assert.ok(!('moved' in b) && !('variants' in b));
});

test('demo nodestate=unknown: a task or a session started on a node ends in outcome unknown with the link, once, and a prompt too', async () => {
  const { w } = hubWorld({ search: '?demo=1&nodestate=unknown', realApi: true });
  setState(w, plain(await w.run("api('GET', '/api/state')")));
  await poll(w);
  let e = null;
  try { await w.run("api('POST', '/api/nodes/build-box/tasks', { project: 'shop', repo: 'api', title: 'x' })"); } catch (x) { e = x; }
  assert.ok(e, 'refused');
  const b = plain(e.body);
  assert.deepEqual([e.status, b.reason, b.outcome, b.check_url], [504, 'unconfirmed', 'unknown', '#/n/build-box']);
});

// ---------------------------------------------------------------- the epic page: GitHub trouble

test('the epic page says what is wrong with GitHub and when the run tries again; held issues carry the board\'s own sentence', async () => {
  const epic = read('epic.json');
  const v = epic.variants.rungithub;
  assert.equal(v.detail.route.error_code, 'github_unavailable');
  assert.ok(v.detail.route.retry_at);
  assert.match(v.child['206'].route.text, /trying again around \d\d:\d\d$/);
  const src = fs.readFileSync(path.join(STATIC, 'pages', 'epic.js'), 'utf8');
  assert.match(src, /r\.error_code === 'github_unavailable'/);
  assert.match(src, /The run tries again/);
});

// ---------------------------------------------------------------- rules of the files

test('the files of this change hold the front-end rules: no innerHTML, no inline style, no cssText, no em-dash in the words', () => {
  for (const f of ['nodes-hub.js', 'nodes-pair.js', 'pages/node.js', 'pages/settings.js', 'pages/epic.js', 'launcher-node.js', 'nodes.js']) {
    const src = fs.readFileSync(path.join(STATIC, f), 'utf8');
    assert.doesNotMatch(src, /\.innerHTML\s*=|insertAdjacentHTML\(|cssText|setAttribute\('style'/, f);
  }
  for (const f of ['nodes-hub.js', 'nodes-pair.js', 'pages/node.js']) {
    const src = fs.readFileSync(path.join(STATIC, f), 'utf8');
    const strings = src.match(/`[^`]*`|'[^'\n]*'/g) || [];
    assert.ok(!strings.some((s) => /(outcome unknown|re-pair|another device|incompatible|pair again|clock off)/i.test(s) && /—/.test(s)), `${f}: no em-dash in UI text`);
  }
});

// ---------------------------------------------------------------- the first-paint budget

test('the first-paint scripts of index.html stay under the budget and none of this change is in them but the demo helper in core.js', () => {
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  let total = 0;
  for (const m of html.matchAll(/<script[^>]*src="\/static\/([^"?]+)/g)) total += fs.statSync(path.join(STATIC, m[1])).size;
  assert.ok(total <= 560000, `first paint is ${total} bytes`);
  for (const f of ['nodes-hub.js', 'nodes-pair.js', 'pages/node.js']) assert.ok(!html.includes(f), `${f} is lazy`);
});
