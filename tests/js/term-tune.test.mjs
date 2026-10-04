// Contract tests for the tuning strip and the send box of app/static/term.js (window.TermPage + the page itself): chip order by registry weight, the
// gate, the two-tap Clear, effort / model / rename / read commands through POST /command, the 409 toast, the pending -> confirmed flash, the registry
// cache, the queue label and POST /prompt (with its /keys fallbacks). term.js is an IIFE that starts the page, so each test builds the page skeleton
// (the ids of term.html), a fake api that records every call, a fake clock and a fake Date.now, then loads the real scripts.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { installDom } from './minidom.mjs';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { homeWorld } from './world.mjs';

// ---------------------------------------------------------------- the world

function fakeClock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  return {
    setTimeout(fn, ms = 0) { const id = ++seq; timers.set(id, { at: now + ms, fn }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn, ms = 0) { const id = ++seq; timers.set(id, { at: now + ms, fn, every: Math.max(1, ms) }); return id; },
    clearInterval(id) { timers.delete(id); },
    get now() { return now; },
    advance(ms) {
      const end = now + ms;
      for (;;) {
        let next = null;
        for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next.t.at || (t.at === next.t.at && id < next.id))) next = { id, t };
        if (!next) break;
        now = next.t.at;
        if (next.t.every) next.t.at += next.t.every; else timers.delete(next.id);
        next.t.fn();
      }
      now = end;
    },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

const NAME = 'shop--api--s1';
const API = '/api/sessions/' + NAME;

const row = (over = {}) => ({
  tmux: NAME, project: 'shop', repo: 'api', name: 's1', agent: 'claude', state: 'idle', state_at: null, last_prompt: 'fix it', last_message: '',
  stats: { model: 'Opus 5', model_id: 'claude-opus-5', effort: 'high', fast: false, context_pct: 12, session_name: 'login work' },
  flags: {}, pending: [], task: null, shell_version: 'v1', ...over,
});

const REGISTRY = {
  clear: { cmd: '/clear', label: 'Clear', arg: false, read: false, verified: false, weight: 155, destructive: true },
  compact: { cmd: '/compact', label: 'Compact', arg: false, read: false, verified: false, weight: 120, destructive: false },
  usage: { cmd: '/usage', label: 'Usage', arg: false, read: true, verified: false, weight: 100, destructive: false },
  effort: { cmd: '/effort', label: 'Effort', arg: true, read: false, verified: false, weight: 58, destructive: false },
  model: { cmd: '/model', label: 'Model', arg: true, read: false, verified: false, weight: 39, destructive: false },
  rename: { cmd: '/rename', label: 'Rename', arg: true, read: false, verified: false, weight: 10, destructive: false },
  context: { cmd: '/context', label: 'Context', arg: false, read: true, verified: false, weight: 9, destructive: false },
  status: { cmd: '/status', label: 'Status', arg: false, read: true, verified: false, weight: 3, destructive: false },
  cost: { cmd: '/cost', label: 'Cost', arg: false, read: true, verified: false, weight: 0, destructive: false },
  fast: { cmd: '/fast', label: 'Fast', arg: false, read: false, verified: false, weight: 0, destructive: false },
};

function httpError(status, message, body) {
  const e = new Error(message);
  e.status = status;
  e.body = body === undefined ? null : body;
  return e;
}

/**
 * The terminal page in a vm world. `fake.row` is what GET /api/sessions/<name> answers; `fake.routes['POST /command']` etc. override a route
 * (a function (body) -> result, or throws an httpError). Every call lands in fake.calls as {method, path, body}.
 */
async function page({ session = row(), agents = { claude: { slash: REGISTRY } }, agentsError = null, cache = null, search = '', allchips = false, name = NAME,
  innerHeight = 844, tune = null, scrolls = null, quick = null } = {}) {
  const clock = fakeClock();
  class FakeDate extends Date { static now() { return clock.now; } }
  const w = makeWorld({
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, setInterval: clock.setInterval, clearInterval: clock.clearInterval, Date: FakeDate, innerHeight,
  });
  const dom = installDom(w);
  if (scrolls) dom.El.prototype.scrollIntoView = function scrollIntoView(o) { scrolls.push({ text: this.textContent, cls: this.className, opts: plain(o) }); };
  w.location.pathname = '/term/' + name;
  w.document.documentElement.style.setProperty = function setProperty(k, v) { this[k] = v; };      // TermKit.viewportFit writes --vvh / --vvt
  w.location.search = search;
  const { El, body } = dom;
  const mk = (tag, id, cls) => { const n = new El(tag); n.setAttribute('id', id); if (cls) n.className = cls; return n; };
  const put = (parent, ...kids) => { parent.append(...kids); return parent; };
  const sendtext = mk('textarea', 'sendtext', 'composer');
  const sendbtn = mk('button', 'sendbtn');
  sendbtn.textContent = 'Send';
  const sendform = put(mk('form', 'sendform'), sendtext, mk('button', 'nl'), sendbtn);
  const ttywrap = put(mk('div', 'ttywrap'), mk('iframe', 'tty'), mk('div', 'rail'), mk('button', 'histchip', 'hidden'));
  const termmain = put(mk('div', 'termmain'), mk('section', 'ctxstrip', 'hidden'), ttywrap, mk('div', 'keyhost'),
    put(mk('div', 'quickrow'), mk('div', 'quick'), mk('div', 'qtools')), sendform);
  const headtools = mk('div', 'headtools');
  body.append(mk('header', 'termhead'), mk('div', 'headid'), headtools, mk('a', 'back'), termmain);
  if (tune !== null) w.localStorage.setItem('ccboard:term:tune', tune);
  if (cache !== null) w.sessionStorage.setItem('ccboard:agents', JSON.stringify(cache));
  if (allchips) w.localStorage.setItem('ccboard:term:allchips', '1');
  if (quick !== null) w.localStorage.setItem('ccboard:quick:' + name, JSON.stringify(quick));

  for (const f of ['core.js', 'components.js', 'termkit.js']) w.load(f);
  const fake = { row: session, calls: [], toasts: [], routes: {}, agents, agentsError, clock, w, dom, termmain, sendtext, sendbtn, sendform, headtools };
  w.ctx.__route = async (method, path, bodyIn) => {
    fake.calls.push({ method, path, body: bodyIn === undefined ? undefined : plain(bodyIn) });
    const key = `${method} ${path.replace(API, '')}`;
    if (fake.routes[key]) { const r = fake.routes[key](bodyIn); if (r instanceof Error) throw r; return r; }
    if (key === 'GET ') return fake.row;
    if (key === 'GET /pane') return { in_mode: false };
    if (key === 'GET /api/agents') { if (fake.agentsError) throw fake.agentsError; return { agents: fake.agents }; }
    if (key === 'POST /command') return { ok: true, sent: '/' + bodyIn.cmd, verified: false };
    if (key === 'POST /prompt') return { ok: true, pasted: true, queued: fake.row.state === 'working' };
    if (key === 'POST /keys') return { ok: true };
    throw httpError(404, '404 Not Found', { detail: 'Not Found' });
  };
  w.ctx.__toast = (text, o) => { fake.toasts.push({ text: String(text), kind: o && o.kind }); return null; };
  w.run('api = (m, p, b) => __route(m, p, b); toast = (t, o) => __toast(t, o);');
  w.load('term.js');
  fake.tick = async (ms = 3000) => { clock.advance(ms); await settle(); await settle(); };
  await fake.tick(700);                                         // the first poll (the page waits 600 ms after load)
  fake.TermPage = w.get('window.TermPage');
  fake.tune = () => termmain.querySelector('#tune');
  fake.toggle = () => headtools.querySelector('.tune-toggle');
  fake.cmdChips = () => termmain.querySelectorAll('#tune button').filter((n) => !n.classList.contains('tune-opt')).map((n) => n.textContent);
  fake.chip = (cmd, arg) => {
    const all = termmain.querySelectorAll(`#tune button[data-cmd="${cmd}"]`);
    return arg === undefined ? all[0] : all.find((n) => n.textContent === arg);
  };
  fake.chips = () => termmain.querySelectorAll('#tune button').map((n) => n.getAttribute('data-cmd'));
  fake.commands = () => fake.calls.filter((c) => c.method === 'POST' && c.path === API + '/command').map((c) => c.body);
  fake.posts = (suffix) => fake.calls.filter((c) => c.method === 'POST' && c.path === API + suffix).map((c) => c.body);
  fake.dialog = (cls) => w.document.querySelector('dialog.' + cls);
  fake.quickChips = () => termmain.querySelectorAll('#quick button').map((n) => n.textContent);
  fake.click = async (node) => { node.click(); await settle(); await settle(); };
  return fake;
}

// ---------------------------------------------------------------- pure helpers

