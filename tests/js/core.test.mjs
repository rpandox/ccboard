// Contract tests for the glyph helpers in app/static/core.js (stateGlyph, agentGlyph, svg) and the way components.js
// uses them (stateBadge, sessionRow). Run in the vm harness: nodes are the harness stubs (className, attrs, children, textContent).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { STATIC, makeWorld, plain } from './harness.mjs';

function coreWorld() {
  const w = makeWorld();
  w.load('core.js');
  return w;
}

function componentsWorld() {
  const w = coreWorld();
  w.load('components.js');
  return w;
}

/** All the text under a stub node, in document order (text nodes and 'text' attrs both end up in textContent). */
function flatText(n) {
  if (n.children && n.children.length) return n.children.map(flatText).join('');
  return n.textContent || '';
}

/** Every stub element under n (depth first) whose className has the given class. */
function withClass(n, cls, out = []) {
  if (typeof n.className === 'string' && n.className.split(/\s+/).includes(cls)) out.push(n);
  for (const c of n.children || []) withClass(c, cls, out);
  return out;
}

test('STATE_GLYPH and AGENT_GLYPH carry the planned glyphs', () => {
  const { get } = coreWorld();
  assert.deepEqual(plain(get('STATE_GLYPH')), { working: '✽', waiting: '✻', idle: '∙', done: '✓', errored: '✕', ended: '○', unknown: '·' });
  assert.deepEqual(plain(get('AGENT_GLYPH')), { claude: '◆', codex: '◇', shell: '▸' });
});

test('every state glyph has a distinct character and a text label', () => {
  const { get } = coreWorld();
  const glyphs = plain(get('STATE_GLYPH'));
  const labels = plain(get('GLYPH_LABEL'));
  assert.deepEqual(Object.keys(labels).sort(), Object.keys(glyphs).sort());
  assert.equal(new Set(Object.values(glyphs)).size, Object.keys(glyphs).length);
  assert.ok(Object.values(labels).every((l) => typeof l === 'string' && l.length > 0), 'state is never colour-only: every glyph names itself');
  assert.equal(labels.waiting, 'needs you');
  assert.equal(labels.errored, 'error');
});

test('stateGlyph(working): a fixed-width .glyph span with the glyph, aria-label and title', () => {
  const { get } = coreWorld();
  const n = get('stateGlyph')('working');
  assert.equal(n.tagName, 'SPAN');
  assert.ok(n.className.includes('glyph working'), n.className);
  assert.equal(n.textContent, '✽');
  assert.equal(n.getAttribute('aria-label'), 'working');
  assert.equal(n.getAttribute('title'), 'working');
  assert.equal(n.getAttribute('role'), 'img');
});

test('stateGlyph: each known state gets its own class, glyph and label', () => {
  const { get } = coreWorld();
  const expected = { working: ['✽', 'working'], waiting: ['✻', 'needs you'], idle: ['∙', 'idle'], done: ['✓', 'done'], errored: ['✕', 'error'], ended: ['○', 'ended'], unknown: ['·', 'unknown'] };
  for (const [state, [glyph, label]] of Object.entries(expected)) {
    const n = get('stateGlyph')(state);
    assert.equal(n.className, `glyph ${state}`);
    assert.equal(n.textContent, glyph);
    assert.equal(n.getAttribute('aria-label'), label);
  }
});

test('stateGlyph: an unknown state maps to "unknown" and never reaches the class list', () => {
  const { get } = coreWorld();
  for (const state of ['bogus', '', undefined, null, 'constructor', '__proto__', 'toString', 'working extra']) {
    const n = get('stateGlyph')(state);
    assert.equal(n.className, 'glyph unknown', String(state));
    assert.equal(n.textContent, '·');
    assert.equal(n.getAttribute('aria-label'), 'unknown');
  }
});

