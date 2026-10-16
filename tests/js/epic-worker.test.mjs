// The epic worker's panel (issue #157, app/static/nodes-worker.js): Settings > Nodes > Worker (this board: the state in words with a glyph, the allowlists, the caps, the last 20 decisions, Stop worker
// and Start worker) and the same state on a paired node's page (read only, with its age). Real core.js, components.js, router.js, nodes.js, pages/settings.js, nodes-hub.js and pages/node.js on
// minidom's DOM; api() is the recorder of world.mjs / hubworld.mjs. Nothing is fetched except the demo's own fixture.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { boardState, hubWorld, iso, rec, answer as hubAnswer, poll as hubPoll, page as hubPage, tick as hubTick } from './hubworld.mjs';
import { calls, fakeState, homeWorld, page, plain, tick, text } from './world.mjs';

const SRC = fs.readFileSync(path.join(STATIC, 'nodes-worker.js'), 'utf8');
const FX = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'worker.json'), 'utf8'));
const CARD = { app: 'ccboard', api: 1, node_id: 'ts:nDEMO1CNTRL', name: 'box', os: { system: 'Linux', release: '6.8.0-demo', tailscale_os: 'linux' }, lanes: { cap: 3, running: 1, free: 2 }, handle: 'local', now: '2026-10-03T03:00:00.000+00:00' };
const AGENTS = { claude: { installed: true, version: '2.1.288', loggedIn: true } };

const dec = (n, kind = 'skip', reason = 'window', issue = 'example/website#15', over = {}) => ({ at: new Date(Date.now() - n * 60000).toISOString(), kind, issue, reason, text: `text of ${reason}`, ...over });
const view = (over = {}) => ({
  on: true, state: 'idle', reason: null, text: 'Idle: no ready issue in the listed epics.', until: null, issues: [], repos: ['example/website'], authors: ['alice'], poll_s: 90, running: true,
  caps: { hour: { used: 2, max: 6, clears_at: null }, day: { used: 5, max: 30, clears_at: null } }, cycle_at: iso(10), next_at: new Date(Date.now() + 60000).toISOString(), at: new Date().toISOString(), stopped_at: null, skipped: {},
  decisions: [dec(1), dec(2, 'start', 'started', 'example/website#12')], ...over,
});

/** Settings, the Nodes tab, on a board whose poll carries `epic_worker` (or not). `load` false leaves nodes-worker.js out, as before the lazy bundle arrived. */
function settingsWorld({ worker, load = true, answers = {}, demo = '' } = {}) {
  const node = worker === undefined ? { ...CARD } : { ...CARD, epic_worker: worker };
  const { w } = homeWorld({ state: fakeState({ node, node_name: 'box', agents: AGENTS }) });
  w.ctx.__answers['/api/nodes/discover'] = { at: new Date().toISOString(), tailscale: { ok: true, reason: null, variant: 'cli' }, rows: [] };
  Object.assign(w.ctx.__answers, answers);
  if (demo) {
    w.location.search = `?demo=1&worker=${demo}`;
    w.run('demoFlag = true');
    w.ctx.__fix = fs.readFileSync(path.join(STATIC, 'demo', 'worker.json'), 'utf8');
    w.run("globalThis.fetch = async (u) => ({ ok: true, status: 200, json: async () => JSON.parse(__fix) });");
  }
  if (load) w.load('nodes-worker.js');
  w.location.hash = '#/settings?sec=nodes';
  return w;
}
const panel = (w) => page(w).querySelector('.settings-panel[data-sec=nodes]');
const worker = (w) => panel(w).querySelector('.ew');
const heads = (w) => panel(w).querySelectorAll('h2.set-h').map(text);
const word = (w) => text(worker(w).querySelector('.ew-word'));
const rowOf = (w, k) => worker(w).querySelectorAll('.ew-row').find((r) => text(r.querySelector('.ew-k')) === k);
const btn = (w, label) => worker(w).querySelectorAll('button').find((b) => text(b) === label);
const hidden = (n) => !n || n.classList.contains('hidden');

// ---------------------------------------------------------------- the words of each state

