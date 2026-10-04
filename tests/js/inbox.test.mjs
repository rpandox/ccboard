// Contract tests for app/static/pages/inbox.js (v0.5.5): Inbox.kind (the nine kinds and their priority), Inbox.items (what is listed, in which
// order), inboxCard (context first, Allow / Deny only without a full client, the suggested-reply chip, the nudge chips), Inbox.section (Home's
// first block) and the #/inbox page. Runs on the real core.js, components.js, router.js and pages/*.js inside the vm harness (tests/js/world.mjs).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { EPOCH, ISO, calls, fakeState, fixtureState, homeWorld, page, plain, projectsOf, sess, setState, text, tick } from './world.mjs';

const KINDS = ['permission', 'plan', 'question', 'needs', 'limit', 'error', 'done-question', 'waiting', 'done'];

/** A world over `st`; kind(over, extraState) classifies a one-off session `shop--api--x` built from the defaults plus `over`. */
function world(st = fixtureState()) {
  const { w } = homeWorld({ state: st });
  const kindOf = (over, stOver = {}) => {
    const s = sess('shop', 'api', 'x', { state: 'idle', needs_attention: true, ...over });
    const state = fakeState({ projects: projectsOf({ shop: { api: [s] } }), ...stOver });
    w.ctx.__s = s; w.ctx.__stx = state;
    return w.run('Inbox.kind(__s, __stx)');
  };
  return { w, kindOf };
}

const PERM = (tmux = 'shop--api--x') => ({ pending_permissions: [{ id: 3, tmux_name: tmux, tool_name: 'Bash', summary: 'Bash: rm -rf build' }] });
const LIMIT = (over = {}) => ({ rate_limited: { value: { session: 'shop--api--x', message: '5-hour limit reached · resets 6:25am', kind: '5h', resets_at: EPOCH(-120), ...over }, at: ISO(5) } });

// ---------------------------------------------------------------- Inbox.kind

test('Inbox.kind: one session per kind, from the shapes the state really carries', () => {
  const { kindOf } = world();
  assert.equal(kindOf({ state: 'waiting' }, PERM()), 'permission');
  assert.equal(kindOf({ state: 'waiting', last_message: 'Here is the plan: 1. migrate 2. deploy. Approve?' }), 'plan');
  assert.equal(kindOf({ state: 'waiting', last_message: 'Ready to exit plan mode and start coding.' }), 'plan');
  assert.equal(kindOf({ state: 'waiting', last_message: 'Calling ExitPlanMode with the steps above' }), 'plan');
  assert.equal(kindOf({ state: 'waiting', last_message: 'Should the endpoint be paginated?' }), 'question');
  assert.equal(kindOf({ state: 'waiting', last_message: 'Which database? (AskUserQuestion)' }), 'question');
  assert.equal(kindOf({ state: 'idle', flags: { registry: { job: { state: 'blocked', needs: 'approve the plan', tempo: 'blocked' } } } }), 'needs');
  assert.equal(kindOf({ state: 'idle', flags: { registry: { job: { state: 'running', tempo: 'blocked', needs: null } } } }), 'needs', "tempo 'blocked' alone is enough");
  assert.equal(kindOf({ state: 'errored', last_message: 'API Error: 500' }), 'error');
  assert.equal(kindOf({ state: 'errored', last_message: "You've hit your session limit" }), 'limit', 'an errored session with a limit message');
  assert.equal(kindOf({ state: 'errored', last_message: 'boom' }, LIMIT()), 'limit', 'the episode names this session');
  assert.equal(kindOf({ state: 'done', last_message: 'Shall I also add tests?' }), 'done-question');
  assert.equal(kindOf({ state: 'waiting', last_message: 'Claude is waiting for your input' }), 'waiting');
  assert.equal(kindOf({ state: 'done', last_message: 'All 12 tests pass.' }), 'done');
});