test('agentGlyph: claude, codex and shell glyphs with .glyph.agent', () => {
  const { get } = coreWorld();
  const agentGlyph = get('agentGlyph');
  assert.equal(agentGlyph('codex').textContent, '◇');
  assert.equal(agentGlyph('claude').textContent, '◆');
  assert.equal(agentGlyph('shell').textContent, '▸');
  const n = agentGlyph('codex');
  assert.equal(n.tagName, 'SPAN');
  assert.equal(n.className, 'glyph agent');
  assert.equal(n.getAttribute('aria-label'), 'codex');
  assert.equal(n.getAttribute('title'), 'codex');
});

test('agentGlyph: a missing or unknown agent falls back to the shell glyph', () => {
  const { get } = coreWorld();
  const agentGlyph = get('agentGlyph');
  assert.equal(agentGlyph(undefined).textContent, '▸');
  assert.equal(agentGlyph(undefined).getAttribute('aria-label'), 'shell');
  assert.equal(agentGlyph('constructor').textContent, '▸');
  const other = agentGlyph('gemini');
  assert.equal(other.textContent, '▸');
  assert.equal(other.getAttribute('aria-label'), 'gemini');          // the label still names the real agent
  assert.equal(other.className, 'glyph agent');
});

test('svg(): namespaced element, every attribute through setAttribute, text and children appended', () => {
  const w = coreWorld();
  const made = [];
  const orig = w.document.createElementNS;
  w.document.createElementNS = (ns, tag) => { made.push([ns, tag]); return orig(ns, tag); };
  const svg = w.get('svg');
  let clicked = 0;
  const circle = svg('circle', { cx: 5, cy: 5, r: 4 });
  const title = svg('title', { text: 'a chart' });
  const root = svg('svg', { class: 'spark', viewBox: '0 0 10 10', hidden: true, skipped: null, gone: false, nothing: undefined, onclick: () => { clicked += 1; } },
    circle, null, [title, 'label']);
  assert.deepEqual(made, [['http://www.w3.org/2000/svg', 'circle'], ['http://www.w3.org/2000/svg', 'title'], ['http://www.w3.org/2000/svg', 'svg']]);
  assert.equal(root.tagName, 'SVG');
  assert.equal(root.getAttribute('class'), 'spark');                 // class goes through setAttribute (SVG className is read-only)
  assert.equal(root.getAttribute('viewBox'), '0 0 10 10');
  assert.equal(root.getAttribute('hidden'), '');
  for (const k of ['skipped', 'gone', 'nothing']) assert.equal(root.getAttribute(k), null, k);
  assert.equal(circle.getAttribute('cx'), '5');
  assert.equal(title.textContent, 'a chart');
  assert.equal(root.children.length, 3);
  assert.equal(root.children[0], circle);
  assert.equal(root.children[1], title);
  assert.equal(root.children[2].textContent, 'label');               // strings become text nodes
  root.dispatch('click');
  assert.equal(clicked, 1);
  const bare = svg('g');
  assert.equal(bare.children.length, 0);
});

test('stateBadge: glyph, space, then the text label (and the age); unknown state renders nothing', () => {
  const { get } = componentsWorld();
  const stateBadge = get('stateBadge');
  const b = stateBadge({ state: 'waiting', needs_attention: true, last_event: 'permission_prompt' });
  assert.equal(b.className, 'state waiting attn');
  assert.equal(b.getAttribute('title'), 'permission_prompt');
  assert.equal(b.children.length, 3);
  assert.equal(b.children[0].className, 'glyph waiting');
  assert.equal(b.children[0].textContent, '✻');
  assert.equal(b.children[0].getAttribute('aria-hidden'), 'true');   // the text label beside it is what is announced
  assert.equal(b.children[1].textContent, ' ');
  assert.equal(b.children[2].textContent, 'needs you');
  assert.equal(flatText(b), '✻ needs you');
  const aged = stateBadge({ state: 'working', state_at: new Date(Date.now() - 120000).toISOString() });
  assert.equal(aged.className, 'state working');
  assert.match(flatText(aged), /^✽ working 2m$/);
  assert.equal(stateBadge({ state: 'unknown' }), null);
  assert.equal(stateBadge({}), null);
});