test('every state is a glyph and words, and the words carry the issue, the cap or the time', async () => {
  const w = settingsWorld({ worker: view() });
  await tick();
  const E = (v) => plain(w.run(`(() => { const h = EpicWorker.headline(EpicWorker.clean(${JSON.stringify(v)})); return [h.glyph, h.tone, h.word]; })()`));
  const until = new Date(Date.now() + 40 * 60000).toISOString();
  assert.deepEqual(E(view({ state: 'working', issues: ['example/website#12', 'example/website#13'] })), ['✽', 'sig', 'Working on example/website#12, example/website#13']);
  assert.deepEqual(E(view({ state: 'idle' })), ['∙', '', 'Idle, nothing ready']);
  assert.deepEqual(E(view({ state: 'stopped', reason: 'stopped' })), ['○', '', 'Stopped by a person']);
  assert.deepEqual(E(view({ state: 'refused', reason: 'repos_empty' })), ['✕', 'bad', 'Refused to start']);
  assert.match(E(view({ state: 'waiting', reason: 'window', until }))[2], /^Waiting for a window until \d\d:\d\d$/, 'waiting says what for and until when, never a bare spinner');
  assert.match(E(view({ state: 'waiting', reason: 'lane' }))[2], /^Waiting for a free lane$/);
  assert.match(E(view({ state: 'paused', reason: 'hour', until }))[2], /^Paused by the hourly cap until \d\d:\d\d$/);
  assert.match(E(view({ state: 'paused', reason: 'day', until }))[2], /^Paused by the daily cap until \d\d:\d\d$/);
  assert.match(E(view({ state: 'resting', reason: 'github', until }))[2], /^Resting, GitHub is not available until \d\d:\d\d$/);
  assert.equal(E(view({ state: 'resting', reason: 'github' }))[0], '✻');
  assert.deepEqual(E({ on: false, state: 'off' }), ['○', '', 'Off']);
});

test('a state or a decision kind the page does not know becomes a plain one, and text of the board is cut and typed', async () => {
  const w = settingsWorld({ worker: view() });
  await tick();
  const v = plain(w.run(`EpicWorker.clean(${JSON.stringify({ ...view(), state: 'constructor', repos: ['a/b', 5, null], decisions: [{ at: 'x', kind: '__proto__', issue: 7, reason: 'r', text: 'y'.repeat(900) }] })})`));
  assert.equal(v.state, 'idle');
  assert.deepEqual(v.repos, ['a/b']);
  assert.equal(v.decisions[0].kind, 'skip');
  assert.equal(v.decisions[0].issue, null);
  assert.ok(v.decisions[0].text.length <= 300);
  assert.equal(plain(w.run('EpicWorker.clean("text")')), null);
});

// ---------------------------------------------------------------- Settings > Nodes > Worker, on

test('Settings > Nodes: a Worker section right after This node, with the state, the allowlists, the caps and the decisions', async () => {
  const w = settingsWorld({ worker: view({ state: 'working', issues: ['example/website#12'], text: 'Working on example/website#12.', caps: { hour: { used: 2, max: 6, clears_at: new Date(Date.now() + 3000000).toISOString() }, day: { used: 5, max: 30, clears_at: null } } }) });
  await tick();
  assert.deepEqual(heads(w).slice(0, 3), ['This node', 'Worker', 'Paired nodes']);
  assert.equal(word(w), 'Working on example/website#12');
  assert.equal(text(worker(w).querySelector('.ew-glyph')), '✽');
  assert.equal(worker(w).querySelector('.ew-glyph').getAttribute('aria-hidden'), 'true', 'the glyph is decoration; the words say it');
  assert.equal(worker(w).querySelector('.ew-state').getAttribute('role'), 'status');
  assert.deepEqual(rowOf(w, 'Repositories').querySelectorAll('.ew-chip').map(text), ['example/website']);
  assert.deepEqual(rowOf(w, 'Authors').querySelectorAll('.ew-chip').map(text), ['alice']);
  assert.match(text(rowOf(w, 'This hour')), /2 of 6 used, the oldest frees at \d\d:\d\d/);
  assert.match(text(rowOf(w, 'Today')), /5 of 30 used$/);
  assert.match(text(rowOf(w, 'Looks every')), /90 s/);
  assert.match(text(worker(w).querySelector('.ew-risk-short')), /starts an agent here with nobody pressing Run/);
  assert.ok(hidden(rowOf(w, 'Read')), 'the age is for the hub; this board reads its own state');
  assert.ok(hidden(worker(w).querySelector('.ew-off')), 'the off text is not shown while it is on');
});

