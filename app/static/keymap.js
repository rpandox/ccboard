/* ccboard keymap (v0.5.3b): one document keydown listener and a table of bindings. Definition only at load: nothing here touches the
   DOM, storage or a timer until main.js calls Keymap.install() (bindDefaults() + the listener). Classic script, one namespace (Keymap).

   A spec is one key ('j', '?', '/', '[', 'Enter', 'Esc'), a modified key ('mod+k', 'mod+1', 'mod+\\', 'mod+enter'; mod is the Command key on
   macOS and iPadOS and Ctrl elsewhere, read from navigator.platform on every event) or a chord of keys typed within 800 ms ('g h').
   Keymap.bindKey(spec, handler, { when?, help?, group?, label?, dialog?, input?, repeat? }) returns an unbind function. The handler gets
   (event, binding); it returns false to say "not mine" (the event is then left alone), anything else counts as handled and the event's
   default is prevented.

   Inert on purpose, so the page never steals a key from what owns it:
     - the target is an input, textarea, select or contenteditable (opt in per binding with input: true: only mod+k does);
     - a <dialog> is open or the legacy login/diff modal is up (opt in with dialog: true, or an array of dialog ids such as ['sheet']);
     - focus is in an iframe: the terminal owns its keys;
     - the event was already handled (defaultPrevented) or is part of an IME composition;
     - the key is held down (event.repeat) unless the binding says repeat: true (j and k): holding a would acknowledge the whole list.
   Keymap.help() lists the bindings that carry a help text, for the dialog Palette.openHelp() draws. */
'use strict';

const Keymap = {
  list: [],               // registered bindings, in registration order: { spec, steps, handler, when, help, group, label, dialog, input }
  pending: null,          // { keys: [stepKey, ...], at } while a chord is half typed
  installed: false,
  defaultsDone: false,
  listener: null,
  CHORD_MS: 800,
  NAMES: { esc: 'escape', return: 'enter', space: ' ', up: 'arrowup', down: 'arrowdown', left: 'arrowleft', right: 'arrowright' },
  MODIFIER_KEYS: ['shift', 'control', 'alt', 'meta', 'altgraph', 'capslock', 'os', 'fn', 'dead'],
};

Keymap.now = function () { return Date.now(); };

/* Command on Mac and iPad (iPadOS reports MacIntel), Ctrl everywhere else. Read per call: navigator is not touched at load. */
Keymap.isMac = function () {
  try { return /Mac|iP(?:hone|ad|od)/i.test((navigator && (navigator.platform || navigator.userAgent)) || ''); } catch (_) { return false; }
};

Keymap.parse = function (spec) {
  const parts = String(spec === null || spec === undefined ? '' : spec).trim().split(/\s+/).filter(Boolean);
  if (!parts.length || parts.length > 3) throw new Error('bad key spec: ' + spec);
  return parts.map((part) => {
    const bits = part.split('+');
    let key = bits.pop().toLowerCase();
    if (!key && part.endsWith('++')) key = '+';
    if (Object.prototype.hasOwnProperty.call(Keymap.NAMES, key)) key = Keymap.NAMES[key];
    const step = { mod: false, shift: false, alt: false, key };
    for (const m of bits) {
      const low = m.toLowerCase();
      if (low === 'mod') step.mod = true;
      else if (low === 'shift') step.shift = true;
      else if (low === 'alt') step.alt = true;
      else throw new Error('bad key spec: ' + spec);
    }
    if (!step.key) throw new Error('bad key spec: ' + spec);
    return step;
  });
};

Keymap.stepKey = function (s) { return (s.mod ? 'M' : '') + (s.shift ? 'S' : '') + (s.alt ? 'A' : '') + ':' + s.key; };

Keymap.eventKey = function (e) { return e && typeof e.key === 'string' ? e.key.toLowerCase() : ''; };

/* Does the event press this step? Letters, digits and named keys must match shift exactly ('J' is not 'j'); punctuation ignores it ('?' needs shift). */
Keymap.matches = function (e, step) {
  if (Keymap.eventKey(e) !== step.key) return false;
  const mac = Keymap.isMac();
  const mod = mac ? !!e.metaKey : !!e.ctrlKey;
  const other = mac ? !!e.ctrlKey : !!e.metaKey;
  if (step.mod) { if (!mod || other) return false; } else if (e.ctrlKey || e.metaKey) return false;
  if (!!e.altKey !== step.alt) return false;
  const punctuation = step.key.length === 1 && !/[a-z0-9]/.test(step.key);
  if (!punctuation && !!e.shiftKey !== step.shift) return false;
  return true;
};

