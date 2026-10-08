// Contract tests for the v0.5.13 launcher sheet (launcher.js openLauncher / launcherForm): ONE sheet for a session, a task or a dispatch.
// The pure parts first (commandPreview per agent and mode, launcherPayload, the reasoning levels per model, the remembered values and the legacy key), then the sheet itself on minidom's
// DOM: the agent picker, the field sets, the presets, the danger gate (once per repo, never in task or dispatch mode), the account line and Switch first, the one filled primary, the
// keyboard, the request and where it goes afterwards. Real core.js, components.js, launcher.js, router.js, widgets.js, pages/agents.js and shell.js; api(), toast(), poll() are recorders.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

const tick = () => new Promise((r) => setImmediate(r));
const text = (n) => (n ? n.textContent : '');

const STATE = () => ({
  user: 'alice@example.com', config: { projects_dir: '/srv/projects', code_https_port: 10000 },
  agents: { claude: { installed: true, loggedIn: true }, codex: { installed: true, loggedIn: true } },
  projects: [
    { name: 'shop', path: '/srv/projects/shop', root: { name: 'root', path: '/srv/projects/shop', root: true, state: 'project', branch: null, dirty: null, sessions: [] }, orphan_sessions: [],
      repos: [{ name: 'api', path: '/srv/projects/shop/api', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [{ name: 's1', tmux: 'shop--api--s1', state: 'idle', agent: 'claude' }] },
        { name: 'web', path: '/srv/projects/shop/web', state: 'ok', branch: 'dev', dirty: false, devcontainer: true, sessions: [] }] },
    { name: 'blog', path: '/srv/projects/blog', root: { name: 'root', path: '/srv/projects/blog', root: true, state: 'ok', branch: 'main', dirty: false, sessions: [] }, orphan_sessions: [],
      repos: [{ name: 'site', path: '/srv/projects/blog/site', state: 'ok', branch: 'main', dirty: false, devcontainer: false, sessions: [] }] },
  ],
  clone_queue: { queued: [], done: [], cap: 3 }, pending_permissions: [], tasks: [], jobs: [], runs: [],
});

/** minidom keeps select.value as a plain property; a browser answers '' for a value that is not one of the options. */
function selectSemantics(w) {
  const make = w.document.createElement;
  w.document.createElement = (tag) => {
    const n = make(tag);
    if (String(tag).toLowerCase() === 'select') {
      let v = '';
      Object.defineProperty(n, 'value', { configurable: true, get: () => v, set: (x) => { v = n.children.some((o) => o.getAttribute('value') === String(x)) ? String(x) : ''; } });
    }
    return n;
  };
}

/**
 * answers: api path prefix -> value | fn({method, path, body}) | {__error, status?} (api() then rejects). `timers` collects setTimeout callbacks instead of running them (the ultracode follow-up).
 * __opened: the blank tabs window.open made; __nav: openPage / Shell.openTerm landings.
 */
function lWorld({ state = STATE(), answers = {}, coarse = false } = {}) {
  const opened = [];
  const timers = [];
  const extra = {
    open: () => { const t = { close() { t.closed = true; }, location: '' }; opened.push(t); return t; },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return { unref() {} }; },
  };
  if (coarse) extra.matchMedia = (q) => ({ matches: /pointer:\s*coarse/.test(q), addEventListener() {}, removeEventListener() {} });
  const w = makeWorld(extra);
  installDom(w);
  selectSemantics(w);
  for (const f of ['core.js', 'components.js', 'launcher.js', 'router.js', 'pages/widgets.js', 'pages/agents.js', 'shell.js']) w.load(f);
  Object.assign(w.ctx, { __calls: [], __toasts: [], __errors: [], __nav: [], __polls: 0, __answers: answers, __st: state, __opened: opened, __timers: timers });
  w.run(`
    api = async (method, path, body) => {
      __calls.push({ method, path, body });
      for (const [prefix, v] of Object.entries(__answers)) {
        if (!path.startsWith(prefix)) continue;
        const r = typeof v === 'function' ? v({ method, path, body }) : v;
        if (r && r.__error) { const e = new Error(r.__error); e.status = r.status; e.body = r.data; throw e; }
        return r;
      }
      return { ok: true };
    };
    poll = async () => { __polls++; };
    toast = (t, o) => { __toasts.push({ text: t, kind: o && o.kind }); };
    setError = (m) => { __errors.push(m); };
    openPage = (u) => { __nav.push(u); };
    renderBanner = () => {};
    state = __st;
  `);
  return w;
}

const calls = (w) => plain(w.get('__calls'));
const posts = (w, re) => calls(w).filter((c) => c.method === 'POST' && re.test(c.path));
const sheet = (w) => w.document.getElementById('sheet');
const form = (w) => sheet(w).querySelector('form.form');
const submit = (f) => f.dispatchEvent({ type: 'submit', preventDefault() {} });
const open = (w, o) => w.run(`openLauncher(${JSON.stringify({ project: 'shop', repo: 'api', mode: 'session', ...o })})`);
const hidden = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return true; return false; };
const off = (n) => n.disabled || n.hasAttribute('disabled');
const group = (f, label) => f.querySelectorAll('.seg-ctl').find((n) => n.getAttribute('aria-label') === label);
const btn = (f, label, name) => group(f, label).querySelectorAll('button').find((b) => text(b).trim() === name);
const pressed = (f, label) => group(f, label).querySelectorAll('button').filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => text(b).trim());
const labels = (f, label) => group(f, label).querySelectorAll('button').map((b) => text(b).trim());
const fieldOf = (f, re) => { const all = f.querySelectorAll('.field').filter((x) => re.test(text(x.querySelector('label')))); return all.find((x) => !hidden(x)) || all[0]; };      // the visible one when two agents share a label
const start = (f) => f.querySelector('button[type=submit]');
const previewOf = (f) => text(f.querySelector('pre.lx-cmd'));
const press = (n, key, mods = {}) => {
  const ev = { type: 'keydown', key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, isComposing: false, defaultPrevented: false, preventDefault() { ev.defaultPrevented = true; }, stopPropagation() {}, ...mods };
  n.dispatchEvent(ev);
  return ev;
};
const store = (w, key) => { const v = w.localStorage.getItem(key); return v === null ? null : (v.startsWith('{') ? JSON.parse(v) : v); };
const isFilled = (b) => (b.classList.contains('primary') || b.classList.contains('bp5-intent-primary')) && !b.classList.contains('tinted');
const filled = (root) => root.querySelectorAll('button').filter((b) => !hidden(b) && isFilled(b));
const inputWith = (f, re) => f.querySelectorAll('input').find((i) => re.test(i.getAttribute('placeholder') || i.getAttribute('aria-label') || ''));
const typeInto = (n, v) => { n.value = v; n.dispatchEvent({ type: 'input' }); };
const choose = (sel, v) => { sel.value = v; sel.dispatchEvent({ type: 'change' }); };
const tickBox = (cb, on = true) => { cb.checked = on; cb.dispatchEvent({ type: 'change' }); };

const CLAUDE = (over = {}) => ({ agent: 'claude', launch: 'new', model: 'opus', effort: 'high', ...over });
const CODEX = (over = {}) => ({ agent: 'codex', launch: 'new', cx_mode: 'default', ...over });
const prev = (w, v, ctx) => w.run(`commandPreview(${JSON.stringify(v)}, ${JSON.stringify(ctx || {})})`);
const pay = (w, v, ctx) => plain(w.run(`launcherPayload(${JSON.stringify(v)}, ${JSON.stringify(ctx || {})})`));

// ---------------------------------------------------------------- commandPreview

test('commandPreview, Claude new: --model, --effort, --permission-mode, the tool lists, --fallback-model, --autocompact, --worktree, in the adapter\'s order, ended by the prompt after --', () => {
  const w = lWorld();
  assert.equal(prev(w, CLAUDE(), { nextName: 's3' }), "claude --session-id '<uuid>' --name s3 --model opus --effort high", 'no prompt: --session-id and --name lead');
  assert.equal(prev(w, CLAUDE({ name: 'fix', prompt: 'fix the login redirect' })),
    "claude --model opus --effort high --session-id '<uuid>' --name fix -- 'fix the login redirect'");
  assert.equal(prev(w, CLAUDE({ permission_mode: 'acceptEdits', allowed_tools: 'Bash(npm test), Read', disallowed_tools: 'WebFetch', tools: 'Bash,Edit', append_system_prompt: 'be brief',
    agent_name: 'reviewer', fallback_model: 'sonnet', autocompact: '150000', mcp_config: '/srv/projects/mcp.json', args: '--verbose', add_dirs: ['shop/web'] }), { nextName: 's1' }),
  "claude --session-id '<uuid>' --name s1 --model opus --effort high --permission-mode acceptEdits --allowedTools 'Bash(npm test)' Read --disallowedTools WebFetch --append-system-prompt 'be brief' --tools Bash,Edit --agent reviewer --fallback-model sonnet --autocompact 150000 --mcp-config /srv/projects/mcp.json --verbose --add-dir shop/web");
  assert.equal(prev(w, CLAUDE({ worktree: true, worktree_name: 'login', prompt: 'go' })), "claude --model opus --effort high --worktree login --session-id '<uuid>' --name s1 -- go",
    'the worktree line: --worktree, --session-id, then --name (after the session id, before the --), as the adapter builds it');
  assert.equal(prev(w, CLAUDE({ worktree: true, name: 'w1' })), "claude --model opus --effort high --worktree w1-xxxxxx --session-id '<uuid>' --name w1",
    'no worktree name: the route makes <session>-<six random hex>, and the six x stand for them');
  assert.equal(prev(w, CLAUDE({ model: 'custom', model_custom: 'claude-fable-5-1', effort: '' })), "claude --session-id '<uuid>' --name s1 --model claude-fable-5-1");
  assert.equal(prev(w, CLAUDE({ model: 'opus[1m]' })), "claude --session-id '<uuid>' --name s1 --model 'opus[1m]' --effort high", 'a [1m] id is quoted like shlex.join does');
  assert.match(prev(w, CLAUDE({ prompt: 'x'.repeat(200) })), /-- 'x{59}…'$/, 'a long prompt is cut: the preview is approximate');
});

test('commandPreview, Claude resume / continue / from PR: no prompt, --fork-session only with the kinds that continue a conversation, the account sits after', () => {
  const w = lWorld();
  assert.equal(prev(w, CLAUDE({ launch: 'resume' })), 'claude --resume --model opus --effort high', 'blank id: the picker');
  assert.equal(prev(w, CLAUDE({ launch: 'resume', resume_id: '0a1b2c3d-0000-4000-8000-000000000001', fork_session: true })),
    'claude --resume 0a1b2c3d-0000-4000-8000-000000000001 --fork-session --model opus --effort high', '--fork-session sits right after --resume <id>, as the adapter builds it');
  assert.equal(prev(w, CLAUDE({ launch: 'resume', fork_session: true })), 'claude --resume --fork-session --model opus --effort high', 'a blank id: the picker, then the fork flag');
  assert.equal(prev(w, CLAUDE({ launch: 'continue', fork_session: true, args: '--verbose' })), 'claude --continue --fork-session --model opus --effort high --verbose', '--continue --fork-session, then the rest');
  assert.equal(prev(w, CLAUDE({ launch: 'continue', prompt: 'ignored' })), 'claude --continue --model opus --effort high', 'a prompt only starts a new session');
  assert.equal(prev(w, CLAUDE({ launch: 'from_pr', from_pr: '#123' })), 'claude --from-pr 123 --model opus --effort high', 'the route strips the # of a PR number');
  assert.equal(prev(w, CLAUDE({ launch: 'from_pr', from_pr: 'https://github.com/o/r/pull/7' })), 'claude --from-pr https://github.com/o/r/pull/7 --model opus --effort high', 'a URL stays as it is');
  assert.equal(prev(w, CLAUDE({ launch: 'from_pr', from_pr: '#9', fork_session: true })), 'claude --from-pr 9 --model opus --effort high', 'a PR launch never forks');
  assert.equal(prev(w, CLAUDE({ launch: 'new', fork_session: true })), "claude --session-id '<uuid>' --name s1 --model opus --effort high", 'no fork flag on a new session');
});

test('commandPreview, Claude: ultracode is a second line (/effort ultracode on) until this box takes --effort ultracode; fast is /fast; the devcontainer wraps the line', () => {
  const w = lWorld();
  assert.equal(prev(w, CLAUDE({ ultracode: true, fast: true })), "claude --session-id '<uuid>' --name s1 --model opus --effort high\n# then /effort ultracode on · /fast");
  assert.equal(prev(w, CLAUDE({ ultracode: true }), { ultraNative: true }), "claude --session-id '<uuid>' --name s1 --model opus --effort ultracode");
  assert.equal(prev(w, CLAUDE({ devcontainer: true }), { cwd: '/srv/projects/shop/web' }),
    "devcontainer up --workspace-folder /srv/projects/shop/web && devcontainer exec --workspace-folder /srv/projects/shop/web -- claude --session-id '<uuid>' --name s1 --model opus --effort high");
});

