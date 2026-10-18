// #311 (#30 rule 2): every action paints its result at once, the board only confirms, a refusal puts it back with the board's words. Kill (Agents row menu > Kill > Confirm Kill), Send from
// the session peek box and Add to backlog from the New task sheet, each with the request held on a promise the test controls: the DOM shows the optimistic state BEFORE the promise settles,
// a rejected request rolls it back. Then the permission mode a sheet starts from on a Claude that cannot start in auto. Real scripts on minidom's DOM through world.mjs's homeWorld.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { calls, fixtureState, homeWorld, page, plain, text, tick } from './world.mjs';

const S1 = 'ccboard--ccboard--s1';
const sheet = (w) => w.document.getElementById('sheet');
const toasts = (w) => plain(w.get('__toasts'));

/** Hold every request whose path starts with `prefix` until release(answer) or refuse(message). */
function hold(w, prefix) {
  w.run(`
    globalThis.__held = [];
    __answers[${JSON.stringify(prefix)}] = () => new Promise((res, rej) => { __held.push({ res, rej }); });
  `);
  return {
    count: () => w.get('__held').length,
    release: async (answer = { ok: true }) => { w.ctx.__ans = answer; w.run('__held.shift().res(__ans)'); await tick(); await tick(); },
    refuse: async (message, status = 409) => { w.ctx.__msg = message; w.ctx.__sts = status; w.run('{ const e = new Error(__msg); e.status = __sts; __held.shift().rej(e); }'); await tick(); await tick(); },
  };
}

const itemLabel = (i) => text(i.querySelector('.mi-name') || i).trim();
function rowMenu(w, row, label) {
  const b = row.querySelector('.rr-more');
  b.click();
  const hit = w.document.querySelectorAll('.menuitem').find((i) => itemLabel(i) === label);
  if (hit) { hit.click(); return true; }
  b.click();
  return false;
}

// ---------------------------------------------------------------- Kill

async function killWorld() {
  const { w } = homeWorld({ state: fixtureState() });
  w.location.hash = '#/agents';
  await tick(); await tick();
  const row = () => page(w).querySelector(`.rrow[data-tmux=${S1}]`);
  const confirm = () => row().querySelector('.slot-kill').querySelectorAll('button').find((b) => /Confirm Kill/.test(text(b)));
  assert.ok(rowMenu(w, row(), 'Kill'));
  return { w, row, confirm, h: hold(w, '/api/sessions/') };
}

test('Confirm Kill: the row is a dimmed "closing…" ghost with its actions off before the board has answered, and a poll that still lists the session does not bring it back', async () => {
  const { w, row, confirm, h } = await killWorld();
  const before = row();
  confirm().click();
  assert.equal(h.count(), 1, 'the DELETE is in flight');
  assert.deepEqual(calls(w).pop(), { method: 'DELETE', path: `/api/sessions/${S1}` });
  assert.ok(row().classList.contains('closing'), 'the ghost is up while the request is held');
  assert.equal(text(row().querySelector('.rr-meta')), 'closing…');
  assert.equal(row().querySelector('.rr-more').disabled, true, 'the menu is off');
  const slot = row().querySelectorAll('.slot-kill button');
  assert.equal(slot.length, 1);
  assert.equal(text(slot[0]), 'Closing…');
  assert.ok(slot[0].disabled || slot[0].hasAttribute('disabled'), 'and off');
  w.run('updateCurrentPage(state)');                                   // a poll answered with the session still in it
  assert.equal(row(), before, 'the same row, not a new one');
  assert.ok(row().classList.contains('closing'), 'still closing: the poll does not resurrect it');
  assert.equal(text(row().querySelector('.rr-meta')), 'closing…');
  await h.release();
  assert.equal(w.run('sessionClosing.size'), 0, 'settled: nothing is pending any more');
  assert.equal(toasts(w).length, 0);
});