test('Inbox.kind: the priority is permission, plan, question, needs, limit, error, done-question, waiting, done', () => {
  const { kindOf } = world();
  const job = { registry: { job: { state: 'blocked', needs: 'approve', tempo: 'blocked' } } };
  assert.equal(kindOf({ state: 'waiting', last_message: 'The plan? Approve it?' }, PERM()), 'permission', 'a pending permission beats a plan');
  assert.equal(kindOf({ state: 'waiting', last_message: 'Do you approve this plan?' }), 'plan', 'plan beats question');
  assert.equal(kindOf({ state: 'waiting', last_message: 'Which one?', flags: job }), 'question', 'question beats a blocked job');
  assert.equal(kindOf({ state: 'errored', last_message: 'limit hit', flags: job }, LIMIT()), 'needs', 'a blocked job beats a limit');
  assert.equal(kindOf({ state: 'errored', last_message: 'limit hit' }), 'limit', 'limit beats error');
  assert.equal(kindOf({ state: 'done', last_message: 'Done. Next?', flags: job }), 'needs', 'a blocked job beats done-question');
});

test('Inbox.kind: what is not asking for you has no kind', () => {
  const { kindOf } = world();
  assert.equal(kindOf({ state: 'idle', last_message: 'Anything else?' }), '', 'an idle session that ends in ? is not a question');
  assert.equal(kindOf({ state: 'working', last_message: 'Should I continue?' }), '', 'working');
  assert.equal(kindOf({ state: 'working', last_message: 'here is the plan' }), '', 'a plan text only counts for a waiting session');
  assert.equal(kindOf({ state: 'ended' }), '');
  assert.equal(kindOf({ state: 'done', needs_attention: false, last_message: 'All done' }), '', 'a done session that was acknowledged');
  assert.equal(kindOf({ state: 'done', needs_attention: false, last_message: 'Anything else?' }), '', 'and a done question that was acknowledged');
});

test('Inbox.kind: a rate limit counts while it is in force and only for the session it names', () => {
  const { kindOf } = world();
  assert.equal(kindOf({ state: 'idle' }, LIMIT()), 'limit');
  assert.equal(kindOf({ state: 'idle' }, LIMIT({ session: 'shop--api--other' })), '', 'another session hit it');
  assert.equal(kindOf({ state: 'errored', last_message: 'boom' }, LIMIT({ resets_at: EPOCH(10) })), 'error', 'the window has reset: an ordinary error');
  assert.equal(kindOf({ state: 'idle' }, LIMIT({ resets_at: EPOCH(10) })), '');
  assert.equal(kindOf({ state: 'idle' }, { rate_limited: { session: 'shop--api--x', message: 'limit', kind: '5h', resets_at: EPOCH(-60) } }), 'limit', 'a flat record reads like the kv wrapper');
  assert.equal(kindOf({ state: 'idle' }, { rate_limited: null }), '');
});

test('Inbox.kind: a question is what ends in ? (quotes and closing brackets allowed), not any ? inside the text', () => {
  const { kindOf } = world();
  for (const msg of ['Which one?', 'Which one? ', 'Which one? ”'.replace('”', '"'), 'Which one?)', 'Use `a` or `b`?']) assert.equal(kindOf({ state: 'waiting', last_message: msg }), 'question', msg);
  assert.equal(kindOf({ state: 'waiting', last_message: 'Is it ok? I will start now.' }), 'waiting', 'a ? in the middle does not make a question');
  assert.equal(kindOf({ state: 'waiting', last_message: '' }), 'waiting');
});

// ---------------------------------------------------------------- Inbox.items

const items = (w, st) => { w.ctx.__stx = st; return plain(w.run('Inbox.items(__stx)')).map((i) => [i.tmux.split('--').slice(1).join('/'), i.kind]); };

