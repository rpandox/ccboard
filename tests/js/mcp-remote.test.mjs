// Settings > Agents > Connect from another device (issue #14): the switch for the remote MCP endpoint, the device tokens (two-tap Revoke), the Add device form,
// the one-time token dialog with the Claude Code and Codex commands, and the empty-URL warning. Real core.js, components.js, router.js and pages/*.js on minidom's
// DOM through tests/js/world.mjs; api() is the recorder from world.mjs, answered by a small fake of the /api/mcp routes.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { calls, fakeState, homeWorld, page, plain, text, tick } from './world.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const TOKEN = 'ccbmcp_' + 'T3st'.repeat(10) + 'abc';                 // the shape of a real one; only ever in tests
const URL0 = 'https://board.example';
const row = (over = {}) => ({ id: 'a1b2c3d4e5f6', name: 'laptop', scopes: ['read', 'tasks'], created_at: '2026-10-01T10:00:00Z', last_used_at: null, last_user: null,
  last_tool: null, expires_at: '2026-12-30T10:00:00Z', expired: false, ...over });

function mcpWorld({ enabled = false, tokens = [], publicUrl = URL0, hold = null, refuse = null } = {}) {
  const st = fakeState();
  st.config = { ...st.config, public_url: publicUrl };
  const { w } = homeWorld({ state: st });
  const server = { enabled, tokens: tokens.map((t) => ({ ...t })) };
  const view = () => ({ enabled: server.enabled, env_default: false, tokens: server.tokens.map((t) => ({ ...t })), recent: [], max_tokens: 10 });
  w.ctx.__copied = [];
  w.ctx.navigator.clipboard = { writeText: async (t) => { w.ctx.__copied.push(t); } };
  w.run('globalThis.__errors = []; setError = (m) => { if (m) __errors.push(m); }; demoOn = () => false;');
  w.run('settingsMcp.data = null; settingsMcp.at = 0; settingsMcp.err = ""; settingsMcp.busy = null; settingsMcp.node = null;');
  w.ctx.__answers['/api/mcp/remote'] = async ({ body }) => { if (hold) await hold.p; server.enabled = body.enabled; return view(); };
  w.ctx.__answers['/api/mcp/tokens'] = async ({ method, path: p, body }) => {
    if (method === 'GET') return view();
    if (method === 'POST') {
      if (hold) await hold.p;
      if (refuse) { const e = new Error(refuse); e.status = 409; throw e; }
      const rec = row({ id: 'n' + server.tokens.length, name: body.name, scopes: body.scopes });
      server.tokens.push(rec);
      return { ...view(), token: TOKEN, record: rec };
    }
    if (method === 'DELETE') {
      if (hold) await hold.p;
      const id = decodeURIComponent(p.split('/').pop());
      server.tokens = server.tokens.filter((t) => t.id !== id);
      return view();
    }
    return view();
  };
  w.location.hash = '#/settings?sec=agents';
  return { w, server };
}
const defer = () => { let resolve; const p = new Promise((r) => { resolve = r; }); return { p, resolve }; };
const block = (w) => page(w).querySelector('.settings-panel[data-sec=agents] .set-mcp');
const sw = (w) => block(w).querySelector('.mcp-switch input');
const onPart = (w) => block(w).querySelector('.mcp-on');
const rows = (w) => block(w).querySelectorAll('.set-mcp-row');
const btn = (root, label) => root.querySelectorAll('button').find((b) => text(b).trim() === label);
const dialog = (w) => w.document.querySelector('dialog.mcp-mint');
const mcpCalls = (w, method) => calls(w).filter((c) => c.path.startsWith('/api/mcp/') && (!method || c.method === method));
const isFilled = (b) => b.classList.contains('primary') && !b.classList.contains('tinted');
const hiddenUp = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const flip = (input, on) => { input.checked = on; input.dispatchEvent({ type: 'change' }); };
const submit = (w, name) => {
  block(w).querySelector('.mcp-name').value = name;
  block(w).querySelector('.mcp-form').dispatchEvent({ type: 'submit', preventDefault() {} });
};
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };

test('off: only the switch, its sentence and the box line show; no form, no list, no filled primary', async () => {
  const { w } = mcpWorld();
  await settle();
  assert.ok(block(w), 'the block sits in Settings > Agents');
  assert.equal(sw(w).checked, false);
  assert.equal(sw(w).getAttribute('role'), 'switch');
  assert.match(text(block(w)), /Allow other devices/);
  assert.match(text(block(w)), /hook token never leaves the box/);
  assert.match(text(block(w)), /Not needed on the box itself/);
  assert.ok(hiddenUp(onPart(w)), 'the list and the form are hidden while off');
  assert.equal(block(w).querySelectorAll('button').filter((b) => !hiddenUp(b) && isFilled(b)).length, 0, 'the switch is not a filled primary');
  assert.deepEqual(mcpCalls(w).map((c) => [c.method, c.path]), [['GET', '/api/mcp/tokens']]);
});