test('commandPreview, Codex: the mode picker is the flags (default, auto, read-only, bypass, custom), reasoning is -c model_reasoning_effort, then -c lines, --search, --add-dir, extra, the prompt after --', () => {
  const w = lWorld();
  assert.equal(prev(w, CODEX()), 'codex --no-alt-screen -s workspace-write -a on-request');
  assert.equal(prev(w, CODEX({ cx_mode: 'auto' })), 'codex --no-alt-screen -s workspace-write -a on-request', 'no --approve-for-me on a Codex without it: it asks on request');
  assert.equal(prev(w, CODEX({ cx_mode: 'auto' }), { caps: { approve_for_me: true } }), 'codex --no-alt-screen --approve-for-me -s workspace-write');
  assert.equal(prev(w, CODEX({ cx_mode: 'read-only' })), 'codex --no-alt-screen -s read-only -a on-request');
  assert.equal(prev(w, CODEX({ cx_mode: 'bypass' })), 'codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox');
  assert.equal(prev(w, CODEX({ cx_mode: 'bypass' }), { caps: { bypass_approvals: false, yolo: true } }), 'codex --no-alt-screen --yolo');
  assert.equal(prev(w, CODEX({ cx_mode: 'custom', sandbox: 'read-only', approval: 'never' })), 'codex --no-alt-screen -s read-only -a never');
  assert.equal(prev(w, CODEX({ model: 'gpt-5.5', reasoning: 'high', config: 'model_verbosity=low\n\ntui.theme=dark', search: true, add_dirs: ['shop/web'], args: '--foo', prompt: 'review the diff' }), { dirs: { 'shop/web': '/srv/projects/shop/web' } }),
    "codex --no-alt-screen -s workspace-write -a on-request -m gpt-5.5 -c 'model_reasoning_effort=\"high\"' -c model_verbosity=low -c tui.theme=dark --search --add-dir /srv/projects/shop/web --foo -- 'review the diff'");
  assert.equal(prev(w, CODEX({ launch: 'resume', resume_id: 'my-session', prompt: 'ignored' })), 'codex resume --no-alt-screen -s workspace-write -a on-request my-session');
  assert.equal(prev(w, CODEX({ launch: 'continue' })), 'codex resume --no-alt-screen -s workspace-write -a on-request --last', 'Last = resume --last');
  assert.equal(prev(w, CODEX({ launch: 'fork', resume_id: 'abc' })), 'codex fork --no-alt-screen -s workspace-write -a on-request abc');
  assert.equal(prev(w, CODEX({ worktree: true, worktree_name: 'try-1' })), 'codex --no-alt-screen -s workspace-write -a on-request\n# in a new worktree: .ccboard/worktrees/try-1', 'Codex has no worktree flag: the board makes it');
  assert.ok(!/--name|--session-id/.test(prev(w, CODEX({ name: 'cx' }))), 'a Codex name is the board\'s label only');
});