test('Inbox.items: the demo fleet lists permission, question, needs, limit, done in that order', () => {
  const { w } = world();
  assert.deepEqual(items(w, fixtureState()), [
    ['website/t-checkout-redesign', 'permission'], ['api/s3', 'question'], ['ccboard/s1', 'needs'], ['api/s2', 'limit'], ['api/s1', 'done'],
  ]);
  assert.deepEqual(plain(w.run('Inbox.ORDER')), KINDS);
  const first = plain(w.run('Inbox.items(__stx)'))[0];
  assert.equal(first.project, 'phasezero');
  assert.equal(first.repo, 'website');
  assert.equal(first.perm.id, 12, 'each item carries its pending permission');
  assert.equal(first.name, 't-checkout-redesign', 'and the roster session fields');
});

test('Inbox.items: acknowledged sessions drop out, except a pending permission or a blocked job, which acknowledging does not answer', () => {
  const { w } = world();
  const st = fixtureState();
  const all = st.projects.flatMap((p) => [p.root, ...p.repos]).filter(Boolean).flatMap((r) => r.sessions);
  const by = (tmux) => all.find((s) => s.tmux === tmux);
  by(P1).needs_attention = false;                                         // the done one is acked: it goes
  by(P3).needs_attention = false;                                         // so does the question
  by(CK).needs_attention = false;                                         // the permission session (acked, but the permission is still pending)
  by(S1).needs_attention = false;                                         // and the blocked-job session was never marked
  const got = items(w, st);
  assert.deepEqual(got.map((x) => x[1]), ['permission', 'needs', 'limit'], 'the done and the question are gone; the permission and the blocked job stay');
  assert.equal(got.some(([n]) => n === 'api/s3' || n === 'api/s1'), false);
  st.pending_permissions = [];                                            // answered: now the acked permission session has nothing left to ask
  assert.deepEqual(items(w, st).map((x) => x[1]), ['needs', 'limit']);
});

test('Inbox.items: within a kind the oldest activity comes first (the one waiting longest)', () => {
  const { w } = world();
  const a = sess('shop', 'api', 'a', { state: 'waiting', needs_attention: true, last_message: 'First?', state_at: ISO(5) });
  const b = sess('shop', 'api', 'b', { state: 'waiting', needs_attention: true, last_message: 'Second?', state_at: ISO(50) });
  const c = sess('shop', 'api', 'c', { state: 'waiting', needs_attention: true, last_message: 'Third?', state_at: ISO(20) });
  const d = sess('shop', 'api', 'd', { state: 'done', needs_attention: true, last_message: 'ok', state_at: ISO(900) });
  assert.deepEqual(items(w, fakeState({ projects: projectsOf({ shop: { api: [a, b, c, d] } }) })).map((x) => x[0]), ['api/b', 'api/c', 'api/a', 'api/d']);
});

test('Inbox.items: no state, no list; an empty fleet is an empty list', () => {
  const { w } = world();
  assert.deepEqual(plain(w.run('Inbox.items(null)')), []);
  assert.deepEqual(items(w, fakeState()), []);
});

// ---------------------------------------------------------------- the card

const card = (w, tmux, st) => {
  w.ctx.__stx = st || fixtureState(); w.ctx.__tmux = tmux;
  w.run('globalThis.__card = inboxCard(Inbox.items(__stx).find((s) => s.tmux === __tmux), __stx, {})');
  return w.get('__card');
};
const CK = 'phasezero--website--t-checkout-redesign', S1 = 'ccboard--ccboard--s1', P2 = 'petroit--api--s2', P3 = 'petroit--api--s3', P1 = 'petroit--api--s1';
const buttons = (n, sel) => n.querySelectorAll(sel).map(text);

