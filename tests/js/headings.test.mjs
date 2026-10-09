// Heading order (issue #45, axe rule heading-order): on a routed page the headings never skip a level going down (h1, then h2, then h3, never h1 then h3), so the
// page outline is a tree. The page tests render the demo fixture in the vm world; the levels are read from the tag (or aria-level when a heading carries one), the way axe
// does. Section headings that sit straight under the page's h1 are h2 (the kanban columns, Chains, the Settings and Memory section heads); a heading level only goes deeper
// under an h2 of its own. Styling is by class, so the tag does not change how a heading looks (tests/test_a11y_static.py pins the rules).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC } from './harness.mjs';
import { makeNetworkWorld, settle } from './treekit.mjs';

const DEMO = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'state.json'), 'utf8'));
const PAGE_FILES = ['home', 'inbox', 'tasks', 'project', 'agents', 'doctor', 'settings', 'search', 'session', 'onboarding', 'placeholders', 'memory'];

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

const levelOf = (h) => {
  const lv = parseInt(h.getAttribute('aria-level') || '', 10);
  return lv >= 1 ? lv : Number(/^H(\d)$/.exec(h.tagName)[1]);
};

/** The levels of the page's headings in document order, with their text, and the first jump of more than one level (null when there is none). */
function outline(w) {
  const heads = w.document.querySelector('#page').querySelectorAll('h1, h2, h3, h4, h5, h6').map((h) => ({ level: levelOf(h), text: h.textContent.trim() }));
  let prev = 0;
  for (const h of heads) {
    if (h.level > prev + 1) return { heads, jump: `${prev ? 'h' + prev : 'the start'} to h${h.level} at "${h.text}"` };
    prev = h.level;
  }
  return { heads, jump: null };
}

const ROUTES = ['#/tasks', '#/p/phasezero?tab=tasks', '#/settings', '#/settings?sec=notify', '#/settings?sec=accounts', '#/settings?sec=doctor', '#/memory'];

for (const route of ROUTES) {
  test(`${route}: no heading skips a level`, async () => {
    const w = await boardAt(route);
    const { heads, jump } = outline(w);
    assert.ok(heads.length > 0, 'the page has headings');
    assert.equal(jump, null, `heading order jumps from ${jump}: ${heads.map((h) => 'h' + h.level + ' ' + h.text).join(' | ')}`);
  });
}

test('the Memory palace names each wing with an h2 (the wing cards hang under the page h1, also on the project page)', async () => {
  const w = await boardAt('#/memory');
  w.ctx.__pal = JSON.parse(fs.readFileSync(path.join(STATIC, 'demo', 'memory_palace.json'), 'utf8'));
  const host = w.run("memPalaceRender(__pal, { project: 'phasezero', repo: '' })");
  const names = host.querySelectorAll('.mem-wing-name');
  assert.ok(names.length > 0, 'the fixture has wings');
  assert.ok(names.every((n) => n.tagName === 'H2'), 'every wing name is an h2');
  assert.equal(host.querySelectorAll('h3').length, 0);
});

test('the tasks board names its sections with h2: the columns and Chains hang under the page h1', async () => {
  const w = await boardAt('#/tasks');
  const page = w.document.querySelector('#page');
  const cols = page.querySelectorAll('.col h2');
  assert.ok(cols.length >= 3, 'a column heading per column, as an h2');
  assert.equal(page.querySelectorAll('.col h3').length, 0);
  assert.equal(page.querySelectorAll('h1')[0].textContent.startsWith('Tasks'), true);
});