test('tuneOrder: heaviest first, weight 0 hidden unless asked, ties in registry order', async () => {
  const { TermPage } = await page();
  assert.deepEqual(plain(TermPage.tuneOrder(REGISTRY)).map((s) => s.key), ['clear', 'compact', 'usage', 'effort', 'model', 'rename', 'context', 'status']);
  assert.deepEqual(plain(TermPage.tuneOrder(REGISTRY, { all: true })).map((s) => s.key), ['clear', 'compact', 'usage', 'effort', 'model', 'rename', 'context', 'status', 'cost', 'fast']);
  const first = plain(TermPage.tuneOrder(REGISTRY))[0];
  assert.deepEqual(first, { key: 'clear', cmd: '/clear', label: 'Clear', arg: false, read: false, weight: 155, destructive: true });
  assert.deepEqual(plain(TermPage.tuneOrder({ a: { label: 'A', weight: 5 }, b: { label: 'B', weight: 5 }, c: null, d: { weight: -1 } })).map((s) => s.key), ['a', 'b']);
  assert.deepEqual(plain(TermPage.tuneOrder(null)), []);
});

test('tuneGate matrix: idle, done, errored and idle-waiting are open; working, a permission, compacting and a pending request are not; a shell has no strip', async () => {
  const { TermPage: T } = await page();
  const gate = (over) => plain(T.tuneGate(row(over)));
  const WHY = 'available when the session is at its prompt';
  assert.deepEqual(gate({ state: 'idle' }), { show: true, enabled: true, title: '' });
  assert.equal(gate({ state: 'done' }).enabled, true);
  assert.equal(gate({ state: 'errored' }).enabled, true, 'the server types into an errored pane (TYPEABLE_STATES): /clear and /compact help there');
  assert.equal(gate({ state: 'errored', flags: { compacting: true } }).enabled, false);
  assert.equal(gate({ state: 'errored', pending: [{ id: 1 }] }).enabled, false);
  assert.equal(gate({ state: 'waiting', flags: { wait_kind: 'idle' } }).enabled, true);
  assert.deepEqual(gate({ state: 'working' }), { show: true, enabled: false, title: WHY });
  assert.equal(gate({ state: 'waiting', flags: { wait_kind: 'permission' } }).enabled, false);
  assert.equal(gate({ state: 'waiting', flags: { wait_kind: 'elicitation' } }).enabled, false);
  assert.equal(gate({ state: 'waiting' }).enabled, false);
  assert.equal(gate({ state: 'idle', flags: { compacting: true } }).enabled, false);
  assert.equal(gate({ state: 'idle', pending: [{ id: 1 }] }).enabled, false);
  assert.equal(gate({ state: 'ended' }).enabled, false);
  assert.equal(gate({ state: null }).enabled, false);
  assert.deepEqual(gate({ agent: 'shell' }), { show: false, enabled: false, title: '' });
  assert.equal(gate({ agent: null }).show, false);
  assert.equal(plain(T.tuneGate(null)).show, false);
  assert.equal(T.GATE_TITLE, WHY);
});

test('effortOptions and modelOptions mark the current one from the statusline stats', async () => {
  const { TermPage: T } = await page();
  const eff = plain(T.effortOptions({ effort: 'XHigh' }));
  assert.deepEqual(eff.map((o) => o.value), ['low', 'medium', 'high', 'xhigh', 'max', 'ultracode']);
  assert.deepEqual(eff.filter((o) => o.current).map((o) => o.value), ['xhigh']);
  assert.equal(eff.find((o) => o.value === 'ultracode').arg, 'ultracode on', 'claude.py V19: /effort ultracode on');
  assert.equal(eff.find((o) => o.value === 'high').arg, 'high');
  assert.deepEqual(plain(T.effortOptions(null)).filter((o) => o.current), []);
  const mod = plain(T.modelOptions({ model: 'Opus 5', model_id: 'claude-opus-5' }));
  assert.deepEqual(mod.map((o) => o.value), ['opus', 'fable', 'sonnet', 'haiku']);
  assert.deepEqual(mod.filter((o) => o.current).map((o) => o.value), ['opus']);
  assert.deepEqual(plain(T.modelOptions({ model_id: 'claude-haiku-4-5' })).filter((o) => o.current).map((o) => o.value), ['haiku']);
  assert.deepEqual(plain(T.modelOptions({})).filter((o) => o.current), []);
});

test('queueLabel / sendMode: queue while working, Send at the prompt, raw keys for a shell and for dialogs', async () => {
  const { TermPage: T } = await page();
  const mode = (over) => T.sendMode(row(over));
  assert.equal(T.queueLabel(row({ state: 'working' })), 'queue');
  assert.equal(T.queueLabel(row({ state: 'idle' })), 'Send');
  assert.equal(T.queueLabel(null), 'Send');
  assert.equal(mode({ state: 'idle' }), 'prompt');
  assert.equal(mode({ state: 'done' }), 'prompt');
  assert.equal(mode({ state: 'errored' }), 'prompt');
  assert.equal(mode({ state: 'working' }), 'queue');
  assert.equal(mode({ state: 'waiting', flags: { wait_kind: 'idle' } }), 'prompt');
  assert.equal(mode({ state: 'waiting', flags: { wait_kind: 'permission' } }), 'keys');
  assert.equal(mode({ state: 'waiting', flags: { wait_kind: 'elicitation' } }), 'keys');
  assert.equal(mode({ state: 'idle', pending: [{ id: 3 }] }), 'keys');
  assert.equal(mode({ state: null }), 'keys');
  assert.equal(mode({ agent: 'shell' }), 'keys');
  assert.equal(T.sendMode(null), 'keys');
});

// ---------------------------------------------------------------- the strip

test('the strip sits between the context strip and the terminal as ONE row: the command chips in weight order, then Effort, then Model', async () => {
  const p = await page();
  const tune = p.tune();
  assert.ok(tune, '#tune exists');
  assert.equal(tune.parentNode, p.termmain);
  const order = p.termmain.children.map((n) => n.getAttribute('id'));
  assert.deepEqual(order.slice(0, 3), ['ctxstrip', 'tune', 'ttywrap']);
  assert.ok(!tune.classList.contains('hidden'));
  const rows = tune.querySelectorAll('.tune-row');
  assert.equal(rows.length, 1, 'one scrolling row, not two (two 44 px rows cost 101 px of the terminal at 390)');
  assert.equal(tune.children.length, 1, 'the row is the only child of #tune');
  assert.equal(tune.querySelectorAll('.tune-cmds').length + tune.querySelectorAll('.tune-set').length, 0, 'no second row');
  const kids = rows[0].children;
  assert.deepEqual(kids.map((n) => n.textContent), ['Clear', 'Compact', 'Usage', 'Rename', 'Context', 'Status', 'effortlowmediumhighxhighmaxultracode', 'modelopusfablesonnethaiku']);
  assert.deepEqual(kids.slice(0, 6).map((n) => n.tagName), Array(6).fill('BUTTON'), 'six command chips first');
  assert.deepEqual(kids.slice(6).map((n) => n.getAttribute('aria-label')), ['Effort', 'Model'], 'then the Effort segment, then the Model segment');
  const segs = rows[0].querySelectorAll('.tune-seg');
  assert.deepEqual(segs[0].querySelectorAll('button').map((n) => n.textContent), ['low', 'medium', 'high', 'xhigh', 'max', 'ultracode']);
  assert.deepEqual(segs[1].querySelectorAll('button').map((n) => n.textContent), ['opus', 'fable', 'sonnet', 'haiku']);
  assert.equal(segs[1].querySelector('.tune-lbl').textContent, 'model', 'the label that says what the four chips are');
  assert.ok(!p.chips().includes('cost') && !p.chips().includes('fast'), 'weight 0 stays out');
  for (const b of tune.querySelectorAll('button')) assert.equal(b.getAttribute('type'), 'button');
});

test('Effort comes before Model in the row whatever their registry weights are', async () => {
  const reg = { ...REGISTRY, model: { ...REGISTRY.model, weight: 400 } };
  const p = await page({ agents: { claude: { slash: reg } } });
  assert.deepEqual(plain(p.TermPage.tuneOrder(reg)).map((s) => s.key).slice(0, 2), ['model', 'clear'], 'model really is the heaviest here');
  assert.deepEqual(p.tune().querySelector('.tune-row').children.slice(-2).map((n) => n.getAttribute('aria-label')), ['Effort', 'Model']);
});

test('ccboard:term:allchips=1 adds the weight-0 chips (Cost and Fast) at the end; Fast shows its state', async () => {
  const p = await page({ allchips: true, session: row({ stats: { fast: true, effort: 'high' } }) });
  const cmds = p.cmdChips();
  assert.deepEqual(cmds, ['Clear', 'Compact', 'Usage', 'Rename', 'Context', 'Status', 'Cost', 'Fast'], 'Cost and Fast end the command chips, before the segments');
  assert.deepEqual(p.tune().querySelector('.tune-row').children.slice(-2).map((n) => n.getAttribute('aria-label')), ['Effort', 'Model']);
  assert.ok(p.chip('fast').classList.contains('on'));
  assert.equal(p.chip('fast').getAttribute('aria-pressed'), 'true');
});