test('a refused Kill puts the row back, the Kill button too, and says why in the board\'s words', async () => {
  const { w, row, confirm, h } = await killWorld();
  confirm().click();
  assert.ok(row().classList.contains('closing'));
  await h.refuse('tmux says the session is busy');
  assert.equal(row().classList.contains('closing'), false, 'the ghost is gone');
  assert.notEqual(text(row().querySelector('.rr-meta')), 'closing…');
  assert.equal(row().querySelector('.rr-more').disabled, false, 'the menu works again');
  assert.equal(text(row().querySelector('.slot-kill')), '', 'no Closing… button left');
  assert.deepEqual(toasts(w), [{ text: `${S1} was not closed: tmux says the session is busy`, kind: 'bad' }]);
  assert.equal(w.run('sessionClosing.size'), 0);
});

test('the peek\'s Kill goes through the same path: its card dims and says closing… until the board answers', async () => {
  const { w } = homeWorld({ state: fixtureState() });
  w.location.hash = `#/s/${S1}`;
  await tick(); await tick();
  const card = () => sheet(w).querySelector('.peek-card');
  const h = hold(w, '/api/sessions/');
  const arm = () => card().querySelector('.slot-kill').querySelectorAll('button').find((b) => text(b) === 'Kill');
  arm().click();
  const go = card().querySelector('.slot-kill').querySelectorAll('button').find((b) => /Confirm Kill/.test(text(b)));
  go.click();
  assert.ok(card().classList.contains('closing'));
  assert.equal(text(card().querySelector('.slot-kill')), 'Closing…');
  await h.refuse('no such session', 404);
  assert.equal(card().classList.contains('closing'), false);
  assert.equal(text(card().querySelector('.slot-kill')), 'Kill', 'the peek keeps its Kill button');
});

// ---------------------------------------------------------------- Send

async function peekWorld() {
  const { w } = homeWorld({ state: fixtureState() });
  w.location.hash = `#/s/${S1}`;
  await tick(); await tick();
  const peek = sheet(w).querySelector('.peek');
  const input = peek.querySelector('form.peek-send textarea');
  const send = (v) => { input.value = v; peek.querySelector('form.peek-send').dispatchEvent({ type: 'submit', preventDefault() {} }); };
  return { w, input, send, h: hold(w, `/api/sessions/${S1}/keys`) };
}

test('Send from the peek box: the text leaves the box at once and a "sending…" note stands in for it; the answer only confirms', async () => {
  const { w, input, send, h } = await peekWorld();
  send('run the tests again');
  assert.equal(input.value, '', 'cleared before the board answered');
  assert.equal(h.count(), 1);
  assert.deepEqual(calls(w).pop(), { method: 'POST', path: `/api/sessions/${S1}/keys`, body: { text: 'run the tests again', enter: true } });
  assert.deepEqual(toasts(w), [{ text: 'sending…', kind: 'info' }]);
  await h.release();
  assert.equal(input.value, '');
  assert.deepEqual(toasts(w).pop(), { text: 'sent', kind: 'ok' });
});

test('a refused Send puts the text back in the box and says why; text typed meanwhile is not overwritten', async () => {
  const a = await peekWorld();
  a.send('first line\nsecond line');
  assert.equal(a.input.value, '');
  await a.h.refuse('the session is not at a prompt');
  assert.equal(a.input.value, 'first line\nsecond line', 'the text is back, line break and all');
  assert.deepEqual(toasts(a.w).pop(), { text: 'the session is not at a prompt', kind: 'bad' });
  const b = await peekWorld();
  b.send('old text');
  b.input.value = 'something new';                                    // typed while the request was out
  await b.h.refuse('nope');
  assert.equal(b.input.value, 'something new', 'the new text wins');
});