test('a cap that is spent says until when, in words', async () => {
  const at = new Date(Date.now() + 41 * 60000).toISOString();
  const w = settingsWorld({ worker: view({ state: 'paused', reason: 'hour', until: at, text: 'Paused by the hourly cap.', caps: { hour: { used: 6, max: 6, clears_at: at }, day: { used: 6, max: 30, clears_at: null } } }) });
  await tick();
  assert.match(word(w), /^Paused by the hourly cap until \d\d:\d\d$/);
  assert.match(text(rowOf(w, 'This hour')), /^This hour6 of 6 used, full until \d\d:\d\d$/);
});

test('the last 20 decisions, newest first, each with a glyph and word for its kind, the issue and its reason', async () => {
  const many = Array.from({ length: 30 }, (_, i) => dec(i + 1, i % 2 ? 'start' : 'skip', i % 2 ? 'started' : 'author', `example/website#${100 + i}`, { text: i % 2 ? 'started the issue as a task.' : 'the issue is not by an allowed author' }));
  const w = settingsWorld({ worker: view({ decisions: many }) });
  await tick();
  const rows = worker(w).querySelectorAll('.ew-dec');
  assert.equal(rows.length, 20, 'at most 20 are drawn');
  assert.equal(text(rows[0].querySelector('.ew-dec-i')), 'example/website#100');
  assert.equal(text(rows[0].querySelector('.ew-dec-k')), '∙Skipped');
  assert.equal(text(rows[0].querySelector('.ew-dec-r')), 'The issue is not by an allowed author', 'the reason starts with a capital');
  assert.equal(text(rows[1].querySelector('.ew-dec-k')), '✽Started');
  assert.match(text(rows[0].querySelector('.ew-dec-t')), /^\d\d:\d\d$/);
});

test('every decision kind has its own glyph and word', async () => {
  const kinds = ['start', 'skip', 'refused', 'cap', 'rest', 'wait', 'park', 'stop', 'resume', 'error'];
  const w = settingsWorld({ worker: view({ decisions: kinds.map((k, i) => dec(i + 1, k, 'x', null)) }) });
  await tick();
  const got = worker(w).querySelectorAll('.ew-dec-k').map(text);
  assert.equal(new Set(got).size, kinds.length, `ten kinds, ten different ${got.join(' | ')}`);
  for (const g of got) assert.match(g, /^[✽∙✕✻○✓]/, 'a glyph first');
  assert.deepEqual(got, ['✽Started', '∙Skipped', '✕Refused', '✻Cap', '✻Resting', '✻Waiting', '○Parked', '○Stopped', '✓Started again', '✕Error']);
});

test('no decision yet says what will appear here; nothing a board sends is markup', async () => {
  const w = settingsWorld({ worker: view({ decisions: [] }) });
  await tick();
  assert.ok(!hidden(worker(w).querySelector('.ew-none')));
  const w2 = settingsWorld({ worker: view({ repos: ['<img src=x onerror=alert(1)>'], decisions: [dec(1, 'skip', 'x', '<b>bold</b>#1', { text: '<script>x</script>' })] }) });
  await tick();
  assert.equal(worker(w2).querySelectorAll('img').length + worker(w2).querySelectorAll('script').length + worker(w2).querySelectorAll('b').filter((b) => !b.classList.contains('ew-k')).length, 0);
  assert.equal(text(rowOf(w2, 'Repositories').querySelector('.ew-chip')), '<img src=x onerror=alert(1)>');
});

// ---------------------------------------------------------------- off