test('commandPreview in task and dispatch mode: always a worktree, the task\'s prompt, never a bypass, whatever the values say', () => {
  const w = lWorld();
  for (const mode of ['task', 'dispatch']) {
    const c = prev(w, CLAUDE({ permission_mode: 'bypassPermissions', prompt: 'do the thing', launch: 'resume' }), { mode, slug: 'do-the-thing' });
    assert.equal(c, "claude --model opus --effort high --permission-mode bypassPermissions --worktree do-the-thing --session-id '<uuid>' -- 'do the thing'".replace(' --permission-mode bypassPermissions', ' --permission-mode bypassPermissions'));
    const x = prev(w, CODEX({ cx_mode: 'bypass', prompt: 'do the thing' }), { mode, slug: 'do-the-thing' });
    assert.ok(!/bypass|yolo/.test(x.split('\n')[0]), `${mode}: no bypass flag on a Codex line`);
    assert.match(x, /-s workspace-write -a on-request/);
    assert.match(x, /# in a new worktree: \.ccboard\/worktrees\/do-the-thing/);
  }
});

test('commandPreview matches the route where the review found it did not: --name after --session-id in a worktree line (never for a task), -<6 hex> placeholder for a blank worktree name, Codex custom danger, a Codex fork without an id', () => {
  const w = lWorld();
  assert.equal(prev(w, CLAUDE({ worktree: true, worktree_name: 'login', name: 'w1' })), "claude --model opus --effort high --worktree login --session-id '<uuid>' --name w1");
  assert.equal(prev(w, CLAUDE({ worktree: true }), { nextName: 's4' }), "claude --model opus --effort high --worktree s4-xxxxxx --session-id '<uuid>' --name s4",
    'a blank worktree name: the route adds a random suffix to the session name; the preview cannot know it, so it says so with xxxxxx instead of pretending');
  assert.equal(prev(w, CLAUDE({ worktree: true, worktree_name: ' ', name: 'ab' })), "claude --model opus --effort high --worktree ab-xxxxxx --session-id '<uuid>' --name ab", 'a blank (spaces) name is a blank name');
  for (const mode of ['task', 'dispatch']) {
    const t = prev(w, CLAUDE({ name: 'ignored', prompt: 'go' }), { mode, slug: 'fix-it' });
    assert.equal(t, "claude --model opus --effort high --worktree fix-it --session-id '<uuid>' -- go", `${mode}: the lane's worktree is its slug and it has no session name of its own`);
  }
  assert.equal(prev(w, CODEX({ worktree: true }), { nextName: 's4' }), 'codex --no-alt-screen -s workspace-write -a on-request\n# in a new worktree: .ccboard/worktrees/s4', 'Codex: the board makes the folder from the name, no random suffix');
  assert.equal(prev(w, CODEX({ cx_mode: 'custom', sandbox: 'danger-full-access', approval: 'never' })), 'codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox', 'the danger sandbox is the one bypass flag, as the adapter builds it');
  assert.equal(prev(w, CODEX({ cx_mode: 'custom', sandbox: 'danger-full-access' }), { caps: { bypass_approvals: false, yolo: true } }), 'codex --no-alt-screen --yolo');
  assert.equal(prev(w, CODEX({ cx_mode: 'custom', sandbox: 'danger-full-access', approval: 'never' }), { mode: 'task', slug: 'x' }).split('\n')[0], 'codex --no-alt-screen -s workspace-write -a never', 'a task is never given the danger sandbox');
  assert.equal(prev(w, CODEX({ launch: 'fork' })), 'codex fork --no-alt-screen -s workspace-write -a on-request', 'a blank id opens Codex\'s picker: the route adds no --last');
  assert.ok(!/--last/.test(prev(w, CODEX({ launch: 'fork', resume_id: '' }))));
  assert.match(prev(w, CODEX({ launch: 'continue' })), / --last$/, '--last stays for Last (resume --last)');
});

test('the preview is one nowrap span per token (a flag is never cut after its --), the spaces between them are the wrap points, and Copy takes the plain line', async () => {
  const w = lWorld();
  open(w, {});
  const f = form(w);
  btn(f, 'Effort', 'low').click();
  tickBox(f.querySelectorAll('label').find((l) => /Fast mode/.test(text(l))).querySelector('input'));
  typeInto(f.querySelector('textarea.lx-prompt'), 'fix the login redirect');
  const pre = f.querySelector('pre.lx-cmd');
  const spans = pre.querySelectorAll('.lx-t');
  const plainLine = text(pre);
  assert.ok(spans.length >= 8, 'every token has a span');
  assert.ok(spans.every((x) => !/\s/.test(text(x)) || /^'.*'$/.test(text(x))), 'a token holds no space unless it is one quoted string');
  assert.ok(spans.some((x) => text(x) === '--effort') && spans.some((x) => text(x) === 'low'), '--effort and its value are separate tokens, each whole');
  assert.ok(spans.some((x) => text(x) === "'fix the login redirect'"), 'a quoted prompt is ONE token');
  assert.ok(!spans.some((x) => /^#/.test(text(x))), 'the note line stays plain text');
  assert.match(plainLine, /\n# then \/fast$/);
  // the nodes in between are single spaces: the only places a line may break
  const between = pre.childNodes.filter((n) => n.nodeType === 3).map((n) => n.textContent);
  assert.ok(between.every((t) => /^( |\n|# then \/fast)$/.test(t) || t === ' '), `only spaces between tokens, got ${JSON.stringify(between)}`);
  // a long path is plain text: it may break anywhere rather than overflow
  const long = w.run(`lxCmdNodes(${JSON.stringify('claude --mcp-config /srv/projects/a-very-long-folder-name/another-long-folder-name/mcp.json')})`);
  assert.equal(long.filter((n) => n && n.classList && n.classList.contains('lx-t')).length, 2, 'claude and --mcp-config are spans; the 50-character path is not');
  const flag = w.run(`lxCmdNodes('codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox')`).filter((n) => n && n.classList && n.classList.contains('lx-t')).map(text);
  assert.deepEqual(plain(flag), ['codex', '--no-alt-screen', '--dangerously-bypass-approvals-and-sandbox'], 'the longest flag is still one whole token (it moves to the next line rather than breaking at a hyphen)');
  // Copy reads the plain string, not the spans' text joined some other way
  const written = [];
  w.ctx.navigator.clipboard = { writeText: async (t) => { written.push(t); } };
  f.querySelector('button.lx-copy').click();
  await tick();
  assert.deepEqual(written, [plainLine]);
});

test('the sheet\'s command line and body for every case of tests/fixtures/launcher_cases.json (the same file tests/test_launcher_api.py posts to the real route): launcherPayload(v) is the body, the preview splits into the route\'s tokens', () => {
  const { cases } = JSON.parse(readFileSync(new URL('../fixtures/launcher_cases.json', import.meta.url), 'utf8'));
  assert.ok(cases.length >= 15);
  const w = lWorld();
  const seen = new Set();
  for (const c of cases) {
    assert.ok(!seen.has(c.id), `${c.id} twice`);
    seen.add(c.id);
    assert.deepEqual(pay(w, c.v, c.ctx), c.body, `${c.id}: the body the sheet sends`);
    const lines = prev(w, c.v, c.ctx).split('\n');
    assert.deepEqual(plain(w.run(`lxSplit(${JSON.stringify(lines[0])})`)), c.tokens, `${c.id}: the preview's tokens`);
    assert.deepEqual(lines.slice(1), c.notes, `${c.id}: the note lines`);
  }
  for (const want of ['claude-resume-fork', 'claude-continue-fork', 'claude-worktree-named', 'claude-worktree-blank-name', 'claude-from-pr-number-with-hash', 'codex-custom-danger-full-access', 'codex-fork-blank-id-has-no-last', 'claude-bypass']) {
    assert.ok(seen.has(want), `the fixture list keeps ${want}`);
  }
});

test('commandPreview, a shell: no agent line, only the devcontainer when it is chosen', () => {
  const w = lWorld();
  assert.equal(prev(w, { agent: 'shell' }, { cwd: '/srv/projects/shop/api' }), '# a plain shell in /srv/projects/shop/api');
  assert.equal(prev(w, { agent: 'shell' }, { cwd: '/srv/x', devcontainer: true }), 'devcontainer up --workspace-folder /srv/x && devcontainer exec --workspace-folder /srv/x -- bash -l');
});

// ---------------------------------------------------------------- launcherPayload

test('launcherPayload, Claude: the launcher\'s words next to the flat compat fields, blanks left out', () => {
  const w = lWorld();
  assert.deepEqual(pay(w, CLAUDE({ name: 'fix', prompt: ' go ', fast: true, worktree: true, worktree_name: 'wt', fallback_model: 'sonnet', autocompact: 'auto', tools: 'Bash', agent_name: 'rev', mcp_config: '/a/b.json',
    permission_mode: 'acceptEdits', allowed_tools: 'Read', args: '--verbose', add_dirs: ['shop/web'] }), { cwd_rel: 'packages/ui' }), {
    launcher: 'claude', agent: 'claude', name: 'fix', prompt: 'go', model: 'opus', effort: 'high', permission_mode: 'acceptEdits', fast: true, allowed_tools: 'Read', tools: 'Bash', agent_name: 'rev',
    fallback_model: 'sonnet', autocompact: 'auto', mcp_config: '/a/b.json', worktree: true, worktree_name: 'wt', args: '--verbose', add_dirs: ['shop/web'], cwd_rel: 'packages/ui' });
  assert.deepEqual(pay(w, CLAUDE({ model: '', effort: '', permission_mode: 'manual' })), { launcher: 'claude', agent: 'claude' }, 'nothing chosen, nothing sent');
  assert.deepEqual(pay(w, CLAUDE({ launch: 'resume', resume_id: ' 0a1b2c3d-0000-4000-8000-000000000001 ', fork_session: true, prompt: 'no', worktree: true })),
    { launcher: 'resume', agent: 'claude', resume_id: '0a1b2c3d-0000-4000-8000-000000000001', model: 'opus', effort: 'high', fork_session: true }, 'a resume takes neither a prompt nor a worktree');
  assert.deepEqual(pay(w, CLAUDE({ launch: 'from_pr', from_pr: '#9', fork_session: true })), { launcher: 'claude', agent: 'claude', from_pr: '#9', model: 'opus', effort: 'high' }, 'from PR is a claude launcher with from_pr beside it');
  assert.deepEqual(pay(w, CLAUDE({ launch: 'continue' })), { launcher: 'continue', agent: 'claude', model: 'opus', effort: 'high' });
  assert.equal(pay(w, CLAUDE({ ultracode: true })).effort, 'high', 'ultracode is not an --effort here: it is applied after the start');
  assert.equal(pay(w, CLAUDE({ ultracode: true }), { ultraNative: true }).effort, 'ultracode', 'unless this box takes it');
  const byp = pay(w, CLAUDE({ permission_mode: 'bypassPermissions' }));
  assert.deepEqual([byp.permission_mode, byp.mode, byp.bypass], ['bypassPermissions', 'bypass', true], 'Claude bypass: the permission mode, mode bypass and bypass true (the route\'s gate needs both words)');
  const ask = pay(w, CLAUDE({ permission_mode: 'acceptEdits' }));
  assert.deepEqual([ask.permission_mode, 'mode' in ask, 'bypass' in ask], ['acceptEdits', false, false], 'no other Claude mode says bypass');
  const known = plain(w.run(`launcherPayload(${JSON.stringify(CLAUDE({ fallback_model: 'sonnet', tools: 'Bash', fast: true }))}, { has: (k) => k !== 'fallback_model' && k !== 'tools' && k !== 'fast' })`));
  assert.deepEqual(known, { launcher: 'claude', agent: 'claude', model: 'opus', effort: 'high' }, 'a field the schema does not know is not sent');
});

test('launcherPayload, Codex: launcher + mode (the one picker) with the adapter\'s older words beside it, reasoning twice, bypass only for the danger choices', () => {
  const w = lWorld();
  assert.deepEqual(pay(w, CODEX({ model: 'gpt-5.5', reasoning: 'high', name: 'cx', prompt: 'hi', search: true, config: 'a.b=1\n\nc=2', worktree: true, add_dirs: ['shop/web'] })), {
    launcher: 'claude', agent: 'codex', name: 'cx', prompt: 'hi', model: 'gpt-5.5', reasoning: 'high', reasoning_effort: 'high', mode: 'default', permission_mode: 'default', search: true, config: ['a.b=1', 'c=2'],
    worktree: true, add_dirs: ['shop/web'] });
  const mode = (m, extra) => { const b = pay(w, CODEX({ cx_mode: m, ...extra })); return [b.mode, b.permission_mode, b.bypass, b.sandbox, b.approval]; };
  assert.deepEqual(mode('auto'), ['auto', 'auto', undefined, undefined, undefined]);
  assert.deepEqual(mode('read-only'), ['read-only', 'plan', undefined, undefined, undefined]);
  assert.deepEqual(mode('bypass'), ['bypass', 'bypassPermissions', true, undefined, undefined], 'bypass carries the acknowledgement');
  assert.deepEqual(mode('custom', { sandbox: 'read-only', approval: 'never' }), ['custom', undefined, undefined, 'read-only', 'never'], 'custom is sandbox + approval, no permission mode');
  assert.deepEqual(mode('custom', { sandbox: 'danger-full-access', approval: 'never' }), ['custom', undefined, true, 'danger-full-access', 'never'], 'the danger sandbox needs the acknowledgement too');
  assert.deepEqual(pay(w, CODEX({ launch: 'resume', resume_id: 'my session' })).resume_id, 'my session', 'a Codex resume takes an id or a name');
  assert.equal(pay(w, CODEX({ launch: 'fork' })).launcher, 'fork');
  assert.equal(pay(w, CODEX({ launch: 'continue' })).launcher, 'continue');
});

test('launcherPayload, a shell, and launcherTaskOpts: no bypass and no worktree flag in a task', () => {
  const w = lWorld();
  assert.deepEqual(pay(w, { agent: 'shell', name: 'sh', devcontainer: true }), { launcher: 'shell', agent: 'shell', name: 'sh', devcontainer: true });
  const t = (v) => plain(w.run(`launcherTaskOpts(${JSON.stringify(v)})`));
  assert.deepEqual(t(CLAUDE({ permission_mode: 'bypassPermissions', allowed_tools: 'Read', worktree: true, fast: true })), { model: 'opus', effort: 'high', allowed_tools: 'Read' });
  assert.deepEqual(t(CODEX({ model: 'gpt-5.5', reasoning: 'low', cx_mode: 'bypass', search: true })), { model: 'gpt-5.5', reasoning_effort: 'low', permission_mode: 'default', opts: { search: true } });
  assert.deepEqual(t(CODEX({ cx_mode: 'custom', sandbox: 'danger-full-access', approval: 'never' })), { opts: { sandbox: 'workspace-write', approval: 'never' } }, 'the danger sandbox is turned back to the safe one for a task');
});

// ---------------------------------------------------------------- the sheet: agent picker and fields

test('the agent picker is a segmented control Claude | Codex | Shell; Codex is disabled with the reason when state.agents says it is not installed, and arrow keys skip it', () => {
  const st = STATE();
  st.agents.codex.installed = false;
  const w = lWorld({ state: st });
  open(w, {});
  const f = form(w);
  assert.deepEqual(labels(f, 'Agent'), ['◆ Claude', '◇ Codex', '▸ Shell']);
  assert.deepEqual(pressed(f, 'Agent'), ['◆ Claude']);
  const cx = btn(f, 'Agent', '◇ Codex');
  assert.ok(off(cx));
  assert.match(cx.getAttribute('title'), /Codex is not installed on this box/);
  cx.click();
  assert.deepEqual(pressed(f, 'Agent'), ['◆ Claude'], 'a disabled button does not switch');
  press(btn(f, 'Agent', '◆ Claude'), 'ArrowRight');
  assert.deepEqual(pressed(f, 'Agent'), ['▸ Shell'], 'the arrow lands on Shell, not on the disabled Codex');
  assert.ok(hidden(f.querySelector('.lx-agentbox[data-agent=claude]')) && !hidden(f.querySelector('.lx-agentbox[data-agent=shell]')), 'the Shell field set shows');
  assert.equal(previewOf(f), '# a plain shell in /srv/projects/shop/api');
  // task and dispatch mode have no Shell
  open(w, { mode: 'task' });
  assert.deepEqual(labels(form(w), 'Agent'), ['◆ Claude', '◇ Codex']);
});

test('Claude defaults: model opus and effort high, chips ordered opus, fable, sonnet, haiku (each with its muted hue), the rest under More models, permissions under a select', () => {
  const w = lWorld();
  open(w, {});
  const f = form(w);
  assert.deepEqual(labels(f, 'Model'), ['opus', 'fable', 'sonnet', 'haiku']);
  assert.deepEqual(pressed(f, 'Model'), ['opus']);
  assert.deepEqual(group(f, 'Model').querySelectorAll('button').map((b) => b.classList.contains('hue-blue') || b.classList.contains('hue-violet') || b.classList.contains('hue-green') || b.classList.contains('hue-slate')), [true, true, true, true]);
  assert.deepEqual(pressed(f, 'Effort'), ['high']);
  assert.deepEqual(labels(f, 'Effort'), ['default', 'low', 'medium', 'high', 'xhigh', 'max']);
  const more = f.querySelectorAll('select').find((x) => x.getAttribute('aria-label') === 'More models');
  assert.deepEqual(more.children.map((o) => o.getAttribute('value')), ['__', '', 'opusplan', 'best', 'opus[1m]', 'sonnet[1m]', 'custom']);
  btn(f, 'Model', 'sonnet').click();
  assert.deepEqual(pressed(f, 'Model'), ['sonnet']);
  assert.match(previewOf(f), /--model sonnet --effort high/);
  choose(more, 'opusplan');
  assert.deepEqual(pressed(f, 'Model'), [], 'a model from the select un-presses the chips');
  assert.match(previewOf(f), /--model opusplan/);
  choose(more, 'custom');
  const id = inputWith(f, /full model id/);
  assert.equal(hidden(id), false);
  typeInto(id, 'claude-fable-5-1');
  assert.match(previewOf(f), /--model claude-fable-5-1/);
  const perm = fieldOf(f, /^Permissions$/).querySelector('select');
  assert.deepEqual(perm.children.map((o) => o.getAttribute('value')), ['', 'acceptEdits', 'plan', 'auto', 'dontAsk', 'bypassPermissions']);
  choose(perm, 'plan');
  assert.match(previewOf(f), /--permission-mode plan/);
});

test('Advanced is collapsed and holds the rest of the Claude fields; siblings are pre-checked for --add-dir, other projects\' repos are not; the devcontainer shows only where the repo has one', () => {
  const w = lWorld();
  open(w, {});
  const f = form(w);
  const adv = f.querySelector('details[data-agent=claude]');
  assert.equal(adv.getAttribute('open'), null, 'collapsed');
  const names = adv.querySelectorAll('label.field-label').map((l) => text(l));
  for (const want of ['Allowed tools', 'Disallowed tools', 'Available tools', 'Append to system prompt', 'Agent', 'Fallback model', 'Auto-compact', 'MCP config file', 'Extra args']) assert.ok(names.includes(want), `${want} is under Advanced`);
  assert.ok(adv.querySelectorAll('label').some((l) => /Start in a new git worktree/.test(text(l))));
  assert.ok(!adv.querySelectorAll('label').some((l) => /devcontainer/.test(text(l)) && !hidden(l)), 'shop/api has no devcontainer');
  const sib = fieldOf(adv, /^Also give access to$/);
  assert.deepEqual(sib.querySelectorAll('input').map((i) => i.checked), [true], 'the sibling repo shop/web is pre-checked');
  assert.match(previewOf(f), /--add-dir \/srv\/projects\/shop\/web/);
  const others = adv.querySelector('details.lx-others');
  assert.deepEqual(others.querySelectorAll('input').map((i) => i.checked), [false], 'blog/site: not checked');
  open(w, { repo: 'web' });
  assert.ok(form(w).querySelector('details[data-agent=claude]').querySelectorAll('label').some((l) => /devcontainer/.test(text(l)) && !hidden(l)), 'shop/web has one');
  open(w, { repo: 'root' });
  assert.equal(form(w).querySelectorAll('input').filter((i) => i.getAttribute('value') === 'shop/web').length, 0, 'the project folder already contains its repos: no sibling boxes');
});

test('launch kinds: New | Resume | Continue | From PR for Claude (a resume id or a PR box appears), New | Resume | Last | Fork for Codex; the first prompt only belongs to New', () => {
  const w = lWorld();
  open(w, {});
  const f = form(w);
  assert.deepEqual(labels(f, 'Launch'), ['New', 'Resume', 'Continue', 'From PR']);
  const prompt = f.querySelector('textarea.lx-prompt');
  assert.equal(hidden(prompt), false);
  assert.equal(hidden(fieldOf(f, /^Session to resume$/)), true);
  btn(f, 'Launch', 'Resume').click();
  assert.equal(hidden(fieldOf(f, /^Session to resume$/)), false);
  assert.equal(hidden(prompt), true, 'a resume takes no first prompt');
  assert.match(previewOf(f), /^claude --resume /);
  btn(f, 'Launch', 'From PR').click();
  assert.equal(hidden(fieldOf(f, /^Pull request$/)), false);
  btn(f, 'Launch', 'Continue').click();
  assert.equal(hidden(fieldOf(f, /^Pull request$/)), true);
  assert.match(previewOf(f), /^claude --continue /);
  assert.equal(hidden(f.querySelector('details[data-agent=claude]').querySelectorAll('label').find((l) => /Fork/.test(text(l)))), false, 'fork-session appears for resume and continue');
  btn(f, 'Agent', '◇ Codex').click();
  assert.deepEqual(labels(f, 'Launch'), ['New', 'Resume', 'Last', 'Fork']);
  assert.deepEqual(pressed(f, 'Launch'), ['Continue'].filter(() => false).concat(['Last']), 'continue is Codex\'s Last (resume --last)');
  btn(f, 'Launch', 'Fork').click();
  assert.match(previewOf(f), /^codex fork /);
  assert.equal(hidden(fieldOf(f, /^Session to resume( or fork)?$/)), false);
});

test('Advanced follows the launch kind: Fork the session only under Resume and Continue, the worktree boxes only under New (both agents), whatever the schema offers', () => {
  const w = lWorld();
  const ctl = open(w, {});
  const f = form(w);
  const claudeAdv = f.querySelector('details[data-agent=claude]');
  const codexAdv = f.querySelector('details[data-agent=codex]');
  const row = (adv, re) => adv.querySelectorAll('label').find((l) => re.test(text(l)));
  for (const [kind, fork, wt] of [['New', true, false], ['Resume', false, true], ['Continue', false, true], ['From PR', true, true]]) {
    btn(f, 'Launch', kind).click();
    assert.equal(hidden(row(claudeAdv, /Fork the session/)), fork, `Claude ${kind}: the fork box is ${fork ? 'hidden' : 'shown'}`);
    assert.equal(hidden(row(claudeAdv, /Start in a new git worktree/)), wt, `Claude ${kind}: the worktree box is ${wt ? 'hidden' : 'shown'}`);
  }
  btn(f, 'Launch', 'New').click();
  btn(f, 'Agent', '◇ Codex').click();
  for (const [kind, wt] of [['New', false], ['Resume', true], ['Last', true], ['Fork', true]]) {
    btn(f, 'Launch', kind).click();
    assert.equal(hidden(row(codexAdv, /Start in a new git worktree/)), wt, `Codex ${kind}: the worktree box is ${wt ? 'hidden' : 'shown'}`);
    assert.equal(hidden(row(claudeAdv, /Fork the session/)), true, 'Codex has no fork-session box: its Fork is a launch kind');
  }
  // a body never carries what its kind cannot use, even when the box was ticked under another kind
  btn(f, 'Agent', '◆ Claude').click();
  btn(f, 'Launch', 'New').click();
  tickBox(row(claudeAdv, /Start in a new git worktree/).querySelector('input'));
  assert.equal(plain(ctl.payload()).worktree, true, 'ticked under New');
  btn(f, 'Launch', 'Resume').click();
  tickBox(row(claudeAdv, /Fork the session/).querySelector('input'));
  assert.deepEqual([plain(ctl.payload()).worktree, plain(ctl.payload()).fork_session], [undefined, true], 'under Resume: the fork box counts, the worktree box does not');
  btn(f, 'Launch', 'New').click();
  assert.deepEqual([plain(ctl.payload()).worktree, plain(ctl.payload()).fork_session], [true, undefined], 'back under New: the other way round');
});

test('Codex: ONE mode picker (default, auto, read-only, bypass, custom) as a segmented control; custom shows sandbox and approval; the model select lists the catalogue', () => {
  const w = lWorld();
  open(w, { agent: 'codex' });
  const f = form(w);
  assert.deepEqual(pressed(f, 'Agent'), ['◇ Codex']);
  assert.deepEqual(labels(f, 'Codex mode'), ['default', 'auto', 'read-only', 'bypass', 'custom']);
  assert.deepEqual(pressed(f, 'Codex mode'), ['default']);
  assert.equal(hidden(f.querySelector('.lx-custom')), true);
  btn(f, 'Codex mode', 'custom').click();
  assert.equal(hidden(f.querySelector('.lx-custom')), false);
  const sandbox = fieldOf(f, /^Sandbox$/).querySelector('select');
  const approval = fieldOf(f, /^Approval policy$/).querySelector('select');
  assert.deepEqual(sandbox.children.map((o) => o.getAttribute('value')), ['read-only', 'workspace-write', 'danger-full-access']);
  assert.deepEqual(approval.children.map((o) => o.getAttribute('value')), ['untrusted', 'on-request', 'never']);
  choose(sandbox, 'read-only'); choose(approval, 'never');
  assert.match(previewOf(f), /-s read-only -a never/);
  const model = fieldOf(f, /^Model$/).querySelector('select');
  assert.deepEqual(model.children.map((o) => o.getAttribute('value')), ['', 'gpt-5.6-terra', 'gpt-5.6-sol', 'gpt-5.6-luna', 'gpt-5.5', 'codex-auto-review', 'custom']);
  choose(model, 'gpt-5.5');
  assert.match(previewOf(f), / -m gpt-5\.5/);
  const adv = f.querySelector('details[data-agent=codex]');
  assert.ok(adv.querySelectorAll('label').some((l) => /Live web search/.test(text(l))));
  assert.ok(adv.querySelectorAll('label.field-label').map(text).includes('Config overrides'));
});

test('Codex reasoning levels are disabled per model from reasoning_by_model (an injected schema), each with its reason, and a level the new model lacks is dropped', async () => {
  const schema = { agents: { codex: { name: 'codex', installed: true, options: [{ key: 'model' }, { key: 'reasoning_effort' }, { key: 'mode' }, { key: 'search' }], models: ['small', 'big'], efforts: ['low', 'medium', 'high', 'xhigh', 'max'],
    permission_modes: ['default'], reasoning_by_model: { small: ['low', 'medium'], big: ['low', 'medium', 'high', 'xhigh', 'max'] }, capabilities: {} } } };
  const w = lWorld({ answers: { '/api/agents': schema } });
  open(w, { agent: 'codex' });
  await tick(); await tick();                                  // the sheet opened on the fallback; the API answer lands and the sheet follows it
  const f = form(w);
  const model = fieldOf(f, /^Model$/).querySelector('select');
  assert.deepEqual(model.children.map((o) => o.getAttribute('value')), ['', 'small', 'big', 'custom'], 'the models are the schema\'s');
  assert.deepEqual(labels(f, 'Reasoning'), ['default', 'low', 'medium', 'high', 'xhigh', 'max']);
  const offLevels = () => group(f, 'Reasoning').querySelectorAll('button').filter(off).map((b) => text(b).trim());
  assert.deepEqual(offLevels(), [], 'no model chosen: every level any model has');
  choose(model, 'small');
  assert.deepEqual(offLevels(), ['high', 'xhigh', 'max']);
  assert.match(btn(f, 'Reasoning', 'xhigh').getAttribute('title'), /small has no xhigh reasoning level/);
  btn(f, 'Reasoning', 'high').click();
  assert.deepEqual(pressed(f, 'Reasoning'), ['default'], 'a disabled level cannot be picked');
  choose(model, 'big');
  assert.deepEqual(offLevels(), []);
  btn(f, 'Reasoning', 'xhigh').click();
  assert.match(previewOf(f), /model_reasoning_effort="xhigh"/);
  choose(model, 'small');
  assert.deepEqual(pressed(f, 'Reasoning'), ['default'], 'xhigh is gone with the model that had it');
  assert.ok(!/model_reasoning_effort/.test(previewOf(f)));
});

// ---------------------------------------------------------------- the schema: once per page load

test('GET /api/agents is read once per page load, never by a poll; its models, efforts and permission modes lay over the embedded fallback; a failure is tried again on the next open', async () => {
  let n = 0;
  const w = lWorld({ answers: { '/api/agents': () => { n += 1; return n === 1 ? { __error: 'offline' } : { agents: { claude: { name: 'claude', options: [{ key: 'model' }, { key: 'effort' }], models: ['opus', 'haiku'], efforts: ['low', 'high'], permission_modes: ['manual', 'plan'], capabilities: {} } } }; } } });
  open(w, {});
  await tick(); await tick();
  assert.equal(calls(w).filter((c) => c.path === '/api/agents').length, 1);
  assert.deepEqual(labels(form(w), 'Model'), ['opus', 'fable', 'sonnet', 'haiku'], 'the request failed: the fallback stands');
  open(w, {});
  await tick(); await tick();
  assert.equal(calls(w).filter((c) => c.path === '/api/agents').length, 2, 'a failed read is tried on the next open');
  const f = form(w);
  assert.deepEqual(group(f, 'Model').querySelectorAll('button').filter(off).map((b) => text(b).trim()), ['fable', 'sonnet'], 'a chip the schema does not list is disabled');
  assert.deepEqual(labels(f, 'Effort'), ['default', 'low', 'high']);
  assert.deepEqual(fieldOf(f, /^Permissions$/).querySelector('select').children.map((o) => o.getAttribute('value')), ['', 'plan']);
  assert.equal(hidden(fieldOf(f.querySelector('details[data-agent=claude]'), /^Fallback model$/)), true, 'an advanced field the schema does not list is not shown');
  assert.equal(hidden(fieldOf(f.querySelector('details[data-agent=claude]'), /^Allowed tools$/)), true);
  open(w, {});
  await tick(); await tick();
  assert.equal(calls(w).filter((c) => c.path === '/api/agents').length, 2, 'answered once: never again');
  await w.run('poll()');
  assert.equal(calls(w).filter((c) => c.path === '/api/agents').length, 2, 'and a poll never asks');
});

test('an answer without agents (the demo board before its fixture) keeps the embedded schema and is not asked again', async () => {
  const w = lWorld({ answers: { '/api/agents': {} } });
  open(w, {});
  await tick(); await tick();
  assert.deepEqual(labels(form(w), 'Model'), ['opus', 'fable', 'sonnet', 'haiku']);
  open(w, {});
  await tick();
  assert.equal(calls(w).filter((c) => c.path === '/api/agents').length, 1);
});

// ---------------------------------------------------------------- what is remembered

test('the pre-v0.5.13 key ccboard:launch:<p>/<r> is read once as Claude, converted into ccboard:launch:<p>/<r>:claude and removed', () => {
  const w = lWorld();
  w.localStorage.setItem('ccboard:launch:shop/api', JSON.stringify({ launcher: 'resume', model_sel: 'custom', model_id: 'claude-fable-5-1', effort: 'xhigh', permission_mode: 'acceptEdits', allowed_tools: 'Read', args: '--verbose' }));
  open(w, {});
  const f = form(w);
  assert.equal(w.localStorage.getItem('ccboard:launch:shop/api'), null, 'the old key is gone');
  assert.deepEqual(store(w, 'ccboard:launch:shop/api:claude'), { model: 'custom', model_custom: 'claude-fable-5-1', effort: 'xhigh', permission_mode: 'acceptEdits', allowed_tools: 'Read', args: '--verbose' }, 'the launch kind is not carried: a + opens a new session');
  assert.deepEqual(pressed(f, 'Effort'), ['xhigh']);
  assert.deepEqual(pressed(f, 'Launch'), ['New']);
  assert.match(previewOf(f), /--model claude-fable-5-1 --effort xhigh --permission-mode acceptEdits --allowedTools Read --verbose/);
  // a legacy 'default (settings)' stays a default, not opus; a legacy bypass is dropped
  w.localStorage.setItem('ccboard:launch:shop/web', JSON.stringify({ model_sel: '', effort: '', permission_mode: 'bypassPermissions' }));
  open(w, { repo: 'web' });
  assert.deepEqual(pressed(form(w), 'Model'), []);
  assert.deepEqual(store(w, 'ccboard:launch:shop/web:claude'), { model: '', effort: '' });
  // the new key wins over a legacy one that came back (an old tab)
  w.localStorage.setItem('ccboard:launch:shop/api', JSON.stringify({ model_sel: 'haiku' }));
  open(w, {});
  assert.deepEqual(pressed(form(w), 'Model'), ['custom'].filter(() => false), 'the :claude key is what is read now');
});

test('remembered per repo AND agent: a launch saves ccboard:launch:<p>/<r>:<agent>, the agent last used, and the project\'s defaults; another repo of the project starts from those', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2', cmd: 'claude ...' } } });
  open(w, {});
  let f = form(w);
  btn(f, 'Model', 'fable').click();
  btn(f, 'Effort', 'max').click();
  choose(fieldOf(f, /^Permissions$/).querySelector('select'), 'acceptEdits');
  typeInto(f.querySelector('textarea.lx-prompt'), 'do the thing');
  submit(f);
  await tick(); await tick();
  assert.deepEqual(store(w, 'ccboard:launch:shop/api:claude'), { model: 'fable', effort: 'max', permission_mode: 'acceptEdits' }, 'no prompt, no name, no resume id');
  assert.equal(store(w, 'ccboard:agent:shop/api'), 'claude');
  assert.deepEqual(store(w, 'ccboard:defaults:shop'), { agent: 'claude', claude: { model: 'fable', effort: 'max', permission_mode: 'acceptEdits' } });
  // the same repo: remembered; another repo of the project: the project's defaults; another project: the built-in opus / high
  open(w, {});
  assert.deepEqual([pressed(form(w), 'Model'), pressed(form(w), 'Effort')], [['fable'], ['max']]);
  open(w, { repo: 'web' });
  assert.deepEqual([pressed(form(w), 'Model'), pressed(form(w), 'Effort')], [['fable'], ['max']]);
  open(w, { project: 'blog', repo: 'site' });
  assert.deepEqual([pressed(form(w), 'Model'), pressed(form(w), 'Effort')], [['opus'], ['high']]);
  // Codex has its own memory under :codex, and becomes the agent the next + opens on
  open(w, { agent: 'codex' });
  f = form(w);
  btn(f, 'Codex mode', 'read-only').click();
  submit(f);
  await tick(); await tick();
  assert.deepEqual(store(w, 'ccboard:launch:shop/api:codex'), { cx_mode: 'read-only' }, 'the sandbox and approval only count in custom mode');
  assert.equal(store(w, 'ccboard:agent:shop/api'), 'codex');
  open(w, {});
  assert.deepEqual(pressed(form(w), 'Agent'), ['◇ Codex'], 'the agent last used here');
  assert.deepEqual(pressed(form(w), 'Codex mode'), ['read-only']);
  assert.deepEqual(store(w, 'ccboard:launch:shop/api:claude'), { model: 'fable', effort: 'max', permission_mode: 'acceptEdits' }, 'Claude\'s memory is untouched');
});

test('a bypass-class choice is never remembered: the next sheet starts from the default again', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2' } } });
  open(w, {});
  let f = form(w);
  choose(fieldOf(f, /^Permissions$/).querySelector('select'), 'bypassPermissions');
  tickBox(f.querySelector('.lx-ack input'));
  submit(f);
  await tick(); await tick();
  assert.equal(store(w, 'ccboard:launch:shop/api:claude').permission_mode, undefined);
  open(w, {});
  assert.equal(fieldOf(form(w), /^Permissions$/).querySelector('select').value, '');
  open(w, { agent: 'codex' });
  f = form(w);
  btn(f, 'Codex mode', 'bypass').click();
  tickBox(f.querySelector('.lx-ack input'), true);
  submit(f);
  await tick(); await tick();
  assert.equal(store(w, 'ccboard:launch:shop/api:codex').cx_mode, 'default');
});

// ---------------------------------------------------------------- the danger gate

test('the danger gate: bypass shows the red BYPASS_WARNING callout and Start waits for "I understand"; the acknowledgement is kept ONCE PER REPO (ccboard:bypass-ack:<p>/<r>)', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2', cmd: 'claude ...' } } });
  open(w, {});
  let f = form(w);
  const danger = () => f.querySelector('.lx-danger');
  assert.equal(hidden(danger()), true, 'nothing dangerous chosen: no callout');
  choose(fieldOf(f, /^Permissions$/).querySelector('select'), 'bypassPermissions');
  assert.equal(hidden(danger()), false);
  assert.equal(text(danger().querySelector('.lx-danger-t')), w.get('BYPASS_WARNING'));
  assert.ok(danger().getAttribute('role') === 'alert');
  assert.equal(hidden(f.querySelector('.lx-ack')), false, 'the checkbox is asked for');
  assert.ok(off(start(f)), 'Start & open waits');
  submit(f);
  await tick();
  assert.equal(posts(w, /\/sessions$/).length, 0, 'a submit (Enter in the prompt) goes nowhere either');
  assert.match(text(f.querySelector('.lx-ack .field-err')), /Tick I understand/);
  tickBox(f.querySelector('.lx-ack input'));
  assert.ok(!off(start(f)));
  submit(f);
  await tick(); await tick();
  const sent = posts(w, /\/sessions$/);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.permission_mode, 'bypassPermissions');
  assert.equal(sent[0].body.mode, 'bypass', 'the danger gate\'s answer is said to the server the way Codex says it: mode bypass');
  assert.equal(sent[0].body.bypass, true, '... with the acknowledgement');
  assert.equal(w.localStorage.getItem('ccboard:bypass-ack:shop/api'), '1');
  // the next time in this repo the callout is still there, the box is not and Start is free
  open(w, {});
  f = form(w);
  choose(fieldOf(f, /^Permissions$/).querySelector('select'), 'bypassPermissions');
  assert.equal(hidden(danger()), false, 'the warning is still shown');
  assert.equal(hidden(f.querySelector('.lx-ack')), true, 'but the box is not asked again in this repo');
  assert.ok(!off(start(f)));
  // another repo has not acknowledged
  open(w, { repo: 'web' });
  f = form(w);
  choose(fieldOf(f, /^Permissions$/).querySelector('select'), 'bypassPermissions');
  assert.equal(hidden(f.querySelector('.lx-ack')), false);
  assert.ok(off(start(f)));
  assert.equal(w.localStorage.getItem('ccboard:bypass-ack:shop/web'), null);
});

test('the danger gate for Codex: bypass and a custom danger-full-access sandbox show the Codex warning and the same box; Start sends bypass: true', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--cx1', cmd: 'codex ...' } } });
  open(w, { agent: 'codex' });
  const f = form(w);
  const danger = f.querySelector('.lx-danger');
  assert.equal(hidden(danger), true);
  btn(f, 'Codex mode', 'bypass').click();
  assert.equal(hidden(danger), false);
  assert.equal(text(danger.querySelector('.lx-danger-t')), w.get('BYPASS_WARNING_CODEX'));
  assert.ok(off(start(f)));
  btn(f, 'Codex mode', 'custom').click();
  assert.equal(hidden(danger), true, 'custom with the default sandbox is not dangerous');
  choose(fieldOf(f, /^Sandbox$/).querySelector('select'), 'danger-full-access');
  assert.equal(hidden(danger), false);
  assert.ok(off(start(f)));
  tickBox(f.querySelector('.lx-ack input'));
  assert.ok(!off(start(f)));
  submit(f);
  await tick(); await tick();
  const b = posts(w, /\/sessions$/)[0].body;
  assert.deepEqual([b.mode, b.sandbox, b.bypass], ['custom', 'danger-full-access', true]);
});

test('task and dispatch mode never offer bypass: no bypass permission, no bypass button, no danger-full-access sandbox in the DOM, and a remembered one is turned back', () => {
  const w = lWorld();
  w.localStorage.setItem('ccboard:task:shop/api', JSON.stringify({ model: 'opus', permission_mode: 'bypassPermissions' }));
  w.localStorage.setItem('ccboard:task:shop/api:codex', JSON.stringify({ cx_mode: 'bypass', sandbox: 'danger-full-access' }));
  const task = { id: 7, title: 'Fix it', prompt: 'fix it', project: 'shop', repo: 'api', agent: 'claude', phase: 'backlog' };
  for (const [mode, extra] of [['task', {}], ['dispatch', { task }]]) {
    open(w, { mode, ...extra });
    const f = form(w);
    const perm = fieldOf(f, /^Permissions$/).querySelector('select');
    assert.ok(!perm.children.some((o) => o.getAttribute('value') === 'bypassPermissions'), `${mode}: no bypass permission`);
    assert.equal(perm.value, '', `${mode}: a remembered bypass is back to ask`);
    assert.ok(!labels(f, 'Codex mode').includes('bypass'), `${mode}: no bypass button`);
    assert.ok(!fieldOf(f, /^Sandbox$/).querySelector('select').children.some((o) => o.getAttribute('value') === 'danger-full-access'), `${mode}: no danger sandbox`);
    btn(f, 'Agent', '◇ Codex').click();
    assert.deepEqual(pressed(f, 'Codex mode'), ['default'], `${mode}: the remembered Codex bypass is back to default`);
    assert.equal(hidden(f.querySelector('.lx-danger')), true);
    assert.equal(f.querySelectorAll('.lx-ack').length, 1);
    assert.equal(hidden(f.querySelector('.lx-ack')), true);
  }
});

// ---------------------------------------------------------------- the account line

const secs = (n) => Math.floor(Date.now() / 1000) + n;
const acct = (key, over = {}) => ({ key, email: `${key}@example.com`, name: key, label: null, plan: 'max', rl_5h: 10, rl_7d: 20, resets_5h: secs(7200), resets_7d: secs(200000), current: false, saved: true, ...over });
const accounts = (list) => ({ current: (list.find((a) => a.current) || {}).key || null, list, store: { supported: true, reason: null, count: list.length } });

test('the footer says which account a new session runs on and how much of the more used window is left; no Switch first below 85 %', () => {
  const st = STATE();
  st.accounts = accounts([acct('Demo', { current: true, rl_5h: 42, rl_7d: 71 }), acct('Work')]);
  const w = lWorld({ state: st });
  open(w, {});
  const f = form(w);
  const line = f.querySelector('.lx-acct');
  assert.equal(hidden(line), false);
  assert.equal(text(line.querySelector('.lx-acct-t')), 'Runs on Demo · 29% of the 7-day window left');
  assert.equal(hidden(line.querySelector('.lx-switch')), true);
  assert.equal(f.querySelector('.lx-foot').children[0], line, 'the line leads the footer: Start & open and Cancel follow');
  btn(f, 'Agent', '▸ Shell').click();
  assert.equal(hidden(line), true, 'a shell has no account');
});

test('at 85 % or more with another saved login that has more room: a quiet Switch first calls the shared accountSwitch, and the line repaints', async () => {
  const st = STATE();
  st.accounts = accounts([acct('Demo', { current: true, rl_5h: 91, rl_7d: 30 }), acct('Work', { rl_5h: 12, rl_7d: 40 }), acct('Spare', { rl_5h: 88 }), acct('Gone', { rl_5h: 1, saved: false })]);
  const w = lWorld({ state: st, answers: { '/api/accounts/': { ok: true } } });
  open(w, {});
  const f = form(w);
  const sw = f.querySelector('.lx-switch');
  assert.equal(hidden(sw), false);
  assert.equal(text(f.querySelector('.lx-acct-t')), 'Runs on Demo · 9% of the 5-hour window left');
  assert.equal(sw.getAttribute('title'), 'Switch to Work first, then start', 'the saved login with the most room, not the unsaved one');
  assert.ok(sw.classList.contains('minimal') && !isFilled(sw), 'quiet: never a second primary');
  sw.click();
  await tick(); await tick();
  assert.deepEqual(posts(w, /^\/api\/accounts\//).map((c) => c.path), ['/api/accounts/Work/switch']);
  assert.equal(text(f.querySelector('.lx-acct-t')), 'Runs on Work · 60% of the 7-day window left', 'repainted from the new account in use');
  assert.equal(hidden(sw), true, 'nothing hotter to leave');
});

test('no Switch first without a better saved login (all hot, or none known), or on a box that keeps no saved logins', () => {
  const hot = STATE();
  hot.accounts = accounts([acct('Demo', { current: true, rl_5h: 95 }), acct('Work', { rl_5h: 96 })]);
  let w = lWorld({ state: hot });
  open(w, {});
  assert.equal(hidden(form(w).querySelector('.lx-switch')), true);
  const nostore = STATE();
  nostore.accounts = { ...accounts([acct('Demo', { current: true, rl_5h: 95 }), acct('Work', { rl_5h: 5 })]), store: { supported: false, reason: 'macOS', count: 0 } };
  w = lWorld({ state: nostore });
  open(w, {});
  assert.equal(hidden(form(w).querySelector('.lx-switch')), true);
  assert.match(text(form(w).querySelector('.lx-acct-t')), /^Runs on Demo/);
  const none = STATE();
  w = lWorld({ state: none });
  open(w, {});
  assert.equal(hidden(form(w).querySelector('.lx-acct')), true, 'no login seen yet: no line, no "undefined"');
});

test('Codex: the footer names the Codex account in use and its window; Switch first at 85 % calls cxSwitch (the state holds no reading of the others)', async () => {
  const st = STATE();
  st.codex_accounts = { current: 'cxa', list: [{ key: 'cxa', label: 'Codex main', saved: true, current: true }, { key: 'cxb', label: 'Codex work', saved: true, current: false }], store: { supported: true, add: true, reason: null, count: 2 }, login: {} };
  st.usage_codex = { value: { primary: { used_percent: 90, window_minutes: 10080, resets_at: secs(100000) }, secondary: null, account: 'cxa' }, at: new Date().toISOString() };
  const w = lWorld({ state: st, answers: { '/api/codex-accounts/': { ok: true } } });
  open(w, { agent: 'codex' });
  const f = form(w);
  assert.equal(text(f.querySelector('.lx-acct-t')), 'Runs on Codex main · 10% of the weekly window left');
  const sw = f.querySelector('.lx-switch');
  assert.equal(hidden(sw), false);
  assert.equal(sw.getAttribute('title'), 'Switch to Codex work first, then start');
  sw.click();
  await tick(); await tick();
  assert.deepEqual(posts(w, /^\/api\/codex-accounts\//).map((c) => c.path), ['/api/codex-accounts/cxb/switch']);
  assert.match(text(f.querySelector('.lx-acct-t')), /^Runs on Codex work/);
  // a reading that belongs to another account is not this one's
  st.usage_codex.value.account = 'cxb';
  btn(f, 'Agent', '◆ Claude').click();
  btn(f, 'Agent', '◇ Codex').click();
  assert.equal(text(f.querySelector('.lx-acct-t')), 'Runs on Codex work · 10% of the weekly window left');
});

// ---------------------------------------------------------------- one primary, the keyboard

test('exactly ONE filled primary in the sheet, whatever the agent, mode or state: Start & open (Switch first, Copy, chips and presets are never filled)', () => {
  const st = STATE();
  st.accounts = accounts([acct('Demo', { current: true, rl_5h: 91 }), acct('Work', { rl_5h: 12 })]);
  const w = lWorld({ state: st });
  const task = { id: 7, title: 'Fix it', prompt: 'fix it', project: 'shop', repo: 'api', agent: 'claude', phase: 'backlog' };
  for (const [mode, extra, agents] of [['session', {}, ['◆ Claude', '◇ Codex', '▸ Shell']], ['task', {}, ['◆ Claude', '◇ Codex']], ['dispatch', { task }, ['◆ Claude', '◇ Codex']]]) {
    open(w, { mode, ...extra });
    const f = form(w);
    for (const a of agents) {
      btn(f, 'Agent', a).click();
      assert.deepEqual(filled(f).map((b) => text(b).trim()), [mode === 'session' ? 'Start & open' : (mode === 'task' ? 'Start task' : (a === '◇ Codex' ? 'Start in Codex' : 'Start in Claude'))], `${mode} / ${a}`);
    }
  }
  open(w, {});
  const f = form(w);
  choose(fieldOf(f, /^Permissions$/).querySelector('select'), 'bypassPermissions');
  assert.equal(filled(f).length, 1, 'the danger gate adds a checkbox, not a button');
  assert.equal(start(f).textContent.trim(), 'Start & open');
});

test('keyboard: Enter in the prompt starts, Shift+Enter does not, Cmd/Ctrl+Enter starts, Esc closes the sheet', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2', cmd: 'claude ...' } } });
  open(w, {});
  const f = form(w);
  const prompt = f.querySelector('textarea.lx-prompt');
  typeInto(prompt, 'hello');
  press(prompt, 'Enter', { shiftKey: true });
  assert.equal(prompt.value, 'hello\n', 'Shift+Enter is a newline');
  await tick();
  assert.equal(posts(w, /\/sessions$/).length, 0);
  const enter = press(prompt, 'Enter');
  assert.equal(enter.defaultPrevented, true);
  await tick(); await tick();
  assert.equal(posts(w, /\/sessions$/).length, 1);
  assert.equal(posts(w, /\/sessions$/)[0].body.prompt, 'hello');
  assert.equal(sheet(w).open, false, 'and the sheet closes');
  open(w, {});
  press(form(w).querySelector('textarea.lx-prompt'), 'Enter', { metaKey: true });
  await tick(); await tick();
  assert.equal(posts(w, /\/sessions$/).length, 2, 'Cmd+Enter starts too');
  open(w, {});
  assert.equal(sheet(w).open, true);
  const esc = press(form(w).querySelector('textarea.lx-prompt'), 'Escape');
  assert.equal(esc.defaultPrevented, true);
  assert.equal(sheet(w).open, false, 'Esc closes it');
  assert.equal(w.get('ui.openForm'), null);
});

// ---------------------------------------------------------------- Start

test('Start & open posts the body to the repo\'s sessions route, takes the response\'s cmd as the truth, opens the terminal and closes the sheet; the blank tab is opened on the tap', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2', attach_url: '/tty/?arg=shop--api--s2', agent: 'claude', agent_session_id: 'u', claude_session_id: 'u', cmd: "claude --session-id u --name s2 --model opus --effort high" } } });
  open(w, {});
  const f = form(w);
  typeInto(f.querySelector('textarea.lx-prompt'), 'fix the login redirect');
  btn(f, 'Model', 'sonnet').click();
  const tail = previewOf(f);
  assert.ok(tail.includes('--model sonnet'));
  submit(f);
  assert.equal(w.get('__opened').length, 1, 'a blank tab is opened synchronously, while the tap still counts for the popup blocker');
  assert.ok(off(start(f)), 'a second tap during the request does nothing');
  await tick(); await tick();
  assert.deepEqual(posts(w, /\/sessions$/).map((c) => [c.path, c.body]), [['/api/projects/shop/repos/api/sessions', {
    launcher: 'claude', agent: 'claude', prompt: 'fix the login redirect', model: 'sonnet', effort: 'high', add_dirs: ['shop/web'] }]]);
  assert.equal(previewOf(f), 'claude --session-id u --name s2 --model opus --effort high', 'the response\'s cmd replaced the approximation');
  assert.equal(w.get('__opened')[0].location, '/term/shop--api--s2');
  assert.equal(sheet(w).open, false);
  assert.equal(w.get('__polls'), 1);
  assert.deepEqual(plain(w.get('__errors')), [null]);
});

test('with the dock on, Start opens the dock through Shell.openTerm and no blank tab is made', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2', cmd: 'claude' } } });
  w.run('globalThis.__term = []; Shell.dockOn = () => true; Shell.dockHeld = () => false; Shell.openTerm = (t) => { __term.push(t); return true; };');
  open(w, {});
  submit(form(w));
  assert.equal(w.get('__opened').length, 0);
  await tick(); await tick();
  assert.deepEqual(plain(w.get('__term')), ['shop--api--s2']);
  assert.deepEqual(plain(w.get('__nav')), []);
});

test('a refusal stays on the sheet next to the field it is about (the sheet covers the banner), closes the blank tab, and keeps what was typed', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { __error: 'resume id must be a UUID', status: 400 } } });
  open(w, {});
  const f = form(w);
  btn(f, 'Launch', 'Resume').click();
  typeInto(inputWith(f, /session id/), '0a1b2c3d-0000-4000-8000-000000000001');
  submit(f);
  await tick(); await tick();
  assert.equal(w.get('__opened')[0].closed, true);
  assert.equal(sheet(w).open, true);
  assert.equal(text(fieldOf(f, /^Session to resume$/).querySelector('.field-err')), 'resume id must be a UUID');
  assert.deepEqual(plain(w.get('__errors')), ['resume id must be a UUID']);
  assert.ok(!off(start(f)), 'the button is free again');
  assert.equal(inputWith(f, /session id/).value, '0a1b2c3d-0000-4000-8000-000000000001');
});

