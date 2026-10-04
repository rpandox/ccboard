// Contract tests for the multi-line send box in app/static/components.js (composer, composerBind, composerInsertNewline,
// composerGrow, newlineButton). Harness stub nodes: no layout, no getComputedStyle, so composerGrow uses its fallbacks.
// The second half covers the quick-reply list and its <dialog> editor (quickLoad, quickSave, quickReplyEditor, quickChip) on the
// small DOM of minidom.mjs, scans app/static for window.prompt (the editor replaced it), and covers the 10x pass (has-text, the touch placeholder, focusFine).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { STATIC, makeWorld, plain } from './harness.mjs';
import { installDom } from './minidom.mjs';

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

// ---------------------------------------------------------------- quick replies: the list, the <dialog> editor, the long-press chip

const DEFAULTS = ['continue', 'merge', 'push', 'pr', 'add commit push', 'do it'];
const KEY = 'ccboard:quick:shop--api--s1';

function domWorld(extra) {
  const w = makeWorld(extra);
  installDom(w);
  w.load('core.js');
  w.load('components.js');
  return w;
}
const editorOf = (w) => w.document.querySelector('dialog.qr-editor');
const inputsOf = (ed) => ed.querySelectorAll('input');
const values = (ed) => inputsOf(ed).map((i) => i.value);
const submit = (ed) => ed.querySelector('form').dispatchEvent({ type: 'submit', preventDefault() {} });
const keyIn = (node, key, extra) => { const e = { type: 'keydown', key, prevented: false, preventDefault() { e.prevented = true; }, ...extra }; node.dispatchEvent(e); return e; };
/** open the editor over `items` and record what onSave receives */
function open(w, items, defaults) {
  w.ctx.__saved = [];
  w.ctx.__items = items;
  w.ctx.__defaults = defaults;
  w.run('quickReplyEditor({ items: __items, defaults: __defaults, onSave: (v) => __saved.push(v) })');
  return editorOf(w);
}
const saved = (w) => plain(w.get('__saved'));

test('quickLoad / quickSave: the agent defaults without a key, a cleaned list with one; a list equal to the defaults removes the key', () => {
  const w = domWorld();
  const load = () => plain(w.run(`quickLoad('shop--api--s1')`));
  assert.deepEqual(load(), DEFAULTS);
  assert.equal(w.run('QUICK_DEFAULTS.length'), 6);
  w.localStorage.setItem(KEY, JSON.stringify(['  y ', '', 'y', 'n', 7]));
  assert.deepEqual(load(), ['y', 'n', '7'], 'trimmed, no blanks, no duplicates');
  w.localStorage.setItem(KEY, '[]');
  assert.deepEqual(load(), [], 'an emptied list stays empty: the person removed them all');
  w.localStorage.setItem(KEY, 'not json');
  assert.deepEqual(load(), DEFAULTS);
  w.localStorage.setItem(KEY, '{"a":1}');
  assert.deepEqual(load(), DEFAULTS, 'not an array');
  w.run(`quickSave('shop--api--s1', ['go', ' go ', '', 'stop'])`);
  assert.deepEqual(JSON.parse(w.localStorage.getItem(KEY)), ['go', 'stop']);
  w.run(`quickSave('shop--api--s1', ${JSON.stringify(DEFAULTS)})`);
  assert.equal(w.localStorage.getItem(KEY), null, 'the defaults need no key (so a better default list reaches everyone who never edited)');
  w.run(`quickSave('shop--api--s1', ['x'])`);
  w.run(`quickSave('shop--api--s1', null)`);
  assert.equal(w.localStorage.getItem(KEY), null, 'null resets');
  const many = Array.from({ length: 20 }, (_, i) => 'r' + i);
  assert.equal(plain(w.run(`quickSave('shop--api--s1', ${JSON.stringify(many)})`)).length, 12, 'at most 12 replies');
});

test('quickLoad / quickSave survive storage that throws (private window): defaults in, nothing stored, no exception', () => {
  const w = domWorld({ localStorage: { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() { throw new Error('denied'); } } });
  assert.deepEqual(plain(w.run(`quickLoad('x')`)), DEFAULTS);
  assert.deepEqual(plain(w.run(`quickSave('x', ['a'])`)), ['a']);
});