test('current effort and model are marked from the statusline and follow the next poll', async () => {
  const p = await page();
  assert.ok(p.chip('effort', 'high').classList.contains('on'));
  assert.equal(p.chip('effort', 'high').getAttribute('aria-pressed'), 'true');
  assert.ok(!p.chip('effort', 'max').classList.contains('on'));
  assert.ok(p.chip('model', 'opus').classList.contains('on'));
  assert.ok(!p.chip('model', 'haiku').classList.contains('on'));
  p.row = row({ stats: { model: 'Haiku 4.5', model_id: 'claude-haiku-4-5', effort: 'max' } });
  await p.tick();
  assert.ok(p.chip('effort', 'max').classList.contains('on'));
  assert.ok(!p.chip('effort', 'high').classList.contains('on'));
  assert.ok(p.chip('model', 'haiku').classList.contains('on'));
  assert.ok(!p.chip('model', 'opus').classList.contains('on'));
});

test('gate in the DOM: idle enables every chip, working disables them all with the title, a shell hides the strip, compacting and a permission disable', async () => {
  const p = await page();
  const all = () => p.tune().querySelectorAll('button');
  assert.ok(all().length >= 16);
  assert.ok(all().every((b) => !b.disabled), 'idle: every chip is enabled');
  p.row = row({ state: 'working' });
  await p.tick();
  assert.ok(all().every((b) => b.disabled), 'working: every chip is disabled');
  assert.ok(all().every((b) => b.getAttribute('title') === 'available when the session is at its prompt'));
  p.row = row({ state: 'waiting', flags: { wait_kind: 'permission' }, pending: [{ id: 1, tool_name: 'Bash' }] });
  await p.tick();
  assert.ok(all().every((b) => b.disabled), 'waiting on a permission: disabled');
  p.row = row({ state: 'idle', flags: { compacting: true } });
  await p.tick();
  assert.ok(all().every((b) => b.disabled), 'compacting: disabled');
  p.row = row({ state: 'waiting', flags: { wait_kind: 'idle' } });
  await p.tick();
  assert.ok(all().every((b) => !b.disabled), 'waiting on the idle prompt: enabled again');
  assert.ok(all().every((b) => !b.getAttribute('title') || !/at its prompt/.test(b.getAttribute('title'))));
  p.row = row({ agent: 'shell', state: 'idle' });
  await p.tick();
  assert.ok(p.tune().classList.contains('hidden'), 'a shell row hides the strip');
});

test('a tap on the dimmed strip says why instead of doing nothing; an enabled strip stays quiet', async () => {
  const p = await page({ session: row({ state: 'working' }) });
  assert.equal(p.toasts.length, 0);
  p.tune().click();
  assert.equal(p.toasts.length, 1);
  assert.equal(p.toasts[0].text, 'Available when the session is at its prompt');
  assert.equal(p.toasts[0].kind, 'info');
  p.row = row({ state: 'idle' });
  await p.tick();
  p.tune().click();
  assert.equal(p.toasts.length, 1, 'enabled: no explanation needed');
  await p.click(p.chip('compact'));                                     // a chip tap bubbles to #tune too: still quiet
  assert.equal(p.toasts.length, 1);
});

test('a shell row never shows the strip, and an unknown agent without a registry hides it too', async () => {
  const shell = await page({ session: row({ agent: 'shell' }) });
  assert.ok(shell.tune().classList.contains('hidden'));
  const codex = await page({ session: row({ agent: 'codex' }), agents: { codex: { slash: {} } } });
  assert.ok(codex.tune().classList.contains('hidden'), 'codex has no slash registry yet');
});

test('a disabled chip sends nothing', async () => {
  const p = await page({ session: row({ state: 'working' }) });
  await p.click(p.chip('compact'));
  assert.deepEqual(p.commands(), []);
});

// ---------------------------------------------------------------- commands

test('Compact posts {cmd:"compact"} and nothing else', async () => {
  const p = await page();
  await p.click(p.chip('compact'));
  assert.deepEqual(p.commands(), [{ cmd: 'compact' }]);
});

test('Clear needs two taps: the first only asks, the second posts confirm:true; the ask lapses after 4 s', async () => {
  const p = await page();
  const clear = p.chip('clear');
  await p.click(clear);
  assert.deepEqual(p.commands(), [], 'the first tap sends nothing');
  assert.equal(clear.textContent, 'Confirm clear');
  assert.ok(clear.classList.contains('confirm'));
  await p.click(clear);
  assert.deepEqual(p.commands(), [{ cmd: 'clear', confirm: true }]);
  assert.equal(clear.textContent, 'Clear');
  assert.ok(!clear.classList.contains('confirm'));
  // lapse
  await p.click(clear);
  assert.equal(clear.textContent, 'Confirm clear');
  p.clock.advance(4100);
  assert.equal(clear.textContent, 'Clear');
  await p.click(clear);
  assert.equal(p.commands().length, 1, 'after the lapse the next tap asks again');
  // another chip disarms it
  await p.click(p.chip('compact'));
  assert.equal(clear.textContent, 'Clear');
  assert.deepEqual(p.commands().slice(-1), [{ cmd: 'compact' }]);
});

test('a poll while Clear waits for its second tap keeps the ask (the strip is patched, not rebuilt)', async () => {
  const p = await page();
  const clear = p.chip('clear');
  await p.click(clear);
  await p.tick(1500);
  assert.equal(p.chip('clear'), clear, 'same node');
  assert.equal(clear.textContent, 'Confirm clear');
  p.row = row({ state: 'working' });
  await p.tick();
  assert.equal(clear.textContent, 'Clear', 'the session left its prompt: the ask is dropped');
});

test('Effort posts {cmd:"effort", arg:"high"} in one tap; ultracode sends the V19 argument', async () => {
  const p = await page();
  await p.click(p.chip('effort', 'high'));
  await p.click(p.chip('effort', 'ultracode'));
  assert.deepEqual(p.commands(), [{ cmd: 'effort', arg: 'high' }, { cmd: 'effort', arg: 'ultracode on' }]);
});

test('Model chips post {cmd:"model", arg:<name>}', async () => {
  const p = await page();
  await p.click(p.chip('model', 'sonnet'));
  assert.deepEqual(p.commands(), [{ cmd: 'model', arg: 'sonnet' }]);
});

test('a 409 shows a toast with the reason and the retry seconds, and the chip never goes pending', async () => {
  const p = await page();
  p.routes['POST /command'] = () => httpError(409, 'working', { error: 'working', message: 'the session is working', state: 'working', wait_kind: null, retry: 5 });
  const gets = () => p.calls.filter((c) => c.method === 'GET' && c.path === API).length;
  const before = gets();
  await p.click(p.chip('compact'));
  assert.equal(p.toasts.length, 1);
  assert.match(p.toasts[0].text, /the session is working/);
  assert.match(p.toasts[0].text, /try again in 5 s/);
  assert.equal(p.toasts[0].kind, 'warn');
  assert.ok(!p.chip('compact').classList.contains('pending'));
  await p.tick(700);
  assert.ok(gets() > before, 'the page re-reads the row soon after a refusal (the gate was stale)');
  p.clock.advance(25000);
  assert.equal(p.toasts.length, 1, 'no "not confirmed" toast for a command that was never sent');
});

test('a 409 without retry, a 400 and a server without /command each toast something readable', async () => {
  const p = await page();
  p.routes['POST /command'] = () => httpError(409, 'elicitation', { error: 'elicitation', message: 'the session is asking a question in a dialog', retry: null });
  await p.click(p.chip('compact'));
  assert.equal(p.toasts.at(-1).text, 'the session is asking a question in a dialog');
  p.clock.advance(3100);
  p.routes['POST /command'] = () => httpError(400, 'unknown command; allowed: /clear', { error: 'unknown command; allowed: /clear' });
  await p.click(p.chip('status'));
  assert.match(p.toasts.at(-1).text, /unknown command/);
  p.clock.advance(3100);
  p.routes['POST /command'] = () => httpError(404, '404 Not Found', { detail: 'Not Found' });
  await p.click(p.chip('compact'));
  assert.match(p.toasts.at(-1).text, /too old/);
});