test('the Agents row\'s own send box does the same: empty at once, the echo line shows the text, a refusal restores both', async () => {
  const { w } = homeWorld({ state: fixtureState() });
  w.location.hash = '#/agents';
  await tick(); await tick();
  const row = () => page(w).querySelector(`.rrow[data-tmux=${S1}]`);
  const ta = row().querySelector('.rr-send textarea');
  const line = () => row().querySelector('.rr-last .dim');
  const was = text(line());
  const h = hold(w, `/api/sessions/${S1}/keys`);
  ta.value = 'continue';
  row().querySelector('form.rr-send').dispatchEvent({ type: 'submit', preventDefault() {}, stopPropagation() {} });
  assert.equal(ta.value, '', 'cleared before the board answered');
  assert.equal(text(line()), '› continue', 'echoed at once');
  await h.refuse('busy');
  assert.equal(ta.value, 'continue');
  assert.equal(text(line()), was, 'the line is as it was');
  assert.deepEqual(toasts(w).pop(), { text: 'busy', kind: 'bad' });
});

// ---------------------------------------------------------------- Add to backlog

const ccbState = () => fixtureState();
const typeInto = (n, v) => { n.value = v; n.dispatchEvent({ type: 'input' }); };
const submit = (f) => f.dispatchEvent({ type: 'submit', preventDefault() {} });
const form = (w) => sheet(w).querySelector('form.lx-form');
const backlog = (w) => page(w).querySelector('.col[data-col=backlog]');
const pendingCards = (w) => page(w).querySelectorAll('.task.pending');

async function laterWorld(state = ccbState()) {
  const { w } = homeWorld({ state });
  w.location.hash = '#/tasks';
  await tick(); await tick();
  const h = hold(w, '/api/tasks');
  w.run(`openLauncher({ project: 'ccboard', repo: 'ccboard', mode: 'task', when: 'later' })`);
  return { w, h };
}

test('Add to backlog: the sheet closes and a pending card stands in the Backlog before the board has answered; the answer swaps it for the real card', async () => {
  const { w, h } = await laterWorld();
  typeInto(form(w).querySelector('textarea.lx-prompt'), 'Fix the login redirect\n\nIt loops on Safari.');
  submit(form(w));
  assert.equal(h.count(), 1, 'the POST is in flight');
  assert.equal(calls(w).pop().body.when, 'later');
  assert.equal(sheet(w).open, false, 'the sheet closed at once');
  const cards = pendingCards(w);
  assert.equal(cards.length, 1, 'one pending card');
  assert.equal(cards[0].parentNode, backlog(w), 'in the Backlog column');
  assert.match(text(cards[0].querySelector('.title')), /Fix the login redirect/);
  assert.match(text(cards[0].querySelector('.meta')), /adding…/);
  assert.equal(cards[0].querySelectorAll('button').length, 0, 'no actions on a card the board has not heard of');
  await h.release({ id: 77, slug: 'fix-the-login-redirect' });
  assert.equal(pendingCards(w).length, 0, 'the pending card is gone');
  const real = page(w).querySelector('.task[data-task="77"]');
  assert.ok(real, 'the real card took its place');
  assert.equal(real.classList.contains('pending'), false);
  assert.equal(page(w).querySelectorAll('.task').filter((c) => /Fix the login redirect/.test(text(c.querySelector('.title')))).length, 1, 'one card, not two');
  assert.deepEqual(toasts(w).pop(), { text: 'added to backlog: Fix the login redirect', kind: 'ok' });
});

test('a refused Add to backlog takes the pending card away and opens the sheet again with what was typed and the board\'s words under it', async () => {
  const { w, h } = await laterWorld();
  typeInto(form(w).querySelector('textarea.lx-prompt'), 'Fix the login redirect');
  typeInto(form(w).querySelector('input[maxlength="120"]') || form(w).querySelectorAll('input').find((i) => i.getAttribute('placeholder') === 'e.g. Fix the login redirect'), 'Login loop');
  submit(form(w));
  assert.equal(pendingCards(w).length, 1);
  assert.equal(sheet(w).open, false);
  await h.refuse('this repo is not a git repository', 400);
  assert.equal(pendingCards(w).length, 0, 'the pending card is gone');
  assert.equal(sheet(w).open, true, 'the sheet is open again');
  const f = form(w);
  assert.equal(f.querySelector('textarea.lx-prompt').value, 'Fix the login redirect', 'the prompt is intact');
  assert.equal(f.querySelectorAll('input').find((i) => i.getAttribute('placeholder') === 'e.g. Fix the login redirect').value || '', 'Login loop', 'and the title');
  assert.match(text(f.querySelector('.form-status')), /this repo is not a git repository/);
  assert.equal(text(f.querySelector('button[type=submit]')).trim(), 'Add to backlog', 'still on Later');
});

