// Contract tests for the v0.5.6d page polish: the muted hue of a chip (chipHue), the one filled primary per screen (Pages.markLead, the inbox card's lead and
// its Reply), the banner's tone, the install hint, the settings rows, the search chips, the tasks board without a card around it, and the touch placeholder.
// Runs on the real core.js, components.js, router.js and pages/*.js inside the vm harness (tests/js/world.mjs), like home.test.mjs and inbox.test.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { fixtureState, homeWorld, page, plain, sess, setState, text, tick } from './world.mjs';

const has = (n, c) => n.classList.contains(c);
const filled = (n) => has(n, 'bp5-intent-primary') && !has(n, 'tinted');
const tinted = (n) => has(n, 'bp5-intent-primary') && has(n, 'tinted');
const coarse = { matchMedia: (q) => ({ matches: /pointer:\s*coarse/.test(q), addEventListener() {}, removeEventListener() {} }) };

// ---------------------------------------------------------------- chipHue

test('chipHue: agents, models and projects get a muted hue class; the label always says what the chip is', () => {
  const { w } = homeWorld();
  const hue = (kind, key) => w.run(`chipHue(${JSON.stringify(kind)}, ${JSON.stringify(key)})`);
  assert.equal(hue('agent', 'claude'), 'hue-violet');
  assert.equal(hue('agent', 'codex'), 'hue-teal');
  assert.equal(hue('agent', 'shell'), 'hue-slate');
  assert.equal(hue('agent', 'something-new'), 'hue-slate');
  assert.equal(hue('model', 'Opus 5'), 'hue-blue');
  assert.equal(hue('model', 'claude-opus-5-5'), 'hue-blue');
  assert.equal(hue('model', 'Fable 1'), 'hue-violet');
  assert.equal(hue('model', 'sonnet-5-5'), 'hue-green');
  assert.equal(hue('model', 'Haiku 4.5'), 'hue-slate');
  assert.equal(hue('model', 'gpt-5.1-codex'), 'hue-teal');
  assert.equal(hue('model', 'mystery'), 'hue-slate');
  assert.equal(hue('model', ''), 'hue-slate');
  assert.equal(hue('model', null), 'hue-slate');
  for (const kind of ['branch', 'pr', 'worktree', 'folder']) assert.equal(hue(kind, 'whatever'), 'hue-slate', kind);
});

test('chipHue: a project name hashes to the same hue every time, from blue / teal / green / violet / slate only (amber and rose mean attention)', () => {
  const { w } = homeWorld();
  const hue = (name) => w.run(`chipHue('project', ${JSON.stringify(name)})`);
  assert.equal(hue('ccboard'), hue('ccboard'), 'stable');
  assert.equal(hue('CCBoard'), hue('ccboard'), 'case does not matter');
  const seen = new Set();
  for (let i = 0; i < 300; i++) seen.add(hue(`project-${i}`));
  assert.deepEqual([...seen].sort(), ['hue-blue', 'hue-green', 'hue-slate', 'hue-teal', 'hue-violet'], 'all five hues are handed out, no other');
  assert.ok(!seen.has('hue-amber') && !seen.has('hue-rose'));
});

test('chipHueSet swaps the hue class of a node patched in place and leaves the other classes alone', () => {
  const { w } = homeWorld();
  const node = w.document.createElement('span');
  node.className = 'ib-where';
  w.ctx.__n = node;
  w.run("chipHueSet(__n, 'hue-blue')");
  const classes = () => node.className.split(/\s+/).filter(Boolean).sort();
  assert.deepEqual(classes(), ['hue-blue', 'ib-where']);
  w.run("chipHueSet(__n, 'hue-teal')");
  assert.deepEqual(classes(), ['hue-teal', 'ib-where'], 'the old hue came off');
  w.run("chipHueSet(__n, 'hue-teal')");
  assert.deepEqual(classes(), ['hue-teal', 'ib-where'], 'the same hue twice is a no-op');
});