Keymap.bindKey = function (spec, handler, opts) {
  if (typeof handler !== 'function') throw new Error('bad key handler: ' + spec);
  const o = opts || {};
  const b = { spec: String(spec), steps: Keymap.parse(spec), handler, when: typeof o.when === 'function' ? o.when : null, help: o.help ? String(o.help) : '',
    group: o.group ? String(o.group) : 'General', label: o.label ? String(o.label) : '', dialog: o.dialog === true || Array.isArray(o.dialog) ? o.dialog : false, input: !!o.input, repeat: !!o.repeat };
  Keymap.list.push(b);
  return () => { const i = Keymap.list.indexOf(b); if (i >= 0) Keymap.list.splice(i, 1); };
};

/* Test and teardown helper: drop every binding and the half-typed chord. */
Keymap.reset = function () { Keymap.list.length = 0; Keymap.pending = null; Keymap.defaultsDone = false; };

Keymap.inEditable = function (t) {
  return !!(t && (t.isContentEditable || /^(?:INPUT|TEXTAREA|SELECT)$/.test(String(t.tagName || ''))));
};

/* Focus inside a keyboard chart (charts.js Charts._kbd marks its root data-kbd="chart"): the chart answers the arrows, Home, End and Esc itself, so the page's plain
   keys (j, k, f, g chords, Esc clearing the selection) stay quiet there, as in a field. A binding that says input: true (the palette) still works. */
Keymap.inChart = function (t) {
  return !!(t && typeof t.closest === 'function' && t.closest('[data-kbd=chart]'));
};

Keymap.inIframe = function (e) {
  const a = typeof document !== 'undefined' ? document.activeElement : null;
  return !!((e && e.target && e.target.tagName === 'IFRAME') || (a && a.tagName === 'IFRAME'));
};

/* Ids of the open modal surfaces: every <dialog open> plus 'modal' for the legacy #modal (ui.modal). */
Keymap.dialogsOpen = function () {
  const ids = [];
  try { for (const d of Array.from(document.querySelectorAll('dialog[open]'))) ids.push(d.id || 'dialog'); } catch (_) { /* no document */ }
  try { if (typeof ui !== 'undefined' && ui && ui.modal) ids.push('modal'); } catch (_) { /* no ui */ }
  return ids;
};

/* Focus on something that acts on Enter itself (a button, a link, a tab): a global Enter binding must leave it alone. */
Keymap.onInteractive = function (e) {
  const t = (e && e.target) || (typeof document !== 'undefined' ? document.activeElement : null);
  if (!t || typeof t.closest !== 'function') return false;
  return !!t.closest('button, a[href], summary, [role=button], [role=link], [role=tab], [role=menuitem], [role=treeitem], [role=option]');
};

Keymap.allowed = function (b, e, dialogs, editable) {
  if (editable && !b.input) return false;
  if (e.repeat && !b.repeat) return false;
  if (dialogs.length) {
    if (b.dialog !== true && !(Array.isArray(b.dialog) && dialogs.every((id) => b.dialog.includes(id)))) return false;
  }
  if (b.when) { try { if (!b.when(e)) return false; } catch (err) { console.error('ccboard keymap when', b.spec, err); return false; } }
  return true;
};

/* The bindings the event could mean after `prefix` (the stepKeys of an earlier half of a chord): { done, more }. */
Keymap.candidates = function (e, prefix, dialogs, editable) {
  const done = [];
  const more = [];
  for (const b of Keymap.list) {
    if (b.steps.length <= prefix.length) continue;
    let ok = true;
    for (let i = 0; i < prefix.length; i++) if (Keymap.stepKey(b.steps[i]) !== prefix[i]) { ok = false; break; }
    if (!ok || !Keymap.matches(e, b.steps[prefix.length])) continue;
    if (!Keymap.allowed(b, e, dialogs, editable)) continue;
    (b.steps.length === prefix.length + 1 ? done : more).push(b);
  }
  return { done, more };
};

/* The keydown handler. Returns the binding that ran (or null), so the tests can ask. */
Keymap.handle = function (e) {
  if (!e || e.defaultPrevented || e.isComposing) return null;
  const key = Keymap.eventKey(e);
  if (!key || Keymap.MODIFIER_KEYS.includes(key)) return null;               // a bare Shift or Ctrl neither starts nor breaks a chord
  if (Keymap.inIframe(e)) { Keymap.pending = null; return null; }
  const now = Keymap.now();
  const prefix = Keymap.pending && now - Keymap.pending.at <= Keymap.CHORD_MS ? Keymap.pending.keys : [];
  Keymap.pending = null;
  const dialogs = Keymap.dialogsOpen();
  const editable = Keymap.inEditable(e.target) || Keymap.inChart(e.target);
  let eff = prefix;
  let c = Keymap.candidates(e, eff, dialogs, editable);
  if (eff.length && !c.done.length && !c.more.length) { eff = []; c = Keymap.candidates(e, eff, dialogs, editable); }   // a broken chord: this key starts afresh
  const first = c.done[0];
  if (first) {
    let r;
    try { r = first.handler(e, first); } catch (err) { console.error('ccboard keymap', first.spec, err); r = false; }
    if (r === false) return null;
    if (typeof e.preventDefault === 'function') e.preventDefault();
    return first;
  }
  if (c.more.length) {
    Keymap.pending = { keys: [...eff, Keymap.stepKey(c.more[0].steps[eff.length])], at: now };
    if (typeof e.preventDefault === 'function') e.preventDefault();
  }
  return null;
};