test('a refusal while something else is open: no second sheet over it, the words come as a toast naming the task', async () => {
  const { w, h } = await laterWorld();
  typeInto(form(w).querySelector('textarea.lx-prompt'), 'Fix the login redirect');
  submit(form(w));
  w.run(`openSheet({ title: 'Something else', body: el('div', { text: 'busy' }) })`);
  await h.refuse('the board is full', 429);
  assert.equal(pendingCards(w).length, 0);
  assert.equal(text(sheet(w).querySelector('.sheet-title')).includes('Something else'), true, 'the other sheet is left alone');
  assert.match(toasts(w).pop().text, /^not added to the backlog: Fix the login redirect: the board is full/);
  assert.equal(toasts(w).pop().kind, 'bad');
});

test('a poll that already lists the new card replaces the pending one (no twin); a card of the same title that was there before does not', async () => {
  const state = ccbState();
  state.tasks.push({ id: 90, slug: 'fix', title: 'Fix the login redirect', project: 'ccboard', repo: 'ccboard', tmux: '', column: 'backlog', phase: 'backlog', mode: 'worktree', pr_number: null, pr_url: null, pr_state: null });
  const { w, h } = await laterWorld(state);
  typeInto(form(w).querySelector('textarea.lx-prompt'), 'Fix the login redirect');
  submit(form(w));
  assert.equal(pendingCards(w).length, 1, 'card 90 was there before: the pending one still shows');
  w.ctx.__st2 = { ...state, tasks: [...state.tasks, { ...state.tasks[state.tasks.length - 1], id: 91 }] };
  w.run('state = __st2; updateCurrentPage(state)');                  // the poll answers with a second card of that title: it is the new one
  assert.equal(pendingCards(w).length, 0, 'the real card is listed: the stand-in goes');
  assert.ok(page(w).querySelector('.task[data-task="91"]'));
  await h.release({ id: 91 });
  assert.equal(page(w).querySelectorAll('.task').filter((c) => c.getAttribute('data-task') === '91').length, 1);
});

test('Now keeps its sheet open with the button busy (a refusal is shown beside the field it is about); only Later closes at once', async () => {
  const { w } = homeWorld({ state: ccbState() });
  w.location.hash = '#/tasks';
  await tick(); await tick();
  const h = hold(w, '/api/tasks');
  w.run(`openLauncher({ project: 'ccboard', repo: 'ccboard', mode: 'task', when: 'now' })`);
  typeInto(form(w).querySelector('textarea.lx-prompt'), 'Fix it');
  submit(form(w));
  assert.equal(sheet(w).open, true);
  assert.equal(text(form(w).querySelector('button[type=submit]')).trim(), 'Starting…');
  await h.refuse('too many sessions', 429);
  assert.equal(sheet(w).open, true);
  assert.match(text(form(w).querySelector('.form-status')), /too many sessions/);
});

// ---------------------------------------------------------------- the permission mode a sheet starts from

const claudeState = (version) => fixtureState({ agents: { claude: { installed: true, loggedIn: true, version }, codex: { installed: false } } });
const permField = (f) => f.querySelectorAll('.field').find((x) => /^Permissions$/.test(text(x.querySelector('label'))));
const permSelect = (f) => permField(f).querySelector('select');
const optionValues = (sel) => sel.children.map((o) => o.getAttribute('value'));