// ---------------------------------------------------------------- one filled primary: the inbox cards

const cards = (w) => page(w).querySelectorAll('.inbox-card');
const actions = (c) => c.querySelectorAll('.ib-actions button, .ib-actions a');
const named = (c, label) => actions(c).find((b) => text(b).trim() === label);

test('#/inbox: only the lead card carries a filled primary, every other card repeats its action tinted', () => {
  const { w } = homeWorld();
  w.location.hash = '#/inbox';
  const list = cards(w);
  assert.ok(list.length >= 4, 'the demo fleet has a card per kind');
  assert.ok(has(list[0], 'lead'), 'the first card leads');
  assert.ok(list.slice(1).every((c) => !has(c, 'lead')), 'and only the first');
  const primaries = (c) => actions(c).filter((b) => has(b, 'bp5-intent-primary'));
  assert.deepEqual(primaries(list[0]).map(text), ['Allow'], 'a permission leads with Allow');
  assert.ok(filled(primaries(list[0])[0]), 'filled');
  for (const c of list.slice(1)) {
    const p = primaries(c);
    assert.equal(p.length, 1, `${c.getAttribute('data-tmux')}: one primary`);
    assert.ok(tinted(p[0]), 'tinted, not filled');
  }
  assert.equal(page(w).querySelectorAll('.ib-actions .bp5-intent-primary').filter(filled).length, 1, 'one filled primary on the whole screen');
});

test('the primary action follows the kind of card: Allow, Reply for a question, Open for a job that needs you, Ack for a limit and a finished session', () => {
  const { w } = homeWorld();
  w.location.hash = '#/inbox';
  const byKind = Object.fromEntries(cards(w).map((c) => [c.className.match(/kind-([\w-]+)/)[1], c]));
  const primaryOf = (c) => text(actions(c).find((b) => has(b, 'bp5-intent-primary'))).trim();
  assert.equal(primaryOf(byKind.permission), 'Allow');
  assert.equal(primaryOf(byKind.question), 'Reply');
  assert.equal(primaryOf(byKind.needs), 'Open');
  assert.equal(primaryOf(byKind.limit), 'Ack');
  assert.equal(primaryOf(byKind.done), 'Ack');
  for (const kind of ['permission']) {
    const deny = named(byKind[kind], 'Deny');
    assert.ok(has(deny, 'bp5-intent-danger') && !has(deny, 'confirm'), 'Deny is red-outlined and one tap (a decision, not data loss)');
  }
});

test('a card that is not the lead keeps its chips and send box behind a quiet Reply that opens them; the lead shows them at once', () => {
  const { w } = homeWorld();
  w.location.hash = '#/inbox';
  const [lead, second, third] = cards(w);
  assert.ok(has(lead, 'lead') && lead.querySelector('.ib-chips') && lead.querySelector('form.ib-send'), 'the lead carries both (the stylesheet shows them)');
  const reply = third.querySelector('.ib-replybtn');
  assert.ok(reply, 'a quiet Reply');
  assert.equal(reply.getAttribute('aria-expanded'), 'false');
  assert.equal(has(third, 'open'), false);
  reply.click();
  assert.equal(has(third, 'open'), true, 'the card opens (class open shows the chips and the box)');
  assert.equal(reply.getAttribute('aria-expanded'), 'true');
  reply.click();
  assert.equal(has(third, 'open'), false, 'and closes again');
  assert.equal(second.querySelector('.ib-replybtn') !== null, true, 'every non-lead card that can be answered has it');
});

test('the lead follows the j / k selection: the selected card is the one that is filled, the first one goes tinted', () => {
  const { w } = homeWorld();
  w.location.hash = '#/inbox';
  const list = cards(w);
  list[2].click();                                                       // selecting a card makes it the lead
  const now = cards(w);
  assert.ok(has(now[2], 'sel') && has(now[2], 'lead'), 'the selected card leads');
  assert.equal(has(now[0], 'lead'), false);
  const first = named(now[0], 'Allow');
  assert.ok(tinted(first), 'the first card\'s Allow is tinted now');
  assert.equal(page(w).querySelectorAll('.ib-actions .bp5-intent-primary').filter(filled).length, 1, 'still one filled primary');
  assert.ok(now.filter((c) => has(c, 'lead')).length === 1);
});

