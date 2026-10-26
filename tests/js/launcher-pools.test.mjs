import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import { makeWorld, plain, STATIC } from './harness.mjs';
import { installDom } from './minidom.mjs';

const accounts = [{ id: 'claude-a', label: 'Claude A', provider: 'claude', state: 'ready' }, { id: 'codex-a', label: 'Codex A', provider: 'codex', state: 'ready' }];
const pools = [{ id: 'claude-work', name: 'Claude work', provider: 'claude', members: [{ account_id: 'claude-a' }], soft_pct_5h: 70, soft_pct_weekly: 80, hard_pct_5h: 85, hard_pct_weekly: 92, concurrency: 2 }, { id: 'codex-work', name: 'Codex work', provider: 'codex', members: [], soft_pct_5h: 70, soft_pct_weekly: 80, hard_pct_5h: 85, hard_pct_weekly: 92, concurrency: 2 }];
const answer = (label = 'Claude A') => ({ will_start_as: { account_id: 'claude-a', account_label: label }, refused_reason: null, candidates: [{ account_id: 'claude-a', label, pct_5h: 12, pct_weekly: 23, resets: { '5h': '2026-10-26T14:20:00Z', weekly: null }, eligible: true, why_not: null }] });
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
function world(on = true) {
  const timers = new Map(); let seq = 0;
  const w = makeWorld({ setTimeout: (fn, ms) => { const id = ++seq; timers.set(id, { fn, ms }); return id; }, clearTimeout: (id) => timers.delete(id) });
  installDom(w);
  const make = w.document.createElement;
  w.document.createElement = (tag) => {
    const n = make(tag), set = n.setAttribute.bind(n);
    n.setAttribute = (k, v) => { set(k, v); if (k === 'value') n.value = String(v); };
    return n;
  };
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', 'pages/agents.js', 'shell.js', 'pages/settings.js']) w.load(f);
  Object.assign(w.ctx, { __accounts: structuredClone(accounts), __pools: structuredClone(pools), __on: on, __answer: answer(), __calls: [] });
  w.run(`
    state = { pools: __on ? { enabled: true, groups: [], pools: __pools } : null,
      agents: { claude: { installed: true, loggedIn: true }, codex: { installed: true, loggedIn: true } },
      config: {}, projects: [{ name: 'shop', path: '/srv/shop', repos: [{ name: 'api', path: '/srv/shop/api', sessions: [] }] }], tasks: [], jobs: [], runs: [] };
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      if (path === '/api/pools/accounts') return __accounts;
      if (path === '/api/pools' && method === 'GET') return __pools;
      if (path === '/api/launch/dry-run') { if (typeof __answer === 'function') return __answer(body); return __answer; }
      if (globalThis.__mutate) return __mutate(method, path, body);
      return {};
    };
    poll = async () => {}; setError = () => {}; toast = () => {};
    globalThis.ctl = launcherForm({ project: 'shop', repo: 'api' });
    document.body.append(ctl.form);
  `);
  w.timers = timers;
  w.fire = async () => { for (const [id, t] of [...timers]) if (t.ms === 250) { timers.delete(id); t.fn(); } await flush(); };
  return w;
}
const change = (n, value) => { n.value = value; n.dispatchEvent({ type: 'change' }); };
const choice = (w) => w.document.querySelector('select[aria-label="Account"]');
const line = (w) => w.document.querySelector('.lx-pool-line');
const button = (n, label) => n.querySelectorAll('button').find((b) => b.textContent === label);
const submit = (n) => n.dispatchEvent({ type: 'submit', preventDefault() {} });

test('pools off adds no controls, checks or settings section', async () => {
  const w = world(false); await flush();
  assert.equal(choice(w), null);
  assert.equal(w.run('ctl.poolKit'), null);
  assert.equal(w.run('__calls.length'), 0);
  w.run('settingsPools.rows = null; globalThis.host = el("div"); settingsAcctSkeleton(host)');
  assert.equal(w.get('host').querySelector('.set-pool-manager'), null);
});

test('Codex lists only Codex accounts and pools; incompatible selection resets with a note', async () => {
  const w = world(); await flush();
  change(choice(w), 'pool:claude-work');
  assert.equal(w.run('ctl.view().on_limit_policy'), 'auto');
  w.run("ctl.V.agent = 'codex'; ctl.paint()");
  assert.deepEqual(choice(w).children.map((n) => n.textContent), ['Default login', 'Pinned: Codex A', 'Pool: Codex work']);
  assert.equal(choice(w).value, 'legacy');
  assert.match(w.document.querySelector('.lx-pools').textContent, /unavailable for this agent.*Default login/);
  change(choice(w), 'pinned:codex-a');
  assert.equal(w.run('ctl.view().on_limit_policy'), 'wait');
});