test('a read command opens the readout dialog with the captured screen, and closing it sends one Escape', async () => {
  const p = await page();
  p.routes['POST /command'] = (b) => ({ ok: true, sent: '/' + b.cmd, verified: false, screen: 'Usage\n  5h  42%\n  7d  10%\n\n\n' });
  await p.click(p.chip('usage'));
  assert.deepEqual(p.commands(), [{ cmd: 'usage' }]);
  const dlg = p.dialog('readout');
  assert.ok(dlg, 'a <dialog class="readout"> exists');
  assert.equal(dlg.tagName, 'DIALOG');
  assert.ok(dlg.open, 'it is open');
  assert.equal(dlg.querySelector('pre').textContent, 'Usage\n  5h  42%\n  7d  10%', 'the text, trailing blank lines trimmed');
  assert.ok(!p.chip('usage').classList.contains('pending'), 'a read command never goes pending');
  assert.deepEqual(p.posts('/keys'), [], 'no Escape while the readout is open');
  const close = dlg.querySelectorAll('button').find((b) => b.textContent === 'Close');
  assert.ok(close, 'a Close button');
  await p.click(close);
  assert.ok(!dlg.open);
  assert.deepEqual(p.posts('/keys'), [{ keys: ['Escape'] }], 'Escape closes Claude\'s own dialog, once');
  // a second readout reuses the same dialog
  await settleAll(p);
  await p.click(p.chip('status'));
  assert.equal(p.w.document.querySelectorAll('dialog.readout').length, 1);
  assert.ok(dlg.open);
  dlg.close();                                                           // Esc / backdrop end the same way as Close
  await settleAll(p);
  assert.equal(p.posts('/keys').length, 2);
});

const settleAll = async (p) => { p.clock.advance(300); await settle(); await settle(); };

test('the readout closes on a click on its backdrop (the dialog itself)', async () => {
  const p = await page();
  p.routes['POST /command'] = () => ({ ok: true, screen: 'x' });
  await p.click(p.chip('context'));
  const dlg = p.dialog('readout');
  assert.ok(dlg.open);
  await p.click(dlg);
  assert.ok(!dlg.open);
  assert.equal(p.posts('/keys').length, 1);
});

test('the next /command waits for the Escape that closes the readout and sends nothing before it', async () => {
  const p = await page();
  p.routes['POST /command'] = (b) => (b.cmd === 'usage' ? { ok: true, screen: 'u' } : { ok: true });
  await p.click(p.chip('usage'));
  p.dialog('readout').close();
  await settle();
  await p.click(p.chip('compact'));                                      // tapped while the Escape's 150 ms gap runs
  assert.equal(p.commands().length, 1, 'the compact is held back by the gap');
  await settleAll(p);
  assert.deepEqual(p.commands(), [{ cmd: 'usage' }, { cmd: 'compact' }]);
  const order = p.calls.filter((c) => c.method === 'POST').map((c) => c.path.replace(API, ''));
  assert.deepEqual(order, ['/command', '/keys', '/command'], 'Escape sits between the two commands');
});

test('pending -> ok flash: a set command is pending until a poll carries last_cmd.confirmed, then flashes ok for 1.5 s', async () => {
  const p = await page();
  await p.click(p.chip('compact'));
  assert.ok(p.chip('compact').classList.contains('pending'), 'pending at once');
  p.row = row({ flags: { pending_cmd: { cmd: 'compact', arg: null, at: 't1', before: {} } } });
  await p.tick();
  assert.ok(p.chip('compact').classList.contains('pending'), 'still pending while the server waits for the statusline');
  assert.ok(!p.chip('effort', 'high').classList.contains('pending'), 'only that command\'s chip');
  p.row = row({ flags: { last_cmd: { cmd: 'compact', arg: null, at: 't1', confirmed: true } } });
  await p.tick();
  assert.ok(!p.chip('compact').classList.contains('pending'));
  assert.ok(p.chip('compact').classList.contains('ok'), 'ok flash');
  p.clock.advance(1600);
  assert.ok(!p.chip('compact').classList.contains('ok'), 'the flash ends after 1.5 s');
  await p.tick();
  assert.ok(!p.chip('compact').classList.contains('ok'), 'the same last_cmd does not flash again');
  assert.equal(p.toasts.length, 0);
});

test('an effort segment is pending only for its own argument', async () => {
  const p = await page();
  await p.click(p.chip('effort', 'max'));
  assert.ok(p.chip('effort', 'max').classList.contains('pending'));
  assert.ok(!p.chip('effort', 'high').classList.contains('pending'));
  p.row = row({ stats: { effort: 'max', model: 'Opus 5' }, flags: { last_cmd: { cmd: 'effort', arg: 'max', at: 't2', confirmed: true } } });
  await p.tick();
  assert.ok(p.chip('effort', 'max').classList.contains('ok'));
  assert.ok(p.chip('effort', 'max').classList.contains('on'));
});

test('an older last_cmd of the same command does not settle a new one', async () => {
  const p = await page({ session: row({ flags: { last_cmd: { cmd: 'compact', arg: null, at: 'old', confirmed: true } } }) });
  await p.click(p.chip('compact'));
  await p.tick();
  assert.ok(p.chip('compact').classList.contains('pending'), 'the stale last_cmd (same at as before the send) is not ours');
  assert.ok(!p.chip('compact').classList.contains('ok'));
});

test('20 s without a confirmation: the chip stops pending and a toast says it was not confirmed by the statusline', async () => {
  const p = await page();
  await p.click(p.chip('compact'));
  p.clock.advance(19000);
  assert.ok(p.chip('compact').classList.contains('pending'));
  assert.equal(p.toasts.length, 0);
  p.clock.advance(1100);
  assert.ok(!p.chip('compact').classList.contains('pending'));
  assert.equal(p.toasts.length, 1);
  assert.match(p.toasts[0].text, /not confirmed by the statusline/);
  assert.match(p.toasts[0].text, /compact/);
});

test('last_cmd.confirmed:false (the server\'s own 20 s) toasts the same way', async () => {
  const p = await page();
  await p.click(p.chip('compact'));
  p.row = row({ flags: { last_cmd: { cmd: 'compact', arg: null, at: 't9', confirmed: false } } });
  await p.tick();
  assert.equal(p.toasts.length, 1);
  assert.match(p.toasts[0].text, /not confirmed by the statusline/);
  assert.ok(!p.chip('compact').classList.contains('pending'));
});

test('flags.pending_cmd from the server marks the chip pending even after a reload (no local send)', async () => {
  const p = await page({ session: row({ flags: { pending_cmd: { cmd: 'effort', arg: 'xhigh', at: 't3', before: {} } } }) });
  assert.ok(p.chip('effort', 'xhigh').classList.contains('pending'));
  assert.ok(!p.chip('effort', 'low').classList.contains('pending'));
  assert.ok(!p.chip('compact').classList.contains('pending'));
});

const iso = (p, agoMs) => new Date(p.clock.now - agoMs).toISOString();

test('a server pending_cmd older than 20 s no longer holds the chip pending; one toast says it was not confirmed by the statusline', async () => {
  const p = await page();
  p.row = row({ flags: { pending_cmd: { cmd: 'rename', arg: 'x', at: iso(p, 30000), before: {} } } });
  await p.tick();
  assert.ok(!p.chip('rename').classList.contains('pending'), 'a stale stamp is not believed (the server clears it only when a statusline arrives)');
  assert.equal(p.toasts.length, 1);
  assert.match(p.toasts[0].text, /\/rename x not confirmed by the statusline/);
  assert.equal(p.toasts[0].kind, 'warn');
  await p.tick(); await p.tick();
  assert.equal(p.toasts.length, 1, 'once, not on every poll');
  p.row = row({ flags: { pending_cmd: { cmd: 'compact', arg: null, at: iso(p, 60000), before: {} } } });
  await p.tick();
  assert.equal(p.toasts.length, 2, 'another stale command is another toast');
  assert.match(p.toasts[1].text, /^\/compact not confirmed/);
});

test('the stamp in the server\'s own format (db.now(): ISO 8601 with a +00:00 offset, whole seconds) is read, fresh and stale', async () => {
  const server = (p, agoMs) => new Date(p.clock.now - agoMs).toISOString().replace(/\.\d+Z$/, '+00:00');
  const p = await page();
  assert.match(server(p, 0), /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$/);
  p.row = row({ flags: { pending_cmd: { cmd: 'rename', arg: 'x', at: server(p, 4000), before: {} } } });
  await p.tick();
  assert.ok(p.chip('rename').classList.contains('pending'), 'a few seconds old: pending');
  p.row = row({ flags: { pending_cmd: { cmd: 'rename', arg: 'x', at: server(p, 48000), before: {} } } });   // the reviewer's case: a reload 48 s after the command
  await p.tick();
  assert.ok(!p.chip('rename').classList.contains('pending'), '48 s old: not pending any more');
  assert.equal(p.toasts.length, 1);
});

