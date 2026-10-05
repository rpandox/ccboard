// Contract tests for app/static/keymap.js (classic script): specs, mod detection per platform, chords, the inert rules (inputs, dialogs, iframes),
// the help list and the app's default bindings. Runs in the vm harness; the page objects the defaults call (Pages, Palette, Shell) are stubs here.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { makeWorld, plain } from './harness.mjs';

const MAC = 'MacIntel';
const LINUX = 'Linux x86_64';

/** A world with keymap.js loaded. `dialogs` is what document.querySelectorAll('dialog[open]') returns. */
function kw({ platform = LINUX, dialogs = [], active = null } = {}) {
  const w = makeWorld({ navigator: { platform, userAgent: 'node-test' } });
  w.document.querySelectorAll = (sel) => (sel === 'dialog[open]' ? dialogs : []);
  Object.defineProperty(w.document, 'activeElement', { get: () => active, configurable: true });
  w.load('keymap.js');
  w.run('globalThis.__t = 1000; Keymap.now = () => __t;');
  return w;
}

const at = (w, ms) => w.run(`__t = ${ms}`);

/** A keydown event; `prevented` says whether a binding handled it. */
function ev(key, mods = {}) {
  const e = { key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, target: null, defaultPrevented: false, ...mods, prevented: false };
  e.preventDefault = () => { e.prevented = true; };
  return e;
}

/** Bind `spec` to a recorder that returns `ret` (undefined = handled); returns a reader for the shared log. */
function rec(w, spec, opts, ret) {
  w.ctx.__log = w.ctx.__log || [];
  w.run(`Keymap.bindKey(${JSON.stringify(spec)}, () => { __log.push(${JSON.stringify(spec)}); return ${ret === false ? 'false' : 'undefined'}; }, ${JSON.stringify(opts || {})})`);
  return () => plain(w.get('__log'));
}

const input = { tagName: 'INPUT' };

// ---------------------------------------------------------------- load and specs

test('keymap.js defines only: no DOM, storage, listener, timer or navigator access at load', () => {
  const trap = (what) => new Proxy(function () {}, {
    get(_t, prop) { if (prop === Symbol.toPrimitive || prop === 'then') return undefined; throw new Error(`load-time access to ${what}.${String(prop)}`); },
    apply() { throw new Error(`load-time call of ${what}()`); },
  });
  const boom = (what) => () => { throw new Error(`load-time call of ${what}()`); };
  const w = makeWorld({ document: trap('document'), localStorage: trap('localStorage'), sessionStorage: trap('sessionStorage'), navigator: trap('navigator'),
    location: trap('location'), history: trap('history'), matchMedia: boom('matchMedia'), addEventListener: boom('window.addEventListener'),
    setTimeout: boom('setTimeout'), setInterval: boom('setInterval'), fetch: boom('fetch') });
  w.load('keymap.js');
  assert.equal(w.run('Keymap.list.length'), 0, 'no binding is registered until install()');
  assert.equal(w.run('Keymap.installed'), false);
});

test('parse: keys, modifiers, aliases and chords; bad specs throw', () => {
  const w = kw();
  const parse = (s) => plain(w.get('Keymap').parse(s));
  assert.deepEqual(parse('j'), [{ mod: false, shift: false, alt: false, key: 'j' }]);
  assert.deepEqual(parse('mod+k'), [{ mod: true, shift: false, alt: false, key: 'k' }]);
  assert.deepEqual(parse('mod+\\'), [{ mod: true, shift: false, alt: false, key: '\\' }]);
  assert.deepEqual(parse('mod+Enter').map((s) => s.key), ['enter']);
  assert.deepEqual(parse('Esc').map((s) => s.key), ['escape']);
  assert.deepEqual(parse('g h').map((s) => s.key), ['g', 'h']);
  assert.deepEqual(parse('?').map((s) => s.key), ['?']);
  assert.deepEqual(parse('mod+shift+p')[0], { mod: true, shift: true, alt: false, key: 'p' });
  for (const bad of ['', '   ', 'mod+', 'hyper+k', 'a b c d']) assert.throws(() => w.get('Keymap').parse(bad), /bad key spec/, JSON.stringify(bad));
  assert.throws(() => w.run("Keymap.bindKey('j', null)"), /bad key handler/);
});