test('no accounts or pools for an agent hides the choice, shell resets it', async () => {
  const w = world(); await flush();
  change(choice(w), 'pinned:claude-a');
  w.run("ctl.V.agent = 'shell'; ctl.paint()");
  assert.ok(w.document.querySelector('.lx-pool-controls').classList.contains('hidden'));
  assert.equal(choice(w).value, 'legacy');
  assert.equal(w.run('ctl.go.disabled'), false);
});

test('dry-run debounces edits for 250 ms and ignores an older answer even before the next fetch', async () => {
  const w = world(); await flush();
  let old; w.ctx.__answer = () => new Promise((r) => { old = r; });
  change(choice(w), 'pool:claude-work');
  w.run("ctl.V.claude.model = 'sonnet'; ctl.paint(); ctl.V.claude.model = 'opus'; ctl.paint()");
  assert.equal([...w.timers.values()].filter((t) => t.ms === 250).length, 1);
  await w.fire();
  assert.equal(w.run("__calls.filter(c => c.path === '/api/launch/dry-run').length"), 1);
  w.run("ctl.V.claude.model = 'haiku'; ctl.paint()");
  old(answer('Old answer')); await flush();
  assert.doesNotMatch(line(w).textContent, /Old answer/);
  w.ctx.__answer = answer('Newest'); await w.fire();
  assert.match(line(w).textContent, /Will start as Newest/);
  const req = w.run("__calls.filter(c => c.path === '/api/launch/dry-run').at(-1).body");
  assert.equal(req.model, 'haiku'); assert.equal(req.pool_id, 'claude-work');
});

test('out-of-order completion cannot replace the latest answer', async () => {
  const w = world(); await flush(); let old;
  w.ctx.__answer = () => new Promise((r) => { old = r; });
  change(choice(w), 'pool:claude-work'); await w.fire();
  w.ctx.__answer = answer('Current');
  change(w.document.querySelectorAll('select').find(n => n.getAttribute('aria-label') === 'On limit'), 'ask'); await w.fire();
  old(answer('Old')); await flush(); assert.match(line(w).textContent, /Current/);
});

test('refusal disables Start, gives a visible reason beside it and blocks Enter', async () => {
  const w = world(); await flush();
  w.ctx.__answer = { will_start_as: null, refused_reason: 'No account below soft thresholds', candidates: [] };
  change(choice(w), 'pool:claude-work'); await w.fire();
  assert.equal(w.run('ctl.go.disabled'), true);
  assert.match(w.document.querySelector('.lx-foot .lx-pool-refusal').textContent, /No account below/);
  assert.equal(w.document.querySelector('.lx-pool-refusal').classList.contains('hidden'), false);
  await w.run('ctl.submit()');
  assert.equal(w.run("__calls.filter(c => /sessions$/.test(c.path)).length"), 0);
});

test('fetch failure blocks pinned and pool, permits Default login, and handles malformed replies', async () => {
  const w = world(); await flush(); w.ctx.__answer = () => { throw new Error('offline'); };
  for (const v of ['pool:claude-work', 'pinned:claude-a', 'legacy']) {
    change(choice(w), v); await w.fire();
    assert.match(line(w).textContent, /Could not check the account/);
    assert.equal(w.run('ctl.go.disabled'), v !== 'legacy');
  }
  w.ctx.__answer = {}; change(choice(w), 'pool:claude-work'); await w.fire();
  assert.equal(w.run('ctl.go.disabled'), true);
});

test('candidate details are keyboard-native, show windows, resets and exclusion text safely', async () => {
  const w = world(); await flush();
  const r = answer('<img src=x>'); r.candidates.push({ label: 'Busy', eligible: false, why_not: 'concurrency cap 2 reached', pct_5h: null, pct_weekly: null, resets: {} });
  w.ctx.__answer = r; change(choice(w), 'pool:claude-work'); await w.fire();
  const d = w.document.querySelector('.lx-pool-details');
  assert.equal(d.classList.contains('hidden'), false);
  assert.equal(d.querySelector('summary').textContent, 'Account candidates');
  d.setAttribute('open', ''); assert.equal(d.hasAttribute('open'), true); d.removeAttribute('open');
  assert.match(d.textContent, /5 h 12%.*weekly 23%.*Eligible/);
  assert.match(d.textContent, /Busy: 5 h unknown.*concurrency cap 2 reached/);
  assert.equal(d.querySelector('img'), null);
});