function session(extra = {}) {
  return { tmux: 'shop--api--s1', name: 's1', launcher: 'claude', command: 'claude --model opus', created: Date.now() / 1000 - 300, attached: 1,
           state: 'idle', stats: { model: 'opus', context_pct: 12.4, cost_usd: 0.5 }, ...extra };
}

test('sessionRow: agent glyph before the name, inferred from the launcher until sessions carry an agent', () => {
  const { get } = componentsWorld();
  const sessionRow = get('sessionRow');
  const lead = (s) => { const main = sessionRow(s).children[0]; return [main.children[0], main.children[1]]; };
  const cases = [[{ launcher: 'claude' }, '◆'], [{ launcher: 'resume' }, '◆'], [{ launcher: 'continue' }, '◆'], [{ launcher: 'task' }, '◆'],
                 [{ launcher: 'external' }, '◆'], [{ launcher: 'shell' }, '▸'], [{ launcher: 'clone' }, '▸'],
                 [{ launcher: 'claude', agent: 'codex' }, '◇'], [{ launcher: 'shell', agent: 'claude' }, '◆']];
  for (const [extra, glyph] of cases) {
    const [first, second] = lead(session(extra));
    assert.equal(first.className, 'glyph agent', JSON.stringify(extra));
    assert.equal(first.textContent, glyph, JSON.stringify(extra));
    assert.equal(second.className, 'name');
    assert.equal(second.textContent, 's1');
  }
});

test('sessionRow: meta text is unchanged, stats and the command carry the mono class', () => {
  const { get } = componentsWorld();
  const row = get('sessionRow')(session());
  const main = row.children[0];
  const meta = withClass(main, 'meta')[0];
  assert.equal(flatText(meta), 'claude · opus · ctx 12% · $0.50 · 5m · 1 attached');
  const mono = withClass(main, 'mono');
  assert.deepEqual(mono.map((n) => n.tagName), ['SPAN', 'CODE']);
  assert.equal(mono[0].textContent, 'opus · ctx 12% · $0.50');
  assert.equal(mono[1].textContent, 'claude --model opus');
  assert.equal(row.getAttribute('data-tmux'), 'shop--api--s1');
  const bare = get('sessionRow')(session({ stats: null, launcher: '' }));
  assert.equal(flatText(withClass(bare, 'meta')[0]), '5m · 1 attached');
  assert.equal(withClass(bare, 'mono').length, 1);                    // only the <code>: no empty stats span
});

// ---------------------------------------------------------------- api(): what a failed call hands to its caller

/** The Error api() rejects with for a fake fetch answer. json: the parsed body, or null for a body that is not JSON. */
async function apiFailure({ status, statusText, json }) {
  const w = makeWorld({ fetch: async () => ({ ok: false, status, statusText, json: async () => { if (json === null) throw new SyntaxError('not json'); return json; } }) });
  w.load('core.js');
  w.run('globalThis.__err = null; api("POST", "/api/tasks/1/dispatch", { session: "x" }).catch((e) => { __err = e; });');
  await new Promise((r) => setImmediate(r));
  return { isError: w.run('__err instanceof Error'), message: w.run('__err && __err.message'), status: w.run('__err && __err.status'), body: plain(w.get('__err').body) };
}

