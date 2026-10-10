// #71: a session parked on a rate limit (session.parked {kind, resets_at}) says "limit reached, continues at <time>" (or "... auto-continue is off")
// on the task card and the Inbox card instead of a bare "error" chip: amber, glyph plus words. Real scripts on minidom through tests/js/world.mjs.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeState, fixtureState, homeWorld, text } from './world.mjs';

const SOON = Math.floor(Date.now() / 1000) + 1800;          // half an hour ahead: the clock shows HH:MM, no weekday
const hhmm = (epoch) => { const d = new Date(epoch * 1000); return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`; };
const MSG = "You've hit your session limit · resets 10:05pm (Asia/Kathmandu)";

const sessionOf = (over = {}) => ({ tmux: 'shop--api--s1', name: 's1', project: 'shop', repo: 'api', agent: 'claude', state: 'errored', state_at: new Date().toISOString(),
  last_message: MSG, needs_attention: true, flags: {}, parked: { kind: '5h', resets_at: SOON }, ...over });

function world(st = fakeState()) {
  const { w } = homeWorld({ state: st });
  return w;
}
const run = (w, js, vars = {}) => { Object.assign(w.ctx, vars); return w.run(js); };

test('the text: continues at the reset, or auto-continue is off (opted out, board off, no reset known), or the reset has passed', () => {
  const w = world();
  const t = (s) => { w.ctx.__s = s; return w.run('limitParkedText(__s)'); };
  assert.equal(t(sessionOf()), `limit reached, continues at ${hhmm(SOON)}`);
  assert.equal(t(sessionOf({ flags: { no_autoresume: true } })), 'limit reached, auto-continue is off');
  assert.equal(t(sessionOf({ parked: { kind: 'other', resets_at: 0 } })), 'limit reached, auto-continue is off');
  assert.equal(t(sessionOf({ parked: { kind: '5h', resets_at: Math.floor(Date.now() / 1000) - 5 } })), 'limit reset, continuing shortly');
  assert.equal(t(sessionOf({ parked: undefined })), '');
  assert.equal(t(null), '');
  w.ctx.__st = fakeState({ config: { ...fakeState().config, auto_continue: false } });
  w.run('state = __st');
  assert.equal(t(sessionOf()), 'limit reached, auto-continue is off', 'CCBOARD_AUTO_CONTINUE=0 for the whole board');
});

test('the state chip of a parked session is amber (class waiting), has the glyph and the words, and no longer says "error"', () => {
  const w = world();
  w.ctx.__s = sessionOf();
  const chip = w.run('stateBadge(__s)');
  assert.ok(text(chip).trim().endsWith(` limit reached, continues at ${hhmm(SOON)}`), text(chip));      // a glyph, then the words
  assert.ok(chip.classList.contains('state') && chip.classList.contains('waiting') && !chip.classList.contains('errored'));
  assert.ok(!/error/.test(text(chip)));
  assert.ok(chip.querySelector('[aria-hidden="true"]'), 'the glyph is decoration beside the words');
  w.ctx.__s = sessionOf({ parked: undefined });
  const plain = w.run('stateBadge(__s)');
  assert.ok(plain.classList.contains('errored') && /error/.test(text(plain)), 'a failed session that is not parked still says error');
});

test('the task card: the badge and the owner chip say the limit, not error', () => {
  const w = world();
  const s = sessionOf();
  w.ctx.__t = { id: 7, project: 'shop', repo: 'api', slug: 'fix', title: 'Fix it', branch: 'task/fix', base: 'main', worktree: '/w', tmux: s.tmux, agent: 'claude', mode: 'worktree',
    phase: 'running', column: 'in_progress', session: s, overlap: [], created_at: new Date().toISOString(), cost_usd: null, pr_url: null };
  const card = w.run('taskCard(__t, {})');
  const badge = card.querySelector('.tk-title-row .state');
  assert.ok(badge.classList.contains('waiting'));
  assert.equal(text(badge).includes(`limit reached, continues at ${hhmm(SOON)}`), true);
  assert.ok(!/\berror\b/.test(text(card.querySelector('.tk-title-row'))));
  assert.match(text(card.querySelector('.tk-owner-state')), /^limit reached/);
  // opted out: the same chip says so
  w.ctx.__t = { ...w.ctx.__t, session: sessionOf({ flags: { no_autoresume: true } }) };
  assert.ok(text(w.run('taskCard(__t, {})').querySelector('.tk-title-row .state')).includes('limit reached, auto-continue is off'));
});

test('the Inbox card of a parked session leads with the limit text and the plain sentence, and repaints when the opt-out flips', () => {
  const st = fixtureState();
  const w = world(st);
  const s = sessionOf();
  const api = st.projects.find((p) => p.name === 'petroit').repos.find((r) => r.name === 'api');
  api.sessions.push({ ...s, project: 'petroit', repo: 'api', tmux: 'petroit--api--s1x', name: 's1x', created: 1, attached: 0 });
  w.ctx.__stx = st;
  w.run('state = __stx');
  const item = w.run('Inbox.items(__stx).find((x) => x.tmux === "petroit--api--s1x")');
  assert.equal(item.kind, 'limit');
  w.ctx.__item = item;
  const card = w.run('inboxCard(__item, __stx, {})');
  assert.equal(text(card.querySelector('.ib-note')), `limit reached, continues at ${hhmm(SOON)}`);
  assert.equal(text(card.querySelector('.ib-kind')), 'limit hit');
  w.ctx.__off = { ...item, flags: { no_autoresume: true } };
  w.run('__card = inboxCard(__off, __stx, {})');
  assert.equal(text(w.get('__card').querySelector('.ib-note')), 'limit reached, auto-continue is off');
  // the signature carries the text, so a poll that flips the switch repaints the same card
  assert.notEqual(w.run('Inbox.sig(__item, __stx, "limit")'), w.run('Inbox.sig(__off, __stx, "limit")'));
});

test('Home rich session row: the state text under the name says the limit, repaints when the opt-out flips, and says error when not parked', () => {
  const w = world(fakeState());
  const row = (s) => { w.ctx.__s = { ...s, project: 'shop', repo: 'api' }; w.run('globalThis.__row = sessionCard(__s, { rich: true, showProject: false })'); return w.get('__row'); };
  const r = row(sessionOf());
  assert.equal(text(r.querySelector('.rr-meta')), `limit reached, continues at ${hhmm(SOON)}`);
  w.ctx.__s2 = { ...sessionOf({ flags: { no_autoresume: true } }), project: 'shop', repo: 'api' };
  w.run('__row.ccPatch(__s2)');
  assert.equal(text(r.querySelector('.rr-meta')), 'limit reached, auto-continue is off');
  w.ctx.__s3 = { ...sessionOf({ parked: undefined }), project: 'shop', repo: 'api' };
  w.run('__row.ccPatch(__s3)');
  assert.equal(text(r.querySelector('.rr-meta')), 'error');
});
