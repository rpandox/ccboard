// #313 (#30 rule 2, second pass): Allow / Deny, Ack and Install now paint at once and the next poll only confirms. The whole board, real scripts in index.html's order on minidom's DOM with the
// shell installed, a real poll() and a fake fetch: the request of each action is held on a promise the test controls, so the DOM is read BEFORE it resolves, through a poll that still sees the old
// state, after the board answered, and after a refusal. The fake board applies an answer to its own state only when the test releases it.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { fixtureState } from './world.mjs';

const SCRIPTS = ['core.js', 'components.js', 'live.js', 'router.js', 'pages/home.js', 'pages/inbox.js', 'pages/widgets.js', 'pages/tasks.js', 'pages/agents.js', 'pages/search.js', 'pages/session.js',
  'pages/placeholders.js', 'shell.js'];
const CK = 'phasezero--website--t-checkout-redesign';     // waits on permission 12
const P1 = 'petroit--api--s1';                            // done, not acknowledged
const settle = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');
const clone = (v) => JSON.parse(JSON.stringify(v));

function sessionOf(st, tmux) {
  for (const p of st.projects) for (const r of [p.root, ...p.repos]) for (const s of (r && r.sessions) || []) if (s.tmux === tmux) return s;
  return null;
}

/** The board: `server.state` is what GET /api/state answers; a POST under a `held` prefix waits for release() / refuse(), and release() applies `apply(path, state)` first. */
async function boardWorld({ hash = '#/inbox', deploy = null } = {}) {
  const server = { state: fixtureState(), posts: [], held: [], queue: [], apply: () => {} };
  if (deploy) server.state.deploy = deploy;
  const reply = (body, ok = true, status = 200) => ({ ok, status, statusText: ok ? 'OK' : 'Conflict', json: async () => body });
  const w = makeWorld({
    setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
    fetch: async (url, init = {}) => {
      if (!init.method || init.method === 'GET') return reply(url === '/api/state' ? clone(server.state) : {});
      server.posts.push(url);
      if (!server.held.some((p) => url.includes(p))) return reply({ ok: true });
      return new Promise((res) => { server.queue.push({ url, res }); });
    },
  });
  installDom(w);
  for (const f of SCRIPTS) w.load(f);
  w.ctx.__toasts = [];
  w.run(`Live.subscribe = () => () => {}; toast = (text, o) => { __toasts.push({ text, kind: o && o.kind }); };`);
  w.run('installShell()');
  w.location.hash = hash;
  const poll = async () => { await w.run('poll(true)'); await settle(); };
  await poll();
  return {
    w, server, poll,
    hold: (prefix) => server.held.push(prefix),
    release: async () => { const q = server.queue.shift(); server.apply(q.url, server.state); q.res(reply({ ok: true })); await settle(); await settle(); },
    refuse: async (message) => { const q = server.queue.shift(); q.res(reply({ error: message }, false, 409)); await settle(); await settle(); },
    toasts: () => plain(w.get('__toasts')),
    q: (sel) => w.document.querySelector(sel),
    card: (tmux) => w.document.querySelectorAll('.inbox-card').find((n) => n.getAttribute('data-tmux') === tmux) || null,
  };
}

// ---------------------------------------------------------------- Allow / Deny

const permButtons = (b, tmux = CK) => {
  const card = b.card(tmux);
  return card ? card.querySelectorAll('.ib-actions button').filter((x) => /^(Allow|Deny|allowing…|denying…)$/.test(text(x))) : [];
};
const words = (bs) => bs.map(text);
const off = (x) => !!x.disabled || x.hasAttribute('disabled');
const answerPermission = (path, st) => {
  st.pending_permissions = st.pending_permissions.filter((p) => !path.startsWith(`/api/permission/${p.id}/`));
  Object.assign(sessionOf(st, CK), { state: 'working', needs_attention: false });
};

test('Allow: both buttons are off and the pressed one reads "allowing…" before the board answers; a poll in between does not bring them back; the answer and the poll that agrees clear it', async () => {
  const b = await boardWorld();
  b.hold('/api/permission/');
  b.server.apply = answerPermission;
  assert.deepEqual(words(permButtons(b)), ['Allow', 'Deny']);
  permButtons(b)[0].click();
  assert.equal(b.server.queue.length, 1, 'the POST is in flight');
  assert.deepEqual(b.server.posts, ['/api/permission/12/allow']);
  assert.deepEqual(words(permButtons(b)), ['allowing…', 'Deny'], 'painted at the click');
  assert.ok(permButtons(b).every(off), 'and both off');
  b.server.state.projects[0].repos[0].sessions[0].last_message = 'something new';       // the poll brings a change, so the page is repainted
  await b.poll();
  assert.deepEqual(words(permButtons(b)), ['allowing…', 'Deny'], 'a poll that still lists the permission keeps the state');
  assert.ok(permButtons(b).every(off));
  await b.poll();                                                                         // and one that changes nothing
  assert.deepEqual(words(permButtons(b)), ['allowing…', 'Deny']);
  await b.release();
  assert.deepEqual(words(permButtons(b)), [], 'the permission is gone from the card once the board has been polled');
  assert.equal(b.w.run('actPending.size'), 0, 'and the name is forgotten');
  assert.deepEqual(b.toasts(), []);
});