test('a card leads with the context, then where it is from, then the actions', () => {
  const { w } = world();
  const n = card(w, P3);
  assert.equal(n.getAttribute('data-tmux'), P3);
  for (const cls of ['inbox-card', 'inbox-item', 'kind-question']) assert.ok(n.classList.contains(cls), cls);
  const order = n.children.map((c) => c.className.split(' ')[0]);
  assert.deepEqual(order.slice(0, 3), ['ib-lead', 'ib-sub', 'ib-prompt'].slice(0, 2).concat(order[2]), 'lead first, then the sub line');
  assert.equal(order[0], 'ib-lead');
  assert.ok(order.indexOf('ib-lead') < order.indexOf('ib-sub') && order.indexOf('ib-sub') < order.indexOf('ib-actions'), order.join(' '));
  assert.equal(text(n.querySelector('.ib-kind')), 'question');
  assert.match(text(n.querySelector('.ib-ctx')), /Which one should GET \/devices expose\?$/, 'the question itself leads');
  assert.equal(text(n.querySelector('.ib-where')), 'petroit/api');
  assert.equal(text(n.querySelector('.ib-name')), 's3');
  assert.equal(n.querySelector('.ib-name').getAttribute('href'), '#/s/petroit--api--s3', 'the name opens the peek');
  const age = n.querySelector('time.age');
  assert.match(text(age), /^\d+m$/);
  assert.ok(Number(age.getAttribute('data-epoch')) > 0);
  assert.ok(n.querySelector('.glyph.waiting') && n.querySelector('.glyph.agent'));
  assert.match(text(n.querySelector('.ib-prompt')), /^› fix the login bug/);
});

test('the permission card leads with the summary in mono; the plan and question cards lead with the text', () => {
  const { w } = world();
  const perm = card(w, CK);
  assert.equal(text(perm.querySelector('.ib-ctx')), 'Bash: npm test');
  assert.ok(perm.querySelector('.ib-ctx').classList.contains('mono'));
  assert.ok(perm.classList.contains('kind-permission') && perm.classList.contains('attn'));
  assert.equal(text(perm.querySelector('.ib-task')), 'Redesign checkout step 2 for mobile', 'the task title is on the card');
  const st = fixtureState();
  st.projects[1].repos[0].sessions[2].last_message = 'Plan: 1) add the cursor column 2) backfill 3) switch the endpoint. Approve this plan?';
  const plan = card(w, P3, st);
  assert.ok(plan.classList.contains('kind-plan'));
  assert.equal(text(plan.querySelector('.ib-kind')), 'plan review');
  assert.match(text(plan.querySelector('.ib-ctx')), /^Plan: 1\) add the cursor column/);
  assert.equal(plan.querySelector('.ib-ctx').classList.contains('mono'), false);
});

test('a long question shows its end (the question), a long plan its start', () => {
  const { w } = world();
  const st = fixtureState();
  const s = st.projects[1].repos[0].sessions[2];
  s.last_message = 'x'.repeat(900) + ' Which one do you want?';
  const q = text(card(w, P3, st).querySelector('.ib-ctx'));
  assert.ok(q.startsWith('…') && q.endsWith('Which one do you want?') && q.length <= 402, q.length);
  s.last_message = 'Plan: ' + 'y'.repeat(900);
  const p = text(card(w, P3, st).querySelector('.ib-ctx'));
  assert.ok(p.startsWith('Plan: yyy') && p.endsWith('…') && p.length <= 702, p.length);
});

test('the blocked-job card leads with what the job needs and offers its suggested reply as a chip', () => {
  const { w } = world();
  const n = card(w, S1);
  assert.ok(n.classList.contains('kind-needs'));
  assert.equal(text(n.querySelector('.ib-kind')), 'job needs you');
  assert.equal(text(n.querySelector('.ib-ctx')), 'approve the plan');
  const chip = n.querySelector('.ib-reply button');
  assert.ok(chip && text(chip) === 'approve');
  assert.equal(n.querySelector('.ib-reply').classList.contains('hidden'), false);
});

test('the suggested-reply chip types the reply with enter through /keys and toasts', async () => {
  const { w } = world();
  const n = card(w, S1);
  n.querySelector('.ib-reply button').click();
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.path.endsWith('/keys')), [{ method: 'POST', path: '/api/sessions/ccboard--ccboard--s1/keys', body: { text: 'approve', enter: true } }]);
  assert.deepEqual(plain(w.get('__toasts')), [{ text: 'sent "approve" to s1', kind: 'ok' }]);
  // a failure is a toast, not a throw
  w.run('api = async () => { throw new Error("no such session"); }');
  n.querySelector('.ib-reply button').click();
  await tick();
  assert.deepEqual(plain(w.get('__toasts')).pop(), { text: 'no such session', kind: 'bad' });
});