test('client checks before the round trip: a malformed resume id, a PR that is not a number or URL, a bad -c line, an auto-compact that is not auto or a number', async () => {
  const w = lWorld();
  open(w, {});
  let f = form(w);
  btn(f, 'Launch', 'Resume').click();
  typeInto(inputWith(f, /session id/), 'not-a-uuid');
  submit(f);
  assert.match(text(fieldOf(f, /^Session to resume$/).querySelector('.field-err')), /8-4-4-4-12/);
  btn(f, 'Launch', 'From PR').click();
  typeInto(inputWith(f, /number or URL/), 'banana');
  submit(f);
  assert.match(text(fieldOf(f, /^Pull request$/).querySelector('.field-err')), /pull request number/);
  btn(f, 'Launch', 'New').click();
  typeInto(inputWith(f, /auto or/), '80%');
  submit(f);
  assert.match(text(fieldOf(f, /^Auto-compact$/).querySelector('.field-err')), /auto, or a context size/);
  assert.equal(f.querySelector('details[data-agent=claude]').getAttribute('open'), '', 'the Advanced box opens to show the field');
  btn(f, 'Agent', '◇ Codex').click();
  typeInto(f.querySelectorAll('textarea').find((t) => /one key/.test(t.getAttribute('placeholder') || '')), 'model verbosity=low');
  submit(f);
  assert.match(text(fieldOf(f, /^Config overrides$/).querySelector('.field-err')), /is not key=value/);
  await tick();
  assert.equal(posts(w, /\/sessions$/).length, 0, 'nothing was sent');
});