test('off: one sentence on what it would do, the risk in plain words, the env keys, and no button, no switch', async () => {
  const w = settingsWorld({});
  await tick();
  assert.deepEqual(heads(w).slice(0, 2), ['This node', 'Worker'], 'the section is there even when the worker is off, so the way to turn it on is findable');
  assert.equal(word(w), 'Off');
  const off = worker(w).querySelector('.ew-off');
  assert.ok(!hidden(off));
  const t = text(off);
  assert.match(t, /takes the next ready issue by itself/);
  assert.match(t, /text of an issue starts an agent on this machine/);
  assert.match(t, /acceptEdits/);
  assert.match(t, /nobody watching/);
  assert.match(t, /Anyone who can create or edit issues in the listed repositories can steer it/);
  assert.match(t, /private repository and a dedicated epic/);
  assert.deepEqual(off.querySelectorAll('code').map(text), ['CCBOARD_EPIC_WORKER=1', 'CCBOARD_EPIC_WRITES=1', 'CCBOARD_EPIC_REPOS=owner/name']);
  assert.match(t, /rerun \.\/install\.sh/);
  assert.equal(worker(w).querySelectorAll('button').filter((b) => !hidden(b)).length, 0, 'no Stop, no Start, nothing that turns it on');
  assert.equal(worker(w).querySelectorAll('input').length, 0, 'no toggle');
  assert.ok(hidden(worker(w).querySelector('.ew-on')));
});

test('without nodes-worker.js nothing about the worker is in the page and nothing breaks', async () => {
  const w = settingsWorld({ worker: view(), load: false });
  await tick();
  assert.deepEqual(heads(w), ['This node', 'Paired nodes', 'Found on your tailnet', 'Who can control this node', 'Activity']);
});

test('a worker that refused to start says why, lists nothing as allowed and offers no button', async () => {
  const w = settingsWorld({ worker: view({ state: 'refused', reason: 'repos_empty', text: 'No repository is listed (CCBOARD_EPIC_REPOS is empty), so the worker refuses to start.', repos: [], running: false, decisions: [] }) });
  await tick();
  assert.equal(word(w), 'Refused to start');
  assert.equal(text(worker(w).querySelector('.ew-glyph')), '✕');
  assert.match(text(worker(w).querySelector('.ew-text')), /CCBOARD_EPIC_REPOS is empty/);
  assert.equal(text(rowOf(w, 'Repositories').querySelector('.dim')), 'none listed');
  assert.equal(worker(w).querySelectorAll('button').filter((b) => !hidden(b)).length, 0);
  assert.match(text(rowOf(w, 'Next look')), /not while refused/);
});

// ---------------------------------------------------------------- Stop worker and Start worker

test('Stop worker is one plain tap: no confirmation, not red, a POST, and the stopped state is on screen at once', async () => {
  const w = settingsWorld({ worker: view({ state: 'working', issues: ['example/website#12'], text: 'Working on example/website#12.' }), answers: { '/api/epic-worker/stop': view({ state: 'stopped', reason: 'stopped', text: 'A person stopped the worker. It starts nothing until Start is pressed.', running: false, next_at: null, stopped_at: new Date().toISOString(), at: new Date(Date.now() + 1000).toISOString() }) } });
  await tick();
  const stop = btn(w, 'Stop worker');
  assert.ok(stop && !hidden(stop));
  assert.ok(!/\bdanger\b|\bprimary\b|\bconfirm\b/.test(stop.className), 'a plain bordered button: the safe direction needs no confirmation, and it is not the one filled primary');
  assert.ok(hidden(btn(w, 'Start worker')));
  stop.click();
  await tick(); await tick();
  assert.deepEqual(calls(w).filter((c) => c.path.startsWith('/api/epic-worker')).map((c) => [c.method, c.path]), [['POST', '/api/epic-worker/stop']], 'one call, no second tap');
  assert.equal(word(w), 'Stopped by a person');
  assert.ok(hidden(btn(w, 'Stop worker')) && !hidden(btn(w, 'Start worker')), 'Start takes its place');
  assert.deepEqual(plain(w.get('__toasts')).map((t) => t.text), ['Worker stopped']);
});