test('no suggested reply, no chip; the chip follows the job when a poll changes it', () => {
  const { w } = world();
  const st = fixtureState();
  const s1 = st.projects[0].repos[0].sessions[0];
  s1.flags.registry.job.suggested_reply = '';
  const n = card(w, S1, st);
  assert.equal(n.querySelector('.ib-reply button'), null);
  assert.ok(n.querySelector('.ib-reply').classList.contains('hidden'));
  s1.flags.registry.job.suggested_reply = 'go ahead';
  w.ctx.__stx = st;
  w.run('__card.ccPatch(Inbox.items(__stx).find((s) => s.tmux === __tmux), __stx)');
  assert.equal(text(n.querySelector('.ib-reply button')), 'go ahead');
  s1.flags.registry.job.suggested_reply = 'x'.repeat(100);
  w.run('__card.ccPatch(Inbox.items(__stx).find((s) => s.tmux === __tmux), __stx)');
  assert.equal(text(n.querySelector('.ib-reply button')).length, 60, 'a long reply is cut at 60');
});

test('the limit card leads with the limit message and the reset time', () => {
  const { w } = world();
  const n = card(w, P2);
  assert.ok(n.classList.contains('kind-limit'));
  assert.equal(text(n.querySelector('.ib-kind')), 'limit hit');
  assert.equal(text(n.querySelector('.ib-ctx')), '5-hour limit reached · resets 6:25am');
  assert.match(text(n.querySelector('.ib-note')), /^resets \d{2}:\d{2}( · in \d+[mh])?/, 'the reset clock time, and the countdown while it is ahead');
  assert.equal(n.querySelector('.ib-note').classList.contains('hidden'), false);
  const st = fixtureState({ rate_limited: null });
  const err = card(w, P2, st);
  assert.ok(err.classList.contains('kind-limit'), 'the errored session with a limit message is a limit card without the episode record too');
  assert.equal(err.querySelector('.ib-note').classList.contains('hidden'), true, 'no reset time to show');
});

test('Allow and Deny show only for a pending permission and only while no full terminal client is attached', async () => {
  const { w } = world();
  const n = card(w, CK);
  assert.deepEqual(buttons(n, '.perm-btns button'), ['Allow', 'Deny'], 'viewers.full is 0');
  assert.equal(n.querySelector('.perm-btns').classList.contains('hidden'), false);
  // a person is at the terminal: the TUI prompt is in front of them, no remote buttons
  const st = fixtureState();
  st.projects[2].repos[1].sessions[0].viewers = { full: 1, grid: 0, ro: 0 };
  w.ctx.__stx = st;
  w.run('__card.ccPatch(Inbox.items(__stx).find((s) => s.tmux === __tmux), __stx)');
  assert.deepEqual(buttons(n, '.perm-btns button'), [], 'viewers.full is 1');
  assert.equal(n.querySelector('.perm-btns').classList.contains('hidden'), true);
  // a grid tile or a read-only view is not a person at the prompt
  st.projects[2].repos[1].sessions[0].viewers = { full: 0, grid: 2, ro: 1 };
  w.run('__card.ccPatch(Inbox.items(__stx).find((s) => s.tmux === __tmux), __stx)');
  assert.deepEqual(buttons(n, '.perm-btns button'), ['Allow', 'Deny']);
  // a missing viewers object counts as nobody
  delete st.projects[2].repos[1].sessions[0].viewers;
  w.run('__card.ccPatch(Inbox.items(__stx).find((s) => s.tmux === __tmux), __stx)');
  assert.deepEqual(buttons(n, '.perm-btns button'), ['Allow', 'Deny']);
  // the permission is answered elsewhere: the buttons go with it
  st.pending_permissions = [];
  w.run('__card.ccPatch({ ...__stx.projects[2].repos[1].sessions[0], project: "phasezero", repo: "website", kind: "waiting" }, __stx)');
  assert.deepEqual(buttons(n, '.perm-btns button'), []);
  // Allow and Deny post the decision
  w.ctx.__stx = fixtureState();
  const fresh = card(w, CK);
  fresh.querySelectorAll('.perm-btns button')[0].click();
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.path.includes('/permission/')).pop(), { method: 'POST', path: '/api/permission/12/allow' });
  fresh.querySelectorAll('.perm-btns button')[1].click();
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.path.includes('/permission/')).pop(), { method: 'POST', path: '/api/permission/12/deny' });
});