test('closing the form invalidates pending dry-runs', async () => {
  const w = world(); await flush(); let resolve;
  w.ctx.__answer = () => new Promise((r) => { resolve = r; });
  change(choice(w), 'pool:claude-work'); await w.fire(); w.run('ctl.poolKit.dispose()');
  resolve(answer('Closed')); await flush(); assert.doesNotMatch(line(w).textContent, /Closed/);
});

test('session and task payloads carry only selected assignment fields and policy', async () => {
  const w = world(); await flush(); change(choice(w), 'pinned:claude-a');
  const expected = { account_mode: 'pinned', pinned_account_id: 'claude-a', on_limit_policy: 'wait' };
  assert.deepEqual(plain(w.run('launcherPoolFields(ctl.payload())')), expected);
  assert.deepEqual(plain(w.run('launcherPoolFields(launcherTaskOpts(ctl.view()))')), expected);
  change(choice(w), 'legacy'); assert.deepEqual(plain(w.run('launcherPoolFields(ctl.payload())')), {});
});

test('remote target keeps the account control and uses the P5 dry-run refusal', async () => {
  const w = world(); await flush();
  w.run(`globalThis.remote = launcherPoolsKit({ selection: ctl.poolKit.selection, agent: () => 'claude', body: () => ({ agent:'claude', launcher:'claude', project:'shop', repo:'api', node:'build-box' }), changed: () => {} }); document.body.append(remote.node, remote.result, remote.refusal)`);
  await flush();
  change(w.get('remote').node.querySelector('select'), 'pool:claude-work');
  w.ctx.__answer = { refused_reason: 'pool accounts run on this node until account grants (P5)', will_start_as: null, candidates: [] };
  await w.fire();
  assert.match(w.run('remote.blocked()'), /P5/);
  assert.equal(w.run("__calls.filter(c => c.path === '/api/launch/dry-run').at(-1).body.node"), 'build-box');
});

test('Settings pools create, filter members, add/remove, edit thresholds and confirmed delete', async () => {
  const w = world(); await flush();
  w.run(`settingsPools.rows = __accounts; globalThis.manager = settingsPoolManager(); document.body.append(manager);
    __mutate = (method, path, body) => {
      if (method === 'POST') __pools.push({ ...body, members: [], soft_pct_5h:70, soft_pct_weekly:80 });
      if (method === 'DELETE' && !path.includes('/members/')) __pools = __pools.filter(p => path !== '/api/pools/' + p.id);
      return {};
    };`); await flush();
  const manager = w.get('manager');
  const create = manager.querySelector('.set-pool-create');
  change(create.querySelector('input'), 'New work'); submit(create); await flush();
  assert.equal(w.run('__pools.at(-1).name'), 'New work');
  assert.equal(w.run('__pools.at(-1).provider'), 'claude');
  let row = manager.querySelectorAll('.set-pool-definition').at(-1);
  const offered = row.querySelector('select');
  assert.deepEqual(offered.children.map((n) => n.textContent), ['Choose account', 'Claude A']);
  change(offered, 'claude-a'); button(row, 'Add member').click(); await flush();
  assert.ok(w.run("__calls.some(c => c.method === 'PUT' && c.path.endsWith('/members/claude-a'))"));
  row = manager.querySelector('.set-pool-definition');
  button(row, 'Remove').click(); await flush();
  assert.ok(w.run("__calls.some(c => c.method === 'DELETE' && c.path.endsWith('/members/claude-a'))"));
  row = manager.querySelector('.set-pool-definition');
  const edit = row.querySelector('form'); change(edit.querySelector('input'), '65'); submit(edit); await flush();
  assert.deepEqual(plain(w.run("__calls.find(c => c.method === 'PATCH').body")), { soft_pct_5h: 65, soft_pct_weekly: 80 });
  button(manager.querySelector('.set-pool-definition'), 'Delete').click();
  assert.equal(w.run("__calls.filter(c => c.method === 'DELETE' && !c.path.includes('/members/')).length"), 0);
  button(manager, 'Confirm Delete').click(); await flush();
  assert.equal(w.run('__pools.some(p => p.id === "claude-work")'), false);
});

