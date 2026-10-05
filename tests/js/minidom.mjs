// A small but faithful DOM for the vm harness (tree, selectors, events, dialogs, focus), shared by keymap.test.mjs and palette.test.mjs.
// Copied from the one in pages.test.mjs, with dialogs that carry an `open` attribute so `dialog[open]` selects them.
//
//   const w = makeWorld(); const dom = installDom(w);   // w.document now has a body with the shell skeleton (#page, #sheet, #helpdlg, ...)
// ---------------------------------------------------------------- a small DOM

class MText {
  constructor(t) { this.nodeType = 3; this._t = String(t); this.parentNode = null; }
  get textContent() { return this._t; }
  set textContent(v) { this._t = String(v); }
}

const COMPOUND = /#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:([\^$*]?=)"?([^\]"]*)"?)?\]/g;

function parseCompound(s) {
  const m = /^([a-zA-Z*][\w-]*)?(.*)$/.exec(s);
  const c = { tag: m[1] && m[1] !== '*' ? m[1].toUpperCase() : null, id: null, classes: [], attrs: [] };
  for (const x of m[2].matchAll(COMPOUND)) {
    if (x[1]) c.id = x[1];
    else if (x[2]) c.classes.push(x[2]);
    else c.attrs.push({ name: x[3], op: x[4] || null, value: x[5] });
  }
  return c;
}

function matchCompound(n, c) {
  if (n.nodeType !== 1) return false;
  if (c.tag && n.tagName !== c.tag) return false;
  if (c.id && n.getAttribute('id') !== c.id) return false;
  for (const k of c.classes) if (!n.classList.contains(k)) return false;
  for (const a of c.attrs) {
    const v = n.getAttribute(a.name);
    if (v === null) return false;
    if (a.op === '=' && v !== a.value) return false;
    if (a.op === '^=' && !v.startsWith(a.value)) return false;
    if (a.op === '$=' && !v.endsWith(a.value)) return false;
    if (a.op === '*=' && !v.includes(a.value)) return false;
  }
  return true;
}

function matchSelector(n, sel) {                       // one selector with descendant combinators
  const chain = sel.trim().split(/\s+/).map(parseCompound);
  if (!matchCompound(n, chain[chain.length - 1])) return false;
  let at = n.parentNode;
  for (let i = chain.length - 2; i >= 0; i--) {
    while (at && !matchCompound(at, chain[i])) at = at.parentNode;
    if (!at) return false;
    at = at.parentNode;
  }
  return true;
}

function matchesAny(n, list) { return list.split(',').some((s) => matchSelector(n, s)); }