test('quickReplyEditor: a modal <dialog class="qr-editor"> in the body, one row per reply with an input and a remove button, Save / Cancel / Reset / + add', () => {
  const w = domWorld();
  const ed = open(w, ['yes', 'no'], DEFAULTS);
  assert.equal(ed.tagName, 'DIALOG');
  assert.ok(ed.open, 'showModal');
  assert.equal(ed.parentNode, w.document.body);
  assert.equal(ed.getAttribute('aria-label'), 'Edit quick replies');
  assert.deepEqual(values(ed), ['yes', 'no']);
  assert.equal(ed.querySelectorAll('.qr-row').length, 2);
  assert.equal(ed.querySelectorAll('.qr-rm').length, 2);
  assert.equal(ed.querySelector('.qr-rm').getAttribute('aria-label'), 'Remove this reply');
  assert.ok(ed.querySelector('.qr-add') && ed.querySelector('.qr-reset') && ed.querySelector('.qr-cancel'));
  const save = ed.querySelector('.qr-save');
  assert.equal(save.getAttribute('type'), 'submit', 'the only submit button: Enter in a row and the Save tap are the same path');
  assert.equal(ed.querySelector('.qr-cancel').getAttribute('type'), 'button');
  for (const i of inputsOf(ed)) assert.equal(i.getAttribute('type'), 'text');
  assert.equal(ed.querySelector('.qr-count').textContent, '2 of 12');
  assert.equal(ed.getAttribute('style'), null, 'never a style attribute (CSP)');
});

test('quickReplyEditor: Save hands over trimmed, non-empty, unique lines (at most 12) and the dialog goes away', () => {
  const w = domWorld();
  const ed = open(w, DEFAULTS, DEFAULTS);
  const inputs = inputsOf(ed);
  inputs[0].value = '  ship it  ';
  inputs[1].value = '   ';
  inputs[2].value = 'ship it';
  inputs[3].value = 'pr';
  submit(ed);
  assert.deepEqual(saved(w), [['ship it', 'pr', 'add commit push', 'do it']]);
  assert.equal(ed.open, false);
  assert.equal(editorOf(w), null, 'removed from the document');
  submit(ed);                                                          // a second submit (Enter right after the Save tap) must not save twice
  assert.equal(saved(w).length, 1);
  const again = open(w, Array.from({ length: 12 }, (_, i) => 'r' + i), DEFAULTS);
  assert.equal(inputsOf(again).length, 12);
  assert.equal(again.querySelector('.qr-add').disabled, true, 'twelve rows: nothing more to add');
  again.querySelector('.qr-rm').click();
  assert.equal(inputsOf(again).length, 11);
  assert.equal(again.querySelector('.qr-add').disabled, false);
});

test('quickReplyEditor: Enter in a row saves, Esc cancels without saving, a click on the backdrop and Cancel cancel too', () => {
  const w = domWorld();
  let ed = open(w, ['a', 'b'], DEFAULTS);
  const enter = keyIn(inputsOf(ed)[1], 'Enter');
  assert.ok(enter.prevented, 'no implicit form submit on top of it');
  assert.deepEqual(saved(w), [['a', 'b']]);
  assert.equal(editorOf(w), null);

  w.localStorage.setItem(KEY, JSON.stringify(['keep me']));
  w.ctx.__save = (v) => w.localStorage.setItem(KEY, JSON.stringify(v));
  w.run(`quickReplyEditor({ items: quickLoad('shop--api--s1'), defaults: QUICK_DEFAULTS, onSave: (v) => __save(v) })`);
  ed = editorOf(w);
  inputsOf(ed)[0].value = 'changed but not saved';
  const esc = keyIn(inputsOf(ed)[0], 'Escape');
  assert.ok(esc.prevented);
  assert.equal(ed.open, false);
  assert.equal(editorOf(w), null);
  assert.equal(w.localStorage.getItem(KEY), JSON.stringify(['keep me']), 'Esc leaves the stored list untouched');

  ed = open(w, ['a'], DEFAULTS);
  ed.dispatchEvent({ type: 'click', target: ed });                     // the dialog itself is the backdrop (it has no padding)
  assert.equal(editorOf(w), null);
  ed = open(w, ['a'], DEFAULTS);
  ed.querySelector('.qr-cancel').click();
  assert.equal(editorOf(w), null);
  assert.deepEqual(saved(w), [], 'none of these saved');
  ed = open(w, ['a'], DEFAULTS);
  inputsOf(ed)[0].dispatchEvent({ type: 'click' });                    // a click inside the box is not a backdrop click
  assert.ok(editorOf(w));
});