test('a fresh server pending_cmd is pending until it is 20 s old, then it ages out', async () => {
  const p = await page();
  p.row = row({ flags: { pending_cmd: { cmd: 'effort', arg: 'xhigh', at: iso(p, 5000), before: {} } } });
  await p.tick();                                                          // about 8 s old
  assert.ok(p.chip('effort', 'xhigh').classList.contains('pending'));
  assert.equal(p.toasts.length, 0);
  await p.tick(); await p.tick();                                          // about 14 s
  assert.ok(p.chip('effort', 'xhigh').classList.contains('pending'));
  await p.tick(); await p.tick();                                          // about 20 s and a bit
  assert.ok(!p.chip('effort', 'xhigh').classList.contains('pending'), 'aged out at PENDING_TTL');
  assert.equal(p.toasts.length, 1);
  assert.match(p.toasts[0].text, /\/effort xhigh not confirmed by the statusline/);
});

test('a pending_cmd stamp that does not parse counts as fresh (and never toasts)', async () => {
  const p = await page({ session: row({ flags: { pending_cmd: { cmd: 'rename', arg: 'y', at: 'not a time', before: {} } } }) });
  assert.ok(p.chip('rename').classList.contains('pending'));
  for (let i = 0; i < 12; i += 1) await p.tick();                          // a minute
  assert.ok(p.chip('rename').classList.contains('pending'));
  assert.equal(p.toasts.length, 0);
  p.row = row({ flags: { pending_cmd: { cmd: 'rename', arg: 'y', before: {} } } });
  await p.tick();
  assert.ok(p.chip('rename').classList.contains('pending'), 'no stamp at all: fresh');
});

test('a stale server pending_cmd for the command this page just typed does not toast on top of the page\'s own timer', async () => {
  const p = await page();
  await p.click(p.chip('compact'));
  p.row = row({ flags: { pending_cmd: { cmd: 'compact', arg: null, at: iso(p, 40000), before: {} } } });
  await p.tick();
  assert.ok(p.chip('compact').classList.contains('pending'), 'this page is still waiting on its own send');
  assert.equal(p.toasts.length, 0);
});

// ---------------------------------------------------------------- one row: the fade, the first scroll, the collapse toggle

test('the edge fade: more-r while the row has more to its right, more-l once it is scrolled, neither when it all fits; it follows a scroll, a poll and a resize', async () => {
  const p = await page();
  const tune = p.tune();
  const row1 = tune.querySelector('.tune-row');
  const fade = () => ['more-l', 'more-r'].filter((c) => tune.classList.contains(c));
  const at = (left, width = 1100) => { Object.assign(row1, { scrollWidth: width, clientWidth: 374, scrollLeft: left }); };
  at(0);
  row1.dispatchEvent({ type: 'scroll' });
  assert.deepEqual(fade(), ['more-r'], 'the start of a 1100 px row in a 374 px box: more behind the right edge');
  at(300);
  row1.dispatchEvent({ type: 'scroll' });
  assert.deepEqual(fade(), ['more-l', 'more-r'], 'in the middle: both');
  at(726);
  row1.dispatchEvent({ type: 'scroll' });
  assert.deepEqual(fade(), ['more-l'], 'at the end: only the left');
  at(0, 375);
  row1.dispatchEvent({ type: 'scroll' });
  assert.deepEqual(fade(), [], 'a pixel of rounding is not "more"');
  at(0);
  await p.tick();
  assert.deepEqual(fade(), ['more-r'], 'every poll recomputes it (Clear turning into "Confirm clear" widens the row)');
  at(0, 374);
  p.w.fire('resize');
  assert.deepEqual(fade(), [], 'a resize that makes it all fit takes the fade away');
  at(0);
  p.w.fire('resize');
  assert.deepEqual(fade(), ['more-r']);
});

test('the first tap on Clear (the label gets longer) recomputes the fade', async () => {
  const p = await page();
  const row1 = p.tune().querySelector('.tune-row');
  Object.assign(row1, { scrollWidth: 380, clientWidth: 374, scrollLeft: 0 });
  assert.ok(!p.tune().classList.contains('more-r'));
  Object.assign(row1, { scrollWidth: 430 });                              // what "Confirm clear" does to the row
  await p.click(p.chip('clear'));
  assert.ok(p.tune().classList.contains('more-r'));
});

test('the current effort chip, the Model label and the current model chip are scrolled into view once, nearest edge only, never smooth', async () => {
  const scrolls = [];
  const p = await page({ scrolls });
  assert.deepEqual(scrolls.map((x) => x.text), ['high', 'model', 'opus'], 'effort chip, then the Model label (it says what the four chips are), then the model chip');
  for (const x of scrolls) assert.deepEqual(x.opts, { inline: 'nearest', block: 'nearest' }, 'no behavior key: an instant jump');
  assert.ok(scrolls[1].cls.includes('tune-lbl'));
  await p.tick(); await p.tick();
  p.row = row({ stats: { model: 'Haiku 4.5', model_id: 'claude-haiku-4-5', effort: 'max' } });
  await p.tick();
  assert.equal(scrolls.length, 3, 'once: later polls (and a changed model) never yank the row from under a finger');
});

test('the first scroll waits for the statusline to say what is current', async () => {
  const scrolls = [];
  const p = await page({ scrolls, session: row({ stats: {} }) });
  assert.deepEqual(scrolls, [], 'nothing is current yet');
  await p.tick();
  assert.deepEqual(scrolls, []);
  p.row = row({ stats: { effort: 'xhigh' } });                             // only the effort is known
  await p.tick();
  assert.deepEqual(scrolls.map((x) => x.text), ['xhigh']);
  p.row = row({ stats: { effort: 'xhigh', model: 'Opus 5' } });
  await p.tick();
  assert.equal(scrolls.length, 1, 'once');
});

test('the header `tune` button collapses and restores the strip; the choice is stored under ccboard:term:tune', async () => {
  const stored = (p) => p.w.localStorage.getItem('ccboard:term:tune');
  const p = await page({ innerHeight: 844 });
  const btn = p.toggle();
  assert.ok(btn, 'a button in #headtools');
  assert.equal(btn.parentNode, p.headtools);
  assert.equal(btn.textContent, 'tune');
  assert.equal(btn.getAttribute('type'), 'button');
  assert.ok(btn.getAttribute('aria-label'));
  assert.ok(!btn.classList.contains('hidden'));
  assert.equal(btn.getAttribute('aria-pressed'), 'true');
  assert.ok(!p.tune().classList.contains('collapsed'));
  assert.equal(stored(p), null, 'nothing is stored until the person chooses');
  await p.click(btn);
  assert.ok(p.tune().classList.contains('collapsed'));
  assert.ok(!p.tune().classList.contains('hidden'), 'collapsed is not the page\'s own hidden state');
  assert.equal(stored(p), '0');
  assert.equal(btn.getAttribute('aria-pressed'), 'false');
  await p.tick(); await p.tick();
  assert.ok(p.tune().classList.contains('collapsed'), 'a poll never undoes the choice');
  assert.ok(!btn.classList.contains('hidden'), 'the toggle stays, so the strip can come back');
  await p.click(btn);
  assert.ok(!p.tune().classList.contains('collapsed'));
  assert.equal(stored(p), '1');
  assert.equal(btn.getAttribute('aria-pressed'), 'true');
});

test('the strip starts shown in a window 700 px tall or more and hidden below that; a stored choice beats the default; an unknown height counts as tall', async () => {
  const collapsed = (p) => p.tune().classList.contains('collapsed');
  assert.equal(collapsed(await page({ innerHeight: 844 })), false);
  assert.equal(collapsed(await page({ innerHeight: 700 })), false, '700 is tall enough');
  assert.equal(collapsed(await page({ innerHeight: 699 })), true);
  const phone = await page({ innerHeight: 664 });                          // iOS Safari with its toolbar
  assert.equal(collapsed(phone), true);
  assert.equal(phone.toggle().getAttribute('aria-pressed'), 'false');
  assert.ok(!phone.toggle().classList.contains('hidden'), 'the way back is on screen');
  await phone.click(phone.toggle());
  assert.equal(collapsed(phone), false);
  assert.equal(phone.w.localStorage.getItem('ccboard:term:tune'), '1');
  assert.equal(collapsed(await page({ innerHeight: 600, tune: '1' })), false, 'stored 1 shows it in a short window');
  assert.equal(collapsed(await page({ innerHeight: 1000, tune: '0' })), true, 'stored 0 hides it in a tall one');
  assert.equal(collapsed(await page({ innerHeight: 600, tune: 'junk' })), true, 'anything else is no choice: the default');
  assert.equal(collapsed(await page({ innerHeight: null })), false);
});

test('the toggle only shows where there is a strip: a shell row and a gone session have neither', async () => {
  const shell = await page({ session: row({ agent: 'shell' }) });
  assert.ok(shell.toggle().classList.contains('hidden'));
  const p = await page();
  assert.ok(!p.toggle().classList.contains('hidden'));
  p.routes['GET '] = () => httpError(404, 'session x not found', { error: 'session x not found' });
  await p.tick();
  assert.ok(p.toggle().classList.contains('hidden'));
});