// ---------------------------------------------------------------- mod and shift

test('mod is Command on macOS and iPadOS, Ctrl elsewhere, and the other one does not count', () => {
  for (const [platform, good, bad] of [[MAC, { metaKey: true }, { ctrlKey: true }], [LINUX, { ctrlKey: true }, { metaKey: true }], ['Win32', { ctrlKey: true }, { metaKey: true }]]) {
    const w = kw({ platform });
    const log = rec(w, 'mod+k');
    const K = w.get('Keymap');
    assert.equal(!!K.handle(ev('k', bad)), false, `${platform}: the wrong modifier`);
    assert.equal(!!K.handle(ev('k')), false, `${platform}: no modifier`);
    assert.ok(K.handle(ev('k', good)), `${platform}: the right modifier`);
    assert.equal(!!K.handle(ev('k', { ...good, shiftKey: true })), false, `${platform}: shift makes it another chord`);
    assert.equal(!!K.handle(ev('k', { ...good, altKey: true })), false, `${platform}: alt too`);
    assert.deepEqual(log(), ['mod+k']);
  }
});

test('an iPad reports MacIntel: the Smart Keyboard Command key is mod', () => {
  const w = makeWorld({ navigator: { platform: 'MacIntel', maxTouchPoints: 5, userAgent: 'iPad' } });
  w.load('keymap.js');
  assert.equal(w.run('Keymap.isMac()'), true);
  const w2 = makeWorld({ navigator: { platform: '', userAgent: 'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)' } });
  w2.load('keymap.js');
  assert.equal(w2.run('Keymap.isMac()'), true, 'with a blank platform the user agent decides');
});

test('plain keys refuse any ctrl or meta; letters respect shift, punctuation does not', () => {
  const w = kw();
  const log = rec(w, 'j');
  rec(w, '?');
  const K = w.get('Keymap');
  assert.equal(!!K.handle(ev('j', { ctrlKey: true })), false);
  assert.equal(!!K.handle(ev('j', { metaKey: true })), false);
  assert.equal(!!K.handle(ev('J', { shiftKey: true })), false, 'shift+j is not j');
  assert.ok(K.handle(ev('j')));
  assert.ok(K.handle(ev('?', { shiftKey: true })), '? needs shift on a US keyboard');
  assert.ok(K.handle(ev('?')), 'and still matches on layouts where it does not');
  assert.deepEqual(log(), ['j', '?', '?']);
});

test('a handled key is prevented; a handler that returns false, or a failing when(), leaves the event alone', () => {
  const w = kw();
  rec(w, 'x');
  rec(w, 'y', {}, false);
  w.run("Keymap.bindKey('z', () => true, { when: () => false })");
  w.run("Keymap.bindKey('w', () => { throw new Error('boom'); })");
  const K = w.get('Keymap');
  const x = ev('x'); K.handle(x); assert.equal(x.prevented, true);
  const y = ev('y'); assert.equal(K.handle(y), null); assert.equal(y.prevented, false, 'the handler said "not mine"');
  const z = ev('z'); assert.equal(K.handle(z), null); assert.equal(z.prevented, false, 'when() is false');
  const wv = ev('w'); w.ctx.console = { error() {} }; assert.equal(K.handle(wv), null); assert.equal(wv.prevented, false, 'a throwing handler never wedges the page');
});

test('bindKey returns an unbind function', () => {
  const w = kw();
  rec(w, 'u');
  const off = w.run("Keymap.bindKey('v', () => true)");
  assert.equal(w.run('Keymap.list.length'), 2);
  off();
  assert.equal(w.run('Keymap.list.length'), 1);
  assert.equal(w.get('Keymap').handle(ev('v')), null);
});

// ---------------------------------------------------------------- chords