test('the answer of a Stop stays until a poll that is newer says something else', async () => {
  const w = settingsWorld({ worker: view({ at: '2026-10-03T03:00:00Z' }), answers: { '/api/epic-worker/stop': view({ state: 'stopped', reason: 'stopped', running: false, at: '2026-10-03T03:00:30Z' }) } });
  await tick();
  btn(w, 'Stop worker').click();
  await tick(); await tick();
  const pick = (stAt) => plain(w.run(`EpicWorker.pick({ node: { epic_worker: ${JSON.stringify(view({ at: stAt }))} } }).state`));
  assert.equal(pick('2026-10-03T03:00:10Z'), 'stopped', 'an older poll does not undo the Stop');
  assert.equal(pick('2026-10-03T03:01:00Z'), 'idle', 'a newer poll is the truth');
  assert.equal(plain(w.run('EpicWorker.pick({ node: {} }).state')), 'stopped', 'a poll without the key (the worker is off) after a Stop: the answer stands until the page is left');
});

test('Start worker only in the stopped state; a refusal shows the board\'s words beside the button and the state stays', async () => {
  const stopped = view({ state: 'stopped', reason: 'stopped', text: 'A person stopped the worker.', running: false });
  const refused = view({ state: 'refused', reason: 'writes_off', text: 'GitHub writes are off (CCBOARD_EPIC_WRITES).', running: false });
  const w = settingsWorld({ worker: stopped, answers: { '/api/epic-worker/start': () => { const e = new Error(refused.text); e.status = 409; e.body = { error: refused.text, reason: 'writes_off', worker: refused }; throw e; } } });
  await tick();
  assert.ok(hidden(btn(w, 'Stop worker')) && !hidden(btn(w, 'Start worker')));
  btn(w, 'Start worker').click();
  await tick(); await tick();
  assert.equal(text(worker(w).querySelector('.ew-err')), refused.text);
  assert.equal(worker(w).querySelector('.ew-err').getAttribute('role'), 'alert');
  assert.equal(word(w), 'Refused to start', 'the board\'s own view of the refusal is on screen');
});

test('a Stop from a session that is not a person says so in plain words', async () => {
  const w = settingsWorld({ worker: view(), answers: { '/api/epic-worker/stop': () => { const e = new Error('forbidden'); e.status = 403; throw e; } } });
  await tick();
  btn(w, 'Stop worker').click();
  await tick(); await tick();
  assert.match(text(worker(w).querySelector('.ew-err')), /Only a signed-in person/);
  assert.ok(!hidden(btn(w, 'Stop worker')), 'the worker still runs, so Stop stays');
});

test('a second tap while one is in flight does nothing', async () => {
  let release;
  const w = settingsWorld({ worker: view(), answers: { '/api/epic-worker/stop': () => new Promise((r) => { release = () => r(view({ state: 'stopped', reason: 'stopped', running: false, at: new Date(Date.now() + 5000).toISOString() })); }) } });
  await tick();
  btn(w, 'Stop worker').click();
  btn(w, 'Stop worker').click();
  await tick();
  assert.equal(calls(w).filter((c) => c.path === '/api/epic-worker/stop').length, 1);
  assert.equal(btn(w, 'Stop worker').getAttribute('aria-busy'), 'true', 'busy is said, the button keeps its place and its focus');
  release();
  await tick(); await tick();
  assert.equal(word(w), 'Stopped by a person');
});

test('a poll repaints the panel in place: the button under a finger is the same node', async () => {
  const w = settingsWorld({ worker: view() });
  await tick();
  const before = btn(w, 'Stop worker');
  w.run(`state = { ...state, node: { ...state.node, epic_worker: ${JSON.stringify(view({ at: new Date(Date.now() + 9000).toISOString(), decisions: [dec(0), dec(1)] }))} } }; updateCurrentPage(state)`);
  await tick();
  assert.equal(btn(w, 'Stop worker'), before);
  assert.equal(worker(w).querySelectorAll('.ew-dec').length, 2);
});

// ---------------------------------------------------------------- the node page (read only, with its age)

function nodeWorld(card) {
  const r = rec('build-box', { card: { ...rec('build-box').card, now: new Date().toISOString(), ...card } });
  const { w } = hubWorld({ state: boardState(), nodes: [r], hash: '#/n/build-box' });
  w.load('nodes-worker.js');
  return { w, r };
}
async function openNode(w, r) {
  hubAnswer(w, 200, { nodes: [r], at: iso(0) }, 'W/"1"');
  await hubPoll(w);
  w.location.hash = '#/n/build-box';
  await hubTick(); await hubTick();
}
const sect = (w) => hubPage(w).querySelector('section[data-sec=worker]');