test('a strip that starts collapsed does its first scroll when the person opens it, not before; reopening after a collapse does it again', async () => {
  const scrolls = [];
  const p = await page({ innerHeight: 600, scrolls });
  assert.deepEqual(scrolls, [], 'a collapsed strip has nothing to scroll');
  await p.click(p.toggle());
  assert.deepEqual(scrolls.map((x) => x.text), ['high', 'model', 'opus']);
  await p.tick(); await p.tick();
  assert.equal(scrolls.length, 3, 'polls do not repeat it');
  await p.click(p.toggle());                                              // collapse
  assert.equal(scrolls.length, 3);
  await p.click(p.toggle());                                              // reopen: a display:none row forgot its scroll position
  assert.deepEqual(scrolls.slice(3).map((x) => x.text), ['high', 'model', 'opus']);
});

// ---- term.css: node has no layout, so the rules that make the strip one row (and lay it out in the side column) are pinned as text

const termCss = () => fs.readFileSync(path.join(STATIC, 'term.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
const rule = (css, selector) => {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const m = new RegExp('(?:^|[\\n}])\\s*' + esc + '\\s*\\{([^}]*)\\}').exec(css);
  return m ? m[1].replace(/\s+/g, ' ').trim() : null;
};
const mediaBlock = (css, query) => {
  const at = css.indexOf('@media ' + query);
  assert.ok(at >= 0, query);
  let depth = 0;
  for (let i = css.indexOf('{', at); i < css.length; i += 1) {
    if (css[i] === '{') depth += 1;
    if (css[i] === '}' && --depth === 0) return css.slice(css.indexOf('{', at) + 1, i);
  }
  throw new Error('unbalanced');
};

test('term.css: #tune is one scrolling row (not a column of rows), the toggle\'s `collapsed` hides it, the edge fades are pseudo-elements on #tune', () => {
  const css = termCss();
  const tune = rule(css, '#tune');
  assert.ok(tune && !/flex-direction:\s*column/.test(tune) && !/display:\s*flex/.test(tune), 'two stacked rows cost 101 px of the terminal at 390: #tune is a plain box around ONE row');
  assert.match(tune, /position:\s*relative/, 'the fades are positioned against #tune');
  const row = rule(css, '.tune-row');
  assert.match(row, /display:\s*flex/);
  assert.match(row, /overflow-x:\s*auto/, 'the row scrolls sideways');
  assert.doesNotMatch(row, /flex-wrap:\s*wrap/, 'below 840 px it does not wrap');
  assert.match(rule(css, '.tune-seg'), /flex:\s*0 0 auto/, 'a segment keeps its width and scrolls with the row');
  assert.match(rule(css, '#tune.hidden, #tune.collapsed'), /display:\s*none/, 'collapsed (the header button) and hidden (the page) both hide it');
  assert.match(rule(css, '#tune::before, #tune::after'), /pointer-events:\s*none/, 'a fade never eats a tap');
  assert.match(rule(css, '#tune.more-l::before, #tune.more-r::after'), /opacity:\s*1/, 'the classes term.js toggles show the fades');
  assert.match(css, /#tune::before\s*\{[^}]*left:\s*0/);
  assert.match(css, /#tune::after\s*\{[^}]*right:\s*0/);
  for (const sel of ['.bp5-dark #tune .bp5-button.tune-chip:not([class*=bp5-intent-])']) assert.match(rule(css, sel), /min-height:\s*var\(--tap\)/, '44 px on a coarse pointer comes from --tap');
});

test('term.css at 840 px and up: an inspector column (320 to 360 px): segmented effort and model over a three-column command grid, a six-column key grid, the reply block at the bottom', () => {
  const css = termCss();
  const wide = mediaBlock(css, '(min-width:840px)');
  const main = rule(wide, '#termmain');
  assert.match(main, /grid-template-columns:\s*minmax\(0, 1fr\) clamp\(320px, 27vw, 360px\)/, 'the column grows with the window, 320 to 360 px');
  assert.match(main, /grid-template-areas:\s*"tty ctx" "tty tune" "tty keys" "tty fill" "tty quick" "tty send"/, 'the free room sits above the reply block: quick replies touch the composer');
  // the commands are a grid of three equal columns, nothing scrolls and nothing fades
  const row = rule(wide, '.tune-row');
  assert.match(row, /display:\s*grid/);
  assert.match(row, /grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(row, /overflow:\s*visible/);
  assert.match(rule(wide, '.tune-row::after, #tune::before, #tune::after'), /display:\s*none/);
  assert.match(rule(wide, '.tune-row::before'), /content:\s*'commands'/, 'the grid is labelled');
  assert.match(rule(wide, '.bp5-dark #tune .tune-row > .bp5-button.tune-chip.destructive:not([class*=bp5-intent-])'), /order:\s*2/, 'Clear closes the grid');
  // effort and model: one line each, above the commands, as a segmented control
  const seg = rule(wide, '.tune-seg');
  assert.match(seg, /grid-column:\s*1 \/ -1/, 'a segment takes the whole width of the column');
  assert.match(seg, /order:\s*-1/, 'what is set comes before what can be run');
  assert.match(seg, /max-width:\s*100%/);
  assert.match(seg, /min-width:\s*0/);
  assert.match(seg, /flex-wrap:\s*wrap/, 'the label takes its own line above the track');
  const opt = rule(wide, '.bp5-dark #tune .tune-seg .bp5-button.tune-opt:not([class*=bp5-intent-])');
  assert.match(opt, /flex:\s*1 1 auto/, 'an option is as wide as its word plus a share of the rest: ultracode fits, low does not waste room');
  assert.match(opt, /min-width:\s*0/);
  assert.match(opt, /margin-left:\s*-1px/, 'neighbours share one border');
  assert.match(opt, /border-radius:\s*0/, 'only the two ends of the track are rounded');
  assert.match(rule(wide, '.bp5-dark #tune .tune-seg .bp5-button.tune-opt.on:not([class*=bp5-intent-])'), /font-weight:\s*500/, 'choosing an option never changes its width');
  // the keys: both rows on one six-column grid, Enter two tracks wide (not in compact mode, where More joins the row)
  assert.match(rule(wide, '.kb-row, .kb-r2, .kb.compact .kb-r1'), /grid-template-columns:\s*repeat\(6, minmax\(0, 1fr\)\)/);
  assert.match(rule(wide, '.kb:not(.compact) .kb-r1 .kb-key[data-key=Enter]'), /grid-column:\s*span 2/);
  assert.match(rule(wide, '.bp5-dark .bp5-button.kb-key:not([class*=bp5-intent-])'), /min-height:\s*var\(--key-h\)/);
  assert.match(rule(wide, 'body.term'), /--key-h:\s*44px/, 'a touch screen keeps 44 px keys');
  assert.match(rule(mediaBlock(css, '(min-width:840px) and (pointer:fine)'), 'html:not(.force-coarse) body.term'), /--key-h:\s*32px/, 'a mouse gets denser keys, never under force-coarse');
  // the reply block: chips wrap under their label, the pencil shares the label's line
  assert.match(rule(wide, '#quickrow'), /flex-wrap:\s*wrap/);
  assert.match(rule(wide, '#quickrow::before'), /content:\s*'quick replies'/);
  const quick = rule(wide, '#quick, #quick.fade');
  assert.match(quick, /flex-wrap:\s*wrap/);
  assert.match(quick, /mask-image:\s*none/, 'nothing is cut off, so nothing fades');
  // a low window gives room back instead of pushing the composer off the column
  const low = mediaBlock(css, '(min-width:840px) and (max-height:700px)');
  assert.match(rule(low, '#quick, #quick.fade'), /flex-wrap:\s*nowrap/, 'one scrolling row of chips again');
  assert.match(rule(low, '#quickrow::before'), /display:\s*none/);
  assert.match(rule(mediaBlock(css, '(min-width:840px) and (max-height:600px)'), '#ctxstrip'), /display:\s*none/);
  assert.match(rule(mediaBlock(css, '(min-width:840px) and (max-height:860px) and (pointer:coarse)'), '#quick, #quick.fade'), /flex-wrap:\s*nowrap/, '44 px controls need the room earlier');
});

test('the tuning strip starts shown in the side column with a mouse down to 560 px of height; a phone and a touch screen keep 700', async () => {
  const src = fs.readFileSync(path.join(STATIC, 'term.js'), 'utf8');
  assert.match(src, /const TUNE_TALL = 700;/);
  assert.match(src, /const TUNE_TALL_WIDE = 560;/);
  assert.match(src, /function tuneTall\(\)/);
  assert.match(src, /h < tuneTall\(\)/, 'the default visibility asks tuneTall, not the constant');
});

// ---------------------------------------------------------------- rename

test('Rename opens a small dialog with one input (prefilled), posts {cmd:"rename", arg} on submit and sends nothing for an empty name', async () => {
  const p = await page();
  await p.click(p.chip('rename'));
  assert.deepEqual(p.commands(), [], 'the chip only opens the dialog');
  const dlg = p.dialog('rename-dlg');
  assert.ok(dlg && dlg.open);
  assert.equal(dlg.tagName, 'DIALOG');
  const inputs = dlg.querySelectorAll('input');
  assert.equal(inputs.length, 1, 'one input');
  assert.equal(inputs[0].value, 'login work', 'prefilled with the statusline session name');
  const form = dlg.querySelector('form');
  inputs[0].value = '   ';
  form.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.deepEqual(p.commands(), []);
  assert.equal(p.toasts.length, 0, 'the error is inline, never only a toast');
  assert.equal(dlg.querySelector('.rn-err').textContent, 'Type a name first.');
  assert.ok(dlg.querySelector('.rn-err').classList.contains('bad'), '--bad-fg text');
  assert.equal(dlg.querySelector('.rn-err').getAttribute('role'), 'alert');
  assert.equal(inputs[0].getAttribute('aria-invalid'), 'true');
  assert.equal(dlg.querySelector('label.rn-label').getAttribute('for'), inputs[0].getAttribute('id'), 'the label sits above its input');
  assert.ok(dlg.open, 'an empty name keeps the dialog');
  inputs[0].value = 'x';
  inputs[0].dispatchEvent({ type: 'input' });
  assert.equal(dlg.querySelector('.rn-err').textContent, '', 'typing clears the error');
  assert.equal(inputs[0].getAttribute('aria-invalid'), null);
  inputs[0].value = '  api cleanup  ';
  form.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle(); await settle();
  assert.deepEqual(p.commands(), [{ cmd: 'rename', arg: 'api cleanup' }]);
  assert.ok(!dlg.open);
  assert.ok(p.chip('rename').classList.contains('pending'));
});

test('Cancel closes the rename dialog without sending', async () => {
  const p = await page();
  await p.click(p.chip('rename'));
  const dlg = p.dialog('rename-dlg');
  await p.click(dlg.querySelectorAll('button').find((b) => b.textContent === 'Cancel'));
  assert.ok(!dlg.open);
  assert.deepEqual(p.commands(), []);
});

// ---------------------------------------------------------------- the registry

test('the registry is fetched once per page load and cached in sessionStorage for 10 minutes', async () => {
  const p = await page();
  const fetches = () => p.calls.filter((c) => c.path === '/api/agents').length;
  assert.equal(fetches(), 1);
  await p.tick(); await p.tick();
  assert.equal(fetches(), 1, 'not again on later polls');
  const cached = JSON.parse(p.w.sessionStorage.getItem('ccboard:agents'));
  assert.equal(typeof cached.at, 'number');
  assert.deepEqual(Object.keys(cached.agents), ['claude']);
  assert.equal(cached.agents.claude.slash.compact.weight, 120);
  assert.equal(p.TermPage.AGENTS_KEY, 'ccboard:agents');
});

test('a fresh cache means no fetch at all; a stale one is ignored', async () => {
  const fresh = await page({ cache: { at: 0, agents: { claude: { slash: { compact: REGISTRY.compact } } } } });
  assert.equal(fresh.calls.filter((c) => c.path === '/api/agents').length, 0);
  assert.deepEqual(fresh.chips(), ['compact'], 'the cached registry drives the strip');
  const stale = await page({ cache: { at: -11 * 60 * 1000, agents: { claude: { slash: { compact: REGISTRY.compact } } } } });
  assert.equal(stale.calls.filter((c) => c.path === '/api/agents').length, 1);
  assert.ok(stale.chips().includes('usage'));
});

test('a registry that arrives after the first paint replaces the embedded list; a failed fetch keeps it', async () => {
  const p = await page({ agents: { claude: { slash: { compact: REGISTRY.compact, usage: REGISTRY.usage } } } });
  assert.deepEqual(p.cmdChips(), ['Compact', 'Usage']);
  const down = await page({ agentsError: httpError(500, 'boom') });
  assert.equal(down.cmdChips().length, 6, 'the embedded list: Clear Compact Usage Rename Context Status');
  assert.deepEqual(down.cmdChips(), ['Clear', 'Compact', 'Usage', 'Rename', 'Context', 'Status']);
  assert.equal(down.toasts.length, 0, 'a failed registry fetch is silent');
});

test('a shell row never fetches the registry', async () => {
  const p = await page({ session: row({ agent: 'shell' }) });
  assert.equal(p.calls.filter((c) => c.path === '/api/agents').length, 0);
});

// ---------------------------------------------------------------- the send box

async function submit(p, text) {
  p.sendtext.value = text;
  p.sendform.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle(); await settle();
}

test('send button label: Send at the prompt, queue while a turn runs', async () => {
  const p = await page();
  assert.equal(p.sendbtn.textContent, 'Send');
  p.row = row({ state: 'working' });
  await p.tick();
  assert.equal(p.sendbtn.textContent, 'queue');
  p.row = row({ state: 'done' });
  await p.tick();
  assert.equal(p.sendbtn.textContent, 'Send');
});

test('a working row posts /prompt with queue:true, keeps multi-line text intact and clears the box', async () => {
  const p = await page({ session: row({ state: 'working' }) });
  await submit(p, 'then add tests\nand docs');
  assert.deepEqual(p.posts('/prompt'), [{ text: 'then add tests\nand docs', enter: true, queue: true }]);
  assert.deepEqual(p.posts('/keys'), []);
  assert.equal(p.sendtext.value, '');
  assert.ok(p.toasts.some((t) => t.text === 'queued' && t.kind === 'ok'));
});

test('an idle row posts /prompt with queue:false', async () => {
  const p = await page();
  await submit(p, 'run the tests');
  assert.deepEqual(p.posts('/prompt'), [{ text: 'run the tests', enter: true, queue: false }]);
  assert.deepEqual(p.posts('/keys'), []);
});

test('an older server (404 on /prompt) falls back to /keys with the same text', async () => {
  const p = await page();
  p.routes['POST /prompt'] = () => httpError(404, '404 Not Found', { detail: 'Not Found' });
  await submit(p, 'hello\nworld');
  assert.equal(p.posts('/prompt').length, 1);
  assert.deepEqual(p.posts('/keys'), [{ text: 'hello\nworld', enter: true }]);
  assert.equal(p.sendtext.value, '');
});

test('a dialog, a shell and an unreported row keep the raw /keys path (typed text answers the dialog)', async () => {
  for (const over of [{ state: 'waiting', flags: { wait_kind: 'permission' }, pending: [{ id: 1 }] }, { state: 'waiting', flags: { wait_kind: 'elicitation' } }, { agent: 'shell' }, { state: null }]) {
    const p = await page({ session: row(over) });
    await submit(p, '2');
    assert.deepEqual(p.posts('/prompt'), [], JSON.stringify(over));
    assert.deepEqual(p.posts('/keys'), [{ text: '2', enter: true }], JSON.stringify(over));
  }
});

test('an idle row that started a turn since the last poll: the 409 "working" is retried once as a queued prompt', async () => {
  const p = await page();
  let n = 0;
  p.routes['POST /prompt'] = (b) => { n += 1; return b.queue ? { ok: true, pasted: true, queued: true } : httpError(409, 'working', { error: 'working', message: 'the session is working', retry: 5 }); };
  await submit(p, 'go on');
  assert.deepEqual(p.posts('/prompt'), [{ text: 'go on', enter: true, queue: false }, { text: 'go on', enter: true, queue: true }]);
  assert.equal(n, 2);
  assert.equal(p.sendtext.value, '');
});

test('a refused prompt (compacting) toasts the reason and gives the text back to the box', async () => {
  const p = await page({ session: row({ state: 'working', flags: { compacting: true } }) });
  p.routes['POST /prompt'] = () => httpError(409, 'compacting', { error: 'compacting', message: 'the session is compacting', retry: 15 });
  await submit(p, 'one more thing');
  assert.equal(p.posts('/prompt').length, 1);
  assert.equal(p.sendtext.value, 'one more thing', 'nothing is lost');
  assert.match(p.toasts.at(-1).text, /the session is compacting · try again in 15 s/);
  assert.deepEqual(p.posts('/keys'), []);
});

test('an empty send box plus Enter still forwards a bare Enter through /keys', async () => {
  const p = await page();
  await submit(p, '   ');
  assert.deepEqual(p.posts('/keys'), [{ keys: ['Enter'] }]);
  assert.deepEqual(p.posts('/prompt'), []);
});

test('the prompt waits for the Escape that closes an open readout', async () => {
  const p = await page();
  p.routes['POST /command'] = () => ({ ok: true, screen: 'u' });
  await p.click(p.chip('usage'));
  p.dialog('readout').close();
  await settle();
  p.sendtext.value = 'next';
  p.sendform.dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.equal(p.posts('/prompt').length, 0, 'held back by the gap');
  await settleAll(p);
  const order = p.calls.filter((c) => c.method === 'POST').map((c) => c.path.replace(API, ''));
  assert.deepEqual(order, ['/command', '/keys', '/prompt']);
});

// ---------------------------------------------------------------- the page keeps its other promises

test('the strip steps aside for a session that is gone', async () => {
  const p = await page();
  assert.ok(!p.tune().classList.contains('hidden'));
  p.routes['GET '] = () => httpError(404, 'session x not found', { error: 'session x not found' });
  await p.tick();
  assert.ok(p.tune().classList.contains('hidden'));
});

test('every chip, the row, the labels, the toggle and the strip itself cancel pointerdown so the terminal and the composer keep focus', async () => {
  const p = await page();
  const down = (n) => { let prevented = 0; n.dispatchEvent({ type: 'pointerdown', preventDefault() { prevented += 1; } }); return prevented; };
  for (const b of p.tune().querySelectorAll('button')) assert.ok(down(b) >= 1, b.textContent);
  assert.ok(down(p.tune()) >= 1, 'a tap in a gap of the strip');
  assert.ok(down(p.tune().querySelector('.tune-row')) >= 1, 'a tap on the row');
  assert.ok(down(p.tune().querySelector('.tune-seg')) >= 1, 'a tap on a segment');
  assert.ok(down(p.tune().querySelector('.tune-lbl')) >= 1, 'a tap on a label');
  assert.ok(down(p.toggle()) >= 1, 'the header toggle must not blur the terminal either');
  p.row = row({ state: 'working' });                                     // dimmed chips are pointer-events:none: the tap lands on the strip
  await p.tick();
  assert.ok(down(p.tune().querySelector('.tune-row')) >= 1);
  assert.ok(down(p.tune()) >= 1);
});

// ---------------------------------------------------------------- the merge: quick row on the shared editor, hierarchy, hues

test('quick row: the chips come from quickLoad (the shared list, key ccboard:quick:<session>), and the page has no page-local editor dialog', async () => {
  const p = await page();
  assert.deepEqual(p.quickChips(), ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'], 'the agent defaults');
  const seeded = await page({ quick: ['go', 'ship it'] });
  assert.deepEqual(seeded.quickChips(), ['go', 'ship it']);
  assert.equal(seeded.w.document.querySelector('#qedit'), null, 'the polish pass\'s page-local #qedit dialog is gone');
  assert.equal(seeded.w.document.querySelector('dialog.qedit'), null);
});

test('quick row: y / yes / n / no chips are hidden while a permission waits (the amber y / n row answers it) and come back when it is answered', async () => {
  const p = await page({ quick: ['y', 'yes', 'no', 'go', 'N'] });                // the defaults hold none of these, so seed a list that does
  assert.deepEqual(p.quickChips(), ['y', 'yes', 'no', 'go', 'N'], 'nothing pending: all of them');
  p.row = row({ state: 'waiting', flags: { wait_kind: 'permission' }, pending: [{ id: 1, tool_name: 'Bash', summary: 'Bash: ls' }] });
  await p.tick();
  assert.deepEqual(p.quickChips(), ['go'], 'a permission is pending: only the replies that are not an approval');
  assert.ok(p.w.document.querySelector('#keyhost .kb').classList.contains('approval'), 'and the key bar shows its amber y / n row');
  p.row = row();
  await p.tick();
  assert.deepEqual(p.quickChips(), ['y', 'yes', 'no', 'go', 'N'], 'answered: the chips return');
});

test('quick row: the pencil opens the SHARED editor (dialog.qr-editor), Save writes ccboard:quick:<session> and repaints the row; the defaults remove the key', async () => {
  const p = await page();
  const pencil = p.termmain.querySelector('#qtools button');
  assert.equal(pencil.getAttribute('title'), 'Edit quick replies');
  await p.click(pencil);
  const dlg = p.dialog('qr-editor');
  assert.ok(dlg && dlg.open, 'components.js quickReplyEditor');
  const ins = dlg.querySelectorAll('input');
  ins[0].value = 'carry on';
  dlg.querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.deepEqual(JSON.parse(p.w.localStorage.getItem('ccboard:quick:' + NAME)), ['carry on', 'merge', 'push', 'pr', 'add commit push', 'do it']);
  assert.deepEqual(p.quickChips(), ['carry on', 'merge', 'push', 'pr', 'add commit push', 'do it']);
  await p.click(pencil);
  p.dialog('qr-editor').querySelector('.qr-reset').click();
  p.dialog('qr-editor').querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.equal(p.w.localStorage.getItem('ccboard:quick:' + NAME), null, 'reset + save = the defaults again, no key');
});

test('quick row: the editor refuses a reply over 200 characters with an inline error and keeps the stored list', async () => {
  const p = await page({ quick: ['keep'] });
  await p.click(p.termmain.querySelector('#qtools button'));
  const dlg = p.dialog('qr-editor');
  dlg.querySelectorAll('input')[0].value = 'x'.repeat(201);
  dlg.querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
  await settle();
  assert.ok(dlg.open, 'Save did not close it');
  assert.match(dlg.querySelector('.qr-err').textContent, /at most 200 characters/);
  assert.deepEqual(JSON.parse(p.w.localStorage.getItem('ccboard:quick:' + NAME)), ['keep'], 'nothing was stored');
});

test('tuning strip: the current effort is the accent TINT (never a second filled primary); the current model wears its own muted hue, the others stay neutral; Clear is red-outlined, armed it is the solid fill', async () => {
  const css = fs.readFileSync(path.join(STATIC, 'term.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rule = (sel) => { const m = css.split('}').map((r) => r.split('{')).find((r) => r[0] && r[0].replace(/\s+/g, ' ').trim() === sel); return m ? m[1].replace(/\s+/g, ' ') : ''; };
  const on = rule('.bp5-dark #tune .bp5-button.tune-chip.on:not([class*=bp5-intent-])');
  assert.match(on, /background:\s*var\(--sig-bg\)/, 'tinted');
  assert.match(on, /color:\s*var\(--sig\)/);
  assert.match(on, /border-color:\s*var\(--sig-bd\)/);
  assert.doesNotMatch(on, /background:\s*var\(--sig\)/, 'not filled');
  assert.match(rule('.bp5-dark #tune .bp5-button.tune-chip:not([class*=bp5-intent-])'), /min-height:\s*var\(--tap\)/);
  assert.doesNotMatch(rule('.bp5-dark #tune .bp5-button.tune-chip:not([class*=bp5-intent-])'), /box-shadow/, 'one border (the default button\'s --line-strong), not a second ring');
  assert.match(rule('.bp5-dark #tune .bp5-button.tune-chip.destructive:not([class*=bp5-intent-])'), /border-color:\s*var\(--bad\)/);
  assert.match(rule('.bp5-dark #tune .bp5-button.tune-chip.confirm:not([class*=bp5-intent-])'), /background:\s*var\(--bad-solid\)/);
  const model = rule('.bp5-dark #tune .bp5-button.tune-model.on:not([class*=bp5-intent-])');
  assert.match(model, /color:\s*var\(--hue/, 'the current model is tinted with its own hue');
  assert.match(model, /background:\s*var\(--hue-bg/);
  assert.equal(rule('.bp5-dark #tune .bp5-button.tune-model:not([class*=bp5-intent-])'), '', 'a model that is not set has no colour of its own: one coloured chip per group');
  assert.equal(css.includes('qedit'), false, 'the page-local quick editor\'s CSS is gone');
  const p = await page();
  const hueOf = (v) => plain(p.chip('model', v).className.split(/\s+/)).find((c) => /^hue-/.test(c));
  assert.deepEqual(['opus', 'fable', 'sonnet', 'haiku'].map(hueOf), ['hue-blue', 'hue-violet', 'hue-green', 'hue-slate']);
  for (const v of ['opus', 'fable', 'sonnet', 'haiku']) assert.ok(p.chip('model', v).classList.contains('tune-model'));
  assert.ok(!p.chip('effort', 'high').classList.contains('tune-model'), 'effort chips carry no hue');
  assert.ok(p.chip('clear').classList.contains('destructive'));
  assert.ok(!p.chip('compact').classList.contains('destructive'));
  assert.ok(p.chip('model', 'opus').classList.contains('on') && p.chip('effort', 'high').classList.contains('on'), 'the statusline marks the current ones');
  const drift = homeWorld().w;                                       // pages/agents.js chipHue is the source of the hue table: the page's own copy must not drift from it
  for (const [model, cls] of Object.entries(plain(p.TermPage.MODEL_HUE))) assert.equal(drift.run(`chipHue('model', ${JSON.stringify(model)})`), cls, model);
});