test("chords: 'g h' fires on the second key within 800 ms, never after, never after a stray key", () => {
  const w = kw();
  const log = rec(w, 'g h');
  const K = w.get('Keymap');
  const g = ev('g');
  assert.equal(K.handle(g), null);
  assert.equal(g.prevented, true, 'the first key of a chord is swallowed');
  at(w, 1500);
  assert.ok(K.handle(ev('h')), '500 ms later');
  assert.deepEqual(log(), ['g h']);
  at(w, 5000);
  K.handle(ev('g'));
  at(w, 5801);
  assert.equal(K.handle(ev('h')), null, '801 ms later is too late');
  assert.deepEqual(log(), ['g h']);
  at(w, 9000);
  K.handle(ev('g'));
  assert.equal(K.handle(ev('x')), null);
  assert.equal(K.handle(ev('h')), null, 'a stray key in between broke the chord');
  K.handle(ev('g'));
  K.handle(ev('Shift', { shiftKey: true }));
  assert.ok(K.handle(ev('h')), 'a bare modifier in between does not');
  assert.deepEqual(log(), ['g h', 'g h']);
});

test('a broken chord restarts with the key that broke it, and chords and single keys live side by side', () => {
  const w = kw();
  const log = rec(w, 'g h');
  rec(w, 'x');
  rec(w, 'g a');
  const K = w.get('Keymap');
  K.handle(ev('g'));
  assert.ok(K.handle(ev('x')), 'x after g is still x');
  K.handle(ev('g'));
  K.handle(ev('g'));
  assert.ok(K.handle(ev('a')), 'g g a: the second g starts afresh');
  assert.deepEqual(log(), ['x', 'g a']);
});

test('no handler on a chord whose first key is not allowed: g in an input is just typing', () => {
  const w = kw();
  const log = rec(w, 'g h');
  const K = w.get('Keymap');
  const g = ev('g', { target: input });
  K.handle(g);
  assert.equal(g.prevented, false);
  assert.ok(!K.handle(ev('h', { target: input })));
  assert.deepEqual(log(), []);
});

// ---------------------------------------------------------------- inert rules

test('inert in input, textarea, select and contenteditable; input: true opts a binding in', () => {
  const w = kw();
  const log = rec(w, 'j');
  rec(w, 'mod+k', { input: true });
  const K = w.get('Keymap');
  for (const t of [{ tagName: 'INPUT' }, { tagName: 'TEXTAREA' }, { tagName: 'SELECT' }, { tagName: 'DIV', isContentEditable: true }]) {
    const e = ev('j', { target: t });
    assert.equal(K.handle(e), null, t.tagName);
    assert.equal(e.prevented, false, `${t.tagName}: the key stays the field's`);
  }
  assert.ok(K.handle(ev('j', { target: { tagName: 'DIV' } })), 'a plain element is not editable');
  assert.ok(K.handle(ev('j', { target: { tagName: 'BUTTON' } })));
  assert.ok(K.handle(ev('k', { ctrlKey: true, target: input })), 'the palette toggle works from a field');
  assert.deepEqual(log(), ['j', 'j', 'mod+k']);
});

test('inert while a dialog is open, except bindings that name the dialog (true or an id list)', () => {
  const dialogs = [{ id: 'helpdlg' }];
  const w = kw({ dialogs });
  const log = rec(w, 'j');
  rec(w, 'mod+k', { dialog: true });
  rec(w, 'r', { dialog: ['sheet'] });
  const K = w.get('Keymap');
  assert.equal(K.handle(ev('j')), null, 'a modal palette or help swallows the page keys');
  assert.ok(K.handle(ev('k', { ctrlKey: true })), 'mod+k still toggles it');
  assert.equal(K.handle(ev('r')), null, 'the sheet-only binding does not run over another dialog');
  dialogs.length = 0;
  dialogs.push({ id: 'sheet' });
  assert.ok(K.handle(ev('r')), 'but runs over the peek sheet');
  assert.equal(K.handle(ev('j')), null);
  dialogs.push({ id: 'helpdlg' });
  assert.equal(K.handle(ev('r')), null, 'both open: the palette wins');
  dialogs.length = 0;
  assert.ok(K.handle(ev('j')));
  assert.deepEqual(log(), ['mod+k', 'r', 'j']);
});