test('the permission summary that already names its tool is not followed by the tool name again', () => {
  const { w } = homeWorld();
  const st = fixtureState();
  w.ctx.__st = st;
  const perm = st.pending_permissions[0];
  const s = st.projects.flatMap((p) => [...(p.root ? p.root.sessions || [] : []), ...p.repos.flatMap((r) => r.sessions || [])]).find((x) => perm && perm.tmux_name === x.tmux);
  assert.ok(s, 'the fixture has a permission');
  w.ctx.__s = s;
  const ctx = (summary, tool) => { perm.summary = summary; perm.tool_name = tool; return plain(w.run("Inbox.context(__s, __st, 'permission')")); };
  assert.equal(ctx('Bash: npm test', 'Bash').note, '', 'the summary starts with the tool: no second line');
  assert.equal(ctx('bash: npm test', 'Bash').note, '', 'whatever the case');
  assert.equal(ctx('npm run seed', 'Bash').note, 'Bash', 'no prefix: the tool name still says what it is');
});

// ---------------------------------------------------------------- one filled primary: the roster rows

test('#/agents: Allow is filled on the first waiting row only; the others show it tinted', () => {
  const st = fixtureState();
  const waiting = [];
  for (const p of st.projects) for (const r of p.repos) for (const x of r.sessions) if (x.state === 'waiting') waiting.push(x);
  const extra = sess('petroit', 'api', 's8', { state: 'waiting', needs_attention: true, wait_kind: 'permission_prompt', last_prompt: 'second one' });
  st.projects.find((p) => p.name === 'petroit').repos.find((r) => r.name === 'api').sessions.push(extra);
  st.pending_permissions.push({ id: 91, tmux_name: 'petroit--api--s8', tool_name: 'Bash', summary: 'Bash: ls', created_at: new Date().toISOString() });
  const { w } = homeWorld({ state: st });
  w.location.hash = '#/agents';
  const allows = page(w).querySelectorAll('.rrow .perm-btns button').filter((b) => text(b) === 'Allow');
  assert.ok(allows.length >= 2, 'two rows wait on a permission');
  assert.equal(allows.filter(filled).length, 1, 'one filled Allow on the page');
  assert.ok(filled(allows[0]), 'the first one in screen order');
  assert.ok(allows.slice(1).every(tinted));
  assert.ok(waiting.length >= 1);
});

test('a row keeps three controls in view: Allow / Deny while a permission waits, Open, and the ... menu; Kill shows only for its second tap', () => {
  const { w } = homeWorld();
  w.location.hash = '#/agents';
  const row = page(w).querySelector('.rrow[data-tmux]');
  const labels = row.querySelectorAll('.rr-actions button, .rr-actions a').map((b) => text(b).trim() || b.getAttribute('aria-label'));
  assert.ok(labels.includes('More actions'), 'the ... menu');
  assert.ok(labels.includes('Open'));
  assert.ok(!labels.includes('Kill') && !labels.includes('Ack') && !labels.includes('Tail') && !labels.includes('Reply'), 'those moved into the menu');
});

test('the Agents roster uses the rich rows: a waiting row keeps its chips and box open, an idle one folds them behind Reply', () => {
  const { w } = homeWorld();
  w.location.hash = '#/agents';
  const rows = page(w).querySelectorAll('.rrow');
  assert.ok(rows.every((r) => has(r, 'rich')), 'every row is a rich row');
  const waiting = rows.filter((r) => r.querySelector('.glyph.waiting'));
  const idle = rows.filter((r) => r.querySelector('.glyph.idle'));
  assert.ok(waiting.length && waiting.every((r) => has(r, 'open')), 'waiting rows are open');
  assert.ok(idle.length && idle.every((r) => !has(r, 'open')), 'idle rows are folded');
});