test('api() rejects with an Error that keeps the status and the parsed body next to the message (a 409 says why in body.mismatch / body.state)', async () => {
  const body = { error: 'this session works in another repo', mismatch: { task: 'shop/api', session: 'shop/web' } };
  const e = await apiFailure({ status: 409, statusText: 'Conflict', json: body });
  assert.equal(e.isError, true);
  assert.equal(e.message, 'this session works in another repo', 'the message is still the server\'s error text');
  assert.equal(e.status, 409);
  assert.deepEqual(e.body, body, 'the whole body, so a caller can branch on its fields instead of on the words');
  const busy = await apiFailure({ status: 409, statusText: 'Conflict', json: { error: 'the session is waiting on a prompt', state: 'waiting', wait_kind: 'permission_prompt' } });
  assert.deepEqual(busy.body, { error: 'the session is waiting on a prompt', state: 'waiting', wait_kind: 'permission_prompt' });
});

test('api() on an answer that is not JSON (a proxy 502 page): the message is the status line, the body is null, the status is kept', async () => {
  const e = await apiFailure({ status: 502, statusText: 'Bad Gateway', json: null });
  assert.equal(e.message, '502 Bad Gateway');
  assert.equal(e.status, 502);
  assert.equal(e.body, null);
});

// ---------------------------------------------------------------- el(): the button hierarchy classes (v0.5.6d)
// The polish pass is CSS only (style.css, tokens.css, shell.css): `primary` and `danger` still become the Blueprint intents there, and the plain
// marker classes the CSS keys on (tinted, confirm, has-text, hue-*, warn, info) are left in the DOM verbatim: no SEMANTIC map entry is needed.

/** Every class of a built node. The harness keeps the authored className string and the classList.add() set apart (a real DOM merges them), so read both. */
function classesOf(n) {
  const out = new Set(String(n.className || '').split(/\s+/).filter(Boolean));
  for (const c of ['bp5-button', 'bp5-minimal', 'bp5-small', 'bp5-tag', 'bp5-round', 'bp5-intent-primary', 'bp5-intent-danger', 'bp5-intent-success', 'bp5-intent-warning', 'bp5-input', 'bp5-text-area', 'bp5-card']) {
    if (n.classList.contains(c)) out.add(c);
  }
  return [...out].sort();
}

function assertClasses(n, want, label) {
  const have = classesOf(n);
  for (const c of want) assert.ok(have.includes(c), `${label || ''} ${c} missing from ${have.join(' ')}`);
}

test('el(button): primary and danger map to the Blueprint intents, small and minimal to their classes, and the marker classes survive', () => {
  const { get } = coreWorld();
  const el = get('el');
  const tinted = el('button', { class: 'small primary tinted', type: 'button' }, 'Allow');
  assertClasses(tinted, ['bp5-button', 'bp5-intent-primary', 'bp5-small', 'tinted']);
  assert.ok(!classesOf(tinted).includes('bp5-minimal'), 'a primary is never minimal');
  assertClasses(el('button', { class: 'danger confirm' }, 'Confirm Delete'), ['bp5-button', 'bp5-intent-danger', 'confirm']);
  const quiet = el('button', { class: 'danger minimal' }, 'Kill');
  assertClasses(quiet, ['bp5-button', 'bp5-intent-danger', 'bp5-minimal']);
  assert.ok(!classesOf(quiet).includes('confirm'), 'the first tap is not the armed state');
  assertClasses(el('a', { class: 'btn small primary', href: '#/' }, 'Open'), ['bp5-button', 'bp5-intent-primary', 'bp5-small']);
});

test('el(button): icon and minimal both become bp5-minimal; a plain button gets no intent and no minimal', () => {
  const { get } = coreWorld();
  const el = get('el');
  assertClasses(el('button', { class: 'icon' }), ['bp5-button', 'bp5-minimal']);
  assertClasses(el('button', { class: 'minimal small' }, 'Reply'), ['bp5-button', 'bp5-minimal', 'bp5-small']);
  const plain = classesOf(el('button', { class: 'small' }, 'Open')).filter((c) => c.startsWith('bp5-'));
  assert.deepEqual(plain, ['bp5-button', 'bp5-small']);
});