test('no other kind carries Allow or Deny', () => {
  const { w } = world();
  for (const tmux of [P3, S1, P2, P1]) {
    const n = card(w, tmux);
    assert.deepEqual(buttons(n, '.perm-btns button'), [], tmux);
    assert.deepEqual(n.querySelectorAll('button').map(text).filter((t) => t === 'Allow' || t === 'Deny'), [], tmux);
  }
});

test('Open links to the terminal, Ack shows only for an unacknowledged session and acknowledges', async () => {
  const { w } = world();
  const n = card(w, P1);
  const open = n.querySelector('a[href="/term/petroit--api--s1"]');
  assert.ok(open && text(open) === 'Open' && open.getAttribute('target') === '_blank');
  const ack = n.querySelector('.slot-ack button');
  assert.equal(text(ack), 'Ack');
  ack.click();
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.path.endsWith('/ack')), [{ method: 'POST', path: '/api/sessions/petroit--api--s1/ack' }]);
  const blocked = card(w, S1);
  assert.equal(blocked.querySelector('.slot-ack button'), null, 'the blocked-job session was never marked as needing an ack');
});

test('the nudge chips and the reply box are on the card; a shell session gets neither', async () => {
  const { w } = world();
  const n = card(w, P3);
  assert.deepEqual(buttons(n, '.ib-chips .chip-btn'), ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it', '…'], 'six chips and the … that shows the last two');
  assert.deepEqual(n.querySelectorAll('.ib-chips .chip-extra').map(text), ['add commit push', 'do it'], 'only the first four show until the … is tapped');
  n.querySelectorAll('.ib-chips .chip-btn').find((b) => text(b) === 'merge').click();
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.path.endsWith('/keys')), [{ method: 'POST', path: '/api/sessions/petroit--api--s3/keys', body: { text: 'merge', enter: true } }]);
  const ta = n.querySelector('form.ib-send textarea.composer');
  assert.ok(ta && /^reply to s3/.test(ta.getAttribute('aria-label')));
  ta.value = 'cursor please\nwith a limit param';
  ta.dispatchEvent({ type: 'keydown', key: 'Enter', preventDefault() {} });
  await tick();
  assert.deepEqual(calls(w).filter((c) => c.path.endsWith('/keys')).pop(), { method: 'POST', path: '/api/sessions/petroit--api--s3/keys', body: { text: 'cursor please\nwith a limit param', enter: true } });
  assert.equal(ta.value, '');
  const st = fixtureState();
  const sh = sess('shop', 'api', 'sh', { state: 'done', needs_attention: true, launcher: 'shell', agent: 'shell', command: 'bash', stats: null });
  st.projects.push(...projectsOf({ shop: { api: [sh] } }));
  const shell = card(w, 'shop--api--sh', st);
  assert.ok(shell.querySelector('.ib-chips').classList.contains('hidden'));
  assert.ok(shell.querySelector('form.ib-send').classList.contains('hidden'));
});