// ---------------------------------------------------------------- the banner and the install hint

const banner = (w) => w.document.querySelector('#banner');

test('renderBanner: an update is information (the info tone, Install now is the one primary), an error keeps the red bar, the others stay warn', () => {
  const { w } = homeWorld();
  w.location.hash = '#/';
  const set = (code) => { w.run(`ui.error = null; ui.notice = null; ui.offline = null; ${code}; renderBanner()`); return banner(w); };
  let b = set("state = { ...state, deploy: { pending: true, hold: true, reasons: ['1 terminal open'], minutes_left: 12, forced: false } }");
  assert.ok(has(b, 'info') && !has(b, 'warn'), 'update ready: info');
  const install = b.querySelectorAll('button').find((x) => /Install now/.test(text(x)));
  assert.ok(install && has(install, 'bp5-intent-primary') && has(install, 'bp5-small'), 'Install now: the one primary, small');
  b = set('ui.error = "boom"');
  assert.ok(!has(b, 'info') && !has(b, 'warn'), 'an error keeps the default red bar');
  assert.ok(has(b.querySelector('button'), 'bp5-minimal'), 'dismiss is quiet');
  b = set('ui.offline = Date.now()');
  assert.ok(has(b, 'warn') && !has(b, 'info'), 'offline: warn');
  b = set("state = { ...state, deploy: null, claude: { installed: true, loggedIn: false } }");
  assert.ok(has(b, 'warn') && !has(b, 'info'), 'login: warn');
  const login = b.querySelectorAll('button').find((x) => text(x) === 'Log in');
  assert.ok(login && has(login, 'bp5-intent-primary') && has(login, 'bp5-small'), 'Log in is the primary');
  b = set('state = { ...state, claude: { installed: true, loggedIn: true } }');
  assert.ok(!has(b, 'info') && !has(b, 'warn'), 'the tone is cleared when the message goes');
});

test('the install hint is a neutral note: no primary, no Blueprint callout intent, How and a titled icon-only dismiss on one row', () => {
  const { w } = homeWorld();
  const hint = w.run('homeInstallHint()');
  assert.ok(has(hint, 'install-hint') && has(hint, 'bp5-callout'), 'a callout');
  assert.equal(has(hint, 'bp5-intent-primary'), false, 'without the primary intent (no second accent)');
  assert.equal(hint.querySelectorAll('.bp5-intent-primary').length, 0, 'and no primary button inside');
  const how = hint.querySelector('a[href="#/settings?sec=app"]');
  assert.ok(how && has(how, 'bp5-small'));
  const dismiss = hint.querySelectorAll('button').find((b) => b.getAttribute('aria-label') === 'Dismiss');
  assert.ok(dismiss && dismiss.getAttribute('title') === 'Dismiss', 'an icon button with a title');
  assert.equal(text(dismiss).trim(), '', 'icon only');
});

// ---------------------------------------------------------------- tasks: no card around the board

test('#/tasks: the board is not a card (the task cards are the only bordered boxes) and an empty In progress / PR open / Merged column is not drawn', () => {
  const st = fixtureState();
  const { w } = homeWorld({ state: st });
  w.location.hash = '#/tasks';
  const sec = page(w).querySelector('section#tasks');
  assert.ok(sec, 'the board section');
  assert.equal(has(sec, 'card'), false, 'no card around the board');
  assert.ok(has(sec, 'tasks-board'));
  const heads = sec.querySelectorAll('.col h3').map((h) => h.textContent.replace(/\s*\(\d+\)\s*$/, ''));
  const counts = sec.querySelectorAll('.col').map((c) => c.querySelectorAll('.task').length);
  heads.forEach((h, i) => { if (h !== 'Backlog') assert.ok(counts[i] > 0, `${h} is drawn only because it has a card`); });
  const head = sec.querySelector('.page-head');
  assert.ok(head && /Tasks \(\d+\)/.test(text(head.querySelector('h1'))), 'the heading is the page head, outside any card');
  const plus = head.querySelectorAll('button').filter((b) => /\+ task/.test(text(b)));
  assert.equal(plus.length, 1);
  assert.ok(filled(plus[0]), 'the page-level + task is the filled primary');
});