test('Settings shows server 409 in-use reason and preserves the pool', async () => {
  const w = world(); await flush();
  w.run(`settingsPools.rows = __accounts; globalThis.manager = settingsPoolManager(); document.body.append(manager);
    __mutate = () => { const e = new Error('Pool is in use by session shop--api--s1'); e.status = 409; throw e; };`);
  await flush(); const m = w.get('manager');
  button(m, 'Delete').click(); button(m, 'Confirm Delete').click(); await flush();
  assert.match(m.querySelector('.set-pool-error').textContent, /Pool is in use by session/);
  assert.equal(w.run('__pools.length'), 2);
});

test('scheduled checks use schedule permissions and omit the hidden Claude model', async () => {
  const w = world(); await flush();
  w.run("ctl.poolKit.dispose(); ctl.form.remove(); ctl = launcherForm({ project:'shop', repo:'api', mode:'task' }); document.body.append(ctl.form)");
  await flush(); change(choice(w), 'pool:claude-work');
  w.run("ctl.V.common.prompt = 'Review the changes'; ctl.T.when = 'schedule'; ctl.T.jobMode = 'plan'; ctl.paint()");
  await w.fire();
  const body = plain(w.run("__calls.filter(c=>c.path === '/api/launch/dry-run').at(-1).body"));
  assert.equal(body.account_mode, 'pool'); assert.equal(body.permission_mode, 'plan');
  assert.equal(body.model, undefined); assert.equal(body.prompt, 'Review the changes');
});

test('account refresh updates Settings member menus without replacing entered thresholds', async () => {
  const w = world(); await flush();
  w.run('settingsPools.rows = __accounts; globalThis.manager = settingsPoolManager(); document.body.append(manager)'); await flush();
  const manager = w.get('manager'), input = manager.querySelector('input');
  input.value = '63';
  w.run("settingsPools.rows.push({id:'claude-new', label:'New Claude', provider:'claude'}); manager.refreshAccounts()");
  assert.equal(input.value, '63');
  assert.match(manager.querySelector('select').textContent, /New Claude/);
  assert.doesNotMatch(manager.querySelector('select').textContent, /Codex/);
});


test('pool demo is lazy, keeps the ordinary flag-off fixture and survives demo polls only when opted in', async () => {
  const w = world(false); await flush();
  assert.equal(JSON.parse(readFileSync(STATIC + '/demo/state.json', 'utf8')).pools, null);
  const fetches = [];
  w.ctx.fetch = async (url) => { fetches.push(url); return { ok: true, json: async () => JSON.parse(readFileSync(STATIC + '/demo/pools.json', 'utf8')) }; };
  w.location.search = '?demo=1&pools=1';
  await w.run("launcherPoolsDemo('GET', '/api/pools/accounts')");
  assert.deepEqual(fetches, ['/static/demo/pools.json']);
  assert.ok(w.run('demoMadeState({ pools: null, projects: [] }).pools.enabled'));
  await w.run("launcherPoolsDemo('POST', '/api/pools', { id:'demo-new', name:'New', provider:'claude' })");
  assert.ok(w.run("demoMadeState({ pools: null, projects: [] }).pools.pools.some(p => p.id === 'demo-new')"));
  w.location.search = '?demo=1';
  assert.equal(w.run('demoMadeState({ pools: null, projects: [] }).pools'), null);
});

test('bare-node account checks never reset the selected permission mode', async () => {
  const w = world(); await flush();
  w.run(`globalThis.bare = lxbForm({ ts_id:'device', name:'Device', check:{repos:['repo'], rows:[{key:'claude',ok:true}]} }, {carry:{agent:'claude'}, poolSelection:ctl.poolKit.selection}); document.body.append(bare.form)`);
  const form = w.get('bare').form;
  const mode = form.querySelectorAll('select').find((n) => n.getAttribute('aria-label') === 'Permissions');
  change(mode, 'plan'); await flush();
  const model = form.querySelectorAll('input').find((n) => n.getAttribute('placeholder') === 'Default model');
  change(model, 'sonnet'); await w.fire();
  assert.equal(mode.value, 'plan');
});

test('an inactive node form cannot reset a selection made on the visible form', async () => {
  const w = world(); await flush(); change(choice(w), 'pool:claude-work');
  w.run(`globalThis.hiddenKit = launcherPoolsKit({ current:()=>false, selection:ctl.poolKit.selection, agent:()=> 'codex', body:()=>({}), changed:()=>{} }); hiddenKit.sync()`);
  await flush();
  assert.equal(w.run('ctl.poolKit.selection.pool_id'), 'claude-work');
  assert.equal(w.run('ctl.poolKit.selection.account_mode'), 'pool');
});