test('ultracode is applied after the start: /effort ultracode on through POST /command, retried while the session is not at its prompt (409), unless this box takes --effort ultracode', async () => {
  let n = 0;
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2', cmd: 'claude' }, '/api/sessions/shop--api--s2/command': () => (++n < 3 ? { __error: 'busy', status: 409 } : { ok: true }) } });
  open(w, {});
  const f = form(w);
  f.querySelectorAll('label').find((l) => /Ultracode/.test(text(l))).querySelector('input').checked = true;
  f.querySelectorAll('label').find((l) => /Ultracode/.test(text(l))).querySelector('input').dispatchEvent({ type: 'change' });
  assert.match(previewOf(f), /# then \/effort ultracode on/);
  submit(f);
  await tick(); await tick();
  assert.equal(posts(w, /\/sessions$/)[0].body.effort, 'high');
  const run = async () => { const t = w.get('__timers').shift(); if (t) { await t.fn(); await tick(); } return !!t; };
  assert.equal(w.get('__timers').length, 1, 'the follow-up waits a moment for the TUI');
  while (await run()) { /* each 409 queues the next try */ }
  assert.deepEqual(posts(w, /\/command$/).map((c) => c.body), [{ cmd: 'effort', arg: 'ultracode on' }, { cmd: 'effort', arg: 'ultracode on' }, { cmd: 'effort', arg: 'ultracode on' }]);
  // a box that takes the flag: it is the effort, nothing follows
  const w2 = lWorld({ answers: { '/api/agents': { agents: { claude: { name: 'claude', options: [{ key: 'model' }], models: ['opus'], efforts: ['low', 'high', 'ultracode'], permission_modes: ['manual'], capabilities: { ultracode_flag: true } } } },
    '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2' } } });
  open(w2, {});
  await tick(); await tick();
  const f2 = form(w2);
  const u = f2.querySelectorAll('label').find((l) => /Ultracode/.test(text(l))).querySelector('input');
  tickBox(u);
  assert.match(previewOf(f2), /--effort ultracode/);
  submit(f2);
  await tick(); await tick();
  assert.equal(posts(w2, /\/sessions$/)[0].body.effort, 'ultracode');
  assert.equal(w2.get('__timers').length, 0);
});