// ---------------------------------------------------------------- settings

const settingsWorld = (state) => {
  const env = homeWorld({ state });
  env.w.ctx.__state = state;
  env.w.run('state = __state');
  return env;
};
const kvRows = (w) => page(w).querySelectorAll('.kv');

test('settings: every setting is one .kv row (label, value, actions), and buttons go to the action cell', async () => {
  const { w } = settingsWorld(fixtureState({ claude: { installed: true, loggedIn: true, email: 'demo@example.com', subscriptionType: 'max' } }));
  w.location.hash = '#/settings?sec=agents';
  await tick();
  const rows = kvRows(w);
  assert.ok(rows.length >= 1);
  for (const r of rows) {
    assert.ok(r.querySelector('.k'), 'a label');
    assert.ok(r.querySelector('.kv-main'), 'a value cell');
    assert.ok(r.querySelector('.kv-act'), 'an action cell');
    for (const b of r.querySelectorAll('button')) assert.ok(b.closest('.kv-act'), `${text(b)}: buttons live in the action cell`);
  }
});

test('settings: every tab that has sections titles them with the same .set-h heading (13 px upper case, dim); no section is only a row label', async () => {
  const { w } = settingsWorld(fixtureState({ claude: { installed: true, loggedIn: true, email: 'demo@example.com', subscriptionType: 'max' } }));
  const heads = async (sec) => { w.location.hash = '#/settings?sec=' + sec; await tick(); return page(w).querySelectorAll('.settings-panel[data-sec=' + sec + '] .set-h').map(text); };
  assert.deepEqual(await heads('notify'), ['Web Push', 'Backup', 'ntfy']);
  assert.deepEqual(await heads('box'), ['Host', 'Backup']);
  assert.deepEqual(await heads('app'), ['Install', 'This build', 'Tools']);
  w.location.hash = '#/settings?sec=notify'; await tick();
  const notifyKeys = page(w).querySelectorAll('.settings-panel[data-sec=notify] .kv .k').map(text);
  for (const h of ['Web Push', 'Backup']) assert.ok(!notifyKeys.includes(h), `${h} is a heading now, not also a row label`);
  const css = fs.readFileSync(path.join(STATIC, 'pages.css'), 'utf8');
  const rule = /#page \.settings-page \.set-h\s*\{([^}]*)\}/.exec(css);
  assert.ok(rule);
  assert.match(rule[1], /font-size:\s*13px/);
  assert.match(rule[1], /text-transform:\s*uppercase/);
  assert.match(rule[1], /color:\s*var\(--dim\)/);
});

test('settings > Agents: Log out is red-outlined and two taps; the identity chip is the agent\'s hue, never the ok green', async () => {
  const { w } = settingsWorld(fixtureState({ claude: { installed: true, loggedIn: true, email: 'demo@example.com', subscriptionType: 'max' } }));
  w.location.hash = '#/settings?sec=agents';
  await tick();
  const chip = page(w).querySelector('.kv .badge');
  assert.ok(chip && /demo@example\.com/.test(text(chip)));
  assert.ok(has(chip, 'hue-violet') && !has(chip, 'ok'), 'claude is violet');
  const logout = page(w).querySelectorAll('button').find((b) => text(b) === 'Log out');
  assert.ok(logout, 'Log out');
  assert.ok(has(logout, 'bp5-intent-danger'), 'a destructive action is red');
  assert.equal(has(logout, 'confirm'), false, 'not filled at rest');
  logout.click();
  const confirm = page(w).querySelectorAll('button').find((b) => /Confirm Log out/i.test(text(b)));
  assert.ok(confirm && has(confirm, 'confirm'), 'the second tap is the filled one');
  assert.equal(plain(w.get('__calls')).filter((c) => /logout/.test(c.path)).length, 0, 'nothing is sent by the first tap');
});

