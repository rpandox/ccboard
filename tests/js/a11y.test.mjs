// Accessibility contract tests (issue #45): the roles, states and keys of the shared components that every page uses, so a page gets them for free.
// tabs(): tablist / tab / tabpanel with aria-selected, a roving tabindex, arrows + Home + End + Enter/Space, ids and aria-controls only where a panel exists.
// menu(): role=menu / menuitem, opened by a pointer it highlights nothing, opened by the keyboard it focuses the first item, arrows / Home / End move, Escape returns to the opener.
// The task card and its lane are named groups; the glyphs name themselves; smooth scrolling asks prefers-reduced-motion. A real axe-core run and a screen reader are NOT part of this
// file (scripts/dev/axe-run.sh drives axe in the gstack browser).
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld } from './harness.mjs';
import { installDom } from './minidom.mjs';

function aWorld(extra = {}) {
  const w = makeWorld(extra);
  installDom(w);
  for (const f of ['core.js', 'components.js']) w.load(f);
  return w;
}
const key = (k, mods = {}) => { const e = { type: 'keydown', key: k, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, isComposing: false, defaultPrevented: false, preventDefault() { e.defaultPrevented = true; }, stopPropagation() {}, ...mods }; return e; };
const text = (n) => (n ? n.textContent : '');
const ITEMS = [{ id: 'a', label: 'Alpha' }, { id: 'b', label: 'Beta', count: 3 }, { id: 'c', label: 'Gamma' }];

test('tabs(): a tablist of tabs, one selected, the others out of the tab order; a click or an arrow key selects and reports', () => {
  const w = aWorld();
  const seen = [];
  const t = w.get('tabs')(ITEMS, 'b', (id) => seen.push(id));
  const list = t.root.children[0];
  assert.equal(list.getAttribute('role'), 'tablist');
  const tabs = list.children;
  assert.deepEqual(tabs.map((n) => n.getAttribute('role')), ['tab', 'tab', 'tab']);
  assert.deepEqual(tabs.map((n) => n.getAttribute('aria-selected')), ['false', 'true', 'false']);
  assert.deepEqual(tabs.map((n) => n.getAttribute('tabindex')), ['-1', '0', '-1'], 'a roving tabindex: Tab enters the list once');
  assert.ok(tabs.every((n) => n.getAttribute('id')), 'every tab has an id (panels point back at it)');
  assert.equal(new Set(tabs.map((n) => n.getAttribute('id'))).size, 3);
  tabs[1].dispatchEvent(key('ArrowRight'));
  assert.deepEqual(seen, ['c']);
  assert.equal(t.value, 'c');
  assert.deepEqual(tabs.map((n) => n.getAttribute('aria-selected')), ['false', 'false', 'true']);
  tabs[2].dispatchEvent(key('ArrowRight'));
  assert.equal(t.value, 'a', 'the arrows wrap');
  tabs[0].dispatchEvent(key('ArrowLeft'));
  assert.equal(t.value, 'c');
  tabs[2].dispatchEvent(key('Home'));
  assert.equal(t.value, 'a');
  tabs[0].dispatchEvent(key('End'));
  assert.equal(t.value, 'c');
  const e = key('x');
  tabs[2].dispatchEvent(e);
  assert.equal(e.defaultPrevented, false, 'other keys are left alone');
  tabs[0].click();
  assert.equal(t.value, 'a');
  assert.deepEqual(seen.slice(-1), ['a']);
});

test('tabs(): set() repaints without calling onChange (a route change cannot loop)', () => {
  const w = aWorld();
  const seen = [];
  const t = w.get('tabs')(ITEMS, 'a', (id) => seen.push(id));
  t.set('c');
  assert.deepEqual(seen, []);
  assert.equal(t.root.children[0].children[2].getAttribute('aria-selected'), 'true');
});

test('tabs().link(): every tab points at its panel with aria-controls and every panel at its tab with aria-labelledby; nothing points at a panel that does not exist', () => {
  const w = aWorld();
  const t = w.get('tabs')(ITEMS, 'a', () => {});
  const tabs = t.root.children[0].children;
  assert.ok(tabs.every((n) => !n.hasAttribute('aria-controls')), 'before link() nothing is claimed');
  const pa = w.document.createElement('div');
  const pc = w.document.createElement('div');
  t.link({ a: pa, c: pc });                                      // no panel for b: no aria-controls on b
  assert.equal(tabs[0].getAttribute('aria-controls'), pa.getAttribute('id'));
  assert.equal(pa.getAttribute('aria-labelledby'), tabs[0].getAttribute('id'));
  assert.equal(pa.getAttribute('role'), 'tabpanel');
  assert.equal(tabs[2].getAttribute('aria-controls'), pc.getAttribute('id'));
  assert.ok(!tabs[1].hasAttribute('aria-controls'));
  assert.notEqual(pa.getAttribute('id'), pc.getAttribute('id'));
});

test('tabs().link(node): ONE panel whose aria-labelledby follows the selected tab', () => {
  const w = aWorld();
  const t = w.get('tabs')(ITEMS, 'b', () => {});
  const tabs = t.root.children[0].children;
  const panel = w.document.createElement('div');
  t.link(panel);
  assert.equal(panel.getAttribute('role'), 'tabpanel');
  assert.ok(tabs.every((n) => n.getAttribute('aria-controls') === panel.getAttribute('id')));
  assert.equal(panel.getAttribute('aria-labelledby'), tabs[1].getAttribute('id'));
  t.set('c');
  assert.equal(panel.getAttribute('aria-labelledby'), tabs[2].getAttribute('id'));
  tabs[0].click();
  assert.equal(panel.getAttribute('aria-labelledby'), tabs[0].getAttribute('id'));
});