test('fast mode is applied after the start: /fast through POST /command with no argument, retried while the session is not at its prompt (409); with ultracode too, the two go one after the other', async () => {
  let n = 0;
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2', cmd: 'claude' }, '/api/sessions/shop--api--s2/command': () => (++n < 3 ? { __error: 'busy', status: 409 } : { ok: true }) } });
  open(w, {});
  const f = form(w);
  const fastBox = f.querySelectorAll('label').find((l) => /Fast mode/.test(text(l)));
  assert.match(fastBox.getAttribute('title') || fastBox.querySelector('input').getAttribute('title') || text(fastBox), /\/fast/, 'the box says what the board types');
  tickBox(fastBox.querySelector('input'));
  assert.match(previewOf(f), /# then \/fast/);
  submit(f);
  await tick(); await tick();
  assert.equal(posts(w, /\/sessions$/)[0].body.fast, true, 'the route stores it');
  assert.equal(w.get('__timers').length, 1, 'the follow-up waits a moment for the TUI');
  const run = async () => { const t = w.get('__timers').shift(); if (t) { await t.fn(); await tick(); } return !!t; };
  while (await run()) { /* each 409 queues the next try */ }
  assert.deepEqual(posts(w, /\/command$/).map((c) => c.body), [{ cmd: 'fast' }, { cmd: 'fast' }, { cmd: 'fast' }], '/fast takes no argument: three tries, the third landed');
  assert.equal(n, 3);

  // ultracode and fast for one session: the second command is not typed until the first has landed
  const sent = [];
  const w2 = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2', cmd: 'claude' }, '/api/sessions/shop--api--s2/command': ({ body }) => { sent.push(body); return { ok: true }; } } });
  open(w2, {});
  const f2 = form(w2);
  for (const re of [/Ultracode/, /Fast mode/]) tickBox(f2.querySelectorAll('label').find((l) => re.test(text(l))).querySelector('input'));
  assert.match(previewOf(f2), /# then \/effort ultracode on · \/fast/);
  submit(f2);
  await tick(); await tick();
  assert.equal(w2.get('__timers').length, 1, 'only the first follow-up is waiting');
  await w2.get('__timers').shift().fn(); await tick(); await tick();
  assert.deepEqual(plain(sent), [{ cmd: 'effort', arg: 'ultracode on' }], 'the second has not been typed yet');
  assert.equal(w2.get('__timers').length, 1, 'it starts once the first has landed');
  await w2.get('__timers').shift().fn(); await tick();
  assert.deepEqual(plain(sent), [{ cmd: 'effort', arg: 'ultracode on' }, { cmd: 'fast' }]);
  // a box whose schema has no fast option never types it
  const w3 = lWorld({ answers: { '/api/agents': { agents: { claude: { name: 'claude', options: [{ key: 'model' }], models: ['opus'], efforts: ['low', 'high'], permission_modes: ['manual'], capabilities: {} } } },
    '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2' } } });
  open(w3, {});
  await tick(); await tick();
  assert.equal(form(w3).querySelectorAll('label').filter((l) => /Fast mode/.test(text(l)) && !hidden(l)).length, 0, 'no Fast box without the option');
});

// ---------------------------------------------------------------- presets

test('preset chips: "opus high worktree" and "ultracode" are built in; a chip fills the fields and shows pressed while the fields match; per-repo presets are saved, applied and removed', () => {
  const w = lWorld();
  open(w, {});
  const f = form(w);
  const chips = () => f.querySelectorAll('.lx-presets button.chip-btn').map(text);
  assert.deepEqual(chips().slice(0, 2), ['opus high worktree', 'ultracode']);
  assert.ok(chips().includes('+ save these'));
  const chip = (name) => f.querySelectorAll('.lx-presets button.chip-btn').find((b) => text(b) === name);
  btn(f, 'Model', 'haiku').click();
  btn(f, 'Effort', 'low').click();
  chip('opus high worktree').click();
  assert.deepEqual([pressed(f, 'Model'), pressed(f, 'Effort')], [['opus'], ['high']]);
  assert.match(previewOf(f), /--worktree s2-xxxxxx --session-id '<uuid>' --name s2/, 'the worktree box is on (named after the next free session, s2: shop/api has an s1)');
  assert.equal(chip('opus high worktree').getAttribute('aria-pressed'), 'true');
  btn(f, 'Effort', 'max').click();
  assert.equal(chip('opus high worktree').getAttribute('aria-pressed'), 'false', 'no longer matches');
  chip('ultracode').click();
  assert.match(previewOf(f), /# then \/effort ultracode on/);
  // save the current choices as a preset of this repo (for this agent)
  btn(f, 'Model', 'fable').click();
  btn(f, 'Effort', 'medium').click();
  chip('+ save these').click();
  const name = f.querySelector('.lx-saverow input');
  assert.equal(hidden(name), false);
  typeInto(name, 'fable quick');
  f.querySelector('.lx-saverow').querySelectorAll('button').find((b) => text(b) === 'Save').click();
  assert.ok(chips().includes('fable quick'));
  assert.deepEqual(store(w, 'ccboard:presets:shop/api').list.map((p) => [p.agent, p.name, p.v.model, p.v.effort]), [['claude', 'fable quick', 'fable', 'medium']]);
  btn(f, 'Model', 'sonnet').click();
  chip('fable quick').click();
  assert.deepEqual([pressed(f, 'Model'), pressed(f, 'Effort')], [['fable'], ['medium']]);
  // another repo does not see it; another agent neither
  open(w, { repo: 'web' });
  assert.ok(!form(w).querySelectorAll('.lx-presets button.chip-btn').map(text).includes('fable quick'));
  open(w, { agent: 'codex' });
  assert.ok(!form(w).querySelectorAll('.lx-presets button.chip-btn').map(text).includes('fable quick'));
  open(w, {});
  const g = form(w);
  g.querySelectorAll('button').find((b) => b.getAttribute('aria-label') === 'Remove preset fable quick').click();
  assert.deepEqual(store(w, 'ccboard:presets:shop/api').list, []);
});

// ---------------------------------------------------------------- the command preview

test('the preview is a read-only monospace line with Copy, and follows every control; Copy puts it on the clipboard', async () => {
  const w = lWorld();
  const copied = [];
  w.ctx.navigator.clipboard = { writeText: async (t) => { copied.push(t); } };
  open(w, {});
  const f = form(w);
  const pre = f.querySelector('pre.lx-cmd');
  assert.ok(pre.getAttribute('aria-label') === 'Command preview');
  const before = text(pre);
  btn(f, 'Effort', 'low').click();
  assert.notEqual(text(pre), before);
  typeInto(f.querySelector('textarea.lx-prompt'), 'go');
  assert.match(text(pre), / -- go$/);
  f.querySelector('.lx-copy').click();
  await tick();
  assert.deepEqual(copied, [text(pre)]);
  assert.ok(plain(w.get('__toasts')).some((t) => t.text === 'Copied'));
});

// ---------------------------------------------------------------- shell, cwd_rel

test('Shell: a name and the devcontainer (where the repo has one), nothing else; it posts launcher shell', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/web/sessions': { tmux: 'shop--web--s1', cmd: null } } });
  open(w, { repo: 'web', agent: 'claude' });
  const f = form(w);
  btn(f, 'Agent', '▸ Shell').click();
  assert.ok(f.querySelectorAll('label').some((l) => /Run in the devcontainer/.test(text(l)) && !hidden(l)));
  typeInto(f.querySelector('input[placeholder^="auto:"]'), 'sh1');
  submit(f);
  await tick(); await tick();
  assert.deepEqual(posts(w, /\/sessions$/).map((c) => c.body), [{ launcher: 'shell', agent: 'shell', name: 'sh1' }]);
  assert.equal(store(w, 'ccboard:agent:shop/web'), 'shell');
});

test('cwd_rel is passed on as given (the project page\'s tree starts a session in a folder)', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/sessions': { tmux: 'shop--api--s2' } } });
  open(w, { cwd_rel: 'packages/ui' });
  submit(form(w));
  await tick(); await tick();
  assert.equal(posts(w, /\/sessions$/)[0].body.cwd_rel, 'packages/ui');
});