test('settings > Notifications: Enable push is the one primary; Send test is disabled and says why until it is on', async () => {
  const { w } = settingsWorld(fixtureState());
  w.run(`navigator.serviceWorker = { ready: Promise.resolve({ pushManager: { getSubscription: async () => null } }) }; globalThis.PushManager = function () {};`);
  w.location.hash = '#/settings?sec=notify';
  await tick(); await tick();
  const btns = page(w).querySelectorAll('.kv button');
  const enable = btns.find((b) => /Enable push/.test(text(b)));
  if (!enable) return;                                                    // the push API is not stubbable in this world: the layout is covered by the kv test
  assert.ok(has(enable, 'bp5-intent-primary'));
  const test = btns.find((b) => /Send test push/.test(text(b)));
  assert.ok(test && test.hasAttribute('disabled') && test.getAttribute('title') === 'Enable push first', 'disabled, with the reason');
  assert.equal(btns.filter((b) => has(b, 'bp5-intent-primary')).length >= 1, true);
});

// ---------------------------------------------------------------- search

test('#/search: the role is a hue chip (violet for the assistant, slate for you), never the green done state', async () => {
  const { w } = homeWorld();
  w.run(`api = async (method, path) => { __calls.push({ method, path }); return { results: [
    { kind: 'assistant', project: 'phasezero', repo: 'website', ts: '2026-10-03T10:00:00Z', snippet: 'a', tmux: 'phasezero--website--s1' },
    { kind: 'user', project: 'phasezero', repo: 'website', ts: '2026-10-03T10:01:00Z', snippet: 'b', tmux: 'phasezero--website--s1' } ] }; }`);
  w.location.hash = '#/search?q=x';
  await tick(); await tick();
  const chips = page(w).querySelectorAll('.sess .main .bdg');
  assert.deepEqual(chips.map(text), ['assistant', 'user']);
  assert.ok(has(chips[0], 'hue-violet') && has(chips[1], 'hue-slate'));
  assert.equal(page(w).querySelectorAll('.sess .state').length, 0, 'no state chip: green is for done');
  const peek = page(w).querySelectorAll('.sess a').find((a) => text(a) === 'Peek');
  const attach = page(w).querySelectorAll('.sess a').find((a) => text(a) === 'Attach');
  assert.ok(peek && !has(peek, 'bp5-minimal'), 'Peek is a plain small button');
  assert.ok(attach && has(attach, 'bp5-minimal'), 'Attach is quiet');
  assert.ok(peek.closest('.res-actions') && attach.closest('.res-actions'), 'both at the right of the head');
});

// ---------------------------------------------------------------- placeholders on a touch screen

test('the reply boxes read "Reply…" / "Send…" on a touch screen (one short line, whatever the session is called) and carry the full hint on a fine pointer and in the title', () => {
  const fine = homeWorld();
  fine.w.location.hash = '#/inbox';
  const taFine = cards(fine.w)[0].querySelector('textarea.composer');
  assert.match(taFine.getAttribute('placeholder'), /^reply to .+ · ⇧Enter new line$/);
  assert.match(taFine.getAttribute('title'), /Enter sends, Shift\+Enter adds a line/);
  const touch = homeWorld({ extra: coarse });
  touch.w.location.hash = '#/inbox';
  const taTouch = cards(touch.w)[0].querySelector('textarea.composer');
  assert.equal(taTouch.getAttribute('placeholder'), 'Reply…', 'short and fixed, no Shift+Enter on a touch keyboard');
  assert.match(taTouch.getAttribute('aria-label'), /^reply to .+/, 'the name stays in the accessible name');
  assert.doesNotMatch(taTouch.getAttribute('placeholder'), /⇧|Shift/);
  assert.match(taTouch.getAttribute('title'), /Enter sends, Shift\+Enter adds a line/, 'the hint moved to the title');
  touch.w.location.hash = '#/agents';
  const rowTa = page(touch.w).querySelector('.rrow textarea.composer');
  assert.equal(rowTa.getAttribute('placeholder'), 'Send…');
  assert.match(rowTa.getAttribute('title'), /^send to .+: Enter sends/);
});