test('quickReplyEditor: remove drops a row, "+ add" appends a focused empty row, Reset puts the defaults back (saved only with Save)', () => {
  const w = domWorld();
  const ed = open(w, ['a', 'b', 'c'], DEFAULTS);
  ed.querySelectorAll('.qr-rm')[1].click();
  assert.deepEqual(values(ed), ['a', 'c']);
  ed.querySelector('.qr-add').click();
  assert.deepEqual(values(ed), ['a', 'c', '']);
  assert.equal(w.document.activeElement, inputsOf(ed)[2], 'the new row has the focus');
  assert.equal(ed.querySelector('.qr-count').textContent, '3 of 12');
  ed.querySelector('.qr-reset').click();
  assert.deepEqual(values(ed), DEFAULTS);
  assert.deepEqual(saved(w), [], 'Reset only fills the rows');
  submit(ed);
  assert.deepEqual(saved(w), [DEFAULTS]);
  // end to end the way term.js and the peek use it: saving the defaults removes the key
  w.localStorage.setItem(KEY, JSON.stringify(['x']));
  w.run(`quickReplyEditor({ items: quickLoad('shop--api--s1'), defaults: QUICK_DEFAULTS, onSave: (v) => quickSave('shop--api--s1', v) })`);
  editorOf(w).querySelector('.qr-reset').click();
  submit(editorOf(w));
  assert.equal(w.localStorage.getItem(KEY), null, 'reset + save = the agent defaults again');
  assert.deepEqual(plain(w.run(`quickLoad('shop--api--s1')`)), DEFAULTS);
});

test('quickReplyEditor: the items default to the defaults, and a second call while one is open returns it instead of stacking another', () => {
  const w = domWorld();
  w.run('quickReplyEditor({ onSave() {} })');
  const ed = editorOf(w);
  assert.deepEqual(values(ed), DEFAULTS);
  const again = w.run('quickReplyEditor({ items: ["other"], onSave() {} })');
  assert.equal(again.dialog, ed);
  assert.equal(w.document.querySelectorAll('dialog.qr-editor').length, 1);
  ed.querySelector('.qr-cancel').click();
  w.run('quickReplyEditor({ items: ["other"], onSave() {} })');
  assert.deepEqual(values(editorOf(w)), ['other'], 'once closed, the next call opens a fresh one');
});

test('quickReplyEditor limits: at most 12 rows ("+ add" stops, with the reason in its title) and 200 characters a reply; over the limit Save refuses with an inline error instead of cutting', () => {
  const w = domWorld();
  const ed = open(w, Array.from({ length: 11 }, (_, i) => 'r' + i), DEFAULTS);
  const add = ed.querySelector('.qr-add');
  assert.equal(add.disabled, false);
  add.click();
  assert.equal(inputsOf(ed).length, 12);
  assert.equal(add.disabled, true, 'the twelfth row is the last');
  assert.equal(add.getAttribute('title'), 'At most 12 replies');
  assert.equal(ed.querySelector('.qr-count').textContent, '12 of 12');
  assert.equal(inputsOf(ed)[0].getAttribute('maxlength'), null, 'no silent cut: a long line is refused out loud');
  const err = ed.querySelector('.qr-err');
  assert.ok(err.classList.contains('bad'), '--bad-fg');
  assert.equal(err.getAttribute('role'), 'alert');
  assert.equal(err.textContent, '', 'no error until Save is refused');
  inputsOf(ed)[3].value = 'x'.repeat(200);
  submit(ed);
  assert.deepEqual(saved(w).length, 1, '200 characters is allowed');
  const again = open(w, ['ok', 'fine'], DEFAULTS);
  const long = inputsOf(again)[1];
  long.value = '  ' + 'y'.repeat(201) + '  ';
  submit(again);
  assert.deepEqual(saved(w), [], 'refused: onSave was not called');
  assert.ok(again.open && editorOf(w), 'the dialog stays open');
  assert.match(again.querySelector('.qr-err').textContent, /^A reply is at most 200 characters \("yyyyyyyyyyyyyyyyyyyyyyyy…" has 201\)\.$/);
  assert.equal(long.getAttribute('aria-invalid'), 'true');
  assert.equal(inputsOf(again)[0].getAttribute('aria-invalid'), null, 'only the offending row');
  assert.equal(w.document.activeElement, long, 'and it gets the focus');
  long.value = 'short now';
  long.dispatchEvent({ type: 'input' });
  assert.equal(again.querySelector('.qr-err').textContent, '', 'typing clears the error');
  assert.equal(long.getAttribute('aria-invalid'), null);
  submit(again);
  assert.deepEqual(saved(w), [['ok', 'short now']]);
  assert.equal(editorOf(w), null);
});