for (const mode of ['task', 'session']) {
  test(`the ${mode} sheet on a Claude older than 2.1.283 picks ask, shows it selected, offers no auto, and the request carries no mode`, async () => {
    const { w } = homeWorld({ state: claudeState('2.1.0 (Claude Code)') });
    w.run(`openLauncher({ project: 'ccboard', repo: 'ccboard', mode: ${JSON.stringify(mode)}, agent: 'claude' })`);
    const f = form(w);
    const sel = permSelect(f);
    assert.equal(sel.value, 'manual', 'ask is selected');
    assert.equal(optionValues(sel).includes('auto'), false, 'auto is not offered');
    assert.ok(optionValues(sel).includes('manual'));
    if (mode === 'task') typeInto(f.querySelector('textarea.lx-prompt'), 'do it');
    submit(f);
    await tick(); await tick();
    const post = calls(w).filter((c) => c.method === 'POST')[0];
    assert.ok(post, 'a request went out');
    assert.equal(post.body.permission_mode, undefined, 'ask sends no mode, the same as the select says');
    assert.equal(post.body.bypass, undefined);
  });
}

test('a remembered auto on such a Claude is ask too; on a Claude that has auto (2.1.283) the sheet starts on auto and sends it', async () => {
  const old = homeWorld({ state: claudeState('2.1.282') });
  old.w.localStorage.setItem('ccboard:task:ccboard/ccboard', JSON.stringify({ permission_mode: 'auto', when: 'later' }));
  old.w.localStorage.setItem('ccboard:launch:ccboard/ccboard:claude', JSON.stringify({ permission_mode: 'auto' }));
  old.w.run(`openLauncher({ project: 'ccboard', repo: 'ccboard', mode: 'task' })`);
  assert.equal(permSelect(form(old.w)).value, 'manual');
  const ok = homeWorld({ state: claudeState('2.1.283') });
  ok.w.run(`openLauncher({ project: 'ccboard', repo: 'ccboard', mode: 'task' })`);
  const sel = permSelect(form(ok.w));
  assert.equal(sel.value, 'auto');
  assert.ok(optionValues(sel).includes('auto'));
  typeInto(form(ok.w).querySelector('textarea.lx-prompt'), 'do it');
  submit(form(ok.w));
  await tick(); await tick();
  assert.equal(calls(ok.w).filter((c) => c.method === 'POST')[0].body.permission_mode, 'auto');
});

test('the older forms (launchControls: the Start-in-a-lane popover, the dispatch sheet) follow the same rule', () => {
  const lc = (version) => {
    const { w } = homeWorld({ state: claudeState(version) });
    w.run('globalThis.__lc = launchControls({}, PERMS)');
    return w.get('__lc');
  };
  const old = lc('2.1.0');
  assert.equal(old.perm.value, '', 'ask is selected');
  assert.equal(optionValues(old.perm).includes('auto'), false);
  assert.equal(old.read().permission_mode, '');
  const fresh = lc('2.1.290');
  assert.equal(fresh.perm.value, 'auto');
  assert.ok(optionValues(fresh.perm).includes('auto'));
  const unknown = lc(undefined);
  assert.equal(unknown.perm.value, 'auto', 'a version nobody can read never hides an option');
});

test('the board\'s own answer wins: a /api/agents list without auto turns auto off whatever the state version says', async () => {
  const { w } = homeWorld({ state: claudeState('2.1.290') });
  w.run(`__answers['/api/agents'] = { agents: { claude: { name: 'claude', installed: true, options: [{ key: 'model' }], models: ['opus'], efforts: ['low'], permission_modes: ['manual', 'acceptEdits', 'plan'], capabilities: {} } } }`);
  await w.run('launcherSchemaLoad()');
  w.run(`openLauncher({ project: 'ccboard', repo: 'ccboard', mode: 'task' })`);
  assert.equal(permSelect(form(w)).value, 'manual');
});