export function installDom(w) {
  let active = null;
  class El {
    constructor(tag) {
      this.nodeType = 1; this.tagName = String(tag).toUpperCase(); this.childNodes = []; this.parentNode = null;
      this._attrs = new Map(); this._cls = new Set(); this._on = {}; this.style = {}; this.dataset = {}; this.open = false;
      this.scrollTop = 0; this.disabled = false; this.value = '';
      const self = this;
      this.classList = {
        add(...c) { c.forEach((x) => self._cls.add(x)); },
        remove(...c) { c.forEach((x) => self._cls.delete(x)); },
        toggle(c, force) { const on = force === undefined ? !self._cls.has(c) : !!force; if (on) self._cls.add(c); else self._cls.delete(c); return on; },
        contains(c) { return self._cls.has(c); },
      };
    }
    get className() { return [...this._cls].join(' '); }
    set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
    setAttribute(k, v) { if (k === 'class') this.className = v; else this._attrs.set(k, String(v)); }
    getAttribute(k) { if (k === 'class') return this._cls.size ? this.className : null; return this._attrs.has(k) ? this._attrs.get(k) : null; }
    removeAttribute(k) { this._attrs.delete(k); }
    hasAttribute(k) { return this._attrs.has(k); }
    get id() { return this.getAttribute('id') || ''; }
    get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
    get firstChild() { return this.childNodes[0] || null; }
    get firstElementChild() { return this.children[0] || null; }
    get childElementCount() { return this.children.length; }
    get nextSibling() { const s = this.parentNode ? this.parentNode.childNodes : []; return s[s.indexOf(this) + 1] || null; }
    get nextElementSibling() { const s = this.parentNode ? this.parentNode.children : []; return s[s.indexOf(this) + 1] || null; }
    get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === w.document.documentElement; }
    _detach(n) { if (n.parentNode) n.parentNode.childNodes.splice(n.parentNode.childNodes.indexOf(n), 1); n.parentNode = null; }
    append(...kids) {
      for (const k of kids) {
        const n = typeof k === 'string' ? new MText(k) : k;
        this._detach(n); n.parentNode = this; this.childNodes.push(n);
      }
    }
    appendChild(n) { this.append(n); return n; }
    insertBefore(n, ref) {
      this._detach(n);
      if (!ref) { this.append(n); return n; }
      n.parentNode = this; this.childNodes.splice(this.childNodes.indexOf(ref), 0, n);
      return n;
    }
    removeChild(n) { this._detach(n); return n; }
    remove() { this._detach(this); }
    get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }
    set textContent(v) {
      for (const n of this.childNodes) n.parentNode = null;
      this.childNodes = [];
      if (String(v) !== '') this.append(String(v));
    }
    contains(n) { while (n) { if (n === this) return true; n = n.parentNode; } return false; }
    querySelectorAll(sel) {
      const out = [];
      const walk = (n) => { for (const c of n.children) { if (matchesAny(c, sel)) out.push(c); walk(c); } };
      walk(this);
      return out;
    }
    querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
    matches(sel) { return matchesAny(this, sel); }
    closest(sel) { let n = this; while (n && n.nodeType === 1) { if (matchesAny(n, sel)) return n; n = n.parentNode; } return null; }
    addEventListener(type, fn) { (this._on[type] ||= []).push(fn); }
    removeEventListener(type, fn) { this._on[type] = (this._on[type] || []).filter((f) => f !== fn); }
    dispatchEvent(ev) {
      ev.target = ev.target || this;
      for (let n = this; n && n.nodeType === 1 && !ev._stopped; n = n.parentNode) {
        ev.currentTarget = n;
        for (const fn of [...(n._on[ev.type] || [])]) fn(ev);
        if (ev.type === 'close') break;
      }
      return true;
    }
    click() {
      if (this.disabled) return;
      this.dispatchEvent({ type: 'click', preventDefault() {}, stopPropagation() { this._stopped = true; } });
    }
    focus() { active = this; }
    blur() { if (active === this) active = null; }
    showModal() { this.open = true; this.setAttribute('open', ''); }
    close() { if (!this.open) return; this.open = false; this.removeAttribute('open'); this.dispatchEvent({ type: 'close' }); }
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }; }
  }
  const doc = w.document;
  const html = new El('html');
  const body = new El('body');
  html.append(body);
  Object.assign(doc, {
    createElement: (t) => new El(t), createElementNS: (_ns, t) => new El(t), createTextNode: (t) => new MText(t),
    body, documentElement: html,
    querySelector: (s) => body.querySelector(s), querySelectorAll: (s) => body.querySelectorAll(s),
    getElementById: (id) => body.querySelector('#' + id),
  });
  Object.defineProperty(doc, 'activeElement', { get: () => active, configurable: true });
  const mk = (tag, id, cls) => { const n = new El(tag); n.setAttribute('id', id); if (cls) n.className = cls; return n; };
  const main = mk('main', 'main');
  main.append(mk('div', 'banner'), mk('div', 'page'));
  body.append(mk('header', 'topbar'), mk('aside', 'sidebar'), main, mk('aside', 'dock', 'hidden'), mk('nav', 'bnav'),
    mk('dialog', 'drawer'), mk('dialog', 'sheet'), mk('dialog', 'helpdlg'), mk('div', 'toasts'));
  return { El, body, page: () => doc.querySelector('#page') };
}