const COARSE = { matchMedia: (q) => ({ matches: /pointer:\s*coarse/.test(q), addEventListener() {}, removeEventListener() {} }) };

test('quickReplyEditor: on a coarse pointer no input is focused: the dialog itself is (tabindex -1), so no soft keyboard pops up under it', () => {
  const fine = domWorld();
  const a = open(fine, ['a'], DEFAULTS);
  assert.equal(fine.document.activeElement, inputsOf(a)[0], 'a mouse or keyboard: ready to type');
  assert.equal(a.getAttribute('tabindex'), null, 'no tabindex on a desktop dialog');
  const touch = domWorld(COARSE);
  const b = open(touch, ['a'], DEFAULTS);
  assert.notEqual(touch.document.activeElement, inputsOf(b)[0]);
  assert.equal(touch.document.activeElement, b, 'the focus sits on the dialog (a browser would have handed it to the first input)');
  assert.equal(b.getAttribute('tabindex'), '-1');
  assert.equal(b.getAttribute('autofocus'), '', 'where showModal honours autofocus on the dialog, the first input never gets the focus at all');
  assert.ok(b.open);
  // html.force-coarse (the QA script's way of forcing a phone) counts too
  const forced = domWorld();
  forced.document.documentElement.classList.add('force-coarse');
  const c = open(forced, ['a'], DEFAULTS);
  assert.equal(forced.document.activeElement, c);
  // and "+ add" still focuses the new row: that one is a tap on purpose
  c.querySelector('.qr-add').click();
  assert.equal(touch.document.activeElement, b);
  assert.equal(forced.document.activeElement, inputsOf(c)[1]);
});

// ---- a soft keyboard shrinks the visual viewport: the dialog follows it (--vvh / --vvt on the dialog) instead of staying centred in the layout viewport

function fakeVV(height, offsetTop = 0) {
  const on = {};
  return {
    height, offsetTop, on,
    addEventListener(t, f) { (on[t] ||= []).push(f); },
    removeEventListener(t, f) { on[t] = (on[t] || []).filter((x) => x !== f); },
    fire(t) { for (const f of [...(on[t] || [])]) f({ type: t }); },
  };
}
/** a dom world whose nodes have a recording style.setProperty (the real CSSOM has one; minidom's style is a bare object) */
function vvWorld(extra) {
  const w = makeWorld(extra);
  installDom(w);
  const create = w.document.createElement;
  w.document.createElement = (t) => { const n = create(t); n.style = { props: {}, setProperty(k, v) { this.props[k] = v; } }; return n; };
  w.load('core.js');
  w.load('components.js');
  return w;
}

test('modalShell: the dialog gets --vvh and --vvt from the visual viewport when it opens (the board pages have none of their own)', () => {
  const vv = fakeVV(520, 0);
  const w = vvWorld({ visualViewport: vv });
  const ed = open(w, ['a', 'b'], DEFAULTS);
  assert.deepEqual(ed.style.props, { '--vvh': '520px', '--vvt': '0px' });
  assert.equal(ed.getAttribute('style'), null, 'CSSOM custom properties only, never a style attribute (CSP)');
});