/* ---------- display, for the help dialog ---------- */

Keymap.KEY_NAMES = { enter: '↵', escape: 'Esc', arrowup: '↑', arrowdown: '↓', arrowleft: '←', arrowright: '→', ' ': 'Space' };

Keymap.stepText = function (s) {
  const mac = Keymap.isMac();
  const k = Object.prototype.hasOwnProperty.call(Keymap.KEY_NAMES, s.key) ? Keymap.KEY_NAMES[s.key] : (s.mod ? s.key.toUpperCase() : s.key);
  const parts = [];
  if (s.mod) parts.push(mac ? '⌘' : 'Ctrl');
  if (s.alt) parts.push(mac ? '⌥' : 'Alt');
  if (s.shift) parts.push(mac ? '⇧' : 'Shift');
  parts.push(k);
  return mac ? parts.join('') : parts.join('+');
};

/* A free-form label such as 'mod+1…9' with the platform's name for mod. */
Keymap.labelText = function (label) { return String(label).replace(/\bmod\+/g, Keymap.isMac() ? '⌘' : 'Ctrl+'); };

/* [{ spec, keys: ['g', 'h'], text: 'g then h', help, group }] for every binding that has a help text, in registration order. */
Keymap.help = function () {
  const out = [];
  for (const b of Keymap.list) {
    if (!b.help) continue;
    const keys = b.label ? [Keymap.labelText(b.label)] : b.steps.map(Keymap.stepText);
    out.push({ spec: b.spec, keys, text: keys.join(' then '), help: b.help, group: b.group });
  }
  return out;
};

Keymap.openHelp = function () {
  if (typeof Palette !== 'undefined' && typeof Palette.openHelp === 'function') { Palette.openHelp(); return true; }
  return false;
};

/* ---------- the app's own bindings ---------- */

Keymap.go = function (hash) { if (typeof navigate === 'function') navigate(hash); else location.hash = hash; };

Keymap.pages = function () { return typeof Pages !== 'undefined' ? Pages : null; };