test('el(button): warn, ok and bad are not button intents (a warn button is the CSS class `warn`; only primary and danger map)', () => {
  const { get } = coreWorld();
  const el = get('el');
  for (const word of ['warn', 'ok', 'bad']) {
    const b = classesOf(el('button', { class: 'small ' + word }, 'Fix CI'));
    assert.ok(!b.some((c) => c.startsWith('bp5-intent-')), `${word} -> ${b.join(' ')}`);
    assert.ok(b.includes(word), `${word} keeps its own class`);
  }
});

test('el(): hue-*, has-text and info are plain classes: nothing is added or dropped, even on a badge or a form', () => {
  const { get } = coreWorld();
  const el = get('el');
  assert.deepEqual(classesOf(el('span', { class: 'bdg hue-violet', text: 'claude' })), ['bdg', 'hue-violet']);
  const state = el('span', { class: 'state working hue-slate' }, 'x');   // a state chip is still a Blueprint tag with the working intent
  assertClasses(state, ['bp5-tag', 'bp5-minimal', 'bp5-round', 'bp5-intent-primary', 'state', 'working', 'hue-slate']);
  assert.deepEqual(classesOf(el('form', { class: 'ib-send has-text' })), ['has-text', 'ib-send']);
  assert.deepEqual(classesOf(el('div', { id: 'banner', class: 'info' })), ['info']);
});

// ---------------------------------------------------------------- demo mode: the usage summary and the accounts' reset times ride along with the state

/** A ?demo=1 world whose fetch serves /static/demo/<name>.json from disk, the way the board does. */
function demoWorld() {
  const w = makeWorld({
    fetch: async (url) => {
      const m = /^\/static\/demo\/(\w+)\.json$/.exec(String(url));
      if (!m) throw new Error(`demo mode asked for ${url}`);
      const body = fs.readFileSync(path.join(STATIC, 'demo', `${m[1]}.json`), 'utf8');
      return { ok: true, status: 200, statusText: 'OK', json: async () => JSON.parse(body) };
    },
  });
  w.location.search = '?demo=1';
  w.load('core.js');
  return w;
}

test('demoRebase shifts the per-account reset times of state.accounts.list (resets_5h, resets_7d) like every other epoch', () => {
  const w = coreWorld();
  assert.ok(w.run("EPOCH_KEYS.has('resets_5h') && EPOCH_KEYS.has('resets_7d') && EPOCH_KEYS.has('resets_at')"));
  const epoch = Math.floor(Date.now() / 1000) - 3600;
  w.ctx.__d = { demo: { epoch }, accounts: { list: [{ key: 'a', rl_5h: 42, resets_5h: epoch + 600, resets_7d: epoch + 86400, resets_never: epoch }] } };
  const out = plain(w.run('demoRebase(__d)'));
  assert.ok(Math.abs(out.accounts.list[0].resets_5h - (epoch + 600 + 3600)) <= 1, 'one hour on');
  assert.ok(Math.abs(out.accounts.list[0].resets_7d - (epoch + 86400 + 3600)) <= 1);
  assert.equal(out.accounts.list[0].resets_never, epoch, 'a key that is not a reset time stays');
  assert.equal(out.accounts.list[0].rl_5h, 42, 'and so do the percentages');
});