test('modalShell: the properties follow the visual viewport while the dialog is open (the keyboard comes up AFTER it opened) and stop when it closes', () => {
  const vv = fakeVV(844, 0);
  const w = vvWorld({ visualViewport: vv });
  const ed = open(w, ['a'], DEFAULTS);
  assert.equal(ed.style.props['--vvh'], '844px');
  vv.height = 519.6;                                                   // the soft keyboard: the visual viewport shrinks, the layout viewport does not
  vv.fire('resize');
  assert.deepEqual(ed.style.props, { '--vvh': '519px', '--vvt': '0px' });
  vv.offsetTop = 37.4;                                                 // iOS pans the visual viewport inside the layout one
  vv.fire('scroll');
  assert.deepEqual(ed.style.props, { '--vvh': '519px', '--vvt': '37px' });
  vv.offsetTop = -3;                                                   // never a negative offset
  vv.fire('scroll');
  assert.equal(ed.style.props['--vvt'], '0px');
  assert.equal(vv.on.resize.length, 1);
  assert.equal(vv.on.scroll.length, 1);
  ed.querySelector('.qr-cancel').click();
  assert.equal(vv.on.resize.length, 0, 'no listener is left behind');
  assert.equal(vv.on.scroll.length, 0);
  const frozen = JSON.stringify(ed.style.props);
  vv.height = 300;
  vv.fire('resize');
  assert.equal(JSON.stringify(ed.style.props), frozen, 'a closed dialog is not touched');
  const again = open(w, ['a'], DEFAULTS);                              // the next one starts from the current viewport
  assert.equal(again.style.props['--vvh'], '300px');
  assert.equal(vv.on.resize.length, 1, 'one listener per open dialog');
});

test('modalShell without visualViewport falls back to the window height; without a CSSOM setProperty it never throws', () => {
  const w = vvWorld({ innerHeight: 700 });
  const ed = open(w, ['a'], DEFAULTS);
  assert.deepEqual(ed.style.props, { '--vvh': '700px', '--vvt': '0px' });
  const bare = domWorld({ visualViewport: fakeVV(500) });              // minidom's style has no setProperty
  assert.doesNotThrow(() => open(bare, ['a'], DEFAULTS));
  const none = domWorld();
  assert.doesNotThrow(() => open(none, ['a'], DEFAULTS));
});