test('two tab lists on one page never share an id', () => {
  const w = aWorld();
  const a = w.get('tabs')(ITEMS, 'a', () => {});
  const b = w.get('tabs')(ITEMS, 'a', () => {});
  const ids = [...a.root.children[0].children, ...b.root.children[0].children].map((n) => n.getAttribute('id'));
  assert.equal(new Set(ids).size, 6);
});

function menuWorld() {
  const w = aWorld();
  w.ctx.__picked = [];
  const btn = w.document.createElement('button');
  btn.getBoundingClientRect = () => ({ left: 10, right: 50, top: 10, bottom: 38, width: 40, height: 28 });
  w.document.body.append(btn);
  const m = w.get('menu')(btn, [{ label: 'One', onClick: () => w.ctx.__picked.push(1) }, { label: 'Two', onClick: () => w.ctx.__picked.push(2) }, { label: 'Three', onClick: () => w.ctx.__picked.push(3) }]);
  return { w, btn, m };
}

test('menu(): the button announces a menu; the popover is role=menu with menuitems', () => {
  const { btn, m } = menuWorld();
  assert.equal(btn.getAttribute('aria-haspopup'), 'menu');
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  m.open(false);
  assert.equal(btn.getAttribute('aria-expanded'), 'true');
  assert.equal(m.root.getAttribute('role'), 'menu');
  assert.deepEqual(m.root.children.map((n) => n.getAttribute('role')), ['menuitem', 'menuitem', 'menuitem']);
  assert.ok(m.root.children.every((n) => n.getAttribute('tabindex') === '-1'), 'items are reached by the arrows, not by Tab');
});

test('menu(): a pointer-opened menu highlights nothing (the popover holds the focus); a keyboard-opened one focuses the first item', () => {
  const a = menuWorld();
  a.m.open(false);
  assert.equal(a.w.document.activeElement, a.m.root);
  const b = menuWorld();
  b.m.open(true);
  assert.equal(b.w.document.activeElement, b.m.root.children[0]);
});

test('menu(): arrows, Home and End move between items (wrapping), Enter picks and closes, Escape closes and returns the focus to the button', () => {
  const { w, btn, m } = menuWorld();
  m.open(true);
  const rows = m.root.children;
  w.document.dispatch('keydown', key('ArrowDown'));
  assert.equal(w.document.activeElement, rows[1]);
  w.document.dispatch('keydown', key('End'));
  assert.equal(w.document.activeElement, rows[2]);
  w.document.dispatch('keydown', key('ArrowDown'));
  assert.equal(w.document.activeElement, rows[0], 'wraps');
  w.document.dispatch('keydown', key('ArrowUp'));
  assert.equal(w.document.activeElement, rows[2]);
  w.document.dispatch('keydown', key('Home'));
  assert.equal(w.document.activeElement, rows[0]);
  rows[1].dispatchEvent(key('Enter'));
  assert.deepEqual(w.ctx.__picked, [2]);
  assert.equal(m.root, null, 'closed after a pick');
  m.open(true);
  w.document.dispatch('keydown', key('Escape'));
  assert.equal(m.root, null);
  assert.equal(btn.getAttribute('aria-expanded'), 'false');
  assert.equal(w.document.activeElement, btn, 'Escape returns the focus to the opener');
});

test('the task card is a named group in the tab order and its lane is a named group; the Backlog card announces its m key', () => {
  const w = aWorld();
  w.run('state = { tasks: [], projects: [], agents: {} }');
  const t = { id: 5, title: 'Fix the login redirect', prompt: 'p', project: 'shop', repo: 'api', agent: 'claude', phase: 'backlog', column: 'backlog', slug: '', branch: '', worktree: '', tmux: '' };
  const card = w.get('taskCardShell')(t, '', true);
  assert.equal(card.getAttribute('role'), 'group');
  assert.equal(card.getAttribute('aria-label'), 'Task: Fix the login redirect');
  assert.equal(card.getAttribute('tabindex'), '0');
  assert.equal(card.getAttribute('aria-keyshortcuts'), 'm');
  const plain = w.get('taskCardShell')({ ...t, id: 6 }, '', false);
  assert.ok(!plain.hasAttribute('aria-keyshortcuts'), 'only a movable card claims the key');
});

test('state and agent glyphs name themselves; state is never colour-only', () => {
  const w = aWorld();
  const labels = { working: 'working', waiting: 'needs you', idle: 'idle', done: 'done', errored: 'error', ended: 'ended' };
  for (const [state, label] of Object.entries(labels)) {
    const g = w.get('stateGlyph')(state);
    assert.equal(g.getAttribute('role'), 'img');
    assert.equal(g.getAttribute('aria-label'), label);
  }
  for (const a of ['claude', 'codex', 'shell']) assert.equal(w.get('agentGlyph')(a).getAttribute('aria-label'), a);
});

test('scrollBehavior(): smooth, unless the person asked for reduced motion (then an instant jump)', () => {
  const w = aWorld({ matchMedia: (q) => ({ matches: false, addEventListener() {}, removeEventListener() {} }) });
  assert.equal(w.run('scrollBehavior()'), 'smooth');
  const r = aWorld({ matchMedia: (q) => ({ matches: /prefers-reduced-motion:\s*reduce/.test(q), addEventListener() {}, removeEventListener() {} }) });
  assert.equal(r.run('scrollBehavior()'), 'auto');
  const n = aWorld({ matchMedia: () => { throw new Error('no media'); } });
  assert.equal(n.run('scrollBehavior()'), 'auto', 'unknown: do not animate');
});