test('Deny reads "denying…" the same way, and the answered request holds the buttons off until a poll no longer lists it', async () => {
  const b = await boardWorld();
  b.hold('/api/permission/');
  permButtons(b)[1].click();
  assert.deepEqual(words(permButtons(b)), ['Allow', 'denying…']);
  await b.release();
  assert.deepEqual(words(permButtons(b)), ['Allow', 'denying…'], 'answered, but the poll still lists it (this board did not apply the answer): still off');
  assert.equal(b.w.run('actPending.size'), 1);
  b.w.run(`actPending.set('perm:12', { word: 'deny', until: Date.now() - 1 })`);          // 15 s went by
  await b.poll();
  assert.deepEqual(words(permButtons(b)), ['Allow', 'Deny'], 'the safety expiry puts the buttons back');
  assert.ok(permButtons(b).every((x) => !off(x)));
  assert.equal(b.w.run('actPending.size'), 0);
});

test('a refused Allow puts both buttons back and says why in the board\'s words', async () => {
  const b = await boardWorld();
  b.hold('/api/permission/');
  permButtons(b)[0].click();
  assert.deepEqual(words(permButtons(b)), ['allowing…', 'Deny']);
  await b.refuse('already answered in the terminal');
  assert.deepEqual(words(permButtons(b)), ['Allow', 'Deny']);
  assert.ok(permButtons(b).every((x) => !off(x)));
  assert.deepEqual(b.toasts(), [{ text: 'Allow was not sent: already answered in the terminal', kind: 'bad' }]);
  assert.equal(b.w.run('actPending.size'), 0);
});

test('the Agents row and the peek card show the same pending state', async () => {
  const b = await boardWorld({ hash: '#/agents' });
  await new Promise((r) => setTimeout(r, 20));
  b.hold('/api/permission/');
  const row = () => b.w.document.querySelector(`.rrow[data-tmux=${CK}]`);
  const rowButtons = () => row().querySelectorAll('.rr-actions button').filter((x) => /^(Allow|Deny|allowing…|denying…)$/.test(text(x)));
  assert.deepEqual(words(rowButtons()), ['Allow', 'Deny']);
  rowButtons()[1].click();
  assert.deepEqual(words(rowButtons()), ['Allow', 'denying…'], 'the row');
  assert.ok(rowButtons().every(off));
  b.w.location.hash = `#/s/${CK}`;
  const peek = () => b.w.document.querySelector('.peek-card') || b.w.document.querySelector('.peek');
  const peekButtons = () => peek().querySelectorAll('.peek-perm button');
  assert.deepEqual(words(peekButtons()), ['Allow', 'denying…'], 'the peek card');
  await b.refuse('gone');
  assert.deepEqual(words(peekButtons()), ['Allow', 'Deny']);
});

// ---------------------------------------------------------------- Ack

const counts = (b) => ({
  chip: text(b.q('#inbox-pill .pv')),
  side: text(b.q('#sidebar [data-cnt=inbox]')),
  summary: text(b.q('.inbox-page .summary')),
  title: b.w.run('inboxTitle()'),
});
const ackButton = (b, tmux = P1) => { const c = b.card(tmux); return c ? c.querySelector('.slot-ack button') : null; };
const ackApply = (path, st) => { sessionOf(st, P1).needs_attention = false; };

test('Ack: the card leaves Needs you and the header chip, the sidebar badge and the page counts drop at the click; a poll that still lists it does not bring it back; the answer and the poll that agrees clear it', async () => {
  const b = await boardWorld();
  b.hold('/ack');
  b.server.apply = ackApply;
  const before = counts(b);
  const n = Number(before.chip);
  assert.ok(n >= 3 && b.card(P1), 'a board with several things waiting');
  assert.equal(before.side, String(n));
  assert.equal(before.title, `(${n}) Needs you`);
  ackButton(b).click();
  assert.equal(b.server.queue.length, 1, 'the POST is in flight');
  assert.deepEqual(b.server.posts, [`/api/sessions/${P1}/ack`]);
  assert.equal(b.card(P1), null, 'the card is gone at the click');
  const after = counts(b);
  assert.equal(after.chip, String(n - 1), 'the header chip');
  assert.equal(after.side, String(n - 1), 'the sidebar badge');
  assert.equal(after.title, `(${n - 1}) Needs you`, 'the page title');
  assert.match(after.summary, new RegExp(`^.* ${n - 1} need`), 'the heading counts');
  b.server.state.projects[0].repos[0].sessions[0].last_message = 'something new';        // a poll that changes things, with the session still listed as needing attention
  await b.poll();
  assert.equal(b.card(P1), null, 'the poll does not resurrect it');
  assert.equal(counts(b).chip, String(n - 1));
  await b.release();
  assert.equal(b.card(P1), null);
  assert.equal(b.w.run('actPending.size'), 0, 'the poll after the answer agrees, so the name is forgotten');
  assert.equal(counts(b).chip, String(n - 1));
  assert.deepEqual(b.toasts(), []);
});

