// Contract tests for the multi-line send box in app/static/components.js (composer, composerBind, composerInsertNewline,
// composerGrow, newlineButton). Harness stub nodes: no layout, no getComputedStyle, so composerGrow uses its fallbacks.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld } from './harness.mjs';

function world() { const w = makeWorld(); w.load('core.js'); w.load('components.js'); return w; }

test('composer(): a one-row textarea with the composer class and the send hints', () => {
  const w = world();
  const ta = w.get('composer')({ placeholder: 'send', onSend() {} });
  assert.equal(ta.tagName, 'TEXTAREA');
  assert.equal(ta.getAttribute('rows'), '1');
  assert.equal(ta.getAttribute('enterkeyhint'), 'send');
  assert.equal(ta.getAttribute('aria-label'), 'send');
  assert.equal(ta.getAttribute('autocapitalize'), 'off');
  assert.ok(ta.classList.contains('composer'));
});

test('Enter sends, Shift+Enter inserts a newline, ⌘/Ctrl+Enter send, IME composition and other keys are left alone', () => {
  const w = world();
  const sent = [];
  const ta = w.get('composer')({ onSend: (t) => sent.push(t) });
  let prevented = 0;
  const key = (ev) => ta.dispatch('keydown', { preventDefault() { prevented += 1; }, ...ev });
  ta.value = 'first';
  key({ key: 'Enter', shiftKey: true });
  assert.deepEqual(sent, [], 'Shift+Enter never sends');
  assert.equal(ta.value, 'first\n');
  ta.value += 'second';
  key({ key: 'Enter' });
  assert.deepEqual(sent, ['first\nsecond']);
  key({ key: 'Enter', metaKey: true });
  key({ key: 'Enter', ctrlKey: true });
  assert.equal(sent.length, 3);
  key({ key: 'Enter', isComposing: true });
  assert.equal(sent.length, 3, 'Enter during IME composition confirms the composition, it does not send');
  key({ key: 'a' });
  key({ key: 'Tab' });
  assert.equal(sent.length, 3);
  assert.equal(prevented, 4, 'every handled Enter is prevented (no stray newline, no form submit); other keys pass through');
});

test('composerBind(): wires an existing textarea (the terminal page keeps its static markup)', () => {
  const w = world();
  const sent = [];
  const ta = w.document.createElement('textarea');
  const back = w.get('composerBind')(ta, { onSend: (t) => sent.push(t), maxRows: 4 });
  assert.equal(back, ta);
  assert.ok(ta.classList.contains('composer'));
  ta.value = 'hi';
  ta.dispatch('keydown', { key: 'Enter', preventDefault() {} });
  assert.deepEqual(sent, ['hi']);
});

test('composerInsertNewline: inserts at the caret and keeps the rest of the text', () => {
  const w = world();
  const ta = w.get('composer')({});
  ta.value = 'ab'; ta.selectionStart = 1; ta.selectionEnd = 1;
  w.get('composerInsertNewline')(ta);
  assert.equal(ta.value, 'a\nb');
  const tail = w.get('composer')({});
  tail.value = 'end';                                                 // no selection API: append
  w.get('composerInsertNewline')(tail);
  assert.equal(tail.value, 'end\n');
});

test('composerGrow: follows the content, caps at maxRows and scrolls beyond it, CSSOM only', () => {
  const w = world();
  const ta = w.get('composer')({ maxRows: 3 });
  ta.value = 'some text';
  ta.scrollHeight = 40;
  w.get('composerGrow')(ta);
  assert.equal(ta.style.height, '40px');
  assert.equal(ta.style.overflowY, 'hidden');
  ta.scrollHeight = 400;
  w.get('composerGrow')(ta);
  assert.equal(ta.style.height, (20 * 3 + 14) + 'px', 'fallback metrics without layout: 20 px lines + 14 px padding');
  assert.equal(ta.style.overflowY, 'auto');
  assert.equal(ta.getAttribute('style'), null, 'never a style attribute (CSP)');
  ta.value = ''; ta.scrollHeight = 102;                                // Chrome counts a wrapped placeholder in scrollHeight
  w.get('composerGrow')(ta);
  assert.equal(ta.style.height, '', 'an empty box returns to its one-row CSS height');
  assert.equal(ta.style.overflowY, 'hidden');
  w.get('composerGrow')(null);                                         // tolerant of a missing node
});

test('newlineButton: a type=button that inserts a newline and cancels pointerdown so the soft keyboard stays up', () => {
  const w = world();
  const ta = w.get('composer')({});
  const b = w.get('newlineButton')(ta);
  assert.equal(b.tagName, 'BUTTON');
  assert.equal(b.getAttribute('type'), 'button');
  assert.equal(b.getAttribute('aria-label'), 'insert newline');
  let prevented = 0;
  b.dispatch('pointerdown', { preventDefault() { prevented += 1; } });
  assert.equal(prevented, 1);
  ta.value = 'x';
  b.dispatch('click', {});
  assert.equal(ta.value, 'x\n');
});
