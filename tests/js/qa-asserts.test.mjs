// The page assertions of scripts/qa-ui.sh (issue #176), run for real: the JS the script sends to the browser for '#/tasks' and '#/p/phasezero?tab=tasks' is
// pulled out of the script and evaluated against the demo fixture (app/static/demo/state.json) in the vm world, so a stale assertion fails here and not
// only in a QA run on a board. What each assertion had gone stale against:
//   * the first Backlog card of the demo is a queued chain step (v0.5.16), which has no Start by design: the check looks at a startable card (data-phase backlog);
//   * the 'in <session>' chip became the owner chip (a.tk-owner: agent glyph, session, state, age) in v0.5.14b;
//   * the session peek is a sheet at every width since v0.5.9 (the terminal dock owns #dock from 1024 px); session.js peekSurface is pinned in dock.test.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { ROOT, STATIC } from './harness.mjs';
import { installDom } from './minidom.mjs';
import { makeNetworkWorld, settle } from './treekit.mjs';

const SCRIPT = fs.readFileSync(path.join(ROOT, 'scripts', 'qa-ui.sh'), 'utf8');
const DEMO = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8'));
const PAGE_FILES = ['home', 'inbox', 'tasks', 'project', 'agents', 'doctor', 'settings', 'search', 'session', 'onboarding', 'placeholders'];

/** The JS expression qa-ui.sh's extra_check() prints for one route, with the shell's double-quote escapes undone. */
function qaExpression(route) {
  const lines = SCRIPT.split('\n');
  const at = lines.findIndex((l) => l.trim() === `"${route}")`);
  assert.ok(at >= 0, `extra_check has a case for ${route}`);
  const body = lines.slice(at + 1).find((l) => l.includes("printf '%s' \""));
  const m = /printf '%s' "(.*)" ;;\s*$/.exec(body);
  assert.ok(m, `the case for ${route} prints one expression`);
  return m[1].replace(/\\(["\\$`])/g, '$1');
}

async function boardAt(hash) {
  const env = makeNetworkWorld({ extra: { matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) } });
  const { w } = env;
  for (const f of ['live.js', 'launcher.js', 'tree.js', 'router.js', 'pages/widgets.js', 'shell.js']) w.load(f);
  w.run('poll = async () => {}; navigate = () => {};');
  for (const f of PAGE_FILES) if (fs.existsSync(path.join(STATIC, 'pages', `${f}.js`))) w.load(`pages/${f}.js`);
  w.ctx.__st = DEMO;
  w.run('state = __st');
  w.location.hash = hash;
  await settle();
  return w;
}

for (const route of ['#/tasks', '#/p/phasezero?tab=tasks']) {
  test(`qa-ui.sh ${route}: the demo has a startable Backlog card${route === '#/tasks' ? '' : ' and the owner chip on the handed in-progress card'}`, async () => {
    const w = await boardAt(route);
    const answer = String(w.run(qaExpression(route)));
    assert.equal(answer, "ok");
  });
}

test('qa-ui.sh does not look for a Start on a queued card, an "in <session>" chip text, or the peek in #dock', () => {
  assert.doesNotMatch(SCRIPT, /\/\^in\\\\s\//, 'the chip is found by its class (tk-owner), not by an "in " prefix');
  assert.doesNotMatch(SCRIPT, /want_peek=dock/, 'the terminal dock owns #dock from 1024 px');
  assert.match(SCRIPT, /want_peek=sheet/);
  assert.match(SCRIPT, /data-phase'\) === 'backlog'/, 'the Start check picks a card of phase backlog');
});

test('the demo fixture still has what those checks need: a startable Backlog card in phasezero, a queued card ahead of it, and a session-mode task with a live session', () => {
  const t = DEMO.tasks;
  assert.ok(t.some((x) => x.project === 'phasezero' && x.phase === 'backlog'));
  assert.ok(t.some((x) => x.phase === 'queued'), 'the queued chain step is why the first card of #/tasks has no Start');
  assert.ok(t.some((x) => x.project === 'phasezero' && x.mode === 'session' && x.tmux && x.session && x.column === 'in_progress'));
});