test('demoApi rebases the usage summary with the state\'s own epoch: the accounts\' reset times, the rate limits and the ISO stamps move to now, and agree with the state', async () => {
  const w = demoWorld();
  const st = plain(await w.run("api('GET', '/api/state')"));
  const sum = plain(await w.run("api('GET', '/api/usage/summary?days=7')"));
  const fx = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'usage_summary.json'), 'utf8'));
  const state = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8'));
  assert.equal(fx.demo.epoch, state.demo.epoch, 'one fixture clock for the state and the summary');
  assert.deepEqual(sum.demo, fx.demo, 'the clock rides along unshifted');
  const now = Date.now() / 1000;
  const [demo, work] = sum.accounts;
  assert.ok(demo.rl_5h.resets_at > now && demo.rl_7d.resets_at > now, 'the account in use has its 5H and 7D windows ahead of it: a countdown, not "rolled over"');
  const near = (a, b) => Math.abs(a - b) <= 2;
  assert.ok(near(demo.rl_5h.resets_at, st.accounts.list[0].resets_5h), 'Accounts row 5H = state list');
  assert.ok(near(demo.rl_7d.resets_at, st.accounts.list[0].resets_7d), 'Accounts row 7D = state list');
  assert.ok(near(demo.rl_5h.resets_at, st.usage.value.five_hour.resets_at), 'and the topbar pill / Limits gauge (state.usage)');
  assert.ok(near(demo.rl_7d.resets_at, st.usage.value.seven_day.resets_at));
  assert.ok(near(work.rl_5h.resets_at, st.accounts.list[1].resets_5h) && near(work.rl_7d.resets_at, st.accounts.list[1].resets_7d), 'the other account too');
  assert.ok(work.rl_5h.resets_at < now, 'the Work 5-hour window of the fixture had reset before it was captured: still behind us');
  assert.ok(near(sum.rate_limits.claude.rl_5h.meta.resets_at, st.usage.value.five_hour.resets_at));
  assert.ok(near(Date.parse(sum.generated_at) / 1000, now), 'generated_at is now');
  assert.ok(near(Date.parse(demo.rl_5h.at) / 1000 - Date.parse(fx.accounts[0].rl_5h.at) / 1000, now - fx.demo.epoch), 'the readings\' own stamps moved by the same amount');
  assert.ok(sum.episodes.every((e, i) => near(Date.parse(e.at) / 1000 - Date.parse(fx.episodes[i].at) / 1000, now - fx.demo.epoch)), 'the episodes keep their age');
  assert.deepEqual(sum.daily, fx.daily, 'the daily buckets (local dates, no clock) are untouched');
  assert.deepEqual(sum.heatmap, fx.heatmap);
  assert.deepEqual(sum.top_sessions, fx.top_sessions);
  assert.deepEqual(sum.total, fx.total);
});

// ---------------------------------------------------------------- demo mode: the make-believe usage refresh (v0.5.17f, part 2)

function demoSessions(st) {
  const out = [];
  for (const p of st.projects) { if (p.root) out.push(...p.root.sessions); for (const r of p.repos) out.push(...r.sessions); out.push(...p.orphan_sessions); }
  return out;
}

test('demo refresh: a tap is answered at once, the reading holds still for 3 s and then moves to that moment, from Claude Code\'s cache', async () => {
  const w = demoWorld();
  const before = plain(await w.run("api('GET', '/api/state')"));
  const at0 = Date.parse(before.usage.at) / 1000;
  assert.ok(Math.abs(at0 - Date.now() / 1000) < 60, 'the fixture reading is seconds old, like the rest of the demo');
  assert.equal(before.usage.value.source, undefined);
  const r = plain(await w.run("api('POST', '/api/usage/refresh', {})"));
  assert.equal(r.ok, true);
  assert.match(r.session, /^[\w-]+--[\w-]+--[\w-]+$/);
  assert.ok(Math.abs(Date.parse(r.started_at) / 1000 - Date.now() / 1000) < 5);
  const held = plain(await w.run("api('GET', '/api/state')"));
  assert.equal(Date.parse(held.usage.at) / 1000, at0, 'held at the reading the tap found: "refreshing…" stays visible');
  assert.equal(held.usage.value.source, undefined);
  w.run('demoRefresh.tap -= 5');                                           // the 3 s are over
  const after = plain(await w.run("api('GET', '/api/state')"));
  assert.ok(Date.parse(after.usage.at) / 1000 > at0, 'a newer reading');
  assert.equal(after.usage.value.source, 'cache');
  assert.equal(after.usage.value.five_hour.used_percentage, before.usage.value.five_hour.used_percentage, 'the numbers themselves are the fixture\'s');
  assert.ok(after.projects.length && demoSessions(after).some((x) => x.agent === 'claude'), 'the sessions are untouched');
});