test('the legacy modal (ui.modal) is a dialog too', () => {
  const w = kw();
  const log = rec(w, 'j');
  w.run('globalThis.ui = { modal: true }');
  assert.equal(w.get('Keymap').handle(ev('j')), null);
  w.run('ui.modal = false');
  assert.ok(w.get('Keymap').handle(ev('j')));
  assert.deepEqual(log(), ['j']);
});

test('inert when focus is in an iframe: the terminal owns its keys, mod+k included', () => {
  const frame = { tagName: 'IFRAME' };
  let w = kw({ active: frame });
  let log = rec(w, 'j');
  rec(w, 'mod+k', { input: true, dialog: true });
  assert.equal(w.get('Keymap').handle(ev('j')), null, 'activeElement is the iframe');
  assert.equal(w.get('Keymap').handle(ev('k', { ctrlKey: true })), null);
  w = kw();
  log = rec(w, 'j');
  assert.equal(w.get('Keymap').handle(ev('j', { target: frame })), null, 'the event target is the iframe');
  assert.ok(w.get('Keymap').handle(ev('j')));
  assert.deepEqual(log(), ['j']);
});

test('already handled, composing and held-down events are skipped; repeat: true opts in (j / k)', () => {
  const w = kw();
  const log = rec(w, 'a');
  rec(w, 'j', { repeat: true });
  const K = w.get('Keymap');
  assert.equal(K.handle(ev('a', { defaultPrevented: true })), null, 'the shell or a widget already took it');
  assert.equal(K.handle(ev('a', { isComposing: true })), null, 'an IME composition');
  assert.equal(K.handle(ev('a', { repeat: true })), null, 'holding a must not acknowledge the whole list');
  assert.ok(K.handle(ev('j', { repeat: true })), 'holding j keeps scrolling the selection');
  assert.ok(K.handle(ev('a')));
  assert.deepEqual(log(), ['j', 'a']);
});

// ---------------------------------------------------------------- the document listener

test('listen() installs one keydown listener on document, install() is idempotent, uninstall() removes it', () => {
  const w = kw();
  const log = rec(w, 'x');
  assert.equal((w.document.listeners.keydown || []).length, 0);
  w.run('Keymap.listen(); Keymap.listen();');
  assert.equal(w.document.listeners.keydown.length, 1);
  const e = ev('x');
  w.document.dispatch('keydown', e);
  assert.deepEqual(log(), ['x'], 'a real document keydown reaches the binding');
  w.run('Keymap.uninstall()');
  assert.equal(w.document.listeners.keydown.length, 0);
  w.document.dispatch('keydown', ev('x'));
  assert.deepEqual(log(), ['x']);
});

// ---------------------------------------------------------------- help

test('help(): only bindings with a help text, in registration order, with the platform key names', () => {
  for (const [platform, k, jump] of [[MAC, '⌘K', '⌘1…9'], [LINUX, 'Ctrl+K', 'Ctrl+1…9']]) {
    const w = kw({ platform });
    w.run(`
      Keymap.bindKey('mod+k', () => true, { help: 'Palette', group: 'General' });
      Keymap.bindKey('g h', () => true, { help: 'Go home', group: 'Go to' });
      Keymap.bindKey('x', () => true);
      Keymap.bindKey('mod+1', () => true, { help: 'Open the nth', label: 'mod+1…9', group: 'Sessions' });
      Keymap.bindKey('Enter', () => true, { help: 'Open', group: 'Sessions' });
      Keymap.bindKey('Esc', () => true, { help: 'Close' });
    `);
    const help = plain(w.get('Keymap').help());
    assert.deepEqual(help.map((h) => h.help), ['Palette', 'Go home', 'Open the nth', 'Open', 'Close'], 'no row for the binding without help');
    assert.deepEqual(help.map((h) => h.keys), [[k], ['g', 'h'], [jump], ['↵'], ['Esc']], platform);
    assert.equal(help[1].text, 'g then h');
    assert.deepEqual(help.map((h) => h.group), ['General', 'Go to', 'Sessions', 'Sessions', 'General']);
  }
});