test('a poll that changes nothing leaves the card alone (and a typed draft); a changed message repaints it', () => {
  const { w } = world();
  const n = card(w, P3);
  const sig = n._sig;
  assert.ok(sig);
  const ta = n.querySelector('textarea.composer');
  ta.value = 'half a reply';
  const kids = n.children.slice();
  w.run('__card.ccPatch(Inbox.items(__stx).find((s) => s.tmux === __tmux), __stx)');
  assert.equal(n._sig, sig);
  assert.deepEqual(n.children, kids);
  assert.equal(ta.value, 'half a reply');
  const st = fixtureState();
  st.projects[1].repos[0].sessions[2].last_message = 'Actually: tabs or spaces?';
  w.ctx.__stx = st;
  w.run('__card.ccPatch(Inbox.items(__stx).find((s) => s.tmux === __tmux), __stx)');
  assert.notEqual(n._sig, sig);
  assert.equal(text(n.querySelector('.ib-ctx')), 'Actually: tabs or spaces?');
  assert.equal(ta.value, 'half a reply', 'the draft survives the repaint');
  assert.equal(n.querySelector('textarea.composer'), ta, 'same composer node');
});

// ---------------------------------------------------------------- Inbox.section (Home)

function section(w, st, opts) {
  if (!w.get('typeof __host') || w.get('__host') === undefined) w.run('globalThis.__host = document.createElement("section")');
  w.ctx.__stx = st; w.ctx.__opts = opts;
  return { n: w.run('Inbox.section(__host, __stx, __opts)'), host: w.get('__host') };
}

test('Inbox.section reconciles the cards into the host and returns the whole count', () => {
  const { w } = world();
  w.run('globalThis.__host = document.createElement("section")');
  let r = section(w, fixtureState(), { limit: 8, link: '#/inbox' });
  assert.equal(r.n, 5);
  assert.deepEqual(r.host.querySelectorAll('.inbox-card').map((c) => c.getAttribute('data-tmux')), [CK, P3, S1, P2, P1]);
  assert.equal(text(r.host.querySelector('.ib-title')), 'Needs you (5)');
  assert.equal(r.host.classList.contains('hidden'), false);
  const more = r.host.querySelector('.ib-more');
  assert.equal(more.getAttribute('href'), '#/inbox');
  assert.equal(more.classList.contains('hidden'), false);
  assert.equal(text(more), 'Open inbox');
  r = section(w, fixtureState(), { limit: 2, link: '#/inbox' });
  assert.equal(r.n, 5, 'the count is of all of them');
  assert.deepEqual(r.host.querySelectorAll('.inbox-card').map((c) => c.getAttribute('data-tmux')), [CK, P3], 'only the first two are drawn');
  assert.equal(text(r.host.querySelector('.ib-more')), 'Show all 5');
  assert.equal(text(r.host.querySelector('.ib-title')), 'Needs you (5)');
});

test('Inbox.section keeps the card nodes across polls, reorders, drops what left and hides when empty', () => {
  const { w } = world();
  w.run('globalThis.__host = document.createElement("section")');
  let r = section(w, fixtureState(), { limit: 8, link: '#/inbox' });
  const before = Object.fromEntries(r.host.querySelectorAll('.inbox-card').map((c) => [c.getAttribute('data-tmux'), c]));
  const st = fixtureState();
  st.pending_permissions = [];                                           // the permission is answered: that session becomes an ordinary waiting one
  st.projects[2].repos[1].sessions[0].needs_attention = true;
  r = section(w, st, { limit: 8, link: '#/inbox' });
  const cards = r.host.querySelectorAll('.inbox-card');
  assert.deepEqual(cards.map((c) => c.getAttribute('data-tmux')), [P3, S1, P2, CK, P1], 'question, needs, limit, the now-ordinary waiting, done');
  for (const c of cards) assert.equal(c, before[c.getAttribute('data-tmux')], `${c.getAttribute('data-tmux')}: the same node`);
  assert.equal(cards[0].getAttribute('data-tmux'), P3, 'the question now leads (the permission card became a waiting card)');
  assert.ok(before[CK].classList.contains('kind-waiting'), 'and its card followed the kind');
  const empty = section(w, fakeState(), { limit: 8, link: '#/inbox' });
  assert.equal(empty.n, 0);
  assert.equal(empty.host.classList.contains('hidden'), true);
  assert.equal(empty.host.querySelectorAll('.inbox-card').length, 0);
});