test('a refused Ack brings the card and the counts back with the board\'s words', async () => {
  const b = await boardWorld();
  b.hold('/ack');
  const n = Number(counts(b).chip);
  ackButton(b).click();
  assert.equal(b.card(P1), null);
  await b.refuse('no such session');
  assert.ok(b.card(P1), 'the card is back');
  assert.equal(counts(b).chip, String(n));
  assert.equal(counts(b).side, String(n));
  assert.ok(ackButton(b), 'with its Ack button');
  assert.deepEqual(b.toasts(), [{ text: 's1 was not acknowledged: no such session', kind: 'bad' }]);
  assert.equal(b.w.run('actPending.size'), 0);
});

test('an Ack the board never shows as done comes back after the safety expiry', async () => {
  const b = await boardWorld();
  b.hold('/ack');
  ackButton(b).click();
  await b.release();                                                                     // answered, but this board keeps listing the session
  assert.equal(b.card(P1), null, 'hidden while the poll still lists it');
  b.w.run(`actPending.set('ack:${P1}', { word: '', until: Date.now() - 1 })`);
  await b.poll();
  assert.ok(b.card(P1), 'the survivor is back');
});

test('Ack on Home\'s Needs you section and from the Agents peek goes through the same path', async () => {
  const b = await boardWorld({ hash: '#/' });
  b.hold('/ack');
  const homeCard = () => b.w.document.querySelectorAll('.inbox-card').find((n) => n.getAttribute('data-tmux') === P1);
  assert.ok(homeCard());
  homeCard().querySelector('.slot-ack button').click();
  assert.equal(homeCard(), undefined, 'the Home card leaves at the click');
  await b.refuse('busy');
  assert.ok(homeCard());
  b.w.location.hash = `#/s/${P1}`;
  const peekAck = () => b.w.document.querySelector('.peek .slot-ack button');
  assert.ok(peekAck());
  peekAck().click();
  assert.equal(peekAck(), null, 'the peek\'s Ack goes at the click');
  await b.refuse('busy');
  assert.ok(peekAck());
});

// ---------------------------------------------------------------- Install now

const HOLD = { pending: true, hold: true, forced: false, reasons: ['1 terminal open'], minutes_left: 20 };
const installButton = (b) => b.w.document.getElementById('banner').querySelectorAll('button').find((x) => /^(Install now|installing at the next check)$/.test(text(x)));

test('Install now: the button reads "installing at the next check" and is off at the click; a poll in flight does not flip it back; the board\'s own answer shows the same words', async () => {
  const b = await boardWorld({ hash: '#/', deploy: { ...HOLD } });
  b.hold('/api/deploy/now');
  b.server.apply = (path, st) => { st.deploy.forced = true; };
  assert.equal(text(installButton(b)), 'Install now');
  installButton(b).click();
  assert.equal(b.server.queue.length, 1, 'the POST is in flight');
  assert.equal(text(installButton(b)), 'installing at the next check');
  assert.ok(off(installButton(b)), 'and off');
  b.server.state.deploy.minutes_left = 19;                                              // a poll that changes the banner, with forced still false
  await b.poll();
  assert.equal(text(installButton(b)), 'installing at the next check', 'the poll does not flip it back');
  assert.ok(off(installButton(b)));
  await b.release();
  assert.equal(text(installButton(b)), 'installing at the next check', 'the board says the same');
  assert.ok(off(installButton(b)), 'a confirmed install is off too');
  assert.deepEqual(b.toasts(), []);
});

test('a refused Install now restores the button and says why', async () => {
  const b = await boardWorld({ hash: '#/', deploy: { ...HOLD } });
  b.hold('/api/deploy/now');
  installButton(b).click();
  assert.equal(text(installButton(b)), 'installing at the next check');
  await b.refuse('the update is already being installed');
  assert.equal(text(installButton(b)), 'Install now');
  assert.equal(off(installButton(b)), false);
  assert.deepEqual(b.toasts(), [{ text: 'The update was not started: the update is already being installed', kind: 'bad' }]);
});