// ---------------------------------------------------------------- the app's bindings

/** install() with stand-ins for everything the defaults reach: Pages, Palette, Shell, navigate, openPage. */
function appWorld({ platform = LINUX, active = true, target = 'shop--api--s1', selected = true, perm = true, dialogs = [], activeEl = null, peek = null } = {}) {
  const w = kw({ platform, dialogs, active: activeEl });
  w.run(`
    globalThis.__log = [];
    globalThis.navigate = (h) => __log.push('go ' + h);
    globalThis.Palette = { toggle: () => __log.push('palette'), openHelp: () => __log.push('help') };
    globalThis.Shell = { toggleSidebar: () => __log.push('sidebar'), toggleDock: () => __log.push('dock'), focusSearch: () => __log.push('search'), openCreate: (k) => __log.push('create ' + k) };
    globalThis.Pages = {
      active: () => ${active}, target: () => ${JSON.stringify(target)}, peekTmux: () => ${JSON.stringify(peek)}, selected: () => ${selected ? '({ tmux: "shop--api--s1" })' : 'null'}, targetPerm: () => ${perm ? '({ id: 7 })' : 'null'},
      select: (d) => { __log.push('select ' + d); return true; }, clear: () => __log.push('clear'),
      openNth: (n) => { __log.push('nth ' + n); return n <= 3; }, act: (what) => { __log.push('act ' + what); return Promise.resolve(true); },
    };
    Keymap.install();
  `);
  const log = () => plain(w.get('__log'));
  const press = (key, mods) => { const e = ev(key, mods); w.document.dispatch('keydown', e); return e; };
  return { w, log, press };
}

test('install(): mod+k toggles the palette, ? opens the help, from the right modifier only', () => {
  const { w, log, press } = appWorld({ platform: MAC });
  press('k', { ctrlKey: true });
  assert.deepEqual(log(), [], 'Ctrl+K is not the Mac palette key');
  press('k', { metaKey: true });
  press('?', { shiftKey: true });
  assert.deepEqual(log(), ['palette', 'help']);
  assert.equal(w.run('Keymap.installed'), true);
});

test('install(): mod+1..9 open the nth session; with no nth session the key is left to the browser', () => {
  const { log, press } = appWorld();
  press('1', { ctrlKey: true });
  const e = press('9', { ctrlKey: true });
  assert.deepEqual(log(), ['nth 1', 'nth 9']);
  assert.equal(e.prevented, false, 'there is no 9th session: Ctrl+9 stays the browser tab shortcut');
});

test('install(): mod+1..9 hop between peeks over the peek sheet, and stay inert over any other dialog or a sheet that is not a peek', () => {
  let a = appWorld({ dialogs: [{ id: 'sheet' }], peek: 'shop--api--s1' });
  a.press('2', { ctrlKey: true });
  assert.deepEqual(a.log(), ['nth 2'], 'the sheet shows a peek: jump to the other session');
  a = appWorld({ dialogs: [{ id: 'sheet' }], peek: null });
  a.press('2', { ctrlKey: true });
  assert.deepEqual(a.log(), [], 'a launcher form in the sheet is not abandoned by a shortcut');
  a = appWorld({ dialogs: [{ id: 'helpdlg' }], peek: 'shop--api--s1' });
  a.press('2', { ctrlKey: true });
  assert.deepEqual(a.log(), [], 'the palette is modal');
  a = appWorld({ dialogs: [{ id: 'sheet' }, { id: 'drawer' }], peek: 'shop--api--s1' });
  a.press('2', { ctrlKey: true });
  assert.deepEqual(a.log(), []);
});

test('install(): mod+\\ and [ toggle the sidebar, mod+j the dock, / the search', () => {
  const { log, press } = appWorld();
  press('\\', { ctrlKey: true });
  press('[');
  press('j', { ctrlKey: true });
  press('/');
  assert.deepEqual(log(), ['sidebar', 'sidebar', 'dock', 'search']);
});