test('the switch shows what the server said: pending until the answer, then on', async () => {
  const hold = defer();
  const { w } = mcpWorld({ hold });
  await settle();
  flip(sw(w), true);
  await settle();
  assert.deepEqual(mcpCalls(w, 'PUT').map((c) => plain(c.body)), [{ enabled: true }]);
  assert.equal(sw(w).checked, false, 'not on before the board says so');
  assert.equal(sw(w).disabled, true);
  assert.match(text(block(w)), /Saving/);
  hold.resolve();
  await settle();
  assert.equal(sw(w).checked, true);
  assert.equal(sw(w).disabled, false);
  assert.ok(!hiddenUp(onPart(w)));
});

test('on: the device rows (name, scope chips, never used, expiry) with a red-outlined Revoke; Add device is the one filled primary', async () => {
  const { w } = mcpWorld({ enabled: true, tokens: [row(), row({ id: 'b2', name: 'desk', scopes: ['read'], last_used_at: new Date(Date.now() - 3 * 3600e3).toISOString(), last_tool: 'list_tasks' })] });
  await settle();
  assert.equal(rows(w).length, 2);
  const [a, b] = rows(w);
  assert.equal(text(a.querySelector('.k')), 'laptop');
  assert.deepEqual(a.querySelectorAll('.badge').map(text), ['read', 'tasks']);
  assert.match(text(a), /never used/);
  assert.match(text(a), /expires 2026-12-30/);
  assert.match(text(b), /last used 3h ago \(list_tasks\)/);
  const revoke = btn(a, 'Revoke');
  assert.ok(revoke.classList.contains('danger') && !isFilled(revoke), 'destructive: red-outlined, not filled at rest');
  const filled = block(w).querySelectorAll('button').filter((x) => !hiddenUp(x) && isFilled(x)).map(text);
  assert.deepEqual(filled, ['Add device']);
  assert.match(text(block(w)), /2 of 10 devices/);
});

test('Revoke needs two taps and the row leaves only when the board confirms', async () => {
  const hold = defer();
  const { w, server } = mcpWorld({ enabled: true, tokens: [row()], hold });
  await settle();
  btn(rows(w)[0], 'Revoke').click();
  await settle();
  assert.equal(mcpCalls(w, 'DELETE').length, 0, 'one tap only arms it');
  const confirm = btn(rows(w)[0], 'Confirm Revoke');
  assert.ok(confirm && confirm.classList.contains('danger'), 'the armed Confirm');
  confirm.click();
  await settle();
  assert.deepEqual(mcpCalls(w, 'DELETE').map((c) => c.path), ['/api/mcp/tokens/a1b2c3d4e5f6']);
  assert.equal(rows(w).length, 1, 'still listed while the answer is on its way');
  assert.match(text(rows(w)[0]), /Revoking/);
  hold.resolve();
  await settle();
  assert.equal(rows(w).length, 0);
  assert.equal(server.tokens.length, 0);
});

test('Add device posts the form, shows the token once with both commands, and closing the dialog empties it; nothing is stored', async () => {
  const { w } = mcpWorld({ enabled: true });
  await settle();
  submit(w, '  my   laptop ');
  await settle();
  assert.deepEqual(mcpCalls(w, 'POST').map((c) => plain(c.body)), [{ name: 'my laptop', scopes: ['read', 'tasks'], expires_days: 90 }]);
  const d = dialog(w);
  assert.ok(d && d.open, 'the one-time dialog is open');
  const shown = text(d);
  assert.ok(shown.includes(TOKEN));
  assert.match(shown, /cannot be shown again/);
  const claude = `claude mcp add --transport http --scope user --header "Authorization: Bearer ${TOKEN}" ccboard ${URL0}/mcp`;
  const codex = `export CCBOARD_MCP_TOKEN=${TOKEN}\ncodex mcp add ccboard --url ${URL0}/mcp --bearer-token-env-var CCBOARD_MCP_TOKEN`;
  assert.deepEqual(d.querySelectorAll('pre.cmd-readout').map(text), [TOKEN, claude, codex]);
  assert.match(shown, /claude mcp list/);
  assert.match(shown, /codex mcp list/);
  btn(d, 'Copy').click();
  await settle();
  assert.deepEqual(plain(w.get('__copied')), [TOKEN]);
  // never kept: not in the block's store, the page state, storage, the URL or the panel
  assert.ok(!JSON.stringify(plain(w.get('settingsMcp.data'))).includes(TOKEN));
  assert.ok(!JSON.stringify(plain(w.get('state'))).includes(TOKEN));
  for (const store of [w.localStorage, w.sessionStorage]) for (let i = 0; i < store.length; i++) assert.ok(!String(store.getItem(store.key(i))).includes(TOKEN));
  assert.ok(!String(w.location.hash).includes(TOKEN));
  assert.ok(!text(block(w)).includes(TOKEN));
  assert.equal(block(w).querySelector('.mcp-name').value, '', 'the form starts again from its defaults');
  btn(d, 'Done').click();
  await settle();
  assert.equal(d.open, false);
  assert.equal(text(d), '', 'every node of the dialog is emptied');
  assert.equal(dialog(w), null, 'and the dialog is gone');
  assert.ok(!text(w.document.body).includes(TOKEN));
  assert.equal(w.document.activeElement, btn(block(w), 'Add device'), 'the focus is back on Add device');
  assert.equal(rows(w).length, 1, 'the new device is listed');
});