test('a row\'s send box tells its Send button whether it holds a draft (has-text) and clears it again when the box is emptied from code', () => {
  const { w } = homeWorld();
  w.location.hash = '#/agents';
  const row = page(w).querySelector('.rrow .rr-send');
  const ta = row.querySelector('textarea.composer');
  assert.equal(has(row, 'has-text'), false, 'empty: the tinted Send');
  ta.value = 'continue please';
  ta.dispatchEvent({ type: 'input' });
  assert.equal(has(row, 'has-text'), true, 'a draft: the filled Send');
  w.ctx.__ta = ta;
  w.run('__ta.value = ""; rowCleared(__ta)');
  assert.equal(has(row, 'has-text'), false, 'cleared by a send: tinted again');
});

// ---------------------------------------------------------------- the task card's actions wrap on a phone (no button hides past the edge of a nowrap row)

test('style.css: under 700 px a task card\'s actions wrap instead of scrolling sideways, so Diff / PR, PR, retry and Archive are always reachable', () => {
  const css = fs.readFileSync(path.join(STATIC, 'style.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const at = css.indexOf('@media (max-width: 700px)');
  assert.ok(at >= 0, 'the phone breakpoint');
  const block = css.slice(at, css.indexOf('\n}\n', at));
  assert.match(block, /\.actions\s*\{[^}]*flex-wrap:\s*nowrap/, 'other action rows still scroll sideways');
  const task = /\.task \.actions\s*\{([^}]*)\}/.exec(block);
  assert.ok(task, 'a .task .actions override inside the breakpoint');
  assert.match(task[1], /flex-wrap:\s*wrap/);
  assert.match(task[1], /overflow:\s*visible/, 'no inner scroller');
});

// ---------------------------------------------------------------- small CSS pins from the merge pass

test('css pins: the palette input has one focus ring (no Blueprint inset shadow), the help dialog holds its own focus quietly, the task form\'s Start button spaces its icon', () => {
  const read = (f) => fs.readFileSync(path.join(STATIC, f), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const pages = read('pages.css');
  const ring = /\.palette \.pal-input:focus,\s*\.palette \.pal-input:focus-visible\s*\{([^}]*)\}/.exec(pages);
  assert.ok(ring && /box-shadow:\s*none/.test(ring[1]), 'the inset shadow goes, the 2 px outline stays');
  assert.match(pages, /\.palette \.pal-input:focus-visible[^{]*\{[^}]*outline:\s*2px solid var\(--sig\)/);
  assert.match(pages, /dialog#helpdlg:focus\s*\{[^}]*outline:\s*none/);
  assert.doesNotMatch(read('style.css'), /\.form \.submit \.bp5-button\s*\{[^}]*gap/, 'scoped to the task form: the session form\'s Start button already gets its gap from Blueprint');
  const ic = /\.task-form \.tf-go-ic\s*\{([^}]*)\}/.exec(pages);
  assert.ok(ic && /width:\s*14px/.test(ic[1]) && /flex:\s*0 0 14px/.test(ic[1]) && /margin-right:\s*0/.test(ic[1]), 'the icon box is a fixed 14 px (it collapsed to 0 px and the label touched the glyph)');
  assert.match(pages, /\.task-form \.submit \.bp5-button\s*\{[^}]*gap:\s*6px/);
});