test('install(): g chords go to the routes', () => {
  const { log, press } = appWorld();
  for (const k of ['h', 'i', 'a', 't', 'u', 'm', 's']) { press('g'); press(k); }
  assert.deepEqual(log(), ['go #/', 'go #/inbox', 'go #/agents', 'go #/tasks', 'go #/usage', 'go #/memory', 'go #/settings']);
});

test('install(): g q goes to the quad view', () => {
  const { log, press, w } = appWorld();
  press('g'); press('q');
  assert.deepEqual(log(), ['go #/quad']);
  const help = plain(w.run('Keymap.help()'));
  assert.ok(help.some((h) => h.text === 'g then q' && /Quad/i.test(h.help) && h.group === 'Go to'), 'g q is in the help list under Go to');
});

test('install(): g q opens the scope the quad was last used on when the shell knows one (Shell.quadHref), else all projects', () => {
  const a = appWorld();
  a.w.run("Shell.quadHref = () => '#/quad?p=ccboard'");
  a.press('g'); a.press('q');
  assert.deepEqual(a.log(), ['go #/quad?p=ccboard']);
  const b = appWorld();
  b.press('g'); b.press('q');
  assert.deepEqual(b.log(), ['go #/quad'], 'a shell without quadHref (a partial deploy): the plain route');
  const c = appWorld();
  c.press('g'); c.press('h');
  assert.deepEqual(c.log(), ['go #/'], 'other chords are untouched');
});

test('install(): c then s / t / p / r open the create sheets (session, task, project, import) through Shell.openCreate', () => {
  const { log, press, w } = appWorld();
  for (const k of ['s', 't', 'p', 'r']) { press('c'); press(k); }
  assert.deepEqual(log(), ['create session', 'create task', 'create project', 'create import']);
  const help = plain(w.run('Keymap.help()'));
  for (const [k, what] of [['s', /session/i], ['t', /task/i], ['p', /project/i], ['r', /import|repo/i]]) {
    const row = help.find((h) => h.text === `c then ${k}`);
    assert.ok(row && what.test(row.help), `c then ${k} is in the help list (${row && row.help})`);
    assert.equal(row.group, help.find((h) => h.text === 'c then s').group, 'one group for the create chords');
  }
});

test('install(): the c chords obey the chord rules: 800 ms window, broken chord, inputs, dialogs, modifiers', () => {
  const a = appWorld();
  const { w, log, press } = a;
  const K = w.get('Keymap');
  at(w, 1000); press('c'); at(w, 1700); press('s');
  assert.deepEqual(log(), ['create session'], 'within 800 ms');
  at(w, 5000); press('c'); at(w, 5900); press('s');
  assert.deepEqual(log(), ['create session'], 'after 800 ms the chord has lapsed and s alone is nothing');
  at(w, 9000); press('c'); press('x'); press('s');
  assert.deepEqual(log(), ['create session'], 'a stray key breaks the chord');
  const e = ev('c', { ctrlKey: true });
  assert.equal(K.handle(e), null, 'Ctrl+C / Cmd+C stays the browser copy');
  assert.equal(e.prevented, false);
  for (const k of ['c', 's']) w.document.dispatch('keydown', ev(k, { target: input }));
  assert.deepEqual(log(), ['create session'], 'typing "cs" in an input is just typing');
  const d = appWorld({ dialogs: [{ id: 'helpdlg' }] });
  d.press('c'); d.press('p');
  assert.deepEqual(d.log(), [], 'no create sheet over an open dialog');
});

test('install(): m is not bound (move arrives with drag and drop in v0.5.15) and none of the chords swallow a plain key', () => {
  const { w, log, press } = appWorld();
  const specs = plain(w.run('Keymap.list.map((b) => b.spec)'));
  assert.equal(specs.includes('m'), false);
  for (const s of ['g q', 'c s', 'c t', 'c p', 'c r']) assert.ok(specs.includes(s), `binding ${s}`);
  const e = press('m');
  assert.equal(e.prevented, false);
  assert.deepEqual(log(), []);
  const q = press('q');
  assert.equal(q.prevented, false, 'q alone is not the start of anything');
  assert.deepEqual(log(), []);
});