Keymap.bindDefaults = function () {
  if (Keymap.defaultsDone) return;
  Keymap.defaultsDone = true;
  const B = Keymap.bindKey;
  const P = Keymap.pages;
  const listOn = () => !!(P() && P().active());
  const targetOn = () => !!(P() && P().target());
  const selectedOn = () => !!(P() && P().selected());
  const permOn = () => !!(P() && P().targetPerm());
  const act = (what) => () => {
    if (!P()) return false;
    const r = P().act(what);
    if (r && typeof r.catch === 'function') r.catch((err) => console.error('ccboard key', what, err));
    return true;
  };

  B('mod+k', () => { if (typeof Palette === 'undefined') return false; Palette.toggle(); return true; },
    { help: 'Command palette: sessions, routes, nudges, controls', group: 'General', dialog: true, input: true });
  B('?', () => { if (typeof Palette === 'undefined') return false; Palette.openHelp(); return true; }, { help: 'This list of shortcuts', group: 'General' });
  B('/', () => { if (typeof Shell === 'undefined' || typeof Shell.focusSearch !== 'function') return false; Shell.focusSearch(); return true; },
    { help: 'Search transcripts', group: 'General' });
  // Esc only reports itself: the native <dialog> closes the palette, sheet and drawer, and the list selection clears when nothing modal is open
  B('Esc', () => { if (P() && !Keymap.dialogsOpen().length) P().clear(); return false; }, { help: 'Close the palette, sheet or drawer; clear the selection', group: 'General', dialog: true });

  const sidebar = () => { if (typeof Shell === 'undefined' || typeof Shell.toggleSidebar !== 'function') return false; Shell.toggleSidebar(); return true; };
  B('[', sidebar, { help: 'Toggle the sidebar', label: '[ or mod+\\', group: 'Layout' });
  B('mod+\\', sidebar, {});
  B('mod+j', () => { if (typeof Shell === 'undefined' || typeof Shell.toggleDock !== 'function') return false; Shell.toggleDock(); return true; },
    { help: 'Toggle the terminal dock', group: 'Layout' });

  // Over the peek sheet (below 1024 px) mod+1..9 hops from one peek to another; over any other dialog (a launcher form in the sheet, the drawer, the palette) it is inert.
  const peekOpen = () => (Keymap.dialogsOpen().length ? !!(P() && P().peekTmux()) : true);
  for (let n = 1; n <= 9; n++) {
    B('mod+' + n, () => { if (!P()) return false; return P().openNth(n); },
      { when: peekOpen, dialog: ['sheet'], help: n === 1 ? 'Open the nth session of the Agents order' : '', label: n === 1 ? 'mod+1…9' : '', group: 'Sessions' });
  }

  for (const [k, hash, text] of [['h', '#/', 'Home'], ['i', '#/inbox', 'Needs you'], ['a', '#/agents', 'Agents'], ['t', '#/tasks', 'Tasks'],
    ['q', '#/quad', 'Quad (terminals side by side)'], ['u', '#/usage', 'Usage'], ['m', '#/memory', 'Memory'], ['s', '#/settings', 'Settings']]) {
    B('g ' + k, () => { Keymap.go(hash === '#/quad' && typeof Shell !== 'undefined' && Shell && typeof Shell.quadHref === 'function' ? Shell.quadHref() : hash); return true; }, { help: 'Go to ' + text, group: 'Go to' });      // the quad opens the scope last used
  }

  // c then s / t / p / r: the + menu's sheets (Shell.openCreate). A chord, so a lone c in a form still types a c. m (move) stays unbound until v0.5.15.
  const create = (kind) => () => {
    if (typeof Shell === 'undefined' || typeof Shell.openCreate !== 'function') return false;
    return Shell.openCreate(kind) !== false;
  };
  for (const [k, kind, text] of [['s', 'session', 'New session'], ['t', 'task', 'New task'], ['p', 'project', 'New project'], ['r', 'import', 'Import repos from GitHub']]) {
    B('c ' + k, create(kind), { help: text, group: 'Create' });
  }

  B('j', () => (P() ? P().select(1) : false), { when: listOn, repeat: true, help: 'Next session in the list', group: 'Sessions' });
  B('k', () => (P() ? P().select(-1) : false), { when: listOn, repeat: true, help: 'Previous session in the list', group: 'Sessions' });
  B('Enter', act('peek'), { when: (e) => selectedOn() && !Keymap.onInteractive(e), help: 'Open the selected session', group: 'Sessions' });
  const peekKeys = ['sheet'];                                                // the peek is a modal sheet below 1024 px: its own keys still work there
  B('o', act('term'), { when: targetOn, dialog: peekKeys, help: 'Open its terminal', group: 'Selected or open session' });
  B('a', act('ack'), { when: targetOn, dialog: peekKeys, help: 'Acknowledge', group: 'Selected or open session' });
  B('y', act('allow'), { when: permOn, dialog: peekKeys, help: 'Allow the pending permission', group: 'Selected or open session' });
  B('d', act('deny'), { when: permOn, dialog: peekKeys, help: 'Deny the pending permission', group: 'Selected or open session' });
  B('r', act('reply'), { when: targetOn, dialog: peekKeys, help: 'Reply: focus the send box', group: 'Selected or open session' });

  // The quad page (v0.5.9c): F is full screen and Z zooms the active tile, only while the quad is mounted (Quad.current); like every plain key they are inert in a field, in a
  // dialog and while a terminal iframe has the focus (Ctrl+Alt+F and Ctrl+Alt+Z, which the page binds itself, are the ones that work there). A page method that says
  // false (nothing to zoom) leaves the key alone.
  const quadOn = () => typeof Quad !== 'undefined' && !!Quad && !!Quad.current;
  const quadDo = (name) => () => (quadOn() && typeof Quad.current[name] === 'function' ? Quad.current[name]() !== false : false);
  B('f', quadDo('toggleFullscreen'), { when: quadOn, help: 'Quad: full screen on or off', label: 'F', group: 'Quad' });
  B('z', quadDo('zoomActive'), { when: quadOn, help: 'Quad: zoom the active tile', label: 'Z', group: 'Quad' });
};

Keymap.listen = function () {
  if (Keymap.installed || typeof document === 'undefined') return;
  Keymap.installed = true;
  Keymap.listener = (e) => Keymap.handle(e);
  document.addEventListener('keydown', Keymap.listener);
};

/* main.js: the bindings, then the one listener. Idempotent. */
Keymap.install = function () {
  Keymap.bindDefaults();
  Keymap.listen();
};

Keymap.uninstall = function () {
  if (Keymap.listener && typeof document !== 'undefined') document.removeEventListener('keydown', Keymap.listener);
  Keymap.listener = null;
  Keymap.installed = false;
  Keymap.pending = null;
};