// ---------------------------------------------------------------- #/inbox

test('#/inbox lists the cards in kind order with a summary and the kind as a label', () => {
  const { w } = homeWorld({ state: fixtureState() });
  w.location.hash = '#/inbox';
  const root = page(w);
  assert.equal(w.document.title, '(5) Needs you · ccboard');
  assert.deepEqual(root.querySelectorAll('.inbox-card').map((c) => c.getAttribute('data-tmux')), [CK, P3, S1, P2, P1]);
  assert.deepEqual(root.querySelectorAll('.ib-kind').map(text), ['permission', 'question', 'job needs you', 'limit hit', 'done']);
  assert.equal(text(root.querySelector('.summary')), '✻ 5 need you · 1 permission · 1 question · 1 job needs you · 1 limit hit · 1 done');
  assert.equal(root.querySelector('.hint').classList.contains('hidden'), false);
  assert.equal(root.querySelectorAll('.nonideal').find((n) => /Nothing needs you/.test(text(n))).classList.contains('hidden'), true);
});

test('#/inbox: nothing needs you says so, and the title loses its count', () => {
  const { w } = homeWorld({ state: fakeState({ projects: projectsOf({ shop: { api: [sess('shop', 'api', 's1')] } }) }) });
  w.location.hash = '#/inbox';
  assert.equal(w.document.title, 'Needs you · ccboard');
  assert.equal(page(w).querySelectorAll('.inbox-card').length, 0);
  const none = page(w).querySelectorAll('.nonideal').find((n) => /nothing needs you/i.test(text(n)));
  assert.equal(none.classList.contains('hidden'), false);
  assert.equal(text(page(w).querySelector('.summary')), '');
});

test('#/inbox: a poll patches the cards in place, a new session adds one, an answered one leaves', () => {
  const { w } = homeWorld({ state: fixtureState() });
  w.location.hash = '#/inbox';
  const root = page(w);
  const keep = root.querySelector('.inbox-card[data-tmux="petroit--api--s3"]');
  const st = fixtureState();
  const extra = sess('petroit', 'api', 's4', { state: 'waiting', needs_attention: true, last_message: 'Squash or merge?', state_at: ISO(1) });
  st.projects[1].repos[0].sessions.push(extra);
  st.projects[1].repos[0].sessions = st.projects[1].repos[0].sessions.filter((s) => s.name !== 's1');
  setState(w, st);
  const tmuxes = root.querySelectorAll('.inbox-card').map((c) => c.getAttribute('data-tmux'));
  assert.deepEqual(tmuxes, [CK, 'petroit--api--s3', 'petroit--api--s4', S1, P2]);
  assert.equal(root.querySelector('.inbox-card[data-tmux="petroit--api--s3"]'), keep, 'the same node');
  assert.equal(w.document.title, '(5) Needs you · ccboard');
});

test('#/inbox: a click on a card selects it (the j / k selection paints the legacy inbox-item class)', () => {
  const { w } = homeWorld({ state: fixtureState() });
  w.location.hash = '#/inbox';
  const root = page(w);
  const p3 = root.querySelector('.inbox-card[data-tmux="petroit--api--s3"]');
  p3.click();
  assert.equal(w.get('ui').inboxSel, 1, 'the question is the second card');
  assert.ok(p3.classList.contains('sel'));
  assert.equal(root.querySelector('.inbox-card[data-tmux="phasezero--website--t-checkout-redesign"]').classList.contains('sel'), false);
  assert.equal(w.run('Pages.selected().tmux'), 'petroit--api--s3');
  assert.equal(w.run('Pages.items().length'), 5, 'the keyboard walks the same list the page draws');
  assert.deepEqual(plain(w.run('Pages.items().map((s) => s.tmux)')), [CK, P3, S1, P2, P1], 'in the same order');
});
