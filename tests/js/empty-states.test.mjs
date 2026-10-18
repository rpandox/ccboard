// Issue #307 (rule 3, no dead ends): an empty state that names an action has the button for it, and the button runs what the + menu entry runs (Shell.openCreate).
// Agents and Needs you on the real scripts through world.mjs's homeWorld (Shell.openCreate is a recorder: __created); the Memory page is in memory.test.mjs and the project page in project.test.mjs.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fakeState, fixtureState, homeWorld, page, plain, projectsOf, sess, setState, text } from './world.mjs';

const shownNode = (n) => { for (let x = n; x && x.nodeType === 1; x = x.parentNode) if (x.classList.contains('hidden')) return false; return true; };
const created = (w) => plain(w.get('__created'));
const emptyOf = (w, re) => page(w).querySelectorAll('.nonideal').find((n) => re.test(text(n)));
const buttons = (n) => n.querySelectorAll('button').filter(shownNode);

const noSessions = () => fakeState({ projects: projectsOf({ shop: { api: [] } }) });
const noProjects = () => fakeState({ projects: [] });
const oneSession = () => fakeState({ projects: projectsOf({ shop: { api: [sess('shop', 'api', 's1')] } }) });

test('pageEmpty takes an optional list of action nodes and puts them under the hint; without one it draws what it drew before', () => {
  const { w } = homeWorld();
  const plainNode = w.run("pageEmpty('console', 'Title', 'Hint')");
  assert.equal(plainNode.querySelector('.empty-actions'), null);
  w.ctx.__a = [w.run("el('button', { type: 'button', text: 'Go' })")];
  const withActions = w.run("pageEmpty('console', 'Title', 'Hint', __a)");
  assert.deepEqual(withActions.querySelector('.empty-actions').querySelectorAll('button').map(text), ['Go']);
  assert.equal(w.run("pageEmpty('console', 'Title', 'Hint', [])").querySelector('.empty-actions'), null);
});

test('Agents with no live session: a New session button that runs Shell.openCreate("session"), and a hint that no longer points at the + menu', () => {
  const { w } = homeWorld({ state: noSessions() });
  w.location.hash = '#/agents';
  const none = emptyOf(w, /No live sessions/);
  assert.ok(none && shownNode(none));
  assert.doesNotMatch(text(none), /\+ menu/);
  assert.match(text(none), /Every Claude, Codex or shell session on this box shows up here\./);
  const b = buttons(none);
  assert.deepEqual(b.map(text), ['New session']);
  assert.ok(b[0].classList.contains('primary'));
  b[0].click();
  assert.deepEqual(created(w), ['session']);
  setState(w, oneSession());
  assert.equal(shownNode(none), false, 'a live session makes the empty state go away');
});

test('Needs you on a board with projects but no session: New session; with no project at all: New session and New project; neither is a dead end', () => {
  const { w } = homeWorld({ state: noSessions() });
  w.location.hash = '#/inbox';
  const none = emptyOf(w, /Nothing needs you/);
  assert.ok(none && shownNode(none));
  assert.deepEqual(buttons(none).map(text), ['New session']);
  assert.equal(none.querySelectorAll('a').filter(shownNode).length, 0, 'no link to an Agents page with nothing on it');
  buttons(none)[0].click();
  assert.deepEqual(created(w), ['session']);
  setState(w, noProjects());
  assert.deepEqual(buttons(none).map(text), ['New session', 'New project']);
  buttons(none)[1].click();
  assert.deepEqual(created(w), ['session', 'project']);
});

test('Needs you with sessions running: a See all agents link to #/agents and no create button', () => {
  const { w } = homeWorld({ state: oneSession() });
  w.location.hash = '#/inbox';
  const none = emptyOf(w, /Nothing needs you/);
  assert.ok(none && shownNode(none));
  assert.deepEqual(buttons(none), []);
  const links = none.querySelectorAll('a').filter(shownNode);
  assert.deepEqual(links.map((a) => [text(a), a.getAttribute('href')]), [['See all agents', '#/agents']]);
  setState(w, noSessions());
  assert.deepEqual(buttons(none).map(text), ['New session']);
  assert.equal(none.querySelectorAll('a').filter(shownNode).length, 0);
});

test('Needs you with items waiting keeps the empty state hidden, buttons and all', () => {
  const { w } = homeWorld({ state: fixtureState() });
  w.location.hash = '#/inbox';
  assert.equal(shownNode(emptyOf(w, /Nothing needs you/)), false);
});