// ---------------------------------------------------------------- the entry: names and objects, false when nothing opens

test('openLauncher takes the state objects (what launch() passes) or names, titles the sheet with the place, and answers false when the place is unknown', () => {
  const w = lWorld();
  w.run(`globalThis.__r = openLauncher({ project: state.projects[0], repo: state.projects[0].repos[1], mode: 'session' })`);
  assert.ok(w.get('__r'));
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New session · shop/web');
  open(w, { repo: 'root' });
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New session · shop · project folder');
  w.run(`openLauncher({ project: 'shop', repo: 'api', label: 'shop/api (here)' })`);
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New session · shop/api (here)');
  assert.equal(w.run(`openLauncher({ project: 'nope', repo: 'api' })`), false);
  assert.equal(w.run(`openLauncher({ project: 'shop', repo: 'api', mode: 'dispatch' })`), false, 'a dispatch needs its task');
  assert.equal(w.get('ui.openForm'), 'sheet');
  sheet(w).close();
  assert.equal(w.get('ui.openForm'), null, 'closing the sheet clears it');
  assert.equal(w.run('typeof sessionForm + typeof taskForm + typeof launchControls + typeof jobForm + typeof confirmButton + typeof field + typeof selectEl'), 'function'.repeat(7), 'the old names still work');
});

test('a task\'s extra directories are a choice: the sibling repo is listed under Advanced but not checked, and ticking it sends add_dirs (a session starts with it checked)', async () => {
  const w = lWorld({ answers: { '/api/tasks': { id: 31, slug: 'fix-login', tmux: 'shop--api--t-fix-login' } } });
  open(w, { mode: 'task' });
  const f = form(w);
  const sib = fieldOf(f.querySelector('details[data-agent=claude]'), /^Also give access to$/);
  assert.deepEqual(sib.querySelectorAll('input').map((i) => i.checked), [false]);
  tickBox(sib.querySelector('input'));
  typeInto(f.querySelector('textarea.lx-prompt'), 'Fix it');
  submit(f);
  await tick(); await tick();
  assert.deepEqual(posts(w, /\/api\/tasks$/)[0].body.add_dirs, ['shop/web']);
});

// ---------------------------------------------------------------- create from anywhere: the create route ends in the sheet

test('Shell.openCreate(session | task, place) and the repo picker\'s rows open the launcher sheet (with the chevron back to the picker); a schedule keeps its own form', () => {
  const w = lWorld();
  assert.equal(w.run(`Shell.openCreate('session', { project: 'shop', repo: 'api' })`), true);
  assert.ok(sheet(w).querySelector('form.lx-form'), 'the launcher sheet, not the old session form');
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New session · shop/api');
  assert.ok(sheet(w).querySelector('.sheet-back') || sheet(w).querySelector('[aria-label="Back to the repo list"]'), 'a chevron back to the repo list');
  assert.equal(w.run(`Shell.openCreate('task', { project: 'shop', repo: 'web' })`), true);
  assert.ok(sheet(w).querySelector('form.lx-form.task-form'), 'task mode of the same sheet');
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New task · shop/web');
  assert.equal(w.run(`Shell.openCreate('schedule', { project: 'shop', repo: 'api' })`), true);
  assert.equal(sheet(w).querySelector('form.lx-form'), null, 'a schedule is the job form');
  w.run(`Shell.openCreate('session')`);                                           // no place: the picker, whose row opens the sheet
  const row = sheet(w).querySelectorAll('button.pick-row').find((b) => /shop\/api/.test(text(b)));
  assert.ok(row, 'the picker lists shop/api');
  row.click();
  assert.ok(sheet(w).querySelector('form.lx-form'));
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New session · shop/api');
});

// ---------------------------------------------------------------- task mode

const TASK_OK = { id: 31, slug: 'fix-login', tmux: 'shop--api--t-fix-login', branch: 'worktree-fix-login' };

test('task mode: Run Now | Later | Schedule, title, prompt, from-GitHub-issue and Close when finished; Now posts /api/tasks with the launch choices and the card appears at once', async () => {
  const w = lWorld({ answers: { '/api/tasks': TASK_OK } });
  open(w, { mode: 'task' });
  const f = form(w);
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New task · shop/api');
  assert.deepEqual(labels(f, 'Run'), ['Now', 'Later', 'Schedule']);
  assert.deepEqual(pressed(f, 'Run'), ['Now']);
  assert.equal(text(start(f)).trim(), 'Start task');
  assert.deepEqual(fieldOf(f, /^Repo$/).querySelector('select').children.map(text), ['shop/api', 'shop/web', 'shop · project folder'], 'the places a task can start in this project');
  assert.ok(f.querySelectorAll('label.field-label').map(text).includes('From a GitHub issue'));
  assert.ok(f.querySelectorAll('label.field-label').map(text).includes('When it finishes'));
  submit(f);
  await tick();
  assert.match(text(fieldOf(f, /^Prompt$/).querySelector('.field-err')), /Write what it should do/);
  assert.equal(posts(w, /\/api\/tasks$/).length, 0);
  typeInto(f.querySelector('textarea.lx-prompt'), 'Fix the login redirect\nmore detail');
  btn(f, 'Model', 'sonnet').click();
  submit(f);
  await tick(); await tick();
  assert.deepEqual(posts(w, /\/api\/tasks$/).map((c) => c.body), [{ project: 'shop', repo: 'api', agent: 'claude', model: 'sonnet', effort: 'high', title: 'Fix the login redirect', prompt: 'Fix the login redirect\nmore detail', when: 'now' }],
    'a task does not start with its sibling repos checked (the old task form did not either): no add_dirs');
  assert.equal(sheet(w).open, false);
  assert.ok(plain(w.get('__toasts')).some((t) => /started fix-login/.test(t.text)));
  assert.deepEqual(store(w, 'ccboard:task:shop/api'), { model: 'sonnet', effort: 'high', when: 'now', auto_close: true, cron: '', job_mode: 'acceptEdits', max_turns: 30 }, 'Claude keeps the bare key components.js and dnd.js read');
  assert.equal(w.localStorage.getItem('ccboard:task:last:shop'), 'api');
});

test('task mode: on a mouse, picking Run moves on to the prompt, an arrow key on the control keeps its own focus, and on touch nothing is focused (no keyboard)', () => {
  const w = lWorld();
  open(w, { mode: 'task' });
  const f = form(w);
  const prompt = f.querySelector('textarea.lx-prompt');
  assert.equal(w.document.activeElement, prompt, 'a task opens on its prompt');
  w.document.activeElement.blur();
  btn(f, 'Run', 'Later').click();
  assert.equal(w.document.activeElement, prompt);
  const later = btn(f, 'Run', 'Later');
  press(later, 'ArrowRight');
  assert.deepEqual(pressed(f, 'Run'), ['Schedule']);
  assert.equal(w.document.activeElement, btn(f, 'Run', 'Schedule'), 'the arrow key leaves the focus on the control');
  const t = lWorld({ coarse: true });
  open(t, { mode: 'task' });
  const tf = form(t);
  btn(tf, 'Run', 'Later').click();
  assert.notEqual(t.document.activeElement, tf.querySelector('textarea.lx-prompt'), 'a phone raises no keyboard');
});

test('task mode: Later adds a Backlog card, Close when finished OFF is the only auto_close sent, the title field wins over the first line, the mode is remembered per repo', async () => {
  const w = lWorld({ answers: { '/api/tasks': { id: 32, phase: 'backlog' } } });
  open(w, { mode: 'task' });
  let f = form(w);
  btn(f, 'Run', 'Later').click();
  assert.equal(text(start(f)).trim(), 'Add to backlog');
  typeInto(inputWith(f, /Fix the login/), 'Login redirect');
  typeInto(f.querySelector('textarea.lx-prompt'), 'body');
  tickBox(f.querySelectorAll('label').find((l) => /Close the session/.test(text(l))).querySelector('input'), false);
  submit(f);
  await tick(); await tick();
  const b = posts(w, /\/api\/tasks$/)[0].body;
  assert.deepEqual([b.when, b.title, b.auto_close], ['later', 'Login redirect', false]);
  open(w, { mode: 'task' });
  f = form(w);
  assert.deepEqual(pressed(f, 'Run'), ['Later'], 'Later is remembered');
  assert.equal(f.querySelectorAll('label').find((l) => /Close the session/.test(text(l))).querySelector('input').checked, false);
});

test('task mode, Schedule: a headless run through POST /jobs (name, cron presets, mode, turns); both agents can schedule', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/jobs': { ok: true } } });
  open(w, { mode: 'task', when: 'schedule' });
  const f = form(w);
  assert.deepEqual(pressed(f, 'Run'), ['Schedule']);
  assert.ok(!off(btn(f, 'Agent', '◇ Codex')), 'v0.5.16: Codex can schedule too');
  assert.ok(!hidden(group(f, 'Agent')), 'the agent is chosen in Schedule mode');
  assert.equal(hidden(f.querySelector('.lx-agentbox[data-agent=claude]')), true, 'the model and effort are not part of a Claude schedule');
  assert.equal(hidden(f.querySelector('.lx-agentbox[data-agent=codex]')), true);
  assert.equal(text(start(f)).trim(), 'Schedule');
  typeInto(f.querySelector('textarea.lx-prompt'), 'Run the audit');
  f.querySelectorAll('button.chip-btn').find((b) => text(b) === 'nightly 02:30').click();
  submit(f);
  await tick(); await tick();
  assert.deepEqual(posts(w, /\/jobs$/).map((c) => c.body), [{ name: 'Run the audit', prompt: 'Run the audit', permission_mode: 'acceptEdits', max_turns: 30, run_now: false, cron: '30 2 * * *' }]);
  const w2 = lWorld();
  open(w2, { mode: 'task', agent: 'codex' });
  assert.ok(!off(btn(form(w2), 'Run', 'Schedule')));
  assert.equal(store(w, 'ccboard:task:shop/api').when, undefined, 'a schedule is never the next task\'s mode');
});

test('task mode, Schedule with Codex: model and reasoning only, no turns or budget, the body names the agent, the Codex task key remembers it', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/jobs': { ok: true } } });
  open(w, { mode: 'task', when: 'schedule', agent: 'codex' });
  const f = form(w);
  assert.deepEqual(pressed(f, 'Agent'), ['◇ Codex']);
  assert.equal(hidden(f.querySelector('.lx-agentbox[data-agent=codex]')), false, 'Codex\'s model and reasoning show in Schedule');
  assert.equal(hidden(fieldOf(f, /^Mode$/)), true, 'the schedule\'s own permission mode replaces the Codex mode picker');
  assert.equal(hidden(fieldOf(f, /^Max turns$/)), true);
  assert.equal(hidden(fieldOf(f, /^Max \$$/)), true);
  assert.match(text(f.querySelector('.tf-lede')), /codex exec/);
  choose(fieldOf(f, /^Model$/).querySelector('select'), 'gpt-5.5');
  btn(f, 'Reasoning', 'high').click();
  typeInto(f.querySelector('textarea.lx-prompt'), 'Review main');
  f.querySelectorAll('button.chip-btn').find((b) => text(b) === 'weekdays 09:00').click();
  submit(f);
  await tick(); await tick();
  const body = posts(w, /\/jobs$/)[0].body;
  assert.deepEqual(body, { name: 'Review main', prompt: 'Review main', permission_mode: 'acceptEdits', run_now: false, cron: '0 9 * * 1-5', agent: 'codex', model: 'gpt-5.5', reasoning_effort: 'high' });
  const kept = store(w, 'ccboard:task:shop/api:codex');
  assert.equal(kept.model, 'gpt-5.5');
  assert.equal(kept.reasoning_effort, 'high');
  assert.equal(kept.cron, '0 9 * * 1-5');
  assert.equal(kept.when, undefined, 'a schedule is never the next task\'s mode');
  const f2 = lWorld();
  open(f2, { mode: 'task', when: 'schedule' });
  btn(form(f2), 'Agent', '◇ Codex').click();
  assert.deepEqual(pressed(form(f2), 'Agent'), ['◇ Codex'], 'the agent can be switched to Codex while Schedule is picked');
  assert.equal(hidden(fieldOf(form(f2), /^Max turns$/)), true);
  btn(form(f2), 'Agent', '◆ Claude').click();
  assert.equal(hidden(fieldOf(form(f2), /^Max turns$/)), false, 'and back');
});