test('style.css anchors dialog.qr-editor to the top of the visual viewport (position fixed, top from --vvt, height from --vvh), not the centre of the layout viewport', () => {
  const css = fs.readFileSync(path.join(STATIC, 'style.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rule = /(?:^|\n)\s*dialog\.qr-editor\s*\{([^}]*)\}/.exec(css);
  assert.ok(rule, 'the dialog.qr-editor rule');
  const body = rule[1].replace(/\s+/g, ' ');
  assert.match(body, /position:\s*fixed/);
  assert.match(body, /inset:\s*calc\(var\(--vvt,\s*0px\)[^;]*\+ 12px\)\s+0\s+auto\s+0/, 'top = the visual viewport offset + 12 px, left and right 0, bottom auto');
  assert.match(body, /margin:\s*0 auto/, 'centred across, never down');
  assert.match(body, /max-height:\s*calc\(var\(--vvh,\s*100dvh\)\s*-\s*24px/, 'the height comes from the visual viewport');
});

function fakeTimers() {
  const q = [];
  return { setTimeout: (fn, ms) => { q.push({ fn, ms }); return q.length; }, clearTimeout: (id) => { if (q[id - 1]) q[id - 1].fn = null; },
    fire() { for (const t of q.splice(0)) if (t.fn) t.fn(); }, pending: () => q.filter((t) => t.fn).length, q };
}

test('quickChip: a tap sends, a 500 ms hold or a right click edits (and the lift that ends the hold does not also send), moving away cancels the hold', () => {
  const t = fakeTimers();
  const w = domWorld({ setTimeout: t.setTimeout, clearTimeout: t.clearTimeout });
  w.ctx.__log = [];
  w.run(`globalThis.__chip = quickChip('continue', { cls: 'chip-btn', onSend: (b) => __log.push('send:' + b.textContent), onEdit: () => __log.push('edit') })`);
  const chip = w.get('__chip');
  const log = () => plain(w.get('__log'));
  assert.equal(chip.textContent, 'continue');
  assert.equal(chip.getAttribute('type'), 'button');
  assert.ok(chip.classList.contains('chip-btn'));
  assert.match(chip.getAttribute('title'), /hold to edit/);
  // a tap
  chip.dispatchEvent({ type: 'pointerdown', pointerType: 'touch' });
  assert.equal(t.pending(), 1);
  assert.equal(t.q[0].ms, 500);
  chip.dispatchEvent({ type: 'pointerup', pointerType: 'touch' });
  assert.equal(t.pending(), 0, 'lifting before 500 ms cancels the hold');
  chip.dispatchEvent({ type: 'click', detail: 1, preventDefault() {} });
  assert.deepEqual(log(), ['send:continue']);
  // a hold: the editor opens; the click that follows the lift is swallowed
  chip.dispatchEvent({ type: 'pointerdown', pointerType: 'touch' });
  t.fire();
  assert.deepEqual(log(), ['send:continue', 'edit']);
  let prevented = 0;
  chip.dispatchEvent({ type: 'pointerup', pointerType: 'touch' });
  chip.dispatchEvent({ type: 'click', detail: 1, preventDefault() { prevented += 1; } });
  assert.deepEqual(log(), ['send:continue', 'edit'], 'no send after a hold');
  assert.equal(prevented, 1);
  chip.dispatchEvent({ type: 'click', detail: 1, preventDefault() {} });
  assert.deepEqual(log(), ['send:continue', 'edit', 'send:continue'], 'the next tap is an ordinary tap again');
  // a swipe along the row (pointercancel) or leaving the chip is not a hold
  chip.dispatchEvent({ type: 'pointerdown', pointerType: 'touch' });
  chip.dispatchEvent({ type: 'pointercancel', pointerType: 'touch' });
  t.fire();
  assert.equal(log().length, 3);
  // right click (and the context menu a touch long press becomes): edit, with the browser menu suppressed
  let menu = 0;
  chip.dispatchEvent({ type: 'contextmenu', preventDefault() { menu += 1; } });
  assert.deepEqual(log().slice(-1), ['edit']);
  assert.equal(menu, 1);
  // a right-button press never starts the hold timer
  chip.dispatchEvent({ type: 'pointerdown', pointerType: 'mouse', button: 2 });
  assert.equal(t.pending(), 0);
  // a keyboard activation (click with detail 0) always sends, even right after a hold
  chip.dispatchEvent({ type: 'click', detail: 0, preventDefault() {} });
  assert.deepEqual(log().slice(-1), ['send:continue']);
});

// ---------------------------------------------------------------- no window.prompt anywhere in the app

test('no script under app/static calls window.prompt (the quick-reply editor and the task preview port sheet are <dialog>s); vendor code is not ours', () => {
  const files = [];
  const walk = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      if (e.isDirectory()) { if (e.name !== 'vendor') walk(path.join(dir, e.name)); } else if (/\.(js|html)$/.test(e.name)) files.push(path.join(dir, e.name));
    }
  };
  walk(STATIC);
  assert.ok(files.length > 20 && files.some((f) => f.endsWith('term.js')) && files.some((f) => f.endsWith('components.js')), 'the scan found the app');
  // window.prompt( / globalThis.prompt( / self.prompt(, window['prompt'](, or a bare prompt( that starts a statement or an expression. ev.prompt() (the PWA install
  // prompt, shell.js) has a dot in front and is fine; prose such as "the system prompt (optional)" has a word in front and is fine.
  const call = /\b(?:window|globalThis|self)\s*\.\s*prompt\s*\(|\[\s*['"]prompt['"]\s*\]\s*\(|(?:^|[;{}(,=!&|?:])\s*(?:await\s+|return\s+)?prompt\s*\(/;
  const bad = [];
  for (const f of files) fs.readFileSync(f, 'utf8').split('\n').forEach((line, i) => { if (call.test(line)) bad.push(`${path.relative(STATIC, f)}:${i + 1}: ${line.trim().slice(0, 100)}`); });
  assert.deepEqual(bad, [], 'use quickReplyEditor() or a sheet (components.js), never window.prompt');
  for (const yes of ["const p = window.prompt('x');", 'prompt("y")', 'const v = await prompt("z")', "x = globalThis['prompt']('q')", 'if (a) { prompt(1) }']) assert.ok(call.test(yes), yes);
  for (const no of ['await ev.prompt();', "const sysPrompt = el('textarea', { placeholder: 'the system prompt (optional)' });", '/* its prompt) and a box */', 'POST /prompt (queue)']) assert.ok(!call.test(no), no);
});

// ---------------------------------------------------------------- the 10x pass: has-text, the touch placeholder, focusFine

const coarseWorld = () => { const w = makeWorld({ matchMedia: (q) => ({ matches: /pointer:\s*coarse/.test(q), addEventListener() {}, removeEventListener() {} }) }); w.load('core.js'); w.load('components.js'); return w; };
const inRow = (w, ta) => { const row = w.document.createElement('form'); row.append(ta); ta.parentNode = row; return row; };

test('has-text: the row of a bound composer says whether the box holds a draft (the Send button is tinted when empty, filled with text)', () => {
  const w = world();
  const ta = w.get('composer')({ onSend() {} });
  const row = inRow(w, ta);
  assert.ok(!row.classList.contains('has-text'), 'a new box is empty');
  ta.value = 'draft';
  ta.dispatch('input', {});
  assert.ok(row.classList.contains('has-text'), 'typing marks the row');
  ta.value = '   ';
  w.get('composerGrow')(ta);
  assert.ok(!row.classList.contains('has-text'), 'whitespace is no draft');
  ta.value = 'again';
  w.get('composerGrow')(ta);
  assert.ok(row.classList.contains('has-text'));
  ta.value = '';                                                       // what sessionSend / peekSend / the terminal do after a send: clear, then composerGrow
  w.get('composerGrow')(ta);
  assert.ok(!row.classList.contains('has-text'), 'a send that clears the box clears the mark with it');
});

test('has-text is only for composers that were bound (the task prompt box keeps its field untouched) and never needs a parent', () => {
  const w = world();
  const plain = w.document.createElement('textarea');                  // a textarea that only borrows composerGrow
  const row = inRow(w, plain);
  plain.value = 'text';
  w.get('composerGrow')(plain);
  assert.ok(!row.classList.contains('has-text'));
  const orphan = w.get('composer')({});                                // no parent yet: composerBind runs before the row exists
  orphan.value = 'x';
  assert.doesNotThrow(() => w.get('composerGrow')(orphan));
});

test('on a touch device the placeholder is the bare verb (one short line, no cut-off second line) and the full text moves to the title', () => {
  const touch = coarseWorld().get('composer')({ placeholder: 'reply to t-checkout-redesign · ⇧Enter new line', label: 'reply to t-checkout-redesign' });
  assert.equal(touch.getAttribute('placeholder'), 'Reply…', 'a fixed short word: a long session name cannot wrap it');
  assert.equal(touch.getAttribute('title'), 'reply to t-checkout-redesign · ⇧Enter new line');
  assert.equal(touch.getAttribute('aria-label'), 'reply to t-checkout-redesign', 'the accessible name is unchanged');
  const desk = world().get('composer')({ placeholder: 'reply to s1 · ⇧Enter new line' });
  assert.equal(desk.getAttribute('placeholder'), 'reply to s1 · ⇧Enter new line', 'a fine pointer keeps the full hint');
  assert.equal(desk.getAttribute('title'), null);
  const plainHint = coarseWorld().get('composer')({ placeholder: 'send' });
  assert.equal(plainHint.getAttribute('placeholder'), 'send', 'a placeholder without the hint is left as it is');
  assert.equal(plainHint.getAttribute('title'), null);
  const css = fs.readFileSync(path.join(STATIC, 'style.css'), 'utf8');
  const ph = /textarea\.composer::placeholder\s*\{([^}]*)\}/.exec(css);
  assert.ok(ph, 'a ::placeholder rule for the composer');
  assert.match(ph[1], /white-space:\s*nowrap/);
  assert.match(ph[1], /text-overflow:\s*ellipsis/);
});

test('focusFine focuses on a fine pointer only: never on touch, never under html.force-coarse, tolerant of a missing node', () => {
  const calls = [];
  const node = () => ({ focus() { calls.push('focus'); } });
  const fine = world();
  assert.equal(fine.get('focusFine')(node()), true);
  assert.deepEqual(calls, ['focus']);
  assert.equal(fine.get('focusFine')(null), false);
  assert.equal(fine.get('focusFine')({}), false, 'a node without focus()');
  calls.length = 0;
  assert.equal(coarseWorld().get('focusFine')(node()), false, 'a phone keeps its soft keyboard down');
  assert.deepEqual(calls, []);
  const forced = makeWorld();
  forced.document.documentElement = { classList: { contains: (c) => c === 'force-coarse' } };
  forced.load('core.js'); forced.load('components.js');
  assert.equal(forced.get('focusFine')(node()), false, 'html.force-coarse is the QA switch for the phone layout');
  const broken = makeWorld({ matchMedia: () => { throw new Error('no matchMedia'); } });
  broken.load('core.js'); broken.load('components.js');
  assert.equal(broken.get('focusFine')(node()), true, 'an environment that cannot tell is treated like a desktop');
});