test('a second tap while minting does nothing; a refusal shows beside the name and keeps what was typed', async () => {
  const hold = defer();
  const { w } = mcpWorld({ enabled: true, hold, refuse: 'a device called laptop already has a token' });
  await settle();
  submit(w, 'laptop');
  submit(w, 'laptop');
  await settle();
  assert.equal(mcpCalls(w, 'POST').length, 1);
  assert.equal(btn(block(w), 'Adding…').disabled, true);
  hold.resolve();
  await settle();
  assert.match(text(block(w).querySelector('.field-err')), /already has a token/);
  assert.equal(block(w).querySelector('.mcp-name').value, 'laptop');
  assert.equal(dialog(w), null);
});

test('Sessions is off by default, with its warning only once it is ticked; a name is needed', async () => {
  const { w } = mcpWorld({ enabled: true });
  await settle();
  const checks = Object.fromEntries(block(w).querySelectorAll('input[data-scope]').map((c) => [c.getAttribute('data-scope'), c]));
  assert.equal(checks.read.checked, true);
  assert.equal(checks.tasks.checked, true);
  assert.equal(!!checks.sessions.checked, false);
  const warn = block(w).querySelector('.mcp-sessions-warn');
  assert.ok(hiddenUp(warn));
  flip(checks.sessions, true);
  assert.ok(!hiddenUp(warn));
  assert.match(text(warn), /type a task into a session that is already running/);
  submit(w, '   ');
  await settle();
  assert.equal(mcpCalls(w, 'POST').length, 0);
  assert.match(text(block(w).querySelector('.field-err')), /1 to 40 characters/);
  submit(w, 'desk');
  await settle();
  assert.deepEqual(plain(mcpCalls(w, 'POST')[0].body.scopes), ['read', 'tasks', 'sessions']);
});

test('no public URL: a visible warning, and the dialog offers no command with a guessed address', async () => {
  const { w } = mcpWorld({ enabled: true, publicUrl: '' });
  await settle();
  const warn = block(w).querySelector('.mcp-nourl');
  assert.ok(warn && !hiddenUp(warn));
  assert.match(text(warn), /CCBOARD_PUBLIC_URL/);
  submit(w, 'laptop');
  await settle();
  const d = dialog(w);
  assert.ok(text(d).includes(TOKEN), 'the token itself is still shown once');
  assert.ok(!/claude mcp add|codex mcp add/.test(text(d)));
  assert.match(text(d), /CCBOARD_PUBLIC_URL/);
  btn(d, 'Done').click();
});

test('the command templates are the README\'s, word for word', () => {
  const { w } = mcpWorld();
  const readme = fs.readFileSync(path.join(ROOT, 'README.md'), 'utf8');
  const url = 'https://<board-host>:<port>/mcp';
  for (const kind of ['claude', 'codex']) {
    const cmd = w.run(`mcpCommand(${JSON.stringify(kind)}, '<token>', ${JSON.stringify(url)})`);
    assert.ok(readme.includes(cmd), `README.md lacks the ${kind} command:\n${cmd}`);
  }
  assert.equal(w.run("mcpUrl({ config: { public_url: 'https://board.example/' } })"), 'https://board.example/mcp');
  assert.equal(w.run("mcpUrl({ config: { public_url: '' } })"), '');
  assert.equal(w.run("mcpUrl({ config: { public_url: 'javascript:alert(1)' } })"), '');
});