test('task mode, Codex: the same sheet with Codex\'s fields; the body names the agent, reasoning and the permission word; its memory is ccboard:task:<p>/<r>:codex', async () => {
  const w = lWorld({ answers: { '/api/tasks': TASK_OK } });
  open(w, { mode: 'task', agent: 'codex' });
  const f = form(w);
  choose(fieldOf(f, /^Model$/).querySelector('select'), 'gpt-5.5');
  btn(f, 'Reasoning', 'high').click();
  btn(f, 'Codex mode', 'read-only').click();
  typeInto(f.querySelector('textarea.lx-prompt'), 'review the diff');
  submit(f);
  await tick(); await tick();
  const b = posts(w, /\/api\/tasks$/)[0].body;
  assert.deepEqual([b.agent, b.model, b.reasoning_effort, b.permission_mode, b.when], ['codex', 'gpt-5.5', 'high', 'plan', 'now']);
  assert.ok(!('bypass' in b) && !('worktree' in b));
  const saved = store(w, 'ccboard:task:shop/api:codex');
  assert.deepEqual([saved.model, saved.reasoning_effort, saved.cx_mode], ['gpt-5.5', 'high', 'read-only']);
});

test('task mode, Then…: a chain posts POST /chains with step 1\'s choices on the first step, and the repo select re-opens the sheet for another place with the text carried', async () => {
  const w = lWorld({ answers: { '/api/projects/shop/repos/api/chains': { chain_id: 9, ids: [41, 42], started: { tmux: 'shop--api--t-a', slug: 'a' } } } });
  open(w, { mode: 'task' });
  let f = form(w);
  typeInto(f.querySelector('textarea.lx-prompt'), 'Step one');
  const then = f.querySelector('details.tf-then');
  then.open = true;
  then.dispatchEvent({ type: 'toggle' });
  typeInto(then.querySelector('textarea'), 'Review it');
  submit(f);
  await tick(); await tick();
  const body = posts(w, /\/chains$/)[0].body;
  assert.equal(body.dispatch, true);
  assert.deepEqual(body.steps.map((s) => [s.title, s.agent]), [['Step one', 'claude'], ['Review it', 'claude']]);
  assert.deepEqual([body.steps[0].model, body.steps[0].effort], ['opus', 'high']);
  // switching the repo keeps the text
  open(w, { mode: 'task' });
  f = form(w);
  typeInto(f.querySelector('textarea.lx-prompt'), 'carry me');
  choose(fieldOf(f, /^Repo$/).querySelector('select'), '1');
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'New task · shop/web');
  assert.equal(form(w).querySelector('textarea.lx-prompt').value, 'carry me');
});

test('task mode refuses bypass spellings typed into the extra args before the round trip', async () => {
  const w = lWorld();
  open(w, { mode: 'task' });
  const f = form(w);
  typeInto(f.querySelector('textarea.lx-prompt'), 'do it');
  typeInto(inputWith(f, /anything else/), '--dangerously-skip-permissions');
  submit(f);
  assert.match(text(fieldOf(f, /^Extra args$/).querySelector('.field-err')), /bypassPermissions is not allowed for tasks/);
  await tick();
  assert.equal(posts(w, /\/api\/tasks$/).length, 0);
});

// ---------------------------------------------------------------- dispatch mode

test('dispatch mode: a new session of either agent with the launch choices (taskStart), or a running session (taskSend); Close when finished defaults on for a new session and off for a running one', async () => {
  const task = { id: 7, title: 'Fix it', prompt: 'fix it', project: 'shop', repo: 'api', agent: 'claude', phase: 'backlog', slug: 'fix-it' };
  const st = STATE();
  st.projects[0].repos[0].sessions = [{ name: 's1', tmux: 'shop--api--s1', state: 'idle', agent: 'claude' }];
  st.tasks = [task];
  const w = lWorld({ state: st, answers: { '/api/tasks/7/dispatch': { id: 7, phase: 'running', tmux: 'shop--api--t-fix-it', slug: 'fix-it' } } });
  w.run(`globalThis.__sheetsClosed = 0;`);
  open(w, { mode: 'dispatch', task });
  let f = form(w);
  assert.equal(text(sheet(w).querySelector('.sheet-title')), 'Start “Fix it”');
  assert.deepEqual(labels(f, 'Where'), ['New session', 'Running session']);
  assert.deepEqual(pressed(f, 'Where'), ['New session']);
  assert.equal(text(start(f)).trim(), 'Start in Claude');
  const close = f.querySelectorAll('label').find((l) => /Close the session/.test(text(l))).querySelector('input');
  assert.equal(close.checked, true);
  assert.equal(hidden(f.querySelector('textarea.lx-prompt')), true, 'the prompt is the task\'s');
  btn(f, 'Model', 'haiku').click();
  btn(f, 'Effort', 'low').click();
  submit(f);
  await tick(); await tick();
  assert.deepEqual(posts(w, /\/dispatch$/).map((c) => c.body), [{ mode: 'lane', model: 'haiku', effort: 'low' }], 'Claude and auto-close on are the server\'s defaults: not sent');
  // a Codex lane with the switch off, then a running session
  open(w, { mode: 'dispatch', task });
  f = form(w);
  btn(f, 'Agent', '◇ Codex').click();
  assert.equal(text(start(f)).trim(), 'Start in Codex');
  tickBox(f.querySelectorAll('label').find((l) => /Close the session/.test(text(l))).querySelector('input'), false);
  btn(f, 'Codex mode', 'read-only').click();
  submit(f);
  await tick(); await tick();
  const b = posts(w, /\/dispatch$/)[1].body;
  assert.deepEqual([b.agent, b.auto_close, b.permission_mode, b.mode], ['codex', false, 'plan', 'lane']);
  open(w, { mode: 'dispatch', task });
  f = form(w);
  btn(f, 'Where', 'Running session').click();
  assert.equal(f.querySelectorAll('label').find((l) => /Close the session/.test(text(l))).querySelector('input').checked, false, 'off for a running session');
  assert.equal(hidden(fieldOf(f, /^Session$/)), false);
  assert.equal(hidden(f.querySelector('pre.lx-cmd')), true, 'no command: the prompt is pasted');
  assert.equal(text(start(f)).trim(), 'Send to the session');
  submit(f);
  await tick(); await tick();
  assert.deepEqual(posts(w, /\/dispatch$/)[2].body, { session: 'shop--api--s1' });
});

test('dispatch mode takes the preset launch() passes at the top level: agent, session and auto_close', () => {
  const task = { id: 7, title: 'Fix it', prompt: 'fix it', project: 'shop', repo: 'api', agent: 'claude', phase: 'backlog', slug: 'fix-it' };
  const st = STATE();
  st.tasks = [task];
  const w = lWorld({ state: st });
  open(w, { mode: 'dispatch', task, agent: 'codex', auto_close: false });
  const f = form(w);
  assert.deepEqual(pressed(f, 'Agent'), ['◇ Codex']);
  assert.equal(f.querySelectorAll('label').find((l) => /Close the session/.test(text(l))).querySelector('input').checked, false);
});

// ---------------------------------------------------------------- the old names

test('sessionForm, taskForm, launchControls and the LAUNCH_KEY helpers keep working next to the sheet (other code and tests call them)', () => {
  const w = lWorld();
  w.run(`globalThis.__f = sessionForm(state.projects[0], state.projects[0].repos[0]); globalThis.__t = taskForm(state.projects[0], state.projects[0].repos[0]);`);
  assert.ok(w.get('__f').querySelector('select'));
  assert.ok(w.get('__t').querySelector('textarea'));
  assert.equal(w.run('LAUNCH_KEY({name: "shop"}, {name: "api"})'), 'ccboard:launch:shop/api');
});


// ---------------------------------------------------------------- the gotchas strip (v0.5.20, pages/memory.js memoryGotchasMount)

const PALACE = JSON.parse(readFileSync(new URL('../../app/static/demo/memory_palace.json', import.meta.url), 'utf8'));
const memState = () => ({ ...STATE(), memory: { state: 'up', version: '13.31.0', observations: 10 } });
/** A launcher world that has the Memory page script, claude-mem in its state and a palace answer; the timers (setTimeout 0 after the sheet painted) are run by the test. */
function gWorld(answer, over = {}) {
  const w = lWorld({ state: memState(), answers: { '/api/memory/shop/palace': answer }, ...over });
  w.load('pages/memory.js');
  return w;
}
const memTimers = (w) => w.get('__timers').filter((t) => t.ms === 0);
const flush = async () => { for (let i = 0; i < 6; i += 1) await tick(); };
const palaceCalls = (w) => calls(w).filter((c) => c.path.startsWith('/api/memory/shop/palace'));

test('gotchas strip in the launcher: the sheet paints first, the palace is asked once per open from a timer, and the strip lands under the repo field', async () => {
  const w = gWorld(PALACE);
  open(w, {});
  const f = form(w);
  assert.ok(f, 'the form is in the sheet before anything was asked');
  assert.equal(palaceCalls(w).length, 0, 'opening the sheet asks nothing');
  assert.ok(hidden(f.querySelector('.lx-mem')), 'the host is empty and hidden');
  for (const t of memTimers(w)) t.fn();
  await flush();
  assert.equal(palaceCalls(w).length, 1, 'one fetch per open');
  const strip = f.querySelector('.lx-mem .mem-gotchas');
  assert.ok(strip);
  assert.equal(strip.querySelectorAll('.mem-gotcha').length, PALACE.gotchas.length);
  assert.equal(hidden(f.querySelector('.lx-mem')), false);
  assert.equal(w.document.activeElement && strip.contains(w.document.activeElement), false, 'the strip never takes the focus');
});

test('gotchas strip in the launcher: a worker that never answers leaves the sheet usable, with no strip, no toast and no error', async () => {
  const w = gWorld(() => new Promise(() => {}));
  open(w, {});
  const f = form(w);
  typeInto(f.querySelector('textarea'), 'fix the cart badge');
  const before = store(w, 'ccboard:agent:shop/api');
  for (const t of memTimers(w)) t.fn();
  await flush();
  assert.equal(f.querySelector('.lx-mem .mem-gotchas'), null);
  assert.ok(hidden(f.querySelector('.lx-mem')));
  assert.equal(f.querySelector('textarea').value, 'fix the cart badge', 'what was typed is untouched');
  assert.equal(off(start(f)), false, 'Start is not held back');
  assert.deepEqual(plain(w.get('__toasts')), []);
  assert.deepEqual(plain(w.get('__errors')), []);
  assert.deepEqual(store(w, 'ccboard:agent:shop/api'), before, 'the remembered agent is not touched');
});

test('gotchas strip in the launcher: a stopped worker (503) or an answer with no gotchas shows nothing and says nothing', async () => {
  const down = gWorld({ __error: 'connection refused', status: 503, data: { state: 'down', up: false, reason: 'connection refused' } });
  open(down, {});
  for (const t of memTimers(down)) t.fn();
  await flush();
  assert.equal(form(down).querySelector('.mem-gotchas'), null);
  assert.deepEqual(plain(down.get('__toasts')), []);
  assert.deepEqual(plain(down.get('__errors')), []);
  const none = gWorld({ ...PALACE, gotchas: [] });
  open(none, {});
  for (const t of memTimers(none)) t.fn();
  await flush();
  assert.equal(form(none).querySelector('.mem-gotchas'), null);
  assert.ok(hidden(form(none).querySelector('.lx-mem')));
});

test('gotchas strip in the launcher: an answer that arrives after the sheet closed is dropped', async () => {
  const w = gWorld(PALACE);
  open(w, {});
  const f = form(w);
  for (const t of memTimers(w)) t.fn();
  w.run('closeSheet()');
  await flush();
  assert.equal(f.querySelector('.mem-gotchas'), null, 'nothing is painted into a closed sheet');
});

test('gotchas strip in the launcher: only a new-session sheet with claude-mem in the state asks; a task sheet and a board without claude-mem do not', async () => {
  const task = gWorld(PALACE);
  open(task, { mode: 'task' });
  assert.equal(form(task).querySelector('.lx-mem'), null, 'no host in a task form');
  for (const t of memTimers(task)) t.fn();
  await flush();
  assert.equal(palaceCalls(task).length, 0);
  const off_ = lWorld({ state: STATE(), answers: { '/api/memory/shop/palace': PALACE } });
  off_.load('pages/memory.js');
  open(off_, {});
  for (const t of memTimers(off_)) t.fn();
  await flush();
  assert.equal(palaceCalls(off_).length, 0, 'no state.memory: nothing is asked');
});

test('gotchas strip in the launcher: markup in a gotcha title is shown as text', async () => {
  const w = gWorld({ ...PALACE, gotchas: [{ ...PALACE.gotchas[0], title: '<img src=x onerror=1>' }] });
  open(w, {});
  for (const t of memTimers(w)) t.fn();
  await flush();
  const f = form(w);
  assert.equal(text(f.querySelector('.mem-gotcha-t')), '<img src=x onerror=1>');
  assert.equal(f.querySelectorAll('img').length, 0);
});