test('install(): j and k move the selection only where a list is on screen', () => {
  let a = appWorld({ active: true });
  a.press('j'); a.press('k');
  assert.deepEqual(a.log(), ['select 1', 'select -1']);
  a = appWorld({ active: false });
  const e = a.press('j');
  assert.deepEqual(a.log(), []);
  assert.equal(e.prevented, false);
});

test('install(): Enter opens the selection but never steals Enter from a focused button or link', () => {
  const a = appWorld({ selected: true });
  a.press('Enter');
  assert.deepEqual(a.log(), ['act peek']);
  const onButton = appWorld({ selected: true });
  const e = ev('Enter', { target: { tagName: 'BUTTON', closest: (s) => (/button/.test(s) ? {} : null) } });
  onButton.w.document.dispatch('keydown', e);
  assert.deepEqual(onButton.log(), [], 'the button gets its own Enter');
  const none = appWorld({ selected: false });
  none.press('Enter');
  assert.deepEqual(none.log(), []);
});

test('install(): o a r act on the target; y and d only with a permission pending', () => {
  let a = appWorld({ perm: true });
  for (const k of ['o', 'a', 'y', 'd', 'r']) a.press(k);
  assert.deepEqual(a.log(), ['act term', 'act ack', 'act allow', 'act deny', 'act reply']);
  a = appWorld({ perm: false });
  for (const k of ['o', 'a', 'y', 'd']) a.press(k);
  assert.deepEqual(a.log(), ['act term', 'act ack'], 'allow and deny need something to answer');
  a = appWorld({ target: null, selected: false, perm: false });
  for (const k of ['o', 'a', 'r']) a.press(k);
  assert.deepEqual(a.log(), [], 'no target: nothing to act on');
});

test('install(): over the peek sheet o a y d r still work; over the palette or drawer they do not; j k never', () => {
  let a = appWorld({ dialogs: [{ id: 'sheet' }] });
  for (const k of ['a', 'r', 'j']) a.press(k);
  assert.deepEqual(a.log(), ['act ack', 'act reply']);
  a = appWorld({ dialogs: [{ id: 'helpdlg' }] });
  for (const k of ['a', 'r', 'j']) a.press(k);
  a.press('k', { ctrlKey: true });
  assert.deepEqual(a.log(), ['palette']);
  a = appWorld({ dialogs: [{ id: 'drawer' }] });
  a.press('a');
  assert.deepEqual(a.log(), []);
});

test('install(): Esc clears the selection when nothing modal is open and is never swallowed', () => {
  let a = appWorld();
  let e = a.press('Escape');
  assert.deepEqual(a.log(), ['clear']);
  assert.equal(e.prevented, false, 'the native <dialog> closes itself on Esc');
  a = appWorld({ dialogs: [{ id: 'helpdlg' }] });
  e = a.press('Escape');
  assert.deepEqual(a.log(), [], 'a dialog is open: leave the selection');
  assert.equal(e.prevented, false);
});

test('install(): nothing fires in an input, and the registered set is the documented one', () => {
  const a = appWorld();
  for (const k of ['j', 'k', 'a', 'r', 'o']) a.w.document.dispatch('keydown', ev(k, { target: input }));
  assert.deepEqual(a.log(), []);
  const specs = plain(a.w.run('Keymap.list.map((b) => b.spec)'));
  for (const s of ['mod+k', '?', '/', 'Esc', '[', 'mod+\\', 'mod+j', 'mod+1', 'mod+9', 'g h', 'g i', 'g a', 'g t', 'g u', 'g m', 'g s', 'j', 'k', 'Enter', 'o', 'a', 'y', 'd', 'r']) {
    assert.ok(specs.includes(s), `binding ${s}`);
  }
  const help = plain(a.w.run('Keymap.help()'));
  assert.ok(help.some((h) => h.keys[0] === 'Ctrl+K'), 'the help dialog lists the palette key');
  assert.ok(help.some((h) => h.text === 'g then a' && /Agents/.test(h.help)));
  assert.equal(help.filter((h) => /1…9/.test(h.keys[0])).length, 1, 'mod+1..9 is one help row, not nine');
});