test('demo refresh: ?refresh=none leaves no Claude session (the Codex one stays) and refuses a tap with the real 409 text', async () => {
  const w = demoWorld();
  w.location.search = '?demo=1&refresh=none';
  const st = plain(await w.run("api('GET', '/api/state')"));
  const rows = demoSessions(st);
  assert.ok(rows.length > 0 && rows.every((x) => x.agent !== 'claude'), rows.map((x) => x.agent).join());
  assert.ok(rows.some((x) => x.agent === 'codex'));
  await assert.rejects(w.run("api('POST', '/api/usage/refresh', {})"), (e) => e.status === 409 && e.message === 'no Claude session is at its prompt; start one to refresh');
});

test('demo refresh: ?refresh=stale makes the newest reading 10 minutes old; ?refresh=stuck never lets a tap move it', async () => {
  const w = demoWorld();
  w.location.search = '?demo=1&refresh=stale';
  const st = plain(await w.run("api('GET', '/api/state')"));
  assert.ok(Math.abs(Date.now() / 1000 - Date.parse(st.usage.at) / 1000 - 600) < 5);
  const w2 = demoWorld();
  w2.location.search = '?demo=1&refresh=stuck';
  const a = plain(await w2.run("api('GET', '/api/state')"));
  await w2.run("api('POST', '/api/usage/refresh', {})");
  w2.run('demoRefresh.tap -= 100');
  const b = plain(await w2.run("api('GET', '/api/state')"));
  assert.equal(b.usage.at, a.usage.at, 'a minute and a half later it is still the reading the tap found');
  assert.equal(b.usage.value.source, undefined);
});

// ---------------------------------------------------------------- tabs(): the tab list scrolls sideways, the selected tab is brought into view

test('tabs(): once the page lays the list out the selected tab is scrolled into its sideways-scrolling list (six Settings tabs at 390 px); without requestAnimationFrame or layout nothing happens and nothing throws', () => {
  const w = componentsWorld();
  const rect = (l, r) => ({ left: l, right: r, top: 0, bottom: 44, width: r - l, height: 44 });
  const LIST = rect(0, 390);
  const TABS = { notify: rect(-30, 60), app: rect(330, 450) };
  const made = w.document.createElement;
  w.document.createElement = (tag) => { const n = made(tag); n.getBoundingClientRect = () => (n.className === 'tablist' ? LIST : (TABS[n.getAttribute('data-tab')] || rect(100, 160))); return n; };
  const queued = [];
  w.ctx.requestAnimationFrame = (f) => { queued.push(f); };
  const items = ['notify', 'nodes', 'box', 'agents', 'accounts', 'app'].map((id) => ({ id, label: id }));
  const t = w.get('tabs')(items, 'app', () => {});
  const list = t.root.children[0];
  list.scrollLeft = 0;
  assert.equal(queued.length, 1, 'the first paint waits for the layout (the list is not in the page yet)');
  queued.shift()();
  assert.equal(list.scrollLeft, 60, 'the sixth tab ends 60 px beyond the list: scroll that far');
  t.set('notify');
  queued.shift()();
  assert.equal(list.scrollLeft, 30, 'the first tab starts 30 px before the list: scroll back');
  t.set('nodes');
  queued.shift()();
  assert.equal(list.scrollLeft, 30, 'a tab that is already in view moves nothing');
  // no rAF: nothing is queued and nothing throws
  delete w.ctx.requestAnimationFrame;
  w.ctx.requestAnimationFrame = undefined;
  assert.doesNotThrow(() => w.get('tabs')(items, 'box', () => {}));
  // no layout: getBoundingClientRect missing or throwing
  w.ctx.requestAnimationFrame = (f) => f();
  w.document.createElement = made;
  assert.doesNotThrow(() => w.get('tabs')(items, 'app', () => {}));
});