test('the node page shows the worker state of a paired node, read only, with the age of the reading', async () => {
  const { w, r } = nodeWorld({ epic_worker: view({ state: 'waiting', reason: 'window', until: new Date(Date.now() + 1800000).toISOString(), text: 'Waiting: the agent\'s rate-limit window is at the limit.', at: iso(5) }) });
  await openNode(w, r);
  const s = sect(w);
  assert.ok(s, 'a Worker section');
  assert.equal(text(s.querySelector('h2')), 'Worker');
  assert.match(text(s.querySelector('.ew-word')), /^Waiting for a window until \d\d:\d\d$/);
  assert.equal(s.querySelectorAll('button').length, 0, 'Stop is on the node\'s own board');
  assert.match(text(s.querySelector('.ew-note')), /Stop the worker on that node's own board/);
  assert.match(text(rowIn(s, 'Read')), /^Read\d+ s ago$|^Read\d+ min ago$/, 'a state read by the hub carries its age');
  const hs = hubPage(w).querySelectorAll('h2.nd-h').map(text);
  assert.ok(hs.indexOf('Worker') >= 0 && hs.indexOf('Worker') < hs.findIndex((x) => /^Needs you/.test(x)), 'the Worker section sits under the header, before Needs you');
});
const rowIn = (s, k) => s.querySelectorAll('.ew-row').find((r) => text(r.querySelector('.ew-k')) === k);

test('the node page has no Worker section while the node\'s card carries none (off there, or an older node), and no heading', async () => {
  const { w, r } = nodeWorld({});
  await openNode(w, r);
  assert.equal(sect(w), null);
  assert.ok(!hubPage(w).querySelectorAll('h2').map(text).includes('Worker'));
});

test('the node page asks for the bundle once when the card has the worker and the script is not here', async () => {
  const r = rec('build-box', { card: { ...rec('build-box').card, now: new Date().toISOString(), epic_worker: view() } });
  const { w } = hubWorld({ state: boardState(), nodes: [r] });
  w.run('globalThis.Lazy = { bundles: { worker: {} }, load: async (n) => { __loads.push(n); }, pending: () => [] }');
  w.ctx.__loads = [];
  await openNode(w, r);
  await openNode(w, r);
  assert.deepEqual(plain(w.get('__loads')), ['worker'], 'asked once while the first ask is out');
  assert.equal(sect(w), null, 'nothing until the script ran');
});

// ---------------------------------------------------------------- the demo

const FLAGS = ['working', 'idle', 'waiting', 'paused', 'pausedday', 'resting', 'stopped', 'refused', 'refusedwrites', 'refusedauthors', 'off'];

test('?demo=1&worker=<name> draws every state without a server, with its own words', async () => {
  const expect = { working: /^Working on example\/website#12$/, idle: /^Idle/, waiting: /^Waiting for a window until/, paused: /^Paused by the hourly cap until/, pausedday: /^Paused by the daily cap until/,
    resting: /^Resting, GitHub is not available until/, stopped: /^Stopped by a person$/, refused: /^Refused to start$/, refusedwrites: /^Refused to start$/, refusedauthors: /^Refused to start$/, off: /^Off$/ };
  for (const flag of FLAGS) {
    const w = settingsWorld({ worker: undefined, demo: flag });
    await tick(); await tick(); await tick();
    assert.ok(worker(w), `${flag}: the panel is there`);
    assert.match(word(w), expect[flag], flag);
    if (flag === 'off') assert.ok(!hidden(worker(w).querySelector('.ew-off')));
  }
});

test('the default demo shows a worker at work with all ten decision kinds in the list of 20', async () => {
  const w = settingsWorld({ worker: undefined, demo: 'working' });
  await tick(); await tick(); await tick();
  assert.equal(worker(w).querySelectorAll('.ew-dec').length, 20);
  const kinds = new Set(worker(w).querySelectorAll('.ew-dec').map((d) => d.className.replace('ew-dec ', '')));
  assert.deepEqual([...kinds].sort(), ['cap', 'error', 'park', 'refused', 'rest', 'resume', 'skip', 'start', 'stop', 'wait']);
  assert.ok(worker(w).querySelectorAll('.ew-dec-i').length >= 10);
});

test('in the demo Stop and Start are played for the life of the page, and a refused worker refuses a Start', async () => {
  const w = settingsWorld({ worker: undefined, demo: 'working', answers: {} });
  w.run('api = demoApi');                                              // the page's own make-believe answers, not the recorder
  await tick(); await tick(); await tick();
  btn(w, 'Stop worker').click();
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(word(w), 'Stopped by a person');
  assert.equal(text(worker(w).querySelector('.ew-dec-k')), '○Stopped', 'the Stop is the newest decision');
  btn(w, 'Start worker').click();
  await new Promise((r) => setTimeout(r, 400));
  assert.match(word(w), /^Idle/);
  assert.equal(text(worker(w).querySelector('.ew-dec-k')), '✓Started again');
  const w2 = settingsWorld({ worker: undefined, demo: 'refused' });
  w2.run('api = demoApi');
  await tick(); await tick(); await tick();
  assert.equal(worker(w2).querySelectorAll('button').filter((b) => !hidden(b)).length, 0);
});

test('demo/worker.json: an off view, a base, a patch for every flag and 20 shared decisions', () => {
  assert.deepEqual(Object.keys(FX.states).sort(), FLAGS.filter((f) => f !== 'off').sort());
  assert.equal(FX.decisions.length, 20);
  assert.equal(FX.off.on, false);
  assert.ok(FX.demo.epoch > 1e9, 'times follow the clock like the other fixtures');
  assert.ok(!/@|\/Users\/|\.ts\.net/.test(JSON.stringify(FX)), 'no email, personal path or host name in a public fixture');
});

// ---------------------------------------------------------------- the rules of the code

test('nodes-worker.js: no markup from text, no inline style, no confirm, no em-dash, no Path of the home', () => {
  const code = SRC.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  assert.ok(!/innerHTML|insertAdjacentHTML|outerHTML|cssText|\bstyle\s*[:=]|setAttribute\(\s*['"]style/.test(code));
  assert.ok(!/confirmButton|\bdanger\b/.test(code), 'Stop is the safe direction: one tap');
  assert.ok(!/—/.test(SRC) && !/–/.test(SRC), 'plain hyphens and commas only');
  assert.ok(!/localStorage|sessionStorage|eval\(|new Function/.test(code));
  const css = fs.readFileSync(path.join(STATIC, 'pages', 'worker.css'), 'utf8');
  assert.ok(!/—/.test(css) && !/!important/.test(css) && !/data:/.test(css));
});

test('the bundle is lazy: index.html carries none of it, and lazy.js lists exactly the script and the sheet', () => {
  const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
  assert.ok(!html.includes('nodes-worker') && !html.includes('worker.css'));
  const lazy = fs.readFileSync(path.join(STATIC, 'lazy.js'), 'utf8');
  assert.match(lazy, /worker: \{ js: \['\/static\/nodes-worker\.js'\], css: \[\{ href: '\/static\/pages\/worker\.css', rank: 18 \}\]/);
});

test('the sentence under the headline uses this page\'s clock and does not repeat the headline', async () => {
  const until = new Date(Date.now() + 1800000).toISOString();
  const w = settingsWorld({ worker: view({ state: 'paused', reason: 'hour_cap', until, text: 'Paused by the hourly cap. It clears at 03:16 UTC.' }) });
  await tick();
  const say = text(worker(w).querySelector('.ew-text'));
  assert.doesNotMatch(say, /UTC/, 'one clock on the page');
  assert.doesNotMatch(say, /^Paused by the hourly cap/, 'the headline already says it');
  assert.match(say, /^It clears at \d\d:\d\d\.$/);
  const w2 = settingsWorld({ worker: view({ state: 'working', issues: ['example/website#12'], text: 'Working on example/website#12.' }) });
  await tick();
  assert.equal(text(worker(w2).querySelector('.ew-text')), '', 'a sentence that only repeats the headline is left out');
});