test('install(): F (full screen) and Z (zoom) belong to the quad page: they call it while it is mounted and are left alone otherwise', () => {
  const a = appWorld();
  a.press('f');
  a.press('z');
  assert.deepEqual(a.log(), [], 'no quad mounted (no Quad at all): neither key is taken');
  a.w.run(`globalThis.Quad = { current: null };`);
  assert.equal(a.press('f').prevented, false, 'Quad exists but no page is mounted');
  a.w.run(`globalThis.Quad = { current: { toggleFullscreen: () => { __log.push('fullscreen'); return true; }, zoomActive: () => { __log.push('zoom'); return __zoomable; } } }; globalThis.__zoomable = true;`);
  let e = a.press('f');
  assert.equal(e.prevented, true, 'the page took F');
  e = a.press('z');
  assert.equal(e.prevented, true);
  assert.deepEqual(a.log(), ['fullscreen', 'zoom']);
  a.w.run('__zoomable = false');
  e = a.press('z');
  assert.equal(e.prevented, false, 'nothing to zoom (one tile): the page says no and the key is left alone');
  // not the modified forms: Ctrl+Alt+F and Ctrl+Alt+Z are the page's own capture listener's, and a bare Shift+F is not F
  const before = a.log().length;
  a.press('f', { ctrlKey: true, altKey: true });
  a.press('F', { shiftKey: true });
  a.press('f', { metaKey: true });
  assert.equal(a.log().length, before, 'only the plain key');
});

test('install(): F and Z are inert in a field, over a dialog and with a terminal iframe focused', () => {
  const stub = `globalThis.Quad = { current: { toggleFullscreen: () => { __log.push('fullscreen'); return true; }, zoomActive: () => { __log.push('zoom'); return true; } } };`;
  let a = appWorld();
  a.w.run(stub);
  for (const target of [{ tagName: 'INPUT' }, { tagName: 'TEXTAREA' }, { tagName: 'SELECT' }, { isContentEditable: true }]) { a.w.document.dispatch('keydown', ev('f', { target })); a.w.document.dispatch('keydown', ev('z', { target })); }
  assert.deepEqual(a.log(), [], 'a field keeps its letters');
  a = appWorld({ dialogs: [{ id: 'sheet' }] });
  a.w.run(stub);
  a.press('f');
  a.press('z');
  assert.deepEqual(a.log(), [], 'a dialog (the tile menu sheet, the tune sheet, the palette) is modal');
  a = appWorld({ activeEl: { tagName: 'IFRAME' } });
  a.w.run(stub);
  a.press('f');
  assert.deepEqual(a.log(), [], 'the terminal owns its keys');
  a = appWorld();
  a.w.run(stub);
  a.w.document.dispatch('keydown', ev('f', { repeat: true }));
  assert.deepEqual(a.log(), [], 'a held key is one press at most');
});

test('install(): the help dialog lists F and Z under Quad, once each', () => {
  const a = appWorld();
  const help = plain(a.w.run('Keymap.help()')).filter((h) => h.group === 'Quad');
  assert.deepEqual(help.map((h) => [h.keys[0], h.help]), [['F', 'Quad: full screen on or off'], ['Z', 'Quad: zoom the active tile']]);
  const specs = plain(a.w.run('Keymap.list.map((b) => b.spec)'));
  assert.ok(specs.includes('f') && specs.includes('z'));
  assert.equal(specs.filter((s) => s === 'f').length, 1);
});

test('the defaults tolerate a partial deploy: no Pages, Palette or Shell, nothing throws', () => {
  const w = kw();
  w.run('Keymap.install()');
  w.ctx.console = { error() {} };
  for (const [k, mods] of [['k', { ctrlKey: true }], ['?', { shiftKey: true }], ['/', {}], ['[', {}], ['1', { ctrlKey: true }], ['j', {}], ['Enter', {}], ['a', {}], ['c', {}], ['s', {}], ['g', {}], ['q', {}], ['f', {}], ['z', {}]]) {
    assert.doesNotThrow(() => w.document.dispatch('keydown', ev(k, mods)), k);
  }
});
